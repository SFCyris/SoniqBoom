// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Rendered-track helpers of player.js (see render_core_harness.mjs).
// Run: node --test tests/js/render_core.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { makeCore, makeServer, makeAudio } from './render_core_harness.mjs';

const amiga = (over = {}) => ({ id: 'a1', title: 'Intro', format: 'TFMX', duration: 0, ...over });
const sid = (over = {}) => ({ id: 's1', title: 'Uridium', format: 'SID', duration: 180, ...over });

// ── URLs carry the tune that is playing ───────────────────────────────────────
test('prewarm asks for the queued tune, not the file default', () => {
  const c = makeCore();
  const u2 = new URL(c._prewarmUrl({ id: 'x', subsong: 2, path: '/m/a.sid' }, true), 'http://h');
  assert.equal(u2.pathname, '/api/stream/x/prewarm');
  assert.equal(u2.searchParams.get('subsong'), '2');
  assert.equal(u2.searchParams.get('priority'), 'next');
  assert.equal(u2.searchParams.get('path'), '/m/a.sid');
  // a picked first tune is named (0); a plain play of the file names none —
  // the server then warms its default tune
  const u0 = new URL(c._prewarmUrl({ id: 'x', subsong: 0 }, false), 'http://h');
  assert.equal(u0.searchParams.get('subsong'), '0');
  for (const subsong of [undefined, null, '0']) {
    const u = new URL(c._prewarmUrl({ id: 'x', subsong }, false), 'http://h');
    assert.equal(u.searchParams.has('subsong'), false, `subsong=${subsong}`);
    assert.equal(u.searchParams.get('priority'), 'ahead');
  }
});

test('prewarms carry one per-page tag, distinct between pages', () => {
  const a = makeCore(), b = makeCore();
  const tagOf = (c, t) => new URL(c._prewarmUrl(t, true), 'http://h').searchParams.get('pw');
  assert.match(a.prewarmId, /^[0-9a-z]{8,32}$/);
  assert.equal(tagOf(a, { id: 'x' }), a.prewarmId);
  assert.equal(tagOf(a, { id: 'y', subsong: 3 }), a.prewarmId, 'same page, same tag');
  assert.notEqual(a.prewarmId, b.prewarmId, 'another tab gets its own tag');
});

test('render-status of the current track names its tune', () => {
  const c = makeCore();
  c.start(sid({ subsong: 3 }));
  assert.equal(c._renderStatusUrl('s1'), '/api/stream/s1/render-status?subsong=3');
  assert.equal(c._renderStatusUrl('other'), '/api/stream/other/render-status');   // not the current track
  c.start(sid({ subsong: 0 }));                       // a picked first tune
  assert.equal(c._renderStatusUrl('s1'), '/api/stream/s1/render-status?subsong=0');
  c.start(sid({}));                                   // a plain play: the default tune
  assert.equal(c._renderStatusUrl('s1'), '/api/stream/s1/render-status');
});

// ── Render recovery ──────────────────────────────────────────────────────────
test('recovery polls the tune that failed and plays once the render is complete', async () => {
  const srv = makeServer([{ state: 'rendering' }, { state: 'complete' }]);
  const c = makeCore({ server: srv });
  const seq = c.start(amiga({ subsong: 2 }));
  c.audio.onPlay = () => { c.audio.duration = 95; };   // the complete render has a real length
  let gaveUp = false;
  assert.equal(c._startRenderRecovery(amiga({ subsong: 2 }), seq, () => { gaveUp = true; }), true);
  assert.equal(c.env.badge.label, 'Rendering…');
  await c.run();
  assert.ok(srv.requests.length >= 2);
  assert.ok(srv.requests.every((u) => u === '/api/stream/a1/render-status?subsong=2'), srv.requests.join());
  assert.equal(gaveUp, false);
  assert.equal(c.audio.plays, 1);
  assert.equal(c.env.badge.shown, false);
});

test('recovery after a mid-tune error resumes where playback was, not at 0:00', async () => {
  const c = makeCore({ server: makeServer([{ state: 'complete' }]) });
  const t = amiga({ duration: 240 });
  const seq = c.start(t);
  c.audio.currentTime = 130;               // the error lands 2:10 in
  c.audio.onPlay = () => { c.audio.duration = 240; c.audio.fire('loadedmetadata'); };
  c._startRenderRecovery(t, seq, () => {});
  await c.run();
  assert.equal(c.audio.plays, 1);
  assert.ok(Math.abs(c.audio.currentTime - 130) < 0.01, `landed at ${c.audio.currentTime}`);
});

test('recovery of a first-play failure starts at 0; a pending seek wins', async () => {
  const c1 = makeCore({ server: makeServer([{ state: 'complete' }]) });
  const t = amiga({ duration: 240 });
  let seq = c1.start(t);
  c1.audio.onPlay = () => { c1.audio.duration = 240; c1.audio.fire('loadedmetadata'); };
  c1._startRenderRecovery(t, seq, () => {});
  await c1.run();
  assert.equal(c1.audio.currentTime, 0);

  const c2 = makeCore({ server: makeServer([{ state: 'complete' }]) });
  seq = c2.start(t);
  c2.audio.currentTime = 12;
  c2.pendingSeek = 50;                      // the listener clicked the timeline meanwhile
  c2.audio.onPlay = () => { c2.audio.duration = 240; c2.audio.fire('loadedmetadata'); };
  c2._startRenderRecovery(t, seq, () => {});
  await c2.run();
  assert.ok(Math.abs(c2.audio.currentTime - 50) < 0.01, `landed at ${c2.audio.currentTime}`);
});

test('recovery reports through giveUp when the render has nothing', async () => {
  const c = makeCore({ server: makeServer([{ state: 'idle' }]) });
  const t = amiga();
  const seq = c.start(t);
  let gaveUp = 0;
  c._startRenderRecovery(t, seq, () => { gaveUp++; });
  await c.run();
  assert.equal(gaveUp, 1);
  assert.equal(c.audio.plays, 0);
  // …and only once per attempt: a second failure in the same attempt is not recovered.
  assert.equal(c._startRenderRecovery(t, seq, () => { gaveUp++; }), false);
});

// ── The badge × ──────────────────────────────────────────────────────────────
test('cancel stops a pending start for good: no late play, no report', async () => {
  const srv = makeServer([{ state: 'rendering' }, { state: 'rendering' }, { state: 'complete' }]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  let gaveUp = 0;
  c._startRenderRecovery(t, seq, () => { gaveUp++; });
  await c.tick();
  c._cancelPendingStart();
  assert.equal(c.seq, seq + 1);
  assert.equal(c.cancelled, true);
  assert.equal(c.env.badge.shown, false);
  assert.equal(c.audio.src, '');                     // the stream request is dropped
  assert.equal(c.audio.paused, true);
  assert.deepEqual(c.env.events.at(-1), ['statechange', { playing: false }]);
  await c.run();
  assert.equal(c.audio.plays, 0, 'the render finishing later must not start the track');
  assert.equal(gaveUp, 0);
  // A late media error on the cancelled attempt gets no second recovery.
  assert.equal(c._startRenderRecovery(t, c.seq, () => { gaveUp++; }), false);
});

// ── Unknown-length render played while it grows ─────────────────────────────
test('while provisional the length is unknown; on completion it is exact and the element moves to the cached file', async () => {
  const srv = makeServer([
    { state: 'ready_for_playback', provisional: true },
    { state: 'ready_for_playback', provisional: true },
    { state: 'complete', duration_seconds: 123.4 },
  ]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.paused = false;
  c.audio.duration = 6 * 3600;                // what a provisional header claims
  c._watchRenderLength(t, seq);
  await c.settle();                            // first answer: provisional
  assert.equal(c.provisional, true);
  assert.equal(c._duration(), 0, 'no bogus length on the seek bar');
  await c.tick();                              // second answer: still provisional
  assert.equal(c.provisional, true);
  c.audio.currentTime = 47;
  await c.tick();                              // → complete: swap requested
  assert.equal(c.provisional, false);
  assert.equal(c.metaDuration, 123.4);
  // A fresh URL for the finished file: the plain one is what served the growing stream.
  assert.match(c.audio.src, /^\/api\/stream\/a1\?r=[0-9a-z]+$/);
  assert.equal(c.audio.paused, true);          // held until the cached file can play
  c.audio.duration = 123.4;
  c.audio.fire('canplay');
  await c.run(2);
  assert.ok(Math.abs(c.audio.currentTime - 47) < 0.01, `resumed at ${c.audio.currentTime}`);
  assert.equal(c.audio.plays, 1);
  assert.equal(c._duration(), 123.4);
  assert.ok(srv.requests.every((u) => u === '/api/stream/a1/render-status'));
});

test('a growing render is recognised by its header length when the server sends no flag', async () => {
  const srv = makeServer([{ state: 'ready_for_playback' }, { state: 'complete' }]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.paused = false;
  c.audio.duration = 0xFFFFFFFF / 176400;      // a "read to the end" WAV header: ~6.8 h
  c._watchRenderLength(t, seq);
  await c.settle();
  assert.equal(c.provisional, true);
  assert.equal(c._duration(), 0);
  c.audio.currentTime = 20;
  await c.tick();                              // → complete
  assert.equal(c.provisional, false);
  c.audio.duration = 88.2;                     // the finished file
  c.audio.fire('canplay');
  await c.run(2);
  assert.ok(Math.abs(c.audio.currentTime - 20) < 0.01);
  assert.equal(c.audio.plays, 1);
  assert.equal(c._duration(), 88.2);
});

test('a track that is not a growing render costs one request and changes nothing', async () => {
  const srv = makeServer([{ state: 'complete' }]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.duration = 95;
  c._watchRenderLength(t, seq);
  await c.run();
  assert.equal(srv.requests.length, 1);
  assert.equal(c.audio.loads, 0);
  assert.equal(c.audio.plays, 0);
  assert.equal(c._duration(), 95);

  // Nothing rendering (idle, failed): one request, nothing changes.
  for (const state of ['idle', 'failed', 'queued']) {
    const srv1 = makeServer([{ state }]);
    const c1 = makeCore({ server: srv1 });
    c1.audio.src = '/api/stream/a1';
    c1.audio.duration = 200;
    c1._watchRenderLength(amiga({ duration: 200 }), c1.start(amiga({ duration: 200 })));
    await c1.run();
    assert.equal(srv1.requests.length, 1, state);
    assert.equal(c1.provisional, false);
    assert.equal(c1.audio.loads, 0);
    assert.equal(c1._duration(), 200);
  }

  // SID, or a native track: not even that one request.
  const srv2 = makeServer([{ state: 'complete' }]);
  const c2 = makeCore({ server: srv2 });
  for (const tt of [sid({ duration: 0 }), { id: 'n', format: 'FLAC', duration: 0 }]) {
    c2._watchRenderLength(tt, c2.start(tt));
  }
  await c2.run();
  assert.equal(srv2.requests.length, 0);
});

// Rows that DO carry a length, served as a render of unknown length.  Two
// server flavours: the uade contract (``provisional`` while it grows, the
// exact ``duration_seconds`` once complete) and ``state`` alone — then the
// element's provisional header is what marks the growing render.
const FLAVOURS = {
  'uade flags': [{ state: 'ready_for_playback', provisional: true },
                 { state: 'ready_for_playback', provisional: true },
                 { state: 'complete', provisional: false, duration_seconds: 199.7 }],
  'state only': [{ state: 'ready_for_playback' }, { state: 'ready_for_playback' }, { state: 'complete' }],
};
for (const [name, row] of [
  ['another tune of a file whose row carries the default tune\'s length',
   { subsong: 2, duration: 143.4 }],
  ['a long tune the server serves with an unknown length',
   { duration: 512 }],
]) for (const [flavour, answers] of Object.entries(FLAVOURS)) {
  test(`${name} (${flavour}): unknown while it grows, exact once complete`, async () => {
    const t = amiga(row);
    const srv = makeServer(answers);
    const c = makeCore({ server: srv });
    const seq = c.start(t);
    c.audio.src = '/api/stream/a1';
    c.audio.paused = false;
    c.audio.duration = 0xFFFFFFFF / 176400;    // ~6.8 h: a "read to the end" header
    assert.equal(c._duration(), 0, 'no hours-long bar before the watch answers');
    c._watchRenderLength(t, seq);
    await c.settle();
    assert.equal(c.provisional, true);
    assert.equal(c._duration(), 0, 'nor the row\'s stored length while it grows');
    await c.tick();
    c.audio.currentTime = 150;                 // past the default tune's 143.4 s
    await c.tick();                            // → complete: onto the finished file
    assert.equal(c.provisional, false);
    assert.match(c.audio.src, /^\/api\/stream\/a1\?r=/);
    c.audio.duration = 199.7;
    c.audio.fire('canplay');
    await c.run(2);
    assert.ok(Math.abs(c.audio.currentTime - 150) < 0.01, `resumed at ${c.audio.currentTime}`);
    assert.equal(c._duration(), 199.7);
    assert.equal(c._knownLength(), 199.7);
    if (flavour === 'uade flags' && t.subsong > 0) {
      assert.equal(t.duration, 199.7, 'the tune\'s queue entry shows its own length');
    } else {
      assert.equal(t.duration, t.subsong > 0 ? 143.4 : 512, 'nothing else is rewritten');
    }
    const sub = Number(t.subsong) > 0 ? `?subsong=${t.subsong}` : '';
    assert.ok(srv.requests.every((u) => u === `/api/stream/a1/render-status${sub}`), srv.requests.join());
  });
}

test('a provisional header is never reported as the track length', async () => {
  // loadedmetadata fires BEFORE the watch's first answer: the length the
  // player reports (durationknown → the library row) must already be none.
  const c = makeCore({ server: makeServer([{ state: 'ready_for_playback' }, { state: 'complete' }]) });
  const t = amiga({ duration: 95 });
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.paused = false;
  c.audio.duration = 24347.887;                // what Firefox reported
  assert.equal(c._knownLength(), 0);
  c.audio.duration = Infinity;                 // Chromium on a length-less stream
  assert.equal(c._knownLength(), 0);
  c.audio.duration = 0xFFFFFFFF / 176400;
  c._watchRenderLength(t, seq);
  await c.settle();
  assert.equal(c.provisional, true);
  assert.equal(c._knownLength(), 0, 'nor while the watch holds it provisional');
  await c.tick();                              // → complete, swap
  c.audio.duration = 15.3;
  c.audio.fire('canplay');
  await c.run(2);
  assert.equal(c._knownLength(), 15.3, 'the finished file\'s length is reported');
  // A native track is never second-guessed, however long.
  const n = makeCore();
  n.start({ id: 'n', format: 'FLAC', duration: 0 });
  n.audio.duration = 4 * 3600;
  assert.equal(n._knownLength(), 4 * 3600);
  assert.equal(n._duration(), 4 * 3600);
});

test('the swap is a no-op once the listener has moved on', async () => {
  const c = makeCore({ server: makeServer([{ state: 'ready_for_playback', provisional: true }, { state: 'complete', duration_seconds: 99 }]) });
  const t = amiga();
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.paused = false;
  c._watchRenderLength(t, seq);
  await c.settle();
  assert.equal(c.provisional, true);
  c.start(amiga({ id: 'a2' }));                // next track
  c.audio.src = '/api/stream/a2';
  await c.run();
  assert.equal(c.audio.src, '/api/stream/a2');
  assert.equal(c.audio.plays, 0);

  // …and a station taking over while the swap waits for canplay.
  const c3 = makeCore({ server: makeServer([{ state: 'complete', duration_seconds: 99 }]) });
  const seq3 = c3.start(t);
  c3.audio.src = '/api/stream/a1';
  c3.audio.paused = false;
  c3.audio.currentTime = 30;
  const p = c3._switchToCachedRender(30);
  c3.station = true;
  c3.audio.fire('canplay');
  await p;
  assert.equal(c3.audio.plays, 0);
  assert.equal(seq3, c3.seq);
});

test('the end-of-partial hand-off plays on from an ended element; a paused one stays paused', async () => {
  const c = makeCore();
  const seq = c.start(sid());
  c.audio.src = '/api/stream/s1';
  c.audio.paused = true;                       // 'ended' leaves the element paused
  const p = c._switchToCachedRender(60, { play: true });
  assert.equal(c.audio.loads, 1, 'preload="none" element: the new source must be loaded explicitly');
  assert.equal(c.audio.src, '/api/stream/s1');
  c.audio.fire('canplay');
  await p;
  assert.equal(c.audio.currentTime, 60);
  assert.equal(c.audio.plays, 1);
  assert.equal(c.seq, seq);

  const c2 = makeCore();
  c2.start(sid());
  c2.audio.paused = true;                      // the listener had paused: stay paused
  const p2 = c2._switchToCachedRender(30);
  c2.audio.fire('canplay');
  await p2;
  assert.equal(c2.audio.currentTime, 30);
  assert.equal(c2.audio.plays, 0);
});

// ── A growing render under a KNOWN (stored) length ──────────────────────────
test('a growing render under a correct known length is watched to the end and left alone', async () => {
  const srv = makeServer([{ state: 'ready_for_playback' }, { state: 'ready_for_playback' },
                          { state: 'complete', duration_seconds: 200.4 }]);
  const c = makeCore({ server: srv });
  const t = amiga({ duration: 200 });
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.paused = false;
  c.audio.duration = 200;                      // an exact header from the stored length
  c._watchRenderLength(t, seq);
  await c.settle();
  assert.equal(c.provisional, false, 'a known length keeps seeking on while it grows');
  assert.equal(c._duration(), 200);
  await c.run();
  assert.equal(srv.requests.length, 3, 'asked again until complete');
  assert.equal(c.audio.loads, 0, 'within the slack: no reload');
  assert.equal(c.audio.src, '/api/stream/a1');
  assert.equal(c.metaDuration, 200.4);
});

// Stored length real + 20 s: the growing file was padded with 20 s of silence;
// real - 20 s: it would stop 20 s early.  Either way the finished file wins.
for (const [what, stored] of [['padded', 119.55 + 20], ['cut short', 119.55 - 20]]) {
  test(`a stale stored length (${what}) is corrected once the render is complete`, async () => {
    const srv = makeServer([{ state: 'ready_for_playback', provisional: false },
                            { state: 'complete', provisional: false, duration_seconds: 119.55 }]);
    const c = makeCore({ server: srv });
    const t = amiga({ duration: stored });
    const seq = c.start(t);
    c.audio.src = '/api/stream/a1';
    c.audio.paused = false;
    c.audio.duration = stored;                 // the header the stored length built
    c._watchRenderLength(t, seq);
    await c.settle();
    assert.equal(c.provisional, false);
    c.audio.currentTime = 42;
    await c.tick();                            // → complete: 20 s off → onto the finished file
    assert.match(c.audio.src, /^\/api\/stream\/a1\?r=[0-9a-z]+$/, 'a fresh URL for the finished file');
    c.audio.duration = 119.55;
    c.audio.fire('canplay');
    await c.run(2);
    assert.ok(Math.abs(c.audio.currentTime - 42) < 0.01, `resumed at ${c.audio.currentTime}`);
    assert.equal(c.audio.plays, 1);
    assert.equal(c._duration(), 119.55);
    assert.equal(c.metaDuration, 119.55);
  });
}

test('a listener already in the padding lands just before the real end, so the track ends naturally', async () => {
  const srv = makeServer([{ state: 'complete', duration_seconds: 119.55 }]);
  const c = makeCore({ server: srv });
  const t = amiga({ duration: 139.55 });
  const seq = c.start(t);
  c.audio.src = '/api/stream/a1';
  c.audio.paused = false;
  c.audio.duration = 139.55;
  c.audio.currentTime = 125;                   // 5.45 s into the padded silence
  c._watchRenderLength(t, seq);
  await c.settle();
  c.audio.duration = 119.55;
  c.audio.fire('canplay');
  await c.run(2);
  assert.ok(Math.abs(c.audio.currentTime - 119.5) < 0.001, `landed at ${c.audio.currentTime}`);
  assert.ok(c.audio.currentTime < c.audio.duration, 'not AT the end: play() there would restart the track');
  assert.equal(c.audio.plays, 1);
});

// ── A failed render names its reason ─────────────────────────────────────────
test('a failed render is reported with the server\'s reason, without a probe', async () => {
  const srv = makeServer([{ state: 'rendering' }, { state: 'failed', error: 'uade123: unknown format.' }]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const seq = c.start(t);
  let reason = null;
  c._startRenderRecovery(t, seq, (why) => { reason = why; c._reportPlayFailure(t, 'Intro', { name: 'NotSupportedError' }, seq, why); });
  await c.run();
  assert.equal(reason, 'uade123: unknown format.');
  assert.deepEqual(c.env.toasts, [['error', 'Couldn’t play “Intro” · TFMX: uade123: unknown format.']]);
  assert.ok(!srv.requests.some((u) => /^\/api\/stream\/a1(\?|$)/.test(u)), `no probe: ${srv.requests.join()}`);
  assert.deepEqual(c.env.events.slice(-2).map((e) => e[0]), ['statechange', 'error']);
  // The <audio> 'error' path of the same attempt stays quiet (one toast per attempt).
  c._reportPlayFailure(t, 'Intro', { code: 4 }, seq, 'again');
  await c.run();
  assert.equal(c.env.toasts.length, 1);
});

test('a render with nothing to say still gets the stream probe', async () => {
  const srv = makeServer([{ state: 'idle' }]);
  srv.onStream = async () => ({ ok: false, status: 404, json: async () => ({ detail: 'File missing on disk.' }) });
  const c = makeCore({ server: srv });
  const t = amiga();
  const seq = c.start(t);
  let reason = null;
  c._startRenderRecovery(t, seq, (why) => { reason = why; c._reportPlayFailure(t, 'Intro', { name: 'NotSupportedError' }, seq, why); });
  await c.run();
  assert.equal(reason, '');
  assert.ok(srv.requests.includes('/api/stream/a1'), 'the 1-byte probe ran');
  assert.deepEqual(c.env.toasts, [['error', 'Couldn’t play “Intro” · TFMX: File missing on disk.']]);
  // A 'failed' state WITHOUT an error string falls back to the probe too.
  const srv2 = makeServer([{ state: 'failed' }]);
  srv2.onStream = async () => ({ ok: false, status: 502, json: async () => ({}) });
  const c2 = makeCore({ server: srv2 });
  const seq2 = c2.start(t);
  c2._startRenderRecovery(t, seq2, (why) => c2._reportPlayFailure(t, 'Intro', { name: 'NotSupportedError' }, seq2, why));
  await c2.run();
  assert.ok(srv2.requests.includes('/api/stream/a1'));
  assert.match(c2.env.toasts[0][1], /Source unavailable/);
});

// ── The format in a failure toast ────────────────────────────────────────────
test('a failure toast keeps the server\'s format name, upper-casing only a bare extension', async () => {
  const c = makeCore();
  assert.equal(c._fmtLabel('MusiclineEditor'), 'MusiclineEditor');
  assert.equal(c._fmtLabel('FastTracker 2'), 'FastTracker 2');
  assert.equal(c._fmtLabel('MP3'), 'MP3');
  assert.equal(c._fmtLabel('mp3'), 'MP3');
  assert.equal(c._fmtLabel('flac'), 'FLAC');
  assert.equal(c._fmtLabel(''), '');
  assert.equal(c._fmtLabel(null), '');
  // …and the report itself.
  const srv = makeServer([{ state: 'failed', error: 'uade123 produced no audio.' }]);
  const c2 = makeCore({ server: srv });
  const t = { id: 'm1', format: 'MusiclineEditor', genre: ['Amiga'], duration: 0 };
  const seq = c2.start(t);
  c2._reportPlayFailure(t, 'anorak3', { name: 'NotSupportedError' }, seq, 'uade123 produced no audio.');
  await c2.run();
  assert.deepEqual(c2.env.toasts, [['error', 'Couldn’t play “anorak3” · MusiclineEditor: uade123 produced no audio.']]);
});
