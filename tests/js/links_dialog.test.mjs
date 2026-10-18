// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The "Find music" dialog (soniqboom/frontend/js/app.js, #links-overlay) is a
// modal dialog: opening it puts focus inside, Escape closes it with focus
// inside (the page's shortcut handler leaves a focused dialog's keys alone, so
// the dialog handles its own), Tab / Shift+Tab stay inside, and closing puts
// focus back on its button.  Source checks (the browser checks are in the
// verification scripts).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(process.env.SB_APP_JS || resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/app.js'), 'utf8');
const block = SRC.slice(SRC.indexOf("const _linksOverlay = document.getElementById('links-overlay');"),
                        SRC.indexOf('// ── Welcome / onboarding overlay'));

test('Escape closes the dialog from inside it, and focus returns to its button', () => {
  assert.match(block, /_linksOverlay\.addEventListener\('keydown'/);
  assert.match(block, /if \(e\.key === 'Escape'\) \{ e\.preventDefault\(\); e\.stopPropagation\(\); closeLinks\(\); return; \}/);
  assert.match(block, /_btnLinks\.focus\(/);
});

test('opening focuses inside; Tab wraps within the dialog', () => {
  assert.match(block, /_linksClose\.focus\(/);
  assert.match(block, /if \(e\.key !== 'Tab'\) return;/);
  assert.match(block, /\(i \+ \(e\.shiftKey \? list\.length - 1 : 1\)\) % list\.length/);
});
