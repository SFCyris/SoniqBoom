// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// auth.js: the REAL logout() and _clearPassword(), lifted out of the module
// and run with a fake fetch / timers / DOM.  A sign-out answered 503 while
// the server restarts is sent again until it lands (then the page reloads);
// given up, the page stays and says so.  The sign-in form keeps no password.
// Run: node --test tests/js/auth_logout.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/auth.js'), 'utf8');
const fn = (name) => {
  const m = SRC.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n}\\n`));
  assert.ok(m, `${name} not found in auth.js`);
  return m[0];
};

function makeLogout(answers, { clockStep = 0 } = {}) {
  const calls = [];
  const toasts = [];
  const reloads = [];
  let now = 0;
  const env = {
    fetch: async (url, init) => {
      calls.push([url, init?.method]);
      const a = answers.length ? answers.shift() : answers.last;
      if (a === 'network') throw new TypeError('Failed to fetch');
      return { status: a, headers: { get: (h) => (h === 'Retry-After' && a === 503 ? '2' : null) } };
    },
    setTimeout: (f, ms) => { now += ms + clockStep; f(); },
    Date: { now: () => now },
    Toast: { error: (m) => toasts.push(m) },
    STATE: { user: { id: 'u' } },
    location: { reload: () => reloads.push(1) },
  };
  const body = `${fn('logout')}; return logout;`;
  const logout = new Function(...Object.keys(env), body)(...Object.values(env));
  return { logout, calls, toasts, reloads, env };
}

test('a sign-out answered 503 during a restart is sent again until it lands', async () => {
  const t = makeLogout([503, 'network', 503, 200]);
  await t.logout();
  assert.equal(t.calls.length, 4);
  assert.ok(t.calls.every(([u, m]) => u === '/api/auth/logout' && m === 'POST'));
  assert.equal(t.reloads.length, 1);
  assert.equal(t.env.STATE.user, null);
  assert.equal(t.toasts.length, 0);
});

test('an immediate success signs out at once', async () => {
  const t = makeLogout([200]);
  await t.logout();
  assert.equal(t.calls.length, 1);
  assert.equal(t.reloads.length, 1);
});

test('a server still restarting after ~30 s: the page stays signed in and says so', async () => {
  const answers = [];
  answers.last = 503;
  const t = makeLogout(answers);
  await t.logout();
  assert.ok(t.calls.length >= 10 && t.calls.length <= 20, `${t.calls.length} attempts`);
  assert.equal(t.reloads.length, 0);
  assert.equal(t.toasts.length, 1);
  assert.notEqual(t.env.STATE.user, null);
});

test('the sign-in form drops the password (and hides it again) after a sign-in', () => {
  const pw = { value: 'Hunter2!secret', type: 'text' };
  const toggle = { textContent: '🙈', title: 'Hide password', setAttribute(k, v) { this[k] = v; } };
  const overlay = { querySelector: (sel) => ({ '#auth-password': pw, '#auth-pw-toggle': toggle }[sel] || null) };
  const clear = new Function(`${fn('_clearPassword')}; return _clearPassword;`)();
  clear(overlay);
  assert.equal(pw.value, '');
  assert.equal(pw.type, 'password');
  assert.equal(toggle.title, 'Show password');
  // and the submit handler calls it on success, before the overlay hides
  const submit = fn('_handleSubmit');
  assert.ok(submit.indexOf('_clearPassword(overlay)') > 0
            && submit.indexOf('_clearPassword(overlay)') < submit.indexOf('_hide()'));
});
