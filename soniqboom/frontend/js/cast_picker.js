// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Cast picker — UI controller for ``/api/cast/*``.
 *
 * Surfaces the discovered Cast / DLNA / AirPlay targets in a popover
 * anchored to the toolbar #btn-cast button and lets the user route
 * the currently-playing track to any of them.  Modeled on the
 * Spotify Connect / AirPlay / YouTube Music picker pattern, with
 * stricter accessibility:
 *
 *   • Focus is captured on open, restored to the trigger on close.
 *   • Tab cycles within the popover (focus trap).
 *   • Esc closes; Enter on a device routes; arrow keys move focus
 *     between list items.
 *   • Live regions announce "Searching", "No devices", "Casting to X"
 *     so screen-reader users hear state changes without polling the
 *     visual UI.
 *
 * State machine:
 *
 *      closed ──open──▶ idle ──refresh──▶ loading
 *                         ▲                  │
 *                         │                  ▼
 *                         └── targets ◀──ready
 *                              │
 *                              ▼
 *                            playing (cast active)
 *
 * Backend contract:
 *   GET  /api/cast/status     → {available, install_hints}
 *   GET  /api/cast/targets    → {targets: [{id, name, protocol, …}]}
 *   POST /api/cast/play       → {ok, delivered_codec, source_codec, transcode}
 *   POST /api/cast/control    → {ok, action}
 *   POST /api/cast/preference → {ok, pref}
 *   POST /api/cast/disconnect → {ok, closed}
 */

(() => {
  const $ = (sel) => document.querySelector(sel);

  // ── Element refs (resolved lazily on first open) ──────────────────────
  let _btn         = null;
  let _overlay     = null;
  let _panel       = null;
  let _closeBtn    = null;
  let _refreshBtn  = null;
  let _stopBtn     = null;
  let _list        = null;
  let _loadingEl   = null;
  let _emptyEl     = null;
  let _emptyHint   = null;
  let _errorEl     = null;
  let _errorText   = null;
  let _errorHint   = null;
  let _activeEl    = null;
  let _activeName  = null;
  let _prefRadios  = null;
  let _previouslyFocused = null;

  // ── Application state ────────────────────────────────────────────────
  let _opened           = false;
  let _targets          = [];            // last /api/cast/targets response
  let _activeTargetId   = null;          // id of currently-casting target
  let _activeTargetName = null;
  let _pref             = 'auto';
  // Cache the device list briefly so toggling the picker doesn't trigger
  // a full SSDP/mDNS sweep every time.  Server-side already caches for
  // 30 s, but the client cache avoids the 50–200 ms HTTP round-trip too.
  let _targetsLastFetch = 0;
  const TARGETS_TTL_MS  = 15_000;

  // ── Resolve DOM (idempotent) ─────────────────────────────────────────
  function _ensureRefs() {
    if (_btn) return;
    _btn        = $('#btn-cast');
    _overlay    = $('#cast-overlay');
    _panel      = $('#cast-panel');
    _closeBtn   = $('#cast-close');
    _refreshBtn = $('#cast-refresh');
    _stopBtn    = $('#cast-stop');
    _list       = $('#cast-list');
    _loadingEl  = $('#cast-loading');
    _emptyEl    = $('#cast-empty');
    _emptyHint  = $('#cast-empty-hint');
    _errorEl    = $('#cast-error');
    _errorText  = $('#cast-error-text');
    _errorHint  = $('#cast-error-hint');
    _activeEl   = $('#cast-active');
    _activeName = $('#cast-active-name');
    _prefRadios = document.querySelectorAll('input[name="cast-pref"]');
  }

  // ── Network ───────────────────────────────────────────────────────────
  async function _apiGet(path) {
    const r = await fetch(path, { credentials: 'same-origin' });
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    return r.json();
  }
  async function _apiPost(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      credentials: 'same-origin',
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      // FastAPI's HTTPException(detail=...) can be either a plain string
      // or a dict (e.g. the AirPlay 412 response carries
      // ``{requires_pairing: true, identifier: ...}``).  Preserve both
      // shapes on the thrown Error so callers can branch on it.
      const detail = err.detail;
      const msg = typeof detail === 'string' ? detail :
                  (detail && detail.message) || `${path} → ${r.status}`;
      const e = new Error(msg);
      e.status = r.status;
      e.detail = detail;
      throw e;
    }
    return r.json();
  }

  // ── Open / close ─────────────────────────────────────────────────────
  function open() {
    _ensureRefs();
    if (_opened) return;
    _opened = true;
    _previouslyFocused = document.activeElement;
    _overlay.classList.remove('hidden');
    _btn.setAttribute('aria-expanded', 'true');
    // Make the rest of the page inert while the picker is open — UX-3 P2
    // flagged that the lyrics-resize handle / other overlays remained
    // clickable beneath our scrim.
    const main = document.getElementById('main-content');
    if (main && 'inert' in main) main.inert = true;
    // Focus management — UX-1 P0: initial focus should NOT land on the
    // destructive-feeling Refresh icon at the top.  Land on the first
    // device list item if devices are already loaded; otherwise fall
    // back to the Close button (least-action default per WAI-ARIA APG
    // Dialog Modal).
    setTimeout(() => {
      const firstDevice = _list && _list.querySelector('.cast-list-item');
      if (firstDevice && firstDevice.focus) {
        firstDevice.focus();
      } else if (_closeBtn && _closeBtn.focus) {
        _closeBtn.focus();
      }
    }, 0);
    // Fetch state.
    refresh();
    _checkActiveSession();
  }

  function close() {
    if (!_opened) return;
    _opened = false;
    _overlay.classList.add('hidden');
    _btn.setAttribute('aria-expanded', 'false');
    // Restore page interactivity (matches the inert toggle in open()).
    const main = document.getElementById('main-content');
    if (main && 'inert' in main) main.inert = false;
    // Restore focus to the trigger so keyboard users don't get dumped
    // at the document start.
    if (_previouslyFocused && _previouslyFocused.focus) {
      _previouslyFocused.focus();
    }
  }

  // ── State views ──────────────────────────────────────────────────────
  function _showState(which) {
    const states = [_loadingEl, _emptyEl, _errorEl];
    for (const el of states) el.classList.add('hidden');
    _list.style.display = 'none';
    if (which === 'loading') _loadingEl.classList.remove('hidden');
    else if (which === 'empty') _emptyEl.classList.remove('hidden');
    else if (which === 'error') _errorEl.classList.remove('hidden');
    else if (which === 'list')  _list.style.display = '';
  }

  // ── Device discovery ─────────────────────────────────────────────────
  // UX-1 P1: avoid the spinner flash on fast LANs.  Doherty threshold —
  // responses < 400 ms should feel instant, not "loading".  Show the
  // spinner only after 150 ms; once shown keep it visible at least
  // 400 ms so it doesn't pop-and-disappear.
  const _SPINNER_DELAY_MS = 150;
  const _SPINNER_MIN_MS   = 400;
  let _spinnerShowTimer   = null;
  let _spinnerShownAt     = 0;

  function _scheduleSpinner() {
    if (_spinnerShowTimer) return;
    _spinnerShowTimer = setTimeout(() => {
      _spinnerShowTimer = null;
      _spinnerShownAt = Date.now();
      _showState('loading');
    }, _SPINNER_DELAY_MS);
  }

  async function _settleSpinner() {
    // Cancel the pending spinner if it hadn't shown yet — request was
    // faster than _SPINNER_DELAY_MS.
    if (_spinnerShowTimer) {
      clearTimeout(_spinnerShowTimer);
      _spinnerShowTimer = null;
      return;
    }
    // If shown, hold the spinner until the minimum display window passes.
    if (_spinnerShownAt) {
      const elapsed = Date.now() - _spinnerShownAt;
      if (elapsed < _SPINNER_MIN_MS) {
        await new Promise(r => setTimeout(r, _SPINNER_MIN_MS - elapsed));
      }
      _spinnerShownAt = 0;
    }
  }

  let _lastFailedTarget = null;  // For retry-from-error.

  async function refresh(force = false) {
    _ensureRefs();
    const now = Date.now();
    const stale = now - _targetsLastFetch > TARGETS_TTL_MS;
    if (!force && _targets.length && !stale) {
      _renderList();
      return;
    }
    _scheduleSpinner();
    try {
      // Status: surfaces install hints if a backend dep is missing.
      const statusReq  = _apiGet('/api/cast/status');
      const targetsReq = _apiGet('/api/cast/targets');
      const [status, data] = await Promise.all([statusReq, targetsReq]);
      _targets = (data && data.targets) || [];
      _targetsLastFetch = Date.now();
      await _settleSpinner();
      if (!_targets.length) {
        _renderEmptyWithHints(status);
        return;
      }
      _renderList();
    } catch (err) {
      await _settleSpinner();
      _showError(
        "Couldn't reach the cast service.",
        "Check that SoniqBoom is running and you have a session cookie.",
      );
      // eslint-disable-next-line no-console
      console.error('[cast-picker] refresh failed:', err);
    }
  }

  function _renderEmptyWithHints(status) {
    _ensureRefs();
    const installable = (status && status.install_hints) || {};
    const installCount = Object.keys(installable).length;
    if (installCount > 0) {
      // At least one backend isn't installed — surface the install hint
      // so the user knows their LAN devices might still be discoverable
      // after installing.
      const items = Object.entries(installable)
        .map(([proto, cmd]) =>
          `<li><strong>${proto}</strong>: <code>${cmd}</code></li>`)
        .join('');
      _emptyHint.innerHTML = (
        "Some discovery backends aren't installed on the server. " +
        "Install them on the SoniqBoom host to find more devices:" +
        `<ul style="margin:8px 0 0 0; padding-left:20px; text-align:left;">${items}</ul>`
      );
    } else {
      _emptyHint.textContent = (
        "Make sure your speaker, TV, or Chromecast is powered on and " +
        "connected to the same Wi-Fi network as this computer."
      );
    }
    _showState('empty');
  }

  function _showError(text, hint, opts) {
    _ensureRefs();
    _errorText.textContent = text;
    _errorHint.textContent = hint || '';
    // UX-1 P0: error state needs a retry affordance.  Render two
    // actions: "Try again" (re-attempt the last action) + "Refresh
    // devices" (re-discover).  Nielsen H9 — help users recover.
    // Clear any prior buttons so toggling between error reasons doesn't
    // stack them.
    const existing = _errorEl.querySelector('.cast-error-actions');
    if (existing) existing.remove();
    const actions = document.createElement('div');
    actions.className = 'cast-error-actions';
    const showRetry = (opts && opts.retry) !== false && _lastFailedTarget;
    if (showRetry) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'cast-error-btn';
      retry.textContent = 'Try again';
      retry.addEventListener('click', () => {
        const tgt = _lastFailedTarget;
        _lastFailedTarget = null;
        _showState('list');
        // Re-render so the list item is visible while we retry.
        _renderList();
        const btn = _list && _list.querySelector(`.cast-list-item[data-id="${tgt.id}"]`);
        _onPickTarget(tgt, btn || document.createElement('button'));
      });
      actions.appendChild(retry);
    }
    const reSearch = document.createElement('button');
    reSearch.type = 'button';
    reSearch.className = 'cast-error-btn';
    reSearch.textContent = 'Search again';
    reSearch.addEventListener('click', () => {
      _lastFailedTarget = null;
      refresh(true);
    });
    actions.appendChild(reSearch);
    _errorEl.appendChild(actions);
    _showState('error');
  }

  // ── Device list rendering ────────────────────────────────────────────
  const _PROTO_ICONS = {
    cast: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 16.1A5 5 0 0 1 5.9 20M2 12.05A9 9 0 0 1 9.95 20M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/><line x1="2" y1="20" x2="2.01" y2="20"/></svg>`,
    dlna: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>`,
    airplay: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 17H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-1"/><polygon points="12 15 17 21 7 21 12 15"/></svg>`,
  };
  const _PROTO_LABELS = { cast: 'Chromecast', dlna: 'DLNA', airplay: 'AirPlay' };

  function _renderList() {
    _ensureRefs();
    _showState('list');
    _list.innerHTML = '';
    for (const t of _targets) {
      const li   = document.createElement('li');
      const btn  = document.createElement('button');
      btn.type       = 'button';
      btn.className  = 'cast-list-item';
      btn.dataset.id = t.id;
      btn.setAttribute('role', 'listitem');
      const isActive = t.id === _activeTargetId;
      if (isActive) btn.classList.add('is-active');
      btn.innerHTML = `
        <span class="cast-item-icon" aria-hidden="true">${_PROTO_ICONS[t.protocol] || ''}</span>
        <span class="cast-item-info">
          <span class="cast-item-name"></span>
          <span class="cast-item-meta">${_PROTO_LABELS[t.protocol] || t.protocol}${t.model ? ' · ' + escapeHtml(t.model) : ''}</span>
        </span>
        <span class="cast-item-state">${isActive ? 'Connected' : ''}</span>
      `;
      // Set name via textContent to avoid HTML injection from device names
      btn.querySelector('.cast-item-name').textContent = t.name || t.id;
      btn.addEventListener('click', () => _onPickTarget(t, btn));
      li.appendChild(btn);
      _list.appendChild(li);
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ── Casting actions ──────────────────────────────────────────────────
  async function _onPickTarget(target, btn) {
    btn.classList.add('is-busy');
    const player = (window.SoniqBoom && window.SoniqBoom.player) || null;
    const currentTrackId =
      (player && (player.currentTrackId || player.trackId || player.currentTrack?.id)) || null;
    if (!currentTrackId) {
      // UX-1 P1: don't take over the picker with the error panel — keep
      // the device list visible so the user can recover with one click.
      _showInlineBanner(
        "Start playing a track first, then pick a device to send it to.",
        { actionLabel: 'Pick a track', onAction: () => {
          close();
          const q = document.querySelector('#sidebar-search') ||
                    document.querySelector('#sb-search') ||
                    document.querySelector('input[type="search"]');
          if (q) q.focus();
        } },
      );
      btn.classList.remove('is-busy');
      return;
    }
    // UX-1 P0: when switching from an already-active target, surface the
    // transition so the user knows the swap is in flight.  Nielsen H1
    // (visibility of system status).
    const isSwitch = _activeTargetId && _activeTargetId !== target.id;
    const originalLabel = btn.querySelector('.cast-item-state')?.textContent;
    if (isSwitch) {
      const stateEl = btn.querySelector('.cast-item-state');
      if (stateEl) stateEl.textContent = 'Switching…';
      // Best-effort disconnect of the prior target.  Don't block the new
      // /play call on its completion — most controllers handle the
      // reconnect gracefully.
      const priorId = _activeTargetId;
      _apiPost('/api/cast/disconnect', { target_id: priorId }).catch(() => {});
    }
    try {
      const res = await _apiPost('/api/cast/play', {
        target_id: target.id,
        track_id:  currentTrackId,
      });
      _activeTargetId   = target.id;
      _activeTargetName = target.name;
      _lastFailedTarget = null;
      _updateActiveUI();
      _markBtnAsCasting(true);
      _renderList();
      // Apply the codec pref this session (if user changed it pre-cast).
      if (_pref && _pref !== 'auto') {
        _apiPost('/api/cast/preference', {
          target_id: target.id, pref: _pref,
        }).catch(() => {});
      }
      // Update active-banner with codec info so the codec disclosure
      // persists past a single toast dismissal — UX-3 P2.
      if (res.delivered_codec) {
        const meta = res.transcode
          ? `transcoded to ${res.delivered_codec.toUpperCase()}`
          : `native ${res.delivered_codec.toUpperCase()}`;
        const active = document.getElementById('cast-active');
        let codecLine = active && active.querySelector('.cast-active-codec');
        if (active && !codecLine) {
          codecLine = document.createElement('div');
          codecLine.className = 'cast-active-codec';
          const info = active.querySelector('.cast-active-info');
          if (info) info.appendChild(codecLine);
        }
        if (codecLine) codecLine.textContent = meta;
      }
      if (window.Toast && res.delivered_codec) {
        const tag = res.transcode
          ? `transcoded to ${res.delivered_codec.toUpperCase()}`
          : `native ${res.delivered_codec.toUpperCase()}`;
        window.Toast.info(
          isSwitch
            ? `Switched to ${target.name} — ${tag}`
            : `Casting to ${target.name} — ${tag}`,
        );
      }
    } catch (err) {
      // AirPlay 412 — the device wants a PIN.  Pop the pairing modal
      // instead of the generic error banner so the user can finish the
      // handshake without leaving the picker.  On success we retry
      // /play once; on cancel we leave the picker in its pre-click
      // state.
      if (err.status === 412 && err.detail && err.detail.requires_pairing) {
        const stateEl = btn.querySelector('.cast-item-state');
        if (stateEl) stateEl.textContent = 'Pair to continue…';
        const paired = await _runPairFlow(target);
        if (stateEl) stateEl.textContent = originalLabel || '';
        if (paired) {
          // Retry the cast one time after successful pairing.  If the
          // retry also fails the user lands in the normal error banner
          // path below (we'd need a second pair, which would also fail
          // immediately — not worth looping).
          try {
            const res2 = await _apiPost('/api/cast/play', {
              target_id: target.id,
              track_id:  currentTrackId,
            });
            _activeTargetId   = target.id;
            _activeTargetName = target.name;
            _lastFailedTarget = null;
            _updateActiveUI();
            _markBtnAsCasting(true);
            _renderList();
            if (window.Toast) {
              window.Toast.info(`Paired with ${target.name} — casting now.`);
            }
            btn.classList.remove('is-busy');
            return;
          } catch (err2) {
            err = err2;  // fall through to the error banner below
          }
        } else {
          // User cancelled.  No banner — they consciously aborted.
          btn.classList.remove('is-busy');
          return;
        }
      }
      // Remember the failed target so the retry button can target it.
      _lastFailedTarget = target;
      // Restore the prior list-item label.
      const stateEl = btn.querySelector('.cast-item-state');
      if (stateEl) stateEl.textContent = originalLabel || '';
      _showError(
        "Couldn't start cast.",
        err.message || "The device may have left the network.",
      );
    } finally {
      btn.classList.remove('is-busy');
    }
  }

  // Inline banner (non-takeover) used for soft errors like
  // "Nothing to cast" — see _onPickTarget.  UX-1 P1.
  let _inlineBanner = null;
  function _showInlineBanner(msg, opts) {
    _ensureRefs();
    _clearInlineBanner();
    const banner = document.createElement('div');
    banner.className = 'cast-inline-banner';
    banner.setAttribute('role', 'status');
    const txt = document.createElement('span');
    txt.textContent = msg;
    banner.appendChild(txt);
    if (opts && opts.actionLabel && opts.onAction) {
      const a = document.createElement('button');
      a.type = 'button';
      a.className = 'cast-inline-banner-btn';
      a.textContent = opts.actionLabel;
      a.addEventListener('click', opts.onAction);
      banner.appendChild(a);
    }
    _list.parentNode.insertBefore(banner, _list);
    _inlineBanner = banner;
    setTimeout(_clearInlineBanner, 8000);
  }
  function _clearInlineBanner() {
    if (_inlineBanner) {
      _inlineBanner.remove();
      _inlineBanner = null;
    }
  }

  // ── AirPlay PIN pairing modal ────────────────────────────────────────
  //
  // Triggered when /api/cast/play returns 412 with requires_pairing=true.
  // The receiver (Apple TV, iMac running AirPlay Receiver, HomePod, …)
  // shows a 4-digit PIN; we open a modal that:
  //   1. POSTs /api/cast/airplay/pair/begin — backend triggers the PIN
  //      display on the device
  //   2. The user types the 4 digits into our input
  //   3. POSTs /api/cast/airplay/pair/finish with the PIN — backend
  //      saves the resulting credentials to disk
  //   4. Resolves to ``true`` on success so the caller can retry /play.
  //
  // Cancel / Escape / backdrop click resolves ``false`` and aborts the
  // flow (we don't try to cancel pairing on the server because pyatv
  // tears down the handler on the next begin / disconnect anyway).
  //
  // Visual style intentionally mirrors the existing ``password-dialog``
  // pattern in index.html for consistency — borrowing its CSS classes
  // would couple us to a different DOM root; instead we build the modal
  // here with inline styles tied to the cast picker's CSS variables.
  async function _runPairFlow(target) {
    // Fire begin in parallel with showing the modal — the device PIN
    // takes ~200 ms to appear after the backend request lands, and we
    // don't want to add a sequential "open modal → request → render
    // input" pause on top of that.
    let beginPromise = _apiPost('/api/cast/airplay/pair/begin', {
      target_id: target.id,
    }).catch(err => {
      // Begin failed before the user got to type anything.  Resolve to
      // null so the modal can render an inline error.
      return { _error: err };
    });

    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'cast-pair-overlay';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.setAttribute('aria-labelledby', 'cast-pair-title');
      overlay.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,.55);' +
        'display:flex;align-items:center;justify-content:center;' +
        'z-index:10000;backdrop-filter:blur(4px)';
      overlay.innerHTML = `
        <div class="cast-pair-card" style="
          background:var(--bg2,#1a1c20);color:var(--text1,#e8eaed);
          border:1px solid var(--border-bright,#2a2d33);
          border-radius:14px;padding:22px 24px;min-width:340px;max-width:420px;
          box-shadow:0 10px 40px rgba(0,0,0,.5)">
          <h3 id="cast-pair-title" style="margin:0 0 8px;font-size:16px;font-weight:600">
            Pair with ${_escapeHtml(target.name || 'AirPlay device')}
          </h3>
          <p style="margin:0 0 16px;font-size:13px;line-height:1.5;color:var(--text2,#b5bccb)">
            A 4-digit code should appear on the device.<br>
            Enter it below to finish setup.
          </p>
          <input type="text" inputmode="numeric" pattern="[0-9]*"
                 maxlength="8" autocomplete="off" autocapitalize="off"
                 id="cast-pair-input"
                 placeholder="0000"
                 style="
                   width:100%;font-size:24px;letter-spacing:.4em;
                   text-align:center;padding:12px 8px;
                   background:var(--bg3,#0e1014);
                   color:var(--text1,#fff);
                   border:1px solid var(--border-bright,#2a2d33);
                   border-radius:8px;outline:none;
                   font-feature-settings:'tnum'">
          <div id="cast-pair-error" style="
            margin-top:10px;font-size:12.5px;color:#e57373;min-height:18px"></div>
          <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
            <button type="button" id="cast-pair-cancel" class="btn-secondary"
                    style="padding:8px 14px;border-radius:7px">Cancel</button>
            <button type="button" id="cast-pair-ok"
                    style="padding:8px 16px;border-radius:7px;
                    background:var(--accent,#f0722a);color:#000;border:0;
                    font-weight:600">Pair</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);

      const input  = overlay.querySelector('#cast-pair-input');
      const okBtn  = overlay.querySelector('#cast-pair-ok');
      const cxlBtn = overlay.querySelector('#cast-pair-cancel');
      const errEl  = overlay.querySelector('#cast-pair-error');

      // Disable submit until the begin call lands (device hasn't shown
      // the PIN yet) and the user has typed something.
      okBtn.disabled = true;
      input.disabled = true;
      input.focus();

      const setError = (msg) => { errEl.textContent = msg || ''; };

      beginPromise.then((result) => {
        if (result && result._error) {
          setError(result._error.message || 'Couldn\'t start pairing on the device.');
          okBtn.disabled = true;
          input.disabled = true;
          // Caller retries via Cancel → false, then can pick again.
          return;
        }
        input.disabled = false;
        input.focus();
        // Enable OK only once the user has typed digits (pyatv accepts
        // 4-digit PINs by default; allow longer for any device that
        // supplies its own digits).
        const refresh = () => {
          okBtn.disabled = !/^\d{4,8}$/.test(input.value.trim());
        };
        input.addEventListener('input', refresh);
        refresh();
      });

      const close = (result) => {
        document.removeEventListener('keydown', onKey);
        try { overlay.remove(); } catch (_) {}
        resolve(result);
      };

      const onKey = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); close(false); }
        else if (e.key === 'Enter' && !okBtn.disabled) {
          e.preventDefault(); doSubmit();
        }
      };
      document.addEventListener('keydown', onKey);

      cxlBtn.addEventListener('click', () => close(false));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });

      const doSubmit = async () => {
        const pin = input.value.trim();
        if (!/^\d{4,8}$/.test(pin)) {
          setError('PIN must be 4 digits.');
          input.focus();
          return;
        }
        okBtn.disabled = true;
        input.disabled = true;
        setError('');
        try {
          await _apiPost('/api/cast/airplay/pair/finish', {
            target_id: target.id,
            pin,
          });
          close(true);
        } catch (err) {
          // Wrong PIN / expired / 5xx — re-enable so the user can try
          // again (within the device's PIN-display window; if it
          // expired the backend will throw on the next attempt and the
          // user knows to start over).
          setError(err.message || 'Pairing failed.  Try again.');
          input.disabled = false;
          okBtn.disabled = false;
          input.focus();
          input.select();
        }
      };
      okBtn.addEventListener('click', doSubmit);
    });
  }

  // Local HTML-escape for the pair modal — keep us decoupled from
  // wherever ``_escapeAttr`` lives elsewhere in this module so the
  // helper is self-contained.
  function _escapeHtml(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  async function _onStop() {
    if (!_activeTargetId) return;
    try {
      await _apiPost('/api/cast/control', {
        target_id: _activeTargetId, action: 'stop',
      });
      await _apiPost('/api/cast/disconnect', {
        target_id: _activeTargetId,
      });
    } catch (err) {
      // Disconnect failures are non-fatal — the renderer might be off-LAN
      // by now.  Update UI either way.
    }
    _activeTargetId = null;
    _activeTargetName = null;
    _updateActiveUI();
    _markBtnAsCasting(false);
    _renderList();
    if (window.Toast) Toast.info('Stopped casting.');
  }

  function _updateActiveUI() {
    _ensureRefs();
    if (_activeTargetId) {
      _activeEl.classList.remove('hidden');
      _activeName.textContent = _activeTargetName || _activeTargetId;
    } else {
      _activeEl.classList.add('hidden');
    }
  }

  function _markBtnAsCasting(on) {
    if (!_btn) return;
    const pill = document.getElementById('cast-active-pill');
    const pillText = pill && pill.querySelector('.cast-active-pill-text');
    if (on) {
      _btn.classList.add('is-casting');
      _btn.title = `Casting to ${_activeTargetName || 'device'} — click to manage`;
      // UX-3 P1: persistent pill outside the picker so the user sees
      // active-cast state at a 1 m glance with the picker closed.
      if (pill) pill.classList.remove('hidden');
      if (pillText) pillText.textContent = _activeTargetName || 'device';
    } else {
      _btn.classList.remove('is-casting');
      _btn.title = 'Cast to device';
      if (pill) pill.classList.add('hidden');
      if (pillText) pillText.textContent = '—';
    }
  }

  async function _checkActiveSession() {
    // Backend tracks per-target sessions — sync UI to whatever it
    // believes is active so a page reload doesn't lose the indicator.
    try {
      const data = await _apiGet('/api/cast/sessions');
      const sess = (data && data.sessions) || [];
      if (sess.length === 0) {
        _activeTargetId = null;
        _activeTargetName = null;
        _markBtnAsCasting(false);
      } else {
        const s = sess[0];
        _activeTargetId   = s.target && s.target.id;
        _activeTargetName = s.target && s.target.name;
        _markBtnAsCasting(true);
      }
      _updateActiveUI();
    } catch { /* silent — picker still works without server state */ }
  }

  // ── Event wiring ─────────────────────────────────────────────────────
  function _wire() {
    _ensureRefs();
    if (!_btn) return;  // not on this page

    _btn.addEventListener('click', () => _opened ? close() : open());
    _closeBtn.addEventListener('click', close);
    _refreshBtn.addEventListener('click', () => refresh(true));
    _stopBtn.addEventListener('click', _onStop);
    // Empty-state "Search again" button (UX-1 P2 — empty state needs
    // an explicit retry affordance instead of relying on the gear icon
    // in the header).
    const emptyRetry = document.getElementById('cast-empty-retry');
    if (emptyRetry) emptyRetry.addEventListener('click', () => refresh(true));

    // Wire the persistent pill (UX-3 P1) to open the picker so users
    // can re-enter the management UI without targeting the small icon.
    const pill = document.getElementById('cast-active-pill');
    if (pill) {
      pill.addEventListener('click', () => _opened ? close() : open());
    }

    // Overlay click-outside-to-close (but NOT clicks inside the panel).
    _overlay.addEventListener('click', (e) => {
      if (e.target === _overlay) close();
    });

    // Keyboard handling for the picker.  Two responsibilities:
    //
    //   1. Trap focus inside the panel (Tab / Shift+Tab cycling).
    //   2. Arrow-key roving navigation across device list items.
    //
    // The capture-phase listener is critical for UX-3 P0: without it the
    // global ArrowUp / ArrowDown handler in app.js adjusts VOLUME while
    // the user is trying to navigate the cast device list.  We
    // ``stopPropagation()`` on the keys we handle so the global doesn't
    // race us.
    document.addEventListener('keydown', (e) => {
      if (!_opened) return;
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation(); close(); return;
      }
      // Arrow-key roving across .cast-list-item buttons.  UX-3 P0:
      // earlier the doc-comment promised arrow keys would move list
      // focus but no handler existed — the keys fell through to the
      // global volume control.  Now: arrow keys ONLY when focus is
      // inside the cast list (or the cast panel with no list focus
      // yet), so they don't fight Tab navigation for radios / details.
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const inList = _list && _list.contains(document.activeElement);
        const items = _list ? Array.from(_list.querySelectorAll('.cast-list-item')) : [];
        if (items.length === 0) return;
        if (!inList) {
          e.preventDefault(); e.stopPropagation();
          items[0].focus();
          return;
        }
        const idx = items.indexOf(document.activeElement);
        const next = e.key === 'ArrowDown'
          ? items[(idx + 1) % items.length]
          : items[(idx - 1 + items.length) % items.length];
        e.preventDefault(); e.stopPropagation();
        next.focus();
        return;
      }
      if (e.key === 'Tab') {
        // Trap focus inside the panel — extended selector covers
        // <summary>, [role="radio"], and the codec disclosure inputs
        // that the previous version skipped.  UX-1 P2 fix.
        const focusable = _panel.querySelectorAll(
          'button, [tabindex]:not([tabindex="-1"]), ' +
          'input:not([type="hidden"]), select, summary, [role="radio"]'
        );
        const visible = Array.from(focusable).filter(
          el => el.offsetParent !== null || el.tagName === 'INPUT'
        );
        if (visible.length === 0) return;
        const first = visible[0];
        const last  = visible[visible.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    }, true /* capture so we beat the global ArrowUp handler */);

    // Codec preference radios.
    for (const r of _prefRadios || []) {
      r.addEventListener('change', () => {
        if (r.checked) {
          _pref = r.value;
          if (_activeTargetId) {
            _apiPost('/api/cast/preference', {
              target_id: _activeTargetId, pref: _pref,
            }).catch(() => {});
          }
        }
      });
    }

    // Initial sync so a page reload picks up an in-flight cast session.
    _checkActiveSession();
  }

  // ── Boot ─────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _wire);
  } else {
    _wire();
  }

  // Public surface for tests / dev console
  window.SoniqBoomCastPicker = { open, close, refresh };
})();
