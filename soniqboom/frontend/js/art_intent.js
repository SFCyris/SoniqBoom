// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

// ── "This track is being played / looked at" for its game box cover ────────
//
// A retro track's game box cover (IGDB, with the user's own Twitch app —
// core/igdb_cover.py) is looked up only when the track is PLAYED or its
// Track Info OPENED, never for a list row.  The server can't tell a list
// row's cover request from the player's, so the page says so:
// ``POST /api/art/{id}/intent``.  Only tracks with a game name ask (the
// lookup is by game); the server decides the rest (IGDB set up, nothing
// known yet) and sends ``art_ready`` when a cover is found, which repaints
// every picture of the track the usual way.
//
// Each id asks at most once per ``_AGAIN_MS`` from this page.

const _AGAIN_MS = 10 * 60 * 1000;
const _MAX = 500;
const _asked = new Map();     // track id → when this page last asked

/** Tell the server ``track`` is being played or looked at.  Returns whether
 *  a request was sent. */
export function artIntent(track, { now = Date.now(), fetchFn = globalThis.fetch } = {}) {
  const id = track && track.id;
  if (!id || track.station || typeof id !== 'string' || !String(track.game || '').trim()) return false;
  const at = _asked.get(id);
  if (at !== undefined && now - at < _AGAIN_MS) return false;
  _asked.delete(id);
  _asked.set(id, now);
  if (_asked.size > _MAX) _asked.delete(_asked.keys().next().value);
  try {
    const p = fetchFn(`/api/art/${encodeURIComponent(id)}/intent`, {
      method: 'POST', credentials: 'same-origin',
    });
    if (p && typeof p.catch === 'function') p.catch(() => {});
  } catch (_) { /* best effort */ }
  return true;
}

/** Ask for every track ``player`` starts (its ``trackchange`` event). */
export function watchPlays(player) {
  if (player && typeof player.on === 'function') {
    player.on('trackchange', (t) => { try { artIntent(t); } catch (_) { /* isolation */ } });
  }
}

/** (tests) forget what was asked. */
export function _resetArtIntent() { _asked.clear(); }
