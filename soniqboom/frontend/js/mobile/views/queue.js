// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * queue.js — Mobile queue view: drag handle reorder, swipe to remove,
 * tap to play.  Subscribes to Player queuechange to stay in sync.  The header
 * is a tape counter: the time the queue still plays (the playing track's rest
 * and every track after it; "+" when a length is unknown or the queue's list
 * goes on).
 */
import { Player } from '../../player.js';
import { attachRowGestures, attachDragReorder } from '../gestures.js';
import { buildTrackRow, fmtDur } from './_common.js';

export function mountQueue(root, ctx) {
  let _gestureCleanups = [];
  let _dragCleanup     = null;

  root.innerHTML = `
    <div class="m-queue-head">
      <div class="m-queue-counter" id="m-queue-counter">
        <span class="m-queue-counter-label" aria-hidden="true">Time left</span>
        <span class="m-tape-counter" id="m-queue-left-digits" aria-hidden="true"></span>
        <span class="sr-only" id="m-queue-left"></span>
      </div>
      <span class="m-queue-count" id="m-queue-count"></span>
      <button id="m-queue-clear" class="m-key m-queue-clear" type="button">Clear</button>
    </div>
    <div id="m-queue-offer" class="m-queue-offer" style="display:none">
      <span id="m-queue-offer-text" class="m-queue-offer-text"></span>
      <button id="m-queue-offer-btn" class="m-key m-key-lit" type="button">Resume</button>
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
  const ctrDigits = root.querySelector('#m-queue-left-digits');
  const ctrSr    = root.querySelector('#m-queue-left');

  // ── Tape counter ────────────────────────────────────────────────────────
  // ``_after``: seconds of the tracks after the playing one (summed per render),
  // ``_unknown``: one of them (or the playing one) has no length, or the queue's
  // list goes on; the playing track's rest comes from timeupdate.
  let _after = 0, _unknown = false, _curDur = 0, _curPos = 0, _shown = '', _curItem = null;
  function sumAhead() {
    const q = Player.queue, idx = Player.queueIdx;
    let sec = 0, unknown = false;
    for (let i = Math.max(0, idx + 1); i < q.length; i++) {
      const d = Number(q[i] && q[i].duration) || 0;
      if (d > 0) sec += d; else unknown = true;
    }
    const cur = q[idx];
    if (cur !== _curItem) { _curItem = cur; _curPos = 0; }   // (another track: its own position)
    _curDur = Number(cur && cur.duration) || 0;
    if (cur && !(_curDur > 0)) unknown = true;
    const src = Player.queueSource;
    _after = sec;
    _unknown = unknown || !!(src && src.hasMore);
    paintCounter();
  }
  function paintCounter() {
    // (an empty queue: the counter at zero)
    const empty = !Player.queue.length || Player.queueIdx < 0;
    const left = empty ? 0 : Math.max(0, Math.round(_after + Math.max(0, _curDur - _curPos)));
    const unknown = !empty && _unknown;
    const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), sec = left % 60;
    const txt = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
      + (unknown ? '+' : '');
    if (txt === _shown) return;               // (timeupdate fires several times a second)
    _shown = txt;
    ctrDigits.replaceChildren(...[...txt].map((ch) => {
      const d = document.createElement('i');
      d.textContent = ch;
      if (ch === ':' || ch === '+') d.className = 'sep';
      return d;
    }));
    ctrSr.textContent = `Time left ${h ? `${h} h ` : ''}${m} min ${sec} s${unknown ? ' or more' : ''}`;
  }
  Player.on('timeupdate', ({ current, duration }) => {
    _curPos = Number(current) || 0;
    if (Number(duration) > 0) _curDur = Number(duration);
    if (root.classList.contains('active')) paintCounter();
  });

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
    sumAhead();

    if (!q.length) {
      emptyEl.classList.remove('hidden');
      return;
    }
    emptyEl.classList.add('hidden');

    q.forEach((t, i) => {
      const dur = document.createElement('span');
      dur.className = 'm-row-artist m-row-trail';
      dur.textContent = fmtDur(t.duration);

      const row = buildTrackRow(t, { trailing: dur, showHandle: true });
      if (i === idx && !Player.startFailed) row.classList.add('playing');

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
      more.className = 'm-row-artist m-queue-more';
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
  // A start that failed (nothing plays): its row is not lit as playing — and
  // lit again once it plays after all.  Only the marker moves.
  Player.on('statechange', () => {
    if (!root.classList.contains('active')) return;
    const idx = Player.queueIdx;
    [...listEl.querySelectorAll('.m-row')].forEach((row, i) => {
      row.classList.toggle('playing', i === idx && !Player.startFailed);
    });
  });
  root.addEventListener('viewactive', render);

  render();
}
