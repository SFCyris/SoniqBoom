// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * playlist/listkit.js — what the playlist view's rows and the queue panel's
 * rows share: the mini-waveform painter, the rows' glide after a move (FLIP),
 * the drop line and the gap under the pointer, the touch long-press that
 * starts a drag, and a few pure helpers.
 *
 * Rows are pooled virtual-list rows (vlist.js) built by their owner with a
 * ``canvas`` in ``row.__wave``, the entry they show in ``row.__trk`` and their
 * index in ``row.dataset.idx``.
 *
 * Exports: createWavePainter, rowTops, flipFrom, clearFlip, createDropLine,
 *          gapAt, bindLongPressDrag, moveTarget, aheadOf, fmtDur, esc
 */
import { drawWave } from './waves.js';

/** "3:07" (blank for an unknown length). */
export function fmtDur(sec) {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}

/** Text → safe HTML text / attribute value. */
export function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Mini waveforms ───────────────────────────────────────────────────────────
/**
 * ``store``: a waves.js createWaveStore.  ``colorsFrom``: the element whose CSS
 * variables ``--<prefix>-wave``, ``-wave-played``, ``-wave-rest``, ``-wave-ph``,
 * ``-wave-head`` give the colours, ``-wave-ph-cells`` the placeholder's cells
 * (0 / unset = a flat bar).  ``stored(track)``: whether the track's
 * stored waveform is its picture (false → the placeholder, e.g. a subsong).
 *
 *   paint(row, track, progress)  draw (progress 0..1 = the playing row, -1 = not);
 *                                an unchanged picture is not redrawn
 *   invalidate(rows)             layout / look / theme changed: measure and read
 *                                the colours again; ``rows``' next paint redraws
 */
export function createWavePainter({ store, colorsFrom, prefix, stored = () => true }) {
  let box = null;               // {w, h} of a row's canvas (CSS px), measured once per layout
  let cols = null;              // colours read from the CSS variables

  function _box(row) {
    if (box) return box;
    const r = row.__wave.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return null;     // hidden (narrow) or not laid out yet
    box = { w: Math.round(r.width), h: Math.round(r.height) };
    return box;
  }
  function _cols() {
    if (cols) return cols;
    const cs = getComputedStyle(colorsFrom);
    const v = (k, d) => (cs.getPropertyValue(`--${prefix}-${k}`) || '').trim() || d;
    cols = {
      bar: v('wave', 'rgba(168,173,182,0.75)'),
      played: v('wave-played', '#f0722a'),
      rest: v('wave-rest', 'rgba(168,173,182,0.45)'),
      ph: v('wave-ph', 'rgba(168,173,182,0.45)'),
      head: v('wave-head', '#ffffff'),
      cells: parseInt(v('wave-ph-cells', '0'), 10) || 0,
    };
    return cols;
  }

  function paint(row, track, progress = -1) {
    const cv = row && row.__wave;
    if (!cv || !track) return;
    const b = _box(row);
    if (!b) return;
    let data = null;
    if (track.id && stored(track)) {
      const st = store.state(track.id);
      if (st === undefined) store.want(track.id);
      else data = st;
    }
    const px = progress >= 0 ? Math.round(Math.max(0, Math.min(1, progress)) * b.w) : -1;
    const key = `${track.id}|${data ? 1 : 0}|${px}|${b.w}x${b.h}`;
    if (cv.__key === key) return;          // (the same picture: nothing to draw)
    cv.__key = key;
    cv.dataset.wave = data ? 'data' : 'none';
    drawWave(cv, data, b.w, b.h, progress, _cols());
  }

  function invalidate(rows = []) {
    box = null;
    cols = null;
    for (const row of rows) { if (row.__wave) row.__wave.__key = ''; }
  }

  return { paint, invalidate };
}

// The narrow-window rule that shrinks the waveform (playlist-styles.css): the
// canvas changes size without its list changing size.
export const NARROW_WAVES = '(max-width: 520px)';

// ── Rows glide to their new places (FLIP) ────────────────────────────────────
/** Where each entry's row is now: entry → [top, …] (an entry listed twice: both, in order). */
export function rowTops(rows) {
  const m = new Map();
  for (const r of rows) {
    if (!r.__trk) continue;
    const top = r.getBoundingClientRect().top;
    const a = m.get(r.__trk);
    if (a) a.push(top); else m.set(r.__trk, [top]);
  }
  return m;
}

/** After a re-render: animate every row whose entry was elsewhere in ``before``. */
export function flipFrom(rows, before, container, reduced = false) {
  if (!before.size || reduced) return;
  const seen = new Map();
  const moved = [];
  for (const r of rows) {
    if (!r.__trk) continue;
    const tops = before.get(r.__trk);
    if (!tops) continue;
    const k = seen.get(r.__trk) || 0;
    seen.set(r.__trk, k + 1);
    if (k >= tops.length) continue;
    const dy = tops[k] - r.getBoundingClientRect().top;
    if (Math.abs(dy) < 1) continue;
    r.style.transition = 'none';
    r.style.transform = `translateY(${dy}px)`;
    moved.push(r);
  }
  if (!moved.length) return;
  void container.offsetHeight;               // commit the start positions
  for (const r of moved) {
    r.style.transition = 'transform 220ms cubic-bezier(.2,.7,.2,1)';
    r.style.transform = '';
    r.addEventListener('transitionend', () => { r.style.transition = ''; }, { once: true });
  }
}

/** Stop a glide at once (the rows are about to show other entries). */
export function clearFlip(rows) {
  for (const r of rows) { r.style.transition = ''; r.style.transform = ''; }
}

// ── The drop line ────────────────────────────────────────────────────────────
/** A line across ``listEl`` (drawn in ``panel``) at a gap between two rows. */
export function createDropLine(panel, cls = '') {
  const el = document.createElement('div');
  el.className = `plx-dropline${cls ? ' ' + cls : ''}`;
  el.hidden = true;
  el.setAttribute('aria-hidden', 'true');
  panel.appendChild(el);
  return {
    el,
    show(y, listEl) {
      const pr = panel.getBoundingClientRect();
      const lr = listEl.getBoundingClientRect();
      const top = Math.max(lr.top, Math.min(lr.bottom, y)) - pr.top;
      el.style.transform = `translateY(${Math.round(top - 1)}px)`;
      el.style.left = `${Math.round(lr.left - pr.left + 6)}px`;
      el.style.width = `${Math.max(0, Math.round(lr.width - 12))}px`;
      el.hidden = false;
    },
    hide() { el.hidden = true; },
  };
}

/**
 * The gap the pointer points at: before ``row`` (upper half) or after it
 * (lower half); below ``lastRow`` when it is the list's last (index
 * ``count - 1``) → the end.  {gap, y} or null.
 */
export function gapAt(e, row, lastRow, count) {
  if (row) {
    const r = row.getBoundingClientRect();
    const i = parseInt(row.dataset.idx, 10);
    const after = e.clientY >= r.top + r.height / 2;
    return { gap: after ? i + 1 : i, y: after ? r.bottom : r.top };
  }
  if (lastRow && +lastRow.dataset.idx === count - 1) {
    const r = lastRow.getBoundingClientRect();
    if (e.clientY >= r.bottom - 1) return { gap: count, y: r.bottom };
  }
  return null;
}

/** One entry at ``from`` dropped into gap ``gap`` (0..count): its new index, or -1 (no move). */
export function moveTarget(from, gap) {
  if (!(from >= 0) || !(gap >= 0)) return -1;
  const to = gap > from ? gap - 1 : gap;
  return to === from ? -1 : to;
}

// ── Touch: a long press on the handle starts a drag ──────────────────────────
// Native drag-and-drop is mouse-only; a 350 ms hold on ``.queue-drag-handle``
// makes the row draggable long enough for the finger to drag.  Movement over
// 8 px before the hold cancels.  A tiny vibrate gives haptic feedback.
export function bindLongPressDrag(listEl, rowOf) {
  let timer = null, x0 = 0, y0 = 0, row = null;
  listEl.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1 || !e.target.closest('.queue-drag-handle')) return;
    const r = rowOf(e);
    if (!r) return;
    row = r;
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      r.draggable = true;
      try { navigator.vibrate?.(8); } catch (_) {}
    }, 350);
  }, { passive: true });
  listEl.addEventListener('touchmove', (e) => {
    if (!timer) return;
    const t = e.touches[0];
    if (!t) return;
    if (Math.hypot(t.clientX - x0, t.clientY - y0) > 8) {
      clearTimeout(timer);
      timer = null;
    }
  }, { passive: true });
  listEl.addEventListener('touchend', () => {
    if (timer) { clearTimeout(timer); timer = null; }
    const r = row;
    row = null;
    if (r) setTimeout(() => { r.draggable = false; }, 0);
  });
  listEl.addEventListener('touchcancel', () => {
    if (timer) { clearTimeout(timer); timer = null; }
    if (row) row.draggable = false;
    row = null;
  });
}

// ── What comes after a position ──────────────────────────────────────────────
/**
 * The entries after index ``at`` (all of them for ``at`` < 0) that will play:
 * ``{n, sec, unknown}`` — how many, their summed known lengths, and how many
 * of them have no length yet.  ``skips(entry)`` true = passed by (not counted).
 */
export function aheadOf(entries, at, skips = () => false) {
  let n = 0, sec = 0, unknown = 0;
  for (let i = Math.max(-1, at) + 1; i < entries.length; i++) {
    const t = entries[i];
    if (!t || skips(t)) continue;
    n++;
    const d = Number(t.duration);
    if (d > 0) sec += d; else unknown++;
  }
  return { n, sec, unknown };
}
