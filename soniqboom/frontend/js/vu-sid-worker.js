/* SoniqBoom — client-side SID per-voice VU worker.
 *
 * Renders a C64 SID tune with a libsidplayfp+reSIDfp WASM core and produces a
 * VUMR v1 sidecar (byte-identical in structure to the server's serialize_vumr,
 * core/openmpt_vu.py) so the meter renderer (_parseVUMR) and the shared cache
 * consume it unchanged.  This offloads the server's 3-pass sidplayfp render to
 * the browser; the main thread uploads the result to /api/tracks/{id}/vu so
 * every later play (any client) hits the cache.
 *
 * Protocol (main → worker):  { id, sidBytes:ArrayBuffer, subsong:int, dur:number }
 * Protocol (worker → main):
 *   { id, type:'progress', frames, done }          // optional incremental (streaming)
 *   { id, type:'done', vumr:ArrayBuffer, frames }  // final sidecar (transferable)
 *   { id, type:'error', error:string }
 *
 * The VU algorithm mirrors core/sid_vu.build_vu exactly: 3 voice-isolation
 * passes (mute 2 of 3), fixed 1470-sample (44100/30) windows, per-voice peak,
 * normalised by the song-wide peak across all voices, frame-major uint8.
 */
'use strict';

const VENDOR = '/assets/js/vendor/';
const WASM_V = '3';                      // cache-bust the glue+wasm on rebuild
importScripts(VENDOR + 'sidwasm.js?v=' + WASM_V);   // defines global createSidModule

const RATE = 44100;
const VU_HZ = 30;
const SPF = Math.floor(RATE / VU_HZ);   // 1470 samples per VU frame
const MAX_DUR = 600;                    // server's _SID_VU_MAX_DURATION

let _modPromise = null;
function getModule() {
  if (!_modPromise) {
    _modPromise = createSidModule({ locateFile: (p) => VENDOR + p + '?v=' + WASM_V });
  }
  return _modPromise;
}

// Normalise the first `frames` per-voice peaks to a frame-major uint8 buffer,
// scaled by the global peak across all 3 voices & those frames.  Returns
// { samples: Uint8Array(frames*3), peak }.  Called both per-chunk (streaming)
// and for the final blob, so early frames re-scale as the global peak grows.
function normalizeSamples(cols, frames) {
  let peak = 0;
  for (let v = 0; v < 3; v++) {
    const c = cols[v];
    for (let f = 0; f < frames; f++) if (c[f] > peak) peak = c[f];
  }
  const samples = new Uint8Array(frames * 3);
  if (peak > 0) {
    for (let f = 0; f < frames; f++) {
      const b = f * 3;
      for (let ch = 0; ch < 3; ch++) {
        let val = Math.floor((cols[ch][f] * 255) / peak);
        if (val > 255) val = 255;
        samples[b + ch] = val;
      }
    }
  }
  return { samples, peak };
}

function buildVUMR(cols, frames) {
  const { samples, peak } = normalizeSamples(cols, frames);
  if (peak <= 0) return null;                 // silent render → caller FFT-fallback
  const channels = 3;
  const buf = new ArrayBuffer(16 + channels + frames * channels);
  const dv = new DataView(buf);
  dv.setUint8(0, 0x56); dv.setUint8(1, 0x55); dv.setUint8(2, 0x4D); dv.setUint8(3, 0x52); // "VUMR"
  dv.setUint8(4, 1);           // version
  dv.setUint8(5, channels);    // channels
  dv.setUint8(6, 0);           // flags
  dv.setUint8(7, 0);           // reserved
  dv.setUint32(8, VU_HZ, true);
  dv.setUint32(12, frames, true);
  new Uint8Array(buf).set(samples, 16 + channels);   // pan[3] stays 0 (centre)
  return buf;
}

async function renderVU(sidBytes, subsong, durSec, onPartial) {
  const EMIT_EVERY = 30;                          // stream a snapshot every ~1 s of VU
  let lastEmit = 0;
  const M = await getModule();
  const bytes = new Uint8Array(sidBytes);
  const load = M.cwrap('sid_load', 'number', ['number', 'number', 'number']);
  const renderVu = M.cwrap('sid_render_vu', 'number', ['number', 'number', 'number']);
  const setd = M.cwrap('sid_set_power_delay', null, ['number']);
  const sets = M.cwrap('sid_set_sampling', null, ['number']);
  const errFn = M.cwrap('sid_error', 'string', []);

  const frames = Math.max(1, Math.round(durSec * VU_HZ));
  const cols = [new Float64Array(frames), new Float64Array(frames), new Float64Array(frames)];

  sets(0);   // INTERPOLATE — VU-adequate, ~faster; identical envelope to RESAMPLE
  setd(0);   // pinned warm-up → deterministic client render
  const p = M._malloc(bytes.length);
  M.HEAPU8.set(bytes, p);
  const songs = load(p, bytes.length, subsong);
  M._free(p);
  if (!songs) throw new Error('sid_load failed: ' + errFn());

  // Single reSIDfp pass with the per-voice tap — ~3x faster than 3 mute passes
  // and all 3 voices advance together, so we can render in chunks and stream
  // progress.  sid_render_vu writes `got*3` floats (per-voice peak per frame)
  // and advances the engine, so successive calls continue where they left off.
  const CHUNK_FRAMES = 90;                        // ~3 s of VU per emit
  const outPtr = M._malloc(CHUNK_FRAMES * 3 * 4);
  let done = 0;
  try {
    while (done < frames) {
      const n = Math.min(CHUNK_FRAMES, frames - done);
      const got = renderVu(outPtr, n, SPF);
      const view = M.HEAPF32.subarray(outPtr >> 2, (outPtr >> 2) + got * 3);
      for (let f = 0; f < got; f++) {
        cols[0][done + f] = view[f * 3];
        cols[1][done + f] = view[f * 3 + 1];
        cols[2][done + f] = view[f * 3 + 2];
      }
      done += got;
      // Stream a live snapshot: re-normalise the frames rendered so far and hand
      // them up so the meter fills in from t=0 instead of waiting for the whole
      // render.  Throttled to keep postMessage traffic modest on long tunes.
      if (onPartial && (done - lastEmit >= EMIT_EVERY || done >= frames)) {
        lastEmit = done;
        onPartial(done, normalizeSamples(cols, done).samples);
      }
      if (got < n) break;                          // tune ended early
    }
  } finally {
    M._free(outPtr);
  }
  const nf = done || frames;
  return { vumr: buildVUMR(cols, nf), frames: nf };
}

// Wrap mono 16-bit 44100 Hz PCM in a WAV container the browser <audio> can play.
function buildWav(pcm) {
  const n = pcm.length;
  const buf = new ArrayBuffer(44 + n * 2);
  const dv = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); ws(8, 'WAVE');
  ws(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, RATE, true); dv.setUint32(28, RATE * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  ws(36, 'data'); dv.setUint32(40, n * 2, true);
  new Int16Array(buf, 44).set(pcm);        // copies out of the WASM heap
  return buf;
}

// Full browser playback path: render the SID's audio AND per-voice VU in ONE
// pass, returning a WAV blob buffer (for <audio>) + the VUMR (for the meter).
async function renderAudioVU(sidBytes, subsong, durSec) {
  const M = await getModule();
  const bytes = new Uint8Array(sidBytes);
  const load = M.cwrap('sid_load', 'number', ['number', 'number', 'number']);
  const rav = M.cwrap('sid_render_audio_vu', 'number', ['number', 'number', 'number', 'number']);
  const setd = M.cwrap('sid_set_power_delay', null, ['number']);
  const sets = M.cwrap('sid_set_sampling', null, ['number']);
  const errFn = M.cwrap('sid_error', 'string', []);
  sets(0); setd(0);
  const p = M._malloc(bytes.length); M.HEAPU8.set(bytes, p);
  const songs = load(p, bytes.length, subsong); M._free(p);
  if (!songs) throw new Error('sid_load failed: ' + errFn());
  const frames = Math.max(1, Math.round(durSec * VU_HZ));
  const aPtr = M._malloc(frames * SPF * 2);      // 16-bit mono PCM
  const vPtr = M._malloc(frames * 3 * 4);        // per-voice peaks (float)
  try {
    const got = rav(aPtr, vPtr, frames, SPF);
    // Read straight out — no intervening alloc that could grow/move the heap.
    const wav = buildWav(M.HEAP16.subarray(aPtr >> 1, (aPtr >> 1) + got * SPF));
    const cols = [new Float64Array(got), new Float64Array(got), new Float64Array(got)];
    const vu = M.HEAPF32.subarray(vPtr >> 2, (vPtr >> 2) + got * 3);
    for (let f = 0; f < got; f++) { cols[0][f] = vu[f * 3]; cols[1][f] = vu[f * 3 + 1]; cols[2][f] = vu[f * 3 + 2]; }
    return { wav, vumr: buildVUMR(cols, got), frames: got };
  } finally {
    M._free(aPtr); M._free(vPtr);
  }
}

self.onmessage = async (e) => {
  const msg = e.data || {};
  const { id, sidBytes, subsong } = msg;
  let durSec = Math.round(Number(msg.dur) || 0);
  if (!(durSec > 0)) durSec = 180;                // fallback if metadata lacks a length
  if (durSec > MAX_DUR) durSec = MAX_DUR;
  try {
    if (!sidBytes || !sidBytes.byteLength) throw new Error('no sid bytes');
    if (msg.mode === 'audiovu') {
      // Browser-plays-SID path: audio blob + VU, one pass.
      const { wav, vumr, frames } = await renderAudioVU(sidBytes, Number(subsong) || 0, durSec);
      if (!wav) throw new Error('render produced no audio');
      const transfer = vumr ? [wav, vumr] : [wav];
      self.postMessage({ id, type: 'done', wav, vumr, frames }, transfer);
    } else {
      // VU-only offload path: stream partial snapshots, final VUMR for upload.
      const { vumr, frames } = await renderVU(
        sidBytes, Number(subsong) || 0, durSec,
        (done, samples) => self.postMessage(
          { id, type: 'partial', done, samples }, [samples.buffer]),
      );
      if (!vumr) throw new Error('silent render (no VU)');
      self.postMessage({ id, type: 'done', vumr, frames }, [vumr]);
    }
  } catch (err) {
    self.postMessage({ id, type: 'error', error: String((err && err.message) || err) });
  }
};
