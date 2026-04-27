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
 * Exports: Playlist singleton
 */
import { Player } from './player.js';
import { Library } from './library.js';
import { artPlaceholderEmoji } from './utils.js';

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

// ── State ─────────────────────────────────────────────────────────────────────
let _playlists    = [];
let _activeId     = null;
let _activeTracks = [];
let _addDropdown  = null;

// ── Selection state ───────────────────────────────────────────────────────────
let _selectedIdxs = new Set();   // selected row indices
let _anchorIdx    = null;        // Shift-click anchor

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtDur(sec) {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
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

function _buildDupMaps(tracks) {
  const idCount    = new Map();     // id → times it appears
  const idFirstAt  = new Map();     // id → first index
  const contentMap = new Map();     // contentKey → [{id, path, idx}]

  tracks.forEach((t, i) => {
    idCount.set(t.id, (idCount.get(t.id) || 0) + 1);
    if (!idFirstAt.has(t.id)) idFirstAt.set(t.id, i);
    const k = _contentKey(t);
    if (!contentMap.has(k)) contentMap.set(k, []);
    contentMap.get(k).push({ id: t.id, path: t.path, idx: i });
  });

  return { idCount, idFirstAt, contentMap };
}

// ── Fetch all playlists ───────────────────────────────────────────────────────
async function refresh() {
  try { _playlists = await _api('/playlists'); }
  catch { _playlists = []; }
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
    li.className = 'sidebar-playlist-item' + (pl.id === _activeId ? ' active' : '');
    li.dataset.id = pl.id;
    const cnt = pl.track_count ?? pl.tracks?.length ?? 0;
    li.innerHTML = `<span class="pl-name">${esc(pl.name)}</span><span class="pl-count">${cnt}</span>`;
    li.addEventListener('click', () => { open(); _openPlaylist(pl.id, pl.name); });
    sidebarList.appendChild(li);
  });
}

// ── List view ─────────────────────────────────────────────────────────────────
function _showListView() {
  _activeId     = null;
  _activeTracks = [];
  _clearSelection();
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
        <span class="playlist-row-name">${esc(pl.name)}</span>
        <span class="playlist-row-count">${cnt} track${cnt !== 1 ? 's' : ''}</span>
      </div>
      <button class="pl-open-btn" title="Open playlist">&#9654;</button>
      <button class="playlist-delete-btn" title="Delete" data-id="${pl.id}">&times;</button>
    `;
    row.addEventListener('click', (e) => {
      if (e.target.closest('.playlist-delete-btn')) return;
      _openPlaylist(pl.id, pl.name);
    });
    row.querySelector('.playlist-delete-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      const confirmFn = window.__sbConfirm || confirm;
      const ok = await confirmFn(`Delete "${pl.name}"?`, { title: 'Delete Playlist', okLabel: 'Delete' });
      if (!ok) return;
      await _api(`/playlists/${pl.id}`, { method: 'DELETE' });
      await refresh();
    });
    listEl.appendChild(row);
  });
}

// ── Track view ────────────────────────────────────────────────────────────────
async function _openPlaylist(id, name, _focusPanel = true) {
  _activeId = id;
  _clearSelection();
  try {
    const data    = await _api(`/playlists/${id}`);
    _activeTracks = data.tracks || [];
  } catch { _activeTracks = []; }

  plActiveNm.textContent = name;
  plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;

  hdrList.hidden   = true;
  hdrTracks.hidden = false;
  listEl.hidden    = true;
  tracksEl.hidden  = false;
  dropZone.classList.remove('drop-zone-disabled');
  dropZone.textContent = 'Drop tracks here to add';

  _renderTracks();
  _updateSidebarActive();
}

function _renderTracks() {
  tracksEl.innerHTML = '';
  _updateSelBar();

  if (!_activeTracks.length) {
    tracksEl.innerHTML = '<div class="queue-empty">No tracks yet.<br>Drag from library or use "Add to Playlist".</div>';
    return;
  }

  const { idCount, idFirstAt, contentMap } = _buildDupMaps(_activeTracks);

  _activeTracks.forEach((track, i) => {
    // ── Duplicate classification ────────────────────────────────────────────
    const isIdDup      = idCount.get(track.id) > 1 && idFirstAt.get(track.id) !== i;
    const contentPeers = contentMap.get(_contentKey(track)) || [];
    const contentDups  = contentPeers.filter(p => p.id !== track.id);
    const isContentDup = contentDups.length > 0;

    const isCurrent  = Player.currentTrack?.id === track.id;
    const isSelected = _selectedIdxs.has(i);

    const row = document.createElement('div');
    row.className = 'queue-row'
      + (isCurrent  ? ' playing'  : '')
      + (isSelected ? ' selected' : '')
      + (isIdDup    ? ' dup-id'   : '');
    row.draggable  = true;
    row.dataset.idx = i;

    // ── Duplicate badge ─────────────────────────────────────────────────────
    let dupBadge = '';
    if (isIdDup) {
      dupBadge = `<span class="dup-badge dup-id-badge" title="This track appears more than once in this playlist — likely added by mistake">2×</span>`;
    } else if (isContentDup) {
      const others = contentDups.map(p => p.path || p.id).join('\n');
      dupBadge = `<span class="dup-badge dup-loc-badge" title="Same song also found at:\n${others}">⧉</span>`;
    }

    const artHtml = track.cover_art
      ? `<div class="queue-row-art"><img src="${esc(track.cover_art)}" loading="lazy" alt=""></div>`
      : `<div class="queue-row-art"><span class="qr-art-ph">${artPlaceholderEmoji(track)}</span></div>`;

    row.innerHTML = `
      <span class="queue-drag-handle" title="Drag to reorder">&#10783;</span>
      <span class="queue-playing-icon">${isCurrent ? '&#9654;' : ''}</span>
      ${artHtml}
      <div class="queue-track-info">
        <div class="queue-track-titlerow">
          <span class="queue-track-title" title="${esc(track.title)}">${esc(track.title || '—')}</span>
          ${dupBadge}
        </div>
        <span class="queue-track-artist">${esc(track.artist || track.album_artist || '')}</span>
      </div>
      <span class="queue-track-dur">${fmtDur(track.duration)}</span>
      <button class="queue-remove-btn" title="Remove from playlist">&times;</button>
    `;

    // ── Click: play / select ────────────────────────────────────────────────
    row.addEventListener('click', (e) => {
      if (e.target.closest('.queue-remove-btn')) return;

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
        Player.setQueue(_activeTracks, i);
      }
    });

    // ── Remove button ───────────────────────────────────────────────────────
    row.querySelector('.queue-remove-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      await _removeTrack(_activeId, track.id);
    });

    // ── Drag start ──────────────────────────────────────────────────────────
    row.addEventListener('dragstart', (e) => {
      e.dataTransfer.effectAllowed = 'move';
      // If this row is in the selection drag all selected; otherwise drag just this row
      const dragging = _selectedIdxs.has(i)
        ? [..._selectedIdxs].sort((a, b) => a - b)
        : [i];
      e.dataTransfer.setData('application/x-soniqboom-pl-idx', JSON.stringify(dragging));
      dragging.forEach(idx => {
        tracksEl.querySelector(`[data-idx="${idx}"]`)?.classList.add('dragging');
      });
    });

    // ── Drag end ────────────────────────────────────────────────────────────
    row.addEventListener('dragend', () => {
      tracksEl.querySelectorAll('.queue-row.dragging, .queue-row.dragging-over')
        .forEach(r => r.classList.remove('dragging', 'dragging-over'));
    });

    // ── Drag over ───────────────────────────────────────────────────────────
    row.addEventListener('dragover', (e) => {
      if (e.dataTransfer.types.includes('application/x-soniqboom-pl-idx')) {
        e.preventDefault();
        tracksEl.querySelectorAll('.queue-row.dragging-over')
          .forEach(r => r.classList.remove('dragging-over'));
        row.classList.add('dragging-over');
      }
    });
    row.addEventListener('dragleave', (e) => {
      if (!row.contains(e.relatedTarget)) row.classList.remove('dragging-over');
    });

    // ── Drop: reorder ───────────────────────────────────────────────────────
    row.addEventListener('drop', async (e) => {
      e.preventDefault();
      row.classList.remove('dragging-over');
      const raw = e.dataTransfer.getData('application/x-soniqboom-pl-idx');
      if (!raw) return;

      let fromIndices;
      try { fromIndices = JSON.parse(raw); }
      catch { return; }
      if (!Array.isArray(fromIndices)) fromIndices = [fromIndices];

      const fromSet = new Set(fromIndices);
      if (fromSet.has(i)) return; // dropped onto a dragged row — no-op

      // Extract items being moved and the rest
      const toInsert = fromIndices.map(idx => _activeTracks[idx]);
      const rest     = _activeTracks.filter((_, idx) => !fromSet.has(idx));
      // Adjust insertion point: each dragged item before `i` shifts the target left
      const shift    = fromIndices.filter(idx => idx < i).length;
      const insertAt = Math.max(0, Math.min(rest.length, i - shift));
      rest.splice(insertAt, 0, ...toInsert);
      _activeTracks = rest;

      _clearSelection();
      await _api(`/playlists/${_activeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: _activeTracks.map(t => t.id) }),
      }).catch(() => {});
      _renderTracks();
    });

    tracksEl.appendChild(row);
  });
}

// ── Mutations ─────────────────────────────────────────────────────────────────
async function _addTracks(playlistId, trackIds) {
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
  } catch (err) { console.warn('Failed to add to playlist:', err); }
}

async function _removeTrack(playlistId, trackId) {
  try {
    await _api(`/playlists/${playlistId}/tracks`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_ids: [trackId] }),
    });
    _activeTracks = _activeTracks.filter(t => t.id !== trackId);
    plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
    _clearSelection();
    _renderTracks();
    await refresh();
  } catch (err) { console.warn('Failed to remove track:', err); }
}

async function _removeSelected() {
  if (!_selectedIdxs.size || !_activeId) return;
  const indices    = [..._selectedIdxs].sort((a, b) => a - b);
  const idsToRemove = indices.map(i => _activeTracks[i].id);
  try {
    await _api(`/playlists/${_activeId}/tracks`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_ids: idsToRemove }),
    });
    _activeTracks = _activeTracks.filter((_, i) => !_selectedIdxs.has(i));
    plActiveCt.textContent = `${_activeTracks.length} track${_activeTracks.length !== 1 ? 's' : ''}`;
    _clearSelection();
    _renderTracks();
    await refresh();
  } catch (err) { console.warn('Failed to remove selected:', err); }
}

function _updateSidebarActive() {
  if (!sidebarList) return;
  sidebarList.querySelectorAll('.sidebar-playlist-item')
    .forEach(li => li.classList.toggle('active', li.dataset.id === _activeId));
}

// ── Custom name prompt (no browser dialog) ────────────────────────────────────
function _promptName(title = 'New playlist name') {
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
        <button class="pl-modal-btn pl-modal-ok">Create</button>
      </div>
    `;
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    const input  = dialog.querySelector('.pl-modal-input');
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
  } catch (err) { console.warn('Failed to create playlist:', err); }
}

// ── Drop zone ─────────────────────────────────────────────────────────────────
dropZone.addEventListener('dragover', (e) => {
  if (!_activeId) return;
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
  if (!_activeId) return;
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    const ids = tracks.map(t => t?.id).filter(Boolean);
    if (ids.length) await _addTracks(_activeId, ids);
  } catch (_) {}
});

// Allow drop on the tracks area too (not just the zone banner)
tracksEl.addEventListener('dragover', (e) => {
  if (_activeId
      && e.dataTransfer.types.includes('application/x-soniqboom-track')
      && !e.dataTransfer.types.includes('application/x-soniqboom-pl-idx')) {
    e.preventDefault();
    dropZone.classList.add('drag-active');
  }
});
tracksEl.addEventListener('dragleave', (e) => {
  if (!tracksEl.contains(e.relatedTarget)) dropZone.classList.remove('drag-active');
});
tracksEl.addEventListener('drop', async (e) => {
  if (!e.dataTransfer.types.includes('application/x-soniqboom-track')) return;
  if ( e.dataTransfer.types.includes('application/x-soniqboom-pl-idx')) return;
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    const ids = tracks.map(t => t?.id).filter(Boolean);
    if (ids.length && _activeId) await _addTracks(_activeId, ids);
  } catch (_) {}
});

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
  _closeAddDropdown();
  const selectedTracks = Library.getSelectedTracks();
  if (!selectedTracks.length) return;

  _api('/playlists').then(playlists => {
    _playlists = playlists;
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
          _addTracks(pl.id, selectedTracks.map(t => t.id));
          Library.clearSelection();
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

    const rect = anchorEl.getBoundingClientRect();
    dd.style.left   = rect.left + 'px';
    dd.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
    document.body.appendChild(dd);
    _addDropdown = dd;
    setTimeout(() => document.addEventListener('click', _handleOutsideClick), 0);
  }).catch(() => {});
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

Player.on('trackchange', () => {
  if (_activeId && !tracksEl.hidden) _renderTracks();
});

export const Playlist = { toggle, refresh, open, close, showAddDropdown, createPlaylist };
