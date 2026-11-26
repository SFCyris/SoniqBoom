// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A finger on the phone's Now Playing seek slot
// (soniqboom/frontend/js/mobile/seek_gesture.js), driven by fake pointer
// events: a tap seeks to the spot tapped, a drag previews and seeks where it
// is lifted, every other way a gesture ends (pointercancel, a lost capture,
// the window's blur) ends it WITHOUT a seek — and nothing is ever left
// "scrubbing" (defect 5 of the v2.2.0 review: a tap froze the
// clock and the slider, because only the range's 'change' cleared the lock).
// Run: node --test tests/js/m_seek_gesture.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pctAtX, attachSeekGesture } from '../../soniqboom/frontend/js/mobile/seek_gesture.js';

class Target {
  constructor() { this.l = {}; }
  addEventListener(t, f) { (this.l[t] ||= []).push(f); }
  removeEventListener(t, f) { this.l[t] = (this.l[t] || []).filter((x) => x !== f); }
  fire(t, e = {}) { (this.l[t] || []).slice().forEach((f) => f({ type: t, ...e })); }
}
function surface(rect = { left: 20, width: 300 }) {
  const s = new Target();
  s.captured = new Set();
  s.getBoundingClientRect = () => rect;
  s.setPointerCapture = (id) => s.captured.add(id);
  s.hasPointerCapture = (id) => s.captured.has(id);
  s.releasePointerCapture = (id) => {
    s.captured.delete(id);
    s.fire('lostpointercapture', { pointerId: id });      // (the browser tells after a release)
  };
  return s;
}
function wire(opts = {}) {
  const s = surface(opts.rect);
  const win = new Target();
  const log = [];
  const g = attachSeekGesture(s, {
    enabled: opts.enabled || (() => true),
    preview: (p) => log.push(['preview', Math.round(p * 10) / 10]),
    commit: (p) => log.push(['commit', Math.round(p * 10) / 10]),
    cancel: () => log.push(['cancel']),
    win,
  });
  return { s, win, g, log };
}
const ev = (x, id = 1, more = {}) => ({ clientX: x, pointerId: id, pointerType: 'touch', button: 0, ...more });

test('x anywhere in the rectangle is the place: 0…100, clamped', () => {
  const r = { left: 20, width: 300 };
  assert.equal(pctAtX(20, r), 0);
  assert.equal(pctAtX(170, r), 50);
  assert.equal(pctAtX(320, r), 100);
  assert.equal(pctAtX(-50, r), 0);
  assert.equal(pctAtX(900, r), 100);
  assert.equal(pctAtX(100, { left: 0, width: 0 }), 0, 'no width: no place');
  assert.equal(pctAtX(Number.NaN, r), 0);
});

test('a tap without movement seeks to the spot tapped, and ends the gesture', () => {
  const { s, g, log } = wire();
  s.fire('pointerdown', ev(95));               // 25 %
  assert.equal(g.active, true);
  s.fire('pointerup', ev(95));
  assert.deepEqual(log, [['preview', 25], ['commit', 25]]);
  assert.equal(g.active, false, 'nothing left scrubbing');
  assert.equal(s.captured.size, 0, 'the capture is released');
});

test('a drag previews where the finger is and seeks where it is lifted', () => {
  const { s, log } = wire();
  s.fire('pointerdown', ev(20));
  s.fire('pointermove', ev(110));
  s.fire('pointermove', ev(245));
  s.fire('pointerup', ev(260));
  assert.deepEqual(log, [['preview', 0], ['preview', 30], ['preview', 75], ['commit', 80]]);
});

test('pointercancel, a lost capture without a lift and the window blur end it without a seek', () => {
  for (const how of ['pointercancel', 'lostpointercapture', 'blur']) {
    const { s, win, g, log } = wire();
    s.fire('pointerdown', ev(170));
    s.fire('pointermove', ev(200));
    if (how === 'blur') win.fire('blur'); else s.fire(how, { pointerId: 1 });
    assert.equal(g.active, false, how);
    assert.deepEqual(log.map((x) => x[0]), ['preview', 'preview', 'cancel'], how);
    s.fire('pointerup', ev(200));              // a late lift is no news
    assert.equal(log.length, 3, how);
  }
});

test('a second finger, another pointer\'s events and a right click change nothing', () => {
  const { s, log } = wire();
  s.fire('pointerdown', ev(50, 1, { pointerType: 'mouse', button: 2 }));
  assert.deepEqual(log, []);
  s.fire('pointerdown', ev(50, 1));
  s.fire('pointerdown', ev(300, 2));
  s.fire('pointermove', ev(300, 2));
  s.fire('pointerup', ev(300, 2));
  s.fire('pointercancel', { pointerId: 2 });
  assert.deepEqual(log, [['preview', 10]]);
  s.fire('pointerup', ev(50, 1));
  assert.deepEqual(log.at(-1), ['commit', 10]);
});

test('a gesture whose end was never told (capture gone) is ended by the next touch', () => {
  const { s, g, log } = wire();
  s.fire('pointerdown', ev(95, 1));
  s.captured.clear();                          // the capture went without any event
  s.fire('pointerdown', ev(245, 2));
  assert.deepEqual(log, [['preview', 25], ['cancel'], ['preview', 75]]);
  s.fire('pointerup', ev(245, 2));
  assert.deepEqual(log.at(-1), ['commit', 75]);
  assert.equal(g.active, false);
});

test('disabled (a station: no position) — no gesture starts', () => {
  const { s, g, log } = wire({ enabled: () => false });
  s.fire('pointerdown', ev(170));
  s.fire('pointerup', ev(170));
  assert.equal(g.active, false);
  assert.deepEqual(log, []);
});

test('detach() ends a gesture under way and listens no more', () => {
  const { s, g, log } = wire();
  s.fire('pointerdown', ev(170));
  g.detach();
  assert.deepEqual(log.map((x) => x[0]), ['preview', 'cancel']);
  s.fire('pointerdown', ev(170));
  assert.equal(log.length, 2);
});

test('Now Playing seeks through the gesture; no pointer handler on the range is left', () => {
  const NP = readFileSync(new URL('../../soniqboom/frontend/js/mobile/views/nowplaying.js', import.meta.url), 'utf8');
  assert.match(NP, /<div class="m-np-seek" id="m-np-seek" aria-hidden="true"><\/div>/);
  assert.match(NP, /attachSeekGesture\(seekEl, \{/);
  assert.doesNotMatch(NP, /scrub\.addEventListener\('pointer/, 'the old pointerdown lock is gone');
  // Every place that sets the lock has a way out: the gesture's commit / cancel clear it.
  assert.match(NP, /commit: seekTo,/);
  assert.match(NP, /function seekTo\(pct\) \{\s*_scrubbing = false;/);
  assert.match(NP, /cancel: \(\) => \{ _scrubbing = false; showPlayback\(\); \}/);
  const CSS = readFileSync(new URL('../../soniqboom/frontend/css/mobile.css', import.meta.url), 'utf8');
  const rule = CSS.slice(CSS.indexOf('.m-np-seek {'), CSS.indexOf('}', CSS.indexOf('.m-np-seek {')));
  assert.match(rule, /grid-column: 2; grid-row: 1;/, 'the same cell as the input and the waveform');
  assert.match(rule, /height: 44px;/);
  assert.match(rule, /touch-action: pan-y;/);
});
