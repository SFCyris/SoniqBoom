// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Desktop toasts must leave the player bar's transport (prev / play / next)
// clickable: the host docks above the bar (the REAL _dockToastHost and
// _barTakesClicks from utils.js, run against a fake bar), and a plain toast
// lets clicks through while an offer toast (Toast.action) still takes them.
// Run: node --test tests/js/toast_dock.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/utils.js'), 'utf8');
const APP = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/app.js'), 'utf8');
const fn = (name, src = SRC) => {
  const m = src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}\\n`));
  assert.ok(m, `${name} not found`);
  return m[0];
};

const BAR = { top: 748, bottom: 900, left: 0, width: 1440, height: 152 };   // 1440×900, bar flush with the bottom

// innerHeight 900.  ``barRect``: the bar's rect (null: no bar).  ``promptRect``:
// the "new version" banner's (null: not up).  ``atCentre``: what a click at the
// bar's centre hits, topmost first ('bar' = the bar itself).
function dock(barRect, { innerHeight = 900, observer = true, promptRect = null, atCentre = ['bar'],
                         hitTest = true } = {}) {
  const offer = { offer: true };
  const host = { style: { bottom: '24px' }, contains: (el) => el === offer };
  const bar = barRect && { getBoundingClientRect: () => barRect, contains: (el) => el === bar };
  const els = () => ({ 'player-bar': bar, 'sb-update-prompt': promptRect && { getBoundingClientRect: () => promptRect } });
  const hits = [];
  const doc = { getElementById: (id) => els()[id] || null };
  if (hitTest) {
    doc.elementsFromPoint = (x, y) => {
      hits.push([x, y]);
      return atCentre.map((e) => (e === 'bar' ? bar : e === 'offer' ? offer : { other: e }));
    };
  }
  const observed = [];
  const RO = observer ? class { constructor(cb) { this.cb = cb; } observe(el, opts) { observed.push([this, el, opts]); } } : undefined;
  const run = new Function('document', 'window', 'ResizeObserver', '_toastHost', `
    let _toastDockObs = null;
    ${fn('_dockToastHost')}
    ${fn('_barTakesClicks')}
    _dockToastHost();
    return { again: _dockToastHost };`);
  const api = run(doc, { innerHeight }, RO, host);
  return {
    host, observed, hits, api,
    setRect: (r) => { barRect = r; },
    setPrompt: (r) => { promptRect = r; },
    setAtCentre: (a) => { atCentre = a; },
  };
}

test('the host is docked when it is made, and again for every toast', () => {
  const src = fn('_ensureToastHost');
  assert.match(src, /\n\s+_dockToastHost\(\);\n\s+return _toastHost;/);
  assert.match(src, /if \(_toastHost\) \{ _dockToastHost\(\); return _toastHost; \}/);
});

test('the toast host docks above a desktop player bar', () => {
  // 152 px bar flush with the bottom → 12 px clear of its top edge.
  const d = dock(BAR);
  assert.equal(d.host.style.bottom, '164px');
  assert.deepEqual(d.hits, [[720, 824]], 'the bar is hit-tested at its centre');
});

test('a wrapped (≤900 px) or safe-area-grown bar is measured, not assumed', () => {
  const { host } = dock({ top: 690.4, bottom: 900, left: 0, width: 820, height: 209.6 });
  assert.equal(host.style.bottom, '222px');
});

test('without a visible bar the toast keeps its 24 px slot', () => {
  assert.equal(dock({ top: 0, bottom: 0, left: 0, width: 0, height: 0 }).host.style.bottom, '24px');   // display:none
  const phone = dock(null);                                                                             // phone shell: no #player-bar
  assert.equal(phone.host.style.bottom, '24px');
  assert.equal(phone.observed.length, 0);
});

test('with Settings or a dialog over the bar, toasts stay low (off the dialog)', () => {
  const d = dock(BAR, { atCentre: ['admin-overlay', 'bar'] });
  assert.equal(d.host.style.bottom, '24px');
  d.setAtCentre(['bar']);                        // dialog closed: the next toast docks above the bar again
  d.api.again();
  assert.equal(d.host.style.bottom, '164px');
});

test('an offer toast over the bar does not count as covering it', () => {
  assert.equal(dock(BAR, { atCentre: ['offer', 'bar'] }).host.style.bottom, '164px');
});

test('without elementsFromPoint the bar is taken as clickable', () => {
  assert.equal(dock(BAR, { hitTest: false }).host.style.bottom, '164px');
});

test('the host follows the bar when it resizes (border box: the safe-area inset is padding)', () => {
  const d = dock(BAR);
  assert.equal(d.observed.length, 1, 'the bar is not observed');
  assert.deepEqual(d.observed[0][2], { box: 'border-box' });
  d.setRect({ top: 690, bottom: 900, left: 0, width: 1440, height: 210 });
  d.observed[0][0].cb();                         // the bar wrapped
  assert.equal(d.host.style.bottom, '222px');
  d.api.again();                                 // a later call does not observe twice
  assert.equal(d.observed.length, 1);
});

test('without ResizeObserver the host is still docked', () => {
  const d = dock(BAR, { observer: false });
  assert.equal(d.host.style.bottom, '164px');
  assert.equal(d.observed.length, 0);
});

test('while the "new version" banner is up, toasts go above it', () => {
  const d = dock(BAR, { promptRect: { top: 693, bottom: 732, height: 39 } });
  assert.equal(d.host.style.bottom, '219px');
  d.setPrompt(null);                             // dismissed: re-docked (Toast.redock / the next toast)
  d.api.again();
  assert.equal(d.host.style.bottom, '164px');
  // A banner lower than the bar's top (≤600px: --player-h is auto, so its calc()
  // bottom is dropped) never pulls the toasts down onto the bar.
  d.setPrompt({ top: 870, bottom: 909, height: 39 });
  d.api.again();
  assert.equal(d.host.style.bottom, '164px');
});

test('the app names its banner and re-docks the toasts as it comes and goes', () => {
  const id = (fn('_dockToastHost').match(/getElementById\('(sb-[\w-]+)'\)/) || [])[1];
  assert.ok(id, 'banner id not found in _dockToastHost');
  const prompt = fn('_showUpdatePrompt', APP);
  assert.match(prompt, new RegExp(`\\.id = '${id}'`));
  assert.match(prompt, /appendChild\(bar\);\n\s+Toast\.redock\?\.\(\);/, 'not re-docked when the banner appears');
  assert.match(prompt, /bar\.remove\(\);[^\n]*Toast\.redock\?\.\(\);/, 'not re-docked when the banner is dismissed');
  assert.match(SRC, /\n\s+redock: \(\) => _dockToastHost\(\),\n/, 'Toast.redock missing');
});

test('a plain toast lets clicks through; an offer toast takes them', () => {
  const plain = fn('_emitToast').match(/el\.style\.cssText = \(([\s\S]*?)\n {2}\);/);
  const offer = fn('_emitActionToast').match(/el\.style\.cssText = \(([\s\S]*?)\n {2}\);/);
  assert.ok(plain && offer, 'toast styles not found');
  assert.match(plain[1], /pointer-events:none/);
  assert.match(offer[1], /pointer-events:auto/);
});
