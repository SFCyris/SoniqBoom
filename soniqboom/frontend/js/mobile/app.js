// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * app.js — Mobile shell entry: view router, tab bar, mini-player, action sheet.
 */
import { Player } from '../player.js';
import { artPlaceholderEmoji, Toast } from '../utils.js';
// UX-3 P0: mobile shell exposes the same globals desktop does so the
// classic-script cast_picker.js can read currentTrackId + emit toasts.
window.SoniqBoom = window.SoniqBoom || {};
window.SoniqBoom.player = Player;
window.Toast = Toast;

import { mountLibrary }    from './views/library.js';
import { mountSearch }     from './views/search.js';
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
  queue:      { title: 'Queue',       mount: mountQueue,      el: document.getElementById('m-view-queue') },
  nowplaying: { title: 'Now Playing', mount: mountNowPlaying, el: document.getElementById('m-view-nowplaying') },
  settings:   { title: 'Settings',    mount: mountSettings,   el: document.getElementById('m-view-settings') },
};

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
  if (!track) {
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
    img.onload  = () => img.classList.add('loaded');
    img.onerror = () => img.remove();
    miniArt.appendChild(img);
    img.src = artSrc;
  }
}

Player.on('trackchange', renderMini);
Player.on('statechange', ({ playing }) => {
  miniPlay.textContent = playing ? '⏸' : '▶';
});
Player.on('timeupdate', ({ pct }) => {
  miniProg.style.width = `${Math.min(100, Math.max(0, pct))}%`;
});

miniPlay.addEventListener('click', (e) => { e.stopPropagation(); Player.playPause(); });
miniNext.addEventListener('click', (e) => { e.stopPropagation(); Player.next(); });
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

activate(initialView());
renderMini(Player.currentTrack);
