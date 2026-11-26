// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The web player's stereo width of retro tunes (soniqboom/frontend/js/player.js,
// ``[width-core:begin]``), run outside a browser on a fake Web Audio graph that
// evaluates the matrix it builds: a mid/side width spliced in between the
// source and the EQ only while a width other than 100 % applies, a mono source
// up-mixed first, the server's width unless this browser keeps its own.
// Run: node --test tests/js/stereo_width.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(process.env.SB_PLAYER_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/player.js'), 'utf8');
const m = SRC.match(/\/\/ \[width-core:begin\][^\n]*\n([\s\S]*?)\/\/ \[width-core:end\]/);
if (!m) throw new Error('width-core region not found');

// A fake graph: nodes record their connections (with output / input
// indices) and gain params; ``evaluate`` pushes one stereo (or mono) frame
// from the source through it, the way Web Audio mixes (a 2-channel
// "speakers" gain up-mixes mono to both sides; a merger input is mono).
function fakeCtx({ strict = false } = {}) {
  const made = [];
  const param = (v) => ({ value: v, cancelScheduledValues() {}, setValueAtTime(x) { this.value = x; },
                          linearRampToValueAtTime(x) { this.value = x; } });
  const node = (kind, extra = {}) => {
    const n = { kind, to: [], gain: param(1), ...extra,
      connect(d, out = 0, inp = 0) { n.to.push({ d, out, inp }); return d; },
      disconnect(d) {
        // (strict: like the spec, disconnecting a link that isn't there throws)
        if (strict && d !== undefined && !n.to.some((e) => e.d === d)) {
          const err = new Error(`InvalidAccessError: ${n.kind} is not connected to ${d.kind}`);
          err.name = 'InvalidAccessError';
          throw err;
        }
        n.to = d === undefined ? [] : n.to.filter((e) => e.d !== d);
      } };
    made.push(n);
    return n;
  };
  return { made, currentTime: 0, state: 'running', node,
    createGain: () => node('gain'), createChannelSplitter: () => node('split'),
    createChannelMerger: () => node('merge') };
}
// Value arriving at ``target`` (summed per channel) for a source frame.
function evaluate(ctx, source, frame, target) {
  const acc = new Map();                       // node → channels array
  const add = (n, ch, v) => { const a = acc.get(n) || []; a[ch] = (a[ch] || 0) + v; acc.set(n, a); };
  const push = (n, chans) => {
    for (const e of n.to) {
      if (n.kind === 'split') { add(e.d, 0, chans[e.out] || 0); continue; }
      if (e.d.kind === 'merge') { add(e.d, e.inp, chans.reduce((s, x) => s + x, 0)); continue; }
      chans.forEach((v, i) => add(e.d, i, v));
    }
  };
  // (a topological order of what the source reaches: Kahn's algorithm)
  const reach = new Set();
  const walk = (n) => { if (reach.has(n)) return; reach.add(n); n.to.forEach((e) => walk(e.d)); };
  walk(source);
  const indeg = new Map([...reach].map((n) => [n, 0]));
  for (const n of reach) for (const e of n.to) indeg.set(e.d, indeg.get(e.d) + 1);
  const order = [], ready = [source];
  while (ready.length) {
    const n = ready.shift();
    order.push(n);
    for (const e of n.to) { indeg.set(e.d, indeg.get(e.d) - 1); if (indeg.get(e.d) === 0) ready.push(e.d); }
  }
  push(source, frame);
  for (const n of order.slice(1)) {
    if (n === target) continue;
    let ch = acc.get(n) || [];
    if (n.kind === 'gain' && n.channelCount === 2 && ch.length === 1) ch = [ch[0], ch[0]];   // speakers up-mix
    if (n.kind === 'gain') ch = ch.map((v) => v * n.gain.value);
    push(n, ch);
  }
  return acc.get(target) || [];
}

function make({ own = null, server = undefined, rendered = true, strict = false } = {}) {
  const ctx = fakeCtx({ strict });
  const source = ctx.node('source');
  const eqPreGain = ctx.node('eqPreGain');
  source.connect(eqPreGain);
  const store = own === null ? {} : { sb_stereo_width: String(own) };
  const env = { store, timers: [], warned: [], track: { id: 't', format: rendered ? 'ProTracker' : 'MP3' } };
  const window = { __sbConfig: server === undefined ? {} : { stereo_width: server } };
  const core = new Function('ctx', 'source', 'eqPreGain', 'localStorage', 'window', 'fetch', 'setTimeout',
    'clearTimeout', 'env', `
    let _width = null, _widthOn = false, _widthPct = 100, _widthOffTimer = null;
    let _extAudio = false, _stationMode = false;
    let _track = env.track;
    const console = { warn: (...a) => env.warned.push(a.map(String).join(' ')) };
    const _initAudioContext = () => {};
    const _isRenderedTrack = (t) => !!t && t.format !== 'MP3';
    ${m[1]}
    return { apply: (t) => _applyStereoWidth(t), gains: _widthGains, pct: _stereoWidthPct,
             get on() { return _widthOn; }, get stage() { return _width; }, get cur() { return _widthPct; },
             voiceOverlay: () => voiceOverlay() };
  `)(ctx, source, eqPreGain,
     { getItem: (k) => (k in store ? store[k] : null) }, window,
     () => Promise.reject(new Error('offline')),
     (fn, ms) => { const t = { fn, ms }; env.timers.push(t); return t; },
     (t) => { env.timers = env.timers.filter((x) => x !== t); }, env);
  return { core, ctx, source, eqPreGain, env };
}

test('the matrix keeps the mid and scales the side: (1, 0) at 100 %', () => {
  const { core } = make();
  assert.deepEqual(core.gains(100), [1, 0]);
  assert.deepEqual(core.gains(60), [0.8, 0.2]);
  assert.deepEqual(core.gains(0), [0.5, 0.5]);
  assert.deepEqual(core.gains(150), [1.25, -0.25]);
  assert.deepEqual(core.gains(999), core.gains(200));          // clamped
});

test('a retro tune at the headphone width: spliced in, a hard-left voice heard 80/20', () => {
  const g = make({ own: 60 });
  g.core.apply(g.env.track);
  assert.equal(g.core.on, true);
  assert.ok(!g.source.to.some((e) => e.d === g.eqPreGain));     // no longer straight into the EQ
  const out = evaluate(g.ctx, g.source, [1, 0], g.eqPreGain);
  assert.ok(Math.abs(out[0] - 0.8) < 1e-12 && Math.abs(out[1] - 0.2) < 1e-12, JSON.stringify(out));
  const mid = evaluate(g.ctx, g.source, [0.5, 0.5], g.eqPreGain);  // the mid is kept
  assert.ok(Math.abs(mid[0] - 0.5) < 1e-12 && Math.abs(mid[1] - 0.5) < 1e-12, JSON.stringify(mid));
  const mono = evaluate(g.ctx, g.source, [0.7], g.eqPreGain);      // a mono source: both sides
  assert.ok(Math.abs(mono[0] - 0.7) < 1e-12 && Math.abs(mono[1] - 0.7) < 1e-12, JSON.stringify(mono));
});

test('ordinary audio, a station or 100 %: the chain without the stage', () => {
  const g = make({ own: 60 });
  g.core.apply(g.env.track);
  assert.equal(g.core.on, true);
  g.core.apply({ id: 'x', format: 'MP3' });                     // the next track is an MP3
  assert.equal(g.core.cur, 100);
  assert.equal(g.env.timers.length, 1);                          // glides to 100 %, then out
  g.env.timers.shift().fn();
  assert.equal(g.core.on, false);
  assert.ok(g.source.to.some((e) => e.d === g.eqPreGain));
  const out = evaluate(g.ctx, g.source, [1, 0], g.eqPreGain);
  assert.deepEqual(out, [1, 0]);                                  // exactly the source
  const h = make({ own: 100 });
  h.core.apply(h.env.track);
  assert.equal(h.core.on, false);
  assert.equal(h.core.stage, null);                              // never built
});

test("the server's width unless this browser keeps its own", () => {
  assert.equal(make({ server: 60 }).core.pct(), 60);
  assert.equal(make({ server: 60, own: 100 }).core.pct(), 100);
  assert.equal(make({}).core.pct(), 100);
  const g = make({ server: 25 });
  g.core.apply(g.env.track);
  const out = evaluate(g.ctx, g.source, [1, -1], g.eqPreGain);    // pure side: × 0.25
  assert.ok(Math.abs(out[0] - 0.25) < 1e-12 && Math.abs(out[1] + 0.25) < 1e-12, JSON.stringify(out));
});

test('wiring: the width is the track’s, applied with its ReplayGain; the crossfade tail copies it', () => {
  assert.match(SRC, /function _applyReplayGain\(track[^)]*\) \{\s*if \(!replayGain\) return;[^]*?_applyStereoWidth\(_extAudio \? null : track\)/);
  const tail = SRC.slice(SRC.indexOf('function _xfRouteTail('), SRC.indexOf('function _xfQuietTail('));
  assert.match(tail, /_setWidthGains\(_xfChain\.wd, _widthOn \? _widthPct : 100, false\)/);
  assert.match(tail, /node\.connect\(wd\.in\); wd\.out\.connect\(pre\);/);
});

// ── The width and the voice mixer's gate share one splice point ────────────
// (a strict graph: disconnecting a link that isn't there throws, as in a
// browser).  Either order, either undo: one path from the element to the EQ,
// none around the gate, and the mixer's input takes the same width.
const paths = (from, to, seen = new Set()) => from === to ? 1
  : from.to.reduce((c, e) => c + (seen.has(e.d) ? 0 : paths(e.d, to, new Set([...seen, e.d]))), 0);
function check(g, { width, gate }) {
  assert.deepEqual(g.env.warned, []);                               // nothing threw
  // one route from the element to the EQ (the matrix's own branches aside)
  const inner = g.core.on ? paths(g.core.stage.in, g.core.stage.out) : 1;
  assert.equal(paths(g.source, g.eqPreGain), inner, 'one route from the element to the EQ');
  if (gate) {
    assert.ok(gate.to.length === 1, 'the gate feeds the join only');
    assert.deepEqual(g.source.to.map((e) => e.d), [gate], 'the element reaches the EQ only through the gate');
    gate.gain.value = 0;                                            // the mixer closes it
    assert.deepEqual(evaluate(g.ctx, g.source, [1, 0], g.eqPreGain), [0, 0], 'a closed gate: no element');
    gate.gain.value = 1;
  }
  const out = evaluate(g.ctx, g.source, [1, 0], g.eqPreGain);       // a hard-left voice
  const want = width === 60 ? [0.8, 0.2] : [1, 0];
  assert.ok(Math.abs(out[0] - want[0]) < 1e-12 && Math.abs(out[1] - want[1]) < 1e-12, JSON.stringify(out));
}
const toFull = (g) => { g.env.store.sb_stereo_width = '100'; g.core.apply(g.env.track); g.env.timers.splice(0).forEach((t) => t.fn()); };

for (const order of [['width', 'voices'], ['voices', 'width']]) {
  test(`width and voices, ${order.join(' then ')}: composed; the width undone, then a second overlay`, () => {
    const g = make({ own: 60, strict: true });
    let ov = null;
    for (const s of order) { if (s === 'width') g.core.apply(g.env.track); else ov = g.core.voiceOverlay(); }
    assert.ok(ov && ov.gate && ov.input, 'the mixer is available');
    assert.equal(g.core.on, true);
    check(g, { width: 60, gate: ov.gate });
    // the mixer's render goes in at the join: the same width as the element
    const mix = evaluate(g.ctx, ov.input, [1, 0], g.eqPreGain);
    assert.ok(Math.abs(mix[0] - 0.8) < 1e-12 && Math.abs(mix[1] - 0.2) < 1e-12, JSON.stringify(mix));
    toFull(g);                                                      // the width back to 100 %
    assert.equal(g.core.on, false);
    check(g, { width: 100, gate: ov.gate });
    assert.equal(g.core.voiceOverlay().gate, ov.gate);              // reused, nothing rewired
    check(g, { width: 100, gate: ov.gate });
    g.env.store.sb_stereo_width = '60';                             // and wide again
    g.core.apply(g.env.track);
    check(g, { width: 60, gate: ov.gate });
  });
}

test('width on, off and on again with no mixer: the strict graph never throws', () => {
  const g = make({ own: 60, strict: true });
  g.core.apply(g.env.track);
  check(g, { width: 60 });
  toFull(g);
  check(g, { width: 100 });
  assert.deepEqual(g.source.to.map((e) => e.d), [g.eqPreGain]);    // exactly the chain without it
  g.env.store.sb_stereo_width = '60';
  g.core.apply(g.env.track);
  check(g, { width: 60 });
});

test('the mixer gate first, the width never: source → gate → join → EQ', () => {
  const g = make({ own: 100, strict: true });
  const ov = g.core.voiceOverlay();
  check(g, { width: 100, gate: ov.gate });
  assert.equal(g.core.stage, null);
});
