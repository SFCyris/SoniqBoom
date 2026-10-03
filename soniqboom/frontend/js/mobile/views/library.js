// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * library.js — Mobile Library view.
 *
 * Group chips: All / Artists / Album Artists / Albums / Genres / Years.
 * - All:                flat track list (paginated lazily on scroll).
 * - Artists/Albums/etc: group list → tap → filtered track list.
 *
 * Track lists, group lists and a playlist's tracks are a pooled virtual list
 * (../../vlist.js): only the rows around the viewport exist, and one delegated
 * set of gesture listeners (tap / long-press / swipe) serves every row.
 */
import { Player } from '../../player.js';
import { attachRowGestures, attachListGestures, attachDragReorder } from '../gestures.js';
import { fmtDur, esc, trackActions, playlistEntry } from './_common.js';
import { artPlaceholderEmoji, probeAdlibDurations } from '../../utils.js';
import { createVirtualList } from '../../vlist.js';

const PAGE_SIZE = 100;

export function mountLibrary(root, ctx) {
  // ── State ────────────────────────────────────────────────────────────
  let group   = 'all';        // 'all' | 'artists' | 'album_artists' | 'albums' | 'genres' | 'years'
  let crumb   = null;         // when set, we're inside a group → showing tracks
  let tracks  = [];
  let groupItems = [];
  let groupField = '';
  let offset  = 0;
  let exhausted = false;
  let loadingGen = -1;        // the view generation whose page is being fetched
  let viewGen = 0;            // bumped by every render(): late fetches of a left view are dropped
  // What the virtual list shows: 'tracks' (All / a group's tracks), 'groups',
  // 'pltracks' (a playlist's tracks), or null (the playlist list / nothing).
  let mode = null;
  let _gestureCleanups = [];
  let _dragCleanup     = null;

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

  // ── Virtual list ─────────────────────────────────────────────────────
  // The list starts below the (sticky) chip bar and the crumb bar; both are
  // read once per list shown, not per frame.
  let listTop = 0, barH = 0;
  function measureChrome() {
    listTop = listEl.offsetTop;
    barH = groupBar.offsetHeight;
  }

  // One row shell for every kind of row; fillRow shows the parts a kind uses.
  function makeRow() {
    const row = document.createElement('div');
    row.className = 'm-row';
    row.innerHTML = `
      <div class="m-row-content">
        <div class="m-row-art"><span></span><img alt="" decoding="async"></div>
        <div class="m-row-meta"><div class="m-row-title"></div><div class="m-row-artist"></div></div>
        <span class="m-row-artist m-row-trail" style="flex-shrink:0;font-size:12px;margin-right:4px"></span>
        <span class="m-row-chev" style="color:var(--text-dim);font-size:18px;flex-shrink:0">›</span>
        <div class="m-row-handle">☰</div>
      </div>`;
    // No cover (404) / not decoded: the img stays transparent over the glyph.
    const img = row.querySelector('img');
    img.onload  = () => img.classList.add('loaded');
    img.onerror = () => img.classList.remove('loaded');
    return row;
  }

  function show(el, on) { el.style.display = on ? '' : 'none'; }

  // A list request failed (and its view is still the one shown): say so.
  function showLoadError(gen) {
    if (gen !== viewGen) return;
    emptyEl.textContent = crumb
      ? 'Couldn’t load this list — go back and open it again.'
      : 'Couldn’t load this list — tap its chip again to retry.';
    emptyEl.classList.remove('hidden');
  }

  function setArt(row, glyph, src) {
    const art = row.querySelector('.m-row-art');
    art.firstElementChild.textContent = glyph;
    const img = art.lastElementChild;
    if (row.__artSrc === src) return;
    row.__artSrc = src;
    img.classList.remove('loaded');
    if (src) img.src = src; else img.removeAttribute('src');
  }

  function setTitle(row, text, defect = '', detail = '') {
    const title = row.querySelector('.m-row-title');
    // Health badge for a known playback defect — only defective rows (rare) take
    // the flex layout so the common path keeps its plain ellipsised text.
    if (defect) {
      title.classList.add('m-row-title--badged');
      const ttlText = document.createElement('span');
      ttlText.className = 'm-row-title-text';
      ttlText.textContent = text;
      const badge = document.createElement('span');
      badge.className = `track-defect-badge track-defect-${defect}`;
      badge.textContent = defect;
      badge.title = detail || '';
      title.replaceChildren(ttlText, badge);
    } else {
      title.classList.remove('m-row-title--badged');
      title.textContent = text;
    }
  }

  // Pool callback: paint row ``idx`` of whatever the list shows.  A forced
  // render (``fresh`` false) leaves a row that already shows the same item.
  function fillRow(row, idx, fresh) {
    const content = row.firstElementChild;
    if (fresh && content.style.transform) { content.style.transform = ''; content.style.transition = ''; }
    row.dataset.idx = idx;
    if (mode === 'groups') {
      const item = groupItems[idx];
      if (!item) return;
      if (!fresh && row.__item === item) return;
      row.__item = item;
      const { display } = groupValue(item);
      delete row.dataset.trackId;
      row.classList.remove('playing');
      setArt(row, emojiFor(group), '');
      setTitle(row, display);
      const count = item.count ? `${item.count}` : '';
      row.querySelector('.m-row-meta .m-row-artist').textContent = count + (count ? ' tracks' : '');
      show(row.querySelector('.m-row-trail'), false);
      show(row.querySelector('.m-row-chev'), true);
      show(row.querySelector('.m-row-handle'), false);
      return;
    }
    const t = tracks[idx];
    if (!t) return;
    row.classList.toggle('playing', !!t.id && t.id === Player.currentTrackId);
    if (!fresh && row.__item === t) return;
    row.__item = t;
    if (t.id) row.dataset.trackId = t.id; else delete row.dataset.trackId;
    // Art: the emoji placeholder always paints; the cover fades in over it.
    setArt(row, artPlaceholderEmoji(t), t.cover_art || (t.id ? `/api/art/${t.id}?size=sm&fallback=404` : ''));
    const defect = (t.defect === 'partial' || t.defect === 'corrupt') ? t.defect : '';
    setTitle(row, t.title || '—', defect, t.defect_detail);
    row.querySelector('.m-row-meta .m-row-artist').textContent = t.artist || t.album_artist || '';
    const trail = row.querySelector('.m-row-trail');
    trail.textContent = fmtDur(t.duration);
    show(trail, true);
    show(row.querySelector('.m-row-chev'), false);
    // Drag handle to reorder — only for regular (hand-editable) playlists.
    show(row.querySelector('.m-row-handle'), mode === 'pltracks' && !!crumb && !crumb.smart);
  }

  const vl = createVirtualList({
    scroller: root, host: listEl, buffer: 8, rowHeight: 61, autoScroll: true,
    make: makeRow,
    fill: fillRow,
    keyOf: (i) => (mode === 'groups' ? groupItems[i] : tracks[i]),
    spacer: () => { const d = document.createElement('div'); d.className = 'm-vl-spacer'; return d; },
    setSpacer: (el, px) => { el.style.height = px + 'px'; },
    offset: () => listTop,
    topInset: () => barH,
    onResize: measureChrome,
  });

  function showList(kind, count) {
    mode = kind;
    measureChrome();
    vl.setCount(count);
    vl.render(true);
  }

  // Gestures for every virtual row, by what the list shows at the time.
  attachListGestures(listEl, (row) => {
    const idx = parseInt(row.dataset.idx, 10);
    if (!Number.isFinite(idx)) return null;
    if (mode === 'groups') {
      const item = groupItems[idx];
      return item ? { onTap: () => openGroup(item) } : null;
    }
    const t = tracks[idx];
    if (!t) return null;
    if (mode === 'tracks') {
      return {
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
      };
    }
    if (mode === 'pltracks') {
      const smart = !!(crumb && crumb.smart);
      const actions = trackActions(t, gctx);
      if (!smart) {
        actions.push({ label: '✕ Remove from playlist', danger: true,
                       onSelect: () => removeFromPlaylist(idx) });
      }
      return {
        onTap: () => Player.setQueue(tracks, idx),
        onLongPress: () => ctx.showSheet({ title: t.title || 'Track', actions }),
        // Smart (query-driven) playlists can't be hand-edited, so no swipe-remove.
        onSwipeAction: smart ? undefined : () => removeFromPlaylist(idx),
        swipeLabel: 'Remove',
        swipeBgClass: 'danger',
      };
    }
    return null;
  });

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
    viewGen++;
    vl.reset();
    mode = null;
    listEl.innerHTML = '';
    root.scrollTop = 0;
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
    const gen = viewGen;
    loadEl.classList.remove('hidden');
    try {
      const res = await fetch('/api/playlists');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const pls = await res.json();
      if (gen !== viewGen) return;
      renderPlaylistList(Array.isArray(pls) ? pls : (pls.playlists || []));
    } catch (err) {
      console.error('Playlist load failed', err);
      showLoadError(gen);
    } finally {
      if (gen === viewGen) loadEl.classList.add('hidden');
    }
  }

  // The (short) list of playlists is plain rows, not the virtual list.
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
    } catch { ctx.toast('Could not create playlist', 'error'); }
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
    } catch { ctx.toast('Could not rename', 'error'); }
  }

  async function deletePlaylist(p) {
    if (!window.confirm(`Delete "${p.name}"? This can’t be undone.`)) return;
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
      if (!r.ok) throw new Error();
      ctx.toast('Deleted');
      crumb = null;
      render();
    } catch { ctx.toast('Could not delete', 'error'); }
  }

  async function loadPlaylistTracks() {
    const gen = viewGen;
    loadEl.classList.remove('hidden');
    try {
      const res = await fetch(`/api/playlists/${encodeURIComponent(crumb.playlistId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const pl = await res.json();
      if (gen !== viewGen) return;
      const plTracks = (pl && (pl.tracks || pl.items)) || [];
      renderPlaylistTracks(plTracks);
    } catch (err) {
      console.error('Playlist tracks load failed', err);
      showLoadError(gen);
    } finally {
      if (gen === viewGen) loadEl.classList.add('hidden');
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
    showList('pltracks', tracks.length);
    // Drag-handle reorder (regular playlists only) — the drag snaps the row back,
    // so onReorder re-renders in the new order and persists it.  The rows are a
    // window of the list, so the indexes come from the rows themselves.
    if (!crumb.smart && !_dragCleanup) {
      _dragCleanup = attachDragReorder(listEl, {
        onReorder: reorderPlaylist,
        getRows: () => vl.rows(),
        indexOf: (row) => parseInt(row.dataset.idx, 10),
      });
    }
  }

  async function reorderPlaylist(from, to) {
    if (from === to) return;
    const moved = tracks.splice(from, 1)[0];
    tracks.splice(to, 0, moved);
    // Repaint locally in the new order (attachDragReorder only reports indices).
    vl.render(true);
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(crumb.playlistId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: tracks.map(playlistEntry) }),
      });
      if (!r.ok) throw new Error();
    } catch { ctx.toast('Could not save order', 'error'); render(); }   // revert from server
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
    } catch { ctx.toast('Could not remove', 'error'); }
  }

  // ── Track list (flat or filtered) ─────────────────────────────────────
  async function loadTrackPage() {
    if (loadingGen === viewGen || exhausted) return;
    const gen = viewGen;
    loadingGen = gen;
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
      const page = res.ok ? await res.json() : null;
      if (gen !== viewGen) return;            // the listener moved to another list meanwhile
      if (!Array.isArray(page)) throw new Error(`HTTP ${res.status}`);
      if (page.length === 0) {
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
      if (offset === 0) showLoadError(gen);
    } finally {
      if (loadingGen === gen) loadingGen = -1;
      if (gen === viewGen) loadEl.classList.add('hidden');
    }
  }

  // "Play from here": build the queue as a bounded forward WINDOW fetched from
  // the track's GLOBAL position, so playback continues past the pages the user
  // happened to scroll in.  Was setQueue(tracks, idx) — only the loaded rows,
  // so "play from here" stopped at the last paged-in page (correctness bug).
  //
  // The window is only the player's STARTING rows: it carries a queue source, so
  // the shared Player extends it on its own — forwards through this list, or,
  // with shuffle on, through a seeded shuffle of EVERY track matching the filter
  // (a fixed 500-row window made shuffle pick only among those 500).
  const QUEUE_WINDOW = 100;
  async function playFrom(startIdx) {
    const filter = {};
    if (crumb) {
      filter[crumb.field] = crumb.value;
      if (crumb.extraField) filter[crumb.extraField] = crumb.extraValue;
    }
    const orderedUrl = crumb ? '/api/search/filter' : '/api/tracks';
    const source = {
      ordered: { url: orderedUrl, params: filter },
      shuffle: { url: '/api/tracks/shuffled', params: filter },
      total: null,
      offset: startIdx,
      label: crumb ? crumb.label : 'All Tracks',
    };
    const params = new URLSearchParams({ ...filter, limit: String(QUEUE_WINDOW), offset: String(startIdx) });
    try {
      const win = await fetch(`${orderedUrl}?${params}`).then(r => (r.ok ? r.json() : null));
      if (Array.isArray(win) && win.length) { Player.setQueue(win, 0, { source }); return; }
    } catch { /* fall through to the loaded slice */ }
    Player.setQueue(tracks, startIdx);   // fallback — never worse than before
  }

  function appendTracks(page) {
    tracks.push(...page);
    if (mode === 'tracks') { vl.setCount(tracks.length); vl.render(true); }
    else showList('tracks', tracks.length);
    // Background-fill real AdLib/IMF lengths for this page's placeholder rows.
    const gen = viewGen;
    probeAdlibDurations(page).then(map => {
      if (gen !== viewGen) return;
      for (const id in map) {
        const sec = map[id];
        if (!(sec > 0)) continue;
        const t = page.find(x => x && x.id === id);
        if (t) t.duration = sec;
        for (const row of vl.rows()) {
          if (row.dataset.trackId === id) row.querySelector('.m-row-trail').textContent = fmtDur(sec);
        }
      }
    });
  }

  // Infinite scroll
  root.addEventListener('scroll', () => {
    if (mode !== 'tracks') return;       // playlists / groups load in full — no pagination
    if (exhausted || loadingGen === viewGen) return;
    if (root.scrollTop + root.clientHeight >= root.scrollHeight - 200) {
      loadTrackPage();
    }
  }, { passive: true });

  // ── Group list (Artists / Albums / Genres / Years) ───────────────────
  const fieldMap = {
    artists:       'artist',
    album_artists: 'album_artist',
    albums:        'album',
    genres:        'genre',
    years:         'year_min',
  };
  async function loadGroupList() {
    const gen = viewGen;
    loadEl.classList.remove('hidden');
    const endpointMap = {
      artists:       '/api/library/artists',
      album_artists: '/api/library/album-artists',
      albums:        '/api/library/albums',
      genres:        '/api/library/genres',
      years:         '/api/library/years',
    };
    try {
      const res = await fetch(endpointMap[group]);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const items = await res.json();
      if (gen !== viewGen) return;
      groupItems = Array.isArray(items) ? items : [];
      groupField = fieldMap[group];
      renderGroupItems();
    } catch (err) {
      console.error('Group load failed', err);
      showLoadError(gen);
    } finally {
      if (gen === viewGen) loadEl.classList.add('hidden');
    }
  }

  // Each aggregation uses a slightly different schema.
  function groupValue(item) {
    const value = item[groupField === 'year_min' ? 'year' : groupField] ?? item.label ?? '';
    return { value, display: item.label || String(value || '[Untagged]') };
  }

  function renderGroupItems() {
    if (!groupItems.length) {
      emptyEl.classList.remove('hidden');
      emptyEl.textContent = 'Nothing here yet.';
      return;
    }
    emptyEl.classList.add('hidden');
    showList('groups', groupItems.length);
  }

  function openGroup(item) {
    const { value, display } = groupValue(item);
    if (groupField === 'year_min') {
      // Exact-year filter via year_min + year_max
      crumb = {
        label: String(value), field: 'year_min', value: String(value),
        extraField: 'year_max', extraValue: String(value),
      };
    } else {
      crumb = { label: display, field: groupField, value: String(value) };
    }
    render();
  }

  function emojiFor(g) {
    return ({
      artists: '🎤', album_artists: '🎤', albums: '💿', genres: '🏷', years: '📅',
    })[g] || '🎵';
  }

  function cleanupGestures() {
    _gestureCleanups.forEach(fn => fn());
    _gestureCleanups = [];
    if (_dragCleanup) { _dragCleanup(); _dragCleanup = null; }
  }

  // Now-playing row highlight.  Mobile library rows subscribed to nothing, so
  // the .m-row.playing style (mobile.css) only ever lit up in the Queue view —
  // you could stare at the playing track and get zero feedback.  One listener;
  // rows filled later take the state in fillRow.
  function markPlaying() {
    if (mode !== 'tracks' && mode !== 'pltracks') return;
    const cur = Player.currentTrackId;
    for (const row of vl.rows()) {
      row.classList.toggle('playing', !!cur && row.dataset.trackId === cur);
    }
  }
  Player.on('trackchange', markPlaying);

  // First render
  render();
}
