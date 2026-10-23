# Seamless turn-taking for dot

Research notes and an implementation design for the open Phase 2 item:
*"Automatic barge-in from the mic while TTS is playing (needs echo handling)."*

Goal: no clicks. The dot is armed once, then the conversation starts when you
talk, ends when you stop, and you can talk over the reply.

---

## 1. The short answer

Seamless dialogue is not one model. It is four layers that fail independently:

| Layer | Question | Failure if missing |
|---|---|---|
| **Full-duplex capture** | Can I hear the user while my own voice is playing? | Agent hears itself, interrupts itself, or is deaf while speaking |
| **End-of-turn detection** | Has the user finished their thought? | Cuts people off mid-sentence, or answers 1 s late |
| **Interruption policy** | Is this speech a floor bid or a backchannel? | Stops for "mm-hmm", or talks through "no, Friday" |
| **State reconciliation** | What did the user actually hear? | Model thinks it said things it never said |

Everything below is detail on those four.

The industry has converged on a **layered detector**, not a single threshold:
denoise/AEC → frame VAD → semantic end-of-turn model → app-level interruption
policy. Each layer exists because the layer below it has a documented failure
mode. LiveKit, Pipecat, OpenAI Realtime and Deepgram Flux all ship some version
of this stack.

---

## 2. What "natural" actually costs

Humans leave ~200 ms between turns, stable across languages (Stivers et al.,
PNAS 2009). Perceptual bands used across the industry:

| Gap after user stops | How it feels |
|---|---|
| 200–300 ms | natural |
| ~500 ms | noticeable |
| 800 ms–1 s | users start talking over the agent |
| > 1.5 s | broken / "did it hear me?" |

Two consequences:

1. **Latency manufactures false barge-ins.** A slow agent gets interrupted out
   of frustration, which then looks like a turn-detection bug.
2. **Fixed silence timeouts can't win.** Every millisecond you add to protect
   slow speakers is added to *every* turn. This is why semantic models exist:
   wait longer only when the utterance looks unfinished.

dot currently pays 700 ms of VAD silence on every turn. That is most of the
1.55 s end-to-last-word latency in `PLAN.md`.

---

## 3. Turn detection: the four strategies

### 3.1 Silence / energy threshold

Wait for N ms below an energy threshold. Cheap, and the cause of most bad voice
agents. Cannot distinguish a thinking pause from a finished thought, or a TV
from a person.

### 3.2 Frame VAD (Silero)

Per-frame speech probability. Robust to noise, but still acoustic only.
Parameters that matter (OpenAI-compatible `server_vad` schema, which is what
`mlx_audio.realtime_vad` implements):

| Param | Meaning | Practical range |
|---|---|---|
| `threshold` | speech probability cutoff | 0.4–0.6 |
| `prefix_padding_ms` | pre-roll kept before onset | 250–500 |
| `silence_duration_ms` | silence needed to close a turn | 200 (with semantic confirm) – 700 (VAD-only) |

Note `prefix_padding_ms` in `mlx_audio` only shifts the *reported* offset; it
does **not** gate transcription. If you want the first phoneme transcribed you
must keep your own pre-roll ring buffer and replay it into the ASR session.

### 3.3 STT endpointing

Use the recognizer's end-of-utterance signal or the trailing transcript. Fast,
but provider-specific and usually punctuation-shaped.

### 3.4 Semantic / model-based end-of-turn

A small classifier scores "is this thought complete?" from audio or text.

- **Pipecat Smart Turn v3** — Whisper-tiny encoder + attention pooling on the
  last ≤8 s of raw audio. ~65 ms on a cloud 1× instance; **4–11 ms measured on
  this M5 via MLX** (see §10). Default local analyzer in Pipecat. Designed to
  run *on the pause*, with VAD `stop_secs=0.2`.
- **LiveKit Turn Detector** — text transformer over the last turns; v1 fuses
  audio, ~50 ms, 14 languages. Extends the silence window only when more speech
  is predicted.
- **Krisp** — ~9 M param audio-only models; a companion *Interrupt Prediction*
  model classifies overlapping speech as real interruption vs. backchannel
  (<6% false positives claimed).
- **OpenAI `semantic_vad`** — `eagerness` maps to max waits: low 8 s,
  medium/auto 4 s, high 2 s. Field reports of premature cuts even at `low`; no
  sensitivity knobs.

**The production pattern** is VAD for *onset* and semantics for *offset*:

```
speech? ──Silero──► START (open ASR, replay pre-roll)
                        │
silence ≥ 200 ms ───────┤
                        ▼
              Smart Turn on the utterance
                 │              │
            complete        incomplete
                 │              │
             commit turn    keep listening (max 2.5–3 s)
```

That gets you endpoint latency of ~200–250 ms without cutting thinkers off.

### 3.5 Realtime speech-to-speech

Moshi-style full-duplex models (and GPT-realtime / Gemini Live) learn turn
taking internally and can even backchannel. Great feel, less control, harder to
ground in local tools, and you lose the transcript as ground truth. For dot —
llama.cpp + local ASR/TTS — the cascaded pipeline plus a semantic end-of-turn
model is the right architecture. Revisit only if you move off `llama-server`.

---

## 4. Barge-in: the four steps everyone converges on

1. **Detect overlap** — speech while the agent is producing or playing audio.
2. **Cancel generation** — abort the LLM stream and TTS synthesis.
3. **Flush playback** — stop scheduled audio *now*, not at the end of the buffer.
4. **Reconcile state** — record how much was actually played, and drop the rest
   from conversation history.

Step 4 is the one teams skip. Speech models generate faster than real time, so
by the time the user interrupts the server has usually recorded the *entire*
reply in history. Skip truncation and the model later says "as I mentioned"
about words nobody heard, and skips re-explaining.

dot has a partial advantage: `handle_turn()` appends to `history` only after the
stream loop finishes, so a cancelled turn is not recorded at all. That is safe
but lossy — the user did hear the first sentence, and the model loses it. The
better behaviour is to record the spoken prefix (sentence-boundary truncation)
and drop the rest.

### Stopping fast

Budget: 10–30 ms frames, and the whole detect → cancel → mute chain should land
within a few frames. Anything past ~500 ms of talking over the user reads as
broken. On localhost the WebSocket round trip is ~1 ms, so the cost is almost
entirely your confirmation window. Hence: **start ASR at speech onset**, so by
the time you confirm (250–350 ms) you already have words to show.

### False interruptions and resume

Two policies, both shipped by LiveKit:

- `false_interruption_timeout` (default 2.0 s) — silence after an interruption
  before it is classified false.
- `resume_false_interruption: true` — continue the reply from where it stopped.

Resume requires keeping the unplayed PCM for the turn. Do it client-side: the
browser already holds every `AudioBuffer` it scheduled and knows the exact
playback clock, so it can reschedule the remainder without a server round trip.

---

## 5. Hearing the user over your own voice

This is the blocker for dot, and the part most tutorials hand-wave.

### 5.1 Browser AEC is not a guarantee

`getUserMedia({audio:{echoCancellation:true}})` enables WebRTC AEC3, and it is
the right first move: platform AEC has direct access to mic and speaker and
handles time alignment.

But Chromium's AEC reference has historically been built from **WebRTC remote
media**, not from arbitrary WebAudio graphs (Chromium issue 40504498,
`webrtc/samples#1243`). Audio you decode from a WebSocket and schedule onto
`AudioBufferSourceNode`s may not be cancelled at all. Safari and Chrome also
differ. **Test on your machine; do not assume the constraint did it.**

Even when it works, AEC is weakest exactly during **double-talk** — the moment
you care about. AEC3 freezes filter adaptation when it detects near-end speech,
so the first ~200 ms of an interruption is the noisiest part of the signal.

### 5.2 Options, cheapest first

**(a) Echo-aware gating (no DSP).**
Keep the mic open. While TTS plays, compare mic RMS to the known playback RMS
per frame. Only treat speech as a barge-in candidate when near-end-to-echo ratio
is > 0 dB — in practice `mic_rms > 1.4–1.6 × ref_rms` — sustained 250–350 ms.
Cheap, works with speakers at moderate volume, degrades when the user is far
from the mic or the room is reverberant.

**(b) Text-level echo rejection.**
You know the exact sentence being spoken. If the partial transcript fuzzy-matches
what TTS is currently saying, it's echo — discard. Deepgram recommends the same
trick server-side. Nearly free, catches the cases gating misses.

**(c) RTCPeerConnection loopback.**
Route the TTS `MediaStreamAudioDestinationNode` through an `RTCPeerConnection`
and play the *remote* track in an `<audio>` element. Chrome then sees it as
remote media and uses it as the AEC reference. Ugly, but it is the documented
community workaround.

**(d) Reference AEC you control.**
Use **one `AudioContext`** for capture *and* playback. Then the TTS samples you
scheduled and the mic frames share a clock, and you can run a small NLMS
adaptive filter in the AudioWorklet (16 kHz, 20–40 ms tail ≈ 320–640 taps,
~10 M MAC/s — trivial) with a double-talk freeze. This is the "one clock, one
reference" argument on-device stacks make; the browser version is the same idea
with a known output latency.

Start with (a)+(b). Add (d) only if speakers-at-volume testing still
self-interrupts. (c) is a fallback if you want browser AEC to do the work.

### 5.3 Duck instead of hard-cut on the first frame

Perceived responsiveness is not the same as confirmed interruption:

- On a **local** candidate (RMS gate in the worklet), drop playback gain to
  ~0.2 within one audio frame and keep the mic running.
- Server confirms within ~250 ms using VAD + first partial.
- Confirmed → cancel the turn, gain to 0, accept the new utterance.
- Not confirmed (silence/backchannel) → restore gain, keep speaking.

This makes the agent *feel* like it stops instantly while the policy still gets
a quarter second to think.

---

## 6. Backchannels: don't stop for "mm-hmm"

An energy detector scored against 30 h of human conversation fired on **45% of
backchannels** (52% in casual talk) — every one of those would stop an agent
mid-sentence (Deepgram, citing arXiv 2608.25218). Listeners backchannel roughly
**7× more often** than they take the floor.

Useful token inventory (English):

| Class | Tokens | Action |
|---|---|---|
| Affirmative continuer | `mhmm`, `mm-hmm`, `uh-huh`, `yeah`, `yep`, `right`, `okay`, `sure`, `got it` | Hold |
| Negative / correction | `mm-mm`, `uh-uh`, `nuh-uh` | Yield immediately |
| Anything else | words, sentences | Yield |

Decision sequence (Deepgram's, adapted):

1. Short vocalization arrives during playback.
2. Is the transcript a standalone affirmative? → hold (optionally resume).
3. Negative token, standalone or not? → yield.
4. Affirmative token **with speech continuing**? → yield (it opened a floor bid).
5. No match → yield.

Caveat: `right`/`okay` are ordinary words; a token lookup can't tell "right."
from "right, but the address is wrong". Word count + whether speech continues
after the token is the practical proxy for the prosody you don't have.

Implementation in dot: on a barge-in candidate, don't cancel immediately —
duck, then inspect the first partial. If it is a lone affirmative and the user
goes quiet, resume the reply from the playback cursor (which also covers
coughs and door slams, so you get false-positive recovery for free).

---

## 7. Where the research frontier is

- **Semantic end-of-turn is now commodity.** Smart Turn v3 (BSD-2), LiveKit's
  turn detector, Krisp's models. All run in tens of milliseconds; the 700 ms
  silence timeout is obsolete.
- **Interrupt *intent* is the new layer.** Krisp's Interrupt Prediction and
  LiveKit's `interruption.mode="adaptive"` classify overlapping speech rather
  than merely detecting it. This is where most remaining UX gains are.
- **Recovery is being measured separately from detection.** IHBench (2026)
  scores post-interruption competence: resume the workflow at the right step,
  don't re-deliver what was already heard. Detection benchmarks alone hid this.
- **Full-duplex audio models** (Moshi lineage, Gemini Live, GPT-realtime) remove
  the pipeline but cede control of turn policy and grounding. Not a fit for a
  local llama.cpp stack yet.
- **Non-English turn detection lags.** Semantic models calibrated on English
  over-trigger on CJK clause pauses. Relevant if dot's ASR language changes.

---

## 8. Concrete design for dot

### 8.1 State machine

Today the mic is opened per turn and torn down on `thinking`/`speaking`
(`stopCapture()` stops the track *and* closes the `AudioContext`). That makes
barge-in impossible and adds ~100–300 ms of mic reopen per turn.

New model:

```
         ┌──────────── armed (mic open, VAD running, nothing sent to LLM)
         │                 │
   speech_started     silence 20 s
         │                 │
         ▼                 ▼
     listening ◄─────── idle (UI dim, mic still open)
         │
   end-of-turn confirmed (VAD pause + Smart Turn)
         ▼
     thinking ──speech_started──► barge-in: cancel LLM, listening
         │
         ▼
     speaking ──speech_started──► barge-in: duck, confirm, resume or listening
```

The only click left is the initial **arm** (browser requires a user gesture for
`getUserMedia` + `AudioContext.resume` on first visit). Persist arm state and
auto-resume on later loads; keep the dot click as *mute* afterwards.

### 8.2 Client (`static/app.js`, `static/pcm-worklet.js`)

1. **One `AudioContext`** for capture and playback. Removes the two-clock
   problem and makes reference AEC / RMS gating meaningful.
2. **Keep the mic graph alive**; never `close()` the context on state changes.
   Gate *sending*, not capture.
3. **Playback bus with a gain node** (`ttsGain`) → duck to 0.2 on candidate,
   0 on confirm, 1 on resume.
4. **Reference level**: `AnalyserNode` on the playback bus; send per-frame
   `ref_rms` alongside PCM, or gate locally and send a
   `{"type":"barge_in_candidate"}` hint.
5. **Playback truth**: track `turnStartAt`, `playHead`, and the list of
   `{buffer, startAt}` for the current turn. On interrupt compute
   `played_ms = (now - turnStartAt) * 1000`, stop sources, keep buffers.
   On resume, reschedule the remainder from `played_ms`.
6. **Drop late audio**: `cancel_playback` sets `dropAudio = true`; the next
   `tts_begin` clears it. Prevents in-flight binary frames from resuming
   playback after a cancel.

### 8.3 Server (`audio_engine.py`)

1. **Two VAD roles**: one long-lived monitor VAD that is never reset (drives
   `speech_started`/`speech_stopped` for the session), plus turn state. Do not
   call `reset_turn()` at the end of every turn — that is what makes the
   detector deaf between turns.
2. **Pre-roll ring buffer** of ~400 ms. On `SPEECH_STARTED`, create the ASR
   session and feed the ring first, then live frames. Without this the
   interrupting word's onset is lost.
3. **Semantic endpointing**: set `silence_duration_ms=200`. On
   `SPEECH_STOPPED`, run Smart Turn over the utterance (≤8 s), threshold ~0.5.
   `prediction == 0` → stay in `listening`, extend by 300 ms, re-check, give up
   at `MAX_TURN_S` / a 3 s extension cap.
4. **Barge-in path**: `SPEECH_STARTED` while `thinking` or `speaking` starts the
   ASR session immediately but does **not** cancel yet. Confirm when either
   (a) ≥2 partial words, or (b) 300 ms of speech; then set a cancel event the
   turn task checks between sentences.
5. **Truncate**: `end_turn()` should return the spoken prefix on interrupt, not
   the full generation. Simplest correct version: keep the sentence list and
   the played_ms boundary; append `assistant: " ".join(spoken_sentences)` to
   history.

Model wiring (verified in this venv):

```python
from mlx_audio.vad import load as load_vad

turn_model = load_vad("mlx-community/smart-turn-v3")   # 8M params, ~30 MB
out = turn_model.predict_endpoint(audio_16k, sample_rate=16000, threshold=0.5)
# out.prediction -> 1 complete / 0 incomplete, out.probability -> float
```

It loads through the same `mlx_audio.vad.load` path already used for Silero, so
no new dependency. Measured: 4–11 ms per call on this M5 (see §10).

### 8.4 Protocol

| Direction | Message | Purpose |
|---|---|---|
| C→S | `{"type":"arm"}` | mic on, start streaming |
| C→S | binary 20 ms PCM | continuous |
| C→S | `{"type":"interrupt","turn_id":"t7","played_ms":1840}` | local candidate confirmed |
| C→S | `{"type":"resume","turn_id":"t7"}` | false positive |
| S→C | `{"type":"speech_started","source":"user"}` | UI/duck |
| S→C | `{"type":"barge_in","turn_id":"t7"}` | confirmed; client cuts |
| S→C | `{"type":"tts_begin","turn_id":"t7"}` then binary | audio for a turn |
| S→C | `{"type":"tts_end","turn_id":"t7"}` | end of audio |

Binary frames stay untagged; `tts_begin`/`tts_end` bracket them and
`dropAudio` handles the cancel race.

### 8.5 Tuning starting points

| Knob | Value | Why |
|---|---|---|
| Silero threshold | 0.5 | default; raise to 0.55–0.6 if false triggers |
| Silence before semantic check | 200 ms | Pipecat's trained-on value |
| Semantic threshold | 0.5 | model default |
| Max semantic extension | 2.5–3 s | bounds the "thinking pause" wait |
| Barge-in min speech | 300 ms or 2 words | filters coughs/clicks |
| Barge-in grace after TTS start | 200 ms | let AEC settle |
| NER gate | mic > 1.4× ref RMS | ~+3 dB near-end over echo |
| Duck level | 0.2 | audible but clearly yielding |
| False-interruption timeout | 1.5–2 s | LiveKit default is 2 s |
| Mic idle timeout | 20–30 s → `idle` | privacy; keeps stream open |

### 8.6 Latency budget after the change

| Stage | Today | After |
|---|---|---|
| VAD silence | 700 ms | 200 ms (+ ~5 ms Smart Turn) |
| ASR finalize | ~50–150 ms | same, but already warm from onset |
| LLM TTFT | ~150–250 ms | same |
| TTS first sentence | ~100–200 ms | same |
| **Last word → first audio** | **~1.5 s** | **~0.6–0.8 s** |

That reaches the natural band without touching the LLM.

---

## 9. Test plan

Deterministic (no mic):

- Feed WAVs through `VoiceEngine` and assert turn events: complete sentence,
  trailing "ummm", 800 ms mid-sentence pause, 3 s pause, 30 s monologue.
- Assert Smart Turn classifies truncated prefixes as incomplete (see §10).
- Assert barge-in cancels within N frames and that history contains only the
  spoken prefix.

Browser (Playwright):

- `--use-fake-device-for-media-stream` + `--use-file-for-fake-audio-capture=…`
  to drive the mic. Note: Playwright ≥ 1.45 defaults to `chromium-headless-shell`,
  which lacks full audio behaviour — force the full Chromium build.
- Simulate echo by playing the TTS WAV out of the same fake device if your
  loopback allows it; otherwise assert the gating logic with injected RMS.

Manual matrix (from Hamming's runbook):

| Case | Expected |
|---|---|
| "no, Friday" 1 s into a reply | stop, accept correction, keep task state |
| "yeah"/"mm-hmm" during a reply | hold (or resume), no lost place |
| cough / door / keyboard | no stop, logged as false positive |
| long account number with pauses | no premature commit |
| "stop" | immediate stop, no new turn |
| interrupting during `thinking` | cancel generation, listen |

Metrics worth logging per session: false-interruption rate (stopped, then no
full user turn within 2 s), missed-correction count, resume success, repeated
user speech rate, and endpoint latency p50/p95.

---

## 10. Measured on this machine

`mlx-community/smart-turn-v3` through `mlx_audio.vad.load`, M5, Python 3.12:

| Input | p(complete) | Prediction |
|---|---|---|
| "I would like to book" (30%) | 0.190 | incomplete |
| "…a table for four" (50%) | 0.010 | incomplete |
| "…four people tomorrow" (70%) | 0.132 | incomplete |
| "…tomorrow evening" (85%) | 0.888 | complete |
| full sentence | 0.990 | complete |

Inference: **4–11 ms** per call (1–8 s of audio, fp32). Load: ~5 s cold,
~30 MB resident. Caveat: this was Kokoro-synthesized speech, not real mic
audio, and one 60%-truncated short question scored 0.98 (a false "complete").
Re-validate the threshold on recordings from your actual mic before trusting
0.5.

---

## 11. What was built

Implemented in `audio_engine.py`, `app.py` and `static/app.js` (October 2026):

- **Persistent mic.** The AudioWorklet stream stays open while armed; the mic is
  closed only on mute or page hide. `localStorage["dot.armed"]` survives reloads.
- **Endpointing.** Silero onset, then a 300 ms pause, then Smart Turn v3 on the
  utterance audio (`TURN_THRESHOLD=0.6`). A 500 ms pre-roll is prepended so the
  first phoneme is not clipped.
- **Barge confirmation** (server-side, no extra model): pause playback on onset and
  hold TTS synthesis; confirm on ≥2 strong words, 1 strong word after 350 ms, or
  1.2 s of sustained speech (`BARGE_HOLD_MS`). Backchannels are short (≤900 ms,
  `BACKCHANNEL_MAX_MS`) and contain no force-yield word (`stop`, `wait`, `no`, …);
  they are discarded and playback resumes from the exact paused offset. A cough
  with no text at all is also a resume.
- **Why the VAD hold exists.** Streaming Nemotron partials trail the audio by
  ~0.7 s on this M5 even with two pinned threads, because Kokoro synthesis keeps
  the ASR from running in real time. Text-only confirmation would fire only after
  the user stops talking. The hold caps the wait at 1.2 s without needing echo
  heuristics.
- **Why TTS is held.** On speech onset the assistant stops synthesizing new
  sentences (`turn["paused"]`), which lets the ASR catch up; on a backchannel it
  simply continues. The already-scheduled playback is paused client-side.
- **Turn lifetime.** A turn expires on `playback_done`, `interrupt`, or
  `total audio + 2 s`. Late barges during the playback tail still truncate the
  assistant history entry to sentences actually heard (`played_ms`).
- **Two MLX threads.** VAD/ASR on `engine.executor`, TTS on `engine.tts_executor`,
  each `max_workers=1`, each model pinned to its thread. This is what keeps
  barge-in responsive; a single executor starved the ASR for over a second.
- **Commit deferral.** While a barge candidate is open, the engine does not commit
  at a pause. It closes and drains the ASR session (the streaming frontend holds
  its last chunk until close) so a one-word “stop” is not mistaken for a
  backchannel, then the app chooses commit or discard.

Tuning notes: 200 ms proved too eager (Kokoro sentence gaps split mid-turn); 300 ms
worked on every test sentence. Smart Turn on synthetic Kokoro audio is
out-of-domain — validate the threshold on real recordings. Barge confirmation was
measured at 0.7–1.6 s after onset (playback pauses at ~0.4 s), and a resumable
backchannel completes in ~1.3 s.

---

## 12. Sources

- LiveKit, *Turn detection for voice agents: VAD, endpointing, and model-based
  detection* — https://livekit.com/blog/turn-detection-voice-agents-vad-endpointing-model-based-detection
- LiveKit Agents docs, turn handling / adaptive interruption options —
  https://docs.livekit.io/agents/logic/turns
- Pipecat docs, Smart Turn v3 + `TurnAnalyzerUserTurnStopStrategy` +
  interruption strategies — https://docs.pipecat.ai
- Pipecat Smart Turn — https://github.com/pipecat-ai/smart-turn
- Deepgram, *Audio Preprocessing & Barge-In* and *Backchannels vs
  interruptions* — https://developers.deepgram.com/guides/deep-dives/audio-preprocessing-barge-in
- Hamming, *Voice Agent Interruption Handling Runbook* (2026) —
  https://hamming.ai/resources/voice-agent-interruption-handling-runbook
- Zylos, *Turn-Taking and Barge-In Mechanics in Realtime Voice Agents* (2026) —
  https://zylos.ai/research/2026-07-17-turn-taking-barge-in-realtime-voice-agents/
- EdgeAI, *Barge-in and interruption handling for on-device voice agents* —
  https://www.runedge.ai/blog/barge-in-interruption-handling-on-device-voice
- Vocal, *AEC barge-in* (near-end-to-echo ratio) —
  https://vocal.com/echo-cancellation/aec-barge-in/
- Chromium issue 40504498, *AEC when using Web Audio API* —
  https://issues.chromium.org/issues/40504498
- Stivers et al., *Universals and cultural variation in turn-taking in
  conversation*, PNAS 2009
- `mlx-community/smart-turn-v3` — https://huggingface.co/mlx-community/smart-turn-v3
- IHBench (2026) — https://arxiv.org/abs/2606.19595
