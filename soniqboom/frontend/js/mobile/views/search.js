// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * search.js — Mobile search view.  Sticky search field, debounced query.
 */
import { Player } from '../../player.js';
import { attachRowGestures } from '../gestures.js';
import { buildTrackRow, fmtDur, trackActions } from './_common.js';

const DEBOUNCE_MS = 250;

export function mountSearch(root, ctx) {
  let _gestureCleanups = [];
  let _timer = null;
  let _seq   = 0;
  let _last  = [];

  const gctx = { player: Player, toast: ctx.toast, showSheet: ctx.showSheet };

  root.innerHTML = `
    <div class="m-search-bar">
      <input class="m-search-input" id="m-search-input" type="search"
             placeholder="Search artist, album, title…"
             autocomplete="off" autocapitalize="off" autocorrect="off">
    </div>
    <ul class="m-list" id="m-search-list"></ul>
    <div class="m-empty hidden" id="m-search-empty"></div>
    <div class="m-loading hidden" id="m-search-loading">Searching…</div>
  `;

  const input  = root.querySelector('#m-search-input');
  const listEl = root.querySelector('#m-search-list');
  const empty  = root.querySelector('#m-search-empty');
  const loadEl = root.querySelector('#m-search-loading');

  input.addEventListener('input', () => {
    clearTimeout(_timer);
    _timer = setTimeout(runQuery, DEBOUNCE_MS);
  });

  // Re-focus the input when the view becomes active again
  root.addEventListener('viewactive', () => {
    // Don't auto-focus on mobile (would pop the keyboard) — just leave the input ready
  });

  async function runQuery() {
    const q = input.value.trim();
    cleanup();
    listEl.innerHTML = '';
    empty.classList.add('hidden');

    if (!q) return;

    loadEl.classList.remove('hidden');
    const mySeq = ++_seq;

    try {
      const url = `/api/search?q=${encodeURIComponent(q)}&limit=100`;
      const res = await fetch(url);
      const tracks = await res.json();
      if (mySeq !== _seq) return;          // superseded by a later query

      _last = Array.isArray(tracks) ? tracks : [];
      if (!_last.length) {
        empty.textContent = `No matches for "${q}".`;
        empty.classList.remove('hidden');
        return;
      }

      _last.forEach((t, idx) => {
        const dur = document.createElement('span');
        dur.className = 'm-row-artist';
        dur.style.flexShrink = '0';
        dur.style.fontSize = '12px';
        dur.style.marginRight = '4px';
        dur.textContent = fmtDur(t.duration);

        const row = buildTrackRow(t, { trailing: dur });
        const c = attachRowGestures(row, {
          onTap:         () => Player.setQueue(_last, idx),
          onLongPress:   () => ctx.showSheet({ title: t.title || 'Track', actions: trackActions(t, gctx) }),
          onSwipeAction: () => { Player.addToQueue(t); ctx.toast('Added to queue'); },
          swipeLabel:    '+ Queue',
          swipeBgClass:  'queue',
        });
        _gestureCleanups.push(c);
        listEl.appendChild(row);
      });
    } catch (err) {
      if (mySeq !== _seq) return;
      console.error('Search failed', err);
      empty.textContent = 'Search failed.';
      empty.classList.remove('hidden');
    } finally {
      if (mySeq === _seq) loadEl.classList.add('hidden');
    }
  }

  function cleanup() {
    _gestureCleanups.forEach(fn => fn());
    _gestureCleanups = [];
  }
}
