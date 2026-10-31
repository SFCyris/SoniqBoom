// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * playlist/hero.js — the open playlist's header ("hero"): a cover mosaic, who
 * shares it, its name, "N tracks · length · format mix" with a coloured mix
 * bar, Play / Shuffle, the owner's Share switch or an imported playlist's
 * Unsubscribe (the existing buttons, moved in) and a ⋯ menu.
 *
 * Modern: a glass header tinted by the covers.  Retro hi-fi: a cassette J-card
 * (handwritten-style name, a tape-counter total) on a brushed-metal plate.
 * The look is CSS (playlist-styles.css, #playlist-panel[data-pl-style]).
 *
 * Exports: createHero, fmtShort, fmtFamily, formatMix, familyMix, FAMILY_NAMES, totalSeconds,
 *          fmtTotal, counterDigits (the queue panel's header uses them too)
 */
import {
  artPlaceholderEmoji, TRACKER_FORMAT_NAMES, CHIP_FORMAT_NAMES, ATARI_FORMAT_NAMES,
  PSF_FORMAT_NAMES, isUadeAmigaTrack,
} from '../utils.js';

// ── Format labels ────────────────────────────────────────────────────────────
const LOSSLESS = new Set(['FLAC', 'ALAC', 'WAV', 'WAVE', 'AIFF', 'AIF', 'WV', 'WAVPACK', 'APE', 'DSF', 'DFF', 'DSD', 'TTA']);
const LOSSY = new Set(['MP3', 'AAC', 'M4A', 'OGG', 'OPUS', 'MPC', 'WMA', 'MP2']);
const TRACKER_EXT = new Set(['mod', 'xm', 's3m', 'it', 'mtm', 'med', 'okt', '669', 'ahx', 'hvl', 'mptm', 'stm', 'far', 'ult', 'dbm', 'mo3', 'umx']);

function _ext(t) {
  const tail = String((t && t.path) || '').split('::').pop().split('/').pop();
  const i = tail.lastIndexOf('.');
  return i > 0 ? tail.slice(i + 1).toLowerCase() : '';
}

function _memo(t, key, fn) {
  if (t && t[key] !== undefined) return t[key];
  const v = fn();
  if (t) {
    try { Object.defineProperty(t, key, { value: v, enumerable: false, configurable: true, writable: true }); }
    catch (_) { /* frozen */ }
  }
  return v;
}

/** A short format label for chips and the mix ("FLAC", "SID", "MOD", "A2M"). */
export function fmtShort(t) {
  return _memo(t, '_plxFmt', () => {
    const f = String((t && t.format) || '').trim();
    const F = f.toUpperCase();
    if (F === 'SID' || F === 'PSID' || F === 'RSID') return 'SID';
    if (F === 'OGG VORBIS' || F === 'VORBIS') return 'OGG';
    if (/^[A-Z0-9]{2,5}$/i.test(f)) return F;
    const e = _ext(t);
    if (/^[a-z0-9]{1,4}$/.test(e)) return e.toUpperCase();
    return f ? F.split(/\s+/)[0].slice(0, 6) : '?';
  });
}

/** The colour family of a track's format: lossless, lossy, sid, tracker, chip, midi, other. */
export function fmtFamily(t) {
  return _memo(t, '_plxFam', () => {
    const f = String((t && t.format) || '');
    const F = f.toUpperCase();
    const s = fmtShort(t);
    if (F === 'SID' || F === 'PSID' || F === 'RSID') return 'sid';
    if (F === 'MIDI' || F === 'MID' || s === 'MID' || s === 'MIDI') return 'midi';
    if (LOSSLESS.has(F) || LOSSLESS.has(s)) return 'lossless';
    if (LOSSY.has(s) || F.includes('VORBIS')) return 'lossy';
    if (TRACKER_FORMAT_NAMES.has(f) || TRACKER_EXT.has(_ext(t)) || isUadeAmigaTrack(t)) return 'tracker';
    if (CHIP_FORMAT_NAMES.has(f) || ATARI_FORMAT_NAMES.has(f) || PSF_FORMAT_NAMES.has(f)) return 'chip';
    return 'other';
  });
}

/** [{label, family, count}] by count (most first; ties by label). */
export function formatMix(tracks) {
  const by = new Map();
  for (const t of tracks || []) {
    if (!t) continue;
    const label = fmtShort(t);
    const g = by.get(label);
    if (g) g.count++;
    else by.set(label, { label, family: fmtFamily(t), count: 1 });
  }
  return [...by.values()].sort((a, b) => (b.count - a.count) || a.label.localeCompare(b.label));
}

export const FAMILY_NAMES = {
  lossless: 'Lossless', lossy: 'Lossy', sid: 'SID', tracker: 'Tracker', chip: 'Chip', midi: 'MIDI', other: 'Other',
};

/** ``formatMix`` grouped by family: [{family, count, labels: [{label, count}]}], largest first. */
export function familyMix(mix) {
  const by = new Map();
  for (const g of mix || []) {
    const f = by.get(g.family);
    if (f) { f.count += g.count; f.labels.push({ label: g.label, count: g.count }); }
    else by.set(g.family, { family: g.family, count: g.count, labels: [{ label: g.label, count: g.count }] });
  }
  return [...by.values()].sort((a, b) => (b.count - a.count) || a.family.localeCompare(b.family));
}

/** {sec, unknown}: the summed known lengths, and how many entries have none. */
export function totalSeconds(tracks) {
  let sec = 0, unknown = 0;
  for (const t of tracks || []) {
    const d = Number(t && t.duration);
    if (d > 0) sec += d; else unknown++;
  }
  return { sec, unknown };
}

function _fmtTotal(sec) {
  sec = Math.round(sec);
  if (sec < 60) return `${sec} s`;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

function _counterDigits(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.min(99, Math.floor(sec / 3600));
  const m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** "42 s" / "43 min" / "5 h 48 min" — and the tape counter's "5:48:12". */
export { _fmtTotal as fmtTotal, _counterDigits as counterDigits };

// ── Mosaic ───────────────────────────────────────────────────────────────────
// The first four DISTINCT covers: one per album (or folder, for tracks without
// an album) — several tracks of one album would show the same picture.
function _coverKey(t) {
  const alb = String(t.album || '').trim().toLowerCase();
  if (alb) return `a:${String(t.album_artist || t.artist || '').trim().toLowerCase()}|${alb}`;
  return t.dir_hash ? `d:${t.dir_hash}` : `t:${t.id}`;
}

function _artUrl(id) {
  const ep = window.__sbArtEpoch ? `&_t=${window.__sbArtEpoch}` : '';
  return `/api/art/${encodeURIComponent(id)}/sm.jpg?fallback=none${ep}`;
}

function _loadImg(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => resolve(img.naturalWidth > 1 ? img : null);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

// The covers' average colour (the modern header's tint); null if unreadable.
function _avgColour(imgs) {
  try {
    const c = document.createElement('canvas');
    c.width = c.height = 8;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    let r = 0, g = 0, b = 0, n = 0;
    for (const img of imgs) {
      ctx.clearRect(0, 0, 8, 8);
      ctx.drawImage(img, 0, 0, 8, 8);
      const d = ctx.getImageData(0, 0, 8, 8).data;
      for (let i = 0; i < d.length; i += 4) { r += d[i]; g += d[i + 1]; b += d[i + 2]; n++; }
    }
    if (!n) return null;
    return `rgb(${Math.round(r / n)}, ${Math.round(g / n)}, ${Math.round(b / n)})`;
  } catch (_) { return null; }
}

const _PH_GLYPH = { sid: '\u{1F579}️', tracker: '\u{1F4BE}', chip: '\u{1F3AE}', midi: '\u{1F3BC}', lossless: '\u{1F3B5}', lossy: '\u{1F3B5}', other: '♪' };

// ── The hero ────────────────────────────────────────────────────────────────
/**
 * ``opts``: { panel, after (element the hero goes after), shareBtn, unsubBtn,
 *   onPlay(), onShuffle(), menuItems() → [{label, run, danger}] }
 */
export function createHero(opts) {
  const el = document.createElement('section');
  el.className = 'plx-hero';
  el.id = 'pl-hero';
  el.hidden = true;
  el.setAttribute('aria-label', 'Playlist');
  el.innerHTML = `
    <div class="plx-card">
      <div class="plx-mosaic" aria-hidden="true"></div>
      <div class="plx-head">
        <div class="plx-kicker"></div>
        <h2 class="plx-name"></h2>
        <div class="plx-meta">
          <span class="plx-count"></span>
          <span class="plx-len"></span>
          <span class="plx-counter" aria-hidden="true"></span>
          <span class="plx-mixtext"></span>
        </div>
        <div class="plx-mixbar" role="img"></div>
      </div>
    </div>
    <div class="plx-actions">
      <button type="button" class="plx-play" aria-label="Play">
        <span class="plx-play-ico" aria-hidden="true"></span><span class="plx-play-lbl">Play</span>
      </button>
      <button type="button" class="plx-shuffle" title="Shuffle this playlist" aria-label="Shuffle">
        <span aria-hidden="true">&#8644;</span><span class="plx-btn-lbl">Shuffle</span>
      </button>
      <span class="plx-slot"></span>
      <div class="plx-menu-wrap">
        <button type="button" class="plx-menu-btn" aria-haspopup="menu" aria-expanded="false" title="More" aria-label="More actions">&#8943;</button>
        <div class="plx-menu" role="menu" hidden></div>
      </div>
    </div>`;
  opts.after.insertAdjacentElement('afterend', el);

  const $ = (s) => el.querySelector(s);
  const mosaic = $('.plx-mosaic'), kicker = $('.plx-kicker'), nameEl = $('.plx-name');
  const countEl = $('.plx-count'), lenEl = $('.plx-len'), counterEl = $('.plx-counter');
  const mixText = $('.plx-mixtext'), mixBar = $('.plx-mixbar');
  const playBtn = $('.plx-play'), playLbl = $('.plx-play-lbl'), shufBtn = $('.plx-shuffle');
  const slot = $('.plx-slot'), menuBtn = $('.plx-menu-btn'), menu = $('.plx-menu');
  // The existing Share / Unsubscribe buttons (their logic stays in playlist.js).
  if (opts.shareBtn) slot.appendChild(opts.shareBtn);
  if (opts.unsubBtn) slot.appendChild(opts.unsubBtn);

  // Space / Enter on a header button press THAT button (the page's Space would
  // play / pause whatever is queued instead).
  el.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Spacebar' || e.key === 'Enter')
        && e.target instanceof HTMLButtonElement && el.contains(e.target)) e.stopPropagation();
  });
  playBtn.addEventListener('click', () => opts.onPlay && opts.onPlay());
  shufBtn.addEventListener('click', () => opts.onShuffle && opts.onShuffle());

  // ── ⋯ menu ────────────────────────────────────────────────────────────────
  function closeMenu(focusBtn = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    menuBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', _outside, true);
    if (focusBtn) menuBtn.focus();
  }
  function _outside(e) { if (!el.querySelector('.plx-menu-wrap').contains(e.target)) closeMenu(); }
  function openMenu() {
    const items = (opts.menuItems && opts.menuItems()) || [];
    menu.innerHTML = '';
    for (const it of items) {
      const b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('role', 'menuitem');
      b.className = 'plx-menu-item' + (it.danger ? ' danger' : '');
      b.textContent = it.label;
      b.addEventListener('click', async () => {
        closeMenu();
        try { await it.run(); } finally {
          // (a dialog the item opened has closed: focus back on ⋯, not the page)
          if (menuBtn.isConnected && !el.hidden
              && (!document.activeElement || document.activeElement === document.body)) menuBtn.focus();
        }
      });
      menu.appendChild(b);
    }
    if (!items.length) return;
    menu.hidden = false;
    menuBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', _outside, true);
    menu.querySelector('button')?.focus();
  }
  menuBtn.addEventListener('click', () => (menu.hidden ? openMenu() : closeMenu()));
  menu.addEventListener('keydown', (e) => {
    const list = [...menu.querySelectorAll('button')];
    const i = list.indexOf(document.activeElement);
    // (The keys the menu owns stop here: the page's shortcuts — arrows are the
    // volume — must not act on them too.)
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); list[(i - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === 'Home') { e.preventDefault(); e.stopPropagation(); list[0]?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); e.stopPropagation(); list[list.length - 1]?.focus(); }
    else if (e.key === 'Tab') closeMenu();
  });

  // ── Mosaic ───────────────────────────────────────────────────────────────
  let _mosaicTok = 0;
  let _mosaicKey = '';
  function _fallbackTile(glyph) {
    const d = document.createElement('div');
    d.className = 'plx-tile plx-tile-ph';
    d.textContent = glyph;
    return d;
  }
  async function paintMosaic(tracks) {
    // Distinct covers in playlist order (at most 16 asked, 4 kept).
    const seen = new Set(), cands = [];
    for (const t of tracks) {
      if (!t || !t.id) continue;
      const k = _coverKey(t);
      if (seen.has(k)) continue;
      seen.add(k);
      cands.push(t);
      if (cands.length >= 16) break;
    }
    const key = cands.map((t) => t.id).join(',');
    if (key === _mosaicKey && mosaic.childElementCount) return;
    _mosaicKey = key;
    const tok = ++_mosaicTok;
    const got = [];
    for (let i = 0; i < cands.length && got.length < 4; i += 4) {
      const batch = cands.slice(i, i + 4);
      const imgs = await Promise.all(batch.map((t) => _loadImg(_artUrl(t.id))));
      if (tok !== _mosaicTok) return;              // a newer playlist / edit took over
      imgs.forEach((img, j) => { if (img && got.length < 4) got.push({ img, t: batch[j] }); });
    }
    if (tok !== _mosaicTok) return;
    mosaic.innerHTML = '';
    mosaic.dataset.tiles = String(got.length >= 4 ? 4 : got.length ? 1 : 0);
    if (got.length >= 4) {
      for (const g of got.slice(0, 4)) {
        const tile = document.createElement('div');
        tile.className = 'plx-tile';
        g.img.alt = '';
        tile.appendChild(g.img);
        mosaic.appendChild(tile);
      }
    } else if (got.length) {
      const tile = document.createElement('div');
      tile.className = 'plx-tile';
      got[0].img.alt = '';
      tile.appendChild(got[0].img);
      mosaic.appendChild(tile);
    } else {
      // No art anywhere: the playlist's main format as a glyph.
      const mix = formatMix(tracks);
      const fam = mix.length ? mix[0].family : 'other';
      const glyph = tracks.length ? (_PH_GLYPH[fam] || artPlaceholderEmoji(tracks[0])) : '♪';
      mosaic.appendChild(_fallbackTile(glyph));
    }
    const tint = got.length ? _avgColour(got.slice(0, 4).map((g) => g.img)) : null;
    if (tint) el.style.setProperty('--plx-tint', tint); else el.style.removeProperty('--plx-tint');
  }

  // ── Render ───────────────────────────────────────────────────────────────
  /** ``info``: {name, tracks, kicker, playingHere, playing, style} */
  function render(info) {
    const tracks = info.tracks || [];
    el.hidden = false;
    kicker.textContent = info.kicker || 'Playlist';
    kicker.title = kicker.textContent;
    nameEl.textContent = info.name || '';
    nameEl.title = info.name || '';
    const n = tracks.length;
    countEl.textContent = `${n} track${n !== 1 ? 's' : ''}`;
    const { sec, unknown } = totalSeconds(tracks);
    const lenTxt = n ? (_fmtTotal(sec) + (unknown && sec ? '+' : '')) : '';
    lenEl.textContent = lenTxt;
    lenEl.hidden = !lenTxt || !sec;
    lenEl.title = unknown ? `${unknown} track${unknown !== 1 ? 's' : ''} without a known length` : '';
    // Retro: the total on a tape counter (aria-hidden — .plx-len says it).
    const digits = sec ? _counterDigits(sec) : '';
    counterEl.innerHTML = '';
    for (const ch of digits) {
      const s = document.createElement('span');
      s.className = ch === ':' ? 'plx-cnt-sep' : 'plx-cnt-dig';
      s.textContent = ch;
      counterEl.appendChild(s);
    }
    counterEl.hidden = !digits;

    const mix = formatMix(tracks);
    const top = mix.slice(0, 3).map((g) => `${g.label} ${g.count}`);
    if (mix.length > 3) top.push(`+${mix.length - 3} more`);
    mixText.textContent = top.join(' · ');
    mixText.hidden = !top.length;
    // The bar: one coloured segment per format family (largest first), its
    // tooltip naming the formats in it.
    mixBar.innerHTML = '';
    const segs = familyMix(mix);
    for (const g of segs) {
      const s = document.createElement('span');
      s.className = `plx-seg plx-fam-${g.family}`;
      s.style.flexGrow = String(g.count);
      s.dataset.count = String(g.count);
      s.title = `${FAMILY_NAMES[g.family] || g.family}: ${g.labels.map((l) => `${l.label} ${l.count}`).join(', ')}`;
      mixBar.appendChild(s);
    }
    mixBar.hidden = !segs.length;
    mixBar.setAttribute('aria-label', 'Formats: ' + mix.map((g) => `${g.label} ${g.count}`).join(', '));

    playBtn.disabled = !n;
    shufBtn.disabled = n < 2;
    setPlaying(info.playingHere, info.playing);
    paintMosaic(tracks);
  }

  function setPlaying(here, playing) {
    const pause = !!(here && playing);
    playBtn.classList.toggle('is-playing', pause);
    playLbl.textContent = pause ? 'Pause' : 'Play';
    playBtn.setAttribute('aria-label', pause ? 'Pause' : 'Play');
  }

  function hide() { el.hidden = true; closeMenu(); }

  return { el, render, setPlaying, hide, closeMenu };
}
