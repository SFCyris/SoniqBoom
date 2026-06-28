// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Service Worker — offline-instant shell.
 *
 * Caches the static UI (index.html / app.js / CSS / mobile shell) so a
 * second visit to a running SoniqBoom paints in ~50 ms instead of the
 * ~600 ms first-visit cost.  Track data + audio streams + admin APIs
 * always pass through to the network — only the immutable shell is
 * served from cache.
 *
 * Cache strategy:
 *   - `/assets/css/...`, `/assets/js/...`, `/assets/icons/...` →
 *       cache-first with stale-while-revalidate background update
 *   - `/`, `/m`, `/multiroom` and their assets → network-first with
 *       cached fallback (so a fresh HTML edit is picked up immediately
 *       online, and the cached copy keeps us usable offline)
 *   - Everything else (`/api/*`, `/rest/*`) → network-only, untouched
 *
 * Cache name is keyed by version so a SW update wipes the old shell
 * cleanly.  Bump SHELL_VERSION when you change which files are in the
 * shell precache list, OR when the cache key derivation changes; you
 * don't need to bump for normal file edits because the cache-busting
 * `?v=N` query strings in app.js's imports already invalidate per-file.
 */

// NOTE: bump SHELL_VERSION whenever the precache list changes, the cache
// strategy below changes, or app.js's `?v=N` cache-buster rolls over.  A
// future improvement is to inject this version string at server-build
// time so it auto-tracks the app.js version — until then, edit by hand.
// v10 (2026-05-23): app.css — service-toggle .slider needs
// pointer-events:none so clicks fall through to the input.  Without
// it, the toggles in the Services panel looked correct but couldn't
// be activated.  Regression from the earlier <label> → <span> wrapper
// change for a11y; the wrapping label's click-anywhere semantics had
// been masking the layering issue.
// v11 (2026-05-23): library.js — All-tracks nav badge now displays
// "5,000+" when the /tracks page cap is hit (and chases the true
// total via /tracks/count for the actual library size).  Without
// this bump the SW serves the old cache-first library.js and the
// "+" suffix never appears in the sidebar.
// v12 (2026-05-23): library.js — fix phantom-autoscroll regression.
// Root-cause candidates addressed in one bump:
//   1) Star-rating .focus() in _setRowRating now passes
//      ``preventScroll: true`` so the browser stops chasing the
//      focused star into view when the row has been re-keyed by a
//      pending virtual-scroll render.
//   2) _fetchVisibleRatings used to call _vsRender(true) on success
//      — a forced full-window rebuild that could shift spacers and
//      retrigger the scroll listener.  Replaced with in-place patches
//      of the col-rating <td> on the rendered row pool.
//   3) groupFilterInput.focus() now ``preventScroll: true`` for the
//      same reason (group views that open below the fold no longer
//      jump the scroll container).
// v13 (2026-05-23): library.js — the v12 changes didn't fully kill the
// "scroll accelerates until it bottoms out" behaviour the user
// described.  Symptom signature (decelerates if you scroll against
// it, reverses, re-accelerates) is the unmistakable shape of a
// scroll feedback loop driven from the scroll listener itself.
// Replaced the inline scroll handler with an rAF-throttled version
// that also short-circuits when scrollTop hasn't actually changed,
// so any layout-shift-induced scroll event in the same task can no
// longer chain into another _vsRender + spacer-resize round.
// v14 (2026-05-23): root-caused the autoscroll.  The user pinned the
// trigger to row 35 — exactly when _vsStart first goes from 0 to 1
// and the top spacer grows by one ROW_H.  Chrome's default scroll
// anchoring (``overflow-anchor: auto``) reads that spacer growth
// as "content above the viewport got taller" and bumps scrollTop by
// the same amount to keep visible content stable.  That scrollTop
// bump fires another scroll event, advances _vsStart again, grows
// the spacer again — a feedback loop drifting scrollTop downward at
// ~ROW_H per frame until the list ends.  Fix: ``overflow-anchor:
// none`` on #track-list-wrap (app.css).  Our virtual-scroll math is
// already self-consistent (pool rows slide with the spacer), so we
// don't need anchoring.  app.css bumped to v60; SW v14 ensures the
// new app.css is fetched.
// v15 (2026-05-23): admin UI gained a "Folder art filenames" text
// input under "Use Folder Art" — drives the server-side priority list
// for the local/remote folder-art fallback (api/art.py).  Default
// (cover.jpg, folder.jpg, ...) preserved when the input is blank.
// Bumps SHELL_VERSION so cache-first admin.js + index.html are
// re-fetched on next visit; without it the new input wouldn't appear
// in the System panel for users who already loaded the previous SW.
// v16 (2026-05-23): player.js — decoupled _hideConvertBadge from
// _stopTranscodePolling.  PERC-9 lets audio.play() resolve in <100 ms
// so the badge-hide path was tearing down the transcode poll before
// the backend ever reported ready=True.  Result: transcode-ready
// never emitted, waveform stayed on the silent-padded initial reading
// until the user navigated away and came back.  Now the badge is
// purely UI (hide on 'playing' or play()-success), while the poll
// runs until backend ready=True, track change, error, or explicit
// cancel — so the in-place waveform refresh actually fires.
// v17 (2026-05-24): app.js — the *transcode-ready → fetchWaveform →
// drawWaveform* chain from v16 was actually firing, but a per-frame
// no-op-skip in _drawWaveform (``if (splitBar === _lastSplitBar)
// return;``) was swallowing the redraw because splitBar at pct=0
// matched the prior frame's cached value.  Result on SACD/DSF:
// fresh _waveformData loaded, paint silently skipped, old silent-
// padded pixels stayed on the canvas until the user switched tracks
// (which reset everything).  Now _fetchWaveform invalidates both
// _cachedBarGeom + _lastSplitBar before painting and uses the live
// seek-bar pct so mid-track refresh draws the correct split.
// v18 (2026-05-24): waveform refresh was still hit-or-miss because
// the browser HTTP cache was reusing the FIRST /waveform response
// (silent-padded reading) for the post-transcode-ready re-fetch
// — eviction is LRU+size-bound so it varied per session, hence the
// "sometimes works" pattern.  Two-sided fix: frontend fetch now
// passes ``cache: 'no-cache'`` AND backend waveform endpoint sets
// ``Cache-Control: no-store`` so neither the disk cache nor the
// memory cache can serve stale bytes.
// v19 (2026-05-24): app.js — _fetchWaveform now clears the canvas +
// the prior track's _waveformData immediately, before awaiting the
// fetch.  Otherwise the previous song's bars stayed painted (via the
// 4Hz timeupdate redraw loop) for the entire fetch+compute round-trip,
// so the user saw the new track inheriting the old waveform briefly.
// Also added an isStillCurrent() guard so a late response from the
// PRIOR track can't overwrite the now-current track's data when the
// user advances mid-fetch.
// v20 (2026-05-24): library.js — All Tracks no longer caps at 5,000.
// For libraries over the cap, the view now uses a Proxy-backed
// WindowedTrackStore: scrollbar reflects the FULL library size
// (e.g. 267 000 rows), CHUNK_SIZE=2000 rows fetch on demand as the
// user scrolls, MAX_CHUNKS=10 caps in-memory to ~20K rows.  Skeleton
// rows render in the meantime; chunk-arrival fires a re-render that
// swaps them for real data.  playFrom builds the queue from a
// loaded-window slice (500 lookahead).  Sort is disabled in this mode
// pending a backend ?sort= param — a Toast explains why.  app.css/js
// versions bumped so the SW serves the new library.js.
// v21 (2026-05-24): sort is now ENABLED in windowed All Tracks.  Backend
// store.py maintains per-column pre-computed sorted indexes (title,
// artist, album_artist, album, format — joining the existing year /
// duration / bpm / added_at).  /tracks?sort=<col>&order=<asc|desc>
// drives the chunked fetcher; a sort click invalidates the windowed
// store, rebuilds it with the new sort params, scrolls to top, and
// re-renders.  library.js bumped to swap in the new sort handler +
// _rebuildWindowedStore helper; the previous "Sort is unavailable"
// Toast is gone for the columns the backend can drive.  Sort keys
// without a backend index (track_number, path) still toast-explain.
// v22 (2026-05-24): trackinfo.js — right-click on a row in the
// windowed All Tracks view used to wrap the WindowedTrackStore Proxy
// in ``[proxy]`` because ``Array.isArray(proxy)`` is false on Proxies
// not backed by an Array; then ``_render(proxy)`` read .title/.artist
// off the proxy target (which has none of those fields) and the info
// panel showed "Track 1 of 1 · —" with every field empty.  Now
// ``open()`` accepts a windowed store directly (``_isWindowedStore``
// flag), preserving prev/next navigation across the full library.
// Also calls ensureRange() + 3s poll for chunk arrival so an LRU-
// evicted chunk re-fetches and the panel re-renders when it lands.
// v23 (2026-05-24): player.js — the play() failure toast used to just
// say ``Couldn't play "X" (NotSupportedError)`` which was useless: the
// browser surfaces "NotSupportedError" for codec mismatches, 502
// responses, 404s, missing files — all of which need different fixes.
// Now on failure the toast still appears immediately but a parallel
// HEAD probe to /api/stream/<id> reports the actual HTTP status with
// a human-readable explanation (e.g. "Source unavailable" for 502,
// "Track or file missing on disk" for 404).  Pairs with the stream.py
// fix that maps FTP 550 / file-not-found errors to 404 instead of 502.
// v24 (2026-05-24): player.js — two follow-ups to the v23 toast work:
//   (a) Stale-track guard.  When the user clicked a new track while the
//       old track's audio.play() was still pending, the old play()
//       would reject later and toast "Couldn't play <old>" even though
//       the new track was buffering happily.  Both the play() catch and
//       the audio.error handler now check whether ``track.id`` matches
//       the current ``trackId`` and silently bail when they don't.
//   (b) Coalesce the diagnostic toasts.  v23 fired two toasts per
//       failure: an immediate generic one + the HEAD-probe-specific
//       one.  Now exactly one toast appears — whichever resolves first
//       wins (HEAD probe vs 400ms fallback timeout).
// v25 (2026-05-24): AirPlay pairing.  /api/cast/play now returns 412
// with ``requires_pairing=true`` when the receiver hasn't been paired
// (Apple TV 4+, HomePod, macOS AirPlay Receiver — all show a PIN by
// default).  cast_picker.js intercepts the 412, opens a styled PIN
// modal, calls the new /api/cast/airplay/pair/{begin,finish} endpoints,
// and retries the cast on success.  Credentials are persisted to
// ``data_dir/airplay_credentials.json`` so the user only pairs once
// per device.  Cancelling the modal is a clean no-op (no error toast).
// v26 (2026-05-25): two follow-ups to the v23 diagnostic-toast work:
//   (a) Filename truncation at ``#``.  ``parse_remote_path`` in
//       core/filesource.py used ``urlsplit`` which treats ``#`` as the
//       fragment delimiter — so a file like
//       ``13. ymniam-orch (sm2_Final#2).flac`` had its tail dropped
//       into ``parts.fragment`` and the FTP fetch got truncated to
//       ``13. ymniam-orch (sm2_Final``.  Now we re-attach
//       ``#fragment`` (and ``?query``) to the path component so files
//       with literal ``#`` / ``?`` in their names round-trip
//       correctly.  Backend-only fix — no SW-cached file changed for
//       it, but bumping anyway because (b) edits player.js.
//   (b) Diagnostic probe HEAD → Range GET.  The stream endpoint is
//       @router.get only, so the HEAD probe returned 405 Method Not
//       Allowed and the toast wrongly reported "Server returned HTTP
//       405".  Switched to a ``Range: bytes=0-0`` GET with an
//       AbortController to cancel as soon as we've read the status
//       code — exercises the same code path the real play does, so
//       the reported HTTP status is what the audio element actually
//       saw (404 / 502 / etc.).
// v27 (2026-05-25): FTP connection pool — lane priority + auto-detect.
//   Backend: _FTPConnectionPool now takes ``borrow(lane='scan'|'stream')``
//   with stream borrows holding queue-jump priority on saturation.
//   FTPFileSource methods route accordingly: read_file→stream, walk/
//   list_dir/stat/is_dir→scan.  Cap detection: 421/530 "too many
//   clients" responses lower the persisted detected_cap (per host:port)
//   to ``observed-1`` and resize the live pool.  Per-share UI in
//   admin.js: scan + stream sliders with live "effective total" preview,
//   plus Test/Reset buttons.  New endpoints: GET /admin/ftp-pool/status,
//   PUT /admin/shares/{id}/ftp-pool, POST /admin/ftp-pool/{probe-cap,
//   reset-cap}.  Admin panel widened 580 → 720px so the slider rows +
//   share-add form fit comfortably.
// v28 (2026-05-25): scan-badge stuck-at-99% fix.  Three backend gaps
// in scanner.py allowed ``_progress.processed`` to advance past the
// last broadcast value: (a) local-scan worker-error path didn't fire
// the % PROGRESS_EVERY / == total broadcast check; (b) remote-scan
// download-error path did the same; (c) remote scan never emitted a
// closing ``running:false`` broadcast (the local path did).  Fixed:
// per-error broadcasts wherever ``processed`` is incremented, plus a
// final on_progress() with running:false at the remote scan's tail.
// app.js gets a defensive watchdog: when a scan_progress message
// arrives with processed >= total but running=true, poll
// /api/admin/scan/status every 3s and finalise the badge as soon as
// the backend reports the scan is done — so even a WS disconnect can't
// strand the badge on screen forever.
// v29 (2026-05-25): FTP pool UI — one card per server, not per share.
// The pool registry is keyed by (host, port, user, pass, encoding), so
// multiple shares on the same NAS share ONE pool.  The v27 UI rendered
// N cards per server (one per share), letting three "Save" buttons
// fight over the same pool's settings.  Canonical storage moved from
// per-share ``network_shares.<id>.ftp_pool`` to top-level
// ``conf.ftp_pools["host:port"]``.  New PUT /admin/ftp-pool endpoint
// takes {host, port, scan, stream}; legacy per-share PUT delegates to
// it.  GET /admin/ftp-pool/status returns ``{servers: [...]}`` deduped
// by host:port, with each entry listing the shares using it.  Legacy
// share-keyed config is auto-migrated on save (cleared from
// network_shares.<id>.ftp_pool when the canonical key is written).
// v30 (2026-05-25): two scan-related fixes:
//   (a) Per-task snap-to-total in scanner._run_remote_scan was firing
//       for EVERY scan that finished, not just the last one.  In a
//       parallel reindex of 4 remote dirs, the first task to finish
//       snapped processed → aggregate total, then tasks B/C/D's
//       continued increments pushed processed past total — the
//       "100% — 137,034 / 76,952" overshoot the user reported.  Now
//       the snap only runs when ``_scan_count == 0`` (this is the
//       last scan finishing).
//   (b) ``_scan_dirs_split`` silently skipped FTP shares whose source
//       wasn't registered + reconnect failed; the API returned
//       ``{started: true}`` even when NOTHING was started, so a
//       per-row Re-Index appeared to "jump to Done" because the
//       previous scan's "Done" badge stayed.  Now returns
//       ``{started, scanned, skipped: [{path, reason}]}`` and the
//       UI surfaces the skip reason as an error toast.
// v31 (2026-05-25): scanner pause / resume.  New
// pause_scan()/resume_scan() in core/scanner.py gate the per-file
// loops via an asyncio.Event; in-flight downloads complete normally
// but no new ones are submitted while paused.  New endpoints:
// POST /admin/scan/pause, POST /admin/scan/resume.  scan_progress
// payload now includes ``paused: bool``.  Admin UI adds a Pause /
// Resume toggle button next to the Scan Progress header that flips
// to accent colour when paused.
//
// v32: metadata repair endpoint — admin.js gains "Repair Garbled
// Metadata" controls, app.js dispatches the new repair_progress WS
// event, index.html grows the new section.  Bump forces clients to
// pick up all three plus the new /admin/metadata/* routes.
//
// v33: repair task now handles zip-virtual paths (``a.zip::b.it``)
// via the scanner's _extract_from_zip — fixes the 869-of-871 error
// count from the first repair run on the user's library.  Backend
// also categorises failures (``zip-missing``, ``local-missing``,
// ``remote-no-source``, …) and admin.js renders the breakdown.
//
// v34: scan completion toast now surfaces the scan plan ("16027
// unchanged, 1928 cleaned up") instead of bare "Scan complete" —
// fixes the user perception that an instant-complete scan was a
// no-op, when it actually skipped 16K files and cleaned ghosts.
// pollScanDone callbacks now receive the final status object.
//
// v35: Rebuild Index now shows LIVE progress on its button label
// ("Rebuilding 23%…") and message line ("Scanning 23% — 60992 /
// 86915 files (5 shares in parallel)") instead of a static
// "Rebuilding schema and scanning all folders…" that froze for
// the duration.  Also scrolls the Scan Progress section into view
// on click so the user sees the detailed per-file feedback.
//
// v36: FLAC partial budget 384 KB → 1.5 MB (88% of Hi-Res FLACs
// were falling back to full fetch — 130× more bytes per file —
// because embedded cover art overflowed the budget).  MP3/Ogg/Opus
// budgets also bumped to 512 KB.  Backend change; no JS changes
// for v36 but bumping the SW so clients pull together with the
// next round of admin.js polish.
//
// v37: two perf knobs.  (a) Scanner now uses a GROWING budget
// (1×, 4×, full) instead of a single shot — saves a full-fetch
// retry when 1× under-shoots but 4× covers the cover art.
// (b) FTP pool can auto-grow connections when a per-server
// "Auto-grow scan workers" toggle is enabled (default OFF).
// v87: perf pass — lazy admin/visualizer/trackinfo modules, album-grid art
// via aggregation track_id, queue spot-updates, group-filter debounce.
// v88: Stations (internet radio, Beta) — sidebar section, stations.js,
// relay-based playback with ICY now-playing.
// v89: Stations QA fixes — SSRF guard on relay/favorites, moveInQueue
// persistence, empty-streams retire.
// v90: utils.js — Toast.ok() success variant (green); fixes silent tag-save
// confirmation + a hard TypeError on the password-update toast.
// v91: Stations — station search (header + global preview), station-info
// modal on the (i) button, and /m no longer precached/pinned (fixes desktop
// being bounced to the mobile shell).
// v92: stations.js — scheme-whitelist favicon/homepage URLs (block
// javascript:/data: from a stranger-editable directory).
// v93: Stations out of Beta; unavailable stations are no longer blacklisted
// (temporary-outage handling).
// v94: folder browsing — local children served store-first (no per-click
// os.scandir on SMB mounts); sidebar tree no longer collapses to root on
// background scan completions (preserves expansion).
// v95: QA follow-ups — astral-plane folder-name infinite-loop fix in the
// children bisect; tree-refresh re-entrancy guard.
// v96: non-recursive folder listing served store-first + windowed (a leaf
// folder with thousands of direct tracks, e.g. modarchive/E, no longer pays
// a live SMB walk + unwindowed payload).
// v97: radio underrun — downgrade quality or pause-and-rebuffer instead of
// the browser skipping to the live edge; never advance the queue under a
// station; reconnect on relay EOF.
// v98: station-switch crash fix — free the previous live-stream decoder before
// attaching the next (renderer OOM / Edge "Error code 5"); ignore
// MEDIA_ERR_ABORTED so a src swap can't self-trigger a downgrade storm;
// backoff + single-flight + sustained-play budget on reconnect.
// v99: Instant-Mix radio advances in its curated order regardless of the
// shuffle toggle (next no longer random-jumps to an unrelated artist/station).
// v100: stations no longer downgrade during startup buffering — a 'waiting'
// before the first 'playing' is startup connect, not an underrun (9s grace).
// Fixes eager quality-drop at start AND the switch-storm renderer crash (the
// cascade was opening 4 connections per station start).
// v101: app.css v63 — #stations-view sits above the visualizer canvas
// (position:relative; z-index:1) so screen-filling visualizers no longer
// wash out the station text (it was painting under the z-index:0 canvas).
// v102: player.js — documented why the AudioContext sample rate is left to
// the browser (no DSD teardown/rebuild); admin.js — FTP probe-cap button now
// posts {host,port} instead of {share_id}.  Bump so the cache-first shell
// serves the edited player.js / admin.js to returning users.
// v103 (2026-06-23): art pipeline — library.js _fillTrackRow cache-busts a
// just-filled cover on row re-entry; app.js art_ready/reconnect re-bust so the
// list recovers backfilled art without a reload.
// v105 (2026-06-24): radio mode — player bar shows now-playing Song/Artist for
// stations (player.js setStationNowPlaying, stations.js), and a live stream now
// swaps the seek row for a LIVE badge + ticker with ◄◄/►► surfing the station
// list (app.js radio-mode toggle + transport branch, app.css .radio-mode).
// Bump forces fresh player.js (precached) + stations.js (lazy) for returning users.
// v106 (2026-06-24): radio mode follow-ups — centre the LIVE/ticker group under
// the transport (app.css), and route stations through the Web Audio graph so the
// EQ/ReplayGain/VU apply to radio (player.js playStation now calls
// _initAudioContext, which it previously skipped — a radio-only session bypassed EQ).
// v107 (2026-06-24): radio surf-target labels are now a fixed equal width
// (app.css .radio-target flex:0 0 120px) so unequal station names keep the
// play button + ticker centred (verified: play/ticker centres coincide).
// v108 (2026-06-24): playlist panel covers — on art 404 the row <img> is now
// KEPT (opacity:0) instead of removed, so the remote-art backfill's art_ready
// → _bustArtImg can recover it (playlist.js).  Previously remote covers only
// appeared after the track was played; now they fill in like the library list.
// v109 (2026-06-24): FIX covers never loading in track lists / playlist / queue.
// Root cause: the row <img> had loading="lazy" but its src was set by JS AFTER
// the row was built inside a scroll subtree, so the browser deferred the load
// past the visible window and never re-fired it — the cover URL was never
// requested (server access log showed 0 hits for those rows).  The album grid
// avoided this with a detached Image().  Removed loading="lazy" from the
// row-cover-img (library.js) and qr-art-img (playlist.js, queue.js) so the
// eagerly-set src fetches immediately; virtual scroll still bounds the table.
// v110 (2026-06-24): SW updates no longer auto-skipWaiting — app.js shows a
// "new version — refresh" prompt and posts SKIP_WAITING on accept, so a code
// change applies on ONE reload (the prompt) instead of needing a second one.
// v111 (2026-06-24): cross-browser radio fixes — (a) player-right now mirrors
// player-art-info's width so the centre column (and the radio transport pill)
// is symmetric/centred in Gecko/WebKit, not just Chromium (app.css); (b) the
// player-bar cover uses a render-generation token instead of the station's
// empty id and no longer blocks the paint on img.decode(), so a slow/rejecting
// cross-origin station logo can't strand the cover on the placeholder (app.js).
// v114 (2026-06-25): perceived-perf polish + onboarding — search debounce
// 150→100 ms and an auto-highlighted top preview row (search.js); art-
// placeholder emoji memoized per track (utils.js); skeleton shimmer cells get
// will-change (app.css); an early "Rendering…" badge for blocking SID/MIDI/
// tracker/AdLib/GME renders + redundant buffering-badge suppression (player.js);
// first-run welcome overlay + header "?" re-open + empty-state CTA + sidebar
// discoverability tooltips (index.html, app.css, app.js).  Bump re-fetches the
// precached shell (app.css/player.js/utils.js) and the versioned app.js.
// v115 (2026-06-25): adversarial-review fixes on v114 — removed the
// will-change anti-pattern from skeleton cells (app.css); buffering badge no
// longer stacks on "Rendering…" (player.js 'waiting' gate); consolidated web
// prewarm + AdLib-probe into one shared background-render gate (stream.py);
// search anchor uses a distinct passive class + preserves keyboard selection
// across Stations re-render (search.js/app.css); welcome dialog focuses the
// panel (not the destructive CTA), marks #app inert, adds aria-describedby +
// aria-expanded + a shortcuts link (app.js/index.html); empty-state "add a
// folder" CTA gated to a genuinely-empty library (library.js/index.html);
// Rendering/buffering badges added to the mobile shell (mobile.html/mobile.css).
// v116 (2026-06-25): fix-review follow-ups — empty-state CTA uses event
// delegation so it survives innerHTML rebuilds (app.js); search selection
// restore matches the track row's data-idx (not raw index) so a Stations
// injection can't shift it onto a station row, and ArrowUp past the top
// re-anchors (search.js); renderer buffering-badge suppression now ends once
// audio is audible (player.js); #btn-welcome ships aria-expanded="false"
// (index.html).
// v117 (2026-06-26): review fixes — defined the missing ``.sr-only`` clip so
// the skeleton's "Loading tracks…" label stops painting on screen (app.css);
// the Cast picker now marks the real ``#content`` / ``#m-content`` inert (it
// targeted a non-existent ``#main-content``, so its aria-modal isolation never
// applied — cast_picker.js).  Backend (not SW-cached): /smart/duplicates now
// gates the 270k-track rebuild behind the mutation-seq cache and runs the cold
// build off the event loop (smart.py).
// v118 (2026-06-26): app.css — the Library Galaxy view (#galaxy-view) was missing
// from the z-index lift list, so a playing music visualizer (z-index:0, opacity
// 0.9) painted over the constellation and washed it out.  Lifted #galaxy-view to
// z-index:1 like the other content views; its opaque scene now fully covers the
// ambient visualizer.  Bump re-fetches the precached app.css.
// v119 (2026-06-27): utils.js — the background duration probe (probeAdlibDurations)
// now covers GME chiptunes (NSF/SPC/GBS/…), not just AdLib, so their real lengths
// replace the sid_default_duration placeholder ("5:00") in the list/modal without
// playing.  Backend persists the rendered length on play + via /probe-durations.
// v120 (2026-06-27): library.js — patchTrackDuration was scoped to AdLib only and
// gated on the 180s placeholder, so it silently DROPPED the backend's GME (NSF/…)
// duration corrections (their placeholder is sid_default_duration, e.g. 300, not
// 180) — the list/now-playing row stayed at "5:00" even though the store had the
// real length.  Widened to render-only formats (CHIP_FORMAT_NAMES), removed the
// 180-only gate.
// v121 (2026-06-27): player.js — the play-failure probe aborted the response body
// and only ever showed "Server returned HTTP <code>".  It now reads the error
// body's human-readable ``detail`` (backend returns specific reasons, e.g. "This
// file is empty or corrupt", "needs a companion instrument bank", "couldn't be
// decoded") and surfaces THAT in the toast.  Bump re-fetches the precached player.js.
// v122 (2026-06-27): app.js — two visualizer fixes for radio.  (1) The global
// keydown guard returned for ANY key when a role=button/tab/radio was focused,
// so "v" (cycle visualizer) did nothing right after clicking a station to play
// it (the focused row swallowed the key) — now it only defers Space/Enter/arrow
// keys to the widget, letter shortcuts pass through.  (2) The visualizer's
// auto-start hooks live in the lazy module; a radio-only session never loaded it
// (no library track play, deferred warm may not have run), so radio didn't
// render the canvas until a song was played first.  Added a statechange listener
// that load+starts the visualizer on the first play of any kind.
// v123 (2026-06-27): utils.js + library.js — AHX/HVL (UADE) duration display.
// .ahx/.hvl are render-only (uade123/hvl2wav) so the scan stores duration 0 and
// the list/modal showed "—" even though playback knew the length.  The duration
// probe + live row-patch were gated on CHIP_FORMAT_NAMES (AdLib+GME only); added
// RENDER_DURATION_FORMAT_NAMES = CHIP ∪ {AHX, HivelyTracker} and switched both
// gates to it.  (Backend also now backfills the UADE/HVL branches and parses
// WAVE_FORMAT_EXTENSIBLE, which stdlib wave.open rejected — server restart.)
const SHELL_VERSION = 'v123';
const SHELL_CACHE = `soniqboom-shell-${SHELL_VERSION}`;
// Downloaded-for-offline audio lives in a STABLE (un-versioned) cache so it
// survives shell upgrades — the activate cleanup only reaps `soniqboom-shell-*`.
const OFFLINE_AUDIO_CACHE = 'soniqboom-offline-audio';

// Precache: entry-point HTML + the four critical-path JS modules and the
// main CSS.  This way a cold first visit warms the cache for the next
// reload (offline-instant on visit #2).  We intentionally omit the
// `?v=N` cache-buster query strings — the runtime handler below will
// re-fetch the busted URLs on demand once index.html lands; the bare
// paths here just seed Module-Cache priming.
const SHELL_PRECACHE = [
  '/',
  // NOTE: ``/m`` is deliberately NOT precached.  Fetching it during SW
  // install would pin desktop browsers to the mobile shell (the route used
  // to set a sticky sb_ui=mobile cookie; even without that, precaching the
  // mobile shell on a desktop install is wasted work).  The mobile shell is
  // cached on first real visit by the network-first navigation handler.
  '/assets/css/app.css',
  '/assets/js/player.js',
  '/assets/js/library.js',
  '/assets/js/utils.js',
  '/assets/js/queue.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // Best-effort precache — a missing entry shouldn't abort install.
    await Promise.allSettled(
      SHELL_PRECACHE.map(url => cache.add(new Request(url, { cache: 'reload' }))),
    );
    // NOTE: we deliberately do NOT self.skipWaiting() here.  An UPDATE (a new
    // SW found while an old one still controls open tabs) now goes to the
    // "waiting" state, and app.js surfaces a "new version — refresh" prompt;
    // the new SW activates only once the user accepts (via the SKIP_WAITING
    // message below).  The first-ever install still activates immediately
    // (nothing is controlling the clients yet), so cold visits are unaffected.
    // This makes an update apply on ONE reload (the prompt's Refresh) instead
    // of the old skipWaiting flow that needed a second manual reload.
  })());
});

// app.js posts this when the user clicks "Refresh" on the update prompt.
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // Reap older shell caches — SHELL_VERSION bump wipes them clean.
    const names = await caches.keys();
    await Promise.all(
      names
        .filter(n => n.startsWith('soniqboom-shell-') && n !== SHELL_CACHE)
        .map(n => caches.delete(n)),
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  // Only handle same-origin requests — third-party resources (CDN
  // fonts etc.) pass through untouched.
  if (url.origin !== self.location.origin) return;

  const path = url.pathname;

  // Offline audio: serve a downloaded track from the offline cache (works both
  // online and off). Only the exact /api/stream/<id> audio URL is considered —
  // status/cancel sub-endpoints fall through. A cache MISS goes straight to the
  // network, so tracks that aren't downloaded stream exactly as before.
  if (/^\/api\/stream\/[^/]+$/.test(path)) {
    e.respondWith(
      caches.open(OFFLINE_AUDIO_CACHE)
        .then(c => c.match(req, { ignoreSearch: true }))
        .then(hit => hit || fetch(req))
        .catch(() => fetch(req)),
    );
    return;
  }

  // Never cache data / streams / live admin endpoints.
  if (path.startsWith('/api/') ||
      path.startsWith('/rest/') ||
      path.startsWith('/admin/')) {
    return;
  }

  // Static asset path: cache-first with background revalidate.
  if (path.startsWith('/assets/')) {
    e.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const hit = await cache.match(req);
      // Background refresh — don't block the response on it.
      const fetchAndUpdate = fetch(req).then(res => {
        if (res && res.status === 200) cache.put(req, res.clone());
        return res;
      }).catch(() => null);
      return hit || fetchAndUpdate || fetch(req);
    })());
    return;
  }

  // Shell HTML routes: network-first with cached fallback (so a fresh
  // deploy is picked up online, and an offline reload still works).
  if (path === '/' || path === '/m' || path.startsWith('/m/') ||
      path === '/multiroom' || path.startsWith('/multiroom/')) {
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        if (res && res.status === 200) {
          const cache = await caches.open(SHELL_CACHE);
          cache.put(req, res.clone());
        }
        return res;
      } catch {
        const cache = await caches.open(SHELL_CACHE);
        const hit = await cache.match(req) || await cache.match('/');
        if (hit) return hit;
        return new Response('Offline — SoniqBoom is unreachable.', {
          status: 503, headers: { 'Content-Type': 'text/plain' },
        });
      }
    })());
    return;
  }

  // Everything else: pass through unchanged.
});
