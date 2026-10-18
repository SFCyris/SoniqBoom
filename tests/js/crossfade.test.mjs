// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The crossfade (soniqboom/frontend/js/player.js, ``[xfade-core:begin]``),
// run outside a browser with a fake clock and fake <audio> elements: the rest
// of the outgoing track plays on a second element and fades OUT while the
// next track fades IN on the player's element, each over the crossfade time —
// and the next track's start (playTrack) keeps the fade instead of cutting it.
// Run: node --test tests/js/crossfade.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(process.env.SB_PLAYER_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/player.js'), 'utf8');
const m = SRC.match(/\/\/ \[xfade-core:begin\][^\n]*\n([\s\S]*?)\/\/ \[xfade-core:end\]/);
if (!m) throw new Error('xfade-core region not found');

function fakeAudio(name) {
  const L = {};
  const a = {
    name, volume: 1, currentTime: 0, duration: NaN, paused: true, src: '', currentSrc: '', crossOrigin: 'anonymous',
    plays: 0, playReject: null, loads: 0, attrs: {},
    addEventListener(ev, fn) { (L[ev] ||= []).push(fn); },
    removeEventListener(ev, fn) { L[ev] = (L[ev] || []).filter((f) => f !== fn); },
    listeners: (ev) => (L[ev] || []).length,
    fire(ev) { (L[ev] || []).slice().forEach((f) => f()); },
    load() { a.loads++; },
    play() { a.plays++; if (a.playReject) return Promise.reject(a.playReject); a.paused = false; return Promise.resolve(); },
    pause() { a.paused = true; },
    removeAttribute(k) { if (k === 'src') { a.src = ''; a.currentSrc = ''; } },
    setAttribute(k, v) { a.attrs[k] = v; },
  };
  return a;
}

// A fake Web Audio graph: nodes record what they connect to and their values.
function fakeCtx() {
  const made = [];
  const node = (kind) => { const n = { kind, to: [], gain: { value: 1 }, frequency: { value: 0 }, Q: { value: 0 }, type: '',
                                       connect(d) { n.to.push(d); return d; } }; made.push(n); return n; };
  return { made, destination: node('destination'), createMediaElementSource: (el) => Object.assign(node('source'), { el }),
           createGain: () => node('gain'), createBiquadFilter: () => node('biquad') };
}

function makeXfade({ userVolume = '0.8', duration = 12, graph = null } = {}) {
  const clock = { now: 0, timers: [] };
  const setT = (fn, ms, every = false) => { const t = { at: clock.now + ms, ms, fn, every, live: true }; clock.timers.push(t); return t; };
  const env = {
    audio: fakeAudio('main'), tail: null, advances: 0, store: { sb_volume: userVolume },
    onAdvance: null, graph,
  };
  env.audio.duration = duration;
  const document = {
    createElement: () => (env.tail = fakeAudio('tail')),
    body: { appendChild() {} }, documentElement: { appendChild() {} },
  };
  const localStorage = { getItem: (k) => (k in env.store ? env.store[k] : null) };
  const performance = { now: () => clock.now };
  let _crossfading = false;
  const core = new Function('env', 'document', 'localStorage', 'performance', 'setTimeout', 'clearTimeout',
    'setInterval', 'clearInterval', `
    const audio = env.audio;
    let _crossfading = false;
    const ctx = env.graph ? env.graph.ctx : null;
    const analyser = env.graph ? env.graph.analyser : null;
    const eqPreGain = env.graph ? env.graph.pre : null;
    const eqFilters = env.graph ? env.graph.eq : [];
    const replayGain = env.graph ? env.graph.rg : null;
    function _duration() { return audio.duration; }
    function _autoAdvance() { env.advances++; if (env.onAdvance) env.onAdvance(); }
    ${m[1]}
    return { _startCrossfade, _cancelCrossfade, _crossfadeOnTrackStart, _crossfadeOnPlaying, _crossfadeOnPause, _crossfadeVolume,
             get xf() { return _xf; }, get crossfading() { return _crossfading; }, set crossfading(v) { _crossfading = v; } };
  `)(env, document, localStorage, performance,
     (fn, ms) => setT(fn, ms), (t) => { if (t) t.live = false; },
     (fn, ms) => setT(fn, ms, true), (t) => { if (t) t.live = false; });
  core.env = env;
  core.flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
  core.advance = async (ms) => {
    const until = clock.now + ms;
    for (;;) {
      const due = clock.timers.filter((t) => t.live && t.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      clock.now = due.at;
      if (due.every) due.at += due.ms; else due.live = false;
      due.fn();
      await core.flush();
    }
    clock.now = until;
  };
  return core;
}

// The player's element at 7 s of a 12 s track, crossfade 5 s: begin, and let
// the tail take over (metadata → seek → playing).
async function begin(c, { at = 7 } = {}) {
  const { audio } = c.env;
  audio.src = audio.currentSrc = '/api/stream/a?pg=1';
  audio.currentTime = at; audio.volume = 0.8; audio.paused = false;
  c.crossfading = true;
  c._startCrossfade(audio.duration - at);
  const tail = c.env.tail;
  tail.duration = audio.duration;
  tail.fire('loadedmetadata');
  tail.currentTime = tail.currentTime || 0;
  tail.fire('seeked');
  await c.flush();
  return tail;
}

test('the outgoing track fades out on the tail while the next one fades in', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  // playTrack, as the advance runs it: keeps the fade, the new track starts silent
  c.env.onAdvance = () => { assert.equal(c._crossfadeOnTrackStart(), true); audio.currentTime = 0; };
  const tail = await begin(c);
  assert.equal(tail.src, '/api/stream/a?pg=1');                 // the same track…
  assert.ok(Math.abs(tail.currentTime - 7.02) < 1e-9);          // …at the same place
  assert.equal(c.env.advances, 0);                              // not before the tail plays
  tail.fire('playing');
  assert.equal(c.env.advances, 1);                              // the next track starts now
  assert.equal(audio.volume, 0);
  assert.equal(tail.volume, 0.8);
  audio.fire?.('playing');
  c._crossfadeOnPlaying();                                      // the new track plays
  const samples = [];
  for (let t = 0; t <= 5000; t += 500) {
    samples.push([t, Math.round(tail.volume * 1000) / 1000, Math.round(audio.volume * 1000) / 1000]);
    await c.advance(500);
  }
  // the tail ramps 0.8 → 0, the new track 0 → 0.8, both monotonic
  for (let i = 1; i < samples.length; i++) {
    assert.ok(samples[i][1] <= samples[i - 1][1] + 1e-9, `tail went up at ${samples[i][0]}`);
    assert.ok(samples[i][2] >= samples[i - 1][2] - 1e-9, `main went down at ${samples[i][0]}`);
  }
  const mid = samples.find(([t]) => t === 2500);
  assert.ok(Math.abs(mid[1] - 0.4) < 0.05 && Math.abs(mid[2] - 0.4) < 0.05, JSON.stringify(mid));
  assert.equal(audio.volume, 0.8);                              // the user's volume at the end
  assert.equal(tail.src, '');                                   // the tail let go
  assert.equal(tail.paused, true);
  assert.equal(c.xf, null);
});

test('playTrack does not cancel the fade it asked for — but a second start does', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  c.env.onAdvance = () => c._crossfadeOnTrackStart();
  const tail = await begin(c);
  tail.fire('playing');
  c._crossfadeOnPlaying();
  await c.advance(1000);
  assert.ok(tail.volume > 0 && tail.volume < 0.8);
  // the user presses Next mid-fade
  assert.equal(c._crossfadeOnTrackStart(), false);
  assert.equal(tail.paused, true);
  assert.equal(tail.src, '');
  assert.equal(audio.volume, 0.8);
  assert.equal(c.xf, null);
});

test('a tail that cannot play: the track fades out where it plays, then ends normally', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  audio.src = audio.currentSrc = '/api/stream/a';
  audio.currentTime = 7; audio.volume = 0.8;
  c.crossfading = true;
  c._startCrossfade(5);
  const tail = c.env.tail;
  tail.playReject = Object.assign(new Error('blocked'), { name: 'NotAllowedError' });
  tail.duration = 12;
  tail.fire('loadedmetadata'); tail.fire('seeked');
  await c.flush();
  assert.equal(c.xf.mode, 'fadeout');
  assert.equal(c.crossfading, false);                           // 'ended' will advance
  assert.equal(c.env.advances, 0);
  await c.advance(2500);
  assert.ok(audio.volume < 0.5 && audio.volume > 0.3, String(audio.volume));
  await c.advance(2600);
  assert.equal(audio.volume, 0);
  // the next track (after 'ended'): the volume is the user's again
  assert.equal(c._crossfadeOnTrackStart(), false);
  assert.equal(audio.volume, 0.8);
});

test('a tail that never gets going in time falls back to the fade-out too', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  audio.src = audio.currentSrc = '/api/stream/a';
  audio.currentTime = 7; audio.volume = 0.8;
  c._startCrossfade(5);                                          // no metadata ever arrives
  await c.advance(2100);
  assert.equal(c.xf.mode, 'fadeout');
  assert.equal(c.env.tail.src, '');
});

test('the volume changed during a fade: the ramps go to the new volume', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  c.env.onAdvance = () => c._crossfadeOnTrackStart();
  const tail = await begin(c);
  tail.fire('playing');
  c._crossfadeOnPlaying();
  await c.advance(1000);
  assert.equal(c._crossfadeVolume(0.5), true);
  await c.advance(4500);
  assert.equal(audio.volume, 0.5);
  assert.equal(c._crossfadeVolume(0.3), false);                 // (no fade any more)
});

test('cancel (a pause, a cleared queue) silences the tail and gives the volume back', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  c.env.onAdvance = () => c._crossfadeOnTrackStart();
  const tail = await begin(c);
  tail.fire('playing');
  c._cancelCrossfade();
  assert.equal(tail.paused, true);
  assert.equal(audio.volume, 0.8);
  assert.equal(tail.listeners('playing'), 0);
});

test('wiring: the crossfade starts the tail, playTrack keeps the fade it asked for', () => {
  const fn = SRC.slice(SRC.indexOf('function _maybeCrossfade('), SRC.indexOf('function _maybeCrossfade(') + 2200);
  assert.match(fn, /_startCrossfade\(remaining\)/);
  assert.doesNotMatch(fn, /setInterval/);                     // (the old fade ramped AFTER switching tracks)
  const pt = SRC.slice(SRC.indexOf('async function playTrack('), SRC.indexOf('async function playTrack(') + 9000);
  assert.match(pt, /if \(!_crossfadeOnTrackStart\(\)\) \{/);
  assert.doesNotMatch(pt, /clearInterval\(_crossfadeTimer\)/);
});

test('a pause while the tail is still arming: the track ends and the queue moves on', async () => {
  const c = makeXfade();
  const { audio } = c.env;
  audio.src = audio.currentSrc = '/api/stream/a';
  audio.currentTime = 7; audio.volume = 0.8;
  c.crossfading = true;
  c._startCrossfade(5);
  c._crossfadeOnPause();                                        // the user paused
  assert.equal(c.xf, null);
  assert.equal(c.crossfading, false);                           // 'ended' will advance later
  assert.equal(c.env.tail.src, '');
  assert.equal(audio.volume, 0.8);
});

test('a volume change while arming on an old page keeps the tail (no ramp before the hand-over)', async () => {
  const c = makeXfade();
  c.env.onAdvance = () => c._crossfadeOnTrackStart();
  const a = c.env.audio;
  a.src = a.currentSrc = '/api/stream/a'; a.currentTime = 7; a.volume = 0.8;
  await c.advance(50000);                                        // (the page has been open a while)
  c.crossfading = true;
  c._startCrossfade(5);
  assert.equal(c._crossfadeVolume(0.5), true);
  assert.equal(c.xf.mode, 'arming');
  assert.equal(a.volume, 0.5);                                   // the track still plays here, at the new volume
  const tail = c.env.tail;
  assert.notEqual(tail.src, '');                                 // the tail was not let go
  tail.duration = 12; tail.fire('loadedmetadata'); tail.fire('seeked'); await c.flush();
  tail.fire('playing');
  assert.equal(tail.volume, 0.5);
  assert.equal(c.env.advances, 1);
});

test('the pause of the switch to the incoming track does not stop the fade', async () => {
  const c = makeXfade();
  c.env.onAdvance = () => c._crossfadeOnTrackStart();
  const tail = await begin(c);
  tail.fire('playing');
  c._crossfadeOnPause();                                        // playTrack's audio.pause() of the switch
  assert.ok(c.xf && c.xf.mode === 'crossfade');
  c._crossfadeOnPlaying();
  c._crossfadeOnPause();                                        // now the user pauses the new track
  assert.equal(c.xf, null);
  assert.equal(tail.paused, true);
});

test('with Web Audio on, the tail runs through an EQ copy with the outgoing ReplayGain', async () => {
  const ctx = fakeCtx();
  const mk = (kind, v) => ({ kind, gain: { value: v }, frequency: { value: 1000 }, Q: { value: 1 }, type: 'peaking' });
  const graph = { ctx, analyser: { kind: 'analyser' }, pre: mk('pre', 0.7), eq: [mk('b0', 3), mk('b1', -2)], rg: mk('rg', 0.4) };
  const c = makeXfade({ graph });
  c.env.onAdvance = () => c._crossfadeOnTrackStart();
  await begin(c);
  const src = ctx.made.find((n) => n.kind === 'source');
  assert.ok(src && src.el === c.env.tail);
  const gains = ctx.made.filter((n) => n.kind === 'gain');
  const biq = ctx.made.filter((n) => n.kind === 'biquad');
  assert.deepEqual(biq.map((n) => n.gain.value), [3, -2]);
  assert.deepEqual(gains.map((n) => n.gain.value), [0.7, 0.4]);  // pre-gain, ReplayGain of the outgoing track
  assert.equal(gains[1].to[0], graph.analyser);                   // into the same meters
  graph.rg.gain.value = 0.9;                                      // a second fade: the chain is reused,
  c._cancelCrossfade();                                           // its values copied again
  await begin(c);
  assert.equal(ctx.made.filter((n) => n.kind === 'source').length, 1);
  assert.equal(gains[1].gain.value, 0.9);
});

test('wiring: repeat-one, a seek and an external takeover end or skip the fade', () => {
  const mc = SRC.slice(SRC.indexOf('function _maybeCrossfade('), SRC.indexOf('function _maybeCrossfade(') + 600);
  assert.match(mc, /if \(repeatMode === 'one'\) return;/);
  const seek = SRC.slice(SRC.indexOf('  function seek(pct) {'), SRC.indexOf('  function seek(pct) {') + 400);
  assert.match(seek, /_cancelCrossfade\(\);/);
  const sus = SRC.slice(SRC.indexOf('function suspendForExternalAudio('), SRC.indexOf('function suspendForExternalAudio(') + 300);
  assert.match(sus, /if \(_extAudio\) _cancelCrossfade\(\);/);
  assert.match(SRC, /audio\.addEventListener\('pause', \(\) => _crossfadeOnPause\(\)\);/);
});
