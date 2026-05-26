// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * slave.js — slave view controller.
 * Full-bleed Now Playing card, sync indicator, read-only progress.
 */
import { Player } from '../player.js';
import { Sync } from './sync.js';
import { artPlaceholderEmoji } from '../utils.js';

const $ = (id) => document.getElementById(id);

function fmt(sec) {
  if (!isFinite(sec) || sec < 0) return '0:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}

let _progressTimer = null;

export function enterSlave() {
  document.body.setAttribute('data-view', 'slave');
  $('mr-slave-room-name').textContent = Sync.roomName || 'Room';
  _bindControls();
  _renderCard(null);
  _updateSyncDot(0);

  // Turn off time-stretching so the ±3 % playbackRate nudges used for drift
  // correction don't trigger Safari/WebKit's pitch-preservation pipeline
  // (which causes audible clicks on every rate change on iPad).
  const a = Player.audio;
  try { a.preservesPitch = false; } catch { /* ignore */ }
  try { a.webkitPreservesPitch = false; } catch { /* ignore */ }
  try { a.mozPreservesPitch = false; } catch { /* ignore */ }

  Sync.addEventListener('state', _onState);
  Sync.addEventListener('drift', _onDrift);
  Sync.addEventListener('master_changed', _onMasterChanged);
  Sync.addEventListener('autoplay_blocked', _onAutoplayBlocked);

  // Simple slave-side progress ticker for the UI (not for sync — sync is separate).
  if (_progressTimer) clearInterval(_progressTimer);
  _progressTimer = setInterval(_tickLocalProgress, 250);
}

export function leaveSlave() {
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
  Sync.removeEventListener('state', _onState);
  Sync.removeEventListener('drift', _onDrift);
  Sync.removeEventListener('master_changed', _onMasterChanged);
  Sync.removeEventListener('autoplay_blocked', _onAutoplayBlocked);
}

function _bindControls() {
  $('mr-btn-slave-leave').onclick = () => {
    // Order matters: detach listeners first so a late `state` frame in flight
    // can't retrigger drift correction after the UI has switched views.
    leaveSlave();
    Sync.close();
    try { Player.audio.pause(); Player.audio.currentTime = 0; } catch { /* ignore */ }
    document.body.setAttribute('data-view', 'landing');
    import('./app.js').then(m => m.refreshLanding && m.refreshLanding());
  };
}

function _onState(ev) {
  const state = ev.detail;
  if (!state) return;
  if (!state.track) return;
  // Render card with fresh track info on any relevant change
  _renderCard(state.track);
}

function _onDrift(ev) {
  const d = Math.round(ev.detail.driftMs);
  const abs = Math.abs(d);
  $('mr-sync-drift').textContent = `${d >= 0 ? '+' : ''}${d} ms`;
  _updateSyncDot(abs);
}

function _updateSyncDot(absMs) {
  const dot = $('mr-sync-dot');
  dot.classList.remove('good', 'warn', 'bad');
  if (absMs < 20) dot.classList.add('good');
  else if (absMs < 150) dot.classList.add('warn');
  else dot.classList.add('bad');
}

function _onMasterChanged(ev) {
  const banner = $('mr-slave-banner');
  if (Sync.masterId === null) {
    banner.classList.remove('hidden');
    banner.innerHTML = `Master left the room. <button id="mr-btn-takeover">Become master</button>`;
    $('mr-btn-takeover').onclick = () => {
      Sync.takeMaster();
      // Server will reply with master_changed; app.js routes view transitions.
    };
  } else {
    banner.classList.add('hidden');
    banner.innerHTML = '';
  }
}

function _onAutoplayBlocked() {
  const banner = $('mr-slave-banner');
  banner.classList.remove('hidden');
  banner.innerHTML = `This browser blocked autoplay. <button id="mr-btn-unlock">Tap to start</button>`;
  $('mr-btn-unlock').onclick = () => {
    banner.classList.add('hidden');
    Player.audio.play().catch(() => {});
  };
}

function _renderCard(track) {
  const artEl = $('mr-slave-art');
  if (!track) {
    $('mr-slave-title').textContent = 'Waiting for master…';
    $('mr-slave-artist').textContent = '';
    artEl.innerHTML = '<span class="mr-art-ph">\u{1F50A}</span>';
    return;
  }
  $('mr-slave-title').textContent = track.title || '(Untitled)';
  $('mr-slave-artist').textContent = track.artist || track.album_artist || '';
  // Same format-aware fallback as master.js — see comment there.
  const src = track.cover_art || (track.id ? `/api/art/${track.id}?size=lg` : '');
  artEl.innerHTML = `<span class="mr-art-ph">${artPlaceholderEmoji(track)}</span>` +
    (src ? `<img class="mr-art-img" src="${_esc(src)}" alt="">` : '');
  const _mrImg = artEl.querySelector('.mr-art-img');
  if (_mrImg) {
    _mrImg.onload  = () => _mrImg.classList.add('loaded');
    _mrImg.onerror = () => _mrImg.remove();
  }
  $('mr-slave-dur').textContent = fmt(track.duration || 0);
}

function _tickLocalProgress() {
  // Skip the layout work when the tab is hidden — the user can't see the
  // progress bar anyway, and 4 Hz layout writes survive tab-hide otherwise
  // (Perf #2).
  if (document.hidden) return;
  const cur = Player.audio.currentTime || 0;
  const dur = Player.audio.duration || 0;
  $('mr-slave-cur').textContent = fmt(cur);
  if (dur) $('mr-slave-dur').textContent = fmt(dur);
  const pct = dur ? Math.min(100, (cur / dur) * 100) : 0;
  $('mr-slave-progress-fill').style.width = `${pct}%`;
}

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
