// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The CSDb section of a SID's Track Info (js/csdb_view.js) and the play /
// Track Info signal for a game's box cover (js/art_intent.js): what the
// section shows for a /api/scene/csdb answer (and that it shows nothing for
// any other), that it escapes what CSDb sends and only links csdb.dk, where
// trackinfo.js asks for it, and that the cover signal goes once per track.
// Run: node --test tests/js/csdb_view.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FE = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FE, p), 'utf8');
const { csdbSectionHtml, csdbHint, releaseMeta, CSDB_SHOWN } = await import(resolve(FE, 'js/csdb_view.js'));
const { artIntent, watchPlays, _resetArtIntent } = await import(resolve(FE, 'js/art_intent.js'));

// The shape core/csdb.py ``_public`` answers (SID 20901 of tests/data/csdb).
const ANSWER = {
  found: true, id: 20901, name: 'Best', author: 'Anders Elmén (Moon)', released: '1990 Flash Inc.',
  hvsc_path: '/MUSICIANS/M/Moon/Best.sid', url: 'https://csdb.dk/sid/?id=20901',
  top_rating: 8.5, screenshot: '/api/scene/csdb/shot/11589.png', screenshot_release: 11589,
  releases: [
    { id: 11589, name: 'Mist II', type: 'C64 Demo', year: 1990, groups: ['Vision'],
      event: 'Censor Halloween Party 1990', compo: 'C64 Demo', place: 2, rating: 8.5,
      group_rating: { name: 'Vision', rating: 8.43 }, url: 'https://csdb.dk/release/?id=11589' },
    { id: 4212, name: 'FHI Classics', type: 'C64 Demo', year: 1992, groups: ['Flash Inc.'],
      event: '', compo: '', place: null, rating: null, url: 'https://csdb.dk/release/?id=4212' },
  ],
};

test('a found answer: entry, screenshot, best rating, the releases with their details', () => {
  const h = csdbSectionHtml(ANSWER);
  assert.match(h, /^<div class="ti-scene-block ti-csdb"/);
  assert.match(h, /<div class="ti-scene-h">CSDb<\/div>/);
  assert.match(h, /<img class="ti-csdb-shot" src="\/api\/scene\/csdb\/shot\/11589\.png" alt="Screenshot of Mist II" width="320" height="200"/);
  assert.ok(!/loading="lazy"/.test(h), 'no native lazy loading on a JS-set image');
  assert.match(h, /Anders Elmén \(Moon\) · 1990 Flash Inc\./);
  assert.match(h, /★ 8\.5 <span class="ti-scene-disco-m">best release rating<\/span>/);
  assert.match(h, /Used in 2 releases/);
  assert.match(h, /<a href="https:\/\/csdb\.dk\/release\/\?id=11589" target="_blank" rel="noopener">Mist II<\/a> <span class="ti-scene-disco-m">Vision \(group ★ 8\.43\) · 1990 · C64 Demo · #2 C64 Demo at Censor Halloween Party 1990 · ★ 8\.5<\/span>/);
  assert.match(h, /FHI Classics<\/a> <span class="ti-scene-disco-m">Flash Inc\. · 1992 · C64 Demo<\/span>/);
  assert.match(h, /class="ti-scene-chip">CSDb<\/a>/);
});

test('no section for anything but a found answer', () => {
  for (const x of [null, undefined, {}, { found: false, reason: 'off' }, { found: 'yes' }]) {
    assert.equal(csdbSectionHtml(x), '');
  }
});

test('what CSDb sends is escaped, and only csdb.dk / the screenshot route are linked', () => {
  const h = csdbSectionHtml({
    ...ANSWER, name: '<img src=x onerror=alert(1)>', url: 'javascript:alert(1)',
    screenshot: 'https://evil.invalid/x.png',
    releases: [{ id: 1, name: '"><script>', groups: ['<b>'], url: 'https://evil.invalid/' }],
  });
  assert.ok(!h.includes('<script>') && !h.includes('<img src=x') && !h.includes('<b>'));
  assert.ok(!h.includes('javascript:') && !h.includes('evil.invalid'));
  assert.ok(!h.includes('ti-csdb-shot'), 'an unexpected screenshot URL is left out');
});

test('a long list stops at the shown count and links the rest on CSDb', () => {
  const releases = Array.from({ length: CSDB_SHOWN + 3 }, (_, i) => ({ id: i + 1, name: `R${i}`, url: `https://csdb.dk/release/?id=${i + 1}` }));
  const h = csdbSectionHtml({ ...ANSWER, releases });
  assert.equal((h.match(/<li>/g) || []).length, CSDB_SHOWN);
  assert.match(h, /3 more on CSDb ▸<\/a>/);
});

test('release lines: placing without a party, party without a placing', () => {
  assert.deepEqual(releaseMeta({ place: 3, compo: 'Music' }), ['#3 Music']);
  assert.deepEqual(releaseMeta({ event: 'X 2001' }), ['at X 2001']);
  assert.deepEqual(releaseMeta({ rating: 9 }), ['★ 9']);
});

test('the hint: the CSDb SID link of the Scene tab’s matched production', () => {
  assert.equal(csdbHint({ release: { links: [{ url: 'https://demozoo.org/x' }, { url: 'https://csdb.dk/sid/?id=20901' }] } }), 20901);
  assert.equal(csdbHint({ release: { links: [{ url: 'https://csdb.dk/release/?id=5' }] } }), null);
  assert.equal(csdbHint(null), null);
});

test('trackinfo.js asks for the section of a SID only, with the hint, into its host', () => {
  const ti = read('js/trackinfo.js');
  assert.match(ti, /import \{ csdbSectionHtml, csdbHint \} from '\.\/csdb_view\.js';/);
  assert.match(ti, /if \(!track \|\| !track\.id \|\| track\.format !== 'SID'\) return;/);
  assert.match(ti, /\/api\/scene\/csdb\?track_id=\$\{encodeURIComponent\(id\)\}/);
  assert.match(ti, /if \(hint\) q \+= `&sid=\$\{hint\}`;/);
  // only a section or a "not this tune" is kept for the page
  assert.match(ti, /if \(data && \(data\.found === true \|\| data\.reason === 'no_match'\)\) _csdbCache\[id\] = data;/);
  // both scene renders keep a host for it
  assert.equal((ti.match(/_CSDB_HOST/g) || []).length >= 3, true);
  const css = read('css/trackinfo-styles.css');
  assert.match(css, /#ti-panel \.ti-csdb-shot\.loaded \{ opacity: 1; \}/);
  assert.match(css, /:root\[data-ui-style="retro"\] #ti-panel \.ti-csdb-shot \{/);
});

test('the box-cover signal: once per track, only with a game, never for a station', () => {
  _resetArtIntent();
  const calls = [];
  const fetchFn = (url, opts) => { calls.push([url, opts.method]); return Promise.resolve({}); };
  assert.equal(artIntent({ id: 'a', game: 'Turrican II' }, { now: 0, fetchFn }), true);
  assert.equal(artIntent({ id: 'a', game: 'Turrican II' }, { now: 1000, fetchFn }), false);
  assert.equal(artIntent({ id: 'b', game: '' }, { now: 0, fetchFn }), false);
  assert.equal(artIntent({ id: 'c', game: 'X', station: true }, { now: 0, fetchFn }), false);
  assert.equal(artIntent({ id: 'a', game: 'Turrican II' }, { now: 11 * 60 * 1000, fetchFn }), true);
  assert.deepEqual(calls, [['/api/art/a/intent', 'POST'], ['/api/art/a/intent', 'POST']]);
  // the players and Track Info send it; a list row never does
  let handler = null;
  watchPlays({ on: (ev, fn) => { if (ev === 'trackchange') handler = fn; } });
  assert.equal(typeof handler, 'function');
  assert.match(read('js/app.js'), /watchPlays\(Player\);/);
  assert.match(read('js/mobile/app.js'), /watchPlays\(Player\);/);
  assert.match(read('js/trackinfo.js'), /artIntent\(track\);/);
  assert.ok(!/artIntent|art_intent/.test(read('js/library.js')));
});
