# AGENTS.md — dot

Instructions for agents working in this repository (`dot`).

## What this is

A single-purpose local web app: the UI is one blue–purple dot. `app.py` launches
`llama-server` with **NVIDIA-Nemotron-3.5-Bolt-E4B-4GB** in the background, polls
`/health`, and streams state to the browser over SSE. The dot pulses while the model
loads and stays steady when it is ready.

Read `PLAN.md` for the full design and roadmap.

## Layout

```
app.py               FastAPI app: static, SSE, /health, /api/chat, /ws/voice
llama_manager.py     llama-server attach-or-spawn + health watcher
audio_engine.py      Silero VAD + Smart Turn + Nemotron ASR + Kokoro TTS (two pinned threads)
static/              dot UI, mic capture, PCM playback
run.sh               venv bootstrap + launch
models/              GGUF weights (gitignored)
logs/                llama-server and build logs (gitignored)
```

## Run

```bash
./run.sh                 # http://127.0.0.1:8765
```

Useful env vars:

| Variable | Default | Meaning |
|---|---|---|
| `DOT_HOST` | `127.0.0.1` | web app bind (`0.0.0.0` exposes it to the LAN) |
| `DOT_PORT` | `8765` | web app port |
| `DOT_TLS` | off | `1` serves HTTPS from `.tls/` (self-signed, auto-generated) |
| `LLAMA_PORT` | `1234` | llama.cpp port |
| `LLAMA_BIN` | `~/Projects/llama.cpp/build/bin/llama-server` | server binary |
| `MODEL` | `models/Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf` | weights |
| `CTX` | `32768` | context size |
| `KEEP_LLAMA=1` | off | don't kill llama on exit |
| `LLAMA_NO_SPAWN=1` | off | attach only, never spawn |
| `DFLASH=1` | off | enable speculative decoding |
| `VAD_SILENCE_MS` | `300` | pause that makes a turn-end candidate |
| `PREROLL_MS` | `500` | audio kept from before onset |
| `MIN_TURN_MS` | `250` | ignore shorter bursts |
| `TURN_MAX_WAIT_MS` | `2000` | commit an unfinished thought after this |
| `SEMANTIC_TURN` | `1` | Smart Turn v3 endpointing |
| `TURN_THRESHOLD` | `0.6` | Smart Turn "complete" threshold |
| `BARGE_HOLD_MS` | `1200` | sustained speech that counts as a floor bid |
| `BACKCHANNEL_MAX_MS` | `900` | short bursts treated as listener signals |
| `VOICE_WARMUP` | `1` | warm Kokoro at load |
| `VOICE_DEBUG` | off | log turn, barge and truncation decisions |
| `DOT_THINKING` | `1` | model reasoning (`enable_thinking`); never spoken |
| `DOT_TOOLS` | `1` | automatic emotion and headless browser tools |
| `DOT_BROWSE` | `1` | enable the browser tools |
| `DOT_BROWSE_TIMEOUT` | `20` | seconds per browser action |
| `DOT_WEATHER_TIMEOUT` | `8` | seconds per weather lookup |
| `DOT_TOOL_ROUNDS` | `6` | tool rounds allowed per turn |
| `DOT_MAX_TOKENS` | `1024` | max completion tokens (reasoning + answer) |
| `DOT_THINKING` | `1` | model reasoning (`enable_thinking`); never spoken |

## Invariants

- **Ports:** app `8765`, llama.cpp `1234`. Do not change defaults without updating
  the README and `.env` example.
- **Health contract:** `llama-server` returns `503` while loading and `200` when ready.
  The watcher polls every 250 ms and never trusts process liveness alone.
- **State machine:** `starting → ready | error`. Voice states (`loading_voice`,
  `listening`, `thinking`, `speaking`) are additive; `error` always wins.
- **Click:** busy (`thinking`/`speaking`) → cancel the reply; armed and idle → mute
  (close the mic); otherwise → arm. Taking a turn never needs a click.
- **Process ownership:** spawned with `start_new_session=True` and killed via the
  process group. Attached servers are never killed.
- **Python:** use the project venv on **Python 3.12**. `misaki` (Kokoro) does not
  support 3.13+.
- **No secrets in git.** `models/`, `logs/`, `.venv/`, `.chat` are ignored. HF token comes from
  `../.env`.
- **Browser origin:** `/api/chat`, `/api/restart`, and `/ws/voice` reject cross-site
  `Origin` headers. Add non-local hosts with `DOT_ALLOWED_ORIGINS`; never disable the
  check to make a LAN URL work. LAN voice additionally needs `DOT_TLS=1`: browsers
  only expose `getUserMedia` on secure origins. `run.sh` generates a self-signed
  cert in `.tls/` (SANs: localhost, `*.local`, current LAN IPs) and serves HTTPS.
- **Tools:** model-driven and optional (`DOT_TOOLS=0` disables all). `emotion` only
  changes the dot; it must never alter the voice state machine. Network access is
  through `browser_tool.BrowserSession`: `browse`/`navigate`, `browser_click`,
  `browser_type`, `browser_snapshot`, `browser_close`. Use Playwright's AI-mode
  accessibility tree and `get_by_ref()`, not CSS inventories. Public HTTP(S),
  validated/pinned DNS, same-host GET/HEAD only; no local files, LAN, passwords,
  uploads or downloads. Browser state is per conversation and persists across turns
  while armed, so follow-up questions about a page read do not need a new URL.
  `browser_close` is for an explicit user request, not for the model to tidy up
  after answering; the server closes the session on cancellation/disconnect/HTTP
  completion. Never ship model-provided JavaScript, `run_code` or arbitrary eval.
  `weather(place)` is the only other network path: fixed Open-Meteo HTTPS hosts
  (geocoding + forecast), no model-supplied URL, no API key. Keep it that way;
  do not turn it into a general fetch. Search engines block headless clients, so
  the prompt sends weather questions to `weather` instead of `browse`.
- **Tool syntax:** this model writes bare `emotion{...}` / `navigate{...}` instead
  of llama.cpp's `<|tool_call>` wrapper. `tools.InlineToolParser` turns those into
  real calls and must keep them out of the spoken reply. Positional, named and bare
  literal single-value arguments are supported. A blocking tool (any browser action)
  discards the rest of that message, because the model tends to answer from memory
  right after the call. Recognized but malformed calls become a parse error the model
  recovers from; they never reach speech or the user.

## llama.cpp

- Source: `~/Projects/llama.cpp` (stock ggml-org), built with Metal.
- Rebuild:
  ```bash
  cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 -DGGML_NATIVE=ON -DGGML_METAL=ON \
    -DGGML_ACCELERATE=ON -DGGML_LTO=ON -DGGML_CCACHE=ON \
    -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_OPENSSL=OFF
  cmake --build build --target llama-server llama-bench -j10
  ```
- Keep `-DLLAMA_OPENSSL=OFF` on this Mac: Homebrew OpenSSL exposes no
  `OpenSSL::SSL` CMake target and generation fails otherwise. HTTPS is not needed for
  localhost.
- Rollback binary: `build/bin/llama-server.bak-11513`.
- llama.cpp's own server UI is still available at `http://127.0.0.1:1234` while
  debugging; the dot app is separate.

## Model

`nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB` — Gemma-4-E4B, 8B total / 4B effective,
Q4_K_M, 6.18 GB. Current llama.cpp supports the `gemma4` arch. Always pass `--jinja`.
DFlash was benchmarked on this M5 and never beat the 15% bar reliably
(−6.9% … +14.9%), so it stays opt-in via `DFLASH=1`; do not enable it by default.

For voice replies the request must use:

```json
"chat_template_kwargs": {
  "enable_thinking": true,
  "conversational_mode": true
}
```

`DOT_THINKING=0` turns reasoning off. llama.cpp streams reasoning in
`reasoning_content`, separate from `content`; it must never reach TTS, history or
the spoken reply. It only drives the transient `pensando` hint and an
`assistant_reasoning` entry in `.chat`.

The browser runs **headed Chromium with its window minimized** via CDP: some sites
(DataDome-backed, e.g. `elpais.com`) return 403 to every headless Chromium variant.
Without a GUI session it falls back to headless. The window is minimized at
creation, so it never appears on the desktop. Full details in `BROWSING.md`.

## Voice stack

Balanced MLX profile, Python 3.12:

- VAD `mlx-community/silero-vad`
- STT `mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit` (~0.76 GB)
- TTS `mlx-community/Kokoro-82M-8bit` (~0.35 GB), needs `misaki[en]`

The browser owns mic capture and playback (AudioWorklet, 16 kHz mono, 24 kHz out).
Spanish is the default: ASR locale `es-ES`, Kokoro `TTS_LANG=e` and
`TTS_VOICE=ef_dora`; assistant replies and accessible UI labels are Spanish.
Keep emotion colors within blue–violet (hues 207–278); do not reintroduce red.
Do not add PortAudio/`sounddevice` capture paths. Incoming PCM is 20 ms int16 frames;
the client resamples from the AudioContext rate, so do not assume 48 kHz.

**The mic stays open while armed**, including while the agent speaks. That is what
makes barge-in and hands-free turn-taking possible. Muting closes it; a click while
idle does not stop a turn. `VoiceEngine.load()` warms Kokoro once; keep that
warmup, it removes a ~10 s first-reply stall.

**Two audio threads, one per model:** VAD/ASR calls go through `engine.executor`,
TTS through `engine.tts_executor` (both `max_workers=1`). MLX GPU streams are
thread-local; calling a model from arbitrary threads (e.g. `asyncio.to_thread`)
intermittently raises `There is no Stream(gpu, N)`. Keep each model on its pinned
thread. Do not merge the executors: with one worker, Kokoro synthesis starves the
streaming ASR and an interruption is only transcribed after the user stops talking.

**Turn lifetime:** a turn stays current until the client reports `playback_done`,
sends an `interrupt`, or a "total audio + 2 s" fallback expires. Do not clear it at
`tts_end`; a barge during the playback tail must still truncate what was heard.
History entries are shared dicts, so late truncation mutates the recorded message.

## Voice protocol

Client → server JSON: `arm`, `disarm`, `cancel`, `stop`,
`interrupt {turn_id, played_ms}`, `playback_done {turn_id}`; binary frames are
16 kHz int16 PCM.

Server → client JSON: `state`, `sample_rate`, `speech_started`, `partial`,
`final`, `assistant_delta`, `tts_begin`, `tts_end`, `pause_playback`,
`resume`, `barge_in`, `interrupted`, `cancel_playback`, `browsing`, `reasoning`, `emotion`, `error`;
binary frames are 24 kHz int16 PCM.

Barge-in is decided on the server. On speech onset it sends `pause_playback` and
holds TTS synthesis, then confirms when the transcript has two strong words, one
strong word after 350 ms, or 1200 ms of sustained speech. A backchannel (short and
without a force-yield word such as "stop" or "no") is discarded and playback
resumes. The engine defers its commit decision until that call, and the pause path
closes the ASR session so the last word is not stuck in the streaming frontend.

`run.sh` passes `--timeout-graceful-shutdown 5`; without it an open SSE tab blocks
uvicorn forever and `llama-server` is orphaned on Ctrl-C.

## Testing

- Conversation debugging: `.chat` is a private, Git-ignored append-only JSONL
  transcript for voice and HTTP chats. `chat_log.py` writes it on its own thread.
  Never commit its contents or confuse generated/synthesized text with confirmed
  playback; interrupted playback text is only a sentence-level estimate.
- Transcript tests: `.venv/bin/python -m unittest discover -s tests -p 'test_chat_log.py' -v`.

```bash
curl -s localhost:8765/health | jq
curl -sN localhost:8765/events          # SSE: expect starting, then ready
curl -s localhost:1234/health
```

Playwright: load `http://127.0.0.1:8765`, assert `#dot[data-state]` and, for
`starting`/`loading_voice`, an active animation on `.dot-core` (`kindle`), then
wait for `ready` (no animation). Arming is remembered in
`localStorage["dot.armed"]`; a fake microphone (Chromium's
`--use-file-for-fake-audio-capture`) can drive a real turn end to end.

## Style

- Python: 4 spaces, type hints where useful. FastAPI/httpx for the web layer;
  numpy/mlx only inside the voice engine. No frameworks in `static/`; plain HTML/CSS/JS.
- Keep the dot UI visually minimal — no text, no chrome. The one exception is the
  transient `browsing` pill: hostname only, no verbs or status copy, while a
  browser tool navigates. Use `aria-live` for status.
- `llama-server` logs go to `logs/llama.log`. Uvicorn access/error logs go to stdout
  when launched by `run.sh`.
