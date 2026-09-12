// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/engine.js — shared lifecycle + scheduling for embedded visualizations.
 *
 * Every embedded viz (scan flow, cache cascade, FTP lanes, signal chain,
 * library galaxy, transcode packets, …) registers a draw callback here
 * instead of running its own ``requestAnimationFrame`` loop.  The engine
 * provides, per the perf advisory:
 *
 *   • ONE shared rAF dispatcher.  Per-component fps caps via timestamp gate
 *     (so the VU can run at 30 fps while a galaxy runs at 60, from one loop).
 *     No idle spin — when nothing is active the rAF chain stops entirely.
 *   • A single IntersectionObserver so off-screen embeds don't draw.
 *   • A single ``visibilitychange`` pause so a backgrounded tab does no work
 *     (and, critically, stops reading the audio AnalyserNode).
 *   • ``prefers-reduced-motion`` handling: registered embeds get ONE
 *     ``freeze()`` call (render a representative static frame) and never
 *     animate.  A live media-query listener flips this at runtime.
 *   • A shared audio-frame sampler: at most one ``getByteTimeDomainData`` +
 *     one ``getByteFrequencyData`` read of the existing ``Player.vuAnalyser``
 *     per sample tick (≤15 Hz), into shared pre-allocated buffers.  This is
 *     the audio-thread-safety rule — multiple now-playing embeds MUST consume
 *     these buffers rather than each calling the analyser (60 Hz analyser
 *     reads demonstrably broke Firefox audio — see app.js VU comment).
 *   • A settings gate (master switch + per-group toggles, persisted to
 *     ``localStorage`` key ``sb_viz_settings``).  Reduced-motion is a hard
 *     override regardless of toggles.
 */

const RM_QUERY = '(prefers-reduced-motion: reduce)';
const SETTINGS_KEY = 'sb_viz_settings';

// ── settings ────────────────────────────────────────────────────────────────
const DEFAULT_SETTINGS = {
  enabled: true,            // master switch
  nowPlaying: true,         // signal chain, CRT mode, VU meter, full-screen visualizer
  admin: true,              // scan flow, cache cascade, FTP lanes, transcode packets
  // The former per-group ``library`` toggle was retired: the Galaxy view owns
  // its animate-vs-list choice in-view (localStorage ``sb_galaxy_view``).  The
  // galaxy still registers with group 'library', so ``vizGroupEnabled('library')``
  // now resolves to the MASTER switch alone (library key absent → not `false`).
};

function loadSettings() {
  let s;
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    s = raw ? { ...DEFAULT_SETTINGS, ...JSON.parse(raw) } : { ...DEFAULT_SETTINGS };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
  // Migration: drop a stored ``library`` key (the toggle is gone) so a user who
  // once disabled it isn't stranded with the galaxy permanently forced to List.
  if (s && 'library' in s) {
    delete s.library;
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch { /* ignore */ }
  }
  return s;
}

let _settings = loadSettings();

export function getVizSettings() { return { ..._settings }; }

export function setVizSettings(patch) {
  _settings = { ..._settings, ...patch };
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(_settings)); } catch { /* ignore */ }
  _reevaluateAll();
  // Notify non-registered consumers that read the settings on their own loop
  // (the player-bar VU meter in app.js is deliberately NOT engine-registered —
  // it reads Player.vuAnalyser directly for audio-thread safety — so it can't
  // be re-gated by _reevaluateAll and listens for this instead).
  try {
    window.dispatchEvent(new CustomEvent('sb:viz-settings', { detail: getVizSettings() }));
  } catch { /* ignore */ }
}

/** True when the master switch is on AND the group is on AND reduced-motion
 *  is not forcing everything off. */
export function vizGroupEnabled(group) {
  if (!_settings.enabled) return false;
  if (group && _settings[group] === false) return false;
  return true;
}

// ── reduced motion ───────────────────────────────────────────────────────────
const _rmMql = window.matchMedia ? window.matchMedia(RM_QUERY) : null;
export function prefersReducedMotion() { return !!(_rmMql && _rmMql.matches); }

// ── registry + dispatcher ─────────────────────────────────────────────────────
let _nextToken = 1;
const _entries = new Map();   // token -> entry
let _rafId = null;
let _io = null;

function _ensureObserver() {
  if (_io || typeof IntersectionObserver === 'undefined') return;
  _io = new IntersectionObserver((records) => {
    for (const rec of records) {
      const token = rec.target.__vizToken;
      const e = token && _entries.get(token);
      if (!e) continue;
      e.onScreen = rec.isIntersecting;
      _sync(e);
    }
  }, { threshold: 0.12 });
}

function _sync(e) {
  const rm = prefersReducedMotion();
  const groupOn = vizGroupEnabled(e.group);
  const run = e.onScreen && !document.hidden && !rm && groupOn && !e.stopped;
  if (run && !e.running) {
    e.running = true;
    e.last = 0;
    _arm();
  } else if (!run && e.running) {
    e.running = false;
  }

  // Two distinct "not running" reasons, with OPPOSITE visual intent:
  //   • The user turned this group (or the master switch) OFF → HIDE the
  //     surface entirely.  "Enable visualizations" unchecked means gone, not
  //     a static ghost frame.  Overlay embeds only (hideWhenOff:true); a
  //     navigable view such as the library galaxy opts out and freezes.
  //   • reduced-motion (group still ON) → keep the surface but FREEZE one
  //     representative static frame, so an accessibility preference doesn't
  //     blank a section the user hasn't asked to remove.
  // The engine only ever touches ``hidden`` for a hideWhenOff host, and only
  // for the group-off case it caused — tracked via ``_engineHidden`` so it can
  // never clobber a hidden state the APP set for its own reasons (e.g. the
  // galaxy view hidden while the user is on another library view).
  if (e.host && e.hideWhenOff) {
    if (!groupOn) {
      if (!e.host.hidden) e.host.hidden = true;
      e._engineHidden = true;
      e._frozenOnce = false;
      return;
    }
    if (e._engineHidden) {
      e.host.hidden = false;                    // reveal only what WE hid
      e._engineHidden = false;
    }
  }
  if (!run && !e._frozenOnce && (rm || !groupOn)) {
    try { e.freeze && e.freeze(); } catch { /* listener isolation */ }
    e._frozenOnce = true;
  }
  if (run) e._frozenOnce = false;
}

function _reevaluateAll() {
  for (const e of _entries.values()) _sync(e);
}

function _arm() {
  if (_rafId != null) return;
  // Any entry running?
  let any = false;
  for (const e of _entries.values()) { if (e.running) { any = true; break; } }
  if (!any) return;
  _rafId = requestAnimationFrame(_tick);
}

function _tick(now) {
  _rafId = null;
  let any = false;
  for (const e of _entries.values()) {
    if (!e.running) continue;
    any = true;
    const interval = 1000 / e.fps;
    if (now - e.last >= interval) {
      const dt = e.last ? Math.min(48, now - e.last) : interval;
      e.last = now;
      try { e.draw(dt, now); } catch (err) { /* listener isolation */ if (window.console) console.warn('viz draw error', err); }
    }
  }
  if (any) _rafId = requestAnimationFrame(_tick);
}

/**
 * Register an embedded visualization.
 * @param {object} opts
 * @param {HTMLElement} opts.host   element observed for on-screen / sizing
 * @param {string} opts.group       'nowPlaying' | 'library' | 'admin'
 * @param {number} [opts.fps=30]    per-component frame cap
 * @param {(dt:number, now:number)=>void} opts.draw   per-frame draw
 * @param {()=>void} [opts.freeze]  render one static frame (reduced-motion / off)
 * @param {boolean} [opts.hideWhenOff=true]  when the user turns this group (or
 *   the master switch) OFF, hide the host entirely.  Set false for a navigable
 *   VIEW (e.g. the library galaxy) that the user opens on purpose — those
 *   freeze a static frame instead of vanishing.
 * @returns {{ unregister: ()=>void, refresh: ()=>void }}
 */
export function registerViz({ host, group, fps = 30, draw, freeze, hideWhenOff = true }) {
  _ensureObserver();
  const token = _nextToken++;
  const entry = {
    token, host, group, fps, draw, freeze, hideWhenOff,
    onScreen: false, running: false, stopped: false, last: 0,
    _frozenOnce: false, _engineHidden: false,
  };
  _entries.set(token, entry);
  if (host) {
    host.__vizToken = token;
    // Test hook: expose the draw fn so harnesses can force a paint without
    // the visibility/IO gate.  Harmless in production (never called there).
    host.__vizDraw = (dt = 16, now = (typeof performance !== 'undefined' ? performance.now() : 0)) => {
      try { draw(dt, now); } catch (e) { /* isolation */ }
    };
    if (_io) _io.observe(host);
    else entry.onScreen = true;   // no IO support → assume visible
  } else {
    entry.onScreen = true;
  }
  // Initial reduced-motion / disabled freeze.
  _sync(entry);
  return {
    unregister() {
      entry.stopped = true;
      entry.running = false;
      if (_io && host) _io.unobserve(host);
      _entries.delete(token);
    },
    refresh() { _sync(entry); },
  };
}

// ── global pause hooks ────────────────────────────────────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    for (const e of _entries.values()) e.running = false;
    _stopAudioSampler();
  } else {
    _reevaluateAll();
  }
});

if (_rmMql) {
  const onRM = () => _reevaluateAll();
  if (_rmMql.addEventListener) _rmMql.addEventListener('change', onRM);
  else if (_rmMql.addListener) _rmMql.addListener(onRM);   // Safari < 14
}

// ── shared audio-frame sampler ────────────────────────────────────────────────
//
// One producer of analyser reads on the now-playing screen.  Consumers call
// ``getAudioFrame()`` every draw; it lazily samples the existing
// ``Player.vuAnalyser`` at most once per ``_AUDIO_SAMPLE_MS`` (15 Hz, matching
// the proven-safe VU cadence) into shared buffers.  Between samples it returns
// the last buffers so a 60 fps oscilloscope re-renders without extra reads.
const _AUDIO_SAMPLE_MS = 66;
let _timeBuf = null;     // Uint8Array — time domain (oscilloscope)
let _freqBuf = null;     // Uint8Array — frequency bins (spectrum/VU)
let _lastSample = 0;
let _audioActive = false;

function _ensureBuffers(analyser) {
  const tlen = analyser.fftSize || 256;
  const flen = analyser.frequencyBinCount || 128;
  if (!_timeBuf || _timeBuf.length !== tlen) _timeBuf = new Uint8Array(tlen);
  if (!_freqBuf || _freqBuf.length !== flen) _freqBuf = new Uint8Array(flen);
}

function _stopAudioSampler() { _audioActive = false; }

/**
 * Return the shared audio buffers, sampling at most once per 66 ms.
 *
 * NOTE: currently UNUSED — the live VU meter reads ``Player.vuAnalyser``
 * directly on its own 15 Hz loop (app.js), and the oscilloscope is a mode of
 * the standalone full-screen ``visualizer.js``.  This is kept as the single
 * safe entry point for any FUTURE engine-registered now-playing canvas embed
 * that needs analyser data, so such embeds share ONE ≤15 Hz read rather than
 * each hammering the analyser (60 Hz reads demonstrably broke Firefox audio).
 * @param {AnalyserNode} analyser  typically ``Player.vuAnalyser``
 * @returns {{time: Uint8Array, freq: Uint8Array}|null}
 */
export function getAudioFrame(analyser) {
  if (!analyser || document.hidden) return null;
  _ensureBuffers(analyser);
  const now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
  if (now - _lastSample >= _AUDIO_SAMPLE_MS) {
    _lastSample = now;
    try {
      analyser.getByteTimeDomainData(_timeBuf);
      analyser.getByteFrequencyData(_freqBuf);
      _audioActive = true;
    } catch { return null; }
  }
  return { time: _timeBuf, freq: _freqBuf };
}

// Small DOM helpers shared by SVG-based embeds.
export const SVGNS = 'http://www.w3.org/2000/svg';
export function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}
export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const rand = (a, b) => a + Math.random() * (b - a);
