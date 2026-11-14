// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What a station says it plays, as the desktop's stations module keeps it
// (soniqboom/frontend/js/stations.js, ``[radio-now:begin]``), run outside a
// browser: a title is a song, the same title said again keeps what was found
// for it, another title drops it, and what was found is taken only for the
// title that still plays.  Source guards: the module asks what a station last
// said once it plays and when the socket connects.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const read = (rel) => readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js', rel), 'utf8');
const SRC = read('stations.js');
const m = SRC.match(/\/\/ \[radio-now:begin\][^\n]*\n([\s\S]*?)\/\/ \[radio-now:end\]/);
if (!m) throw new Error('radio-now region not found');
const make = () => new Function(`${m[1]}
  return { title: _takeTitle, art: _takeArt, forget: _forgetNow, held: _heldCover,
           now: () => _nowTitle, meta: () => _artMeta, seq: () => _nowSeq,
           who: () => [_nowSong, _nowArtist] };`)();
const ART = { title: 'A - B', cover_url: '/api/stations/nowplaying-art/a-b', album: 'Alb', year: 1991,
              label: null, source: 'musicbrainz' };

test('a title is taken once; said again it changes nothing', () => {
  const r = make();
  assert.equal(r.title({ title: 'A - B' }), true);
  assert.equal(r.now(), 'A - B');
  assert.equal(r.seq(), 1);
  assert.equal(r.title({ title: 'A - B' }), false);
  assert.equal(r.seq(), 1);
});

test('no title, or one that is not text, is no song', () => {
  const r = make();
  for (const bad of [null, undefined, {}, { title: '' }, { title: 7 }, { title: null }]) {
    assert.equal(r.title(bad), false);
  }
  assert.equal(r.now(), '');
  assert.equal(r.seq(), 0);
});

test('the same title said again keeps what was found for it', () => {
  const r = make();
  r.title({ title: 'A - B' });
  assert.equal(r.art(ART), true);
  assert.equal(r.title({ title: 'A - B' }), false);
  assert.equal(r.meta(), ART);
});

test('another title drops what was found for the earlier one', () => {
  const r = make();
  r.title({ title: 'A - B' });
  r.art(ART);
  assert.equal(r.title({ title: 'C - D' }), true);
  assert.equal(r.meta(), null);
  assert.equal(r.now(), 'C - D');
});

test('what was found for another title, or with no title playing, is not taken', () => {
  const r = make();
  assert.equal(r.art(ART), false);
  r.title({ title: 'C - D' });
  assert.equal(r.art(ART), false);
  assert.equal(r.art(null), false);
  assert.equal(r.meta(), null);
  assert.equal(r.seq(), 1);
});

test('the same finding said again is not taken twice; a different one is', () => {
  const r = make();
  r.title({ title: 'A - B' });
  r.art(ART);
  const seq = r.seq();
  assert.equal(r.art({ ...ART, label: undefined }), false);     // (null and absent are the same)
  assert.equal(r.seq(), seq);
  assert.equal(r.art({ ...ART, cover_url: '/api/art/abc/lg.jpg', source: 'library' }), true);
  assert.equal(r.meta().source, 'library');
  assert.equal(r.seq(), seq + 1);
});

test('forgetting drops the title and what was found, and counts', () => {
  const r = make();
  r.title({ title: 'A - B' });
  r.art(ART);
  const seq = r.seq();
  r.forget();
  assert.deepEqual([r.now(), r.meta(), r.seq()], ['', null, seq + 1]);
  assert.equal(r.title({ title: 'A - B' }), true);               // (the same title is a song again)
});

test('the module asks what a station last said once it plays and when the socket connects', () => {
  const ask = SRC.match(/async function _askNow\(\) \{[\s\S]*?\n\}\n/);
  assert.ok(ask, '_askNow not found');
  assert.match(ask[0], /\/api\/stations\/now\/\$\{encodeURIComponent\(cur\.station\.sid\)\}/);
  // An answer for a station left meanwhile, or one a newer title overtook, is dropped.
  assert.match(ask[0], /_current !== cur \|\| seq !== _nowSeq/);
  const tryPlay = SRC.match(/async function _tryPlay\(\) \{[\s\S]*?\n\}\n/)[0];
  // (asked as the start is issued, not once the stream plays)
  assert.match(tryPlay, /const started = Player\.playStation\([^)]*\);\n(?:\s*\/\/[^\n]*\n)*\s*_askNow\(\);\n\s*await started;/);
  assert.equal((tryPlay.match(/_askNow\(\)/g) || []).length, 1);
  // Every (re)start of the stream puts what is held back on the bar when the
  // player announces the item it built — not only once the stream plays.
  assert.match(SRC, /\nPlayer\.on\('trackchange', _onStationItem\);\n/);
  assert.doesNotMatch(tryPlay, /_showHeld/);
  assert.match(SRC, /addEventListener\('sb:socket-open', \(\) => \{ _askNow\(\); \}\)/);
  assert.match(read('app.js'), /ws\.onopen = \(\) => \{[\s\S]*?dispatchEvent\(new CustomEvent\('sb:socket-open'\)\)/);
});

test('a tune-in, a station that cannot play, a stop and a library track taking over forget the song', () => {
  assert.equal((SRC.match(/\b_forgetNow\(\);/g) || []).length, 4);
  const gone = SRC.match(/function _unavailable\(station\) \{[\s\S]*?\n\}\n/);
  assert.ok(gone, '_unavailable not found');
  assert.match(gone[0], /_forgetNow\(\);/);
  // (nothing outside the region writes the title)
  const outside = SRC.replace(m[0], '');
  assert.doesNotMatch(outside, /_nowTitle\s*=[^=]/);
});

// ── The song and its halves; the cover that may be shown ─────────────────────

test('a title keeps its two halves; another title or forgetting replaces them', () => {
  const r = make();
  r.title({ title: 'The Reds - Red Song', song: 'Red Song', artist: 'The Reds' });
  assert.deepEqual(r.who(), ['Red Song', 'The Reds']);
  r.title({ title: 'The Reds - Red Song', song: 'x', artist: 'y' });     // (said again: nothing moves)
  assert.deepEqual(r.who(), ['Red Song', 'The Reds']);
  r.title({ title: 'Jingle' });                                          // (a title the server did not split)
  assert.deepEqual(r.who(), ['', '']);
  r.title({ title: 'A - B', song: 7, artist: null });                    // (halves that are no text)
  assert.deepEqual(r.who(), ['', '']);
  r.title({ title: 'C - D', song: 'D', artist: 'C' });
  r.forget();
  assert.deepEqual(r.who(), ['', '']);
});

test('a cover is shown only from this server, in the two shapes it names', () => {
  const held = (cover_url) => {
    const r = make();
    r.title({ title: 'A - B' });
    r.art({ ...ART, cover_url });
    return r.held();
  };
  for (const ok of ['/api/art/abc_DEF-123/lg.jpg', '/api/art/abc/sm.jpg', '/api/art/abc/full.jpg',
                    '/api/stations/nowplaying-art/a-b', '/api/stations/nowplaying-art/the-reds-red-song-1991']) {
    assert.equal(held(ok), ok);
  }
  for (const bad of ['https://example.org/cover.jpg', '//example.org/api/art/abc/lg.jpg',
                     '/api/art/abc/lg.jpg?x=1', '/api/art/../admin/lg.jpg', '/api/art/abc/xl.jpg',
                     '/api/stations/nowplaying-art/A-B', '/api/stations/nowplaying-art/a/b',
                     `/api/stations/nowplaying-art/${'a'.repeat(121)}`, 'javascript:alert(1)',
                     ' /api/art/abc/lg.jpg', '/api/art/abc/lg.jpg\n', '', null, undefined, 7, {}]) {
    assert.equal(held(bad), null, String(bad));
  }
  assert.equal(make().held(), null);                                     // (nothing found yet)
});

// ── The handlers, run as they are ────────────────────────────────────────────

const fnOf = (re, name) => {
  const f = SRC.match(re);
  if (!f) throw new Error(`${name} not found`);
  return f[0];
};
const HANDLERS = [
  fnOf(/function _showHeld\(\) \{[\s\S]*?\n\}\n/, '_showHeld'),
  fnOf(/let _itemShown = null;\nfunction _onStationItem\(t\) \{[\s\S]*?\n\}\n/, '_onStationItem'),
  fnOf(/function _onMeta\(d\) \{[\s\S]*?\n\}\n/, '_onMeta'),
  fnOf(/function _onArt\(d\) \{[\s\S]*?\n\}\n/, '_onArt'),
  fnOf(/async function _askNow\(\) \{[\s\S]*?\n\}\n/, '_askNow'),
].join('\n');
const SID = 'test:one';
const RED = { sid: SID, title: 'The Reds - Red Song', song: 'Red Song', artist: 'The Reds' };
const RED_ART = { sid: SID, title: RED.title, cover_url: '/api/stations/nowplaying-art/the-reds-red-song',
                  album: 'Red', year: 1991, label: null, source: 'musicbrainz' };

/** The region and its handlers over a player that notes what it is told. */
function rig() {
  const bar = [];                      // what the player bar was told, in order
  const Player = {
    stationMode: true,
    setStationNowPlaying: (song, artist) => bar.push(['song', song, artist]),
    updateStationArt: (url) => bar.push(['art', url]),
  };
  const net = { urls: [], answer: null };
  const fetch = async (url) => {
    net.urls.push(url);
    const a = typeof net.answer === 'function' ? await net.answer() : net.answer;
    if (a instanceof Error) throw a;
    return a === null ? { ok: false, json: async () => null } : { ok: true, json: async () => a };
  };
  const api = new Function('Player', 'fetch', `${m[1]}
    let _current = { station: { sid: '${SID}' } };
    const painted = [];
    const _updateNowTitle = () => painted.push('card'), _updateInfoNow = () => painted.push('info'),
          _updateRadioBar = () => painted.push('bar');
    ${HANDLERS}
    return { meta: _onMeta, art: _onArt, ask: _askNow, show: _showHeld, forget: _forgetNow,
             item: _onStationItem,
             held: _heldCover, now: () => _nowTitle, painted,
             tune: (sid) => { _current = sid ? { station: { sid } } : null; } };`)(Player, fetch);
  return { ...api, bar, Player, net };
}

test('a title puts the song and artist on the bar and the logo back; its cover follows', () => {
  const r = rig();
  r.meta(RED);
  assert.deepEqual(r.bar, [['song', 'Red Song', 'The Reds'], ['art', null]]);
  r.art(RED_ART);
  assert.deepEqual(r.bar.at(-1), ['art', RED_ART.cover_url]);
  assert.equal(r.bar.length, 3);
});

test('the same title or the same finding said again tells the bar nothing', () => {
  const r = rig();
  r.meta(RED); r.art(RED_ART);
  const told = r.bar.length, painted = r.painted.length;
  r.meta({ ...RED }); r.art({ ...RED_ART }); r.meta({ ...RED });
  assert.equal(r.bar.length, told);
  assert.equal(r.painted.length, painted);
});

test('a restarted stream: what is held goes back on the bar with no word from the station', () => {
  const r = rig();
  r.meta(RED); r.art(RED_ART);
  r.bar.length = 0;                    // (the player built the station's item anew: name and logo)
  r.show();
  assert.deepEqual(r.bar, [['song', 'Red Song', 'The Reds'], ['art', RED_ART.cover_url]]);
  // … and a song with no cover found gets the logo, not an older song's cover
  r.meta({ sid: SID, title: 'The Blues - Blue Song', song: 'Blue Song', artist: 'The Blues' });
  r.bar.length = 0;
  r.show();
  assert.deepEqual(r.bar, [['song', 'Blue Song', 'The Blues'], ['art', null]]);
});

/** A player that keeps one station item and announces it the way the real one
 *  does: a new item per start, the same item again per song / cover put on it. */
function itemRig() {
  const r = rig();
  const seen = [];                     // every announcement: [title, cover]
  let item = null, depth = 0, deepest = 0;
  const announce = () => {
    if (++depth > 20) throw new Error('announcements do not end');
    deepest = Math.max(deepest, depth);
    seen.push([item.title, item.cover_art]);
    r.item(item);
    depth--;
  };
  r.Player.setStationNowPlaying = (song, artist) => {
    item.song = song || ''; item.title = song || item.stationName; item.artist = artist || 'Internet radio';
    announce();
  };
  r.Player.updateStationArt = (url) => { item.cover_art = url || ''; announce(); };
  const start = (sid = SID) => {
    item = { id: '', title: 'Test One FM', stationName: 'Test One FM', station: true, sid, song: '', cover_art: '' };
    announce();
    return item;
  };
  return { ...r, seen, start, depth: () => deepest, announceAgain: () => announce() };
}

test('a new station item is given the held song and cover as it is announced, once', () => {
  const r = itemRig();
  r.start();                           // (tune-in: nothing held, nothing put on it)
  assert.deepEqual(r.seen, [['Test One FM', '']]);
  r.seen.length = 0;
  r.meta(RED);                         // (its first song: put on the item once, not twice)
  assert.deepEqual(r.seen, [['Red Song', ''], ['Red Song', '']]);
  r.art(RED_ART);
  r.seen.length = 0;
  const item = r.start();              // (the stream restarted: the player built the item anew)
  assert.equal(item.title, 'Red Song');
  assert.equal(item.artist, 'The Reds');
  assert.equal(item.cover_art, RED_ART.cover_url);
  // the bare item, then the song, then the cover — and no more
  assert.deepEqual(r.seen, [['Test One FM', ''], ['Red Song', ''], ['Red Song', RED_ART.cover_url]]);
  assert.ok(r.depth() <= 2);
  r.announceAgain();                   // (the same item said again: nothing put on it twice)
  assert.equal(r.seen.length, 4);
});

test('a new item for a song with no cover found gets the song and the logo', () => {
  const r = itemRig();
  r.start();
  r.meta(RED); r.art(RED_ART);
  r.meta({ sid: SID, title: 'Nobody Known - Tune Without Cover', song: 'Tune Without Cover', artist: 'Nobody Known' });
  const item = r.start();
  assert.deepEqual([item.title, item.artist, item.cover_art], ['Tune Without Cover', 'Nobody Known', '']);
});

test('an item of another station, a library track, or one with no station tuned is left alone', () => {
  for (const how of ['other', 'library', 'untuned', 'left']) {
    const r = itemRig();
    r.start();
    r.meta(RED); r.art(RED_ART);
    r.seen.length = 0;
    if (how === 'other') { const it = r.start('test:two'); assert.equal(it.title, 'Test One FM'); }
    if (how === 'library') r.item({ id: 'abc', title: 'A library tune', sid: SID });
    if (how === 'untuned') { r.tune(null); assert.equal(r.start().title, 'Test One FM'); }
    if (how === 'left') { r.Player.stationMode = false; assert.equal(r.start().title, 'Test One FM'); }
    assert.ok(r.seen.length <= 1, how);
    r.item(null); r.item(undefined);   // (nothing to look at)
  }
});

/** _tryPlay as it is, over a player whose start the test settles. */
function startRig() {
  const src = fnOf(/async function _tryPlay\(\) \{[\s\S]*?\n\}\n/, '_tryPlay');
  const log = [];
  const starts = [];                   // every start's promise, to settle by hand
  const Player = {
    stationMode: true,
    playStation: (st, url) => { log.push(['start', url]); return new Promise((ok, no) => { starts.push({ ok, no }); }); },
  };
  const api = new Function('Player', 'log', 'navigator', 'setTimeout', `
    let _current = { station: { sid: '${SID}' }, cands: [{ v: 0, s: { codec: 'MP3' } }], i: 0 };
    let _playGen = 0, _activeUrl = '', _startupTimer = null, _hasPlayed = false, _rebuffering = false;
    const STARTUP_GRACE_MS = 1;
    const _resetBuffering = () => {}, _onUnderrun = () => log.push(['underrun']),
          _renderNowPlaying = () => {}, _askNow = () => log.push(['ask']);
    ${src}
    return { tryPlay: _tryPlay };`)(Player, log, { vendor: '' }, () => 0);
  return { ...api, log, ok: (n = 0) => starts[n].ok(), no: (n = 0) => starts[n].no(new Error('AbortError')) };
}

test('a start asks what the station last said at once — whether the stream plays, waits or is refused', async () => {
  for (const how of ['ok', 'no', 'never']) {
    const r = startRig();
    const run = r.tryPlay();
    // asked after the start was issued (the player is in station mode by then), not once it played
    assert.deepEqual(r.log, [['start', '/api/stations/relay/test%3Aone?v=0'], ['ask']], how);
    if (how === 'never') continue;
    r[how]();
    await run;
    assert.equal(r.log.length, 2, how);  // (and not a second time)
  }
});

test('every start asks once: a reconnect, and the start it overtook', async () => {
  for (const how of ['ok', 'no']) {
    const r = startRig();
    const first = r.tryPlay();
    const second = r.tryPlay();          // (a reconnect: the newer start)
    r[how](0);
    await first;
    r.ok(1);
    await second;
    assert.deepEqual(r.log.map(e => e[0]), ['start', 'ask', 'start', 'ask'], how);
  }
});

test('with no song held a restart tells the bar nothing', () => {
  const r = rig();
  r.show();
  r.meta(RED); r.forget(); r.bar.length = 0;
  r.show();
  assert.deepEqual(r.bar, []);
});

test('a finding with no cover, or a cover that is not this server\'s, shows the logo', () => {
  for (const cover_url of [null, '', 'https://example.org/c.jpg']) {
    const r = rig();
    r.meta(RED); r.art(RED_ART);
    r.art({ ...RED_ART, cover_url, source: 'library' });
    assert.deepEqual(r.bar.at(-1), ['art', null], String(cover_url));
  }
});

test('another station\'s title or finding, or one with no station tuned, is not taken', () => {
  const r = rig();
  r.meta({ ...RED, sid: 'test:two' });
  r.art({ ...RED_ART, sid: 'test:two' });
  r.tune(null);
  r.meta(RED); r.art(RED_ART);
  assert.deepEqual(r.bar, []);
  assert.equal(r.now(), '');
});

test('asking: the song and cover a running station last said are shown, with one request', async () => {
  const r = rig();
  r.net.answer = { ...RED, ...RED_ART, station: 'One' };
  await r.ask();
  assert.deepEqual(r.net.urls, ['/api/stations/now/test%3Aone']);
  assert.deepEqual(r.bar, [['song', 'Red Song', 'The Reds'], ['art', null], ['art', RED_ART.cover_url]]);
  assert.equal(r.held(), RED_ART.cover_url);
});

test('asking: an answer with nothing found leaves the cover that is held', async () => {
  const r = rig();
  r.meta(RED); r.art(RED_ART);
  const told = r.bar.length;
  r.net.answer = { ...RED, cover_url: null, album: null, year: null, label: null, source: null };
  await r.ask();
  assert.equal(r.bar.length, told);
  assert.equal(r.held(), RED_ART.cover_url);
});

test('asking: an answer for another song takes the song and drops the old cover', async () => {
  const r = rig();
  r.meta(RED); r.art(RED_ART);
  r.net.answer = { sid: SID, title: 'The Blues - Blue Song', song: 'Blue Song', artist: 'The Blues', cover_url: null };
  await r.ask();
  assert.deepEqual(r.bar.slice(-2), [['song', 'Blue Song', 'The Blues'], ['art', null]]);
  assert.equal(r.held(), null);
});

test('asking: no station, a library track playing, no answer or a failed request change nothing', async () => {
  const off = rig();
  off.Player.stationMode = false;
  await off.ask();
  const none = rig();
  none.tune(null);
  await none.ask();
  assert.deepEqual([off.net.urls, none.net.urls], [[], []]);          // (not even asked)
  for (const answer of [null, new Error('offline'), { sid: SID, title: null }]) {
    const r = rig();
    r.net.answer = answer;
    await r.ask();
    assert.deepEqual(r.bar, []);
    assert.equal(r.now(), '');
  }
});

test('asking: an answer a newer title overtook, or for a station left meanwhile, is dropped', async () => {
  const r = rig();
  r.net.answer = () => {               // (the socket says a newer song while the answer is on its way)
    r.meta({ sid: SID, title: 'The Blues - Blue Song', song: 'Blue Song', artist: 'The Blues' });
    return { ...RED, ...RED_ART };
  };
  await r.ask();
  assert.equal(r.now(), 'The Blues - Blue Song');
  assert.deepEqual(r.bar, [['song', 'Blue Song', 'The Blues'], ['art', null]]);

  const left = rig();
  left.net.answer = () => { left.tune('test:two'); return { ...RED, ...RED_ART }; };
  await left.ask();
  assert.deepEqual(left.bar, []);

  const gone = rig();
  gone.net.answer = () => { gone.Player.stationMode = false; return { ...RED, ...RED_ART }; };
  await gone.ask();
  assert.deepEqual(gone.bar, []);
});
