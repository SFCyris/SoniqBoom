// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * utils.js — Shared utility functions used across multiple modules.
 * No imports from other SoniqBoom modules (prevents circular deps).
 */

// ── Toast / banner ─────────────────────────────────────────────────────────
// Previously player/search/playlist/admin pushed network failures to
// console.error only — the user saw nothing.  This minimal toast surface
// gives every module a single line of error feedback.
let _toastHost = null;
function _ensureToastHost() {
  if (_toastHost) return _toastHost;
  _toastHost = document.createElement('div');
  _toastHost.setAttribute('role', 'status');
  _toastHost.setAttribute('aria-live', 'polite');
  _toastHost.style.cssText = (
    'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);'
    + 'display:flex;flex-direction:column;gap:8px;align-items:center;'
    // z-index 100000 puts Toast above the playlist/restart modals
    // (10000) and admin overlay (8000) — Visual-Test #1 caught the
    // inversion where Toast.error during a playlist-rename was invisible
    // because the modal backdrop sat on top.
    + 'z-index:100000;pointer-events:none;'
  );
  document.body.appendChild(_toastHost);
  return _toastHost;
}

// Coalesce identical toasts that arrive within a 2s window: a backend
// hiccup hitting 5 concurrent users used to produce 5 stacked toasts;
// we now show one toast with a ``×N`` badge that bumps in place
// (UX-under-load #7).  Cap concurrent visible toasts at 3 — beyond
// that, queue and ratchet through (FIFO) so the screen doesn't fill
// with overlapping rectangles on mobile.
const _COALESCE_WINDOW_MS = 2000;
const _MAX_VISIBLE_TOASTS = 3;
const _recent = new Map();   // "kind\0msg" → { el, count, expiresAt }

function _toastKey(kind, msg) { return `${kind}\0${msg}`; }

function _emitToast(kind, msg) {
  if (!msg) return;
  const host = _ensureToastHost();
  const key = _toastKey(kind, msg);
  const now = performance.now();
  const recent = _recent.get(key);
  if (recent && recent.expiresAt > now && document.body.contains(recent.el)) {
    // Same toast text within the coalesce window — bump the count badge
    // and reset the dismiss timer rather than spawn a new node.
    recent.count++;
    recent.expiresAt = now + _COALESCE_WINDOW_MS;
    let badge = recent.el.querySelector('.toast-badge');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'toast-badge';
      badge.style.cssText = (
        'margin-left:10px;background:rgba(0,0,0,0.35);'
        + 'padding:1px 7px;border-radius:999px;font-size:11px;'
      );
      recent.el.appendChild(badge);
    }
    badge.textContent = `×${recent.count}`;
    clearTimeout(recent.dismissTimer);
    recent.dismissTimer = setTimeout(() => _dismissToast(recent.el), kind === 'error' ? 5000 : 3500);
    return;
  }
  // Cap concurrent toasts: if we already show N, push the oldest out
  // first so the new one is visible.
  while (host.childElementCount >= _MAX_VISIBLE_TOASTS) {
    _dismissToast(host.firstElementChild);
  }
  const el = document.createElement('div');
  el.textContent = msg;
  const bg = kind === 'error' ? '#7a1f1f' : kind === 'warn' ? '#6b5310' : '#1f3f7a';
  el.style.cssText = (
    `background:${bg};color:#fff;padding:10px 16px;border-radius:6px;`
    + 'box-shadow:0 4px 16px rgba(0,0,0,0.35);font:13px/1.4 system-ui,sans-serif;'
    + 'max-width:520px;pointer-events:auto;opacity:0;transition:opacity 180ms;'
    + 'display:flex;align-items:center;gap:6px;'
  );
  // ARIA: errors are assertive (announced immediately); info/warn polite.
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
  host.appendChild(el);
  requestAnimationFrame(() => { el.style.opacity = '1'; });
  const dismissTimer = setTimeout(() => _dismissToast(el), kind === 'error' ? 5000 : 3500);
  _recent.set(key, {
    el,
    count: 1,
    expiresAt: now + _COALESCE_WINDOW_MS,
    dismissTimer,
  });
}

function _dismissToast(el) {
  if (!el) return;
  el.style.opacity = '0';
  setTimeout(() => el.remove(), 220);
  // Purge from the coalesce map.
  for (const [k, rec] of _recent) {
    if (rec.el === el) { _recent.delete(k); break; }
  }
}

export const Toast = {
  info:  (msg) => _emitToast('info',  msg),
  warn:  (msg) => _emitToast('warn',  msg),
  error: (msg) => _emitToast('error', msg),
};


/**
 * Tracker format names as reported by mutagen / openmpt123.
 * Used both for VU-meter detection and for placeholder emoji selection.
 */
export const TRACKER_FORMAT_NAMES = new Set([
  'ProTracker', 'ScreamTracker 3', 'FastTracker 2', 'Impulse Tracker',
  'MultiTracker', 'OctaMED', 'Composer 669', 'DigiBooster Pro',
  'AHX', 'HivelyTracker', 'UltraTracker', 'ScreamTracker 2',
  'Farandole', 'ASYLUM/DMP', 'General DigiMusic', 'Imago Orpheus',
  'Oktalyzer', 'SoundFX', 'Grave Composer', 'DSIK',
]);

/**
 * Chip / FM formats rendered server-side (libgme console chiptunes + AdPlug
 * AdLib/OPL).  No per-voice VUMR sidecar exists for them, so they fall back to
 * the FFT spectrum — but they still must pass the VU gate to show it at all.
 * Names mirror metadata.py FORMAT_NAMES.
 */
export const CHIP_FORMAT_NAMES = new Set([
  // libgme console chiptunes
  'NSF', 'NSFe', 'SPC', 'GBS', 'VGM', 'VGZ', 'AY', 'KSS', 'SAP', 'GYM', 'HES',
  // AdPlug AdLib / OPL2 FM
  'AdLib IMF', 'AdLib ROL', 'Creative Music', 'EdLib', 'Reality AdLib',
  'LucasArts AdLib', 'Sierra AdLib', 'DOSBox OPL', 'HSC AdLib', 'RIX OPL',
  'AdLib Tracker 2', 'AdLib', "Bob's AdLib", "Ken's AdLib",
]);

/** File extension fallback list (lower-case, no dot). */
const TRACKER_EXTS = new Set([
  'mod', 's3m', 'xm', 'it', 'mtm', 'med', 'oct', '669',
  'dbm', 'ahx', 'hvl', 'ult', 'stm', 'far', 'amf',
  'gdm', 'imf', 'okt', 'sfx', 'wow', 'dsm',
]);

/**
 * Return the format-appropriate placeholder emoji for a track with no art.
 *
 *   🕹️  SID / PSID         (C64 chiptune)
 *   🎼  MIDI               (MIDI music)
 *   💾  Tracker / module   (MOD, S3M, XM, IT …)
 *   🎵  FLAC               (lossless, compressed)
 *   🎧  ALAC               (Apple Lossless)
 *   💿  WAV / AIFF         (uncompressed PCM)
 *   📀  DSD / DSF / DFF    (high-resolution 1-bit audio — SACD-derived)
 *   🔊  Everything else    (MP3, OGG, Opus, AAC …)
 */
export function artPlaceholderEmoji(track) {
  const fmt   = track?.format || '';     // raw mutagen format name — see metadata.py FORMAT_NAMES
  const fmtUp = fmt.toUpperCase();

  // SID / PSID
  if (fmtUp === 'SID' || fmtUp === 'PSID') return '\u{1F579}\uFE0F'; // 🕹️

  // MIDI
  if (fmtUp === 'MID' || fmtUp === 'MIDI') return '\u{1F3BC}';       // 🎼

  // Tracker — primary check via mutagen format name (exact, case-sensitive)
  if (TRACKER_FORMAT_NAMES.has(fmt)) return '\u{1F4BE}';              // 💾

  // Tracker — fallback: check file extension
  const ext = ((track?.path || '').split('.').pop() || '').toLowerCase();
  if (TRACKER_EXTS.has(ext)) return '\u{1F4BE}';                      // 💾

  // FLAC — lossless compressed
  if (fmtUp === 'FLAC') return '\u{1F3B5}';                           // 🎵

  // ALAC — Apple Lossless (stored as "ALAC" by metadata.py)
  if (fmtUp === 'ALAC') return '\u{1F3A7}';                           // 🎧

  // WAV / AIFF — uncompressed PCM
  if (fmtUp === 'WAV' || fmtUp === 'WAVE' || fmtUp === 'AIFF') return '\u{1F4BF}'; // 💿

  // DSD / DSF / DFF — high-resolution 1-bit audio (SACD-derived).  The
  // DVD-disc glyph (📀) pairs visually with 💿 used for WAV/AIFF so the
  // two physical-medium PCM/DSD formats land near each other, and reads
  // as "some kind of disc" even to users who don't know what DSD is.
  if (fmtUp === 'DSD' || fmtUp.startsWith('DSD')
      || fmtUp === 'DSF' || fmtUp === 'DFF') return '\u{1F4C0}'; // 📀

  return '\u{1F50A}'; // 🔊 default (MP3, OGG, Opus, AAC, WavPack …)
}


// ── Focus trap ──────────────────────────────────────────────────────────────
/**
 * Trap focus inside ``rootEl`` — used by modal overlays (auth, trackinfo,
 * playlist edit, etc.) so Tab / Shift+Tab cycle within the modal instead
 * of leaking back into the dimmed app underneath.
 *
 * Returns a ``release()`` callback that detaches the listener.  Callers
 * are responsible for invoking ``release()`` on close, AND for restoring
 * focus to wherever the user was before the modal opened (capture
 * ``document.activeElement`` before invoking ``trapFocus`` — see the
 * auth + admin modules for the WCAG 2.4.3 restore pattern).
 *
 * Example:
 *
 *   const prevFocus = document.activeElement;
 *   modal.classList.remove('hidden');
 *   const release = trapFocus(modal);
 *   modal.querySelector('input').focus();
 *   // ...later, on close:
 *   release();
 *   prevFocus?.focus?.();
 *
 * @param {HTMLElement} rootEl
 * @returns {() => void} release callback
 */
export function trapFocus(rootEl) {
  if (!rootEl) return () => {};
  const FOCUSABLE_SELECTOR = (
    'a[href], button:not([disabled]), '
    + 'input:not([disabled]):not([type="hidden"]), '
    + 'select:not([disabled]), textarea:not([disabled]), '
    + '[tabindex]:not([tabindex="-1"])'
  );
  const _onKey = (e) => {
    if (e.key !== 'Tab') return;
    // Re-query every time — modal content can change while open (e.g.
    // the auth overlay toggles a display-name field, the password
    // dialog hides the "current password" row in set mode).
    const focusables = rootEl.querySelectorAll(FOCUSABLE_SELECTOR);
    // Filter to visibly-rendered nodes only (skip ``hidden`` ancestors).
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
  };
  rootEl.addEventListener('keydown', _onKey);
  return () => rootEl.removeEventListener('keydown', _onKey);
}
