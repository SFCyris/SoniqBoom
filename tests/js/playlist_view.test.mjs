// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

// The playlist view's pure parts: format labels / mix / total length
// (js/playlist/hero.js), the batched waveform store and the mini-waveform
// drawing (js/playlist/waves.js), and the shared look (js/ui_style.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const { fmtShort, fmtFamily, formatMix, familyMix, totalSeconds } = await import(resolve(FE, 'js/playlist/hero.js'));
globalThis.window = globalThis.window || { devicePixelRatio: 1 };
const { createWaveStore, drawWave } = await import(resolve(FE, 'js/playlist/waves.js'));

const T = (format, path, duration = 0) => ({ id: path, format, path, duration });

test('short format labels: the short name, else the extension', () => {
  assert.equal(fmtShort(T('FLAC', '/m/a.flac')), 'FLAC');
  assert.equal(fmtShort(T('PSID', '/m/a.sid')), 'SID');
  assert.equal(fmtShort(T('Ogg Vorbis', '/m/a.ogg')), 'OGG');
  assert.equal(fmtShort(T('ProTracker', '/m/a.mod')), 'MOD');
  assert.equal(fmtShort(T('AdLib Tracker 2', '/m/A.A2M')), 'A2M');
  assert.equal(fmtShort(T('ProTracker', '/m/pack.lha::songs/b.mod')), 'MOD');   // an archive member
  assert.equal(fmtShort(T('', '/m/noext')), '?');
});

test('format families colour the mix', () => {
  assert.equal(fmtFamily(T('FLAC', '/a.flac')), 'lossless');
  assert.equal(fmtFamily(T('MP3', '/a.mp3')), 'lossy');
  assert.equal(fmtFamily(T('Ogg Vorbis', '/a.ogg')), 'lossy');
  assert.equal(fmtFamily(T('SID', '/a.sid')), 'sid');
  assert.equal(fmtFamily(T('ProTracker', '/a.mod')), 'tracker');
  assert.equal(fmtFamily(T('NSF', '/a.nsf')), 'chip');
  assert.equal(fmtFamily(T('SNDH', '/a.sndh')), 'chip');
  assert.equal(fmtFamily(T('MIDI', '/a.mid')), 'midi');
});

test('the mix counts by label, most first, ties by name; total length', () => {
  const tr = [T('SID', '/1.sid', 100), T('FLAC', '/2.flac', 60), T('SID', '/3.sid', 0),
              T('ProTracker', '/4.mod', 30), T('FLAC', '/5.flac', 10), T('SID', '/6.sid', 5)];
  assert.deepEqual(formatMix(tr).map((g) => [g.label, g.count]), [['SID', 3], ['FLAC', 2], ['MOD', 1]]);
  assert.deepEqual(totalSeconds(tr), { sec: 205, unknown: 1 });
  assert.deepEqual(formatMix([]), []);
});

test('the mix bar groups the formats by family (one colour each)', () => {
  const tr = [T('NSF', '/1.nsf'), T('SPC', '/2.spc'), T('SNDH', '/3.sndh'), T('FLAC', '/4.flac'),
              T('ProTracker', '/5.mod'), T('MP3', '/6.mp3'), T('Ogg Vorbis', '/7.ogg'), T('NSF', '/8.nsf')];
  const fam = familyMix(formatMix(tr));
  assert.deepEqual(fam.map((f) => [f.family, f.count]), [['chip', 4], ['lossy', 2], ['lossless', 1], ['tracker', 1]]);
  assert.deepEqual(fam[0].labels, [{ label: 'NSF', count: 2 }, { label: 'SNDH', count: 1 }, { label: 'SPC', count: 1 }]);
});

test('memoised labels never leak into a JSON copy of the track', () => {
  const t = T('FLAC', '/x.flac');
  fmtShort(t); fmtFamily(t);
  assert.deepEqual(Object.keys(JSON.parse(JSON.stringify(t))).sort(), ['duration', 'format', 'id', 'path']);
});

test('the waveform store batches the rows asked together, once each', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push(body.ids);
    return { ok: true, json: async () => ({ points: body.points, waveforms: { a: [0, 128, 255] } }) };
  };
  let ready = null;
  const ws = createWaveStore({ onReady: (ids) => { ready = ids; } });
  ws.want('a'); ws.want('b'); ws.want('a');
  assert.equal(ws.state('a'), undefined);
  await new Promise((r) => setTimeout(r, 80));
  assert.deepEqual(calls, [['a', 'b']]);
  assert.deepEqual(ready, ['a', 'b']);
  assert.deepEqual([...ws.state('a')], [0, 128, 255]);
  assert.equal(ws.state('b'), null);                       // none stored: the placeholder
  ws.want('a'); ws.want('b');
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(calls.length, 1);                           // known: not asked again
  ws.forgetMissing();                                      // (the playlist opened again)
  ws.want('b');
  await new Promise((r) => setTimeout(r, 80));
  assert.deepEqual(calls[1], ['b']);
});

test('a failed batch shows the placeholder, no retry loop', async () => {
  let n = 0;
  globalThis.fetch = async () => { n++; throw new Error('offline'); };
  const ws = createWaveStore({});
  ws.want('z');
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(ws.state('z'), null);
  ws.want('z');
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(n, 1);
});

function fakeCanvas() {
  const rects = [];
  const ctx = {
    fillStyle: '', setTransform() {}, clearRect() {},
    fillRect(x, y, w, h) { rects.push({ x, y, w, h, c: this.fillStyle }); },
  };
  return { width: 0, height: 0, getContext: () => ctx, rects };
}
const COLS = { bar: 'B', played: 'P', rest: 'R', ph: 'H', head: 'X' };

test('drawWave: bars by amplitude; the played part lit up to the playhead', () => {
  const cv = fakeCanvas();
  drawWave(cv, Uint8Array.from([255, 0, 128, 255]), 40, 20, -1, COLS);
  assert.equal(cv.width, 40); assert.equal(cv.height, 20);
  assert.deepEqual(cv.rects.map((r) => r.c), ['B', 'B', 'B', 'B']);
  assert.equal(cv.rects[0].h, 18); assert.equal(cv.rects[1].h, 1);
  const pv = fakeCanvas();
  drawWave(pv, Uint8Array.from([255, 255, 255, 255]), 40, 20, 0.5, COLS);
  assert.deepEqual(pv.rects.map((r) => r.c), ['P', 'P', 'R', 'R', 'X']);
  assert.equal(pv.rects[4].x, 20);                          // the playhead at 50 %
});

test('drawWave: no waveform → a flat bar as tall as a full-scale bar, its played part lit', () => {
  const cv = fakeCanvas();
  drawWave(cv, null, 16, 10, 0.25, COLS);
  assert.deepEqual(cv.rects.map((r) => r.c), ['H', 'P', 'X']);
  assert.deepEqual([cv.rects[0].y, cv.rects[0].w, cv.rects[0].h], [1, 16, 8]);
  assert.equal(cv.rects[1].w, 4);
});

test('drawWave: no waveform with cells → unlit cells on the waveform\'s pitch, the played ones lit', () => {
  const cv = fakeCanvas();
  drawWave(cv, null, 40, 20, 0.5, { ...COLS, cells: 4 });
  assert.deepEqual(cv.rects.map((r) => r.c), ['P', 'P', 'H', 'H', 'X']);
  assert.deepEqual(cv.rects.slice(0, 4).map((r) => [r.x, r.h]), [[0, 18], [10, 18], [20, 18], [30, 18]]);
  const bars = fakeCanvas();                                  // (the same pitch as a stored waveform's bars)
  drawWave(bars, Uint8Array.from([255, 255, 255, 255]), 40, 20, -1, COLS);
  assert.deepEqual(bars.rects.map((r) => [r.x, r.w]), cv.rects.slice(0, 4).map((r) => [r.x, r.w]));
});

test('ui_style.js: the shared look API and its storage key', () => {
  const src = readFileSync(resolve(FE, 'js/ui_style.js'), 'utf8');
  for (const name of ['getUiStyle', 'setUiStyle', 'onUiStyleChange']) assert.match(src, new RegExp(`export function ${name}\\(`));
  assert.match(src, /const KEY = 'sb_ui_style';/);
  assert.match(src, /new CustomEvent\('sb:ui-style', \{ detail: \{ style:/);
  assert.match(src, /addEventListener\('storage'/);
  const pl = readFileSync(resolve(FE, 'js/playlist.js'), 'utf8');
  assert.match(pl, /onUiStyleChange\(_applyStyle\)/);
  // (the playlist follows the look; the Style switch is the equalizer's and Settings')
  assert.doesNotMatch(pl, /setUiStyle|onStyle/);
});

test('wiring: reorder only in an own, ordinary playlist; the stylesheet is linked once', () => {
  const pl = readFileSync(resolve(FE, 'js/playlist.js'), 'utf8');
  assert.match(pl, /function _canReorder\(\) \{ return !!_activeId && !_activeRO && !_activeSmart; \}/);
  assert.match(pl, /if \(!_canReorder\(\)\) \{ e\.preventDefault\(\); return; \}/);
  const html = readFileSync(resolve(FE, 'index.html'), 'utf8');
  assert.equal(html.split('/assets/css/playlist-styles.css?v=').length - 1, 1);
});
