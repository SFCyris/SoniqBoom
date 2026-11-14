// The phone page's list re-read after a ``catalogue_changed``: which rows count
// as changed.  The region ``[m-reread:begin]`` of mobile/views/library.js is
// run as is.  A list row (GET /api/tracks) leaves out the fields a track has
// no value for; the row read back by id (POST /api/tracks/meta/batch) spells
// each of them out with its empty default.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/mobile/views/library.js'), 'utf8');
const m = SRC.match(/\/\/ \[m-reread:begin\][^\n]*\n([\s\S]*?)\/\/ \[m-reread:end\]/);
if (!m) throw new Error('m-reread region not found');
const { _takeFresh, _applyFresh, _unseen } = new Function(`${m[1]}\nreturn { _takeFresh, _applyFresh, _unseen };`)();

// A list row as the server sends it, and the same track read back by id.
const listRow = () => ({ id: 'a1', title: 'braah', path: 'ftp://x:/Protracker/Whiskas/braah.mod',
  format: 'mod', duration: 240, artist: 'Whiskas' });
const freshRow = (over = {}) => ({ id: 'a1', title: 'braah', path: 'ftp://x:/Protracker/Whiskas/braah.mod',
  format: 'mod', duration: 240, artist: 'Whiskas',
  album: '', album_artist: '', year: null, bpm: null, rating: 0, play_count: 0, mtime: 0,
  genre: '', genre_derived: [], songdb_fields: {}, favorite: false, ...over });

test('the same track read back with its empty defaults is no change', () => {
  const t = listRow();
  const before = JSON.stringify(t);
  assert.equal(_takeFresh(t, freshRow()), false);
  assert.equal(JSON.stringify(t), before);              // (and the row took no empty fields)
});

test('a field the list row lacks arrives', () => {
  const t = listRow();
  assert.equal(_takeFresh(t, freshRow({ album: 'Road Rash', year: 1992 })), true);
  assert.equal(t.album, 'Road Rash');
  assert.equal(t.year, 1992);
  assert.equal('bpm' in t, false);
});

test('a field the list row holds changes, and one that was withdrawn empties', () => {
  const t = { ...listRow(), album: 'Old Album' };
  assert.equal(_takeFresh(t, freshRow({ artist: 'Whiskas of Orange', album: '' })), true);
  assert.equal(t.artist, 'Whiskas of Orange');
  assert.equal(t.album, '');
});

test('lists and maps compare by content', () => {
  const t = { ...listRow(), genre_derived: ['Game', 'SNES'], songdb_fields: { artist: 1 } };
  assert.equal(_takeFresh(t, freshRow({ genre_derived: ['Game', 'SNES'], songdb_fields: { artist: 1 } })), false);
  assert.equal(_takeFresh(t, freshRow({ genre_derived: ['Game', 'SNES', 'Road Rash'],
    songdb_fields: { artist: 1 } })), true);
  assert.deepEqual(t.genre_derived, ['Game', 'SNES', 'Road Rash']);
});

test('a length found on this page stays when the server has none', () => {
  const t = listRow();
  assert.equal(_takeFresh(t, freshRow({ duration: 0 })), false);
  assert.equal(t.duration, 240);
  assert.equal(_takeFresh(t, freshRow({ duration: 251.5 })), true);
  assert.equal(t.duration, 251.5);
});

test('the track list takes its rows through this rule', () => {
  assert.match(SRC, /if \(_takeFresh\(t, row\)\) changed\.add\(t\);/);
  assert.match(SRC, /const \{ changed, gone \} = _applyFresh\(list, asked, fresh, Player\.currentTrack\);/);
});

// Three loaded rows, the rows read back for them, and who was asked.
const rows3 = () => [
  { id: 'a1', title: 'one', mtime: 0 }, { id: 'b2', title: 'two', mtime: 0 }, { id: 'c3', title: 'three', mtime: 0 }];
const back = (...rows) => new Map(rows.map((r) => [r.id, r]));

test('only the rows that took a field count as changed; a row no longer held counts as gone', () => {
  const list = rows3();
  const asked = new Set(['a1', 'b2', 'c3']);
  const res = _applyFresh(list, asked, back({ id: 'a1', title: 'one', mtime: 0, album: '' },
                                            { id: 'b2', title: 'two', mtime: 0, artist: 'Whiskas' }), null);
  assert.deepEqual([...res.changed], [list[1]]);
  assert.equal(list[1].artist, 'Whiskas');
  assert.equal(res.gone, 1);                                   // c3 left the library
  assert.equal(list[2].title, 'three');                        // (its row is the caller's to drop)
});

test('a row of a page that arrived meanwhile is neither read nor counted', () => {
  const list = [...rows3(), null, { id: 'd4', title: 'late' }];
  const res = _applyFresh(list, new Set(['a1', 'b2', 'c3']),
                          back({ id: 'a1', title: 'ONE' }, { id: 'b2', title: 'two' }, { id: 'c3', title: 'three' },
                               { id: 'd4', title: 'changed' }), null);
  assert.deepEqual([...res.changed].map((t) => t.id), ['a1']);
  assert.equal(res.gone, 0);
  assert.equal(list[4].title, 'late');
});

test('the playing tune not read yet waits for the player; once read, or another tune, it is taken', () => {
  const fresh = () => back({ id: 'a1', title: 'one', mtime: 0, artist: 'A' }, { id: 'b2', title: 'two', mtime: 0, artist: 'B' },
                           { id: 'c3', title: 'three', mtime: 0, artist: 'C' });
  const asked = new Set(['a1', 'b2', 'c3']);
  // the queue's own object, and a separate copy of the same tune: both wait
  let list = rows3();
  let res = _applyFresh(list, asked, fresh(), list[1]);
  assert.deepEqual([...res.changed].map((t) => t.id), ['a1', 'c3']);
  assert.equal('artist' in list[1], false);
  list = rows3();
  res = _applyFresh(list, asked, fresh(), { id: 'b2', title: 'two' });          // (no mtime at all)
  assert.deepEqual([...res.changed].map((t) => t.id), ['a1', 'c3']);
  assert.equal(res.gone, 0);
  // read meanwhile (mtime set), a tune that is not in the list, nothing playing
  for (const playing of [{ id: 'b2', mtime: 1731500000 }, { id: 'zz', mtime: 0 }, null, undefined]) {
    list = rows3();
    res = _applyFresh(list, asked, fresh(), playing);
    assert.deepEqual([...res.changed].map((t) => t.id), ['a1', 'b2', 'c3'], JSON.stringify(playing));
  }
  // a playing tune that left the library still counts as gone
  list = rows3();
  res = _applyFresh(list, asked, back({ id: 'a1', title: 'one' }, { id: 'c3', title: 'three' }), list[1]);
  assert.equal(res.gone, 1);
});

// ── A further page of a list whose order moved ───────────────────────────────

test('a further page gives only the rows the list does not hold', () => {
  const have = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  const page = [{ id: 'c' }, { id: 'd' }, { id: 'b' }, { id: 'e' }];
  assert.deepEqual(_unseen(have, page).map(t => t.id), ['d', 'e']);
  assert.equal(have.length, 3);                           // (the list is not written)
});

test('a page of new rows passes as it is, in its order', () => {
  const page = [{ id: 'x' }, { id: 'y' }];
  assert.deepEqual(_unseen([{ id: 'a' }], page), page);
  assert.deepEqual(_unseen([], page), page);
});

test('a row twice in one page is shown once; a page of known rows gives none', () => {
  assert.deepEqual(_unseen([], [{ id: 'x' }, { id: 'x' }, { id: 'y' }]).map(t => t.id), ['x', 'y']);
  assert.deepEqual(_unseen([{ id: 'a' }, { id: 'b' }], [{ id: 'b' }, { id: 'a' }]), []);
});

test('the list page appends only unseen rows, counts the whole page, and asks on when none came', () => {
  const fn = SRC.match(/async function loadTrackPage\(\) \{[\s\S]*?\n  \}\n/)[0];
  assert.match(fn, /const rows = _unseen\(tracks, page\);/);
  assert.match(fn, /if \(rows\.length\) appendTracks\(rows\);\s*offset \+= page\.length;/);
  assert.match(fn, /again = !rows\.length && !exhausted;/);
  assert.match(fn, /if \(again && gen === viewGen\) loadTrackPage\(\);/);
});

test('play from here starts at the picked track inside the window, else the loaded rows', () => {
  const fn = SRC.match(/async function playFrom\(startIdx\) \{[\s\S]*?\n  \}\n/)[0];
  assert.match(fn, /const pick = tracks\[startIdx\];/);
  assert.match(fn, /win\.findIndex\(t => t && t\.id === pick\.id\)/);
  assert.match(fn, /const starts = \[pick \? _posOf\.get\(pick\) : undefined, startIdx\]/);
  assert.match(fn, /if \(at >= 0\) \{ Player\.setQueue\(win, at, \{ source: \{ \.\.\.source, offset: from \} \}\); return; \}/);
  assert.match(fn, /Player\.setQueue\(tracks, startIdx\);/);
});

// ── Play from here, run as it is ─────────────────────────────────────────────

/** ``playFrom`` out of the file, over the given list / answer.  ``answer`` is
 *  one answer for every request, or a function of the address; ``pos`` holds
 *  where a row stood in the server's order (row → position). */
function playFromOver({ tracks, answer, crumb = null, pos = new Map() }) {
  const fn = SRC.match(/async function playFrom\(startIdx\) \{[\s\S]*?\n  \}\n/)[0];
  const calls = [], urls = [];
  const Player = { setQueue: (...a) => calls.push(a) };
  const fetch = async (url) => {
    urls.push(url);
    const a = typeof answer === 'function' ? answer(url) : answer;
    if (a instanceof Error) throw a;
    return a === null ? { ok: false, json: async () => null } : { ok: true, json: async () => a };
  };
  const playFrom = new Function('crumb', 'tracks', 'QUEUE_WINDOW', 'Player', 'fetch', '_posOf',
    `${fn}\nreturn playFrom;`)(crumb, tracks, 100, Player, fetch, pos);
  return { playFrom, calls, urls };
}
/** An answer per window start (``offset`` of the address); none listed → no rows. */
const byOffset = (wins) => (url) => {
  const from = Number(new URL(url, 'http://x').searchParams.get('offset'));
  return from in wins ? wins[from] : [];
};
const rowsOf = (ids) => ids.map(id => ({ id }));

test('play from here: the window starts with the picked track', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const win = rowsOf(['c', 'd', 'e']);
  const { playFrom, calls, urls } = playFromOver({ tracks, answer: win });
  await playFrom(2);
  assert.equal(urls[0], '/api/tracks?limit=100&offset=2');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], win);
  assert.equal(calls[0][1], 0);
  assert.equal(calls[0][2].source.offset, 2);
});

test('play from here: the picked track is further on in the window (rows were repeated ahead of it)', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const win = rowsOf(['x', 'y', 'c', 'd']);              // (the window starts two rows before it)
  const { playFrom, calls } = playFromOver({ tracks, answer: win });
  await playFrom(2);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], win);
  assert.equal(calls[0][1], 2);                           // (the tapped track, not the window's first)
  assert.equal(calls[0][0][calls[0][1]].id, 'c');
  assert.equal(calls[0][2].source.offset, 2);             // (the window's own start)
});

test('play from here: the picked track is not in the window → the loaded rows, from it', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const { playFrom, calls } = playFromOver({ tracks, answer: rowsOf(['x', 'y', 'z']) });
  await playFrom(2);
  assert.deepEqual(calls, [[tracks, 2]]);
});

test('play from here: no answer, a refused answer or one that is no list → the loaded rows', async () => {
  for (const answer of [null, new Error('offline'), { tracks: [] }]) {
    const tracks = rowsOf(['a', 'b', 'c']);
    const { playFrom, calls } = playFromOver({ tracks, answer });
    await playFrom(1);
    assert.deepEqual(calls, [[tracks, 1]]);
  }
});

test('play from here: a filtered list asks its own address with its filter', async () => {
  const tracks = rowsOf(['a', 'b']);
  const crumb = { field: 'artist', value: 'Whiskas', label: 'Whiskas' };
  const { playFrom, calls, urls } = playFromOver({ tracks, answer: rowsOf(['b', 'c']), crumb });
  await playFrom(1);
  assert.equal(urls[0], '/api/search/filter?artist=Whiskas&limit=100&offset=1');
  assert.equal(calls[0][1], 0);
  assert.deepEqual(calls[0][2].source.ordered, { url: '/api/search/filter', params: { artist: 'Whiskas' } });
  assert.equal(calls[0][2].source.label, 'Whiskas');
});

// ── Play from here, where the row stood in the server's order ────────────────

test('play from here: rows were left out ahead of the track → the window starts where it stood', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const win = rowsOf(['c', 'd', 'e']);
  const pos = new Map([[tracks[2], 150]]);                // (148 repeated rows never entered the list)
  const { playFrom, calls, urls } = playFromOver({ tracks, pos, answer: byOffset({ 150: win }) });
  await playFrom(2);
  assert.deepEqual(urls, ['/api/tracks?limit=100&offset=150']);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], win);
  assert.equal(calls[0][1], 0);
  assert.equal(calls[0][2].source.offset, 150);           // (the queue goes on from the window's start)
  assert.equal(calls[0][2].source.label, 'All Tracks');
});

test('play from here: not where it stood → its place in the list is asked next', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const win = rowsOf(['b', 'c', 'd']);
  const pos = new Map([[tracks[2], 150]]);
  const { playFrom, calls, urls } = playFromOver({
    tracks, pos, answer: byOffset({ 150: rowsOf(['x', 'y']), 2: win }),
  });
  await playFrom(2);
  assert.deepEqual(urls, ['/api/tracks?limit=100&offset=150', '/api/tracks?limit=100&offset=2']);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], win);
  assert.equal(calls[0][1], 1);
  assert.equal(calls[0][2].source.offset, 2);
});

test('play from here: in neither window → the loaded rows, after two requests and no more', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const pos = new Map([[tracks[2], 150]]);
  const { playFrom, calls, urls } = playFromOver({ tracks, pos, answer: byOffset({}) });
  await playFrom(2);
  assert.equal(urls.length, 2);
  assert.deepEqual(calls, [[tracks, 2]]);
});

test('play from here: a refused first answer is not asked a second time', async () => {
  for (const answer of [null, new Error('offline'), { tracks: [] }]) {
    const tracks = rowsOf(['a', 'b', 'c', 'd']);
    const pos = new Map([[tracks[2], 150]]);
    const { playFrom, calls, urls } = playFromOver({ tracks, pos, answer });
    await playFrom(2);
    assert.equal(urls.length, 1);
    assert.deepEqual(calls, [[tracks, 2]]);
  }
});

test('play from here: the row stands where the list has it → one request', async () => {
  const tracks = rowsOf(['a', 'b', 'c', 'd']);
  const pos = new Map(tracks.map((t, i) => [t, i]));
  const { playFrom, calls, urls } = playFromOver({ tracks, pos, answer: byOffset({}) });
  await playFrom(3);
  assert.deepEqual(urls, ['/api/tracks?limit=100&offset=3']);
  assert.deepEqual(calls, [[tracks, 3]]);
});

test('play from here: a position that is no whole number from 0 on is not asked', async () => {
  for (const bad of [-1, 2.5, '7', null, NaN]) {
    const tracks = rowsOf(['a', 'b', 'c']);
    const { playFrom, urls } = playFromOver({
      tracks, pos: new Map([[tracks[1], bad]]), answer: byOffset({ 1: rowsOf(['b']) }),
    });
    await playFrom(1);
    assert.deepEqual(urls, ['/api/tracks?limit=100&offset=1']);
  }
});

// ── A page of the list, run as it is ─────────────────────────────────────────

/** ``loadTrackPage`` out of the file over pages keyed by their ``offset``
 *  (none listed → an empty page; ``null`` → a refused request). */
function pagerOver({ pages, pageSize = 3, crumb = null }) {
  const fn = SRC.match(/async function loadTrackPage\(\) \{[\s\S]*?\n  \}\n/)[0];
  const urls = [];
  const fetch = async (url) => {
    urls.push(url);
    const from = Number(new URL(url, 'http://x').searchParams.get('offset'));
    const p = from in pages ? pages[from] : [];
    return p === null ? { ok: false, status: 500, json: async () => null } : { ok: true, status: 200, json: async () => p };
  };
  const el = () => {
    const set = new Set(['hidden']);
    return { textContent: '', classList: { add: c => set.add(c), remove: c => set.delete(c), has: c => set.has(c) } };
  };
  const loadEl = el(), emptyEl = el();
  const api = new Function('fetch', '_unseen', 'PAGE_SIZE', 'loadEl', 'emptyEl', 'crumb', 'console', `
    let loadingGen = -1, viewGen = 1, exhausted = false, offset = 0;
    const tracks = [], _posOf = new WeakMap(), appended = [], errors = [];
    const appendTracks = (rows) => { appended.push(rows.map(t => t.id)); tracks.push(...rows); };
    const showLoadError = (gen) => { errors.push(gen); };
    ${fn}
    return { load: loadTrackPage, tracks, appended, errors, pos: (t) => _posOf.get(t),
             leave: () => { viewGen++; },
             state: () => ({ offset, exhausted, busy: loadingGen !== -1 }) };`)(
    fetch, _unseen, pageSize, loadEl, emptyEl, crumb, { error() {} });
  /** Until no page is on its way (a follow-up request is not awaited by its caller). */
  const settle = async () => {
    for (let i = 0; i < 50; i++) {
      await new Promise(r => setImmediate(r));
      if (!api.state().busy) return;
    }
    throw new Error('a page never came back');
  };
  return { ...api, urls, loadEl, emptyEl, settle };
}
const ids = (rows) => rows.map(t => t.id);

test('a page: only its new rows join the list, the whole page counts, each row keeps where it stood', async () => {
  const p = pagerOver({ pages: { 0: rowsOf(['a', 'b', 'c']), 3: rowsOf(['c', 'd', 'b']), 6: rowsOf(['e']) } });
  await p.load(); await p.settle();
  assert.deepEqual(ids(p.tracks), ['a', 'b', 'c']);
  assert.deepEqual(p.state(), { offset: 3, exhausted: false, busy: false });
  await p.load(); await p.settle();
  assert.deepEqual(ids(p.tracks), ['a', 'b', 'c', 'd']);  // ('c' and 'b' came a second time)
  assert.deepEqual(p.appended, [['a', 'b', 'c'], ['d']]);
  assert.equal(p.state().offset, 6);                      // (the server's rows, not the list's)
  assert.deepEqual(p.tracks.map(p.pos), [0, 1, 2, 4]);    // ('d' is the list's 4th row and the server's 5th)
  assert.deepEqual(p.urls, ['/api/tracks?limit=3&offset=0', '/api/tracks?limit=3&offset=3']);
});

test('a page of rows the list holds already is followed by the next one, with no scroll', async () => {
  const p = pagerOver({ pages: {
    0: rowsOf(['a', 'b', 'c']), 3: rowsOf(['b', 'c', 'a']), 6: rowsOf(['c', 'a', 'b']), 9: rowsOf(['d', 'e', 'f']),
  } });
  await p.load(); await p.settle();
  await p.load(); await p.settle();                       // (one call: the pages at 3, 6 and 9)
  assert.deepEqual(p.urls.map(u => Number(u.split('offset=')[1])), [0, 3, 6, 9]);
  assert.deepEqual(ids(p.tracks), ['a', 'b', 'c', 'd', 'e', 'f']);
  assert.deepEqual(p.tracks.map(p.pos), [0, 1, 2, 9, 10, 11]);
  assert.deepEqual(p.state(), { offset: 12, exhausted: false, busy: false });
});

test('a short page, or an empty one, ends the list: nothing more is asked', async () => {
  const p = pagerOver({ pages: { 0: rowsOf(['a', 'b', 'c']), 3: rowsOf(['c', 'b']) } });
  await p.load(); await p.settle();
  await p.load(); await p.settle();                       // (a short page of known rows: the end, no follow-up)
  assert.equal(p.state().exhausted, true);
  assert.equal(p.urls.length, 2);
  await p.load(); await p.settle();
  assert.equal(p.urls.length, 2);
  assert.deepEqual(ids(p.tracks), ['a', 'b', 'c']);

  const q = pagerOver({ pages: {} });
  await q.load(); await q.settle();
  assert.equal(q.state().exhausted, true);
  assert.equal(q.emptyEl.classList.has('hidden'), false); // (an empty library says so)
  assert.equal(q.emptyEl.textContent, 'No tracks yet — add music from the desktop app.');
  assert.deepEqual(q.appended, []);
});

test('a page that comes after the listener left the list is dropped', async () => {
  const p = pagerOver({ pages: { 0: rowsOf(['a', 'b', 'c']) } });
  const going = p.load();
  p.leave();
  await going;
  assert.deepEqual(p.tracks, []);
  assert.equal(p.state().offset, 0);
});

test('a refused page adds nothing, keeps its place and can be asked again', async () => {
  const pages = { 0: rowsOf(['a', 'b', 'c']), 3: null };
  const p = pagerOver({ pages });
  await p.load(); await p.settle();
  await p.load(); await p.settle();
  assert.deepEqual(p.state(), { offset: 3, exhausted: false, busy: false });
  assert.deepEqual(p.errors, []);                         // (the full-page error is the first page's only)
  pages[3] = rowsOf(['d']);
  await p.load(); await p.settle();
  assert.deepEqual(ids(p.tracks), ['a', 'b', 'c', 'd']);
  assert.equal(p.urls.length, 3);
});

test('a filtered list pages its own address with its filter', async () => {
  const crumb = { field: 'artist', value: 'Whiskas', label: 'Whiskas' };
  const p = pagerOver({ pages: { 0: rowsOf(['a']) }, crumb });
  await p.load(); await p.settle();
  assert.deepEqual(p.urls, ['/api/search/filter?limit=3&offset=0&artist=Whiskas']);
  assert.deepEqual(ids(p.tracks), ['a']);
});
