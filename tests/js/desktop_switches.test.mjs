// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Small desktop pieces: the top bar's Retro hi-fi / Modern switch
// (js/ui_style_switch.js + its wiring), the welcome guide's lead key
// (js/welcome_lead.js), the sign-in fields' names (js/auth.js) and leaving a
// search (search.js / library.js never jump to All Tracks).
// Run: node --test tests/js/desktop_switches.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const { styleSwitchText } = await import(resolve(FE, 'js/ui_style_switch.js'));
const { welcomeLead } = await import(resolve(FE, 'js/welcome_lead.js'));
const read = (p) => readFileSync(resolve(FE, p), 'utf8');

test('the style switch names the style in use and the one a press gives', () => {
  assert.deepEqual(styleSwitchText('retro'), {
    label: 'Switch to Modern style (now Retro hi-fi)', title: 'Style: Retro hi-fi — click for Modern',
    item: 'Switch to Modern style', next: 'modern' });
  assert.equal(styleSwitchText('modern').label, 'Switch to Retro hi-fi style (now Modern)');
  assert.equal(styleSwitchText('modern').next, 'retro');
  assert.equal(styleSwitchText('bogus').next, 'modern');      // (unknown → the default, Retro hi-fi)
});

test('the style switch: one button in the top bar, wired to the shared style, Settings follows it', () => {
  const H = read('index.html'), A = read('js/app.js'), AD = read('js/admin.js'), M = read('mobile.html');
  const actions = H.slice(H.indexOf('<div class="header-actions">'), H.indexOf('<div id="hdr-more-menu"'));
  assert.match(actions, /<button id="btn-ui-style" type="button"/);
  assert.match(actions, /ui-style-ico-retro[\s\S]*ui-style-ico-modern/);
  assert.ok(actions.indexOf('id="btn-ui-style"') < actions.indexOf('id="btn-admin"'), 'next to the gear');
  assert.match(A, /btn\?\.addEventListener\('click', \(\) => setUiStyle\(getUiStyle\(\) === 'retro' \? 'modern' : 'retro'\)\)/);
  assert.match(A, /onUiStyleChange\(paint\)/);
  assert.match(AD, /onUiStyleChange\(\(style\) => \{\s*for \(const id of \['setting-ui-style', 'dp-ui-style'\]\)/);
  assert.doesNotMatch(M, /btn-ui-style/, 'the phone page is Retro only');
});

test('the welcome guide leads with "Add your music" only while the library is empty', () => {
  assert.deepEqual(welcomeLead(0), { lead: 'add', add: 'Add your music', dismiss: 'Explore on my own' });
  assert.equal(welcomeLead(null).lead, 'add');
  assert.equal(welcomeLead(undefined).lead, 'add');
  assert.deepEqual(welcomeLead(506094), { lead: 'dismiss', add: 'Add more music', dismiss: 'Start listening' });
  assert.equal(welcomeLead('12').lead, 'dismiss');
});

test('the sign-in fields have names: label for= and aria-labelledby', () => {
  const S = read('js/auth.js');
  for (const id of ['auth-username', 'auth-password', 'auth-display-name']) {
    assert.match(S, new RegExp(`<label class="auth-field[^"]*" for="${id}"`), `${id}: label for`);
    assert.match(S, new RegExp(`id="${id}"[^>]*aria-labelledby="${id}-label"`), `${id}: aria-labelledby`);
    assert.match(S, new RegExp(`id="${id}-label"`), `${id}: label element`);
  }
  assert.match(S, /id="auth-password"[^>]*aria-describedby="auth-pw-rule"/);
});

test('an emptied or cleared search box never calls Library.showAll', () => {
  const S = read('js/search.js');
  assert.doesNotMatch(S, /Library\.showAll\(\)/);
  assert.equal((S.match(/_leaveSearch\(\);/g) || []).length, 3, 'empty Enter, emptied box, × clear');
  const L = read('js/library.js');
  assert.match(L, /setBrowseHeader\(_searchHeadText\(ctx\.q, ctx\.total\), \(\) => leaveSearch\(\), ctx\.total > 0\)/);
  const leave = L.slice(L.indexOf('async function leaveSearch('), L.indexOf('// Open the full results of a search'));
  assert.match(leave, /if \(!_searchCtx && !loading\) return false;/);
  assert.doesNotMatch(leave, /showAll\(/, 'leaving a search never falls back to All Tracks');
  // a loading search's flags come back only while it is still the view entry
  assert.match(L, /const _searchLoading = \(\) => !!\(_searchPending && _searchPending\.gen === _browseNavGen\);/);
});
