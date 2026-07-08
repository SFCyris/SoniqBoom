// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * library.js — Mobile Library view.
 *
 * Group chips: All / Artists / Album Artists / Albums / Genres / Years.
 * - All:                flat track list (paginated lazily on scroll).
 * - Artists/Albums/etc: group list → tap → filtered track list.
 */
import { Player } from '../../player.js';
import { attachRowGestures, attachDragReorder } from '../gestures.js';
import { buildTrackRow, fmtDur, esc, trackActions, playlistEntry } from './_common.js';
import { probeAdlibDurations } from '../../utils.js';

const PAGE_SIZE = 100;

export function mountLibrary(root, ctx) {
  // ── State ────────────────────────────────────────────────────────────
  let group   = 'all';        // 'all' | 'artists' | 'album_artists' | 'albums' | 'genres' | 'years'
  let crumb   = null;         // when set, we're inside a group → showing tracks
  let tracks  = [];
  let groupItems = [];
  let offset  = 0;
  let exhausted = false;
  let loading   = false;
  let _gestureCleanups = [];
  let _dragCleanup     = null;
  let _groupRenderGen  = 0;   // cancels an in-flight chunked group build on view switch

  const gctx = { player: Player, toast: ctx.toast, showSheet: ctx.showSheet };

  // ── DOM scaffold ─────────────────────────────────────────────────────
  root.innerHTML = `
    <div class="m-group-bar" id="lib-groups">
      <button class="m-group-chip active" data-g="all">All</button>
      <button class="m-group-chip"        data-g="artists">Artists</button>
      <button class="m-group-chip"        data-g="album_artists">Album Artists</button>
      <button class="m-group-chip"        data-g="albums">Albums</button>
      <button class="m-group-chip"        data-g="genres">Genres</button>
      <button class="m-group-chip"        data-g="years">Years</button>
      <button class="m-group-chip"        data-g="playlists">Playlists</button>
    </div>
    <div class="m-crumb-bar hidden" id="lib-crumb">
      <button class="m-crumb-back" id="lib-back" aria-label="Back">←</button>
      <span class="m-crumb-text" id="lib-crumb-text"></span>
    </div>
    <ul class="m-list" id="lib-list"></ul>
    <div class="m-empty hidden" id="lib-empty">No tracks yet — add a folder in Settings on desktop.</div>
    <div class="m-loading hidden" id="lib-loading">Loading…</div>
  `;

  const groupBar  = root.querySelector('#lib-groups');
  const crumbBar  = root.querySelector('#lib-crumb');
  const crumbText = root.querySelector('#lib-crumb-text');
  const backBtn   = root.querySelector('#lib-back');
  const listEl    = root.querySelector('#lib-list');
  const emptyEl   = root.querySelector('#lib-empty');
  const loadEl    = root.querySelector('#lib-loading');

  // ── Group chip switching ─────────────────────────────────────────────
  groupBar.addEventListener('click', (e) => {
    const chip = e.target.closest('.m-group-chip');
    if (!chip) return;
    group = chip.dataset.g;
    crumb = null;
    [...groupBar.children].forEach(c => c.classList.toggle('active', c === chip));
    render();
  });

  backBtn.addEventListener('click', () => {
    crumb = null;
    render();
  });

  // ── Render dispatcher ────────────────────────────────────────────────
  function render() {
    cleanupGestures();
    listEl.innerHTML = '';
    tracks = [];
    groupItems = [];
    offset = 0;
    exhausted = false;

    if (group === 'playlists') {
      if (crumb && crumb.playlistId) {
        crumbBar.classList.remove('hidden');
        crumbText.textContent = crumb.label;
        loadPlaylistTracks();
      } else {
        crumbBar.classList.add('hidden');
        loadPlaylistList();
      }
      return;
    }

    if (group === 'all' || crumb) {
      crumbBar.classList.toggle('hidden', !crumb);
      if (crumb) crumbText.textContent = crumb.label;
      loadTrackPage();
    } else {
      crumbBar.classList.add('hidden');
      loadGroupList();
    }
  }

  // ── Playlists (CRUD) ─────────────────────────────────────────────────
  async function loadPlaylistList() {
    loadEl.classList.remove('hidden');
    try {
      const res = await fetch('/api/playlists');
      const pls = res.ok ? await res.json() : [];
      renderPlaylistList(Array.isArray(pls) ? pls : (pls.playlists || []));
    } catch (err) {
      console.error('Playlist load failed', err);
    } finally {
      loadEl.classList.add('hidden');
    }
  }

  function renderPlaylistList(pls) {
    emptyEl.classList.add('hidden');
    // "New playlist" affordance always at the top.
    const newRow = document.createElement('div');
    newRow.className = 'm-row m-pl-new';
    newRow.innerHTML = `<div class="m-row-content">
        <div class="m-row-art"><span>＋</span></div>
        <div class="m-row-meta"><div class="m-row-title">New playlist…</div></div>
      </div>`;
    newRow.addEventListener('click', createPlaylist);
    listEl.appendChild(newRow);

    pls.forEach(p => {
      const row = document.createElement('div');
      row.className = 'm-row';
      row.innerHTML = `
        <div class="m-row-content">
          <div class="m-row-art"><span>${p.smart ? '⚡' : '🎵'}</span></div>
          <div class="m-row-meta">
            <div class="m-row-title">${esc(p.name || 'Playlist')}</div>
            <div class="m-row-artist">${p.track_count || 0} tracks${p.smart ? ' · smart' : ''}</div>
          </div>
          <span style="color:var(--text-dim);font-size:18px;flex-shrink:0">›</span>
        </div>`;
      const cleanup = attachRowGestures(row, {
        onTap: () => { crumb = { label: p.name, playlistId: p.id, smart: !!p.smart }; render(); },
        onLongPress: () => ctx.showSheet({ title: p.name || 'Playlist', actions: [
          { label: '✎ Rename', onSelect: () => renamePlaylist(p) },
          { label: '🗑 Delete', danger: true, onSelect: () => deletePlaylist(p) },
        ]}),
      });
      _gestureCleanups.push(cleanup);
      listEl.appendChild(row);
    });
  }

  async function createPlaylist() {
    const name = (window.prompt('New playlist name') || '').trim();
    if (!name) return;
    try {
      const r = await fetch('/api/playlists', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, track_ids: [] }),
      });
      if (!r.ok) throw new Error();
      ctx.toast(`Created "${name}"`);
      render();
    } catch { ctx.toast('Could not create playlist'); }
  }

  async function renamePlaylist(p) {
    const name = (window.prompt('Rename playlist', p.name || '') || '').trim();
    if (!name || name === p.name) return;
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(p.id)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) throw new Error();
      ctx.toast('Renamed');
      render();
    } catch { ctx.toast('Could not rename'); }
  }

  async function deletePlaylist(p) {
    if (!window.confirm(`Delete "${p.name}"? This can’t be undone.`)) return;
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
      if (!r.ok) throw new Error();
      ctx.toast('Deleted');
      crumb = null;
      render();
    } catch { ctx.toast('Could not delete'); }
  }

  async function loadPlaylistTracks() {
    loadEl.classList.remove('hidden');
    try {
      const res = await fetch(`/api/playlists/${encodeURIComponent(crumb.playlistId)}`);
      const pl = res.ok ? await res.json() : null;
      const plTracks = (pl && (pl.tracks || pl.items)) || [];
      renderPlaylistTracks(plTracks);
    } catch (err) {
      console.error('Playlist tracks load failed', err);
    } finally {
      loadEl.classList.add('hidden');
    }
  }

  function renderPlaylistTracks(plTracks) {
    tracks = plTracks;
    if (!plTracks.length) {
      emptyEl.textContent = 'This playlist is empty — add tracks from the ♫ menu on any song.';
      emptyEl.classList.remove('hidden');
      return;
    }
    emptyEl.classList.add('hidden');
    plTracks.forEach((t, idx) => {
      const dur = document.createElement('span');
      dur.className = 'm-row-artist';
      dur.style.flexShrink = '0'; dur.style.fontSize = '12px'; dur.style.marginRight = '4px';
      dur.textContent = fmtDur(t.duration);
      // Drag handle to reorder — only for regular (hand-editable) playlists.
      const row = buildTrackRow(t, { trailing: dur, showHandle: !crumb.smart });

      const actions = trackActions(t, gctx);
      if (!crumb.smart) {
        actions.push({ label: '✕ Remove from playlist', danger: true,
                       onSelect: () => removeFromPlaylist(idx) });
      }
      const cleanup = attachRowGestures(row, {
        onTap: () => Player.setQueue(tracks, idx),
        onLongPress: () => ctx.showSheet({ title: t.title || 'Track', actions }),
        // Smart (query-driven) playlists can't be hand-edited, so no swipe-remove.
        onSwipeAction: crumb.smart ? undefined : () => removeFromPlaylist(idx),
        swipeLabel: 'Remove',
        swipeBgClass: 'danger',
      });
      _gestureCleanups.push(cleanup);
      listEl.appendChild(row);
    });
    // Drag-handle reorder (regular playlists only) — the drag snaps the row back,
    // so onReorder re-renders in the new order and persists it.
    if (!crumb.smart) {
      _dragCleanup = attachDragReorder(listEl, { onReorder: reorderPlaylist });
    }
  }

  async function reorderPlaylist(from, to) {
    if (from === to) return;
    const moved = tracks.splice(from, 1)[0];
    tracks.splice(to, 0, moved);
    // Repaint locally in the new order (attachDragReorder only reports indices).
    cleanupGestures();
    listEl.innerHTML = '';
    renderPlaylistTracks(tracks);
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(crumb.playlistId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: tracks.map(playlistEntry) }),
      });
      if (!r.ok) throw new Error();
    } catch { ctx.toast('Could not save order'); render(); }   // revert from server
  }

  async function removeFromPlaylist(idx) {
    // Rebuild the entry list minus the removed index and PUT it — precise
    // (order-preserving, handles duplicates) unlike a remove-by-id.
    const entries = tracks.filter((_, i) => i !== idx).map(playlistEntry);
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(crumb.playlistId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: entries }),
      });
      if (!r.ok) throw new Error();
      ctx.toast('Removed');
      render();
    } catch { ctx.toast('Could not remove'); }
  }

  // ── Track list (flat or filtered) ─────────────────────────────────────
  async function loadTrackPage() {
    if (loading || exhausted) return;
    loading = true;
    if (offset === 0) loadEl.classList.remove('hidden');

    let url;
    if (crumb) {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
      params.set(crumb.field, crumb.value);
      if (crumb.extraField) params.set(crumb.extraField, crumb.extraValue);
      url = `/api/search/filter?${params}`;
    } else {
      url = `/api/tracks?limit=${PAGE_SIZE}&offset=${offset}`;
    }

    try {
      const res = await fetch(url);
      const page = await res.json();
      if (!Array.isArray(page) || page.length === 0) {
        exhausted = true;
        if (offset === 0) {
          // Reset the context text — another view (e.g. an empty playlist) may
          // have left its own message in this shared element.
          emptyEl.textContent = crumb ? 'No tracks here.'
            : 'No tracks yet — add music from the desktop app.';
          emptyEl.classList.remove('hidden');
        }
      } else {
        emptyEl.classList.add('hidden');
        appendTracks(page);
        offset += page.length;
        if (page.length < PAGE_SIZE) exhausted = true;
      }
    } catch (err) {
      console.error('Library load failed', err);
    } finally {
      loading = false;
      loadEl.classList.add('hidden');
    }
  }

  // "Play from here": build the queue as a bounded forward WINDOW fetched from
  // the track's GLOBAL position, so playback continues past the pages the user
  // happened to scroll in.  Was setQueue(tracks, idx) — only the loaded rows,
  // so "play from here" stopped at the last paged-in page (correctness bug).
  const QUEUE_WINDOW = 500;
  async function playFrom(startIdx) {
    let url;
    if (crumb) {
      const params = new URLSearchParams({ limit: String(QUEUE_WINDOW), offset: String(startIdx) });
      params.set(crumb.field, crumb.value);
      if (crumb.extraField) params.set(crumb.extraField, crumb.extraValue);
      url = `/api/search/filter?${params}`;
    } else {
      url = `/api/tracks?limit=${QUEUE_WINDOW}&offset=${startIdx}`;
    }
    try {
      const win = await fetch(url).then(r => (r.ok ? r.json() : null));
      if (Array.isArray(win) && win.length) { Player.setQueue(win, 0); return; }
    } catch { /* fall through to the loaded slice */ }
    Player.setQueue(tracks, startIdx);   // fallback — never worse than before
  }

  function appendTracks(page) {
    const durEls = new Map();          // track id -> its duration <span>, for backfill
    page.forEach((t, i) => {
      const idx = tracks.length + i;
      const dur = document.createElement('span');
      dur.className = 'm-row-artist';
      dur.style.flexShrink = '0';
      dur.style.fontSize = '12px';
      dur.style.marginRight = '4px';
      dur.textContent = fmtDur(t.duration);
      if (t && t.id) durEls.set(t.id, dur);

      const row = buildTrackRow(t, { trailing: dur });
      const cleanup = attachRowGestures(row, {
        onTap: () => playFrom(idx),
        onLongPress: () => {
          ctx.showSheet({ title: t.title || 'Track', actions: trackActions(t, gctx) });
        },
        onSwipeAction: () => {
          Player.addToQueue(t);
          ctx.toast('Added to queue');
        },
        swipeLabel: '+ Queue',
        swipeBgClass: 'queue',
      });
      _gestureCleanups.push(cleanup);
      listEl.appendChild(row);
    });
    tracks.push(...page);
    markPlaying();   // a just-loaded page may contain the now-playing track
    // Background-fill real AdLib/IMF lengths for this page's placeholder rows.
    probeAdlibDurations(page).then(map => {
      for (const id in map) {
        const sec = map[id];
        if (!(sec > 0)) continue;
        const el = durEls.get(id);
        if (el) el.textContent = fmtDur(sec);
        const t = page.find(x => x && x.id === id);
        if (t) t.duration = sec;
      }
    });
  }

  // Infinite scroll
  root.addEventListener('scroll', () => {
    if (group === 'playlists') return;   // playlists load in full — no pagination
    if (exhausted || loading) return;
    if (root.scrollTop + root.clientHeight >= root.scrollHeight - 200) {
      loadTrackPage();
    }
  });

  // ── Group list (Artists / Albums / Genres / Years) ───────────────────
  async function loadGroupList() {
    loadEl.classList.remove('hidden');
    const endpointMap = {
      artists:       '/api/library/artists',
      album_artists: '/api/library/album-artists',
      albums:        '/api/library/albums',
      genres:        '/api/library/genres',
      years:         '/api/library/years',
    };
    const fieldMap = {
      artists:       'artist',
      album_artists: 'album_artist',
      albums:        'album',
      genres:        'genre',
      years:         'year_min',
    };
    try {
      const res = await fetch(endpointMap[group]);
      const items = await res.json();
      groupItems = Array.isArray(items) ? items : [];
      renderGroupItems(fieldMap[group]);
    } catch (err) {
      console.error('Group load failed', err);
    } finally {
      loadEl.classList.add('hidden');
    }
  }

  function renderGroupItems(field) {
    if (!groupItems.length) {
      emptyEl.classList.remove('hidden');
      emptyEl.textContent = 'Nothing here yet.';
      return;
    }
    emptyEl.classList.add('hidden');

    // Build in rAF CHUNKS (a 30k-artist aggregation in one synchronous forEach
    // freezes the phone for hundreds of ms).  The generation token — bumped by
    // cleanupGestures() on any view switch — abandons a stale build.
    const items = groupItems;
    const gen = ++_groupRenderGen;

    const buildRow = (item) => {
      // Each aggregation uses a slightly different schema.
      const value = item[field === 'year_min' ? 'year' : field] ?? item.label ?? '';
      const display = item.label || String(value || '[Untagged]');
      const count   = item.count ? `${item.count}` : '';

      const row = document.createElement('div');
      row.className = 'm-row';
      row.innerHTML = `
        <div class="m-row-content">
          <div class="m-row-art"><span>${esc(emojiFor(group))}</span></div>
          <div class="m-row-meta">
            <div class="m-row-title">${esc(display)}</div>
            <div class="m-row-artist">${esc(count + (count ? ' tracks' : ''))}</div>
          </div>
          <span style="color:var(--text-dim);font-size:18px;flex-shrink:0">›</span>
        </div>
      `;
      const cleanup = attachRowGestures(row, {
        onTap: () => {
          if (field === 'year_min') {
            // Exact-year filter via year_min + year_max
            crumb = {
              label: String(value), field: 'year_min', value: String(value),
              extraField: 'year_max', extraValue: String(value),
            };
          } else {
            crumb = { label: display, field, value: String(value) };
          }
          render();
        },
      });
      _gestureCleanups.push(cleanup);
      return row;
    };

    const CHUNK = 60;
    let i = 0;
    const step = () => {
      if (gen !== _groupRenderGen) return;   // superseded by a view switch
      const frag = document.createDocumentFragment();
      const end = Math.min(i + CHUNK, items.length);
      for (; i < end; i++) frag.appendChild(buildRow(items[i]));
      listEl.appendChild(frag);
      if (i < items.length) requestAnimationFrame(step);
    };
    step();
  }

  function emojiFor(g) {
    return ({
      artists: '🎤', album_artists: '🎤', albums: '💿', genres: '🏷', years: '📅',
    })[g] || '🎵';
  }

  function cleanupGestures() {
    _groupRenderGen++;            // stop any in-flight chunked group build
    _gestureCleanups.forEach(fn => fn());
    _gestureCleanups = [];
    if (_dragCleanup) { _dragCleanup(); _dragCleanup = null; }
  }

  // Now-playing row highlight.  Mobile library rows subscribed to nothing, so
  // the .m-row.playing style (mobile.css) only ever lit up in the Queue view —
  // you could stare at the playing track and get zero feedback.  One listener.
  function markPlaying() {
    const cur = Player.currentTrackId;
    listEl.querySelectorAll('.m-row.playing').forEach(r => r.classList.remove('playing'));
    if (!cur) return;
    const sel = (window.CSS && CSS.escape) ? CSS.escape(cur) : String(cur).replace(/["\\]/g, '\\$&');
    const row = listEl.querySelector(`.m-row[data-track-id="${sel}"]`);
    if (row) row.classList.add('playing');
  }
  Player.on('trackchange', markPlaying);

  // First render
  render();
}
