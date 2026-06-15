// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * queue.js — Queue panel module.
 * Exports: Queue singleton with toggle, refresh, open, close.
 */
import { Player } from './player.js';
import { artPlaceholderEmoji } from './utils.js';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const panel    = document.getElementById('queue-panel');
const listEl   = document.getElementById('queue-list');
const countEl  = document.getElementById('queue-count');
const dropZone = document.getElementById('queue-drop-zone');

document.getElementById('btn-queue-close').addEventListener('click', () => close());

// ── Clear queue with confirmation + undo ──────────────────────────────────────
//
// Clicking ``Clear`` while the queue has ≥5 tracks pops a styled confirm
// modal — small queues clear silently because the user can rebuild them
// trivially.  After a successful clear we show a 5-second undo toast that
// restores the previous queue and play position from a swap variable.
const CLEAR_CONFIRM_MIN  = 5;
const UNDO_WINDOW_MS     = 5000;
let _undoQueue   = null;          // snapshot of Player.queue from the last clear
let _undoIdx     = 0;
let _undoTimer   = null;
let _undoToast   = null;

function _renderUndoToast(count) {
  // Lightweight inline toast — we avoid utils' Toast for this because it
  // doesn't expose an action button.  Positioning matches Toast (bottom-
  // right) so it co-locates with the rest of the notification stack.
  const el = document.createElement('div');
  el.className = 'queue-undo-toast';
  el.style.cssText = (
    'position:fixed;right:18px;bottom:18px;z-index:100001;'
    + 'background:var(--bg3,#222);color:var(--text1,#eee);'
    + 'border:1px solid var(--border-bright,#444);border-radius:8px;'
    + 'padding:10px 14px;display:flex;align-items:center;gap:12px;'
    + 'font-size:13px;box-shadow:var(--glass-shadow,0 4px 12px rgba(0,0,0,.4));'
  );
  el.innerHTML = `
    <span>Cleared ${count} track${count === 1 ? '' : 's'}.</span>
    <button type="button" class="queue-undo-btn"
            style="background:transparent;border:1px solid currentColor;color:inherit;
                   padding:3px 10px;border-radius:5px;cursor:pointer;font:inherit">
      Undo
    </button>`;
  el.querySelector('.queue-undo-btn').addEventListener('click', () => {
    _applyUndo();
  });
  document.body.appendChild(el);
  return el;
}

function _dismissUndoToast() {
  if (_undoTimer) { clearTimeout(_undoTimer); _undoTimer = null; }
  if (_undoToast) { _undoToast.remove(); _undoToast = null; }
}

function _applyUndo() {
  if (!_undoQueue || !_undoQueue.length) { _dismissUndoToast(); return; }
  Player.setQueue(_undoQueue, _undoIdx);
  _undoQueue = null;
  _dismissUndoToast();
  refresh();
}

function _showClearConfirm(count) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'pl-modal-backdrop';
    const dialog = document.createElement('div');
    dialog.className = 'pl-modal-dialog';
    dialog.innerHTML = `
      <div class="pl-modal-title">Clear queue</div>
      <div class="pl-modal-body" style="padding:6px 0 14px;color:var(--text2,#bbb);font-size:13px">
        Clear ${count} tracks from the queue?
      </div>
      <div class="pl-modal-actions">
        <button class="pl-modal-btn pl-modal-cancel">Cancel</button>
        <button class="pl-modal-btn pl-modal-ok">Clear</button>
      </div>
    `;
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    const btnOk  = dialog.querySelector('.pl-modal-ok');
    const btnCan = dialog.querySelector('.pl-modal-cancel');
    function finish(value) {
      backdrop.classList.add('pl-modal-out');
      setTimeout(() => backdrop.remove(), 150);
      resolve(value);
    }
    btnOk.focus();
    btnOk.addEventListener('click',  () => finish(true));
    btnCan.addEventListener('click', () => finish(false));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish(false); });
    dialog.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') finish(false);
      if (e.key === 'Enter')  finish(true);
    });
  });
}

document.getElementById('btn-queue-clear').addEventListener('click', async () => {
  const q   = Player.queue;
  const idx = Player.queueIdx;
  if (!q || !q.length) return;

  if (q.length >= CLEAR_CONFIRM_MIN) {
    const ok = await _showClearConfirm(q.length);
    if (!ok) return;
  }

  // Snapshot for undo BEFORE we clear.
  _undoQueue = q.slice();
  _undoIdx   = Math.max(0, Math.min(idx, _undoQueue.length - 1));

  Player.setQueue([], 0);

  _dismissUndoToast();
  _undoToast = _renderUndoToast(_undoQueue.length);
  _undoTimer = setTimeout(() => {
    _undoQueue = null;
    _dismissUndoToast();
  }, UNDO_WINDOW_MS);
});

// ── Drag state ────────────────────────────────────────────────────────────────
let _dragFromIdx = null;   // index of queue row being dragged within the queue

// ── Format helpers ────────────────────────────────────────────────────────────
function fmtDur(sec) {
  if (!sec) return '';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Render ────────────────────────────────────────────────────────────────────
// Spot-updates: remove/reorder initiated from inside this panel mutate the
// existing DOM (drop a node / move a node + renumber) instead of rebuilding
// every row.  The full rebuild re-creates all rows, re-attaches ~10 listeners
// each and re-fires every cover-art <img> — visible as a flicker and a
// scroll-position wobble on big queues.  ``_suppressRefresh`` swallows the
// ``queuechange``-driven refresh for those self-initiated mutations; every
// other queue change (adds from the library, clear, undo) still rebuilds.
let _suppressRefresh = false;

// Re-sync row indices + playing highlight after a spot-update.  Handlers
// read ``row.dataset.idx`` at event time, so this sweep is all that's
// needed to keep them correct.
function _renumber() {
  const q   = Player.queue;
  const idx = Player.queueIdx;
  countEl.textContent = q.length ? `(${q.length})` : '';
  listEl.querySelectorAll('.queue-row').forEach((row, i) => {
    row.dataset.idx = i;
    row.classList.toggle('playing', i === idx);
    const icon = row.querySelector('.queue-playing-icon');
    if (icon) icon.innerHTML = i === idx ? '&#9654;' : '';
  });
}

function refresh() {
  if (_suppressRefresh) return;   // self-initiated spot-update already applied
  const q   = Player.queue;
  const idx = Player.queueIdx;

  // Update count badge
  countEl.textContent = q.length ? `(${q.length})` : '';

  listEl.innerHTML = '';

  if (!q.length) {
    const empty = document.createElement('div');
    empty.className = 'queue-empty';
    empty.textContent = 'No tracks queued.';
    listEl.appendChild(empty);
    return;
  }

  q.forEach((track, i) => {
    const row = document.createElement('div');
    row.className = 'queue-row' + (i === idx ? ' playing' : '');
    row.dataset.idx = i;
    // Drag is gated to the handle only — see _enableHandleDrag below.  This
    // keeps scroll/select gestures on the row body from accidentally
    // starting a drag, especially on touch screens.
    row.draggable = false;

    // Render the format-emoji placeholder underneath an absolutely-
    // positioned <img>; ``track.cover_art`` is only populated when art
    // was extracted at scan time, but ``/api/art/{id}`` extracts on
    // demand for tracks where it wasn't.  Asking for the endpoint
    // unconditionally — and dropping the <img> when it 404s — keeps the
    // queue/playlist row visually consistent with the bottom-left
    // player, which uses the same endpoint.
    // ``fallback=404`` so an art-less track 404s (→ <img> onerror removes it)
    // and the format-emoji placeholder shows — without it the endpoint returns
    // the generic ♪ placeholder JPEG, which paints over the emoji.
    const artSrc = track.id ? `/api/art/${track.id}?size=sm&fallback=404` : '';
    const artHtml = `<div class="queue-row-art">
      <span class="qr-art-ph">${artPlaceholderEmoji(track)}</span>
      ${artSrc ? `<img class="qr-art-img" src="${esc(artSrc)}" loading="lazy" decoding="async" alt="">` : ''}
    </div>`;

    row.innerHTML = `
      <span class="queue-drag-handle" draggable="true" title="Drag to reorder">&#10783;</span>
      <span class="queue-playing-icon">${i === idx ? '&#9654;' : ''}</span>
      ${artHtml}
      <div class="queue-track-info">
        <span class="queue-track-title" title="${esc(track.title)}">${esc(track.title || '—')}</span>
        <span class="queue-track-artist">${esc(track.artist || track.album_artist || '')}</span>
      </div>
      <span class="queue-track-dur">${fmtDur(track.duration)}</span>
      <button class="queue-remove-btn" title="Remove from queue" data-idx="${i}">&times;</button>
    `;

    // Cover thumbnail load/error handlers — fade-in on decode, remove
    // the <img> on 404 so the format emoji stays the visible default.
    const _qArtImg = row.querySelector('.qr-art-img');
    if (_qArtImg) {
      _qArtImg.onload  = () => _qArtImg.classList.add('loaded');
      _qArtImg.onerror = () => _qArtImg.remove();
    }

    // Click to play this row (but not if user clicked the remove button).
    // Index is read from the row at event time — spot-updates renumber
    // ``dataset.idx`` without re-binding handlers.
    row.addEventListener('click', (e) => {
      if (e.target.closest('.queue-remove-btn')) return;
      Player.setQueue(Player.queue, +row.dataset.idx);
    });

    // Remove button — spot-update: drop this row and renumber the rest.
    // Removing the playing row (or the last row) changes playback state,
    // so those fall through to the normal full refresh.
    row.querySelector('.queue-remove-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      const ri = +row.dataset.idx;
      if (ri === Player.queueIdx || Player.queue.length <= 1) {
        Player.removeFromQueue(ri);   // queuechange listener does the rebuild
        return;
      }
      _suppressRefresh = true;
      Player.removeFromQueue(ri);     // emits queuechange synchronously
      _suppressRefresh = false;
      row.remove();
      _renumber();
    });

    // ── Drag-to-reorder within queue ──────────────────────────────────────
    // The handle is the only draggable child by default — the row itself
    // becomes draggable transiently while a touch long-press is active.
    // Both paths funnel through the row's dragstart handler so the data
    // transfer payload is set in one place.
    const handle = row.querySelector('.queue-drag-handle');

    row.addEventListener('dragstart', (e) => {
      _dragFromIdx = +row.dataset.idx;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('application/x-soniqboom-queue-idx', String(_dragFromIdx));
      row.classList.add('dragging');
    });

    row.addEventListener('dragend', () => {
      _dragFromIdx = null;
      row.classList.remove('dragging');
      // After a drag completes, drop the transient draggable state set by
      // the touch long-press so the next click/tap isn't interpreted as a
      // drag-handle event.
      row.draggable = false;
      listEl.querySelectorAll('.queue-row.dragging-over').forEach(r => r.classList.remove('dragging-over'));
    });

    // ── Touch long-press to begin drag ────────────────────────────────────
    // Native HTML5 drag-and-drop is mouse-only; on touch screens we
    // synthesise a long-press gesture that enables `draggable` on the row
    // long enough for the user's finger to drag.  Movement before the
    // 350 ms hold cancels.  Tiny vibrate gives haptic feedback on lift.
    let _lpTimer = null;
    let _lpStartX = 0;
    let _lpStartY = 0;
    handle.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      _lpStartX = e.touches[0].clientX;
      _lpStartY = e.touches[0].clientY;
      _lpTimer = setTimeout(() => {
        // Promote to draggable.  The browser then fires dragstart on the
        // next touchmove that crosses the drag threshold.
        row.draggable = true;
        try { navigator.vibrate?.(8); } catch (_) {}
      }, 350);
    }, { passive: true });
    handle.addEventListener('touchmove', (e) => {
      if (!_lpTimer) return;
      const t = e.touches[0];
      if (!t) return;
      // Cancel long-press if the finger moves too far before the timer fires.
      if (Math.hypot(t.clientX - _lpStartX, t.clientY - _lpStartY) > 8) {
        clearTimeout(_lpTimer);
        _lpTimer = null;
      }
    }, { passive: true });
    handle.addEventListener('touchend', () => {
      if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
      // Reset draggable after the (potential) drag ends so the next plain
      // tap on the handle doesn't accidentally inherit drag mode.
      setTimeout(() => { row.draggable = false; }, 0);
    });
    handle.addEventListener('touchcancel', () => {
      if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
      row.draggable = false;
    });

    row.addEventListener('dragover', (e) => {
      // Only handle internal queue reorder drags here (not library drops)
      if (e.dataTransfer.types.includes('application/x-soniqboom-queue-idx')) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        listEl.querySelectorAll('.queue-row.dragging-over').forEach(r => r.classList.remove('dragging-over'));
        row.classList.add('dragging-over');
      }
    });

    row.addEventListener('dragleave', (e) => {
      if (!row.contains(e.relatedTarget)) {
        row.classList.remove('dragging-over');
      }
    });

    row.addEventListener('drop', (e) => {
      e.preventDefault();
      row.classList.remove('dragging-over');
      const fromIdx = parseInt(e.dataTransfer.getData('application/x-soniqboom-queue-idx'), 10);
      const toIdx   = +row.dataset.idx;
      if (isNaN(fromIdx) || fromIdx === toIdx) return;
      // Spot-update: move the dragged node into place and renumber instead
      // of rebuilding every row (keeps scroll position, no art re-fetch).
      _suppressRefresh = true;
      Player.moveInQueue(fromIdx, toIdx);   // emits queuechange synchronously
      _suppressRefresh = false;
      const rows = listEl.querySelectorAll('.queue-row');
      const dragged = rows[fromIdx];
      const target  = rows[toIdx];
      if (dragged && target) {
        if (fromIdx < toIdx) target.after(dragged);
        else                 target.before(dragged);
        _renumber();
      } else {
        refresh();   // DOM out of sync with the queue — rebuild
      }
    });

    listEl.appendChild(row);
  });
}

// ── Drop zone — receives library track drops ──────────────────────────────────
dropZone.addEventListener('dragover', (e) => {
  if (e.dataTransfer.types.includes('application/x-soniqboom-track')) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dropZone.classList.add('drag-active');
  }
});

dropZone.addEventListener('dragleave', (e) => {
  if (!dropZone.contains(e.relatedTarget)) {
    dropZone.classList.remove('drag-active');
  }
});

dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    tracks.forEach(t => { if (t?.id) Player.addToQueue(t); });
    if (tracks.length) refresh();
  } catch (_) {}
});

// Also allow dropping library tracks directly onto the queue list area
listEl.addEventListener('dragover', (e) => {
  if (e.dataTransfer.types.includes('application/x-soniqboom-track') &&
      !e.dataTransfer.types.includes('application/x-soniqboom-queue-idx')) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dropZone.classList.add('drag-active');
  }
});

listEl.addEventListener('dragleave', (e) => {
  if (!listEl.contains(e.relatedTarget) && !dropZone.contains(e.relatedTarget)) {
    dropZone.classList.remove('drag-active');
  }
});

listEl.addEventListener('drop', (e) => {
  if (!e.dataTransfer.types.includes('application/x-soniqboom-track')) return;
  if (e.dataTransfer.types.includes('application/x-soniqboom-queue-idx')) return;
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    tracks.forEach(t => { if (t?.id) Player.addToQueue(t); });
    if (tracks.length) refresh();
  } catch (_) {}
});

// ── Panel visibility ──────────────────────────────────────────────────────────
function open() {
  document.dispatchEvent(new CustomEvent('panelopen', { detail: { panel: 'queue' } }));
  panel.classList.remove('hidden');
  refresh();
}

function close() {
  panel.classList.add('hidden');
}

function toggle() {
  if (panel.classList.contains('hidden')) {
    open();
  } else {
    close();
  }
}

// Close when another panel opens
document.addEventListener('panelopen', (e) => {
  if (e.detail?.panel !== 'queue') close();
});

export const Queue = { toggle, refresh, open, close };
