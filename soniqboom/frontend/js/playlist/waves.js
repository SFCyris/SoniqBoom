// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * playlist/waves.js — the playlist rows' mini waveforms.
 *
 * Only STORED waveforms are shown (a track gets one when it is played): the
 * rows on screen ask ``POST /api/tracks/meta/waveforms`` in one batch, which
 * never computes a waveform.  A track without one draws a placeholder strip
 * of the waveform's height (unlit cells, or one faint bar).  The playing row
 * redraws its own canvas from the player's timeupdate (≈4 Hz) with the played
 * part lit — no timer of its own.
 *
 * Exports: createWaveStore, drawWave, WAVE_POINTS
 */

export const WAVE_POINTS = 48;
const MAX_CACHE = 5000;
const BATCH = 200;              // the endpoint's cap

/**
 * ``onReady(ids)`` is called with the ids whose state just became known.
 * ``state(id)``: Uint8Array (a waveform), null (none stored) or undefined
 * (not asked yet / on the way).
 */
export function createWaveStore({ onReady, points = WAVE_POINTS } = {}) {
  const cache = new Map();      // id -> Uint8Array | null
  const wanted = new Set();     // ids to ask in the next batch
  const inFlight = new Set();
  let timer = null;
  let busy = false;

  function _remember(id, v) {
    if (cache.has(id)) cache.delete(id);
    cache.set(id, v);
    if (cache.size > MAX_CACHE) cache.delete(cache.keys().next().value);
  }

  async function _flush() {
    timer = null;
    if (busy || !wanted.size) return;
    busy = true;
    const ids = [];
    for (const id of wanted) { ids.push(id); if (ids.length >= BATCH) break; }
    ids.forEach((id) => { wanted.delete(id); inFlight.add(id); });
    let got = null;
    try {
      const res = await fetch('/api/tracks/meta/waveforms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, points }),
      });
      if (res.ok) got = await res.json();
    } catch (_) { got = null; }
    const map = (got && got.waveforms) || {};
    for (const id of ids) {
      inFlight.delete(id);
      const arr = map[id];
      // (A failed request: the placeholder, until the list asks again.)
      _remember(id, Array.isArray(arr) && arr.length ? Uint8Array.from(arr) : null);
    }
    busy = false;
    try { onReady && onReady(ids); } catch (_) {}
    if (wanted.size) _schedule();
  }

  function _schedule() {
    if (timer == null) timer = setTimeout(_flush, 40);
  }

  return {
    state(id) { return cache.get(id); },
    /** Ask for ``id``'s waveform (batched) unless it is known or on the way. */
    want(id) {
      if (!id || cache.has(id) || inFlight.has(id) || wanted.has(id)) return;
      wanted.add(id);
      _schedule();
    },
    /** Forget what is known of ``id`` (it may have a waveform now). */
    forget(id) { cache.delete(id); },
    /** Forget the "none stored" answers (the list was opened again). */
    forgetMissing() { for (const [id, v] of cache) if (v === null) cache.delete(id); },
  };
}

/**
 * Draw ``data`` (Uint8Array 0..255, or null for the placeholder) into
 * ``canvas`` of CSS size ``w``×``h``.  ``progress`` 0..1 lights the played
 * part (the playing row), -1 = not playing.  ``c`` = colours
 * {bar, played, rest, ph, head} and ``cells``: the placeholder as that many
 * unlit cells (Retro's LCD strip), 0 = one flat bar.
 */
export function drawWave(canvas, data, w, h, progress, c) {
  if (!canvas || !(w > 0) || !(h > 0)) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
  if (canvas.width !== pw) canvas.width = pw;
  if (canvas.height !== ph) canvas.height = ph;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const playing = progress >= 0;
  const cut = playing ? Math.max(0, Math.min(1, progress)) * w : -1;
  const mid = h / 2;
  if (!data || !data.length) {
    // No stored waveform: a strip as tall as a full-scale bar, unlit (the
    // played part lit) — cells on the waveform's pitch, or one flat bar.
    const top = 1, sh = Math.max(1, h - 2);
    const n = c.cells > 0 ? Math.min(c.cells, Math.floor(w / 2)) : 0;
    if (n) {
      const step = w / n;
      const bw = Math.max(1, step * 0.62);
      for (let i = 0; i < n; i++) {
        const x = i * step;
        ctx.fillStyle = playing && x + bw / 2 <= cut ? c.played : c.ph;
        ctx.fillRect(x, top, bw, sh);
      }
    } else {
      ctx.fillStyle = c.ph;
      ctx.fillRect(0, top, w, sh);
      if (playing && cut > 0) {
        ctx.fillStyle = c.played;
        ctx.globalAlpha = 0.6;              // (a full-height block: softer than bars)
        ctx.fillRect(0, top, cut, sh);
        ctx.globalAlpha = 1;
      }
    }
  } else {
    const n = data.length;
    const step = w / n;
    const bw = Math.max(1, step * 0.62);
    for (let i = 0; i < n; i++) {
      const x = i * step;
      const amp = data[i] / 255;
      const bh = Math.max(1, amp * (h - 2));
      ctx.fillStyle = playing ? (x + bw / 2 <= cut ? c.played : c.rest) : c.bar;
      ctx.fillRect(x, mid - bh / 2, bw, bh);
    }
  }
  if (playing && cut >= 0) {
    ctx.fillStyle = c.head;
    ctx.fillRect(Math.min(w - 1, cut), 0, 1, h);
  }
}
