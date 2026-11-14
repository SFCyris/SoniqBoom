// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The phone's page hears "server restarting" too (soniqboom/frontend/js/
// mobile/app.js, ``[m-restart:begin]``), run outside a browser with a fake
// WebSocket: the event tells the player to wait a failing play out
// (noteServerRestart), other events are ignored, and the socket comes back
// after a growing pause (1 s after the server's restart close).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const SRC = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
  '../../soniqboom/frontend/js/mobile/app.js'), 'utf8');
const m = SRC.match(/\/\/ \[m-restart:begin\][^\n]*\n([\s\S]*?)\/\/ \[m-restart:end\]/);
if (!m) throw new Error('m-restart region not found');
const { _listenForRestart } = new Function('Player', `${m[1]}\nreturn { _listenForRestart };`)({});

function env() {
  const sockets = [], waits = [], notes = [];
  class WS { constructor(url) { this.url = url; sockets.push(this); } }
  const doc = { visibilityState: 'visible', addEventListener() {} };
  const loc = { protocol: 'http:', host: '127.0.0.1:8501' };
  return { sockets, waits, notes, opts: { WS, doc, loc, wait: (fn, ms) => waits.push([fn, ms]), note: () => notes.push(1) } };
}

test('the restart event notes the restart; other events do nothing', () => {
  const e = env();
  _listenForRestart(e.opts);
  assert.equal(e.sockets.length, 1);
  assert.equal(e.sockets[0].url, 'ws://127.0.0.1:8501/api/library/ws');
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'scan_progress', running: true }) });
  e.sockets[0].onmessage({ data: 'not json' });
  assert.equal(e.notes.length, 0);
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'server_restarting' }) });
  assert.equal(e.notes.length, 1);
});

test('reconnects: 1 s after a restart close, else a pause that doubles up to 30 s', () => {
  const e = env();
  _listenForRestart(e.opts);
  e.sockets[0].onclose({ code: 1012 });
  assert.equal(e.waits.at(-1)[1], 1000);
  e.waits.at(-1)[0]();
  const seen = [];
  for (let i = 0; i < 6; i++) {
    e.sockets.at(-1).onclose({ code: 1006 });
    seen.push(e.waits.at(-1)[1]);
    e.waits.at(-1)[0]();
  }
  assert.deepEqual(seen, [4000, 8000, 16000, 30000, 30000, 30000]);
  e.sockets.at(-1).onopen();
  e.sockets.at(-1).onclose({ code: 1006 });
  assert.equal(e.waits.at(-1)[1], 2000);                         // back to the start once connected
});

test('the phone page starts listening once signed in', () => {
  const after = SRC.slice(SRC.indexOf('activate(initialView());'));
  assert.match(after.slice(0, 200), /_listenForRestart\(\);/);
});

test('an availability change re-reads the state (Hide leaves rows out on the phone too)', () => {
  const e = env();
  const avails = [];
  _listenForRestart({ ...e.opts, avail: (m) => avails.push(m) });
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'scan_progress', running: true }) });
  assert.equal(avails.length, 0);
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'availability_changed', token: 't', mode: 'mark' }) });
  assert.equal(avails.length, 1);
  assert.equal(avails[0].token, 't');
  assert.equal(e.notes.length, 0);
  // the default handler asks offline.js (imported by the phone's app.js) to re-read
  assert.match(SRC, /avail = \(\) => Offline\.refresh\(\)/);
  assert.match(SRC, /^import \{ Offline \} from '\.\.\/offline\.js';$/m);
});

test('a catalogue change, a station\'s song and its cover reach their hooks', () => {
  const e = env();
  const changed = [], radio = [];
  _listenForRestart({ ...e.opts, changed: (m) => changed.push(m), radio: (m) => radio.push(m.event) });
  const say = (m) => e.sockets[0].onmessage({ data: JSON.stringify(m) });
  say({ event: 'catalogue_changed', root: 'modland://', lib_gen: 7 });
  say({ event: 'radio_meta', sid: 'rb:1', title: 'A - B' });
  say({ event: 'radio_art', sid: 'rb:1', title: 'A - B', cover_url: '/api/art/x/lg.jpg' });
  say({ event: 'radio_status', sid: 'rb:1' });                   // (not one of them)
  assert.equal(changed.length, 1);
  assert.equal(changed[0].lib_gen, 7);
  assert.deepEqual(radio, ['radio_meta', 'radio_art']);
  assert.equal(e.notes.length, 0);
});

test('every connect asks once; a hook that throws breaks nothing', () => {
  const e = env();
  let opened = 0;
  _listenForRestart({ ...e.opts, opened: () => { opened++; throw new Error('x'); },
                      changed: () => { throw new Error('x'); }, radio: () => { throw new Error('x'); } });
  e.sockets[0].onopen();
  assert.equal(opened, 1);
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'catalogue_changed' }) });
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'radio_meta' }) });
  e.sockets[0].onclose({ code: 1006 });
  e.waits.at(-1)[0]();
  e.sockets[1].onopen();
  assert.equal(opened, 2);
  e.sockets[1].onmessage({ data: JSON.stringify({ event: 'server_restarting' }) });
  assert.equal(e.notes.length, 1);                               // still listening
});

test('the page wires the new events to the open list and the radio player', () => {
  assert.match(SRC, /changed = \(m\) => _catalogueChanged\(m\)/);
  assert.match(SRC, /radio = \(m\) => _radioSaid\(m\)/);
  assert.match(SRC, /opened = \(again\) => _socketOpened\(again\)/);
  assert.match(SRC, /art = \(id\) => noteArtChanged\(id\)/);
  assert.match(SRC, /function _catalogueChanged\(\) \{\s*document\.dispatchEvent\(new CustomEvent\('soniqboom:catalogue-changed'\)\);/);
  assert.match(SRC, /function _radioSaid\(m\) \{\s*if \(m\.event === 'radio_meta'\) MobileRadio\.noteMeta\(m\); else MobileRadio\.noteArt\(m\);/);
  assert.match(SRC, /function _socketOpened\(again\) \{\s*MobileRadio\.refreshNow\(\);\s*if \(again\) noteArtMissed\(\);\s*\}/);
  const lib = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)),
    '../../soniqboom/frontend/js/mobile/views/library.js'), 'utf8');
  assert.match(lib, /addEventListener\('soniqboom:catalogue-changed'/);
});

test('a cover that arrived, and a changed cover setting, reach the art slots', () => {
  const e = env();
  const art = [];
  _listenForRestart({ ...e.opts, art: (id) => art.push(id) });
  const say = (m) => e.sockets[0].onmessage({ data: JSON.stringify(m) });
  say({ event: 'art_ready', track_id: 'abc123' });
  say({ event: 'art_ready' });                                   // (names no track: nothing to ask)
  say({ event: 'art_ready', track_id: '' });
  say({ event: 'art_refresh' });
  say({ event: 'art_ready', track_id: 77 });
  assert.deepEqual(art, ['abc123', null, '77']);
  assert.equal(e.notes.length, 0);
});

test('a cover hook that throws breaks nothing', () => {
  const e = env();
  _listenForRestart({ ...e.opts, art: () => { throw new Error('x'); } });
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'art_ready', track_id: 'a' }) });
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'art_refresh' }) });
  e.sockets[0].onmessage({ data: JSON.stringify({ event: 'server_restarting' }) });
  assert.equal(e.notes.length, 1);
});

test('the open hook hears whether this is a reconnect', () => {
  const e = env();
  const seen = [];
  _listenForRestart({ ...e.opts, opened: (again) => seen.push(again) });
  e.sockets[0].onopen();
  e.sockets[0].onclose({ code: 1006 });
  e.waits.at(-1)[0]();
  e.sockets[1].onclose({ code: 1006 });                          // (never opened: still no reconnect seen)
  e.waits.at(-1)[0]();
  e.sockets[2].onopen();
  e.sockets[2].onclose({ code: 1012 });
  e.waits.at(-1)[0]();
  e.sockets[3].onopen();
  assert.deepEqual(seen, [false, true, true]);
});
