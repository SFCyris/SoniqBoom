// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The header search box's station matches (station_lookup.js): one request per
// typing pause, the stale one aborted, recent answers kept — and asked for only
// while the Stations view is open (search.js).
// Run: node --test tests/js/station_lookup.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createStationLookup, stationQueryKey } from '../../soniqboom/frontend/js/station_lookup.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const read = (p) => readFileSync(resolve(ROOT, p), 'utf8');

// A fake clock: timers fire only when the test advances it.
function clock() {
  let t = 0, id = 0;
  const timers = new Map();
  return {
    now: () => t,
    setTimer: (fn, ms) => { timers.set(++id, { at: t + ms, fn }); return id; },
    clearTimer: (h) => { timers.delete(h); },
    async advance(ms) {
      const end = t + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]); t = due[1].at; due[1].fn();
        for (let i = 0; i < 5; i++) await Promise.resolve();
      }
      t = end;
      for (let i = 0; i < 5; i++) await Promise.resolve();
    },
  };
}

function fixture(opts = {}) {
  const c = clock();
  const calls = [];
  const fetchRows = (text, signal) => {
    const call = { text, signal, aborted: false };
    calls.push(call);
    signal?.addEventListener('abort', () => { call.aborted = true; });
    if (opts.fail) return Promise.reject(new Error('HTTP 502'));
    if (opts.body !== undefined) return Promise.resolve(opts.body);   // a 200 whose body is no list
    return new Promise((res, rej) => {
      const done = () => res([{ sid: `rb:${text}`, name: `${text} FM` }]);
      if (opts.latency) c.setTimer(done, opts.latency); else done();
      signal?.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    });
  };
  const lk = createStationLookup({ fetchRows, now: c.now, setTimer: c.setTimer, clearTimer: c.clearTimer, ...opts.lookup });
  return { c, calls, lk };
}

async function typeText(fx, word, perKeyMs, seen) {
  for (let i = 1; i <= word.length; i++) {
    const text = word.slice(0, i);
    fx.lk.request(text, (rows) => seen.push([text, rows]));
    await fx.c.advance(perKeyMs);
  }
}

for (const perKey of [60, 180, 300]) {
  test(`typing "chiptune" at ${perKey} ms per key asks once, for the whole word`, async () => {
    const fx = fixture(); const seen = [];
    await typeText(fx, 'chiptune', perKey, seen);
    await fx.c.advance(1000);
    assert.deepEqual(fx.calls.map((x) => x.text), ['chiptune']);
    assert.deepEqual(seen.map(([t]) => t), ['chiptune']);
  });
}

test('a pause longer than the debounce asks for that text; the next keystroke aborts it', async () => {
  const fx = fixture({ latency: 2000 }); const seen = [];
  fx.lk.request('chip', (r) => seen.push(['chip', r]));
  await fx.c.advance(400);                          // asked, answer still in flight
  assert.equal(fx.calls.length, 1);
  fx.lk.request('chiptune', (r) => seen.push(['chiptune', r]));
  assert.equal(fx.calls[0].aborted, true);          // superseded → aborted
  await fx.c.advance(5000);
  assert.deepEqual(fx.calls.map((x) => x.text), ['chip', 'chiptune']);
  assert.deepEqual(seen.map(([t]) => t), ['chiptune']);   // the stale answer never shows
});

test('an answered text is answered again from memory, at once (cold vs warm)', async () => {
  const fx = fixture(); const seen = [];
  fx.lk.request('amiga', (r) => seen.push(r));       // cold: one request after the pause
  assert.equal(fx.calls.length, 0);
  await fx.c.advance(400);
  assert.equal(fx.calls.length, 1);
  let sync = null;
  fx.lk.request('  amiga ', (r) => { sync = r; });  // warm: same key, no request, no wait
  assert.ok(sync && sync[0].sid === 'rb:amiga');
  fx.lk.request('demoscene', () => {});            // a different text is cold again
  await fx.c.advance(400);
  assert.deepEqual(fx.calls.map((x) => x.text), ['amiga', 'demoscene']);
});

test('remembered answers expire, and only the newest texts are kept', async () => {
  const fx = fixture({ lookup: { ttlMs: 1000, maxEntries: 2 } });
  for (const q of ['aa', 'bb', 'cc']) { fx.lk.request(q, () => {}); await fx.c.advance(400); }
  assert.equal(fx.lk.size, 2);
  fx.lk.request('aa', () => {}); await fx.c.advance(400);   // evicted → asked again
  assert.equal(fx.calls.length, 4);
  await fx.c.advance(2000);
  fx.lk.request('cc', () => {}); await fx.c.advance(400);   // expired → asked again
  assert.equal(fx.calls.length, 5);
});

test('short text, cancel and failures', async () => {
  const fx = fixture(); const seen = [];
  assert.equal(fx.lk.request('c', () => seen.push(1)), false);
  fx.lk.request('chip', () => seen.push(2));
  fx.lk.cancel();                                   // Enter / Escape / box emptied
  await fx.c.advance(1000);
  assert.deepEqual(fx.calls, []); assert.deepEqual(seen, []);
  const bad = fixture({ fail: true });
  bad.lk.request('chip', () => seen.push(3)); await bad.c.advance(400);
  bad.lk.request('chip', () => seen.push(4)); await bad.c.advance(400);
  assert.equal(bad.calls.length, 2);               // a failure is not remembered
  assert.deepEqual(seen, []);
});

test('the key trims and folds runs of spaces only', () => {
  assert.equal(stationQueryKey('  Soma   FM '), 'Soma FM');
  assert.equal(stationQueryKey(null), '');
});

test('search.js asks for stations only in the Stations view, through the lookup', () => {
  const src = read('soniqboom/frontend/js/search.js');
  // one place builds the request, inside the lookup's fetchRows
  assert.equal(src.match(/\/api\/stations\/search/g).length, 1);
  assert.match(src, /createStationLookup\(\{\s*fetchRows: \(text, signal\) =>\s*fetch\(`\/api\/stations\/search/);
  const fn = src.slice(src.indexOf('function _lookupStations('), src.indexOf('function _cancelPreviewSearch'));
  assert.match(fn, /if \(!_inStationsView\(\)\) \{ _stationLookup\.cancel\(\); return; \}/);
  // the track preview no longer starts a station request of its own
  const qs = src.slice(src.indexOf('async function _quickSearch('), src.indexOf('// ── Event handlers'));
  assert.doesNotMatch(qs, /stations/);
  // Enter / Escape / blur drop a pending station ask too
  const cancel = src.slice(src.indexOf('function _cancelPreviewSearch('), src.indexOf('async function _quickSearch('));
  assert.match(cancel, /_stationLookup\.cancel\(\);/);
  // an empty (cached) station answer before the tracks never flashes "No matches"
  assert.match(fn, /if \(!rows\.length && _lastTracksText !== text\) return;\s*_showPreview\(/);
});

test('a failed request reports onError (latest ask only); an aborted one reports nothing', async () => {
  const bad = fixture({ fail: true }); const errs = [];
  bad.lk.request('chip', () => {}, () => errs.push('chip'));
  await bad.c.advance(400);
  assert.deepEqual(errs, ['chip']);
  const slow = fixture({ latency: 2000 }); const errs2 = [];
  slow.lk.request('chip', () => {}, () => errs2.push('chip'));
  await slow.c.advance(400);
  slow.lk.request('chiptune', () => {}, () => errs2.push('chiptune'));   // aborts 'chip'
  await slow.c.advance(5000);
  assert.deepEqual(errs2, []);
});

test('the mobile radio search uses the same lookup, drops stale answers and says a failure', () => {
  const src = read('soniqboom/frontend/js/mobile/views/radio.js');
  assert.match(src, /import \{ createStationLookup, stationQueryKey \} from '\.\.\/\.\.\/station_lookup\.js';/);
  assert.equal(src.match(/\/api\/stations\/search/g).length, 1);
  const fn = src.slice(src.indexOf('function search()'), src.indexOf('async function load()'));
  assert.match(fn, /const gen = \+\+loadGen;/);
  assert.match(fn, /\(rows\) => \{ if \(gen !== loadGen\) return;[^}]*render\(rows\); \}/);
  assert.match(fn, /\(\) => \{ if \(gen !== loadGen\) return;[^}]*showError\(search\); \}/);
  const load = src.slice(src.indexOf('async function load()'), src.indexOf('function render('));
  assert.match(load, /if \(gen !== loadGen\) return;\s*render\(/);
  assert.match(load, /showError\(load\)/);
  assert.doesNotMatch(src, /render\(\[\]\)/);          // a failure is never shown as an empty list
});

test('a 200 answer that is no station list reports onError (no "Loading…" left up) and is not kept', async () => {
  for (const body of [{ detail: 'mirror down' }, null, 'oops']) {
    const fx = fixture({ body }); const rows = [], errs = [];
    fx.lk.request('chip', (r) => rows.push(r), () => errs.push('chip'));
    await fx.c.advance(400);
    assert.deepEqual(rows, []);
    assert.deepEqual(errs, ['chip']);
    assert.equal(fx.lk.size, 0);
  }
});

test('the mobile radio error is said in a live region that is always on the page', () => {
  const src = read('soniqboom/frontend/js/mobile/views/radio.js');
  assert.match(src, /<div class="sr-only" id="radio-status" role="status" aria-live="polite"><\/div>/);
  const fn = src.slice(src.indexOf('function showError('), src.indexOf('function search()'));
  assert.match(fn, /announce\('Couldn’t load stations\./);
});
