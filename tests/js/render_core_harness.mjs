// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Runs the REAL rendered-track helpers from soniqboom/frontend/js/player.js
// outside a browser: render recovery after a failed first play, the failure
// report (probe or the server's reason), the badge × cancel, the render-length
// watch, the swap onto the cached render, the element length the player
// reports and the prewarm / render-status URLs.
// They are lifted out by their
// ``[render-core:begin]`` / ``[render-core:end]`` markers and compiled with the
// few stubs they need (a fake <audio>, fetch, timers, the badge).  Nothing here
// re-implements player logic.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const PLAYER_JS = process.env.SB_PLAYER_JS
  || resolve(HERE, '../../soniqboom/frontend/js/player.js');

function extractCore() {
  const src = readFileSync(PLAYER_JS, 'utf8');
  const parts = [];
  const re = /\/\/ \[render-core:begin\][^\n]*\n([\s\S]*?)\/\/ \[render-core:end\]/g;
  let m;
  while ((m = re.exec(src))) parts.push(m[1]);
  if (parts.length !== 2) throw new Error(`expected 2 render-core regions, found ${parts.length}`);
  return parts.join('\n');
}

const FACTORY = new Function('env', `
  'use strict';
  const { fetch, setTimeout, clearTimeout, audio } = env;
  const Toast = { error: (m) => env.toasts.push(['error', m]) };
  const console = { warn() {}, error() {}, log() {} };
  function emit(name, data) { env.events.push([name, data]); }
  let trackId = null, _track = null, _playSeq = 0, _pendingSeekSec = null;
  let _stationMode = false, _extAudio = false, _metaDuration = 0, _seekOffset = 0;
  let _sidPartial = false, _sidTargetSec = 0, _isTranscoded = false;
  const _RENDERED = new Set(['SID', 'PSID', 'MOD', 'TFMX', 'XM']);
  const _isRenderedTrack = (t) => !!t && _RENDERED.has(String(t.format || '').toUpperCase());
  function _streamUrlFor(id) { return '/api/stream/' + id; }
  function _renderTextOnlyConvertBadge(text) { env.badge.label = text; }
  function _showConvertBadge() { env.badge.shown = true; }
  function _hideConvertBadge() { env.badge.shown = false; }
  function _hideBufferingBadge() {}
  function _stopTranscodePolling() { env.polls.stopped++; }
  function _resetSidPartial() { _sidPartial = false; _provisional = false; }
  let _failToastFor = null, _failToastSeq = -1;       // queue-core state the report shares
  function _nextHint() { return ''; }
  ${extractCore()}
  return {
    // Put the player in the state playTrack leaves it in for track t.
    start(t) { _track = t; trackId = t.id; _metaDuration = t.duration || 0; _pendingSeekSec = null; return ++_playSeq; },
    set pendingSeek(v) { _pendingSeekSec = v; },
    set station(v) { _stationMode = v; },
    get seq() { return _playSeq; },
    get provisional() { return _provisional; },
    get metaDuration() { return _metaDuration; },
    get cancelled() { return _cancelledStart; },
    get recoverySeq() { return _renderRecoverySeq; },
    _subQs, _renderStatusUrl, _prewarmUrl, _startRenderRecovery, _cancelPendingStart,
    _watchRenderLength, _switchToCachedRender, _duration, _knownLength, _reportPlayFailure, _fmtLabel,
    get prewarmId() { return PREWARM_PAGE_ID; },
  };
`);

// ── Fake <audio> ──────────────────────────────────────────────────────────────
// ``load()`` / a new ``src`` reset the position like a real element.  ``play()``
// runs ``onPlay`` (the test decides whether metadata / canplay fire) and then
// resolves, or rejects with ``playError``.
export function makeAudio() {
  const L = {};
  const a = {
    currentTime: 0, duration: NaN, paused: true, loads: 0, plays: 0,
    _src: '',
    get src() { return a._src; },
    set src(v) { a._src = v; a.currentTime = 0; },
    addEventListener(ev, fn, opts) { (L[ev] ||= []).push({ fn, once: !!(opts && opts.once) }); },
    removeEventListener(ev, fn) { L[ev] = (L[ev] || []).filter((x) => x.fn !== fn); },
    listeners: (ev) => (L[ev] || []).length,
    fire(ev) {
      const ls = (L[ev] || []).slice();
      L[ev] = (L[ev] || []).filter((x) => !x.once);
      ls.forEach((x) => x.fn());
    },
    load() { a.loads++; a.currentTime = 0; },
    pause() { a.paused = true; },
    play() {
      a.plays++;
      if (a.onPlay) a.onPlay();
      if (a.playError) return Promise.reject(a.playError);
      a.paused = false;
      return Promise.resolve();
    },
    removeAttribute(k) { if (k === 'src') a._src = ''; },
  };
  return a;
}

// ── Fake server: render-status answers, in order (the last one repeats) ───────
export function makeServer(answers = []) {
  const srv = { requests: [], answers: answers.slice() };
  srv.fetch = async (url, opts) => {
    srv.requests.push(url);
    if (srv.onStream && /^\/api\/stream\/[^/?]+(\?|$)/.test(url)) return srv.onStream(url, opts);
    const next = srv.answers.length > 1 ? srv.answers.shift() : srv.answers[0];
    if (next instanceof Error) throw next;
    return { ok: true, status: 200, json: async () => next || {} };
  };
  return srv;
}

export function makeCore({ server = makeServer(), audio = makeAudio() } = {}) {
  const timers = new Map();                 // id → callback, fired by tick()
  let nextId = 1;
  const env = {
    fetch: server.fetch, audio,
    setTimeout: (fn) => { const id = nextId++; timers.set(id, fn); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    events: [], toasts: [], badge: { shown: false, label: '' }, polls: { stopped: 0 },
  };
  const core = FACTORY(env);
  core.env = env;
  core.audio = audio;
  core.server = server;
  // Let pending promises (fetch answers, awaits) settle — no timer fires.
  core.settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
  // settle, then fire every pending timer (a poll interval, a sleep, a timeout)
  // once, then settle again: one "round" of the code under test.
  core.tick = async () => {
    await core.settle();
    const due = [...timers.values()];
    timers.clear();
    due.forEach((fn) => fn());
    await core.settle();
  };
  core.run = async (rounds = 10) => { for (let i = 0; i < rounds; i++) await core.tick(); };
  return core;
}
