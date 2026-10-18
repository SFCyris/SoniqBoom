// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * station_lookup.js — the header search box's station matches (Stations view).
 *
 * Station names are searched in the Radio Browser directory (through
 * ``GET /api/stations/search``), so the box asks only once typing paused
 * (``delayMs``), cancels the request a newer keystroke made stale, and keeps
 * recent answers (``ttlMs``, the newest ``maxEntries`` texts) so retyping or
 * deleting back to an earlier text asks nothing.
 *
 * ``fetchRows(text, signal)`` resolves to the station rows (an array) or
 * rejects; timers and the clock are injectable for tests.
 */
export function stationQueryKey(text) {
  return String(text ?? '').trim().replace(/\s+/g, ' ');
}

export function createStationLookup({
  fetchRows,
  delayMs = 350,
  minChars = 2,
  ttlMs = 5 * 60 * 1000,
  maxEntries = 32,
  now = () => Date.now(),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (t) => clearTimeout(t),
} = {}) {
  const cache = new Map();   // key → { at, rows }   (insertion order = age)
  let timer = null;
  let ctl = null;
  let ask = 0;               // the latest request(); older answers are dropped

  function cached(key) {
    const e = cache.get(key);
    if (!e) return null;
    if (now() - e.at >= ttlMs) { cache.delete(key); return null; }
    cache.delete(key); cache.set(key, e);            // newest again
    return e.rows;
  }
  function remember(key, rows) {
    cache.delete(key);
    cache.set(key, { at: now(), rows });
    while (cache.size > maxEntries) cache.delete(cache.keys().next().value);
  }

  /** Forget the pending ask and abort its request (Enter, Escape, the box emptied). */
  function cancel() {
    ask++;
    if (timer !== null) { clearTimer(timer); timer = null; }
    if (ctl) { try { ctl.abort(); } catch (_) { /* already done */ } ctl = null; }
  }

  /**
   * Station rows for ``text``: ``onRows(rows)`` runs once with the answer —
   * straight away for a remembered text, after the pause otherwise — unless a
   * newer request() or a cancel() came first; ``onError`` (optional) runs
   * instead when that request failed (not when it was aborted).  Text shorter
   * than ``minChars`` asks nothing.  Returns whether an answer is on its way.
   */
  function request(text, onRows, onError) {
    cancel();
    const key = stationQueryKey(text);
    if (key.length < minChars) return false;
    const hit = cached(key);
    if (hit) { onRows(hit); return true; }
    const mine = ask;
    timer = setTimer(() => {
      timer = null;
      if (mine !== ask) return;
      const c = typeof AbortController === 'function' ? new AbortController() : null;
      ctl = c;
      Promise.resolve()
        .then(() => fetchRows(key, c ? c.signal : undefined))
        .then((rows) => {
          if (ctl === c) ctl = null;
          // (an answer that is no station list — an error body — is a failure)
          if (!Array.isArray(rows)) throw new Error('not a station list');
          remember(key, rows);
          if (mine === ask) onRows(rows);
        })
        .catch(() => {                                   // aborted or failed: nothing kept
          if (ctl === c) ctl = null;
          if (mine === ask && onError) onError();
        });
    }, delayMs);
    return true;
  }

  return { request, cancel, get size() { return cache.size; } };
}
