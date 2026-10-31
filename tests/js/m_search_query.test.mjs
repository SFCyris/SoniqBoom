// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The phone Search view's query text (soniqboom/frontend/js/mobile/search_query.js):
//   * a scope turns the field's free text into one quoted operator term
//     (ARTIST + "vnv nation" → artist:"vnv nation"); a " splits it, colons and
//     commas stay inside the quotes; ALL sends the text as typed;
//   * operator terms and exclusions typed by hand are kept as typed and not
//     scoped; a space after an operator's colon keeps it text in a scope
//     ("Back on Track: 2"), a range typed with spaces still joins;
//   * filters are quoted terms, put before the typed text (an unclosed quote
//     can't swallow them; the server can't join them to a typed "-5");
//   * a filter alone is a search; stored filters are validated;
//   * the operator list matches the desktop's (js/search.js _SUPPORTED_OPS,
//     itself test-guarded against the server);
//   * the phone page says "1 track" / "1 match" (mobile/views/_common.js countNoun).
//   * a scoped search is ranked by its scope (rankBy → rank_by on the view's
//     pages and its queue source) only when the scope applies to some text;
//   * the split matches the server on the cases in m_search_split_cases.json
//     (tests/test_mobile_search_split.py checks the same cases against api/search.py).
// Run: node --test tests/js/m_search_query.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const FE = resolve(ROOT, 'soniqboom/frontend/js');
const Q = await import(pathToFileURL(resolve(FE, 'mobile/search_query.js')).href);
const { buildQuery, splitQuery, scopedTerms, filterTerms, filterChips, normalizeFilters, tightenRanges, rankBy } = Q;

test('ALL sends the text as typed', () => {
  assert.equal(buildQuery({ text: '  vnv nation ', scope: 'all' }), 'vnv nation');
  assert.equal(buildQuery({ text: 'artist:vnv -live', scope: 'all' }), 'artist:vnv -live');
  assert.equal(buildQuery({ text: '', scope: 'artist' }), '');
  assert.equal(buildQuery({ text: '   ', scope: 'all' }), '');
  assert.equal(buildQuery({ text: 'x', scope: 'nonsense' }), 'x');        // unknown scope = ALL
});

test('a scope quotes the free text as one value', () => {
  assert.equal(buildQuery({ text: 'vnv nation', scope: 'artist' }), 'artist:"vnv nation"');
  assert.equal(buildQuery({ text: 'vnv   nation', scope: 'title' }), 'title:"vnv nation"');
  assert.equal(buildQuery({ text: 'imagin', scope: 'album' }), 'album:"imagin"');
  assert.equal(buildQuery({ text: 'synth', scope: 'genre' }), 'genre:"synth"');
  assert.equal(buildQuery({ text: 'last ninja', scope: 'game' }), 'game:"last ninja"');
});

test('quotes, colons and commas in a scoped value', () => {
  assert.equal(buildQuery({ text: 'Dwayne "The Rock" Johnson', scope: 'artist' }),
    'artist:"Dwayne" artist:"The Rock" artist:"Johnson"');
  assert.equal(buildQuery({ text: '"vnv nation"', scope: 'artist' }), 'artist:"vnv nation"');
  assert.equal(buildQuery({ text: '12" mix', scope: 'title' }), 'title:"12" title:"mix"');
  assert.equal(buildQuery({ text: 'Re:Cycle', scope: 'artist' }), 'artist:"Re:Cycle"');
  assert.equal(buildQuery({ text: 'Back on Track: 2', scope: 'album' }), 'album:"Back on Track: 2"');
  assert.equal(buildQuery({ text: 'Crosby, Stills', scope: 'artist' }), 'artist:"Crosby, Stills"');
  assert.equal(buildQuery({ text: 'a-ha', scope: 'artist' }), 'artist:"a-ha"');
  assert.equal(buildQuery({ text: '"', scope: 'artist' }), '');
  assert.deepEqual(scopedTerms('artist', ' " x "" y '), ['artist:"x"', 'artist:"y"']);
});

test('typed operators and exclusions combine with the scope, unscoped', () => {
  assert.equal(buildQuery({ text: 'nation year:>2000', scope: 'artist' }), 'artist:"nation" year:>2000');
  assert.equal(buildQuery({ text: 'vnv -live', scope: 'artist' }), 'artist:"vnv" -live');
  assert.equal(buildQuery({ text: 'vnv -"live at" nation', scope: 'artist' }), 'artist:"vnv nation" -"live at"');
  assert.equal(buildQuery({ text: '-format:mp3 vnv', scope: 'artist' }), 'artist:"vnv" -format:mp3');
  assert.equal(buildQuery({ text: 'format:flac, alac storm', scope: 'title' }), 'title:"storm" format:flac, alac');
  assert.equal(buildQuery({ text: 'Album:Once nemo', scope: 'title' }), 'title:"nemo" Album:Once');
  // an operator only: nothing to scope
  assert.equal(buildQuery({ text: 'year:1990', scope: 'artist' }), 'year:1990');
  // no operator name (foo:bar) stays text; an operator inside a free-text quote is phrase text
  assert.equal(buildQuery({ text: 'foo:bar', scope: 'title' }), 'title:"foo:bar"');
  assert.equal(buildQuery({ text: '"song year:1999 remaster"', scope: 'title' }), 'title:"song year:1999 remaster"');
});

test('a space after a colon is text in a scope; a typed range still joins', () => {
  assert.equal(splitQuery('back on track: 2').free, 'back on track: 2');
  assert.deepEqual(splitQuery('year:1990 - 1999 x').terms, ['year:1990-1999']);
  assert.deepEqual(splitQuery('bitrate:128 kbps x').terms, ['bitrate:128kbps']);
  // (ALL: the server's own tightening — mirrored, incl. "not inside a word")
  assert.equal(tightenRanges('track: >= 3'), 'track:>=3');
  assert.equal(tightenRanges('year:1990 - 2000'), 'year:1990-2000');
  assert.equal(tightenRanges('multi-track: 2'), 'multi-track: 2');
  assert.equal(tightenRanges('album:"back on track: 2"'), 'album:"back on track: 2"');
});

test('filters: quoted terms, before the typed text', () => {
  const f = { year: { from: '1990', to: '1999' }, format: 'FLAC', added: '<30d', rating: '>=4',
              length: '<2:00', chip: '6581', channels: '>8', tunes: '>1' };
  assert.deepEqual(filterTerms(f), ['year:"1990-1999"', 'format:"FLAC"', 'added:"<30d"', 'rating:">=4"',
    'length:"<2:00"', 'chip:"6581"', 'channels:">8"', 'tunes:">1"']);
  assert.equal(buildQuery({ text: 'vnv', scope: 'artist', filters: { year: { from: '2000', to: '' } } }),
    'year:">=2000" artist:"vnv"');
  assert.equal(buildQuery({ text: '"unclosed phrase', scope: 'all', filters: { format: 'MP3' } }),
    'format:"MP3" "unclosed phrase');
  assert.equal(buildQuery({ text: '-5', scope: 'all', filters: { year: { from: '1990', to: '1999' } } }),
    'year:"1990-1999" -5');
  // a filter alone searches
  assert.equal(buildQuery({ text: '', scope: 'artist', filters: { rating: '0' } }), 'rating:"0"');
  assert.deepEqual(filterTerms({ year: { from: '1999', to: '1990' } }), ['year:"1990-1999"']);
  assert.deepEqual(filterTerms({ year: { from: '', to: '1980' } }), ['year:"<=1980"']);
  assert.deepEqual(filterTerms({ year: { from: '1985', to: '1985' } }), ['year:"1985"']);
  assert.deepEqual(filterTerms({ format: 'DSF (Dreamcast)' }), ['format:"DSF (Dreamcast)"']);
});

test('stored filters are validated', () => {
  assert.deepEqual(normalizeFilters(null), {});
  assert.deepEqual(normalizeFilters('x'), {});
  assert.deepEqual(normalizeFilters({ year: { from: 'abc', to: '12345' }, rating: '>=9', added: '<2d',
                                     format: '"', chip: '6581', bogus: 1 }), { chip: '6581' });
  assert.deepEqual(normalizeFilters({ format: 'a "b" c' }), { format: 'a b c' });
});

test('the keys under the field name each filter and say "remove"', () => {
  const chips = filterChips({ year: { from: '1990', to: '1999' }, format: 'lossless', rating: '>=4', tunes: '>1' });
  assert.deepEqual(chips.map((c) => c.label), ['Year 1990–1999', 'Format Lossless', 'Rating 4★+', 'Multi-tune']);
  for (const c of chips) assert.match(c.aria, /^Remove filter: /);
  assert.equal(filterChips({ year: { from: '2000', to: '' } })[0].label, 'Year from 2000');
});

test('the phone knows the desktop search box operators', () => {
  const src = readFileSync(resolve(FE, 'search.js'), 'utf8');
  const desk = new Function(`${src.match(/const _SUPPORTED_OPS = new Set\(\[[\s\S]*?\]\);/)[0]}\nreturn _SUPPORTED_OPS;`)();
  assert.deepEqual([...Q.SUPPORTED_OPS].sort(), [...desk].sort());
  for (const [, , op] of Q.SCOPES) if (op) assert.ok(desk.has(op), op);
  for (const k of Q.FILTER_ORDER) assert.ok(desk.has(k), k);
});

test('the phone page says "1 track"', () => {
  const src = readFileSync(resolve(FE, 'mobile/views/_common.js'), 'utf8');
  const countNoun = new Function(`${src.match(/export function countNoun[\s\S]*?\n\}/)[0].replace('export ', '')}\nreturn countNoun;`)();
  assert.equal(countNoun(1, 'track'), '1 track');
  assert.equal(countNoun(2, 'track'), '2 tracks');
  assert.equal(countNoun(0, 'match', 'matches'), '0 matches');
  assert.equal(countNoun(1, 'match', 'matches'), '1 match');
  // no hand-made "N tracks" left in the phone views
  for (const f of ['library.js', 'search.js', 'queue.js', 'radio.js', 'settings.js']) {
    const v = readFileSync(resolve(FE, 'mobile/views', f), 'utf8');
    assert.doesNotMatch(v, /\$\{[^}]+\}\s*tracks\b|' tracks'/, f);
  }
});

test('the split matches the server on the shared cases (tests/test_mobile_search_split.py)', () => {
  const cases = JSON.parse(readFileSync(resolve(ROOT, 'tests/js/m_search_split_cases.json'), 'utf8'));
  assert.ok(cases.length >= 100);
  for (const c of cases) {
    const got = splitQuery(c.text);
    assert.deepEqual([[...got.ops].sort(), [...got.excl].sort(), got.free], [c.ops, c.excl, c.free], JSON.stringify(c.text));
    assert.equal(got.terms.length, got.ops.length + got.excl.length);
  }
});

test('a comparator typed with a space after it still works in a scope', () => {
  assert.equal(buildQuery({ text: 'vnv year:>= 2000', scope: 'artist' }), 'artist:"vnv" year:>=2000');
  assert.equal(buildQuery({ text: 'vnv year:< 2000', scope: 'artist' }), 'artist:"vnv" year:<2000');
});

test('a scoped search is ranked by its scope only when the scope applies to some text', () => {
  assert.equal(rankBy({ text: 'space', scope: 'title' }), 'title');
  assert.equal(rankBy({ text: 'beek', scope: 'artist' }), 'artist');
  assert.equal(rankBy({ text: '  imagine dragons ', scope: 'album' }), 'album');
  assert.equal(rankBy({ text: 'synth', scope: 'genre' }), 'genre');
  assert.equal(rankBy({ text: 'uridium', scope: 'game' }), 'game');
  assert.equal(rankBy({ text: 'space', scope: 'all' }), null);            // ALL: as before
  assert.equal(rankBy({ text: '', scope: 'title' }), null);              // filters only
  assert.equal(rankBy({ text: 'artist:vnv -live', scope: 'title' }), null);   // only typed terms
  assert.equal(rankBy({ text: '"', scope: 'title' }), null);
  assert.equal(rankBy({ text: 'space -live', scope: 'title' }), 'title');
  assert.equal(rankBy({ text: 'space', scope: 'bogus' }), null);
  // exactly when the scope turned some text into its operator (the header's "scoped")
  for (const text of ['space', '', 'year:2000', 'a "b" c', '-x']) {
    for (const [scope] of Q.SCOPES) {
      const scoped = scope !== 'all' && buildQuery({ text, scope }) !== buildQuery({ text, scope: 'all' });
      assert.equal(rankBy({ text, scope }) !== null, scoped, `${scope} ${JSON.stringify(text)}`);
    }
  }
});

test('the phone view sends rank_by on its pages and in the queue source', () => {
  const src = readFileSync(resolve(FE, 'mobile/views/search.js'), 'utf8');
  assert.match(src, /if \(rank\) p\.set\('rank_by', rank\)/);
  assert.match(src, /const rank = rankBy\(\{ text, scope \}\)/);
  assert.match(src, /_fetchPage\(qq, rs, off, lim, rank\)/);
  assert.match(src, /if \(pager\.rankBy\) params\.rank_by = pager\.rankBy/);
  assert.match(src, /ordered: \{ url: '\/api\/search\/page', params \}/);
});
