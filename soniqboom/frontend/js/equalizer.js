// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * equalizer.js  —  10-band EQ with fully custom pointer-event sliders.
 *
 * No native <input type="range"> — those are unreliable vertically across
 * browsers. Instead each band is a <div class="eq-track"> with a draggable
 * <div class="eq-thumb"> driven by pointer-capture events.
 *
 * Bands: 32, 64, 125, 250, 500, 1k, 2k, 4k, 8k, 16k Hz
 * Gain range: −12 … +12 dB   step: 0.5 dB
 * Persisted in localStorage as JSON array under 'sb_eq'.
 */
import { Player } from './player.js';

// ── Presets ───────────────────────────────────────────────────────────────────
//                  32   64  125  250  500   1k   2k   4k   8k  16k
const PRESETS = {
  flat:      [  0,  0,  0,  0,  0,  0,  0,  0,  0,  0 ],
  bass:      [  8,  6,  4,  2,  0, -1, -1, -1, -1, -1 ],
  rock:      [  5,  4,  3,  1, -1,  0,  1,  3,  4,  4 ],
  pop:       [ -1,  0,  2,  4,  5,  4,  2,  1,  0, -1 ],
  jazz:      [  3,  2,  1,  2,  3,  3,  2,  1,  1,  2 ],
  classical: [  4,  3,  2,  1, -1,  0,  0,  1,  3,  4 ],
  vocal:     [ -3, -2,  0,  3,  5,  5,  4,  2,  0, -1 ],
};

const TRACK_H  = 200;   // px — visual height of each slider track
const GAIN_MAX =  12;
const GAIN_MIN = -12;
const GAIN_RNG = GAIN_MAX - GAIN_MIN;   // 24

// ── DOM refs ──────────────────────────────────────────────────────────────────
const overlay   = document.getElementById('eq-overlay');
const btnToggle = document.getElementById('btn-eq');
const presetSel = document.getElementById('eq-preset');
const btnReset  = document.getElementById('eq-reset');
const btnClose  = document.getElementById('eq-close');

const bands = Array.from(document.querySelectorAll('.eq-band'));

// Per-band refs
const dbLabels = bands.map(b => b.querySelector('.eq-db-val'));
const tracks   = bands.map(b => b.querySelector('.eq-track'));
const thumbs   = bands.map(b => b.querySelector('.eq-thumb'));
const fills    = bands.map(b => b.querySelector('.eq-fill'));

// Current gain values (mirror of filter nodes)
const gains = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];

// ── Geometry helpers ──────────────────────────────────────────────────────────
/** Convert gain (dB) → pixel offset from track top */
function gainToY(gain) {
  // +12 dB → 0px (top),  -12 dB → TRACK_H px (bottom)
  return ((GAIN_MAX - gain) / GAIN_RNG) * TRACK_H;
}

/** Convert pixel offset from track top → gain (dB), snapped to 0.5 dB */
function yToGain(y) {
  const raw  = GAIN_MAX - (Math.max(0, Math.min(TRACK_H, y)) / TRACK_H) * GAIN_RNG;
  return Math.round(raw * 2) / 2;   // snap to nearest 0.5
}

// ── Visual update for one band ────────────────────────────────────────────────
function renderBand(i, gain) {
  const y     = gainToY(gain);
  const midY  = gainToY(0);          // pixel position of 0 dB line

  // Position thumb
  thumbs[i].style.top = y + 'px';

  // Fill: accent from 0-dB line to thumb
  const fillTop  = Math.min(y, midY);
  const fillH    = Math.abs(y - midY);
  fills[i].style.top    = fillTop + 'px';
  fills[i].style.height = fillH + 'px';
  fills[i].style.background = gain >= 0 ? 'var(--accent)' : '#5599ee';

  // Label
  const v = gain.toFixed(1).replace('.0', '');
  dbLabels[i].textContent = gain > 0 ? '+' + v : v;
  dbLabels[i].style.color =
    gain > 0 ? 'var(--accent)' :
    gain < 0 ? '#5599ee'       :
               'var(--text2)';
}

// ── Apply gain to audio filter + visuals ──────────────────────────────────────
function setGain(i, gain) {
  const clamped = Math.max(GAIN_MIN, Math.min(GAIN_MAX, gain));
  gains[i] = clamped;
  const filters = Player.eqFilters;
  if (filters[i]) filters[i].gain.value = clamped;
  renderBand(i, clamped);
}

function saveGains() {
  localStorage.setItem('sb_eq', JSON.stringify(gains));
}

// ── Pointer-event drag handling ───────────────────────────────────────────────
tracks.forEach((track, i) => {
  let dragging = false;

  function applyY(clientY) {
    const rect = track.getBoundingClientRect();
    const y    = clientY - rect.top;
    setGain(i, yToGain(y));
    presetSel.value = '';
    saveGains();
  }

  track.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    dragging = true;
    track.setPointerCapture(e.pointerId);
    applyY(e.clientY);
  });

  track.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    applyY(e.clientY);
  });

  track.addEventListener('pointerup', () => { dragging = false; });
  track.addEventListener('pointercancel', () => { dragging = false; });

  // Scroll wheel: ±0.5 dB per tick
  track.addEventListener('wheel', (e) => {
    e.preventDefault();
    const delta = e.deltaY < 0 ? 0.5 : -0.5;
    setGain(i, gains[i] + delta);
    presetSel.value = '';
    saveGains();
  }, { passive: false });
});

// ── Apply an entire gains array ───────────────────────────────────────────────
function applyAll(newGains, { save = true } = {}) {
  newGains.forEach((g, i) => setGain(i, g));
  if (save) saveGains();
}

// ── Preset selector ───────────────────────────────────────────────────────────
presetSel.addEventListener('change', () => {
  const p = PRESETS[presetSel.value];
  if (p) { applyAll(p); presetSel.value = presetSel.value; }
});

// ── Reset ─────────────────────────────────────────────────────────────────────
btnReset.addEventListener('click', () => {
  applyAll(PRESETS.flat);
  presetSel.value = 'flat';
});

// ── Overlay open/close ────────────────────────────────────────────────────────
function open() {
  overlay.classList.remove('hidden');
  btnToggle.classList.add('on');
}
function close() {
  overlay.classList.add('hidden');
  btnToggle.classList.remove('on');
}
function toggle() {
  overlay.classList.contains('hidden') ? open() : close();
}

btnClose.addEventListener('click', close);
overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
});

// ── Restore from localStorage on load ────────────────────────────────────────
try {
  const saved = JSON.parse(localStorage.getItem('sb_eq') || 'null');
  if (Array.isArray(saved) && saved.length === 10) {
    applyAll(saved, { save: false });
    // Sync preset selector
    for (const [name, vals] of Object.entries(PRESETS)) {
      if (vals.every((v, i) => v === saved[i])) { presetSel.value = name; break; }
    }
  } else {
    applyAll(PRESETS.flat, { save: false });
    presetSel.value = 'flat';
  }
} catch (_) {
  applyAll(PRESETS.flat, { save: false });
}

export const Equalizer = { toggle, open, close };
