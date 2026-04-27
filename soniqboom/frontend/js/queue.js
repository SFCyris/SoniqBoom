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
document.getElementById('btn-queue-clear').addEventListener('click', () => {
  // Clear by replacing queue with an empty array; play nothing new
  Player.setQueue([], 0);
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
function refresh() {
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
    row.draggable = true;

    const artHtml = track.cover_art
      ? `<div class="queue-row-art"><img src="${esc(track.cover_art)}" loading="lazy" alt=""></div>`
      : `<div class="queue-row-art"><span class="qr-art-ph">${artPlaceholderEmoji(track)}</span></div>`;

    row.innerHTML = `
      <span class="queue-drag-handle" title="Drag to reorder">&#10783;</span>
      <span class="queue-playing-icon">${i === idx ? '&#9654;' : ''}</span>
      ${artHtml}
      <div class="queue-track-info">
        <span class="queue-track-title" title="${esc(track.title)}">${esc(track.title || '—')}</span>
        <span class="queue-track-artist">${esc(track.artist || track.album_artist || '')}</span>
      </div>
      <span class="queue-track-dur">${fmtDur(track.duration)}</span>
      <button class="queue-remove-btn" title="Remove from queue" data-idx="${i}">&times;</button>
    `;

    // Click to play this row (but not if user clicked the remove button)
    row.addEventListener('click', (e) => {
      if (e.target.closest('.queue-remove-btn')) return;
      Player.setQueue(Player.queue, i);
    });

    // Remove button
    row.querySelector('.queue-remove-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      Player.removeFromQueue(i);
      refresh();
    });

    // ── Drag-to-reorder within queue ──────────────────────────────────────
    row.addEventListener('dragstart', (e) => {
      _dragFromIdx = i;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('application/x-soniqboom-queue-idx', String(i));
      row.classList.add('dragging');
    });

    row.addEventListener('dragend', () => {
      _dragFromIdx = null;
      row.classList.remove('dragging');
      // Clean up all drag-over indicators
      listEl.querySelectorAll('.queue-row.dragging-over').forEach(r => r.classList.remove('dragging-over'));
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
      if (isNaN(fromIdx) || fromIdx === i) return;
      Player.moveInQueue(fromIdx, i);
      refresh();
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
