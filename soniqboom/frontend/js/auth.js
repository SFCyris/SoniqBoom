// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * auth.js — login / register overlay + signed-in user state.
 *
 * Boot order:
 *   1. ``Auth.boot()`` runs before the rest of the app.  It calls
 *      ``GET /api/auth/me`` to see if a session cookie is already valid.
 *   2. If yes, ``Auth.user`` is set and the rest of ``app.js`` proceeds
 *      normally.
 *   3. If no, the login overlay is shown.  When the user signs in (or
 *      creates an account), ``Auth.user`` is populated and the overlay
 *      closes — ``app.js`` is then allowed to finish booting.
 *
 * Session is stored in an HTTP-only cookie set by the server, so this
 * module never touches credentials directly after the login POST.
 */

import { Toast } from './utils.js';
import { passwordHint, passwordProblem } from './password-rule.js';

const STATE = {
  user: null,                    // { id, username, role, ... } | null
  authStatus: null,              // server-reported auth status
  _readyResolve: null,
  ready: null,                   // Promise that resolves once the user is known
};

STATE.ready = new Promise(res => { STATE._readyResolve = res; });

// ── DOM ─────────────────────────────────────────────────────────────────────

function _ensureOverlay() {
  let el = document.getElementById('auth-overlay');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'auth-overlay';
  el.className = 'auth-overlay hidden';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-labelledby', 'auth-title');
  el.innerHTML = `
    <div class="auth-card">
      <div class="auth-brand">
        <span class="auth-brand-icon" aria-hidden="true">🔊</span>
        <span class="auth-brand-name">SoniqBoom</span>
      </div>
      <h2 id="auth-title" class="auth-title">Sign in</h2>
      <form id="auth-form" class="auth-form" autocomplete="on">
        <label class="auth-field">
          <span>Username</span>
          <input id="auth-username" type="text" autocomplete="username"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 inputmode="text"
                 required minlength="2" maxlength="64"
                 pattern="[A-Za-z0-9._\\-]+">
        </label>
        <label class="auth-field auth-field-password">
          <span>Password <em id="auth-pw-rule" hidden></em></span>
          <input id="auth-password" type="password"
                 autocomplete="current-password"
                 required>
          <button type="button" id="auth-pw-toggle" class="auth-pw-toggle"
                  aria-label="Show password" title="Show password">👁</button>
        </label>
        <label class="auth-field auth-field-display" hidden>
          <span>Display name <em>(optional)</em></span>
          <input id="auth-display-name" type="text" maxlength="64"
                 autocomplete="nickname">
        </label>
        <button id="auth-submit" type="submit" class="auth-submit">Sign in</button>
        <div id="auth-error" class="auth-error" role="alert" aria-live="assertive" hidden></div>
      </form>
      <div class="auth-switch">
        <button type="button" id="auth-mode-register" class="auth-link" hidden>
          Create an account
        </button>
        <button type="button" id="auth-mode-login"    class="auth-link" hidden>
          ← Back to sign in
        </button>
      </div>
      <div class="auth-bootstrap-hint" id="auth-bootstrap-hint" hidden>
        <div id="auth-bootstrap-lead">No admin exists yet.  On the server, run:</div>
        <div style="margin:6px 0">
          <code id="auth-bootstrap-cmd">soniqboom-setadm -user alice -passwd 'changeme123'</code>
        </div>
        <div id="auth-bootstrap-datadir" style="margin:4px 0;color:var(--text2)"></div>
        <div>Then
          <button type="button" id="auth-bootstrap-retry" class="auth-link"
                  style="display:inline;padding:0">re-check</button>
          — no reload needed.
        </div>
        <div id="auth-bootstrap-retry-msg" style="margin-top:6px;color:#ffb0b0" hidden></div>
      </div>
    </div>`;
  document.body.appendChild(el);
  return el;
}

// ── Mode toggle (login vs register) ─────────────────────────────────────────

let _mode = 'login';

function _applyMode() {
  const overlay = document.getElementById('auth-overlay');
  if (!overlay) return;
  const title   = overlay.querySelector('#auth-title');
  const submit  = overlay.querySelector('#auth-submit');
  const pwd     = overlay.querySelector('#auth-password');
  const display = overlay.querySelector('.auth-field-display');
  const toReg   = overlay.querySelector('#auth-mode-register');
  const toLogin = overlay.querySelector('#auth-mode-login');
  const err     = overlay.querySelector('#auth-error');
  err.hidden = true;
  err.textContent = '';
  const canRegister = !!STATE.authStatus?.registration_open;
  // New accounts: show the password rule ("Require strong passwords").
  const rule = overlay.querySelector('#auth-pw-rule');
  const hint = passwordHint(!!STATE.authStatus?.strong_passwords);
  rule.hidden = _mode === 'login' || !hint;
  rule.textContent = hint ? `(${hint})` : '';
  if (_mode === 'login') {
    title.textContent  = 'Sign in';
    submit.textContent = 'Sign in';
    pwd.autocomplete   = 'current-password';
    display.hidden     = true;
    toReg.hidden   = !canRegister;
    toLogin.hidden = true;
  } else {
    title.textContent  = 'Create account';
    submit.textContent = 'Create account';
    pwd.autocomplete   = 'new-password';
    display.hidden     = false;
    toReg.hidden   = true;
    toLogin.hidden = false;
  }
}

// ── Submit handler ──────────────────────────────────────────────────────────

async function _handleSubmit(e) {
  e.preventDefault();
  const overlay = document.getElementById('auth-overlay');
  const submit  = overlay.querySelector('#auth-submit');
  const err     = overlay.querySelector('#auth-error');
  err.hidden = true;
  err.textContent = '';

  const username = overlay.querySelector('#auth-username').value.trim();
  const password = overlay.querySelector('#auth-password').value;
  const display  = overlay.querySelector('#auth-display-name').value.trim();
  if (_mode !== 'login') {
    const problem = passwordProblem(password, !!STATE.authStatus?.strong_passwords);
    if (problem) {
      err.textContent = problem;
      err.hidden = false;
      overlay.querySelector('#auth-password').focus();
      return;
    }
  }
  const endpoint = _mode === 'login' ? '/api/auth/login' : '/api/auth/register';
  const body     = _mode === 'login'
    ? { username, password }
    : { username, password, display_name: display || null };

  // Show the busy state — visible feedback that the request is in flight,
  // and prevents double-submits via Enter / impatient clicks.
  submit.classList.add('busy');
  submit.disabled = true;
  try {
    const res = await fetch(endpoint, {
      method:      'POST',
      credentials: 'same-origin',
      headers:     { 'Content-Type': 'application/json' },
      body:        JSON.stringify(body),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Request failed (${res.status})`);
    }
    const data = await res.json();
    STATE.user = data.user;
    // The password leaves the page with the sign-in: nothing keeps it in the
    // DOM (an extension, a later script, devtools) once it was sent.
    _clearPassword(overlay);
    _hide();
    STATE._readyResolve?.(data.user);
    STATE._readyResolve = null;
  } catch (ex) {
    err.textContent = String(ex.message || ex);
    err.hidden = false;
    // Re-focus the password input so the user can correct quickly.
    overlay.querySelector('#auth-password').focus();
    overlay.querySelector('#auth-password').select?.();
  } finally {
    submit.classList.remove('busy');
    submit.disabled = false;
  }
}

function _clearPassword(overlay) {
  const pw = overlay?.querySelector('#auth-password');
  if (!pw) return;
  pw.value = '';
  if (pw.type !== 'password') {
    pw.type = 'password';
    const t = overlay.querySelector('#auth-pw-toggle');
    if (t) {
      t.textContent = '👁';
      t.title = 'Show password';
      t.setAttribute('aria-label', t.title);
    }
  }
}

function _wireOverlayControls(el) {
  // Password visibility toggle — eye icon on the password field.
  const pwInput  = el.querySelector('#auth-password');
  const pwToggle = el.querySelector('#auth-pw-toggle');
  pwToggle?.addEventListener('click', () => {
    const showing = pwInput.type === 'text';
    pwInput.type = showing ? 'password' : 'text';
    pwToggle.textContent  = showing ? '👁' : '🙈';
    pwToggle.title        = showing ? 'Show password' : 'Hide password';
    pwToggle.setAttribute('aria-label', pwToggle.title);
  });

  // Bootstrap "re-check" — after the admin runs the CLI, this lets them
  // refresh status without a full page reload.  We call /auth/reload
  // which forces the server to re-read users.json from disk so the new
  // admin is visible to the running process.
  el.querySelector('#auth-bootstrap-retry')?.addEventListener('click', async () => {
    const msgEl = el.querySelector('#auth-bootstrap-retry-msg');
    msgEl.hidden = true;
    try {
      let r = await fetch('/api/auth/reload', {
        method: 'POST', credentials: 'same-origin',
      });
      // 403 = an admin exists now, or no account is loaded yet (fresh install,
      // recovery): then the server re-reads users.json only for the CLI —
      // which already told it about the new account — and picks a restored
      // users.json up by itself.  Just read the status.
      if (r.status === 403) {
        r = await fetch('/api/auth/status', { credentials: 'same-origin' });
      }
      if (!r.ok) throw new Error(`status ${r.status}`);
      STATE.authStatus = await r.json();
    } catch (e) {
      msgEl.textContent = "Couldn't reach the server — check it's running and try again.";
      msgEl.hidden = false;
      // Don't clobber the previous status — leave the overlay state as-is.
      return;
    }
    el.querySelector('#auth-submit').disabled = !STATE.authStatus?.has_any_user;
    el.querySelector('#auth-bootstrap-hint').hidden = !!STATE.authStatus?.has_any_admin;
    _refreshBootstrapHint();
    _applyMode();
    if (STATE.authStatus?.has_any_user) {
      el.querySelector('#auth-username').focus();
    }
  });
}

function _refreshBootstrapHint() {
  const el = document.getElementById('auth-overlay');
  if (!el) return;
  // Recovery: users.json is unreadable on an install that had accounts —
  // sign-in is locked until an admin restores it or creates an account.
  const lead = el.querySelector('#auth-bootstrap-lead');
  if (lead) {
    lead.textContent = STATE.authStatus?.recovery_required
      ? 'The user accounts file (users.json) could not be read, so sign-in is '
        + 'locked.  Restore users.json from a backup into the data dir, or '
        + 'create an admin on the server:'
      : 'No admin exists yet.  On the server, run:';
  }
  const dd = el.querySelector('#auth-bootstrap-datadir');
  if (dd && STATE.authStatus?.data_dir) {
    dd.textContent = `(Server data dir: ${STATE.authStatus.data_dir})`;
  }
}

// ── Show / hide ─────────────────────────────────────────────────────────────

function _show() {
  const el = _ensureOverlay();
  const firstTime = !el.dataset.wired;
  el.classList.remove('hidden');
  document.body.classList.add('auth-blocked');
  // Take the background app out of the keyboard tab order while the
  // overlay is up.  ``inert`` was widely supported by 2023 (Chrome 102+,
  // Safari 15.5+, Firefox 112+) and is the modern replacement for
  // manually setting tabindex=-1 on every focusable descendant.
  _inertBackground(el, true);
  if (firstTime) {
    el.querySelector('#auth-form').addEventListener('submit', _handleSubmit);
    el.querySelector('#auth-mode-register')
      .addEventListener('click', () => { _mode = 'register'; _applyMode(); });
    el.querySelector('#auth-mode-login')
      .addEventListener('click', () => { _mode = 'login';    _applyMode(); });
    _wireOverlayControls(el);
    _installFocusTrap(el);
    el.dataset.wired = '1';
  }

  // Bootstrap hint visible only when no admin exists yet.
  const hint = el.querySelector('#auth-bootstrap-hint');
  hint.hidden = !!STATE.authStatus?.has_any_admin;
  _refreshBootstrapHint();

  _applyMode();
  // No account to sign in to (a fresh install, or recovery — users.json
  // can't be read): "Sign in" stays off however the overlay came up (at
  // boot, or a session ending mid-use), until re-check finds an account.
  el.querySelector('#auth-submit').disabled = STATE.authStatus?.has_any_user === false;
  // Focus where the user can act: Username — or, while the server can't be
  // reached (Sign in off), the Retry button.
  setTimeout(() => {
    const retry = el.querySelector('#auth-retry-network');
    const target = retry && !retry.hidden ? retry : el.querySelector('#auth-username');
    try { target.focus(); } catch (_) { /* gone */ }
  }, 50);
}

// [auth-trap:begin] — tests/js/auth_overlay.test.mjs runs this region as is
// The controls Tab can reach in ``modal``: enabled, and shown — an element
// inside a ``hidden`` ancestor (the register link, the setup hint's re-check)
// or with no box is skipped.
const _TRAP_SEL = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), '
  + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
function _trapFocusables(modal) {
  return [...modal.querySelectorAll(_TRAP_SEL)].filter((el) =>
    !el.closest('[hidden], [inert]') && !el.disabled
    && (typeof el.getClientRects !== 'function' || el.getClientRects().length > 0));
}
// Tab / Shift+Tab: the next / previous control, wrapping at both ends; focus
// that is outside the list (on the card, or lost) goes to the first / last.
function _trapStep(list, active, back) {
  if (!list.length) return null;
  const i = list.indexOf(active);
  if (i < 0) return back ? list[list.length - 1] : list[0];
  return list[(i + (back ? list.length - 1 : 1)) % list.length];
}
// [auth-trap:end]

function _installFocusTrap(modal) {
  // Tab/Shift+Tab cycle focus inside ``modal`` — every press is handled here,
  // so focus never leaves for the page behind (also when the overlay's
  // controls change: Sign in disabled, Retry added, the register link shown).
  modal.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const next = _trapStep(_trapFocusables(modal), document.activeElement, e.shiftKey);
    e.preventDefault();
    if (next) next.focus();
  });
}

function _hide() {
  const el = document.getElementById('auth-overlay');
  if (el) el.classList.add('hidden');
  document.body.classList.remove('auth-blocked');
  _stopAutoRetry();
  _signedInOnce = true;
  // (the next time the card shows for a sign-in, it shows no stale Retry)
  const retry = el && el.querySelector('#auth-retry-network');
  if (retry) retry.hidden = true;
  _inertBackground(el, false);
}

// The page behind the overlay is out of reach (``inert``: no Tab, no click,
// not read out) — on the desktop page and the phone's page alike: every element
// of <body> except the overlay itself and the toasts (a live region).
function _inertBackground(overlay, on) {
  for (const n of document.body.children) {
    if (n === overlay || n.matches('script, audio, .sb-toast-host')) continue;
    if (on) {
      if (n.hasAttribute('inert')) continue;           // (inert for another reason: left alone)
      n.setAttribute('inert', '');
      n.setAttribute('aria-hidden', 'true');
      n.dataset.authInert = '1';
    } else if (n.dataset.authInert) {
      n.removeAttribute('inert');
      n.removeAttribute('aria-hidden');
      delete n.dataset.authInert;
    }
  }
}

// The server can't be reached, or answers 503 (``restarting``: the source
// install's restart refuses requests until the new process is up): the overlay
// says so and offers Retry — never the setup hint (nothing is known about the
// accounts) and never an enabled Sign in.
// [auth-unavailable:begin] — tests/js/auth_overlay.test.mjs runs this region as is
// What the card says while the server can't be reached.  (No "click": the page
// tries again by itself, and on a phone nothing is clicked.)
function _unavailableText(restarting) {
  return restarting
    ? 'SoniqBoom is restarting. This page reconnects by itself as soon as it is back.'
    : 'Can’t reach the SoniqBoom server. Check your connection — this page keeps trying to reconnect.';
}
// The pause before try ``n`` (0, 1, …) to reach the server again: every 2 s
// for the first five tries (a restart takes a few seconds), then longer, at
// most 15 s.
function _retryDelayMs(n) { return Math.min(15000, Math.round(2000 * Math.pow(1.5, Math.max(0, n - 4)))); }
// [auth-unavailable:end]

// While the card shows, the server is asked again on its own (_retryDelayMs);
// Retry asks at once.  Back up: a page that never got past start-up (no
// sign-in on it yet) loads again; a session that ended mid-use
// (_promptReauth) gets the sign-in form back — or, when its sign-in is still
// valid, simply carries on.
let _retryTimer = null, _retryTry = 0, _retrying = false, _signedInOnce = false;
function _stopAutoRetry() { clearTimeout(_retryTimer); _retryTimer = null; _retryTry = 0; }
function _scheduleAutoRetry() {
  clearTimeout(_retryTimer);
  _retryTimer = setTimeout(_retryNow, _retryDelayMs(_retryTry++));
}
async function _retryNow() {
  if (_retrying) return;
  _retrying = true;
  clearTimeout(_retryTimer); _retryTimer = null;
  let me = null, st = null;
  try {
    [me, st] = await Promise.all([
      fetch('/api/auth/me',     { credentials: 'same-origin' }),     // (/api answers are no-store)
      fetch('/api/auth/status', { credentials: 'same-origin' }),
    ]);
  } catch { me = st = null; }
  _retrying = false;
  const el = document.getElementById('auth-overlay');
  if (!el || el.classList.contains('hidden')) return;          // signed in meanwhile
  const down = !me || !st || [502, 503, 504].includes(me.status) || [502, 503, 504].includes(st.status);
  if (down) {
    const restarting = !!((me && me.status === 503) || (st && st.status === 503));
    const err = el.querySelector('#auth-error');
    const text = _unavailableText(restarting);
    if (err.textContent !== text) err.textContent = text;      // (announced only when it changes)
    _scheduleAutoRetry();
    return;
  }
  _stopAutoRetry();
  if (!_signedInOnce) { location.reload(); return; }
  STATE.authStatus = await st.json().catch(() => ({}));
  if (me.ok) {
    const user = (await me.json().catch(() => ({}))).user;
    if (user) {
      STATE.user = user;
      _hide();
      STATE._readyResolve?.(user);
      STATE._readyResolve = null;
      return;
    }
  }
  // The server is back and wants a sign-in: the ordinary form.
  const retryBtn = el.querySelector('#auth-retry-network');
  if (retryBtn) retryBtn.hidden = true;
  _show();
}

function _showUnavailable(restarting) {
  _show();
  const el = document.getElementById('auth-overlay');
  const submit = el.querySelector('#auth-submit');
  const err = el.querySelector('#auth-error');
  submit.disabled = true;
  el.querySelector('#auth-bootstrap-hint').hidden = true;
  err.textContent = _unavailableText(restarting);
  err.hidden = false;
  // Replace the submit affordance with an inline "Retry connection"
  // button.  We re-append rather than recreate the element so the
  // existing styles + focus state survive.
  let retryBtn = el.querySelector('#auth-retry-network');
  if (!retryBtn) {
    retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.id = 'auth-retry-network';
    retryBtn.className = 'auth-submit';
    retryBtn.textContent = 'Retry connection';
    retryBtn.style.marginTop = '8px';
    retryBtn.addEventListener('click', () => _retryNow());
    submit.insertAdjacentElement('afterend', retryBtn);
  }
  retryBtn.hidden = false;
  _retryTry = 0;
  _scheduleAutoRetry();
}

// ── Boot ────────────────────────────────────────────────────────────────────

/**
 * Resolve the auth state at startup.
 *
 * Boot timing contract:
 *   * On a fresh hit, callers ``await Auth.boot()`` *before* registering
 *     any Player/Library listeners that read ``Auth.user`` — see app.js
 *     top-of-file, which awaits boot() and then ``Auth.ready`` before
 *     wiring any Player.on(...) handlers.  Otherwise the first
 *     ``trackchange`` could fire before ``STATE.user`` is populated and
 *     downstream modules (Library, TrackInfo, Admin) would see ``null``.
 *   * ``boot()`` resolves the ``Auth.ready`` promise on success (valid
 *     session cookie) **before returning** — so any code that awaits
 *     ``Auth.ready`` after boot resolves is guaranteed to see a non-null
 *     ``STATE.user``.
 *   * On failure (no session, network error), the promise stays pending
 *     until the user signs in via the overlay; sign-in resolves
 *     ``Auth.ready`` via the submit handler.
 */
async function boot() {
  let networkOk = true;
  let restarting = false;
  try {
    const [me, st] = await Promise.all([
      fetch('/api/auth/me',     { credentials: 'same-origin' }),
      fetch('/api/auth/status', { credentials: 'same-origin' }),
    ]);
    // 503: the server is restarting (it answers nothing else meanwhile) —
    // the same affordance as an unreachable server, not the setup hint.
    if (me.status === 503 || st.status === 503) { restarting = true; throw new Error('restarting'); }
    // (a reverse proxy's 502 / 504 while the server is down or starting)
    if ([502, 504].includes(me.status) || [502, 504].includes(st.status)) throw new Error('unreachable');
    STATE.authStatus = await st.json().catch(() => ({}));
    if (me.ok) {
      STATE.user = (await me.json()).user;
      _signedInOnce = true;
      STATE._readyResolve?.(STATE.user);
      STATE._readyResolve = null;
      return STATE.user;
    }
  } catch {
    // Both probes failed — server is unreachable.  Show the overlay
    // with an explicit "can't reach server" affordance so the user
    // knows to check the connection / retry, instead of staring at a
    // login form that won't resolve.
    networkOk = false;
  }
  if (!networkOk) {
    _showUnavailable(restarting);
    return null;
  }

  // No valid session.  If the server reports there's not a single user
  // yet (fresh install, no admin bootstrap), don't trap the user inside
  // a login overlay with no way out — show the bootstrap hint with a
  // "re-check" affordance so they can refresh status after running the
  // CLI without a full page reload.
  if (STATE.authStatus && STATE.authStatus.has_any_user === false) {
    _show();
    const el = document.getElementById('auth-overlay');
    el.querySelector('#auth-submit').disabled = true;
    el.querySelector('#auth-bootstrap-hint').hidden = false;
    return null;
  }
  _show();
  // Make sure submit is enabled in the regular re-auth case (e.g. after
  // a successful bootstrap → fresh login attempt).
  const el = document.getElementById('auth-overlay');
  el.querySelector('#auth-submit').disabled = false;
  return null;
}

// ── Re-auth on session expiry ───────────────────────────────────────────────
// Wrap the global ``fetch`` so any 401 from a same-origin /api call
// re-shows the login overlay.  The user keeps their app state — the
// overlay re-resolves ``Auth.ready`` once they sign back in and the
// triggering caller can retry.  Auth endpoints themselves (login,
// register, status, me) are skipped — they're allowed to 401 as part
// of their own flow.

let _reauthInFlight = null;

function _isAuthRoute(u) {
  // Routes that handle their own 401s — must NOT trigger the global
  // re-login overlay (would create double-modal / confusing UX).
  //   * login/register/status/me/reload — overlay's own probes
  //   * logout — by definition a 401 is "already signed out", no need to re-prompt
  //   * change-password — has its own inline error path; bad current
  //     password returns 401 but we want to show "wrong current password",
  //     not boot the user back to the login screen.
  return /\/api\/auth\/(login|register|status|me|reload|logout|change-password)\b/.test(u);
}

function _promptReauth() {
  // Synchronous in-flight guard — set the promise *before* awaiting
  // anything so concurrent 401s that race past the first call see a
  // non-null _reauthInFlight on their first check and bail out.
  if (_reauthInFlight) return _reauthInFlight;
  _reauthInFlight = (async () => {
    STATE.user = null;
    STATE.ready = new Promise(res => { STATE._readyResolve = res; });
    // Capture the focused element so we can restore focus after sign-in.
    const focusedBefore = document.activeElement;
    // Refresh server-reported status (registration_open may have changed
    // since the user signed in, e.g. an admin closed signups).
    let restarting = false;
    try {
      const st = await fetch('/api/auth/status', { credentials: 'same-origin' });
      if (st.status === 503) restarting = true;
      else STATE.authStatus = await st.json().catch(() => ({}));
    } catch { /* network */ }
    return await new Promise(resolve => {
      if (restarting) _showUnavailable(true);
      else _show();
      // Hijack _readyResolve so the *original* resolver (from boot) doesn't
      // double-fire — only the reauth caller awaits this promise.
      const original = STATE._readyResolve;
      STATE._readyResolve = (user) => {
        original?.(user);
        _reauthInFlight = null;
        // Restore focus to whatever the user was interacting with before
        // the session expired, so they pick up exactly where they left off.
        if (focusedBefore && typeof focusedBefore.focus === 'function'
            && document.contains(focusedBefore)) {
          try { focusedBefore.focus(); } catch { /* ignore */ }
        }
        resolve(user);
      };
    });
  })();
  return _reauthInFlight;
}

const _origFetch = window.fetch.bind(window);
window.fetch = async function _interceptedFetch(input, init) {
  const url = typeof input === 'string' ? input : (input?.url || '');
  const res = await _origFetch(input, init);
  if (res.status === 401 && url.startsWith('/api') && !_isAuthRoute(url)) {
    // Session expired mid-use.  Show the login overlay and, once the
    // user signs in, *replay* the original request so the caller sees a
    // fresh response instead of the stale 401.  This means clicking a
    // track and getting hit with re-auth no longer requires a second
    // click — the play continues automatically after sign-in.
    await _promptReauth();
    try {
      return await _origFetch(input, init);
    } catch (e) {
      // Replay failed (network / aborted) — fall back to the original 401
      // so callers can surface the failure themselves.
      return res;
    }
  }
  return res;
};

async function logout() {
  // While the server restarts it answers 503 (Retry-After): the sign-out is
  // sent again until it lands, so the session never outlives the restart.
  // Given up (still restarting / unreachable after ~30 s): the page stays
  // signed in and says so, rather than reloading into a session that is
  // still valid.
  const deadline = Date.now() + 30000;
  for (;;) {
    let res = null;
    try {
      res = await fetch('/api/auth/logout', {
        method:      'POST',
        credentials: 'same-origin',
      });
    } catch { /* network — retried below */ }
    if (res && res.status !== 503) break;
    if (Date.now() >= deadline) {
      Toast.error("Couldn't sign out — the server isn't answering. Try again in a moment.");
      return;
    }
    const after = Number(res?.headers.get('Retry-After')) || 2;
    await new Promise(r => setTimeout(r, Math.min(Math.max(after, 1), 5) * 1000));
  }
  STATE.user = null;
  location.reload();
}

async function changePassword(currentPassword, newPassword) {
  const res = await fetch('/api/auth/change-password', {
    method:      'POST',
    credentials: 'same-origin',
    headers:     { 'Content-Type': 'application/json' },
    body:        JSON.stringify({
      current_password: currentPassword,
      new_password:     newPassword,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  Toast.ok('Password updated.');
}

export const Auth = {
  boot,
  logout,
  changePassword,
  get user()       { return STATE.user; },
  get isAdmin()    { return STATE.user?.role === 'admin'; },
  get canEdit()    { return STATE.user?.role === 'admin' || STATE.user?.role === 'edit'; },
  get ready()      { return STATE.ready; },
};

// Convenience: a single ``window.__sbAuth`` handle for modules that don't
// import directly (kept tiny — full API is the ES module).
window.__sbAuth = Auth;
