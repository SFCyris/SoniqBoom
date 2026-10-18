// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * _common.js — Shared row builder + helpers for mobile views.
 */
import { artPlaceholderEmoji, staticArtUrl } from '../../utils.js';
import { offlineKind, offlineBadge } from '../../offline.js';
import { healthBadge, canReport, mark as healthMark, isUnplayable } from '../../health.js';

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
  // network request — cheap for same-origin /api/art (mostly cacheable 204s in a
  // chiptune library).  This is deliberately NOT loading="lazy" + no manual
  // IntersectionObserver: an observer does not fire for imgs inside a
  // content-visibility-skipped subtree, which left covers permanently blank.
  img.src = src;
}

// A playback problem reported / cleared (health.js): rows built here (search
// results, the queue) repaint their badge in place.
document.addEventListener('soniqboom:health-changed', (e) => {
  const d = (e && e.detail) || {};
  for (const id of Array.isArray(d.ids) ? d.ids : []) {
    const sel = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
    document.querySelectorAll(`.m-row[data-track-id="${sel}"]`).forEach((row) => {
      const t = row.__track;
      if (!t) return;
      Object.assign(t, d.fields || {});
      const title = row.querySelector('.m-row-title');
      if (!title) return;
      title.querySelector('.track-health-badge')?.remove();
      const b = healthBadge(t);
      if (!b) return;
      if (!title.classList.contains('m-row-title--badged')) {
        const txt = document.createElement('span');
        txt.className = 'm-row-title-text';
        txt.textContent = title.textContent;
        title.textContent = '';
        title.classList.add('m-row-title--badged');
        title.append(txt);
      }
      const off = title.querySelector('.track-offline-badge');
      title.insertBefore(b, off || null);
    });
  }
});

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
  row.__track = track;

  const content = document.createElement('div');
  content.className = 'm-row-content';

  // Art: always paint the emoji placeholder first, then try to load real art async.
  const art = document.createElement('div');
  art.className = 'm-row-art';
  const ph = document.createElement('span');
  ph.textContent = artPlaceholderEmoji(track);
  art.appendChild(ph);
  const artSrc = staticArtUrl(track.cover_art) || (track.id ? `/api/art/${track.id}/sm.jpg?fallback=none` : null);
  if (artSrc) {
    const img = new Image();
    img.alt = '';
    img.decoding = 'async';   // decode off the main thread — keeps scroll smooth
    // Append the img alongside the placeholder span (no innerHTML wipe)
    // so a 204 (no art) / decode failure leaves the format glyph visible.  The
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
  // Health badge for a playback problem (health.js), "offline" for a track that
  // can't play right now — only badged rows (rare) take the flex layout so the
  // common path keeps its plain ellipsised text.
  const _dfct = healthBadge(track);
  const _off = offlineKind(track);
  if (_off) row.classList.add('track-offline');
  if (_dfct || _off) {
    title.classList.add('m-row-title--badged');
    const ttlText = document.createElement('span');
    ttlText.className = 'm-row-title-text';
    ttlText.textContent = track.title || '—';
    title.append(ttlText);
    if (_dfct) title.append(_dfct);
    if (_off) title.append(offlineBadge(_off));
  } else {
    title.textContent = track.title || '—';
  }
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
 *  Deliberately NO tag-editing on mobile — playback + playlist actions only,
 *  and (edit rights) reporting a playback problem, which is no tag edit. */
export function trackActions(track, ctx) {
  // (Not for a track that doesn't play at all — health.js isUnplayable —
  // unless a report is on it to clear.)
  const report = canReport() && (!isUnplayable(track) || track.health) ? [
    { label: '⚠︎ Report a problem…', onSelect: () => reportSheet(track, ctx) },
  ] : [];
  return [
    { label: '▶ Play Now', onSelect: () => {
      ctx.player.setQueue([track], 0);
    }},
    { label: '+ Add to Queue', onSelect: () => {
      ctx.player.addToQueue(track);
      ctx.toast(`Added "${track.title || 'track'}" to queue`);
    }},
    { label: '♫ Add to Playlist…', onSelect: () => addToPlaylistSheet(track, ctx) },
    ...report,
  ];
}

/** "Report a problem": No problem · Glitched · Broken (health.js). */
export function reportSheet(track, ctx) {
  const cur = track.health === 'broken' || track.health === 'glitched' ? track.health : '';
  const tick = (s) => (s === cur ? '✓ ' : '');
  ctx.showSheet({
    title: `Playback problem — ${track.title || 'track'}`,
    actions: [
      { label: `${tick('glitched')}Glitched — plays, with wrong sounds`,
        onSelect: () => { if (cur !== 'glitched') healthMark(track, 'glitched'); } },
      { label: `${tick('broken')}Broken — passed by when a list plays through`,
        onSelect: () => { if (cur !== 'broken') healthMark(track, 'broken'); } },
      { label: `${tick('')}No problem`,
        onSelect: () => { if (cur) healthMark(track, null); } },
    ],
  });
}

// ── Playlists ────────────────────────────────────────────────────────────────

/** The playlist API accepts a bare id string, or `{id, subsong}` for a
 *  specific subsong.  Mobile tracks rarely carry a subsong, but honour it if
 *  present so a queued subsong lands in a playlist as the right tune. */
export function playlistEntry(track) {
  return (Number.isInteger(track.subsong) && track.subsong >= 0)
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
      } catch { ctx.toast('Could not create playlist', 'error'); }
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
        } catch { ctx.toast('Could not add to playlist', 'error'); }
      },
    })),
  ];
  ctx.showSheet({ title: 'Add to playlist', actions });
}
