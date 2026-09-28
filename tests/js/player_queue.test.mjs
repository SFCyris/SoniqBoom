// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Queue / shuffle state machine of player.js (run: node --test tests/js/).
// Regression guard for GitHub issue #11 — shuffle must deal from the WHOLE result
// set, not from the rows a list happens to have loaded.
import test from 'node:test';
import assert from 'node:assert/strict';
import { makePlayer, makeServer, makeStorage, SOURCE, CAPPED, rows, tid } from './player_queue_harness.mjs';

const uniq = (a) => new Set(a).size === a.length;
async function pressNext(p, n) { for (let i = 0; i < n; i++) { p.next(); await p.settle(); } }

test('issue #11: shuffle deals from the whole result set, clicked track first, no repeats', async () => {
  const server = makeServer(5000);
  const p = makePlayer({ server });
  p.toggleShuffle();
  p.setQueue(rows(10, 100), 0, { source: { ...SOURCE(5000), offset: 10 } });
  await p.settle();
  assert.equal(p.current.id, tid(10));
  assert.equal(p.st.mode, 'shuffled');
  await pressNext(p, 600);
  const played = p.env.plays;
  assert.equal(played.length, 601);
  assert.ok(uniq(played), 'a track repeated before the set was exhausted');
  const beyondOldWindow = played.filter((id) => Number(id.slice(1)) >= 510).length;
  assert.ok(beyondOldWindow > 450, `only ${beyondOldWindow} picks came from outside the old 500-row window`);
  assert.ok(p.queue.length <= 300, `queue grew to ${p.queue.length}`);
});

test('a full shuffled pass plays every track exactly once', async () => {
  const server = makeServer(230);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(230), { shuffle: true });
  await p.settle();
  await pressNext(p, 229);
  assert.equal(new Set(p.env.plays).size, 230);
  assert.equal(p.env.plays.length, 230);
});

test('list-order play runs past the loaded window instead of looping back', async () => {
  const server = makeServer(2000);
  const p = makePlayer({ server });
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(2000), offset: 0 } });
  await p.settle();
  await pressNext(p, 320);
  assert.deepEqual(p.env.plays, Array.from({ length: 321 }, (_, i) => tid(i)));
});

test('Prev under shuffle is the track that actually played before', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.toggleShuffle();
  p.setQueue(rows(0, 100), 5, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  await pressNext(p, 3);
  const [, , before, now] = p.env.plays;
  assert.equal(p.current.id, now);
  p.prev(); await p.settle();
  assert.equal(p.current.id, before);
});

test('shuffle off resumes the list from the row after the clicked one; current track untouched', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(40, 100), 0, { source: { ...SOURCE(3000), offset: 40 } });
  await p.settle();
  p.toggleShuffle(); await p.settle();
  await pressNext(p, 4);
  const cur = p.current.id, playsBefore = p.env.plays.length;
  p.toggleShuffle(); await p.settle();
  assert.equal(p.current.id, cur);
  assert.equal(p.env.plays.length, playsBefore, 'toggling shuffle restarted playback');
  assert.deepEqual(p.ids().slice(p.queueIdx + 1, p.queueIdx + 4), [tid(41), tid(42), tid(43)]);
});

test('capped list (no ordered endpoint): shuffle off gives the list back, nothing lost', async () => {
  const list = rows(0, 40);
  const p = makePlayer({ server: makeServer(900) });
  p.setQueue(list, 3, { source: { ...CAPPED(), offset: 0 } });
  await p.settle();
  p.toggleShuffle(); await p.settle();
  await pressNext(p, 2);
  const cur = p.current.id;
  p.toggleShuffle(); await p.settle();
  assert.equal(p.current.id, cur);
  const after = p.ids().slice(p.queueIdx + 1);
  const expected = list.slice(4).map((t) => t.id).filter((id) => !p.ids().slice(0, p.queueIdx + 1).includes(id));
  assert.deepEqual(after, expected, 'upcoming is not the rest of the list in list order');
  for (const t of list) assert.ok(p.ids().includes(t.id), `${t.id} vanished from the queue`);
  assert.ok(uniq(p.ids()));
});

test('capped list: repeat-all wraps, manual Next on the last row wraps', async () => {
  const p = makePlayer({ server: makeServer(900) });
  p.setQueue(rows(0, 5), 4, { source: { ...CAPPED(), offset: 0 } });
  await p.settle();
  p.toggleRepeat();                                   // none → all
  p._advance(); await p.settle();                     // what the 'ended' handler calls
  assert.equal(p.current.id, tid(0));
  assert.equal(p.env.plays.at(-1), tid(0));
});

test('capped list survives a reload whole; a pageable queue is stored as a slice and re-extends', async () => {
  const storage = makeStorage();
  const a = makePlayer({ server: makeServer(900), storage });
  a.setQueue(rows(0, 200), 5, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  const b = makePlayer({ server: makeServer(900), storage });
  await b.settle();
  assert.equal(b.queue.length, 200);
  assert.equal(b.current.id, tid(5));

  const storage2 = makeStorage();
  const server = makeServer(4000);
  const c = makePlayer({ server, storage: storage2 });
  c.setQueue(rows(0, 100), 0, { source: { ...SOURCE(4000), offset: 0 } });
  await c.settle();
  await pressNext(c, 160);
  const saved = JSON.parse(storage2.getItem('sb_queue'));
  assert.ok(saved.tracks.length <= 120, `persisted ${saved.tracks.length} rows`);
  const d = makePlayer({ server, storage: storage2 });
  await d.settle();
  assert.equal(d.current.id, tid(160));
  await pressNext(d, 150);
  assert.deepEqual(d.env.plays, Array.from({ length: 150 }, (_, i) => tid(161 + i)));
});

test('a restored queue whose flag and order disagree is settled before anything plays', async () => {
  const storage = makeStorage({ sb_queue: JSON.stringify({
    tracks: rows(200, 50), idx: 0, shuffle: true,
    src: { desc: SOURCE(4000), mode: 'ordered', seed: 7, total: 4000, ordNext: 250,
           shufNext: 0, ordResume: 201, pos: Array.from({ length: 50 }, (_, i) => 200 + i) } }) });
  const p = makePlayer({ server: makeServer(4000), storage });
  await p.settle();
  assert.equal(p.shuffle, true);
  assert.equal(p.st.mode, 'shuffled');
  assert.equal(p.current.id, tid(200));
  assert.ok(p.env.events.some(([n]) => n === 'shufflechange'));
});

test('Undo clear brings back the exact order, the source and the shuffle deal', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.toggleShuffle();
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  await pressNext(p, 3);
  const before = p.ids(), idx = p.queueIdx, seed = p.st.seed;
  const snap = p.snapshotQueue();
  p.setQueue([], 0);
  assert.equal(p.queue.length, 0);
  assert.equal(p.restoreQueue(snap), true);
  await p.settle();
  assert.deepEqual(p.ids().slice(0, before.length), before);
  assert.equal(p.queueIdx, idx);
  assert.equal(p.st.seed, seed);
  assert.equal(p.st.mode, 'shuffled');
  await pressNext(p, 80);
  assert.ok(uniq(p.env.plays.slice(0, 4).concat(p.env.plays.slice(5))), 'repeat after undo');
});

test('tracks queued by hand survive shuffle on and shuffle off', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  const mine = { id: 'mine-1', title: 'Mine' };
  p.playNext(mine);
  p.toggleShuffle(); await p.settle();
  assert.equal(p.queue[p.queueIdx + 1].id, 'mine-1');
  p.toggleShuffle(); await p.settle();
  assert.equal(p.queue[p.queueIdx + 1].id, 'mine-1');
  assert.equal(p.queue[p.queueIdx + 2].id, tid(1));
});

test('Radio Mode refuses the shuffle toggle; ending radio leaves the mix and the deal alone', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  p.setRadioActive(true);
  const events = p.env.events.length;
  assert.equal(p.toggleShuffle(), false);               // refused: the flag must not light a dead button
  assert.equal(p.shuffle, false);
  assert.ok(!p.env.events.slice(events).some(([n]) => n === 'shufflechange'));
  for (let i = 0; i < 6; i++) p.addToQueue({ id: 'radio-' + i, title: 'mix' }, { auto: true });
  const before = p.ids();
  p.setRadioActive(false); await p.settle();
  assert.deepEqual(p.ids().slice(0, before.length), before, 'stopping radio threw the mix away');
  assert.equal(p.st.mode, 'ordered');
  // Already shuffled when radio starts: stopping it must not re-deal either.
  p.toggleShuffle(); await p.settle();
  const seed = p.st.seed;
  p.setRadioActive(true); p.setRadioActive(false); await p.settle();
  assert.equal(p.st.seed, seed, 're-dealt an order that was already dealt');
  assert.equal(p.st.mode, 'shuffled');
});

test('a dead server is not hammered, and playback picks up when it is back', async () => {
  const server = makeServer(3000);
  const p = makePlayer({ server });
  p.setQueue(rows(0, 12), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  server.down = true;
  const before = server.requests.length;
  await pressNext(p, 30);
  assert.ok(server.requests.length - before <= 31, `${server.requests.length - before} requests for 30 presses`);
  assert.equal(p.current.id, tid(11));                 // parked on the last loaded row
  server.down = false;
  await pressNext(p, 3);
  assert.equal(p.current.id, tid(14));
});

test('a burst of Next presses past the window neither skips nor repeats', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 20), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  for (let i = 0; i < 45; i++) p.next();               // synchronous burst
  await p.settle();
  assert.ok(uniq(p.env.plays));
  assert.deepEqual(p.env.plays, p.env.plays.slice().sort(), 'list order broken');
  assert.ok(uniq(p.ids()));
});

test('natural track end is never recorded as a skip; Next is', async () => {
  const p = makePlayer({ server: makeServer(100) });
  p.setQueue(rows(0, 10), 0);
  p._advance(); p.next();
  assert.deepEqual(p.env.advances, [true]);
});

test('plain array: shuffle is a permutation, a pass has no repeats, un-shuffle restores order', async () => {
  const list = rows(0, 12);
  const dup = list[2];
  const playlist = [...list, dup];                     // the same track twice, like a playlist
  const p = makePlayer({});
  p.setQueue(playlist, 5);
  assert.equal(p.st.src, null);
  p.toggleShuffle();
  assert.equal(p.current.id, tid(5));
  assert.equal(p.queueIdx, 0);
  assert.deepEqual(p.ids().slice().sort(), playlist.map((t) => t.id).sort());
  for (let i = 0; i < 12; i++) p.next();
  assert.equal(p.env.plays.length, 13);                // the click + 12 Next; toggling never replays
  assert.equal(p.queue.length, 13, 'a queue entry was lost or doubled');
  const cur = p.current;
  p.toggleShuffle();
  assert.deepEqual(p.ids(), playlist.map((t) => t.id));
  assert.equal(p.current, cur);
});

test('Shuffle all on a capped list, then shuffle off, gives the listed rows', async () => {
  const list = rows(0, 30);
  const p = makePlayer({ server: makeServer(900) });
  assert.equal(await p.playSource(CAPPED(), { shuffle: true, rows: list }), true);
  await p.settle();
  assert.equal(p.shuffle, true);
  p.toggleShuffle(); await p.settle();
  const upcoming = p.ids().slice(p.queueIdx + 1);
  assert.deepEqual(upcoming, list.map((t) => t.id).filter((id) => id !== p.current.id));
});

test('queue-panel jump keeps the source and the deal', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.toggleShuffle();
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  const seed = p.st.seed, target = p.queue[45].id;
  p.setQueue(p.queue, 45); await p.settle();
  assert.equal(p.current.id, target);
  assert.equal(p.st.seed, seed);
  assert.equal(p.st.mode, 'shuffled');
  assert.ok(p.queue.length > 51);
  assert.ok(uniq(p.ids()));
});

test('list position survives manual edits: Prev pages backwards, shuffle off resumes at the right row', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(300, 100), 0, { source: { ...SOURCE(3000), offset: 300 } });
  await p.settle();
  p.addToQueue({ id: 'mine-2', title: 'Mine' });        // a manual edit
  p.removeFromQueue(1);                                 // drop list row 301
  p.prev(); await p.settle();                           // first row → page backwards
  assert.equal(p.current.id, tid(299));
  p.next(); await p.settle();
  assert.equal(p.current.id, tid(300));
  p.toggleShuffle(); await p.settle();
  p.toggleShuffle(); await p.settle();
  const up = p.ids().slice(p.queueIdx + 1, p.queueIdx + 4);
  assert.deepEqual(up, ['mine-2', tid(302), tid(303)]);  // 301 stays removed, nothing skipped
});

test('list positions are restored with the queue after a reload', async () => {
  const storage = makeStorage();
  const server = makeServer(3000);
  const a = makePlayer({ server, storage });
  a.setQueue(rows(500, 100), 0, { source: { ...SOURCE(3000), offset: 500 } });
  await a.settle();
  const b = makePlayer({ server, storage });
  await b.settle();
  assert.equal(b.posOf(b.queue[0]), 500);
  b.prev(); await b.settle();
  assert.equal(b.current.id, tid(499));
  b.toggleShuffle(); await b.settle();
  b.toggleShuffle(); await b.settle();
  assert.equal(b.queue[b.queueIdx + 1].id, tid(500));
});

// ── found by the second independent review ───────────────────────────────────

test('a track that played outside the shuffle order is not dealt again after history is trimmed', async () => {
  const server = makeServer(5000);
  server.place.set(10, 400);                            // the clicked track sits deep in EVERY order
  server.place.set(4321, 450);                          // …and so does a hand-queued one
  const p = makePlayer({ server });
  p.setQueue(rows(10, 100), 0, { source: { ...SOURCE(5000), offset: 10 } });
  await p.settle();
  p.toggleShuffle(); await p.settle();
  p.addToQueue({ id: tid(4321), title: 'queued by hand' });
  await pressNext(p, 700);                              // far past Q_KEEP_BEHIND
  const count = (id) => p.env.plays.filter((x) => x === id).length;
  assert.equal(count(tid(10)), 1, 'the clicked track was dealt a second time');
  assert.equal(count(tid(4321)), 1, 'the hand-queued track was dealt a second time');
  assert.ok(uniq(p.env.plays));
});

test('…and that still holds across a reload in the middle of the pass', async () => {
  const server = makeServer(5000);
  server.place.set(10, 400);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(10, 100), 0, { source: { ...SOURCE(5000), offset: 10 } });
  await a.settle();
  a.toggleShuffle(); await a.settle();
  await pressNext(a, 250);
  const b = makePlayer({ server, storage, rngSeed: 999 });
  await b.settle();
  assert.equal(b.st.mode, 'shuffled');
  await pressNext(b, 400);
  assert.ok(!b.env.plays.includes(tid(10)), 'the pinned track came back after the reload');
  assert.ok(uniq(a.env.plays.concat(b.env.plays)));
});

test('capped list: shuffle on, RELOAD, shuffle off → the list comes back (rows re-fetched by id)', async () => {
  const list = rows(0, 40);
  const server = makeServer(900);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(list, 3, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  a.toggleShuffle(); await a.settle();
  await pressNext(a, 2);
  const b = makePlayer({ server, storage });
  await b.settle();
  assert.equal(b.st.mode, 'shuffled');
  const cur = b.current.id;
  b.toggleShuffle(); await b.settle();
  assert.equal(b.current.id, cur);
  const hist = b.ids().slice(0, b.queueIdx + 1);
  assert.deepEqual(b.ids().slice(b.queueIdx + 1), list.slice(4).map((t) => t.id).filter((id) => !hist.includes(id)));
  for (const t of list) assert.ok(b.ids().includes(t.id), `${t.id} unreachable after the reload`);
  b.toggleRepeat();                                     // repeat-all: the restored list wraps, it does not dead-end
  b.setQueue(b.queue, b.queue.length - 1); b.next(); await b.settle();
  assert.equal(b.queueIdx, 0);
});

test('plain array: shuffle on, reload, shuffle off → original order, without asking the server', async () => {
  const list = rows(0, 12);
  const playlist = [...list, list[2]];
  const server = makeServer(100);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(playlist, 5);
  a.toggleShuffle(); a.next(); await a.settle();
  const b = makePlayer({ server, storage });
  await b.settle();
  const cur = b.current.id;
  b.toggleShuffle(); await b.settle();
  assert.deepEqual(b.ids(), playlist.map((t) => t.id));
  assert.equal(b.current.id, cur);
  assert.equal(server.requests.length, 0);
});

test('Prev at the window head: one request for a burst, a full page back, lands on the row before', async () => {
  const server = makeServer(3000);
  const p = makePlayer({ server });
  p.setQueue(rows(300, 100), 0, { source: { ...SOURCE(3000), offset: 300 } });
  await p.settle();
  const before = server.requests.length;
  p.prev(); p.prev(); p.prev();
  await p.settle();
  assert.equal(server.requests.length - before, 1, 'duplicate backward requests');
  assert.equal(p.current.id, tid(299));
  assert.equal(p.posOf(p.queue[0]), 250, 'a backward page is Q_PAGE rows, not one');
  const n = server.requests.length;
  for (let i = 0; i < 49; i++) p.prev();
  await p.settle();
  assert.equal(p.current.id, tid(250));
  assert.equal(server.requests.length, n, 'rows already paged in were fetched again');
});

test('Prev lands on the row before even when that track is also queued elsewhere', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(300, 100), 0, { source: { ...SOURCE(3000), offset: 300 } });
  await p.settle();
  p.addToQueue({ id: tid(299), title: 'same track, queued by hand' });
  p.prev(); await p.settle();
  assert.equal(p.current.id, tid(299));
  assert.equal(p.queueIdx, 49);
});

test('a hand-queued CURRENT track is not doubled by a re-deal', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  p.playNext({ id: 'mine-3', title: 'Mine' });
  p.next(); await p.settle();
  assert.equal(p.current.id, 'mine-3');
  p.toggleShuffle(); await p.settle();
  assert.equal(p.ids().filter((id) => id === 'mine-3').length, 1);
  p.toggleShuffle(); await p.settle();
  assert.equal(p.ids().filter((id) => id === 'mine-3').length, 1);
});

test('hand-queued tracks stay protected after a reload', async () => {
  const server = makeServer(3000);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await a.settle();
  a.playNext({ id: 'mine-4', title: 'Mine' });
  await a.settle();
  const b = makePlayer({ server, storage });
  await b.settle();
  assert.ok(b.isManual(b.queue[b.queueIdx + 1]));
  b.toggleShuffle(); await b.settle();
  assert.equal(b.queue[b.queueIdx + 1].id, 'mine-4');
});

test('Radio Mode refills are not "hand-queued": a deal after radio replaces them', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 20), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  p.setRadioActive(true);
  for (let i = 0; i < 12; i++) p.addToQueue({ id: 'radio-' + i, title: 'mix' }, { auto: true });
  p.addToQueue({ id: 'mine-5', title: 'Mine' });
  p.setRadioActive(false); await p.settle();
  p.toggleShuffle(); await p.settle();                  // the listener asks for a shuffle AFTER radio
  const up = p.ids().slice(p.queueIdx + 1);
  assert.equal(up[0], 'mine-5');
  assert.ok(!up.some((id) => id.startsWith('radio-')), 'radio leftovers outrank the shuffle');
});

test('a queue saved with rows cut off its tail re-fetches exactly those rows', async () => {
  const server = makeServer(3000);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 400), 0, { source: { ...SOURCE(3000), offset: 0 } });   // a capped drill-down: 400 rows at hand
  await a.settle();
  const saved = JSON.parse(storage.getItem('sb_queue'));
  assert.ok(saved.tracks.length < 400, 'expected the save to be a slice');
  const b = makePlayer({ server, storage });
  await b.settle();
  await pressNext(b, 450);
  assert.deepEqual(b.env.plays, Array.from({ length: 450 }, (_, i) => tid(i + 1)));
});

test('paging stops at the known total; history deep enough to walk 100 tracks back', async () => {
  const server = makeServer(5000);                      // the server has more than the view's total
  const p = makePlayer({ server });
  p.setQueue(rows(0, 20), 0, { source: { ...SOURCE(170), offset: 0 } });
  await p.settle();
  await pressNext(p, 169);
  assert.equal(p.current.id, tid(169));
  const offsets = server.requests.map((u) => Number(new URL(u, 'http://x').searchParams.get('offset')));
  assert.ok(offsets.every((o) => o < 170), `asked for offset ${Math.max(...offsets)} of a 170-row list`);
  const n = server.requests.length;
  for (let i = 0; i < 100; i++) p.prev();
  await p.settle();
  assert.equal(p.current.id, tid(69));
  assert.equal(server.requests.length, n, 'history was trimmed too early');
});

test('a new shuffle pass does not open with the track that just closed the last one', async () => {
  for (let seed = 1; seed <= 40; seed++) {
    const p = makePlayer({ server: makeServer(3), rngSeed: seed });
    await p.playSource(SOURCE(3), { shuffle: true });
    await p.settle();
    await pressNext(p, 2);
    const last = p.current.id;
    p.next(); await p.settle();                         // end of the pass → a fresh deal
    assert.notEqual(p.current.id, last, `rngSeed ${seed}`);
    assert.equal(p.st.mode, 'shuffled');
  }
});

// ── one shuffle rule for both queue kinds: deal what has not played yet ───────

test('plain array: what already played stays behind; only the rest is dealt; Prev and un-shuffle still work', async () => {
  const list = rows(0, 12);
  const p = makePlayer({});
  p.setQueue(list, 0);
  for (let i = 0; i < 8; i++) p.next();                 // tracks 0..8 have played
  p.toggleShuffle();
  assert.equal(p.current.id, tid(8));
  assert.deepEqual(p.ids().slice(0, p.queueIdx), list.slice(0, 8).map((t) => t.id));
  assert.deepEqual(p.ids().slice(p.queueIdx + 1).sort(), [tid(9), tid(10), tid(11)]);
  p.prev();
  assert.equal(p.current.id, tid(7));
  p.next();
  p.toggleShuffle();
  assert.deepEqual(p.ids(), list.map((t) => t.id));
  assert.equal(p.current.id, tid(8));
});

test('plain array: clicking a track with shuffle already on deals ALL the others', async () => {
  const list = rows(0, 12);
  const p = makePlayer({});
  p.toggleShuffle();
  p.setQueue(list, 9);
  assert.equal(p.queueIdx, 0);
  assert.equal(p.current.id, tid(9));
  assert.equal(new Set(p.ids()).size, 12);
  p.setQueue(list, 3);                                  // same list again: earlier plays do not count
  assert.equal(p.current.id, tid(3));
  assert.equal(p.queueIdx, 0, 'a play from the PREVIOUS queue was kept as history');
  assert.equal(p.ids().slice(1).length, 11);
});

test('view-backed: rows left behind the clicked one that never played are part of the deal', async () => {
  const p = makePlayer({ server: makeServer(40) });
  p.toggleShuffle();
  p.setQueue(rows(0, 40), 20, { source: { ...CAPPED(), offset: 0 } });
  await p.settle();
  await pressNext(p, 39);
  assert.equal(new Set(p.env.plays).size, 40, 'rows 0–19 never played, yet the pass left some out');
  assert.equal(p.env.plays.length, 40);
});

test('a shuffled order with a hole of deleted ids keeps going to its real end', async () => {
  const server = makeServer(300);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(300), { shuffle: true });
  await p.settle();
  const order = server.perms.get(String(p.st.seed));
  order.slice(50, 160).forEach((i) => server.deleted.add(i));   // two whole pages + a bit
  await pressNext(p, 189);
  assert.equal(new Set(p.env.plays).size, 190);
  assert.equal(p.env.plays.length, 190);                // 300 − 110 deleted, each once
});

test('Radio Mode mix goes right after the current track, ahead of what was queued', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 30), 4);
  p.setRadioActive(true);
  for (let i = 0; i < 5; i++) p.addToQueue({ id: 'radio-' + i, title: 'mix' }, { auto: true });
  assert.deepEqual(p.ids().slice(5, 10), ['radio-0', 'radio-1', 'radio-2', 'radio-3', 'radio-4']);
  assert.equal(p.ids()[10], tid(5));
  assert.equal(p.autoAhead, 5);
  p.next();
  assert.equal(p.current.id, 'radio-0');
  assert.equal(p.autoAhead, 4);
  p.addToQueue({ id: 'radio-5', title: 'mix' }, { auto: true });   // a refill lands behind the mix
  assert.equal(p.ids()[p.queueIdx + 5], 'radio-5');
  p.playNext({ id: 'mine-6', title: 'Mine' });          // "play next" sits between current and the mix
  assert.equal(p.autoAhead, 5, 'the radio lost count of its mix → it would refill on every track');
  p.addToQueue({ id: 'radio-6', title: 'mix' }, { auto: true });
  assert.equal(p.ids()[p.ids().indexOf('radio-5') + 1], 'radio-6');
});

test('browser storage full: a smaller save replaces the old one; if nothing fits, the old one is removed', async () => {
  const big = rows(0, 3000);
  const storage = makeStorage({ sb_queue: JSON.stringify({ tracks: rows(900, 3), idx: 0 }) }, { quota: 20000 });
  const p = makePlayer({ storage });
  await p.settle();
  p.setQueue(big, 1500);
  await p.settle();
  const saved = JSON.parse(storage.getItem('sb_queue'));
  assert.equal(saved.tracks[saved.idx].id, tid(1500));
  assert.ok(saved.tracks.length <= 320);
  const none = makeStorage({ sb_queue: JSON.stringify({ tracks: rows(900, 3), idx: 0 }) }, { quota: 10 });
  const q = makePlayer({ storage: none });
  await q.settle();
  q.setQueue(big, 7);
  await q.settle();
  assert.equal(none.getItem('sb_queue'), null, 'an older queue was left behind to be restored');
});

test('Undo clear remembers what had played', async () => {
  const list = rows(0, 12);
  const p = makePlayer({});
  p.setQueue(list, 0);
  for (let i = 0; i < 5; i++) p.next();
  const snap = p.snapshotQueue();
  p.setQueue([], 0);
  p.restoreQueue(snap);
  p.toggleShuffle();
  assert.deepEqual(p.ids().slice(0, p.queueIdx), list.slice(0, 5).map((t) => t.id));
  assert.equal(p.ids().slice(p.queueIdx + 1).length, 6);
});

// ── found by the third independent review ────────────────────────────────────

test('a hand-queued track that is removed again before it played rejoins the shuffle pass', async () => {
  const server = makeServer(400);
  server.place.set(321, 300);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(400), { shuffle: true });
  await p.settle();
  p.addToQueue({ id: tid(321), title: 'changed my mind' });
  p.removeFromQueue(p.ids().indexOf(tid(321)));
  await pressNext(p, 399);
  assert.equal(p.env.plays.filter((x) => x === tid(321)).length, 1);
  assert.equal(new Set(p.env.plays).size, 400);
});

test('"Play next" marks the track as played outside the order too', async () => {
  const server = makeServer(5000);
  server.place.set(4321, 420);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(5000), { shuffle: true });
  await p.settle();
  p.playNext({ id: tid(4321), title: 'next up' });
  await pressNext(p, 700);
  assert.equal(p.env.plays.filter((x) => x === tid(4321)).length, 1);
});

test('capped list after a reload: a FAILED row fetch leaves the queue alone, a later one restores it', async () => {
  const list = rows(0, 40);
  const server = makeServer(900);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(list, 3, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  a.toggleShuffle(); await a.settle();
  await pressNext(a, 2);
  const b = makePlayer({ server, storage });
  await b.settle();
  const before = b.ids();
  server.failBatch = true;
  b.toggleShuffle(); await b.settle();
  assert.deepEqual(b.ids(), before, 'a partial list was applied');
  server.failBatch = false;
  b.toggleShuffle(); await b.settle();                  // on again…
  b.toggleShuffle(); await b.settle();                  // …and off: the ids were kept for this
  for (const t of list) assert.ok(b.ids().includes(t.id), `${t.id} lost`);
});

test('the pre-shuffle list of one view never leaks into the next queue', async () => {
  const server = makeServer(900);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 40), 3, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  a.toggleShuffle(); await a.settle();
  const b = makePlayer({ server, storage });            // reloaded: the first list lives on as ids
  await b.settle();
  const second = rows(500, 30);
  b.setQueue(second, 0, { source: { ...CAPPED(), offset: 0 } });    // shuffle is still on
  await b.settle();
  b.toggleShuffle(); await b.settle();
  const up = b.ids().slice(b.queueIdx + 1);
  assert.deepEqual(up, second.slice(1).map((t) => t.id));
});

test('a failed wrap changes nothing; the pass that finally starts covers every track', async () => {
  const server = makeServer(300);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(300), { shuffle: true });
  await p.settle();
  p.toggleRepeat();                                     // repeat all
  await pressNext(p, 299);
  const seed = p.st.seed, shufNext = p.st.shufNext;
  server.down = true;
  p.next(); await p.settle();
  assert.equal(p.st.seed, seed, 'seed changed although the restart failed');
  assert.equal(p.st.shufNext, shufNext);
  server.down = false;
  p.next(); await p.settle();
  assert.notEqual(p.st.seed, seed);
  const from = p.env.plays.length - 1;
  await pressNext(p, 299);
  assert.equal(new Set(p.env.plays.slice(from)).size, 300, 'the new pass skipped rows left over from the old one');
});

test('a new pass may play everything again, including what was pinned in the last one', async () => {
  const server = makeServer(120);
  server.place.set(7, 80);                              // beyond the first page of EVERY order
  const p = makePlayer({ server });
  p.setQueue(rows(0, 60), 7, { source: { ...SOURCE(120), offset: 0 } });
  await p.settle();
  p.toggleShuffle(); await p.settle();
  p.toggleRepeat();
  while (!(p.st.ended && p.queueIdx === p.queue.length - 1) && p.env.plays.length < 200) {
    p.next(); await p.settle();                         // to the last track of the first pass
  }
  const seed = p.st.seed;
  p.next(); await p.settle();                           // wraps into a new pass
  assert.notEqual(p.st.seed, seed);
  const from = p.env.plays.length - 1;
  await pressNext(p, 119);
  const second = p.env.plays.slice(from);
  assert.equal(new Set(second).size, 120);
  assert.ok(second.includes(tid(7)), 'the track pinned in the first pass was kept out of the second');
});

test('Undo clear keeps the record of what played outside the order', async () => {
  const server = makeServer(5000);
  server.place.set(10, 400);
  const p = makePlayer({ server });
  p.setQueue(rows(10, 100), 0, { source: { ...SOURCE(5000), offset: 10 } });
  await p.settle();
  p.toggleShuffle(); await p.settle();
  const snap = p.snapshotQueue();
  p.setQueue([], 0);
  p.restoreQueue(snap); await p.settle();
  await pressNext(p, 700);
  assert.equal(p.env.plays.filter((x) => x === tid(10)).length, 2, 'once before the clear, once as the restored current track');
  assert.ok(uniq(p.env.plays.slice(1)));
});

test('what played before a reload, or before shuffle went on, is not dealt again', async () => {
  const server = makeServer(40);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 40), 0, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  await pressNext(a, 4);                                // t0…t4 played, in list order
  const b = makePlayer({ server, storage });
  await b.settle();
  b.toggleShuffle(); await b.settle();
  await pressNext(b, 35);
  assert.equal(b.env.plays.length, 35);
  assert.ok(uniq(b.env.plays));
  for (let i = 0; i < 5; i++) assert.ok(!b.env.plays.includes(tid(i)), `${tid(i)} replayed`);
});

test('"Shuffle all" on a complete list starts on a random row, not always the first', async () => {
  const firsts = new Set();
  for (let seed = 1; seed <= 12; seed++) {
    const p = makePlayer({ rngSeed: seed });
    p.setQueue(rows(0, 50), 0, { shuffle: true });
    assert.equal(p.shuffle, true);
    assert.equal(new Set(p.ids()).size, 50);
    firsts.add(p.current.id);
  }
  assert.ok(firsts.size > 6, `only ${firsts.size} distinct opening tracks in 12 tries`);
});

// ── Settings → "Shuffle may replay tracks already played" (sb_shuffle_replay) ──

test('option ON, plain array: the deal covers every other track, played ones included', async () => {
  const list = rows(0, 12);
  const p = makePlayer({ storage: makeStorage({ sb_shuffle_replay: '1' }) });
  p.setQueue(list, 0);
  for (let i = 0; i < 8; i++) p.next();
  p.toggleShuffle();
  assert.equal(p.queueIdx, 0);
  assert.equal(p.current.id, tid(8));
  assert.equal(p.ids().slice(1).length, 11);
  assert.ok(p.ids().slice(1).includes(tid(2)), 'a played track was kept out although the option allows it');
  p.toggleShuffle();
  assert.deepEqual(p.ids(), list.map((t) => t.id));     // un-shuffle is unchanged
});

test('option ON, view-backed: tracks that played before shuffle went on are dealt again; hand-queued and current are not', async () => {
  const server = makeServer(40);
  const p = makePlayer({ server, storage: makeStorage({ sb_shuffle_replay: '1' }) });
  p.setQueue(rows(0, 40), 0, { source: { ...CAPPED(), offset: 0 } });
  await p.settle();
  await pressNext(p, 4);                                // t0…t4 played
  p.toggleShuffle(); await p.settle();
  const from = p.env.plays.length;
  await pressNext(p, 39);
  const dealt = p.env.plays.slice(from);
  assert.equal(new Set(dealt).size, 39);
  assert.ok([0, 1, 2, 3].every((i) => dealt.includes(tid(i))), 'played tracks were kept out although the option allows it');
  assert.ok(!dealt.includes(tid(4)), 'the current track was dealt again');
});

test('option OFF is the default, and the switch is read at deal time', async () => {
  const storage = makeStorage();
  const p = makePlayer({ storage });
  p.setQueue(rows(0, 12), 0);
  for (let i = 0; i < 8; i++) p.next();
  p.toggleShuffle();
  assert.equal(p.ids().slice(p.queueIdx + 1).length, 3);
  p.toggleShuffle();
  storage.setItem('sb_shuffle_replay', '1');            // changed in Settings while playing
  p.toggleShuffle();
  assert.equal(p.ids().slice(p.queueIdx + 1).length, 11);
});

// ── found by the UX / performance reviews ────────────────────────────────────

test('playSource: superseded → null (not an error), failed → false, and a failed start never lights shuffle', async () => {
  const server = makeServer(3000);
  const p = makePlayer({ server });
  const first = p.playSource(SOURCE(3000), { shuffle: true });
  const second = p.playSource(SOURCE(3000), { shuffle: true });     // a second click before the first landed
  assert.equal(await first, null);
  assert.equal(await second, true);
  assert.equal(p.shuffle, true);
  const q = makePlayer({ server: Object.assign(makeServer(10), { down: true }) });
  assert.equal(await q.playSource(SOURCE(10), { shuffle: true }), false);
  assert.equal(q.shuffle, false, 'the shuffle button would be lit although nothing was shuffled');
});

test('running dry because the server is gone is announced (queuestall), not silent', async () => {
  const server = makeServer(3000);
  const p = makePlayer({ server });
  p.setQueue(rows(0, 12), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  server.down = true;
  await pressNext(p, 11);
  const before = p.env.events.filter(([n]) => n === 'queuestall').length;
  p._advance(); await p.settle();                       // the track ends with nothing left
  assert.equal(p.env.events.filter(([n]) => n === 'queuestall').length, before + 1);
  assert.ok(p.env.events.some(([n, d]) => n === 'statechange' && d.playing === false));
});

// ── found by the fourth QA pass ──────────────────────────────────────────────

test('a reload does not turn rows that never played into "played" (playlist clicked at row 50)', async () => {
  const storage = makeStorage();
  const a = makePlayer({ storage });
  a.setQueue(rows(0, 100), 50);
  await a.settle();
  const b = makePlayer({ storage });
  await b.settle();
  b.toggleShuffle();
  assert.equal(b.queueIdx, 0);
  assert.equal(b.ids().slice(1).length, 99);
  assert.ok(b.ids().includes(tid(0)));
  // …while what DID play before the reload stays behind.
  const c = makePlayer({ storage: makeStorage() });
  c.setQueue(rows(0, 100), 50); c.next(); c.next();
  await c.settle();
  const d = makePlayer({ storage: c.storage });
  await d.settle();
  d.toggleShuffle();
  assert.deepEqual(d.ids().slice(0, d.queueIdx), [tid(50), tid(51)]);
  assert.equal(d.ids().slice(d.queueIdx + 1).length, 97);
});

test('capped list clicked deep, reloaded, shuffled: the rows above the click are still dealt', async () => {
  const server = makeServer(300);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 300), 150, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  const b = makePlayer({ server, storage });
  await b.settle();
  b.toggleShuffle(); await b.settle();
  await pressNext(b, 299);
  assert.equal(new Set(b.env.plays).size, 299);
  assert.ok(b.env.plays.includes(tid(3)), 'rows above the clicked one were treated as played');
});

test('repeat-all wrap with shuffle OFF starts a fresh pass: a later shuffle has everything to deal', async () => {
  const p = makePlayer({});
  p.setQueue(rows(0, 10), 0);
  p.toggleRepeat();
  for (let i = 0; i < 12; i++) p._advance();            // through the end and around
  assert.equal(p.current.id, tid(2));
  p.toggleShuffle();
  assert.equal(p.ids().slice(p.queueIdx + 1).length, 7, 'plays from the PREVIOUS pass still counted');
});

test('clicking a deep row with shuffle on plays THAT row first, then the deal', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.toggleShuffle();
  assert.equal(await p.playSource(SOURCE(3000), { offset: 1234 }), true);
  await p.settle();
  assert.equal(p.env.plays[0], tid(1234));
  assert.equal(p.st.mode, 'shuffled');
  assert.ok(p.ids().slice(1, 6).some((id) => id !== tid(1235)), 'upcoming is list order, not a deal');
});

test('Radio Mode stops view extension; a new session clears the old mix; a stray mix row at the tail does not capture refills', async () => {
  const server = makeServer(3000);
  const p = makePlayer({ server });
  p.setQueue(rows(0, 30), 0, { source: { ...SOURCE(3000), offset: 0 } });   // 29 ahead: no fetch yet
  await p.settle();
  p.setRadioActive(true);
  const before = server.requests.length;
  await pressNext(p, 25);                                // 4 ahead — would page if radio did not own the order
  assert.equal(server.requests.length, before, 'the view kept paging under radio');
  p.setRadioActive(false);

  const q = makePlayer({});
  q.setQueue(rows(0, 10), 2);
  q.setRadioActive(true);
  for (let i = 0; i < 3; i++) q.addToQueue({ id: 'a' + i, title: 'mix' }, { auto: true });
  q.moveInQueue(q.queueIdx + 3, q.queue.length - 1);     // the listener drags a2 to the very end
  q.addToQueue({ id: 'a3', title: 'mix' }, { auto: true });          // a refill
  assert.deepEqual(q.ids().slice(q.queueIdx + 1, q.queueIdx + 4), ['a0', 'a1', 'a3']);
  q.setRadioActive(false);
  q.clearAutoUpcoming();                                 // what app.js does when radio starts again
  assert.ok(!q.ids().slice(q.queueIdx + 1).some((id) => /^a\d$/.test(id)));
  q.setRadioActive(true);
  for (let i = 0; i < 3; i++) q.addToQueue({ id: 'b' + i, title: 'mix' }, { auto: true });
  assert.deepEqual(q.ids().slice(q.queueIdx + 1, q.queueIdx + 4), ['b0', 'b1', 'b2']);
  assert.equal(q.autoAhead, 3);
});

test('removing a hand-queued track that already PLAYED does not put it back into the pass', async () => {
  const server = makeServer(400);
  server.place.set(321, 300);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(400), { shuffle: true });
  await p.settle();
  p.playNext({ id: tid(321), title: 'mine' });
  p.next(); await p.settle();                            // it plays
  p.next(); await p.settle();
  p.removeFromQueue(p.ids().indexOf(tid(321)));          // tidy it out of the history
  await pressNext(p, 397);
  assert.equal(p.env.plays.filter((x) => x === tid(321)).length, 1);
});

test('storage-full fallback keeps the source, so "shuffle everything" survives the reload', async () => {
  const server = makeServer(5000);
  const storage = makeStorage({}, { quota: 60000 });
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 3000), 1500, { source: { ...CAPPED(), offset: 0 } });
  await a.settle();
  const saved = JSON.parse(storage.getItem('sb_queue'));
  assert.ok(saved.tracks.length < 400 && saved.src && saved.src.desc.shuffle, 'fallback dropped the source');
  const b = makePlayer({ server, storage });
  await b.settle();
  b.toggleShuffle(); await b.settle();
  assert.equal(b.st.mode, 'shuffled');
  await pressNext(b, 120);
  assert.ok(b.env.plays.some((id) => Number(id.slice(1)) >= 3000), 'the shuffle is confined to the saved rows');
});

// ── found by the follow-up QA pass ───────────────────────────────────────────

test('storage-full fallback keeps what played and what was hand-queued, for ITS slice', async () => {
  const storage = makeStorage({}, { quota: 60000 });
  const a = makePlayer({ storage });
  a.setQueue(rows(0, 3000), 1500);
  a.playNext({ id: 'mine-9', title: 'Mine' });
  await a.settle();
  const saved = JSON.parse(storage.getItem('sb_queue'));
  assert.ok(saved.tracks.length < 400, 'expected the fallback slice');
  assert.equal(saved.played.filter(Boolean).length, 1);
  assert.equal(saved.tracks[saved.played.indexOf(1)].id, tid(1500));
  const b = makePlayer({ storage });
  await b.settle();
  assert.ok(b.isManual(b.queue[b.queueIdx + 1]));
  b.toggleShuffle();
  assert.equal(b.queueIdx, 0, 'rows before the click were treated as played');
  assert.equal(b.queue[1].id, 'mine-9');
});

test('a new radio session gives the old mix back to the shuffle pass', async () => {
  const server = makeServer(300);
  server.place.set(201, 250); server.place.set(202, 251);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(300), { shuffle: true });
  await p.settle();
  p.setRadioActive(true);
  p.addToQueue({ id: tid(201), title: 'mix' }, { auto: true });
  p.addToQueue({ id: tid(202), title: 'mix' }, { auto: true });
  p.setRadioActive(false);
  p.clearAutoUpcoming();
  await pressNext(p, 299);
  assert.equal(new Set(p.env.plays).size, 300, 'mix rows that never played were kept out of the pass');
});

test('the radio mix never jumps ahead of a hand-queued "play next", and rows dragged away do not count', async () => {
  const p = makePlayer({});
  p.setQueue(rows(0, 80), 0);
  p.playNext({ id: 'hand', title: 'Mine' });
  p.setRadioActive(true);
  p.addToQueue({ id: 'mix-1', title: 'mix' }, { auto: true });
  assert.deepEqual(p.ids().slice(1, 3), ['hand', 'mix-1']);
  for (let i = 2; i <= 5; i++) p.addToQueue({ id: 'mix-' + i, title: 'mix' }, { auto: true });
  assert.equal(p.autoAhead, 5);
  p.moveInQueue(6, 70);                                  // the listener drags mix-5 far down
  assert.equal(p.autoAhead, 4, 'a mix row 60 rows away still counted → the radio would never refill');
  assert.deepEqual(p.autoUpcoming.map((t) => t.id), ['mix-1', 'mix-2', 'mix-3', 'mix-4']);
});

test('an ordered view with no known total ends on a SHORT page, not on a full one', async () => {
  const server = makeServer(600);
  const p = makePlayer({ server });
  p.setQueue(rows(0, 100), 0, { source: { ordered: { url: '/api/tracks', params: {} }, shuffle: null, total: null, label: 'x', offset: 0 } });
  await p.settle();
  await pressNext(p, 599);
  assert.equal(p.current.id, tid(599));
  assert.equal(p.st.ended, true);
});

test('"the order has ended" survives a reload (no stuck "loading…" row)', async () => {
  const server = makeServer(60);
  const storage = makeStorage();
  const a = makePlayer({ server, storage });
  a.setQueue(rows(0, 60), 59, { source: { ...SOURCE(60), offset: 0 } });
  await a.settle();
  assert.equal(a.st.ended, true);
  const b = makePlayer({ server, storage });
  await b.settle();
  assert.equal(b.st.ended, true);
});

// ── unplayable tracks (the auto-skip lives in the third [queue-core] region) ──

test('a dealt track that cannot play is skipped; the third failure in a row stops with one clear message', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  await p.playSource(SOURCE(3000), { shuffle: true });
  await p.settle();
  const first = p.current.id;
  p.failCurrent(); await p.settle();
  assert.notEqual(p.current.id, first, 'a dead track ended the shuffle session');
  const second = p.current.id;
  p.failCurrent(); await p.settle();
  assert.notEqual(p.current.id, second);
  const third = p.current.id;
  p.failCurrent(); await p.settle();
  assert.equal(p.current.id, third, 'kept skipping through a dead source');
  assert.equal(p.env.toasts.filter(([k, m]) => k === 'error' && /in a row/.test(m)).length, 1);
  // Something plays again → the streak starts over.
  p.next(); await p.settle(); p.nowPlaying();
  const cur = p.current.id;
  p.failCurrent(); await p.settle();
  assert.notEqual(p.current.id, cur);
});

test('a track the listener CLICKED (or went Prev to) is reported but never skipped; "Play all" row 0 is not a click', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 100), 7, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  assert.equal(p.explicitPick, true);
  p.failCurrent(); await p.settle();
  assert.equal(p.current.id, tid(7));
  p.next(); await p.settle();
  assert.equal(p.explicitPick, false);
  p.prev(); await p.settle();
  assert.equal(p.explicitPick, true);
  p.failCurrent(); await p.settle();
  assert.equal(p.current.id, tid(7), 'Prev onto a dead track bounced the listener forward');

  const q = makePlayer({ server: makeServer(3000) });
  q.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 }, explicit: false });   // "Play all"
  await q.settle();
  q.failCurrent(); await q.settle();
  assert.equal(q.current.id, tid(1), '"Play all" died on one dead file');
  assert.equal(await q.playSource(SOURCE(3000), { offset: 0, explicit: false }), true);
  assert.equal(q.explicitPick, false);
});

test('restarting the failed track within the skip window (from ANY start path) cancels the skip', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  await p.playSource(SOURCE(3000), { shuffle: true });
  await p.settle();
  const cur = p.current;
  p.failCurrent();
  p.playTrackDirect(cur);                               // double-click in the library / info-panel Play
  await p.settle();
  assert.equal(p.current.id, cur.id, 'the listener was yanked off the track they had just restarted');
});

test('a failed "Shuffle all" never lights the shuffle button, whichever entry point started it', async () => {
  const server = Object.assign(makeServer(3000), { down: true });
  const p = makePlayer({ server });
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 }, shuffle: true });
  await p.settle();
  assert.equal(p.shuffle, false);
  assert.ok(!p.env.events.some(([n]) => n === 'shufflechange'));
});

test('a new radio session does not hand an ALREADY PLAYED mix row back to the pass', async () => {
  const server = makeServer(300);
  server.place.set(201, 250);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(300), { shuffle: true });
  await p.settle();
  p.setRadioActive(true);
  p.addToQueue({ id: tid(201), title: 'mix' }, { auto: true });
  p.addToQueue({ id: tid(202), title: 'mix' }, { auto: true });
  p.next(); await p.settle();                           // the first mix row plays
  p.setRadioActive(false);
  p.clearAutoUpcoming();
  await pressNext(p, 298);
  assert.equal(p.env.plays.filter((x) => x === tid(201)).length, 1, 'a mix row that had played was dealt again');
});

test('Undo clear settles the order if shuffle was switched while the queue was empty', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  p.setQueue(rows(0, 100), 0, { source: { ...SOURCE(3000), offset: 0 } });
  await p.settle();
  const snap = p.snapshotQueue();
  p.setQueue([], 0);
  p.toggleShuffle();                                    // nothing queued: only the flag flips
  p.restoreQueue(snap); await p.settle();
  assert.equal(p.shuffle, true);
  assert.equal(p.st.mode, 'shuffled', 'button says shuffle, queue plays list order');
});

// ── gaps the fourth-round mutation run exposed ───────────────────────────────

test('a pick made OUTSIDE the queue (similar-tracks row, info panel) that fails is never auto-skipped', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  await p.playSource(SOURCE(3000), { shuffle: true });
  await p.settle();
  p.next(); await p.settle();                           // the queue advanced on its own: not a pick
  assert.equal(p.explicitPick, false);
  const before = p.current.id;
  p.playTrackDirect({ id: 'similar-1', title: 'picked from Similar tracks' });
  assert.equal(p.explicitPick, true);
  p.failCurrent(); await p.settle();
  assert.equal(p.current.id, before, 'the queue moved');
  assert.equal(p.env.plays.at(-1), 'similar-1', 'an unrelated queue row started after the failed pick');
});

test('auto-skip bookkeeping: one skip per failure, a good play resets the streak, nothing to skip to → stay', async () => {
  const p = makePlayer({ server: makeServer(3000) });
  await p.playSource(SOURCE(3000), { shuffle: true });
  await p.settle();
  const at = p.queueIdx;
  p.failCurrent(); p.failCurrent();                     // the element can report twice for one track
  await p.settle();
  assert.equal(p.queueIdx, at + 1, 'two timers fired for one failure');
  p.nowPlaying();                                        // this one plays → streak back to zero
  p.next(); await p.settle();
  p.failCurrent(); await p.settle();
  p.failCurrent(); await p.settle();
  assert.equal(p.env.toasts.filter(([, m]) => /in a row/.test(m)).length, 0, 'stopped early: the streak was not reset by a good play');

  const q = makePlayer({});
  q.setQueue(rows(0, 3), 2, { explicit: false });        // last row, repeat off
  q.failCurrent(); await q.settle();
  assert.equal(q.current.id, tid(2));
  assert.equal(q.env.plays.length, 1);
});

test('queueSource-style "has more": true while pages remain, false at the end, under radio, and after a fresh pass starts', async () => {
  const server = makeServer(120);
  const p = makePlayer({ server });
  p.setQueue(rows(0, 20), 0, { source: { ...SOURCE(120), offset: 0 } });
  assert.equal(p.st.ended, false);
  await p.settle();
  await pressNext(p, 119);
  assert.equal(p.st.ended, true);
  p.toggleRepeat();
  p.next(); await p.settle();                            // wraps: a new pass from the top
  assert.equal(p.current.id, tid(0));
  assert.equal(p.st.ended, false, 'a restarted order still claimed to have ended');
});

test('a page made only of tracks that are already queued pulls the next one straight away', async () => {
  const server = makeServer(300);
  const p = makePlayer({ server });
  await p.playSource(SOURCE(300), { shuffle: true });
  await p.settle();
  const order = server.perms.get(String(p.st.seed));
  for (const i of order.slice(50, 100)) p.addToQueue({ id: tid(i), title: 'queued by hand' });   // = the whole next page
  const n = p.queue.length;
  for (let k = 0; k < 95; k++) { p.next(); await p.settle(); }
  assert.ok(p.queue.length > n, 'the window was never topped up past an all-duplicates page');
  assert.ok(uniq(p.ids()));
});

test('"Shuffle all" of a complete list is not a pick: a dead first track is skipped, a clicked row is not', async () => {
  const p = makePlayer({ server: makeServer(100) });
  p.setQueue(rows(0, 30), 0, { shuffle: true });                 // plain array, dealt by the player
  await p.settle();
  assert.equal(p.explicitPick, false, 'a dealt first track was treated as the listener\'s pick');
  const dead = p.current.id;
  p.failCurrent(); await p.settle();
  assert.notEqual(p.current.id, dead, '"Shuffle all" died on one dead file');

  const q = makePlayer({ server: makeServer(100) });
  q.setQueue(rows(0, 30), 4);                                    // a clicked row
  await q.settle();
  assert.equal(q.explicitPick, true);
  q.failCurrent(); await q.settle();
  assert.equal(q.current.id, tid(4));
});

test('a superseded "Shuffle all" whose request fails LATE resolves null, not false (no stale error toast)', async () => {
  const srv = makeServer(3000);
  const p = makePlayer({ server: srv });
  let failA;
  srv.gate = () => new Promise((_, rej) => { failA = rej; });     // deal A hangs…
  const a = p.playSource(SOURCE(3000), { shuffle: true });
  await p.settle();
  const b = p.playSource(SOURCE(3000), { shuffle: true });        // …the listener clicks again
  assert.equal(await b, true);
  const playing = p.current.id;
  failA(new Error('socket finally gave up'));
  assert.equal(await a, null, 'the abandoned deal reported a failure while the new shuffle was playing');
  assert.equal(p.current.id, playing);
  // …while a request that fails with nothing newer in flight IS a failure.
  srv.down = true;
  assert.equal(await p.playSource(SOURCE(3000), { shuffle: true }), false);
  assert.equal(p.current.id, playing, 'a failed start replaced the live queue');
});

test('a page request that never answers times out: the start fails cleanly, a stalled lookahead is reported and can be retried', async () => {
  const srv = makeServer(3000);
  const p = makePlayer({ server: srv });
  srv.hang = true;
  const start = p.playSource(SOURCE(3000), { shuffle: true });
  await p.settle();
  assert.equal(p.queue.length, 0);
  assert.ok(p.env.signals.every((s) => s.ms >= 5000 && s.ms <= 60000), 'page requests carry no sane timeout');
  assert.ok(p.expireRequests() >= 1, 'the first page was requested without a timeout signal');
  assert.equal(await start, false);
  assert.equal(p.shuffle, false, 'a start that timed out lit the shuffle button');

  srv.hang = false;
  assert.equal(await p.playSource(SOURCE(3000), { shuffle: true }), true);
  await p.settle();
  srv.hang = true;                                   // …now the NEXT page hangs
  while (p.queueIdx < p.queue.length - 1) { p.next(); await p.settle(); }
  p.next(); await p.settle();                        // at the window edge: waits for the pending page
  const stuckAt = p.current.id, len = p.queue.length;
  assert.ok(p.expireRequests() >= 1, 'the lookahead page was requested without a timeout signal');
  await p.settle();
  assert.ok(p.env.events.some(([n]) => n === 'queuestall'), 'a lookahead that timed out was never reported');
  assert.equal(p.current.id, stuckAt);
  srv.hang = false;
  p.next(); await p.settle();                        // Next retries — the latch was cleared
  assert.ok(p.queue.length > len, 'the timed-out lookahead could not be retried');
  assert.notEqual(p.current.id, stuckAt);
});
