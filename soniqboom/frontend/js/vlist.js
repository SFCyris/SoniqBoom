// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * vlist.js — pooled virtual list with a fixed row height.
 *
 * Shared by the library track table, the group lists (Artists / Albums / …),
 * the album grid, the playlist panel and the phone's Library view.  Only the
 * rows in and around the viewport exist; two spacer elements hold the height
 * of everything above and below them.
 *
 * Rows are KEYED by their index: scrolling by one row recycles one row (the one
 * that left the window) for the one that entered — the other rows are not
 * touched, so their covers, focus and running animations (the now-playing
 * bars, the loading shimmer) carry on.  A forced render (new data under the
 * same indexes) hands every row to ``fill`` with ``fresh = false``; the owner
 * decides what, if anything, to repaint.
 *
 * Very long lists: an element can only be so tall (Firefox stops at ~17.9 M px,
 * which 500,000 track rows exceed).  Above ``VL_MAX_PX`` the spacers are capped
 * and the scroll position maps onto the rows proportionally (1:1 for the first
 * and last few rows, so both ends line up exactly) — the same in every engine.
 *
 * Exports: VL_MAX_PX, vlGeometry, vlToV, vlToS, vlWindow, vlReconcile,
 *          createVirtualList
 */

export const VL_MAX_PX = 15_000_000;

// [vlist-core:begin] — pure geometry + pool planning (tests/js/vlist.test.mjs)

// Geometry of a list of ``n`` rows of ``rowH`` px seen through ``viewH`` px,
// rendering ``buf`` extra rows on each side.  ``total`` is the height the
// spacers + rows add up to; ``scaled`` lists map scroll offsets onto rows.
function vlGeometry(n, rowH, viewH, buf, maxPx = VL_MAX_PX) {
  n = Math.max(0, n | 0);
  rowH = rowH > 0 ? rowH : 1;
  viewH = Math.max(0, viewH || 0);
  const full = n * rowH;
  if (full <= maxPx) return { n, rowH, viewH, buf, total: full, scaled: false };
  // 1:1 zones at both ends keep both spacers ≥ 0 (see vlWindow); the middle
  // stretches linearly.  ``edge`` must be ≥ (buf + 1) rows.
  const edge = (buf + 2) * rowH;
  const sMax = Math.max(0, maxPx - viewH);   // scroll offset at the end
  const vMax = Math.max(0, full - viewH);    // virtual offset at the end
  const k = (vMax - 2 * edge) / Math.max(1, sMax - 2 * edge);
  return { n, rowH, viewH, buf, total: maxPx, scaled: true, edge, sMax, vMax, k };
}

// Scroll offset (px into the list) → virtual offset (px into the full list).
function vlToV(g, s) {
  if (!g.scaled || s <= g.edge) return s;
  if (s >= g.sMax - g.edge) return s + (g.vMax - g.sMax);
  return g.edge + (s - g.edge) * g.k;
}

// Virtual offset → scroll offset (inverse of vlToV).
function vlToS(g, v) {
  if (!g.scaled || v <= g.edge) return v;
  if (v >= g.vMax - g.edge) return v - (g.vMax - g.sMax);
  return g.edge + (v - g.edge) / g.k;
}

// Rows to render for scroll offset ``s`` + the two spacer heights.
// Row i sits at ``top + (i - start) * rowH`` from the list's top.
function vlWindow(g, s) {
  const { n, rowH, viewH, buf } = g;
  if (!n) return { start: 0, end: 0, top: 0, bottom: 0 };
  const v = vlToV(g, s);
  const first = Math.floor(v / rowH);
  const last = Math.ceil((v + viewH) / rowH);
  const start = Math.min(n, Math.max(0, first - buf));
  const end = Math.min(n, Math.max(start, last + buf));
  const top = g.scaled ? s - v + start * rowH : start * rowH;
  const bottom = g.total - top - (end - start) * rowH;
  return { start, end, top: Math.max(0, top), bottom: Math.max(0, bottom) };
}

// Plan which pooled rows show which index.  ``have`` = the indexes the pool's
// rows show now, in DOM order (contiguous, ascending; ``undefined`` for a row
// that shows nothing).  Returns ``rows``: one entry per index of [start, end)
// in order — ``{ from, idx }`` with ``from`` the pool slot it reuses, or -1 for
// a new row — plus ``drop`` (slots no longer needed).  A row whose index stays
// in the window keeps its slot (``kept``); rows that leave are reused for the
// indexes that enter, the ones on the far side first.
function vlReconcile(have, start, end) {
  const keepSlot = new Map();
  const freeAbove = [], freeBelow = [];
  let seenKept = false;
  have.forEach((idx, slot) => {
    if (idx !== undefined && idx >= start && idx < end && !keepSlot.has(idx)) {
      keepSlot.set(idx, slot); seenKept = true;
    } else (seenKept ? freeBelow : freeAbove).push(slot);
  });
  let kS = end, kE = end;
  if (keepSlot.size) {
    kS = Math.min(...keepSlot.keys()); kE = Math.max(...keepSlot.keys()) + 1;
  }
  const rows = [];
  // Entering above the kept run: rows that sit above it need no move; take the
  // ones nearest the run first.  Then the ones from below (they move up).
  const above = freeAbove.slice(), below = freeBelow.slice();
  const take = (preferAbove) => {
    if (preferAbove) return above.length ? above.pop() : (below.length ? below.pop() : -1);
    return below.length ? below.shift() : (above.length ? above.shift() : -1);
  };
  if (!keepSlot.size) {
    // Nothing stays (a jump, a fresh pool): reuse the rows in DOM order, new
    // ones after them.
    for (let i = start; i < end; i++) rows.push({ from: take(false), idx: i, kept: false });
    return { rows, drop: [...above, ...below] };
  }
  const top = [];
  for (let i = Math.min(kS, end) - 1; i >= start; i--) top.push({ from: take(true), idx: i, kept: false });
  top.reverse();
  rows.push(...top);
  for (let i = kS; i < kE; i++) {
    if (keepSlot.has(i)) rows.push({ from: keepSlot.get(i), idx: i, kept: true });
    else rows.push({ from: take(false), idx: i, kept: false });   // a hole in the run (duplicate index)
  }
  for (let i = Math.max(kE, start); i < end; i++) rows.push({ from: take(false), idx: i, kept: false });
  return { rows, drop: [...above, ...below] };
}
// [vlist-core:end]

export { vlGeometry, vlToV, vlToS, vlWindow, vlReconcile };

/**
 * createVirtualList(opts) — a pooled list on ``opts.host`` scrolled by
 * ``opts.scroller``.
 *
 *   make()                 → a new, empty row element
 *   fill(row, idx, fresh)  → paint row ``idx``; ``fresh`` = the row showed
 *                            another index (or nothing) before.  With
 *                            ``fresh = false`` (a forced render) the row already
 *                            shows ``idx``: repaint only what may have changed.
 *                            ``row.__vlMoved`` is true when the row was just
 *                            (re)inserted (its CSS animations restarted); the
 *                            owner may clear it.
 *   spacer(which)          → a new spacer element ('top' | 'bottom')
 *   setSpacer(el, px)      → give a spacer its height
 *   rowHeight              → initial row height in px (re-measured)
 *   measurable(row)        → may this row's height be measured?  (default: yes)
 *   measure(row)           → its height (default: getBoundingClientRect().height)
 *   keyOf(idx)             → what row ``idx`` shows (e.g. its track id); a
 *                            row that held the focus gets it back only when
 *                            its index shows the same thing again
 *   offset()               → px from the scroller's content top to the list top
 *   topInset()             → px of the viewport's top covered by sticky chrome
 *   buffer                 → rows rendered beyond each edge (default 10)
 *   autoScroll             → listen to the scroller's scroll events itself
 *                            (rAF-throttled); otherwise the owner calls render()
 *   observeResize          → re-render when the scroller changes size
 *                            (default: autoScroll)
 *   onResize()             → before that re-render (e.g. re-measure columns)
 *   onRender(start, end)   → after every render that changed something
 *
 * Several lists may share one scroller + host (the library's track table and
 * its group lists): only the one whose spacers are in the host renders; reset()
 * the outgoing one when the host is handed over.
 */
export function createVirtualList(opts) {
  const scroller = opts.scroller, host = opts.host;
  const buf = opts.buffer ?? 10;
  const maxPx = opts.maxPx ?? VL_MAX_PX;
  let rowH = opts.rowHeight || 30;
  let measured = false;
  let n = 0;
  let pool = [];             // rows in DOM (and index) order
  let topSp = null, botSp = null;
  let start = 0, end = 0, lastTop = -1, lastBottom = -1;
  let geo = null;
  // A row that held the focus and scrolled out: refocus its element when that
  // index comes back showing the same thing, unless the focus went somewhere
  // else meanwhile.
  let lostFocus = null;      // { idx, key, path: [childIndex…] }
  const keyOf = (idx) => (opts.keyOf ? opts.keyOf(idx) : undefined);

  try { scroller.style.overflowAnchor = 'none'; } catch (_) { /* old engine */ }

  const offsetOf = () => (opts.offset ? opts.offset() : 0);
  const insetOf = () => (opts.topInset ? opts.topInset() : 0);
  const viewOf = () => Math.max(0, scroller.clientHeight - insetOf());
  const sOf = () => scroller.scrollTop + insetOf() - offsetOf();

  function attached() { return !!topSp && topSp.parentNode === host; }
  function attach() {
    if (!topSp) topSp = opts.spacer('top');
    if (!botSp) botSp = opts.spacer('bottom');
    host.replaceChildren(topSp, ...pool, botSp);
  }

  function pathOf(el, row) {
    const path = [];
    for (let e = el; e && e !== row; e = e.parentNode) {
      path.unshift(Array.prototype.indexOf.call(e.parentNode.children, e));
    }
    return path;
  }
  function elAt(row, path) {
    let e = row;
    for (const i of path) { e = e && e.children[i]; }
    return e || null;
  }
  function noteFocus(row) {
    const a = document.activeElement;
    const idx = row.__vlIdx;
    if (a && a !== row && row.contains(a)) lostFocus = { idx, key: keyOf(idx), path: pathOf(a, row) };
    else if (a === row) lostFocus = { idx, key: keyOf(idx), path: [] };
  }
  function focusIsFree() {
    const a = document.activeElement;
    return !a || a === document.body || a === document.documentElement || !a.isConnected;
  }

  function geometry() {
    geo = vlGeometry(n, rowH, viewOf(), buf, maxPx);
    return geo;
  }

  function render(force = false) {
    if (!attached()) { attach(); force = true; }
    const g = geometry();
    const w = vlWindow(g, sOf());
    const moved = w.start !== start || w.end !== end;
    if (!force && !moved && w.top === lastTop && w.bottom === lastBottom) return false;
    if (w.top !== lastTop) { opts.setSpacer(topSp, w.top); lastTop = w.top; }
    if (w.bottom !== lastBottom) { opts.setSpacer(botSp, w.bottom); lastBottom = w.bottom; }
    if (moved || force) place(w.start, w.end, force);
    start = w.start; end = w.end;
    if (!measured && pool.length) measureNow();
    if (opts.onRender) opts.onRender(start, end);
    return true;
  }

  function place(ns, ne, force) {
    const plan = vlReconcile(pool.map((r) => r.__vlIdx), ns, ne);
    const old = pool;
    for (const slot of plan.drop) {
      const r = old[slot];
      noteFocusIfAny(r);
      r.remove();
      r.__vlIdx = undefined;
    }
    const next = plan.rows.map(({ from, idx, kept }) => {
      let row;
      if (from >= 0) row = old[from];
      else { row = opts.make(); row.__vlIdx = undefined; }
      return { row, idx, kept };
    });
    // DOM order: walk backwards from the bottom spacer.  Kept rows are anchors
    // and never move (an untouched row keeps its focus and its CSS animations);
    // a reused row moves only when it isn't already right before the next one.
    let ref = botSp;
    for (let i = next.length - 1; i >= 0; i--) {
      const { row, kept } = next[i];
      if (!kept && (row.nextSibling !== ref || row.parentNode !== host)) {
        if (row.parentNode === host) noteFocusIfAny(row);
        host.insertBefore(row, ref);
        row.__vlMoved = true;      // (re)inserted: its CSS animations restarted
      }
      ref = row;
    }
    pool = next.map((x) => x.row);
    for (const { row, idx, kept } of next) {
      if (!kept) {
        if (row.__vlIdx !== undefined && row.__vlIdx !== idx) noteFocusIfAny(row);
        row.__vlIdx = idx;
        opts.fill(row, idx, true);
        if (lostFocus && lostFocus.idx === idx) {
          const same = lostFocus.key === keyOf(idx);
          const el = same && focusIsFree() ? elAt(row, lostFocus.path) : null;
          lostFocus = null;
          if (el && typeof el.focus === 'function') { try { el.focus({ preventScroll: true }); } catch (_) {} }
        }
      } else if (force) {
        opts.fill(row, idx, false);
      }
    }
  }
  // A row about to show another index holds the focus: remember where, and let
  // go of it (the element is about to stand for something else).
  function noteFocusIfAny(row) {
    if (row && row.__vlIdx !== undefined && row.contains(document.activeElement)) {
      noteFocus(row);
      try { document.activeElement.blur(); } catch (_) { /* nothing focused */ }
    }
  }

  function measureNow() {
    const row = pool.find((r) => (opts.measurable ? opts.measurable(r) : true));
    if (!row) return;
    const h = opts.measure ? opts.measure(row) : row.getBoundingClientRect().height;
    if (!(h > 0)) return;
    measured = true;
    if (Math.abs(h - rowH) >= 0.25) {
      // Keep the same first row on screen across the new geometry.
      const s = sOf();
      const firstIdx = geo ? vlToV(geo, Math.max(0, s)) / rowH : 0;
      rowH = h;
      const g = geometry();
      if (s > 0) {
        const want = vlToS(g, firstIdx * rowH) - insetOf() + offsetOf();
        if (Math.abs(scroller.scrollTop - want) >= 1) scroller.scrollTop = want;
      }
      lastTop = lastBottom = -1;
      render(true);
    }
  }

  // ── scroll listener (optional) ─────────────────────────────────────────
  let rafPending = false, lastY = -1;
  const onScroll = () => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      const y = scroller.scrollTop;
      if (y === lastY) return;
      lastY = y;
      if (n && attached()) render(false);
    });
  };
  if (opts.autoScroll) scroller.addEventListener('scroll', onScroll, { passive: true });
  let ro = null;
  if ((opts.observeResize ?? opts.autoScroll) && typeof ResizeObserver === 'function') {
    let lastH = -1, lastW = -1;
    ro = new ResizeObserver(() => {
      const h = scroller.clientHeight, w = scroller.clientWidth;
      if (h === lastH && w === lastW) return;
      lastH = h; lastW = w;
      // A new width can move a CSS breakpoint (paddings, hidden columns):
      // measure the row height again — also for a list not on screen now.
      measured = false;
      // (A hidden scroller has no size: nothing to render until it shows.)
      if (n && attached() && h > 0) { if (opts.onResize) opts.onResize(); render(true); }
    });
    ro.observe(scroller);
  }

  return {
    get count() { return n; },
    get start() { return start; },
    get end() { return end; },
    get rowHeight() { return rowH; },
    get scaled() { return !!(geo && geo.scaled); },
    // New row count.  The rows on screen are repainted by the next render(true).
    setCount(count) { n = Math.max(0, count | 0); },
    // Forget the pool (its host was emptied by someone else, or the list is
    // being switched to another data set).  The next render starts afresh.
    reset() {
      pool.forEach((r) => { r.__vlIdx = undefined; });
      pool = []; topSp = null; botSp = null;
      start = end = 0; lastTop = lastBottom = -1; lastY = -1; lostFocus = null;
      measured = false;          // the next list's rows are measured afresh
    },
    render,
    // Row height changed (theme, font size, viewport breakpoint): re-measure.
    remeasure() { measured = false; if (n && attached()) render(true); },
    setRowHeight(h) { if (h > 0) { rowH = h; measured = true; } },
    rows() { return pool; },
    rowFor(idx) { return pool.find((r) => r.__vlIdx === idx) || null; },
    indexOf(row) { return row ? row.__vlIdx : undefined; },
    // scrollTop that puts row ``idx`` at the top of the list's viewport.
    scrollTopFor(idx) {
      const g = geometry();
      return Math.max(0, vlToS(g, Math.max(0, idx) * rowH) - insetOf() + offsetOf());
    },
    // Scroll so row ``idx`` is visible: 'start' (top), 'center', or 'nearest'.
    scrollToIndex(idx, align = 'start') {
      if (!n) return;
      idx = Math.max(0, Math.min(n - 1, idx));
      const g = geometry();
      const view = viewOf();
      const v = idx * rowH;
      const s = sOf();
      const cur = vlToV(g, Math.max(0, s));
      let wantV;
      if (align === 'center') wantV = v - (view - rowH) / 2;
      else if (align === 'nearest') {
        // Already fully visible: just make sure it is rendered (the scroll
        // position may have moved since the last frame, e.g. by a focus()).
        if (v >= cur && v + rowH <= cur + view) { render(false); return; }
        wantV = v < cur ? v : v + rowH - view;
      } else wantV = v;
      wantV = Math.max(0, wantV);
      scroller.scrollTop = Math.max(0, vlToS(g, wantV) - insetOf() + offsetOf());
      render(false);
    },
    // Is this list the one currently in the host?
    get active() { return attached(); },
    destroy() {
      if (opts.autoScroll) scroller.removeEventListener('scroll', onScroll);
      if (ro) ro.disconnect();
    },
  };
}
