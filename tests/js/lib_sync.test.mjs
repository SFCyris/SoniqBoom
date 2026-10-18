// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The WebSocket connect snapshot re-reads the library only when it changed
// since the page last synced (lib_sync.js), and the app routes every
// ``running:false`` snapshot through that check instead of the scan-done path.
// Run: node --test tests/js/lib_sync.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createLibSync, snapshotPlan, watchBootLoads } from '../../soniqboom/frontend/js/lib_sync.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const read = (p) => readFileSync(resolve(ROOT, p), 'utf8');

test('the first snapshot adopts the token: the page is loading its data now', () => {
  const s = createLibSync();
  assert.equal(s.gen, null);
  assert.equal(s.snapshot('b1.0.(5,None,None)'), 'first');
  assert.equal(s.gen, 'b1.0.(5,None,None)');
});

test('a reconnect with the same token re-reads nothing; a new one does, once', () => {
  const s = createLibSync();
  s.snapshot('A');
  assert.equal(s.snapshot('A'), 'same');
  assert.equal(s.snapshot('B'), 'changed');
  assert.equal(s.snapshot('B'), 'same');           // remembered
});

test('a scan completion is a sync point', () => {
  const s = createLibSync();
  s.snapshot('A');
  s.note('C');                                     // a scan finished while connected (the page re-read)
  assert.equal(s.snapshot('C'), 'same');
  assert.equal(s.snapshot('D'), 'changed');        // something changed while disconnected
});

test('a counts reply is not a sync point: it re-reads the badges, not the open view', () => {
  const lib = read('soniqboom/frontend/js/library.js');
  assert.doesNotMatch(lib, /LibSync/);
  // the only notes in app.js: completion frames and the library-change event
  // (both re-read the open view)
  const app = read('soniqboom/frontend/js/app.js');
  assert.deepEqual([...app.matchAll(/LibSync\.note\(([^)]*)\)/g)].map((m) => m[1]),
                   ['msg.lib_gen', 'msg.lib_gen']);
  // so a counts-only refresh between two snapshots can't hide a change
  const s = createLibSync();
  s.snapshot('A');                                 // connected
  // (a remote-new-tracks toast refreshed only the badges: the page notes nothing)
  assert.equal(s.snapshot('B'), 'changed');
});

test('snapshotPlan: nothing changed → nothing re-read; changed / unknown → re-read', () => {
  assert.deepEqual(snapshotPlan('first'), { reread: false, showDone: false, pollFacet: false });
  assert.deepEqual(snapshotPlan('same'), { reread: false, showDone: false, pollFacet: false });
  assert.deepEqual(snapshotPlan('changed'), { reread: true, showDone: false, pollFacet: false });
  assert.deepEqual(snapshotPlan('unknown'), { reread: true, showDone: false, pollFacet: false });
});

test('snapshotPlan: a scan the page showed ended while disconnected', () => {
  // first snapshot was a running scan (no token synced yet): still re-read
  assert.deepEqual(snapshotPlan('first', { wasScanning: true }), { reread: true, showDone: true, pollFacet: true });
  assert.deepEqual(snapshotPlan('changed', { wasScanning: true }), { reread: true, showDone: true, pollFacet: true });
  // it changed nothing: badge says Done, nothing re-read
  assert.deepEqual(snapshotPlan('same', { wasScanning: true }), { reread: false, showDone: true, pollFacet: false });
  // now in its embedding phase: the badge keeps going
  assert.deepEqual(snapshotPlan('first', { wasScanning: true, embedding: true }), { reread: true, showDone: false, pollFacet: false });
  assert.deepEqual(snapshotPlan('same', { embedding: true }), { reread: false, showDone: false, pollFacet: false });
});

test('no token is unknown (re-read), and never overwrites a known one', () => {
  const s = createLibSync();
  s.note('A');
  for (const t of [undefined, null, '', 5]) {
    assert.equal(s.snapshot(t), 'unknown');
    s.note(t);
  }
  assert.equal(s.gen, 'A');
});

test('app.js: an idle / embedding snapshot never takes the scan-done path', () => {
  const app = read('soniqboom/frontend/js/app.js');
  // the snapshot check comes first in the scan_progress handler
  assert.match(app, /if \(msg\.event === 'scan_progress'\) \{[^]*?if \(msg\.snapshot && !msg\.running\) \{ _onScanSnapshot\(msg\); return; \}/);
  const fn = app.slice(app.indexOf('function _onScanSnapshot('), app.indexOf('let _stuckBadgeTimer'));
  assert.match(fn, /snapshotPlan\(LibSync\.snapshot\(msg\.lib_gen\), \{ wasScanning, embedding: !!msg\.embedding \}\)/);
  // every re-read sits behind plan.reread
  const idle = fn.slice(fn.indexOf('if (plan.showDone)'));
  const gate = idle.indexOf('if (!plan.reread) return;');
  assert.ok(gate > 0);
  // a changed generation re-reads everything on screen, in place (the counts,
  // the open view whatever it is, the tree): _rereadLibrary
  assert.ok(idle.indexOf('_rereadLibrary()') > gate, 're-read before the generation check');
  const rr = app.slice(app.indexOf('function _rereadLibrary('));
  for (const call of ['Library.refreshBadges()', "new CustomEvent('soniqboom:library-changed')", 'FolderTree.refresh()']) {
    assert.ok(rr.slice(0, 500).includes(call), call);
  }
  const emb = fn.slice(fn.indexOf('if (msg.embedding)'), fn.indexOf('if (plan.showDone)'));
  assert.match(emb, /if \(plan\.reread\) _rereadLibrary\(\);/);      // everything, in place
  assert.match(app, /const moved = msg\.silent \? msg\.lib_gen !== LibSync\.gen : LibSync\.note\(msg\.lib_gen\);/);
});

test('a post-scan / library-changed All-Tracks refresh keeps the scroll position', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const fn = app.slice(app.indexOf('function _refreshMainAfterScan('), app.indexOf('function _onScanSnapshot('));
  assert.match(fn, /Library\.refreshAllTracksInPlace\(\)/);
  const lc = app.slice(app.indexOf("document.addEventListener('soniqboom:library-changed'"));
  assert.match(lc.slice(0, 600), /Library\.refreshAllTracksInPlace\(\)/);
  const lib = read('soniqboom/frontend/js/library.js');
  assert.match(lib, /if \(quiet\) _viewIsAllTracks = true;\s*\/\/ \(restoreFullHeader cleared it; we never left\)\s*else _showSkeletonRows\(\);/);
});

test('the server marks the connect snapshot and stamps completions', () => {
  const py = read('soniqboom/api/library.py');
  assert.match(py, /"snapshot": True, "lib_gen": library_generation\(\)/);
  assert.match(py, /data = _with_lib_gen\(data\)/);
});


// ── The first loads vs the first snapshot (a change between them) ───────────

test('the first snapshot re-reads when a first load was served at another generation', () => {
  const s = createLibSync();
  assert.equal(s.booting, true);
  s.served('G0');                                  // All Tracks served before a change
  s.served('G1');                                  // counts served after it
  assert.equal(s.snapshot('G1'), 'changed');       // → re-read
  assert.equal(s.booting, false);
  const t = createLibSync();
  t.served('G1'); t.served('G1');
  assert.equal(t.snapshot('G1'), 'first');         // all at the snapshot's: nothing to re-read
  const u = createLibSync();
  assert.equal(u.snapshot('G1'), 'first');         // no header seen (an older server): as before
});

test('a first load answering after the first snapshot at another generation re-reads once', () => {
  const s = createLibSync();
  assert.equal(s.snapshot('G1'), 'first');
  assert.equal(s.served('G1'), false);             // same generation: fine
  assert.equal(s.served('G0'), true);              // served before a change: re-read …
  assert.equal(s.served('G0'), false);             // … once
  assert.equal(s.served(null), false);
});

test('note() says whether the generation moved; a frame ends the first-load tracking', () => {
  const s = createLibSync();
  s.served('G0');
  assert.equal(s.note('G1'), true);
  assert.equal(s.booting, false);
  assert.equal(s.note('G1'), false);               // a silent scan's end that changed nothing
  assert.equal(s.note('G2'), true);
  assert.equal(s.note(undefined), true);           // no token: treat as changed
  assert.equal(s.gen, 'G2');
});

function fakeTarget(answers) {
  const calls = [];
  const target = {
    fetch(url) {
      calls.push(url);
      const gen = answers[url];
      return Promise.resolve({ headers: { get: (h) => (h === 'X-SB-Lib-Gen' ? gen ?? null : null) } });
    },
  };
  return { target, calls };
}

test('watchBootLoads reports the first loads, re-reads once for a late one, then unwraps', async () => {
  const s = createLibSync();
  const { target } = fakeTarget({ '/api/tracks?x=1': 'G0', '/api/library/counts': 'G1', '/api/fstree/children': 'G0' });
  const orig = target.fetch;
  let stale = 0;
  watchBootLoads(s, () => { stale++; }, target);
  assert.notEqual(target.fetch, orig);
  await target.fetch('/api/library/counts');       // answered before the snapshot
  const late = target.fetch('/api/tracks?x=1');    // made before the snapshot …
  assert.equal(s.snapshot('G1'), 'first');         // (counts at G1 = the snapshot's)
  await late;                                      // … answered after it, at G0
  assert.equal(stale, 1);
  assert.equal(target.fetch, orig);                // nothing tracked any more
  await target.fetch('/api/fstree/children');      // after the sync point: not tracked
  assert.equal(stale, 1);
});

test('watchBootLoads ignores non-API requests and never breaks a failing one', async () => {
  const s = createLibSync();
  const target = { fetch: (u) => (u === '/boom/api/x' ? Promise.reject(new Error('net'))
                                                       : Promise.resolve({ headers: { get: () => 'G9' } })) };
  watchBootLoads(s, () => {}, target);
  await target.fetch('/assets/js/app.js');
  await assert.rejects(target.fetch('/boom/api/x'));
  assert.equal(s.snapshot('G1'), 'first');         // nothing from /assets or a failure
});

test('app.js: first loads tracked from the start, a silent end that changed nothing re-reads nothing', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const watch = app.indexOf('watchBootLoads(LibSync, () => _rereadLibrary());');
  assert.ok(watch > 0 && watch < app.indexOf('await Auth.boot();'));
  assert.match(app, /if \(msg\.silent && !msg\.embedding && !moved\) \{ FolderTree\.setScanActive\(false\); return; \}/);
  // a silent end with a change refreshes All Tracks in place only
  assert.match(app, /_refreshMainAfterScan\(finished, msg\.silent \? 'in-place' : true\);/);
  const fn = app.slice(app.indexOf('function _refreshMainAfterScan('), app.indexOf('function _doneLabel('));
  assert.match(fn, /else if \(allowAllTracksRefresh !== 'in-place'\) Library\.showAll\(\);/);
  // the boot one-shot never spins the tree for a silent scan
  assert.match(app, /if \(\(s\.running && !s\.silent\) \|\| s\.embedding\) FolderTree\.setScanActive\(true\);/);
});

test('"Done" without a count when the scan reported none (a page that outlived a restart)', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const src = app.slice(app.indexOf('function _doneLabel('), app.indexOf('function _rereadLibrary('));
  const _doneLabel = new Function(`${src}; return _doneLabel;`)();
  assert.equal(_doneLabel(0), 'Done');
  assert.equal(_doneLabel(undefined), 'Done');
  assert.equal(_doneLabel(12), 'Done — 12 tracks');
  assert.doesNotMatch(app, /`Done \\u2014 \$\{msg\.processed\} tracks`/);
  assert.doesNotMatch(app, /`Done — \$\{st\.processed/);
});


test('a silent end is not a sync point: the next snapshot still compares with the last full sync', () => {
  const s = createLibSync();
  s.snapshot('G1');                                // synced
  // a silent end frame at G2 refreshes the folder in place but notes nothing
  const moved = 'G2' !== s.gen;
  assert.equal(moved, true);
  assert.equal(s.gen, 'G1');
  assert.equal(s.snapshot('G2'), 'changed');       // the reconnect re-reads everything
});

test('first loads at three generations still decide the first snapshot', () => {
  const s = createLibSync();
  for (const g of ['G0', 'G1', 'G2', 'G2', 'G1']) s.served(g);
  assert.equal(s.snapshot('G2'), 'changed');
});


test('app.js: a "service restart" close (1012) reconnects at the base delay', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const oc = app.slice(app.indexOf('ws.onclose = async (ev) => {'));
  const i1012 = oc.indexOf('ev.code === 1012');
  assert.ok(i1012 > 0 && i1012 < oc.indexOf('const next = Math.min(30000, last * 2);'));
  assert.match(oc.slice(i1012, i1012 + 200), /window\.__sbWsBackoff = 1000;\s*setTimeout\(connectWS, 1000\);\s*return;/);
});

test('auth.js: a restarting / unreachable server never shows the setup hint or an enabled Sign in', () => {
  const auth = read('soniqboom/frontend/js/auth.js');
  const fn = auth.slice(auth.indexOf('function _showUnavailable('), auth.indexOf('// ── Boot'));
  assert.match(fn, /submit\.disabled = true;/);
  assert.match(fn, /querySelector\('#auth-bootstrap-hint'\)\.hidden = true;/);
  const boot = auth.slice(auth.indexOf('async function boot()'));
  assert.match(boot, /me\.status === 503 \|\| st\.status === 503/);
  assert.match(boot, /\[502, 504\]\.includes\(me\.status\)/);
  const re = auth.slice(auth.indexOf('function _promptReauth()'));
  assert.match(re, /if \(st\.status === 503\) restarting = true;/);
  assert.match(re, /if \(restarting\) _showUnavailable\(true\);/);
});
