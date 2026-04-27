// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * restart.js — Client-side UX for the /api/admin/restart endpoint.
 *
 * Flow:
 *   1.  Show a full-screen overlay and set document.title so the Dock /
 *       window wrapper shows "Restarting…".
 *   2.  Decide whether to keep playing:
 *        - If the audio buffer has > FADE_THRESHOLD seconds of lead time,
 *          let playback continue silently; the browser will coast across
 *          the outage from its own buffer.
 *        - Otherwise fade audio out, park the element with its src intact,
 *          and surface "Restarting — will continue playing when restart
 *          completed." in the overlay.
 *   3.  Poll /api/health until the server comes back.
 *   4.  Restore document.title, dismiss the overlay; if playback was
 *       paused/faded, resume streaming (the <audio> element re-requests
 *       byte ranges automatically) and blend back in.
 */

import { Player } from './player.js';

const FADE_THRESHOLD = 1.0;   // seconds of remaining buffer required to avoid the fade
const HEALTH_URL     = '/api/health';
const POLL_INITIAL   = 500;   // ms, let the server breathe before first poll
const POLL_INTERVAL  = 750;   // ms between health probes
const POLL_TIMEOUT   = 60000; // give up after 60 s
const FADE_MS        = 500;

let _originalTitle = '';
let _inFlight = false;

// ── DOM ─────────────────────────────────────────────────────────────────────

function _ensureOverlay() {
  let el = document.getElementById('restart-overlay');
  if (el) return el;

  el = document.createElement('div');
  el.id = 'restart-overlay';
  el.setAttribute('role', 'alertdialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-live', 'assertive');
  el.innerHTML = `
    <div class="restart-card">
      <div class="restart-spinner" aria-hidden="true"></div>
      <div class="restart-title">Restarting SoniqBoom…</div>
      <div class="restart-subtitle" id="restart-subtitle">
        The server is coming back online.
      </div>
    </div>
  `;
  document.body.appendChild(el);
  return el;
}

function _setSubtitle(text) {
  const s = document.getElementById('restart-subtitle');
  if (s) s.textContent = text;
}

// ── Audio helpers ───────────────────────────────────────────────────────────

function _bufferedAhead(audio) {
  try {
    const b = audio.buffered;
    const t = audio.currentTime;
    for (let i = 0; i < b.length; i++) {
      if (t >= b.start(i) && t <= b.end(i)) return b.end(i) - t;
    }
  } catch { /* ignore */ }
  return 0;
}

function _fadeVolume(audio, to, ms) {
  return new Promise(resolve => {
    const from = audio.volume;
    const steps = Math.max(1, Math.round(ms / 16));
    let i = 0;
    const id = setInterval(() => {
      i++;
      audio.volume = from + (to - from) * (i / steps);
      if (i >= steps) { clearInterval(id); resolve(); }
    }, 16);
  });
}

// ── Health polling ──────────────────────────────────────────────────────────

async function _pollUntilHealthy() {
  const deadline = Date.now() + POLL_TIMEOUT;
  await new Promise(r => setTimeout(r, POLL_INITIAL));
  while (Date.now() < deadline) {
    try {
      const res = await fetch(HEALTH_URL, { cache: 'no-store' });
      if (res.ok) return true;
    } catch { /* server is down — expected */ }
    await new Promise(r => setTimeout(r, POLL_INTERVAL));
  }
  return false;
}

// ── Public entry point ──────────────────────────────────────────────────────

/**
 * Begin the restart flow.  Call *after* you have already POSTed to
 * /api/admin/restart — the overlay is decoupled from the HTTP request so
 * the caller can surface request errors separately.
 *
 * @returns {Promise<boolean>}  true on successful reconnect, false on timeout.
 */
export async function runRestartFlow() {
  if (_inFlight) return false;
  _inFlight = true;

  const overlay = _ensureOverlay();
  overlay.classList.add('visible');

  _originalTitle = document.title;
  document.title = 'Restarting — SoniqBoom';

  const audio = Player.audio;
  const wasPlaying = !audio.paused;
  const aheadAtStart = _bufferedAhead(audio);

  let faded = false;
  let savedVolume = audio.volume;
  let resumeSrc = null;
  let resumeTime = 0;

  if (wasPlaying && aheadAtStart < FADE_THRESHOLD) {
    // Not enough buffer to coast — fade out, remember where we were, and
    // park the <audio> element so it doesn't spam failing requests while
    // the server is down.
    _setSubtitle('Restarting — will continue playing when restart completed.');
    savedVolume = audio.volume;
    resumeSrc = audio.currentSrc || audio.src || null;
    resumeTime = audio.currentTime || 0;
    await _fadeVolume(audio, 0, FADE_MS);
    try { audio.pause(); } catch { /* ignore */ }
    faded = true;
  } else if (wasPlaying) {
    _setSubtitle(`Playback will continue (${aheadAtStart.toFixed(1)} s buffered).`);
  } else {
    _setSubtitle('The server is coming back online.');
  }

  const ok = await _pollUntilHealthy();

  if (!ok) {
    _setSubtitle('Restart is taking longer than expected — reloading.');
    document.title = _originalTitle || document.title;
    setTimeout(() => window.location.reload(), 1500);
    _inFlight = false;
    return false;
  }

  if (faded && resumeSrc) {
    try {
      // Re-assigning src forces the element to re-open the connection.
      audio.src = resumeSrc;
      audio.currentTime = resumeTime;
      audio.volume = 0;
      await audio.play().catch(() => {});
      await _fadeVolume(audio, savedVolume, FADE_MS);
    } catch { /* fall through — user can tap play */ }
  } else if (wasPlaying) {
    // Buffer carried us through — nothing to do, but nudge playback just in
    // case the element stalled during the outage.
    try { await audio.play().catch(() => {}); } catch { /* ignore */ }
  }

  document.title = _originalTitle || document.title;
  overlay.classList.remove('visible');
  _inFlight = false;
  return true;
}
