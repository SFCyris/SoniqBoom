// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * eq/retro.js — the "Retro hi-fi" look of the equalizer panel: a 1980s
 * graphic equaliser.  Ten vertical slots on a metal faceplate with printed dB
 * marks; the slider caps are the knobs (DOM/CSS, #eq-panel[data-eq-style=
 * "retro"] in app.css) and set the gain.  Beside each slot an LED column
 * (green → amber → red, a peak-hold segment that falls slowly) shows the
 * playing music's post-EQ level in that band (before the headroom pre-gain,
 * so a boosted band's LEDs rise).  Above the slots, unless the
 * Display switch is off (panel data-eq-vfd="off"), a cyan vacuum-fluorescent
 * display strip: the response curve over a segmented spectrum, EQ / BYPASS
 * annunciators.  Opening the panel (or choosing the look) runs a short
 * power-on: the display flickers on and the LEDs do a lamp test — not with
 * reduced motion or with visualizations off.
 *
 * The slots sit on an octave grid (band i ↔ 31.25·2^i Hz, evenly spaced), and
 * the display uses the same frequency axis, so the curve runs right above the
 * caps.  Colours come from CSS custom properties (`vars`).
 */

import { registerEqStyle, cssAlpha } from './engine.js';

const OCT0 = 31.25;                 // band i's slot ↔ 31.25 · 2^i Hz (32 … 16 kHz)
const DB_MARKS = [12, 6, 0, -6, -12];
const DB_MINOR = [9, 3, -3, -9];
const SLOT_DB = 12.9;               // a slot runs ±SLOT_DB (the caps travel ±12)
// Band level (the engine's 0…1 post-EQ spectrum level, 1 = 88 dB) → the LED
// column's bottom and top: ≈ 3.4 dB a segment with the display, 2.6 without.
const LED_LO = 0.22, LED_HI = 0.80;
const RED_FROM = 0.86, AMBER_FROM = 0.62;   // column fractions where the colours change
const PEAK_HOLD_MS = 700;
const PEAK_FALL = 0.006;            // segments per ms once the hold is over
const POWER_MS = 720;               // power-on: display flicker + LED lamp test
const VFD_DB = 15;                  // the display's dB range (±)
const VFD_LO = 0.12, VFD_HI = 0.9;  // spectrum level → the display's empty / full column

// Low-resolution layer for the glow (LEDs, phosphor): drawn small, scaled up
// smoothed — a cheap bloom.
let _bloom = null, _bloomG = null;
const BLOOM_SCALE = 0.25;

// Per-band meter state (one panel per page).
let _peaks = new Float32Array(10), _holdUntil = new Float64Array(10);
let _lastT = 0, _lastSince = -1;

// ── geometry ─────────────────────────────────────────────────────────────────
let _vfdWasOn = false, _vfdSince = 0;
function layout({ w, h, narrow, panel }) {
  const vfdOn = !panel || panel.dataset.eqVfd !== 'off';
  if (vfdOn && !_vfdWasOn) _vfdSince = performance.now();
  _vfdWasOn = vfdOn;
  const l = narrow ? 24 : 36, r = narrow ? 6 : 36;
  const pw = Math.max(10, w - l - r);
  const cw = pw / 10;
  const vfdH = vfdOn ? (narrow ? 62 : 88) : 0;
  const top = vfdOn ? vfdH + (narrow ? 18 : 22) : 14;
  const plot = { l, t: top, w: pw, h: Math.max(20, h - top - 12) };
  const x = (f) => l + cw * (Math.log2(f / OCT0) + 0.5);
  const bandX = (i) => l + cw * (i + 0.5);
  const vfd = !vfdOn ? null
    : narrow ? { l: 2, t: 2, w: w - 4, h: vfdH - 2, since: _vfdSince }
    : { l: l - 12, t: 2, w: pw + 24, h: vfdH - 2, since: _vfdSince };
  return { plot, dbSpan: 13, x, bandX, cw, narrow, vfd };
}

function _leds(s) {
  const L = s.layout;
  const capHalf = L.narrow ? 11 : 15;
  // narrow: a slimmer column hugging its own slot, clear of the next cap
  const lw = L.narrow ? 3 : Math.max(5, Math.min(8, Math.round(L.cw * 0.1)));
  const lx = capHalf + (L.narrow ? 1 : 5);
  const pad = L.narrow ? 1 : 2;      // the dark window around the lenses
  const y0 = s.y(12), y1 = s.y(-12);
  const len = y1 - y0;
  const n = Math.max(8, Math.min(20, Math.round(len / 11)));
  const pitch = len / n;
  return { lw, lx, pad, y0, y1, n, pitch, segH: Math.max(2, pitch * 0.68) };
}
const _zone = (k, n) => { const f = (k + 1) / n; return f > RED_FROM ? 2 : f > AMBER_FROM ? 1 : 0; };

function _vfdInner(v) { return { l: v.l + 7, t: v.t + 7, w: v.w - 14, h: v.h - 14 }; }
function _vfdCols(s) { return s.layout.narrow ? 2 : 3; }       // spectrum columns per band
function _vfdRows(inner) { return Math.max(6, Math.floor((inner.h - 2) / 3)); }

function _rrect(g, x, y, w, h, r) {
  g.beginPath();
  if (g.roundRect) g.roundRect(x, y, w, h, r); else g.rect(x, y, w, h);
}

// ── the static layer ─────────────────────────────────────────────────────────
function drawGrid(g, s, T) {
  const L = s.layout;
  if (!L) return;
  const { plot } = s;
  const face = T.face || 'sans-serif';
  const print = T.print || '#c9ccd2', dim = T.printDim || 'rgba(200,204,210,0.5)';

  // display window: bezel, tinted glass, the unlit phosphor
  if (L.vfd) {
    const v = L.vfd, inner = _vfdInner(v);
    _rrect(g, v.l, v.t, v.w, v.h, 7);
    g.fillStyle = T.bezel || '#0b0c0e';
    g.fill();
    g.strokeStyle = 'rgba(255,255,255,0.10)';
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(v.l + 7, v.t + v.h - 0.5); g.lineTo(v.l + v.w - 7, v.t + v.h - 0.5); g.stroke();
    const gl = g.createLinearGradient(0, inner.t, 0, inner.t + inner.h);
    gl.addColorStop(0, T.vfdGlass || '#06191b');
    gl.addColorStop(1, T.vfdGlass2 || '#020a0b');
    _rrect(g, inner.l, inner.t, inner.w, inner.h, 3);
    g.fillStyle = gl;
    g.fill();
    const vc = T.vfd || '#5ff6e6';
    g.save();
    g.beginPath(); g.rect(inner.l, inner.t, inner.w, inner.h); g.clip();
    // unlit segments of the spectrum columns
    const C = _vfdCols(s), rows = _vfdRows(inner), cwc = L.cw / C;
    g.fillStyle = cssAlpha(vc, 0.055);
    g.beginPath();
    for (let k = 0; k < 10 * C; k++) {
      const x0 = plot.l + k * cwc + 1;
      for (let r = 0; r < rows; r++) g.rect(x0, inner.t + inner.h - 1 - (r + 1) * 3 + 1, cwc - 2, 2);
    }
    g.fill();
    // 0 dB line, dotted
    const y0 = inner.t + inner.h / 2;
    g.fillStyle = cssAlpha(vc, 0.22);
    for (let x = inner.l + 2; x < inner.l + inner.w; x += 4) g.fillRect(x, Math.round(y0), 1.5, 1);
    // unlit annunciators
    g.font = `700 9px ${face}`;
    g.textBaseline = 'top';
    g.textAlign = 'right';
    g.fillStyle = cssAlpha(vc, 0.10);
    _annunciators(g, inner, true);
    g.restore();
  }

  // printed dB scale (both sides on the desktop) and the 0 dB line
  g.fillStyle = print;
  g.font = `600 ${L.narrow ? 9 : 10}px ${face}`;
  g.textBaseline = 'middle';
  for (const db of DB_MARKS) {
    const y = s.y(db);
    const txt = db > 0 ? `+${db}` : db < 0 ? `−${-db}` : '0';
    g.textAlign = 'right';
    g.fillText(txt, plot.l - 6, y);
    if (!L.narrow) { g.textAlign = 'left'; g.fillText(txt, plot.l + plot.w + 6, y); }
  }
  g.fillStyle = dim;
  const yz = Math.round(s.y(0)) + 0.5;
  for (let x = plot.l; x < plot.l + plot.w; x += 3) g.fillRect(x, yz - 0.5, 1, 1);
  if (!L.narrow) {
    g.fillStyle = T.print2 || print;
    g.font = `600 9px ${face}`;
    g.textAlign = 'right';
    g.fillText('dB', plot.l - 6, s.y(SLOT_DB) - 5);
  }

  // slots, tick marks beside them, the LED columns' unlit lenses
  const led = _leds(s);
  const sw = L.narrow ? 4 : 6;
  const ys = s.y(SLOT_DB), ye = s.y(-SLOT_DB);
  const offA = Number(T.ledOff) || 0.16;
  for (let i = 0; i < 10; i++) {
    const bx = s.bands[i].x;
    // slot: a dark recess with a lit lower lip
    _rrect(g, bx - sw / 2, ys, sw, ye - ys, sw / 2);
    g.fillStyle = T.slot || '#050506';
    g.fill();
    g.strokeStyle = T.slotEdge || 'rgba(255,255,255,0.12)';
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(bx + sw / 2 + 0.5, ys + 2); g.lineTo(bx + sw / 2 + 0.5, ye - 1); g.stroke();
    // ticks: long at the printed marks, short between
    if (!L.narrow) {
      g.fillStyle = print;
      for (const db of DB_MARKS) g.fillRect(bx - sw / 2 - 9, Math.round(s.y(db)), db === 0 ? 7 : 5, 1);
      g.fillStyle = dim;
      for (const db of DB_MINOR) g.fillRect(bx - sw / 2 - 7, Math.round(s.y(db)), 3, 1);
    }
    // LED column: a dark window, then each segment's lens, unlit
    const lx = bx + led.lx;
    _rrect(g, lx - led.pad, led.y0 - 2, led.lw + 2 * led.pad, led.y1 - led.y0 + 4, 2);
    g.fillStyle = T.ledWin || 'rgba(0,0,0,0.55)';
    g.fill();
    const cols = [T.ledG || '#38e06a', T.ledA || '#ffb42e', T.ledR || '#ff4036'];
    for (let z = 0; z < 3; z++) {
      g.fillStyle = cssAlpha(cols[z], offA);
      g.beginPath();
      for (let k = 0; k < led.n; k++) {
        if (_zone(k, led.n) !== z) continue;
        g.rect(lx, led.y1 - (k + 1) * led.pitch + (led.pitch - led.segH) / 2, led.lw, led.segH);
      }
      g.fill();
    }
  }
}

function _annunciators(g, inner, unlitBoth, bypassOn) {
  const x = inner.l + inner.w - 6, y = inner.t + 4;
  // "BYPASS" right, "EQ" left of it
  const wB = g.measureText('BYPASS').width;
  if (unlitBoth || bypassOn) g.fillText('BYPASS', x, y);
  if (unlitBoth || !bypassOn) g.fillText('EQ', x - wB - 8, y);
}

// ── the moving layer ─────────────────────────────────────────────────────────
// Power-on brightness of the display at t ms: off, a few flickers, warm-up.
function _vfdPower(t) {
  if (t < 0) return 0;
  if (t < 60) return 0;
  if (t < 110) return 0.85;
  if (t < 170) return 0.12;
  if (t < 230) return 1;
  if (t < 280) return 0.35;
  if (t < POWER_MS) return 0.6 + 0.4 * ((t - 280) / (POWER_MS - 280));
  return 1;
}
// LED lamp test at t ms: a sweep up and back down, band by band.
function _lampTest(t, i) {
  const u = t - i * 22;
  if (u < 0 || u > 480) return 0;
  return u < 240 ? u / 240 : 1 - (u - 240) / 240;
}

// A post-EQ level as if there were no headroom pre-gain (a boost shows as a
// rise); silence stays silence.
function _lift(s, v) {
  return v > 0.01 ? v + (s.headroomDb || 0) / (s.specRangeDb || 88) : v;
}
function _specAt(s, f) {
  const n = s.specFreqs.length;
  const p = (Math.log(f / s.specFreqs[0]) / Math.log(s.specFreqs[n - 1] / s.specFreqs[0])) * (n - 1);
  if (p <= 0) return s.post[0];
  if (p >= n - 1) return s.post[n - 1];
  const k = Math.floor(p), fr = p - k;
  return s.post[k] * (1 - fr) + s.post[k + 1] * fr;
}
function _curveAt(s, db, f) {
  const n = s.curveFreqs.length;
  const p = (Math.log(f / s.curveFreqs[0]) / Math.log(s.curveFreqs[n - 1] / s.curveFreqs[0])) * (n - 1);
  const k = Math.max(0, Math.min(n - 2, Math.floor(p))), fr = Math.max(0, Math.min(1, p - k));
  return db[k] * (1 - fr) + db[k + 1] * fr;
}

function _bloomLayer(s) {
  const sw = Math.max(1, Math.round(s.w * BLOOM_SCALE)), sh = Math.max(1, Math.round(s.h * BLOOM_SCALE));
  if (!_bloom) { _bloom = document.createElement('canvas'); _bloomG = _bloom.getContext('2d'); }
  if (_bloom.width !== sw || _bloom.height !== sh) { _bloom.width = sw; _bloom.height = sh; }
  _bloomG.setTransform(1, 0, 0, 1, 0, 0);
  _bloomG.clearRect(0, 0, sw, sh);
  _bloomG.setTransform(BLOOM_SCALE, 0, 0, BLOOM_SCALE, 0, 0);
  return _bloomG;
}

function drawFrame(g, s, T) {
  const L = s.layout;
  if (!L) return false;
  const t = s.time;
  const dt = _lastT ? Math.max(0, Math.min(100, t - _lastT)) : 16.7;
  _lastT = t;
  if (s.since !== _lastSince) { _lastSince = s.since; _peaks.fill(0); _holdUntil.fill(0); }
  const fx = s.motion && s.viz !== false;                   // power-on animation allowed
  const power = fx ? t - s.since : POWER_MS;                // ms since power-on
  const powering = power < POWER_MS;
  const bg = _bloomLayer(s);
  let anim = powering;

  // 1. the display: segmented spectrum, response curve, annunciators
  if (L.vfd) {
    const inner = _vfdInner(L.vfd);
    const vfdT = fx ? Math.min(power, t - L.vfd.since) : POWER_MS;
    const pb = _vfdPower(vfdT);
    if (vfdT < POWER_MS) anim = true;
    if (pb > 0) _drawVfd(g, bg, s, T, inner, pb);
  }

  // 2. the LED columns
  const led = _leds(s);
  const cols = [T.ledG || '#38e06a', T.ledA || '#ffb42e', T.ledR || '#ff4036'];
  const paths = [[], [], []];
  for (let i = 0; i < 10; i++) {
    const lv = s.post ? _lift(s, s.bands[i].level) : 0;
    let frac = (lv - LED_LO) / (LED_HI - LED_LO);
    frac = frac < 0 ? 0 : frac > 1 ? 1 : frac;
    const lit = Math.round(frac * led.n);
    // peak hold (of the music, not the lamp test): the highest segment
    // lately, held, then falling slowly
    if (!s.motion || !s.post) {
      _peaks[i] = s.post ? lit : 0;
    } else if (lit >= _peaks[i]) {
      _peaks[i] = lit; _holdUntil[i] = t + PEAK_HOLD_MS;
    } else if (t > _holdUntil[i]) {
      _peaks[i] = Math.max(lit, _peaks[i] - dt * PEAK_FALL);
    }
    // (until exactly 0: a peak just above 0 still lights segment 0)
    if (s.motion && s.post && _peaks[i] > 0) anim = true;
    const shown = powering ? Math.max(lit, Math.round(_lampTest(power, i) * led.n)) : lit;
    for (let k = 0; k < shown; k++) paths[_zone(k, led.n)].push(i, k);
    const pk = Math.ceil(_peaks[i]) - 1;
    if (pk >= shown && pk >= 0) paths[_zone(pk, led.n)].push(i, pk);
  }
  for (let z = 0; z < 3; z++) {
    const p = paths[z];
    if (!p.length) continue;
    g.beginPath(); bg.beginPath();
    for (let j = 0; j < p.length; j += 2) {
      const x = s.bands[p[j]].x + led.lx, y = led.y1 - (p[j + 1] + 1) * led.pitch + (led.pitch - led.segH) / 2;
      g.rect(x, y, led.lw, led.segH);
      bg.rect(x - 2, y - 1, led.lw + 4, led.segH + 2);
    }
    g.fillStyle = cols[z]; g.fill();
    bg.fillStyle = cols[z]; bg.fill();
  }

  // 3. the glow over everything lit
  g.save();
  g.globalCompositeOperation = 'lighter';
  g.globalAlpha = Number(T.bloom) || 0.6;
  g.imageSmoothingEnabled = true;
  g.imageSmoothingQuality = 'high';
  g.drawImage(_bloom, 0, 0, _bloom.width, _bloom.height, 0, 0, s.w, s.h);
  g.restore();
  return s.motion && anim;
}

function _drawVfd(g, bg, s, T, inner, pb) {
  const L = s.layout;
  const vc = T.vfd || '#5ff6e6', vcDim = T.vfdDim || '#2fc4b8';
  const vy = (db) => inner.t + 3 + ((VFD_DB - db) / (2 * VFD_DB)) * (inner.h - 6);
  g.save();
  g.beginPath(); g.rect(inner.l, inner.t, inner.w, inner.h); g.clip();
  bg.save();
  bg.beginPath(); bg.rect(inner.l, inner.t, inner.w, inner.h); bg.clip();

  // spectrum columns (1/3 octave on the desktop)
  if (s.post) {
    const C = _vfdCols(s), rows = _vfdRows(inner), cwc = L.cw / C;
    g.beginPath(); bg.beginPath();
    for (let k = 0; k < 10 * C; k++) {
      const fc = OCT0 * Math.pow(2, (k + 0.5) / C - 0.5);
      let v = (_lift(s, _specAt(s, fc)) - VFD_LO) / (VFD_HI - VFD_LO);
      v = v < 0 ? 0 : v > 1 ? 1 : v;
      const n = Math.round(v * rows);
      if (!n) continue;
      const x0 = s.layout.plot.l + k * cwc + 1;
      const top = inner.t + inner.h - 1 - n * 3 + 1;
      for (let r = 0; r < n; r++) g.rect(x0, inner.t + inner.h - 1 - (r + 1) * 3 + 1, cwc - 2, 2);
      bg.rect(x0, top, cwc - 2, n * 3);
    }
    g.fillStyle = cssAlpha(vcDim, 0.55 * pb); g.fill();
    bg.fillStyle = cssAlpha(vcDim, 0.3 * pb); bg.fill();
  }

  // bypassed: the curve being bypassed, dashed
  if (s.ghostDb) {
    g.beginPath();
    _vfdCurve(g, s, s.ghostDb, vy, inner);
    g.setLineDash([3, 4]);
    g.lineWidth = 1.25;
    g.strokeStyle = cssAlpha(vc, 0.45 * s.bypassMix * pb);
    g.stroke();
    g.setLineDash([]);
  }
  // the response curve: phosphor glow + a crisp line + a dot per band
  g.beginPath();
  _vfdCurve(g, s, s.curveDb, vy, inner);
  g.lineJoin = 'round'; g.lineCap = 'round';
  g.strokeStyle = cssAlpha(vc, 0.22 * pb);
  g.lineWidth = 5;
  g.stroke();
  g.strokeStyle = cssAlpha(vc, pb);
  g.lineWidth = 1.75;
  g.stroke();
  bg.beginPath();
  _vfdCurve(bg, s, s.curveDb, vy, inner);
  bg.strokeStyle = cssAlpha(vc, 0.9 * pb);
  bg.lineWidth = 6;
  bg.stroke();
  g.fillStyle = cssAlpha(vc, pb);
  for (let i = 0; i < 10; i++) {
    const b = s.bands[i];
    const y = vy(_curveAt(s, s.curveDb, OCT0 * Math.pow(2, i)));
    g.fillRect(Math.round(b.x) - 1.5, Math.round(y) - 1.5, 3, 3);
  }

  // annunciators
  g.font = `700 9px ${T.face || 'sans-serif'}`;
  g.textBaseline = 'top';
  g.textAlign = 'right';
  g.fillStyle = cssAlpha(vc, pb);
  _annunciators(g, inner, false, s.bypassMix > 0.5);
  bg.restore();
  g.restore();
}

function _vfdCurve(g, s, db, vy, inner) {
  const n = s.curveFreqs.length;
  const top = inner.t + 1, bot = inner.t + inner.h - 1;
  for (let i = 0; i < n; i++) {
    const x = s.x(s.curveFreqs[i]);
    let y = vy(db[i]);
    y = y < top ? top : y > bot ? bot : y;
    if (i) g.lineTo(x, y); else g.moveTo(x, y);
  }
}

const retro = {
  name: 'retro',
  vars: {
    face: '--eq-face', print: '--eq-print', print2: '--eq-print-2', printDim: '--eq-print-dim',
    slot: '--eq-slot', slotEdge: '--eq-slot-edge',
    ledG: '--eq-led-green', ledA: '--eq-led-amber', ledR: '--eq-led-red', ledOff: '--eq-led-off',
    ledWin: '--eq-led-window', bloom: '--eq-bloom',
    vfd: '--eq-vfd', vfdDim: '--eq-vfd-dim', vfdGlass: '--eq-vfd-glass', vfdGlass2: '--eq-vfd-glass-2', bezel: '--eq-vfd-bezel',
  },
  layout,
  drawGrid,
  drawFrame,
};

registerEqStyle('retro', retro);
export default retro;
