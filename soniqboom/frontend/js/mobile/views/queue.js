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
    <div id="m-queue-offer" style="display:none;align-items:center;gap:8px;padding:8px 16px;border-bottom:1px solid var(--border);font-size:14px">
      <span id="m-queue-offer-text" style="flex:1;min-width:0;overflow-wrap:anywhere;color:var(--text-dim)"></span>
      <button id="m-queue-offer-btn" type="button" style="font-size:14px;color:var(--accent);min-height:44px;padding:0 8px">Resume</button>
    </div>
    <ul class="m-list" id="m-queue-list"></ul>
    <div class="m-empty hidden" id="m-queue-empty">Queue is empty.<br><br>Tap a track in the Library or Search to start playing.</div>
  `;

  const listEl   = root.querySelector('#m-queue-list');
  const countEl  = root.querySelector('#m-queue-count');
  const emptyEl  = root.querySelector('#m-queue-empty');
  const clearBtn = root.querySelector('#m-queue-clear');
  const offerEl  = root.querySelector('#m-queue-offer');
  const offerTxt = root.querySelector('#m-queue-offer-text');
  const offerBtn = root.querySelector('#m-queue-offer-btn');

  // A queue saved on another device, on offer until something plays (the
  // "Resume the queue from …?" toast times out; this row stays).
  const paintOffer = () => {
    const who = Player.queueOffer || null;
    if (who) {
      offerTxt.textContent = `Queue saved on ${who}`;
      offerBtn.setAttribute('aria-label', `Resume the queue from ${who}`);
    }
    offerEl.style.display = who ? 'flex' : 'none';
  };
  offerBtn.addEventListener('click', () => { Player.resumeQueueOffer?.(); });
  Player.on('queueoffer', paintOffer);
  paintOffer();

  clearBtn.addEventListener('click', () => {
    const n = Player.queue.length;
    if (!n) return;
    // One mis-tap must not wipe a long queue: small ones clear at once, the rest ask.
    if (n < 5) { Player.setQueue([], 0); return; }
    ctx.showSheet({ title: `Clear ${n.toLocaleString()} queued tracks?`, actions: [
      { label: 'Clear queue', danger: true, onSelect: () => Player.setQueue([], 0) },
    ] });
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

    // A view-backed queue refills a page at a time — right after shuffle goes on
    // there is nothing behind the current track for a moment.  Say so (as the
    // desktop panel does), or the toggle looks like it wiped the queue.
    const src = Player.queueSource;
    if (src && src.hasMore && Player.queueIdx >= Player.queue.length - 1) {
      const more = document.createElement('div');
      more.className = 'm-row-artist';
      more.style.cssText = 'padding:14px 16px;text-align:center';
      more.textContent = src.mode === 'shuffled' ? 'Shuffling the rest of the list\u2026' : 'Loading the next tracks\u2026';
      listEl.appendChild(more);
    }

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

  // Rebuilding every row (gestures + drag controller) on each track change while
  // another tab is showing is wasted main-thread time; 'viewactive' renders fresh.
  const renderIfShown = () => { if (root.classList.contains('active')) render(); };
  Player.on('queuechange',  renderIfShown);
  Player.on('trackchange',  renderIfShown);    // highlights the now-playing row
  root.addEventListener('viewactive', render);

  render();
}
