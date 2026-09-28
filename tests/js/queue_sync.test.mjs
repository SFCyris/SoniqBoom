// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The web / mobile queue kept on the server (PUT/GET /api/me/play-queue) — the
// REAL code from player.js, run through the queue harness.
// Run: node --test tests/js/queue_sync.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { makePlayer, makeServer, makeStorage, rows } from './player_queue_harness.mjs';

// A signed-in page (the sync is off without one), for the length of a test.
function signedIn(t) {
  globalThis.window = { __sbAuth: { user: { id: 'u' } }, addEventListener() {} };
  t.after(() => { delete globalThis.window; });
}

// The harness server, plus /api/me/play-queue from ``pq`` (``status``, ``get``).
function withQueueEndpoint(srv, pq) {
  const base = srv.fetch;
  pq.puts = [];
  srv.fetch = async (url, opts = {}) => {
    if (url === '/api/me/play-queue') {
      srv.requests.push(`${opts.method || 'GET'} ${url}`);
      if (pq.status && pq.status !== 200) return { ok: false, status: pq.status, json: async () => ({}) };
      if (opts.method === 'PUT') {
        const b = JSON.parse(opts.body);
        pq.puts.push(b);
        // ``okOnly``: the server answers {ok: true} and keeps the stamp to itself.
        pq.saved = { ids: b.ids, current_index: b.current_index, position: b.position,
                     changed: 1000 + pq.puts.length, changed_by: b.client };
        return { ok: true, status: 200, json: async () => (pq.okOnly ? { ok: true } : { changed: pq.saved.changed }) };
      }
      if (pq.okOnly && pq.saved) return { ok: true, status: 200, json: async () => pq.saved };
      return { ok: true, status: 200, json: async () => pq.get || null };
    }
    return base(url, opts);
  };
  return srv;
}

test('the queue sent: library entries around the current one, tune ids, the current index inside that window', async (t) => {
  signedIn(t);
  const pq = {};
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), pq) });
  const list = rows(0, 6);
  list[1] = { ...list[1], subsong: 2 };                 // a pinned tune
  list.splice(2, 0, { id: '', station: true, title: 'Radio' });   // not a library track
  p.setQueue(list, 4);                                   // t00003
  p.env.currentTime = 42.5;
  await p._pushQueueSync();
  assert.equal(pq.puts.length, 1);
  assert.deepEqual(pq.puts[0].ids, ['t00000', 't00001~2', 't00002', 't00003', 't00004', 't00005']);
  assert.equal(pq.puts[0].current_index, 3);
  assert.equal(pq.puts[0].position, 42500);
  assert.equal(pq.puts[0].client, 'SoniqBoom Web');
  assert.equal(p.storage.getItem('sb_queue_sync_own'), '1001', 'this browser\'s save is remembered');

  // A long queue: 50 behind the current entry, 450 ahead of it.
  const p2 = makePlayer({ server: withQueueEndpoint(makeServer(2000), pq) });
  p2.setQueue(rows(0, 2000), 1000);
  await p2._pushQueueSync();
  const last = pq.puts.at(-1);
  assert.equal(last.ids.length, 500);
  assert.equal(last.ids[0], 't00950');
  assert.equal(last.ids[last.current_index], 't01000');
});

test('nothing is sent again until the window, the current entry or the position (to 5 s) changes', async (t) => {
  signedIn(t);
  const pq = {};
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), pq) });
  p.setQueue(rows(0, 5), 0);
  p.env.currentTime = 10;
  await p._pushQueueSync();
  await p._pushQueueSync();                              // a paused tab: same state
  p.env.currentTime = 12;
  await p._pushQueueSync();                              // same 5 s bucket
  assert.equal(pq.puts.length, 1);
  p.env.currentTime = 16;
  await p._pushQueueSync();
  assert.equal(pq.puts.length, 2);
  p.next();
  await p._pushQueueSync();
  assert.equal(pq.puts.length, 3);
  assert.equal(pq.puts.at(-1).current_index, 1);
});

test('a server without the endpoint turns the sync off for the page', async (t) => {
  signedIn(t);
  const pq = { status: 404 };
  const srv = withQueueEndpoint(makeServer(100), pq);
  const p = makePlayer({ server: srv });
  p.setQueue(rows(0, 5), 0);
  await p._offerServerQueue();
  assert.equal(p.queueSyncSupported, false);
  await p._pushQueueSync();
  assert.equal(srv.requests.filter((r) => r.startsWith('PUT')).length, 0);
  assert.equal(p.env.toasts.length, 0);

  // Found out by a save (no offer asked first): one try, then off.
  const srv2 = withQueueEndpoint(makeServer(100), { status: 405 });
  const p2 = makePlayer({ server: srv2 });
  p2.setQueue(rows(0, 5), 0);
  await p2._pushQueueSync();
  p2.next();
  await p2._pushQueueSync();
  assert.equal(srv2.requests.filter((r) => r.startsWith('PUT')).length, 1);
  assert.equal(p2.queueSyncSupported, false);
});

test('switched off in this browser: nothing sent, nothing offered', async (t) => {
  signedIn(t);
  const pq = { get: { ids: ['t00002'], current_index: 0, position: 0, changed: 5e9, changed_by: 'DSub' } };
  const srv = withQueueEndpoint(makeServer(100), pq);
  const p = makePlayer({ server: srv, storage: makeStorage({ sb_queue_sync: '0' }) });
  p.setQueue(rows(0, 5), 0);
  await p._pushQueueSync();
  await p._offerServerQueue();
  assert.equal(srv.requests.filter((r) => r.includes('play-queue')).length, 0);
  assert.equal(p.env.toasts.length, 0);
});

test('a newer queue from another device is offered, and Resume loads it without playing', async (t) => {
  signedIn(t);
  const pq = { get: { ids: ['t00002', 't00003~1', 'gone-id'], current_index: 1, position: 9000,
                      changed: 5e9, changed_by: 'DSub' } };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), pq) });
  await p._offerServerQueue();
  const offer = p.env.toasts.find((x) => x[0] === 'action');
  assert.ok(offer, 'offered');
  assert.equal(offer[1], 'Resume the queue from DSub?');
  assert.equal(offer[2], 'Resume');
  const played = p.env.plays.length;
  await offer[3]();
  await p.settle();
  assert.deepEqual(p.ids(), ['t00002', 't00003'], 'a vanished track is left out');
  assert.equal(p.queue[1].subsong, 1, 'the tune id became a pinned tune');
  assert.equal(p.queueIdx, 1);
  assert.equal(p.env.plays.length, played, 'nothing starts on its own');
  assert.equal(p.resumeAt.entry, p.queue[1]);
  assert.equal(p.resumeAt.sec, 9);
});

test('no offer for this browser\'s own save, an older copy, or the same queue', async (t) => {
  signedIn(t);
  const get = { ids: ['t00000', 't00001'], current_index: 1, position: 0, changed: 1700000000, changed_by: 'DSub' };
  // Saved by this browser.
  let p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }),
                      storage: makeStorage({ sb_queue_sync_own: '1700000000' }) });
  await p._offerServerQueue();
  assert.equal(p.env.toasts.length, 0, 'own save');
  // This browser's queue is newer.
  p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }),
                   storage: makeStorage({ sb_queue: JSON.stringify({ tracks: rows(5, 2), idx: 0, savedAt: 1700000001000 }) }) });
  await p.settle();
  await p._offerServerQueue();
  assert.equal(p.env.toasts.filter((x) => x[0] === 'action').length, 0, 'older copy');
  // The very queue that is here already.
  p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }),
                   storage: makeStorage({ sb_queue: JSON.stringify({ tracks: rows(0, 2), idx: 1, savedAt: 1 }) }) });
  await p.settle();
  await p._offerServerQueue();
  assert.equal(p.env.toasts.filter((x) => x[0] === 'action').length, 0, 'same queue');
});

test('no offer once the listener has started something on this page', async (t) => {
  signedIn(t);
  const get = { ids: ['t00007'], current_index: 0, position: 0, changed: 5e9, changed_by: 'DSub' };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }) });
  p.setQueue(rows(0, 3), 0);                             // a click before the offer came
  await p._offerServerQueue();
  assert.equal(p.queueSyncSupported, true, 'still asked: that is how support is found out');
  assert.equal(p.env.toasts.filter((x) => x[0] === 'action').length, 0);
});

test('a server that answers a save with {ok} only: its stamp is read back once', async (t) => {
  signedIn(t);
  const pq = { okOnly: true };
  const srv = withQueueEndpoint(makeServer(100), pq);
  const p = makePlayer({ server: srv });
  p.setQueue(rows(0, 4), 2);
  await p._pushQueueSync();
  assert.deepEqual(srv.requests.filter((r) => r.includes('play-queue')), ['PUT /api/me/play-queue', 'GET /api/me/play-queue']);
  assert.equal(p.storage.getItem('sb_queue_sync_own'), '1001');
  // …so this browser's next page load does not offer its own queue back.
  const p2 = makePlayer({ server: srv, storage: p.storage });
  await p2._offerServerQueue();
  assert.equal(p2.env.toasts.filter((x) => x[0] === 'action').length, 0);
});

test('a different window around the same current song is not offered', async (t) => {
  signedIn(t);
  // The server holds 50 + 450 around the current song; this browser stored a
  // shorter slice of the same queue.
  const get = { ids: rows(0, 30).map((r) => r.id), current_index: 20, position: 5000, changed: 5e9, changed_by: 'SoniqBoom Web' };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }),
                        storage: makeStorage({ sb_queue: JSON.stringify({ tracks: rows(15, 10), idx: 5, savedAt: 1 }) }) });
  await p.settle();
  await p._offerServerQueue();
  assert.equal(p.env.toasts.filter((x) => x[0] === 'action').length, 0);
});

test('the offer goes once anything starts playing, and its Resume then does nothing', async (t) => {
  signedIn(t);
  const get = { ids: ['t00007', 't00008'], current_index: 1, position: 4000, changed: 5e9, changed_by: 'DSub' };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }) });
  await p._offerServerQueue();
  const offer = p.env.toasts.find((x) => x[0] === 'action');
  assert.ok(offer);
  assert.equal(p.queueOffer, 'DSub', 'the Queue panel entry is open too');
  assert.deepEqual(p.env.events.filter((e) => e[0] === 'queueoffer').map((e) => e[1].who), ['DSub']);
  p.setQueue(rows(0, 3), 0);                             // the listener plays something else
  assert.equal(p.queueOffer, null);
  assert.deepEqual(p.env.dismissed, [offer[4]], 'the toast is closed');
  assert.deepEqual(p.env.events.filter((e) => e[0] === 'queueoffer').map((e) => e[1].who), ['DSub', null]);
  await offer[3]();                                      // a stale click on Resume
  await p.settle();
  assert.deepEqual(p.ids(), ['t00000', 't00001', 't00002'], 'the playing queue is left alone');
  assert.equal(p.resumeQueueOffer(), false);
});

test('Resume from the Queue panel entry loads the queue like the toast does', async (t) => {
  signedIn(t);
  const get = { ids: ['t00004', 't00005'], current_index: 0, position: 0, changed: 5e9, changed_by: 'DSub' };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }) });
  await p._offerServerQueue();
  const offer = p.env.toasts.find((x) => x[0] === 'action');
  assert.equal(p.resumeQueueOffer(), true);
  await p.settle();
  assert.deepEqual(p.ids(), ['t00004', 't00005']);
  assert.equal(p.queueOffer, null, 'the offer is used up');
  assert.deepEqual(p.env.dismissed, [offer[4]], 'and its toast closed');
  assert.ok(p.env.toasts.some((x) => x[0] === 'ok' && /Press Play\.$/.test(x[1])));
});

test('a start while the resumed tracks load wins: the queue is not replaced', async (t) => {
  signedIn(t);
  const get = { ids: ['t00004', 't00005'], current_index: 0, position: 0, changed: 5e9, changed_by: 'DSub' };
  const srv = withQueueEndpoint(makeServer(100), { get });
  const p = makePlayer({ server: srv });
  await p._offerServerQueue();
  let release;
  srv.gate = () => new Promise((r) => { release = r; });   // hold /meta/batch open
  const applying = p._applyServerQueue(get);
  await p.settle();
  p.setQueue(rows(20, 2), 0);                            // a double-click meanwhile
  release();
  await applying;
  await p.settle();
  assert.deepEqual(p.ids(), ['t00020', 't00021']);
  assert.equal(p.env.toasts.filter((x) => x[0] === 'ok').length, 0);
});

test('a queue saved by the same app in a browser of the same kind is "from another browser"', async (t) => {
  signedIn(t);
  const get = { ids: ['t00002'], current_index: 0, position: 0, changed: 5e9, changed_by: 'SoniqBoom Web' };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), { get }) });
  await p._offerServerQueue();
  assert.equal(p.env.toasts.find((x) => x[0] === 'action')[1], 'Resume the queue from another browser?');
});

test('the saving client names the browser and OS, within the server\'s 64 characters', (t) => {
  const had = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const setUA = (ua) => Object.defineProperty(globalThis, 'navigator', { value: { userAgent: ua }, configurable: true });
  t.after(() => { if (had) Object.defineProperty(globalThis, 'navigator', had); else delete globalThis.navigator; delete globalThis.window; });
  const p = makePlayer();
  setUA('Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:130.0) Gecko/20100101 Firefox/130.0');
  assert.equal(p.qsyncClient(), 'SoniqBoom Web · Firefox on Mac');
  setUA('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 Edg/128.0');
  assert.equal(p.qsyncClient(), 'SoniqBoom Web · Edge on Windows');
  setUA('Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1');
  globalThis.window = { __sbClientLabel: 'SoniqBoom Mobile' };
  assert.equal(p.qsyncClient(), 'SoniqBoom Mobile · Safari on iPhone');
  globalThis.window = { __sbClientLabel: 'x'.repeat(80) };
  assert.equal(p.qsyncClient().length, 60);
  setUA('Node.js/22');
  globalThis.window = {};
  assert.equal(p.qsyncClient(), 'SoniqBoom Web', 'an unknown browser adds nothing');
});

test('after Resume the player knows what Play starts and where; a start or radio clears it', async (t) => {
  signedIn(t);
  const pq = { get: { ids: ['t00002', 't00003'], current_index: 1, position: 9000, changed: 5e9, changed_by: 'DSub' } };
  const p = makePlayer({ server: withQueueEndpoint(makeServer(100), pq) });
  await p._offerServerQueue();
  await p.env.toasts.find((x) => x[0] === 'action')[3]();
  await p.settle();
  assert.equal(p.cuedTrack, p.queue[1]);
  assert.equal(p.cuedSec, 9);
  const cue = p.env.events.filter((e) => e[0] === 'cue').at(-1);
  assert.ok(cue, "'cue' emitted");
  assert.equal(cue[1].track, p.queue[1]);
  assert.equal(cue[1].sec, 9);
  // Radio took the output: its takeover leaves trackId on the entry, src gone.
  p.setExtAudio(true);
  assert.equal(p.cuedTrack, null);
  p.setExtAudio(false);
  p.setTrackId(p.queue[1].id);
  assert.equal(p.cuedTrack, null, 'a start is under way (or radio detached it)');
  p.setTrackId(null);
  p.setAudioSrc('/api/stream/x');
  assert.equal(p.cuedTrack, null, 'something is loaded');
  p.setAudioSrc('');
  assert.equal(p.cuedTrack, p.queue[1]);
  p.setQueue(p.queue, 1);                                // Play
  assert.equal(p.cuedTrack, null);
  assert.equal(p.cuedSec, 0);
});

test('a queue restored after a reload is cued at its current entry, from the start', async () => {
  const p = makePlayer({ storage: makeStorage({ sb_queue: JSON.stringify({ tracks: rows(0, 3), idx: 2, savedAt: 1 }) }) });
  await p.settle();
  const cue = p.env.events.find((e) => e[0] === 'cue');
  assert.ok(cue);
  assert.equal(cue[1].track.id, 't00002');
  assert.equal(cue[1].sec, 0);
  assert.equal(p.cuedTrack, p.queue[2]);
});
