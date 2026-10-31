// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * stations.js — internet-radio Stations view.  Lazy module: app.js
 * imports it on the first Stations sidebar click.
 *
 * Views: Favorites (server-side list, seeded with Nectarine), Scene (the
 * curated demoscene/chiptune pack) and World (Radio Browser directory:
 * continent → country → Top 10 / 11–50 / Remaining).
 *
 * Playback goes through ``Player.playStation`` against the server relay.
 * A station's quality ladder is reordered for THIS browser (canPlayType),
 * starts at the best supported stream and steps down when the stream
 * errors or keeps rebuffering.  When every stream fails the station is
 * reported as a temporary outage (no blacklisting — it stays listed so
 * the listener can retry).  Now-playing titles arrive over the library
 * WebSocket as ``radio_meta`` events (re-dispatched by app.js as
 * ``sb:radio-meta``).
 *
 * Layout (top to bottom): the location (breadcrumbs), in the shared UI style
 * (js/ui_style.js: Modern / Retro hi-fi); the now-playing card (station art + LIVE badge, the
 * song big with its artist, codec/bitrate/votes/plays-today, the signal meter,
 * the live scope, a play/pause ring, ★ favourite, stop, details); Retro hi-fi only: the
 * favourites as preset keys; the tuner dial (stations/dial.js — the shown
 * list's stations, a needle on the playing one); the list itself as a grid of
 * station cards (the playing one marked, with a mini live meter).  The scope,
 * mini meter and signal meter: stations/live.js.
 */
import { Player } from './player.js';
import { Toast } from './utils.js';
import { getUiStyle, onUiStyleChange } from './ui_style.js';
import { createDial } from './stations/dial.js';
import { createLive } from './stations/live.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Station favicon/homepage URLs come from a stranger-editable directory.
// esc() stops attribute breakout, but a ``javascript:`` href still executes
// on click (entities decode before URI evaluation), so the scheme must be
// whitelisted to http(s) before any URL reaches an href/src.
const _safeUrl = (u) => {
  const s = String(u == null ? '' : u).trim();
  return /^https?:\/\//i.test(s) ? s : '';
};

const view = () => $('stations-view');

// ── Playback state ────────────────────────────────────────────────────────────
let _current = null;     // { station, cands, i } while a station is selected
let _nowTitle = '';      // last ICY StreamTitle for the current station
let _artMeta = null;     // last radio_art result (cover + album/year/source) for _nowTitle
// Surf context: the station list (+ index) the playing station was launched
// from, so the player-bar ◄◄ / ►► can move through it.  Set by _stationRows
// when a row is clicked; realigned by _syncPlayCtx for non-list plays.
let _playCtx = { list: null, idx: -1 };

function _syncPlayCtx(station) {
  // Keep the surf index pointing at the now-playing station.  If it isn't in
  // the current list (played from Info/search), collapse to a single-item
  // context so surfing becomes a no-op rather than jumping to a wrong station.
  if (_playCtx.list && _playCtx.list[_playCtx.idx]?.sid === station.sid) return;
  const i = _playCtx.list ? _playCtx.list.findIndex((s) => s.sid === station.sid) : -1;
  if (i >= 0) _playCtx.idx = i;
  else _playCtx = { list: [station], idx: 0 };
}

// Station the ◄◄ (dir -1) / ►► (dir +1) button would surf to, or null at an end.
function _peekStation(dir) {
  if (!_playCtx.list) return null;
  return _playCtx.list[_playCtx.idx + dir] || null;
}

// Surf to the adjacent station in the current list.  No-op at the list ends.
function _surfStation(dir) {
  const next = _peekStation(dir);
  if (!next) return;
  _playCtx.idx += dir;
  play(next);
}

// Populate the radio-mode pieces of the player bar: the now-playing ticker,
// and the names of the stations ◄◄ / ►► will surf to (inline + as tooltips).
// The .radio-mode class itself is toggled by the player-bar renderer (app.js)
// off track.station, so this only fills content while a station is on air.
function _updateRadioBar() {
  const ticker = document.getElementById('radio-ticker');
  if (!ticker) return;
  const onAir = !!(_current && Player.stationMode);
  // Keep the radio-mode class in sync here too: stopStation() emits only
  // 'statechange' (no trackchange), so app.js's trackchange toggle never fires
  // on stop — without this the seek row would stay hidden after stopping.
  document.getElementById('player-bar')?.classList.toggle('radio-mode', onAir);
  const prevT = document.getElementById('radio-prev-target');
  const nextT = document.getElementById('radio-next-target');
  const btnPrev = document.getElementById('btn-prev');
  const btnNext = document.getElementById('btn-next');
  if (!onAir) {
    ticker.textContent = '';
    if (prevT) prevT.textContent = '';
    if (nextT) nextT.textContent = '';
    return;
  }
  // Ticker shows the raw "Artist - Song" StreamTitle, falling back to the
  // station name between songs / when no metadata is shared.
  ticker.textContent = _nowTitle || _current.station.name || 'Live';
  _fitTicker(ticker);
  const prev = _peekStation(-1);
  const next = _peekStation(1);
  if (prevT) prevT.textContent = prev ? prev.name : '';
  if (nextT) nextT.textContent = next ? next.name : '';
  if (btnPrev) btnPrev.title = prev ? `Previous station: ${prev.name}` : 'Start of list';
  if (btnNext) btnNext.title = next ? `Next station: ${next.name}` : 'End of list';
}

// Marquee the ticker only when its text actually overflows the wrapper — a
// measured shift (no magic numbers), recomputed each song.
function _fitTicker(el) {
  el.classList.remove('scroll');
  el.style.removeProperty('--ticker-shift');
  el.style.removeProperty('--ticker-dur');
  requestAnimationFrame(() => {
    const wrap = el.parentElement;
    if (!wrap || !el.isConnected) return;
    const overflow = el.scrollWidth - wrap.clientWidth;
    if (overflow > 8) {
      el.style.setProperty('--ticker-shift', `-${overflow + 12}px`);
      el.style.setProperty('--ticker-dur', `${Math.max(8, Math.round((overflow + 12) / 22))}s`);
      el.classList.add('scroll');
    }
  });
}

// ── Buffering / underrun tuning ────────────────────────────────────────────────
// A brief buffer dip recovers on its own; a SUSTAINED stall is handled.  With a
// lower-quality stream available we drop to it; with none we pause and rebuffer
// ourselves (a longer 2x buffer) so the browser can't skip to the live edge —
// which sounds like jumping to the next song.
const STALL_SUSTAINED_MS = 1200;   // a MID-STREAM 'waiting' lasting this long = a real underrun
const STARTUP_GRACE_MS   = 9000;   // a fresh stream gets this long to CONNECT before we downgrade
const REBUFFER_TARGET_S  = 6;      // "longer buffer (2x)": seconds buffered ahead before resuming
const REBUFFER_QUARTER_S = REBUFFER_TARGET_S / 4;   // worst-case floor — a quarter of the target
const REBUFFER_GIVEUP_MS = 8000;   // after this long without the full buffer, accept the quarter
const MAX_RECONNECTS     = 3;      // relay/upstream EOF: reconnect the same stream up to N times
const RECONNECT_BASE_MS  = 600;    // first 'ended' reconnect waits this long (then backs off)
const RECONNECT_MAX_MS   = 4000;   // backoff ceiling
const GOOD_PLAY_RESET_MS = 10000;  // sustained playback before the reconnect budget is forgiven
let _stallTimer     = null;
let _rebufferTimer  = null;        // the in-flight rebuffer poll (must be cancellable)
let _reconnectTimer = null;        // pending 'ended' reconnect (backoff; cancel on switch/stop)
let _goodPlayTimer  = null;        // fires after sustained play to forgive the reconnect budget
let _startupTimer   = null;        // fires if a fresh stream never starts within the grace window
let _hasPlayed      = false;       // has the CURRENT stream actually started playing yet?
let _activeUrl      = '';          // relay URL of the stream we're currently loading/playing
let _rebuffering    = false;
let _waiting        = false;       // the element is waiting for data right now (signal meter)
let _reconnects     = 0;
let _playGen        = 0;           // single-flight token: bumped on each (re)connect

function _resetBuffering() {
  clearTimeout(_stallTimer);
  clearInterval(_rebufferTimer);   // a station change must abort an in-flight rebuffer
  clearTimeout(_reconnectTimer);   // ...and any pending backoff reconnect
  clearTimeout(_goodPlayTimer);
  clearTimeout(_startupTimer);
  _stallTimer = null;
  _rebufferTimer = null;
  _reconnectTimer = null;
  _goodPlayTimer = null;
  _startupTimer = null;
  _rebuffering = false;
  _waiting = false;
  _hasPlayed = false;   // a (re)connect resets "has it played?"; set true on 'playing'
}

// Stream health for the signal meter (stations/live.js).
function _health() {
  const audio = Player.audio;
  return {
    on: !!_current,
    hasPlayed: _hasPlayed,
    rebuffering: _rebuffering,
    waiting: _waiting,
    ahead: audio ? _bufferedAhead(audio) : 0,
    downgrade: _current ? _current.i : 0,
  };
}

// ── Codec support → candidate order ──────────────────────────────────────────
const _probe = document.createElement('audio');

function _mimeFor(codec) {
  switch ((codec || '').toUpperCase()) {
    case 'MP3':  return 'audio/mpeg';
    case 'AAC': case 'AAC+': case 'AACP': return 'audio/aac';
    case 'OGG': case 'VORBIS': return 'audio/ogg; codecs="vorbis"';
    case 'OPUS': return 'audio/ogg; codecs="opus"';
    case 'FLAC': return 'audio/flac';
    default:     return '';
  }
}

// Streams reordered for this browser: supported codecs first (highest
// bitrate first within a tier), unknown codecs as a middle hail-mary,
// definitely-unsupported last.  ``v`` keeps the server-side index so the
// relay picks the same stream we chose.
function _candidates(station) {
  const scored = (station.streams || []).map((s, v) => {
    const mime = _mimeFor(s.codec);
    const support = !mime ? 1 : (_probe.canPlayType(mime) ? 2 : 0);
    return { v, s, support };
  });
  scored.sort((a, b) => (b.support - a.support) || ((b.s.bitrate || 0) - (a.s.bitrate || 0)));
  return scored;
}

// ── Playback ──────────────────────────────────────────────────────────────────

function play(station) {
  const cands = _candidates(station);
  if (!cands.length) {
    // No stream this browser can play — surface it as a temporary outage
    // (the directory entry may gain a playable mount later).
    _unavailable(station);
    return;
  }
  _wireAudioHealth();   // play() can be reached from global search, not just show()
  _syncPlayCtx(station);   // keep the ◄◄/►► surf context aligned to this station
  _current = { station, cands, i: 0 };
  _nowTitle = '';
  _artMeta = null;
  _resetBuffering();
  _reconnects = 0;
  _tryPlay();
  _updateRadioBar();    // enter / refresh the radio-mode player bar
  _syncUi();            // the dial's needle, the playing card, the presets
}

async function _tryPlay() {
  if (!_current) return;
  _resetBuffering();   // (re)starting a stream — drop any pending stall/rebuffer (resets _hasPlayed)
  const gen = ++_playGen;   // single-flight: a newer (re)connect invalidates this one
  _activeUrl = '';
  // Give the fresh stream time to actually CONNECT before we consider a
  // downgrade.  Connecting to an upstream relay routinely fires 'waiting' for a
  // second or two before the first audio arrives — that is NOT an underrun, and
  // treating it as one made stations cascade down the whole quality ladder at
  // startup.  Only if the stream never starts within the grace window do we act.
  _startupTimer = setTimeout(() => {
    if (gen === _playGen && Player.stationMode && _current && !_hasPlayed && !_rebuffering) {
      _onUnderrun();
    }
  }, STARTUP_GRACE_MS);
  const { station, cands, i } = _current;
  const c = cands[i];
  // WebKit gives the audio graph (the live view, the equalizer) samples of a
  // file but not of an endless stream: the relay answers it as one (file=1).
  const url = `/api/stations/relay/${encodeURIComponent(station.sid)}?v=${c.v}`
    + (/Apple/.test(navigator.vendor || '') ? '&file=1' : '');
  _activeUrl = url;   // so the 'error' handler can ignore late errors from a superseded stream
  _renderNowPlaying(true);
  try {
    await Player.playStation(station, url, c.s.codec);
    if (gen !== _playGen) return;   // superseded mid-load (user switched / reconnected)
    _renderNowPlaying();
  } catch (_) {
    if (gen !== _playGen) return;   // superseded — don't ladder-walk a stale stream
    // play() rejected.  A genuine load failure ALSO fires the media 'error'
    // event (handled there → _onUnderrun); a transient AbortError from swapping
    // src does not, and the stream often still comes up.  So do NOT downgrade
    // here — the 'error' handler covers real failures and the startup-grace
    // timer is the backstop if the stream never starts.
    _renderNowPlaying();
  }
}

function _qualityLabel(c) {
  if (!c) return '';
  return `${c.s.codec || '?'}${c.s.bitrate ? ` ${c.s.bitrate}k` : ''}`;
}

function _stepDown() {
  if (!_current) return;
  if (_current.i + 1 < _current.cands.length) {
    _current.i++;
    Toast?.info?.(`Stream trouble — switching to ${_qualityLabel(_current.cands[_current.i])}.`);
    _tryPlay();
  } else {
    _unavailable(_current.station);
  }
}

// A station that won't play right now is treated as a TEMPORARY outage —
// the listener can try again later.  We don't blacklist it or remove it
// from the list, so it reappears and stays playable next time the relay
// or the station itself recovers.
function _unavailable(station) {
  Toast?.error?.(`${station.name} is unavailable right now — it may be temporary. Try again later.`);
  _resetBuffering();
  Player.stopStation();
  _current = null;
  _artMeta = null;
  _renderNowPlaying();
  _updateRadioBar();    // (the ticker / surf targets leave with the station)
  _syncUi();
}

function stop() {
  _resetBuffering();
  Player.stopStation();
  _current = null;
  _nowTitle = '';
  _artMeta = null;
  _playCtx = { list: null, idx: -1 };
  _renderNowPlaying();
  _updateRadioBar();    // clear the radio-mode ticker + surf targets
  _syncUi();
}

// Seconds of CONTIGUOUS audio buffered ahead of the playhead (0 when nothing is
// buffered there).  We measure the range that actually covers currentTime, not
// the end of the last range — a gap ahead must not count as "buffered" or we'd
// resume into silence.
function _bufferedAhead(audio) {
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

// Show a transient status in the now-playing card's title (e.g. "Buffering…"),
// or restore the live ICY title when ``msg`` is null.
function _setRebufferStatus(msg) {
  _updateNowTitle(msg || null);
}

// A sustained underrun on a station with NO lower-quality fallback: pause and
// fill a longer buffer ourselves, then resume from where we stalled.  This
// keeps the browser from skipping forward to the live edge (which sounds like
// jumping to the next song).  The buffer target is 2x a nominal dip; if even a
// long wait can't fill it, we resume on a quarter of it so the listener isn't
// stranded on silence (they'll switch stations if it stays bad).
function _rebuffer() {
  const audio = Player.audio;
  if (!audio || _rebuffering) return;
  _rebuffering = true;
  try { audio.pause(); } catch (_) {}
  _setRebufferStatus('Buffering…');
  const started = performance.now();
  clearInterval(_rebufferTimer);   // never run two rebuffer polls at once
  _rebufferTimer = setInterval(() => {
    // Station stopped or swapped out from under us — abort, don't touch the new audio.
    if (!Player.stationMode || !_current) { clearInterval(_rebufferTimer); _rebufferTimer = null; _rebuffering = false; return; }
    const ahead   = _bufferedAhead(audio);
    const waited   = performance.now() - started;
    const full    = ahead >= REBUFFER_TARGET_S || audio.readyState >= 4;   // HAVE_ENOUGH_DATA
    const quarter = waited >= REBUFFER_GIVEUP_MS && ahead >= REBUFFER_QUARTER_S;
    if (full || quarter) {
      clearInterval(_rebufferTimer);
      _rebufferTimer = null;
      _rebuffering = false;
      _setRebufferStatus(null);
      audio.play().catch(() => {});
    }
  }, 250);
}

// A real underrun fired (sustained 'waiting', a hard media error, or a stream
// that never started within the startup grace).  Prefer a lower-quality stream
// — a 64k that plays beats a 320k that gaps.  With no lower quality: if we WERE
// playing, rebuffer in place; if we never started at all, the stream is dead —
// surface it as unavailable rather than spinning on "Buffering…" forever.
function _onUnderrun() {
  if (!Player.stationMode || !_current || _rebuffering) return;
  if (_current.i + 1 < _current.cands.length) {
    _stepDown();
  } else if (_hasPlayed) {
    _rebuffer();
  } else {
    _unavailable(_current.station);
  }
}

// Stream health for stations.  A brief buffer dip recovers on its own
// ('playing' clears the stall timer); only a sustained stall is acted on.
function _wireAudioHealth() {
  const audio = Player.audio;
  if (!audio || audio.__sbStationsWired) return;
  audio.__sbStationsWired = true;

  // Hard media error: the stream/relay died — step down or surface an outage.
  // MEDIA_ERR_ABORTED (code 1) is what the element reports when WE swap src to
  // switch/stop a station — it is NOT an underrun.  Treating it as one made
  // every station switch self-trigger _onUnderrun → _stepDown → a fresh relay
  // fetch, cascading down the candidate ladder and storming the relay.
  audio.addEventListener('error', () => {
    const e = audio.error;
    if (!e || e.code === e.MEDIA_ERR_ABORTED /* 1 */) return;
    // Ignore a late error from a stream we already swapped away from — acting on
    // it would fire _onUnderrun against the NEW candidate.  currentSrc still
    // names the resource that errored; only act if it's the one we're on.
    if (_activeUrl && audio.currentSrc && !audio.currentSrc.endsWith(_activeUrl)) return;
    if (Player.stationMode && _current) _onUnderrun();
  });

  // EOF: the relay/upstream dropped the connection.  Reconnect the SAME stream
  // a few times (with backoff) before downgrading / giving up — never advance
  // the queue.  The reconnect is deferred + cancellable so a station switch or
  // stop aborts it (no runaway, no stale reconnect onto the new station).
  audio.addEventListener('ended', () => {
    if (!Player.stationMode || !_current) return;
    if (_reconnects >= MAX_RECONNECTS) {
      _reconnects = 0;
      if (_current.i + 1 < _current.cands.length) _stepDown();
      else _unavailable(_current.station);
      return;
    }
    _reconnects++;
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** (_reconnects - 1), RECONNECT_MAX_MS);
    clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(() => {
      if (Player.stationMode && _current) _tryPlay();
    }, delay);
  });

  // MID-STREAM buffer underrun: only act if still stalled after a beat (brief
  // dips recover).  Ignored until the stream has actually played — a 'waiting'
  // before the first 'playing' is just startup buffering, handled by the
  // startup-grace timer, NOT a reason to downgrade.
  audio.addEventListener('waiting', () => {
    if (Player.stationMode && _current) _waiting = true;
    if (!Player.stationMode || !_current || _rebuffering || !_hasPlayed) return;
    clearTimeout(_stallTimer);
    _stallTimer = setTimeout(_onUnderrun, STALL_SUSTAINED_MS);
  });

  // Playback actually started (or recovered).  Mark the stream as live, cancel
  // the startup-grace + stall timers, and forgive the reconnect budget ONLY
  // after sustained healthy playback (a stream that plays a fraction of a
  // second before dropping must NOT keep zeroing the counter, or it would
  // reconnect forever and never give up).
  // The rebuffer pauses / resumes the element itself (no Player statechange):
  // the ring and the live loop follow the element.
  audio.addEventListener('pause', () => { if (_current) { _renderRing(); _live?.evaluate(); } });
  audio.addEventListener('playing', () => {
    if (_current) { _renderRing(); _live?.evaluate(); }
    _hasPlayed = true;
    _waiting = false;
    clearTimeout(_startupTimer);
    _startupTimer = null;
    clearTimeout(_stallTimer);
    _stallTimer = null;
    clearTimeout(_goodPlayTimer);
    _goodPlayTimer = setTimeout(() => { _reconnects = 0; }, GOOD_PLAY_RESET_MS);
  });
}

// ── Favorites ─────────────────────────────────────────────────────────────────

async function _toggleFavorite(station, btn) {
  const makeFav = !station.favorite;
  try {
    const r = makeFav
      ? await fetch('/api/stations/favorites', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(station),
      })
      : await fetch(`/api/stations/favorites/${encodeURIComponent(station.sid)}`, { method: 'DELETE' });
    if (!r.ok) {
      let msg = 'Could not update favorites.';
      try { msg = (await r.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    station.favorite = makeFav;
    if (_current && _current.station.sid === station.sid) _current.station.favorite = makeFav;
    const favBtn = $('st-fav-btn');
    for (const b of [btn, _current && _current.station.sid === station.sid ? favBtn : null]) {
      if (!b) continue;
      b.classList.toggle('on', makeFav);
      if (b === favBtn) b.setAttribute('aria-pressed', String(makeFav));
      b.title = makeFav ? 'Remove from favorites' : 'Add to favorites';
    }
    _presets = null;                                  // the preset keys re-read the list
    if (getUiStyle() === 'retro' && _sview !== 'favorites') _loadPresets();
    (Toast?.ok || Toast?.info)?.(makeFav
      ? `${station.name} added to favorites.`
      : `${station.name} removed from favorites.`);
    if (_sview === 'favorites') _renderFavorites();   // list reflects the change
  } catch (e) {
    Toast?.error?.((e && e.message) || 'Could not update favorites.');
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

let _sview = null;       // 'favorites' | 'scene' | 'world' | 'search'
// Monotonic render token — bumped on every view switch (_enterView).  Each
// async list renderer captures it and re-checks after its await, so a slow
// response (Radio Browser can lag seconds) can't paint into a view the user
// already navigated away from — content + crumb would otherwise disagree.
let _renderGen = 0;

// The view's parts, made once (_shell): the dial and the live feedback live
// as long as the page.
let _dial = null;
let _live = null;
let _dialList = null;        // the station list on the dial (the one shown, else the playing one)
let _dialLabel = '';
let _presets = null;         // Retro: the favourites on the preset keys (null = not loaded)
let _presetsGen = 0;

const GLYPH = '<span class="st-glyph" aria-hidden="true">📻</span>';
const STAR = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8l2.84 5.76 6.36.92-4.6 4.48 1.09 6.33L12 17.3l-5.69 2.99 1.09-6.33-4.6-4.48 6.36-.92z"/></svg>';
// The Retro signal meter's printed face: ticks −50°…+50° around the needle's pivot.
const SIG_FACE = '<svg class="st-sig-face" viewBox="0 0 116 62" aria-hidden="true"><path class="st-sig-arc" d="M13.6 34.7 A58 58 0 0 1 102.4 34.7"/><path class="st-sig-red" d="M15.9 36.6 A55 55 0 0 1 30.5 24.4"/><g class="st-sig-ticks"><line x1="19.7" y1="39.9" x2="13.6" y2="34.7"/><line x1="23.9" y1="31.4" x2="20.7" y2="27.6"/><line x1="31.5" y1="26.1" x2="29.0" y2="21.8"/><line x1="39.9" y1="22.2" x2="38.2" y2="17.5"/><line x1="48.8" y1="19.8" x2="47.9" y2="14.9"/><line x1="58.0" y1="22.0" x2="58.0" y2="14.0"/><line x1="67.2" y1="19.8" x2="68.1" y2="14.9"/><line x1="76.1" y1="22.2" x2="77.8" y2="17.5"/><line x1="84.5" y1="26.1" x2="87.0" y2="21.8"/><line x1="92.1" y1="31.4" x2="95.3" y2="27.6"/><line x1="96.3" y1="39.9" x2="102.4" y2="34.7"/></g><text x="12" y="30">0</text><text x="98" y="30">5</text></svg>';
const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.2v13.6L19 12z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h3.6v14H7zM13.4 5H17v14h-3.6z"/></svg>';

function _shell() {
  const v = view();
  if (!v.__sbShell) {
    v.innerHTML = `
      <div class="st-top">
        <div id="st-crumbs"></div>
      </div>
      <section id="st-now" class="st-now" aria-label="Now playing"></section>
      <div id="st-presets" class="st-presets" role="group" aria-label="Favorite presets" hidden></div>
      <div id="st-dial" class="st-dial"></div>
      <div id="st-body"></div>`;
    v.__sbShell = true;
    // Space / Enter on the view's buttons press the button — keep them from
    // the app-wide Space (play / pause) shortcut, as the EQ panel does.
    v.addEventListener('keydown', (e) => {
      const t = e.target;
      if (t && t.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) e.stopPropagation();
    });
    _dial = createDial($('st-dial'), {
      onTune: (station, idx, list) => { _playCtx = { list, idx, label: _dialLabel }; play(station); },
    });
    _live = createLive({ player: Player, view: v, health: _health });
    onUiStyleChange(_onUiStyle);
    _onUiStyle(getUiStyle());
  }
  return { now: $('st-now'), crumbs: $('st-crumbs'), body: $('st-body') };
}

function _onUiStyle(style) {
  if (!_dial) return;
  _dial.layout();                 // the two looks space the dial differently
  if (style === 'retro') _loadPresets();
  else { const p = $('st-presets'); if (p) p.hidden = true; }
  _live?.bind();
}

// Show list ``stations`` on the dial (a list renderer calls this with the list
// it shows; a page without stations keeps the playing station's list).
function _setDial(stations, label) {
  _dialList = stations;
  _dialLabel = label;
  _dial?.setStations(stations || [], label);
  _dial?.setCurrent(_current ? _current.station.sid : null);
  if (!_current) _renderIdleRing();
}
// Idle card: the round button plays the dial's station (the list may arrive
// after the card was drawn).
function _renderIdleRing() {
  const b = $('st-ring');
  if (!b || _current) return;
  const list = _dialList || [];
  const st = list[Math.max(0, _dial ? _dial.selected : 0)] || list[0];
  b.disabled = !st;
  b.setAttribute('aria-label', st ? `Play ${st.name}` : 'Play');
  b.title = st ? `Play ${st.name}` : 'Pick a station to play';
}
function _dialFallback() {
  // A page with no stations of its own (World continents / countries): the
  // dial shows the list the playing station came from.
  if (_current && _playCtx.list && _playCtx.list.length > 1) _setDial(_playCtx.list, _playCtx.label || 'Playing list');
  else _setDial([], '');
}

const _fmtCount = (n) => (n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

function _metaChips(station, cand) {
  const chips = [];
  if (cand) chips.push(`<span class="st-chip st-chip-q">${esc(_qualityLabel(cand))}</span>`);
  if (station.votes) chips.push(`<span class="st-chip" title="Radio Browser votes">▲ ${esc(_fmtCount(+station.votes))}</span>`);
  if (station.clicks) chips.push(`<span class="st-chip" title="Radio Browser: plays in the last 24 hours">${esc(_fmtCount(+station.clicks))} plays today</span>`);
  if (station.country) chips.push(`<span class="st-chip">${esc(_flag(station.country))} ${esc(station.country)}</span>`);
  return chips.join('');
}

function _artHtml(url, cls) {
  const u = _safeUrl(url);
  return u ? `<img class="${cls}" src="${esc(u)}" alt="" decoding="async">` : `<span class="${cls} st-art-none">${GLYPH}</span>`;
}
function _artFallback(root, sel, cls) {
  const img = root.querySelector(sel);
  if (img) img.onerror = () => { img.outerHTML = `<span class="${cls} st-art-none">${GLYPH}</span>`; };
}

function _renderNowPlaying(connecting = false) {
  const el = $('st-now');
  if (!el) return;
  el.classList.remove('hidden');
  const signal = `
    <div class="st-signal" role="meter" aria-label="Signal" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" data-level="0">
      <span class="st-sig-bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span>
      <span class="st-sig-meter" aria-hidden="true">${SIG_FACE}<span class="st-sig-needle"></span><span class="st-sig-cap">Signal</span></span>
    </div>`;
  if (!_current) {
    // Keep the card in the layout (don't collapse it) so starting a station
    // doesn't shove the whole list down under the cursor — an easy way to
    // double-click the wrong station.  Show a muted placeholder instead.
    el.classList.add('st-now-empty');
    el.dataset.state = 'idle';
    el.innerHTML = `
      <div class="st-np-art"><span class="st-np-img st-art-none">${GLYPH}</span></div>
      <div class="st-np-main">
        <div class="st-np-head"><span class="st-np-station">No station playing</span></div>
        <div class="st-now-title">Pick a station below or turn the dial</div>
        <div class="st-np-meta">${signal}</div>
        <canvas class="st-scope" aria-hidden="true"></canvas>
      </div>
      <div class="st-np-side">
        <div class="st-signal-side">${signal}</div>
        <button type="button" id="st-ring" class="st-ring">${ICON_PLAY}</button>
      </div>`;
    _renderIdleRing();
    $('st-ring')?.addEventListener('click', () => {
      const list = _dialList || [];
      const at = Math.max(0, _dial ? _dial.selected : 0);
      const st = list[at] || list[0];
      if (st) { _playCtx = { list, idx: list.indexOf(st), label: _dialLabel }; play(st); }
    });
    _live?.bind();
    return;
  }
  const { station, cands, i } = _current;
  el.classList.remove('st-now-empty');
  el.dataset.state = connecting ? 'connecting' : (Player.playing ? 'live' : 'paused');
  const home = _safeUrl(station.homepage);
  const at = _dial && _dialList ? _dialList.findIndex((s) => s.sid === station.sid) : -1;
  const freq = at >= 0 && _dialList.length > 1 ? (87.5 + (20.5 * at) / (_dialList.length - 1)).toFixed(1)
             : at >= 0 ? '98.0' : '';
  el.innerHTML = `
    <div class="st-np-art">
      ${_artHtml(station.favicon, 'st-np-img')}
      <span class="st-live-badge" aria-hidden="true">LIVE</span>
    </div>
    <div class="st-np-main">
      <div class="st-np-head">
        <span class="st-np-station">${esc(station.name)}</span>
        ${freq ? `<span class="st-np-freq" aria-hidden="true">${freq} MHz</span>` : ''}
        <span class="st-np-lamps" aria-hidden="true"><span class="st-lamp st-lamp-tuned">Tuned</span><span class="st-lamp st-lamp-stereo">Stereo</span></span>
      </div>
      <div class="st-now-title" aria-live="polite"></div>
      <div class="st-np-artist"></div>
      <div class="st-np-extra"></div>
      <div class="st-np-meta">${_metaChips(station, cands[i])}${home
        ? `<a class="st-chip st-chip-link" href="${esc(home)}" target="_blank" rel="noopener">Website</a>` : ''}${signal}</div>
      <canvas class="st-scope" aria-hidden="true"></canvas>
    </div>
    <div class="st-np-side">
      <div class="st-signal-side">${signal}</div>
      <button type="button" id="st-ring" class="st-ring"></button>
      <div class="st-np-btns">
        <button type="button" id="st-fav-btn" class="st-btn st-star ${station.favorite ? 'on' : ''}"
                aria-pressed="${station.favorite ? 'true' : 'false'}"
                aria-label="Favorite" title="${station.favorite ? 'Remove from favorites' : 'Add to favorites'}">${STAR}</button>
        <button type="button" id="st-stop-btn" class="st-btn" aria-label="Stop station" title="Stop station">⏹</button>
        <button type="button" id="st-info-btn" class="st-btn" aria-label="Station details" title="Station details">ⓘ</button>
      </div>
    </div>`;
  _artFallback(el, 'img.st-np-img', 'st-np-img');
  _updateNowTitle(connecting ? 'Connecting…' : null);
  _renderRing();
  $('st-ring')?.addEventListener('click', () => { Player.playPause(); });
  $('st-fav-btn')?.addEventListener('click', () => _toggleFavorite(station, $('st-fav-btn')));
  $('st-stop-btn')?.addEventListener('click', stop);
  $('st-info-btn')?.addEventListener('click', () => showInfo(station));
  _live?.bind();
}

function _renderRing() {
  const b = $('st-ring');
  const el = $('st-now');
  if (!b || !_current) return;
  const playing = Player.playing && Player.stationMode;
  b.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  b.setAttribute('aria-label', playing ? 'Pause' : 'Play');
  b.title = playing ? 'Pause' : 'Play';
  b.classList.toggle('on', playing);
  if (el && el.dataset.state !== 'connecting') el.dataset.state = playing ? 'live' : 'paused';
}

/** The card's song lines: ``status`` (Buffering… / Connecting…) or the live song. */
function _updateNowTitle(status = null) {
  const v = view();
  const t = v?.querySelector('.st-now-title');
  if (!t || !_current) return;
  const tr = Player.currentTrack;
  const song = (tr && tr.station && tr.song) || '';
  const artist = (tr && tr.station && tr.songArtist) || '';
  t.textContent = status || song || _nowTitle || 'Live';
  const a = v.querySelector('.st-np-artist');
  if (a) a.textContent = status ? '' : (song ? artist : '');
  const x = v.querySelector('.st-np-extra');
  if (x) {
    let extra = '';
    if (_artMeta && !status) {
      const album = [_artMeta.album, _artMeta.year].filter(Boolean).join(' · ');
      extra = album ? `From ${album}` : '';
    }
    x.textContent = extra;
  }
  const el = $('st-now');
  if (el && !status && el.dataset.state === 'connecting') { el.dataset.state = Player.playing ? 'live' : 'paused'; }
  if (el && status === 'Connecting…') el.dataset.state = 'connecting';
}

/** Mark the playing station everywhere: grid card, dial needle, presets, ring. */
function _syncUi() {
  const sid = _current ? _current.station.sid : null;
  const body = $('st-body');
  if (body) {
    for (const c of body.querySelectorAll('.st-card')) {
      const on = c.dataset.sid === sid;
      c.classList.toggle('playing', on);
      if (on) c.setAttribute('aria-current', 'true'); else c.removeAttribute('aria-current');
    }
  }
  // The dial keeps the SHOWN list; a station from elsewhere (a preset, search,
  // Info) leaves its needle parked.
  _dial?.setCurrent(sid);
  _renderPresets();
  _renderRing();
  _live?.bind();
}

// ── Retro hi-fi: favourites on preset keys ──────────────────────────────────
const PRESET_KEYS = 8;
async function _loadPresets() {
  const host = $('st-presets');
  if (!host) return;
  const gen = ++_presetsGen;
  if (_presets === null) {
    try {
      const favs = await _fetchJson('/api/stations/favorites');
      if (gen !== _presetsGen) return;
      _presets = Array.isArray(favs) ? favs : [];
    } catch (_) { _presets = []; }
  }
  _renderPresets();
}
function _renderPresets() {
  const host = $('st-presets');
  if (!host) return;
  if (getUiStyle() !== 'retro' || _presets === null) { host.hidden = true; return; }
  host.hidden = false;
  const sid = _current ? _current.station.sid : null;
  const favs = _presets.slice(0, PRESET_KEYS);
  let html = '<span class="st-presets-cap" aria-hidden="true">Presets</span>';
  for (let k = 0; k < PRESET_KEYS; k++) {
    const st = favs[k];
    if (st) {
      html += `<button type="button" class="st-preset${st.sid === sid ? ' on' : ''}" data-k="${k}"
                 ${st.sid === sid ? 'aria-current="true"' : ''} aria-label="Preset ${k + 1}: ${esc(st.name)}"
                 title="${esc(st.name)}"><span class="st-preset-n">${k + 1}</span><span class="st-preset-name">${esc(st.name)}</span></button>`;
    } else {
      html += `<button type="button" class="st-preset st-preset-empty" disabled aria-label="Preset ${k + 1}: empty — add a favorite with ★"
                 title="Empty — add a favorite with ★"><span class="st-preset-n">${k + 1}</span><span class="st-preset-name">—</span></button>`;
    }
  }
  host.innerHTML = html;
  host.querySelectorAll('button.st-preset[data-k]').forEach((b) => {
    b.addEventListener('click', () => {
      const k = +b.dataset.k;
      _playCtx = { list: favs, idx: k, label: 'Favorites' };
      play(favs[k]);
    });
  });
}

function _crumbsHtml(parts) {
  // parts: [{label, fn}] — last one is the current page (no link)
  return parts.map((p, i) => i === parts.length - 1
    ? `<span class="st-crumb-here">${esc(p.label)}</span>`
    : `<a class="st-crumb" data-i="${i}">${esc(p.label)}</a>`,
  ).join(' <span class="st-crumb-sep">›</span> ');
}

function _setCrumbs(parts) {
  const el = $('st-crumbs');
  el.innerHTML = _crumbsHtml(parts);
  el.querySelectorAll('a.st-crumb').forEach((a) => {
    a.addEventListener('click', () => parts[+a.dataset.i].fn());
  });
}

function _loading(msg = 'Loading…') {
  // Default load → shimmer skeleton rows (perceived-perf 2.8); explicit
  // error/status messages still render as plain text.
  if (msg === 'Loading…') {
    const row = '<div class="st-skel-row"><span class="st-skel-art skel-bar"></span>'
              + '<span class="st-skel-line skel-bar"></span></div>';
    $('st-body').innerHTML = `<div class="st-skel" aria-hidden="true">${row.repeat(8)}</div>`;
    return;
  }
  $('st-body').innerHTML = `<div class="st-empty">${esc(msg)}</div>`;
  _dialFallback();          // (a list that failed to load leaves no stations on the dial)
}

// Hover-intent station warm: after the pointer rests on a row briefly (or the
// row takes keyboard focus), ask the server to pre-resolve the station + its
// stream host DNS so a click starts the relay from a warm resolver.  Deduped
// per session; the pointer delay keeps a fast scroll-past from firing.  The
// server does only the cheap, side-effect-free half — no play-click report, no
// relay spawn — see api/stations.py warm().
const _warmed = new Set();
const WARM_INTENT_MS = 140;
function _warmStation(station) {
  const sid = station && station.sid;
  if (!sid || _warmed.has(sid)) return;
  _warmed.add(sid);
  // Warm the SAME stream the click will pick — _candidates() reorders by
  // browser-playability then bitrate, and play() starts at index 0 (?v=cands[0].v).
  // Warming streams[0] instead would DNS-prime a different host on multi-stream
  // stations and waste the warm, so pass the chosen v.
  const cands = _candidates(station);
  const v = cands.length ? cands[0].v : 0;
  try {
    fetch(`/api/stations/warm/${encodeURIComponent(sid)}?v=${v}`, { priority: 'low' })
      .catch(() => {});
  } catch { /* ignore */ }
}

function _stationRows(body, stations, { showCountry = false, label = '' } = {}) {
  _setDial(stations, label);
  if (!stations.length) {
    body.innerHTML = '<div class="st-empty">Nothing here yet.</div>';
    return;
  }
  body.innerHTML = '';
  const grid = document.createElement('div');
  grid.className = 'st-grid';
  const playingSid = _current ? _current.station.sid : null;
  stations.forEach((st, i) => {
    const card = document.createElement('div');
    card.className = 'st-card' + (st.sid === playingSid ? ' playing' : '');
    if (st.sid === playingSid) card.setAttribute('aria-current', 'true');
    card.dataset.sid = st.sid;
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    const best = (st.streams || [])[0];
    const nQual = (st.streams || []).length;
    const genre = String(st.tags || '').split(',').map((t) => t.trim()).filter(Boolean).slice(0, 2).join(', ');
    const qual = best ? `${best.codec || ''}${best.bitrate ? ` ${best.bitrate}k` : ''}`.trim() : '';
    const meta = [qual, nQual > 1 ? `${nQual} streams` : '', showCountry && st.country ? st.country : '',
                  st.votes ? `▲${st.votes}` : ''].filter(Boolean).join(' · ');
    card.innerHTML = `
      <span class="st-card-art">${_safeUrl(st.favicon)
        ? `<img src="${esc(_safeUrl(st.favicon))}" alt="" loading="lazy" decoding="async">` : GLYPH}</span>
      <span class="st-card-body">
        <span class="st-card-name">${st.favorite ? '<span class="st-card-fav" aria-label="Favorite">★</span> ' : ''}${esc(st.name)}</span>
        ${genre ? `<span class="st-card-genre">${esc(genre)}</span>` : ''}
        <span class="st-card-meta">${esc(meta)}</span>
      </span>
      <span class="st-card-meter" aria-hidden="true"><i></i><i></i><i></i><i></i></span>`;
    const img = card.querySelector('.st-card-art img');
    if (img) img.onerror = () => { img.outerHTML = GLYPH; };
    const go = () => { _playCtx = { list: stations, idx: i, label }; play(st); };
    card.addEventListener('click', go);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
    // Hover-intent + keyboard-focus warm.  Pointer waits out a short intent
    // delay (cancelled on leave) so scrolling past a card doesn't warm it;
    // focus fires immediately (a deliberate keyboard landing).
    let _warmTimer = null;
    card.addEventListener('pointerenter', () => {
      _warmTimer = setTimeout(() => _warmStation(st), WARM_INTENT_MS);
    });
    card.addEventListener('pointerleave', () => {
      if (_warmTimer) { clearTimeout(_warmTimer); _warmTimer = null; }
    });
    card.addEventListener('focus', () => _warmStation(st));
    grid.appendChild(card);
  });
  body.appendChild(grid);
  _live?.bind();
}

async function _fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

async function _renderFavorites() {
  const gen = _renderGen;
  _setCrumbs([{ label: 'Favorites' }]);
  _loading();
  try {
    const favs = await _fetchJson('/api/stations/favorites');
    if (gen !== _renderGen) return;
    _presets = favs;              // (Retro's preset keys show the same list)
    _presetsGen++;                // (an older preset fetch must not overwrite it)
    _renderPresets();
    if (!favs.length) {
      _setDial([], 'Favorites');
      $('st-body').innerHTML =
        '<div class="st-empty">No favorite stations yet — play one and hit ★.</div>';
      return;
    }
    _stationRows($('st-body'), favs, { label: 'Favorites' });
  } catch (e) { _loading(`Could not load favorites: ${e.message}`); }
}

async function _renderScene() {
  const gen = _renderGen;
  _setCrumbs([{ label: 'Scene' }]);
  _loading();
  try {
    const scene = await _fetchJson('/api/stations/scene');
    if (gen !== _renderGen) return;
    _stationRows($('st-body'), scene, { label: 'Scene' });
  } catch (e) { _loading(`Could not load scene stations: ${e.message}`); }
}

async function _renderWorld() {
  const gen = _renderGen;
  _setCrumbs([{ label: 'World' }]);
  _dialFallback();
  _loading();
  let continents;
  try {
    continents = await _fetchJson('/api/stations/world');
  } catch (e) { _loading(`Radio directory unavailable: ${e.message}`); return; }
  if (gen !== _renderGen) return;
  const body = $('st-body');
  body.innerHTML = '';
  continents.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'st-row st-row-group';
    row.tabIndex = 0;
    row.innerHTML = `<span class="st-row-art">🌍</span>
      <span class="st-row-name">${esc(c.continent)}</span>
      <span class="st-row-meta">${c.countries.length} countries</span>`;
    row.addEventListener('click', () => _renderContinent(c));
    body.appendChild(row);
  });
}

function _renderContinent(cont) {
  _setCrumbs([{ label: 'World', fn: _renderWorld }, { label: cont.continent }]);
  _dialFallback();
  const body = $('st-body');
  body.innerHTML = '';
  cont.countries.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'st-row st-row-group';
    row.tabIndex = 0;
    row.innerHTML = `<span class="st-row-art">${esc(_flag(c.code))}</span>
      <span class="st-row-name">${esc(c.name)}</span>
      <span class="st-row-meta">${c.count} stations</span>`;
    row.addEventListener('click', () => _renderCountry(cont, c));
    body.appendChild(row);
  });
}

function _flag(code) {
  // Regional-indicator emoji from the ISO code; falls back to a globe.
  try {
    const cc = (code || '').toUpperCase();
    if (!/^[A-Z]{2}$/.test(cc)) return '🌐';
    return String.fromCodePoint(...[...cc].map((ch) => 0x1F1E6 + ch.charCodeAt(0) - 65));
  } catch (_) { return '🌐'; }
}

const _BUCKETS = [
  ['top10', 'Top 10'],
  ['top50', 'Top 11–50'],
  ['rest', 'Remaining'],
];

async function _renderCountry(cont, country, bucket = 'top10') {
  const gen = _renderGen;   // top-level view-switch guard (matches the sibling renderers)
  _setCrumbs([
    { label: 'World', fn: _renderWorld },
    { label: cont.continent, fn: () => _renderContinent(cont) },
    { label: country.name },
  ]);
  _dialFallback();          // (until this bucket's stations arrive)
  const body = $('st-body');
  body.innerHTML = `<div class="st-tabs">${_BUCKETS.map(([k, lbl]) =>
    `<button class="st-tab ${k === bucket ? 'on' : ''}" data-b="${k}">${lbl}</button>`).join('')}
    </div><div class="st-tab-body"><div class="st-empty">Loading…</div></div>`;
  // Capture THIS render's tab-body: a rapid bucket switch (or country re-select)
  // replaces it, so a late fetch must not fill a tab-body that's been detached.
  const tb = body.querySelector('.st-tab-body');
  body.querySelectorAll('.st-tab').forEach((b) => {
    b.addEventListener('click', () => _renderCountry(cont, country, b.dataset.b));
  });
  try {
    const stations = await _fetchJson(
      `/api/stations/country/${encodeURIComponent(country.code)}?bucket=${bucket}`);
    if (gen !== _renderGen || !tb.isConnected) return;   // superseded by a newer view / bucket
    _stationRows(tb, stations,
      { label: `${country.name} · ${(_BUCKETS.find((b) => b[0] === bucket) || [, ''])[1]}` });
  } catch (e) {
    if (gen !== _renderGen || !tb.isConnected) return;
    tb.innerHTML = `<div class="st-empty">Could not load: ${esc(e.message)}</div>`;
    _dialFallback();
  }
}

// ── View switching ────────────────────────────────────────────────────────────

// ── Station info modal (the player (i) button while a station plays) ──────────

function showInfo(station) {
  station = station || (_current && _current.station);
  if (!station) return;
  let ov = $('st-info-overlay');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'st-info-overlay';
    ov.addEventListener('click', (e) => { if (e.target === ov) _closeInfo(); });
    document.body.appendChild(ov);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && $('st-info-overlay')?.classList.contains('open')) _closeInfo();
    });
  }
  const isCurrent = !!(_current && _current.station.sid === station.sid);
  const cands = isCurrent ? _current.cands : _candidates(station);
  const playingIdx = isCurrent ? _current.i : -1;
  const favUrl = _safeUrl(station.favicon);
  const art = favUrl
    ? `<img class="st-info-art" src="${esc(favUrl)}" alt="" decoding="async">`
    : '<span class="st-info-art st-info-glyph">📻</span>';
  const streams = (station.streams || []).map((s, i) => {
    const v = cands.find((c) => c.v === i);   // map back to candidate for the playing mark
    const mark = (isCurrent && cands[playingIdx] && cands[playingIdx].v === i)
      ? '<span class="st-info-playing">▶ playing</span>' : '';
    const sup = v && v.support === 0 ? ' <span class="st-info-nosup">(unsupported)</span>' : '';
    return `<li>${esc(s.codec || '?')}${s.bitrate ? ` · ${s.bitrate} kbps` : ''}${mark}${sup}</li>`;
  }).join('');
  ov.innerHTML = `
    <div id="st-info-panel" role="dialog" aria-label="Station details">
      <button id="st-info-close" class="st-btn" title="Close">×</button>
      <div class="st-info-head">
        ${art}
        <div class="st-info-headtext">
          <div class="st-info-name">${esc(station.name)}</div>
          <div class="st-info-now">${isCurrent
            ? `🎵 ${esc(_nowTitle || 'Live — fetching track…')}` : 'Not currently playing'}</div>
        </div>
      </div>
      <dl class="st-info-meta">
        ${station.tags ? `<dt>Tags</dt><dd>${esc(station.tags)}</dd>` : ''}
        ${station.country ? `<dt>Country</dt><dd>${esc(station.country)}</dd>` : ''}
        ${station.votes ? `<dt>Votes</dt><dd>▲ ${esc(station.votes)}</dd>` : ''}
        ${_safeUrl(station.homepage) ? `<dt>Website</dt><dd><a href="${esc(_safeUrl(station.homepage))}" target="_blank" rel="noopener">${esc(station.homepage)}</a></dd>` : ''}
        <dt>Streams</dt><dd><ul class="st-info-streams">${streams || '<li>—</li>'}</ul></dd>
      </dl>
      <div class="st-info-actions">
        <button id="st-info-fav" class="st-btn ${station.favorite ? 'on' : ''}">${station.favorite ? '★ Favorited' : '☆ Add to favorites'}</button>
        ${isCurrent ? '<button id="st-info-stop" class="st-btn">⏹ Stop</button>' : '<button id="st-info-play" class="st-btn">▶ Play</button>'}
      </div>
    </div>`;
  ov.classList.add('open');
  $('st-info-close').addEventListener('click', _closeInfo);
  const imgEl = ov.querySelector('img.st-info-art');
  if (imgEl) imgEl.onerror = () => { imgEl.outerHTML = '<span class="st-info-art st-info-glyph">📻</span>'; };
  $('st-info-fav')?.addEventListener('click', async () => {
    await _toggleFavorite(station, null);
    showInfo(station);   // re-render with the new favorite state
  });
  $('st-info-stop')?.addEventListener('click', () => { stop(); _closeInfo(); });
  $('st-info-play')?.addEventListener('click', () => { play(station); _closeInfo(); });
}

function _closeInfo() {
  $('st-info-overlay')?.classList.remove('open');
}

function _updateInfoNow() {
  const el = $('st-info-overlay');
  if (!el || !el.classList.contains('open')) return;
  const now = el.querySelector('.st-info-now');
  if (now && _current) {
    let txt = `🎵 ${_nowTitle || 'Live — fetching track…'}`;
    if (_artMeta) {
      // Append the cover-lookup metadata when we have it (Discogs/MusicBrainz/library).
      const album = [_artMeta.album, _artMeta.year].filter(Boolean).join(' · ');
      const src = _artMeta.source ? `via ${_artMeta.source}` : '';
      const extra = [album, _artMeta.label, src].filter(Boolean).join(' — ');
      if (extra) txt += `  ·  ${extra}`;
    }
    now.textContent = txt;
  }
}

// ── Header search placeholder: while Stations is open, the global search
// box reads (and behaves) as a station search.  search.js checks the same
// view-visibility to decide whether to prioritise station results. ────────────
let _origSearchPlaceholder = null;

function _stationsSearchMode(on) {
  const inp = $('search-input');
  if (!inp) return;
  if (on) {
    if (_origSearchPlaceholder === null) _origSearchPlaceholder = inp.getAttribute('placeholder') || '';
    inp.setAttribute('placeholder', 'Search stations…');
  } else if (_origSearchPlaceholder !== null) {
    inp.setAttribute('placeholder', _origSearchPlaceholder);
  }
}

// Switch the content pane into the Stations view (shared by the sidebar
// sections AND the full-results station search).  ``sview`` highlights the
// matching sidebar item; 'search' has no sidebar item, so all deactivate.
function _enterView(sview) {
  _sview = sview;
  _renderGen++;              // invalidate any in-flight list render for the old view
  _shell();
  _wireAudioHealth();
  _stationsSearchMode(true);
  const v = view();
  v.hidden = false;
  // Take the content pane: hide the library surfaces (their own show
  // functions restore visibility when a library view is opened).
  const tt = $('track-table'); if (tt) tt.style.display = 'none';
  const ag = $('album-grid'); if (ag) ag.hidden = true;
  const gx = $('galaxy-view'); if (gx) gx.hidden = true;
  // …and the list's own chrome (Play all / Shuffle / crumb, the group filter,
  // the A–Z rail) — a library view sets them again.
  for (const id of ['browse-header', 'group-filter-bar', 'az-rail']) {
    const el = $(id); if (el) el.hidden = true;
  }
  document.querySelectorAll('#nav-library li.active, #nav-smart li.active')
    .forEach((li) => li.classList.remove('active'));
  document.querySelectorAll('#nav-stations li').forEach((li) =>
    li.classList.toggle('active', li.dataset.sview === sview));
  _renderNowPlaying();
}

function show(sview) {
  _enterView(sview);
  if (sview === 'favorites') _renderFavorites();
  else if (sview === 'scene') _renderScene();
  else _renderWorld();
}

// Full station search results.  This is the stations parallel to the Library's
// "Enter = all song results": the omnibox routes here (on Enter while the
// Stations view is open, or via the dropdown's "Show all →") so stations get
// their COMPLETE match list — up to the endpoint's cap of 100 — instead of the
// 4-row dropdown preview.  Rendered through the shared ``_stationRows`` so play,
// favorite ★, and warm-on-hover behave exactly like every other station list.
async function _renderSearch(q) {
  const gen = _renderGen;
  _setCrumbs([{ label: `Search: “${q}”` }]);
  _loading();
  try {
    const stations = await _fetchJson(
      `/api/stations/search?q=${encodeURIComponent(q)}&limit=100`);
    if (gen !== _renderGen) return;    // user switched views while we fetched
    if (!Array.isArray(stations) || !stations.length) {
      _setDial([], `Search: ${q}`);
      $('st-body').innerHTML =
        `<div class="st-empty">No stations match “${esc(q)}”.</div>`;
      return;
    }
    _setCrumbs([{ label: `Search: “${q}” · ${stations.length} station${
      stations.length === 1 ? '' : 's'}` }]);
    _stationRows($('st-body'), stations, { showCountry: true, label: `Search: ${q}` });
  } catch (e) { _loading(`Station search failed: ${e.message}`); }
}

function showSearchResults(q) {
  if (!q || !q.trim()) return;
  _enterView('search');
  _renderSearch(q.trim());
}

function hide() {
  const v = view();
  if (!v || v.hidden) return;
  v.hidden = true;
  _stationsSearchMode(false);
  const tt = $('track-table'); if (tt) tt.style.display = '';
  document.querySelectorAll('#nav-stations li.active')
    .forEach((li) => li.classList.remove('active'));
}

// Leaving Stations: any click on the other sidebar sections hides the view.
document.addEventListener('click', (e) => {
  if (view()?.hidden !== false) return;
  if (e.target.closest('#nav-library li, #nav-smart li, #folder-tree li, .sidebar-playlist-list li')) {
    hide();
  }
}, true);

// Now-playing titles pushed from the relay over the library WebSocket.
window.addEventListener('sb:radio-meta', (e) => {
  const d = e.detail || {};
  if (_current && d.sid === _current.station.sid) {
    _nowTitle = d.title || '';
    _artMeta = null;                  // new song — drop the previous cover/metadata
    // Bottom-left player bar: show the now-playing Song (title) + Artist
    // (subtitle), falling back to the station name between songs.
    Player.setStationNowPlaying(d.song, d.artist);
    Player.updateStationArt(null);    // back to the station logo until a cover lands
    _updateNowTitle();
    _updateInfoNow();
    _updateRadioBar();                // ticker + LIVE + prev/next targets
  }
});

// Now-playing cover + metadata resolved by the relay (library/Discogs/MusicBrainz).
// Swap the bottom-left art to the song cover; show the gathered album/year/source.
window.addEventListener('sb:radio-art', (e) => {
  const d = e.detail || {};
  // Ignore a stale result (the song already changed) or a different station.
  if (!_current || d.sid !== _current.station.sid || d.title !== _nowTitle) {
    return;   // _artMeta was already cleared by the radio_meta song-change reset
  }
  _artMeta = d;
  if (d.cover_url) Player.updateStationArt(d.cover_url);
  _updateInfoNow();
  _updateNowTitle();                // "From <album> · <year>" on the card
});

// A library track (or anything else) took over the player: the station is no
// longer on air — the card, dial, presets and radio bar let go of it (the
// player already left station mode; nothing to stop).
Player.on('trackchange', () => {
  if (Player.stationMode || !_current) return;
  _resetBuffering();
  _current = null;
  _nowTitle = '';
  _artMeta = null;
  _playCtx = { list: null, idx: -1 };
  _renderNowPlaying();
  _updateRadioBar();
  _syncUi();
});

// Play / pause (the ring, the player bar, Space): the ring and the live loop follow.
Player.on('statechange', () => {
  if (!_current) return;
  _renderRing();
  _live?.evaluate();
});

export const Stations = {
  show, hide, play, stop, showInfo, surf: _surfStation, showSearchResults,
  // Diagnostics / tests
  get dial() { return _dial; },
  get live() { return _live; },
  get current() { return _current ? _current.station : null; },
};
window.Stations = Stations;   // test/debug escape hatch — same pattern as RadioMode
