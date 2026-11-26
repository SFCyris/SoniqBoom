// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Volume leveling (utils.js levelingGain / loudnessTuneKey / measuredLoudness):
// ReplayGain tags first (Per track / Per album / Auto), else the loudness the
// server measured (core/loudness.py) as a track gain of −18 − LUFS dB, capped
// at 0.99 / true peak; an overloaded render's 1 / peak applies with tags or
// none, never on top of a measurement (whose true-peak cap covers it).
// Run: node --test tests/js/leveling.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const { levelingGain, loudnessTuneKey, measuredLoudness, LEVELING_REF_LUFS } =
  await import(resolve(FE, 'js/utils.js'));

const db = (g) => 20 * Math.log10(g);
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} vs ${b}`);

test('the reference is ReplayGain 2.0\'s −18 LUFS', () => {
  assert.equal(LEVELING_REF_LUFS, -18);
});

test('tags: the mode\'s gain with its own peak, the other as fallback (unchanged behaviour)', () => {
  const t = { replaygain_track_gain: -6, replaygain_track_peak: 0.5,
              replaygain_album_gain: -3, replaygain_album_peak: 0.9 };
  near(db(levelingGain(t, 'track')), -6);
  near(db(levelingGain(t, 'album')), -3);
  near(levelingGain(t, 'off'), 1);
  // +6 dB on a 0.95 peak is capped to 0.99 / 0.95
  near(levelingGain({ replaygain_track_gain: 6, replaygain_track_peak: 0.95 }, 'track'), 0.99 / 0.95);
  near(levelingGain({ replaygain_track_gain: '-4.5 dB' }, 'album'), Math.pow(10, -4.5 / 20));
  // an overloaded render's 1/peak multiplies with tags and with nothing
  near(levelingGain({ replaygain_track_gain: -6 }, 'track', { render: 0.5 }), Math.pow(10, -6 / 20) * 0.5);
  near(levelingGain({}, 'track', { render: 0.5 }), 0.5);
  near(levelingGain(null, 'album'), 1);
});

test('auto: album gain while an album plays in order, else the track gain', () => {
  const t = { replaygain_track_gain: -6, replaygain_album_gain: -3 };
  near(db(levelingGain(t, 'auto', { albumRun: true })), -3);
  near(db(levelingGain(t, 'auto', { albumRun: false })), -6);
});

test('a measured tune levels to −18 LUFS, capped by its true peak, never × the render peak', () => {
  const t = { id: 'x', format: 'ProTracker', loudness: { 0: [-10, 0.5] } };
  near(db(levelingGain(t, 'track')), -8);
  near(db(levelingGain(t, 'album')), -8);              // (no album measurement: the track's)
  near(db(levelingGain(t, 'auto', { albumRun: true })), -8);
  near(db(levelingGain(t, 'track', { render: 0.5 })), -8);       // TP covers the render
  // a quiet tune with a hot peak is raised only to 0.99 / TP
  const q = { id: 'q', format: 'SID', loudness: { 0: [-24, 0.9] } };
  near(levelingGain(q, 'track'), 0.99 / 0.9);
  // never above +12 dB
  near(levelingGain({ id: 'z', format: 'SID', loudness: { 0: [-50, 0.01] } }, 'track'), 3.98);
  // tags win over a measurement
  near(db(levelingGain({ ...t, replaygain_track_gain: -2 }, 'track')), -2);
  // Off stays off
  near(levelingGain(t, 'off'), 1);
  // a measurement learnt this page load (render-status) when the row has none
  near(db(levelingGain({ id: 'y', format: 'SPC' }, 'track', { measured: [-12, 0.5] })), -6);
  // malformed entries are ignored
  near(levelingGain({ id: 'y', loudness: { 0: ['a', 1] } }, 'track'), 1);
  near(levelingGain({ id: 'y', loudness: { 0: [-12, 0] } }, 'track'), 1);
});

test('the tune key is the render\'s tune index (stream.tune_index)', () => {
  // SID / SNDH: the wire; a bare play wire 0
  assert.equal(loudnessTuneKey({ format: 'SID', subsong: 2 }), '2');
  assert.equal(loudnessTuneKey({ format: 'SID', default_subsong: 3 }), '0');
  assert.equal(loudnessTuneKey({ format: 'SNDH' }), '0');
  // others: the wire, else the decided default tune, else 0
  assert.equal(loudnessTuneKey({ format: 'NSF', subsong: 0 }), '0');
  assert.equal(loudnessTuneKey({ format: 'NSF', subsong: 4 }), '4');
  assert.equal(loudnessTuneKey({ format: 'NSF', default_subsong: 3 }), '3');
  assert.equal(loudnessTuneKey({ format: 'ProTracker' }), '0');
  assert.equal(loudnessTuneKey(null), '0');
  const t = { format: 'NSF', subsong: 1, loudness: { 0: [-10, 0.5], 1: [-20, 0.3] } };
  assert.deepEqual(measuredLoudness(t), [-20, 0.3]);
  assert.equal(measuredLoudness({ ...t, subsong: 2 }), null);
});
