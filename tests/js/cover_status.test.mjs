// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// cover_status.js: list rows of tracks without an embedded cover ask "which of
// these have any cover?" in ONE request per paint; a track the server knows to
// have none never asks its image; art_ready / art_refresh make it ask again;
// a failed status request falls back to asking every image.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

const calls = [];
let answer = (ids) => ({ none: [] });
let fail = false;
globalThis.fetch = async (url, opts) => {
  const ids = JSON.parse(opts.body).ids;
  calls.push({ url, ids, method: opts.method });
  if (fail) throw new Error('offline');
  return { ok: true, json: async () => answer(ids) };
};

const M = await import('../../soniqboom/frontend/js/cover_status.js');
const tick = () => new Promise((r) => setTimeout(r, 5));

beforeEach(() => { calls.length = 0; fail = false; answer = () => ({ none: [] }); M.forgetAllCovers(); });

test('one request for a whole paint, and only the rows that may have a cover ask', async () => {
  answer = () => ({ none: ['b', 'c'] });
  const asked = [];
  for (const id of ['a', 'b', 'c', 'd']) M.askCoverWhenUseful(id, () => asked.push(id));
  assert.deepEqual(asked, []);                     // nothing before the answer
  await tick();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/art/status');
  assert.equal(calls[0].method, 'POST');
  assert.deepEqual(calls[0].ids, ['a', 'b', 'c', 'd']);
  assert.deepEqual(asked, ['a', 'd']);
  assert.ok(M.knownNoCover('b') && !M.knownNoCover('a'));
});

test('what is known is not asked about again', async () => {
  answer = () => ({ none: ['x'] });
  M.askCoverWhenUseful('x', () => {});
  M.askCoverWhenUseful('y', () => {});
  await tick();
  const asked = [];
  M.askCoverWhenUseful('x', () => asked.push('x'));
  M.askCoverWhenUseful('y', () => asked.push('y'));   // "may have one": asks at once
  await tick();
  assert.equal(calls.length, 1);
  assert.deepEqual(asked, ['y']);
});

test('art_ready for a track, and art_refresh for all, make the rows ask again', async () => {
  answer = () => ({ none: ['x', 'z'] });
  M.askCoverWhenUseful('x', () => {});
  M.askCoverWhenUseful('z', () => {});
  await tick();
  M.forgetCover('x');
  answer = () => ({ none: [] });
  const asked = [];
  M.askCoverWhenUseful('x', () => asked.push('x'));
  M.askCoverWhenUseful('z', () => asked.push('z'));   // still known: no image asked
  await tick();
  assert.deepEqual(asked, ['x']);
  M.forgetAllCovers();
  M.askCoverWhenUseful('z', () => asked.push('z'));
  await tick();
  assert.deepEqual(asked, ['x', 'z']);
});

test('a failed status request lets every row ask, and learns nothing', async () => {
  fail = true;
  const asked = [];
  M.askCoverWhenUseful('p', () => asked.push('p'));
  M.askCoverWhenUseful('q', () => asked.push('q'));
  await tick();
  assert.deepEqual(asked, ['p', 'q']);
  fail = false;
  M.askCoverWhenUseful('p', () => asked.push('p2'));
  await tick();
  assert.equal(calls.length, 2);                    // asked about again
});

test('a row without a track id asks at once; big paints go in chunks of 300', async () => {
  const asked = [];
  M.askCoverWhenUseful('', () => asked.push('none'));
  assert.deepEqual(asked, ['none']);
  for (let i = 0; i < 650; i += 1) M.askCoverWhenUseful(`t${i}`, () => {});
  await tick();
  assert.deepEqual(calls.map((c) => c.ids.length), [300, 300, 50]);
});

test('several rows of one track all get the answer', async () => {
  const asked = [];
  M.askCoverWhenUseful('same', () => asked.push(1));
  M.askCoverWhenUseful('same', () => asked.push(2));
  await tick();
  assert.deepEqual(calls[0].ids, ['same']);
  assert.deepEqual(asked, [1, 2]);
});

test('an art_ready that arrives while the status request is out wins over its "none"', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    await gate;
    return { ok: true, json: async () => ({ none: ['late', 'other'] }) };
  };
  try {
    const asked = [];
    M.askCoverWhenUseful('late', () => asked.push('late'));
    M.askCoverWhenUseful('other', () => asked.push('other'));
    await tick();                       // the request is out
    M.forgetCover('late');              // art_ready for it meanwhile
    release();
    await tick();
    assert.deepEqual(asked, ['late']);  // asked its image; 'other' stays "none"
    assert.ok(!M.knownNoCover('late') && M.knownNoCover('other'));
  } finally { globalThis.fetch = realFetch; }
});

test('an art_refresh while the request is out drops its whole answer', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => { await gate; return { ok: true, json: async () => ({ none: ['a1', 'a2'] }) }; };
  try {
    const asked = [];
    M.askCoverWhenUseful('a1', () => asked.push('a1'));
    M.askCoverWhenUseful('a2', () => asked.push('a2'));
    await tick();
    M.forgetAllCovers();
    release();
    await tick();
    assert.deepEqual(asked, ['a1', 'a2']);
    assert.ok(!M.knownNoCover('a1') && !M.knownNoCover('a2'));
  } finally { globalThis.fetch = realFetch; }
});
