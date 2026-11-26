// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Performance guards for the phone's seek gesture and radio recovery (the
// owner: "any negative impact should be not noticeable by the user").  The
// pieces that run per pointer move and per media event
// must stay far below one frame (16.7 ms): the budgets here are about 100×
// above what they cost on a laptop (measured 2026-11-25: ~0.3 µs per move,
// ~0.1 µs per media event), so they fail only on a real regression — an
// O(n) scan, a layout read per move, a timer storm — not on a busy machine.
// Run: node --test tests/js/m_phone_perf.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { attachSeekGesture } from '../../soniqboom/frontend/js/mobile/seek_gesture.js';
import { createStreamRecovery } from '../../soniqboom/frontend/js/stream_recovery.js';

function surface() {
  const l = {};
  let rectReads = 0;
  return {
    addEventListener: (t, f) => { (l[t] ||= []).push(f); },
    removeEventListener() {},
    fire: (t, e) => { for (const f of l[t] || []) f({ type: t, ...e }); },
    getBoundingClientRect: () => { rectReads++; return { left: 0, width: 400 }; },
    setPointerCapture() {}, hasPointerCapture: () => false, releasePointerCapture() {},
    get rectReads() { return rectReads; },
  };
}

test('a drag of 20,000 moves costs well under a frame per move, one rect read each', () => {
  const s = surface();
  let previews = 0;
  attachSeekGesture(s, { preview: () => { previews++; }, commit() {}, cancel() {}, win: { addEventListener() {} } });
  s.fire('pointerdown', { pointerId: 1, clientX: 10, pointerType: 'touch', button: 0 });
  const N = 20000;
  const t0 = performance.now();
  for (let i = 0; i < N; i++) s.fire('pointermove', { pointerId: 1, clientX: i % 400 });
  const per = (performance.now() - t0) / N;
  s.fire('pointerup', { pointerId: 1, clientX: 200 });
  assert.equal(previews, N + 1);
  assert.equal(s.rectReads, N + 2, 'exactly one layout read per event (no extra reads)');
  assert.ok(per < 0.05, `${per.toFixed(4)} ms per move (budget 0.05 ms)`);
});

test('media events of the recovery cost microseconds; timers do not pile up', () => {
  let live = 0;
  const pending = new Set();
  let id = 0;
  const timers = {
    setTimeout: () => { const k = ++id; pending.add(k); return k; }, clearTimeout: (k) => pending.delete(k),
    setInterval: () => { const k = ++id; pending.add(k); return k; }, clearInterval: (k) => pending.delete(k),
  };
  const r = createStreamRecovery({ timers, now: () => 0, reconnect() {}, giveUp() {}, status: () => { live++; } });
  const a = { paused: false, ended: false, error: null, readyState: 4, currentTime: 0,
              buffered: { length: 0 }, play: () => Promise.resolve(), pause() {} };
  r.watch(a);
  const N = 50000;
  const t0 = performance.now();
  for (let i = 0; i < N; i++) { r.handle('playing', a); r.handle('waiting', a); }
  const per = (performance.now() - t0) / (2 * N);
  assert.ok(per < 0.02, `${per.toFixed(5)} ms per media event (budget 0.02 ms)`);
  // Every 'playing' replaces its timers instead of adding: a handful stay, not 100,000.
  assert.ok(pending.size <= 4, `${pending.size} timers pending`);
  assert.ok(live <= 2, 'the status is told on a change only');
});
