// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every tune playable on its own (feature 7): the tunes of a tracker module's
// subsongs, a GoatTracker song, a FamiTracker module and a HES / KSS file use
// the plain wire mapping — wire w is tune w + 1 (no start-song swap: these
// files have no header start song) — in utils.js subsongWireToTune, the same
// mapping the server's tune_index and Subsonic's wire_tune use
// (tests/test_every_tune.py).  Their tune lists are asked for even when a
// row doesn't count tunes yet (tunes.js TUNE_CHIP_FORMAT_NAMES), and a tune's
// queue entry carries its own length (subsongVirtualTrack).
// Run: node --test tests/js/every_tune.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const { subsongStartOf, subsongWireToTune, subsongTuneToWire, loudnessTuneKey } =
  await import(resolve(FE, 'js/utils.js'));

// (tunes.js builds DOM badges; its pure parts are imported with a stub document)
globalThis.document = globalThis.document || { createElement: () => ({}) };
const { TUNE_CHIP_FORMAT_NAMES, subsongVirtualTrack } = await import(resolve(FE, 'js/tunes.js'));

const ROWS = [
  { id: 'm', format: 'ProTracker', subsongs: 2, tune_lengths: [38.4, 2.64], duration: 38.4 },
  { id: 'g', format: 'GoatTracker', subsongs: 4, duration: 180 },
  { id: 'f', format: 'FamiTracker', subsongs: 3, duration: 21.9 },
  { id: 'k', format: 'KSS', subsongs: 3, tune_slots: [0, 2, 5], tune_lengths: [180, 180, 180] },
  { id: 'h', format: 'HES', subsongs: 5, tune_slots: [0, 1, 2, 7, 9] },
];

test('the wire is the tune index: wire w plays tune w + 1, every tune one wire', () => {
  for (const r of ROWS) {
    const s = subsongStartOf(r);
    assert.equal(s, 1, r.format);
    for (let w = 0; w < r.subsongs; w++) {
      assert.equal(subsongWireToTune(w, s, r.subsongs), w + 1, `${r.format} w=${w}`);
      assert.equal(subsongTuneToWire(w + 1, s, r.subsongs), w);
    }
  }
});

test('their tune lists are asked for before a row counts them', () => {
  for (const f of ['GoatTracker', 'FamiTracker', 'KSS', 'HES', 'ProTracker', 'FastTracker 2',
                   'Impulse Tracker']) {
    assert.ok(TUNE_CHIP_FORMAT_NAMES.has(f), f);
  }
});

test('a picked tune\'s queue entry has its own length and label', () => {
  const m = ROWS[0];
  const t2 = subsongVirtualTrack(m, 1, { count: 2, start: 1, lengths: m.tune_lengths });
  assert.equal(t2.subsong, 1);
  assert.equal(t2.duration, 2.64);
  assert.equal(t2.subsongLabel, 'Tune 2');
  const g3 = subsongVirtualTrack(ROWS[1], 2, { count: 4, start: 1 });
  assert.equal(g3.duration, 0, 'an unknown length, never the default tune\'s');
  // (its measured loudness is keyed by the same tune index — a dense one for HES / KSS)
  assert.equal(loudnessTuneKey({ ...ROWS[3], subsong: 2 }), '2');
});
