// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * app.js — Mobile shell entry: view router, tab bar, mini-player, action sheet.
 */
import { Player } from '../player.js';
import { Auth }   from '../auth.js';
import { artPlaceholderEmoji, Toast } from '../utils.js';
// UX-3 P0: mobile shell exposes the same globals desktop does so the
// classic-script cast_picker.js can read currentTrackId + emit toasts.
window.SoniqBoom = window.SoniqBoom || {};
window.SoniqBoom.player = Player;
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

import { MobileRadio }     from './radio-player.js';
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

const toast       = document.getElementById('m-toast');

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
  tabs.forEach(t => t.classList.toggle('active', t.dataset.view === viewName));
  topbarTitle.textContent = v.title;

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
function renderMini(track) {
  // Radio takes precedence: while a station plays it's the active source.
  if (MobileRadio.active) { renderMiniStation(MobileRadio.station); return; }
  // Hide when there's nothing, OR when a radio takeover detached the shared
  // element (src cleared) so ``track`` is no longer resumable — otherwise the
  // mini would show a dead ▶ that playPause() ignores after radio stops.
  const resumable = track && (Player.playing || (Player.audio && Player.audio.getAttribute('src')));
  if (!resumable) {
    mini.classList.add('hidden');
    return;
  }
  mini.classList.remove('hidden');
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
  const artSrc = track.cover_art || (track.id ? `/api/art/${track.id}?size=sm&fallback=404` : null);
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

function renderMiniStation(st) {
  mini.classList.remove('hidden');
  miniTitle.textContent  = (st && st.name) || 'Radio';
  miniArtist.textContent = 'Internet radio';
  miniArt.innerHTML = '';
  const span = document.createElement('span');
  span.className = 'm-mp-art-ph';
  span.textContent = '📻';
  miniArt.appendChild(span);
  if (st && st.favicon) {
    const img = new Image();
    img.alt = ''; img.decoding = 'async';
    img.onload  = () => img.classList.add('loaded');
    img.onerror = () => img.remove();
    miniArt.appendChild(img);
    img.src = st.favicon;
  }
  miniProg.style.width = '0%';   // live stream — no meaningful progress
}

Player.on('trackchange', renderMini);
Player.on('statechange', ({ playing }) => {
  if (MobileRadio.active) return;         // radio owns the transport while active
  miniPlay.textContent = playing ? '⏸' : '▶';
});
Player.on('timeupdate', ({ pct }) => {
  if (MobileRadio.active) return;
  miniProg.style.width = `${Math.min(100, Math.max(0, pct))}%`;
});

// Radio drives the mini-player when a station is the active source.
MobileRadio.on('change', () => renderMini(Player.currentTrack));
MobileRadio.on('state',  () => {
  if (MobileRadio.active) miniPlay.textContent = MobileRadio.playing ? '⏸' : '▶';
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
mini.addEventListener('click',     ()  => activate('nowplaying'));

// ── Action sheet ──────────────────────────────────────────────────────────
function showSheet({ title = 'Actions', actions = [] }) {
  sheetTitle.textContent = title;
  sheetList.innerHTML = '';

  actions.forEach(a => {
    const li = document.createElement('li');
    li.textContent = a.label;
    if (a.danger) li.classList.add('danger');
    li.addEventListener('click', () => {
      hideSheet();
      try { a.onSelect(); } catch (err) { console.error(err); }
    });
    sheetList.appendChild(li);
  });

  sheet.classList.remove('hidden');
  sheetBg.classList.remove('hidden');
}

function hideSheet() {
  sheet.classList.add('hidden');
  sheetBg.classList.add('hidden');
}

sheetBg.addEventListener('click',     hideSheet);
sheetCancel.addEventListener('click', hideSheet);

// ── Toast ─────────────────────────────────────────────────────────────────
let _toastTimer = null;
function showToast(message) {
  toast.textContent = message;
  toast.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.add('hidden'), 2000);
}

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
