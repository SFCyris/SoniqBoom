// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The composer page and the compo winners' view (library.js), run outside a
// browser — the regions are lifted out by their markers:
//   * ``[composer-groups:begin]`` — composerRows marks the first tune of
//     each platform (the list's group separators) in the server's platform
//     order, narrows to one platform, and an unknown format falls into
//     "Other formats"; placeText writes Demozoo's rankings as places;
//   * ``[winners-query:begin]`` — winnersQuery builds the compo winners'
//     search from the keys chosen (place, chip, decade, competition) with
//     the search box's operators.
// And the source: every view entry of the new views takes the nav token,
// the panel's declarations come before any function that uses them runs,
// and the Scene keys include Chips and Discover.
// Run: node --test tests/js/composer_page.test.mjs
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
const groups = new Function(`${region('composer-groups')}\nreturn { composerRows, placeText };`)();
const { winnersQuery } = new Function(`${region('winners-query')}\nreturn { winnersQuery };`)();

const FP = { SID: 'SID', ProTracker: 'Amiga', TFMX: 'Amiga', SNDH: 'Atari ST' };
const rows = () => [
  { id: 'a', format: 'SID' }, { id: 'b', format: 'SID' }, { id: 'c', format: 'ProTracker' },
  { id: 'd', format: 'TFMX' }, { id: 'e', format: 'SNDH' }, { id: 'f', format: 'MP3' },
];

test('the first tune of each platform starts its group', () => {
  const out = groups.composerRows(rows(), FP);
  assert.deepEqual(out.map((t) => [t.id, t._platformFirst]),
    [['a', true], ['b', false], ['c', true], ['d', false], ['e', true], ['f', true]]);
});

test('one platform: only its tunes, the first starting the group', () => {
  const out = groups.composerRows(rows(), FP, 'Amiga');
  assert.deepEqual(out.map((t) => [t.id, t._platformFirst]), [['c', true], ['d', false]]);
  // (a format the page names no platform for: "Other formats")
  assert.deepEqual(groups.composerRows(rows(), FP, 'Other formats').map((t) => t.id), ['f']);
  assert.deepEqual(groups.composerRows(rows(), null).map((t) => t._platformFirst),
    [true, false, false, false, false, false]);
});

test('a ranking reads as a place', () => {
  assert.equal(groups.placeText('1'), '1st');
  assert.equal(groups.placeText('2'), '2nd');
  assert.equal(groups.placeText('3'), '3rd');
  assert.equal(groups.placeText('4'), '4th');
  assert.equal(groups.placeText('11'), '11th');
  assert.equal(groups.placeText('12'), '12th');
  assert.equal(groups.placeText('13'), '13th');
  assert.equal(groups.placeText('21'), '21st');
  assert.equal(groups.placeText('=2'), '=2nd');
  assert.equal(groups.placeText('3-4'), '3rd–4th');
  assert.equal(groups.placeText('DQ'), 'DQ');
  assert.equal(groups.placeText(''), '');
  assert.equal(groups.placeText(null), '');
});

test('the compo winners search, key by key', () => {
  assert.equal(winnersQuery({ placeMax: 1 }), 'place:<=1');
  assert.equal(winnersQuery({ placeMax: 3, chip: 'paula' }), 'place:<=3 chip:paula');
  assert.equal(winnersQuery({ placeMax: 1, decade: 1990 }), 'place:<=1 year:1990-1999');
  assert.equal(winnersQuery({ placeMax: 1, compo: 'Amiga "Music"' }), 'place:<=1 compo:"Amiga  Music"');
  assert.equal(winnersQuery({}), 'place:<=1');
  assert.equal(winnersQuery({ placeMax: 0 }), 'place:<=1');
});

test('every new view entry takes the nav token first', () => {
  for (const fn of ['showComposer', '_showChipTracks', '_showWinners', '_showOnThisDay',
                    '_showUpcoming', '_showSeriesTracks']) {
    const m = SRC.match(new RegExp(`async function ${fn}\\([^)]*\\) \\{\\n([\\s\\S]*?)\\n\\}`));
    assert.ok(m, fn);
    const body = m[1];
    const first = body.indexOf('++_browseNavGen');
    assert.ok(first >= 0, `${fn} takes no nav token`);
    assert.ok(first < body.indexOf('await'), `${fn} awaits before it takes the token`);
    // (bails when a newer view was asked for)
    assert.match(body, /_gen !== _browseNavGen\) return/, fn);
    // (the stale-view flags cleared at entry: restoreFullHeader / setGroupHeader)
    assert.match(body.slice(0, body.indexOf('await')), /restoreFullHeader\(\)|setGroupHeader\(/, fn);
  }
});

test("the panel's state is declared before the functions that use it run", () => {
  const decl = SRC.indexOf("const scenePanel        = document.getElementById('scene-panel');");
  assert.ok(decl > 0);
  for (const use of ['async function renderTracks(', 'function hideBrowseHeader()',
                     'function setGroupHeader(', 'function restoreFullHeader(']) {
    assert.ok(SRC.indexOf(use) > decl, `${use} before the panel's declarations`);
  }
});

test('the Scene keys hold Chips and Discover (Discover always shown)', () => {
  assert.match(SRC, /chip: +\{ url: 'chips', +count: 'chips'/);
  assert.match(SRC, /discover: +\{ url: 'discover', +count: null,[^}]*always: true/);
  assert.match(SRC, /!\(n > 0\) && !f\.always\) continue;/);
});
