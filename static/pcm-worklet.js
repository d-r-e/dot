// Capture mono PCM, resample to 16 kHz, and post 20 ms int16 frames.
// The browser's AudioContext rate is arbitrary; this resampler is linear and
// good enough for ASR.

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 320; // 20 ms at 16 kHz

class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.buffer = new Float32Array(0);
    this.pos = 0;
    this.pending = [];
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.append(channel);
    return true;
  }

  append(channel) {
    const merged = new Float32Array(this.buffer.length + channel.length);
    merged.set(this.buffer);
    merged.set(channel, this.buffer.length);
    this.buffer = merged;

    const out = [];
    while (this.pos + 1 < this.buffer.length) {
      const index = Math.floor(this.pos);
      const frac = this.pos - index;
      out.push(this.buffer[index] * (1 - frac) + this.buffer[index + 1] * frac);
      this.pos += this.ratio;
    }

    const consumed = Math.floor(this.pos);
    if (consumed > 0) {
      this.buffer = this.buffer.slice(consumed);
      this.pos -= consumed;
    }

    if (out.length) {
      for (const sample of out) this.pending.push(sample);
      while (this.pending.length >= FRAME_SAMPLES) {
        const frame = new Int16Array(FRAME_SAMPLES);
        for (let i = 0; i < FRAME_SAMPLES; i += 1) {
          const value = Math.max(-1, Math.min(1, this.pending[i]));
          frame[i] = value * 32767;
        }
        this.pending.splice(0, FRAME_SAMPLES);
        this.port.postMessage(frame.buffer, [frame.buffer]);
      }
    }
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
