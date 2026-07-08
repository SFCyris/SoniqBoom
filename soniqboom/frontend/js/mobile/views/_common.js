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

// Lazy cover-art loader.  The library rows carry ``content-visibility:auto``
// (see mobile.css), which is the browser's own viewport-based deferral — it
// skips layout/paint/decode for off-screen rows.  A manual IntersectionObserver
// does NOT compose with that (observers don't fire for imgs the browser is
// skipping), so we use native ``loading="lazy"`` — also viewport-driven, so the
// two coordinate — and set ``src`` in the SAME call (creating the img with a src
// rather than the detached-append-then-src trap that dropped covers before).
export function lazyArt(img, src) {
  if (!src) return;
  img.decoding = 'async';
  // Set src directly.  The rows carry ``content-visibility:auto`` which already
  // skips off-screen layout/paint/DECODE, so the only remaining cost is the
  // network request — cheap for same-origin /api/art (mostly cacheable 404s in a
  // chiptune library).  This is deliberately NOT loading="lazy" + no manual
  // IntersectionObserver: an observer does not fire for imgs inside a
  // content-visibility-skipped subtree, which left covers permanently blank.
  img.src = src;
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
  const artSrc = track.cover_art || (track.id ? `/api/art/${track.id}?size=sm&fallback=404` : null);
  if (artSrc) {
    const img = new Image();
    img.alt = '';
    img.decoding = 'async';   // decode off the main thread — keeps scroll smooth
    // Append the img alongside the placeholder span (no innerHTML wipe)
    // so a 404 / decode failure leaves the format glyph visible.  The
    // .loaded class triggers the CSS opacity fade-in defined in mobile.css.
    // src is set on viewport entry via lazyArt() (see the trap note above).
    img.onload  = () => img.classList.add('loaded');
    img.onerror = () => img.remove();
    art.appendChild(img);
    lazyArt(img, artSrc);
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

/** Standard long-press action sheet for a track row.
 *  Deliberately NO tag-editing on mobile — playback + playlist actions only. */
export function trackActions(track, ctx) {
  return [
    { label: '▶ Play Now', onSelect: () => {
      ctx.player.setQueue([track], 0);
    }},
    { label: '+ Add to Queue', onSelect: () => {
      ctx.player.addToQueue(track);
      ctx.toast(`Added "${track.title || 'track'}" to queue`);
    }},
    { label: '♫ Add to Playlist…', onSelect: () => addToPlaylistSheet(track, ctx) },
  ];
}

// ── Playlists ────────────────────────────────────────────────────────────────

/** The playlist API accepts a bare id string, or `{id, subsong}` for a
 *  specific subsong.  Mobile tracks rarely carry a subsong, but honour it if
 *  present so a queued subsong lands in a playlist as the right tune. */
export function playlistEntry(track) {
  return (Number.isInteger(track.subsong) && track.subsong > 0)
    ? { id: track.id, subsong: track.subsong } : track.id;
}

/** Fetch the user's playlists, or `[]`. */
export async function fetchPlaylists() {
  try {
    const r = await fetch('/api/playlists');
    if (!r.ok) return [];
    const d = await r.json();
    return Array.isArray(d) ? d : (d.playlists || []);
  } catch { return []; }
}

/** "Add to playlist" sub-sheet: pick an existing (non-smart) playlist or make a
 *  new one.  Smart playlists are query-driven and can't take manual tracks, so
 *  they're excluded here. */
export async function addToPlaylistSheet(track, ctx) {
  const pls = (await fetchPlaylists()).filter(p => !p.smart);
  const actions = [
    { label: '＋ New playlist…', onSelect: async () => {
      const name = (window.prompt('New playlist name') || '').trim();
      if (!name) return;
      try {
        const r = await fetch('/api/playlists', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, track_ids: [playlistEntry(track)] }),
        });
        if (!r.ok) throw new Error();
        ctx.toast(`Created "${name}"`);
      } catch { ctx.toast('Could not create playlist'); }
    }},
    ...pls.map(p => ({
      label: `${p.name}${p.track_count ? `  ·  ${p.track_count}` : ''}`,
      onSelect: async () => {
        try {
          const r = await fetch(`/api/playlists/${encodeURIComponent(p.id)}/tracks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: [playlistEntry(track)] }),
          });
          if (!r.ok) throw new Error();
          ctx.toast(`Added to "${p.name}"`);
        } catch { ctx.toast('Could not add to playlist'); }
      },
    })),
  ];
  ctx.showSheet({ title: 'Add to playlist', actions });
}
