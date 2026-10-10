// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * offline.js — Settings → "Tracks on unavailable drives" in the web UI.
 *
 * The server flags a track row that can't play right now (its drive or share
 * is offline and no cached copy can serve it) with
 * ``offline: 'drive' | 'share' | 'catalogue'`` — in Mark and Hide mode; in
 * Hide mode searches and track lists leave such tracks out altogether, and in
 * Show mode nothing is flagged.  This module:
 *
 *  - renders the flag the same way everywhere (desktop rows, search preview,
 *    queue, playlists, the phone's rows): a dimmed row + an "offline" badge
 *    whose tooltip and screen-reader text say why;
 *  - keeps the player's skip switch (``Player.setOfflineSkip``) in step with
 *    the setting, so auto-play passes those tracks by — and the other skip
 *    rule the server decides, "Skip short tunes" (``Player.setSkipShort``);
 *  - follows the server's availability token (``GET /api/library/availability``;
 *    pushed as the ``availability_changed`` WebSocket event on desktop, polled
 *    while visible on both shells) and, when it changes, re-flags the queue's
 *    rows (``POST /api/tracks/meta/availability`` — their offline flags and
 *    their reported playback problems, ``health``) and fires
 *    ``soniqboom:availability-changed`` so the open view re-reads.
 *
 * Its own module (not utils.js / player.js): a page still on an older service
 * worker's cached copy of those must not fail to link a new named import.
 */
import { Player } from './player.js';

const TITLES = {
  drive: 'Drive not connected — plays once you reconnect it; library data kept',
  share: 'Share offline — plays once the share is back; library data kept',
  catalogue: 'Catalogue offline — plays once it can be reached again',
};
const SR_TEXT = {
  drive: 'drive not connected: plays once you reconnect it, library data kept',
  share: 'share offline: plays once the share is back, library data kept',
  catalogue: 'catalogue offline: plays once it can be reached again',
};

/** ``'drive' | 'share' | 'catalogue'`` when the row can't play right now, else ''. */
export function offlineKind(t) {
  const k = t && t.offline;
  return (k === 'drive' || k === 'share' || k === 'catalogue') ? k : '';
}

/** The tooltip of an offline row. */
export function offlineTitle(kind) { return TITLES[kind] || TITLES.drive; }

/** The badge element (a fresh one per call). */
export function offlineBadge(kind) {
  const b = document.createElement('span');
  b.className = `track-offline-badge track-offline-${kind || 'drive'}`;
  b.title = offlineTitle(kind);
  const icon = document.createElement('span');
  icon.className = 'track-offline-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '⏏';                 // ⏏
  const txt = document.createElement('span');
  txt.textContent = 'offline';
  const sr = document.createElement('span');
  sr.className = 'sr-only';
  sr.textContent = ` — can't play right now, ${SR_TEXT[kind] || SR_TEXT.drive}`;
  b.append(icon, txt, sr);
  return b;
}

/** The same badge as HTML (for rows built from a template string). */
export function offlineBadgeHTML(kind) {
  const k = kind || 'drive';
  return `<span class="track-offline-badge track-offline-${k}" title="${offlineTitle(k)}">`
    + '<span class="track-offline-icon" aria-hidden="true">⏏</span><span>offline</span>'
    + `<span class="sr-only"> — can't play right now, ${SR_TEXT[k] || SR_TEXT.drive}</span></span>`;
}

const state = { mode: null, token: null, marks: null, busy: null, again: false, polling: false };
const POLL_MS = 60000;

function _apply(mode) {
  state.mode = mode;
  try { Player.setOfflineSkip?.(mode === 'mark' || mode === 'hide'); } catch (_) { /* older cached player.js */ }
}

function _applyShort(secs, placeholders) {
  try { Player.setSkipShort?.(Number(secs) || 0, placeholders); } catch (_) { /* older cached player.js */ }
}

// The fields a reported playback problem / a decode defect puts on a row.
const HEALTH_FIELDS = ['health', 'health_note', 'health_by', 'health_at', 'health_renderer',
                       'defect', 'defect_detail'];

/**
 * Re-flag ``rows`` in place from the server (they were flagged when fetched);
 * true when a flag changed.  Rows are shared between the queue and the view
 * that queued them, so both follow — a caller re-paints its rows itself.
 */
async function recheckRows(rows) {
  if (!rows || !rows.length) return false;
  const ids = [...new Set(rows.map(t => t && t.id).filter(Boolean))];
  const offline = {};
  const health = {};
  try {
    for (let i = 0; i < ids.length; i += 5000) {
      const r = await fetch('/api/tracks/meta/availability', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids.slice(i, i + 5000) }),
      });
      if (!r.ok) return false;
      const j = await r.json();
      Object.assign(offline, (j && j.offline) || {});
      Object.assign(health, (j && j.health) || {});
      if (j && j.mode && j.mode !== state.mode) _apply(j.mode);
    }
  } catch (_) { return false; }
  const flag = state.mode !== 'show';
  let changed = false;
  for (const t of rows) {
    if (!t || typeof t !== 'object') continue;
    const k = flag ? (offline[t.id] || '') : '';
    if ((t.offline || '') !== k) {
      if (k) t.offline = k; else delete t.offline;
      changed = true;
    }
    const h = health[t.id] || {};
    for (const f of HEALTH_FIELDS) {
      const v = h[f] ?? null;
      if ((t[f] ?? null) !== v) {
        if (v === null) delete t[f]; else t[f] = v;
        changed = true;
      }
    }
  }
  return changed;
}

/** Re-flag the queue's rows from the server. */
async function recheckQueue() {
  const q = Player.queue || [];
  if (!q.length) return;
  if (await recheckRows(q.slice())) { try { Player.refreshQueueView?.(); } catch (_) {} }
}

/**
 * Read the server's availability state; when its token moved, re-flag the
 * queue and tell the page (``soniqboom:availability-changed``).  Concurrent
 * calls share one request.
 */
async function refresh() {
  // A call while one runs (a WebSocket event during the queue re-check) asks
  // for one more read once it is done — never answered by the older read.
  if (state.busy) { state.again = true; return state.busy; }
  state.busy = (async () => {
    let changed = false;
    do {
      state.again = false;
      try {
        const r = await fetch('/api/library/availability');
        if (!r.ok) continue;
        const j = await r.json();
        if (!j || typeof j.token !== 'string') continue;
        const prevMode = state.mode, prevToken = state.token;
        _apply(j.mode || 'mark');
        if (j.skip_short_tunes !== undefined) _applyShort(j.skip_short_tunes, j.tune_placeholders);
        // A playback problem reported / cleared (anyone's): only the rows held
        // here — the queue — re-read their marks; no list changes.
        const prevMarks = state.marks;
        state.marks = j.marks ?? null;
        if (prevMarks !== null && j.marks != null && prevMarks !== j.marks && prevToken === j.token) {
          await recheckQueue();
          document.dispatchEvent(new CustomEvent('soniqboom:marks-changed'));
        }
        state.token = j.token;
        if (prevToken === null || prevToken === j.token) continue;
        // (List positions moved — Hide mode's lists changed length, and with
        // duplicates hidden a group whose primary can't play is listed by
        // another member, at that member's place (Mark too): the queue's next
        // page starts after its last list row, not at the old position.)
        if (j.mode !== 'show' || prevMode !== 'show') {
          try { Player.reanchorOrdered?.(); } catch (_) { /* older cached player.js */ }
        }
        await recheckQueue();
        document.dispatchEvent(new CustomEvent('soniqboom:availability-changed', {
          detail: { mode: j.mode, prevMode, token: j.token, unavailable: j.unavailable || 0 },
        }));
        changed = true;
      } catch (_) { /* the next poll tries again */ }
    } while (state.again);
    state.busy = null;
    return changed;
  })();
  return state.busy;
}

/**
 * Start following the setting.  ``mode``: the value the page already has (the
 * ui-config's ``offline_tracks``), applied at once so the player skips from the
 * first track.  Re-flags a queue restored from the last visit.
 */
async function init({ mode, skipShort, placeholders } = {}) {
  if (mode) _apply(mode);
  if (skipShort !== undefined) _applyShort(skipShort, placeholders);
  if (!state.polling) {
    state.polling = true;
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  }
  await refresh();
  // A queue restored from localStorage carries the flags of its last visit.
  await recheckQueue();
}

export const Offline = {
  get mode() { return state.mode; },
  init, refresh, recheckQueue, recheckRows,
};
