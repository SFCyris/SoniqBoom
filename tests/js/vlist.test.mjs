// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The pooled virtual list's geometry + pool planning (soniqboom/frontend/js/
// vlist.js, the ``[vlist-core:begin]`` / ``[vlist-core:end]`` region), run
// outside a browser.  What it guards: every row lands where the scroll offset
// says (no gaps, no overlaps), both spacers stay ≥ 0, the first and the LAST
// row are reachable on a list too tall for an element (Firefox's ~17.9 M px),
// and a scroll by a few rows reuses only the rows that left the window.
// Run: node --test tests/js/vlist.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/vlist.js'), 'utf8');

function core() {
  const m = SRC.match(/\/\/ \[vlist-core:begin\][^\n]*\n([\s\S]*?)\/\/ \[vlist-core:end\]/);
  if (!m) throw new Error('vlist-core region not found');
  const max = Number((SRC.match(/export const VL_MAX_PX = ([\d_]+);/) || [])[1].replace(/_/g, ''));
  return new Function(`const VL_MAX_PX = ${max};\n${m[1]}\nreturn { vlGeometry, vlToV, vlToS, vlWindow, vlReconcile, VL_MAX_PX };`)();
}
const { vlGeometry, vlToV, vlToS, vlWindow, vlReconcile, VL_MAX_PX } = core();

// Where row i is drawn, relative to the list's top, for window w.
const rowTop = (w, g, i) => w.top + (i - w.start) * g.rowH;

function checkWindow(g, s) {
  const w = vlWindow(g, s);
  assert.ok(w.top >= 0 && w.bottom >= 0, `spacers ≥ 0 at s=${s}: ${w.top} / ${w.bottom}`);
  assert.ok(Math.abs(w.top + (w.end - w.start) * g.rowH + w.bottom - g.total) < 1e-6,
    `spacers + rows = the list's height at s=${s}`);
  // Every row overlapping the viewport is rendered, at its own offset.
  const v = vlToV(g, s);
  for (let i = Math.max(0, Math.floor(v / g.rowH)); i * g.rowH < v + g.viewH && i < g.n; i++) {
    assert.ok(i >= w.start && i < w.end, `row ${i} visible at s=${s} but not rendered [${w.start}, ${w.end})`);
    // …drawn where the virtual offset puts it: (i·rowH − v) below the viewport top.
    assert.ok(Math.abs((rowTop(w, g, i) - s) - (i * g.rowH - v)) < 1e-6, `row ${i} misplaced at s=${s}`);
  }
  return w;
}

test('a list that fits is laid out 1:1', () => {
  const g = vlGeometry(50_000, 38, 900, 10);
  assert.equal(g.scaled, false);
  assert.equal(g.total, 50_000 * 38);
  for (const s of [0, 1, 37, 38, 39, 12_345.5, g.total - 900]) checkWindow(g, s);
  const w = vlWindow(g, 0);
  assert.equal(w.start, 0);
  assert.equal(w.top, 0);
  assert.equal(vlToV(g, 777), 777);
  assert.equal(vlToS(g, 777), 777);
});

test('a list taller than an element can be is capped and still reaches both ends', () => {
  const n = 500_000, rowH = 38, viewH = 860, buf = 10;
  assert.ok(n * rowH > 17_895_697, 'the case Firefox clips');
  const g = vlGeometry(n, rowH, viewH, buf);
  assert.equal(g.scaled, true);
  assert.equal(g.total, VL_MAX_PX);
  assert.ok(VL_MAX_PX < 17_895_697, 'the cap itself fits in Firefox');
  // Top: row 0 at the top.
  let w = checkWindow(g, 0);
  assert.equal(w.start, 0);
  assert.equal(rowTop(w, g, 0), 0);
  // Bottom: the scroll offset at the end shows the LAST row ending at the list's end.
  const sEnd = g.total - viewH;
  w = checkWindow(g, sEnd);
  assert.equal(w.end, n);
  assert.ok(Math.abs(rowTop(w, g, n - 1) + rowH - g.total) < 1e-6, 'last row ends at the bottom');
  assert.ok(Math.abs(rowTop(w, g, n - 1) - sEnd - (viewH - rowH)) < 1e-6, 'last row is the bottom row on screen');
  // Everywhere in between: spacers ≥ 0, rows where they belong (incl. the zone edges).
  const probes = [1, g.edge - 1, g.edge, g.edge + 1, 1e5, 7_500_000.25, sEnd - g.edge - 1,
    sEnd - g.edge, sEnd - g.edge + 1, sEnd - 1];
  for (let k = 0; k < 400; k++) probes.push((k / 400) * sEnd);
  for (const s of probes) checkWindow(g, s);
});

test('scaled mapping is continuous, monotonic and invertible', () => {
  const g = vlGeometry(1_000_000, 41, 700, 10);
  let prev = -1;
  for (let k = 0; k <= 2000; k++) {
    const s = (k / 2000) * g.sMax;
    const v = vlToV(g, s);
    assert.ok(v >= prev, 'monotonic');
    prev = v;
    assert.ok(Math.abs(vlToS(g, v) - s) < 1e-6, 'vlToS inverts vlToV');
  }
  for (const s of [g.edge, g.sMax - g.edge]) {
    assert.ok(Math.abs(vlToV(g, s - 1e-7) - vlToV(g, s + 1e-7)) < 1e-3, `continuous at ${s}`);
  }
  assert.equal(vlToV(g, 0), 0);
  assert.ok(Math.abs(vlToV(g, g.sMax) - g.vMax) < 1e-6);
});

test('a short or empty list', () => {
  const w0 = vlWindow(vlGeometry(0, 38, 900, 10), 0);
  assert.deepEqual(w0, { start: 0, end: 0, top: 0, bottom: 0 });
  const g = vlGeometry(5, 38, 900, 10);
  const w = vlWindow(g, 0);
  assert.equal(w.start, 0);
  assert.equal(w.end, 5);
  assert.equal(w.bottom, 0);
});

test('a scroll by a few rows reuses only the rows that left, nothing moves needlessly', () => {
  const have = Array.from({ length: 44 }, (_, k) => 100 + k);     // rows show 100…143
  // Down by 3: rows 100–102 leave, 144–146 enter.
  let p = vlReconcile(have, 103, 147);
  assert.deepEqual(p.rows.map(r => r.idx), Array.from({ length: 44 }, (_, k) => 103 + k));
  const kept = p.rows.filter(r => r.kept);
  assert.equal(kept.length, 41);
  kept.forEach(r => assert.equal(r.from, r.idx - 100), 'a kept row keeps its slot');
  assert.deepEqual(p.rows.filter(r => !r.kept).map(r => r.from), [0, 1, 2], 'the three that left are reused');
  assert.deepEqual(p.drop, []);
  // Up by 2: rows 142–143 leave, 98–99 enter; the bottom rows are reused.
  p = vlReconcile(have, 98, 142);
  assert.deepEqual(p.rows.slice(0, 2).map(r => [r.idx, r.from, r.kept]), [[98, 42, false], [99, 43, false]]);
  assert.ok(p.rows.slice(2).every(r => r.kept && r.from === r.idx - 100));
});

test('a jump reuses every row in order; a shrinking window drops rows; a growing one asks for new ones', () => {
  const have = Array.from({ length: 10 }, (_, k) => k);
  let p = vlReconcile(have, 5000, 5010);
  assert.deepEqual(p.rows.map(r => r.from), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 'no kept rows: reuse in DOM order');
  assert.ok(p.rows.every(r => !r.kept));
  p = vlReconcile(have, 2, 6);
  assert.deepEqual(p.rows.map(r => [r.idx, r.from]), [[2, 2], [3, 3], [4, 4], [5, 5]]);
  assert.deepEqual(p.drop.sort((a, b) => a - b), [0, 1, 6, 7, 8, 9]);
  p = vlReconcile(have, 0, 13);
  assert.deepEqual(p.rows.slice(10).map(r => r.from), [-1, -1, -1], 'new rows for the extra indexes');
  // A fresh pool (rows showing nothing yet).
  p = vlReconcile([undefined, undefined], 0, 3);
  assert.deepEqual(p.rows.map(r => r.from), [0, 1, -1]);
});

test('every index of the window appears exactly once, every slot at most once', () => {
  let seed = 7;
  const rnd = (n) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; };
  for (let k = 0; k < 500; k++) {
    const len = rnd(60);
    const s0 = rnd(1000);
    const have = Array.from({ length: len }, (_, j) => s0 + j);
    const ns = Math.max(0, s0 + rnd(120) - 60), ne = ns + rnd(70);
    const p = vlReconcile(have, ns, ne);
    assert.deepEqual(p.rows.map(r => r.idx), Array.from({ length: ne - ns }, (_, j) => ns + j));
    const slots = [...p.rows.map(r => r.from).filter(f => f >= 0), ...p.drop];
    assert.equal(new Set(slots).size, slots.length, 'no slot used twice');
    assert.equal(slots.length, Math.max(len, slots.length));
    assert.deepEqual([...slots].sort((a, b) => a - b), Array.from({ length: len }, (_, j) => j), 'every slot accounted for');
    for (const r of p.rows) if (r.kept) assert.equal(have[r.from], r.idx);
  }
});
