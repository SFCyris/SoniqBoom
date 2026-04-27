// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * _common.js — Shared row builder + helpers for mobile views.
 */
import { artPlaceholderEmoji } from '../../utils.js';

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * Build a track row DOM node.  Caller wires gestures via attachRowGestures().
 *
 * Options:
 *   showHandle   — render a drag handle on the right (queue view)
 *   trailing     — extra element placed before the handle (e.g. duration)
 */
export function buildTrackRow(track, opts = {}) {
  const row = document.createElement('div');
  row.className = 'm-row';
  if (track.id) row.dataset.trackId = track.id;

  const content = document.createElement('div');
  content.className = 'm-row-content';

  // Art: always paint the emoji placeholder first, then try to load real art async.
  const art = document.createElement('div');
  art.className = 'm-row-art';
  const ph = document.createElement('span');
  ph.textContent = artPlaceholderEmoji(track);
  art.appendChild(ph);
  const artSrc = track.cover_art || (track.id ? `/api/art/${track.id}?size=sm` : null);
  if (artSrc) {
    const img = new Image();
    img.alt = '';
    img.loading = 'lazy';
    img.onload = () => { art.innerHTML = ''; art.appendChild(img); };
    img.src = artSrc;
  }
  content.appendChild(art);

  // Title + artist
  const meta = document.createElement('div');
  meta.className = 'm-row-meta';
  const title  = document.createElement('div');
  title.className = 'm-row-title';
  title.textContent = track.title || '—';
  const artist = document.createElement('div');
  artist.className = 'm-row-artist';
  artist.textContent = track.artist || track.album_artist || '';
  meta.appendChild(title);
  meta.appendChild(artist);
  content.appendChild(meta);

  if (opts.trailing) content.appendChild(opts.trailing);

  if (opts.showHandle) {
    const handle = document.createElement('div');
    handle.className = 'm-row-handle';
    handle.innerHTML = '☰';
    content.appendChild(handle);
  }

  row.appendChild(content);
  return row;
}

export function fmtDur(sec) {
  if (!sec || !isFinite(sec)) return '';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}

/** Standard long-press action sheet for a track row. */
export function trackActions(track, ctx) {
  return [
    { label: '▶ Play Now', onSelect: () => {
      ctx.player.setQueue([track], 0);
    }},
    { label: '+ Add to Queue', onSelect: () => {
      ctx.player.addToQueue(track);
      ctx.toast(`Added "${track.title || 'track'}" to queue`);
    }},
  ];
}
