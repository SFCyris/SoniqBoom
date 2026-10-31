// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

// The queue panel (js/queue.js): the header's time formats it shares with the
// playlist view (js/playlist/hero.js), the list helpers both use
// (js/playlist/listkit.js), and the markup it needs in index.html.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');
const { fmtTotal, counterDigits } = await import(resolve(FE, 'js/playlist/hero.js'));
const { moveTarget, aheadOf, fmtDur, esc, NARROW_WAVES } = await import(resolve(FE, 'js/playlist/listkit.js'));

test('time left: words and the tape counter', () => {
  assert.equal(fmtTotal(42), '42 s');
  assert.equal(fmtTotal(43 * 60 + 10), '43 min');
  assert.equal(fmtTotal(5 * 3600 + 48 * 60 + 12), '5 h 48 min');
  assert.equal(counterDigits(5 * 3600 + 48 * 60 + 12), '5:48:12');
  assert.equal(counterDigits(59.6), '0:01:00');
  assert.equal(counterDigits(-3), '0:00:00');
});

test('every element queue.js looks up is in the page', () => {
  const js = read('js/queue.js');
  const html = read('index.html');
  const ids = [...new Set([...js.matchAll(/getElementById\('([\w-]+)'\)/g)].map((m) => m[1]))];
  assert.ok(ids.length >= 15, `only ${ids.length} ids found`);
  const missing = ids.filter((id) => !html.includes(`id="${id}"`));
  assert.deepEqual(missing, []);
});

test('the queue rows are a virtual list; its stylesheet is linked once', () => {
  const js = read('js/queue.js');
  assert.match(js, /const _vl = createVirtualList\(\{\s*scroller: listEl, host: rowsEl,/);
  const html = read('index.html');
  assert.equal((html.match(/\/assets\/css\/queue-styles\.css\?v=\d+/g) || []).length, 1);
  // after the other looks' sheets (it reuses the playlist view's pieces)
  assert.ok(html.indexOf('playlist-styles.css') < html.indexOf('queue-styles.css'));
});

test('a row dropped into a gap: its new index, or no move', () => {
  // gaps are 0..count: before row 0 … after the last row
  assert.equal(moveTarget(1, 5), 4, 'down: below row 4');
  assert.equal(moveTarget(7, 2), 2, 'up: above row 2');
  assert.equal(moveTarget(3, 3), -1, 'just above itself');
  assert.equal(moveTarget(3, 4), -1, 'just below itself');
  assert.equal(moveTarget(0, 10), 9, 'to the end of ten');
  assert.equal(moveTarget(9, 0), 0, 'to the top');
  assert.equal(moveTarget(-1, 2), -1, 'nothing dragged');
  assert.equal(moveTarget(2, -1), -1, 'no gap');
});

test('what is ahead: after the current entry, passed-by entries left out', () => {
  const q = [{ duration: 100 }, { duration: 60 }, { duration: 0 }, { duration: 30, skip: true }, { duration: 45 }];
  const skips = (t) => !!t.skip;
  assert.deepEqual(aheadOf(q, 0, skips), { n: 3, sec: 105, unknown: 1 });
  assert.deepEqual(aheadOf(q, 4, skips), { n: 0, sec: 0, unknown: 0 }, 'the last one');
  assert.deepEqual(aheadOf(q, -1), { n: 5, sec: 235, unknown: 1 }, 'nothing current: all of it');
  assert.deepEqual(aheadOf([], 0), { n: 0, sec: 0, unknown: 0 });
});

test('row text helpers', () => {
  assert.equal(fmtDur(0), '');
  assert.equal(fmtDur(187.9), '3:07');
  assert.equal(esc('<a href="x">&</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
});

test('the waveform painter watches the same breakpoint that resizes the waveform', () => {
  const css = read('css/playlist-styles.css');
  const block = css.split(`@media ${NARROW_WAVES}`)[1] || '';
  assert.match(block.slice(0, 400), /\.plx-wave-wrap \{ width: \d+px; \}/);
});
