// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Seeking by the player bar's waveform (soniqboom/frontend/js/player/
// wave_seek.js): anywhere in its rectangle, by x only; a drag scrubs and the
// release seeks.  (The pixel check — top edge, middle, bottom edge seek to the
// same place under a real mouse — is the Playwright run in the handover.)
// Run: node --test tests/js/wave_seek.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const { pctAtX, bindWaveSeek } = await import(resolve(FE, 'js/player/wave_seek.js'));

test('x across the box → 0–100, clamped; a box with no width → null', () => {
  assert.equal(pctAtX(100, 100, 400), 0);
  assert.equal(pctAtX(300, 100, 400), 50);
  assert.equal(pctAtX(500, 100, 400), 100);
  assert.equal(pctAtX(50, 100, 400), 0);
  assert.equal(pctAtX(900, 100, 400), 100);
  assert.equal(pctAtX(200, 100, 0), null);
  assert.equal(pctAtX(NaN, 100, 400), null);
});

function fakeEl(rect) {
  const on = {};
  const el = {
    captured: null,
    getBoundingClientRect: () => rect,
    addEventListener: (k, fn) => { (on[k] ||= []).push(fn); },
    setPointerCapture(id) { this.captured = id; },
    releasePointerCapture(id) { if (this.captured === id) this.captured = null; },
    fire(k, e) { for (const fn of on[k] || []) fn({ preventDefault() { this.prevented = true; }, button: 0, pointerId: 1, ...e }); },
  };
  return el;
}

test('a press at the top edge, the middle and the bottom edge seeks the same place (y is not read)', () => {
  const rect = { left: 200, top: 700, width: 600, height: 44 };
  for (const y of [702, 722, 742]) {
    const el = fakeEl(rect);
    const seeks = [];
    bindWaveSeek(el, { commit: (p) => seeks.push(p) });
    el.fire('pointerdown', { clientX: 350, clientY: y });
    el.fire('pointerup', { clientX: 350, clientY: y });
    assert.deepEqual(seeks, [25], `y=${y}`);
  }
});

test('a drag scrubs (paint per move, one seek on release, at the release x)', () => {
  const el = fakeEl({ left: 0, top: 0, width: 200, height: 44 });
  const painted = [], seeks = [];
  const ws = bindWaveSeek(el, { paint: (p) => painted.push(p), commit: (p) => seeks.push(p) });
  el.fire('pointerdown', { clientX: 20 });
  assert.equal(ws.active(), true);
  assert.equal(el.captured, 1);
  el.fire('pointermove', { clientX: 60 });
  el.fire('pointermove', { clientX: 250 });          // (past the end: clamped)
  el.fire('pointermove', { clientX: 100, pointerId: 2 });   // another pointer: ignored
  el.fire('pointerup', { clientX: 150 });
  assert.deepEqual(painted, [10, 30, 100]);
  assert.deepEqual(seeks, [75]);
  assert.equal(ws.active(), false);
  assert.equal(el.captured, null);
});

test('a lost press seeks nothing and puts the display back; a right click does nothing', () => {
  const el = fakeEl({ left: 0, top: 0, width: 100, height: 44 });
  const seeks = []; let cancelled = 0;
  bindWaveSeek(el, { commit: (p) => seeks.push(p), cancel: () => cancelled++ });
  el.fire('pointerdown', { clientX: 40 });
  el.fire('pointercancel', {});
  assert.deepEqual(seeks, []);
  assert.equal(cancelled, 1);
  el.fire('pointerdown', { clientX: 40, button: 2 });
  el.fire('pointerup', { clientX: 40, button: 2 });
  assert.deepEqual(seeks, []);
});
