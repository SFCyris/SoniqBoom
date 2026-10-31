// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The phone Now Playing's meters and visuals (js/mobile/views/np_visuals.js)
// and the modules it shares with the desktop: vu/vumr.js (the per-voice VU
// meter's data), vu/needles.js (the needle meters), viz/bands.js and
// viz/phosphor.js (the spectrum analyser and the phosphor scope).
// Run: node --test tests/js/np_visuals.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');
const VU = await import('../../soniqboom/frontend/js/vu/vumr.js');
const BANDS = await import('../../soniqboom/frontend/js/viz/bands.js');
const { createPhosphor } = await import('../../soniqboom/frontend/js/viz/phosphor.js');

function vumrBuf(channels, rateHz, frames, fill) {
  const buf = new ArrayBuffer(16 + channels + frames * channels);
  const u = new Uint8Array(buf), dv = new DataView(buf);
  u.set([86, 85, 77, 82]);             // "VUMR"
  u[4] = 1; u[5] = channels;
  dv.setUint32(8, rateHz, true); dv.setUint32(12, frames, true);
  for (let f = 0; f < frames; f++) for (let c = 0; c < channels; c++) u[16 + channels + f * channels + c] = fill(f, c);
  return buf;
}

test('a VUMR sidecar parses; a wrong magic, version or size does not', () => {
  const v = VU.parseVUMR(vumrBuf(4, 30, 10, (f, c) => f * 10 + c));
  assert.deepEqual([v.channels, v.rateHz, v.frames, v.samples.length], [4, 30, 10, 40]);
  const bad = vumrBuf(4, 30, 10, () => 0);
  new Uint8Array(bad)[0] = 0;
  assert.equal(VU.parseVUMR(bad), null);
  const v2 = vumrBuf(4, 30, 10, () => 0); new Uint8Array(v2)[4] = 2;
  assert.equal(VU.parseVUMR(v2), null);
  assert.equal(VU.parseVUMR(vumrBuf(4, 30, 10, () => 0).slice(0, 40)), null, 'truncated');
  assert.equal(VU.parseVUMR(new ArrayBuffer(8)), null);
});

test('the voices\' levels follow the playback time (gamma 0.7); an empty sidecar reads 0', () => {
  const v = VU.parseVUMR(vumrBuf(2, 10, 5, (f, c) => (c === 0 ? 255 : f * 50)));
  const out = new Float32Array(2);
  VU.vumrLevelsAt(v, 0.25, out);         // frame 2
  assert.equal(out[0], 1);
  assert.ok(Math.abs(out[1] - Math.pow(100 / 255, 0.7)) < 1e-6);
  VU.vumrLevelsAt(v, 99, out);           // past the end: the last frame
  assert.ok(Math.abs(out[1] - Math.pow(200 / 255, 0.7)) < 1e-6);
  VU.vumrLevelsAt({ channels: 3, rateHz: 30, frames: 0, samples: new Uint8Array(0) }, 1, out, 2);
  assert.deepEqual(Array.from(out), [0, 0]);
});

test('channel numbers thin out as the desktop meter\'s do (§8 77)', () => {
  assert.equal(VU.vuLabelStep(4, 300), 1);
  assert.equal(VU.vuLabelStep(32, 258), 4);      // Track Info's cover
  assert.equal(VU.vuLabelStep(32, 1500), 1);     // the player bar
  assert.ok(VU.vuLabelStep(64, 2000) >= 8);
  const app = read('js/app.js');
  assert.match(app, /const labelStep = vuLabelStep\(_vuChannelCount, hostWidth\);/);
});

test('which tracks get the per-voice meter (the needles get the rest)', () => {
  assert.equal(VU.vuEligible({ format: 'ProTracker' }), true);
  assert.equal(VU.vuEligible({ format: 'SID' }), true);
  assert.equal(VU.vuEligible({ format: 'SNDH' }), true);
  assert.equal(VU.vuEligible({ format: 'MP3' }), false);
  assert.equal(VU.vuEligible({ format: 'FLAC' }), false);
  assert.equal(VU.vuEligible(null), false);
  assert.equal(VU.vuKeyOf({ id: 'a', subsong: 3 }), 'a:3');
  assert.equal(VU.vuKeyOf({ id: 'a' }), 'a:0');
});

test('FFT columns average equal slices of the byte spectrum', () => {
  const bins = new Uint8Array(128).fill(0);
  bins.fill(255, 0, 4);
  const out = new Float32Array(32);
  VU.fftColumnLevels(bins, 32, out);
  assert.equal(out[0], 1);
  assert.equal(out[1], 0);
});

test('band ballistics: bars rise at once and fall 1.5 / s, peaks hold then fall', () => {
  const st = BANDS.createBandState(2);
  st.target[0] = 0.8;
  assert.equal(BANDS.stepBands(st, 0.1, 1000), true);
  assert.equal(st.level[0], st.target[0]);
  st.target[0] = 0;
  BANDS.stepBands(st, 0.1, 1100);
  assert.ok(Math.abs(st.level[0] - (st.target[0] + 0.8 - 0.15)) < 1e-6);
  assert.ok(Math.abs(st.peak[0] - 0.8) < 1e-6, 'the peak holds');
  for (let t = 1200; t < 4000; t += 100) BANDS.stepBands(st, 0.1, t);
  assert.equal(BANDS.stepBands(st, 0.1, 4000), false, 'everything back on the floor');
});

test('the phosphor trace keeps at most `keep` traces and forgets them on reset', () => {
  const calls = { stroke: 0 };
  const g = new Proxy({}, { get: (_, k) => (k === 'stroke' ? () => { calls.stroke++; } : () => {}), set: () => true });
  const ph = createPhosphor({ keep: 4, minAlpha: 0.03 });
  const T = { trace: '#fff', traceGlow: '#fff', traceCore: '#fff', persist: 0.99, level: 1 };
  const buf = new Uint8Array(64).fill(128);
  for (let i = 0; i < 10; i++) ph.draw(g, 100, 50, buf, T, 1, i);
  assert.equal(ph.traces, 4);
  ph.reset();
  assert.equal(ph.traces, 0);
});

test('the phone shell links mobile-visuals.css once, after mobile.css', () => {
  const html = read('mobile.html').replace(/<!--[\s\S]*?-->/g, '');
  const links = html.match(/\/assets\/css\/mobile-visuals\.css\?v=\d+/g) || [];
  assert.equal(links.length, 1);
  assert.ok(html.indexOf('/assets/css/mobile-visuals.css') > html.indexOf('/assets/css/mobile.css'));
});

test('Now Playing mounts the visuals; both shells read the shared modules', () => {
  const np = read('js/mobile/views/nowplaying.js');
  assert.match(np, /import \{ mountNpVisuals \} from '\.\/np_visuals\.js';/);
  assert.match(np, /visuals = mountNpVisuals\(\{ root, np, ctx \}\);/);
  for (const id of ['m-npm', 'm-npm-glass', 'm-npm-leds', 'm-npm-cvs', 'm-npm-note', 'm-npm-off', 'm-npm-peak', 'm-npm-count', 'm-npw']) {
    assert.ok(np.includes(`id="${id}"`), `nowplaying.js has #${id}`);
  }
  const v = read('js/mobile/views/np_visuals.js');
  assert.match(v, /from '\.\.\/\.\.\/vu\/vumr\.js';/);
  assert.match(v, /from '\.\.\/\.\.\/vu\/needles\.js';/);
  assert.match(v, /from '\.\.\/\.\.\/viz\/bands\.js';/);
  assert.match(v, /from '\.\.\/\.\.\/viz\/phosphor\.js';/);
  // the phone is Retro only: nothing keys on the Style
  assert.doesNotMatch(v, /getUiStyle|ui_style/);
  assert.match(read('js/app.js'), /from '\.\/vu\/vumr\.js';/);
  assert.match(read('js/player/vu_needles.js'), /from '\.\.\/vu\/needles\.js';/);
  assert.match(read('js/visualizer.js'), /import \{ createPhosphor \} from '\.\/viz\/phosphor\.js';/);
});

test('one loop, gated: view, visibility, on screen, visualizations, reduced motion; taps released', () => {
  const v = read('js/mobile/views/np_visuals.js');
  assert.equal((v.match(/requestAnimationFrame\(_frame\)/g) || []).length, 3, 'only _frame schedules frames');
  assert.match(v, /function _shown\(\) \{\s+return viz && _active\(\) && !document\.hidden && onScreen;\s+\}/);
  assert.match(v, /function _wanted\(\) \{\s+return _shown\(\) && !reducedMotion\(\);\s+\}/);
  assert.match(v, /if \(taps && Player\.releaseVuTaps\) Player\.releaseVuTaps\(\);/);
  assert.match(v, /const DPR_MAX = 2;/);
  assert.match(v, /const FRAME_MS = 33;/);
});

test('on a phone the mini player steps aside on Now Playing; the deck\'s cast key stands in for its one', () => {
  const css = read('css/mobile.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const phone = /@media \(max-width: 427\.98px\), \(max-height: 427\.98px\) \{([^@]*?)\n\}/g;
  const blocks = [...css.matchAll(phone)].map(m => m[1]).join('\n');
  assert.match(blocks, /:root:has\(#m-view-nowplaying\.active\) \{ --mini-h: 0px; \}/);
  assert.match(blocks, /#m-app:has\(#m-view-nowplaying\.active\) > #m-miniplayer \{ display: none; \}/);
  assert.match(blocks, /#m-app:has/);
  // the deck's cast key shows there too — only where :has() works (else the mini player stays)
  assert.match(css, /@supports selector\(:has\(a\)\) \{\s*@media \(max-width: 427\.98px\), \(max-height: 427\.98px\) \{\s*#m-np-cast \{ display: flex; \}/);
  assert.match(css, /#m-np-cast \{ display: none; \}/);
  // the phone CSS no longer keys on the Style (the page is always Retro hi-fi)
  assert.doesNotMatch(css, /data-ui-style/);
  assert.doesNotMatch(read('css/mobile-visuals.css').replace(/\/\*[\s\S]*?\*\//g, ''), /data-ui-style/);
  const np = read('js/mobile/views/nowplaying.js');
  assert.match(np, /id="m-np-cast" aria-label="Cast to device \(Beta\)" aria-haspopup="dialog" aria-expanded="false"/);
  assert.match(np, /castBtn\.addEventListener\('click', \(\) => miniCast\.click\(\)\);/);
  assert.match(np, /attributeFilter: \['class', 'aria-expanded', 'title'\]/);
});

test('the deck\'s lamps: the "off" rule never outranks the "lit" ones (no id in it, no :is())', () => {
  const css = read('css/mobile.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const off = css.match(/([^{}]*\.m-np-btn\.primary::before[^{}]*)\{[^}]*background: var\(--np-lamp-off\)/);
  assert.ok(off, 'the off-lamp rule not found');
  assert.doesNotMatch(off[1], /#|:is\(/);
  const lit = css.match(/([^{}]*\.m-np-btn\[aria-pressed="true"\]::before[^{}]*)\{[^}]*background: var\(--np-amber\)/);
  assert.ok(lit, 'the lit-lamp rule not found');
  assert.match(lit[1], /\.m-np\[data-state="playing"\] \.m-np-btn\.primary::before/);
  assert.doesNotMatch(lit[1], /#/);
});

test('Now Playing draws its transport glyphs as text (U+FE0E), never as emoji', () => {
  const np = read('js/mobile/views/nowplaying.js').replace(/^\s*\/\/.*$/gm, '');
  // every ⏮ ⏭ ⏸ ▶ in the markup or a string is followed by the text selector
  const bare = np.match(/[\u23EE\u23ED\u23F8\u25B6](?!\uFE0E|&#xFE0E;)/g) || [];
  assert.deepEqual(bare, []);
  assert.match(np, /const PLAY_GLYPH = '\\u25B6\\uFE0E', PAUSE_GLYPH = '\\u23F8\\uFE0E';/);
  assert.match(np, /playBtn\.textContent = playing \? PAUSE_GLYPH : PLAY_GLYPH;/);
});

test('the meter legends are keys (a tap picks that display) and a change is said once', () => {
  const np = read('js/mobile/views/nowplaying.js');
  for (const m of ['meter', 'spectrum', 'scope']) {
    assert.match(np, new RegExp(`<button type="button" class="m-npm-mode" data-mode="${m}" aria-pressed="false">`));
  }
  assert.match(np, /id="m-npm-glass" aria-label="Meter display" aria-describedby="m-npm-desc"/);
  const v = read('js/mobile/views/np_visuals.js');
  // the glass keeps its name; what it shows is its description
  assert.doesNotMatch(v, /glass\.setAttribute\('aria-label'/);
  assert.match(v, /el\.setAttribute\('aria-pressed', on \? 'true' : 'false'\);/);
  // a legend's own pick is said by its pressed state, not the status region too
  assert.match(v, /setMode\(m === mode \? MODES\[\(MODES\.indexOf\(mode\) \+ 1\) % MODES\.length\] : m, m === mode\);/);
  const css = read('css/mobile-visuals.css');
  assert.match(css, /#m-view-nowplaying \.m-npm-mode \{[^}]*min-width: 44px;[^}]*height: 44px;/);
});
