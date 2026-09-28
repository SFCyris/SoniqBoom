// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Runs the REAL queue state machine from soniqboom/frontend/js/player.js outside
// a browser.  player.js is one big DOM-bound closure, so the queue code is lifted
// out by its ``[queue-core:begin]`` / ``[queue-core:end]`` markers and compiled
// together with the few stubs it needs (audio element, playTrack, event bus,
// timers, fetch, localStorage).  Nothing here re-implements queue logic.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
// SB_PLAYER_JS: point the harness at another copy (mutation-testing the tests).
const PLAYER_JS = process.env.SB_PLAYER_JS
  || resolve(HERE, '../../soniqboom/frontend/js/player.js');

function extractCore() {
  const src = readFileSync(PLAYER_JS, 'utf8');
  const parts = [];
  const re = /\/\/ \[queue-core:begin\][^\n]*\n([\s\S]*?)\/\/ \[queue-core:end\]/g;
  let m;
  while ((m = re.exec(src))) parts.push(m[1]);
  if (parts.length !== 3) throw new Error(`expected 3 queue-core regions, found ${parts.length}`);
  return parts.join('\n');
}

const FACTORY = new Function('env', `
  'use strict';
  const { localStorage, fetch, setTimeout, clearTimeout, AbortSignal } = env;
  // Seeded randomness (shuffle seeds, Fisher–Yates) so every run is the same run.
  const Math = Object.create(globalThis.Math);
  Math.random = env.random;
  const audioListeners = {};
  const audio = { pause() {}, play() { return Promise.resolve(); }, removeAttribute() {}, load() {},
                  getAttribute(k) { return k === 'src' ? (this.src || null) : null; },
                  addEventListener(ev, fn) { (audioListeners[ev] ||= []).push(fn); },
                  currentTime: 0, src: '' };
  const Toast = { error: (m) => env.toasts.push(['error', m]), info: (m) => env.toasts.push(['info', m]),
                  ok: (m) => env.toasts.push(['ok', m]),
                  action: (m, label, fn) => { const el = { m }; env.toasts.push(['action', m, label, fn, el]); return el; },
                  dismiss: (el) => env.dismissed.push(el) };
  const plays = env.plays, events = env.events;
  function playTrack(t, opts = {}) {                       // mirrors the real one's bookkeeping
    _playSeq++; _noteStart(); if (!opts.auto) _explicitPick = true;
    trackId = t && t.id; plays.push(t && t.id);
  }
  function emit(name, data) { events.push([name, data]); }
  function _currentTime() { return env.currentTime || 0; }
  function _recordAdvance(skip) { env.advances.push(!!skip); }
  function _invalidatePContinue() {}
  function _streamUrlFor() { return ''; }
  let _seekOffset = 0, _isTranscoded = false, trackId = null, _extAudio = false, _stationMode = false;
  ${extractCore()}
  return {
    setQueue, playSource, next, prev, _advance, toggleShuffle, toggleRepeat,
    addToQueue, playNext, removeFromQueue, moveInQueue, setRadioActive,
    snapshotQueue, restoreQueue, clearAutoUpcoming,
    // The <audio> element's side of things, for the auto-skip tests:
    failCurrent: () => _skipUnplayable(),                       // what the 'error' handler ends with
    nowPlaying: () => (audioListeners.playing || []).forEach((fn) => fn()),
    playTrackDirect: (t) => playTrack(t),                       // a start that bypasses the queue
    get explicitPick() { return _explicitPick; },
    get queue() { return queue; }, get queueIdx() { return queueIdx; },
    get shuffle() { return shuffle; }, get repeatMode() { return repeatMode; },
    get current() { return queue[queueIdx]; },
    get st() { return { mode: _srcMode, seed: _seed, src: _src, ended: _srcEnded,
                        ordNext: _ordNext, ordResume: _ordResume,
                        shufNext: _shufNext, total: _srcTotal, hasOrig: !!_origQueue }; },
    posOf: (t) => _ordPos.get(t),
    isManual: (t) => _manual.has(t),
    get autoAhead() { return _autoUpcoming().length; },   // the core function the public getter uses
    get autoUpcoming() { return _autoUpcoming(); },
    get radioActive() { return _radioActive; },
    // Queue sync across devices.
    _qsyncBody, _pushQueueSync, _offerServerQueue, _applyServerQueue, resumeQueueOffer,
    get queueSyncSupported() { return _qsyncSupported; },
    get resumeAt() { return _resumeAt; },
    get queueOffer() { return _qsyncOffer ? _qsyncOffer.who : null; },
    get cuedTrack() { return _cuedTrack(); }, get cuedSec() { return _cuedSec(); },
    qsyncClient: () => _qsyncClient(),
    // The <audio> element / radio side, for the cued-track tests.
    setAudioSrc: (v) => { audio.src = v; },
    setExtAudio: (v) => { _extAudio = !!v; },
    setTrackId: (v) => { trackId = v; },
  };
`);

// ── Fake server ───────────────────────────────────────────────────────────────
export const tid = (i) => 't' + String(i).padStart(5, '0');

function seededPerm(n, seed) {               // deterministic per seed, like the API
  const a = Array.from({ length: n }, (_, i) => i);
  let x = (Number(seed) >>> 0) || 1;
  for (let i = n - 1; i > 0; i--) {
    x = (Math.imul(x, 1664525) + 1013904223) >>> 0;
    const j = x % (i + 1);
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

export function makeServer(n) {
  // ``place``: Map(trackIndex → permutation position) forced into EVERY seed's
  // order, so a test can put a given track at a known place in the shuffle.
  const srv = { n, requests: [], down: false, perms: new Map(), place: new Map(), deleted: new Set() };
  srv.fetch = async (url, opts = {}) => {
    srv.requests.push(url);
    if (srv.down) throw new Error('network down');
    // ``hang``: the request never answers — it only ends when its timeout signal fires.
    if (srv.hang) {
      await new Promise((_, rej) => opts.signal && opts.signal.addEventListener('abort', () => rej(opts.signal.reason)));
    }
    // ``gate``: a function returning a promise the NEXT request waits on (then is
    // cleared) — lets a test hold one request open and fail it later.
    if (srv.gate) { const g = srv.gate; srv.gate = null; await g(); }
    const u = new URL(url, 'http://x');
    const off = Number(u.searchParams.get('offset') || 0);
    const lim = Number(u.searchParams.get('limit') || 50);
    const row = (i) => ({ id: tid(i), title: 'T' + i });      // NEW objects every time
    let body;
    if (u.pathname.endsWith('/meta/batch')) {
      if (srv.failBatch) return { ok: false, status: 500, json: async () => ({}) };
      const ids = JSON.parse(opts.body || '{}').ids || [];
      body = ids.filter((id) => /^t\d{5}$/.test(id) && Number(id.slice(1)) < srv.n)
        .map((id) => row(Number(id.slice(1))));
    } else if (u.pathname.endsWith('/shuffled')) {
      const seed = u.searchParams.get('seed');
      if (!srv.perms.has(seed)) {
        const perm = seededPerm(srv.n, seed);
        for (const [track, at] of srv.place) {
          const from = perm.indexOf(track);
          [perm[from], perm[at]] = [perm[at], perm[from]];
        }
        srv.perms.set(seed, perm);
      }
      const order = srv.perms.get(seed);
      // ``deleted``: ids gone since the order was dealt — skipped, total unchanged (like the API).
      body = { total: srv.n, tracks: order.slice(off, off + lim).filter((i) => !srv.deleted.has(i)).map(row) };
    } else {
      body = [];
      for (let i = off; i < Math.min(srv.n, off + lim); i++) body.push(row(i));
    }
    return { ok: true, status: 200, json: async () => body };
  };
  return srv;
}

export const SOURCE = (total) => ({
  ordered: { url: '/api/tracks', params: {} },
  shuffle: { url: '/api/tracks/shuffled', params: {} },
  total, label: 'All Tracks',
});
export const CAPPED = () => ({          // no ordered endpoint: search / client-sorted list
  ordered: null, shuffle: { url: '/api/tracks/shuffled', params: { q: 'x' } },
  total: null, label: 'Search: x',
});
export const rows = (from, count) =>
  Array.from({ length: count }, (_, k) => ({ id: tid(from + k), title: 'T' + (from + k) }));

// ── Player factory ────────────────────────────────────────────────────────────
export function makeStorage(init = {}, { quota = Infinity } = {}) {
  const m = new Map(Object.entries(init));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null),
           setItem: (k, v) => { if (String(v).length > quota) throw new Error('QuotaExceededError'); m.set(k, String(v)); },
           removeItem: (k) => m.delete(k), _map: m };
}

function mulberry32(a) {
  return () => {
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function makePlayer({ server, storage = makeStorage(), rngSeed = 12345 } = {}) {
  const timers = [];
  const env = {
    random: mulberry32(rngSeed),
    localStorage: storage, fetch: server ? server.fetch : async () => { throw new Error('no server'); },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: (id) => { if (id) timers[id - 1] = null; },
    plays: [], events: [], advances: [], toasts: [], dismissed: [], currentTime: 0,
    // The page timeout, under test control: ``p.expireRequests()`` fires every
    // pending one (the real AbortSignal.timeout would mean a 20 s wait).
    signals: [],
    AbortSignal: { timeout(ms) { const c = new AbortController();
      env.signals.push({ ms, fire: () => c.abort(new DOMException('timed out', 'TimeoutError')) }); return c.signal; } },
  };
  const p = FACTORY(env);
  p.env = env;
  p.storage = storage;
  // Let every pending fetch / promise chain settle, then run due timers (the
  // debounced queue save).
  p.settle = async () => {
    for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r));
    for (let i = 0; i < timers.length; i++) { const fn = timers[i]; timers[i] = null; if (fn) fn(); }
  };
  p.expireRequests = () => { const n = env.signals.length; env.signals.splice(0).forEach((s) => s.fire()); return n; };
  p.ids = () => p.queue.map((t) => t.id);
  return p;
}
