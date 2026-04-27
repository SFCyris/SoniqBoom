// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * visualizer.js — Full-width visualizer rendered behind the track list.
 *
 * The canvas sits `position:absolute; inset:0` inside #content and is
 * non-interactive (pointer-events: none). Opacity transitions in/out with playback.
 *
 * Modes (cycled via the V key):
 *   - 'oscilloscope' — time-domain waveform
 *   - 'spectrogram'  — scrolling frequency spectrogram (waterfall)
 *   - 'hyperspace'   — neon vortex tunnel with rotating geometric objects flying past
 *   - 'synthwave'    — neon Tron grid + reactive sun + beat lightning + spectrum spires
 *   - 'globe'        — wireframe globe rotating behind a colourful frequency-bar halo
 *   - 'cosmic'       — All-Seeing Eye in a kaleidoscopic dreamscape, surreal emoji orbiters
 *   - 'lavalamp'     — psychedelic glowing lava lamp, blobs float behind a pill of glass,
 *                      hue cycles slowly, additive blob blending makes the wax glow
 *   - 'raccoon'      — front-facing raccoon head; mouth opens with the music as if singing,
 *                      pupils dilate on bass kicks, gentle head bob, occasional blink
 */
import { Player } from './player.js';

const canvas = document.getElementById('visualizer-canvas');
const ctx2d  = canvas.getContext('2d');
let rafId    = null;

// ── Mode management ─────────────────────────────────────────────────────────
const MODES = [
  'oscilloscope', 'spectrogram',
  'hyperspace', 'synthwave', 'globe', 'cosmic', 'lavalamp', 'raccoon',
];
let _mode = localStorage.getItem('sb_vis_mode') || 'oscilloscope';
if (!MODES.includes(_mode)) _mode = 'oscilloscope';

// Reusable buffers — allocating per frame pegs Chromium's GC and makes the
// oscilloscope appear laggy on Edge in particular.
let _timeBuf = null;

// Cached oscilloscope stroke gradient — invalidated on resize (see resize()).
let _oscStrokeGrad = null;
function _rebuildOscGradients() {
  const H = canvas.height;
  _oscStrokeGrad = ctx2d.createLinearGradient(0, 0, 0, H);
  _oscStrokeGrad.addColorStop(0,   'rgba(240,114,42,0.55)');
  _oscStrokeGrad.addColorStop(0.5, 'rgba(240,114,42,0.22)');
  _oscStrokeGrad.addColorStop(1,   'rgba(240,114,42,0.04)');
}

// Per-mode state ──────────────────────────────────────────────────────────────
// Spectrogram
let _spectroColumn = 0;
let _spectroImageData = null;

// Hyperspace — neon vortex tunnel with rotating geometric objects flying past.
const _HYPER_TUBE_SEGS = 8;        // sides of the tunnel polygon (octagonal)
const _HYPER_TUBE_RINGS = 16;      // rings stacked along the tunnel
const _HYPER_MAX_OBJECTS = 14;     // cap on simultaneous flying shapes
let _hyperHue = 0;                 // global hue cycle
let _hyperBass = 0;                // smoothed bass (0..1)
let _hyperBassEnv = 0;             // long-term running average for beat detection
let _hyperBoost = 0;               // beat-triggered warp boost, decays each frame
let _hyperRot = 0;                 // tunnel barrel-roll, unwrapped
let _hyperZ = 0;                   // unwrapped depth offset for ring scrolling
let _hyperObjects = [];            // flying geometric shapes
let _hyperSpawn = 0;               // spawn accumulator (1.0 = one new object ready)

// Synthwave — neon retro-futurist hyperdrive.
// Tron grid + reactive sun + beat-triggered lightning + spectrum spires.
let _synthBass = 0;            // smoothed bass envelope (0..1)
let _synthBassEnv = 0;         // long-term running average for beat detection
let _synthBeat = 0;            // peak-detector decay channel (1.0 = fresh beat)
let _synthHue = 0;             // global hue cycle, drifts each frame
let _synthGridZ = 0;           // unwrapped z-position for the receding grid
let _synthStars = null;        // twinkling background stars (lazy-init at resize)
let _synthLightning = [];      // active beat-triggered lightning bolts

// Globe
let _globeRotation = 0;        // longitude offset (radians) — UNWRAPPED for seamless rendering
let _globeHue = 0;             // base hue, drifts each frame
let _globeBass = 0;            // smoothed bass for rotation-speed boost
let _globeFlash = 0;           // beat-triggered colour flash, decays fast
let _globeBassEnv = 0;         // running average of bass for beat detection

// Cosmic — All-Seeing Eye + kaleidoscope + emoji orbiters + ripples
let _cosmicTime = 0;           // accumulating "scene clock" (seconds), feeds orbits/sin
let _cosmicRotation = 0;       // kaleidoscope spin, unwrapped
let _cosmicHue = 0;            // base hue, mod 360 at use
let _cosmicBass = 0;           // smoothed bass (0..1)
let _cosmicMid = 0;            // smoothed mid (0..1) — drives kaleido spin & iris stripes
let _cosmicBassEnv = 0;        // running average of bass for beat detection
let _cosmicIris = 0;           // smoothed iris-dilation (0..1)
let _cosmicBlink = 0;          // 0=open, ramps to ~0.85 on hard kicks, decays fast
let _cosmicRipples = [];       // [{r, max, alpha, hue}, …]  beat-spawned shockwaves
let _cosmicOrbiters = null;    // 6 floating emojis; lazy-init when canvas size known
let _cosmicLastTs = 0;         // performance.now() of previous frame for dt
const _COSMIC_EMOJIS = [
  '🌀','🪐','💀','🫥','🍄','🌈','✨','👁️','🌌','🦋','🔮','🧿',
  '🌙','☄️','🪞','🫧','🐙','🌸','💫','🪬',
];

// Emoji sprite cache. Safari's color-emoji renderer mis-renders fillText()
// under the combination of rotation + globalAlpha + shadowBlur — only the
// shadow paints, the bitmap glyph drops out. Pre-rendering each emoji to an
// offscreen canvas once and using drawImage() bypasses the fillText path and
// composites bitmap pixels reliably across Safari, Firefox, and Chromium.
const _COSMIC_EMOJI_SPRITE_SIZE = 128;
const _cosmicEmojiSprites = new Map();
function _cosmicEmojiSprite(emoji) {
  let sprite = _cosmicEmojiSprites.get(emoji);
  if (sprite) return sprite;
  const s   = _COSMIC_EMOJI_SPRITE_SIZE;
  const pad = Math.ceil(s * 0.15);
  sprite = document.createElement('canvas');
  sprite.width  = s + pad * 2;
  sprite.height = s + pad * 2;
  const sctx = sprite.getContext('2d');
  sctx.font = `${s}px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif`;
  sctx.textAlign    = 'center';
  sctx.textBaseline = 'middle';
  sctx.fillText(emoji, sprite.width / 2, sprite.height / 2);
  _cosmicEmojiSprites.set(emoji, sprite);
  return sprite;
}

// Lavalamp — 3D-shaded glass wax lamp on a wooden table, with a blurred
// reflection.  Lamps are rendered to a per-lamp offscreen canvas so the
// reflection can be a true mirror+blur+fade of the same pixels.  There is
// intentionally no rotation — the cylindrical shading and central specular
// stripe stay fixed so the motion in the scene comes entirely from the
// rising wax blobs and audio-reactive halo glow.
let _lavaTime = 0;             // accumulated seconds
let _lavaLastTs = 0;           // performance.now() of last frame
let _lavaHue = 0;              // base hue, drifts each frame
let _lavaBass = 0;             // smoothed bass (0..1) — pulses blob radii
let _lavaBassEnv = 0;          // long-term running average for beat detection
let _lavaBlobs = null;         // [{y, vy, baseR, swirlPhase, swirlRate, phase, hueShift}, …]
let _lavaOffscreen = null;     // offscreen canvas for the lamp body (re-used for reflection)
let _lavaOffscreenCtx = null;
let _lavaBgCanvas = null;      // cached room+table background (regen on canvas resize)
let _lavaBgCanvasCtx = null;
let _lavaLampsState = null;    // [{hue, blobs, sizeMul, offscreen, offscreenCtx}, …]

// Raccoon — full-body raccoon running toward the camera while singing.
let _raccoonTime = 0;          // accumulated seconds
let _raccoonLastTs = 0;        // performance.now() of last frame
let _raccoonAmp = 0;           // smoothed mouth-opening amplitude (0..1) — fast attack, slow release
let _raccoonBass = 0;          // smoothed bass — pupil dilation + head bob
let _raccoonBob = 0;           // 0..1, follows bass with light damping → head bob offset
let _raccoonBlink = 0;         // 0=open, 1=closed; ramps up on schedule, decays fast
let _raccoonNextBlink = 3;     // seconds until next blink trigger
let _raccoonStepPhase = 0;     // 0..2π running cadence (drives leg cycle + bob)
let _raccoonSwayPhase = 0;     // 0..2π body sway (counter-phase tail swing)
let _raccoonStreaks = null;    // radial speed-line pool [{angle, r, speed}, …]

function _resetModeState() {
  _spectroColumn = 0;
  _spectroImageData = null;
  _hyperHue = 0;
  _hyperBass = 0;
  _hyperBassEnv = 0;
  _hyperBoost = 0;
  _hyperRot = 0;
  _hyperZ = 0;
  _hyperObjects = [];
  _hyperSpawn = 0;
  _synthBass = 0;
  _synthBassEnv = 0;
  _synthBeat = 0;
  _synthHue = 0;
  _synthGridZ = 0;
  _synthStars = null;
  _synthLightning = [];
  _globeRotation = 0;
  _globeBass = 0;
  _globeFlash = 0;
  _globeBassEnv = 0;
  _cosmicTime = 0;
  _cosmicRotation = 0;
  _cosmicBass = 0;
  _cosmicMid = 0;
  _cosmicBassEnv = 0;
  _cosmicIris = 0;
  _cosmicBlink = 0;
  _cosmicRipples = [];
  _cosmicOrbiters = null;
  _cosmicLastTs = 0;
  _lavaTime = 0;
  _lavaLastTs = 0;
  _lavaHue = 0;
  _lavaBass = 0;
  _lavaBassEnv = 0;
  _lavaBlobs = null;
  _lavaOffscreen = null;
  _lavaOffscreenCtx = null;
  _lavaBgCanvas = null;
  _lavaBgCanvasCtx = null;
  _lavaLampsState = null;
  _raccoonTime = 0;
  _raccoonLastTs = 0;
  _raccoonAmp = 0;
  _raccoonBass = 0;
  _raccoonBob = 0;
  _raccoonBlink = 0;
  _raccoonNextBlink = 3;
  _raccoonStepPhase = 0;
  _raccoonSwayPhase = 0;
  _raccoonStreaks = null;
}

function setMode(mode) {
  if (!MODES.includes(mode)) mode = 'oscilloscope';
  _mode = mode;
  localStorage.setItem('sb_vis_mode', mode);
  _resetModeState();
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
}

function toggleMode() {
  const next = MODES[(MODES.indexOf(_mode) + 1) % MODES.length];
  setMode(next);
  return next;
}

// ── Resize canvas to fill its parent ─────────────────────────────────────────
// Cap the internal bitmap by total pixel count while preserving the parent's
// aspect ratio. On 5K / Retina displays the parent is easily 2400×900 CSS
// pixels; per-frame gradient work at native size drops the oscilloscope to
// ~20fps on Chromium. A fixed WxH cap (e.g. 1280×320) used to work for a
// single-line waveform, but it distorts the circular/perspective modes
// (globe, cosmic, synthwave, raccoon) which expect a sane aspect ratio.
const MAX_VIS_PIXELS = 1280 * 320;  // ≈ 409,600 — same budget as before
function resize() {
  const parent = canvas.parentElement;
  if (!parent) return;
  const { width, height } = parent.getBoundingClientRect();
  const parentW = Math.floor(width)  || 800;
  const parentH = Math.floor(height) || 400;
  const scale = Math.min(1, Math.sqrt(MAX_VIS_PIXELS / (parentW * parentH)));
  const targetW = Math.max(1, Math.floor(parentW * scale));
  const targetH = Math.max(1, Math.floor(parentH * scale));
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width  = targetW;
    canvas.height = targetH;
    // Re-seed mode state that depends on canvas dimensions.
    _spectroImageData = null;
    _spectroColumn = 0;
    _synthStars = null;        // synthwave star pool depends on canvas size
    _cosmicOrbiters = null;    // emoji orbits depend on canvas size
    _lavaBlobs = null;         // lavalamp blob seed depends on lamp size
    _lavaOffscreen = null;     // offscreen size depends on lamp size
    _lavaBgCanvas = null;      // cached bg depends on canvas size
    _raccoonStreaks = null;    // speed-line pool depends on canvas size
    _oscStrokeGrad = null;     // oscilloscope gradient is bound to canvas H
  }
}

const _ro = new ResizeObserver(resize);
_ro.observe(canvas.parentElement);
resize();

// ── Colour map for spectrogram ──────────────────────────────────────────────
const _SPECTRO_COLORS = [];
function _buildColorMap() {
  // Warm palette: black → dark red → orange → yellow → white
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let r, g, b;
    if (t < 0.25) {
      // black → dark purple
      const s = t / 0.25;
      r = Math.floor(30 * s);
      g = 0;
      b = Math.floor(60 * s);
    } else if (t < 0.5) {
      // dark purple → red-orange
      const s = (t - 0.25) / 0.25;
      r = Math.floor(30 + 210 * s);
      g = Math.floor(40 * s);
      b = Math.floor(60 * (1 - s));
    } else if (t < 0.75) {
      // red-orange → bright orange
      const s = (t - 0.5) / 0.25;
      r = 240;
      g = Math.floor(40 + 74 * s);
      b = Math.floor(42 * s);
    } else {
      // bright orange → white
      const s = (t - 0.75) / 0.25;
      r = Math.floor(240 + 15 * s);
      g = Math.floor(114 + 141 * s);
      b = Math.floor(42 + 213 * s);
    }
    _SPECTRO_COLORS.push([r, g, b]);
  }
}
_buildColorMap();

// ── Draw one frame ────────────────────────────────────────────────────────────
function draw() {
  rafId = requestAnimationFrame(draw);
  const analyser = Player.analyser;
  if (!analyser) { clear(); return; }

  switch (_mode) {
    case 'spectrogram': _drawSpectrogram(analyser); break;
    case 'hyperspace':  _drawHyperspace(analyser);  break;
    case 'synthwave':   _drawSynthwave(analyser);   break;
    case 'globe':       _drawGlobe(analyser);       break;
    case 'cosmic':      _drawCosmic(analyser);      break;
    case 'lavalamp':    _drawLavaLamp(analyser);    break;
    case 'raccoon':     _drawRaccoon(analyser);     break;
    case 'oscilloscope':
    default:            _drawOscilloscope(analyser); break;
  }
}

function _drawOscilloscope(analyser) {
  const W = canvas.width;
  const H = canvas.height;

  // Time-domain (oscilloscope) data — reuse buffer across frames
  if (!_timeBuf || _timeBuf.length !== analyser.fftSize) {
    _timeBuf = new Uint8Array(analyser.fftSize);
  }
  analyser.getByteTimeDomainData(_timeBuf);
  const buf = _timeBuf;

  ctx2d.clearRect(0, 0, W, H);

  if (!_oscStrokeGrad) _rebuildOscGradients();

  ctx2d.lineWidth   = 1.5;
  ctx2d.strokeStyle = _oscStrokeGrad;
  // No shadowBlur or gradient fill — both are slow ops in Chromium's 2D
  // rasterizer on 5K displays. Two stroked open paths hit only the edge
  // pixels and keep Edge/Chrome pinned at 60fps.

  const sliceW = W / buf.length;
  const midY   = H / 2;

  // Top curve
  ctx2d.beginPath();
  for (let i = 0; i < buf.length; i++) {
    const v = (buf[i] / 128.0 - 1.0);
    const x = i * sliceW;
    const y = midY - v * midY * 0.72;
    i === 0 ? ctx2d.moveTo(x, y) : ctx2d.lineTo(x, y);
  }
  ctx2d.stroke();

  // Bottom curve (mirrored)
  ctx2d.beginPath();
  for (let i = 0; i < buf.length; i++) {
    const v = (buf[i] / 128.0 - 1.0);
    const x = i * sliceW;
    const y = midY + v * midY * 0.72;
    i === 0 ? ctx2d.moveTo(x, y) : ctx2d.lineTo(x, y);
  }
  ctx2d.stroke();
}

function _drawSpectrogram(analyser) {
  const W = canvas.width;
  const H = canvas.height;

  // Frequency domain data
  const bufLen = analyser.frequencyBinCount;
  const freqData = new Uint8Array(bufLen);
  analyser.getByteFrequencyData(freqData);

  // Scroll existing content left by 2px
  const scrollW = 2;
  if (_spectroColumn >= W) {
    // Shift image left
    const existing = ctx2d.getImageData(scrollW, 0, W - scrollW, H);
    ctx2d.putImageData(existing, 0, 0);
    ctx2d.clearRect(W - scrollW, 0, scrollW, H);
    _spectroColumn = W - scrollW;
  }

  // Draw new column at current position
  const x = _spectroColumn;
  for (let y = 0; y < H; y++) {
    // Map y (0=top=high freq, H=bottom=low freq) to frequency bin
    const freqIdx = Math.floor((1 - y / H) * bufLen);
    const val = freqData[Math.min(freqIdx, bufLen - 1)];

    // Apply logarithmic scaling for better visibility
    const scaled = Math.min(255, Math.floor(Math.pow(val / 255, 0.7) * 255));
    const [r, g, b] = _SPECTRO_COLORS[scaled];
    const alpha = 0.12 + (scaled / 255) * 0.45;  // subtle overlay

    ctx2d.fillStyle = `rgba(${r},${g},${b},${alpha})`;
    ctx2d.fillRect(x, y, scrollW, 1);
  }

  _spectroColumn += scrollW;
}

// ── Hyperspace — neon vortex tunnel + flying geometric objects ──────────────
//
// We're flying down a slowly-twisting octagonal tunnel rendered as a wireframe
// mesh of rings + longitudinals.  Inside the tube, free-floating wireframe
// geometric solids (tetrahedra, cubes, octahedra, rings) drift past — each
// rotating on three axes, glowing in its own neon hue, sorted back-to-front
// for proper depth occlusion.  Beat detection (sudden bass jump above running
// envelope) triggers a warp-speed boost that surges the whole scene forward
// for ~1 s and bursts new objects into the field.  Hue cycles globally so the
// palette never sits still.

// 3D wireframe shape vertex/edge tables — all coordinates in object-local
// space, [-1, 1] cube.  Object size + rotation + position are applied per-frame.
const _HYPER_SHAPES = (() => {
  const tetraV = [[1,1,1],[-1,-1,1],[-1,1,-1],[1,-1,-1]];
  const tetraE = [[0,1],[0,2],[0,3],[1,2],[1,3],[2,3]];
  const cubeV  = [
    [-1,-1,-1],[ 1,-1,-1],[ 1, 1,-1],[-1, 1,-1],
    [-1,-1, 1],[ 1,-1, 1],[ 1, 1, 1],[-1, 1, 1],
  ];
  const cubeE  = [
    [0,1],[1,2],[2,3],[3,0],
    [4,5],[5,6],[6,7],[7,4],
    [0,4],[1,5],[2,6],[3,7],
  ];
  const octaV  = [[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[0,0,1],[0,0,-1]];
  const octaE  = [
    [0,2],[0,3],[0,4],[0,5],
    [1,2],[1,3],[1,4],[1,5],
    [2,4],[2,5],[3,4],[3,5],
  ];
  // Ring (circle of points lying in the local XY plane).
  const ringN  = 14;
  const ringV  = [];
  const ringE  = [];
  for (let i = 0; i < ringN; i++) {
    const a = (i / ringN) * Math.PI * 2;
    ringV.push([Math.cos(a), Math.sin(a), 0]);
    ringE.push([i, (i + 1) % ringN]);
  }
  return {
    tetra: { v: tetraV, e: tetraE },
    cube:  { v: cubeV,  e: cubeE  },
    octa:  { v: octaV,  e: octaE  },
    ring:  { v: ringV,  e: ringE  },
  };
})();
const _HYPER_SHAPE_KEYS = ['tetra', 'cube', 'octa', 'ring'];

// Rotate a 3-vector by Euler angles (X, then Y, then Z).
function _rotateXYZ(p, rx, ry, rz) {
  let x = p[0], y = p[1], z = p[2];
  // X-axis
  const cx_ = Math.cos(rx), sx_ = Math.sin(rx);
  const y1 = y * cx_ - z * sx_;
  const z1 = y * sx_ + z * cx_;
  y = y1; z = z1;
  // Y-axis
  const cy_ = Math.cos(ry), sy_ = Math.sin(ry);
  const x2 =  x * cy_ + z * sy_;
  const z2 = -x * sy_ + z * cy_;
  x = x2; z = z2;
  // Z-axis
  const cz_ = Math.cos(rz), sz_ = Math.sin(rz);
  const x3 = x * cz_ - y * sz_;
  const y3 = x * sz_ + y * cz_;
  return [x3, y3, z];
}

// Spawn a new flying object at the back of the tunnel with random shape,
// random offset inside the tube, random rotation + spin, random hue.
function _makeHyperObject(hueBase) {
  const type = _HYPER_SHAPE_KEYS[Math.floor(Math.random() * _HYPER_SHAPE_KEYS.length)];
  const startAngle = Math.random() * Math.PI * 2;
  const startR = 0.2 + Math.random() * 0.7;     // inside the tube (radius ≈ 1)
  return {
    type,
    x: Math.cos(startAngle) * startR,
    y: Math.sin(startAngle) * startR,
    z: 5 + Math.random() * 3,                   // start at the far end
    rx: Math.random() * Math.PI * 2,
    ry: Math.random() * Math.PI * 2,
    rz: Math.random() * Math.PI * 2,
    vrx: (Math.random() - 0.5) * 0.07,
    vry: (Math.random() - 0.5) * 0.07,
    vrz: (Math.random() - 0.5) * 0.07,
    // Mix of complementary hues — half pull from the global hue, half from its complement.
    hue: (hueBase + Math.random() * 100 + (Math.random() < 0.45 ? 180 : 0)) % 360,
    size: 0.12 + Math.random() * 0.22,
  };
}

function _drawHyperspace(analyser) {
  const W = canvas.width;
  const H = canvas.height;
  const cx = W / 2;
  const cy = H / 2;
  const f  = Math.min(W, H) * 0.55;          // focal length for 1/z projection

  // ── Audio analysis ────────────────────────────────────────────────────
  const fBuf = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(fBuf);
  const bassEnd = Math.max(4, Math.floor(fBuf.length * 0.10));
  let bass = 0;
  for (let i = 0; i < bassEnd; i++) bass += fBuf[i];
  bass /= (bassEnd * 255);

  // Beat detection — sudden bass jump above running average.
  if (bass > _hyperBassEnv + 0.16 && bass > 0.32) {
    _hyperBoost = Math.min(2.5, _hyperBoost + 1.4);
  }
  _hyperBassEnv += (bass - _hyperBassEnv) * 0.08;
  _hyperBoost   *= 0.92;
  if (_hyperBoost < 0.02) _hyperBoost = 0;
  _hyperBass    += (bass - _hyperBass) * 0.22;
  _hyperHue      = (_hyperHue + 0.6 + _hyperBass * 1.8) % 360;
  _hyperRot     += 0.004 + _hyperBoost * 0.022;
  const warp     = 0.045 * (1 + _hyperBoost * 1.5);
  _hyperZ       += warp;

  // ── Background fade ───────────────────────────────────────────────────
  // Heavier fade during boost so warp streaks look snappier.
  ctx2d.fillStyle = `rgba(2,0,12,${0.28 + _hyperBoost * 0.06})`;
  ctx2d.fillRect(0, 0, W, H);

  ctx2d.lineCap  = 'round';
  ctx2d.lineJoin = 'round';

  // ── Tunnel walls ──────────────────────────────────────────────────────
  // Build ring vertex tables for each of _HYPER_TUBE_RINGS depths.  zFrac in
  // (0,1] cycles forward; we map it to actual z with a power curve so far
  // rings are spaced more loosely than near rings.  Rings rotate with the
  // global twist + a per-depth offset, so the tunnel reads as a helix.
  const rings = [];
  for (let r = 0; r < _HYPER_TUBE_RINGS; r++) {
    const zFrac = ((r + 1 - (_hyperZ % 1)) / _HYPER_TUBE_RINGS);
    if (zFrac <= 0 || zFrac > 1) continue;
    const z = 0.4 + Math.pow(zFrac, 1.6) * 6;
    const pts = new Array(_HYPER_TUBE_SEGS);
    const rotBase = _hyperRot + r * 0.18;
    for (let s = 0; s < _HYPER_TUBE_SEGS; s++) {
      const a = (s / _HYPER_TUBE_SEGS) * Math.PI * 2 + rotBase;
      pts[s] = {
        sx: cx + (Math.cos(a) / z) * f,
        sy: cy + (Math.sin(a) / z) * f,
      };
    }
    rings.push({ r, z, pts });
  }

  // Draw rings sorted back-to-front (closer overdraws farther).
  const ringsByDepth = rings.slice().sort((a, b) => b.z - a.z);
  for (const ring of ringsByDepth) {
    const a = Math.min(1, (1 / ring.z) * 1.2);
    const hue = (_hyperHue + ring.r * 12) % 360;
    ctx2d.strokeStyle = `hsla(${hue}, 100%, 60%, ${a * 0.85})`;
    ctx2d.lineWidth   = Math.max(0.8, Math.min(3.5, (1 / ring.z) * 1.4));
    ctx2d.shadowColor = `hsl(${hue}, 100%, 60%)`;
    ctx2d.shadowBlur  = 10;
    ctx2d.beginPath();
    for (let s = 0; s <= _HYPER_TUBE_SEGS; s++) {
      const p = ring.pts[s % _HYPER_TUBE_SEGS];
      if (s === 0) ctx2d.moveTo(p.sx, p.sy);
      else         ctx2d.lineTo(p.sx, p.sy);
    }
    ctx2d.stroke();
  }

  // Longitudinal "rails" — connect each ring vertex to the same vertex on
  // the next neighbouring ring (sorted by ringIdx so they form continuous
  // lines along the tube).
  const ringsByIdx = rings.slice().sort((a, b) => a.r - b.r);
  for (let i = 0; i < ringsByIdx.length - 1; i++) {
    const A = ringsByIdx[i];
    const B = ringsByIdx[i + 1];
    const zAvg = (A.z + B.z) / 2;
    const a = Math.min(1, (1 / zAvg) * 0.9);
    const hue = (_hyperHue + A.r * 12 + 30) % 360;
    ctx2d.strokeStyle = `hsla(${hue}, 100%, 55%, ${a * 0.55})`;
    ctx2d.lineWidth   = 0.9;
    ctx2d.shadowColor = `hsl(${hue}, 100%, 55%)`;
    ctx2d.shadowBlur  = 5;
    ctx2d.beginPath();
    for (let s = 0; s < _HYPER_TUBE_SEGS; s++) {
      const p1 = A.pts[s];
      const p2 = B.pts[s];
      ctx2d.moveTo(p1.sx, p1.sy);
      ctx2d.lineTo(p2.sx, p2.sy);
    }
    ctx2d.stroke();
  }

  // ── Flying objects ────────────────────────────────────────────────────
  // Update kinematics; cull anything past the camera.
  for (let i = _hyperObjects.length - 1; i >= 0; i--) {
    const obj = _hyperObjects[i];
    obj.z  -= warp;
    obj.rx += obj.vrx;
    obj.ry += obj.vry;
    obj.rz += obj.vrz;
    if (obj.z < 0.1) _hyperObjects.splice(i, 1);
  }
  // Spawn — light continuous baseline + burst on every beat boost.
  _hyperSpawn += 0.06 + _hyperBoost * 0.5 + _hyperBass * 0.18;
  while (_hyperSpawn >= 1 && _hyperObjects.length < _HYPER_MAX_OBJECTS) {
    _hyperSpawn -= 1;
    _hyperObjects.push(_makeHyperObject(_hyperHue));
  }

  // Render objects back-to-front.
  const sortedObjs = _hyperObjects.slice().sort((a, b) => b.z - a.z);
  for (const obj of sortedObjs) {
    const shape = _HYPER_SHAPES[obj.type];
    // Project all vertices: rotate, scale by size, translate by obj position,
    // then 1/z perspective.
    const proj = new Array(shape.v.length);
    let anyVisible = false;
    for (let i = 0; i < shape.v.length; i++) {
      const r  = _rotateXYZ(shape.v[i], obj.rx, obj.ry, obj.rz);
      const wx = obj.x + r[0] * obj.size;
      const wy = obj.y + r[1] * obj.size;
      const wz = obj.z + r[2] * obj.size;
      if (wz <= 0.05) { proj[i] = null; continue; }
      proj[i] = {
        sx: cx + (wx / wz) * f,
        sy: cy + (wy / wz) * f,
      };
      anyVisible = true;
    }
    if (!anyVisible) continue;
    // Fade in as object spawns, fade out as it gets close.
    const farFade  = Math.min(1, (8 - obj.z) / 4);
    const nearFade = Math.min(1, (obj.z - 0.1) / 0.6);
    const alpha    = Math.max(0.15, Math.min(1, farFade * nearFade));
    ctx2d.strokeStyle = `hsla(${obj.hue}, 100%, 65%, ${alpha})`;
    ctx2d.lineWidth   = Math.max(1, Math.min(3.5, (1 / obj.z) * 1.6));
    ctx2d.shadowColor = `hsl(${obj.hue}, 100%, 60%)`;
    ctx2d.shadowBlur  = 14;
    ctx2d.beginPath();
    for (const [a, b] of shape.e) {
      const pa = proj[a], pb = proj[b];
      if (!pa || !pb) continue;
      ctx2d.moveTo(pa.sx, pa.sy);
      ctx2d.lineTo(pb.sx, pb.sy);
    }
    ctx2d.stroke();
  }

  // ── Centre vortex glow ───────────────────────────────────────────────
  const coreR    = Math.min(W, H) * (0.04 + _hyperBass * 0.06 + _hyperBoost * 0.04);
  const coreGrad = ctx2d.createRadialGradient(cx, cy, 0, cx, cy, coreR * 6);
  const coreHue  = (_hyperHue + 180) % 360;
  coreGrad.addColorStop(0,    `hsla(${coreHue}, 100%, 90%, ${0.75 + _hyperBass * 0.20})`);
  coreGrad.addColorStop(0.35, `hsla(${_hyperHue}, 100%, 60%, ${0.30 + _hyperBoost * 0.25})`);
  coreGrad.addColorStop(1,    'rgba(0,0,0,0)');
  ctx2d.shadowBlur = 0;
  ctx2d.fillStyle  = coreGrad;
  ctx2d.beginPath();
  ctx2d.arc(cx, cy, coreR * 6, 0, Math.PI * 2);
  ctx2d.fill();
}

// ── Synthwave hyperdrive ────────────────────────────────────────────────────
//
// Retro-futurist neon scene.  Layers (back → front):
//   1. Sky gradient, deep purple drifting through the colour wheel
//   2. Twinkling star field above the horizon
//   3. Giant neon sun pinned to the horizon, sliced by horizontal black bars,
//      pulses size + glow with the bass
//   4. Beat-triggered jagged lightning bolts arcing from the sun to the sky
//   5. Tron-style perspective grid below the horizon, flowing toward the camera
//   6. Reactive neon spectrum spires rising from the horizon (city silhouette)
// Every layer reacts: sun radiates with bass, grid scrolls faster on bass +
// mids, hue cycles continuously so the whole palette drifts.

// Generate a jagged lightning-bolt path from (x0,y0) to (x1,y1) using
// recursive midpoint displacement.  5 iterations → 33 vertices, getting
// finer at each subdivision (displacement scales by 0.6^iter).
function _makeLightning(x0, y0, x1, y1) {
  let pts = [{x: x0, y: y0}, {x: x1, y: y1}];
  for (let iter = 0; iter < 5; iter++) {
    const next = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      next.push(a);
      const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      // Perpendicular displacement, smaller each subdivision.
      const off = (Math.random() - 0.5) * len * 0.4 * Math.pow(0.6, iter);
      const dx = -(b.y - a.y) / len;
      const dy =  (b.x - a.x) / len;
      next.push({x: mx + dx * off, y: my + dy * off});
    }
    next.push(pts[pts.length - 1]);
    pts = next;
  }
  return { path: pts, life: 1.0, hue: 180 + Math.random() * 80 };
}

function _drawSynthwave(analyser) {
  const W = canvas.width;
  const H = canvas.height;
  const cx = W / 2;
  const horizonY = H * 0.55;

  // ─── Audio analysis ──────────────────────────────────────────────────
  const fBuf = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(fBuf);
  const bassEnd = Math.max(4, Math.floor(fBuf.length * 0.10));
  let bass = 0;
  for (let i = 0; i < bassEnd; i++) bass += fBuf[i];
  bass /= (bassEnd * 255);
  const midEnd = Math.min(fBuf.length, bassEnd * 4);
  let mid = 0;
  for (let i = bassEnd; i < midEnd; i++) mid += fBuf[i];
  mid /= ((midEnd - bassEnd) * 255 || 1);

  // Asymmetric envelope — fast attack, slow release.
  if (bass > _synthBass) _synthBass += (bass - _synthBass) * 0.55;
  else                   _synthBass += (bass - _synthBass) * 0.10;

  // Beat detection vs long-term average.
  _synthBassEnv = _synthBassEnv * 0.97 + bass * 0.03;
  if (bass - _synthBassEnv > 0.16 && _synthBeat < 0.4) _synthBeat = 1.0;
  _synthBeat *= 0.86;

  // Hue cycle (slow drift) and grid scroll (faster on bass + mid).
  _synthHue   += 0.25;
  _synthGridZ += 0.012 + _synthBass * 0.045 + mid * 0.03;

  // Lightning spawn — only on a fresh strong beat, throttled with random gate.
  if (_synthBeat > 0.7 && _synthLightning.length < 3 && Math.random() < 0.45) {
    const tx = cx + (Math.random() - 0.5) * W * 0.95;
    const ty = Math.random() * horizonY * 0.78;
    _synthLightning.push(_makeLightning(cx, horizonY, tx, ty));
  }

  // ─── 1. Sky gradient ─────────────────────────────────────────────────
  const skyHue1 = (_synthHue + 260) % 360;
  const skyHue2 = (_synthHue + 320) % 360;
  const sky = ctx2d.createLinearGradient(0, 0, 0, horizonY);
  sky.addColorStop(0,   `hsl(${skyHue1}, 80%, 6%)`);
  sky.addColorStop(0.6, `hsl(${skyHue2}, 70%, 14%)`);
  sky.addColorStop(1,   `hsl(${(skyHue2 + 20) % 360}, 80%, 22%)`);
  ctx2d.fillStyle = sky;
  ctx2d.fillRect(0, 0, W, horizonY);

  // Foreground "ground" base under the grid — almost black.
  ctx2d.fillStyle = '#04020a';
  ctx2d.fillRect(0, horizonY, W, H - horizonY);

  // ─── 2. Twinkling stars ──────────────────────────────────────────────
  if (!_synthStars) {
    _synthStars = [];
    const starCount = 110;
    for (let i = 0; i < starCount; i++) {
      _synthStars.push({
        x: Math.random() * W,
        y: Math.random() * horizonY * 0.85,
        a: 0.25 + Math.random() * 0.7,
        twinkle: Math.random() * Math.PI * 2,
        speed: 0.02 + Math.random() * 0.04,
      });
    }
  }
  for (const st of _synthStars) {
    st.twinkle += st.speed;
    const a = st.a * (0.4 + 0.6 * Math.abs(Math.sin(st.twinkle)));
    ctx2d.fillStyle = `rgba(255,240,255,${a})`;
    ctx2d.fillRect(st.x, st.y, 1.3, 1.3);
  }

  // ─── 3. Neon sun (sliced) ────────────────────────────────────────────
  const sunBaseR = Math.min(W, H) * 0.18;
  const sunR = sunBaseR * (1 + _synthBass * 0.18 + _synthBeat * 0.06);
  const sunHueTop = (_synthHue + 50)  % 360;    // yellow-orange at top
  const sunHueMid = (_synthHue + 10)  % 360;    // pink in middle
  const sunHueBot = (_synthHue + 330) % 360;    // deep magenta at base

  ctx2d.save();
  ctx2d.shadowColor = `hsl(${sunHueMid}, 100%, 60%)`;
  ctx2d.shadowBlur  = 90 + _synthBass * 80 + _synthBeat * 40;
  const sunGrad = ctx2d.createLinearGradient(cx, horizonY - sunR, cx, horizonY);
  sunGrad.addColorStop(0,    `hsl(${sunHueTop}, 100%, 75%)`);
  sunGrad.addColorStop(0.45, `hsl(${sunHueMid}, 100%, 60%)`);
  sunGrad.addColorStop(1,    `hsl(${sunHueBot}, 100%, 38%)`);
  ctx2d.fillStyle = sunGrad;
  ctx2d.beginPath();
  // Top-half disc: flat bottom rests on the horizon.
  ctx2d.arc(cx, horizonY, sunR, Math.PI, 0);
  ctx2d.closePath();
  ctx2d.fill();
  ctx2d.restore();

  // ─── 4. Lightning bolts ──────────────────────────────────────────────
  for (let i = _synthLightning.length - 1; i >= 0; i--) {
    const lt = _synthLightning[i];
    lt.life -= 0.075;
    if (lt.life <= 0) { _synthLightning.splice(i, 1); continue; }
    const a = lt.life;
    ctx2d.save();
    // Two-pass stroke: wide neon-coloured glow underneath, bright white core on top.
    ctx2d.shadowColor = `hsl(${lt.hue}, 100%, 70%)`;
    ctx2d.shadowBlur  = 22;
    ctx2d.strokeStyle = `hsla(${lt.hue}, 100%, 70%, ${a * 0.85})`;
    ctx2d.lineWidth   = 3.5;
    ctx2d.beginPath();
    for (let p = 0; p < lt.path.length; p++) {
      const pt = lt.path[p];
      if (p === 0) ctx2d.moveTo(pt.x, pt.y);
      else         ctx2d.lineTo(pt.x, pt.y);
    }
    ctx2d.stroke();
    ctx2d.shadowBlur  = 8;
    ctx2d.strokeStyle = `rgba(255,255,255,${a})`;
    ctx2d.lineWidth   = 1.4;
    ctx2d.stroke();
    ctx2d.restore();
  }

  // ─── 5. Tron grid floor ──────────────────────────────────────────────
  ctx2d.save();
  const gridHue = (_synthHue + 180) % 360;          // cyan range
  ctx2d.strokeStyle = `hsla(${gridHue}, 100%, 55%, 0.9)`;
  ctx2d.lineWidth   = 1.2;
  ctx2d.shadowColor = `hsla(${gridHue}, 100%, 60%, 1)`;
  ctx2d.shadowBlur  = 6 + _synthBass * 14;

  // Vertical fan lines — radiate from a tight vanishing point at horizon centre.
  const fanCount = 25;
  ctx2d.beginPath();
  for (let i = 0; i <= fanCount; i++) {
    const t = (i / fanCount - 0.5) * 2;          // -1..1
    const xTop = cx + t * 6;                     // narrow at horizon
    const xBot = cx + t * W * 1.6;               // wide at viewer
    ctx2d.moveTo(xTop, horizonY);
    ctx2d.lineTo(xBot, H);
  }
  ctx2d.stroke();

  // Horizontal lines — perspective rows that flow toward the camera.
  // zFrac in (0,1] cycles forward, reproject with y = horizonY + (H - horizonY) * zFrac^1.7.
  const horizCount = 16;
  ctx2d.beginPath();
  for (let i = 0; i < horizCount; i++) {
    const zFrac = ((i + 1 - (_synthGridZ % 1)) / horizCount);
    if (zFrac <= 0 || zFrac > 1) continue;
    const y = horizonY + (H - horizonY) * Math.pow(zFrac, 1.7);
    if (y > horizonY + 0.5 && y < H + 0.5) {
      ctx2d.moveTo(0, y);
      ctx2d.lineTo(W, y);
    }
  }
  ctx2d.stroke();
  ctx2d.restore();

  // ─── 6. Reactive neon spectrum spires ────────────────────────────────
  // A row of glowing pillars across the horizon, heights driven by the
  // spectrum (logarithmic bin distribution so bass spires aren't all
  // jammed left), hues cycling magenta → pink → cyan.  We skip bars that
  // would obscure the sun's centre so the iconic disc stays visible.
  const barCount = 38;
  const usableBins = Math.min(fBuf.length, 96);
  ctx2d.save();
  for (let b = 0; b < barCount; b++) {
    const binIdx = Math.floor(Math.pow(b / barCount, 1.4) * usableBins);
    const v = fBuf[binIdx] / 255;
    if (v < 0.025) continue;
    const tBar = (b / (barCount - 1) - 0.5) * 2;
    const barX = cx + tBar * W * 0.5;
    if (Math.abs(barX - cx) < sunR * 0.45) continue;       // keep sun visible
    const barW = (W / barCount) * 0.55;
    const barH = v * (H * 0.30) * (0.55 + _synthBass * 0.55);
    const hue = (300 + (b / barCount) * 120 + _synthHue * 0.4) % 360;
    const grad = ctx2d.createLinearGradient(0, horizonY - barH, 0, horizonY);
    grad.addColorStop(0,   `hsla(${(hue + 30) % 360}, 100%, 75%, 1)`);
    grad.addColorStop(0.6, `hsla(${hue}, 100%, 55%, 0.95)`);
    grad.addColorStop(1,   `hsla(${hue}, 100%, 35%, 0.85)`);
    ctx2d.fillStyle = grad;
    ctx2d.shadowColor = `hsl(${hue}, 100%, 60%)`;
    ctx2d.shadowBlur  = 14;
    ctx2d.fillRect(barX - barW / 2, horizonY - barH, barW, barH);
    // Bright cap line on top of each spire.
    ctx2d.fillStyle = `hsla(${(hue + 60) % 360}, 100%, 88%, 0.95)`;
    ctx2d.fillRect(barX - barW / 2, horizonY - barH - 1, barW, 2);
  }
  ctx2d.restore();

  ctx2d.shadowBlur = 0;
}

// ── Spinning globe with spectrum halo ───────────────────────────────────────
//
// Wireframe sphere drawn as latitude rings (horizontal ellipses) plus
// longitude meridians (vertical great-circle ellipses, scaled by sin(lon)).
// Around the equator we project the frequency spectrum as outward radial
// spikes whose hue cycles around the globe.
//
// Important: _globeRotation is *unwrapped* (just keeps growing).  Wrapping
// at 2π was the source of the glitch — even though sin/cos are periodic,
// the "0.6 ×" factor used for the spike rotation made the halo jump every
// 2π/0.012 ≈ 524 frames (~9 s).  Floats stay accurate well into the millions
// of revolutions, so we just let it grow and the wrap is invisible.
//
// Activity: the rotation speed reacts to bass, an inner counter-rotating ring
// of mirror spikes adds motion, and bass beats trigger a brief colour-flash
// that ripples across the halo.
function _drawGlobe(analyser) {
  const W = canvas.width;
  const H = canvas.height;
  const cx = W / 2;
  const cy = H / 2;

  const bufLen = analyser.frequencyBinCount;
  const freqData = new Uint8Array(bufLen);
  analyser.getByteFrequencyData(freqData);

  // ── Bass envelope + beat detection ────────────────────────────────────
  const bassEnd = Math.max(4, Math.floor(bufLen * 0.10));
  let bass = 0;
  for (let i = 0; i < bassEnd; i++) bass += freqData[i];
  bass /= (bassEnd * 255);                           // 0..1

  if (bass > _globeBassEnv + 0.16 && bass > 0.35) {
    _globeFlash = Math.min(1.5, _globeFlash + 1.0);
  }
  _globeBassEnv += (bass - _globeBassEnv) * 0.07;
  _globeFlash *= 0.88;
  if (_globeFlash < 0.01) _globeFlash = 0;
  _globeBass += (bass - _globeBass) * 0.20;

  // Trail / fade.
  ctx2d.fillStyle = 'rgba(4,8,18,0.28)';
  ctx2d.fillRect(0, 0, W, H);

  // Rotation: base + bass-driven boost.  NEVER wrap — sin/cos handle big
  // floats fine and dropping the modulo eliminates the seam-jump glitch.
  _globeRotation += 0.010 + _globeBass * 0.030;
  _globeHue += 0.7 + _globeBass * 1.5;               // unwrapped, modulo only at use

  const R = Math.min(W, H) * 0.28;

  ctx2d.save();
  ctx2d.translate(cx, cy);
  ctx2d.lineWidth = 1;

  // ── Latitude rings (horizontal ellipses) ────────────────────────────
  const LAT_LINES = 10;
  for (let i = 1; i < LAT_LINES; i++) {
    const lat = (i / LAT_LINES - 0.5) * Math.PI;
    const ringR  = R * Math.cos(lat);
    const ringY  = R * Math.sin(lat);
    const ringRY = ringR * 0.18;
    const alpha = 0.18 + Math.cos(lat) * 0.20 + _globeFlash * 0.15;
    ctx2d.strokeStyle = `rgba(150,200,255,${alpha})`;
    ctx2d.beginPath();
    ctx2d.ellipse(0, ringY, ringR, ringRY, 0, 0, Math.PI * 2);
    ctx2d.stroke();
  }

  // ── Longitude meridians ──────────────────────────────────────────────
  // Horizontal radius = R · |sin(lon - rotation)|, signed.  Periodic in
  // _globeRotation so unwrapping it is harmless.
  const LON_LINES = 14;
  for (let i = 0; i < LON_LINES; i++) {
    const lon = (i / LON_LINES) * Math.PI * 2 + _globeRotation;
    const k = Math.sin(lon);
    const meridianRX = Math.abs(k) * R;
    if (meridianRX < 1) continue;
    const facing = k > 0 ? 1 : 0.35;
    const tint = _globeFlash > 0.05
      ? `hsla(${(_globeHue % 360 + 360) % 360}, 90%, 75%, ${0.20 * facing + _globeFlash * 0.20})`
      : `rgba(180,210,255,${0.22 * facing})`;
    ctx2d.strokeStyle = tint;
    ctx2d.beginPath();
    ctx2d.ellipse(0, 0, meridianRX, R, 0, 0, Math.PI * 2);
    ctx2d.stroke();
  }

  // Body glow.
  const bodyGrad = ctx2d.createRadialGradient(0, 0, R * 0.2, 0, 0, R);
  bodyGrad.addColorStop(0, `rgba(60,90,160,${0.35 + _globeFlash * 0.15})`);
  bodyGrad.addColorStop(1, 'rgba(10,20,50,0.0)');
  ctx2d.fillStyle = bodyGrad;
  ctx2d.beginPath();
  ctx2d.arc(0, 0, R, 0, Math.PI * 2);
  ctx2d.fill();

  // ── Outer frequency-spectrum halo (more spikes, drift+flash) ────────
  // Spike angle uses _globeRotation directly — same scalar as the meridians,
  // so spike-vs-globe motion stays in lockstep through the seamless unwrap.
  const SPIKES = 128;
  ctx2d.lineCap = 'round';
  for (let i = 0; i < SPIKES; i++) {
    const a = (i / SPIKES) * Math.PI * 2 + _globeRotation * 0.6;
    const binIdx = 1 + Math.floor((i / SPIKES) * (bufLen - 2));
    const mag = freqData[binIdx] / 255;
    const spikeLen = R * (0.08 + mag * 0.65 + _globeFlash * 0.12);
    const r0 = R * 1.02;
    const r1 = r0 + spikeLen;
    const x0 = Math.cos(a) * r0;
    const y0 = Math.sin(a) * r0 * 0.92;
    const x1 = Math.cos(a) * r1;
    const y1 = Math.sin(a) * r1 * 0.92;
    const hue = (_globeHue + (i / SPIKES) * 360) % 360;
    const lightness = 50 + mag * 20 + _globeFlash * 18;
    const alpha = 0.35 + mag * 0.55 + _globeFlash * 0.20;
    ctx2d.lineWidth = 1.5 + mag * 1.5;
    ctx2d.strokeStyle = `hsla(${hue}, 95%, ${lightness}%, ${alpha})`;
    ctx2d.shadowColor = `hsla(${hue}, 100%, 65%, 0.75)`;
    ctx2d.shadowBlur = 12 + _globeFlash * 18;
    ctx2d.beginPath();
    ctx2d.moveTo(x0, y0);
    ctx2d.lineTo(x1, y1);
    ctx2d.stroke();
  }

  // ── Inner counter-rotating ring of mirror spikes (spinning the other way) ──
  // Adds visible motion even when bass is steady — lower-third bins point
  // INWARD from the globe surface for a "core energy" feel.
  const INNER_SPIKES = 64;
  for (let i = 0; i < INNER_SPIKES; i++) {
    const a = (i / INNER_SPIKES) * Math.PI * 2 - _globeRotation * 0.9;
    const binIdx = 1 + Math.floor((i / INNER_SPIKES) * (bufLen / 3));
    const mag = freqData[binIdx] / 255;
    const spikeLen = R * (0.05 + mag * 0.30);
    const r0 = R * 0.98;
    const r1 = r0 - spikeLen;                         // grows INWARD
    const x0 = Math.cos(a) * r0;
    const y0 = Math.sin(a) * r0 * 0.92;
    const x1 = Math.cos(a) * r1;
    const y1 = Math.sin(a) * r1 * 0.92;
    const hue = (_globeHue + 180 + (i / INNER_SPIKES) * 360) % 360;
    ctx2d.lineWidth = 1.2;
    ctx2d.strokeStyle = `hsla(${hue}, 90%, 70%, ${0.30 + mag * 0.40})`;
    ctx2d.shadowBlur = 8;
    ctx2d.shadowColor = `hsla(${hue}, 100%, 70%, 0.5)`;
    ctx2d.beginPath();
    ctx2d.moveTo(x0, y0);
    ctx2d.lineTo(x1, y1);
    ctx2d.stroke();
  }

  ctx2d.shadowBlur = 0;
  ctx2d.restore();
}

// ── Cosmic All-Seeing Eye (the surreal one) ─────────────────────────────────
//
// A multi-layer trip:
//   1. Slow shifting radial-gradient nebula in the background.
//   2. 8-fold kaleidoscope of frequency-warped colour ribbons spinning behind
//      the eye — mid energy spins it faster, mirror-symmetric so it looks
//      like a sacred-geometry mandala.
//   3. Up to 8 ripple shockwaves that expand from the eye on bass beats.
//   4. 6 surreal emojis (👁🪐🍄🦋🪞🧿…) orbiting on epicycles, each with a
//      finite TTL so the cast keeps changing — never the same scene twice.
//   5. The All-Seeing Eye itself: rainbow sclera, dilating iris with rotating
//      striations, dilating pupil, double catchlight, eyelids that snap shut
//      on hard kicks then re-open in a few frames.
//   6. Outer eye-ring that glows brighter on bass.
//
// Hue, rotation, and time-clock are all UNWRAPPED — sin/cos are periodic so
// nothing seams, and the only modulo is at the HSL use-site.
function _drawCosmic(analyser) {
  const W = canvas.width;
  const H = canvas.height;
  const cx = W / 2;
  const cy = H / 2;
  const R = Math.min(W, H);
  const eyeBase = R * 0.20;

  // Real wall-clock dt (independent of frame rate hiccups).
  const now = performance.now();
  const dt = _cosmicLastTs ? Math.min(0.1, (now - _cosmicLastTs) / 1000) : 0.016;
  _cosmicLastTs = now;
  _cosmicTime += dt;

  // ── Audio analysis: bass + mid + beat ─────────────────────────────────
  const bufLen = analyser.frequencyBinCount;
  const fBuf = new Uint8Array(bufLen);
  analyser.getByteFrequencyData(fBuf);

  const bassEnd = Math.max(4, Math.floor(bufLen * 0.10));
  let bass = 0;
  for (let i = 0; i < bassEnd; i++) bass += fBuf[i];
  bass /= (bassEnd * 255);

  const midStart = bassEnd;
  const midEnd = Math.floor(bufLen * 0.55);
  let mid = 0;
  for (let i = midStart; i < midEnd; i++) mid += fBuf[i];
  mid /= ((midEnd - midStart) * 255);

  // Beat detection (rising bass past envelope) → blink + ripple + flash.
  let kicked = false;
  if (bass > _cosmicBassEnv + 0.16 && bass > 0.35) {
    kicked = true;
    _cosmicBlink = Math.min(0.85, _cosmicBlink + 0.55);
    _cosmicRipples.push({
      r: eyeBase * 1.05,
      max: R * 0.85,
      alpha: 1,
      hue: _cosmicHue,
    });
    if (_cosmicRipples.length > 8) _cosmicRipples.shift();
  }
  _cosmicBassEnv += (bass - _cosmicBassEnv) * 0.07;
  _cosmicBass    += (bass - _cosmicBass)    * 0.20;
  _cosmicMid     += (mid  - _cosmicMid)     * 0.15;
  _cosmicIris    += (bass - _cosmicIris)    * 0.30;
  _cosmicBlink   *= 0.78;     // open the lid quickly after the kick
  if (_cosmicBlink < 0.01) _cosmicBlink = 0;

  _cosmicRotation += (0.10 + _cosmicMid * 0.45) * dt;
  _cosmicHue      += (12 + _cosmicBass * 25)   * dt;     // degrees / second

  // ── Background trail + nebula ────────────────────────────────────────
  ctx2d.fillStyle = 'rgba(2,0,8,0.22)';
  ctx2d.fillRect(0, 0, W, H);

  // Slow shifting nebula gradient (centre at the eye, fading to black).
  const bgGrad = ctx2d.createRadialGradient(cx, cy, R * 0.05, cx, cy, R * 0.75);
  bgGrad.addColorStop(0,   `hsla(${(_cosmicHue + 280) % 360}, 70%, 16%, 0.35)`);
  bgGrad.addColorStop(0.45,`hsla(${(_cosmicHue + 100) % 360}, 80%, 10%, 0.22)`);
  bgGrad.addColorStop(1,   'rgba(0,0,0,0)');
  ctx2d.fillStyle = bgGrad;
  ctx2d.fillRect(0, 0, W, H);

  // ── 8-fold kaleidoscope of frequency-warped colour ribbons ───────────
  const SEGMENTS = 8;
  const SEG_HALF = Math.PI / SEGMENTS;
  ctx2d.save();
  ctx2d.translate(cx, cy);
  ctx2d.rotate(_cosmicRotation);
  ctx2d.lineCap = 'round';
  ctx2d.lineJoin = 'round';

  for (let s = 0; s < SEGMENTS; s++) {
    ctx2d.save();
    ctx2d.rotate((s / SEGMENTS) * Math.PI * 2);
    if (s % 2) ctx2d.scale(1, -1);                  // mirror alternate slices

    for (let layer = 0; layer < 4; layer++) {
      const STEPS = 24;
      const radiusBase = eyeBase * 1.25 + layer * R * 0.05;
      const hue = (_cosmicHue + s * 30 + layer * 70) % 360;

      ctx2d.beginPath();
      for (let i = 0; i <= STEPS; i++) {
        const t = i / STEPS;
        const ang = -SEG_HALF + t * (SEG_HALF * 2);
        const binIdx = Math.floor(t * (bufLen - 1));
        const mag = fBuf[binIdx] / 255;
        const r = radiusBase + R * (0.04 + mag * 0.22);
        const x = Math.cos(ang) * r;
        const y = Math.sin(ang) * r;
        i === 0 ? ctx2d.moveTo(x, y) : ctx2d.lineTo(x, y);
      }
      ctx2d.lineWidth = 1.4 + layer * 0.4;
      ctx2d.strokeStyle = `hsla(${hue}, 95%, ${55 + _cosmicBass * 12}%, ${0.35 + _cosmicBass * 0.30})`;
      ctx2d.shadowColor = `hsla(${hue}, 100%, 70%, 0.65)`;
      ctx2d.shadowBlur = 10 + _cosmicBass * 16;
      ctx2d.stroke();
    }
    ctx2d.restore();
  }
  ctx2d.restore();

  // ── Beat-spawned ripple shockwaves ───────────────────────────────────
  for (let i = _cosmicRipples.length - 1; i >= 0; i--) {
    const rip = _cosmicRipples[i];
    rip.r += R * 0.55 * dt;       // expansion in canvas-units / second
    rip.alpha *= 0.96;
    if (rip.r > rip.max || rip.alpha < 0.02) {
      _cosmicRipples.splice(i, 1);
      continue;
    }
    const hueR = ((rip.hue % 360) + 360) % 360;
    ctx2d.lineWidth = 2.2;
    ctx2d.strokeStyle = `hsla(${hueR}, 100%, 65%, ${rip.alpha * 0.55})`;
    ctx2d.shadowColor = `hsla(${hueR}, 100%, 70%, ${rip.alpha * 0.7})`;
    ctx2d.shadowBlur = 18;
    ctx2d.beginPath();
    ctx2d.arc(cx, cy, rip.r, 0, Math.PI * 2);
    ctx2d.stroke();
  }
  ctx2d.shadowBlur = 0;

  // ── Orbiting surreal emojis ──────────────────────────────────────────
  if (!_cosmicOrbiters) {
    _cosmicOrbiters = [];
    for (let i = 0; i < 6; i++) _cosmicOrbiters.push(_makeOrbiter(R, eyeBase));
  }
  for (let i = 0; i < _cosmicOrbiters.length; i++) {
    const orb = _cosmicOrbiters[i];
    orb.elapsed += dt;
    if (orb.elapsed >= orb.ttl) {
      _cosmicOrbiters[i] = _makeOrbiter(R, eyeBase);
      continue;
    }
    const a = orb.phase + orb.angVel * _cosmicTime;
    // Breathing radius (epicycle): orbit pulses in and out.
    const r = orb.baseR + Math.sin(_cosmicTime * 0.6 + orb.phase * 1.3) * R * 0.05
                        + _cosmicBass * R * 0.04;
    const x = cx + Math.cos(a) * r;
    const y = cy + Math.sin(a) * r;

    // Fade in / out at edges of life.
    const lifeT = orb.elapsed / orb.ttl;
    let alpha = 1;
    if (lifeT < 0.18) alpha = lifeT / 0.18;
    else if (lifeT > 0.82) alpha = (1 - lifeT) / 0.18;
    alpha = Math.max(0, Math.min(1, alpha));

    const size = orb.size * (1 + _cosmicBass * 0.35);
    const hue = (_cosmicHue + a * 40 + orb.hueShift) % 360;
    const sprite = _cosmicEmojiSprite(orb.emoji);
    // Display size is padded (sprite includes 15% padding around the glyph)
    // so the visible emoji ends up matching `size`.
    const drawSize = size * (sprite.width / _COSMIC_EMOJI_SPRITE_SIZE);
    ctx2d.save();
    ctx2d.globalAlpha = alpha * 0.85;
    ctx2d.translate(x, y);
    ctx2d.rotate(orb.spin * _cosmicTime + orb.phase);
    ctx2d.shadowColor = `hsla(${(hue + 360) % 360}, 100%, 70%, 0.75)`;
    ctx2d.shadowBlur = 12 + _cosmicBass * 18;
    ctx2d.drawImage(sprite, -drawSize / 2, -drawSize / 2, drawSize, drawSize);
    ctx2d.restore();
  }

  // ── The All-Seeing Eye ───────────────────────────────────────────────
  const eyeR = eyeBase * (1 + _cosmicBass * 0.10);

  ctx2d.save();
  ctx2d.beginPath();
  ctx2d.arc(cx, cy, eyeR, 0, Math.PI * 2);
  ctx2d.clip();   // everything inside this block is clipped to the eye disc

  // Sclera — rainbow-tinted radial gradient.
  const scleraGrad = ctx2d.createRadialGradient(cx, cy, eyeR * 0.25, cx, cy, eyeR);
  scleraGrad.addColorStop(0, `hsla(${(_cosmicHue + 60)  % 360}, 30%, 92%, 0.92)`);
  scleraGrad.addColorStop(1, `hsla(${(_cosmicHue + 200) % 360}, 80%, 28%, 0.85)`);
  ctx2d.fillStyle = scleraGrad;
  ctx2d.fillRect(cx - eyeR, cy - eyeR, eyeR * 2, eyeR * 2);

  // Iris — rainbow ring, dilates with bass.
  const irisR = eyeR * (0.55 + _cosmicIris * 0.18);
  const irisGrad = ctx2d.createRadialGradient(cx, cy, irisR * 0.20, cx, cy, irisR);
  irisGrad.addColorStop(0,    `hsla(${(_cosmicHue + 200) % 360}, 100%, 60%, 1)`);
  irisGrad.addColorStop(0.55, `hsla(${(_cosmicHue + 120) % 360}, 100%, 38%, 1)`);
  irisGrad.addColorStop(0.85, `hsla(${(_cosmicHue + 40)  % 360}, 100%, 22%, 1)`);
  irisGrad.addColorStop(1,    `hsla(${_cosmicHue % 360},          100%, 8%,  1)`);
  ctx2d.fillStyle = irisGrad;
  ctx2d.beginPath();
  ctx2d.arc(cx, cy, irisR, 0, Math.PI * 2);
  ctx2d.fill();

  // Iris striations — radial fibers that rotate (counter to kaleidoscope).
  const STRIATIONS = 36;
  ctx2d.lineWidth = 0.9;
  for (let i = 0; i < STRIATIONS; i++) {
    const a = (i / STRIATIONS) * Math.PI * 2 - _cosmicRotation * 1.6;
    const hue = (_cosmicHue + i * 10) % 360;
    ctx2d.strokeStyle = `hsla(${hue}, 100%, 80%, ${0.4 + _cosmicMid * 0.4})`;
    ctx2d.beginPath();
    ctx2d.moveTo(cx + Math.cos(a) * irisR * 0.22, cy + Math.sin(a) * irisR * 0.22);
    ctx2d.lineTo(cx + Math.cos(a) * irisR,        cy + Math.sin(a) * irisR);
    ctx2d.stroke();
  }

  // Pupil — also dilates with bass; ringed thinly with a colour band.
  const pupilR = irisR * (0.30 + _cosmicIris * 0.22);
  ctx2d.fillStyle = '#000';
  ctx2d.beginPath();
  ctx2d.arc(cx, cy, pupilR, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.lineWidth = 1.2;
  ctx2d.strokeStyle = `hsla(${(_cosmicHue + 300) % 360}, 100%, 70%, 0.7)`;
  ctx2d.stroke();

  // Catch-light glints (top-left primary, lower-right secondary).
  ctx2d.fillStyle = 'rgba(255,255,255,0.9)';
  ctx2d.beginPath();
  ctx2d.arc(cx - pupilR * 0.42, cy - pupilR * 0.42, pupilR * 0.20, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.fillStyle = 'rgba(255,255,255,0.55)';
  ctx2d.beginPath();
  ctx2d.arc(cx + pupilR * 0.30, cy + pupilR * 0.32, pupilR * 0.10, 0, Math.PI * 2);
  ctx2d.fill();

  // Eyelid blink — black bars from top and bottom that close the eye on kicks.
  // Clipped to the eye disc by the surrounding ctx.clip().
  if (_cosmicBlink > 0) {
    const lidH = _cosmicBlink * eyeR;
    ctx2d.fillStyle = '#000';
    ctx2d.fillRect(cx - eyeR, cy - eyeR, eyeR * 2, lidH);
    ctx2d.fillRect(cx - eyeR, cy + eyeR - lidH, eyeR * 2, lidH);
    // Lash hint along the closing edges.
    ctx2d.lineWidth = 1.5;
    ctx2d.strokeStyle = `hsla(${_cosmicHue % 360}, 80%, 75%, 0.6)`;
    ctx2d.beginPath();
    ctx2d.moveTo(cx - eyeR, cy - eyeR + lidH);
    ctx2d.lineTo(cx + eyeR, cy - eyeR + lidH);
    ctx2d.moveTo(cx - eyeR, cy + eyeR - lidH);
    ctx2d.lineTo(cx + eyeR, cy + eyeR - lidH);
    ctx2d.stroke();
  }

  ctx2d.restore();   // pop the clip

  // Outer eye ring with halo glow that pulses on bass.
  ctx2d.lineWidth = 2.2;
  ctx2d.strokeStyle = `hsla(${(_cosmicHue + 60) % 360}, 100%, 72%, ${0.65 + _cosmicBass * 0.30})`;
  ctx2d.shadowColor = `hsla(${_cosmicHue % 360}, 100%, 70%, 0.85)`;
  ctx2d.shadowBlur = 18 + _cosmicBass * 28 + (kicked ? 25 : 0);
  ctx2d.beginPath();
  ctx2d.arc(cx, cy, eyeR, 0, Math.PI * 2);
  ctx2d.stroke();
  ctx2d.shadowBlur = 0;
}

function _makeOrbiter(R, eyeBase) {
  return {
    emoji:    _COSMIC_EMOJIS[Math.floor(Math.random() * _COSMIC_EMOJIS.length)],
    baseR:    eyeBase * (1.7 + Math.random() * 1.4),
    angVel:   (0.15 + Math.random() * 0.45) * (Math.random() < 0.5 ? -1 : 1),
    phase:    Math.random() * Math.PI * 2,
    spin:     (Math.random() - 0.5) * 0.8,         // self-rotation
    size:     R * (0.045 + Math.random() * 0.045),
    hueShift: Math.floor(Math.random() * 360),
    ttl:      5 + Math.random() * 9,
    elapsed:  0,
  };
}

// ── Lavalamp — 3D-shaded glass cylinder of wax on a wooden table ────────────
//
// The lamp sits on a wooden table near the bottom of the frame.  The glass
// body is rendered as a vertical cylinder using:
//   1. A multiply-blended cylindrical shading gradient (dark left/right,
//      bright centre) — fakes a rounded surface.
//   2. A centred vertical specular stripe — reads as a lit glass surface.
//   No rotation: the shading + glint stay fixed so the eye focuses on the
//   rising wax, not on a sweeping highlight.
// The lamp body is rendered into an offscreen canvas so the table reflection
// can be a true vertical mirror of the same pixels — squished, blurred, and
// alpha-faded toward the floor.  The wood-grain table is precomputed once per
// canvas size into another offscreen canvas, so per-frame cost is constant.

function _initLavaBlobs() {
  const n = 6;
  const blobs = [];
  for (let i = 0; i < n; i++) {
    blobs.push({
      // Distribute initial y across full canvas height — the glass clip culls
      // anything outside automatically, so an over-broad spawn range is fine.
      y: Math.random() * canvas.height,
      // Mix of upward (negative) and downward (positive) drifts; lamps look
      // best with most going up but a couple sinking.
      vy: (Math.random() < 0.65 ? -1 : 1) * (9 + Math.random() * 16),
      baseR: 18 + Math.random() * 22,
      swirlPhase: Math.random() * Math.PI * 2,
      swirlRate: 0.18 + Math.random() * 0.30,        // horizontal sway frequency
      phase: Math.random() * Math.PI * 2,            // radius-pulse phase
    });
  }
  return blobs;
}

// Per-lamp config: three classic Mathmos-silhouette lamps in different colors.
// hue:     base colour for the liquid, internal bulb glow, halo, and wax.
// sizeMul: slight per-lamp size variation so the row doesn't look stamped.
function _initLavaLamps() {
  return [
    { hue: 208, blobs: _initLavaBlobs(), sizeMul: 1.00 }, // blue
    { hue: 340, blobs: _initLavaBlobs(), sizeMul: 0.93 }, // pink / magenta
    { hue: 128, blobs: _initLavaBlobs(), sizeMul: 0.97 }, // green
  ];
}

// Render a wall + wooden table into a cached offscreen canvas.  Regenerated
// only when canvas dimensions or the table line change.
function _ensureLavaBg(W, H, tableY) {
  if (_lavaBgCanvas
      && _lavaBgCanvas.width === W
      && _lavaBgCanvas.height === H
      && _lavaBgCanvas._tableY === tableY) return;
  _lavaBgCanvas = document.createElement('canvas');
  _lavaBgCanvas.width = W;
  _lavaBgCanvas.height = H;
  _lavaBgCanvas._tableY = tableY;
  _lavaBgCanvasCtx = _lavaBgCanvas.getContext('2d');
  const c = _lavaBgCanvasCtx;

  // Wall (above table) — vertical purple-black gradient.
  const wallG = c.createLinearGradient(0, 0, 0, tableY);
  wallG.addColorStop(0,    '#0a0510');
  wallG.addColorStop(0.55, '#160a22');
  wallG.addColorStop(1,    '#1f0e2e');
  c.fillStyle = wallG;
  c.fillRect(0, 0, W, tableY);

  // Wall vignette — radial darkening outward from upper centre.
  const vG = c.createRadialGradient(W / 2, tableY * 0.30, 0,
                                    W / 2, tableY * 0.30, Math.max(W, H) * 0.75);
  vG.addColorStop(0,   'rgba(0,0,0,0)');
  vG.addColorStop(0.7, 'rgba(0,0,0,0.30)');
  vG.addColorStop(1,   'rgba(0,0,0,0.75)');
  c.fillStyle = vG;
  c.fillRect(0, 0, W, tableY);

  // Table — wood gradient below table line.
  const tG = c.createLinearGradient(0, tableY, 0, H);
  tG.addColorStop(0,   '#2a1810');
  tG.addColorStop(0.4, '#1a1008');
  tG.addColorStop(1,   '#0c0604');
  c.fillStyle = tG;
  c.fillRect(0, tableY, W, H - tableY);

  // Wood grain — faint horizontal streaks (deterministic LCG so it's stable).
  c.save();
  c.globalAlpha = 0.10;
  c.fillStyle = '#5a3018';
  let seed = (W * 73856093) ^ (H * 19349663) ^ tableY;
  const rand = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
  for (let y = tableY + 4; y < H; y += 8 + rand() * 9) {
    const len = 0.5 + rand() * 0.45;
    const offset = rand() * (1 - len) * W;
    c.fillRect(offset, y, len * W, 1 + rand() * 1.4);
  }
  c.restore();

  // Bright table edge — thin highlight at the table top (front lip catching light).
  c.fillStyle = 'rgba(120, 80, 50, 0.45)';
  c.fillRect(0, tableY - 1, W, 2);
  c.fillStyle = 'rgba(0, 0, 0, 0.45)';
  c.fillRect(0, tableY + 1, W, 1);
}

// Render one classic Mathmos-silhouette lava lamp into the supplied offscreen.
//   bodyW / bodyH — bounding box of the glass body (ogival / rocket shape).
//   capH          — height of the metal cap above the glass.
//   baseH         — height of the metal base below the glass.
//   lampPad       — top/side padding inside the offscreen (reserved for halo blur).
//   beat          — true during a bass-kick frame (pumps the halo glow).
//   lamp          — per-lamp config: { hue, blobs, sizeMul }.
// Pipeline: cap → glass clip → glass background → internal bulb glow →
// additive wax blobs → multiply cylindrical shading band → screen specular
// stripe → thin top inner shade → halo-glow outline → base.
function _renderLavaBody(oc, bodyW, bodyH, capH, baseH, lampPad, beat, lamp) {
  const cx       = oc.canvas.width / 2;
  const glassTop = lampPad + capH;       // top edge of glass in offscreen coords
  const glassBot = glassTop + bodyH;
  const capY     = lampPad;
  const hue      = lamp.hue;

  // Key silhouette measurements — matches a classic Mathmos tear-drop profile:
  // narrow neck at top, widest ~44% down, narrow neck at bottom where it meets
  // the cone base.
  const neckTopHalf = bodyW * 0.11;
  const widestHalf  = bodyW * 0.50;
  const neckBotHalf = bodyW * 0.21;
  const widestY     = glassTop + bodyH * 0.44;

  // ── Metal cap (trapezoid, narrow→wider) ─────────────────────────────
  const capTopHalf = neckTopHalf * 1.05;
  const capBotHalf = neckTopHalf * 1.75;
  oc.save();
  oc.beginPath();
  oc.moveTo(cx - capTopHalf, capY);
  oc.lineTo(cx + capTopHalf, capY);
  oc.lineTo(cx + capBotHalf, capY + capH);
  oc.lineTo(cx - capBotHalf, capY + capH);
  oc.closePath();
  const capGrad = oc.createLinearGradient(cx - capBotHalf, 0, cx + capBotHalf, 0);
  capGrad.addColorStop(0,    '#2c2630');
  capGrad.addColorStop(0.35, '#7a7480');
  capGrad.addColorStop(0.55, '#b0acb6');
  capGrad.addColorStop(0.75, '#5e5a64');
  capGrad.addColorStop(1,    '#1f1c25');
  oc.fillStyle = capGrad;
  oc.fill();
  oc.fillStyle = 'rgba(255,255,255,0.22)';
  oc.fillRect(cx - capTopHalf, capY, capTopHalf * 2, 1);
  oc.fillStyle = 'rgba(0,0,0,0.55)';
  oc.fillRect(cx - capBotHalf, capY + capH - 1, capBotHalf * 2, 1);
  oc.restore();

  // ── Build the glass silhouette path (re-usable: clip once, stroke once) ─
  const buildGlass = () => {
    oc.beginPath();
    oc.moveTo(cx - neckTopHalf, glassTop);
    oc.lineTo(cx + neckTopHalf, glassTop);
    // Right shoulder → widest
    oc.bezierCurveTo(
      cx + neckTopHalf + bodyW * 0.06, glassTop + bodyH * 0.07,
      cx + widestHalf,                  widestY - bodyH * 0.14,
      cx + widestHalf,                  widestY
    );
    // Right widest → bottom neck
    oc.bezierCurveTo(
      cx + widestHalf,                  widestY + bodyH * 0.22,
      cx + neckBotHalf + bodyW * 0.04, glassBot - bodyH * 0.05,
      cx + neckBotHalf,                 glassBot
    );
    oc.lineTo(cx - neckBotHalf, glassBot);
    // Left bottom neck → widest (mirror)
    oc.bezierCurveTo(
      cx - neckBotHalf - bodyW * 0.04, glassBot - bodyH * 0.05,
      cx - widestHalf,                  widestY + bodyH * 0.22,
      cx - widestHalf,                  widestY
    );
    // Left widest → shoulder
    oc.bezierCurveTo(
      cx - widestHalf,                  widestY - bodyH * 0.14,
      cx - neckTopHalf - bodyW * 0.06, glassTop + bodyH * 0.07,
      cx - neckTopHalf,                 glassTop
    );
    oc.closePath();
  };

  oc.save();
  buildGlass();
  oc.clip();

  // Glass background — very dark hue tint, gradient top → bottom (bottom brighter).
  const glassBg = oc.createLinearGradient(0, glassTop, 0, glassBot);
  glassBg.addColorStop(0,    `hsla(${hue}, 60%, 6%, 1)`);
  glassBg.addColorStop(0.55, `hsla(${hue}, 75%, 14%, 1)`);
  glassBg.addColorStop(1,    `hsla(${hue}, 90%, 30%, 1)`);
  oc.fillStyle = glassBg;
  oc.fillRect(cx - widestHalf - 4, glassTop - 4, widestHalf * 2 + 8, bodyH + 8);

  // Internal bulb glow — bright radial concentrated at the very bottom of
  // the glass (simulating the incandescent bulb shining up), decaying fast
  // so it doesn't wash the middle/top of the lamp.
  oc.save();
  oc.globalCompositeOperation = 'lighter';
  const bulbCY = glassBot - bodyH * 0.02;
  const bulbR  = bodyH * 0.48;
  const bulbGlow = oc.createRadialGradient(cx, bulbCY, 0, cx, bulbCY, bulbR);
  bulbGlow.addColorStop(0,    `hsla(${hue}, 100%, 85%, 0.80)`);
  bulbGlow.addColorStop(0.35, `hsla(${hue}, 100%, 60%, 0.40)`);
  bulbGlow.addColorStop(0.75, `hsla(${hue}, 100%, 45%, 0.10)`);
  bulbGlow.addColorStop(1,    `hsla(${hue}, 100%, 40%, 0.00)`);
  oc.fillStyle = bulbGlow;
  oc.fillRect(cx - widestHalf - 4, glassTop - 4, widestHalf * 2 + 8, bodyH + 8);
  oc.restore();

  // ── Wax blobs — pale centre, hue halo, additive blending ────────────
  oc.save();
  oc.globalCompositeOperation = 'lighter';
  const scale = bodyW / 150;
  const blobSpanHalf = widestHalf * 0.55;
  for (const b of lamp.blobs) {
    const cxB   = cx + Math.sin(b.swirlPhase) * blobSpanHalf;
    const pulse = 1 + Math.sin(_lavaTime * 1.2 + b.phase) * 0.14 + _lavaBass * 0.32;
    const r     = Math.max(8, b.baseR * pulse * scale);
    const grad  = oc.createRadialGradient(cxB, b.y, 0, cxB, b.y, r);
    // Pale core → hue halo → transparent — lamp-hue consistency across lamps.
    grad.addColorStop(0,    `hsla(${hue}, 90%, 92%, 1.00)`);
    grad.addColorStop(0.35, `hsla(${hue}, 100%, 75%, 0.85)`);
    grad.addColorStop(0.75, `hsla(${hue}, 100%, 55%, 0.35)`);
    grad.addColorStop(1,    `hsla(${hue}, 100%, 45%, 0.00)`);
    oc.fillStyle = grad;
    oc.beginPath();
    oc.arc(cxB, b.y, r, 0, Math.PI * 2);
    oc.fill();
  }
  oc.restore();

  // ── Cylindrical shading band — fixed centred highlight (no rotation)
  const bandCentre = 0.5;   // 0..1 across glass — always centred
  oc.save();
  oc.globalCompositeOperation = 'multiply';
  const shadeGrad = oc.createLinearGradient(cx - widestHalf, 0, cx + widestHalf, 0);
  shadeGrad.addColorStop(0,                                  'rgba(40,30,60,1)');
  shadeGrad.addColorStop(Math.max(0, bandCentre - 0.28),     'rgba(95,85,115,1)');
  shadeGrad.addColorStop(bandCentre,                         'rgba(220,215,235,1)');
  shadeGrad.addColorStop(Math.min(1, bandCentre + 0.28),     'rgba(95,85,115,1)');
  shadeGrad.addColorStop(1,                                  'rgba(30,25,45,1)');
  oc.fillStyle = shadeGrad;
  oc.fillRect(cx - widestHalf - 4, glassTop - 4, widestHalf * 2 + 8, bodyH + 8);
  oc.restore();

  // ── Specular highlight stripe (subtle vertical glint on the glass) ──
  oc.save();
  oc.globalCompositeOperation = 'screen';
  const specX = cx - widestHalf + bandCentre * widestHalf * 2;
  const specGrad = oc.createLinearGradient(specX - bodyW * 0.06, 0,
                                           specX + bodyW * 0.06, 0);
  specGrad.addColorStop(0,    'rgba(255,255,255,0.00)');
  specGrad.addColorStop(0.5,  'rgba(255,255,255,0.22)');
  specGrad.addColorStop(1,    'rgba(255,255,255,0.00)');
  oc.fillStyle = specGrad;
  oc.fillRect(specX - bodyW * 0.06, glassTop, bodyW * 0.12, bodyH);
  oc.restore();

  // ── Thin top darkening — gives the glass neck volume (bottom isn't
  // darkened because the bulb glow already brightens it).
  oc.save();
  oc.globalCompositeOperation = 'multiply';
  const topShade = oc.createLinearGradient(0, glassTop, 0, glassTop + bodyH * 0.18);
  topShade.addColorStop(0, 'rgba(30,25,45,1)');
  topShade.addColorStop(1, 'rgba(255,255,255,1)');
  oc.fillStyle = topShade;
  oc.fillRect(cx - widestHalf, glassTop, widestHalf * 2, bodyH * 0.18);
  oc.restore();

  oc.restore();   // pop glass clip

  // ── Glass outline + halo glow (pulses on bass + beat) ───────────────
  oc.save();
  buildGlass();
  oc.lineWidth    = 2;
  oc.strokeStyle  = `hsla(${hue}, 70%, 80%, 0.55)`;
  oc.shadowColor  = `hsla(${hue}, 100%, 60%, 0.90)`;
  oc.shadowBlur   = 14 + _lavaBass * 22 + (beat ? 16 : 0);
  oc.stroke();
  oc.restore();

  // ── Metal base (cone / trapezoid below the glass) ───────────────────
  const baseTopHalf = neckBotHalf * 1.05;
  const baseBotHalf = neckBotHalf * 1.90;
  oc.save();
  oc.beginPath();
  oc.moveTo(cx - baseTopHalf, glassBot);
  oc.lineTo(cx + baseTopHalf, glassBot);
  oc.lineTo(cx + baseBotHalf, glassBot + baseH);
  oc.lineTo(cx - baseBotHalf, glassBot + baseH);
  oc.closePath();
  const baseGrad = oc.createLinearGradient(cx - baseBotHalf, 0, cx + baseBotHalf, 0);
  baseGrad.addColorStop(0,    '#1a1620');
  baseGrad.addColorStop(0.30, '#5e5868');
  baseGrad.addColorStop(0.55, '#948e9c');
  baseGrad.addColorStop(0.75, '#46414e');
  baseGrad.addColorStop(1,    '#0e0c12');
  oc.fillStyle = baseGrad;
  oc.fill();
  oc.fillStyle = 'rgba(255,255,255,0.22)';
  oc.fillRect(cx - baseTopHalf, glassBot, baseTopHalf * 2, 1);
  oc.fillStyle = 'rgba(0,0,0,0.65)';
  oc.fillRect(cx - baseBotHalf, glassBot + baseH - 1, baseBotHalf * 2, 1);
  oc.restore();
}

function _drawLavaLamp(analyser) {
  const W = canvas.width;
  const H = canvas.height;
  const now = performance.now();
  const dt = _lavaLastTs ? Math.min(0.05, (now - _lavaLastTs) / 1000) : 0.016;
  _lavaLastTs = now;
  _lavaTime += dt;

  // ── Audio analysis ────────────────────────────────────────────────────
  const fBuf = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(fBuf);
  const bassEnd = Math.max(4, Math.floor(fBuf.length * 0.10));
  let bass = 0;
  for (let i = 0; i < bassEnd; i++) bass += fBuf[i];
  bass /= (bassEnd * 255);
  const beat = bass > _lavaBassEnv + 0.18 && bass > 0.32;
  _lavaBassEnv += (bass - _lavaBassEnv) * 0.05;
  _lavaBass    += (bass - _lavaBass) * 0.20;

  if (!_lavaLampsState) _lavaLampsState = _initLavaLamps();
  const lamps = _lavaLampsState;

  const baseS  = Math.min(W, H);
  const tableY = Math.floor(H * 0.72);

  // ── Background (cached wall + table) ────────────────────────────────
  _ensureLavaBg(W, H, tableY);
  ctx2d.drawImage(_lavaBgCanvas, 0, 0);

  // Lamp size — shared across all three, chosen so three fit with margin.
  const bodyWMax = Math.min(W / lamps.length * 0.62, baseS * 0.17);
  const bodyHMax = Math.min(H * 0.48, bodyWMax * 2.7);

  // Horizontal layout: evenly spaced across ~75 % of canvas width.
  const spreadFrac = 0.72;
  const stepX = (W * spreadFrac) / lamps.length;
  const startX = W * (0.5 - spreadFrac / 2) + stepX / 2;

  lamps.forEach((lamp, idx) => {
    // Advance this lamp's rotation (direction varies per lamp).
    // No rotation: lamps stand still on the table.  The audio-reactive
    // motion lives in the wax blobs and halo glow, not in a sweeping glint.

    // Advance wax-blob simulation in offscreen coords.
    for (const b of lamp.blobs) {
      b.y += b.vy * dt * (1 + _lavaBass * 0.6);
      b.swirlPhase += dt * b.swirlRate;
    }

    const bodyW = bodyWMax * lamp.sizeMul;
    const bodyH = bodyHMax * lamp.sizeMul;
    const capH  = Math.max(6, bodyH * 0.08);
    const baseH = Math.max(8, bodyH * 0.12);
    const lampPad = Math.ceil(Math.max(32, bodyW * 0.70));
    const ocW = Math.ceil(bodyW + lampPad * 2);
    const ocH = Math.ceil(capH + bodyH + baseH + lampPad * 2);

    // (Re)allocate this lamp's offscreen if size changed.
    if (!lamp.offscreen
        || lamp.offscreen.width !== ocW
        || lamp.offscreen.height !== ocH) {
      lamp.offscreen = document.createElement('canvas');
      lamp.offscreen.width  = ocW;
      lamp.offscreen.height = ocH;
      lamp.offscreenCtx = lamp.offscreen.getContext('2d');
    }
    const oc = lamp.offscreenCtx;
    oc.clearRect(0, 0, ocW, ocH);

    // Recycle blobs to stay within the glass body bounds in offscreen Y.
    const glassTop = lampPad + capH;
    const glassBot = glassTop + bodyH;
    for (const b of lamp.blobs) {
      if (b.y < glassTop - b.baseR * 1.6) b.y = glassBot + b.baseR * 1.6;
      if (b.y > glassBot + b.baseR * 1.6) b.y = glassTop - b.baseR * 1.6;
    }

    // Render lamp into its offscreen.
    _renderLavaBody(oc, bodyW, bodyH, capH, baseH, lampPad, beat, lamp);

    // On-canvas position — base bottom sits exactly on tableY.
    const lampCX   = Math.round(startX + stepX * idx);
    const lampDestX = Math.round(lampCX - ocW / 2);
    const lampDestY = tableY - (lampPad + capH + bodyH + baseH);

    // ── Wall halo behind this lamp in its colour (additive) ──────────
    const haloCY = tableY - (capH + bodyH + baseH) * 0.55;
    const haloR  = Math.max(bodyW * 1.9, baseS * 0.18);
    ctx2d.save();
    ctx2d.globalCompositeOperation = 'lighter';
    const haloG = ctx2d.createRadialGradient(lampCX, haloCY, 0, lampCX, haloCY, haloR);
    haloG.addColorStop(0,    `hsla(${lamp.hue}, 95%, 65%, ${0.22 + _lavaBass * 0.18})`);
    haloG.addColorStop(0.45, `hsla(${lamp.hue}, 90%, 50%, 0.10)`);
    haloG.addColorStop(1,    'rgba(0,0,0,0)');
    ctx2d.fillStyle = haloG;
    ctx2d.beginPath();
    ctx2d.arc(lampCX, haloCY, haloR, 0, Math.PI * 2);
    ctx2d.fill();
    ctx2d.restore();

    // ── Reflection on table (mirror + squish + alpha-fade + blur) ────
    const reflSquish = 0.42;
    const reflH = Math.ceil(ocH * reflSquish);
    const scratch = document.createElement('canvas');
    scratch.width  = ocW;
    scratch.height = reflH;
    const sc = scratch.getContext('2d');

    sc.save();
    sc.translate(0, reflH);
    sc.scale(1, -reflSquish);
    sc.drawImage(lamp.offscreen, 0, 0);
    sc.restore();

    sc.save();
    sc.globalCompositeOperation = 'destination-out';
    const fadeGrad = sc.createLinearGradient(0, 0, 0, reflH);
    fadeGrad.addColorStop(0,    'rgba(0,0,0,0.45)');
    fadeGrad.addColorStop(0.55, 'rgba(0,0,0,0.85)');
    fadeGrad.addColorStop(1,    'rgba(0,0,0,1.00)');
    sc.fillStyle = fadeGrad;
    sc.fillRect(0, 0, ocW, reflH);
    sc.restore();

    ctx2d.save();
    ctx2d.globalAlpha = 0.80;
    ctx2d.filter = `blur(${Math.max(2, Math.round(baseS * 0.010))}px)`;
    ctx2d.drawImage(scratch, lampDestX, tableY);
    ctx2d.restore();

    // ── Contact shadow at the base ──────────────────────────────────
    ctx2d.save();
    const shadowW = bodyW * 1.55;
    const shadowH = baseS * 0.010;
    const shGrad  = ctx2d.createRadialGradient(lampCX, tableY, 0,
                                               lampCX, tableY, shadowW * 0.5);
    shGrad.addColorStop(0,   'rgba(0,0,0,0.55)');
    shGrad.addColorStop(0.6, 'rgba(0,0,0,0.20)');
    shGrad.addColorStop(1,   'rgba(0,0,0,0.00)');
    ctx2d.fillStyle = shGrad;
    ctx2d.beginPath();
    ctx2d.ellipse(lampCX, tableY, shadowW * 0.5, shadowH * 1.5, 0, 0, Math.PI * 2);
    ctx2d.fill();
    ctx2d.restore();

    // ── The upright lamp ────────────────────────────────────────────
    ctx2d.drawImage(lamp.offscreen, lampDestX, lampDestY);
  });
}

// ── Raccoon — full-body raccoon running toward the camera, singing ─────────
//
// Layered front-on view: dark vignette → radial speed lines (motion cue) →
// tail (S-curve, swings counter to the body) → torso (oval, slight sway) →
// front paws (alternate forward in step cycle) → head (with mask, eyes,
// snout, mouth that opens to the music).  Mouth opens on mid + treble with
// an asymmetric envelope (snappy attack, slow release).  Body sways and tail
// counter-swings in counter-phase off a single sway phase; the step phase
// drives the running gait.  Approach pulse on bass adds a subtle "coming at
// you" feel.

function _initRaccoonStreaks(W, H) {
  const n = 90;
  const arr = [];
  const maxR = Math.hypot(W, H) * 0.6;
  for (let i = 0; i < n; i++) {
    arr.push({
      angle: Math.random() * Math.PI * 2,
      r: Math.random() * maxR,                       // initial radius from centre
      speed: 220 + Math.random() * 380,              // px/sec outward
      length: 18 + Math.random() * 50,               // streak length
      hue: Math.floor(Math.random() * 60) + 220,     // cool blue/purple range
    });
  }
  return arr;
}

function _drawRaccoonSpeedLines(W, H, dt, bassPulse) {
  if (!_raccoonStreaks) _raccoonStreaks = _initRaccoonStreaks(W, H);
  const cx = W / 2, cy = H / 2;
  const maxR = Math.hypot(W, H) * 0.60;
  ctx2d.save();
  ctx2d.lineCap = 'round';
  for (const s of _raccoonStreaks) {
    s.r += s.speed * dt * (1 + bassPulse * 1.4);
    if (s.r > maxR) {
      // Respawn near centre at a fresh angle.
      s.r = 30 + Math.random() * 60;
      s.angle = Math.random() * Math.PI * 2;
    }
    const x1 = cx + Math.cos(s.angle) * s.r;
    const y1 = cy + Math.sin(s.angle) * s.r;
    const r2 = Math.max(0, s.r - s.length);
    const x2 = cx + Math.cos(s.angle) * r2;
    const y2 = cy + Math.sin(s.angle) * r2;
    // Streak fades in as it leaves the centre, brightest mid-screen, fades at edges.
    const t = s.r / maxR;          // 0..1
    const alpha = Math.min(0.55, t * 0.9) * (1 - Math.max(0, t - 0.7) / 0.3);
    if (alpha <= 0.01) continue;
    ctx2d.strokeStyle = `hsla(${s.hue}, 70%, 70%, ${alpha})`;
    ctx2d.lineWidth = 1 + t * 1.5;
    ctx2d.beginPath();
    ctx2d.moveTo(x1, y1);
    ctx2d.lineTo(x2, y2);
    ctx2d.stroke();
  }
  ctx2d.restore();
}

// Tail — cubic-bezier S-curve coming out from the right side of the body,
// drawn as overlapping ringed segments (raccoon banding).  Swing is
// counter-phase to the body sway.
function _drawRaccoonTail(rootX, rootY, length, swing, scale) {
  const segs = 14;
  // Base direction points down-right; swing rotates the tip vertically.
  const tipX = rootX + length * 0.35 + swing * length * 0.45;
  const tipY = rootY + length * 0.85 + Math.cos(swing) * length * 0.10;
  const c1x  = rootX + length * 0.50;
  const c1y  = rootY + length * 0.20 + swing * length * 0.10;
  const c2x  = rootX + length * 0.55 + swing * length * 0.30;
  const c2y  = rootY + length * 0.70;

  // Sample the bezier
  const pts = [];
  for (let i = 0; i <= segs; i++) {
    const t = i / segs;
    const omt = 1 - t;
    const x = omt*omt*omt*rootX + 3*omt*omt*t*c1x + 3*omt*t*t*c2x + t*t*t*tipX;
    const y = omt*omt*omt*rootY + 3*omt*omt*t*c1y + 3*omt*t*t*c2y + t*t*t*tipY;
    pts.push({ x, y, t });
  }

  // Draw ringed segments tip-to-base (so base segments overlap tip segments)
  for (let i = pts.length - 1; i >= 1; i--) {
    const a = pts[i - 1], b = pts[i];
    const t = b.t;                              // 0..1 toward tip
    const r = (18 - t * 12) * scale;            // tapers from 18 → 6 px (scaled)
    const dark = i % 2 === 0;
    ctx2d.fillStyle = dark ? '#1c1822' : '#807a86';
    ctx2d.beginPath();
    ctx2d.ellipse(b.x, b.y, r, r * 0.85, 0, 0, Math.PI * 2);
    ctx2d.fill();
    // Connect a→b with a thick line for fur continuity.
    ctx2d.strokeStyle = dark ? '#1c1822' : '#807a86';
    ctx2d.lineWidth = r * 1.6;
    ctx2d.lineCap = 'round';
    ctx2d.beginPath();
    ctx2d.moveTo(a.x, a.y);
    ctx2d.lineTo(b.x, b.y);
    ctx2d.stroke();
  }
  // White tail tip
  const tip = pts[pts.length - 1];
  ctx2d.fillStyle = '#e6e3eb';
  ctx2d.beginPath();
  ctx2d.ellipse(tip.x, tip.y, 6 * scale, 5 * scale, 0, 0, Math.PI * 2);
  ctx2d.fill();
}

// Body — ellipsoid torso with a fur gradient and a lighter chest patch.
function _drawRaccoonBody(cx, cy, w, h, scale) {
  // Outer torso
  const grad = ctx2d.createRadialGradient(cx, cy - h * 0.20, w * 0.10,
                                          cx, cy + h * 0.30, w * 0.85);
  grad.addColorStop(0,    '#9c97a3');
  grad.addColorStop(0.55, '#5b5862');
  grad.addColorStop(1,    '#2a2630');
  ctx2d.fillStyle = grad;
  ctx2d.beginPath();
  ctx2d.ellipse(cx, cy, w / 2, h / 2, 0, 0, Math.PI * 2);
  ctx2d.fill();

  // Lighter chest fur patch (drop shape).
  const chestGrad = ctx2d.createRadialGradient(cx, cy - h * 0.10, w * 0.05,
                                               cx, cy + h * 0.05, w * 0.30);
  chestGrad.addColorStop(0,   'rgba(220,215,225,0.85)');
  chestGrad.addColorStop(0.7, 'rgba(180,175,185,0.50)');
  chestGrad.addColorStop(1,   'rgba(180,175,185,0.0)');
  ctx2d.fillStyle = chestGrad;
  ctx2d.beginPath();
  ctx2d.ellipse(cx, cy + h * 0.05, w * 0.32, h * 0.34, 0, 0, Math.PI * 2);
  ctx2d.fill();

  // Soft body shadow under (roots the raccoon to the ground)
  ctx2d.save();
  const sh = ctx2d.createRadialGradient(cx, cy + h * 0.55, 0,
                                        cx, cy + h * 0.55, w * 0.55);
  sh.addColorStop(0,   'rgba(0,0,0,0.45)');
  sh.addColorStop(0.6, 'rgba(0,0,0,0.18)');
  sh.addColorStop(1,   'rgba(0,0,0,0.0)');
  ctx2d.fillStyle = sh;
  ctx2d.beginPath();
  ctx2d.ellipse(cx, cy + h * 0.55, w * 0.55, h * 0.10, 0, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.restore();
}

// Hind feet — two paw blobs with claws that alternate forward/back as the
// step phase advances.  At phase 0, left foot forward (large); at π, right
// foot forward.  These sit at the bottom of the body doing the running cycle.
function _drawRaccoonPaws(cx, cy, bodyW, bodyH, stepPhase, scale) {
  const baseY = cy + bodyH * 0.35;
  const baseX = bodyW * 0.30;
  // Left paw — sin(phase) forward when sin > 0
  const leftFwd  =  Math.sin(stepPhase);          // -1..1
  const rightFwd = -leftFwd;                       // counter
  const drawPaw = (sx, sy, fwd) => {
    // fwd=1 → larger, lower (closer to camera); fwd=-1 → smaller, higher (back)
    const sizeMul = 1 + Math.max(0, fwd) * 0.35;
    const drop    = Math.max(0, fwd) * bodyH * 0.10;
    const x = sx;
    const y = sy + drop;
    const w = bodyW * 0.16 * sizeMul;
    const h = bodyH * 0.13 * sizeMul;
    // Paw pad (dark)
    ctx2d.fillStyle = '#3a3640';
    ctx2d.beginPath();
    ctx2d.ellipse(x, y, w, h, 0, 0, Math.PI * 2);
    ctx2d.fill();
    // Paw fur top
    const grad = ctx2d.createLinearGradient(0, y - h, 0, y + h);
    grad.addColorStop(0, '#9c97a3');
    grad.addColorStop(1, '#3a3640');
    ctx2d.fillStyle = grad;
    ctx2d.beginPath();
    ctx2d.ellipse(x, y - h * 0.15, w * 0.92, h * 0.78, 0, 0, Math.PI * 2);
    ctx2d.fill();
    // Three little claws
    ctx2d.fillStyle = '#e6e3eb';
    for (let c = -1; c <= 1; c++) {
      ctx2d.beginPath();
      ctx2d.ellipse(x + c * w * 0.32, y + h * 0.55, w * 0.10, h * 0.18, 0, 0, Math.PI * 2);
      ctx2d.fill();
    }
  };
  drawPaw(cx - baseX, baseY, leftFwd);
  drawPaw(cx + baseX, baseY, rightFwd);
}

// A side-view cartoon fish, head to the right, tail to the left.
// (len, thick) define the bounding ellipse; scale is picked up implicitly.
function _drawRaccoonFish(cx, cy, len, thick) {
  const headX = cx + len * 0.42;   // tip of snout
  const tailX = cx - len * 0.42;   // base of tail fin

  // Tail fin — V-shaped behind the body.
  ctx2d.fillStyle = '#5a6678';
  ctx2d.beginPath();
  ctx2d.moveTo(tailX + len * 0.02, cy);
  ctx2d.lineTo(tailX - len * 0.16, cy - thick * 1.05);
  ctx2d.lineTo(tailX - len * 0.08, cy);
  ctx2d.lineTo(tailX - len * 0.16, cy + thick * 1.05);
  ctx2d.closePath();
  ctx2d.fill();

  // Dorsal fin — triangle rising from the middle of the back.
  ctx2d.fillStyle = '#6e7a8c';
  ctx2d.beginPath();
  ctx2d.moveTo(cx - len * 0.10, cy - thick * 0.80);
  ctx2d.lineTo(cx + len * 0.02, cy - thick * 1.40);
  ctx2d.lineTo(cx + len * 0.10, cy - thick * 0.80);
  ctx2d.closePath();
  ctx2d.fill();

  // Body — silver-to-slate vertical gradient (back darker, belly lighter).
  const bodyGrad = ctx2d.createLinearGradient(0, cy - thick, 0, cy + thick);
  bodyGrad.addColorStop(0,    '#4a5666');
  bodyGrad.addColorStop(0.45, '#9ab0c4');
  bodyGrad.addColorStop(1,    '#d8e2ec');
  ctx2d.fillStyle = bodyGrad;
  ctx2d.beginPath();
  ctx2d.ellipse(cx, cy, len * 0.42, thick, 0, 0, Math.PI * 2);
  ctx2d.fill();

  // Lateral line + scale arcs — subtle shine suggesting scales.
  ctx2d.strokeStyle = 'rgba(30,40,55,0.35)';
  ctx2d.lineWidth = Math.max(1, thick * 0.06);
  ctx2d.beginPath();
  ctx2d.moveTo(cx - len * 0.32, cy - thick * 0.10);
  ctx2d.quadraticCurveTo(cx, cy + thick * 0.02, cx + len * 0.28, cy - thick * 0.05);
  ctx2d.stroke();

  ctx2d.strokeStyle = 'rgba(240,246,252,0.35)';
  ctx2d.lineWidth = 1;
  for (let s = -3; s <= 2; s++) {
    const sx = cx + s * len * 0.08;
    ctx2d.beginPath();
    ctx2d.moveTo(sx, cy - thick * 0.35);
    ctx2d.quadraticCurveTo(sx + len * 0.025, cy, sx, cy + thick * 0.35);
    ctx2d.stroke();
  }

  // Gill slit behind the head.
  ctx2d.strokeStyle = 'rgba(20,30,45,0.55)';
  ctx2d.lineWidth = Math.max(1, thick * 0.09);
  ctx2d.beginPath();
  ctx2d.moveTo(headX - len * 0.18, cy - thick * 0.45);
  ctx2d.quadraticCurveTo(headX - len * 0.22, cy, headX - len * 0.18, cy + thick * 0.45);
  ctx2d.stroke();

  // Eye — white sclera + dark pupil (looks outward).
  const eyeX = headX - len * 0.10;
  const eyeY = cy - thick * 0.22;
  const eyeR = Math.max(2, thick * 0.30);
  ctx2d.fillStyle = '#f0f4f8';
  ctx2d.beginPath();
  ctx2d.arc(eyeX, eyeY, eyeR, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.fillStyle = '#0a0810';
  ctx2d.beginPath();
  ctx2d.arc(eyeX, eyeY, eyeR * 0.55, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.fillStyle = 'rgba(255,255,255,0.85)';
  ctx2d.beginPath();
  ctx2d.arc(eyeX - eyeR * 0.20, eyeY - eyeR * 0.25, eyeR * 0.20, 0, Math.PI * 2);
  ctx2d.fill();

  // Mouth at the head tip — a small open gape.
  ctx2d.strokeStyle = 'rgba(20,10,20,0.65)';
  ctx2d.lineWidth = Math.max(1, thick * 0.12);
  ctx2d.lineCap = 'round';
  ctx2d.beginPath();
  ctx2d.moveTo(headX + len * 0.02, cy + thick * 0.05);
  ctx2d.quadraticCurveTo(headX - len * 0.02, cy + thick * 0.15,
                         headX - len * 0.08, cy + thick * 0.02);
  ctx2d.stroke();
}

// Front paws cradling a fish at chest level.  Both paws bob up and down
// in unison off the step phase (subtle — the fish-holding is a steady grip,
// not a gait like the hind feet).
function _drawRaccoonFrontPaws(cx, cy, bodyW, bodyH, stepPhase, scale) {
  const pw = bodyW * 0.14;
  const ph = bodyH * 0.13;
  // Gentle in-phase bob (not alternating — paws grip the fish together).
  const bob = Math.sin(stepPhase * 2) * bodyH * 0.015;
  const py  = cy - bodyH * 0.08 + bob;

  const drawPaw = (x, flip) => {
    const s = flip ? -1 : 1;
    // Forearm / wrist fur — shaded ellipse with tilt toward the fish.
    ctx2d.save();
    ctx2d.translate(x, py);
    ctx2d.rotate(s * 0.35);    // tilt inward so paws appear to grip
    const grad = ctx2d.createLinearGradient(0, -ph * 1.2, 0, ph * 1.2);
    grad.addColorStop(0, '#7a7582');
    grad.addColorStop(1, '#2c2832');
    ctx2d.fillStyle = grad;
    ctx2d.beginPath();
    ctx2d.ellipse(0, 0, pw * 0.95, ph * 1.25, 0, 0, Math.PI * 2);
    ctx2d.fill();
    // Paw pad — dark lower ellipse.
    ctx2d.fillStyle = '#3a3640';
    ctx2d.beginPath();
    ctx2d.ellipse(0, ph * 0.3, pw * 0.80, ph * 0.70, 0, 0, Math.PI * 2);
    ctx2d.fill();
    // Three claws curved toward the fish (inner side).
    ctx2d.fillStyle = '#e6e3eb';
    for (let c = -1; c <= 1; c++) {
      ctx2d.beginPath();
      ctx2d.ellipse(pw * 0.35, ph * 0.15 + c * pw * 0.28,
                    pw * 0.14, ph * 0.18,
                    0, 0, Math.PI * 2);
      ctx2d.fill();
    }
    ctx2d.restore();
  };

  // Grip a bit wider than the fish so claws visibly wrap the fish ends.
  drawPaw(cx - bodyW * 0.32, false);
  drawPaw(cx + bodyW * 0.32, true);
}

function _drawRaccoonHead(cx, cy, headW, headH, mouthOpen, blinkS, bass) {
  const hHW = headW / 2;
  const hHH = headH / 2;

  ctx2d.save();
  ctx2d.translate(cx, cy);

  // ── Ears ─────────────────────────────────────────────────────────────
  ctx2d.fillStyle = '#5a5663';
  ctx2d.beginPath();
  ctx2d.moveTo(-hHW * 0.85, -hHH * 0.50);
  ctx2d.lineTo(-hHW * 1.05, -hHH * 1.20);
  ctx2d.lineTo(-hHW * 0.45, -hHH * 0.85);
  ctx2d.closePath();
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.moveTo( hHW * 0.85, -hHH * 0.50);
  ctx2d.lineTo( hHW * 1.05, -hHH * 1.20);
  ctx2d.lineTo( hHW * 0.45, -hHH * 0.85);
  ctx2d.closePath();
  ctx2d.fill();
  ctx2d.fillStyle = '#3a2030';
  ctx2d.beginPath();
  ctx2d.moveTo(-hHW * 0.78, -hHH * 0.65);
  ctx2d.lineTo(-hHW * 0.95, -hHH * 1.05);
  ctx2d.lineTo(-hHW * 0.55, -hHH * 0.85);
  ctx2d.closePath();
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.moveTo( hHW * 0.78, -hHH * 0.65);
  ctx2d.lineTo( hHW * 0.95, -hHH * 1.05);
  ctx2d.lineTo( hHW * 0.55, -hHH * 0.85);
  ctx2d.closePath();
  ctx2d.fill();

  // ── Head (rounded ellipse, soft radial gradient) ─────────────────────
  const headGrad = ctx2d.createRadialGradient(0, -hHH * 0.30, hHW * 0.10, 0, 0, hHW);
  headGrad.addColorStop(0, '#b1adb8');
  headGrad.addColorStop(1, '#56535e');
  ctx2d.fillStyle = headGrad;
  ctx2d.beginPath();
  ctx2d.ellipse(0, 0, hHW, hHH, 0, 0, Math.PI * 2);
  ctx2d.fill();

  // ── Black mask band ──────────────────────────────────────────────────
  const maskCY = -hHH * 0.10;
  ctx2d.fillStyle = '#1a1620';
  ctx2d.beginPath();
  ctx2d.ellipse(-hHW * 0.35, maskCY, hHW * 0.32, hHH * 0.27, 0, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.ellipse( hHW * 0.35, maskCY, hHW * 0.32, hHH * 0.27, 0, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.ellipse(0, maskCY + hHH * 0.04, hHW * 0.20, hHH * 0.17, 0, 0, Math.PI * 2);
  ctx2d.fill();

  // ── Eyes ─────────────────────────────────────────────────────────────
  const eyeR    = hHW * 0.13;
  const eyeY    = maskCY;
  const eyeOff  = hHW * 0.35;

  ctx2d.fillStyle = '#f0eef2';
  ctx2d.beginPath();
  ctx2d.ellipse(-eyeOff, eyeY, eyeR, eyeR * 0.85 * blinkS, 0, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.ellipse( eyeOff, eyeY, eyeR, eyeR * 0.85 * blinkS, 0, 0, Math.PI * 2);
  ctx2d.fill();

  const pupilR = eyeR * (0.40 + bass * 0.30);
  ctx2d.fillStyle = '#0a0610';
  ctx2d.beginPath();
  ctx2d.ellipse(-eyeOff, eyeY, pupilR, pupilR * blinkS, 0, 0, Math.PI * 2);
  ctx2d.fill();
  ctx2d.beginPath();
  ctx2d.ellipse( eyeOff, eyeY, pupilR, pupilR * blinkS, 0, 0, Math.PI * 2);
  ctx2d.fill();
  if (blinkS > 0.5) {
    ctx2d.fillStyle = 'rgba(255,255,255,0.95)';
    ctx2d.beginPath();
    ctx2d.arc(-eyeOff - eyeR * 0.25, eyeY - eyeR * 0.25, eyeR * 0.18, 0, Math.PI * 2);
    ctx2d.fill();
    ctx2d.beginPath();
    ctx2d.arc( eyeOff - eyeR * 0.25, eyeY - eyeR * 0.25, eyeR * 0.18, 0, Math.PI * 2);
    ctx2d.fill();
  }

  // ── Snout, nose, mouth ──────────────────────────────────────────────
  const snoutCY = hHH * 0.18;
  const snoutW  = hHW * 0.55;
  const snoutH  = hHH * 0.42;
  ctx2d.fillStyle = '#cdc8d2';
  ctx2d.beginPath();
  ctx2d.ellipse(0, snoutCY, snoutW, snoutH, 0, 0, Math.PI * 2);
  ctx2d.fill();

  const noseTopY = snoutCY - snoutH * 0.55;
  const noseR    = hHW * 0.075;
  ctx2d.fillStyle = '#0c0810';
  ctx2d.beginPath();
  ctx2d.moveTo(-noseR, noseTopY);
  ctx2d.lineTo( noseR, noseTopY);
  ctx2d.lineTo(0, noseTopY + noseR * 1.25);
  ctx2d.closePath();
  ctx2d.fill();

  const mouthY    = snoutCY + snoutH * 0.30;
  const maxMouth  = hHH * 0.32;
  const mouthH    = Math.max(2, mouthOpen * maxMouth);
  const mouthW    = hHW * 0.30 * (1 + mouthOpen * 0.25);

  if (mouthOpen < 0.07) {
    ctx2d.strokeStyle = '#0c0810';
    ctx2d.lineWidth   = 2;
    ctx2d.lineCap     = 'round';
    ctx2d.beginPath();
    ctx2d.moveTo(-mouthW * 0.85, mouthY);
    ctx2d.quadraticCurveTo(0, mouthY + 3, mouthW * 0.85, mouthY);
    ctx2d.stroke();
  } else {
    ctx2d.fillStyle = '#180a14';
    ctx2d.beginPath();
    ctx2d.ellipse(0, mouthY, mouthW, mouthH, 0, 0, Math.PI * 2);
    ctx2d.fill();

    if (mouthOpen > 0.18) {
      const tongueScale = Math.min(1, (mouthOpen - 0.18) / 0.5);
      ctx2d.fillStyle = '#c8285a';
      ctx2d.beginPath();
      ctx2d.ellipse(0, mouthY + mouthH * 0.40,
                    mouthW * 0.70, mouthH * 0.55 * tongueScale,
                    0, 0, Math.PI * 2);
      ctx2d.fill();
    }
    if (mouthOpen > 0.30) {
      const teethScale = Math.min(1, (mouthOpen - 0.30) / 0.4);
      ctx2d.fillStyle = '#f4f0f6';
      const tW = mouthW * 0.16 * teethScale;
      const tH = mouthH * 0.45 * teethScale;
      const tTopY = mouthY - mouthH * 0.85;
      ctx2d.beginPath();
      ctx2d.moveTo(-mouthW * 0.20 - tW / 2, tTopY);
      ctx2d.lineTo(-mouthW * 0.20 + tW / 2, tTopY);
      ctx2d.lineTo(-mouthW * 0.20,          tTopY + tH);
      ctx2d.closePath();
      ctx2d.fill();
      ctx2d.beginPath();
      ctx2d.moveTo( mouthW * 0.20 - tW / 2, tTopY);
      ctx2d.lineTo( mouthW * 0.20 + tW / 2, tTopY);
      ctx2d.lineTo( mouthW * 0.20,          tTopY + tH);
      ctx2d.closePath();
      ctx2d.fill();
    }
  }

  // Whiskers
  ctx2d.strokeStyle = 'rgba(220,215,225,0.4)';
  ctx2d.lineWidth   = 1;
  for (let s = -1; s <= 1; s += 2) {
    for (let w = 0; w < 3; w++) {
      const sx = s * snoutW * 0.55;
      const sy = snoutCY + (w - 1) * 7;
      const tipDy = (w - 1) * 6;
      ctx2d.beginPath();
      ctx2d.moveTo(sx, sy);
      ctx2d.lineTo(sx + s * snoutW * 0.55, sy + tipDy);
      ctx2d.stroke();
    }
  }

  ctx2d.restore();
}

function _drawRaccoon(analyser) {
  const W = canvas.width;
  const H = canvas.height;
  const now = performance.now();
  const dt = _raccoonLastTs ? Math.min(0.05, (now - _raccoonLastTs) / 1000) : 0.016;
  _raccoonLastTs = now;
  _raccoonTime += dt;

  // ── Audio analysis ────────────────────────────────────────────────────
  const fBuf = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(fBuf);

  const startMouth = Math.max(2, Math.floor(fBuf.length * 0.04));
  const endMouth   = Math.floor(fBuf.length * 0.55);
  let total = 0;
  for (let i = startMouth; i < endMouth; i++) total += fBuf[i];
  const amp = total / ((endMouth - startMouth) * 255);

  const bassEnd = Math.max(4, Math.floor(fBuf.length * 0.10));
  let bass = 0;
  for (let i = 0; i < bassEnd; i++) bass += fBuf[i];
  bass /= (bassEnd * 255);

  // Asymmetric envelope on amp.
  if (amp > _raccoonAmp) _raccoonAmp += (amp - _raccoonAmp) * 0.55;
  else                   _raccoonAmp += (amp - _raccoonAmp) * 0.10;
  _raccoonBass += (bass - _raccoonBass) * 0.20;
  _raccoonBob   = _raccoonBob * 0.85 + _raccoonBass * 0.15;

  // Step + sway phases — bass adds tempo so the gait kicks during drops.
  const stepFreq = 2.0 + _raccoonBass * 1.6;        // Hz
  const swayFreq = 1.0 + _raccoonBass * 0.5;
  _raccoonStepPhase = (_raccoonStepPhase + dt * stepFreq * Math.PI * 2) % (Math.PI * 2);
  _raccoonSwayPhase = (_raccoonSwayPhase + dt * swayFreq * Math.PI * 2) % (Math.PI * 2);

  // Blink scheduler
  _raccoonNextBlink -= dt;
  if (_raccoonNextBlink <= 0) {
    _raccoonBlink = 1;
    _raccoonNextBlink = 2.5 + Math.random() * 4;
  }
  _raccoonBlink = Math.max(0, _raccoonBlink - dt * 6);

  // ── Background — soft dark vignette ───────────────────────────────────
  const bg = ctx2d.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, Math.max(W, H) * 0.7);
  bg.addColorStop(0, '#100a18');
  bg.addColorStop(1, '#020005');
  ctx2d.fillStyle = bg;
  ctx2d.fillRect(0, 0, W, H);

  // ── Speed lines — sense of motion toward camera ─────────────────────
  _drawRaccoonSpeedLines(W, H, dt, _raccoonBass);

  // ── Whole-figure transform: approach pulse + step bob + sway ────────
  const baseS = Math.min(W, H);
  const approach = 1 + _raccoonBass * 0.06;          // subtle "coming at you"
  // Use abs(sin) so the bob lands at every footfall (twice per phase cycle).
  const stepBob  = Math.abs(Math.sin(_raccoonStepPhase)) * baseS * 0.012;
  const sway     = Math.sin(_raccoonSwayPhase);      // -1..1

  const cx = W / 2;
  const cy = H / 2 + _raccoonBob * baseS * 0.025 + stepBob;

  ctx2d.save();
  ctx2d.translate(cx, cy);
  ctx2d.scale(approach, approach);
  // Body sway angle — left/right tilt of the whole figure.
  const figureTilt = sway * 0.06;
  ctx2d.rotate(figureTilt);

  const headW = baseS * 0.42;
  const headH = headW * 0.92;
  const bodyW = headW * 1.15;                        // a bit wider than head
  const bodyH = headH * 0.95;
  const headCY = -headH * 0.05;                      // slightly above origin
  const bodyCY =  headCY + headH * 0.55 + bodyH * 0.40;

  // ── Tail — drawn first (behind everything else). Counter-phase to body. ─
  // Tail root is on the right side of the body, lower-mid.
  const tailRootX = bodyW * 0.45;
  const tailRootY = bodyCY + bodyH * 0.05;
  const tailLen   = bodyW * 0.95;
  const tailSwing = -sway * 0.7;                     // counter to figure tilt
  _drawRaccoonTail(tailRootX, tailRootY, tailLen, tailSwing, baseS / 480);

  // ── Body torso ──────────────────────────────────────────────────────
  _drawRaccoonBody(0, bodyCY, bodyW, bodyH, baseS / 480);

  // ── Hind feet (alternating with step phase — the running gait) ──────
  _drawRaccoonPaws(0, bodyCY, bodyW, bodyH, _raccoonStepPhase, baseS / 480);

  // ── Fish held across the chest, tilted head-up so the head is visible ─
  // Length is longer than the gap between paws so head and tail poke out.
  // Constant tilt (-0.32 rad ≈ 18°, head up-right) plus a small idle
  // wobble and step-phase bob.
  const fishLen   = bodyW * 0.85;
  const fishThick = bodyH * 0.18;
  const fishBob    = Math.sin(_raccoonStepPhase * 2) * bodyH * 0.015;
  const fishWobble = Math.sin(_raccoonTime * 3.1) * 0.04;
  const fishTilt   = -0.32;
  ctx2d.save();
  ctx2d.translate(0, bodyCY - bodyH * 0.08 + fishBob);
  ctx2d.rotate(fishTilt + fishWobble);
  _drawRaccoonFish(0, 0, fishLen, fishThick);
  ctx2d.restore();

  // ── Front paws gripping the fish ────────────────────────────────────
  _drawRaccoonFrontPaws(0, bodyCY, bodyW, bodyH, _raccoonStepPhase, baseS / 480);

  // ── Head — drawn last so it occludes upper body / paw if raised ─────
  // Head sway is a small additional tilt on top of the figure tilt.
  ctx2d.save();
  ctx2d.translate(0, headCY);
  ctx2d.rotate(Math.sin(_raccoonTime * 1.4) * _raccoonBass * 0.05);
  const blinkS = 1 - _raccoonBlink * 0.95;
  const mouthOpen = Math.max(0.04, _raccoonAmp);
  _drawRaccoonHead(0, 0, headW, headH, mouthOpen, blinkS, _raccoonBass);
  ctx2d.restore();

  ctx2d.restore();   // pop figure transform
}

function clear() {
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  _spectroColumn = 0;
}

// ── Visibility tracking ────────────────────────────────────────────────────
// We suspend the rAF loop whenever the page is hidden (tab backgrounded) or
// the canvas scrolls off-screen.  Browsers already throttle rAF on hidden
// tabs, but the analyser + canvas state churn still allocates — pausing
// outright is cheaper and also keeps CPU flat when the user is looking at
// the admin panel or track info tab.
let _pageVisible   = !document.hidden;
let _canvasVisible = true;        // assume visible until the observer says otherwise
let _wantRunning   = false;       // what Player told us — the "intent" layer

function _shouldRun() {
  return _wantRunning && _pageVisible && _canvasVisible;
}

function _resume() {
  if (_shouldRun() && !rafId) draw();
}

function _suspend() {
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

document.addEventListener('visibilitychange', () => {
  _pageVisible = !document.hidden;
  if (_pageVisible) _resume(); else _suspend();
});
// iOS Safari sometimes fires pagehide/pageshow for app-switch / BFCache
// paths that don't reliably fire visibilitychange. Belt-and-suspenders.
window.addEventListener('pagehide',  () => { _pageVisible = false; _suspend(); });
window.addEventListener('pageshow',  () => { _pageVisible = !document.hidden; if (_pageVisible) _resume(); });

// Cheap & reliable — an IntersectionObserver fires once when the canvas
// enters / exits the viewport.  Cheaper than polling getBoundingClientRect.
try {
  const io = new IntersectionObserver(entries => {
    for (const e of entries) _canvasVisible = e.isIntersecting;
    if (_canvasVisible) _resume(); else _suspend();
  }, { threshold: 0.01 });
  io.observe(canvas);
} catch { /* older engines — fall back to page-visibility only */ }

function start() {
  canvas.style.opacity = '0.9';
  _wantRunning = true;
  _resume();
}

function stop() {
  canvas.style.opacity = '0';
  _wantRunning = false;
  _suspend();
  // Fade out then clear
  setTimeout(clear, 650);
}

// Auto-start/stop with playback
Player.on('statechange', ({ playing }) => playing ? start() : stop());
Player.on('trackchange', () => { resize(); _spectroColumn = 0; start(); });

export const Visualizer = { start, stop, toggleMode, setMode, get mode() { return _mode; } };
