// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The voice mixer's DOM-free parts (voices/mixer.js) and the per-voice meter's
// voice names (vu/vumr.js).  Run: node --test tests/js/voices_mixer.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '../..');
const read = (p) => readFileSync(resolve(ROOT, p), 'utf8');
const VU = await import('../../soniqboom/frontend/js/vu/vumr.js');
const MX = await import('../../soniqboom/frontend/js/voices/mixer.js');

function vumr(channels, frames, { names = null, flags } = {}) {
  const tail = names ? names.map((n) => [n.length, ...Buffer.from(n, 'utf8')]).flat() : [];
  const buf = new ArrayBuffer(16 + channels + frames * channels + tail.length);
  const u = new Uint8Array(buf);
  u.set([0x56, 0x55, 0x4d, 0x52, 1, channels, flags ?? (names ? 1 : 0), 0]);
  new DataView(buf).setUint32(8, 30, true);
  new DataView(buf).setUint32(12, frames, true);
  for (let i = 0; i < frames * channels; i++) u[16 + channels + i] = (i * 37) % 256;
  u.set(tail, 16 + channels + frames * channels);
  return buf;
}

test('a sidecar with names: parsed, and the levels are as without them', () => {
  const names = ['Square 1', 'Square 2', 'Triangle'];
  const a = VU.parseVUMR(vumr(3, 10, { names }));
  const b = VU.parseVUMR(vumr(3, 10));
  assert.deepEqual(a.names, names);
  assert.equal(b.names, null);
  assert.deepEqual([...a.samples], [...b.samples]);
  assert.equal(a.frames, 10);
});

test('a damaged names block reads as no names, never as a failed sidecar', () => {
  const buf = vumr(2, 4, { names: ['A', 'B'] });
  const cut = buf.slice(0, buf.byteLength - 1);              // last name truncated
  const p = VU.parseVUMR(cut);
  assert.ok(p, 'the levels still parse');
  assert.equal(p.names, null);
  // flag set, no block at all
  assert.equal(VU.parseVUMR(vumr(2, 4, { flags: 1 })).names, null);
});

test('column labels: a short voice name where there is room, else the number', () => {
  const v = { names: ['Square 1', 'Triangle', 'DSP 3', 'FM 2', 'OPL 7 BD', 'Noise'] };
  assert.deepEqual([0, 1, 2, 3, 4, 5].map((i) => VU.vuVoiceLabel(v, i, 'roomy')),
                   ['SQ1', 'TRI', 'DSP3', 'FM2', 'BD', 'NOI']);
  assert.equal(VU.vuVoiceLabel(v, 2, 'compact'), '3');
  assert.equal(VU.vuVoiceLabel({ names: null }, 0, 'roomy'), 'CH:1');
  for (const n of ['Square 1', 'OPL 18', 'Voice 12', 'Pulse Wave 3'])
    assert.ok(VU.shortVoiceName(n).length <= 6, n);
});

test('the server builds the meters on ask for Amiga, libgme and AdLib tracks, not SID or trackers', () => {
  assert.equal(VU.serverPassTrack({ format: 'NSF' }), true);
  assert.equal(VU.serverPassTrack({ format: 'SPC' }), true);
  assert.equal(VU.serverPassTrack({ format: 'VGZ' }), true);
  assert.equal(VU.serverPassTrack({ format: 'AdLib Tracker 2' }), true);
  assert.equal(VU.serverPassTrack({ format: 'SID' }), false);
  assert.equal(VU.serverPassTrack({ format: 'MOD' }), false);
  assert.equal(VU.serverPassTrack({ format: 'MP3' }), false);
});

test('Mute / Solo → the voices heard', () => {
  assert.equal(MX.allMask(5), 0b11111);
  assert.equal(MX.allMask(32), 0xFFFFFFFF);
  assert.equal(MX.audibleMask(5, 0, 0), 0b11111);
  assert.equal(MX.audibleMask(5, 0b00001, 0), 0b11110);            // mute 1
  assert.equal(MX.audibleMask(5, 0, 0b00100), 0b00100);            // solo 3
  assert.equal(MX.audibleMask(5, 0b00100, 0b00110), 0b00010);      // solo 2+3, mute 3
  assert.equal(MX.audibleMask(5, 0b11111, 0), 0);                  // all muted
  assert.equal(MX.audibleMask(3, 0, 0b11000), 0);                  // a solo beyond the voices
});

test('PCM chunks: int16 little-endian interleaved → one Float32Array per channel', () => {
  const buf = new ArrayBuffer(12);
  const dv = new DataView(buf);
  [0, 16384, -32768, 32767, -1, 1].forEach((v, i) => dv.setInt16(i * 2, v, true));
  const [l, r] = MX.pcmToChannels(buf, 2, 3);
  assert.deepEqual([...l], [0, -1, -1 / 32768]);
  assert.deepEqual([...r], [0.5, 32767 / 32768, 1 / 32768]);
  const [m] = MX.pcmToChannels(buf, 1, 6);
  assert.equal(m.length, 6);
});

test('the mixer: a muted voice plays the server render in place of the reference, all on gives it back', async () => {
  // A fake AudioContext / player: enough to see what is scheduled when.
  const started = [];
  const param = (log) => ({ value: 1, cancelScheduledValues() {},
    setValueAtTime(v, t) { this.value = v; },
    linearRampToValueAtTime(v, t) { this.value = v; if (log) log.push([v, t]); } });
  const ctx = {
    currentTime: 10,
    createGain() { return { gain: param(null), connect() {} }; },
    createBuffer(ch, n, rate) { return { ch, n, rate, copyToChannel() {} }; },
    createBufferSource() {
      const s = { connect() {}, start(when, off) { started.push({ when, off, n: s.buffer.n }); }, stop() {} };
      return s;
    },
  };
  const gateCalls = [];
  const gate = { gain: param(gateCalls) };
  const handlers = {};
  const player = {
    currentTime: 3, playing: true, audio: { volume: 0.5, muted: false },
    voiceOverlay: () => ({ ctx, input: {}, gate }),
    on(evt, fn) { (handlers[evt] ||= []).push(fn); },
  };
  const urls = [];
  const fetchImpl = async (url) => {
    urls.push(url);
    const q = new URL(url, 'http://x').searchParams;
    const frames = Number(q.get('count'));
    const h = new Map([['X-Voices-Channels', '2'], ['X-Voices-Frames', String(frames)],
                       ['X-Voices-Total', '9999999']]);
    return { ok: true, status: 200, headers: { get: (k) => h.get(k) ?? null },
             arrayBuffer: async () => new ArrayBuffer(frames * 4) };
  };
  const m = MX.createVoiceMixer({ player, fetchImpl });
  m.attach({ id: 't1', subsong: 2 }, { voices: ['A', 'B', 'C'], rate: 44100, scope_rate: 22050, scope: true });
  try {
  assert.equal(m.state, 'off');
  m.setAudible(0b110);                                 // mute voice 1
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(m.state, 'on');
  const q = new URL(urls[0], 'http://x').searchParams;
  assert.equal(q.get('kind'), 'mix');
  assert.equal(q.get('mask'), '6');
  assert.equal(q.get('subsong'), '2');
  // the first buffer starts where the tune will be when it plays (3 s + the lead)
  assert.equal(Number(q.get('start')), Math.round((3 + 0.35) * 44100));
  assert.equal(started[0].when, 10 + 0.35);
  // the reference's own path ramps out at that instant (5 ms)
  assert.deepEqual(gateCalls.at(-1), [0, 10 + 0.35]);
  // a seek while muted: the reference stays out (silence, never the muted voice)
  const n0 = gateCalls.length, u0 = urls.length;
  player.currentTime = 40;
  (handlers.seeked || []).forEach((f) => f());
  assert.ok(gateCalls.slice(n0).every(([v]) => v === 0), 'the gate opened on a re-anchor');
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(m.state, 'on');
  // the first chunk after the seek starts where the tune will be then; the
  // next one follows it back to back
  const starts = urls.slice(u0).map((u) => Number(new URL(u, 'http://x').searchParams.get('start')));
  assert.equal(starts[0], Math.round((40 + 0.35) * 44100));
  if (starts.length > 1) assert.equal(starts[1], starts[0] + 2 * 44100);
  m.setAudible(0b111);                                 // all on → the reference
  assert.equal(m.state, 'off');
  assert.equal(gateCalls.at(-1)[0], 1);
  } finally { m.detach(); }
});

test('the Voices tab: mounted by the Track Info panel, scopes through the viz engine', () => {
  const ti = read('soniqboom/frontend/js/trackinfo.js');
  assert.match(ti, /mountVoicesTab\(\{ overlay, tabBtn: tabVoices, pane: voicesPane, tabInfo \}\)/);
  assert.match(ti, /new CustomEvent\('trackinfo:tab', \{ detail: \{ tab \} \}\)/);
  const tab = read('soniqboom/frontend/js/voices/voices_tab.js');
  assert.match(tab, /registerViz\(\{\s*host: listEl, group: 'nowPlaying'/);
  const html = read('soniqboom/frontend/index.html');
  assert.match(html, /id="ti-tab-voices"[^>]*hidden/);
  assert.match(html, /id="ti-voices-pane"/);
  // the server's poll window covers the voices tab's poll too
  const py = read('soniqboom/api/voices.py');
  const f = py.match(/^_POLL_FRESH_S\s*=\s*([\d.]+)/m);
  const poll = tab.match(/const POLL_MS = (\d+)/);
  assert.ok(f && poll && Number(poll[1]) <= Number(f[1]) * 1000 - 5000);
});
