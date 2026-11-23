// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * app.js — Mobile shell entry: view router, tab bar, mini-player, action sheet.
 */
import { Player } from '../player.js';
import { Auth }   from '../auth.js';
import { artPlaceholderEmoji, Toast, staticArtUrl } from '../utils.js';
import { Offline } from '../offline.js';
import { Health, nothingToPlayText } from '../health.js';
// The phone page is always Retro hi-fi: mobile.html fixes <html data-ui-style>
// (data-ui-style-fixed — js/ui_style.js, should a shared module load it, keeps
// it and never writes the desktop page's Style).
// UX-3 P0: mobile shell exposes the same globals desktop does so the
// classic-script cast_picker.js can read currentTrackId + emit toasts.
window.SoniqBoom = window.SoniqBoom || {};
window.SoniqBoom.player = Player;
// Names this client in the synced play queue ("Resume the queue from …").
window.__sbClientLabel = 'SoniqBoom Mobile';
// …and where its Play control is, for "Queue resumed — … ".
window.__sbPlayHint = 'Tap \u25B6 in the mini player.';
window.Toast = Toast;

// Codec capability handshake (WIN 5) — same as desktop app.js: probe what this
// browser can decode and hand it to the server in ``sb_caps`` so ALAC/AAC/Opus
// direct-serve is driven by real capability, not UA-sniffing.  Best-effort.
(function setCodecCaps() {
  try {
    const a = document.createElement('audio');
    const can = (t) => { const r = a.canPlayType(t); return r === 'probably' || r === 'maybe'; };
    const caps = [];
    if (can('audio/mp4; codecs="alac"'))      caps.push('alac');
    if (can('audio/mp4; codecs="mp4a.40.2"')) caps.push('aac');
    if (can('audio/ogg; codecs="opus"'))      caps.push('opus');
    if (can('audio/ogg; codecs="vorbis"'))    caps.push('vorbis');
    if (can('audio/flac'))                    caps.push('flac');
    document.cookie = 'sb_caps=' + caps.join('.')
      + '; path=/; max-age=31536000; SameSite=Lax';
  } catch (_) { /* server falls back to UA detection */ }
})();

import { MobileRadio, paintStationArt, forgetStationArt } from './radio-player.js';
import { freshArt, reaskArt, noteArtChanged, noteArtMissed } from './views/_common.js';
import { forgetCover, forgetAllCovers } from '../cover_status.js';
// A cover changed (art_ready: that track; art_refresh or a reconnect: any —
// _common.js sends 'soniqboom:art-changed' for each): forget what the batched
// cover status (cover_status.js) said, so list rows told "no cover" ask again.
document.addEventListener('soniqboom:art-changed', (e) => {
  const d = (e && e.detail) || {};
  if (d.id && !d.missing) forgetCover(String(d.id)); else forgetAllCovers();
});
import { mountLibrary }    from './views/library.js';
import { mountSearch }     from './views/search.js';
import { mountRadio }      from './views/radio.js';
import { mountQueue }      from './views/queue.js';
import { mountNowPlaying } from './views/nowplaying.js';
import { mountSettings }   from './views/settings.js';

// ── DOM refs ──────────────────────────────────────────────────────────────
const tabs        = document.querySelectorAll('.m-tab');
const views       = document.querySelectorAll('.m-view');
const topbarTitle = document.getElementById('m-topbar-title');

const mini        = document.getElementById('m-miniplayer');
const miniArt     = document.getElementById('m-mp-art');
const miniTitle   = document.getElementById('m-mp-title');
const miniArtist  = document.getElementById('m-mp-artist');
const miniPlay    = document.getElementById('m-mp-play');
const miniNext    = document.getElementById('m-mp-next');
const miniProg    = document.getElementById('m-mp-progress-fill');

const sheet       = document.getElementById('m-sheet');
const sheetBg     = document.getElementById('m-sheet-backdrop');
const sheetTitle  = document.getElementById('m-sheet-title');
const sheetList   = document.getElementById('m-sheet-actions');
const sheetCancel = document.getElementById('m-sheet-cancel');


// ── View routing ──────────────────────────────────────────────────────────
const VIEWS = {
  library:    { title: 'Library',     mount: mountLibrary,    el: document.getElementById('m-view-library') },
  search:     { title: 'Search',      mount: mountSearch,     el: document.getElementById('m-view-search') },
  radio:      { title: 'Radio',       mount: mountRadio,      el: document.getElementById('m-view-radio') },
  queue:      { title: 'Queue',       mount: mountQueue,      el: document.getElementById('m-view-queue') },
  nowplaying: { title: 'Now Playing', mount: mountNowPlaying, el: document.getElementById('m-view-nowplaying') },
  settings:   { title: 'Settings',    mount: mountSettings,   el: document.getElementById('m-view-settings') },
};

// Settings lives behind the top-bar gear rather than a bottom tab (5 tabs max).
const settingsBtn = document.getElementById('m-settings-btn');
if (settingsBtn) settingsBtn.addEventListener('click', () => activate('settings'));

const _mounted = {};

function activate(viewName) {
  const v = VIEWS[viewName];
  if (!v) return;

  views.forEach(el => el.classList.toggle('active', el === v.el));
  tabs.forEach(t => {
    const on = t.dataset.view === viewName;
    t.classList.toggle('active', on);
    // (a navigation bar, not a tab list: the section shown is "current")
    if (on) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
  });
  topbarTitle.textContent = v.title;
  // (Settings has no tab key: the gear shows it pressed)
  if (settingsBtn) {
    settingsBtn.classList.toggle('active', viewName === 'settings');
    if (viewName === 'settings') settingsBtn.setAttribute('aria-current', 'page'); else settingsBtn.removeAttribute('aria-current');
  }

  if (!_mounted[viewName]) {
    v.mount(v.el, { showSheet, toast: showToast, navigate: activate });
    _mounted[viewName] = true;
  } else {
    // Notify view it's been re-shown — useful for queue/nowplaying refresh
    v.el.dispatchEvent(new CustomEvent('viewactive'));
  }

  // Sync URL hash for deep-linking / back button
  if (location.hash !== `#${viewName}`) {
    history.replaceState(null, '', `#${viewName}`);
  }
}

tabs.forEach(t => t.addEventListener('click', () => activate(t.dataset.view)));

// Initial route — accept #view fragment, /m/<view> path, or default to library
function initialView() {
  const hash = (location.hash || '').replace(/^#/, '');
  if (VIEWS[hash]) return hash;
  const path = location.pathname.replace(/^\/m\/?/, '').split('/')[0];
  if (VIEWS[path]) return path;
  return 'library';
}

// ── Mini-player wiring ────────────────────────────────────────────────────
// (U+FE0E: drawn as text in the key's legend colour — iOS shows ⏸ as an emoji)
const PLAY_GLYPH = '\u25B6\uFE0E', PAUSE_GLYPH = '\u23F8\uFE0E';
function renderMini(track) {
  // Radio takes precedence: while a station plays it's the active source.
  if (MobileRadio.active) { renderMiniStation(MobileRadio.station); return; }
  // Hide when there's nothing, OR when a radio takeover detached the shared
  // element (src cleared) so ``track`` is no longer resumable — otherwise the
  // mini would show a dead ▶ that playPause() ignores after radio stops.
  const resumable = track && (Player.playing || (Player.audio && Player.audio.getAttribute('src')));
  // Nothing loaded, but a queue waits (restored after a reload, or resumed from
  // another device): show the track ▶ starts — playPause starts exactly this
  // one (Player.cuedTrack is null in the radio-takeover case above).
  const cued = resumable ? null : (Player.cuedTrack || null);
  _miniCuedKey = cued ? `${cued.id}~${cued.subsong ?? ''}` : null;
  if (!resumable && !cued) {
    mini.classList.add('hidden');
    miniArt.__artSrc = null;
    return;
  }
  if (cued) {
    track = cued;
    miniPlay.textContent = PLAY_GLYPH;
    mini.dataset.state = 'stop';
    const dur = Number(cued.duration) || 0;
    miniProg.style.width = `${dur > 0 ? Math.min(100, ((Player.cuedSec || 0) / dur) * 100) : 0}%`;
  }
  mini.classList.remove('hidden');
  delete mini.dataset.src;
  forgetStationArt(miniArt);
  miniTitle.textContent  = track.title  || '—';
  miniArtist.textContent = track.artist || track.album_artist || '';

  // Artwork: glowing-blue format-emoji placeholder behind a faded <img>.
  // The .loaded class triggers the CSS opacity fade-in; onerror removes
  // the img so the placeholder stays visible (no broken-image glyph).
  miniArt.innerHTML = '';
  const span = document.createElement('span');
  span.className = 'm-mp-art-ph';
  span.textContent = artPlaceholderEmoji(track);
  miniArt.appendChild(span);
  const artSrc = freshArt(staticArtUrl(track.cover_art) || (track.id ? `/api/art/${track.id}/sm.jpg?fallback=none` : null));
  miniArt.__artSrc = artSrc;   // (asked again when the cover changes — _common.js)
  if (artSrc) {
    const img = new Image();
    img.alt = '';
    img.decoding = 'async';   // decode off the main thread on track change
    img.onload  = () => img.classList.add('loaded');
    img.onerror = () => img.remove();
    miniArt.appendChild(img);
    img.src = artSrc;
  }
}

// A station: its name and logo — and, while it says what it plays
// (MobileRadio.now), the song with the artist and the station under it, and
// the song's cover over the logo once one was found.
function renderMiniStation(st) {
  const now = MobileRadio.now;
  const lines = MobileRadio.lines;
  mini.classList.remove('hidden');
  mini.dataset.src = 'radio';
  miniArt.__artSrc = null;
  miniTitle.textContent  = lines.title;
  miniArtist.textContent = lines.sub;
  miniProg.style.width = '0%';   // live stream — no meaningful progress
  paintStationArt(miniArt, { logo: (st && st.favicon) || '', cover: (now && now.cover) || '',
                             phClass: 'm-mp-art-ph' });
}

Player.on('trackchange', renderMini);
// The shown track's cover changed (art_ready / art_refresh — _common.js).
document.addEventListener('soniqboom:art-changed', (e) => {
  if (!MobileRadio.active) reaskArt(miniArt, miniArt, e.detail && e.detail.id, e.detail && e.detail.missing);
});

// A tune of an online collection not read yet — a package (Aminet, scene.org)
// that opens into its first tune on this play, a catalogue file read on its
// first play: the server rewrites its row a moment after the play starts.
// The desktop page hears that over the library WebSocket; this shell reads
// the row again a few times, and a package's other tunes follow it in the
// queue once it opened.
let _rowRefreshGen = 0;
const _REMOTE_RE = /^[a-z][a-z0-9+.-]*:\/\//i;
Player.on('trackchange', (t) => {
  const gen = ++_rowRefreshGen;
  if (!t || !t.id || (t.mtime || 0) > 0 || !_REMOTE_RE.test(String(t.path || ''))) return;
  const id = t.id;
  let queued = false;
  [2000, 5000, 10000, 20000].forEach((ms) => setTimeout(async () => {
    if (gen !== _rowRefreshGen) return;
    try {
      const r = await fetch('/api/tracks/meta/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [id] }), credentials: 'same-origin',
      });
      if (!r.ok || gen !== _rowRefreshGen) return;
      const row = ((await r.json()) || []).find((x) => x && x.id === id);
      if (!row) return;
      const fields = { ...row }; delete fields.id;
      const changed = Player.patchTrackFields?.(id, fields);
      if (!changed || !Object.keys(changed).length) return;
      renderMini(Player.currentTrack);
      document.dispatchEvent(new CustomEvent('soniqboom:track-refreshed',
        { detail: { id, fields: changed } }));
      if (!queued && typeof changed.path === 'string' && changed.path.includes('::')) {
        queued = true;
        const m = await fetch(`/api/tracks/${encodeURIComponent(id)}/package-members`,
                              { credentials: 'same-origin' });
        const ids = m.ok ? (((await m.json()) || {}).ids || []) : [];
        if (!ids.length) return;
        const b = await fetch('/api/tracks/meta/batch', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }), credentials: 'same-origin',
        });
        if (!b.ok) return;
        const byId = new Map(((await b.json()) || []).filter(Boolean).map((x) => [x.id, x]));
        Player.insertAfterTrack?.(id, ids.map((i) => byId.get(i)).filter(Boolean));
      }
    } catch (_) {}
  }, ms));
});
// [m-restart:begin] — tests/js/mobile_restart.test.mjs runs this region as is
// The server says it restarts (the library socket's "server_restarting", as
// the desktop page hears it): a play that fails while it is away — also while
// the new process doesn't answer at all yet — is waited out and played again
// (Player.noteServerRestart), instead of being reported as a failure.  Also
// read: rows of an online collection that left the library
// (``catalogue_tracks_left`` → ``left``, see _tracksLeft), and which tracks
// can play changed (``availability_changed`` → ``avail``: offline.js re-reads
// the state, and the open view and the settings follow), tracks that took
// fields without a scan (``catalogue_changed`` → ``changed``, see
// _catalogueChanged), and what a relayed station plays (``radio_meta`` /
// ``radio_art`` → ``radio``; ``opened`` at every connect: a title said while
// the socket was away is asked for).  The socket comes back after a pause
// that grows to 30 s (1 s after the server's own restart close, 1012) and
// rests while the page is hidden.
function _listenForRestart({ WS = globalThis.WebSocket, doc = globalThis.document,
                             loc = globalThis.location, wait = (fn, ms) => setTimeout(fn, ms),
                             note = () => Player.noteServerRestart(),
                             left = (m) => _tracksLeft(m),
                             avail = () => Offline.refresh(),
                             changed = (m) => _catalogueChanged(m),
                             radio = (m) => _radioSaid(m),
                             art = (id) => noteArtChanged(id),
                             opened = (again) => _socketOpened(again) } = {}) {
  let backoff = 2000;
  let again = false;          // the socket was open before (this is a reconnect)
  const connect = () => {
    if (doc.visibilityState === 'hidden') { doc.addEventListener('visibilitychange', connect, { once: true }); return; }
    let ws;
    try { ws = new WS(`${loc.protocol === 'https:' ? 'wss' : 'ws'}://${loc.host}/api/library/ws`); } catch (_) { return; }
    ws.onmessage = (e) => {
      let m = null;
      try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m && m.event === 'server_restarting') { try { note(); } catch (_) { /* older player */ } }
      else if (m && m.event === 'catalogue_tracks_left') { try { left(m); } catch (_) { /* isolation */ } }
      else if (m && m.event === 'availability_changed') { try { avail(m); } catch (_) { /* isolation */ } }
      else if (m && m.event === 'catalogue_changed') { try { changed(m); } catch (_) { /* isolation */ } }
      else if (m && (m.event === 'radio_meta' || m.event === 'radio_art')) { try { radio(m); } catch (_) { /* isolation */ } }
      // A track's cover arrived after its row was painted (a network share's,
      // one looked up online) / a cover setting changed: the slots ask again.
      else if (m && m.event === 'art_ready' && m.track_id) { try { art(String(m.track_id)); } catch (_) { /* isolation */ } }
      else if (m && m.event === 'art_refresh') { try { art(null); } catch (_) { /* isolation */ } }
    };
    ws.onopen = () => {
      backoff = 2000;
      const was = again;
      again = true;
      try { opened(was); } catch (_) { /* isolation */ }
    };
    ws.onclose = (ev) => {
      const ms = ev && ev.code === 1012 ? 1000 : backoff;
      backoff = Math.min(30000, backoff * 2);
      wait(connect, ms);
    };
  };
  connect();
}
// [m-restart:end]

// Rows of an online collection left the library (a package with no tune in
// it): the player's copies show why (health.js "no tunes"), the rows built
// here repaint their badge, and the open list drops them.
function _tracksLeft(m) {
  const ids = Array.isArray(m && m.ids) ? m.ids.filter((x) => typeof x === 'string') : [];
  if (!ids.length) return;
  const reasons = (m.reasons && typeof m.reasons === 'object') ? m.reasons : {};
  try { Player.markGone?.(ids, reasons); } catch (_) { /* older player */ }
  for (const id of ids) {
    document.dispatchEvent(new CustomEvent('soniqboom:health-changed',
      { detail: { ids: [id], fields: { defect: 'empty', defect_detail: String(reasons[id] || '') } } }));
  }
  document.dispatchEvent(new CustomEvent('soniqboom:tracks-left', { detail: { ids } }));
}

// Tracks took fields without a scan (an online collection's unplayed tunes
// filled from the song database, an enrichment pass that ended): the open
// list reads its rows again in place (views/library.js).
function _catalogueChanged() {
  document.dispatchEvent(new CustomEvent('soniqboom:catalogue-changed'));
}

// What a relayed station plays, and the cover found for that song: the radio
// player keeps it when it is of the station playing here.
function _radioSaid(m) {
  if (m.event === 'radio_meta') MobileRadio.noteMeta(m); else MobileRadio.noteArt(m);
}
// The socket (re)opened.  What it tells only once may have passed while it
// was away: the station's song is asked, and — on a reconnect — the covers
// that are not showing (an ``art_ready`` may have been missed).
function _socketOpened(again) {
  MobileRadio.refreshNow();
  if (again) noteArtMissed();
}

// A queue cued for ▶ (see renderMini): repaint when the cued entry changes —
// the resume / restore itself, a queue edit or a clear.  While a track is
// loaded this is one test and nothing else.
let _miniCuedKey = null;
function _renderMiniCued() {
  if (MobileRadio.active) return;
  if (Player.playing || (Player.audio && Player.audio.getAttribute('src'))) return;
  const c = Player.cuedTrack || null;
  const key = c ? `${c.id}~${c.subsong ?? ''}` : null;
  if (key === _miniCuedKey) return;
  renderMini(Player.currentTrack);
}
Player.on('cue', _renderMiniCued);
Player.on('queuechange', _renderMiniCued);
Player.on('statechange', ({ playing }) => {
  if (MobileRadio.active) return;         // radio owns the transport while active
  miniPlay.textContent = playing ? PAUSE_GLYPH : PLAY_GLYPH;
  mini.dataset.state = playing ? 'playing' : 'paused';    // (the play key's lamp, mobile.css)
  // 'trackchange' fires before the source is on the element, so the very first
  // play of a session found nothing resumable and left the mini hidden: show
  // it once playback has actually started.
  if (playing && mini.classList.contains('hidden') && Player.currentTrack) renderMini(Player.currentTrack);
  // A start that failed (the toast says why, and "Press Next to move on"):
  // the mini-player shows the track, not playing, so Next is there to press.
  else if (!playing && Player.startFailed && mini.classList.contains('hidden') && Player.currentTrack) {
    renderMini(Player.currentTrack);
  }
});
Player.on('timeupdate', ({ pct }) => {
  if (MobileRadio.active) return;
  miniProg.style.width = `${Math.min(100, Math.max(0, pct))}%`;
});

// Radio drives the mini-player when a station is the active source.
MobileRadio.on('change', () => renderMini(Player.currentTrack));
MobileRadio.on('meta',   () => { if (MobileRadio.active) renderMiniStation(MobileRadio.station); });
MobileRadio.on('state',  () => {
  if (MobileRadio.active) {
    miniPlay.textContent = MobileRadio.playing ? PAUSE_GLYPH : PLAY_GLYPH;
    mini.dataset.state = MobileRadio.playing ? 'playing' : 'paused';
  }
});

miniPlay.addEventListener('click', (e) => {
  e.stopPropagation();
  if (MobileRadio.active) MobileRadio.toggle(); else Player.playPause();
});
miniNext.addEventListener('click', (e) => {
  e.stopPropagation();
  if (MobileRadio.active) return;         // a single live station has no "next"
  Player.next();
});
// The cast button (#btn-cast, cast_picker.js) opens its picker in place.
mini.addEventListener('click', (e) => {
  if (e.target && e.target.closest && e.target.closest('#btn-cast')) return;
  activate('nowplaying');
});

// ── Action sheet ──────────────────────────────────────────────────────────
function showSheet({ title = 'Actions', actions = [] }) {
  sheetTitle.textContent = title;
  sheetList.innerHTML = '';

  actions.forEach(a => {
    const li = document.createElement('li');
    li.textContent = a.label;
    li.setAttribute('role', 'button');       // it IS one: announce it, focus it, Enter/Space it
    li.tabIndex = 0;
    if (a.danger) li.classList.add('danger');
    const choose = () => {
      hideSheet();
      try { a.onSelect(); } catch (err) { console.error(err); }
    };
    li.addEventListener('click', choose);
    li.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(); }
    });
    sheetList.appendChild(li);
  });

  _sheetOpener = document.activeElement;     // focus goes back here when the sheet closes
  _swallowClick = _pressDown;                // (opened while a finger is still down: a long-press)
  sheet.classList.remove('hidden');
  sheetBg.classList.remove('hidden');
  (sheetList.firstElementChild || sheet.querySelector('#m-sheet-cancel'))?.focus?.();
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !sheet.classList.contains('hidden')) hideSheet();
});

let _sheetOpener = null;
function hideSheet() {
  const was = !sheet.classList.contains('hidden');
  if (was) { try { _sheetOpener?.focus?.({ preventScroll: true }); } catch (_) {} _sheetOpener = null; }
  sheet.classList.add('hidden');
  sheetBg.classList.add('hidden');
}

sheetBg.addEventListener('click',     hideSheet);
// A sheet opened by a long-press: the release of that press can arrive as a
// click on whatever now lies under the finger — the backdrop (the sheet closed
// at once) or one of the actions (Chromium's touch emulation does this).  That
// one click is dropped; the next press is the user's.
let _pressDown = false, _swallowClick = false, _swallowTimer = 0;
document.addEventListener('pointerdown', () => {
  _pressDown = true;
  _swallowClick = false;                 // (a new press: its click is the user's)
}, true);
window.addEventListener('pointerup', () => {
  _pressDown = false;
  // No click came after the release (a real phone usually sends none after a
  // long-press): stop waiting for it.
  if (_swallowClick) { clearTimeout(_swallowTimer); _swallowTimer = setTimeout(() => { _swallowClick = false; }, 400); }
}, true);
window.addEventListener('pointercancel', () => { _pressDown = false; _swallowClick = false; }, true);
for (const el of [sheet, sheetBg]) {
  el.addEventListener('click', (e) => {
    if (!_swallowClick) return;
    _swallowClick = false;
    e.stopPropagation(); e.preventDefault();
  }, true);
}
sheetCancel.addEventListener('click', hideSheet);

// ── Toast ─────────────────────────────────────────────────────────────────
// ONE toast surface on the phone: the shared Toast (docked under the top bar by
// mobile.css, taps pass through, identical messages coalesce, screen readers hear
// it).  The old bottom pill put "Shuffle on" over the shuffle button it confirmed
// while errors appeared at the other end of the screen.
function showToast(message, kind = 'info') {
  (Toast[kind] || Toast.info)(String(message));   // 'error' → red, announced at once, stays 5 s
}
// The play queue ran dry because the next page could not be fetched — mobile
// queues are view-backed too, and silence here reads as a crash.
Player.on('queuestall', () => {
  Toast.error('Couldn\u2019t load the next tracks \u2014 check the connection, then press Next.');
});
// Settings → "Tracks on unavailable drives": nothing left to play can play now.
Player.on('offlinequeue', (d) => {
  Toast.info(nothingToPlayText(d && d.reasons));
});
// Playback problems: the one-time skip notice, "clear the broken mark?", the
// "Mark broken" action on a failed play (health.js).
Health.init();

// ── Boot ──────────────────────────────────────────────────────────────────
window.addEventListener('hashchange', () => {
  const hash = (location.hash || '').replace(/^#/, '');
  if (VIEWS[hash]) activate(hash);
});

// Gate the app on a valid session.  Without this the mobile UI mounts over a
// wall of 401s with no way to authenticate: Auth.boot() shows the login overlay
// when there's no valid session cookie.  Importing auth.js also installs the
// global 401 → re-login fetch interceptor, so a mid-session expiry re-prompts.
//
// We POLL ``Auth.user`` rather than ``await Auth.ready`` (which the desktop
// shell does): on mobile a stray /api 401 during boot — cast_picker.js probes
// /api/cast/sessions before the user signs in — trips auth.js's re-auth flow,
// which REPLACES the ready promise.  A single ``await Auth.ready`` would then
// wait on an orphaned promise and hang forever after a successful login (the
// app never mounts).  ``Auth.user`` is set synchronously on sign-in, so polling
// it is immune to that promise swap.
await Auth.boot();
while (!Auth.user) {
  await new Promise(r => setTimeout(r, 120));
}

activate(initialView());
renderMini(Player.currentTrack);
_listenForRestart();
// Follow "Tracks on unavailable drives" and "Tracks that can't be played"
// (marks, hidden rows, auto-play skipping); the views re-read their lists on
// 'soniqboom:availability-changed' (the socket's availability_changed asks).
Offline.init();
