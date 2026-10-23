// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The All-Tracks in-place refresh (library.js), run outside a browser:
//   * ``[row-repaint:begin]`` — a forced render repaints a row only when it
//     shows another track, so an in-place refresh must forget what the rows
//     show first (else a re-tagged track or a new offline mark never appears);
//   * ``[quiet-refresh:begin]`` — refreshAllTracksInPlace runs one refresh at
//     a time, runs once more for a request that arrived meanwhile or a refresh
//     another All-Tracks render superseded, and stops when the user left.
// Both regions are lifted out by their markers and compiled with stubs.
// Run: node --test tests/js/row_repaint.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/library.js'), 'utf8');
const region = (name) => {
  const m = SRC.match(new RegExp(`// \\[${name}:begin\\][^\\n]*\\n([\\s\\S]*?)// \\[${name}:end\\]`));
  if (!m) throw new Error(`${name} region not found`);
  return m[1];
};

function repaintCore() {
  return new Function('env', `
    let currentTracks = env.tracks;
    const _fillFailedRow = () => {}, _fillSkeletonRow = () => {};
    function _fillTrackRow(tr, t) { env.painted.push(t.id); tr.__tid = t.id; tr.text = t.title; }
    ${region('row-repaint')}
    return { _fillTrackRowAt, _forgetPaintedRows, setTracks(t) { currentTracks = t; } };
  `);
}

test('a forced render alone keeps a row that shows the same track id (the trap)', () => {
  const env = { tracks: [{ id: 'a', title: 'Old A' }, { id: 'b', title: 'Old B' }], painted: [] };
  const core = repaintCore()(env);
  const rows = [{}, {}];
  rows.forEach((r, i) => core._fillTrackRowAt(r, i, true));
  env.painted.length = 0;
  core.setTracks([{ id: 'a', title: 'New A' }, { id: 'b', title: 'New B' }]);   // re-tagged, same ids
  rows.forEach((r, i) => core._fillTrackRowAt(r, i, false));
  assert.deepEqual(env.painted, []);
  assert.equal(rows[0].text, 'Old A');
});

test('forgetting the painted rows first repaints every row with the new data', () => {
  const env = { tracks: [{ id: 'a', title: 'Old A' }, { id: 'b', title: 'Old B' }], painted: [] };
  const core = repaintCore()(env);
  const rows = [{}, {}];
  rows.forEach((r, i) => core._fillTrackRowAt(r, i, true));
  env.painted.length = 0;
  core.setTracks([{ id: 'a', title: 'New A', offline: 'drive' }, { id: 'b', title: 'New B' }]);
  core._forgetPaintedRows({ rows: () => rows });
  rows.forEach((r, i) => core._fillTrackRowAt(r, i, false));
  assert.deepEqual(env.painted, ['a', 'b']);
  assert.deepEqual(rows.map((r) => r.text), ['New A', 'New B']);
});

test('the in-place render forgets the painted rows before it renders', () => {
  const fn = SRC.slice(SRC.indexOf('async function renderTracks('), SRC.indexOf('// The load-error message of a view that is gone'));
  const forget = fn.indexOf('_forgetPaintedRows(_trackVL)');
  assert.ok(forget > 0 && forget < fn.indexOf('_vsRender(true)'));
  assert.match(fn, /if \(keepScroll\) \{\s*_forgetPaintedRows\(_trackVL\);/);
});

function quietCore(env) {
  return new Function('env', `
    let _viewIsAllTracks = true;
    async function showAll(opts) { env.runs.push(opts); return env.next(); }
    ${region('quiet-refresh')}
    return { refreshAllTracksInPlace, set allTracks(v) { _viewIsAllTracks = v; } };
  `)(env);
}
const tick = () => new Promise((r) => setTimeout(r, 0));

test('a refresh asked for during a refresh runs once more after it (not dropped, not doubled)', async () => {
  let release;
  const env = { runs: [], next: () => new Promise((r) => { release = r; }) };
  const q = quietCore(env);
  const p1 = q.refreshAllTracksInPlace();
  const p2 = q.refreshAllTracksInPlace();             // a scan finished meanwhile
  const p3 = q.refreshAllTracksInPlace();             // …and another
  assert.equal(p1, p2);
  await tick(); assert.equal(env.runs.length, 1);
  release('done'); await tick();
  assert.equal(env.runs.length, 2);                   // exactly one follow-up
  release('done'); await p3;
  assert.equal(env.runs.length, 2);
  assert.ok(env.runs.every((o) => o && o.quiet === true));
});

test('a refresh another All-Tracks render superseded runs again; leaving All Tracks stops it', async () => {
  const results = ['superseded', 'done'];
  const env = { runs: [], next: async () => results.shift() };
  await quietCore(env).refreshAllTracksInPlace();
  assert.equal(env.runs.length, 2);
  const env2 = { runs: [], next: async () => { q2.allTracks = false; return 'superseded'; } };
  const q2 = quietCore(env2);
  await q2.refreshAllTracksInPlace();
  assert.equal(env2.runs.length, 1);
  const env3 = { runs: [], next: async () => 'superseded' };   // something keeps superseding it
  await quietCore(env3).refreshAllTracksInPlace();
  assert.equal(env3.runs.length, 3);                 // bounded
  const env4 = { runs: [], next: async () => 'done' };
  const q4 = quietCore(env4); q4.allTracks = false;
  await q4.refreshAllTracksInPlace();
  assert.equal(env4.runs.length, 0);                 // not on All Tracks: nothing to refresh
});

test('keep-scroll is an explicit render option, never a module-wide token', () => {
  assert.doesNotMatch(SRC, /_keepScrollGen/);
  const calls = [...SRC.matchAll(/(?<!function )renderTracks\([^)]*keepScroll[^)]*\)/g)].map((m) => m[0]);
  // (All Tracks' quiet refresh, a windowed store's install, the search view's in-place re-read)
  assert.deepEqual(calls.sort(), ['renderTracks(rows, { keepScroll: quiet })', 'renderTracks(store, { keepScroll })',
                                  'renderTracks(list, { keepScroll })'].sort());
  // a quiet refresh whose count probe fails keeps the rows on screen (large libraries too)
  const all = SRC.slice(SRC.indexOf('async function showAll('), SRC.indexOf('function _rebuildWindowedStore('));
  assert.match(all, /catch \{\s*if \(quiet\) return _gen === _browseNavGen \? 'failed' : 'superseded';/);
  // …and swaps a windowed store only once the rows on screen are loaded
  assert.match(all, /await Promise\.all\(\[\.\.\.store\._pending\.values\(\)\]\);[\s\S]*?if \(!store\._chunks\.has\(c\)\) return 'failed';[\s\S]*?_installWindowedStore\(store, total, sortBy, sortOrder, \{ keepScroll: true \}\)/);
});
