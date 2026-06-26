// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * library.js — Mobile Library view.
 *
 * Group chips: All / Artists / Album Artists / Albums / Genres / Years.
 * - All:                flat track list (paginated lazily on scroll).
 * - Artists/Albums/etc: group list → tap → filtered track list.
 */
import { Player } from '../../player.js';
import { attachRowGestures } from '../gestures.js';
import { buildTrackRow, fmtDur, esc, trackActions } from './_common.js';
import { probeAdlibDurations } from '../../utils.js';

const PAGE_SIZE = 100;

export function mountLibrary(root, ctx) {
  // ── State ────────────────────────────────────────────────────────────
  let group   = 'all';        // 'all' | 'artists' | 'album_artists' | 'albums' | 'genres' | 'years'
  let crumb   = null;         // when set, we're inside a group → showing tracks
  let tracks  = [];
  let groupItems = [];
  let offset  = 0;
  let exhausted = false;
  let loading   = false;
  let _gestureCleanups = [];

  const gctx = { player: Player, toast: ctx.toast, showSheet: ctx.showSheet };

  // ── DOM scaffold ─────────────────────────────────────────────────────
  root.innerHTML = `
    <div class="m-group-bar" id="lib-groups">
      <button class="m-group-chip active" data-g="all">All</button>
      <button class="m-group-chip"        data-g="artists">Artists</button>
      <button class="m-group-chip"        data-g="album_artists">Album Artists</button>
      <button class="m-group-chip"        data-g="albums">Albums</button>
      <button class="m-group-chip"        data-g="genres">Genres</button>
      <button class="m-group-chip"        data-g="years">Years</button>
    </div>
    <div class="m-crumb-bar hidden" id="lib-crumb">
      <button class="m-crumb-back" id="lib-back" aria-label="Back">←</button>
      <span class="m-crumb-text" id="lib-crumb-text"></span>
    </div>
    <ul class="m-list" id="lib-list"></ul>
    <div class="m-empty hidden" id="lib-empty">No tracks yet — add a folder in Settings on desktop.</div>
    <div class="m-loading hidden" id="lib-loading">Loading…</div>
  `;

  const groupBar  = root.querySelector('#lib-groups');
  const crumbBar  = root.querySelector('#lib-crumb');
  const crumbText = root.querySelector('#lib-crumb-text');
  const backBtn   = root.querySelector('#lib-back');
  const listEl    = root.querySelector('#lib-list');
  const emptyEl   = root.querySelector('#lib-empty');
  const loadEl    = root.querySelector('#lib-loading');

  // ── Group chip switching ─────────────────────────────────────────────
  groupBar.addEventListener('click', (e) => {
    const chip = e.target.closest('.m-group-chip');
    if (!chip) return;
    group = chip.dataset.g;
    crumb = null;
    [...groupBar.children].forEach(c => c.classList.toggle('active', c === chip));
    render();
  });

  backBtn.addEventListener('click', () => {
    crumb = null;
    render();
  });

  // ── Render dispatcher ────────────────────────────────────────────────
  function render() {
    cleanupGestures();
    listEl.innerHTML = '';
    tracks = [];
    groupItems = [];
    offset = 0;
    exhausted = false;

    if (group === 'all' || crumb) {
      crumbBar.classList.toggle('hidden', !crumb);
      if (crumb) crumbText.textContent = crumb.label;
      loadTrackPage();
    } else {
      crumbBar.classList.add('hidden');
      loadGroupList();
    }
  }

  // ── Track list (flat or filtered) ─────────────────────────────────────
  async function loadTrackPage() {
    if (loading || exhausted) return;
    loading = true;
    if (offset === 0) loadEl.classList.remove('hidden');

    let url;
    if (crumb) {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
      params.set(crumb.field, crumb.value);
      if (crumb.extraField) params.set(crumb.extraField, crumb.extraValue);
      url = `/api/search/filter?${params}`;
    } else {
      url = `/api/tracks?limit=${PAGE_SIZE}&offset=${offset}`;
    }

    try {
      const res = await fetch(url);
      const page = await res.json();
      if (!Array.isArray(page) || page.length === 0) {
        exhausted = true;
        if (offset === 0) emptyEl.classList.remove('hidden');
      } else {
        emptyEl.classList.add('hidden');
        appendTracks(page);
        offset += page.length;
        if (page.length < PAGE_SIZE) exhausted = true;
      }
    } catch (err) {
      console.error('Library load failed', err);
    } finally {
      loading = false;
      loadEl.classList.add('hidden');
    }
  }

  function appendTracks(page) {
    const durEls = new Map();          // track id -> its duration <span>, for backfill
    page.forEach((t, i) => {
      const idx = tracks.length + i;
      const dur = document.createElement('span');
      dur.className = 'm-row-artist';
      dur.style.flexShrink = '0';
      dur.style.fontSize = '12px';
      dur.style.marginRight = '4px';
      dur.textContent = fmtDur(t.duration);
      if (t && t.id) durEls.set(t.id, dur);

      const row = buildTrackRow(t, { trailing: dur });
      const cleanup = attachRowGestures(row, {
        onTap: () => {
          // Start a queue from this track to the end of currently visible tracks
          Player.setQueue(tracks, idx);
        },
        onLongPress: () => {
          ctx.showSheet({ title: t.title || 'Track', actions: trackActions(t, gctx) });
        },
        onSwipeAction: () => {
          Player.addToQueue(t);
          ctx.toast('Added to queue');
        },
        swipeLabel: '+ Queue',
        swipeBgClass: 'queue',
      });
      _gestureCleanups.push(cleanup);
      listEl.appendChild(row);
    });
    tracks.push(...page);
    // Background-fill real AdLib/IMF lengths for this page's placeholder rows.
    probeAdlibDurations(page).then(map => {
      for (const id in map) {
        const sec = map[id];
        if (!(sec > 0)) continue;
        const el = durEls.get(id);
        if (el) el.textContent = fmtDur(sec);
        const t = page.find(x => x && x.id === id);
        if (t) t.duration = sec;
      }
    });
  }

  // Infinite scroll
  root.addEventListener('scroll', () => {
    if (exhausted || loading) return;
    if (root.scrollTop + root.clientHeight >= root.scrollHeight - 200) {
      loadTrackPage();
    }
  });

  // ── Group list (Artists / Albums / Genres / Years) ───────────────────
  async function loadGroupList() {
    loadEl.classList.remove('hidden');
    const endpointMap = {
      artists:       '/api/library/artists',
      album_artists: '/api/library/album-artists',
      albums:        '/api/library/albums',
      genres:        '/api/library/genres',
      years:         '/api/library/years',
    };
    const fieldMap = {
      artists:       'artist',
      album_artists: 'album_artist',
      albums:        'album',
      genres:        'genre',
      years:         'year_min',
    };
    try {
      const res = await fetch(endpointMap[group]);
      const items = await res.json();
      groupItems = Array.isArray(items) ? items : [];
      renderGroupItems(fieldMap[group]);
    } catch (err) {
      console.error('Group load failed', err);
    } finally {
      loadEl.classList.add('hidden');
    }
  }

  function renderGroupItems(field) {
    if (!groupItems.length) {
      emptyEl.classList.remove('hidden');
      emptyEl.textContent = 'Nothing here yet.';
      return;
    }
    emptyEl.classList.add('hidden');

    groupItems.forEach(item => {
      // Each aggregation uses a slightly different schema
      const value = item[field === 'year_min' ? 'year' : field] ?? item.label ?? '';
      const display = item.label || String(value || '[Untagged]');
      const count   = item.count ? `${item.count}` : '';

      const row = document.createElement('div');
      row.className = 'm-row';
      row.innerHTML = `
        <div class="m-row-content">
          <div class="m-row-art"><span>${esc(emojiFor(group))}</span></div>
          <div class="m-row-meta">
            <div class="m-row-title">${esc(display)}</div>
            <div class="m-row-artist">${esc(count + (count ? ' tracks' : ''))}</div>
          </div>
          <span style="color:var(--text-dim);font-size:18px;flex-shrink:0">›</span>
        </div>
      `;
      const cleanup = attachRowGestures(row, {
        onTap: () => {
          if (field === 'year_min') {
            // Exact-year filter via year_min + year_max
            crumb = {
              label: String(value), field: 'year_min', value: String(value),
              extraField: 'year_max', extraValue: String(value),
            };
          } else {
            crumb = { label: display, field, value: String(value) };
          }
          render();
        },
      });
      _gestureCleanups.push(cleanup);
      listEl.appendChild(row);
    });
  }

  function emojiFor(g) {
    return ({
      artists: '🎤', album_artists: '🎤', albums: '💿', genres: '🏷', years: '📅',
    })[g] || '🎵';
  }

  function cleanupGestures() {
    _gestureCleanups.forEach(fn => fn());
    _gestureCleanups = [];
  }

  // First render
  render();
}
