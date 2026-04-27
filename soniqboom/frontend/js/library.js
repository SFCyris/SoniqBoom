// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * library.js — Library browser: artists / albums / genres / years / all tracks.
 * Exports: Library singleton
 */
import { Player } from './player.js';

const API = (path, q = {}) => {
  const qs = new URLSearchParams(q).toString();
  return fetch(`/api${path}${qs ? '?' + qs : ''}`).then(r => r.json());
};

// ── DOM refs ──────────────────────────────────────────────────────────────────
const tbody            = document.getElementById('track-tbody');
const emptyEl          = document.getElementById('track-empty');
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
let activeRow = null;
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
const ROW_H  = 28;   // px per row (approximate, matches td padding)
const VS_BUF = 10;   // rows to render above/below viewport

let _vsStart = 0;    // first rendered data index
let _vsEnd   = 0;    // one past last rendered data index

// ── Multi-select state ─────────────────────────────────────────────────────────
let _selected     = new Set();
let _lastClickIdx = -1;

// ── Grid view state ────────────────────────────────────────────────────────────
let _gridView = false;

// ── Column visibility state ────────────────────────────────────────────────────
const ALL_COLS = ['col-num','col-title','col-album-artist','col-artist','col-album','col-track','col-year','col-dur','col-format','col-location','col-rating'];
const COL_LABELS = {
  'col-num':          '#',
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
  let html = '';
  for (let i = 1; i <= 5; i++) {
    const cls = i <= rating ? 'star star-filled' : 'star star-empty';
    html += `<span class="${cls}" data-val="${i}">★</span>`;
  }
  return html;
}

// ── Skeleton loading rows ─────────────────────────────────────────────────────
function _showSkeletonRows(count = 18) {
  const albumGrid = document.getElementById('album-grid');
  if (albumGrid) albumGrid.hidden = true;
  document.getElementById('track-table').style.display = '';
  tbody.innerHTML = '';
  emptyEl.hidden = true;
  loadingEl.hidden = true;
  for (let i = 0; i < count; i++) {
    const tr = document.createElement('tr');
    tr.className = 'skeleton';
    tr.innerHTML = `
      <td class="col-num"><span class="skel-bar" style="width:${16 + Math.random() * 8|0}px"></span></td>
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
function _updateNavBadge(view, count) {
  const li = document.querySelector(`#nav-library li[data-view="${view}"]`);
  if (!li) return;
  let badge = li.querySelector('.nav-count');
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'nav-count';
    li.appendChild(badge);
  }
  badge.textContent = count != null ? count.toLocaleString() : '';
}

async function _refreshTrackCount() {
  try {
    const { count } = await API('/tracks/count');
    _updateNavBadge('all', count);
  } catch {}
}

// ── Make a single track row element ───────────────────────────────────────────
function _makeTrackRow(t, i) {
  const tr = document.createElement('tr');
  tr.dataset.id  = t.id;
  tr.dataset.idx = i;

  if (_selected.has(i)) tr.classList.add('multi-selected');
  if (t._scanned === false) tr.classList.add('unscanned');

  // Duplicate group styling (set by showDuplicates)
  if (t._dupGroupFirst) tr.classList.add('dup-group-first');
  if (t._dupIsPrimary)  tr.classList.add('dup-primary');
  if (t._dupGroupId && !t._dupIsPrimary) tr.classList.add('dup-variant');

  // Format disc + track as "D1-01" (disc only shown when present alongside track)
  const disc = t.disc_number != null ? `D${t.disc_number}` : '';
  const trk  = t.track_number != null ? String(t.track_number).padStart(2, '0') : '';
  const trackStr = disc && trk ? `${disc}-${trk}` : (trk || disc || '');

  const rating = _ratingsCache[t.id] || 0;
  const stars = _renderStars(rating);
  const unscan = t._scanned === false;

  // Clickable metadata cells + dim dashes for empty values
  const hasAA = !unscan && t.album_artist;
  const hasAr = !unscan && t.artist;
  const hasAl = !unscan && t.album;
  const aaHtml = unscan ? '' : hasAA ? `<span class="cell-link" data-action="album-artist">${esc(t.album_artist)}</span>` : '—';
  const arHtml = unscan ? '' : esc(t.artist || '—');
  const alHtml = unscan ? '' : hasAl ? `<span class="cell-link" data-action="album">${esc(t.album)}</span>` : '—';

  tr.innerHTML = `
    <td class="col-num">${i + 1}</td>
    <td class="col-title"        title="${esc(t.title)}"       >${esc(t.title        || '—')}</td>
    <td class="col-album-artist${!unscan && !hasAA ? ' col-empty' : ''}" title="${esc(t.album_artist)}">${aaHtml}</td>
    <td class="col-artist${!unscan && !hasAr ? ' col-empty' : ''}"       title="${esc(t.artist)}"      >${arHtml}</td>
    <td class="col-album${!unscan && !hasAl ? ' col-empty' : ''}"        title="${esc(t.album)}"       >${alHtml}</td>
    <td class="col-track">${trackStr}</td>
    <td class="col-year">${t.year || ''}</td>
    <td class="col-dur${!unscan && !t.duration ? ' col-empty' : ''}">${unscan ? '' : fmtDur(t.duration)}</td>
    <td class="col-format"><span class="fmt-badge fmt-${_fmtClass(t.format)}">${esc(t.format || '')}</span></td>
    <td class="col-location" title="${_exposeLocalFiles ? esc(t.path) : ''}">${esc(_displayPath(t.path || ''))}</td>
    <td class="col-rating">${stars}</td>`;

  // Rating click handler — uses event delegation on the td (survives innerHTML replacement)
  const ratingTd = tr.querySelector('.col-rating');
  function _handleRatingClick(e) {
    e.stopPropagation();
    const star = e.target.closest('.star');
    if (!star) return;
    const newRating = parseInt(star.dataset.val);
    const finalRating = (_ratingsCache[t.id] === newRating) ? 0 : newRating;
    _ratingsCache[t.id] = finalRating;
    ratingTd.innerHTML = _renderStars(finalRating);
    fetch(`/api/tracks/${t.id}/rating`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: finalRating }),
    }).catch(() => {});
  }
  ratingTd.addEventListener('click', _handleRatingClick);

  tr.setAttribute('draggable', 'true');
  tr.addEventListener('dragstart', (e) => {
    e.dataTransfer.effectAllowed = 'copy';
    // If the dragged row is part of a multi-selection, carry all selected tracks
    const dragTracks = _selected.size > 1 && _selected.has(i)
      ? [..._selected].sort((a, b) => a - b).map(j => currentTracks[j]).filter(Boolean)
      : [currentTracks[i]];
    e.dataTransfer.setData('application/x-soniqboom-track', JSON.stringify(dragTracks));
    tr.classList.add('dragging');
    if (dragTracks.length > 1) {
      tbody.querySelectorAll('tr.multi-selected').forEach(r => r.classList.add('dragging'));
    }
  });
  tr.addEventListener('dragend', () => {
    tbody.querySelectorAll('tr.dragging').forEach(r => r.classList.remove('dragging'));
  });

  tr.addEventListener('click', (e) => {
    // Handle clickable metadata cells (album artist / album)
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
      // Select range
      const lo = Math.min(_lastClickIdx, i), hi = Math.max(_lastClickIdx, i);
      if (!e.metaKey && !e.ctrlKey) _selected.clear();
      for (let j = lo; j <= hi; j++) _selected.add(j);
    } else if (e.metaKey || e.ctrlKey) {
      // Toggle individual row
      if (_selected.has(i)) _selected.delete(i); else _selected.add(i);
      _lastClickIdx = i;
    } else {
      // Single select — clear multi-selection first
      _selected.clear();
      _selected.add(i);
      _lastClickIdx = i;
      selectRow(tr, i);
    }
    // Update .multi-selected classes directly — avoids _vsRender() early-return
    // when the virtual scroll viewport hasn't changed (no scroll between clicks)
    _refreshSelectionClasses();
    _updateSelectionBar();
  });

  tr.addEventListener('dblclick', () => playFrom(i));
  tr.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    if (_infoCallback) _infoCallback(currentTracks, i);
  });

  return tr;
}

// ── Virtual scroll render ──────────────────────────────────────────────────────
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

  // Rebuild tbody: top spacer + visible rows + bottom spacer
  const topH = _vsStart * ROW_H;
  const botH = (currentTracks.length - _vsEnd) * ROW_H;

  tbody.innerHTML = '';

  // Top spacer
  if (topH > 0) {
    const sp = document.createElement('tr');
    sp.className = 'vs-spacer';
    sp.innerHTML = `<td colspan="11" style="height:${topH}px;padding:0;border:none"></td>`;
    tbody.appendChild(sp);
  }

  // Visible rows
  for (let i = _vsStart; i < _vsEnd; i++) {
    tbody.appendChild(_makeTrackRow(currentTracks[i], i));
  }

  // Bottom spacer
  if (botH > 0) {
    const sp = document.createElement('tr');
    sp.className = 'vs-spacer';
    sp.innerHTML = `<td colspan="11" style="height:${botH}px;padding:0;border:none"></td>`;
    tbody.appendChild(sp);
  }

  markPlayingRow();
  _applyColVisibility();

  // Restore keyboard focus indicator after virtual scroll rebuild
  if (_focusedIdx >= _vsStart && _focusedIdx < _vsEnd) {
    const focusRow = tbody.querySelector(`tr[data-idx="${_focusedIdx}"]`);
    if (focusRow) focusRow.classList.add('kb-focused');
  }
}

async function renderTracks(tracks) {
  currentTracks = tracks;
  _selected.clear();
  _lastClickIdx = -1;
  _focusedIdx   = -1;
  _vsStart = 0;
  _vsEnd   = 0;

  emptyEl.hidden  = tracks.length > 0;
  loadingEl.hidden = true;

  // Hide album grid, show table
  const albumGrid = document.getElementById('album-grid');
  if (albumGrid) albumGrid.hidden = true;
  document.getElementById('track-table').style.display = '';

  tbody.innerHTML = '';

  if (tracks.length > 0) {
    _vsRender(true);
    // Kick off lazy ratings fetch for the initial visible range
    _fetchVisibleRatings();
  }

  // Scroll to top
  const wrap = document.getElementById('track-list-wrap');
  if (wrap) wrap.scrollTop = 0;

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
    // Re-render to show the ratings
    _vsRender(true);
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
document.getElementById('track-list-wrap').addEventListener('scroll', () => {
  _vsRender();
  _fetchVisibleRatings();
}, { passive: true });

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
  // Show "More Like This" button
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
  Player.setQueue(currentTracks, idx);
}

// ── Sort persistence — save/restore sort column + direction ──────────────────
function _saveSortState() {
  if (sortKey) {
    localStorage.setItem('sb_sort_key', sortKey);
    localStorage.setItem('sb_sort_asc', sortAsc ? '1' : '0');
  }
}

function _restoreSortState() {
  const key = localStorage.getItem('sb_sort_key');
  const asc = localStorage.getItem('sb_sort_asc');
  if (key) {
    sortKey = key;
    sortAsc = asc !== '0';
    // Apply visual indicator to the header
    const th = document.querySelector(`th[data-sort="${sortKey}"]`);
    if (th) th.classList.add('sorted', sortAsc ? 'sorted-asc' : 'sorted-desc');
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

document.querySelectorAll('#track-table th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = true; }
    document.querySelectorAll('th[data-sort]').forEach(t => {
      t.classList.remove('sorted', 'sorted-asc', 'sorted-desc');
    });
    th.classList.add('sorted', sortAsc ? 'sorted-asc' : 'sorted-desc');
    _saveSortState();
    const sorted = [...currentTracks].sort((a, b) => _compareTrack(a, b, sortKey, sortAsc));
    renderTracks(sorted);
  });
});

// ── Table header helpers ───────────────────────────────────────────────────────
const trackTableHead = document.querySelector('#track-table thead tr');
const FULL_HEADERS = `
  <th class="col-num"          data-sort="">#</th>
  <th class="col-title"        data-sort="title">Title</th>
  <th class="col-album-artist" data-sort="album_artist">Album Artist</th>
  <th class="col-artist"       data-sort="artist">Artist</th>
  <th class="col-album"        data-sort="album">Album</th>
  <th class="col-track"        data-sort="track_number">Track</th>
  <th class="col-year"         data-sort="year">Year</th>
  <th class="col-dur"          data-sort="duration">Duration</th>
  <th class="col-format"       data-sort="format">Type</th>
  <th class="col-location"     data-sort="path">Location</th>
  <th class="col-rating"       data-sort="">★</th>`.trim();

function setGroupHeader(label) {
  if (!trackTableHead) return;
  trackTableHead.innerHTML = `<th colspan="11" style="font-weight:600;padding:6px 10px">${esc(label)}</th>`;
}

function restoreFullHeader() {
  _dupViewActive = false;  // leaving duplicates view — restore user column prefs
  if (!trackTableHead) return;
  trackTableHead.innerHTML = FULL_HEADERS;
  // Re-attach sort listeners after rebuilding the header
  trackTableHead.querySelectorAll('th[data-sort]').forEach(th => {
    if (!th.dataset.sort) return;
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = true; }
      document.querySelectorAll('th[data-sort]').forEach(t => {
        t.classList.remove('sorted', 'sorted-asc', 'sorted-desc');
      });
      th.classList.add('sorted', sortAsc ? 'sorted-asc' : 'sorted-desc');
      _saveSortState();
      const sorted = [...currentTracks].sort((a, b) => _compareTrack(a, b, sortKey, sortAsc));
      renderTracks(sorted);
    });
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

async function showAll() {
  hideBrowseHeader();
  _hideGroupFilter();
  _hideGridToggle();
  restoreFullHeader();
  _showSkeletonRows();
  const tracks = await API('/tracks', { limit: 5000 });
  renderTracks(tracks);
  _updateNavBadge('all', tracks.length);
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
      const artist = card.dataset.artist;
      if (!album) return;

      // Fetch a track for this album to get its art
      const params = new URLSearchParams({ album, limit: '1' });
      if (artist) params.set('artist', artist);
      fetch(`/api/search/filter?${params}`)
        .then(r => r.json())
        .then(tracks => {
          if (!tracks.length) return;
          const track = tracks[0];
          const artEl = card.querySelector('.album-card-art');
          if (!artEl) return;
          const img = new Image();
          img.onload = () => {
            artEl.style.backgroundImage = `url("${img.src}")`;
            artEl.style.backgroundSize = 'cover';
            artEl.style.backgroundPosition = 'center';
            const initialsEl = artEl.querySelector('.album-card-initials');
            if (initialsEl) initialsEl.style.display = 'none';
            card.classList.add('album-card-art-loaded');
          };
          img.src = `/api/art/${track.id}?size=sm`;
        })
        .catch(() => {});

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
    card.innerHTML = `
      <div class="album-card-art">
        <span class="album-card-initials">${esc(initials)}</span>
      </div>
      <div class="album-card-info">
        <div class="album-card-title" title="${esc(name)}">${esc(name)}</div>
        <div class="album-card-sub">${count} ${countLabel}</div>
      </div>`;
    card.addEventListener('click', () => onClick(item));
    albumGrid.appendChild(card);

    // Observe for lazy-load
    _albumArtObserver.observe(card);
  });
}

// Renders a grouped list (artists/albums/genres/years) into the table
function renderGroupList(items, nameKey, countKey, countLabel, onClick, showFilter = true) {
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
      setTimeout(() => groupFilterInput.focus(), 50);
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

function _renderGroupRows(items, nameKey, countKey, countLabel, onClick) {
  tbody.innerHTML = '';
  emptyEl.hidden = items.length > 0;
  items.forEach(item => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="col-num"></td>
      <td class="col-title" colspan="7" style="font-weight:500;${item.label ? 'font-style:italic;color:var(--text2)' : ''}">${esc(item.label || item[nameKey] || '—')}</td>
      <td class="col-dur" style="color:var(--text2)">${item[countKey]} ${countLabel}</td>
      <td class="col-rating"></td>`;
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => onClick(item));
    tbody.appendChild(tr);
  });
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
browseFilter.addEventListener('input', () => {
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
function hideBrowseHeader() { browseHdr.hidden = true; _dupViewActive = false; _hideExportBtn(); }

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
  _lyricsSyncLastActive = active;

  const lineEls = lyricsContent.querySelectorAll('.lyrics-line');
  for (let i = 0; i < lineEls.length; i++) {
    const el = lineEls[i];
    const ln = _lyricsLines[i];
    if (!ln) continue;
    el.classList.toggle('active', i === active);
    el.classList.toggle('past', ln.time !== null && i < active);
  }

  // Scroll active line into view (centered)
  if (active >= 0 && lineEls[active]) {
    lineEls[active].scrollIntoView({ block: 'center', behavior: 'smooth' });
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

// When track changes, update button state and reload lyrics if panel is open
Player.on('trackchange', (track) => {
  _updateLyricsBtn(track);
  if (!lyricsPanel.hidden) {
    _loadLyrics(track);
  } else {
    _lyricsTrackId = null;
    _stopLyricsSync();
  }
});

async function showFolder(path, recursive = false) {
  _hideGridToggle();
  _showSkeletonRows();

  setBrowseHeader(path.split('/').filter(Boolean).pop() || path, () => {
    hideBrowseHeader();
    document.querySelectorAll('#nav-library li')[0]?.click();
  });

  try {
    const res = await fetch(
      `/api/fstree/tracks-with-meta?path=${encodeURIComponent(path)}&recursive=${recursive}`
    );
    const tracks = await res.json();
    renderTracks(Array.isArray(tracks) ? tracks : []);
  } catch {
    loadingEl.hidden = true;
    emptyEl.hidden = false;
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
      emptyEl.hidden = false;
      emptyEl.textContent = 'No duplicate tracks found.';
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

export const Library = {
  showAll, showArtists, showAlbumArtists, showAlbums, showAlbumTracks,
  showGenres, showYears, showFolder, renderTracks,
  setBrowseHeader, hideBrowseHeader, onInfo,
  getSelectedTracks, clearSelection, refreshBadges: _refreshTrackCount,
  // Smart & duplicates views
  showSmart, showDuplicates,
  // Keyboard navigation
  navigateTrack, addFocusedToQueue, playFocused,
  // Location / alias configuration
  setAliasMap, setExposeLocalFiles,
};
