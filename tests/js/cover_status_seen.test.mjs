// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// cover_status.js remembers (per browser, across page loads) the tracks
// without an embedded cover whose row showed one — their folder's image, an
// online cover — so on the next view the row asks its image at once instead
// of waiting for the status request first.
import { test } from 'node:test';
import assert from 'node:assert/strict';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
};
const calls = [];
globalThis.fetch = async (url, opts) => {
  calls.push(JSON.parse(opts.body).ids);
  return { ok: true, json: async () => ({ none: [] }) };
};
const URL_ = '../../soniqboom/frontend/js/cover_status.js';
const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms));

test('a track seen with a cover asks its image at once, also after a reload', async () => {
  const A = await import(`${URL_}?a`);
  A.noteCoverShown('f1');
  const asked = [];
  A.askCoverWhenUseful('f1', () => asked.push('f1'));
  assert.deepEqual(asked, ['f1']);                 // no status round trip
  await tick(1100);                                // (saved after a second)
  assert.deepEqual(JSON.parse(store.get('sb_cover_seen_v1')), ['f1']);
  const B = await import(`${URL_}?b`);             // a new page in the same browser
  const again = [];
  B.askCoverWhenUseful('f1', () => again.push('f1'));
  assert.deepEqual(again, ['f1']);
  assert.equal(calls.length, 0);
});

test('a cover that is gone is forgotten, and the row asks the status again', async () => {
  const C = await import(`${URL_}?c`);
  C.noteCoverShown('g1');
  C.noteCoverGone('g1');
  const asked = [];
  C.askCoverWhenUseful('g1', () => asked.push('g1'));
  assert.deepEqual(asked, []);                     // waits for the status request
  await tick();
  assert.deepEqual(calls.at(-1), ['g1']);
  assert.deepEqual(asked, ['g1']);
});

test('at most 4000 ids are kept, the last seen', async () => {
  const D = await import(`${URL_}?d`);
  for (let i = 0; i < 4100; i += 1) D.noteCoverShown(`x${i}`);
  await tick(1100);
  const kept = JSON.parse(store.get('sb_cover_seen_v1'));
  assert.equal(kept.length, 4000);
  assert.equal(kept[0], 'x100');
  assert.equal(kept.at(-1), 'x4099');
});

test('no storage (a private window): only this page remembers, nothing breaks', async () => {
  const saved = globalThis.localStorage;
  globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  try {
    const E = await import(`${URL_}?e`);
    E.noteCoverShown('p1');
    const asked = [];
    E.askCoverWhenUseful('p1', () => asked.push('p1'));
    assert.deepEqual(asked, ['p1']);
    await tick(1100);
  } finally { globalThis.localStorage = saved; }
});
