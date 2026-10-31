// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * playlist.js — Playlist panel with queue-style UX.
 *
 * Views:
 *   "list"   — shows all playlists; click one to enter track view
 *   "tracks" — shows the active playlist's tracks
 *
 * Selection:
 *   Click          → play track (clears any selection)
 *   ⌘/Ctrl+click   → toggle row in selection (no play change)
 *   Shift+click    → range-select from anchor to here
 *   Delete/⌫       → remove selected tracks from playlist
 *   Drag selection → reorder all selected rows together
 *
 * Duplicates:
 *   Same track ID added twice     → amber "2×" pill (mistake indicator)
 *   Same song, different location → muted "⧉" icon (info indicator)
 *
 * The open playlist: a header (playlist/hero.js — cover mosaic, who shares it,
 * name, tracks · length · format mix, Play / Shuffle / Share / ⋯ / Style) over
 * rows with a cover, title + artist, a format chip, a mini waveform (the
 * stored one — playlist/waves.js; the playing row shows its progress in it)
 * and the length.  Two looks, Modern and Retro hi-fi (a cassette J-card),
 * shared with the equalizer and Stations (ui_style.js) — the CSS is
 * css/playlist-styles.css.  Reordering shows a drop line and animates the
 * rows to their new places; imported and smart playlists can't be reordered.
 * The waveform painter, the glide, the drop line and the touch long-press are
 * shared with the queue panel (playlist/listkit.js).
 *
 * Exports: Playlist singleton
 */
import { Player } from './player.js';
import { Library } from './library.js';
import { artPlaceholderEmoji, Toast, probeAdlibDurations, subsongWireToTune, subsongStartOf } from './utils.js';
import { createVirtualList } from './vlist.js';
import { offlineKind, offlineBadgeHTML } from './offline.js';
import { healthBadgeHTML } from './health.js';
import { Auth } from './auth.js';
import { getUiStyle, onUiStyleChange } from './ui_style.js';
import { vizGroupEnabled } from './viz/engine.js';
import { createHero, fmtShort, fmtFamily } from './playlist/hero.js';
import { createWaveStore } from './playlist/waves.js';
import {
  createWavePainter, rowTops, flipFrom, clearFlip, createDropLine, gapAt, bindLongPressDrag,
  fmtDur, esc, NARROW_WAVES,
} from './playlist/listkit.js';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const panel       = document.getElementById('playlist-panel');
const hdrList     = document.getElementById('pl-hdr-list');
const hdrTracks   = document.getElementById('pl-hdr-tracks');
const btnNew      = document.getElementById('btn-playlist-new');
const btnClose    = document.getElementById('btn-playlist-close');
const btnClose2   = document.getElementById('btn-playlist-close2');
const btnBack     = document.getElementById('btn-playlist-back');
const plActiveNm  = document.getElementById('pl-active-name');
const plActiveCt  = document.getElementById('pl-active-count');
const listEl      = document.getElementById('playlist-list');
const tracksEl    = document.getElementById('playlist-tracks');
const dropZone    = document.getElementById('playlist-drop-zone');
const selBarEl    = document.getElementById('pl-sel-bar');
const selCountEl  = document.getElementById('pl-sel-count');
const btnSelRm    = document.getElementById('pl-sel-remove');
const btnSelClr   = document.getElementById('pl-sel-deselect');
const sidebarList = document.getElementById('sidebar-playlist-list');
const btnSideNew  = document.getElementById('btn-sidebar-playlist-new');
const btnImport   = document.getElementById('btn-playlist-import');
const btnShare    = document.getElementById('btn-playlist-share');
const btnUnsub    = document.getElementById('btn-playlist-unsub');

// The open playlist's header (cover mosaic, name, stats, actions, Style).
const _hero = createHero({
  panel, after: hdrTracks, shareBtn: btnShare, unsubBtn: btnUnsub,
  onPlay: () => _heroPlay(),
  onShuffle: () => _heroShuffle(),
  menuItems: () => _heroMenuItems(),
});
panel.dataset.plStyle = getUiStyle();

// ── State ─────────────────────────────────────────────────────────────────────
let _playlists    = [];
let _activeId     = null;
let _activeTracks = [];
let _activeRO     = false;   // the open playlist is someone else's (imported): no edits
let _activeSmart  = false;   // a smart playlist (a saved search): no reorder / remove
let _activeData   = null;    // GET /playlists/{id} of the open playlist (owner_name, subscriber_count…)
let _queuedFrom   = null;    // the playlist whose Play / row click made the current queue
let _queuedObjs   = new Set(); // …and the entry objects it handed the player (identity)
let _addDropdown  = null;

// ── Selection state ───────────────────────────────────────────────────────────
let _selectedIdxs = new Set();   // selected row indices
let _anchorIdx    = null;        // Shift-click anchor

// ── Helpers ───────────────────────────────────────────────────────────────────
// (esc / fmtDur: playlist/listkit.js)

// Inline styled confirm — installed synchronously so playlist.js never falls
// back to the OS-native ``confirm()`` (which is blocking, ugly, and ignores
// title/okLabel options).  If admin.js later overrides ``__sbConfirm`` with
// a richer dialog the playlist will pick that up automatically because the
// callers read ``window.__sbConfirm`` lazily on each invocation.
function _inlineConfirm(message, { title = 'Confirm', okLabel = 'OK' } = {}) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'pl-modal-backdrop';
    const dialog = document.createElement('div');
    dialog.className = 'pl-modal-dialog';
    dialog.innerHTML = `
      <div class="pl-modal-title"></div>
      <div class="pl-modal-body"
           style="padding:6px 0 14px;color:var(--text2,#bbb);font-size:13px"></div>
      <div class="pl-modal-actions">
        <button class="pl-modal-btn pl-modal-cancel">Cancel</button>
        <button class="pl-modal-btn pl-modal-ok"></button>
      </div>
    `;
    // textContent assignments — message/title come from caller code, but we
    // still avoid HTML interpolation as a defence-in-depth measure.
    dialog.querySelector('.pl-modal-title').textContent = title;
    dialog.querySelector('.pl-modal-body').textContent  = message;
    dialog.querySelector('.pl-modal-ok').textContent    = okLabel;
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    const btnOk  = dialog.querySelector('.pl-modal-ok');
    const btnCan = dialog.querySelector('.pl-modal-cancel');
    btnOk.focus();
    function finish(value) {
      backdrop.classList.add('pl-modal-out');
      setTimeout(() => backdrop.remove(), 150);
      resolve(value);
    }
    btnOk.addEventListener('click',  () => finish(true));
    btnCan.addEventListener('click', () => finish(false));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish(false); });
    dialog.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') finish(false);
      if (e.key === 'Enter')  finish(true);
    });
  });
}

// Guarantee a global confirm helper at module load.  If admin.js later
// installs its richer ``__sbConfirm`` we leave it alone — admin.js runs
// after this module so the override path is preserved by checking that the
// property isn't already set.
if (!window.__sbConfirm) {
  window.__sbConfirm = _inlineConfirm;
}
async function _api(path, opts = {}) {
  const res = await fetch(`/api${path}`, opts);
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

// ── Selection ─────────────────────────────────────────────────────────────────
function _clearSelection(rerender = false) {
  _selectedIdxs.clear();
  _anchorIdx = null;
  _updateSelBar();
  if (rerender) _renderTracks();
}

function _updateSelBar() {
  if (!selBarEl) return;
  const n = _selectedIdxs.size;
  selBarEl.hidden = n === 0;
  if (n > 0 && selCountEl) {
    selCountEl.textContent = `${n} track${n !== 1 ? 's' : ''} selected`;
  }
}

// Light DOM update — toggle .selected class without full re-render
function _refreshRowClasses() {
  tracksEl.querySelectorAll('.queue-row[data-idx]').forEach(row => {
    const idx = parseInt(row.dataset.idx, 10);
    row.classList.toggle('selected', _selectedIdxs.has(idx));
  });
}

// ── Duplicate detection ───────────────────────────────────────────────────────
function _contentKey(t) {
  const title  = (t.title  || '').toLowerCase().replace(/[^\w]/g, '');
  const artist = (t.artist || t.album_artist || '').toLowerCase().replace(/[^\w]/g, '');
  const dur    = Math.round((t.duration || 0) / 4) * 4;   // ±4 s bucket
  return `${artist}::${title}::${dur}`;
}

// Entry identity for the "added twice" badge: the track AND its tune — two
// tunes of one multi-tune file are different entries, not a duplicate.
function _dupKey(t) { const s = _plSubKey(t); return s === null ? t.id : `${t.id}#${s}`; }

function _buildDupMaps(tracks) {
  const idCount    = new Map();     // entry key → times it appears
  const idFirstAt  = new Map();     // entry key → first index
  const contentMap = new Map();     // contentKey → [{id, path, idx}]

  tracks.forEach((t, i) => {
    const key = _dupKey(t);
    idCount.set(key, (idCount.get(key) || 0) + 1);
    if (!idFirstAt.has(key)) idFirstAt.set(key, i);
    const k = _contentKey(t);
    if (!contentMap.has(k)) contentMap.set(k, []);
    contentMap.get(k).push({ id: t.id, path: t.path, idx: i });
  });

  return { idCount, idFirstAt, contentMap };
}

// ── Fetch all playlists ───────────────────────────────────────────────────────
async function refresh() {
  try { _playlists = await _api('/playlists'); }
  catch (err) {
    _playlists = [];
    console.warn('Failed to load playlists:', err);
    Toast.error("Couldn't load playlists — the list may be out of date.");
  }
  _renderSidebar();
  if (!panel.classList.contains('hidden')) {
    if (_activeId) {
      const pl = _playlists.find(p => p.id === _activeId);
      pl ? await _openPlaylist(_activeId, pl.name, false) : _showListView();
    } else {
      _renderListView();
    }
  }
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function _renderSidebar() {
  if (!sidebarList) return;
  sidebarList.innerHTML = '';
  if (!_playlists.length) {
    const empty = document.createElement('li');
    empty.className = 'sidebar-playlist-empty';
    empty.textContent = 'No playlists yet';
    sidebarList.appendChild(empty);
    return;
  }
  _playlists.forEach(pl => {
    const li = document.createElement('li');
    li.className = 'sidebar-playlist-item' + (pl.id === _activeId ? ' active' : '')
      + (pl.subscribed ? ' pl-subscribed' : '');
    li.dataset.id = pl.id;
    const cnt = pl.smart ? '⚡' : (pl.track_count ?? pl.tracks?.length ?? 0);
    const cntTitle = pl.smart ? 'Smart playlist — updates itself from a saved search' : '';
    li.innerHTML = `<span class="pl-name">${esc(pl.name)}</span>${_sharedBadge(pl)}<span class="pl-count" title="${cntTitle}">${cnt}</span>`;
    li.addEventListener('click', () => { open(); _openPlaylist(pl.id, pl.name); });
    sidebarList.appendChild(li);
  });
}

// A shared playlist's mark: the owner's own ("Shared") or an imported one
// ("by <owner>", read-only).
function _sharedBadge(pl) {
  if (pl.subscribed) {
    return `<span class="pl-shared-badge pl-by" title="Imported from ${esc(pl.owner_name || 'another user')} — read-only">by ${esc(pl.owner_name || '?')}</span>`;
  }
  return pl.shared ? '<span class="pl-shared-badge" title="Shared with every user">Shared</span>' : '';
}

// The signed-in account may share playlists (readonly accounts may not).
function _canShare() { return !!(Auth.user && Auth.user.role !== 'readonly'); }

async function _setSubscribed(id, subscribed) {
  await _api(`/playlists/${id}/subscription`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscribed }),
  });
}

// ── List view ─────────────────────────────────────────────────────────────────
function _showListView() {
  _activeId     = null;
  _activeTracks = [];
  _clearSelection();
  _activeRO = false;
  _activeSmart = false;
  _activeData = null;
  panel.classList.remove('pl-readonly', 'pl-smart', 'plx-open');
  _hero.hide();
  _hideDropLine();
  hdrList.hidden   = false;
  hdrTracks.hidden = true;
  listEl.hidden    = false;
  tracksEl.hidden  = true;
  if (selBarEl) selBarEl.hidden = true;
  dropZone.classList.add('drop-zone-disabled');
  dropZone.textContent = 'Open a playlist to drop tracks here';
  _renderListView();
  _updateSidebarActive();
}

function _renderListView() {
  listEl.innerHTML = '';
  if (!_playlists.length) {
    listEl.innerHTML = '<div class="playlist-empty">No playlists yet.<br>Click <strong>+ New</strong> to create one.</div>';
    return;
  }
  _playlists.forEach(pl => {
    const row = document.createElement('div');
    row.className = 'playlist-row';
    row.dataset.id = pl.id;
    const cnt = pl.track_count ?? pl.tracks?.length ?? 0;
    row.innerHTML = `
      <div class="playlist-row-info">
        <span class="playlist-row-name">${esc(pl.name)}</span>${_sharedBadge(pl)}
        <span class="playlist-row-count">${cnt} track${cnt !== 1 ? 's' : ''}</span>
      </div>
      <button class="pl-open-btn" title="Open playlist">&#9654;</button>
      <button class="playlist-delete-btn" title="${pl.subscribed ? 'Unsubscribe' : 'Delete'}" data-id="${pl.id}">&times;</button>
    `;
    row.addEventListener('click', (e) => {
      if (e.target.closest('.playlist-delete-btn')) return;
      _openPlaylist(pl.id, pl.name);
    });
    row.querySelector('.playlist-delete-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      if (pl.subscribed) {                     // someone else's: unsubscribe, never delete
        try { await _setSubscribed(pl.id, false); await refresh(); }
        catch (err) { Toast.error('Could not remove the imported playlist.'); }
        return;
      }
      // __sbConfirm is installed synchronously at module load (see top of
      // this file), so we don't fall back to the OS-native confirm() here.
      const ok = await window.__sbConfirm(
        `Delete "${pl.name}"?`,
        { title: 'Delete Playlist', okLabel: 'Delete' },
      );
      if (!ok) return;
      try {
        await _api(`/playlists/${pl.id}`, { method: 'DELETE' });
        await refresh();
      } catch (err) {
        console.warn('Failed to delete playlist:', err);
        Toast.error('Could not delete playlist — the server rejected the request.');
      }
    });
    listEl.appendChild(row);
  });
}

// ── Track view ────────────────────────────────────────────────────────────────
async function _openPlaylist(id, name, _focusPanel = true) {
  _activeId = id;
  _clearSelection();
  // Load BEFORE swapping the panel header — on failure we keep the list
  // view visible so the user sees the existing playlists rather than a
  // "0 tracks" empty-state that looks identical to a real empty playlist.
  let loadFailed = false;
  let loadedTracks = [];
  let loadedData = null;
  try {
    const data   = await _api(`/playlists/${id}`);
    loadedData   = data;
    loadedTracks = data.tracks || [];
  } catch (err) {
    loadFailed = true;
    console.warn('Failed to open playlist:', err);
    Toast.error("Couldn't load this playlist's tracks — staying on the list view.");
  }

  if (loadFailed) {
    _activeId = null;
    return;
  }

  // A newer _openPlaylist (rapid A→B) may have superseded this one while it
  // loaded — _activeId now points at B.  Bail BEFORE mutating shared state so B's
  // slower load isn't overwritten by A's tracks (a later re-render would paint
  // A's tracks under B's header) or panel.
  if (_activeId !== id) return;
  _activeTracks = loadedTracks;   // commit only after confirming we're still current
  const summary = _playlists.find(p => p.id === id) || {};
  _activeRO = !!(loadedData && loadedData.read_only);
  _activeSmart = !!(loadedData && (loadedData.smart || loadedData.query)) || !!summary.smart;
  _activeData = loadedData;
  panel.classList.toggle('pl-readonly', _activeRO);
  panel.classList.toggle('pl-smart', _activeSmart);
  panel.classList.add('plx-open');
  if (_focusPanel) _waves.forgetMissing();   // (opened again: tracks played since may have one now)
  if (btnShare) {
    // (a readonly owner may still unshare a playlist shared before)
    btnShare.hidden = _activeRO || (!_canShare() && !summary.shared);
    btnShare.textContent = summary.shared ? 'Shared ✓' : 'Share';
    btnShare.classList.toggle('active', !!summary.shared);
    btnShare.setAttribute('aria-pressed', String(!!summary.shared));
  }
  if (btnUnsub) btnUnsub.hidden = !_activeRO;

  plActiveNm.textContent = name;
  plActiveNm.title = name;   // (one ellipsised line — the full name on hover)
  plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`
    + (_activeRO && loadedData.owner_name ? ` · by ${loadedData.owner_name}` : '');

  hdrList.hidden   = true;
  hdrTracks.hidden = false;
  listEl.hidden    = true;
  tracksEl.hidden  = false;
  dropZone.classList.remove('drop-zone-disabled');
  dropZone.textContent = 'Drop tracks here to add';

  _renderTracks({ toTop: true });
  _updateSidebarActive();
}

// Which tracks can play changed (a drive / share went offline or came back):
// the open playlist re-reads its entries for their offline marks, keeping its
// scroll position (the entries themselves are the same).
document.addEventListener('soniqboom:availability-changed', async () => {
  const id = _activeId;
  if (!id || tracksEl.hidden) return;
  try {
    const data = await _api(`/playlists/${id}`);
    if (_activeId !== id || !Array.isArray(data.tracks)
        || data.tracks.length !== _activeTracks.length) return;
    _activeTracks = data.tracks;
    _renderTracks();
  } catch (_) { /* keep what is shown */ }
});

// Playlist entry for a rendered track row: a bare id (default tune) OR
// {id, subsong} for a specific subsong.  Reorder / remove MUST round-trip this
// — sending a bare id for a subsong entry would flatten it to the default tune
// (reorder) or fail to match on the backend's (id, subsong) key (remove).
function _plEntry(t) {
  return Number.isInteger(t.subsong) ? { id: t.id, subsong: t.subsong } : t.id;
}
// Subsong key for local (id, subsong) matches: null = the default tune; an
// explicit 0 is tune 1, which need not be the default.
function _plSubKey(t) { const s = t && t.subsong; return (Number.isInteger(s) && s >= 0) ? s : null; }

// ── Track rows: a pooled virtual list (vlist.js) ──────────────────────────────
// Only the rows around the viewport exist; every event is delegated to
// ``tracksEl`` (below), and a track change only moves the now-playing marker
// (_paintPlaying) instead of rebuilding the list.
let _dup = { idCount: new Map(), idFirstAt: new Map(), contentMap: new Map() };
let _dragIdxs = null;            // indices being dragged (reorder), for rows filled mid-drag

function _makePlRow() {
  const row = document.createElement('div');
  row.className = 'queue-row';
  // Drag begins from the handle only — see the touch long-press below.  The
  // row itself becomes transiently draggable during a touch long-press.
  row.draggable = false;
  row.innerHTML = `
      <span class="queue-drag-handle" draggable="true" title="Drag to reorder">&#10783;</span>
      <span class="queue-playing-icon"></span>
      <div class="queue-row-art"><span class="qr-art-ph"></span><img class="qr-art-img" decoding="async" alt=""></div>
      <div class="queue-track-info">
        <div class="queue-track-titlerow"><span class="queue-track-title"></span></div>
        <span class="queue-track-artist"></span>
      </div>
      <span class="plx-fmt"></span>
      <span class="plx-wave-wrap" aria-hidden="true"><canvas class="plx-wave"></canvas></span>
      <span class="queue-track-dur"></span>
      <button class="queue-remove-btn" title="Remove from playlist">&times;</button>`;
  row.classList.add('plx-row');
  row.__wave = row.querySelector('.plx-wave');
  row.__fmt = row.querySelector('.plx-fmt');
  // The cover fades in on a successful decode; on error the <img> STAYS (it's
  // opacity:0, so the placeholder shows through and no broken glyph renders)
  // and just drops ``loaded``.  Keeping it in the DOM is what lets remote
  // (FTP/SMB) covers recover: the first request gets a 204 and fires a background
  // art-backfill, which broadcasts ``art_ready`` → app.js's _bustArtImg re-busts
  // every in-DOM /api/art/ <img> for that track.  Mirrors the library rows.
  const img = row.querySelector('.qr-art-img');
  img.onload  = () => img.classList.add('loaded');
  img.onerror = () => img.classList.remove('loaded');
  return row;
}

function _isCurrentEntry(track) {
  if (Player.startFailed) return false;          // (a start that failed: nothing plays)
  return Player.currentTrack?.id === track.id
    && _plSubKey(Player.currentTrack || {}) === _plSubKey(track);   // 0/undefined == default
}

// Pool callback: paint row ``i`` (text, badges, state); the cover is re-asked
// only when the row shows another track than before.
function _fillPlRow(row, i) {
  const track = _activeTracks[i];
  if (!track) return;
  const { idCount, idFirstAt, contentMap } = _dup;
  // ── Duplicate classification ────────────────────────────────────────────
  const dupKey       = _dupKey(track);
  const isIdDup      = idCount.get(dupKey) > 1 && idFirstAt.get(dupKey) !== i;
  const contentPeers = contentMap.get(_contentKey(track)) || [];
  const contentDups  = contentPeers.filter(p => p.id !== track.id);
  const isContentDup = contentDups.length > 0;
  const isCurrent  = _isCurrentEntry(track);

  // A playlist lists every entry ("Tracks on unavailable drives: Hide" never
  // drops one): an entry that can't play right now is marked instead.
  const offKind = offlineKind(track);
  row.className = 'queue-row plx-row'
    + (isCurrent  ? ' playing'  : '')
    + (_selectedIdxs.has(i) ? ' selected' : '')
    + (isIdDup    ? ' dup-id'   : '')
    + (offKind    ? ' track-offline' : '')
    + (_dragIdxs && _dragIdxs.has(i) ? ' dragging' : '');
  row.dataset.idx = i;
  row.querySelector('.queue-playing-icon').textContent = isCurrent ? '▶' : '';

  // ── Title row: title + "Tune n" + duplicate badge ───────────────────────
  let dupBadge = '';
  if (isIdDup) {
    dupBadge = `<span class="dup-badge dup-id-badge" title="This track appears more than once in this playlist — likely added by mistake">2×</span>`;
  } else if (isContentDup) {
    const others = contentDups.map(p => p.path || p.id).join('\n');
    dupBadge = `<span class="dup-badge dup-loc-badge" title="Same song also found at:\n${esc(others)}">⧉</span>`;
  }
  const tune = Number.isInteger(track.subsong)
    ? (() => { const n = subsongWireToTune(track.subsong, subsongStartOf(track), track.subsongs); return `<span class="qr-subsong" title="Tune ${n}">Tune ${n}</span>`; })()
    : '';
  const titleHtml = `<span class="queue-track-title" title="${esc(track.title)}">${esc(track.title || '—')}</span>${tune}${dupBadge}${healthBadgeHTML(track)}${offKind ? offlineBadgeHTML(offKind) : ''}`;
  const titleRow = row.querySelector('.queue-track-titlerow');
  if (row.__titleHtml !== titleHtml) { titleRow.innerHTML = titleHtml; row.__titleHtml = titleHtml; }
  row.querySelector('.queue-track-artist').textContent = track.artist || track.album_artist || '';
  row.querySelector('.queue-track-dur').textContent = fmtDur(track.duration);
  // Format chip + mini waveform (the playing row: with its progress).
  const fl = fmtShort(track), fam = fmtFamily(track);
  if (row.__fmtTxt !== fl || row.__fmtFam !== fam) {
    row.__fmt.textContent = fl;
    row.__fmt.className = `plx-fmt plx-fam-${fam}`;
    row.__fmt.title = track.format || fl;
    row.__fmtTxt = fl; row.__fmtFam = fam;
  }
  row.__trk = track;
  _paintWave(row, track, isCurrent ? _curPct : -1);

  // ── Cover: always ask /api/art (``track.cover_art`` is only set when art was
  // extracted at scan time; the endpoint extracts on demand).  ``fallback=none``
  // so an art-less track gets a 204 and the format-emoji placeholder shows instead of
  // the endpoint's generic ♪ JPEG.
  if (row.__artId !== track.id) {
    row.__artId = track.id;
    row.querySelector('.qr-art-ph').textContent = artPlaceholderEmoji(track);
    const img = row.querySelector('.qr-art-img');
    img.classList.remove('loaded');
    if (track.id) {
      const ep = window.__sbArtEpoch ? `&_t=${window.__sbArtEpoch}` : '';
      img.src = `/api/art/${track.id}/sm.jpg?fallback=none${ep}`;
    } else {
      img.removeAttribute('src');
    }
  }
}

const _plVL = createVirtualList({
  scroller: tracksEl, host: tracksEl, buffer: 8, rowHeight: 47, autoScroll: true,
  make: _makePlRow,
  fill: _fillPlRow,
  keyOf: (i) => _activeTracks[i],
  spacer: () => { const d = document.createElement('div'); d.className = 'vl-spacer'; d.style.flex = 'none'; return d; },
  setSpacer: (el, px) => { el.style.height = px + 'px'; },
});

// ── Mini waveforms ────────────────────────────────────────────────────────────
// The stored waveforms of the rows on screen, asked in one batch; a row whose
// answer arrives is repainted in place.
const _waves = createWaveStore({
  onReady(ids) {
    if (!_plVL.active) return;
    const got = new Set(ids);
    for (const row of _plVL.rows()) {
      const t = _activeTracks[+row.dataset.idx];
      if (t && got.has(t.id)) _paintWave(row, t, _isCurrentEntry(t) ? _curPct : -1);
    }
  },
});
let _curPct = 0;              // the playing entry's progress 0..1 (player timeupdate)
// A subsong entry gets the placeholder: the stored waveform is the file's
// default tune's.
const _painter = createWavePainter({ store: _waves, colorsFrom: panel, prefix: 'plx', stored: (t) => _plSubKey(t) === null });

// Layout / look / theme changed: measure + read the colours again, repaint.
function _waveRelayout() {
  _painter.invalidate(_plVL.active ? _plVL.rows() : []);
  if (!_plVL.active) return;
  for (const row of _plVL.rows()) {
    const t = _activeTracks[+row.dataset.idx];
    if (t) _paintWave(row, t, _isCurrentEntry(t) ? _curPct : -1);
  }
}

// Paint ``row``'s mini waveform.
function _paintWave(row, track, progress = -1) { _painter.paint(row, track, progress); }

// The rendered rows of the playing entry (an entry in the list twice: both).
function _playingRows() {
  const out = [];
  if (!_plVL.active) return out;
  for (const row of _plVL.rows()) if (row.classList.contains('playing')) out.push(row);
  return out;
}

// Animated touches (the progress sheen, Retro's turning reel) only while
// visualizations are on and motion is welcome; and only while it plays.
const _rmq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
function _syncMotion() {
  panel.classList.toggle('plx-anim', vizGroupEnabled('nowPlaying') && !(_rmq && _rmq.matches));
  panel.classList.toggle('plx-is-playing', !!Player.playing && !Player.startFailed);
}
window.addEventListener('sb:viz-settings', _syncMotion);
if (_rmq && _rmq.addEventListener) _rmq.addEventListener('change', _syncMotion);

// Move the now-playing marker on the rendered rows (a track change), and show
// a length the player has learnt meanwhile (rendered formats).
function _paintPlaying() {
  _syncMotion();
  _paintHeroPlaying();
  if (!_plVL.active) return;
  for (const row of _plVL.rows()) {
    const track = _activeTracks[+row.dataset.idx];
    const dur = row.querySelector('.queue-track-dur');
    if (track && dur) { const txt = fmtDur(track.duration); if (dur.textContent !== txt) dur.textContent = txt; }
    const on = !!track && _isCurrentEntry(track);
    if (row.classList.contains('playing') === on) continue;
    row.classList.toggle('playing', on);
    row.querySelector('.queue-playing-icon').textContent = on ? '▶' : '';
    if (track) _paintWave(row, track, on ? _curPct : -1);
  }
}

// (Re)paint the open playlist after its tracks changed.  ``toTop``: a playlist
// that was just opened starts at its first row; edits keep the scroll position.
function _renderTracks(opts = {}) {
  _updateSelBar();
  _renderHero();

  if (!_activeTracks.length) {
    _plVL.reset();
    tracksEl.innerHTML = '<div class="queue-empty">No tracks yet.<br>Drag from library or use "Add to Playlist".</div>';
    return;
  }

  _dup = _buildDupMaps(_activeTracks);
  _dragIdxs = null;                // indices changed: no drag carries over
  if (opts.toTop) tracksEl.scrollTop = 0;
  _plVL.setCount(_activeTracks.length);
  _plVL.render(true);

  // Fill real AdLib/IMF lengths for rows still showing the 180s placeholder.
  _probePlaylistDurations();
}

// A click on a row focuses the list (not in the Tab order), so Delete / ⌫ after
// a mouse selection reaches the panel's key handler below.
tracksEl.tabIndex = -1;

// ── Delegated row events ───────────────────────────────────────────────────────
function _plRowOf(e) {
  const row = e.target.closest('.queue-row');
  return row && tracksEl.contains(row) && row.dataset.idx != null ? row : null;
}

// ── Touch long-press to begin drag (playlist/listkit.js) ─────────────────
// A 350 ms hold on the handle, without moving more than 8 px, makes the row
// draggable — a swipe that scrolls never starts a drag on a touch screen.
bindLongPressDrag(tracksEl, _plRowOf);

// ── Click: play / select / remove ───────────────────────────────────────────
tracksEl.addEventListener('click', async (e) => {
  const row = _plRowOf(e);
  if (!row) return;
  const i = parseInt(row.dataset.idx, 10);
  const track = _activeTracks[i];
  if (!track) return;

  if (e.target.closest('.queue-remove-btn')) {
    e.stopPropagation();
    await _removeTrack(_activeId, track);
    return;
  }

  if (e.metaKey || e.ctrlKey) {
    // Toggle this row
    if (_selectedIdxs.has(i)) _selectedIdxs.delete(i);
    else { _selectedIdxs.add(i); _anchorIdx = i; }
    _updateSelBar();
    _refreshRowClasses();
  } else if (e.shiftKey && _anchorIdx !== null) {
    // Range select from anchor to here
    const lo = Math.min(_anchorIdx, i), hi = Math.max(_anchorIdx, i);
    for (let j = lo; j <= hi; j++) _selectedIdxs.add(j);
    _updateSelBar();
    _refreshRowClasses();
  } else if (_selectedIdxs.size > 0) {
    // Plain click while selection active → clear selection (don't play)
    _clearSelection(false);
    _refreshRowClasses();
  } else {
    // Plain click, nothing selected → play
    _anchorIdx = i;
    _noteQueued();
    Player.setQueue(_activeTracks, i);
  }
});

// ── Drag start / end (reorder) ─────────────────────────────────────────────
// Own, ordinary playlists only: an imported one is read-only and a smart one's
// order is its saved search's.
function _canReorder() { return !!_activeId && !_activeRO && !_activeSmart; }

tracksEl.addEventListener('dragstart', (e) => {
  if (!_canReorder()) { e.preventDefault(); return; }
  const row = _plRowOf(e);
  if (!row) return;
  const i = parseInt(row.dataset.idx, 10);
  e.dataTransfer.effectAllowed = 'move';
  // If this row is in the selection drag all selected; otherwise drag just this row
  const dragging = _selectedIdxs.has(i)
    ? [..._selectedIdxs].sort((a, b) => a - b)
    : [i];
  e.dataTransfer.setData('application/x-soniqboom-pl-idx', JSON.stringify(dragging));
  _dragIdxs = new Set(dragging);
  for (const r of _plVL.rows()) {
    if (_dragIdxs.has(+r.dataset.idx)) r.classList.add('dragging');
  }
});

// The drop line: where the dragged rows will land (a gap between two rows).
const _dropLine = createDropLine(panel);
let _dropGap = -1;            // insert before this index (0..count); -1 = none

function _hideDropLine() { _dropLine.hide(); _dropGap = -1; }

// The gap the pointer points at: before the row under it (upper half) or after
// it (lower half); below the last row → the end.  null = not over the list.
function _gapAt(e) {
  const row = _plRowOf(e);
  if (row) return gapAt(e, row, null, _activeTracks.length);
  let last = null;
  for (const r of _plVL.rows()) if (!last || +r.dataset.idx > +last.dataset.idx) last = r;
  return gapAt(e, null, last, _activeTracks.length);
}

// A drop into the dragged block itself (or right next to a contiguous one)
// changes nothing.
function _isNoopGap(gap) {
  if (!_dragIdxs || !_dragIdxs.size) return false;
  const idx = [..._dragIdxs].sort((a, b) => a - b);
  const contiguous = idx[idx.length - 1] - idx[0] === idx.length - 1;
  return contiguous && gap >= idx[0] && gap <= idx[idx.length - 1] + 1;
}

function _showDropLine(g) {
  if (!g || _isNoopGap(g.gap)) { _hideDropLine(); return; }
  _dropGap = g.gap;
  _dropLine.show(g.y, tracksEl);
}

function _endPlDrag() {
  _dragIdxs = null;
  _hideDropLine();
  tracksEl.querySelectorAll('.queue-row.dragging, .queue-row.dragging-over')
    .forEach(r => r.classList.remove('dragging', 'dragging-over'));
  // Drop the transient draggable flag (set during a touch long-press) so the
  // next tap on the row isn't treated as a drag-handle gesture.
  tracksEl.querySelectorAll('.queue-row[draggable="true"]').forEach(r => { r.draggable = false; });
}
tracksEl.addEventListener('dragend', _endPlDrag);

// Rows glide to their new places after a reorder (FLIP): remember where each
// entry's row was, re-render, then animate every moved row from there.
function _rowTops() { return rowTops(_plVL.rows()); }
function _clearFlip() { clearFlip(_plVL.rows()); }
function _flipFrom(before) { flipFrom(_plVL.rows(), before, tracksEl, !!(_rmq && _rmq.matches)); }

// Library tracks dropped on the list add them; playlist rows dropped between
// rows reorder.  (The list may have scrolled — and recycled the dragged row —
// while dragging, so the drop goes by the indices carried in the drag data.)
tracksEl.addEventListener('dragover', (e) => {
  const types = e.dataTransfer.types;
  if (types.includes('application/x-soniqboom-pl-idx')) {
    if (!_canReorder()) return;
    const g = _gapAt(e);
    if (!g) { _hideDropLine(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    _showDropLine(g);
    return;
  }
  if (_activeId && !_activeRO && !_activeSmart && types.includes('application/x-soniqboom-track')) {
    e.preventDefault();
    dropZone.classList.add('drag-active');
  }
});
tracksEl.addEventListener('dragleave', (e) => {
  // (Chromium often leaves relatedTarget null between two rows: go by where the
  // pointer is, so the line doesn't flicker from row to row.)
  const r = tracksEl.getBoundingClientRect();
  const out = e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom;
  if (out || (e.relatedTarget && !tracksEl.contains(e.relatedTarget))) {
    dropZone.classList.remove('drag-active');
    _hideDropLine();
  }
});
tracksEl.addEventListener('drop', async (e) => {
  if (_activeRO || _activeSmart) { e.preventDefault(); _endPlDrag(); return; }
  const raw = e.dataTransfer.getData('application/x-soniqboom-pl-idx');
  if (raw || e.dataTransfer.types.includes('application/x-soniqboom-pl-idx')) {
    // ── Drop: reorder ──────────────────────────────────────────────────────
    e.preventDefault();
    const g = _gapAt(e);
    const gap = g ? g.gap : _dropGap;
    // (No drag of ours in flight — the list re-rendered meanwhile, or the rows
    // came from another tab — so the carried indices may name other entries.)
    if (gap < 0 || !raw || !_activeId || !_dragIdxs) { _endPlDrag(); return; }

    let fromIndices;
    try { fromIndices = JSON.parse(raw); }
    catch { _endPlDrag(); return; }
    if (!Array.isArray(fromIndices)) fromIndices = [fromIndices];
    fromIndices = fromIndices.filter(i => Number.isInteger(i) && i >= 0 && i < _activeTracks.length);
    if (!fromIndices.length || _isNoopGap(gap)) { _endPlDrag(); return; }

    // Keep a snapshot for rollback so we can revert the local order if
    // the server PUT fails — the optimistic UI shouldn't lie to the user
    // about what was persisted.
    const previousOrder = _activeTracks.slice();
    const fromSet  = new Set(fromIndices);
    const toInsert = fromIndices.map(idx => _activeTracks[idx]);
    const rest     = _activeTracks.filter((_, idx) => !fromSet.has(idx));
    // Each dragged row above the gap shifts the insertion point up by one.
    const insertAt = Math.max(0, Math.min(rest.length, gap - fromIndices.filter(idx => idx < gap).length));
    rest.splice(insertAt, 0, ...toInsert);
    if (rest.every((t, k) => t === previousOrder[k])) { _endPlDrag(); return; }

    const tops = _rowTops();
    _activeTracks = rest;
    _clearSelection();
    _endPlDrag();
    _renderTracks();
    _flipFrom(tops);
    const id = _activeId;
    try {
      await _api(`/playlists/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: _activeTracks.map(_plEntry) }),
      });
    } catch (err) {
      // Revert the optimistic local order and re-render so the displayed
      // state matches what's actually on the server.
      if (_activeId === id) { _activeTracks = previousOrder; _clearFlip(); _renderTracks(); }
      console.warn('Drag-reorder save failed:', err);
      Toast.error("Reorder couldn't be saved — order reverted.");
    }
    return;
  }
  // ── Drop: library tracks onto the list → add ───────────────────────────
  if (!e.dataTransfer.types.includes('application/x-soniqboom-track')) return;
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    const ids = tracks.map(t => t?.id).filter(Boolean);
    if (ids.length && _activeId) await _addTracks(_activeId, ids);
  } catch (_) {}
});

// Background-fill AdLib/IMF durations for the rendered rows (one-time per track;
// shared dedup with the library + mobile via probeAdlibDurations).
async function _probePlaylistDurations() {
  const map = await probeAdlibDurations(_activeTracks);
  for (const id in map) {
    const sec = map[id];
    if (!(sec > 0)) continue;
    const idx = _activeTracks.findIndex(t => t && t.id === id);
    if (idx < 0) continue;
    _activeTracks[idx].duration = sec;
    const span = _plVL.rowFor(idx)?.querySelector('.queue-track-dur');
    if (span) span.textContent = fmtDur(sec);
  }
}

// ── Header (hero) ─────────────────────────────────────────────────────────────
// Who shares it: an imported one names its owner; the owner's own shared one
// says how many listeners imported it (never who — the API doesn't say).
function _heroKicker() {
  const d = _activeData || {};
  const summary = _playlists.find(p => p.id === _activeId) || {};
  const parts = [_activeSmart ? 'Smart playlist' : 'Playlist'];
  if (_activeRO) {
    parts.push(`shared by ${d.owner_name || summary.owner_name || 'another user'}`, 'imported');
  } else if (summary.shared) {
    const n = Number(d.subscriber_count ?? summary.subscriber_count ?? 0) || 0;
    parts.push(n ? `shared · ${n} listener${n !== 1 ? 's' : ''} imported it` : 'shared · not imported yet');
  }
  return parts.join(' · ');
}

// The player's queue is still the one this playlist handed it (another list's
// play replaces it with other objects) and its current entry is one of ours.
function _playingHere() {
  if (!_activeId || _queuedFrom !== _activeId || !Player.currentTrack) return false;
  const q = Player.queue || [];
  if (!(_queuedObjs.has(Player.currentTrack) || q.some(t => _queuedObjs.has(t)))) return false;
  return _activeTracks.some(t => _isCurrentEntry(t));
}
function _noteQueued() {
  _queuedFrom = _activeId;
  _queuedObjs = new Set(_activeTracks);
}

function _renderHero() {
  if (!_activeId) return;
  _hero.render({
    name: plActiveNm.textContent,
    tracks: _activeTracks,
    kicker: _heroKicker(),
    playingHere: _playingHere(),
    playing: Player.playing,
    style: getUiStyle(),
  });
}
function _paintHeroPlaying() {
  if (_activeId) _hero.setPlaying(_playingHere(), Player.playing);
}

function _heroPlay() {
  if (!_activeTracks.length) return;
  if (_playingHere()) { Player.playPause(); return; }
  _noteQueued();
  Player.setQueue(_activeTracks, 0, { explicit: false });
}
function _heroShuffle() {
  if (!_activeTracks.length) return;
  _noteQueued();
  Player.setQueue(_activeTracks, 0, { shuffle: true });
}

function _heroMenuItems() {
  const id = _activeId;
  if (!id) return [];
  const items = [];
  if (_activeTracks.length) {
    items.push({ label: 'Add all to queue', run: () => {
      Player.addAllToQueue(_activeTracks);
      Toast.ok(`Added ${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''} to the queue.`);
    } });
  }
  items.push({ label: 'Export as M3U…', run: () => _exportPlaylist(id, 'm3u8') });
  items.push({ label: 'Export as XSPF…', run: () => _exportPlaylist(id, 'xspf') });
  if (_activeRO) {
    if (btnUnsub) items.push({ label: 'Unsubscribe', danger: true, run: () => btnUnsub.click() });
    return items;
  }
  items.push({ label: 'Rename…', run: async () => {
    const pl = _playlists.find(p => p.id === id);
    const name = await _promptName('Rename playlist', { okLabel: 'Rename', value: pl ? pl.name : plActiveNm.textContent });
    if (!name || !name.trim() || _activeId !== id) return;
    try {
      await _api(`/playlists/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() }),
      });
      plActiveNm.textContent = name.trim();
      plActiveNm.title = name.trim();
      await refresh();
    } catch (err) {
      Toast.error('Could not rename the playlist.');
    }
  } });
  items.push({ label: 'Delete playlist…', danger: true, run: async () => {
    const pl = _playlists.find(p => p.id === id);
    const ok = await window.__sbConfirm(
      `Delete "${pl ? pl.name : plActiveNm.textContent}"?`,
      { title: 'Delete Playlist', okLabel: 'Delete' },
    );
    if (!ok) return;
    try {
      await _api(`/playlists/${id}`, { method: 'DELETE' });
      if (_activeId === id) _showListView();
      await refresh();
    } catch (err) {
      Toast.error('Could not delete playlist — the server rejected the request.');
    }
  } });
  return items;
}

// The look (Modern / Retro hi-fi) is the shared UI style (js/ui_style.js).
function _applyStyle(style) {
  panel.dataset.plStyle = style;
  _waveRelayout();
  if (_plVL.active) _plVL.remeasure();
}
onUiStyleChange(_applyStyle);

// The theme (light / dark) changes the waveform colours.
new MutationObserver(() => _waveRelayout())
  .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
if (typeof ResizeObserver !== 'undefined') {
  let _lastW = 0;
  new ResizeObserver((ents) => {
    const w = Math.round(ents[0]?.contentRect?.width || 0);
    if (w && w !== _lastW) { _lastW = w; _waveRelayout(); }
  }).observe(tracksEl);
}
// The narrow-window rule resizes the waveform while the list keeps its width.
try { window.matchMedia(NARROW_WAVES).addEventListener('change', () => _waveRelayout()); } catch (_) { /* old engine */ }

// ── Mutations ─────────────────────────────────────────────────────────────────
async function _addTracks(playlistId, trackIds) {
  // (someone else's playlist, or a smart one — its saved search picks its tracks)
  if (playlistId === _activeId && (_activeRO || _activeSmart)) return;
  try {
    await _api(`/playlists/${playlistId}/tracks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_ids: trackIds }),
    });
    if (playlistId === _activeId) {
      const data    = await _api(`/playlists/${playlistId}`);
      _activeTracks = data.tracks || [];
      plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
      _renderTracks();
    }
    await refresh();
  } catch (err) {
    console.warn('Failed to add to playlist:', err);
    Toast.error('Add to playlist failed — the change was not saved.');
  }
}

async function _removeTrack(playlistId, track) {
  if (_activeRO || _activeSmart) return;
  const entry = _plEntry(track);
  const subKey = _plSubKey(track);
  const prev = _activeTracks.slice();      // rollback snapshot (2.6 optimistic)
  // Optimistic: drop it locally + repaint BEFORE the round-trip.  Match on
  // (id, subsong) so removing one tune doesn't drop the other tunes of the file.
  _activeTracks = _activeTracks.filter(t => !(t.id === track.id && _plSubKey(t) === subKey));
  plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
  _clearSelection();
  _renderTracks();
  try {
    await _api(`/playlists/${playlistId}/tracks`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_ids: [entry] }),
    });
    refresh();                             // sync sidebar count in the background
  } catch (err) {
    _activeTracks = prev;                  // rollback — the optimistic UI must not lie
    plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
    _renderTracks();
    console.warn('Failed to remove track:', err);
    Toast.error('Remove failed — the track was restored.');
  }
}

async function _removeSelected() {
  if (!_selectedIdxs.size || !_activeId || _activeRO || _activeSmart) return;
  // Multi-track delete is a destructive action.  Single-row removal stays
  // instant — the row's own "x" button is already an explicit gesture — but
  // sweeping out a multi-row selection (especially via Delete/Backspace)
  // deserves an explicit OK first.
  // Always confirm — the selection-bar "Remove" reaches here regardless of
  // selection size, and a single accidental keystroke shouldn't drop a
  // track without a chance to cancel.  The row-level "x" button has its
  // own behaviour and isn't affected.
  // __sbConfirm is installed synchronously at module load, so the
  // OS-native confirm fallback is gone (it didn't support title/okLabel).
  const promptText = _selectedIdxs.size > 1
    ? `Remove ${_selectedIdxs.size} tracks from this playlist?`
    : `Remove this track from the playlist?`;
  const ok = await window.__sbConfirm(
    promptText,
    { title: 'Remove Tracks', okLabel: 'Remove' },
  );
  if (!ok) return;
  const indices    = [..._selectedIdxs].sort((a, b) => a - b);
  const idsToRemove = indices.map(i => _plEntry(_activeTracks[i]));
  const prev = _activeTracks.slice();      // rollback snapshot (2.6 optimistic)
  // Optimistic: drop the selected rows + repaint before the round-trip
  // (filter reads _selectedIdxs before _clearSelection wipes it).
  _activeTracks = _activeTracks.filter((_, i) => !_selectedIdxs.has(i));
  plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
  _clearSelection();
  _renderTracks();
  try {
    await _api(`/playlists/${_activeId}/tracks`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_ids: idsToRemove }),
    });
    refresh();                             // sync sidebar count in the background
  } catch (err) {
    _activeTracks = prev;                  // rollback
    plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
    _renderTracks();
    console.warn('Failed to remove selected:', err);
    Toast.error('Remove failed — the tracks were restored.');
  }
}

function _updateSidebarActive() {
  if (!sidebarList) return;
  sidebarList.querySelectorAll('.sidebar-playlist-item')
    .forEach(li => li.classList.toggle('active', li.dataset.id === _activeId));
}

// ── Custom name prompt (no browser dialog) ────────────────────────────────────
function _promptName(title = 'New playlist name', { okLabel = 'Create', value = '' } = {}) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'pl-modal-backdrop';
    const dialog = document.createElement('div');
    dialog.className = 'pl-modal-dialog';
    dialog.innerHTML = `
      <div class="pl-modal-title">${title}</div>
      <input class="pl-modal-input" type="text" placeholder="Name…" autocomplete="off" spellcheck="false" maxlength="120">
      <div class="pl-modal-actions">
        <button class="pl-modal-btn pl-modal-cancel">Cancel</button>
        <button class="pl-modal-btn pl-modal-ok"></button>
      </div>
    `;
    dialog.querySelector('.pl-modal-ok').textContent = okLabel;
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    const input  = dialog.querySelector('.pl-modal-input');
    if (value) { input.value = value; input.select(); }
    const btnOk  = dialog.querySelector('.pl-modal-ok');
    const btnCan = dialog.querySelector('.pl-modal-cancel');
    input.focus();
    function finish(value) {
      backdrop.classList.add('pl-modal-out');
      setTimeout(() => backdrop.remove(), 150);
      resolve(value ?? null);
    }
    btnOk.addEventListener('click',  () => { const v = input.value.trim(); if (v) finish(v); });
    btnCan.addEventListener('click', () => finish(null));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish(null); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter')  { const v = input.value.trim(); if (v) finish(v); }
      if (e.key === 'Escape') finish(null);
    });
  });
}

async function createPlaylist() {
  const name = await _promptName();
  if (!name?.trim()) return;
  try {
    const pl = await _api('/playlists', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    });
    await refresh();
    open();
    await _openPlaylist(pl.id ?? pl.playlist_id, name.trim());
  } catch (err) {
    console.warn('Failed to create playlist:', err);
    Toast.error('Could not create playlist — the server rejected the request.');
  }
}

// ── Drop zone ─────────────────────────────────────────────────────────────────
dropZone.addEventListener('dragover', (e) => {
  if (!_activeId || _activeRO || _activeSmart) return;
  if (e.dataTransfer.types.includes('application/x-soniqboom-track')) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dropZone.classList.add('drag-active');
  }
});
dropZone.addEventListener('dragleave', (e) => {
  if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('drag-active');
});
dropZone.addEventListener('drop', async (e) => {
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  if (!_activeId || _activeRO || _activeSmart) return;
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    const ids = tracks.map(t => t?.id).filter(Boolean);
    if (ids.length) await _addTracks(_activeId, ids);
  } catch (_) {}
});

// (Drops on the tracks area itself — reorder or add — are handled by the
// delegated listeners next to _renderTracks.)

// ── Keyboard: Delete/⌫ removes selected tracks ────────────────────────────────
panel.addEventListener('keydown', (e) => {
  if ((e.key === 'Delete' || e.key === 'Backspace')
      && _selectedIdxs.size > 0
      && !tracksEl.hidden
      && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLButtonElement)) {
    e.preventDefault();
    _removeSelected();
  }
});

// ── Selection bar buttons ─────────────────────────────────────────────────────
btnSelRm ?.addEventListener('click', () => _removeSelected());
btnSelClr?.addEventListener('click', () => _clearSelection(true));

// ── Panel button wiring ───────────────────────────────────────────────────────
btnClose.addEventListener('click', () => close());
btnClose2?.addEventListener('click', () => close());
btnBack.addEventListener('click', () => _showListView());
btnNew.addEventListener('click', (e) => { e.stopPropagation(); createPlaylist(); });
btnSideNew?.addEventListener('click', (e) => { e.stopPropagation(); createPlaylist(); });
btnImport?.addEventListener('click', (e) => { e.stopPropagation(); showImportDialog(); });
btnShare?.addEventListener('click', async () => {
  const id = _activeId;
  const pl = _playlists.find(p => p.id === id);
  if (!id || !pl) return;
  try {
    await _api(`/playlists/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shared: !pl.shared }),
    });
    Toast.info(pl.shared ? 'No longer shared — users who imported it lose it.'
                         : 'Shared — every user can now import it (read-only).');
    await refresh();
  } catch (err) {
    Toast.error('Could not change sharing.');
  }
});
btnUnsub?.addEventListener('click', async () => {
  const id = _activeId;
  if (!id) return;
  try { await _setSubscribed(id, false); _showListView(); await refresh(); }
  catch (err) { Toast.error('Could not remove the imported playlist.'); }
});

// ── Playlist files: export (⋯ menu) and import (the Import dialog) ─────────
// GET /api/playlists/{id}/export?format=m3u8|xspf answers with the file as an
// attachment; POST /api/playlists/import makes a new playlist from a file's
// text and says which entries it didn't find (core/playlist_files.py).
async function _exportPlaylist(id, format) {
  let blob, name = `playlist.${format}`;
  try {
    const res = await fetch(`/api/playlists/${encodeURIComponent(id)}/export?format=${format}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cd = res.headers.get('content-disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="([^"]+)"/i);
    if (m) { try { name = decodeURIComponent(m[1]); } catch (_) { name = m[1]; } }
    blob = await res.blob();
  } catch (err) {
    Toast.error("Couldn't export the playlist.");
    return;
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

const _IMPORT_MAX_BYTES = 5 * 1024 * 1024;
// The file's text: UTF-16 with its BOM, UTF-8 (M3U8, XSPF), else
// Windows-1252 (an older M3U).
async function _readPlaylistFile(file) {
  const buf = await file.arrayBuffer();
  const b = new Uint8Array(buf, 0, Math.min(2, buf.byteLength));
  if (b.length === 2 && ((b[0] === 0xff && b[1] === 0xfe) || (b[0] === 0xfe && b[1] === 0xff))) {
    return new TextDecoder(b[0] === 0xff ? 'utf-16le' : 'utf-16be').decode(buf);
  }
  try { return new TextDecoder('utf-8', { fatal: true }).decode(buf); }
  catch (_) { return new TextDecoder('windows-1252').decode(buf); }
}

async function _importPlaylistFile(file, out) {
  out.hidden = false;
  out.textContent = `Reading ${file.name}…`;
  if (file.size > _IMPORT_MAX_BYTES) {
    out.textContent = 'This file is too large to be a playlist (over 5 MB).';
    return;
  }
  let res, body;
  try {
    const content = await _readPlaylistFile(file);
    res = await fetch('/api/playlists/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, content }),
    });
    body = await res.json().catch(() => null);
  } catch (err) {
    out.textContent = "Couldn't import the file — the server didn't answer.";
    return;
  }
  const detail = body && body.detail;
  if (!res.ok) {
    const msg = typeof detail === 'string' ? detail
      : (detail && detail.message)
      || (Array.isArray(detail) ? "This file can't be read as a playlist (too large or not text)."
                                : `Couldn't import the file (error ${res.status}).`);
    out.textContent = msg;
    if (detail && Array.isArray(detail.unmatched)) _listUnmatched(out, detail.unmatched, detail.unmatched_count);
    return;
  }
  const pl = body.playlist || {};
  out.textContent = '';
  const head = document.createElement('div');
  head.className = 'pl-import-done';
  head.textContent = `Made “${pl.name}”: ${body.matched.toLocaleString()} of ${body.total.toLocaleString()} track${body.total !== 1 ? 's' : ''} found.`;
  out.appendChild(head);
  if (body.truncated) {
    const t = document.createElement('div');
    t.className = 'pl-import-hint';
    t.textContent = 'The file lists more than 20,000 tracks — the first 20,000 were read.';
    out.appendChild(t);
  }
  if (body.unmatched_count) _listUnmatched(out, body.unmatched, body.unmatched_count);
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'pl-modal-btn pl-modal-cancel pl-import-open';
  open.textContent = 'Open the playlist';
  open.addEventListener('click', () => {
    out.closest('.pl-modal-backdrop')?.querySelector('.pl-modal-ok')?.click();
    if (pl.id) _openPlaylist(pl.id, pl.name);
  });
  out.appendChild(open);
  await refresh();
}

function _listUnmatched(out, rows, count) {
  const hint = document.createElement('div');
  hint.className = 'pl-import-hint';
  hint.textContent = `Not found (${count.toLocaleString()}):`;
  const ul = document.createElement('ul');
  ul.className = 'pl-import-missing';
  for (const r of rows) {
    const li = document.createElement('li');
    const what = document.createElement('span');
    what.textContent = r.entry;
    what.title = r.entry;
    const why = document.createElement('span');
    why.className = 'pl-import-meta';
    why.textContent = r.reason;
    li.append(what, why);
    ul.appendChild(li);
  }
  if (count > rows.length) {
    const li = document.createElement('li');
    li.className = 'pl-import-meta';
    li.textContent = `…and ${(count - rows.length).toLocaleString()} more`;
    ul.appendChild(li);
  }
  out.append(hint, ul);
}

// ── Import dialog: a playlist file, and the playlists other users share
//    (ticked = in my list) ─────────────────────────────────────────────────
async function showImportDialog() {
  let rows;
  try { rows = await _api('/playlists/shared'); }
  catch (err) { Toast.error("Couldn't load the shared playlists."); return; }
  const backdrop = document.createElement('div');
  backdrop.className = 'pl-modal-backdrop';
  const dialog = document.createElement('div');
  dialog.className = 'pl-modal-dialog pl-import-dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-label', 'Import playlists');
  dialog.innerHTML = `
    <div class="pl-modal-title">Import playlists</div>
    <div class="pl-import-section">A playlist file</div>
    <div class="pl-import-hint">An M3U, M3U8 or XSPF file — its tracks are found in your library by file, folder or artist and title.</div>
    <div class="pl-import-file">
      <button type="button" class="pl-modal-btn pl-modal-cancel pl-import-choose">Choose a file…</button>
      <input type="file" class="pl-import-input" accept=".m3u,.m3u8,.xspf,audio/x-mpegurl,audio/mpegurl,application/xspf+xml" hidden>
    </div>
    <div class="pl-import-result" role="status" aria-live="polite" hidden></div>
    <div class="pl-import-section">Shared by other users</div>
    <div class="pl-import-hint">Ticked playlists appear in your list, read-only — they follow their owner's changes.</div>
    <div class="pl-import-list"></div>
    <div class="pl-modal-actions"><button class="pl-modal-btn pl-modal-ok">Done</button></div>`;
  const fileInput = dialog.querySelector('.pl-import-input');
  const result = dialog.querySelector('.pl-import-result');
  dialog.querySelector('.pl-import-choose').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';               // (the same file can be chosen again)
    if (f) _importPlaylistFile(f, result);
  });
  const list = dialog.querySelector('.pl-import-list');
  if (!rows.length) {
    list.innerHTML = '<div class="playlist-empty">No other user shares a playlist yet.</div>';
  }
  for (const r of rows) {
    const lab = document.createElement('label');
    lab.className = 'pl-import-row';
    const cnt = r.smart ? 'smart' : `${r.track_count} track${r.track_count !== 1 ? 's' : ''}`;
    lab.innerHTML = `<input type="checkbox"> <span class="pl-import-name"></span>
      <span class="pl-import-meta"></span>`;
    lab.querySelector('.pl-import-name').textContent = r.name;
    lab.querySelector('.pl-import-meta').textContent = `by ${r.owner_name || '?'} · ${cnt}`;
    const box = lab.querySelector('input');
    box.checked = !!r.subscribed;
    box.dataset.id = r.id;
    box.addEventListener('change', async () => {
      box.disabled = true;
      try { await _setSubscribed(r.id, box.checked); await refresh(); }
      catch (err) { box.checked = !box.checked; Toast.error('Could not change the import.'); }
      box.disabled = false;
    });
    list.appendChild(lab);
  }
  backdrop.appendChild(dialog);
  document.body.appendChild(backdrop);
  const done = () => { backdrop.classList.add('pl-modal-out'); setTimeout(() => backdrop.remove(), 150); };
  dialog.querySelector('.pl-modal-ok').addEventListener('click', done);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) done(); });
  dialog.addEventListener('keydown', (e) => { if (e.key === 'Escape') done(); });
  dialog.querySelector('.pl-modal-ok').focus();
}

// ── Panel visibility ──────────────────────────────────────────────────────────
function open() {
  document.dispatchEvent(new CustomEvent('panelopen', { detail: { panel: 'playlist' } }));
  panel.classList.remove('hidden');
  if (!_activeId) _renderListView();
}
function close() {
  panel.classList.add('hidden');
  _closeAddDropdown();
}
function toggle() {
  if (panel.classList.contains('hidden')) open(); else close();
}

// Close when another panel opens
document.addEventListener('panelopen', (e) => {
  if (e.detail?.panel !== 'playlist') close();
});

// ── "Add to Playlist" dropdown ────────────────────────────────────────────────
function showAddDropdown(anchorEl) {
  const selectedTracks = Library.getSelectedTracks();
  if (!selectedTracks.length) return;
  _showAddDropdownWith(anchorEl, () => selectedTracks.map(t => t.id), () => Library.clearSelection());
}

// Add specific entries (bare ids OR {id, subsong} objects) to a chosen
// playlist — used by the Track Info subsong picker to pin one tune or all N,
// and by the track context menu (the tracks it was opened on).  ``onDone``
// runs once they were added.
function showAddDropdownForEntries(anchorEl, entries, onDone = null) {
  if (!entries || !entries.length) return;
  _showAddDropdownWith(anchorEl, () => entries, onDone);
}

function _showAddDropdownWith(anchorEl, getEntries, onDone) {
  _closeAddDropdown();
  // Capture the anchor's position synchronously — a caller may remove the
  // anchor element right after opening us, before this fetch resolves, and a
  // late getBoundingClientRect() would then read all-zeros.
  const rect = anchorEl.getBoundingClientRect();

  _api('/playlists').then(all => {
    _playlists = all;
    // (imported ones can't be edited; a smart one's saved search picks its tracks)
    const playlists = all.filter(pl => !pl.read_only && !pl.smart);
    const dd = document.createElement('div');
    dd.className = 'playlist-add-dropdown';
    dd.style.cssText = 'position:fixed;z-index:9999';

    if (!playlists.length) {
      dd.innerHTML = `<div class="playlist-dd-empty">No playlists — click + New.</div>
        <div class="playlist-dd-item playlist-dd-create">+ Create new playlist</div>`;
      dd.querySelector('.playlist-dd-create').addEventListener('click', () => {
        _closeAddDropdown(); createPlaylist();
      });
    } else {
      playlists.forEach(pl => {
        const item = document.createElement('div');
        item.className = 'playlist-dd-item';
        item.textContent = pl.name;
        item.addEventListener('click', () => {
          _addTracks(pl.id, getEntries());
          if (onDone) onDone();
          _closeAddDropdown();
        });
        dd.appendChild(item);
      });
      const create = document.createElement('div');
      create.className = 'playlist-dd-item playlist-dd-create';
      create.textContent = '+ New playlist…';
      create.addEventListener('click', () => { _closeAddDropdown(); createPlaylist(); });
      dd.appendChild(create);
    }

    dd.style.left   = rect.left + 'px';
    dd.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
    document.body.appendChild(dd);
    _addDropdown = dd;
    setTimeout(() => document.addEventListener('click', _handleOutsideClick), 0);
  }).catch((err) => {
    console.warn('Failed to load playlists for dropdown:', err);
    Toast.error("Couldn't load playlists — please try again.");
  });
}

function _handleOutsideClick(e) {
  if (_addDropdown && !_addDropdown.contains(e.target)) _closeAddDropdown();
}
function _closeAddDropdown() {
  if (_addDropdown) {
    _addDropdown.remove();
    _addDropdown = null;
    document.removeEventListener('click', _handleOutsideClick);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
refresh();

// A track change only moves the now-playing marker on the rows shown.
let _waveRecheck = [];
Player.on('trackchange', () => {
  _curPct = 0;
  _paintPlaying();
  // A track's first play stores its waveform: ask again for it a little later.
  _waveRecheck.forEach(clearTimeout);
  const id = Player.currentTrack && Player.currentTrack.id;
  if (!id) return;
  // (The same steps as the player bar's own waveform retries, ~1 min in all.)
  _waveRecheck = [5000, 15000, 30000, 60000].map(ms => setTimeout(() => {
    if (_waves.state(id) !== null) return;
    _waves.forget(id);
    for (const row of _playingRows()) {
      if (row.__trk && row.__trk.id === id) _paintWave(row, row.__trk, _curPct);
    }
  }, ms));
});
Player.on('statechange', () => _paintPlaying());
// The playing row's progress inside its mini waveform (≈4 Hz, no timer).
Player.on('timeupdate', (d) => {
  _curPct = d && d.duration ? Math.max(0, Math.min(1, d.current / d.duration)) : 0;
  if (panel.classList.contains('hidden') || tracksEl.hidden) return;
  for (const row of _playingRows()) if (row.__trk) _paintWave(row, row.__trk, _curPct);
});
_syncMotion();

export const Playlist = { toggle, refresh, open, close, showAddDropdown, showAddDropdownForEntries, createPlaylist, showImportDialog };
