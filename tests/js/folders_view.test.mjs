// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The desktop Folders view's names and way up (soniqboom/frontend/js/
// folders_view.js) and its wiring in library.js / app.js / index.html: a view
// entry (nav token, flags cleared at once), unreachable music folders get no
// request, Back goes up a level.
// Run: node --test tests/js/folders_view.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const F = await import(resolve(FE, 'js/folders_view.js'));

test('the folder above: up one name, never above the music folder', () => {
  assert.equal(F.folderParent('/m/C64Music/MUSICIANS/H', '/m/C64Music'), '/m/C64Music/MUSICIANS');
  assert.equal(F.folderParent('/m/C64Music/MUSICIANS', '/m/C64Music'), '/m/C64Music');
  assert.equal(F.folderParent('/m/C64Music', '/m/C64Music'), null);
  assert.equal(F.folderParent('/m/C64Music/', '/m/C64Music'), null);
  assert.equal(F.folderParent('/other/x', '/m/C64Music'), null);
  assert.equal(F.folderParent('/m/C64MusicX/a', '/m/C64Music'), null);     // (a sibling with the same start)
  // an archive's inner folder is a child of the archive's folder
  assert.equal(F.folderParent('/m/Mods/Mixed/pack.zip::inner', '/m/Mods'), '/m/Mods/Mixed');
  assert.equal(F.folderParent('/m/Mods/Mixed/pack.zip::inner/sub', '/m/Mods'), '/m/Mods/Mixed/pack.zip::inner');
  assert.equal(F.folderParent('smb://nas/music/A/B', 'smb://nas/music'), 'smb://nas/music/A');
  assert.equal(F.folderParent('smb://nas/music/A', 'smb://nas/music'), 'smb://nas/music');
});

test('the trail and the crumb', () => {
  const t = F.folderTrail('/m/C64Music/MUSICIANS/H/Hubbard_Rob', '/m/C64Music', 'HVSC');
  assert.deepEqual(t.map((x) => x.name), ['HVSC', 'MUSICIANS', 'H', 'Hubbard_Rob']);
  assert.equal(t[3].path, '/m/C64Music/MUSICIANS/H/Hubbard_Rob');
  assert.equal(F.foldersCrumb(t), 'Folders › … › MUSICIANS › H › Hubbard_Rob');
  assert.equal(F.foldersCrumb(t.slice(0, 2)), 'Folders › HVSC › MUSICIANS');
  assert.equal(F.foldersCrumb(F.folderTrail('/m/Mods/Mixed/pack.zip::inner', '/m/Mods', 'Mods')),
    'Folders › Mods › Mixed › pack.zip › inner');
  assert.deepEqual(F.folderTrail('/m/Mods', '/m/Mods', 'Mods').map((x) => x.name), ['Mods']);
});

test('what an unreachable music folder says', () => {
  assert.match(F.rootBlockedText({ name: 'NAS', kind: 'share' }), /NAS can’t be reached right now\. Its tracks are kept — reconnect the share/);
  assert.match(F.rootBlockedText({ name: 'USB', kind: 'drive' }), /reconnect the drive/);
  assert.match(F.rootBlockedText({ name: 'Modland', catalogue: 'modland' }), /Tunes played before still play/);
});

const L = readFileSync(resolve(FE, 'js/library.js'), 'utf8');
const view = L.slice(L.indexOf('async function showFolders('), L.indexOf('async function showFolder(path'));

test('showFolders is a view entry: nav token first, flags cleared before the first await, bails when stale', () => {
  assert.ok(view.length > 500, 'showFolders not found');
  const head = view.slice(0, view.indexOf('await'));
  assert.match(head, /const _gen = \+\+_browseNavGen;/);
  assert.match(head, /setGroupHeader\(/);                    // (clears _viewIsAllTracks / _currentBrowsePath / _viewReload)
  // (the awaits of the view itself: the listing request's own awaits are inside its closure)
  const own = view.slice(0, view.indexOf('const listing = (async')) + view.slice(view.indexOf('})();'));
  const awaits = (own.match(/await /g) || []).length;
  const bails = (view.match(/_gen !== _browseNavGen\) return/g) || []).length;
  assert.ok(bails >= awaits, `${awaits} awaits but ${bails} stale checks`);
});

test('an unreachable music folder gets no request (home row: a note; a level: a note before any fetch)', () => {
  const home = view.slice(0, view.indexOf('// The music folder\'s status'));
  assert.match(home, /r && r\.unavailable && !r\.catalogue\) \{ window\.Toast\?\.error\?\.\(rootBlockedText\(r\)\); return; \}/);
  const level = view.slice(view.indexOf('// The music folder\'s status'));
  const blocked = level.indexOf('if (r && r.unavailable && !r.catalogue)');
  const fetchAt = level.indexOf('fetch(`/api/fstree/children');
  assert.ok(blocked > 0 && fetchAt > blocked, 'the blocked check must come before the listing request');
});

test('Back goes up a level; levels record their reload; a folder of tracks opens in showFolder with the crumb', () => {
  assert.match(view, /const back = \(\) => \(parent \? showFolders\(parent, rootPath\) : showFolders\(\)\);/);
  assert.match(view, /_setViewReload\(`folders\\0\$\{path\}`/);
  assert.match(view, /showFolder\(path, false, \{ label: crumb, back \}\)/);
  assert.match(L, /const fo = opts\.quiet \? \(_currentFolderOpts \|\| \{\}\) : \{ label: opts\.label \|\| null, back: opts\.back \|\| null \};/);
});

test('entry points: the Library entry, the Folders heading key, the views map', () => {
  const H = readFileSync(resolve(FE, 'index.html'), 'utf8');
  const A = readFileSync(resolve(FE, 'js/app.js'), 'utf8');
  assert.match(H, /<li data-view="folders"[^>]*>Folders<\/li>/);
  assert.match(H, /id="btn-folders-open"[^>]*aria-label="Open the Folders view in the main pane"/);
  assert.match(A, /folders:\s+\(\) => \{ _cancelAncillaryFetches\(\); return Library\.showFolders\(\); \}/);
  assert.match(A, /getElementById\('btn-folders-open'\)[\s\S]{0,200}li\[data-view="folders"\]/);
});
