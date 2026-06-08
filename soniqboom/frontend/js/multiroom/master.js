// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * master.js — master view controller.
 * Library / Playlists / Albums pickers, transport, queue, listener list.
 * Emits `state_update` on Player events; queue advance goes through the
 * Sync barrier so slaves stay aligned on track transitions.
 */
import { Player } from '../player.js';
import { Sync } from './sync.js';
import { artPlaceholderEmoji } from '../utils.js';

const $ = (id) => document.getElementById(id);

function fmt(sec) {
  if (!isFinite(sec) || sec < 0) return '0:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}

let _stateTimer = null;
let _selectedTrackId = null;
let _searchAbort = null;

// Pickers
let _activeTab = 'tracks';               // 'tracks' | 'playlists' | 'albums'
let _detailOpen = false;
let _cachePlaylists = null;
let _cacheAlbums    = null;

// Detail view state (the list currently shown under the Back button)
let _detailTracks   = [];
let _detailBackTab  = 'tracks';

// Master queue (drives auto-advance through the Sync barrier). Independent of
// Player.queue — we keep Player.queue empty so its built-in auto-advance
// no-ops and never bypasses the barrier.
let _queue    = [];
let _queueIdx = 0;

// Guard against double-advance (one `ended` event + any race with user skip).
let _advancing = false;

// Shuffle + repeat. Persisted across sessions so a returning master resumes
// their preferred playback mode. Repeat cycles none → all → one.
const SHUFFLE_KEY = 'sb_mr_shuffle';
const REPEAT_KEY  = 'sb_mr_repeat';
let _shuffle    = localStorage.getItem(SHUFFLE_KEY) === '1';
let _repeatMode = (() => {
  const v = localStorage.getItem(REPEAT_KEY);
  return (v === 'all' || v === 'one') ? v : 'none';
})();

export function enterMaster() {
  document.body.setAttribute('data-view', 'master');
  $('mr-master-room-name').textContent = Sync.roomName || 'Room';

  _bindControls();
  _renderListeners();
  _renderNow(null);
  _renderQueue();
  _updateShuffleRepeatUI();
  _switchTab('tracks');

  Sync.addEventListener('roster', _renderListeners);

  // Periodically relay our state (drives slave drift correction) — but
  // only while we're actually playing.  An idle/paused master used to
  // broadcast 2Hz to every slave in every room (3 rooms × 9 listeners =
  // sustained WS chatter even with nothing playing).
  if (_stateTimer) clearInterval(_stateTimer);
  _stateTimer = setInterval(() => {
    if (Player.audio && !Player.audio.paused) Sync._emitStateUpdate();
  }, 500);

  Player.on('trackchange', () => Sync._emitStateUpdate());
  Player.on('statechange', () => Sync._emitStateUpdate());
  Player.on('timeupdate',  (p) => _tickProgress(p));
  Player.on('ended',       () => _onTrackEnded());
}

export function leaveMaster() {
  if (_stateTimer) { clearInterval(_stateTimer); _stateTimer = null; }
  Sync.removeEventListener('roster', _renderListeners);
  _queue = [];
  _queueIdx = 0;
  _advancing = false;
  _detailOpen = false;
}

function _bindControls() {
  $('mr-btn-master-leave').onclick = () => {
    leaveMaster();
    Sync.close();
    try { Player.audio.pause(); Player.audio.currentTime = 0; } catch { /* ignore */ }
    document.body.setAttribute('data-view', 'landing');
    import('./app.js').then(m => m.refreshLanding && m.refreshLanding());
  };

  $('mr-ctrl-play').onclick = () => Sync.masterPlayPause();

  $('mr-ctrl-prev').onclick = () => {
    // If more than 3 s in, restart current track. Otherwise step the queue.
    if (Player.audio.currentTime > 3 || _queueIdx <= 0) {
      Sync.masterSeek(0);
      return;
    }
    _playQueueIdx(_queueIdx - 1);
  };
  $('mr-ctrl-next').onclick = () => {
    if (!_queue.length) return;
    const nextIdx = _pickNextIdx();
    if (nextIdx !== null) _playQueueIdx(nextIdx);
  };

  $('mr-ctrl-shuffle').onclick = () => {
    _shuffle = !_shuffle;
    localStorage.setItem(SHUFFLE_KEY, _shuffle ? '1' : '0');
    _updateShuffleRepeatUI();
  };
  $('mr-ctrl-repeat').onclick = () => {
    _repeatMode = _repeatMode === 'none' ? 'all'
                : _repeatMode === 'all'  ? 'one' : 'none';
    localStorage.setItem(REPEAT_KEY, _repeatMode);
    _updateShuffleRepeatUI();
  };

  // Tabs
  $('mr-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.mr-tab');
    if (!btn) return;
    _switchTab(btn.dataset.tab);
  });

  // Detail view back button
  $('mr-detail-back').onclick = () => {
    _detailOpen = false;
    _switchTab(_detailBackTab);
  };

  // Detail view play all
  $('mr-detail-play-all').onclick = () => {
    if (!_detailTracks.length) return;
    _playQueueFrom(_detailTracks, 0);
  };

  // Clear queue
  $('mr-queue-clear').onclick = () => {
    _queue = [];
    _queueIdx = 0;
    _renderQueue();
  };

  // Search (scopes to active tab)
  const search = $('mr-lib-search');
  let t = null;
  search.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => _onSearch(search.value.trim()), 180);
  });
}

// ── Tabs ────────────────────────────────────────────────────────────────────

function _switchTab(tab) {
  _activeTab = tab;
  _detailOpen = false;
  const panes = {
    tracks:    'mr-pane-tracks',
    playlists: 'mr-pane-playlists',
    albums:    'mr-pane-albums',
  };
  for (const paneId of Object.values(panes)) {
    $(paneId).classList.add('hidden');
  }
  $('mr-pane-detail').classList.add('hidden');
  if (panes[tab]) $(panes[tab]).classList.remove('hidden');

  document.querySelectorAll('#mr-tabs .mr-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });

  // Reset search for the tab (different lists search differently)
  const search = $('mr-lib-search');
  search.value = '';
  search.placeholder = tab === 'playlists' ? 'Filter playlists…'
                     : tab === 'albums'    ? 'Filter albums…'
                     : 'Search tracks…';

  if (tab === 'tracks')    _loadInitialLibrary();
  if (tab === 'playlists') _loadPlaylists();
  if (tab === 'albums')    _loadAlbums();
}

function _onSearch(q) {
  if (_detailOpen) return;  // search doesn't apply in detail view
  if (_activeTab === 'tracks')    return _searchTracks(q);
  if (_activeTab === 'playlists') return _filterPlaylists(q);
  if (_activeTab === 'albums')    return _filterAlbums(q);
}

// ── Tracks tab ──────────────────────────────────────────────────────────────

async function _loadInitialLibrary() {
  try {
    const r = await fetch('/api/tracks?limit=100');
    if (!r.ok) throw new Error('library fetch');
    const tracks = await r.json();
    _renderLibrary(tracks);
  } catch (e) {
    $('mr-lib-list').innerHTML = '<li class="mr-empty">Library unavailable</li>';
  }
}

async function _searchTracks(q) {
  if (!q) return _loadInitialLibrary();
  try {
    if (_searchAbort) _searchAbort.abort();
    _searchAbort = new AbortController();
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=80`, { signal: _searchAbort.signal });
    if (!r.ok) return;
    const tracks = await r.json();
    _renderLibrary(tracks);
  } catch (e) {
    if (e.name !== 'AbortError') console.warn(e);
  }
}

function _renderLibrary(tracks) {
  const ul = $('mr-lib-list');
  ul.innerHTML = '';
  if (!tracks.length) {
    ul.innerHTML = '<li class="mr-empty">No tracks</li>';
    return;
  }
  for (const t of tracks) {
    const li = document.createElement('li');
    if (t.id === _selectedTrackId) li.classList.add('active');
    const title = [t.artist || t.album_artist, t.title].filter(Boolean).join(' — ');
    li.innerHTML = `
      <span class="mr-lib-title">${_esc(title || '(Untitled)')}</span>
      <span class="mr-lib-dur">${fmt(t.duration || 0)}</span>`;
    li.onclick = () => _playSingle(t);
    ul.appendChild(li);
  }
}

async function _playSingle(track) {
  _selectedTrackId = track.id;
  // Single-track play clears the queue; no auto-advance expected.
  _queue    = [track];
  _queueIdx = 0;
  await Sync.masterPlayTrack(track);
  _renderNow(track);
  _renderQueue();
}

// ── Playlists tab ───────────────────────────────────────────────────────────

async function _loadPlaylists() {
  const ul = $('mr-pl-list');
  ul.innerHTML = '<li class="mr-empty">Loading…</li>';
  try {
    const r = await fetch('/api/playlists');
    if (!r.ok) throw new Error('playlists fetch');
    _cachePlaylists = await r.json();
    _renderPlaylists(_cachePlaylists);
  } catch (e) {
    ul.innerHTML = '<li class="mr-empty">Playlists unavailable</li>';
  }
}

function _filterPlaylists(q) {
  if (!_cachePlaylists) return;
  const needle = q.toLowerCase();
  const rows = q ? _cachePlaylists.filter(p => (p.name || '').toLowerCase().includes(needle))
                 : _cachePlaylists;
  _renderPlaylists(rows);
}

function _renderPlaylists(rows) {
  const ul = $('mr-pl-list');
  ul.innerHTML = '';
  if (!rows.length) {
    ul.innerHTML = '<li class="mr-empty">No playlists</li>';
    return;
  }
  for (const p of rows) {
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="mr-lib-title-wrap">
        <span class="mr-lib-title">${_esc(p.name || '(Untitled)')}</span>
        <span class="mr-lib-sub">${p.track_count || 0} track(s)</span>
      </div>
      <span class="mr-lib-dur">▶</span>`;
    li.onclick = () => _openPlaylist(p);
    ul.appendChild(li);
  }
}

async function _openPlaylist(summary) {
  try {
    const r = await fetch(`/api/playlists/${encodeURIComponent(summary.id)}`);
    if (!r.ok) throw new Error('playlist fetch');
    const full = await r.json();
    _detailTracks  = Array.isArray(full.tracks) ? full.tracks : [];
    _detailBackTab = 'playlists';
    _showDetail(full.name || summary.name, `${_detailTracks.length} track(s)`);
  } catch (e) {
    _toast('Could not load playlist');
  }
}

// ── Albums tab ──────────────────────────────────────────────────────────────

async function _loadAlbums() {
  const ul = $('mr-album-list');
  ul.innerHTML = '<li class="mr-empty">Loading…</li>';
  try {
    const r = await fetch('/api/library/albums');
    if (!r.ok) throw new Error('albums fetch');
    _cacheAlbums = await r.json();
    _renderAlbums(_cacheAlbums);
  } catch (e) {
    ul.innerHTML = '<li class="mr-empty">Albums unavailable</li>';
  }
}

function _filterAlbums(q) {
  if (!_cacheAlbums) return;
  const needle = q.toLowerCase();
  const rows = q ? _cacheAlbums.filter(a => (a.album || '').toLowerCase().includes(needle))
                 : _cacheAlbums;
  _renderAlbums(rows);
}

function _renderAlbums(rows) {
  const ul = $('mr-album-list');
  ul.innerHTML = '';
  if (!rows.length) {
    ul.innerHTML = '<li class="mr-empty">No albums</li>';
    return;
  }
  for (const a of rows) {
    const li = document.createElement('li');
    const label = a.album || '(No album)';
    li.innerHTML = `
      <div class="mr-lib-title-wrap">
        <span class="mr-lib-title">${_esc(label)}</span>
        <span class="mr-lib-sub">${a.count || 0} track(s)</span>
      </div>
      <span class="mr-lib-dur">▶</span>`;
    li.onclick = () => _openAlbum(a);
    ul.appendChild(li);
  }
}

async function _openAlbum(summary) {
  try {
    const params = new URLSearchParams();
    params.set('limit', '500');
    if (summary.album_artist) params.set('album_artist', summary.album_artist);
    else if (summary.artist)  params.set('artist',       summary.artist);
    if (summary.album)        params.set('album',        summary.album);
    const r = await fetch(`/api/search/filter?${params.toString()}`);
    if (!r.ok) throw new Error('album fetch');
    const tracks = await r.json();
    // Album track order: by disc, then track number.
    tracks.sort((a, b) => {
      const da = a.disc_number ?? 0, db = b.disc_number ?? 0;
      if (da !== db) return da - db;
      return (a.track_number ?? 0) - (b.track_number ?? 0);
    });
    _detailTracks  = tracks;
    _detailBackTab = 'albums';
    const sub = [summary.album_artist || summary.artist, `${tracks.length} track(s)`]
      .filter(Boolean).join(' · ');
    _showDetail(summary.album || '(No album)', sub);
  } catch (e) {
    _toast('Could not load album');
  }
}

// ── Detail view ─────────────────────────────────────────────────────────────

function _showDetail(title, sub) {
  _detailOpen = true;
  $('mr-detail-title').textContent = title;
  $('mr-detail-sub').textContent   = sub || '';
  // Hide tab panes, show detail
  ['mr-pane-tracks', 'mr-pane-playlists', 'mr-pane-albums'].forEach(id => $(id).classList.add('hidden'));
  $('mr-pane-detail').classList.remove('hidden');
  _renderDetailList();
}

function _renderDetailList() {
  const ul = $('mr-detail-list');
  ul.innerHTML = '';
  if (!_detailTracks.length) {
    ul.innerHTML = '<li class="mr-empty">No tracks</li>';
    return;
  }
  const playingId = _queue[_queueIdx]?.id;
  _detailTracks.forEach((t, i) => {
    const li = document.createElement('li');
    if (t.id === playingId) li.classList.add('playing');
    const title = [t.artist || t.album_artist, t.title].filter(Boolean).join(' — ');
    li.innerHTML = `
      <span class="mr-lib-title">${i + 1}. ${_esc(title || '(Untitled)')}</span>
      <span class="mr-lib-dur">${fmt(t.duration || 0)}</span>`;
    li.onclick = () => _playQueueFrom(_detailTracks, i);
    ul.appendChild(li);
  });
}

// ── Queue playback ──────────────────────────────────────────────────────────

async function _playQueueFrom(tracks, startIdx) {
  _queue    = tracks.slice();
  _queueIdx = Math.max(0, Math.min(startIdx, _queue.length - 1));
  const track = _queue[_queueIdx];
  if (!track) return;
  _selectedTrackId = track.id;
  _advancing = true;
  try {
    await Sync.masterPlayTrack(track);
  } finally {
    _advancing = false;
  }
  _renderNow(track);
  _renderQueue();
  if (_detailOpen) _renderDetailList();
}

async function _playQueueIdx(idx) {
  if (idx < 0 || idx >= _queue.length) return;
  _queueIdx = idx;
  const track = _queue[_queueIdx];
  if (!track) return;
  _selectedTrackId = track.id;
  _advancing = true;
  try {
    await Sync.masterPlayTrack(track);
  } finally {
    _advancing = false;
  }
  _renderNow(track);
  _renderQueue();
  if (_detailOpen) _renderDetailList();
}

async function _onTrackEnded() {
  if (_advancing) return;
  if (!_queue.length) return;

  // Repeat-one: replay the same track.
  if (_repeatMode === 'one') {
    await _playQueueIdx(_queueIdx);
    return;
  }

  const nextIdx = _pickNextIdx({ wrapIfRepeatAll: true });
  if (nextIdx !== null) await _playQueueIdx(nextIdx);
}

/** Pick the next queue index based on shuffle + repeat state. */
function _pickNextIdx({ wrapIfRepeatAll = false } = {}) {
  if (!_queue.length) return null;

  if (_shuffle && _queue.length > 1) {
    // Random, but avoid immediately replaying the current track.
    let idx;
    do { idx = Math.floor(Math.random() * _queue.length); }
    while (idx === _queueIdx);
    return idx;
  }

  if (_queueIdx < _queue.length - 1) {
    return _queueIdx + 1;
  }

  // Sequential end-of-queue. Wrap only when asked to (auto-advance) and
  // repeat-all is on. Explicit Next at end-of-queue also wraps under repeat-all
  // so the user can keep clicking forward indefinitely.
  if (_repeatMode === 'all') return 0;

  return wrapIfRepeatAll ? null : null;
}

function _updateShuffleRepeatUI() {
  const sh = $('mr-ctrl-shuffle');
  if (sh) sh.classList.toggle('active', _shuffle);

  const rp = $('mr-ctrl-repeat');
  if (rp) {
    rp.classList.toggle('active', _repeatMode !== 'none');
    const glyph = _repeatMode === 'one' ? '🔂' : '🔁';
    rp.textContent = glyph;
    rp.dataset.emoji = glyph;
    rp.title = _repeatMode === 'one' ? 'Repeat: one'
             : _repeatMode === 'all' ? 'Repeat: all' : 'Repeat: off';
  }
}

function _renderQueue() {
  const ul = $('mr-queue-list');
  ul.innerHTML = '';
  const clearBtn = $('mr-queue-clear');
  if (!_queue.length) {
    ul.innerHTML = '<li class="mr-empty">Pick a playlist or album to queue tracks.</li>';
    clearBtn.classList.add('hidden');
    return;
  }
  clearBtn.classList.remove('hidden');
  _queue.forEach((t, i) => {
    const li = document.createElement('li');
    if (i === _queueIdx) li.classList.add('current');
    const title = [t.artist || t.album_artist, t.title].filter(Boolean).join(' — ');
    li.innerHTML = `
      <span class="mr-queue-idx">${i + 1}.</span>
      <span class="mr-queue-title">${_esc(title || '(Untitled)')}</span>
      <span class="mr-queue-dur">${fmt(t.duration || 0)}</span>`;
    li.onclick = () => _playQueueIdx(i);
    ul.appendChild(li);
  });
}

// ── Now-playing card ────────────────────────────────────────────────────────

function _renderNow(track) {
  const artEl = $('mr-now-art');
  if (!track) {
    $('mr-now-title').textContent = 'No track';
    $('mr-now-artist').textContent = '';
    // Default state: no track means no specific format → use the generic
    // 🔊 glyph the rest of the app uses for "unspecified".
    artEl.innerHTML = '<span class="mr-art-ph">\u{1F50A}</span>';
    $('mr-now-cur').textContent = '0:00';
    $('mr-now-dur').textContent = '0:00';
    $('mr-now-progress-fill').style.width = '0%';
    return;
  }
  $('mr-now-title').textContent = track.title || '(Untitled)';
  $('mr-now-artist').textContent = track.artist || track.album_artist || '';
  // Format-aware placeholder + on-demand cover fetch.  Previously this
  // only rendered ``track.cover_art`` (null on FTP/SMB libraries) so
  // every track on a remote share painted the bare 🎵 emoji.  Now we
  // layer the same format-emoji + ``/api/art/{id}`` fallback the
  // desktop player uses, so the multiroom controller shows real album
  // art consistently with the bottom-left bar.
  const src = track.cover_art || (track.id ? `/api/art/${track.id}?size=lg&fallback=404` : '');
  artEl.innerHTML = `<span class="mr-art-ph">${artPlaceholderEmoji(track)}</span>` +
    (src ? `<img class="mr-art-img" src="${_esc(src)}" alt="">` : '');
  const _mrImg = artEl.querySelector('.mr-art-img');
  if (_mrImg) {
    _mrImg.onload  = () => _mrImg.classList.add('loaded');
    _mrImg.onerror = () => _mrImg.remove();
  }
  $('mr-now-dur').textContent = fmt(track.duration || 0);
}

function _tickProgress(p) {
  $('mr-now-cur').textContent = fmt(p.current);
  $('mr-now-dur').textContent = fmt(p.duration);
  $('mr-now-progress-fill').style.width = `${p.pct}%`;
  $('mr-ctrl-play').textContent = Player.audio.paused ? '▶' : '⏸';
}

function _renderListeners() {
  const ul = $('mr-listener-list');
  ul.innerHTML = '';
  const clients = Sync.clients || [];
  $('mr-master-client-count').textContent = clients.length;
  for (const c of clients) {
    const li = document.createElement('li');
    li.innerHTML = `
      <span>${_esc(c.label)}${c.client_id === Sync.clientId ? ' (you)' : ''}</span>
      <span class="mr-role-badge ${c.role}">${c.role}</span>`;
    ul.appendChild(li);
  }
}

function _toast(msg) {
  const el = $('mr-toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(_toast._t);
  _toast._t = setTimeout(() => el.classList.add('hidden'), 2400);
}

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
