// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The sign-in overlay (soniqboom/frontend/js/auth.js), run outside a browser:
//   * ``[auth-trap:begin]`` — Tab / Shift+Tab move between the controls that
//     are shown and enabled only (a control inside a ``hidden`` block — the
//     register link, the setup hint — or with no box is skipped), wrapping at
//     both ends, so focus never leaves the dialog;
//   * ``[auth-unavailable:begin]`` — the "can't reach / restarting" card says
//     the page reconnects by itself (no "click below"), and tries again on a
//     growing pause;
//   * wiring: the error line is a live region, the whole page behind the
//     overlay is made inert (also on the phone's page, which has no #app), and
//     the unavailable card retries by itself.
// Run: node --test tests/js/auth_overlay.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(process.env.SB_AUTH_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/auth.js'), 'utf8');
const region = (name) => {
  const m = SRC.match(new RegExp(`// \\[${name}:begin\\][^\\n]*\\n([\\s\\S]*?)// \\[${name}:end\\]`));
  if (!m) throw new Error(`${name} region not found`);
  return m[1];
};

// A minimal element: ``hiddenIn`` = inside a [hidden] ancestor; ``box`` = has a layout box.
function el(id, { disabled = false, hiddenIn = false, box = true } = {}) {
  return { id, disabled, closest: (sel) => (hiddenIn && /\[hidden\]/.test(sel) ? {} : null),
           getClientRects: () => (box ? [{}] : []) };
}
function modal(list) { return { querySelectorAll: () => list }; }

const trap = new Function(`${region('auth-trap')}\nreturn { _trapFocusables, _trapStep };`)();

test('only shown, enabled controls count — the hidden register link and setup re-check are skipped', () => {
  const user = el('auth-username'), pw = el('auth-password'), eye = el('auth-pw-toggle');
  const submit = el('auth-submit', { disabled: true });
  const retry = el('auth-retry-network');
  const reg = el('auth-mode-register', { hiddenIn: true });
  const recheck = el('auth-bootstrap-retry', { hiddenIn: true });
  const nobox = el('collapsed', { box: false });
  const got = trap._trapFocusables(modal([user, pw, eye, submit, retry, reg, recheck, nobox]));
  assert.deepEqual(got.map((x) => x.id), ['auth-username', 'auth-password', 'auth-pw-toggle', 'auth-retry-network']);
});

test('Tab and Shift+Tab wrap both ways; lost focus re-enters at the right end', () => {
  const a = el('a'), b = el('b'), c = el('c');
  const list = [a, b, c];
  assert.equal(trap._trapStep(list, a, false), b);
  assert.equal(trap._trapStep(list, c, false), a);           // Tab on the last → first
  assert.equal(trap._trapStep(list, a, true), c);            // Shift+Tab on the first → last
  assert.equal(trap._trapStep(list, b, true), a);
  assert.equal(trap._trapStep(list, { id: 'body' }, false), a);
  assert.equal(trap._trapStep(list, { id: 'body' }, true), c);
  assert.equal(trap._trapStep([], a, false), null);
});

test('every Tab inside the dialog is handled (focus never walks out)', () => {
  const fn = SRC.slice(SRC.indexOf('function _installFocusTrap('), SRC.indexOf('function _installFocusTrap(') + 700);
  assert.match(fn, /_trapStep\(_trapFocusables\(modal\), document\.activeElement, e\.shiftKey\)/);
  assert.match(fn, /e\.preventDefault\(\);\s*if \(next\) next\.focus\(\);/);
});

const un = new Function(`${region('auth-unavailable')}\nreturn { _unavailableText, _retryDelayMs };`)();

test('the unavailable card never says "click", and says it reconnects by itself', () => {
  for (const r of [true, false]) {
    const t = un._unavailableText(r);
    assert.doesNotMatch(t, /click/i);
    assert.match(t, /reconnect/i);
  }
  assert.match(un._unavailableText(true), /restarting/);
  assert.match(un._unavailableText(false), /Can’t reach/);
});

test('it tries again every 2 s at first, then on a growing pause up to 15 s', () => {
  const d = Array.from({ length: 14 }, (_, i) => un._retryDelayMs(i));
  assert.deepEqual(d.slice(0, 5), [2000, 2000, 2000, 2000, 2000]);
  for (let i = 1; i < d.length; i++) assert.ok(d[i] >= d[i - 1]);
  assert.ok(d[5] > 2000);
  assert.equal(d[13], 15000);
});

test('the error line is a live region; the page behind is inert on both shells', () => {
  assert.match(SRC, /id="auth-error" class="auth-error" role="alert" aria-live="assertive"/);
  const show = SRC.slice(SRC.indexOf('function _show()'), SRC.indexOf('function _show()') + 900);
  assert.match(show, /_inertBackground\(el, true\)/);
  assert.doesNotMatch(show, /getElementById\('app'\)/);       // (the phone's page has no #app)
  const inert = SRC.slice(SRC.indexOf('function _inertBackground('), SRC.indexOf('function _inertBackground(') + 900);
  assert.match(inert, /for \(const n of document\.body\.children\)/);
});

test('the unavailable card retries by itself, and Retry asks at once (no reload mid-use)', () => {
  const fn = SRC.slice(SRC.indexOf('function _showUnavailable('), SRC.indexOf('// ── Boot'));
  assert.match(fn, /_scheduleAutoRetry\(\)/);
  assert.match(fn, /addEventListener\('click', \(\) => _retryNow\(\)\)/);
  assert.doesNotMatch(fn, /Click below/);
});

test('signing in (or reconnecting with a valid sign-in) hides the Retry button for next time', () => {
  const fn = SRC.slice(SRC.indexOf('function _hide()'), SRC.indexOf('function _inertBackground('));
  assert.match(fn, /querySelector\('#auth-retry-network'\)/);
  assert.match(fn, /retry\.hidden = true;/);
});
