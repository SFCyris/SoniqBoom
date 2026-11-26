// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * player/wave_seek.js — seeking by the player bar's waveform (app.js; tests in
 * tests/js/wave_seek.test.mjs).
 *
 * The whole rectangle of the waveform seeks, not only the thin seek bar in its
 * middle: a press anywhere in it — its top edge, its middle, its bottom edge —
 * goes to the place under the pointer's x, and dragging scrubs (the time and
 * the waveform follow; the seek happens on release).  The place is where the
 * waveform draws it: x across the rectangle's width, 0 – 100 %.  The keyboard
 * still seeks through the seek bar (an <input type=range>).
 *
 * The pointer target is ``#waveform-hit``, a transparent box app.js lays over
 * the canvas (above the seek bar, whose thumb still shows under it).  It takes
 * the pointer while a waveform shows (``has-waveform``), and in Retro hi-fi
 * wherever the LCD glass is painted; never in radio mode (a live stream has
 * nothing to seek into) — app.css.
 */

/** The seek position (0 – 100) of ``clientX`` over a box at ``left``, ``width``
 *  px wide; null for a box with no width. */
export function pctAtX(clientX, left, width) {
  const w = Number(width);
  if (!(w > 0)) return null;
  const p = ((Number(clientX) - Number(left)) / w) * 100;
  if (!Number.isFinite(p)) return null;
  return Math.max(0, Math.min(100, p));
}

/**
 * Wire scrubbing on ``el`` (the hit box over the waveform).  ``paint(pct)`` shows a place
 * while the pointer is down, ``commit(pct)`` seeks there on release,
 * ``cancel()`` puts the display back when the press is lost.  Returns
 * ``{ active() }`` — true while a press is scrubbing.
 */
export function bindWaveSeek(el, { paint, commit, cancel } = {}) {
  let press = null;          // { id, pct }
  const at = (e) => {
    const r = el.getBoundingClientRect();
    return pctAtX(e.clientX, r.left, r.width);
  };
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || press) return;
    const pct = at(e);
    if (pct === null) return;
    e.preventDefault();                    // (no text selection, no native drag)
    try { el.setPointerCapture(e.pointerId); } catch (_) { /* an old engine */ }
    press = { id: e.pointerId, pct };
    if (paint) paint(pct);
  });
  el.addEventListener('pointermove', (e) => {
    if (!press || e.pointerId !== press.id) return;
    const pct = at(e);
    if (pct === null) return;
    press.pct = pct;
    if (paint) paint(pct);
  });
  const end = (e, seek) => {
    if (!press || (e && e.pointerId !== press.id)) return;
    const pct = press.pct;
    press = null;
    try { if (e) el.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
    if (seek) { if (commit) commit(pct); }
    else if (cancel) cancel();
  };
  el.addEventListener('pointerup', (e) => {
    if (press && e.pointerId === press.id) {
      const pct = at(e);
      if (pct !== null) press.pct = pct;
    }
    end(e, true);
  });
  el.addEventListener('pointercancel', (e) => end(e, false));
  el.addEventListener('lostpointercapture', (e) => end(e, false));
  return { active: () => !!press };
}
