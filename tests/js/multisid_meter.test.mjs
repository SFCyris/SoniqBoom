// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A 2SID / 3SID tune's per-voice meter (2.3.1): its 6 / 9 voices come from
// the server's pass alone — the browser's WASM core renders one SID chip, and
// a 3-voice sidecar (the mono render keyed before 2.3.0, a one-chip pass) is
// never its meter.  vu/vumr.js: the chip / voice counts, the re-ask ladder and
// the /vu reader's voice check; app.js, player.js and the phone use them.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONT = resolve(HERE, '../../soniqboom/frontend');
const read = (p) => readFileSync(resolve(FRONT, p), 'utf8');
const VU = await import('../../soniqboom/frontend/js/vu/vumr.js');

const sid = (channels) => ({ id: 't', format: 'SID', channels });

test('a SID tune has 3 voices per chip; other tracks have no count known in advance', () => {
  assert.equal(VU.sidChips(sid(1)), 1);
  assert.equal(VU.sidChips(sid(2)), 2);
  assert.equal(VU.sidChips(sid(3)), 3);
  assert.equal(VU.sidChips({ format: 'SID' }), 1);              // (no channels stored: one chip)
  assert.equal(VU.sidChips(sid(7)), 3);                         // (clamped)
  assert.equal(VU.sidChips({ format: 'MOD', channels: 4 }), 0);
  assert.equal(VU.sidChips(null), 0);
  assert.deepEqual([1, 2, 3].map((c) => VU.vuExpectedVoices(sid(c))), [3, 6, 9]);
  assert.equal(VU.vuExpectedVoices({ format: 'ProTracker', channels: 4 }), 0);
  assert.deepEqual([1, 2, 3].map((c) => VU.multiSidTrack(sid(c))), [false, true, true]);
});

test('a multi-SID tune asks on at the last rung while it plays; the others stop at the end', () => {
  const L = VU.MULTI_SID_VU_RETRY_MS;
  assert.equal(VU.vuRetryDelay(sid(3), L, 0), 3000);
  assert.equal(VU.vuRetryDelay(sid(3), L, L.length + 500), 15000);
  assert.equal(VU.vuRetryDelay(sid(1), VU.SID_VU_RETRY_MS, VU.SID_VU_RETRY_MS.length), null);
  assert.equal(VU.vuRetryDelay({ format: 'MOD' }, VU.AMIGA_VU_RETRY_MS, 99), null);
});

test('a scope chunk some voice had not reached is asked again until every voice is in it', async () => {
  const MX = await import('../../soniqboom/frontend/js/voices/mixer.js');
  const player = { currentTime: 3.5, playing: true, audio: { volume: 1, muted: false },
                   voiceOverlay: () => null, on() {} };
  const asked = [];
  const answers = new Map();                         // (start → answers so far: the first two miss a voice)
  const fetchImpl = async (url) => {
    const q = new URL(url, 'http://x').searchParams;
    asked.push(`${q.get('kind')}:${q.get('start')}`);
    const k = q.get('start');
    answers.set(k, (answers.get(k) || 0) + 1);
    const n = Number(q.get('count'));
    const h = new Map([['X-Voices-Channels', '9'], ['X-Voices-Frames', String(n)],
                       ['X-Voices-Partial', answers.get(k) <= 2 ? '1' : null]]);
    const buf = new ArrayBuffer(9 * n * 2);
    const dv = new DataView(buf);
    for (let v = 0; v < 9; v++) for (let i = 0; i < n; i++) dv.setInt16((v * n + i) * 2, 1000 * (v + 1), true);
    return { ok: true, status: 200, headers: { get: (k) => h.get(k) ?? null }, arrayBuffer: async () => buf };
  };
  const m = MX.createVoiceMixer({ player, fetchImpl });
  m.attach({ id: 't', subsong: 0 }, { voices: Array.from({ length: 9 }, (_, i) => `V${i + 1}`),
                                      rate: 44100, scope_rate: 22050, scope: true });
  const out = new Float32Array(64);
  try {
    m.wantScopes(true);
    const until = Date.now() + 2200;
    while (Date.now() < until) { m.scopeWindow(0, 64, out); await new Promise((r) => setTimeout(r, 100)); }
    const cur = `scope:${3 * 22050}`;
    const again = asked.filter((a) => a === cur).length;
    assert.equal(again, 3, `twice partial, then complete — never again (${asked.join(', ')})`);
    assert.ok(m.scopeWindow(8, 64, out) && Math.abs(out[63] - 9000 / 32768) < 1e-6, 'voice 9 reads its own samples');
  } finally { m.wantScopes(false); m.detach(); }
});

test('a multi-SID tune is asked again often enough for the server pass, for minutes', () => {
  const L = VU.MULTI_SID_VU_RETRY_MS;
  const total = L.reduce((a, b) => a + b, 0);
  assert.ok(total >= 240000, `the ladder ends after ${total} ms`);
  assert.ok(Math.max(...L) <= 15000 && L.slice(0, 10).every((ms) => ms <= 3000));
  assert.equal(VU.vuRetryLadder(sid(3), false), L);
  assert.equal(VU.vuRetryLadder(sid(1), false), VU.SID_VU_RETRY_MS);
  assert.equal(VU.vuRetryLadder({ format: 'MOD' }, true), VU.AMIGA_VU_RETRY_MS);
});

function vumr(channels, frames = 2) {
  const buf = new ArrayBuffer(16 + channels + frames * channels);
  const dv = new DataView(buf);
  [...'VUMR'].forEach((c, i) => dv.setUint8(i, c.charCodeAt(0)));
  dv.setUint8(4, 1); dv.setUint8(5, channels);
  dv.setUint32(8, 30, true); dv.setUint32(12, frames, true);
  return buf;
}

async function withFetch(body, fn) {
  const urls = [];
  const saved = globalThis.fetch;
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return new Response(body, { status: body ? 200 : 204 });
  };
  try { return { got: await fn(), urls }; } finally { globalThis.fetch = saved; }
}

test('the /vu reader turns away a sidecar with fewer voices, and a multi-SID meter has its own URL', async () => {
  const f = VU.createVumrFetcher();
  // a 3SID tune, an old 3-voice sidecar: not its meter — "not yet"
  let r = await withFetch(vumr(3), () => f.fetch('t', 0, { voices: 9 }));
  assert.equal(r.got, null);
  assert.match(r.urls[0], /\/api\/tracks\/t\/vu\?subsong=0&voices=9$/);
  // its 9 voices: the meter
  r = await withFetch(vumr(9), () => f.fetch('t', 0, { voices: 9 }));
  assert.equal(r.got.channels, 9);
  // a row that doesn't carry the tune's chips yet (3 expected): the server's
  // 9-voice meter is the tune's — taken
  r = await withFetch(vumr(9), () => f.fetch('t', 0, { voices: 3 }));
  assert.equal(r.got.channels, 9);
  // a one-chip SID: the URL it always had (its cached answers stay valid)
  r = await withFetch(vumr(3), () => f.fetch('t', 0, { voices: 3 }));
  assert.equal(r.got.channels, 3);
  assert.match(r.urls[0], /\/vu\?subsong=0$/);
  // no count asked (a tracker): any sidecar, as before
  r = await withFetch(vumr(4), () => f.fetch('t', undefined));
  assert.equal(r.got.channels, 4);
  assert.match(r.urls[0], /\/vu$/);
});

test('the desktop and the phone ask with the count, and the one-chip WASM meter keeps off multi-SID tunes', () => {
  const app = read('js/app.js');
  assert.match(app, /_fetchVUMR\(track\.id, track\.subsong,\s*\{ start: !serverPassTrack\(track\), voices: vuExpectedVoices\(track\) \}\)/);
  assert.match(app, /_fetchVUMR\(track\.id, track\.subsong, \{ voices: vuExpectedVoices\(track\) \}\)/);
  assert.match(app, /!_sidVuPending && !multiSidTrack\(track\)\) \{\s*\n\s*_debouncedSidVU\(track, key\);/);
  assert.match(app, /if \(_sidVuUnavailable \|\| multiSidTrack\(track\)\) return false;/);
  assert.match(app, /if \(multiSidTrack\(Player\.currentTrack\)\) return;/);
  assert.match(app, /multiSidTrack\(track\) \? MULTI_SID_VU_RETRY_MS/);
  const player = read('js/player.js');
  assert.match(player, /isC64SidTrack\(track\) && !multiSidTrack\(track\) && sidWasmSupported\(\)/);
  const np = read('js/mobile/views/np_visuals.js');
  assert.equal((np.match(/voices: vuExpectedVoices\(t\)/g) || []).length, 2);
  assert.match(np, /_poll\(t, key, vuRetryLadder\(t, amiga\), amiga, 0, 0\)/);
  assert.match(read('js/mobile/views/nowplaying.js'),
               /const ch = Number\(t\.channels\) > 0 \? \(vuExpectedVoices\(t\) \|\| Number\(t\.channels\)\) : 0;/);
});
