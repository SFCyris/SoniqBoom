// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * player.js — HTML5 Audio engine with Web Audio API hook for visualizer.
 * Exports: Player singleton
 */
import { TRACKER_FORMAT_NAMES } from './utils.js';

export const Player = (() => {
  const audio = document.getElementById('audio-el');
  let trackId        = null;
  let _track         = null;   // full track object currently loaded
  let _metaDuration  = 0;      // track.duration from library metadata
  let _seekOffset    = 0;      // seconds already skipped for transcoded streams
  let _isTranscoded  = false;  // true when server is piping ffmpeg output (no range support)
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
    return v !== null ? parseFloat(v) : 300;          // default 300 ms
  }
  function _showConvertBadge() {
    if (_convertBadge) _convertBadge.hidden = false;
  }
  function _hideConvertBadge() {
    clearTimeout(_convertTimer);
    _convertTimer = null;
    if (_convertBadge) _convertBadge.hidden = true;
  }

  // Formats served as FileResponse with Accept-Ranges (browser can seek natively).
  const NATIVE_FORMATS = new Set(['MP3', 'FLAC', 'WAV', 'OGG', 'OPUS', 'AAC']);

  // Safari decodes ALAC natively, so the backend serves the original .m4a
  // directly (with Range support) instead of transcoding to FLAC. The
  // frontend must mirror that decision so seeks use audio.currentTime
  // rather than the ?seek= reload path (which is for ffmpeg pipes only).
  const _IS_SAFARI = (() => {
    const ua = navigator.userAgent;
    return /Safari/.test(ua)
        && !/Chrome|Chromium|Edg\/|OPR\//.test(ua);
  })();
  function _nativeForThisBrowser(fmtUp) {
    if (NATIVE_FORMATS.has(fmtUp)) return true;
    if (_IS_SAFARI && fmtUp === 'ALAC') return true;
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
  // Wait until at least N seconds of audio are buffered ahead of the play
  // head before starting playback. Prevents stutter at track start on slow
  // connections; a hard timeout guarantees we never hang the UI.
  const PRELOAD_KEY = 'sb_preload_buffer';
  function _getPreloadBuffer() {
    const v = localStorage.getItem(PRELOAD_KEY);
    return v !== null ? Math.max(0, parseFloat(v)) : 5;   // default 5 s
  }
  const _bufferingBadge = document.getElementById('buffering-badge');
  function _showBufferingBadge() { if (_bufferingBadge) _bufferingBadge.hidden = false; }
  function _hideBufferingBadge() { if (_bufferingBadge) _bufferingBadge.hidden = true; }

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
  let source    = null;
  let eqFilters = [];   // 10 BiquadFilterNodes (lowshelf, 8×peaking, highshelf)

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
  const _handlers = { timeupdate: [], trackchange: [], ended: [], statechange: [], error: [], queuechange: [] };
  function emit(evt, data) { (_handlers[evt] || []).forEach(fn => fn(data)); }

  // ── Wake Lock (prevent screen sleep during playback) ─────────────────────
  let _wakeLock = null;
  async function _acquireWakeLock() {
    if (_wakeLock || !navigator.wakeLock) return;
    try { _wakeLock = await navigator.wakeLock.request('screen'); }
    catch (_) { /* user denied or not supported */ }
  }
  function _releaseWakeLock() {
    if (_wakeLock) { _wakeLock.release().catch(() => {}); _wakeLock = null; }
  }
  _handlers.statechange.push(({ playing }) => playing ? _acquireWakeLock() : _releaseWakeLock());
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (audio && !audio.paused) _acquireWakeLock();
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
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source = ctx.createMediaElementSource(audio);

      // Build 5-band EQ filter chain
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

      // Chain: source → eq[0..4] → analyser → destination
      let node = source;
      for (const f of eqFilters) { node.connect(f); node = f; }
      node.connect(analyser);
      analyser.connect(ctx.destination);
    } catch (e) {
      console.warn('Web Audio API unavailable:', e);
      ctx = null; analyser = null; source = null; eqFilters = [];
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

  async function _checkSidPartial(track) {
    // Query the lightweight render-status endpoint to check for partial cache
    try {
      const res = await fetch(`/api/stream/${track.id}/render-status`);
      const j   = await res.json();

      if (j.partial && j.cached_seconds > 0 && j.target_seconds > j.cached_seconds) {
        _sidPartial   = true;
        _sidCachedSec = j.cached_seconds;
        _sidTargetSec = j.target_seconds;
        _sidFullReady = false;
        _metaDuration = j.target_seconds;    // show target duration in UI
        // Poll for full render every 2 s
        _sidPollTimer = setInterval(async () => {
          try {
            const st = await fetch(`/api/stream/${track.id}/render-status`);
            const jr = await st.json();
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
    } catch (_) { /* ignore — non-critical */ }
  }

  /** Switch to the full-duration SID version, continuing from `resumeAt`. */
  async function _switchToFullSid(resumeAt) {
    if (!trackId) return;
    _sidPartial = false;
    _seekOffset = 0;
    const wasPlaying = !audio.paused;
    audio.pause();
    audio.src = _streamUrl(trackId);
    audio.currentTime = 0;
    // Wait for enough data to seek
    await new Promise((r) => { audio.addEventListener('canplay', r, { once: true }); });
    if (resumeAt > 0) audio.currentTime = resumeAt;
    if (wasPlaying) audio.play().catch(console.warn);
    _hideConvertBadge();
  }

  // ── Core playback ─────────────────────────────────────────────────────────
  async function playTrack(track) {
    _track         = track;
    _metaDuration  = track.duration || 0;
    _seekOffset    = 0;
    // _isTranscoded = true only for ffmpeg-piped formats (ALAC, WavPack, AIFF…).
    // SID/MIDI/Tracker are rendered to cached WAV files served with Accept-Ranges,
    // so the browser can seek them natively via audio.currentTime — NOT via ?seek=.
    const _fmtUp  = (track.format || '').toUpperCase();
    const _fmtRaw = (track.format || '');
    const _native = _nativeForThisBrowser(_fmtUp);
    _isTranscoded  = !_native && !RENDERED_SEEKABLE_FORMATS.has(_fmtRaw);
    // _needsConvert covers ALL non-native formats: both ffmpeg-transcoded and rendered (SID/MIDI/Tracker)
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
    audio.src = _streamUrl(track.id);
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
      _hideConvertBadge();
      emit('statechange', { playing: true });

      // Check for SID partial cache (non-blocking, after playback starts)
      const fmt = (track.format || '').toUpperCase();
      if (fmt === 'SID' || fmt === 'PSID') {
        _checkSidPartial(track);
      }
    } catch (err) {
      _hideConvertBadge();
      if (err.name === 'AbortError') return; // superseded by newer call
      console.error('audio.play() failed:', err.name, err.message);
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
      // Full version still rendering — flash badge and wait
      _showConvertBadge();
      (async () => {
        while (!_sidFullReady) {
          await new Promise(r => setTimeout(r, 1500));
          try {
            const res = await fetch(`/api/stream/${trackId}/render-status`);
            const j   = await res.json();
            if (j.ready) _sidFullReady = true;
          } catch (_) {}
        }
        _hideConvertBadge();
        _switchToFullSid(targetSec);
      })();
      return;
    }

    if (!_isTranscoded) {
      // Native format (MP3, FLAC file, WAV, OGG, AAC) — browser range requests work
      _seekOffset = 0;
      audio.currentTime = targetSec;
    } else {
      // Transcoded pipe (ALAC→FLAC via ffmpeg): no range support on the pipe.
      // Reload the URL with a server-side -ss seek offset.
      const wasPlaying = !audio.paused;
      _seekOffset = targetSec;
      audio.pause();
      audio.src = _streamUrl(trackId, { seek: targetSec.toFixed(2) });
      if (wasPlaying) {
        audio.play().catch(console.warn);
      }
    }
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
        audio.src = _streamUrl(trackId);
        audio.play().catch(() => {});
      }
      return;
    }
    queueIdx = (queueIdx - 1 + queue.length) % queue.length;
    playTrack(queue[queueIdx]);
    emit('queuechange', { queue, queueIdx });
  }

  function setQueue(tracks, startIdx = 0) {
    queue    = tracks;
    queueIdx = startIdx;
    playTrack(queue[queueIdx]);
    emit('queuechange', { queue, queueIdx });
  }

  function addToQueue(track) {
    queue = [...queue, track];
    emit('queuechange', { queue, queueIdx });
  }

  function removeFromQueue(idx) {
    if (idx < 0 || idx >= queue.length) return;
    queue = queue.filter((_, i) => i !== idx);
    if (idx < queueIdx) {
      queueIdx = queueIdx - 1;
    } else if (idx === queueIdx) {
      // Currently playing track removed; clamp to valid range
      queueIdx = Math.min(queueIdx, queue.length - 1);
    }
    emit('queuechange', { queue, queueIdx });
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
  audio.addEventListener('timeupdate', () => {
    const dur     = _duration();
    const current = _currentTime();
    emit('timeupdate', {
      current,
      duration: dur,
      pct: dur ? Math.min(100, (current / dur) * 100) : 0,
    });

    // Crossfade: when approaching end of track, trigger crossfade to next
    const xfade = _getCrossfade();
    if (xfade > 0 && !_crossfading && dur > 0 && queue.length > 0) {
      const remaining = dur - current;
      if (remaining <= xfade && remaining > 0.2 && (repeatMode === 'all' || queueIdx < queue.length - 1)) {
        _crossfading = true;
        // Fade out current track
        const fadeSteps = 20;
        const fadeInterval = (remaining * 1000) / fadeSteps;
        const origVol = audio.volume;
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
    }
  });

  // When metadata loads for a native file, refresh duration display
  audio.addEventListener('loadedmetadata', () => {
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
      fetch(`/api/tracks/${trackId}/played`, { method: 'POST' }).catch(() => {});
    }
  }

  audio.addEventListener('timeupdate', _checkPlayRecording);

  // ── Next-track prefetch (gapless warmup) ──────────────────────────────────
  // When the current track nears its end and there is a known next track in
  // the queue, issue a tiny Range request so the browser HTTP cache + server
  // range handler have the first few hundred KB warm by the time playTrack()
  // fires. Only runs once per track; shuffle-mode skips (next is random).
  let _prefetchDoneForId = null;
  const PREFETCH_WINDOW = 15;          // seconds before end of current track
  const PREFETCH_RANGE  = '0-262143';  // first 256 KB — enough to start decode
  function _maybePrefetchNext() {
    if (shuffle) return;
    if (!trackId || _prefetchDoneForId === trackId) return;
    const dur = _duration();
    const cur = _currentTime();
    if (!dur || dur - cur > PREFETCH_WINDOW) return;
    if (!queue.length) return;
    const nextIdx = (queueIdx + 1) % queue.length;
    if (nextIdx === queueIdx) return;
    if (repeatMode !== 'all' && nextIdx === 0 && queueIdx === queue.length - 1) return;
    const nextTrack = queue[nextIdx];
    if (!nextTrack || !nextTrack.id) return;
    _prefetchDoneForId = trackId;
    try {
      fetch(`/api/stream/${nextTrack.id}`, {
        headers: { Range: `bytes=${PREFETCH_RANGE}` },
        cache: 'default',
        // `priority` is a hint supported on Chrome/Edge; harmless elsewhere.
        priority: 'low',
      }).catch(() => {});
    } catch { /* ignore */ }
  }
  audio.addEventListener('timeupdate', _maybePrefetchNext);
  // Reset the per-track lock whenever the current track flips.
  audio.addEventListener('loadstart', () => { _prefetchDoneForId = null; });

  audio.addEventListener('ended', async () => {
    // ── SID partial: audio ended at the cached boundary ─────────────────
    if (_sidPartial && !_sidFullReady) {
      // Full version still rendering — show badge and wait
      _showConvertBadge();
      const _waitForFull = () => new Promise((resolve) => {
        const iv = setInterval(async () => {
          try {
            const r = await fetch(`/api/stream/${trackId}/render-status`);
            const j = await r.json();
            if (j.ready) { clearInterval(iv); _sidFullReady = true; resolve(); }
          } catch (_) {}
        }, 1500);
      });
      await _waitForFull();
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
      fetch(`/api/tracks/${trackId}/played`, { method: 'POST' }).catch(() => {});
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

  audio.addEventListener('error', () => {
    const err = audio.error;
    if (!err) return;
    const codes = { 1: 'ABORTED', 2: 'NETWORK', 3: 'DECODE', 4: 'SRC_NOT_SUPPORTED' };
    console.error(`Media error [${codes[err.code] || err.code}]: ${err.message}`);
  });

  const savedVol = localStorage.getItem('sb_volume');
  if (savedVol !== null) audio.volume = parseFloat(savedVol);

  return {
    get analyser()       { return analyser; },
    get ctx()            { return ctx; },
    get eqFilters()      { return eqFilters; },
    get currentTrackId() { return trackId; },
    get playing()        { return !audio.paused; },
    get queue()          { return queue; },
    get queueIdx()       { return queueIdx; },
    get currentTrack()   { return _track; },
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
    on(evt, fn) { if (_handlers[evt]) _handlers[evt].push(fn); },
  };
})();
