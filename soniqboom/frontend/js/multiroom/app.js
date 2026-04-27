// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * multiroom/app.js — entry point, view router, landing page wiring.
 */
import { Sync } from './sync.js';
import { enterMaster, leaveMaster } from './master.js';
import { enterSlave, leaveSlave } from './slave.js';

const $ = (id) => document.getElementById(id);
const LABEL_KEY = 'sb_mr_label';

let _roomsPollTimer = null;

document.addEventListener('DOMContentLoaded', () => {
  _initLabel();
  _bindLanding();
  refreshLanding();
  _roomsPollTimer = setInterval(refreshLanding, 2000);

  Sync.addEventListener('master_changed', () => {
    // If we just got promoted from slave→master (via take_master)
    if (Sync.role === 'slave' && Sync.masterId === Sync.clientId) {
      Sync.role = 'master';
      leaveSlave();
      enterMaster();
    }
  });
  Sync.addEventListener('welcome', () => {
    if (Sync.role === 'master') {
      if (_roomsPollTimer) { clearInterval(_roomsPollTimer); _roomsPollTimer = null; }
      enterMaster();
    } else {
      if (_roomsPollTimer) { clearInterval(_roomsPollTimer); _roomsPollTimer = null; }
      enterSlave();
    }
  });
  Sync.addEventListener('disconnected', () => {
    _toast('Disconnected — retrying…');
  });
  Sync.addEventListener('sync_error', (ev) => {
    const e = ev.detail;
    if (e.code === 'MASTER_LOCKED') {
      _toast('Room already has a master.');
    } else if (e.code === 'ROOM_GONE') {
      _toast('That room no longer exists.');
      document.body.setAttribute('data-view', 'landing');
      refreshLanding();
    }
  });
});

function _initLabel() {
  const saved = localStorage.getItem(LABEL_KEY) || '';
  if (saved) $('mr-label').value = saved;
  $('mr-label').addEventListener('change', () => {
    localStorage.setItem(LABEL_KEY, $('mr-label').value.trim());
  });
}

function _bindLanding() {
  $('mr-btn-new').onclick = () => {
    document.body.setAttribute('data-view', 'newroom');
    $('mr-newroom-name').focus();
  };
  $('mr-btn-back-landing').onclick = () => {
    document.body.setAttribute('data-view', 'landing');
  };
  $('mr-btn-create').onclick = () => {
    const name = $('mr-newroom-name').value.trim() || 'Room';
    const label = $('mr-label').value.trim() || 'Device';
    localStorage.setItem(LABEL_KEY, label);
    const roomId = (crypto?.randomUUID?.() || `r-${Date.now()}`);
    Sync.connect({ roomId, roomName: name, role: 'master', label }).catch(() => {
      _toast('Could not reach server.');
    });
  };
}

export async function refreshLanding() {
  try {
    const r = await fetch('/api/multiroom/rooms', { cache: 'no-store' });
    if (!r.ok) return;
    const rooms = await r.json();
    const ul = $('mr-rooms-list');
    ul.innerHTML = '';
    if (!rooms.length) {
      ul.innerHTML = '<li class="mr-empty">No active rooms yet.</li>';
      return;
    }
    for (const room of rooms) {
      const li = document.createElement('li');
      const current = room.current_track
        ? `🎵 ${_esc(room.current_track.artist || '')} — ${_esc(room.current_track.title || '')}`
        : '';
      li.innerHTML = `
        <div>
          <div class="mr-room-name">${_esc(room.room_name)}</div>
          <div class="mr-room-meta">
            ${room.client_count} listener(s)${room.has_master ? '' : ' · no master'}
          </div>
        </div>
        <div class="mr-room-current">${current}</div>`;
      li.onclick = () => _joinRoom(room);
      ul.appendChild(li);
    }
  } catch (e) {
    // Silent — panel retains last known content.
  }
}

function _joinRoom(room) {
  const label = $('mr-label').value.trim() || 'Device';
  localStorage.setItem(LABEL_KEY, label);
  Sync.connect({
    roomId:   room.room_id,
    roomName: room.room_name,
    role:     room.has_master ? 'slave' : 'master',
    label,
  }).catch(() => _toast('Could not connect.'));
}

function _toast(msg) {
  const el = $('mr-toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(_toast._t);
  _toast._t = setTimeout(() => el.classList.add('hidden'), 2600);
}

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
