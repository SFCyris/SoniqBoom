// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The queue's "Cleared N tracks · Undo" offer is the shared offer toast
// (Toast.action, docked above the player bar) — not a toast of its own on the
// volume slider.  The REAL _offerUndo / _dismissUndoToast from queue.js run
// against a fake Toast and Player; the clear handler and refresh() are checked
// in the source.
// Run: node --test tests/js/queue_undo_offer.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/queue.js'), 'utf8');
const fn = (name) => {
  const m = SRC.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}\\n`));
  assert.ok(m, `${name} not found in queue.js`);
  return m[0];
};

function load() {
  const offers = [];
  const dismissed = [];
  const restored = [];
  let refreshes = 0;
  const Toast = {
    action(msg, label, onAction, ms) {
      const el = { isConnected: true, msg, label, ms, press: () => { el.isConnected = false; onAction(); } };
      offers.push(el);
      return el;
    },
    dismiss(el) { el.isConnected = false; dismissed.push(el); },
  };
  let api;
  const logged = [];
  const Player = {
    fail: false,
    restoreQueue(snap) { if (Player.fail) throw new Error('boom'); restored.push({ snap, offerKnown: api.toast !== null }); },
  };
  api = new Function('Toast', 'Player', 'refresh', 'console', `
    const UNDO_WINDOW_MS = 5000;
    let _undoToast = null;
    ${fn('_offerUndo')}
    ${fn('_dismissUndoToast')}
    return { offer: _offerUndo, dismiss: _dismissUndoToast, get toast() { return _undoToast; } };`)(
    Toast, Player, () => { refreshes++; }, { error: (...a) => logged.push(a) });
  return { api, offers, dismissed, restored, logged, Player, refreshes: () => refreshes };
}

test('a clear offers Undo through the shared offer toast, for the 5 s window', () => {
  const t = load();
  t.api.offer(3, { q: 'snap' });
  assert.equal(t.offers.length, 1);
  assert.deepEqual([t.offers[0].msg, t.offers[0].label, t.offers[0].ms], ['Cleared 3 tracks.', 'Undo', 5000]);
  assert.equal(t.api.toast, t.offers[0]);
  t.api.offer(1, {});
  assert.equal(t.offers[1].msg, 'Cleared 1 track.');
});

test('Undo puts back the snapshot taken at the clear, the offer forgotten first', () => {
  const t = load();
  const snap = { queue: ['a', 'b'] };
  t.api.offer(2, snap);
  t.offers[0].press();
  assert.equal(t.restored.length, 1);
  assert.equal(t.restored[0].snap, snap);
  // (forgotten before the restore, so its 'queuechange' → refresh() can't withdraw it)
  assert.equal(t.restored[0].offerKnown, false);
  assert.equal(t.api.toast, null);
  assert.equal(t.refreshes(), 1);
});

test('an Undo that fails is logged (Toast.action swallows errors) and the panel still refreshes', () => {
  const t = load();
  t.Player.fail = true;
  t.api.offer(2, {});
  t.offers[0].press();
  assert.equal(t.logged.length, 1);
  assert.equal(t.refreshes(), 1);
});

test('a second clear withdraws the first offer; its Undo belongs to the new clear', () => {
  const t = load();
  t.api.offer(4, { n: 1 });
  t.api.offer(2, { n: 2 });
  assert.deepEqual(t.dismissed, [t.offers[0]]);
  t.offers[1].press();
  assert.deepEqual(t.restored.map((r) => r.snap.n), [2]);
});

test('withdrawing the offer (a new queue started) closes it', () => {
  const t = load();
  t.api.offer(5, {});
  t.api.dismiss();
  assert.deepEqual(t.dismissed, [t.offers[0]]);
  assert.equal(t.api.toast, null);
  t.api.dismiss();                                // nothing open: no-op
  assert.equal(t.dismissed.length, 1);
});

test('the clear handler snapshots before it clears, then offers Undo', () => {
  const h = SRC.match(/getElementById\('btn-queue-clear'\)\.addEventListener\('click'[\s\S]*?\n}\);\n/);
  assert.ok(h, 'clear handler not found');
  const src = h[0];
  const snapAt = src.indexOf('Player.snapshotQueue()');
  const clearAt = src.indexOf('Player.setQueue([], 0)');
  const offerAt = src.indexOf('_offerUndo(count, snap)');
  assert.ok(snapAt > 0 && clearAt > snapAt && offerAt > clearAt, 'snapshot → clear → offer');
});

test('refresh() drops a timed-out offer and withdraws Undo once a new queue starts', () => {
  const r = fn('refresh');
  assert.match(r, /if \(_undoToast && !_undoToast\.isConnected\) _undoToast = null;/);
  assert.match(r, /if \(_undoToast && q\.length && Player\.autoAhead < q\.length\) _dismissUndoToast\(\);/);
});

test('the offer after a confirm dialog docks above the bar; the closing dialog still takes clicks', () => {
  // utils.js hit-tests the bar's centre: a backdrop still fading out read as
  // "the bar is covered", and the offer sat on the bar for its whole window.
  // It must be skipped there — not made click-through: a double-click on
  // "Clear" would then land its second click on a track row and start a queue.
  const utils = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/utils.js'), 'utf8');
  const take = utils.match(/function _barTakesClicks\([\s\S]*?\n}\n/);
  assert.ok(take, '_barTakesClicks not found');
  assert.match(take[0], /!el\.closest\?\.\('\.pl-modal-out'\)/);
  const css = readFileSync(resolve(HERE, '../../soniqboom/frontend/css/app.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [...css.matchAll(/([^{}]*\.pl-modal-out[^{}]*)\{([^}]*)\}/g)];
  assert.ok(rules.length, '.pl-modal-out rule not found');
  for (const [, sel, body] of rules) assert.doesNotMatch(body, /pointer-events/, `${sel.trim()} lets clicks through`);
  assert.match(SRC, /backdrop\.classList\.add\('pl-modal-out'\)/);
});

test('no toast of its own any more (it sat bottom-right, on the volume slider)', () => {
  assert.doesNotMatch(SRC, /queue-undo-toast|right:18px;bottom:18px/);
  assert.match(SRC, /import \{[^}]*\bToast\b[^}]*\} from '\.\/utils\.js';/);
});
