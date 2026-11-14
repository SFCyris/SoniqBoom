// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What a station says it plays, as the phone's radio player keeps it
// (soniqboom/frontend/js/mobile/radio-player.js, ``[m-radio-now:begin]``),
// run outside a browser: a title is a song, the same title said again keeps
// its cover, another title drops it, a cover is taken only for the title
// that still plays and only from this server; and the two lines a station
// shows (``_stationLines``).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/mobile/radio-player.js'), 'utf8');
const m = SRC.match(/\/\/ \[m-radio-now:begin\][^\n]*\n([\s\S]*?)\/\/ \[m-radio-now:end\]/);
if (!m) throw new Error('m-radio-now region not found');
const make = () => new Function(`${m[1]}
  return { meta: _applyMeta, art: _applyArt, forget: _forgetNow,
           now: () => _now, seq: () => _nowSeq, lines: _stationLines, coverOf: _coverOf };`)();
const read = (rel) => readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/mobile', rel), 'utf8');

test('a title is a song; its halves and no cover yet', () => {
  const r = make();
  assert.equal(r.now(), null);
  assert.equal(r.meta({ title: 'Jogeir Liljedahl - Guitar Slinger', artist: 'Jogeir Liljedahl', song: 'Guitar Slinger' }), true);
  assert.deepEqual(r.now(), { title: 'Jogeir Liljedahl - Guitar Slinger', artist: 'Jogeir Liljedahl',
                              song: 'Guitar Slinger', cover: null });
  assert.equal(r.seq(), 1);
});

test('no title, or one that is not text, is no song', () => {
  const r = make();
  for (const bad of [null, {}, { title: '' }, { title: 7 }, { title: null, artist: 'x' }]) {
    assert.equal(r.meta(bad), false);
  }
  assert.equal(r.now(), null);
  assert.equal(r.seq(), 0);
  r.meta({ title: 'Only a title', artist: 3, song: undefined });
  assert.deepEqual([r.now().artist, r.now().song], ['', '']);
});

test('the cover is taken for the title that plays — and stays when the title is said again', () => {
  const r = make();
  r.meta({ title: 'A - B', artist: 'A', song: 'B' });
  assert.equal(r.art({ title: 'A - B', cover_url: '/api/stations/nowplaying-art/a-b-1f', album: 'Alb', year: 1992 }), true);
  assert.deepEqual(r.now(), { title: 'A - B', artist: 'A', song: 'B', cover: '/api/stations/nowplaying-art/a-b-1f' });
  const seq = r.seq();
  assert.equal(r.art({ title: 'A - B', cover_url: '/api/stations/nowplaying-art/a-b-1f' }), false);   // the same again
  assert.equal(r.meta({ title: 'A - B', artist: 'A', song: 'B' }), false);                             // a relay that reconnected
  assert.equal(r.now().cover, '/api/stations/nowplaying-art/a-b-1f');
  assert.equal(r.seq(), seq);
});

test('another title is another song: the earlier cover goes, a late cover for it is refused', () => {
  const r = make();
  r.meta({ title: 'A - B' });
  r.art({ title: 'A - B', cover_url: '/api/art/0123abcd/lg.jpg' });
  assert.equal(r.meta({ title: 'C - D', artist: 'C', song: 'D' }), true);
  assert.equal(r.now().cover, null);
  assert.equal(r.art({ title: 'A - B', cover_url: '/api/art/0123abcd/lg.jpg' }), false);
  assert.equal(r.now().cover, null);
  assert.equal(r.art({ title: 'C - D', cover_url: '/api/art/89ef/lg.jpg' }), true);
  assert.equal(r.now().cover, '/api/art/89ef/lg.jpg?fallback=none');
});

test("a library track's art is asked for without the placeholder image; a fetched cover as it is named", () => {
  const r = make();
  assert.equal(r.coverOf('/api/art/0123abcd/lg.jpg'), '/api/art/0123abcd/lg.jpg?fallback=none');
  assert.equal(r.coverOf('/api/art/Ab_9-z/sm.jpg'), '/api/art/Ab_9-z/sm.jpg?fallback=none');
  assert.equal(r.coverOf('/api/art/0123abcd/full.jpg'), '/api/art/0123abcd/full.jpg?fallback=none');
  assert.equal(r.coverOf('/api/stations/nowplaying-art/a-b-1f'), '/api/stations/nowplaying-art/a-b-1f');
  // the same cover named again is no news
  r.meta({ title: 'A - B' });
  assert.equal(r.art({ title: 'A - B', cover_url: '/api/art/0123abcd/lg.jpg' }), true);
  const seq = r.seq();
  assert.equal(r.art({ title: 'A - B', cover_url: '/api/art/0123abcd/lg.jpg' }), false);
  assert.equal(r.seq(), seq);
});

test('a cover with no song, or from anywhere but this server, is refused', () => {
  const r = make();
  assert.equal(r.art({ title: 'A - B', cover_url: '/api/art/x/lg.jpg' }), false);      // no song yet
  r.meta({ title: 'A - B' });
  for (const url of [null, '', 7, 'https://example.org/c.jpg', '//example.org/api/art/x/lg.jpg',
                     '/api/art/', '/api/other/x.jpg', '/api/art/x/lg.jpg?next=//example.org',
                     'javascript:alert(1)', '/api/art/x y/lg.jpg', ' /api/art/x/lg.jpg',
                     // only the two shapes the server names: no way up, no other size, no query
                     '/api/art/../../api/auth/logout', '/api/art/../x/lg.jpg', '/api/art/%2e%2e/lg.jpg',
                     '/api/art/x/huge.jpg', '/api/art/x/lg.png', '/api/art/x', '/api/art/x/lg.jpg/',
                     '/api/art/x/lg.jpg?fallback=none', '/api/art/x/lg.jpg#f', '/api/art/a/b/lg.jpg',
                     '/api/stations/nowplaying-art/../relay/x', '/api/stations/nowplaying-art/A-B',
                     '/api/stations/nowplaying-art/a-b?x=1', '/api/stations/nowplaying-art/',
                     `/api/stations/nowplaying-art/${'a'.repeat(121)}`,
                     '/api/art/x/lg.jpg\n', '/api/stations/now/test']) {
    assert.equal(r.coverOf(url), null, String(url));
    assert.equal(r.art({ title: 'A - B', cover_url: url }), false, String(url));
  }
  assert.equal(r.now().cover, null);
});

test('a station shows two lines: the song over "artist — station", else its name over "Internet radio"', () => {
  const r = make();
  const st = { name: 'Nectarine' };
  assert.deepEqual(r.lines(st, null), { title: 'Nectarine', sub: 'Internet radio' });
  assert.deepEqual(r.lines({}, null), { title: 'Radio', sub: 'Internet radio' });
  assert.deepEqual(r.lines(null, null), { title: 'Radio', sub: 'Internet radio' });
  assert.deepEqual(r.lines(st, { title: 'Jogeir Liljedahl - Guitar Slinger', artist: 'Jogeir Liljedahl', song: 'Guitar Slinger', cover: null }),
                   { title: 'Guitar Slinger', sub: 'Jogeir Liljedahl \u2014 Nectarine' });
  // a title that does not split: the whole title, the station alone under it
  assert.deepEqual(r.lines(st, { title: 'Station ident', artist: '', song: '', cover: null }),
                   { title: 'Station ident', sub: 'Nectarine' });
  // a song without an artist
  assert.deepEqual(r.lines(st, { title: 'x', artist: '', song: 'Only a song', cover: null }),
                   { title: 'Only a song', sub: 'Nectarine' });
  // a station without a name
  assert.deepEqual(r.lines({}, { title: 'A - B', artist: 'A', song: 'B', cover: null }),
                   { title: 'B', sub: 'A \u2014 Radio' });
});

test('forgetting leaves no song and outdates what was asked before', () => {
  const r = make();
  r.meta({ title: 'A - B' });
  const seq = r.seq();
  r.forget();
  assert.equal(r.now(), null);
  assert.ok(r.seq() > seq);
});

test('the player takes events only of its own relayed station, and asks the server once it plays', () => {
  assert.match(SRC, /const _mine = \(m\) => !!\(_station && _viaRelay && m && m\.sid && m\.sid === _station\.sid\);/);
  assert.match(SRC, /noteMeta\(m\) \{ if \(_mine\(m\) && _applyMeta\(m\)\) _said\(\); \}/);
  assert.match(SRC, /noteArt\(m\)  \{ if \(_mine\(m\) && _applyArt\(m\)\) _said\(\); \}/);
  assert.match(SRC, /_takeOver\(station\);[^\n]*\n\s*emit\('state'\);\n\s*MobileRadio\.refreshNow\(\);/);
  assert.match(SRC, /if \(!m \|\| _station !== st \|\| !_viaRelay \|\| seq !== _nowSeq\) return;/);
});

test('a station paused while it tuned in, and one resumed, ask what it plays too', () => {
  assert.match(SRC, /if \(_pausedInSeq === seq\) \{\n\s*_takeOver\(station\); emit\('state'\);\n\s*MobileRadio\.refreshNow\(\);/);
  assert.match(SRC, /cur\.play\(\)\.then\(\(\) => MobileRadio\.refreshNow\(\)\)\.catch\(\(\) => \{\}\);/);
});

test('the Now Playing screen and the mini-player show the station through the same two lines', () => {
  assert.match(SRC, /get lines\(\)\s+\{ return _stationLines\(_station, _station \? _now : null\); \}/);
  const np = read('views/nowplaying.js');
  assert.match(np, /const lines = MobileRadio\.lines;\n\s*titleEl\.textContent\s+= lines\.title;\n\s*artistEl\.textContent = lines\.sub;/);
  const app = read('app.js');
  assert.match(app, /const lines = MobileRadio\.lines;[\s\S]{0,120}miniTitle\.textContent\s+= lines\.title;\n\s*miniArtist\.textContent = lines\.sub;/);
});
