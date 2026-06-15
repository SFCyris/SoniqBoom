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
import { artPlaceholderEmoji, trapFocus } from './utils.js';
import { mountSignalChain }    from './viz/signalchain.js';
import { vizGroupEnabled }     from './viz/engine.js';

// ── Chapters (E-18) ────────────────────────────────────────────────────────

// AbortControllers for the three track-info panel fetches.  Switching the
// panel's track (open / prev / next / re-open) aborts the previous track's
// in-flight loads so they release their browser connection slot instead of
// racing the new track's data in.  (Folder navigation does NOT abort these —
// the panel may still be showing this track and legitimately wants its data.)
let _chaptersAbort = null;
let _extendedAbort = null;
let _lyricsAbort   = null;

async function _loadChapters(track) {
  const host = document.getElementById('ti-chapters');
  if (!host) return;
  host.innerHTML = '';
  host.hidden = true;
  if (!track || !track.id) return;
  if (_chaptersAbort) { try { _chaptersAbort.abort(); } catch (_) {} }
  const _c = (typeof AbortController === 'function') ? new AbortController() : null;
  _chaptersAbort = _c;
  try {
    const res = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/chapters`,
                            _c ? { credentials: 'same-origin', signal: _c.signal }
                               : { credentials: 'same-origin' });
    if (!res.ok) return;
    const { chapters } = await res.json();
    if (!Array.isArray(chapters) || chapters.length === 0) return;
    const header = document.createElement('div');
    header.className = 'ti-chapter-header';
    header.textContent = `Chapters (${chapters.length})`;
    const list = document.createElement('div');
    list.className = 'ti-chapter-list';
    list.setAttribute('role', 'list');
    list.setAttribute('aria-label', 'Chapters');
    chapters.forEach((ch, i) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'ti-chapter-row';
      row.setAttribute('role', 'listitem');
      const start = Number(ch.start) || 0;
      const mm = Math.floor(start / 60);
      const ss = String(Math.floor(start % 60)).padStart(2, '0');
      const label = ch.title || `Chapter ${i + 1}`;
      const timeEl = document.createElement('span');
      timeEl.className = 'ti-chapter-time';
      timeEl.textContent = `${mm}:${ss}`;
      const titleEl = document.createElement('span');
      titleEl.className = 'ti-chapter-title';
      titleEl.textContent = label;
      row.append(timeEl, titleEl);
      row.addEventListener('click', () => {
        const cur = Player.currentTrack;
        if (cur && cur.id === track.id) {
          Player.seek(start);
        } else {
          Player.playTrack(track);
          setTimeout(() => Player.seek(start), 400);
        }
      });
      list.appendChild(row);
    });
    host.append(header, list);
    host.hidden = false;
  } catch { /* silent — best-effort (incl. AbortError on track switch) */ }
  finally { if (_chaptersAbort === _c) _chaptersAbort = null; }
}

// ── Signal-path viz (#4) ──────────────────────────────────────────────────
let _signalChain = null;     // handle from mountSignalChain
let _sigTrack = null;        // the track the chain is currently rendering

function _mountSignalChainFor(track) {
  _sigTrack = track;
  const section = document.getElementById('ti-section-signal');
  const host = document.getElementById('ti-signal-chain');
  if (!section || !host) return;
  if (!vizGroupEnabled('nowPlaying')) {
    // Group off (or reduced-motion handled inside the engine) — hide section.
    section.hidden = true;
    if (_signalChain) { _signalChain.unregister(); _signalChain = null; host.textContent = ''; }
    return;
  }
  section.hidden = false;
  if (!_signalChain) {
    _signalChain = mountSignalChain(host, () => ({
      format: _sigTrack?.format || '',
      playing: !!(Player && Player.playing),
    }));
  } else {
    _signalChain.rebuild();
  }
}

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
let _focusReturn = null;      // Element that had focus when the panel opened

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

async function _loadArtistAbout(track) {
  const host = document.getElementById('ti-artist-about');
  if (!host) return;
  host.hidden = true; host.innerHTML = '';
  const artist = ((track && track.artist) || '').trim();
  if (!artist) return;
  try {
    // Album/track context lets the server pin the right artist for ambiguous
    // names ("Ghost" the band on this record, not anything else).
    let q = '/api/artist/info?name=' + encodeURIComponent(artist);
    if (track.album) q += '&album=' + encodeURIComponent(track.album);
    if (track.title) q += '&track=' + encodeURIComponent(track.title);
    const r = await fetch(q);
    if (!r.ok) return;
    const info = await r.json();
    if (!info || !info.found || !info.bio) return;
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
    const full = info.bio;
    const bio = full.length > 420 ? full.slice(0, 420).replace(/\s+\S*$/, '') + '…' : full;
    const img = info.image
      ? `<img src="${esc(info.image)}" alt="${esc(info.title || artist)}" loading="lazy" decoding="async" style="float:left;width:72px;height:72px;object-fit:cover;border-radius:8px;margin:0 12px 8px 0">`
      : '';
    const link = info.url
      ? ` <a href="${esc(info.url)}" target="_blank" rel="noopener" style="white-space:nowrap">Read on ${esc(info.source || 'Wikipedia')} ▸</a>`
      : '';
    host.innerHTML =
      `<h4 style="margin:0 0 6px;font-size:13px;opacity:.85">About ${esc(info.title || artist)}</h4>` +
      `<div style="overflow:hidden;font-size:12.5px;line-height:1.55">${img}<span>${esc(bio)}</span>${link}</div>`;
    host.hidden = false;
  } catch (e) { /* network/parse issue — leave hidden */ }
}

// ── Tag editing ────────────────────────────────────────────────────────────────
const _TAG_FIELDS = [
  ['title', 'Title'], ['artist', 'Artist'], ['album', 'Album'],
  ['album_artist', 'Album artist'], ['genre', 'Genre'], ['year', 'Year'],
];
function _renderTagEdit(track) {
  const wrap = document.getElementById('ti-tagedit-wrap');
  if (!wrap) return;
  wrap.innerHTML = '';
  if (!track || !track.id) return;
  const remote = /^(smb|ftp|https?):\/\//.test(track.path || '');
  const btn = document.createElement('button');
  btn.id = 'ti-edit-tags';
  btn.textContent = '✏️ Edit tags';
  btn.style.cssText = 'font-size:12px;padding:4px 10px;opacity:.85';
  if (remote) {
    btn.disabled = true;
    btn.title = 'Tags can only be edited on local files — this track lives on a network share.';
  }
  btn.addEventListener('click', () => _showTagForm(track, wrap));
  wrap.appendChild(btn);
}
function _showTagForm(track, wrap) {
  wrap.innerHTML = '';
  const form = document.createElement('div');
  form.style.cssText = 'display:grid;grid-template-columns:90px 1fr;gap:6px 10px;align-items:center;font-size:12.5px';
  const inputs = {};
  for (const [key, label] of _TAG_FIELDS) {
    const lab = document.createElement('label');
    lab.textContent = label;
    lab.style.opacity = '.7';
    const inp = document.createElement('input');
    inp.type = key === 'year' ? 'number' : 'text';
    inp.value = key === 'genre'
      ? (Array.isArray(track.genre) ? track.genre.join(', ') : (track.genre || ''))
      : (track[key] ?? '');
    inp.style.cssText = 'font-size:12.5px;padding:4px 8px;min-width:0';
    inputs[key] = inp;
    form.appendChild(lab); form.appendChild(inp);
  }
  const row = document.createElement('div');
  row.style.cssText = 'grid-column:1/-1;display:flex;gap:8px;margin-top:4px';
  const save = document.createElement('button');
  save.textContent = 'Save tags';
  save.style.cssText = 'font-size:12px;padding:4px 12px';
  const cancel = document.createElement('button');
  cancel.textContent = 'Cancel';
  cancel.style.cssText = 'font-size:12px;padding:4px 12px;opacity:.7';
  cancel.addEventListener('click', () => _renderTagEdit(track));
  save.addEventListener('click', async () => {
    const body = {};
    for (const [key] of _TAG_FIELDS) {
      let v = inputs[key].value.trim();
      if (key === 'genre') {
        const cur = Array.isArray(track.genre) ? track.genre.join(', ') : (track.genre || '');
        // Send the full string — "Rock, Pop" round-trips as one tag value
        // rather than silently dropping everything after the first comma.
        if (v !== cur && v) body.genre = v;
      } else if (key === 'year') {
        const n = parseInt(v, 10);
        if (v && n && n !== track.year) body.year = n;
      } else if (v && v !== (track[key] || '')) {
        body[key] = v;
      }
    }
    if (!Object.keys(body).length) { _renderTagEdit(track); return; }
    save.disabled = true; save.textContent = 'Saving…';
    try {
      const r = await fetch(`/api/tracks/${track.id}/tags`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = 'Could not save tags.';
        try { msg = (await r.json()).detail || msg; } catch (_) {}
        throw new Error(msg);
      }
      const res = await r.json();
      Object.assign(track, res.applied || {});
      if (res.applied && res.applied.genre) track.genre = [res.applied.genre];
      window.Toast?.ok?.('Tags saved — file and library updated.');
      _render(track);
    } catch (e) {
      window.Toast?.error?.(e.message || 'Could not save tags.');
      save.disabled = false; save.textContent = 'Save tags';
    }
  });
  row.appendChild(save); row.appendChild(cancel);
  form.appendChild(row);
  wrap.appendChild(form);
  inputs.title?.focus();
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

  // Request the ``lg`` thumbnail rather than the raw embedded JPEG —
  // tracks with high-res cover art (1400×1248+) would otherwise download
  // several MB into a ~260×260 dialog box.  The ``lg`` cache is shared
  // with the now-playing overlay so the bytes are typically already on
  // disk by the time the dialog opens.
  //
  // ``fallback=404`` tells the server to return a cacheable 404 (no
  // body) when there's no real art, so the IMG.onerror fires and we
  // keep the format-specific emoji visible.  We previously tried to
  // read the ``X-SoniqBoom-Art`` header via fetch+blob, which forced
  // a full body download per modal open and silently regressed
  // perceived modal latency (D14).
  const img = new Image();
  img.decoding = 'async';
  const reqTrackId = track.id;
  img.onload = () => {
    if (_queue[_idx]?.id !== reqTrackId) return;
    artImg.src = img.src;
    artImg.style.display = 'block';
    artEl.classList.remove('ti-art-loading');
    artEl.classList.add('ti-has-art');
  };
  img.onerror = () => {
    if (_queue[_idx]?.id !== reqTrackId) return;
    artEl.classList.remove('ti-art-loading');
    // Placeholder emoji (above) shows automatically when
    // ``ti-has-art`` is absent.
  };
  img.src = `/api/art/${track.id}?size=lg&fallback=404`;

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

  // ── Chapters (podcast/audiobook) — appended after the regular tags
  // when the file has them.  Click jumps the player.
  _loadChapters(track);
  _loadArtistAbout(track);
  _renderTagEdit(track);

  // Reset lyrics pane if navigating away from current lyrics
  if (_activeTab === 'lyrics') {
    _loadLyrics(track);
  } else {
    // Reset to "not loaded" state so it fetches fresh on tab switch
    lyricsPane.innerHTML = '<div id="ti-lyrics-state" class="ti-lyrics-state"></div>';
  }

  // Load extended module/SID/MIDI info
  _loadExtendedInfo(track);

  // Signal-path viz (#4): per-format decode pipeline.  Mounted lazily and
  // gated on the now-playing viz group.  ``getState`` reads the DISPLAYED
  // track's format (illustrative of how that format decodes) and the global
  // play state (the signal flows while audio plays, freezes when paused).
  _mountSignalChainFor(track);

  // Notify app.js which track this modal is currently DISPLAYING
  // (which may not be the track currently PLAYING — the user can
  // browse with the ◀ ▶ navigation buttons).  app.js uses this to
  // decide whether to park the VU/FFT overlay on the modal's cover
  // art: only when displayed == playing, otherwise the overlay
  // shows the wrong track's analysis (e.g. a SID's FFT spectrum
  // sitting on an XM's info card, which the user pointed out as a
  // visual lie).
  try {
    overlay.dispatchEvent(new CustomEvent('trackinfo:render', {
      detail: { trackId: track?.id || null }
    }));
  } catch (_) {}
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

    if (_extendedAbort) { try { _extendedAbort.abort(); } catch (_) {} }
    const _c = (typeof AbortController === 'function') ? new AbortController() : null;
    _extendedAbort = _c;
    try {
        const res = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/extended`,
                                _c ? { signal: _c.signal } : undefined);
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
    } catch (e) {
        if (e && e.name === 'AbortError') return;   // track switched; new load owns the UI
        section.style.display = 'none';
    } finally {
        if (_extendedAbort === _c) _extendedAbort = null;
    }
}

// ── Tab switching ─────────────────────────────────────────────────────────────
// Mark the panes as tabpanels once at startup so screen readers see the
// pair as a real tab/tabpanel relationship instead of two unrelated divs.
if (metaPane && lyricsPane) {
  metaPane.setAttribute('role', 'tabpanel');
  metaPane.setAttribute('aria-labelledby', 'ti-tab-info');
  metaPane.tabIndex = 0;
  lyricsPane.setAttribute('role', 'tabpanel');
  lyricsPane.setAttribute('aria-labelledby', 'ti-tab-lyrics');
  lyricsPane.tabIndex = 0;
}

function _switchTab(tab) {
  _activeTab = tab;
  tabInfo.classList.toggle('active', tab === 'info');
  tabLyrics.classList.toggle('active', tab === 'lyrics');
  tabInfo.setAttribute('aria-selected',   tab === 'info'   ? 'true' : 'false');
  tabLyrics.setAttribute('aria-selected', tab === 'lyrics' ? 'true' : 'false');
  // Single show/hide convention: both panes default to ``hidden`` and we
  // add ``active`` for the visible one.  Previously meta used ``hidden``
  // while lyrics used ``active``, so during the open animation both
  // panes could be visible (Visual-Test #1 caught the race).
  metaPane.classList.toggle('active', tab === 'info');
  metaPane.classList.toggle('hidden', tab !== 'info');
  lyricsPane.classList.toggle('active', tab === 'lyrics');
  lyricsPane.classList.toggle('hidden', tab !== 'lyrics');
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
      // Prime the active line immediately so the user sees the current
      // verse highlighted the moment lyrics load — previously this had to
      // wait for the next ``timeupdate`` tick (up to 250 ms) which felt
      // like the lyrics were "behind" the audio (REG-3).
      try { _updateSyncedLine(Player.currentTime); } catch (_) {}
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
  if (_lyricsAbort) { try { _lyricsAbort.abort(); } catch (_) {} }
  const _lc = (typeof AbortController === 'function') ? new AbortController() : null;
  _lyricsAbort = _lc;

  try {
    const res  = await fetch(`/api/tracks/${encodeURIComponent(id)}/lyrics`,
                             _lc ? { signal: _lc.signal } : undefined);
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
  } catch (e) {
    if (e && e.name === 'AbortError') {
      // Track switched mid-fetch — drop the 'loading' marker so a later
      // revisit re-fetches instead of sticking on the spinner forever.
      delete _lyricsCache[id];
      return;
    }
    _lyricsCache[id] = 'error';
    if (_queue[_idx]?.id === id && _activeTab === 'lyrics') {
      _setLyricsState('Could not load lyrics.');
    }
  } finally {
    if (_lyricsAbort === _lc) _lyricsAbort = null;
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
      // ``smooth`` adds a ~300 ms scroll animation per line, which on fast
      // verses (rap, chiptune) cumulatively makes the visible highlight
      // trail the audio.  Instant scroll keeps the active verse pinned to
      // centre without the trailing animation; the ``transition`` on
      // ``.lrc-line.active`` still gives the colour fade (REG-3).
      el.scrollIntoView({ block: 'center', behavior: 'auto' });
    }
  }
}

Player.on('timeupdate', ({ current }) => {
  if (isOpen()) _updateSyncedLine(current);
});

// Re-sync the active lyric line the instant a seek lands, instead of
// waiting for the next ``timeupdate`` tick (~250 ms).  Fixes the
// perception that lyrics drift after scrubbing the seek bar (REG-3).
Player.on('seeked', ({ current }) => {
  if (isOpen()) {
    // Force-refresh the highlight by invalidating the cached line index.
    _activeLine = -1;
    _updateSyncedLine(current);
  }
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
// trapFocus release callback held across open/close so close() can let go.
let _focusTrapRelease = null;

function open(queue, idx) {
  // ``queue`` can be:
  //   1. A plain Array (small-library views, group views, Player.queue)
  //   2. A WindowedTrackStore Proxy (the All Tracks view above 5,000
  //      tracks).  The proxy implements ``.length`` and numeric indexing
  //      so it walks like an array — but ``Array.isArray`` returns false
  //      on it, so the old code wrapped it as ``[proxy]`` and tried to
  //      render the proxy itself as a track, which produced an info
  //      panel with "Track 1 of 1 · —" and every field empty.
  //   3. A single track object (legacy callers; openSingle path)
  // Accept (1) and (2) as-is; only wrap (3).
  const isArrayLike =
    Array.isArray(queue) ||
    (queue && queue._isWindowedStore);
  _queue = isArrayLike ? queue : [queue];
  _idx   = Math.max(0, Math.min(_queue.length - 1, idx ?? 0));
  // Capture the previously-focused element so close() can restore focus
  // there — keyboard users expect to land back at the row/button they
  // activated, not on document.body.  Skip null/body to avoid sending
  // focus to no-op targets.
  const prev = document.activeElement;
  _focusReturn = (prev && prev !== document.body) ? prev : null;
  // Always open on Info tab
  _switchTab('info');
  overlay.classList.remove('hidden');
  document.body.classList.add('ti-open');
  // Notify app.js so it can reparent the VU/FFT meters onto the
  // cover-art box as a spectrum overlay (app.js _placeVUContainer).
  // The event is fired AFTER the ``hidden`` class is removed so the
  // listener sees the open state.
  try { overlay.dispatchEvent(new CustomEvent('trackinfo:open')); } catch (_) {}

  // Windowed store: nudge the chunk containing this index in case the
  // LRU has evicted it.  ``ensureRange`` is async fire-and-forget; the
  // synchronous render below sees whatever's already cached.  If the
  // chunk hasn't arrived yet we poll up to ~3 seconds for it to land
  // and re-render once.  We deliberately don't hook the store's
  // ``setOnChunkLoad`` because library.js already owns that single slot
  // (table virtual-scroll repaint).
  if (_queue._isWindowedStore && typeof _queue.ensureRange === 'function') {
    try { _queue.ensureRange(_idx, _idx + 1); } catch (_) {}
  }

  let t = _queue[_idx];
  _render(t);

  if (!t && _queue._isWindowedStore) {
    // Poll briefly for the chunk to arrive; bail when it lands or the
    // panel closes / navigates away.  6 retries × 500ms = 3 s budget,
    // matches the user's tolerance for "did the click do something?".
    const capturedIdx = _idx;
    let tries = 0;
    const pump = setInterval(() => {
      tries += 1;
      if (!isOpen() || _idx !== capturedIdx || tries > 6) {
        clearInterval(pump);
        return;
      }
      const arrived = _queue[capturedIdx];
      if (arrived) {
        clearInterval(pump);
        _render(arrived);
      }
    }, 500);
  }
  // Trap focus inside the panel so Tab doesn't escape into the dimmed
  // app behind (WCAG 2.4.3).  Defer to next tick so the just-revealed
  // overlay's focusable elements are queryable.
  try {
    if (_focusTrapRelease) { _focusTrapRelease(); _focusTrapRelease = null; }
    requestAnimationFrame(() => {
      try { _focusTrapRelease = trapFocus(panel || overlay); }
      catch (_) { _focusTrapRelease = null; }
    });
  } catch (_) {}
}

function openSingle(track) {
  open([track], 0);
}

function close() {
  // Release the focus trap BEFORE moving focus, otherwise the trap's
  // refocus-on-blur logic fights the restore.
  if (_focusTrapRelease) {
    try { _focusTrapRelease(); } catch (_) {}
    _focusTrapRelease = null;
  }
  overlay.classList.add('hidden');
  document.body.classList.remove('ti-open');
  // Dispatch AFTER the ``hidden`` class lands so app.js's
  // _placeVUContainer reads ``modalOpen=false`` and returns the
  // VU meters to the player bar.  If we dispatched first the
  // listener would still see ``modalOpen=true`` and skip the
  // reparent — verified in preview.
  try { overlay.dispatchEvent(new CustomEvent('trackinfo:close')); } catch (_) {}
  // Restore focus to the element that opened the panel.  Guarded against
  // the element being removed from the DOM in the meantime (defensive —
  // ``focus`` is a no-op on detached nodes but we don't want to throw if
  // the host is null / undefined).
  if (_focusReturn && typeof _focusReturn.focus === 'function') {
    try { _focusReturn.focus(); } catch (_) {}
  }
  _focusReturn = null;
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
