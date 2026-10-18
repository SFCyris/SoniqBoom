// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A loud re-index that changed nothing (its end frame's ``lib_gen`` is the one
// the page already synced to) re-reads nothing: no counts, no list, no folder
// tree, no Scene-groups poll — so the user's place, selection and tree focus
// are left alone (soniqboom/frontend/js/app.js, the library socket's
// ``scan_progress`` handler; the generation bookkeeping is lib_sync.js).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createLibSync } from '../../soniqboom/frontend/js/lib_sync.js';

const SRC = readFileSync(process.env.SB_APP_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/app.js'), 'utf8');

test('LibSync.note says "not moved" for the generation already synced (the gate the handler uses)', () => {
  const ls = createLibSync();
  assert.equal(ls.snapshot('g1'), 'first');
  assert.equal(ls.note('g1'), false);
  assert.equal(ls.note('g2'), true);
  assert.equal(ls.note(undefined), true);              // no token: treated as changed
});

test('an unchanged loud scan end returns before every re-read', () => {
  const at = SRC.indexOf("if (msg.event === 'scan_progress') {");
  const fn = SRC.slice(at, SRC.indexOf("} else if (msg.event === 'availability_changed')", at));
  assert.match(fn, /let unchanged = false;/);
  assert.match(fn, /unchanged = !moved && !msg\.embedding;/);
  const done = fn.slice(fn.indexOf('// Both phases complete.'));
  const gate = done.indexOf('if (unchanged) return;');
  assert.ok(gate > 0, 'no gate');
  for (const call of ['Library.refreshBadges()', '_pollSceneGroupsFacet(', '_refreshMainAfterScan(', 'FolderTree.refresh()']) {
    const i = done.indexOf(call);
    assert.ok(i > gate, `${call} runs before the gate`);
  }
  // the badge still says Done and the tree spinner still stops
  assert.ok(done.indexOf('_doneLabel(msg.processed)') < gate);
  assert.ok(done.indexOf('FolderTree.setScanActive(false)') < gate);
});
