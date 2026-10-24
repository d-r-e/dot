# Dot — local voice/LLM UI

A tiny web app whose entire UI is a blue–purple dot. The app launches `llama-server` in the
background with **NVIDIA-Nemotron-3.5-Bolt-E4B-4GB** (GGUF, Q4_K_M). The dot pulses while
the model loads and turns steady once `llama-server` is healthy.

Status: **in progress** — Phase 0/1/2 below.

## Hardware / environment

- Apple M5, 24 GB unified memory, macOS 27
- `~/Projects/llama.cpp` (stock ggml-org build, Metal)
- Python: 3.12 venv managed by `uv` (Kokoro's `misaki` requires `<3.13`)
- Ports: app `8765`, llama.cpp `1234`

## Model

| Item | Value |
|---|---|
| Repo | `nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB` |
| File | `Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf` |
| Size | 6.18 GB |
| Architecture | Gemma-4-E4B, 8B total / 4B effective |
| Context | 64K |
| Drafter (optional) | `Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf`, 0.74 GB |
| License | OpenMDW-1.1 |

## Phase 0 — toolchain and weights

- [x] `git pull --ff-only` llama.cpp master; keep `build/bin/llama-server.bak-11513`.
- [x] Rebuild `llama-server` + `llama-bench` with Metal (build 11841, `280141e6d`).
- [x] Download both GGUF files into `models/`.
- [x] Smoke-test model load and one chat completion (~29 tok/s decode).
- [x] Benchmark DFlash speculative decoding; **not enabled by default**.

### DFlash result

Measured on the M5 with `max_tokens=128`, temperature 0, same prompt, via
`llama-server` timings (llama-bench in this build has no draft flags):

| Config | Median decode | Delta |
|---|---|---|
| base | 31.0–31.3 tok/s | — |
| DFlash, n-max 4 | 35.97 tok/s / 28.89 tok/s | +14.9% / −6.9% |
| DFlash, n-max 7 | 34.2–34.3 tok/s | +9.5% |
| DFlash, n-max 10 | 33.8 tok/s | +7.9% |
| DFlash, n-max 16 | 33.6 tok/s | +7.4% |

The gain is within run-to-run noise, never clears 15% reliably, and the greedy
output length changed (339 → 377 chars). The drafter stays downloaded and is
opt-in with `DFLASH=1`, not part of the default path.

## Phase 1 — dot app

- [x] `app.py` (FastAPI/uvicorn): static files, SSE, health, chat proxy, voice WS.
- [x] `llama_manager.py`: attach-or-spawn, `/health` polling, log capture,
      process-group cleanup, error classification.
- [x] Dot UI: pulse while starting, steady when ready, muted violet on error.
- [x] `run.sh`: venv bootstrap, model check, start.
- [x] `README.md`, `.gitignore`.

### State machine

```
starting --(/health 200)--> ready
    |                         |
    +--(crash/timeout)--> error <--(restart click)--> starting
```

`/health` returns 503 while the model loads and 200 when it is ready. Poll every 250 ms;
fail after 180 s, on child exit, missing binary/model, or a foreign listener on 1234.

### Process lifecycle

- Spawn `llama-server` in its own session; log to `logs/llama.log`.
- If a healthy server already listens on 1234, attach instead of spawning.
- Ctrl-C / SIGTERM terminates the whole process group. `KEEP_LLAMA=1` opts out.
- `LLAMA_NO_SPAWN=1` attaches only (useful in development).

## Phase 2 — voice scaffold (Balanced MLX)

- [x] VAD/ASR and Kokoro TTS running on separate pinned single-worker executors.
- [x] Browser AudioWorklet mic (mono, resampled to 16 kHz) over `/ws/voice`.
- [x] Streaming LLM → sentence chunker → TTS → 24 kHz PCM playback.
- [x] Manual barge-in: click the dot while thinking/speaking to cancel.
- [x] Preload/warmup during `loading_voice`; ~0.6–0.8 s from end-of-turn to
      first audio.
- [x] Automatic barge-in from the mic while TTS is playing. The client pauses
      playback at speech onset; the server confirms from the transcript or 1.2 s
      of sustained speech, truncates the assistant message and starts the next
      turn. Backchannels are discarded and playback resumes.
- [x] Hands-free turn-taking: persistent mic while armed, 300 ms pause + Smart
      Turn v3 endpointing, 500 ms pre-roll.

All speech models run locally with `mlx-audio` 0.6.1 on Python 3.12.

| Role | Model | Size | Notes |
|---|---|---|---|
| VAD | `mlx-community/silero-vad` | ~2 MB | onset detection |
| Smart Turn | `mlx-community/smart-turn-v3` | ~0.1 GB | semantic end-of-turn, ~5 ms |
| STT | `mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit` | 0.76 GB | 40 locales, streaming, 160 ms look-ahead |
| LLM | Nemotron-3.5-Bolt Q4_K_M | 6.18 GB | `enable_thinking: true`, `conversational_mode: true`; reasoning never spoken |
| TTS | `mlx-community/Kokoro-82M-8bit` | 0.35 GB | 54 voices, 24 kHz |

Audio path:

```
browser mic (AudioWorklet, 16 kHz mono)
  -> /ws/voice -> Silero VAD -> Nemotron ASR (partials)
  -> llama.cpp streaming chat -> sentence chunker -> Kokoro TTS
  -> PCM frames -> browser AudioContext
```

- Dot states: `starting`, `ready`, `loading_voice`, `listening`, `thinking`,
  `speaking`, `error`.
- First click arms the mic (permission on localhost secure context); the mic stays
  open while armed, and the armed flag persists in `localStorage`.
- Barge-in: speaking over the reply pauses playback immediately; the server then
  confirms, cancels the LLM request and TTS, and truncates history to the heard
  prefix. A click during thinking/speaking also cancels.
- Voice engine runs on two pinned worker threads (VAD/ASR and TTS). MLX streams are
  thread-local; keep each model on its own thread. Move to processes if contention
  hurts latency.

## Phase 3 — automatic tools

- [x] `tools.py`: `emotion(name)`, weather and isolated browser tool definitions, executed
      by the model without a user click.
- [x] Streaming tool loop in `/ws/voice`: tools run before their grounded answer;
      a barge cancels the tool. Navigation shows a hostname-only hint.
- [x] Inline-call recovery: this model emits `emotion{name:<|"|>smile<|"|>}`
      instead of llama.cpp's `<|tool_call>` wrapper, so the parser turns those
      fragments into real calls and keeps them out of speech.
- [x] `emotion` arrives as a WS frame and plays a one-shot expression on the dot
      (`smile`, `sad`, `surprise`, `ask`, `angry`, `laugh`).
- [x] Replaced the failing curl-based search (DuckDuckGo 202 / Brave 429) with
      `browse`/`navigate`, `browser_click`, `browser_type`, `browser_snapshot`
      and `browser_close` (explicit user request only; cleanup is server-owned).
      Playwright AI accessibility trees supply the refs used by actions, matching
      the Playwright MCP pattern. Strictly headless full Chromium with a
      version-consistent Chrome UA and the automation flag disabled;
      public pages only. See `BROWSING.md`.
- [x] Same tool loop for `POST /api/chat`, with a `dot_tools` trace.
- [x] Migrated runtime browser actions to official Playwright MCP with a private
      per-conversation stdio policy gateway. Full 72-tool catalog tested; default
      exposure is eight safe browser tools. Added a local Madrid clock tool;
      weather stays on fixed Open-Meteo endpoints. Live clock/weather/BTC results
      are recorded in `docs/mcp-trial.md`.
- [ ] Local code execution (`run_code`) — deliberately deferred until it can be
      sandboxed; a fetched page must never be able to run code.

## Acceptance

- [x] `./run.sh` → dot pulses, then stays steady; `curl :8765/health` reports `ready`.
- [x] One chat completion through `/api/chat`.
- [x] Killing `llama-server` flips the dot to `error`; clicking retries.
- [x] Ctrl-C / SIGTERM leaves no orphan `llama-server` (7 s with an open SSE tab).
- [x] Voice: synthetic speech → exact transcript → spoken reply
      ("What is two plus two?" → "It is four.", 1.55 s audio). Full-duplex
      protocol tested end to end, including barge-in and backchannel resume.
- [x] Automatic barge-in while speaking, with history truncation to the heard
      prefix.
- [x] Browser smoke test: hands-free arm/mute, persisted armed flag, no console
      errors.
- [x] Audio stack footprint: app `phys_footprint` 1.5 GB with voice loaded
      (1.4 GB over the 0.12 GB web baseline; peak 2.6 GB while loading), slightly
      over the 1.1 GB target. The GGUF itself is page-cache/mmap backed.

## Known risks

- DFlash speculative decoding is not a reliable win on Metal; stays opt-in.
- `mlx-audio`'s Nemotron streaming API is pinned to 0.6.1.
- Kokoro needs `misaki[en]` (spaCy/phonemizer) and Python <3.13.
- Gemma-4 chat template kwargs (`conversational_mode`) require `--jinja`.
- Barge-in relies on the browser's echo cancellation to keep TTS out of the mic.
  Headphones remove the risk; with speakers, measure with `VOICE_DEBUG=1` before
  changing VAD thresholds.
- Streaming ASR partials trail the audio by ~0.7 s on this M5, so barge
  confirmation also uses speech duration (`BARGE_HOLD_MS`).
- The model omits llama.cpp's `<|tool_call>` wrapper and writes bare
  `name{...}` fragments. `InlineToolParser` compensates; a template fix in a
  future llama.cpp release could make that fallback unnecessary.
- Browser automation does not guarantee search engine access. Google also
  returned an antibot page in live validation. Cross-domain assets are blocked
  by the browser network policy, which can limit script-heavy pages. A blocked
  page must never become an invented current answer.
