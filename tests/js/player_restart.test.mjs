// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The web player while the server restarts (every request answered 503 +
// Retry-After, then no answer for a moment): a start that fails then is
// waited out and the same track played again — no force_transcode retry, no
// failure toast, no skip.  Runs the REAL helpers from player.js
// (render_core_harness).
import test from 'node:test';
import assert from 'node:assert/strict';
import { makeCore, makeServer } from './render_core_harness.mjs';

const flac = (over = {}) => ({ id: 'f1', title: 'Song', format: 'FLAC', duration: 200, ...over });

function resp(status, headers = {}, body = {}) {
  const h = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { ok: status >= 200 && status < 300, status, headers: { get: (k) => h[k.toLowerCase()] ?? null },
           json: async () => body };
}

// A server that is restarting: streams answer 503 + Retry-After, /api/health
// answers 503 (refusing), then fails (exec'ing), then answers 200.
function restartingServer({ retryAfter = '2', healthSeq = ['503', 'down', 'down', 'ok'] } = {}) {
  const srv = makeServer();
  const health = healthSeq.slice();
  srv.streamStatus = 503;
  srv.fetch = async (url) => {
    srv.requests.push(url);
    if (/^\/api\/stream\//.test(url)) {
      return srv.streamStatus === 503 ? resp(503, retryAfter ? { 'Retry-After': retryAfter } : {},
                                             { detail: 'The server is restarting.' })
        : resp(206, { 'Content-Range': 'bytes 0-0/100' });
    }
    if (url === '/api/health') {
      const h = health.length > 1 ? health.shift() : health[0];
      if (h === 'down') throw new TypeError('Failed to fetch');
      return h === 'ok' ? resp(200) : resp(503, { 'Retry-After': '2' });
    }
    return resp(200);
  };
  return srv;
}

test('Retry-After is read only from a 503', () => {
  const c = makeCore();
  assert.equal(c._restartRetryAfter(resp(503, { 'Retry-After': '2' })), 2);
  assert.equal(c._restartRetryAfter(resp(503, { 'Retry-After': '120' })), 30);
  assert.equal(c._restartRetryAfter(resp(503)), 0);
  assert.equal(c._restartRetryAfter(resp(200, { 'Retry-After': '2' })), 0);
  assert.equal(c._restartRetryAfter(resp(503, { 'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT' })), 0);
  assert.equal(c._restartRetryAfter(null), 0);
});

test('a start that fails while the server restarts plays the same track once it is back', async () => {
  const server = restartingServer();
  const c = makeCore({ server });
  const t = flac();
  const seq = c.start(t);
  c.audio.src = '/api/stream/f1';
  c._reportPlayFailure(t, 'Song', { name: 'NotSupportedError' }, seq);
  await c.settle();
  assert.equal(c._restartWaitFor(seq), true);
  assert.deepEqual(c.env.toasts.map(([k]) => k), ['warn']);           // the notice, no failure toast
  server.streamStatus = 206;                                            // (back up by the time it reloads)
  await c.run(8);
  assert.equal(c._restartWaitFor(seq), false);
  assert.ok(/^\/api\/stream\/f1\?r=/.test(c.audio.src), c.audio.src);   // the same track, fetched anew
  assert.ok(!/force_transcode/.test(c.audio.src));
  assert.equal(c.audio.plays, 1);
  assert.equal(c.env.toasts.filter(([k]) => k === 'error').length, 0);
  assert.ok(server.requests.filter((u) => u === '/api/health').length >= 4);
});

test('a 503 without Retry-After is still reported (not a restart)', async () => {
  const server = restartingServer({ retryAfter: null });
  const c = makeCore({ server });
  const t = flac();
  const seq = c.start(t);
  c._reportPlayFailure(t, 'Song', { name: 'NotSupportedError' }, seq);
  await c.run(2);
  assert.equal(c._restartWaitFor(seq), false);
  const errs = c.env.toasts.filter(([k]) => k === 'error').map(([, m]) => m);
  assert.equal(errs.length, 1);
  assert.match(errs[0], /Server busy|restarting/);
  assert.equal(c.audio.plays, 0);
});

test('a probe of the failed element source tells a restart', async () => {
  const c = makeCore({ server: restartingServer() });
  assert.equal(await c._probeRestarting('/api/stream/f1'), 2);
  const c2 = makeCore({ server: restartingServer({ retryAfter: null }) });
  assert.equal(await c2._probeRestarting('/api/stream/f1'), 0);
});

test('a mid-track failure resumes where playback was', async () => {
  const c = makeCore({ server: restartingServer({ healthSeq: ['ok'] }) });
  const t = flac();
  const seq = c.start(t);
  assert.equal(c._waitServerRestart(t, seq, '/api/stream/f1', 93.5, 2), true);
  assert.equal(c._waitServerRestart(t, seq, '/api/stream/f1', 93.5, 2), true);   // once per start
  assert.equal(c.env.toasts.length, 1);
  await c.run(3);
  assert.equal(c.audio.plays, 1);
  c.audio.fire('loadedmetadata');
  assert.equal(c.audio.currentTime, 93.5);
});

test('another start during the wait cancels the reload', async () => {
  const c = makeCore({ server: restartingServer({ healthSeq: ['down', 'down', 'ok'] }) });
  const t = flac();
  const seq = c.start(t);
  c._waitServerRestart(t, seq, '/api/stream/f1', 0, 2);
  await c.tick();
  c.start(flac({ id: 'f2', title: 'Other' }));
  c.audio.src = '/api/stream/f2';
  await c.run(5);
  assert.equal(c.audio.src, '/api/stream/f2');
  assert.equal(c.audio.plays, 0);
  assert.equal(c._restartWaitFor(seq), false);
});

test('a stale start never begins a wait', () => {
  const c = makeCore({ server: restartingServer() });
  const t = flac();
  const seq = c.start(t);
  c.start(flac({ id: 'f2' }));
  assert.equal(c._waitServerRestart(t, seq, '/api/stream/f1', 0, 2), false);
  assert.equal(c.env.toasts.length, 0);
});

// The new process isn't listening yet right after the refusals: no answer at all.
function downServer({ healthSeq = ['down', 'down', 'ok'] } = {}) {
  const srv = restartingServer({ healthSeq });
  srv.streamStatus = 'down';
  const inner = srv.fetch;
  srv.fetch = async (url, o) => {
    if (/^\/api\/stream\//.test(url) && srv.streamStatus === 'down') {
      srv.requests.push(url);
      throw new TypeError('Failed to fetch');
    }
    return inner(url, o);
  };
  return srv;
}

test('unreachable right after the server said it restarts: waited out too', async () => {
  const server = downServer();
  const c = makeCore({ server });
  c.noteServerRestart();                               // the library socket's event
  const t = flac();
  const seq = c.start(t);
  c._reportPlayFailure(t, 'Song', { name: 'NotSupportedError' }, seq);
  await c.settle();
  assert.equal(c._restartWaitFor(seq), true);
  assert.equal(c.env.toasts.filter(([k]) => k === 'error').length, 0);
  server.streamStatus = 206;
  await c.run(8);
  assert.equal(c.audio.plays, 1);
  assert.ok(/^\/api\/stream\/f1\?r=/.test(c.audio.src));
  assert.equal(await makeCore({ server: downServer() })._probeRestarting('/api/stream/f1'), 0);
});

test('unreachable with no restart said: reported as before', async () => {
  const c = makeCore({ server: downServer() });
  const t = flac();
  const seq = c.start(t);
  c._reportPlayFailure(t, 'Song', { name: 'NotSupportedError' }, seq);
  await c.run(2);
  assert.equal(c._restartWaitFor(seq), false);
  const errs = c.env.toasts.filter(([k]) => k === 'error').map(([, m]) => m);
  assert.equal(errs.length, 1);
  assert.match(errs[0], /reach the server/);
});

test('one wait per start: a failure after the retry is reported, not waited again', async () => {
  const c = makeCore({ server: restartingServer({ healthSeq: ['ok'] }) });
  const t = flac();
  const seq = c.start(t);
  assert.equal(c._waitServerRestart(t, seq, '/api/stream/f1', 0, 2), true);
  await c.run(3);
  assert.equal(c.audio.plays, 1);
  assert.equal(c._waitServerRestart(t, seq, '/api/stream/f1', 0, 2), false);
  const seq2 = c.start(t);                              // a new start may wait again
  assert.equal(c._waitServerRestart(t, seq2, '/api/stream/f1', 0, 2), true);
});

// The transport button, the now-playing bars and the phone's mini player all
// follow 'statechange': the wait says "not playing", so playing again must
// say "playing" — else the UI shows Paused and the next press pauses.
const states = (c) => c.env.events.filter(([n]) => n === 'statechange').map(([, d]) => d.playing);

test('playing again after the wait says so (statechange playing: true)', async () => {
  const server = restartingServer();
  const c = makeCore({ server });
  const t = flac();
  const seq = c.start(t);
  c.audio.src = '/api/stream/f1';
  c._reportPlayFailure(t, 'Song', { name: 'NotSupportedError' }, seq);
  await c.settle();
  assert.ok(states(c).length > 0 && !states(c).includes(true));       // waiting: not playing
  const n = states(c).length;
  server.streamStatus = 206;
  await c.run(8);
  assert.equal(c.audio.plays, 1);
  assert.deepEqual(states(c).slice(n), [true]);                         // playing again
});

test('a mid-track resume says playing too; a start replaced meanwhile does not', async () => {
  const c = makeCore({ server: restartingServer({ healthSeq: ['ok'] }) });
  const t = flac();
  const seq = c.start(t);
  c._waitServerRestart(t, seq, '/api/stream/f1', 40, 2);
  await c.run(3);
  assert.deepEqual(states(c), [false, true]);

  const c2 = makeCore({ server: restartingServer({ healthSeq: ['ok'] }) });
  const seq2 = c2.start(t);
  c2._waitServerRestart(t, seq2, '/api/stream/f1', 0, 2);
  let release;
  c2.audio.play = () => { c2.audio.plays++; return new Promise((r) => { release = r; }); };
  await c2.run(3);
  assert.equal(c2.audio.plays, 1);
  c2.start(flac({ id: 'f2' }));                                          // another track started
  release();
  await c2.settle();
  assert.deepEqual(states(c2), [false]);                                 // no stale "playing"
});

test('a blocked autoplay after the wait is said, and the UI stays Paused', async () => {
  const c = makeCore({ server: restartingServer({ healthSeq: ['ok'] }) });
  c.audio.playError = Object.assign(new Error('blocked'), { name: 'NotAllowedError' });
  const t = flac();
  const seq = c.start(t);
  c._waitServerRestart(t, seq, '/api/stream/f1', 0, 2);
  await c.run(3);
  assert.deepEqual(states(c), [false]);
  assert.ok(c.env.toasts.some(([k, m]) => k === 'error' && /autoplay/.test(m)));
});
