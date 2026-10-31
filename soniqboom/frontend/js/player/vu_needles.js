// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * player/vu_needles.js — the Retro hi-fi player bar's twin analogue VU meters
 * (L / R), one each side of the transport keys.
 *
 * They stand in for the per-voice VU meter (app.js #vu-meters) on the tracks
 * that have none — ordinary audio (MP3, FLAC, …): they show while ``show()``
 * says so (app.js: such a track is loaded and the Now-playing visualizations
 * are on), the shared UI style is Retro (ui_style.js), the bar is not in radio
 * mode, and both flanks of the transport have room for a meter (from about
 * 1250 px of window width).  A tracker / chip tune keeps its LED columns only.
 *
 * Audio: Player.vuTaps() — one analyser per channel behind a splitter on what
 * is heard (crossfade tail included), connected only while the needles show
 * (releaseVuTaps()).  The samples are read at most every READ_MS and divided by
 * the volume setting (``volume()``), so the needles show the music's level, not
 * the volume knob's — like the per-voice meters.  The needle position is the RMS
 * level on a VU scale (0 VU = REF_DBFS), drawn with meter ballistics: a fast
 * attack (ATTACK_S) and a slower release (RELEASE_S).  The PEAK lamp lights
 * when a sample reaches PEAK_DBFS and holds PEAK_HOLD_MS.  Without Web Audio
 * the needles follow ``fallback()`` (the per-voice levels).
 *
 * One rAF loop, running only while the needles show, the page is visible and
 * motion is not reduced, and while music plays — after a pause it runs on
 * until the needles have fallen back to rest, then stops; a frame whose needles
 * and lamps did not move draws nothing.  Reduced motion: one reading (the
 * average of FROZEN_READS reads) FROZEN_DELAY_MS after the needles show or the
 * play state changes, drawn where it lands (no ballistics) and kept (shown, not
 * hidden); a resize, theme or look change repaints it.  Visualizations off
 * hide them (``show()``).
 *
 *   createVuNeedles({ host, bar, player, fallback, volume, show }) → { sync(), stats, state() }
 *     host      the #vu-needles element (two canvas.vu-needle children)
 *     bar       #player-bar
 *     player    Player (vuTaps / releaseVuTaps / playing / on('statechange'))
 *     fallback  () → [l, r] levels 0…1 when no analyser exists (optional)
 *     volume    () → the volume setting 0…1 (optional; 1 when absent)
 *     show      () → whether the current track wants them (optional; always when absent)
 *   sync() re-decides visibility, placement, the loop and the seek row's span
 *   (the waveform from one meter's outer edge to the other's, _spanSeek); app.js calls it when
 *   the VU meter starts, stops or moves, the module itself on a style, theme,
 *   size, pixel-ratio, radio-mode, playback, visibility or reduced-motion
 *   change.  Hiding waits a microtask: _initVU's stop + rebuild (a sidecar that
 *   arrives mid-track) then keeps the needles where they are.
 */
import { getUiStyle, onUiStyleChange } from '../ui_style.js';
// The meter itself — scale, ballistics, levels, drawing — is shared with the
// phone's Now Playing (vu/needles.js); this module places and runs it here.
import { READ_MS, ASPECT, vuToPos, needleAngle, createNeedleState, readNeedleLevels,
         stepNeedles, drawNeedleFace, drawNeedle } from '../vu/needles.js';

export { vuToPos };

const FROZEN_DELAY_MS = 400;
const FROZEN_READS = 5, FROZEN_GAP_MS = 60;
const MAX_W = 112, MIN_W = 68, GAP = 14;
const SEEK_SIDE = 64;                  // room each side of a spanned seek row: gap + a time window
const _rm = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
const reducedMotion = () => !!(_rm && _rm.matches);

export function createVuNeedles({ host, bar, player, fallback, volume, show }) {
  const cvs = host ? Array.from(host.querySelectorAll('canvas.vu-needle')) : [];
  const ctxs = cvs.map(c => c.getContext('2d'));
  const st = createNeedleState();
  const pos = st.pos, target = st.target, lampUntil = st.lampUntil;
  const faces = [null, null];          // cached scale layer per meter
  let shown = false, raf = 0, lastT = 0, lastRead = 0, frozenT = 0, frozen = false, hidePending = false;
  const drawn = [NaN, NaN], drawnLamp = [null, null];   // what the canvases show
  let taps = null, size = { w: 0, h: 0, dpr: 0 };
  const stats = { frames: 0, drawMs: 0, maxMs: 0, reads: 0 };
  if (!host || cvs.length !== 2) return { sync() {}, stats, state: () => ({}) };
  const progress = bar.querySelector('.player-progress');

  // The meters' slots beside the transport, whether or not the needles show.
  function _slots() {
    if (getUiStyle() !== 'retro') return null;
    if (bar.classList.contains('radio-mode')) return null;
    const ctrls = bar.querySelector('.player-controls');
    const center = bar.querySelector('.player-center');
    if (!ctrls || !center) return null;
    const b = bar.getBoundingClientRect(), k = ctrls.getBoundingClientRect(), c = center.getBoundingClientRect();
    if (!k.width || !c.width) return null;
    const side = Math.min(k.left - c.left, c.right - k.right);
    const w = Math.floor(Math.min(MAX_W, side - 2 * GAP));
    if (w < MIN_W) return null;
    const h = Math.round(w * ASPECT);
    // Positions are inside the bar's padding box (its top border is 1 px).
    const ox = b.left + bar.clientLeft, oy = b.top + bar.clientTop;
    const cy = k.top - oy + k.height / 2;
    return {
      w, h, top: Math.round(cy - h / 2),
      left: [Math.round(k.left - ox - GAP - w), Math.round(k.right - ox + GAP)],
      cw: c.width,
    };
  }

  function _wanted() {
    if (show && !show()) return null;
    return _slots();
  }

  // The seek row spans the meters: the waveform runs from the left meter's
  // outer bezel to the right one's and the time windows sit outside it
  // (.player-progress.span-meters, --pb-span; player-styles.css).  Laid out
  // whenever the slots exist — a chip tune without needles keeps the same row,
  // so the waveform doesn't change width between tracks — and the windows
  // have room beside it; otherwise the row keeps its own layout.
  let spanned = -1;
  function _spanSeek(g) {
    if (!progress) return;
    let span = 0;
    if (g) {
      const s = g.left[1] + g.w - g.left[0] + 4;     // + the 2 px bezels
      if ((g.cw - s) / 2 >= SEEK_SIDE) span = s;
    }
    if (span === spanned) return;
    spanned = span;
    progress.classList.toggle('span-meters', span > 0);
    if (span) progress.style.setProperty('--pb-span', span + 'px');
    else progress.style.removeProperty('--pb-span');
  }

  function _place(g) {
    const dpr = window.devicePixelRatio || 1;
    const resized = size.w !== g.w || size.h !== g.h || size.dpr !== dpr;
    cvs.forEach((c, i) => {
      c.style.left = g.left[i] + 'px';
      c.style.top = g.top + 'px';
      c.style.width = g.w + 'px';
      c.style.height = g.h + 'px';
      if (resized) { c.width = Math.round(g.w * dpr); c.height = Math.round(g.h * dpr); }
    });
    if (resized) { size = { w: g.w, h: g.h, dpr }; faces[0] = faces[1] = null; drawn[0] = drawn[1] = NaN; }
  }

  // ── Drawing (vu/needles.js) ─────────────────────────────────────────────
  function _draw(i) {
    const c = ctxs[i], { w, h, dpr } = size;
    if (!c || !w) return;
    drawn[i] = pos[i];
    if (!faces[i]) faces[i] = drawNeedleFace(w, h, dpr, i);
    const lit = lampUntil[i] > performance.now();
    drawnLamp[i] = lit;
    drawNeedle(c, faces[i], w, h, dpr, pos[i], lit);
  }

  // ── Levels ────────────────────────────────────────────────────────────────
  function _read(now) {
    lastRead = now;
    stats.reads++;
    if (!taps) taps = player.vuTaps ? player.vuTaps() : null;
    if (taps) {
      // The taps hear the volume setting; take it out (silence stays silence).
      const vol = typeof volume === 'function' ? Number(volume()) : 1;
      const gain = vol > 0.001 && Number.isFinite(vol) ? 1 / Math.min(1, vol) : 0;
      readNeedleLevels(taps, st, gain, now);
    } else {
      const lv = typeof fallback === 'function' ? fallback() : null;
      for (let i = 0; i < 2; i++) target[i] = lv ? Math.max(0, Math.min(1, lv[i] || 0)) * 0.85 : 0;
    }
    if (!player.playing) target[0] = target[1] = 0;
  }

  function _frame(now) {
    raf = 0;
    if (!shown || document.hidden || reducedMotion()) return;
    const t0 = performance.now();
    const dt = lastT ? Math.min(0.1, (now - lastT) / 1000) : 0.016;
    lastT = now;
    if (now - lastRead >= READ_MS) _read(now);
    let moving = stepNeedles(st, dt);
    for (let i = 0; i < 2; i++) {
      const lit = lampUntil[i] > now;
      if (lit) moving = true;
      if (Math.abs(pos[i] - drawn[i]) > 0.0005 || lit !== drawnLamp[i] || drawn[i] !== drawn[i]) _draw(i);
    }
    const ms = performance.now() - t0;
    stats.frames++; stats.drawMs += ms; if (ms > stats.maxMs) stats.maxMs = ms;
    if (player.playing || moving) raf = requestAnimationFrame(_frame);
    else { pos[0] = pos[1] = 0; _draw(0); _draw(1); lastT = 0; }
  }

  // Reduced motion: the average of a few reads (one read can land on a beat
  // or a gap), drawn where it lands and kept.
  function _freezeSoon() {
    clearTimeout(frozenT);
    const sum = [0, 0];
    let n = 0;
    const step = () => {
      frozenT = 0;
      if (!shown || !reducedMotion()) return;
      _read(performance.now());
      sum[0] += target[0]; sum[1] += target[1];
      if (++n < FROZEN_READS) { frozenT = setTimeout(step, FROZEN_GAP_MS); return; }
      pos[0] = sum[0] / n; pos[1] = sum[1] / n;
      lampUntil[0] = lampUntil[1] = 0;          // a frozen lamp would read as "clipping now"
      frozen = true;
      _draw(0); _draw(1);
    };
    frozenT = setTimeout(step, FROZEN_DELAY_MS);
  }

  function _stopLoop() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    lastT = 0;
  }

  function _hide() {
    shown = false;
    host.hidden = true;
    _stopLoop();
    clearTimeout(frozenT); frozenT = 0; frozen = false;
    if (player.releaseVuTaps) player.releaseVuTaps();
    taps = null;
    pos[0] = pos[1] = target[0] = target[1] = 0;
    lampUntil[0] = lampUntil[1] = 0;
    drawn[0] = drawn[1] = NaN;
  }

  function sync() {
    _spanSeek(_slots());
    const g = _wanted();
    if (!g) {
      // Decide after the current task: a stop that a rebuild follows at once
      // (_initVU) must not drop the needles to rest.
      if (shown && !hidePending) {
        hidePending = true;
        queueMicrotask(() => {
          hidePending = false;
          if (!shown) return;
          if (_wanted()) sync(); else _hide();
        });
      }
      return;
    }
    const was = shown;
    shown = true;
    host.hidden = false;
    // Connect now: an analyser read right after connecting returns silence
    // (the frozen reduced-motion reading was always "rest").
    if (!taps && player.vuTaps) taps = player.vuTaps();
    _place(g);
    if (reducedMotion()) {
      _stopLoop();
      _draw(0); _draw(1);                       // the frozen reading (or rest) stays
      if (!frozen || !was) _freezeSoon();
      return;
    }
    frozen = false;
    if (!document.hidden && !raf && (player.playing || pos[0] > 0.001 || pos[1] > 0.001)) {
      raf = requestAnimationFrame(_frame);
    } else if (!raf) {
      _draw(0); _draw(1);
    }
  }

  // Re-decide on everything that changes placement, visibility or the loop.
  onUiStyleChange(() => sync());
  new MutationObserver(() => { faces[0] = faces[1] = null; if (shown) { _draw(0); _draw(1); } sync(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  new MutationObserver(() => sync()).observe(bar, { attributes: true, attributeFilter: ['class'] });
  if ('ResizeObserver' in window) {
    const ro = new ResizeObserver(() => sync());
    ro.observe(bar);
    const ctrls = bar.querySelector('.player-controls');
    if (ctrls) ro.observe(ctrls);
  } else {
    window.addEventListener('resize', () => sync());
  }
  if (player.on) {
    player.on('statechange', () => {
      if (reducedMotion()) { if (shown) _freezeSoon(); return; }
      sync();
    });
  }
  document.addEventListener('visibilitychange', () => sync());
  // A move to a screen with another pixel ratio re-renders the faces.
  const watchDpr = () => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    const on = () => { if (mq.removeEventListener) mq.removeEventListener('change', on); sync(); watchDpr(); };
    if (mq.addEventListener) mq.addEventListener('change', on);
  };
  watchDpr();
  if (_rm) {
    const onRm = () => { frozen = false; sync(); };
    if (_rm.addEventListener) _rm.addEventListener('change', onRm);
    else if (_rm.addListener) _rm.addListener(onRm);
  }

  return {
    sync,
    stats,
    /** Debug / tests: the current reading. */
    state: () => ({
      shown, running: !!raf, frozen,
      pos: pos.slice(), target: target.slice(),
      angleDeg: pos.map(p => +(needleAngle(p) * 180 / Math.PI).toFixed(2)),
      lamp: lampUntil.map(t => t > performance.now()),
      taps: !!taps,
    }),
  };
}
