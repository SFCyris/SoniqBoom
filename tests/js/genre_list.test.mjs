// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The web Genres view lists genres of two tracks or more unless "Show all N
// genres" is on (soniqboom/frontend/js/genre_list.js), remembered per device.
// Run: node --test tests/js/genre_list.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const G = await import(resolve(FE, 'js/genre_list.js'));

const list = [
  { genre: 'Game', count: 212618 }, { genre: 'Rock', count: 12 }, { genre: 'Commando', count: 1 },
  { genre: 'Fairlight', count: 1 }, { genre: 'Demo', count: 2 }, { label: '[No Genre]', genre: '', count: 1 },
  { genre: 'Zero', count: 0 },
];

test('one-track genres are left out by default; the [No Genre] row stays', () => {
  const r = G.genreRows(list, false);
  assert.deepEqual(r.rows.map((g) => g.label || g.genre), ['Game', 'Rock', 'Demo', '[No Genre]']);
  assert.equal(r.total, 7);
  assert.equal(r.single, 3);
});

test('Show all lists every genre, in the server order', () => {
  const r = G.genreRows(list, true);
  assert.equal(r.rows.length, 7);
  assert.deepEqual(r.rows.map((g) => g.genre), list.map((g) => g.genre));
  assert.notEqual(r.rows, list);            // (a copy: the cached list is never changed)
});

test('the key says what it does; no key when no genre names one track', () => {
  const r = G.genreRows(list, false);
  assert.equal(G.genreToggleText(r, false).text, 'Show all 7 genres');
  assert.match(G.genreToggleText(r, false).title, /3 genres of one track are not listed/);
  assert.equal(G.genreToggleText(r, true).text, 'Hide 3 one-track genres');
  assert.equal(G.genreToggleText({ total: 30924, single: 14828 }, false).text, 'Show all 30,924 genres');
  assert.equal(G.genreToggleText({ total: 2, single: 1 }, true).text, 'Hide 1 one-track genre');
  assert.equal(G.genreToggleText({ total: 5, single: 0 }, false), null);
  assert.deepEqual(G.genreRows(null, false), { rows: [], total: 0, single: 0 });
});

test('the choice is kept on the device (sb_genres_all); unreadable storage = default', () => {
  const store = new Map();
  const ls = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  assert.equal(G.readShowAllGenres(ls), false);
  G.saveShowAllGenres(true, ls);
  assert.equal(store.get('sb_genres_all'), '1');
  assert.equal(G.readShowAllGenres(ls), true);
  G.saveShowAllGenres(false, ls);
  assert.equal(G.readShowAllGenres(ls), false);
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.equal(G.readShowAllGenres(broken), false);
  assert.doesNotThrow(() => G.saveShowAllGenres(true, broken));
});

test('library.js Genres view filters the list it already has (no extra request per render)', () => {
  const L = readFileSync(resolve(FE, 'js/library.js'), 'utf8');
  const g = L.slice(L.indexOf('async function showGenres()'), L.indexOf('// Drill-down fetch ceiling'));
  assert.equal((g.match(/API\(/g) || []).length, 1, 'one fetch: /library/genres');
  assert.match(g, /genreRows\(genres, readShowAllGenres\(\)\)/);
  assert.match(g, /renderGroupList\(shown\.rows, 'genre'/);
  assert.match(g, /genreRows\(all, next\)/);
});
