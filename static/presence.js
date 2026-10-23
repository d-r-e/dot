// Visual-only behavior. Never opens a mic, sends a frame, or changes voice state.
const EXPRESSIONS = {
  smile: { duration: 1650, poses: [
    [0.12, "translateY(2px) scale(1.04, .96)"],
    [0.32, "translateY(-6px) scale(1.08, .96) rotate(-3deg)"],
    [0.65, "translateY(-3px) scale(1.04, .98) rotate(-2deg)"],
  ] },
  sad: { duration: 2400, poses: [
    [0.25, "translateY(6px) scale(.97, .92) rotate(4deg)"],
    [0.7, "translateY(5px) scale(.98, .94) rotate(3deg)"],
  ] },
  surprise: { duration: 1250, poses: [
    [0.12, "translateY(2px) scale(1.04, .93)"],
    [0.3, "translateY(-8px) scale(1.08, 1.13)"],
    [0.52, "translateY(-3px) scale(.98, 1.04)"],
    [0.72, "translateY(1px) scale(1.02, .99)"],
  ] },
  ask: { duration: 2100, poses: [
    [0.25, "translate(4px, -3px) rotate(-12deg) scale(.98, 1.04)"],
    [0.65, "translate(4px, -3px) rotate(-10deg) scale(.99, 1.03)"],
    [0.84, "translate(-1px, 1px) rotate(3deg)"],
  ] },
  angry: { duration: 1400, poses: [
    [0.18, "translateY(2px) scale(1.07, .92)"],
    [0.32, "translateX(-3px) rotate(-4deg) scale(1.05, .96)"],
    [0.46, "translateX(3px) rotate(4deg) scale(1.05, .96)"],
    [0.62, "translateX(-1px) scale(1.04, .97)"],
  ] },
  laugh: { duration: 1700, poses: [
    [0.1, "translateY(2px) scale(1.08, .91)"],
    [0.23, "translateY(-7px) scale(.97, 1.08) rotate(-4deg)"],
    [0.36, "translateY(2px) scale(1.07, .93)"],
    [0.49, "translateY(-5px) scale(.98, 1.06) rotate(3deg)"],
    [0.62, "translateY(1px) scale(1.04, .96)"],
    [0.76, "translateY(-2px) scale(.99, 1.02)"],
  ] },
};

export function createPresence(dot) {
  const body = dot.querySelector(".dot-body");
  const voice = dot.querySelector(".dot-voice");
  const halo = dot.querySelector(".dot-halo");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let gesture = null;
  let expressionTimer = null;
  let pendingTimer = null;
  let pending = null;
  let lastName = null;
  let lastExpression = -Infinity;
  let lastNod = -Infinity;
  let frame = null;
  let analyser = null;
  let samples = null;
  let level = 0;
  let previousTime = 0;

  function stopMeter() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    level = 0;
    previousTime = 0;
    voice.style.removeProperty("transform");
    halo.style.removeProperty("opacity");
  }

  function meter(time) {
    const elapsed = previousTime ? Math.min(64, time - previousTime) : 16;
    previousTime = time;
    analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (const value of samples) sum += value * value;
    // Fast attack, softer release. Silence really settles; no fake syllables.
    const target = Math.min(1, Math.max(0, Math.sqrt(sum / samples.length) - .008) * 5);
    level += (target - level) * (1 - Math.exp(-elapsed / (target > level ? 45 : 180)));
    voice.style.transform = `scale(${1 + level * .075}, ${1 + level * .12})`;
    halo.style.opacity = String(.55 + level * .45);
    frame = requestAnimationFrame(meter);
  }

  function syncMeter() {
    if (reduced.matches || document.hidden || !analyser || dot.dataset.state !== "speaking" ||
        dot.dataset.paused === "true") {
      stopMeter();
    } else if (frame === null) {
      frame = requestAnimationFrame(meter);
    }
  }

  function clear() {
    clearTimeout(expressionTimer);
    clearTimeout(pendingTimer);
    pending = null;
    gesture?.cancel();
    gesture = null;
    delete dot.dataset.emotion;
  }

  function play(name) {
    const expression = EXPRESSIONS[name];
    // Capture the current pose so a newer expression doesn't snap to neutral.
    const start = getComputedStyle(body).transform;
    clear();
    dot.dataset.emotion = name;
    if (!reduced.matches && !document.hidden) {
      gesture = body.animate([
        { offset: 0, transform: start === "none" ? "none" : start },
        ...expression.poses.map(([offset, transform]) => ({ offset, transform })),
        { offset: 1, transform: "none" },
      ], { duration: expression.duration, easing: "cubic-bezier(.4, 0, .2, 1)" });
    }
    expressionTimer = setTimeout(clear, expression.duration);
  }

  function emotion(name) {
    if (!Object.hasOwn(EXPRESSIONS, name) || dot.dataset.state === "error") return;
    const now = performance.now();
    if (name === lastName && now - lastExpression < 900) return;
    lastName = name;
    lastExpression = now;
    if (dot.dataset.state === "thinking") {
      clear();
      pending = name;
      // Tool calls can precede slow synthesis. Expire rather than react late.
      pendingTimer = setTimeout(() => { pending = null; }, 5000);
    } else {
      play(name);
    }
  }

  function playback() {
    if (pending) play(pending);
    syncMeter();
  }

  function attentive(active, nod = false) {
    dot.dataset.attentive = String(active);
    if (!active || !nod) return;
    // Yield the expression even when another nod would be too soon.
    clear();
    if (reduced.matches || document.hidden || dot.dataset.state === "error") return;
    const now = performance.now();
    if (now - lastNod < 2000) return;
    lastNod = now;
    gesture = body.animate([
      { transform: "none" },
      { transform: "translateY(3px) scale(1.025, .97)", offset: .3 },
      { transform: "translateY(-2px) scale(.99, 1.02)", offset: .65 },
      { transform: "none" },
    ], { duration: 650, easing: "ease-in-out" });
  }

  function state() {
    if (["error", "starting", "loading_voice"].includes(dot.dataset.state)) clear();
    if (dot.dataset.state !== "listening") attentive(false);
    syncMeter();
  }

  reduced.addEventListener("change", () => {
    if (reduced.matches) gesture?.cancel();
    syncMeter();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clear();
    syncMeter();
  });

  return {
    emotion, clear, attentive, state, playback,
    analyser(node) {
      stopMeter();
      analyser = node;
      samples = node ? new Float32Array(node.fftSize) : null;
      syncMeter();
    },
    pause(paused) {
      dot.dataset.paused = String(paused);
      syncMeter();
    },
  };
}
