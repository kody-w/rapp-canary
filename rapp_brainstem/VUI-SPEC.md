# RAPP VUI — Build Spec (scrub the old, build fresh from this)

The prior `gesturepad.html` / demo attempts are a **turd — do not polish them, delete them.**
Build fresh from this spec. Iterate WITH a visual loop (screenshot each change),
because this is a look-and-feel artifact that cannot be nailed blind.

## What it is
The **Virtual UI**: a spatial, **hands-free** way to talk to a RAPP Brainstem.
Not a keypad. An immersive 3D scene with a living AI presence you speak to,
that answers with destinations you enter by **gaze + pinch** (and voice).

## THE POINT IS THE GESTURE. Everything else is secondary.
Face/eye tracking moves the cursor; a **hand pinch selects**. That interaction —
look at a portal, pinch to enter — IS the product. Nail that feel first and
alone. **No iframes, no back-button/history, no frame stacks — forget all of it.**
Mouse is only the fallback menu. Build and tune the gesture loop until it feels
effortless, then add voice and the rest.

## References (match this quality bar)
- **NexusWorlds** (kody-w/localFirstTools/NexusWorlds.html) — immersive Three.js
  world, glowing emissive **companion orb**, shader portals you travel into,
  WASD + mouse-look. This is the aesthetic + spatial-navigation north star.
- **iframe-tunneler-10** (kody-w/localtoolsdev) — a central **form that mutates
  and accumulates** with every step; depth = history.
- **The brainstem "First Interview"** (localhost:7071 → `startTour`) — spotlight
  hole + coach card + pulse-to-target that TEACHES the user step by step. The VUI
  must ship this guided loop, VUI-native.

## Non-negotiables (decided this session)
1. **Living 3D centerpiece**, WebGL2 (`canvas.getContext("webgl2")`), Three.js.
   A companion orb / form that **mutates with every turn**; mutation is
   **deterministic by turn-depth** so it can rewind.
2. **LIGHT / bright by default** — the screen is a "mirror" that brightens while
   listening/speaking to **fill-light the user's face for the camera**.
3. **Options as a clean, even orbit** around the center — first at 12 o'clock,
   then 360°/N apart (triangle for 3, cross for 4). **Big** targets, well
   separated (Fitts's law for gaze + pinch). Never crumpled to one side.
4. **Voice-first transcript** — show only the SPOKEN line; full text behind a
   quiet disclosure (⌄).
5. **Apple-grade, minimal — NO emojis.** Thin-stroke SF-style SVG icons only.
   Very few controls. "Just because the keypad had a button doesn't mean we keep it."
6. **Two modes, clearly separated:**
   - **Mouse = the menu** (simple fallback: click, native cursor, `:hover`).
     NO glowing gaze-cursor or dwell ring in mouse mode.
   - **Hands-free = the real VUI** (face + gesture) behind ONE obvious CTA
     ("Enter hands-free"). Only here show the glow cursor + dwell ring.
7. **Barge-in voice** — user can talk over the assistant; it stops and listens.
8. **First Interview guided loop** — spotlight + coach card + pulse leads the user
   end to end through the modalities (speak → choose a portal → go hands-free →
   barge-in → the living form → back-button time-travel). Teaches, highlights, guides.
9. **No frames, no history/back-button, no rewind.** Dropped — do not build it.
10. **Static demo == live shape.** The page always `POST`s `/chat` and reads
    `data.response`. A REMOVABLE "static brainstem" shim intercepts `fetch` and
    returns the identical shape from canned tour data. Delete the shim / pass
    `?server=<url>` → live, page unchanged.

## The response protocol (keep — the brainstem already speaks it)
`/chat` returns `{ response: "<full text>|||VOICE|||<spoken line>|||HOLO|||{json}" }`
- `|||VOICE|||` → the spoken line (what TTS says + what the transcript shows).
- `|||HOLO|||{ "prompt": "...", "options": [{label,value}] }` → the orbit portals.

## Suggested architecture (fresh)
- Single self-contained `.html`. Three.js (r128+) via CDN for the flight; the
  companion orb + emissive shader form is the scene.
- Modules: `scene3d` (orb + mutation-by-depth), `mirror` (light fill), `voice`
  (STT/TTS + barge-in), `vision` (MediaPipe face+hand, gaze→cursor, pinch→select;
  camera OPT-IN), `transport` (fetch /chat; shim separate + removable),
  `orbit` (even-distributed portals), `history` (pushState per turn),
  `tour` (First Interview steps).
- Ship as the flight's `/vui` on `feature/voice-gesturepad` once it clears the bar.

## How to work it (so it doesn't become another turd)
Change one thing → screenshot → judge → next. Never batch blind. Start with the
scene + orb + light theme ALONE and get that beautiful before adding controls.
