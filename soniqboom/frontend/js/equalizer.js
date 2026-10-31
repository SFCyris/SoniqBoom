// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * equalizer.js  —  the 10-band EQ panel: state, audio, controls.
 *
 * Bands: 32, 64, 125, 250, 500, 1k, 2k, 4k, 8k, 16k Hz (Player.eqBandDefs)
 * Gain range: −12 … +12 dB   step: 0.5 dB
 *
 * The panel draws a response curve over the live spectrum (js/eq/engine.js,
 * loaded on first open, + the look's renderer js/eq/<style>.js).  Each band is
 * a knob on the graph: a role="slider" element (Tab, arrows, PageUp/Down,
 * Home/End, Enter/Space = 0 dB), dragged vertically, double-click = 0 dB,
 * wheel over a knob = ±0.5 dB.  Bypass (button / B) sets every band flat until
 * pressed again or the panel closes — the pre-gain stays, so the compare is at
 * the same level.  Presets and Reset morph the gains over MORPH_MS (instant
 * with reduced motion).  Headroom: the pre-gain is lowered by the peak of the
 * combined curve (eq/response.js).  The CLIP lamp lights when the output peaks
 * past 0 dBFS; Auto pre-gain then lowers the pre-gain by that overshoot (the
 * "trim", kept until Reset).
 *
 * Audio parameters move with setTargetAtTime (RAMP_TC) while the context runs
 * — no zipper noise — and are set directly while it is suspended.
 *
 * Persisted in localStorage 'sb_eq' as {v:2, gains:[10], style, trim, vfd}
 * (a bare gains array — the older format — is read too; player.js reads the
 * gains from either shape when it builds the audio graph).  ``style`` there is
 * only a copy of the shared UI style (it seeds 'sb_ui_style' on first run).
 *
 * Looks ("styles"): the panel carries data-eq-style; STYLE_MODULES maps a
 * style name to its renderer module (it registers itself with the engine),
 * loaded when the look is first shown.  The look is the app-wide UI style
 * (js/ui_style.js — shared with Stations and Playlists, kept in
 * 'sb_ui_style'; Settings and Preferences set it too): the Style switch
 * (#eq-style, a radiogroup in the header)
 * calls setUiStyle, and the panel follows every change (onUiStyleChange),
 * whichever panel made it.  Modern: knobs on the graph.
 * Retro hi-fi: slider caps in slots with LED columns, preset keys with lamps
 * (#eq-preset-row, the same presets as the menu) and a Display switch
 * (#eq-vfd → data-eq-vfd on the panel: the VFD strip on or off).
 */
import { Player } from './player.js';
import { trapFocus } from './utils.js';
import { createResponse } from './eq/response.js';
import { getUiStyle, setUiStyle, onUiStyleChange } from './ui_style.js';

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
const PRESET_NAMES = {
  flat: 'Flat', bass: 'Bass Boost', rock: 'Rock', pop: 'Pop', jazz: 'Jazz', classical: 'Classical', vocal: 'Vocal',
};

const GAIN_MAX =  12;
const GAIN_MIN = -12;
const TRIM_MIN = -12;          // Auto pre-gain lowers the pre-gain at most this far
const MORPH_MS = 250;
const RAMP_TC  = 0.012;        // s — setTargetAtTime time constant (≈ 50 ms to settle)
const CLIP_HOLD_MS = 1500;
const OVER_KEEP_MS = 10000;    // Auto pre-gain stays offered this long after the last clip
const STORE_KEY = 'sb_eq';

// The looks.  Each module registers its renderer with js/eq/engine.js.
const STYLE_MODULES = {
  modern: () => import('./eq/modern.js'),
  retro:  () => import('./eq/retro.js'),
};
const DEFAULT_STYLE = 'retro';

const FREQ_LABELS = ['32 Hz','64 Hz','125 Hz','250 Hz','500 Hz','1 kHz','2 kHz','4 kHz','8 kHz','16 kHz'];

// ── DOM refs ──────────────────────────────────────────────────────────────────
const overlay    = document.getElementById('eq-overlay');
const panel      = document.getElementById('eq-panel');
const btnToggle  = document.getElementById('btn-eq');
const presetBtn  = document.getElementById('eq-preset');
const presetList = document.getElementById('eq-preset-list');
const btnBypass  = document.getElementById('eq-bypass');
const btnReset   = document.getElementById('eq-reset');
const btnClose   = document.getElementById('eq-close');
const rgSel      = document.getElementById('eq-rg');   // ReplayGain mode
const stage      = document.getElementById('eq-stage');
const canvas     = document.getElementById('eq-canvas');
const readout    = document.getElementById('eq-readout');
const npEl       = document.getElementById('eq-np');
const clipEl     = document.getElementById('eq-clip');
const pregainEl  = document.getElementById('eq-pregain');
const btnAuto    = document.getElementById('eq-autogain');
const liveEl     = document.getElementById('eq-live');      // polite announcements (sr-only)
const styleGroup = document.getElementById('eq-style');   // [eq-style-toggle]
const presetRow  = document.getElementById('eq-preset-row'); // Retro: preset keys (a radiogroup)
const presetCustom = document.getElementById('eq-preset-custom');
const _presetKeys = presetRow ? Array.from(presetRow.querySelectorAll('button[data-preset]')) : [];
const btnVfd     = document.getElementById('eq-vfd');        // Retro: Display switch

const knobs  = Array.from(document.querySelectorAll('#eq-panel .eq-band'));
const labels = Array.from(document.querySelectorAll('#eq-panel .eq-band-label'));
const dbLabels = labels.map(l => l.querySelector('.eq-db-val'));

// ── State ─────────────────────────────────────────────────────────────────────
const gains   = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];   // the setting (saved)
const applied = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];   // what is shown / heard now (differs mid-morph)
let bypass = false;
let trim = 0;                  // dB ≤ 0 — Auto pre-gain's extra headroom
let style = DEFAULT_STYLE;
let vfd = true;                // Retro's display strip
let presetName = 'flat';       // '' = custom

let graph = null;              // js/eq/engine.js graph, made on first open
let _graphP = null;
let _morph = null;             // { from, to, t0, raf }
let _clipUntil = 0, _clipTimer = 0, _overDb = 0, _overTimer = 0;
let _meterQuietUntil = 0;      // after Auto pre-gain: the analysers still hold the old samples
let _restoreFocus = null, _releaseTrap = null;
const _resp = createResponse(Player.eqBandDefs, () => (Player.ctx && Player.ctx.sampleRate) || 48000);
let _preDb = 0;                // the pre-gain (dB) last computed by _applyAudio

const _rm = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
const _reducedMotion = () => !!(_rm && _rm.matches);
const _clamp = (g) => Math.max(GAIN_MIN, Math.min(GAIN_MAX, g));
const _snap = (g) => Math.round(g * 2) / 2;
const _isOpen = () => overlay && !overlay.classList.contains('hidden');
const _fmtDb = (g) => {
  const v = (Math.round(g * 10) / 10).toFixed(1).replace('.0', '');
  return g > 0.04 ? '+' + v : g < -0.04 ? '−' + v.replace('-', '') : '0';
};

// ── Audio ─────────────────────────────────────────────────────────────────────
// Last values sent to each AudioParam (so a drag moves one band's param, not
// all ten); reset when the player builds a new graph.
let _sentFor = null;
const _sent = new Array(10).fill(NaN);
let _sentPre = NaN;

function _setParam(param, v) {
  const ctx = Player.ctx;
  try {
    if (ctx && ctx.state === 'running') {
      const t = ctx.currentTime;
      if (typeof param.cancelAndHoldAtTime === 'function') param.cancelAndHoldAtTime(t);
      else { param.cancelScheduledValues(t); param.setValueAtTime(param.value, t); }
      param.setTargetAtTime(v, t, RAMP_TC);
    } else {
      param.cancelScheduledValues(0);
      param.value = v;
    }
  } catch (_) {
    try { param.value = v; } catch (_) { /* engine refused — leave it */ }
  }
}

function _preGainDb() {
  // Headroom: the input to the EQ is lowered by the highest point of the
  // combined curve — neighbouring boosts add up (Pop: +5 bands, +6.7 dB
  // curve), a lone +3 dB band costs about 3 dB, cuts cost nothing — plus
  // Auto pre-gain's trim.  (Without an OfflineAudioContext: the largest band.)
  const pk = _resp.peakDb(applied);
  const peakBoost = Math.max(0, pk == null ? Math.max(...applied) : pk);
  return -peakBoost + trim;
}

/** Put the applied gains / bypass / pre-gain on the player's nodes. */
function _applyAudio(only) {
  const filters = Player.eqFilters;
  if (filters !== _sentFor) { _sentFor = filters; _sent.fill(NaN); _sentPre = NaN; }
  if (filters && filters.length) {
    for (let i = 0; i < filters.length; i++) {
      if (only != null && i !== only) continue;
      const v = bypass ? 0 : applied[i];
      if (_sent[i] === v) continue;
      _sent[i] = v;
      _setParam(filters[i].gain, v);
    }
  }
  // The pre-gain is kept while bypassed: the compare is at the same level.
  _preDb = _preGainDb();
  const pre = Player.eqPreGain;
  if (pre) {
    const lin = Math.pow(10, _preDb / 20);
    if (_sentPre !== lin) { _sentPre = lin; _setParam(pre.gain, lin); }
  }
  _renderPregain();
}

// ── Visuals ───────────────────────────────────────────────────────────────────
function _placeKnob(i) {
  if (!graph) return;
  const k = knobs[i];
  if (!k) return;
  k.style.left = graph.geom.bandX(i) + 'px';
  k.style.top = graph.geom.y(applied[i]) + 'px';
}
function _placeLabels(geom) {
  labels.forEach((l, i) => { l.style.left = geom.bandX(i) + 'px'; });
}
function _renderBand(i) {
  const g = applied[i];
  if (dbLabels[i]) {
    dbLabels[i].textContent = _fmtDb(g);
    labels[i].dataset.sign = g > 0.04 ? 'pos' : g < -0.04 ? 'neg' : 'zero';
  }
  _placeKnob(i);
}
function _renderAria(i) {
  const k = knobs[i];
  if (!k) return;
  const g = gains[i];
  k.setAttribute('aria-valuenow', String(g));
  k.setAttribute('aria-valuetext', `${g > 0 ? '+' : ''}${g} dB`);
}
function _renderAll() {
  for (let i = 0; i < 10; i++) { _renderBand(i); _renderAria(i); }
  if (graph) graph.setGains(applied);
}
function _renderPregain() {
  if (!pregainEl) return;
  const db = Math.round(_preDb * 10) / 10;
  pregainEl.textContent = `Pre-gain ${db === 0 ? '0' : _fmtDb(db)} dB`;
}
function _announce(msg) {
  if (!liveEl) return;
  liveEl.textContent = '';
  setTimeout(() => { liveEl.textContent = msg; }, 30);
}

let _readoutTimer = 0;
function _showReadout(i, holdMs = 0) {
  if (!readout || !graph) return;
  const x = graph.geom.bandX(i), y = graph.geom.y(applied[i]);
  readout.textContent = `${_fmtDb(applied[i])} dB · ${FREQ_LABELS[i]}`;
  const w = graph.geom.width;
  readout.style.left = Math.max(48, Math.min(w - 48, x)) + 'px';
  readout.style.top = y + 'px';
  readout.classList.toggle('eq-below', y < 44);
  readout.classList.add('eq-show');
  clearTimeout(_readoutTimer);
  if (holdMs) _readoutTimer = setTimeout(_hideReadout, holdMs);
}
function _hideReadout() { clearTimeout(_readoutTimer); if (readout) readout.classList.remove('eq-show'); }

// ── Saving ────────────────────────────────────────────────────────────────────
// Debounced — a drag or wheel scrub fires per 0.5 dB tick; one write at the
// end of the gesture (Perf #2).
let _saveTimer = null;
function save() {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({ v: 2, gains, style, trim, vfd }));
    } catch { /* quota or private-mode — fine to drop */ }
  }, 150);
}

// ── Presets ───────────────────────────────────────────────────────────────────
function _matchPreset(arr) {
  for (const [name, vals] of Object.entries(PRESETS)) {
    if (vals.every((v, i) => v === arr[i])) return name;
  }
  return '';
}
function _renderPreset() {
  const name = presetName;
  const nameEl = presetBtn && presetBtn.querySelector('.eq-preset-name');
  if (nameEl) nameEl.textContent = name ? PRESET_NAMES[name] : 'Custom';
  if (presetBtn) presetBtn.setAttribute('aria-label', `Preset: ${name ? PRESET_NAMES[name] : 'Custom'}`);
  if (presetList) {
    for (const li of presetList.querySelectorAll('[role="option"]')) {
      li.setAttribute('aria-selected', String(li.dataset.preset === name));
    }
  }
  // Retro's keys: the preset in use is the checked radio and the one Tab stop
  // (none checked — Custom — the first key is).
  _presetKeys.forEach((b, j) => {
    const on = b.dataset.preset === name;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = (name ? on : j === 0) ? 0 : -1;
  });
  if (presetCustom) presetCustom.hidden = !!name;
}
function _drawPresetIcons() {
  if (!graph || !presetList) return;
  const freqs = new Float32Array(48);
  for (let i = 0; i < 48; i++) freqs[i] = 20 * Math.pow(1000, i / 47);
  for (const li of presetList.querySelectorAll('[role="option"]')) {
    const vals = PRESETS[li.dataset.preset];
    const path = li.querySelector('path');
    if (!vals || !path) continue;
    const db = graph.response(vals, freqs);
    let d = '';
    for (let i = 0; i < 48; i++) {
      const x = (i / 47) * 40;
      const y = 7 - Math.max(-8, Math.min(8, db[i])) * (6 / 8);   // ±8 dB fills the icon
      d += (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
    }
    path.setAttribute('d', d);
  }
}

// ── Changing the gains ────────────────────────────────────────────────────────
function _forgetOvershoot() {
  _overDb = 0;
  clearTimeout(_overTimer);
  if (btnAuto) btnAuto.disabled = true;
}
function _edited() {
  // A hand edit: you hear what you edit; an earlier overshoot no longer applies.
  if (bypass) setBypass(false);
  _forgetOvershoot();
  presetName = _matchPreset(gains);
  _renderPreset();
  _refreshEqBadge();
  save();
}

/** A hand edit of one band (drag, wheel, keys): immediate, no morph. */
function setGain(i, gain) {
  _cancelMorph(true);
  const g = _clamp(_snap(gain));
  if (g === gains[i] && g === applied[i]) return false;
  gains[i] = g; applied[i] = g;
  _renderBand(i); _renderAria(i);
  if (graph) graph.setGains(applied);
  _applyAudio(i);            // (the pre-gain follows the largest boost)
  _edited();
  return true;
}

function _cancelMorph(finish) {
  if (!_morph) return;
  cancelAnimationFrame(_morph.raf);
  _morph = null;
  if (finish) {
    for (let i = 0; i < 10; i++) applied[i] = gains[i];
    _renderAll();
    _applyAudio();
  }
}

/** Move every band to `target`: animated over MORPH_MS (instant with reduced
 *  motion or a hidden page); the audio follows each step. */
function morphTo(target) {
  _cancelMorph(true);
  for (let i = 0; i < 10; i++) gains[i] = _clamp(target[i]);
  if (bypass) setBypass(false);
  _forgetOvershoot();
  presetName = _matchPreset(gains);
  _renderPreset();
  save();
  const finish = () => {
    for (let i = 0; i < 10; i++) applied[i] = gains[i];
    _renderAll(); _applyAudio(); _refreshEqBadge();
  };
  if (_reducedMotion() || document.hidden || !_isOpen()) { finish(); return; }
  const from = applied.slice();
  const m = { t0: performance.now(), raf: 0 };
  _morph = m;
  const step = (ts) => {
    if (_morph !== m) return;
    const k = Math.min(1, Math.max(0, (ts - m.t0) / MORPH_MS));
    const e = 1 - Math.pow(1 - k, 3);          // ease-out
    for (let i = 0; i < 10; i++) applied[i] = from[i] + (gains[i] - from[i]) * e;
    if (k >= 1) { _morph = null; finish(); return; }
    for (let i = 0; i < 10; i++) _renderBand(i);
    if (graph) graph.setGains(applied);
    _applyAudio();
    m.raf = requestAnimationFrame(step);
  };
  m.raf = requestAnimationFrame(step);
  for (let i = 0; i < 10; i++) _renderAria(i);
}

// ── Bypass ────────────────────────────────────────────────────────────────────
function setBypass(on) {
  on = !!on;
  if (on === bypass) return;
  bypass = on;
  if (btnBypass) btnBypass.setAttribute('aria-pressed', String(on));
  if (panel) panel.classList.toggle('eq-bypassed', on);
  if (graph) graph.setBypass(on);
  _applyAudio();
  _announce(on ? 'Bypass on' : 'Bypass off');
}

// ── Clip lamp + Auto pre-gain ─────────────────────────────────────────────────
function _setLamp(on) {
  if (!clipEl) return;
  const v = String(!!on);
  if (clipEl.dataset.on === v) return;
  clipEl.dataset.on = v;
  clipEl.setAttribute('aria-label', on ? 'Clip lamp: on' : 'Clip lamp: off');
}
function _onMeter({ peak, clip }) {
  if (bypass) return;          // the dry signal's peaks are not the EQ's doing
  const now = performance.now();
  if (now < _meterQuietUntil) return;
  if (clip) {
    _clipUntil = now + CLIP_HOLD_MS;
    _overDb = Math.max(_overDb, 20 * Math.log10(peak));
    _setLamp(true);
    if (btnAuto) btnAuto.disabled = trim <= TRIM_MIN;
    clearTimeout(_clipTimer);
    _clipTimer = setTimeout(() => { if (performance.now() >= _clipUntil) _setLamp(false); }, CLIP_HOLD_MS + 20);
    clearTimeout(_overTimer);
    _overTimer = setTimeout(_forgetOvershoot, OVER_KEEP_MS);
  }
}
function autoPreGain() {
  if (_overDb <= 0) return;
  const step = Math.ceil((_overDb + 0.5) * 2) / 2;      // the overshoot + 0.5 dB, in 0.5 dB steps
  trim = Math.max(TRIM_MIN, trim - step);
  _forgetOvershoot();
  _meterQuietUntil = performance.now() + 300;   // ≈ 85 ms analyser window + the param ramp
  _setLamp(false);
  _applyAudio(-1);
  save();
  _announce(`Pre-gain lowered to ${_fmtDb(Math.round(_preDb * 10) / 10)} dB`);
}

// ── Pointer: drag a knob (or the nearest band) on the graph ──────────────────
const _coarse = window.matchMedia ? window.matchMedia('(pointer: coarse)') : null;
const _hitR = () => ((_coarse && _coarse.matches) ? 22 : 19);   // a knob's grab radius (px)
/** The knob nearest the pointer (stage coordinates) and its distance. */
function _nearestKnob(x, y) {
  let best = -1, bestD = Infinity;
  for (let i = 0; i < 10; i++) {
    const d = Math.hypot(graph.geom.bandX(i) - x, graph.geom.y(applied[i]) - y);
    if (d < bestD) { bestD = d; best = i; }
  }
  return { i: best, d: bestD };
}
/** The band whose column holds x (within half a band's spacing), or -1. */
function _bandAtX(x) {
  let best = -1, bestD = Infinity;
  for (let i = 0; i < 10; i++) {
    const d = Math.abs(graph.geom.bandX(i) - x);
    if (d < bestD) { bestD = d; best = i; }
  }
  const spacing = graph.geom.bandX(1) - graph.geom.bandX(0);
  return bestD <= spacing * 0.5 ? best : -1;
}
let _drag = null;   // { i, id, offset, rect, pending, y0 }
stage?.addEventListener('pointerdown', (e) => {
  if (!graph || _drag || (e.button !== undefined && e.button !== 0)) return;
  const rect = stage.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  // A knob under the pointer — the nearest one, so overlapping touch hit
  // areas pick the dot you aimed at — keeps its grab offset (no jump).
  // Elsewhere in a band's column the band follows once the pointer moves; a
  // plain click on the graph changes nothing.
  const near = _nearestKnob(x, y);
  const onKnob = near.d <= _hitR();
  const i = onKnob ? near.i : _bandAtX(x);
  if (i < 0) return;
  e.preventDefault();
  _drag = { i, id: e.pointerId, rect, pending: !onKnob, y0: e.clientY,
            offset: onKnob ? y - graph.geom.y(applied[i]) : 0 };
  try { stage.setPointerCapture(e.pointerId); } catch (_) {}
  knobs[i].classList.remove('eq-settle');
  if (onKnob) { knobs[i].classList.add('eq-dragging'); _showReadout(i); }
  try { knobs[i].focus({ preventScroll: true }); } catch (_) {}
});
stage?.addEventListener('pointermove', (e) => {
  if (!_drag || e.pointerId !== _drag.id || !graph) return;
  if (_drag.pending) {
    if (Math.abs(e.clientY - _drag.y0) < 3) return;
    _drag.pending = false;
    knobs[_drag.i].classList.add('eq-dragging');
  }
  setGain(_drag.i, graph.geom.dbAt(e.clientY - _drag.rect.top - _drag.offset));
  _showReadout(_drag.i);
});
function _endDrag(e) {
  if (!_drag || (e && e.pointerId !== _drag.id)) return;
  const k = knobs[_drag.i];
  const moved = !_drag.pending;
  _drag = null;
  k.classList.remove('eq-dragging');
  if (moved && !_reducedMotion()) {
    k.classList.remove('eq-settle');
    void k.offsetWidth;                      // restart the animation
    k.classList.add('eq-settle');
  }
  _hideReadout();
}
stage?.addEventListener('pointerup', _endDrag);
stage?.addEventListener('pointercancel', _endDrag);
stage?.addEventListener('lostpointercapture', _endDrag);
// Wheel over a knob: ±0.5 dB per notch.  Small (trackpad) deltas add up to a
// notch's worth first; over the rest of the graph the wheel scrolls as usual.
let _wheelAcc = 0, _wheelBand = -1;
const WHEEL_STEP_PX = 50;
stage?.addEventListener('wheel', (e) => {
  if (!graph) return;
  const knob = e.target.closest && e.target.closest('.eq-band');
  if (!knob) return;
  const i = Number(knob.dataset.band);
  e.preventDefault();
  if (i !== _wheelBand) { _wheelBand = i; _wheelAcc = 0; }
  const px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
  _wheelAcc += px;
  if (Math.abs(_wheelAcc) < WHEEL_STEP_PX && e.deltaMode === 0) return;
  const dir = _wheelAcc < 0 ? 1 : -1;
  _wheelAcc = 0;
  setGain(i, gains[i] + dir * 0.5);
  _showReadout(i, 900);
}, { passive: false });
// Double-click a knob = 0 dB.  On the stage, not the knob: the first click's
// pointer capture makes the stage the dblclick's target.
stage?.addEventListener('dblclick', (e) => {
  if (!graph) return;
  const rect = stage.getBoundingClientRect();
  const near = _nearestKnob(e.clientX - rect.left, e.clientY - rect.top);
  if (near.i < 0 || near.d > _hitR()) return;
  e.preventDefault();
  setGain(near.i, 0);
  _showReadout(near.i, 700);
});
knobs.forEach((k, i) => {
  k.addEventListener('animationend', () => k.classList.remove('eq-settle'));
  // Keyboard — the role="slider" contract.  stopPropagation: the arrows and
  // Space are also app-wide shortcuts (volume, play/pause).
  k.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const STEP = 0.5, BIG = 3;
    let next = gains[i];
    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowRight':  next = gains[i] + STEP; break;
      case 'ArrowDown':
      case 'ArrowLeft':   next = gains[i] - STEP; break;
      case 'PageUp':      next = gains[i] + BIG;  break;
      case 'PageDown':    next = gains[i] - BIG;  break;
      case 'Home':        next = GAIN_MIN;        break;
      case 'End':         next = GAIN_MAX;        break;
      case 'Enter':
      case ' ':           next = 0;               break;  // quick reset to flat
      default: return;
    }
    e.preventDefault();
    e.stopPropagation();
    setGain(i, next);
    _showReadout(i, 900);
  });
  k.addEventListener('blur', () => { if (!_drag) _hideReadout(); });
});

// ── Preset menu (listbox) ─────────────────────────────────────────────────────
const _options = presetList ? Array.from(presetList.querySelectorAll('[role="option"]')) : [];
let _activeOpt = -1;
function _setActiveOpt(n) {
  _activeOpt = Math.max(0, Math.min(_options.length - 1, n));
  _options.forEach((o, j) => o.classList.toggle('eq-active', j === _activeOpt));
  const o = _options[_activeOpt];
  if (o) { if (!o.id) o.id = `eq-preset-opt-${o.dataset.preset}`; presetList.setAttribute('aria-activedescendant', o.id); }
}
function _menuOpen() { return presetList && !presetList.classList.contains('hidden'); }
function openPresetMenu() {
  if (!presetList) return;
  _drawPresetIcons();
  presetList.classList.remove('hidden');
  presetBtn.setAttribute('aria-expanded', 'true');
  const cur = _options.findIndex(o => o.dataset.preset === presetName);
  _setActiveOpt(cur >= 0 ? cur : 0);
  presetList.focus({ preventScroll: true });
}
function closePresetMenu(focusBtn = true) {
  if (!_menuOpen()) return;
  presetList.classList.add('hidden');
  presetBtn.setAttribute('aria-expanded', 'false');
  if (focusBtn) presetBtn.focus({ preventScroll: true });
}
function _pickPreset(name) {
  const p = PRESETS[name];
  closePresetMenu();
  if (p) { morphTo(p); _announce(`Preset: ${PRESET_NAMES[name]}`); }
}
presetBtn?.addEventListener('click', () => { _menuOpen() ? closePresetMenu() : openPresetMenu(); });
presetBtn?.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); openPresetMenu(); }
});
presetList?.addEventListener('click', (e) => {
  const li = e.target.closest('[role="option"]');
  if (li) _pickPreset(li.dataset.preset);
});
// A radiogroup's keys (APG): arrows / Home / End move to a radio and choose it.
// stopPropagation: the arrows and Home / End are app-wide shortcuts too.
function _radioKeys(group, btns, choose) {
  group?.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const at = btns.indexOf(document.activeElement);
    if (at < 0) return;
    const n = btns.length;
    let to;
    switch (e.key) {
      case 'ArrowRight': case 'ArrowDown': to = (at + 1) % n; break;
      case 'ArrowLeft':  case 'ArrowUp':   to = (at - 1 + n) % n; break;
      case 'Home': to = 0; break;
      case 'End':  to = n - 1; break;
      default: return;
    }
    e.preventDefault();
    e.stopPropagation();
    btns.forEach((b, j) => { b.tabIndex = j === to ? 0 : -1; });
    btns[to].focus();
    choose(btns[to]);
  });
}
// Retro's preset keys: the same presets, one key each.
presetRow?.addEventListener('click', (e) => {
  const b = e.target.closest('button[data-preset]');
  if (b) _pickPreset(b.dataset.preset);
});
_radioKeys(presetRow, _presetKeys, (b) => _pickPreset(b.dataset.preset));
presetList?.addEventListener('keydown', (e) => {
  switch (e.key) {
    case 'ArrowDown': _setActiveOpt(_activeOpt + 1); break;
    case 'ArrowUp':   _setActiveOpt(_activeOpt - 1); break;
    case 'Home':      _setActiveOpt(0); break;
    case 'End':       _setActiveOpt(_options.length - 1); break;
    case 'Enter':
    case ' ':         if (_options[_activeOpt]) _pickPreset(_options[_activeOpt].dataset.preset); break;
    case 'Escape':    closePresetMenu(); break;
    case 'Tab':       closePresetMenu(false); return;
    default: return;
  }
  e.preventDefault();
  e.stopPropagation();
});
// A press outside the menu closes it; on the graph that press does nothing
// else (it would otherwise also move a band).
document.addEventListener('pointerdown', (e) => {
  if (!_menuOpen() || e.target.closest('.eq-preset-wrap')) return;
  closePresetMenu(false);
  if (stage && stage.contains(e.target)) { e.preventDefault(); e.stopPropagation(); }
}, true);

// ── Buttons ───────────────────────────────────────────────────────────────────
btnReset?.addEventListener('click', () => {
  trim = 0; _overDb = 0;
  if (btnAuto) btnAuto.disabled = true;
  morphTo(PRESETS.flat);
  _applyAudio(-1);
});
btnBypass?.addEventListener('click', () => setBypass(!bypass));
btnAuto?.addEventListener('click', autoPreGain);
// Optional-chained: a markup change that drops a control degrades to "that
// control does nothing" instead of throwing and breaking SPA boot.
panel?.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return;
  // Space / Enter on the panel's buttons press the button — keep them from
  // the app-wide Space (play / pause) shortcut.
  if (t && t.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) { e.stopPropagation(); return; }
  if (e.key === 'b' || e.key === 'B') {
    e.preventDefault();
    e.stopPropagation();
    setBypass(!bypass);
  }
});

// "EQ active" indicator on the toolbar button: ``on`` while the overlay
// is open *or* whenever any band is non-flat.
function _eqIsActive() {
  return gains.some(g => Math.abs(g) > 0.05);
}
function _refreshEqBadge() {
  if (!btnToggle) return;
  const open = _isOpen();
  btnToggle.classList.toggle('on', open || _eqIsActive());
  btnToggle.title = _eqIsActive() && !open
    ? 'Equalizer (active — non-flat preset)'
    : 'Equalizer';
}

// ── What's playing (the track the EQ shapes) ─────────────────────────────────
function _renderNowPlaying() {
  if (!npEl) return;
  let title = '', who = '';
  if (Player.stationMode && Player.station) {
    title = Player.station.name || 'Radio';
  } else {
    const t = Player.currentTrack;
    if (t && Player.nowPlayingId) { title = t.title || t.filename || ''; who = t.artist || t.album_artist || ''; }
  }
  npEl.textContent = '';
  if (!title) { npEl.textContent = 'Nothing playing'; return; }
  npEl.append('Now playing ');
  const b = document.createElement('b');
  b.textContent = title;
  npEl.append(b);
  if (who) npEl.append(` — ${who}`);
  npEl.title = who ? `${title} — ${who}` : title;
}

// ── Style (look) ──────────────────────────────────────────────────────────────
const _styleBtns = styleGroup ? Array.from(styleGroup.querySelectorAll('button[data-eq-style]')) : [];
function _renderStyleToggle() {
  // A radiogroup: the chosen look is the one Tab stop, arrows move between.
  for (const b of _styleBtns) {
    const on = b.dataset.eqStyle === style;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  }
}
let _styleTok = 0;
// Show look ``name`` (the shared UI style changed — here or in another panel).
// Before the graph exists only the panel's attribute changes: _ensureGraph
// loads the look's renderer on first open.
async function _applyStyle(name) {
  if (!STYLE_MODULES[name]) return false;
  const tok = ++_styleTok;                    // the last choice wins a slow module load
  if (graph) {
    try { await STYLE_MODULES[name](); } catch (_) { return false; }
    if (tok !== _styleTok) return false;
  }
  if (name === style) { _renderStyleToggle(); return true; }
  style = name;
  if (panel) panel.dataset.eqStyle = name;   // the look's CSS (and colours the renderer reads)
  if (graph) graph.setStyle(name);
  _renderStyleToggle();
  save();                                     // (sb_eq keeps a copy of the style)
  return true;
}
/** Choose the look — the app-wide UI style, so Stations and Playlists follow. */
function setStyle(name) {
  if (!STYLE_MODULES[name]) return Promise.resolve(false);
  setUiStyle(name);
  return _applyStyle(name);
}
onUiStyleChange((name) => { _applyStyle(name); });
styleGroup?.addEventListener('click', (e) => {
  const b = e.target.closest('button[data-eq-style]');
  if (b) setStyle(b.dataset.eqStyle);
});
_radioKeys(styleGroup, _styleBtns, (b) => setStyle(b.dataset.eqStyle));

// Retro's Display switch: the VFD strip over the slots, on or off.
function _renderVfd() {
  if (panel) panel.dataset.eqVfd = vfd ? 'on' : 'off';
  if (btnVfd) btnVfd.setAttribute('aria-pressed', String(vfd));
}
function setDisplay(on) {
  vfd = !!on;
  _renderVfd();
  if (graph && _isOpen()) graph.layout();     // the slots take the display's room
  save();
}
btnVfd?.addEventListener('click', () => setDisplay(!vfd));

// ── The graph (lazy: first open) ──────────────────────────────────────────────
function _ensureGraph() {
  if (graph) return Promise.resolve(graph);
  if (!_graphP) {
    const want = style;
    _graphP = Promise.all([import('./eq/engine.js'), STYLE_MODULES[want]()]).then(([eng]) => {
      graph = eng.createEqGraph({
        stage, canvas, panel, player: Player, styleName: want,
        onLayout: (geom) => { _placeLabels(geom); for (let i = 0; i < 10; i++) _placeKnob(i); },
        onMeter: _onMeter,
      });
      graph.setGains(applied);
      graph.setBypass(bypass);
      // The UI style changed while the renderer loaded: switch to it now.
      if (style !== want) { const now = style; style = want; _applyStyle(now); }
      return graph;
    }).catch((err) => { _graphP = null; console.warn('Equalizer graph unavailable:', err); return null; });
  }
  return _graphP;
}

// ── Overlay open/close ────────────────────────────────────────────────────────
function open() {
  overlay.classList.remove('hidden');
  // Reflect the live ReplayGain mode each time the panel opens (the setting
  // lives in the Player + localStorage, so it may have changed elsewhere).
  if (rgSel && Player.replayGainMode) rgSel.value = Player.replayGainMode;
  _renderNowPlaying();
  _refreshEqBadge();
  _restoreFocus = document.activeElement;
  try { panel.focus({ preventScroll: true }); } catch (_) {}
  if (_releaseTrap) _releaseTrap();
  _releaseTrap = trapFocus(panel);
  _ensureGraph().then((g) => {
    if (!g || !_isOpen()) return;
    g.setActive(true);
    _renderAll();
  });
}
function close() {
  closePresetMenu(false);
  _cancelMorph(true);
  _renderAll(); _applyAudio();
  setBypass(false);            // bypass is a momentary compare, not a setting
  _setLamp(false);
  _hideReadout();
  overlay.classList.add('hidden');
  if (_releaseTrap) { _releaseTrap(); _releaseTrap = null; }
  if (graph) graph.setActive(false);
  _refreshEqBadge();
  const rf = _restoreFocus;
  _restoreFocus = null;
  if (rf && typeof rf.focus === 'function' && document.contains(rf)) { try { rf.focus({ preventScroll: true }); } catch (_) {} }
}
function toggle() {
  _isOpen() ? close() : open();
}

btnClose?.addEventListener('click', close);
overlay?.addEventListener('click', (e) => { if (e.target === overlay) close(); });
// ReplayGain mode — off / per-track / per-album.  Player persists the choice
// and re-applies it to the current track immediately.
if (rgSel) {
  rgSel.addEventListener('change', () => {
    if (Player.setReplayGainMode) Player.setReplayGainMode(rgSel.value);
  });
}
const _above = ['shortcuts-overlay', 'links-overlay'].map(id => document.getElementById(id)).filter(Boolean);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !_isOpen()) return;
  if (_menuOpen()) { closePresetMenu(); return; }   // Escape closes just the menu first
  // An overlay opened over the EQ (the ? sheet) takes this Escape (app.js).
  if (_above.some(el => !el.classList.contains('hidden'))) return;
  close();
});

// Player wiring: a new audio graph (first play) gets the panel's state —
// including the pre-gain, which the graph is built without; play / pause
// starts / stops the spectrum; a new track renames "Now playing …".
Player.on('eqready', () => { _sentFor = null; _applyAudio(); if (graph) graph.resetTaps(); });
Player.on('statechange', () => { if (graph && _isOpen()) graph.refresh(); });
Player.on('trackchange', () => { if (_isOpen()) _renderNowPlaying(); });

// ── Restore from localStorage on load ────────────────────────────────────────
function _restore() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null'); } catch (_) { saved = null; }
  const arr = Array.isArray(saved) ? saved : (saved && Array.isArray(saved.gains) ? saved.gains : null);
  const ok = arr && arr.length === 10 && arr.every(Number.isFinite);
  const src = ok ? arr : PRESETS.flat;
  for (let i = 0; i < 10; i++) { gains[i] = _clamp(_snap(src[i])); applied[i] = gains[i]; }
  if (saved && !Array.isArray(saved)) {
    if (Number.isFinite(saved.trim)) trim = Math.max(TRIM_MIN, Math.min(0, saved.trim));
    if (typeof saved.vfd === 'boolean') vfd = saved.vfd;
  }
  // The look is the shared UI style (seeded from sb_eq.style on first run).
  const ui = getUiStyle();
  if (STYLE_MODULES[ui]) style = ui;
  if (panel) panel.dataset.eqStyle = style;
  _renderVfd();
  // Until the graph lays them out (or if it can't load): spread in a row.
  knobs.forEach((k, i) => { k.style.left = ((i + 0.5) * 10) + '%'; k.style.top = '50%'; });
  labels.forEach((l, i) => { l.style.left = ((i + 0.5) * 10) + '%'; });
  presetName = _matchPreset(gains);
  _renderPreset();
  _renderStyleToggle();
  for (let i = 0; i < 10; i++) { _renderBand(i); _renderAria(i); }
  _applyAudio();
  _refreshEqBadge();
}
_restore();

// Reflect the live ReplayGain mode on the control at load — not only when the
// panel is first opened — so it never shows "Off" while album-mode leveling
// (the default) is actually applying.
if (rgSel && Player.replayGainMode) rgSel.value = Player.replayGainMode;

export const Equalizer = {
  toggle, open, close, setBypass, setStyle, setDisplay,
  // Diagnostics / tests
  get gains() { return gains.slice(); },
  get applied() { return applied.slice(); },
  get bypassed() { return bypass; },
  get trim() { return trim; },
  get style() { return style; },
  get display() { return vfd; },
  get graph() { return graph; },
};
