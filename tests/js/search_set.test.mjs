// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The search view's result-set paging (soniqboom/frontend/js/library.js, the
// ``[search-set:begin]`` / ``[search-set:end]`` region) run outside a browser:
//   * a chunk is asked for from the set the list shows (its handle ``rs``, the
//     list's own column order, the list's fields only);
//   * an answer from ANOTHER set (``renewed``: the library changed, the server
//     restarted) is never stored next to rows of the old one — the chunk fails
//     and the list re-reads itself in place, once, seeded with that answer;
//   * one re-read at a time, a renewal noticed meanwhile makes it run once more,
//     at most three runs; a list no longer on screen is not re-read.
// Also the phone's search pager (mobile/views/search.js ``[m-search-pager]``).
// Run: node --test tests/js/search_set.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const LIB = readFileSync(process.env.SB_LIBRARY_JS || resolve(HERE, '../../soniqboom/frontend/js/library.js'), 'utf8');
const MOB = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/mobile/views/search.js'), 'utf8');
const region = (src, name) => {
  const m = src.match(new RegExp(`// \\[${name}:begin\\][^\\n]*\\n([\\s\\S]*?)// \\[${name}:end\\]`));
  if (!m) throw new Error(`${name} region not found`);
  return m[1];
};
const flush = () => new Promise((r) => setImmediate(r));

function core(env) {
  return new Function('API', 'env', `
    let _searchCtx = null;
    let _browseNavGen = 0;
    const _searchReread = (seed) => env.reread(seed);
    ${region(LIB, 'search-set')}
    return { _searchPage, _searchChunk, _searchRenewed, _refreshSearchInPlace, _putSearchCtx,
             set ctx(c) { if (c) c.gen = _browseNavGen; _searchCtx = c; }, get ctx() { return _searchCtx; },
             navigate() { _browseNavGen++; } };
  `)(env.API, env);
}

test('a chunk names the set, the column order and the list fields', async () => {
  const calls = [];
  const env = { API: async (path, p) => { calls.push([path, p]); return { rs: 'R1', total: 9, tracks: [{ id: 'a' }] }; },
                reread: async () => 'ok' };
  const c = core(env);
  const ctx = { q: 'crack', rs: 'R1', total: 9, sort: 'title', order: 'desc' };
  c.ctx = ctx;
  assert.deepEqual(await c._searchChunk(ctx, 2000, 2000), [{ id: 'a' }]);
  assert.deepEqual(calls[0], ['/search/page', { q: 'crack', offset: 2000, limit: 2000, fields: 'list', rs: 'R1', sort: 'title', order: 'desc' }]);
  await c._searchPage('x', null, 0, 8, null, null);
  assert.deepEqual(calls[1][1], { q: 'x', offset: 0, limit: 8, fields: 'list' });   // (no rs, no sort: the default order)
});

test('an answer from another set is not a chunk: the list re-reads itself once, seeded with it', async () => {
  const reads = [];
  let release;
  // (a re-read installs the new set's list: renewals from the old one are then moot)
  const env = { API: async () => ({ rs: 'R2', total: 7, offset: 2000, tracks: [{ id: 'z' }] }),
                reread: (seed) => { reads.push(seed); return new Promise((r) => { release = () => { c.ctx = { q: 'crack', rs: seed.rs, total: 7 }; r('ok'); }; }); } };
  const c = core(env);
  const ctx = { q: 'crack', rs: 'R1', total: 9 };
  c.ctx = ctx;
  await assert.rejects(c._searchChunk(ctx, 2000, 2000), /renewed/);
  await assert.rejects(c._searchChunk(ctx, 4000, 2000), /renewed/);      // (a second chunk while it runs)
  release(); await flush(); await flush();
  await assert.rejects(c._searchChunk(ctx, 6000, 2000), /renewed/);      // (a late one from the old list)
  await flush(); await flush();
  assert.equal(reads.length, 2 - 1);
  assert.equal(reads[0].rs, 'R2');
  // a list no longer on screen (another search) is not re-read
  c.ctx = { q: 'other', rs: 'R9', total: 1 };
  await assert.rejects(c._searchChunk(ctx, 0, 10), /renewed/);
  await flush();
  assert.equal(reads.length, 1);
  // an error body is a failure, never rows
  const c2 = core({ API: async () => ({ detail: 'nope' }), reread: async () => 'ok' });
  c2.ctx = ctx;
  await assert.rejects(c2._searchChunk(ctx, 0, 10), /not a track list/);
});

test('one re-read at a time; a renewal meanwhile runs it once more; bounded at three', async () => {
  let release;
  const runs = [];
  const env = { API: async () => ({}), reread: (seed) => { runs.push(seed); return new Promise((r) => { release = r; }); } };
  const c = core(env);
  c.ctx = { q: 'q', rs: 'R1', total: 1 };
  const p = c._refreshSearchInPlace({ rs: 'S' });
  c._refreshSearchInPlace();                     // joins the run
  c._searchRenewed(c.ctx, { rs: 'T' });          // noticed meanwhile
  assert.equal(runs.length, 1);
  release('ok'); await flush(); await flush();
  assert.equal(runs.length, 2);                  // once more (unseeded: it reads the newest set itself)
  assert.equal(runs[1], null);
  release('ok'); await p;
  assert.equal(runs.length, 2);
  // a set that keeps changing while it is read: three runs, then it stops
  const env3 = { API: async () => ({}), reread: async () => 'renewed' };
  const c3 = core(env3);
  c3.ctx = { q: 'q', rs: 'R1', total: 1 };
  let n = 0;
  env3.reread = async () => { n++; return 'renewed'; };
  await c3._refreshSearchInPlace();
  assert.equal(n, 3);
  // the user left the search: the loop stops
  const env4 = { API: async () => ({}), reread: async () => { c4.ctx = null; return 'renewed'; } };
  const c4 = core(env4);
  c4.ctx = { q: 'q', rs: 'R1', total: 1 };
  let m = 0;
  const r4 = env4.reread;
  env4.reread = async (s) => { m++; return r4(s); };
  await c4._refreshSearchInPlace();
  assert.equal(m, 1);
});

// ── The phone's pager ────────────────────────────────────────────────────────
function pager(fetchPage, PAGE = 3, REREAD_MAX = 10) {
  return new Function('fetchPage', 'PAGE', 'REREAD_MAX', `
    ${region(MOB, 'm-search-pager')}
    return createSearchPager;
  `)(fetchPage, PAGE, REREAD_MAX);
}

function server(sets) {
  // sets: { rs: [ids...] }; ``cur`` names the set a request without / with a
  // stale handle is answered from.
  const log = [];
  const srv = { cur: Object.keys(sets)[0], log,
    fetch: async (q, rs, offset, limit) => {
      log.push([rs, offset, limit]);
      const use = rs && rs === srv.cur ? rs : srv.cur;
      const ids = sets[use];
      return { rs: use, total: ids.length, tracks: ids.slice(offset, offset + limit).map((id) => ({ id })) };
    } };
  return srv;
}

test('phone: pages are appended from one set; the end is known from the total', async () => {
  const srv = server({ A: ['a', 'b', 'c', 'd', 'e', 'f', 'g'] });
  const changes = [];
  const p = pager(srv.fetch)('q', srv.fetch, (r) => changes.push(r));
  await p.first();
  assert.deepEqual(p.st.rows.map((t) => t.id), ['a', 'b', 'c']);
  await Promise.all([p.more(), p.more()]);           // (one request for both)
  assert.equal(srv.log.length, 2);
  await p.more();
  assert.deepEqual(p.st.rows.map((t) => t.id), ['a', 'b', 'c', 'd', 'e', 'f', 'g']);
  assert.equal(p.st.ended, true);
  await p.more();
  assert.equal(srv.log.length, 3);                   // ended: nothing more asked
  assert.deepEqual(changes, ['first', 'more', 'more']);
});

test('phone: a page from another set replaces the rows read so far — never mixed', async () => {
  const srv = server({ A: ['a', 'b', 'c', 'd', 'e', 'f'], B: ['x', 'a', 'b', 'y', 'c', 'd', 'e', 'f'] });
  const changes = [];
  const p = pager(srv.fetch)('q', srv.fetch, (r) => changes.push(r));
  await p.first();
  srv.cur = 'B';                                     // the library changed
  await p.more();
  assert.deepEqual(p.st.rows.map((t) => t.id), ['x', 'a', 'b']);   // the first rows of B, read again
  assert.equal(p.st.rs, 'B');
  assert.equal(p.st.total, 8);
  assert.deepEqual(changes, ['first', 'reread']);
  await p.more(); await p.more();
  assert.deepEqual(p.st.rows.map((t) => t.id), ['x', 'a', 'b', 'y', 'c', 'd', 'e', 'f']);
  // a closed pager ignores late answers
  const q = pager(srv.fetch)('q', srv.fetch, () => { throw new Error('painted after close'); });
  const f = q.first();
  q.close();
  await f;
});

test('a renewal noticed after a newer view began (another search loading) re-reads nothing', async () => {
  const reads = [];
  const env = { API: async () => ({ rs: 'R2', total: 3, offset: 0, tracks: [] }),
                reread: async (seed) => { reads.push(seed); return 'ok'; } };
  const c = core(env);
  const ctx = { q: 'ma', rs: 'R1', total: 9 };
  c.ctx = ctx;                      // (rendered under the current token)
  c.navigate();                     // Enter on a new search: a view entry, its first page still loading
  await assert.rejects(c._searchChunk(ctx, 2000, 2000), /renewed/);
  await flush();
  assert.equal(reads.length, 0);
  // the list re-rendered under the newest token: renewals count again
  c._putSearchCtx(ctx);
  await assert.rejects(c._searchChunk(ctx, 2000, 2000), /renewed/);
  await flush();
  assert.equal(reads.length, 1);
});

test('phone: a failed re-read changes nothing; the next page reads again, one answer, committed together', async () => {
  const srv = server({ A: ['a1', 'a2', 'a3', 'a4'], B: ['b1', 'b2', 'b3', 'b4', 'b5', 'b6'] });
  let fail = true;
  // (the first page that sees B is answered — then B's re-read fails for a moment)
  let seen = 0;
  const fetch2 = async (...a) => { if (srv.cur === 'B' && seen++ > 0 && fail) throw new Error('HTTP 503'); return srv.fetch(...a); };
  const q = pager(fetch2)('q', fetch2, () => {});
  await q.first();                                   // a1..a3 of A
  srv.cur = 'B';                                     // the library changed
  await assert.rejects(q.more());                    // B's page renews; its re-read fails
  assert.equal(q.st.rs, 'A');                        // nothing changed: the old set's rows, handle and total
  assert.deepEqual(q.st.rows.map((t) => t.id), ['a1', 'a2', 'a3']);
  assert.equal(q.st.total, 4);
  fail = false;
  await q.more();                                    // reads again: B's first rows, committed together
  assert.equal(q.st.rs, 'B');
  assert.deepEqual(q.st.rows.map((t) => t.id), ['b1', 'b2', 'b3']);
  await q.more();
  assert.deepEqual(q.st.rows.map((t) => t.id), ['b1', 'b2', 'b3', 'b4', 'b5', 'b6']);   // never mixed
});
