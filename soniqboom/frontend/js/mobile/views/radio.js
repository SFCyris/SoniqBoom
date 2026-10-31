// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * radio.js — Mobile Internet-radio view.
 *
 * Segments: ★ Favorites / Scene / Browse (search).  Tap a station to play it;
 * tap the star to favourite / un-favourite.  Above the list: the tuner — a
 * display with the station that plays (and its play / stop keys) over a dial
 * whose needle stands at that station's place in the list shown, and the
 * first eight favourites as numbered preset keys.
 *
 * Playback goes through MobileRadio (radio-player.js), a dedicated <audio>
 * element.  With the Now Playing meters on (``metersOn``) a station plays
 * through /api/stations/relay on the element routed through the audio graph,
 * so the meters follow it (WebKit gets the relay as a file, ``file=1`` — it
 * hands the graph no samples of an endless stream); with them off, or when the
 * relay can't play it, it streams the station's own URL DIRECTLY (station →
 * phone) whenever the browser can play it, falling back to the plain relay
 * when direct isn't viable (mixed content / HLS / playlist wrapper) or fails
 * at runtime.  Turning the meters on or off re-tunes the playing station.
 * MobileRadio's 'change'/'state' events drive the mini-player and Now Playing
 * view.
 */
import { MobileRadio, metersOn } from '../radio-player.js';
import { esc } from './_common.js';
import { createStationLookup, stationQueryKey } from '../../station_lookup.js';

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
  let loadGen = 0;                       // the latest load / search; older answers are dropped
  // Browse searches the Radio Browser directory: once typing paused, the stale
  // request aborted, recent answers kept (station_lookup.js).
  const lookup = createStationLookup({
    fetchRows: (text, signal) =>
      fetch(`/api/stations/search?q=${encodeURIComponent(text)}&limit=40`, { signal })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
  });

  root.innerHTML = `
    <div class="m-tuner" id="radio-tuner">
      <div class="m-tuner-now" id="radio-now" hidden>
        <div class="m-tuner-vfd m-vfd">
          <div class="m-tuner-ann" aria-hidden="true">
            <span class="m-tuner-state" id="radio-now-state">LIVE</span>
            <span class="m-tuner-flag">STEREO</span>
            <span class="m-tuner-flag" id="radio-now-codec"></span>
          </div>
          <div class="m-tuner-name" id="radio-now-name"></div>
          <div class="m-tuner-dial" aria-hidden="true">
            <span class="m-tuner-scale"><i>88</i><i>92</i><i>96</i><i>100</i><i>104</i><i>108</i></span>
            <span class="m-tuner-needle" id="radio-needle"></span>
          </div>
        </div>
        <div class="m-tuner-keys">
          <button type="button" class="m-key m-tuner-play" id="radio-now-play" aria-label="Pause">⏸&#xFE0E;</button>
          <button type="button" class="m-key" id="radio-now-stop" aria-label="Stop">■&#xFE0E;</button>
        </div>
      </div>
      <div class="m-presets" id="radio-presets" role="group" aria-label="Preset stations" hidden></div>
    </div>
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
    <div class="sr-only" id="radio-status" role="status" aria-live="polite"></div>
  `;

  const tabsBar  = root.querySelector('#radio-tabs');
  const searchWrap = root.querySelector('#radio-search-wrap');
  const searchIn = root.querySelector('#radio-search');
  const listEl   = root.querySelector('#radio-list');
  const emptyEl  = root.querySelector('#radio-empty');
  const loadEl   = root.querySelector('#radio-loading');
  const statusEl = root.querySelector('#radio-status');
  const nowEl    = root.querySelector('#radio-now');
  const nowName  = root.querySelector('#radio-now-name');
  const nowState = root.querySelector('#radio-now-state');
  const nowCodec = root.querySelector('#radio-now-codec');
  const nowPlay  = root.querySelector('#radio-now-play');
  const needle   = root.querySelector('#radio-needle');
  const presetsEl = root.querySelector('#radio-presets');
  let shown = [];                        // the stations the list shows (the dial's band)
  // Said to a screen reader (a live region that is always there: one that is
  // shown only with its message is not read out reliably).  Emptied first, so
  // the same message said again is read again.
  function announce(text) {
    statusEl.textContent = '';
    setTimeout(() => { statusEl.textContent = text; }, 60);
  }

  tabsBar.addEventListener('click', (e) => {
    const chip = e.target.closest('.m-group-chip');
    if (!chip) return;
    tab = chip.dataset.t;
    root.dataset.tab = tab;                // (a phone shows the presets beside the other lists only)
    [...tabsBar.children].forEach(c => c.classList.toggle('active', c === chip));
    searchWrap.classList.toggle('hidden', tab !== 'browse');
    if (tab === 'browse') {
      loadGen++; lookup.cancel();
      listEl.innerHTML = ''; emptyEl.classList.add('hidden'); loadEl.classList.add('hidden');
      searchIn.focus();
      if (searchIn.value.trim()) search();
    }
    else load();
  });

  searchIn.addEventListener('input', () => search());

  // A failed load says so, with a Retry (not "No stations found").
  function showError(retry) {
    listEl.innerHTML = '';
    emptyEl.innerHTML = `Couldn’t load stations. <button type="button" class="m-retry">Retry</button>`;
    emptyEl.dataset.kind = 'error';
    emptyEl.querySelector('.m-retry').addEventListener('click', () => { announce(''); retry(); });
    emptyEl.classList.remove('hidden');
    announce('Couldn’t load stations. Retry is below.');
  }

  function search() {
    const gen = ++loadGen;
    delete emptyEl.dataset.kind;
    const text = searchIn.value;
    const n = stationQueryKey(text).length;
    if (n < 2) {
      lookup.cancel();
      listEl.innerHTML = '';
      loadEl.classList.add('hidden');
      emptyEl.textContent = 'Type two or more characters.';
      emptyEl.classList.toggle('hidden', n === 0);
      return;
    }
    emptyEl.classList.add('hidden');
    loadEl.classList.remove('hidden');
    lookup.request(text,
      (rows) => { if (gen !== loadGen) return; loadEl.classList.add('hidden'); render(rows); },
      () => { if (gen !== loadGen) return; loadEl.classList.add('hidden'); showError(search); });
  }

  async function load() {
    if (tab === 'browse') { search(); return; }
    lookup.cancel();
    const gen = ++loadGen;
    delete emptyEl.dataset.kind;
    loadEl.classList.remove('hidden');
    emptyEl.classList.add('hidden');
    const url = tab === 'favorites' ? '/api/stations/favorites' : '/api/stations/scene';
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const stations = await res.json();
      if (gen !== loadGen) return;
      render(Array.isArray(stations) ? stations : []);
    } catch (err) {
      if (gen !== loadGen) return;
      console.error('Stations load failed', err);
      showError(load);
    } finally {
      if (gen === loadGen) loadEl.classList.add('hidden');
    }
  }

  function render(stations) {
    listEl.innerHTML = '';
    shown = stations;
    if (tab === 'favorites') paintPresets(stations);
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
    presetsEl.querySelectorAll('.m-preset').forEach(k => {
      const on = !!sid && k.dataset.sid === sid;
      k.classList.toggle('playing', on);
      if (on) k.setAttribute('aria-current', 'true'); else k.removeAttribute('aria-current');
    });
    paintTuner();
  }

  // ── Tuner ───────────────────────────────────────────────────────────────
  // The display: the station on air, LIVE / PAUSE, its codec; the
  // needle at the station's place in the list shown (outside it: its place by
  // name hash, so it still moves from station to station).
  function paintTuner() {
    const st = MobileRadio.active ? MobileRadio.station : null;
    nowEl.hidden = !st;
    if (!st) return;
    nowName.textContent = st.name || 'Station';
    const best = (st.streams || [])[0] || {};
    nowCodec.textContent = [best.codec, best.bitrate ? `${best.bitrate}k` : ''].filter(Boolean).join(' ').toUpperCase();
    const playing = MobileRadio.playing;
    nowEl.dataset.state = playing ? 'live' : 'paused';
    nowState.textContent = playing ? 'LIVE' : 'PAUSE';
    nowPlay.textContent = playing ? '\u23F8\uFE0E' : '\u25B6\uFE0E';
    nowPlay.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    const at = shown.findIndex((x) => x && x.sid === st.sid);
    let f;
    if (at >= 0) f = shown.length > 1 ? at / (shown.length - 1) : 0.5;
    else {
      let h = 0;
      for (const ch of String(st.sid || st.name || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
      f = (h % 1000) / 999;
    }
    needle.style.setProperty('--f', (0.04 + 0.92 * f).toFixed(4));
  }
  nowPlay.addEventListener('click', () => MobileRadio.toggle());
  root.querySelector('#radio-now-stop').addEventListener('click', () => MobileRadio.stop());
  MobileRadio.on('state', paintTuner);

  // Preset keys: the first eight favourites (read with the Favorites tab, or on
  // their own while another tab shows).
  const PRESETS = 8;
  function paintPresets(favs) {
    const presets = (favs || []).slice(0, PRESETS);
    presetsEl.hidden = !presets.length;
    presetsEl.replaceChildren(...presets.map((st, i) => {
      const k = document.createElement('button');
      k.type = 'button';
      k.className = 'm-preset';
      k.dataset.sid = st.sid;
      k.setAttribute('aria-label', `Preset ${i + 1}: ${st.name || 'Station'}`);
      const n = document.createElement('b');
      n.textContent = String(i + 1);
      const name = document.createElement('span');
      name.textContent = st.name || 'Station';
      k.append(n, name);
      k.addEventListener('click', () => playStation(st));
      return k;
    }));
    highlightPlaying();
  }
  async function loadPresets() {
    try {
      const r = await fetch('/api/stations/favorites');
      if (!r.ok) return;
      const favs = await r.json();
      if (Array.isArray(favs)) paintPresets(favs);
    } catch (_) { /* the keys keep what they show */ }
  }

  // The meters setting the playing station was tuned for: a change made here
  // (a tap — the audio graph may need its gesture) re-tunes a playing
  // station; a paused one, or a change made in another tab, is tuned in again
  // when it resumes.
  let meteredWanted = null;
  const _stale = () => MobileRadio.active && meteredWanted !== null
    && metersOn() !== meteredWanted;
  window.addEventListener('sb:viz-settings', () => {
    if (_stale() && MobileRadio.playing) playStation(MobileRadio.station);
  });
  MobileRadio.onResume = () => {
    if (!_stale()) return false;
    playStation(MobileRadio.station);
    return true;
  };

  async function playStation(st) {
    const c = _pickStream(st);
    if (!c) { ctx.toast('This browser can’t play any of this station’s streams', 'error'); return; }
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
    meteredWanted = metersOn();
    // WebKit's audio graph hears the relay only as a file, which it is for
    // MP3 / AAC stations; any other one plays directly (no meters).
    const webkit = /Apple/.test(navigator.vendor || '');
    if (meteredWanted && (!webkit || /mp3|aac/i.test(c.s.codec || ''))) {
      const url = relayUrl + (/Apple/.test(navigator.vendor || '') ? '&file=1' : '');
      try {
        await MobileRadio.play(st, url, { metered: true });
        highlightPlaying();
        return;
      } catch (_) {
        if (MobileRadio.station !== st) return;      // superseded meanwhile
      }                                              // → as with the meters off
    }
    try {
      await MobileRadio.play(st, canDirect ? directUrl : relayUrl);
    } catch (err) {
      if (canDirect) {
        try { await MobileRadio.play(st, relayUrl); }
        catch { MobileRadio.stop(); ctx.toast('Could not start the station', 'error'); return; }
      } else { MobileRadio.stop(); ctx.toast('Could not start the station', 'error'); return; }
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
      loadPresets();
    } catch {
      st.favorite = was;                 // rollback on failure
      btn.textContent = st.favorite ? '★' : '☆';
      ctx.toast('Could not update favourites', 'error');
    }
  }

  // Re-load favourites when the view is re-shown (a station favourited elsewhere).
  root.addEventListener('viewactive', () => { if (tab === 'favorites') load(); else loadPresets(); });
  // Keep the active-row highlight in sync when the station changes (started,
  // switched, or stopped — from here, the mini-player, or Now Playing).
  MobileRadio.on('change', highlightPlaying);

  root.dataset.tab = tab;
  paintTuner();
  load();   // initial: favourites
}
