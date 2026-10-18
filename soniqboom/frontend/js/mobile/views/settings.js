// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * settings.js — Mobile settings: server health, scan dirs (read-only) and the
 * browser-local playback preference.  Use the desktop UI at `/` for admin actions.
 */
import { esc } from './_common.js';
import { Player } from '../../player.js';
import { Auth } from '../../auth.js';
import { Offline } from '../../offline.js';

// One availability listener for whichever settings view is mounted now (a
// view mounted again replaces it, never adds another).
let _paintOffline = null;
document.addEventListener('soniqboom:availability-changed', () => { if (_paintOffline) _paintOffline(); });

export function mountSettings(root, ctx) {
  root.innerHTML = `
    <div class="m-settings">
      <div class="m-settings-section">
        <h3>Server</h3>
        <div class="m-settings-card" id="m-set-server">
          <div class="m-settings-row"><span class="label">Status</span><span class="value" id="set-status">Loading…</span></div>
          <div class="m-settings-row"><span class="label">Version</span><span class="value" id="set-version">—</span></div>
          <div class="m-settings-row"><span class="label">Tracks</span><span class="value" id="set-tracks">—</span></div>
        </div>
      </div>

      <div class="m-settings-section">
        <h3>Music Folders</h3>
        <div class="m-settings-card" id="m-set-dirs">
          <div class="m-settings-row"><span class="label">Loading…</span></div>
        </div>
      </div>

      <div class="m-settings-section">
        <h3>Library</h3>
        <div class="m-settings-card">
          <label class="m-settings-row" for="set-offline-tracks">
            <span class="label"><span id="set-offline-name">Tracks on unavailable drives</span>
              <small id="set-offline-hint">Tracks whose drive or share isn’t connected. Mark: dimmed, with an “offline” badge. Hide: left out of searches and lists (playlists still show them). Show: as usual. Shuffle and the next track skip them in Mark and Hide.</small></span>
            <select id="set-offline-tracks" class="m-settings-select"
                    aria-labelledby="set-offline-name" aria-describedby="set-offline-hint">
              <option value="mark">Mark</option>
              <option value="hide">Hide</option>
              <option value="show">Show</option>
            </select>
          </label>
        </div>
      </div>

      <div class="m-settings-section">
        <h3>Playback</h3>
        <div class="m-settings-card">
          <label class="m-settings-row" for="set-shuffle-replay">
            <span class="label">Let shuffle repeat tracks you’ve heard
              <small>Off: each track plays once per shuffle. This browser only; takes effect the next time you turn shuffle on.</small></span>
            <input type="checkbox" id="set-shuffle-replay">
          </label>
          <label class="m-settings-row" for="set-queue-sync" id="set-queue-sync-row"${Player.queueSyncSupported === false ? ' style="display:none"' : ''}>
            <span class="label">Sync the play queue across devices
              <small>Keeps this browser’s queue on the server for your other devices and Subsonic apps, and offers to resume a newer queue saved elsewhere.</small></span>
            <input type="checkbox" id="set-queue-sync">
          </label>
        </div>
      </div>

      <div class="m-settings-section">
        <h3>About</h3>
        <div class="m-settings-card">
          <div class="m-settings-row column">
            <span class="label">Mobile UI</span>
            <span class="value">Touch-first SoniqBoom shell. Use the desktop UI at <code>/</code> for admin actions, equalizer, and visualizer.</span>
          </div>
          <div class="m-settings-row column">
            <span class="label">License</span>
            <span class="value">AGPL-3.0-or-later &mdash; &copy; 2026 S.F. Cyris. SoniqBoom is intended for streaming music you already own or have the right to use.</span>
          </div>
          <div class="m-settings-row column">
            <span class="label">Source</span>
            <span class="value"><a href="https://github.com/SFCyris/SoniqBoom" target="_blank" rel="noopener noreferrer">github.com/SFCyris/SoniqBoom</a>
              <br><a id="set-source" href="https://github.com/SFCyris/SoniqBoom/releases" target="_blank" rel="noopener noreferrer">Source code of this version</a></span>
          </div>
        </div>
      </div>
    </div>
  `;

  const statusEl  = root.querySelector('#set-status');
  const versionEl = root.querySelector('#set-version');
  const tracksEl  = root.querySelector('#set-tracks');
  const dirsCard  = root.querySelector('#m-set-dirs');
  // Same browser-local preference the desktop Settings page edits; the player
  // reads it at the next shuffle deal.
  const shufReplay = root.querySelector('#set-shuffle-replay');
  shufReplay.checked = Player.shuffleReplaysPlayed;
  shufReplay.addEventListener('change', () => Player.setShuffleReplaysPlayed(shufReplay.checked));
  const qsync = root.querySelector('#set-queue-sync');
  const qsyncRow = root.querySelector('#set-queue-sync-row');
  qsync.checked = Player.queueSync;
  qsync.addEventListener('change', () => Player.setQueueSync(qsync.checked));
  // "Tracks on unavailable drives" — a server setting (every user, like "Hide
  // duplicates"): an admin changes it here or in the desktop Settings; anyone
  // else sees what it is.
  const offSel  = root.querySelector('#set-offline-tracks');
  const offHint = root.querySelector('#set-offline-hint');
  const offHintText = offHint.textContent;
  function paintOffline() {
    const admin = !!Auth.isAdmin;
    if (Offline.mode) offSel.value = Offline.mode;
    offSel.disabled = !admin;
    offHint.textContent = admin ? offHintText : `${offHintText} Set by an admin for every user.`;
  }
  offSel.addEventListener('change', async () => {
    const want = offSel.value;
    try {
      const r = await fetch('/api/admin/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ offline_tracks: want }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await Offline.refresh();
      ctx.toast(want === 'hide' ? 'Tracks on unavailable drives are hidden.'
        : want === 'show' ? 'Tracks on unavailable drives are listed as usual.'
        : 'Tracks on unavailable drives are marked.');
    } catch (_) {
      ctx.toast('Couldn’t save the setting.', 'error');
    }
    paintOffline();
  });
  _paintOffline = paintOffline;
  paintOffline();
  Offline.refresh().finally(paintOffline);

  // The player learns whether the server keeps a queue a few seconds after load:
  // this view may already be up by then.
  Player.on('queuesync', ({ supported }) => { qsyncRow.style.display = supported === false ? 'none' : ''; });

  async function refresh() {
    try {
      const [hRes, cRes, dRes] = await Promise.all([
        fetch('/api/health'),
        fetch('/api/tracks/count'),
        fetch('/api/library/dirs'),
      ]);
      const h = await hRes.json();
      const c = await cRes.json();
      const d = await dRes.json();

      statusEl.textContent  = h.status === 'ok' ? 'Online' : 'Offline';
      versionEl.textContent = h.version || '—';
      if (h.version) {   // AGPL-3.0 §13: the source of the version that is running
        root.querySelector('#set-source').href =
          `https://github.com/SFCyris/SoniqBoom/releases/tag/v${encodeURIComponent(h.version)}`;
      }
      tracksEl.textContent  = (c.visible ?? c.count ?? 0).toLocaleString();   // same number as the desktop badge

      const dirs = (d && Array.isArray(d.dirs)) ? d.dirs : [];
      if (!dirs.length) {
        dirsCard.innerHTML = `<div class="m-settings-row"><span class="label">No folders configured</span></div>`;
      } else {
        dirsCard.innerHTML = dirs.map(dir => {
          // A catalogue root (Settings → Online collections) shows its name.
          const path  = dir.catalogue ? `${dir.label || dir.catalogue} catalogue` : (dir.path || '');
          const stat  = dir.status || 'ok';
          const color = stat === 'ok' ? 'var(--accent)' : 'var(--danger)';
          return `
            <div class="m-settings-row column">
              <span class="value" style="font-size:14px">${esc(path)}</span>
              <span class="label" style="font-size:12px;color:${color}">${esc(stat)}</span>
            </div>
          `;
        }).join('');
      }
    } catch (err) {
      statusEl.textContent = 'Unreachable';
      console.error('Settings refresh failed', err);
    }
  }

  root.addEventListener('viewactive', () => {
    Offline.refresh().finally(paintOffline);
    shufReplay.checked = Player.shuffleReplaysPlayed;
    qsync.checked = Player.queueSync;
    // The server has no queue to sync with: no row (mobile.css has no [hidden] rule).
    qsyncRow.style.display = Player.queueSyncSupported === false ? 'none' : '';
    refresh();
  });
  refresh();
}
