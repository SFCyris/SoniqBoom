// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Track Info in the shared Modern / Retro hi-fi style (css/trackinfo-styles.css):
// the stylesheet is linked once after the other looks' sheets, the VFD strip's
// extras are in the markup and filled by trackinfo.js, and a tune row keeps
// the same words ("Tune N", the marks, the STIL title) with the number in its
// own span for Retro's lamp keys.
// Run: node --test tests/js/trackinfo_style.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');

test('the Track Info stylesheet is linked once, after the other looks', () => {
  const html = read('index.html');
  assert.equal((html.match(/\/assets\/css\/trackinfo-styles\.css\?v=\d+/g) || []).length, 1);
  for (const sheet of ['stations-styles.css', 'playlist-styles.css', 'player-styles.css', 'queue-styles.css']) {
    assert.ok(html.indexOf(sheet) < html.indexOf('trackinfo-styles.css'), `${sheet} first`);
  }
});

test('the VFD strip: its elements exist and trackinfo.js fills them', () => {
  const html = read('index.html');
  for (const id of ['ti-vfd-len', 'ti-vfd-specs', 'ti-vfd-chip-item', 'ti-vfd-chip',
                    'ti-vfd-renderer-item', 'ti-vfd-renderer']) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  // the chip and the length repeat rows a screen reader already has
  assert.match(html, /id="ti-vfd-len" class="ti-vfd-len" aria-hidden="true"/);
  assert.match(html, /id="ti-vfd-chip-item" aria-hidden="true"/);
  const ti = read('js/trackinfo.js');
  assert.match(ti, /_vfdLen\.textContent = track\.duration > 0 \? _fmt\(track\.duration\) : '';/);
  assert.match(ti, /_vfdSpec\('chip', m\);/);
  assert.equal((ti.match(/_vfdSpec\('renderer', data\.renderer\);/g) || []).length, 2);
  assert.match(ti, /_vfdSpec\('renderer', null\);/);
  // the server names it
  const py = readFileSync(resolve(HERE, '../../soniqboom/api/tracks.py'), 'utf8');
  assert.match(py, /"renderer": _renderer_key\(track_id, track\),/);
});

test('a tune row reads as before; Retro gets the number on its own', () => {
  const src = read('js/trackinfo.js');
  const fn = src.match(/\nfunction _subRowHtml\(tune\) \{[\s\S]*?\n\}\n/);
  assert.ok(fn, '_subRowHtml not found');
  const st = { count: 4, start: 1, def: 1, lengths: [192, 45, 90.5, 8], stilTitles: { 0: 'Main <Theme>' } };
  const make = new Function('st', `
    const _subState = st;
    const _subWire = (t) => t - 1;
    const _subIsShort = (t) => t === 4;
    const _subShortSecs = () => 10;
    const _subFmtLen = (s) => s > 0 ? Math.floor(s / 60) + ':' + String(Math.round(s % 60)).padStart(2, '0') : '';
    const _escHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    ${fn[0]}
    return _subRowHtml;`);
  const row = make(st);
  // the name's words as one run (the number's span joins "Tune "), then the length
  const name = (h) => h.match(/<span class="ti-sub-name">([\s\S]*?)<\/span><span class="ti-sub-len">([^<]*)/)
    .slice(1).map((x) => x.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim()).join(' | ');
  const one = row(1);
  assert.match(one, /<span class="ti-sub-word">Tune <\/span><span class="ti-sub-n">1<\/span>/);
  assert.match(one, /aria-label="Tune 1 \(default\): Main &lt;Theme&gt;"/);
  assert.equal(name(one), 'Tune 1 default · Main &lt;Theme&gt; | 3:12');
  const four = row(4);
  assert.equal(name(four), 'Tune 4 short | 0:08');
  assert.match(four, /class="ti-sub-row ti-sub-row--short"/);
});

test('the look follows the shared style; the playing tune stays in view', () => {
  const ti = read('js/trackinfo.js');
  assert.match(ti, /import \{ onUiStyleChange, getUiStyle \} from '\.\/ui_style\.js';/);
  assert.match(ti, /onUiStyleChange\(\(\) => \{ if \(isOpen\(\)\) _revealPlayingSub\(\); \}\);/);
  const css = read('css/trackinfo-styles.css');
  assert.match(css, /:root\[data-ui-style="retro"\] #ti-panel \.ti-sub-row\.playing \.ti-sub-eq \{/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});

test('STIL shows flush left, each line where the source breaks it', () => {
  const src = read('js/trackinfo.js');
  const fn = src.match(/\nfunction _stilText\(blob\) \{[\s\S]*?\n\}\n/);
  assert.ok(fn, '_stilText not found');
  const stil = new Function(`${fn[0]}; return _stilText;`)();
  const blob = 'COMMENT: "There is a story. I went down to their\r\n         office and worked." (RH)\r\n'
    + '(#1)\r\n  TITLE: BGM1 (0:00)\r\n ARTIST: Tamayo Kawamoto\r\n\r\n\t  Drums: X\rCOMMENT: a';
  assert.equal(stil(blob),
    'COMMENT: "There is a story. I went down to their\noffice and worked." (RH)\n'
    + '(#1)\nTITLE: BGM1 (0:00)\nARTIST: Tamayo Kawamoto\n\nDrums: X\nCOMMENT: a');
});

test('Track Info: no placeholder dashes under the cover, SID voices, line icons', () => {
  const ti = read('js/trackinfo.js');
  const html = read('index.html');
  // the audio facts: a part with no value and its separator are left out
  assert.match(html, /<span id="ti-bit-depth">—<\/span><span class="ti-stat-sep"> \/ <\/span><span id="ti-sample-rate">—<\/span>/);
  assert.match(html, /<span id="ti-channels">—<\/span><span class="ti-stat-sep"> · <\/span><span id="ti-bitrate">—<\/span>/);
  assert.match(ti, /_show\('ti-file-size',\s+track\.file_size,\s+_fmtSize\);\n  _syncStatRows\(isUnplayable\(track\)\);/);
  // a SID's channels are its chips, three voices each
  assert.match(ti, /function _sidVoices\(n\) \{ return n > 0 \? n \* 3 : null; \}/);
  assert.match(ti, /if \(chLabel\) chLabel\.textContent = isSid \? 'Voices' : 'Channels';/);
  // drawn icons, not emoji
  assert.doesNotMatch(ti, /✏️/);
  assert.doesNotMatch(html, /id="ti-sub-shuffle"[^>]*>&#128256;/);
  assert.match(html, /id="ti-sub-shuffle"[^>]*><svg class="ti-icon"[^>]*stroke="currentColor"/);
  assert.match(ti, /<h4 class="ti-section-hdr ti-about-hdr">About /);
});
