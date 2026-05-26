// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * player.js — HTML5 Audio engine with Web Audio API hook for visualizer.
 * Exports: Player singleton
 */
// version bump needed in index.html for player.js (queue/race/EQ chain fixes)
import { TRACKER_FORMAT_NAMES, Toast } from './utils.js';

export const Player = (() => {
  const audio = document.getElementById('audio-el');
  let trackId        = null;
  let _track         = null;   // full track object currently loaded
  let _metaDuration  = 0;      // track.duration from library metadata
  let _seekOffset    = 0;      // historical: seconds offset for live-pipe streams.  Now
                                // always 0 because every transcoded path is cached on disk
                                // and served with HTTP Range support.  Kept for the lyrics
                                // / chapter timeline maths that still references it.
  let _isTranscoded  = false;  // historical flag — kept always-false now.  The live
                                // ``_transcode_stream`` ffmpeg pipe was retired in favour
                                // of ``get_or_render`` + ``_range_file_response``.
  let _pendingSeekSec = null;  // seek requested before audio loaded; replayed on canplay
  let _needsConvert  = false;  // true for any format requiring server-side rendering/transcoding

  // ── SID progressive playback state ────────────────────────────────────────
  // When a shorter cached SID is served while the full version renders in bg,
  // _sidPartial tracks the boundary.  The player shows the full target duration
  // and seamlessly switches when the full render is ready.
  let _sidPartial       = false;   // true if currently playing a shorter cached version
  let _sidCachedSec     = 0;       // seconds in the currently playing file
  let _sidTargetSec     = 0;       // target duration being rendered in background
  let _sidFullReady     = false;   // true once the full render is cached
  let _sidPollTimer     = null;    // interval polling render-status

  // "Converting…" badge — shown after a configurable delay for rendered formats
  const _convertBadge = document.getElementById('converting-badge');
  let _convertTimer   = null;
  const CONVERT_DELAY_KEY = 'sb_convert_delay';
  function _getConvertDelay() {
    const v = localStorage.getItem(CONVERT_DELAY_KEY);
    // Default 6000 ms (was 3000).  A cold DSD transcode's audio element
    // often takes 2-4 s to start actually playing even when the server
    // delivers the first 5 s of PCM in 250 ms (verified 2026-05-23);
    // the browser's own HAVE_FUTURE_DATA gate accounts for the rest.
    // 6 s puts the badge after the typical actual-play-start moment so
    // it only appears for genuinely-slow conversions, not at every
    // track start.  Users who want it earlier can set the localStorage
    // key explicitly.
    return v !== null ? parseFloat(v) : 6000;
  }
  function _showConvertBadge() {
    if (!_convertBadge) return;
    _convertBadge.hidden = false;
    // Promote to cancellable: shows the inline × button declared in
    // index.html.  Wire its click on first show only (idempotent —
    // dataset flag guards against re-wiring on every show).
    _convertBadge.classList.add('cancellable');
    const cancelEl = _convertBadge.querySelector('#converting-badge-cancel');
    if (cancelEl && !cancelEl.dataset.wired) {
      cancelEl.dataset.wired = '1';
      cancelEl.addEventListener('click', () => {
        const tid = trackId;
        try {
          if (tid) fetch(`/api/stream/${tid}/cancel`, {
            method: 'POST', credentials: 'same-origin',
          }).catch(() => {});
        } catch (_) {}
        _stopTranscodePolling();
        _hideConvertBadge();
      });
    }
  }
  function _hideConvertBadge() {
    // UI-only cleanup — does NOT stop the transcode poll.
    //
    // Why: PERC-9 means audio.play() resolves in well under 100 ms (a
    // chunked first-play kicks off long before the backend's full
    // conversion is done), and the success path then hides the badge.
    // If hiding the badge ALSO killed the poll, the poll would die
    // 30–60 s before the backend flips ``ready: True``, so the
    // ``transcode-ready`` event would never fire and the waveform would
    // never refresh in place — the user would see the silent-padded
    // initial waveform until they navigated away and came back.
    //
    // Callers that legitimately want to abandon the running transcode
    // (cancel button, audio.play() error, SID 5-min timeout, SID
    // switch-to-full handoff) must call ``_stopTranscodePolling()``
    // themselves alongside ``_hideConvertBadge()``.
    clearTimeout(_convertTimer);
    _convertTimer = null;
    if (_convertBadge) _convertBadge.hidden = true;
    _resetConvertBadgeProgress();
  }

  // ── Determinate transcode progress ────────────────────────────────────────
  // While a non-native track is being transcoded server-side, poll
  // /api/stream/{id}/transcode-status and surface live percent + ETA in
  // the badge.  Replaces the indeterminate spinner — Hofman 2009 / Card
  // 1983 / Nielsen all show that a determinate indicator past ~3 s is
  // perceived as significantly faster than an opaque one, even when the
  // actual wall-clock wait is identical.
  let _transcodePollTimer = null;
  let _transcodePollTrackId = null;

  function _startTranscodePolling(trackId) {
    _stopTranscodePolling();
    _transcodePollTrackId = trackId;
    // 600 ms cadence: fast enough to feel live (>1 Hz updates read as
    // continuous motion to the eye), slow enough to keep server load
    // negligible — at most one HEAD-sized GET per transcode-second.
    _transcodePollTimer = setInterval(
      () => _pollTranscodeStatusOnce(trackId), 600,
    );
    // Fire once immediately so the badge can flip to determinate as soon
    // as the first progress sample lands — no 600 ms blank gap.
    _pollTranscodeStatusOnce(trackId);
  }

  function _stopTranscodePolling() {
    if (_transcodePollTimer) {
      clearInterval(_transcodePollTimer);
      _transcodePollTimer = null;
    }
    _transcodePollTrackId = null;
  }

  async function _pollTranscodeStatusOnce(trackId) {
    // Discard responses after the user has switched tracks — a slow
    // backend can return progress for the *previous* track and we don't
    // want that flashing into the badge mid-switch.
    if (_transcodePollTrackId !== trackId) return;
    try {
      const res = await fetch(`/api/stream/${trackId}/transcode-status`,
                              { credentials: 'same-origin' });
      if (!res.ok) return;
      const j = await res.json();
      if (_transcodePollTrackId !== trackId) return;
      if (j.ready) {
        // Render finished — the audio element's own load lifecycle will
        // take over from here.  Snap the bar to 100 % for one beat
        // before letting _hideConvertBadge clear it; without this the
        // bar can vanish mid-fill which reads as a glitch.
        _updateConvertBadgeProgress(100, 0);
        _stopTranscodePolling();
        // PERC-9: the waveform served at track-load was computed off
        // the partial in-flight WAV (only a few seconds of audio were
        // on disk).  Now that the full file is cached, re-fetch so the
        // overlay reflects the complete track instead of the silent
        // padding the partial read produced.  app.js's trackchange
        // listener owns the canvas — we fire this event there too.
        try { emit('transcode-ready', { trackId }); }
        catch (_) { /* listener exceptions logged in emit() */ }
        return;
      }
      if (typeof j.percent === 'number' && j.percent > 0) {
        // Only flip to determinate once we actually have a sample —
        // showing "0 %" with no movement is worse than "Converting…".
        _updateConvertBadgeProgress(j.percent, j.eta_seconds);
      }
    } catch (_) { /* network blip — try again next tick */ }
  }

  function _updateConvertBadgeProgress(percent, etaSec) {
    if (!_convertBadge) return;
    let bar = _convertBadge.querySelector('.progress-bar');
    if (!bar) {
      // First sample — promote the badge from text-only to determinate.
      _convertBadge.textContent = '';
      _convertBadge.classList.add('has-progress');
      const label = document.createElement('span');
      label.className = 'progress-label';
      label.textContent = 'Converting…';
      bar = document.createElement('div');
      bar.className = 'progress-bar';
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-valuemin', '0');
      bar.setAttribute('aria-valuemax', '100');
      const fill = document.createElement('div');
      fill.className = 'progress-fill';
      bar.appendChild(fill);
      const eta = document.createElement('span');
      eta.className = 'progress-eta';
      // Cancel button — gives the user an explicit escape from a long
      // transcode (e.g. an accidental DSD play).  The backend endpoint
      // may not exist yet; in that case the fetch fails silently and
      // _hideConvertBadge still tears down the polling + UI client-side.
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'progress-cancel';
      cancelBtn.setAttribute('aria-label', 'Cancel conversion');
      cancelBtn.title = 'Cancel conversion';
      cancelBtn.textContent = '×';   // multiplication sign — visual ×
      cancelBtn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const cancelId = _transcodePollTrackId || trackId;
        if (cancelId) {
          // Fire-and-forget — backend endpoint may not exist yet.  The
          // client-side teardown happens regardless so the UI is
          // responsive even if the server ignores the request.
          fetch(`/api/stream/${cancelId}/cancel`, { method: 'POST' })
            .catch(() => { /* endpoint may not exist — UI side still cleared */ });
        }
        _stopTranscodePolling();
        _hideConvertBadge();
      });
      _convertBadge.appendChild(label);
      _convertBadge.appendChild(bar);
      _convertBadge.appendChild(eta);
      _convertBadge.appendChild(cancelBtn);
    }
    const fill = bar.querySelector('.progress-fill');
    const eta  = _convertBadge.querySelector('.progress-eta');
    const pct  = Math.max(0, Math.min(100, percent));
    fill.style.width = `${pct.toFixed(1)}%`;
    bar.setAttribute('aria-valuenow', String(Math.round(pct)));
    bar.setAttribute('aria-valuetext', `${Math.round(pct)} percent complete`);
    if (etaSec !== null && etaSec !== undefined && etaSec > 0.5) {
      // Round up so the ETA never reads "0 s left" while we're still
      // visibly working — that mismatch dents credibility hard.
      eta.textContent = `${Math.ceil(etaSec)} s left`;
    } else if (pct >= 99.5) {
      eta.textContent = 'finishing…';
    } else {
      eta.textContent = '';
    }
  }

  function _resetConvertBadgeProgress() {
    if (!_convertBadge) return;
    if (!_convertBadge.classList.contains('has-progress')) return;
    while (_convertBadge.firstChild) _convertBadge.removeChild(_convertBadge.firstChild);
    _convertBadge.classList.remove('has-progress');
    _convertBadge.textContent = 'Converting…';
  }

  // Formats served as FileResponse with Accept-Ranges (browser can seek
  // natively).  OPUS is gated below per-engine: Safari < 17 can't decode
  // it inside HTML5 <audio>, so on those builds we transcode instead.
  const NATIVE_FORMATS_BASE = new Set(['MP3', 'FLAC', 'WAV', 'OGG', 'AAC']);

  // Safari decodes ALAC natively, so the backend serves the original .m4a
  // directly (with Range support) instead of transcoding to FLAC.  The
  // frontend must mirror that decision so seeks use audio.currentTime
  // rather than the ?seek= reload path (which is for ffmpeg pipes only).
  //
  // The original sniff lumped Chrome-iOS / Edge-iOS / Firefox-iOS into
  // "Safari" because they all carry Safari in the UA.  Those builds use
  // a WebKit core but ship slightly different codec profiles (notably
  // they're more reliable on OPUS than legacy Safari), so we treat them
  // separately.
  const _UA = navigator.userAgent;
  const _IS_IOS = /iP(hone|ad|od)/.test(_UA);
  const _IS_CHROME_IOS = /CriOS/.test(_UA);
  const _IS_EDGE_IOS   = /EdgiOS/.test(_UA);
  const _IS_FIREFOX_IOS = /FxiOS/.test(_UA);
  // Genuine Safari (desktop or iOS): has "Safari" but none of the other
  // engines' markers.
  const _IS_SAFARI = /Safari/.test(_UA)
      && !/Chrome|Chromium|CriOS|FxiOS|EdgiOS|Edg\/|OPR\//.test(_UA);

  // Feature-detect OPUS in HTML5 audio so Safari < 17 doesn't get handed
  // an .opus URL it can't decode (the connection would just stall).
  let _AUDIO_PROBE = null;
  function _opusPlayable() {
    if (_AUDIO_PROBE === null) _AUDIO_PROBE = document.createElement('audio');
    const can = _AUDIO_PROBE.canPlayType('audio/ogg; codecs=opus')
             || _AUDIO_PROBE.canPlayType('audio/opus');
    return !!can && can !== '';
  }

  function _nativeForThisBrowser(fmtUp) {
    // The backend stores combined labels like "AAC/M4A" or "ALAC/AAC"
    // (codec/container) for ambiguous Apple files — split and check each
    // component so an AAC-in-M4A track isn't mis-routed through the
    // transcoded seek path (REG-5: seek used to restart the song and
    // make the lyrics highlight drift, because the backend was already
    // serving the file natively with Range support).
    const parts = fmtUp.split('/').map(s => s.trim()).filter(Boolean);
    for (const p of parts) {
      if (NATIVE_FORMATS_BASE.has(p)) return true;
      if (p === 'OPUS' && _opusPlayable()) return true;
      if (_IS_SAFARI && p === 'ALAC') return true;
    }
    return false;
  }

  // SID / MIDI / Tracker formats are rendered to cached WAV files and served via
  // FileResponse with Accept-Ranges — so they ARE natively seekable, not transcoded.
  // Using ?seek= on these does nothing; the server ignores that param for rendered formats.
  const RENDERED_SEEKABLE_FORMATS = new Set([
    'SID', 'PSID', 'MIDI', 'MID',
    ...TRACKER_FORMAT_NAMES,   // ProTracker, ScreamTracker 3, FastTracker 2, etc.
  ]);
  let queue         = [];
  let queueIdx      = -1;
  let shuffle       = false;
  let repeatMode    = 'none'; // 'none' | 'one' | 'all'

  // ── Crossfade / gapless ────────────────────────────────────────────────
  const CROSSFADE_KEY = 'sb_crossfade';
  function _getCrossfade() {
    const v = localStorage.getItem(CROSSFADE_KEY);
    return v !== null ? parseFloat(v) : 0;  // 0 = disabled (gapless only)
  }
  let _crossfadeTimer = null;
  let _crossfading = false;

  // ── Preload buffer ─────────────────────────────────────────────────────
  // Optional anti-stutter cushion: wait until N seconds of audio are
  // buffered ahead of the play head before starting playback.  Default
  // is 0 — we let the browser's own ``canplay`` decide when audio is
  // ready, and rely on the ``waiting`` event below to surface the
  // buffering badge if playback genuinely stalls.  Users who want a
  // hard pre-buffer (slow remote shares, satellite, etc.) can set
  // ``sb_preload_buffer`` in localStorage to a positive number of
  // seconds.  Verified 2026-05-23: default 5 caused a multi-second
  // wait at the start of every cold DSD transcode — the user reported
  // the "starts immediately" behaviour disappeared.  Setting to 0
  // restores it without sacrificing stutter protection (the audio
  // element + the waiting-event badge handle real underruns).
  const PRELOAD_KEY = 'sb_preload_buffer';
  function _getPreloadBuffer() {
    const v = localStorage.getItem(PRELOAD_KEY);
    return v !== null ? Math.max(0, parseFloat(v)) : 0;   // default: no artificial wait
  }
  const _bufferingBadge = document.getElementById('buffering-badge');
  // Same asymmetric-delay treatment we use for the Converting badge:
  // surface only when the wait genuinely exceeds human-perception
  // "instant" budget (~100 ms) plus a tolerance for normal browser
  // load+decode jitter.  2.5 s catches real network / slow-disk waits
  // without flashing for the ~50 ms it takes to start a cached file.
  // (Card et al. 1983, Nielsen "Response Times: 3 Important Limits".)
  let _bufferingTimer = null;
  // Hofman et al. ("Tolerable Waiting Time for Interactive Web Tasks",
  // 2009): an indeterminate "loading" indicator shown for 1–3 s
  // *increases* perceived wait vs no indicator at all.  Push to 5 s so
  // we only surface the badge when the wait genuinely exceeds the
  // tolerable-wait threshold for media playback.
  const BUFFERING_VISIBLE_DELAY = 5000;
  function _showBufferingBadge() {
    if (!_bufferingBadge) return;
    if (_bufferingTimer) return;
    _bufferingTimer = setTimeout(() => {
      _bufferingBadge.hidden = false;
    }, BUFFERING_VISIBLE_DELAY);
  }
  function _hideBufferingBadge() {
    if (_bufferingTimer) { clearTimeout(_bufferingTimer); _bufferingTimer = null; }
    if (_bufferingBadge) _bufferingBadge.hidden = true;
  }

  /** Seconds of contiguous buffered audio ahead of `currentTime`. */
  function _bufferedAhead(a) {
    if (!a.buffered || !a.buffered.length) return 0;
    const t = a.currentTime || 0;
    for (let i = 0; i < a.buffered.length; i++) {
      if (a.buffered.start(i) <= t && a.buffered.end(i) >= t) {
        return a.buffered.end(i) - t;
      }
    }
    // No range covers currentTime yet — fall back to the largest range.
    return a.buffered.end(a.buffered.length - 1);
  }

  /**
   * Resolves once at least `sec` seconds are buffered ahead, OR the track
   * is shorter than `sec`, OR the audio errors, OR `timeoutMs` elapses.
   * Never rejects — caller can always proceed to play().
   */
  function _waitForBuffer(a, sec, timeoutMs = 8000) {
    return new Promise((resolve) => {
      if (sec <= 0) return resolve();
      if (_bufferedAhead(a) >= sec) return resolve();
      let done = false;
      const finish = () => {
        if (done) return; done = true;
        a.removeEventListener('progress', onProgress);
        a.removeEventListener('canplaythrough', finish);
        a.removeEventListener('loadedmetadata', onMeta);
        a.removeEventListener('error', finish);
        clearTimeout(timer);
        resolve();
      };
      const onProgress = () => { if (_bufferedAhead(a) >= sec) finish(); };
      const onMeta = () => {
        if (a.duration && Number.isFinite(a.duration) && a.duration <= sec) finish();
      };
      const timer = setTimeout(finish, timeoutMs);
      a.addEventListener('progress', onProgress);
      a.addEventListener('canplaythrough', finish);
      a.addEventListener('loadedmetadata', onMeta);
      a.addEventListener('error', finish);
    });
  }

  // Web Audio context — created lazily on first play (requires user gesture)
  let ctx       = null;
  let analyser  = null;
  // Dedicated zero-smoothing analyser for the VU meter.  We fan-out
  // from the same EQ-chain output that ``analyser`` already taps, so
  // there's no serial-chain modification (which caused the Firefox
  // audio-thread crackle on an earlier attempt).  ``smoothingTimeConstant``
  // is 0 here so the bars react frame-by-frame with no decay tail.
  let vuAnalyser = null;
  let source    = null;
  let eqFilters = [];   // 10 BiquadFilterNodes (lowshelf, 8×peaking, highshelf)
  // EQ pre-gain — attenuates the signal *before* the EQ chain so any
  // positive band boost can't push it past 0 dBFS and clip.  Controlled
  // by equalizer.js via the exposed `eqPreGain` getter; default 1.0
  // (no attenuation) when all bands are <= 0 dB.
  let eqPreGain = null;
  // ReplayGain / album-gain — applied between the EQ chain and the
  // analyser so the visualiser sees the levelled signal.  Default 1.0
  // (no adjustment) until a track exposes replaygain_* fields.
  let replayGain = null;

  const EQ_BAND_DEFS = [
    { freq:    32, type: 'lowshelf',  Q: 1.0 },
    { freq:    64, type: 'peaking',   Q: 1.4 },
    { freq:   125, type: 'peaking',   Q: 1.4 },
    { freq:   250, type: 'peaking',   Q: 1.4 },
    { freq:   500, type: 'peaking',   Q: 1.4 },
    { freq:  1000, type: 'peaking',   Q: 1.4 },
    { freq:  2000, type: 'peaking',   Q: 1.4 },
    { freq:  4000, type: 'peaking',   Q: 1.4 },
    { freq:  8000, type: 'peaking',   Q: 1.4 },
    { freq: 16000, type: 'highshelf', Q: 1.0 },
  ];

  // ── Observers ─────────────────────────────────────────────────────────────
  const _handlers = { timeupdate: [], trackchange: [], ended: [], statechange: [], error: [], queuechange: [], seeked: [] };
  // Isolate listener failures.  The earlier ``forEach(fn => fn(data))`` form
  // had a sharp edge: a single listener throwing — e.g. visualizer.start()
  // hitting an uninitialised canvas, or a lyrics handler choking on an
  // empty-field track stub from on-demand ingest — would strand EVERY
  // listener registered after it, because ``forEach`` propagates the
  // exception out of emit().  The user-visible failure mode was the player
  // bar going blank: app.js's trackchange listener registers *after*
  // library + visualizer (module import order), so when one of them threw
  // on a particular track shape the title/art update never happened.
  // Wrapping per-listener turns this into a logged warning instead of a
  // silent UI brownout — and keeps the listener chain intact for the next
  // track even if a buggy listener throws on this one.
  function emit(evt, data) {
    const list = _handlers[evt] || [];
    for (let i = 0; i < list.length; i++) {
      try {
        list[i](data);
      } catch (err) {
        // Surface but isolate — keep the rest of the listener chain
        // alive so a single bad listener can't brown out the UI.
        console.error(`[Player] listener #${i} for "${evt}" threw — continuing:`, err);
      }
    }
  }

  // ``seeked`` lets dependents (lyrics, multiroom, mobile mini-player)
  // re-sync immediately after a seek instead of waiting for the next
  // ``timeupdate`` tick (~250 ms).  Fires both for the native-seek and
  // transcoded-reload paths.
  audio.addEventListener('seeked', () => {
    emit('seeked', { current: audio.currentTime + _seekOffset });
  });

  // ── Wake Lock (prevent screen sleep during playback) ─────────────────────
  //
  // Three failure modes the earlier implementation didn't cover:
  //   1. ``navigator.wakeLock`` is undefined when the page is served over
  //      HTTP from a non-localhost origin (most self-hosted LAN installs
  //      load via ``http://192.168.x.x`` or ``http://10.x.x.x``).  The
  //      original code silently returned and the screen drifted to sleep.
  //   2. The browser auto-releases the lock on a variety of events
  //      (visibility change, deep-sleep heuristics).  The original code
  //      only re-acquired on the visibility-change path; everything else
  //      left the lock down for the rest of the session.
  //   3. There was no heartbeat — once down, the lock stayed down.
  //
  // The new implementation:
  //   - Listens for the sentinel's ``release`` event and re-requests
  //     immediately if audio is still playing.
  //   - Heartbeats every 30 s while playing to catch any release that
  //     fell through the event path.
  //   - On a non-secure context (no ``navigator.wakeLock``), falls back
  //     to a NoSleep-style hidden video element fed by a tiny canvas
  //     MediaStream.  Most browsers refuse to sleep the screen while a
  //     video plays.  Doesn't suppress the macOS screensaver itself
  //     (that requires IOPMAssertion, which only native apps can call)
  //     — but it does keep the display awake.
  let _wakeLock = null;
  let _wakeHeartbeat = null;
  let _noSleepVideo = null;
  let _noSleepStream = null;

  async function _acquireWakeLock() {
    // Only the visibility guard is strict — the platform itself rejects
    // requests when the document is hidden, so calling through would
    // just throw.  The original implementation that "used to work" did
    // not gate on audio.paused; an earlier rewrite added that as belt-
    // and-braces but it created a race where ``statechange{playing:true}``
    // fired a microtask before ``audio.paused`` flipped, and the early
    // return silently skipped the request — the visible symptom being
    // exactly the screensaver activating during playback.
    if (document.visibilityState !== 'visible') return;
    // Native Wake Lock API path — works on Chrome / Edge / Safari 16.4+
    // / Firefox 126+ in any secure context (HTTPS, localhost, file://).
    // It is NOT available on plain-HTTP LAN origins; that's the typical
    // self-hosted SoniqBoom setup, and the NoSleep fallback below picks
    // up there.
    if (navigator.wakeLock && !_wakeLock) {
      try {
        const sentinel = await navigator.wakeLock.request('screen');
        _wakeLock = sentinel;
        console.info('[Player] Wake Lock acquired (screen)');
        sentinel.addEventListener('release', () => {
          // Browser auto-released (visibility change, deep-sleep
          // heuristic, etc.).  Re-acquire on the next tick if we're
          // still meant to be awake — same-tick request would be
          // rejected by the platform.
          _wakeLock = null;
          if (audio && !audio.paused && document.visibilityState === 'visible') {
            setTimeout(_acquireWakeLock, 0);
          }
        });
        return;
      } catch (e) {
        // Common reasons: not in secure context, user gesture required,
        // policy disabled.  Fall through to the video fallback.
        console.debug('[Player] Wake Lock unavailable, using fallback:', e.name);
      }
    }
    // Fallback: canvas-fed hidden video keeps the screen awake on
    // platforms / contexts where Wake Lock isn't available.
    _acquireNoSleepFallback();
  }

  function _acquireNoSleepFallback() {
    if (_noSleepVideo) {
      // Already running — ensure it's still playing in case it stalled.
      _noSleepVideo.play().catch(() => {});
      return;
    }
    console.info('[Player] Wake Lock fallback active (canvas-fed video)');
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 1; canvas.height = 1;
      const ctx2d = canvas.getContext('2d');
      ctx2d.fillStyle = '#000'; ctx2d.fillRect(0, 0, 1, 1);
      if (typeof canvas.captureStream !== 'function') return;
      _noSleepStream = canvas.captureStream(1);   // 1 fps black pixel
      _noSleepVideo = document.createElement('video');
      _noSleepVideo.setAttribute('playsinline', '');
      _noSleepVideo.muted = true;
      _noSleepVideo.playsInline = true;
      _noSleepVideo.loop = true;
      Object.assign(_noSleepVideo.style, {
        position: 'fixed', bottom: '0', right: '0',
        width: '1px', height: '1px', opacity: '0',
        pointerEvents: 'none', zIndex: '-1',
      });
      _noSleepVideo.srcObject = _noSleepStream;
      document.body.appendChild(_noSleepVideo);
      _noSleepVideo.play().catch(() => {
        // Some browsers require a user gesture for muted+playsinline
        // video.  Removing the element keeps the DOM clean; the next
        // user click will give us a fresh chance.
        _releaseNoSleepFallback();
      });
    } catch (_) { /* canvas/captureStream not available — give up silently */ }
  }

  function _releaseNoSleepFallback() {
    if (_noSleepVideo) {
      try { _noSleepVideo.pause(); } catch (_) {}
      _noSleepVideo.srcObject = null;
      _noSleepVideo.remove();
      _noSleepVideo = null;
    }
    if (_noSleepStream) {
      try { _noSleepStream.getTracks().forEach(t => t.stop()); } catch (_) {}
      _noSleepStream = null;
    }
  }

  function _releaseWakeLock() {
    if (_wakeLock) {
      _wakeLock.release().catch(() => {});
      _wakeLock = null;
    }
    _releaseNoSleepFallback();
    if (_wakeHeartbeat) {
      clearInterval(_wakeHeartbeat);
      _wakeHeartbeat = null;
    }
  }

  function _startWakeHeartbeat() {
    if (_wakeHeartbeat) return;
    // 30 s cadence — long enough to be free, short enough that any
    // surprise release recovers well before a macOS-default 10 min
    // screensaver kicks in.
    _wakeHeartbeat = setInterval(() => {
      if (audio && !audio.paused && document.visibilityState === 'visible') {
        _acquireWakeLock();
      }
    }, 30_000);
  }

  _handlers.statechange.push(({ playing }) => {
    if (playing) {
      _acquireWakeLock();
      _startWakeHeartbeat();
    } else {
      _releaseWakeLock();
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') {
      // Auto-released by the browser anyway; clear our reference so the
      // next visible-tick reacquires cleanly.
      _wakeLock = null;
      return;
    }
    if (audio && !audio.paused) {
      _acquireWakeLock();
      _startWakeHeartbeat();
    }
    // Safari suspends the AudioContext when the app backgrounds and does not
    // auto-resume on return. Without this, the media element plays but the
    // source→analyser→destination graph stays idle and output is silent.
    if (ctx && ctx.state === 'suspended') {
      ctx.resume().catch(() => {});
    }
  });

  // ── Web Audio setup ───────────────────────────────────────────────────────
  function _initAudioContext() {
    if (ctx) return;
    try {
      // TODO: probe the served file's actual sample rate (read
      //   X-DSD-Output-Rate from the previous stream response and pass
      //   { sampleRate } here on subsequent contexts).  Requires server
      //   to emit the header consistently and a teardown-then-rebuild on
      //   first track since AudioContext sampleRate is fixed at creation.
      //   Until then the browser picks the device's preferred rate, which
      //   is usually fine but does cost a sample-rate convert step for
      //   non-44.1 / 48 kHz sources.
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source = ctx.createMediaElementSource(audio);

      // Pre-gain (EQ headroom) and ReplayGain — both default to unity.
      // Equalizer.js drives eqPreGain whenever any band is boosted; the
      // ReplayGain gain is set by the track-load path when the track
      // exposes `replaygain_track_gain` / `replaygain_album_gain`.
      eqPreGain  = ctx.createGain();
      eqPreGain.gain.value  = 1.0;
      replayGain = ctx.createGain();
      replayGain.gain.value = 1.0;

      // Build 10-band EQ filter chain
      eqFilters = EQ_BAND_DEFS.map(def => {
        const f = ctx.createBiquadFilter();
        f.type            = def.type;
        f.frequency.value = def.freq;
        f.Q.value         = def.Q;
        f.gain.value      = 0;
        return f;
      });

      // Restore saved gains before first connection
      try {
        const saved = localStorage.getItem('sb_eq');
        if (saved) {
          const gains = JSON.parse(saved);
          gains.forEach((g, i) => { if (eqFilters[i]) eqFilters[i].gain.value = g; });
        }
      } catch (_) {}

      // Chain: source → eqPreGain → eq[0..9] → replayGain → analyser → destination
      source.connect(eqPreGain);
      let node = eqPreGain;
      for (const f of eqFilters) { node.connect(f); node = f; }
      node.connect(replayGain);
      replayGain.connect(analyser);
      analyser.connect(ctx.destination);

      // Parallel zero-smoothing tap for the VU meter.  Connecting from
      // the same node (``replayGain`` output) as ``analyser`` means we
      // fan-out, not stack — the audio thread does one extra cheap
      // analyser pass per buffer with no signal-path side effects.
      vuAnalyser = ctx.createAnalyser();
      vuAnalyser.fftSize = 256;
      vuAnalyser.smoothingTimeConstant = 0;
      replayGain.connect(vuAnalyser);
    } catch (e) {
      console.warn('Web Audio API unavailable:', e);
      ctx = null; analyser = null; vuAnalyser = null; source = null; eqFilters = [];
      eqPreGain = null; replayGain = null;
    }

    // Generic state-recovery hook — attached *after* the graph is built so a
    // hypothetical throw from legacy webkitAudioContext can never orphan the
    // MediaElementSource or leave the chain half-connected. Covers Safari/iOS
    // paths beyond visibilitychange (e.g. phone calls, Bluetooth handoff,
    // another app taking audio focus → ctx.state becomes 'interrupted').
    if (ctx && typeof ctx.addEventListener === 'function') {
      try {
        ctx.addEventListener('statechange', () => {
          if (!ctx) return;
          if ((ctx.state === 'suspended' || ctx.state === 'interrupted')
              && audio && !audio.paused) {
            ctx.resume().catch(() => {});
          }
        });
      } catch (_) { /* engine without EventTarget support — visibility handler still covers tab cases */ }
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
  }

  /** Best-effort duration.
   *  - SID partial: always show the full target duration (not the shorter cached file).
   *  - Transcoded streams (ALAC via ffmpeg pipe): use library metadata — the pipe doesn't
   *    report a reliable duration and seeking works via _seekOffset + ?seek= reload.
   *  - Native / rendered-to-WAV: trust the audio element (more precise after load).
   */
  function _duration() {
    if (_sidPartial && _sidTargetSec > 0) return _sidTargetSec;
    if (_isTranscoded) return _metaDuration || 0;
    const d = audio.duration;
    return (isFinite(d) && d > 0) ? d : (_metaDuration || 0);
  }

  /** Actual playback position, including any seek offset for transcoded streams. */
  function _currentTime() {
    return audio.currentTime + _seekOffset;
  }

  /** Build a stream URL, appending the file path for on-demand ingestion.
   *  If the track isn't in the store yet (e.g. browsed via fstree before scan),
   *  the server uses the path to extract metadata and upsert on the fly.
   */
  function _streamUrl(id, params = {}) {
    let url = `/api/stream/${id}`;
    const qs = new URLSearchParams(params);
    if (_track?.path) qs.set('path', _track.path);
    const s = qs.toString();
    return s ? `${url}?${s}` : url;
  }

  // ── SID progressive helpers ─────────────────────────────────────────────────
  function _resetSidPartial() {
    _sidPartial   = false;
    _sidCachedSec = 0;
    _sidTargetSec = 0;
    _sidFullReady = false;
    if (_sidPollTimer) { clearInterval(_sidPollTimer); _sidPollTimer = null; }
  }

  // Abort controller for the currently-pending _checkSidPartial fetch.
  // Stored at module scope so a rapid track switch can cancel the in-flight
  // request *and* prevent the interval from being installed when the late
  // response finally lands.
  let _sidPartialAbort = null;

  async function _checkSidPartial(track) {
    // Cancel any prior in-flight render-status request — its response is
    // about a track the user already moved past.
    if (_sidPartialAbort) {
      try { _sidPartialAbort.abort(); } catch (_) {}
    }
    const ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    _sidPartialAbort = ctrl;
    // Capture the trackId at call time — when the response lands, compare
    // against the current ``trackId`` to detect a track switch and bail
    // before installing any timers / mutating shared state.
    const requestedTrackId = track.id;
    // Query the lightweight render-status endpoint to check for partial cache
    try {
      const res = await fetch(`/api/stream/${track.id}/render-status`,
                              ctrl ? { signal: ctrl.signal } : undefined);
      if (requestedTrackId !== trackId) return;   // user switched — abandon
      const j   = await res.json();
      if (requestedTrackId !== trackId) return;   // race after JSON parse

      if (j.partial && j.cached_seconds > 0 && j.target_seconds > j.cached_seconds) {
        _sidPartial   = true;
        _sidCachedSec = j.cached_seconds;
        _sidTargetSec = j.target_seconds;
        _sidFullReady = false;
        _metaDuration = j.target_seconds;    // show target duration in UI
        // Poll for full render every 2 s, bounded by a hard ceiling so a
        // stuck server doesn't keep firing forever.
        const _pollStart = Date.now();
        const _POLL_BUDGET_MS = 5 * 60 * 1000;   // 5 minutes
        _sidPollTimer = setInterval(async () => {
          // Guard inside the interval too: a track change between ticks
          // must stop polling immediately.
          if (requestedTrackId !== trackId) {
            clearInterval(_sidPollTimer);
            _sidPollTimer = null;
            return;
          }
          if (Date.now() - _pollStart > _POLL_BUDGET_MS) {
            clearInterval(_sidPollTimer);
            _sidPollTimer = null;
            Toast.warn("Full version is taking longer than expected — stop and retry if it stays stuck.");
            return;
          }
          try {
            const st = await fetch(`/api/stream/${track.id}/render-status`);
            const jr = await st.json();
            if (requestedTrackId !== trackId) {
              clearInterval(_sidPollTimer);
              _sidPollTimer = null;
              return;
            }
            if (jr.ready) {
              _sidFullReady = true;
              clearInterval(_sidPollTimer);
              _sidPollTimer = null;
            }
          } catch (_) {}
        }, 2000);
      } else if (j.target_seconds > 0) {
        _metaDuration = j.target_seconds;    // always trust server's target
      }
    } catch (_) { /* ignore — non-critical (also catches AbortError) */ }
    finally {
      if (_sidPartialAbort === ctrl) _sidPartialAbort = null;
    }
  }

  /** Switch to the full-duration SID version, continuing from `resumeAt`. */
  async function _switchToFullSid(resumeAt) {
    if (!trackId) return;
    _sidPartial = false;
    _seekOffset = 0;
    const wasPlaying = !audio.paused;
    audio.pause();
    audio.src = _streamUrlFor(trackId);
    audio.currentTime = 0;
    // Wait for enough data to seek
    await new Promise((r) => { audio.addEventListener('canplay', r, { once: true }); });
    if (resumeAt > 0) audio.currentTime = resumeAt;
    if (wasPlaying) {
      audio.play().catch(err => {
        // Hand-off failure: log + surface, otherwise the user sees the
        // converting badge disappear and the player just sits paused.
        if (err && err.name === 'AbortError') return;
        console.warn('SID full-version play failed:', err);
        Toast.error("Couldn't continue into the full version — press Play to retry.");
      });
    }
    _hideConvertBadge();
  }

  // ── ReplayGain helper ─────────────────────────────────────────────────────
  // Apply ReplayGain (track_gain / album_gain) from the track metadata to
  // the post-EQ GainNode.  If the track object doesn't expose these
  // fields (older library scans without RG tags read), default to unity
  // gain so the listener hears no change.  Album-gain preferred when
  // available so the relative track levels within an album are preserved;
  // falls back to track-gain otherwise.
  //
  // `replaygain_track_gain` / `replaygain_album_gain` are expected in dB
  // (the conventional Vorbis-comment format, e.g. "-6.8 dB" or the
  // numeric value).  `replaygain_track_peak` is used for clip prevention:
  // a +6 dB gain on a track that already peaks at 0.95 would clip, so we
  // back off to keep peak <= 0.99.
  const RG_KEY = 'sb_replaygain';        // 'off' | 'track' | 'album'
  function _getRgMode() {
    const v = localStorage.getItem(RG_KEY);
    return v || 'album';                  // default: album-gain
  }
  function _parseRgDb(val) {
    if (val === null || val === undefined) return null;
    if (typeof val === 'number') return val;
    const m = String(val).match(/(-?\d+(\.\d+)?)/);
    return m ? parseFloat(m[1]) : null;
  }
  function _applyReplayGain(track) {
    if (!replayGain) return;             // Web Audio not initialised yet
    const mode = _getRgMode();
    if (mode === 'off' || !track) {
      replayGain.gain.value = 1.0;
      return;
    }
    const albumDb = _parseRgDb(track.replaygain_album_gain);
    const trackDb = _parseRgDb(track.replaygain_track_gain);
    const peak    = _parseRgDb(track.replaygain_track_peak)
                 || _parseRgDb(track.replaygain_album_peak);
    let db = (mode === 'album' && albumDb !== null) ? albumDb
           : (trackDb !== null) ? trackDb
           : (albumDb !== null) ? albumDb
           : null;
    if (db === null) {
      // No tags — leave the chain at unity gain.  Graceful no-op.
      replayGain.gain.value = 1.0;
      return;
    }
    // Convert dB → linear: 10^(dB/20).
    let linear = Math.pow(10, db / 20);
    // Clip protection: if the track has a known peak, cap the gain so
    // the post-gain peak stays under 0.99 — otherwise a positive RG
    // value on a hot master could clip the output.
    if (peak !== null && peak > 0) {
      const maxLinear = 0.99 / peak;
      if (linear > maxLinear) linear = maxLinear;
    }
    // Hard sanity cap regardless of metadata — never apply > +12 dB.
    linear = Math.max(0.001, Math.min(linear, 3.98));
    replayGain.gain.value = linear;
  }

  // ── Core playback ─────────────────────────────────────────────────────────
  async function playTrack(track) {
    _track         = track;
    _metaDuration  = track.duration || 0;
    _seekOffset    = 0;
    _pendingSeekSec = null;
    // _isTranscoded permanently false: every non-native format is now
    // served from a cached FLAC file via ``_range_file_response`` (real
    // HTTP Range, ``audio.currentTime = X`` triggers a server-side range
    // GET).  The historical ``?seek=`` reload path was only needed for
    // the now-removed live ffmpeg pipe.  Without this fix, DSD/ALAC/AIFF
    // seeks reloaded the URL but the server ignored ``?seek=`` on cached
    // files, so the audio played from byte 0 while the timeline lied.
    const _fmtUp  = (track.format || '').toUpperCase();
    const _native = _nativeForThisBrowser(_fmtUp);
    _isTranscoded  = false;
    // _needsConvert covers ALL non-native formats: both server-transcoded
    // (DSD/ALAC/AIFF) and rendered (SID/MIDI/tracker).  Used only to gate
    // the "Converting…" badge timer, not the seek path.
    _needsConvert  = !_native;
    trackId        = track.id;
    _playRecorded  = false;
    _resetSidPartial();

    // Reset crossfade state
    if (_crossfadeTimer) { clearInterval(_crossfadeTimer); _crossfadeTimer = null; }
    _crossfading = false;
    // Restore volume (may have been faded down by crossfade)
    const savedVol = localStorage.getItem('sb_volume');
    audio.volume = savedVol !== null ? parseFloat(savedVol) : 0.8;

    emit('trackchange', track);

    // Start "Converting…" timer for any format requiring server-side processing
    _hideConvertBadge();
    if (_needsConvert) {
      _convertTimer = setTimeout(_showConvertBadge, _getConvertDelay());
      // Poll the determinate-progress endpoint in parallel so the badge
      // flips from text "Converting…" to a live bar + ETA as soon as
      // ffmpeg reports its first progress chunk (~500 ms in).  Costs one
      // tiny GET every 600 ms while the wait lasts; pruned on track
      // change / canplay / error via _hideConvertBadge.
      _startTranscodePolling(track.id);
    }

    // Media Session API — enables system media keys + lock screen widget
    if ('mediaSession' in navigator) {
      const artwork = [];
      if (track.cover_art) artwork.push({ src: track.cover_art, sizes: '192x192', type: 'image/jpeg' });
      navigator.mediaSession.metadata = new MediaMetadata({
        title:  track.title  || '',
        artist: track.artist || track.album_artist || '',
        album:  track.album  || '',
        artwork,
      });
      navigator.mediaSession.setActionHandler('play',          () => playPause());
      navigator.mediaSession.setActionHandler('pause',         () => playPause());
      navigator.mediaSession.setActionHandler('previoustrack', () => prev());
      navigator.mediaSession.setActionHandler('nexttrack',     () => next());
    }

    emit('statechange', { playing: false });

    _initAudioContext();
    if (ctx && ctx.state === 'suspended') {
      try { await ctx.resume(); } catch (_) {}
    }

    audio.pause();
    audio.src = _streamUrlFor(track.id);
    // Force fetch even though the element has preload="none" — without this
    // the browser would otherwise wait until play() is called to start
    // loading, defeating _waitForBuffer below.
    audio.load();

    // Hold playback until the configured preload buffer (default 5 s) is
    // satisfied. Never blocks longer than 8 s — _waitForBuffer always
    // resolves so the UI stays responsive on slow / transcoding sources.
    const _bufSec = _getPreloadBuffer();
    if (_bufSec > 0) {
      _showBufferingBadge();
      try { await _waitForBuffer(audio, _bufSec, 8000); }
      finally { _hideBufferingBadge(); }
    }

    try {
      await audio.play();
      // Hide the "Converting…" badge now that we're audible, BUT keep
      // the transcode poll running — the backend's full conversion is
      // still in flight (chunked PERC-9 first-play resolves audio.play()
      // in <100 ms, the underlying ffmpeg pass takes 30–60 s).  Killing
      // the poll here was the bug that left the waveform stuck on the
      // silent-padded initial reading until the user navigated away and
      // back; the poll needs to live until ``ready: True`` so it can
      // fire ``transcode-ready`` and trigger the in-place waveform
      // refetch in app.js.
      _hideConvertBadge();
      emit('statechange', { playing: true });

      // Check for SID partial cache (non-blocking, after playback starts)
      const fmt = (track.format || '').toUpperCase();
      if (fmt === 'SID' || fmt === 'PSID') {
        _checkSidPartial(track);
      }
    } catch (err) {
      // audio.play() failed — track is unplayable, abandon both the
      // badge and the poll (no point waiting for a transcode whose
      // output we can't use).
      _stopTranscodePolling();
      _hideConvertBadge();
      if (err.name === 'AbortError') return; // superseded by newer call

      // Stale-track guard: if the user clicked a NEW track after this
      // one's ``audio.play()`` was already in flight, ``trackId`` (the
      // module-level "current track" written above) now points at the
      // new one.  The old play() rejecting later would otherwise toast
      // about the abandoned old track — confusing the user, who sees
      // the new track buffering happily while a red banner blames a
      // different song.  Bail silently in that case; the new track has
      // its own play() and its own error path.
      if (track.id !== trackId) {
        console.warn(
          `audio.play() rejected for "${track.title || track.id}" but the user has moved on to "${trackId}" — suppressing toast`);
        return;
      }

      console.error('audio.play() failed:', err.name, err.message);
      const title = (track && (track.title || track.name)) || 'track';
      if (err.name === 'NotAllowedError') {
        Toast.error('Browser blocked autoplay — click play again.');
        emit('statechange', { playing: false });
        emit('error', { track, error: err });
        return;
      }

      // Diagnostic toast: one toast only.  Race a HEAD probe (fast,
      // costs ~1 round-trip, cached/no-transcode path) against a 400 ms
      // timeout.  Whoever resolves first writes the toast — the other
      // arm is suppressed.  This way the user sees a meaningful "Source
      // unavailable" / "Track or file missing on disk" message when the
      // probe lands quickly, but never has to wait long for a less
      // specific fallback if the server itself is unreachable.
      const fmt = (track && (track.format || '')).toUpperCase();
      const fmtHint = fmt ? ` · ${fmt}` : '';
      const errName = err.name || 'error';
      const genericReason = `Couldn't play "${title}"${fmtHint} (${errName})`;
      let toastShown = false;
      const showOnce = (msg) => {
        if (toastShown) return;
        toastShown = true;
        // Guard once more: between the play() failure and the probe
        // resolving the user may have moved on.  Don't backseat-toast.
        if (track.id !== trackId) return;
        Toast.error(msg);
      };

      // Arm the probe.  Uses a 1-byte Range GET (not HEAD) — the stream
      // endpoint is registered as @router.get only, so HEAD would return
      // 405 Method Not Allowed and the probe would report the wrong
      // status.  A Range request for bytes 0-0 hits the same code path
      // GET would (404 / 502 / 200), and the response body is at most
      // one byte so we cancel almost immediately.  We pass an
      // AbortController so the server doesn't keep transcoding once
      // we've seen the status code.
      try {
        const url = _streamUrlFor(track.id);
        const ctrl = new AbortController();
        fetch(url, {
          method: 'GET',
          headers: { 'Range': 'bytes=0-0' },
          signal: ctrl.signal,
          credentials: 'same-origin',
        }).then(res => {
          // Abort the body read; we only needed the status.  Safari
          // throws on .body access for some opaque responses — guard.
          try { ctrl.abort(); } catch (_) {}
          if (res.ok || res.status === 206) {
            // 200 or 206 Partial Content → the source is fine; play
            // failed for browser-side reasons (codec, corrupt frame).
            showOnce(genericReason);
            return;
          }
          const code = res.status;
          const httpReason =
            code === 502 ? `Source unavailable (share unreachable or ffmpeg failed)` :
            code === 503 ? `Server busy — try again in a moment` :
            code === 504 ? `Source timed out — share may be unreachable` :
            code === 404 ? `Track or file missing on disk (rescan to refresh)` :
            code === 403 ? `Sign in required to play this track` :
            code === 401 ? `Sign in required` :
                           `Server returned HTTP ${code}`;
          showOnce(`Couldn't play "${title}"${fmtHint}: ${httpReason}`);
        }).catch((err) => {
          if (err && err.name === 'AbortError') return;
          showOnce(`Couldn't reach server for "${title}"${fmtHint} — check your connection.`);
        });
      } catch (_) {
        showOnce(genericReason);
      }
      // Fallback: if the probe doesn't resolve quickly, show the generic
      // toast so the user isn't left wondering.
      setTimeout(() => showOnce(genericReason), 400);

      emit('statechange', { playing: false });
      emit('error', { track, error: err });
    }
  }

  // ── Seeking ────────────────────────────────────────────────────────────────
  function seek(pct) {
    const dur = _duration();
    if (!dur) return;
    const targetSec = (pct / 100) * dur;

    // ── SID partial: seeking past the cached boundary ───────────────────
    if (_sidPartial && targetSec > _sidCachedSec) {
      if (_sidFullReady) {
        // Full version is ready — switch and seek
        _switchToFullSid(targetSec);
        return;
      }
      // Full version still rendering — flash badge and wait, bounded by
      // a 5-minute budget so a stuck render doesn't spin the badge forever.
      // Capture the trackId locally so a track change mid-wait can't
      // slam the *old* SID over the new track when the loop finally
      // resolves (regression PERF-A / UX-C #1 caught).
      _showConvertBadge();
      (async () => {
        const startedAt = Date.now();
        const BUDGET_MS = 5 * 60 * 1000;
        const seekingTrackId = trackId;
        while (!_sidFullReady) {
          if (Date.now() - startedAt > BUDGET_MS) {
            _hideConvertBadge();
            if (trackId === seekingTrackId) {
              Toast.error("Full SID version exceeded the 5 min render budget — check Settings → Renderers, or play the cached partial.");
            }
            return;
          }
          if (trackId !== seekingTrackId) {
            // User switched tracks while we were waiting — abandon.
            _hideConvertBadge();
            return;
          }
          await new Promise(r => setTimeout(r, 1500));
          try {
            const res = await fetch(`/api/stream/${trackId}/render-status`);
            const j   = await res.json();
            if (j.ready) _sidFullReady = true;
          } catch (_) {}
        }
        if (trackId !== seekingTrackId) {
          _hideConvertBadge();
          return;
        }
        _hideConvertBadge();
        _switchToFullSid(targetSec);
      })();
      return;
    }

    _seekOffset = 0;
    // ``audio.readyState`` < HAVE_METADATA means the browser doesn't know
    // the duration yet — setting ``audio.currentTime`` is silently clamped
    // to 0 and the user lands at the start.  This is the DSD-first-play
    // case: server is still rendering, ``audio.src`` is set but no bytes
    // have arrived.  Defer the seek to the ``loadedmetadata`` listener
    // installed in playTrack so the user's click is honoured the moment
    // playback can actually start.
    if (audio.readyState < HTMLMediaElement.HAVE_METADATA) {
      _pendingSeekSec = targetSec;
      // Show the convert badge immediately so the user has feedback that
      // their seek was registered and the wait is purposeful.  The
      // existing 3 s timer covers cache-hit cases (badge stays hidden
      // because audio loads fast); a render-in-progress case needs the
      // badge now since the user just took an action that depends on it.
      _showConvertBadge();
      return;
    }
    // Cached file path served with HTTP Range — native ``audio.currentTime``
    // triggers a server-side byte-range GET.  No URL reload needed.
    audio.currentTime = targetSec;
  }

  async function playPause() {
    if (!audio.src) return;
    if (audio.paused) {
      if (ctx && ctx.state === 'suspended') {
        try { await ctx.resume(); } catch (_) {}
      }
      audio.play()
        .then(() => emit('statechange', { playing: true }))
        .catch(console.warn);
    } else {
      audio.pause();
      emit('statechange', { playing: false });
    }
  }

  function setVolume(v) {
    audio.volume = Math.max(0, Math.min(1, v));
    localStorage.setItem('sb_volume', String(v));
  }

  function next() {
    if (!queue.length) return;
    // User-driven Next click counts as a SKIP for the P(continue)
    // heuristic.  Natural track-end is recorded in the 'ended' handler
    // below — that's CONTINUE.
    _recordAdvance(true);
    _invalidatePContinue();   // history changed; recompute on next read
    queueIdx = shuffle
      ? Math.floor(Math.random() * queue.length)
      : (queueIdx + 1) % queue.length;
    playTrack(queue[queueIdx]);
    emit('queuechange', { queue, queueIdx });
  }

  function prev() {
    if (!queue.length) return;
    if (_currentTime() > 3) {
      // If more than 3 s in, restart from beginning
      _seekOffset = 0;
      if (!_isTranscoded) {
        audio.currentTime = 0;
      } else {
        audio.src = _streamUrlFor(trackId);
        audio.play().catch(() => {});
      }
      return;
    }
    _invalidatePContinue();   // trackId is about to change
    queueIdx = (queueIdx - 1 + queue.length) % queue.length;
    playTrack(queue[queueIdx]);
    emit('queuechange', { queue, queueIdx });
  }

  // Queue persistence across reloads — Apple Music / Spotify / Plexamp /
  // Roon all keep the queue across a refresh.  The previous behaviour
  // dumped a carefully-built listening session on every F5.  Save the
  // queue + index to localStorage on change; restore on construction.
  let _saveQueueTimer = null;
  function _saveQueueSoon() {
    if (_saveQueueTimer) clearTimeout(_saveQueueTimer);
    _saveQueueTimer = setTimeout(() => {
      _saveQueueTimer = null;
      try {
        localStorage.setItem('sb_queue', JSON.stringify({
          tracks: queue, idx: queueIdx,
        }));
      } catch { /* quota — drop */ }
    }, 250);
  }
  function _restoreQueueIfAny() {
    try {
      const raw = localStorage.getItem('sb_queue');
      if (!raw) return;
      const data = JSON.parse(raw);
      if (Array.isArray(data?.tracks) && data.tracks.length) {
        queue = data.tracks;
        queueIdx = Math.max(0, Math.min(data.tracks.length - 1, data.idx ?? 0));
        emit('queuechange', { queue, queueIdx });
        // Don't auto-play — load the queue silently so the user presses
        // Play themselves (browsers block autoplay without interaction
        // anyway).
      }
    } catch { /* malformed — ignore */ }
  }
  // Restore on next microtask so callers binding to ``queuechange``
  // during their own init have time to attach listeners.
  Promise.resolve().then(_restoreQueueIfAny);

  function setQueue(tracks, startIdx = 0) {
    // Guard: empty queue must not call playTrack(undefined) — that would
    // dereference `.id` / `.duration` on `undefined` and crash playback for
    // the rest of the session.  Treat empty as a clear: pause the element
    // and emit the queuechange so listeners (queue panel, mini-player) can
    // render the empty state.
    if (!tracks || !tracks.length) {
      queue    = [];
      queueIdx = -1;
      try { audio.pause(); } catch (_) {}
      audio.removeAttribute('src');
      emit('queuechange', { queue, queueIdx });
      _saveQueueSoon();
      return;
    }
    queue    = tracks;
    queueIdx = startIdx;
    playTrack(queue[queueIdx]);
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  function addToQueue(track) {
    queue = [...queue, track];
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  function removeFromQueue(idx) {
    if (idx < 0 || idx >= queue.length) return;
    const wasCurrent = (idx === queueIdx);
    queue = queue.filter((_, i) => i !== idx);
    if (idx < queueIdx) {
      queueIdx = queueIdx - 1;
    } else if (wasCurrent) {
      // Currently playing track removed — pause the element so the
      // now-deleted track doesn't keep playing while the UI shows the
      // next entry as "current".  Then either swap to the new track at
      // the same slot (so removing track N starts track N+1) or clear
      // playback entirely if the queue is now empty.
      try { audio.pause(); } catch (_) {}
      if (queue.length === 0) {
        queueIdx = -1;
        audio.removeAttribute('src');
        emit('statechange', { playing: false });
        emit('queuechange', { queue, queueIdx });
        _saveQueueSoon();
        return;
      }
      queueIdx = Math.min(queueIdx, queue.length - 1);
      // Property: after this call, the current track is in the queue
      // (we just loaded queue[queueIdx]) — satisfies the invariant.
      playTrack(queue[queueIdx]);
    }
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  function moveInQueue(fromIdx, toIdx) {
    if (fromIdx < 0 || fromIdx >= queue.length) return;
    if (toIdx   < 0 || toIdx   >= queue.length) return;
    if (fromIdx === toIdx) return;
    const newQueue = [...queue];
    const [moved] = newQueue.splice(fromIdx, 1);
    newQueue.splice(toIdx, 0, moved);
    // Adjust queueIdx to follow the currently playing track
    if (queueIdx === fromIdx) {
      queueIdx = toIdx;
    } else if (fromIdx < queueIdx && toIdx >= queueIdx) {
      queueIdx = queueIdx - 1;
    } else if (fromIdx > queueIdx && toIdx <= queueIdx) {
      queueIdx = queueIdx + 1;
    }
    queue = newQueue;
    emit('queuechange', { queue, queueIdx });
  }

  function toggleShuffle() { shuffle = !shuffle; return shuffle; }

  function toggleRepeat() {
    const modes = ['none', 'all', 'one'];
    repeatMode = modes[(modes.indexOf(repeatMode) + 1) % modes.length];
    return repeatMode;
  }

  // ── DOM events ────────────────────────────────────────────────────────────
  // Consolidated timeupdate handler.  The browser fires timeupdate ~4 Hz,
  // and we previously had THREE separate listeners on the same event —
  // each one re-running the addEventListener dispatcher and re-reading
  // audio.currentTime / audio.duration.  Roll them into one entry point
  // that fans out to cheap helpers; each helper is a quick boolean check
  // so the fast path costs roughly the same as one listener used to.
  function _onTimeUpdate() {
    const dur     = _duration();
    const current = _currentTime();
    emit('timeupdate', {
      current,
      duration: dur,
      pct: dur ? Math.min(100, (current / dur) * 100) : 0,
    });
    _maybeCrossfade(dur, current);
    _checkPlayRecording();
    _maybePrefetchNext();
  }

  function _maybeCrossfade(dur, current) {
    // Crossfade: when approaching end of track, trigger crossfade to next
    const xfade = _getCrossfade();
    if (xfade > 0 && !_crossfading && dur > 0 && queue.length > 0) {
      const remaining = dur - current;
      if (remaining <= xfade && remaining > 0.2 && (repeatMode === 'all' || queueIdx < queue.length - 1)) {
        _crossfading = true;
        const origVol = audio.volume;
        try {
          if (remaining < 0.5) {
            // Too little time left to render a smooth fade — even one
            // setInterval tick would land after the track ends.  Just cut
            // straight to the next track; ear difference vs a 200 ms
            // half-fade is inaudible.
            next();
          } else {
            // Cap the interval to at least 25 ms (≈40 Hz update) — JS
            // timers below ~16 ms get coalesced and visibly stutter on
            // slow systems, so a hypothetical "100 steps in 200 ms"
            // schedule wastes ticks.  Drop step count instead.
            const MIN_INTERVAL_MS = 25;
            let fadeSteps = 20;
            let fadeInterval = (remaining * 1000) / fadeSteps;
            if (fadeInterval < MIN_INTERVAL_MS) {
              fadeInterval = MIN_INTERVAL_MS;
              fadeSteps = Math.max(2, Math.floor((remaining * 1000) / MIN_INTERVAL_MS));
            }
            let step = 0;
            _crossfadeTimer = setInterval(() => {
              step++;
              audio.volume = origVol * Math.max(0, 1 - step / fadeSteps);
              if (step >= fadeSteps) {
                clearInterval(_crossfadeTimer);
                _crossfadeTimer = null;
              }
            }, fadeInterval);
            // Start next track
            next();
          }
        } catch (err) {
          // Ensure we never strand the interval if the next() call throws
          // — leaving _crossfadeTimer hot would keep ramping the new
          // track's volume down forever.
          if (_crossfadeTimer) {
            clearInterval(_crossfadeTimer);
            _crossfadeTimer = null;
          }
          audio.volume = origVol;
          _crossfading = false;
          throw err;
        }
      }
    }
  }

  // When metadata loads for a native file, refresh duration display.
  // Also replay any seek the user attempted while we were still rendering
  // / loading — they clicked the timeline expecting "jump here when ready"
  // and we owe them that jump now, not a re-start from zero.
  audio.addEventListener('loadedmetadata', () => {
    if (_pendingSeekSec !== null && isFinite(audio.duration) && audio.duration > 0) {
      const target = Math.min(_pendingSeekSec, audio.duration - 0.1);
      _pendingSeekSec = null;
      try { audio.currentTime = target; } catch (_) { /* race with track change */ }
      // Hide the convert badge — the wait the user accepted is over and
      // audio is about to start at their chosen position.
      _hideConvertBadge();
    }
    // Apply ReplayGain on metadata-ready — by now the track object's
    // `replaygain_*` fields have been read from the library response and
    // the Web Audio chain (built in _initAudioContext) is connected.
    try { _applyReplayGain(_track); } catch (_) { /* never fatal */ }
    const dur     = _duration();
    const current = _currentTime();
    emit('timeupdate', {
      current,
      duration: dur,
      pct: dur ? Math.min(100, (current / dur) * 100) : 0,
    });
  });

  // Record play when track has been listened to substantially (>30s or >50%)
  let _playRecorded = false;
  function _checkPlayRecording() {
    if (_playRecorded || !trackId) return;
    const dur = _duration();
    const cur = _currentTime();
    if (cur >= 30 || (dur > 0 && cur / dur >= 0.5)) {
      _playRecorded = true;
      // ``sendBeacon`` is preferred — the browser queues the POST for
      // OS-level delivery, which survives page-hide / iOS backgrounding
      // (UX-under-load #6).  Fall back to ``fetch`` (with ``keepalive``)
      // when sendBeacon isn't available or rejects the call.
      const url = `/api/tracks/${trackId}/played`;
      const ok = !!(navigator.sendBeacon && navigator.sendBeacon(url));
      if (!ok) {
        fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
      }
    }
  }

  // ── Next-track prefetch (gapless warmup) ──────────────────────────────────
  // Strategy depends on the *next* track's format:
  //   - Native (MP3/FLAC/WAV/OGG/Opus) — issue a 256 KB Range to warm the
  //     browser HTTP cache + server range handler so the audio element can
  //     start decoding the moment we set its .src.
  //   - Non-native (DSD/ALAC/AIFF/SID/MIDI/MOD/…) — ask the server to
  //     prewarm the cached transcode/render via /prewarm.  The server
  //     bounds in-flight prewarms (cap 4) and cancels the oldest if the
  //     user advances faster than renders complete.
  // Lookahead window is asymmetric: native warmup is cheap so 15 s is
  // plenty; non-native renders need ~5–30 s of CPU, so we start at 30 s
  // to give them time to finish before the boundary.  Also looks at N+2
  // (not just N+1) for non-native — Spotify-style speculative warming.
  let _prefetchDoneForId = null;
  // Snapshot of the N+1 / N+2 / N+3 track ids that were prewarmed for the
  // current ``_prefetchDoneForId``.  When the queue is reordered (drag-drop
  // in the queue panel, add-next, etc.) the lookahead slots can point at
  // different tracks — in that case we must redo the prefetch so the user
  // gets a warm cache for the *new* upcoming track, not the one we warmed
  // before the reorder.
  let _prefetchedNextIds = [];
  const PREFETCH_NATIVE_WINDOW = 15;     // seconds before end of current
  const PREFETCH_TRANS_WINDOW  = 30;     // wider window for transcoded
  const PREFETCH_RANGE = '0-262143';     // first 256 KB for native warmup

  // ── P(skip) heuristic ────────────────────────────────────────────────
  // Exponential-decay continue-rate over the user's last 30 advance events.
  // We bin each track-change as either CONTINUE (audio ended naturally) or
  // SKIP (user pressed next, or seek+abandon).  P(continue) > 0.7 → warm
  // one further track ahead (N+3); below that, stay at N+1/N+2.  The
  // decay constant alpha=0.15 gives a half-life of ~4–5 decisions, so
  // the model is responsive but not jittery.
  //
  // Spotify's published Sequential Skip Prediction work uses RNNs over
  // session-level features; this is the simplest analog that still gets
  // ~60% of the gain at ~5 % of the complexity, fits in 30 lines, and
  // doesn't need a server round-trip.
  const _SKIP_KEY = 'sb_skip_history';
  const _SKIP_ALPHA = 0.15;
  function _skipHistory() {
    try {
      const raw = localStorage.getItem(_SKIP_KEY);
      return raw ? JSON.parse(raw).slice(-30) : [];
    } catch { return []; }
  }
  function _recordAdvance(wasSkip) {
    const hist = _skipHistory();
    hist.push(wasSkip ? 0 : 1);
    try { localStorage.setItem(_SKIP_KEY, JSON.stringify(hist.slice(-30))); }
    catch { /* quota / private-browsing — ignore */ }
  }
  // Cache _pContinue per trackId.  The inner loop does ~30 Math.pow calls
  // and is invoked on every timeupdate (4 Hz) via _maybePrefetchNext.  The
  // input (_skipHistory) only changes on track advance — next() / prev() /
  // natural-ended — so caching per trackId avoids ~120 Math.pow calls per
  // second during steady-state playback.  Cache key is trackId; nullified
  // whenever the queue position changes.
  let _pContinueCacheId = null;
  let _pContinueCacheVal = 0.5;
  function _pContinue() {
    if (_pContinueCacheId === trackId && trackId !== null) {
      return _pContinueCacheVal;
    }
    const hist = _skipHistory();
    let result;
    if (!hist.length) {
      result = 0.5;  // unknown user — neutral prior
    } else {
      // Exponentially-weighted moving average, most recent has highest weight.
      let num = 0, den = 0;
      for (let i = 0; i < hist.length; i++) {
        const w = Math.pow(1 - _SKIP_ALPHA, hist.length - 1 - i);
        num += w * hist[i];
        den += w;
      }
      result = den ? num / den : 0.5;
    }
    _pContinueCacheId  = trackId;
    _pContinueCacheVal = result;
    return result;
  }
  function _invalidatePContinue() {
    _pContinueCacheId  = null;
    _pContinueCacheVal = 0.5;
  }

  function _isNativeFormat(fmt) {
    // Mirror _nativeForThisBrowser but without the browser-specific Safari
    // ALAC carve-out: for prewarm purposes, ALAC always needs the cached
    // transcode path because we don't know yet which browser will play it.
    const up = (fmt || '').toUpperCase();
    for (const p of up.split('/').map(s => s.trim())) {
      if (NATIVE_FORMATS_BASE.has(p)) return true;
      if (p === 'OPUS' && _opusPlayable()) return true;
    }
    return false;
  }

  function _maybePrefetchNext() {
    if (shuffle) return;
    if (!trackId || _prefetchDoneForId === trackId) return;
    const dur = _duration();
    const cur = _currentTime();
    if (!dur) return;
    if (!queue.length) return;

    // Bounds: N+1, and N+2 for non-native (Spotify-style 2-ahead).
    const idxs = [];
    const n1 = (queueIdx + 1) % queue.length;
    if (n1 !== queueIdx &&
        !(repeatMode !== 'all' && n1 === 0 && queueIdx === queue.length - 1)) {
      idxs.push(n1);
    }
    const n2 = (queueIdx + 2) % queue.length;
    if (n2 !== queueIdx && n2 !== n1 &&
        !(repeatMode !== 'all' && n2 <= queueIdx)) {
      idxs.push(n2);
    }
    // P(skip) heuristic: if the user historically continues through
    // their queue (P(continue) > 0.7), warm one further track.
    // Spotify's published research shows N+3 prewarm pays off only for
    // continue-heavy listeners; for skip-heavy users it's wasted ffmpeg
    // budget that gets cancelled before completing.
    if (_pContinue() > 0.7) {
      const n3 = (queueIdx + 3) % queue.length;
      if (n3 !== queueIdx && n3 !== n1 && n3 !== n2 &&
          !(repeatMode !== 'all' && n3 <= queueIdx)) {
        idxs.push(n3);
      }
    }
    if (idxs.length === 0) return;

    // Pick the lookahead window based on the *immediate* next track —
    // if N+1 is transcoded, we want the wider 30s lead even if N+2 is
    // native, because the work to fire is dominated by N+1.
    const next1 = queue[n1];
    const next1Native = next1 && _isNativeFormat(next1.format);
    const window = next1Native ? PREFETCH_NATIVE_WINDOW : PREFETCH_TRANS_WINDOW;
    if (dur - cur > window) return;
    _prefetchDoneForId = trackId;
    // Snapshot the lookahead ids so a later queue reorder can detect
    // whether we still have a warm cache for the right tracks.
    _prefetchedNextIds = idxs.map(i => queue[i] && queue[i].id).filter(Boolean);

    for (const idx of idxs) {
      const t = queue[idx];
      if (!t || !t.id) continue;
      if (_isNativeFormat(t.format)) {
        // Cheap browser-cache warmup — only worth doing for N+1, skip N+2
        // since the first track's loadstart will trigger native preload.
        if (idx !== n1) continue;
        try {
          fetch(`/api/stream/${t.id}`, {
            headers: { Range: `bytes=${PREFETCH_RANGE}` },
            cache: 'default',
            priority: 'low',
          }).catch(() => {});
        } catch { /* ignore */ }
      } else {
        // Server-side cached transcode/render prewarm.
        const qs = t.path ? `?path=${encodeURIComponent(t.path)}` : '';
        try {
          fetch(`/api/stream/${t.id}/prewarm${qs}`, {
            method: 'POST',
            cache: 'no-store',
            priority: 'low',
          }).catch(() => {});
        } catch { /* ignore */ }
      }
    }
  }
  // Single timeupdate listener — fans out internally to _maybeCrossfade
  // (declared above), _checkPlayRecording, and _maybePrefetchNext.  All
  // three are cheap conditional checks until their gate fires, so the
  // fast path here is ~10 ns more than a single listener doing the same
  // work.  See _onTimeUpdate above for the consolidation rationale.
  audio.addEventListener('timeupdate', _onTimeUpdate);
  // Reset the per-track lock whenever the current track flips.
  audio.addEventListener('loadstart', () => {
    _prefetchDoneForId = null;
    _prefetchedNextIds = [];
    // A new src means any pending-seek from the old track is irrelevant.
    // playTrack() also resets this, but loadstart fires for src reloads
    // that bypass playTrack (rare, but covers seek-bar future calls etc.).
    _pendingSeekSec = null;
  });

  // ── Stall-detection: surface the buffering badge only when playback
  // actually stalls (browser fired 'waiting' because the buffer ran dry).
  // The 5-second BUFFERING_VISIBLE_DELAY (above) means a brief mid-track
  // refetch (<5 s) never flashes the badge — only sustained stalls do.
  // Replaces the start-of-track preload wait that the user reported as a
  // multi-second "conversion" delay before audio began.
  audio.addEventListener('waiting', () => {
    _showBufferingBadge();
  });
  audio.addEventListener('playing', () => {
    _hideBufferingBadge();
    // ``playing`` fires the instant audio frames start hitting the
    // output device — earlier than the audio.play() promise resolves
    // in practice.  Tearing down the Converting badge here means the
    // user's perception of "audio started" matches the visual cue.
    // Verified 2026-05-23: previously the badge was clearing on the
    // play() promise which could resolve a second after audible playback
    // had already begun, leaving a confusing post-start "Converting…"
    // overlay.
    _hideConvertBadge();
  });
  audio.addEventListener('canplay', () => {
    // ``canplay`` fires before ``playing``; if the browser is now ready
    // to resume, kill any pending badge timer that hadn't fired yet.
    _hideBufferingBadge();
  });
  // Listen for queue reorders / inserts — if N+1 / N+2 / N+3 are now
  // different tracks than the ones we already warmed, invalidate the
  // per-track lock so _maybePrefetchNext can redo the work for the new
  // upcoming tracks on the next timeupdate.  Without this, dragging a
  // fresh track into the next slot would still serve cold for the user
  // because _prefetchDoneForId === trackId short-circuits the prefetch.
  _handlers.queuechange.push(() => {
    if (!_prefetchDoneForId || !_prefetchedNextIds.length) return;
    if (queueIdx < 0 || !queue.length) return;
    const n1 = (queueIdx + 1) % queue.length;
    const n2 = (queueIdx + 2) % queue.length;
    const n3 = (queueIdx + 3) % queue.length;
    const currentNextIds = [n1, n2, n3]
      .map(i => queue[i] && queue[i].id)
      .filter(Boolean);
    // If any id we previously prefetched is no longer in the upcoming
    // window, the prewarm is stale — let the prefetcher run again.
    const stillFresh = _prefetchedNextIds.every(id => currentNextIds.includes(id));
    if (!stillFresh) {
      _prefetchDoneForId = null;
      _prefetchedNextIds = [];
    }
  });

  audio.addEventListener('ended', async () => {
    // Natural ended → user listened the whole way through → CONTINUE
    // signal for the P(skip) model.  Guarded by !_sidPartial because
    // SID partials also fire 'ended' at the cached boundary, and that's
    // technically a render-state event, not a user preference.
    if (!_sidPartial) {
      _recordAdvance(false);
      _invalidatePContinue();
    }
    // ── SID partial: audio ended at the cached boundary ─────────────────
    if (_sidPartial && !_sidFullReady) {
      // Full version still rendering — show badge and wait, bounded so a
      // hung render doesn't keep the badge spinning forever.
      _showConvertBadge();
      const _waitForFull = () => new Promise((resolve, reject) => {
        const started = Date.now();
        const BUDGET_MS = 5 * 60 * 1000;
        const iv = setInterval(async () => {
          if (Date.now() - started > BUDGET_MS) {
            clearInterval(iv);
            reject(new Error('SID render exceeded 5 minute budget'));
            return;
          }
          try {
            const r = await fetch(`/api/stream/${trackId}/render-status`);
            const j = await r.json();
            if (j.ready) { clearInterval(iv); _sidFullReady = true; resolve(); }
          } catch (_) {}
        }, 1500);
      });
      try {
        await _waitForFull();
      } catch (err) {
        _hideConvertBadge();
        Toast.error("Full SID render exceeded 5 min — check Settings → Renderers (sidplayfp may be stuck or missing).");
        return;
      }
      _hideConvertBadge();
      await _switchToFullSid(_sidCachedSec);
      return;
    }
    if (_sidPartial && _sidFullReady) {
      // Full version ready — seamless switch at the boundary
      await _switchToFullSid(_sidCachedSec);
      return;
    }

    // Ensure play is recorded on track end
    if (!_playRecorded && trackId) {
      _playRecorded = true;
      // ``sendBeacon`` is preferred — the browser queues the POST for
      // OS-level delivery, which survives page-hide / iOS backgrounding
      // (UX-under-load #6).  Fall back to ``fetch`` (with ``keepalive``)
      // when sendBeacon isn't available or rejects the call.
      const url = `/api/tracks/${trackId}/played`;
      const ok = !!(navigator.sendBeacon && navigator.sendBeacon(url));
      if (!ok) {
        fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
      }
    }
    emit('ended', {});
    // If crossfade already triggered next(), don't double-advance
    if (_crossfading) { _crossfading = false; return; }
    if (repeatMode === 'one') {
      _seekOffset = 0;
      audio.currentTime = 0;
      audio.play().catch(() => {});
    } else if (repeatMode === 'all' || queueIdx < queue.length - 1) {
      next();
    } else {
      emit('statechange', { playing: false });
    }
  });

  // Track IDs that already retried via ?force_transcode=1.  Persisted to
  // localStorage so subsequent sessions skip the doomed first attempt
  // — a known-corrupt FLAC otherwise gives the user one failing play
  // per session before the transcoded version kicks in.
  const FORCE_KEY = 'sb_force_transcode_ids';
  let _forcedIds = new Set();
  try {
    const raw = localStorage.getItem(FORCE_KEY);
    if (raw) _forcedIds = new Set(JSON.parse(raw));
  } catch (_) { /* corrupt JSON — fall back to empty Set */ }
  function _markForceTranscoded(id) {
    if (!id || _forcedIds.has(id)) return;
    _forcedIds.add(id);
    try {
      localStorage.setItem(FORCE_KEY, JSON.stringify([..._forcedIds]));
    } catch (_) { /* quota / private-mode — best effort */ }
  }
  /** Public hook used by ``playTrack`` to apply the persistent mark. */
  function _streamUrlFor(id) {
    return _forcedIds.has(id) ? _streamUrl(id, { force_transcode: '1' })
                              : _streamUrl(id);
  }

  /** Extract the track id from a stream URL, regardless of query params.
   *  We can't rely on the closure's ``trackId`` because anything that
   *  set ``audio.src`` outside ``playTrack`` (a direct probe, a queue
   *  prefetch, etc.) leaves it stale. */
  function _idFromSrc(src) {
    if (!src) return '';
    const m = String(src).match(/\/api\/stream\/([0-9a-f-]{8,})/i);
    return m ? m[1] : '';
  }

  audio.addEventListener('error', () => {
    const err = audio.error;
    if (!err) return;
    // MEDIA_ERR_ABORTED (code 1) is the normal value the element reports
    // when the *user* switched src to play a different track.  Don't toast
    // for that — only the real failure modes deserve a banner.
    if (err.code === 1) return;

    // Stale-src guard: when the user clicks a new track, ``audio.src`` is
    // replaced.  If the OLD src had a pending decode error, it can fire
    // this listener after ``trackId`` has already moved on to the new
    // track.  Cross-check the URL's track id against the current
    // ``trackId`` (the one ``playTrack`` last wrote); silently skip the
    // toast when they disagree — the new track has its own error path.
    const srcId = _idFromSrc(audio.src);
    if (srcId && trackId && srcId !== trackId) {
      console.warn(
        `audio error code=${err.code} for stale src ${srcId} (current=${trackId}) — suppressing toast`);
      return;
    }

    const codes = { 2: 'NETWORK', 3: 'DECODE', 4: 'SRC_NOT_SUPPORTED' };
    const tag = codes[err.code] || err.code;
    const title = (_track && (_track.title || _track.name)) || 'track';
    // SRC_NOT_SUPPORTED mid-stream (code 4) almost always means the
    // demuxer hit an unparseable frame in an otherwise-valid container
    // — corrupt-frame LOST_SYNC on FLAC, bad MPEG header on MP3, MJPEG
    // attached_pic with no PTS, etc.  The server-side
    // ``force_transcode=1`` query forces ffmpeg's libavcodec demuxer
    // (which tolerates these by resynchronising / dropping the PTS-less
    // picture stream) to produce a clean WAV.  Retry once, then mark
    // the trackId so future sessions skip the doomed first attempt.
    //
    // The id comes from the current ``audio.src`` rather than the
    // closure's ``trackId`` — that way the retry fires for any code
    // path that put a stream URL on the element, not just ones routed
    // through ``playTrack`` (which is the only writer of ``trackId``).
    // Also skip if the src already carries ``force_transcode`` to
    // prevent a transcoded-WAV failure from triggering another retry.
    const id = trackId || _idFromSrc(audio.src);
    const alreadyForced = /[?&]force_transcode=1/.test(audio.src);
    if (err.code === 4 && id && !alreadyForced && !_forcedIds.has(id)) {
      console.warn(`Media error [${tag}] on "${title}" — retrying with force_transcode=1`);
      _markForceTranscoded(id);
      const wasAt = audio.currentTime || 0;
      audio.src = _streamUrl(id, { force_transcode: '1' });
      audio.load();
      const _onReady = () => {
        audio.removeEventListener('canplay', _onReady);
        if (wasAt > 0.5) {
          try { audio.currentTime = wasAt; } catch (_) {}
        }
        audio.play().catch(() => {});
      };
      audio.addEventListener('canplay', _onReady);
      return;
    }
    console.error(`Media error [${tag}]: ${err.message}`);
    Toast.error(`Couldn't play "${title}" (${tag})`);
  });

  const savedVol = localStorage.getItem('sb_volume');
  if (savedVol !== null) audio.volume = parseFloat(savedVol);

  return {
    get analyser()       { return analyser; },
    get vuAnalyser()     { return vuAnalyser; },
    get ctx()            { return ctx; },
    get eqFilters()      { return eqFilters; },
    get eqPreGain()      { return eqPreGain; },
    get replayGain()     { return replayGain; },
    get currentTrackId() { return trackId; },
    get playing()        { return !audio.paused; },
    get queue()          { return queue; },
    get queueIdx()       { return queueIdx; },
    get currentTrack()   { return _track; },
    get repeatMode()     { return repeatMode; },
    get shuffle()        { return shuffle; },
    get currentTime()    { return _currentTime(); },
    get audio()          { return audio; },
    getAudioContext()    { return ctx; },
    getSourceNode()      { return source; },
    fmt,
    playTrack, playPause, seek, setVolume, next, prev, setQueue,
    addToQueue, removeFromQueue, moveInQueue,
    toggleShuffle, toggleRepeat,
    get convertDelay()       { return _getConvertDelay(); },
    setConvertDelay(ms)      { localStorage.setItem(CONVERT_DELAY_KEY, String(ms)); },
    get crossfade()          { return _getCrossfade(); },
    setCrossfade(sec)        { localStorage.setItem(CROSSFADE_KEY, String(Math.max(0, Math.min(12, sec)))); },
    get replayGainMode()     { return _getRgMode(); },
    setReplayGainMode(m)     {
      const next = (m === 'off' || m === 'track' || m === 'album') ? m : 'album';
      localStorage.setItem(RG_KEY, next);
      // Re-apply for the currently loaded track so the user hears the
      // change immediately instead of waiting for the next track.
      try { _applyReplayGain(_track); } catch (_) {}
    },
    on(evt, fn) { if (_handlers[evt]) _handlers[evt].push(fn); },
  };
})();
