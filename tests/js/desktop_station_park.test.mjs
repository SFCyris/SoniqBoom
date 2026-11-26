// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The desktop's station start parks the library track like the phone's radio
// takeover does (player.js ``playStation`` → ``_parkForExternalAudio``), and
// ``stopStation`` announces the parked entry ('cue'), so ▶ after a station
// resumes the queue's current entry at the place it had reached instead of
// from 0:00.  The park itself is exercised through the queue harness in
// m_radio_park.test.mjs; this file pins the desktop wiring.
// Run: node --test tests/js/desktop_station_park.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const SRC = readFileSync(new URL('../../soniqboom/frontend/js/player.js', import.meta.url), 'utf8');
const body = (name) => {
  const at = SRC.indexOf(`function ${name}(`);
  assert.ok(at > 0, `${name} exists`);
  const open = SRC.indexOf(') {', at) + 2;     // (past a default param like ``opts = {}``)
  let depth = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === '{') depth++;
    else if (SRC[i] === '}' && --depth === 0) return SRC.slice(open, i + 1);
  }
  throw new Error(`unbalanced ${name}`);
};

test('playStation parks the playing library track before station mode clears it', () => {
  const b = body('playStation');
  const park = b.indexOf('_parkForExternalAudio()');
  assert.ok(park > 0, 'playStation calls _parkForExternalAudio');
  assert.match(b.slice(0, park + 40), /if \(!_stationMode && !_extAudio\) _parkForExternalAudio\(\)/,
    'only for a library track (station → station keeps the first park; the phone parks itself)');
  assert.ok(park < b.indexOf('_stationMode  = true'), 'parked before station mode is set');
  assert.ok(park < b.indexOf('trackId       = null'), 'parked before trackId is cleared (the park reads it)');
  assert.ok(park < b.indexOf("audio.removeAttribute('src')"), 'parked before the element is detached (the park reads its time)');
});

test('stopStation announces the parked entry so the bar shows what ▶ resumes', () => {
  const b = body('stopStation');
  const cue = b.indexOf('_emitCue()');
  assert.ok(cue > 0, 'stopStation emits the cue');
  assert.ok(cue > b.indexOf("audio.removeAttribute('src')"), 'after the source is gone (_cuedTrack needs no src)');
  assert.ok(cue > b.indexOf('_stationMode = false'), 'after station mode ends (_cuedTrack refuses in station mode)');
});

test('the resumed start seeks to the parked place (playTrack reads _resumeAt for that entry)', () => {
  const b = body('playTrack');
  assert.match(b, /_resumeSeek = \(_resumeAt && _resumeAt\.entry === track\)/);
});
