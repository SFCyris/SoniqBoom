// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * radio-player.js — dedicated internet-radio player for the mobile UI.
 *
 * Uses its OWN <audio> element that is deliberately NOT:
 *   · crossorigin — so a station without CORS headers still loads and plays
 *     (the shared library element sets crossorigin="anonymous", which turns a
 *     non-CORS cross-origin stream into a hard load failure);
 *   · routed through the Web Audio graph — a cross-origin element connected to
 *     Web Audio without CORS outputs silence, so keeping radio OUT of the graph
 *     is what makes DIRECT (station → phone) playback audible.
 *
 * With the Now Playing meters off, the phone streams the station DIRECTLY,
 * sparing the server the relay hop (and its bandwidth) for every station whose
 * URL a browser can open on its own; stations whose direct URL a browser can't
 * play (SHOUTcast roots that serve an HTML page, HLS, playlist wrappers) fall
 * back to /api/stations/relay — which this same raw element plays too.
 * With the meters on (``metersOn``), the station plays through the relay on a
 * SECOND element, ``elG``, routed through the shared Player's Web Audio graph
 * (``Player.routeExternalAudio``) — the relay is same-origin, so the meters and
 * the equalizer get its samples.  A MediaElementSource is for good, so the
 * direct element is never routed: a cross-origin stream through the graph
 * would be silent.  ``play(…, {metered})`` picks the element; ``metered`` says
 * which one plays.
 *
 * Radio and the shared library Player are MUTUALLY EXCLUSIVE: starting one
 * stops the other, so only one thing is ever making sound.
 */
import { Player } from '../player.js';

const el = new Audio();
el.preload = 'none';
try {
  const v = localStorage.getItem('sb_volume');
  el.volume = v !== null ? parseFloat(v) : 0.8;
} catch { /* ignore */ }
let elG = null;          // the relay through the audio graph (created on first use)
let cur = el;            // the element playing (or last asked to)

const listeners = { change: [], state: [] };
let _station = null;
// Monotonic token: every play() and stop() bumps it.  An in-flight play() that
// awaits ``el.play()`` compares its captured token against the live one before
// taking over — if a newer play() or a stop() ran meanwhile, that call now owns
// (or silenced) the shared element, so the stale one abandons instead of
// resurrecting itself on top.  (QA 2026-07-05: buffering-window race.)
let _playSeq = 0;
// The play() a pause by the user landed in while it tuned in: that play keeps
// its station, paused, instead of failing (a fallback would play on).
let _pausedInSeq = -1;

function emit(ev) {
  (listeners[ev] || []).forEach(cb => { try { cb(); } catch (e) { console.error(e); } });
}

function _setPlaybackState(state) {
  if (_station && typeof navigator !== 'undefined' && 'mediaSession' in navigator) {
    try { navigator.mediaSession.playbackState = state; } catch { /* ignore */ }
  }
}

function _wire(a) {
  // Only the element in use reports: the other one's late events (its stream
  // released) are no news.
  a.addEventListener('playing', () => { if (a === cur) { _setPlaybackState('playing'); emit('state'); } });
  a.addEventListener('pause',   () => { if (a === cur) { _setPlaybackState('paused');  emit('state'); } });
  a.addEventListener('ended',   () => { if (a === cur) emit('state'); });
  a.addEventListener('error',   () => { if (a === cur) emit('state'); });
}
_wire(el);

function _graphEl() {
  if (!elG) {
    elG = new Audio();
    elG.preload = 'none';
    _wire(elG);
  }
  return elG;
}

function _release(a) {
  if (!a) return;
  try { a.pause(); } catch { /* ignore */ }
  a.removeAttribute('src');
  a.load();
}

/** The Now Playing meters are on (the phone's visualization settings: on,
 *  unless the master switch or the Now-playing group is off) — read from
 *  storage, so a change made in another tab counts. */
export function metersOn() {
  try {
    const s = JSON.parse(localStorage.getItem('sb_viz_settings') || '{}');
    return s.enabled !== false && s.nowPlaying !== false;
  } catch { return true; }
}

/** Once a station is confirmed live, fully take ownership of audio output from
 *  the shared library element: clear its ``src`` so neither the lock-screen
 *  transport (its MediaSession handlers) nor its own 'error'/'ended' recovery
 *  can resume or advance the paused library track underneath the stream — and
 *  point the OS media session at the station.  (QA 2026-07-05.) */
function _takeOver(station) {
  // Suspend the shared Player FIRST: this sets a guard its own ended/error/SID-
  // handoff paths early-return on, so clearing ``src`` below can't be undone by
  // a recovery path re-writing it (clearing src alone was insufficient).
  try { Player.suspendForExternalAudio(true); } catch { /* ignore */ }
  try { Player.audio && Player.audio.removeAttribute('src'); Player.audio && Player.audio.load(); }
  catch { /* ignore */ }
  if (typeof navigator !== 'undefined' && 'mediaSession' in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title:   station.name || 'Radio',
        artist:  'Internet radio',
        artwork: station.favicon ? [{ src: station.favicon }] : [],
      });
      navigator.mediaSession.setActionHandler('play',  () => { cur.play().catch(() => {}); });
      navigator.mediaSession.setActionHandler('pause', () => { cur.pause(); });
      navigator.mediaSession.setActionHandler('nexttrack', null);
      navigator.mediaSession.setActionHandler('previoustrack', null);
      navigator.mediaSession.playbackState = 'playing';
    } catch { /* ignore */ }
  }
}

export const MobileRadio = {
  get el()      { return cur; },
  get active()  { return !!_station; },
  get station() { return _station; },
  get playing() { return !!_station && !cur.paused; },
  /** The station plays through the audio graph (the meters can read it). */
  get metered() { return !!_station && cur === elG; },

  on(ev, cb) { (listeners[ev] || (listeners[ev] = [])).push(cb); },

  /** Play a station at ``url`` (direct or relay).  ``metered``: on the element
   *  routed through the audio graph (``url`` must be the same-origin relay); it
   *  plays on the plain one when there is no graph.  Rejects if playback can't
   *  start, so the caller can fall back (e.g. direct → relay). */
  async play(station, url, { metered = false } = {}) {
    const seq = ++_playSeq;        // supersede any in-flight play()/stop()
    // Pause library playback (mutual exclusion) but DON'T clear its src yet — a
    // failed tune-in can still fall back / resume it.  The full take-over that
    // clears the shared element only happens once the station is confirmed live.
    try { Player.audio && Player.audio.pause(); } catch { /* ignore */ }
    _station = station;
    const a = metered && Player.routeExternalAudio && Player.routeExternalAudio(_graphEl())
      ? elG : el;
    try {
      const v = localStorage.getItem('sb_volume');   // re-sync (may have changed)
      if (v !== null) a.volume = parseFloat(v);
    } catch { /* ignore */ }
    _release(el);                  // release the previous live stream's decoder
    _release(elG);
    cur = a;
    a.src = url;
    a.load();
    emit('change');                // UI shows the new station immediately
    try {
      await a.play();
    } catch (e) {
      // Superseded while connecting (newer play() or a stop() ran) → abandon
      // quietly so the caller doesn't fall back to the relay for a station the
      // user already moved on from.
      if (seq !== _playSeq) return;
      // Paused by the user while it tuned in: the station stays, paused.
      if (_pausedInSeq === seq) { _takeOver(station); emit('state'); return; }
      emit('state');
      throw e;                     // genuine failure — caller falls back to relay
    }
    // A newer play() or a stop() landed while we awaited a.play(); that call now
    // owns the element (playing its own stream) or silenced it — don't take over
    // on top of it, or radio would resurrect after the user picked something else.
    if (seq !== _playSeq) { emit('state'); return; }
    _takeOver(station);            // station is live → own the output + lock screen
    emit('state');
  },

  /** Set by the radio view: called on a resume, it may tune the station in
   *  again instead (the meters were switched meanwhile) and say so (true). */
  onResume: null,

  toggle() {
    if (!_station) return;
    if (cur.paused) {
      if (MobileRadio.onResume && MobileRadio.onResume()) return;
      cur.play().catch(() => {});
    } else {
      _pausedInSeq = _playSeq;
      cur.pause();
    }
  },

  stop() {
    _playSeq++;                    // invalidate any in-flight play() takeover
    _release(el);
    _release(elG);
    cur = el;
    _station = null;
    // Un-suspend the shared Player so the library element works normally again.
    try { Player.suspendForExternalAudio(false); } catch { /* ignore */ }
    if (typeof navigator !== 'undefined' && 'mediaSession' in navigator) {
      try {
        navigator.mediaSession.playbackState = 'none';
        navigator.mediaSession.setActionHandler('play', null);
        navigator.mediaSession.setActionHandler('pause', null);
      } catch { /* ignore */ }
    }
    emit('change');
    emit('state');
  },
};

// A library track starting (real track id) supersedes radio — stop it so the
// two elements never play at once.  Station "tracks" have an empty id and never
// reach here (radio doesn't go through Player), so this only fires for library.
Player.on('trackchange', (t) => {
  if (t && t.id && _station) MobileRadio.stop();
});
