// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The keys of a yes / no confirm (soniqboom/frontend/js/confirm_keys.js) and
// both confirms that use them: the playlist module's inline confirm (in use
// until Settings has been opened once) and Settings' styledConfirm.  Enter on
// a focused Cancel used to delete the playlist.
// Run: node --test tests/js/confirm_keys.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const { confirmKeyAction, confirmInitialFocus } = await import(resolve(FE, 'js/confirm_keys.js'));

const ok = { id: 'ok' }, cancel = { id: 'cancel' }, body = { id: 'dialog' };

test('Enter and Space answer for the focused button only', () => {
  for (const key of ['Enter', ' ', 'Spacebar']) {
    assert.equal(confirmKeyAction(key, ok, ok, cancel), 'ok', key);
    assert.equal(confirmKeyAction(key, cancel, ok, cancel), 'cancel', `${key} on Cancel`);
    assert.equal(confirmKeyAction(key, body, ok, cancel), 'none', `${key} on neither`);
    assert.equal(confirmKeyAction(key, null, ok, cancel), 'none', `${key} with no focus`);
  }
});

test('Escape cancels wherever the focus is; other keys are not the confirm\'s', () => {
  assert.equal(confirmKeyAction('Escape', ok, ok, cancel), 'cancel');
  assert.equal(confirmKeyAction('Esc', body, ok, cancel), 'cancel');
  for (const key of ['Tab', 'a', 'ArrowDown', 'Delete']) assert.equal(confirmKeyAction(key, ok, ok, cancel), null, key);
});

test('a destructive confirm opens on Cancel, another on OK', () => {
  assert.equal(confirmInitialFocus(true, ok, cancel), cancel);
  assert.equal(confirmInitialFocus(false, ok, cancel), ok);
});

// Both confirms use the shared keys — no handler that says yes to any Enter.
const PL = readFileSync(resolve(FE, 'js/playlist.js'), 'utf8');
const AD = readFileSync(resolve(FE, 'js/admin.js'), 'utf8');
const inline = PL.slice(PL.indexOf('function _inlineConfirm('), PL.indexOf('let _inlineConfirmSeq'));

test('the playlist inline confirm: shared keys, Cancel first when destructive, its keys kept from the page', () => {
  assert.ok(inline.length > 200, 'inline confirm not found');
  assert.doesNotMatch(inline, /key === 'Enter'\)\s*finish\(true\)/, 'an unconditional Enter → yes is back');
  assert.match(inline, /confirmKeyAction\(e\.key, document\.activeElement, btnOk, btnCan\)/);
  assert.match(inline, /confirmInitialFocus\(dangerColor, btnOk, btnCan\)\.focus\(\)/);
  assert.match(inline, /dangerColor = true/);
  assert.match(inline, /e\.stopPropagation\(\)/);
  assert.match(inline, /trapFocus\(dialog\)/);
  assert.match(inline, /role', 'alertdialog'/);
});

test("Settings' styledConfirm: Cancel first when destructive, Enter answers the focused button", () => {
  const sc = AD.slice(AD.indexOf('function styledConfirm('), AD.indexOf('function _closeConfirmDialog('));
  assert.match(sc, /confirmInitialFocus\(dangerColor, confirmOk, confirmCancel\)\.focus\(\)/);
  assert.doesNotMatch(sc, /confirmOk\.focus\(\)/);
  assert.match(AD, /confirmDialog\.addEventListener\('keydown'[\s\S]{0,200}confirmKeyAction\(e\.key, document\.activeElement, confirmOk, confirmCancel\)/);
});

test('the playlist deletes and removes ask a destructive confirm (Cancel focused)', () => {
  // (both pass no dangerColor: the default, true)
  const calls = [...PL.matchAll(/__sbConfirm\(\s*[^,]+,\s*\{([^}]*)\}/g)].map((m) => m[1]);
  assert.ok(calls.length >= 3, `found ${calls.length} confirm calls`);
  for (const opts of calls) assert.doesNotMatch(opts, /dangerColor:\s*false/, opts);
});

test("the queue's Clear-queue confirm follows the same keys (it said yes to any Enter)", async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../soniqboom/frontend/js/queue.js', import.meta.url), 'utf8');
  const body = src.slice(src.indexOf('function _showClearConfirm('), src.indexOf("document.getElementById('btn-queue-clear')"));
  assert.ok(body.length > 100, '_showClearConfirm found');
  assert.match(body, /confirmInitialFocus\(true, btnOk, btnCan\)\.focus\(\)/, 'opens on Cancel');
  assert.match(body, /confirmKeyAction\(e\.key, document\.activeElement, btnOk, btnCan\)/, 'keys answer for the focused button');
  assert.doesNotMatch(body, /e\.key === 'Enter'\)\s*finish\(true\)/, 'no Enter-anywhere yes');
});
