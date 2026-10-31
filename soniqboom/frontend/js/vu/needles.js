// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * vu/needles.js — the twin analogue VU meters' model and drawing, shared by
 * the desktop Retro player bar (player/vu_needles.js: placement, its own loop)
 * and the phone's Now Playing (mobile/views/np_visuals.js: one loop for all of
 * its visuals).  No DOM beyond the canvases it is handed.
 *
 * Levels: Player.vuTaps() — one analyser per channel (time-domain samples) —
 * divided by the volume setting.  The needle position is the RMS level on a
 * VU scale (0 VU = REF_DBFS), linear in voltage as a real meter's scale; meter
 * ballistics: a fast attack (ATTACK_S), a slower release (RELEASE_S).  The
 * PEAK lamp lights when a sample reaches PEAK_DBFS and holds PEAK_HOLD_MS.
 *
 *   vuToPos(vu)                          → needle position 0…1.02
 *   needleAngle(pos)                     → radians from upright
 *   readNeedleLevels(taps, st, gain, now)→ st.target / st.lampUntil from the taps
 *   stepNeedles(st, dt)                  → advance st.pos; true while anything moves
 *   drawNeedleFace(w, h, dpr, ch)        → the static face (a canvas to cache)
 *   drawNeedle(c, face, w, h, dpr, pos, lit, ox, oy) → one meter onto context c
 * ``st`` = createNeedleState(): {pos, target, lampUntil, buf}.
 */

// Levels are before the volume setting.  The needles show for ordinary audio
// (MP3, FLAC, …), measured in 21 ms windows: most tracks sit round -15 dBFS RMS
// and reach -11 in their loud passages, loud masters -8.5 and -5.5.  0 VU =
// -8 dBFS RMS: an everyday track swings between -7 and -3 VU, a loud master up
// to +2 without resting on the stop.  The PEAK lamp: a sample within 0.5 dB of
// full scale — clipped masters light it often, everyday tracks rarely.
export const REF_DBFS = -8;         // 0 VU
export const PEAK_DBFS = -0.5;      // the PEAK lamp
export const PEAK_HOLD_MS = 500;
export const ATTACK_S = 0.08;
export const RELEASE_S = 0.45;
export const READ_MS = 50;          // analyser reads ≤ 20 per second (Firefox: keep the main thread light)
export const ASPECT = 0.5;          // face height / width
const ANG = 48 * Math.PI / 180;     // needle swing each side of upright
const SCALE = [                     // VU marks: [vu, label]
  [-20, '20'], [-10, '10'], [-7, '7'], [-5, '5'], [-3, '3'], [-2, ''], [-1, ''], [0, '0'], [1, ''], [2, ''], [3, '3'],
];
const INK = '#2a2116', RED = '#c3120c';
const FACE_FONT = '"DIN Alternate","DIN Condensed",Bahnschrift,"Arial Narrow",sans-serif';

/** Needle position 0…1 (rest … +3 VU) for a VU reading; a real meter's scale is
 *  linear in voltage, so -20 VU sits near the left end and 0 VU at ~71 %. */
export function vuToPos(vu) {
  return Math.max(0, Math.min(1.02, Math.pow(10, vu / 20) / Math.pow(10, 3 / 20)));
}

export function needleAngle(p) { return -ANG + p * 2 * ANG; }

export function createNeedleState() {
  return { pos: [0, 0], target: [0, 0], lampUntil: [0, 0], buf: null };
}

/** One reading of the two channel analysers into ``st.target`` (and the PEAK
 *  lamps), the volume setting taken out by ``gain`` (1 / volume; 0 = muted). */
export function readNeedleLevels(taps, st, gain, now) {
  for (let i = 0; i < 2; i++) {
    const an = taps.ch[i];
    if (!st.buf || st.buf.length !== an.fftSize) st.buf = new Float32Array(an.fftSize);
    const buf = st.buf;
    an.getFloatTimeDomainData(buf);
    let sum = 0, peak = 0;
    for (let k = 0; k < buf.length; k++) {
      const v = buf[k];
      sum += v * v;
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
    }
    const rms = Math.sqrt(sum / buf.length) * gain;
    peak *= gain;
    st.target[i] = rms > 1e-6 ? vuToPos(20 * Math.log10(rms) - REF_DBFS) : 0;
    if (peak > 0 && 20 * Math.log10(peak) >= PEAK_DBFS) st.lampUntil[i] = now + PEAK_HOLD_MS;
  }
}

/** Meter ballistics over ``dt`` seconds; true while a needle moves or rests
 *  off the stop pin. */
export function stepNeedles(st, dt) {
  let moving = false;
  for (let i = 0; i < 2; i++) {
    const tau = st.target[i] > st.pos[i] ? ATTACK_S : RELEASE_S;
    st.pos[i] += (st.target[i] - st.pos[i]) * (1 - Math.exp(-dt / tau));
    if (Math.abs(st.target[i] - st.pos[i]) > 0.001 || st.pos[i] > 0.001) moving = true;
  }
  return moving;
}

function _geom(w, h) {
  const px = w / 2, py = h * 1.30;              // pivot below the window
  const r = h * 0.90;                           // the scale arc
  return { px, py, r };
}

/** The static face of meter ``ch`` (0 = L, 1 = R) at w × h CSS px. */
export function drawNeedleFace(w, h, dpr, ch) {
  const off = document.createElement('canvas');
  off.width = Math.round(w * dpr); off.height = Math.round(h * dpr);
  const g = off.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  // Backlit cream dial (as the Stations signal meter).
  const bg = g.createRadialGradient(w / 2, h * 1.1, h * 0.1, w / 2, h * 1.1, w * 0.8);
  bg.addColorStop(0, '#fff2cf'); bg.addColorStop(0.55, '#f2d9a2'); bg.addColorStop(1, '#c79a52');
  g.fillStyle = bg; g.fillRect(0, 0, w, h);
  const { px, py, r } = _geom(w, h);
  const a0 = -Math.PI / 2 - ANG, a1 = -Math.PI / 2 + ANG;
  const aOf = (vu) => -Math.PI / 2 + needleAngle(vuToPos(vu));
  // Scale arc (black) and the red zone 0…+3.
  g.lineWidth = 1; g.strokeStyle = INK;
  g.beginPath(); g.arc(px, py, r, a0 + 0.06, aOf(0)); g.stroke();
  g.lineWidth = Math.max(2.5, h * 0.07); g.strokeStyle = RED;
  g.beginPath(); g.arc(px, py, r + g.lineWidth / 2 - 0.5, aOf(0), a1); g.stroke();
  // Ticks and figures.  (The figures, legends and lamp keep the player bar's
  // sizes up to a 112 × 56 face and grow more slowly on a bigger one — the
  // phone's display, an iPad's.)
  const fs = Math.max(6.5, Math.round(Math.min(h * 0.17, 6.5 + h * 0.11) * 2) / 2);
  const ex = Math.max(3, w * 0.026), lx0 = Math.max(5, w * 0.044), base = Math.max(7, h * 0.125);
  g.font = `700 ${fs}px ${FACE_FONT}`;
  g.textAlign = 'center'; g.textBaseline = 'middle';
  for (const [vu, label] of SCALE) {
    const a = aOf(vu), major = label !== '';
    const r0 = r, r1 = r + (major ? h * 0.13 : h * 0.08);
    g.strokeStyle = vu > 0 ? RED : INK;
    g.lineWidth = major ? 1.3 : 1;
    g.beginPath();
    g.moveTo(px + r0 * Math.cos(a), py + r0 * Math.sin(a));
    g.lineTo(px + r1 * Math.cos(a), py + r1 * Math.sin(a));
    g.stroke();
    if (major) {
      const rt = r + h * 0.27;
      g.fillStyle = vu > 0 ? RED : INK;
      g.fillText(label, px + rt * Math.cos(a), py + rt * Math.sin(a));
    }
  }
  // − / + at the ends, VU, the channel and the PEAK legend.
  const sm = Math.max(6, fs - 1);
  g.font = `700 ${sm}px ${FACE_FONT}`;
  g.fillStyle = INK; g.textAlign = 'left';
  g.fillText('−', ex, h * 0.2);
  g.fillStyle = RED; g.textAlign = 'right';
  g.fillText('+', w - ex, h * 0.2);
  g.fillStyle = INK; g.textAlign = 'center';
  g.font = `700 ${Math.max(7, fs + 1)}px ${FACE_FONT}`;
  g.fillText('VU', w / 2, h * 0.70);
  g.font = `700 ${sm}px ${FACE_FONT}`;
  g.textAlign = 'left';
  g.fillText(ch === 0 ? 'L' : 'R', lx0, h - base);
  if (w >= 96) {                       // a small face has no room for the legend; the lamp stays
    g.textAlign = 'right';
    g.fillText('PEAK', w - Math.max(13, w * 0.116 + Math.max(0, h - 56) * 0.06), h - base);
  }
  return off;
}

/** Meter (face + needle + PEAK lamp) onto context ``c`` at (ox, oy) CSS px —
 *  0, 0 for a canvas of its own.  ``lit``: the PEAK lamp. */
export function drawNeedle(c, face, w, h, dpr, pos, lit, ox = 0, oy = 0) {
  c.setTransform(1, 0, 0, 1, 0, 0);
  c.drawImage(face, Math.round(ox * dpr), Math.round(oy * dpr));
  c.setTransform(dpr, 0, 0, dpr, ox * dpr, oy * dpr);
  const { px, py, r } = _geom(w, h);
  const a = -Math.PI / 2 + needleAngle(pos);
  const tip = r + h * 0.16;
  const x1 = px + tip * Math.cos(a), y1 = py + tip * Math.sin(a);
  // The needle may not leave its window (the pivot sits below it).
  c.save();
  c.beginPath(); c.rect(0, 0, w, h); c.clip();
  // Shadow, then the needle.
  c.lineCap = 'round';
  c.strokeStyle = 'rgba(40,24,8,0.22)'; c.lineWidth = 1.6;
  c.beginPath(); c.moveTo(px + 1.5, py + 1.5); c.lineTo(x1 + 1.5, y1 + 1.5); c.stroke();
  c.strokeStyle = '#1c0d08'; c.lineWidth = 1.4;
  c.beginPath(); c.moveTo(px, py); c.lineTo(x1, y1); c.stroke();
  c.strokeStyle = RED; c.lineWidth = 1.4;
  const tr = r - h * 0.05;
  c.beginPath(); c.moveTo(px + tr * Math.cos(a), py + tr * Math.sin(a)); c.lineTo(x1, y1); c.stroke();
  // Pivot housing along the bottom edge.
  const hb = c.createLinearGradient(0, h - 4, 0, h);
  hb.addColorStop(0, '#3a332a'); hb.addColorStop(1, '#14110d');
  c.fillStyle = hb; c.fillRect(w * 0.36, h - 4, w * 0.28, 4);
  // PEAK lamp.
  const lx = w - Math.max(8, w * 0.071), ly = h - Math.max(7, h * 0.125), lr = Math.max(2.4, h * 0.06);
  c.beginPath(); c.arc(lx, ly, lr, 0, Math.PI * 2);
  if (lit) {
    c.fillStyle = '#ff3b1f'; c.shadowColor = 'rgba(255,59,31,0.9)'; c.shadowBlur = 6;
    c.fill(); c.shadowBlur = 0;
  } else {
    c.fillStyle = '#5a2a1e'; c.fill();
  }
  c.restore();
}
