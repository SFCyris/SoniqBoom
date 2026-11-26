// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What an edit of the open playlist keeps (soniqboom/frontend/js/playlist/
// edit_view.js): the selected rows, found again by entry after the re-read; a
// new playlist made from "Add to playlist" starts with its entries, a picked
// tune kept as {id, subsong}.  And the wiring in playlist.js: a re-read after
// an edit no longer scrolls to the top or drops the selection.
// Run: node --test tests/js/playlist_edit_view.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const { entryKey, newPlaylistEntries, remapSelection, remapIndex, movedSelection } =
  await import(resolve(FE, 'js/playlist/edit_view.js'));

const T = (id, subsong) => (subsong === undefined ? { id } : { id, subsong });

test('a new playlist keeps bare ids and picked tunes, in order, repeats too', () => {
  assert.deepEqual(newPlaylistEntries(['a', { id: 'b', subsong: 2 }, 'a', { id: 'c' }, { id: 'd', subsong: 0 }]),
    ['a', { id: 'b', subsong: 2 }, 'a', 'c', { id: 'd', subsong: 0 }]);
  assert.deepEqual(newPlaylistEntries([null, '', { subsong: 1 }, { id: 'x', subsong: -1 }, { id: 'y', subsong: 1.5 }, 7]),
    ['x', 'y']);
  assert.deepEqual(newPlaylistEntries(null), []);
  assert.deepEqual(newPlaylistEntries(undefined), []);
});

test('entry keys: the track and its tune', () => {
  assert.equal(entryKey(T('a')), 'a');
  assert.equal(entryKey(T('a', 0)), 'a#0');
  assert.equal(entryKey(T('a', 3)), 'a#3');
  assert.equal(entryKey('a'), 'a');
  assert.notEqual(entryKey(T('a', 1)), entryKey(T('a', 2)));
});

test('a re-read with the same entries keeps the same rows selected', () => {
  const old = [T('a'), T('b'), T('c'), T('d')];
  const fresh = old.map((t) => ({ ...t }));          // (new objects from the server)
  assert.deepEqual([...remapSelection(old, new Set([1, 3]), fresh)].sort(), [1, 3]);
});

test('after a remove the other selected rows stay selected at their new places', () => {
  const old = [T('a'), T('b'), T('c'), T('d'), T('e')];
  const after = [T('a'), T('c'), T('d'), T('e')];       // b removed
  assert.deepEqual([...remapSelection(old, new Set([1, 2, 4]), after)].sort(), [1, 3]);
});

test('a track in the playlist twice: the second copy stays the second copy', () => {
  const old = [T('a'), T('x'), T('b'), T('x')];
  const after = [T('x'), T('a'), T('b'), T('x')];
  assert.deepEqual([...remapSelection(old, new Set([3]), after)], [3]);
  assert.deepEqual([...remapSelection(old, new Set([1]), after)], [0]);
  assert.equal(remapIndex(old, 3, after), 3);
  assert.equal(remapIndex(old, 1, after), 0);
});

test('two tunes of one file are two entries', () => {
  const old = [T('f', 0), T('f', 1), T('g')];
  const after = [T('g'), T('f', 1), T('f', 0)];
  assert.deepEqual([...remapSelection(old, new Set([1]), after)], [1]);
  assert.deepEqual([...remapSelection(old, new Set([0]), after)], [2]);
});

test('a selected row whose entry is gone is no longer selected; bad anchors are null', () => {
  assert.equal(remapSelection([T('a'), T('b')], new Set([1]), [T('a')]).size, 0);
  assert.equal(remapIndex([T('a')], null, [T('a')]), null);
  assert.equal(remapIndex([T('a')], 5, [T('a')]), null);
  assert.equal(remapSelection([T('a')], new Set(), [T('a')]).size, 0);
});

test('a drag: the selection follows its rows (dragged or not), a twice-listed track stays the copy it was', () => {
  const a = T('a'), x1 = T('x'), b = T('b'), x2 = T('x'), c = T('c');
  const before = [a, x1, b, x2, c];
  const after = [b, a, x1, c, x2];                     // (a and x1 dragged below b; c above x2)
  assert.deepEqual([...movedSelection(before, after, new Set([0, 1]))].sort(), [1, 2]);
  assert.deepEqual([...movedSelection(before, after, new Set([3]))], [4]);     // the second x stays the second
  assert.deepEqual([...movedSelection(before, after, new Set([2, 4]))].sort(), [0, 3]);   // not dragged: followed too
  assert.equal(movedSelection(before, after, new Set()).size, 0);
});

// The wiring: the re-read after an edit keeps the view; "+ New playlist…" passes its entries.
const PL = readFileSync(resolve(FE, 'js/playlist.js'), 'utf8');
test('playlist.js: the re-read after an edit keeps the view; a playlist opened anew starts at the top', () => {
  assert.match(PL, /_openPlaylist\(_activeId, pl\.name, false, \{ keepView: true \}\)/);
  const op = PL.slice(PL.indexOf('async function _openPlaylist('), PL.indexOf('// Which tracks can play changed'));
  assert.match(op, /if \(!keepWanted\) _clearSelection\(\);/);
  // (scroll and selection read after the load: what the listener did meanwhile counts)
  assert.ok(op.indexOf("const keep = keepWanted") > op.indexOf("if (_activeId !== id) return;"));
  assert.match(op, /if \(keep\) \{[\s\S]*remapSelection\(keep\.tracks, keep\.sel, _activeTracks\)[\s\S]*\} else \{\s*_renderTracks\(\{ toTop: true \}\);/);
});
test('playlist.js: both "new playlist" items of the Add menu create it WITH the entries', () => {
  const dd = PL.slice(PL.indexOf('function _showAddDropdownWith('), PL.indexOf('function _handleOutsideClick('));
  const calls = [...dd.matchAll(/createPlaylist\((.*?)\);/g)].map((m) => m[1]);
  assert.equal(calls.length, 2);
  for (const c of calls) assert.equal(c, 'getEntries(), onDone');
  const cp = PL.slice(PL.indexOf('async function createPlaylist('), PL.indexOf('// ── Drop zone'));
  assert.match(cp, /track_ids: trackIds/);
  assert.match(cp, /newPlaylistEntries\(entries\)/);
});
