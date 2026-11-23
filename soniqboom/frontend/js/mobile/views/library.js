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
import { fmtDur, esc, trackActions, playlistEntry, countNoun, freshArt } from './_common.js';
import { askCoverWhenUseful, noteCoverShown, noteCoverGone } from '../../cover_status.js';
import { artPlaceholderEmoji, probeAdlibDurations, staticArtUrl, compoLabel } from '../../utils.js';
import { createVirtualList } from '../../vlist.js';
import { offlineKind, offlineBadge, Offline } from '../../offline.js';
import { healthBadge } from '../../health.js';

const countTracks = (n) => countNoun(n, 'track');   // "1 track", "2 tracks"

const PAGE_SIZE = 100;

// The track the now-playing highlight shows: the current one, unless its start
// failed (nothing plays) — ``Player.nowPlayingId``; an older cached player.js:
// the current one.
function _nowPlayingId() {
  return ('nowPlayingId' in Player) ? Player.nowPlayingId : Player.currentTrackId;
}

export function mountLibrary(root, ctx) {
  // ── State ────────────────────────────────────────────────────────────
  let group   = 'all';        // 'all' | 'artists' | 'album_artists' | 'albums' | 'genres' | 'years'
                              // | 'composers' | 'games' | 'platforms' | 'parties' | 'playlists'
  let crumb   = null;         // when set, we're inside a group → showing tracks
  let tracks  = [];
  // A loaded row → its position in the server's order when its page came.
  const _posOf = new WeakMap();
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
      <button class="m-group-chip hidden" data-g="composers">Composers</button>
      <button class="m-group-chip hidden" data-g="games">Games</button>
      <button class="m-group-chip hidden" data-g="platforms">Platforms</button>
      <button class="m-group-chip hidden" data-g="parties">Parties</button>
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
        <span class="m-row-album"></span>
        <span class="m-row-artist m-row-trail"></span>
        <span class="m-row-chev" aria-hidden="true">›</span>
        <div class="m-row-handle">☰</div>
      </div>`;
    // No cover (204) / not decoded: the img stays transparent over the glyph.
    const img = row.querySelector('img');
    // (``img.__seenId``: the track of a row without an embedded cover —
    // remembered once it shows one, cover_status.js)
    img.onload  = () => { img.classList.add('loaded'); if (img.__seenId) noteCoverShown(img.__seenId); };
    img.onerror = () => { img.classList.remove('loaded'); if (img.__seenId) noteCoverGone(img.__seenId); };
    return row;
  }

  function show(el, on) { el.style.display = on ? '' : 'none'; }

  // A list request failed (and its view is still the one shown): say so.
  function showLoadError(gen) {
    if (gen !== viewGen) return;
    emptyEl.textContent = crumb
      ? 'Couldn’t load this list — go back and open it again.'
      : 'Couldn’t load this list — tap its chip again to retry.';
    emptyEl.dataset.kind = 'error';          // (shown red on the display, mobile.css)
    emptyEl.classList.remove('hidden');
  }

  // ``id``: the row's track carries no embedded cover — its image is asked
  // only when the batched status request (cover_status.js) does not know it
  // to have none.  ``row.__artSrc`` is set either way, so an ``art_ready``
  // for it asks the image (``reaskArt``, _common.js).
  function setArt(row, glyph, src, id = null) {
    const art = row.querySelector('.m-row-art');
    art.firstElementChild.textContent = glyph;
    const img = art.lastElementChild;
    if (row.__artSrc === src) return;
    row.__artSrc = src;
    img.__seenId = id || null;
    img.classList.remove('loaded');
    if (!src) { img.removeAttribute('src'); return; }
    if (!id) { img.src = src; return; }
    img.removeAttribute('src');
    askCoverWhenUseful(id, () => {
      if (row.__artSrc === src && !img.getAttribute('src')) img.src = src;
    });
  }

  function setTitle(row, text, health = null, offline = '') {
    const title = row.querySelector('.m-row-title');
    row.classList.toggle('track-offline', !!offline);
    // Health badge for a known playback defect, "offline" for a track that
    // can't play right now — only badged rows (rare) take the flex layout so the
    // common path keeps its plain ellipsised text.
    if (health || offline) {
      title.classList.add('m-row-title--badged');
      const ttlText = document.createElement('span');
      ttlText.className = 'm-row-title-text';
      ttlText.textContent = text;
      const parts = [ttlText];
      if (health) parts.push(health);
      if (offline) parts.push(offlineBadge(offline));
      title.replaceChildren(...parts);
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
      row.classList.remove('playing', 'track-offline');
      setArt(row, emojiFor(group), '');
      setTitle(row, display);
      row.querySelector('.m-row-meta .m-row-artist').textContent = item.count ? countTracks(item.count) : '';
      row.querySelector('.m-row-album').textContent = '';
      show(row.querySelector('.m-row-trail'), false);
      show(row.querySelector('.m-row-chev'), true);
      show(row.querySelector('.m-row-handle'), false);
      return;
    }
    const t = tracks[idx];
    if (!t) return;
    row.classList.toggle('playing', !!t.id && t.id === _nowPlayingId());
    if (!fresh && row.__item === t) return;
    row.__item = t;
    if (t.id) row.dataset.trackId = t.id; else delete row.dataset.trackId;
    // Art: the emoji placeholder always paints; the cover fades in over it.
    setArt(row, artPlaceholderEmoji(t), freshArt(staticArtUrl(t.cover_art) || (t.id ? `/api/art/${t.id}/sm.jpg?fallback=none` : '')),
           t.cover_art ? null : t.id);
    setTitle(row, t.title || '—', healthBadge(t), offlineKind(t));
    // (a party's list: the compo and place beside the artist)
    const compo = crumb && crumb.field === 'party' ? compoLabel(t, { party: false }) : '';
    const who = t.artist || t.album_artist || '';
    row.querySelector('.m-row-meta .m-row-artist').textContent = compo ? (who ? `${compo} · ${who}` : compo) : who;
    row.querySelector('.m-row-album').textContent = t.album || '';
    const trail = row.querySelector('.m-row-trail');
    trail.textContent = fmtDur(t.duration);
    show(trail, true);
    show(row.querySelector('.m-row-chev'), false);
    // Drag handle to reorder — only for regular (hand-editable) playlists.
    show(row.querySelector('.m-row-handle'),
         mode === 'pltracks' && !!crumb && !crumb.smart && !crumb.readOnly);
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
    // The album column (a wide list): not for groups, nor inside one album.
    listEl.dataset.album = (kind === 'groups' || (crumb && crumb.field === 'album')) ? 'off' : 'on';
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
      // Smart (query-driven) and imported (someone else's, read-only)
      // playlists can't be hand-edited.
      const smart = !!(crumb && (crumb.smart || crumb.readOnly));
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

  // The scene browse chips (Composers / Games / Platforms / Parties) show once
  // the library has an entry for them (GET /api/library/counts; a null count:
  // the server is still building what it reads after a start — asked again).
  const SCENE_CHIPS = ['composers', 'games', 'platforms', 'parties'];
  async function revealSceneChips(tries = 0) {
    let c = null;
    try {
      const res = await fetch('/api/library/counts');
      c = res.ok ? await res.json() : null;
    } catch (_) { /* offline: the chips stay as they are */ }
    if (!c) return;
    let unknown = false;
    for (const g of SCENE_CHIPS) {
      const n = c[g];
      if (n === null) { unknown = true; continue; }
      const chip = groupBar.querySelector(`.m-group-chip[data-g="${g}"]`);
      if (chip && Number.isFinite(n)) chip.classList.toggle('hidden', n <= 0 && group !== g);
    }
    clearTimeout(chipRetry);                 // (one retry chain at a time)
    if (unknown && tries < 20) chipRetry = setTimeout(() => revealSceneChips(tries + 1), 3000);
  }
  let chipRetry = null;

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

  // A playback problem reported / cleared here (health.js): the list's copies
  // of those tracks take the new fields and their rows repaint in place.
  document.addEventListener('soniqboom:health-changed', (e) => {
    const d = (e && e.detail) || {};
    const want = new Set(Array.isArray(d.ids) ? d.ids : []);
    if (!want.size || !Array.isArray(tracks)) return;
    let hit = false;
    for (const t of tracks) if (t && want.has(t.id)) { Object.assign(t, d.fields || {}); hit = true; }
    if (!hit) return;
    for (const row of vl.rows()) row.__item = null;
    vl.render(true);
  });

  // Rows of an online collection that left the library (a package with no
  // tune in it — app.js _tracksLeft): the open list drops them in place.  The
  // server's list shrank with it: the next page starts that many rows earlier.
  document.addEventListener('soniqboom:tracks-left', (e) => {
    const ids = new Set(((e && e.detail) || {}).ids || []);
    if (!ids.size || (mode !== 'tracks' && mode !== 'pltracks')) return;
    const before = tracks.length;
    tracks = tracks.filter((t) => !(t && ids.has(t.id)));
    const gone = before - tracks.length;
    if (!gone) return;
    if (mode === 'tracks') offset = Math.max(0, offset - gone);
    for (const row of vl.rows()) row.__item = null;
    vl.setCount(tracks.length);
    vl.render(true);
  });

  // Which tracks can play changed (a drive came back / went away, a track
  // became one that can't be played or stopped being one, or the "Tracks on
  // unavailable drives" / "Tracks that can't be played" setting).  Either Hide
  // (now or before): the lists change length — re-read the open one.  Mark /
  // Show: the same rows — their flags are re-read in place, the scroll kept.
  document.addEventListener('soniqboom:availability-changed', (e) => {
    const d = (e && e.detail) || {};
    if (d.mode === 'hide' || d.prevMode === 'hide' || d.removed
        || d.unplayable === 'hide' || d.prevUnplayable === 'hide') { render(); return; }
    if (mode !== 'tracks' && mode !== 'pltracks') return;
    const list = tracks;
    Promise.resolve(Offline.recheckRows?.(list)).then(() => {
      if (tracks !== list) return;
      for (const row of vl.rows()) row.__item = null;      // (repaint rows showing the same track)
      vl.render(true);
    }).catch(() => {});
  });

  // Tracks took fields without a scan (app.js _catalogueChanged — an online
  // collection's unplayed tunes filled from the song database, a catalogue
  // built, refreshed or removed, a library restored): the open list reads
  // what it shows again IN PLACE — the scroll position stays, and only the
  // rows whose fields changed repaint.  A track list asks for its loaded rows
  // by id (rows no longer in the library leave it; rows the library gained
  // show once the list is opened again); a group list is read again whole.
  // Events in a burst are answered once.
  let _rereadTimer = null, _rereading = false, _rereadAgain = false;
  document.addEventListener('soniqboom:catalogue-changed', () => {
    clearTimeout(_rereadTimer);
    _rereadTimer = setTimeout(rereadInPlace, 600);
  });

  // [m-reread:begin] — tests/js/mobile_reread.test.mjs runs this region as is
  const _same = (a, b) => a === b
    || (a && b && typeof a === 'object' && typeof b === 'object'
        && JSON.stringify(a) === JSON.stringify(b));
  // A list row leaves out the fields a track has no value for; the row read
  // back by id spells each of them out with its empty default.  A field the
  // list row lacks and the fresh row holds empty is the same field: no change.
  const _empty = (v) => v == null || v === '' || v === 0 || v === false
    || (typeof v === 'object' && !Object.keys(v).length);
  // The fresh row's fields go into the list row ``t``; true when one differed.
  function _takeFresh(t, row) {
    let changed = false;
    for (const k in row) {
      // (a length found here for a row the server has none for stays)
      if (k === 'duration' && !(row.duration > 0) && t.duration > 0) continue;
      if (!(k in t) && _empty(row[k])) continue;
      if (!_same(t[k], row[k])) { t[k] = row[k]; changed = true; }
    }
    return changed;
  }
  // The rows read back by id (``fresh``: id → row) go into the rows of
  // ``list`` that were asked for: {changed: the rows that took a field,
  // gone: how many of them the library no longer holds}.
  function _applyFresh(list, asked, fresh, playing) {
    const changed = new Set();
    let gone = 0;
    for (const t of list) {
      if (!t || !asked.has(t.id)) continue;               // (a page that arrived meanwhile)
      const row = fresh.get(t.id);
      if (!row) { gone++; continue; }
      // The playing tune of an online collection not read yet is the player's
      // to read again (app.js: what changed there brings its package's other
      // tunes, and a row may be the queue's own object) — its row waits.
      if (playing && t.id === playing.id && !((playing.mtime || 0) > 0)) continue;
      if (_takeFresh(t, row)) changed.add(t);
    }
    return { changed, gone };
  }
  /** The rows of a further page the list does not hold yet.  Pages are cut by
   *  position, so a list whose order moved between two requests (a track
   *  gained its artist) can hand a row out a second time. */
  function _unseen(have, page) {
    const ids = new Set();
    for (const t of have) if (t && t.id) ids.add(t.id);
    return page.filter(t => !t || !t.id || (!ids.has(t.id) && !!ids.add(t.id)));
  }
  // [m-reread:end]

  async function rereadInPlace() {
    if (_rereading) { _rereadAgain = true; return; }
    _rereading = true;
    try {
      if (!crumb) revealSceneChips();
      if (mode === 'tracks' || mode === 'pltracks') await rereadTracks();
      else if (mode === 'groups') await rereadGroups();
    } catch (err) {
      console.error('Library re-read failed', err);     // (the list stays as it is)
    } finally {
      _rereading = false;
      if (_rereadAgain) { _rereadAgain = false; rereadInPlace(); }
    }
  }

  async function rereadTracks() {
    const gen = viewGen, list = tracks, kind = mode;
    const ids = [...new Set(list.filter((t) => t && t.id).map((t) => t.id))];
    if (!ids.length) return;
    const fresh = new Map();
    for (let i = 0; i < ids.length; i += 5000) {          // (the server's cap per request)
      const r = await fetch('/api/tracks/meta/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids.slice(i, i + 5000) }), credentials: 'same-origin',
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      for (const row of (await r.json()) || []) if (row && row.id) fresh.set(row.id, row);
      if (gen !== viewGen || tracks !== list) return;     // the listener moved on meanwhile
    }
    const asked = new Set(ids);
    const { changed, gone } = _applyFresh(list, asked, fresh, Player.currentTrack);
    // A track list drops the rows that left the library (the server's list
    // shrank with them: the next page starts that many rows earlier); a
    // playlist keeps its entries — it is the server's to say what it holds.
    if (gone && kind === 'tracks') {
      if (gone === list.filter((t) => t && asked.has(t.id)).length) { render(); return; }
      tracks = list.filter((t) => !(t && asked.has(t.id) && !fresh.has(t.id)));
      offset = Math.max(0, offset - gone);
      for (const row of vl.rows()) row.__item = null;
      vl.setCount(tracks.length);
      vl.render(true);
      return;
    }
    if (!changed.size) return;
    for (const row of vl.rows()) if (changed.has(row.__item)) row.__item = null;
    vl.render(true);
  }

  async function rereadGroups() {
    const gen = viewGen, shown = groupItems;
    const res = await fetch(GROUP_ENDPOINTS[group]);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const items = await res.json();
    if (gen !== viewGen || groupItems !== shown || mode !== 'groups') return;
    if (!Array.isArray(items) || _same(items, shown)) return;
    if (!items.length) { render(); return; }              // (nothing left: the empty note)
    // The rows already showing an entry that did not change keep their paint.
    const before = new Map(shown.map((it) => [JSON.stringify(it), it]));
    groupItems = items.map((it) => before.get(JSON.stringify(it)) || it);
    vl.setCount(groupItems.length);
    vl.render(true);
  }

  // ── Render dispatcher ────────────────────────────────────────────────
  function render() {
    cleanupGestures();
    viewGen++;
    // (the top of a list — a scan or an enrichment may have brought the
    // first Scene entries since: the chips are asked again)
    if (!crumb) revealSceneChips();
    delete emptyEl.dataset.kind;
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
    // The playlists other users share: tick to import (read-only).
    const impRow = document.createElement('div');
    impRow.className = 'm-row m-pl-import';
    impRow.innerHTML = `<div class="m-row-content">
        <div class="m-row-art"><span>⇩</span></div>
        <div class="m-row-meta"><div class="m-row-title">Import shared playlists…</div></div>
      </div>`;
    impRow.addEventListener('click', importSheet);
    listEl.appendChild(impRow);
    // A playlist file (M3U, M3U8, XSPF): a new playlist of the tracks found.
    const fileRow = document.createElement('div');
    fileRow.className = 'm-row m-pl-import';
    fileRow.innerHTML = `<div class="m-row-content">
        <div class="m-row-art"><span>⇩</span></div>
        <div class="m-row-meta"><div class="m-row-title">Import a playlist file…</div>
          <div class="m-row-artist">M3U, M3U8 or XSPF</div></div>
      </div>
      <input type="file" accept=".m3u,.m3u8,.xspf,audio/x-mpegurl,audio/mpegurl,application/xspf+xml" hidden>`;
    const fileInput = fileRow.querySelector('input');
    fileRow.addEventListener('click', (e) => { if (e.target !== fileInput) fileInput.click(); });
    fileInput.addEventListener('change', () => {
      const f = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (f) importFile(f);
    });
    listEl.appendChild(fileRow);

    pls.forEach(p => {
      const row = document.createElement('div');
      row.className = 'm-row';
      const sub = p.subscribed
        ? ` · by ${esc(p.owner_name || '?')} · read-only`
        : (p.shared ? ' · shared' : '');
      row.innerHTML = `
        <div class="m-row-content">
          <div class="m-row-art"><span>${p.smart ? '⚡' : '🎵'}</span></div>
          <div class="m-row-meta">
            <div class="m-row-title">${esc(p.name || 'Playlist')}</div>
            <div class="m-row-artist">${countTracks(p.track_count || 0)}${p.smart ? ' · smart' : ''}${sub}</div>
          </div>
          <span class="m-row-chev" aria-hidden="true">›</span>
        </div>`;
      const cleanup = attachRowGestures(row, {
        onTap: () => {
          crumb = { label: p.name, playlistId: p.id, smart: !!p.smart, readOnly: !!p.read_only };
          render();
        },
        onLongPress: () => ctx.showSheet({ title: p.name || 'Playlist', actions: [
          { label: '⇪ Export as M3U', onSelect: () => exportPlaylist(p, 'm3u8') },
          { label: '⇪ Export as XSPF', onSelect: () => exportPlaylist(p, 'xspf') },
          ...(p.subscribed ? [
            { label: '✕ Remove from my list', danger: true, onSelect: () => setSubscribed(p, false) },
          ] : [
            { label: '✎ Rename', onSelect: () => renamePlaylist(p) },
            { label: '🗑 Delete', danger: true, onSelect: () => deletePlaylist(p) },
          ]),
        ]}),
      });
      _gestureCleanups.push(cleanup);
      listEl.appendChild(row);
    });
  }

  async function setSubscribed(p, subscribed) {
    try {
      const r = await fetch(`/api/playlists/${encodeURIComponent(p.id)}/subscription`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscribed }),
      });
      if (!r.ok) throw new Error();
      ctx.toast(subscribed ? `Imported "${p.name}"` : `Removed "${p.name}"`);
      if (!subscribed && crumb && crumb.playlistId === p.id) crumb = null;
      render();
    } catch { ctx.toast('Could not change the import', 'error'); }
  }

  // A checkbox list (☑ imported / ☐ not) of the playlists other users share;
  // a tap toggles one and opens the list again.
  async function importSheet() {
    let rows = [];
    try {
      const r = await fetch('/api/playlists/shared');
      if (!r.ok) throw new Error();
      rows = await r.json();
    } catch { ctx.toast("Couldn't load the shared playlists", 'error'); return; }
    if (!rows.length) { ctx.toast('No other user shares a playlist yet'); return; }
    ctx.showSheet({ title: 'Import shared playlists', actions: rows.map(p => ({
      label: `${p.subscribed ? '☑' : '☐'}  ${p.name}  ·  by ${p.owner_name || '?'}`,
      onSelect: async () => { await setSubscribed(p, !p.subscribed); importSheet(); },
    }))});
  }

  // The playlist as a file to save (GET /api/playlists/{id}/export).
  async function exportPlaylist(p, format) {
    let blob, name = `playlist.${format}`;
    try {
      const res = await fetch(`/api/playlists/${encodeURIComponent(p.id)}/export?format=${format}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const cd = res.headers.get('content-disposition') || '';
      const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="([^"]+)"/i);
      if (m) { try { name = decodeURIComponent(m[1]); } catch (_) { name = m[1]; } }
      blob = await res.blob();
    } catch { ctx.toast("Couldn't export the playlist", 'error'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  // A new playlist from a playlist file (POST /api/playlists/import): its
  // text as UTF-8, else Windows-1252 (an older M3U).
  async function importFile(file) {
    if (file.size > 5 * 1024 * 1024) { ctx.toast('This file is too large to be a playlist', 'error'); return; }
    let res, body;
    try {
      const buf = await file.arrayBuffer();
      const b = new Uint8Array(buf, 0, Math.min(2, buf.byteLength));
      let content;
      if (b.length === 2 && ((b[0] === 0xff && b[1] === 0xfe) || (b[0] === 0xfe && b[1] === 0xff))) {
        content = new TextDecoder(b[0] === 0xff ? 'utf-16le' : 'utf-16be').decode(buf);
      } else {
        try { content = new TextDecoder('utf-8', { fatal: true }).decode(buf); }
        catch (_) { content = new TextDecoder('windows-1252').decode(buf); }
      }
      res = await fetch('/api/playlists/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, content }),
      });
      body = await res.json().catch(() => null);
    } catch { ctx.toast("Couldn't import the file", 'error'); return; }
    if (!res.ok) {
      const d = body && body.detail;
      ctx.toast(typeof d === 'string' ? d : (d && d.message) || "Couldn't import the file", 'error');
      return;
    }
    ctx.toast(`Made "${body.playlist.name}": ${body.matched} of ${body.total} found`);
    render();
    // The entries not found, in a sheet (reason beside each).
    if (body.unmatched_count) {
      const more = body.unmatched_count - body.unmatched.length;
      ctx.showSheet({ title: `Not found (${body.unmatched_count})`, actions: [
        ...body.unmatched.map(u => ({ label: `${u.entry} — ${u.reason}`, onSelect: () => {} })),
        ...(more > 0 ? [{ label: `…and ${more} more`, onSelect: () => {} }] : []),
      ]});
    }
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
    if (!crumb.smart && !crumb.readOnly && !_dragCleanup) {
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
      url = `${crumb.url || '/api/search/filter'}?${params}`;
    } else {
      url = `/api/tracks?limit=${PAGE_SIZE}&offset=${offset}`;
    }

    let again = false;
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
        // (where each row stands in the server's order, for "play from here")
        page.forEach((t, i) => { if (t && typeof t === 'object') _posOf.set(t, offset + i); });
        const rows = _unseen(tracks, page);   // (only the rows the list does not hold)
        if (rows.length) appendTracks(rows);
        offset += page.length;
        if (page.length < PAGE_SIZE) exhausted = true;
        // A page that added nothing moved no row into view: no scroll will
        // ask for the next one.
        again = !rows.length && !exhausted;
      }
    } catch (err) {
      console.error('Library load failed', err);
      if (offset === 0) showLoadError(gen);
    } finally {
      if (loadingGen === gen) loadingGen = -1;
      if (gen === viewGen) loadEl.classList.add('hidden');
    }
    if (again && gen === viewGen) loadTrackPage();
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
    const orderedUrl = crumb ? (crumb.url || '/api/search/filter') : '/api/tracks';
    const source = {
      ordered: { url: orderedUrl, params: filter },
      shuffle: { url: '/api/tracks/shuffled', params: filter },
      total: null,
      offset: startIdx,
      label: crumb ? crumb.label : 'All Tracks',
    };
    const pick = tracks[startIdx];
    // The window starts where the row stood in the server's order when its
    // page came — further on than its place in this list once repeated rows
    // of a page were left out (_unseen) — and, when the track is not there,
    // at its place in this list (rows that left the list moved it back).  The
    // picked track is looked for in the window by id: the order may have
    // moved since.  In neither window: the loaded rows play, from the picked one.
    const starts = [pick ? _posOf.get(pick) : undefined, startIdx]
      .filter((n, i, all) => Number.isInteger(n) && n >= 0 && all.indexOf(n) === i);
    try {
      for (const from of starts) {
        const params = new URLSearchParams({ ...filter, limit: String(QUEUE_WINDOW), offset: String(from) });
        const win = await fetch(`${orderedUrl}?${params}`).then(r => (r.ok ? r.json() : null));
        if (!Array.isArray(win)) break;
        const at = pick ? win.findIndex(t => t && t.id === pick.id) : -1;
        if (at >= 0) { Player.setQueue(win, at, { source: { ...source, offset: from } }); return; }
      }
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
    composers:     'composer',
    games:         'game',
    platforms:     'platform',
    parties:       'party',
  };
  const GROUP_ENDPOINTS = {
    artists:       '/api/library/artists',
    album_artists: '/api/library/album-artists',
    albums:        '/api/library/albums',
    genres:        '/api/library/genres',
    years:         '/api/library/years',
    composers:     '/api/library/composers',
    games:         '/api/library/games',
    platforms:     '/api/library/platforms',
    parties:       '/api/library/parties',
  };
  async function loadGroupList() {
    const gen = viewGen;
    loadEl.classList.remove('hidden');
    try {
      const res = await fetch(GROUP_ENDPOINTS[group]);
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
    } else if (item.label && !String(value).trim()) {
      // "[No Artist]" / "[No Album Artist]": the tracks whose tag is blank — the
      // rows the bucket counts.  (``artist=`` with no value is no filter at all:
      // it listed, played and shuffled the whole library.)
      crumb = { label: display, field: 'untagged', value: groupField, url: '/api/tracks' };
    } else {
      crumb = { label: display, field: groupField, value: String(value) };
    }
    render();
  }

  function emojiFor(g) {
    return ({
      artists: '🎤', album_artists: '🎤', albums: '💿', genres: '🏷', years: '📅',
      composers: '🎼', games: '🎮', platforms: '🕹', parties: '🎉',
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
    const cur = _nowPlayingId();
    for (const row of vl.rows()) {
      row.classList.toggle('playing', !!cur && row.dataset.trackId === cur);
    }
  }
  Player.on('trackchange', markPlaying);
  Player.on('statechange', markPlaying);       // (a start that failed: no row is lit)

  // First render
  render();
}
