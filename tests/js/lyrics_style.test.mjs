// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The lyrics panel in the shared Modern / Retro hi-fi style (css/lyrics-styles.css):
// the stylesheet is linked once after the other looks' sheets, and the LRC
// parser the panel and Track Info's Lyrics tab share (utils.js parseLRC) drops
// ID tags, applies [offset:] and keeps an untimed line without a time.
// Run: node --test tests/js/lyrics_style.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');

test('the lyrics stylesheet is linked once, after the other looks', () => {
  const html = read('index.html');
  assert.equal((html.match(/\/assets\/css\/lyrics-styles\.css\?v=\d+/g) || []).length, 1);
  for (const sheet of ['stations-styles.css', 'playlist-styles.css', 'player-styles.css', 'queue-styles.css']) {
    assert.ok(html.indexOf(sheet) < html.indexOf('lyrics-styles.css'), `${sheet} first`);
  }
});

test('one LRC parser: ID tags dropped, [offset:] applied, untimed lines kept without a time', async () => {
  const { parseLRC } = await import(resolve(FE, 'js/utils.js'));
  const got = parseLRC('[ar:Someone]\n[ti:A Song]\n[offset:+1000]\n[00:05.00]five\nspoken\n[00:03.50]three');
  assert.deepEqual(got, [
    { time: 4, text: 'five' },
    { time: null, text: 'spoken' },
    { time: 2.5, text: 'three' },
  ]);
  assert.deepEqual(parseLRC('[00:00.40]first\n[offset:+1000]').map(l => l.time), [0]);   // never below 0
});

test('the panel and Track Info both read lyrics through it', () => {
  assert.match(read('js/library.js'), /const _parseLRC = parseLRC;/);
  assert.match(read('js/trackinfo.js'), /parseLRC\(text\)\.filter\(l => l\.time != null\)/);
});
