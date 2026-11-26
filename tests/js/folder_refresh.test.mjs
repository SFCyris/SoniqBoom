// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A folder's in-place refresh (library.js refreshCurrentFolderInPlace →
// showFolder({quiet})) after the library changed under it — an admin
// enrichment pass, a re-tag picked up by a scan:
//   * ``[folder-refresh:begin]`` — _sameTrackList (same tracks, same order),
//     _sameRow (the row on screen still says what the server says),
//     _sameChunks (the same for a windowed list) and the page's live values
//     (_recordLive / _keepLive: what the cached folder rows don't follow),
//     lifted out by their markers and compiled with a copy of offline.js's
//     offlineKind rule (checked against offline.js below);
//   * ``[folder-quiet:begin]`` — one refresh at a time, a request arriving
//     meanwhile runs once more after it (or for the folder opened meanwhile),
//     and the scroll offset is put back only after a list was rendered anew at
//     the top;
//   * the two quiet paths repaint a re-tagged folder in place instead of
//     keeping the old rows (they used to stop at "same ids").
// Run: node --test tests/js/folder_refresh.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(process.env.SB_LIBRARY_JS || resolve(HERE, '../../soniqboom/frontend/js/library.js'), 'utf8');
const OFFLINE_SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/offline.js'), 'utf8');
const region = (name) => {
  const m = SRC.match(new RegExp(`// \\[${name}:begin\\][^\\n]*\\n([\\s\\S]*?)// \\[${name}:end\\]`));
  if (!m) throw new Error(`${name} region not found`);
  return m[1];
};

const OFFLINE_RULE = "return (k === 'drive' || k === 'share' || k === 'catalogue') ? k : '';";
// A fresh copy of the region per call (its live-value record is module state).
const compile = () => new Function(`
  function offlineKind(t) {            // (offline.js — see the drift check below)
    const k = t && t.offline;
    ${OFFLINE_RULE}
  }
  ${region('folder-refresh')}
  return { _sameTrackList, _sameRow, _sameChunks, _keepLive, _recordLive, _liveVals };
`)();
const { _sameTrackList, _sameRow, _sameChunks } = compile();

test('the copied offlineKind rule is still offline.js\'s', () => {
  assert.ok(OFFLINE_SRC.includes(OFFLINE_RULE), 'offline.js offlineKind changed — update the copy');
});

const row = (id, extra = {}) => ({
  id, title: `T ${id}`, artist: 'A', album: 'Al', year: 2001, genre: ['Chip'], format: 'MP3',
  game: null, duration: 120, duration_rev: null, offline: null,
  health: null, health_at: null, defect: null, defect_detail: null, ...extra,
});
const store = (chunks) => ({ _isWindowedStore: true, _chunks: new Map(chunks) });

test('the same rows, read again, are the same (no repaint)', () => {
  assert.equal(_sameRow(row('a'), row('a')), true);
  assert.equal(_sameRow(row('a', { genre: ['Chip', 'Demo'] }), row('a', { genre: ['Chip', 'Demo'] })), true);
});

test('a re-tagged field is a change — the trap the old same-ids check fell into', () => {
  const shown = [row('a'), row('b')];
  const fresh = [row('a'), row('b', { title: 'Renamed' })];
  assert.equal(_sameTrackList(fresh, shown), true);          // same tracks, same order …
  assert.equal(fresh.every((t, i) => _sameRow(t, shown[i])), false);   // … but not the same rows
  assert.equal(_sameRow(row('a', { year: 1999 }), row('a')), false);
  assert.equal(_sameRow(row('a', { genre: ['Rock'] }), row('a')), false);
  assert.equal(_sameRow(row('a', { game: 'Uridium' }), row('a')), false);
});

test('a field cleared on the server is a change (folder rows come back whole)', () => {
  assert.equal(_sameRow(row('a', { album: '' }), row('a')), false);
  assert.equal(_sameRow(row('a', { game: null }), row('a', { game: 'Uridium' })), false);
});

test('missing and null are the same; a field only the page set is not compared', () => {
  const fresh = row('a', { comment: null });
  const shown = row('a');                                      // no ``comment`` key at all
  shown._pageOnly = 1;                                         // set on the page, not by the server
  assert.equal(_sameRow(fresh, shown), true);
});

test('another track or another offline mark is never the same row', () => {
  assert.equal(_sameRow(row('a'), row('b')), false);
  assert.equal(_sameRow(row('a', { offline: 'drive' }), row('a')), false);
  assert.equal(_sameRow(null, row('a')), false);
  assert.equal(_sameTrackList([row('a')], [row('a', { offline: 'share' })]), false);
});

// The server's cached folder rows don't follow a problem report, an unplayable
// mark or a render-only length backfill; the page learns those live.
test('a live mark the page learnt is put on the folder rows read again (no revert, no change)', () => {
  const L = compile();
  const shown = row('a', { health: 'glitched', health_at: 1795621410.59 });
  L._recordLive('a', { health: 'glitched', health_at: 1795621410.59, title: 'not live' });
  assert.deepEqual(L._liveVals.get('a'), { health: 'glitched', health_at: 1795621410.59 });   // only live fields
  const fresh = L._keepLive([row('a')]);                       // the cache: no mark
  assert.equal(fresh[0].health, 'glitched');
  assert.equal(L._sameRow(fresh[0], shown), true);             // nothing to repaint
  // a real change elsewhere in the row still lands, with the mark
  const f2 = L._keepLive([row('a', { title: 'Re-tagged' })]);
  assert.equal(L._sameRow(f2[0], shown), false);
  assert.deepEqual([f2[0].title, f2[0].health], ['Re-tagged', 'glitched']);
  // a mark the page cleared stays cleared over a cached one
  L._recordLive('b', { health: null, health_at: 1795621500 });
  assert.equal(L._keepLive([row('b', { health: 'broken', health_at: 1795621000 })])[0].health, null);
});

test('a live value is forgotten once the server says the same — then the server counts again', () => {
  const L = compile();
  L._recordLive('x', { duration: 105.07 });
  assert.equal(L._keepLive([row('x', { format: 'AHX', duration: 0 })])[0].duration, 105.07);    // stale cache
  assert.equal(L._keepLive([row('x', { format: 'AHX', duration: 105.07 })])[0].duration, 105.07); // caught up
  assert.equal(L._liveVals.has('x'), false);
  // a later rescan's value is the server's again
  assert.equal(L._keepLive([row('x', { format: 'AHX', duration: 98 })])[0].duration, 98);
});

test('the same row object is given the live values once (a second pass is not "the server caught up")', () => {
  const L = compile();
  L._recordLive('a', { health: 'broken' });
  const rows = [row('a')];
  L._keepLive(rows); L._keepLive(rows);
  assert.equal(rows[0].health, 'broken');
  assert.deepEqual(L._liveVals.get('a'), { health: 'broken' });
});

test('a value the page never learnt live is the server\'s: a scan-set defect, a re-tagged length', () => {
  const L = compile();
  const shownPsf = row('p', { format: 'PSF', duration: 180 });
  const rescanned = L._keepLive([row('p', { format: 'PSF', duration: 212, defect: 'partial' })]);
  assert.equal(L._sameRow(rescanned[0], shownPsf), false);
  assert.deepEqual([rescanned[0].duration, rescanned[0].defect], [212, 'partial']);
});

test('windowed: chunks are the same only when every fresh chunk matches one the list holds', () => {
  const shown = store([[0, [row('a'), row('b')]], [3, [row('x'), row('y')]]]);
  assert.equal(_sameChunks(store([[0, [row('a'), row('b')]]]), shown), true);
  assert.equal(_sameChunks(store([[0, [row('a'), row('b')]], [3, [row('x'), row('y')]]]), shown), true);
  // a re-tag in a chunk on screen
  assert.equal(_sameChunks(store([[3, [row('x'), row('y', { album: 'New' })]]]), shown), false);
  // the list never read that chunk: can't say it is the same
  assert.equal(_sameChunks(store([[1, [row('m')]]]), shown), false);
  // same count, other tracks (an add + a remove)
  assert.equal(_sameChunks(store([[0, [row('a'), row('c')]]]), shown), false);
});

// ── [folder-quiet] — one refresh at a time ───────────────────────────────────
function quietCore(env) {
  return new Function('env', `
    let _currentBrowsePath = env.path, _currentBrowseRecursive = false;
    const document = { getElementById: () => env.wrap };
    const requestAnimationFrame = (fn) => env.frames.push(fn);
    async function showFolder(path, rec, opts) { env.runs.push([path, opts]); return env.next(); }
    ${region('folder-quiet')}
    return { refreshCurrentFolderInPlace, set path(v) { _currentBrowsePath = v; } };
  `)(env);
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const mkEnv = () => ({ path: '/m/a', runs: [], frames: [], wrap: { scrollTop: 500 }, next: () => Promise.resolve() });

test('a refresh asked for during a refresh runs once more after it (not dropped, not doubled)', async () => {
  const env = mkEnv();
  let release;
  env.next = () => new Promise((r) => { release = r; });
  const core = quietCore(env);
  const p1 = core.refreshCurrentFolderInPlace();
  const p2 = core.refreshCurrentFolderInPlace();
  const p3 = core.refreshCurrentFolderInPlace();
  assert.equal(p1, p2); assert.equal(p2, p3);                  // one run, shared
  assert.equal(env.runs.length, 1);
  release(); await tick();
  assert.equal(env.runs.length, 2);                            // once more, for the requests meanwhile
  release(); await p1;
  assert.equal(env.runs.length, 2);
  assert.deepEqual(env.runs[0], ['/m/a', { quiet: true }]);
});

test('no refresh when no folder is open, and none goes on after the user left the folders', async () => {
  const env = mkEnv();
  env.path = null;
  await quietCore(env).refreshCurrentFolderInPlace();
  assert.equal(env.runs.length, 0);
  const env2 = mkEnv();
  let release;
  env2.next = () => new Promise((r) => { release = r; });
  const core = quietCore(env2);
  const p = core.refreshCurrentFolderInPlace();
  core.refreshCurrentFolderInPlace();                          // (asks for one more)
  core.path = null;                                            // … but the user left for another view
  release(); await p;
  assert.equal(env2.runs.length, 1);
});

test('a request for a folder opened during a refresh runs for that folder', async () => {
  const env = mkEnv();
  let release;
  env.next = () => new Promise((r) => { release = r; });
  const core = quietCore(env);
  const p = core.refreshCurrentFolderInPlace();
  core.path = '/m/b';                                          // the user opened another folder …
  core.refreshCurrentFolderInPlace();                          // … and its scan finished meanwhile
  release(); await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(env.runs.map((r) => r[0]), ['/m/a', '/m/b']);
  release(); await p;
  assert.equal(env.runs.length, 2);
});

test('the scroll offset is put back only after a list was rendered anew at the top', async () => {
  const env = mkEnv();                                         // kept in place: scrollTop stays 500
  await quietCore(env).refreshCurrentFolderInPlace();
  assert.equal(env.frames.length, 0);
  const env2 = mkEnv();
  env2.next = () => { env2.wrap.scrollTop = 0; return Promise.resolve(); };   // a rebuild at the top
  await quietCore(env2).refreshCurrentFolderInPlace();
  assert.equal(env2.frames.length, 1);
  env2.frames[0]();
  assert.equal(env2.wrap.scrollTop, 500);
});

// ── the two quiet paths ──────────────────────────────────────────────────────
const fnBody = (sig, endMarker) => {
  const at = SRC.indexOf(sig);
  assert.ok(at >= 0, `${sig} not found`);
  return SRC.slice(at, SRC.indexOf(endMarker, at));
};

test('small folder: rows arrive with the live values; an unchanged re-read paints nothing, any other in place', () => {
  const fn = fnBody('async function showFolder(', '\nasync function _showFolderRecursiveWindowed(');
  assert.doesNotMatch(fn, /_sameTrackList\(rows, currentTracks\)\) return;/);   // the old early stop
  assert.match(fn, /const firstChunk = _keepLive\(/);
  const quiet = fn.indexOf('if (opts.quiet) {');
  assert.ok(quiet > 0, 'no quiet branch');
  const branch = fn.slice(quiet, fn.indexOf('renderTracks(rows, { keepScroll: !!opts.quiet });', quiet) + 60);
  assert.match(branch, /if \(_sameTrackList\(rows, currentTracks\) && rows\.every\(\(t, i\) => _sameRow\(t, currentTracks\[i\]\)\)\) return;/);
});

test('the live values are recorded where the page learns them and given to every folder chunk read', () => {
  const pt = fnBody('function patchTrack(id, fields) {', '\n}\n');
  assert.match(pt, /_recordLive\(id, fields\);/);
  const pd = fnBody('function patchTrackDuration(', '\n}\n');
  assert.ok(pd.indexOf('_recordLive(id, { duration: seconds });') > 0 && pd.indexOf('_recordLive(id, { duration: seconds });') < pd.indexOf('t.duration = seconds;'));
  const win = fnBody('async function _showFolderRecursiveWindowed(', '\nfunction getSelectedTracks(');
  assert.match(win, /const firstChunk = _keepLive\(/);
  assert.match(win, /return _keepLive\(j\.tracks\);/);
});

test('windowed folder: same size → read on-screen chunks, bail if superseded, re-check the window, compare, swap in place', () => {
  const fn = fnBody('async function _showFolderRecursiveWindowed(', '\nfunction getSelectedTracks(');
  assert.doesNotMatch(fn, /currentTracks\._total === total\) return;/);   // the old early stop
  const inPlace = fn.indexOf('if (inPlace) {');
  const awaitAt = fn.indexOf('await Promise.all([...store._pending.values()]);', inPlace);
  const bail = fn.indexOf('if (_gen !== _browseNavGen || currentTracks !== old) return;', inPlace);
  const rewin = fn.indexOf('[first, last] = win();', bail);
  const same = fn.indexOf('if (_sameChunks(store, old)) {', rewin);
  const render = fn.indexOf('renderTracks(store, { keepScroll: inPlace });', same);
  assert.ok(inPlace > 0 && awaitAt > inPlace && bail > awaitAt && rewin > bail && same > rewin && render > same,
    'order: read → bail when superseded → window again → compare → render in place');
  // the scroll + selection reset belongs to a rebuild only
  const reset = fn.indexOf('if (wrap) wrap.scrollTop = 0;');
  assert.ok(reset > fn.indexOf('} else {', same) && reset < render);
});
