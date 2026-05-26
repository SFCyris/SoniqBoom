// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * app.js — Bootstrap: wires all modules together, binds global UI events.
 *
 * IMPORTANT: every import below uses the bare ``./<module>.js`` URL with
 * NO ``?v=`` query string.  Under ES module semantics two URLs that differ
 * only in query string are *different modules* — each gets its own copy
 * of every closure-scoped state (``STATE.user``, ``_track``, ``_handlers``,
 * etc).  The other modules (library.js, queue.js, trackinfo.js, …) all
 * import each other with the bare URL, so app.js must do the same or
 * we end up with two singletons for every module: one initialized via
 * app.js's boot, one read by everyone else.  The visible failure mode
 * was the admin gear demanding re-login because admin.js's Auth instance
 * had ``STATE.user = null`` even after the user signed in — app.js's
 * Auth had STATE.user populated, but admin.js was reading from the
 * other copy.  Same bug recurred for Player (player-bar not updating),
 * which prompted this fix.
 *
 * Cache invalidation is handled at TWO layers above this file:
 *   1. ``app.js?v=N`` in index.html — bumping N forces a fresh fetch
 *      of app.js, which re-runs all top-level evaluation (including
 *      every import here).
 *   2. ``SHELL_VERSION`` in sw.js — bumping wipes the entire SW asset
 *      cache so stale ``./<module>.js`` entries from before the fix
 *      get discarded.
 */
import { Auth }       from './auth.js';
import { Player }     from './player.js';
// Expose Player on the global so the cast picker (a plain non-module
// script — see index.html) can read currentTrackId without becoming
// an ES module itself.  Module-isolation purists may wince; this is
// a deliberate single-call-site escape hatch.
window.SoniqBoom = window.SoniqBoom || {};
window.SoniqBoom.player = Player;
import { Library }    from './library.js';
import { Search }     from './search.js';
import { Visualizer } from './visualizer.js';
import { FolderTree } from './foldertree.js';
import { Admin }      from './admin.js';
import { Equalizer }  from './equalizer.js';
import { TrackInfo }  from './trackinfo.js';
import { Queue }      from './queue.js';
import { Playlist }   from './playlist.js';
import { artPlaceholderEmoji, TRACKER_FORMAT_NAMES, Toast } from './utils.js';
// Expose Toast globally so the classic-script cast picker (cast_picker.js,
// not an ES module) can call ``Toast.info(…)``.  Without this all
// ``if (window.Toast) Toast.x(…)`` guards in cast_picker fall through —
// QA-2 P0 flagged the user-visible result: no codec-choice toast, no
// "stopped casting" confirmation.  Same global-escape-hatch pattern as
// ``window.SoniqBoom.player`` above; documented at that comment.
window.Toast = Toast;

// ── Service Worker — offline-instant shell (PERC-6) ─────────────────────
// Registered at top-level so the SW takes control on the first
// navigation; subsequent loads paint from the precache in <50 ms.
// Skipped on http:// (only) because Service Workers require a secure
// context (https or localhost).  Best-effort — registration failure is
// silent because the app works perfectly without an SW.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .catch(() => { /* SW disabled in this browser — no fallback needed */ });
  });
}

// Gate the rest of app boot on the auth overlay: if there's no valid
// session cookie, Auth.boot() shows the login overlay and the promise
// only resolves once the user signs in (or registers).  Anything below
// the awaited promise runs *after* we have a Auth.user identity.
await Auth.boot();
await Auth.ready;

// ── Player bar UI bindings ────────────────────────────────────────────────────
const btnPlay    = document.getElementById('btn-play');
const btnPrev    = document.getElementById('btn-prev');
const btnNext    = document.getElementById('btn-next');
const btnShuffle = document.getElementById('btn-shuffle');
const btnRepeat  = document.getElementById('btn-repeat');
const seekBar    = document.getElementById('seek-bar');
const volBar     = document.getElementById('volume-bar');
const timeCur    = document.getElementById('time-cur');
const timeDur    = document.getElementById('time-dur');
const playerTitle    = document.getElementById('player-title');
const playerMetaTags = document.getElementById('player-meta-tags');
const playerPathCrumb = document.getElementById('player-path-crumb');
const playerArt      = document.getElementById('player-art');

// ── Waveform canvas ──────────────────────────────────────────────────────────
const waveformCanvas = document.getElementById('waveform-canvas');
const waveformCtx    = waveformCanvas ? waveformCanvas.getContext('2d') : null;
let _waveformData    = null;   // Float array [0..1], 200 values

const WAVE_H = 44; // visual height of waveform — much taller than the 3px seek-bar track

// Cached once per track so the per-tick draw never hits getComputedStyle
// (which forces a synchronous style/layout recalc and was contributing to
// Firefox audio-thread underruns and Chromium oscilloscope stutter).
let _accentColor = '#f0722a';
const _DIM_COLOR = 'rgba(255,255,255,0.32)';
let _lastSplitBar = -1;
let _cachedBarGeom = null; // { w, h, barCount, barW, gap }

function _refreshAccentColor() {
  const v = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  if (v) _accentColor = v;
}

function _resizeWaveformCanvas() {
  if (!waveformCanvas) return;
  const sb = document.getElementById('seek-bar');
  if (!sb) return;
  const sbRect     = sb.getBoundingClientRect();
  const parentRect = sb.parentElement.getBoundingClientRect();
  const dpr        = window.devicePixelRatio || 1;

  // Centre the taller canvas on the seek-bar track
  const topOffset = sbRect.top - parentRect.top - (WAVE_H - sbRect.height) / 2;
  waveformCanvas.style.left   = (sbRect.left - parentRect.left) + 'px';
  waveformCanvas.style.top    = topOffset + 'px';
  waveformCanvas.style.width  = sbRect.width + 'px';
  waveformCanvas.style.height = WAVE_H + 'px';
  waveformCanvas.width  = sbRect.width * dpr;
  waveformCanvas.height = WAVE_H * dpr;
  waveformCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  _cachedBarGeom = null;   // force geometry recompute on next draw
  _lastSplitBar  = -1;
}

function _drawWaveform(pct = 0) {
  if (!waveformCtx || !_waveformData || !_waveformData.length) return;

  // Bar geometry is stable for the lifetime of a track — compute once.
  if (!_cachedBarGeom) {
    const w = parseFloat(waveformCanvas.style.width)  || waveformCanvas.width;
    const h = parseFloat(waveformCanvas.style.height) || waveformCanvas.height;
    const barCount = _waveformData.length;
    const barW     = Math.max(1, w / barCount - 0.5);
    const gap      = (w - barW * barCount) / barCount;
    _cachedBarGeom = { w, h, barCount, barW, gap };
  }
  const { w, h, barCount, barW, gap } = _cachedBarGeom;

  // Convert pct → which bar is the split, and skip redraw if unchanged.
  // timeupdate fires ~4×/sec; on a 400-bar waveform that's still only
  // ~1 bar per tick of movement, so 3 out of every 4 redraws are no-ops.
  const stride    = barW + gap;
  const splitX    = (pct / 100) * w;
  const splitBar  = Math.min(barCount, Math.max(0, Math.floor(splitX / stride) + 1));
  if (splitBar === _lastSplitBar) return;
  _lastSplitBar = splitBar;

  // Two-pass fill, one fillStyle mutation per pass instead of per bar.
  waveformCtx.clearRect(0, 0, w, h);
  const halfH = h * 0.85;
  const yMid  = h / 2;

  waveformCtx.fillStyle = _accentColor;
  for (let i = 0; i < splitBar; i++) {
    const amp  = _waveformData[i];
    const barH = amp * halfH; if (barH < 1) continue;
    waveformCtx.fillRect(i * stride, yMid - barH / 2, barW, barH);
  }
  waveformCtx.fillStyle = _DIM_COLOR;
  for (let i = splitBar; i < barCount; i++) {
    const amp  = _waveformData[i];
    const barH = amp * halfH; if (barH < 1) continue;
    waveformCtx.fillRect(i * stride, yMid - barH / 2, barW, barH);
  }
}

async function _fetchWaveform(trackId) {
  const progressEl = document.querySelector('.player-progress');
  // Clear the OLD track's waveform immediately so the canvas blanks the
  // moment the user clicks a new song — otherwise the prior song's bars
  // stay painted (via the 4Hz timeupdate redraw loop) for the entire
  // fetch+compute round-trip (can be several seconds on a remote DSF).
  // Reads as "the new track inherits the old waveform briefly".  Also
  // resets the bar-geometry + last-split caches so the eventual real
  // data draws cleanly (same reason as the v17 fix when fresh data lands
  // — without resetting, the first paint at the same split position as
  // the prior frame gets no-op-skipped).
  _waveformData  = null;
  _cachedBarGeom = null;
  _lastSplitBar  = -1;
  if (waveformCtx && waveformCanvas) {
    waveformCtx.clearRect(0, 0, waveformCanvas.width, waveformCanvas.height);
  }
  progressEl?.classList.remove('has-waveform');

  // Stash this fetch's trackId so that, if the user advances tracks
  // while we're awaiting the response, the LATE arrival of the prior
  // track's waveform doesn't overwrite the now-current track's data.
  // Compared against the current track at every assignment site below.
  const fetchedFor = trackId;
  const isStillCurrent = () => {
    const cur = (Player.currentTrack && Player.currentTrack.id)
      || (Player.queue && Player.queue[Player.queueIdx] && Player.queue[Player.queueIdx].id);
    return cur === fetchedFor;
  };

  try {
    // ``cache: 'no-cache'`` is intentional — without it the browser's
    // HTTP cache happily reuses the FIRST fetch's response (often the
    // silent-padded reading taken while the in-flight WAV was still
    // partial, or the all-zero placeholder from before the backend
    // self-heal landed) for every subsequent call against the same
    // URL.  ``transcode-ready`` then fires, we hit the endpoint again,
    // and the browser hands us the stale bytes — so the canvas paints
    // the same nothing-burger that was there at track-load.  Manifests
    // as "the waveform updates sometimes but not others" because cache
    // population/eviction is timing-dependent across formats and
    // browsers (Chrome's disk-cache eviction is LRU + size-bound; what
    // gets reused varies per session).  ``no-cache`` forces a
    // revalidation hit; combined with the backend's ``Cache-Control:
    // no-store`` header on this endpoint the body is always fresh.
    const res  = await fetch(`/api/tracks/${trackId}/waveform`,
                             { cache: 'no-cache' });
    if (!isStillCurrent()) return;  // user advanced; discard late response
    if (!res.ok) { _waveformData = null; progressEl?.classList.remove('has-waveform'); return; }
    const data = await res.json();
    if (!isStillCurrent()) return;
    // The waveform endpoint returns two shapes that drifted apart over
    // time:
    //   • First fetch (computed inline)   → `{peaks: [...], rms: [...]}`
    //   • Cached fetch (read from store)  → flat list of RMS values
    // Both should render visibly.  Earlier code path treated _waveformData
    // as a Float array unconditionally, so the dict shape rendered
    // nothing (`_waveformData.length === undefined`) and the dim bars
    // RMS produces on high-dynamic-range tracks (DSD with clipping-loud
    // transients) read as "blocks with gaps" — every quieter chunk
    // normalised to <0.1 of peak became a 1-pixel bar invisible against
    // the dark seek-track.  Normalise to a single representation here:
    // prefer peaks (visually higher and more uniform) when available,
    // fall back to the rms list otherwise.  Apply a √ curve so the
    // small bars are still visible without losing the contrast at peaks.
    let arr = null;
    if (Array.isArray(data.waveform)) {
      arr = data.waveform;
    } else if (data.waveform && typeof data.waveform === 'object') {
      arr = data.waveform.peaks || data.waveform.rms || null;
    }
    if (arr && arr.length) {
      // Square-root curve compresses the dynamic range so a 10:1 peak-to-
      // background ratio renders as ~3.2:1 visually — quiet music stays
      // visible (4-8 px instead of 1 px), loud peaks still stand out.
      // (Same trick Audacity / iZotope / Spotify use for visible
      // waveform display vs analytical RMS.)
      _waveformData = arr.map(v => {
        const n = Math.max(0, Math.min(1, Number(v) || 0));
        return Math.sqrt(n);
      });
    } else {
      _waveformData = null;
    }
    if (_waveformData) {
      progressEl?.classList.add('has-waveform');
      _refreshAccentColor();
      _resizeWaveformCanvas();
      // Invalidate the per-frame no-op-skip caches BEFORE drawing.
      // ``_drawWaveform`` short-circuits when ``splitBar === _lastSplitBar``
      // so consecutive timeupdate ticks at the same play position don't
      // repaint.  After ``_fetchWaveform`` swapped in fresh ``_waveformData``
      // that guard would silently swallow our redraw — the old pixels
      // stayed on the canvas (the silent-padded waveform from the partial
      // in-flight WAV) until something *else* moved the split.  The
      // SACD/DSF transcode-ready refresh hit this every time: new data
      // loaded, split still at the same position as the prior frame,
      // ``return`` before the new bars hit the canvas.  Resetting both
      // caches and using the current seek-bar position as the split point
      // forces a clean repaint at the correct play position.
      _cachedBarGeom = null;
      _lastSplitBar  = -1;
      const curPct = parseFloat(seekBar.value) || 0;
      _drawWaveform(curPct);
    } else {
      progressEl?.classList.remove('has-waveform');
    }
  } catch {
    _waveformData = null;
    progressEl?.classList.remove('has-waveform');
  }
}

// Debounce ``resize`` via rAF: the event fires many times per drag (often
// once per pixel) and each call to ``_resizeWaveformCanvas`` forces a
// layout read + DPR-scaled canvas rebuild.  Coalesce to a single pass per
// frame.
let _resizePending = false;
window.addEventListener('resize', () => {
  if (!_waveformData) return;
  if (_resizePending) return;
  _resizePending = true;
  requestAnimationFrame(() => {
    _resizePending = false;
    _resizeWaveformCanvas();
    _drawWaveform(parseFloat(seekBar.value));
  });
});

// ── VU Meters for tracker/module playback ─────────────────────────────────────
const vuContainer = document.getElementById('vu-meters');
let _vuAnalyser = null;
let _vuBars = [];
let _vuAnimFrame = null;
let _vuChannelCount = 0;

// Re-use the shared tracker format set (plus SID for VU meters)
const _TRACKER_FORMATS = new Set([...TRACKER_FORMAT_NAMES, 'SID']);

// VU draw cadence — pinned to 15 Hz instead of the browser's
// requestAnimationFrame default (60 Hz).  The earlier 60 Hz draw was
// what triggered Firefox audio underruns: the main thread spending
// time per-frame inside ``getByteFrequencyData`` while the audio thread
// was also reading from the analyser starved the pipeline.  15 Hz looks
// identical to the eye for a VU meter and frees the budget back up.
const _VU_INTERVAL_MS = 66;
let _vuDrawTimer = null;
let _vuBuffer = null;
let _vuRunning = false;

function _initVU(channelCount) {
  if (!vuContainer) return;
  _stopVU();

  _vuChannelCount = channelCount || 4;
  vuContainer.innerHTML = '';
  vuContainer.hidden = false;
  _vuBars = [];

  for (let i = 0; i < _vuChannelCount; i++) {
    const bar = document.createElement('div');
    bar.className = 'vu-bar';
    bar.style.setProperty('--vu-hue', `${(i * 360 / _vuChannelCount) + 15}`);
    vuContainer.appendChild(bar);
    _vuBars.push(bar);
  }

  // Prefer the dedicated zero-smoothing tap (Player.vuAnalyser) — its
  // ``smoothingTimeConstant`` is 0 so the bars react frame-by-frame
  // with no decay tail.  Fall back to the shared analyser if for some
  // reason the dedicated tap wasn't created.
  try {
    _vuAnalyser = Player.vuAnalyser || Player.analyser || null;
    if (!_vuAnalyser) {
      // No audio context yet (e.g. autoplay-blocked) — fall back to
      // pure CSS animation so the row still feels alive.
      _vuBars.forEach(bar => bar.classList.add('vu-animated'));
      return;
    }
  } catch (e) {
    _vuBars.forEach(bar => bar.classList.add('vu-animated'));
    return;
  }

  // Pre-allocate the buffer once per analyser.  The size never changes
  // for a given analyser node — only re-alloc if the analyser itself
  // changed (e.g. Player rebuilt its graph between tracks).
  if (!_vuBuffer || _vuBuffer.length !== _vuAnalyser.frequencyBinCount) {
    _vuBuffer = new Uint8Array(_vuAnalyser.frequencyBinCount);
  }
  // Self-rescheduling setTimeout chain — exits when document.hidden
  // becomes true (no setInterval ticking pointlessly in a hidden tab,
  // chewing CPU + draining battery).  ``visibilitychange`` re-arms the
  // chain so it resumes naturally when the tab comes back.
  _vuRunning = true;
  _scheduleNextVU();
}

function _scheduleNextVU() {
  if (!_vuRunning) return;
  if (document.hidden) {
    // Stop the chain — visibilitychange below will restart it.
    _vuDrawTimer = null;
    return;
  }
  _vuDrawTimer = setTimeout(() => {
    _drawVU();
    _scheduleNextVU();
  }, _VU_INTERVAL_MS);
}

function _drawVU() {
  if (!_vuAnalyser || !_vuBars.length || !_vuBuffer) return;
  if (document.hidden) return;   // tab not visible — skip the work entirely
  _vuAnalyser.getByteFrequencyData(_vuBuffer);
  const bufLen = _vuBuffer.length;
  const binsPerChannel = Math.floor(bufLen / _vuChannelCount);
  for (let ch = 0; ch < _vuChannelCount; ch++) {
    const start = ch * binsPerChannel;
    const end = start + binsPerChannel;
    let sum = 0;
    for (let i = start; i < end && i < bufLen; i++) sum += _vuBuffer[i];
    const avg = sum / binsPerChannel / 255;
    const level = Math.pow(avg, 0.7);
    // No smoothing — bars snap to the current frame's value in both
    // directions.  Some baseline smoothing still comes from the
    // upstream AnalyserNode's smoothingTimeConstant; if you want the
    // bars even more raw, drop that to 0 in the player.
    _vuBars[ch].style.setProperty('--vu-level', level.toFixed(3));
  }
}

// Resume the VU draw chain when the tab regains visibility.  We only
// re-arm if a draw was previously running — _vuRunning is the flag
// _initVU set and _stopVU clears.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && _vuRunning && !_vuDrawTimer) _scheduleNextVU();
});

function _stopVU() {
  _vuRunning = false;
  if (_vuDrawTimer) {
    clearTimeout(_vuDrawTimer);
    _vuDrawTimer = null;
  }
  if (_vuAnimFrame) {
    cancelAnimationFrame(_vuAnimFrame);
    _vuAnimFrame = null;
  }
  // We no longer create a private analyser, so nothing to disconnect.
  // Keep the cleanup defensive in case an older code path still owned
  // a node (e.g. saved session, hot-reload during dev).
  if (_vuAnalyser && typeof _vuAnalyser.disconnect === 'function'
      && _vuAnalyser !== Player.analyser) {
    try { _vuAnalyser.disconnect(); } catch (_) {}
  }
  if (vuContainer) {
    vuContainer.hidden = true;
    vuContainer.innerHTML = '';
  }
  _vuBars = [];
  _vuAnalyser = null;
  _vuBuffer = null;
}

/** Init the VU meters for tracker / SID formats; tear down otherwise. */
function _handleVU(track) {
  if (!track) { _stopVU(); return; }
  // ``_TRACKER_FORMATS`` stores mixed-case names ("ProTracker",
  // "ScreamTracker 3", "SID"…) so we match the raw format string, not
  // an upper-cased version.  Strip the "AAC/M4A"-style slash labels
  // first by taking the primary token.
  const primary = String(track.format || '').split('/')[0].trim();
  if (!_TRACKER_FORMATS.has(primary)) { _stopVU(); return; }
  // Channel count: track.channels is the file's channel count for
  // trackers (set by openmpt123 metadata extract); fall back to 4 for
  // SID + classic ProTracker.
  const ch = Math.max(1, Math.min(32, Number(track.channels) || 4));
  _initVU(ch);
}

// Optimistic Play: flip the button icon the moment the user clicks,
// before audio.play() resolves.  Card et al. 1983: <100 ms gives the
// "I caused this" perception; the actual play() round-trip is often
// 100–400 ms, well above that threshold.  Player.statechange will
// reconcile if play() rejects (e.g. autoplay block).
//
// Watchdog: if statechange{playing:true} hasn't arrived within 400ms of
// an optimistic flip to "pause", snap the icon back, shake the button,
// and surface an inline error pill so the user knows the request didn't
// stick (autoplay block, missing source, decode error).
let _playWatchdogTimer = null;
let _playWatchdogExpectPlaying = false;

function _clearPlayWatchdog() {
  if (_playWatchdogTimer) { clearTimeout(_playWatchdogTimer); _playWatchdogTimer = null; }
  _playWatchdogExpectPlaying = false;
}

function _snapPlayIconBack() {
  // Snap back to "play" glyph — the watchdog only triggers when we
  // optimistically expected ``playing:true`` but it never arrived.
  btnPlay.innerHTML = '&#9654;';
  btnPlay.title = 'Play';
  // Shake — CSS may animate ``.shake``; we remove it after 500ms regardless.
  btnPlay.classList.add('shake');
  setTimeout(() => btnPlay.classList.remove('shake'), 500);
  // Inline error pill below the player title.
  const titleEl = document.getElementById('player-title');
  if (titleEl && !document.getElementById('play-error')) {
    const pill = document.createElement('span');
    pill.id = 'play-error';
    pill.textContent = 'Playback failed';
    pill.style.cssText = (
      'display:inline-block;margin-left:8px;padding:2px 8px;'
      + 'background:#7a1f1f;color:#fff;border-radius:999px;font-size:11px;'
      + 'opacity:0;transition:opacity 180ms;'
    );
    titleEl.insertAdjacentElement('afterend', pill);
    requestAnimationFrame(() => { pill.style.opacity = '1'; });
    setTimeout(() => {
      pill.style.opacity = '0';
      setTimeout(() => pill.remove(), 220);
    }, 2200);
  }
}

btnPlay.addEventListener('click', () => {
  const currentlyPaused = btnPlay.innerHTML.trim().startsWith('&#9654;') ||
                           btnPlay.innerHTML.includes('▶');
  // Pre-paint: snap to the *opposite* of the visible state.  CSS transition
  // (if any) starts immediately; the canonical statechange fires later.
  if (currentlyPaused) {
    btnPlay.innerHTML = '&#9646;&#9646;';
    btnPlay.title = 'Pause';
    // Arm the watchdog: we expect statechange{playing:true} within 400ms.
    _clearPlayWatchdog();
    _playWatchdogExpectPlaying = true;
    _playWatchdogTimer = setTimeout(() => {
      if (_playWatchdogExpectPlaying) _snapPlayIconBack();
      _clearPlayWatchdog();
    }, 400);
  } else {
    btnPlay.innerHTML = '&#9654;';
    btnPlay.title = 'Play';
    _clearPlayWatchdog();
  }
  Player.playPause();
});
btnPrev.addEventListener('click',  () => Player.prev());
btnNext.addEventListener('click',  () => Player.next());
volBar.addEventListener('input',   () => Player.setVolume(parseFloat(volBar.value)));

// 'change' fires on mouse-up after dragging — and also on plain clicks
// without a drag.  We also bind ``pointerup`` for the edge case where a
// browser doesn't fire ``change`` on a no-movement click, but we dedupe
// via ``_lastSeekPct`` so identical values don't double-fire Player.seek.
let _lastSeekPct = NaN;
function _commitSeek() {
  const pct = parseFloat(seekBar.value);
  if (pct === _lastSeekPct) return;   // dedupe — same value just arrived
  _lastSeekPct = pct;
  Player.seek(pct);
}
seekBar.addEventListener('change', _commitSeek);
seekBar.addEventListener('pointerup', _commitSeek);

btnShuffle.addEventListener('click', () => {
  btnShuffle.classList.toggle('on', Player.toggleShuffle());
});
// Per-mode glyph + label so the user can read the repeat state at a
// glance.  Previously the icon stayed identical for all three modes and
// only the tooltip differed (UX/UI #1 #4).
const _REPEAT_GLYPHS = {
  none: '↻',   // ↻ unfilled circular arrow
  all:  '\u{1F501}',// 🔁 loop
  one:  '\u{1F502}',// 🔂 loop with "1" overlay
};
function _renderRepeatBtn(mode) {
  btnRepeat.classList.toggle('on', mode !== 'none');
  btnRepeat.textContent = _REPEAT_GLYPHS[mode] || _REPEAT_GLYPHS.none;
  btnRepeat.title = { none: 'Repeat off', all: 'Repeat all', one: 'Repeat one' }[mode];
  btnRepeat.setAttribute('aria-label',
    { none: 'Toggle repeat (currently off)',
      all:  'Toggle repeat (currently: repeat all)',
      one:  'Toggle repeat (currently: repeat one)' }[mode],
  );
}
btnRepeat.addEventListener('click', () => {
  _renderRepeatBtn(Player.toggleRepeat());
});
// Seed initial state from the player's saved value (next tick — the
// player module hasn't necessarily finished restoring localStorage by
// the time this script runs).
Promise.resolve().then(() => {
  try { _renderRepeatBtn(Player.repeatMode || 'none'); } catch {}
});

// ── Player callbacks ──────────────────────────────────────────────────────────
// timeupdate fires ~4Hz. We coalesce all DOM writes into a single rAF pass
// per tick and skip the write entirely when the tab is hidden (nothing to
// see). Also skip string/DOM churn when the user-visible value hasn't
// actually changed — seek-bar integer pct only changes a few times per track.
let _tuPending = false;
let _tuLast = { curStr: '', durStr: '', pct: -1 };
let _tuLatest = null;

function _applyTimeUpdate() {
  _tuPending = false;
  if (!_tuLatest) return;
  const { current, duration, pct } = _tuLatest;
  const curStr = Player.fmt(current);
  const durStr = Player.fmt(duration);
  const pctRounded = Math.round(pct * 10) / 10;  // 0.1% granularity — plenty

  if (curStr !== _tuLast.curStr) { timeCur.textContent = curStr; _tuLast.curStr = curStr; }
  if (durStr !== _tuLast.durStr) { timeDur.textContent = durStr; _tuLast.durStr = durStr; }
  if (pctRounded !== _tuLast.pct) {
    seekBar.value = pct;
    // CSS rule on #seek-bar now uses ``linear-gradient(... var(--pct))``
    // so the JS side only needs to set the custom property each tick.
    // The browser re-evaluates the same gradient with the new variable
    // (no per-tick style-string churn).  See app.css ``#seek-bar``
    // background rule added in the same regression-fix pass.
    seekBar.style.setProperty('--pct', pct + '%');
    _tuLast.pct = pctRounded;
  }
  _drawWaveform(pct);  // internally skips when split-bar hasn't moved
}

Player.on('timeupdate', (payload) => {
  _tuLatest = payload;
  if (document.hidden) return;             // nothing to render
  if (_tuPending) return;                  // already queued for next frame
  _tuPending = true;
  requestAnimationFrame(_applyTimeUpdate);
});

Player.on('statechange', ({ playing }) => {
  btnPlay.innerHTML = playing ? '&#9646;&#9646;' : '&#9654;';
  btnPlay.title = playing ? 'Pause' : 'Play';
  // Cancel the optimistic-play watchdog — if we asked for ``playing:true``
  // and it arrived, no need to shake/snap.  Also clear on ``playing:false``
  // because the user's intent has been reconciled either way.
  if (playing) _clearPlayWatchdog();
});

// ── Build "Album Artist: X · Artist: Y" meta-tags line ──────────────────────
// Constructed via createElement + replaceChildren — skips the innerHTML
// parser round-trip (HTML string → tokenize → parse → DOM tree) and lets
// the browser go straight to layout.  Also lets us attach click handlers
// directly without a re-query.
function _buildMetaTags(track) {
  const aa = (track.album_artist || '').trim();
  const ar = (track.artist || '').trim();
  if (!aa && !ar) { playerMetaTags.replaceChildren(); return; }

  function _makeItem(labelText, type, name) {
    const span = document.createElement('span');
    span.className = 'meta-tags-item';
    const lab = document.createElement('span');
    lab.className = 'meta-tags-label';
    lab.textContent = labelText;
    const link = document.createElement('a');
    link.className = 'meta-link';
    link.href = '#';
    link.dataset.type = type;
    link.dataset.name = name;
    link.title = `Browse ${name}`;
    link.textContent = name;
    link.addEventListener('click', (e) => {
      e.preventDefault();
      if (type === 'album_artist') Library.showAlbums(null, name, 'album_artist');
      else Library.showAlbums(name, null, 'artist');
    });
    span.appendChild(lab);
    span.appendChild(document.createTextNode(' '));
    span.appendChild(link);
    return span;
  }
  function _makeSep() {
    const sep = document.createElement('span');
    sep.className = 'meta-tags-sep';
    sep.textContent = '·';
    return sep;
  }

  const children = [];
  if (aa) children.push(_makeItem('Album Artist:', 'album_artist', aa));
  if (ar && ar !== aa) {
    if (children.length) children.push(_makeSep());
    children.push(_makeItem('Artist:', 'artist', ar));
  }
  playerMetaTags.replaceChildren(...children);
}

// ── Build "Playing Now: /path/ > folder > folder" breadcrumb ─────────────────
function _buildPathCrumb(track) {
  const raw = track.path || '';
  // Strip ZIP virtual path (outer.zip::member → show the zip's directory)
  const fsPath = raw.includes('::') ? raw.split('::')[0] : raw;
  const parts = fsPath.split('/').filter(Boolean);

  if (!parts.length) { playerPathCrumb.replaceChildren(); return; }

  // Build cumulative paths for each segment
  let cumulative = '';
  const segments = [];
  for (const part of parts) {
    cumulative += '/' + part;
    segments.push({ label: part, path: cumulative });
  }

  // Last segment is the filename — show without a link
  const fileSegment = segments.pop();

  const children = [];
  const label = document.createElement('span');
  label.className = 'crumb-label';
  label.textContent = 'Playing Now:';
  children.push(label);
  children.push(document.createTextNode(' '));

  segments.forEach((seg, idx) => {
    if (idx > 0) {
      const sep = document.createElement('span');
      sep.className = 'crumb-sep';
      sep.textContent = '›';
      children.push(sep);
    }
    const a = document.createElement('a');
    a.className = 'crumb-link';
    a.href = '#';
    a.dataset.path = seg.path;
    a.title = seg.path;
    a.textContent = seg.label;
    a.addEventListener('click', (e) => {
      e.preventDefault();
      Library.showFolder(seg.path);
    });
    children.push(a);
  });

  if (segments.length) {
    const sep = document.createElement('span');
    sep.className = 'crumb-sep';
    sep.textContent = '›';
    children.push(sep);
  }
  const fileEl = document.createElement('span');
  fileEl.className = 'crumb-file';
  fileEl.textContent = fileSegment.label;
  children.push(fileEl);

  playerPathCrumb.replaceChildren(...children);
}

// Defer meta-tags + path-crumb rendering to idle time — neither is on the
// critical audible path; the user hears audio before they read these.
// Falls back to setTimeout(…, 0) where requestIdleCallback is missing
// (Safari < 17, older Firefox forks).
const _ric = (cb) => {
  if (typeof window.requestIdleCallback === 'function') {
    return window.requestIdleCallback(cb, { timeout: 250 });
  }
  return setTimeout(cb, 0);
};

function _escHtml(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function _escAttr(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
}

Player.on('trackchange', (track) => {
  playerTitle.textContent  = track.title || '—';
  document.title = `${track.title || 'SoniqBoom'} — SoniqBoom`;
  // Defer non-audible meta/crumb updates to idle time so they don't
  // compete with audio-pipeline work on the main thread.
  _ric(() => {
    _buildMetaTags(track);
    _buildPathCrumb(track);
  });

  // Always try the art API — it extracts embedded + folder art lazily.
  // Only fall back to the placeholder emoji if the API returns 404.
  // ``decoding="async"`` + ``img.decode()`` ensures the image is fully
  // decoded *off* the main thread before we commit to display — no
  // half-painted flash on track change, GPU-paint only at insert time.
  {
    const artSrc = track.cover_art || `/api/art/${track.id}?size=sm`;
    // Preload + decode the cover image off the main thread.  The decoded
    // resource enters the browser's HTTP cache, so the subsequent
    // ``background-image: url(...)`` assignments on the ambient-glow
    // elements pick up the same bytes without re-fetching.  We can't
    // hand a decoded image to a background-image directly — that's a
    // limitation of CSS background images — but the cache hit avoids
    // the duplicate network round-trip and the background-image paint
    // can use the already-decoded pixels.
    const img = new Image();
    img.decoding = 'async';
    img.onload = async () => {
      try {
        if (typeof img.decode === 'function') await img.decode();
      } catch (_) { /* decode unsupported in older browsers — fine, onload already done */ }
      playerArt.innerHTML = '';
      playerArt.appendChild(img);
      img.alt = 'cover';
      // Ambient art background glow in player bar — uses the now-cached
      // resource so this is a paint-only operation, not a fresh fetch.
      const bg = document.getElementById('player-art-bg');
      bg.style.backgroundImage = `url("${artSrc}")`;
      bg.classList.add('active');
      // Sidebar ambient glow
      const sg = document.getElementById('sidebar-glow');
      if (sg) { sg.style.backgroundImage = `url("${artSrc}")`; sg.classList.add('active'); }
      // Header ambient glow
      const hg = document.getElementById('header-glow');
      if (hg) { hg.style.backgroundImage = `url("${artSrc}")`; hg.classList.add('active'); }
    };
    img.onerror = () => {
      playerArt.innerHTML = `<span class="art-placeholder">${artPlaceholderEmoji(track)}</span>`;
      const bg = document.getElementById('player-art-bg');
      bg.classList.remove('active');
      const sg = document.getElementById('sidebar-glow');
      if (sg) sg.classList.remove('active');
      const hg = document.getElementById('header-glow');
      if (hg) hg.classList.remove('active');
    };
    img.src = artSrc;
  }

  // Waveform — fetch and render behind seek bar
  _fetchWaveform(track.id);

  // VU meters — show for tracker/module formats
  _handleVU(track);
});

// PERC-9: when a transcode finishes, the waveform we fetched at
// trackchange was computed off the partial in-flight WAV (which only
// had a few seconds of real PCM and silence-padding for the rest).
// Re-fetch from the now-cached complete WAV so the overlay matches
// what's actually playing.  Player.js fires this event the first time
// the transcode-status endpoint reports ``ready: true``.
Player.on('transcode-ready', ({ trackId }) => {
  const cur = Player.currentTrack || (Player.queue && Player.queue[Player.queueIdx]);
  // Guard against late events from a prior track (user already advanced).
  if (cur && cur.id === trackId) {
    _fetchWaveform(trackId);
  }
});

// ── Now Playing large art display ────────────────────────────────────────
const npArt      = document.getElementById('now-playing-art');
const npArtImg   = document.getElementById('now-playing-art-img');
const npTitle    = document.getElementById('np-title');
const npArtistEl = document.getElementById('np-artist');
const npAlbum    = document.getElementById('np-album');

// Click small art thumbnail to open the full song overview (Track Info
// overlay) — same behaviour as the toolbar Track Info button.  Older
// builds toggled the large-art splash here; users found the splash less
// useful than the full metadata + lyrics view.
playerArt.style.cursor = 'pointer';
playerArt.title = 'Song overview';
playerArt.setAttribute('role', 'button');
playerArt.setAttribute('tabindex', '0');
playerArt.setAttribute('aria-label', 'Open song overview');
function _openSongOverview() {
  if (!Player.currentTrack) return;
  const q   = Player.queue;
  const idx = Player.queueIdx;
  if (q.length > 0 && idx >= 0) TrackInfo.open(q, idx);
  else                          TrackInfo.openSingle(Player.currentTrack);
}
playerArt.addEventListener('click', _openSongOverview);
playerArt.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openSongOverview(); }
});

// Click large art to dismiss
if (npArt) npArt.addEventListener('click', () => { npArt.hidden = true; });

function _showNowPlayingArt(track) {
  if (!track) return;
  const src = track.cover_art || `/api/art/${track.id}?size=lg`;
  // Reset to placeholder state, fade in real art when it decodes, drop
  // the src on error so the browser's broken-image glyph never paints.
  // (Previously a 404 left the broken-image icon on the dialog
  // permanently until the next track loaded successfully.)
  npArtImg.classList.remove('loaded');
  npArtImg.onload  = () => npArtImg.classList.add('loaded');
  npArtImg.onerror = () => {
    npArtImg.removeAttribute('src');
    npArtImg.classList.remove('loaded');
  };
  npArtImg.src = src;
  npTitle.textContent = track.title || '';
  npArtistEl.textContent = track.album_artist || track.artist || '';
  npAlbum.textContent = track.album || '';
  npArt.hidden = false;
}

// Update info when track changes (if art is visible)
Player.on('trackchange', (track) => {
  if (!npArt.hidden) _showNowPlayingArt(track);
});

volBar.value = parseFloat(localStorage.getItem('sb_volume') ?? '0.8');

// Restore the user's prior pre-mute volume across sessions — used by
// both the keyboard ``M`` shortcut and the glyph mute toggle below.
// Previously defined later; hoisted so the mute IIFE can read it.
let _prevVolume = parseFloat(localStorage.getItem('sb_prev_volume')) || 0.8;

// ── Volume glyph mute toggle ────────────────────────────────────────────────
// The 🔉 glyph immediately preceding the slider becomes a clickable mute
// affordance — discoverable without keyboard shortcut, swaps to 🔇 when
// muted.  Restores the previous (non-zero) volume on unmute.
(() => {
  const glyph = volBar.previousElementSibling;
  if (!glyph) return;
  // Make it interactive without a CSS dependency.
  glyph.style.cursor = 'pointer';
  glyph.setAttribute('role', 'button');
  glyph.setAttribute('tabindex', '0');
  glyph.setAttribute('aria-label', 'Mute / unmute');
  const _syncGlyph = () => {
    const v = parseFloat(volBar.value);
    // Only swap glyph when it's a speaker icon we recognise — preserves any
    // custom styled icon the CSS may have added.
    if (glyph.textContent === '🔉' || glyph.textContent === '🔇') {
      glyph.textContent = (v <= 0) ? '🔇' : '🔉';
    }
    glyph.title = (v <= 0) ? `Unmute (${Math.round(_prevVolume * 100)}%)`
                            : `Mute (currently ${Math.round(v * 100)}%)`;
  };
  const _toggleMute = () => {
    const cur = parseFloat(volBar.value);
    if (cur > 0) {
      // Save current pre-mute volume so we restore precisely on unmute.
      _prevVolume = cur;
      try { localStorage.setItem('sb_prev_volume', String(cur)); } catch {}
      volBar.value = 0;
      Player.setVolume(0);
    } else {
      const restore = _prevVolume > 0 ? _prevVolume : 0.8;
      volBar.value = restore;
      Player.setVolume(restore);
    }
    _syncGlyph();
  };
  glyph.addEventListener('click', _toggleMute);
  glyph.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggleMute(); }
  });
  // Keep the icon in sync as the slider moves.
  volBar.addEventListener('input', _syncGlyph);
  _syncGlyph();
})();

// ── Sidebar navigation ────────────────────────────────────────────────────────
const views = {
  all:           () => Library.showAll(),
  artists:       () => Library.showArtists(),
  album_artists: () => Library.showAlbumArtists(),
  albums:        () => Library.showAlbums(),
  genres:        () => Library.showGenres(),
  years:         () => Library.showYears(),
};

// Smart playlist views (fetched from /api/smart/* endpoints)
const smartViews = {
  'history':        () => Library.showSmart('history',        'Listening History'),
  'most-played':    () => Library.showSmart('most-played',    'Most Played'),
  'recently-added': () => Library.showSmart('recently-added', 'Recently Added'),
  'top-rated':      () => Library.showSmart('top-rated',      'Top Rated'),
  'unplayed':       () => Library.showSmart('unplayed',       'Unplayed'),
  'duplicates':     () => Library.showDuplicates(),
};

/** Deactivate all sidebar nav items across all sections. */
function _deactivateAllNav() {
  document.querySelectorAll('#nav-library li, #nav-smart li').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tree-node.active').forEach(n => n.classList.remove('active'));
}

// Keyboard activation for the role="link" sidebar entries — without this,
// Tab + Enter does nothing because the click handler bound here is the only
// activation path (no native <a href>).
function _bindNav(rootSel, viewMap) {
  document.querySelectorAll(`${rootSel} li`).forEach(li => {
    const activate = () => {
      _deactivateAllNav();
      li.classList.add('active');
      // ``aria-current="page"`` is the standard signal screen readers use
      // for the active nav item.  Clear it from every sibling first, then
      // set on the chosen one.
      document.querySelectorAll('#nav-library li, #nav-smart li').forEach(
        el => el.removeAttribute('aria-current'),
      );
      li.setAttribute('aria-current', 'page');
      const view = li.dataset.view;
      if (viewMap[view]) viewMap[view]();
      // Remember the last-active view across reloads so power-users land
      // back where they were (UX/UI #1 #17).
      try {
        localStorage.setItem('sb_last_view', JSON.stringify({
          section: rootSel === '#nav-library' ? 'library' : 'smart',
          view,
        }));
      } catch {}
    };
    li.addEventListener('click', activate);
    li.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        // Stop the document keydown handler below from also seeing this —
        // otherwise Space here toggles playback and Enter triggers
        // Library.playFocused() in addition to switching the view.
        e.stopPropagation();
        activate();
      }
    });
  });
}

_bindNav('#nav-library', views);
_bindNav('#nav-smart',   smartViews);

// Restore the last-active view (UX/UI #1 #17) — Library.showAll runs at
// boot below, but if the user was on a different view at last reload we
// switch to it after the initial render so they land where they were.
try {
  const saved = JSON.parse(localStorage.getItem('sb_last_view') || 'null');
  if (saved && saved.view && saved.view !== 'all') {
    const sel = saved.section === 'smart'
      ? `#nav-smart li[data-view="${saved.view}"]`
      : `#nav-library li[data-view="${saved.view}"]`;
    // Defer to the next microtask so views/smartViews entries are wired.
    Promise.resolve().then(() => {
      const li = document.querySelector(sel);
      if (li) li.click();
    });
  }
} catch {}

// ── Remote-freshness helpers ──────────────────────────────────────────────────
//
// Toast on new tracks (rate-limited).  Folder-open trigger — when the
// user clicks into a remote folder we fire a freshness check for the
// owning share (debounced so rapid clicks coalesce).  Visibility
// trigger — when the tab returns to focus after >10 min idle, fire
// a freshness check on the last-viewed remote share.

const _MAX_TOASTS_PER_HOUR = 5;
const _TOAST_HISTORY = []; // timestamps

function _shareAliasForRoot(scanRoot) {
  // Show the user-friendly alias if configured; else the bare URL.
  const aliases = (window.__sbConfig && window.__sbConfig.folder_aliases) || {};
  return aliases[scanRoot] || scanRoot;
}

function _emitRemoteNewTracksToast(scanRoot, count) {
  const now = Date.now();
  // Prune older-than-1-hour entries
  while (_TOAST_HISTORY.length && _TOAST_HISTORY[0] < now - 3600_000) {
    _TOAST_HISTORY.shift();
  }
  if (_TOAST_HISTORY.length >= _MAX_TOASTS_PER_HOUR) {
    console.info(`remote_new_tracks toast suppressed (rate limit): ${count} in ${scanRoot}`);
    return;
  }
  _TOAST_HISTORY.push(now);
  const alias = _shareAliasForRoot(scanRoot);
  const noun = count === 1 ? 'new track' : 'new tracks';
  Toast.info(`🎵 ${count} ${noun} in ${alias}`);
  // Refresh the library + tree so the new entries are visible without
  // a page reload.
  try { Library.refreshBadges?.(); } catch {}
  try { FolderTree.refresh?.(); } catch {}
}

// Debounce per scan_root so a rapid folder-open burst coalesces to one
// /check_now call.  Map: scan_root → last fire timestamp.
const _CHECK_NOW_DEBOUNCE_MS = 30_000;
const _checkNowLastFired = new Map();

async function _maybeFireFreshnessCheck(scanRoot, source) {
  if (!scanRoot || !/^(ftp|smb|webdav):/i.test(scanRoot)) return;
  const now = Date.now();
  const last = _checkNowLastFired.get(scanRoot) || 0;
  if (now - last < _CHECK_NOW_DEBOUNCE_MS) return;
  _checkNowLastFired.set(scanRoot, now);
  try {
    await fetch('/api/admin/freshness/check_now', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scan_root: scanRoot }),
    });
  } catch (err) {
    // Network failure is non-fatal — the next user action (or the
    // background tick) will retry.  Logged for diagnostics only.
    console.debug(`freshness check_now(${source}) for ${scanRoot} failed:`, err);
  }
}

function _resolveRemoteShareFromPath(path) {
  // The folder tree shows nested paths (e.g. "ftp://host/share:/Album").
  // We want the SHARE root for the freshness check.  ``parse_remote_path``
  // splits at the ':' separator on the backend; the frontend just keeps
  // everything up to the first ':' (which is part of the scheme).
  if (!path) return null;
  // Strip the colon-separated relative tail.
  // e.g. "ftp://host/share:/foo/bar" → "ftp://host/share"
  const sepIdx = path.indexOf(':/', 'ftp://'.length);
  if (sepIdx > 0) return path.slice(0, sepIdx);
  // For paths without the ':/' tail (the bare share root), return as-is.
  return path;
}

// On-app-focus trigger: when the tab returns to focus after >10 min of
// being hidden, fire a freshness check for the share whose folder the
// user was last viewing (if any).  Catches the "left it open
// overnight" case without burning API calls during active use.
let _lastHiddenAt = 0;
let _lastViewedShare = null;
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    _lastHiddenAt = Date.now();
  } else if (document.visibilityState === 'visible' && _lastHiddenAt > 0) {
    const idleMs = Date.now() - _lastHiddenAt;
    _lastHiddenAt = 0;
    if (idleMs >= 10 * 60 * 1000 && _lastViewedShare) {
      _maybeFireFreshnessCheck(_lastViewedShare, 'app_focus');
    }
  }
});

// ── Folder tree → show tracks in directory ────────────────────────────────────
//
// onSelect overwrites the previous listener (FolderTree allows one),
// so the share-tracking + folder-open freshness trigger has to live in
// the SAME callback that drives Library.showFolder.
FolderTree.onSelect(async (path) => {
  _deactivateAllNav();
  const share = _resolveRemoteShareFromPath(path);
  if (share && /^(ftp|smb|webdav):/i.test(share)) {
    // Track the last-viewed remote share so the visibility-change
    // handler above knows what to poll on app re-focus.
    _lastViewedShare = share;
    // On-folder-open freshness trigger — fire-and-forget background
    // check.  The library view loads concurrently; if new tracks
    // arrive, the remote_new_tracks toast lands a few seconds later.
    _maybeFireFreshnessCheck(share, 'folder_open');
  }
  await Library.showFolder(path);
});

// ── Scan badge (progress shown via WebSocket) ─────────────────────────────────
const scanBadge = document.getElementById('scan-badge');
// Clicking the visible "Scanning…" indicator opens admin → library so
// the user can see progress detail (UX/UI #1 #7).  Pointer-style + role
// so it announces as a button to assistive tech.
if (scanBadge) {
  scanBadge.style.cursor = 'pointer';
  scanBadge.setAttribute('role', 'button');
  scanBadge.setAttribute('tabindex', '0');
  scanBadge.setAttribute('title', 'Open admin → library');
  const _openAdminLibrary = () => {
    Admin.open();
    document.querySelector('.admin-tab[data-tab="tab-library"]')?.click();
  };
  scanBadge.addEventListener('click', _openAdminLibrary);
  scanBadge.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openAdminLibrary(); }
  });
}

// ── Sidebar drawer toggle (mobile / tablet) ────────────────────────────────
// The CSS hides #sidebar-toggle at desktop widths (>900px), so attaching the
// handler unconditionally is fine — clicks are only reachable when the drawer
// is active.  Sets a ``.sidebar-open`` class on <body> so both the sidebar
// transform and the scrim opacity toggle off the same flag.
(() => {
  const toggleBtn = document.getElementById('sidebar-toggle');
  const sidebar   = document.getElementById('sidebar');
  const scrim     = document.getElementById('sidebar-scrim');
  if (!toggleBtn || !sidebar) return;
  // Off-canvas viewport: below this width the sidebar slides off-screen
  // and we need to suppress its tab order while collapsed.  Mirrors the
  // CSS breakpoint at app.css around the @media (max-width:900px) rule.
  const _narrowMQ = window.matchMedia('(max-width: 900px)');
  const _syncInert = () => {
    const collapsed = !sidebar.classList.contains('is-open');
    // ``inert`` strips the subtree from tab order + assistive-tech tree.
    // Only apply when we're in the drawer mode AND the drawer is closed —
    // at desktop widths the sidebar is permanently visible and should
    // remain interactive.
    sidebar.inert = _narrowMQ.matches && collapsed;
  };
  _narrowMQ.addEventListener?.('change', _syncInert);
  const _setOpen = (open) => {
    sidebar.classList.toggle('is-open', open);
    document.body.classList.toggle('sidebar-open', open);
    toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    _syncInert();
  };
  // Initial state at module load.
  _syncInert();
  toggleBtn.addEventListener('click', () => {
    _setOpen(!sidebar.classList.contains('is-open'));
  });
  if (scrim) scrim.addEventListener('click', () => _setOpen(false));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebar.classList.contains('is-open')) _setOpen(false);
  });
  // Auto-close when the user picks something inside the drawer — otherwise
  // the content they wanted to see is hidden behind the just-opened drawer.
  sidebar.addEventListener('click', (e) => {
    if (!sidebar.classList.contains('is-open')) return;
    const t = e.target;
    if (t instanceof HTMLElement &&
        (t.closest('a, button') || t.closest('[data-route], [data-scan-dir]'))) {
      _setOpen(false);
    }
  });
})();

// ── Admin button ──────────────────────────────────────────────────────────────
document.getElementById('btn-admin').addEventListener('click', () => Admin.open());

// ── EQ button ─────────────────────────────────────────────────────────────────
document.getElementById('btn-eq').addEventListener('click', () => Equalizer.toggle());

// ── Queue / Playlist buttons — sync open-state so user can see which
// panel is currently visible (UX/UI #1 #5).
const _btnQueue    = document.getElementById('btn-queue');
const _btnPlaylist = document.getElementById('btn-playlist');
const _queuePanel  = document.getElementById('queue-panel');
const _playlistPnl = document.getElementById('playlist-panel');
function _syncPanelButtons() {
  if (_btnQueue && _queuePanel) {
    _btnQueue.classList.toggle('on', !_queuePanel.classList.contains('hidden'));
  }
  if (_btnPlaylist && _playlistPnl) {
    _btnPlaylist.classList.toggle('on', !_playlistPnl.classList.contains('hidden'));
  }
}
_btnQueue.addEventListener('click', () => {
  Queue.toggle();
  // Run after the toggle's classList mutation lands.
  setTimeout(_syncPanelButtons, 0);
});
_btnPlaylist.addEventListener('click', () => {
  Playlist.toggle();
  setTimeout(_syncPanelButtons, 0);
});
// Update when the panels themselves change visibility (e.g. close button
// inside the panel) — a MutationObserver on the .hidden class.
new MutationObserver(_syncPanelButtons).observe(
  _queuePanel, { attributes: true, attributeFilter: ['class'] },
);
new MutationObserver(_syncPanelButtons).observe(
  _playlistPnl, { attributes: true, attributeFilter: ['class'] },
);

// ── Add to Playlist from selection bar ───────────────────────────────────────
const selAddPlaylist = document.getElementById('sel-add-playlist');
if (selAddPlaylist) {
  selAddPlaylist.addEventListener('click', () => Playlist.showAddDropdown(selAddPlaylist));
}

// Keep queue panel in sync when queue state changes
Player.on('queuechange', () => Queue.refresh());

// ── Track Info button (player bar) ────────────────────────────────────────────
document.getElementById('btn-track-info').addEventListener('click', () => {
  const q   = Player.queue;
  const idx = Player.queueIdx;
  if (q.length > 0 && idx >= 0) {
    TrackInfo.open(q, idx);
  } else if (Player.currentTrack) {
    // Fallback: open for current track even if queue idx is stale
    TrackInfo.openSingle(Player.currentTrack);
  }
});

// ── Track Info via right-click on library rows ────────────────────────────────
Library.onInfo((tracks, idx) => TrackInfo.open(tracks, idx));

// ── Admin → main page sync ────────────────────────────────────────────────────
// Fired by admin.js after adding/removing/scanning folders so the tree and alias state stay in sync
document.addEventListener('soniqboom:dirs-changed', async () => {
  try {
    const cfg = await fetch('/api/ui-config').then(r => r.json());
    window.__sbConfig = { ...window.__sbConfig, ...cfg };
    Library.setAliasMap(cfg.folder_aliases || {});
    Library.setExposeLocalFiles(cfg.expose_local_files !== false);
  } catch (_) {}
  FolderTree.refresh();
});

// ── Sidebar resize drag handle ────────────────────────────────────────────────
const sidebar       = document.getElementById('sidebar');
const resizeHandle  = document.getElementById('sidebar-resize');

let _resizing = false;
let _startX   = 0;
let _startW   = 0;

resizeHandle.addEventListener('mousedown', (e) => {
  _resizing = true;
  _startX   = e.clientX;
  _startW   = sidebar.offsetWidth;
  resizeHandle.classList.add('dragging');
  document.body.style.cursor = 'col-resize';
  document.body.style.userSelect = 'none';
  e.preventDefault();
});

document.addEventListener('mousemove', (e) => {
  if (!_resizing) return;
  const delta = e.clientX - _startX;
  const newW  = Math.max(140, Math.min(480, _startW + delta));
  sidebar.style.width = `${newW}px`;
});

document.addEventListener('mouseup', () => {
  if (!_resizing) return;
  _resizing = false;
  resizeHandle.classList.remove('dragging');
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  localStorage.setItem('sb_sidebar_w', sidebar.offsetWidth);
});

// Restore saved sidebar width
const savedW = localStorage.getItem('sb_sidebar_w');
if (savedW) sidebar.style.width = `${savedW}px`;

// ── Sidebar horizontal splitters (resizable sections) ────────────────────────

/**
 * Wire up a horizontal splitter that resizes the sections above and below it.
 *
 * @param {string}  splitterId  – ID of the splitter div
 * @param {string}  aboveId     – ID of the section above the splitter
 * @param {string}  belowId     – ID of the section below the splitter
 * @param {string}  storageKey  – localStorage key for persisting the above-section height
 * @param {number}  minAbove    – minimum height (px) for the section above
 * @param {number}  minBelow    – minimum height (px) for the section below
 * @param {boolean} pinBelow    – when false, only the above section gets an explicit height;
 *                                the below section stays flex:1 and fills remaining space.
 *                                Use false for the bottom-most splitter so the last panel
 *                                always fills the sidebar without leaving a dead gap.
 */
function _initHSplitter(splitterId, aboveId, belowId, storageKey,
                        minAbove = 40, minBelow = 40, pinBelow = true) {
  const splitter = document.getElementById(splitterId);
  const above    = document.getElementById(aboveId);
  const below    = document.getElementById(belowId);
  if (!splitter || !above || !below) return;

  let dragging = false, startY = 0, startAboveH = 0, startBelowH = 0;

  splitter.addEventListener('mousedown', (e) => {
    dragging    = true;
    startY      = e.clientY;
    startAboveH = above.offsetHeight;
    startBelowH = below.offsetHeight;
    splitter.classList.add('dragging');
    document.body.style.cursor     = 'row-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const delta  = e.clientY - startY;
    const totalH = startAboveH + startBelowH;
    let newAbove = Math.max(minAbove, Math.min(totalH - minBelow, startAboveH + delta));

    above.style.height = `${newAbove}px`;
    above.style.flex   = 'none';

    if (pinBelow) {
      below.style.height = `${totalH - newAbove}px`;
      below.style.flex   = 'none';
    }
    // else: below keeps flex:1 and fills remaining space automatically
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('dragging');
    document.body.style.cursor     = '';
    document.body.style.userSelect = '';
    localStorage.setItem(storageKey, above.offsetHeight);
    if (pinBelow) localStorage.setItem(storageKey + '_b', below.offsetHeight);
  });

  // Restore persisted heights.  Heights are clamped to the current sidebar
  // height — stale localStorage values from an earlier session (bigger
  // window, more monitors, etc.) used to leave sections taller than the
  // sidebar itself, which clipped the folder-tree scroll container and made
  // it look like scrolling was broken.
  const sidebarH = splitter.parentElement?.clientHeight || window.innerHeight;
  const maxSection = Math.max(minAbove + minBelow, Math.floor(sidebarH * 0.6));
  const clamp = (v) => Math.max(minAbove, Math.min(maxSection, parseInt(v, 10) || 0));

  const savedAbove = localStorage.getItem(storageKey);
  if (savedAbove) {
    above.style.height = `${clamp(savedAbove)}px`;
    above.style.flex   = 'none';
  }
  if (pinBelow) {
    const savedBelow = localStorage.getItem(storageKey + '_b');
    if (savedBelow) {
      below.style.height = `${clamp(savedBelow)}px`;
      below.style.flex   = 'none';
    }
  }
}

_initHSplitter('splitter-library-folders', 'section-library', 'section-folders',
               'sb_split_lib',   40, 40, true);
_initHSplitter('splitter-folders-smart',   'section-folders', 'section-smart',
               'sb_split_fold',  40, 40, true);
// pinBelow=false: Playlists is the last section and must always fill remaining space
_initHSplitter('sidebar-splitter',         'section-smart',   'section-playlists',
               'sb_split_smart', 40, 40, false);

// Post-restore sanity check: if the three persisted splitter heights still
// sum to more than the sidebar can display, the last (flex:1) section —
// Playlists — gets clipped to 0 px and users see an empty strip instead of
// the playlist list.  This happens when localStorage carries over heights
// from a session with a taller sidebar (external monitor, resized window).
// Detect and reset once; the user gets the default flex layout back.
requestAnimationFrame(() => {
  const plSec = document.getElementById('section-playlists');
  if (!plSec) return;
  if (plSec.offsetHeight < 60) {
    ['sb_split_lib', 'sb_split_lib_b',
     'sb_split_fold', 'sb_split_fold_b',
     'sb_split_smart', 'sb_split_smart_b',
    ].forEach(k => localStorage.removeItem(k));
    // Wipe the inline styles the restore block applied so the default flex
    // rules from CSS take effect again.
    ['section-library', 'section-folders', 'section-smart'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.style.height = ''; el.style.flex = ''; }
    });
  }
});

// ── Scan-badge stuck-at-99% watchdog ─────────────────────────────────────────
//
// Defensive safety-net for the scan_progress WebSocket: if the server
// emits a final ``running:true, processed >= total`` payload and then
// crashes / disconnects before the closing ``running:false`` message,
// the badge would stick on screen forever showing "Scanning N% (M/M)".
// The watchdog polls the HTTP /api/admin/scan/status endpoint every 3s
// once we've seen processed >= total; the moment the backend says the
// scan is not running we flip the badge to "Done" ourselves.  Cleared
// by any subsequent non-running scan_progress event.
let _stuckBadgeTimer = null;
function _armStuckBadgeWatchdog() {
  if (_stuckBadgeTimer) return;
  _stuckBadgeTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/admin/scan/status', { credentials: 'same-origin' });
      if (!res.ok) return;  // auth not ready or transient — try again next tick
      const st = await res.json();
      if (!st.running && !st.embedding) {
        scanBadge.textContent =
          `Done — ${st.processed != null ? st.processed : st.total ?? 0} tracks`;
        setTimeout(() => { scanBadge.hidden = true; }, 4000);
        _disarmStuckBadgeWatchdog();
        try {
          Library.showAll();
          Library.refreshBadges();
          FolderTree.refresh();
          FolderTree.setScanActive(false);
        } catch (_) { /* defensive — module may not be ready */ }
      }
    } catch (_) {
      // network blip — keep polling
    }
  }, 3000);
}
function _disarmStuckBadgeWatchdog() {
  if (_stuckBadgeTimer) {
    clearInterval(_stuckBadgeTimer);
    _stuckBadgeTimer = null;
  }
}

// ── WebSocket — scan progress ─────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/library/ws`);

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'scan_progress') {
      if (msg.running) {
        // Phase 1: metadata scan in progress.  Server can emit a
        // ``pct:0, total:0`` payload while it walks the FS to *count*
        // files — at that point "0% (0/0)" reads as broken to users.
        // Show a friendlier "counting" label until totals are known.
        scanBadge.hidden = false;
        if (!msg.total) {
          scanBadge.textContent = 'Scanning… (counting files)';
        } else {
          scanBadge.textContent = `Scanning ${msg.pct}% (${msg.processed}/${msg.total})`;
        }
        FolderTree.setScanActive(true);
        // Safety net for the badge-stuck-at-99% bug: if processed has
        // caught up to total (or close to it) but ``running`` is still
        // true, the server is doing post-scan work (dedup, aggregation
        // cache, sort-index rebuild) — that can take 5–20 s on big
        // libraries.  Schedule a poll that asks the HTTP status
        // endpoint for the real state every 3 s; the moment the
        // backend says ``running:false`` we flip the badge to "Done"
        // ourselves without waiting for a (potentially missed) WS
        // broadcast.  Cleared by any non-running scan_progress event.
        if (msg.total && msg.processed >= msg.total) {
          _armStuckBadgeWatchdog();
        } else {
          _disarmStuckBadgeWatchdog();
        }
      } else if (msg.embedding) {
        // Phase 1 done, phase 2 embedding in background.  Distinct label
        // so users know the library is already usable.
        scanBadge.hidden = false;
        scanBadge.textContent = 'Computing embeddings…';
        _disarmStuckBadgeWatchdog();
        // Library is already usable — refresh now
        Library.showAll();
        FolderTree.refresh();
        FolderTree.setScanActive(true);
      } else {
        // Both phases complete
        scanBadge.textContent = `Done \u2014 ${msg.processed} tracks`;
        setTimeout(() => { scanBadge.hidden = true; }, 4000);
        _disarmStuckBadgeWatchdog();
        Library.showAll();
        Library.refreshBadges();
        FolderTree.refresh();
        FolderTree.setScanActive(false);
      }
    } else if (msg.event === 'repair_progress') {
      // The metadata repair task (admin > Library > Repair Garbled
      // Metadata) emits progress via the same WS.  We don't render
      // anything in the main shell — admin.js binds its own handler
      // via a custom DOM event so the badge UI stays self-contained.
      window.dispatchEvent(new CustomEvent('soniqboom:repair-progress', { detail: msg }));
    } else if (msg.event === 'remote_new_tracks') {
      // Adaptive remote-freshness scanner discovered new tracks in a
      // remote share.  Show ONE toast per share per coalesce window
      // ("3 new tracks in <share alias>").  Tap to refresh the library
      // view so the new tracks are visible immediately.
      //
      // Rate limit: never more than _MAX_TOASTS_PER_HOUR per session
      // to handle the bulk-import-of-10000-files case gracefully.
      try {
        const count = Number(msg.count || 0);
        if (count > 0) {
          _emitRemoteNewTracksToast(msg.scan_root, count);
        }
      } catch (err) {
        console.warn('remote_new_tracks toast failed', err);
      }
    }
  };

  // Exponential backoff with jitter — a fixed 2s reconnect produced a
  // 5-user reconnect-storm against a flaky server.  Starts at 1s, doubles
  // up to 30s, with ±25% jitter.
  ws.onclose = async (ev) => {
    // 4401 is our custom "auth required" close from the server.  Don't
    // burn through backoff retrying a stale cookie — wait for the user
    // to sign in (HTTP 401 elsewhere will already have shown the overlay)
    // and then immediately reconnect with the fresh session.
    if (ev && ev.code === 4401) {
      try { await Auth.ready; } catch { /* if Auth not yet defined */ }
      window.__sbWsBackoff = 1000;
      setTimeout(connectWS, 200);
      return;
    }
    const last = window.__sbWsBackoff || 1000;
    const next = Math.min(30000, last * 2);
    window.__sbWsBackoff = next;
    const jitter = next * (0.75 + Math.random() * 0.5);
    setTimeout(connectWS, Math.round(jitter));
  };
  ws.onopen = () => { window.__sbWsBackoff = 1000; };
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
const _shortcutsOverlay = document.getElementById('shortcuts-overlay');
document.getElementById('shortcuts-close').addEventListener('click', () => _shortcutsOverlay.classList.add('hidden'));
_shortcutsOverlay.addEventListener('click', (e) => { if (e.target === _shortcutsOverlay) _shortcutsOverlay.classList.add('hidden'); });

// ``_prevVolume`` is declared earlier (near the volume bar init) so the
// glyph mute toggle and this keyboard handler share the same restore
// value across the lifetime of the page.

document.addEventListener('keydown', (e) => {
  // Don't intercept when typing in an input or interacting with a
  // composite widget.  The check is intentionally broad:
  //   * INPUT / TEXTAREA — the original guard (text typing).
  //   * isContentEditable — rich-text fields, lyrics editor, anywhere a
  //     contenteditable host swallows printable keys.
  //   * role=button / tab / radio — focused composite widgets handle their
  //     own key bindings (Space activates, Arrow moves selection).
  //   * focused dialog — if any modal we know about is visible and has
  //     focus inside, treat the dialog as owning all keystrokes.
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
  if (t && t.isContentEditable) return;
  if (t && typeof t.matches === 'function'
      && t.matches('[role="button"], [role="tab"], [role="radio"]')) return;
  // If any visible dialog has focus inside it, let the dialog own the keys.
  if (document.querySelector(
    '.dialog.visible:focus-within, .modal.visible:focus-within, '
    + '[role="dialog"]:not(.hidden):focus-within',
  )) return;
  // Don't hijack modifier combos: Cmd+S (Save), Cmd+R (Reload), Cmd+E,
  // Cmd+Q etc. — the previous handlers fired regardless of modifier so
  // Cmd+S was toggling shuffle while also opening Save.
  if (e.metaKey || e.ctrlKey || e.altKey) {
    // Alt+digit shortcuts are wired below intentionally; let those through.
    const isAltDigit = e.altKey && !e.metaKey && !e.ctrlKey
      && /^Digit[1-9]$/.test(e.code);
    if (!isAltDigit) return;
  }

  // ? — toggle shortcuts overlay
  if (e.key === '?') {
    e.preventDefault();
    _shortcutsOverlay.classList.toggle('hidden');
    return;
  }

  // Escape — close any open overlay
  if (e.code === 'Escape') {
    if (!_shortcutsOverlay.classList.contains('hidden')) {
      _shortcutsOverlay.classList.add('hidden');
      return;
    }
    return; // let other handlers handle their own Escape
  }

  // Space — play/pause
  if (e.code === 'Space') { e.preventDefault(); Player.playPause(); return; }

  // Meta+Right / Meta+Left — next / prev
  if (e.code === 'ArrowRight' && e.metaKey) { Player.next(); return; }
  if (e.code === 'ArrowLeft'  && e.metaKey) { Player.prev(); return; }

  // ArrowUp / ArrowDown — volume
  if (e.code === 'ArrowUp' && !e.metaKey) {
    e.preventDefault();
    const newVol = Math.min(1, parseFloat(volBar.value) + 0.05);
    volBar.value = newVol;
    Player.setVolume(newVol);
    return;
  }
  if (e.code === 'ArrowDown' && !e.metaKey) {
    e.preventDefault();
    const newVol = Math.max(0, parseFloat(volBar.value) - 0.05);
    volBar.value = newVol;
    Player.setVolume(newVol);
    return;
  }

  // M — mute/unmute
  if (e.code === 'KeyM') {
    const cur = parseFloat(volBar.value);
    if (cur > 0) {
      _prevVolume = cur;
      localStorage.setItem('sb_prev_volume', String(cur));
      volBar.value = 0;
      Player.setVolume(0);
    } else {
      volBar.value = _prevVolume;
      Player.setVolume(_prevVolume);
    }
    return;
  }

  // S — toggle shuffle
  if (e.code === 'KeyS') {
    btnShuffle.classList.toggle('on', Player.toggleShuffle());
    return;
  }

  // R — cycle repeat
  if (e.code === 'KeyR') {
    _renderRepeatBtn(Player.toggleRepeat());
    return;
  }

  // / — focus search
  if (e.key === '/') {
    e.preventDefault();
    document.getElementById('search-input').focus();
    return;
  }

  // V — toggle visualizer mode (oscilloscope / spectrogram)
  if (e.code === 'KeyV') { Visualizer.toggleMode(); return; }

  // E — toggle EQ
  if (e.code === 'KeyE') { Equalizer.toggle(); return; }

  // Q — toggle queue
  if (e.code === 'KeyQ') { Queue.toggle(); return; }

  // I — track info
  if (e.code === 'KeyI') {
    const q = Player.queue;
    const idx = Player.queueIdx;
    if (q.length > 0 && idx >= 0) TrackInfo.open(q, idx);
    return;
  }

  // Enter — play focused track (when not in a text input)
  if (e.code === 'Enter') {
    e.preventDefault();
    Library.playFocused();
    return;
  }

  // J / K — navigate tracks down / up in library
  if (e.code === 'KeyJ') { Library.navigateTrack(1); return; }
  if (e.code === 'KeyK') { Library.navigateTrack(-1); return; }

  // A — add focused track to queue
  if (e.code === 'KeyA') { Library.addFocusedToQueue(); return; }

  // H — toggle history view
  if (e.code === 'KeyH') {
    _deactivateAllNav();
    const li = document.querySelector('#nav-smart li[data-view="history"]');
    if (li) li.classList.add('active');
    smartViews['history']();
    return;
  }

  // D — toggle duplicates view
  if (e.code === 'KeyD') {
    _deactivateAllNav();
    const li = document.querySelector('#nav-smart li[data-view="duplicates"]');
    if (li) li.classList.add('active');
    smartViews['duplicates']();
    return;
  }

  // 1-6 — Library views.  Alt+1..6 — Smart views (overlap-free with the
  // numeric Library shortcuts above; the Alt-namespace was empty).
  const viewKeys = { 'Digit1': 'all', 'Digit2': 'artists', 'Digit3': 'album_artists', 'Digit4': 'albums', 'Digit5': 'genres', 'Digit6': 'years' };
  if (!e.altKey && viewKeys[e.code]) {
    const view = viewKeys[e.code];
    _deactivateAllNav();
    const li = document.querySelector(`#nav-library li[data-view="${view}"]`);
    if (li) { li.classList.add('active'); li.setAttribute('aria-current', 'page'); }
    if (views[view]) views[view]();
    return;
  }
  const smartKeys = {
    'Digit1': 'history',        // Alt+1
    'Digit2': 'most-played',    // Alt+2
    'Digit3': 'recently-added', // Alt+3
    'Digit4': 'top-rated',      // Alt+4
    'Digit5': 'unplayed',       // Alt+5
    'Digit6': 'duplicates',     // Alt+6
  };
  if (e.altKey && !e.metaKey && !e.ctrlKey && smartKeys[e.code]) {
    const view = smartKeys[e.code];
    e.preventDefault();
    _deactivateAllNav();
    const li = document.querySelector(`#nav-smart li[data-view="${view}"]`);
    if (li) { li.classList.add('active'); li.setAttribute('aria-current', 'page'); }
    if (smartViews[view]) smartViews[view]();
    return;
  }
});

// ── Startup: fetch UI config, distribute alias map & toggle ────────────────────
(async () => {
  try {
    const cfg = await fetch('/api/ui-config').then(r => r.json());
    window.__sbConfig = { ...window.__sbConfig, ...cfg };

    // Distribute alias map & expose toggle to Library module
    Library.setAliasMap(cfg.folder_aliases || {});
    Library.setExposeLocalFiles(cfg.expose_local_files !== false);
  } catch (_) { /* Store not ready — ignore */ }
})();

// ── Startup intro animation ───────────────────────────────────────────────────
(async () => {
  try {
    const cfg = window.__sbConfig || await fetch('/api/ui-config').then(r => r.json());
    if (!cfg.display_startup_logo) return;
  } catch (_) { return; }

  const overlay = document.createElement('div');
  overlay.id = 'sb-intro-overlay';
  overlay.innerHTML = `
    <div id="sb-intro-logo">
      <span class="sb-intro-icon">
        <span class="sb-intro-icon-glow">🔊</span><span class="sb-intro-icon-top">🔊</span>
      </span>
      <div class="sb-intro-title-wrap">
        <span class="sb-intro-text">SoniqBoom</span>
        <span class="sb-intro-byline">by S.F.Cyris</span>
        <canvas class="sb-intro-electric"></canvas>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const logo = overlay.querySelector('#sb-intro-logo');
  const titleWrap = overlay.querySelector('.sb-intro-title-wrap');
  const elCanvas  = overlay.querySelector('.sb-intro-electric');

  // ── Electric arc animation ──────────────────────────────────────────────
  function _initElectric() {
    const dpr = window.devicePixelRatio || 1;
    const rect = titleWrap.getBoundingClientRect();
    elCanvas.width  = rect.width  * dpr;
    elCanvas.height = rect.height * dpr;
    elCanvas.style.width  = rect.width  + 'px';
    elCanvas.style.height = rect.height + 'px';
    const ctx = elCanvas.getContext('2d');
    ctx.scale(dpr, dpr);

    const W = rect.width, H = rect.height;
    const arcs = [];          // active arcs

    function spawnArc() {
      // Start from a random point along the text area
      const x = Math.random() * W;
      const y = Math.random() * H * 0.75;    // bias toward the title text
      const len = 20 + Math.random() * 60;   // arc length in px
      const angle = (Math.random() - 0.5) * 1.2;  // mostly horizontal
      const segs = 4 + Math.floor(Math.random() * 6);
      const points = [{ x, y }];
      for (let i = 1; i <= segs; i++) {
        const t = i / segs;
        const px = x + Math.cos(angle) * len * t + (Math.random() - 0.5) * 14;
        const py = y + Math.sin(angle) * len * t + (Math.random() - 0.5) * 14;
        points.push({ x: px, y: py });
      }
      arcs.push({
        points,
        life: 1.0,
        decay: 0.03 + Math.random() * 0.06,
        width: 0.5 + Math.random() * 1.5,
      });
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      // Spawn new arcs randomly
      if (Math.random() < 0.4) spawnArc();
      if (Math.random() < 0.15) spawnArc();   // occasional double

      for (let i = arcs.length - 1; i >= 0; i--) {
        const a = arcs[i];
        a.life -= a.decay;
        if (a.life <= 0) { arcs.splice(i, 1); continue; }

        // Jitter the points slightly each frame for sizzle
        for (let j = 1; j < a.points.length - 1; j++) {
          a.points[j].x += (Math.random() - 0.5) * 3;
          a.points[j].y += (Math.random() - 0.5) * 3;
        }

        ctx.save();
        ctx.globalAlpha = a.life;
        // Outer glow
        ctx.strokeStyle = 'rgba(107,200,240,0.6)';
        ctx.lineWidth = a.width + 3;
        ctx.shadowColor = 'rgba(107,200,240,0.8)';
        ctx.shadowBlur = 12;
        ctx.beginPath();
        ctx.moveTo(a.points[0].x, a.points[0].y);
        for (let j = 1; j < a.points.length; j++) ctx.lineTo(a.points[j].x, a.points[j].y);
        ctx.stroke();
        // Bright white core
        ctx.strokeStyle = 'rgba(255,255,255,0.9)';
        ctx.lineWidth = a.width;
        ctx.shadowColor = '#fff';
        ctx.shadowBlur = 4;
        ctx.beginPath();
        ctx.moveTo(a.points[0].x, a.points[0].y);
        for (let j = 1; j < a.points.length; j++) ctx.lineTo(a.points[j].x, a.points[j].y);
        ctx.stroke();
        ctx.restore();
      }
    }
    return draw;
  }

  // Blurred backdrop from the start
  overlay.style.backdropFilter = 'blur(18px)';
  overlay.style.webkitBackdropFilter = 'blur(18px)';

  // Fade in
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  logo.style.transition = 'opacity 0.25s ease';
  logo.style.opacity = '1';
  await new Promise(r => setTimeout(r, 300));

  // Start electric arcs once visible
  const drawElectric = _initElectric();
  let _elecRaf;
  const _elecLoop = () => { drawElectric(); _elecRaf = requestAnimationFrame(_elecLoop); };
  _elecLoop();

  // Hold for 2 s with electricity
  await new Promise(r => setTimeout(r, 2000));

  // Enlarge to 400% + fade out in 1 s, while blur dissolves to 0
  overlay.style.transition = 'backdrop-filter 1s ease-out, -webkit-backdrop-filter 1s ease-out';
  overlay.style.backdropFilter = 'blur(0px)';
  overlay.style.webkitBackdropFilter = 'blur(0px)';
  logo.style.transition = 'transform 1s cubic-bezier(0.2,0,0.8,1), opacity 1s ease-out';
  logo.style.transform = 'scale(4)';
  logo.style.opacity = '0';
  await new Promise(r => setTimeout(r, 1000));

  cancelAnimationFrame(_elecRaf);
  overlay.remove();
})();

// ── Init ──────────────────────────────────────────────────────────────────────
Library.showAll();
FolderTree.refresh();
// Populate the sidebar playlist list on startup.  Without this, the sidebar
// only rendered once the user opened the right-hand playlist panel (which
// calls Playlist.refresh() internally).
Playlist.refresh();
connectWS();
// One-shot check: if a scan was already running when the page opened,
// the WebSocket will deliver progress events to keep the UI in sync.
fetch('/api/library/scan/status').then(r => r.json()).then(s => {
  if (s.running || s.embedding) FolderTree.setScanActive(true);
}).catch(() => {});
