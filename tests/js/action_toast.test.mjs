// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The offer toast (Toast.action — "Resume the queue from …?"): the REAL
// _emitActionToast / _dismissToast from utils.js, run against a small fake DOM
// with mocked timers.  Its countdown waits while the pointer is over it or
// focus is in it, Escape closes it, and closing it hands focus back.
// Run: node --test tests/js/action_toast.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/utils.js'), 'utf8');
const fn = (name) => {
  const m = SRC.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}\\n`));
  assert.ok(m, `${name} not found in utils.js`);
  return m[0];
};

// ── A fake DOM: just what the two functions touch ───────────────────────────
function makeDom() {
  const doc = { activeElement: null, byId: {} };
  const bubble = (node, type, init) => {
    const ev = { type, target: node, stopPropagation() {}, ...init };
    for (let x = node; x; x = x.parent) (x.listeners[type] || []).forEach((f) => f(ev));
  };
  class El {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
      this.style = {}; this.listeners = {}; this.attrs = {}; this.className = ''; this.textContent = '';
    }
    append(...ns) { ns.forEach((n) => this.appendChild(n)); }
    appendChild(n) { n.remove(); n.parent = this; this.children.push(n); return n; }
    remove() {
      if (!this.parent) return;
      if (this.contains(doc.activeElement)) doc.activeElement = doc.body;   // as a browser does
      this.parent.children = this.parent.children.filter((c) => c !== this);
      this.parent = null;
    }
    contains(n) { for (let x = n; x; x = x.parent) if (x === this) return true; return false; }
    get isConnected() { return doc.body.contains(this); }
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    focus() {
      const prev = doc.activeElement;
      if (prev === this) return;
      doc.activeElement = this;
      if (prev && prev !== doc.body) bubble(prev, 'focusout', { relatedTarget: this });
      bubble(this, 'focusin', { relatedTarget: prev === doc.body ? null : prev });
    }
    blur() {
      if (doc.activeElement !== this) return;
      doc.activeElement = doc.body;
      bubble(this, 'focusout', { relatedTarget: null });
    }
    click() { bubble(this, 'click', {}); }
    fire(type, init = {}) { (this.listeners[type] || []).forEach((f) => f({ type, target: this, stopPropagation() {}, ...init })); }
  }
  doc.body = new El('body');
  doc.activeElement = doc.body;
  doc.createElement = (t) => new El(t);
  doc.querySelector = (sel) => sel.split(',').map((s) => doc.byId[s.trim().replace(/^#/, '')]).find(Boolean) || null;
  const add = (tag, id) => { const e = new El(tag); doc.byId[id] = e; doc.body.appendChild(e); return e; };
  const host = add('div', 'toast-host');
  host._polite = host.appendChild(new El('div'));
  host._alert = host.appendChild(new El('div'));
  return { doc, add, host, El };
}

function load(dom) {
  const make = new Function('document', '_ensureToastHost', 'requestAnimationFrame', `
    let _toastSeq = 0;
    const _recent = new Map();
    ${fn('_dismissToast')}
    ${fn('_emitActionToast')}
    return { show: _emitActionToast, dismiss: _dismissToast };`);
  return make(dom.doc, () => dom.host, (f) => f());
}

function setup(t) {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const dom = makeDom();
  const search = dom.add('input', 'search-input');
  const play = dom.add('button', 'btn-play');
  const api = load(dom);
  const ran = [];
  const offer = () => {
    const el = api.show('Resume the queue from DSub?', 'Resume', () => ran.push('resume'));
    const [, act, close] = el.children;
    return { el, act, close };
  };
  const leaving = (el) => !!el._sbLeaving;
  return { dom, api, search, play, ran, offer, leaving, tick: (ms) => t.mock.timers.tick(ms) };
}

test('left alone, the offer closes after its 15 s', (t) => {
  const { offer, leaving, tick } = setup(t);
  const { el } = offer();
  tick(14999);
  assert.equal(leaving(el), false);
  tick(1);
  assert.equal(leaving(el), true);
  tick(220);
  assert.equal(el.isConnected, false);
});

test('the countdown waits while the pointer is over it, then goes on with what was left', (t) => {
  const { offer, leaving, tick } = setup(t);
  const { el } = offer();
  tick(5000);
  el.fire('mouseenter');
  tick(60000);
  assert.equal(leaving(el), false, 'a hovered offer stays');
  el.fire('mouseleave');
  tick(9999);
  assert.equal(leaving(el), false);
  tick(1);
  assert.equal(leaving(el), true);
});

test('hovered just before it would close, it stays at least 3 s after the pointer leaves', (t) => {
  const { offer, leaving, tick } = setup(t);
  const { el } = offer();
  tick(14900);
  el.fire('mouseenter');
  tick(10000);
  el.fire('mouseleave');
  tick(2999);
  assert.equal(leaving(el), false);
  tick(1);
  assert.equal(leaving(el), true);
});

test('keyboard focus in it keeps it; Escape closes it and focus goes back where it was', (t) => {
  const { dom, offer, leaving, tick, search, ran } = setup(t);
  search.focus();
  const { el, act, close } = offer();
  tick(3000);
  act.focus();                         // Tab into the offer
  tick(60000);
  close.focus();                       // between its own buttons: still paused
  tick(60000);
  assert.equal(leaving(el), false, 'a focused offer stays');
  el.fire('keydown', { key: 'Escape' });
  assert.equal(leaving(el), true);
  assert.equal(dom.doc.activeElement, search, 'focus back on the search box');
  assert.deepEqual(ran, [], 'Escape does not resume');
});

test('focus that came from elsewhere goes back there; a vanished origin falls back to Play', (t) => {
  const { dom, offer, tick, search, play } = setup(t);
  const other = dom.add('button', 'btn-other');
  const { act } = offer();
  other.focus();
  act.focus();                         // entered from #btn-other
  tick(1000);
  act.click();                         // Resume: closes, then acts
  assert.equal(dom.doc.activeElement, other);

  const b = offer();
  const gone = dom.add('button', 'btn-gone');
  gone.focus();
  b.act.focus();
  gone.remove();
  b.el.fire('keydown', { key: 'Escape' });
  assert.equal(dom.doc.activeElement, play, 'origin gone: focus lands on Play');
  assert.notEqual(dom.doc.activeElement, search);
});

test('focus leaving the offer restarts the countdown; the action runs once and closes it', (t) => {
  const { dom, offer, leaving, tick, ran } = setup(t);
  const out = dom.add('button', 'btn-out');
  const { el, act } = offer();
  act.focus();
  tick(30000);
  out.focus();                         // Tab out of it
  tick(14999);
  assert.equal(leaving(el), false);
  tick(1);
  assert.equal(leaving(el), true);
  assert.equal(dom.doc.activeElement, out, 'closing without focus inside leaves focus alone');

  const b = offer();
  b.act.click();
  b.act.click();
  assert.deepEqual(ran, ['resume'], 'the second click lands on a closing toast');
  assert.equal(leaving(b.el), true);
});

test('Toast.dismiss (an offer that no longer applies) stops its timer and hands focus back', (t) => {
  const { dom, api, offer, leaving, tick, search } = setup(t);
  search.focus();
  const { el, close } = offer();
  close.focus();
  api.dismiss(el);
  assert.equal(leaving(el), true);
  assert.equal(dom.doc.activeElement, search);
  tick(60000);                          // no stray timer fires on the closed toast
  assert.equal(el.isConnected, false);
});
