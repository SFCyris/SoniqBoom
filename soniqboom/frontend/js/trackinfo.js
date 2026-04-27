// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * trackinfo.js — iTunes-style track info panel.
 *
 * Shows all metadata, artwork, and audio file details for a track.
 * Navigate between tracks with ◀ ▶ buttons, keyboard arrows, or swipe.
 *
 * Usage:
 *   TrackInfo.open(track, queue, idx)  — open panel for track at idx in queue
 *   TrackInfo.openSingle(track)        — open panel for a single track
 */
import { Player }              from './player.js';
import { artPlaceholderEmoji } from './utils.js';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const overlay      = document.getElementById('ti-overlay');
const panel        = document.getElementById('ti-panel');
const btnClose     = document.getElementById('ti-close');
const btnPrev      = document.getElementById('ti-prev');
const btnNext      = document.getElementById('ti-next');
const navLabel     = document.getElementById('ti-nav-label');
const artEl        = document.getElementById('ti-art');
const artImg       = document.getElementById('ti-art-img');
const artPhEl      = document.getElementById('ti-art-ph');

// Tabs
const tabInfo     = document.getElementById('ti-tab-info');
const tabLyrics   = document.getElementById('ti-tab-lyrics');
const metaPane    = document.getElementById('ti-meta-pane');
const lyricsPane  = document.getElementById('ti-lyrics-pane');
const lyricsState = document.getElementById('ti-lyrics-state');

// ── State ─────────────────────────────────────────────────────────────────────
let _queue       = [];
let _idx         = 0;
let _activeTab   = 'info';   // 'info' | 'lyrics'
let _lyricsCache = {};        // track_id → {lyrics, synced, source, lines} | 'loading' | 'error'
let _syncedLines = [];        // [{time: seconds, text: '...'}, ...]
let _activeLine  = -1;        // index of currently highlighted line

// ── Format helpers ────────────────────────────────────────────────────────────
function _fmt(sec) {
  if (!sec || !isFinite(sec)) return '—';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}
function _fmtSize(bytes) {
  if (!bytes) return '—';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.round(bytes / 1e3)} KB`;
}
function _fmtRate(hz) {
  if (!hz) return '—';
  return `${(hz / 1000).toFixed(hz % 1000 === 0 ? 0 : 1)} kHz`;
}
function _fmtBitrate(bps) {
  if (!bps) return '—';
  // Lossless files report bitrate in bits/s (e.g. 16934400 for ALAC)
  const kbps = Math.round(bps / 1000);
  return kbps > 9999
    ? `${(kbps / 1000).toFixed(1)} Mbps`   // lossless: e.g. "16.9 Mbps"
    : `${kbps} kbps`;
}
function _fmtChannels(n) {
  return { 1: 'Mono', 2: 'Stereo', 6: '5.1 Surround', 8: '7.1 Surround' }[n] ?? (n ? `${n}ch` : '—');
}
function _fmtDate(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
function _val(v) { return (v != null && v !== '' && !(Array.isArray(v) && !v.length)) ? v : null; }
function _show(id, val, formatter) {
  const el = document.getElementById(id);
  if (!el) return;
  const raw = _val(val);
  const text = raw != null ? (formatter ? formatter(raw) : String(raw)) : null;
  el.textContent = text ?? '—';
  el.classList.toggle('ti-empty', !text);
}

// ── Render one track ──────────────────────────────────────────────────────────
function _render(track) {
  if (!track) return;

  // ── Artwork ──
  artImg.src = '';
  artImg.style.display = 'none';
  artEl.classList.add('ti-art-loading');
  artEl.classList.remove('ti-has-art');

  // Set format-appropriate placeholder emoji while art loads (or if no art)
  if (artPhEl) artPhEl.textContent = artPlaceholderEmoji(track);

  const img = new Image();
  img.onload = () => {
    artImg.src = img.src;
    artImg.style.display = 'block';
    artEl.classList.remove('ti-art-loading');
    artEl.classList.add('ti-has-art');
  };
  img.onerror = () => {
    artEl.classList.remove('ti-art-loading');
    // Placeholder emoji (above) is shown automatically when ti-has-art is absent
  };
  img.src = `/api/art/${track.id}`;

  // ── Navigation label ──
  const title = track.title || track.path?.split('/').pop() || '—';
  navLabel.textContent = `Track ${_idx + 1} of ${_queue.length}  ·  ${title}`;
  btnPrev.disabled = _idx === 0;
  btnNext.disabled = _idx >= _queue.length - 1;
  btnPrev.classList.toggle('ti-btn-disabled', _idx === 0);
  btnNext.classList.toggle('ti-btn-disabled', _idx >= _queue.length - 1);

  // ── TRACK INFO fields ──
  _show('ti-title',        track.title);
  _show('ti-artist',       track.artist);
  _show('ti-album-artist', track.album_artist);
  _show('ti-album',        track.album);
  _show('ti-composer',     track.composer);
  _show('ti-year',         track.year);

  // ── NUMBERING ──
  const trkStr = track.track_number
    ? `${track.track_number}${track.total_tracks ? ' of ' + track.total_tracks : ''}`
    : null;
  const discStr = track.disc_number
    ? `${track.disc_number}${track.total_discs ? ' of ' + track.total_discs : ''}`
    : null;
  _show('ti-track', trkStr);
  _show('ti-disc',  discStr);

  // ── DETAILS ──
  const genre = Array.isArray(track.genre) ? track.genre.join(', ') : track.genre;
  _show('ti-genre',    genre);
  _show('ti-duration', track.duration, _fmt);
  _show('ti-bpm',      track.bpm);
  _show('ti-comment',  track.comment);
  _show('ti-isrc',     track.isrc);
  _show('ti-label',    track.label);

  // ── FILE ──
  _show('ti-format',      track.format);
  _show('ti-bit-depth',   track.bit_depth,   v => `${v}-bit`);
  _show('ti-sample-rate', track.sample_rate, _fmtRate);
  _show('ti-channels',    track.channels,    _fmtChannels);
  _show('ti-bitrate',     track.bitrate,     _fmtBitrate);
  _show('ti-file-size',   track.file_size,   _fmtSize);
  _show('ti-added',       track.added_at,    _fmtDate);
  _show('ti-path',        track.path);

  // Reset lyrics pane if navigating away from current lyrics
  if (_activeTab === 'lyrics') {
    _loadLyrics(track);
  } else {
    // Reset to "not loaded" state so it fetches fresh on tab switch
    lyricsPane.innerHTML = '<div id="ti-lyrics-state" class="ti-lyrics-state"></div>';
  }

  // Load extended module/SID/MIDI info
  _loadExtendedInfo(track);
}

// ── Module / SID / MIDI extended info ─────────────────────────────────────────
const _MODULE_FORMATS = new Set([
    'SID', 'MIDI', 'ProTracker', 'ScreamTracker 3', 'FastTracker 2',
    'Impulse Tracker', 'MultiTracker', 'OctaMED', 'Composer 669',
    'DigiBooster Pro', 'AHX', 'HivelyTracker', 'UltraTracker',
    'ScreamTracker 2', 'Farandole', 'ASYLUM/DMP', 'General DigiMusic',
    'Imago Orpheus', 'Oktalyzer', 'SoundFX', 'Grave Composer', 'DSIK',
]);

async function _loadExtendedInfo(track) {
    const section = document.getElementById('ti-section-module');
    if (!section) return;

    if (!_MODULE_FORMATS.has(track.format)) {
        section.style.display = 'none';
        return;
    }

    section.style.display = '';
    // Set section header based on format type
    const hdr = section.querySelector('.ti-section-hdr');
    if (track.format === 'SID') hdr.textContent = 'SID Details';
    else if (track.format === 'MIDI') hdr.textContent = 'MIDI Details';
    else hdr.textContent = 'Module Details';

    try {
        const res = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/extended`);
        const data = await res.json();

        // Channels
        const chField = document.getElementById('ti-field-channels-ext');
        const chEl = document.getElementById('ti-channels-ext');
        if (data.channels) {
            chEl.textContent = data.channels;
            chField.style.display = '';
        } else {
            chField.style.display = 'none';
        }

        // Patterns
        const patField = document.getElementById('ti-field-patterns');
        const patEl = document.getElementById('ti-patterns');
        if (data.patterns) {
            patEl.textContent = data.patterns;
            patField.style.display = '';
        } else {
            patField.style.display = 'none';
        }

        // Subsongs
        const subField = document.getElementById('ti-field-subsongs');
        const subEl = document.getElementById('ti-subsongs');
        if (data.subsongs && data.subsongs > 1) {
            subEl.textContent = data.subsongs;
            subField.style.display = '';
        } else {
            subField.style.display = 'none';
        }

        // Instruments
        const instField = document.getElementById('ti-field-instruments');
        const instList = document.getElementById('ti-instrument-list');
        if (data.instruments && data.instruments.length) {
            instList.innerHTML = data.instruments
                .map((name, i) => `<div class="ti-instrument">${i + 1}. ${_escHtml(name)}</div>`)
                .join('');
            instField.style.display = '';
        } else {
            instField.style.display = 'none';
        }
    } catch {
        section.style.display = 'none';
    }
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function _switchTab(tab) {
  _activeTab = tab;
  tabInfo.classList.toggle('active', tab === 'info');
  tabLyrics.classList.toggle('active', tab === 'lyrics');
  metaPane.classList.toggle('hidden', tab !== 'info');
  lyricsPane.classList.toggle('active', tab === 'lyrics');
  if (tab === 'lyrics') _loadLyrics(_queue[_idx]);
}

tabInfo.addEventListener('click',   () => _switchTab('info'));
tabLyrics.addEventListener('click', () => _switchTab('lyrics'));

// ── Lyrics loading ────────────────────────────────────────────────────────────
function _setLyricsState(html) {
  // Remove any existing lyrics text node and source node, restore state div
  lyricsPane.innerHTML = `<div id="ti-lyrics-state" class="ti-lyrics-state">${html}</div>`;
}

function _parseLRC(text) {
  const lines = [];
  for (const line of text.split('\n')) {
    const m = line.match(/^\[(\d{1,2}):(\d{2})[.:](\d{2,3})\]\s*(.*)/);
    if (m) {
      const min = parseInt(m[1], 10);
      const sec = parseInt(m[2], 10);
      const ms  = m[3].length === 2 ? parseInt(m[3], 10) * 10 : parseInt(m[3], 10);
      lines.push({ time: min * 60 + sec + ms / 1000, text: m[4] });
    }
  }
  return lines.sort((a, b) => a.time - b.time);
}

function _showLyrics(data) {
  _syncedLines = [];
  _activeLine  = -1;

  if (data.synced) {
    _syncedLines = _parseLRC(data.lyrics);
    if (_syncedLines.length) {
      lyricsPane.innerHTML = `
        <div class="ti-lyrics-synced" id="ti-lyrics-synced">
          ${_syncedLines.map((l, i) =>
            `<div class="lrc-line" data-idx="${i}">${_escHtml(l.text) || '&nbsp;'}</div>`
          ).join('')}
        </div>
        <div class="ti-lyrics-source">${_escHtml(data.source)}</div>`;
      return;
    }
  }

  // Plain lyrics fallback
  lyricsPane.innerHTML = `
    <div class="ti-lyrics-text">${_escHtml(data.lyrics)}</div>
    <div class="ti-lyrics-source">${_escHtml(data.source)}</div>`;
}

function _escHtml(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function _loadLyrics(track) {
  if (!track) { _setLyricsState('No track selected.'); return; }
  const id = track.id;
  const cached = _lyricsCache[id];
  if (cached === 'loading') return;
  if (cached && cached !== 'error') { _showLyrics(cached); return; }

  _lyricsCache[id] = 'loading';
  _setLyricsState('<div class="ti-lyrics-spinner"></div>Fetching lyrics…');

  try {
    const res  = await fetch(`/api/tracks/${encodeURIComponent(id)}/lyrics`);
    const data = await res.json();
    if (data.lyrics) {
      _lyricsCache[id] = data;
      if (_queue[_idx]?.id === id && _activeTab === 'lyrics') {
        _showLyrics(data);
      }
    } else {
      _lyricsCache[id] = 'error';
      if (_queue[_idx]?.id === id && _activeTab === 'lyrics') {
        _setLyricsState('No lyrics found for this track.');
      }
    }
  } catch {
    _lyricsCache[id] = 'error';
    if (_queue[_idx]?.id === id && _activeTab === 'lyrics') {
      _setLyricsState('Could not load lyrics.');
    }
  }
}

// ── Synced lyrics highlight on timeupdate ─────────────────────────────────────
function _updateSyncedLine(currentTime) {
  if (!_syncedLines.length || _activeTab !== 'lyrics') return;
  // Find the last line whose time <= currentTime
  let idx = -1;
  for (let i = _syncedLines.length - 1; i >= 0; i--) {
    if (_syncedLines[i].time <= currentTime) { idx = i; break; }
  }
  if (idx === _activeLine) return;
  _activeLine = idx;

  const container = document.getElementById('ti-lyrics-synced');
  if (!container) return;
  container.querySelectorAll('.lrc-line.active').forEach(el => el.classList.remove('active'));
  if (idx >= 0) {
    const el = container.querySelector(`.lrc-line[data-idx="${idx}"]`);
    if (el) {
      el.classList.add('active');
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }
}

Player.on('timeupdate', ({ current }) => {
  if (isOpen()) _updateSyncedLine(current);
});

// ── Navigation ────────────────────────────────────────────────────────────────
function _go(dir) {
  const next = _idx + dir;
  if (next < 0 || next >= _queue.length) return;
  _idx = next;
  _render(_queue[_idx]);
}

btnPrev.addEventListener('click', () => _go(-1));
btnNext.addEventListener('click', () => _go(+1));

// ── Open / close ──────────────────────────────────────────────────────────────
function open(queue, idx) {
  _queue = Array.isArray(queue) ? queue : [queue];
  _idx   = Math.max(0, Math.min(_queue.length - 1, idx ?? 0));
  // Always open on Info tab
  _switchTab('info');
  overlay.classList.remove('hidden');
  document.body.classList.add('ti-open');
  _render(_queue[_idx]);
}

function openSingle(track) {
  open([track], 0);
}

function close() {
  overlay.classList.add('hidden');
  document.body.classList.remove('ti-open');
}

function isOpen() { return !overlay.classList.contains('hidden'); }

// ── Keyboard ──────────────────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  if (!isOpen()) return;
  if (e.key === 'Escape')     { close(); return; }
  if (e.key === 'ArrowLeft')  { _go(-1); e.preventDefault(); }
  if (e.key === 'ArrowRight') { _go(+1); e.preventDefault(); }
});

// ── Close on backdrop click ───────────────────────────────────────────────────
overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
btnClose.addEventListener('click', close);

export const TrackInfo = { open, openSingle, close, isOpen };
