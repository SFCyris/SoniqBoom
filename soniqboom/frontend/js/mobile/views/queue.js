// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * queue.js — Mobile queue view: drag handle reorder, swipe to remove,
 * tap to play.  Subscribes to Player queuechange to stay in sync.
 */
import { Player } from '../../player.js';
import { attachRowGestures, attachDragReorder } from '../gestures.js';
import { buildTrackRow, fmtDur } from './_common.js';

export function mountQueue(root, ctx) {
  let _gestureCleanups = [];
  let _dragCleanup     = null;

  root.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface)">
      <span id="m-queue-count" style="font-size:14px;color:var(--text-dim)"></span>
      <button id="m-queue-clear" style="font-size:14px;color:var(--accent);min-height:44px;padding:0 8px">Clear</button>
    </div>
    <ul class="m-list" id="m-queue-list"></ul>
    <div class="m-empty hidden" id="m-queue-empty">Queue is empty.<br><br>Tap a track in the Library or Search to start playing.</div>
  `;

  const listEl   = root.querySelector('#m-queue-list');
  const countEl  = root.querySelector('#m-queue-count');
  const emptyEl  = root.querySelector('#m-queue-empty');
  const clearBtn = root.querySelector('#m-queue-clear');

  clearBtn.addEventListener('click', () => {
    if (Player.queue.length) Player.setQueue([], 0);
  });

  function render() {
    cleanup();
    listEl.innerHTML = '';

    const q   = Player.queue;
    const idx = Player.queueIdx;
    countEl.textContent = q.length ? `${q.length} track${q.length === 1 ? '' : 's'}` : '';

    if (!q.length) {
      emptyEl.classList.remove('hidden');
      return;
    }
    emptyEl.classList.add('hidden');

    q.forEach((t, i) => {
      const dur = document.createElement('span');
      dur.className = 'm-row-artist';
      dur.style.flexShrink = '0';
      dur.style.fontSize = '12px';
      dur.style.marginRight = '4px';
      dur.textContent = fmtDur(t.duration);

      const row = buildTrackRow(t, { trailing: dur, showHandle: true });
      if (i === idx) row.classList.add('playing');

      const c = attachRowGestures(row, {
        onTap: () => Player.setQueue(Player.queue, i),
        onSwipeAction: () => {
          Player.removeFromQueue(i);
          // queuechange listener re-renders
        },
        swipeLabel: 'Remove',
      });
      _gestureCleanups.push(c);
      listEl.appendChild(row);
    });

    // Drag handle reorder — wired once per render, scoped to this list
    _dragCleanup = attachDragReorder(listEl, {
      onReorder: (from, to) => {
        Player.moveInQueue(from, to);
      },
    });
  }

  function cleanup() {
    _gestureCleanups.forEach(fn => fn());
    _gestureCleanups = [];
    if (_dragCleanup) { _dragCleanup(); _dragCleanup = null; }
  }

  Player.on('queuechange',  render);
  Player.on('trackchange',  render);    // highlights the now-playing row
  root.addEventListener('viewactive', render);

  render();
}
