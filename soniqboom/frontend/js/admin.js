// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * admin.js — Admin panel: OS auth, folder management, scan, export/import.
 */

import { runRestartFlow } from './restart.js';
import { Auth }           from './auth.js';
import { Player }         from './player.js';
import { Toast, trapFocus } from './utils.js';
import { vizGroupEnabled, getVizSettings, setVizSettings } from './viz/engine.js';
// Experimental in-browser SID playback toggle (see sid-wasm-player.js).
import { sidWasmPlaybackEnabled, setSidWasmPlayback } from './sid-wasm-player.js';
import { mountScanFlow }   from './viz/scanflow.js';
import { mountCacheCascade } from './viz/cachecascade.js';
import { mountFtpLanes }   from './viz/ftplanes.js';

// ── Admin visualizations (#1 scan flow, #2 cache cascade, #3 FTP lanes) ──
// Lazily mounted into their admin sections.  Each is gated on the "admin"
// viz group; the shared engine pauses them when off-screen / hidden tab.
let _vizScan = null, _vizCascade = null, _vizFtp = null;
function _ensureAdminViz() {
  if (!vizGroupEnabled('admin')) return;
  const scanHost = document.getElementById('viz-scan-flow');
  if (scanHost && !_vizScan) { scanHost.hidden = false; try { _vizScan = mountScanFlow(scanHost); } catch (e) { console.warn('scanflow', e); } }
  const cascadeHost = document.getElementById('viz-cache-cascade');
  if (cascadeHost && !_vizCascade) { cascadeHost.hidden = false; try { _vizCascade = mountCacheCascade(cascadeHost); } catch (e) { console.warn('cascade', e); } }
  const ftpHost = document.getElementById('viz-ftp-lanes');
  if (ftpHost && !_vizFtp) { ftpHost.hidden = false; try { _vizFtp = mountFtpLanes(ftpHost); } catch (e) { console.warn('ftplanes', e); } }
}

// ── Visualization settings UI (client-side preference, applies instantly) ──
// The viz settings live in localStorage (sb_viz_settings), read by the viz
// engine.  These controls are NOT part of the server-persisted Save Settings
// flow — each change is applied live via setVizSettings().
let _vizSettingsWired = false;
function _initVizSettingsUI() {
  const s = getVizSettings();
  const en   = document.getElementById('setting-viz-enabled');
  const np   = document.getElementById('setting-viz-nowplaying');
  const adm  = document.getElementById('setting-viz-admin');
  const vu   = document.getElementById('setting-viz-vustyle');
  const sidw = document.getElementById('setting-sid-wasm-playback');   // experimental: in-browser SID
  if (en)  en.checked  = s.enabled    !== false;
  if (np)  np.checked  = s.nowPlaying !== false;
  if (adm) adm.checked = s.admin      !== false;
  if (vu)  vu.value    = s.vuStyle === 'circuit' ? 'circuit' : 'bars';
  if (sidw) sidw.checked = sidWasmPlaybackEnabled();
  if (_vizSettingsWired) return;     // bind change handlers exactly once
  _vizSettingsWired = true;
  if (sidw) sidw.addEventListener('change', () => setSidWasmPlayback(sidw.checked));
  if (en)  en.addEventListener('change',  () => setVizSettings({ enabled:    en.checked }));
  if (np)  np.addEventListener('change',  () => setVizSettings({ nowPlaying: np.checked }));
  if (adm) adm.addEventListener('change', () => { setVizSettings({ admin: adm.checked }); if (adm.checked) _ensureAdminViz(); });
  if (vu)  vu.addEventListener('change',  () => {
    setVizSettings({ vuStyle: vu.value });
    _applyVuStyleLive(vu.value);
  });
}
// Live-apply a VU style to a currently-playing VUMR meter so the change is
// visible immediately rather than only on the next track.  The circuit skin is
// VUMR-only and gated on the now-playing group, mirroring app.js.
function _applyVuStyleLive(style) {
  const vc = document.getElementById('vu-meters');
  if (vc && vc.dataset.source === 'vumr') {
    vc.dataset.style = (style === 'circuit' && vizGroupEnabled('nowPlaying')) ? 'circuit' : 'bars';
  }
}

const overlay        = document.getElementById('admin-overlay');
const aliasDialog    = document.getElementById('admin-alias-dialog');
const adminPanel     = document.getElementById('admin-panel');

// ⓘ icons sit inside their row's <label for=…>, so a click or tap on one (touch
// users tap it to read the tip) would activate the label and flip the setting —
// and the next Save would commit that.  One delegated handler blocks label
// activation from any ⓘ in the panel, current rows and future ones alike; a click
// on the label TEXT still toggles its control, and focusing the ⓘ still shows the
// tip (``.setting-info:focus::after``).
adminPanel?.addEventListener('click', (e) => {
  if (e.target instanceof Element && e.target.closest('label .setting-info')) e.preventDefault();
});

// The ⓘ tips are drawn by CSS (``::after { content: attr(data-tip) }``), which
// assistive tech does not read.  Mirror each tip into a visually-hidden element
// and point the row's control at it with aria-describedby, so a screen reader
// announces the tip with the setting (the mirror sits outside the <label>).
// The control is named by its label's text WITHOUT the ⓘ: the icon
// (role=img "Info") sits inside the <label>, so the computed name would end in
// "Info" — and a <label> with no ``for`` names nothing at all.  A control that
// already carries its own name keeps it.  The ⓘ itself stays focusable for
// sighted keyboard users and is described by the same text.  Runs once: the
// markup is static in index.html.
(function _wireSettingTips() {
  document.querySelectorAll('.setting-info[data-tip]').forEach((icon, n) => {
    const row = icon.closest('.admin-setting');
    const lbl = icon.closest('label');
    const ctl = (lbl && lbl.htmlFor && document.getElementById(lbl.htmlFor))
             || (row && row.querySelector('input, select, textarea'));
    const host = row || (lbl && lbl.parentNode) || icon.parentNode;
    if (!ctl || !host) return;
    const id = `setting-tip-${n}`;
    const mirror = document.createElement('span');
    mirror.id = id;
    mirror.className = 'sr-only';
    mirror.textContent = icon.dataset.tip;
    host.appendChild(mirror);
    const prev = ctl.getAttribute('aria-describedby');
    ctl.setAttribute('aria-describedby', prev ? `${prev} ${id}` : id);
    icon.setAttribute('aria-describedby', id);
    if (lbl && !ctl.hasAttribute('aria-label') && !ctl.hasAttribute('aria-labelledby')) {
      const copy = lbl.cloneNode(true);
      copy.querySelectorAll('.setting-info').forEach(el => el.remove());
      const name = copy.textContent.replace(/\s+/g, ' ').trim();
      if (name) ctl.setAttribute('aria-label', name);
    }
  });
})();

// A tip is drawn ABOVE its ⓘ; one whose row sits near the top of the panel's
// scroll box would be cut off there (the box cannot scroll above its top), so
// it is drawn below instead when there is less room above than the tip needs
// and more room below.  Decided as the tip is about to show (hover / focus) —
// the row's position depends on scrolling.
function _placeSettingTip(e) {
  const icon = e.target instanceof Element ? e.target.closest('.setting-info[data-tip]') : null;
  if (!icon) return;
  let box = icon.parentElement;
  while (box && box !== document.body) {
    const oy = getComputedStyle(box).overflowY;
    if (oy === 'auto' || oy === 'scroll' || oy === 'hidden') break;
    box = box.parentElement;
  }
  const b = (box && box !== document.body) ? box.getBoundingClientRect()
                                           : { top: 0, bottom: window.innerHeight };
  const r = icon.getBoundingClientRect();
  // The tip's own height (the pseudo-element is laid out even while invisible);
  // an estimate from its length where the engine reports none.
  let h = parseFloat(getComputedStyle(icon, '::after').height);
  if (!(h > 0)) h = Math.ceil(icon.dataset.tip.length / 32) * 15 + 12;
  const above = r.top - b.top, below = b.bottom - r.bottom;
  icon.classList.toggle('tip-below', above < h + 8 && below > above);
}
adminPanel?.addEventListener('pointerover', _placeSettingTip, { passive: true });
adminPanel?.addEventListener('focusin', _placeSettingTip);

let _isOpen = false;
// WCAG 2.4.3: when a modal closes, focus should return to whatever the
// user was on before it opened.  Captured by ``open()``, restored by
// ``close()`` — same pattern the password / trackinfo dialogs use.
let _adminFocusBefore = null;

// ── Reusable styled confirm dialog (replaces native confirm()) ───────────────

const confirmDialog = document.getElementById('confirm-dialog');
const confirmTitle  = document.getElementById('confirm-dialog-title');
const confirmMsg    = document.getElementById('confirm-dialog-message');
const confirmOk     = document.getElementById('btn-confirm-ok');
const confirmCancel = document.getElementById('btn-confirm-cancel');
let _confirmResolve = null;
let _confirmPreviousDialog = null; // which dialog was showing before confirm
let _confirmTrapRelease = null;    // focus-trap release fn while the confirm is up
let _confirmPrevFocus = null;      // element to restore focus to on close

/**
 * Show an in-app styled confirm dialog. Returns a promise that resolves
 * to true (OK) or false (Cancel).
 */
function styledConfirm(message, { title = 'Confirm', okLabel = 'OK', dangerColor = true } = {}) {
  return new Promise((resolve) => {
    _confirmResolve = resolve;
    _confirmPrevFocus = (document.activeElement instanceof HTMLElement) ? document.activeElement : null;
    confirmTitle.textContent = title;
    confirmMsg.textContent = message;
    confirmOk.textContent = okLabel;
    confirmOk.className = dangerColor ? 'btn-danger-action' : 'btn-primary';
    // Track which dialog is currently visible so we can restore it
    _confirmPreviousDialog = null;
    if (!adminPanel.classList.contains('hidden')) {
      _confirmPreviousDialog = 'panel';
      adminPanel.classList.add('hidden');
    } else if (!aliasDialog.classList.contains('hidden')) {
      _confirmPreviousDialog = 'alias';
      aliasDialog.classList.add('hidden');
    }
    // When invoked from outside the admin context (e.g. a multi-row playlist
    // delete), don't paint the heavy admin backdrop over the whole page.
    // ``confirm-standalone`` is a one-off class that the CSS uses to give
    // the dialog its own translucent layer instead of co-opting the admin
    // overlay's dark fill.
    const standalone = _confirmPreviousDialog === null;
    overlay.classList.toggle('confirm-standalone', standalone);
    confirmDialog.classList.remove('hidden');
    overlay.classList.remove('hidden');
    confirmOk.focus();
    // Trap Tab within the confirm so focus can't wander to the (inert-looking)
    // page behind a standalone confirm (bug 4.3); released on close.
    _confirmTrapRelease = trapFocus(confirmDialog);
  });
}

function _closeConfirmDialog(result) {
  if (_confirmTrapRelease) { _confirmTrapRelease(); _confirmTrapRelease = null; }
  confirmDialog.classList.add('hidden');
  // Restore the previous dialog, or hide overlay if confirm was standalone
  if (_confirmPreviousDialog === 'panel') adminPanel.classList.remove('hidden');
  else if (_confirmPreviousDialog === 'alias') aliasDialog.classList.remove('hidden');
  else overlay.classList.add('hidden'); // no parent dialog — hide overlay entirely
  overlay.classList.remove('confirm-standalone');
  _confirmPreviousDialog = null;
  if (_confirmPrevFocus && _confirmPrevFocus.isConnected) { try { _confirmPrevFocus.focus(); } catch (_) {} }
  _confirmPrevFocus = null;
  if (_confirmResolve) { _confirmResolve(result); _confirmResolve = null; }
}

confirmOk.addEventListener('click', (e) => { e.stopPropagation(); _closeConfirmDialog(true); });
confirmCancel.addEventListener('click', (e) => { e.stopPropagation(); _closeConfirmDialog(false); });

// Expose styledConfirm globally so other modules (e.g. playlist.js) can
// fall back to it before admin.js's deeper-bodied init runs.  Install at
// top level — synchronous, right after the DOM refs are gathered — so
// any module that imports admin.js can rely on ``window.__sbConfirm``
// being defined immediately, not 1700 lines later in the file.
window.__sbConfirm = styledConfirm;

// ── Auth ──────────────────────────────────────────────────────────────────────

function _hideAllDialogs() {
  adminPanel.classList.add('hidden');
  aliasDialog.classList.add('hidden');
  confirmDialog.classList.add('hidden');
}

async function open() {
  // Toggle: if already open, close it
  if (_isOpen) { close(); return; }
  _isOpen = true;
  // Capture focus before the overlay grabs it.  Restored by close()
  // when the user dismisses the panel (Esc / Cancel / X / scrim).
  _adminFocusBefore = document.activeElement;

  // Reset all child dialogs to a clean state
  _hideAllDialogs();

  // Multi-user path: the user is already signed in via the auth overlay.
  // If they have role=admin, open the admin panel directly.  Otherwise
  // refuse — non-admin users have no business inside the admin overlay
  // (they manage their own scrobble tokens via System tab instead).
  if (Auth.user) {
    if (!Auth.isAdmin) {
      // No admin panel for this account — but crossfade, the shuffle option and the
      // theme are the LISTENER's own, stored in this browser, so the gear opens those.
      _isOpen = false;
      _openDevicePrefs();
      return;
    }
    adminPanel.classList.remove('hidden');
    overlay.classList.remove('hidden');
    loadStats();
    loadDirs();
    loadSettings();
    loadServices();
    loadRendererStatus();
    loadSoundfonts();
    startScanPoller();
    _syncRepairStatus?.();
    _syncGameTagStatus?.();
    _showAdminOnlyTabs();
    // If the Log tab is the active one when admin opens (e.g. it was the
    // last-active tab in this session, or the panel was opened directly
    // to it), kick the log fetch — the click handler at line ~218 only
    // fires on an actual click, so without this the user sees the static
    // placeholder forever.
    if (document.querySelector('.admin-tab[data-tab="tab-log"][aria-selected="true"]')) {
      loadLogs();
    }
    return;
  }

  // No signed-in user: admin requires an admin account.  The first admin is
  // created out-of-band with ``soniqboom-setadm`` — there is no in-app
  // credential prompt (the legacy OS-credential path was removed).
  _isOpen = false;
  const { Toast } = await import('./utils.js');
  Toast.error?.('Sign in as an admin to open settings.');
}

function close() {
  _isOpen = false;
  _clearRevealedSecrets();         // a just-generated Subsonic password is shown once
  _hideAllDialogs();
  overlay.classList.add('hidden');
  stopScanPoller();
  // Close the conversion-cache SSE stream on EVERY dismissal path —
  // it used to be wired only to the ✕ button, so Escape / Cancel left
  // an EventSource reconnecting forever in the background (QA).
  try { _stopCacheStream(); } catch { /* defined later in this file */ }
  // Cancel any pending confirm dialog
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
  // WCAG 2.4.3: restore focus to the element that had it before open().
  if (_adminFocusBefore && typeof _adminFocusBefore.focus === 'function'
      && document.contains(_adminFocusBefore)) {
    try { _adminFocusBefore.focus(); } catch { /* ignore */ }
  }
  _adminFocusBefore = null;
}

document.getElementById('btn-admin-close').addEventListener('click', close);
// Escape closes the admin overlay too — EQ and Track Info already
// honoured Escape (trackinfo.js), but Admin (the biggest modal) didn't,
// breaking modal-dismissal muscle memory (UX/UI #1 #6).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  // The confirm dialog is Escape-closable whether it was opened from INSIDE the
  // admin overlay or STANDALONE (playlist delete, DLNA toggle, ffmpeg download).
  // The old guard on ``_isOpen`` left the standalone confirm in an Escape
  // dead-zone (bug 4.3) — check the confirm first, regardless of admin state.
  if (!confirmDialog.classList.contains('hidden')) { _closeConfirmDialog(false); return; }
  if (_isOpen && !overlay.classList.contains('hidden')) {
    close();
  }
});
overlay.addEventListener('click', (e) => {
  if (e.target !== overlay) return;
  // If confirm dialog is open, treat background click as cancel
  if (!confirmDialog.classList.contains('hidden')) { _closeConfirmDialog(false); return; }
  close();
});

// ── Tab switching ────────────────────────────────────────────────────────────
// Wire role="tablist" / role="tab" / role="tabpanel" semantics so screen
// readers announce "Selected, tab 2 of 5".  Mark every pane with role +
// labelledby once at startup; the click handler keeps aria-selected synced.
document.querySelectorAll('.admin-tab').forEach(tab => {
  const paneEl = document.getElementById(tab.dataset.tab);
  if (paneEl) {
    paneEl.setAttribute('role', 'tabpanel');
    paneEl.setAttribute('aria-labelledby', tab.id || tab.dataset.tab);
    paneEl.tabIndex = 0;
  }
});

document.querySelectorAll('.admin-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.admin-tab').forEach(t => {
      t.classList.remove('active');
      t.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.admin-tab-pane').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    const pane = document.getElementById(tab.dataset.tab);
    if (pane) pane.classList.add('active');
    // Load tab-specific data on switch
    if (tab.dataset.tab === 'tab-system') {
      loadDiskUsage();
      loadSettings();
      _apiKeyRefreshers.forEach(r => r());   // "last used" moves on as apps sign in
    } else if (tab.dataset.tab === 'tab-log') {
      loadLogs();
    } else if (tab.dataset.tab === 'tab-users') {
      loadUsers();
    } else if (tab.dataset.tab === 'tab-renderers') {
      loadHvscStatus();
      // Populates the SID fidelity + renderer-path fields (and the rest
      // of the settings mirrors — cheap, idempotent).
      loadSettings();
    } else if (tab.dataset.tab === 'tab-metadata') {
      loadSceneStatus();
      loadSongdbStatus();
      loadGameTitlesStatus();
      loadDemozooStatus();
      loadMdHvscStatus();
    } else if (tab.dataset.tab === 'tab-backup') {
      loadBackupStatus();
    }
  });
});

// Hide / show admin-only tabs (currently just "Users") based on the
// signed-in user's role.  The Users tab carries class .admin-tab-admin-only
// and starts hidden in HTML; we flip it visible once we know the role.
function _showAdminOnlyTabs() {
  const isAdmin = !!Auth.isAdmin;
  document.querySelectorAll('.admin-tab-admin-only').forEach(el => {
    el.hidden = !isAdmin;
  });
}

// ── API helper ────────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const isFormData = opts.body instanceof FormData;
  // Auth is the signed-in user's session cookie (admin role) — sent same-origin.
  const res = await fetch(`/api${path}`, {
    ...opts,
    credentials: 'same-origin',
    headers: {
      ...(!isFormData ? { 'Content-Type': 'application/json' } : {}),
      ...(opts.headers || {}),
    },
  });
  // Throw on non-2xx so every call site's surrounding try/catch surfaces
  // server errors instead of optimistically continuing.  QA-2 P0 flagged
  // the previous bug: a /admin/services PUT that 500'd showed the UI as
  // "saved" while the server hadn't applied the change.  Callers that
  // genuinely need to inspect 4xx/5xx bodies can pass ``allowNonOK: true``
  // in the opts to bypass — none currently do.
  if (!res.ok && !opts.allowNonOK) {
    let detail = '';
    try {
      const j = await res.clone().json();
      detail = j.detail || j.error || '';
    } catch {
      detail = await res.clone().text().catch(() => '');
    }
    const err = new Error(detail || `${path} → ${res.status}`);
    err.status = res.status;
    err.response = res;
    throw err;
  }
  return res;
}

// The text for a failed api() call: the server's reason when it answered
// (api() throws on non-2xx with the response's detail as the message), else
// "Network error." — a fetch that never reached the server has no status.
function _apiErrText(e, fallback = 'Network error.') {
  return (e && e.status) ? (e.message || fallback) : 'Network error.';
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
      // Still try to refresh the FTP pool section in case a recent
      // share-remove cleared everything.
      loadFtpPools().catch(() => {});
      loadFreshness().catch(() => {});
      return;
    }
    dirs.forEach(d => renderDirRow(list, d, scanActive));
    // Refresh the FTP Connection Pools section alongside the dir list
    // so the user always sees current effective caps + detected values
    // when they navigate to the Library tab.
    loadFtpPools().catch(() => {});
    // Same for the remote-freshness section — refreshes per-share
    // last_check / next_check times whenever the Library tab opens.
    loadFreshness().catch(() => {});
  } catch {
    list.innerHTML = '<span style="color:#e55;font-size:12px">Failed to load.</span>';
  }
}

// ── FTP Connection Pools section ──────────────────────────────────────
//
// Fetched via GET /api/admin/ftp-pool/status; renders one card per FTP
// share with scan + stream sliders, the detected server cap, and live
// pool state (in-use / idle / waiting).  Slider changes PUT to
// /api/admin/shares/{id}/ftp-pool; "Test now" runs the active probe;
// "Reset detected" clears the auto-learned cap.
//
// The section auto-hides when there are no FTP shares so non-FTP users
// don't see an empty card.

async function loadFtpPools() {
  const section = document.getElementById('admin-ftp-pool-section');
  const list    = document.getElementById('admin-ftp-pool-list');
  if (!section || !list) return;
  let data;
  try {
    const res = await api('/admin/ftp-pool/status');
    if (!res.ok) {
      section.style.display = 'none';
      return;
    }
    data = await res.json();
  } catch {
    section.style.display = 'none';
    return;
  }
  // Backend now returns ``{servers: [...], detected_caps: {...}}`` —
  // one entry per host:port, not per share.  Older backends returned
  // ``{shares: [...]}`` keyed by share_id; if we ever get that shape
  // (e.g. transient roll-back) hide the section to avoid rendering
  // the wrong thing.
  const servers = (data && data.servers) || [];
  if (!servers.length) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  _ensureAdminViz();
  list.innerHTML = '';
  servers.forEach(srv => list.appendChild(_renderFtpPoolCard(srv)));
}


// ── Remote Freshness section ──────────────────────────────────────────
//
// Fetches per-share state from GET /admin/freshness/status, renders one
// row per share with "Last checked", "Next in", and a "Check now"
// button.  The user-visible cadence math is left to the backend
// (statistics.median over the change-history window) — the UI just
// surfaces what came back.

function _formatAgo(seconds) {
  if (seconds == null) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function _formatIn(seconds) {
  if (seconds == null) return '—';
  if (seconds <= 0) return 'soon';
  if (seconds < 60) return `in ${Math.round(seconds)}s`;
  if (seconds < 3600) return `in ${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `in ${Math.round(seconds / 3600)}h`;
  return `in ${Math.round(seconds / 86400)}d`;
}

async function loadFreshness() {
  const section = document.getElementById('admin-freshness-section');
  const list    = document.getElementById('admin-freshness-list');
  if (!section || !list) return;
  let data;
  try {
    const res = await api('/admin/freshness/status');
    if (!res.ok) { section.style.display = 'none'; return; }
    data = await res.json();
  } catch {
    section.style.display = 'none';
    return;
  }
  const shares = (data && data.shares) || [];
  if (!shares.length) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  // Map alias from the ui-config so the user sees friendly names.
  const aliases = (window.__sbConfig && window.__sbConfig.folder_aliases) || {};
  list.innerHTML = '';
  shares.forEach(sh => {
    const row = document.createElement('div');
    row.className = 'admin-freshness-row';
    row.style.cssText = (
      'display:flex;align-items:center;gap:10px;padding:8px 10px;'
      + 'border-bottom:1px solid var(--border);font-size:13px;'
    );
    const alias = aliases[sh.scan_root] || sh.scan_root;
    const cadenceMin = Math.round((sh.cadence_seconds || 0) / 60);
    const armedDot = sh.armed
      ? '<span style="color:#5c5;font-size:10px" title="Auto-polling enabled">●</span>'
      : '<span style="color:#888;font-size:10px" title="Not armed">○</span>';
    const inflightTag = sh.inflight
      ? '<span style="color:#bb5;font-size:11px;margin-left:6px">⏳ checking…</span>'
      : '';
    row.innerHTML = (
      `<span style="flex:0 0 14px">${armedDot}</span>`
      + `<span style="flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(sh.scan_root)}">${esc(alias)}${inflightTag}</span>`
      + `<span style="flex:0 0 auto;color:var(--text2);font-size:11px" title="Adaptive interval — narrows on busy shares, widens on stable ones">~${cadenceMin}m</span>`
      + `<span style="flex:0 0 auto;color:var(--text2);font-size:11px" title="${sh.last_check_ts ? new Date(sh.last_check_ts * 1000).toLocaleString() : 'never checked'}">${_formatAgo(sh.seconds_since_check)}</span>`
      + `<span style="flex:0 0 auto;color:var(--text2);font-size:11px" title="${sh.next_check_ts ? new Date(sh.next_check_ts * 1000).toLocaleString() : 'no next-check scheduled'}">${_formatIn(sh.seconds_until_next)}</span>`
      + `<button class="btn-secondary" style="flex:0 0 auto;padding:3px 8px;font-size:11px" data-scan-root="${esc(sh.scan_root)}">Check now</button>`
    );
    const btn = row.querySelector('button');
    btn.addEventListener('click', () => _checkFreshnessNow(sh.scan_root, btn));
    list.appendChild(row);
  });
}

async function _checkFreshnessNow(scanRoot, btn) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Checking…';
  try {
    const res = await api('/admin/freshness/check_now', {
      method: 'POST',
      body: JSON.stringify({ scan_root: scanRoot }),
    });
    const data = await res.json().catch(() => ({}));
    const plan = data.plan || {};
    const fresh = Number(plan.extract || 0);
    if (fresh > 0) {
      // The remote_new_tracks WS event from the backend will fire the
      // user-visible toast.  Just give immediate button feedback.
      btn.textContent = `+${fresh}`;
    } else {
      btn.textContent = 'No changes';
    }
  } catch (err) {
    btn.textContent = 'Failed';
    console.warn(`/admin/freshness/check_now(${scanRoot}) failed`, err);
  } finally {
    setTimeout(() => {
      btn.textContent = origText;
      btn.disabled = false;
      loadFreshness().catch(() => {});
    }, 2000);
  }
}

// Each card represents ONE physical pool (one host:port credential
// pair), listing every share that connects via it.  Save / Test now /
// Reset all operate against the host:port key so the canonical store
// stays in sync regardless of how many shares share the server.
function _renderFtpPoolCard(srv) {
  const card = document.createElement('div');
  card.className = 'admin-ftp-pool-card';
  card.dataset.label = srv.label;

  const detected = srv.detected_cap;
  const isClamped = detected != null && srv.configured_total > srv.effective_max;
  const detectedTxt = detected == null
    ? 'Unknown (will auto-learn)'
    : `${detected}${isClamped ? ` — clamping to ${srv.effective_max}` : ''}`;

  const live = srv.live || {};
  const liveTxt = srv.live
    ? `live: in_use=${live.in_use} idle=${live.idle} waiting_stream=${live.waiting_stream} waiting_scan=${live.waiting_scan}`
    : 'live: pool not yet created (no traffic this session)';

  // Render the list of shares using this server so the user knows what
  // they're tuning.  Aliases (when set) take priority over the auto-
  // generated share_id — they're the human label the user picked.
  const sharesHtml = (srv.shares || [])
    .map(s => `<span class="pool-share-chip" title="${esc(s.share_id)}">${esc(s.alias || s.name)}</span>`)
    .join(' ');

  const idScan   = `pool-scan-${srv.label.replace(/[^a-z0-9]/gi, '_')}`;
  const idStream = `pool-stream-${srv.label.replace(/[^a-z0-9]/gi, '_')}`;

  card.innerHTML = `
    <div class="pool-head">
      <div>
        <span class="pool-name">${esc(srv.label)}</span>
      </div>
      <div class="pool-detected ${isClamped ? 'warn' : ''}">
        Server cap: <b>${esc(detectedTxt)}</b>
      </div>
    </div>
    <div class="pool-shares" style="margin: -4px 0 8px; font-size:12px; color:var(--text2)">
      Shares: ${sharesHtml || '<i>(none)</i>'}
    </div>
    <div class="pool-slider-row">
      <label for="${idScan}">Scan workers</label>
      <input id="${idScan}" class="pool-scan"
             type="range" min="1" max="16" value="${srv.scan_budget}">
      <span class="pool-val" data-for="scan">${srv.scan_budget}</span>
    </div>
    <div class="pool-slider-row">
      <label for="${idStream}">Stream workers</label>
      <input id="${idStream}" class="pool-stream"
             type="range" min="1" max="8" value="${srv.stream_budget}">
      <span class="pool-val" data-for="stream">${srv.stream_budget}</span>
    </div>
    <div class="pool-effective">
      Effective total: <b class="pool-total">${srv.effective_max}</b>
      <span class="pool-total-hint" style="opacity:.7"></span>
    </div>
    <div class="pool-grow-row" style="margin:6px 0 8px;font-size:12px;color:var(--text2)">
      <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" class="pool-auto-grow" ${srv.auto_grow ? 'checked' : ''}>
        Auto-grow scan workers up to server cap
      </label>
      <div style="margin-left:22px;opacity:.7;font-size:11px">
        Background probe tries +1 worker every 60 s while saturated; stops at the server cap.
        Saved scan slider creeps up so the gain survives restart.
      </div>
    </div>
    <div class="pool-actions">
      <button class="btn-pool-save">Save</button>
      <button class="btn-pool-probe btn-secondary"
              title="Open connections one-by-one until the server rejects, to detect the limit">Test now</button>
      <button class="btn-pool-reset btn-secondary"
              title="Forget the auto-learned cap and use your configured total directly">Reset detected</button>
    </div>
    <div class="pool-live">${esc(liveTxt)}</div>
  `;

  const scanInput   = card.querySelector('.pool-scan');
  const streamInput = card.querySelector('.pool-stream');
  const totalEl     = card.querySelector('.pool-total');
  const hintEl      = card.querySelector('.pool-total-hint');

  const updateTotals = () => {
    const sc = parseInt(scanInput.value, 10);
    const st = parseInt(streamInput.value, 10);
    card.querySelector('[data-for="scan"]').textContent   = sc;
    card.querySelector('[data-for="stream"]').textContent = st;
    const sum = sc + st;
    let effective = sum;
    let hint = '';
    if (detected != null && sum > detected - 1) {
      effective = Math.max(1, detected - 1);
      hint = ` (clamped to detected − 1 = ${effective})`;
    }
    totalEl.textContent = effective;
    hintEl.textContent  = hint;
  };
  scanInput.addEventListener('input', updateTotals);
  streamInput.addEventListener('input', updateTotals);

  // Save → PUT /admin/ftp-pool with {host, port, scan, stream}.
  // The new canonical endpoint stores under conf.ftp_pools[host:port].
  card.querySelector('.btn-pool-save').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const sc = parseInt(scanInput.value, 10);
      const st = parseInt(streamInput.value, 10);
      const ag = !!card.querySelector('.pool-auto-grow')?.checked;
      const res = await api('/admin/ftp-pool', {
        method: 'PUT',
        body: JSON.stringify({
          host: srv.host, port: srv.port,
          scan: sc, stream: st,
          auto_grow: ag,
        }),
      });
      if (res.ok) {
        showMsg('admin-ftp-pool-msg',
                `Saved: ${srv.label} scan=${sc}, stream=${st} (live pool resized).`, 'ok');
        loadFtpPools();
      } else {
        const d = await res.json().catch(() => ({}));
        showMsg('admin-ftp-pool-msg', d.detail || 'Save failed.', 'err');
      }
    } catch {
      showMsg('admin-ftp-pool-msg', 'Network error.', 'err');
    } finally {
      btn.disabled = false;
    }
  });

  // Probe / Reset still accept either share_id OR (host, port).  We
  // pass host+port since the card no longer has a single share_id.
  card.querySelector('.btn-pool-probe').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = 'Probing…';
    try {
      // probe-cap accepts host+port directly now; the backend resolves a
      // matching configured FTP share on this endpoint to borrow creds.
      const res = await api('/admin/ftp-pool/probe-cap', {
        method: 'POST',
        body: JSON.stringify({ host: srv.host, port: srv.port }),
      });
      const d = await res.json();
      if (res.ok) {
        const msg = d.detected != null
          ? `${srv.label}: detected server cap = ${d.detected}.`
          : `${srv.label}: probe completed without hitting a limit.`;
        showMsg('admin-ftp-pool-msg', msg, 'ok');
        loadFtpPools();
      } else {
        showMsg('admin-ftp-pool-msg', d.detail || 'Probe failed.', 'err');
      }
    } catch {
      showMsg('admin-ftp-pool-msg', 'Network error.', 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Test now';
    }
  });

  card.querySelector('.btn-pool-reset').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await api('/admin/ftp-pool/reset-cap', {
        method: 'POST',
        body: JSON.stringify({ host: srv.host, port: srv.port }),
      });
      if (res.ok) {
        showMsg('admin-ftp-pool-msg', `${srv.label}: forgot detected cap.`, 'ok');
        loadFtpPools();
      } else {
        const d = await res.json().catch(() => ({}));
        showMsg('admin-ftp-pool-msg', d.detail || 'Reset failed.', 'err');
      }
    } catch {
      showMsg('admin-ftp-pool-msg', 'Network error.', 'err');
    } finally {
      btn.disabled = false;
    }
  });

  return card;
}

function renderDirRow(list, d, scanActive = false) {
  const alreadyIndexed = (d.track_count ?? 0) > 0;
  const isNetwork = !!d.network_share_id;
  // Unavailable = an offline share OR a local drive that is not mounted.  Either
  // way the row is marked, not hidden, and its library data is kept.
  const isUnavailable = d.status === 'unavailable';
  const row = document.createElement('div');
  row.className = 'admin-dir-row' + (isUnavailable ? ' dir-unavailable' : '');
  row.dataset.path = d.path;

  const btnLabel = alreadyIndexed ? 'Re-Index' : 'Index';
  const btnClass = alreadyIndexed ? 'btn-reindex-small' : 'btn-index-small';

  const aliases = (window.__sbConfig && window.__sbConfig.folder_aliases) || {};
  const alias = aliases[d.path] || '';
  const aliasStr = alias ? ` [${alias}]` : '';

  const statusDot = (isNetwork || isUnavailable)
    ? `<span class="dir-status-dot ${isUnavailable ? 'dot-red' : 'dot-green'}" title="${isUnavailable ? (isNetwork ? 'Unavailable' : 'Drive not connected') : 'Connected'}"></span>`
    : '';
  // Reconnect needs a network share; a local drive just has to be plugged in.
  const localNote = (isUnavailable && !isNetwork)
    ? '<span class="admin-dir-note" title="Drive not connected \u2014 library data kept">Drive not connected \u2014 library data kept</span>'
    : (isNetwork && d.auth_refused)
      ? (() => {
          const mins = Math.max(1, Math.round((d.auth_retry_in_s || 0) / 60));
          const txt = `Sign-in refused \u2014 next try in ${mins} min (Reconnect tries now)`;
          return `<span class="admin-dir-note" title="${esc(txt)}">${esc(txt)}</span>`;
        })()
      : '';

  row.innerHTML = `
    ${statusDot}
    <span class="admin-dir-path" title="${esc(d.path)}">${esc(d.path)}${esc(aliasStr)}</span>
    ${localNote}
    <span class="admin-dir-count">${d.track_count ?? 0} tracks</span>
    <button class="btn-alias-edit" data-path="${esc(d.path)}">Alias</button>
    ${(isUnavailable && isNetwork) ? `<button class="btn-reconnect" data-share="${esc(d.network_share_id)}">Reconnect</button>` : ''}
    <button class="btn-index ${btnClass}"
            data-path="${esc(d.path)}"${isUnavailable ? ` disabled title="${isNetwork ? 'Reconnect the share to re-index' : 'Connect the drive to re-index'}"` : ''}>
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
  showMsg('admin-dir-msg', `Scanning ${path}\u2026`, 'info');

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
    // Surface skipped shares — without this the user thinks the
    // re-index "jumped to Done" because no scan task actually started
    // (e.g. the remote share's source isn't registered and reconnect
    // failed).  The backend now returns ``{started, scanned, skipped}``;
    // a fully-skipped request means nothing's happening, so abort the
    // poll and let the user know why.
    const data = await res.json().catch(() => ({}));
    if (data && data.skipped && data.skipped.length && (!data.scanned || !data.scanned.length)) {
      const reasons = data.skipped.map(s => `${s.path} — ${s.reason}`).join('; ');
      showMsg('admin-dir-msg',
        `Couldn't scan: ${reasons}.  Try reconnecting the share first.`, 'err');
      btn.textContent = 'Re-Index';
      btn.disabled = false;
      return;
    }
    // Poll in the background — don't block the UI
    pollScanDone((finalStatus) => {
      btn.textContent = 'Re-Index';
      btn.disabled = false;
      btn.classList.remove('btn-index-small');
      btn.classList.add('btn-reindex-small');
      document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
      loadDirs();
      loadStats();
      showMsg('admin-dir-msg',
              _formatScanCompleteMsg(path, finalStatus), 'ok');
    });
  } catch {
    showMsg('admin-dir-msg', 'Network error.', 'err');
    btn.textContent = 'Re-Index';
    btn.disabled = false;
  }
}

/**
 * Build a "Scan complete" toast message that reflects what the scan
 * actually did.  Without ``last_plan`` we'd show bare "Scan complete"
 * even when the optimisation skipped every file — making a working
 * re-index look like a no-op (which is exactly what confused the user
 * who clicked Re-Index 3-4 times because nothing seemed to happen).
 *
 * Format examples:
 *   "Scan complete: 16027 unchanged, 1928 cleaned up"
 *   "Scan complete: 12 added, 14 unchanged"
 *   "Scan complete for ftp://…" (no plan available)
 */
function _formatScanCompleteMsg(path, status) {
  const plan = (status && status.last_plan) || {};
  // Only include the plan summary when it actually pertains to THIS
  // path — backend overwrites last_plan per scan, so for a
  // single-share Re-Index this is reliable.  For multi-share
  // operations the last finishing scan wins.
  const parts = [];
  if (plan.extract)       parts.push(`${plan.extract} extracted`);
  if (plan.mtime_refresh) parts.push(`${plan.mtime_refresh} refreshed`);
  if (plan.skip)          parts.push(`${plan.skip} unchanged`);
  if (plan.ghosts)        parts.push(`${plan.ghosts} cleaned up`);
  if (parts.length === 0) return `Scan complete for ${path}.`;
  return `Scan complete: ${parts.join(', ')}.`;
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

      // Drive the scan-flow viz from the live progress numbers.
      _ensureAdminViz();
      if (_vizScan) _vizScan.onProgress(s);

      // While a Rebuild Index is in progress, also push live numbers
      // into the Index section's status line so the user gets feedback
      // even if the Scan Progress section is scrolled out of view.
      // Without this, "Schema rebuilt. Scanning N folder(s)…" stays
      // static for the entire scan and looks frozen.
      if (_reindexActive && s.running) {
        const pct = s.pct || 0;
        const proc = (s.processed || 0).toLocaleString();
        const total = (s.total || 0).toLocaleString();
        const dirs = (s.current_dirs || []).length;
        showMsg('admin-index-msg',
                `Scanning ${pct}% — ${proc} / ${total} files`
                + (dirs > 1 ? ` (${dirs} shares in parallel)` : ''),
                'ok');
        // Reflect progress on the button label too — at a glance the
        // user sees "Rebuilding 23%" instead of static "Rebuilding...".
        const btn = document.getElementById('btn-admin-reindex');
        if (btn && btn.disabled) {
          btn.textContent = `Rebuilding ${pct}%…`;
        }
      }

      // Update the Pause/Resume button label + visibility to match
      // the current paused state.  Hidden when no scan is running so
      // the button doesn't linger on a stale section.
      const pauseBtn = document.getElementById('btn-scan-pause');
      if (pauseBtn) {
        pauseBtn.style.display = s.running ? '' : 'none';
        pauseBtn.textContent = s.paused ? 'Resume' : 'Pause';
        pauseBtn.classList.toggle('is-paused', !!s.paused);
        pauseBtn.dataset.paused = s.paused ? '1' : '0';
      }

      // Progress bar
      const fill = document.getElementById('scan-progress-fill');
      if (fill) fill.style.width = (s.running ? s.pct : 0) + '%';

      // Text \u2014 when paused, swap the prefix so the user sees
      // "Paused at N% (M/Total)" rather than "Scanning\u2026".
      const txt = document.getElementById('scan-progress-text');
      if (s.running) {
        const prefix = s.paused ? 'Paused at' : '';
        const count = `${s.processed.toLocaleString()} / ${s.total.toLocaleString()} files`;
        txt.textContent = prefix
          ? `${prefix} ${s.pct}% \u2014 ${count}`
          : `${s.pct}% \u2014 ${count}`;
      } else {
        txt.textContent = 'Discovering files\u2026';
      }

      // Current file
      const fileEl = document.getElementById('scan-progress-file');
      if (fileEl) fileEl.textContent = s.current_file || '';

      // Post-extract phase detection: when the backend sets
      // current_file to one of these phase markers, extract is
      // already done and the per-share queue list is stale (the
      // scan loop completed for each root but `_scan_count` won't
      // decrement until duplicate detection + aggregation refresh
      // finish, which can take minutes on a 270K-track library).
      // Hide the queue list during these phases so the user doesn't
      // see scan paths that aren't actually being scanned anymore.
      const phaseLabel = s.current_file || '';
      const inPostExtract = phaseLabel.startsWith('Detecting duplicates')
                         || phaseLabel.startsWith('Refreshing aggregations');

      // Queue list
      const qList = document.getElementById('scan-queue-list');
      if (qList) {
        let html = '';
        if (!inPostExtract) {
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
        }
        qList.innerHTML = html;
      }
    } else {
      // Scan finished
      if (section) section.style.display = 'none';
      stopScanPoller();
      // Fire all done callbacks.  Pass the final status object
      // (including ``last_plan``) so callbacks can show a meaningful
      // toast like "Skipped 16027, deleted 1928 ghosts" instead of
      // bare "Scan complete".
      while (_scanDoneCallbacks.length) {
        const cb = _scanDoneCallbacks.shift();
        try { cb(s); } catch { /* ignore */ }
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
    // The backend returns ``purging: true`` because the actual track delete
    // is async — there's no synchronous count to show.  Previously the UI
    // pretended to know (and always rendered "0 tracks deleted").  Tell
    // the truth.
    const purging = data.purging === true || data.tracks_deleted === undefined;
    const msg = purging
      ? 'Folder removed. Tracks are purging in the background.'
      : `Removed. ${data.tracks_deleted ?? 0} tracks deleted.`;
    showMsg('admin-dir-msg', msg, 'ok');
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

// ── Scan pause / resume ──────────────────────────────────────────────────────
//
// Single button toggles between Pause and Resume.  Reads the current
// paused state from its ``data-paused`` attribute (set by pollScanStatus
// when the scan_progress event comes in).  POST to the matching endpoint;
// the next poll updates the label so we don't need to optimistically
// flip here (and avoid a flicker if the API call fails).
document.getElementById('btn-scan-pause')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const paused = btn.dataset.paused === '1';
  btn.disabled = true;
  const wasLabel = btn.textContent;
  btn.textContent = paused ? 'Resuming…' : 'Pausing…';
  try {
    const path = paused ? '/admin/scan/resume' : '/admin/scan/pause';
    const res = await api(path, { method: 'POST' });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      showMsg('admin-dir-msg', d.detail || 'Pause/resume failed.', 'err');
      btn.textContent = wasLabel;
    }
    // On success the next pollScanStatus() will update label + state
    // from the broadcasted scan_progress event.
  } catch {
    showMsg('admin-dir-msg', 'Network error.', 'err');
    btn.textContent = wasLabel;
  } finally {
    btn.disabled = false;
  }
});


// ── Rebuild Index (full rescan of all dirs) ────────────────────────────────────

// While Rebuild Index is running, ``_reindexActive`` tells pollScanStatus
// to also push live progress into the admin-index-msg + button label \u2014
// without it, the user sees only the initial "Rebuilding schema and
// scanning all folders\u2026" forever (the Scan Progress section may be
// out of viewport when they clicked, so they have no other feedback
// that the multi-share scan is actually running).
let _reindexActive = false;

document.getElementById('btn-admin-reindex').addEventListener('click', async () => {
  const btn = document.getElementById('btn-admin-reindex');
  btn.textContent = 'Rebuilding...';
  btn.disabled = true;
  showMsg('admin-index-msg', 'Rebuilding schema and scanning all folders\u2026', 'info');
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
      showMsg('admin-index-msg', `Schema rebuilt. Scanning ${data.dirs.length} folder(s)\u2026`, 'info');
      _reindexActive = true;
      // Scroll the Scan Progress section into view so the user can
      // actually see the file count / current-file feedback while the
      // multi-share scan runs in parallel.
      const scanSection = document.getElementById('admin-scan-section');
      if (scanSection) {
        scanSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      pollScanDone((finalStatus) => {
        _reindexActive = false;
        btn.textContent = '\u21B5 Rebuild Index';
        btn.disabled = false;
        document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
        loadStats();
        loadDirs();
        showMsg('admin-index-msg',
                _formatRebuildCompleteMsg(finalStatus), 'ok');
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

function _formatRebuildCompleteMsg(status) {
  const plan = (status && status.last_plan) || {};
  // last_plan reflects the LAST scan to finish (one share); for a
  // multi-share rebuild we can't easily aggregate without backend
  // changes.  Show what we have, plus the totals from the final
  // progress snapshot.
  const total = status && status.total ? status.total : 0;
  const processed = status && status.processed ? status.processed : 0;
  if (total > 0) {
    return `Rebuild complete \u2014 ${processed.toLocaleString()} file(s) processed across all shares.`;
  }
  if (plan.skip || plan.mtime_refresh || plan.ghosts) {
    const parts = [];
    if (plan.extract)       parts.push(`${plan.extract} extracted`);
    if (plan.mtime_refresh) parts.push(`${plan.mtime_refresh} refreshed`);
    if (plan.skip)          parts.push(`${plan.skip} unchanged`);
    if (plan.ghosts)        parts.push(`${plan.ghosts} cleaned up`);
    return `Rebuild complete \u2014 ${parts.join(', ')} (last share).`;
  }
  return 'Rebuild complete.';
}

// ── Metadata repair (re-extract U+FFFD-tainted titles) ───────────────────────
//
// Three buttons:
//   Scan — counts candidates (cheap, in-memory; populates the preview).
//   Repair — kicks off the background task.
//   Cancel — visible only while a repair is running.
//
// Progress comes in via the ``soniqboom:repair-progress`` custom event
// (dispatched by app.js when the WS repair_progress message arrives).
// We also fall back to a 3 s poll in case the WS is down.

let _syncRepairStatus = null;   // set by setupRepairControls; open() calls it for admins

(function setupRepairControls() {
  const btnScan   = document.getElementById('btn-repair-scan');
  const btnStart  = document.getElementById('btn-repair-start');
  const btnCancel = document.getElementById('btn-repair-cancel');
  const chkTrackerOnly = document.getElementById('repair-tracker-only');
  const statusEl  = document.getElementById('admin-repair-status');
  const progWrap  = document.getElementById('admin-repair-progress');
  const progFill  = document.getElementById('repair-progress-fill');
  const progText  = document.getElementById('repair-progress-text');
  const progFile  = document.getElementById('repair-progress-file');

  if (!btnScan || !btnStart) return;  // section not in the DOM (older shell)

  let lastScanCount = 0;
  let pollHandle = null;
  let gameNamesRunning = false;     // a game-name run holds the shared task

  function showStatus(text, type = 'ok') {
    statusEl.dataset.gameNames = '';
    statusEl.textContent = text;
    statusEl.className   = `admin-msg ${type}`;
    statusEl.style.display = '';
  }

  function setRunningUI(running) {
    btnScan.disabled   = running;
    btnStart.disabled  = running || lastScanCount === 0;
    btnCancel.disabled = !running;
    btnCancel.style.display = running ? '' : 'none';
    progWrap.style.display  = running ? '' : 'none';
  }

  function _renderErrorBreakdown(p) {
    // Build a short readable summary of error categories from the
    // backend's ``error_reasons`` map + the first few sample paths.
    // The repair task tags each failure with a short reason key
    // (e.g. ``zip-missing``, ``local-missing``, ``remote-no-source``)
    // so the operator can tell at a glance whether the failures are
    // "files moved on disk" vs "FTP share unreachable" vs "zip member
    // missing from the archive".
    const reasons = p.error_reasons || {};
    const samples = p.error_samples || [];
    if (!Object.keys(reasons).length && !samples.length) return '';

    const parts = [];
    // Sort reasons by count desc so the dominant cause leads.
    const entries = Object.entries(reasons).sort((a, b) => b[1] - a[1]);
    // Escaped: reasons and file names come from the library (a file may be
    // NAMED like markup) and this is set as innerHTML.
    for (const [reason, count] of entries) {
      parts.push(`${count}× ${_escAttr(reason)}`);
    }
    let html = ` — ${parts.join(', ')}`;
    if (samples.length) {
      const shown = samples.slice(0, 3).map(s => {
        const base = (s.path || '').split('/').pop() || s.path;
        return `<code>${_escAttr(base)}</code>`;
      }).join(', ');
      const more = samples.length > 3 ? ` (+${samples.length - 3})` : '';
      html += `. Examples: ${shown}${more}`;
    }
    return html;
  }

  function renderProgress(p) {
    if (!p) return;
    // A game-name run (Metadata tab) shares the task: it only holds this
    // section's Repair back while it runs — its progress is shown there.
    if (p.kind === 'game-names') {
      gameNamesRunning = !!p.running;
      progWrap.style.display = 'none';
      btnCancel.style.display = 'none';
      btnStart.disabled = gameNamesRunning || lastScanCount === 0;
      // The waiting note never covers a Scan result shown meanwhile.
      const ours = statusEl.dataset.gameNames === '1';
      if (p.running && (ours || statusEl.style.display === 'none')) {
        showStatus('Game names are being read (Metadata tab) \u2014 Repair can start when that has finished.', 'info');
        statusEl.dataset.gameNames = '1';
      } else if (!p.running && ours) {
        statusEl.style.display = 'none';
        statusEl.dataset.gameNames = '';
      }
      if (!p.running && pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      return;
    }
    gameNamesRunning = false;
    statusEl.dataset.gameNames = '';
    if (p.running) {
      progWrap.style.display = '';
      progFill.style.width = `${p.pct || 0}%`;
      progText.textContent = `Repairing ${p.pct}% — ${p.processed}/${p.total}`
                           + ` (${p.repaired} fixed, ${p.errors} errors)`;
      progFile.textContent = p.current_file || '';
      setRunningUI(true);
    } else {
      // Finished or never started.  If we just transitioned from
      // running → not-running, show a one-shot summary with an
      // error breakdown when applicable.
      if (p.total > 0 && p.finished_at) {
        const tag = p.cancelled ? 'Cancelled' : 'Done';
        const base = `${tag} — repaired ${p.repaired} of ${p.processed} track(s)`
                   + ` (${p.errors} error${p.errors === 1 ? '' : 's'})`;
        const breakdown = p.errors > 0 ? _renderErrorBreakdown(p) : '';
        statusEl.innerHTML = base + breakdown + '.';
        statusEl.className = `admin-msg ${p.errors > 0 ? 'warn' : 'ok'}`;
        statusEl.style.display = '';
      }
      setRunningUI(false);
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
    }
  }

  async function fetchStatus() {
    try {
      const res = await api('/admin/metadata/repair-status');
      const p = await res.json();
      renderProgress(p);
    } catch (e) {
      // Network blip — keep polling, the next tick will retry.
    }
  }

  btnScan.addEventListener('click', async () => {
    btnScan.disabled = true;
    showStatus('Scanning index for garbled titles…', 'info');
    try {
      const res = await api('/admin/metadata/repair-scan', {
        method: 'POST',
        body: JSON.stringify({ tracker_only: chkTrackerOnly.checked }),
      });
      const data = await res.json();
      lastScanCount = data.count | 0;
      if (lastScanCount === 0) {
        showStatus('No garbled titles found in the index.', 'ok');
        btnStart.disabled = true;
      } else {
        const sample = (data.sample || []).slice(0, 5)
          .map(p => (p || '').split('/').pop()).join(', ');
        const more = lastScanCount > 5 ? `, +${lastScanCount - 5} more` : '';
        showStatus(
          `Found ${lastScanCount} track(s) with U+FFFD in metadata`
          + ` (e.g. ${sample}${more}). Click “Repair Now” to re-extract.`,
          'ok',
        );
        btnStart.disabled = gameNamesRunning;
      }
    } catch (e) {
      showStatus(`Scan failed: ${e.message || e}`, 'err');
    } finally {
      btnScan.disabled = false;
    }
  });

  btnStart.addEventListener('click', async () => {
    if (!confirm(
      `Re-extract metadata for ${lastScanCount} track(s)?\n\n`
      + 'Local files extract instantly. Remote (FTP/SMB) files re-download '
      + 'on the scan lane — this can take a while if many are on a slow share.'
    )) return;
    btnStart.disabled = true;
    showStatus('Starting…', 'info');
    try {
      const res = await api('/admin/metadata/repair-start', {
        method: 'POST',
        body: JSON.stringify({ tracker_only: chkTrackerOnly.checked }),
      });
      const data = await res.json();
      showStatus(`Repair started — processing ${data.total} track(s).`, 'ok');
      setRunningUI(true);
      // WS will drive progress; polling is just a safety net.
      if (pollHandle) clearInterval(pollHandle);
      pollHandle = setInterval(fetchStatus, 3000);
    } catch (e) {
      showStatus(`Could not start repair: ${e.message || e}`, 'err');
      setRunningUI(false);
    }
  });

  btnCancel.addEventListener('click', async () => {
    btnCancel.disabled = true;
    try {
      await api('/admin/metadata/repair-cancel', { method: 'POST' });
      showStatus('Cancel requested — finishing current file…', 'warn');
    } catch (e) {
      showStatus(`Cancel failed: ${e.message || e}`, 'err');
    }
  });

  // Live progress from WS broadcasts (preferred path).
  window.addEventListener('soniqboom:repair-progress', (ev) => {
    renderProgress(ev.detail);
  });

  // Synced each time an admin opens the panel (open()) — picks up a repair
  // started in another tab / by another admin, so the controls correctly
  // disable themselves and show progress.  Cheap (one GET; the endpoint is
  // in-memory).  Admins only: the endpoint is admin-only, and a listener's
  // 401 would put the sign-in screen over their Preferences.
  _syncRepairStatus = fetchStatus;
})();


// ── Backup status: the last known-good snapshot (library.json.prev) ───────────
async function loadBackupStatus() {
  const el = document.getElementById('backup-snapshot-status');
  if (!el) return;
  try {
    const res = await api('/admin/backup/status');
    const s = await res.json();
    const kg = s.known_good || {};
    if (!kg.exists) {
      el.textContent = 'Not captured yet — it appears after the next clean startup.';
      return;
    }
    const mb = kg.size / (1024 * 1024);
    const size = mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
    const when = kg.mtime ? new Date(kg.mtime * 1000).toLocaleString() : 'unknown time';
    el.innerHTML = `<code>library.json.prev</code> &middot; <strong>${size}</strong> &middot; captured ${when}`;
  } catch {
    el.textContent = 'Could not load backup status.';
  }
}

// ── Export / Import ───────────────────────────────────────────────────────────

document.getElementById('btn-admin-export').addEventListener('click', async () => {
  showMsg('admin-io-msg', 'Exporting...', 'info');
  try {
    const res = await fetch('/api/admin/export', {
      credentials: 'same-origin',
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

  showMsg('admin-io-msg', 'Importing...', 'info');
  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/admin/import', {
      method: 'POST',
      credentials: 'same-origin',
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
  // The conversion-cache panel lives in the same admin tab as renderer
  // status, so every load of this section refreshes both — covers the
  // "user opened admin and the Renderers tab is already active" case
  // that MutationObserver-on-aria-selected misses (no transition fires).
  try { loadConvCacheStats(); } catch (_) { /* defined later */ }
  try { loadSceneStatus(); } catch (_) { /* defined later */ }
  const names = ['ffmpeg', 'sidplayfp', 'fluidsynth', 'openmpt123',
                 'uade123', 'hvl2wav', 'gme', 'adplay',
                 'psgplay', 'ym2wav', 'sc68', 'zxtune123'];
  // Set all to loading state \u2014 ``renderer-loading`` adds a subtle pulse
  // animation so the user sees the row is actively probing rather than
  // frozen at "\u2026".
  names.forEach(n => {
    const el = document.getElementById(`renderer-icon-${n}`);
    if (!el) return;
    el.textContent = '\u2026';
    el.className = 'renderer-icon renderer-loading';
  });

  try {
    const res = await api('/admin/renderers');
    const data = await res.json();
    names.forEach(n => {
      const info = data[n];
      const iconEl = document.getElementById(`renderer-icon-${n}`);
      if (!iconEl) return;
      if (info && info.installed) {
        // File exists but the binary won't actually RUN \u2014 a system library it
        // links was upgraded out from under it (e.g. Homebrew bumped boost under
        // a prebuilt zxtune123 \u2192 dyld symbol-not-found abort).  Distinct from
        // "missing": the path is present, so the fix is reinstall/replace, not
        // install.  This is the case the old file-exists check false-greened.
        if (info.runs === false) {
          iconEl.textContent = '!';
          iconEl.className = 'renderer-icon renderer-warn';
          iconEl.title =
            `Found at ${info.path || '(configured path)'} but it fails to run \u2014 a `
            + 'system library it depends on was likely upgraded. Reinstall or '
            + 'replace this renderer (see the server log for the exact error).';
        // ffmpeg has a deeper feature audit \u2014 even when installed it can
        // be missing libmp3lame / libvorbis / DSD demuxers (Homebrew's
        // default bottle is a classic culprit).  Surface "warning" tier
        // so the user knows playback will fail for some formats even
        // though the binary itself exists.
        } else if (n === 'ffmpeg' && info.fully_capable === false) {
          iconEl.textContent = '!';
          iconEl.className = 'renderer-icon renderer-warn';
          iconEl.title =
            'ffmpeg is installed but missing: ' +
            (info.missing || []).join(', ') +
            '. Click "Fix ffmpeg" below to download a complete bundled copy.';
        } else {
          iconEl.textContent = '\u2713';
          iconEl.className = 'renderer-icon renderer-ok';
          iconEl.title = info.path || '';
        }
      } else {
        iconEl.textContent = '\u2717';
        iconEl.className = 'renderer-icon renderer-missing';
        // Bundled engines (ym2wav/hvl2wav) report a diagnostic sentence
        // in "path" even when not built yet (e.g. "builds on first use —
        // NO C++ compiler found") — surface it instead of discarding it.
        iconEl.title = (info && info.path) || 'Not found';
      }
    });
    // ffmpeg-specific banner: show a refetch CTA when the running binary
    // is incomplete (missing encoders / demuxers).  This addresses the
    // user-reported "Amperfy DSF \u2192 silence" pattern: Homebrew ffmpeg
    // can demux DSF but cannot encode to MP3 because libmp3lame isn't
    // in the default bottle, and Amperfy's transcode request fails
    // silently from the user's perspective.
    _renderFfmpegBanner(data.ffmpeg || {});
  } catch (err) {
    // Replace the loading "..." with a real error state so the user isn't
    // left staring at a frozen ellipsis when the renderer probe fails.
    names.forEach(n => {
      const iconEl = document.getElementById(`renderer-icon-${n}`);
      if (iconEl) {
        iconEl.textContent = '!';
        iconEl.className = 'renderer-icon renderer-missing';
        iconEl.title = "Couldn't reach /admin/renderers \u2014 click the refresh button to retry.";
      }
    });
    console.warn('loadRendererStatus failed:', err);
  }
}

// Wire the refresh button so the `!` tooltip's "click refresh" advice is
// actually actionable.  Defensive guard \u2014 the element only exists once
// the admin panel template renders.
document.getElementById('btn-renderer-refresh')?.addEventListener(
  'click', () => loadRendererStatus(),
);

// \u2500\u2500 ffmpeg fix-it banner \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// Shown when the running ffmpeg is incomplete (missing libmp3lame /
// libvorbis / DSD demuxers).  Offers a one-click download of the
// SoniqBoom-vetted static build that has every encoder + demuxer we use.
function _renderFfmpegBanner(ff) {
  // Banner lives directly under the renderer grid; the static markup in
  // index.html provides ``#ffmpeg-banner`` so we don't need to inject
  // anything if it's missing.
  const banner = document.getElementById('ffmpeg-banner');
  if (!banner) return;
  const missing = Array.isArray(ff.missing) ? ff.missing : [];
  const incomplete = !ff.installed || ff.fully_capable === false;
  if (!incomplete) {
    banner.classList.add('hidden');
    banner.innerHTML = '';
    return;
  }
  // Choose copy based on what's wrong + whether a bundled copy already
  // exists locally (then we just need to switch to it) versus whether we
  // need to download from upstream.
  const usingBundled = ff.using_bundled === true;
  const bundledPresent = ff.bundled_present === true;
  let msg;
  let cta;
  if (!ff.installed) {
    msg = 'ffmpeg is not installed. Without it, no transcoded playback works.';
    cta = 'Download bundled ffmpeg';
  } else if (usingBundled) {
    // We're already using the bundled copy and it's incomplete \u2014 that
    // means the bundled binary is stale / from a previous SoniqBoom
    // release that didn't ship the new encoder requirements.
    msg = `The bundled ffmpeg is missing: ${missing.join(', ')}. ` +
          'Re-download to refresh.';
    cta = 'Re-download ffmpeg';
  } else if (bundledPresent) {
    // Bundled exists but we're using system; system is incomplete.
    msg = `The system ffmpeg is missing: ${missing.join(', ')}. ` +
          'A complete bundled copy is already installed \u2014 restart to ' +
          'use it, or re-download to refresh.';
    cta = 'Re-download + switch';
  } else {
    msg = `The system ffmpeg is missing: ${missing.join(', ')}. ` +
          'Download a complete bundled copy with full DSD + lossy ' +
          'encoder support.';
    cta = 'Download bundled ffmpeg';
  }
  banner.classList.remove('hidden');
  banner.innerHTML = '';
  const text = document.createElement('div');
  text.className = 'ffmpeg-banner-text';
  text.textContent = msg;
  banner.appendChild(text);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ffmpeg-banner-btn';
  btn.textContent = cta;
  btn.addEventListener('click', () => _fetchFfmpeg(btn, banner));
  banner.appendChild(btn);
}

async function _fetchFfmpeg(btn, banner) {
  // One-click download + activation.  The server installs into the data
  // dir's ``bin/`` and updates ``settings.ffmpeg_path`` in-memory so
  // subsequent transcodes pick up the new binary without restart.
  // Confirm because a refetch is a ~60 MB download and overwrites the
  // currently-installed binary.
  const ok = window.confirm(
    'Download the bundled ffmpeg (~60 MB)?\n\n' +
    'This installs a static build with full DSD demuxer support ' +
    '(dsf / dff / wsd) plus libmp3lame, libvorbis, and libopus encoders ' +
    'into the SoniqBoom data directory.\n\n' +
    'Existing transcoded plays will continue; new requests will use the ' +
    'new binary immediately.'
  );
  if (!ok) return;
  btn.disabled = true;
  btn.textContent = 'Downloading\u2026';
  try {
    const r = await api('/admin/ffmpeg/fetch', { method: 'POST' });
    const j = await r.json();
    btn.textContent = 'Downloaded \u2713';
    if (window.Toast) {
      window.Toast.info(
        `ffmpeg installed at ${j.active_path || j.path || j.dest}. ` +
        `Transcodes now use the bundled binary.`,
      );
    }
    // Re-probe so the icon flips green + banner hides.
    setTimeout(loadRendererStatus, 250);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Download bundled ffmpeg';
    if (window.Toast) {
      window.Toast.error(
        `ffmpeg download failed: ${err.message || err}`,
      );
    } else {
      banner.appendChild(Object.assign(document.createElement('div'), {
        className: 'ffmpeg-banner-error',
        role: 'alert',
        textContent: `Download failed: ${err.message || err}`,
      }));
    }
  }
}

// \u2500\u2500 Conversion cache panel \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
function _fmtBytes(n) {
  if (n == null || isNaN(n)) return '\u2014';
  if (n < 1024)            return `${n} B`;
  if (n < 1024 ** 2)       return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3)       return `${(n / (1024 ** 2)).toFixed(1)} MB`;
  return `${(n / (1024 ** 3)).toFixed(2)} GB`;
}

async function loadConvCacheStats() {
  // Mount the cache-cascade viz once the Renderers cache section is in play.
  _ensureAdminViz();
  try {
    const r = await api('/admin/cache/conversion');
    if (!r.ok) return;
    const s = await r.json();
    const total = s.total_bytes || 0;
    const max   = s.max_bytes   || 0;
    const pct   = max ? Math.min(100, (total / max) * 100) : 0;
    const bar   = document.getElementById('cache-fill-bar-inner');
    const lbl   = document.getElementById('cache-fill-label');
    const pinned = s.pinned_count || 0;
    // Build the screen-reader-friendly description once so the visible
    // label and aria-valuetext stay in lock-step.
    const pinSuffix = pinned ? `, ${pinned} pinned` : '';
    const valueText =
      `${_fmtBytes(total)} of ${_fmtBytes(max)} used, ` +
      `${Math.round(pct)} percent (${s.entry_count} entries${pinSuffix})`;
    if (bar) {
      bar.style.width = `${pct}%`;
      // Colour band: low / mid / high.  Threshold colour alone is NOT
      // sufficient to communicate state (WCAG 1.4.1) \u2014 see the label
      // below which carries the same info as plain text.
      bar.dataset.fill = pct > 90 ? 'high' : (pct > 70 ? 'mid' : 'low');
      const wrap = document.getElementById('cache-fill-bar');
      if (wrap) {
        wrap.setAttribute('aria-valuenow', String(Math.round(pct)));
        wrap.setAttribute('aria-valuetext', valueText);
      }
    }
    if (lbl) {
      lbl.textContent =
        `${_fmtBytes(total)} used of ${_fmtBytes(max)} (${Math.round(pct)} %) \u00b7 ` +
        `${s.entry_count} entries${pinSuffix}`;
    }
    const by = s.by_type_bytes || {};
    for (const t of CONV_CACHE_TYPES) {
      const el = document.getElementById(`cache-bytes-${t}`);
      if (el) el.textContent = _fmtBytes(by[t] || 0);
    }
  } catch (_) { /* non-critical */ }
}

// Per-type rows in the cache breakdown — one entry per backend
// FORMAT_TYPES key (conversion_cache.py); ids are cache-bytes-<key>.
// Shared by loadConvCacheStats and the SSE _applyCacheStats path so the
// two can never drift apart again.
const CONV_CACHE_TYPES = ['sid', 'midi', 'tracker', 'uade', 'hvl', 'gme',
                          'adlib', 'imf', 'sndh', 'ym', 'sc68', 'psf',
                          'transcoded'];

document.getElementById('btn-cache-refresh')?.addEventListener(
  'click', loadConvCacheStats,
);

// Generic inline cross-tab links: any element with [data-tab-target="..."]
// switches the admin panel to the referenced tab and scrolls to the matching
// section.  Keeps the cache-section pointer in System -> Renderers cheap.
document.addEventListener('click', (e) => {
  const el = (e.target instanceof HTMLElement) && e.target.closest('[data-tab-target]');
  if (!el) return;
  e.preventDefault();
  const target = el.getAttribute('data-tab-target');
  const tab = document.querySelector(`.admin-tab[data-tab="${target}"]`);
  if (tab instanceof HTMLElement) tab.click();
});

document.getElementById('btn-conv-cache-apply')?.addEventListener(
  'click', async () => {
    const input = document.getElementById('setting-conv-cache-mb');
    if (!input) return;
    const raw = parseInt(input.value, 10);
    if (!Number.isFinite(raw) || raw < 256 || raw > 102400) {
      Toast.error('Cache size must be between 256 MB and 100 GB.');
      return;
    }
    try {
      const res = await api('/admin/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversion_cache_max_mb: raw }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      Toast.info(`Cache budget set to ${raw} MB.  Eviction runs immediately if you lowered it.`);
      // Re-fetch stats so the fill bar reflects the new max and any
      // post-shrink byte count.
      loadConvCacheStats();
    } catch (e) {
      Toast.error(`Couldn't update cache size: ${e.message || e}`);
    }
  },
);

document.getElementById('btn-cache-clear-transcoded')?.addEventListener(
  'click', async () => {
    const ok = await styledConfirm(
      'Clear all cached DSD / ALAC / AIFF transcodes? SID, MIDI, and tracker renders will be kept.',
      { title: 'Clear transcoded cache', okLabel: 'Clear', dangerColor: false },
    );
    if (!ok) return;
    try {
      const r = await api('/admin/cache/clear-conversion?types=transcoded', { method: 'POST' });
      const j = await r.json();
      Toast.info(`Cleared ${j.deleted_files || 0} transcoded entries.`);
      loadConvCacheStats();
    } catch { Toast.error('Failed to clear transcoded cache.'); }
  },
);

document.getElementById('btn-cache-clear-all')?.addEventListener(
  'click', async () => {
    const ok = await styledConfirm(
      'Clear the entire conversion cache? SID, MIDI, tracker, and transcoded renders will all be regenerated on next play.',
      { title: 'Clear all conversion cache', okLabel: 'Clear all' },
    );
    if (!ok) return;
    try {
      const r = await api('/admin/cache/clear-conversion?types=all', { method: 'POST' });
      const j = await r.json();
      Toast.info(`Cleared ${j.deleted_files || 0} entries.`);
      loadConvCacheStats();
    } catch { Toast.error('Failed to clear conversion cache.'); }
  },
);

// ── Live SSE feed for the cache fill panel ──────────────────────────────
// Keep a single EventSource alive while the Renderers tab is visible and
// the admin overlay is open.  Watching the bar fill in real time as a
// playlist warms the cache is the kind of "I see the system working"
// feedback that turns a passive status display into a live system.
let _cacheStream = null;

function _applyCacheStats(s) {
  // SSE error payloads ({"error": ...}) or malformed frames must not
  // zero the fill bar and render "undefined entries" — ignore anything
  // without real numbers and keep showing the last good stats.
  if (!s || typeof s.total_bytes !== 'number'
        || typeof s.entry_count !== 'number') return;
  const total = s.total_bytes || 0;
  const max   = s.max_bytes   || 0;
  const pct   = max ? Math.min(100, (total / max) * 100) : 0;
  const bar   = document.getElementById('cache-fill-bar-inner');
  const lbl   = document.getElementById('cache-fill-label');
  const pinned = s.pinned_count || 0;
  const pinSuffix = pinned ? `, ${pinned} pinned` : '';
  const valueText =
    `${_fmtBytes(total)} of ${_fmtBytes(max)} used, ` +
    `${Math.round(pct)} percent (${s.entry_count} entries${pinSuffix})`;
  if (bar) {
    bar.style.width = `${pct}%`;
    bar.dataset.fill = pct > 90 ? 'high' : (pct > 70 ? 'mid' : 'low');
    const wrap = document.getElementById('cache-fill-bar');
    if (wrap) {
      wrap.setAttribute('aria-valuenow', String(Math.round(pct)));
      wrap.setAttribute('aria-valuetext', valueText);
    }
  }
  if (lbl) {
    lbl.textContent =
      `${_fmtBytes(total)} used of ${_fmtBytes(max)} (${Math.round(pct)} %) · ` +
      `${s.entry_count} entries${pinSuffix}`;
  }
  const by = s.by_type_bytes || {};
  for (const t of CONV_CACHE_TYPES) {
    const el = document.getElementById(`cache-bytes-${t}`);
    if (el) el.textContent = _fmtBytes(by[t] || 0);
  }
}

function _startCacheStream() {
  if (_cacheStream) return;
  try {
    _cacheStream = new EventSource('/api/admin/cache/conversion/stream', {
      withCredentials: true,
    });
    _cacheStream.onmessage = (ev) => {
      try { _applyCacheStats(JSON.parse(ev.data)); } catch (_) {}
    };
    _cacheStream.onerror = () => {
      // EventSource auto-reconnects on error — close it ourselves only
      // when the panel goes away (handled by _stopCacheStream).  But if
      // it keeps erroring (e.g. server gone), bail out so we don't pin
      // a reconnect loop forever.
      if (_cacheStream && _cacheStream.readyState === EventSource.CLOSED) {
        _cacheStream = null;
      }
    };
  } catch (_) { /* EventSource unsupported — fall back to polling */ }
}

function _stopCacheStream() {
  if (_cacheStream) {
    _cacheStream.close();
    _cacheStream = null;
  }
}

// Expose loadConvCacheStats so the renderer-status loader (and any future
// "panel just opened" caller) can chain it without overwriting function
// declarations.  Window-scoped so a freestanding script tag in index.html
// (or a regression test) can poke it.
window.loadConvCacheStats = loadConvCacheStats;

// Refresh on tab activation too — fires when the user clicks the
// Renderers tab while the admin overlay is already open.  Re-probes
// both the cache panel and the renderer-status grid so installing a
// renderer (sidplayfp, fluidsynth) and clicking the tab actually
// reflects the new state without closing the panel.  Also opens the
// live SSE stream so the fill bar animates as the cache changes.
document.querySelectorAll('.admin-tab[data-tab="tab-renderers"]').forEach(t => {
  t.addEventListener('click', () => {
    setTimeout(loadConvCacheStats, 0);
    setTimeout(loadRendererStatus, 0);
    _startCacheStream();
  });
});

// Close the stream when leaving the Renderers tab — every other tab
// click switches us away.  Polling for the active tab on each click is
// cheaper than wiring a MutationObserver here.
document.querySelectorAll('.admin-tab:not([data-tab="tab-renderers"])').forEach(t => {
  t.addEventListener('click', _stopCacheStream);
});

// Close the stream when the admin overlay closes — otherwise it pins
// an open server-side connection indefinitely.
document.getElementById('btn-admin-close')?.addEventListener('click', _stopCacheStream);

// ── Soundfont Management ─────────────────────────────────────────────────────

// Curated soundfont marketplace (E-16).  Categorised so the UI can
// group by use-case.  Each entry is a one-click install via
// POST /admin/soundfonts/download.  Sizes are approximate (the actual
// download is reported after fetch).
const KNOWN_SOUNDFONTS = [
  // ── General MIDI ──────────────────────────────────────────────────────
  {
    name: "GeneralUser_GS.sf2",
    label: "GeneralUser GS",
    category: "General MIDI",
    description: "Best size/quality ratio. Excellent all-around GM soundfont.",
    size: "30 MB",
    url: "https://www.dropbox.com/s/4x27l49kxcwamp5/GeneralUser_GS_v1.471.sf2?dl=1",
    license: "Free (attribution)",
  },
  {
    name: "FluidR3_GM.sf2",
    label: "FluidR3 GM",
    category: "General MIDI",
    description: "Ships with FluidSynth. Solid general MIDI soundfont.",
    size: "141 MB",
    url: "https://sourceforge.net/projects/pianobooster/files/pianobooster/1.0.0/FluidR3_GM.sf2/download",
    license: "MIT",
  },
  {
    name: "MuseScore_General.sf2",
    label: "MuseScore General",
    category: "General MIDI",
    description: "Highest quality. Rich, natural instrument samples from MuseScore.",
    size: "350 MB",
    url: "https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/MuseScore_General.sf2",
    license: "MIT",
  },
  {
    name: "Timbres_of_Heaven.sf2",
    label: "Timbres of Heaven",
    category: "General MIDI",
    description: "Lush orchestral sounds with excellent pianos and strings. Vorbis-compressed.",
    size: "15 MB",
    url: "https://archive.org/download/toh-gmgsxg/Timbres%20Of%20Heaven%20GM_GS_XG_SFX%20V%203.4%20Final_Vorbis.sf2",
    license: "Free",
  },
  // ── Retro / Game ──────────────────────────────────────────────────────
  {
    name: "TimGM6mb.sf2",
    label: "TimGM6mb",
    category: "Retro & game",
    description: "Tiny GM soundfont (~6 MB). Authentic 'old MIDI' character.",
    size: "6 MB",
    url: "https://musical-artifacts.com/artifacts/4146/TimGM6mb.sf2",
    license: "GPL",
  },
  {
    name: "SC-55.sf2",
    label: "Roland SC-55 (Patch95)",
    category: "Retro & game",
    description: "Roland Sound Canvas SC-55 emulation — the '90s MIDI sound.",
    size: "32 MB",
    url: "https://musical-artifacts.com/artifacts/2986/SC-55.sf2",
    license: "Free (non-commercial)",
  },
  // ── Piano ─────────────────────────────────────────────────────────────
  {
    name: "Salamander_Grand_Piano.sf2",
    label: "Salamander Grand Piano",
    category: "Piano",
    description: "High-quality Yamaha C5 grand piano samples by Alexander Holm.",
    size: "120 MB",
    url: "https://musical-artifacts.com/artifacts/1281/SalamanderGrandPianoV3_44.1khz16bit.sf2",
    license: "CC-BY",
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
        row.querySelector('.sf-delete').addEventListener('click', () => deleteSoundfont(sf.name, sf.active));
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
  // Group by category so users can scan the marketplace by use-case
  // (General MIDI / Retro / Piano).  Entries without a category land
  // under "Other" to keep older catalog rows compatible.
  const groups = new Map();
  for (const sf of KNOWN_SOUNDFONTS) {
    const cat = sf.category || 'Other';
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(sf);
  }
  for (const [cat, items] of groups) {
    const hdr = document.createElement('div');
    hdr.className = 'sf-known-cat-hdr';
    hdr.textContent = cat;
    container.appendChild(hdr);
    items.forEach(sf => {
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

async function deleteSoundfont(name, isActive = false) {
  // Deleting the active soundfont silently broke MIDI playback for the user
  // until they noticed and re-picked one — make the consequence explicit.
  const prompt = isActive
    ? `Delete the active soundfont "${name}"?\n\nMIDI playback will be unavailable until another soundfont is selected.`
    : `Delete soundfont "${name}"?`;
  const ok = await styledConfirm(prompt, { title: 'Delete Soundfont', okLabel: 'Delete' });
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
  // Check whether an upload would silently overwrite an existing soundfont
  // and ask first — otherwise users lose customised fonts to a same-name
  // upload with no warning.
  try {
    const listRes = await api('/admin/soundfonts');
    if (listRes.ok) {
      const { soundfonts = [] } = await listRes.json();
      // Case-insensitive: APFS and exFAT are case-insensitive by default on
      // macOS, so "MyFont.sf2" silently replaces "myfont.sf2".
      const uploadName = file.name.toLowerCase();
      if (soundfonts.some(s => (s.name || '').toLowerCase() === uploadName)) {
        const ok = await styledConfirm(
          `A soundfont named "${file.name}" already exists.  Replace it?`,
          { title: 'Replace Soundfont', okLabel: 'Replace' },
        );
        if (!ok) return;
      }
    }
  } catch {
    // List check is best-effort — proceed with the upload either way.
  }
  showMsg('admin-sf-msg', `Uploading ${file.name}...`, 'info');
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
  showMsg('admin-sf-msg', `Downloading ${name}... This may take a while.`, 'info');

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
  if (!el) return;
  if (!text) { el.style.display = 'none'; return; }
  el.className = `admin-msg ${type}`;
  el.textContent = text;
  el.style.display = 'block';
  // Make every status surface announcable to screen readers.  ``role="alert"``
  // for errors triggers immediate announcement; ``polite`` for success
  // waits for the user to be idle so it doesn't interrupt mid-task (WCAG 4.1.3).
  el.setAttribute('role', type === 'err' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'err' ? 'assertive' : 'polite');
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
  showMsg('admin-cache-msg', 'Clearing art cache...', 'info');
  try {
    const res = await api('/admin/cache/clear-art', { method: 'POST' });
    const d = await res.json();
    // Backend now keeps going past per-file failures and reports the count.
    // Surface partial-failure cases instead of pretending everything cleared.
    const failed = d.failed || 0;
    if (failed) {
      const sample = (d.failed_samples || []).slice(0, 3).join('; ');
      showMsg(
        'admin-cache-msg',
        `Cleared ${d.cleared} files. ${failed} could not be removed${sample ? ` (e.g. ${sample})` : ''}.`,
        'warn',
      );
    } else {
      showMsg('admin-cache-msg', `Cleared ${d.cleared} files.`, 'ok');
    }
    loadDiskUsage();
  } catch { showMsg('admin-cache-msg', 'Error clearing cache.', 'err'); }
});

document.getElementById('btn-clear-waveforms')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Clear all waveform data? Waveforms will be regenerated on next scan.',
    { title: 'Clear Waveforms', okLabel: 'Clear' }
  );
  if (!ok) return;
  showMsg('admin-cache-msg', 'Clearing waveforms...', 'info');
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
  showMsg('admin-cache-msg', 'Clearing remote cache...', 'info');
  try {
    const res = await api('/admin/cache/clear-remote', { method: 'POST' });
    const d = await res.json();
    showMsg('admin-cache-msg', `Cleared ${d.cleared} cached files.`, 'ok');
    loadDiskUsage();
  } catch { showMsg('admin-cache-msg', 'Error clearing remote cache.', 'err'); }
});

document.getElementById('btn-clear-zip-extract')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Clear extracted-from-ZIP audio files? Tracks inside archives will be re-extracted on next play. Files currently streaming are kept until playback ends.',
    { title: 'Clear ZIP Extract Cache', okLabel: 'Clear' }
  );
  if (!ok) return;
  showMsg('admin-cache-msg', 'Clearing ZIP extract cache...', 'info');
  try {
    const res = await api('/admin/cache/clear-zip-extract', { method: 'POST' });
    const d = await res.json();
    const deferred = d.deferred ? `, ${d.deferred} deferred (in use)` : '';
    const failed = d.failed ? `, ${d.failed} failed` : '';
    showMsg('admin-cache-msg', `Cleared ${d.cleared} extracted files${deferred}${failed}.`, 'ok');
    loadDiskUsage();
  } catch { showMsg('admin-cache-msg', 'Error clearing ZIP extract cache.', 'err'); }
});

document.getElementById('btn-clear-lyrics-cache')?.addEventListener('click', async () => {
  showMsg('admin-cache-msg', 'Clearing lyrics cache...', 'info');
  try {
    const res = await api('/admin/cache/clear-lyrics', { method: 'POST' });
    const d = await res.json();
    showMsg('admin-cache-msg', `Cleared ${d.cleared} cached lyrics.`, 'ok');
    const countEl = document.getElementById('lyrics-cache-count');
    if (countEl) countEl.textContent = '';
  } catch { showMsg('admin-cache-msg', 'Error clearing lyrics cache.', 'err'); }
});

// "Save fetched lyrics to files" lives on the Metadata tab and saves on toggle
// (no separate Save button there), so persist immediately on change.
document.getElementById('setting-lyrics-writeback')?.addEventListener('change', async (e) => {
  try {
    await api('/admin/settings', {
      method: 'PUT',
      body: JSON.stringify({ lyrics_writeback: !!e.target.checked }),
    });
    showMsg('md-lyrics-msg', e.target.checked
      ? 'On — lyrics found online will be saved into local files that have none.'
      : 'Off — files are not modified.', 'ok');
  } catch {
    showMsg('md-lyrics-msg', 'Could not save the setting.', 'err');
  }
});

// Server toggles that save as soon as they change (they sit away from the
// Settings grid's Save button).  Each checkbox ships ``disabled`` and is enabled
// by loadSettings only once the server has told us its real value, so a failed
// load can never post the HTML default.  ``pending``: message while the request
// runs (default "Saving…"); ``describe(on, r)``: the success message, from the
// PUT's JSON result; ``done()``: called after the request, whatever its outcome
// (e.g. to re-read a status line).  While one request is in flight further clicks are undone
// (``aria-disabled`` dims the box but keeps focus on it).  If the request fails,
// the stored value is read back rather than assumed: the server may have saved
// the choice and only lost contact during a long album pass.
function _bindServerToggle(id, key, msgId, { describe, pending, done } = {}) {
  const cb = document.getElementById(id);
  if (!cb) return;
  let inFlight = false;
  cb.addEventListener('change', async () => {
    const on = cb.checked;
    if (inFlight) { cb.checked = !on; return; }   // one request at a time; keeps focus
    inFlight = true;
    cb.setAttribute('aria-busy', 'true');
    cb.setAttribute('aria-disabled', 'true');
    showMsg(msgId, pending ? pending(on) : 'Saving\u2026', 'info');
    try {
      const res = await api('/admin/settings', {
        method: 'PUT', body: JSON.stringify({ [key]: on }),
      });
      const r = await res.json().catch(() => ({}));
      if (r.album_pass_error) {
        // The choice itself was saved; only the pass failed.
        showMsg(msgId, `${on ? 'On' : 'Off'} \u2014 saved, but applying it failed: ${r.album_pass_error}`, 'err');
      } else if (r.started === true) {
        // The server runs the album pass in the background.
        showMsg(msgId, `${on ? 'On' : 'Off'} \u2014 saved; updating albums in the background\u2026`, 'info');
      } else {
        showMsg(msgId, describe ? describe(on, r) : `${on ? 'On' : 'Off'} \u2014 saved.`, 'ok');
      }
      // Albums changed on the server: the open library view refreshes (app.js).
      // Every track written — albums and game names alike.
      const changed = (Number(r.folder_tracks_updated) || 0) + (Number(r.modland_tracks_updated) || 0)
        + (Number(r.archive_tracks_updated) || 0);
      if (changed > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    } catch (err) {
      // Show what the server actually stored, not what we hoped.
      let stored = null;
      try {
        const st = await (await api('/admin/settings')).json();
        if (typeof st[key] === 'boolean') stored = st[key];
      } catch { /* still unreachable */ }
      cb.checked = stored === null ? !on : stored;
      if (stored === on) {
        showMsg(msgId, `${on ? 'On' : 'Off'} \u2014 saved, but the page lost contact while the server was applying it. Reopen Settings later to see the result.`, 'err');
      } else {
        showMsg(msgId, `Could not save the setting: ${err.message}`, 'err');
      }
    } finally {
      inFlight = false;
      cb.removeAttribute('aria-busy');
      cb.removeAttribute('aria-disabled');
      if (done) done();
    }
  });
}
const _fmtCount = v => Number(v || 0).toLocaleString();
// Metadata tab — the retro-album options.  The server acts on a change at once:
// ON fills empty albums now, OFF removes only the albums that option added
// (never a tag or a hand edit).
_bindServerToggle('setting-retro-album-folder', 'retro_album_from_folder', 'md-retro-album-msg', {
  pending: on => on ? 'Saving \u2014 filling albums\u2026' : 'Saving \u2014 removing the albums it added\u2026',
  describe: (on, r) => {
    if (on && 'folder_albums_filled' in r) return `On \u2014 ${_fmtCount(r.folder_albums_filled)} retro track(s) got an album from their folder name.`;
    if (!on && 'folder_albums_reverted' in r) return `Off \u2014 removed ${_fmtCount(r.folder_albums_reverted)} folder-name album(s).`;
    return on ? 'On.' : 'Off.';                      // value was already set
  },
});
_bindServerToggle('setting-modland-filename-game', 'modland_filename_game', 'admin-scene-msg', {
  pending: on => on ? 'Saving \u2014 filling albums\u2026' : 'Saving \u2014 removing the albums it added\u2026',
  describe: (on, r) => {
    if (!on) return 'modland_albums_reverted' in r
      ? `Off \u2014 removed ${_fmtCount(r.modland_albums_reverted)} album(s) guessed from Modland file names.`
      : 'Off.';
    if ('modland_albums_filled' in r) return `On \u2014 Modland index applied: ${_fmtCount(r.modland_albums_filled)} album(s) filled or updated.`;
    return 'On \u2014 used the next time you apply the Modland index.';
  },
});
_bindServerToggle('setting-game-from-archive', 'game_from_archive_name', 'md-game-titles-msg', {
  pending: on => on ? 'Saving \u2014 naming games from archive names\u2026' : 'Saving \u2014 removing the names it added\u2026',
  describe: (on, r) => {
    if ('archive_tracks_updated' in r) {
      return on
        ? `On \u2014 ${_gtNamedText(r.archive_games_primary, r.archive_games_named)}.`
        : `Off \u2014 removed ${_fmtCount(r.archive_tracks_updated)} game name(s) taken from archive names.`;
    }
    return on ? 'On.' : 'Off.';                      // value was already set
  },
  done: () => loadGameTitlesStatus(),
});
// Renderers tab — Playback preparation.
_bindServerToggle('setting-render-prewarm', 'render_prewarm', 'admin-playprep-msg', {
  describe: on => on
    ? 'On \u2014 the next retro or Amiga track is prepared while the current one plays.'
    : 'Off \u2014 retro and Amiga tracks are rendered when they start; Amiga, HVL, SC68 and PSF tracks show their length once they have played.',
});
_bindServerToggle('setting-uade-vu-meters', 'uade_vu_meters', 'admin-playprep-msg', {
  describe: on => on
    ? 'On \u2014 new Amiga tracks get per-voice meters shortly after they start.'
    : 'Off \u2014 new Amiga tracks show the spectrum meter.',
});
// System tab — Subsonic card.
_bindServerToggle('setting-subsonic-folder-albums', 'subsonic_folder_albums', 'subsonic-folder-albums-msg', {
  describe: on => on
    ? 'On \u2014 Subsonic apps list album-less tracks as albums named after their folder.'
    : 'Off \u2014 album-less tracks are reached by browsing folders in Subsonic apps.',
});

// Metadata tab — re-read the game name from SPC / NSF / GBS / VGM headers into
// the album, and the GAME tag of other music files into the game
// (POST /admin/metadata/backfill-game-albums).  It runs on the shared repair
// task (progress ``kind: "game-names"``): progress arrives as
// ``soniqboom:repair-progress`` (app.js, from the WebSocket), with a slow
// repair-status poll in case the socket is down; a game-name run already going
// (started automatically, in another tab, or before a reload) is picked up
// when the panel opens (_syncGameTagStatus) or its progress arrives.  Cancel
// stops it after the current file (POST /admin/metadata/repair-cancel).
let _syncGameTagStatus = null;
(function _wireGameTagBackfill() {
  const btn = document.getElementById('btn-md-game-tags');
  if (!btn) return;
  const btnCancel = document.getElementById('btn-md-game-tags-cancel');
  const chkRemote = document.getElementById('md-game-tags-remote');
  const MSG = 'md-game-tags-msg';
  let watching = false, poll = null, deferred = 0, refused = false;
  const skipped = () => deferred
    ? ` ${_fmtCount(deferred)} file(s) in a music folder that is offline were skipped.` : '';
  const setRunning = (on) => {
    btn.disabled = on;
    if (btnCancel) { btnCancel.style.display = on ? '' : 'none'; btnCancel.disabled = !on; }
  };
  const finish = (p) => {
    watching = false;
    clearInterval(poll); poll = null;
    setRunning(false);
    if (p && p.finished_at) {
      if (Number(p.repaired) > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
      const errs = p.errors ? `; ${_fmtCount(p.errors)} could not be read` : '';
      showMsg(MSG, `${p.cancelled ? 'Cancelled' : 'Done'} \u2014 updated ${_fmtCount(p.repaired)} of ${_fmtCount(p.processed)} file(s)${errs}.${skipped()}`,
              p.errors || deferred ? 'warn' : 'ok');
    } else {
      showMsg(MSG, '');
    }
  };
  const onProgress = (p) => {
    if (!watching || !p) return;
    // Another kind of run on the shared task: the one followed has ended
    // (its last event was missed) — stop, never show or cancel that one.
    if (p.kind && p.kind !== 'game-names') { finish(null); return; }
    if (p.running) showMsg(MSG, `Reading ${_fmtCount(p.processed)} / ${_fmtCount(p.total)} file(s)\u2026`, 'info');
    else finish(p);
  };
  // Follow the running game-name task (idempotent; admins only — the status
  // endpoint is admin-only).  A refused poll (signed out, role changed) ends
  // the watch instead of polling until a reload.
  const adopt = () => {
    if (watching || refused || !Auth.isAdmin) return;
    watching = true;
    setRunning(true);
    poll = setInterval(async () => {
      try { onProgress(await (await api('/admin/metadata/repair-status')).json()); }
      catch (err) {
        if (err.status === 401 || err.status === 403) { refused = true; finish(null); }
      }
    }, 3000);
  };
  window.addEventListener('soniqboom:repair-progress', (ev) => {
    const p = ev.detail;
    if (p && p.running && p.kind === 'game-names' && !watching) { deferred = 0; adopt(); }
    onProgress(p);
  });
  _syncGameTagStatus = async () => {
    if (!Auth.isAdmin) return;
    refused = false;                                // opening the panel re-checks
    try {
      const p = await (await api('/admin/metadata/repair-status')).json();
      if (p && p.running && p.kind === 'game-names') {
        if (!watching) { deferred = 0; adopt(); }
        onProgress(p);
      }
    } catch { /* the panel still works; the next progress event adopts the run */ }
  };
  btn.addEventListener('click', async () => {
    refused = false;                                // the admin is here, clicking
    setRunning(true);
    if (btnCancel) btnCancel.disabled = true;      // enabled once the run started
    try {
      const r = await (await api('/admin/metadata/backfill-game-albums', {
        method: 'POST',
        body: JSON.stringify({ include_remote: chkRemote ? chkRemote.checked : true }),
      })).json();
      deferred = Number(r.deferred) || 0;
      if (!r.total) {
        setRunning(false);
        showMsg(MSG, `Nothing to read.${skipped()}`, deferred ? 'warn' : 'ok');
        return;
      }
      adopt();
      if (btnCancel) btnCancel.disabled = false;
      showMsg(MSG, `Reading ${_fmtCount(r.total)} file(s)\u2026`, 'info');
    } catch (err) {
      if (!watching) setRunning(false);          // (a run it already follows stays shown)
      // A 409 names its cause (a run going, or the last one still finishing).
      showMsg(MSG, err.status === 409
        ? (err.message || 'Another repair or backfill is running \u2014 try again when it has finished.')
        : `Could not start: ${err.message}`, 'err');
    }
  });
  if (btnCancel) btnCancel.addEventListener('click', async () => {
    btnCancel.disabled = true;
    try {
      await api('/admin/metadata/repair-cancel', { method: 'POST' });
      showMsg(MSG, 'Cancelling \u2014 finishing the current file\u2026', 'warn');
    } catch (err) {
      btnCancel.disabled = false;
      showMsg(MSG, `Could not cancel: ${err.message}`, 'err');
    }
  });
})();

// ── Services panel ──────────────────────────────────────────────────────────
// PhD-UX rationale: a service toggle is a high-stakes UX (turning subsonic
// off means every Subsonic client on the LAN stops working), so we surface
// the state explicitly with a per-row hint of what each service does + a
// restart-required banner the moment the user flips anything.
//
// Backend contract: GET /api/admin/services lists current state;
// PUT /api/admin/services/{name} {"enabled": bool} flips one.

// Service-row hint text.  Each hint LEADS WITH the consequence of turning
// the service OFF (Nielsen H10: help & documentation framed for the user's
// task) so the user can predict what breaks.  UX-2 P1: feature-marketing
// copy ("required for X") was less informative than consequence-framed copy.
const _SERVICE_HINTS = {
  subsonic:    'Lets Subsonic-compatible clients (Amperfy, DSub, Symfonium, play:Sub) connect over /rest/*. Turning off blocks every such client.',
  multiroom:   'Synchronises playback across multiple SoniqBoom instances on the LAN. Turning off leaves only single-machine playback.',
  cast:        'Beta — outgoing Cast / DLNA / AirPlay sender; picks a TV, speaker, or receiver from the player toolbar. Turning off hides the toolbar cast button.',
  // UX-2 P0: dlna_server was missing from the hint dict.  Also flag the
  // privacy implication of turning it ON so the user can make an informed
  // decision (Nielsen H5: error prevention).
  dlna_server: 'Beta — incoming DLNA Media Server; exposes your library to every DLNA/UPnP client on this network. Off by default; turning on lets phones, TVs, and speakers browse and play tracks anonymously.',
};

// Helper: HTML-escape any text we interpolate into innerHTML attributes
// or content.  Service names + labels come from the backend (so they're
// trustworthy in practice), but defence-in-depth — UX-2 P0 flagged the
// missing escape on s.label inside title= / aria-label= attributes.
function _escAttr(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

let _servicesDirty = false;

async function loadServices() {
  const root = document.getElementById('services-list');
  if (!root) return;  // not on this page
  root.innerHTML = '<div class="services-empty">Loading services…</div>';
  try {
    const r = await api('/admin/services');
    const data = await r.json();
    const services = (data && data.services) || [];
    _renderServices(root, services);
  } catch (err) {
    root.innerHTML = (
      '<div class="services-empty">' +
      'Could not load services. ' +
      'Check the server log for /api/admin/services.' +
      '</div>'
    );
    /* eslint-disable-next-line no-console */
    console.error('[services] load failed:', err);
  }
}

function _renderServices(root, services) {
  root.innerHTML = '';
  for (const s of services) {
    const row = document.createElement('div');
    row.className = 'service-row' + (s.enabled ? '' : ' is-disabled');
    // Snapshot the at-load state so we can distinguish "row has pending
    // change" (UX-2 P0: don't bake "restart required" into the status
    // pill — only show it when the row actually differs from load).
    row.dataset.enabledAtLoad = s.enabled ? '1' : '0';

    // Stable per-row id so the <input> has an explicit <label for=...>
    // association — UX-2 P0 a11y fix.  ``role="switch"`` + aria-checked
    // is the canonical ARIA pattern for an on/off control, and the
    // visible name is associated to the input via the label-for binding
    // so screen readers announce "<service name> switch, on/off" instead
    // of duplicating the name from a stale aria-label.
    const inputId = 'svc-toggle-' + s.name;
    const safeName = _escAttr(s.label || s.name);
    row.innerHTML = `
      <div class="service-info">
        <label class="service-name" for="${_escAttr(inputId)}"></label>
        <div class="service-hint"></div>
        <div class="service-status ${s.enabled ? 'is-on' : 'is-off'}"
             aria-live="polite">${s.enabled ? 'On' : 'Off'}</div>
      </div>
      <span class="service-toggle"
            title="${_escAttr(s.enabled ? 'Disable' : 'Enable')} ${safeName}">
        <input id="${_escAttr(inputId)}" type="checkbox"
               role="switch"
               ${s.enabled ? 'checked aria-checked="true"' : 'aria-checked="false"'}>
        <span class="slider" aria-hidden="true"></span>
      </span>
    `;
    // textContent for user-visible strings — defence-in-depth even though
    // the strings come from our own backend.  Same XSS-class concern QA-2
    // P0 flagged on the prior attribute-interpolation form.
    row.querySelector('.service-name').textContent = s.label || s.name;
    row.querySelector('.service-hint').textContent =
      _SERVICE_HINTS[s.name] || '';

    const cb = row.querySelector('input[type="checkbox"]');
    // Inline error slot used when the PUT fails — UX-2 P1 (errors should
    // attach to the row that bounced, not a generic toast).
    let errSlot = null;

    cb.addEventListener('change', async () => {
      cb.disabled = true;
      // Clear any previous inline error before retrying.
      if (errSlot) { errSlot.remove(); errSlot = null; }

      const wantOn = cb.checked;
      // UX-2 P1: confirm before enabling dlna_server because flipping it
      // ON exposes the library to anyone on the LAN.  Match Synology /
      // QNAP DLNA toggles.  Skip the confirm if the user already opted
      // in via the cli on a previous session.
      if (s.name === 'dlna_server' && wantOn && !s.enabled) {
        const ok = window.confirm(
          'Enabling the DLNA Media Server exposes your entire library to ' +
          'anyone on this Wi-Fi / LAN — there is no per-device authentication ' +
          'in the DLNA protocol.\n\nContinue?'
        );
        if (!ok) {
          cb.checked = false;
          cb.disabled = false;
          return;
        }
      }
      try {
        await api('/admin/services/' + encodeURIComponent(s.name), {
          method: 'PUT',
          body: JSON.stringify({ enabled: wantOn }),
        });
        // Reflect new state without re-fetching — UX feels instant.
        s.enabled = wantOn;
        cb.setAttribute('aria-checked', wantOn ? 'true' : 'false');
        row.classList.toggle('is-disabled', !wantOn);
        const status = row.querySelector('.service-status');
        status.classList.toggle('is-on',  wantOn);
        status.classList.toggle('is-off', !wantOn);
        // UX-2 P0: status pill shows current state ONLY.  Per-row
        // "restart pending" is added next to it only when this row's
        // state differs from the at-load snapshot.
        const loadedOn = row.dataset.enabledAtLoad === '1';
        const dirty    = (wantOn ? '1' : '0') !== row.dataset.enabledAtLoad;
        status.textContent = wantOn ? 'On' : 'Off';
        // Per-row "restart pending" badge (sits next to status pill,
        // only present for actually-modified rows).
        let pending = row.querySelector('.service-pending');
        if (dirty && !pending) {
          pending = document.createElement('span');
          pending.className = 'service-pending';
          pending.textContent = 'restart pending';
          status.insertAdjacentElement('afterend', pending);
        } else if (!dirty && pending) {
          pending.remove();
        }
        _servicesDirty = _servicesDirty || dirty;
        if (_servicesDirty) _showRestartBanner();
        // Update title for the new state.
        const tog = row.querySelector('.service-toggle');
        if (tog) tog.title = (wantOn ? 'Disable ' : 'Enable ') + (s.label || s.name);
      } catch (err) {
        // Revert checkbox + ARIA to actual state — server didn't accept.
        cb.checked = s.enabled;
        cb.setAttribute('aria-checked', s.enabled ? 'true' : 'false');
        // Inline error so the user maps the message back to THIS row.
        errSlot = document.createElement('div');
        errSlot.className = 'service-error';
        errSlot.setAttribute('role', 'alert');
        errSlot.textContent =
          `Couldn't update — ${err.message || err}`;
        row.querySelector('.service-info').appendChild(errSlot);
        // Toast as secondary (matches the global notification convention
        // for actions that started elsewhere).
        if (window.Toast) window.Toast.error(
          `Couldn't update ${s.label || s.name}: ${err.message || err}`
        );
      } finally {
        cb.disabled = false;
      }
    });

    root.appendChild(row);
  }
}

function _showRestartBanner() {
  // UX-2 P1: the banner is now permanent informational ("Service changes
  // apply on next restart") AT THE TOP of the services panel, planted by
  // the static section markup.  This function only marks it ACTIVE +
  // attaches the action button when there are unsaved-to-runtime changes.
  // Nielsen H1 (visibility of system status) + H5 (error prevention) —
  // user sees the restart requirement BEFORE they toggle anything.
  const banner = document.querySelector('#services-section .services-restart-banner');
  if (!banner) return;
  banner.classList.add('is-active');
  // Add a "Restart Now" affordance — clicking it POSTs /admin/restart
  // (graceful restart endpoint that already exists for ScanZips toggle).
  if (banner.querySelector('.services-restart-btn')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'services-restart-btn';
  btn.textContent = 'Restart now';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Restarting…';
    try {
      // /admin/restart is allowNonOK because the connection drops mid-call
      // by design (the server exits before sending a response).
      await api('/admin/restart', { method: 'POST', allowNonOK: true });
    } catch {
      // Expected — the connection drops as the server restarts.
    }
    // Show a "reconnecting" state; the page will hard-reload when the
    // server comes back.
    btn.textContent = 'Reconnecting…';
    const ping = setInterval(async () => {
      try {
        const r = await fetch('/api/admin/stats', { credentials: 'same-origin' });
        if (r.ok || r.status === 401) {
          clearInterval(ping);
          window.location.reload();
        }
      } catch { /* still down — keep pinging */ }
    }, 1000);
  });
  banner.appendChild(btn);
}


// ── Settings ────────────────────────────────────────────────────────────────

// The player's own defaults — what applies when nothing was ever saved.  The forms
// must show THESE (they used to show 5 s / 300 ms, so one press of Save added a
// 5-second pre-roll to every track start).
const _PREF_DEFAULTS = { sb_crossfade: 0, sb_preload_buffer: 0, sb_convert_delay: 6000 };
function _prefValue(key) {
  const v = parseFloat(localStorage.getItem(key));
  return Number.isFinite(v) ? v : _PREF_DEFAULTS[key];
}
// Read a number field: blank / unreadable keeps the stored value (never silently
// 0), out of range is clamped — and either way ``notes`` says so.
function _readNumPref(el, key, lo, hi, name, notes) {
  const v = parseFloat(el?.value);
  if (!Number.isFinite(v)) { notes.push(`${name} was left unchanged.`); return _prefValue(key); }
  const c = Math.max(lo, Math.min(hi, v));
  if (c !== v) notes.push(`${name} was set to ${c} (${lo}\u2013${hi} allowed).`);
  return c;
}

// ── Preferences for non-admin users (this browser only) ──────────────────────
// Same localStorage keys the admin Settings grid edits; nothing here touches the
// server.  Built on the shared pl-modal styles.
function _openDevicePrefs() {
  const open_ = document.getElementById('device-prefs');
  if (open_) { open_._close?.(); return; }         // the gear toggles, like the admin panel
  const backdrop = document.createElement('div');
  backdrop.className = 'pl-modal-backdrop';
  backdrop.id = 'device-prefs';
  const dialog = document.createElement('div');
  dialog.className = 'pl-modal-dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'device-prefs-title');
  const row = 'display:flex;align-items:center;justify-content:space-between;gap:16px;margin:10px 0 0;font-size:13px';
  const hint = 'color:var(--text2,#bbb);font-size:12px;margin:2px 0 8px';
  dialog.innerHTML = `
    <div class="pl-modal-title" id="device-prefs-title">Preferences</div>
    <div style="color:var(--text2,#bbb);font-size:12px;margin:2px 0 12px">These preferences are saved in this browser only.</div>
    <label style="${row}" for="dp-crossfade">Crossfade (seconds)
      <input id="dp-crossfade" aria-describedby="dp-crossfade-hint" class="pl-modal-input" type="number" min="0" max="12" step="0.5" style="width:80px;margin:0"></label>
    <div id="dp-crossfade-hint" style="${hint}">Overlap between one track and the next. 0 = off.</div>
    <label style="${row}" for="dp-preload">Preload buffer (seconds)
      <input id="dp-preload" aria-describedby="dp-preload-hint" class="pl-modal-input" type="number" min="0" max="30" step="1" style="width:80px;margin:0"></label>
    <div id="dp-preload-hint" style="${hint}">Wait until this much audio is buffered before starting. 0 = off.</div>
    <label style="${row}" for="dp-delay">\u201cConverting\u2026\u201d notice delay (ms)
      <input id="dp-delay" aria-describedby="dp-delay-hint" class="pl-modal-input" type="number" min="0" max="30000" step="100" style="width:80px;margin:0"></label>
    <div id="dp-delay-hint" style="${hint}">How long a slow conversion (formats your browser can\u2019t play directly, such as DSD or ALAC) may take before the \u201cConverting\u2026\u201d notice appears. Retro and Amiga tracks show \u201cRendering\u2026\u201d after 1 second instead.</div>
    <label style="${row}" for="dp-shuffle-replay">Let shuffle repeat tracks you’ve heard
      <input id="dp-shuffle-replay" aria-describedby="dp-shuffle-replay-hint" type="checkbox"></label>
    <div id="dp-shuffle-replay-hint" style="color:var(--text2,#bbb);font-size:12px;margin:-4px 0 8px">Off: a shuffle plays each track once before anything repeats. Takes effect the next time you turn shuffle on.</div>
    <div id="dp-queue-sync-row"${Player.queueSyncSupported === false ? ' hidden' : ''}>
    <label style="${row}" for="dp-queue-sync">Sync the play queue across devices
      <input id="dp-queue-sync" aria-describedby="dp-queue-sync-hint" type="checkbox"></label>
    <div id="dp-queue-sync-hint" style="color:var(--text2,#bbb);font-size:12px;margin:-4px 0 8px">Keeps this browser’s queue on the server for your other devices and Subsonic apps, and offers to resume a newer queue saved elsewhere.</div>
    </div>
    <label style="${row}" for="dp-viz-enabled">Visualizations
      <input id="dp-viz-enabled" aria-describedby="dp-viz-enabled-hint" type="checkbox"></label>
    <div id="dp-viz-enabled-hint" style="color:var(--text2,#bbb);font-size:12px;margin:-4px 0 8px">Master switch for every animated visualization.</div>
    <label style="${row}" for="dp-viz-nowplaying">Now-playing visualizations
      <input id="dp-viz-nowplaying" aria-describedby="dp-viz-nowplaying-hint" type="checkbox"></label>
    <div id="dp-viz-nowplaying-hint" style="color:var(--text2,#bbb);font-size:12px;margin:-4px 0 8px">Decode signal-chain, CRT oscilloscope mode and the VU meter skin on the player screen.</div>
    <label style="${row}" for="dp-viz-vustyle">VU meter style
      <select id="dp-viz-vustyle" class="pl-modal-input" style="width:120px;margin:0"><option value="bars">Bars</option><option value="circuit">Circuit board</option></select></label>
    <label style="${row}" for="dp-sid-wasm">Play SID in browser (experimental)
      <input id="dp-sid-wasm" aria-describedby="dp-sid-wasm-hint" type="checkbox"></label>
    <div id="dp-sid-wasm-hint" style="color:var(--text2,#bbb);font-size:12px;margin:-4px 0 8px">Also renders C64 SID tunes in this browser to drive the per-voice VU meter. Playback still streams from the server.</div>
    <label style="${row}" for="dp-theme">Theme
      <select id="dp-theme" class="pl-modal-input" style="width:120px;margin:0"><option value="dark">Dark</option><option value="light">Light</option></select></label>
    <div style="border-top:1px solid var(--glass-border,rgba(255,255,255,.12));margin:14px 0 0;padding-top:10px">
      <div style="color:var(--text2,#bbb);font-size:12px;margin:0 0 8px">Your account (saved on the server):</div>
      <div id="dp-sspw" style="font-size:13px"></div>
      <div id="dp-apikeys" style="font-size:13px;margin-top:14px"></div>
    </div>
    <div class="pl-modal-actions">
      <button class="pl-modal-btn pl-modal-cancel" type="button">Cancel</button>
      <button class="pl-modal-btn pl-modal-ok" type="button">Save</button>
    </div>`;
  backdrop.appendChild(dialog);
  document.body.appendChild(backdrop);
  const $ = (id) => dialog.querySelector('#' + id);
  $('dp-crossfade').value = _prefValue('sb_crossfade');
  $('dp-preload').value = _prefValue('sb_preload_buffer');
  $('dp-delay').value = _prefValue('sb_convert_delay');
  $('dp-shuffle-replay').checked = localStorage.getItem('sb_shuffle_replay') === '1';
  $('dp-queue-sync').checked = Player.queueSync;
  $('dp-theme').value = localStorage.getItem('sb_theme') || 'dark';
  const viz = getVizSettings();
  $('dp-viz-enabled').checked = viz.enabled !== false;
  $('dp-viz-nowplaying').checked = viz.nowPlaying !== false;
  $('dp-viz-vustyle').value = viz.vuStyle === 'circuit' ? 'circuit' : 'bars';
  $('dp-sid-wasm').checked = sidWasmPlaybackEnabled();
  // Safari does not focus a <button> on click, so activeElement would be <body>.
  const opener = document.getElementById('btn-admin') || document.activeElement;
  const close = () => { backdrop.remove(); _clearRevealedSecrets(); try { opener?.focus?.(); } catch (_) {} };
  backdrop._close = close;
  _mountSubsonicPassword($('dp-sspw'), 'dp');
  _mountApiKeys($('dp-apikeys'), 'dp');
  dialog.querySelector('.pl-modal-cancel').addEventListener('click', close);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
  // A modal: Escape closes, Enter saves, and Tab stays inside it (the page behind
  // is neither inert nor hidden from assistive tech, so focus must not wander out).
  dialog.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
    if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); dialog.querySelector('.pl-modal-ok').click(); return; }
    if (e.key !== 'Tab') return;
    const f = [...dialog.querySelectorAll('input, select, button')];
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  dialog.querySelector('.pl-modal-ok').addEventListener('click', async () => {
    // A blank / unreadable field keeps the stored value; out of range is clamped.
    const notes = [];
    localStorage.setItem('sb_crossfade', String(_readNumPref($('dp-crossfade'), 'sb_crossfade', 0, 12, 'Crossfade', notes)));
    localStorage.setItem('sb_preload_buffer', String(_readNumPref($('dp-preload'), 'sb_preload_buffer', 0, 30, 'Preload buffer', notes)));
    localStorage.setItem('sb_convert_delay', String(Math.round(_readNumPref($('dp-delay'), 'sb_convert_delay', 0, 30000, '\u201cConverting\u2026\u201d notice delay', notes))));
    localStorage.setItem('sb_shuffle_replay', $('dp-shuffle-replay').checked ? '1' : '0');
    Player.setQueueSync($('dp-queue-sync').checked);
    const theme = $('dp-theme').value === 'light' ? 'light' : 'dark';
    localStorage.setItem('sb_theme', theme);
    if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
    else document.documentElement.removeAttribute('data-theme');
    // Visualization preferences (localStorage, applied live like the admin grid's).
    const vuStyle = $('dp-viz-vustyle').value === 'circuit' ? 'circuit' : 'bars';
    setVizSettings({ enabled: $('dp-viz-enabled').checked, nowPlaying: $('dp-viz-nowplaying').checked, vuStyle });
    _applyVuStyleLive(vuStyle);
    setSidWasmPlayback($('dp-sid-wasm').checked);
    close();
    try { (await import('./utils.js')).Toast.ok(['Preferences saved for this browser.', ...notes].join(' ')); } catch (_) {}
  });
  $('dp-crossfade').focus();
}

let _loadedDedupFolders = null;   // value the form was loaded with → Save knows if it changed
// Server toggles that save on change (see _bindServerToggle): filled — and only
// then enabled — from the server's settings.
const _SAVE_ON_CHANGE_TOGGLES = [
  ['setting-render-prewarm',         'render_prewarm'],
  ['setting-uade-vu-meters',         'uade_vu_meters'],
  ['setting-subsonic-folder-albums', 'subsonic_folder_albums'],
  ['setting-modland-filename-game',  'modland_filename_game'],
  ['setting-game-from-archive',      'game_from_archive_name'],
  ['setting-retro-album-folder',     'retro_album_from_folder'],
];

// The player learns whether the server keeps a play queue a few seconds after
// load; Settings or Preferences may already be open by then.
Player.on('queuesync', ({ supported }) => {
  for (const id of ['setting-queue-sync-row', 'dp-queue-sync-row']) {
    const el = document.getElementById(id);
    if (el) el.hidden = supported === false;
  }
});

async function loadSettings() {
  const xfadeEl   = document.getElementById('setting-crossfade');
  const preloadEl = document.getElementById('setting-preload-buffer');
  const delayEl   = document.getElementById('setting-convert-delay');
  const themeEl   = document.getElementById('setting-theme');
  if (xfadeEl)   xfadeEl.value   = _prefValue('sb_crossfade');
  if (preloadEl) preloadEl.value = _prefValue('sb_preload_buffer');
  if (delayEl)   delayEl.value   = _prefValue('sb_convert_delay');
  const shufReplayEl = document.getElementById('setting-shuffle-replay');
  if (shufReplayEl) shufReplayEl.checked = localStorage.getItem('sb_shuffle_replay') === '1';
  const qsyncEl = document.getElementById('setting-queue-sync');
  if (qsyncEl) qsyncEl.checked = Player.queueSync;
  const qsyncRow = document.getElementById('setting-queue-sync-row');
  if (qsyncRow) qsyncRow.hidden = Player.queueSyncSupported === false;   // the server has no queue to sync with
  if (themeEl)   themeEl.value   = localStorage.getItem('sb_theme') || 'dark';
  // Visualization preference controls (client-side, applies live).
  _initVizSettingsUI();
  // Populate the About panel each time settings load (cheap, idempotent)
  loadAbout();
  try {
    const res = await api('/admin/settings');
    const s = await res.json();
    const zipEl = document.getElementById('setting-scan-zips');
    if (zipEl) zipEl.checked = s.scan_zips !== false;
    const remoteZipEl = document.getElementById('setting-scan-remote-zips');
    if (remoteZipEl) remoteZipEl.checked = s.scan_remote_zips !== false;
    const reconcileEl = document.getElementById('setting-startup-reconcile-scan');
    if (reconcileEl) reconcileEl.checked = s.startup_reconcile_scan !== false;
    // Keep the add-dir form checkbox in sync with the global setting
    const addZipEl = document.getElementById('admin-scan-zips');
    if (addZipEl) addZipEl.checked = s.scan_zips !== false;
    const sidEl = document.getElementById('setting-sid-duration');
    if (sidEl) sidEl.value = s.renderers?.sid_default_duration || 180;
    const dupEl = document.getElementById('setting-filter-duplicates');
    if (dupEl) dupEl.checked = !!s.filter_duplicates;
    const dedupFoldersEl = document.getElementById('setting-dedup-folders');
    if (dedupFoldersEl) dedupFoldersEl.checked = !!s.dedup_folders;
    _loadedDedupFolders = !!s.dedup_folders;
    for (const [id, key] of _SAVE_ON_CHANGE_TOGGLES) {
      const el = document.getElementById(id);
      if (!el || typeof s[key] !== 'boolean' || el.getAttribute('aria-busy')) continue;
      el.checked = s[key];
      el.disabled = false;
    }
    const folderArtEl = document.getElementById('setting-use-folder-art');
    if (folderArtEl) folderArtEl.checked = s.use_folder_art !== false;
    const lyricsWbEl = document.getElementById('setting-lyrics-writeback');
    if (lyricsWbEl) lyricsWbEl.checked = !!s.lyrics_writeback;
    const lyricsCountEl = document.getElementById('lyrics-cache-count');
    if (lyricsCountEl) lyricsCountEl.textContent = s.lyrics_cache_size ? `(${s.lyrics_cache_size})` : '';
    const folderArtNamesEl = document.getElementById('setting-folder-art-names');
    // We render the user's last value verbatim (empty = "fall back to the
    // built-in default") rather than back-filling the default into the
    // textbox.  Showing the default would make a fresh install look like
    // it was already overridden, and the placeholder communicates the
    // default's contents without committing them to the config.
    if (folderArtNamesEl) folderArtNamesEl.value = s.folder_art_names || '';
    // Logging verbosity dials (server-side, applied live)
    const logLevelEl = document.getElementById('setting-log-level');
    if (logLevelEl) logLevelEl.value = s.log_level || 'normal';
    const accessLogEl = document.getElementById('setting-access-log');
    if (accessLogEl) accessLogEl.value = s.access_log || 'problems';
    const rcMbEl = document.getElementById('setting-remote-cache-mb');
    if (rcMbEl) rcMbEl.value = s.remote_cache_max_mb || 2048;
    const ccMbEl = document.getElementById('setting-conv-cache-mb');
    if (ccMbEl) ccMbEl.value = s.conversion_cache_max_mb || 4096;
    // SID fidelity controls (Renderers tab)
    const r = s.renderers || {};
    const sidModelEl = document.getElementById('setting-sid-model');
    if (sidModelEl) sidModelEl.value = r.sid_model || 'auto';
    const sidForceEl = document.getElementById('setting-sid-model-force');
    if (sidForceEl) sidForceEl.checked = !!r.sid_model_force;
    const sidFilterEl = document.getElementById('setting-sid-filter');
    if (sidFilterEl) sidFilterEl.checked = r.sid_filter !== false;
    const sidCurveEl = document.getElementById('setting-sid-fcurve');
    // -1 is the backend's "engine default" sentinel — shown as empty.
    if (sidCurveEl) {
      const c = r.sid_filter_curve;
      sidCurveEl.value = (typeof c === 'number' && c >= 0) ? String(c) : '';
    }
    const sidBoostEl = document.getElementById('setting-sid-digiboost');
    if (sidBoostEl) sidBoostEl.checked = !!r.sid_digiboost;
    // Renderer binary paths (Renderers tab)
    for (const name of ['uade123', 'gme', 'adplay', 'psgplay', 'sc68',
                        'zxtune123']) {
      const el = document.getElementById(`setting-path-${name}`);
      if (el) el.value = r[`${name}_path`] || '';
    }
  } catch { /* non-fatal */ }
  // Refresh the HVSC hint on the SID-duration field so opening the
  // System tab reflects whether HVSC is in charge of per-tune lengths.
  loadHvscStatus();
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
  const _notes = [];
  const xfade   = _readNumPref(document.getElementById('setting-crossfade'),      'sb_crossfade',      0, 12,    'Crossfade', _notes);
  const preload = _readNumPref(document.getElementById('setting-preload-buffer'), 'sb_preload_buffer', 0, 30,    'Preload buffer', _notes);
  const delay   = Math.round(_readNumPref(document.getElementById('setting-convert-delay'), 'sb_convert_delay', 0, 30000, '\u201cConverting\u2026\u201d notice delay', _notes));
  const theme    = document.getElementById('setting-theme')?.value || 'dark';
  localStorage.setItem('sb_crossfade',      String(xfade));
  localStorage.setItem('sb_preload_buffer', String(Math.max(0, preload))); // takes effect on next track
  localStorage.setItem('sb_convert_delay',  String(delay));
  // Read by the player at the next shuffle deal (player.js ``sb_shuffle_replay``).
  localStorage.setItem('sb_shuffle_replay',
    document.getElementById('setting-shuffle-replay')?.checked ? '1' : '0');
  const _qsyncEl = document.getElementById('setting-queue-sync');
  if (_qsyncEl) Player.setQueueSync(_qsyncEl.checked);
  localStorage.setItem('sb_theme', theme);
  // Apply the theme immediately so the user sees the flip without reload.
  if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else                   document.documentElement.removeAttribute('data-theme');
  try {
    const scanZips = document.getElementById('setting-scan-zips')?.checked ?? true;
    const scanRemoteZips = document.getElementById('setting-scan-remote-zips')?.checked ?? true;
    const startupReconcile = document.getElementById('setting-startup-reconcile-scan')?.checked ?? true;
    const sidDur = parseInt(document.getElementById('setting-sid-duration')?.value || '180');
    const filterDups = document.getElementById('setting-filter-duplicates')?.checked ?? false;
    const dedupFolders = document.getElementById('setting-dedup-folders')?.checked ?? false;
    const useFolderArt = document.getElementById('setting-use-folder-art')?.checked ?? true;
    // ``folder_art_names`` is a CSV the server trims, lower-cases, and
    // dedupes server-side (api/art.py:_parse_folder_art_names).  We send
    // the raw textbox value so the user's capitalisation round-trips for
    // display, but the lookup itself is case-insensitive.  Empty string =
    // "use the built-in default".
    const folderArtNames = (document.getElementById('setting-folder-art-names')?.value ?? '').trim();
    const remoteCacheMb = parseInt(document.getElementById('setting-remote-cache-mb')?.value || '2048');
    const logLevel  = document.getElementById('setting-log-level')?.value  || 'normal';
    const accessLog = document.getElementById('setting-access-log')?.value || 'problems';
    await api('/admin/settings', {
      method: 'PUT',
      body: JSON.stringify({
        scan_zips: scanZips,
        scan_remote_zips: scanRemoteZips,
        startup_reconcile_scan: startupReconcile,
        renderers: { sid_default_duration: sidDur },
        filter_duplicates: filterDups,
        dedup_folders: dedupFolders,
        use_folder_art: useFolderArt,
        folder_art_names: folderArtNames,
        remote_cache_max_mb: remoteCacheMb,
        log_level: logLevel,
        access_log: accessLog,
      }),
    });
    // Rebuild the sidebar tree so the hide-empty toggle takes effect
    // immediately.  ``FolderTree.refresh()`` rewinds every expanded node
    // — without this the user has to collapse + re-expand to see the
    // filter applied to nodes whose children are already cached
    // client-side.  Imported dynamically because admin.js may load
    // before the foldertree module attaches its scan-root list.
    try {
      const mod = await import('./foldertree.js');
      mod.FolderTree?.refresh?.();
    } catch { /* non-fatal — next page reload picks it up anyway */ }
    // A folder that is open behind this panel lists by the folder-duplicates
    // setting — redraw it in place so the change is visible without re-navigating.
    if (dedupFolders !== _loadedDedupFolders) {
      _loadedDedupFolders = dedupFolders;
      try { (await import('./library.js')).Library?.refreshCurrentFolderInPlace?.(); } catch { /* non-fatal */ }
    }
    showMsg('admin-settings-msg', ['Settings saved.', ..._notes].join(' '), 'ok');
  } catch {
    // The browser-local half (crossfade, preload, convert delay, shuffle, theme)
    // was written before the request — say so, or the user re-does it for nothing.
    showMsg('admin-settings-msg', 'Saved for this browser. The server settings could not be saved \u2014 check the connection and try again.', 'err');
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
  const hostInput  = document.getElementById('net-host');
  const pathInput  = document.getElementById('net-path');
  const anonText = document.getElementById('net-anon-text');
  const anonBox = document.getElementById('net-anonymous');
  const webdavRow = document.getElementById('net-webdav-row');
  const isWebDav = proto === 'webdav' || proto === 'webdavs';
  // WebDAV uses a single Base-URL field instead of host/share/path.
  if (webdavRow) webdavRow.style.display = isWebDav ? '' : 'none';
  hostInput.style.display  = isWebDav ? 'none' : '';
  shareInput.style.display = isWebDav ? 'none' : (proto === 'ftp' ? 'none' : '');
  pathInput.style.display  = isWebDav ? 'none' : '';
  if (proto === 'ftp') {
    pathInput.placeholder = 'Remote path (e.g. /music)';
    anonText.textContent = 'Anonymous';
    if (!anonBox._userToggled) anonBox.checked = true;
  } else if (isWebDav) {
    anonText.textContent = 'Public (no auth)';
    if (!anonBox._userToggled) anonBox.checked = false;
  } else {
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
  } catch (e) {
    msg.textContent = _apiErrText(e);
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
      const davUrl = document.getElementById('net-webdav-url');
      if (davUrl) davUrl.value = '';
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
  } catch (e) {
    msg.textContent = _apiErrText(e);
    msg.className = 'net-status-msg net-err';
  }
  btn.disabled = false;
});

function _getNetFormData() {
  const proto = document.getElementById('net-protocol').value;
  const base = {
    protocol: proto,
    username: document.getElementById('net-user').value.trim(),
    password: document.getElementById('net-pass').value,
    alias:    document.getElementById('net-alias').value.trim(),
  };
  if (proto === 'webdav' || proto === 'webdavs') {
    // For WebDAV, the user gives us one URL — but the backend (/admin/shares)
    // expects host/share/path-style fields.  Derive them from the URL so the
    // existing share record format stays uniform.
    const raw = (document.getElementById('net-webdav-url').value || '').trim();
    try {
      const u = new URL(raw);
      base.host        = u.host;
      base.share       = '';   // WebDAV doesn't have shares per se
      base.remote_path = u.pathname || '/';
      base.base_url    = raw;
    } catch {
      base.host = '';
      base.share = '';
      base.remote_path = '/';
      base.base_url = raw;     // backend will reject the bad URL
    }
    return base;
  }
  return {
    ...base,
    host:        document.getElementById('net-host').value.trim(),
    share:       document.getElementById('net-share').value.trim(),
    remote_path: document.getElementById('net-path').value.trim() || '/',
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
  } catch (e) {
    showMsg('admin-dir-msg', _apiErrText(e), 'err');
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
  // Immediate "Loading…" feedback so the user knows the click registered.
  // Without this, a slow / failing fetch left the static placeholder text
  // showing, which looked identical to "the button does nothing".
  const wasAtBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
  viewer.textContent = 'Loading…';
  try {
    const res = await api(`/admin/logs?lines=${lines}`);
    if (!res.ok) {
      viewer.textContent =
        `Couldn't load logs — HTTP ${res.status} ${res.statusText || ''}\n\n` +
        `Tip: this can happen if your browser cached an older admin.js — ` +
        `hard-reload (Cmd+Shift+R) and try again.`;
      return;
    }
    const d = await res.json();
    viewer.textContent = (d.lines || []).join('\n') || 'No log lines yet.';
    if (wasAtBottom) viewer.scrollTop = viewer.scrollHeight;
  } catch (e) {
    viewer.textContent =
      `Couldn't load logs — ${e && e.message ? e.message : 'network error'}\n\n` +
      `Tip: hard-reload (Cmd+Shift+R) if you recently updated SoniqBoom.`;
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
  showMsg('admin-restart-msg', 'Sending restart request…', 'info');
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

// ``window.__sbConfirm`` is installed at the top of this file alongside
// the confirm-dialog DOM refs — moved there so it's available
// synchronously to other modules that import admin.js, instead of only
// after the deeper init code below has finished.

// ── Styled password dialog (replaces prompt() in admin.js) ──────────────────

const _pwDialog       = document.getElementById('password-dialog');
const _pwForm         = document.getElementById('password-dialog-form');
const _pwTitle        = document.getElementById('password-dialog-title');
const _pwMessage      = document.getElementById('password-dialog-message');
const _pwCurrentWrap  = document.getElementById('password-dialog-current-wrap');
const _pwCurrent      = document.getElementById('password-dialog-current');
const _pwNew          = document.getElementById('password-dialog-new');
const _pwConfirm      = document.getElementById('password-dialog-confirm');
const _pwError        = document.getElementById('password-dialog-error');
const _pwCancel       = document.getElementById('password-dialog-cancel');

let _pwResolve = null;

/**
 * Show a password-collection dialog.  Resolves to:
 *   { password: '...' }                   (set-password mode)
 *   { current: '...', password: '...' }   (change-password mode)
 *   null                                  (user cancelled)
 */
let _pwFocusBefore = null;

function styledPasswordDialog({
  title = 'Set password',
  message = '',
  requireCurrent = false,
} = {}) {
  return new Promise((resolve) => {
    _pwResolve = resolve;
    _pwFocusBefore = document.activeElement;
    _pwTitle.textContent   = title;
    _pwMessage.textContent = message;
    _pwCurrentWrap.hidden  = !requireCurrent;
    _pwCurrent.required    = !!requireCurrent;
    _pwCurrent.value = '';
    _pwNew.value     = '';
    _pwConfirm.value = '';
    _pwError.hidden  = true;
    _pwError.textContent = '';
    _pwDialog.classList.remove('hidden');
    setTimeout(() => (requireCurrent ? _pwCurrent : _pwNew).focus(), 30);
  });
}

// Focus trap for the password dialog — Tab/Shift+Tab cycle inside.
_pwDialog.addEventListener('keydown', (e) => {
  if (e.key !== 'Tab') return;
  const focusables = _pwDialog.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), '
    + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  );
  // Filter to visible (current-pw row may be hidden)
  const visible = Array.from(focusables).filter(
    el => !el.closest('[hidden]') && el.offsetParent !== null,
  );
  if (!visible.length) return;
  const first = visible[0];
  const last  = visible[visible.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});

function _closePasswordDialog(result) {
  _pwDialog.classList.add('hidden');
  const r = _pwResolve;
  _pwResolve = null;
  // Wipe values so they don't sit in memory between opens.
  _pwCurrent.value = '';
  _pwNew.value     = '';
  _pwConfirm.value = '';
  // Restore focus to whatever the user was on before we opened (WCAG 2.4.3).
  if (_pwFocusBefore && typeof _pwFocusBefore.focus === 'function'
      && document.contains(_pwFocusBefore)) {
    try { _pwFocusBefore.focus(); } catch { /* ignore */ }
  }
  _pwFocusBefore = null;
  r?.(result);
}

_pwForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const requireCurrent = !_pwCurrentWrap.hidden;
  const cur = _pwCurrent.value;
  const nw  = _pwNew.value;
  const cf  = _pwConfirm.value;
  if (requireCurrent && !cur) {
    _pwError.textContent = 'Enter your current password.';
    _pwError.hidden = false;
    return;
  }
  if (nw.length < 8) {
    _pwError.textContent = 'Password must be at least 8 characters.';
    _pwError.hidden = false;
    return;
  }
  if (nw !== cf) {
    _pwError.textContent = 'The two new passwords don’t match.';
    _pwError.hidden = false;
    return;
  }
  _closePasswordDialog(requireCurrent ? { current: cur, password: nw }
                                       : { password: nw });
});
_pwCancel.addEventListener('click', () => _closePasswordDialog(null));
_pwDialog.addEventListener('click', (e) => {
  if (e.target === _pwDialog) _closePasswordDialog(null);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !_pwDialog.classList.contains('hidden')) {
    _closePasswordDialog(null);
  }
});
window.__sbPasswordDialog = styledPasswordDialog;

// ── Subsonic connection card (System tab) ──────────────────────────────

(function _wireSubsonicCard() {
  const urlEl = document.getElementById('subsonic-server-url');
  const usrEl = document.getElementById('subsonic-username');
  const cpy   = document.getElementById('btn-copy-subsonic-url');
  if (!urlEl) return;
  // Server URL is whatever the user typed into their browser — that's
  // the URL clients should target too (modulo VPN / reverse-proxy).
  const baseUrl = `${location.protocol}//${location.host}`;
  urlEl.textContent = baseUrl;
  if (Auth.user) usrEl.textContent = Auth.user.username;
  cpy?.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(baseUrl);
      const orig = cpy.textContent;
      cpy.textContent = 'Copied!';
      setTimeout(() => { cpy.textContent = orig; }, 1300);
    } catch {
      // Fallback for browsers without clipboard permission — select the URL.
      const range = document.createRange();
      range.selectNodeContents(urlEl);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });
})();

// ── HVSC integration (Renderers tab) ─────────────────────────────────────

async function loadHvscStatus() {
  const pathEl   = document.getElementById('setting-hvsc-path');
  const statusEl = document.getElementById('hvsc-status');
  const sidHint  = document.getElementById('setting-sid-duration-hint');
  if (!pathEl || !statusEl) return;
  try {
    const res = await api('/admin/hvsc/status');
    if (!res.ok) return;
    const s = await res.json();
    pathEl.value = s.docs_path || '';
    if (s.enabled && (s.songlengths || s.stil)) {
      statusEl.classList.remove('off');
      statusEl.classList.add('on');
      statusEl.textContent = `loaded · ${s.songlengths.toLocaleString()} song lengths · ${s.stil.toLocaleString()} STIL entries`;
      // Surface the "fallback only" semantics on the SID-duration field
      // in the System tab so users understand HVSC is in charge.
      if (sidHint) {
        sidHint.textContent =
          `Fallback only — ${s.songlengths.toLocaleString()} tunes use their exact HVSC length instead.`;
        sidHint.style.color = 'var(--accent)';
      }
    } else if (s.enabled) {
      statusEl.classList.remove('off');
      statusEl.classList.add('on');
      statusEl.textContent = 'configured (waiting for first lookup)';
      if (sidHint) {
        sidHint.textContent = 'Fallback only — HVSC is configured and overrides this per tune.';
        sidHint.style.color = 'var(--accent)';
      }
    } else {
      statusEl.classList.remove('on');
      statusEl.classList.add('off');
      statusEl.textContent = 'disabled';
      if (sidHint) {
        sidHint.textContent = 'Used for every SID tune. Configure HVSC under Renderers to get exact per-tune durations.';
        sidHint.style.color = 'var(--text2)';
      }
    }
  } catch { /* ignore */ }
}

document.getElementById('btn-save-hvsc')?.addEventListener('click', async () => {
  const path = (document.getElementById('setting-hvsc-path')?.value || '').trim();
  try {
    const res = await api('/admin/settings', {
      method: 'PUT',
      body:   JSON.stringify({ renderers: { hvsc_docs_path: path } }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showMsg('admin-hvsc-msg', `Failed: ${data.detail || res.status}`, 'err');
      return;
    }
    showMsg('admin-hvsc-msg', 'Saved. Reloading database…', 'info');
    await loadHvscStatus();
  } catch (err) {
    showMsg('admin-hvsc-msg', `Network error: ${err.message}`, 'err');
  }
});

document.getElementById('btn-rescan-sids')?.addEventListener('click', async () => {
  try {
    const res = await api('/admin/hvsc/rescan-sids', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showMsg('admin-hvsc-msg', `Failed: ${data.detail || res.status}`, 'err');
      return;
    }
    showMsg('admin-hvsc-msg', data.message || 'Re-extract queued.', 'ok');
  } catch (err) {
    showMsg('admin-hvsc-msg', `Network error: ${err.message}`, 'err');
  }
});

// Clean up scan-dir rows that an older release auto-imported from the
// HVSC tree.  Removes only entries that live under the HVSC root and
// contain exclusively SID tracks — anything mixed gets left alone.
document.getElementById('btn-hvsc-cleanup')?.addEventListener('click', async () => {
  const ok = await styledConfirm(
    'Remove scan-dir entries under the HVSC tree that contain only SID tracks?  '
    + 'Your tracks stay in the library; only the redundant folder rows are removed.',
    { title: 'Clean up HVSC folders', okLabel: 'Remove', dangerColor: true },
  );
  if (!ok) return;
  try {
    const res = await api('/admin/hvsc/cleanup-orphans', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showMsg('admin-hvsc-msg', `Failed: ${data.detail || res.status}`, 'err');
      return;
    }
    showMsg('admin-hvsc-msg', data.message || 'Cleaned up.', 'ok');
    // Refresh the library-folders panel so the user sees the rows disappear.
    if (typeof loadDirs === 'function') loadDirs();
    document.dispatchEvent(new CustomEvent('soniqboom:dirs-changed'));
  } catch (err) {
    showMsg('admin-hvsc-msg', `Network error: ${err.message}`, 'err');
  }
});

// ── SID fidelity (Renderers tab) ─────────────────────────────────────────────

document.getElementById('btn-save-sidfid')?.addEventListener('click', async () => {
  const modelEl = document.getElementById('setting-sid-model');
  const forceEl = document.getElementById('setting-sid-model-force');
  const filterEl = document.getElementById('setting-sid-filter');
  const curveEl = document.getElementById('setting-sid-fcurve');
  const boostEl = document.getElementById('setting-sid-digiboost');
  // Empty curve field = "engine default" = the backend's -1 sentinel.
  // Anything non-empty must be a real 0..1 number — the backend silently
  // coerces out-of-range values to the default, which would show "Saved"
  // while quietly discarding the user's input.
  const rawCurve = (curveEl?.value || '').trim();
  const curve = rawCurve === '' ? -1 : parseFloat(rawCurve);
  if (rawCurve !== '' && (!Number.isFinite(curve) || curve < 0 || curve > 1)) {
    showMsg('admin-sidfid-msg',
            'Filter curve must be between 0.0 and 1.0 (or empty for the '
            + 'engine default) — nothing was saved.', 'err');
    return;
  }
  try {
    // api() throws on non-2xx with the backend detail in err.message.
    await api('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ renderers: {
        sid_model: modelEl?.value || 'auto',
        sid_model_force: !!forceEl?.checked,
        sid_filter: filterEl ? !!filterEl.checked : true,
        sid_filter_curve: curve,
        sid_digiboost: !!boostEl?.checked,
      } }),
    });
    showMsg('admin-sidfid-msg',
            'Saved. New renders use these options; cached tunes keep their '
            + 'old sound until the SID cache is cleared.', 'ok');
  } catch (err) {
    showMsg('admin-sidfid-msg', `Save failed: ${err.message}`, 'err');
  }
});

// ── Renderer binary paths (Renderers tab) ────────────────────────────────────

document.getElementById('btn-save-renderer-paths')?.addEventListener('click', async () => {
  const renderers = {};
  for (const name of ['uade123', 'gme', 'adplay', 'psgplay', 'sc68',
                      'zxtune123']) {
    const el = document.getElementById(`setting-path-${name}`);
    if (el) renderers[`${name}_path`] = el.value.trim();
  }
  try {
    // api() throws on non-2xx with the backend detail in err.message.
    await api('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ renderers }),
    });
    showMsg('admin-rpaths-msg', 'Saved — re-checking renderer status…', 'info');
    loadRendererStatus();   // reflect the new paths in the chips above
  } catch (err) {
    showMsg('admin-rpaths-msg', `Save failed: ${err.message}`, 'err');
  }
});

// ── Scene metadata (Modland) ─────────────────────────────────────────────────

function _renderSceneStatus(s) {
  const line = document.getElementById('scene-status-line');
  if (!line) return;
  if (!s || typeof s !== 'object') { line.textContent = 'Index: —'; return; }
  let txt;
  if (s.refreshing) {
    txt = 'Index: downloading…';
  } else if (!s.index_rows) {
    txt = 'Index: not downloaded yet';
  } else {
    const built = s.index_built_at
      ? new Date(s.index_built_at * 1000).toLocaleDateString() : 'unknown date';
    txt = `Index: ${s.index_rows.toLocaleString()} entries (built ${built})`;
  }
  if (s.applying) txt += ' · applying…';
  else if (s.last_apply) {
    txt += ` · last apply: ${s.last_apply.matched.toLocaleString()} matched, `
         + `${s.last_apply.updated.toLocaleString()} updated`;
  }
  if (s.error) txt += ` · ${s.error}`;
  line.textContent = txt;
}

async function loadSceneStatus() {
  try {
    const res = await api('/admin/scene/status');
    if (res.ok) _renderSceneStatus(await res.json());
  } catch (_) { /* non-critical */ }
}

document.getElementById('btn-scene-refresh')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  const line = document.getElementById('scene-status-line');
  if (line) line.textContent = 'Index: downloading (~21 MB — can take a minute)…';
  try {
    const res = await api('/admin/scene/refresh-index', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderSceneStatus(s);
    if (s && !s.error) showMsg('admin-scene-msg', 'Index refreshed.', 'ok');
    else showMsg('admin-scene-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
  } catch (err) {
    // api() throws on non-2xx too — err.message carries the backend detail.
    showMsg('admin-scene-msg', `Refresh failed: ${err.message}`, 'err');
    loadSceneStatus();
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('btn-scene-apply')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const res = await api('/admin/scene/apply', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderSceneStatus(s);
    if (s && !s.error) {
      const la = s.last_apply || {};
      const albums = typeof la.albums === 'number'
        ? ` (${la.albums.toLocaleString()} album change${la.albums === 1 ? '' : 's'})` : '';
      showMsg('admin-scene-msg',
              `Applied: ${(la.matched ?? 0).toLocaleString()} matched, `
              + `${(la.updated ?? 0).toLocaleString()} tracks updated${albums}.`, 'ok');
      // Artists, albums and scene paths changed: the open library view refreshes (app.js).
      if ((Number(la.updated) || 0) > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    } else {
      showMsg('admin-scene-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    }
  } catch (err) {
    // api() throws on non-2xx too — err.message carries the backend detail.
    showMsg('admin-scene-msg', `Apply failed: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
});

// ── Metadata tab: game names from archive names (game-title lists) ───────────

const _GT_NAMES = { tosec: 'TOSEC', redump: 'Redump' };
const _GT_FILES = { tosec: 'pack', redump: 'lists' };   // what its kept files are called
let _gtPoll = null;
let _gtLastBusy = false;
let _gtFailedPolls = 0;
const _gtWasBusy = {};

function _gtBusy(job) {
  return !!job && !['done', 'error', 'interrupted', undefined].includes(job.state);
}

// "N retro tracks take their game from their archive's name" (+ how many more
// carry it only as another name, below a higher-ranked source).
function _gtNamedText(primary, named) {
  const p = Number(primary) || 0, n = Number(named) || 0;
  let txt = `${_fmtCount(p)} retro track(s) take their game from their archive's name`;
  if (n > p) txt += ` (${_fmtCount(n - p)} more list it as another name)`;
  return txt;
}

function _renderGameTitlesStatus(s) {
  const line = document.getElementById('md-game-titles-status');
  if (!line || !s || typeof s !== 'object') return;
  const parts = [];
  if (Number(s.titles) > 0) parts.push(`${_fmtCount(s.titles)} distinct titles`);
  let busy = !!s.pass_busy;
  for (const [src, d] of Object.entries(s.downloads || {})) {
    const name = _GT_NAMES[src] || src;
    const kind = _GT_FILES[src] || 'files';
    const dl = document.getElementById(`md-gt-dl-${src}`);
    const rm = document.getElementById(`md-gt-rm-${src}`);
    const del = document.getElementById(`md-gt-del-${src}`);
    const stop = document.getElementById(`md-gt-stop-${src}`);
    const files = d.files && typeof d.files === 'object' ? d.files : null;
    const resumable = !!(files && files.resumable);
    const size = files ? _fmtBytes(Number(files.bytes)) : '';
    const running = _gtBusy(d);
    busy = busy || running;
    if (running) {
      let txt = `${name}: ${d.message || 'working'}`;
      if (d.state === 'downloading' && Number(d.total) > 1e6) {
        txt += ` (${(Number(d.bytes || 0) / 1e6).toFixed(0)} of ${(Number(d.total) / 1e6).toFixed(0)} MB)`;
      }
      parts.push(txt + '…');
    } else if (d.state === 'error' || d.state === 'interrupted') {
      parts.push(`${name}: ${d.message}${d.present ? ' (the earlier list is kept)' : ''}`);
    } else if (d.present) {
      let txt = `${name} downloaded ${d.downloaded || ''}`.trim();
      const missing = Array.isArray(d.systems_failed) ? d.systems_failed.length : 0;
      if (resumable) txt += ' (a stopped update can be continued)';
      else if (missing) txt += ` (${missing} system${missing === 1 ? '' : 's'} could not be fetched — Update to retry)`;
      if (files) txt += `, the downloaded ${kind} kept (${size})`;
      parts.push(txt);
    } else if (resumable) {
      parts.push(`${name}: ${files.done} of ${files.of} systems downloaded — continue the download for the rest`);
    } else if (files) {
      parts.push(`${name} not in use — the downloaded ${kind}${files.release ? ` of ${files.release}` : ''} kept (${size})`);
    }
    if (dl) {
      if (!dl.dataset.label) dl.dataset.label = dl.textContent;      // "Download X (~N MB)"
      dl.disabled = running;
      let label = dl.dataset.label;
      let local = false;
      if (resumable) label = `Continue the ${name} download`;
      else if (d.present) label = `Update ${name}`;
      else if (files) { label = `Add ${name} from the downloaded ${kind}`; local = true; }
      dl.textContent = label;
      dl.dataset.gtLocal = local ? '1' : '';
    }
    if (rm) rm.hidden = !d.present || running;
    if (del) {
      del.hidden = !files || running;
      del.textContent = `Delete the downloaded ${kind}`;
      del.setAttribute('aria-label', `Delete the downloaded ${name} ${kind}${files ? ` (${size})` : ''}`);
    }
    if (stop) stop.hidden = !running;
    // A download that just finished: the game names update in the background.
    if (_gtWasBusy[src] && !running) {
      if (d.state === 'done') {
        const changed = d.changed !== false;
        const partial = Array.isArray(d.systems_failed) && d.systems_failed.length;
        const said = d.message && d.message !== 'Downloaded';
        let msg = said ? `${name}: ${d.message}` : `${name} titles downloaded`;
        if (changed) msg += said ? '. Game names are being updated.' : '; game names are being updated.';
        else msg += '.';
        showMsg('md-game-titles-msg', msg, partial ? 'info' : 'ok');
        if (changed) setTimeout(() => document.dispatchEvent(new CustomEvent('soniqboom:library-changed')), 4000);
      } else if (d.state === 'error') {
        showMsg('md-game-titles-msg', `${name}: ${d.message}`, 'err');
      } else if (d.state === 'interrupted') {
        showMsg('md-game-titles-msg', `${name}: ${d.message}`, 'info');
      }
    }
    _gtWasBusy[src] = running;
  }
  const lp = s.last_pass;
  if (s.pass_waiting_for_scan) parts.push('game names update when the scan finishes');
  else if (s.pass_busy) parts.push('updating game names…');
  else if (lp && typeof lp.named === 'number') parts.push(_gtNamedText(lp.primary, lp.named));
  if (s.enabled === false) parts.push('off');
  line.textContent = `Game title lists: ${parts.join(' · ') || '—'}`;
  // A live region: while it changes every poll, screen readers wait (aria-busy)
  // and announce the settled line.
  line.setAttribute('aria-busy', busy ? 'true' : 'false');
  _gtLastBusy = busy;
  _gtFailedPolls = 0;
  clearTimeout(_gtPoll);
  if (busy) _gtPoll = setTimeout(loadGameTitlesStatus, 1500);
}

async function loadGameTitlesStatus() {
  try {
    const res = await api('/admin/game-titles/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _renderGameTitlesStatus(await res.json());
  } catch (_) {
    // The server may be restarting mid-download: keep asking while something
    // was running (backing off), so the line doesn't freeze on old progress.
    if (_gtLastBusy) {
      _gtFailedPolls += 1;
      const line = document.getElementById('md-game-titles-status');
      if (line && _gtFailedPolls === 1) line.textContent = 'Game title lists: waiting for the server…';
      clearTimeout(_gtPoll);
      _gtPoll = setTimeout(loadGameTitlesStatus, Math.min(15000, 1500 * 2 ** _gtFailedPolls));
    }
  }
}

function _gtFocusMain(src) {
  const dl = document.getElementById(`md-gt-dl-${src}`);
  if (dl && !dl.hidden) dl.focus();
}

document.querySelectorAll('[data-gt-source]').forEach(btn => btn.addEventListener('click', async () => {
  const src = btn.dataset.gtSource;
  const local = btn.dataset.gtLocal === '1';
  const name = _GT_NAMES[src] || src;
  btn.disabled = true;
  showMsg('md-game-titles-msg', local
    ? `Adding the ${name} title list from the downloaded ${_GT_FILES[src] || 'files'}…`
    : `Downloading the ${name} title list…`, 'info');
  try {
    const res = await api(`/admin/game-titles/download/${src}${local ? '?local=true' : ''}`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    await loadGameTitlesStatus();
  } catch (err) {
    btn.disabled = false;
    showMsg('md-game-titles-msg', `Could not start the download: ${err.message}`, 'err');
  }
}));

document.querySelectorAll('[data-gt-stop]').forEach(btn => btn.addEventListener('click', async () => {
  const src = btn.dataset.gtStop;
  btn.disabled = true;
  try {
    const res = await api(`/admin/game-titles/download/${src}/stop`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    _renderGameTitlesStatus(await res.json());
    _gtFocusMain(src);
  } catch (err) {
    showMsg('md-game-titles-msg', `Could not stop the download: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
}));

document.querySelectorAll('[data-gt-remove]').forEach(btn => btn.addEventListener('click', async () => {
  const src = btn.dataset.gtRemove;
  const name = _GT_NAMES[src] || src;
  btn.disabled = true;
  try {
    const res = await api(`/admin/game-titles/download/${src}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const st = await res.json();
    _renderGameTitlesStatus(st);
    const kept = st && st.downloads && st.downloads[src] && st.downloads[src].files;
    showMsg('md-game-titles-msg', `Removed the ${name} title list; game names are being updated.`
      + (kept ? ` The downloaded ${_GT_FILES[src] || 'files'} ${src === 'tosec' ? 'is' : 'are'} kept — Add ${name} puts the list back without downloading.` : ''), 'ok');
    document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    _gtFocusMain(src);
  } catch (err) {
    showMsg('md-game-titles-msg', `Could not remove the list: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
}));

document.querySelectorAll('[data-gt-delete]').forEach(btn => btn.addEventListener('click', async () => {
  const src = btn.dataset.gtDelete;
  const name = _GT_NAMES[src] || src;
  const kind = _GT_FILES[src] || 'files';
  const ok = await styledConfirm(
    `The downloaded ${name} ${kind} ${src === 'tosec' ? 'is' : 'are'} deleted from SoniqBoom's data folder; `
    + 'your title list stays and your music files are not touched. The next download fetches '
    + `${src === 'tosec' ? 'it' : 'them'} again.`,
    { title: `Delete the downloaded ${name} ${kind}?`, okLabel: 'Delete' });
  if (!ok) return;
  btn.disabled = true;
  try {
    const res = await api(`/admin/game-titles/files/${src}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    _renderGameTitlesStatus(await res.json());
    showMsg('md-game-titles-msg', `Deleted the downloaded ${name} ${kind}.`, 'ok');
    _gtFocusMain(src);
  } catch (err) {
    showMsg('md-game-titles-msg', `Could not delete the files: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
}));

// ── Metadata tab: UADE song database (audacious-uade-tools) ──────────────────

function _renderSongdbStatus(s) {
  const line = document.getElementById('md-songdb-status');
  if (!line) return;
  if (!s || typeof s !== 'object') { line.textContent = 'Index: —'; return; }
  let txt;
  if (s.refreshing)                     txt = 'Index: downloading…';
  else if (!s.exists || !s.meta_rows)   txt = 'Index: not downloaded yet';
  else {
    const built = s.built_at ? new Date(s.built_at * 1000).toLocaleDateString() : 'unknown date';
    txt = `Index: ${Number(s.meta_rows).toLocaleString()} songs with metadata, `
        + `${Number(s.length_rows || 0).toLocaleString()} with lengths (built ${built})`;
  }
  if (s.applying) txt += ' · applying…';
  else if (s.last_apply) {
    txt += ` · last apply: ${Number(s.last_apply.matched || 0).toLocaleString()} matched, `
         + `${Number(s.last_apply.updated || 0).toLocaleString()} updated`;
  }
  if (s.error) txt += ` · ${s.error}`;
  line.textContent = txt;
  const cb = document.getElementById('md-songdb-autoapply');
  if (cb && typeof s.auto_apply === 'boolean') cb.checked = s.auto_apply;
}

async function loadSongdbStatus() {
  try {
    const res = await api('/admin/songdb/status');
    if (res.ok) _renderSongdbStatus(await res.json());
  } catch (_) { /* non-critical */ }
}

document.getElementById('md-songdb-autoapply')?.addEventListener('change', async (e) => {
  const cb = e.currentTarget; const enabled = cb.checked; cb.disabled = true;
  try {
    const res = await api('/admin/songdb/auto-apply',
                          { method: 'POST', body: JSON.stringify({ enabled }) });
    const s = await res.json().catch(() => null);
    if (s && !s.error) _renderSongdbStatus(s);
    else { cb.checked = !enabled; showMsg('md-songdb-msg', (s && s.error) || `Failed: ${res.status}`, 'err'); }
  } catch (err) {
    cb.checked = !enabled; showMsg('md-songdb-msg', `Failed: ${err.message}`, 'err');
  } finally { cb.disabled = false; }
});

document.getElementById('md-songdb-refresh')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  const line = document.getElementById('md-songdb-status');
  if (line) line.textContent = 'Index: downloading (~27 MB — can take a minute)…';
  try {
    const res = await api('/admin/songdb/refresh-index', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderSongdbStatus(s);
    if (s && !s.error) {
      showMsg('md-songdb-msg', s.auto_apply
        ? 'Index refreshed — applying it to the library in the background.'
        : 'Index refreshed.', 'ok');
    } else {
      showMsg('md-songdb-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    }
  } catch (err) {
    showMsg('md-songdb-msg', `Refresh failed: ${err.message}`, 'err');
    loadSongdbStatus();
  } finally { btn.disabled = false; }
});

document.getElementById('md-songdb-apply')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  try {
    const res = await api('/admin/songdb/apply', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderSongdbStatus(s);
    if (s && !s.error) {
      const la = s.last_apply || {};
      const albums = typeof la.albums === 'number'
        ? ` (${la.albums.toLocaleString()} album change${la.albums === 1 ? '' : 's'})` : '';
      showMsg('md-songdb-msg',
              `Applied: ${Number(la.matched || 0).toLocaleString()} matched, `
              + `${Number(la.updated || 0).toLocaleString()} tracks updated${albums}.`, 'ok');
      if ((Number(la.updated) || 0) > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    } else {
      showMsg('md-songdb-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    }
  } catch (err) {
    showMsg('md-songdb-msg', `Apply failed: ${err.message}`, 'err');
  } finally { btn.disabled = false; }
});

document.getElementById('md-songdb-reset')?.addEventListener('click', async (e) => {
  if (!confirm(
        'Remove the song database enrichment from your library?\n\n'
        + '• Artists, labels, albums and years it filled are cleared\n'
        + '• Song lengths stay (a track is re-measured when it is played)\n\n'
        + 'Your own edits are kept. This also turns OFF auto-apply after '
        + 'scans, so it stays reset until you click Apply (or re-enable the toggle).')) return;
  const btn = e.currentTarget; btn.disabled = true;
  try {
    const res = await api('/admin/songdb/reset', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderSongdbStatus(s);
    if (s && !s.error) {
      showMsg('md-songdb-msg',
              `Reset: ${Number(s.cleared || 0).toLocaleString()} tracks restored. `
              + `Auto-apply after scans turned off.`, 'ok');
      if ((Number(s.cleared) || 0) > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    } else {
      showMsg('md-songdb-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    }
  } catch (err) {
    showMsg('md-songdb-msg', `Reset failed: ${err.message}`, 'err');
  } finally { btn.disabled = false; }
});

// ── Metadata tab: Demozoo scene groups + HVSC status/rescan ───────────────────

function _renderDemozooStatus(s) {
  const line = document.getElementById('md-demozoo-status');
  if (!line) return;
  if (!s || typeof s !== 'object') { line.textContent = 'Index: —'; return; }
  let txt;
  if (s.refreshing)                txt = 'Index: downloading…';
  else if (!s.exists || !s.names)  txt = 'Index: not downloaded yet';
  else {
    const built = s.built_at ? new Date(s.built_at * 1000).toLocaleDateString() : 'unknown date';
    txt = `Index: ${Number(s.names).toLocaleString()} name→group entries (built ${built})`;
    if (s.games === null) txt += ' \u00b7 refresh the index to add game names';
    else if (Number(s.games) > 0) txt += ` \u00b7 ${Number(s.games).toLocaleString()} game soundtracks`;
  }
  if (s.applying) txt += ' · applying…';
  else if (s.last_apply) {
    txt += ` · last apply: ${Number(s.last_apply.matched || 0).toLocaleString()} matched, `
         + `${Number(s.last_apply.updated || 0).toLocaleString()} tagged`;
  }
  if (s.error) txt += ` · ${s.error}`;
  line.textContent = txt;
  const cb = document.getElementById('md-demozoo-autoapply');
  if (cb && typeof s.auto_apply === 'boolean') cb.checked = s.auto_apply;
}

document.getElementById('md-demozoo-autoapply')?.addEventListener('change', async (e) => {
  const cb = e.currentTarget; const enabled = cb.checked; cb.disabled = true;
  try {
    const res = await api('/admin/demozoo/auto-apply',
                          { method: 'POST', body: JSON.stringify({ enabled }) });
    const s = await res.json().catch(() => null);
    if (s && !s.error) _renderDemozooStatus(s);
    else { cb.checked = !enabled; showMsg('md-demozoo-msg', (s && s.error) || `Failed: ${res.status}`, 'err'); }
  } catch (err) {
    cb.checked = !enabled; showMsg('md-demozoo-msg', `Failed: ${err.message}`, 'err');
  } finally { cb.disabled = false; }
});

async function loadDemozooStatus() {
  try {
    const res = await api('/admin/demozoo/status');
    if (res.ok) _renderDemozooStatus(await res.json());
  } catch (_) { /* non-critical */ }
}

document.getElementById('md-demozoo-refresh')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  const line = document.getElementById('md-demozoo-status');
  if (line) line.textContent = 'Index: downloading (~192 MB — this takes a few minutes)…';
  try {
    const res = await api('/admin/demozoo/refresh-index', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderDemozooStatus(s);
    if (s && !s.error) showMsg('md-demozoo-msg', 'Index refreshed.', 'ok');
    else showMsg('md-demozoo-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
  } catch (err) {
    showMsg('md-demozoo-msg', `Refresh failed: ${err.message}`, 'err');
    loadDemozooStatus();
  } finally { btn.disabled = false; }
});

document.getElementById('md-demozoo-apply')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  try {
    const res = await api('/admin/demozoo/apply', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderDemozooStatus(s);
    if (s && !s.error) {
      showMsg('md-demozoo-msg',
              `Applied: ${Number(s.matched || 0).toLocaleString()} matched, `
              + `${Number(s.updated || 0).toLocaleString()} tracks tagged with scene groups.`, 'ok');
      if ((Number(s.updated) || 0) > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    } else {
      showMsg('md-demozoo-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    }
  } catch (err) {
    showMsg('md-demozoo-msg', `Apply failed: ${err.message}`, 'err');
  } finally { btn.disabled = false; }
});

document.getElementById('md-demozoo-reset')?.addEventListener('click', async (e) => {
  if (!confirm(
        'Remove ALL Demozoo enrichment from your library?\n\n'
        + '• Release years filled from Demozoo (reverted to the file’s own)\n'
        + '• Scene groups\n'
        + '• Auto-filled composers on scene modules\n\n'
        + 'Your own tag edits are kept. This also turns OFF auto-apply after '
        + 'scans, so it stays reset until you click Apply (or re-enable the toggle).')) return;
  const btn = e.currentTarget; btn.disabled = true;
  try {
    const res = await api('/admin/demozoo/reset', { method: 'POST' });
    const s = await res.json().catch(() => null);
    _renderDemozooStatus(s);
    if (s && !s.error) {
      showMsg('md-demozoo-msg',
              `Reset: ${Number(s.cleared || 0).toLocaleString()} tracks restored to file metadata. `
              + `Auto-apply after scans turned off.`, 'ok');
      if ((Number(s.cleared) || 0) > 0) document.dispatchEvent(new CustomEvent('soniqboom:library-changed'));
    } else {
      showMsg('md-demozoo-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    }
  } catch (err) {
    showMsg('md-demozoo-msg', `Reset failed: ${err.message}`, 'err');
  } finally { btn.disabled = false; }
});

// HVSC status/rescan mirrored into the Metadata tab (path setting stays in Renderers).
async function loadMdHvscStatus() {
  const line = document.getElementById('md-hvsc-status');
  if (!line) return;
  try {
    const res = await api('/admin/hvsc/status');
    line.textContent = res.ok
      ? 'SID song-length database: ready'
      : 'Not loaded — set the HVSC documents path under Renderers.';
  } catch (_) { line.textContent = 'Status: —'; }
}

document.getElementById('md-hvsc-rescan')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget; btn.disabled = true;
  try {
    const res = await api('/admin/hvsc/rescan-sids', { method: 'POST' });
    const s = await res.json().catch(() => null);
    if (s && !s.error) showMsg('md-hvsc-msg', 'SID length rescan complete.', 'ok');
    else showMsg('md-hvsc-msg', (s && s.error) || `Failed: ${res.status}`, 'err');
    loadMdHvscStatus();
  } catch (err) {
    showMsg('md-hvsc-msg', `Rescan failed: ${err.message}`, 'err');
  } finally { btn.disabled = false; }
});

// ── My Account (every signed-in user) ───────────────────────────────────────

(function _wireAccount() {
  const info  = document.getElementById('my-account-info');
  const lbEl  = document.getElementById('setting-lb-token');
  const fmEl  = document.getElementById('setting-lastfm-sk');
  const save  = document.getElementById('btn-save-account');
  const cpw   = document.getElementById('btn-change-password');
  if (!info || !lbEl || !fmEl) return;

  if (Auth.user) {
    const u = Auth.user;
    // HTML-escape display_name and username — they're user-controlled and
    // displayed via innerHTML in this card.  Without the escape, a display
    // name containing markup would render (stored-XSS).
    const safeName = _esc(u.display_name || u.username);
    const safeRole = _esc(u.role);
    info.innerHTML = `Signed in as <strong>${safeName}</strong> · role <code>${safeRole}</code>.  ` +
      'Tokens are stored per-user and used to forward your plays to last.fm and/or ListenBrainz.';
    // ``listenbrainz_token`` and ``lastfm_session_key`` come back as
    // booleans from the API (presence flag, not the value), so we only
    // show a placeholder hint when one is set.
    lbEl.placeholder = u.listenbrainz_token ? '(currently set — type to change)'
                                            : 'Paste token to enable scrobbling';
    fmEl.placeholder = u.lastfm_session_key ? '(currently set — type to change)'
                                            : 'Paste session key to enable scrobbling';
  }

  save?.addEventListener('click', async () => {
    const lb = lbEl.value.trim();
    const fm = fmEl.value.trim();
    try {
      const res = await api('/me/tokens', {
        method: 'PUT',
        body: JSON.stringify({
          listenbrainz_token: lb,
          lastfm_session_key: fm,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showMsg('admin-account-msg', `Failed: ${data.detail || res.status}`, 'err');
        return;
      }
      showMsg('admin-account-msg', 'Scrobble tokens saved.', 'ok');
      lbEl.value = '';
      fmEl.value = '';
    } catch (err) {
      showMsg('admin-account-msg', `Network error: ${err.message}`, 'err');
    }
  });

  cpw?.addEventListener('click', async () => {
    const r = await styledPasswordDialog({
      title:   'Change password',
      message: 'You stay signed in on this device.  Other sessions sign out.',
      requireCurrent: true,
    });
    if (!r) return;
    try {
      await Auth.changePassword(r.current, r.password);
      showMsg('admin-account-msg', 'Password updated.', 'ok');
    } catch (err) {
      showMsg('admin-account-msg', `Failed: ${err.message}`, 'err');
    }
  });
})();

// ── Subsonic app password (My Account, and the non-admin Preferences dialog) ──
// Subsonic apps that sign in with a token (``t = md5(password + salt)``) cannot
// use the scrypt-hashed login password, so each user may set a separate Subsonic
// app password (PUT /api/me/subsonic-password; the server only ever reports
// WHETHER one is set).  We generate a random one here and show it once, with
// Copy; the revealed value is wiped when the panel or dialog closes.  Replacing
// or removing one takes a second click within a few seconds (no modal, so this
// works inside the Preferences dialog too).
const _subsonicPwReveals = new Set();
function _clearRevealedSecrets() {
  for (const el of _subsonicPwReveals) {
    el.textContent = '';
    el.closest('.subsonic-pw-reveal')?.setAttribute('hidden', '');
    if (!el.isConnected) _subsonicPwReveals.delete(el);     // a closed dialog's copy
  }
}
function _randomSubsonicPassword(len = 24) {
  // Unambiguous alphabet (no 0/O, 1/l/I); rejection sampling keeps it unbiased.
  const abc = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  const limit = 256 - (256 % abc.length);
  let out = '';
  while (out.length < len) {
    const bytes = crypto.getRandomValues(new Uint8Array(len * 2));
    for (const b of bytes) {
      if (b < limit && out.length < len) out += abc[b % abc.length];
    }
  }
  return out;
}
function _mountSubsonicPassword(host, idPrefix) {
  if (!host) return;
  const msgId = `${idPrefix}-sspw-msg`;
  host.innerHTML = `
    <div style="font-size:13px;font-weight:600;margin-bottom:4px">Subsonic app password</div>
    <div class="admin-hint sspw-status" role="status" aria-live="polite" style="margin-bottom:4px"></div>
    <div class="admin-hint" style="font-size:11px;margin-bottom:8px">Apps that sign in with a token (salt + hash) work with your SoniqBoom password. Optionally generate a separate app password so apps never hold your login password; once you do, token apps must use it. It is shown once &mdash; copy it into the app.</div>
    <div class="admin-io-row">
      <button type="button" class="sspw-generate">Generate new password</button>
      <button type="button" class="btn-secondary sspw-remove">Remove</button>
    </div>
    <div class="subsonic-pw-reveal subsonic-card-row" style="margin-top:8px" hidden>
      <code class="sspw-value" aria-label="New Subsonic app password"></code>
      <button type="button" class="sspw-copy">Copy</button>
    </div>
    <div id="${msgId}" class="admin-msg" style="display:none"></div>`;
  const status = host.querySelector('.sspw-status');
  const gen    = host.querySelector('.sspw-generate');
  const rem    = host.querySelector('.sspw-remove');
  const reveal = host.querySelector('.subsonic-pw-reveal');
  const value  = host.querySelector('.sspw-value');
  const copy   = host.querySelector('.sspw-copy');
  _subsonicPwReveals.add(value);
  let has = !!(Auth.user && Auth.user.subsonic_password);
  // true: a generated app password is in use; false: token apps use the
  // SoniqBoom password; undefined: the server does not say which.
  let custom = (Auth.user && typeof Auth.user.subsonic_password_custom === 'boolean')
    ? Auth.user.subsonic_password_custom : undefined;
  let armed = null, armTimer = null;       // 'generate' | 'remove' awaiting its second click
  const paint = () => {
    status.textContent = !has
      ? 'Token sign-in is off \u2014 generate an app password to let apps that sign in with a token connect.'
      : custom === true ? 'A generated Subsonic app password is in use \u2014 token apps must use it.'
      : custom === false ? 'Token apps can sign in with your SoniqBoom password.'
      : 'Token sign-in is on \u2014 with your SoniqBoom password, or with your generated app password if you created one.';
    rem.hidden = !has || custom === false;   // nothing generated to remove
  };
  const disarm = () => { armed = null; clearTimeout(armTimer); armTimer = null; };
  const arm = (what, text) => {
    armed = what;
    showMsg(msgId, text, 'warn');
    clearTimeout(armTimer);
    armTimer = setTimeout(() => { if (armed === what) { disarm(); showMsg(msgId, ''); } }, 8000);
  };
  const put = async (password) => {
    // Disabling the clicked button drops keyboard focus to <body>: put it back
    // (on Generate if Remove has just hidden itself).
    const had = document.activeElement;
    gen.disabled = rem.disabled = true;
    try {
      await api('/me/subsonic-password', { method: 'PUT', body: JSON.stringify({ password }) });
      has = !!password;
      custom = has;
      if (Auth.user) {
        Auth.user.subsonic_password = has;
        Auth.user.subsonic_password_custom = has;
      }
      return true;
    } catch (err) {
      showMsg(msgId, `Could not save: ${err.message}`, 'err');
      return false;
    } finally {
      gen.disabled = rem.disabled = false;
      paint();
      if ((had === gen || had === rem) && !host.contains(document.activeElement)) {
        (had === rem && !rem.hidden ? rem : gen).focus();
      }
    }
  };
  gen.addEventListener('click', async () => {
    if (has && armed !== 'generate') {
      arm('generate', custom === false
        ? 'After this, apps that sign in with a token must use the new app password instead of your SoniqBoom password. Click \u201cGenerate new password\u201d again to confirm.'
        : 'This replaces your current Subsonic app password \u2014 apps using it will need the new one. Click \u201cGenerate new password\u201d again to confirm.');
      return;
    }
    disarm();
    const pw = _randomSubsonicPassword();
    if (!(await put(pw))) return;
    value.textContent = pw;
    reveal.hidden = false;
    copy.focus();                            // the next step: copy it
    showMsg(msgId, 'Saved. Copy it into your Subsonic app now \u2014 it is not shown again.', 'ok');
  });
  rem.addEventListener('click', async () => {
    if (armed !== 'remove') {
      arm('remove', 'Apps that sign in with a token will stop connecting. Click \u201cRemove\u201d again to confirm.');
      return;
    }
    disarm();
    if (!(await put(''))) return;
    _clearRevealedSecrets();
    showMsg(msgId, 'Removed.', 'ok');
  });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(value.textContent);
      copy.textContent = 'Copied!';
      setTimeout(() => { copy.textContent = 'Copy'; }, 1300);
    } catch {
      const range = document.createRange();
      range.selectNodeContents(value);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });
  paint();
}
_mountSubsonicPassword(document.getElementById('my-subsonic-password'), 'acct');

// ── Subsonic API keys (OpenSubsonic apiKeyAuthentication) ────────────────────
// Per-user keys an app sends as ``apiKey=`` instead of a username + password
// (/api/me/api-keys).  A new key is shown once, with Copy — the server keeps only
// its hash — and wiped with the other revealed secrets; revoking one signs that
// app out.  The list is read when the block mounts and each time the System tab
// is shown again; a server without key support hides the block.
const _apiKeyRefreshers = [];
function _fmtWhen(sec) {
  return sec ? new Date(sec * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'never';
}
function _mountApiKeys(host, idPrefix) {
  if (!host) return null;
  const msgId = `${idPrefix}-apikey-msg`;
  host.innerHTML = `
    <div style="font-size:13px;font-weight:600;margin-bottom:4px">API keys for Subsonic apps</div>
    <div class="admin-hint" style="font-size:11px;margin-bottom:8px">Apps that support OpenSubsonic API keys can sign in with a key instead of a password. A new key is shown once &mdash; copy it into the app. Revoke a key to sign that app out.</div>
    <div class="apikey-list" role="list" style="display:none;flex-direction:column;gap:6px;margin-bottom:8px"></div>
    <div class="admin-hint apikey-empty" style="font-size:11px;margin-bottom:8px;display:none">No API keys yet.</div>
    <div class="admin-io-row" style="flex-wrap:wrap">
      <input type="text" class="apikey-name pl-modal-input" maxlength="64" autocomplete="off" spellcheck="false"
             placeholder="Label, e.g. Symfonium on my phone" aria-label="Label for the new API key"
             style="flex:1 1 160px;min-width:0;width:auto;margin:0">
      <button type="button" class="apikey-create">Create key</button>
    </div>
    <div class="subsonic-pw-reveal subsonic-card-row" style="margin-top:8px" hidden>
      <code class="apikey-value" aria-label="New API key" style="word-break:break-all"></code>
      <button type="button" class="apikey-copy">Copy</button>
    </div>
    <div id="${msgId}" class="admin-msg" style="display:none"></div>`;
  const list   = host.querySelector('.apikey-list');
  const empty  = host.querySelector('.apikey-empty');
  const name   = host.querySelector('.apikey-name');
  const create = host.querySelector('.apikey-create');
  const reveal = host.querySelector('.subsonic-pw-reveal');
  const value  = host.querySelector('.apikey-value');
  const copy   = host.querySelector('.apikey-copy');
  _subsonicPwReveals.add(value);
  let armedId = null, armTimer = null;
  const render = (keys) => {
    list.replaceChildren();
    // The empty note sits OUTSIDE the list (a list holds only list items) and
    // the empty list is hidden with it (its inline flex beats [hidden]).
    empty.style.display = keys.length ? 'none' : '';
    list.style.display = keys.length ? 'flex' : 'none';
    if (!keys.length) return;
    for (const k of keys) {
      const row = document.createElement('div');
      row.setAttribute('role', 'listitem');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:12px';
      const txt = document.createElement('div');
      txt.style.cssText = 'flex:1 1 auto;min-width:0;overflow-wrap:anywhere';
      const nm = document.createElement('strong');
      nm.textContent = k.name || 'Unnamed key';
      const meta = document.createElement('div');
      meta.className = 'admin-hint';
      meta.style.fontSize = '11px';
      meta.textContent = `Created ${_fmtWhen(k.created)} · last used ${_fmtWhen(k.last_used)}`;
      txt.append(nm, meta);
      const rv = document.createElement('button');
      rv.type = 'button';
      rv.className = 'btn-secondary';
      rv.textContent = 'Revoke';
      rv.setAttribute('aria-label', `Revoke API key ${k.name || 'Unnamed key'}`);
      rv.addEventListener('click', async () => {
        if (armedId !== k.id) {
          armedId = k.id;
          clearTimeout(armTimer);
          armTimer = setTimeout(() => { if (armedId === k.id) { armedId = null; showMsg(msgId, ''); } }, 8000);
          showMsg(msgId, `The app using \u201c${k.name || 'Unnamed key'}\u201d will be signed out. Click \u201cRevoke\u201d again to confirm.`, 'warn');
          return;
        }
        armedId = null; clearTimeout(armTimer);
        rv.disabled = true;
        try {
          await api(`/me/api-keys/${encodeURIComponent(k.id)}`, { method: 'DELETE' });
          showMsg(msgId, 'Revoked.', 'ok');
        } catch (err) {
          showMsg(msgId, `Could not revoke: ${err.message}`, 'err');
        }
        refresh();
      });
      row.append(txt, rv);
      list.appendChild(row);
    }
  };
  let loading = null;
  const refresh = () => (loading ||= (async () => {
    try {
      const res = await api('/me/api-keys', { allowNonOK: true });
      // A server without API keys answers 404 — or, behind an older build's SPA
      // fallback, a page of HTML: either way there is nothing to manage here.
      if (res.status === 404 || !/json/i.test(res.headers.get('Content-Type') || '')) {
        host.hidden = true;
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      host.hidden = false;
      render(((await res.json()) || {}).keys || []);
    } catch (err) {
      showMsg(msgId, `Could not load API keys: ${err.message}`, 'err');
    } finally { loading = null; }
  })());
  create.addEventListener('click', async () => {
    create.disabled = true;
    try {
      const res = await api('/me/api-keys', { method: 'POST', body: JSON.stringify({ name: name.value.trim() }) });
      const j = await res.json();
      value.textContent = j.key || '';
      reveal.hidden = !j.key;
      name.value = '';
      showMsg(msgId, 'Key created. Copy it into your Subsonic app now \u2014 it is not shown again.', 'ok');
    } catch (err) {
      showMsg(msgId, `Could not create a key: ${err.message}`, 'err');
    } finally {
      create.disabled = false;
      refresh();
    }
  });
  // Enter in the label field creates the key (and must not submit a dialog around it).
  name.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    e.stopPropagation();
    create.click();
  });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(value.textContent);
      copy.textContent = 'Copied!';
      setTimeout(() => { copy.textContent = 'Copy'; }, 1300);
    } catch {
      const range = document.createRange();
      range.selectNodeContents(value);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });
  refresh();
  return refresh;
}
{
  const r = _mountApiKeys(document.getElementById('my-api-keys'), 'acct');
  if (r) _apiKeyRefreshers.push(r);
}

// ── Users tab (admin-only) ───────────────────────────────────────────────────

async function loadUsers() {
  const tbody = document.getElementById('users-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text2)">Loading…</td></tr>';
  try {
    const res = await api('/users');
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      tbody.innerHTML = `<tr><td colspan="6" style="color:#ffb0b0">${data.detail || 'Failed to load users.'}</td></tr>`;
      return;
    }
    const { users } = await res.json();
    tbody.innerHTML = '';
    for (const u of users) {
      tbody.appendChild(_renderUserRow(u));
    }
    if (!users.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text2)">No users.</td></tr>';
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:#ffb0b0">Network error: ${err.message}</td></tr>`;
  }
}

function _renderUserRow(u) {
  const tr = document.createElement('tr');
  tr.dataset.userId = u.id;
  const last = u.last_login_at
    ? new Date(u.last_login_at * 1000).toLocaleString()
    : '—';
  const isSelf = Auth.user && Auth.user.id === u.id;
  tr.innerHTML = `
    <td class="col-username">${_esc(u.username)}${isSelf ? ' <span style="color:var(--text2);font-size:10px">(you)</span>' : ''}</td>
    <td>${_esc(u.display_name || '')}</td>
    <td>
      <select class="role-select" ${isSelf ? 'data-self="1"' : ''}>
        <option value="admin"${u.role === 'admin' ? ' selected' : ''}>admin</option>
        <option value="edit"${u.role === 'edit' ? ' selected' : ''}>edit</option>
        <option value="readonly"${u.role === 'readonly' ? ' selected' : ''}>read-only</option>
      </select>
    </td>
    <td><label style="cursor:pointer"><input type="checkbox" class="enabled-toggle" ${u.enabled ? 'checked' : ''}> ${u.enabled ? 'enabled' : 'disabled'}</label></td>
    <td style="color:var(--text2);font-size:11px">${last}</td>
    <td class="users-row-actions">
      <button class="btn-set-pw">Set password</button>
      <button class="btn-delete danger" ${isSelf ? 'disabled title="Sign in as another admin to delete this account"' : ''}>Delete</button>
    </td>
  `;
  const roleSel = tr.querySelector('.role-select');
  const enToggle = tr.querySelector('.enabled-toggle');

  roleSel.addEventListener('change', async (e) => {
    const newRole = e.target.value;
    const oldRole = u.role;
    // Confirm privilege jumps and downgrades — a misclick on an inline
    // <select> should not silently grant or revoke admin powers.  No-op
    // re-selection of the same value skips the prompt.
    if (newRole === oldRole) return;
    const isPromote = newRole === 'admin' && oldRole !== 'admin';
    const isDemote  = oldRole === 'admin' && newRole !== 'admin';
    if (isPromote || isDemote) {
      const ok = await styledConfirm(
        isPromote
          ? `Make "${u.username}" an admin? They'll be able to manage users and settings.`
          : `Remove admin from "${u.username}"? They'll lose access to user management and settings.`,
        { title: isPromote ? 'Promote to admin' : 'Demote from admin',
          okLabel: isPromote ? 'Promote' : 'Demote',
          dangerColor: isDemote },
      );
      if (!ok) {
        e.target.value = oldRole;          // revert select UI
        return;
      }
    }
    await _updateUser(u.id, { role: newRole });
  });

  enToggle.addEventListener('change', async (e) => {
    const enabled = e.target.checked;
    if (!enabled) {
      const ok = await styledConfirm(
        `Disable "${u.username}"? They can't sign in and any open sessions are kicked out.`,
        { title: 'Disable account', okLabel: 'Disable', dangerColor: true },
      );
      if (!ok) {
        e.target.checked = true;            // revert toggle UI
        return;
      }
    }
    // Update the visible label text in lockstep with the toggle so the row
    // doesn't read "enabled" while the checkbox is unchecked.
    const labelTextNode = e.target.parentNode.lastChild;
    if (labelTextNode && labelTextNode.nodeType === Node.TEXT_NODE) {
      labelTextNode.nodeValue = ' ' + (enabled ? 'enabled' : 'disabled');
    }
    await _updateUser(u.id, { enabled });
  });
  tr.querySelector('.btn-set-pw').addEventListener('click', async () => {
    // Own-row: route through the change-password flow that requires
    // the current password — admin-bypass shouldn't apply to yourself.
    if (isSelf) {
      const r = await styledPasswordDialog({
        title:   'Change my password',
        message: 'You stay signed in on this device.  Other sessions sign out.',
        requireCurrent: true,
      });
      if (!r) return;
      try {
        await Auth.changePassword(r.current, r.password);
        showMsg('users-list-msg', 'Password updated.', 'ok');
      } catch (err) {
        showMsg('users-list-msg', _passwordError(err), 'err');
      }
      return;
    }
    // Other user: admin reset (no current-password challenge).
    const res0 = await styledPasswordDialog({
      title:   `Set password for ${u.username}`,
      message: 'The user will need to sign back in with the new password.',
      requireCurrent: false,
    });
    if (!res0) return;
    try {
      const res = await api(`/users/${u.id}/password`, {
        method: 'POST',
        body:   JSON.stringify({ new_password: res0.password }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showMsg('users-list-msg', _passwordErrorFromResponse(res.status, data), 'err');
        return;
      }
      showMsg('users-list-msg', `Password updated for ${u.username}.`, 'ok');
    } catch (err) {
      showMsg('users-list-msg', _networkError(err), 'err');
    }
  });
  tr.querySelector('.btn-delete').addEventListener('click', async () => {
    if (isSelf) return;
    const ok = await styledConfirm(
      `Delete user "${u.username}"?  Their personal playlists, ratings, and history will be lost.`,
      { title: 'Delete user', okLabel: 'Delete', dangerColor: true },
    );
    if (!ok) return;
    try {
      const res = await api(`/users/${u.id}`, { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showMsg('users-list-msg', `Failed: ${data.detail || res.status}`, 'err');
        return;
      }
      tr.remove();
      showMsg('users-list-msg', `Deleted ${u.username}.`, 'ok');
    } catch (err) {
      showMsg('users-list-msg', `Network error: ${err.message}`, 'err');
    }
  });
  return tr;
}

async function _updateUser(id, patch) {
  try {
    const res = await api(`/users/${id}`, {
      method: 'PATCH',
      body:   JSON.stringify(patch),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showMsg('users-list-msg', `Update failed: ${data.detail || res.status}`, 'err');
      // Reload to revert UI to actual server state.
      loadUsers();
      return;
    }
    showMsg('users-list-msg', 'Saved.', 'ok');
  } catch (err) {
    showMsg('users-list-msg', `Network error: ${err.message}`, 'err');
  }
}

document.getElementById('users-new-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = document.getElementById('users-new-username').value.trim();
  const password = document.getElementById('users-new-password').value;
  const role     = document.getElementById('users-new-role').value;
  const display  = document.getElementById('users-new-display').value.trim();
  try {
    const res = await api('/users', {
      method: 'POST',
      body:   JSON.stringify({
        username, password, role,
        display_name: display || null,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showMsg('users-new-msg', `Failed: ${data.detail || res.status}`, 'err');
      return;
    }
    showMsg('users-new-msg', `Added user "${username}".`, 'ok');
    document.getElementById('users-new-username').value = '';
    document.getElementById('users-new-password').value = '';
    document.getElementById('users-new-display').value  = '';
    loadUsers();
  } catch (err) {
    showMsg('users-new-msg', `Network error: ${err.message}`, 'err');
  }
});

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ── Friendly error messages ────────────────────────────────────────────
// Convert raw HTTP failures into human-readable strings.  The previous
// pattern (``Failed: ${data.detail || res.status}``) leaked HTTP codes
// like "Failed: 502" with no recovery hint.  These helpers branch on the
// status to give the user something actionable.

function _networkError(err) {
  return `Couldn't reach the server (${err?.message || err}). ` +
         `Check your connection or try again.`;
}

function _httpStatusError(status, detail) {
  // ``detail`` is the JSON body's "detail" field if present — useful when
  // it's a specific server-supplied message ("Username already taken: …").
  if (detail && status >= 400 && status < 500 && status !== 401) {
    return detail;          // server gave a specific reason — trust it
  }
  switch (status) {
    case 400: return detail || "The server rejected that request — check your input.";
    case 401: return "Your session expired. Sign in again.";
    case 403: return "You don't have permission for that.";
    case 404: return detail || "Not found.";
    case 409: return detail || "That conflicts with an existing record.";
    case 429: return "Too many requests — wait a moment and try again.";
    case 502:
    case 503:
    case 504: return "The server is unavailable. Wait a moment and try again.";
    default:
      if (status >= 500) return "The server hit a snag. Try again in a moment.";
      return detail || `Request failed (${status}).`;
  }
}

function _passwordErrorFromResponse(status, data) {
  // Specialised for password-set failures: the server returns 400 with a
  // detail like "Password must be at least 8 characters" — pass that
  // straight through, otherwise fall back to the generic helper.
  if (status === 400 && data?.detail) return data.detail;
  return _httpStatusError(status, data?.detail);
}

function _passwordError(err) {
  // ``Auth.changePassword`` throws Error("status N") on failure.  Try to
  // parse out the HTTP code and route to the friendlier helper.
  const m = /HTTP\s+(\d+)/.exec(err?.message || '');
  if (m) return _httpStatusError(parseInt(m[1], 10), err.message);
  return err?.message || 'Password update failed.';
}

// ── Header user chip ────────────────────────────────────────────────────────

(function _wireUserChip() {
  const chip = document.getElementById('user-chip');
  const name = document.getElementById('user-chip-name');
  const out  = document.getElementById('user-chip-logout');
  if (!chip || !name || !out) return;
  if (Auth.user) {
    name.textContent = Auth.user.display_name || Auth.user.username;
    chip.hidden = false;
    chip.title = `Signed in as ${Auth.user.username} (${Auth.user.role})`;
  }
  out.addEventListener('click', () => Auth.logout());
})();

export const Admin = { open };
