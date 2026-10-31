// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/bands.js — the spectrum analyser's bands and ballistics, shared by the
 * full-screen visualizer's analyser mode (visualizer.js) and the phone's Now
 * Playing display (mobile/views/np_visuals.js).  No DOM.
 *
 * Bands are ISO 1/3-octave (25 Hz … 20 kHz) or octaves (a narrow display).
 * Band level = the power of the band's FFT bins (summed — a real 1/3-octave
 * filter bank's reading; a band narrower than a bin reads the bin at its
 * centre, scaled by its width) between AN_FLOOR_DB and AN_CEIL_DB.  Bars rise
 * at once and fall AN_FALL per second; a peak holds AN_HOLD_MS, then falls
 * AN_PEAK_FALL per second.
 *
 *   bandBins(freqs, rate, fft)              → Float64Array, per band [sum?, a, b]
 *   readBands(spec, bins, n, target)        → target[0…n) from a dB spectrum
 *   stepBands(st, dt, now)                  → the bars' and peaks' ballistics
 *   createBandState(n)                      → {target, level, peak, hold}
 */

export const AN_THIRDS = [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630,
  800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000];
export const AN_OCTAVES = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
export const AN_FLOOR_DB = -84;       // band power (dBFS) at an empty bar …
export const AN_CEIL_DB = -18;        // … and at a full one
export const AN_FALL = 1.5;           // a bar falls at most this much (full scale) per second; it rises at once
export const AN_HOLD_MS = 700;        // a peak stays put this long (as the EQ's LED columns) …
export const AN_PEAK_FALL = 0.4;      // … then falls this much (full scale) per second

/** Which bins of an ``fft``-point spectrum at ``rate`` Hz make up each band of
 *  ``freqs`` (AN_THIRDS or AN_OCTAVES): per band [1, a, b] (bins a…b summed)
 *  or [0, centre bin, width in bins] (a band narrower than a bin). */
export function bandBins(freqs, rate, fft) {
  const binHz = rate / fft;
  const last = fft / 2 - 1;
  const step = freqs === AN_THIRDS ? 1 / 3 : 1;              // band width, in octaves
  const out = new Float64Array(freqs.length * 3);
  for (let i = 0; i < freqs.length; i++) {
    // the exact base-2 centre behind the nominal one (800 → 793.7 Hz), so
    // neighbours share their edge and no bin is in two bands
    const fc = 1000 * Math.pow(2, Math.round(Math.log2(freqs[i] / 1000) / step) * step);
    const lo = fc * Math.pow(2, -step / 2) / binHz;
    const hi = Math.min(last + 1, fc * Math.pow(2, step / 2) / binHz);
    const a = Math.max(1, Math.ceil(lo)), b = Math.ceil(hi) - 1;
    if (b >= a) { out[i * 3] = 1; out[i * 3 + 1] = a; out[i * 3 + 2] = b; }
    else { out[i * 3] = 0; out[i * 3 + 1] = Math.min(last - 1, fc / binHz); out[i * 3 + 2] = hi - lo; }
  }
  return out;
}

/** Band levels 0…1 from a dB spectrum (AnalyserNode.getFloatFrequencyData). */
export function readBands(spec, bins, n, target) {
  const span = AN_CEIL_DB - AN_FLOOR_DB;
  for (let i = 0; i < n; i++) {
    let p = 0;
    if (bins[i * 3]) {
      for (let j = bins[i * 3 + 1], e = bins[i * 3 + 2]; j <= e; j++) {
        const d = spec[j];
        if (d > -200) p += Math.pow(10, d / 10);
      }
    } else {
      const c = bins[i * 3 + 1], j = Math.floor(c), fr = c - j;
      const d0 = spec[j], d1 = spec[j + 1];
      if (d0 > -200 && d1 > -200) p = Math.pow(10, (d0 * (1 - fr) + d1 * fr) / 10) * bins[i * 3 + 2];
    }
    const v = p > 0 ? (10 * Math.log10(p) - AN_FLOOR_DB) / span : 0;
    target[i] = v > 1 ? 1 : v > 0 ? v : 0;
  }
  return target;
}

export function createBandState(n) {
  return { target: new Float32Array(n), level: new Float32Array(n),
           peak: new Float32Array(n), hold: new Float64Array(n) };
}

/** Advance the bars (rise at once, fall AN_FALL / s) and their peaks (held
 *  AN_HOLD_MS, then AN_PEAK_FALL / s) towards ``st.target``; true while a bar
 *  or a peak is above the floor. */
export function stepBands(st, dt, now) {
  let up = false;
  const { target, level, peak, hold } = st;
  for (let i = 0; i < level.length; i++) {
    const t = target[i];
    const lv = t >= level[i] ? t : Math.max(t, level[i] - AN_FALL * dt);
    level[i] = lv;
    if (lv >= peak[i]) { peak[i] = lv; hold[i] = now + AN_HOLD_MS; }
    else if (now > hold[i]) peak[i] = Math.max(lv, peak[i] - AN_PEAK_FALL * dt);
    if (lv > 0.002 || peak[i] > 0.002) up = true;
  }
  return up;
}
