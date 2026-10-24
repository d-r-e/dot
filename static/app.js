// Dot UI: SSE for llama health, WebSocket for the voice loop.
// Typing reveals a plain-text conversation beside the dot.
//
// Voice is hands-free once armed: the mic stays open (including while the
// agent speaks), Silero decides when speech starts, and the server drives
// turn-taking. A click only arms, mutes, or cancels.

import { createPresence } from "./presence.js";
import { createTextChat } from "./chat.js?v=1";

const dot = document.getElementById("dot");
const statusEl = document.getElementById("status");
const hintEl = document.getElementById("hint");
const hintTextEl = document.getElementById("hint-text");
const presence = createPresence(dot);

const VOICE_STATES = new Set([
  "loading_voice",
  "listening",
  "thinking",
  "speaking",
  "error",
]);

const ARMED_KEY = "dot.armed";

let llamaState = "starting";
let llamaDetail = "starting";
let voiceState = null;
let voiceDetail = "";
let voiceReady = false;
let armed = false;
let armGeneration = 0;

let ws = null;
let wsOpen = null;
let ctx = null;
let micStream = null;
let captureNode = null;
let captureSource = null;
let ttsGain = null;
let ttsRate = 24000;

// One playback turn. Buffers are kept until the turn ends so a false
// interruption can resume exactly where the user heard it stop.
let turn = null;
let playTimer = null;
let booted = false;
let browsingHost = "";
let reasoningActive = false;
let textThinking = false;

const textChat = createTextChat({
  onActivate() {
    // One input channel at a time; typing never requests microphone permission.
    if (voiceState === "thinking" || voiceState === "speaking") textChat.interruptVoice();
    if (armed || voiceState) disarm();
    if (voiceState === "error") setVoiceState(null, "");
  },
  onBusy(value) { textThinking = value; render(); },
  onEmotion: flashEmotion,
});

function render() {
  const raw = llamaState === "error" ? "error" :
    textThinking ? "thinking" : voiceState && VOICE_STATES.has(voiceState) ? voiceState : llamaState;
  const state = raw === "stopped" ? "starting" : raw;
  dot.dataset.state = state;
  dot.dataset.armed = String(armed);
  presence.state();
  dot.setAttribute("aria-label", labelFor(state));
  statusEl.textContent = labelFor(state);
  // Preserve technical diagnostics without announcing English server details.
  dot.dataset.detail = voiceDetail || llamaDetail || state;
  renderHint();
}

function labelFor(state) {
  switch (state) {
    case "starting":
      return "Iniciando el modelo";
    case "ready":
      return armed ? "Escuchando. Pulsa para silenciar" : "Listo. Pulsa para hablar";
    case "loading_voice":
      return "Preparando el micrófono y la voz";
    case "listening":
      return "Escuchando";
    case "thinking":
      return "Pensando. Habla para interrumpir";
    case "speaking":
      return "Hablando. Habla para interrumpir";
    default:
      return "Error. Pulsa para volver a intentarlo";
  }
}

function setVoiceState(state, detail = "") {
  voiceState = state;
  voiceDetail = detail;
  render();
}

function flashEmotion(name) {
  presence.emotion(name);
}

// One line under the dot: a hostname while a page opens, "pensando" while
// the model reasons. Never two at once; the hostname wins.
function renderHint() {
  const thinking = textThinking || reasoningActive || voiceState === "thinking";
  const text = browsingHost || (thinking ? "pensando" : "");
  if (!text) {
    hintEl.classList.remove("is-on");
    return;
  }
  hintTextEl.textContent = text;
  hintEl.dataset.mode = browsingHost ? "browsing" : "thinking";
  hintEl.classList.add("is-on");
}

function showBrowsing(host) {
  browsingHost = typeof host === "string" ? host : "";
  renderHint();
}

function setReasoning(active) {
  reasoningActive = active === true;
  renderHint();
}

function clearHint() {
  browsingHost = "";
  reasoningActive = false;
  renderHint();
}

function clearEmotion() {
  presence.clear();
}

// ---------------------------------------------------------------- llama state
const events = new EventSource("/events");
events.onmessage = (event) => {
  try {
    const snapshot = JSON.parse(event.data);
    llamaState = snapshot.state || "starting";
    llamaDetail = snapshot.detail || "";
    if (llamaState === "error") {
      disarm();
    }
    render();
  } catch {
    /* ignore malformed frame */
  }
};
events.onerror = () => {
  // EventSource reconnects on its own; keep the last known state.
};

async function restartLlama() {
  llamaState = "starting";
  llamaDetail = "restarting";
  render();
  try {
    await fetch("/api/restart", { method: "POST" });
  } catch {
    /* SSE will report the real state */
  }
}

// ------------------------------------------------------------------- audio in
async function ensureAudio() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error(`el micrófono necesita HTTPS; abre https://${location.host}`);
  }
  if (!ctx) {
    ctx = new AudioContext();
    await ctx.audioWorklet.addModule("/static/pcm-worklet.js");
    ttsGain = ctx.createGain();
    ttsGain.gain.value = 1;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    ttsGain.connect(analyser);
    analyser.connect(ctx.destination);
    presence.analyser(analyser);
  }
  if (ctx.state === "suspended") {
    try {
      await ctx.resume();
    } catch {
      /* needs a gesture; the global unlock handler will retry */
    }
  }
  if (!micStream) {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    captureSource = ctx.createMediaStreamSource(micStream);
    captureNode = new AudioWorkletNode(ctx, "pcm-worklet");
    captureNode.port.onmessage = (event) => {
      if (armed && voiceReady && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(event.data);
      }
    };
    const mute = ctx.createGain();
    mute.gain.value = 0;
    captureSource.connect(captureNode);
    captureNode.connect(mute);
    mute.connect(ctx.destination);
  }
}

function closeAudio() {
  presence.analyser(null);
  if (captureNode) {
    captureNode.port.onmessage = null;
    try {
      captureNode.disconnect();
    } catch {
      /* noop */
    }
    captureNode = null;
  }
  if (captureSource) {
    try {
      captureSource.disconnect();
    } catch {
      /* noop */
    }
    captureSource = null;
  }
  if (micStream) {
    for (const track of micStream.getTracks()) track.stop();
    micStream = null;
  }
  if (ctx) {
    ctx.close().catch(() => {});
    ctx = null;
  }
  ttsGain = null;
  discardTurn();
}

// ------------------------------------------------------------------ audio out
function newTurn(id) {
  discardTurn();
  turn = {
    id,
    chunks: [],
    totalMs: 0,
    sources: [],
    startAt: 0,
    endAt: 0,
    playedMs: 0,
    playing: false,
    paused: false,
    drop: false,
  };
  return turn;
}

function discardTurn() {
  presence.pause(true);
  if (playTimer) {
    clearTimeout(playTimer);
    playTimer = null;
  }
  if (turn) {
    for (const source of turn.sources) {
      try {
        source.stop();
      } catch {
        /* already stopped */
      }
    }
  }
  turn = null;
}

function schedule(chunk, fromMs, at) {
  if (!ctx || !turn || !ttsGain) return;
  const offset = Math.max(0, (fromMs - chunk.startMs) / 1000);
  if (offset >= chunk.buffer.duration) return;
  const source = ctx.createBufferSource();
  source.buffer = chunk.buffer;
  source.connect(ttsGain);
  source.start(at, offset);
  turn.sources.push(source);
  source.onended = () => {
    if (turn) turn.sources = turn.sources.filter((item) => item !== source);
  };
}

function startPlayback() {
  if (!ctx || !turn || turn.playing) return;
  turn.playing = true;
  if (turn.paused) {
    // Speech started before the first chunk arrived; stay paused so a
    // backchannel can resume from the beginning instead of talking over it.
    return;
  }
  turn.startAt = ctx.currentTime + 0.06;
  presence.pause(false);
  presence.playback();
  let at = turn.startAt;
  for (const chunk of turn.chunks) {
    schedule(chunk, chunk.startMs, at);
    at += chunk.durMs / 1000;
  }
  turn.endAt = at;
  scheduleEnd();
}

function scheduleEnd() {
  if (playTimer) {
    clearTimeout(playTimer);
    playTimer = null;
  }
  if (!ctx || !turn || !turn.playing || turn.paused) return;
  const remaining = Math.max(0, (turn.endAt - ctx.currentTime) * 1000);
  playTimer = setTimeout(() => {
    playTimer = null;
    if (!ctx || !turn || turn.paused) return;
    if (turn.endAt - ctx.currentTime > 0.08) {
      scheduleEnd();
      return;
    }
    turn.playing = false;
    presence.pause(true);
    if (!turn.reported) {
      turn.reported = true;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "playback_done", turn_id: turn.id }));
      }
    }
    if (voiceState === "speaking") setVoiceState(null, "");
  }, Math.max(60, remaining + 120));
}

function queueAudio(buffer) {
  if (!ctx || !turn || turn.drop) return;
  const pcm = new Int16Array(buffer);
  if (!pcm.length) return;
  const floats = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i += 1) {
    floats[i] = pcm[i] / 32768;
  }
  const audioBuffer = ctx.createBuffer(1, floats.length, ttsRate);
  audioBuffer.copyToChannel(floats, 0);
  const chunk = {
    buffer: audioBuffer,
    startMs: turn.totalMs,
    durMs: audioBuffer.duration * 1000,
  };
  turn.chunks.push(chunk);
  turn.totalMs += chunk.durMs;
  if (!turn.playing) {
    startPlayback();
    return;
  }
  if (!turn.paused) {
    const at = Math.max(ctx.currentTime + 0.02, turn.endAt);
    schedule(chunk, chunk.startMs, at);
    turn.endAt += chunk.durMs / 1000;
    scheduleEnd();
  }
}

function pausePlayback() {
  presence.pause(true);
  if (!ctx || !turn) return 0;
  if (turn.paused) return turn.playedMs;
  if (!turn.playing) {
    turn.paused = true;
    // Audio that already finished must not replay on a false interruption.
    turn.playedMs = turn.totalMs;
    return turn.playedMs;
  }
  turn.playedMs = Math.max(0, (ctx.currentTime - turn.startAt) * 1000);
  turn.paused = true;
  if (playTimer) {
    clearTimeout(playTimer);
    playTimer = null;
  }
  for (const source of turn.sources) {
    try {
      source.stop();
    } catch {
      /* already stopped */
    }
  }
  turn.sources = [];
  return turn.playedMs;
}

function resumePlayback() {
  if (!ctx || !turn || !turn.paused) return;
  const from = Math.min(turn.playedMs, turn.totalMs);
  turn.paused = false;
  presence.attentive(false);
  presence.pause(false);
  presence.playback();
  turn.playing = true;
  turn.startAt = ctx.currentTime + 0.05 - from / 1000;
  let at = ctx.currentTime + 0.05;
  for (const chunk of turn.chunks) {
    const end = chunk.startMs + chunk.durMs;
    if (end <= from) continue;
    schedule(chunk, Math.max(from, chunk.startMs), at);
    at += (end - Math.max(from, chunk.startMs)) / 1000;
  }
  turn.endAt = at;
  scheduleEnd();
}

function playedMs() {
  if (!ctx || !turn) return 0;
  if (turn.paused) return turn.playedMs;
  if (!turn.playing) return 0;
  return Math.max(0, (ctx.currentTime - turn.startAt) * 1000);
}

// ------------------------------------------------------------------ voice ws
function connectVoice() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return wsOpen ?? Promise.resolve();
  }
  voiceReady = false;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws/voice`);
  textChat.beginVoiceSession();
  ws = socket;
  socket.binaryType = "arraybuffer";

  wsOpen = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      try {
        socket.close();
      } catch {
        /* noop */
      }
      reject(new Error("voice connection timed out"));
    }, 30000);
    socket.addEventListener(
      "open",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
    socket.addEventListener(
      "error",
      () => {
        clearTimeout(timer);
        reject(new Error("voice socket failed"));
      },
      { once: true },
    );
    socket.addEventListener(
      "close",
      () => {
        clearTimeout(timer);
        reject(new Error("voice socket closed"));
      },
      { once: true },
    );
  });

  socket.onmessage = (event) => {
    if (ws !== socket) return;
    if (typeof event.data !== "string") {
      queueAudio(event.data);
      return;
    }
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    textChat.voiceMessage(message);
    switch (message.type) {
      case "state": {
        const state = message.state;
        if (state === "ready") {
          voiceReady = true;
          const stillPlaying = turn && turn.playing && !turn.paused;
          setVoiceState(stillPlaying ? "speaking" : null, stillPlaying ? "speaking" : "");
          if (stillPlaying) scheduleEnd();
        } else if (state === "loading_voice") {
          voiceReady = false;
          setVoiceState("loading_voice", message.detail || "loading speech models");
        } else if (state === "listening") {
          setVoiceState("listening", message.detail || "listening");
        } else if (state === "thinking") {
          setVoiceState("thinking", message.detail || "thinking");
        } else if (state === "speaking") {
          setVoiceState("speaking", message.detail || "speaking");
        }
        break;
      }
      case "sample_rate":
        ttsRate = Number(message.value) || 24000;
        break;
      case "speech_started":
        presence.attentive(true, true);
        dot.dataset.transcript = "";
        break;
      case "partial":
        if (message.text) dot.dataset.transcript = message.text;
        break;
      case "final":
        presence.attentive(false);
        dot.dataset.transcript = message.text || "";
        break;
      case "assistant_delta":
        break;
      case "emotion":
        flashEmotion(message.value);
        break;
      case "browsing":
        showBrowsing(typeof message.host === "string" ? message.host : "");
        break;
      case "reasoning":
        setReasoning(message.active === true);
        break;
      case "tts_begin":
        newTurn(message.turn_id);
        break;
      case "pause_playback":
        pausePlayback();
        break;
      case "resume":
        resumePlayback();
        break;
      case "barge_in": {
        const heard = turn ? pausePlayback() : 0;
        const turnId = message.turn_id ?? (turn && turn.id);
        if (turn) {
          turn.drop = true;
          turn.chunks = [];
        }
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(
            JSON.stringify({
              type: "interrupt",
              turn_id: turnId,
              played_ms: Math.round(heard),
            }),
          );
        }
        discardTurn();
        clearHint();
        setVoiceState("listening", "listening");
        break;
      }
      case "cancel_playback":
        discardTurn();
        clearEmotion();
        clearHint();
        break;
      case "interrupted":
      case "tts_end":
        break;
      case "error":
        disarm();
        setVoiceState("error", message.error || "voice error");
        break;
      default:
        break;
    }
  };

  socket.onclose = () => {
    if (ws !== socket) return;
    ws = null;
    wsOpen = null;
    voiceReady = false;
    armed = false;
    presence.clear();
    presence.attentive(false);
    clearHint();
    cancelPlayback();
    if (voiceState !== "error") setVoiceState(null, "");
    render();
  };

  return wsOpen;
}

function closeVoice() {
  if (ws) {
    try {
      ws.close();
    } catch {
      /* noop */
    }
  }
  ws = null;
  wsOpen = null;
  voiceReady = false;
}

function cancelPlayback() {
  discardTurn();
}

// ------------------------------------------------------------------ arm / mute
async function arm() {
  if (armed || textChat.busy) return;
  const generation = ++armGeneration;
  setVoiceState("loading_voice", "opening microphone");
  try {
    await ensureAudio();
    if (generation !== armGeneration) { closeAudio(); return; }
  } catch (error) {
    if (generation !== armGeneration) return;
    setVoiceState("error", `microphone: ${error.message}`);
    return;
  }
  try {
    await connectVoice();
    if (generation !== armGeneration) { closeAudio(); return; }
  } catch (error) {
    if (generation !== armGeneration) return;
    setVoiceState("error", `voice: ${error.message}`);
    return;
  }
  armed = true;
  try {
    localStorage.setItem(ARMED_KEY, "1");
  } catch {
    /* private mode */
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "context", messages: textChat.context() }));
    ws.send(JSON.stringify({ type: "arm" }));
  }
  setVoiceState(null, "");
  render();
}

function disarm() {
  armGeneration += 1;
  const wasArmed = armed;
  armed = false;
  voiceReady = false;
  clearEmotion();
  presence.attentive(false);
  clearHint();
  try {
    localStorage.removeItem(ARMED_KEY);
  } catch {
    /* private mode */
  }
  if (wasArmed && ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "disarm" }));
    } catch {
      /* socket already gone */
    }
  }
  cancelPlayback();
  closeAudio();
  if (voiceState !== "error") setVoiceState(null, "");
  render();
}

function cancelTurn() {
  cancelPlayback();
  clearEmotion();
  presence.attentive(false);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "cancel" }));
  }
  setVoiceState(null, "");
}

async function boot() {
  if (booted) return;
  booted = true;
  let wanted = false;
  try {
    wanted = localStorage.getItem(ARMED_KEY) === "1";
  } catch {
    return;
  }
  if (!wanted) return;
  if (navigator.permissions) {
    try {
      const status = await navigator.permissions.query({ name: "microphone" });
      if (status.state === "denied") return;
    } catch {
      /* Safari has no microphone permission name */
    }
  }
  await arm();
}

function unlock() {
  if (ctx && ctx.state === "suspended") {
    ctx.resume().catch(() => {});
  }
  if (!armed && booted) boot();
}

// ----------------------------------------------------------------------- click
dot.addEventListener("click", async () => {
  if (textChat.busy) {
    textChat.cancel();
    return;
  }
  if (llamaState === "error") {
    await restartLlama();
    return;
  }
  if (voiceState === "error") {
    disarm();
    return;
  }
  if (!armed) {
    await arm();
    return;
  }
  if (voiceState === "thinking" || voiceState === "speaking") {
    cancelTurn();
    return;
  }
  disarm();
});

window.addEventListener("pointerdown", unlock);
window.addEventListener("keydown", unlock);
window.addEventListener("pagehide", () => {
  // Keep the armed flag so the next load can pick the conversation back up.
  presence.clear();
  cancelPlayback();
  closeVoice();
  closeAudio();
});

render();
boot();
