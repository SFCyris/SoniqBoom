// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The phone page asks a cover again once the server says it has one
// (``art_ready``), a cover setting changed (``art_refresh``), or the socket
// was away.  The region ``[m-art:begin]`` of mobile/views/_common.js is run
// as is, against a small stand-in for the document: art slots are a
// placeholder plus the cover <img>, and record the URL they asked.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/mobile/views/_common.js'), 'utf8');
const m = SRC.match(/\/\/ \[m-art:begin\][^\n]*\n([\s\S]*?)\/\/ \[m-art:end\]/);
if (!m) throw new Error('m-art region not found');
const BODY = m[1].replace(/^export /gm, '');

class Img {
  constructor() { this.cls = new Set(); this.src = ''; this.gone = false; this.parent = null;
    this.classList = { add: (c) => this.cls.add(c), remove: (c) => this.cls.delete(c), contains: (c) => this.cls.has(c) }; }
  remove() { this.gone = true; if (this.parent) this.parent.imgs = this.parent.imgs.filter((i) => i !== this); }
}
function slot(img = null) {
  const art = { imgs: [],
    appendChild(i) { i.parent = art; art.imgs.push(i); },
    querySelector(sel) {
      if (sel === 'img') return art.imgs[0] || null;
      if (sel === 'img.loaded') return art.imgs.find((i) => i.cls.has('loaded')) || null;
      throw new Error(`unexpected selector ${sel}`);
    } };
  if (img) art.appendChild(img);
  return art;
}
// A list row showing ``id``: asked ``src``; ``shown`` — the cover came.
function row(id, src, shown) {
  const img = new Img();
  img.src = src;
  if (shown) img.cls.add('loaded');
  const art = slot(shown === null ? null : img);     // (null: the <img> left on "no cover")
  return { dataset: id ? { trackId: id } : {}, __artSrc: src, art,
    querySelector(sel) { assert.equal(sel, '.m-row-art'); return art; } };
}
function load(rows = []) {
  const listeners = [], sent = [], asked = [];
  const document = {
    addEventListener(type, fn) { listeners.push([type, fn]); },
    dispatchEvent(ev) { sent.push(ev); for (const [t, fn] of listeners) if (t === ev.type) fn(ev); },
    querySelectorAll(sel) {
      asked.push(sel);
      const one = /^\.m-row\[data-track-id="([^"]+)"\]$/.exec(sel);
      if (one) return rows.filter((r) => r.dataset.trackId === one[1]);
      assert.equal(sel, '.m-row[data-track-id]');
      return rows.filter((r) => r.dataset.trackId);
    },
  };
  class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
  const api = new Function('document', 'window', 'CSS', 'Image', 'CustomEvent',
    `${BODY}\nreturn { freshArt, noteArtChanged, noteArtMissed, askArt, reaskArt, _artAgain, _ART_AGAIN_MAX };`)(
    document, { CSS: null }, undefined, Img, CustomEvent);
  return { ...api, sent, asked, rows };
}
const SM = (id) => `/api/art/${id}/sm.jpg?fallback=none`;

test('a cover URL is asked as it is until that track\'s cover changes', () => {
  const p = load();
  assert.equal(p.freshArt(SM('a1')), SM('a1'));
  assert.equal(p.freshArt('/assets/covers/x.jpg'), '/assets/covers/x.jpg');
  assert.equal(p.freshArt(null), null);
  assert.equal(p.freshArt(''), '');
  p.noteArtChanged('a1', 1000);
  assert.equal(p.freshArt(SM('a1')), `${SM('a1')}&_t=1000`);
  assert.equal(p.freshArt('/api/art/a1'), '/api/art/a1?_t=1000');
  assert.equal(p.freshArt(SM('b2')), SM('b2'));                   // (another track's is not)
  assert.equal(p.freshArt('/assets/covers/a1.jpg'), '/assets/covers/a1.jpg');
});

test('each change gives a new address, also within the same millisecond', () => {
  const p = load();
  p.noteArtChanged('a1', 1000);
  p.noteArtChanged('a1', 1000);
  assert.equal(p.freshArt(SM('a1')), `${SM('a1')}&_t=1001`);
  p.noteArtChanged('a1', 900);                                    // (a clock that stepped back)
  assert.equal(p.freshArt(SM('a1')), `${SM('a1')}&_t=1002`);
  assert.equal(p.freshArt(`${SM('a1')}&_t=1000`), `${SM('a1')}&_t=1002`);   // replaced, not stacked
  assert.equal(p.freshArt('/api/art/a1?_t=5'), '/api/art/a1?_t=1002');
});

test('a changed cover setting gives every cover a new address', () => {
  const p = load();
  p.noteArtChanged('a1', 1000);
  p.noteArtChanged(null, 2000);
  assert.equal(p.freshArt(SM('a1')), `${SM('a1')}&_t=2000`);
  assert.equal(p.freshArt(SM('zz')), `${SM('zz')}&_t=2000`);
  p.noteArtChanged('a1', 2000);                                   // (an arrival right after: a new address again)
  assert.equal(p.freshArt(SM('a1')), `${SM('a1')}&_t=2001`);
  p.noteArtChanged('a1', 2500);                                   // (a later arrival for one track wins there)
  assert.equal(p.freshArt(SM('a1')), `${SM('a1')}&_t=2500`);
  assert.equal(p.freshArt(SM('zz')), `${SM('zz')}&_t=2000`);
  p.noteArtChanged(null, 2000);
  assert.equal(p.freshArt(SM('zz')), `${SM('zz')}&_t=2001`);
});

test('the change is announced with the track it is about', () => {
  const p = load();
  p.noteArtChanged('a1', 10);
  p.noteArtChanged(null, 20);
  p.noteArtMissed(30);
  assert.deepEqual(p.sent.map((e) => [e.type, e.detail]), [
    ['soniqboom:art-changed', { id: 'a1' }],
    ['soniqboom:art-changed', { id: null }],
    ['soniqboom:art-changed', { id: null, missing: 30 }],
  ]);
});

test('only the rows of that track ask again, with the <img> they have', () => {
  const a = row('a1', SM('a1'), true), b = row('b2', SM('b2'), true), none = row('', '', true);
  const p = load([a, b, none]);
  const imgA = a.art.imgs[0], imgB = b.art.imgs[0];
  p.noteArtChanged('a1', 1000);
  assert.deepEqual(p.asked, ['.m-row[data-track-id="a1"]']);
  assert.equal(a.art.imgs.length, 1);
  assert.equal(a.art.imgs[0], imgA);
  assert.equal(imgA.src, `${SM('a1')}&_t=1000`);
  assert.equal(imgA.cls.has('loaded'), false);                    // (hidden until the new one decodes)
  assert.equal(a.__artSrc, `${SM('a1')}&_t=1000`);
  assert.equal(imgB.src, SM('b2'));
  assert.equal(imgB.cls.has('loaded'), true);
  assert.equal(b.__artSrc, SM('b2'));
});

test('a row whose <img> left on "no cover" gets a new one that shows once decoded', () => {
  const a = row('a1', SM('a1'), null);
  const p = load([a]);
  assert.equal(a.art.imgs.length, 0);
  p.noteArtChanged('a1', 1000);
  assert.equal(a.art.imgs.length, 1);
  const img = a.art.imgs[0];
  assert.equal(img.src, `${SM('a1')}&_t=1000`);
  assert.equal(img.alt, '');
  assert.equal(img.cls.has('loaded'), false);
  img.onload();
  assert.equal(img.cls.has('loaded'), true);
  img.onerror();                                                  // (still none: it leaves again)
  assert.equal(a.art.imgs.length, 0);
});

test('a changed cover setting asks every row again', () => {
  const a = row('a1', SM('a1'), true), b = row('b2', SM('b2'), null), st = row('c3', '/assets/covers/c3.jpg', true);
  const p = load([a, b, st]);
  p.noteArtChanged(null, 3000);
  assert.deepEqual(p.asked, ['.m-row[data-track-id]']);
  assert.equal(a.art.imgs[0].src, `${SM('a1')}&_t=3000`);
  assert.equal(b.art.imgs[0].src, `${SM('b2')}&_t=3000`);
  assert.equal(st.art.imgs[0].src, '/assets/covers/c3.jpg');      // (a cover file of its own: not asked)
  assert.equal(st.art.imgs[0].cls.has('loaded'), true);
});

test('a slot is asked only for the track it shows', () => {
  const p = load();
  const holder = { __artSrc: '/api/art/a1/lg.jpg?fallback=none' };
  const art = slot(new Img());
  assert.equal(p.reaskArt(holder, art, 'b2'), false);
  assert.equal(art.imgs[0].src, '');
  assert.equal(p.reaskArt({ __artSrc: null }, art, 'a1'), false);         // (a station, nothing loaded)
  assert.equal(p.reaskArt({ __artSrc: '/assets/x.jpg' }, art, null), false);
  assert.equal(p.reaskArt(holder, null, 'a1'), false);
  assert.equal(p.reaskArt(null, art, 'a1'), false);
  assert.equal(art.imgs[0].src, '');
  p.noteArtChanged('a1', 50);
  assert.equal(p.reaskArt(holder, art, 'a1'), true);
  assert.equal(art.imgs[0].src, '/api/art/a1/lg.jpg?fallback=none&_t=50');
  assert.equal(holder.__artSrc, '/api/art/a1/lg.jpg?fallback=none&_t=50');
  assert.equal(p.reaskArt(holder, art, null), true);              // (no id: whatever it shows)
});

test('after the socket was away, only the slots showing no cover ask again', () => {
  const shown = row('a1', SM('a1'), true), blank = row('b2', SM('b2'), null), pending = row('c3', SM('c3'), false);
  const p = load([shown, blank, pending]);
  p.noteArtMissed(7000);
  assert.equal(shown.art.imgs[0].src, SM('a1'));
  assert.equal(shown.art.imgs[0].cls.has('loaded'), true);
  assert.equal(shown.__artSrc, SM('a1'));
  assert.equal(blank.art.imgs[0].src, `${SM('b2')}&_t=7000`);
  assert.equal(pending.art.imgs[0].src, `${SM('c3')}&_t=7000`);
  // A row painted for that track afterwards asks the same address (the list
  // does not ask twice), and a track that showed its cover keeps its own.
  assert.equal(p.freshArt(SM('b2')), `${SM('b2')}&_t=7000`);
  assert.equal(p.freshArt(SM('a1')), SM('a1'));
  p.noteArtMissed(7000);                                          // (again: a new address each time)
  assert.equal(blank.art.imgs[0].src, `${SM('b2')}&_t=7001`);
  p.noteArtChanged(null, 9000);                                   // (and after a setting change, past its address)
  blank.art.imgs[0].cls.delete('loaded');
  p.noteArtMissed(9000);
  assert.equal(blank.art.imgs[0].src, `${SM('b2')}&_t=9001`);
});

test('the remembered changes stay bounded', () => {
  const p = load();
  for (let i = 0; i < p._ART_AGAIN_MAX; i++) p.noteArtChanged(`t${i}`, 100);
  assert.equal(p._artAgain.size, p._ART_AGAIN_MAX);
  p.noteArtChanged('t0', 200);                                    // (one it holds: no reset)
  assert.equal(p._artAgain.size, p._ART_AGAIN_MAX);
  p.noteArtChanged('one-more', 300);
  assert.equal(p._artAgain.size, 1);
  assert.equal(p.freshArt(SM('one-more')), `${SM('one-more')}&_t=300`);
});

test('the views ask through freshArt and keep the asked address', () => {
  const dir = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend/js/mobile');
  const lib = readFileSync(resolve(dir, 'views/library.js'), 'utf8');
  const srch = readFileSync(resolve(dir, 'views/search.js'), 'utf8');
  const np = readFileSync(resolve(dir, 'views/nowplaying.js'), 'utf8');
  const app = readFileSync(resolve(dir, 'app.js'), 'utf8');
  assert.match(lib, /setArt\(row, artPlaceholderEmoji\(t\), freshArt\(/);
  assert.match(srch, /const src = freshArt\(staticArtUrl\(t\.cover_art\)/);
  assert.match(SRC, /const artSrc = freshArt\(staticArtUrl\(track\.cover_art\)[^\n]*\n\s*row\.__artSrc = artSrc;/);
  assert.match(np, /const artSrc = freshArt\(t\.id \?[^\n]*\n\s*art\.__artSrc = artSrc;/);
  assert.match(np, /if \(!MobileRadio\.active\) reaskArt\(art, art, e\.detail && e\.detail\.id, e\.detail && e\.detail\.missing\);/);
  assert.match(app, /const artSrc = freshArt\(staticArtUrl\(track\.cover_art\)[^\n]*\n\s*miniArt\.__artSrc = artSrc;/);
  assert.match(app, /if \(!MobileRadio\.active\) reaskArt\(miniArt, miniArt, e\.detail && e\.detail\.id, e\.detail && e\.detail\.missing\);/);
});
