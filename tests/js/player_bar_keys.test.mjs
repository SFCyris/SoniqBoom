// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The player bar's transport (◄◄ ▶ ►► ◎) must never lie under the icon row.
// Above 1024 px the row may spill past its column into the free space beside
// the transport, but app.js fit() caps it (.keys-capped → --keys-room, scrolls)
// once its keys would come within 4 px — Cast's device pill and Retro's wider
// keys at ≈1101 px used to put them over the transport.  In radio mode the
// station names beside ◄◄ / ►► give way first (--radio-target-w, one width for
// both).  The REAL fit() / fitRadio() from app.js run against a small layout
// model; the CSS is checked in the source.  Also: toasts stay out of print.
// Run: node --test tests/js/player_bar_keys.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(resolve(HERE, '../../soniqboom/frontend', p), 'utf8');
const APP = read('js/app.js');
const PLAYER_CSS = read('css/player-styles.css').replace(/\/\*[\s\S]*?\*\//g, '');
const APP_CSS = read('css/app.css').replace(/\/\*[\s\S]*?\*\//g, '');

const SRC = (() => {
  const a = APP.indexOf('    const keysWidth = () => {');
  const b = APP.indexOf('    const fitObs = new ResizeObserver(fit);');
  assert.ok(a > 0 && b > a, 'fit() block not found in app.js');
  return APP.slice(a, b);
})();

const R = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left, height: bottom - top });

// A bar laid out like the desktop one at >1024 px, on one line (y 768–810):
// the centre column [cl, cr] holds the transport, centred: ``base`` px wide
// plus two station names of the current width in radio mode.  The keys are
// ``keys`` px wide, right-aligned in the column [colL, colR].
function layout({ cl = 374, cr = 727, colL = 743, colR = 1085, base = 214, keys = 414,
                  radio = false, wide = true, artR = 358, line = { col: 768, art: 768 } } = {}) {
  const vars = {};
  const state = { capped: false };
  const label = () => (radio ? parseFloat(vars['--radio-target-w'] ?? '120') : 0);
  const pill = () => {
    const w = base + 2 * label(), cx = (cl + cr) / 2;
    return R(cx - w / 2, 768, cx + w / 2, 810);
  };
  const center = { getBoundingClientRect: () => R(cl, 768, cr, 857) };
  const ctrls = { getBoundingClientRect: pill, parentElement: center };
  const bar = {
    classList: { contains: (c) => c === 'radio-mode' && radio },
    style: { setProperty: (k, v) => { vars[k] = v; }, removeProperty: (k) => { delete vars[k]; } },
  };
  const art = { getBoundingClientRect: () => R(16, line.art, artR, line.art + 150) };
  const prevT = { getBoundingClientRect: () => R(0, 0, label(), 12) };
  const col = { getBoundingClientRect: () => R(colL, line.col, colR, line.col + 90) };
  // the keys, where they lie uncapped (a capped row scrolls them, their span stays), and a hidden one
  const key = (l, r) => ({ getClientRects: () => [1], getBoundingClientRect: () => R(l, line.col, r, line.col + 38) });
  // (hidden last, like ⋯ above 1100 px — the row's last child)
  const children = [key(colR - keys, colR - keys + 38), key(colR - 38, colR),
                    { getClientRects: () => [], getBoundingClientRect: () => R(0, 0, 0, 0) }];
  const row = {
    children,
    getBoundingClientRect: () => (state.capped
      ? R(colR + 4 - Math.min(keys + 8, Math.max(colR - colL, parseFloat(vars['--keys-room']))), line.col, colR + 4, line.col + 44)
      : R(colR - keys, line.col, colR, line.col + 44)),
    classList: { toggle: (c, on) => { if (c === 'keys-capped') state.capped = !!on; } },
    style: { setProperty: (k, v) => { vars[k] = v; } },
  };
  const getComputedStyle = () => (state.capped ? { paddingLeft: '4px', paddingRight: '4px' } : { paddingLeft: '0px', paddingRight: '0px' });
  const fit = new Function('row', 'col', 'ctrls', 'bar', 'art', 'prevT', 'wide', 'getComputedStyle',
    `${SRC}; return fit;`)(row, col, ctrls, bar, art, prevT, { matches: wide }, getComputedStyle);
  return { fit, vars, state, pill };
}

test('keys that stay clear of the transport are left alone (not capped)', () => {
  // 1101 px Modern: transport 448–654 (base 206 here), keys reach 1085 − 414 = 671 ≥ 654 + 4
  const l = layout({ base: 206 });
  l.fit();
  assert.equal(l.state.capped, false);
  assert.equal(l.vars['--keys-room'], `${Math.floor(1085 + 4 - l.pill().right - 12)}px`);
});

test('keys 13 px from the transport are not capped (a 3 px scroll would only fade keys)', () => {
  // ≈ Retro 1200 px: transport 497–703 (base 206 centred in 406–794), keys reach 1184 − 468 = 716
  const l = layout({ cl: 406, cr: 794, colL: 810, colR: 1184, base: 206, keys: 468 });
  l.fit();
  assert.ok(1184 - 468 - l.pill().right >= 4);
  assert.equal(l.state.capped, false);
});

test('keys that would come within 4 px of the transport are capped 12 px short, and stay capped', () => {
  // Retro ≈1101 px: wider keys, 460 px → their left edge 625 < 654 + 4
  const l = layout({ base: 206, keys: 460 });
  l.fit();
  assert.equal(l.state.capped, true);
  const room = parseFloat(l.vars['--keys-room']);
  assert.ok(1085 + 4 - room >= l.pill().right + 12, 'the capped row ends 12 px short of the transport');
  for (let i = 0; i < 4; i++) {                  // capping changes the row's box: the decision must not flip
    l.fit();
    assert.equal(l.state.capped, true, `flipped on pass ${i + 2}`);
  }
});

test('a row capped earlier is released once its keys clear the transport again', () => {
  // keys reach 6 px short of the transport: fine uncapped — read from the keys'
  // own width, not the capped row's (its 4 px padding each side must not count)
  const l = layout({ base: 206, keys: 1085 - 654 - 6 });
  l.state.capped = true;
  l.fit();
  assert.equal(l.state.capped, false);
});

test('radio mode: both station names get one width, what is free beside the transport', () => {
  // 1200 px: centre column 406–794, keys reach 1184 − 414 = 770
  const l = layout({ cl: 406, cr: 794, colL: 810, colR: 1184, radio: true });
  l.fit();
  const w = parseFloat(l.vars['--radio-target-w']);
  const p = l.pill();
  assert.ok(w > 0 && w <= 120, `width ${w}`);
  assert.ok(p.right <= 770 - 12 + 1, `transport ends at ${p.right}, keys start at 770`);
  assert.equal(l.state.capped, false, 'the names gave way, the keys did not have to');
});

test('radio mode: 120 px where there is room; none under 40 px', () => {
  const roomy = layout({ cl: 452, cr: 1468, colL: 1484, colR: 1904, radio: true });   // 1920 px
  roomy.fit();
  assert.equal(roomy.vars['--radio-target-w'], '120px');
  const tight = layout({ cl: 342, cr: 658, colL: 674, colR: 984, radio: true });     // 1000 px
  tight.fit();
  assert.equal(tight.vars['--radio-target-w'], '0px');
  // ~20 px free a side: too little for a name — none rather than a 20 px sliver
  const sliver = layout({ cl: 373.5, cr: 651.5, colL: 668, colR: 1200, keys: 300, radio: true });
  sliver.fit();
  assert.equal(sliver.vars['--radio-target-w'], '0px');
});

test('radio mode: a flank or the keys on another line (≤600 px rows) do not count', () => {
  const l = layout({ cl: 12, cr: 488, colL: 12, colR: 488, artR: 488, radio: true, line: { col: 856, art: 600 } });
  l.fit();
  assert.ok(parseFloat(l.vars['--radio-target-w']) > 90, l.vars['--radio-target-w']);
});

test('radio mode off: the names width is dropped', () => {
  const l = layout({ radio: true });
  l.fit();
  assert.ok('--radio-target-w' in l.vars);
  const off = layout({ radio: false });
  off.vars['--radio-target-w'] = '50px';
  off.fit();
  assert.ok(!('--radio-target-w' in off.vars));
});

test('hidden keys do not count, nor does a transport that is not laid out', () => {
  const l = layout({ base: 206 });
  l.fit();
  assert.equal(l.state.capped, false, 'the hidden key (rect at 0) was counted');
  const gone = layout({ base: 0, radio: false });
  gone.fit();                                     // width 0: nothing measured, nothing set
  assert.deepEqual(gone.vars, {});
  assert.equal(gone.state.capped, false);
});

test('≤1024 px the row keeps to its column (player-styles.css): never capped here', () => {
  const l = layout({ base: 206, keys: 600, wide: false });
  l.fit();
  assert.equal(l.state.capped, false);
});

test('fit() follows the column, the transport, its column, the row, the bar and the keys themselves', () => {
  assert.match(APP, /const fitObs = new ResizeObserver\(fit\);\n\s+for \(const el of \[col, ctrls, ctrls && ctrls\.parentElement, row, bar, \.\.\.row\.children\]\) if \(el\) fitObs\.observe\(el\);/);
  assert.match(APP, /const col = row\.parentElement;/);
});

test('above 1024 px a capped row is held to --keys-room and scrolls', () => {
  const media = PLAYER_CSS.match(/@media \(min-width: 1025px\) \{([\s\S]*?)\n\}/);
  assert.ok(media, '≥1025 px block not found in player-styles.css');
  const rule = media[1].match(/#player-bar \.player-extra-controls\.keys-capped \{([^}]*)\}/);
  assert.ok(rule, '.keys-capped rule not found');
  for (const decl of [/width:\s*max-content/, /max-width:\s*max\(100%,\s*var\(--keys-room,\s*100%\)\)/,
    /overflow-x:\s*auto/, /justify-content:\s*flex-start/,
    /padding:\s*4px/, /margin:\s*-4px/]) {        // (--keys-room adds the −4 px margin back)
    assert.match(rule[1], decl);
  }
  assert.match(SRC, /row\.classList\.toggle\('keys-capped', wide\.matches && keysLeft < p\.right \+ 4\)/);
  assert.match(APP, /window\.matchMedia\('\(min-width: 1025px\)'\)/);
});

test('the station names take the measured width — one for both, 120 px until measured', () => {
  const rule = APP_CSS.match(/#player-bar\.radio-mode \.radio-target \{([^}]*)\}/);
  assert.ok(rule, 'radio-target rule not found');
  for (const p of ['flex: 0 0', 'min-width:', 'max-width:']) {
    assert.ok(rule[1].includes(`${p} var(--radio-target-w, 120px)`) || rule[1].includes(`${p}var(--radio-target-w, 120px)`),
      `${p} does not use var(--radio-target-w, 120px)`);
  }
});

test('toasts are left out of print', () => {
  const print = APP_CSS.match(/@media print \{([\s\S]*?)\n\}/);
  assert.ok(print, 'print block not found');
  const sels = [...print[1].matchAll(/([^{}]+)\{\s*display:\s*none !important;\s*\}/g)]
    .flatMap((m) => m[1].split(',').map((s) => s.trim()));
  assert.ok(sels.includes('#player-bar'), 'print hide list not found');
  assert.ok(sels.includes('.sb-toast-host'), `.sb-toast-host not hidden in print: ${sels.join(' | ')}`);
  assert.match(read('js/utils.js'), /className = 'sb-toast-host'/);
});
