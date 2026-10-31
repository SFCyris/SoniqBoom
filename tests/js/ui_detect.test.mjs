// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Which interface / starts with (soniqboom/frontend/index.html, the head
// script's ``[ui-detect:begin]`` / ``[ui-detect:end]`` region), run outside a
// browser:
//   * sbTouchDevice: phones and tablets by user agent, an iPad asking for the
//     desktop site (Macintosh + touch points), a coarse primary pointer with
//     touch — never a Mac, a desktop, or a touch-screen laptop (fine pointer);
//   * sbUiPick: ?ui= first (and pinned), then the sb_ui cookie, then the device;
//   * the whole head script in a fake window: redirects to /m, sets the
//     attributes the header's phone button keys off, writes the cookie for ?ui=.
// Also the markup / CSS the switch needs (index.html, app.css, mobile.html,
// mobile.css).
// Run: node --test tests/js/ui_detect.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');
const INDEX = read('index.html');

const region = (src, name) => {
  const m = src.match(new RegExp(`// \\[${name}:begin\\][^\\n]*\\n([\\s\\S]*?)// \\[${name}:end\\]`));
  if (!m) throw new Error(`${name} region not found`);
  return m[1];
};
const { sbTouchDevice, sbUiPick, sbDropUi } = new Function(
  `${region(INDEX, 'ui-detect')}\nreturn { sbTouchDevice, sbUiPick, sbDropUi };`)();

const UA = {
  iphone: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
  ipadDesktop: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
  ipadMobile: 'Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
  androidPhone: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36',
  androidTablet: 'Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
  macChrome: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
  windows: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 Edg/126.0',
  linuxDesktop: 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
  chromebook: 'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
  firefoxIos: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/127.0 Mobile/15E148 Safari/605.1.15',
};

test('phones and tablets are touch devices; desktops, Macs and touch laptops are not', () => {
  assert.equal(sbTouchDevice(UA.iphone, 5, true), true);
  assert.equal(sbTouchDevice(UA.ipadMobile, 5, true), true);
  assert.equal(sbTouchDevice(UA.ipadDesktop, 5, true), true, 'iPad asking for the desktop site');
  assert.equal(sbTouchDevice(UA.ipadDesktop, 5, false), true, 'iPad with a trackpad attached (fine pointer)');
  assert.equal(sbTouchDevice(UA.androidPhone, 5, true), true);
  assert.equal(sbTouchDevice(UA.androidTablet, 10, true), true);
  assert.equal(sbTouchDevice(UA.firefoxIos, 5, true), true);
  // a tablet asking for the desktop site with a Linux / Windows UA: the pointer
  assert.equal(sbTouchDevice(UA.linuxDesktop, 10, true), true);
  // not touch-primary
  assert.equal(sbTouchDevice(UA.ipadDesktop, 0, false), false, 'a Mac (Safari reports 0 touch points)');
  assert.equal(sbTouchDevice(UA.ipadDesktop, 1, false), false);
  assert.equal(sbTouchDevice(UA.macChrome, 0, false), false);
  assert.equal(sbTouchDevice(UA.windows, 0, false), false);
  assert.equal(sbTouchDevice(UA.windows, 10, false), false, 'touch-screen laptop: fine primary pointer');
  assert.equal(sbTouchDevice(UA.chromebook, 10, false), false, 'touch Chromebook in laptop mode');
  assert.equal(sbTouchDevice(UA.windows, 0, true), false, 'coarse pointer without touch points');
  assert.equal(sbTouchDevice('', undefined, false), false);
  assert.equal(sbTouchDevice(undefined, 'x', true), false);
});

test('the user-agent test matches the server regex (main.py _MOBILE_UA_RE)', () => {
  const py = readFileSync(resolve(HERE, '../../soniqboom/main.py'), 'utf8');
  const m = /_MOBILE_UA_RE = [^\n]*\n\s*r"([^"]+)"/.exec(py);
  assert.ok(m, 'server regex not found');
  const server = new RegExp(m[1], 'i');
  const samples = [...Object.values(UA), 'Opera Mini/8', 'IEMobile/11', 'EdgiOS/126', 'MobileSafari', 'Mozilla/5.0 (Mobile; rv:48.0) Gecko'];
  for (const ua of samples) assert.equal(sbTouchDevice(ua, 0, false), server.test(ua), ua);
});

test('?ui= wins and is pinned; then the cookie; then the device', () => {
  const pick = (...a) => { const r = sbUiPick(...a); return [r.ui, r.pin, r.pinned, r.stored]; };
  assert.deepEqual(pick('', '', true), ['mobile', null, null, false]);
  assert.deepEqual(pick('', '', false), ['desktop', null, null, false]);
  assert.deepEqual(pick('', 'a=1; sb_ui=desktop', true), ['desktop', null, 'desktop', false]);
  assert.deepEqual(pick('', 'sb_ui=mobile; b=2', false), ['mobile', null, 'mobile', false]);
  assert.deepEqual(pick('?ui=desktop', 'sb_ui=mobile', true), ['desktop', 'desktop', 'desktop', false]);
  assert.deepEqual(pick('?x=1&ui=MOBILE', 'sb_ui=desktop', false), ['mobile', 'mobile', 'mobile', false]);
  // the server already stored it: nothing to write
  assert.deepEqual(pick('?ui=desktop', 'sb_session=x; sb_ui=desktop', true), ['desktop', 'desktop', 'desktop', true]);
  // as the server reads it (Starlette): the LAST ui= wins, the key is case-sensitive
  assert.equal(sbUiPick('?ui=mobile&ui=desktop', '', true).pin, 'desktop');
  assert.equal(sbUiPick('?UI=desktop', '', true).pin, null);
  // look-alikes are not a choice
  assert.equal(sbUiPick('?ui=tablet', '', false).pin, null);
  assert.equal(sbUiPick('?gui=mobile', '', false).pin, null);
  assert.equal(sbUiPick('?ui=mobilex', '', false).pin, null);
  assert.equal(sbUiPick('', 'xsb_ui=mobile', false).pinned, null);
  assert.equal(sbUiPick('', 'sb_ui_style=retro', true).pinned, null, 'the Style key is not the interface cookie');
  assert.equal(sbUiPick('', 'sb_ui=mobilex', false).pinned, null);
});

test('the ui= is taken out of the address, other parameters kept', () => {
  assert.equal(sbDropUi('?ui=desktop'), '');
  assert.equal(sbDropUi('?a=1&ui=desktop&b=2'), '?a=1&b=2');
  assert.equal(sbDropUi('?ui=mobile&ui=desktop'), '');
  assert.equal(sbDropUi('?ui'), '');
  assert.equal(sbDropUi('?guide=1&ui=x'), '?guide=1');
  assert.equal(sbDropUi(''), '');
});

// The whole head script in a fake window.
function runHead({ ua, touchPoints = 0, coarse = false, path = '/', search = '', cookie = '', noHistory = false }) {
  const script = /<script>\s*(\(function \(\) \{\s*\/\/ \[ui-detect:begin\][\s\S]*?)<\/script>/.exec(INDEX);
  assert.ok(script, 'head script not found');
  const attrs = {};
  const writes = [];
  const replaced = [];
  const urls = [];
  const listeners = {};
  const de = { style: {}, setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; } };
  const document = {
    documentElement: de,
    get cookie() { return cookie; },
    set cookie(v) { writes.push(v); },
  };
  const window = {
    matchMedia: (q) => ({ matches: q === '(pointer: coarse)' && coarse }),
    addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); },
  };
  const navigator = { userAgent: ua, maxTouchPoints: touchPoints };
  const location = { pathname: path, search, hash: '', replace: (u) => replaced.push(u) };
  const history = noHistory ? {} : {
    state: null,
    replaceState: (st, t, u) => { urls.push(u); const q = u.indexOf('?'); location.search = q < 0 ? '' : u.slice(q); },
  };
  new Function('window', 'document', 'navigator', 'location', 'history', script[1])(window, document, navigator, location, history);
  const env = { attrs, writes, replaced, urls, de,
    get hidden() { return de.style.visibility === 'hidden'; },
    setCookie: (c) => { cookie = c; },
    pageshow: (persisted) => (listeners.pageshow || []).forEach((fn) => fn({ persisted })) };
  return env;
}

test('the head script: an iPad / phone goes to /m unless desktop is pinned', () => {
  let r = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true });
  assert.deepEqual(r.replaced, ['/m']);
  assert.equal(r.hidden, true);
  assert.deepEqual(r.writes, [], 'auto-detection never pins');
  assert.equal(r.attrs['data-sb-touch'], '');

  r = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true, cookie: 'sb_ui=desktop' });
  assert.deepEqual(r.replaced, []);
  assert.equal(r.hidden, false);
  assert.equal(r.attrs['data-sb-touch'], '');
  assert.equal(r.attrs['data-sb-ui-pinned'], 'desktop');

  // ?ui=desktop (the phone page's key) served by the worker: stays, pinned, the
  // cookie written, the ui= taken out of the address
  r = runHead({ ua: UA.iphone, touchPoints: 5, coarse: true, search: '?ui=desktop' });
  assert.deepEqual(r.replaced, []);
  assert.equal(r.attrs['data-sb-ui-pinned'], 'desktop');
  assert.equal(r.writes.length, 1);
  assert.match(r.writes[0], /^sb_ui=desktop; path=\/; max-age=31536000; samesite=lax$/);
  assert.deepEqual(r.urls, ['/']);

  // ... served by the server, which set the cookie: no script write (it would be kept for less)
  r = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true, search: '?a=1&ui=desktop', cookie: 'sb_ui=desktop' });
  assert.deepEqual(r.writes, []);
  assert.deepEqual(r.replaced, []);
  assert.deepEqual(r.urls, ['/?a=1']);

  // without history.replaceState it still works
  r = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true, search: '?ui=desktop', cookie: 'sb_ui=desktop', noHistory: true });
  assert.deepEqual(r.replaced, []);
  assert.equal(r.attrs['data-sb-ui-pinned'], 'desktop');

  // ?ui=mobile answered from the worker's cache: pinned and sent to /m
  r = runHead({ ua: UA.macChrome, search: '?ui=mobile', cookie: 'sb_ui=desktop' });
  assert.deepEqual(r.replaced, ['/m']);
  assert.match(r.writes[0], /^sb_ui=mobile;/);

  // a pinned mobile choice on a cached /
  r = runHead({ ua: UA.macChrome, cookie: 'sb_ui=mobile' });
  assert.deepEqual(r.replaced, ['/m']);
});

test('the head script: Back to a kept copy of the page follows a choice made since', () => {
  // the iPad on the desktop interface, then 📱 pinned mobile, then Back
  const r = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true, search: '?ui=desktop', cookie: 'sb_ui=desktop' });
  assert.deepEqual(r.replaced, []);
  r.setCookie('sb_ui=mobile');
  r.pageshow(false);
  assert.deepEqual(r.replaced, [], 'a fresh load is not re-checked');
  r.pageshow(true);
  assert.deepEqual(r.replaced, ['/m']);
  assert.equal(r.hidden, true);
  assert.equal(r.attrs['data-sb-ui-pinned'], undefined);
  // and a kept copy that left for /m and comes back pinned desktop shows again
  const q = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true });
  assert.deepEqual(q.replaced, ['/m']);
  q.setCookie('sb_ui=desktop');
  q.pageshow(true);
  assert.deepEqual(q.replaced, ['/m']);
  assert.equal(q.hidden, false);
  assert.equal(q.attrs['data-sb-ui-pinned'], 'desktop');
});

test('the head script: a desktop or touch laptop stays, without the phone button', () => {
  for (const p of [{ ua: UA.macChrome }, { ua: UA.windows, touchPoints: 10, coarse: false },
                   { ua: UA.chromebook, touchPoints: 10 }, { ua: UA.ipadDesktop, touchPoints: 0 }]) {
    const r = runHead(p);
    assert.deepEqual(r.replaced, [], p.ua);
    assert.equal(r.hidden, false);
    assert.deepEqual(r.attrs, {}, 'no data-sb-touch / data-sb-ui-pinned');
    assert.deepEqual(r.writes, []);
  }
});

test('the head script never redirects away from another path (no loop on /m served from cache)', () => {
  for (const path of ['/m', '/m/', '/some/spa/route', '/multiroom']) {
    const r = runHead({ ua: UA.ipadDesktop, touchPoints: 5, coarse: true, path });
    assert.deepEqual(r.replaced, [], path);
    assert.equal(r.hidden, false);
  }
});

test('the head script runs before any stylesheet, module or markup of index.html', () => {
  const at = INDEX.indexOf('[ui-detect:begin]');
  assert.ok(at > 0);
  const head = INDEX.slice(0, at);
  assert.doesNotMatch(head, /<link\b|<script\b[^>]*\bsrc=|<div\b|<body\b/,
    'only <meta> tags may come before the detection script');
});

test('the switch keys: desktop header button and the phone top bar key', () => {
  const a = /<a id="btn-phone-ui"[^>]*>/.exec(INDEX);
  assert.ok(a, '#btn-phone-ui missing');
  assert.match(a[0], /href="\/\?ui=mobile"/);
  assert.match(a[0], /aria-label="[^"]*phone and iPad interface"/);
  const css = read('css/app.css');
  assert.match(css, /#btn-phone-ui \{ display: none;/);
  assert.match(css, /:root\[data-sb-touch\] #btn-phone-ui,\s*:root\[data-sb-ui-pinned="desktop"\] #btn-phone-ui \{ display: inline-flex; \}/);
  const css131 = /app\.css\?v=(\d+)/.exec(INDEX);
  assert.ok(Number(css131[1]) >= 131, 'app.css cache-buster bumped for the phone button rule');

  const mob = read('mobile.html');
  assert.match(mob, /<a id="m-desktop-link"\s+href="\/\?ui=desktop"/);
  const mcss = read('css/mobile.css');
  assert.match(mcss, /@media \(max-width: 427\.98px\), \(max-height: 427\.98px\) \{\s*\.m-desktop-link \{ display: none; \}/,
    'the Desktop site key hides on a small display (the 428 px rule)');
  // the phone page never pins (no file of it writes the cookie)
  const files = ['mobile.html', 'js/mobile/app.js', ...readdirSync(resolve(FE, 'js/mobile/views')).map((f) => `js/mobile/views/${f}`)];
  for (const f of files.filter((f) => /\.(js|html)$/.test(f))) assert.ok(!/document\.cookie\s*=\s*['"`]\s*sb_ui\b|sb_ui=mobile/.test(read(f)), f);
});
