// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * search.js — Mobile search view.  Sticky search field, debounced query.
 *
 * A search lists its WHOLE result set, Best match first (the server's default
 * order — newest first for a search with only field operators, ``ranked``
 * false; album-artist matches first for an ``artist:`` one, ``artist_first``;
 * a scoped search is sent ``rank_by`` — ../search_query.js ``rankBy`` — and
 * comes Best match for its words on that field):
 * the server keeps it
 * (``GET /api/search/page`` — the exact total and a handle, ``rs``) and the list
 * reads it a page at a time as it is scrolled, in a pooled virtual list
 * (../../vlist.js) like the Library view.  A tap plays from that row with a
 * queue source covering every match (the set's own pages; Shuffle: the seeded
 * shuffle of the same search).  A page answered from another set (the library
 * changed meanwhile — ``renewed``) is never appended to rows of the old one:
 * the rows read so far are read again from the new set, in place.
 *
 * Under the field: scope keys (ALL / TITLE / ARTIST / ALBUM / GENRE / GAME —
 * a radiogroup; the chosen one's lamp lit) and FILTERS, which opens a sheet of
 * filters (year, format, added, rating, length; SID chip, channels, tunes when
 * the library has retro formats).  The query sent is built from the text as
 * typed, the scope and the filters (../search_query.js ``buildQuery``; the
 * field is never rewritten); each applied filter is a key under the field (tap:
 * remove it), CLEAR FILTERS removes them all.  The scope and the filters are
 * kept per device (localStorage ``sb_m_search``).  A scope key or a filter key
 * tapped while typing keeps the field focused (the keyboard stays up).
 */
import { Player } from '../../player.js';
import { attachListGestures } from '../gestures.js';
import { countNoun, esc, fmtDur, trackActions } from './_common.js';
import {
  SCOPES, FILTERS, FILTER_ORDER, FORMAT_FAMILIES, buildQuery, filterChips, normalizeFilters,
  rankBy, scopeLabel, scopeOf,
} from '../search_query.js';
import { artPlaceholderEmoji, staticArtUrl } from '../../utils.js';
import { createVirtualList } from '../../vlist.js';
import { offlineKind, offlineBadge } from '../../offline.js';
import { healthBadge } from '../../health.js';

const DEBOUNCE_MS = 250;
const PAGE = 200;              // rows a page brings
const REREAD_MAX = 5000;       // rows read again in place when the set was replaced (one answer)
const QUEUE_WINDOW = 100;      // rows handed to the player up front (it extends itself)
const STORE_KEY = 'sb_m_search';   // {scope, filters} — this device's last choice
const YEAR_INPUT_MS = 500;     // a typed year applies after this pause
const FORMAT_KEYS = 10;        // the library's most common formats offered as keys
const DECADES = [1960, 1970, 1980, 1990, 2000, 2010, 2020];

// The placeholder and the intro line of each scope.
const _HINT = {
  all:    ['Search… or try game:uridium', 'a title, an artist, an album, a genre or a game'],
  title:  ['Title…', 'a title'],
  artist: ['Artist…', 'an artist or album artist'],
  album:  ['Album…', 'an album'],
  genre:  ['Genre…', 'a genre'],
  game:   ['Game…', 'a game (C64, Atari ST, console rips)'],
};

function _loadChoice() {
  try {
    const j = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (j && typeof j === 'object') return { scope: scopeOf(j.scope), filters: normalizeFilters(j.filters) };
  } catch (_) { /* no storage / bad JSON: defaults */ }
  return { scope: 'all', filters: {} };
}
function _saveChoice(scope, filters) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify({ scope, filters })); } catch (_) { /* private mode */ }
}

// [m-search-pager:begin] — tests/js/search_set.test.mjs runs this region as is
// The rows of one search, read a page at a time.  ``fetchPage(q, rs, offset,
// limit)`` → ``{rs, total, tracks}``.  ``onChange(reason)``: the rows changed
// ('first' | 'more' | 'reread').  A page from another set than the one the rows
// came from (``rs`` differs) is not appended: the rows read so far are read
// again (up to ``REREAD_MAX``) in ONE answer — whichever set answers it — and
// its handle, rows and total replace the old ones together; a re-read that
// fails changes nothing (the next page asks again).
function createSearchPager(q, fetchPage, onChange) {
  const st = { q, rs: null, total: 0, rows: [], ended: false, busy: null, gone: false, ranked: true,
               artistFirst: false };
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
    st.artistFirst = j.artist_first === true;
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
    if (typeof k.artist_first === 'boolean') st.artistFirst = k.artist_first;
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

// ``rank``: the scope's operator the set is ranked by (``rankBy``), or null.
async function _fetchPage(q, rs, offset, limit, rank = null) {
  const p = new URLSearchParams({ q, offset: String(offset), limit: String(limit), fields: 'list' });
  if (rs) p.set('rs', rs);
  if (rank) p.set('rank_by', rank);
  const res = await fetch(`/api/search/page?${p}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export function mountSearch(root, ctx) {
  let _timer = null;
  let _seq   = 0;
  let pager  = null;           // the search on screen (createSearchPager)
  const choice = _loadChoice();
  let scope   = choice.scope;  // a SCOPES key
  let filters = choice.filters;
  let formats = null;          // /api/library/formats rows (the sheet's format keys), once read
  let shown   = null;          // {text, scope, filters} of the search on screen

  const gctx = { player: Player, toast: ctx.toast, showSheet: ctx.showSheet };

  const scopeKeys = SCOPES.map(([k, label]) =>
    `<button type="button" role="radio" class="m-group-chip m-search-scope" data-scope="${k}"
             aria-checked="false" tabindex="-1">${esc(label)}</button>`).join('');

  root.innerHTML = `
    <div class="m-search-bar">
      <div class="m-search-row">
        <div class="m-search-field">
          <input class="m-search-input" id="m-search-input" type="search" enterkeyhint="search"
                 aria-label="Search" placeholder="${esc(_HINT.all[0])}"
                 autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
          <button type="button" class="m-search-clear" id="m-search-clear" aria-label="Clear the search text" hidden>×</button>
        </div>
        <div class="m-search-scopes" id="m-search-scopes" role="radiogroup" aria-label="Search in">${scopeKeys}</div>
        <button type="button" class="m-key m-search-filters-btn" id="m-search-filters-btn"
                aria-haspopup="dialog" aria-expanded="false" aria-controls="m-fsheet">Filters<span class="m-search-fcount" id="m-search-fcount"></span></button>
      </div>
      <div class="m-search-applied" id="m-search-applied" role="group" aria-label="Filters applied" hidden></div>
    </div>
    <div class="m-search-count hidden" id="m-search-count" aria-live="polite"></div>
    <ul class="m-list" id="m-search-list"></ul>
    <div class="m-search-intro" id="m-search-intro">
      <div class="m-search-intro-title" id="m-search-intro-title"></div>
      <p class="m-search-intro-text" id="m-search-intro-text"></p>
      <p class="m-search-intro-text">Pick ALL, TITLE, ARTIST, ALBUM, GENRE or GAME to choose where the words are looked for.</p>
      <p class="m-search-intro-text">FILTERS narrows by year, format, when added, rating or length — with or without words.</p>
      <p class="m-search-intro-ops">Operators work too: <span>artist:vnv year:&gt;2000</span> <span>game:uridium</span></p>
    </div>
    <div class="m-empty hidden" id="m-search-empty"></div>
    <div class="m-loading hidden" id="m-search-loading">Searching…</div>
    <div class="m-sheet-backdrop m-fsheet-bg hidden" id="m-fsheet-bg"></div>
    <div class="m-sheet m-fsheet hidden" id="m-fsheet" role="dialog" aria-modal="true" aria-labelledby="m-fsheet-title">
      <div class="m-sheet-header" id="m-fsheet-title">Filters</div>
      <div class="m-fsheet-body" id="m-fsheet-body"></div>
      <div class="m-fsheet-foot">
        <button type="button" class="m-key" id="m-fsheet-clear">Clear filters</button>
        <button type="button" class="m-key m-fsheet-done" id="m-fsheet-done">Done</button>
      </div>
    </div>
  `;

  const input   = root.querySelector('#m-search-input');
  const clearX  = root.querySelector('#m-search-clear');
  const scopesEl = root.querySelector('#m-search-scopes');
  const filtersBtn = root.querySelector('#m-search-filters-btn');
  const fcountEl = root.querySelector('#m-search-fcount');
  const appliedEl = root.querySelector('#m-search-applied');
  const listEl  = root.querySelector('#m-search-list');
  const countEl = root.querySelector('#m-search-count');
  const introEl = root.querySelector('#m-search-intro');
  const empty   = root.querySelector('#m-search-empty');
  const loadEl  = root.querySelector('#m-search-loading');
  const bar     = root.querySelector('.m-search-bar');
  const sheet   = root.querySelector('#m-fsheet');
  const sheetBg = root.querySelector('#m-fsheet-bg');
  const sheetBody = root.querySelector('#m-fsheet-body');

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
        <span class="m-row-album"></span>
        <span class="m-row-artist m-row-trail"></span>
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
    row.querySelector('.m-row-album').textContent = t.album || '';
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
    // (the same ``rank_by`` as the list's pages: the queue reads the same
    // set, in the order shown)
    const params = { q: st.q, rs: st.rs };
    if (pager.rankBy) params.rank_by = pager.rankBy;
    const source = {
      ordered: { url: '/api/search/page', params },
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

  // What the header and the "no matches" line say about the search shown.
  function _order(st) {
    return st.ranked ? 'Best match' : st.artistFirst ? 'Album artist first' : 'Newest first';
  }
  function paint(reason) {
    const st = pager.st;
    // (the scope is named when it applies: some free text was searched in it)
    const scoped = !!shown && shown.scope !== 'all'
      && buildQuery(shown) !== buildQuery({ ...shown, scope: 'all' });
    const sc = scoped ? ` · ${scopeLabel(shown.scope)}` : '';
    countEl.textContent = `${countNoun(st.total, 'match', 'matches')}${sc}`
      + (st.total > 1 ? ` · ${_order(st)}` : '');
    countEl.classList.toggle('hidden', !st.total);
    if (!st.total) {
      vl.reset();
      listEl.innerHTML = '';
      const text = shown ? shown.text : st.q;
      const nf = shown ? Object.keys(shown.filters).length : 0;
      empty.textContent = text
        ? `No matches for "${text}"${scoped ? ` in ${scopeLabel(shown.scope)}` : ''}`
          + `${nf ? (nf === 1 ? ' with this filter' : ' with these filters') : ''}.`
        : `No matches for ${nf === 1 ? 'this filter' : 'these filters'}.`;
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');
    if (reason !== 'more') for (const row of vl.rows()) row.__item = null;   // (other rows under the same indexes)
    measureChrome();
    vl.setCount(st.rows.length);
    vl.render(true);
  }

  // ── Scope keys, applied filters, intro ─────────────────────────────────
  // (a narrow phone: FILTERS shares the field's row — the long hint wouldn't fit)
  const _narrow = matchMedia('(max-width: 599px)');
  _narrow.addEventListener?.('change', () => paintScope());

  // The scope keys scroll sideways where they don't fit: the edge with more
  // keys behind it fades.
  function paintFade() {
    const more = scopesEl.scrollWidth - scopesEl.clientWidth;
    scopesEl.classList.toggle('m-fade-r', more > 2 && scopesEl.scrollLeft < more - 2);
    scopesEl.classList.toggle('m-fade-l', more > 2 && scopesEl.scrollLeft > 2);
  }
  scopesEl.addEventListener('scroll', paintFade, { passive: true });
  if (typeof ResizeObserver === 'function') new ResizeObserver(paintFade).observe(scopesEl);

  function paintScope() {
    for (const b of scopesEl.children) {
      const on = b.dataset.scope === scope;
      b.classList.toggle('active', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    }
    input.placeholder = scope === 'all' && _narrow.matches ? 'Search…' : (_HINT[scope] || _HINT.all)[0];
    input.setAttribute('aria-label', scope === 'all' ? 'Search' : `Search by ${scopeLabel(scope).toLowerCase()}`);
    root.querySelector('#m-search-intro-title').textContent =
      scope === 'all' ? 'Search everything' : `Search by ${scopeLabel(scope)}`;
    root.querySelector('#m-search-intro-text').textContent = `Type ${(_HINT[scope] || _HINT.all)[1]}.`;
  }

  function _formatLabel(v) {
    const row = formats && formats.find((f) => String(f.format).toLowerCase() === v.toLowerCase());
    return row ? row.format : v;
  }

  function paintApplied() {
    const chips = filterChips(filters, _formatLabel);
    const n = chips.length;
    fcountEl.textContent = n ? ` · ${n}` : '';
    filtersBtn.classList.toggle('m-search-filters-on', n > 0);
    filtersBtn.setAttribute('aria-label', n ? `Filters, ${n} applied` : 'Filters');
    appliedEl.replaceChildren();
    for (const c of chips) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'm-key m-search-chip';
      b.dataset.field = c.field;
      b.setAttribute('aria-label', c.aria);
      const t = document.createElement('span');
      t.textContent = c.label;
      const x = document.createElement('span');
      x.className = 'm-search-chip-x';
      x.setAttribute('aria-hidden', 'true');
      x.textContent = '×';
      b.append(t, x);
      appliedEl.appendChild(b);
    }
    if (n > 1) {
      const all = document.createElement('button');
      all.type = 'button';
      all.className = 'm-key m-search-chip m-search-chip-clear';
      all.dataset.field = '*';
      all.textContent = 'Clear filters';
      appliedEl.appendChild(all);
    }
    appliedEl.hidden = !n;
  }

  function paintIdle() {
    const idle = !buildQuery({ text: input.value, scope, filters });     // (nothing to search)
    introEl.classList.toggle('hidden', !idle);
    clearX.hidden = !input.value;
  }

  // A key tapped while the field has the focus (typing, the keyboard up):
  // the field keeps it.  ``mousedown``'s default would move the focus to the
  // key (Chromium, Firefox); WebKit doesn't focus a tapped button, and the
  // click re-focuses the field if anything took the focus anyway.
  let _typing = false;
  function keepTyping(el) {
    el.addEventListener('pointerdown', () => { _typing = document.activeElement === input; });
    el.addEventListener('mousedown', (e) => { if (document.activeElement === input) e.preventDefault(); });
  }
  function refocus() {
    if (_typing && document.activeElement !== input) {
      try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
    }
    _typing = false;
  }
  keepTyping(scopesEl); keepTyping(appliedEl); keepTyping(clearX);

  function setScope(next, { focusKey = false } = {}) {
    next = scopeOf(next);
    if (next === scope) return;
    scope = next;
    _saveChoice(scope, filters);
    paintScope();
    if (focusKey) scopesEl.querySelector(`[data-scope="${scope}"]`)?.focus();
    runNow();
  }
  scopesEl.addEventListener('click', (e) => {
    const b = e.target.closest('.m-search-scope');
    if (!b) return;
    setScope(b.dataset.scope);
    refocus();
  });
  // (a radiogroup: arrows move the choice, Home / End the ends)
  scopesEl.addEventListener('keydown', (e) => {
    const keys = SCOPES.map(([k]) => k);
    const i = keys.indexOf(scope);
    let j = null;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % keys.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + keys.length) % keys.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = keys.length - 1;
    if (j === null) return;
    e.preventDefault();
    setScope(keys[j], { focusKey: true });
    const b = scopesEl.querySelector(`[data-scope="${scope}"]`);
    b?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  });

  function setFilters(next) {
    filters = normalizeFilters(next);
    _saveChoice(scope, filters);
    paintApplied();
    if (!sheet.classList.contains('hidden')) syncSheet();
    measureChrome();
    runNow();
  }
  appliedEl.addEventListener('click', (e) => {
    const b = e.target.closest('.m-search-chip');
    if (!b) return;
    const field = b.dataset.field;
    const wasFocused = document.activeElement === b;
    if (field === '*') setFilters({});
    else { const next = { ...filters }; delete next[field]; setFilters(next); }
    if (_typing) refocus();
    else if (wasFocused) (appliedEl.querySelector('.m-search-chip') || filtersBtn).focus();   // (keyboard users)
    _typing = false;
  });

  clearX.addEventListener('click', () => {
    input.value = '';
    clearTimeout(_timer);
    runQuery();
    try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
    _typing = false;
  });

  // ── The filter sheet ───────────────────────────────────────────────────
  // Each field: a label printed on the plate and a row of keys (aria-pressed;
  // tap the lit one to take it off).  Year: decade keys and From / To fields.
  function _keyRow(field, label, items, current) {
    const sec = document.createElement('section');
    sec.className = 'm-fsec';
    sec.dataset.field = field;
    const h = document.createElement('h3');
    h.className = 'm-fsec-label';
    h.id = `m-fsec-${field}`;
    h.textContent = label;
    const row = document.createElement('div');
    row.className = 'm-fsec-keys';
    row.setAttribute('role', 'group');
    row.setAttribute('aria-labelledby', h.id);
    for (const [value, shownLabel, said] of items) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'm-group-chip m-fkey';
      b.dataset.field = field;
      b.dataset.value = value;
      const on = current === value;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (said && said !== shownLabel) b.setAttribute('aria-label', `${label} ${said}`);
      b.textContent = shownLabel;
      row.appendChild(b);
    }
    sec.append(h, row);
    return sec;
  }

  // The retro filters a library has formats for (all of them before the
  // formats are read).
  function _retroOffered(k) {
    if (!formats) return true;
    const has = (fam) => formats.some((f) => f.family === fam);
    if (k === 'chip') return has('chiptune');
    if (k === 'channels') return has('trackers');
    return has('trackers') || has('chiptune');        // tunes
  }

  // Build the sheet (on open, and when the formats arrive while it is open).
  function paintSheet() {
    const keep = document.activeElement && sheet.contains(document.activeElement)
      ? { field: document.activeElement.dataset.field, value: document.activeElement.dataset.value,
          id: document.activeElement.id } : null;
    const typed = keep && keep.id && keep.id.startsWith('m-fyear-')
      ? { from: sheetBody.querySelector('#m-fyear-from').value, to: sheetBody.querySelector('#m-fyear-to').value } : null;
    sheetBody.replaceChildren();
    // Year: decade keys, From / To
    const ysec = _keyRow('year', 'Year',
      DECADES.map((d) => [String(d), `${d}–${String(d + 9).slice(2)}`, `${d} to ${d + 9}`]), null);
    const yr = document.createElement('div');
    yr.className = 'm-fyear';
    yr.innerHTML = `
      <label class="m-fyear-label">From <input class="m-fyear-in" id="m-fyear-from" inputmode="numeric"
             pattern="[0-9]*" maxlength="4" autocomplete="off" placeholder="year" aria-label="Year from"></label>
      <label class="m-fyear-label">To <input class="m-fyear-in" id="m-fyear-to" inputmode="numeric"
             pattern="[0-9]*" maxlength="4" autocomplete="off" placeholder="year" aria-label="Year to"></label>`;
    ysec.appendChild(yr);
    sheetBody.appendChild(ysec);
    // Format: the families, then the library's most common formats
    const fams = FORMAT_FAMILIES.filter(([v]) => !formats
      || formats.some((f) => f.family === (v === 'tracker' ? 'trackers' : v)));
    const named = (formats || []).filter((f) => f && f.format && f.count > 0)
      .sort((a, b) => b.count - a.count).slice(0, FORMAT_KEYS)
      .map((f) => [String(f.format), String(f.format), `${f.format}`]);
    sheetBody.appendChild(_keyRow('format', 'Format', [...fams.map(([v, l]) => [v, l, l]), ...named], null));
    for (const k of FILTER_ORDER) {
      if (k === 'year' || k === 'format') continue;
      if ((k === 'chip' || k === 'channels' || k === 'tunes') && !_retroOffered(k) && !(k in filters)) continue;
      sheetBody.appendChild(_keyRow(k, FILTERS[k].label, FILTERS[k].values, null));
    }
    syncSheet();
    if (typed) {                          // (what was being typed stays as typed)
      sheetBody.querySelector('#m-fyear-from').value = typed.from;
      sheetBody.querySelector('#m-fyear-to').value = typed.to;
    }
    if (keep) {
      const el = keep.id ? sheetBody.querySelector(`#${keep.id}`)
        : sheetBody.querySelector(`.m-fkey[data-field="${keep.field}"][data-value="${CSS.escape(keep.value || '')}"]`);
      if (el) { el.focus({ preventScroll: true }); if (el.tagName === 'INPUT') { const n = el.value.length; el.setSelectionRange?.(n, n); } }
    }
  }

  // Show the filters on the sheet's keys and in From / To — the fields only
  // while neither is being typed in (a year being typed is never rewritten).
  function syncSheet() {
    const y = filters.year || { from: '', to: '' };
    for (const b of sheetBody.querySelectorAll('.m-fkey')) {
      const { field, value } = b.dataset;
      let on;
      if (field === 'year') on = y.from === value && y.to === String(Number(value) + 9);
      else if (field === 'format') on = !!filters.format && filters.format.toLowerCase() === value.toLowerCase();
      else on = filters[field] === value;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    const a = document.activeElement;
    if (!(a && a.classList && a.classList.contains('m-fyear-in'))) {
      const f = sheetBody.querySelector('#m-fyear-from'), t = sheetBody.querySelector('#m-fyear-to');
      if (f) f.value = y.from || '';
      if (t) t.value = y.to || '';
    }
  }

  sheetBody.addEventListener('click', (e) => {
    const b = e.target.closest('.m-fkey');
    if (!b) return;
    const { field, value } = b.dataset;
    const next = { ...filters };
    if (field === 'year') {
      const d = Number(value);
      const on = next.year && next.year.from === String(d) && next.year.to === String(d + 9);
      if (on) delete next.year; else next.year = { from: String(d), to: String(d + 9) };
    } else if (field === 'format') {
      if ((next.format || '').toLowerCase() === value.toLowerCase()) delete next.format; else next.format = value;
    } else if (next[field] === value) delete next[field];
    else next[field] = value;
    setFilters(next);
  });
  // From / To: a year applies once it has four digits (or a field is
  // emptied) and a moment passes; Enter or closing the sheet applies what is
  // there.
  let _yearTimer = null;
  sheetBody.addEventListener('input', (e) => {
    if (!e.target.classList.contains('m-fyear-in')) return;
    const clean = e.target.value.replace(/\D+/g, '').slice(0, 4);
    if (clean !== e.target.value) e.target.value = clean;
    clearTimeout(_yearTimer);
    _yearTimer = setTimeout(() => applyYear(false), YEAR_INPUT_MS);
  });
  sheetBody.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.classList.contains('m-fyear-in')) {
      e.preventDefault(); clearTimeout(_yearTimer); applyYear(true);
    }
  });
  function applyYear(force) {
    const fromEl = sheetBody.querySelector('#m-fyear-from'), toEl = sheetBody.querySelector('#m-fyear-to');
    if (!fromEl || !toEl) return;
    const from = fromEl.value.trim(), to = toEl.value.trim();
    if (!force && [from, to].some((v) => v && v.length < 4)) return;     // (still typing)
    const next = { ...filters };
    if (from || to) next.year = { from, to }; else delete next.year;
    const was = JSON.stringify(normalizeFilters(filters).year || null);
    if (JSON.stringify(normalizeFilters(next).year || null) !== was) setFilters(next);
  }

  async function loadFormats() {
    if (formats) return;
    try {
      const r = await fetch('/api/library/formats');
      if (!r.ok) return;
      const j = await r.json();
      if (!Array.isArray(j)) return;
      formats = j;
      if (!sheet.classList.contains('hidden')) paintSheet();
      paintApplied();
    } catch (_) { /* the families and the retro filters stay offered */ }
  }

  let _sheetOpener = null;
  function onSheetKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeSheet(); return; }
    if (e.key !== 'Tab') return;
    // (a modal: Tab stays in the sheet)
    const f = [...sheet.querySelectorAll('button, input')].filter((el) => el.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    const inside = sheet.contains(document.activeElement);
    if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
  }
  function openSheet() {
    _sheetOpener = document.activeElement;
    paintSheet();
    sheet.classList.remove('hidden');
    sheetBg.classList.remove('hidden');
    filtersBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('keydown', onSheetKey, true);
    (sheetBody.querySelector('.m-fkey') || sheet.querySelector('#m-fsheet-done')).focus({ preventScroll: true });
    loadFormats();
  }
  function closeSheet() {
    if (sheet.classList.contains('hidden')) return;
    clearTimeout(_yearTimer);
    applyYear(true);                      // (a year typed just before Done)
    sheet.classList.add('hidden');
    sheetBg.classList.add('hidden');
    filtersBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('keydown', onSheetKey, true);
    const back = _sheetOpener && _sheetOpener !== input && root.contains(_sheetOpener) ? _sheetOpener : filtersBtn;
    try { back.focus({ preventScroll: true }); } catch (_) {}
    _sheetOpener = null;
  }
  filtersBtn.addEventListener('click', openSheet);
  sheetBg.addEventListener('click', closeSheet);
  sheet.querySelector('#m-fsheet-done').addEventListener('click', closeSheet);
  sheet.querySelector('#m-fsheet-clear').addEventListener('click', () => {
    clearTimeout(_yearTimer);
    for (const el of sheetBody.querySelectorAll('.m-fyear-in')) el.value = '';
    setFilters({});
  });

  // ── Running the search ─────────────────────────────────────────────────
  input.addEventListener('input', () => {
    paintIdle();
    clearTimeout(_timer);
    _timer = setTimeout(runQuery, DEBOUNCE_MS);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    runNow();
    input.blur();                         // (the Search key: the results, without the keyboard)
  });
  function runNow() { clearTimeout(_timer); runQuery(); }

  // Which tracks can play changed: run the search again (marks / hidden rows).
  document.addEventListener('soniqboom:availability-changed', () => {
    if (buildQuery({ text: input.value, scope, filters })) runQuery();
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
    const text = input.value.trim();
    const q = buildQuery({ text, scope, filters });
    if (pager) pager.close();
    pager = null;
    shown = null;
    vl.reset();
    listEl.innerHTML = '';
    root.scrollTop = 0;
    empty.classList.add('hidden');
    delete empty.dataset.kind;
    countEl.classList.add('hidden');
    paintIdle();
    if (!q) { ++_seq; loadEl.classList.add('hidden'); return; }

    loadEl.classList.remove('hidden');
    const mySeq = ++_seq;
    const rank = rankBy({ text, scope });
    const p = createSearchPager(q, (qq, rs, off, lim) => _fetchPage(qq, rs, off, lim, rank), (reason) => {
      if (mySeq === _seq && p === pager) paint(reason);
    });
    p.rankBy = rank;
    pager = p;
    shown = { text, scope, filters };
    try {
      await p.first();
    } catch (err) {
      if (mySeq !== _seq) return;          // superseded by a later query
      console.error('Search failed', err);
      empty.textContent = 'Search failed.';
      empty.dataset.kind = 'error';
      empty.classList.remove('hidden');
    } finally {
      if (mySeq === _seq) loadEl.classList.add('hidden');
    }
  }

  paintScope();
  paintApplied();
  paintIdle();
  paintFade();
  loadFormats();                        // (the sheet's format keys; the format key's name)
  if (Object.keys(filters).length) runQuery();   // (filters kept from before search on their own)
}
