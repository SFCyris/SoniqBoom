// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The full-screen visualizer and the large now-playing art in the shared
// Modern / Retro hi-fi style (css/visualizer-styles.css): the stylesheet is
// linked once after the other looks' sheets, the spectrum analyser mode is in
// the V cycle, its band reading sums the right FFT bins, reduced motion
// freezes instead of hiding, and the art's turntable markup keeps the ids
// app.js fills.
// Run: node --test tests/js/visualizer_style.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');

test('the visualizer stylesheet is linked once, after the other looks', () => {
  const html = read('index.html');
  assert.equal((html.match(/\/assets\/css\/visualizer-styles\.css\?v=\d+/g) || []).length, 1);
  for (const sheet of ['stations-styles.css', 'playlist-styles.css', 'player-styles.css',
                       'queue-styles.css', 'trackinfo-styles.css', 'lyrics-styles.css']) {
    assert.ok(html.indexOf(sheet) < html.indexOf('visualizer-styles.css'), `${sheet} first`);
  }
});

test('the analyser is a mode of the V cycle and draws per look', () => {
  const src = read('js/visualizer.js');
  const modes = src.match(/const MODES = \[([\s\S]*?)\];/)[1].match(/'([a-z]+)'/g).map((s) => s.slice(1, -1));
  assert.deepEqual(modes, ['oscilloscope', 'crt', 'spectrogram', 'analyser',
                           'hyperspace', 'synthwave', 'globe', 'cosmic', 'lavalamp', 'raccoon']);
  assert.match(src, /case 'analyser':\s+_drawAnalyser\(analyser\);\s+break;/);
  assert.match(src, /if \(_look === 'retro'\) _drawPhosphorScope\(analyser\);\s+else\s+_drawOscilloscope\(analyser\);/);
  assert.match(src, /import \{ getUiStyle, onUiStyleChange \} from '\.\/ui_style\.js';/);
  assert.match(src, /import \{ cssAlpha \} from '\.\/eq\/engine\.js';/);
  assert.match(src, /onUiStyleChange\(_restyle\);/);
  // the canvas tells the CSS which mode it shows (the Retro graticule)
  assert.match(src, /canvas\.dataset\.visMode = mode;/);
  const css = read('css/visualizer-styles.css');
  assert.match(css, /:root\[data-ui-style="retro"\] #visualizer-canvas\[data-vis-mode="oscilloscope"\] \{/);
  for (const t of ['--vz-trace', '--vz-persist', '--vz-an-bar', '--vz-an-peak', '--vz-an-label', '--vz-an-lit', '--vz-an-off', '--vz-an-glow']) {
    assert.ok(src.includes(`'${t}'`), `visualizer.js reads ${t}`);
    assert.ok(css.includes(`${t}:`), `visualizer-styles.css sets ${t}`);
  }
});

// The band reading, run on a made-up spectrum: visualizer.js's _anBinsFor /
// _anRead (its cache + the AnalyserNode read) over viz/bands.js (shared with the
// phone's Now Playing).
const BANDS = await import('../../soniqboom/frontend/js/viz/bands.js');
function loadReader() {
  const src = read('js/visualizer.js');
  assert.match(src, /import \{ AN_THIRDS, AN_OCTAVES, bandBins, readBands, stepBands \} from '\.\/viz\/bands\.js';/);
  const consts = ['AN_FFT']
    .map((n) => src.match(new RegExp(`\\nconst ${n} = (\\[[\\s\\S]*?\\]|[^;\\n]+);`))[0]).join('');
  const fns = ['_anBinsFor', '_anRead']
    .map((n) => src.match(new RegExp(`\\nfunction ${n}\\([\\s\\S]*?\\n\\}\\n`))[0]).join('');
  return new Function('B', `const { AN_THIRDS, AN_OCTAVES, bandBins, readBands } = B;
    let _anBins = null, _anBinsKey = '', _anSpec = null;${consts}${fns}
    return { _anBinsFor, _anRead, AN_THIRDS, AN_OCTAVES, AN_FFT };`)(BANDS);
}
function fakeTap(rate, dbAt) {
  const n = 2048;
  return {
    frequencyBinCount: n,
    context: { sampleRate: rate },
    getFloatFrequencyData(out) { for (let i = 0; i < n; i++) out[i] = dbAt(i * rate / 4096); },
  };
}

test('every bin from 22 Hz up belongs to at most one band, in order', () => {
  const R = loadReader();
  for (const rate of [44100, 48000]) {
    for (const freqs of [R.AN_THIRDS, R.AN_OCTAVES]) {
      const bins = R._anBinsFor(freqs, rate);
      let last = 0;
      for (let i = 0; i < freqs.length; i++) {
        if (!bins[i * 3]) continue;                     // narrower than a bin: read at its centre
        assert.ok(bins[i * 3 + 1] > last, `band ${freqs[i]} starts after the previous one (${rate})`);
        assert.ok(bins[i * 3 + 2] >= bins[i * 3 + 1]);
        assert.ok(bins[i * 3 + 2] <= 2047);
        last = bins[i * 3 + 2];
      }
      // above ~250 Hz every third-octave band has bins of its own
      if (freqs === R.AN_THIRDS) for (let i = 0; i < freqs.length; i++) if (freqs[i] >= 250) assert.equal(bins[i * 3], 1, `${freqs[i]} Hz`);
    }
  }
});

test('a 1 kHz tone lights the 1 kHz band most; silence reads 0', () => {
  const R = loadReader();
  const out = new Float32Array(R.AN_THIRDS.length);
  R._anRead(fakeTap(48000, (f) => (Math.abs(f - 1000) < 12 ? -20 : -140)), R.AN_THIRDS, out);
  const top = out.indexOf(Math.max(...out));
  assert.equal(R.AN_THIRDS[top], 1000);
  assert.ok(out[top] > 0.9, `1 kHz at ${out[top]}`);
  assert.ok(out[top - 2] === 0 && out[top + 2] === 0, 'two thirds away stays dark');
  R._anRead(fakeTap(48000, () => -Infinity), R.AN_THIRDS, out);
  assert.ok(out.every((v) => v === 0), 'silence: every band 0, no NaN');
  // a bass tone below the bins' resolution still reads in its own band
  const oct = new Float32Array(R.AN_OCTAVES.length);
  R._anRead(fakeTap(48000, (f) => (f < 40 ? -30 : -140)), R.AN_OCTAVES, oct);
  assert.ok(oct[0] > 0 && oct[0] >= Math.max(...oct.slice(1)), `31.5 Hz octave ${oct[0]}`);
});

test('reduced motion takes a still frame; viz off hides', () => {
  const src = read('js/visualizer.js');
  const start = src.match(/\nfunction start\(\) \{[\s\S]*?\n\}\n/)[0];
  // viz off → hidden, before anything else
  assert.match(start, /if \(!vizGroupEnabled\('nowPlaying'\)\) \{\s+canvas\.style\.opacity = '0';/);
  // reduced motion → _freeze(), never the hidden state
  assert.match(start, /if \(_userPrefersStillVisualizer\(\)\) \{[\s\S]*?_freeze\(\);\s+return;\s+\}/);
  assert.doesNotMatch(start, /\|\| _userPrefersStillVisualizer\(\)/);
  const freeze = src.match(/\nfunction _freeze\(\) \{[\s\S]*?\n\}\n/)[0];
  assert.match(freeze, /setTimeout\(\(\) => \{[\s\S]*?_suspend\(\);[\s\S]*?canvas\.style\.opacity = '0\.9';[\s\S]*?\}, FREEZE_AFTER_MS\);/);
  // the frames drawn meanwhile never show: no fade while it runs
  assert.match(freeze, /canvas\.style\.transition = 'none';\s+canvas\.style\.opacity = '0';/);
  // nothing drawn (a hidden tab) → not frozen, taken again when the page shows
  assert.match(freeze, /if \(_drawCount === drawn\) \{ _frozenKey = null; _refreezeOnShow = true; return; \}/);
  // the analyser's tap is released whenever the loop stops
  assert.match(src, /function _suspend\(\) \{\s+if \(rafId\) \{ cancelAnimationFrame\(rafId\); rafId = null; \}\s+_anRelease\(\);\s+\}/);
});

test('the now-playing art: one markup for both looks, the ids app.js fills', () => {
  const html = read('index.html');
  const art = html.match(/<div id="now-playing-art" class="now-playing-art" hidden>[\s\S]*?\n {8}<\/div>\n/)[0];
  assert.match(art, /<div class="np-deck">\s+<div class="np-platter">\s+<div class="np-record">\s+<img id="now-playing-art-img" alt="Now Playing" decoding="async">/);
  assert.match(art, /<div class="np-tonearm" aria-hidden="true"><\/div>/);
  assert.match(art, /<div class="np-anns" aria-hidden="true">/);
  for (const id of ['np-title', 'np-artist', 'np-album']) assert.ok(art.includes(`id="${id}"`), id);
  const app = read('js/app.js');
  assert.match(app, /npArt\.dataset\.state = Player\.playing \? 'playing' : 'paused';/);
  assert.match(app, /npArt\.dataset\.motion = _nowPlayingVizOn\(\) \? 'on' : 'off';/);
  assert.match(app, /window\.addEventListener\('sb:viz-settings', _syncNpArtState\);/);
  const css = read('css/visualizer-styles.css');
  // Modern: the deck is only a wrapper (the cover + text as before)
  assert.match(css, /\.now-playing-art \.np-deck,\s+\.now-playing-art \.np-platter,\s+\.now-playing-art \.np-record \{ display: contents; \}/);
  // Retro: the record turns only while playing with motion welcome, never with reduced motion
  assert.match(css, /\.now-playing-art\[data-state="playing"\]\[data-motion="on"\] \.np-record \{\s+animation-play-state: running;/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{\s+:root\[data-ui-style="retro"\] \.now-playing-art \.np-record \{ animation: none; \}/);
});
