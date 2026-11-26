// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * voices/mixer.js — the voice mixer's audio and scope data for the track that
 * is playing (api/voices.py).  No DOM.
 *
 * Mute / solo never touches the cached render: while a voice is muted, the
 * server renders the tune again with those voices switched off inside the
 * emulation, and this module plays that render in place of the reference
 * from the same position — Web Audio buffers scheduled on the player's
 * AudioContext into the EQ's input (Player.voiceOverlay()), with the
 * reference's own path closed at the same instant.  With every voice on,
 * the reference plays as always.  Pause, seek, a new track or a drift of more
 * than ``DRIFT_S`` re-anchor or stop it.
 *
 *   createVoiceMixer({ player })  → mixer
 *     mixer.attach(track, info)   the voices of the playing tune (GET …/voices)
 *     mixer.setAudible(mask)      the voices heard (bit v = voice v); all → reference
 *   audibleMask(n, muted, soloed) → the voices heard for these Mute / Solo keys
 *     mixer.state                 'off' | 'preparing' | 'on' | 'error'
 *     mixer.scopeWindow(v, n, out) → out[0…n) = voice v's last n scope samples (−1…1), or false
 *     mixer.wantScopes(on)        fetch scope data while a scope view shows
 *     mixer.onChange(fn)          state / mask changes
 *     mixer.detach()
 */

export const CHUNK_S = 2;            // seconds of mix per request
const AHEAD_S = 6;                   // keep this much scheduled ahead
const LEAD_S = 0.35;                 // first chunk starts this far ahead of now
const DRIFT_S = 0.06;                // re-anchor beyond this drift from the element
const SCOPE_CHUNK_S = 1;
const RETRY_MS = 300;
const RAMP_S = 0.005;                // gate / render swaps ramp over this
const SCOPE_BACKOFF_MS = 4000;       // after a failed scope fetch

export function allMask(n) { return n >= 32 ? 0xFFFFFFFF : ((1 << n) >>> 0) - 1; }

/** Mute / solo → the voices heard (bit v = voice v): the soloed ones (all
 *  when none is), less the muted. */
export function audibleMask(n, muted, soloed) {
  const all = allMask(n);
  const base = soloed ? ((soloed & all) >>> 0) : all;
  return (base & ~(muted || 0)) >>> 0;
}

/** int16 little-endian interleaved → one Float32Array per channel. */
export function pcmToChannels(buf, channels, frames) {
  const dv = new DataView(buf);
  const out = [];
  for (let c = 0; c < channels; c++) out.push(new Float32Array(frames));
  let o = 0;
  for (let f = 0; f < frames; f++) {
    for (let c = 0; c < channels; c++, o += 2) out[c][f] = dv.getInt16(o, true) / 32768;
  }
  return out;
}

export function createVoiceMixer({ player, fetchImpl = (...a) => fetch(...a) }) {
  let track = null, info = null, n = 0;
  let audible = 0, state = 'off', gen = 0;
  let engaged = false;               // a muted render has played since the mask left "all"

  let sources = [], anchorCtx = 0, anchorFrame = 0, nextFrame = 0, ended = false;
  let out = null, overlay = null, tickTimer = null, inflight = false;
  const listeners = new Set();
  // scopes: chunk start (scope frames) → Float32Array per voice
  let scopeOn = false, scopeChunks = new Map(), scopeInflight = false, scopeGen = 0;

  const emitChange = () => { for (const fn of listeners) { try { fn(); } catch (_) {} } };
  const setState = (s) => { if (s !== state) { state = s; emitChange(); } };

  function url(kind, extra) {
    const q = new URLSearchParams({ kind, ...extra });
    if (Number.isInteger(track.subsong)) q.set('subsong', String(track.subsong));
    return `/api/tracks/${encodeURIComponent(track.id)}/voices/pcm?${q}`;
  }

  function tuneTime() { return Number(player.currentTime) || 0; }

  function stopSources(at) {
    for (const s of sources) { try { s.stop(at); } catch (_) {} }
    sources = [];
  }

  // ``keepGate``: the reference stays out (a re-anchor while voices are
  // muted — a seek, a resume — is a moment of silence, never the muted
  // voices).  Gate changes ramp over RAMP_S: no click.
  function closeOverlay(keepGate = false) {
    gen++;
    if (overlay) {
      const t = overlay.ctx.currentTime;
      stopSources(t + RAMP_S);
      if (out) ramp(out.gain, 0, t);
      if (!keepGate) ramp(overlay.gate.gain, 1, t);
    }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    inflight = false;
  }

  function ramp(param, value, at) {
    try {
      param.cancelScheduledValues(at);
      param.setValueAtTime(param.value, at);
      param.linearRampToValueAtTime(value, at + RAMP_S);
    } catch (_) {}
  }

  function volume() {
    const a = player.audio;
    return a ? (a.muted ? 0 : a.volume) : 1;
  }

  // Start (or restart) the muted render at the playing position.
  async function anchor() {
    closeOverlay(engaged && !!track && audible !== allMask(n));
    if (!track || audible === allMask(n)) { engaged = false; setState('off'); return; }
    if (!player.playing) { setState('preparing'); return; }     // starts on play
    overlay = player.voiceOverlay ? player.voiceOverlay() : null;
    if (!overlay) { setState('error'); return; }
    const my = ++gen;
    if (!out) {
      out = overlay.ctx.createGain();
      out.gain.value = 0;
      out.connect(overlay.input);
    }
    if (state !== 'on') setState('preparing');
    const rate = info.rate || 44100;
    // Ask for the chunk at the position the first buffer will play from.
    for (;;) {
      if (my !== gen) return;
      const ctx = overlay.ctx;
      const startCtx = ctx.currentTime + LEAD_S;
      const startFrame = Math.max(0, Math.round((tuneTime() + LEAD_S) * rate));
      const got = await fetchChunk(my, startFrame);
      if (my !== gen) return;
      if (got === 'retry') { await new Promise(r => setTimeout(r, RETRY_MS)); continue; }
      if (!got) return;
      if (!got.frames) { closeOverlay(); setState('preparing'); return; }   // past the end
      // Late (the request took longer than the lead): skip what has passed.
      const now = ctx.currentTime + 0.03;
      const late = Math.max(0, now - startCtx);
      anchorCtx = startCtx;
      anchorFrame = startFrame;
      if (late * rate >= got.frames) continue;           // whole chunk already past: ask again
      play(got, startCtx + late, late);
      // The swap: the reference ramps out as the render ramps in.
      ramp(overlay.gate.gain, 0, startCtx + late - RAMP_S);
      ramp(out.gain, volume(), startCtx + late - RAMP_S);
      nextFrame = startFrame + got.frames;
      ended = got.end;
      engaged = true;
      setState('on');
      tickTimer = setInterval(() => tick(my), 250);
      tick(my);
      return;
    }
  }

  async function fetchChunk(my, frame) {
    const rate = info.rate || 44100;
    let res;
    try {
      res = await fetchImpl(url('mix', { mask: String(audible >>> 0), start: String(frame),
                                         count: String(CHUNK_S * rate) }),
                            { credentials: 'include' });
    } catch (_) { return 'retry'; }
    if (my !== gen) return null;
    if (res.status === 202) return 'retry';
    if (res.status === 204) return { frames: 0, end: true, data: null };
    if (!res.ok) { setState('error'); closeOverlay(); return null; }
    const ch = parseInt(res.headers.get('X-Voices-Channels') || '2', 10) || 2;
    const frames = parseInt(res.headers.get('X-Voices-Frames') || '0', 10) || 0;
    const total = parseInt(res.headers.get('X-Voices-Total') || '0', 10) || 0;
    const buf = await res.arrayBuffer();
    if (my !== gen) return null;
    return { frames, ch, data: pcmToChannels(buf, ch, frames),
             end: total > 0 && frame + frames >= total };
  }

  function play(chunk, when, offset) {
    if (!chunk.frames || !chunk.data) return;
    const ctx = overlay.ctx;
    const rate = info.rate || 44100;
    const b = ctx.createBuffer(chunk.ch, chunk.frames, rate);
    chunk.data.forEach((d, c) => b.copyToChannel(d, c));
    const s = ctx.createBufferSource();
    s.buffer = b;
    s.connect(out);
    s.start(when, offset || 0);
    s.onended = () => { sources = sources.filter(x => x !== s); };
    sources.push(s);
  }

  async function tick(my) {
    if (my !== gen || !overlay) return;
    const ctx = overlay.ctx;
    const rate = info.rate || 44100;
    if (out && Math.abs(out.gain.value - volume()) > 1e-3) ramp(out.gain, volume(), ctx.currentTime);
    // Drift against the element (a stall, a seek the events missed).
    const expect = (anchorFrame + (ctx.currentTime - anchorCtx) * rate) / rate;
    if (ctx.currentTime > anchorCtx + 0.5 && Math.abs(expect - tuneTime()) > DRIFT_S) {
      anchor();
      return;
    }
    if (inflight || ended) return;
    const aheadS = (nextFrame - anchorFrame) / rate - (ctx.currentTime - anchorCtx);
    if (aheadS > AHEAD_S) return;
    inflight = true;
    let got;
    for (;;) {
      got = await fetchChunk(my, nextFrame);
      if (got !== 'retry' || my !== gen) break;
      await new Promise(r => setTimeout(r, RETRY_MS));
    }
    if (my !== gen) return;               // (a re-anchor took over: its own fetches run)
    inflight = false;
    if (!got) return;
    if (!got.frames) { ended = true; return; }
    const when = anchorCtx + (nextFrame - anchorFrame) / rate;
    if (when < ctx.currentTime) { anchor(); return; }          // fell behind: start over
    play(got, when, 0);
    nextFrame += got.frames;
    ended = got.end;
  }

  // ── scopes ────────────────────────────────────────────────────────────────
  let scopeWait = 0, lastScopeUse = 0;
  async function scopeFetch() {
    if (!scopeOn || scopeInflight || !track || !info || !info.scope) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    if (performance.now() < scopeWait) return;
    if (performance.now() - lastScopeUse > 1500) return;        // nothing draws them
    const sr = info.scope_rate || 22050;
    const size = SCOPE_CHUNK_S * sr;
    const cur = Math.floor(tuneTime() * sr / size) * size;
    const want = [cur, cur + size].filter(k => !scopeChunks.has(k));
    // Forget chunks behind the playhead.
    for (const k of scopeChunks.keys()) if (k < cur - size) scopeChunks.delete(k);
    if (!want.length) return;
    scopeInflight = true;
    const my = scopeGen;
    try {
      const k = want[0];
      const res = await fetchImpl(url('scope', { start: String(k), count: String(size) }),
                                  { credentials: 'include' });
      if (my !== scopeGen) return;
      if (res.status === 200) {
        const ch = parseInt(res.headers.get('X-Voices-Channels') || '0', 10) || 0;
        const frames = parseInt(res.headers.get('X-Voices-Frames') || '0', 10) || 0;
        const buf = await res.arrayBuffer();
        if (my !== scopeGen) return;
        const dv = new DataView(buf);
        const voices = [];
        for (let v = 0; v < ch; v++) {
          const a = new Float32Array(frames);
          for (let i = 0, o = v * frames * 2; i < frames; i++, o += 2) a[i] = dv.getInt16(o, true) / 32768;
          voices.push(a);
        }
        scopeChunks.set(k, { frames, voices });
      } else if (res.status !== 202) {
        scopeWait = performance.now() + SCOPE_BACKOFF_MS;
      }
    } catch (_) { scopeWait = performance.now() + SCOPE_BACKOFF_MS; }
    finally { if (my === scopeGen) scopeInflight = false; }
  }

  /** Voice ``v``'s ``count`` scope samples ending at the playhead into
   *  ``outArr`` (−1…1); false while the data isn't there. */
  function scopeWindow(v, count, outArr) {
    lastScopeUse = performance.now();
    if (!info || !scopeChunks.size) return false;
    const sr = info.scope_rate || 22050;
    const size = SCOPE_CHUNK_S * sr;
    const end = Math.floor(tuneTime() * sr);
    let ok = false;
    for (let i = 0; i < count; i++) {
      const f = end - count + i;
      const k = Math.floor(f / size) * size;
      const c = scopeChunks.get(k);
      const x = c && c.voices[v] && f - k < c.frames ? c.voices[v][f - k] : 0;
      if (c) ok = true;
      outArr[i] = x;
    }
    return ok;
  }

  let scopeTimer = null;
  function wantScopes(on) {
    scopeOn = !!on;
    if (scopeOn && !scopeTimer) {
      lastScopeUse = performance.now();
      scopeTimer = setInterval(scopeFetch, 250);
      scopeFetch();
    } else if (!scopeOn && scopeTimer) {
      clearInterval(scopeTimer); scopeTimer = null;
    }
  }

  // ── player events ─────────────────────────────────────────────────────────
  if (player.on) {
    player.on('seeked', () => { scopeChunks.clear(); scopeGen++; scopeInflight = false; if (state !== 'off') anchor(); });
    player.on('statechange', ({ playing } = {}) => {
      if (state === 'off' || state === 'error') return;
      if (playing) anchor();
      else { closeOverlay(engaged); setState('preparing'); }
    });
    player.on('trackchange', (t) => {
      if (track && (!t || t.id !== track.id || t.subsong !== track.subsong)) detach();
    });
  }

  function attach(t, inf) {
    detach();
    track = t; info = inf; n = (inf && inf.voices) ? inf.voices.length : 0;
    audible = allMask(n);
    emitChange();
  }

  function detach() {
    engaged = false;
    closeOverlay();
    track = null; info = null; n = 0; audible = 0;
    scopeChunks.clear(); scopeGen++; scopeInflight = false;
    setState('off');
    emitChange();
  }

  function setAudible(mask) {
    if (!track || !n) return;
    const m = (mask >>> 0) & allMask(n);
    if (m === audible) return;
    audible = m;
    emitChange();
    anchor();
  }

  return {
    attach, detach, setAudible, wantScopes, scopeWindow,
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    get state() { return state; },
    get audible() { return audible; },
    get track() { return track; },
    get info() { return info; },
    get voices() { return n; },
    // (tests / verification)
    get _debug() { return { sources: sources.length, anchorCtx, anchorFrame, nextFrame, scopeChunks: scopeChunks.size }; },
  };
}
