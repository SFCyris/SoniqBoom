// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The search box's "Unknown field" hint (search.js _detectBadOperators): the
// phase-1 operators (length: added: rating: plays: played:) and the phase-2
// ones (folder: path: group: party: status: composer: title: lossless:
// bitdepth: samplerate: bitrate: source:) and the phase-3 ones (bpm: tunes:
// chip: dup: channels: has:) are known, the dropped instrument: label:
// comment: are not, an exclusion's "-" is no part of the name (-format:mp3,
// -genre:podcast), and a quoted value's inside is no operator.
// Run: node --test tests/js/search_operators.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const SRC = readFileSync(resolve(ROOT, 'soniqboom/frontend/js/search.js'), 'utf8');

// The two declarations, evaluated on their own (search.js imports the DOM-bound app).
const opsSrc = SRC.match(/const _SUPPORTED_OPS = new Set\(\[[\s\S]*?\]\);/)[0];
const fnSrc = SRC.match(/function _detectBadOperators\(text\) \{[\s\S]*?\n\}/)[0];
const detect = new Function(`${opsSrc}\n${fnSrc}\nreturn _detectBadOperators;`)();

test('every phase-1 operator is known', () => {
  for (const q of ['length:>5:00', 'added:<30d', 'rating:>=4', 'plays:0', 'played:<7d',
                   'length:1:02:03', 'added:2026-01-2026-06', 'rating:unrated',
                   'format:sid year:1985-1989 -genre:game', 'artist:nightwish track:3']) {
    assert.deepEqual(detect(q), [], q);
  }
});

test('every phase-2 operator is known', () => {
  for (const q of ['format:flac,alac', 'genre:metal,"hard rock"', 'folder:itunes',
                   'path:.flac', 'group:fairlight', 'party:revision', 'status:broken',
                   'composer:hubbard', 'title:"song of"', 'lossless:yes', 'bitdepth:24',
                   'samplerate:>48000', 'bitrate:<192', 'source:hvsc',
                   'Status:cantplay -source:local', 'folder:itunes -lossless:yes']) {
    assert.deepEqual(detect(q), [], q);
  }
});

test('every phase-3 operator is known; a quoted phrase is no operator', () => {
  for (const q of ['bpm:120-130', 'tunes:>1', 'chip:6581', 'chip:2sid',
                   'dup:yes', 'channels:>2', 'has:cover',
                   '"song of myself"', '-"live at" nightwish', '"time: the ride"',
                   'artist:nightwish "song of"']) {
    assert.deepEqual(detect(q), [], q);
  }
});

test('the dropped instrument: label: comment: are unknown fields', () => {
  assert.deepEqual(detect('instrument:bass'), ['instrument']);
  assert.deepEqual(detect('label:warp'), ['label']);
  assert.deepEqual(detect('comment:"ripped by"'), ['comment']);
});

test('an exclusion is checked by its name', () => {
  assert.deepEqual(detect('-format:mp3 -genre:podcast -live'), []);
  assert.deepEqual(detect('-Played:<7d -title:x'), []);
  assert.deepEqual(detect('-mood:x'), ['mood']);
  assert.deepEqual(detect('nightwish -lyrics:x'), ['lyrics']);
});

test('unknown fields are still flagged; quoted values are not operators', () => {
  assert.deepEqual(detect('mood:happy artist:Metallica'), ['mood']);
  assert.deepEqual(detect('genre:"foo: bar" tempo:120'), ['tempo']);
  assert.deepEqual(detect('album:"back on track: 2"'), []);
  assert.deepEqual(detect('a-ha x-ray'), []);
});
