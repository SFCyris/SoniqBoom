// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * health.js — playback problems in the web UI (GitHub #36 / #37).
 *
 * A track can carry one playback problem the listener should see:
 *
 *  - reported by a listener with edit rights (``health``): **broken** (doesn't
 *    play right — passed by when a list plays through; a click still plays
 *    it) or **glitched** (plays, with wrong sounds — only shown);
 *  - detected by the server (``defect``): **corrupt** (no engine can decode
 *    it, or an archive that can't be opened — passed by like broken),
 *    **empty** (a package of an online collection that holds no tune
 *    SoniqBoom plays: it has left the library — a copy still in a queue or
 *    Track Info shows why) or **partial** (plays with substituted content —
 *    only shown).
 *
 * One badge per row, the most serious first (broken > corrupt > empty >
 * glitched > partial); its colour says what happens (red: passed by, amber: plays with
 * problems), its word and screen-reader text say what and why — never colour
 * alone.  Built the same way as the "offline" badge (offline.js) and shown
 * wherever that one is: library rows, search results, playlists, the queue,
 * Track Info and the phone's rows.
 *
 * Also here: reporting / clearing a problem (``mark`` — with Undo), the
 * one-time notice the first time auto-play passes something by, and — when a
 * track marked broken is clicked and then plays — the offer to clear the mark.
 *
 * Its own module (not utils.js / player.js): a page still on an older service
 * worker's cached copy of those must not fail to link a new named import.
 */
import { Player } from './player.js';
import { Auth } from './auth.js';

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

// A defect found when an archive was opened (core/online_catalog.py), not at scan.
const OPENED_RE = /^The archive is damaged/;

// key → [glyph, word, skipped when playing through?]
const KINDS = {
  broken:   ['⊘', 'broken',   true],
  corrupt:  ['⚠︎', 'corrupt', true],   // (text presentation, not the emoji)
  empty:    ['∅', 'no tunes', true],
  glitched: ['≈', 'glitched', false],
  partial:  ['◐', 'partial',  false],
};

function _when(at) {
  const n = Number(at);
  if (!(n > 0)) return '';
  try { return new Date(n * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }); }
  catch (_) { return ''; }
}

/** Who reported it, when, with which renderer: "Reported by Alice · 3 Oct 2026 · openmpt123 0.8.9". */
export function reportedLine(t) {
  if (!t || !t.health) return '';
  const parts = [t.health_by ? `Reported by ${t.health_by}` : 'Reported'];
  const d = _when(t.health_at);
  if (d) parts.push(d);
  if (t.health_renderer) parts.push(t.health_renderer);
  return parts.join(' · ');
}

/**
 * The row's playback problem, or null: ``{ key, glyph, word, skipped, title,
 * sr }`` — ``title`` the tooltip, ``sr`` the screen-reader text.
 */
export function healthOf(t) {
  if (!t || typeof t !== 'object') return null;
  let key = '';
  if (t.health === 'broken') key = 'broken';
  else if (t.defect === 'corrupt') key = 'corrupt';
  else if (t.defect === 'empty') key = 'empty';
  else if (t.health === 'glitched') key = 'glitched';
  else if (t.defect === 'partial') key = 'partial';
  if (!key) return null;
  const [glyph, word, skipped] = KINDS[key];
  // ``what``: what it means for playback (after the word, which the badge shows).
  // (a detail loses its full stop: the badge's text goes on after it)
  const detail = String(t.defect_detail || '').trim().replace(/[.\s]+$/, '');
  let what;
  if (key === 'broken') what = 'passed by when a list plays through; pick it to play anyway';
  else if (key === 'glitched') what = 'plays, with wrong sounds';
  else if (key === 'corrupt') what = (detail || 'no engine can decode it')
    + ' — passed by when a list plays through';
  else if (key === 'empty') what = (detail || 'it holds no tune SoniqBoom plays')
    + ' — passed by when a list plays through';
  else what = detail || 'plays with substituted content';
  const by = (key === 'broken' || key === 'glitched') ? reportedLine(t)
    : (key === 'empty' || OPENED_RE.test(String(t.defect_detail || ''))) ? 'Found when it was opened'
    : 'Detected when scanned';
  const note = (key === 'broken' || key === 'glitched') && t.health_note ? ` — “${t.health_note}”` : '';
  const title = `${word[0].toUpperCase()}${word.slice(1)}: ${what}. ${by}${note}`;
  return { key, glyph, word, skipped, title, sr: `: ${what}. ${by}${note}` };
}

/** Does ``t`` play at all?  False for a track no engine can decode or an
 *  archive that can't be opened (``defect`` corrupt) and for a package with
 *  no tune in it (``empty``): no "how it plays" report, signal path or
 *  audio facts apply to it. */
export function isUnplayable(t) {
  return !!t && typeof t === 'object' && (t.defect === 'corrupt' || t.defect === 'empty');
}

/** The badge element for ``t`` (a fresh one per call), or null. */
export function healthBadge(t) {
  const h = healthOf(t);
  if (!h) return null;
  const b = document.createElement('span');
  b.className = `track-health-badge track-health-${h.key}`;
  b.title = h.title;
  const icon = document.createElement('span');
  icon.className = 'track-health-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = h.glyph;
  const txt = document.createElement('span');
  txt.textContent = h.word;
  const sr = document.createElement('span');
  sr.className = 'sr-only';
  sr.textContent = h.sr;
  b.append(icon, txt, sr);
  return b;
}

/** The same badge as HTML (rows built from a template string); '' when none. */
export function healthBadgeHTML(t) {
  const h = healthOf(t);
  if (!h) return '';
  return `<span class="track-health-badge track-health-${h.key}" title="${esc(h.title)}">`
    + `<span class="track-health-icon" aria-hidden="true">${h.glyph}</span><span>${h.word}</span>`
    + `<span class="sr-only">${esc(h.sr)}</span></span>`;
}

// What the queue panel says beside a row the queue will pass by (an offline
// row says it with its own badge; a broken / corrupt one's badge says why).
const SKIP_WORDS = { short: 'short · skipped' };
/** ``'short · skipped'`` for a queue row auto-play passes by as a short tune,
 *  else '' (a broken / corrupt row's badge says it; it is dimmed all the same —
 *  ``skipReason``). */
export function skipWord(t) { return SKIP_WORDS[skipReasonOf(t)] || ''; }
/** Why auto-play passes ``t`` by ('offline' | 'broken' | 'corrupt' | 'empty' | 'short'), else ''. */
export function skipReasonOf(t) {
  try { return Player.skipReason?.(t) || ''; } catch (_) { return ''; }
}

// ── Reporting ────────────────────────────────────────────────────────────────

const _canEdit = () => {
  try { return !!Auth.canEdit; } catch (_) { return false; }
};
/** May the signed-in user report problems (edit rights)? */
export function canReport() { return _canEdit(); }

// Apply the fields a mark wrote to every copy of those tracks this page holds:
// the queue's rows, the open view (``soniqboom:track-edited`` → Library.patchTrack)
// and anyone else listening (``soniqboom:health-changed``).
function _spread(ids, fields) {
  const want = new Set(ids);
  try {
    let hit = false;
    for (const t of Player.queue || []) {
      if (t && typeof t === 'object' && want.has(t.id)) { Object.assign(t, fields); hit = true; }
    }
    const cur = Player.currentTrack;
    if (cur && want.has(cur.id)) Object.assign(cur, fields);
    if (hit) Player.refreshQueueView?.();
  } catch (_) { /* older cached player.js */ }
  for (const id of ids) {
    document.dispatchEvent(new CustomEvent('soniqboom:track-edited', { detail: { id, fields } }));
  }
  document.dispatchEvent(new CustomEvent('soniqboom:health-changed', { detail: { ids, fields } }));
}

async function _put(id, state, note) {
  const r = await fetch(`/api/tracks/${encodeURIComponent(id)}/health`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state: state || null, note: note || null }),
  });
  if (!r.ok) {
    let msg = r.status === 403 ? 'Reporting a problem needs edit rights.' : `Couldn’t save (HTTP ${r.status}).`;
    try { const j = await r.json(); if (j && typeof j.detail === 'string' && r.status !== 403) msg = j.detail; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

const _title = (t) => (t && t.title) || 'this track';

/**
 * Report ``track`` broken / glitched (``state``), or clear its report (null).
 * Saves, updates every copy this page holds, confirms with Undo.  Resolves to
 * the fields written, or null when it failed (already reported in a toast).
 */
export async function mark(track, state, note = null, { quiet = false } = {}) {
  if (!track || !track.id) return null;
  const before = {};
  for (const f of ['health', 'health_note', 'health_by', 'health_at', 'health_renderer']) {
    before[f] = track[f] ?? null;
  }
  let res;
  try { res = await _put(track.id, state, note); }
  catch (e) { window.Toast?.error?.(e.message || 'Couldn’t save.'); return null; }
  const ids = Array.isArray(res.ids) && res.ids.length ? res.ids : [track.id];
  _spread(ids, res.fields || {});
  if (!quiet) {
    const name = `“${_title(track)}”`;
    const copies = ids.length > 1 ? ` (and ${ids.length - 1} identical ${ids.length === 2 ? 'copy' : 'copies'})` : '';
    const msg = state === 'broken'
      ? `Marked ${name}${copies} broken — from now on it’s passed by when a list plays through.`
      : state === 'glitched' ? `Marked ${name}${copies} glitched.`
      : `Cleared the report on ${name}${copies}.`;
    window.Toast?.action?.(msg, 'Undo', async () => {
      // (Puts the earlier state and note back; a restored report is stamped
      // with who restored it and when.)
      try {
        const r2 = await _put(track.id, before.health, before.health_note);
        _spread(Array.isArray(r2.ids) && r2.ids.length ? r2.ids : ids, r2.fields || {});
      } catch (e) { window.Toast?.error?.(e.message || 'Couldn’t undo.'); }
    });
  }
  return res.fields || {};
}

// ── Notices ──────────────────────────────────────────────────────────────────

const TAUGHT_KEY = 'sb_skip_taught';
// The first time auto-play passes something by (per browser): say what
// happened and that picking it still plays it — once, not on every skip.
const TEACH = {
  broken: 'it was reported broken. Tracks reported broken are passed by when a list plays through',
  corrupt: 'it can’t be decoded. Tracks that can’t be decoded are passed by when a list plays through',
  empty: 'it has no tunes SoniqBoom plays and is no longer listed',
  short: (secs) => `it’s shorter than ${secs} s. Tunes that short are passed by when a list plays through (Settings → Skip short tunes)`,
};
function _onDealtSkip(e) {
  try { if (localStorage.getItem(TAUGHT_KEY) === '1') return; } catch (_) { return; }
  const first = (e && e.tracks || []).find((x) => x && TEACH[x.reason]);
  if (!first) return;
  try { localStorage.setItem(TAUGHT_KEY, '1'); } catch (_) {}
  let why = TEACH[first.reason];
  if (typeof why === 'function') why = why(Number(Player.skipShortSecs) || 0);
  // (a package with no tune in it doesn't play when picked either)
  const tail = first.reason === 'empty' ? '.' : '; a track you pick still plays.';
  window.Toast?.info?.(`Skipped “${_title(first.track)}” — ${why}${tail}`);
}

// A track marked broken that the listener picked: once it has played 15 s —
// or a short one to within a second of its end — offer to clear the mark:
// evidence, not a question asked before anything was heard (a stub that
// ends at once is no evidence).  Once per track per page.
const RECHECK_AFTER_S = 15;
const RECHECK_MIN_S = 5;
const _offered = new Set();
let _watch = null;                 // { id, track, cur, dur } while a picked broken track plays

function _playedEnough(w) {
  return w.cur >= RECHECK_AFTER_S || (w.dur >= RECHECK_MIN_S && w.cur >= w.dur - 1);
}

function _onTrackChange(t) {
  // (Moving on — a crossfade fires no 'ended' — judges what the last one played.)
  if (_watch && (!t || t.id !== _watch.id) && _playedEnough(_watch)) _offerClear();
  _watch = null;
  if (!t || t.health !== 'broken' || !canReport() || _offered.has(t.id)) return;
  let picked = false;
  try { picked = !!Player.explicitPick; } catch (_) { picked = false; }
  if (picked) _watch = { id: t.id, track: t, cur: 0, dur: 0 };
}

function _offerClear() {
  const w = _watch;
  _watch = null;
  if (!w || _offered.has(w.id) || w.track.health !== 'broken') return;
  _offered.add(w.id);
  window.Toast?.action?.(`“${_title(w.track)}” played — clear its broken mark?`, 'Clear mark',
    () => { mark(w.track, null); }, 15000);
}

function _onTime(d) {
  if (!_watch || !d) return;
  _watch.cur = Math.max(_watch.cur, Number(d.current) || 0);
  _watch.dur = Number(d.duration) || _watch.dur;
  if (_watch.cur >= RECHECK_AFTER_S) _offerClear();
}

// What "nothing to play" says (the player's ``offlinequeue`` event: every
// track of a queue / Play all is passed by), naming why when it knows.
const NOTHING_WHY = {
  offline: 'tracks on a drive or share that isn’t connected',
  broken: 'tracks reported broken', corrupt: 'tracks that can’t be decoded', short: 'short tunes',
  empty: 'packages with no tunes',
};
const _andList = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);
/** The notice for a queue none of whose tracks plays through. */
export function nothingToPlayText(reasons) {
  const rs = Array.isArray(reasons) ? reasons.filter((r) => NOTHING_WHY[r]) : [];
  if (rs.length === 1 && rs[0] === 'offline') {
    return 'None of these tracks can play right now — their drive or share isn’t connected.';
  }
  if (!rs.length) {
    return 'Nothing here plays through — offline, broken and undecodable tracks and short tunes are passed by. Pick a track to play it anyway.';
  }
  return `Nothing here plays through: ${_andList(rs.map((r) => NOTHING_WHY[r]))} are passed by. Pick a track to play it anyway.`;
}

// Failure messages that are about reaching the file, not the file
// (api/stream.py's 404 / 503 details, the player's network errors).
const NOT_THE_FILE = /not found|missing|unavailable|offline|not connected|unreachable|network|can.t be reached|interrupted|blocked|timed out|sign in/i;

let _inited = false;
/** Hook the notices to the player (once per page). */
export function init() {
  if (_inited) return;
  _inited = true;
  try {
    // "Couldn't play …" offers to mark the track that failed (not whatever
    // plays by the time the toast is clicked) — for users with edit rights,
    // and only when the file itself failed: a file that is missing, a source
    // that can't be reached or a network error is no broken track.
    // (Nor for a track already found unplayable — no engine decodes it, or a
    // package with no tune in it: it is passed by already, or gone.)
    Player.setFailAction?.((t, msg) => (canReport() && t && t.id && t.health !== 'broken' && !t.isStation
      && !isUnplayable(t) && !NOT_THE_FILE.test(String(msg || ''))
      ? { label: 'Mark broken', run: (track) => mark(track, 'broken') } : null));
    Player.on('dealtskip', _onDealtSkip);
    Player.on('trackchange', _onTrackChange);
    Player.on('timeupdate', _onTime);
    Player.on('ended', () => { if (_watch && _playedEnough(_watch)) _offerClear(); });
  } catch (_) { /* older cached player.js */ }
}

export const Health = { healthOf, healthBadge, healthBadgeHTML, skipWord, mark, canReport, reportedLine,
                         nothingToPlayText, isUnplayable, init };
