// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The track list keeps the user's place across an in-place refresh
// (soniqboom/frontend/js/library.js), run outside a browser:
//   * ``[list-place:begin]`` — the selection, the keyboard row, the shift-click
//     anchor and the rows on screen are carried over by TRACK ID: in the default
//     newest-first order new tracks at the top move every row down, so the same
//     indexes would point at other tracks;
//   * ``[windowed-store:begin]`` — the store's ``peek`` reads a loaded row
//     without fetching (used to read what the old list showed);
//   * ``[rating-keys:begin]`` — arrow keys on the stars step and clamp, they
//     never clear a rating at 1★ / 5★;
//   * ``[view-error:begin]`` — a view that failed says "didn't answer" only when
//     there was no answer, and names the server's error otherwise.
// Regions are lifted out by their markers and compiled with stubs.
// Run: node --test tests/js/list_place.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(process.env.SB_LIBRARY_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/library.js'), 'utf8');
const region = (name) => {
  const m = SRC.match(new RegExp(`// \\[${name}:begin\\][^\\n]*\\n([\\s\\S]*?)// \\[${name}:end\\]`));
  if (!m) throw new Error(`${name} region not found`);
  return m[1];
};

const core = new Function(`
  ${region('windowed-store')}
  ${region('list-place')}
  return { createWindowedStore, CHUNK_SIZE, _peekTrack, _idIndex, _captureListPlace, _planListPlace };
`)();

const rows = (ids) => ids.map((id) => ({ id, title: `T${id}` }));
const range = (a, b) => Array.from({ length: b - a }, (_, i) => a + i);

// The user's state on the old list, then the plan on the new one.
function carry(oldList, newList, state) {
  const place = core._captureListPlace(oldList, state);
  return core._planListPlace(place, core._idIndex(newList), newList.length);
}

test('newest-first: two tracks added at the top — selection, keyboard row and view move by 2', () => {
  const old = rows(range(100, 200));                    // ids 100…199
  const neu = rows([1, 2, ...range(100, 200)]);          // two new tracks first
  const plan = carry(old, neu, { selected: new Set([10, 11, 50]), focusIdx: 39, lastIdx: 11,
                                 first: { idx: 30, px: 7 } });
  assert.deepEqual(plan.selected.sort((a, b) => a - b), [12, 13, 52]);
  assert.equal(neu[plan.focus].id, old[39].id);
  assert.equal(plan.last, 13);
  assert.equal(plan.shift, 2);
  assert.deepEqual(plan.top, { idx: 32, px: 7 });        // the same rows at the top of the viewport
  assert.equal(neu[plan.top.idx].id, old[30].id);
});

test('nothing changed: the same indexes, no shift', () => {
  const old = rows(range(0, 50));
  const plan = carry(old, rows(range(0, 50)), { selected: new Set([3, 4]), focusIdx: 4, lastIdx: 3,
                                                first: { idx: 0, px: 0 } });
  assert.deepEqual(plan.selected, [3, 4]);
  assert.equal(plan.focus, 4);
  assert.equal(plan.shift, 0);
});

test('a selected track that is gone is dropped; one re-sorted far away does not drag the view', () => {
  const old = rows(range(0, 60));
  // id 5 removed, id 20 moved to the end (re-tagged under a title sort)
  const ids = range(0, 60).filter((i) => i !== 5 && i !== 20);
  const neu = rows([...ids, 20]);
  const plan = carry(old, neu, { selected: new Set([5, 20, 30]), focusIdx: -1, lastIdx: -1,
                                 first: { idx: 10, px: 0 } });
  assert.deepEqual(plan.selected.map((j) => neu[j].id).sort((a, b) => a - b), [20, 30]);
  assert.equal(plan.focus, -1);
  // rows 10…49 on screen: the first visible track (id 10) moved up by one (id 5
  // left above it) — the view follows it; id 20, which jumped to the end, is
  // not taken as the anchor
  assert.equal(plan.shift, -1);
  assert.equal(neu[plan.top.idx].id, 10);
  // …and when the topmost visible track is the one that jumped away, the next
  // one anchors the view: id 21 stays in the second row
  const plan2 = carry(old, neu, { selected: new Set(), focusIdx: -1, lastIdx: -1, first: { idx: 20, px: 0 } });
  assert.equal(plan2.shift, -2);
  assert.equal(neu[plan2.top.idx + 1].id, 21);
});

test('the keyboard row whose track went away stays at its place on screen', () => {
  const old = rows(range(0, 30));
  const neu = rows([100, ...range(0, 30).filter((i) => i !== 12)]);   // one added above, row 12's track gone
  const plan = carry(old, neu, { selected: new Set(), focusIdx: 12, lastIdx: -1, first: { idx: 0, px: 0 } });
  assert.equal(plan.shift, 1);
  assert.equal(plan.focus, 13);                          // old place + the shift
});

test('windowed store: peek reads loaded rows only and never fetches', async () => {
  const asked = [];
  const store = core.createWindowedStore(5000, (off, lim) => { asked.push(off); return Promise.resolve(rows(range(off, off + lim))); });
  store.ensureRange(0, 10);
  await Promise.all([...store._pending.values()]);
  assert.deepEqual(asked, [0]);
  assert.equal(store.peek(5).id, 5);
  assert.equal(store.peek(core.CHUNK_SIZE + 3), undefined);   // chunk 1 not loaded…
  assert.deepEqual(asked, [0]);                               // …and not fetched by peek
  assert.equal(core._peekTrack(store, 7).id, 7);
  assert.equal(core._idIndex(store).get(1999), 1999);
  assert.equal(core._idIndex(store).has(2000), false);
});

test('windowed store refreshed with a track added at the top: the place moves by one', async () => {
  const mk = (ids) => core.createWindowedStore(ids.length, (off, lim) => Promise.resolve(rows(ids.slice(off, off + lim))));
  const oldIds = range(1000, 7000), newIds = [1, ...oldIds];
  const old = mk(oldIds), neu = mk(newIds);
  old.ensureRange(1180, 1220); neu.ensureRange(1180, 1220);
  await Promise.all([...old._pending.values(), ...neu._pending.values()]);
  const plan = carry(old, neu, { selected: new Set([1190, 1191]), focusIdx: 1200, lastIdx: 1191,
                                 first: { idx: 1185, px: 12 } });
  assert.deepEqual(plan.selected, [1191, 1192]);
  assert.equal(plan.focus, 1201);
  assert.equal(plan.shift, 1);
  assert.deepEqual(plan.top, { idx: 1186, px: 12 });
});

// ── keyboard stars ─────────────────────────────────────────────────────────────
const stars = new Function(`${region('rating-keys')}\nreturn { _ratingForKey, _RATING_STEP_KEYS };`)();

test('arrow keys step the rating and stay put at either end (never clear it)', () => {
  const f = stars._ratingForKey;
  assert.equal(f(5, 'ArrowRight'), 5);
  assert.equal(f(5, 'ArrowUp'), 5);
  assert.equal(f(1, 'ArrowLeft'), 1);
  assert.equal(f(1, 'ArrowDown'), 1);
  assert.equal(f(3, 'ArrowRight'), 4);
  assert.equal(f(3, 'ArrowLeft'), 2);
  assert.equal(f(0, 'ArrowRight'), 1);
  assert.equal(f(1, 'Home'), 1);
  assert.equal(f(5, 'End'), 5);
  assert.ok(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].every((k) => stars._RATING_STEP_KEYS.has(k)));
});

test('the star key handler leaves an unchanged rating alone (no toggle-off)', () => {
  const fn = SRC.slice(SRC.indexOf("tbody.addEventListener('keydown'"), SRC.indexOf("tbody.addEventListener('keydown'") + 1500);
  assert.match(fn, /const to = _ratingForKey\(cur, e\.key\);\s*if \(to !== cur\) _setRowRating/);
});

// ── a view that failed to load ──────────────────────────────────────────────────
const verr = new Function(`${region('view-error')}\nreturn { _viewErrorText };`)();

test('no answer vs an error answer are worded apart', () => {
  assert.equal(verr._viewErrorText(new TypeError('Failed to fetch')), 'The server didn’t answer.');
  assert.equal(verr._viewErrorText(null), 'The server didn’t answer.');
  assert.match(verr._viewErrorText(new Error('HTTP 500')), /problem \(error 500\)/);
  assert.match(verr._viewErrorText(new Error('HTTP 503')), /busy or restarting/);
  assert.match(verr._viewErrorText(new Error('HTTP 404')), /refused the request \(error 404\)/);
  assert.doesNotMatch(verr._viewErrorText(new Error('HTTP 502')), /didn’t answer/);
});

// ── wiring: renderTracks / the windowed install / the context menu ─────────────
test('the in-place render reads the place before it replaces the rows, and restores it', () => {
  const fn = SRC.slice(SRC.indexOf('async function renderTracks('), SRC.indexOf('// The load-error message of a view that is gone'));
  const read = fn.indexOf('_currentListPlace()');
  assert.ok(read > 0 && read < fn.indexOf('currentTracks = tracks'));
  assert.match(fn, /_planListPlace\(place, _idIndex\(tracks\), tracks\.length\)/);
  const inst = SRC.slice(SRC.indexOf('function _installWindowedStore('), SRC.indexOf('function _installWindowedStore(') + 600);
  assert.match(inst, /if \(!keepScroll\) \{\s*_selected\.clear\(\);/);
});

test('the context menu acts on the tracks it was opened on', () => {
  const fn = SRC.slice(SRC.indexOf('function _showTrackContextMenu('), SRC.indexOf('// Persist a rating with optimistic UI'));
  assert.doesNotMatch(fn, /playFrom\(i\)/);
  assert.doesNotMatch(fn, /_infoCallback\(currentTracks, i\)/);
  assert.match(fn, /showAddDropdownForEntries\(el, targets\.map/);
});
