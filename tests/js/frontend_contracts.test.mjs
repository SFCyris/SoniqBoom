// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Contracts between frontend code and the other side of a boundary that no
// runtime test crosses: constants that must agree with the server, and event
// names that must reach their listeners.
// Run: node --test tests/js/frontend_contracts.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '../..');
const read = (p) => readFileSync(resolve(ROOT, p), 'utf8');

test('every Amiga VU re-ask comes before the server drops the queued pass', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const m = app.match(/const _AMIGA_VU_RETRY_MS = \[([\d,\s]+)\];/);
  assert.ok(m, '_AMIGA_VU_RETRY_MS not found');
  const ladder = m[1].split(',').map((x) => Number(x.trim())).filter((x) => x > 0);
  const py = read('soniqboom/api/stream.py');
  const f = py.match(/^_UADE_VU_POLL_FRESH_S\s*=\s*([\d.]+)/m);
  assert.ok(f, '_UADE_VU_POLL_FRESH_S not found');
  const freshMs = Number(f[1]) * 1000;
  // 5 s of margin for the fetch itself and timer drift (a throttled tab).
  assert.ok(Math.max(...ladder) <= freshMs - 5000,
    `a rung of ${Math.max(...ladder)} ms outlives the server's ${freshMs} ms window`);
  // …and the ladder still waits long enough for a pass queued behind others.
  const total = ladder.reduce((a, b) => a + b, 0);
  assert.ok(total >= 150000, `ladder spans only ${total} ms`);
});

test('every event the player emits can be listened to', () => {
  const src = read('soniqboom/frontend/js/player.js');
  const emitted = new Set([...src.matchAll(/\bemit\('([\w-]+)'/g)].map((m) => m[1]));
  assert.ok(emitted.size > 5);
  const lazy = /on\(evt, fn\)\s*\{[^}]*\(_handlers\[evt\] \|\|= \[\]\)\.push\(fn\)/.test(src);
  if (lazy) return;             // on() makes any event's list on first use
  const decl = src.match(/const _handlers = \{([^}]*)\}/);
  assert.ok(decl, '_handlers not found');
  const known = new Set([...decl[1].matchAll(/([\w-]+|'[\w-]+')\s*:/g)].map((m) => m[1].replace(/'/g, '')));
  const missing = [...emitted].filter((e) => !known.has(e));
  assert.deepEqual(missing, [], `Player.on() silently drops listeners for: ${missing.join(', ')}`);
});

test('the SID worker URL is versioned the same everywhere it is loaded', () => {
  const urls = ['soniqboom/frontend/js/app.js', 'soniqboom/frontend/js/sid-wasm-player.js']
    .map((p) => (read(p).match(/\/assets\/js\/vu-sid-worker\.js\?v=(\d+)/) || [])[1]);
  assert.ok(urls[0], 'worker URL not found');
  assert.equal(urls[0], urls[1]);
});

test('the phone lets an offer toast (Toast.action) take a tap', () => {
  // mobile.css lets taps fall through every toast; an offer's buttons
  // ("Resume the queue from …?") must still be reachable there.
  const utils = read('soniqboom/frontend/js/utils.js');
  const cls = (utils.match(/function _emitActionToast[\s\S]*?className = 'sb-toast ([\w-]+)'/) || [])[1];
  assert.ok(cls, 'Toast.action class not found');
  const css = read('soniqboom/frontend/css/mobile.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].map((m) => [m[1].trim(), m[2]]);
  const blocksAll = rules.some(([sel, body]) => /\.sb-toast-host \*/.test(sel) && /pointer-events:\s*none/.test(body));
  if (!blocksAll) return;       // nothing blocks taps on the phone
  const reopens = rules.some(([sel, body]) => sel.split(',').some((s) => s.includes(`.${cls}`))
    && /pointer-events:\s*auto\s*!important/.test(body));
  assert.ok(reopens, `mobile.css blocks taps on toasts but never re-enables them for .${cls}`);
});

test('a browser-rendered SID upload names the tune it holds for any subsong but the default', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const src = (app.match(/function _sidUploadQuery\([\s\S]*?\n}\n/) || [])[0];
  assert.ok(src, '_sidUploadQuery not found');
  const q = new Function(`${src}; return _sidUploadQuery;`)();
  assert.equal(q(0, 0), 'subsong=0');
  assert.equal(q(0, undefined), 'subsong=0', 'the default tune needs no tune=');
  assert.equal(q(undefined, 3), 'subsong=0');
  assert.equal(q(2, 3), 'subsong=2&tune=3');
  assert.equal(q(1, 1), 'subsong=1&tune=1', 'start song 2: wire 1 is tune 1');
  assert.equal(q(2, undefined), null, 'no tune → not sent (the server would 409 it)');
  assert.equal(q(2, 0), null);
  // Both uploads go through it, and both callers hand it the worker's tune.
  for (const fn of ['_uploadVUMR', '_uploadSidWav']) {
    const body = (app.match(new RegExp(`async function ${fn}\\(([^)]*)\\)[\\s\\S]*?\\n}\\n`)) || []);
    assert.ok(body[0], `${fn} not found`);
    assert.match(body[1], /\btune\b/, `${fn} takes the tune`);
    assert.match(body[0], /_sidUploadQuery\(subsong, tune\)/, `${fn} builds its query with _sidUploadQuery`);
    assert.doesNotMatch(body[0], /subsong=\$\{ss\}/, `${fn} still builds its own subsong= query`);
  }
  assert.match(app, /_uploadVUMR\(track\.id, track\.subsong \|\| 0, result\.vumr, result\.tune\)/);
  assert.match(app, /_uploadSidWav\(id, subsong, dur, wav, tune\)/);
  assert.match(app, /_uploadVUMR\(id, subsong, vumr, tune\)/);
  assert.match(read('soniqboom/frontend/js/player.js'), /emit\('sidwarm', \{[^}]*\btune: r\.tune\b/);
});

test('the SID worker reports the tune it rendered in both of its results', () => {
  const w = read('soniqboom/frontend/js/vu-sid-worker.js');
  const dones = [...w.matchAll(/postMessage\(\{ id, type: 'done'([^}]*)\}/g)].map((m) => m[1]);
  assert.equal(dones.length, 2);
  for (const d of dones) assert.match(d, /\btune\b/);
  // …the one it loaded (not a second guess).
  assert.equal((w.match(/load\(p, bytes\.length, tune\)/g) || []).length, 2);
  assert.match(read('soniqboom/frontend/js/sid-wasm-player.js'), /tune: Number\.isInteger\(tune\) \? tune : null/);
});

test('the spectrum label says why no per-voice meters come, in the server\'s words for it', () => {
  const app = read('soniqboom/frontend/js/app.js');
  const src = (app.match(/function _vuFallbackText\([\s\S]*?\n}\n/) || [])[0];
  assert.ok(src, '_vuFallbackText not found');
  const make = (isAdmin) => new Function('Auth', `${src}; return _vuFallbackText;`)({ isAdmin });
  const admin = make(true), user = make(false);
  assert.equal(admin('TFMX Pro', 'off'), 'Spectrum — per-voice meters are turned off (Settings → Renderers)');
  assert.equal(user('TFMX Pro', 'off'), 'Spectrum — per-voice meters are turned off on this server',
    'only an admin can reach Settings → Renderers');
  assert.equal(user('TFMX Pro', 'skipped'), 'Spectrum — per-voice meters not available for this tune');
  assert.equal(user('TFMX Pro', 'unsupported'), 'Spectrum — per-voice meters not available on this server');
  assert.equal(user('TFMX Pro', ''), 'Spectrum — per-voice meters not available for TFMX Pro');
  assert.equal(user('SID', null), 'Spectrum — per-voice meters not available for SID');
  // Every reason the server sends is one the label knows.
  const py = read('soniqboom/api/stream.py');
  const sent = new Set([...py.matchAll(/^def uade_vu_(?:unavailable|skipped)_reason[\s\S]*?(?=^def )/gm)]
    .flatMap((m) => [...m[0].matchAll(/return "(\w+)"/g)].map((r) => r[1])));
  assert.deepEqual([...sent].sort(), ['off', 'skipped', 'unsupported']);
  for (const r of sent) assert.notEqual(user('X', r), user('X', ''), `reason ${r} has its own words`);
  // The reason is carried from the header to both label paths.
  assert.match(app, /reason \? \{ unavailable: true, reason: /);
  assert.match(app, /return _end\(true, fetched\.reason\)/);
  assert.match(app, /_addFallbackLabel\(fmtLabel, willPoll, vuUnavailable \? fetched0\.reason : ''\)/);
});

test('the tune-part words the Modland guess tooltip and manual name are ones the server knows', () => {
  const py = read('soniqboom/core/scene_metadata.py');
  const m = py.match(/_PART_WORDS = frozenset\(\{([\s\S]*?)\}\)/);
  assert.ok(m, '_PART_WORDS not found');
  const known = new Set([...m[1].matchAll(/"(\w+)"/g)].map((x) => x[1]));
  const tip = (read('soniqboom/frontend/index.html')
    .match(/for="setting-modland-filename-game"[^\n]*data-tip="([^"]*)"/) || [])[1];
  assert.ok(tip, 'tooltip not found');
  const tipWords = (tip.match(/tune-part word such as ([^.]*)\./) || [])[1];
  assert.ok(tipWords, 'the tooltip names no tune-part words');
  const manual = read('docs/manual/index.html');
  const manWords = (manual.match(/usual tune-part word such as ([\s\S]*?)\(/) || [])[1];
  assert.ok(manWords, 'the manual names no tune-part words');
  for (const list of [tipWords, manWords.replace(/<\/?code>/g, '')]) {
    const words = list.split(/,|\bor\b/).map((w) => w.trim()).filter(Boolean);
    assert.ok(words.length >= 3);
    for (const w of words) assert.ok(known.has(w), `"${w}" is not in _PART_WORDS`);
  }
});

test('every control in the phone\'s mini player has a column of its own, so the progress bar stays in view', () => {
  // A control without a column wraps onto a second row and pushes the 2 px
  // progress bar below the mini player, under the tab bar (the cast button did).
  const html = read('soniqboom/frontend/mobile.html');
  const block = (html.match(/<div id="m-miniplayer"[^>]*>([\s\S]*?)<div id="m-mp-progress"/) || [])[1];
  assert.ok(block, '#m-miniplayer markup not found');
  let depth = 0, top = 0;
  for (const m of block.replace(/<!--[\s\S]*?-->/g, '').matchAll(/<(\/?)(div|button|span|svg|path|line)\b[^>]*?(\/?)>/g)) {
    if (m[1]) { depth--; continue; }
    if (depth === 0) top++;
    if (!m[3] && !['path', 'line'].includes(m[2])) depth++;
  }
  const css = read('soniqboom/frontend/css/mobile.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = (css.match(/\.m-miniplayer\s*\{([^}]*)\}/) || [])[1];
  assert.ok(rule, '.m-miniplayer rule not found');
  const cols = (rule.match(/grid-template-columns:\s*([^;]+);/) || [])[1];
  assert.ok(cols, 'grid-template-columns not found');
  const tracks = cols.replace(/minmax\([^)]*\)/g, 'M').trim().split(/\s+/);
  assert.equal(tracks.length, top, `${top} controls in the mini player, ${tracks.length} grid columns`);
  // …and the rows fit its height: padding-top + tallest control + row gap + bar.
  const px = (re) => Number((rule.match(re) || [])[1]);
  const h = Number((css.match(/--mini-h:\s*(\d+)px/) || [])[1]);
  const padTop = px(/padding:\s*(\d+)px/);
  const rowGap = px(/row-gap:\s*(\d+)px/);
  assert.ok(h && padTop >= 0 && rowGap >= 0, 'mini height / padding / row-gap not found');
  assert.ok(padTop + 48 + rowGap + 2 <= h, `the progress bar sits ${padTop + 48 + rowGap + 2 - h} px below the mini player`);
});
