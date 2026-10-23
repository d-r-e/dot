"""Speech engine for the dot app (Balanced MLX profile).

Lazy-loads Silero VAD, Smart Turn, Nemotron 3.5 streaming ASR and Kokoro TTS
through mlx-audio. The engine is full-duplex aware: the VAD runs continuously
while the app is armed, so speech onset is detected while the agent is still
speaking.

All model work runs on one of two pinned threads: VAD/ASR on ``engine.executor``
and TTS on ``engine.tts_executor``. MLX GPU streams are thread-local, and
asyncio's default pool does not promise the same thread between calls, which
intermittently raises ``There is no Stream(gpu, N)``. Two threads (not one) keep
Kokoro synthesis from starving the ASR while the agent speaks, which is what
makes barge-in feel immediate.

Turn boundaries use two signals: Silero for onset, then a 300 ms pause plus
Smart Turn v3 on the utterance audio to decide whether the pause is a finished
thought or a mid-sentence breath. While a barge-in candidate is open the commit
decision belongs to the caller, so a cough or "mm-hmm" can resume the reply.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Deque, List, Optional

import numpy as np

VAD_MODEL = os.environ.get("VAD_MODEL", "mlx-community/silero-vad")
ASR_MODEL = os.environ.get(
    "ASR_MODEL", "mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit"
)
TTS_MODEL = os.environ.get("TTS_MODEL", "mlx-community/Kokoro-82M-8bit")
TTS_VOICE = os.environ.get("TTS_VOICE", "ef_dora")
TTS_LANG = os.environ.get("TTS_LANG", "e")
ASR_LANGUAGE = os.environ.get("ASR_LANGUAGE", "es-ES")
ASR_DELAY_MS = int(os.environ.get("ASR_DELAY_MS", "160"))
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.5"))
# A pause this long is a *candidate* turn end; Smart Turn then decides.
PAUSE_MS = int(os.environ.get("VAD_SILENCE_MS", "300"))
# Audio kept from before onset so the first phoneme is transcribed.
PREROLL_MS = int(os.environ.get("PREROLL_MS", "500"))
# Utterances shorter than this never commit on their own (coughs, clicks).
MIN_TURN_MS = int(os.environ.get("MIN_TURN_MS", "250"))
# How long an unfinished thought may hold the floor before we commit anyway.
TURN_MAX_WAIT_MS = int(os.environ.get("TURN_MAX_WAIT_MS", "2000"))
MAX_TURN_S = float(os.environ.get("MAX_TURN_S", "30"))
SEMANTIC_TURN = os.environ.get("SEMANTIC_TURN", "1") == "1"
TURN_MODEL = os.environ.get("TURN_MODEL", "mlx-community/smart-turn-v3")
TURN_THRESHOLD = float(os.environ.get("TURN_THRESHOLD", "0.6"))

SAMPLE_RATE = 16000
MAX_UTTERANCE_SAMPLES = 8 * SAMPLE_RATE


@dataclass(frozen=True)
class TurnSignal:
    """One state change emitted while feeding microphone audio.

    ``kind`` is one of ``speech_started``, ``speech_paused``, ``partial`` or
    ``committed``. ``text`` carries the partial transcript, the transcript at a
    pause, or the final transcript for a commit. ``ms`` is the speech duration
    of the utterance, excluding pre-roll; a short burst with a short transcript
    is a listener signal rather than a floor bid.
    """

    kind: str
    text: str = ""
    ms: int = 0


class VoiceEngine:
    def __init__(self) -> None:
        # MLX GPU streams are thread-local, so each model stays on one thread:
        # VAD/ASR on ``executor``, TTS on ``tts_executor``. asyncio.to_thread
        # hands calls to arbitrary pool threads and intermittently raises
        # "There is no Stream(gpu, N)".
        #
        # Two pinned threads (rather than one) matter for barge-in: with a
        # single worker, Kokoro synthesis of a reply starves the ASR queue for
        # over a second and the user's interruption is only transcribed after
        # they stop talking.
        self._asr_lock = threading.RLock()
        self._tts_lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dot-asr")
        self.tts_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dot-tts"
        )
        self._loaded = False
        self._load_error: Optional[str] = None
        self.vad = None
        self.asr = None
        self.tts = None
        self.turn_model = None
        self.sample_rate = 24000

        self._session = None
        self._partial = ""
        self._turn_open = False
        self._turn_started = 0.0
        self._awaiting_end: Optional[float] = None
        self._preroll: Deque[np.ndarray] = deque()
        self._preroll_samples = 0
        self._utterance: Deque[np.ndarray] = deque()
        self._utterance_samples = 0
        self._prefix_samples = 0
        self._TurnEventKind = None
        # Set after a discarded utterance: ignore onset until the speaker
        # actually pauses, so the tail of a backchannel cannot start a turn.
        self._suppress = False
        # While a barge-in candidate is open, the app owns the commit decision:
        # a cough or "mm-hmm" must be resumable, not a new assistant turn.
        self._hold_commits = False

    # ------------------------------------------------------------------- models
    def load(self) -> None:
        with self._asr_lock:
            if self._loaded:
                return
            # A failed warmup used to leave _loaded=True while _load_error was
            # set, so the next connection skipped loading and used a broken
            # engine. Clear the error and retry cleanly instead.
            self._load_error = None
            try:
                from mlx_audio.realtime_vad import (
                    ServerVadConfig,
                    StreamingVad,
                    TurnEventKind,
                )
                from mlx_audio.stt import load as load_stt
                from mlx_audio.vad import load as load_vad

                self._TurnEventKind = TurnEventKind
                vad_model = load_vad(VAD_MODEL)
                self.vad = StreamingVad(
                    vad_model,
                    ServerVadConfig(
                        threshold=VAD_THRESHOLD,
                        prefix_padding_ms=PREROLL_MS,
                        silence_duration_ms=PAUSE_MS,
                    ),
                )
                self.asr = load_stt(ASR_MODEL)
                if SEMANTIC_TURN:
                    try:
                        self.turn_model = load_vad(TURN_MODEL)
                    except Exception:
                        # Endpointing still works, just with the fixed pause.
                        self.turn_model = None
                # The TTS model is created, owned and used only on the TTS
                # thread, never on this one.
                self.tts_executor.submit(self._load_tts).result()
                self._loaded = True
                if os.environ.get("VOICE_WARMUP", "1") == "1":
                    # Initialise misaki/spaCy and the first Kokoro pipeline now,
                    # while the dot already shows "loading voice", instead of
                    # stalling the first spoken reply.
                    self.tts_executor.submit(self.synthesize, "Hola, estoy lista.").result()
            except Exception as exc:  # pragma: no cover - environment dependent
                self._loaded = False
                self._load_error = f"{type(exc).__name__}: {exc}"
                raise

    def _load_tts(self) -> None:
        from mlx_audio.tts.utils import load_model as load_tts

        self.tts = load_tts(TTS_MODEL)
        self.sample_rate = int(getattr(self.tts, "sample_rate", 24000))

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def active(self) -> bool:
        return self._turn_open

    # -------------------------------------------------------------------- turns
    def feed_pcm(self, pcm: bytes) -> List[TurnSignal]:
        """Feed int16 mono 16 kHz PCM and return the resulting turn signals.

        Safe to call at any time: the monitor VAD is always running, including
        while the assistant is speaking.
        """
        with self._asr_lock:
            if len(pcm) % 2:
                pcm = pcm[:-1]
            if not pcm:
                return []
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            signals: List[TurnSignal] = []
            self._push_preroll(samples)

            events = self.vad.process(samples)
            started = any(e.kind == self._TurnEventKind.SPEECH_STARTED for e in events)
            stopped = any(e.kind == self._TurnEventKind.SPEECH_STOPPED for e in events)

            if self._suppress:
                if stopped:
                    # The discarded utterance is over; listen normally again.
                    self._suppress = False
                    self._preroll.clear()
                    self._preroll_samples = 0
                return signals

            # The pre-roll already contains the current chunk, so a session
            # created here must not also be fed ``samples``.
            fed = False
            if started and not self._turn_open:
                self._begin_locked()
                self._awaiting_end = None
                fed = True
                signals.append(TurnSignal("speech_started", self._partial))

            if self._turn_open and not fed:
                self._push_utterance(samples)
                if self._session is not None:
                    self._session.feed(samples)

            if self._session is not None:
                deltas = self._session.step(max_decode_tokens=4)
                if deltas:
                    self._partial += "".join(deltas)
                    signals.append(TurnSignal("partial", self._partial))

            if stopped and self._turn_open and self._hold_commits:
                # The app owns this decision and needs the words now. The
                # streaming session holds its last chunk until close, so a
                # short "stop" would otherwise look like a silent cough; a
                # one-shot pass over the utterance audio is fast (~60-100 ms)
                # and gives the final text.
                self._awaiting_end = None
                text = self._finalize_text_locked()
                signals.append(
                    TurnSignal("speech_paused", text, int(self._speech_ms()))
                )
            elif stopped:
                signals.append(
                    TurnSignal("speech_paused", self._partial, int(self._speech_ms()))
                )
                if self._turn_open:
                    self._awaiting_end = None
                    if self._utterance_ms() >= MIN_TURN_MS and self._is_complete():
                        signals.append(self._commit_locked())
                    else:
                        self._awaiting_end = (
                            time.monotonic() + TURN_MAX_WAIT_MS / 1000.0
                        )

            now = time.monotonic()
            if (
                self._turn_open
                and self._awaiting_end is not None
                and now >= self._awaiting_end
            ):
                signals.append(self._commit_locked())
            elif self._turn_open and now - self._turn_started > MAX_TURN_S:
                signals.append(self._commit_locked())

            return signals

    def hold_commits(self, hold: bool) -> None:
        """Defer the pause/commit decision to the caller (barge-in window)."""
        with self._asr_lock:
            self._hold_commits = bool(hold)

    def end_turn(self) -> str:
        """Commit the open utterance now (manual stop)."""
        with self._asr_lock:
            if not self._turn_open:
                return ""
            return self._commit_locked().text

    def cancel_turn(self, suppress_tail: bool = True) -> None:
        """Discard the open utterance and re-arm onset detection.

        ``suppress_tail`` is for a backchannel: the rest of that burst must not
        start a turn. A disconnect or a fresh arm passes ``False`` so the next
        utterance is not swallowed by the suppression window.
        """
        with self._asr_lock:
            try:
                speaking = bool(self.vad.in_speech)
            except Exception:
                speaking = False
            self._discard_locked()
            # If the user is mid-utterance, the remaining audio would otherwise
            # re-trigger onset with the pre-roll attached and become a turn.
            self._suppress = speaking and suppress_tail
            self._hold_commits = False
            if speaking:
                self._preroll.clear()
                self._preroll_samples = 0
            try:
                self.vad.reset_turn()
            except Exception:
                pass

    # ----------------------------------------------------------------- internals
    def _push_preroll(self, samples: np.ndarray) -> None:
        self._preroll.append(samples)
        self._preroll_samples += samples.size
        limit = PREROLL_MS * SAMPLE_RATE // 1000
        while self._preroll and self._preroll_samples - self._preroll[0].size >= limit:
            self._preroll_samples -= self._preroll.popleft().size

    def _push_utterance(self, samples: np.ndarray) -> None:
        self._utterance.append(samples)
        self._utterance_samples += samples.size
        while (
            self._utterance
            and self._utterance_samples - self._utterance[0].size
            >= MAX_UTTERANCE_SAMPLES
        ):
            self._utterance_samples -= self._utterance.popleft().size

    def _utterance_ms(self) -> float:
        return 1000.0 * self._utterance_samples / SAMPLE_RATE

    def _speech_ms(self) -> float:
        """Utterance length without the pre-roll."""
        return (
            1000.0
            * max(0, self._utterance_samples - self._prefix_samples)
            / SAMPLE_RATE
        )

    def _begin_locked(self) -> None:
        pre = (
            np.concatenate(self._preroll) if self._preroll else np.zeros(0, np.float32)
        )
        self._session = self.asr.create_streaming_session(
            language=ASR_LANGUAGE, transcription_delay_ms=ASR_DELAY_MS
        )
        if pre.size:
            self._session.feed(pre)
        self._utterance.clear()
        self._utterance_samples = 0
        self._prefix_samples = int(pre.size)
        if pre.size:
            self._utterance.append(pre)
            self._utterance_samples = pre.size
        self._partial = ""
        self._turn_open = True
        self._turn_started = time.monotonic()

    def _finalize_text_locked(self) -> str:
        """Close and drain the session so the final word is decoded.

        The streaming frontend holds its last partial chunk until ``close``;
        without this, a one-word barge ("stop") looks like silence. Closing is
        safe here because the pause ends the utterance either way: the app
        either commits it or discards it as a backchannel.
        """
        if self._session is None:
            return self._partial
        try:
            self._session.close()
            deadline = time.monotonic() + 0.25
            while not self._session.done and time.monotonic() < deadline:
                deltas = self._session.step(max_decode_tokens=8)
                if deltas:
                    self._partial += "".join(deltas)
        except Exception:
            pass
        return self._partial

    def _is_complete(self) -> bool:
        """True when the utterance sounds like a finished thought."""
        if self.turn_model is None or not self._utterance:
            return True
        audio = np.concatenate(self._utterance)
        if audio.size > MAX_UTTERANCE_SAMPLES:
            audio = audio[-MAX_UTTERANCE_SAMPLES:]
        try:
            out = self.turn_model.predict_endpoint(
                audio, sample_rate=SAMPLE_RATE, threshold=TURN_THRESHOLD
            )
            return bool(out.prediction)
        except Exception:
            # Endpointing must never lose a turn to a model hiccup.
            return True

    def _commit_locked(self) -> TurnSignal:
        session = self._session
        self._session = None
        self._turn_open = False
        self._awaiting_end = None
        if session is not None:
            try:
                session.close()
                deadline = time.monotonic() + 3.0
                while not session.done and time.monotonic() < deadline:
                    deltas = session.step(max_decode_tokens=8)
                    if deltas:
                        self._partial += "".join(deltas)
            except Exception:
                pass
        text = " ".join(self._partial.split()).strip()
        self._partial = ""
        self._utterance.clear()
        self._utterance_samples = 0
        self._prefix_samples = 0
        return TurnSignal("committed", text)

    def _discard_locked(self) -> None:
        session = self._session
        self._session = None
        self._turn_open = False
        self._awaiting_end = None
        self._hold_commits = False
        self._partial = ""
        self._utterance.clear()
        self._utterance_samples = 0
        self._prefix_samples = 0
        if session is not None:
            try:
                session.cancel()
            except Exception:
                pass

    # ---------------------------------------------------------------------- tts
    def synthesize(self, text: str) -> bytes:
        """Return int16 mono PCM at ``sample_rate`` for one sentence.

        Must run on ``tts_executor``; the Kokoro model is only ever touched
        from that thread.
        """
        if not self._loaded:
            raise RuntimeError("voice engine is not loaded")
        text = clean_for_speech(text)
        if not text:
            return b""
        with self._tts_lock:
            chunks = []
            for result in self.tts.generate(
                text, voice=TTS_VOICE, lang_code=TTS_LANG
            ):
                audio = np.array(result.audio, dtype=np.float32).reshape(-1)
                if audio.size:
                    chunks.append(audio)
            if not chunks:
                return b""
            audio = np.concatenate(chunks)
            pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
            return pcm.tobytes()


def clean_for_speech(text: str) -> str:
    """Strip markup the model may emit despite the voice system prompt."""
    cleaned = "".join(" " if ch in "*_`#" else ch for ch in text)
    return " ".join(cleaned.split())[:600]


_engine: Optional[VoiceEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> VoiceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = VoiceEngine()
        return _engine
