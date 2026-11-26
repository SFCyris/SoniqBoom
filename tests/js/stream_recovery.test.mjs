// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// How a radio stream recovers (soniqboom/frontend/js/stream_recovery.js):
// the desktop's numbers and helpers, and the phone's watcher
// (``createStreamRecovery``, used by js/mobile/radio-player.js) driven by a
// fake <audio> element and fake timers — a dropped stream reconnects with the
// backoff, the budget is spent after 3 and forgiven after 10 s of play, a
// sustained stall rebuffers, a dead rebuffer reconnects, the listener's pause
// stops every attempt, and stop() leaves nothing pending.
// Run: node --test tests/js/stream_recovery.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  createStreamRecovery, reconnectDelay, bufferedAhead, rebufferReady,
  STALL_SUSTAINED_MS, STARTUP_GRACE_MS, MAX_RECONNECTS, GOOD_PLAY_RESET_MS,
  REBUFFER_GIVEUP_MS, REBUFFER_RECONNECT_MS, REBUFFER_TARGET_S, FROZEN_MS,
} from '../../soniqboom/frontend/js/stream_recovery.js';

// ── fake clock ─────────────────────────────────────────────────────────────
function clock() {
  let t = 0, seq = 0;
  const q = new Map();          // id → {at, fn, every}
  const add = (fn, ms, every) => { const id = ++seq; q.set(id, { at: t + Math.max(0, ms), fn, every: every ? ms : 0 }); return id; };
  const timers = {
    setTimeout: (fn, ms) => add(fn, ms, false), clearTimeout: (id) => { q.delete(id); },
    setInterval: (fn, ms) => add(fn, ms, true), clearInterval: (id) => { q.delete(id); },
  };
  function advance(ms) {
    const end = t + ms;
    for (;;) {
      let next = null;
      for (const [id, e] of q) if (e.at <= end && (!next || e.at < next[1].at)) next = [id, e];
      if (!next) break;
      const [id, e] = next;
      t = e.at;
      if (e.every) e.at = t + e.every; else q.delete(id);
      e.fn();
    }
    t = end;
  }
  return { timers, now: () => t, advance, get pending() { return q.size; } };
}

// ── fake element ──────────────────────────────────────────────────────────
// Its playhead runs with the fake clock while it plays, unless ``frozen``
// (WebKit after a dropped connection: "playing", but the playhead stands).
function element(c) {
  let base = 0, since = 0, paused = false;
  const clk = () => (c ? c.now() : 0);
  const a = {
    ended: false, error: null, readyState: 0, frozen: !c,
    ranges: [], plays: 0, pauses: 0,
    get paused() { return paused; },
    set paused(v) { a.currentTime = a.currentTime; paused = !!v; },
    get currentTime() { return base + (paused || a.frozen ? 0 : (clk() - since) / 1000); },
    set currentTime(v) { base = v; since = clk(); },
    freeze() { a.currentTime = a.currentTime; a.frozen = true; },
    get buffered() { const r = a.ranges; return { length: r.length, start: (i) => r[i][0], end: (i) => r[i][1] }; },
    play() { a.plays++; a.paused = false; a.ended = false; a.frozen = false; return Promise.resolve(); },
    pause() { a.pauses++; a.paused = true; },
  };
  return a;
}

function setup() {
  const c = clock();
  const log = [];
  const statuses = [];
  const a = element(c);
  const r = createStreamRecovery({
    timers: c.timers, now: c.now,
    reconnect: (x) => { log.push(['reconnect', c.now()]); x.play(); },
    giveUp: (why) => log.push(['giveUp', why, c.now()]),
    status: (s) => statuses.push(s),
  });
  r.watch(a);
  return { c, a, r, log, statuses, reconnects: () => log.filter((x) => x[0] === 'reconnect') };
}
const drop = (s) => { s.a.paused = true; s.a.ended = true; s.r.handle('ended', s.a); };

test('the shared numbers and helpers are the desktop ones', () => {
  assert.deepEqual([1, 2, 3, 4, 9].map(reconnectDelay), [600, 1200, 2400, 4000, 4000]);
  assert.equal(reconnectDelay(0), 600);
  assert.equal(STALL_SUSTAINED_MS, 1200);
  assert.equal(STARTUP_GRACE_MS, 9000);
  assert.equal(MAX_RECONNECTS, 3);
  assert.equal(GOOD_PLAY_RESET_MS, 10000);
  const a = element();
  a.currentTime = 10; a.ranges = [[0, 4], [9.9, 14.5], [20, 30]];
  assert.equal(bufferedAhead(a), 4.5, 'the range under the playhead, not the last one');
  a.ranges = [[11, 30]];
  assert.equal(bufferedAhead(a), 0, 'a gap ahead is nothing buffered');
  assert.equal(bufferedAhead({ get buffered() { throw new Error('x'); } }), 0);
  assert.equal(rebufferReady(REBUFFER_TARGET_S, 0, 0), true);
  assert.equal(rebufferReady(1, 0, 4), true, 'HAVE_ENOUGH_DATA');
  assert.equal(rebufferReady(1.5, REBUFFER_GIVEUP_MS - 1, 0), false);
  assert.equal(rebufferReady(1.5, REBUFFER_GIVEUP_MS, 0), true, 'a quarter after the wait');
  // stations.js takes them from the shared module (no second copy of the numbers).
  const ST = readFileSync(new URL('../../soniqboom/frontend/js/stations.js', import.meta.url), 'utf8');
  assert.match(ST, /from '\.\/stream_recovery\.js'/);
  assert.doesNotMatch(ST, /const (STALL_SUSTAINED_MS|MAX_RECONNECTS|RECONNECT_BASE_MS)\b/);
  assert.match(ST, /const delay = reconnectDelay\(_reconnects\);/);
});

test('the desktop keeps its exact timings: the shared helpers equal the v2.2.0 inline expressions', () => {
  // stations.js at v2.2.0: delay = min(600 * 2 ** (n - 1), 4000); resume when
  // ahead >= 6 || readyState >= 4, or after 8 s with ahead >= 1.5.
  for (let n = 1; n <= 12; n++) assert.equal(reconnectDelay(n), Math.min(600 * 2 ** (n - 1), 4000), `n=${n}`);
  for (const ahead of [0, 1, 1.49, 1.5, 3, 5.99, 6, 9]) {
    for (const waited of [0, 4000, 7999, 8000, 20000]) {
      for (const rs of [0, 1, 2, 3, 4]) {
        const old = (ahead >= 6 || rs >= 4) || (waited >= 8000 && ahead >= 1.5);
        assert.equal(rebufferReady(ahead, waited, rs), old, `${ahead}/${waited}/${rs}`);
      }
    }
  }
});

test('a fresh start is "tuning"; its own errors are the start\'s business', () => {
  const s = setup();
  assert.equal(s.r.status, 'tuning');
  s.a.error = { code: 4 };
  s.r.handle('error', s.a);
  s.r.handle('ended', s.a);
  s.c.advance(60000);
  assert.deepEqual(s.log, [], 'no reconnect before the stream ever played');
  s.a.error = null;
  s.r.handle('playing', s.a);
  assert.equal(s.r.status, 'live');
});

test('a dropped stream reconnects with the backoff; the 4th drop in a row gives up', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  const at = [];
  for (let i = 1; i <= MAX_RECONNECTS; i++) {
    const t0 = s.c.now();
    drop(s);
    assert.equal(s.r.status, 'reconnecting');
    assert.equal(s.r.recovering, true, 'the key keeps meaning "pause" meanwhile');
    s.c.advance(reconnectDelay(i) - 1);
    assert.equal(s.reconnects().length, i - 1, `not before ${reconnectDelay(i)} ms`);
    s.c.advance(1);
    assert.equal(s.reconnects().length, i);
    at.push(s.c.now() - t0);
    s.c.advance(500);
    s.r.handle('playing', s.a);                  // plays briefly, drops again
    assert.equal(s.r.status, 'live');
    s.c.advance(2000);
  }
  assert.deepEqual(at, [600, 1200, 2400]);
  drop(s);
  assert.deepEqual(s.log.at(-1).slice(0, 2), ['giveUp', 'ended']);
  assert.equal(s.r.status, 'off');
  s.c.advance(60000);
  assert.equal(s.reconnects().length, MAX_RECONNECTS, 'nothing after giving up');
});

test('10 s of good play forgives the budget', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  for (let round = 0; round < 3; round++) {
    for (let i = 0; i < MAX_RECONNECTS; i++) {
      drop(s); s.c.advance(5000); s.r.handle('playing', s.a); s.c.advance(1000);
    }
    s.c.advance(GOOD_PLAY_RESET_MS);              // a long healthy stretch
    assert.equal(s.r.reconnects, 0);
  }
  assert.equal(s.log.filter((x) => x[0] === 'giveUp').length, 0);
  assert.equal(s.reconnects().length, 9);
});

test('waiting / playing blips do not postpone the forgiveness', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  drop(s); s.c.advance(600); s.r.handle('playing', s.a);
  assert.equal(s.r.reconnects, 1);
  for (let i = 0; i < 6; i++) { s.c.advance(2000); s.r.handle('waiting', s.a); s.c.advance(300); s.r.handle('playing', s.a); }
  assert.equal(s.r.reconnects, 0, 'forgiven 10 s after it came back, blips or not');
});

test('a hard error mid-stream reconnects; the "aborted" of a swapped source does not', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.a.error = { code: 1 };
  s.r.handle('error', s.a);
  s.c.advance(10000);
  assert.equal(s.reconnects().length, 0);
  s.a.error = { code: 2 };                       // MEDIA_ERR_NETWORK
  s.r.handle('error', s.a);
  s.c.advance(600);
  assert.equal(s.reconnects().length, 1);
});

test('a reconnect that does not play within the grace is the next attempt', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  drop(s);
  s.c.advance(600);                              // reconnect #1 issued
  s.c.advance(STARTUP_GRACE_MS + 1200);          // never plays → #2 after its backoff
  assert.equal(s.reconnects().length, 2);
  s.c.advance(STARTUP_GRACE_MS + 2400);
  assert.equal(s.reconnects().length, 3);
  s.c.advance(STARTUP_GRACE_MS);
  assert.deepEqual(s.log.at(-1).slice(0, 2), ['giveUp', 'startup']);
});

test('a sustained stall rebuffers and resumes; a short dip does nothing', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.r.handle('waiting', s.a);
  s.c.advance(STALL_SUSTAINED_MS - 100);
  s.r.handle('playing', s.a);                    // recovered on its own
  s.c.advance(5000);
  assert.equal(s.a.pauses, 0);
  assert.equal(s.r.status, 'live');
  s.r.handle('waiting', s.a);
  s.c.advance(STALL_SUSTAINED_MS);
  assert.equal(s.r.status, 'buffering');
  assert.equal(s.r.recovering, true);
  assert.equal(s.a.pauses, 1, 'paused to fill a longer buffer');
  const plays = s.a.plays;
  s.a.currentTime = 30; s.a.ranges = [[0, 33]];
  s.c.advance(1000);
  assert.equal(s.a.plays, plays, 'not on 3 s');
  s.a.ranges = [[0, 36.5]];
  s.c.advance(250);
  assert.equal(s.a.plays, plays + 1, 'resumed on 6 s');
  s.r.handle('playing', s.a);
  assert.equal(s.r.status, 'live');
  assert.equal(s.reconnects().length, 0);
});

test('a rebuffer that never gets its quarter reconnects (a connection that died silently)', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.r.handle('waiting', s.a);
  s.c.advance(STALL_SUSTAINED_MS);
  assert.equal(s.r.status, 'buffering');
  s.c.advance(REBUFFER_RECONNECT_MS - 300);
  assert.equal(s.r.status, 'buffering');
  s.c.advance(300);
  assert.equal(s.r.status, 'reconnecting');
  s.c.advance(600);
  assert.equal(s.reconnects().length, 1);
});

test("the listener's pause stops every attempt; play loads a dropped stream again", () => {
  const s = setup();
  s.r.handle('playing', s.a);
  drop(s);
  s.r.pause();
  assert.equal(s.r.status, 'paused');
  assert.equal(s.r.recovering, false);
  s.c.advance(60000);
  assert.equal(s.reconnects().length, 0, 'no reconnect behind a pause');
  assert.equal(s.r.resume(), true, 'the stream is loaded again');
  assert.equal(s.reconnects().length, 1);
  assert.equal(s.r.status, 'reconnecting');
  s.r.handle('playing', s.a);
  assert.equal(s.r.status, 'live');
  // A plain pause of a live stream resumes by play() (not a reload).
  s.r.pause();
  s.a.paused = true;
  s.r.handle('waiting', s.a); s.c.advance(5000);
  assert.equal(s.r.status, 'paused', 'a stall while paused is no underrun');
  assert.equal(s.r.resume(), false);
  assert.equal(s.r.status, 'live');
});

test('a pause nobody here asked for (lock screen, a call) is the listener\'s; the others are not', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  // The pause the element makes at the end of the stream comes before 'ended'.
  s.a.paused = true; s.a.ended = true;
  s.r.handle('pause', s.a);
  assert.equal(s.r.status, 'live');
  s.r.handle('ended', s.a);
  assert.equal(s.r.status, 'reconnecting');
  s.c.advance(600);
  s.r.handle('playing', s.a);
  // A stale pause (the element plays again by the time it is told): nothing.
  s.r.handle('pause', s.a);
  assert.equal(s.r.status, 'live');
  // A real one: no reconnect behind it, even when the stream fails meanwhile.
  s.a.paused = true;
  s.r.handle('pause', s.a);
  assert.equal(s.r.status, 'paused');
  s.a.error = { code: 2 };
  s.r.handle('error', s.a);
  s.c.advance(60000);
  assert.equal(s.reconnects().length, 1);
  assert.equal(s.r.resume(), true, 'the failed stream is loaded again on play');
  assert.equal(s.reconnects().length, 2);
});

test('WebKit: a playhead that stands while "playing" — reconnect with nothing ahead, rebuffer with audio ahead', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.c.advance(20000);
  assert.equal(s.reconnects().length, 0, 'a moving playhead is fine');
  s.a.freeze();                                  // the connection went: no event at all
  s.a.ranges = [[0, s.a.currentTime + 0.2]];
  s.c.advance(FROZEN_MS + 1000);
  assert.equal(s.r.status, 'reconnecting');
  s.c.advance(600);
  assert.equal(s.reconnects().length, 1);
  s.r.handle('playing', s.a);
  s.c.advance(3000);
  s.a.freeze();
  s.a.ranges = [[0, s.a.currentTime + 5]];       // audio ahead, the decoder stuck
  s.c.advance(FROZEN_MS + 1000);
  assert.equal(s.r.status, 'buffering');
  // A paused element's standing playhead is no stall.
  const t = setup();
  t.r.handle('playing', t.a);
  t.r.pause(); t.a.paused = true;
  t.c.advance(30000);
  assert.equal(t.reconnects().length, 0);
  assert.equal(t.r.status, 'paused');
});

test('the system resuming a paused station (after a call) is watched again', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.a.paused = true;
  s.r.handle('pause', s.a);                      // a call took the audio
  assert.equal(s.r.status, 'paused');
  s.a.play();                                    // the system resumes it, not toggle()
  s.r.handle('playing', s.a);
  assert.equal(s.r.status, 'live');
  assert.equal(s.r.recovering, false);
  drop(s);
  assert.equal(s.r.status, 'reconnecting', 'a drop after the resume is recovered');
  s.c.advance(600);
  assert.equal(s.reconnects().length, 1);
  // A stale 'playing' (the element was paused again since) changes nothing.
  s.r.handle('playing', s.a);
  s.r.pause(); s.a.paused = true;
  s.r.handle('playing', s.a);
  assert.equal(s.r.status, 'paused');
});

test('a rebuffer whose play() is refused, or never plays, reconnects — no "Buffering…" for good', async () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.a.play = () => { s.a.plays++; return Promise.reject(new Error('NotAllowedError')); };
  s.r.handle('waiting', s.a);
  s.c.advance(STALL_SUSTAINED_MS);
  s.a.readyState = 4;
  s.c.advance(250);                              // ready → play() → refused
  await Promise.resolve(); await Promise.resolve();
  assert.equal(s.r.status, 'reconnecting');
  const t = setup();
  t.r.handle('playing', t.a);
  t.a.play = () => { t.a.plays++; return new Promise(() => {}); };   // never settles, no 'playing'
  t.r.handle('waiting', t.a);
  t.c.advance(STALL_SUSTAINED_MS);
  t.a.readyState = 4;
  t.c.advance(250);
  assert.equal(t.r.status, 'buffering');
  t.c.advance(STARTUP_GRACE_MS);
  assert.equal(t.r.status, 'reconnecting');
});

test('play on a station paused while it tuned in: errors count, a silent start is retried, a refused one shows paused', () => {
  const s = setup();                             // never played
  s.r.pause();
  assert.equal(s.r.resume(), false);
  assert.equal(s.r.status, 'tuning');
  s.a.error = { code: 2 };
  s.r.handle('error', s.a);
  assert.equal(s.r.status, 'reconnecting', 'its error is now recovered');
  const t = setup();
  t.r.pause();
  t.r.resume();                                  // play() issued, nothing plays
  t.c.advance(STARTUP_GRACE_MS);
  assert.equal(t.r.status, 'reconnecting');
  const u = setup();
  u.r.handle('playing', u.a);
  u.r.pause(); u.a.paused = true;
  u.r.resume();                                  // play() refused: the element stays paused
  assert.equal(u.r.status, 'live');
  u.c.advance(STARTUP_GRACE_MS);
  assert.equal(u.r.status, 'paused', 'no LIVE shown over a refused play');
  assert.equal(u.reconnects().length, 0);
});

test('a playhead that keeps freezing with audio ahead reconnects the second time (no BUFFER/LIVE loop)', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  s.c.advance(5000);
  s.a.freeze(); s.a.readyState = 4;
  s.a.ranges = [[0, s.a.currentTime + 5]];
  s.c.advance(FROZEN_MS + 500);
  assert.equal(s.r.status, 'buffering');
  s.c.advance(250);                              // readyState 4: resumes at once
  s.r.handle('playing', s.a);
  s.a.freeze();
  s.a.ranges = [[0, s.a.currentTime + 5]];
  s.c.advance(FROZEN_MS + 500);
  assert.equal(s.r.status, 'reconnecting');
});

test('stop() leaves nothing pending; a newly watched element ignores the old one', () => {
  const s = setup();
  s.r.handle('playing', s.a);
  drop(s);
  s.r.stop();
  assert.equal(s.r.status, 'off');
  assert.equal(s.c.pending, 0, 'no timer left');
  s.c.advance(60000);
  assert.equal(s.reconnects().length, 0);
  const b = element(s.c);
  s.r.watch(b);
  s.r.handle('playing', s.a);                    // the old element's late news
  assert.equal(s.r.status, 'tuning');
  s.r.handle('playing', b);
  s.a.ended = true; s.r.handle('ended', s.a);
  s.c.advance(60000);
  assert.equal(s.reconnects().length, 0);
  assert.equal(s.r.status, 'live');
});

test('radio-player.js runs its element through the watcher', () => {
  const RP = readFileSync(new URL('../../soniqboom/frontend/js/mobile/radio-player.js', import.meta.url), 'utf8');
  assert.match(RP, /import \{ createStreamRecovery, STARTUP_GRACE_MS \} from '\.\.\/stream_recovery\.js';/);
  for (const ev of ['playing', 'pause', 'waiting', 'ended', 'error']) {
    assert.match(RP, new RegExp(`_rec\\.handle\\('${ev}', a\\)`), ev);
  }
  assert.match(RP, /_rec\.watch\(a\);/);
  assert.match(RP, /_rec\.stop\(\);/);
  assert.match(RP, /_rec\.pause\(\);/);
  assert.match(RP, /if \(_rec\.resume\(\)\) return;/);
  assert.match(RP, /get playing\(\) \{ return !!_station && \(_rec\.recovering \|\| !cur\.paused\); \}/);
});
