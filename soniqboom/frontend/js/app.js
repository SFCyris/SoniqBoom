// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * app.js — Bootstrap: wires all modules together, binds global UI events.
 */
import { Player }     from './player.js';
import { Library }    from './library.js';
import { Search }     from './search.js';
import { Visualizer } from './visualizer.js';
import { FolderTree } from './foldertree.js';
import { Admin }      from './admin.js';
import { Equalizer }  from './equalizer.js';
import { TrackInfo }  from './trackinfo.js';
import { Queue }      from './queue.js';
import { Playlist }   from './playlist.js';
import { artPlaceholderEmoji, TRACKER_FORMAT_NAMES } from './utils.js';

// ── Player bar UI bindings ────────────────────────────────────────────────────
const btnPlay    = document.getElementById('btn-play');
const btnPrev    = document.getElementById('btn-prev');
const btnNext    = document.getElementById('btn-next');
const btnShuffle = document.getElementById('btn-shuffle');
const btnRepeat  = document.getElementById('btn-repeat');
const seekBar    = document.getElementById('seek-bar');
const volBar     = document.getElementById('volume-bar');
const timeCur    = document.getElementById('time-cur');
const timeDur    = document.getElementById('time-dur');
const playerTitle    = document.getElementById('player-title');
const playerMetaTags = document.getElementById('player-meta-tags');
const playerPathCrumb = document.getElementById('player-path-crumb');
const playerArt      = document.getElementById('player-art');

// ── Waveform canvas ──────────────────────────────────────────────────────────
const waveformCanvas = document.getElementById('waveform-canvas');
const waveformCtx    = waveformCanvas ? waveformCanvas.getContext('2d') : null;
let _waveformData    = null;   // Float array [0..1], 200 values

const WAVE_H = 44; // visual height of waveform — much taller than the 3px seek-bar track

// Cached once per track so the per-tick draw never hits getComputedStyle
// (which forces a synchronous style/layout recalc and was contributing to
// Firefox audio-thread underruns and Chromium oscilloscope stutter).
let _accentColor = '#f0722a';
const _DIM_COLOR = 'rgba(255,255,255,0.32)';
let _lastSplitBar = -1;
let _cachedBarGeom = null; // { w, h, barCount, barW, gap }

function _refreshAccentColor() {
  const v = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  if (v) _accentColor = v;
}

function _resizeWaveformCanvas() {
  if (!waveformCanvas) return;
  const sb = document.getElementById('seek-bar');
  if (!sb) return;
  const sbRect     = sb.getBoundingClientRect();
  const parentRect = sb.parentElement.getBoundingClientRect();
  const dpr        = window.devicePixelRatio || 1;

  // Centre the taller canvas on the seek-bar track
  const topOffset = sbRect.top - parentRect.top - (WAVE_H - sbRect.height) / 2;
  waveformCanvas.style.left   = (sbRect.left - parentRect.left) + 'px';
  waveformCanvas.style.top    = topOffset + 'px';
  waveformCanvas.style.width  = sbRect.width + 'px';
  waveformCanvas.style.height = WAVE_H + 'px';
  waveformCanvas.width  = sbRect.width * dpr;
  waveformCanvas.height = WAVE_H * dpr;
  waveformCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  _cachedBarGeom = null;   // force geometry recompute on next draw
  _lastSplitBar  = -1;
}

function _drawWaveform(pct = 0) {
  if (!waveformCtx || !_waveformData || !_waveformData.length) return;

  // Bar geometry is stable for the lifetime of a track — compute once.
  if (!_cachedBarGeom) {
    const w = parseFloat(waveformCanvas.style.width)  || waveformCanvas.width;
    const h = parseFloat(waveformCanvas.style.height) || waveformCanvas.height;
    const barCount = _waveformData.length;
    const barW     = Math.max(1, w / barCount - 0.5);
    const gap      = (w - barW * barCount) / barCount;
    _cachedBarGeom = { w, h, barCount, barW, gap };
  }
  const { w, h, barCount, barW, gap } = _cachedBarGeom;

  // Convert pct → which bar is the split, and skip redraw if unchanged.
  // timeupdate fires ~4×/sec; on a 400-bar waveform that's still only
  // ~1 bar per tick of movement, so 3 out of every 4 redraws are no-ops.
  const stride    = barW + gap;
  const splitX    = (pct / 100) * w;
  const splitBar  = Math.min(barCount, Math.max(0, Math.floor(splitX / stride) + 1));
  if (splitBar === _lastSplitBar) return;
  _lastSplitBar = splitBar;

  // Two-pass fill, one fillStyle mutation per pass instead of per bar.
  waveformCtx.clearRect(0, 0, w, h);
  const halfH = h * 0.85;
  const yMid  = h / 2;

  waveformCtx.fillStyle = _accentColor;
  for (let i = 0; i < splitBar; i++) {
    const amp  = _waveformData[i];
    const barH = amp * halfH; if (barH < 1) continue;
    waveformCtx.fillRect(i * stride, yMid - barH / 2, barW, barH);
  }
  waveformCtx.fillStyle = _DIM_COLOR;
  for (let i = splitBar; i < barCount; i++) {
    const amp  = _waveformData[i];
    const barH = amp * halfH; if (barH < 1) continue;
    waveformCtx.fillRect(i * stride, yMid - barH / 2, barW, barH);
  }
}

async function _fetchWaveform(trackId) {
  const progressEl = document.querySelector('.player-progress');
  try {
    const res  = await fetch(`/api/tracks/${trackId}/waveform`);
    if (!res.ok) { _waveformData = null; progressEl?.classList.remove('has-waveform'); return; }
    const data = await res.json();
    _waveformData = data.waveform || null;
    if (_waveformData) {
      progressEl?.classList.add('has-waveform');
      _refreshAccentColor();
      _resizeWaveformCanvas();
      _drawWaveform(0);
    } else {
      progressEl?.classList.remove('has-waveform');
    }
  } catch {
    _waveformData = null;
    progressEl?.classList.remove('has-waveform');
  }
}

window.addEventListener('resize', () => {
  if (_waveformData) {
    _resizeWaveformCanvas();
    _drawWaveform(parseFloat(seekBar.value));
  }
});

// ── VU Meters for tracker/module playback ─────────────────────────────────────
const vuContainer = document.getElementById('vu-meters');
let _vuAnalyser = null;
let _vuBars = [];
let _vuAnimFrame = null;
let _vuChannelCount = 0;

// Re-use the shared tracker format set (plus SID for VU meters)
const _TRACKER_FORMATS = new Set([...TRACKER_FORMAT_NAMES, 'SID']);

function _initVU(channelCount) {
  if (!vuContainer) return;
  _stopVU();

  _vuChannelCount = channelCount || 4;
  vuContainer.innerHTML = '';
  vuContainer.hidden = false;
  _vuBars = [];

  for (let i = 0; i < _vuChannelCount; i++) {
    const bar = document.createElement('div');
    bar.className = 'vu-bar';
    bar.style.setProperty('--vu-hue', `${(i * 360 / _vuChannelCount) + 15}`);
    vuContainer.appendChild(bar);
    _vuBars.push(bar);
  }

  // Connect to Web Audio API analyser for real-time frequency data
  try {
    const audioCtx = Player.getAudioContext();
    if (!audioCtx) {
      _vuBars.forEach(bar => bar.classList.add('vu-animated'));
      return;
    }

    _vuAnalyser = audioCtx.createAnalyser();
    _vuAnalyser.fftSize = 256;
    _vuAnalyser.smoothingTimeConstant = 0.7;

    const source = Player.getSourceNode();
    if (source) {
      // Connect source through the existing EQ chain to our analyser too
      // The analyser node at the end of the Player chain already has the data
      // — use that instead to avoid double-connecting the source
      const existingAnalyser = Player.analyser;
      if (existingAnalyser) {
        existingAnalyser.connect(_vuAnalyser);
        // Don't connect _vuAnalyser to destination (it's a tap, not in signal path)
      } else {
        source.connect(_vuAnalyser);
      }
    } else {
      _vuBars.forEach(bar => bar.classList.add('vu-animated'));
      return;
    }
  } catch (e) {
    // AudioContext not available — use CSS animation fallback
    _vuBars.forEach(bar => bar.classList.add('vu-animated'));
    return;
  }

  _vuAnimFrame = requestAnimationFrame(_drawVU);
}

function _drawVU() {
  if (!_vuAnalyser || !_vuBars.length) return;

  const bufLen = _vuAnalyser.frequencyBinCount;
  const data = new Uint8Array(bufLen);
  _vuAnalyser.getByteFrequencyData(data);

  // Distribute frequency bins across channels
  const binsPerChannel = Math.floor(bufLen / _vuChannelCount);

  for (let ch = 0; ch < _vuChannelCount; ch++) {
    const start = ch * binsPerChannel;
    const end = start + binsPerChannel;
    let sum = 0;
    for (let i = start; i < end && i < bufLen; i++) {
      sum += data[i];
    }
    const avg = sum / binsPerChannel / 255; // 0-1
    const level = Math.pow(avg, 0.7); // slight compression for visual
    _vuBars[ch].style.setProperty('--vu-level', level.toFixed(3));
  }

  _vuAnimFrame = requestAnimationFrame(_drawVU);
}

function _stopVU() {
  if (_vuAnimFrame) {
    cancelAnimationFrame(_vuAnimFrame);
    _vuAnimFrame = null;
  }
  // Disconnect our VU analyser if it was connected
  if (_vuAnalyser) {
    try { _vuAnalyser.disconnect(); } catch (_) {}
  }
  if (vuContainer) {
    vuContainer.hidden = true;
    vuContainer.innerHTML = '';
  }
  _vuBars = [];
  _vuAnalyser = null;
}

/** Check if a track is a tracker/module format and init VU meters if so. */
function _handleVU(track) {
  // Disabled: the VU meter's secondary AnalyserNode taps the signal path and
  // adds audio-thread work that triggered Firefox underruns (crackling) after
  // the other perf fixes landed. Keeping the function so callers still work,
  // but always tearing down any leftover analyser.
  _stopVU();
}

btnPlay.addEventListener('click',  () => Player.playPause());
btnPrev.addEventListener('click',  () => Player.prev());
btnNext.addEventListener('click',  () => Player.next());
volBar.addEventListener('input',   () => Player.setVolume(parseFloat(volBar.value)));

// 'change' fires on mouse-up after dragging — correct for seeking
seekBar.addEventListener('change', () => Player.seek(parseFloat(seekBar.value)));
// Also handle click without drag (pointerup on the track)
seekBar.addEventListener('pointerup', () => Player.seek(parseFloat(seekBar.value)));

btnShuffle.addEventListener('click', () => {
  btnShuffle.classList.toggle('on', Player.toggleShuffle());
});
btnRepeat.addEventListener('click', () => {
  const mode = Player.toggleRepeat();
  btnRepeat.classList.toggle('on', mode !== 'none');
  btnRepeat.title = { none: 'Repeat off', all: 'Repeat all', one: 'Repeat one' }[mode];
});

// ── Player callbacks ──────────────────────────────────────────────────────────
// timeupdate fires ~4Hz. We coalesce all DOM writes into a single rAF pass
// per tick and skip the write entirely when the tab is hidden (nothing to
// see). Also skip string/DOM churn when the user-visible value hasn't
// actually changed — seek-bar integer pct only changes a few times per track.
let _tuPending = false;
let _tuLast = { curStr: '', durStr: '', pct: -1 };
let _tuLatest = null;

function _applyTimeUpdate() {
  _tuPending = false;
  if (!_tuLatest) return;
  const { current, duration, pct } = _tuLatest;
  const curStr = Player.fmt(current);
  const durStr = Player.fmt(duration);
  const pctRounded = Math.round(pct * 10) / 10;  // 0.1% granularity — plenty

  if (curStr !== _tuLast.curStr) { timeCur.textContent = curStr; _tuLast.curStr = curStr; }
  if (durStr !== _tuLast.durStr) { timeDur.textContent = durStr; _tuLast.durStr = durStr; }
  if (pctRounded !== _tuLast.pct) {
    seekBar.value = pct;
    seekBar.style.backgroundImage =
      `linear-gradient(to right, var(--accent) ${pct}%, rgba(255,255,255,0.12) ${pct}%)`;
    _tuLast.pct = pctRounded;
  }
  _drawWaveform(pct);  // internally skips when split-bar hasn't moved
}

Player.on('timeupdate', (payload) => {
  _tuLatest = payload;
  if (document.hidden) return;             // nothing to render
  if (_tuPending) return;                  // already queued for next frame
  _tuPending = true;
  requestAnimationFrame(_applyTimeUpdate);
});

Player.on('statechange', ({ playing }) => {
  btnPlay.innerHTML = playing ? '&#9646;&#9646;' : '&#9654;';
  btnPlay.title = playing ? 'Pause' : 'Play';
});

// ── Build "Album Artist: X · Artist: Y" meta-tags line ──────────────────────
function _buildMetaTags(track) {
  const aa = (track.album_artist || '').trim();
  const ar = (track.artist || '').trim();
  if (!aa && !ar) { playerMetaTags.innerHTML = ''; return; }

  const parts = [];
  if (aa) {
    const span = document.createElement('span');
    span.className = 'meta-tags-item';
    span.innerHTML = `<span class="meta-tags-label">Album Artist:</span> <a class="meta-link" data-type="album_artist" data-name="${_escAttr(aa)}" href="#" title="Browse ${_escAttr(aa)}">${_escHtml(aa)}</a>`;
    parts.push(span.outerHTML);
  }
  if (ar && ar !== aa) {
    const span = document.createElement('span');
    span.className = 'meta-tags-item';
    span.innerHTML = `<span class="meta-tags-label">Artist:</span> <a class="meta-link" data-type="artist" data-name="${_escAttr(ar)}" href="#" title="Browse ${_escAttr(ar)}">${_escHtml(ar)}</a>`;
    parts.push(span.outerHTML);
  }
  playerMetaTags.innerHTML = parts.join('<span class="meta-tags-sep">·</span>');

  // Wire up clicks: navigate to albums filtered by that artist/album artist
  playerMetaTags.querySelectorAll('.meta-link').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const type = a.dataset.type;
      const name = a.dataset.name;
      if (type === 'album_artist') Library.showAlbums(null, name, 'album_artist');
      else Library.showAlbums(name, null, 'artist');
    });
  });
}

// ── Build "Playing Now: /path/ > folder > folder" breadcrumb ─────────────────
function _buildPathCrumb(track) {
  const raw = track.path || '';
  // Strip ZIP virtual path (outer.zip::member → show the zip's directory)
  const fsPath = raw.includes('::') ? raw.split('::')[0] : raw;
  const parts = fsPath.split('/').filter(Boolean);

  if (!parts.length) { playerPathCrumb.innerHTML = ''; return; }

  // Build cumulative paths for each segment
  let html = '<span class="crumb-label">Playing Now:</span> ';
  let cumulative = '';
  const segments = [];
  for (const part of parts) {
    cumulative += '/' + part;
    segments.push({ label: part, path: cumulative });
  }

  // Last segment is the filename — show without a link
  const fileSegment = segments.pop();
  html += segments.map(seg =>
    `<a class="crumb-link" href="#" data-path="${_escAttr(seg.path)}" title="${_escAttr(seg.path)}">${_escHtml(seg.label)}</a>`
  ).join('<span class="crumb-sep">›</span>');

  if (segments.length) html += '<span class="crumb-sep">›</span>';
  html += `<span class="crumb-file">${_escHtml(fileSegment.label)}</span>`;

  playerPathCrumb.innerHTML = html;

  // Wire up folder clicks
  playerPathCrumb.querySelectorAll('.crumb-link').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      Library.showFolder(a.dataset.path);
    });
  });
}

function _escHtml(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function _escAttr(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
}

Player.on('trackchange', (track) => {
  playerTitle.textContent  = track.title || '—';
  document.title = `${track.title || 'SoniqBoom'} — SoniqBoom`;
  _buildMetaTags(track);
  _buildPathCrumb(track);

  // Always try the art API — it extracts embedded + folder art lazily.
  // Only fall back to the placeholder emoji if the API returns 404.
  {
    const artSrc = track.cover_art || `/api/art/${track.id}?size=sm`;
    const img = new Image();
    img.onload = () => {
      playerArt.innerHTML = '';
      playerArt.appendChild(img);
      img.alt = 'cover';
      // Ambient art background glow in player bar
      const bg = document.getElementById('player-art-bg');
      bg.style.backgroundImage = `url("${artSrc}")`;
      bg.classList.add('active');
      // Sidebar ambient glow
      const sg = document.getElementById('sidebar-glow');
      if (sg) { sg.style.backgroundImage = `url("${artSrc}")`; sg.classList.add('active'); }
      // Header ambient glow
      const hg = document.getElementById('header-glow');
      if (hg) { hg.style.backgroundImage = `url("${artSrc}")`; hg.classList.add('active'); }
    };
    img.onerror = () => {
      playerArt.innerHTML = `<span class="art-placeholder">${artPlaceholderEmoji(track)}</span>`;
      const bg = document.getElementById('player-art-bg');
      bg.classList.remove('active');
      const sg = document.getElementById('sidebar-glow');
      if (sg) sg.classList.remove('active');
      const hg = document.getElementById('header-glow');
      if (hg) hg.classList.remove('active');
    };
    img.src = artSrc;
  }

  // Waveform — fetch and render behind seek bar
  _fetchWaveform(track.id);

  // VU meters — show for tracker/module formats
  _handleVU(track);
});

// ── Now Playing large art display ────────────────────────────────────────
const npArt      = document.getElementById('now-playing-art');
const npArtImg   = document.getElementById('now-playing-art-img');
const npTitle    = document.getElementById('np-title');
const npArtistEl = document.getElementById('np-artist');
const npAlbum    = document.getElementById('np-album');

// Click small art thumbnail to toggle large art
playerArt.style.cursor = 'pointer';
playerArt.addEventListener('click', () => {
  if (!Player.currentTrack) return;
  if (npArt.hidden) {
    _showNowPlayingArt(Player.currentTrack);
  } else {
    npArt.hidden = true;
  }
});

// Click large art to dismiss
if (npArt) npArt.addEventListener('click', () => { npArt.hidden = true; });

function _showNowPlayingArt(track) {
  if (!track) return;
  const src = track.cover_art || `/api/art/${track.id}?size=lg`;
  npArtImg.src = src;
  npTitle.textContent = track.title || '';
  npArtistEl.textContent = track.album_artist || track.artist || '';
  npAlbum.textContent = track.album || '';
  npArt.hidden = false;
}

// Update info when track changes (if art is visible)
Player.on('trackchange', (track) => {
  if (!npArt.hidden) _showNowPlayingArt(track);
});

volBar.value = parseFloat(localStorage.getItem('sb_volume') ?? '0.8');

// ── Sidebar navigation ────────────────────────────────────────────────────────
const views = {
  all:           () => Library.showAll(),
  artists:       () => Library.showArtists(),
  album_artists: () => Library.showAlbumArtists(),
  albums:        () => Library.showAlbums(),
  genres:        () => Library.showGenres(),
  years:         () => Library.showYears(),
};

// Smart playlist views (fetched from /api/smart/* endpoints)
const smartViews = {
  'history':        () => Library.showSmart('history',        'Listening History'),
  'most-played':    () => Library.showSmart('most-played',    'Most Played'),
  'recently-added': () => Library.showSmart('recently-added', 'Recently Added'),
  'top-rated':      () => Library.showSmart('top-rated',      'Top Rated'),
  'unplayed':       () => Library.showSmart('unplayed',       'Unplayed'),
  'duplicates':     () => Library.showDuplicates(),
};

/** Deactivate all sidebar nav items across all sections. */
function _deactivateAllNav() {
  document.querySelectorAll('#nav-library li, #nav-smart li').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tree-node.active').forEach(n => n.classList.remove('active'));
}

document.querySelectorAll('#nav-library li').forEach(li => {
  li.addEventListener('click', () => {
    _deactivateAllNav();
    li.classList.add('active');
    const view = li.dataset.view;
    if (views[view]) views[view]();
  });
});

// Wire up Smart sidebar entries
document.querySelectorAll('#nav-smart li').forEach(li => {
  li.addEventListener('click', () => {
    _deactivateAllNav();
    li.classList.add('active');
    const view = li.dataset.view;
    if (smartViews[view]) smartViews[view]();
  });
});

// ── Folder tree → show tracks in directory ────────────────────────────────────
FolderTree.onSelect(async (path) => {
  _deactivateAllNav();
  await Library.showFolder(path);
});

// ── Scan badge (progress shown via WebSocket) ─────────────────────────────────
const scanBadge = document.getElementById('scan-badge');

// ── Admin button ──────────────────────────────────────────────────────────────
document.getElementById('btn-admin').addEventListener('click', () => Admin.open());

// ── EQ button ─────────────────────────────────────────────────────────────────
document.getElementById('btn-eq').addEventListener('click', () => Equalizer.toggle());

// ── Queue button ──────────────────────────────────────────────────────────────
document.getElementById('btn-queue').addEventListener('click', () => Queue.toggle());

// ── Playlist button ──────────────────────────────────────────────────────────
document.getElementById('btn-playlist').addEventListener('click', () => Playlist.toggle());

// ── Add to Playlist from selection bar ───────────────────────────────────────
const selAddPlaylist = document.getElementById('sel-add-playlist');
if (selAddPlaylist) {
  selAddPlaylist.addEventListener('click', () => Playlist.showAddDropdown(selAddPlaylist));
}

// Keep queue panel in sync when queue state changes
Player.on('queuechange', () => Queue.refresh());

// ── Track Info button (player bar) ────────────────────────────────────────────
document.getElementById('btn-track-info').addEventListener('click', () => {
  const q   = Player.queue;
  const idx = Player.queueIdx;
  if (q.length > 0 && idx >= 0) {
    TrackInfo.open(q, idx);
  } else if (Player.currentTrack) {
    // Fallback: open for current track even if queue idx is stale
    TrackInfo.openSingle(Player.currentTrack);
  }
});

// ── Track Info via right-click on library rows ────────────────────────────────
Library.onInfo((tracks, idx) => TrackInfo.open(tracks, idx));

// ── Admin → main page sync ────────────────────────────────────────────────────
// Fired by admin.js after adding/removing/scanning folders so the tree and alias state stay in sync
document.addEventListener('soniqboom:dirs-changed', async () => {
  try {
    const cfg = await fetch('/api/ui-config').then(r => r.json());
    window.__sbConfig = { ...window.__sbConfig, ...cfg };
    Library.setAliasMap(cfg.folder_aliases || {});
    Library.setExposeLocalFiles(cfg.expose_local_files !== false);
  } catch (_) {}
  FolderTree.refresh();
});

// ── Sidebar resize drag handle ────────────────────────────────────────────────
const sidebar       = document.getElementById('sidebar');
const resizeHandle  = document.getElementById('sidebar-resize');

let _resizing = false;
let _startX   = 0;
let _startW   = 0;

resizeHandle.addEventListener('mousedown', (e) => {
  _resizing = true;
  _startX   = e.clientX;
  _startW   = sidebar.offsetWidth;
  resizeHandle.classList.add('dragging');
  document.body.style.cursor = 'col-resize';
  document.body.style.userSelect = 'none';
  e.preventDefault();
});

document.addEventListener('mousemove', (e) => {
  if (!_resizing) return;
  const delta = e.clientX - _startX;
  const newW  = Math.max(140, Math.min(480, _startW + delta));
  sidebar.style.width = `${newW}px`;
});

document.addEventListener('mouseup', () => {
  if (!_resizing) return;
  _resizing = false;
  resizeHandle.classList.remove('dragging');
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  localStorage.setItem('sb_sidebar_w', sidebar.offsetWidth);
});

// Restore saved sidebar width
const savedW = localStorage.getItem('sb_sidebar_w');
if (savedW) sidebar.style.width = `${savedW}px`;

// ── Sidebar horizontal splitters (resizable sections) ────────────────────────

/**
 * Wire up a horizontal splitter that resizes the sections above and below it.
 *
 * @param {string}  splitterId  – ID of the splitter div
 * @param {string}  aboveId     – ID of the section above the splitter
 * @param {string}  belowId     – ID of the section below the splitter
 * @param {string}  storageKey  – localStorage key for persisting the above-section height
 * @param {number}  minAbove    – minimum height (px) for the section above
 * @param {number}  minBelow    – minimum height (px) for the section below
 * @param {boolean} pinBelow    – when false, only the above section gets an explicit height;
 *                                the below section stays flex:1 and fills remaining space.
 *                                Use false for the bottom-most splitter so the last panel
 *                                always fills the sidebar without leaving a dead gap.
 */
function _initHSplitter(splitterId, aboveId, belowId, storageKey,
                        minAbove = 40, minBelow = 40, pinBelow = true) {
  const splitter = document.getElementById(splitterId);
  const above    = document.getElementById(aboveId);
  const below    = document.getElementById(belowId);
  if (!splitter || !above || !below) return;

  let dragging = false, startY = 0, startAboveH = 0, startBelowH = 0;

  splitter.addEventListener('mousedown', (e) => {
    dragging    = true;
    startY      = e.clientY;
    startAboveH = above.offsetHeight;
    startBelowH = below.offsetHeight;
    splitter.classList.add('dragging');
    document.body.style.cursor     = 'row-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const delta  = e.clientY - startY;
    const totalH = startAboveH + startBelowH;
    let newAbove = Math.max(minAbove, Math.min(totalH - minBelow, startAboveH + delta));

    above.style.height = `${newAbove}px`;
    above.style.flex   = 'none';

    if (pinBelow) {
      below.style.height = `${totalH - newAbove}px`;
      below.style.flex   = 'none';
    }
    // else: below keeps flex:1 and fills remaining space automatically
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('dragging');
    document.body.style.cursor     = '';
    document.body.style.userSelect = '';
    localStorage.setItem(storageKey, above.offsetHeight);
    if (pinBelow) localStorage.setItem(storageKey + '_b', below.offsetHeight);
  });

  // Restore persisted heights.  Heights are clamped to the current sidebar
  // height — stale localStorage values from an earlier session (bigger
  // window, more monitors, etc.) used to leave sections taller than the
  // sidebar itself, which clipped the folder-tree scroll container and made
  // it look like scrolling was broken.
  const sidebarH = splitter.parentElement?.clientHeight || window.innerHeight;
  const maxSection = Math.max(minAbove + minBelow, Math.floor(sidebarH * 0.6));
  const clamp = (v) => Math.max(minAbove, Math.min(maxSection, parseInt(v, 10) || 0));

  const savedAbove = localStorage.getItem(storageKey);
  if (savedAbove) {
    above.style.height = `${clamp(savedAbove)}px`;
    above.style.flex   = 'none';
  }
  if (pinBelow) {
    const savedBelow = localStorage.getItem(storageKey + '_b');
    if (savedBelow) {
      below.style.height = `${clamp(savedBelow)}px`;
      below.style.flex   = 'none';
    }
  }
}

_initHSplitter('splitter-library-folders', 'section-library', 'section-folders',
               'sb_split_lib',   40, 40, true);
_initHSplitter('splitter-folders-smart',   'section-folders', 'section-smart',
               'sb_split_fold',  40, 40, true);
// pinBelow=false: Playlists is the last section and must always fill remaining space
_initHSplitter('sidebar-splitter',         'section-smart',   'section-playlists',
               'sb_split_smart', 40, 40, false);

// Post-restore sanity check: if the three persisted splitter heights still
// sum to more than the sidebar can display, the last (flex:1) section —
// Playlists — gets clipped to 0 px and users see an empty strip instead of
// the playlist list.  This happens when localStorage carries over heights
// from a session with a taller sidebar (external monitor, resized window).
// Detect and reset once; the user gets the default flex layout back.
requestAnimationFrame(() => {
  const plSec = document.getElementById('section-playlists');
  if (!plSec) return;
  if (plSec.offsetHeight < 60) {
    ['sb_split_lib', 'sb_split_lib_b',
     'sb_split_fold', 'sb_split_fold_b',
     'sb_split_smart', 'sb_split_smart_b',
    ].forEach(k => localStorage.removeItem(k));
    // Wipe the inline styles the restore block applied so the default flex
    // rules from CSS take effect again.
    ['section-library', 'section-folders', 'section-smart'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.style.height = ''; el.style.flex = ''; }
    });
  }
});

// ── WebSocket — scan progress ─────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/library/ws`);

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'scan_progress') {
      if (msg.running) {
        // Phase 1: metadata scan in progress
        scanBadge.hidden = false;
        scanBadge.textContent = `Scanning ${msg.pct}% (${msg.processed}/${msg.total})`;
        FolderTree.setScanActive(true);
      } else if (msg.embedding) {
        // Phase 1 done, phase 2 embedding in background
        scanBadge.hidden = false;
        scanBadge.textContent = `Computing embeddings...`;
        // Library is already usable — refresh now
        Library.showAll();
        FolderTree.refresh();
        FolderTree.setScanActive(true);
      } else {
        // Both phases complete
        scanBadge.textContent = `Done \u2014 ${msg.processed} tracks`;
        setTimeout(() => { scanBadge.hidden = true; }, 4000);
        Library.showAll();
        Library.refreshBadges();
        FolderTree.refresh();
        FolderTree.setScanActive(false);
      }
    }
  };

  ws.onclose = () => setTimeout(connectWS, 2000);
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
const _shortcutsOverlay = document.getElementById('shortcuts-overlay');
document.getElementById('shortcuts-close').addEventListener('click', () => _shortcutsOverlay.classList.add('hidden'));
_shortcutsOverlay.addEventListener('click', (e) => { if (e.target === _shortcutsOverlay) _shortcutsOverlay.classList.add('hidden'); });

let _prevVolume = 0.8; // for mute toggle

document.addEventListener('keydown', (e) => {
  // Don't intercept when typing in an input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

  // ? — toggle shortcuts overlay
  if (e.key === '?') {
    e.preventDefault();
    _shortcutsOverlay.classList.toggle('hidden');
    return;
  }

  // Escape — close any open overlay
  if (e.code === 'Escape') {
    if (!_shortcutsOverlay.classList.contains('hidden')) {
      _shortcutsOverlay.classList.add('hidden');
      return;
    }
    return; // let other handlers handle their own Escape
  }

  // Space — play/pause
  if (e.code === 'Space') { e.preventDefault(); Player.playPause(); return; }

  // Meta+Right / Meta+Left — next / prev
  if (e.code === 'ArrowRight' && e.metaKey) { Player.next(); return; }
  if (e.code === 'ArrowLeft'  && e.metaKey) { Player.prev(); return; }

  // ArrowUp / ArrowDown — volume
  if (e.code === 'ArrowUp' && !e.metaKey) {
    e.preventDefault();
    const newVol = Math.min(1, parseFloat(volBar.value) + 0.05);
    volBar.value = newVol;
    Player.setVolume(newVol);
    return;
  }
  if (e.code === 'ArrowDown' && !e.metaKey) {
    e.preventDefault();
    const newVol = Math.max(0, parseFloat(volBar.value) - 0.05);
    volBar.value = newVol;
    Player.setVolume(newVol);
    return;
  }

  // M — mute/unmute
  if (e.code === 'KeyM') {
    const cur = parseFloat(volBar.value);
    if (cur > 0) {
      _prevVolume = cur;
      volBar.value = 0;
      Player.setVolume(0);
    } else {
      volBar.value = _prevVolume;
      Player.setVolume(_prevVolume);
    }
    return;
  }

  // S — toggle shuffle
  if (e.code === 'KeyS') {
    btnShuffle.classList.toggle('on', Player.toggleShuffle());
    return;
  }

  // R — cycle repeat
  if (e.code === 'KeyR') {
    const mode = Player.toggleRepeat();
    btnRepeat.classList.toggle('on', mode !== 'none');
    btnRepeat.title = { none: 'Repeat off', all: 'Repeat all', one: 'Repeat one' }[mode];
    return;
  }

  // / — focus search
  if (e.key === '/') {
    e.preventDefault();
    document.getElementById('search-input').focus();
    return;
  }

  // V — toggle visualizer mode (oscilloscope / spectrogram)
  if (e.code === 'KeyV') { Visualizer.toggleMode(); return; }

  // E — toggle EQ
  if (e.code === 'KeyE') { Equalizer.toggle(); return; }

  // Q — toggle queue
  if (e.code === 'KeyQ') { Queue.toggle(); return; }

  // I — track info
  if (e.code === 'KeyI') {
    const q = Player.queue;
    const idx = Player.queueIdx;
    if (q.length > 0 && idx >= 0) TrackInfo.open(q, idx);
    return;
  }

  // Enter — play focused track (when not in a text input)
  if (e.code === 'Enter') {
    e.preventDefault();
    Library.playFocused();
    return;
  }

  // J / K — navigate tracks down / up in library
  if (e.code === 'KeyJ') { Library.navigateTrack(1); return; }
  if (e.code === 'KeyK') { Library.navigateTrack(-1); return; }

  // A — add focused track to queue
  if (e.code === 'KeyA') { Library.addFocusedToQueue(); return; }

  // H — toggle history view
  if (e.code === 'KeyH') {
    _deactivateAllNav();
    const li = document.querySelector('#nav-smart li[data-view="history"]');
    if (li) li.classList.add('active');
    smartViews['history']();
    return;
  }

  // D — toggle duplicates view
  if (e.code === 'KeyD') {
    _deactivateAllNav();
    const li = document.querySelector('#nav-smart li[data-view="duplicates"]');
    if (li) li.classList.add('active');
    smartViews['duplicates']();
    return;
  }

  // 1-6 — sidebar views
  const viewKeys = { 'Digit1': 'all', 'Digit2': 'artists', 'Digit3': 'album_artists', 'Digit4': 'albums', 'Digit5': 'genres', 'Digit6': 'years' };
  if (viewKeys[e.code]) {
    const view = viewKeys[e.code];
    _deactivateAllNav();
    const li = document.querySelector(`#nav-library li[data-view="${view}"]`);
    if (li) li.classList.add('active');
    if (views[view]) views[view]();
    return;
  }
});

// ── Startup: fetch UI config, distribute alias map & toggle ────────────────────
(async () => {
  try {
    const cfg = await fetch('/api/ui-config').then(r => r.json());
    window.__sbConfig = { ...window.__sbConfig, ...cfg };

    // Distribute alias map & expose toggle to Library module
    Library.setAliasMap(cfg.folder_aliases || {});
    Library.setExposeLocalFiles(cfg.expose_local_files !== false);
  } catch (_) { /* Store not ready — ignore */ }
})();

// ── Startup intro animation ───────────────────────────────────────────────────
(async () => {
  try {
    const cfg = window.__sbConfig || await fetch('/api/ui-config').then(r => r.json());
    if (!cfg.display_startup_logo) return;
  } catch (_) { return; }

  const overlay = document.createElement('div');
  overlay.id = 'sb-intro-overlay';
  overlay.innerHTML = `
    <div id="sb-intro-logo">
      <span class="sb-intro-icon">
        <span class="sb-intro-icon-glow">🔊</span><span class="sb-intro-icon-top">🔊</span>
      </span>
      <div class="sb-intro-title-wrap">
        <span class="sb-intro-text">SoniqBoom</span>
        <span class="sb-intro-byline">by S.F.Cyris</span>
        <canvas class="sb-intro-electric"></canvas>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const logo = overlay.querySelector('#sb-intro-logo');
  const titleWrap = overlay.querySelector('.sb-intro-title-wrap');
  const elCanvas  = overlay.querySelector('.sb-intro-electric');

  // ── Electric arc animation ──────────────────────────────────────────────
  function _initElectric() {
    const dpr = window.devicePixelRatio || 1;
    const rect = titleWrap.getBoundingClientRect();
    elCanvas.width  = rect.width  * dpr;
    elCanvas.height = rect.height * dpr;
    elCanvas.style.width  = rect.width  + 'px';
    elCanvas.style.height = rect.height + 'px';
    const ctx = elCanvas.getContext('2d');
    ctx.scale(dpr, dpr);

    const W = rect.width, H = rect.height;
    const arcs = [];          // active arcs

    function spawnArc() {
      // Start from a random point along the text area
      const x = Math.random() * W;
      const y = Math.random() * H * 0.75;    // bias toward the title text
      const len = 20 + Math.random() * 60;   // arc length in px
      const angle = (Math.random() - 0.5) * 1.2;  // mostly horizontal
      const segs = 4 + Math.floor(Math.random() * 6);
      const points = [{ x, y }];
      for (let i = 1; i <= segs; i++) {
        const t = i / segs;
        const px = x + Math.cos(angle) * len * t + (Math.random() - 0.5) * 14;
        const py = y + Math.sin(angle) * len * t + (Math.random() - 0.5) * 14;
        points.push({ x: px, y: py });
      }
      arcs.push({
        points,
        life: 1.0,
        decay: 0.03 + Math.random() * 0.06,
        width: 0.5 + Math.random() * 1.5,
      });
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      // Spawn new arcs randomly
      if (Math.random() < 0.4) spawnArc();
      if (Math.random() < 0.15) spawnArc();   // occasional double

      for (let i = arcs.length - 1; i >= 0; i--) {
        const a = arcs[i];
        a.life -= a.decay;
        if (a.life <= 0) { arcs.splice(i, 1); continue; }

        // Jitter the points slightly each frame for sizzle
        for (let j = 1; j < a.points.length - 1; j++) {
          a.points[j].x += (Math.random() - 0.5) * 3;
          a.points[j].y += (Math.random() - 0.5) * 3;
        }

        ctx.save();
        ctx.globalAlpha = a.life;
        // Outer glow
        ctx.strokeStyle = 'rgba(107,200,240,0.6)';
        ctx.lineWidth = a.width + 3;
        ctx.shadowColor = 'rgba(107,200,240,0.8)';
        ctx.shadowBlur = 12;
        ctx.beginPath();
        ctx.moveTo(a.points[0].x, a.points[0].y);
        for (let j = 1; j < a.points.length; j++) ctx.lineTo(a.points[j].x, a.points[j].y);
        ctx.stroke();
        // Bright white core
        ctx.strokeStyle = 'rgba(255,255,255,0.9)';
        ctx.lineWidth = a.width;
        ctx.shadowColor = '#fff';
        ctx.shadowBlur = 4;
        ctx.beginPath();
        ctx.moveTo(a.points[0].x, a.points[0].y);
        for (let j = 1; j < a.points.length; j++) ctx.lineTo(a.points[j].x, a.points[j].y);
        ctx.stroke();
        ctx.restore();
      }
    }
    return draw;
  }

  // Blurred backdrop from the start
  overlay.style.backdropFilter = 'blur(18px)';
  overlay.style.webkitBackdropFilter = 'blur(18px)';

  // Fade in
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  logo.style.transition = 'opacity 0.25s ease';
  logo.style.opacity = '1';
  await new Promise(r => setTimeout(r, 300));

  // Start electric arcs once visible
  const drawElectric = _initElectric();
  let _elecRaf;
  const _elecLoop = () => { drawElectric(); _elecRaf = requestAnimationFrame(_elecLoop); };
  _elecLoop();

  // Hold for 2 s with electricity
  await new Promise(r => setTimeout(r, 2000));

  // Enlarge to 400% + fade out in 1 s, while blur dissolves to 0
  overlay.style.transition = 'backdrop-filter 1s ease-out, -webkit-backdrop-filter 1s ease-out';
  overlay.style.backdropFilter = 'blur(0px)';
  overlay.style.webkitBackdropFilter = 'blur(0px)';
  logo.style.transition = 'transform 1s cubic-bezier(0.2,0,0.8,1), opacity 1s ease-out';
  logo.style.transform = 'scale(4)';
  logo.style.opacity = '0';
  await new Promise(r => setTimeout(r, 1000));

  cancelAnimationFrame(_elecRaf);
  overlay.remove();
})();

// ── Init ──────────────────────────────────────────────────────────────────────
Library.showAll();
FolderTree.refresh();
// Populate the sidebar playlist list on startup.  Without this, the sidebar
// only rendered once the user opened the right-hand playlist panel (which
// calls Playlist.refresh() internally).
Playlist.refresh();
connectWS();
// One-shot check: if a scan was already running when the page opened,
// the WebSocket will deliver progress events to keep the UI in sync.
fetch('/api/library/scan/status').then(r => r.json()).then(s => {
  if (s.running || s.embedding) FolderTree.setScanActive(true);
}).catch(() => {});
