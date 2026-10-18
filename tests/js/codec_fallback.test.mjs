// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A file the browser can't decode (an IMA-ADPCM WAV): the <audio> 'error'
// handler plays the server's transcode instead (force_transcode) — and the
// play() failure report of that start stays quiet unless the transcode fails
// too.  Firefox reports such a file as a DECODE error (code 3), Chromium as
// SRC_NOT_SUPPORTED (code 4): both get the transcode.  Runs the REAL helpers
// from player.js (render_core_harness) plus source checks of the wiring.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { makeCore, makeServer } from './render_core_harness.mjs';

const SRC = readFileSync(process.env.SB_PLAYER_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/player.js'), 'utf8');

const wav = (over = {}) => ({ id: 'w1', title: 'adpcm_tone', format: 'WAV', duration: 3, ...over });
// The stream answers 206: the file is there, the browser just can't decode it.
function okServer() {
  const srv = makeServer();
  srv.onStream = async () => ({ ok: true, status: 206, headers: { get: () => null }, json: async () => ({}) });
  return srv;
}
const errors = (c) => c.env.toasts.filter(([k]) => k === 'error');

test('a codec failure whose transcode plays: no "Couldn’t play" toast', async () => {
  const c = makeCore({ server: okServer() });
  const t = wav();
  const seq = c.start(t);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);   // play() rejected…
  await c.settle();                                                       // (the probe answered 206)
  c._codecRetryBegin(seq);                                                // …then the element's 'error' (code 4), within 250 ms
  await c.run(4);
  assert.equal(errors(c).length, 0);
  c._codecRetryEnd(seq, true);                                            // the transcode plays
  await c.run(4);
  assert.equal(errors(c).length, 0);
});

test('the transcode fails too: the failure is said, once', async () => {
  const c = makeCore({ server: okServer() });
  const t = wav();
  const seq = c.start(t);
  c._codecRetryBegin(seq);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.equal(errors(c).length, 0);
  c._codecRetryEnd(seq, false);
  await c.run(2);
  assert.equal(errors(c).length, 1);
  assert.match(errors(c)[0][1], /Couldn’t play “adpcm_tone”/);
});

test('no transcode tried (another failure): reported as before', async () => {
  const c = makeCore({ server: okServer() });
  const t = wav();
  const seq = c.start(t);
  c._reportPlayFailure(t, t.title, { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.equal(errors(c).length, 1);
});

test('a transcode of an older start does not silence a new start', async () => {
  const c = makeCore({ server: okServer() });
  const t = wav();
  const old = c.start(t);
  c._codecRetryBegin(old);
  const seq = c.start(wav({ id: 'w2', title: 'other' }));
  c._reportPlayFailure(wav({ id: 'w2', title: 'other' }), 'other', { name: 'NotSupportedError' }, seq);
  await c.run(4);
  assert.equal(errors(c).length, 1);
});

test('wiring: decode errors (Firefox) get the transcode too, and the error starts the wait', () => {
  const elig = SRC.slice(SRC.indexOf('function _codecFallbackEligible('), SRC.indexOf('function _codecFallbackEligible(') + 500);
  assert.match(elig, /err\.code !== 3 && err\.code !== 4/);
  const onErr = SRC.slice(SRC.indexOf("audio.addEventListener('error', () => {"), SRC.indexOf('function _onMediaError('));
  assert.match(onErr, /if \(_codecFallbackEligible\(err, id\)\) _codecRetryBegin\(_playSeq\);/);
  const onMe = SRC.slice(SRC.indexOf('function _onMediaError('), SRC.indexOf('function _reportMediaError('));
  assert.match(onMe, /if \(!rendered && !refused && _codecFallbackEligible\(err, id\)\)/);
  assert.match(onMe, /_codecRetryEnd\(seq, true\);\s*emit\('statechange', \{ playing: true \}\);/);
  assert.match(onMe, /setTimeout\(\(\) => _codecRetryEnd\(seq, false\), 30000\);/);   // a transcode that never answers
});
