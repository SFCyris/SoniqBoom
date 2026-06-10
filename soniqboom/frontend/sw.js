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
const SHELL_VERSION = 'v73';
const SHELL_CACHE = `soniqboom-shell-${SHELL_VERSION}`;

// Precache: entry-point HTML + the four critical-path JS modules and the
// main CSS.  This way a cold first visit warms the cache for the next
// reload (offline-instant on visit #2).  We intentionally omit the
// `?v=N` cache-buster query strings — the runtime handler below will
// re-fetch the busted URLs on demand once index.html lands; the bare
// paths here just seed Module-Cache priming.
const SHELL_PRECACHE = [
  '/',
  '/m',
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
    self.skipWaiting();
  })());
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
