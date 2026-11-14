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
 *
 * What the station plays (``now``): a station played through the relay has its
 * song title read by the server, which tells every client at a change of song
 * (the library socket's ``radio_meta``, and ``radio_art`` when it found the
 * song's cover — app.js hands both to ``noteMeta`` / ``noteArt``); what it
 * last said is asked once the station plays and when the socket comes back
 * (``refreshNow`` → GET /api/stations/now/{sid}).  A station the phone streams
 * directly has no relay reading its titles: ``now`` stays null.
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

const listeners = { change: [], state: [], meta: [] };
let _station = null;
let _viaRelay = false;   // the station plays through the server's relay
let _owned = false;      // the station owns the media session (_takeOver ran)
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

// [m-radio-now:begin] — tests/js/mobile_radio_now.test.mjs runs this region as is
// What the station said it plays: {title, artist, song, cover} — ``title`` as
// the station sent it, ``artist`` / ``song`` its two halves when it splits
// cleanly, ``cover`` the song's cover once one was found.
let _now = null;
let _nowSeq = 0;         // every title / cover taken: an older answer is dropped
// A cover comes from this server only, in the two shapes the server names:
// the art of a matched library track, or the cover it fetched for the song.
const _COVER_RE = /^\/api\/(?:art\/[A-Za-z0-9_-]+\/(?:sm|lg|full)\.jpg|stations\/nowplaying-art\/[a-z0-9-]{1,120})$/;
const _str = (v) => (typeof v === 'string' ? v : '');
// A track's art is asked for without the placeholder image: a track whose
// cover is gone answers with no image, and the station's logo stays.
const _coverOf = (u) => (!_COVER_RE.test(u) ? null
  : u.startsWith('/api/art/') ? `${u}?fallback=none` : u);

/** A title the station said.  The same title said again (a relay that
 *  reconnected) changes nothing — its cover stays; another title is another
 *  song: the cover goes until one is found for it. */
function _applyMeta(m) {
  const title = _str(m && m.title);
  if (!title || (_now && _now.title === title)) return false;
  _now = { title, artist: _str(m.artist), song: _str(m.song), cover: null };
  _nowSeq++;
  return true;
}

/** The cover found for a title — only for the title that still plays. */
function _applyArt(m) {
  if (!_now || !m || m.title !== _now.title) return false;
  const cover = _coverOf(_str(m.cover_url));
  if (!cover || cover === _now.cover) return false;
  _now.cover = cover;
  _nowSeq++;
  return true;
}

function _forgetNow() { _now = null; _nowSeq++; }

/** A station's two lines: the song with "artist — station" under it while it
 *  says what it plays (``now``), else its name and "Internet radio". */
function _stationLines(st, now) {
  const name = (st && st.name) || 'Radio';
  if (!now) return { title: name, sub: 'Internet radio' };
  return { title: now.song || now.title, sub: [now.artist, name].filter(Boolean).join(' \u2014 ') };
}
// [m-radio-now:end]

/** The lock screen / notification: the song and its cover while the station
 *  says one, else the station and its logo. */
function _sessionMeta() {
  if (!_station || !_owned) return;
  if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
  const st = _station, n = _now;
  const art = (n && n.cover) || st.favicon;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title:   n ? (n.song || n.title) : (st.name || 'Radio'),
      artist:  n ? (n.artist || st.name || 'Internet radio') : 'Internet radio',
      album:   n ? (st.name || '') : '',
      artwork: art ? [{ src: art }] : [],
    });
  } catch { /* ignore */ }
}

function _said() { _sessionMeta(); emit('meta'); }
const _mine = (m) => !!(_station && _viaRelay && m && m.sid && m.sid === _station.sid);

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
  _owned = true;
  _sessionMeta();
  if (typeof navigator !== 'undefined' && 'mediaSession' in navigator) {
    try {
      navigator.mediaSession.setActionHandler('play',  () => { cur.play().catch(() => {}); });
      navigator.mediaSession.setActionHandler('pause', () => { cur.pause(); });
      navigator.mediaSession.setActionHandler('nexttrack', null);
      navigator.mediaSession.setActionHandler('previoustrack', null);
      navigator.mediaSession.playbackState = 'playing';
    } catch { /* ignore */ }
  }
}

/** The station's picture in ``host`` (the mini-player's art, the cassette
 *  label): the 📻 placeholder, the station's logo over it, and the playing
 *  song's cover over both.  Each image fades in once it loaded and leaves no
 *  trace if it fails, so what is under it shows.  Only what changed is
 *  touched: a new title with the same logo leaves the logo alone, and a song
 *  without a cover takes the earlier song's cover away. */
export function paintStationArt(host, { logo = '', cover = '', phClass = '' } = {}) {
  const base = `radio\n${logo}`;
  if (host.dataset.artBase !== base) {
    host.innerHTML = '';
    const span = document.createElement('span');
    if (phClass) span.className = phClass;
    span.textContent = '\u{1F4FB}';   // 📻
    host.appendChild(span);
    if (logo) host.appendChild(_fadeImg(logo, 'logo'));
    host.dataset.artBase = base;
    host.dataset.artCover = '';
  }
  if ((host.dataset.artCover || '') === cover) return;
  host.querySelectorAll('img[data-role="cover"]').forEach((i) => i.remove());
  if (cover) host.appendChild(_fadeImg(cover, 'cover'));
  host.dataset.artCover = cover;
}
/** ``host`` shows something else now (a track's cover): forget the station's. */
export function forgetStationArt(host) {
  delete host.dataset.artBase;
  delete host.dataset.artCover;
}
function _fadeImg(src, role) {
  const img = new Image();
  img.alt = ''; img.decoding = 'async';
  img.dataset.role = role;
  img.onload  = () => img.classList.add('loaded');
  img.onerror = () => img.remove();
  img.src = src;
  return img;
}

export const MobileRadio = {
  get el()      { return cur; },
  get active()  { return !!_station; },
  get station() { return _station; },
  get playing() { return !!_station && !cur.paused; },
  /** The station plays through the audio graph (the meters can read it). */
  get metered() { return !!_station && cur === elG; },
  /** What the station said it plays ({title, artist, song, cover}), or null:
   *  it has not said, or the phone streams it directly. */
  get now()     { return _station ? _now : null; },
  /** The two lines ({title, sub}) a surface shows for the station playing. */
  get lines()   { return _stationLines(_station, _station ? _now : null); },

  /** The library socket's ``radio_meta`` / ``radio_art`` (app.js): taken when
   *  it is of the station playing here through the relay. */
  noteMeta(m) { if (_mine(m) && _applyMeta(m)) _said(); },
  noteArt(m)  { if (_mine(m) && _applyArt(m)) _said(); },

  /** Ask what the station last said: it tells only at a change of song, so
   *  tuning in between two — or a socket that was away for one — hears
   *  nothing.  An answer with no title changes nothing, and one that a newer
   *  title or cover overtook is dropped. */
  async refreshNow() {
    const st = _station;
    if (!st || !st.sid || !_viaRelay) return;
    const seq = _nowSeq;
    let m = null;
    try {
      const r = await fetch(`/api/stations/now/${encodeURIComponent(st.sid)}`,
                            { credentials: 'same-origin' });
      m = r.ok ? await r.json() : null;
    } catch { return; }
    if (!m || _station !== st || !_viaRelay || seq !== _nowSeq) return;
    const a = _applyMeta(m), b = _applyArt(m);
    if (a || b) _said();
  },

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
    // Another station: what the earlier one played is not this one's.  The
    // same station tuned in again keeps it (its relay says the title anew).
    const relay = String(url || '').startsWith('/api/stations/relay/');
    if (!relay || !_station || _station.sid !== (station && station.sid)) _forgetNow();
    _viaRelay = relay;
    _owned = false;
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
      if (_pausedInSeq === seq) {
        _takeOver(station); emit('state');
        MobileRadio.refreshNow();  // (the relay may run already and know the song)
        return;
      }
      emit('state');
      throw e;                     // genuine failure — caller falls back to relay
    }
    // A newer play() or a stop() landed while we awaited a.play(); that call now
    // owns the element (playing its own stream) or silenced it — don't take over
    // on top of it, or radio would resurrect after the user picked something else.
    if (seq !== _playSeq) { emit('state'); return; }
    _takeOver(station);            // station is live → own the output + lock screen
    emit('state');
    MobileRadio.refreshNow();      // (not awaited: the song follows on its own)
  },

  /** Set by the radio view: called on a resume, it may tune the station in
   *  again instead (the meters were switched meanwhile) and say so (true). */
  onResume: null,

  toggle() {
    if (!_station) return;
    if (cur.paused) {
      if (MobileRadio.onResume && MobileRadio.onResume()) return;
      // (what the station plays may have changed while it was paused)
      cur.play().then(() => MobileRadio.refreshNow()).catch(() => {});
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
    _forgetNow();
    _viaRelay = false;
    _owned = false;
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
