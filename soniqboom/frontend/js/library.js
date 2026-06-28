// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * library.js — Library browser: artists / albums / genres / years / all tracks.
 * Exports: Library singleton
 */
import { Player } from './player.js';
import { artPlaceholderEmoji, ADLIB_FORMAT_NAMES, CHIP_FORMAT_NAMES, RENDER_DURATION_FORMAT_NAMES, probeAdlibDurations } from './utils.js';

const API = (path, q = {}) => {
  const qs = new URLSearchParams(q).toString();
  return fetch(`/api${path}${qs ? '?' + qs : ''}`).then(r => r.json());
};

// ── DOM refs ──────────────────────────────────────────────────────────────────
const tbody            = document.getElementById('track-tbody');
const emptyEl          = document.getElementById('track-empty');
// Capture the default empty-state markup at module load.  Per-call
// customisations (``_renderBranchEmpty`` for branch-folder clicks,
// ``showDuplicates`` for the dedicated dupe view) overwrite
// ``emptyEl.innerHTML``; ``renderTracks`` restores this snapshot
// whenever it gets a fresh, non-empty result set so the customised
// markup doesn't leak into unrelated views.
const _EMPTY_DEFAULT_HTML = emptyEl.innerHTML;
// Set by showAll() when the WHOLE library is empty (total === 0); consumed once
// by renderTracks() to decide whether the empty state shows the "add a folder"
// CTA (genuinely-empty library) vs neutral "no matches" copy (a filter/search
// that merely returned nothing).  Reset to false after each render.
let _showAddFolderCta = false;

// ── Background freshness-refresh per folder navigation ────────────────
//
// Schedule POST /api/fstree/refresh for ``path`` so the backend walks
// that subtree and indexes newly-added files in parallel with the
// user's browsing.  We debounce per-path: rapid nav (prev/next, quick
// drill-in-then-out) batches into the most recent path only.  Each
// in-flight request is also tracked so we never have two outstanding
// refreshes for the same path; the scanner's own queue dedups
// concurrent requests across paths.
const _bgRefreshState = {
  pendingTimer: null,
  pendingPath: null,
  recentlyScanned: new Map(), // path → timestamp; suppresses duplicates within window
};
const _BG_REFRESH_DEBOUNCE_MS = 400;
const _BG_REFRESH_TTL_MS      = 30_000;
// Don't auto-refresh huge archive subtrees (e.g. modarchive's ~100K ZIPs) on
// drill-down — re-walking them over SMB is expensive and they're static.  The
// folders the user actually edits (albums) are far below this.  Re-index from
// Settings still covers everything.
const FRESHNESS_MAX_TRACKS    = 5000;

// RE-ENABLED: POST /api/fstree/refresh now runs the dedicated
// refresh_subtree_under_root() pass — it scans the clicked folder under its
// EXISTING scan root (no upsert_scan_dir, no re-rooting), upserts new/changed
// files, prunes vanished ones (capped, never on a missing mount), and emits a
// silent scan_progress completion so the open folder refreshes in place.
const FRESHNESS_DRILLDOWN_ENABLED = true;

function _scheduleBackgroundRefresh(path) {
  if (!FRESHNESS_DRILLDOWN_ENABLED) return;
  if (!path) return;
  // Local-paths only (remote shares are auto-polled by remote_freshness).
  if (/^(smb|ftp|webdav|webdavs|https?):\/\//.test(path)) return;
  // Skip if we just refreshed this path — keeps the load light while
  // the user clicks back-and-forth between siblings.
  const now = performance.now();
  const last = _bgRefreshState.recentlyScanned.get(path);
  if (last && (now - last) < _BG_REFRESH_TTL_MS) return;

  _bgRefreshState.pendingPath = path;
  if (_bgRefreshState.pendingTimer) clearTimeout(_bgRefreshState.pendingTimer);
  _bgRefreshState.pendingTimer = setTimeout(() => {
    _bgRefreshState.pendingTimer = null;
    const target = _bgRefreshState.pendingPath;
    _bgRefreshState.pendingPath = null;
    if (!target) return;
    _bgRefreshState.recentlyScanned.set(target, performance.now());
    fetch('/api/fstree/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: target }),
    }).catch(() => { /* fire-and-forget — silent on error */ });
  }, _BG_REFRESH_DEBOUNCE_MS);
}

// ── Current browse-folder state (drives in-place freshness refresh) ───────────
// When the user is viewing a folder we remember which one (+ whether it's the
// recursive/windowed view).  A scan that finishes touching this folder can then
// refresh it IN PLACE — preserving scroll — instead of the old behaviour of
// resetting the whole tree to root on every scan completion.
let _currentBrowsePath = null;
let _currentBrowseRecursive = false;

function isInFolderView() { return _currentBrowsePath != null; }

// True if any of the just-scanned resolved dirs overlaps the open folder
// (scan-root is a parent of the folder → re-index case; or the folder is a
// parent of a scanned subfolder → drill-down case; or they're equal).
function currentFolderAffectedBy(dirs) {
  if (!_currentBrowsePath || !Array.isArray(dirs)) return false;
  const cur = _currentBrowsePath.replace(/\/+$/, '');
  for (const d of dirs) {
    if (!d) continue;
    const dd = String(d).replace(/\/+$/, '');
    if (cur === dd || cur.startsWith(dd + '/') || dd.startsWith(cur + '/')) return true;
  }
  return false;
}

// Cheap "did the folder change?" check for the in-place refresh: same length
// and same track ids in the same order ⇒ nothing to re-render.
function _sameTrackList(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if ((a[i] && a[i].id) !== (b[i] && b[i].id)) return false;
  }
  return true;
}

async function refreshCurrentFolderInPlace() {
  if (!_currentBrowsePath) return;
  const wrap = document.getElementById('track-list-wrap');
  const scrollY = wrap ? wrap.scrollTop : 0;
  const path = _currentBrowsePath, rec = _currentBrowseRecursive;
  try {
    await showFolder(path, rec, { quiet: true });   // no skeleton flash, no re-schedule
  } catch (_) { return; }
  // Restore scroll after the new rows paint (best-effort; the windowed
  // recursive view manages its own scroll and may land at top — acceptable).
  if (wrap) requestAnimationFrame(() => { try { wrap.scrollTop = scrollY; } catch (_) {} });
}

const loadingEl        = document.getElementById('track-loading');
const browseHdr        = document.getElementById('browse-header');
const browseCrumb      = document.getElementById('browse-crumb');
const browseBack       = document.getElementById('browse-back');
const simPanel         = document.getElementById('similar-panel');
const simList          = document.getElementById('similar-list');
const simBtn           = document.getElementById('btn-similar');
const browseFilterWrap  = document.getElementById('browse-filter-wrap');
const browseFilter      = document.getElementById('browse-filter');
const groupFilterBar    = document.getElementById('group-filter-bar');
const groupFilterLabel  = document.getElementById('group-filter-label');
const groupFilterInput  = document.getElementById('group-filter-input');

// ── State ─────────────────────────────────────────────────────────────────────
let currentTracks = [];
let sortKey = null;
let sortAsc = true;
// Extra query params for the windowed track view (the chunked /tracks fetcher
// merges these in).  null = the plain All-Tracks view; {format:'MIDI'} = the
// Galaxy per-format browse.  Preserved across sort-clicks so re-sorting a
// format view keeps filtering by that format.
let _windowedFilter = null;
let activeRow = null;

// ── Windowed track store (large-library viewing) ──────────────────────────────
//
// For libraries above the /tracks page cap (5000) we replace the plain
// Array assigned to ``currentTracks`` with a Proxy that LOOKS like an
// array (numeric indexing + ``.length``) but only keeps a sliding window
// of chunks loaded.  The scrollbar reflects the FULL library size; rows
// the user scrolls into are fetched in the background and rendered when
// they land.  This is the Apple Photos / Spotify model — one logical
// list, bounded memory footprint, no pagination chrome.
//
// Why a Proxy and not a class with explicit ``get(i)`` calls: every
// existing call site uses ``currentTracks[i]`` and ``currentTracks.length``.
// Replacing 27 call sites would be invasive and bug-prone; intercepting
// the access at the variable boundary keeps the refactor localised here.
//
// Tunables:
//   * CHUNK_SIZE = 2000 — one round-trip per ~75 rows × 27 visible per
//     viewport ≈ ~4 viewports of headroom before the next fetch fires.
//     Small enough that re-sort or jump-to-unfetched-region pays only
//     2000 rows of latency, not the full 5000.
//   * MAX_CHUNKS = 10 — keeps ~20 000 tracks (≈ 10 MB at our row size)
//     in memory at any time, evicting least-recently-touched chunks.
const CHUNK_SIZE = 2000;
const MAX_CHUNKS = 10;

function createWindowedStore(total, fetcher, opts = {}) {
  const chunks  = new Map();   // chunkIdx → Track[]
  const pending = new Map();   // chunkIdx → Promise (dedup)
  const lru     = [];          // chunkIdx access order, oldest first
  let onChunkLoad = null;      // called with chunkIdx when a fetch lands
  // Sort metadata is carried on the store so callers (e.g. column-header
  // click) can read the current key+direction back without re-deriving it
  // from the persisted localStorage state.  Mutation of these fields
  // requires ``invalidate()`` from the caller — the store does not auto-
  // refetch on its own (showAll owns the lifecycle).
  const target = {
    _isWindowedStore: true,
    _total: total,
    _chunks: chunks,
    _pending: pending,
    _sortBy: opts.sortBy || null,
    _sortOrder: opts.sortOrder || null,
    setOnChunkLoad(cb) { onChunkLoad = cb; },
    // Fetch every chunk that overlaps [start, end).  Callers fire this
    // from the scroll handler so the data shows up before the user
    // gets to it.
    ensureRange(start, end) {
      if (end <= start) return;
      const first = Math.max(0, Math.floor(start / CHUNK_SIZE));
      const last  = Math.min(
        Math.ceil(total / CHUNK_SIZE) - 1,
        Math.floor((end - 1) / CHUNK_SIZE),
      );
      for (let c = first; c <= last; c++) fetchChunk(c);
    },
    // ``Array.findIndex``-compat: search only loaded chunks.  Returns
    // ``-1`` for tracks outside the loaded window — callers already have
    // a graceful fallback when the search fails (the queue-from-now-
    // playing button hits ``Player.playTrack(t)`` as the alternative).
    findIndex(pred) {
      for (const [c, arr] of chunks) {
        const localIdx = arr.findIndex(pred);
        if (localIdx >= 0) return c * CHUNK_SIZE + localIdx;
      }
      return -1;
    },
    // Discard every chunk + any in-flight fetches.  Used when the user
    // changes sort (the existing data is in a different order now) or
    // switches off this view entirely.
    invalidate() {
      chunks.clear();
      pending.clear();
      lru.length = 0;
    },
    // Slice-like helper for cases where the caller needs the currently
    // loaded contiguous window starting at idx (Player.setQueue path).
    // Returns at most ``count`` loaded entries; stops at the first
    // unloaded slot so the queue is dense, not sparse.
    loadedSliceFrom(idx, count) {
      const out = [];
      for (let i = idx; i < total && out.length < count; i++) {
        const t = readSlot(i);
        if (!t) break;
        out.push(t);
      }
      return out;
    },
    // Iterator yielding only the LOADED tracks across all chunks, in
    // their chunk-index order.  Lets ``[...store]`` work without
    // crashing (it'd otherwise throw "is not iterable" on the Proxy)
    // and gives reasonable behaviour to any incidental iteration
    // (selection-bar batch helpers, dev-console inspection).  Callers
    // who need a fully-loaded array must use loadedSliceFrom + extend
    // or push every chunk to load first; iteration over the proxy
    // never triggers fetches by design.
    *[Symbol.iterator]() {
      const sorted = [...chunks.keys()].sort((a, b) => a - b);
      for (const c of sorted) {
        for (const t of chunks.get(c)) yield t;
      }
    },
  };

  function fetchChunk(chunkIdx) {
    if (chunks.has(chunkIdx) || pending.has(chunkIdx)) return;
    const offset = chunkIdx * CHUNK_SIZE;
    const limit  = Math.min(CHUNK_SIZE, total - offset);
    if (limit <= 0) return;
    const p = fetcher(offset, limit).then(arr => {
      chunks.set(chunkIdx, arr);
      pending.delete(chunkIdx);
      // LRU bookkeeping — push then trim from the front, skipping the
      // chunk we just loaded so a single-chunk window can't evict
      // itself.
      const ix = lru.indexOf(chunkIdx);
      if (ix >= 0) lru.splice(ix, 1);
      lru.push(chunkIdx);
      while (lru.length > MAX_CHUNKS) {
        const evict = lru.shift();
        if (evict !== chunkIdx) chunks.delete(evict);
      }
      if (onChunkLoad) {
        try { onChunkLoad(chunkIdx); } catch (_) { /* listener isolation */ }
      }
    }).catch(() => {
      // Don't keep the failed chunk in ``pending`` forever — let the
      // next access retry.
      pending.delete(chunkIdx);
    });
    pending.set(chunkIdx, p);
  }

  function readSlot(i) {
    if (i < 0 || i >= total) return undefined;
    const chunkIdx = Math.floor(i / CHUNK_SIZE);
    const arr = chunks.get(chunkIdx);
    if (arr) {
      // Touch LRU on access so a chunk we're actively rendering doesn't
      // get evicted by a jump-to-far-away chunk fetch.
      const ix = lru.indexOf(chunkIdx);
      if (ix >= 0) { lru.splice(ix, 1); lru.push(chunkIdx); }
      return arr[i - chunkIdx * CHUNK_SIZE];
    }
    fetchChunk(chunkIdx);  // trigger background fetch on first access
    return undefined;
  }

  // Proxy presents the store as an array-shaped object: numeric indexing
  // returns the track (or undefined while loading), ``.length`` reports
  // the FULL library size, and named members (ensureRange, invalidate,
  // findIndex, etc.) pass through unchanged.
  return new Proxy(target, {
    get(t, prop) {
      if (prop === 'length') return total;
      if (typeof prop === 'string') {
        // Convert numeric-string indexes ("0", "47", …) into reads.
        // ``Number.isInteger`` accepts the parsed integer; non-numeric
        // string props ("length", "ensureRange", "_chunks", etc.) fall
        // through to the target.
        const n = Number(prop);
        if (Number.isInteger(n) && n >= 0 && String(n) === prop) {
          return readSlot(n);
        }
      }
      return t[prop];
    },
    has(t, prop) {
      if (prop === 'length') return true;
      if (typeof prop === 'string') {
        const n = Number(prop);
        if (Number.isInteger(n) && n >= 0) return n < total;
      }
      return prop in t;
    },
  });
}
let _infoCallback = null;
let _focusedIdx = -1;   // keyboard-navigated row index (J/K navigation)
let _dupViewActive = false;  // true while in duplicates view (forces Location column visible)
// Sub-view filter state
let _groupItems     = [];   // raw list for current group view
let _groupNameKey   = '';   // which field to filter on
let _groupCountKey  = '';
let _groupLabel     = '';
let _groupOnClick   = null;

// ── Virtual scroll state ───────────────────────────────────────────────────────
let ROW_H    = 28;   // px per row (measured after first paint; default matches td padding)
const VS_BUF = 10;   // rows to render above/below viewport
let _rowHMeasured = false;  // becomes true after the first measurement

let _vsStart = 0;    // first rendered data index
let _vsEnd   = 0;    // one past last rendered data index

// Row pool for virtual scroll — pre-built TRs reused across scroll events.
// Each entry is the same DOM node lifecycle: we mutate text/dataset/classes
// instead of detaching + recreating, which keeps scroll inexpensive at large
// row counts (the react-window pattern).
const _rowPool   = [];
let   _vsTopSpacer = null;
let   _vsBotSpacer = null;

// ── Multi-select state ─────────────────────────────────────────────────────────
let _selected     = new Set();
let _lastClickIdx = -1;

// ── Grid view state ────────────────────────────────────────────────────────────
let _gridView = false;

// ── Column visibility state ────────────────────────────────────────────────────
const ALL_COLS = ['col-num','col-cover','col-title','col-album-artist','col-artist','col-album','col-track','col-year','col-dur','col-format','col-location','col-rating'];
const COL_LABELS = {
  'col-num':          '#',
  'col-cover':        'Cover',
  'col-title':        'Title',
  'col-album-artist': 'Album Artist',
  'col-artist':       'Artist',
  'col-album':        'Album',
  'col-track':        'Track',
  'col-year':         'Year',
  'col-dur':          'Duration',
  'col-format':       'Type',
  'col-location':     'Location',
  'col-rating':       '★',
};

// ── Ratings cache (loaded per view) ───────────────────────────────────────
let _ratingsCache = {};  // track_id → rating (0-5)
let _hiddenCols = new Set(JSON.parse(localStorage.getItem('sb_hidden_cols') || '["col-location"]'));

// ── Location / alias state ──────────────────────────────────────────────────
let _aliasMap = {};           // { "/abs/path": "alias", ... }
let _exposeLocalFiles = true; // true = show full file path, false = show alias-based path

export function setAliasMap(map) { _aliasMap = map || {}; }
export function setExposeLocalFiles(v) { _exposeLocalFiles = v; }

function _displayPath(fullPath) {
  if (!fullPath) return '';
  if (_exposeLocalFiles) return fullPath;
  // Mode OFF: replace scan-root prefix with alias (longest prefix match)
  let bestRoot = '', bestAlias = '';
  for (const [root, alias] of Object.entries(_aliasMap)) {
    if (fullPath.startsWith(root) && root.length > bestRoot.length) {
      bestRoot = root;
      bestAlias = alias;
    }
  }
  if (bestRoot && bestAlias) {
    return bestAlias + fullPath.slice(bestRoot.length);
  }
  return fullPath;
}

// Register a callback: cb(tracks, idx) called when user requests info for a track
export function onInfo(cb) { _infoCallback = cb; }

// ── Render helpers ─────────────────────────────────────────────────────────────
function fmtDur(sec) {
  if (!sec) return '—';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/** Map audio format to a CSS class suffix for colour-coded badges. */
function _fmtClass(fmt) {
  if (!fmt) return 'unknown';
  const f = fmt.toUpperCase();
  if (f === 'FLAC' || f === 'ALAC' || f === 'WAV' || f === 'WAVE' || f === 'AIFF')
    return 'lossless';
  if (f === 'MP3') return 'mp3';
  if (f.includes('OGG') || f.includes('VORBIS') || f === 'OPUS') return 'ogg';
  if (f === 'AAC' || f === 'M4A') return 'aac';
  if (f === 'SID' || f === 'PSID') return 'sid';
  if (f === 'MIDI' || f === 'MID') return 'midi';
  // Tracker formats
  if (['MOD','S3M','XM','IT','MTM','MED','OCT','669','AHX','HVL'].includes(f))
    return 'tracker';
  return 'other';
}

function _renderStars(rating) {
  // The radiogroup pattern requires exactly one ``aria-checked="true"``
  // child at a time — the *current value*, not "all the stars up to it".
  // Visual fill is still cumulative; only the screen-reader marker is
  // single-selected.  Tabindex goes on whichever star is currently
  // checked (or the first star when rating is 0) so Tab lands there.
  const checkedIdx = rating > 0 ? rating : 1;
  let html = `<span class="star-group" role="radiogroup" aria-label="Rating, ${rating} of 5 stars">`;
  for (let i = 1; i <= 5; i++) {
    const filled = i <= rating;
    const cls = filled ? 'star star-filled' : 'star star-empty';
    const glyph = filled ? '★' : '☆';
    const isChecked = i === checkedIdx && rating > 0;
    const tabindex = i === checkedIdx ? 0 : -1;
    html += (
      `<span class="${cls}" role="radio" tabindex="${tabindex}"`
      + ` data-val="${i}" aria-checked="${isChecked}"`
      + ` aria-label="${i} star${i === 1 ? '' : 's'}">${glyph}</span>`
    );
  }
  html += '</span>';
  return html;
}

// ── Skeleton loading rows ─────────────────────────────────────────────────────
function _showSkeletonRows(count = 18) {
  const albumGrid = document.getElementById('album-grid');
  if (albumGrid) albumGrid.hidden = true;
  // Restore the track-list scroll container in case we're arriving from the
  // Galaxy view (which sets it display:none).  Without this the skeleton —
  // and then the loaded rows — would paint into a hidden wrapper, so clicking
  // a galaxy cluster chip showed a blank screen for the whole fetch.
  const galaxyView = document.getElementById('galaxy-view');
  if (galaxyView && !galaxyView.hidden) galaxyView.hidden = true;
  const wrap = document.getElementById('track-list-wrap');
  if (wrap) wrap.style.display = '';
  document.getElementById('track-table').style.display = '';
  // The skeleton view replaces the entire tbody contents, so the pool
  // detaches.  Clearing it here makes the next _vsRender treat the pool as
  // empty rather than try to reuse phantom nodes.
  _vsResetPool();
  tbody.innerHTML = '';
  emptyEl.hidden = true;
  loadingEl.hidden = true;
  for (let i = 0; i < count; i++) {
    const tr = document.createElement('tr');
    tr.className = 'skeleton';
    tr.innerHTML = `
      <td class="col-num"><span class="skel-bar" style="width:${16 + Math.random() * 8|0}px"></span></td>
      <td class="col-cover"><span class="skel-bar" style="width:28px;height:28px;border-radius:4px;display:inline-block"></span></td>
      <td class="col-title"><span class="skel-bar" style="width:${80 + Math.random() * 80|0}px"></span></td>
      <td class="col-album-artist"><span class="skel-bar" style="width:${50 + Math.random() * 50|0}px"></span></td>
      <td class="col-artist"><span class="skel-bar" style="width:${40 + Math.random() * 50|0}px"></span></td>
      <td class="col-album"><span class="skel-bar" style="width:${50 + Math.random() * 60|0}px"></span></td>
      <td class="col-track"><span class="skel-bar" style="width:20px"></span></td>
      <td class="col-year"><span class="skel-bar" style="width:28px"></span></td>
      <td class="col-dur"><span class="skel-bar" style="width:32px"></span></td>
      <td class="col-format"><span class="skel-bar" style="width:36px"></span></td>
      <td class="col-location"><span class="skel-bar" style="width:${60 + Math.random() * 80|0}px"></span></td>
      <td class="col-rating"><span class="skel-bar" style="width:44px"></span></td>`;
    tbody.appendChild(tr);
  }
}

// ── Sidebar count badges ──────────────────────────────────────────────────────
// ``truncated`` = the value was capped at the API page-size and the real
// total may be higher.  We append a literal ``+`` to communicate "at least
// this many" instead of misleading the user with a hard ceiling.  The
// All-Tracks view uses /tracks?limit=5000 and hits the cap on big libraries
// — without the ``+`` a 50 000-track library looks like exactly 5 000.
function _updateNavBadge(view, count, truncated = false) {
  const li = document.querySelector(`#nav-library li[data-view="${view}"]`);
  if (!li) return;
  let badge = li.querySelector('.nav-count');
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'nav-count';
    li.appendChild(badge);
  }
  if (count == null) {
    badge.textContent = '';
  } else {
    badge.textContent = count.toLocaleString() + (truncated ? '+' : '');
  }
}

async function _refreshTrackCount() {
  try {
    const { count } = await API('/tracks/count');
    _updateNavBadge('all', count);
  } catch {}
}

// ── Make a single track row element ───────────────────────────────────────────
//
// Rows are produced by ``_makeRowSkeleton`` (no data) and filled via
// ``_fillTrackRow``.  All event handling is delegated to ``tbody`` (see the
// listener block below ``_vsRender``) — that lets the virtual-scroll loop
// reuse a fixed pool of DOM nodes instead of attaching ~7 listeners per row
// on every scroll frame.
const _CANONICAL_ROW_HTML = `
    <td class="col-num"><span class="row-play-glyph" aria-hidden="true" title="Double-click to play">▶</span><span class="row-num"></span></td>
    <td class="col-cover"><div class="col-cover-frame"><span class="art-placeholder row-art-placeholder"></span><img class="row-cover-img" decoding="async" alt=""></div></td>
    <td class="col-title"></td>
    <td class="col-album-artist"></td>
    <td class="col-artist"></td>
    <td class="col-album"></td>
    <td class="col-track"></td>
    <td class="col-year"></td>
    <td class="col-dur"></td>
    <td class="col-format"><span class="fmt-badge"></span></td>
    <td class="col-location"></td>
    <td class="col-rating"></td>`;

function _makeRowSkeleton() {
  const tr = document.createElement('tr');
  tr.setAttribute('draggable', 'true');
  tr.innerHTML = _CANONICAL_ROW_HTML;
  return tr;
}

function _fillTrackRow(tr, t, i) {
  // Windowed-store skeleton path: when the row at global index ``i``
  // hasn't been fetched yet, ``t`` is undefined.  Render a row-shaped
  // skeleton (matches _showSkeletonRows visually) so the layout stays
  // stable while the chunk fetch lands.  Once it does the windowed
  // store fires its onChunkLoad callback which forces a re-render and
  // this row gets the real data on the next pass.
  if (!t) {
    tr.removeAttribute('data-id');
    tr.dataset.idx = i;
    tr.className = 'skeleton';
    tr.innerHTML = `
      <td class="col-num"><span class="skel-bar" style="width:24px"></span></td>
      <td class="col-cover"><span class="skel-bar" style="width:28px;height:28px;border-radius:4px;display:inline-block"></span></td>
      <td class="col-title"><span class="skel-bar" style="width:120px"></span></td>
      <td class="col-album-artist"><span class="skel-bar" style="width:80px"></span></td>
      <td class="col-artist"><span class="skel-bar" style="width:80px"></span></td>
      <td class="col-album"><span class="skel-bar" style="width:90px"></span></td>
      <td class="col-track"><span class="skel-bar" style="width:20px"></span></td>
      <td class="col-year"><span class="skel-bar" style="width:28px"></span></td>
      <td class="col-dur"><span class="skel-bar" style="width:32px"></span></td>
      <td class="col-format"><span class="skel-bar" style="width:36px"></span></td>
      <td class="col-location"><span class="skel-bar" style="width:80px"></span></td>
      <td class="col-rating"><span class="skel-bar" style="width:44px"></span></td>`;
    return;
  }
  // If the row was previously rendered as a skeleton (different cell
  // structure — shimmer bars instead of row-num / cover-frame / etc.),
  // restore the canonical layout so the field assignments below find
  // the elements they ``querySelector`` for.
  if (tr.classList.contains('skeleton')) {
    tr.innerHTML = _CANONICAL_ROW_HTML;
  }
  tr.dataset.id  = t.id;
  tr.dataset.idx = i;

  // Reset class list to base + apply state-dependent classes.  We keep
  // ``kb-focused`` / ``playing`` / ``dragging`` off the row by default —
  // the surrounding code re-applies them after a render finishes.
  tr.className = '';
  if (_selected.has(i)) tr.classList.add('multi-selected');
  if (t._scanned === false) tr.classList.add('unscanned');
  if (t._dupGroupFirst) tr.classList.add('dup-group-first');
  if (t._dupIsPrimary)  tr.classList.add('dup-primary');
  if (t._dupGroupId && !t._dupIsPrimary) tr.classList.add('dup-variant');

  const disc = t.disc_number != null ? `D${t.disc_number}` : '';
  const trk  = t.track_number != null ? String(t.track_number).padStart(2, '0') : '';
  const trackStr = disc && trk ? `${disc}-${trk}` : (trk || disc || '');

  const rating = _ratingsCache[t.id] || 0;
  const unscan = t._scanned === false;
  const hasAA = !unscan && t.album_artist;
  const hasAr = !unscan && t.artist;
  const hasAl = !unscan && t.album;

  const cells = tr.children;
  // col-num: keep play glyph + numbered span
  const numSpan = cells[0].querySelector('.row-num');
  if (numSpan) numSpan.textContent = String(i + 1);

  // col-cover: format-aware emoji placeholder sitting under a lazy <img>.
  // The placeholder is always painted (matching the bottom-left player and
  // the mobile row) so a missing cover never shows a broken-image glyph.
  // On successful load the <img> picks up ``.loaded`` and its opacity goes
  // to 1, hiding the placeholder behind it.  ``onerror`` keeps the
  // placeholder visible so 404 art still reads as the format icon.
  const coverCell  = cells[1];
  const coverFrame = coverCell.firstElementChild;
  const coverPh    = coverFrame?.firstElementChild;
  const coverImg   = coverFrame?.lastElementChild;
  if (coverPh) coverPh.textContent = artPlaceholderEmoji(t);
  if (coverImg) {
    // ``fallback=404`` is the cheap way to ask "is there real art?"
    // — when there isn't, the server returns a cacheable 404 (no
    // body) and IMG.onerror fires, leaving the format emoji visible.
    // Earlier revisions tried to read the X-SoniqBoom-Art header via
    // fetch+blob+ObjectURL, which lost the browser image pipeline's
    // bitmap-cache coalescing and image-priority queue — folder-open
    // went from ~50 ms to multiple seconds on big SID/MOD folders
    // (regression D14).  NOTE: the request bound is the virtual-scroll
    // WINDOWING (only visible rows + buffer are ever filled), NOT a
    // ``loading="lazy"`` attribute — that attribute was REMOVED from the
    // row <img> because, set after the row was built inside the scroll
    // subtree, the browser deferred the load past the visible window and
    // never re-fired it, so covers silently never requested.  The grid
    // (_loadAlbumCardArt) sidesteps the same trap with a detached Image().
    const wantedSrc = t.id ? `/api/art/${t.id}?size=sm&fallback=404` : '';
    // If a background art-fill (play / on-demand extract / backfill) landed for
    // this track while its row was scrolled out of view, the bare URL would
    // reuse the browser-cached placeholder.  ``__sbArtPending`` (set by the
    // art_ready WS handler in app.js) flags those ids so we bust them once on
    // the render that brings the row back into view.
    const pendingBust = !!(t.id && window.__sbArtPending && window.__sbArtPending.has(t.id));
    // Guard on the TRACK id, not the exact URL: a recycled row (new track) or a
    // pending bust re-fetches; a re-render of the SAME track keeps whatever it
    // already loaded (so we never revert a just-busted cover back to the bare,
    // still-cached placeholder URL).
    if (coverImg.dataset.tid !== (t.id || '') || pendingBust) {
      if (pendingBust) window.__sbArtPending.delete(t.id);
      coverImg.dataset.tid = t.id || '';
      const src = wantedSrc && pendingBust ? `${wantedSrc}&_t=${Date.now()}` : wantedSrc;
      coverImg.dataset.src = src;
      coverImg.removeAttribute('src');
      coverImg.classList.remove('loaded');
      if (src) {
        coverImg.onload  = () => coverImg.classList.add('loaded');
        coverImg.onerror = () => coverImg.classList.remove('loaded');
        coverImg.src = src;
      }
    }
  }

  // col-title
  const titleTd = cells[2];
  titleTd.textContent = t.title || '—';
  titleTd.title = t.title || '';

  // col-album-artist
  const aaTd = cells[3];
  aaTd.classList.toggle('col-empty', !unscan && !hasAA);
  aaTd.title = t.album_artist || '';
  if (unscan) {
    aaTd.textContent = '';
  } else if (hasAA) {
    aaTd.innerHTML = `<span class="cell-link" data-action="album-artist">${esc(t.album_artist)}</span>`;
  } else {
    aaTd.textContent = '—';
  }

  // col-artist
  const arTd = cells[4];
  arTd.classList.toggle('col-empty', !unscan && !hasAr);
  arTd.title = t.artist || '';
  arTd.textContent = unscan ? '' : (t.artist || '—');

  // col-album
  const alTd = cells[5];
  alTd.classList.toggle('col-empty', !unscan && !hasAl);
  alTd.title = t.album || '';
  if (unscan) {
    alTd.textContent = '';
  } else if (hasAl) {
    alTd.innerHTML = `<span class="cell-link" data-action="album">${esc(t.album)}</span>`;
  } else {
    alTd.textContent = '—';
  }

  // col-track / col-year
  cells[6].textContent = trackStr;
  cells[7].textContent = t.year || '';

  // col-dur
  const durTd = cells[8];
  durTd.classList.toggle('col-empty', !unscan && !t.duration);
  durTd.textContent = unscan ? '' : fmtDur(t.duration);

  // col-format
  const fmtBadge = cells[9].firstElementChild;
  fmtBadge.className = `fmt-badge fmt-${_fmtClass(t.format)}`;
  fmtBadge.textContent = t.format || '';

  // col-location
  const locTd = cells[10];
  locTd.title = _exposeLocalFiles ? (t.path || '') : '';
  locTd.textContent = _displayPath(t.path || '');

  // col-rating — innerHTML required because stars use nested elements
  cells[11].innerHTML = _renderStars(rating);
}

// Persist rating + bring focus back into the star group after a change.
//
// ``preventScroll: true`` on the focus() call is critical — without it the
// browser auto-scrolls the focused star into view, which on a virtualised
// list can land a long way from the user's actual viewport if the row was
// re-keyed during a pending VS render.  The autoscroll then triggers our
// scroll listener, which renders the new window and may end up shifting
// the spacers further; the user perceives this as the list "running away"
// on its own until it bottoms out.
function _setRowRating(trackId, newRating, ratingTd) {
  const finalRating = (_ratingsCache[trackId] === newRating) ? 0 : newRating;
  _ratingsCache[trackId] = finalRating;
  ratingTd.innerHTML = _renderStars(finalRating);
  const focusTarget = ratingTd.querySelector(`.star[data-val="${Math.max(1, finalRating)}"]`);
  if (focusTarget) {
    ratingTd.querySelectorAll('.star').forEach(s => { s.tabIndex = -1; });
    focusTarget.tabIndex = 0;
    focusTarget.focus({ preventScroll: true });
  }
  fetch(`/api/tracks/${trackId}/rating`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating: finalRating }),
  }).catch(() => {});
}

// ── Virtual scroll render ──────────────────────────────────────────────────────
//
// We keep a row-pool of pre-built TRs (``_rowPool``).  On each scroll we
// resize the pool to cover the new visible window, mutate each pool row's
// contents/dataset, and let the top/bottom spacer TRs hold the height of
// the off-screen ranges.  This avoids the per-scroll
// ``tbody.innerHTML = ''`` + DOM-build cycle that dominated profiles.
function _vsResetPool() {
  _rowPool.length = 0;
  _vsTopSpacer = null;
  _vsBotSpacer = null;
}

function _ensureSpacer(which) {
  // Lazy-create the top/bottom spacer TRs.  We never destroy them — they
  // just get their height set to 0 when not needed.
  const ref = which === 'top' ? _vsTopSpacer : _vsBotSpacer;
  if (ref) return ref;
  const sp = document.createElement('tr');
  sp.className = 'vs-spacer';
  sp.innerHTML = `<td colspan="11" style="height:0;padding:0;border:none"></td>`;
  if (which === 'top') _vsTopSpacer = sp; else _vsBotSpacer = sp;
  return sp;
}

function _vsRender(force = false) {
  if (!currentTracks.length) return;
  const wrap    = document.getElementById('track-list-wrap');
  const scrollY = wrap.scrollTop;
  const viewH   = wrap.clientHeight;

  const firstVis = Math.floor(scrollY / ROW_H);
  const lastVis  = Math.ceil((scrollY + viewH) / ROW_H);
  const newStart = Math.max(0, firstVis - VS_BUF);
  const newEnd   = Math.min(currentTracks.length, lastVis + VS_BUF);

  if (!force && newStart === _vsStart && newEnd === _vsEnd) return;
  _vsStart = newStart;
  _vsEnd   = newEnd;

  const visibleCount = _vsEnd - _vsStart;
  const topH = _vsStart * ROW_H;
  const botH = (currentTracks.length - _vsEnd) * ROW_H;

  // If tbody was wiped by a sibling renderer (group view / skeleton),
  // _rowPool's nodes are detached.  Detect that and reattach.  We compare
  // by parentNode rather than re-querying because that's O(1).
  const topSp = _ensureSpacer('top');
  const botSp = _ensureSpacer('bot');
  const needsReattach = topSp.parentNode !== tbody;

  if (needsReattach) {
    tbody.innerHTML = '';
    tbody.appendChild(topSp);
    for (const row of _rowPool) tbody.appendChild(row);
    tbody.appendChild(botSp);
  }

  // Resize the pool to match the visible window.  Grow by appending new
  // skeleton rows; shrink by removing trailing rows from the DOM and pool.
  while (_rowPool.length < visibleCount) {
    const tr = _makeRowSkeleton();
    _rowPool.push(tr);
    tbody.insertBefore(tr, botSp);
  }
  while (_rowPool.length > visibleCount) {
    const tr = _rowPool.pop();
    if (tr.parentNode) tr.parentNode.removeChild(tr);
  }

  // Update spacer heights (single style write, no innerHTML).
  topSp.firstElementChild.style.height = topH + 'px';
  botSp.firstElementChild.style.height = botH + 'px';

  // Fill the visible rows.  For a WindowedTrackStore, ``currentTracks[i]``
  // returns ``undefined`` while the chunk fetches; ``_fillTrackRow``
  // renders a shimmer skeleton for those.  Once the chunk lands the
  // store's onChunkLoad callback re-fires _vsRender(true) and the
  // skeleton gets replaced with real data.
  for (let n = 0; n < visibleCount; n++) {
    _fillTrackRow(_rowPool[n], currentTracks[_vsStart + n], _vsStart + n);
  }
  // Trigger lazy chunk loading for the visible window plus the buffer.
  // No-op for plain arrays (no ``ensureRange`` method).
  if (currentTracks && typeof currentTracks.ensureRange === 'function') {
    currentTracks.ensureRange(_vsStart, _vsEnd);
  }
  // Background-fill real lengths for any AdLib/IMF rows now on screen that still
  // show the 180s placeholder (debounced; one-time per track; any view).
  _scheduleDurationProbe();

  markPlayingRow();
  _applyColVisibility();

  // Restore keyboard focus indicator after a rebuild
  if (_focusedIdx >= _vsStart && _focusedIdx < _vsEnd) {
    const focusRow = tbody.querySelector(`tr[data-idx="${_focusedIdx}"]`);
    if (focusRow) focusRow.classList.add('kb-focused');
  }

  // Measure row height on the first paint with real data and re-render
  // once with the corrected ROW_H so the spacer maths matches actual layout.
  if (!_rowHMeasured && _rowPool.length) {
    requestAnimationFrame(() => _measureRowHeight());
  }
}

// Measure the rendered height of the first visible row and update ROW_H
// if it diverges from the current default.  Dispatched once per data load
// (gated by ``_rowHMeasured``) plus on a ``themechange`` custom event so
// theme-driven padding changes don't desync the virtual scroll math.
function _measureRowHeight() {
  if (!_rowPool.length) return;
  const rect = _rowPool[0].getBoundingClientRect();
  const h = Math.round(rect.height);
  if (h > 0 && Math.abs(h - ROW_H) >= 1) {
    ROW_H = h;
    _vsStart = _vsEnd = 0;   // force the next _vsRender to recompute
    _vsRender(true);
  }
  _rowHMeasured = true;
}

// Optional theme-change hook — if the app dispatches ``themechange`` on
// document we'll re-measure.  Absent the event we simply rely on the
// per-load measurement above.
document.addEventListener('themechange', () => {
  _rowHMeasured = false;
  _vsRender(true);
});

async function renderTracks(tracks) {
  _leaveGalaxy();          // switching to a table view exits the galaxy
  currentTracks = tracks;
  _selected.clear();
  _lastClickIdx = -1;
  _focusedIdx   = -1;
  _vsStart = 0;
  _vsEnd   = 0;
  _rowHMeasured = false;     // re-measure for the next dataset

  // Reset the empty-state markup every time — both ``showDuplicates``
  // ("No duplicate tracks found.") and ``_renderBranchEmpty`` (branch-
  // folder copy + opt-in recursive button) replace the heading and
  // body for their specific cases.  Restoring the default snapshot
  // captured at module load keeps the empty state consistent across
  // view switches.
  if (emptyEl.innerHTML !== _EMPTY_DEFAULT_HTML && tracks.length > 0) {
    // Only restore when we have real tracks to render — keeps the
    // customised state visible while the empty case persists.
    emptyEl.innerHTML = _EMPTY_DEFAULT_HTML;
  } else if (tracks.length > 0) {
    // No-op: default markup is already in place.
  } else {
    // Empty result.  Show the "add a folder" CTA + matching heading ONLY when
    // the whole library is empty (showAll set _showAddFolderCta); a merely
    // filtered/searched-empty view gets neutral copy and no misdirected CTA.
    const _emptyHeading = emptyEl.querySelector('h4');
    const _addFolder    = emptyEl.querySelector('#empty-add-folder');
    if (_showAddFolderCta) {
      if (_emptyHeading) _emptyHeading.textContent = 'Your library is empty';
      if (_addFolder) _addFolder.hidden = false;
    } else {
      if (_emptyHeading) _emptyHeading.textContent = 'No tracks found';
      if (_addFolder) _addFolder.hidden = true;
    }
  }
  _showAddFolderCta = false;   // consume once — defaults off for the next render

  emptyEl.hidden  = tracks.length > 0;
  loadingEl.hidden = true;

  // Hide album grid, show table
  const albumGrid = document.getElementById('album-grid');
  if (albumGrid) albumGrid.hidden = true;
  document.getElementById('track-table').style.display = '';

  // Tear down the pool — switching datasets (and group/track views toggle
  // tbody anyway) so the old DOM nodes don't belong here.
  _vsResetPool();
  tbody.innerHTML = '';

  if (tracks.length > 0) {
    _vsRender(true);
    // Kick off lazy ratings fetch for the initial visible range
    _fetchVisibleRatings();
  }

  // Scroll to top — also reset the scroll-throttle cache so the first
  // post-render scroll event isn't dropped by the same-position guard.
  const wrap = document.getElementById('track-list-wrap');
  if (wrap) wrap.scrollTop = 0;
  _scrollLastY = -1;

  _updateSelectionBar();
  _applyColVisibility();
}

// ── Lazy ratings: fetch only for visible rows ─────────────────────────────────
let _ratingsFetchPending = false;

async function _fetchVisibleRatings() {
  if (_ratingsFetchPending) return;
  const ids = [];
  for (let i = _vsStart; i < _vsEnd && i < currentTracks.length; i++) {
    const t = currentTracks[i];
    if (t && t.id && _ratingsCache[t.id] === undefined) ids.push(t.id);
  }
  if (!ids.length) return;
  _ratingsFetchPending = true;
  try {
    const res = await fetch('/api/tracks/meta/ratings/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    const data = await res.json();
    Object.assign(_ratingsCache, data);
    // Mark fetched IDs that had no rating as 0 so we don't re-fetch
    for (const id of ids) if (_ratingsCache[id] === undefined) _ratingsCache[id] = 0;
    // Patch the rating cell on visible rows in place.  Previously we
    // called ``_vsRender(true)`` here which rebuilt the entire virtual
    // window — any spacer-height or row-pool resize that happened in the
    // forced render could cascade into a scroll-event feedback loop
    // (autoscroll regression).  In-place mutation only touches the cells
    // whose rating actually changed, so the scroll position never moves
    // and the listener never re-fires.
    for (const row of _rowPool) {
      const id = row.dataset.id;
      if (!id || !(id in _ratingsCache)) continue;
      const ratingTd = row.children[11];   // col-rating is index 11
      if (ratingTd) ratingTd.innerHTML = _renderStars(_ratingsCache[id]);
    }
  } catch { /* non-fatal */ }
  _ratingsFetchPending = false;
}

// ── Update multi-selected classes on visible rows without full VS rebuild ─────
function _refreshSelectionClasses() {
  tbody.querySelectorAll('tr[data-idx]').forEach(row => {
    const idx = parseInt(row.dataset.idx, 10);
    row.classList.toggle('multi-selected', _selected.has(idx));
  });
}

// ── Scroll listener for virtual scroll ────────────────────────────────────────
//
// rAF-throttled with a same-position short-circuit.  Why both:
//
//   1) rAF coalesces — Chrome on macOS fires `scroll` faster than 60 Hz
//      on a trackpad fling, especially after layout shifts.  Without a
//      throttle, _vsRender ran multiple times per frame.  More
//      importantly, any side effect of _vsRender (a spacer-height
//      adjustment, a pool resize) could in turn trigger another scroll
//      event in the same task, and we'd be in a feedback loop that
//      drifted scrollTop in one direction until the list bottomed out
//      — the "phantom autoscroll that only stops at the end" defect.
//
//   2) Same-position short-circuit — even with rAF throttling we can be
//      handed a scroll event where scrollTop hasn't actually changed
//      since the previous render (browsers fire scroll on layout shifts
//      that happen to leave scrollTop alone).  Skipping the re-render
//      keeps _vsStart/_vsEnd stable and the spacer heights untouched,
//      which is what breaks the feedback loop in step 1.
let _scrollRafPending = false;
let _scrollLastY      = -1;
document.getElementById('track-list-wrap').addEventListener('scroll', () => {
  if (_scrollRafPending) return;
  _scrollRafPending = true;
  requestAnimationFrame(() => {
    _scrollRafPending = false;
    const wrap = document.getElementById('track-list-wrap');
    const y = wrap.scrollTop;
    if (y === _scrollLastY) return;
    _scrollLastY = y;
    _vsRender();
    _fetchVisibleRatings();
  });
}, { passive: true });

// ── Delegated row event listeners ─────────────────────────────────────────────
//
// All click/dblclick/contextmenu/keydown/dragstart for track rows are
// handled here on ``tbody`` instead of per row.  Saves ~7 listener
// attach/detach pairs per row on every virtual-scroll frame.
function _rowFromEvent(e) {
  return e.target.closest('tr[data-idx]');
}

tbody.addEventListener('click', (e) => {
  const tr = _rowFromEvent(e);
  if (!tr) return;
  const i = parseInt(tr.dataset.idx, 10);
  if (!Number.isFinite(i)) return;
  const t = currentTracks[i];
  if (!t) return;

  // Star rating cell — handle clicks here before the row-select logic.
  const ratingTd = e.target.closest('.col-rating');
  if (ratingTd && tr.contains(ratingTd)) {
    const star = e.target.closest('.star');
    if (star) {
      e.stopPropagation();
      _setRowRating(t.id, parseInt(star.dataset.val, 10), ratingTd);
    }
    return;
  }

  // Clickable metadata cells (album artist / album)
  const cellLink = e.target.closest('.cell-link');
  if (cellLink) {
    e.stopPropagation();
    const action = cellLink.dataset.action;
    if (action === 'album-artist' && t.album_artist) {
      showAlbums(null, t.album_artist, 'album_artist');
    } else if (action === 'album' && t.album) {
      showAlbumTracks(t.artist, t.album_artist, t.album,
        () => showAlbums(t.artist, t.album_artist, t.album_artist ? 'album_artist' : 'artist'));
    }
    return;
  }

  if (e.shiftKey && _lastClickIdx >= 0) {
    const lo = Math.min(_lastClickIdx, i), hi = Math.max(_lastClickIdx, i);
    if (!e.metaKey && !e.ctrlKey) _selected.clear();
    for (let j = lo; j <= hi; j++) _selected.add(j);
  } else if (e.metaKey || e.ctrlKey) {
    if (_selected.has(i)) _selected.delete(i); else _selected.add(i);
    _lastClickIdx = i;
  } else {
    _selected.clear();
    _selected.add(i);
    _lastClickIdx = i;
    selectRow(tr, i);
  }
  _refreshSelectionClasses();
  _updateSelectionBar();
});

tbody.addEventListener('dblclick', (e) => {
  const tr = _rowFromEvent(e);
  if (!tr) return;
  const i = parseInt(tr.dataset.idx, 10);
  if (Number.isFinite(i)) playFrom(i);
});

tbody.addEventListener('contextmenu', (e) => {
  const tr = _rowFromEvent(e);
  if (!tr) return;
  const i = parseInt(tr.dataset.idx, 10);
  if (!Number.isFinite(i)) return;
  e.preventDefault();
  if (_infoCallback) _infoCallback(currentTracks, i);
});

tbody.addEventListener('keydown', (e) => {
  // Star ratings — the col-rating td contains focusable stars.
  const ratingTd = e.target.closest('.col-rating');
  if (ratingTd) {
    const tr = _rowFromEvent(e);
    if (!tr) return;
    const i = parseInt(tr.dataset.idx, 10);
    const t = currentTracks[i];
    if (!t) return;
    const star = e.target.closest('.star');
    if (!star) return;
    const current = parseInt(star.dataset.val, 10);
    const cur = _ratingsCache[t.id] || 0;
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault(); e.stopPropagation();
      _setRowRating(t.id, current, ratingTd);
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
      e.preventDefault(); e.stopPropagation();
      _setRowRating(t.id, Math.max(1, cur - 1), ratingTd);
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation();
      _setRowRating(t.id, Math.min(5, cur + 1 || 1), ratingTd);
    } else if (e.key === 'Home') {
      e.preventDefault(); _setRowRating(t.id, 1, ratingTd);
    } else if (e.key === 'End') {
      e.preventDefault(); _setRowRating(t.id, 5, ratingTd);
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      _ratingsCache[t.id] = 0;
      ratingTd.innerHTML = _renderStars(0);
      fetch(`/api/tracks/${t.id}/rating`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rating: 0 }),
      }).catch(() => {});
    }
  }
});

tbody.addEventListener('dragstart', (e) => {
  const tr = _rowFromEvent(e);
  if (!tr) return;
  const i = parseInt(tr.dataset.idx, 10);
  if (!Number.isFinite(i)) return;
  e.dataTransfer.effectAllowed = 'copy';
  const dragTracks = _selected.size > 1 && _selected.has(i)
    ? [..._selected].sort((a, b) => a - b).map(j => currentTracks[j]).filter(Boolean)
    : [currentTracks[i]];
  e.dataTransfer.setData('application/x-soniqboom-track', JSON.stringify(dragTracks));
  tr.classList.add('dragging');
  if (dragTracks.length > 1) {
    tbody.querySelectorAll('tr.multi-selected').forEach(r => r.classList.add('dragging'));
  }
});

tbody.addEventListener('dragend', () => {
  tbody.querySelectorAll('tr.dragging').forEach(r => r.classList.remove('dragging'));
});

// ── Selection bar ──────────────────────────────────────────────────────────────
const selBar   = document.getElementById('selection-bar');
const selCount = document.getElementById('sel-count');

if (document.getElementById('sel-queue')) {
  document.getElementById('sel-queue').addEventListener('click', () => {
    [..._selected].forEach(i => Player.addToQueue(currentTracks[i]));
    _selected.clear(); _vsRender(); _updateSelectionBar();
  });
}
if (document.getElementById('sel-play')) {
  document.getElementById('sel-play').addEventListener('click', () => {
    const sorted = [..._selected].sort((a, b) => a - b);
    Player.setQueue(sorted.map(i => currentTracks[i]), 0);
    _selected.clear(); _vsRender(); _updateSelectionBar();
  });
}
if (document.getElementById('sel-clear')) {
  document.getElementById('sel-clear').addEventListener('click', () => {
    _selected.clear(); _vsRender(); _updateSelectionBar();
  });
}

function _updateSelectionBar() {
  if (!selBar) return;
  const n = _selected.size;
  selBar.hidden = n < 2;
  if (n >= 2 && selCount) selCount.textContent = `${n} tracks selected`;
}

// ── selectRow (single-click visual highlight) ─────────────────────────────────
function selectRow(tr, idx) {
  activeRow?.classList.remove('selected');
  tr.classList.add('selected');
  activeRow = tr;
  // "More like this" — backed by /search/similar (metadata affinity blended
  // with a loudness-contour cosine where waveform data exists for both
  // tracks, i.e. tracks that have been played at least once).
  simBtn.hidden = false;
  simBtn.dataset.id = currentTracks[idx]?.id;
}

function markPlayingRow() {
  tbody.querySelectorAll('tr.playing').forEach(r => r.classList.remove('playing'));
  if (Player.currentTrackId) {
    const row = tbody.querySelector(`tr[data-id="${Player.currentTrackId}"]`);
    if (row) row.classList.add('playing');
  }
}

function playFrom(idx) {
  // For a plain array (small libraries, group views, search results, etc.)
  // the queue IS the full list — Player handles auto-next from the array.
  // For the WindowedTrackStore (large All-Tracks view) we can't pass the
  // 267 000-entry proxy as the queue — Player would iterate it and force
  // every chunk to load.  Instead, slice the currently-loaded contiguous
  // window starting at ``idx`` and queue THAT.  When the user's auto-next
  // reaches the end of that window, the played-track listener can extend
  // the queue further from the store.  This is the same lazy-queue model
  // Spotify / Apple Music use for "Songs" view in large libraries.
  if (currentTracks && currentTracks._isWindowedStore) {
    // 500 lookahead is plenty: at typical track lengths that's 30+ hours
    // of music in the queue.  If the user keeps it playing for that long
    // the ``played`` event handler extends.  We also kick off background
    // chunk loads ahead so auto-next can keep flowing without bursts of
    // skeleton placeholders.
    currentTracks.ensureRange(idx, idx + 500);
    const queue = currentTracks.loadedSliceFrom(idx, 500);
    if (queue.length) Player.setQueue(queue, 0);
    return;
  }
  Player.setQueue(currentTracks, idx);
}

// ── Sort persistence — save/restore sort column + direction ──────────────────
function _saveSortState() {
  if (sortKey) {
    localStorage.setItem('sb_sort_key', sortKey);
    localStorage.setItem('sb_sort_asc', sortAsc ? '1' : '0');
  }
}

function _updateAriaSort(activeTh, asc) {
  document.querySelectorAll('th[data-sort]').forEach(t => {
    if (!t.dataset.sort) return;
    if (t === activeTh) {
      t.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
    } else {
      t.setAttribute('aria-sort', 'none');
    }
  });
}

function _restoreSortState() {
  const key = localStorage.getItem('sb_sort_key');
  const asc = localStorage.getItem('sb_sort_asc');
  // Default state: every sortable header advertises "none" so screen
  // readers don't claim a column is sorted before the user clicks.
  document.querySelectorAll('th[data-sort]').forEach(t => {
    if (t.dataset.sort) t.setAttribute('aria-sort', 'none');
  });
  if (key) {
    sortKey = key;
    sortAsc = asc !== '0';
    // Apply visual + aria indicator to the header
    const th = document.querySelector(`th[data-sort="${sortKey}"]`);
    if (th) {
      th.classList.add('sorted', sortAsc ? 'sorted-asc' : 'sorted-desc');
      _updateAriaSort(th, sortAsc);
    }
  }
}

// ── Sorting ────────────────────────────────────────────────────────────────────
function _compareTrack(a, b, key, asc) {
  if (key === 'track_number') {
    // Sort by disc first, then track number
    const da = a.disc_number ?? 0, db = b.disc_number ?? 0;
    if (da !== db) return asc ? da - db : db - da;
    const ta = a.track_number ?? 0, tb = b.track_number ?? 0;
    return asc ? ta - tb : tb - ta;
  }
  const av = a[key] ?? '', bv = b[key] ?? '';
  return asc ? (av > bv ? 1 : av < bv ? -1 : 0) : (av < bv ? 1 : av > bv ? -1 : 0);
}

// Shared sort-click handler — wired here for the initial header render,
// and re-wired by ``restoreFullHeader()`` after a header-rebuild (group
// view / duplicates view exit).  Hoisted into a function so the two
// attach sites can't drift.
function _onSortHeaderClick(th) {
  const key = th.dataset.sort;
  if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = true; }
  document.querySelectorAll('th[data-sort]').forEach(t => {
    t.classList.remove('sorted', 'sorted-asc', 'sorted-desc');
  });
  th.classList.add('sorted', sortAsc ? 'sorted-asc' : 'sorted-desc');
  _updateAriaSort(th, sortAsc);
  _saveSortState();

  // Windowed mode: the loaded chunks cover ~20K of N rows, so an
  // in-memory sort would lie about positions outside the loaded window.
  // Round-trip to the backend instead, which has pre-computed sorted
  // indexes per column (store.py _SORT_INDEX_MAP).  Keys without a
  // backend index (track_number, path) toast-explain instead of producing
  // a partial sort.
  if (currentTracks && currentTracks._isWindowedStore) {
    if (!WINDOWED_SORT_KEYS.has(key)) {
      if (window.Toast?.info) {
        window.Toast.info(
          `Sort by ${key.replace('_', ' ')} isn't available in the full-library view yet.`,
        );
      }
      return;
    }
    const total = currentTracks._total;
    _rebuildWindowedStore(total, key, sortAsc ? 'asc' : 'desc');
    return;
  }

  // Small-library path: client-side sort over the full in-memory array.
  const sorted = [...currentTracks].sort((a, b) => _compareTrack(a, b, sortKey, sortAsc));
  renderTracks(sorted);
}

document.querySelectorAll('#track-table th[data-sort]').forEach(th => {
  // Skip non-sortable columns (#, ★) — they have data-sort="" purely as a
  // marker, but clicking them should be a no-op and they shouldn't look
  // sortable to assistive tech.
  if (!th.dataset.sort) return;
  th.addEventListener('click', () => _onSortHeaderClick(th));
});

// ── Table header helpers ───────────────────────────────────────────────────────
const trackTableHead = document.querySelector('#track-table thead tr');
const FULL_HEADERS = `
  <th class="col-num">#</th>
  <th class="col-cover" aria-label="Cover"></th>
  <th class="col-title"        data-sort="title"        aria-sort="none">Title</th>
  <th class="col-album-artist" data-sort="album_artist" aria-sort="none">Album Artist</th>
  <th class="col-artist"       data-sort="artist"       aria-sort="none">Artist</th>
  <th class="col-album"        data-sort="album"        aria-sort="none">Album</th>
  <th class="col-track"        data-sort="track_number" aria-sort="none">Track</th>
  <th class="col-year"         data-sort="year"         aria-sort="none">Year</th>
  <th class="col-dur"          data-sort="duration"     aria-sort="none">Duration</th>
  <th class="col-format"       data-sort="format"       aria-sort="none">Type</th>
  <th class="col-location"     data-sort="path"         aria-sort="none">Location</th>
  <th class="col-rating">★</th>`.trim();

function setGroupHeader(label) {
  if (!trackTableHead) return;
  trackTableHead.innerHTML = `<th colspan="12" style="font-weight:600;padding:6px 10px">${esc(label)}</th>`;
}

function restoreFullHeader() {
  _dupViewActive = false;  // leaving duplicates view — restore user column prefs
  if (!trackTableHead) return;
  trackTableHead.innerHTML = FULL_HEADERS;
  // Re-attach sort listeners after rebuilding the header.  Uses the same
  // ``_onSortHeaderClick`` as the initial wire-up — single source of
  // truth so the two attach sites can't drift on what counts as a
  // sortable header click.
  trackTableHead.querySelectorAll('th[data-sort]').forEach(th => {
    if (!th.dataset.sort) return;
    th.addEventListener('click', () => _onSortHeaderClick(th));
  });
  // Restore persisted sort indicator
  _restoreSortState();
  _applyColVisibility();
  _initColResize();
}

// ── Column visibility ──────────────────────────────────────────────────────────
function _applyColVisibility() {
  ALL_COLS.forEach(cls => {
    // In duplicates view, force Location column visible regardless of user prefs
    const hidden = (cls === 'col-location' && _dupViewActive) ? false : _hiddenCols.has(cls);
    document.querySelectorAll(`.${cls}`).forEach(el => {
      el.style.display = hidden ? 'none' : '';
    });
  });
}

// Context menu for column show/hide
const _colMenu = document.createElement('div');
_colMenu.id = 'col-ctx-menu';
_colMenu.style.cssText = 'position:fixed;z-index:999;background:var(--bg3);border:1px solid var(--border-bright);border-radius:8px;padding:6px 0;min-width:160px;display:none;box-shadow:var(--glass-shadow)';
document.body.appendChild(_colMenu);

function _showColMenu(x, y) {
  _colMenu.innerHTML = ALL_COLS.map(cls => `
    <label style="display:flex;align-items:center;gap:8px;padding:5px 14px;cursor:pointer;font-size:12.5px;color:var(--text1);white-space:nowrap">
      <input type="checkbox" data-col="${cls}" ${_hiddenCols.has(cls) ? '' : 'checked'}>
      ${COL_LABELS[cls]}
    </label>`).join('');
  _colMenu.querySelectorAll('input').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) _hiddenCols.delete(cb.dataset.col);
      else _hiddenCols.add(cb.dataset.col);
      localStorage.setItem('sb_hidden_cols', JSON.stringify([..._hiddenCols]));
      _applyColVisibility();
    });
  });
  _colMenu.style.left = x + 'px';
  _colMenu.style.top  = y + 'px';
  _colMenu.style.display = 'block';
}

document.addEventListener('click', () => { _colMenu.style.display = 'none'; });

// Attach right-click to header
if (trackTableHead) {
  trackTableHead.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    _showColMenu(e.clientX, e.clientY);
  });
}

// ── Column resize ──────────────────────────────────────────────────────────────
function _initColResize() {
  // Switch to auto layout so widths work correctly with resize
  document.getElementById('track-table').style.tableLayout = 'auto';

  const ths = trackTableHead.querySelectorAll('th');
  ths.forEach((th, idx) => {
    if (idx === ths.length - 1) return; // skip last col
    // Remove old handle if present
    th.querySelector('.col-resize-handle')?.remove();
    const handle = document.createElement('div');
    handle.className = 'col-resize-handle';
    th.style.position = 'relative';
    th.appendChild(handle);

    let startX = 0, startW = 0;
    handle.addEventListener('mousedown', (e) => {
      startX = e.clientX;
      startW = th.offsetWidth;
      e.preventDefault();
      e.stopPropagation();
      const onMove = (me) => {
        const newW = Math.max(40, startW + me.clientX - startX);
        th.style.width = newW + 'px';
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        _saveColWidths();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
  _restoreColWidths();
}

function _saveColWidths() {
  const ths = trackTableHead.querySelectorAll('th');
  const widths = [...ths].map(th => th.style.width || '');
  localStorage.setItem('sb_col_widths', JSON.stringify(widths));
}

function _restoreColWidths() {
  try {
    const saved = JSON.parse(localStorage.getItem('sb_col_widths') || '[]');
    const ths = trackTableHead.querySelectorAll('th');
    saved.forEach((w, i) => { if (ths[i] && w) ths[i].style.width = w; });
  } catch (_) {}
}

// Initialize resize on module load
_initColResize();

// ── Views ──────────────────────────────────────────────────────────────────────
function _hideGroupFilter() {
  browseFilterWrap.hidden = true;
  browseFilter.value = '';
  if (groupFilterBar) groupFilterBar.hidden = true;
  if (groupFilterInput) groupFilterInput.value = '';
}

// Sort keys the backend has a pre-computed index for — keys outside this
// set use legacy in-memory sort (which only works in non-windowed mode).
// Mirrors api/tracks.py _ALLOWED_SORT_KEYS exactly; if they drift the API
// silently falls back to default order, but the windowed UX will still
// behave correctly because the spinner shows for ANY remote re-fetch.
const WINDOWED_SORT_KEYS = new Set([
  'title', 'artist', 'album_artist', 'album',
  'year', 'duration', 'format',
]);

async function showAll() {
  hideBrowseHeader();
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  _showSkeletonRows();
  _windowedFilter = null;          // plain All-Tracks view (no format filter)
  const limit = 5000;
  // Probe the real total first.  Cheap (one count() in the store) and
  // tells us whether to use the simple-array path (small libraries) or
  // the windowed-store path (large libraries) without round-tripping
  // 5000 rows we may not even render.
  let total = 0;
  try {
    const { count } = await API('/tracks/count');
    total = Number(count) || 0;
  } catch { /* fall through to legacy path */ }

  if (total > limit) {
    // Pick up persisted sort state so the windowed view comes back the
    // same way the user left it.  ``sortKey`` and ``sortAsc`` are the
    // module-level state already restored by ``_restoreSortState()``
    // (called inside restoreFullHeader above).  Only keys the backend
    // can drive get honoured — others would silently fall back to
    // default order in the API; better to skip the round-trip and stay
    // on the default explicitly.
    const sortBy    = (sortKey && WINDOWED_SORT_KEYS.has(sortKey)) ? sortKey : null;
    const sortOrder = sortBy ? (sortAsc ? 'asc' : 'desc') : null;
    _rebuildWindowedStore(total, sortBy, sortOrder);
    _updateNavBadge('all', total);  // exact total, no "+" needed
    return;
  }

  // Small-library path (≤ 5000): legacy single-fetch array — simpler,
  // no chunking overhead, no skeleton rows for unloaded slots.
  const tracks = await API('/tracks', { limit });
  _showAddFolderCta = (total === 0);   // genuinely-empty library → offer the add-folder CTA
  renderTracks(tracks);
  const truncated = tracks.length >= limit;
  _updateNavBadge('all', tracks.length, truncated);
  if (truncated) _refreshTrackCount();
}

// Build (or rebuild) the windowed store for the All Tracks view.  Sort
// re-application calls this with new sort params so the chunked fetcher
// requests pre-sorted pages from the backend instead of trying to sort the
// partial in-memory window (which would only sort the ~20K loaded rows,
// not all 267K).  Track-list container is scrolled to top so the user sees
// the fresh ordering from the start, and selection is cleared because the
// previous selection indexes refer to the old ordering.
function _rebuildWindowedStore(total, sortBy, sortOrder) {
  const store = createWindowedStore(
    total,
    (offset, lim) => {
      const params = { limit: lim, offset };
      if (sortBy)    params.sort  = sortBy;
      if (sortOrder) params.order = sortOrder;
      if (_windowedFilter) Object.assign(params, _windowedFilter);  // e.g. {format}
      return API('/tracks', params);
    },
    { sortBy, sortOrder },
  );
  // When a chunk lands, re-paint the visible window so the skeleton
  // rows we showed get replaced with real data.  ``_vsRender(true)``
  // forces the render even though _vsStart/_vsEnd didn't move.
  store.setOnChunkLoad(() => _vsRender(true));
  // Reset scroll + selection — the indexes the user was looking at no
  // longer point at the same tracks.
  _selected.clear();
  _lastClickIdx = -1;
  _focusedIdx = -1;
  const wrap = document.getElementById('track-list-wrap');
  if (wrap) wrap.scrollTop = 0;
  renderTracks(store);
}

async function showArtists() {
  hideBrowseHeader();
  setGroupHeader('Artist');
  _hideGridToggle();
  _showSkeletonRows(12);
  const artists = await API('/library/artists');
  _updateNavBadge('artists', artists.length);
  renderGroupList(artists, 'artist', 'count', 'Albums', (item) => {
    if (item.label || !item.artist) {
      showUntaggedTracks('artist', '[No Artist]', () => showArtists());
    } else {
      showAlbums(item.artist, null, 'artist');
    }
  });
}

async function showAlbumArtists() {
  hideBrowseHeader();
  setGroupHeader('Album Artist');
  _hideGridToggle();
  _showSkeletonRows(12);
  const albumArtists = await API('/library/album-artists');
  _updateNavBadge('album_artists', albumArtists.length);
  renderGroupList(albumArtists, 'album_artist', 'count', 'Albums', (item) => {
    if (item.label || !item.album_artist) {
      // "[No Album Artist]" — show all untagged tracks directly
      showUntaggedTracks('album_artist', '[No Album Artist]', () => showAlbumArtists());
    } else {
      showAlbums(null, item.album_artist, 'album_artist');
    }
  });
}

async function showAlbums(artist = null, albumArtist = null, backView = null) {
  const params = {};
  if (artist) params.artist = artist;
  if (albumArtist) params.album_artist = albumArtist;
  setGroupHeader('Album');
  _showSkeletonRows(12);
  const albums = await API('/library/albums', params);
  if (!artist && !albumArtist) _updateNavBadge('albums', albums.length);
  const backLabel = artist || albumArtist || null;
  const backFn = backView === 'album_artist'
    ? () => showAlbumArtists()
    : backView === 'artist'
      ? () => showArtists()
      : null;
  if (backLabel && backFn) {
    setBrowseHeader(backLabel, backFn);
  } else {
    hideBrowseHeader();
  }
  renderGroupList(albums, 'album', 'count', 'Tracks', (item) => {
    const backTo = () => showAlbums(artist, albumArtist, backView);
    if (item.label || !item.album) {
      // "[No Album]" — show tracks without album metadata
      showUntaggedTracks('album', '[No Album]', backTo);
    } else {
      showAlbumTracks(item.artist, item.album_artist, item.album, backTo);
    }
  });
}

async function showUntaggedTracks(field, label, backFn) {
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  setBrowseHeader(label, backFn);
  _showSkeletonRows();
  // Fetch a large batch and filter client-side (tag index can't query empty fields)
  const all = await API('/tracks', { limit: 5000 });
  const filtered = all.filter(t => !t[field] || !t[field].toString().trim());
  renderTracks(filtered);
}

async function showAlbumTracks(artist, albumArtist, album, backFn) {
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  const label = albumArtist ? `${albumArtist} — ${album}`
    : artist ? `${artist} — ${album}`
    : album;
  setBrowseHeader(label, backFn || (() => showAlbums()));
  _showSkeletonRows();
  // Build filter params — omit empty/null values so the API isn't confused
  const params = { limit: 500 };
  if (albumArtist) params.album_artist = albumArtist;
  else if (artist) params.artist = artist;
  if (album) params.album = album;
  const tracks = await API('/search/filter', params);
  // Sort by disc number, then track number (album track order)
  tracks.sort((a, b) => {
    const da = a.disc_number ?? 0, db = b.disc_number ?? 0;
    if (da !== db) return da - db;
    const ta = a.track_number ?? 0, tb = b.track_number ?? 0;
    return ta - tb;
  });
  renderTracks(tracks);
}

async function showGenres() {
  hideBrowseHeader();
  setGroupHeader('Genre');
  _hideGridToggle();
  _showSkeletonRows(12);
  const genres = await API('/library/genres');
  _updateNavBadge('genres', genres.length);
  renderGroupList(genres, 'genre', 'count', 'Tracks', (item) => showGenreTracks(item.genre));
}

async function showGenreTracks(genre) {
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  setBrowseHeader(`Genre: ${genre}`, () => showGenres());
  _showSkeletonRows();
  const tracks = await API('/search/filter', { genre, limit: 500 });
  renderTracks(tracks);
}

async function showYears() {
  hideBrowseHeader();
  setGroupHeader('Year');
  _hideGridToggle();
  _showSkeletonRows(12);
  const years = await API('/library/years');
  _updateNavBadge('years', years.length);
  renderGroupList(years, 'year', 'count', 'Tracks', (item) => showYearTracks(item.year));
}

// ── Library Galaxy view (viz #6) ──────────────────────────────────────────
let _galaxy = null;
function _leaveGalaxy() {
  const view = document.getElementById('galaxy-view');
  const wrap = document.getElementById('track-list-wrap');
  if (view && !view.hidden) view.hidden = true;
  if (wrap) wrap.style.display = '';
}
async function showGalaxy() {
  hideBrowseHeader();
  _hideGroupFilter();
  _hideGridToggle();
  setGroupHeader('Galaxy');
  // Hide the table surfaces, reveal the galaxy canvas host.
  const wrap = document.getElementById('track-list-wrap');
  const grid = document.getElementById('album-grid');
  const view = document.getElementById('galaxy-view');
  if (wrap) wrap.style.display = 'none';
  if (grid) grid.hidden = true;
  if (view) view.hidden = false;
  // Lazy-mount; clicking a cluster filters the library to that format.
  if (!_galaxy && view) {
    const { mountGalaxy } = await import('./viz/galaxy.js');
    _galaxy = mountGalaxy(view, {
      onPickFormat: (fmt, count) => showFormatTracks(fmt, count),
    });
  } else if (_galaxy) {
    _galaxy.reload();
  }
}
async function showFormatTracks(format, count = 0) {
  restoreFullHeader();
  setBrowseHeader(`Format: ${format}`, () => showGalaxy());
  _hideGridToggle();
  _showSkeletonRows();
  // Same windowed virtual-scroll path as All Tracks, just filtered by format —
  // so even the giant formats (ProTracker 62K, SID 57K, …) are fully
  // browsable, not capped.  The chunked fetcher reads _windowedFilter.
  _windowedFilter = { format };
  const WINDOW_THRESHOLD = 5000;
  // Prefer the count the Galaxy chip already showed; probe once if absent.
  let total = Number(count) || 0;
  if (!total) {
    try {
      const fmts = await API('/library/formats');
      const hit = Array.isArray(fmts) ? fmts.find(f => f.format === format) : null;
      total = hit ? Number(hit.count) || 0 : 0;
    } catch { /* fall through to single-fetch */ }
  }
  if (total > WINDOW_THRESHOLD) {
    const sortBy    = (sortKey && WINDOWED_SORT_KEYS.has(sortKey)) ? sortKey : null;
    const sortOrder = sortBy ? (sortAsc ? 'asc' : 'desc') : null;
    _rebuildWindowedStore(total, sortBy, sortOrder);   // renders the windowed store
    return;
  }
  // Small format: a single format-filtered fetch (no chunking overhead).
  const tracks = await API('/tracks', { format, limit: WINDOW_THRESHOLD });
  renderTracks(tracks);
}

async function showYearTracks(year) {
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  setBrowseHeader(`Year: ${year}`, () => showYears());
  _showSkeletonRows();
  const tracks = await API('/search/filter', { year_min: year, year_max: year, limit: 500 });
  renderTracks(tracks);
}

// ── Grid toggle helpers ────────────────────────────────────────────────────────
function _showGridToggle() {
  const btn = document.getElementById('btn-grid-toggle');
  if (btn) btn.hidden = false;
}
function _hideGridToggle() {
  const btn = document.getElementById('btn-grid-toggle');
  if (btn) {
    btn.hidden = true;
    _gridView = false;
    btn.textContent = '⊞';
    btn.classList.remove('active');
  }
}

// ── Album grid rendering ───────────────────────────────────────────────────────
let _albumArtObserver = null;

function _loadAlbumCardArt(card, trackId) {
  const artEl = card.querySelector('.album-card-art');
  if (!artEl) return;
  const img = new Image();
  img.decoding = 'async';
  img.onload = async () => {
    // Decode off the main thread when the API is available —
    // ``backgroundImage`` commits paint atomically once the
    // decode promise resolves, so we never paint a half-decoded
    // bitmap during the card's scroll-in.
    try {
      if (typeof img.decode === 'function') await img.decode();
    } catch (_) { /* fallback to onload-only timing */ }
    artEl.style.backgroundImage = `url("${img.src}")`;
    artEl.style.backgroundSize = 'cover';
    artEl.style.backgroundPosition = 'center';
    const initialsEl = artEl.querySelector('.album-card-initials');
    if (initialsEl) initialsEl.style.display = 'none';
    card.classList.add('album-card-art-loaded');
  };
  // fallback=404 so art-less albums keep their letter initials instead
  // of the generic ♪ placeholder JPEG overwriting them as a background.
  img.src = `/api/art/${encodeURIComponent(trackId)}?size=sm&fallback=404`;
}

function _renderAlbumGrid(items, nameKey, countKey, countLabel, onClick) {
  const albumGrid = document.getElementById('album-grid');
  if (!albumGrid) return;

  albumGrid.innerHTML = '';
  albumGrid.hidden = false;
  document.getElementById('track-table').style.display = 'none';

  // Disconnect previous observer
  if (_albumArtObserver) _albumArtObserver.disconnect();

  // Create IntersectionObserver for lazy-loading album art
  _albumArtObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const card = entry.target;
      if (card.classList.contains('album-card-art-loaded')) return;
      const album  = card.dataset.album;
      if (!album) return;

      // Aggregation rows carry a representative ``track_id``, so the art
      // URL is known without a per-card /search/filter round-trip.  Cards
      // built from rows without one (group views that aren't albums) fall
      // back to the lookup.
      if (card.dataset.trackId) {
        _loadAlbumCardArt(card, card.dataset.trackId);
      } else {
        const artist = card.dataset.artist;
        const params = new URLSearchParams({ album, limit: '1' });
        if (artist) params.set('artist', artist);
        fetch(`/api/search/filter?${params}`)
          .then(r => r.json())
          .then(tracks => { if (tracks.length) _loadAlbumCardArt(card, tracks[0].id); })
          .catch(() => {});
      }

      _albumArtObserver.unobserve(card);
    });
  }, { rootMargin: '100px' });

  items.forEach(item => {
    const name = item.label || item[nameKey] || '—';
    const count = item[countKey] || 0;
    const artist = item.artist || item.album_artist || '';
    // Generate initials from first 2 words
    const words = name.replace(/[^a-zA-Z0-9 ]/g, '').trim().split(/\s+/);
    const initials = words.length >= 2
      ? (words[0][0] + words[words.length - 1][0]).toUpperCase()
      : name.substring(0, 2).toUpperCase();

    const card = document.createElement('div');
    card.className = 'album-card';
    card.dataset.name = name;
    card.dataset.album = name;
    card.dataset.artist = artist;
    if (item.track_id) card.dataset.trackId = item.track_id;
    // Keyboard activation — cards behave like buttons (Enter/Space activates).
    // The `keyboard-focus` class is added on keyboard-induced focus so CSS
    // can render a focus-visible ring without lighting up on every click.
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `${name}, ${count} ${countLabel}`);
    card.innerHTML = `
      <div class="album-card-art">
        <span class="album-card-initials">${esc(initials)}</span>
      </div>
      <div class="album-card-info">
        <div class="album-card-title" title="${esc(name)}">${esc(name)}</div>
        <div class="album-card-sub">${count} ${countLabel}</div>
      </div>`;
    card.addEventListener('click', () => onClick(item));
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onClick(item);
      }
    });
    // Show focus ring only for keyboard-driven focus (not mouse clicks).
    card.addEventListener('focus', () => {
      // :focus-visible may not be supported by ancient browsers — wrap in
      // try/catch so the focus handler never throws.
      try {
        if (card.matches(':focus-visible')) card.classList.add('keyboard-focus');
      } catch (_) { /* selector unsupported — skip the ring */ }
    });
    card.addEventListener('blur', () => card.classList.remove('keyboard-focus'));
    albumGrid.appendChild(card);

    // Observe for lazy-load
    _albumArtObserver.observe(card);
  });
}

// Renders a grouped list (artists/albums/genres/years) into the table
function renderGroupList(items, nameKey, countKey, countLabel, onClick, showFilter = true) {
  _leaveGalaxy();          // switching to a group/list view exits the galaxy
  // Save state for live filtering
  _groupItems    = items;
  _groupNameKey  = nameKey;
  _groupCountKey = countKey;
  _groupLabel    = countLabel;
  _groupOnClick  = onClick;

  currentTracks = [];
  loadingEl.hidden = true;
  emptyEl.hidden = items.length > 0;

  // Show grid toggle only for album views
  if (nameKey === 'album') {
    _showGridToggle();
  } else {
    _hideGridToggle();
  }

  // Show/hide the browse filter boxes (sidebar + content area)
  if (showFilter && items.length > 0) {
    // Sidebar filter (legacy, keep for compatibility)
    browseFilterWrap.hidden = false;
    browseFilter.value = '';
    // Content-area filter bar (more prominent)
    if (groupFilterBar) {
      const viewLabel = nameKey === 'artist' ? 'Artists'
        : nameKey === 'album_artist' ? 'Album Artists'
        : nameKey === 'album' ? 'Albums'
        : nameKey === 'genre' ? 'Genres'
        : nameKey === 'year' ? 'Years'
        : nameKey.charAt(0).toUpperCase() + nameKey.slice(1);
      groupFilterLabel.textContent = `${items.length} ${viewLabel}`;
      groupFilterInput.value = '';
      groupFilterInput.placeholder = `Filter ${viewLabel.toLowerCase()}…`;
      groupFilterBar.hidden = false;
      // Sync: content filter drives both
      groupFilterInput.oninput = () => {
        browseFilter.value = groupFilterInput.value;
        browseFilter.dispatchEvent(new Event('input'));
      };
      // ``preventScroll`` keeps the freshly-opened group view from
      // jumping if the filter input was previously below the viewport.
      setTimeout(() => groupFilterInput.focus({ preventScroll: true }), 50);
    }
  } else {
    browseFilterWrap.hidden = true;
    if (groupFilterBar) groupFilterBar.hidden = true;
  }

  if (_gridView && nameKey === 'album') {
    // Ensure table is hidden, grid is shown
    document.getElementById('track-table').style.display = 'none';
    tbody.innerHTML = '';
    _renderAlbumGrid(items, nameKey, countKey, countLabel, onClick);
  } else {
    // Ensure grid is hidden, table is shown
    const albumGrid = document.getElementById('album-grid');
    if (albumGrid) albumGrid.hidden = true;
    document.getElementById('track-table').style.display = '';
    _renderGroupRows(items, nameKey, countKey, countLabel, onClick);
  }
}

let _groupRenderGen = 0;

function _renderGroupRows(items, nameKey, countKey, countLabel, onClick) {
  // Group rows replace the tbody — drop any pool nodes that were here.
  _vsResetPool();
  tbody.innerHTML = '';
  emptyEl.hidden = items.length > 0;
  // Chunked render: the first screenful paints synchronously (instant
  // perceived response on any library size), the rest streams in between
  // frames. A generation token cancels stale batches when the user
  // navigates away mid-stream.
  const gen = ++_groupRenderGen;
  const FIRST = 150, BATCH = 800;
  const build = (item) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="col-num"></td>
      <td class="col-cover"></td>
      <td class="col-title" colspan="7" style="font-weight:500;${item.label ? 'font-style:italic;color:var(--text2)' : ''}">${esc(item.label || item[nameKey] || '—')}</td>
      <td class="col-dur" style="color:var(--text2)">${item[countKey]} ${countLabel}</td>
      <td class="col-rating"></td>`;
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => onClick(item));
    return tr;
  };
  const frag = document.createDocumentFragment();
  items.slice(0, FIRST).forEach(item => frag.appendChild(build(item)));
  tbody.appendChild(frag);
  let pos = Math.min(FIRST, items.length);
  const more = () => {
    if (gen !== _groupRenderGen || pos >= items.length) return;
    const f = document.createDocumentFragment();
    items.slice(pos, pos + BATCH).forEach(item => f.appendChild(build(item)));
    tbody.appendChild(f);
    pos += BATCH;
    if (pos < items.length) requestAnimationFrame(more);
  };
  if (pos < items.length) requestAnimationFrame(more);
}

// ── Grid toggle button handler ─────────────────────────────────────────────────
const _gridToggleBtn = document.getElementById('btn-grid-toggle');
if (_gridToggleBtn) {
  _gridToggleBtn.addEventListener('click', () => {
    _gridView = !_gridView;
    _gridToggleBtn.textContent = _gridView ? '☰' : '⊞';
    _gridToggleBtn.classList.toggle('active', _gridView);
    // Re-render current group list
    if (_groupItems.length && _groupOnClick) {
      if (_gridView && _groupNameKey === 'album') {
        document.getElementById('track-table').style.display = 'none';
        tbody.innerHTML = '';
        _renderAlbumGrid(_groupItems, _groupNameKey, _groupCountKey, _groupLabel, _groupOnClick);
      } else {
        const albumGrid = document.getElementById('album-grid');
        if (albumGrid) albumGrid.hidden = true;
        document.getElementById('track-table').style.display = '';
        _renderGroupRows(_groupItems, _groupNameKey, _groupCountKey, _groupLabel, _groupOnClick);
      }
    }
  });
}

// ── Browse filter (live filtering of group lists) ──────────────────────────────
// Debounced: each render is a full teardown+rebuild of the visible rows, so
// firing it per keystroke makes fast typing jank on big group lists (~6k
// artists).  150 ms matches the quick-search debounce — under the threshold
// where the pause itself reads as lag.
let _browseFilterTimer = null;
browseFilter.addEventListener('input', () => {
  clearTimeout(_browseFilterTimer);
  _browseFilterTimer = setTimeout(() => {
    const q = browseFilter.value.trim().toLowerCase();
    if (!_groupItems.length || !_groupOnClick) return;
    const filtered = q
      ? _groupItems.filter(item => String(item.label || item[_groupNameKey] || '').toLowerCase().includes(q))
      : _groupItems;
    if (_gridView && _groupNameKey === 'album') {
      _renderAlbumGrid(filtered, _groupNameKey, _groupCountKey, _groupLabel, _groupOnClick);
    } else {
      _renderGroupRows(filtered, _groupNameKey, _groupCountKey, _groupLabel, _groupOnClick);
    }
  }, 150);
});

browseFilter.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    browseFilter.value = '';
    browseFilter.dispatchEvent(new Event('input'));
  }
});

// ── Browse header helpers ─────────────────────────────────────────────────────
function setBrowseHeader(label, backFn) {
  browseHdr.hidden = false;
  browseCrumb.textContent = label;
  browseBack.onclick = backFn;
  _hideExportBtn();
}
function hideBrowseHeader() { browseHdr.hidden = true; _dupViewActive = false; _hideExportBtn(); _currentBrowsePath = null; }

// Export CSV button — injected into browse-header when a view supports it
function _showExportBtn(onClick) {
  _hideExportBtn();
  const btn = document.createElement('button');
  btn.id = 'btn-export-csv';
  btn.className = 'btn-export-csv';
  btn.textContent = 'Export CSV';
  btn.addEventListener('click', onClick);
  browseHdr.insertBefore(btn, browseBack);
}
function _hideExportBtn() {
  const existing = document.getElementById('btn-export-csv');
  if (existing) existing.remove();
}

// ── Panel mutual exclusivity ─────────────────────────────────────────────────
// Any right-side panel fires 'panelopen' when it opens; all others auto-close.
function _firePanelOpen(name) {
  document.dispatchEvent(new CustomEvent('panelopen', { detail: { panel: name } }));
}

function _closeSimilar() {
  if (simPanel && !simPanel.hidden) simPanel.hidden = true;
}
function _closeLyrics() {
  if (!lyricsPanel.hidden) {
    lyricsPanel.hidden = true;
    lyricsBtn.classList.remove('on');
    _stopLyricsSync();
  }
}

// Listen for other panels opening — close ours
document.addEventListener('panelopen', (e) => {
  const who = e.detail?.panel;
  if (who !== 'similar')  _closeSimilar();
  if (who !== 'lyrics')   _closeLyrics();
});

// ── Similar tracks ─────────────────────────────────────────────────────────────
async function showSimilar(trackId) {
  _firePanelOpen('similar');
  simPanel.hidden = false;
  simList.innerHTML = '<li style="color:var(--text2);padding:8px 12px">Loading…</li>';
  try {
    const results = await API(`/search/similar/${trackId}`, { k: 10 });
    simList.innerHTML = '';
    results.forEach(({ track: t, score }) => {
      const li = document.createElement('li');
      li.innerHTML = `
        <span class="sim-score">${(score * 100).toFixed(0)}%</span>
        <div class="sim-title">${esc(t.title)}</div>
        <div class="sim-artist">${esc(t.artist)}</div>`;
      li.addEventListener('dblclick', () => Player.playTrack(t));
      li.addEventListener('click', () => {
        const idx = currentTracks.findIndex(x => x.id === t.id);
        if (idx >= 0) playFrom(idx); else Player.playTrack(t);
      });
      simList.appendChild(li);
    });
  } catch {
    simList.innerHTML = '<li style="color:var(--text2);padding:8px 12px">Semantic search not available.</li>';
  }
}

simBtn.addEventListener('click', () => {
  const id = simBtn.dataset.id;
  if (!id) return;
  if (simPanel.hidden) showSimilar(id); else simPanel.hidden = true;
});

// Update playing highlight when track changes
Player.on('trackchange', () => markPlayingRow());

// ── Lyrics panel ──────────────────────────────────────────────────────────────

const lyricsBtn     = document.getElementById('btn-lyrics');
const lyricsPanel   = document.getElementById('lyrics-panel');
const lyricsBody    = document.getElementById('lyrics-body');
const lyricsContent = document.getElementById('lyrics-content');
const lyricsSource  = document.getElementById('lyrics-source');

// ── Lyrics panel resize handle ───────────────────────────────────────────────
{
  const handle = document.getElementById('lyrics-resize');
  if (handle && lyricsPanel) {
    let startX, startW;
    handle.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      startX = e.clientX;
      startW = lyricsPanel.offsetWidth;
      handle.classList.add('dragging');
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener('pointermove', (e) => {
      if (!handle.hasPointerCapture(e.pointerId)) return;
      // Dragging left = wider panel (panel is on the right side)
      const w = Math.max(200, Math.min(700, startW - (e.clientX - startX)));
      lyricsPanel.style.width = w + 'px';
    });
    handle.addEventListener('pointerup', (e) => {
      handle.classList.remove('dragging');
      handle.releasePointerCapture(e.pointerId);
      localStorage.setItem('sb_lyrics_w', lyricsPanel.style.width);
    });
    // Restore saved width
    const saved = localStorage.getItem('sb_lyrics_w');
    if (saved) lyricsPanel.style.width = saved;
  }
}

// Formats that never have lyrics (rendered/converted, no metadata tags)
const _NO_LYRICS_FMTS = new Set([
  'SID', 'PSID', 'MOD', 'S3M', 'XM', 'IT', 'MTM', 'MED', 'OCT',
  '669', 'DBM', 'AHX', 'HVL', 'ULT', 'STM', 'FAR', 'AMF', 'GDM',
  'IMF', 'OKT', 'SFX', 'WOW', 'DSM', 'MID', 'MIDI',
]);

let _lyricsTrackId   = null;   // track ID we fetched lyrics for
let _lyricsLines     = null;   // array of { time: seconds|null, text }
let _lyricsSynced    = false;  // true if timestamps available

/**
 * Parse LRC format into structured lines.
 * Input: "[00:12.34] Some lyrics line\n[00:16.00] Next line"
 * Returns: [{ time: 12.34, text: "Some lyrics line" }, ...]
 */
function _parseLRC(raw) {
  const lines = [];
  const re = /^\[(\d{1,2}):(\d{2})[.:](\d{2,3})\]\s*(.*)/;
  for (const line of raw.split('\n')) {
    const m = line.match(re);
    if (m) {
      const mins = parseInt(m[1], 10);
      const secs = parseInt(m[2], 10);
      const ms   = m[3].length === 2 ? parseInt(m[3], 10) * 10 : parseInt(m[3], 10);
      lines.push({ time: mins * 60 + secs + ms / 1000, text: m[4] });
    } else if (line.trim()) {
      lines.push({ time: null, text: line.trim() });
    }
  }
  return lines;
}

/**
 * Render lyrics lines into the panel DOM.
 */
function _renderLyrics(lines, synced) {
  lyricsContent.innerHTML = '';
  _lyricsLines  = lines;
  _lyricsSynced = synced;

  if (!lines || lines.length === 0) {
    lyricsContent.innerHTML = '<div class="lyrics-empty">No lyrics available</div>';
    return;
  }

  lines.forEach((ln, i) => {
    const div = document.createElement('div');
    div.className = 'lyrics-line' + (synced && ln.time !== null ? ' synced' : '');
    div.textContent = ln.text || '';
    div.dataset.idx = i;
    if (synced && ln.time !== null) {
      div.addEventListener('click', () => {
        const track = Player.currentTrack;
        if (track && track.duration > 0) {
          Player.seek((ln.time / track.duration) * 100);
        }
      });
    }
    lyricsContent.appendChild(div);
  });

  if (synced) _startLyricsSync();
}

/**
 * Lyrics sync — driven by Player's timeupdate event (fires ~4×/sec).
 * More reliable than rAF because it uses the exact same time values
 * that drive the seek bar and time display.
 *
 * Player.on() has no off(), so we register ONE persistent listener
 * and gate it with _lyricsSyncActive.
 */
let _lyricsSyncLastActive = -1;
let _lyricsSyncActive     = false;

// Single persistent listener — registered once, gated by _lyricsSyncActive
Player.on('timeupdate', ({ current }) => {
  if (!_lyricsSyncActive || !_lyricsLines || !_lyricsSynced) return;

  const t = current;
  if (typeof t !== 'number' || isNaN(t)) return;

  // Find the active line: last line whose time <= current playback position
  let active = -1;
  for (let i = 0; i < _lyricsLines.length; i++) {
    if (_lyricsLines[i].time !== null && _lyricsLines[i].time <= t) active = i;
  }
  if (active === _lyricsSyncLastActive) return;
  const prevActive = _lyricsSyncLastActive;
  _lyricsSyncLastActive = active;

  const lineEls = lyricsContent.querySelectorAll('.lyrics-line');
  for (let i = 0; i < lineEls.length; i++) {
    const el = lineEls[i];
    const ln = _lyricsLines[i];
    if (!ln) continue;
    el.classList.toggle('active', i === active);
    el.classList.toggle('past', ln.time !== null && i < active);
  }

  // Scroll active line into view (centered).  Smooth scroll for big jumps
  // (seek / scrub) where the animation provides spatial context; instant
  // scroll for line-by-line progression so the highlight doesn't visibly
  // trail the audio on fast verses.
  if (active >= 0 && lineEls[active]) {
    const bigJump = prevActive < 0 || Math.abs(active - prevActive) >= 3;
    lineEls[active].scrollIntoView({
      block: 'center',
      behavior: bigJump ? 'smooth' : 'instant',
    });
  }
});

function _startLyricsSync() {
  _lyricsSyncLastActive = -1;
  _lyricsSyncActive = true;
}

function _stopLyricsSync() {
  _lyricsSyncActive = false;
  _lyricsSyncLastActive = -1;
}

/**
 * Fetch and display lyrics for the given track.
 */
async function _loadLyrics(track) {
  if (!track) return;
  _lyricsTrackId = track.id;
  lyricsContent.innerHTML = '<div class="lyrics-loading">Loading lyrics...</div>';
  lyricsSource.textContent = '';
  _stopLyricsSync();

  try {
    const res = await fetch(`/api/tracks/${track.id}/lyrics`);
    if (!res.ok) throw new Error('Not found');
    const data = await res.json();

    // Track changed while we were fetching
    if (_lyricsTrackId !== track.id) return;

    if (!data.lyrics) {
      lyricsContent.innerHTML = '<div class="lyrics-empty">No lyrics found</div>';
      lyricsSource.textContent = '';
      return;
    }

    lyricsSource.textContent = data.source || '';
    const lines = data.synced ? _parseLRC(data.lyrics) : _parsePlain(data.lyrics);
    _renderLyrics(lines, data.synced);
  } catch {
    if (_lyricsTrackId === track.id) {
      lyricsContent.innerHTML = '<div class="lyrics-empty">Could not load lyrics</div>';
    }
  }
}

function _parsePlain(text) {
  return text.split('\n').map(line => ({ time: null, text: line }));
}

/**
 * Update the lyrics button state based on the current track's format.
 */
function _updateLyricsBtn(track) {
  if (!track) {
    lyricsBtn.classList.add('disabled');
    lyricsBtn.title = 'No track playing';
    return;
  }
  const fmt = (track.format || '').toUpperCase();
  if (_NO_LYRICS_FMTS.has(fmt)) {
    lyricsBtn.classList.add('disabled');
    lyricsBtn.title = 'No lyrics';
    // Close the panel if it was open
    if (!lyricsPanel.hidden) {
      lyricsPanel.hidden = true;
      lyricsBtn.classList.remove('on');
      _stopLyricsSync();
    }
  } else {
    lyricsBtn.classList.remove('disabled');
    lyricsBtn.title = 'Lyrics';
  }
}

// Button click toggle
lyricsBtn.addEventListener('click', () => {
  if (lyricsBtn.classList.contains('disabled')) return;
  const track = Player.currentTrack;
  if (!track) return;

  if (lyricsPanel.hidden) {
    _firePanelOpen('lyrics');          // closes queue, playlist, similar
    lyricsPanel.hidden = false;
    lyricsBtn.classList.add('on');
    if (_lyricsTrackId !== track.id) _loadLyrics(track);
  } else {
    lyricsPanel.hidden = true;
    lyricsBtn.classList.remove('on');
    _stopLyricsSync();
  }
});

// When track changes, update button state and reload lyrics if panel is open.
// Player.emit already isolates listener exceptions, but historically a stray
// throw here broke the trackchange chain for downstream subscribers — guard
// internally so a fetch/render glitch can't poison the event.
Player.on('trackchange', (track) => {
  try {
    _updateLyricsBtn(track);
    if (!lyricsPanel.hidden) {
      _loadLyrics(track);
    } else {
      _lyricsTrackId = null;
      _stopLyricsSync();
    }
  } catch (err) {
    console.warn('Lyrics trackchange handler failed:', err);
  }
});

async function showFolder(path, recursive = false, opts = {}) {
  _currentBrowsePath = path;
  _currentBrowseRecursive = recursive;
  _hideGridToggle();
  // ``opts.quiet`` = an in-place freshness refresh (not a user navigation):
  // skip the skeleton flash and don't re-schedule another background scan.
  if (!opts.quiet) _showSkeletonRows();

  setBrowseHeader(path.split('/').filter(Boolean).pop() || path, () => {
    hideBrowseHeader();
    document.querySelectorAll('#nav-library li')[0]?.click();
  });

  // Drill-down freshness: kick a debounced background scan of THIS folder
  // (POST /api/fstree/refresh).  The view still renders instantly from the
  // store first; when the scan finds new/removed files it broadcasts a
  // scan_progress completion carrying ``last_dirs``, and the WS handler then
  // refreshes THIS folder in place (see app.js + refreshCurrentFolderInPlace).
  //
  // This was previously removed (VU-D19) because the per-click scans wrote to
  // the store, bumped the global ``_mutation_seq``, and flushed the metadata
  // cache for EVERY folder — queue depth + cache thrash.  That root cause is
  // GONE: the folder-browse caches now key on the directory's own mtime /
  // scan-root bucket size, not ``_mutation_seq``.  Re-enabling is also
  // necessary, not just nice: on SMB/NFS network mounts the FS watcher is
  // silently deaf (FSEvents doesn't fire for network volumes), so a folder
  // browse is the only signal that a subtree may have changed.  The helper
  // debounces 400 ms and skips a path it scanned in the last 30 s, and
  // ``start_scan`` dedups overlapping queued subtrees, so rapid nav is cheap.
  // The actual call is made BELOW, once we know the folder's size — so we skip
  // huge archive subtrees (FRESHNESS_MAX_TRACKS) and never re-fire from an
  // in-place refresh (opts.quiet).

  try {
    // Recursive request: skip the shallow round-trip and go straight to
    // the windowed-fetch path.  Used both by branch-folder auto-flatten
    // (fallthrough below) and by any caller that explicitly wants the
    // full subtree.
    if (recursive) {
      await _showFolderRecursiveWindowed(path, opts);
      return;
    }
    // Non-recursive: probe with a WINDOWED first chunk.  The backend serves
    // this store-first (no per-click SMB ``os.scandir``) and paginates it, so
    // a folder that flattens thousands of archive tracks straight into it
    // (e.g. ``modarchive_2007/E``) no longer ships its whole listing as one
    // un-windowed array — it lands the first 2 000 instantly and streams the
    // rest as the user scrolls.
    const enc = encodeURIComponent(path);
    let first;
    try {
      first = await fetch(
        `/api/fstree/tracks-with-meta?path=${enc}&recursive=false` +
        `&offset=0&limit=${CHUNK_SIZE}&filter_duplicates=true`,
      ).then(r => r.json());
    } catch {
      loadingEl.hidden = true;
      emptyEl.hidden = false;
      return;
    }
    const windowed   = first && !Array.isArray(first) && typeof first.total === 'number';
    const total      = windowed ? Number(first.total) : (Array.isArray(first) ? first.length : 0);
    const firstChunk = windowed
      ? (Array.isArray(first.tracks) ? first.tracks : [])
      : (Array.isArray(first) ? first : []);

    // Branch-folder case: the clicked directory has no DIRECT audio
    // (e.g. ``modarchive_2007`` root holds only zip subfolders).  Auto-flatten
    // recursively — the backend's store-side recursive path returns the whole
    // subtree windowed, restoring "click and see everything".
    if (total === 0) {
      await _showFolderRecursiveWindowed(path, opts);
      return;
    }
    // Whole folder fit in the first chunk → render it directly (the common
    // album-sized case; no windowed store needed).
    if (total <= firstChunk.length) {
      if (opts.quiet && _sameTrackList(firstChunk, currentTracks)) return;
      renderTracks(firstChunk);
      if (!opts.quiet) _scheduleBackgroundRefresh(path);
      return;
    }
    // Large leaf folder — hand the prefetched first chunk + total to the
    // windowed store (non-recursive), which pulls more on scroll.
    await _showFolderRecursiveWindowed(path, opts, false, { total, tracks: firstChunk });
  } catch {
    loadingEl.hidden = true;
    emptyEl.hidden = false;
  }
}


/**
 * Fetch the first chunk of the recursive flatten + the total count,
 * then build a windowed store sized to ``total`` and install it as
 * ``currentTracks``.  Subsequent chunks pull lazily as the virtual
 * scroll moves — each chunk hits the server-side
 * ``_STORE_RECURSIVE_CACHE`` so the first chunk pays the ~1.2 s
 * (C64Music) / ~2.6 s (modarchive) helper-build cost and every later
 * chunk is sub-millisecond.
 *
 * The first fetch goes via the same ``/api/fstree/tracks-with-meta``
 * endpoint with ``offset=0`` and ``limit=CHUNK_SIZE`` so we get both
 * ``{total, tracks}`` back in one round trip — no separate count
 * probe.  We then preload chunk index 0 from that response so the
 * store never re-fetches it.
 *
 * Folder duplicate-collapsing is OWNED BY THE SERVER: we pass no
 * ``filter_duplicates`` param, so the endpoint resolves the
 * ``dedup_folders`` config toggle (Settings → "Hide duplicates when
 * browsing folders"; default off → folder views show every audio file on
 * disk).  Resolving it server-side means ``total`` and every chunk read
 * the same setting, so the windowed scroll stays consistent.  (Empty
 * folders are hidden separately via ``hide_empty_folders`` +
 * ``_has_audio`` — directory-level, not track-level dedup.)
 */
async function _showFolderRecursiveWindowed(path, opts = {}, recursive = true, prefetched = null) {
  const enc = encodeURIComponent(path);
  // Non-recursive (a single leaf folder with thousands of direct tracks, e.g.
  // an archive bucket) collapses duplicate groups server-side, matching the
  // old shallow-listing behaviour; recursive flattens uses the config default.
  const extra = recursive ? '' : '&filter_duplicates=true';
  const urlFor = (off, lim) =>
    `/api/fstree/tracks-with-meta?path=${enc}&recursive=${recursive}` +
    `&offset=${off}&limit=${lim}${extra}`;
  let firstRes = prefetched;
  if (!firstRes) {
    try {
      firstRes = await fetch(urlFor(0, CHUNK_SIZE)).then(r => r.json());
    } catch {
      loadingEl.hidden = true;
      emptyEl.hidden = false;
      return;
    }
  }
  const total      = Number(firstRes?.total || 0);
  const firstChunk = Array.isArray(firstRes?.tracks) ? firstRes.tracks : [];

  // Silent, size-gated drill-down freshness: skip in-place refreshes
  // (opts.quiet) and huge static archive subtrees (modarchive) whose recursive
  // re-walk over SMB would be expensive and pointless.
  if (!opts.quiet && total > 0 && total <= FRESHNESS_MAX_TRACKS) {
    _scheduleBackgroundRefresh(path);
  }

  // On a silent in-place refresh, skip the windowed rebuild (and its scroll
  // reset) when the subtree's total is unchanged — the common "nothing new"
  // case.  A same-count add+remove is rare and caught by the next real scan.
  if (opts.quiet && currentTracks && currentTracks._total === total) return;

  if (total === 0) {
    // Truly nothing under this subtree — render standard empty state.
    renderTracks([]);
    return;
  }

  const fetcher = async (offset, lim) => {
    try {
      const r = await fetch(urlFor(offset, lim)).then(r => r.json());
      return Array.isArray(r?.tracks) ? r.tracks : [];
    } catch {
      return [];
    }
  };
  const store = createWindowedStore(total, fetcher);
  // Preload chunk 0 from the first response so ``fetchChunk(0)``
  // short-circuits the moment ``_vsRender`` reaches into the store.
  store._chunks.set(0, firstChunk);
  store.setOnChunkLoad(() => _vsRender(true));
  // Reset scroll + selection — the indexes the user was looking at no
  // longer point at the same tracks.
  _selected.clear();
  _lastClickIdx = -1;
  _focusedIdx = -1;
  const wrap = document.getElementById('track-list-wrap');
  if (wrap) wrap.scrollTop = 0;
  renderTracks(store);
}

/**
 * Render the "branch folder" empty state in the main panel:
 * the clicked folder has no direct audio, only subfolders.  We tell
 * the user how to navigate (sidebar tree) AND give them an opt-in
 * "Show all tracks recursively" button.  Matches user mental model
 * (folder click = show what's in it = subfolders for branch nodes),
 * and avoids the 117 MB recursive-flatten that the old code did
 * silently on every branch-folder click.
 */
function _renderBranchEmpty(path) {
  // Tear down anything currently in the tbody (skeleton rows from
  // _showSkeletonRows, or stale data).  renderTracks([]) does the
  // tbody cleanup, sets emptyEl.hidden=false, and resets the heading
  // to "No tracks found" — we then customise the empty state below.
  renderTracks([]);

  const folderName = path.split('/').filter(Boolean).pop() || path;
  const safeName   = String(folderName).replace(/</g, '&lt;').replace(/>/g, '&gt;');
  emptyEl.innerHTML = `
    <span class="empty-icon" aria-hidden="true">&#128193;</span>
    <h4>${safeName} has no direct music files</h4>
    <p>This folder only contains subfolders.  Pick one in the sidebar tree
       on the left to drill in — that's where the music is.</p>
    <p style="margin-top:18px">
      <button id="show-recursively" class="btn-secondary"
              title="Flatten every track under ${safeName} into one list (this may take several seconds for large libraries)">
        Show all tracks recursively
      </button>
    </p>
  `;
  const btn = document.getElementById('show-recursively');
  if (btn) {
    btn.addEventListener('click', () => {
      // User explicitly opted in to the heavy flatten — reset the
      // empty state heading immediately so the recursive call's
      // ``_showSkeletonRows`` shimmer reads as "loading" rather than
      // "still on the branch-folder screen".
      emptyEl.hidden = true;
      showFolder(path, true);
    });
  }
}

function getSelectedTracks() {
  return [..._selected].sort((a, b) => a - b).map(i => currentTracks[i]).filter(Boolean);
}

function clearSelection() {
  _selected.clear();
  _vsRender();
  _updateSelectionBar();
}

// ── Smart playlist views ──────────────────────────────────────────────────────

/**
 * Fetch and display a Smart playlist from /api/smart/{view}.
 * @param {string} view  One of: history, most-played, recently-added, top-rated, unplayed
 * @param {string} label  Human-readable heading for the browse header
 */
async function showSmart(view, label) {
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  setBrowseHeader(label, () => showAll());
  _showSkeletonRows();

  try {
    const url = `/api/smart/${view}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status}`);
    const data = await res.json();

    if (view === 'history') {
      // History returns [{track_id, title, artist, ts}, ...]
      // We need to fetch the full track metadata for display
      const trackIds = data.map(h => h.track_id).filter(Boolean);
      if (!trackIds.length) { renderTracks([]); return; }
      const unique = [...new Set(trackIds)];

      // Fetch all tracks in one batch request (use search filter)
      const allTracks = [];
      for (let i = 0; i < unique.length; i += 50) {
        const chunk = unique.slice(i, i + 50);
        const promises = chunk.map(id =>
          fetch(`/api/tracks/${id}`).then(r => r.ok ? r.json() : null).catch(() => null)
        );
        const results = await Promise.all(promises);
        results.forEach(t => { if (t) allTracks.push(t); });
      }
      const trackMap = Object.fromEntries(allTracks.map(t => [t.id, t]));
      // Maintain history order (newest first)
      const ordered = data.map(h => trackMap[h.track_id]).filter(Boolean);
      renderTracks(ordered);
    } else {
      // All other smart views return track arrays directly
      renderTracks(Array.isArray(data) ? data : []);
    }
  } catch (err) {
    console.error('Smart view fetch failed:', err);
    renderTracks([]);
  }
}


// ── CSV export ──────────────────────────────────────────────────────────────

function _exportCSV(tracks, filename = 'soniqboom-export.csv') {
  const csvEsc = (v) => {
    const s = String(v ?? '');
    return s.includes(',') || s.includes('"') || s.includes('\n')
      ? '"' + s.replace(/"/g, '""') + '"' : s;
  };

  const headers = ['#','Title','Album Artist','Artist','Album','Track','Year',
                   'Duration','Format','Location','Primary','Group'];
  const rows = tracks.map((t, i) => [
    i + 1,
    t.title || '',
    t.album_artist || '',
    t.artist || '',
    t.album || '',
    t.track_number ?? '',
    t.year ?? '',
    t.duration ? fmtDur(t.duration) : '',
    t.format || '',
    _displayPath(t.path || ''),
    t._dupIsPrimary ? 'Yes' : (t._dupGroupId ? 'No' : ''),
    t._dupGroupId || '',
  ]);

  const csv = [headers, ...rows].map(r => r.map(csvEsc).join(',')).join('\r\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Duplicates view ──────────────────────────────────────────────────────────

/**
 * Show duplicate groups — each group is displayed as an expandable section
 * with the primary track shown and variants collapsed beneath.
 */
async function showDuplicates() {
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  setBrowseHeader('Duplicates', () => showAll());
  _dupViewActive = true;   // force Location column visible
  _showSkeletonRows();

  try {
    const res = await fetch('/api/smart/duplicates?limit=200');
    if (!res.ok) throw new Error(`${res.status}`);
    const groups = await res.json();

    if (!groups.length) {
      renderTracks([]);
      // Mutate just the heading so the leading icon + body paragraph
      // survive — ``textContent = …`` would have stripped them, leaving a
      // bare line of text on every subsequent empty state until reload.
      const heading = emptyEl.querySelector('h4');
      if (heading) heading.textContent = 'No duplicate tracks found.';
      emptyEl.hidden = false;
      return;
    }

    // Flatten: for each group, include primary first marked with group separator,
    // then variants. We annotate tracks with _dupGroup metadata so the renderer
    // can show group dividers and expand/collapse controls.
    const flatTracks = [];
    for (const g of groups) {
      const sorted = g.tracks.sort((a, b) => {
        if (a.is_duplicate_primary && !b.is_duplicate_primary) return -1;
        if (!a.is_duplicate_primary && b.is_duplicate_primary) return 1;
        return (b.format_score || 0) - (a.format_score || 0);
      });
      for (let i = 0; i < sorted.length; i++) {
        const t = sorted[i];
        t._dupGroupId     = g.group_id;
        t._dupGroupSize   = g.count;
        t._dupGroupFirst  = (i === 0);  // first in group = group header row
        t._dupIsPrimary   = t.is_duplicate_primary || false;
        t._dupFormatScore = t.format_score || 0;
        flatTracks.push(t);
      }
    }

    renderTracks(flatTracks);

    // Show CSV export button in the browse header
    _showExportBtn(() => _exportCSV(flatTracks, 'soniqboom-duplicates.csv'));
  } catch (err) {
    console.error('Duplicates fetch failed:', err);
    renderTracks([]);
  }
}


// ── Keyboard track navigation (J/K = down/up, Enter = play, A = queue) ──────

/**
 * Move the keyboard focus indicator by delta rows (+1 = down, -1 = up).
 * Scrolls the focused row into view.
 */
function navigateTrack(delta) {
  if (!currentTracks.length) return;
  const newIdx = Math.max(0, Math.min(currentTracks.length - 1, _focusedIdx + delta));
  if (newIdx === _focusedIdx) return;
  _focusedIdx = newIdx;

  // Update visual: remove old focus, add new
  tbody.querySelectorAll('tr.kb-focused').forEach(r => r.classList.remove('kb-focused'));
  const row = tbody.querySelector(`tr[data-idx="${_focusedIdx}"]`);
  if (row) {
    row.classList.add('kb-focused');
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } else {
    // Row not in virtual-scroll viewport — scroll to it
    const wrap = document.getElementById('track-list-wrap');
    if (wrap) {
      wrap.scrollTop = _focusedIdx * ROW_H;
      // After scroll, vsRender will fire and we re-apply focus
      requestAnimationFrame(() => {
        const r = tbody.querySelector(`tr[data-idx="${_focusedIdx}"]`);
        if (r) r.classList.add('kb-focused');
      });
    }
  }
}

/**
 * Add the keyboard-focused track to the player queue.
 */
function addFocusedToQueue() {
  if (_focusedIdx < 0 || _focusedIdx >= currentTracks.length) return;
  const t = currentTracks[_focusedIdx];
  if (t) Player.addToQueue(t);
}

/**
 * Play from the keyboard-focused track position.
 */
function playFocused() {
  if (_focusedIdx < 0 || _focusedIdx >= currentTracks.length) return;
  playFrom(_focusedIdx);
}


// ── Sort persistence: restore on startup ──────────────────────────────────────
_restoreSortState();

// Fetch initial sidebar badge counts
_refreshTrackCount();

// Background duration probe — for AdLib/IMF rows shown in ANY view that still
// carry the 180s placeholder, ask the server (via the shared probeAdlibDurations
// util) to compute the real length so the overview shows it without the user
// playing the track.  Debounced against scroll; the util dedups across views.
let _durProbeTimer = null;

function _scheduleDurationProbe() {
  if (_durProbeTimer) return;
  _durProbeTimer = setTimeout(() => {
    _durProbeTimer = null;
    _probeVisibleAdlibDurations();
  }, 300);
}

async function _probeVisibleAdlibDurations() {
  const arr = [];
  const refs = new Map();          // id -> track object, to patch a scrolled-away row
  for (let i = _vsStart; i < _vsEnd && i < currentTracks.length; i++) {
    const t = currentTracks[i];
    if (t && t.id) { arr.push(t); refs.set(t.id, t); }
  }
  const map = await probeAdlibDurations(arr);
  for (const id in map) {
    const sec = map[id];
    if (!(sec > 0)) continue;
    patchTrackDuration(id, sec);          // updates the visible cell + currentTracks[idx]
    const t = refs.get(id);               // also catch a row scrolled away before the reply
    if (t && Math.abs((+t.duration || 0) - sec) >= 0.5) t.duration = sec;  // probe result is authoritative
  }
}

// Live-correct the AdLib/IMF "3:00" placeholder once the player learns the real
// decoded length (audio.duration).  Updates the visible row's duration cell AND
// the cached track object in place; the server persists the same value via
// backfill, so a later folder re-fetch stays consistent.  Gated on the 180s
// placeholder, so it never overwrites a real or duration-capped (SID/GME) value.
function patchTrackDuration(id, seconds) {
  if (!id || !isFinite(seconds) || seconds <= 0 || !tbody) return;
  const sel = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
  const row = tbody.querySelector(`tr[data-id="${sel}"]`);
  if (!row) return;                       // only a currently-visible row
  const idx = parseInt(row.dataset.idx, 10);
  const t = (idx >= 0 && idx < currentTracks.length) ? currentTracks[idx] : null;
  // Render-only formats (AdLib/IMF + GME chiptunes: NSF/SPC/GBS/… + UADE/HVL
  // .ahx/.hvl) carry a scanner placeholder (or 0) until rendered; the server now
  // backfills + PERSISTS the real length, and the player's audio.duration equals
  // that rendered length — so for these formats the incoming value is
  // AUTHORITATIVE.  (Other formats already have a real scan duration and aren't
  // routed here.)  No placeholder gate is needed — the GME placeholder is the
  // configurable sid_default_duration (not always 180), and AHX/HVL are 0.
  if (!t || t.id !== id || !RENDER_DURATION_FORMAT_NAMES.has(t.format)) return;
  const cur = (+t.duration) || 0;
  if (Math.abs(seconds - cur) < 0.5) return;   // already the right value — no-op
  t.duration = seconds;
  const durTd = row.cells[8];             // col-dur
  if (durTd) {
    durTd.classList.remove('col-empty');
    durTd.textContent = fmtDur(seconds);
  }
}

export const Library = {
  patchTrackDuration,
  showAll, showArtists, showAlbumArtists, showAlbums, showAlbumTracks,
  showGenres, showYears, showGalaxy, showFolder, renderTracks,
  isInFolderView, currentFolderAffectedBy, refreshCurrentFolderInPlace,
  setBrowseHeader, hideBrowseHeader, onInfo,
  getSelectedTracks, clearSelection, refreshBadges: _refreshTrackCount,
  // Smart & duplicates views
  showSmart, showDuplicates,
  // Keyboard navigation
  navigateTrack, addFocusedToQueue, playFocused,
  // Location / alias configuration
  setAliasMap, setExposeLocalFiles,
};
