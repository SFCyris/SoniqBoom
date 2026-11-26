// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * playlist/edit_view.js — what an edit of the open playlist keeps (pure: no
 * DOM, no fetch; tests/js/playlist_edit_view.test.mjs).
 *
 * An edit (reorder, remove, rename, add to the open playlist) re-reads the
 * playlist and paints it again.  The list stays where it was scrolled to and
 * the rows that were selected stay selected — found again by their entry (the
 * track and its tune), the second copy of a track that is in the playlist
 * twice as the second copy.  A new playlist made from the Add-to-playlist
 * menu starts with the entries it was opened for.
 *
 * Exports: entryKey, newPlaylistEntries, remapSelection, remapIndex, movedSelection
 */

const _tune = (t) => {
  const s = t && t.subsong;
  return (Number.isInteger(s) && s >= 0) ? s : null;
};

/** An entry's identity: the track id, and its tune when one was picked. */
export function entryKey(t) {
  if (t == null) return '';
  if (typeof t === 'string') return t;
  const s = _tune(t);
  return s === null ? String(t.id) : `${t.id}#${s}`;
}

/**
 * The ``track_ids`` of a new playlist from the entries it starts with: a bare
 * id stays a bare id, a picked tune stays ``{id, subsong}`` (anything else —
 * no id, a tune that isn't a whole number ≥ 0 — is dropped, or kept as the
 * bare id).  Order and repeats are kept: they are the listener's.
 */
export function newPlaylistEntries(entries) {
  if (!Array.isArray(entries)) return [];
  const out = [];
  for (const e of entries) {
    if (typeof e === 'string') { if (e) out.push(e); continue; }
    if (!e || typeof e !== 'object' || typeof e.id !== 'string' || !e.id) continue;
    const s = _tune(e);
    out.push(s === null ? e.id : { id: e.id, subsong: s });
  }
  return out;
}

// key → the indices it appears at in ``tracks`` (in order)
function _positions(tracks) {
  const at = new Map();
  (tracks || []).forEach((t, i) => {
    const k = entryKey(t);
    const list = at.get(k);
    if (list) list.push(i); else at.set(k, [i]);
  });
  return at;
}
// index → [key, n]: the n-th copy of key
function _occurrence(tracks, i) {
  const k = entryKey(tracks[i]);
  let n = 0;
  for (let j = 0; j < i; j++) if (entryKey(tracks[j]) === k) n++;
  return [k, n];
}

/**
 * The selected rows of ``oldTracks`` (a Set of indices) as indices into
 * ``newTracks``: each row by its entry, the n-th copy of an entry as its
 * n-th copy.  A row whose entry is gone is no longer selected.
 */
export function remapSelection(oldTracks, selected, newTracks) {
  const out = new Set();
  if (!selected || !selected.size) return out;
  const at = _positions(newTracks);
  const seen = new Map();
  (oldTracks || []).forEach((t, i) => {
    const k = entryKey(t);
    const n = seen.get(k) || 0;
    seen.set(k, n + 1);
    if (!selected.has(i)) return;
    const list = at.get(k);
    if (list && n < list.length) out.add(list[n]);
  });
  return out;
}

/** One index (the Shift-click anchor) the same way; null when it is gone. */
export function remapIndex(oldTracks, i, newTracks) {
  if (!Number.isInteger(i) || !oldTracks || i < 0 || i >= oldTracks.length) return null;
  const [k, n] = _occurrence(oldTracks, i);
  const list = _positions(newTracks).get(k);
  return list && n < list.length ? list[n] : null;
}

/**
 * After a reorder: the selected rows of ``before`` (a Set of indices) at their
 * places in ``after`` — the same entry objects in a new order (a drag moves
 * objects, it doesn't copy them), so a track that is in the list twice stays
 * the copy it was.
 */
export function movedSelection(before, after, selected) {
  const at = new Map();
  (after || []).forEach((t, i) => at.set(t, i));
  const out = new Set();
  for (const i of selected || []) {
    const j = at.get((before || [])[i]);
    if (j !== undefined) out.add(j);
  }
  return out;
}
