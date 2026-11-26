// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The phone's radio taking the output parks the library track
// (player.js ``suspendForExternalAudio`` → ``_parkForExternalAudio``, the REAL
// code through the queue harness): nothing stays loaded, the queue's current
// entry is cued at the place it had reached, and ▶ after the station starts
// it there.  Before, the takeover left trackId on the entry with no source:
// playPause did nothing and the phone hid its mini player.
// Run: node --test tests/js/m_radio_park.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { makePlayer, makeServer, rows } from './player_queue_harness.mjs';

// A queue of three playing its second entry at ``sec``.
async function playing(sec = 83.5) {
  const p = makePlayer({ server: makeServer(10) });
  p.setQueue(rows(0, 3).map((t) => ({ ...t, duration: 200 })), 1);
  await p.settle();
  p.setAudioSrc('/api/stream/t00001');
  p.env.currentTime = sec;
  return p;
}
// What radio-player.js does: suspend, then clear the element's source.
function radioTakes(p) { p.suspendForExternalAudio(true); p.setAudioSrc(''); }

test('the takeover parks the playing entry at its place; ▶ after the station is cued there', async () => {
  const p = await playing(83.5);
  assert.equal(p.trackId, 't00001');
  radioTakes(p);
  assert.equal(p.trackId, null, 'nothing is loaded any more');
  assert.equal(p.currentTrack, null);
  assert.equal(p.cuedTrack, null, 'radio owns the output: nothing cued while it plays');
  p.env.events.length = 0;
  p.suspendForExternalAudio(false);                // MobileRadio.stop()
  assert.equal(p.cuedTrack, p.queue[1]);
  assert.equal(p.cuedSec, 83.5);
  const cue = p.env.events.find((e) => e[0] === 'cue');
  assert.ok(cue, "'cue' told when the station stops");
  assert.equal(cue[1].track, p.queue[1]);
  assert.equal(cue[1].sec, 83.5);
  assert.deepEqual(p.resumeAt, { entry: p.queue[1], sec: 83.5 }, 'the next start of that entry seeks there');
});

test('a second station keeps the first park', async () => {
  const p = await playing(42);
  radioTakes(p);
  p.env.currentTime = 0;                           // (the detached element)
  p.suspendForExternalAudio(true);                 // another station tuned in
  p.suspendForExternalAudio(false);
  assert.equal(p.cuedSec, 42);
  // the queue sync sends the cued entry's place, not 0 (it used to overwrite it)
  assert.equal(p._qsyncBody().position, 42000);
});

test('no place to keep: the track at its end, a track outside the queue, nothing loaded', async () => {
  const end = await playing(199.5);
  radioTakes(end);
  end.suspendForExternalAudio(false);
  assert.equal(end.cuedTrack, end.queue[1]);
  assert.equal(end.cuedSec, 0, 'at its end: from the start');

  const other = await playing(50);
  other.playTrackDirect({ id: 'x1', title: 'From the info panel', duration: 300 });
  radioTakes(other);
  other.suspendForExternalAudio(false);
  assert.equal(other.cuedTrack, other.queue[1], "the queue's entry, like the desktop");
  assert.equal(other.cuedSec, 0);

  const none = makePlayer({ server: makeServer(10) });
  radioTakes(none);
  none.suspendForExternalAudio(false);
  assert.equal(none.cuedTrack, null);
  assert.equal(none.trackId, null);
});

test('the source of the park: player.js wires it into the takeover', async () => {
  const { readFileSync } = await import('node:fs');
  const SRC = readFileSync(new URL('../../soniqboom/frontend/js/player.js', import.meta.url), 'utf8');
  const sus = SRC.slice(SRC.indexOf('function suspendForExternalAudio('), SRC.indexOf('function _parkForExternalAudio('));
  assert.match(sus, /if \(_extAudio\) _parkForExternalAudio\(\);/);
  assert.match(sus, /if \(!_extAudio\) _emitCue\(\);/);
  // radio-player.js suspends BEFORE it clears the element's source (the place is read from it).
  const RP = readFileSync(new URL('../../soniqboom/frontend/js/mobile/radio-player.js', import.meta.url), 'utf8');
  const take = RP.slice(RP.indexOf('function _takeOver('), RP.indexOf('function _takeOver(') + 900);
  assert.ok(take.indexOf('suspendForExternalAudio(true)') < take.indexOf("removeAttribute('src')"));
});

test('a station that takes over while a resumed entry is still loading keeps that place', async () => {
  const p = makePlayer({ server: makeServer(10) });
  p.setQueue(rows(0, 3).map((t) => ({ ...t, duration: 200 })), 1);
  await p.settle();
  // A queue resumed at 45 s (as _applyServerQueue leaves it): stop, cue there.
  p.suspendForExternalAudio(true); p.setAudioSrc(''); p.suspendForExternalAudio(false);
  p.setResumeAt(p.queue[1], 45);
  p.setQueue(p.queue, 1);                 // ▶ (playPause → _playCurrent)
  await p.settle();
  p.setAudioSrc('/api/stream/t00001');
  p.env.currentTime = 0;                  // its render / load still under way
  p.suspendForExternalAudio(true); p.setAudioSrc('');   // a station takes over
  p.suspendForExternalAudio(false);       // …and stops
  assert.equal(p.cuedSec, 45, 'the pending place survives the station');
  assert.equal(p._qsyncBody().position, 45000, 'and is what the next queue save sends');
});
