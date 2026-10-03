// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * nowplaying.js — Mobile Now Playing view: artwork, scrubber, transport, and
 * the tune list of a multi-tune file (SID, Amiga modules, console rips …).
 */
import { Player } from '../../player.js';
import { MobileRadio } from '../radio-player.js';
import { artPlaceholderEmoji, subsongStart, subsongStartOf, subsongWireToTune,
         subsongTuneToWire } from '../../utils.js';
import { subsongVirtualTrack, TUNE_CHIP_FORMAT_NAMES } from '../../tunes.js';
import { fmtDur } from './_common.js';

export function mountNowPlaying(root, ctx) {
  root.innerHTML = `
    <div class="m-np">
      <div class="m-np-art" id="m-np-art"><span></span></div>
      <div>
        <div class="m-np-title"  id="m-np-title">No track playing</div>
        <div class="m-np-artist" id="m-np-artist"></div>
      </div>
      <div class="m-np-scrubber-wrap">
        <input class="m-np-scrubber" id="m-np-scrubber" type="range" min="0" max="100" value="0" step="0.1">
        <div class="m-np-times">
          <span id="m-np-cur">0:00</span>
          <span id="m-np-dur">0:00</span>
        </div>
      </div>
      <div class="m-np-transport">
        <button class="m-np-btn"         id="m-np-prev"    aria-label="Previous">⏮</button>
        <button class="m-np-btn primary" id="m-np-play"    aria-label="Play/Pause">▶</button>
        <button class="m-np-btn"         id="m-np-next"    aria-label="Next">⏭</button>
      </div>
      <div class="m-np-transport" style="gap:32px">
        <button class="m-np-btn" id="m-np-shuffle" aria-label="Shuffle">⇄</button>
        <button class="m-np-btn" id="m-np-repeat"  aria-label="Repeat">↻</button>
      </div>
      <section class="m-np-tunes" id="m-np-tunes" aria-labelledby="m-np-tunes-hdr" hidden>
        <div class="m-np-tunes-hdr" id="m-np-tunes-hdr">Tunes <span class="m-np-tunes-count" id="m-np-tunes-count"></span></div>
        <div class="m-np-tune-list" id="m-np-tune-list" role="list"></div>
      </section>
    </div>
  `;

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
    scrub.value = '0'; scrub.disabled = true;
    curEl.textContent = '● LIVE'; durEl.textContent = '';
  }

  function renderTrack(t) {
    // Radio is the active source while a station plays.
    if (MobileRadio.active) { renderStation(MobileRadio.station); return; }
    scrub.disabled = false;
    // Nothing loaded, but a queue waits for ▶ (restored after a reload, or
    // resumed from another device): show that track at the position ▶ starts.
    const cued = t ? null : (Player.cuedTrack || null);
    if (cued) {
      t = cued;
      const sec = Player.cuedSec || 0;
      const dur = Number(cued.duration) || 0;
      scrub.value = String(dur > 0 ? Math.min(100, (sec / dur) * 100) : 0);
      curEl.textContent = fmtDur(sec) || '0:00';
      durEl.textContent = fmtDur(dur);
    }
    _shownCuedKey = cued ? `${cued.id}~${cued.subsong ?? ''}` : null;
    if (!t) {
      titleEl.textContent  = 'No track playing';
      artistEl.textContent = '';
      // Layered placeholder with the default 🔊 glyph (no track to ask
      // ``artPlaceholderEmoji`` about format).
      art.innerHTML = '<span>\u{1F50A}</span>';
      scrub.value = '0'; curEl.textContent = '0:00'; durEl.textContent = '0:00';
      return;
    }
    // A picked tune of a multi-tune file: its number after the title (the
    // wire maps to it with the file's start song — utils.js).
    const _subTotal = t.subsongTotal || t.subsongs;
    const _tune = (Number.isInteger(t.subsong) && _subTotal > 1)
      ? ` · Tune ${subsongWireToTune(t.subsong, subsongStartOf(t), _subTotal)} / ${_subTotal}` : '';
    titleEl.textContent  = (t.title  || '—') + _tune;   // wipes any prior badge
    if (t.defect === 'partial' || t.defect === 'corrupt') {
      const badge = document.createElement('span');
      badge.className = `track-defect-badge track-defect-${t.defect}`;
      badge.textContent = t.defect;
      badge.title = t.defect_detail || '';
      titleEl.appendChild(badge);
    }
    artistEl.textContent = [t.artist || t.album_artist, t.album].filter(Boolean).join(' — ');
    // Layered placeholder + cover img (same pattern as mobile mini-player
    // and the desktop row covers).  The img fades in via the ``.loaded``
    // class on successful decode; onerror removes it so the format
    // glyph stays put — no broken-image glyph ever paints.
    art.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = artPlaceholderEmoji(t);
    art.appendChild(span);
    const artSrc = t.id ? `/api/art/${t.id}?size=lg&fallback=404` : t.cover_art;
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
    playBtn.textContent = playing ? '⏸' : '▶';
  });

  Player.on('timeupdate', ({ current, duration, pct }) => {
    if (MobileRadio.active) return;
    if (!_scrubbing) {
      scrub.value = String(pct || 0);
      curEl.textContent = fmtDur(current);
    }
    durEl.textContent = fmtDur(duration);
  });

  // Radio drives the view + play button while a station is the active source.
  MobileRadio.on('change', () => { renderTrack(Player.currentTrack); renderTunes(Player.currentTrack); });
  MobileRadio.on('state',  () => {
    if (MobileRadio.active) playBtn.textContent = MobileRadio.playing ? '⏸' : '▶';
  });

  // Scrubber: pointerdown locks, input previews, change commits
  scrub.addEventListener('pointerdown', () => { _scrubbing = true; });
  scrub.addEventListener('input', () => {
    // Preview the time without committing
    const pct = parseFloat(scrub.value);
    const dur = parseFloat(durEl.textContent) || 0; // not reliable; use Player
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
  const paintShuffle = () => {
    shufBtn.style.color = Player.shuffle ? 'var(--accent)' : 'var(--text)';
    shufBtn.setAttribute('aria-pressed', Player.shuffle ? 'true' : 'false');
  };
  Player.on('shufflechange', paintShuffle);
  paintShuffle();      // a restored queue may already have shuffle on
  shufBtn.addEventListener('click', () => {
    if (Player.radioActive) { ctx.toast('Radio Mode picks the order \u2014 shuffle is available again when radio stops.'); return; }
    ctx.toast(Player.toggleShuffle() ? 'Shuffle on' : 'Shuffle off');
  });
  repBtn.addEventListener('click', () => {
    const mode = Player.toggleRepeat();
    repBtn.style.color = (mode === 'none') ? 'var(--text)' : 'var(--accent)';
    repBtn.textContent = (mode === 'one') ? '↻¹' : '↻';
    ctx.toast(`Repeat: ${mode}`);
  });

  // Initial paint
  renderTrack(Player.currentTrack);
  renderTunes(Player.currentTrack);
  playBtn.textContent = (MobileRadio.active ? MobileRadio.playing : Player.playing) ? '⏸' : '▶';
}
