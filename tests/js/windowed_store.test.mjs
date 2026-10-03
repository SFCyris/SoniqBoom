// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The library's windowed track store (soniqboom/frontend/js/library.js, the
// ``[windowed-store:begin]`` / ``[windowed-store:end]`` region) run outside a
// browser with a fake clock: a chunk whose fetch fails is asked again after a
// pause (not on every frame), its rows report "failed" once the automatic
// attempts are used up, Retry asks again at once, an error body is never stored
// as a chunk, and a chunk that loads clears its failure.
// Run: node --test tests/js/windowed_store.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/library.js'), 'utf8');

function load() {
  const m = SRC.match(/\/\/ \[windowed-store:begin\][^\n]*\n([\s\S]*?)\/\/ \[windowed-store:end\]/);
  if (!m) throw new Error('windowed-store region not found');
  const clock = { now: 0, timers: [] };
  const fakeDate = { now: () => clock.now };
  const setT = (fn, ms) => { const t = { at: clock.now + ms, fn, live: true }; clock.timers.push(t); return t; };
  const clearT = (t) => { if (t) t.live = false; };
  const make = new Function('Date', 'setTimeout', 'clearTimeout',
    `${m[1]}\nreturn { createWindowedStore, CHUNK_SIZE, CHUNK_RETRY_MS };`);
  const mod = make(fakeDate, setT, clearT);
  // Advance the fake clock, firing due timers in order.
  mod.advance = async (ms) => {
    const until = clock.now + ms;
    for (;;) {
      const due = clock.timers.filter(t => t.live && t.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      clock.now = due.at; due.live = false; due.fn();
      await flush();
    }
    clock.now = until;
  };
  return mod;
}
const flush = () => new Promise((r) => setImmediate(r));

test('a failing chunk backs off, then reports failed rows; Retry asks again at once', async () => {
  const { createWindowedStore, CHUNK_SIZE, CHUNK_RETRY_MS, advance } = load();
  assert.ok(CHUNK_RETRY_MS.length >= 2);
  let fail = true;
  const calls = [];
  const fetcher = (offset, limit) => {
    calls.push(offset);
    return fail ? Promise.reject(new Error('HTTP 500'))
                : Promise.resolve(Array.from({ length: limit }, (_, k) => ({ id: `t${offset + k}` })));
  };
  const store = createWindowedStore(3 * CHUNK_SIZE, fetcher);
  let repaints = 0;
  store.setOnChunkLoad(() => { repaints++; });
  const i = CHUNK_SIZE + 50;                      // a row of chunk 1
  assert.equal(store[i], undefined);              // first touch fetches
  await flush();
  assert.deepEqual(calls, [CHUNK_SIZE]);
  assert.equal(store.isFailed(i), false, 'still retrying: the row keeps loading');
  // The view re-reads the row every frame — no new request while backing off.
  for (let k = 0; k < 30; k++) { void store[i]; store.ensureRange(i - 10, i + 10); }
  await flush();
  assert.equal(calls.length, 1, 'no request storm while backing off');
  // After each pause a repaint lets the visible rows ask again.
  for (let attempt = 1; attempt < CHUNK_RETRY_MS.length; attempt++) {
    await advance(CHUNK_RETRY_MS[attempt - 1]);
    assert.equal(repaints, attempt, `repaint after pause ${attempt}`);
    void store[i];
    await flush();
    assert.equal(calls.length, attempt + 1);
  }
  assert.equal(store.isFailed(i), true, 'out of automatic attempts: the rows say so');
  assert.equal(store.isFailed(i + CHUNK_SIZE), false, 'other chunks are not affected');
  assert.equal(repaints, CHUNK_RETRY_MS.length, 'the failure is repainted');
  // Still backing off: rows on screen don't hammer the server.
  void store[i]; await flush();
  assert.equal(calls.length, CHUNK_RETRY_MS.length);
  // Retry: asks again immediately; while the request runs the rows load again.
  fail = false;
  store.retryFailed();
  assert.equal(store[i], undefined);
  assert.equal(store.isFailed(i), false, 'loading again, not failed');
  await flush();
  assert.equal(calls.length, CHUNK_RETRY_MS.length + 1);
  assert.deepEqual(store[i], { id: `t${i}` });
  assert.equal(store.isFailed(i), false);
});

test('a scroll back after the last pause asks once more by itself', async () => {
  const { createWindowedStore, CHUNK_SIZE, CHUNK_RETRY_MS, advance } = load();
  let calls = 0;
  const store = createWindowedStore(2 * CHUNK_SIZE, () => { calls++; return Promise.reject(new Error('x')); });
  store.setOnChunkLoad(() => {});
  for (let k = 0; k < CHUNK_RETRY_MS.length; k++) {
    void store[0]; await flush();
    await advance(CHUNK_RETRY_MS[k] - 1);
    void store[0]; await flush();
    await advance(1);
  }
  const before = calls;
  void store[0]; await flush();
  assert.equal(calls, before + 1, 'after the longest pause the row asks again');
});

test('an error body is a failure, not a chunk', async () => {
  const { createWindowedStore, CHUNK_SIZE } = load();
  const store = createWindowedStore(CHUNK_SIZE, () => Promise.resolve({ detail: 'Internal Server Error' }));
  store.setOnChunkLoad(() => {});
  void store[3];
  await flush();
  assert.equal(store._chunks.size, 0, 'nothing stored');
  assert.equal(store[3], undefined);
});

test('invalidate() drops failures and their timers', async () => {
  const { createWindowedStore, CHUNK_SIZE, CHUNK_RETRY_MS, advance } = load();
  let repaints = 0;
  const store = createWindowedStore(CHUNK_SIZE, () => Promise.reject(new Error('x')));
  store.setOnChunkLoad(() => { repaints++; });
  void store[0];
  await flush();
  store.invalidate();
  await advance(CHUNK_RETRY_MS[0] * 2);
  assert.equal(repaints, 0, 'no repaint from a timer of an invalidated store');
  assert.equal(store.isFailed(0), false);
});
