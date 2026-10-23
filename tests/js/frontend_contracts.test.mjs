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

test('the queue re-anchors after an availability change in Mark mode too', () => {
  // With duplicates hidden, a copy stands in for an offline primary at its own
  // list position (core/availability.primary_swap) in Mark mode as well as in
  // Hide, so an ordered queue resumes after its last list row, not at the old
  // offset; only Show → Show leaves list positions where they were — unless
  // "Tracks that can't be played: Hide" is (or was) on, whose lists change
  // length whenever a track becomes one that can't be played.
  const src = read('soniqboom/frontend/js/offline.js');
  const m = src.match(/if \(([^\n{}]*)\) \{\s*try \{ Player\.reanchorOrdered/);
  assert.ok(m, 'the reanchorOrdered call (and its condition) not found');
  const u = src.match(/const unplayHide = ([^\n;]*);/);
  assert.ok(u, 'the unplayHide test not found');
  const hide = new Function('state', 'prevUnplayable', `return (${u[1]});`);
  const cond = new Function('j', 'prevMode', 'unplayHide', `return (${m[1]});`);
  for (const [now, before] of [['mark', 'mark'], ['mark', null], ['hide', 'mark'], ['mark', 'hide'],
                               ['hide', 'hide']]) {
    const uh = hide({ unplayable: now }, before);
    assert.equal(uh, now === 'hide' || before === 'hide', `${before} → ${now}`);
    for (const prev of ['mark', 'hide', 'show']) {
      for (const mode of ['mark', 'hide', 'show']) {
        assert.equal(cond({ mode }, prev, uh), uh || !(prev === 'show' && mode === 'show'),
                     `${prev} → ${mode}, can't be played ${before} → ${now}`);
      }
    }
  }
});

test('the sidebar browse counts are fetched at page load and after a scan (GitHub #26)', () => {
  const lib = read('soniqboom/frontend/js/library.js');
  assert.match(lib, /refreshBadges: _refreshBadges,/);
  assert.match(lib, /function _refreshBadges\(\) \{\s*return _refreshNavCounts\(\);/);
  assert.match(lib, /setTimeout\(\(\) => _refreshNavCounts\(\), \d+\);/);
  // one small counts request, not the lists (those load when their view opens)
  assert.match(lib, /API\('\/library\/counts'\)/);
  const keys = lib.match(/const _NAV_COUNT_KEYS = \[([^\]]*)\]/);
  assert.ok(keys, '_NAV_COUNT_KEYS not found');
  for (const v of ['artists', 'album_artists', 'albums', 'genres', 'years']) {
    assert.ok(keys[1].includes(`'${v}'`), `${v} is not refreshed at load`);
  }
  // every key the page reads is one the server sends
  const py = read('soniqboom/api/library.py');
  const body = py.slice(py.indexOf('async def library_counts'), py.indexOf('@router.get("/artists")'));
  for (const k of ['tracks', 'visible', 'scene_groups', ...keys[1].match(/'(\w+)'/g).map((x) => x.slice(1, -1))]) {
    assert.ok(body.includes(`"${k}":`), `GET /library/counts has no "${k}"`);
  }
  // the "[No Artist]" bucket rows are not artists, in the views or at load
  assert.match(lib, /_updateNavBadge\('artists', _navCount\(artists\)\)/);
  assert.match(lib, /_updateNavBadge\('album_artists', _navCount\(albumArtists\)\)/);
  assert.match(lib, /const _navCount = \(rows\) => rows\.filter\(r => !r\.label\)\.length;/);
});

test("the Track Info tune list's play button pauses the loaded tune (GitHub #28)", () => {
  const ti = read('soniqboom/frontend/js/trackinfo.js');
  assert.match(ti, /if \(act === 'play' && wire === _currentSubWire\(\) && !Player\.audio\?\.error\) \{\s*Player\.playPause\(\);/);
  assert.match(ti, /Player\.on\?\.\('statechange', \(\) => _highlightPlayingSubsong\(\)\);/);
  // Space on a row is handled there only — not also by the page's play/pause
  assert.match(ti, /e\.preventDefault\(\);\s*e\.stopPropagation\(\);[^\n]*\n\s*_subAction\('play'/);
});

test('every web-player stream request names its page (pg), never from sessionStorage', () => {
  const src = read('soniqboom/frontend/js/player.js');
  const body = src.match(/function _streamUrl\(id, params = \{\}\) \{([\s\S]*?)\n  \}/);
  assert.ok(body, '_streamUrl not found');
  assert.match(body[1], /qs\.set\('pg', PREWARM_PAGE_ID\)/);
  // the gapless preload of the next track is NOT a play start of this page:
  // it carries no pg (the server would retire the page's prewarms / render)
  const pre = src.match(/function _maybeGaplessPreload\([\s\S]*?\n  \}/);
  assert.ok(pre, '_maybeGaplessPreload not found');
  assert.match(pre[0], /fetch\(`\/api\/stream\/\$\{next\.id\}/);
  assert.doesNotMatch(pre[0], /pg=/);
  const def = src.match(/const PREWARM_PAGE_ID = \(\(\) => \{([\s\S]*?)\}\)\(\);/);
  assert.ok(def, 'PREWARM_PAGE_ID not found');
  assert.doesNotMatch(def[1], /sessionStorage|localStorage/);
  // the server accepts what the page sends
  const py = read('soniqboom/api/stream.py');
  const re = py.match(/_PAGE_ID_RE = re\.compile\(r"([^"]+)"\)/);
  assert.ok(re, '_PAGE_ID_RE not found');
  const ok = new RegExp(`^(?:${re[1]})$`);
  assert.ok(ok.test('0123456789abcdef0123456789abcdef'));                 // randomUUID form
  assert.ok(ok.test((Math.random().toString(36).slice(2) + Date.now().toString(36)).slice(0, 32)));
});

test('the offline match ignores the query, so a page id never splits the download cache', () => {
  const sw = read('soniqboom/frontend/sw.js');
  assert.match(sw, /c\.match\(req, \{ ignoreSearch: true \}\)/);
});

test('a share check re-reads the counts once: remote_new_tracks skips them at the end frame\'s generation', () => {
  const app = read('soniqboom/frontend/js/app.js');
  assert.match(app, /if \(!msg\.running && !msg\.snapshot\) \{[\s\S]{0,300}_scanEndGen = msg\.lib_gen;/);
  assert.match(app, /msg\.lib_gen === _scanEndGen/);
  assert.match(app, /if \(reread\) \{ try \{ Library\.refreshBadges\?\.\(\); \} catch \{\} \}/);
});

test('a page going away tells the server (page-gone beacon with its page id)', () => {
  const src = read('soniqboom/frontend/js/player.js');
  assert.match(src, /addEventListener\?\.\('pagehide'[\s\S]{0,200}sendBeacon\?\.\(`\/api\/stream\/page-gone\?pg=\$\{PREWARM_PAGE_ID\}`\)/);
  assert.match(read('soniqboom/api/stream.py'), /@router\.post\("\/page-gone"\)/);
});

test('a hovered row may warm again once the server-side hover prewarm has lapsed', () => {
  const lib = read('soniqboom/frontend/js/library.js');
  assert.match(lib, /const _hoverPrefetched = new Map\(\);/);
  const ms = Number((lib.match(/const _HOVER_REWARM_MS = (\d+);/) || [])[1]);
  assert.ok(ms > 0 && ms <= 10000, 'memo must not outlive the 10 s server lapse');
  assert.match(lib, /function _hoverWarmed\(id\)[\s\S]*?Date\.now\(\) - at < _HOVER_REWARM_MS/);
  assert.doesNotMatch(lib, /_hoverPrefetched\.has\(/);
});

test('"Tracks that can\'t be played: Hide" rebuilds the open list and the phone list, and a broken mark re-reads them', () => {
  // Desktop: the open view is re-read in place when no list can change length,
  // rebuilt when either Hide is (or was) on, and left alone only when nothing
  // is flagged or left out (offline Show → Show with can't-be-played Mark).
  const lib = read('soniqboom/frontend/js/library.js');
  const fn = lib.slice(lib.indexOf('function refreshAvailability('));
  const uh = fn.match(/const unplayHide = ([^;\n]*);/);
  const lc = fn.match(/const lengthsChange = ([^;\n]*);/);
  const early = fn.match(/if \(([^\n{}]*)\) return;/);
  assert.ok(uh && lc && early, 'refreshAvailability conditions not found');
  const unplayHide = new Function('unplayable', 'prevUnplayable', `return (${uh[1]});`);
  const lengths = new Function('mode', 'prevMode', 'unplayHide', `return (${lc[1]});`);
  const skip = new Function('mode', 'prevMode', 'unplayHide', `return (${early[1]});`);
  const M = ['mark', 'hide', 'show'];
  for (const prevMode of M) for (const mode of M) for (const u of ['mark', 'hide']) for (const pu of ['mark', 'hide']) {
    const h = unplayHide(u, pu);
    assert.equal(h, u === 'hide' || pu === 'hide');
    assert.equal(lengths(mode, prevMode, h), mode === 'hide' || prevMode === 'hide' || h,
                 `${prevMode}→${mode}, ${pu}→${u}`);
    assert.equal(skip(mode, prevMode, h), mode === 'show' && prevMode === 'show' && !h,
                 `${prevMode}→${mode}, ${pu}→${u}`);
  }
  // The phone's library view re-reads its list under the same rule.
  const mob = read('soniqboom/frontend/js/mobile/views/library.js');
  const at = mob.indexOf("addEventListener('soniqboom:availability-changed'");
  const mc = mob.slice(at).match(/if \(([\s\S]*?)\) \{ render\(\); return; \}/);
  assert.ok(mc, 'the phone list condition not found');
  const rerender = new Function('d', `return (${mc[1]});`);
  for (const prevMode of M) for (const mode of M) for (const u of ['mark', 'hide']) for (const pu of ['mark', 'hide']) {
    assert.equal(rerender({ mode, prevMode, unplayable: u, prevUnplayable: pu }),
                 mode === 'hide' || prevMode === 'hide' || u === 'hide' || pu === 'hide');
  }
  // offline.js sends both values with the event (and seeds the setting from ui-config).
  const off = read('soniqboom/frontend/js/offline.js');
  assert.match(off, /unplayable: state\.unplayable, prevUnplayable: prevUnplayable \|\| state\.unplayable/);
  assert.match(read('soniqboom/frontend/js/app.js'), /unplayable: \(window\.__sbConfig \|\| \{\}\)\.unplayable_tracks/);
  // health.js: a broken mark (or its clearing) re-reads the lists at once while Hide is on.
  const hj = read('soniqboom/frontend/js/health.js');
  const hc = hj.match(/if \(hiding && \(([^\n]*)\)\) _refreshLists\(\);/);
  assert.ok(hc, 'the health.js re-read condition not found');
  const changes = new Function('state', 'before', `return (${hc[1]});`);
  assert.equal(changes('broken', { health: null }), true);
  assert.equal(changes(null, { health: 'broken' }), true);
  assert.equal(changes('glitched', { health: null }), false);        // glitched plays: no list change
  assert.equal(changes('broken', { health: 'broken' }), false);     // a new note only
  assert.equal(changes('glitched', { health: 'broken' }), true);
});
