// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * library.js — Mobile Library view.
 *
 * Group chips: All / Folders / Artists / Album Artists / Albums / Genres / Years.
 * - All:                flat track list (paginated lazily on scroll).
 * - Folders:            the music folders → one folder per screen (its
 *                       subfolders, then its own tracks); see "Folders" below.
 * - Artists/Albums/etc: group list → tap → filtered track list.
 *
 * Going into a folder or a group puts one entry in the browser's history, so
 * the system Back (Android, Safari's edge swipe) goes up a level instead of
 * leaving the page (the desktop library does the same).
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
import { folderLabel, isArchiveFolder, rootKind, rootLabel, rootOf, parentsText, folderScope,
         folderMatcher, savePath, restorePath } from '../folders.js';

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

  // Folders (see "Folders" below).  ``fStack``: the open folders, the music
  // folder first; the last one is shown.  Each keeps what was read of it, so
  // Back shows a folder as it was, where it was scrolled.
  let fStack   = [];
  let fRoots   = null;           // the music folders: [{ path, hash, name, kind, status, catalogue, count }]
  const fHome  = { scrollTop: 0, from: null, restore: null };   // the Folders home's own place
  let fPending = null;           // folders kept on this device: opened once the roots are known
  let fLast    = null;           // the folders open when one was last opened (the home's Continue)
  let fLastPending = null;       // …as kept on this device, until the roots are known
  let fRootsAt = 0;              // when the music folders were read (their status ages)
  let fBusy    = false;          // a Play / Shuffle start in flight
  let shownLevel;                // the folder drawn (null: the Folders home; undefined: neither)

  const gctx = { player: Player, toast: ctx.toast, showSheet: ctx.showSheet };

  // ── DOM scaffold ─────────────────────────────────────────────────────
  root.innerHTML = `
    <div class="m-group-bar" id="lib-groups">
      <button class="m-group-chip active" data-g="all">All</button>
      <button class="m-group-chip"        data-g="folders">Folders</button>
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
    <div class="m-lib-sticky" id="lib-sticky">
      <div class="m-crumb-bar hidden" id="lib-crumb">
        <button class="m-crumb-back" id="lib-back" aria-label="Back">←</button>
        <span class="m-crumb-text" id="lib-crumb-text"></span>
        <button class="m-crumb-back m-crumb-find hidden" id="lib-ffind" aria-label="Filter these folders"
                aria-pressed="false" aria-controls="lib-ffilter"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5"/></svg></button>
      </div>
      <div class="m-ffilter hidden" id="lib-ffilter">
        <input class="m-search-input" id="lib-ffilter-input" type="search" enterkeyhint="done"
               autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
               placeholder="Filter folders" aria-label="Folder name">
        <span class="m-ffilter-count" id="lib-ffilter-count" aria-live="polite"></span>
      </div>
    </div>
    <div class="m-fhead hidden" id="lib-fhead">
      <div class="m-fhead-keys">
        <button class="m-key" id="lib-fplay">▶︎ <span>Play</span></button>
        <button class="m-key" id="lib-fshuf">⇄ <span>Shuffle</span></button>
        <button class="m-key" id="lib-fmore" aria-label="More for this folder">⋯</button>
      </div>
      <div class="m-fhead-text" id="lib-fhead-text"></div>
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
  const stickyEl  = root.querySelector('#lib-sticky');
  const findKey   = root.querySelector('#lib-ffind');
  const filterRow = root.querySelector('#lib-ffilter');
  const filterIn  = root.querySelector('#lib-ffilter-input');
  const filterNum = root.querySelector('#lib-ffilter-count');
  const headEl    = root.querySelector('#lib-fhead');
  const headPlay  = root.querySelector('#lib-fplay');
  const headShuf  = root.querySelector('#lib-fshuf');
  const headMore  = root.querySelector('#lib-fmore');
  const headText  = root.querySelector('#lib-fhead-text');

  // ── Virtual list ─────────────────────────────────────────────────────
  // The list starts below the (sticky) chip bar and the crumb bar; both are
  // read once per list shown, not per frame.  Inside a folder the chip bar
  // steps aside and the folder bar (with the filter under it) is what sticks.
  let listTop = 0, barH = 0;
  function measureChrome() {
    listTop = listEl.offsetTop;
    barH = inFolder() ? stickyEl.offsetHeight : groupBar.offsetHeight;
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
  // A row shows a folder, or something else (the pool reuses rows for both).
  function setFolderRow(row, label) {
    row.classList.toggle('m-row--folder', label != null);
    if (label != null) {
      row.setAttribute('role', 'button');
      row.setAttribute('aria-label', `Folder, ${label}`);
      row.tabIndex = 0;                    // (a keyboard reaches it: see the keydown on the list)
    } else if (row.hasAttribute('role')) {
      row.removeAttribute('role');
      row.removeAttribute('aria-label');
      row.removeAttribute('tabindex');
    }
  }

  // A folder row: its name on up to two lines, the chevron.
  function paintFolderRow(row, f, fresh) {
    if (!f) return;
    if (!fresh && row.__item === f) return;
    row.__item = f;
    const label = folderLabel(f.name);
    setFolderRow(row, label);
    delete row.dataset.trackId;
    row.classList.remove('playing', 'track-offline');
    setArt(row, isArchiveFolder(f.name) ? '🗜' : '📁', '');
    setTitle(row, label);
    row.querySelector('.m-row-meta .m-row-artist').textContent = '';
    row.querySelector('.m-row-album').textContent = '';
    show(row.querySelector('.m-row-trail'), false);
    show(row.querySelector('.m-row-chev'), true);
    show(row.querySelector('.m-row-handle'), false);
  }

  function fillRow(row, idx, fresh) {
    const content = row.firstElementChild;
    if (fresh && content.style.transform) { content.style.transform = ''; content.style.transition = ''; }
    row.dataset.idx = idx;
    if (mode === 'folder') {
      // The open folder: its subfolders, then its own tracks.
      const lv = curLevel();
      const nf = lv ? lv.shown.length : 0;
      if (idx < nf) { paintFolderRow(row, lv.shown[idx], fresh); return; }
      paintTrackRow(row, tracks[idx - nf], fresh);
      return;
    }
    if (mode === 'groups') {
      const item = groupItems[idx];
      if (!item) return;
      if (!fresh && row.__item === item) return;
      row.__item = item;
      const { display } = groupValue(item);
      setFolderRow(row, null);
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
    paintTrackRow(row, tracks[idx], fresh);
  }

  function paintTrackRow(row, t, fresh) {
    if (!t) return;
    row.classList.toggle('playing', !!t.id && t.id === _nowPlayingId());
    if (!fresh && row.__item === t) return;
    row.__item = t;
    setFolderRow(row, null);
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
    keyOf: (i) => (mode === 'groups' ? groupItems[i] : mode === 'folder' ? folderItem(i) : tracks[i]),
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
    if (mode === 'folder') {
      // A subfolder opens on a tap (no swipe: a swipe that queued a whole
      // folder would be too easy to set off); a long-press offers it whole.
      const lv = curLevel();
      const nf = lv ? lv.shown.length : 0;
      if (idx < nf) {
        const f = lv.shown[idx];
        return f ? { onTap: () => openFolder(f), onLongPress: () => folderSheet(f.path, folderLabel(f.name)) } : null;
      }
      const ti = idx - nf;
      const ft = tracks[ti];
      if (!ft) return null;
      return {
        onTap: () => playFolderFrom(ti),
        onLongPress: () => ctx.showSheet({ title: ft.title || 'Track', actions: trackActions(ft, gctx) }),
        onSwipeAction: () => { Player.addToQueue(ft); ctx.toast('Added to queue'); },
        swipeLabel: '+ Queue',
        swipeBgClass: 'queue',
      };
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
    saveFolders();
    render();
  });

  // Back: one level up.  While a history entry stands for the level (see
  // "History" below), Back goes through it, so the system Back and this key
  // walk the same way.
  backBtn.addEventListener('click', () => {
    if (armed()) { try { history.back(); return; } catch (_) { /* go up here */ } }
    up();
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
    if (!ids.size || (mode !== 'tracks' && mode !== 'pltracks' && mode !== 'folder')) return;
    const before = tracks.length;
    tracks = tracks.filter((t) => !(t && ids.has(t.id)));
    const gone = before - tracks.length;
    if (!gone) return;
    if (mode === 'tracks') offset = Math.max(0, offset - gone);
    if (mode === 'folder') folderTracksLeft(gone);
    for (const row of vl.rows()) row.__item = null;
    vl.setCount(listCount());
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
    // (a folder lists every file — tracks that can't play are marked there,
    // never left out: its rows are re-read in place)
    if (mode !== 'tracks' && mode !== 'pltracks' && mode !== 'folder') return;
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
      if (!crumb && !inFolder()) revealSceneChips();
      if (mode === 'tracks' || mode === 'pltracks' || mode === 'folder') await rereadTracks();
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
    // A track list (and a folder's tracks) drops the rows that left the
    // library (the server's list shrank with them: the next page starts that
    // many rows earlier); a playlist keeps its entries — it is the server's to
    // say what it holds.
    if (gone && (kind === 'tracks' || kind === 'folder')) {
      if (gone === list.filter((t) => t && asked.has(t.id)).length) {
        if (kind === 'folder') forgetFolder(curLevel());      // (read the folder again)
        render();
        return;
      }
      tracks = list.filter((t) => !(t && asked.has(t.id) && !fresh.has(t.id)));
      if (kind === 'tracks') offset = Math.max(0, offset - gone);
      else folderTracksLeft(gone);
      for (const row of vl.rows()) row.__item = null;
      vl.setCount(listCount());
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
    // The same folder (or the Folders home) drawn again in place — a re-read,
    // a drive that came back — keeps its scroll position.
    noteFolderStay();
    viewGen++;
    syncHistory();
    // (the top of a list — a scan or an enrichment may have brought the
    // first Scene entries since: the chips are asked again)
    if (!crumb && !inFolder()) revealSceneChips();
    delete emptyEl.dataset.kind;
    vl.reset();
    mode = null;
    shownLevel = undefined;
    listEl.innerHTML = '';
    root.scrollTop = 0;
    tracks = [];
    groupItems = [];
    offset = 0;
    exhausted = false;
    // (Folders' own bar, filter and Play keys show only inside a folder)
    const inF = inFolder();
    root.classList.toggle('m-lib-folder', inF);
    if (!inF) {
      findKey.classList.add('hidden');
      filterRow.classList.add('hidden');
      headEl.classList.add('hidden');
      plainCrumb(crumb ? crumb.label : '');
    }

    if (group === 'folders') {
      crumbBar.classList.toggle('hidden', !inF);
      if (inF) loadFolder(); else loadFolderHome();
      return;
    }

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
    if (mode === 'folder') {             // (a folder's own tracks come page by page)
      if (root.scrollTop + root.clientHeight >= root.scrollHeight - 200) loadFolderPage();
      return;
    }
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
      // "[No Artist]" / "[No Album Artist]" / "[No Album]" / "[No Genre]": the
      // tracks whose tag is blank — the rows the bucket counts.  (``artist=``
      // with no value is no filter at all: it listed, played and shuffled the
      // whole library.)
      crumb = { label: display, field: 'untagged', value: groupField, url: '/api/tracks' };
    } else if (groupField === 'artist' && item.composer) {
      // An Artists row of a scene musician folds every spelling of his name
      // (the server's composer identity): its tracks are the composer's —
      // the rows the row counts.
      crumb = { label: display, field: 'composer', value: String(item.composer) };
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
    if (mode !== 'tracks' && mode !== 'pltracks' && mode !== 'folder') return;
    const cur = _nowPlayingId();
    for (const row of vl.rows()) {
      row.classList.toggle('playing', !!cur && row.dataset.trackId === cur);
    }
  }
  Player.on('trackchange', markPlaying);
  Player.on('statechange', markPlaying);       // (a start that failed: no row is lit)

  // ── Folders ──────────────────────────────────────────────────────────
  // The Folders key lists the music folders (GET /api/library/dirs), named by
  // their alias (GET /api/ui-config) or, a collection, by its label: a band
  // for the folders on drives and shares, one for the online collections.  A
  // healthy one shows no lamp; one that can't be reached is greyed, with a red
  // lamp and the note that its tracks are kept, and isn't opened (no request
  // goes to it).  An online collection that is offline still opens: it is
  // listed from its index.
  //
  // A folder opens on a screen of its own: its subfolders (GET
  // /api/fstree/children, in the server's order — numbers as numbers), then
  // its own tracks (GET /api/fstree/tracks-with-meta, page by page; the server
  // applies "Hide duplicates when browsing folders" and marks the tracks that
  // can't play).  Play and Shuffle head the list and say what they cover: a
  // folder's own tracks, or — a folder of folders — everything under it, as
  // the desktop's folder view plays it, through the same queue source.  A
  // folder of many subfolders opens with its filter field.
  //
  // Opening a folder here never asks the server to read the disk or the share
  // again (the desktop's folder view does): browsing on a phone doesn't make a
  // NAS rescan.

  const KIND_TEXT = { drive: 'Drive', share: 'Network share', collection: 'Online collection' };
  const FILTER_OPEN_AT = 50;       // subfolders: the filter field shows at once
  const QUEUE_ADD_MAX  = 500;      // tracks "Add to Queue" takes from a folder at most
  const FOLDERS_KEY    = 'sb_m_folders';

  function curLevel() { return fStack.length ? fStack[fStack.length - 1] : null; }
  function inFolder() { return group === 'folders' && fStack.length > 0; }
  function rootInfo(path) { return (fRoots || []).find((r) => r.path === path) || null; }
  // What row ``i`` of the open folder shows: a subfolder, then its tracks.
  function folderItem(i) {
    const lv = curLevel();
    if (!lv) return undefined;
    return i < lv.shown.length ? lv.shown[i] : tracks[i - lv.shown.length];
  }
  // (while its filter holds text, a folder lists only the matching subfolders)
  function folderCount(lv) { return lv ? lv.shown.length + (lv.filter ? 0 : lv.tracks.length) : 0; }
  function listCount() {
    return mode === 'folder' ? folderCount(curLevel()) : mode === 'groups' ? groupItems.length : tracks.length;
  }
  function newLevel(path, rootPath, name) {
    return { path, root: rootPath, name, children: null, shown: [], tracks: [], total: null, offset: 0,
             exhausted: false, loading: false, deep: null, filter: '', filterOpen: null,
             scrollTop: 0, from: null, restore: null, blocked: false };
  }
  // A folder read again from the server the next time it is drawn.
  function forgetFolder(lv) {
    if (lv) Object.assign(lv, { children: null, shown: [], tracks: [], total: null, offset: 0,
                                exhausted: false, deep: null });
  }
  // Rows the open folder's tracks lost (they left the library): the server's
  // list lost them too, so the next page starts that many rows earlier.
  function folderTracksLeft(gone) {
    const lv = curLevel();
    if (!lv) return;
    lv.tracks = tracks;
    lv.offset = Math.max(0, lv.offset - gone);
    if (typeof lv.total === 'number') lv.total = Math.max(0, lv.total - gone);
  }
  // The folder (or the home) on screen is about to be drawn again in place:
  // it comes back where it is scrolled.
  function noteFolderStay() {
    if (group !== 'folders') return;
    const lv = curLevel();
    if (lv && shownLevel === lv && mode === 'folder' && !lv.restore) {
      lv.scrollTop = root.scrollTop; lv.restore = 'stay';
    } else if (!lv && shownLevel === null && !fHome.restore) {
      fHome.scrollTop = root.scrollTop; fHome.restore = 'stay';
    }
  }

  // ── History ──
  // Going into a folder or a group puts ONE entry in the browser's history
  // (the desktop library does the same): the system Back — Android's, Safari's
  // edge swipe, a browser's — takes it out, and the view goes one level up and
  // puts a new entry in while it is still below the top.  The on-screen Back
  // goes through the same entry, so both walk alike; an open action sheet
  // closes first.  A tab switch keeps the entry (app.js keeps history.state);
  // Back from another tab returns to the Library, at the same folder.  Only an
  // entry this page put in counts (``_pushed``: a reload keeps history.state).
  const SENTINEL = 'sbLib';
  let _pushed = false, _consume = false;
  function isActive() { return root.classList.contains('active'); }
  function inDrill() { return inFolder() || !!crumb; }
  function armed() {
    try { return _pushed && !!(history.state && history.state[SENTINEL]); } catch (_) { return false; }
  }
  function arm() {
    if (!isActive() || !inDrill() || armed()) return;
    try { history.pushState({ [SENTINEL]: true }, ''); _pushed = true; } catch (_) { /* no history */ }
  }
  // The history follows the view: an entry while it is below the top, none at
  // the top (an entry left over is taken out — quietly).
  function syncHistory() {
    if (!isActive()) return;
    if (inDrill()) { arm(); return; }
    if (armed()) { _consume = true; try { history.back(); } catch (_) { _consume = false; } }
  }
  window.addEventListener('popstate', (e) => {
    if (e && e.sbSheet) return;            // (the action sheet's own entry: app.js closed it)
    if (_consume) { _consume = false; arm(); return; }
    if (!isActive()) return;
    if (ctx.sheetOpen && ctx.sheetOpen()) { ctx.hideSheet(); arm(); return; }
    up();
    arm();
  });
  // A reload keeps the current entry's state: an entry this view put in before
  // is still there — it counts as ours (a group or a folder that doesn't come
  // back takes it out at the first render, one that does uses it).
  try { if (history.state && history.state[SENTINEL]) _pushed = true; } catch (_) { /* no history */ }
  // Back on the Library from another tab: its level gets its entry again (or
  // one left over — the folder went away meanwhile — is taken out).
  root.addEventListener('viewactive', () => syncHistory());
  // The Library key pressed again while the Library shows: to the top of the
  // chosen list (the Folders home, the artists …), and then to its first row.
  root.addEventListener('viewreselect', () => {
    if (inFolder()) { goToDepth(0); return; }
    if (crumb) { crumb = null; render(); return; }
    try { root.scrollTo({ top: 0, behavior: 'smooth' }); } catch (_) { root.scrollTop = 0; }
  });

  // One level up: the folder it was opened from (or the home, or out of a group).
  function up() {
    if (inFolder()) { goToDepth(fStack.length - 1); return true; }
    if (crumb) { crumb = null; render(); return true; }
    return false;
  }
  // Straight to the level ``n`` folders down (0: the Folders home): it comes
  // back where it was scrolled, the folder you came from flashing.
  function goToDepth(n) {
    if (n < 0 || n >= fStack.length) return;
    // (the folder left keeps its place: Continue on the home goes back there)
    const leaving = curLevel();
    if (leaving && shownLevel === leaving && mode === 'folder') leaving.scrollTop = root.scrollTop;
    if (n === 0) fHome.from = fStack[0].path;     // (also after a reload: the home never opened it)
    fStack.length = n;
    const lv = curLevel();
    if (lv) lv.restore = 'back'; else fHome.restore = 'back';
    saveFolders();
    render();
  }

  // ── Kept on this device ──
  // The music folder's hash and the names of the folders below it (never a
  // share's address); and whether the Folders key was the one chosen.
  function saveFolders() {
    try {
      // (a folder found gone is not kept: the folders above it are)
      const gone = fStack.findIndex((l) => l.gone);
      const keep = gone >= 0 ? fStack.slice(0, gone) : fStack;
      const at = fRoots ? savePath(keep, fRoots) : fPending;
      const last = fRoots ? savePath(fLast || [], fRoots) : fLastPending;
      localStorage.setItem(FOLDERS_KEY, JSON.stringify({ open: group === 'folders', at: at || null, last: last || null }));
    } catch (_) { /* storage off: not kept */ }
  }
  // Levels for kept folders ([{path, name}]); each knows the one below it.
  function levelsOf(at) {
    const levels = at.map((e) => newLevel(e.path, at[0].path, e.name));
    levels.forEach((l, i) => { if (i + 1 < levels.length) l.from = levels[i + 1].path; });
    return levels;
  }
  function loadSavedFolders() {
    try {
      const v = JSON.parse(localStorage.getItem(FOLDERS_KEY) || 'null');
      return v && typeof v === 'object' ? v : null;
    } catch (_) { return null; }
  }

  // ── The music folders ──
  async function loadRoots() {
    const [dRes, cRes] = await Promise.all([
      fetch('/api/library/dirs'),
      fetch('/api/ui-config').catch(() => null),
    ]);
    if (!dRes.ok) throw new Error(`HTTP ${dRes.status}`);
    const dirs = (((await dRes.json()) || {}).dirs || []).filter((d) => d && typeof d.path === 'string' && d.path);
    let aliases = {};
    try { if (cRes && cRes.ok) aliases = ((await cRes.json()) || {}).folder_aliases || {}; } catch (_) { /* names from the paths */ }
    fRoots = dirs.map((d) => ({
      path: d.path, hash: d.path_hash || '', name: rootLabel(d, aliases), kind: rootKind(d),
      status: d.status === 'unavailable' ? 'unavailable' : 'ok', catalogue: d.catalogue || '',
      count: Number(d.track_count) || 0,
    }));
    fRootsAt = Date.now();
    return fRoots;
  }
  // A folder is opened with its music folder's status at most this old (the
  // server answers from a probe it keeps a few seconds).
  const ROOTS_FRESH_MS = 15000;
  // The music folder a path lies in can't be reached (an online collection
  // still opens): what to say instead of asking for it.
  function blockedNote(path) {
    const r = rootOf(path, fRoots);
    if (!r || r.status !== 'unavailable' || r.catalogue) return '';
    return `${r.name} can’t be reached right now. Its tracks are kept — reconnect the `
           + `${r.kind === 'drive' ? 'drive' : 'share'} to browse it.`;
  }

  async function loadFolderHome() {
    const gen = viewGen;
    shownLevel = null;
    loadEl.classList.remove('hidden');
    try {
      const roots = await loadRoots();
      if (gen !== viewGen) return;
      if (fLastPending) {                  // (the home's Continue, as kept on this device)
        const last = restorePath(fLastPending, roots);
        fLastPending = null;
        fLast = last.length ? levelsOf(last) : null;
      }
      if (fPending) {                      // (where the Folders key was left on this device)
        const at = restorePath(fPending, roots);
        fPending = null;
        if (at.length) {
          fStack = levelsOf(at);
          if (!fLast) fLast = fStack.slice();
          render();
          return;
        }
        saveFolders();
      }
      renderFolderHome(roots);
      syncHistory();                       // (a kept folder that didn't come back: no entry left)
    } catch (err) {
      console.error('Folders load failed', err);
      showLoadError(gen);
    } finally {
      if (gen === viewGen) loadEl.classList.add('hidden');
    }
  }

  // The home is a short list: plain rows, not the virtual list.
  function renderFolderHome(roots) {
    if (!roots.length) {
      emptyEl.textContent = 'No music folders yet — add one in Settings on the desktop page.';
      emptyEl.classList.remove('hidden');
      return;
    }
    emptyEl.classList.add('hidden');
    let from = null;
    // Continue: back to the last folder opened (below a music folder) — the
    // selector keys step aside inside a folder, so this is the way back to it
    // from another list.
    const last = fLast && fLast.length >= 2 && roots.some((r) => r.path === fLast[0].path) ? fLast : null;
    if (last) {
      const band = document.createElement('div');
      band.className = 'm-fband';
      band.setAttribute('role', 'heading');
      band.setAttribute('aria-level', '2');
      band.textContent = 'Continue';
      listEl.appendChild(band);
      listEl.appendChild(continueRow(last));
    }
    for (const [title, list] of [['Your folders', roots.filter((r) => r.kind !== 'collection')],
                                 ['Online collections', roots.filter((r) => r.kind === 'collection')]]) {
      if (!list.length) continue;
      const band = document.createElement('div');
      band.className = 'm-fband';
      band.setAttribute('role', 'heading');
      band.setAttribute('aria-level', '2');
      band.textContent = title;
      listEl.appendChild(band);
      for (const r of list) {
        const row = rootRow(r);
        if (r.path === fHome.from) from = row;
        listEl.appendChild(row);
      }
    }
    const how = fHome.restore;
    fHome.restore = null;
    if (how) root.scrollTop = fHome.scrollTop || 0;
    if (how === 'back' && from) pulseRow(from);
  }

  function rootRow(r) {
    const off = r.status === 'unavailable';
    const blocked = off && !r.catalogue;
    // (the badge says it can't be reached; a tap says what to do)
    const sub = blocked ? 'Its tracks are kept'
      : `${KIND_TEXT[r.kind]}${r.count ? ` · ${countTracks(r.count)}` : ''}`;
    const row = document.createElement('div');
    row.className = `m-row m-froot${blocked ? ' m-froot--off' : ''}`;
    row.innerHTML = `
      <div class="m-row-content">
        <div class="m-row-art"><span>${r.kind === 'drive' ? '📁' : '🌐'}</span></div>
        <div class="m-row-meta"><div class="m-row-title">${esc(r.name)}</div><div class="m-row-artist">${esc(sub)}</div></div>
        ${off ? `<span class="m-froot-badge">${r.catalogue ? 'offline' : 'unavailable'}</span><span class="m-froot-lamp" aria-hidden="true"></span>` : ''}
        ${blocked ? '' : '<span class="m-row-chev" aria-hidden="true">›</span>'}
      </div>`;
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.setAttribute('aria-label', `${r.name}, ${off ? (r.catalogue ? 'offline, ' : 'can’t be reached, ') : ''}${sub}`);
    _gestureCleanups.push(attachRowGestures(row, {
      onTap: () => openRoot(r),
      onLongPress: blocked ? undefined : () => folderSheet(r.path, r.name),
    }));
    // (a keyboard: Enter / Space opens it, the menu key or Shift+F10 its sheet)
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openRoot(r); }
      else if (!blocked && (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10'))) {
        e.preventDefault(); folderSheet(r.path, r.name);
      }
    });
    return row;
  }

  function continueRow(last) {
    const lv = last[last.length - 1];
    const name = folderLabel(lv.name);
    const above = parentsText(last.slice(0, -1).map((l) => l.name), 3);
    const row = document.createElement('div');
    row.className = 'm-row m-froot m-fcontinue';
    row.innerHTML = `
      <div class="m-row-content">
        <div class="m-row-art"><span>${isArchiveFolder(lv.name) ? '🗜' : '📁'}</span></div>
        <div class="m-row-meta"><div class="m-row-title">${esc(name)}</div><div class="m-row-artist">${esc(above)}</div></div>
        <span class="m-row-chev" aria-hidden="true">›</span>
      </div>`;
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.setAttribute('aria-label', `Continue in ${name}, in ${above}`);
    const go = () => {
      fStack = last.slice();
      const at = curLevel();
      at.restore = 'stay';                 // (as it was left)
      saveFolders();
      render();
    };
    _gestureCleanups.push(attachRowGestures(row, { onTap: go }));
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    return row;
  }

  function openRoot(r) {
    if (r.status === 'unavailable' && !r.catalogue) {
      ctx.toast(`${r.name} can’t be reached right now. Its tracks are kept — reconnect the `
                + `${r.kind === 'drive' ? 'drive' : 'share'} to browse it.`);
      return;
    }
    fHome.scrollTop = root.scrollTop;
    fHome.from = r.path;
    fStack = [newLevel(r.path, r.path, r.name)];
    fLast = fStack.slice();
    saveFolders();
    render();
  }

  function openFolder(f) {
    const lv = curLevel();
    if (!lv || !f || !f.path) return;
    lv.scrollTop = root.scrollTop;
    lv.from = f.path;
    lv.restore = null;
    fStack.push(newLevel(f.path, lv.root, f.name));
    fLast = fStack.slice();
    saveFolders();
    render();
  }

  // The bar: Back, the folder's name under the folders above it (tap it: a
  // sheet to go straight up to any of them), the filter key.
  function paintFolderBar(lv) {
    const parents = fStack.slice(0, -1).map((l) => l.name);
    const name = folderLabel(lv.name);
    const small = document.createElement('small');
    small.className = 'm-crumb-parent';
    small.textContent = `${parents.length ? parentsText(parents) : 'Folders'} ›`;
    const big = document.createElement('span');
    big.className = 'm-crumb-name';
    big.textContent = name;
    // (a button in the heading: a keyboard reaches the sheet too)
    const path = document.createElement('button');
    path.type = 'button';
    path.className = 'm-crumb-path';
    path.setAttribute('aria-label', parents.length ? 'Go to a folder above' : 'Go to the Folders list');
    path.setAttribute('aria-haspopup', 'dialog');
    path.append(small, big);
    crumbText.replaceChildren(path);
    crumbText.classList.add('m-crumb-text--path');
    crumbText.setAttribute('role', 'heading');
    crumbText.setAttribute('aria-level', '2');
    crumbText.setAttribute('aria-label', [name, ...parents.slice().reverse().map(folderLabel)].join(', in '));
    crumbText.tabIndex = -1;
    backBtn.setAttribute('aria-label', `Back to ${parents.length ? folderLabel(parents[parents.length - 1]) : 'Folders'}`);
    crumbBar.classList.remove('hidden');
  }
  // A group's crumb: its name only.
  function plainCrumb(label) {
    if (crumbText.classList.contains('m-crumb-text--path')) {
      crumbText.classList.remove('m-crumb-text--path');
      for (const a of ['role', 'aria-level', 'aria-label', 'tabindex']) crumbText.removeAttribute(a);
    }
    crumbText.textContent = label || '';
    backBtn.setAttribute('aria-label', 'Back');
  }
  crumbText.addEventListener('click', () => {
    if (!inFolder()) return;
    const lv = curLevel();
    ctx.showSheet({ title: folderLabel(lv.name), actions: [
      { label: '↑ Folders', onSelect: () => goToDepth(0) },
      ...fStack.slice(0, -1).map((l, i) => ({ label: `↑ ${folderLabel(l.name)}`, onSelect: () => goToDepth(i + 1) })),
    ]});
  });

  async function loadFolder() {
    const lv = curLevel();
    const gen = viewGen;
    shownLevel = lv;
    paintFolderBar(lv);
    // Its music folder's status first — read again when older than
    // ROOTS_FRESH_MS (or not read yet: a folder kept on this device).
    if (!fRoots || Date.now() - fRootsAt > ROOTS_FRESH_MS) {
      try { await loadRoots(); } catch (_) { /* as it was known */ }
      if (gen !== viewGen) return;
    }
    const r = rootInfo(lv.root);
    lv.blocked = !!(r && r.status === 'unavailable' && !r.catalogue);
    if (lv.blocked) { folderNote(lv, 'unavailable'); return; }
    if (lv.children) { paintFolder(lv); return; }       // (read before: as it was)
    loadEl.classList.remove('hidden');
    try {
      const q = (o) => new URLSearchParams(o).toString();
      const [cRes, tRes] = await Promise.all([
        fetch(`/api/fstree/children?${q({ path: lv.path, root: lv.root })}`),
        fetch(`/api/fstree/tracks-with-meta?${q({ path: lv.path, recursive: 'false', offset: '0', limit: String(PAGE_SIZE) })}`),
      ]);
      if (gen !== viewGen) return;
      if (cRes.status === 404 || tRes.status === 404) {
        // (moved, renamed or deleted since it was opened or kept)
        lv.gone = true;
        saveFolders();
        folderNote(lv, 'gone');
        return;
      }
      if (!cRes.ok || !tRes.ok) throw new Error(`HTTP ${cRes.status} / ${tRes.status}`);
      const c = await cRes.json();
      const t = await tRes.json();
      if (gen !== viewGen) return;
      lv.children = Array.isArray(c && c.children)
        ? c.children.filter((x) => x && typeof x.path === 'string' && typeof x.name === 'string') : [];
      takeFolderPage(lv, t, 0);
      paintFolder(lv);
      if (!lv.total && lv.children.length && lv.deep == null) countDeep(lv);
    } catch (err) {
      console.error('Folder load failed', err);
      if (gen === viewGen) folderNote(lv, 'error');
    } finally {
      if (gen === viewGen) loadEl.classList.add('hidden');
    }
  }

  // A page of the open folder's own tracks (``at``: the place of its first row).
  function takeFolderPage(lv, j, at) {
    const page = Array.isArray(j) ? j : (j && Array.isArray(j.tracks) ? j.tracks : []);
    if (j && !Array.isArray(j) && typeof j.total === 'number') lv.total = j.total;
    else if (lv.total == null) lv.total = page.length;
    page.forEach((t, i) => { if (t && typeof t === 'object') _posOf.set(t, at + i); });
    const rows = _unseen(lv.tracks, page);
    lv.offset = at + page.length;
    lv.exhausted = !page.length || lv.offset >= lv.total;
    if (rows.length) lv.tracks.push(...rows);
    return rows.length;
  }

  async function loadFolderPage() {
    const lv = curLevel();
    if (!lv || mode !== 'folder' || lv.exhausted || lv.loading || lv.filter) return;
    if (blockedNote(lv.path)) return;      // (its music folder went away: the note follows the event)
    lv.loading = true;
    const gen = viewGen;
    let again = false;
    try {
      const q = new URLSearchParams({ path: lv.path, recursive: 'false', offset: String(lv.offset), limit: String(PAGE_SIZE) });
      const r = await fetch(`/api/fstree/tracks-with-meta?${q}`);
      const j = r.ok ? await r.json() : null;
      if (gen !== viewGen || curLevel() !== lv) return;
      if (!j) throw new Error(`HTTP ${r.status}`);
      const added = takeFolderPage(lv, j, lv.offset);
      if (added) { vl.setCount(folderCount(lv)); vl.render(true); }
      paintFolderHead(lv);                 // (its length, once every track is read)
      again = !added && !lv.exhausted;     // (no row came into view: no scroll asks for more)
    } catch (err) {
      console.error('Folder page failed', err);
    } finally {
      lv.loading = false;
    }
    if (again && gen === viewGen) loadFolderPage();
  }

  // A folder of folders: how many tracks lie under it (Play all / Shuffle all).
  async function countDeep(lv) {
    if (blockedNote(lv.path)) return;
    try {
      const q = new URLSearchParams({ path: lv.path, recursive: 'true', offset: '0', limit: '1' });
      const r = await fetch(`/api/fstree/tracks-with-meta?${q}`);
      const j = r.ok ? await r.json() : null;
      if (j && typeof j.total === 'number') lv.deep = j.total;
    } catch (_) { return; }
    if (curLevel() === lv && mode === 'folder') paintFolderHead(lv);
  }

  function paintFolder(lv) {
    const m = folderMatcher(lv.filter);
    lv.shown = lv.filter ? lv.children.filter((f) => m(f.name)) : lv.children;
    tracks = lv.tracks;                    // (the shared track code reads ``tracks``)
    if (lv.filterOpen == null) lv.filterOpen = lv.children.length > FILTER_OPEN_AT;
    paintFilter(lv);
    paintFolderHead(lv);
    if (!lv.children.length && !lv.tracks.length) {
      emptyEl.textContent = 'No music in this folder.';
      emptyEl.classList.remove('hidden');
      return;
    }
    paintFilterEmpty(lv);
    showList('folder', folderCount(lv));
    const how = lv.restore;
    lv.restore = null;
    if (how) { root.scrollTop = lv.scrollTop || 0; vl.render(false); }
    if (how === 'back' && lv.from) {
      // Back from a subfolder: it flashes (and takes the focus).
      const i = lv.shown.findIndex((f) => f.path === lv.from);
      if (i >= 0) {
        vl.scrollToIndex(i, 'nearest');
        const row = vl.rowFor(i);
        if (row) pulseRow(row);
      }
    } else if (!how) {
      // A folder just opened: the focus (a screen reader, a keyboard) starts at its name.
      try { crumbText.focus({ preventScroll: true }); } catch (_) { /* old engine */ }
    }
  }

  function pulseRow(row) {
    row.classList.remove('m-row--pulse');
    void row.offsetWidth;                  // (the flash starts again)
    row.classList.add('m-row--pulse');
    setTimeout(() => row.classList.remove('m-row--pulse'), 2600);
    if (!row.hasAttribute('tabindex')) row.tabIndex = -1;
    try { row.focus({ preventScroll: true }); } catch (_) { /* old engine */ }
  }

  // A folder that can't be listed: why, and Try again.
  function folderNote(lv, kind) {
    mode = null;
    headEl.classList.add('hidden');
    findKey.classList.add('hidden');
    filterRow.classList.add('hidden');
    const r = rootInfo(lv.root);
    const what = r && r.kind === 'drive' ? 'drive' : 'share';
    const name = r ? r.name : 'This music folder';
    const msg = kind === 'unavailable'
      ? `${name} can’t be reached right now. Its tracks are kept — reconnect the ${what} to browse it.`
      : kind === 'gone' ? 'This folder isn’t there any more — it was moved, renamed or deleted.'
      : r && r.kind === 'collection' ? `Couldn’t list this folder of ${name}. Try again in a moment.`
      : `Couldn’t list this folder. The ${what} may be unreachable — its tracks are kept.`;
    const p = document.createElement('div');
    p.textContent = msg;
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'm-retry';
    if (kind === 'gone') {
      retry.textContent = 'Go up';
      retry.addEventListener('click', () => { if (armed()) history.back(); else up(); });
    } else {
      retry.textContent = 'Try again';
      retry.addEventListener('click', () => { forgetFolder(lv); refreshFolders(); });
    }
    emptyEl.replaceChildren(p, retry);
    emptyEl.dataset.kind = 'error';
    emptyEl.classList.remove('hidden');
  }

  // The music folders read again (a drive or a share came back or went away,
  // a music folder was removed): the home and the open folder follow.
  async function refreshFolders() {
    if (group !== 'folders') return;
    const gen = viewGen;
    let roots;
    try { roots = await loadRoots(); } catch (_) { return; }
    if (gen !== viewGen || group !== 'folders') return;
    if (!fStack.length) { render(); return; }
    const r = roots.find((x) => x.path === fStack[0].path);
    if (!r) { fStack = []; saveFolders(); render(); return; }   // (its music folder was removed)
    const lv = curLevel();
    const blocked = r.status === 'unavailable' && !r.catalogue;
    if (blocked !== lv.blocked || !lv.children) {
      const back = lv.blocked && !blocked;
      render();
      if (back) recheckSoon(lv);
    }
  }
  document.addEventListener('soniqboom:availability-changed', () => { refreshFolders(); });
  // A music folder that came back: which of its tracks can play follows on the
  // server a moment after its status (its watch, every 10 s) — the rows read in
  // between still say "offline", so they are asked again, twice.
  function recheckSoon(lv) {
    for (const ms of [2500, 12000]) {
      setTimeout(() => {
        if (curLevel() !== lv || mode !== 'folder') return;
        const list = tracks;
        Promise.resolve(Offline.recheckRows?.(list)).then((changed) => {
          if (!changed || tracks !== list) return;
          for (const row of vl.rows()) row.__item = null;
          vl.render(true);
        }).catch(() => {});
      }, ms);
    }
  }

  // ── Play and Shuffle ──
  function folderScopeOf(lv) {
    let seconds = null;
    if (lv.exhausted && lv.tracks.length === lv.total) {
      seconds = 0;
      for (const t of lv.tracks) {
        const d = Number(t && t.duration) || 0;
        if (!(d > 0)) { seconds = null; break; }
        seconds += d;
      }
    }
    return folderScope({ direct: lv.total, folders: lv.children ? lv.children.length : 0, deep: lv.deep, seconds });
  }
  function paintFolderHead(lv) {
    const sc = folderScopeOf(lv);
    const on = !lv.filter && sc.scope !== 'none';
    const was = !headEl.classList.contains('hidden');
    headEl.classList.toggle('hidden', !on);
    if (on) {
      headPlay.lastElementChild.textContent = sc.play;
      headShuf.lastElementChild.textContent = sc.shuffle;
      headText.textContent = sc.text;
    }
    if (was !== on && mode === 'folder') { measureChrome(); vl.render(true); }
  }
  // The queue source of a folder (the desktop folder view's): page by page in
  // folder order, or a seeded shuffle of all of it.
  function folderSource(path, recursive, label, total = null) {
    const params = { path, recursive: String(!!recursive) };
    return { ordered: { url: '/api/fstree/tracks-with-meta', params },
             shuffle: { url: '/api/fstree/tracks-with-meta', params, seedParam: 'shuffle_seed' },
             total: typeof total === 'number' ? total : null, label };
  }
  async function startSource(src, shuffle, key = null) {
    if (fBusy) return;                     // (a second tap while the first one starts)
    const note = blockedNote(src.ordered.params.path);
    if (note) { ctx.toast(note); return; }
    fBusy = true;
    if (key) key.setAttribute('aria-busy', 'true');
    try {
      const ok = shuffle ? await Player.playSource(src, { shuffle: true })
                         : await Player.playSource(src, { offset: 0, explicit: false });
      if (ok && shuffle) {
        const total = Player.queueSource?.total ?? src.total;
        ctx.toast(`Shuffling ${typeof total === 'number' && total > 0 ? countTracks(total) : 'every track'} · ${src.label}`);
      } else if (ok === false && !Player.lastStartAllOffline) {
        ctx.toast(shuffle ? 'Couldn’t start shuffle — try again.' : 'Couldn’t start playback — try again.', 'error');
      }
    } catch (_) {
      ctx.toast('Couldn’t start playback — try again.', 'error');
    } finally {
      fBusy = false;
      if (key) key.removeAttribute('aria-busy');
    }
  }
  function playOpenFolder(shuffle, key) {
    const lv = curLevel();
    if (!lv) return;
    const sc = folderScopeOf(lv);
    if (sc.scope === 'none') return;
    const deep = sc.scope === 'deep';
    startSource(folderSource(lv.path, deep, folderLabel(lv.name), deep ? lv.deep : lv.total), shuffle, key);
  }
  headPlay.addEventListener('click', () => playOpenFolder(false, headPlay));
  headShuf.addEventListener('click', () => playOpenFolder(true, headShuf));
  headMore.addEventListener('click', () => {
    const lv = curLevel();
    if (!lv) return;
    const sc = folderScopeOf(lv);
    const name = folderLabel(lv.name);
    const both = sc.scope === 'direct' && lv.children && lv.children.length > 0;
    const actions = [];
    if (both) {
      const all = () => folderSource(lv.path, true, name, lv.deep);
      actions.push({ label: '▶︎ Play all, with subfolders', onSelect: () => startSource(all(), false) },
                   { label: '⇄ Shuffle all, with subfolders', onSelect: () => startSource(all(), true) });
    }
    actions.push({ label: '+ Add to Queue', onSelect: () => queueFolder(lv.path, name, sc.scope === 'deep') });
    if (both) actions.push({ label: '+ Add to Queue, with subfolders', onSelect: () => queueFolder(lv.path, name, true) });
    ctx.showSheet({ title: name, actions });
  });

  // A subfolder or a music folder held down: it, whole.
  function folderSheet(path, label) {
    ctx.showSheet({ title: label, actions: [
      { label: '▶︎ Play', onSelect: () => startSource(folderSource(path, true, label), false) },
      { label: '⇄ Shuffle', onSelect: () => startSource(folderSource(path, true, label), true) },
      { label: '+ Add to Queue', onSelect: () => queueFolder(path, label, true) },
    ]});
  }

  // "Add to Queue" for a folder: its tracks when there are no more than
  // QUEUE_ADD_MAX (a queue of 80,000 rows would be read row by row) — else
  // Play or Shuffle, which read the folder as they go.
  async function queueFolder(path, label, recursive) {
    const note = blockedNote(path);
    if (note) { ctx.toast(note); return; }
    let j = null;
    try {
      const q = new URLSearchParams({ path, recursive: String(!!recursive), offset: '0', limit: String(QUEUE_ADD_MAX) });
      const r = await fetch(`/api/fstree/tracks-with-meta?${q}`);
      j = r.ok ? await r.json() : null;
    } catch (_) { j = null; }
    const rows = j && Array.isArray(j.tracks) ? j.tracks.filter((t) => t && t.id) : null;
    if (!rows) { ctx.toast('Couldn’t read this folder — try again.', 'error'); return; }
    const total = typeof j.total === 'number' ? j.total : rows.length;
    if (!rows.length) { ctx.toast(`${label} has no tracks to add.`); return; }
    if (total > QUEUE_ADD_MAX) {
      ctx.toast(`${label} holds ${countTracks(total)} — more than the ${QUEUE_ADD_MAX} that can be added at once. Use Play or Shuffle.`);
      return;
    }
    Player.addAllToQueue(rows);
    ctx.toast(`Added ${countTracks(rows.length)} to the queue`);
  }

  // A track of the open folder tapped: the folder plays from there and on
  // (the same window as "play from here" in a track list, from the folder).
  async function playFolderFrom(ti) {
    const lv = curLevel();
    if (!lv) return;
    const note = blockedNote(lv.path);
    if (note) { ctx.toast(note); return; }
    const pick = tracks[ti];
    const src = folderSource(lv.path, false, folderLabel(lv.name), lv.total);
    const starts = [pick ? _posOf.get(pick) : undefined, ti]
      .filter((n, i, all) => Number.isInteger(n) && n >= 0 && all.indexOf(n) === i);
    try {
      for (const from of starts) {
        const q = new URLSearchParams({ path: lv.path, recursive: 'false', offset: String(from), limit: String(QUEUE_WINDOW) });
        const j = await fetch(`/api/fstree/tracks-with-meta?${q}`).then((r) => (r.ok ? r.json() : null));
        const win = Array.isArray(j) ? j : (j && Array.isArray(j.tracks) ? j.tracks : null);
        if (!win) break;
        const at = pick ? win.findIndex((t) => t && t.id === pick.id) : -1;
        if (at >= 0) { Player.setQueue(win, at, { source: { ...src, offset: from } }); return; }
      }
    } catch (_) { /* the loaded rows, below */ }
    Player.setQueue(tracks, ti);
  }

  // ── The filter ──
  // A key in the bar opens a field over the folder's subfolders: what is typed
  // must begin a word of the name (any part of it in Japanese, Chinese, Korean
  // or Thai).  While it holds text, the folder lists only the matching
  // subfolders, and Play / Shuffle (which would play the whole folder) step
  // aside.  Typing with an input method is read once a word is composed.
  function paintFilter(lv) {
    const n = lv.children ? lv.children.length : 0;
    const can = n >= 2;
    findKey.classList.toggle('hidden', !can);
    const open = can && !!lv.filterOpen;
    findKey.setAttribute('aria-pressed', open ? 'true' : 'false');
    filterRow.classList.toggle('hidden', !open);
    if (!open) return;
    if (filterIn.value !== lv.filter) filterIn.value = lv.filter;
    filterNum.textContent = lv.filter ? `${lv.shown.length.toLocaleString()} of ${countNoun(n, 'folder')}`
                                      : countNoun(n, 'folder');
  }
  function paintFilterEmpty(lv) {
    if (lv.filter && !lv.shown.length) {
      emptyEl.textContent = `No folder here matches “${lv.filter.trim()}”.`;
      emptyEl.classList.remove('hidden');
    } else {
      emptyEl.classList.add('hidden');
    }
  }
  function refilter(lv) {
    const m = folderMatcher(lv.filter);
    lv.shown = lv.filter ? lv.children.filter((f) => m(f.name)) : lv.children;
    paintFilter(lv);
    paintFolderHead(lv);
    paintFilterEmpty(lv);
    for (const row of vl.rows()) row.__item = null;
    if (mode !== 'folder') showList('folder', folderCount(lv));
    else { measureChrome(); vl.setCount(folderCount(lv)); }
    root.scrollTop = 0;
    vl.render(true);
  }
  function applyFilter() {
    const lv = curLevel();
    if (!lv || !lv.children || lv.blocked) return;
    if (filterIn.value === lv.filter) return;
    lv.filter = filterIn.value;
    refilter(lv);
  }
  let _composing = false;
  filterIn.addEventListener('compositionstart', () => { _composing = true; });
  filterIn.addEventListener('compositionend', () => { _composing = false; applyFilter(); });
  filterIn.addEventListener('input', (e) => { if (_composing || e.isComposing) return; applyFilter(); });
  filterIn.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); filterIn.value = ''; applyFilter(); }
    else if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); filterIn.blur(); }
  });
  findKey.addEventListener('click', () => {
    const lv = curLevel();
    if (!lv || !lv.children) return;
    lv.filterOpen = !lv.filterOpen;
    if (!lv.filterOpen && lv.filter) { lv.filter = ''; filterIn.value = ''; refilter(lv); }
    paintFilter(lv);
    measureChrome();
    vl.render(true);
    if (lv.filterOpen) { try { filterIn.focus(); } catch (_) { /* old engine */ } }
  });

  // A keyboard on a folder row: Enter / Space opens it, the menu key (or
  // Shift+F10) its sheet.
  listEl.addEventListener('keydown', (e) => {
    if (mode !== 'folder') return;
    const row = e.target && e.target.closest ? e.target.closest('.m-row--folder') : null;
    if (!row || !listEl.contains(row)) return;
    const lv = curLevel();
    const f = lv && lv.shown[parseInt(row.dataset.idx, 10)];
    if (!f) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openFolder(f); }
    else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
      e.preventDefault(); folderSheet(f.path, folderLabel(f.name));
    }
  });

  // Where the Folders key was left on this device: back there once the music
  // folders are known (the Folders key chosen, or at its next press).
  {
    const saved = loadSavedFolders();
    if (saved && saved.at) fPending = saved.at;
    if (saved && saved.last) fLastPending = saved.last;
    if (saved && saved.open) {
      group = 'folders';
      [...groupBar.children].forEach((c) => c.classList.toggle('active', c.dataset.g === 'folders'));
    }
  }

  // First render
  render();
}
