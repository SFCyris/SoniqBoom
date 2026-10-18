// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A track its renderer refused the same way twice (core/unplayable.py: the
// server marks it ``defect`` 'unplayable', its play answers a 422 naming why
// and render-status says so).  Runs the REAL helpers from player.js
// (render_core_harness / player_queue_harness) and health.js (its pure
// functions, lifted out of the module), plus source checks of the wiring:
//  - a click says the server's sentence once, with no "Mark broken" offer,
//    and nothing shows it as playing;
//  - every copy the page holds takes the mark, so the queue passes it by;
//  - the badge says "can't play", with the reason and the renderer;
//  - Track Info shows no Playback marks / signal path for it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { makeCore, makeServer } from './render_core_harness.mjs';
import { makePlayer, tid } from './player_queue_harness.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(resolve(HERE, '../../soniqboom/frontend/js', rel), 'utf8');

const WHY = '“seven cities of gold” can’t be played here — the Amiga player (uade) stops on this file: score crashed.';
const DETAIL = 'The Amiga player (uade) stops on this file: score crashed';
const amiga = () => ({ id: 'a1', title: 'seven cities of gold', format: 'TFMX', duration: 0 });
const FAILED = { state: 'failed', error: WHY, error_status: 422, defect: 'unplayable',
                 defect_detail: DETAIL, defect_renderer: 'uade123 3.05', defect_at: 1760781600 };

// health.js without its imports: its pure functions, with stand-ins for the
// player / auth it reads.
function loadHealth({ canEdit = true } = {}) {
  const src = read('health.js')
    .replace(/^import .*$/gm, '')
    .replace(/^export (function|const|async function)/gm, '$1');
  const fn = new Function('Player', 'Auth', 'document', 'window', 'localStorage',
    `${src}\nreturn { healthOf, isUnplayable, foundLine, nothingToPlayText, init };`);
  const handlers = {};
  const Player = { on: (n, f) => { handlers[n] = f; }, setFailAction(f) { Player.failAction = f; },
                   queue: [], skipReason: () => '' };
  const events = [];
  const document = { dispatchEvent: (e) => events.push(e), createElement: () => ({}) };
  globalThis.CustomEvent ??= class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
  const h = fn(Player, { canEdit }, document, {}, { getItem: () => null, setItem() {} });
  return { ...h, Player, handlers, events };
}

test('a click on a refused track says the server’s reason once, no "Mark broken", marks it', async () => {
  const srv = makeServer([FAILED]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const marked = [];
  c.onUnplayable = (id, fields) => marked.push([id, fields]);
  c.failAction = () => ({ label: 'Mark broken', run() {} });   // (health.js would offer it otherwise)
  const seq = c.start(t);
  assert.equal(c._startRenderRecovery(t, seq, (why, info) => c._reportPlayFailure(t, t.title,
    { name: 'NotSupportedError' }, seq, why, info)), true);
  await c.run(4);
  assert.deepEqual(c.env.toasts, [['error', WHY]]);
  assert.deepEqual(marked, [['a1', { defect: 'unplayable', defect_detail: DETAIL,
                                     defect_renderer: 'uade123 3.05', defect_at: 1760781600 }]]);
  assert.equal(c.startFailed, true, 'no row or bar shows it as playing');
  assert.equal(c.env.badge.shown, false, 'no "Rendering…" left up');
  const names = c.env.events.map((e) => e[0]);
  assert.ok(names.includes('statechange') && names.includes('error'));
  const playing = c.env.events.filter(([n, d]) => n === 'statechange' && d && d.playing);
  assert.equal(playing.length, 0, 'never "playing"');
  // one render-status request — the stream isn't asked again (its render would only fail again)
  assert.ok(srv.requests.every((u) => u === '/api/stream/a1/render-status'), srv.requests.join());
});

test('the same failure without the mark still offers "Mark broken" (control)', async () => {
  const srv = makeServer([{ state: 'failed', error: 'uade renderer exited with status 1', error_status: 502 }]);
  const c = makeCore({ server: srv });
  const t = amiga();
  const marked = [];
  c.onUnplayable = (id) => marked.push(id);
  c.failAction = () => ({ label: 'Mark broken', run() {} });
  const seq = c.start(t);
  c._startRenderRecovery(t, seq, (why, info) => c._reportPlayFailure(t, t.title,
    { name: 'NotSupportedError' }, seq, why, info));
  await c.run(4);
  assert.equal(c.env.toasts.length, 1);
  assert.deepEqual(c.env.toasts[0].slice(0, 1), ['action']);
  assert.match(c.env.toasts[0][1], /uade renderer exited with status 1/);
  assert.deepEqual(marked, []);
});

test('the probe path: a 422 with X-SB-Defect: unplayable says the reason, no "Mark broken"', async () => {
  const srv = makeServer();
  srv.onStream = async () => ({ ok: false, status: 422,
    headers: { get: (k) => (k === 'X-SB-Defect' ? 'unplayable' : null) },
    json: async () => ({ detail: WHY }) });
  const c = makeCore({ server: srv });
  const t = { ...amiga(), format: 'FLAC' };
  const marked = [];
  c.onUnplayable = (id, f) => marked.push([id, f.defect, f.defect_detail]);
  c.failAction = () => ({ label: 'Mark broken', run() {} });
  const seq = c.start(t);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.deepEqual(c.env.toasts, [['error', WHY]]);
  assert.deepEqual(marked, [['a1', 'unplayable', null]]);   // (no earlier defect's detail stays)
});

test('the queue passes a refused track by when it moves on; a click still tries it', async () => {
  const p = makePlayer();
  const list = [{ id: tid(0), title: 'A' }, { id: tid(1), title: 'B', defect: 'unplayable', defect_detail: DETAIL },
                { id: tid(2), title: 'C' }];
  p.setQueue(list, 0, { explicit: false });
  await p.settle();
  assert.equal(p.skipReason(list[1]), 'unplayable');
  assert.equal(p.skipReason({ id: 'x', health: 'broken', defect: 'unplayable' }), 'unplayable');
  p._advance(); await p.settle();
  assert.equal(p.current.id, tid(2));
  const skip = p.env.events.find(([n]) => n === 'dealtskip');
  assert.deepEqual(skip[1].tracks.map((x) => x.reason), ['unplayable']);
});

test('the badge: "can’t play", red, before a listener’s report, with the reason and renderer', () => {
  const H = loadHealth();
  const t = { id: 'a1', defect: 'unplayable', defect_detail: `${DETAIL}.`, defect_renderer: 'uade123 3.05',
              defect_at: 1760781600, health: 'broken', health_by: 'Ed' };
  const h = H.healthOf(t);
  assert.equal(h.key, 'unplayable');
  assert.equal(h.word, 'can’t play');
  assert.equal(h.skipped, true);
  assert.match(h.title, /^Can’t play: the Amiga player \(uade\) stops on this file: score crashed — passed by when a list plays through\. Found when it was played · .+ · uade123 3\.05$/);
  assert.ok(!/corrupt/i.test(h.title), 'never called corrupt');
  assert.equal(H.isUnplayable(t), true);
  assert.equal(H.healthOf({ health: 'broken' }).key, 'broken');      // the report alone: its own badge
  assert.equal(H.isUnplayable({ health: 'broken' }), false);
  assert.match(H.nothingToPlayText(['unplayable']), /tracks their player can’t play are passed by/);
});

test('health.js: no "Mark broken" for a refused track; its mark reaches every view', () => {
  const H = loadHealth();
  H.init();
  const act = H.Player.failAction;
  assert.equal(act({ id: 'a1', defect: 'unplayable' }, 'Couldn’t play'), null);
  assert.ok(act({ id: 'a2' }, 'Couldn’t play “x”: uade renderer exited with status 1.'));
  H.handlers.trackunplayable({ id: 'a1', fields: { defect: 'unplayable', defect_detail: DETAIL } });
  const types = H.events.map((e) => e.type);
  assert.deepEqual(types, ['soniqboom:track-edited', 'soniqboom:health-changed']);
  assert.deepEqual(H.events[1].detail, { ids: ['a1'], fields: { defect: 'unplayable', defect_detail: DETAIL } });
});

test('wiring: Track Info, the queue re-check and the player API know the mark', () => {
  const ti = read('trackinfo.js');
  assert.match(ti, /if \(t\.defect === 'unplayable'\) return 'Can’t be played here';/);
  assert.match(ti, /canReport\(\) && !track\.isStation && \(!isUnplayable\(track\) \|\| !!track\.health\)/);
  assert.match(ti, /if \(!vizGroupEnabled\('nowPlaying'\) \|\| isUnplayable\(track\)\)/);
  const off = read('offline.js');
  assert.match(off, /'defect', 'defect_detail', 'defect_renderer', 'defect_at'\]/);
  const pl = read('player.js');
  assert.match(pl, /_onUnplayable = \(id, fields\) => markUnplayable\(id, fields\);/);
  assert.match(pl, /patchTrackFields, markGone, markUnplayable,/);
  assert.match(pl, /giveUp\(why, state === 'failed' \? last : null\);/);
  assert.equal((pl.match(/_startRenderRecovery\([^)]*\(why, info\) =>/g) || []).length, 2);
});
