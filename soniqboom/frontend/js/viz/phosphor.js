// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/phosphor.js — a phosphor oscilloscope trace with persistence, shared by
 * the full-screen visualizer's Retro scope (visualizer.js) and the phone's Now
 * Playing display (mobile/views/np_visuals.js).
 *
 * One trace across the middle, a crisp hot core over a wide soft glow.  The
 * last traces are kept (``keep``, y per sample + when) and drawn again, older
 * ones fainter — alpha = persist ^ (age / 16.7 ms), so the afterglow lasts as
 * long at any frame rate and fades out completely (a destination-out fade of
 * the canvas stalls at 1–10/255 from 8-bit rounding).  The canvas is cleared
 * each frame and stays transparent: what is behind it (a printed graticule)
 * shows through.
 *
 *   createPhosphor({keep, minAlpha}) → {draw(g, W, H, buf, T, k, now), reset()}
 *     g    the canvas's 2D context (W × H device px)
 *     buf  Uint8Array time-domain samples (getByteTimeDomainData; 128 = 0)
 *     T    {trace, traceGlow, traceCore, persist, level} — colours, persistence, strength
 *     k    device px per CSS px (line widths)
 */
export function createPhosphor({ keep = 24, minAlpha = 0.03 } = {}) {
  let hist = [];                 // [{t, ys: Float32Array}] oldest first
  let spare = null;              // a dropped trace's buffer, reused for the next

  function draw(g, W, H, buf, T, k, now) {
    const midY = H / 2;
    const amp = midY * 0.8;
    const step = W / (buf.length - 1);

    // the newest trace, in a recycled buffer
    const maxAge = 16.7 * Math.log(minAlpha) / Math.log(Math.max(0.01, T.persist));
    while (hist.length && (now - hist[0].t > maxAge || hist.length >= keep)) {
      const old = hist.shift();
      if (old.ys.length === buf.length && !spare) spare = old.ys;
    }
    const ys = spare && spare.length === buf.length ? spare : new Float32Array(buf.length);
    spare = null;
    for (let i = 0; i < buf.length; i++) ys[i] = midY - (buf[i] / 128 - 1) * amp;
    hist.push({ t: now, ys });

    g.clearRect(0, 0, W, H);
    g.save();
    g.lineJoin = 'round';
    g.lineCap = 'round';
    const trace = (p) => {
      g.beginPath();
      g.moveTo(0, p[0]);
      for (let i = 1; i < p.length; i++) g.lineTo(i * step, p[i]);
    };
    // the afterglow: the older traces, one stroke each
    g.strokeStyle = T.trace;
    g.lineWidth = 2 * k;
    const last = hist.length - 1;
    for (let j = 0; j < last; j++) {
      const h = hist[j];
      if (h.ys.length !== buf.length) continue;
      g.globalAlpha = T.level * 0.85 * Math.pow(T.persist, (now - h.t) / 16.7);
      trace(h.ys);
      g.stroke();
    }
    g.globalAlpha = T.level;
    // the beam: glow, line, hot core
    trace(ys);
    g.strokeStyle = T.traceGlow;
    g.lineWidth = 7 * k;
    g.stroke();
    g.strokeStyle = T.trace;
    g.lineWidth = 2 * k;
    g.stroke();
    g.strokeStyle = T.traceCore;
    g.lineWidth = 0.9 * k;
    g.stroke();
    g.restore();
  }

  return {
    draw,
    reset() { hist = []; spare = null; },
    get traces() { return hist.length; },
  };
}
