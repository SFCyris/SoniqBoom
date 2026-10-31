// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * stations/live.js — the Stations view's live feedback of the stream itself:
 *
 *   • the scope strip in the now-playing card (canvas.st-scope): Modern = a
 *     scrolling level strip (mirrored bars, newest on the right); Retro hi-fi =
 *     a phosphor oscilloscope trace behind glass, auto-scaled to the stream's
 *     recent peak (a stream's samples peak well under full scale, so drawn 1:1
 *     the 52 px trace was a near-flat line while the Modern strip moved);
 *   • the mini meter on the playing station's card (.st-card-meter: four bars
 *     from four frequency bands);
 *   • the signal meter (.st-signal: bars in Modern, a needle in Retro) — the
 *     stream's health: tuning, buffered seconds ahead, a stall, a rebuffer, a
 *     step down the quality ladder.
 *
 * Audio comes from Player.analyser — already in the output chain, so no node
 * is added or connected; it is only READ while the view is visible.  One rAF
 * loop drives the scope and the mini meter, and only while: the view is shown,
 * the page is visible, a station is playing, Visualizations (now playing) are
 * on, and motion is not reduced.  Paused: the last frame stays (static).
 * Reduced motion: one frozen frame (shown, not hidden; a resize or a look /
 * theme change repaints that same frame).  Visualizations off: the scope is
 * blank and the mini meter hidden (#stations-view.st-viz-off).  The signal meter is a status
 * readout, not a visualization: it updates on a 400 ms timer while a station is
 * on and the view is shown (its needle eases by CSS; instant with reduced
 * motion).
 */
import { vizGroupEnabled } from '../viz/engine.js';

const PUSH_MS = 33;            // the level strip advances ~30 columns a second
const SIGNAL_MS = 400;
const FROZEN_DELAY_MS = 400;   // reduced motion: the one frame waits for audio to arrive
const AHEAD_FULL_S = 3;        // the signal meter is full with this much audio buffered ahead
// Retro trace auto-scale: the recent peak (attack at once, release over
// SCOPE_RELEASE_S) is drawn at SCOPE_FILL of the trace's full scale (±0.44·H);
// quiet passages are lifted by at most SCOPE_MAX_GAIN, so hiss / near-silence
// stays a near-flat line.
const SCOPE_FILL = 0.9;
const SCOPE_MAX_GAIN = 6;
const SCOPE_RELEASE_S = 1.5;

const _rm = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
export const reducedMotion = () => !!(_rm && _rm.matches);

/**
 * @param {object} o
 * @param {object} o.player   Player (analyser, playing, stationMode)
 * @param {HTMLElement} o.view the #stations-view element
 * @param {() => object} o.health  stream health snapshot (see stations.js _health)
 */
export function createLive({ player, view, health }) {
  let raf = 0, frozenT = 0, sigT = 0;
  let scope = null, scopeCtx = null, mini = null, signals = [];
  let td = null, fd = null;
  const HIST = 600;
  let haveTd = false;                    // td holds a real frame (repaints reuse it)
  let frozenFor = null;                  // reduced motion: the station whose one frame is shown
  const hist = new Float32Array(HIST);   // ring of levels 0…1
  let histHead = 0, histLen = 0, lastPush = 0;
  let level = 0;
  let scopePeak = 0, scopePeakT = 0;     // Retro auto-scale envelope (0…1) and when it was fed
  let colors = null;
  const stats = { frames: 0, drawMs: 0, maxMs: 0 };
  let _sizeObs = null;

  const visible = () => !!view && !view.hidden && !document.hidden;
  const on = () => !!(player.stationMode && health().on);
  const style = () => document.documentElement.dataset.uiStyle === 'retro' ? 'retro' : 'modern';

  function _readColors() {
    const cs = getComputedStyle(view);
    const v = (n, d) => (cs.getPropertyValue(n) || '').trim() || d;
    colors = {
      a: v('--st-scope-a', '#f0722a'),
      b: v('--st-scope-b', '#6bc8f0'),
      grid: v('--st-scope-grid', 'rgba(255,255,255,0.08)'),
      trace: v('--st-scope-trace', '#50ff8c'),
      glow: v('--st-scope-glow', 'rgba(80,255,140,0.55)'),
      idle: v('--st-scope-idle', 'rgba(255,255,255,0.18)'),
    };
  }
  // Theme / UI-style changes re-read the canvas colours.
  new MutationObserver(() => { colors = null; if (scope) _paintStatic(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-ui-style'] });

  function _size() {
    if (!scope) return false;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(2, Math.round(scope.clientWidth * dpr));
    const h = Math.max(2, Math.round(scope.clientHeight * dpr));
    if (scope.width !== w || scope.height !== h) { scope.width = w; scope.height = h; return true; }
    return false;
  }

  /** Re-find the elements after the card / grid re-rendered. */
  function bind() {
    const s = view.querySelector('canvas.st-scope');
    if (s !== scope) {
      scope = s;
      scopeCtx = s ? s.getContext('2d') : null;
      if (_sizeObs) _sizeObs.disconnect();
      if (s && window.ResizeObserver) {
        _sizeObs = new ResizeObserver(() => { if (_size()) _paintStatic(); });
        _sizeObs.observe(s);
      }
      _size();
    }
    mini = view.querySelector('.st-card.playing .st-card-meter');
    signals = Array.from(view.querySelectorAll('.st-signal'));
    _paintStatic();
    _signalTick();
    evaluate();
  }

  // ── Audio read ────────────────────────────────────────────────────────────
  function _read() {
    const an = player.analyser;
    if (!an) return null;
    if (!td || td.length !== an.fftSize) td = new Uint8Array(an.fftSize);
    if (!fd || fd.length !== an.frequencyBinCount) fd = new Uint8Array(an.frequencyBinCount);
    an.getByteTimeDomainData(td);
    an.getByteFrequencyData(fd);
    haveTd = true;
    let peak = 0;
    for (let i = 0; i < td.length; i++) { const d = Math.abs(td[i] - 128); if (d > peak) peak = d; }
    peak /= 128;
    _envelope(peak);
    return peak;
  }

  /** Feed the Retro trace's auto-scale: peak hold with an exponential release. */
  function _envelope(v) {
    const now = performance.now();
    const dt = scopePeakT ? Math.max(0, now - scopePeakT) / 1000 : 0;
    scopePeakT = now;
    const held = scopePeak * Math.exp(-dt / SCOPE_RELEASE_S);
    scopePeak = v > held ? v : held;
  }
  function _resetEnvelope() { scopePeak = 0; scopePeakT = 0; }

  function _push(v, now) {
    if (now - lastPush < PUSH_MS && histLen) return;
    lastPush = now;
    hist[histHead] = v;
    histHead = (histHead + 1) % HIST;
    histLen = Math.min(HIST, histLen + 1);
  }

  // ── Drawing ───────────────────────────────────────────────────────────────
  function _drawModern(g, W, H) {
    const dpr = window.devicePixelRatio || 1;
    const bw = 3 * dpr, gap = 2 * dpr, step = bw + gap;
    const cols = Math.floor(W / step);
    const mid = H / 2;
    const grad = g.createLinearGradient(0, 0, W, 0);
    grad.addColorStop(0, colors.b);
    grad.addColorStop(1, colors.a);
    g.fillStyle = colors.grid;
    g.fillRect(0, Math.round(mid - dpr / 2), W, Math.max(1, dpr));
    g.fillStyle = grad;
    const n = Math.min(cols, histLen);
    for (let k = 0; k < n; k++) {
      const v = hist[(histHead - 1 - k + HIST) % HIST];
      const h = Math.max(dpr, Math.min(1, v * 1.15) * (H - 2 * dpr));
      const x = W - (k + 1) * step;
      g.fillRect(x, mid - h / 2, bw, h);
    }
  }

  function _drawRetro(g, W, H, live) {
    const dpr = window.devicePixelRatio || 1;
    // Graticule
    g.strokeStyle = colors.grid;
    g.lineWidth = Math.max(1, dpr);
    g.beginPath();
    for (let i = 1; i < 10; i++) { const x = Math.round((W * i) / 10) + 0.5; g.moveTo(x, 0); g.lineTo(x, H); }
    for (let i = 1; i < 4; i++) { const y = Math.round((H * i) / 4) + 0.5; g.moveTo(0, y); g.lineTo(W, y); }
    g.stroke();
    g.lineWidth = Math.max(1.5, 1.6 * dpr);
    g.strokeStyle = colors.trace;
    g.shadowColor = colors.glow;
    g.shadowBlur = 6 * dpr;
    g.beginPath();
    if (live && td) {
      const n = td.length;
      const gain = Math.min(SCOPE_MAX_GAIN, SCOPE_FILL / Math.max(scopePeak, 1e-3));
      for (let i = 0; i < n; i++) {
        const x = (i / (n - 1)) * W;
        const s = Math.max(-1, Math.min(1, ((td[i] - 128) / 128) * gain));
        const y = H / 2 + s * (H * 0.44);
        if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
      }
    } else {
      g.moveTo(0, H / 2); g.lineTo(W, H / 2);
    }
    g.stroke();
    g.shadowBlur = 0;
  }

  function _draw(live) {
    if (!scope || !scopeCtx) return;
    if (!colors) _readColors();
    const g = scopeCtx, W = scope.width, H = scope.height;
    g.clearRect(0, 0, W, H);
    if (style() === 'retro') _drawRetro(g, W, H, live);
    else _drawModern(g, W, H);
  }

  function _drawMini() {
    if (!mini || !fd) return;
    const bars = mini.children;
    const bands = [[1, 3], [4, 10], [11, 28], [29, 70]];
    for (let k = 0; k < bars.length && k < bands.length; k++) {
      const [a, b] = bands[k];
      let s = 0, c = 0;
      for (let i = a; i <= b && i < fd.length; i++) { s += fd[i]; c++; }
      const v = c ? s / c / 255 : 0;
      bars[k].style.transform = `scaleY(${Math.max(0.12, Math.min(1, v * 1.25)).toFixed(3)})`;
    }
  }
  function _miniStill() {
    if (!mini) return;
    for (const b of mini.children) b.style.transform = '';
  }

  /** Paint without reading audio (a fresh canvas, a resize, viz off, paused). */
  function _paintStatic() {
    view.classList.toggle('st-viz-off', !vizGroupEnabled('nowPlaying'));
    if (!scope || !scopeCtx) return;
    if (!vizGroupEnabled('nowPlaying')) { scopeCtx.clearRect(0, 0, scope.width, scope.height); return; }
    _draw(haveTd && on());               // the last frame again (paused / frozen), else idle
  }

  function _frame(ts) {
    raf = 0;
    if (!_wantLoop()) { evaluate(); return; }
    const t0 = performance.now();
    const v = _read();
    if (v != null) {
      level = level * 0.6 + v * 0.4;
      _push(v, ts);
    }
    _draw(v != null);
    _drawMini();
    const dt = performance.now() - t0;
    stats.frames++; stats.drawMs += dt; if (dt > stats.maxMs) stats.maxMs = dt;
    raf = requestAnimationFrame(_frame);
  }

  function _wantLoop() {
    return visible() && on() && player.playing && vizGroupEnabled('nowPlaying') && !reducedMotion();
  }

  function _stopLoop() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    clearTimeout(frozenT); frozenT = 0;
  }

  /** Decide what runs now (call on any state change). */
  function evaluate() {
    // The signal timer: while a station is on and the view is shown.
    if (visible() && on()) { if (!sigT) sigT = setInterval(_signalTick, SIGNAL_MS); }
    else if (sigT) { clearInterval(sigT); sigT = 0; }
    if (!visible()) { _stopLoop(); return; }
    view.classList.toggle('st-viz-off', !vizGroupEnabled('nowPlaying'));
    if (!vizGroupEnabled('nowPlaying')) {
      _stopLoop();
      if (scopeCtx) scopeCtx.clearRect(0, 0, scope.width, scope.height);
      _miniStill();
      return;
    }
    if (reducedMotion()) {
      // Freeze, don't hide: one frame of the live audio, then still.
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      const sid = player.station ? player.station.sid : null;
      if (on() && frozenFor === sid && haveTd) { _paintStatic(); return; }   // the frame stays
      if (on() && player.playing && !frozenT) {
        frozenT = setTimeout(() => {
          frozenT = 0;
          if (!visible() || !vizGroupEnabled('nowPlaying')) return;
          frozenFor = player.station ? player.station.sid : null;
          _resetEnvelope();              // the one frame is scaled to itself, not to an earlier station
          const v = _read();
          if (v != null) { histLen = 0; for (let i = 0; i < 160; i++) _push(v, lastPush + PUSH_MS); }
          _draw(v != null);
          _drawMini();
        }, FROZEN_DELAY_MS);
      } else if (!on()) { histLen = 0; level = 0; haveTd = false; frozenFor = null; _resetEnvelope(); _paintStatic(); _miniStill(); }
      return;
    }
    if (_wantLoop()) { if (!raf) raf = requestAnimationFrame(_frame); return; }
    _stopLoop();
    if (!on()) { histLen = 0; level = 0; haveTd = false; _resetEnvelope(); _paintStatic(); _miniStill(); }
    // paused: the last frame stays as it is
  }

  // ── Signal meter ──────────────────────────────────────────────────────────
  function signal() {
    const h = health();
    if (!h.on) return { v: 0, text: 'No signal' };
    if (h.rebuffering) return { v: 0.12, text: 'Weak — rebuffering' };
    if (!h.hasPlayed) {
      const v = reducedMotion() ? 0.18 : 0.1 + Math.random() * 0.14;
      return { v, text: 'Tuning…' };
    }
    if (h.waiting) return { v: 0.25, text: 'Weak — the stream stalled' };
    const ahead = Math.max(0, Math.min(1, (h.ahead || 0) / AHEAD_FULL_S));
    let v = 0.45 + 0.55 * ahead - 0.12 * (h.downgrade || 0);
    v = Math.max(0.3, Math.min(1, v));
    const text = v >= 0.75 ? 'Strong' : v >= 0.5 ? 'Good' : 'Fair';
    return { v, text };
  }
  function _signalTick() {
    if (!signals.length) return;
    const s = signal();
    const bars = s.v <= 0 ? 0 : Math.max(1, Math.round(s.v * 5));
    for (const el of signals) {
      el.style.setProperty('--sig', s.v.toFixed(3));
      el.dataset.level = String(bars);
      el.setAttribute('aria-valuenow', String(Math.round(s.v * 100)));
      el.setAttribute('aria-valuetext', `Signal: ${s.text}`);
      el.title = `Signal: ${s.text}`;
    }
  }

  document.addEventListener('visibilitychange', evaluate);
  window.addEventListener('sb:viz-settings', () => { _paintStatic(); evaluate(); });
  if (_rm) { try { _rm.addEventListener('change', evaluate); } catch (_) { _rm.addListener?.(evaluate); } }
  if (view) {
    new MutationObserver(evaluate).observe(view, { attributes: true, attributeFilter: ['hidden'] });
  }

  return {
    bind, evaluate, signal,
    get level() { return level; },
    get running() { return !!raf; },
    get stats() { return { ...stats }; },
    resetStats() { stats.frames = 0; stats.drawMs = 0; stats.maxMs = 0; },
  };
}
