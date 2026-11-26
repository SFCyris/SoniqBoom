// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * stream_recovery.js — how an internet-radio stream recovers, shared by the
 * desktop (stations.js) and the phone (mobile/radio-player.js).
 *
 * A brief buffer dip recovers on its own; a SUSTAINED stall is handled.  A
 * station that drops the connection (the relay or the station ended the
 * stream) is reconnected — the same stream, a few times, with a backoff —
 * and the reconnect budget is forgiven only after sustained healthy play, so
 * a stream that plays a fraction of a second before dropping cannot
 * reconnect forever.  The phone also watches the playhead: WebKit gives no
 * event at all when a direct stream's connection drops (``FROZEN_MS``).
 *
 * The desktop uses the numbers and the helpers below inside its own handlers
 * (it also steps down to a lower-quality stream, which the phone has no
 * ladder for).  ``createStreamRecovery`` is the phone's whole watcher for the
 * element that carries a station; it touches nothing but that element and
 * the hooks it is given, so it runs outside a browser too
 * (tests/js/stream_recovery.test.mjs).
 */

export const STALL_SUSTAINED_MS = 1200;   // a MID-STREAM 'waiting' lasting this long = a real underrun
export const STARTUP_GRACE_MS   = 9000;   // a fresh stream gets this long to CONNECT
export const REBUFFER_TARGET_S  = 6;      // "longer buffer (2x)": seconds buffered ahead before resuming
export const REBUFFER_QUARTER_S = REBUFFER_TARGET_S / 4;   // worst-case floor — a quarter of the target
export const REBUFFER_GIVEUP_MS = 8000;   // after this long without the full buffer, accept the quarter
export const MAX_RECONNECTS     = 3;      // relay/upstream EOF: reconnect the same stream up to N times
export const RECONNECT_BASE_MS  = 600;    // first reconnect waits this long (then backs off)
export const RECONNECT_MAX_MS   = 4000;   // backoff ceiling
export const GOOD_PLAY_RESET_MS = 10000;  // sustained playback before the reconnect budget is forgiven
// The phone: a rebuffer that has not even the quarter after this long is a
// connection that died without saying so (a phone moving from Wi-Fi to the
// mobile network) — reconnect instead of waiting on it for good.
export const REBUFFER_RECONNECT_MS = 16000;
// The phone: a playhead that has not moved for this long while the element
// says it plays.  WebKit tells nothing when a direct stream's connection
// drops — no 'ended', no 'error', no 'waiting', readyState stays 4: the
// playhead just stops at the end of what arrived (measured: a 'stalled' only).
export const FROZEN_MS = 2500;
const REBUFFER_POLL_MS = 250;
const WATCH_POLL_MS = 500;
const FROZEN_AGAIN_MS = 60000;

/** The wait before reconnect number ``n`` (1, 2, 3 …): 600 ms, 1.2 s, 2.4 s, then 4 s. */
export function reconnectDelay(n) {
  const k = Math.max(1, Math.floor(Number(n) || 1));
  return Math.min(RECONNECT_BASE_MS * 2 ** (k - 1), RECONNECT_MAX_MS);
}

/** Seconds of CONTIGUOUS audio buffered ahead of the playhead (0 when nothing
 *  is buffered there).  The range that covers ``currentTime`` counts, not the
 *  end of the last range — a gap ahead must not count as "buffered", or a
 *  rebuffer would resume into silence. */
export function bufferedAhead(audio) {
  try {
    const b = audio.buffered;
    if (!b || !b.length) return 0;
    const t = audio.currentTime || 0;
    for (let i = 0; i < b.length; i++) {
      if (b.start(i) <= t + 0.25 && b.end(i) > t) return Math.max(0, b.end(i) - t);
    }
    return 0;
  } catch (_) { return 0; }
}

/** A rebuffer may resume: the full target is buffered (or the element says it
 *  has enough), or — after the give-up wait — a quarter of it. */
export function rebufferReady(ahead, waitedMs, readyState) {
  return ahead >= REBUFFER_TARGET_S || readyState >= 4      // HAVE_ENOUGH_DATA
    || (waitedMs >= REBUFFER_GIVEUP_MS && ahead >= REBUFFER_QUARTER_S);
}

/**
 * The watcher for the element that plays a station.
 *
 *   hooks.reconnect(a)   load the same stream on ``a`` again and play it
 *   hooks.giveUp(why)    the reconnect budget is spent: the station is lost
 *   hooks.status(s)      the status changed (see below)
 *   hooks.timers         { setTimeout, clearTimeout, setInterval, clearInterval } (tests)
 *   hooks.now            a millisecond clock (tests)
 *
 * Status: 'off' (nothing watched), 'tuning' (a fresh start, not played yet),
 * 'live', 'buffering' (a sustained stall: paused here to fill a longer
 * buffer), 'reconnecting' (the stream dropped: waiting for, or making, the
 * next connection), 'paused' (by the listener).
 *
 * The owner forwards the element's media events with ``handle(type, a)``
 * ('playing', 'pause', 'waiting', 'ended', 'error'), calls ``watch(a)`` when a fresh
 * start was issued on ``a`` (a new station, a re-tune), ``pause()`` /
 * ``resume()`` for the listener's pause and play, ``stop()`` when the station
 * goes.  Before a fresh start first plays, its errors are the start's own
 * business (the owner's play() rejects and falls back): only a stream that
 * played once, or a reconnect, is recovered here.
 */
export function createStreamRecovery(hooks = {}) {
  const T = hooks.timers || globalThis;
  const now = hooks.now || (() => (typeof performance !== 'undefined' ? performance.now() : Date.now()));
  let el = null;
  let status = 'off';
  let hasPlayed = false;     // the element played since the last (re)start
  let reconnecting = false;  // a reconnect is pending or loading
  let reconnects = 0;
  let rebuffering = false;
  let userPaused = false;
  let frozeAt = -Infinity;   // the last frozen playhead that was rebuffered (not reconnected)
  let stallT = null, startupT = null, reconnectT = null, goodT = null, rebufferI = null, watchI = null;

  function setStatus(s) {
    if (s === status) return;
    status = s;
    try { hooks.status && hooks.status(s); } catch (e) { console.error(e); }
  }
  function clearAll() {
    T.clearTimeout(stallT); T.clearTimeout(startupT); T.clearTimeout(reconnectT);
    T.clearTimeout(goodT); T.clearInterval(rebufferI); T.clearInterval(watchI);
    stallT = startupT = reconnectT = goodT = rebufferI = watchI = null;
    rebuffering = false;
  }

  // While it is live: a playhead that stopped moving with nothing buffered
  // ahead is a connection that went (reconnect); with audio still ahead, a
  // stall (rebuffer) — once: frozen again within a minute, it reconnects
  // (a decoder that keeps sticking would otherwise loop BUFFER / LIVE).
  function watchPlayhead(a) {
    T.clearInterval(watchI);
    let last = a.currentTime || 0;
    let movedAt = now();
    watchI = T.setInterval(() => {
      if (el !== a) { T.clearInterval(watchI); watchI = null; return; }
      const t = a.currentTime || 0;
      if (t !== last || a.paused || status !== 'live' || userPaused) { last = t; movedAt = now(); return; }
      if (now() - movedAt < FROZEN_MS) return;
      movedAt = now();
      if (bufferedAhead(a) < REBUFFER_QUARTER_S || now() - frozeAt < FROZEN_AGAIN_MS) { frozeAt = -Infinity; reconnect('frozen'); }
      else { frozeAt = now(); rebuffer(); }
    }, WATCH_POLL_MS);
  }

  function reconnect(why) {
    if (!el || userPaused) return;
    clearAll();
    if (reconnects >= MAX_RECONNECTS) {
      reconnecting = false;
      setStatus('off');
      el = null;
      try { hooks.giveUp && hooks.giveUp(why); } catch (e) { console.error(e); }
      return;
    }
    reconnects++;
    reconnecting = true;
    hasPlayed = false;
    setStatus('reconnecting');
    const a = el;
    reconnectT = T.setTimeout(() => {
      reconnectT = null;
      if (el !== a || userPaused) return;
      // A reconnect that never plays within the grace is the next attempt.
      startupT = T.setTimeout(() => {
        startupT = null;
        if (el === a && !hasPlayed && !userPaused) reconnect('startup');
      }, STARTUP_GRACE_MS);
      try { hooks.reconnect && hooks.reconnect(a); } catch (e) { console.error(e); reconnect('error'); }
    }, reconnectDelay(reconnects));
  }

  // A sustained stall: pause and fill a longer buffer here, then resume from
  // where it stalled — the browser would otherwise skip to the live edge,
  // which sounds like jumping to the next song.
  function rebuffer() {
    const a = el;
    if (!a || rebuffering || userPaused) return;
    rebuffering = true;
    setStatus('buffering');
    try { a.pause(); } catch (_) { /* ignore */ }
    const started = now();
    T.clearInterval(rebufferI);
    rebufferI = T.setInterval(() => {
      if (el !== a || !rebuffering) { T.clearInterval(rebufferI); rebufferI = null; return; }
      const waited = now() - started;
      if (rebufferReady(bufferedAhead(a), waited, a.readyState || 0)) {
        T.clearInterval(rebufferI); rebufferI = null;
        rebuffering = false;
        // A refused play(), or no 'playing' within the grace: the next connection.
        startupT = T.setTimeout(() => {
          startupT = null;
          if (el === a && status === 'buffering' && !userPaused) reconnect('stalled');
        }, STARTUP_GRACE_MS);
        const p = a.play();
        if (p && p.catch) p.catch(() => { if (el === a && status === 'buffering' && !userPaused) reconnect('stalled'); });
      } else if (waited >= REBUFFER_RECONNECT_MS) {
        T.clearInterval(rebufferI); rebufferI = null;
        rebuffering = false;
        reconnect('stalled');
      }
    }, REBUFFER_POLL_MS);
  }

  function handle(type, a) {
    if (!el || a !== el) return;
    if (type === 'playing') {
      if (a.paused) return;                    // (stale: paused again since)
      // (also a play nobody here asked for — the system resuming after a call:
      // the listener's pause is over, recovery watches again)
      userPaused = false;
      hasPlayed = true;
      reconnecting = false;
      T.clearTimeout(startupT); startupT = null;
      T.clearTimeout(stallT); stallT = null;
      // The budget is forgiven 10 s after the stream came back — counted from
      // its first 'playing' (the brief waiting / playing blips of a live
      // stream must not keep postponing it; a reconnect clears the timer).
      if (!goodT && reconnects > 0) goodT = T.setTimeout(() => { goodT = null; reconnects = 0; }, GOOD_PLAY_RESET_MS);
      if (!rebuffering) setStatus('live');
      watchPlayhead(a);
    } else if (type === 'pause') {
      // A pause nobody here asked for (the lock screen, a call taking the
      // audio): the listener's — nothing is reconnected behind it.  Not the
      // pause before 'ended', the rebuffer's own, a failed element's, or a
      // stale one (the element plays again by the time it is told).
      if (status === 'live' && a.paused && !a.ended && !a.error && !rebuffering) {
        clearAll();
        userPaused = true;
        setStatus('paused');
      }
    } else if (type === 'waiting') {
      // (a 'waiting' before the first 'playing' is the start buffering)
      if (!hasPlayed || rebuffering || userPaused || reconnecting) return;
      T.clearTimeout(stallT);
      stallT = T.setTimeout(() => {
        stallT = null;
        if (el === a && !rebuffering && !userPaused && !reconnecting) rebuffer();
      }, STALL_SUSTAINED_MS);
    } else if (type === 'ended') {
      if (userPaused) return;
      if (hasPlayed || reconnecting) reconnect('ended');
    } else if (type === 'error') {
      const e = a.error;
      if (e && e.code === 1 /* MEDIA_ERR_ABORTED: we swapped the source */) return;
      if (userPaused) return;
      if (hasPlayed || reconnecting) reconnect('error');
    }
  }

  return {
    get status() { return status; },
    /** Recovering on the listener's behalf: the key still means "playing". */
    get recovering() { return !userPaused && (status === 'buffering' || status === 'reconnecting'); },
    get reconnects() { return reconnects; },
    handle,
    watch(a) {
      clearAll();
      frozeAt = -Infinity;
      el = a;
      hasPlayed = false;
      reconnecting = false;
      reconnects = 0;
      userPaused = false;
      setStatus(a ? 'tuning' : 'off');
    },
    pause() {
      if (!el) return;
      // A reconnect pending or loading: the stream is gone — the resume loads it again.
      const dead = reconnecting;
      clearAll();
      userPaused = true;
      reconnecting = dead;
      setStatus('paused');
    },
    /** The listener pressed play: true when the stream had to be loaded
     *  again (it had dropped, or the element ended / failed) — then the
     *  owner must not call play() itself. */
    resume() {
      if (!el) return false;
      const a = el;
      userPaused = false;
      if (reconnecting || a.ended || a.error) {
        reconnecting = true;
        hasPlayed = false;
        setStatus('reconnecting');
        T.clearTimeout(startupT);
        startupT = T.setTimeout(() => {
          startupT = null;
          if (el === a && !hasPlayed && !userPaused) reconnect('startup');
        }, STARTUP_GRACE_MS);
        try { hooks.reconnect && hooks.reconnect(a); } catch (e) { console.error(e); }
        return true;
      }
      // A plain play(): 'playing' follows.  A station that never played
      // counts its errors as a reconnect's; none comes within the grace —
      // the element still trying: the next connection; refused (paused): paused.
      if (!hasPlayed) reconnecting = true;
      setStatus(hasPlayed ? 'live' : 'tuning');
      T.clearTimeout(startupT);
      startupT = T.setTimeout(() => {
        startupT = null;
        if (el !== a || userPaused) return;
        if (a.paused) { clearAll(); userPaused = true; setStatus('paused'); }
        else reconnect('startup');
      }, STARTUP_GRACE_MS);
      return false;
    },
    stop() {
      clearAll();
      el = null;
      hasPlayed = false;
      reconnecting = false;
      reconnects = 0;
      userPaused = false;
      setStatus('off');
    },
  };
}
