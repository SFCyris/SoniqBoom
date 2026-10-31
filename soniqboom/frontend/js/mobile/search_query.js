// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * search_query.js — the phone Search view's query text (pure: no DOM, no
 * imports; tests/js/m_search_query.test.mjs runs it as is).
 *
 * The view sends ONE ``q`` to ``GET /api/search/page`` (and the queue's
 * shuffle), in the search box's own operator syntax (api/search.py
 * ``_parse_advanced_query``), built from three things:
 *
 *   * the text in the field, never rewritten on screen;
 *   * a scope (ALL / TITLE / ARTIST / ALBUM / GENRE / GAME): the field's free
 *     text — what is left after the operator terms typed by hand
 *     (``artist:x``, ``-format:mp3``) and the free-text exclusions (``-live``,
 *     ``-"live at"``) — becomes ``<op>:"<text>"``; a ``"`` in it can't be
 *     carried inside a quoted value, so it splits the text into pieces, one
 *     term each (the same operator twice must match both);
 *   * filters (year, format, added, rating, length, chip, channels, tunes),
 *     each ``<op>:"<value>"``.
 *
 * A scoped search is also sent ``rank_by=<operator>`` (``rankBy``): the server
 * then lists it Best match for the scoped words, judged on that field, instead
 * of newest first (core/search_rank.py ``SCOPES``).
 *
 * The built terms come first and are all quoted (a quoted value can't be
 * joined to the next word by the server's range tightening, ``year:1990 - 5``),
 * then the field's own operator terms and exclusions as typed, in order — an
 * unclosed quote can only be the last of them, so it never swallows a term
 * built here.
 */

// The search box's operators — js/search.js ``_SUPPORTED_OPS`` (test-guarded:
// tests/js/m_search_query.test.mjs compares the two).
export const SUPPORTED_OPS = new Set([
  'artist', 'album_artist', 'album', 'game',
  'year', 'track', 'disc', 'disk', 'genre', 'format', 'type',
  'length', 'added', 'rating', 'plays', 'played',
  'folder', 'path', 'group', 'party', 'status', 'composer', 'title',
  'lossless', 'bitdepth', 'samplerate', 'bitrate', 'source',
  'bpm', 'tunes', 'chip', 'dup', 'channels', 'has',
]);

// The scope keys, in order: [key, label, operator].
export const SCOPES = [
  ['all', 'All', null],
  ['title', 'Title', 'title'],
  ['artist', 'Artist', 'artist'],
  ['album', 'Album', 'album'],
  ['genre', 'Genre', 'genre'],
  ['game', 'Game', 'game'],
];
const _SCOPE_OP = new Map(SCOPES.map(([k, , op]) => [k, op]));
export function scopeOf(key) { return _SCOPE_OP.has(key) ? key : 'all'; }
export function scopeLabel(key) { return (SCOPES.find(([k]) => k === key) || SCOPES[0])[1]; }

// ── Splitting the field's text the way the server does ──────────────────────
// Python's ``\w`` (letters, digits, marks, underscore — Unicode).
const _W = /[\p{L}\p{M}\p{N}_]/u;
const _isW = (ch) => !!ch && _W.test(ch);
const _isSpace = (ch) => ch === undefined || /\s/.test(ch);

// The range operators whose values the server joins across spaces
// (api/search.py ``_RANGE_FIELDS`` / ``_tighten_ranges``).
const _RANGE = 'year|track|disc|disk|length|rating|plays|added|played|bitdepth|samplerate|bitrate|bpm|tunes|channels';
const _RANGE_OP_SPACE = new RegExp(`(${_RANGE}):\\s*([<>]=?|-)?\\s*(?=\\d)`, 'giu');
// (a comparator right after the colon, then spaces: ``year:>= 2000``)
const _RANGE_CMP_SPACE = new RegExp(`(${_RANGE}):([<>]=?|-)\\s+(?=\\d)`, 'giu');
const _RANGE_RANGE_SPACE = new RegExp(
  `((?:${_RANGE}):[\\d:.\\-]*\\d(?:khz|kbps|hz|[smhk])?)(?:\\s+-\\s*|\\s*-\\s+)(?=\\d)`, 'giu');
const _UNIT_SPACE = /((?:samplerate|bitrate):\S*?\d)\s+(khz|kbps|hz|k)\b/giu;

// ``(?<!\w)(?<!\w-)`` of the server's patterns (no lookbehind: Safari < 16.4).
function _startsToken(s, at) {
  return !_isW(s[at - 1]) && !(s[at - 1] === '-' && _isW(s[at - 2]));
}
function _sub(s, re, fn) {
  return s.replace(re, (...a) => {
    const at = a[a.length - 2];
    return _startsToken(s, at) ? fn(...a) : a[0];
  });
}

/** ``q`` with the spaces inside numeric / time values removed, outside quoted
 * values only — api/search.py ``_tighten_ranges``.  ``opSpace`` false: a
 * space right after the colon is kept (``track: 2`` stays text — in a scoped
 * search "Back on Track: 2" is an album, not ``track:2``); a value or a
 * comparator that starts at the colon still joins (``year:1990 - 1999``,
 * ``year:>= 2000``, ``bitrate:128 kbps``). */
export function tightenRanges(q, opSpace = true) {
  const pieces = String(q).split(/("[^"]*")/);
  for (let i = 0; i < pieces.length; i += 2) {
    let p = _sub(pieces[i], opSpace ? _RANGE_OP_SPACE : _RANGE_CMP_SPACE, (m, f, op) => `${f}:${op || ''}`);
    p = _sub(p, _UNIT_SPACE, (m, a, u) => a + u);
    p = _sub(p, _RANGE_RANGE_SPACE, (m, a) => `${a}-`);
    pieces[i] = _sub(p, _UNIT_SPACE, (m, a, u) => a + u);
  }
  return pieces.join('');
}

// A value run: quoted runs and non-space non-quote characters
// (``_VALUE_RUN``), at ``i``; its end, or ``i`` when none.
function _valueRun(s, i) {
  let j = i;
  for (;;) {
    if (s[j] === '"') {
      const k = s.indexOf('"', j + 1);
      if (k === -1) break;
      j = k + 1;
    } else if (j < s.length && !/\s/.test(s[j])) {
      j++;
    } else break;
  }
  return j;
}

// An operator term (``_TERM_RE``) starting at ``i``: {start, end, neg, name}
// or null.
function _termAt(s, i) {
  let neg = false, at = i;
  if (s[i] === '-' && _isSpace(s[i - 1]) && _isW(s[i + 1])) { neg = true; at = i + 1; }
  let j = at;
  while (_isW(s[j])) j++;
  if (j === at || s[j] !== ':') return neg ? _termAt(s, i + 1) : null;
  const name = s.slice(at, j);
  const v0 = j + 1;
  let end = _valueRun(s, v0);
  if (end > v0) {
    // after a trailing comma the next word is the next value (not ``-word``)
    for (;;) {
      if (s[end - 1] !== ',') break;
      let k = end;
      while (k < s.length && /\s/.test(s[k])) k++;
      if (k === end || k >= s.length) break;
      if (s[k] === '-' && /[^\W\d_]/u.test(s[k + 1] || '')) break;
      const e2 = _valueRun(s, k);
      if (e2 === k) break;
      end = e2;
    }
  } else {
    while (end < s.length && !/\s/.test(s[end])) end++;     // ``\S+``
    if (end === v0) return neg ? _termAt(s, i + 1) : null;
  }
  return { start: neg ? i : at, end, neg, name };
}

// The free text's words: whitespace-separated, a quoted run kept whole
// (``_WORD_RE``).
export function words(text) {
  return String(text).match(/(?:[^\s"]|"[^"]*"?)+/g) || [];
}

/** The word a free-text ``word`` excludes (``-live`` → ``live``), or null —
 * api/search.py ``_excluded_word``. */
export function excludedWord(word) {
  if (word.length < 2 || word[0] !== '-' || word[1] === '-') return null;
  const rest = word.slice(1).replace(/"/g, ' ').trim();
  return /[\p{L}\p{N}]/u.test(rest) ? rest : null;
}

/** The field's text split as the server reads it (``_SCAN_RE``; but a space
 * after an operator's colon keeps it text — ``tightenRanges(…, false)``):
 * ``terms`` — the operator terms typed by hand and the free-text exclusions,
 * as typed, in order (``ops`` / ``excl``: each kind alone) — and ``free`` —
 * the words the scope applies to (one string). */
export function splitQuery(text) {
  const s = tightenRanges(String(text || ''), false);
  const spans = [];
  let i = 0;
  while (i < s.length) {
    const t = _termAt(s, i);
    if (t) {
      if (SUPPORTED_OPS.has(t.name.toLowerCase())) spans.push(t);
      i = Math.max(t.end, i + 1);
      continue;
    }
    // a quoted run of the free text (an operator inside it is phrase text)
    if (_isSpace(s[i - 1]) && (s[i] === '"' || (s[i] === '-' && s[i + 1] === '"'))) {
      const q0 = s[i] === '"' ? i : i + 1;
      const k = s.indexOf('"', q0 + 1);
      i = k === -1 ? s.length : k + 1;
      continue;
    }
    i++;
  }
  // The free text: the rest, joined as the server joins it (no separator),
  // with each character's place in ``s`` (to keep the order of what is typed).
  const out = [];             // {at, text}: operator terms and exclusions
  let rest = '', at = 0;
  const where = [];
  const keep = (a, b) => { for (let k = a; k < b; k++) where.push(k); rest += s.slice(a, b); };
  for (const t of spans) {
    keep(at, t.start);
    out.push({ at: t.start, text: s.slice(t.start, t.end), op: true });
    at = t.end;
  }
  keep(at, s.length);
  const free = [];
  const wre = /(?:[^\s"]|"[^"]*"?)+/g;
  for (let m; (m = wre.exec(rest));) {
    if (excludedWord(m[0]) !== null) out.push({ at: where[m.index], text: m[0] });
    else free.push(m[0]);
  }
  out.sort((a, b) => a.at - b.at);
  return { terms: out.map((o) => o.text), ops: out.filter((o) => o.op).map((o) => o.text),
           excl: out.filter((o) => !o.op).map((o) => o.text), free: free.join(' ') };
}

/** ``op:"text"`` terms for a scope: the text cut at every ``"`` (a quoted
 * value can't hold one), each piece with its whitespace collapsed; empty
 * pieces dropped. */
export function scopedTerms(op, text) {
  const out = [];
  for (const piece of String(text || '').split('"')) {
    const v = piece.split(/\s+/).filter(Boolean).join(' ');
    if (v) out.push(`${op}:"${v}"`);
  }
  return out;
}

// ── Filters ────────────────────────────────────────────────────────────────
// Each filter field: its label and its preset values [value, key label,
// accessible label].  ``year`` is a range {from, to} instead.
export const FILTERS = {
  format: { label: 'Format', values: [] },          // (values: the library's — /api/library/formats)
  added: { label: 'Added', values: [
    ['<1d', '24 h', 'in the last 24 hours'], ['<7d', '7 days', 'in the last 7 days'],
    ['<30d', '30 days', 'in the last 30 days'], ['<1y', '1 year', 'in the last year'],
  ] },
  rating: { label: 'Rating', values: [
    ['>=1', '1★+', '1 star or more'], ['>=2', '2★+', '2 stars or more'], ['>=3', '3★+', '3 stars or more'],
    ['>=4', '4★+', '4 stars or more'], ['5', '5★', '5 stars'], ['0', 'Unrated', 'unrated'],
  ] },
  length: { label: 'Length', values: [
    ['<2:00', '< 2 min', 'under 2 minutes'], ['2:00-5:00', '2–5 min', '2 to 5 minutes'],
    ['5:00-10:00', '5–10 min', '5 to 10 minutes'], ['>10:00', '> 10 min', 'over 10 minutes'],
  ] },
  chip: { label: 'SID chip', values: [
    ['6581', '6581', '6581'], ['8580', '8580', '8580'], ['2sid', '2SID', 'two chips'], ['3sid', '3SID', 'three chips'],
  ] },
  channels: { label: 'Channels', values: [
    ['4', '4', '4 channels'], ['8', '8', '8 channels'], ['>8', '> 8', 'more than 8 channels'],
  ] },
  tunes: { label: 'Tunes', values: [['>1', 'Multi-tune', 'more than one tune']] },
};
// Format keys every library gets: the families (search_values.FAMILIES).
export const FORMAT_FAMILIES = [
  ['lossless', 'Lossless'], ['lossy', 'Lossy'], ['tracker', 'Tracker'], ['chiptune', 'Chiptune'],
];
// The order of the filters (query, keys under the field, the sheet).
export const FILTER_ORDER = ['year', 'format', 'added', 'rating', 'length', 'chip', 'channels', 'tunes'];

const _YEAR_RE = /^\d{1,4}$/;
const _cleanValue = (v) => String(v).replace(/["\s]+/g, ' ').trim();

/** ``raw`` (stored / chosen) as a filter set with only valid entries:
 * ``{year: {from, to}, format: 'FLAC', added: '<7d', …}`` (absent = off). */
export function normalizeFilters(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  const y = raw.year;
  if (y && typeof y === 'object') {
    let from = _YEAR_RE.test(String(y.from ?? '')) ? String(Number(y.from)) : '';
    let to = _YEAR_RE.test(String(y.to ?? '')) ? String(Number(y.to)) : '';
    if (from && to && Number(from) > Number(to)) [from, to] = [to, from];
    if (from || to) out.year = { from, to };
  }
  if (typeof raw.format === 'string') {
    const f = _cleanValue(raw.format).slice(0, 64);
    if (f && /[\p{L}\p{N}]/u.test(f)) out.format = f;
  }
  for (const k of ['added', 'rating', 'length', 'chip', 'channels', 'tunes']) {
    const v = raw[k];
    if (typeof v === 'string' && FILTERS[k].values.some(([val]) => val === v)) out[k] = v;
  }
  return out;
}

function _yearValue({ from, to }) {
  if (from && to) return from === to ? from : `${from}-${to}`;
  return from ? `>=${from}` : `<=${to}`;
}

/** The filter terms of ``filters`` (normalized), in FILTER_ORDER. */
export function filterTerms(filters) {
  const f = normalizeFilters(filters);
  const out = [];
  for (const k of FILTER_ORDER) {
    if (!(k in f)) continue;
    out.push(`${k}:"${k === 'year' ? _yearValue(f.year) : f[k]}"`);
  }
  return out;
}

/** The keys under the field: [{field, label, aria}] — one per filter. */
export function filterChips(filters, formatLabel = (v) => v) {
  const f = normalizeFilters(filters);
  const out = [];
  for (const k of FILTER_ORDER) {
    if (!(k in f)) continue;
    let text, spoken;
    if (k === 'year') {
      const { from, to } = f.year;
      text = from && to ? (from === to ? from : `${from}–${to}`) : from ? `from ${from}` : `to ${to}`;
      spoken = from && to ? (from === to ? from : `${from} to ${to}`) : text;
      out.push({ field: k, label: `Year ${text}`, aria: `Remove filter: year ${spoken}` });
      continue;
    }
    if (k === 'format') {
      const fam = FORMAT_FAMILIES.find(([v]) => v === f.format.toLowerCase());
      text = fam ? fam[1] : formatLabel(f.format);
      out.push({ field: k, label: `Format ${text}`, aria: `Remove filter: format ${text}` });
      continue;
    }
    const def = FILTERS[k];
    const [, shown, said] = def.values.find(([v]) => v === f[k]);
    const name = k === 'tunes' ? '' : `${def.label} `;
    out.push({ field: k, label: `${name}${shown}`, aria: `Remove filter: ${def.label.toLowerCase()} ${said}` });
  }
  return out;
}

/** The operator a search is ranked by (``GET /api/search/page?rank_by=``):
 * the scope's, when the scope applies to some of the text (``buildQuery``
 * made an ``<op>:"…"`` term of it); else null (ALL, filters only, only typed
 * operator terms). */
export function rankBy({ text = '', scope = 'all' } = {}) {
  const op = _SCOPE_OP.get(scopeOf(scope));
  if (!op) return null;
  return scopedTerms(op, splitQuery(String(text || '').trim()).free).length ? op : null;
}

/** The ``q`` of a search: ``text`` (the field, as typed), ``scope`` (a SCOPES
 * key) and ``filters``.  '' = nothing to search. */
export function buildQuery({ text = '', scope = 'all', filters = {} } = {}) {
  const built = filterTerms(filters);
  const op = _SCOPE_OP.get(scopeOf(scope));
  const typed = String(text || '').trim();
  if (!op) return [...built, typed].filter(Boolean).join(' ');
  const { terms, free } = splitQuery(typed);
  return [...built, ...scopedTerms(op, free), ...terms].join(' ');
}
