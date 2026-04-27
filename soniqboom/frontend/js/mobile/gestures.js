// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * gestures.js — Touch-first interaction primitives for the mobile shell.
 *
 * Pointer Events only — no mousedown/mousemove, no HTML5 DnD.
 * Each helper attaches to a single row element and is cleaned up via the
 * returned `destroy()` function when the view re-renders.
 */

const LONG_PRESS_MS = 500;
const MOVE_TOLERANCE = 8;          // px — cancels long-press if exceeded
const SWIPE_TRIGGER_PCT = 0.30;    // fraction of row width to commit a swipe
const SWIPE_AXIS_LOCK   = 10;      // px before deciding horiz vs vert

/**
 * Attach long-press, swipe-to-action, and tap to a row's content layer.
 *
 * Options:
 *   onTap()              — fired on quick tap with no swipe/long-press
 *   onLongPress()        — fired after LONG_PRESS_MS hold without movement
 *   onSwipeAction()      — fired when user swipes past trigger threshold
 *   swipeLabel           — text shown in the reveal background (default "Remove")
 *   swipeBgClass         — extra class on the reveal bg (e.g. "queue")
 */
export function attachRowGestures(row, opts = {}) {
  const content = row.querySelector('.m-row-content');
  if (!content) return () => {};

  // Build the swipe-reveal background once (lazy)
  let swipeBg = null;
  function ensureSwipeBg() {
    if (swipeBg) return;
    swipeBg = document.createElement('div');
    swipeBg.className = 'm-row-swipe-bg' + (opts.swipeBgClass ? ` ${opts.swipeBgClass}` : '');
    swipeBg.textContent = opts.swipeLabel || 'Remove';
    row.insertBefore(swipeBg, content);
  }

  let pointerId   = null;
  let startX      = 0;
  let startY      = 0;
  let dx          = 0;
  let dy          = 0;
  let axis        = null;          // null | 'h' | 'v'
  let longTimer   = null;
  let cancelled   = false;
  let didSwipe    = false;

  function onDown(e) {
    if (pointerId !== null) return;
    // Allow the long-press timer for any pointer type (touch, pen, mouse)
    pointerId = e.pointerId;
    startX = e.clientX;
    startY = e.clientY;
    dx = dy = 0;
    axis = null;
    cancelled = false;
    didSwipe = false;

    if (opts.onLongPress) {
      longTimer = setTimeout(() => {
        if (cancelled) return;
        // Cancel any in-flight swipe state and fire menu
        cancelled = true;
        opts.onLongPress();
        if (navigator.vibrate) navigator.vibrate(15);
      }, LONG_PRESS_MS);
    }
  }

  function onMove(e) {
    if (e.pointerId !== pointerId) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;

    // Decide axis once movement exceeds lock threshold
    if (axis === null && (Math.abs(dx) > SWIPE_AXIS_LOCK || Math.abs(dy) > SWIPE_AXIS_LOCK)) {
      axis = Math.abs(dx) > Math.abs(dy) ? 'h' : 'v';
      if (axis === 'h') {
        // Capture pointer so the row keeps tracking even if finger leaves bounds
        try { row.setPointerCapture(pointerId); } catch (_) {}
      }
    }

    // Any meaningful movement cancels long-press
    if (Math.hypot(dx, dy) > MOVE_TOLERANCE) {
      clearTimeout(longTimer); longTimer = null;
    }

    // Vertical scroll: let the OS handle it, abort our gesture
    if (axis === 'v') return;

    // Horizontal swipe: only allow if onSwipeAction is configured, only leftwards
    if (axis === 'h' && opts.onSwipeAction) {
      ensureSwipeBg();
      // Clamp to leftwards translation only; resist over-pull
      const tx = Math.min(0, dx);
      content.style.transform = `translateX(${tx}px)`;
      e.preventDefault();
    }
  }

  function onUp(e) {
    if (e.pointerId !== pointerId) return;
    clearTimeout(longTimer); longTimer = null;
    try { row.releasePointerCapture(pointerId); } catch (_) {}
    pointerId = null;

    if (cancelled) { resetTransform(); return; }

    if (axis === 'h' && opts.onSwipeAction) {
      const width = row.getBoundingClientRect().width;
      if (Math.abs(dx) >= width * SWIPE_TRIGGER_PCT) {
        // Animate fully out, then fire action
        didSwipe = true;
        content.style.transition = 'transform 0.18s ease-out';
        content.style.transform = `translateX(-${width}px)`;
        setTimeout(() => {
          opts.onSwipeAction();
          // Caller is expected to re-render the list; if not, snap back
          resetTransform();
        }, 180);
        return;
      }
      resetTransform(true);
      return;
    }

    // Plain tap (no swipe, no long-press fired)
    if (axis === null && opts.onTap) {
      opts.onTap(e);
    }
    resetTransform();
  }

  function onCancel(e) {
    if (e.pointerId !== pointerId) return;
    clearTimeout(longTimer); longTimer = null;
    pointerId = null;
    resetTransform(true);
  }

  function resetTransform(animate = false) {
    if (!content.style.transform) return;
    if (animate) content.style.transition = 'transform 0.15s ease-out';
    content.style.transform = '';
    if (animate) setTimeout(() => { content.style.transition = ''; }, 160);
    else content.style.transition = '';
  }

  row.addEventListener('pointerdown',   onDown);
  row.addEventListener('pointermove',   onMove);
  row.addEventListener('pointerup',     onUp);
  row.addEventListener('pointercancel', onCancel);

  return function destroy() {
    row.removeEventListener('pointerdown',   onDown);
    row.removeEventListener('pointermove',   onMove);
    row.removeEventListener('pointerup',     onUp);
    row.removeEventListener('pointercancel', onCancel);
    clearTimeout(longTimer);
  };
}

/**
 * Drag-reorder a list of rows. Initiated only on the visible drag handle
 * (`.m-row-handle`), so the rest of the row remains tap/swipe-able.
 *
 * Options:
 *   onReorder(fromIdx, toIdx) — fired once on commit
 *   getRows()                 — returns the live array of row DOM nodes
 */
export function attachDragReorder(container, opts = {}) {
  let pointerId = null;
  let dragRow   = null;
  let fromIdx   = -1;
  let toIdx     = -1;
  let startY    = 0;

  function rows() {
    return opts.getRows ? opts.getRows() : Array.from(container.querySelectorAll('.m-row'));
  }

  function indexOfRow(row) {
    return rows().indexOf(row);
  }

  function onHandleDown(e) {
    const handle = e.target.closest('.m-row-handle');
    if (!handle) return;
    const row = handle.closest('.m-row');
    if (!row) return;

    pointerId = e.pointerId;
    dragRow   = row;
    fromIdx   = indexOfRow(row);
    toIdx     = fromIdx;
    startY    = e.clientY;

    row.classList.add('dragging');
    try { handle.setPointerCapture(pointerId); } catch (_) {}
    e.preventDefault();
  }

  function onMove(e) {
    if (e.pointerId !== pointerId || !dragRow) return;

    // Translate the dragged row to follow the finger
    const dy = e.clientY - startY;
    dragRow.style.transform = `translateY(${dy}px)`;

    // Find which row we're hovering over
    clearDropMarkers();
    const rs = rows();
    for (let i = 0; i < rs.length; i++) {
      const r = rs[i];
      if (r === dragRow) continue;
      const rect = r.getBoundingClientRect();
      if (e.clientY >= rect.top && e.clientY <= rect.bottom) {
        const above = e.clientY < rect.top + rect.height / 2;
        r.classList.add(above ? 'drop-above' : 'drop-below');
        toIdx = above ? i : i + 1;
        // Index correction: removing the dragged row shifts the index when moving down
        if (toIdx > fromIdx) toIdx -= 1;
        return;
      }
    }
  }

  function onUp(e) {
    if (e.pointerId !== pointerId || !dragRow) return;
    try { dragRow.releasePointerCapture(pointerId); } catch (_) {}
    pointerId = null;

    dragRow.classList.remove('dragging');
    dragRow.style.transform = '';
    clearDropMarkers();

    if (toIdx !== fromIdx && toIdx >= 0 && opts.onReorder) {
      opts.onReorder(fromIdx, toIdx);
    }
    dragRow = null;
    fromIdx = toIdx = -1;
  }

  function onCancel(e) {
    if (e.pointerId !== pointerId) return;
    if (dragRow) {
      dragRow.classList.remove('dragging');
      dragRow.style.transform = '';
    }
    clearDropMarkers();
    pointerId = null;
    dragRow = null;
  }

  function clearDropMarkers() {
    container.querySelectorAll('.drop-above, .drop-below').forEach(r => {
      r.classList.remove('drop-above', 'drop-below');
    });
  }

  container.addEventListener('pointerdown',   onHandleDown);
  container.addEventListener('pointermove',   onMove);
  container.addEventListener('pointerup',     onUp);
  container.addEventListener('pointercancel', onCancel);

  return function destroy() {
    container.removeEventListener('pointerdown',   onHandleDown);
    container.removeEventListener('pointermove',   onMove);
    container.removeEventListener('pointerup',     onUp);
    container.removeEventListener('pointercancel', onCancel);
  };
}
