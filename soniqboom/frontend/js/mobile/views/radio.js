// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * radio.js — Mobile Internet-radio view.
 *
 * Segments: ★ Favorites / Scene / Browse (search).  Tap a station to play it;
 * tap the star to favourite / un-favourite.
 *
 * Playback goes through MobileRadio (radio-player.js), a dedicated <audio>
 * element: it streams the station's own URL DIRECTLY (station → phone) whenever
 * the browser can play it, sparing the server the relay hop, and falls back to
 * /api/stations/relay only when direct isn't viable (mixed content / HLS /
 * playlist wrapper) or fails at runtime.  MobileRadio's 'change'/'state' events
 * drive the mini-player and Now Playing view.
 */
import { MobileRadio } from '../radio-player.js';
import { esc } from './_common.js';

const _probe = document.createElement('audio');

function _mimeFor(codec) {
  const c = (codec || '').toLowerCase();
  if (c.includes('mp3')) return 'audio/mpeg';
  if (c.includes('aac')) return 'audio/aac';
  if (c.includes('opus')) return 'audio/ogg; codecs=opus';
  if (c.includes('ogg') || c.includes('vorbis')) return 'audio/ogg';
  if (c.includes('flac')) return 'audio/flac';
  return '';
}

/** Pick the best stream this browser can play (support, then bitrate). */
function _pickStream(station) {
  const streams = station.streams || [];
  if (!streams.length) return null;
  const scored = streams.map((s, v) => {
    const mime = _mimeFor(s.codec);
    const support = !mime ? 1 : (_probe.canPlayType(mime) ? 2 : 0);
    return { v, s, support };
  });
  scored.sort((a, b) => (b.support - a.support) || ((b.s.bitrate || 0) - (a.s.bitrate || 0)));
  // Return the best stream even when the browser can't play any codec DIRECTLY
  // (support 0) — the server relay can still transcode/serve it; the caller
  // forces the relay for a support-0 pick rather than rejecting the station.
  return scored[0];
}

/** Can the browser load this station URL DIRECTLY (no server relay)?
 *  Direct playback saves the server→client hop (mobile bandwidth), but the
 *  relay is required when:
 *    · the page is https and the stream is http → mixed-content, hard-blocked;
 *    · the stream is HLS → <audio> can't play a manifest (Safari aside);
 *    · the URL is a .pls/.m3u/.xspf/.asx WRAPPER → needs resolving first.
 *  In those cases the caller falls back to /api/stations/relay. */
function _canPlayDirect(url, stream) {
  if (!url) return false;
  if (location.protocol === 'https:' && /^http:\/\//i.test(url)) return false;
  if (stream && stream.hls) return false;
  if (/\.(m3u8?|pls|xspf|asx)(\?|#|$)/i.test(url)) return false;
  return true;
}

export function mountRadio(root, ctx) {
  let tab = 'favorites';                 // 'favorites' | 'scene' | 'browse'
  let searchTimer = null;

  root.innerHTML = `
    <div class="m-group-bar" id="radio-tabs">
      <button class="m-group-chip active" data-t="favorites">★ Favorites</button>
      <button class="m-group-chip"        data-t="scene">Scene</button>
      <button class="m-group-chip"        data-t="browse">Browse</button>
    </div>
    <div class="m-radio-search hidden" id="radio-search-wrap">
      <input type="search" id="radio-search" placeholder="Search stations…"
             enterkeyhint="search" autocapitalize="none" autocomplete="off">
    </div>
    <ul class="m-list" id="radio-list"></ul>
    <div class="m-empty hidden" id="radio-empty"></div>
    <div class="m-loading hidden" id="radio-loading">Loading…</div>
  `;

  const tabsBar  = root.querySelector('#radio-tabs');
  const searchWrap = root.querySelector('#radio-search-wrap');
  const searchIn = root.querySelector('#radio-search');
  const listEl   = root.querySelector('#radio-list');
  const emptyEl  = root.querySelector('#radio-empty');
  const loadEl   = root.querySelector('#radio-loading');

  tabsBar.addEventListener('click', (e) => {
    const chip = e.target.closest('.m-group-chip');
    if (!chip) return;
    tab = chip.dataset.t;
    [...tabsBar.children].forEach(c => c.classList.toggle('active', c === chip));
    searchWrap.classList.toggle('hidden', tab !== 'browse');
    if (tab === 'browse') { listEl.innerHTML = ''; emptyEl.classList.add('hidden'); searchIn.focus(); }
    else load();
  });

  searchIn.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(), 250);
  });

  async function load() {
    loadEl.classList.remove('hidden');
    emptyEl.classList.add('hidden');
    let url;
    if (tab === 'favorites') url = '/api/stations/favorites';
    else if (tab === 'scene') url = '/api/stations/scene';
    else {
      const q = searchIn.value.trim();
      if (!q) { listEl.innerHTML = ''; loadEl.classList.add('hidden'); return; }
      url = `/api/stations/search?q=${encodeURIComponent(q)}&limit=40`;
    }
    try {
      const res = await fetch(url);
      const stations = res.ok ? await res.json() : [];
      render(Array.isArray(stations) ? stations : []);
    } catch (err) {
      console.error('Stations load failed', err);
      render([]);
    } finally {
      loadEl.classList.add('hidden');
    }
  }

  function render(stations) {
    listEl.innerHTML = '';
    if (!stations.length) {
      emptyEl.textContent = tab === 'favorites'
        ? 'No favourite stations yet — Browse or open Scene, then tap ☆ to save one.'
        : tab === 'browse' ? 'No stations found.' : 'Nothing here.';
      emptyEl.classList.remove('hidden');
      return;
    }
    for (const st of stations) {
      const best = (st.streams || [])[0] || {};
      const meta = [best.codec, best.bitrate ? `${best.bitrate}k` : '',
                    (st.streams || []).length > 1 ? `${st.streams.length} streams` : '']
                   .filter(Boolean).join(' · ');
      const row = document.createElement('div');
      row.className = 'm-row m-radio-row';
      row.dataset.sid = st.sid;
      row.innerHTML = `
        <div class="m-row-content">
          <div class="m-row-art"><span>📻</span>${st.favicon
            ? `<img src="${esc(st.favicon)}" alt="" decoding="async">`
            : ''}</div>
          <div class="m-row-meta">
            <div class="m-row-title">${esc(st.name || 'Station')}</div>
            <div class="m-row-artist">${esc(meta)}</div>
          </div>
          <button class="m-radio-fav" aria-label="${st.favorite ? 'Remove favourite' : 'Add favourite'}">${st.favorite ? '★' : '☆'}</button>
        </div>`;
      const img = row.querySelector('.m-row-art img');
      if (img) {
        // ``.m-row-art img`` starts at opacity:0 and only shows once it gets the
        // ``.loaded`` class (the fade-in the track rows + Now Playing use).  This
        // img is built via innerHTML with its src already set, so wire load here
        // — and handle the already-cached case where 'load' won't fire again.
        const markLoaded = () => img.classList.add('loaded');
        if (img.complete && img.naturalWidth > 0) markLoaded();
        else img.addEventListener('load', markLoaded, { once: true });
        // The 📻 glyph sits behind the img already — on error just drop the img
        // so the glyph shows through (no duplicate span).
        img.onerror = () => img.remove();
      }
      // Tap the row (not the star) → play.
      row.querySelector('.m-row-content').addEventListener('click', (e) => {
        if (e.target.closest('.m-radio-fav')) return;
        playStation(st);
      });
      row.querySelector('.m-radio-fav').addEventListener('click', (e) => {
        e.stopPropagation();
        toggleFavorite(st, e.currentTarget);
      });
      listEl.appendChild(row);
    }
    highlightPlaying();
  }

  /** Mark the row of the currently-playing station (if it's in this list). */
  function highlightPlaying() {
    const sid = MobileRadio.active && MobileRadio.station.sid;
    listEl.querySelectorAll('.m-radio-row').forEach(r => {
      r.classList.toggle('playing', !!sid && r.dataset.sid === sid);
    });
  }

  async function playStation(st) {
    const c = _pickStream(st);
    if (!c) { ctx.toast('This browser can’t play any of this station’s streams'); return; }
    // Prefer DIRECT playback (station → phone) so the server never relays it;
    // fall back to the relay when direct isn't viable (mixed content / HLS /
    // playlist wrapper) or if the direct attempt fails at runtime (a SHOUTcast
    // root that serves HTML to browsers, a dead mount, an odd redirect).
    const directUrl = c.s.url;
    const relayUrl  = `/api/stations/relay/${encodeURIComponent(st.sid)}?v=${c.v}`;
    // Direct only when the browser can play this codec (support > 0) AND the URL
    // is viable; a support-0 pick or a non-direct URL goes through the relay.
    const canDirect = c.support > 0 && _canPlayDirect(directUrl, c.s);
    ctx.toast(`Tuning in — ${st.name || 'station'}`);
    try {
      await MobileRadio.play(st, canDirect ? directUrl : relayUrl);
    } catch (err) {
      if (canDirect) {
        try { await MobileRadio.play(st, relayUrl); }
        catch { MobileRadio.stop(); ctx.toast('Could not start the station'); return; }
      } else { MobileRadio.stop(); ctx.toast('Could not start the station'); return; }
    }
    highlightPlaying();
  }

  async function toggleFavorite(st, btn) {
    const was = st.favorite;
    st.favorite = !was;
    btn.textContent = st.favorite ? '★' : '☆';
    try {
      if (was) {
        await fetch(`/api/stations/favorites/${encodeURIComponent(st.sid)}`, { method: 'DELETE' });
        ctx.toast('Removed from favourites');
      } else {
        await fetch('/api/stations/favorites', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sid: st.sid }),
        });
        ctx.toast('Added to favourites');
      }
    } catch {
      st.favorite = was;                 // rollback on failure
      btn.textContent = st.favorite ? '★' : '☆';
      ctx.toast('Could not update favourites');
    }
  }

  // Re-load favourites when the view is re-shown (a station favourited elsewhere).
  root.addEventListener('viewactive', () => { if (tab === 'favorites') load(); });
  // Keep the active-row highlight in sync when the station changes (started,
  // switched, or stopped — from here, the mini-player, or Now Playing).
  MobileRadio.on('change', highlightPlaying);

  load();   // initial: favourites
}
