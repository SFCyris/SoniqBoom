// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * admin.js — Admin panel: OS auth, folder management, scan, export/import.
 */

import { runRestartFlow } from './restart.js';

const overlay        = document.getElementById('admin-overlay');
const authDialog     = document.getElementById('admin-auth-dialog');
const aliasDialog    = document.getElementById('admin-alias-dialog');
const adminPanel     = document.getElementById('admin-panel');
const authError      = document.getElementById('admin-auth-error');
const usernameInput  = document.getElementById('admin-username');
const passwordInput  = document.getElementById('admin-password');

let _token = null;
let _isOpen = false;

// ── Reusable styled confirm dialog (replaces native confirm()) ───────────────

const confirmDialog = document.getElementById('confirm-dialog');
const confirmTitle  = document.getElementById('confirm-dialog-title');
const confirmMsg    = document.getElementById('confirm-dialog-message');
const confirmOk     = document.getElementById('btn-confirm-ok');
const confirmCancel = document.getElementById('btn-confirm-cancel');
let _confirmResolve = null;
let _confirmPreviousDialog = null; // which dialog was showing before confirm

/**
 * Show an in-app styled confirm dialog. Returns a promise that resolves
 * to true (OK) or false (Cancel).
 */
function styledConfirm(message, { title = 'Confirm', okLabel = 'OK', dangerColor = true } = {}) {
  return new Promise((resolve) => {
    _confirmResolve = resolve;
    confirmTitle.textContent = title;
    confirmMsg.textContent = message;
    confirmOk.textContent = okLabel;
    confirmOk.className = dangerColor ? 'btn-danger-action' : 'btn-primary';
    // Track which dialog is currently visible so we can restore it
    _confirmPreviousDialog = null;
    if (!adminPanel.classList.contains('hidden')) {
      _confirmPreviousDialog = 'panel';
      adminPanel.classList.add('hidden');
    } else if (!authDialog.classList.contains('hidden')) {
      _confirmPreviousDialog = 'auth';
      authDialog.classList.add('hidden');
    } else if (!aliasDialog.classList.contains('hidden')) {
      _confirmPreviousDialog = 'alias';
      aliasDialog.classList.add('hidden');
    }
    confirmDialog.classList.remove('hidden');
    overlay.classList.remove('hidden');
    confirmOk.focus();
  });
}

function _closeConfirmDialog(result) {
  confirmDialog.classList.add('hidden');
  // Restore the previous dialog, or hide overlay if confirm was standalone
  if (_confirmPreviousDialog === 'panel') adminPanel.classList.remove('hidden');
  else if (_confirmPreviousDialog === 'auth') authDialog.classList.remove('hidden');
  else if (_confirmPreviousDialog === 'alias') aliasDialog.classList.remove('hidden');
  else overlay.classList.add('hidden'); // no parent dialog — hide overlay entirely
  _confirmPreviousDialog = null;
  if (_confirmResolve) { _confirmResolve(result); _confirmResolve = null; }
}

confirmOk.addEventListener('click', (e) => { e.stopPropagation(); _closeConfirmDialog(true); });
confirmCancel.addEventListener('click', (e) => { e.stopPropagation(); _closeConfirmDialog(false); });

// ── Auth ──────────────────────────────────────────────────────────────────────

function _hideAllDialogs() {
  authDialog.classList.add('hidden');
  adminPanel.classList.add('hidden');
  aliasDialog.classList.add('hidden');
  confirmDialog.classList.add('hidden');
}

async function open() {
  // Toggle: if already open, close it
  if (_isOpen) { close(); return; }
  _isOpen = true;

  // Reset all child dialogs to a clean state
  _hideAllDialogs();

  const skipLocal = localStorage.getItem('sb_skip_auth') === '1';
  if (skipLocal) {
    // Tell the server to skip auth too, then open the panel directly.
    try {
      await fetch('/api/admin/auth/skip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled: true }),
      });
    } catch { /* best effort */ }
    _token = '__skip__';
    adminPanel.classList.remove('hidden');
    overlay.classList.remove('hidden');
    // Load data (non-blocking — panel is already visible)
    loadStats();
    loadDirs();
    loadSettings();   // sync scan-zips checkbox with global setting
    loadRendererStatus();
    loadSoundfonts();
    startScanPoller();
    return;
  }
  authError.textContent = '';
  usernameInput.value = '';
  passwordInput.value = '';
  authDialog.classList.remove('hidden');
  overlay.classList.remove('hidden');
  usernameInput.focus();
}

function close() {
  _isOpen = false;
  _hideAllDialogs();
  overlay.classList.add('hidden');
  stopScanPoller();
  // Cancel any pending confirm dialog
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
}

document.getElementById('btn-admin-cancel').addEventListener('click', close);
document.getElementById('btn-admin-close').addEventListener('click', close);
overlay.addEventListener('click', (e) => {
  if (e.target !== overlay) return;
  // If confirm dialog is open, treat background click as cancel
  if (!confirmDialog.classList.contains('hidden')) { _closeConfirmDialog(false); return; }
  close();
});

// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.admin-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.admin-tab-pane').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    const pane = document.getElementById(tab.dataset.tab);
    if (pane) pane.classList.add('active');
    // Load tab-specific data on switch
    if (tab.dataset.tab === 'tab-system') {
      loadDiskUsage();
      loadSettings();
    } else if (tab.dataset.tab === 'tab-log') {
      loadLogs();
    }
  });
});

document.getElementById('btn-admin-login').addEventListener('click', login);
passwordInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });

async function login() {
  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  if (!username || !password) {
    authError.textContent = 'Username and password are required.';
    return;
  }
  authError.textContent = 'Verifying...';

  try {
    const res = await fetch('/api/admin/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      authError.textContent = data.detail || 'Authentication failed.';
      passwordInput.value = '';
      return;
    }
    const { token } = await res.json();
    _token = token;
    authDialog.classList.add('hidden');
    adminPanel.classList.remove('hidden');
    // Load data (non-blocking — panel is already visible)
    loadStats();
    loadDirs();
    loadSettings();   // sync scan-zips checkbox with global setting
    loadRendererStatus();
    loadSoundfonts();
    startScanPoller();
  } catch {
    authError.textContent = 'Network error — is the server running?';
  }
}

// ── API helper ────────────────────────────────────────────────────────────────

function api(path, opts = {}) {
  const isFormData = opts.body instanceof FormData;
  return fetch(`/api${path}`, {
    ...opts,
    headers: {
      ...(!isFormData ? { 'Content-Type': 'application/json' } : {}),
      'X-Admin-Token': _token,
      ...(opts.headers || {}),
    },
  });
}

// ── Stats ─────────────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const res = await api('/admin/stats');
    const s = await res.json();
    document.getElementById('stat-tracks').textContent = s.track_count ?? '—';
    document.getElementById('stat-dirs').textContent   = s.dir_count ?? '—';
    const idxEl = document.getElementById('stat-index');
    idxEl.textContent = s.index_ok ? 'OK' : 'ERROR';
    idxEl.className   = 'stat-value ' + (s.index_ok ? 'stat-ok' : 'stat-warn');
    document.getElementById('stat-docs').textContent = s.index_docs ?? '—';
  } catch { /* non-fatal */ }
}

// ── Dirs ──────────────────────────────────────────────────────────────────────

async function loadDirs() {
  const list = document.getElementById('admin-dir-list');
  list.innerHTML = '<span style="color:var(--text2);font-size:12px">Loading...</span>';
  try {
    // Fetch dirs and scan status in parallel
    const [dirsRes, statusRes] = await Promise.all([
      api('/admin/dirs'),
      api('/admin/scan/status'),
    ]);
    const { dirs } = await dirsRes.json();
    const scanStatus = await statusRes.json().catch(() => ({}));
    const scanActive = !!(scanStatus.running || scanStatus.embedding);
    list.innerHTML = '';
    if (!dirs.length) {
      list.innerHTML = '<span style="color:var(--text2);font-size:12px">No folders added yet.</span>';
      return;
    }
    dirs.forEach(d => renderDirRow(list, d, scanActive));
  } catch {
    list.innerHTML = '<span style="color:#e55;font-size:12px">Failed to load.</span>';
  }
}

function renderDirRow(list, d, scanActive = false) {
  const alreadyIndexed = (d.track_count ?? 0) > 0;
  const isNetwork = !!d.network_share_id;
  const isUnavailable = isNetwork && d.status === 'unavailable';
  const row = document.createElement('div');
  row.className = 'admin-dir-row' + (isUnavailable ? ' dir-unavailable' : '');
  row.dataset.path = d.path;

  const btnLabel = alreadyIndexed ? 'Re-Index' : 'Index';
  const btnClass = alreadyIndexed ? 'btn-reindex-small' : 'btn-index-small';

  const aliases = (window.__sbConfig && window.__sbConfig.folder_aliases) || {};
  const alias = aliases[d.path] || '';
  const aliasStr = alias ? ` [${alias}]` : '';

  const statusDot = isNetwork
    ? `<span class="dir-status-dot ${isUnavailable ? 'dot-red' : 'dot-green'}" title="${isUnavailable ? 'Unavailable' : 'Connected'}"></span>`
    : '';

  row.innerHTML = `
    ${statusDot}
    <span class="admin-dir-path" title="${esc(d.path)}">${esc(d.path)}${esc(aliasStr)}</span>
    <span class="admin-dir-count">${d.track_count ?? 0} tracks</span>
    <button class="btn-alias-edit" data-path="${esc(d.path)}">Alias</button>
    ${isUnavailable ? `<button class="btn-reconnect" data-share="${esc(d.network_share_id)}">Reconnect</button>` : ''}
    <button class="btn-index ${btnClass}"
            data-path="${esc(d.path)}"${isUnavailable ? ' disabled' : ''}>
      ${btnLabel}
    </button>
    <button class="btn-danger" data-path="${esc(d.path)}">${isNetwork ? 'Disconnect' : 'Remove'}</button>`;

  row.querySelector('.btn-alias-edit').addEventListener('click', (e) => {
    e.stopPropagation();
    const current = aliases[d.path] || '';
    openAliasDialog(d.path, current);
  });
  const reconnectBtn = row.querySelector('.btn-reconnect');
  if (reconnectBtn) {
    reconnectBtn.addEventListener('click', () => reconnectShare(d.network_share_id, reconnectBtn));
  }
  row.querySelector('.btn-index').addEventListener('click', () => scanDir(d.path, row));
  const dangerBtn = row.querySelector('.btn-danger');
  dangerBtn.addEventListener('click', async () => {
    dangerBtn.disabled = true;
    try {
      if (isNetwork) await removeShare(d.network_share_id, d.path);
      else await removeDir(d.path);
    } finally {
      dangerBtn.disabled = false;
    }
  });
  list.appendChild(row);
}

async function scanDir(path, row) {
  const btn = row.querySelector('.btn-index');
  btn.textContent = 'Scanning\u2026';
  btn.disabled = true;
  showMsg('admin-dir-msg', `Scanning ${path}\u2026`, 'ok');

  try {
    const res = await api('/admin/scan', {
      method: 'POST',
      body: JSON.stringify({ dirs: [path] }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showMsg('admin-dir-msg', data.detail || 'Scan failed.', 'err');
      btn.textContent = 'Re-Index';
      btn.disabled = false;
      return;
    }
    // Poll in the background — don't block the UI
    pollScanDone(() => {
      btn.textContent = 'Re-Index';
      btn.disabled = false;
      btn.classList.remove('btn-index-small');
      btn.classList.add('btn-reindex-small');
      document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
      loadDirs();
      loadStats();
      showMsg('admin-dir-msg', `Scan complete for ${path}.`, 'ok');
    });
  } catch {
    showMsg('admin-dir-msg', 'Network error.', 'err');
    btn.textContent = 'Re-Index';
    btn.disabled = false;
  }
}

// ── Scan Progress Poller ──────────────────────────────────────────────────────

let _pollTimer = null;
const _scanDoneCallbacks = [];

function pollScanDone(onDone) {
  if (onDone) _scanDoneCallbacks.push(onDone);
  startScanPoller();
}

function startScanPoller() {
  if (_pollTimer) return; // already polling
  _pollTimer = setInterval(pollScanStatus, 2500);
  pollScanStatus(); // immediate first check
}

function stopScanPoller() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function pollScanStatus() {
  const section = document.getElementById('admin-scan-section');
  try {
    const res = await api('/admin/scan/status');
    const s = await res.json();

    const hasQueue = (s.current_dirs || []).length > 0 || (s.queue_depth || 0) > 0;
    const active = s.running || s.embedding || hasQueue;

    if (active) {
      // Show progress section
      if (section) section.style.display = '';

      // Progress bar
      const fill = document.getElementById('scan-progress-fill');
      if (fill) fill.style.width = (s.running ? s.pct : 0) + '%';

      // Text
      const txt = document.getElementById('scan-progress-text');
      if (s.running) {
        txt.textContent = `${s.pct}% \u2014 ${s.processed.toLocaleString()} / ${s.total.toLocaleString()} files`;
      } else {
        txt.textContent = 'Discovering files\u2026';
      }

      // Current file
      const fileEl = document.getElementById('scan-progress-file');
      if (fileEl) fileEl.textContent = s.current_file || '';

      // Queue list
      const qList = document.getElementById('scan-queue-list');
      if (qList) {
        let html = '';
        // Currently scanning dirs
        (s.current_dirs || []).forEach(d => {
          html += `<div class="scan-queue-item"><span class="sq-icon active">\u25B6</span><span class="sq-path">${esc(d)}</span></div>`;
        });
        // Queued dirs
        (s.queued || []).forEach(dirs => {
          dirs.forEach(d => {
            html += `<div class="scan-queue-item"><span class="sq-icon queued">\u23F3</span><span class="sq-path">${esc(d)}</span></div>`;
          });
        });
        qList.innerHTML = html;
      }
    } else {
      // Scan finished
      if (section) section.style.display = 'none';
      stopScanPoller();
      // Fire all done callbacks
      while (_scanDoneCallbacks.length) {
        const cb = _scanDoneCallbacks.shift();
        try { cb(); } catch { /* ignore */ }
      }
    }
  } catch { /* ignore network errors during polling */ }
}

let _removingDir = false;

async function removeDir(path) {
  if (_removingDir) return;  // guard against double-click
  const confirmed = await styledConfirm(
    `Remove "${path}" from library?\n\nPress OK to also delete all indexed tracks from this folder.`,
    { title: 'Remove Folder', okLabel: 'Remove' }
  );
  if (!confirmed) return;  // user cancelled
  _removingDir = true;
  showMsg('admin-dir-msg', '');
  try {
    const res = await api('/admin/dirs', {
      method: 'DELETE',
      body: JSON.stringify({ path, purge_tracks: true }),
    });
    const data = await res.json();
    if (!res.ok) { showMsg('admin-dir-msg', data.detail || 'Error', 'err'); return; }
    const n = data.tracks_deleted ?? 0;
    showMsg('admin-dir-msg', `Removed.${n ? ` ${n} tracks deleted.` : ''}`, 'ok');
    document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
    await loadDirs();
    await loadStats();
  } catch { showMsg('admin-dir-msg', 'Network error.', 'err'); }
  finally { _removingDir = false; }
}

let _removingShare = false;

async function removeShare(shareId, path) {
  if (_removingShare) return;  // guard against double-click
  const confirmed = await styledConfirm(
    `Disconnect "${path}"?\n\nPress OK to also delete all indexed tracks from this share.`,
    { title: 'Disconnect Share', okLabel: 'Disconnect' }
  );
  if (!confirmed) return;  // user cancelled
  _removingShare = true;
  showMsg('admin-dir-msg', '');
  try {
    const res = await api('/admin/shares', {
      method: 'DELETE',
      // Pass scan_root so the backend can clean up an orphaned scan_dir
      // even if its share record is missing from conf.
      body: JSON.stringify({ id: shareId, scan_root: path, purge_tracks: true }),
    });
    const data = await res.json();
    if (!res.ok) { showMsg('admin-dir-msg', data.detail || 'Error', 'err'); return; }
    showMsg('admin-dir-msg', 'Disconnected. Tracks purging in background.', 'ok');
    document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
    await loadDirs();
    await loadStats();
  } catch { showMsg('admin-dir-msg', 'Network error.', 'err'); }
  finally { _removingShare = false; }
}

document.getElementById('btn-admin-add-dir').addEventListener('click', async () => {
  const addBtn = document.getElementById('btn-admin-add-dir');
  const path = document.getElementById('admin-add-path').value.trim();
  if (!path) return;
  // Guard against double-clicks
  addBtn.disabled = true;
  showMsg('admin-dir-msg', '');
  try {
    const scanZips = document.getElementById('admin-scan-zips')?.checked ?? true;
    const alias = document.getElementById('admin-add-alias')?.value?.trim() || '';
    const res = await api('/admin/dirs', {
      method: 'POST',
      body: JSON.stringify({ path, scan_zips: scanZips, alias }),
    });
    const data = await res.json();
    if (!res.ok) { showMsg('admin-dir-msg', data.detail || 'Error', 'err'); addBtn.disabled = false; return; }
    document.getElementById('admin-add-path').value = '';
    if (document.getElementById('admin-add-alias')) document.getElementById('admin-add-alias').value = '';
    showMsg('admin-dir-msg', `Folder added: ${path}`, 'ok');
    document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
    await loadDirs();
    await loadStats();
    addBtn.disabled = false;
    // Start progress poller — it will refresh when done
    pollScanDone(() => {
      document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
      loadDirs();
      loadStats();
    });
  } catch { showMsg('admin-dir-msg', 'Network error.', 'err'); addBtn.disabled = false; }
});

// ── Rebuild Index (full rescan of all dirs) ────────────────────────────────────

document.getElementById('btn-admin-reindex').addEventListener('click', async () => {
  const btn = document.getElementById('btn-admin-reindex');
  btn.textContent = 'Rebuilding...';
  btn.disabled = true;
  showMsg('admin-index-msg', 'Rebuilding schema and scanning all folders...', 'ok');
  try {
    const res = await api('/admin/reindex', { method: 'POST' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showMsg('admin-index-msg', data.detail || 'Reindex failed.', 'err');
      btn.textContent = 'Rebuild Index';
      btn.disabled = false;
      return;
    }
    const data = await res.json();
    if (data.scanning) {
      showMsg('admin-index-msg', `Schema rebuilt. Scanning ${data.dirs.length} folder(s)\u2026`, 'ok');
      pollScanDone(() => {
        btn.textContent = '\u21B5 Rebuild Index';
        btn.disabled = false;
        document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
        loadStats();
        loadDirs();
        showMsg('admin-index-msg', 'Rebuild complete.', 'ok');
      });
    } else {
      showMsg('admin-index-msg', 'Schema rebuilt. No folders registered to scan.', 'ok');
      btn.textContent = 'Rebuild Index';
      btn.disabled = false;
      await loadStats();
    }
  } catch {
    showMsg('admin-index-msg', 'Network error.', 'err');
    btn.textContent = 'Rebuild Index';
    btn.disabled = false;
  }
});

// ── Export / Import ───────────────────────────────────────────────────────────

document.getElementById('btn-admin-export').addEventListener('click', async () => {
  showMsg('admin-io-msg', 'Exporting...', 'ok');
  try {
    const res = await fetch('/api/admin/export', {
      headers: { 'X-Admin-Token': _token },
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      showMsg('admin-io-msg', d.detail || 'Export failed.', 'err');
      return;
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `soniqboom-${new Date().toISOString().slice(0, 10)}.sbz`;
    a.click();
    URL.revokeObjectURL(url);
    showMsg('admin-io-msg', 'Export downloaded.', 'ok');
  } catch { showMsg('admin-io-msg', 'Network error.', 'err'); }
});

document.getElementById('btn-admin-import-trigger').addEventListener('click', () => {
  document.getElementById('admin-import-file').click();
});

document.getElementById('admin-import-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const ok = await styledConfirm(
    `Import "${file.name}"? This will overwrite existing data.`,
    { title: 'Import Backup', okLabel: 'Import' }
  );
  if (!ok) return;

  showMsg('admin-io-msg', 'Importing...', 'ok');
  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/admin/import', {
      method: 'POST',
      headers: { 'X-Admin-Token': _token },
      body: formData,
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      showMsg('admin-io-msg', `Import complete: ${data.imported ?? '?'} keys restored.`, 'ok');
      await loadStats();
      await loadDirs();
    } else {
      showMsg('admin-io-msg', data.detail || 'Import failed.', 'err');
    }
  } catch { showMsg('admin-io-msg', 'Network error.', 'err'); }
  e.target.value = '';
});

// ── Renderer Status ──────────────────────────────────────────────────────────

async function loadRendererStatus() {
  const names = ['ffmpeg', 'sidplayfp', 'fluidsynth', 'openmpt123'];
  // Set all to loading state
  names.forEach(n => {
    document.getElementById(`renderer-icon-${n}`).textContent = '\u2026';
    document.getElementById(`renderer-icon-${n}`).className = 'renderer-icon';
  });

  try {
    const res = await api('/admin/renderers');
    const data = await res.json();
    names.forEach(n => {
      const info = data[n];
      const iconEl = document.getElementById(`renderer-icon-${n}`);
      if (info && info.installed) {
        iconEl.textContent = '\u2713';
        iconEl.className = 'renderer-icon renderer-ok';
        iconEl.title = info.path || '';
      } else {
        iconEl.textContent = '\u2717';
        iconEl.className = 'renderer-icon renderer-missing';
        iconEl.title = 'Not found';
      }
    });
  } catch { /* non-fatal */ }
}

// ── Soundfont Management ─────────────────────────────────────────────────────

const KNOWN_SOUNDFONTS = [
  {
    name: "GeneralUser_GS.sf2",
    label: "GeneralUser GS",
    description: "Best size/quality ratio. Excellent all-around GM soundfont.",
    size: "30 MB",
    url: "https://www.dropbox.com/s/4x27l49kxcwamp5/GeneralUser_GS_v1.471.sf2?dl=1",
    license: "Free (attribution)",
  },
  {
    name: "FluidR3_GM.sf2",
    label: "FluidR3 GM",
    description: "Ships with FluidSynth. Solid general MIDI soundfont.",
    size: "141 MB",
    url: "https://sourceforge.net/projects/pianobooster/files/pianobooster/1.0.0/FluidR3_GM.sf2/download",
    license: "MIT",
  },
  {
    name: "MuseScore_General.sf2",
    label: "MuseScore General",
    description: "Highest quality. Rich, natural instrument samples from MuseScore.",
    size: "350 MB",
    url: "https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/MuseScore_General.sf2",
    license: "MIT",
  },
  {
    name: "Timbres_of_Heaven.sf2",
    label: "Timbres of Heaven",
    description: "Lush orchestral sounds with excellent pianos and strings. Vorbis-compressed.",
    size: "15 MB",
    url: "https://archive.org/download/toh-gmgsxg/Timbres%20Of%20Heaven%20GM_GS_XG_SFX%20V%203.4%20Final_Vorbis.sf2",
    license: "Free",
  },
];

async function loadSoundfonts() {
  const list = document.getElementById('admin-sf-list');
  list.innerHTML = '<span style="color:var(--text2);font-size:12px">Loading...</span>';

  try {
    const res = await api('/admin/soundfonts');
    const { soundfonts, active } = await res.json();
    list.innerHTML = '';

    if (!soundfonts.length) {
      list.innerHTML = '<span style="color:var(--text2);font-size:12px">No soundfonts installed.</span>';
    } else {
      soundfonts.forEach(sf => {
        const row = document.createElement('div');
        row.className = 'admin-sf-row' + (sf.active ? ' sf-active' : '');

        row.innerHTML = `
          <button class="sf-select-btn${sf.active ? ' sf-selected' : ''}"
                  title="${sf.active ? 'Active' : 'Click to activate'}">${sf.active ? '\u25C9' : '\u25CB'}</button>
          <span class="sf-name">${esc(sf.name)}</span>
          <span class="sf-size">${formatSize(sf.size)}</span>
          <button class="btn-danger sf-delete" title="Delete">\u00d7</button>`;

        row.querySelector('.sf-select-btn').addEventListener('click', () => setActiveSoundfont(sf.name));
        row.querySelector('.sf-delete').addEventListener('click', () => deleteSoundfont(sf.name));
        list.appendChild(row);
      });
    }

    // Update known soundfonts list (mark already-installed ones)
    renderKnownSoundfonts(soundfonts.map(s => s.name));
  } catch {
    list.innerHTML = '<span style="color:#e55;font-size:12px">Failed to load soundfonts.</span>';
  }
}

function renderKnownSoundfonts(installedNames) {
  const container = document.getElementById('admin-sf-known-list');
  container.innerHTML = '';

  KNOWN_SOUNDFONTS.forEach(sf => {
    const installed = installedNames.includes(sf.name);
    const row = document.createElement('div');
    row.className = 'sf-known-row';
    row.innerHTML = `
      <div class="sf-known-info">
        <span class="sf-known-label">${esc(sf.label)}</span>
        <span class="sf-known-desc">${esc(sf.description)}</span>
        <span class="sf-known-meta">${esc(sf.size)} &middot; ${esc(sf.license)}</span>
      </div>
      <button class="sf-known-btn${installed ? ' sf-known-installed' : ''}"
              ${installed ? 'disabled' : ''}
              data-name="${esc(sf.name)}"
              data-url="${esc(sf.url)}">${installed ? '\u2713 Installed' : '\u2193 Download'}</button>`;

    if (!installed) {
      row.querySelector('.sf-known-btn').addEventListener('click', () => {
        downloadKnownSoundfont(sf.name, sf.url, row.querySelector('.sf-known-btn'));
      });
    }
    container.appendChild(row);
  });
}

async function setActiveSoundfont(name) {
  showMsg('admin-sf-msg', '');
  try {
    const res = await api('/admin/soundfonts/active', {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      showMsg('admin-sf-msg', d.detail || 'Failed to set active soundfont.', 'err');
      return;
    }
    showMsg('admin-sf-msg', `Active soundfont set to ${name}.`, 'ok');
    await loadSoundfonts();
  } catch {
    showMsg('admin-sf-msg', 'Network error.', 'err');
  }
}

async function deleteSoundfont(name) {
  const ok = await styledConfirm(`Delete soundfont "${name}"?`, { title: 'Delete Soundfont', okLabel: 'Delete' });
  if (!ok) return;
  showMsg('admin-sf-msg', '');
  try {
    const res = await api(`/admin/soundfonts/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      showMsg('admin-sf-msg', d.detail || 'Delete failed.', 'err');
      return;
    }
    showMsg('admin-sf-msg', `Deleted ${name}.`, 'ok');
    await loadSoundfonts();
  } catch {
    showMsg('admin-sf-msg', 'Network error.', 'err');
  }
}

async function uploadSoundfont(file) {
  showMsg('admin-sf-msg', `Uploading ${file.name}...`, 'ok');
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await api('/admin/soundfonts/upload', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showMsg('admin-sf-msg', data.detail || 'Upload failed.', 'err');
      return;
    }
    showMsg('admin-sf-msg', `Uploaded ${data.name} (${formatSize(data.size)}).`, 'ok');
    await loadSoundfonts();
  } catch {
    showMsg('admin-sf-msg', 'Network error.', 'err');
  }
}

async function downloadKnownSoundfont(name, url, btn) {
  const orig = btn.textContent;
  btn.textContent = 'Downloading\u2026';
  btn.disabled = true;
  showMsg('admin-sf-msg', `Downloading ${name}... This may take a while.`, 'ok');

  try {
    const res = await api('/admin/soundfonts/download', {
      method: 'POST',
      body: JSON.stringify({ name, url }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showMsg('admin-sf-msg', data.detail || 'Download failed.', 'err');
      btn.textContent = orig;
      btn.disabled = false;
      return;
    }
    showMsg('admin-sf-msg', `Downloaded ${data.name} (${formatSize(data.size)}).`, 'ok');
    btn.textContent = '\u2713 Installed';
    btn.classList.add('sf-known-installed');
    await loadSoundfonts();
  } catch {
    showMsg('admin-sf-msg', 'Network error.', 'err');
    btn.textContent = orig;
    btn.disabled = false;
  }
}

// Wire up upload button
document.getElementById('btn-sf-upload').addEventListener('click', () => {
  document.getElementById('sf-upload-file').click();
});
document.getElementById('sf-upload-file').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (file) uploadSoundfont(file);
  e.target.value = '';
});

function formatSize(bytes) {
  if (typeof bytes !== 'number' || bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return size.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}

// ── Alias Dialog ─────────────────────────────────────────────────────────────

const aliasInput  = document.getElementById('alias-dialog-input');
let _aliasPath = '';

function openAliasDialog(path, current) {
  _aliasPath = path;
  // Hide all other dialogs, show alias dialog
  _hideAllDialogs();
  aliasDialog.classList.remove('hidden');
  overlay.classList.remove('hidden');
  document.getElementById('alias-dialog-path').textContent = path;
  aliasInput.value = current;
  setTimeout(() => aliasInput.focus(), 50);
}

function closeAliasDialog() {
  aliasDialog.classList.add('hidden');
  adminPanel.classList.remove('hidden');
}

document.getElementById('btn-alias-cancel').addEventListener('click', (e) => {
  e.stopPropagation();
  closeAliasDialog();
});
document.getElementById('btn-alias-save').addEventListener('click', (e) => {
  e.stopPropagation();
  saveAlias();
});
aliasInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') saveAlias();
  if (e.key === 'Escape') closeAliasDialog();
});

async function saveAlias() {
  const newAlias = aliasInput.value.trim();
  closeAliasDialog();
  await api('/admin/dirs/alias', {
    method: 'PATCH',
    body: JSON.stringify({ path: _aliasPath, alias: newAlias }),
  });
  document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
  loadDirs();
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showMsg(id, text, type = 'ok') {
  const el = document.getElementById(id);
  if (!text) { el.style.display = 'none'; return; }
  el.className = `admin-msg ${type}`;
  el.textContent = text;
  el.style.display = 'block';
}

// ── Disk Usage ──────────────────────────────────────────────────────────────

async function loadDiskUsage() {
  // Show loading state
  document.getElementById('disk-data').textContent = '...';
  document.getElementById('disk-art').textContent = '...';
  document.getElementById('disk-sf').textContent = '...';
  document.getElementById('disk-remote').textContent = '...';
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    const res = await api('/admin/disk-usage', { signal: controller.signal });
    clearTimeout(timer);
    const d = await res.json();
    const fmt = (v) => (v == null || v < 0) ? 'N/A' : formatSize(v);
    document.getElementById('disk-data').textContent = fmt(d.data_dir);
    document.getElementById('disk-art').textContent = fmt(d.art_cache);
    document.getElementById('disk-sf').textContent = fmt(d.soundfonts);
    // Remote cache: show usage vs limit
    const rcEl = document.getElementById('disk-remote');
    if (d.remote_cache != null && d.remote_cache >= 0) {
      const used = formatSize(d.remote_cache);
      const limit = formatSize((d.remote_cache_max_mb || 2048) * 1024 * 1024);
      rcEl.textContent = `${used} / ${limit}`;
    } else {
      rcEl.textContent = '—';
    }
  } catch {
    document.getElementById('disk-data').textContent = '—';
    document.getElementById('disk-art').textContent = '—';
    document.getElementById('disk-sf').textContent = '—';
    document.getElementById('disk-remote').textContent = '—';
  }
}

// ── Cache Management ────────────────────────────────────────────────────────

document.getElementById('btn-clear-art-cache')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Clear all cached artwork? Thumbnails will be regenerated on demand.',
    { title: 'Clear Art Cache', okLabel: 'Clear' }
  );
  if (!ok) return;
  showMsg('admin-cache-msg', 'Clearing art cache...', 'ok');
  try {
    const res = await api('/admin/cache/clear-art', { method: 'POST' });
    const d = await res.json();
    showMsg('admin-cache-msg', `Cleared ${d.cleared} files.`, 'ok');
    loadDiskUsage();
  } catch { showMsg('admin-cache-msg', 'Error clearing cache.', 'err'); }
});

document.getElementById('btn-clear-waveforms')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Clear all waveform data? Waveforms will be regenerated on next scan.',
    { title: 'Clear Waveforms', okLabel: 'Clear' }
  );
  if (!ok) return;
  showMsg('admin-cache-msg', 'Clearing waveforms...', 'ok');
  try {
    const res = await api('/admin/cache/clear-waveforms', { method: 'POST' });
    const d = await res.json();
    showMsg('admin-cache-msg', `Cleared ${d.cleared} waveforms.`, 'ok');
    loadDiskUsage();
  } catch { showMsg('admin-cache-msg', 'Error clearing waveforms.', 'err'); }
});

document.getElementById('btn-clear-agg-cache')?.addEventListener('click', async () => {
  try {
    await api('/admin/cache/clear-aggregations', { method: 'POST' });
    showMsg('admin-cache-msg', 'Aggregation cache cleared.', 'ok');
  } catch { showMsg('admin-cache-msg', 'Error.', 'err'); }
});

document.getElementById('btn-clear-remote-cache')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Clear all cached remote audio files? Playback of remote tracks will require re-downloading.',
    { title: 'Clear Remote Cache', okLabel: 'Clear' }
  );
  if (!ok) return;
  showMsg('admin-cache-msg', 'Clearing remote cache...', 'ok');
  try {
    const res = await api('/admin/cache/clear-remote', { method: 'POST' });
    const d = await res.json();
    showMsg('admin-cache-msg', `Cleared ${d.cleared} cached files.`, 'ok');
    loadDiskUsage();
  } catch { showMsg('admin-cache-msg', 'Error clearing remote cache.', 'err'); }
});

// ── Settings ────────────────────────────────────────────────────────────────

async function loadSettings() {
  const xfadeEl   = document.getElementById('setting-crossfade');
  const preloadEl = document.getElementById('setting-preload-buffer');
  const delayEl   = document.getElementById('setting-convert-delay');
  const skipEl    = document.getElementById('setting-skip-auth');
  if (xfadeEl)   xfadeEl.value   = localStorage.getItem('sb_crossfade')      || '0';
  if (preloadEl) preloadEl.value = localStorage.getItem('sb_preload_buffer') || '5';
  if (delayEl)   delayEl.value   = localStorage.getItem('sb_convert_delay')  || '300';
  if (skipEl)    skipEl.checked  = localStorage.getItem('sb_skip_auth') === '1';
  // Populate the About panel each time settings load (cheap, idempotent)
  loadAbout();
  try {
    const res = await api('/admin/settings');
    const s = await res.json();
    const zipEl = document.getElementById('setting-scan-zips');
    if (zipEl) zipEl.checked = s.scan_zips !== false;
    // Keep the add-dir form checkbox in sync with the global setting
    const addZipEl = document.getElementById('admin-scan-zips');
    if (addZipEl) addZipEl.checked = s.scan_zips !== false;
    const sidEl = document.getElementById('setting-sid-duration');
    if (sidEl) sidEl.value = s.renderers?.sid_default_duration || 180;
    const dupEl = document.getElementById('setting-filter-duplicates');
    if (dupEl) dupEl.checked = !!s.filter_duplicates;
    const folderArtEl = document.getElementById('setting-use-folder-art');
    if (folderArtEl) folderArtEl.checked = s.use_folder_art !== false;
    const rcMbEl = document.getElementById('setting-remote-cache-mb');
    if (rcMbEl) rcMbEl.value = s.remote_cache_max_mb || 2048;
  } catch { /* non-fatal */ }
}

/**
 * Populate the About panel. Version comes from /api/health which is the
 * single source of truth (mirrors soniqboom.__version__).
 */
async function loadAbout() {
  const verEl = document.getElementById('about-version');
  if (!verEl) return;
  try {
    const res = await fetch('/api/health');
    const j   = await res.json();
    verEl.textContent = j.version || 'unknown';
  } catch {
    verEl.textContent = 'unknown';
  }
}

document.getElementById('btn-save-settings')?.addEventListener('click', async () => {
  const xfade   = parseFloat(document.getElementById('setting-crossfade')?.value      || '0');
  const preload = parseFloat(document.getElementById('setting-preload-buffer')?.value || '5');
  const delay   = parseInt(  document.getElementById('setting-convert-delay')?.value  || '300');
  const skipAuth = document.getElementById('setting-skip-auth')?.checked ?? false;
  localStorage.setItem('sb_crossfade',      String(xfade));
  localStorage.setItem('sb_preload_buffer', String(Math.max(0, preload))); // takes effect on next track
  localStorage.setItem('sb_convert_delay',  String(delay));
  localStorage.setItem('sb_skip_auth', skipAuth ? '1' : '0');
  try {
    const scanZips = document.getElementById('setting-scan-zips')?.checked ?? true;
    const sidDur = parseInt(document.getElementById('setting-sid-duration')?.value || '180');
    const filterDups = document.getElementById('setting-filter-duplicates')?.checked ?? false;
    const useFolderArt = document.getElementById('setting-use-folder-art')?.checked ?? true;
    const remoteCacheMb = parseInt(document.getElementById('setting-remote-cache-mb')?.value || '2048');
    await api('/admin/settings', {
      method: 'PUT',
      body: JSON.stringify({
        scan_zips: scanZips,
        renderers: { sid_default_duration: sidDur },
        filter_duplicates: filterDups,
        use_folder_art: useFolderArt,
        remote_cache_max_mb: remoteCacheMb,
      }),
    });
    // Sync skip-auth to server
    await fetch('/api/admin/auth/skip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled: skipAuth }),
    });
    showMsg('admin-settings-msg', 'Settings saved.', 'ok');
  } catch {
    showMsg('admin-settings-msg', 'Error saving server settings.', 'err');
  }
});

// ── Source toggle (Local / Network) ──────────────────────────────────────────

document.querySelectorAll('.source-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.source-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const isNetwork = tab.dataset.source === 'network';
    document.getElementById('admin-add-local').style.display = isNetwork ? 'none' : '';
    document.getElementById('admin-add-network').style.display = isNetwork ? '' : 'none';
    if (isNetwork) {
      document.getElementById('net-anonymous')._userToggled = false;
      _updateNetFormForProtocol();
    }
  });
});

function _updateNetFormForProtocol() {
  const proto = document.getElementById('net-protocol').value;
  const shareInput = document.getElementById('net-share');
  const pathInput = document.getElementById('net-path');
  const anonText = document.getElementById('net-anon-text');
  const anonBox = document.getElementById('net-anonymous');
  if (proto === 'ftp') {
    shareInput.style.display = 'none';
    pathInput.placeholder = 'Remote path (e.g. /music)';
    anonText.textContent = 'Anonymous';
    if (!anonBox._userToggled) anonBox.checked = true;
  } else {
    shareInput.style.display = '';
    pathInput.placeholder = 'Path';
    anonText.textContent = 'Guest access';
    if (!anonBox._userToggled) anonBox.checked = false;
  }
  _toggleAnonFields();
}

function _toggleAnonFields() {
  const anon = document.getElementById('net-anonymous').checked;
  const userEl = document.getElementById('net-user');
  const passEl = document.getElementById('net-pass');
  userEl.disabled = anon;
  passEl.disabled = anon;
  if (anon) {
    userEl.value = '';
    passEl.value = '';
    userEl.placeholder = 'Not required';
    passEl.placeholder = 'Not required';
  } else {
    userEl.placeholder = 'Username';
    passEl.placeholder = 'Password';
  }
}

document.getElementById('net-protocol')?.addEventListener('change', _updateNetFormForProtocol);

document.getElementById('net-anonymous')?.addEventListener('change', (e) => {
  e.target._userToggled = true;
  _toggleAnonFields();
});

document.getElementById('btn-net-test')?.addEventListener('click', async () => {
  const btn = document.getElementById('btn-net-test');
  const msg = document.getElementById('net-status-msg');
  btn.disabled = true;
  msg.textContent = 'Testing...';
  msg.className = 'net-status-msg';
  try {
    const body = _getNetFormData();
    const res = await api('/admin/shares/test', {
      method: 'POST', body: JSON.stringify(body),
    });
    const d = await res.json();
    if (res.ok) {
      msg.textContent = d.message || 'Connected!';
      msg.className = 'net-status-msg net-ok';
    } else {
      msg.textContent = d.detail || 'Connection failed.';
      msg.className = 'net-status-msg net-err';
    }
  } catch {
    msg.textContent = 'Network error.';
    msg.className = 'net-status-msg net-err';
  }
  btn.disabled = false;
});

document.getElementById('btn-net-connect')?.addEventListener('click', async () => {
  const btn = document.getElementById('btn-net-connect');
  const msg = document.getElementById('net-status-msg');
  btn.disabled = true;
  msg.textContent = 'Connecting...';
  msg.className = 'net-status-msg';
  try {
    const body = _getNetFormData();
    const res = await api('/admin/shares', {
      method: 'POST', body: JSON.stringify(body),
    });
    const d = await res.json();
    if (res.ok) {
      msg.textContent = 'Connected! Scanning...';
      msg.className = 'net-status-msg net-ok';
      document.getElementById('net-host').value = '';
      document.getElementById('net-share').value = '';
      document.getElementById('net-user').value = '';
      document.getElementById('net-pass').value = '';
      document.getElementById('net-alias').value = '';
      document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
      loadDirs();
      loadStats();
      pollScanDone(() => {
        document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
        loadDirs();
        loadStats();
      });
    } else {
      msg.textContent = d.detail || 'Failed to connect.';
      msg.className = 'net-status-msg net-err';
    }
  } catch {
    msg.textContent = 'Network error.';
    msg.className = 'net-status-msg net-err';
  }
  btn.disabled = false;
});

function _getNetFormData() {
  return {
    protocol: document.getElementById('net-protocol').value,
    host: document.getElementById('net-host').value.trim(),
    share: document.getElementById('net-share').value.trim(),
    remote_path: document.getElementById('net-path').value.trim() || '/',
    username: document.getElementById('net-user').value.trim(),
    password: document.getElementById('net-pass').value,
    alias: document.getElementById('net-alias').value.trim(),
  };
}

async function reconnectShare(shareId, btn) {
  btn.disabled = true;
  btn.textContent = 'Reconnecting...';
  try {
    const res = await api('/admin/shares/reconnect', {
      method: 'POST', body: JSON.stringify({ id: shareId }),
    });
    if (res.ok) {
      showMsg('admin-dir-msg', 'Reconnected.', 'ok');
      loadDirs();
    } else {
      const d = await res.json();
      showMsg('admin-dir-msg', d.detail || 'Reconnect failed.', 'err');
      btn.textContent = 'Reconnect';
      btn.disabled = false;
    }
  } catch {
    showMsg('admin-dir-msg', 'Network error.', 'err');
    btn.textContent = 'Reconnect';
    btn.disabled = false;
  }
}

// ── Log viewer ──────────────────────────────────────────────────────────────
let _logAutoTimer = null;

async function loadLogs() {
  const viewer = document.getElementById('admin-log-viewer');
  if (!viewer) return;
  const lines = document.getElementById('log-lines-select')?.value || '200';
  try {
    const res = await api(`/admin/logs?lines=${lines}`);
    const d = await res.json();
    const wasAtBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
    viewer.textContent = (d.lines || []).join('\n') || 'No logs available.';
    if (wasAtBottom) viewer.scrollTop = viewer.scrollHeight;
  } catch {
    viewer.textContent = 'Error loading logs.';
  }
}

function _startLogAutoRefresh() {
  _stopLogAutoRefresh();
  _logAutoTimer = setInterval(loadLogs, 5000);
}
function _stopLogAutoRefresh() {
  if (_logAutoTimer) { clearInterval(_logAutoTimer); _logAutoTimer = null; }
}

document.getElementById('btn-refresh-logs')?.addEventListener('click', loadLogs);

document.getElementById('log-auto-refresh')?.addEventListener('change', (e) => {
  if (e.target.checked) _startLogAutoRefresh();
  else _stopLogAutoRefresh();
});

// ── Restart ────────────────────────────────────────────────────────────────

document.getElementById('btn-restart-app')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Restart SoniqBoom? Playback will continue from the buffer if possible, '
    + 'otherwise it fades out and resumes automatically once the server is back.',
    { title: 'Restart Server', okLabel: 'Restart' }
  );
  if (!ok) return;
  showMsg('admin-restart-msg', 'Sending restart request…', 'ok');
  try {
    const res = await api('/admin/restart', { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json().catch(() => ({}));
    showMsg(
      'admin-restart-msg',
      `Restart initiated (${data.mode || 'server'}). Reconnecting…`,
      'ok'
    );
    // Kick off the overlay + reconnect flow.  Don't await — the request's
    // reply arrives before the server actually exits, so the overlay owns
    // the user-facing loop from here.
    runRestartFlow().then(() => {
      showMsg('admin-restart-msg', 'Server is back online.', 'ok');
    });
  } catch (err) {
    showMsg('admin-restart-msg', `Error: ${err.message || err}`, 'err');
  }
});

// Expose styledConfirm globally so other modules (e.g. playlist.js) can use it
window.__sbConfirm = styledConfirm;

export const Admin = { open };
