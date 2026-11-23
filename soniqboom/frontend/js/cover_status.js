// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

// ── Which list rows need to ask for a cover at all ──────────────────────────
//
// A list row asks its cover as an <img>.  A browser runs about six requests
// to one server at once (HTTP/1.1), so in a list full of tracks WITHOUT a
// cover — most of a retro-music library — those "no cover" answers queue in
// front of the rows that have one, and each is asked again every minute.
//
// So a row whose track carries no embedded cover (``cover_art`` empty) does
// not ask its image straight away: its id joins the batch of this paint, and
// the batch goes to the server in ONE request (``POST /api/art/status``,
// api/art.py).  Ids the server knows to have no cover at all (no embedded
// one, its folder recorded as holding no image, no online cover that may
// still come) are remembered and never asked; every other row asks its image
// as before.  Rows WITH an embedded cover are not part of this: they ask at
// once, and the browser's cache answers those without any request.
//
// What was learned is forgotten — so the row asks again — on ``art_ready``
// for that track (``forgetCover``) and on ``art_refresh`` or a re-connected
// socket (``forgetAllCovers``).  A failed status request falls back to the
// old behaviour: every queued row asks its image.

const _none = new Set();     // ids with no cover at all (the server said so)
const _maybe = new Set();    // ids asked about: they may have one — ask the image
let _queue = new Map();      // id → [callbacks] waiting for this paint's answer
let _timer = 0;
// What was forgotten while a status request was out: its "none" for such an
// id is older than the news (an art_ready that came meanwhile) and is not
// believed — the row asks its image.
let _epoch = 0;              // bumped by forgetAllCovers
const _forgotAt = new Map(); // id → _seq when forgetCover last ran for it
let _seq = 0;
const _MAX = 20000;          // bound both sets (a huge library scrolled through)
const _CHUNK = 300;          // the server's per-request limit

// ── Rows this viewer saw WITH a cover (kept across page loads) ──────────────
// A track without an embedded cover can still show one: its folder's image,
// cached on the server, or an online cover.  Such a row went through the
// status request on every page load — one round trip before its image was
// even asked, although the browser had that image cached: a repeat view with
// the browser 60 ms away painted 51 ms (Chromium) / 71 ms (Firefox) later
// than 2.1.1 did.  So the ids whose cover this browser has shown are kept
// (localStorage, at most ``_SEEN_MAX``, the last seen kept) and those rows ask
// their image at once.  Never wrong in the other direction: a cover that is
// gone answers "no cover" and the id is forgotten (``noteCoverGone``).
const _SEEN_KEY = 'sb_cover_seen_v1';
const _SEEN_MAX = 4000;
let _seen = null;            // Map id → 1, oldest first (loaded on first use)
let _seenTimer = 0;

function _seenIds() {
  if (_seen) return _seen;
  _seen = new Map();
  try {
    const raw = globalThis.localStorage && globalThis.localStorage.getItem(_SEEN_KEY);
    const ids = raw ? JSON.parse(raw) : [];
    if (Array.isArray(ids)) for (const id of ids.slice(-_SEEN_MAX)) if (typeof id === 'string') _seen.set(id, 1);
  } catch (_) { /* no storage (private window, blocked): only this page remembers */ }
  return _seen;
}

function _saveSeenSoon() {
  if (_seenTimer) return;
  _seenTimer = setTimeout(() => {
    _seenTimer = 0;
    try { globalThis.localStorage.setItem(_SEEN_KEY, JSON.stringify([..._seenIds().keys()])); } catch (_) { /* ignore */ }
  }, 1000);
}

/** The row of track ``id`` (no embedded cover) showed a cover. */
export function noteCoverShown(id) {
  if (!id) return;
  const s = _seenIds();
  if (s.has(id)) s.delete(id);             // (to the newest end)
  s.set(id, 1);
  while (s.size > _SEEN_MAX) s.delete(s.keys().next().value);
  _saveSeenSoon();
}

/** The row of track ``id`` asked its cover and got none. */
export function noteCoverGone(id) {
  if (id && _seenIds().delete(id)) _saveSeenSoon();
}

/** Run ``go`` (the row asks its cover image) unless track ``id`` is known to
 *  have no cover; an id not asked about yet waits for one batched status
 *  request first.  ``go`` must check that its row still shows ``id``. */
export function askCoverWhenUseful(id, go) {
  if (!id) { go(); return; }
  if (_none.has(id)) return;
  if (_maybe.has(id) || _seenIds().has(id)) { go(); return; }   // (seen with a cover before)
  let q = _queue.get(id);
  if (!q) _queue.set(id, (q = []));
  q.push(go);
  if (!_timer) _timer = setTimeout(_flush, 0);   // one request per paint
}

/** Is track ``id`` known to have no cover? */
export function knownNoCover(id) {
  return !!id && _none.has(id);
}

/** Track ``id``'s cover may have changed (``art_ready``): ask it again. */
export function forgetCover(id) {
  if (!id) return;
  _none.delete(id);
  _maybe.delete(id);
  _seq += 1;
  _forgotAt.set(id, _seq);
  if (_forgotAt.size > _MAX) _forgotAt.clear();
}

/** Any cover may have changed (``art_refresh``, a re-connected socket). */
export function forgetAllCovers() {
  _none.clear();
  _maybe.clear();
  _epoch += 1;
}

async function _flush() {
  _timer = 0;
  const batch = _queue;
  _queue = new Map();
  const ids = [...batch.keys()];
  for (let i = 0; i < ids.length; i += _CHUNK) {
    const chunk = ids.slice(i, i + _CHUNK);
    const sentEpoch = _epoch;
    const sentSeq = _seq;
    let none = null;
    try {
      const r = await fetch('/api/art/status', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: chunk }),
      });
      if (r.ok) {
        const body = await r.json();
        none = new Set(Array.isArray(body && body.none) ? body.none : []);
      }
    } catch (_) { none = null; }            // (old behaviour: every row asks)
    if (_none.size > _MAX) _none.clear();
    if (_maybe.size > _MAX) _maybe.clear();
    const stale = _epoch !== sentEpoch;
    for (const id of chunk) {
      const fresh = !stale && !((_forgotAt.get(id) || 0) > sentSeq);
      if (none && none.has(id) && fresh) { _none.add(id); continue; }
      if (none && fresh) _maybe.add(id);
      for (const go of batch.get(id) || []) {
        try { go(); } catch (_) { /* one row never stops the others */ }
      }
    }
  }
}
