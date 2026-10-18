// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A package of an online collection with no tune in it (the server answers
// its play 410: it left the library, the detail says what it holds), and a
// start that failed in general.  Runs the REAL helpers from player.js
// (render_core_harness / player_queue_harness) plus source checks of the
// wiring the browser runs:
//  - the toast is the server's sentence, once, with no "Mark broken" offer;
//  - every copy of the track is marked (defect 'empty') so the queue shows
//    why and passes it by;
//  - once a start is reported failed no row or bar shows it as playing;
//  - a refused answer (4xx) gets no codec transcode retry.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { makeCore, makeServer } from './render_core_harness.mjs';
import { makePlayer, tid } from './player_queue_harness.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(resolve(HERE, '../../soniqboom/frontend/js', rel), 'utf8');
const PLAYER = read('player.js');

const WHY = 'amaze-revolutions.zip has no tunes SoniqBoom plays — it holds an Amiga program, '
  + 'not tune files. It is no longer listed.';
const pkg = () => ({ id: 'p1', title: 'amaze-revolutions', format: 'ZIP package', duration: 0 });
function goneServer() {
  const srv = makeServer();
  srv.onStream = async () => ({ ok: false, status: 410, headers: { get: () => null },
                                json: async () => ({ detail: WHY }) });
  return srv;
}

test('a 410 says the server’s reason once, offers no "Mark broken", marks the track gone', async () => {
  const c = makeCore({ server: goneServer() });
  const t = pkg();
  const gone = [];
  c.onGone = (id, why) => gone.push([id, why]);
  c.failAction = () => ({ label: 'Mark broken', run() {} });   // (health.js would offer it)
  const seq = c.start(t);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.deepEqual(c.env.toasts, [['error', WHY]]);
  assert.deepEqual(gone, [['p1', WHY]]);
  assert.equal(c.startFailed, true);
  const names = c.env.events.map((e) => e[0]);
  assert.ok(names.includes('statechange') && names.includes('error'));
});

test('another failure still offers the fail action; a refused probe status is kept', async () => {
  const srv = makeServer();
  srv.onStream = async () => ({ ok: false, status: 422, headers: { get: () => null },
                                json: async () => ({ detail: 'x.zip can’t be opened — the archive is damaged.' }) });
  const c = makeCore({ server: srv });
  const t = pkg();
  c.failAction = () => ({ label: 'Mark broken', run() {} });
  const seq = c.start(t);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.equal(c.env.toasts.length, 1);
  assert.equal(c.env.toasts[0][0], 'action');                 // (health.js decides; here: offered)
  assert.match(c.env.toasts[0][1], /the archive is damaged/);
  const probe = {};
  await c._probeRestarting('/api/stream/p1', probe);
  assert.equal(probe.status, 422);
});

test('a new start clears the failed state', async () => {
  const c = makeCore({ server: goneServer() });
  const t = pkg();
  const seq = c.start(t);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.equal(c.startFailed, true);
  c.start({ ...pkg(), id: 'p2' });
  assert.equal(c.startFailed, false);
});

test('the queue passes a track marked gone by when it moves on; a click still tries it', async () => {
  const p = makePlayer();
  const list = [{ id: tid(0), title: 'A' }, { id: tid(1), title: 'B', defect: 'empty', defect_detail: WHY },
                { id: tid(2), title: 'C' }];
  p.setQueue(list, 0, { explicit: false });
  await p.settle();
  assert.equal(p.skipReason(list[1]), 'empty');
  p._advance(); await p.settle();
  assert.equal(p.current.id, tid(2));
  const skip = p.env.events.find(([n]) => n === 'dealtskip');
  assert.deepEqual(skip[1].tracks.map((x) => x.reason), ['empty']);
});

test('wiring: markGone patches every copy; nowPlayingId / startFailed are public', () => {
  assert.match(PLAYER, /function markGone\(ids, reasons = \{\}\)/);
  assert.match(PLAYER, /patchTrackFields\(id, \{ defect: 'empty', defect_detail: why \}\)/);
  assert.match(PLAYER, /_onGone = \(id, reason\) => markGone\(\[id\], \{ \[id\]: reason \}\);/);
  assert.match(PLAYER, /get nowPlayingId\(\)\s*\{ return _startFailedNow\(\) \? '' : \(trackId \|\| ''\); \}/);
  assert.match(PLAYER, /_clearStartFailed\(\);\s+\/\/ \(a new start: nothing failed yet\)/);
  assert.match(PLAYER, /audio\.addEventListener\('playing', \(\) => \{\s*_clearStartFailed\(\);/);
});

test('wiring: a refused answer (4xx) gets no transcode retry', () => {
  const onErr = PLAYER.slice(PLAYER.indexOf("audio.addEventListener('error', () => {"),
                             PLAYER.indexOf('function _onMediaError('));
  assert.match(onErr, /const probe = \{ status: 0 \};\s*_probeRestarting\(src, probe\)/);
  assert.match(onErr, /const st = probe\.status;\s*_onMediaError\(err, id, st >= 400 && st < 500 && st !== 408 && st !== 429\);/);
  const onMe = PLAYER.slice(PLAYER.indexOf('function _onMediaError('), PLAYER.indexOf('function _reportMediaError('));
  assert.match(onMe, /if \(!rendered && !refused && _codecFallbackEligible\(err, id\)\)/);
});

test('wiring: lists mark the now-playing row only while something plays', () => {
  const lib = read('library.js');
  assert.match(lib, /_setRowPlaying\(tr, _nowPlayingIs\(t\.id\)\);/);
  assert.match(lib, /Player\.on\('statechange', \(\) => markPlayingRow\(\)\);/);
  const pl = read('playlist.js');
  assert.match(pl, /if \(Player\.startFailed\) return false;/);
  const m = read('mobile/views/library.js');
  assert.match(m, /row\.classList\.toggle\('playing', !!t\.id && t\.id === _nowPlayingId\(\)\);/);
  assert.match(m, /Player\.on\('statechange', markPlaying\);/);
});

test('wiring: health.js has the "no tunes" badge and no report for what doesn’t play', () => {
  const h = read('health.js');
  assert.match(h, /empty:\s+\['∅', 'no tunes', true\]/);
  assert.match(h, /else if \(t\.defect === 'empty'\) key = 'empty';/);
  assert.match(h, /&& !isUnplayable\(t\) && !NOT_THE_FILE\.test/);
  const ti = read('trackinfo.js');
  assert.match(ti, /canReport\(\) && !track\.isStation && \(!isUnplayable\(track\) \|\| !!track\.health\)/);
  assert.match(ti, /if \(!vizGroupEnabled\('nowPlaying'\) \|\| isUnplayable\(track\)\)/);
});

test('wiring: both shells hear catalogue_tracks_left', () => {
  const app = read('app.js');
  assert.match(app, /msg\.event === 'catalogue_tracks_left'/);
  assert.match(app, /Player\.markGone\?\.\(ids, reasons\)/);
  const mapp = read('mobile/app.js');
  assert.match(mapp, /m\.event === 'catalogue_tracks_left'\) \{ try \{ left\(m\); \}/);
  assert.match(read('mobile/views/library.js'), /addEventListener\('soniqboom:tracks-left'/);
});

test('a failed start tells the OS media controls "paused"; a new start hands them back', async () => {
  const had = Object.getOwnPropertyDescriptor(globalThis.navigator, 'mediaSession');
  const session = { playbackState: 'playing' };
  Object.defineProperty(globalThis.navigator, 'mediaSession', { value: session, configurable: true, writable: true });
  try {
    const c = makeCore({ server: goneServer() });
    const t = pkg();
    const seq = c.start(t);
    c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
    await c.run(4);
    assert.equal(session.playbackState, 'paused');
  } finally {
    if (had) Object.defineProperty(globalThis.navigator, 'mediaSession', had);
    else delete globalThis.navigator.mediaSession;
  }
  assert.match(PLAYER, /function _clearStartFailed\(\) \{[\s\S]*?_setSessionState\('none'\);/);
});

test('wiring: the queue panels light no row while a start has failed', () => {
  const q = read('queue.js');
  assert.match(q, /function _playingIdx\(\) \{\s*return Player\.startFailed \? -1 : Player\.queueIdx;/);
  assert.equal((q.match(/const idx = _playingIdx\(\);/g) || []).length, 2);
  assert.match(q, /Player\.on\('statechange', \(\) => refresh\(\)\);/);
  const mq = read('mobile/views/queue.js');
  assert.match(mq, /if \(i === idx && !Player\.startFailed\) row\.classList\.add\('playing'\);/);
  assert.match(mq, /Player\.on\('statechange', \(\) => \{[\s\S]*?i === idx && !Player\.startFailed/);
});

test('wiring: the "no tunes" badge says it is passed by; its teaching toast promises no play', () => {
  const h = read('health.js');
  assert.match(h, /else if \(key === 'empty'\) what = \(detail \|\| 'it holds no tune SoniqBoom plays'\)\s*\+ ' — passed by when a list plays through';/);
  assert.match(h, /const tail = first\.reason === 'empty' \? '\.'\s*: first\.reason === 'unplayable' \? '; a track you pick is tried again\.'\s*: '; a track you pick still plays\.';/);
});

test('wiring: Track Info of a package that left shows no list position and no editor', () => {
  const ti = read('trackinfo.js');
  assert.match(ti, /return track && track\.defect === 'empty' \? title : `Track \$\{_idx \+ 1\} of \$\{_queue\.length\}  ·  \$\{title\}`;/);
  assert.match(ti, /if \(track\.defect === 'empty'\) return;\s+\/\/ \(it left the library: nothing to edit\)/);
});
