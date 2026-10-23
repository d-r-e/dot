// Run this function with Playwright's browser_run_code_unsafe tool (filename), with
// ./run.sh already serving localhost:8765. No real microphone or model calls.
(async (page) => {
  const study = await page.context().newPage();
  const checks = [];
  const check = (name, ok) => {
    if (!ok) throw new Error(name);
    checks.push(name);
  };
  try {
    await study.route("**/static/app.js*", route => route.fulfill({
      contentType: "text/javascript", body: "",
    }));
    await study.emulateMedia({ reducedMotion: "no-preference" });
    await study.goto("http://127.0.0.1:8765");
    await study.locator("#dot").click();
    checks.push(...await study.evaluate(async () => {
      const { createPresence } = await import("/static/presence.js");
      const dot = document.querySelector("#dot");
      const p = createPresence(dot);
      window.presenceTest = p;
      const results = [];
      const assert = (name, ok) => {
        if (!ok) throw new Error(name);
        results.push(name);
      };
      const body = dot.querySelector(".dot-body");
      const voice = dot.querySelector(".dot-voice");
      const bounds = JSON.stringify(dot.getBoundingClientRect().toJSON());
      for (const name of ["smile", "sad", "surprise", "ask", "angry", "laugh"]) {
        p.clear();
        dot.dataset.state = "speaking";
        p.emotion(name);
        assert(`${name}: expression`, dot.dataset.emotion === name);
        const animation = body.getAnimations()[0];
        assert(`${name}: animated`, !!animation);
        animation.pause();
        animation.currentTime = animation.effect.getTiming().duration * .35;
        assert(`${name}: pose`, getComputedStyle(body).transform !== "none");
        assert(`${name}: stable target`, JSON.stringify(dot.getBoundingClientRect().toJSON()) === bounds);
      }
      p.clear();
      dot.dataset.state = "thinking";
      p.emotion("smile");
      assert("deferred while thinking", !dot.dataset.emotion);
      dot.dataset.state = "speaking";
      p.playback();
      assert("starts with playback", dot.dataset.emotion === "smile");
      p.clear();
      dot.dataset.state = "listening";
      p.attentive(true, true);
      assert("attentive nod", dot.dataset.attentive === "true" && body.getAnimations().length > 0);
      p.emotion("ask");
      p.attentive(true, true);
      assert("rapid interruption clears expression despite nod cooldown", !dot.dataset.emotion);
      p.clear();
      dot.dataset.state = "error";
      p.state();
      p.emotion("surprise");
      assert("error wins", !dot.dataset.emotion && body.getAnimations().length === 0 && dot.dataset.attentive === "false");

      // Real Web Audio samples, silenced downstream of the analyser.
      const audio = new AudioContext();
      await audio.resume();
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      const analyser = audio.createAnalyser();
      const mute = audio.createGain();
      analyser.fftSize = 512;
      gain.gain.value = .15;
      mute.gain.value = 0;
      oscillator.connect(gain).connect(analyser).connect(mute).connect(audio.destination);
      oscillator.start();
      dot.dataset.state = "speaking";
      p.analyser(analyser);
      p.pause(false);
      const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
      await wait(300);
      assert("real audio drives scale", parseFloat(voice.style.transform.match(/scale\(([^,]+)/)?.[1]) > 1.02);
      p.emotion("laugh");
      assert("gesture and audio coexist", body.getAnimations().length > 0 && voice.style.transform.includes("scale"));
      gain.gain.value = 0;
      await wait(1000);
      assert("silence settles", parseFloat(voice.style.transform.match(/scale\(([^,]+)/)?.[1]) < 1.002);
      p.pause(true);
      assert("pause clears audio motion", voice.style.transform === "");
      p.analyser(null);
      p.clear();
      oscillator.stop();
      await audio.close();
      return results;
    }));

    for (const size of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 320, height: 568 }]) {
      await study.setViewportSize(size);
      for (const state of ["starting", "ready", "loading_voice", "listening", "thinking", "speaking", "error"]) {
        const layout = await study.evaluate(state => {
          const dot = document.querySelector("#dot");
          dot.dataset.state = state;
          presenceTest.state();
          const box = dot.getBoundingClientRect();
          return {
            centered: Math.abs(box.x + box.width / 2 - innerWidth / 2) < 1 &&
              Math.abs(box.y + box.height / 2 - innerHeight / 2) < 1,
            fits: document.documentElement.scrollWidth === innerWidth && box.width >= 144,
          };
        }, state);
        check(`${size.width}px ${state}: centered stable target`, layout.centered && layout.fits);
      }
    }
    await study.emulateMedia({ reducedMotion: "reduce" });
    // Let the matchMedia change listener finish before dispatching an expression.
    await study.waitForFunction(() => matchMedia("(prefers-reduced-motion: reduce)").matches);
    const reduced = await study.evaluate(() => {
      const dot = document.querySelector("#dot");
      dot.dataset.state = "speaking";
      presenceTest.emotion("sad");
      return dot.dataset.emotion;
    });
    // Browsers can expose the just-cancelled CSS transitions for another frame.
    await study.waitForFunction(() => document.querySelector("#dot").getAnimations({ subtree: true }).length === 0);
    check("reduced motion: static expression, no animations", reduced === "sad");
    const palette = await study.evaluate(() => {
      const dot = document.querySelector("#dot");
      presenceTest.clear();
      const colors = {};
      for (const [name, hue] of Object.entries({ smile: 262, sad: 225, surprise: 207, ask: 238, angry: 278, laugh: 272 })) {
        dot.dataset.state = "speaking";
        dot.dataset.emotion = name;
        const style = getComputedStyle(dot);
        colors[name] = { correct: Number(style.getPropertyValue("--dot-hue")) === hue, color: style.color };
      }
      const states = {};
      for (const state of ["starting", "ready", "loading_voice", "listening", "thinking", "speaking", "error"]) {
        dot.dataset.state = state;
        // Also prove error overrides an emotion's palette.
        if (state !== "error") delete dot.dataset.emotion;
        const hue = Number(getComputedStyle(dot).getPropertyValue("--dot-hue"));
        states[state] = hue >= 207 && hue <= 278 && (state !== "error" || hue === 256);
      }
      delete dot.dataset.emotion;
      presenceTest.state();
      return { colors, states, lang: document.documentElement.lang };
    });
    for (const [name, value] of Object.entries(palette.colors)) check(`${name}: blue–purple palette`, value.correct);
    check("six distinct emotion shades", new Set(Object.values(palette.colors).map(value => value.color)).size === 6);
    for (const [state, valid] of Object.entries(palette.states)) check(`${state}: no red palette`, valid);
    check("Spanish document language", palette.lang === "es");
    await study.evaluate(() => {
      const dot = document.querySelector("#dot");
      dot.dataset.state = "ready";
      getComputedStyle(dot).getPropertyValue("--dot-hue");
    });
    await study.emulateMedia({ reducedMotion: "no-preference" });
    await study.evaluate(() => {
      const dot = document.querySelector("#dot");
      getComputedStyle(dot).getPropertyValue("--dot-hue");
      dot.dataset.emotion = "angry";
    });
    await study.waitForFunction(() => document.querySelector("#dot").getAnimations()
      .some(animation => animation.transitionProperty === "--dot-hue"));
    const transition = await study.evaluate(() => {
      const dot = document.querySelector("#dot");
      const animation = dot.getAnimations().find(animation => animation.transitionProperty === "--dot-hue");
      animation.pause();
      animation.currentTime = 325;
      const hue = Number(getComputedStyle(dot).getPropertyValue("--dot-hue"));
      const synchronized = [".dot-core", ".dot-halo", ".dot-ring"].every(selector =>
        Number(getComputedStyle(dot.querySelector(selector)).getPropertyValue("--dot-hue")) === hue);
      dot.getAnimations().forEach(animation => animation.finish());
      delete dot.dataset.emotion;
      return { hue, synchronized };
    });
    check("color interpolates without a red detour", transition.hue > 250 && transition.hue < 278);
    check("core, glow and ring share the interpolated shade", transition.synchronized);
    await study.waitForFunction(() => Number(getComputedStyle(document.querySelector("#dot")).getPropertyValue("--dot-hue")) === 250);
    check("expression eases back to the state palette", true);
    await study.keyboard.press("Tab");
    await study.locator("#dot").focus();
    check("keyboard focus retained", await study.locator("#dot").evaluate(dot => dot === document.activeElement));
    await study.emulateMedia({ forcedColors: "active" });
    check("forced colors: core visible", await study.locator(".dot-core").evaluate(core => {
      const style = getComputedStyle(core);
      return style.backgroundColor !== "rgba(0, 0, 0, 0)";
    }));
    return { passed: checks.length, checks };
  } finally {
    await study.close();
  }
})
