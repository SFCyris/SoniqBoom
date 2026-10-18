// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Track Info open on a package of an online collection: opening it opens the
// package, the server re-reads the track and says so (track_updated); app.js
// patches the list's copy of the track (the very object the panel shows) and
// then tells the panel.  The panel must still see what changed — before, it
// compared the new fields with that already-patched object, found nothing
// new and kept showing "LHA package" with no duration.
// Runs the REAL _keptFields / _panelBase / _applyFields of trackinfo.js on
// stubs for the panel's state.
// Run: node --test tests/js/trackinfo_refresh.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '../../soniqboom/frontend/js/trackinfo.js'), 'utf8');

function fn(name, optional = false) {
  const m = SRC.match(new RegExp(`\\nfunction ${name}\\([\\s\\S]*?\\n\\}\\n`));
  if (!m && optional) return '';
  assert.ok(m, `${name} not found in trackinfo.js`);
  return m[0];
}

function panel() {
  // The panel's module state and the DOM it touches, as stubs.
  const make = new Function('document', `
    let _queue = null, _idx = 0, _shown = null, _open = true;
    const rendered = [];
    function isOpen() { return _open; }
    function _render(t) { _shown = { ...t }; rendered.push({ ...t }); }
    ${fn('_keptFields')}
    ${fn('_panelBase', true)}
    ${fn('_applyFields')}
    return {
      show(queue, idx) { _queue = queue; _idx = idx; _render(queue[idx]); },
      apply: _applyFields, rendered,
    };`);
  return make({ getElementById: () => null });
}

test('a re-read the list took first still re-renders the open panel', () => {
  const p = panel();
  const row = { id: 'pkg', title: 'Jonahara', format: 'LHA package', duration: null };
  p.show([row], 0);
  const fields = { format: 'Face The Music', duration: 302.09, channels: 8,
                   path: 'x.lha::Jonahara.ftm' };
  Object.assign(row, fields);                 // Library.patchTrack: the shared object
  assert.equal(p.apply('pkg', fields), true);
  assert.equal(p.rendered.length, 2);
  assert.equal(p.rendered[1].format, 'Face The Music');
  assert.equal(p.rendered[1].duration, 302.09);
});

test('the same fields again render nothing, another track is not touched', () => {
  const p = panel();
  const row = { id: 'a', format: 'Face The Music', duration: 302.09 };
  p.show([row], 0);
  assert.equal(p.apply('a', { format: 'Face The Music', duration: 302.09 }), true);
  assert.equal(p.apply('b', { duration: 5 }), false);
  assert.equal(p.rendered.length, 1);
});

test('a tune of a multi-tune file keeps its own length', () => {
  const p = panel();
  const row = { id: 's', subsong: 2, duration: 61, format: 'SID' };
  p.show([row], 0);
  p.apply('s', { duration: 300, title: 'New' });
  assert.equal(p.rendered.at(-1).duration, 61);
  assert.equal(p.rendered.at(-1).title, 'New');
});
