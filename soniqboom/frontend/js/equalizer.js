// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

// version bump needed in index.html for equalizer.js (pre-gain headroom)
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

// Promote each .eq-track to a real ARIA slider so screen readers + keyboard
// users can drive it.  The previous implementation was pointer-only on a
// plain <div>.  We keep the existing pointer handlers below — Tab + arrow
// keys now do everything they do, just with announce / focus semantics.
const FREQ_LABELS = ['32 Hz','64 Hz','125 Hz','250 Hz','500 Hz','1 kHz','2 kHz','4 kHz','8 kHz','16 kHz'];
tracks.forEach((track, i) => {
  track.setAttribute('role', 'slider');
  track.setAttribute('tabindex', '0');
  track.setAttribute('aria-valuemin', String(GAIN_MIN));
  track.setAttribute('aria-valuemax', String(GAIN_MAX));
  track.setAttribute('aria-valuenow', '0');
  track.setAttribute('aria-orientation', 'vertical');
  track.setAttribute('aria-label', `${FREQ_LABELS[i] || ''} gain in decibels`);
});

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

// ── EQ headroom (pre-gain) — clipping prevention ─────────────────────────────
// Boosting any band can push the signal past 0 dBFS and clip the output.
// To keep the chain headroom-safe without changing perceived loudness,
// we attenuate the *input* to the EQ chain by the actual peak boost
// (NOT by GAIN_MAX) — so a single +3 dB band only costs 3 dB of input
// headroom, not the full 12 dB the slider could in theory produce.
//
// When all bands are <= 0 dB, the pre-gain stays at unity (1.0) so cuts
// don't lose any signal — only boosts trigger the attenuation.
function _updateEqPreGain() {
  const pre = Player.eqPreGain;
  if (!pre) return;        // Web Audio not initialised yet (no first play)
  const peakBoost = Math.max(0, ...gains);
  if (peakBoost <= 0) {
    pre.gain.value = 1.0;
  } else {
    // 1 / 10^(peakBoost/20) — exactly cancels the maximum band's boost
    // at the unity-pre-EQ point, leaving headroom for the boost itself.
    pre.gain.value = 1.0 / Math.pow(10, peakBoost / 20);
  }
}

// ── Apply gain to audio filter + visuals ──────────────────────────────────────
function setGain(i, gain) {
  const clamped = Math.max(GAIN_MIN, Math.min(GAIN_MAX, gain));
  gains[i] = clamped;
  const filters = Player.eqFilters;
  if (filters[i]) filters[i].gain.value = clamped;
  renderBand(i, clamped);
  // Keep ARIA value in sync so screen readers announce the new dB level
  // whenever pointer / keyboard / preset changes the band.
  if (tracks[i]) {
    tracks[i].setAttribute('aria-valuenow', String(clamped));
    tracks[i].setAttribute('aria-valuetext', `${clamped > 0 ? '+' : ''}${clamped} dB`);
  }
  _updateEqPreGain();
  _refreshEqBadge();
}

// Debounce the localStorage write — a wheel drag fires `saveGains` per
// 0.5 dB tick, which was sync JSON.stringify + storage write per pointer
// event (Perf #2).  Coalesce to 150 ms so a scrub flushes once at the
// end of the gesture.
let _saveGainsTimer = null;
function saveGains() {
  if (_saveGainsTimer) clearTimeout(_saveGainsTimer);
  _saveGainsTimer = setTimeout(() => {
    _saveGainsTimer = null;
    try {
      localStorage.setItem('sb_eq', JSON.stringify(gains));
    } catch { /* quota or private-mode — fine to drop */ }
  }, 150);
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

  // Keyboard equivalents — required for the role="slider" promise above.
  track.addEventListener('keydown', (e) => {
    const STEP = 0.5;
    const BIG = 3;
    let next = gains[i];
    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowRight':  next = gains[i] + STEP; break;
      case 'ArrowDown':
      case 'ArrowLeft':   next = gains[i] - STEP; break;
      case 'PageUp':      next = gains[i] + BIG;  break;
      case 'PageDown':    next = gains[i] - BIG;  break;
      case 'Home':        next = GAIN_MAX;        break;
      case 'End':         next = GAIN_MIN;        break;
      case 'Enter':
      case ' ':           next = 0;               break;  // quick reset to flat
      default: return;
    }
    e.preventDefault();
    setGain(i, next);
    presetSel.value = '';
    saveGains();
  });

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

// "EQ active" indicator on the toolbar button: ``on`` while the overlay
// is open *or* whenever any band is non-flat.  Previously the button only
// glowed while the overlay was open, so non-default gains went unnoticed
// after closing it (UX/UI #1 #18).
function _eqIsActive() {
  return gains.some(g => Math.abs(g) > 0.05);
}
function _refreshEqBadge() {
  const open = !overlay.classList.contains('hidden');
  btnToggle.classList.toggle('on', open || _eqIsActive());
  btnToggle.title = _eqIsActive() && !open
    ? 'Equalizer (active — non-flat preset)'
    : 'Equalizer';
}

// ── Overlay open/close ────────────────────────────────────────────────────────
function open() {
  overlay.classList.remove('hidden');
  _refreshEqBadge();
}
function close() {
  overlay.classList.add('hidden');
  _refreshEqBadge();
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
