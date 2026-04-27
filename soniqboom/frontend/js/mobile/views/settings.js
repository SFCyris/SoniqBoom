// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * settings.js — Mobile read-only settings: server health, scan dirs.
 * Use the desktop UI at `/` for write actions in v1.
 */
import { esc } from './_common.js';

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
            <span class="value"><a href="https://github.com/SFCyris/SoniqBoom" target="_blank" rel="noopener noreferrer">github.com/SFCyris/SoniqBoom</a></span>
          </div>
        </div>
      </div>
    </div>
  `;

  const statusEl  = root.querySelector('#set-status');
  const versionEl = root.querySelector('#set-version');
  const tracksEl  = root.querySelector('#set-tracks');
  const dirsCard  = root.querySelector('#m-set-dirs');

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
      tracksEl.textContent  = (c.count ?? 0).toLocaleString();

      const dirs = (d && Array.isArray(d.dirs)) ? d.dirs : [];
      if (!dirs.length) {
        dirsCard.innerHTML = `<div class="m-settings-row"><span class="label">No folders configured</span></div>`;
      } else {
        dirsCard.innerHTML = dirs.map(dir => {
          const path  = dir.path || '';
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

  root.addEventListener('viewactive', refresh);
  refresh();
}
