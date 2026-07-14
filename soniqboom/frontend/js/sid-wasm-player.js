/* SoniqBoom — FEATURE-FLAGGED in-browser SID playback (experimental).
 *
 * When the flag is on, a C64 SID plays entirely in the browser: the
 * libsidplayfp+reSIDfp WASM core renders the tune to a WAV blob (fed to the
 * normal <audio> element, so seek/scrub/timeline all work unchanged) and the
 * per-voice VU falls out of the SAME render pass — no server SID render, no VU
 * cache, VU perfectly synced to the audio.
 *
 * REMOVABILITY: this whole feature is (a) this file, (b) one branch in
 * player.js `playTrack` guarded by `sidWasmPlaybackEnabled()`, (c) one branch
 * in app.js `_handleVU` that consumes `window.__sbSidWasmVU`, and (d) the
 * Settings toggle. Delete those four and the app reverts to server-streamed
 * SID + the cached VU-offload path with zero residue.
 *
 * Tradeoff vs the default path: the tune is fully rendered before audio starts
 * (~tune_len / 13 seconds; a 3-min tune ≈ 14s), in exchange for full server
 * offload + zero-latency synced VU. That's why it's opt-in (default OFF); the
 * default SID path stays the instant-start progressive server stream.
 */
'use strict';

const FLAG_KEY = 'sb.sidWasmPlayback';
const WORKER_URL = '/assets/js/vu-sid-worker.js?v=4';

export function sidWasmPlaybackEnabled() {
  // Default OFF (opt-in): only an explicit '1' (user turned it on in Settings)
  // enables it, so the default SID path stays the instant-start progressive
  // server stream.  On any WASM error it falls back to that server stream too.
  try { return localStorage.getItem(FLAG_KEY) === '1'; } catch (_) { return false; }
}
export function setSidWasmPlayback(on) {
  try { localStorage.setItem(FLAG_KEY, on ? '1' : '0'); } catch (_) {}
}

// A track is eligible only if it's a C64 SID (the server's /sid endpoint is the
// real gate — it 415s anything else, and on a 415 renderSidForPlayback just
// logs + falls back to the server stream, so this is a cheap, safe pre-check).
// Match case-insensitively and accept the PSID/RSID sub-labels for parity with
// player.js's own SID checks.  (The scanner currently normalises both PSID and
// RSID containers to format "SID", so in practice this only ever sees "SID"
// today — the extra labels are defensive against a differently-tagged source.)
export function isC64SidTrack(track) {
  const fmt = String((track && track.format) || '').split('/')[0].trim().toUpperCase();
  return fmt === 'SID' || fmt === 'PSID' || fmt === 'RSID';
}

// Cheap synchronous check that the browser can actually run the SID core: the
// WASM is built with fixed-width SIMD (-msimd128), which Firefox <89 and
// SIMD-disabled configs reject.  Validating a tiny SIMD module up-front lets us
// keep the server path (and explain why) instead of fetching /sid + spawning a
// worker only to fail.  This is the wasm-feature-detect SIMD probe.
const _SIMD_PROBE = new Uint8Array([
  0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11,
]);
let _supported = null;
export function sidWasmSupported() {
  if (_supported === null) {
    try {
      _supported = typeof Worker === 'function'
        && typeof WebAssembly === 'object'
        && typeof WebAssembly.validate === 'function'
        && WebAssembly.validate(_SIMD_PROBE);
    } catch (_) { _supported = false; }
  }
  return _supported;
}

// Announce ONCE per session that in-browser SID isn't working and we fell back
// to the server render: a console line for diagnosis + a window event that
// app.js turns into a non-blocking toast, so the "Play SID in browser" toggle
// never looks silently broken.  (The console.warn logs every time; the toast
// fires once.)
let _notified = false;
function _notifyUnavailable(reason) {
  console.warn('[sid-wasm] in-browser SID unavailable — using the server render. Reason:', reason);
  if (_notified) return;
  _notified = true;
  try {
    window.dispatchEvent(new CustomEvent('sb-sidwasm-unavailable', { detail: String(reason || '') }));
  } catch (_) {}
}

let _worker = null;
let _reqId = 0;
let _pending = null;
let _unavailable = false;
let _workerFails = 0;                       // consecutive worker-load failures — latch only after 2 (tolerate a transient blip)

function _getWorker() {
  if (_worker || _unavailable) return _worker;
  if (typeof Worker !== 'function') { _unavailable = true; return null; }
  try {
    const w = new Worker(WORKER_URL);
    w.onmessage = (e) => {
      const m = e.data || {};
      const pend = _pending;
      if (!pend || m.id !== pend.id) return;
      if (m.type === 'done') { _pending = null; pend.resolve(m); }
      else if (m.type === 'error') {
        _pending = null;
        console.warn('[sid-wasm] worker render error:', m.error);
        pend.reject(new Error(m.error || 'worker error'));
      }
      // 'partial' snapshots aren't used by the playback path
    };
    w.onerror = (ev) => {
      const where = (ev && (ev.message || ev.filename))
        ? `${ev.message || ''} @ ${ev.filename || '?'}:${ev.lineno || 0}:${ev.colno || 0}` : ev;
      console.warn('[sid-wasm] worker failed to load/run:', where);
      _worker = null;                       // drop the dead worker so the next play can rebuild it
      if (++_workerFails >= 2) _unavailable = true;   // deterministic (not a one-off blip) → stop retrying
      // (renderSidForPlayback's catch fires the one-time toast from the reject.)
      if (_pending) { const p = _pending; _pending = null; p.reject(new Error('worker crashed')); }
    };
    _worker = w;
  } catch (e) {
    console.warn('[sid-wasm] could not create the SID worker:', (e && e.message) || e);
    _worker = null;
    if (++_workerFails >= 2) _unavailable = true;
  }
  return _worker;
}

function _renderAudioVU(sidBytes, subsong, dur) {
  return new Promise((resolve, reject) => {
    const w = _getWorker();
    if (!w) { reject(new Error('worker unavailable')); return; }
    if (_pending) {
      // A newer render request cancels this one — NOT a failure (the user
      // started a different/newer play).  Flag it so the caller abandons
      // quietly instead of toasting "unavailable" + falling back to server.
      const p = _pending; _pending = null;
      const e = new Error('superseded'); e.superseded = true; p.reject(e);
    }
    const id = ++_reqId;
    _pending = { id, resolve, reject };
    w.postMessage({ id, mode: 'audiovu', sidBytes, subsong, dur }, [sidBytes]);
  });
}

/**
 * Render a C64 SID to a playable object URL + its VUMR, in the browser.
 * @returns {Promise<{url:string, vumr:ArrayBuffer|null, frames:number}|null>}
 *   `url` is an object URL the caller MUST revoke when done; null on any failure
 *   (caller then falls back to the normal server-stream path).
 */
export async function renderSidForPlayback(trackId, subsong, durSec) {
  if (_unavailable) return null;
  if (!sidWasmSupported()) {
    _notifyUnavailable('this browser lacks WebAssembly SIMD, which the SID core needs');
    return null;
  }
  try {
    const res = await fetch(`/api/tracks/${encodeURIComponent(trackId)}/sid`, { credentials: 'include' });
    if (!res.ok) { console.warn('[sid-wasm] /sid fetch failed:', res.status); return null; }
    const sidBytes = await res.arrayBuffer();
    const { wav, vumr, frames } = await _renderAudioVU(sidBytes, Number(subsong) || 0, durSec);
    if (!wav || !wav.byteLength) { console.warn('[sid-wasm] render produced no audio'); return null; }
    const url = URL.createObjectURL(new Blob([wav], { type: 'audio/wav' }));
    // Return the raw WAV too (a fresh allocation, distinct from the Blob's copy)
    // so the caller can upload it to warm the server cache — the next play of
    // this SID (any client, cast, offline) then streams it with zero render.
    return { url, wav, vumr: vumr || null, frames: frames || 0 };
  } catch (e) {
    // A superseded render is a cancellation, not a failure — a newer play took
    // over.  Return a sentinel so the caller abandons quietly (no toast, no
    // server fallback); the newer play owns the audio element now.
    if (e && e.superseded) return { superseded: true };
    // Any REAL failure falls through to the server stream — but NOISILY, so the
    // reason is diagnosable in the console + a one-time toast instead of
    // masquerading as a normal server render.
    _notifyUnavailable((e && e.message) || String(e));
    return null;
  }
}

// Probe whether the server already has this SID's render CACHED (warmed by a
// prior play) and the server-derived target length.  A `ready:true` result
// means the play can be an instant plain-cache stream with NO render anywhere;
// otherwise the caller renders in-browser (and warms the cache).  Returns
// `{ ready, target_seconds, warm_eligible }` or null on any error (→ caller
// treats as not-ready).  Once ready is seen for an (id, subsong) it is cached
// for the session so repeat plays skip even this probe round-trip.
const _sidReady = new Set();
export async function sidRenderStatus(trackId, subsong) {
  const ss = Number(subsong) || 0;
  const key = `${trackId}:${ss}`;
  if (_sidReady.has(key)) return { ready: true, cached: true };
  try {
    const r = await fetch(
      `/api/stream/${encodeURIComponent(trackId)}/render-status?subsong=${ss}`,
      { credentials: 'include' });
    if (!r.ok) return null;
    const s = await r.json();
    if (s && s.ready) _sidReady.add(key);
    return s;
  } catch (_) { return null; }
}
