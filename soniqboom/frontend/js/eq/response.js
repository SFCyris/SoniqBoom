// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * eq/response.js — the EQ's combined frequency response, from a mirror set of
 * BiquadFilterNodes (same type / frequency / Q as the player's chain) in an
 * OfflineAudioContext: getFrequencyResponse for any gains, exact, without the
 * player's audio graph (which exists only after the first play) and without
 * waiting on the audio clock (the player's params move by setTargetAtTime).
 *
 * Used by equalizer.js (the headroom: the curve's peak) and eq/engine.js (the
 * drawn curve, the preset icons).
 */

export const F_MIN = 20;
export const F_MAX = 20000;

export function logFreqs(n, lo = F_MIN, hi = F_MAX) {
  const out = new Float32Array(n);
  const r = Math.log(hi / lo);
  for (let i = 0; i < n; i++) out[i] = lo * Math.exp(r * i / (n - 1));
  return out;
}

const PEAK_FREQS = logFreqs(256);

/** createResponse(defs, rateFn) — defs: [{freq, type, Q}], rateFn() → sample rate. */
export function createResponse(defs, rateFn = () => 48000) {
  let mirror = null, mirrorRate = 0, failed = false;
  const bufs = new Map();     // length → {mag, ph}
  function _filters() {
    const rate = rateFn() || 48000;
    if (mirror && mirrorRate === rate) return mirror;
    if (failed && mirrorRate === rate) return null;
    mirror = null; mirrorRate = rate; failed = false;
    try {
      const Off = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      const oc = new Off(1, 128, rate);
      mirror = defs.map((d) => {
        const f = oc.createBiquadFilter();
        f.type = d.type; f.frequency.value = d.freq; f.Q.value = d.Q; f.gain.value = 0;
        return f;
      });
    } catch (_) { mirror = null; failed = true; }
    return mirror;
  }
  function _buf(n) {
    let b = bufs.get(n);
    if (!b) { b = { mag: new Float32Array(n), ph: new Float32Array(n) }; bufs.set(n, b); }
    return b;
  }
  /** Combined response (dB) of the bands at `gains`, at `freqs`, into `out`.
   *  All zeros when no mirror can be built (no OfflineAudioContext). */
  function response(gains, freqs, out = new Float32Array(freqs.length)) {
    out.fill(0);
    const m = _filters();
    if (!m) return out;
    const { mag, ph } = _buf(freqs.length);
    for (let b = 0; b < m.length; b++) {
      const gb = gains[b] || 0;
      if (Math.abs(gb) < 1e-4) continue;          // a 0 dB peaking / shelf band is flat
      m[b].gain.value = gb;
      m[b].getFrequencyResponse(freqs, mag, ph);
      for (let i = 0; i < freqs.length; i++) out[i] += 20 * Math.log10(Math.max(1e-6, mag[i]));
    }
    return out;
  }
  const _peakOut = new Float32Array(PEAK_FREQS.length);
  /** The curve's highest point (dB) over 20 Hz – 20 kHz; null without a mirror. */
  function peakDb(gains) {
    if (!_filters()) return null;
    response(gains, PEAK_FREQS, _peakOut);
    let p = -Infinity;
    for (let i = 0; i < _peakOut.length; i++) if (_peakOut[i] > p) p = _peakOut[i];
    return p;
  }
  return { response, peakDb, get available() { return !!_filters(); } };
}
