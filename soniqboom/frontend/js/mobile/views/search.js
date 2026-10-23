// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * search.js — Mobile search view.  Sticky search field, debounced query.
 *
 * A search lists its WHOLE result set, Best match first (the server's default
 * order — newest first for a search with only field operators, ``ranked``
 * false): the server keeps it
 * (``GET /api/search/page`` — the exact total and a handle, ``rs``) and the list
 * reads it a page at a time as it is scrolled, in a pooled virtual list
 * (../../vlist.js) like the Library view.  A tap plays from that row with a
 * queue source covering every match (the set's own pages; Shuffle: the seeded
 * shuffle of the same search).  A page answered from another set (the library
 * changed meanwhile — ``renewed``) is never appended to rows of the old one:
 * the rows read so far are read again from the new set, in place.
 */
import { Player } from '../../player.js';
import { attachListGestures } from '../gestures.js';
import { fmtDur, trackActions } from './_common.js';
import { artPlaceholderEmoji, staticArtUrl } from '../../utils.js';
import { createVirtualList } from '../../vlist.js';
import { offlineKind, offlineBadge } from '../../offline.js';
import { healthBadge } from '../../health.js';

const DEBOUNCE_MS = 250;
const PAGE = 200;              // rows a page brings
const REREAD_MAX = 5000;       // rows read again in place when the set was replaced (one answer)
const QUEUE_WINDOW = 100;      // rows handed to the player up front (it extends itself)

// [m-search-pager:begin] — tests/js/search_set.test.mjs runs this region as is
// The rows of one search, read a page at a time.  ``fetchPage(q, rs, offset,
// limit)`` → ``{rs, total, tracks}``.  ``onChange(reason)``: the rows changed
// ('first' | 'more' | 'reread').  A page from another set than the one the rows
// came from (``rs`` differs) is not appended: the rows read so far are read
// again (up to ``REREAD_MAX``) in ONE answer — whichever set answers it — and
// its handle, rows and total replace the old ones together; a re-read that
// fails changes nothing (the next page asks again).
function createSearchPager(q, fetchPage, onChange) {
  const st = { q, rs: null, total: 0, rows: [], ended: false, busy: null, gone: false, ranked: true };
  async function load(offset, limit, rs = st.rs) {
    const j = await fetchPage(q, rs, offset, limit);
    if (st.gone) return null;
    if (!j || !Array.isArray(j.tracks) || typeof j.total !== 'number') throw new Error('not a track list');
    return j;
  }
  async function first() {
    const j = await load(0, PAGE);
    if (!j) return;
    st.rs = j.rs; st.total = j.total; st.rows = j.tracks; st.ranked = j.ranked !== false;
    st.ended = st.rows.length >= st.total || j.tracks.length < PAGE;
    onChange('first');
  }
  async function reread(j) {
    // ``j``: the answer that came from the newer set (its rs and total).
    const n = Math.min(Math.max(st.rows.length, PAGE), REREAD_MAX, j.total);
    const k = n ? await load(0, n, j.rs) : { rs: j.rs, total: j.total, tracks: [] };
    if (!k) return;
    st.rs = k.rs; st.total = k.total; st.rows = k.tracks;
    if (typeof k.ranked === 'boolean') st.ranked = k.ranked;
    st.ended = st.rows.length >= st.total || k.tracks.length < n;
    onChange('reread');
  }
  async function more() {
    if (st.ended || st.gone) return;
    if (st.busy) return st.busy;
    st.busy = (async () => {
      const j = await load(st.rows.length, PAGE);
      if (!j) return;
      if (j.rs !== st.rs) { await reread(j); return; }
      st.rows = st.rows.concat(j.tracks);
      st.ended = st.rows.length >= st.total || j.tracks.length < PAGE;
      onChange('more');
    })().finally(() => { st.busy = null; });
    return st.busy;
  }
  return { st, first, more, close() { st.gone = true; } };
}
// [m-search-pager:end]

function _nowPlayingId() {
  return ('nowPlayingId' in Player) ? Player.nowPlayingId : Player.currentTrackId;
}

async function _fetchPage(q, rs, offset, limit) {
  const p = new URLSearchParams({ q, offset: String(offset), limit: String(limit), fields: 'list' });
  if (rs) p.set('rs', rs);
  const res = await fetch(`/api/search/page?${p}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export function mountSearch(root, ctx) {
  let _timer = null;
  let _seq   = 0;
  let pager  = null;           // the search on screen (createSearchPager)

  const gctx = { player: Player, toast: ctx.toast, showSheet: ctx.showSheet };

  root.innerHTML = `
    <div class="m-search-bar">
      <input class="m-search-input" id="m-search-input" type="search"
             placeholder="Search… or try game:uridium"
             autocomplete="off" autocapitalize="off" autocorrect="off">
    </div>
    <div class="m-search-count hidden" id="m-search-count" aria-live="polite"></div>
    <ul class="m-list" id="m-search-list"></ul>
    <div class="m-empty hidden" id="m-search-empty"></div>
    <div class="m-loading hidden" id="m-search-loading">Searching…</div>
  `;

  const input   = root.querySelector('#m-search-input');
  const listEl  = root.querySelector('#m-search-list');
  const countEl = root.querySelector('#m-search-count');
  const empty   = root.querySelector('#m-search-empty');
  const loadEl  = root.querySelector('#m-search-loading');
  const bar     = root.querySelector('.m-search-bar');

  // ── Virtual list ──────────────────────────────────────────────────────
  let listTop = 0, barH = 0;
  function measureChrome() { listTop = listEl.offsetTop; barH = bar.offsetHeight; }

  function makeRow() {
    const row = document.createElement('div');
    row.className = 'm-row';
    row.innerHTML = `
      <div class="m-row-content">
        <div class="m-row-art"><span></span><img alt="" decoding="async"></div>
        <div class="m-row-meta"><div class="m-row-title"></div><div class="m-row-artist"></div></div>
        <span class="m-row-artist m-row-trail" style="flex-shrink:0;font-size:12px;margin-right:4px"></span>
      </div>`;
    const img = row.querySelector('img');
    img.onload  = () => img.classList.add('loaded');
    img.onerror = () => img.classList.remove('loaded');
    return row;
  }

  function fillRow(row, idx, fresh) {
    const content = row.firstElementChild;
    if (fresh && content.style.transform) { content.style.transform = ''; content.style.transition = ''; }
    row.dataset.idx = idx;
    const t = pager && pager.st.rows[idx];
    if (!t) return;
    row.classList.toggle('playing', !!t.id && t.id === _nowPlayingId());
    if (!fresh && row.__item === t) return;
    row.__item = t;
    row.__track = t;                        // (the shared health-badge repaint reads it)
    if (t.id) row.dataset.trackId = t.id; else delete row.dataset.trackId;
    const art = row.querySelector('.m-row-art');
    art.firstElementChild.textContent = artPlaceholderEmoji(t);
    const img = art.lastElementChild;
    const src = staticArtUrl(t.cover_art) || (t.id ? `/api/art/${t.id}/sm.jpg?fallback=none` : '');
    if (row.__artSrc !== src) {
      row.__artSrc = src;
      img.classList.remove('loaded');
      if (src) img.src = src; else img.removeAttribute('src');
    }
    const title = row.querySelector('.m-row-title');
    const health = healthBadge(t), off = offlineKind(t);
    row.classList.toggle('track-offline', !!off);
    if (health || off) {
      title.classList.add('m-row-title--badged');
      const txt = document.createElement('span');
      txt.className = 'm-row-title-text';
      txt.textContent = t.title || '—';
      const parts = [txt];
      if (health) parts.push(health);
      if (off) parts.push(offlineBadge(off));
      title.replaceChildren(...parts);
    } else {
      title.classList.remove('m-row-title--badged');
      title.textContent = t.title || '—';
    }
    row.querySelector('.m-row-meta .m-row-artist').textContent = t.artist || t.album_artist || '';
    row.querySelector('.m-row-trail').textContent = fmtDur(t.duration);
  }

  const vl = createVirtualList({
    scroller: root, host: listEl, buffer: 8, rowHeight: 61, autoScroll: true,
    make: makeRow,
    fill: fillRow,
    keyOf: (i) => (pager ? pager.st.rows[i] : undefined),
    spacer: () => { const d = document.createElement('div'); d.className = 'm-vl-spacer'; return d; },
    setSpacer: (el, px) => { el.style.height = px + 'px'; },
    offset: () => listTop,
    topInset: () => barH,
    onResize: measureChrome,
  });

  // The whole set to the player: its own pages from row ``idx`` on, and the
  // seeded shuffle of the same search — both cover every match.
  function playFrom(idx) {
    const st = pager && pager.st;
    if (!st || !st.rows[idx]) return;
    const source = {
      ordered: { url: '/api/search/page', params: { q: st.q, rs: st.rs } },
      shuffle: { url: '/api/tracks/shuffled', params: { q: st.q } },
      total: st.total,
      offset: idx,
      label: `Search: ${st.q}`,
    };
    Player.setQueue(st.rows.slice(idx, idx + QUEUE_WINDOW), 0, { source });
  }

  attachListGestures(listEl, (row) => {
    const idx = parseInt(row.dataset.idx, 10);
    const t = Number.isFinite(idx) && pager ? pager.st.rows[idx] : null;
    if (!t) return null;
    return {
      onTap:         () => playFrom(idx),
      onLongPress:   () => ctx.showSheet({ title: t.title || 'Track', actions: trackActions(t, gctx) }),
      onSwipeAction: () => { Player.addToQueue(t); ctx.toast('Added to queue'); },
      swipeLabel:    '+ Queue',
      swipeBgClass:  'queue',
    };
  });

  function paint(reason) {
    const st = pager.st;
    countEl.textContent = `${st.total.toLocaleString()} match${st.total === 1 ? '' : 'es'}`
      + (st.total > 1 ? ` · ${st.ranked ? 'Best match' : 'Newest first'}` : '');
    countEl.classList.toggle('hidden', !st.total);
    if (!st.total) {
      vl.reset();
      listEl.innerHTML = '';
      empty.textContent = `No matches for "${st.q}".`;
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');
    if (reason !== 'more') for (const row of vl.rows()) row.__item = null;   // (other rows under the same indexes)
    measureChrome();
    vl.setCount(st.rows.length);
    vl.render(true);
  }

  input.addEventListener('input', () => {
    clearTimeout(_timer);
    _timer = setTimeout(runQuery, DEBOUNCE_MS);
  });

  // Which tracks can play changed: run the search again (marks / hidden rows).
  document.addEventListener('soniqboom:availability-changed', () => {
    if (input.value.trim()) runQuery();
  });

  // Read the next page once the end of the rows read so far comes near.
  root.addEventListener('scroll', () => {
    if (!pager || pager.st.ended || pager.st.busy) return;
    if (root.scrollTop + root.clientHeight >= root.scrollHeight - 600) {
      const p = pager;
      p.more().catch(() => { if (p === pager) ctx.toast('Couldn’t load more results.', 'error'); });
    }
  }, { passive: true });

  function markPlaying() {
    const cur = _nowPlayingId();
    for (const row of vl.rows()) row.classList.toggle('playing', !!cur && row.dataset.trackId === cur);
  }
  Player.on('trackchange', markPlaying);
  Player.on('statechange', markPlaying);

  async function runQuery() {
    const q = input.value.trim();
    if (pager) pager.close();
    pager = null;
    vl.reset();
    listEl.innerHTML = '';
    root.scrollTop = 0;
    empty.classList.add('hidden');
    countEl.classList.add('hidden');
    if (!q) return;

    loadEl.classList.remove('hidden');
    const mySeq = ++_seq;
    const p = createSearchPager(q, _fetchPage, (reason) => {
      if (mySeq === _seq && p === pager) paint(reason);
    });
    pager = p;
    try {
      await p.first();
    } catch (err) {
      if (mySeq !== _seq) return;          // superseded by a later query
      console.error('Search failed', err);
      empty.textContent = 'Search failed.';
      empty.classList.remove('hidden');
    } finally {
      if (mySeq === _seq) loadEl.classList.add('hidden');
    }
  }
}
