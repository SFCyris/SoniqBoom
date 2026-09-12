// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/galaxy.js — "Library Galaxy" view: browse the library by format.
 *
 * TWO co-equal presentations of the SAME data (``GET /api/library/formats`` →
 * ``[{format, count}]``), switchable with a Galaxy | List toggle in the view
 * toolbar (choice persisted per-device in ``sb_galaxy_view``):
 *
 *   • Galaxy — every format a drifting star-constellation sized by track count
 *     (a proportional-area chart).  The ``<canvas>`` is decorative
 *     (``aria-hidden``); the operable surface is the legend chips beneath it.
 *   • List — a sortable, searchable table of Format · Tracks · Share.  This is
 *     the accessible/semantic equivalent of the size-encoded galaxy and the
 *     better surface for known-item lookup across many formats.
 *
 * Both surfaces call the SAME ``onPickFormat`` filter handler, so cluster,
 * chip, and row can never drift.  Motion is a presentation layer: the animated
 * galaxy is suppressed (List becomes the forced default) whenever the master
 * "Enable visualizations" switch is off OR the OS requests reduced motion.
 */
import { registerViz, rand, vizGroupEnabled, prefersReducedMotion } from './engine.js';

const VIEW_KEY = 'sb_galaxy_view';   // 'galaxy' | 'list'
const SORT_KEY = 'sb_galaxy_sort';   // e.g. 'count:desc' | 'format:asc'
const FAMILY_KEY = 'sb_galaxy_family';   // 'all' | one of FAMILY_ORDER

// Coarse format families (the backend stamps each /api/library/formats entry
// with `family` via core.retro.coarse_family; the galaxy only presents them).
// Display order for the filter chips (families with zero members are omitted).
const FAMILY_ORDER = ['trackers', 'chiptune', 'lossless', 'lossy', 'other'];
const FAMILY_LABEL = { trackers: 'Trackers', chiptune: 'Chiptune', lossless: 'Lossless', lossy: 'Lossy', other: 'Other' };
const FAMILY_HUE   = { trackers: 150, chiptune: 280, lossless: 200, lossy: 35, other: 220 };

const HUE = {
  SID: 280, PSID: 280,
  ProTracker: 150, 'FastTracker 2': 95, 'Impulse Tracker': 188, 'ScreamTracker 3': 70,
  MOD: 150, XM: 95, IT: 188, S3M: 70,
  FLAC: 200, ALAC: 190, WAV: 210, AIFF: 210,
  MP3: 35, 'Ogg Vorbis': 45, Opus: 50, AAC: 30,
  DSD: 320, DSF: 320, DFF: 320,
  MIDI: 120, NSF: 100, SPC: 110,
};
function hueFor(fmt) {
  const f = String(fmt || '');
  if (HUE[f] != null) return HUE[f];
  const u = f.toUpperCase();
  for (const k in HUE) if (u.includes(k.toUpperCase())) return HUE[k];
  // Position-independent fallback: hash the NAME so an unmapped format's colour
  // is identical on the galaxy (visible-subset index) and the list (global
  // index) and never shifts when a family filter re-indexes the subset.
  let h = 0;
  for (let j = 0; j < f.length; j++) h = (h * 31 + f.charCodeAt(j)) >>> 0;
  return h % 360;
}

const STAR_BUDGET = 4000;

export function mountGalaxy(host, { onPickFormat } = {}) {
  const pick = (fmt, count) => { try { onPickFormat && onPickFormat(fmt, count); } catch { /* isolation */ } };

  host.classList.add('galaxy-flex');   // flex column: toolbar + body (no magic offsets)

  // ── Toolbar ────────────────────────────────────────────────────────────────
  const toolbar = document.createElement('div');
  toolbar.className = 'galaxy-toolbar';
  const seg = document.createElement('div');
  seg.className = 'galaxy-mode-toggle';
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', 'Galaxy display');
  const btnGalaxy = _modeBtn('Galaxy', 'galaxy');
  const btnList   = _modeBtn('List', 'list');
  seg.append(btnGalaxy, btnList);
  const search = document.createElement('input');
  search.type = 'search';
  search.className = 'galaxy-search';
  search.placeholder = 'Filter formats…';
  search.setAttribute('aria-label', 'Filter formats');
  search.hidden = true;
  const reason = document.createElement('span');   // visible + programmatic reason when Galaxy is unavailable
  reason.className = 'galaxy-reason';
  reason.id = 'galaxy-reason-' + Math.random().toString(36).slice(2, 8);
  reason.hidden = true;
  const count = document.createElement('span');
  count.className = 'galaxy-count';
  count.setAttribute('role', 'status');
  count.setAttribute('aria-live', 'polite');
  toolbar.append(seg, search, reason, count);
  btnGalaxy.setAttribute('aria-describedby', reason.id);
  host.appendChild(toolbar);

  // ── Family filter (second toolbar row; filters BOTH surfaces) ───────────────
  const famBar = document.createElement('div');
  famBar.className = 'galaxy-family-bar';
  famBar.setAttribute('role', 'group');
  famBar.setAttribute('aria-label', 'Filter by format family');
  famBar.hidden = true;
  host.appendChild(famBar);

  function _modeBtn(label, mode) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'galaxy-mode-btn';
    b.dataset.mode = mode;
    b.textContent = label;
    b.addEventListener('click', () => {
      if (mode === 'galaxy' && _forced()) return;   // suppressed → List is forced
      try { localStorage.setItem(VIEW_KEY, mode); } catch { /* ignore */ }
      applyMode();
    });
    return b;
  }

  // ── Body (canvas + legend + list share this flex:1 area) ───────────────────
  const body = document.createElement('div');
  body.className = 'galaxy-body';
  host.appendChild(body);

  const cv = document.createElement('canvas');
  cv.setAttribute('aria-hidden', 'true');   // decorative — the data lives in the list/chips
  body.appendChild(cv);
  const ctx = cv.getContext('2d');
  function size() {
    if (_mode && _mode !== 'galaxy') return;   // don't reallocate a display:none canvas in List mode
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.max(1, body.clientWidth * dpr);
    cv.height = Math.max(1, body.clientHeight * dpr);
  }
  const ro = new ResizeObserver(() => size()); ro.observe(body);

  const sr = document.createElement('div');
  sr.className = 'galaxy-legend';
  body.appendChild(sr);

  const listWrap = document.createElement('div');
  listWrap.className = 'galaxy-list';
  body.appendChild(listWrap);

  let _formats = [];        // [{format, count}]  (normalized)
  let _loaded = false;      // first fetch has resolved (success or error)
  let _loadError = false;
  let _laidOut = false;     // galaxy stars/chips built at least once
  let clusters = [];
  let stars = [];
  let t = 0;
  const { key: _sk0, asc: _sa0 } = _loadSort();
  let _sortKey = _sk0;      // 'count' | 'format'
  let _sortAsc = _sa0;
  let _filter  = '';
  let _family  = _loadFamily();   // 'all' | family key

  // persistent list DOM (thead built once → sorting never detaches the focused header)
  let _table = null, _tbody = null;
  const _headBtns = {};     // key -> button
  const _headTh = {};       // key -> th

  let _searchTimer = null;
  search.addEventListener('input', () => {
    _filter = search.value;
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(renderRows, 120);   // debounce the rebuild
  });

  // ── Family filter ───────────────────────────────────────────────────────────
  function _visibleFormats() {
    return _family === 'all' ? _formats : _formats.filter(f => f.family === _family);
  }
  function _saveFamily() { try { localStorage.setItem(FAMILY_KEY, _family); } catch { /* ignore */ } }

  function _famChip(key, label, tracks) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'galaxy-fam-chip';
    b.dataset.family = key;
    if (key !== 'all') b.style.setProperty('--gx-hue', FAMILY_HUE[key]);
    const l = document.createElement('span'); l.className = 'galaxy-fam-label'; l.textContent = label;
    const n = document.createElement('span'); n.className = 'galaxy-fam-n'; n.textContent = tracks.toLocaleString();
    b.append(l, n);
    b.setAttribute('aria-label', `${label}, ${tracks.toLocaleString()} tracks`);
    b.addEventListener('click', () => _selectFamily(key));
    return b;
  }

  function _buildFamilyBar() {
    famBar.replaceChildren();
    if (!_formats.length) { famBar.hidden = true; return; }
    const tot = {};
    let all = 0;
    for (const f of _formats) { tot[f.family] = (tot[f.family] || 0) + f.count; all += f.count; }
    // If only one real family is present, a family filter adds nothing — hide it.
    // Chips are ordered biggest-first (like the list's default Tracks-desc sort),
    // with FAMILY_ORDER as the stable tiebreaker for equal totals.
    const present = FAMILY_ORDER.filter(k => tot[k]).sort((a, b) => tot[b] - tot[a]);
    if (present.length < 2) { famBar.hidden = true; if (_family !== 'all') { _family = 'all'; _saveFamily(); } return; }
    famBar.appendChild(_famChip('all', 'All', all));
    for (const key of present) famBar.appendChild(_famChip(key, FAMILY_LABEL[key], tot[key]));
    // A persisted family that no longer has members falls back to All.
    if (_family !== 'all' && !tot[_family]) { _family = 'all'; _saveFamily(); }
    famBar.hidden = false;
    _paintFamilyState();
  }

  function _paintFamilyState() {
    for (const b of famBar.querySelectorAll('.galaxy-fam-chip')) {
      const on = b.dataset.family === _family;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function _selectFamily(key) {
    if (_family === key) return;
    _family = key;
    _saveFamily();
    _laidOut = false;        // galaxy must relayout with the new subset
    _paintFamilyState();
    applyMode();             // relayout galaxy / re-render list for the new family
  }

  // ── Mode ────────────────────────────────────────────────────────────────────
  function _forced() { return !vizGroupEnabled('library') || prefersReducedMotion(); }
  function resolveMode() {
    if (_forced()) return 'list';
    let saved = 'galaxy';
    try { saved = localStorage.getItem(VIEW_KEY) || 'galaxy'; } catch { /* ignore */ }
    return saved === 'list' ? 'list' : 'galaxy';
  }

  let _ctl = null;
  function _ensureLaidOut() {
    const vis = _visibleFormats();
    if (!_laidOut && vis.length) { layout(vis); _laidOut = true; }
  }
  function _startAnim() {
    if (_ctl) return;
    _ctl = registerViz({ host, group: 'library', fps: 60, hideWhenOff: false, draw, freeze });
  }
  function _stopAnim() { if (_ctl) { _ctl.unregister(); _ctl = null; } }

  let _mode = null;
  function applyMode() {
    const mode = resolveMode();
    _mode = mode;
    host.classList.toggle('mode-galaxy', mode === 'galaxy');
    host.classList.toggle('mode-list',   mode === 'list');
    const forced = _forced();
    for (const b of [btnGalaxy, btnList]) {
      const on = b.dataset.mode === mode;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    // Galaxy unavailable → keep it focusable (aria-disabled), no-op the click, and
    // show the recoverable reason both visibly and programmatically.
    btnGalaxy.setAttribute('aria-disabled', forced ? 'true' : 'false');
    btnGalaxy.classList.toggle('is-disabled', forced);
    if (forced) {
      reason.textContent = prefersReducedMotion()
        ? 'Animation off (system reduce-motion)'
        : 'Animation off — enable it in Settings → System → Visualizations.';
      reason.hidden = false;
    } else {
      reason.textContent = '';
      reason.hidden = true;
    }
    // Only offer search when there's data to search (not during Loading…/empty/error).
    search.hidden = mode !== 'list' || !_formats.length;
    if (mode === 'galaxy') {
      size();
      _ensureLaidOut();     // build stars/chips lazily once data exists (D7: never in List mode)
      _startAnim();
      count.textContent = '';               // list status doesn't belong on the galaxy
    } else {
      _stopAnim();
      renderList();
    }
  }

  // ── Galaxy layout ───────────────────────────────────────────────────────────
  function layout(formats) {
    const total = formats.reduce((a, f) => a + f.count, 0) || 1;
    const N = formats.length;
    clusters = formats.map((f, i) => {
      const ang = i * 2.399963;
      const rad = 0.08 + 0.40 * Math.sqrt(i / Math.max(1, N - 1));
      return {
        fmt: f.format, count: f.count, hue: hueFor(f.format),
        cx: 0.5 + Math.cos(ang) * rad,
        cy: 0.30 + Math.sin(ang) * rad * 0.44,
        spread: 0.04 + 0.10 * Math.sqrt(f.count / total),
        n: Math.max(6, Math.round(STAR_BUDGET * (f.count / total))),
      };
    });
    stars = [];
    for (const c of clusters) {
      for (let i = 0; i < c.n; i++) {
        const a = Math.random() * 7, r = Math.pow(Math.random(), 0.55) * c.spread;
        stars.push({
          c, x: c.cx + Math.cos(a) * r, y: c.cy + Math.sin(a) * r * 0.85,
          tw: Math.random() * 7, sp: rand(0.4, 1.3),
        });
      }
    }
    if (stars.length > STAR_BUDGET + 600) stars.length = STAR_BUDGET + 600;
    sr.innerHTML = '';
    formats.forEach((f, i) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'galaxy-chip';
      chip.style.setProperty('--gx-hue', hueFor(f.format));
      chip.textContent = `${f.format} · ${f.count.toLocaleString()}`;
      chip.addEventListener('click', () => pick(f.format, f.count));
      sr.appendChild(chip);
    });
  }

  // ── List ────────────────────────────────────────────────────────────────────
  function _buildTable() {
    listWrap.innerHTML = '';
    _table = document.createElement('table');
    _table.className = 'galaxy-table';
    const cap = document.createElement('caption');
    cap.className = 'sr-only';
    cap.textContent = 'Library formats by track count — select a format to filter the library';
    _table.appendChild(cap);
    const thead = document.createElement('thead');
    const htr = document.createElement('tr');
    htr.append(_headCell('Format', 'format'), _headCell('Tracks', 'count'));
    const shareTh = document.createElement('th');
    shareTh.scope = 'col'; shareTh.textContent = 'Share';
    htr.appendChild(shareTh);
    thead.appendChild(htr);
    _table.appendChild(thead);
    _tbody = document.createElement('tbody');
    _table.appendChild(_tbody);
    listWrap.appendChild(_table);
    _paintSortState();
  }

  function _headCell(label, key) {
    const th = document.createElement('th');
    th.scope = 'col';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'galaxy-th-btn';
    btn.dataset.sort = key;
    btn.innerHTML = `${label}<span class="galaxy-sort-caret" aria-hidden="true"></span>`;
    btn.addEventListener('click', () => {
      if (_sortKey === key) _sortAsc = !_sortAsc;
      else { _sortKey = key; _sortAsc = (key === 'format'); }   // format A→Z, count hi→lo
      _saveSort();
      _paintSortState();     // in place — thead persists, so focus stays on this button
      renderRows();
    });
    th.appendChild(btn);
    _headBtns[key] = btn; _headTh[key] = th;
    return th;
  }

  function _paintSortState() {
    for (const key of ['format', 'count']) {
      const active = _sortKey === key;
      _headTh[key].setAttribute('aria-sort', active ? (_sortAsc ? 'ascending' : 'descending') : 'none');
      _headBtns[key].querySelector('.galaxy-sort-caret').textContent = active ? (_sortAsc ? '▲' : '▼') : '';
    }
  }

  function _saveSort() {
    try { localStorage.setItem(SORT_KEY, `${_sortKey}:${_sortAsc ? 'asc' : 'desc'}`); } catch { /* ignore */ }
  }

  function renderList() {
    if (_loadError) { _renderError(); return; }
    if (!_formats.length) {
      listWrap.innerHTML = '';
      _table = _tbody = null;
      const d = document.createElement('div');
      d.className = 'galaxy-empty';
      // Distinguish "still loading" from a genuinely empty index so the first
      // frame before the fetch resolves doesn't accuse the user of an empty library.
      d.textContent = _loaded
        ? 'No formats indexed yet — run a library scan to populate.'
        : 'Loading formats…';
      listWrap.appendChild(d);
      count.textContent = _loaded ? 'No formats yet' : '';
      return;
    }
    if (!_table || !listWrap.contains(_table)) _buildTable();
    renderRows();
  }

  function renderRows() {
    if (!_tbody) return;
    // Share % is relative to the ACTIVE family's total (the whole library when
    // 'All' is selected), so the shares sum to ~100% within the current view and
    // share the count line's denominator — better within-family comparison.
    const base = _visibleFormats();
    const baseTotal = base.reduce((a, f) => a + f.count, 0);
    const q = _filter.trim().toLowerCase();
    let rows = q ? base.filter(f => f.format.toLowerCase().includes(q)) : base.slice();
    rows.sort((a, b) => {
      let r;
      if (_sortKey === 'format') r = a.format.localeCompare(b.format, undefined, { sensitivity: 'base' });
      else r = a.count - b.count;
      return _sortAsc ? r : -r;
    });
    const frag = document.createDocumentFragment();
    for (const f of rows) {
      const hue = hueFor(f.format);
      const tr = document.createElement('tr');
      // Format is the ROW HEADER so Tracks/Share announce with their format.
      const thF = document.createElement('th');
      thF.scope = 'row';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'galaxy-row-btn';
      btn.style.setProperty('--gx-hue', hue);
      btn.setAttribute('aria-label', `Filter to ${f.format}, ${f.count.toLocaleString()} tracks`);
      btn.innerHTML = `<span class="galaxy-dot" aria-hidden="true"></span><span class="galaxy-fmt"></span>`;
      btn.querySelector('.galaxy-fmt').textContent = f.format;   // textContent → XSS-safe
      btn.addEventListener('click', () => pick(f.format, f.count));
      thF.appendChild(btn);
      const tdC = document.createElement('td');
      tdC.className = 'galaxy-num';
      tdC.textContent = f.count.toLocaleString();
      const pct = (f.count / (baseTotal || 1)) * 100;
      const tdS = document.createElement('td');
      tdS.className = 'galaxy-share';
      const bar = document.createElement('span');
      bar.className = 'galaxy-bar';
      bar.style.setProperty('--w', Math.max(pct, 0.5).toFixed(2) + '%');   // bar == the % (one denominator)
      bar.style.setProperty('--gx-hue', hue);
      const pctEl = document.createElement('span');
      pctEl.className = 'galaxy-pct';
      pctEl.textContent = (pct < 0.1 ? '<0.1' : pct.toFixed(1)) + '%';
      tdS.append(bar, pctEl);
      tr.append(thF, tdC, tdS);
      frag.appendChild(tr);
    }
    _tbody.replaceChildren(frag);
    // no-match hint
    let hint = listWrap.querySelector('.galaxy-nomatch');
    if (!rows.length) {
      if (!hint) { hint = document.createElement('div'); hint.className = 'galaxy-empty galaxy-nomatch'; listWrap.appendChild(hint); }
      hint.textContent = _filter.trim()
        ? `No formats match “${_filter}”.`
        : `No ${FAMILY_LABEL[_family] || ''} formats.`;
    } else if (hint) hint.remove();
    const famSuffix = _family === 'all' ? '' : ` · ${FAMILY_LABEL[_family]}`;
    count.textContent = q
      ? `${rows.length} of ${base.length} formats${famSuffix}`
      : `${base.length} formats, ${baseTotal.toLocaleString()} tracks${famSuffix}`;
  }

  function _renderError() {
    listWrap.innerHTML = '';
    _table = _tbody = null;
    const err = document.createElement('div');
    err.className = 'galaxy-empty';
    err.setAttribute('role', 'alert');
    const retry = document.createElement('button');
    retry.type = 'button'; retry.className = 'galaxy-retry'; retry.textContent = 'Retry';
    retry.addEventListener('click', load);
    err.append('Couldn’t load formats. ', retry);
    listWrap.appendChild(err);
    count.textContent = 'Couldn’t load formats';
  }

  // ── Data ────────────────────────────────────────────────────────────────────
  function _normalize(raw) {
    if (!Array.isArray(raw)) return [];
    const out = [];
    for (const f of raw) {
      if (!f || typeof f !== 'object') continue;
      const fmt = String(f.format == null ? '' : f.format);
      const c = Number(f.count);
      if (!fmt || !Number.isFinite(c) || c <= 0) continue;
      // FAMILY_ORDER is an array → .includes is prototype-safe (a family of
      // 'toString'/'constructor' must NOT slip through an object-key truthiness check).
      const fam = FAMILY_ORDER.includes(f.family) ? f.family : 'other';
      out.push({ format: fmt, count: c, family: fam });
    }
    return out;
  }

  function _legendNotice(msg, withRetry) {
    sr.innerHTML = '';
    const span = document.createElement('span');
    span.className = 'galaxy-empty';
    span.textContent = msg;
    sr.appendChild(span);
    if (withRetry) {
      const retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'galaxy-retry'; retry.textContent = 'Retry';
      retry.addEventListener('click', load);
      sr.appendChild(retry);
    }
  }

  async function load() {
    _loadError = false;
    listWrap.setAttribute('aria-busy', 'true');
    try {
      const r = await fetch('/api/library/formats', { credentials: 'same-origin' });
      if (!r.ok) throw new Error('formats ' + r.status);
      _formats = _normalize(await r.json());
      _laidOut = false;
      _buildFamilyBar();   // family chips + subtotals; validates the persisted family
      if (!_formats.length) { clusters = []; stars = []; _legendNotice('No formats indexed yet — run a library scan to populate the galaxy.', false); }
    } catch {
      _loadError = true;
      _formats = []; clusters = []; stars = []; _laidOut = false;
      _buildFamilyBar();   // hides the bar when there's no data
      _legendNotice('Couldn’t load formats.', true);
    } finally {
      _loaded = true;
      listWrap.removeAttribute('aria-busy');
    }
    applyMode();
  }
  // Pick a surface synchronously so there's no dual-visible flash (canvas + list)
  // before the first fetch resolves; load() re-runs applyMode with real data.
  applyMode();
  load();

  // ── Live re-resolve on master-viz change / reduce-motion flip ──────────────
  const onSettings = () => applyMode();
  window.addEventListener('sb:viz-settings', onSettings);
  const rmMql = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const onRM = () => applyMode();
  if (rmMql && rmMql.addEventListener) rmMql.addEventListener('change', onRM);
  else if (rmMql && rmMql.addListener) rmMql.addListener(onRM);

  // ── Canvas draw / freeze ────────────────────────────────────────────────────
  function draw(dt) {
    const dpr = Math.min(2, window.devicePixelRatio || 1), W = cv.width, H = cv.height;
    t += dt / 1000;
    ctx.fillStyle = 'rgba(7,10,15,0.30)';
    ctx.fillRect(0, 0, W, H);
    for (const s of stars) {
      const tw = 0.45 + 0.55 * Math.sin(t * s.sp + s.tw);
      const x = s.x * W + Math.sin(t * 0.16 + s.tw) * 3 * dpr;
      const y = s.y * H + Math.cos(t * 0.14 + s.tw) * 3 * dpr;
      ctx.beginPath();
      ctx.arc(x, y, (0.7 + tw * 1.3) * dpr, 0, 7);
      ctx.fillStyle = `hsla(${s.c.hue}, 82%, ${52 + tw * 26}%, ${0.35 + tw * 0.55})`;
      ctx.fill();
    }
    ctx.textAlign = 'center';
    for (const c of clusters) {
      ctx.font = `${10.5 * dpr}px sans-serif`;
      ctx.fillStyle = `hsla(${c.hue}, 70%, 72%, 0.85)`;
      ctx.fillText(c.fmt, c.cx * W, (c.cy - c.spread) * H - 6 * dpr);
    }
  }
  function freeze() {
    const dpr = Math.min(2, window.devicePixelRatio || 1), W = cv.width, H = cv.height;
    ctx.fillStyle = '#070a0f'; ctx.fillRect(0, 0, W, H);
    for (const s of stars) {
      ctx.beginPath(); ctx.arc(s.x * W, s.y * H, 1.1 * dpr, 0, 7);
      ctx.fillStyle = `hsla(${s.c.hue}, 80%, 60%, 0.7)`; ctx.fill();
    }
  }

  return {
    reload: load,
    unregister() {
      _stopAnim();
      if (_searchTimer) clearTimeout(_searchTimer);
      ro.disconnect();
      window.removeEventListener('sb:viz-settings', onSettings);
      if (rmMql && rmMql.removeEventListener) rmMql.removeEventListener('change', onRM);
      else if (rmMql && rmMql.removeListener) rmMql.removeListener(onRM);
    },
  };
}

// ── sort persistence ──────────────────────────────────────────────────────────
function _loadSort() {
  try {
    const raw = localStorage.getItem(SORT_KEY) || '';
    const [key, dir] = raw.split(':');
    if (key === 'format' || key === 'count') return { key, asc: dir === 'asc' };
  } catch { /* ignore */ }
  return { key: 'count', asc: false };
}

function _loadFamily() {
  try {
    const v = localStorage.getItem(FAMILY_KEY);
    if (v === 'all' || FAMILY_ORDER.includes(v)) return v;
  } catch { /* ignore */ }
  return 'all';
}
