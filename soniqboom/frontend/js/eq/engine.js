// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * eq/engine.js — the equalizer panel's drawing engine, shared by every look.
 *
 * The engine owns what is the same whatever the panel looks like:
 *   • geometry: a log frequency axis (20 Hz – 20 kHz) and a dB axis;
 *   • the response curve: the combined dB response of the 10 bands for the
 *     gains the panel shows (mid-morph too), from eq/response.js (mirror
 *     BiquadFilterNodes, getFrequencyResponse);
 *   • the spectrum of the playing audio, pre- and post-EQ, log-binned and
 *     smoothed, per-band levels and a loudness value, and the clip peak;
 *   • the loop: one requestAnimationFrame loop, run only while the panel is
 *     open, the page visible, audio playing and visualizations on (plus the
 *     short bypass / fade-out animations); the analysers' spectra are read at
 *     most READ_HZ times a second (60 Hz analyser reads starved Firefox's
 *     audio thread — app.js VU), the clip peak on its own PEAK_MS timer
 *     whatever the frame rate.  Visualizations off → the curve without the
 *     moving spectrum; reduced motion → no animation, the spectrum frozen (one
 *     frame), not hidden;
 *   • the canvas (devicePixelRatio-aware, resized with its stage).
 *
 * A look ("style") is a renderer registered with registerEqStyle(name, r):
 *   r.vars      — {key: '--css-custom-property'} read from the panel once per
 *                 theme / style change into the theme object handed to r;
 *   r.drawGrid(g, scene, theme)   — the static layer (cached; redrawn on
 *                 resize / theme / style change);
 *   r.drawFrame(g, scene, theme)  — everything that moves (curve, spectrum,
 *                 glows), drawn over the cached grid each frame.  Returning
 *                 true asks for another frame (the look's own animation —
 *                 a falling peak, a power-on flicker); it is never asked
 *                 while the panel is closed or the page hidden, and a look
 *                 must not ask when scene.motion is false (reduced motion);
 *   scene.headroomDb / specRangeDb — the pre-gain (headroom) the post spectrum
 *                 was read under, and the dB a 0…1 spectrum value spans: a look
 *                 can add headroomDb / specRangeDb to show a band's level as
 *                 boosted (the pre-gain otherwise cancels a boost); scene.viz —
 *                 visualizations on (a look's decorative animation follows it);
 *   r.layout({w, h, narrow, panel})  — optional: the look's own geometry,
 *                 {plot, dbSpan?, x?(f), bandX?(i), …}; plot is the area the
 *                 knobs move in (y(dB) maps into it), x / bandX replace the
 *                 log axis / the bands' x, and the whole object is handed to
 *                 the renderer as scene.layout.  Without it: a log axis over
 *                 a plot that fills the stage.
 * The panel carries data-eq-style="<name>", so a look's CSS (knobs, labels,
 * panel surface, the --eq-* colour tokens) is scoped to it.
 */

import { vizGroupEnabled, prefersReducedMotion } from '../viz/engine.js';
import { createResponse, logFreqs, F_MIN, F_MAX } from './response.js';

export { F_MIN, F_MAX };
export const DB_SPAN = 15;          // the dB axis runs −DB_SPAN … +DB_SPAN
const CURVE_POINTS = 256;
const SPEC_POINTS = 112;
const READ_HZ = 15;                 // spectrum reads per second (live mode)
const PEAK_MS = 80;                 // clip-peak reads (each covers 4096 samples ≈ 85 ms at 48 kHz)
const SPEC_FLOOR = -100;            // dBFS at the bottom of the spectrum
const SPEC_CEIL = -12;              // dBFS at the top
const SPEC_TILT = 3;                // dB per octave around 1 kHz (music falls off ~3 dB/oct)
const BYPASS_MS = 180;
const FREEZE_AFTER_MS = 400;        // a just-connected analyser holds no audio yet

// ── style registry ───────────────────────────────────────────────────────────
const _styles = new Map();
export function registerEqStyle(name, renderer) { _styles.set(name, renderer); }
export function eqStyle(name) { return _styles.get(name) || null; }
export function eqStyleNames() { return [..._styles.keys()]; }

// Any CSS colour (hex, rgb(), hsl(), a named colour, …) → its RGB, resolved
// once by painting it into a 1×1 canvas; then `rgba(r,g,b,a)` at any alpha.
const _rgbCache = new Map();
let _probe = null;
function _rgb(color) {
  const key = (color || '').trim();
  let v = _rgbCache.get(key);
  if (v) return v;
  v = [240, 114, 42];
  try {
    if (!_probe) { const c = document.createElement('canvas'); c.width = c.height = 1; _probe = c.getContext('2d', { willReadFrequently: true }); }
    _probe.clearRect(0, 0, 1, 1);
    _probe.fillStyle = '#f0722a';
    if (key) _probe.fillStyle = key;
    _probe.fillRect(0, 0, 1, 1);
    const d = _probe.getImageData(0, 0, 1, 1).data;
    v = [d[0], d[1], d[2]];
  } catch (_) { /* keep the default */ }
  _rgbCache.set(key, v);
  return v;
}
/** `color` (any CSS colour) at alpha `a`, as rgba() — for the looks' gradients. */
export function cssAlpha(color, a) {
  const [r, g, b] = _rgb(color);
  return `rgba(${r},${g},${b},${a})`;
}

const _now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

/**
 * createEqGraph({stage, canvas, panel, player, styleName, onLayout, onMeter})
 *   stage    — the element the canvas fills (sized by CSS; observed)
 *   panel    — the element carrying data-eq-style and the theme variables
 *   player   — the Player (ctx, playing, eqTaps(), releaseEqTaps(),
 *              replayGain, eqPreGain, eqBandDefs)
 *   onLayout(geom) — after a resize: place knobs / labels
 *   onMeter({peak, clip}) — after each peak read (linear peak; clip = past 0 dBFS)
 */
export function createEqGraph({ stage, canvas, panel, player, styleName = 'modern', onLayout, onMeter }) {
  const g = canvas.getContext('2d');
  const defs = player.eqBandDefs;
  const nBands = defs.length;
  const resp = createResponse(defs, () => (player.ctx && player.ctx.sampleRate) || 48000);

  let renderer = eqStyle(styleName) || eqStyle('modern');
  let theme = null;
  let active = false;
  const shown = new Float32Array(nBands);    // gains the curve shows
  const scaled = new Float32Array(nBands);
  let bypassMix = 0, bypassTo = 0, bypassFrom = 0, bypassT0 = 0;
  let curveDirty = true;
  const curveFreqs = logFreqs(CURVE_POINTS);
  const curveDb = new Float32Array(CURVE_POINTS);
  const ghostDb = new Float32Array(CURVE_POINTS);
  const specFreqs = logFreqs(SPEC_POINTS);
  const preT = new Float32Array(SPEC_POINTS), postT = new Float32Array(SPEC_POINTS);   // targets
  const preD = new Float32Array(SPEC_POINTS), postD = new Float32Array(SPEC_POINTS);   // shown
  const levels = new Float32Array(nBands);
  let loudT = 0, loudD = 0;
  let specLive = false;            // a spectrum is (or was just) being shown
  let headroomDb = 0;              // −pre-gain (dB) when the post spectrum was read (scene.headroomDb)
  let frozenShown = false;         // reduced motion: the one frozen spectrum frame was taken
  let rafId = 0, peakTimer = 0, lastRead = 0, lastFrame = 0;
  let pendingDraw = 0;
  let W = 0, H = 0, dpr = 1;
  let plot = { l: 34, t: 10, w: 100, h: 100 };
  let span = DB_SPAN, xOver = null, bandXOver = null, lay = null;
  let since = 0;                   // when the panel opened / the look changed (scene.since)
  let styleAnim = false;           // the look asked for another frame
  let gridCanvas = null;
  let taps = null, tapsAt = 0;
  let fbuf = null, tbuf = null, binMap = null, binMapKey = '';
  const stats = { frames: 0, drawMs: 0, maxDrawMs: 0, reads: 0, peakReads: 0 };

  // ── geometry ──────────────────────────────────────────────────────────────
  const LOGR = Math.log(F_MAX / F_MIN);
  const xOf = (f) => (xOver ? xOver(f) : plot.l + (Math.log(f / F_MIN) / LOGR) * plot.w);
  const yOf = (db) => plot.t + ((span - db) / (2 * span)) * plot.h;
  const dbOfY = (y) => span - ((y - plot.t) / plot.h) * 2 * span;
  const bandXOf = (i) => (bandXOver ? bandXOver(i) : xOf(defs[i].freq));
  const geom = {
    get plot() { return plot; }, get width() { return W; }, get height() { return H; },
    x: xOf, y: yOf, dbAt: dbOfY, bandX: bandXOf, narrow: false,
  };

  let dprMql = null;
  const onDpr = () => { if (active) layout(); };
  function _watchDpr() {
    if (!window.matchMedia) return;
    if (dprMql) { try { dprMql.removeEventListener('change', onDpr); } catch (_) {} }
    dprMql = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    try { dprMql.addEventListener('change', onDpr); } catch (_) {}
  }

  function layout() {
    // clientWidth/Height, not getBoundingClientRect: the panel opens with a
    // scale animation and the canvas must take its untransformed size.
    const nw = Math.max(1, stage.clientWidth), nh = Math.max(1, stage.clientHeight);
    const nd = Math.min(3, window.devicePixelRatio || 1);
    const changed = nw !== W || nh !== H || nd !== dpr;
    if (nd !== dpr || !dprMql) _watchDpr();
    W = nw; H = nh; dpr = nd;
    // (a little hysteresis: a look's narrow CSS can change the panel's height
    // and so, with classic scrollbars, the stage's width)
    const narrow = W < (geom.narrow ? 532 : 520);
    geom.narrow = narrow;
    panel.toggleAttribute('data-eq-narrow', narrow);   // the looks' CSS follows the canvas' narrow layout
    lay = renderer.layout ? renderer.layout({ w: W, h: H, narrow, panel }) : null;
    if (lay && lay.plot) {
      plot = lay.plot;
      span = lay.dbSpan || DB_SPAN;
      xOver = lay.x || null;
      bandXOver = lay.bandX || null;
    } else {
      const l = narrow ? 28 : 34, rp = narrow ? 8 : 12;
      plot = { l, t: 10, w: Math.max(10, W - l - rp), h: Math.max(10, H - 10 - 10) };
      span = DB_SPAN; xOver = bandXOver = null; lay = null;
    }
    if (changed) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = W + 'px';
      canvas.style.height = H + 'px';
    }
    gridCanvas = null;             // the look's geometry may change without a resize
    if (onLayout) onLayout(geom);
    requestDraw();
  }

  function readTheme() {
    const cs = getComputedStyle(panel);
    const t = {};
    for (const [k, v] of Object.entries(renderer.vars || {})) t[k] = cs.getPropertyValue(v).trim();
    t.font = cs.fontFamily || 'system-ui, sans-serif';
    theme = t;
    gridCanvas = null;
  }

  // ── spectrum binning ──────────────────────────────────────────────────────
  function _ensureBins(an) {
    const rate = player.ctx ? player.ctx.sampleRate : 48000;
    const key = an.frequencyBinCount + '@' + rate;
    if (binMapKey === key) return;
    binMapKey = key;
    const n = an.frequencyBinCount, hz = rate / an.fftSize;
    fbuf = new Float32Array(n);
    binMap = [];
    for (let i = 0; i < SPEC_POINTS; i++) {
      const f = specFreqs[i];
      const fl = i ? Math.sqrt(f * specFreqs[i - 1]) : f;
      const fh = i < SPEC_POINTS - 1 ? Math.sqrt(f * specFreqs[i + 1]) : f;
      const lo = Math.max(1, Math.floor(fl / hz)), hi = Math.min(n - 1, Math.ceil(fh / hz));
      binMap.push({ lo, hi, pos: Math.min(n - 1.001, f / hz), tilt: SPEC_TILT * Math.log2(f / 1000) });
    }
  }
  function _readSpectrum(an, out, offsetDb) {
    _ensureBins(an);
    an.getFloatFrequencyData(fbuf);
    for (let i = 0; i < SPEC_POINTS; i++) {
      const m = binMap[i];
      let db;
      if (m.hi - m.lo <= 1) {           // narrower than a bin: interpolate
        const k = Math.floor(m.pos), fr = m.pos - k;
        const a = fbuf[k], b = fbuf[k + 1];
        db = (Number.isFinite(a) ? a : -200) * (1 - fr) + (Number.isFinite(b) ? b : -200) * fr;
      } else {
        db = -200;
        for (let k = m.lo; k <= m.hi; k++) if (fbuf[k] > db) db = fbuf[k];
      }
      const v = (db + offsetDb + m.tilt - SPEC_FLOOR) / (SPEC_CEIL - SPEC_FLOOR);
      out[i] = v < 0 ? 0 : v > 1 ? 1 : v;
    }
    // light spatial smoothing (soft peaks)
    let prev = out[0];
    for (let i = 1; i < SPEC_POINTS - 1; i++) {
      const cur = out[i];
      out[i] = 0.25 * prev + 0.5 * cur + 0.25 * out[i + 1];
      prev = cur;
    }
  }
  function _readPeak() {
    if (!taps) return;
    stats.peakReads++;
    let peak = 0, sum = 0, n = 0;
    for (const an of taps.peaks) {
      if (!tbuf || tbuf.length !== an.fftSize) tbuf = new Float32Array(an.fftSize);
      an.getFloatTimeDomainData(tbuf);
      let s = 0;
      for (let i = 0; i < tbuf.length; i++) {
        const v = tbuf[i], a = v < 0 ? -v : v;
        if (a > peak) peak = a;
        s += v * v;
      }
      if (s > 0) { sum += s / tbuf.length; n++; }
    }
    const rms = n ? Math.sqrt(sum / n) : 0;
    const rmsDb = rms > 0 ? 20 * Math.log10(rms) : -120;
    loudT = Math.max(0, Math.min(1, (rmsDb + 42) / 36));
    if (onMeter) onMeter({ peak, clip: peak > 1.0 });
  }
  function _readSpectra() {
    if (!taps) return;
    stats.reads++;
    const gainDb = (node) => {
      const v = node ? node.gain.value : 1;
      return v > 0 ? 20 * Math.log10(v) : 0;
    };
    // The pre tap sits after the pre-gain: add it back (and the ReplayGain the
    // post tap includes) so the pre trace is the track as it comes in, at the
    // post trace's level reference — it doesn't move when a band is boosted.
    _readSpectrum(taps.pre, preT, gainDb(player.replayGain) - gainDb(player.eqPreGain));
    _readSpectrum(taps.post, postT, 0);
    headroomDb = -gainDb(player.eqPreGain);
    specLive = true;
  }
  function _bandLevels() {
    for (let b = 0; b < nBands; b++) {
      const f = defs[b].freq, lo = f / 1.26, hi = f * 1.26;
      let s = 0, c = 0;
      for (let i = 0; i < SPEC_POINTS; i++) {
        if (specFreqs[i] >= lo && specFreqs[i] <= hi) { s += postD[i]; c++; }
      }
      levels[b] = c ? s / c : 0;
    }
  }

  // ── modes ─────────────────────────────────────────────────────────────────
  function _vizOn() { try { return vizGroupEnabled('nowPlaying'); } catch (_) { return true; } }
  function _rm() { try { return prefersReducedMotion(); } catch (_) { return false; } }
  function _audible() { return active && !document.hidden && !!player.playing; }
  /** 'live' (animated spectrum), 'meter' (clip peak only; reduced motion /
   *  visualizations off) or 'idle'.  `acquire` connects the taps if needed. */
  function _mode(acquire = true) {
    if (!_audible()) return 'idle';
    if (!taps && acquire) { taps = player.eqTaps ? player.eqTaps() : null; tapsAt = _now(); }
    if (!taps) return 'idle';
    if (_rm() || !_vizOn()) return 'meter';
    return 'live';
  }
  function _animating() {
    if (bypassMix !== bypassTo || styleAnim) return true;
    if (specLive && !_rm()) {             // fading to silence after a pause
      for (let i = 0; i < SPEC_POINTS; i++) if (postD[i] > 0.004 || preD[i] > 0.004) return true;
    }
    return false;
  }

  function _peakTick() {
    const mode = _mode();
    if (mode === 'idle') { refresh(); return; }
    _readPeak();
    // Reduced motion freezes the spectrum — one representative frame, taken
    // once the taps hold audio — it doesn't hide it.
    if (mode === 'meter' && _vizOn() && !frozenShown && _now() - tapsAt >= FREEZE_AFTER_MS) {
      _readSpectra();
      preD.set(preT); postD.set(postT); loudD = 0;
      _bandLevels();
      frozenShown = true;
      requestDraw();
    }
  }

  /** Re-decide what runs: call on open/close, play state, visibility, settings. */
  function refresh() {
    const mode = _mode();
    if (mode === 'idle') {
      if (peakTimer) { clearInterval(peakTimer); peakTimer = 0; }
    } else if (!peakTimer) {
      peakTimer = setInterval(_peakTick, PEAK_MS);
    }
    if (!_vizOn() && specLive) _clearSpectrum();
    if (mode === 'live') { frozenShown = false; _arm(); return; }
    if (mode === 'meter') { if (rafId && !_animating()) _stopRaf(); requestDraw(); return; }
    // idle
    if (!active) { _stopRaf(); return; }
    if (_animating()) _arm(); else requestDraw();
  }
  function _clearSpectrum() {
    preT.fill(0); postT.fill(0); preD.fill(0); postD.fill(0); levels.fill(0);
    loudT = loudD = 0; specLive = false; frozenShown = false;
  }
  function _arm() { if (!rafId) { lastFrame = 0; rafId = requestAnimationFrame(_tick); } }
  function _stopRaf() { if (rafId) { cancelAnimationFrame(rafId); rafId = 0; } }

  function _tick(ts) {
    rafId = 0;
    const mode = _mode();
    const dt = lastFrame ? Math.min(100, ts - lastFrame) : 16.7;
    lastFrame = ts;
    if (mode === 'live') {
      if (ts - lastRead >= 1000 / READ_HZ - 2) { lastRead = ts; _readSpectra(); }
    } else if (specLive && !frozenShown) {   // fade out after a pause
      preT.fill(0); postT.fill(0); loudT = 0;
    }
    if (mode === 'live' || (specLive && !frozenShown)) {
      // temporal smoothing toward the last read (fast rise, slower fall)
      const up = 1 - Math.pow(1 - 0.45, dt / 16.7), down = 1 - Math.pow(1 - 0.12, dt / 16.7);
      for (let i = 0; i < SPEC_POINTS; i++) {
        const a = postT[i] - postD[i]; postD[i] += a * (a > 0 ? up : down);
        const b = preT[i] - preD[i]; preD[i] += b * (b > 0 ? up : down);
      }
      loudD += (loudT - loudD) * (loudT > loudD ? up * 0.6 : down * 0.6);
      _bandLevels();
      if (mode !== 'live' && !_animating()) _clearSpectrum();
    }
    _draw(ts);
    // _draw may already have re-armed (bypass animation): never queue twice.
    if (!rafId && (mode === 'live' || _animating())) rafId = requestAnimationFrame(_tick);
  }

  function requestDraw() {
    if (rafId || pendingDraw) return;
    pendingDraw = requestAnimationFrame((ts) => { pendingDraw = 0; _draw(ts); });
  }

  // ── drawing ───────────────────────────────────────────────────────────────
  function _draw(ts) {
    if (!W || !H || !renderer) return;
    const t0 = _now();
    if (!theme) readTheme();
    // bypass mix (the curve flattens / comes back)
    if (bypassMix !== bypassTo) {
      const k = _rm() ? 1 : Math.min(1, (_now() - bypassT0) / BYPASS_MS);
      bypassMix = bypassFrom + (bypassTo - bypassFrom) * (1 - Math.pow(1 - k, 3));
      if (k >= 1) bypassMix = bypassTo;
      curveDirty = true;
      if (bypassMix !== bypassTo) _arm();
    }
    if (curveDirty) {
      for (let b = 0; b < nBands; b++) scaled[b] = shown[b] * (1 - bypassMix);
      resp.response(scaled, curveFreqs, curveDb);
      if (bypassMix > 0) resp.response(shown, curveFreqs, ghostDb);
      curveDirty = false;
    }
    if (!gridCanvas) {
      gridCanvas = document.createElement('canvas');
      gridCanvas.width = canvas.width; gridCanvas.height = canvas.height;
      const gg = gridCanvas.getContext('2d');
      gg.setTransform(dpr, 0, 0, dpr, 0, 0);
      renderer.drawGrid(gg, _scene(ts), theme);
    }
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, canvas.width, canvas.height);
    g.drawImage(gridCanvas, 0, 0);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    styleAnim = renderer.drawFrame(g, _scene(ts), theme) === true && active && !document.hidden;
    if (styleAnim) _arm();
    const ms = _now() - t0;
    stats.frames++; stats.drawMs += ms; if (ms > stats.maxDrawMs) stats.maxDrawMs = ms;
  }
  const _bands = defs.map((d) => ({ f: d.freq, gain: 0, x: 0, y: 0, level: 0 }));
  const _sc = { bands: _bands, curveFreqs, curveDb, specFreqs, x: xOf, y: yOf, dbSpan: DB_SPAN };
  function _scene(ts) {
    for (let i = 0; i < nBands; i++) {
      const b = _bands[i];
      b.gain = shown[i]; b.x = bandXOf(i); b.y = yOf(shown[i]); b.level = levels[i];
    }
    _sc.w = W; _sc.h = H; _sc.dpr = dpr; _sc.plot = plot; _sc.dbSpan = span; _sc.layout = lay; _sc.since = since;
    _sc.ghostDb = bypassMix > 0 ? ghostDb : null; _sc.bypassMix = bypassMix;
    _sc.pre = specLive ? preD : null; _sc.post = specLive ? postD : null;
    _sc.loud = loudD; _sc.motion = !_rm(); _sc.viz = _vizOn(); _sc.time = ts || _now();
    _sc.headroomDb = headroomDb; _sc.specRangeDb = SPEC_CEIL - SPEC_FLOOR;
    return _sc;
  }

  // ── wiring (the graph is made once and kept; everything here is idle while
  //    the panel is closed) ──────────────────────────────────────────────────
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => { if (active) layout(); }).observe(stage);
  }
  window.addEventListener('resize', () => { if (active) layout(); });
  const onVis = () => { if (active) refresh(); };
  document.addEventListener('visibilitychange', onVis);
  window.addEventListener('sb:viz-settings', onVis);
  const rmq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  if (rmq && rmq.addEventListener) rmq.addEventListener('change', () => { frozenShown = false; onVis(); });
  new MutationObserver(() => { if (active) { readTheme(); requestDraw(); } else theme = null; })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  return {
    geom, stats, refresh, requestDraw, layout,
    /** Combined response (dB) for any gains (preset icons). */
    response: (gains, freqs) => resp.response(gains, freqs),
    get curveDb() { return curveDb; },
    get mode() { return _mode(false); },
    get running() { return !!rafId; },
    get levels() { return Array.from(levels); },
    setActive(on) {
      active = !!on;
      if (active) {
        since = _now();
        readTheme();
        layout();
      } else {
        _stopRaf();
        if (peakTimer) { clearInterval(peakTimer); peakTimer = 0; }
        if (player.releaseEqTaps) player.releaseEqTaps();
        taps = null;
        _clearSpectrum();
        bypassMix = bypassTo;
        styleAnim = false;
      }
      refresh();
    },
    /** A new player graph (first play): taps are taken again. */
    resetTaps() { taps = null; binMapKey = ''; curveDirty = true; if (active) refresh(); },
    setGains(gains) {
      for (let b = 0; b < nBands; b++) shown[b] = gains[b] || 0;
      curveDirty = true;
      if (!rafId) requestDraw();
    },
    setBypass(on) {
      const to = on ? 1 : 0;
      if (bypassTo === to) return;
      bypassFrom = bypassMix; bypassTo = to; bypassT0 = _now();
      curveDirty = true;
      if (_rm() || !active) { bypassMix = to; requestDraw(); } else _arm();
    },
    setStyle(name) {
      const r = eqStyle(name);
      if (!r) return false;
      renderer = r;
      since = _now();
      readTheme();
      if (active) layout(); else W = H = 0;   // (re)laid out on the next open
      return true;
    },
  };
}
