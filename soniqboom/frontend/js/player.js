// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * player.js — HTML5 Audio engine with Web Audio API hook for visualizer.
 * Exports: Player singleton
 */
// version bump needed in index.html for player.js (queue/race/EQ chain fixes)
import { TRACKER_FORMAT_NAMES, CHIP_FORMAT_NAMES, Toast, subsongWireToTune, subsongStartOf } from './utils.js';
// Experimental, flag-gated in-browser SID playback (see sid-wasm-player.js).
// Remove the feature by deleting this import + the one branch it guards below.
import { sidWasmPlaybackEnabled, isC64SidTrack, renderSidForPlayback,
         sidWasmSupported, sidRenderStatus } from './sid-wasm-player.js';

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

  // "Converting…" badge — shown after a configurable delay for transcoded
  // (non-rendered) formats; rendered ones show "Rendering…" after RENDER_BADGE_DELAY
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
    // Resilience watchdog: progress now arrives via the library WebSocket
    // (app.js ``transcode_progress`` branch → _onTranscodeProgress).  If no
    // WS sample for the playing track lands within ~2 s of the badge
    // appearing, fall back to ONE guarded HTTP poll so a dropped WS never
    // leaves the badge stuck on the indeterminate spinner.  Re-enable the
    // poll guard so that fallback fetch is accepted, then arm the watchdog.
    _transcodePollTrackId = trackId;
    _armTranscodeWatchdog(trackId);
    // Promote to cancellable: shows the inline × button declared in
    // index.html.  Wire its click on first show only (idempotent —
    // dataset flag guards against re-wiring on every show).
    _convertBadge.classList.add('cancellable');
    const cancelEl = _convertBadge.querySelector('#converting-badge-cancel');
    // The button ships ``hidden`` (and the global ``[hidden]`` rule wins over
    // ``.cancellable``), so reveal it here.
    if (cancelEl) cancelEl.hidden = false;
    if (cancelEl && !cancelEl.dataset.wired) {
      cancelEl.dataset.wired = '1';
      cancelEl.addEventListener('click', () => {
        // The × hides with its badge, which would drop keyboard focus to
        // <body>: move it to Play (which starts the track again).
        const hadFocus = _convertBadge.contains(document.activeElement);
        _cancelPendingStart();
        if (hadFocus) document.getElementById('btn-play')?.focus();
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
  // AbortController for an in-flight transcode-status fetch (the single
  // WS-fallback poll, below).  Held at module scope so a track switch /
  // folder nav can cancel the late response — same pattern as
  // ``_sidPartialAbort``.
  let _transcodePollAbort = null;
  // WS-push is now the primary progress channel (see app.js onmessage
  // ``transcode_progress`` branch).  These drive the resilience watchdog:
  // when the convert badge first shows we arm a short timer; if no WS
  // message for the playing track has arrived by then we fire ONE poll so
  // progress is never lost to a WS blip.  Cleared on WS message / track
  // change.
  let _transcodeWatchdogTimer = null;
  let _transcodeWatchdogTrackId = null;
  const _TRANSCODE_WATCHDOG_MS = 2000;

  // Arm a one-shot fallback: if no WS transcode_progress message for
  // ``trackId`` lands within ~2 s of the badge showing, do a single
  // guarded poll of /api/stream/{id}/transcode-status so a dropped WS
  // connection never leaves the badge stuck on the indeterminate spinner.
  function _armTranscodeWatchdog(watchTrackId) {
    _clearTranscodeWatchdog();
    if (!watchTrackId) return;
    _transcodeWatchdogTrackId = watchTrackId;
    _transcodeWatchdogTimer = setTimeout(() => {
      _transcodeWatchdogTimer = null;
      // Only poll if the track is still current — a switch between arming
      // and firing makes the fallback moot.
      if (trackId === watchTrackId) {
        _transcodePollTrackId = watchTrackId;   // re-enable the guard
        _pollTranscodeStatusOnce(watchTrackId);
      }
      _transcodeWatchdogTrackId = null;
    }, _TRANSCODE_WATCHDOG_MS);
  }

  function _clearTranscodeWatchdog() {
    if (_transcodeWatchdogTimer) {
      clearTimeout(_transcodeWatchdogTimer);
      _transcodeWatchdogTimer = null;
    }
    _transcodeWatchdogTrackId = null;
  }

  function _stopTranscodePolling() {
    if (_transcodePollTimer) {
      clearInterval(_transcodePollTimer);
      _transcodePollTimer = null;
    }
    // Abort any in-flight WS-fallback fetch so its connection slot frees
    // immediately and a late response can't flash into the badge.
    if (_transcodePollAbort) {
      try { _transcodePollAbort.abort(); } catch (_) {}
    }
    _clearTranscodeWatchdog();
    _transcodePollTrackId = null;
    _transcodePollAbort = null;
  }

  // WS-fallback poll: fired ONCE by the watchdog if no transcode_progress
  // message arrived in time.  Determinate transcode progress is normally
  // pushed over the library WebSocket (app.js → _onTranscodeProgress); this
  // single guarded GET is the resilience net against a WS blip.  Aborted
  // on track change / folder nav via ``_transcodePollAbort``.
  async function _pollTranscodeStatusOnce(reqTrackId) {
    // Discard responses after the user has switched tracks — a slow
    // backend can return progress for the *previous* track and we don't
    // want that flashing into the badge mid-switch.
    if (_transcodePollTrackId !== reqTrackId) return;
    const ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    const prevAbort = _transcodePollAbort;
    _transcodePollAbort = ctrl;
    if (prevAbort && prevAbort !== ctrl) {
      try { prevAbort.abort(); } catch (_) {}
    }
    try {
      const res = await fetch(`/api/stream/${reqTrackId}/transcode-status`,
                              ctrl ? { credentials: 'same-origin', signal: ctrl.signal }
                                   : { credentials: 'same-origin' });
      if (!res.ok) return;
      const j = await res.json();
      if (_transcodePollTrackId !== reqTrackId) return;
      // Route the sample through the same bridge the WS path uses so the
      // dual-purpose (badge progress AND transcode-ready emit) stays in
      // one place.
      _onTranscodeProgress(
        reqTrackId,
        (typeof j.percent === 'number') ? j.percent : 0,
        j.eta_seconds,
        j.ready === true,
        j.error === true,   // tear down the badge if the fallback poll sees a failure
      );
    } catch (err) {
      if (err && err.name === 'AbortError') return;  // intentional — stay quiet
    } finally {
      if (_transcodePollAbort === ctrl) _transcodePollAbort = null;
    }
  }

  // Single source of truth for applying a transcode-progress sample to the
  // UI, driven by BOTH the WebSocket push (app.js) and the WS-fallback poll
  // above.  Preserves the dual duty the old 600 ms poll performed:
  //   (a) in-progress → flip the badge to a determinate bar + ETA;
  //   (b) ready       → snap to 100 %, tear down poll/watchdog + hide the
  //       badge, AND emit ``transcode-ready`` so app.js re-fetches the
  //       waveform off the now-complete file (PERC-9).
  //   (c) error       → tear down poll/watchdog + hide the badge (no emit).
  // ``msgTrackId`` is matched against the live ``trackId`` so a late sample
  // for a track the user already moved past can't flash into the new badge.
  function _onTranscodeProgress(msgTrackId, percent, etaSec, ready, isError) {
    if (msgTrackId !== trackId) return;   // not the playing track — ignore
    // A real sample arrived → the WS (or fallback) channel is alive; the
    // resilience watchdog has done its job.
    _clearTranscodeWatchdog();
    if (isError) {
      // Server reported the transcode failed.  Abandon the poll + badge so
      // the user isn't left staring at a spinner that will never advance.
      // No ``transcode-ready`` emit — the file never completed.
      _stopTranscodePolling();
      _hideConvertBadge();
      return;
    }
    if (ready) {
      // Render finished — the audio element's own load lifecycle will
      // take over from here.  Snap the bar to 100 % for one beat
      // before letting _hideConvertBadge clear it; without this the
      // bar can vanish mid-fill which reads as a glitch.
      _updateConvertBadgeProgress(100, 0);
      _stopTranscodePolling();
      _hideConvertBadge();
      // PERC-9: the waveform served at track-load was computed off
      // the partial in-flight WAV (only a few seconds of audio were
      // on disk).  Now that the full file is cached, re-fetch so the
      // overlay reflects the complete track instead of the silent
      // padding the partial read produced.  app.js's transcode-ready
      // listener owns the canvas re-fetch.
      try { emit('transcode-ready', { trackId: msgTrackId }); }
      catch (_) { /* listener exceptions logged in emit() */ }
      return;
    }
    if (typeof percent === 'number' && percent > 0) {
      // Only flip to determinate once we actually have a sample —
      // showing "0 %" with no movement is worse than "Converting…".
      _updateConvertBadgeProgress(percent, etaSec);
    }
  }

  // Read the now-playing viz toggle straight from localStorage (avoids an
  // import cycle with the viz engine from this core module).
  function _vizPacketsOn() {
    try {
      const s = JSON.parse(localStorage.getItem('sb_viz_settings') || '{}');
      return s.enabled !== false && s.nowPlaying !== false;
    } catch { return true; }
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
      // Transcode "packet assembly" skin (viz #8): render the fill as
      // discrete blocks growing block-by-block as the WAV is assembled.
      // Pure CSS on the existing fill — preserves the progressbar a11y
      // contract below.  Gated on the now-playing viz group + reduced-motion.
      if (_vizPacketsOn()) bar.classList.add('viz-packets');
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-valuemin', '0');
      bar.setAttribute('aria-valuemax', '100');
      const fill = document.createElement('div');
      fill.className = 'progress-fill';
      bar.appendChild(fill);
      const eta = document.createElement('span');
      eta.className = 'progress-eta';
      // Cancel button — gives the user an explicit escape from a long
      // transcode (e.g. an accidental DSD play): stops starting the track
      // (see _cancelPendingStart).
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'progress-cancel';
      cancelBtn.setAttribute('aria-label', 'Cancel conversion');
      cancelBtn.title = 'Cancel conversion';
      cancelBtn.textContent = '×';   // multiplication sign — visual ×
      cancelBtn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        _cancelPendingStart();
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

  // Build (or rebuild) the canonical TEXT-ONLY badge: a ``.badge-label`` span
  // plus the hidden inline cancel button that _showConvertBadge wires.  Used
  // (a) to set the label — "Rendering…" for blocking renders vs "Converting…"
  // for transcodes — and (b) to restore the structure after a determinate-
  // progress teardown flattened it to a bare text node (which would otherwise
  // lose both the label span AND the cancel button for the next text-only
  // badge).  Idempotent; safe to call while the badge is hidden.
  function _renderTextOnlyConvertBadge(text) {
    if (!_convertBadge) return;
    _convertBadge.classList.remove('has-progress');
    _convertBadge.textContent = '';
    const lbl = document.createElement('span');
    lbl.className = 'badge-label';
    lbl.textContent = text;
    _convertBadge.appendChild(lbl);
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'badge-cancel';
    cancel.id = 'converting-badge-cancel';
    const cancelLabel = /^Rendering/.test(text) ? 'Cancel rendering' : 'Cancel conversion';
    cancel.setAttribute('aria-label', cancelLabel);
    cancel.title = cancelLabel;
    cancel.hidden = true;
    cancel.textContent = '×';   // × — multiplication sign
    _convertBadge.appendChild(cancel);
  }

  function _resetConvertBadgeProgress() {
    if (!_convertBadge) return;
    if (!_convertBadge.classList.contains('has-progress')) return;
    // Restore the canonical text-only structure (label + cancel button) rather
    // than a bare "Converting…" text node, so a later label set / cancel wire
    // still finds the elements it expects.
    _renderTextOnlyConvertBadge('Converting…');
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

  // Formats that BLOCK until the server finishes rendering the whole tune
  // (sidplayfp / fluidsynth / openmpt123 / adplay write a complete cached WAV
  // before the first byte is served), so a cold-cache play has a guaranteed
  // multi-second silent gap.  These get the "Rendering…" badge — unlike
  // DSD/ALAC/generic transcodes, which stream an in-flight WAV whose audio
  // starts in <100 ms and so keep the delayed "Converting…" badge.  Uppercased
  // so the lookup matches playTrack's ``_fmtUp``.  CHIP_FORMAT_NAMES already
  // includes the AdLib/OPL (ADLIB_FORMAT_NAMES) plus libgme chiptune names.
  const SERVER_RENDERED_FORMATS = new Set(
    ['SID', 'PSID', 'MIDI', 'MID',
     'SNDH', 'YM', 'SC68',
     'PSF', 'PSF2', 'USF', 'GSF', '2SF', 'SSF', 'DSF (DREAMCAST)', 'NCSF',
     ...TRACKER_FORMAT_NAMES, ...CHIP_FORMAT_NAMES,
    ].map(s => s.toUpperCase())
  );
  // The ~175 exotic-Amiga uade formats carry dynamic names ("TFMX Pro", …);
  // their ["Amiga","Module"] genre pair is the reliable render-badge signal.
  const _isUadeAmiga = (t) => Array.isArray(t && t.genre)
    && t.genre.includes('Amiga') && t.genre.includes('Module');
  // A track the server has to RENDER before it can be played (SID, MIDI,
  // trackers, chip formats, Amiga modules).
  const _isRenderedTrack = (t) => !!t
    && (SERVER_RENDERED_FORMATS.has(String(t.format || '').toUpperCase()) || _isUadeAmiga(t));
  // Show the "Rendering…" badge after this delay (ms).  Long enough that a
  // warm-cache hit — whose audio starts well under it — clears the timer
  // before it fires (no flash on replays), short enough that a cold render
  // reads as near-immediate feedback instead of the old 6 s of dead air.
  const RENDER_BADGE_DELAY = 1000;
  // [queue-core:begin] — this region + the one below are what
  // tests/js/player_queue.test.mjs extracts and runs; keep them self-contained.
  let queue         = [];
  let queueIdx      = -1;
  let shuffle       = false;
  let repeatMode    = 'none'; // 'none' | 'one' | 'all'

  // ── Queue model ─────────────────────────────────────────────────────────
  // The queue is ALWAYS held in PLAY ORDER: "next" is ``queue[queueIdx + 1]``
  // whether or not shuffle is on.  Shuffle never random-picks an index — it
  // changes what gets queued:
  //
  //   • View-backed queue (``_src``): the queue is a small sliding window over
  //     a server-side order of the view's WHOLE result set — the view's sort
  //     order, or (shuffle) a seeded permutation of every matching track
  //     (``/api/tracks/shuffled``).  The window is extended as playback nears
  //     its end, so shuffle reaches every track of a 250 000-track library while
  //     the browser still holds a few dozen rows.  (The old model random-picked
  //     inside a ≤500-track window — issue #11.)
  //   • Plain-array queue (playlist, album, selection…): shuffle physically
  //     reorders the tracks (Fisher–Yates) and un-shuffle restores the original
  //     order.
  //   One rule for both kinds: a shuffle deals everything that has NOT PLAYED yet
  //   in this queue; what already played stays behind the current track (so Prev
  //   walks back through it) and does not come round again within the pass.
  //   Settings → "Let shuffle repeat tracks you’ve heard" (``sb_shuffle_replay``)
  //   switches that off: the deal then covers every other track of the context.
  //   • In between: a source with no ``ordered`` spec (capped search, a list
  //     re-sorted in the browser).  Its shuffle is server-backed like the first
  //     kind; in list order it IS a plain array of the rows the view had.  Code
  //     that asks "can I page this?" must use ``_srcPageable()``, not ``_src``.
  //
  // A known "next" also means prefetch / gapless / prewarm and Prev all work
  // under shuffle, and nothing repeats within a pass: the server order is a
  // permutation, and tracks that played outside it are kept out (``_shufExtras``).
  const Q_PAGE        = 50;    // tracks fetched per extension
  const Q_LOOKAHEAD   = 10;    // extend when fewer than this remain ahead
  const Q_KEEP_BEHIND = 200;   // played tracks kept behind the current one (older ones are trimmed)
  let _src          = null;    // queue-source descriptor (plain JSON) | null
  let _srcMode      = 'ordered';   // 'ordered' | 'shuffled'
  let _seed         = 0;       // shuffle seed (same seed ⇒ same order ⇒ resumable)
  let _ordNext      = 0;       // next ORDERED offset to append
  let _ordResume    = 0;       // where ordered play resumes when shuffle goes off
  let _shufNext     = 0;       // next SHUFFLED offset to append
  let _srcTotal     = null;    // result-set size when known (null ⇒ detect by short page)
  let _srcEnded     = false;   // the current order has no more pages
  let _srcGen       = 0;       // bumps on every queue replacement → stale fetches drop
  let _extendP      = null;    // in-flight extension (dedup)
  let _origQueue    = null;    // pre-shuffle order, for un-shuffle restore
  let _origIdx      = -1;
  let _origIds      = null;    // …its ids only: all that survives a reload
  let _backP        = null;    // in-flight backward page (Prev at the window head)
  // Ids that played / were queued OUTSIDE the current shuffled order: the track
  // that was on when shuffle went on, the history before it, anything queued by
  // hand.  The permutation reaches each of them eventually, and by then history
  // trimming may have dropped them from ``queue`` — so "did this already play?"
  // cannot be answered from the queue alone.
  let _shufExtras   = new Set();
  // The current track was picked by the listener (a click), not dealt / advanced
  // to — an unplayable pick is reported and left alone, never auto-skipped.
  let _explicitPick = false;
  let _playSeq      = 0;       // bumped by playTrack on every start, so a stale skip timer can tell
  // Tracks the listener queued by hand (Add to queue / Play next).  Re-dealing
  // a view-backed queue replaces what the VIEW put there, never these.
  const _manual     = new WeakSet();
  // …and the ones the app queued itself (Radio Mode's mix): they go right after
  // the current track (behind mix rows already there), and a later re-deal may
  // replace them.
  const _auto       = new WeakSet();
  // Tracks that have started playing in THIS queue (reset with the queue / pass).
  let _played       = new WeakSet();
  // Preference (per browser, read at deal time): may a shuffle deal tracks that
  // already played in this queue?  Default no.
  const SHUFFLE_REPLAY_KEY = 'sb_shuffle_replay';
  function _shuffleReplaysPlayed() {
    try { return localStorage.getItem(SHUFFLE_REPLAY_KEY) === '1'; } catch (_) { return false; }
  }
  // LIST position of each queued track that came from the view's ordered list.
  // Index arithmetic cannot stand in for this: "Play next", a removed row or a
  // trimmed history all shift queue indexes without moving anyone in the list.
  const _ordPos     = new WeakMap();
  // Instant-Mix radio supplies its own curated, ever-refilling order, so while
  // a radio session is active the queue advances sequentially, the view source is
  // not extended, and the shuffle toggle is REFUSED (callers check ``radioActive``
  // and say why) — a shuffled "next" would re-seed the radio from an unrelated
  // track.  Set by RadioMode.start/stop via setRadioActive().
  let _radioActive  = false;
  // [queue-core:end]

  // ── Crossfade / gapless ────────────────────────────────────────────────
  const CROSSFADE_KEY = 'sb_crossfade';
  function _getCrossfade() {
    const v = localStorage.getItem(CROSSFADE_KEY);
    return v !== null ? parseFloat(v) : 0;  // 0 = disabled (gapless only)
  }
  let _crossfadeTimer = null;
  let _crossfading = false;

  // ── Gapless next-track preload ─────────────────────────────────────────
  // Near the end of a track (and only when crossfade is off) the next queue
  // track's stream is fetched into a blob; playTrack then swaps to the blob
  // URL, so the seam pays no network/transcode latency — just the element's
  // own decode start.  Not sample-accurate gapless (one <audio> element),
  // but it removes the audible network gap.  ``sb_gapless`` = '0' disables.
  const GAPLESS_KEY = 'sb_gapless';
  const GAPLESS_WINDOW_S = 20;          // start preloading in the last N seconds
  const GAPLESS_MAX_BYTES = 220 * 1024 * 1024;  // skip preload for huge streams
  function _gaplessEnabled() { return localStorage.getItem(GAPLESS_KEY) !== '0'; }
  let _nextPreload = null;              // { id, url, abort } | null
  let _activeBlobUrl = null;            // blob URL currently used as audio.src
  function _dropNextPreload() {
    if (!_nextPreload) return;
    try { _nextPreload.abort?.abort(); } catch (_) {}
    if (_nextPreload.url) { try { URL.revokeObjectURL(_nextPreload.url); } catch (_) {} }
    _nextPreload = null;
  }
  function _maybeGaplessPreload(dur, current) {
    if (_stationMode) return;   // never seam-swap the queue track sitting under a station
    if (!_gaplessEnabled() || _getCrossfade() > 0) return;
    if (!(dur > 0) || queue.length === 0) return;
    const next = _peekNext();
    if (!next || !next.id) return;
    const remaining = dur - current;
    if (remaining > GAPLESS_WINDOW_S || remaining <= 1) return;
    // Subsong-aware: a multi-tune file preloaded WITHOUT ?subsong= would seam to
    // its DEFAULT tune, so "Play all" of a SID would play tune 1 N times while
    // the label advanced.  Key + fetch on (id, subsong) — 0-based wire index.
    const nextSub = Number(next.subsong) > 0 ? Number(next.subsong) : 0;
    if (_nextPreload && _nextPreload.id === next.id && _nextPreload.subsong === nextSub) return;
    _dropNextPreload();
    const abort = new AbortController();
    _nextPreload = { id: next.id, subsong: nextSub, url: null, abort };
    fetch(`/api/stream/${next.id}${nextSub > 0 ? `?subsong=${nextSub}` : ''}`, { signal: abort.signal })
      .then(res => {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const len = parseInt(res.headers.get('content-length') || '0', 10);
        if (len > GAPLESS_MAX_BYTES) throw new Error('too large to preload');
        return res.blob();
      })
      .then(blob => {
        // Guard on !url too: if a drop+re-preload of the same (id, subsong) raced
        // this resolution, overwriting would orphan the fresher blob URL.
        if (_nextPreload && _nextPreload.id === next.id && _nextPreload.subsong === nextSub && !_nextPreload.url) {
          _nextPreload.url = URL.createObjectURL(blob);
        }
      })
      .catch(() => {
        // Preload is best-effort: on any failure the seam simply falls back
        // to the normal network fetch.
        if (_nextPreload && _nextPreload.id === next.id && _nextPreload.subsong === nextSub && !_nextPreload.url) {
          _nextPreload = null;
        }
      });
  }

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
  // True while a blocking-renderer track is loading — suppresses the (centered)
  // buffering badge so it never stacks on the top-right "Rendering…" badge (the
  // early render badge IS the feedback).  The module-scope `waiting` handler
  // can't see playTrack's block-local _serverRendered, so it reads this instead.
  let _suppressBufferingBadge = false;
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
  const _handlers = { timeupdate: [], trackchange: [], ended: [], statechange: [], error: [], queuechange: [], seeked: [], durationknown: [], shufflechange: [], queuestall: [] };
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
      // We deliberately let the browser pick the AudioContext sample rate
      // (the output device's preferred rate) rather than matching it to the
      // served file (e.g. a DSD track's X-DSD-Output-Rate).  Matching would
      // require tearing the context down and rebuilding it whenever the rate
      // changes — AudioContext.sampleRate is immutable after creation — and
      // that is unsafe here for three compounding reasons:
      //   1. createMediaElementSource(audio) is single-use per element; once
      //      this element is routed through a context it cannot be re-routed
      //      through a new one without throwing, so a rebuild would break the
      //      element's audio path permanently.
      //   2. The context must be created inside a user gesture (Safari/iOS),
      //      so it cannot be deferred until after a stream response reveals
      //      the rate.
      //   3. The entire output chain — EQ, ReplayGain, analyser/VU meter —
      //      hangs off this one source node; a teardown/rebuild would risk
      //      all of it.
      // The only cost of not matching is a transparent, high-quality browser
      // resample of non-device-rate sources (inaudible; negligible CPU), so
      // the rebuild is not worth the regression surface.
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
  // [render-core:begin] — the render-core regions are what
  // tests/js/render_core.test.mjs extracts and runs; keep them self-contained.
  // A growing render whose length the server doesn't know yet is playing: the
  // element's duration comes from a provisional header and means nothing —
  // and neither does the row's stored length (it is the default tune's, or
  // one the server did not trust), so the length reads as unknown.
  let _provisional = false;
  // A provisional "read to the end" WAV header makes the element claim hours
  // (0xFFFFFFFF bytes ≈ 6.8 h); no rendered tune is that long.  Caught here
  // too because the element reports it before the render-status watch answers.
  const PROVISIONAL_LENGTH_SEC = 3 * 3600;
  function _provisionalLength(d) {
    return _isRenderedTrack(_track) && isFinite(d) && d > PROVISIONAL_LENGTH_SEC;
  }
  function _duration() {
    if (_sidPartial && _sidTargetSec > 0) return _sidTargetSec;
    if (_provisional) return 0;
    if (_isTranscoded) return _metaDuration || 0;
    const d = audio.duration;
    if (_provisionalLength(d)) return 0;
    return (isFinite(d) && d > 0) ? d : (_metaDuration || 0);
  }
  // The element's length as the library may keep it (``durationknown``): 0
  // while a growing render's provisional header is on the element — the
  // complete-file switch reloads it, and its real length is reported then.
  function _knownLength() {
    const d = audio.duration;
    if (!isFinite(d) || d <= 0 || _provisional || _provisionalLength(d)) return 0;
    return d;
  }
  // [render-core:end]

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
    _provisional  = false;   // the unknown-length render watch (_watchRenderLength) too
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
      const res = await fetch(`/api/stream/${track.id}/render-status${_subQs(track)}`,
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
            const st = await fetch(`/api/stream/${track.id}/render-status${_subQs(track)}`);
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

  /** Switch to the full-duration SID version, continuing from `resumeAt`
   *  (``opts`` → _switchToCachedRender). */
  async function _switchToFullSid(resumeAt, opts) {
    if (!trackId || _extAudio) return;   // radio owns output — don't re-arm the SID track
    _sidPartial = false;
    await _switchToCachedRender(resumeAt, opts);
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
    const albumDb   = _parseRgDb(track.replaygain_album_gain);
    const trackDb   = _parseRgDb(track.replaygain_track_gain);
    const albumPeak = _parseRgDb(track.replaygain_album_peak);
    const trackPeak = _parseRgDb(track.replaygain_track_peak);
    // Prefer the selected mode's gain; fall back to the other tag so a track
    // tagged for only one still gets levelled (better than unity).  Pair each
    // gain WITH ITS OWN peak (album gain ↔ album peak, track gain ↔ track peak)
    // so the 0.99/peak clip headroom below matches the gain actually applied —
    // and carry that source's peak through the fallback, never a mismatched one.
    let db, peak;
    if (mode === 'album') {
      if (albumDb !== null) { db = albumDb; peak = albumPeak; }
      else                  { db = trackDb; peak = trackPeak; }
    } else {                          // 'track'
      if (trackDb !== null) { db = trackDb; peak = trackPeak; }
      else                  { db = albumDb; peak = albumPeak; }
    }
    // If the mode-matched peak is missing, fall back to whichever peak IS
    // tagged so clip protection still engages — a file tagged with a gain but
    // no matching peak would otherwise apply that gain UNCAPPED (bounded only
    // by the +12 dB hard cap below) and a hot master could clip. The other
    // scope's peak is an imperfect but safe-enough ceiling; unity has none.
    if (peak === null || peak === undefined) {
      peak = (trackPeak !== null && trackPeak !== undefined) ? trackPeak
           : ((albumPeak !== null && albumPeak !== undefined) ? albumPeak : null);
    }
    if (db === null || db === undefined) {
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
    // Defence in depth: a non-finite value would throw on gain.value assignment.
    if (!Number.isFinite(linear)) linear = 1.0;
    replayGain.gain.value = linear;
  }

  // ── Internet-radio station mode ────────────────────────────────────────────
  // A station is NOT a queue track: it streams the server relay endpoint,
  // has no duration/seek, and never advances the queue.  The queue and its
  // index are left untouched so next/prev (or picking any track) drops the
  // user straight back into their music.
  let _stationMode = false;
  let _station = null;
  let _stationLogo = '';   // the current station's logo URL — the art fallback

  // External-audio suspend (mobile).  The mobile UI can hand audio output to a
  // DEDICATED radio <audio> element (radio-player.js) that streams a station
  // directly.  While suspended, the shared library element is paused with its
  // ``src`` detached and MUST NOT auto-resume, advance the queue, recover from
  // errors (force_transcode reload), or hand a SID partial off to its full
  // version — every such path early-returns on this flag so the paused library
  // track can never play UNDERNEATH the live stream.  Cleared when radio stops.
  // (QA 2026-07-05: clearing ``src`` alone was insufficient — the ended/error/
  // SID-handoff paths re-write ``src`` and guarded only on ``_stationMode``,
  // which the mobile radio path never sets.)
  let _extAudio = false;

  // Swap the player-bar art for a station: ``coverUrl`` when a now-playing song
  // cover was identified, else fall back to the station logo.  Re-renders via
  // the normal trackchange path so the art + ambient glow update together.
  function updateStationArt(coverUrl) {
    if (!_stationMode || !_track) return;
    _track.cover_art = coverUrl || _stationLogo || '';
    emit('trackchange', _track);
  }

  // Update the player bar with the now-playing SONG for a station: title shows
  // the song, the subtitle shows the artist (the renderer's normal title/artist
  // path).  Falls back to the station name / "Internet radio" between songs.
  // ``stationName`` and ``song``/``artist`` are also stashed on _track so the
  // radio-mode UI (ticker, station label) can read the structured values.
  function setStationNowPlaying(song, artist) {
    if (!_stationMode || !_track) return;
    _track.song = song || '';
    _track.songArtist = artist || '';
    _track.title  = song   || _track.stationName || (_station && _station.name) || 'Live';
    _track.artist = artist || 'Internet radio';
    emit('trackchange', _track);
  }

  async function playStation(station, relayUrl, codec = '') {
    _noteStart();
    _stationMode  = true;
    _suppressBufferingBadge = false;   // stations surface their own status
    _station      = station;
    trackId       = null;
    _metaDuration = 0;
    _seekOffset   = 0;
    _pendingSeekSec = null;
    _isTranscoded = false;
    _needsConvert = false;
    _playRecorded = true;        // never record a play-stat against a station
    _resetSidPartial();
    _stopTranscodePolling();
    _hideConvertBadge();
    if (_crossfadeTimer) { clearInterval(_crossfadeTimer); _crossfadeTimer = null; }
    _crossfading = false;
    const savedVol = localStorage.getItem('sb_volume');
    audio.volume = savedVol !== null ? parseFloat(savedVol) : 0.8;

    // Route the station through the Web Audio graph (EQ / ReplayGain / VU /
    // analyser) — playTrack does this, playStation used to skip it.  Without it
    // a radio-only session (no library track played first) leaves
    // createMediaElementSource unbuilt, so the stream bypasses the EQ entirely.
    // Guarded by ``if (ctx) return`` so it's a no-op once built; must run inside
    // the click gesture (Safari/iOS), which playStation is.
    _initAudioContext();
    if (ctx && ctx.state === 'suspended') {
      try { await ctx.resume(); } catch (_) {}
    }
    // Stations carry no ReplayGain tags and a live stream often never fires
    // ``loadedmetadata``, so reset the RG node to unity — otherwise the
    // previous track's gain (e.g. -8 dB) carries over and the station plays
    // too quiet.  ``_applyReplayGain(null)`` collapses to gain = 1.0.
    _applyReplayGain(null);

    // Synthetic "track" so the player bar renders the station like any
    // other now-playing item.  Empty id → art/waveform fetchers fall back
    // to their placeholder paths.
    _track = {
      id: '', title: station.name, artist: 'Internet radio',
      album: station.tags || '', duration: 0,
      format: codec || 'RADIO', station: true, sid: station.sid,
      stationName: station.name, song: '', songArtist: '',
      // Station logo as the default player-bar art; swapped for the
      // now-playing song cover when one is identified (see updateStationArt).
      cover_art: (station.favicon && /^https?:\/\//.test(station.favicon)) ? station.favicon : '',
    };
    _stationLogo = _track.cover_art;
    emit('trackchange', _track);
    // Free the PREVIOUS stream's decoder + close its relay socket before
    // attaching the new one.  Overwriting ``audio.src`` alone leaves the old
    // live-stream media resource alive until GC; with continuous radio streams
    // a couple of un-freed decoders is enough to OOM the renderer (the Edge
    // "Error code: 5" crash on switching stations).  removeAttribute+load()
    // forces the element to abort and release the old resource immediately,
    // which also propagates the disconnect so the server relay closes upstream.
    try { audio.pause(); } catch (_) {}
    audio.removeAttribute('src');
    audio.load();
    audio.src = relayUrl;
    audio.load();
    try {
      await audio.play();
      emit('statechange', { playing: true });
    } catch (err) {
      emit('error', { track: _track, error: err });
      throw err;
    }
  }

  function stopStation() {
    if (!_stationMode) return;
    _stationMode = false;
    _station = null;
    try { audio.pause(); } catch (_) {}
    audio.removeAttribute('src');
    // load() is required to make the element abort + release the live decoder
    // and propagate the disconnect that closes the relay socket — same contract
    // as playStation's teardown.  Without it the last station's decoder/socket
    // leaks until GC.
    audio.load();
    emit('statechange', { playing: false });
  }

  // ── Core playback ─────────────────────────────────────────────────────────
  async function playTrack(track, opts = {}) {
    const attempt = ++_playSeq;  // ANY start (row double-click, info panel, multiroom) disarms a pending auto-skip
    _noteStart();                // an open "Resume the queue from …?" offer no longer applies
    _cancelledStart = false;
    // Started from outside the queue logic (similar-tracks row, info panel, a
    // multiroom follower): the listener / master chose THIS track — if it cannot
    // play it is reported, never auto-skipped to an unrelated queue row.
    if (!opts.auto) _explicitPick = true;
    _stationMode   = false;      // any real track ends station mode
    _station       = null;
    _track         = track;
    _metaDuration  = track.duration || 0;
    _seekOffset    = 0;
    _pendingSeekSec = null;
    // A queue resumed from another device starts where that device was — once
    // the element knows its length ('loadedmetadata'; a pending seek would not
    // survive the new source's 'loadstart').
    _resumeSeek = (_resumeAt && _resumeAt.entry === track) ? { seq: attempt, sec: _resumeAt.sec } : null;
    _resumeAt = null;
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
    // Renderer formats (SID/MIDI/tracker/AdLib/GME) block until the full WAV
    // is cached — a guaranteed multi-second silent gap on a cold cache — so
    // they get an early "Rendering…" badge and skip the redundant buffering
    // badge.  In-flight transcodes (DSD/ALAC/generic) stream audio in <100 ms
    // and keep the delayed "Converting…" badge.
    const _serverRendered = _needsConvert
      && (SERVER_RENDERED_FORMATS.has(_fmtUp) || _isUadeAmiga(track));
    // Mirror to the module flag so the 'waiting' event handler (which can't see
    // this block-local const) also suppresses the buffering badge for renderers.
    _suppressBufferingBadge = _serverRendered;
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
    // Abort any in-flight transcode-status fetch + watchdog from the prior
    // track so a late response can't flash into the new track's badge and
    // its connection slot frees immediately.  NEVER touches the audio
    // element's own stream — only this ancillary metadata fetch.
    _stopTranscodePolling();

    // Feedback for any format requiring server-side processing.  Renderer
    // formats block with a guaranteed silent gap, so arm a short
    // RENDER_BADGE_DELAY and label the badge "Rendering…"; in-flight
    // transcodes keep the 6 s convert delay that only surfaces for genuinely
    // slow waits.  Either way the badge is hidden the instant audio is audible
    // (the ``playing`` handler + the audio.play() success path).
    _hideConvertBadge();
    if (_needsConvert) {
      _renderTextOnlyConvertBadge(_serverRendered ? 'Rendering…' : 'Converting…');
      _convertTimer = setTimeout(
        _showConvertBadge,
        _serverRendered ? RENDER_BADGE_DELAY : _getConvertDelay(),
      );
      // Determinate progress (bar + ETA) is pushed over the library
      // WebSocket: app.js's ``transcode_progress`` handler filters by the
      // playing track id and calls Player.onTranscodeProgress(...), which
      // drives _updateConvertBadgeProgress and — on ready — the
      // ``transcode-ready`` waveform-refresh emit.  Renderer formats emit no
      // ffmpeg progress, so they stay on the indeterminate "Rendering…" label.
    }

    // Media Session API — enables system media keys + lock screen widget
    if ('mediaSession' in navigator) {
      // The OS Now-Playing widget (macOS Control Center, Android, lock
      // screen) fetches these URLs itself, so they must be ABSOLUTE.
      // ``track.cover_art`` is usually null (art is extracted on demand),
      // so point at the ``/api/art`` endpoint — it returns the real cover
      // or the format placeholder, never nothing.  Without this the widget
      // falls back to the browser's own icon (the "Firefox" logo).
      const artwork = [];
      if (track.id) {
        const base = `${location.origin}/api/art/${encodeURIComponent(track.id)}`;
        artwork.push(
          { src: `${base}?size=sm`, sizes: '256x256', type: 'image/jpeg' },
          { src: `${base}?size=lg`, sizes: '512x512', type: 'image/jpeg' },
        );
      } else if (track.cover_art) {
        artwork.push({ src: track.cover_art, sizes: '512x512', type: 'image/jpeg' });
      }
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
    // Gapless seam: if the previous track's tail preloaded THIS track into a
    // blob, play from the blob — no network fetch at the seam.  The blob URL
    // is revoked on the NEXT track change (revoking while it's the active
    // src would kill playback).
    if (_activeBlobUrl) { try { URL.revokeObjectURL(_activeBlobUrl); } catch (_) {} _activeBlobUrl = null; }
    // Match on (id, subsong): a preload for the file's default tune must NOT be
    // reused for a specific subsong (it would play the wrong tune at the seam).
    const _curSub = Number(track.subsong) > 0 ? Number(track.subsong) : 0;
    // ── In-browser SID render → server cache-warm (flag-gated) ─────────────
    // SID audio ALWAYS plays from the server's instant progressive/cached stream
    // (the fall-through below) — NEVER blocked on a render or even the probe.
    // When the flag is on AND the browser can render, we ADDITIONALLY run the
    // in-browser render in the BACKGROUND (debounced, entirely off the audio
    // path) to (a) drive the per-voice VU and (b) warm the server cache (WAV+VU)
    // so every later play — any client, cast, offline — is a zero-render cache
    // hit.  Audio never depends on any of it.  Remove = delete this block + the
    // app.js 'sidwarm' handler.  (VU for warm-hits / incapable browsers comes
    // from the server .vu sidecar via _handleVU's poll — see app.js.)
    const _sidWasmActive = false;   // SID always plays the server stream below
    if (sidWasmPlaybackEnabled() && isC64SidTrack(track) && sidWasmSupported()) {
      _dropNextPreload();
      const _bgId = track.id, _bgSub = _curSub;
      const _bgLens = Array.isArray(track.hvsc_lengths) ? track.hvsc_lengths : null;   // in tune order
      const _bgLen = _bgLens ? Number(_bgLens[subsongWireToTune(_bgSub, subsongStartOf(track),
                                                                 track.subsongTotal || track.subsongs) - 1]) : 0;
      const _bgFallbackDur = Math.max(1, Math.min(600, Math.round(
        (_bgLen > 0 ? _bgLen : (Number(track.duration) || 0)) || 180)));
      (async () => {
        // Debounce (C1): skipping through cold SIDs must not pile full renders
        // on the serial worker — only the tune the user actually lands on renders.
        await new Promise((res) => setTimeout(res, 400));
        if (trackId !== _bgId) return;
        const _status = await sidRenderStatus(_bgId, _bgSub);
        if (trackId !== _bgId) return;
        if (_status && _status.ready) return;              // already warm → nothing to render/warm
        // Render to the SERVER's target length so a warmed WAV lands in the exact
        // slot /stream requests.
        const _target = (_status && _status.target_seconds > 0) ? _status.target_seconds : _bgFallbackDur;
        if (_target > 600) return;                         // client can't render the full length
        const r = await renderSidForPlayback(_bgId, _bgSub, _target);
        if (!r || r.superseded) return;
        if (r.url) { try { URL.revokeObjectURL(r.url); } catch (_) {} }   // VU/warm only — audio is the server stream
        if (r.vumr && trackId === _bgId) {
          emit('sidwasmvu', { id: _bgId, subsong: _bgSub, vumr: r.vumr });
        }
        // Warm only when the server can key it (default fidelity + matched
        // target) AND it hasn't self-cached during the listen (F2: re-probe →
        // skip the redundant ~50 MB upload the server would 204-drop anyway).
        if (_status && _status.warm_eligible && _status.target_seconds === _target && r.wav) {
          const fresh = await sidRenderStatus(_bgId, _bgSub);
          if (!(fresh && fresh.ready)) {
            emit('sidwarm', { id: _bgId, subsong: _bgSub, dur: _target, wav: r.wav, vumr: r.vumr, tune: r.tune });
          }
        }
      })();
    }
    if (!_sidWasmActive) {
      if (_nextPreload && _nextPreload.id === track.id && _nextPreload.subsong === _curSub && _nextPreload.url) {
        _activeBlobUrl = _nextPreload.url;
        _nextPreload = null;
        audio.src = _activeBlobUrl;
      } else {
        _dropNextPreload();                 // stale preload for some other track/tune
        audio.src = _streamUrlFor(track.id);
      }
    }
    // Force fetch even though the element has preload="none" — without this
    // the browser would otherwise wait until play() is called to start
    // loading, defeating _waitForBuffer below.
    audio.load();

    // Hold playback until the configured preload buffer (default 5 s) is
    // satisfied. Never blocks longer than 8 s — _waitForBuffer always
    // resolves so the UI stays responsive on slow / transcoding sources.
    const _bufSec = _getPreloadBuffer();
    if (_bufSec > 0) {
      // Renderer formats already show the "Rendering…" badge — don't stack the
      // buffering badge on top of it.
      if (!_serverRendered) _showBufferingBadge();
      try { await _waitForBuffer(audio, _bufSec, 8000); }
      finally { _hideBufferingBadge(); }
      if (attempt !== _playSeq) return;     // a newer start, or the badge's × cancelled this one
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
      } else {
        _watchRenderLength(track, attempt);   // a growing render of unknown length
      }
    } catch (err) {
      // Superseded (a newer start, or the badge's × cancelled this one): the
      // badge, poll and report now belong to whoever superseded it — touching
      // them here would clear the NEW track's "Rendering…" timer.
      if (attempt !== _playSeq) return;
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
      // A rendered track whose first attempt failed while the server may still
      // be rendering it: wait for the render instead of reporting a failure.
      if (_renderRecoverySeq === attempt) return;
      if (_startRenderRecovery(track, attempt, (why) => _reportPlayFailure(track, title, err, attempt, why))) return;
      _reportPlayFailure(track, title, err, attempt);
    }
  }

  // [render-core:begin]
  // A format name as a failure toast shows it: in the server's own casing
  // ('FastTracker 2', 'MusiclineEditor', 'MP3') — only a short all-lowercase
  // token (an extension-style 'mp3') is upper-cased.
  function _fmtLabel(f) {
    const s = String(f || '').trim();
    return /^[a-z0-9]{2,5}$/.test(s) ? s.toUpperCase() : s;
  }
  // The play() rejection report: one diagnostic toast per attempt.
  // ``serverReason``: why the server's render failed (render-status
  // ``state: 'failed'``) — said as is, with no probe.
  function _reportPlayFailure(track, title, err, attempt, serverReason = '') {
    // Diagnostic toast: one toast only.  Race a 1-byte probe of the stream
    // against a 400 ms timeout.  Whoever resolves first writes the toast —
    // the other arm is suppressed.  This way the user sees a meaningful
    // "Source unavailable" / "Track or file missing on disk" message when
    // the probe lands quickly, but never has to wait long for a less
    // specific fallback if the server itself is unreachable.
    const fmt = _fmtLabel(track && track.format);
    const fmtHint = fmt ? ` · ${fmt}` : '';
    // ``err`` is a DOMException from play() or, from the <audio> 'error' event, a
    // MediaError (no ``name``, only a ``code``).
    const errName = { NotSupportedError: 'unsupported or missing file', AbortError: 'interrupted',
                      NotAllowedError: 'the browser blocked playback' }[err && err.name]
      || { 2: 'network error', 3: 'unreadable file', 4: 'unsupported or missing file' }[err && err.code]
      || 'playback error';
    const genericReason = `Couldn\u2019t play \u201c${title}\u201d${fmtHint} (${errName}).`;
    let toastShown = false;
    const showOnce = (msg) => {
      if (toastShown) return;
      toastShown = true;
      // Guard once more: between the play() failure and the probe
      // resolving the user may have moved on.  Don't backseat-toast.
      if (track.id !== trackId) return;
      // One toast per ATTEMPT, not per track: a retry of the same row that fails
      // again must say so again.
      if (_failToastFor === track.id && _failToastSeq === attempt) return;   // the <audio> 'error' handler already reported it
      _failToastFor = track.id; _failToastSeq = attempt;                     // …and it stays quiet if we got here first
      Toast.error(msg + _nextHint());
    };
    const why = typeof serverReason === 'string' ? serverReason.trim().slice(0, 200) : '';
    if (why) {
      // The render itself failed and the server said why: no probe (it would
      // only run the failing render again).
      showOnce(`Couldn\u2019t play \u201c${title}\u201d${fmtHint}: ${why.replace(/[.\s]+$/, '')}.`);
      emit('statechange', { playing: false });
      emit('error', { track, error: err });
      return;
    }

    // Arm the probe.  A 1-byte Range GET, not HEAD: the stream route has a
    // HEAD handler, but it never renders, transcodes or fetches a remote
    // source, so it cannot surface a render or source failure — a Range
    // request for bytes 0-0 goes through the real GET path (404 / 502 /
    // 200), and its body is at most one byte, so we cancel almost at once.
    // We pass an AbortController so the server doesn't keep transcoding
    // once we've seen the status code.
    try {
      const url = _streamUrlFor(track.id);
      const ctrl = new AbortController();
      fetch(url, {
        method: 'GET',
        headers: { 'Range': 'bytes=0-0' },
        signal: ctrl.signal,
        credentials: 'same-origin',
      }).then(async res => {
        if (res.ok || res.status === 206) {
          // 200 or 206 Partial Content → the source is fine; play failed for
          // browser-side reasons (codec, corrupt frame).  Abort the (large)
          // audio body; we only needed the status.  Safari throws on .body
          // access for some opaque responses — guard.
          try { ctrl.abort(); } catch (_) {}
          showOnce(genericReason);
          return;
        }
        const code = res.status;
        // Error responses carry a SMALL JSON body with a human-readable
        // ``detail`` (e.g. "This file is empty or corrupt") — read it and
        // prefer it over the bare status code so the listener understands WHY.
        let detail = '';
        try {
          const body = await res.json();
          if (body && typeof body.detail === 'string') detail = body.detail.trim().slice(0, 200);
        } catch (_) {}
        try { ctrl.abort(); } catch (_) {}
        const httpReason = detail ||
          (code === 502 ? `Source unavailable (share unreachable or ffmpeg failed)` :
           code === 503 ? `Server busy — try again in a moment` :
           code === 504 ? `Source timed out — share may be unreachable` :
           code === 404 ? `Track or file missing on disk (rescan to refresh)` :
           code === 403 ? `Sign in required to play this track` :
           code === 401 ? `Sign in required` :
                          `Server returned HTTP ${code}`);
        showOnce(`Couldn\u2019t play \u201c${title}\u201d${fmtHint}: ${String(httpReason).replace(/[.\s]+$/, '')}.`);
      }).catch((err) => {
        if (err && err.name === 'AbortError') return;
        showOnce(`Couldn\u2019t reach the server for \u201c${title}\u201d${fmtHint} \u2014 check your connection.`);
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

  // ``?subsong=N`` for an entry that pins a tune (N > 0), '' otherwise: the
  // server treats 0 and "absent" alike, so 0 stays off the URL.
  function _subQs(t) {
    const s = Number(t && t.subsong);
    return s > 0 ? `?subsong=${s}` : '';
  }
  // render-status of track ``id`` — for the tune that is playing when ``id`` is
  // the current track (the waits below only carry the id).
  function _renderStatusUrl(id) {
    return `/api/stream/${encodeURIComponent(id)}/render-status${_subQs(_track && _track.id === id ? _track : null)}`;
  }
  // ``url`` with a throwaway ``r=`` parameter (the server ignores it), for a
  // reload after a failed or growing response: it must fetch anew.  Chromium
  // re-requests on load() anyway (measured); the distinct URL keeps any media
  // cache from answering with the old response.
  function _freshUrl(url) {
    return `${url}${url.includes('?') ? '&' : '?'}r=${Date.now().toString(36)}`;
  }
  // One id per page load, sent as ``pw=`` with every prewarm and retain
  // request: a retain from this page drops only this page's queued prewarms,
  // never those of another tab signed in as the same listener.
  // (crypto.randomUUID exists only in secure contexts — plain-http LAN
  // installs get the Math.random id.)
  const PREWARM_PAGE_ID = (() => {
    try {
      if (globalThis.crypto && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID().replace(/-/g, '');
      }
    } catch (_) { /* fall through */ }
    return (Math.random().toString(36).slice(2) + Date.now().toString(36)).slice(0, 32);
  })();
  // The look-ahead prewarm request for queue entry ``t``.  ``next``: it plays
  // right after the current track, so it jumps the server's background queue.
  // A pinned tune is warmed, not the file's default one.
  function _prewarmUrl(t, next) {
    const params = new URLSearchParams();
    params.set('priority', next ? 'next' : 'ahead');
    const ss = Number(t.subsong);
    if (ss > 0) params.set('subsong', String(ss));
    if (t.path) params.set('path', t.path);
    params.set('pw', PREWARM_PAGE_ID);
    return `/api/stream/${t.id}/prewarm?${params}`;
  }

  // ── Rendered tracks: wait for the render instead of failing ─────────────
  // The first play of a cold SID / Amiga / tracker track can fail while the
  // server is still rendering it (a proxy timeout, the render finishing just
  // after the browser gave up).  Once per attempt: keep "Rendering…" up, ask
  // the server whether it is still working, and play again once it is done —
  // from where playback was (a network blip mid-tune resumes there, not at
  // 0:00) or from a seek the listener asked for meanwhile.  Only when it has
  // nothing (the render failed) is the failure reported — through ``giveUp``,
  // the path that would have reported it anyway, with the server's reason
  // when it names one (``state: 'failed'``, ``error``).
  let _renderRecoverySeq = -1;     // attempt whose recovery is running
  let _renderRecoveryUsed = -1;    // attempt that already had its one recovery
  const RENDER_RECOVERY_MAX_MS = 180000;
  const RENDER_RECOVERY_POLL_MS = 1500;
  function _startRenderRecovery(t, seq, giveUp) {
    if (!t || !_isRenderedTrack(t) || _renderRecoveryUsed === seq || _stationMode || _extAudio) return false;
    _renderRecoveryUsed = seq;
    _renderRecoverySeq = seq;
    const id = t.id;
    // Read now: the failed source is still on the element (0 on a first play).
    const wasAt = audio.currentTime || 0;
    const current = () => trackId === id && _playSeq === seq;
    _renderTextOnlyConvertBadge('Rendering…');
    _showConvertBadge();
    (async () => {
      let state = 'idle', last = null;
      const until = Date.now() + RENDER_RECOVERY_MAX_MS;
      while (Date.now() < until) {
        try {
          const r = await fetch(`/api/stream/${encodeURIComponent(id)}/render-status${_subQs(t)}`,
                                { cache: 'no-store', credentials: 'same-origin' });
          if (r.ok) { last = (await r.json()) || {}; state = last.state || 'idle'; }
        } catch (_) { state = 'unknown'; }
        if (!current()) return;
        if (state !== 'rendering' && state !== 'queued' && state !== 'unknown') break;
        await new Promise((res) => setTimeout(res, RENDER_RECOVERY_POLL_MS));
        if (!current()) return;
      }
      if (!current()) return;
      _renderRecoverySeq = -1;      // a failure from here on is reported normally
      if (state !== 'complete' && state !== 'ready_for_playback') {
        _hideConvertBadge();
        // A failed render names its reason: report that as is.  Anything else
        // (nothing cached, a server without failure reasons) keeps the probe.
        const why = state === 'failed' && last && typeof last.error === 'string' ? last.error : '';
        giveUp(why);
        return;
      }
      const at = _pendingSeekSec != null ? _pendingSeekSec : wasAt;
      audio.src = _freshUrl(_streamUrlFor(id));
      audio.load();
      // Land on ``at`` before the first frame is heard: metadata arrives before
      // play() can start, so this one-shot seeks first; the check after play()
      // covers an engine that dropped it.
      const target = () => Math.min(at, (isFinite(audio.duration) && audio.duration > 0) ? audio.duration - 0.1 : at);
      const seekOnMeta = () => { if (current()) { try { audio.currentTime = target(); } catch (_) {} } };
      if (at > 0.5) audio.addEventListener('loadedmetadata', seekOnMeta, { once: true });
      try {
        await audio.play();
        if (!current()) return;
        if (at > 0.5 && Math.abs((audio.currentTime || 0) - target()) > 2) {
          try { audio.currentTime = target(); } catch (_) {}
        }
        _hideConvertBadge();
        emit('statechange', { playing: true });
        _watchRenderLength(t, seq);
      } catch (e) {
        audio.removeEventListener('loadedmetadata', seekOnMeta);
        if (!current()) return;
        _hideConvertBadge();
        if (e && e.name === 'NotAllowedError') {
          Toast.error('Browser blocked autoplay — click play again.');
          emit('statechange', { playing: false });
        }
        // Anything else: the <audio> 'error' event reports it (recovery used).
      }
    })();
    return true;
  }

  // ── The × on the "Rendering…" / "Converting…" badge ───────────────────────
  // Stops starting this track, on the client: the server finishes its render
  // into the cache (a later play is then instant).  Bumping the attempt ends the
  // render-recovery poll, a pending buffer wait and any auto-skip; dropping the
  // source ends the stream request and keeps a late error on it quiet.  Play
  // starts the same track again (playPause).
  let _cancelledStart = false;
  function _cancelPendingStart() {
    const seq = ++_playSeq;
    _renderRecoverySeq = -1;
    _renderRecoveryUsed = seq;
    _cancelledStart = true;
    _resetSidPartial();
    _stopTranscodePolling();
    _hideConvertBadge();
    _hideBufferingBadge();
    try { audio.pause(); audio.removeAttribute('src'); audio.load(); } catch (_) {}
    emit('statechange', { playing: false });
  }

  // ── A render of unknown length, played while it grows ────────────────────
  // A cold Amiga tune whose length nobody knows yet is streamed while it
  // renders, under a provisional "read to the end" WAV header — the element's
  // duration (hours, or none) means nothing until the render is complete.
  // While the render is still running and the server reports the stream as
  // provisional (``provisional: true``), or the element shows such a length,
  // the length reads as unknown (so seeking is off); once the render is
  // complete the player takes the exact length and moves the element onto the
  // finished file at the same position, so length and seeking are exact.  A
  // track that turns out not to need this costs one render-status request.
  // Runs whatever length the row carries: the server serves a render of
  // unknown length for any tune but the default one (the row's length is the
  // default tune's) and for long tunes, and only its answer says which it is.
  // A growing render served under a KNOWN length is watched to the end too:
  // that length came from the library row, and a stale one would pad the tune
  // with silence or cut it short — once complete, a length more than
  // RENDER_LENGTH_SLACK_SEC off the render's exact one moves the element onto
  // the finished file (a listener already past the tune's real end lands just
  // before it, so the track ends and advances as it naturally would).
  const RENDER_LENGTH_POLL_MS = 2000;
  const RENDER_LENGTH_SLACK_SEC = 1.5;
  const RENDER_LENGTH_BUDGET_MS = 5 * 60 * 1000;
  function _watchRenderLength(t, seq) {
    if (!t || !_isRenderedTrack(t)) return;
    const fmt = String(t.format || '').toUpperCase();
    if (fmt === 'SID' || fmt === 'PSID') return;       // SID has its own partial → full path
    const id = t.id;
    const current = () => trackId === id && _playSeq === seq && !_stationMode && !_extAudio;
    (async () => {
      const until = Date.now() + RENDER_LENGTH_BUDGET_MS;
      while (current() && Date.now() < until) {
        let j = null;
        try {
          const r = await fetch(`/api/stream/${encodeURIComponent(id)}/render-status${_subQs(t)}`,
                                { cache: 'no-store', credentials: 'same-origin' });
          if (r.ok) j = await r.json();
        } catch (_) { /* transient — ask again */ }
        if (!current()) return;
        // A provisional header's length is far off: hours, or none at all.
        const d = audio.duration;
        const exact = (j && Number(j.duration_seconds)) || 0;
        const bogus = !isFinite(d) || d <= 0 || (exact > 0 ? d > exact + 30 : d > PROVISIONAL_LENGTH_SEC);
        if (j && j.state === 'complete') {
          if (exact > 0) {
            _metaDuration = exact;
            // A tune other than the file's default one carries no stored
            // length: its queue entry takes the exact one (the library row
            // keeps the default tune's).
            if (Number(t.subsong) > 0) t.duration = exact;
          }
          const wasProvisional = _provisional;
          _provisional = false;
          // The header's length (a stored one) is not the render's: padded or cut.
          const off = exact > 0 && isFinite(d) && d > 0 && Math.abs(d - exact) > RENDER_LENGTH_SLACK_SEC;
          if (wasProvisional || bogus || off) {
            const at = audio.currentTime || 0;
            await _switchToCachedRender(exact > 0 ? Math.min(at, Math.max(0, exact - 0.05)) : at,
                                        { fresh: true });
          }
          return;
        }
        if (j) {
          const growing = j.state === 'ready_for_playback' || j.state === 'rendering';
          // Not a render in progress (nothing running, queued, failed, not a
          // uade render): nothing to watch.
          if (!growing) { _provisional = false; return; }
          // Still growing: keep asking until it completes.  The length reads
          // as unknown only when the header's is provisional; a known one
          // keeps seeking on.
          _provisional = j.provisional === true || bogus;
        }
        await new Promise((res) => setTimeout(res, RENDER_LENGTH_POLL_MS));
      }
      if (current()) _provisional = false;   // budget spent — trust the element again
    })();
  }

  // Move the element onto the complete cached render of the CURRENT track and
  // carry on from ``resumeAt`` (SID partial → full version; a growing render →
  // the finished file).  A no-op once the listener has moved on (another track,
  // a cancel, a station or radio takeover) — checked again after the bounded
  // wait for ``canplay``.  An error on the new source is the <audio> 'error'
  // handler's to report.  ``play``: resume playback afterwards (default: if it
  // was playing — the end-of-partial hand-off passes true, the element has just
  // ended).  ``fresh``: the same URL just served a growing file (_freshUrl).
  async function _switchToCachedRender(resumeAt, { fresh = false, play = null } = {}) {
    if (!trackId || _extAudio || _stationMode) return;
    const id = trackId, seq = _playSeq;
    const still = () => trackId === id && _playSeq === seq && !_extAudio && !_stationMode;
    _seekOffset = 0;
    const wasPlaying = play !== null ? !!play : !audio.paused;
    audio.pause();
    const url = _streamUrlFor(id);
    audio.src = fresh ? _freshUrl(url) : url;
    // The element is preload="none": without load() it fetches nothing until
    // play(), and ``canplay`` never comes (measured in Chromium: readyState 0,
    // no request).
    audio.load();
    const outcome = await new Promise((res) => {
      let timer = null;
      const done = (v) => {
        clearTimeout(timer);
        audio.removeEventListener('canplay', onReady);
        audio.removeEventListener('error', onError);
        res(v);
      };
      const onReady = () => done('ready'), onError = () => done('error');
      audio.addEventListener('canplay', onReady);
      audio.addEventListener('error', onError);
      timer = setTimeout(() => done('timeout'), 20000);
    });
    if (!still() || outcome === 'error') return;
    if (resumeAt > 0) { try { audio.currentTime = resumeAt; } catch (_) {} }
    if (wasPlaying) {
      audio.play().catch(err => {
        // Hand-off failure: log + surface, otherwise the user sees the
        // badge disappear and the player just sits paused.
        if (err && err.name === 'AbortError') return;
        console.warn('Switch to the cached render failed:', err);
        if (still()) Toast.error("Couldn't continue into the full version — press Play to retry.");
      });
    }
    _hideConvertBadge();
  }
  // [render-core:end]

  // ── Seeking ────────────────────────────────────────────────────────────────
  function seek(pct) {
    if (_stationMode) return;    // live stream — nothing to seek into
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
        const seekingSeq = _playSeq;       // a cancel from the badge × bumps it
        while (!_sidFullReady) {
          if (_playSeq !== seekingSeq) return;
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
            const res = await fetch(_renderStatusUrl(trackId));
            const j   = await res.json();
            if (j.ready) _sidFullReady = true;
          } catch (_) {}
        }
        if (trackId !== seekingTrackId || _playSeq !== seekingSeq) {
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
    if (_extAudio) return;   // radio owns output — ignore stray transport calls
    if (!audio.src) {
      // A start cancelled from the badge ×: Play starts that track again.
      if (_cancelledStart && _track && _track.id && trackId === _track.id) {
        _cancelledStart = false;
        _explicitPick = true;
        const cur = queue[queueIdx];
        if (cur && cur.id === _track.id) _playCurrent();
        else playTrack(_track);
        return;
      }
      // A queue restored after a reload has no source on the element yet: Play /
      // Space must start its current track, not twitch the button and do nothing.
      // (trackId is set as soon as a start is under way — a second Space inside the
      // AudioContext-resume await must not start the same track again.)
      // (queueIdx is -1 after "clear queue → add to queue": start at the top then.)
      const at = Math.max(0, queueIdx);
      const cur = queue.length ? queue[at] : null;
      if (cur && trackId !== cur.id) {
        queueIdx = at;
        _explicitPick = true;
        _playCurrent();            // start + queuechange + save + lookahead (prefetch the next rows)
      }
      return;
    }
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

  // [queue-core:begin]
  // ── Queue source paging ─────────────────────────────────────────────────
  function _newSeed() {
    // 31 bits: survives JSON + query strings on every engine, plenty of orders.
    return Math.floor(Math.random() * 0x7fffffff) + 1;
  }

  function _shuffleInPlace(a) {
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  // One page of the source's order.  ``kind`` = 'ordered' (the view's own list
  // endpoint + params) | 'shuffled' (the seeded permutation of the same filter).
  // Both answer either a bare array or ``{total, tracks}``.
  const PAGE_TIMEOUT_MS = 20000;
  async function _fetchPage(src, kind, offset, limit, seed) {
    const spec = src && (kind === 'shuffled' ? src.shuffle : src.ordered);
    if (!spec || !spec.url) return { tracks: [], total: null, ended: true };
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(spec.params || {})) {
      if (v !== null && v !== undefined && v !== '') p.set(k, String(v));
    }
    p.set('offset', String(offset));
    p.set('limit', String(limit));
    if (kind === 'shuffled') p.set(spec.seedParam || 'seed', String(seed));
    // A request that never answers (half-open connection, laptop woken on a dead
    // network) must FAIL, not hang: a pending page would park the lookahead — and
    // "Shuffle all" — for the rest of the session.  A cold deal takes ~0.2 s.
    const init = { credentials: 'same-origin' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) init.signal = AbortSignal.timeout(PAGE_TIMEOUT_MS);
    const r = await fetch(`${spec.url}?${p}`, init);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    const tracks = Array.isArray(j) ? j : (Array.isArray(j?.tracks) ? j.tracks : []);
    const total  = (!Array.isArray(j) && typeof j?.total === 'number') ? j.total : null;
    return { tracks, total };
  }

  function _pageWasLast(tracks, total, offset, limit, mode) {
    // A shuffled order reports its own exact length, so only that decides: an
    // empty page inside it just means those ids were deleted since it was dealt.
    if (mode === 'shuffled' && total != null) return offset + limit >= total;
    if (!tracks.length) return true;       // (an ordered view's total can overstate its list)
    if (total != null) return offset + limit >= total;
    return tracks.length < limit;          // bare-array endpoints: short page = end
  }

  // Can the ACTIVE order be paged from the server?  A source without a spec for
  // the active order — e.g. ``ordered: null`` on a client-sorted or capped list
  // playing in list order — is a plain array in every respect: the loaded rows
  // are all there is, so it wraps, persists whole and is never trimmed.
  function _srcPageable() {
    return !!_src && !!(_srcMode === 'shuffled' ? _src.shuffle : _src.ordered);
  }

  // More tracks exist beyond the loaded window?
  function _srcHasMore() { return _srcPageable() && !_srcEnded && !_radioActive; }

  // The track "next" will play, or null.  A pageable window never wraps to its
  // own first row (that is not the list's first track); a plain array wraps
  // only under repeat-all, as before.
  function _peekNext() {
    if (queueIdx < queue.length - 1) return queue[queueIdx + 1];
    if (!_srcPageable() && repeatMode === 'all' && queue.length > 1) return queue[0];
    return null;
  }
  function _hasNextTrack() {
    return queueIdx < queue.length - 1 || _srcHasMore();
  }

  function _trimBehind() {
    if (!_srcPageable()) return;
    const drop = queueIdx - Q_KEEP_BEHIND;
    if (drop <= 0) return;
    queue = queue.slice(drop);
    queueIdx -= drop;
  }

  function _notePositions(tracks, firstPos) {
    tracks.forEach((t, i) => { if (t && typeof t === 'object') _ordPos.set(t, firstPos + i); });
  }

  // The list row that would play next in list order: the first upcoming track
  // that has a list position, else the row after the current one, else wherever
  // ordered paging had got to.
  function _orderedResumePoint() {
    for (let i = queueIdx + 1; i < queue.length; i++) {
      const pos = _ordPos.get(queue[i]);
      if (pos != null) return pos;
    }
    const cur = _ordPos.get(queue[queueIdx]);
    return cur != null ? cur + 1 : _ordNext;
  }

  // Keep at least Q_LOOKAHEAD tracks queued ahead of the current one by pulling
  // the next page of the active order.  Idempotent + deduped; safe to call on
  // every advance.
  function _ensureLookahead() {
    if (!_srcHasMore() || _extendP) return;
    if (queue.length - 1 - queueIdx >= Q_LOOKAHEAD) return;
    const gen = _srcGen, mode = _srcMode, src = _src, seed = _seed;
    const off = mode === 'shuffled' ? _shufNext : _ordNext;
    if (_srcTotal != null && off >= _srcTotal) { _srcEnded = true; return; }
    const p = _fetchPage(src, mode, off, Q_PAGE, seed).then(({ tracks, total }) => {
      if (gen !== _srcGen || mode !== _srcMode) return;      // queue was replaced meanwhile
      if (total != null) _srcTotal = total;
      if (mode === 'shuffled') _shufNext = off + Q_PAGE;
      else { _ordNext = off + Q_PAGE; _notePositions(tracks, off); }
      if (_pageWasLast(tracks, _srcTotal, off, Q_PAGE, mode)) _srcEnded = true;
      // Ordered: skip anything already queued.  Shuffled: skip what is current /
      // still ahead, and whatever already PLAYED outside this order (the pinned
      // track reappears once in the permutation).  Rows left behind the current
      // track that never played are fair game — the deal covers them too.
      const have = new Set((mode === 'shuffled' ? queue.slice(queueIdx) : queue).map(t => t && t.id));
      const fresh = tracks.filter(t => t && t.id && !have.has(t.id)
        && !(mode === 'shuffled' && _shufExtras.has(t.id)));
      if (fresh.length) {
        queue = queue.concat(fresh);
        _trimBehind();
        emit('queuechange', { queue, queueIdx });
        _saveQueueSoon();
      }
      return true;
    }).catch(() => false);      // offline / 5xx: give up quietly, the next advance retries
    _extendP = p;
    p.then((ok) => {
      if (_extendP === p) _extendP = null;
      // A page that added nothing (all duplicates) leaves the window short —
      // pull the next one.  Never on failure, or a dead server would hot-loop.
      if (ok && gen === _srcGen) _ensureLookahead();
    });
  }

  function _startTrack(t) {
    if (t && typeof t === 'object') _played.add(t);
    playTrack(t, { auto: true }); // queue-driven start: _explicitPick is the caller's call (bumps _playSeq)
  }

  function _playCurrent() {
    _startTrack(queue[queueIdx]);
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
    _ensureLookahead();
  }

  // Start the active order over (end of list under repeat-all, or a manual Next
  // on the very last track): ordered → from the top, shuffled → a fresh seed.
  async function _restartSource() {
    const gen = ++_srcGen;
    const last = queue[queueIdx];
    const mode = _srcMode;
    const seed = mode === 'shuffled' ? _newSeed() : _seed;
    _extendP = null;
    // Nothing of the OLD pass is touched until the first page of the new one is
    // in hand: a failed fetch must leave seed / offsets / extras exactly as they
    // were, so the next attempt restarts cleanly instead of resuming a half-reset
    // order (which skipped the rows still sitting in the window).
    let page, failed = false;
    try { page = await _fetchPage(_src, mode, 0, Q_PAGE, seed); }
    catch (_) { page = { tracks: [] }; failed = true; }
    if (gen !== _srcGen || mode !== _srcMode) return;
    if (!page.tracks.length) {
      emit('statechange', { playing: false });
      if (failed) emit('queuestall', {});
      return;
    }
    if (page.total != null) _srcTotal = page.total;
    if (_srcMode === 'shuffled') {
      _seed = seed;
      _shufNext = Q_PAGE;
      _shufExtras = new Set();                    // a new pass: everything may play again
      _played = new WeakSet();
      // …but not the track that just ended, back to back.
      if (page.tracks.length > 1 && last && page.tracks[0].id === last.id) {
        page.tracks.push(page.tracks.shift());
      }
    } else { _ordNext = Q_PAGE; _notePositions(page.tracks, 0); }
    _srcEnded = _pageWasLast(page.tracks, _srcTotal, 0, Q_PAGE, _srcMode);
    queue = page.tracks;
    queueIdx = 0;
    _playCurrent();
  }

  // Advance to the next track in play order.  NOT a user action — natural
  // track end and crossfade come through here, so they never count as a skip.
  function _advance() {
    if (!queue.length) return;
    _explicitPick = false;
    if (queueIdx < queue.length - 1) {
      queueIdx++;
      _playCurrent();
      return;
    }
    if (_srcHasMore()) {
      // Ran past the loaded window faster than the lookahead (rapid skipping):
      // wait for the pending page, then step.
      _ensureLookahead();
      const pending = _extendP;
      if (pending) {
        const gen = _srcGen;
        pending.then((ok) => {
          if (gen !== _srcGen) return;
          if (queueIdx < queue.length - 1) { queueIdx++; _playCurrent(); }
          else if (ok) _advance();     // page added nothing new → keep going / wrap
          else {
            // The next page could not be fetched and nothing is left to play.  Stay
            // put (the next Next / Play retries) — but SAY so: music that just stops
            // reads as a crash.
            emit('statechange', { playing: false });
            emit('queuestall', {});
          }
        });
        return;
      }
      // No fetch was needed after all (the order just ended) → fall through.
      if (_srcHasMore()) return;
    }
    _wrapToStart();
  }

  function _wrapToStart() {
    if (_srcPageable() && !_radioActive) { _restartSource(); return; }
    _played = new WeakSet();                    // a new pass, shuffled or not: everything may play again
    if (shuffle && !_radioActive && queue.length > 1) {
      // A full pass of a shuffled plain queue is done — deal a new order rather
      // than replaying the identical one.
      const last = queue[queueIdx];
      queue = _shuffleInPlace(queue.slice());   // own copy — never reorder a caller's array
      if (queue[0] === last) [queue[0], queue[queue.length - 1]] = [queue[queue.length - 1], queue[0]];
    }
    queueIdx = 0;
    _playCurrent();
  }

  // Crossfade reached the fade window: the listener let the track play out, so
  // this is a CONTINUE for the P(skip) model (the old track's 'ended' never
  // fires once src changes, so it has to be recorded here).
  function _autoAdvance() {
    _recordAdvance(false);
    _invalidatePContinue();
    _advance();
  }

  function next() {
    if (!queue.length) return;
    // User-driven Next click counts as a SKIP for the P(continue)
    // heuristic.  Natural track-end is recorded in the 'ended' handler
    // below — that's CONTINUE.
    _recordAdvance(true);
    _invalidatePContinue();   // history changed; recompute on next read
    _advance();
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
    _explicitPick = true;     // the listener asked for THIS track: report a failure, don't bounce them forward
    // The queue is in play order, so "previous" is simply the entry before —
    // under shuffle that is the track that actually played before this one.
    if (queueIdx > 0) {
      queueIdx--;
      _playCurrent();
      return;
    }
    if (_srcPageable()) {
      // First row of a pageable window: page BACKWARDS through the ordered
      // list from that row's list position; a dealt / hand-queued first row has
      // none, so there is no "before".
      const first = _srcMode === 'ordered' ? _ordPos.get(queue[0]) : undefined;
      if (first > 0 && !_backP) {                 // one backward page at a time
        const gen = _srcGen, src = _src;
        const start = Math.max(0, first - Q_PAGE);
        const p = _fetchPage(src, 'ordered', start, first - start, 0).then(({ tracks }) => {
          if (gen !== _srcGen || !tracks.length) return;
          _notePositions(tracks, start);
          // The row right before queue[0] is where Prev must land, so it always
          // goes in; older rows that are already queued elsewhere are left out.
          const have = new Set(queue.map(t => t && t.id));
          const lastI = tracks.length - 1;
          const fresh = tracks.filter((t, i) => t && t.id && (i === lastI || !have.has(t.id)));
          if (!fresh.length) return;
          queue = fresh.concat(queue);
          queueIdx = fresh.length - 1;
          _playCurrent();
        }).catch(() => {});
        _backP = p;
        p.then(() => { if (_backP === p) _backP = null; });
      }
      return;
    }
    queueIdx = queue.length - 1;          // plain array: wrap, as before
    _playCurrent();
  }

  // Queue persistence across reloads — Apple Music / Spotify / Plexamp /
  // Roon all keep the queue across a refresh.  The previous behaviour
  // dumped a carefully-built listening session on every F5.  Save the
  // queue + index to localStorage on change; restore on construction.
  let _saveQueueTimer = null;
  function _saveQueueSoon() {
    if (_saveQueueTimer) clearTimeout(_saveQueueTimer);
    _saveQueueTimer = setTimeout(_saveQueueNow, 250);
  }
  function _saveQueueNow() {
    _saveQueueTimer = null;
    _scheduleQueueSync();
    try {
      // A pageable queue re-extends itself from ``src`` after a reload, so
      // only a slice around the current track needs to be stored — not the
      // whole window.  Anything that cannot be re-fetched (plain arrays, and a
      // source whose active order has no spec) is stored whole, as before.
      let from = 0, to = queue.length;
      if (_srcPageable() && queue.length > 120) {
        from = Math.max(0, queueIdx - 20);
        to   = Math.min(queue.length, queueIdx + 100);
      }
      const tracks = (from === 0 && to === queue.length) ? queue : queue.slice(from, to);
      const data = { tracks, idx: queueIdx - from, shuffle, savedAt: Date.now() };
      if (_src) {
        // Tail rows that were loaded but NOT stored must be fetched again after
        // a reload.  Back the resume offset up by that many rows plus one page
        // of slack (pages can hold skipped duplicates); any overlap is dropped
        // by id on append, so the only cost is one partly-redundant fetch.
        const cut = queue.length - to;
        const back = cut ? cut + Q_PAGE : 0;
        data.src = {
          desc: _src, mode: _srcMode, seed: _seed, total: _srcTotal,
          ordNext:  _srcMode === 'ordered'  ? Math.max(0, _ordNext  - back) : _ordNext,
          shufNext: _srcMode === 'shuffled' ? Math.max(0, _shufNext - back) : _shufNext,
          ordResume: _ordResume, ended: _srcEnded,
          // List positions of the stored rows (null = dealt / hand-queued).
          pos: tracks.map(t => { const v = _ordPos.get(t); return v == null ? null : v; }),
          // One short id each.  10,000 (≈ 400 KB) is a backstop for a marathon pass:
          // beyond it the OLDEST ids fall off and those tracks could be dealt again.
          extras: _srcMode === 'shuffled' ? [..._shufExtras].slice(-10000) : [],
        };
      }
      // What object identity / WeakSets carry in memory, as plain data:
      const man = tracks.map(t => (t && _manual.has(t) ? 1 : 0));
      if (man.includes(1)) data.man = man;
      // …and which rows actually PLAYED.  Position cannot stand in for it: a
      // playlist clicked at row 50 has 50 rows behind the current one that never
      // played, and the next shuffle must still be able to deal them.
      data.played = tracks.map(t => (t && typeof t === 'object' && _played.has(t) ? 1 : 0));
      const origIds = _origQueue ? _origQueue.map(t => t && t.id).filter(Boolean) : _origIds;
      if (origIds && origIds.length && origIds.length <= 50000) {   // ids only: ~40 B each
        data.orig = { ids: origIds, idx: _origIdx };     // the pre-shuffle list's ORDER
      }
      try {
        localStorage.setItem('sb_queue', JSON.stringify(data));
      } catch (_) {
        // Over quota (a few thousand unpageable rows).  An OLDER queue left on
        // disk would come back after a reload, so store what fits — the rows
        // around the current track, still WITH the source (so "shuffle everything"
        // survives the reload) — or nothing.
        try {
          const a = Math.max(0, queueIdx - 20), b = Math.min(queue.length, queueIdx + 300);
          const small = { tracks: queue.slice(a, b), idx: queueIdx - a, shuffle, savedAt: data.savedAt };
          if (data.src) {
            small.src = { ...data.src, extras: data.src.extras.slice(-2000),
                          pos: small.tracks.map(t => { const v = _ordPos.get(t); return v == null ? null : v; }) };
          }
          if (data.orig) small.orig = data.orig;
          small.played = small.tracks.map(t => (t && typeof t === 'object' && _played.has(t) ? 1 : 0));
          const sman = small.tracks.map(t => (t && _manual.has(t) ? 1 : 0));
          if (sman.includes(1)) small.man = sman;
          localStorage.setItem('sb_queue', JSON.stringify(small));
        } catch (_) {
          try { localStorage.removeItem('sb_queue'); } catch (_) {}
        }
      }
    } catch { /* malformed state — drop */ }
  }
  function _restoreQueueIfAny() {
    try {
      const raw = localStorage.getItem('sb_queue');
      if (!raw) return;
      const data = JSON.parse(raw);
      if (Array.isArray(data?.tracks) && data.tracks.length) {
        queue = data.tracks;
        queueIdx = Math.max(0, Math.min(data.tracks.length - 1, data.idx ?? 0));
        // Which rows played before the reload (the current one always did).  A
        // save from before this was recorded falls back to "everything behind it".
        const pl = Array.isArray(data.played) ? data.played : null;
        for (let i = 0; i < queue.length; i++) {
          const t = queue[i];
          if (!t || typeof t !== 'object') continue;
          if (i === queueIdx || (pl ? pl[i] : i < queueIdx)) _played.add(t);
        }
        const s = data.src;
        if (s && s.desc && typeof s.desc === 'object') {
          _src = s.desc;
          _srcMode = s.mode === 'shuffled' ? 'shuffled' : 'ordered';
          _seed = Number(s.seed) || _newSeed();
          _srcTotal = typeof s.total === 'number' ? s.total : null;
          _ordNext = Number(s.ordNext) || 0;
          _shufNext = Number(s.shufNext) || 0;
          _ordResume = Number(s.ordResume) || 0;
          if (Array.isArray(s.pos)) {
            queue.forEach((t, i) => {
              if (t && typeof t === 'object' && typeof s.pos[i] === 'number') _ordPos.set(t, s.pos[i]);
            });
          }
          if (Array.isArray(s.extras)) _shufExtras = new Set(s.extras.filter(x => typeof x === 'string'));
          _srcEnded = !!s.ended;
        }
        if (Array.isArray(data.man)) {
          queue.forEach((t, i) => { if (t && typeof t === 'object' && data.man[i]) _manual.add(t); });
        }
        if (data.orig && Array.isArray(data.orig.ids) && data.orig.ids.length) {
          _origIds = data.orig.ids.filter(x => typeof x === 'string');
          _origIdx = Number.isInteger(data.orig.idx) ? data.orig.idx : -1;
        }
        if (data.shuffle === true && !shuffle) {
          shuffle = true;
          emit('shufflechange', { shuffle });
        }
        // A view-backed queue saved while radio had shuffle suspended can carry
        // a flag that disagrees with its order — settle it before anything plays.
        // (Plain arrays are left exactly as saved: their order IS the state.)
        if (_src && shuffle !== (_srcMode === 'shuffled') && (shuffle ? _src.shuffle : true)) {
          if (shuffle) _shuffleUpcoming(); else _unshuffleUpcoming();
        }
        emit('queuechange', { queue, queueIdx });
        // Don't auto-play — load the queue silently so the user presses
        // Play themselves (browsers block autoplay without interaction
        // anyway).  The player bar shows what Play will start.
        _emitCue();
      }
    } catch { /* malformed — ignore */ }
  }
  // Restore on next microtask so callers binding to ``queuechange``
  // during their own init have time to attach listeners.
  Promise.resolve().then(_restoreQueueIfAny);

  // ── The queue on the server (sync across devices) ─────────────────────────
  // The queue is also kept on the server as the listener's play queue — the one
  // Subsonic apps read with getPlayQueue — so another device can pick it up:
  // PUT /api/me/play-queue QSYNC_DELAY_MS after the last change (a queue edit or
  // track change — every local save — or a pause), holding the library tracks in
  // a window around the current one, and only when that window, the current
  // entry or the position (to 5 s) changed: a paused tab sends nothing.  A page
  // being closed sends a pending change at once.  On load, a queue another
  // device saved AFTER this browser's own is offered ("Resume"), never applied
  // on its own.  Per browser: ``sb_queue_sync`` = '0' turns both off.  A server
  // without the endpoint (404 / 405) turns it off for the page.
  const QSYNC_KEY = 'sb_queue_sync';
  const QSYNC_OWN_KEY = 'sb_queue_sync_own';     // ``changed`` of this browser's last save
  const QSYNC_DELAY_MS = 10000;
  const QSYNC_BEFORE = 50, QSYNC_AFTER = 450;
  const QSYNC_URL = '/api/me/play-queue';
  let _qsyncTimer = null;
  let _qsyncSent = '';            // key of the last window sent
  let _qsyncSupported = null;     // null: not known yet · false: no endpoint
  let _resumeAt = null;           // { entry, sec }: where a resumed queue's first play starts
  let _resumeSeek = null;         // { seq, sec }: that start, until its metadata arrives
  function queueSyncEnabled() {
    try { return localStorage.getItem(QSYNC_KEY) !== '0'; } catch (_) { return true; }
  }
  function setQueueSyncEnabled(on) {
    try { localStorage.setItem(QSYNC_KEY, on ? '1' : '0'); } catch (_) {}
    if (on) _scheduleQueueSync();
    else { clearTimeout(_qsyncTimer); _qsyncTimer = null; }
  }
  // Who saved the queue, as other players show it: the shell's name plus a short
  // browser / OS hint ("SoniqBoom Web · Firefox on Mac"), so two browsers of the
  // same app tell apart.  At most 60 characters (the server takes 64).
  function _uaHint() {
    try {
      const ua = String((typeof navigator !== 'undefined' && navigator.userAgent) || '');
      const b = /Edg[A-Z]?\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera'
        : /Firefox\/|FxiOS\//.test(ua) ? 'Firefox' : /Chrome\/|CriOS\//.test(ua) ? 'Chrome'
        : /Safari\//.test(ua) ? 'Safari' : '';
      if (!b) return '';
      const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
        : /CrOS/.test(ua) ? 'ChromeOS' : /Macintosh|Mac OS X/.test(ua) ? 'Mac'
        : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : '';
      return os ? `${b} on ${os}` : b;
    } catch (_) { return ''; }
  }
  const _qsyncClient = () => {
    const base = (typeof window !== 'undefined' && window.__sbClientLabel) || 'SoniqBoom Web';
    const hint = _uaHint();
    return (hint ? `${base} · ${hint}` : base).slice(0, 60);
  };
  const _qsyncSignedIn = () => !!(typeof window !== 'undefined' && window.__sbAuth && window.__sbAuth.user);
  // A library entry's Subsonic song id (``<id>~<wire>`` for a pinned tune);
  // '' for anything else (a station, a stream, a bare file).
  function _qsyncId(t) {
    if (!t || typeof t !== 'object' || t.station || t.stream) return '';
    const id = typeof t.id === 'string' ? t.id : '';
    if (!id || id.includes('~')) return '';
    const ss = Number(t.subsong);
    return ss > 0 ? `${id}~${Math.floor(ss)}` : id;
  }
  function _qsyncBody() {
    if (_stationMode || !queue.length || queueIdx < 0 || queueIdx >= queue.length) return null;
    const from = Math.max(0, queueIdx - QSYNC_BEFORE);
    const to = Math.min(queue.length, queueIdx + QSYNC_AFTER);
    const ids = [];
    let cur = -1;
    for (let i = from; i < to; i++) {
      const id = _qsyncId(queue[i]);
      if (!id) continue;
      if (i === queueIdx) cur = ids.length;
      ids.push(id);
    }
    if (cur < 0) return null;                    // the current entry is not a library track
    const entry = queue[queueIdx];
    const sec = (entry && trackId === entry.id) ? (_currentTime() || 0) : 0;
    return { ids, current_index: cur, position: Math.max(0, Math.round(sec * 1000)),
             client: _qsyncClient() };
  }
  function _scheduleQueueSync() {
    if (_qsyncSupported === false || !queueSyncEnabled()) return;
    clearTimeout(_qsyncTimer);
    _qsyncTimer = setTimeout(() => { _qsyncTimer = null; _pushQueueSync(); }, QSYNC_DELAY_MS);
  }
  async function _pushQueueSync(keepalive = false) {
    if (_qsyncSupported === false || !queueSyncEnabled() || !_qsyncSignedIn()) return;
    const body = _qsyncBody();
    if (!body) return;
    const key = `${body.ids.join(',')}|${body.current_index}|${Math.floor(body.position / 5000)}`;
    if (key === _qsyncSent) return;
    _qsyncSent = key;
    try {
      const r = await fetch(QSYNC_URL, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), credentials: 'same-origin', keepalive,
      });
      if (r.status === 404 || r.status === 405) { _qsyncSupported = false; return; }
      if (!r.ok) { _qsyncSent = ''; return; }
      _qsyncSupported = true;
      // Remember the server's stamp of this save, so a later page load in this
      // browser never offers its own queue back.  A server that doesn't return
      // it is asked once — if the saved queue is still the one just sent.
      const j = await r.json().catch(() => null);
      let changed = j && Number(j.changed);
      if (!(changed > 0) && !keepalive) {
        const g = await fetch(QSYNC_URL, { cache: 'no-store', credentials: 'same-origin' });
        const q = g.ok ? await g.json().catch(() => null) : null;
        if (q && Array.isArray(q.ids) && q.ids.join(',') === body.ids.join(',')) changed = Number(q.changed);
      }
      if (changed > 0) { try { localStorage.setItem(QSYNC_OWN_KEY, String(changed)); } catch (_) {} }
    } catch (_) { _qsyncSent = ''; }       // offline — the next change tries again
  }
  audio.addEventListener('pause', () => { if (!_stationMode && !_extAudio) _scheduleQueueSync(); });
  // Leaving the page: send a pending change now (keepalive outlives the page).
  if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', () => {
      if (!_qsyncTimer) return;
      clearTimeout(_qsyncTimer); _qsyncTimer = null;
      _pushQueueSync(true);
    });
  }
  // On load: offer the server's queue when ANOTHER device saved it after this
  // browser's own save, it differs from the queue here, and nothing has been
  // started since the page loaded.  The offer is a toast plus a "Resume" row in
  // the Queue panel ('queueoffer' {who}); both go once anything starts playing
  // (a track, a station, radio taking over) or the queue is resumed.
  const _qsyncLoadSeq = _playSeq;       // starts so far: none, at load
  let _qsyncStarted = false;            // a station / external audio started on this page
  let _qsyncOffer = null;               // { q, who, el }: the offer still open
  function _clearQueueOffer() {
    if (!_qsyncOffer) return;
    const el = _qsyncOffer.el;
    _qsyncOffer = null;
    try { if (el && Toast.dismiss) Toast.dismiss(el); } catch (_) {}
    emit('queueoffer', { who: null });
  }
  // Called by every start (playTrack, playStation, radio taking the output).
  function _noteStart() {
    _qsyncStarted = true;
    _clearQueueOffer();
  }
  const _qsyncMovedOn = () => _qsyncStarted || _playSeq !== _qsyncLoadSeq;
  function resumeQueueOffer() {
    if (!_qsyncOffer || _qsyncMovedOn()) return false;
    _applyServerQueue(_qsyncOffer.q);
    return true;
  }
  async function _offerServerQueue() {
    if (_qsyncSupported === false || !queueSyncEnabled() || !_qsyncSignedIn()) return;
    let q = null;
    try {
      const r = await fetch(QSYNC_URL, { cache: 'no-store', credentials: 'same-origin' });
      if (r.status === 404 || r.status === 405) { _qsyncSupported = false; emit('queuesync', { supported: false }); return; }
      if (!r.ok) return;
      _qsyncSupported = true;
      emit('queuesync', { supported: true });
      q = await r.json().catch(() => null);
    } catch (_) { return; }
    // Something was started since the page loaded: the listener has moved on.
    if (!q || !Array.isArray(q.ids) || !q.ids.length || _qsyncMovedOn()) return;
    const changed = Number(q.changed);
    if (!(changed > 0)) return;
    let own = 0, savedAt = 0;
    try { own = Number(localStorage.getItem(QSYNC_OWN_KEY)) || 0; } catch (_) {}
    try { savedAt = Number((JSON.parse(localStorage.getItem('sb_queue') || 'null') || {}).savedAt) || 0; } catch (_) {}
    if (Math.abs(changed - own) < 0.001) return;       // this browser saved it
    if (changed * 1000 <= savedAt) return;             // this browser's queue is newer
    // The same song is current here already: nothing worth resuming.
    const mine = _qsyncBody();
    if (mine && mine.ids[mine.current_index] === q.ids[q.current_index]) return;
    let who = (typeof q.changed_by === 'string' && q.changed_by.trim()) ? q.changed_by.trim().slice(0, 60) : 'another device';
    if (who === _qsyncClient()) who = 'another browser';      // same app, same browser kind
    _qsyncOffer = { q, who, el: null };
    const el = Toast.action?.(`Resume the queue from ${who}?`, 'Resume', () => { resumeQueueOffer(); });
    if (_qsyncOffer) _qsyncOffer.el = el || null;
    emit('queueoffer', { who });
  }
  // Replace the queue with the server's: a plain list (no view behind it), the
  // current entry selected and started only by Play — at the saved position.
  // A start while the tracks load (a row double-click) wins: nothing changes.
  async function _applyServerQueue(q) {
    _clearQueueOffer();
    const seq0 = _playSeq;
    const want = q.ids.filter(x => typeof x === 'string').slice(0, 5000);
    const base = [...new Set(want.map(x => x.replace(/~\d+$/, '')))];
    let rows = [];
    try {
      const r = await fetch('/api/tracks/meta/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: base }), credentials: 'same-origin',
      });
      if (r.ok) rows = await r.json();
    } catch (_) { /* reported below */ }
    const byId = new Map((Array.isArray(rows) ? rows : []).filter(t => t && t.id).map(t => [t.id, t]));
    const tracks = [];
    let idx = -1;
    want.forEach((sid, i) => {
      const m = /^(.*)~(\d+)$/.exec(sid);
      const t = byId.get(m ? m[1] : sid);
      if (!t) return;
      if (i === q.current_index) idx = tracks.length;
      const n = m ? Number(m[2]) : 0;
      tracks.push(n > 0 ? { ...t, subsong: n, subsongTotal: t.subsongs } : { ...t });
    });
    if (_playSeq !== seq0 || _qsyncStarted) return;       // the listener started something meanwhile
    if (!tracks.length) { Toast.error('That queue\u2019s tracks are no longer in the library.'); return; }
    const at = idx >= 0 ? idx : 0;
    _clearSource();
    _setShuffleFlag(false);      // the saved list is already in play order
    queue = tracks;
    queueIdx = at;
    _played.add(queue[at]);
    const sec = idx >= 0 ? (Number(q.position) || 0) / 1000 : 0;
    _resumeAt = sec > 1 ? { entry: queue[at], sec } : null;
    _qsyncSent = '';
    emit('queuechange', { queue, queueIdx });
    _emitCue();
    _saveQueueSoon();
    // The shell names its own Play control (the phone's is in the mini player).
    const hint = (typeof window !== 'undefined' && window.__sbPlayHint) || 'Press Play.';
    Toast.ok(`Queue resumed \u2014 ${tracks.length} track${tracks.length === 1 ? '' : 's'}. ${hint}`);
  }
  // The queue's current entry while nothing is loaded yet (a queue restored
  // after a reload, or resumed from another device): what Play starts \u2014
  // playPause's own test.  null while a track is loaded or starting (trackId is
  // set at once), while radio owns the output (its takeover leaves trackId on
  // the old entry), or with no queue.
  function _cuedTrack() {
    if (_extAudio || _stationMode || audio.getAttribute('src')) return null;
    const cur = queue.length ? queue[Math.max(0, queueIdx)] : null;
    if (!cur || typeof cur !== 'object' || trackId === cur.id) return null;
    return cur;
  }
  // \u2026and where Play starts it (a resumed queue's saved position), in seconds.
  function _cuedSec() {
    const c = _cuedTrack();
    return (c && _resumeAt && _resumeAt.entry === c) ? _resumeAt.sec : 0;
  }
  // Tells the player bar / mini player what Play would start ('cue' {track, sec}).
  function _emitCue() {
    const track = _cuedTrack();
    if (track) emit('cue', { track, sec: _cuedSec() });
  }
  // After first paint, off the critical path.
  setTimeout(() => { _offerServerQueue(); }, 2500);

  function _clearSource() {
    _srcGen++;
    _extendP = null;
    _src = null;
    _srcMode = 'ordered';
    _srcTotal = null;
    _srcEnded = false;
    _origQueue = null;
    _origIdx = -1;
    _origIds = null;
    _backP = null;
    _shufExtras = new Set();
    _played = new WeakSet();
  }

  function _setShuffleFlag(v) {
    v = !!v;
    if (shuffle === v) return;
    shuffle = v;
    emit('shufflechange', { shuffle });
  }

  // Upcoming tracks the listener queued by hand — they survive a re-deal.
  function _manualUpcoming() {
    return queue.slice(queueIdx + 1).filter(t => t && _manual.has(t));
  }

  // Re-deal what plays AFTER the current track: everything in the context that
  // has not played yet, in random order.  The current track keeps playing and
  // what already played stays behind it.
  function _shuffleUpcoming() {
    if (!queue.length || queueIdx < 0 || _radioActive) return;
    const cur = queue[queueIdx];
    if (_src && _src.shuffle) {
      // View-backed: upcoming = a fresh seeded permutation of the ENTIRE result
      // set, pulled page by page.  Remember where ordered play had got to so
      // un-shuffle can pick the list back up from there.
      if (_srcMode === 'ordered') _ordResume = _orderedResumePoint();
      if (!_src.ordered && !_origIds) {           // (pending ids = the list is already on file)
        _origQueue = queue.slice();
        _origIdx = queueIdx;
      }
      _srcGen++;
      _extendP = null;
      _srcMode = 'shuffled';
      _seed = _newSeed();
      _shufNext = 0;
      _srcEnded = false;
      queue = queue.slice(0, queueIdx + 1).concat(_manualUpcoming());
      const replay = _shuffleReplaysPlayed();
      _shufExtras = new Set(queue
        .filter((t, i) => t && (i >= queueIdx || (!replay && _played.has(t))))
        .map(t => t.id));
      emit('queuechange', { queue, queueIdx });
      _saveQueueSoon();
      _ensureLookahead();
      return;
    }
    // Plain array: [what already played…, current, …everything else in random
    // order] — a click with shuffle already on has played nothing, so that is
    // simply the current track followed by the whole rest of the list.
    _origQueue = queue.slice();
    _origIdx = queueIdx;
    _origIds = null;
    const keep = !_shuffleReplaysPlayed();
    const isBehind = (t, i) => keep && i !== queueIdx && t && typeof t === 'object' && _played.has(t);
    // Tracks queued by hand to play NEXT keep their place, as in a view-backed deal.
    const mine = new Set(_manualUpcoming().filter(t => !(keep && _played.has(t))));   // (a played one goes behind)
    const behind = queue.filter(isBehind);                       // one pass each — a plain
    const rest = queue.filter((t, i) => i !== queueIdx && !isBehind(t, i) && !mine.has(t));   // queue can be thousands of rows
    queue = [...behind, cur, ...mine, ..._shuffleInPlace(rest)];
    queueIdx = behind.length;
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  // The pre-shuffle list as track objects, from the ids that survived a reload:
  // objects still in the queue first (a playlist may hold one track twice, so
  // they are handed out in order), the rest — rows a view-backed deal replaced —
  // from the server when ``fetchMissing``.  ``done`` runs synchronously when
  // nothing has to be fetched.
  function _rowsForOrigIds(fetchMissing, done) {
    const ids = _origIds || [];
    const local = new Map();
    for (const t of queue) {
      if (!t || !t.id) continue;
      const l = local.get(t.id);
      if (l) l.push(t); else local.set(t.id, [t]);
    }
    const build = (extra) => ids.map((id) => {
      const l = local.get(id);
      if (l && l.length) return l.length > 1 ? l.shift() : l[0];
      return extra.get(id);
    }).filter(Boolean);
    const missing = fetchMissing ? [...new Set(ids.filter(id => !local.has(id)))] : [];
    if (!missing.length) { done(build(new Map())); return; }
    const gen = _srcGen;
    fetch('/api/tracks/meta/batch', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: missing }),
    }).then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then((list) => {
        if (gen !== _srcGen) return;              // the queue moved on meanwhile
        // Full Track records come back; the queue holds list rows — drop the one
        // heavy extra field (the similarity embedding) before they are persisted.
        const extra = new Map((Array.isArray(list) ? list : []).map((t) => {
          const { embedding, ...row } = t;
          return [row.id, row];
        }));
        done(build(extra));
      })
      .catch(() => {});
  }

  // Shuffle switched off: put the upcoming tracks back in list order.
  function _unshuffleUpcoming() {
    if (!queue.length || queueIdx < 0 || _radioActive) return;
    const cur = queue[queueIdx];
    if (_src && _srcMode === 'shuffled' && _src.ordered) {
      // Resume the view's own order from where ordered play left off.
      _srcGen++;
      _extendP = null;
      _srcMode = 'ordered';
      _ordNext = _ordResume;
      _srcEnded = false;
      _origQueue = null;
      queue = queue.slice(0, queueIdx + 1).concat(_manualUpcoming());
      emit('queuechange', { queue, queueIdx });
      _saveQueueSoon();
      _ensureLookahead();
      return;
    }
    if (_src && _srcMode === 'shuffled') {
      // No ordered paging for this view (client-sorted / capped list): its list
      // order is the rows it had loaded.  The dealt tracks came from the server
      // as NEW objects, so match by id — history stays, then whatever the
      // listener queued by hand, then the list from the row after where ordered
      // play left off (minus anything the shuffle already played).
      _srcGen++;
      _extendP = null;
      _srcMode = 'ordered';
      _srcEnded = true;
      const apply = (orig) => {
        const head = queue.slice(0, queueIdx + 1).concat(_manualUpcoming());
        const have = new Set(head.map(t => t && t.id));
        const rest = orig.slice(Math.max(_origIdx, -1) + 1)
          .filter(t => t && !have.has(t.id));
        queue = head.concat(rest);
        _origQueue = null;
        _origIds = null;
        emit('queuechange', { queue, queueIdx });
        _saveQueueSoon();
      };
      if (_origQueue) apply(_origQueue);
      else if (_origIds) _rowsForOrigIds(true, apply);      // reloaded since: rows come back by id
      else { emit('queuechange', { queue, queueIdx }); _saveQueueSoon(); }
      return;
    }
    if (!_origQueue && _origIds) {
      // Reloaded since shuffle went on: the list's ORDER survived as ids, and a
      // plain array still holds every track — put the objects back in that order.
      _rowsForOrigIds(false, (rows) => { _origQueue = rows; });
      _origIds = null;
    }
    if (!_origQueue) return;       // nothing to restore — keep the order as is
    // Tracks are the same object refs, so identity tells us what was removed
    // from / added to the queue while it was shuffled.
    const live = new Set(queue);
    const orig = new Set(_origQueue);
    const restored = _origQueue.filter(t => live.has(t));
    const added = queue.filter(t => !orig.has(t) && t !== cur);
    let at = restored.indexOf(cur);
    if (at < 0) {                  // current track came from the server shuffle
      at = Math.min(Math.max(_origIdx, -1) + 1, restored.length);
      restored.splice(at, 0, cur);
    }
    queue = restored.concat(added);
    queueIdx = at;
    _origQueue = null;
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  // opts.source  — queue-source descriptor: the queue is a window over a larger
  //                server-side list (see "Queue model" above).  ``source.offset``
  //                is the ordered position of ``tracks[0]``.
  // opts.shuffle — true: switch shuffle ON and start on a RANDOM track
  //                ("Shuffle all").  Otherwise the track at ``startIdx`` plays
  //                first and, if shuffle is already on, everything after it is
  //                shuffled.
  function setQueue(tracks, startIdx = 0, opts = {}) {
    // Guard: empty queue must not call playTrack(undefined) — that would
    // dereference `.id` / `.duration` on `undefined` and crash playback for
    // the rest of the session.  Treat empty as a clear: pause the element
    // and emit the queuechange so listeners (queue panel, mini-player) can
    // render the empty state.
    if (!tracks || !tracks.length) {
      _clearSource();
      queue    = [];
      queueIdx = -1;
      try { audio.pause(); } catch (_) {}
      audio.removeAttribute('src');
      emit('queuechange', { queue, queueIdx });
      _saveQueueSoon();
      return;
    }
    // Same array handed back = "jump to this row of the current queue" (queue
    // panel click).  Keep the source / play order intact.
    if (tracks === queue && !opts.source && !opts.shuffle) {
      queueIdx = Math.max(0, Math.min(queue.length - 1, startIdx | 0));
      _playCurrent();
      return;
    }

    _clearSource();
    const src = opts.source && typeof opts.source === 'object' ? opts.source : null;
    _explicitPick = !opts.shuffle && opts.explicit !== false;   // a row the listener chose (not "play / shuffle all")
    if (opts.shuffle && !(src && src.shuffle)) _setShuffleFlag(true);   // playSource sets it once it has a page
    const wantShuffle = (shuffle || !!opts.shuffle) && !_radioActive;

    if (opts.shuffle && src && src.shuffle) {
      // "Shuffle all" on a view-backed list: even the FIRST track must come
      // from the whole result set, so don't start from the rows at hand.
      playSource(src, { shuffle: true });
      return;
    }

    queue    = Array.isArray(tracks) ? tracks.slice() : Array.from(tracks);
    queueIdx = Math.max(0, Math.min(queue.length - 1, startIdx | 0));
    if (opts.shuffle) queueIdx = Math.floor(Math.random() * queue.length);

    if (src) {
      const base = Number(src.offset) || 0;
      _src = { ordered: src.ordered || null, shuffle: src.shuffle || null,
               total: typeof src.total === 'number' ? src.total : null,
               label: src.label || '' };
      _srcMode = 'ordered';
      _srcTotal = _src.total;
      _ordNext = base + queue.length;
      _ordResume = base + queueIdx + 1;
      if (_src.ordered) _notePositions(queue, base);
      _srcEnded = !_src.ordered || (_srcTotal != null && _ordNext >= _srcTotal);
    }

    _startTrack(queue[queueIdx]);
    if (wantShuffle && (queue.length > 1 || _src)) {
      _shuffleUpcoming();                 // emits queuechange + saves + extends
    } else {
      emit('queuechange', { queue, queueIdx });
      _saveQueueSoon();
      _ensureLookahead();
    }
  }

  // Start playing a view-backed list WITHOUT having its rows at hand (a windowed
  // list may have evicted them) — the first page comes straight from the source.
  //   opts.shuffle — switch shuffle ON; the first track is then drawn from the
  //                  whole result set ("Shuffle all").
  //   opts.offset  — start at this ORDERED position; that row plays first and, if
  //                  shuffle is on, everything after it is shuffled.
  //   opts.rows    — the view's loaded rows, for a source with no ``ordered`` spec.
  // Resolves true once playback started, false if it could not start, and null
  // if a newer setQueue / playSource superseded it (callers must not report that
  // as an error).
  async function playSource(source, opts = {}) {
    if (!source || typeof source !== 'object') return false;
    // Supersede any in-flight extension, but keep the live queue's source until
    // the new first page has actually landed (a failed fetch must not strand it).
    const gen = ++_srcGen;
    _extendP = null;
    const pinned = opts.offset != null;
    _explicitPick = pinned && opts.explicit !== false;
    const startOff = pinned ? Math.max(0, Number(opts.offset) || 0) : 0;
    const wantShuffle = (shuffle || !!opts.shuffle) && !_radioActive;
    const mode = (!pinned && wantShuffle && source.shuffle) ? 'shuffled' : 'ordered';
    const desc = { ordered: source.ordered || null, shuffle: source.shuffle || null,
                   total: typeof source.total === 'number' ? source.total : null,
                   label: source.label || '' };
    const seed = _newSeed();
    let page;
    try { page = await _fetchPage(desc, mode, startOff, Q_PAGE, seed); }
    catch (_) { return gen !== _srcGen ? null : false; }   // a superseded request that ALSO failed is still not a failure
    if (gen !== _srcGen) return null;              // superseded by a newer request — not a failure
    if (!page.tracks.length) return false;
    if (opts.shuffle) _setShuffleFlag(true);       // only now: a failed start must not light the button
    // A list with no ordered endpoint gets its list order back (shuffle off)
    // from the rows the view had — which only the caller can supply.
    _origQueue = (mode === 'shuffled' && !desc.ordered && Array.isArray(opts.rows))
      ? opts.rows.slice() : null;
    _origIdx = -1;
    _origIds = null;
    _shufExtras = new Set();
    _src = desc;
    _srcMode = mode;
    _seed = seed;
    _srcTotal = page.total != null ? page.total : desc.total;
    _ordResume = mode === 'ordered'  ? startOff + 1 : 0;
    _ordNext   = mode === 'ordered'  ? startOff + Q_PAGE : 0;
    _shufNext  = mode === 'shuffled' ? Q_PAGE : 0;
    if (mode === 'ordered') _notePositions(page.tracks, startOff);
    _srcEnded = _pageWasLast(page.tracks, _srcTotal, startOff, Q_PAGE, mode);
    queue = page.tracks;
    queueIdx = 0;
    _startTrack(queue[0]);
    if (mode === 'ordered' && wantShuffle) {
      _shuffleUpcoming();                 // chosen row first, the rest shuffled
    } else {
      emit('queuechange', { queue, queueIdx });
      _saveQueueSoon();
      _ensureLookahead();
    }
    return true;
  }

  // "Undo clear": the queue together with its play-order state.  Going back
  // through setQueue() would drop the source and re-deal a shuffled order.
  function snapshotQueue() {
    return {
      queue: queue.slice(), queueIdx, src: _src, mode: _srcMode, seed: _seed,
      ordNext: _ordNext, ordResume: _ordResume,
      shufNext: _shufNext, total: _srcTotal, ended: _srcEnded,
      origQueue: _origQueue ? _origQueue.slice() : null, origIdx: _origIdx,
      origIds: _origIds ? _origIds.slice() : null, extras: [..._shufExtras],
      played: queue.filter(t => t && typeof t === 'object' && _played.has(t)),
    };
  }
  // Puts a snapshotQueue() result back and plays its current track.
  function restoreQueue(snap) {
    if (!snap || !Array.isArray(snap.queue) || !snap.queue.length) return false;
    _clearSource();
    queue = snap.queue.slice();
    queueIdx = Math.max(0, Math.min(queue.length - 1, snap.queueIdx | 0));
    if (snap.src) {
      _src = snap.src;
      _srcMode = snap.mode === 'shuffled' ? 'shuffled' : 'ordered';
      _seed = snap.seed;
      _ordNext = snap.ordNext; _ordResume = snap.ordResume;
      _shufNext = snap.shufNext;
      _srcTotal = snap.total;
      _srcEnded = !!snap.ended;
    }
    _origQueue = snap.origQueue ? snap.origQueue.slice() : null;
    _origIdx = snap.origIdx;
    _origIds = snap.origIds ? snap.origIds.slice() : null;
    _shufExtras = new Set(snap.extras || []);
    (snap.played || []).forEach(t => _played.add(t));
    _playCurrent();
    _reconcileShuffle();          // shuffle may have been toggled while it was empty
    return true;
  }

  // opts.auto — queued by the app itself (Radio Mode's mix), not picked by the
  //             listener: it goes right AFTER the current track, behind earlier
  //             auto tracks, and a later re-deal may replace it.
  function addToQueue(track, opts = {}) {
    if (track && typeof track === 'object') {
      if (opts.auto) _auto.add(track); else _manual.add(track);
      if (track.id) _shufExtras.add(track.id);
    }
    if (opts.auto) {
      // Behind the LAST mix track still ahead (a hand-queued "play next" may sit
      // in between), else right after the current track.
      // Behind everything the listener or the radio already lined up next —
      // never in front of a hand-queued "play next".
      let at = queueIdx + 1;
      while (at < queue.length && (_auto.has(queue[at]) || _manual.has(queue[at]))) at++;
      queue = [...queue.slice(0, at), track, ...queue.slice(at)];
    } else {
      queue = [...queue, track];
    }
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  // The radio mix that is actually UP NEXT: the run of mix / hand-queued rows
  // right after the current track.  A mix row the listener dragged far down the
  // queue must not count, or the radio would think it still has plenty queued.
  function _autoUpcoming() {
    const out = [];
    for (let i = queueIdx + 1; i < queue.length; i++) {
      const t = queue[i];
      if (_auto.has(t)) out.push(t);
      else if (!_manual.has(t)) break;
    }
    return out;
  }

  // A NEW Radio Mode session: drop what an earlier mix left ahead, so the new
  // seed's mix plays next and the refill counter starts from zero.
  function clearAutoUpcoming() {
    const keep = queue.filter((t, i) => i <= queueIdx || !(t && _auto.has(t)));
    if (keep.length === queue.length) return;
    // Like removeFromQueue: a mix row that never played is an ordinary member of
    // the shuffle pass again (unless the same track is still queued elsewhere).
    const kept = new Set(keep.map(t => t && t.id));
    for (const t of queue) {
      if (t && t.id && !kept.has(t.id) && !_played.has(t)) _shufExtras.delete(t.id);
    }
    queue = keep;
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  // Insert right after the currently-playing track (front when nothing plays)
  // so the "Play next" action jumps the queue without disturbing what's on.
  function playNext(track) {
    const at = queueIdx >= 0 ? queueIdx + 1 : 0;
    if (track && typeof track === 'object') {
      _manual.add(track);
      if (track.id) _shufExtras.add(track.id);
    }
    queue = [...queue.slice(0, at), track, ...queue.slice(at)];
    emit('queuechange', { queue, queueIdx });
    _saveQueueSoon();
  }

  function removeFromQueue(idx) {
    if (idx < 0 || idx >= queue.length) return;
    const wasCurrent = (idx === queueIdx);
    const gone = queue[idx];
    queue = queue.filter((_, i) => i !== idx);
    // Taken out before it played: it is an ordinary member of the shuffle pass
    // again (queuing it by hand had marked it "plays outside the order").
    if (gone && gone.id && !wasCurrent && !(typeof gone === 'object' && _played.has(gone))
        && !queue.some(t => t && t.id === gone.id)) {
      _shufExtras.delete(gone.id);
    }
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
      _startTrack(queue[queueIdx]);
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
    _saveQueueSoon();   // persist reorders like every other queue mutation
  }

  // Shuffle is a property of WHAT IS QUEUED NEXT, not of how "next" picks: turning
  // it on re-deals everything after the current track from the whole context
  // (server-side for a view-backed queue), turning it off restores list order.
  // Returns the new state synchronously (callers paint their button from it).
  function toggleShuffle() {
    // Radio Mode plays its own curated order; a lit shuffle button that does
    // nothing (and would throw the mix away when radio stops) is worse than a
    // refusal — callers check ``radioActive`` to tell the listener why.
    if (_radioActive) return shuffle;
    shuffle = !shuffle;
    if (shuffle) _shuffleUpcoming(); else _unshuffleUpcoming();
    emit('shufflechange', { shuffle });
    _saveQueueSoon();
    return shuffle;
  }
  // RadioMode tells the player a curated-radio session is (in)active so queue
  // advance follows the radio's order regardless of the shuffle toggle (and a
  // view-backed queue stops extending — the radio refills the queue itself).
  function setRadioActive(v) {
    const was = _radioActive;
    _radioActive = !!v;
    // Radio over → the toggle takes effect again, whichever way it was left.
    if (was && !_radioActive) _reconcileShuffle();
  }

  // Make the upcoming order agree with the shuffle flag.  The two drift apart
  // while a radio session suspends shuffle (the toggle is refused meanwhile),
  // and a reload in that state restores the mismatch.
  function _reconcileShuffle() {
    if (_radioActive || !queue.length || queueIdx < 0) return;
    const dealt = (!!_src && _srcMode === 'shuffled') || !!_origQueue;
    if (shuffle && !dealt) _shuffleUpcoming();
    else if (!shuffle && dealt) _unshuffleUpcoming();
  }
  // Mobile: suspend/resume the shared element while a dedicated radio element
  // owns audio output.  See ``_extAudio`` for the full rationale.
  function suspendForExternalAudio(v) {
    _extAudio = !!v;
    if (_extAudio) _noteStart();     // the phone's radio took the output
  }

  function toggleRepeat() {
    const modes = ['none', 'all', 'one'];
    repeatMode = modes[(modes.indexOf(repeatMode) + 1) % modes.length];
    return repeatMode;
  }
  // [queue-core:end]

  // A reload / tab close inside the 250 ms debounce (or in a background tab,
  // where timers are throttled) must not lose the latest queue state.
  window.addEventListener('pagehide', () => {
    if (_saveQueueTimer) { clearTimeout(_saveQueueTimer); _saveQueueNow(); }
  });

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
    _maybeGaplessPreload(dur, current);
    _checkPlayRecording();
    _maybePrefetchNext();
  }

  function _maybeCrossfade(dur, current) {
    if (_stationMode) return;   // a live station has no track end to cross-fade into
    // Crossfade: when approaching end of track, trigger crossfade to next
    const xfade = _getCrossfade();
    if (xfade > 0 && !_crossfading && dur > 0 && queue.length > 0) {
      const remaining = dur - current;
      // Only fade out when the next track is actually IN HAND.  At the edge of a
      // pageable window "there is more" still means a fetch, and a fade that
      // finishes before (or without) it would leave the player silent at volume 0;
      // there the track simply plays out and 'ended' advances.
      const nextReady = !!_peekNext() || (repeatMode === 'all' && !_srcPageable());
      if (remaining <= xfade && remaining > 0.2 && nextReady) {
        _crossfading = true;
        const origVol = audio.volume;
        try {
          if (remaining < 0.5) {
            // Too little time left to render a smooth fade — even one
            // setInterval tick would land after the track ends.  Just cut
            // straight to the next track; ear difference vs a 200 ms
            // half-fade is inaudible.
            _autoAdvance();
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
            _autoAdvance();
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
    if (_resumeSeek) {
      const r = _resumeSeek;
      _resumeSeek = null;
      if (r.seq === _playSeq && isFinite(audio.duration) && audio.duration > r.sec + 1) {
        try { audio.currentTime = r.sec; } catch (_) { /* race with track change */ }
      }
    }
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
    // The decoded WAV's true length is now known — tell the library so it can
    // correct an AdLib/IMF "3:00" placeholder row in place (the server also
    // persists it via backfill).  Use raw audio.duration, not _duration()'s
    // metadata fallback — but never a growing render's provisional header
    // (_knownLength).  ``subsong``: the row's length is its default tune's —
    // another tune's must not land on it.
    const known = _knownLength();
    if (trackId && known > 0) {
      emit('durationknown', { id: trackId, seconds: known, subsong: Number(_track && _track.subsong) || 0 });
    }
    const dur     = _duration();
    const current = _currentTime();
    emit('timeupdate', {
      current,
      duration: dur,
      pct: dur ? Math.min(100, (current / dur) * 100) : 0,
    });
  });

  // Record play once the track's been heard substantially: >=30s listened, OR
  // >=50% AND at least 20s actually listened.  The 20s LISTEN floor keeps
  // chiptune SFX/jingle subsongs honest — a 2s tune played to its end clears a
  // bare 50% rule, so "Add all" of a short-tune SID would otherwise log dozens
  // of junk plays/scrobbles — while STILL counting genuinely-short songs the
  // user sat through (a 25s track played fully is >=20s).  (The natural-end path
  // below floors on a >=20s duration for the same reason.)
  let _playRecorded = false;
  function _checkPlayRecording() {
    if (_playRecorded || !trackId) return;
    const dur = _duration();
    const cur = _currentTime();
    if (cur >= 30 || (cur >= 20 && dur > 0 && cur / dur >= 0.5)) {
      _playRecorded = true;
      // ``sendBeacon`` is preferred — the browser queues the POST for
      // OS-level delivery, which survives page-hide / iOS backgrounding
      // (UX-under-load #6).  Fall back to ``fetch`` (with ``keepalive``)
      // when sendBeacon isn't available or rejects the call.
      const _recId = trackId;
      const url = `/api/tracks/${_recId}/played`;
      const ok = !!(navigator.sendBeacon && navigator.sendBeacon(url));
      if (!ok) {
        fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
      }
      // mark_played has no server push, so an already-open "Listening History"
      // / "Most Played" view would stay stale until a manual reload.  Announce
      // the recorded play so app.js can live-refresh it.  (This path is shared
      // by server-stream AND in-browser blob playback, so it fixes both.)
      emit('playrecorded', { trackId: _recId });
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
  // Rendered next tracks are prepared this many seconds into the current one.
  const PREFETCH_RENDER_SETTLE = 3;
  let _prewarmsIssued = false;           // any server prewarm asked for this session

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
    // The queue is in play order even under shuffle, so the lookahead below is
    // always the real upcoming tracks (it used to be skipped for shuffle, which
    // cold-started every shuffled SID / tracker / AdLib render).
    if (!trackId || _prefetchDoneForId === trackId) return;
    const dur = _duration();
    const cur = _currentTime();
    if (!queue.length) return;

    // Bounds: N+1, and N+2 for non-native (Spotify-style 2-ahead).
    const idxs = [];
    // Wrapping to the head is only meaningful for a plain array under
    // repeat-all; a view-backed window's row 0 is NOT what plays after its end.
    const wraps = !_srcPageable() && repeatMode === 'all';
    const n1 = (queueIdx + 1) % queue.length;
    if (n1 !== queueIdx &&
        !(!wraps && n1 === 0 && queueIdx === queue.length - 1)) {
      idxs.push(n1);
    }
    const n2 = (queueIdx + 2) % queue.length;
    if (n2 !== queueIdx && n2 !== n1 &&
        !(!wraps && n2 <= queueIdx)) {
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
          !(!wraps && n3 <= queueIdx)) {
        idxs.push(n3);
      }
    }
    if (idxs.length === 0) return;

    // Pick the lookahead window based on the *immediate* next track —
    // if N+1 is transcoded, we want the wider 30s lead even if N+2 is
    // native, because the work to fire is dominated by N+1.
    const next1 = queue[n1];
    const next1Native = next1 && _isNativeFormat(next1.format);
    const next1Rendered = !!next1 && !next1Native && _isRenderedTrack(next1);
    if (next1Rendered) {
      // A render can take longer than the last 30 s of a track, and a cold
      // SID / Amiga track has no known length yet — so prepare the next one as
      // soon as this one is actually PLAYING (after a short settle, so
      // skipping through the queue doesn't queue renders nobody will hear).
      if (audio.paused || cur < PREFETCH_RENDER_SETTLE) return;
    } else {
      if (!dur) return;
      const window = next1Native ? PREFETCH_NATIVE_WINDOW : PREFETCH_TRANS_WINDOW;
      if (dur - cur > window) return;
    }
    _prefetchDoneForId = trackId;
    // Snapshot the lookahead ids so a later queue reorder can detect
    // whether we still have a warm cache for the right tracks.
    _prefetchedNextIds = idxs.map(i => queue[i] && queue[i].id).filter(Boolean);
    // Drop our own queued prewarms for tracks that are no longer coming up
    // (skipped, dequeued, reshuffled) so the server spends CPU on what plays.
    if (_prewarmsIssued) {
      try {
        fetch(`/api/stream/prewarm/retain?pw=${PREWARM_PAGE_ID}`, {
          method: 'POST', cache: 'no-store', priority: 'low',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: [trackId, ..._prefetchedNextIds] }),
        }).catch(() => {});
      } catch { /* ignore */ }
    }

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
        // Warm the server-side waveform compute for the immediate-next native
        // track so its seek-bar waveform is ready the moment it becomes
        // current, instead of computing on track-change.  The endpoint sends
        // no-store (browser won't cache stale placeholders), so this GET always
        // reaches the server and populates its computed-waveform cache; a blank
        // result (unreadable source) isn't persisted, so an early fire is safe.
        // Native only — a transcoded track's waveform needs its render, which
        // the prewarm above only just kicked off, so warming it here would
        // decode a not-yet-rendered file and throw the blank away.
        try {
          fetch(`/api/tracks/${t.id}/waveform`, { cache: 'no-cache', priority: 'low' })
            .catch(() => {});
        } catch { /* ignore */ }
      } else {
        // Server-side cached transcode/render prewarm.  Renders are heavy:
        // one track ahead (the NEXT one, served first), a second only for a
        // listener who usually plays on (P(continue) > 0.7).
        const renderT = _isRenderedTrack(t);
        if (renderT && idx !== n1 && (idx !== n2 || _pContinue() <= 0.7)) continue;
        _prewarmsIssued = true;
        try {
          fetch(_prewarmUrl(t, idx === n1), {
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
    // Stations show their own status; blocking-renderer tracks show the early
    // "Rendering…" badge — don't stack the buffering badge on top of either.
    if (_stationMode || _suppressBufferingBadge) return;
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
    // Audio is audible now — scope the renderer buffering-badge suppression to
    // the load window only, so a genuine mid-track underrun can still surface
    // the buffering badge.
    _suppressBufferingBadge = false;
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
    // A live station stream "ending" is an upstream drop, not a queue
    // advance — stations.js owns reconnect/downgrade; never auto-next.
    // ``_extAudio``: the mobile radio takeover detached this element; a stray
    // 'ended' must not repeat-one/SID-handoff/next the paused library track
    // under the live stream.
    if (_stationMode || _extAudio) return;
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
      const waitSeq = _playSeq, waitId = trackId;    // a new track or a badge-× cancel ends the wait
      const _waitForFull = () => new Promise((resolve, reject) => {
        const started = Date.now();
        const BUDGET_MS = 5 * 60 * 1000;
        const iv = setInterval(async () => {
          if (_playSeq !== waitSeq || trackId !== waitId) {
            clearInterval(iv);
            reject(new Error('superseded'));
            return;
          }
          if (Date.now() - started > BUDGET_MS) {
            clearInterval(iv);
            reject(new Error('SID render exceeded 5 minute budget'));
            return;
          }
          try {
            const r = await fetch(_renderStatusUrl(waitId));
            const j = await r.json();
            if (j.ready) { clearInterval(iv); _sidFullReady = true; resolve(); }
          } catch (_) {}
        }, 1500);
      });
      try {
        await _waitForFull();
      } catch (err) {
        if (err && err.message === 'superseded') return;
        _hideConvertBadge();
        Toast.error("Full SID render exceeded 5 min — check Settings → Renderers (sidplayfp may be stuck or missing).");
        return;
      }
      if (_playSeq !== waitSeq || trackId !== waitId) return;
      _hideConvertBadge();
      await _switchToFullSid(_sidCachedSec, { play: true });   // the element has ended — play on
      return;
    }
    if (_sidPartial && _sidFullReady) {
      // Full version ready — seamless switch at the boundary
      await _switchToFullSid(_sidCachedSec, { play: true });
      return;
    }

    // Ensure play is recorded on natural end — but floor on a >=20s duration, so
    // a short subsong (chiptune SFX/jingle) that simply plays out isn't logged
    // as a play + scrobbled, while a genuinely-short song still counts.
    if (!_playRecorded && trackId && _duration() >= 20) {
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
    } else if (repeatMode === 'all' || _hasNextTrack()) {
      // _advance(), not next(): this handler already logged the CONTINUE above;
      // next() would log a SKIP on top of it and drag P(continue) down on every
      // track that simply played out.
      _advance();
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
    const params = {};
    if (_forcedIds.has(id)) params.force_transcode = '1';
    // Subsong is 0-based on the wire (matches the server's ?subsong=).  The
    // current track object carries the wire index in ``subsong``; forward it
    // verbatim — but only for the track it belongs to, so a stale ``_track``
    // left by a prefetch can't leak its subsong onto a different id.  0 or
    // absent means the file's default tune.
    const ss = (_track && _track.id === id) ? Number(_track.subsong) : 0;
    if (ss > 0) params.subsong = String(ss);
    return _streamUrl(id, params);
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

    // Radio owns output — the shared element was intentionally detached by the
    // mobile takeover; its abort/decode errors are expected and must NOT
    // trigger a force_transcode reload that would replay the library track
    // under the live stream.
    if (_extAudio) return;

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
    // A rendered track (SID, Amiga, tracker, …) that failed while the server
    // may still be rendering it: wait for the render instead of reporting —
    // and never mark it for force_transcode, which means nothing for a render.
    const rendered = !!(_track && _track.id === id && _isRenderedTrack(_track));
    if (rendered) {
      if (_renderRecoverySeq === _playSeq) return;            // recovery running — it reports
      // When the render has nothing to offer, say WHY (the server's reason — an
      // offline share, a file gone from disk — via the probe in
      // _reportPlayFailure, which speaks within 400 ms), then let
      // _reportMediaError do the rest: its own later toast stays quiet once the
      // probe has reported, and a dealt track still auto-skips.
      const seq = _playSeq, t = _track;
      if (_startRenderRecovery(t, seq, (why) => {
        _reportPlayFailure(t, title, err, seq, why);
        _reportMediaError(tag, title, err);
      })) return;
    }
    const alreadyForced = /[?&]force_transcode=1/.test(audio.src);
    if (err.code === 4 && id && !rendered && !alreadyForced && !_forcedIds.has(id)) {
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
    _reportMediaError(tag, title, err);
  });

  // The <audio> error report: one toast per attempt, then auto-skip a dealt track.
  function _reportMediaError(tag, title, err) {
    console.error(`Media error [${tag}]: ${err && err.message}`);
    // ONE message per failed track: play()'s rejection path explains the failure
    // better (HTTP status, missing file, unreachable share) and lands within
    // ~400 ms — only speak up here if it did not.
    const failedId = trackId, failedSeq = _playSeq;
    const failedFmt = _fmtLabel(_track && _track.format);
    setTimeout(() => {
      const told = _failToastFor === failedId && _failToastSeq === failedSeq;
      if (!told && trackId === failedId && _playSeq === failedSeq) {
        _failToastFor = failedId; _failToastSeq = failedSeq;
        const why = { NETWORK: 'network error', DECODE: 'unreadable file', SRC_NOT_SUPPORTED: 'unsupported or missing file' }[tag] || tag;
        Toast.error(`Couldn\u2019t play \u201c${title}\u201d${failedFmt ? ` \u00b7 ${failedFmt}` : ''} (${why}).` + _nextHint());
      }
    }, 700);
    emit('statechange', { playing: false });
    emit('error', { track: _track, error: err });
    _skipUnplayable();
  }

  // A pick that failed is left alone (no auto-skip) — tell the listener how to move
  // on, but only when there is somewhere to move on TO.
  function _nextHint() { return (_explicitPick && _hasNextTrack()) ? ' Press Next to move on.' : ''; }

  // [queue-core:begin]
  // One unplayable track (an offline share, a broken file) must not end the
  // session — now that shuffle deals from the whole library, hitting one is
  // routine.  Move on after a beat; but a RUN of failures means the source is
  // down, and skipping through thousands of dead tracks helps nobody.
  const MAX_FAILS_IN_A_ROW = 3;
  let _failStreak = 0;
  let _failToastFor = null;      // track id whose failure has already been reported…
  let _failToastSeq = -1;        // …for THIS start (_playSeq): a retry that fails again is reported again
  let _skipTimer = null;
  audio.addEventListener('playing', () => { _failStreak = 0; _failToastFor = null; });
  function _skipUnplayable() {
    const failedId = trackId;
    if (_skipTimer) { clearTimeout(_skipTimer); _skipTimer = null; }
    if (!failedId) return;
    // A track the listener clicked: tell them, and leave it — starting something
    // else half a second after their pick failed is not what they asked for.
    if (_explicitPick) return;
    if (!_hasNextTrack() && repeatMode !== 'all') return;
    if (++_failStreak >= MAX_FAILS_IN_A_ROW) {
      _failStreak = 0;
      Toast.error('Several tracks in a row couldn\u2019t be played \u2014 stopped. Is the music source online?');
      return;
    }
    const startedAt = _playSeq;
    _skipTimer = setTimeout(() => {
      _skipTimer = null;
      // Only if NOTHING was started meanwhile — a retry of the same track counts.
      if (trackId === failedId && _playSeq === startedAt) _advance();
    }, 1200);
  }
  // [queue-core:end]

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
    // This page's ``pw=`` tag for prewarm requests made elsewhere (row hover).
    get prewarmId()      { return PREWARM_PAGE_ID; },
    getAudioContext()    { return ctx; },
    getSourceNode()      { return source; },
    fmt,
    playTrack, playPause, seek, setVolume, next, prev, setQueue, playSource,
    // Diagnostics / tests: the live queue-source state (null for a plain array).
    // Radio Mode: how many of ITS tracks are still queued right after the current one.
    get radioActive() { return _radioActive; },
    clearAutoUpcoming,
    get autoUpcoming() { return _autoUpcoming(); },
    get autoAhead() { return _autoUpcoming().length; },
    get queueSource() {
      return _src ? { desc: _src, mode: _srcMode, seed: _seed, total: _srcTotal,
                      ordNext: _ordNext, shufNext: _shufNext, ended: _srcEnded,
                      hasMore: _srcHasMore() } : null;
    },
    addToQueue, playNext, removeFromQueue, moveInQueue,
    playStation, stopStation, updateStationArt, setStationNowPlaying,
    get stationMode() { return _stationMode; },
    get station()     { return _station; },
    snapshotQueue, restoreQueue,
    // Queue sync across devices (per browser; see _offerServerQueue).
    get queueSync()          { return queueSyncEnabled(); },
    setQueueSync(on)         { setQueueSyncEnabled(!!on); },
    get queueSyncSupported() { return _qsyncSupported; },
    // Who saved the queue on offer ("Resume the queue from …?"), or null.
    get queueOffer()         { return _qsyncOffer ? _qsyncOffer.who : null; },
    resumeQueueOffer,
    // What Play starts while nothing is loaded (a restored / resumed queue), and where.
    get cuedTrack()          { return _cuedTrack(); },
    get cuedSec()            { return _cuedSec(); },
    toggleShuffle, toggleRepeat, setRadioActive, suspendForExternalAudio,
    // Stop the transcode-status poll + WS-fallback watchdog and abort any
    // in-flight transcode-status fetch.  Called by app.js's
    // _cancelAncillaryFetches() on folder navigation to free a browser
    // connection slot.  Never touches the playing audio stream.
    cancelAncillaryFetches() { _stopTranscodePolling(); },
    // WS bridge: app.js's library-WebSocket ``transcode_progress`` handler
    // feeds samples here.  Drives the convert badge AND, on ready, the
    // ``transcode-ready`` waveform-refresh emit; on error, tears the badge
    // down.  Filters internally by the playing trackId so stale per-track
    // messages are ignored.
    onTranscodeProgress(trackId, percent, etaSeconds, ready, error) {
      _onTranscodeProgress(trackId, percent, etaSeconds, ready, error);
    },
    get convertDelay()       { return _getConvertDelay(); },
    setConvertDelay(ms)      { localStorage.setItem(CONVERT_DELAY_KEY, String(ms)); },
    get shuffleReplaysPlayed() { return _shuffleReplaysPlayed(); },
    // Takes effect at the next deal (shuffle switched on, or a new queue).
    setShuffleReplaysPlayed(on) {
      try { localStorage.setItem(SHUFFLE_REPLAY_KEY, on ? '1' : '0'); } catch (_) {}
    },
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
    // Any event name: the list is made on first use.  (A fixed list here once
    // dropped every listener for an event it did not name — 'transcode-ready',
    // 'playrecorded', 'sidwasmvu', 'sidwarm' never reached app.js.)
    on(evt, fn) { if (typeof fn === 'function') (_handlers[evt] ||= []).push(fn); },
  };
})();
