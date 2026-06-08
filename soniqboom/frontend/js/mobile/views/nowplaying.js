// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * nowplaying.js — Mobile Now Playing view: artwork, scrubber, transport.
 */
import { Player } from '../../player.js';
import { artPlaceholderEmoji } from '../../utils.js';
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

  let _scrubbing = false;

  function renderTrack(t) {
    if (!t) {
      titleEl.textContent  = 'No track playing';
      artistEl.textContent = '';
      // Layered placeholder with the default 🔊 glyph (no track to ask
      // ``artPlaceholderEmoji`` about format).
      art.innerHTML = '<span>\u{1F50A}</span>';
      return;
    }
    titleEl.textContent  = t.title  || '—';
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
      img.onload  = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      art.appendChild(img);
      img.src = artSrc;
    }
  }

  Player.on('trackchange', renderTrack);

  Player.on('statechange', ({ playing }) => {
    playBtn.textContent = playing ? '⏸' : '▶';
  });

  Player.on('timeupdate', ({ current, duration, pct }) => {
    if (!_scrubbing) {
      scrub.value = String(pct || 0);
      curEl.textContent = fmtDur(current);
    }
    durEl.textContent = fmtDur(duration);
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
    if (!Player.currentTrack && Player.queue.length === 0) {
      ctx.toast('Tap a song to start playing');
      return;
    }
    Player.playPause();
  });
  prevBtn.addEventListener('click', () => Player.prev());
  nextBtn.addEventListener('click', () => Player.next());
  shufBtn.addEventListener('click', () => {
    const on = Player.toggleShuffle();
    shufBtn.style.color = on ? 'var(--accent)' : 'var(--text)';
    ctx.toast(on ? 'Shuffle on' : 'Shuffle off');
  });
  repBtn.addEventListener('click', () => {
    const mode = Player.toggleRepeat();
    repBtn.style.color = (mode === 'none') ? 'var(--text)' : 'var(--accent)';
    repBtn.textContent = (mode === 'one') ? '↻¹' : '↻';
    ctx.toast(`Repeat: ${mode}`);
  });

  // Initial paint
  renderTrack(Player.currentTrack);
  playBtn.textContent = Player.playing ? '⏸' : '▶';
}
