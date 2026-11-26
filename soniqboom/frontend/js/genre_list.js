// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * genre_list.js — which genres the web Genres view lists (pure; tests in
 * tests/js/genre_list.test.mjs).
 *
 * A retro library's genre tag often holds a game's or a group's name, so most
 * of its genres name one track each (14,828 of 30,924 on one library).  The
 * Genres view lists the genres of two tracks or more; a key in its filter bar
 * shows all of them ("Show all 30,924 genres"), and the choice is kept on the
 * device (localStorage ``sb_genres_all``).  The counts are the ones the list
 * already carries (``/api/library/genres``, the store's genre aggregate): the
 * library is never read for this.  Subsonic's genre list is not affected.
 */

export const GENRES_ALL_KEY = 'sb_genres_all';

const _n = (k) => Number(k || 0).toLocaleString('en-US');

/** A genre row of one track (the "[No Genre]" row, ``label``, always shows). */
export function isSingleTrackGenre(item) {
  return !!item && !item.label && (Number(item.count) || 0) <= 1;
}

/**
 * The rows to list: every row when ``showAll``, else those of two tracks or
 * more.  → { rows, total, single } (``single``: how many rows name one track).
 */
export function genreRows(items, showAll) {
  const all = Array.isArray(items) ? items : [];
  let single = 0;
  const kept = [];
  for (const g of all) {
    if (isSingleTrackGenre(g)) { single++; if (showAll) kept.push(g); }
    else kept.push(g);
  }
  return { rows: showAll ? all.slice() : kept, total: all.length, single };
}

/** The filter bar key's text and tooltip, or null when no genre names one track. */
export function genreToggleText({ total, single }, showAll) {
  if (!single) return null;
  return showAll
    ? { text: `Hide ${_n(single)} one-track genre${single === 1 ? '' : 's'}`,
        title: 'List only the genres of two tracks or more' }
    : { text: `Show all ${_n(total)} genres`,
        title: `${_n(single)} genre${single === 1 ? '' : 's'} of one track ${single === 1 ? 'is' : 'are'} not listed — often a game's or group's name in the genre tag` };
}

export function readShowAllGenres(storage) {
  try { return (storage || globalThis.localStorage).getItem(GENRES_ALL_KEY) === '1'; } catch (_) { return false; }
}
export function saveShowAllGenres(on, storage) {
  try { (storage || globalThis.localStorage).setItem(GENRES_ALL_KEY, on ? '1' : '0'); } catch (_) { /* private mode */ }
}
