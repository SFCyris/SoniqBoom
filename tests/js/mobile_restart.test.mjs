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
