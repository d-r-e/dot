# Dot — motion research and visual direction

## Live-chat presence revision — 2026-10-23

### Blue–purple palette and Spanish defaults

The red palette below is historical and has now been replaced completely,
including the core, halo, perimeter, thinking arc, error state and page tint.
Typed CSS color channels transition over 650ms on state/expression changes;
all hue interpolation stays between 207° and 278° (blue through violet), so
there is no red flash between colors. Clearing an expression eases back to
the current conversation state's palette. Reduced motion changes colors without
transitions. These brief palette changes repaint gradients; unlike the movement
loops, they are not claimed to be compositor-only.

| Expression | Palette |
| --- | --- |
| Smile | Light lavender |
| Sad | Muted, deeper blue |
| Surprise | Bright ice blue |
| Ask | Periwinkle |
| Angry | Saturated deep violet, never red |
| Laugh | Luminous lilac |

Neutral is indigo, listening is blue, thinking is violet, speaking is bright
indigo, and error is muted purple. These mappings are authored, not a claim
that color alone universally communicates an emotion.

The document language and accessible labels are Spanish. The assistant defaults
to Spanish replies, Nemotron recognition uses `es-ES`, and Kokoro uses Spanish
`lang_code=e` / `ef_dora`. Environment overrides remain supported. Interruption
tokens preserve Spanish accents and include “para”, “espera” and “basta”; short
“sí”, “vale” and “de acuerdo” remain listener signals. Protocol names stay unchanged.

The current implementation extends the original visual pass documented below.
It keeps one dot: no eyes, mouth, visible text, controls, or new dependencies.
Emotion expresses the assistant's conversational intent, **not** a diagnosis of
the user's feelings. Nothing infers emotion from the microphone or webcam.

### Research and interpretation

- [INFP, CVPR 2025](https://openaccess.thecvf.com/content/CVPR2025/html/Zhu_INFP_Audio-Driven_Interactive_Head_Generation_in_Dyadic_Conversations_CVPR_2025_paper.html)
  treats listening and speaking as parts of one interaction, rather than only
  animating the speaker. **Applied:** a small acknowledgement on server-detected
  speech onset and a more receptive perimeter while the user holds the floor.
  This is an attention cue, not a claim of agreement or understanding.
- [GenECA, Interspeech 2025](https://www.isca-archive.org/interspeech_2025/patapati25_interspeech.html)
  emphasizes context-aware gestures synchronized with speech. **Applied:** an
  emotion received during thinking waits for the first playback batch, with a
  five-second expiry. Gestures and audio deformation use separate nested layers.
  This is turn-onset alignment, not word-level alignment or lip synchronization.
- [An Evaluation of Shape Changes for Conveying Emotions, CHI 2016](https://doi.org/10.1145/2858036.2858537)
  reports relationships between shape and valence, and movement and arousal.
  [Saerbeck & Bartneck, HRI 2010](https://www.bartneck.de/publications/2010/perceptionAffectElicitedRobotMotion/saerbeckBartneckHRI2010.pdf)
  studies perceived affect through acceleration and curvature. **Applied:** soft
  lift/opening for warmth, slow sinking for sadness, quick anticipation/recoil
  for surprise, an asymmetric held tilt for curiosity, compressed tension for
  anger, and diminishing squash/stretch bounces for laughter. These are design
  hypotheses for an abstract dot, not validated universal emotion symbols.
- [Tam et al., Assessing Naturalness and Emotional Intensity, 2014](https://www.cs.cmu.edu/~kiesler/publications/2014/2014_assessing-naturalness.pdf)
  found stronger facial motion increased perceived intensity but decreased
  naturalness for their realistic animated character. **Applied cautiously:**
  restrained excursions, recovery to neutral, repeat suppression, no gesture
  queue. That study does not establish optimal amplitudes for an abstract dot.
- [MDN: AnalyserNode](https://developer.mozilla.org/en-US/docs/Web/API/AnalyserNode)
  documents access to the live audio waveform. **Applied:** RMS of synthesized
  playback after its gain node, with 45ms attack / 180ms release. Silent gaps
  settle naturally; paused/cancelled playback immediately removes audio scaling.
  The listener's microphone is not connected to this analyser.

### Current motion score

| Situation | Behavior |
| --- | --- |
| Ready, muted | Still |
| Ready, armed | Barely perceptible 5.4s breath, under 2% stretch |
| User speech onset | One 650ms acknowledgement; at most once per two seconds |
| Listening to speech | Receptive perimeter, not a fake mic-level meter |
| Thinking | Existing orbit; a pending emotion waits for playback |
| Speaking | Actual output RMS, capped at 7.5% horizontal / 12% vertical expansion |
| Expression | 1.25–2.4s gesture with anticipation, hold and recovery |
| Interruption | Yield the expression; audio scaling stops with playback pause |
| Error | Clear expressions, suppress gestures; error always wins |

`static/presence.js` owns visual behavior only. The server, tool schema, VAD,
model threads, mic capture, and turn-taking protocol are unchanged. Runtime
loops stop when the page is hidden, motion is reduced, playback pauses, or audio
is closed. Reduced motion retains static light/perimeter signatures, with no
gesture or audio animation. The button hit area and focus outline never move.

### Verification

`tests/presence.browser.js` is a repeatable Playwright tool script: pass its file
path to `browser_run_code_unsafe` with the app running. It suppresses the application
bootstrap in its isolated test tab, imports the visual controller, and exercises
all six expressions, deferred playback, rapid interruptions, actual Web Audio
samples, silence, pauses, three viewport sizes, reduced motion, keyboard focus,
and forced colors. Palette coverage also checks each emotion/state, interpolated
colors, synchronized layers, and return to the state palette. It does not request
microphone access. A six-expression contact sheet was also visually inspected;
it is a test-only page, not an additional application screen.

The blue–purple revision passed **75 Chromium checks**. Spanish defaults and
accent-preserving interruption vocabulary were checked in Python. A real chat
request returned a Spanish greeting, and Kokoro generated 2.73 seconds of Spanish
PCM with `ef_dora` on the pinned TTS executor. The loaded ASR model confirms
`es-ES` is supported; live Spanish microphone transcription was not tested.

An additional browser integration check drives the real `app.js` with a mocked
WebSocket and silent synthetic microphone stream: arm, listening, pending smile,
PCM playback, pause/resume, cancel and mute. It does not run real ASR/LLM/TTS.
No Safari/Firefox testing, perceptual user study, or frame-rate/battery profiling
has been performed. Motion constants above are authored choices.

---

## Original visual pass (historical)

## Intent

A live presence, not a dashboard. Keep one red dot, no visible copy, no waveform,
no extra controls. The previous large scale pulses, bright specular highlight,
and animated shadows made every state feel similarly urgent. This revision uses
a softer ember-like surface, more negative space, and motion with a specific job.

## Research → decisions

### 1. Paint the light once; move the layer

[web.dev: High-performance CSS animations](https://web.dev/articles/animations-guide)
recommends transform and opacity, avoiding layout/paint-triggering properties
where possible. Blur and shadows can be particularly expensive. It also cautions
against indiscriminate `will-change`.

**Applied:** the halo is a static radial gradient on a separate decorative span.
No keyframe animates a shadow, filter, gradient, border, or layout property. Loops
only transform layers or vary opacity. No permanent `will-change` hints. This is
compositor-friendly design, not a claim of measured frame rate on every device.

### 2. Feedback should explain state, not demand attention

[Apple HIG: Motion](https://developer.apple.com/design/human-interface-guidelines/motion)
and [Material: Understanding motion](https://m2.material.io/design/motion/understanding-motion.html)
frame motion as feedback and communication. Those principles do not prescribe
specific breathing periods for a voice dot. Material's page required JavaScript
when fetched; these are principle-level references, not copied implementation specs.

**Applied:** healthy idle is completely still. Active states use different spatial
signatures: inward breathing for startup, a receptive perimeter for listening,
a traveling arc for processing, and outward resonance for speech. The dot never
travels across the screen. The speaking cadence is decorative state feedback,
**not** a microphone meter or a visualization of real audio amplitude.

### 3. Reduce motion without losing state

[W3C Technique C39](https://www.w3.org/WAI/WCAG22/Techniques/css/C39)
explicitly supports static styles with animation enabled only under
`prefers-reduced-motion: no-preference`.

**Applied:** static-first CSS; reduced-motion users receive no loops, press scaling,
hover scaling, or transitions. Perimeter styles remain: dotted loading, solid
listening, dashed thinking, double speaking. Error has a heavier dashed ring and
a muted core. Existing accessible name and live status stay intact. These subtle
visual signatures complement the accessible status; they are not a complete,
self-explanatory visual vocabulary for every first-time user.

[WCAG 2.2.2: Pause, Stop, Hide](https://www.w3.org/WAI/WCAG22/Understanding/pause-stop-hide.html)
has conditional requirements for automatic movement alongside other content.
Reduced-motion support alone is not a blanket claim of WCAG compliance. Avoiding
a decorative idle loop is the conservative design choice here; active motion
exists only to communicate ongoing activity in this single-control surface.

### 4. Keep interaction spatially predictable

[WCAG 2.5.8: Target Size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
sets a 24 CSS pixel minimum, with exceptions. A stable hit area is an additional
interaction design choice, not a quotation from that criterion.

**Applied:** the real button is 144–168px; the visible core is 80–104px. Only its
non-interactive children animate. The button and keyboard focus outline stay
fixed. Touch has no sticky hover dependency. Forced-colors mode uses system
colors instead of relying on gradients or glow.

## Motion score (design choices, not research-derived constants)

| State | Visual / rhythm |
| --- | --- |
| Starting / loading voice | 2.6s inhale/exhale; core scale 0.97–1.015; faint dotted perimeter |
| Ready | Still, warm red core and low halo |
| Listening | 3.6s perimeter breath; core stays still |
| Thinking | 2s linear arc orbit; core stays still |
| Speaking | 1.9s uneven, small core inflection; one soft outward ring |
| Error | Still, muted red core with heavier broken perimeter |

Hover gently opens the halo; press compresses the core 5%. Interaction settling
uses a decelerating curve; repeating breaths use ease-in-out. Rotation is linear
to avoid a visible speed discontinuity at the loop boundary.

## Scope and verification

Only `static/index.html` and `static/style.css` change application behavior, and
only visually. No JS, protocol, microphone, playback, pause-detection, or server
changes. No dependencies, fonts, assets, or network calls added.

Visual checks use the existing app with `app.js` intercepted to an empty module
in the test browser. This prevents microphone activation and lets every
`data-state` be inspected without racing SSE updates or the parallel voice work.
The live app still loads the unchanged script normally outside that browser.

Verified in Chromium:
- 95 assertions passed: seven states at 1440×1000, 390×844, and 320×568;
  centered layout, no overflow, stable target, reduced motion, distinct static
  perimeter patterns, keyboard focus, and forced-colors core visibility.
- CSSOM audit: all six keyframe definitions use only transform/opacity.
- Running speaking animation and 0.95 press scale verified independently;
  button bounds stay identical during hover/press. The press check explicitly
  finishes its transition before sampling the computed value.
- Desktop, mobile-focus, and state-study screenshots visually inspected.
- `git diff --check` passed for both application files.

Not verified: Safari/Firefox rendering, device-level frame-rate/battery profiling,
assistive-technology announcements, or live voice turn-taking (owned by the
parallel task). No full accessibility conformance claim is made.
