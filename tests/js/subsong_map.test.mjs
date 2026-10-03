// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Multi-tune files: the wire index (?subsong=) ↔ tune number mapping the web
// player, the Track Info picker and the in-browser SID worker share
// (utils.js subsongWireToTune / subsongTuneToWire; vu-sid-worker.js
// tuneForWire).  With the WASM core and the local SID fixture present, the
// worker's mapping is also checked against real renders.
// Run: node --test tests/js/subsong_map.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const { subsongStart, subsongStartOf, subsongWireToTune, subsongTuneToWire } =
  await import(resolve(FE, 'js/utils.js'));

test('start song 1: wire w is tune w+1', () => {
  assert.deepEqual([0, 1, 2].map((w) => subsongWireToTune(w, 1, 3)), [1, 2, 3]);
  assert.deepEqual([1, 2, 3].map((t) => subsongTuneToWire(t, 1, 3)), [0, 1, 2]);
});

test('start song s != 1: wire 0 is tune s, wire s-1 is tune 1, the rest w+1', () => {
  // The regression case: a PSID with start=2 of 3 tunes → wires 0/1/2 play -o2/-o1/-o3.
  assert.deepEqual([0, 1, 2].map((w) => subsongWireToTune(w, 2, 3)), [2, 1, 3]);
  assert.deepEqual([0, 1, 2, 3].map((w) => subsongWireToTune(w, 3, 4)), [3, 2, 1, 4]);
  assert.equal(subsongTuneToWire(1, 3, 4), 2);
  assert.equal(subsongTuneToWire(3, 3, 4), 0);
});

test('every tune has exactly one wire, and the two directions agree', () => {
  for (let n = 1; n <= 9; n++) {
    for (let s = 1; s <= n; s++) {
      const tunes = [];
      for (let w = 0; w < n; w++) {
        const t = subsongWireToTune(w, s, n);
        assert.ok(t >= 1 && t <= n, `n=${n} s=${s} w=${w} → ${t}`);
        assert.equal(subsongTuneToWire(t, s, n), w, `n=${n} s=${s} w=${w}`);
        tunes.push(t);
      }
      assert.deepEqual([...tunes].sort((a, b) => a - b), Array.from({ length: n }, (_, i) => i + 1));
      assert.equal(subsongWireToTune(0, s, n), s, 'wire 0 is the default tune');
    }
  }
});

test('a start song outside 1..N (or none) counts as 1', () => {
  for (const s of [0, -1, 9, NaN, undefined, null, 'x']) {
    assert.equal(subsongStart(s, 4), 1, String(s));
    assert.equal(subsongWireToTune(1, s, 4), 2);
  }
  assert.equal(subsongStart(3, 0), 3, 'count unknown: trusted');
});

test('the start song a track object carries', () => {
  assert.equal(subsongStartOf({ subsongStart: 3, default_track: 2 }), 3);
  assert.equal(subsongStartOf({ default_track: 2 }), 2);
  assert.equal(subsongStartOf({ start_subsong: 1 }), 2, 'the scan\'s 0-based index');
  assert.equal(subsongStartOf({}), 1);
  assert.equal(subsongStartOf(null), 1);
});

// ── The SID worker (a classic worker: its own copy of the mapping) ───────────
const WORKER_SRC = readFileSync(resolve(FE, 'js/vu-sid-worker.js'), 'utf8');
const tuneForWire = (() => {
  const m = WORKER_SRC.match(/function tuneForWire\(bytes, wire\) \{[\s\S]*?\n\}/);
  assert.ok(m, 'tuneForWire not found in vu-sid-worker.js');
  return new Function(`${m[0]}; return tuneForWire;`)();
})();

function psid(songs, start, magic = 'PSID') {
  const b = new Uint8Array(0x7c);
  for (let i = 0; i < 4; i++) b[i] = magic.charCodeAt(i);
  b[0x0e] = songs >> 8; b[0x0f] = songs & 0xff;
  b[0x10] = start >> 8; b[0x11] = start & 0xff;
  return b;
}

test('the worker maps a wire to sid_load\'s tune exactly like utils.js', () => {
  for (const magic of ['PSID', 'RSID']) {
    for (let n = 1; n <= 6; n++) {
      for (let s = 1; s <= n; s++) {
        const bytes = psid(n, s, magic);
        assert.equal(tuneForWire(bytes, 0), 0, 'wire 0 → 0: sid_load plays the start song');
        for (let w = 1; w < n; w++) {
          assert.equal(tuneForWire(bytes, w), subsongWireToTune(w, s, n), `${magic} n=${n} s=${s} w=${w}`);
        }
      }
    }
  }
  // Not a SID header / an out-of-range start: start song 1.
  assert.equal(tuneForWire(new Uint8Array(4), 2), 3);
  assert.equal(tuneForWire(psid(4, 9), 2), 3);
});

// ── Against the real WASM core (local fixture; skipped when absent) ──────────
const FIXTURE = resolve(HERE, '../../internal/testdata/sid/SX-64_Demo.sid');
const GLUE = resolve(FE, 'js/vendor/sidwasm.js');
test('real renders: with the start song patched to 3, each wire renders its tune', {
  skip: !(existsSync(FIXTURE) && existsSync(GLUE)) && 'SID fixture or WASM core not present',
}, async () => {
  const require = createRequire(import.meta.url);
  const src = readFileSync(GLUE, 'utf8');
  const createSidModule = new Function('require', '__filename', '__dirname',
    `${src};return createSidModule;`)(require, GLUE, dirname(GLUE));
  const orig = new Uint8Array(readFileSync(FIXTURE));
  assert.equal(((orig[0x0e] << 8) | orig[0x0f]), 4, 'fixture has 4 tunes');
  const patched = orig.slice();
  patched[0x10] = 0; patched[0x11] = 3;              // start song 3
  // A per-voice VU fingerprint of the first 5 s of one tune (fresh module per
  // render: reusing one leaks state between loads).
  async function fingerprint(bytes, tune) {
    const M = await createSidModule({ locateFile: (p) => resolve(dirname(GLUE), p) });
    M.cwrap('sid_set_power_delay', null, ['number'])(0);
    const load = M.cwrap('sid_load', 'number', ['number', 'number', 'number']);
    const rv = M.cwrap('sid_render_vu', 'number', ['number', 'number', 'number']);
    const p = M._malloc(bytes.length); M.HEAPU8.set(bytes, p);
    const songs = load(p, bytes.length, tune); M._free(p);
    assert.ok(songs > 0);
    const out = M._malloc(150 * 3 * 4);
    const got = rv(out, 150, 1470);
    let h = 0;
    for (const x of M.HEAPF32.subarray(out >> 2, (out >> 2) + got * 3)) h = (h * 31 + Math.round(x * 1e4)) | 0;
    return h;
  }
  const byTune = [];
  for (let t = 1; t <= 4; t++) byTune[t] = await fingerprint(orig, t);
  assert.equal(new Set(byTune.slice(1)).size, 4, 'the four tunes differ');
  const want = [3, 2, 1, 4];                          // wires 0..3 with start song 3
  for (let w = 0; w < 4; w++) {
    const got = await fingerprint(patched, tuneForWire(patched, w));
    assert.equal(got, byTune[want[w]], `wire ${w} should render tune ${want[w]}`);
  }
});

test('a picked tune is the file plus its wire, count, start song and label', async () => {
  const { subsongVirtualTrack, TUNE_CHIP_FORMAT_NAMES } = await import(resolve(FE, 'js/tunes.js'));
  const file = { id: 'f', title: 'Song', duration: 19.2, subsongs: 8, start_subsong: 1 };
  // default tune 2 of 8 (an empty first tune): wire 0 is tune 2, wire 1 tune 1
  const def = subsongVirtualTrack(file, 0, { count: 8, start: 2 });
  assert.deepEqual([def.subsong, def.subsongTotal, def.subsongStart, def.subsongLabel, def.duration],
                   [0, 8, 2, 'Tune 2', 19.2], 'wire 0 keeps the file\'s (default tune\'s) length');
  const first = subsongVirtualTrack(file, 1, { count: 8, start: 2 });
  assert.equal(first.subsongLabel, 'Tune 1');
  assert.equal(first.duration, 0, 'another tune\'s length is unknown until it plays');
  const third = subsongVirtualTrack(file, 2, { count: 8, start: 2, lengths: [0.3, 19.2, 24.6] });
  assert.deepEqual([third.subsongLabel, third.duration], ['Tune 3', 24.6]);
  assert.equal(file.subsong, undefined, 'the file object is not modified');
  // an out-of-range start song counts as 1
  assert.equal(subsongVirtualTrack(file, 0, { count: 3, start: 9 }).subsongStart, 1);
  assert.ok(TUNE_CHIP_FORMAT_NAMES.has('GBS') && TUNE_CHIP_FORMAT_NAMES.has('NSF'));
  assert.ok(!TUNE_CHIP_FORMAT_NAMES.has('SPC'), 'one tune per file');
});
