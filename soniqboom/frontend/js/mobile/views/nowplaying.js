// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * nowplaying.js — Mobile Now Playing view: artwork, scrubber, transport, and
 * the tune list of a multi-tune file (SID, Amiga modules, console rips …).
 */
import { Player } from '../../player.js';
import { MobileRadio } from '../radio-player.js';
import { artPlaceholderEmoji, subsongStart, subsongStartOf, subsongWireToTune,
         subsongTuneToWire, staticArtUrl } from '../../utils.js';
import { subsongVirtualTrack, TUNE_CHIP_FORMAT_NAMES } from '../../tunes.js';
import { fmtDur } from './_common.js';
import { healthBadge } from '../../health.js';
import { mountNpVisuals } from './np_visuals.js';

export function mountNowPlaying(root, ctx) {
  // A portable cassette player's face (Retro hi-fi, the phone's only look —
  // css/mobile.css): the cover as the cassette's label with the reels in its
  // window, title / artist on a VFD strip, time windows round a ribbed seek
  // slot, keys with lamps.  .m-np carries the state the CSS needs: data-state
  // (playing | paused | stop), data-src="radio", data-shuffle, data-repeat,
  // --np-pos (0…1, how far the tape has run).  The meter display (.m-npm), the
  // waveform in the seek slot (.m-npw), the tape counter and the reels' speed:
  // np_visuals.js (css/mobile-visuals.css).  The cast key (#m-np-cast) shows
  // only where the mini player — which has the other one — steps aside while
  // Now Playing is open (a phone, css/mobile.css); it works the mini player's
  // (#btn-cast, cast_picker.js) and mirrors its state.
  root.innerHTML = `
    <div class="m-np" data-state="stop">
      <div class="m-np-deck">
        <div class="m-np-cassette">
          <div class="m-np-art" id="m-np-art"><span></span></div>
          <div class="m-np-tape" aria-hidden="true">
            <span class="m-np-reel"><i></i></span>
            <span class="m-np-reel"><i></i></span>
          </div>
        </div>
        <div class="m-npm" id="m-npm" data-mode="meter" data-viz="on">
          <div class="m-npm-legend" role="group" aria-label="Meter display picker">
            <button type="button" class="m-npm-mode" data-mode="meter" aria-pressed="false">METER</button>
            <button type="button" class="m-npm-mode" data-mode="spectrum" aria-pressed="false">SPECTRUM</button>
            <button type="button" class="m-npm-mode" data-mode="scope" aria-pressed="false">SCOPE</button>
            <span class="m-npm-peak" id="m-npm-peak" aria-hidden="true">PEAK</span>
            <span class="m-npm-count" id="m-npm-count" aria-hidden="true"><i>0</i><i>0</i><i>0</i></span>
          </div>
          <button type="button" class="m-npm-glass" id="m-npm-glass" aria-label="Meter display" aria-describedby="m-npm-desc">
            <span class="m-npm-leds" id="m-npm-leds"></span>
            <canvas class="m-npm-cvs" id="m-npm-cvs"></canvas>
            <span class="m-npm-note" id="m-npm-note"></span>
          </button>
          <button type="button" class="m-npm-off" id="m-npm-off" hidden>Meters off · tap to turn them on</button>
          <span class="m-npm-said" id="m-npm-desc" hidden></span>
          <span class="m-npm-said" id="m-npm-said" role="status" aria-live="polite"></span>
        </div>
        <div class="m-np-vfd">
          <div class="m-np-ann" aria-hidden="true">
            <span class="m-np-ann-state" id="m-np-ann-state">STOP</span>
            <span class="m-np-ann-src" id="m-np-ann-src"></span>
            <span class="m-np-ann-flag" data-ann="shuffle">SHUF</span>
            <span class="m-np-ann-flag" data-ann="repeat">RPT</span>
          </div>
          <div class="m-np-meta">
            <div class="m-np-title"  id="m-np-title">No track playing</div>
            <div class="m-np-artist" id="m-np-artist"></div>
          </div>
        </div>
        <div class="m-np-scrubber-wrap">
          <div class="m-npw" id="m-npw" aria-hidden="true" hidden><canvas></canvas><canvas></canvas></div>
          <input class="m-np-scrubber" id="m-np-scrubber" type="range" min="0" max="100" value="0" step="0.1" aria-label="Position">
          <div class="m-np-times">
            <span id="m-np-cur">0:00</span>
            <span id="m-np-dur">0:00</span>
          </div>
        </div>
        <div class="m-np-transport">
          <button class="m-np-btn"         id="m-np-prev"    aria-label="Previous">⏮&#xFE0E;</button>
          <button class="m-np-btn primary" id="m-np-play"    aria-label="Play/Pause">▶&#xFE0E;</button>
          <button class="m-np-btn"         id="m-np-next"    aria-label="Next">⏭&#xFE0E;</button>
        </div>
        <div class="m-np-transport m-np-modes">
          <button class="m-np-btn" id="m-np-shuffle" aria-label="Shuffle" aria-pressed="false">⇄</button>
          <button class="m-np-btn" id="m-np-repeat"  aria-label="Repeat" aria-pressed="false">↻</button>
          <button class="m-np-btn m-np-cast" id="m-np-cast" aria-label="Cast to device (Beta)" aria-haspopup="dialog" aria-expanded="false" data-casting="off"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 16.1A5 5 0 0 1 5.9 20M2 12.05A9 9 0 0 1 9.95 20M2 8V6a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-6"/><line x1="2" y1="20" x2="2.01" y2="20"/></svg></button>
        </div>
      </div>
      <section class="m-np-tunes" id="m-np-tunes" aria-labelledby="m-np-tunes-hdr" hidden>
        <div class="m-np-tunes-hdr" id="m-np-tunes-hdr">Tunes <span class="m-np-tunes-count" id="m-np-tunes-count"></span></div>
        <div class="m-np-tune-list" id="m-np-tune-list" role="list"></div>
      </section>
    </div>
  `;

  const np       = root.querySelector('.m-np');
  const annState = root.querySelector('#m-np-ann-state');
  const annSrc   = root.querySelector('#m-np-ann-src');
  const art      = root.querySelector('#m-np-art');
  const titleEl  = root.querySelector('#m-np-title');
  const artistEl = root.querySelector('#m-np-artist');
  const scrub    = root.querySelector('#m-np-scrubber');
  const curEl    = root.querySelector('#m-np-cur');
  const durEl    = root.querySelector('#m-np-dur');
  const playBtn  = root.querySelector('#m-np-play');
  const prevBtn  = root.querySelector('#m-np-prev');
  const nextBtn  = root.querySelector('#m-np-next');
  const shufBtn  = root.querySelector('#m-np-shuffle');
  const repBtn   = root.querySelector('#m-np-repeat');
  const tunesEl  = root.querySelector('#m-np-tunes');
  const tuneCountEl = root.querySelector('#m-np-tunes-count');
  const tuneList = root.querySelector('#m-np-tune-list');

  let _scrubbing = false;
  let _showingStation = false;      // the times say "● LIVE" (renderStation)

  // The scrubber's position also fills the played part of its track (--np-pct)
  // and, in Retro, sets how much tape sits on each reel (--np-pos on .m-np).
  function setScrub(v) {
    const pct = Math.min(100, Math.max(0, Number(v) || 0));
    scrub.value = String(pct);
    scrub.style.setProperty('--np-pct', `${pct}%`);
    np.style.setProperty('--np-pos', String(pct / 100));
    if (visuals) visuals.position(pct / 100);
  }
  let visuals = null;

  // The VFD's source annunciator: the format (a tracker's voice count after
  // it), LIVE for a station; empty with nothing loaded.
  function paintSource(t) {
    let txt = '';
    if (MobileRadio.active) txt = 'LIVE';
    else if (t) {
      // a long player name ("Sonic Arranger") gives way to the file's type
      const f = String(t.format || '').split('/')[0].trim().toUpperCase();
      const ext = (String(t.path || '').split('::').pop().match(/\.([a-z0-9]{1,5})$/i) || [])[1] || '';
      txt = f.length <= 8 ? f : (ext ? ext.toUpperCase() : f.split(/\s+/)[0].slice(0, 8));
      const ch = Number(t.channels) || 0;
      if (txt && ch > 2 && ch <= 64) txt += ` ${ch}CH`;
    }
    if (annSrc.textContent !== txt) annSrc.textContent = txt;
  }

  // (U+FE0E: drawn as text in the key's legend colour — iOS shows ⏸ / ▶ as emoji)
  const PLAY_GLYPH = '\u25B6\uFE0E', PAUSE_GLYPH = '\u23F8\uFE0E';

  // What the transport does: the play key's glyph; for the CSS the play lamp
  // and the reels (data-state), the VFD's PLAY / PAUSE / STOP, the radio source.
  // ``playing`` = the Player's statechange value (read from the source otherwise).
  function paintState(playing) {
    const radio = MobileRadio.active;
    if (radio) playing = MobileRadio.playing;
    else if (typeof playing !== 'boolean') playing = Player.playing;
    const loaded = radio || !!Player.currentTrack || !!Player.cuedTrack;
    const state = playing ? 'playing' : (loaded ? 'paused' : 'stop');
    playBtn.textContent = playing ? PAUSE_GLYPH : PLAY_GLYPH;
    np.dataset.state = state;
    annState.textContent = state === 'playing' ? `${PLAY_GLYPH} PLAY` : state === 'paused' ? '❚❚ PAUSE' : '■ STOP';
    if (radio) np.dataset.src = 'radio'; else delete np.dataset.src;
  }

  function renderStation(st) {
    titleEl.textContent  = (st && st.name) || 'Radio';
    artistEl.textContent = 'Internet radio';
    art.innerHTML = '<span>\u{1F4FB}</span>';   // 📻
    if (st && st.favicon) {
      const img = new Image();
      img.alt = ''; img.decoding = 'async';
      img.onload  = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      art.appendChild(img);
      img.src = st.favicon;
    }
    // Live stream — no seekable position.
    setScrub(0); scrub.disabled = true;
    curEl.textContent = '● LIVE'; durEl.textContent = '';
    _showingStation = true;
    paintSource(null);
  }

  function renderTrack(t) {
    paintState();
    // Radio is the active source while a station plays.
    if (MobileRadio.active) { renderStation(MobileRadio.station); return; }
    scrub.disabled = false;
    // A station stopped: its "● LIVE" goes (the next timeupdate fills the times in).
    if (_showingStation) {
      _showingStation = false;
      setScrub(0);
      curEl.textContent = '0:00';
      durEl.textContent = fmtDur(Number(t && t.duration) || 0) || '0:00';
    }
    // Nothing loaded, but a queue waits for ▶ (restored after a reload, or
    // resumed from another device): show that track at the position ▶ starts.
    const cued = t ? null : (Player.cuedTrack || null);
    if (cued) {
      t = cued;
      const sec = Player.cuedSec || 0;
      const dur = Number(cued.duration) || 0;
      setScrub(dur > 0 ? (sec / dur) * 100 : 0);
      curEl.textContent = fmtDur(sec) || '0:00';
      durEl.textContent = fmtDur(dur);
    }
    _shownCuedKey = cued ? `${cued.id}~${cued.subsong ?? ''}` : null;
    paintSource(t);
    if (!t) {
      titleEl.textContent  = 'No track playing';
      artistEl.textContent = '';
      // Layered placeholder with the default 🔊 glyph (no track to ask
      // ``artPlaceholderEmoji`` about format).
      art.innerHTML = '<span>\u{1F50A}</span>';
      setScrub(0); curEl.textContent = '0:00'; durEl.textContent = '0:00';
      return;
    }
    // A picked tune of a multi-tune file: its number after the title (the
    // wire maps to it with the file's start song — utils.js).
    const _subTotal = t.subsongTotal || t.subsongs;
    const _tune = (Number.isInteger(t.subsong) && _subTotal > 1)
      ? ` · Tune ${subsongWireToTune(t.subsong, subsongStartOf(t), _subTotal)} / ${_subTotal}` : '';
    titleEl.textContent  = (t.title  || '—') + _tune;   // wipes any prior badge
    const _health = healthBadge(t);     // a playback problem (health.js)
    if (_health) titleEl.appendChild(_health);
    artistEl.textContent = [t.artist || t.album_artist, t.album].filter(Boolean).join(' — ');
    // Layered placeholder + cover img (same pattern as mobile mini-player
    // and the desktop row covers).  The img fades in via the ``.loaded``
    // class on successful decode; onerror removes it so the format
    // glyph stays put — no broken-image glyph ever paints.
    art.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = artPlaceholderEmoji(t);
    art.appendChild(span);
    const artSrc = t.id ? `/api/art/${t.id}/lg.jpg?fallback=none` : staticArtUrl(t.cover_art);
    if (artSrc) {
      const img = new Image();
      img.alt = '';
      img.decoding = 'async';   // lg art decode off the main thread
      img.onload  = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      art.appendChild(img);
      img.src = artSrc;
    }
  }

  // ── Tunes of a multi-tune file ─────────────────────────────────────────
  // The playing file's tunes, in tune order; a tap plays that tune (the
  // queue becomes that one tune, like the desktop Track Info picker).  The
  // count, the start song (SID / SNDH: it moves the wires) and the default
  // tune — the one a plain play plays, marked "default" — come from
  // /extended.  For an Amiga module or a console rip the server learns the
  // default on the first play (its first tune that isn't empty): while it is
  // unknown, or /extended failed, the list asks again a few times.
  let _tunes = null;          // { id, count, start, def, lengths, base, settled }
  let _tunesAbort = null;
  let _tunesRetry = null;
  const _TUNES_RETRY_MS = [1500, 4000, 10000];

  function _tuneRows() {
    const st = _tunes;
    const rows = [];
    for (let n = 1; n <= st.count; n++) {
      const wire = subsongTuneToWire(n, st.start, st.count);
      const isDef = n === st.def;
      const len = Array.isArray(st.lengths) ? fmtDur(Number(st.lengths[n - 1]) || 0) : '';
      rows.push(`<div class="m-np-tune-item" role="listitem">`
        + `<button type="button" class="m-np-tune" data-wire="${wire}"`
        + ` aria-label="Play tune ${n}${isDef ? ' (default)' : ''}">`
        + `<span class="m-np-tune-name">Tune ${n}</span>`
        + (isDef ? '<span class="m-np-tune-def">default</span>' : '')
        + `<span class="m-np-tune-len">${len}</span></button></div>`);
    }
    tuneList.innerHTML = rows.join('');
  }

  function _markPlayingTune() {
    const st = _tunes;
    if (!st) return;
    const cur = Player.currentTrack;
    // A plain play of the file (no ``subsong``) plays its default tune.
    const wire = (cur && cur.id === st.id)
      ? (Number.isInteger(cur.subsong) ? cur.subsong : subsongTuneToWire(st.def, st.start, st.count))
      : -1;
    for (const b of tuneList.querySelectorAll('.m-np-tune')) {
      const on = Number(b.dataset.wire) === wire;
      b.classList.toggle('playing', on);
      if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
    }
  }

  function _hideTunes() {
    _tunes = null;
    if (_tunesRetry) { clearTimeout(_tunesRetry); _tunesRetry = null; }
    tunesEl.hidden = true;
    tuneList.innerHTML = '';
  }

  async function renderTunes(t, attempt = 0) {
    if (MobileRadio.active || !t || !t.id) { _hideTunes(); return; }
    if (_tunes && _tunes.id === t.id && (_tunes.settled || attempt === 0)) {
      _markPlayingTune();
      return;
    }
    const known = Number(t.subsongTotal || t.subsongs) || 0;
    if (!(known > 1) && !TUNE_CHIP_FORMAT_NAMES.has(t.format)) { _hideTunes(); return; }
    if (_tunesRetry) { clearTimeout(_tunesRetry); _tunesRetry = null; }
    if (_tunesAbort) { try { _tunesAbort.abort(); } catch (_) {} }
    const ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    _tunesAbort = ctl;
    let data = null;
    try {
      const res = await fetch(`/api/tracks/${encodeURIComponent(t.id)}/extended`,
                              ctl ? { signal: ctl.signal } : undefined);
      if (res.ok) data = await res.json();
    } catch (e) {
      if (e && e.name === 'AbortError') return;      // a newer track owns the list
    } finally {
      if (_tunesAbort === ctl) _tunesAbort = null;
    }
    const cur = Player.currentTrack;
    if (!cur || cur.id !== t.id || MobileRadio.active) return;
    const count = (data && Number(data.subsongs) > 1) ? Number(data.subsongs) : known;
    if (!(count > 1)) {
      if (data || attempt >= _TUNES_RETRY_MS.length) { _hideTunes(); return; }
    } else {
      // The file itself (a picked tune's entry carries that tune's length).
      const base = { ...t };
      if (Number.isInteger(t.subsong)) base.duration = 0;
      for (const k of ['subsong', 'subsongTotal', 'subsongStart', 'subsongLabel']) delete base[k];
      const start = subsongStart((data && data.default_track) ?? subsongStartOf(t), count);
      const defTune = data && data.default_tune;
      _tunes = { id: t.id, count, start,
                 def: subsongStart(defTune ?? start, count),
                 lengths: (data && Array.isArray(data.hvsc_lengths)) ? data.hvsc_lengths : null,
                 base, settled: !!data && defTune != null && !data.default_tune_pending };
      tuneCountEl.textContent = `· ${count}`;
      _tuneRows();
      tunesEl.hidden = false;
      _markPlayingTune();
      if (_tunes.settled) return;
    }
    // Not known yet (or the request failed): ask again while this track plays.
    if (attempt < _TUNES_RETRY_MS.length) {
      _tunesRetry = setTimeout(() => {
        _tunesRetry = null;
        const now = Player.currentTrack;
        if (now && now.id === t.id) renderTunes(now, attempt + 1);
      }, _TUNES_RETRY_MS[attempt]);
    }
  }

  tuneList.addEventListener('click', (e) => {
    const b = e.target.closest('.m-np-tune');
    const st = _tunes;
    if (!b || !st) return;
    const wire = parseInt(b.dataset.wire, 10);
    if (!(wire >= 0)) return;
    Player.setQueue([subsongVirtualTrack(st.base, wire,
                     { count: st.count, start: st.start, lengths: st.lengths, def: st.def })], 0);
  });

  Player.on('trackchange', renderTrack);
  Player.on('trackchange', (t) => { renderTunes(t); });
  // The playing track's row was re-read (an online collection's package
  // opened into its first tune, a catalogue tune's first-play read).
  document.addEventListener('soniqboom:track-refreshed', (e) => {
    const cur = Player.currentTrack;
    if (cur && e && e.detail && e.detail.id === cur.id) { renderTrack(cur); renderTunes(cur); }
  });
  // A playback problem reported / cleared for the playing track: its badge follows.
  document.addEventListener('soniqboom:health-changed', (e) => {
    const d = (e && e.detail) || {};
    const cur = Player.currentTrack;
    if (cur && Array.isArray(d.ids) && d.ids.includes(cur.id)) renderTrack(cur);
  });
  // The cued track (see renderTrack) appears, changes or goes: the resume /
  // restore, a queue edit, a clear.  Nothing to do while a track is loaded.
  let _shownCuedKey = null;
  const _renderCued = () => {
    if (MobileRadio.active || Player.currentTrack) return;
    const c = Player.cuedTrack || null;
    if ((c ? `${c.id}~${c.subsong ?? ''}` : null) === _shownCuedKey) return;
    renderTrack(null);
  };
  Player.on('cue', _renderCued);
  Player.on('queuechange', _renderCued);

  Player.on('statechange', ({ playing }) => {
    if (MobileRadio.active) return;         // radio owns the transport while active
    paintState(playing);
  });

  Player.on('timeupdate', ({ current, duration, pct }) => {
    if (MobileRadio.active) return;
    if (!_scrubbing) {
      setScrub(pct);
      curEl.textContent = fmtDur(current);
    }
    durEl.textContent = fmtDur(duration);
  });

  // Radio drives the view + play button while a station is the active source.
  MobileRadio.on('change', () => { renderTrack(Player.currentTrack); renderTunes(Player.currentTrack); });
  MobileRadio.on('state',  () => {
    if (MobileRadio.active) paintState();
  });

  // Scrubber: pointerdown locks, input previews, change commits
  scrub.addEventListener('pointerdown', () => { _scrubbing = true; });
  scrub.addEventListener('input', () => {
    // Preview the time without committing
    const pct = parseFloat(scrub.value);
    setScrub(pct);                          // the fill / tape follow the finger
    curEl.textContent = fmtDur((pct / 100) * (Player.currentTrack?.duration || 0));
  });
  scrub.addEventListener('change', () => {
    Player.seek(parseFloat(scrub.value));
    _scrubbing = false;
  });

  playBtn.addEventListener('click', async () => {
    if (MobileRadio.active) { MobileRadio.toggle(); return; }
    if (!Player.currentTrack && Player.queue.length === 0) {
      ctx.toast('Tap a song to start playing');
      return;
    }
    Player.playPause();
  });
  prevBtn.addEventListener('click', () => { if (!MobileRadio.active) Player.prev(); });
  nextBtn.addEventListener('click', () => { if (!MobileRadio.active) Player.next(); });
  // On = aria-pressed (the CSS colours the key / lights its lamp) and the
  // VFD's SHUF / RPT annunciators (data-shuffle / data-repeat on .m-np).
  const paintShuffle = () => {
    shufBtn.setAttribute('aria-pressed', Player.shuffle ? 'true' : 'false');
    np.dataset.shuffle = Player.shuffle ? 'on' : 'off';
  };
  const paintRepeat = (mode = Player.repeatMode || 'none') => {
    repBtn.setAttribute('aria-pressed', mode === 'none' ? 'false' : 'true');
    repBtn.textContent = (mode === 'one') ? '↻¹' : '↻';
    np.dataset.repeat = mode;
  };
  Player.on('shufflechange', paintShuffle);
  paintShuffle();      // a restored queue may already have shuffle on
  shufBtn.addEventListener('click', () => {
    if (Player.radioActive) { ctx.toast('Radio Mode picks the order \u2014 shuffle is available again when radio stops.'); return; }
    ctx.toast(Player.toggleShuffle() ? 'Shuffle on' : 'Shuffle off');
  });
  repBtn.addEventListener('click', () => {
    const mode = Player.toggleRepeat();
    paintRepeat(mode);
    ctx.toast(`Repeat: ${mode}`);
  });

  // The deck's cast key: the mini player's (#btn-cast) does the work — the
  // picker opens, its focus returns here — and its state shows here too.
  const castBtn = root.querySelector('#m-np-cast');
  const miniCast = document.getElementById('btn-cast');
  if (castBtn && miniCast) {
    castBtn.addEventListener('click', () => miniCast.click());
    const mirror = () => {
      castBtn.setAttribute('aria-expanded', miniCast.getAttribute('aria-expanded') === 'true' ? 'true' : 'false');
      castBtn.dataset.casting = miniCast.classList.contains('is-casting') ? 'on' : 'off';
      if (miniCast.title) castBtn.title = miniCast.title; else castBtn.removeAttribute('title');
    };
    new MutationObserver(mirror).observe(miniCast, { attributes: true, attributeFilter: ['class', 'aria-expanded', 'title'] });
    mirror();
  } else if (castBtn) castBtn.remove();

  visuals = mountNpVisuals({ root, np, ctx });

  // Initial paint
  renderTrack(Player.currentTrack);
  renderTunes(Player.currentTrack);
  paintRepeat();
  paintState();
}
