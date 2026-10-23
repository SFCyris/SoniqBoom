// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A list sorted in the browser (soniqboom/frontend/js/library.js, the
// ``[list-sort:begin]`` / ``[list-sort:end]`` region) orders exactly as the
// server's sorted indexes do (store.py _SORTED_SPECS): text trimmed and
// lower-cased, blanks last, compared by code point; numbers with no value last;
// ties by id; descending = ascending reversed.  (tests/test_list_sort_parity.py
// compares it with the server's own orders.)
// Run: node --test tests/js/list_sort.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const LIB = readFileSync(process.env.SB_LIBRARY_JS || resolve(HERE, '../../soniqboom/frontend/js/library.js'), 'utf8');
const m = LIB.match(/\/\/ \[list-sort:begin\][^\n]*\n([\s\S]*?)\/\/ \[list-sort:end\]/);
if (!m) throw new Error('list-sort region not found');
const { _sortRows } = new Function(`${m[1]}; return { _sortRows };`)();
const ids = (rows, key, asc = true) => _sortRows(rows, key, asc).map((t) => t.id);

test('text: case-insensitive, trimmed, blanks after everything (asc), ties by id', () => {
  const rows = [
    { id: 'e', title: '' }, { id: 'a', title: 'beta' }, { id: 'c', title: 'Alpha' },
    { id: 'b', title: 'alpha' }, { id: 'd', title: '  Beta  ' }, { id: 'f' }, { id: 'g', title: '   ' },
    { id: 'h', title: 'Zed' },
  ];
  assert.deepEqual(ids(rows, 'title'), ['b', 'c', 'a', 'd', 'h', 'e', 'f', 'g']);
  // descending is the exact reverse — blanks first, ties by id descending
  assert.deepEqual(ids(rows, 'title', false), ['g', 'f', 'e', 'h', 'd', 'a', 'c', 'b']);
});

test("Python's whitespace and code-point order", () => {
  const rows = [
    { id: 'bom', artist: '﻿x' },          // U+FEFF is not whitespace to Python: kept
    { id: 'fs', artist: '\x1cx' },             // U+001C is: trimmed
    { id: 'x', artist: 'x' },
    { id: 'emoji', artist: '\u{1F600}' },      // past U+FFFF: after U+FFEF and after a blank
    { id: 'pua', artist: '￯' },
    { id: 'blank', artist: ' ' },
  ];
  assert.deepEqual(ids(rows, 'artist'), ['fs', 'x', 'bom', 'pua', 'blank', 'emoji']);
});

test('numbers: numeric strings, YYYYMMDD years, no value last; zero length is no length', () => {
  const years = [
    { id: 'a', year: '1992' }, { id: 'b', year: 19930101 }, { id: 'c', year: 1991 },
    { id: 'd', year: 'x' }, { id: 'e' }, { id: 'f', year: ' 1_990 ' }, { id: 'g', year: 'inf' },
    { id: 'h', year: true },
  ];
  assert.deepEqual(ids(years, 'year'), ['h', 'f', 'c', 'a', 'b', 'd', 'e', 'g']);
  const durs = [{ id: 'a', duration: 0 }, { id: 'b', duration: 7 }, { id: 'c', duration: null },
                { id: 'd', duration: '120' }, { id: 'e', duration: 50.5 }];
  assert.deepEqual(ids(durs, 'duration'), ['b', 'e', 'd', 'a', 'c']);
});

test('Track: disc then number, no track number last; Location: as text', () => {
  const rows = [
    { id: 'a', track_number: 2, disc_number: 1 }, { id: 'b', track_number: 1, disc_number: 2 },
    { id: 'c', track_number: 1 }, { id: 'd' }, { id: 'e', track_number: '3', disc_number: 1 },
  ];
  assert.deepEqual(ids(rows, 'track_number'), ['c', 'a', 'e', 'b', 'd']);
  const paths = [{ id: 'a', path: '/m/Zed.mod' }, { id: 'b', path: '/m/alpha.mod' }, { id: 'c', path: '/M/beta.mod' }];
  assert.deepEqual(ids(paths, 'path'), ['b', 'c', 'a']);
});

test('an unknown column keeps the order; the input is not changed', () => {
  const rows = [{ id: 'b' }, { id: 'a' }];
  assert.deepEqual(ids(rows, 'nope'), ['b', 'a']);
  _sortRows(rows, 'title');
  assert.deepEqual(rows.map((r) => r.id), ['b', 'a']);
});
