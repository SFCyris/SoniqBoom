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
import { artPlaceholderEmoji, trapFocus, isUadeAmigaTrack, ATARI_FORMAT_NAMES, PSF_FORMAT_NAMES, canEditTags } from './utils.js';
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
let _patternsAbort = null;
let _sceneAbort    = null;

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
const tabScene    = document.getElementById('ti-tab-scene');
const metaPane    = document.getElementById('ti-meta-pane');
const lyricsPane  = document.getElementById('ti-lyrics-pane');
const lyricsState = document.getElementById('ti-lyrics-state');
const scenePane   = document.getElementById('ti-scene-pane');
const sceneBody   = document.getElementById('ti-scene-body');

// ── State ─────────────────────────────────────────────────────────────────────
let _queue       = [];
let _idx         = 0;
let _activeTab   = 'info';   // 'info' | 'lyrics' | 'scene'
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
function _fmtChannels(n, track) {
  // Module-family "channels" are VOICES (tracker channels, chip voices),
  // not a speaker layout — an 8-channel ScreamTracker module is 8 voices
  // mixed to stereo, not 7.1 Surround.  Speaker-layout labels only apply
  // to real PCM audio.
  if (track && _isModuleFamily(track)) {
    return n === 1 ? '1 channel' : (n ? `${n} channels` : '—');
  }
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
  // Format-aware layout: a row with no value is hidden outright instead of
  // rendering an em-dash — a tracker module structurally has no Album
  // Artist/ISRC/Label, and eleven dashes told the user nothing.  Sections
  // whose rows are ALL hidden collapse too (_updateSectionVisibility).
  const field = el.closest('.ti-field');
  if (field) field.style.display = text ? '' : 'none';
}

/** Hide any .ti-autohide section whose field rows are all hidden. */
function _updateSectionVisibility() {
  if (!metaPane) return;
  for (const sec of metaPane.querySelectorAll('.ti-section.ti-autohide')) {
    let any = false;
    for (const f of sec.querySelectorAll('.ti-field')) {
      if (f.style.display !== 'none') { any = true; break; }
    }
    sec.style.display = any ? '' : 'none';
  }
}

// Placeholder / "unknown artist" tokens that must NOT trigger a bio lookup —
// mirrors artistinfo._PLACEHOLDER_ARTISTS on the backend (defense in depth).
// A fuzzy lookup on "<?>" (the SID header's unknown-author sentinel) returned
// a confident-but-wrong bio ("Kenji Kawai").
const _PLACEHOLDER_ARTISTS = new Set([
  '<?>', '?', '??', '???', '<no artist>', '<unknown>', '<unknown artist>',
  'unknown', 'unknown artist', 'various', 'various artists', 'va', 'n/a',
  'none', 'no artist', 'untitled',
]);
function _isPlaceholderArtist(name) {
  // Only the explicit sentinels — NOT "all-symbol" names: "!!!" (Chk Chk
  // Chk), "☭" etc. are real acts, and pure-punctuation garbage resolves to
  // found:False on the backend anyway (mirrors artistinfo._is_placeholder_artist).
  return _PLACEHOLDER_ARTISTS.has(name.trim().toLowerCase());
}

async function _loadArtistAbout(track) {
  const host = document.getElementById('ti-artist-about');
  if (!host) return;
  host.hidden = true; host.innerHTML = '';
  const artist = ((track && track.artist) || '').trim();
  if (!artist || _isPlaceholderArtist(artist)) return;
  // Guard against a slow response racing into a DIFFERENT track's modal on
  // rapid prev/next (mirrors the artwork loader's reqTrackId check).
  const reqTrackId = track.id;
  // Skeleton while the (external) bio fetch is in flight — pending vs absent
  // were previously indistinguishable (perceived-perf 2.4).
  host.innerHTML = '<div class="ti-bio-skel"><span class="ti-bio-skel-img skel-bar"></span>'
    + '<span class="skel-bar"></span><span class="skel-bar" style="width:92%"></span>'
    + '<span class="skel-bar" style="width:74%"></span></div>';
  host.hidden = false;
  let _bioRendered = false;
  try {
    // Album/track context lets the server pin the right artist for ambiguous
    // names ("Ghost" the band on this record, not anything else).
    let q = '/api/artist/info?name=' + encodeURIComponent(artist);
    if (track.album) q += '&album=' + encodeURIComponent(track.album);
    if (track.title) q += '&track=' + encodeURIComponent(track.title);
    // Format drives Demozoo-first identification for retro/scene music so a
    // scene handle resolves to the demoscene musician, not a mainstream band.
    if (track.format) q += '&format=' + encodeURIComponent(track.format);
    // Explicit scene flag: the uade Amiga-exotica formats (TFMX, Hippel,
    // Hubbard, Whittaker, ProWizard …) carry dynamic playernames that aren't in
    // the server's static retro-format set, yet those composers are exactly the
    // ones whose handles collide with mainstream MusicBrainz entities.  Reuse
    // the same scene detection the pattern/subsong UI uses.
    const _scene = isUadeAmigaTrack(track) || _MODULE_FORMATS.has(track.format)
        || ATARI_FORMAT_NAMES.has(track.format) || PSF_FORMAT_NAMES.has(track.format);
    if (_scene) q += '&retro=1';
    const r = await fetch(q);
    if (_queue[_idx]?.id !== reqTrackId) return;   // user navigated away
    if (!r.ok) return;
    const info = await r.json();
    if (_queue[_idx]?.id !== reqTrackId) return;
    if (!info || !info.found || !info.bio) return;
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
      c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
    // Allow-list the URL SCHEME on hrefs — esc() blocks attribute breakout but
    // not a `javascript:`/`data:` URI (a Demozoo scener's user-contributed
    // external link is untrusted).  Only http(s) may become a live anchor.
    const safeUrl = (u) => (/^https?:\/\//i.test(String(u == null ? '' : u)) ? String(u) : '');
    const full = info.bio;
    const bio = full.length > 420 ? full.slice(0, 420).replace(/\s+\S*$/, '') + '…' : full;
    const img = info.image
      ? `<img src="${esc(info.image)}" alt="${esc(info.title || artist)}" loading="lazy" decoding="async" style="float:left;width:72px;height:72px;object-fit:cover;border-radius:8px;margin:0 12px 8px 0">`
      : '';
    const _infoUrl = safeUrl(info.url);
    const link = _infoUrl
      ? ` <a href="${esc(_infoUrl)}" target="_blank" rel="noopener" style="white-space:nowrap">Read on ${esc(info.source || 'Wikipedia')} ▸</a>`
      : '';
    // Demozoo enrichment ships an external-links list (Bandcamp / Soundcloud /
    // Wikipedia / Discogs …).  Render them as chips labelled by host so we
    // don't hardcode Demozoo's link_class vocabulary.
    let linksHtml = '';
    if (Array.isArray(info.links) && info.links.length) {
      const chip = (l) => {
        const u = safeUrl(l && l.url);
        if (!u) return '';
        let label = l.class || 'link';
        try { label = new URL(u).hostname.replace(/^www\./, ''); } catch { /* keep class */ }
        return `<a href="${esc(u)}" target="_blank" rel="noopener" style="display:inline-block;`
          + `font-size:11px;padding:2px 8px;margin:4px 5px 0 0;border-radius:10px;`
          + `background:var(--surface-2,rgba(255,255,255,.08));color:var(--text-2,#bbb);`
          + `text-decoration:none;white-space:nowrap">${esc(label)}</a>`;
      };
      const chips = info.links.slice(0, 10).map(chip).join('');
      if (chips) linksHtml = `<div style="margin-top:6px">${chips}</div>`;
    }
    host.innerHTML =
      `<h4 style="margin:0 0 6px;font-size:13px;opacity:.85">About ${esc(info.title || artist)}</h4>` +
      `<div style="overflow:hidden;font-size:12.5px;line-height:1.55">${img}<span>${esc(bio)}</span>${link}</div>` +
      linksHtml;
    host.hidden = false;
    _bioRendered = true;
  } catch (e) { /* network/parse issue — leave hidden */ }
  finally {
    // Not rendered (no bio / error) and still on this track → drop the skeleton.
    // If the user navigated away, the newer call owns the host — don't touch it.
    if (!_bioRendered && _queue[_idx]?.id === reqTrackId) { host.hidden = true; host.innerHTML = ''; }
  }
}

// ── Scene tab (retro/demoscene tracks) ─────────────────────────────────────────
// Demozoo-sourced composer identity + discography + this track's release
// details, fetched from /api/artist/scene (which resolves the scener with the
// same confidence gate as /artist/info).  Baseline module context (Modland
// origin, format) always shows so the tab is never a dead end, even when the
// composer doesn't resolve on Demozoo.  When a production matches this track,
// its canonical release year is reflected into the INFO tab's Year row.
//
// The payload is resolved on EVERY retro-track render (not just when the SCENE
// tab is active) so the Year overwrite reaches the default INFO view, and is
// cached per-track (_sceneCache) so toggling to the tab is instant with no
// re-fetch / skeleton flash.
let _sceneCache = {};   // track_id → payload | 'loading' | 'error'

/** Render a resolved scene payload (or an empty-state marker) into the pane. */
function _renderScene(track, info) {
  if (!sceneBody || !track) return;
  const artist  = ((track.artist) || '').trim();
  const esc     = _escHtml;
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u == null ? '' : u)) ? String(u) : '');
  const hostOf  = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return 'link'; } };
  const chip    = (u, label) => {
    const su = safeUrl(u); if (!su) return '';
    return `<a href="${esc(su)}" target="_blank" rel="noopener" class="ti-scene-chip">${esc(label)}</a>`;
  };
  // Baseline context — present for ANY scene track, independent of a Demozoo
  // match, so the tab always shows something.
  const baseline = () => {
    const rows = [];
    if (track.scene_path)
      rows.push(`<div class="ti-scene-b-row"><span class="ti-scene-b-k">Scene archive</span><span>Modland: ${esc(track.scene_path)}</span></div>`);
    if (track.format)
      rows.push(`<div class="ti-scene-b-row"><span class="ti-scene-b-k">Format</span><span>${esc(track.format)}</span></div>`);
    return rows.length ? `<div class="ti-scene-block ti-scene-baseline">${rows.join('')}</div>` : '';
  };
  const empty = (msg) => `<div class="ti-scene-empty">${esc(msg)}</div>`;

  if (!info || !info.found || !info.artist) {
    const msg = info && info.__noartist ? 'No composer name to look up on Demozoo.'
      : info && info.__net             ? 'Couldn’t reach the scene database.'
      : `No demoscene entry found for “${artist}”.`;
    sceneBody.innerHTML = baseline() + empty(msg);
    return;
  }

  const a     = info.artist;
  const rel   = info.release;
  const disco = Array.isArray(info.discography) ? info.discography : [];
  const parts = [];

  // ── Artist block ──
  let ab = `<div class="ti-scene-artist"><div class="ti-scene-name">${esc(a.real_name || artist)}</div>`;
  const sub = [];
  if (a.real_name && a.real_name.toLowerCase() !== artist.toLowerCase()) sub.push(`“${esc(artist)}”`);
  if (Array.isArray(a.groups) && a.groups.length) sub.push('Member of ' + esc(a.groups.slice(0, 5).join(', ')));
  if (sub.length) ab += `<div class="ti-scene-sub">${sub.join(' · ')}</div>`;
  const seen = new Set([artist.toLowerCase(), (a.real_name || '').toLowerCase()]);
  const aliases = (a.aliases || []).filter(n => n && !seen.has(String(n).toLowerCase()));
  if (aliases.length) ab += `<div class="ti-scene-aka">aka ${esc(aliases.slice(0, 6).join(', '))}</div>`;
  const aChips = [chip(a.url, 'Demozoo')]
    .concat((a.links || []).slice(0, 12).map(l => chip(l && l.url, hostOf(l && l.url))))
    .filter(Boolean).join('');
  if (aChips) ab += `<div class="ti-scene-chips">${aChips}</div>`;
  ab += `</div>`;
  parts.push(ab);

  // ── This release block ──
  if (rel) {
    let rb = `<div class="ti-scene-block"><div class="ti-scene-h">This release</div>`;
    if (rel.title) rb += `<div class="ti-scene-rel-title">${esc(rel.title)}</div>`;
    const meta = [];
    if (rel.year) meta.push(String(rel.year));
    if (rel.type) meta.push(esc(rel.type));
    if (Array.isArray(rel.platforms) && rel.platforms.length) meta.push(esc(rel.platforms.join(', ')));
    if (meta.length) rb += `<div class="ti-scene-meta">${meta.join(' · ')}</div>`;
    (rel.placings || []).slice(0, 4).forEach(p => {
      const where = [p.competition, p.party].filter(Boolean).map(esc).join(' at ');
      const rank  = p.rank ? `#${esc(p.rank)} · ` : '';
      const yr    = p.year ? ` (${p.year})` : '';
      const txt   = `${rank}${where}${yr}`.trim();
      if (txt) rb += `<div class="ti-scene-compo">🏆 ${txt}</div>`;
    });
    (rel.parties || []).slice(0, 3).forEach(pt => {
      if (pt && pt.name) rb += `<div class="ti-scene-compo">▸ Released at ${esc(pt.name)}${pt.year ? ` (${pt.year})` : ''}</div>`;
    });
    const rChips = [chip(rel.url, 'Demozoo')]
      .concat((rel.links || []).slice(0, 8).map(l => chip(l && l.url, hostOf(l && l.url))))
      .filter(Boolean).join('');
    if (rChips) rb += `<div class="ti-scene-chips">${rChips}</div>`;
    rb += `</div>`;
    parts.push(rb);
  }

  // ── Discography ──
  if (disco.length) {
    let db = `<div class="ti-scene-block"><div class="ti-scene-h">More by ${esc(artist)}</div><ul class="ti-scene-disco">`;
    disco.forEach(p => {
      const su    = safeUrl(p.url);
      const label = esc(p.title);
      const inner = su ? `<a href="${esc(su)}" target="_blank" rel="noopener">${label}</a>` : label;
      const m     = [p.year, p.type].filter(Boolean).map(esc).join(' · ');
      db += `<li>${inner}${m ? ` <span class="ti-scene-disco-m">${m}</span>` : ''}</li>`;
    });
    db += `</ul>`;
    const allUrl = safeUrl(a.url);
    if (allUrl) db += `<a class="ti-scene-more" href="${esc(allUrl)}" target="_blank" rel="noopener">See all on Demozoo ▸</a>`;
    db += `</div>`;
    parts.push(db);
  }

  parts.push(baseline());
  sceneBody.innerHTML = parts.join('');
}

/** Reflect a matched production's canonical release year into the INFO Year row
 *  (display-time overwrite).  Runs regardless of the active tab so the default
 *  INFO view shows the corrected year.  Guarded on the track still being shown. */
function _applySceneYear(track, info, reqTrackId) {
  const rel = info && info.release;
  if (!(rel && rel.year)) return;
  // Only enhance a track with NO persisted year provenance — i.e. one the
  // Demozoo apply hasn't covered yet.  A stored demozoo year is already shown
  // (identical value) and a user-pinned year must never be overridden, so
  // this display-time overwrite stays out of the way of both.
  if (track.year_source === 'demozoo' || track.year_source === 'user') return;
  if (_queue[_idx]?.id !== reqTrackId) return;
  const yEl = document.getElementById('ti-year');
  if (!yEl || Number(rel.year) === Number(track.year)) return;
  yEl.textContent = String(rel.year);
  yEl.title = `Demozoo release year${track.year ? ` · file tag says ${track.year}` : ''}`;
  yEl.classList.remove('ti-empty');
  yEl.classList.add('ti-year-scene');
  const field = yEl.closest('.ti-field');
  if (field) field.style.display = '';
  _updateSectionVisibility();
}

async function _loadScene(track) {
  if (!sceneBody) return;
  const reqTrackId = track && track.id;
  if (!track) { sceneBody.innerHTML = ''; return; }
  const artist = ((track.artist) || '').trim();

  // No composer name → immediate, cached baseline-only state.
  if (!artist || _isPlaceholderArtist(artist)) {
    const payload = { found: false, __noartist: true };
    if (reqTrackId) _sceneCache[reqTrackId] = payload;
    _renderScene(track, payload);
    return;
  }

  const cached = reqTrackId ? _sceneCache[reqTrackId] : null;
  if (cached === 'loading') return;                    // in flight — will render on resolve
  if (cached && cached !== 'error') {                  // resolved payload → instant
    _renderScene(track, cached);
    _applySceneYear(track, cached, reqTrackId);
    return;
  }

  if (reqTrackId) _sceneCache[reqTrackId] = 'loading';
  // Skeleton while the (external) Demozoo fetch is in flight.  Written into the
  // pane even when INFO is active (harmless — the pane is hidden) so the tab is
  // pre-populated when opened.
  sceneBody.innerHTML = '<div class="ti-bio-skel"><span class="skel-bar"></span>'
    + '<span class="skel-bar" style="width:88%"></span>'
    + '<span class="skel-bar" style="width:70%"></span></div>';

  if (_sceneAbort) { try { _sceneAbort.abort(); } catch (_) {} }
  const _c = (typeof AbortController === 'function') ? new AbortController() : null;
  _sceneAbort = _c;
  try {
    let q = '/api/artist/scene?name=' + encodeURIComponent(artist) + '&retro=1';
    if (track.title)  q += '&track='  + encodeURIComponent(track.title);
    if (track.format) q += '&format=' + encodeURIComponent(track.format);
    const r = await fetch(q, _c ? { signal: _c.signal } : undefined);
    if (!r.ok) throw new Error('http ' + r.status);
    const info = await r.json();
    if (reqTrackId) _sceneCache[reqTrackId] = info;    // cache resolved payload
    // Apply the year + render only if still on this track (else the pane/INFO
    // row belong to a newer track; the payload stays cached for a revisit).
    if (_queue[_idx]?.id === reqTrackId) {
      _renderScene(track, info);
      _applySceneYear(track, info, reqTrackId);
    }
  } catch (e) {
    if (e && e.name === 'AbortError') {
      // Track switched mid-fetch — drop the 'loading' marker so a revisit
      // re-fetches instead of sticking forever.
      if (reqTrackId) delete _sceneCache[reqTrackId];
      return;
    }
    if (reqTrackId) _sceneCache[reqTrackId] = 'error';
    if (_queue[_idx]?.id === reqTrackId) _renderScene(track, { found: false, __net: true });
  } finally {
    if (_sceneAbort === _c) _sceneAbort = null;
  }
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
  // Tags can only be written to LOCAL files (tagwriter can't write to a network
  // share), so hide the edit affordance entirely for any non-local track rather
  // than showing a disabled button.
  const remote = /^(smb|ftp|webdav|webdavs|https?):\/\//.test(track.path || '');
  // Full tag editor writes the FILE, so it needs a local, mutagen-writable
  // container (MP3/FLAC/M4A/OGG/Opus/WavPack/Musepack).  Remote shares,
  // archive members, modules, SID, chip formats etc. would 422 on a file write.
  if (!remote && canEditTags(track)) {
    const btn = document.createElement('button');
    btn.id = 'ti-edit-tags';
    btn.textContent = '✏️ Edit tags';
    btn.style.cssText = 'font-size:12px;padding:4px 10px;opacity:.85';
    btn.addEventListener('click', () => _showTagForm(track, wrap));
    wrap.appendChild(btn);
    return;
  }
  // Retro/module tracks can't be file-tagged, but their Demozoo-backfilled
  // release year is worth being able to correct or revert — that's a
  // STORE-ONLY edit (PUT /year), so it works for remote/archive tracks too.
  if (_isModuleFamily(track)) {
    const btn = document.createElement('button');
    btn.id = 'ti-edit-year';
    btn.textContent = '✏️ Edit year';
    btn.style.cssText = 'font-size:12px;padding:4px 10px;opacity:.85';
    btn.addEventListener('click', () => _showYearForm(track, wrap));
    wrap.appendChild(btn);
  }
}

function _showYearForm(track, wrap) {
  wrap.innerHTML = '';
  const form = document.createElement('div');
  form.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12.5px';
  const lab = document.createElement('label');
  lab.textContent = 'Year'; lab.style.opacity = '.7';
  const inp = document.createElement('input');
  inp.type = 'number'; inp.min = '1000'; inp.max = '2100';
  inp.value = (track.year != null ? track.year : '');
  inp.style.cssText = 'font-size:12.5px;padding:4px 8px;width:88px';
  const save = document.createElement('button');
  save.textContent = 'Save'; save.style.cssText = 'font-size:12px;padding:4px 12px';
  const cancel = document.createElement('button');
  cancel.textContent = 'Cancel';
  cancel.style.cssText = 'font-size:12px;padding:4px 12px;opacity:.7';
  cancel.addEventListener('click', () => _renderTagEdit(track));
  form.append(lab, inp, save, cancel);
  // A stamped year (Demozoo or a prior user edit) that preserved the original
  // gets a one-click revert to the file/rip value.
  if ((track.year_source === 'demozoo' || track.year_source === 'user')
      && track.year_file != null) {
    const rev = document.createElement('button');
    rev.textContent = `↺ Revert to file year (${track.year_file})`;
    rev.style.cssText = 'font-size:12px;padding:4px 12px;opacity:.85';
    rev.addEventListener('click', () => _saveYear(track, wrap, { revert: true }));
    form.appendChild(rev);
  }
  save.addEventListener('click', () => {
    const v = inp.value.trim();
    const n = v === '' ? null : parseInt(v, 10);
    if (v !== '' && !(n >= 1000 && n <= 2100)) {
      window.Toast?.error?.('Year must be between 1000 and 2100.'); return;
    }
    _saveYear(track, wrap, { year: n });
  });
  wrap.appendChild(form);
  inp.focus();
}

async function _saveYear(track, wrap, body) {
  try {
    const r = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/year`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), credentials: 'same-origin',
    });
    if (!r.ok) {
      let msg = 'Could not update the year.';
      try { msg = (await r.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    const res = await r.json();
    Object.assign(track, res.applied || {});     // year, year_source, year_file
    window.Toast?.ok?.('Year updated.');
    // Guard the post-await repaint: the user may have hit ◀/▶ during the PUT,
    // and re-rendering the old track would overwrite the newer one's panel
    // (mirrors every other async render's _queue[_idx] check).
    if (_queue[_idx]?.id === track.id) _render(track);   // re-render INFO (Year row + marker)
  } catch (e) {
    window.Toast?.error?.(e.message || 'Could not update the year.');
  }
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
      // Don't repaint a track the user navigated away from during the save.
      if (_queue[_idx]?.id === track.id) _render(track);
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
  // Mark a non-file year from its PERSISTED provenance (the Demozoo backfill or
  // a manual edit both store year_source), so the accent marker + tooltip are
  // right the moment the panel opens — no wait on the scene fetch.  _loadScene
  // may still add the marker for an as-yet-unapplied track (year_source unset).
  const _yEl0 = document.getElementById('ti-year');
  if (_yEl0) {
    const _ys = track.year_source;
    if ((_ys === 'demozoo' || _ys === 'user') && track.year != null) {
      _yEl0.classList.add('ti-year-scene');
      _yEl0.title = _ys === 'demozoo'
        ? `Demozoo release year${track.year_file != null ? ` · file tag says ${track.year_file}` : ''}`
        : 'Year set manually';
    } else {
      _yEl0.classList.remove('ti-year-scene');
      _yEl0.title = '';
    }
  }
  _renderDefect(track.defect, track.defect_detail);

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
  // Scene provenance from the Modland MD5 join (admin → Scene metadata) —
  // "Format/Author/…/file", the module's home in the scene archive.
  _show('ti-scene-origin', track.scene_path, v => `Modland: ${v}`);
  _show('ti-isrc',     track.isrc);
  _show('ti-label',    track.label);

  // ── FILE ──
  _show('ti-format',      track.format);
  _show('ti-bit-depth',   track.bit_depth,   v => `${v}-bit`);
  _show('ti-sample-rate', track.sample_rate, _fmtRate);
  _show('ti-channels',    track.channels,    v => _fmtChannels(v, track));
  _show('ti-bitrate',     track.bitrate,     _fmtBitrate);
  _show('ti-file-size',   track.file_size,   _fmtSize);
  _show('ti-added',       track.added_at,    _fmtDate);
  _show('ti-path',        track.path);

  // ── Chapters (podcast/audiobook) — appended after the regular tags
  // when the file has them.  Click jumps the player.
  _loadChapters(track);
  // Scene/retro tracks route the artist identity to the SCENE tab (richer:
  // discography, release, links), so the INFO-pane bio block is suppressed for
  // them to avoid showing the same person twice.  Regular audio keeps it.
  const _scene = _isSceneTrack(track);
  if (_scene) {
    const _ab = document.getElementById('ti-artist-about');
    if (_ab) { _ab.hidden = true; _ab.innerHTML = ''; }
  } else {
    _loadArtistAbout(track);
  }
  _renderTagEdit(track);
  _loadPatterns(track);

  // Format-aware section order: for module-family tracks the module
  // identity (details, pattern grid, song message) leads, right after the
  // main tag block; PCM audio keeps the classic tag-centric order.  The
  // nodes are MOVED (insertBefore), so listeners and state survive.
  _reorderSections(track);
  _updateSectionVisibility();

  // Tab set: INFO always; a retro/scene track shows SCENE (which REPLACES
  // Lyrics — modules have no lyrics), regular audio keeps Lyrics.  Lyrics is
  // never dropped for regular tracks even when empty: the empty state is the
  // affordance that lazily fetches lyrics from the server on click.
  if (tabScene)  tabScene.hidden  = !_scene;
  if (tabLyrics) tabLyrics.hidden = _scene;
  // Navigating prev/next across a retro↔regular boundary can leave the active
  // tab hidden — fall back to Info so no invisible pane is "current".
  if ((_activeTab === 'lyrics' && _scene) || (_activeTab === 'scene' && !_scene)) {
    _switchTab('info');
  }

  // Resolve the SCENE data for EVERY retro track (not just when the SCENE tab
  // is active) so the Year overwrite reaches the default INFO view and the pane
  // is pre-populated for an instant tab-open.  _loadScene is cached + idempotent
  // per track.  Non-retro: clear the pane.
  if (_scene) {
    _loadScene(track);
  } else if (sceneBody) {
    sceneBody.innerHTML = '';
  }

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

/** Module-family test: static tracker/SID/MIDI names plus the dynamic-name
 *  scene families (uade Amiga exotica, Atari ST, PSF console rips). */
function _isModuleFamily(track) {
  if (!track) return false;
  return _MODULE_FORMATS.has(track.format)
      || isUadeAmigaTrack(track)
      || ATARI_FORMAT_NAMES.has(track.format)
      || PSF_FORMAT_NAMES.has(track.format);
}

/** Demoscene-track test — drives the SCENE-vs-LYRICS tab swap.  It's the
 *  module family MINUS General MIDI: GM/karaoke MIDI isn't demoscene (no
 *  Demozoo entry to show) and .kar files can carry real lyrics, so MIDI keeps
 *  the Lyrics tab rather than getting an always-empty Scene tab.  (Module
 *  section reordering still uses _isModuleFamily, so MIDI's module details are
 *  unaffected.) */
function _isSceneTrack(track) {
  return _isModuleFamily(track) && track && track.format !== 'MIDI';
}

// Sections that participate in the format-aware reorder, in DOM units.
const _SECTION_ORDER_AUDIO = [
  'ti-section-main', 'ti-section-numbering', 'ti-section-details',
  'ti-section-module', 'ti-section-subsongs', 'ti-section-stil', 'ti-section-patterns',
  'ti-section-message', 'ti-section-file',
];
const _SECTION_ORDER_MODULE = [
  'ti-section-main', 'ti-section-module', 'ti-section-subsongs', 'ti-section-stil',
  'ti-section-patterns', 'ti-section-message', 'ti-section-details', 'ti-section-numbering',
  'ti-section-file',
];

function _reorderSections(track) {
  const anchor = document.getElementById('ti-chapters');
  if (!metaPane || !anchor) return;
  const order = _isModuleFamily(track) ? _SECTION_ORDER_MODULE : _SECTION_ORDER_AUDIO;
  for (const id of order) {
    const el = document.getElementById(id);
    if (el) metaPane.insertBefore(el, anchor);
  }
}

// ── Subsong picker ────────────────────────────────────────────────────────────
// Multi-tune files (SID/SNDH/AHX/SC68/UADE/GME/tracker) expose N subsongs; the
// backend renders any one via ?subsong=<0-based>.  A "virtual track" is the base
// track object plus a 0-based ``subsong`` + ``subsongTotal`` — the player forwards
// ``subsong`` to the stream URL (see player.js _streamUrlFor) and shows
// "Tune N / total".  DISPLAY is 1-based ("Tune 1".."Tune N"); the wire index is
// always the label minus one.  Never persist/forward the 1-based number.
const _subSection = document.getElementById('ti-section-subsongs');
const _subListEl  = document.getElementById('ti-sub-list');
const SUB_CAP     = 60;                 // rows rendered up-front; "jump to #" reaches the tail
let   _subState   = null;               // { track, count, defaultWire, lengths }

function _subFmtLen(sec) {
  if (!(sec > 0)) return '';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function _subVirtual(base, wire) {
  // Carry only what playback needs; ``subsong`` is the 0-based wire index.
  return { ...base, subsong: wire,
           subsongTotal: _subState ? _subState.count : 0,
           subsongLabel: `Tune ${wire + 1}` };
}

// HVSC STIL: parse the raw blob's ``(#N)`` subtune markers into a
// { wire → title } map.  ``(#N)`` is 1-based (matches display "Tune N"), so
// wire = N-1.  We take the FIRST ``TITLE:`` inside each block; ARTIST/COMMENT
// are left for the raw commentary panel.  Files without ``(#N)`` markers (a
// single file-level TITLE) yield null — the picker then shows bare tune
// numbers, which is the correct graceful degrade.
function _parseStilTitles(blob) {
  if (!blob || typeof blob !== 'string') return null;
  const titles = {};
  let wire = -1;
  for (const raw of blob.split('\n')) {
    const mk = raw.match(/^\s*\(#(\d+)\)/);
    if (mk) { wire = parseInt(mk[1], 10) - 1; continue; }
    if (wire < 0) continue;
    const tm = raw.match(/^\s*TITLE:\s?(.+?)\s*$/);
    if (tm && titles[wire] === undefined) titles[wire] = tm[1].trim();
  }
  return Object.keys(titles).length ? titles : null;
}

function _subRowHtml(wire) {
  const st = _subState;
  const isDef = st && wire === st.defaultWire;
  const len = (st && Array.isArray(st.lengths)) ? _subFmtLen(st.lengths[wire]) : '';
  const title = (st && st.stilTitles) ? st.stilTitles[wire] : '';
  const titleHtml = title
    ? ` <span class="ti-sub-title" title="${_escHtml(title)}">· ${_escHtml(title)}</span>`
    : '';
  return `<div class="ti-sub-row" role="listitem" data-wire="${wire}" tabindex="0" aria-label="Tune ${wire + 1}${title ? ': ' + _escHtml(title) : ''}">`
    + `<span class="ti-sub-eq" aria-hidden="true"><i></i><i></i><i></i></span>`
    + `<span class="ti-sub-name">Tune ${wire + 1}${isDef ? ' <span class="ti-sub-def">default</span>' : ''}${titleHtml}</span>`
    + `<span class="ti-sub-len">${len}</span>`
    + `<span class="ti-sub-rowacts">`
    +   `<button type="button" class="ti-sub-rbtn" data-act="play" tabindex="-1" aria-label="Play tune ${wire + 1}" title="Play">&#9654;</button>`
    +   `<button type="button" class="ti-sub-rbtn" data-act="queue" tabindex="-1" aria-label="Add tune ${wire + 1} to queue" title="Add to queue">&#65291;</button>`
    +   `<button type="button" class="ti-sub-rbtn" data-act="playlist" tabindex="-1" aria-label="Add tune ${wire + 1} to a playlist" title="Add to playlist">&#9776;</button>`
    + `</span></div>`;
}

function _renderSubsongPicker(track, count, defaultTrack1, lengths, stilTitles) {
  if (!_subSection || !_subListEl) return;
  if (!(count > 1)) { _subSection.hidden = true; _subState = null; return; }
  _subState = {
    track,
    count,
    // default_track is 1-based (PSID/SNDH header); convert to 0-based wire, or
    // fall back to the first tune when the file doesn't record a default.
    defaultWire: (Number(defaultTrack1) > 0) ? (Number(defaultTrack1) - 1) : 0,
    lengths: Array.isArray(lengths) ? lengths : null,
    // { wire → HVSC STIL tune title }, or null when the file has no per-tune
    // STIL names — the rows then show bare "Tune N".
    stilTitles: (stilTitles && typeof stilTitles === 'object') ? stilTitles : null,
  };
  const cntEl = document.getElementById('ti-sub-count');
  if (cntEl) cntEl.textContent = `· ${count} tunes`;
  const addLbl = document.getElementById('ti-sub-addall-lbl');
  if (addLbl) addLbl.textContent = `Add all · ${count}`;
  const shown = Math.min(count, SUB_CAP);
  const rows = [];
  for (let w = 0; w < shown; w++) rows.push(_subRowHtml(w));
  _subListEl.innerHTML = rows.join('');
  const moreEl = document.getElementById('ti-sub-more');
  if (moreEl) moreEl.textContent = count > SUB_CAP ? `+ ${count - SUB_CAP} more — use “jump to #”` : '';
  _subSection.hidden = false;
  _highlightPlayingSubsong();
}

function _subAllVirtual() {
  const st = _subState; if (!st) return [];
  const out = [];
  for (let w = 0; w < st.count; w++) out.push(_subVirtual(st.track, w));
  return out;
}

function _subAction(act, wire, anchor) {
  const st = _subState; if (!st || !(wire >= 0)) return;
  if (act === 'queue') {
    Player.addToQueue(_subVirtual(st.track, wire));
    window.Toast?.ok?.(`Added tune ${wire + 1} to the queue`);
  } else if (act === 'playlist') {
    _subToPlaylist([{ id: st.track.id, subsong: wire }], anchor);
  } else {
    Player.setQueue([_subVirtual(st.track, wire)], 0);   // 'play' (default)
  }
}

async function _subToPlaylist(entries, anchor) {
  try {
    const mod = await import('./playlist.js');
    mod.Playlist.showAddDropdownForEntries(anchor || _subListEl, entries);
  } catch (_) { window.Toast?.error?.('Could not open playlists.'); }
}

function _highlightPlayingSubsong() {
  if (!_subListEl) return;
  _subListEl.querySelectorAll('.ti-sub-row.playing').forEach(r => r.classList.remove('playing'));
  const st = _subState, cur = Player.currentTrack;
  if (st && cur && cur.id === st.track.id && Number.isInteger(cur.subsong)) {
    const row = _subListEl.querySelector(`.ti-sub-row[data-wire="${cur.subsong}"]`);
    if (row) row.classList.add('playing');
  }
}

// One delegated handler covers any tune count (even 256) — no per-row listeners.
if (_subListEl) {
  _subListEl.addEventListener('click', (e) => {
    const row = e.target.closest('.ti-sub-row'); if (!row) return;
    const btn = e.target.closest('.ti-sub-rbtn');
    _subAction(btn ? btn.dataset.act : 'play', parseInt(row.dataset.wire, 10), btn || row);
  });
  _subListEl.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest('.ti-sub-row'); if (!row) return;
    e.preventDefault();
    _subAction('play', parseInt(row.dataset.wire, 10));
  });
}
document.getElementById('ti-sub-playall')?.addEventListener('click', () => {
  const list = _subAllVirtual(); if (list.length) Player.setQueue(list, 0);
});
document.getElementById('ti-sub-shuffle')?.addEventListener('click', () => {
  const list = _subAllVirtual();
  for (let i = list.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [list[i], list[j]] = [list[j], list[i]];
  }
  if (list.length) Player.setQueue(list, 0);
});
document.getElementById('ti-sub-addall')?.addEventListener('click', () => {
  const list = _subAllVirtual();
  list.forEach(vt => Player.addToQueue(vt));
  window.Toast?.ok?.(`Added all ${list.length} tunes to the queue`);
});
document.getElementById('ti-sub-addall-pl')?.addEventListener('click', (e) => {
  const st = _subState; if (!st) return;
  const entries = [];
  for (let w = 0; w < st.count; w++) entries.push({ id: st.track.id, subsong: w });
  _subToPlaylist(entries, e.currentTarget);
});
document.getElementById('ti-sub-jump-in')?.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const n = parseInt(e.target.value, 10);
  if (_subState && n >= 1 && n <= _subState.count) { _subAction('play', n - 1); e.target.value = ''; }
});
Player.on?.('trackchange', () => _highlightPlayingSubsong());

// ── HVSC STIL commentary + SID chip badge ─────────────────────────────────────
// STIL is the SID Tune Information List — per-file (and per-subtune) trivia.
// We render the raw blob (human-authored; reads well as-is, and mirrors how
// SID players surface STIL); the per-tune titles are separately parsed into the
// picker rows via _parseStilTitles.
const _stilSection = document.getElementById('ti-section-stil');
const _stilBody    = document.getElementById('ti-stil-body');
function _renderStil(blob) {
  if (!_stilSection || !_stilBody) return;
  const text = (typeof blob === 'string') ? blob.trim() : '';
  if (!text) { _stilSection.hidden = true; _stilBody.textContent = ''; return; }
  _stilBody.textContent = text;
  _stilSection.hidden = false;
}

// SID chip model (6581 / 8580 / 6581/8580) from the PSID header — a field in the
// module-details section, styled as a chip badge.  Hidden when unknown.
const _sidChipField = document.getElementById('ti-field-sidchip');
const _sidChipEl    = document.getElementById('ti-sidchip');
function _renderSidChip(model) {
  if (!_sidChipField || !_sidChipEl) return;
  const m = (typeof model === 'string') ? model.trim() : '';
  if (!m) { _sidChipField.style.display = 'none'; _sidChipEl.textContent = ''; return; }
  _sidChipEl.textContent = m;
  _sidChipField.style.display = '';
}

// Track-health badge + detail (a known playback defect flagged at scan) — a
// "Status" row at the top of Track Info, styled like the row badge.  Hidden
// for healthy tracks.  Mirrors _renderSidChip's manual show/hide.
const _defectField  = document.getElementById('ti-field-defect');
const _defectBadge  = document.getElementById('ti-defect-badge');
const _defectDetail = document.getElementById('ti-defect-detail');
function _renderDefect(defect, detail) {
  if (!_defectField || !_defectBadge) return;
  const d = (defect === 'partial' || defect === 'corrupt') ? defect : '';
  if (!d) {
    _defectField.style.display = 'none';
    _defectBadge.textContent = '';
    _defectBadge.className = 'track-defect-badge';
    if (_defectDetail) _defectDetail.textContent = '';
    return;
  }
  _defectBadge.className = `track-defect-badge track-defect-${d}`;
  _defectBadge.textContent = d;
  _defectBadge.title = detail || '';
  if (_defectDetail) _defectDetail.textContent = detail || '';
  _defectField.style.display = '';
}

async function _loadExtendedInfo(track) {
    const section = document.getElementById('ti-section-module');
    if (!section) return;
    // Reset the subsong picker for the new track — re-shown below only when the
    // file has >1 tune, so an early-return (non-module track) leaves it hidden
    // instead of showing the previous track's tunes.
    if (_subSection) { _subSection.hidden = true; }
    _subState = null;
    // STIL panel + chip badge are SID-only extras — clear them up-front so a
    // non-SID track (or the early-return below) never shows the prior track's.
    _renderStil(null);
    _renderSidChip(null);

    // Beyond the static tracker/SID/MIDI names: exotic-Amiga uade formats
    // (dynamic names, genre-keyed), Atari ST, and PSF console rips all
    // carry module-style extras (subsongs especially).
    const _isScene = isUadeAmigaTrack(track)
        || ATARI_FORMAT_NAMES.has(track.format)
        || PSF_FORMAT_NAMES.has(track.format);
    if (!_MODULE_FORMATS.has(track.format) && !_isScene) {
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

        // Subsongs — the extended endpoint knows tracker/SID counts; the
        // new scene formats (uade/SNDH/SC68) carry theirs on the track
        // metadata itself, so fall back to it.
        const subField = document.getElementById('ti-field-subsongs');
        const subEl = document.getElementById('ti-subsongs');
        const subCount = (data.subsongs && data.subsongs > 1)
            ? data.subsongs
            : (track.subsongs && track.subsongs > 1 ? track.subsongs : null);
        if (subCount) {
            subEl.textContent = subCount;
            subField.style.display = '';
        } else {
            subField.style.display = 'none';
        }
        // Interactive tune list (SID/SNDH/AHX/… with >1 subsong).  default_track
        // + hvsc_lengths + stil come from /extended when the server has them
        // (older servers omit them → picker falls back to first-tune-default, no
        // times, bare tune numbers).
        const stilTitles = _parseStilTitles(data.stil);
        _renderSubsongPicker(track, subCount, data.default_track, data.hvsc_lengths, stilTitles);
        _renderStil(data.stil);
        _renderSidChip(data.sid_model);

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

// ── Pattern grid (tracker modules) ────────────────────────────────────────────
// Fed by GET /api/tracks/{id}/patterns (core/tracker_patterns.py contract):
// order list + per-pattern cell grid + row→time map + song message + tempo.
// AHX/HivelyTracker are absent by design — libopenmpt can't parse them, so
// they'd cost a fetch that always answers ``available: false``.
const _PATTERN_FORMATS = new Set([
  'ProTracker', 'ScreamTracker 3', 'ScreamTracker 2', 'FastTracker 2',
  'Impulse Tracker', 'MultiTracker', 'OctaMED', 'Composer 669',
  'DigiBooster Pro', 'UltraTracker', 'Farandole', 'ASYLUM/DMP',
  'General DigiMusic', 'Imago Orpheus', 'Oktalyzer', 'SoundFX',
  'Grave Composer', 'DSIK',
]);

const _patSection   = document.getElementById('ti-section-patterns');
const _patOrderEl   = document.getElementById('ti-pat-order');
const _patWrapEl    = document.getElementById('ti-pat-gridwrap');
const _patGridEl    = document.getElementById('ti-pat-grid');
const _patNoteEl    = document.getElementById('ti-pat-note');
const _patFollowBtn = document.getElementById('ti-pat-follow');
const _patPosEl     = document.getElementById('ti-pat-pos');
const _msgSection   = document.getElementById('ti-section-message');
const _msgPre       = document.getElementById('ti-message');
const _msgScrollBtn = document.getElementById('ti-msg-scroll');
const _msgMarquee   = document.getElementById('ti-msg-marquee');
const _msgMarqueeText = document.getElementById('ti-msg-marquee-text');

let _pat = null;   // {trackId, data, byIndex, orderPos, curRow, follow,
                   //  flatTimes/flatOrder/flatRow, rowEls, chipEls}

function _resetPatterns() {
  _pat = null;
  if (_patSection) _patSection.hidden = true;
  if (_msgSection) _msgSection.hidden = true;
  if (_patGridEl)  {
    _patGridEl.innerHTML = '';
    // Clear the per-column VU custom properties — the <table> element
    // survives re-renders, so without this a track that ISN'T playing
    // would show the PREVIOUS track's frozen wash on its columns.
    for (let c = 0; c < 64; c++) _patGridEl.style.removeProperty(`--vu${c}`);
  }
  if (_patOrderEl) _patOrderEl.innerHTML = '';
  if (_patPosEl)   _patPosEl.textContent = '';
  if (_patNoteEl)  { _patNoteEl.hidden = true; _patNoteEl.textContent = ''; }
  // Scroller: collapse back to the plain message on every track switch.
  if (_msgMarquee) _msgMarquee.hidden = true;
  if (_msgScrollBtn) _msgScrollBtn.setAttribute('aria-pressed', 'false');
  if (_msgMarqueeText) _msgMarqueeText.textContent = '';
  // "Made with" is populated only from the libopenmpt tracker field (via the
  // patterns payload) and lives in the always-present Module Details section,
  // so it MUST be reset per track — otherwise a SID / uade / any non-pattern
  // track keeps the previous tracker's value ("SID … made with FastTracker 2").
  const mwField = document.getElementById('ti-field-madewith');
  const mwEl    = document.getElementById('ti-madewith');
  if (mwField) mwField.style.display = 'none';
  if (mwEl)    mwEl.textContent = '—';
}

async function _loadPatterns(track) {
  _resetPatterns();
  if (!track || !track.id || !_PATTERN_FORMATS.has(track.format)) return;
  if (_patternsAbort) { try { _patternsAbort.abort(); } catch (_) {} }
  const _c = (typeof AbortController === 'function') ? new AbortController() : null;
  _patternsAbort = _c;
  try {
    const res = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/patterns`,
                            _c ? { signal: _c.signal } : undefined);
    if (!res.ok) return;
    const data = await res.json();
    if (_queue[_idx]?.id !== track.id) return;   // user navigated away
    _applyModuleExtras(track, data);
    if (!data.available || !Array.isArray(data.patterns) || !data.patterns.length) return;

    const byIndex = new Map(data.patterns.map(p => [p.index, p]));
    // Flatten the time map for binary search.  Full per-row map when the
    // server sent one; otherwise order starts only (rows interpolated).
    let flatTimes = null, flatOrder = null, flatRow = null;
    if (Array.isArray(data.row_times) && data.row_times.length) {
      flatTimes = []; flatOrder = []; flatRow = [];
      data.row_times.forEach((rows, o) => rows.forEach((t, r) => {
        flatTimes.push(t); flatOrder.push(o); flatRow.push(r);
      }));
    } else if (Array.isArray(data.order_times) && data.order_times.length) {
      flatTimes = data.order_times.slice();
      flatOrder = data.order_times.map((_, o) => o);
      flatRow   = data.order_times.map(() => 0);
    }

    const playingThis = Player.currentTrack && Player.currentTrack.id === track.id;
    _pat = {
      trackId: track.id, data, byIndex, orderPos: 0, curRow: -1,
      follow: !!(playingThis && flatTimes),
      flatTimes, flatOrder, flatRow, rowEls: [], chipEls: [],
    };
    if (_pat.follow) {
      const i = _timeIndex(Player.currentTime || 0);
      if (i >= 0) _pat.orderPos = _pat.flatOrder[i];
    } else {
      const fo = data.order.findIndex(pi => pi >= 0 && byIndex.has(pi));
      _pat.orderPos = fo >= 0 ? fo : 0;
    }
    _renderOrderStrip();
    _renderPatternGrid();
    _syncFollowBtn();
    _patSection.hidden = false;
    if (Array.isArray(data.truncated) && data.truncated.length) {
      _patNoteEl.textContent =
        `${data.truncated.length} large pattern${data.truncated.length > 1 ? 's' : ''} ` +
        'skipped to keep the grid payload small.';
      _patNoteEl.hidden = false;
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    // Anything else: section simply stays hidden (best-effort, like chapters).
  } finally {
    if (_patternsAbort === _c) _patternsAbort = null;
  }
}

/** Song message / BPM / "Made with" ride the patterns payload. */
function _applyModuleExtras(track, data) {
  const msg = (data && typeof data.message === 'string') ? data.message.replace(/\s+$/, '') : '';
  if (msg && _msgSection && _msgPre) {
    _msgPre.textContent = msg;      // textContent — never innerHTML: scene text is untrusted
    // Scroller line: the message flattened to one row, ◆-separated (classic
    // demoscene scrolltext).  textContent again — untrusted scene text.
    if (_msgMarqueeText) {
      _msgMarqueeText.textContent =
        msg.split('\n').map(s => s.trim()).filter(Boolean).join('  ◆  ');
    }
    _msgSection.hidden = false;
  }
  if (data && data.tempo && !track.bpm) {
    // Initial tempo ≈ BPM for the overwhelming majority of module formats.
    _show('ti-bpm', Math.round(data.tempo));
  }
  const mw = ((data && data.tracker) || '').trim();
  const mwField = document.getElementById('ti-field-madewith');
  const mwEl    = document.getElementById('ti-madewith');
  if (mwField && mwEl) {
    mwEl.textContent = mw || '—';
    mwField.style.display = mw ? '' : 'none';
  }
  _updateSectionVisibility();
}

/** Last index in the flat time map with time <= t (binary search). */
function _timeIndex(t) {
  const a = _pat && _pat.flatTimes;
  if (!a || !a.length) return -1;
  let lo = 0, hi = a.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid] <= t) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return ans;
}

function _renderOrderStrip() {
  if (!_patOrderEl || !_pat) return;
  _patOrderEl.innerHTML = '';
  _pat.chipEls = [];
  _pat.data.order.forEach((pi, pos) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ti-pat-chip';
    if (pi < 0) {
      chip.classList.add('ti-pat-marker');
      chip.textContent = '—';
      chip.title = `Order ${pos}: marker`;
    } else if (!_pat.byIndex.has(pi)) {
      chip.classList.add('ti-pat-marker');
      chip.textContent = String(pi).padStart(2, '0');
      chip.title = `Order ${pos}: pattern ${pi} (grid data skipped)`;
    } else {
      chip.textContent = String(pi).padStart(2, '0');
      chip.title = `Order ${pos}: pattern ${pi}`;
      chip.addEventListener('click', () => {
        _pat.orderPos = pos;
        _pat.follow = false;          // manual navigation pauses follow
        _syncFollowBtn();
        _updateActiveChip();
        _renderPatternGrid();
      });
    }
    _patOrderEl.appendChild(chip);
    _pat.chipEls.push(chip);
  });
  _updateActiveChip();
}

function _updateActiveChip() {
  if (!_pat) return;
  _pat.chipEls.forEach((c, pos) => c.classList.toggle('active', pos === _pat.orderPos));
  const active = _pat.chipEls[_pat.orderPos];
  if (active && _patOrderEl) {
    _patOrderEl.scrollLeft = active.offsetLeft - _patOrderEl.clientWidth / 2 + active.offsetWidth / 2;
  }
  if (_patPosEl) {
    _patPosEl.textContent =
      `position ${String(_pat.orderPos).padStart(2, '0')} / ${_pat.data.order.length}`;
  }
}

function _renderPatternGrid() {
  if (!_patGridEl || !_pat) return;
  const pi = _pat.data.order[_pat.orderPos];
  const pattern = pi >= 0 ? _pat.byIndex.get(pi) : null;
  _pat.curRow = -1;
  _pat.rowEls = [];
  if (!pattern || !pattern.rows.length) { _patGridEl.innerHTML = ''; return; }
  const nCh = _pat.data.channels;
  const hueBase = _pat.data.channels_total || nCh;
  const esc = _escHtml;

  let html = '<thead><tr><th class="tprn" scope="col"></th>';
  for (let c = 0; c < nCh; c++) {
    const hue = Math.round((c * 360 / hueBase) + 15);
    html += `<th scope="col" style="--h:${hue}">${c + 1}` +
            `<span class="tpdot" style="--h:${hue};--vc:var(--vu${c},0)"></span></th>`;
  }
  html += '</tr></thead><tbody>';
  pattern.rows.forEach((row, r) => {
    html += `<tr${r % 4 === 0 ? ' class="tpb"' : ''}>` +
            `<td class="tprn">${String(r).padStart(2, '0')}</td>`;
    for (let c = 0; c < nCh; c++) {
      const hue = Math.round((c * 360 / hueBase) + 15);
      const cell = row[c] || '';
      // libopenmpt fixed 13-wide layout (pad=1):
      //   note[0:3] sep[3] inst[4:6] vol[6:9] sep[9] fx[10:13]
      // The volume column is THREE chars — command letter + 2 digits
      // ("v20"/"p3A"), or " .." when empty — so it starts at index 6 and
      // has no separator before it (the leading space handles alignment).
      // Slicing at [7:9] silently dropped the command letter.
      const empty = /^[.\s]+$/.test(cell);
      const body = empty
        ? `<span class="tpe">${esc(cell)}</span>`
        : `<span class="tpn">${esc(cell.slice(0, 3))}</span> ` +
          `<span class="tpi">${esc(cell.slice(4, 6))}</span>` +
          `<span class="tpv">${esc(cell.slice(6, 9))}</span> ` +
          `<span class="tpf">${esc(cell.slice(10, 13))}</span>`;
      html += `<td class="tpc" style="--h:${hue};--vc:var(--vu${c},0)">${body}</td>`;
    }
    html += '</tr>';
  });
  html += '</tbody>';
  _patGridEl.innerHTML = html;
  const tbody = _patGridEl.tBodies[0];
  _pat.rowEls = tbody ? Array.from(tbody.rows) : [];
}

function _setPlayheadRow(r) {
  if (!_pat || r === _pat.curRow) return;
  const prev = _pat.rowEls[_pat.curRow];
  if (prev) prev.classList.remove('tph');
  const cur = _pat.rowEls[r];
  if (cur) {
    cur.classList.add('tph');
    // Scroll the grid's own container — scrollIntoView would also scroll
    // the modal pane behind it.
    if (_patWrapEl) {
      _patWrapEl.scrollTop = cur.offsetTop - _patWrapEl.clientHeight / 2 + cur.offsetHeight / 2;
    }
  }
  _pat.curRow = r;
}

function _syncFollowBtn() {
  if (!_patFollowBtn || !_pat) return;
  _patFollowBtn.style.display = _pat.flatTimes ? '' : 'none';
  _patFollowBtn.setAttribute('aria-pressed', _pat.follow ? 'true' : 'false');
}

if (_patFollowBtn) {
  _patFollowBtn.addEventListener('click', () => {
    if (!_pat || !_pat.flatTimes) return;
    _pat.follow = !_pat.follow;
    _syncFollowBtn();
    if (_pat.follow) _patFollowTick(Player.currentTime || 0);
  });
}

// Song-message scroller toggle — reveal the one-line demoscene marquee.
if (_msgScrollBtn && _msgMarquee) {
  _msgScrollBtn.addEventListener('click', () => {
    const show = _msgMarquee.hidden;
    _msgMarquee.hidden = !show;
    _msgScrollBtn.setAttribute('aria-pressed', show ? 'true' : 'false');
  });
}

function _patFollowTick(t) {
  if (!_pat || !_pat.follow || !isOpen() || !_patSection || _patSection.hidden) return;
  if (!Player.currentTrack || Player.currentTrack.id !== _pat.trackId) return;
  const i = _timeIndex(t);
  if (i < 0) return;
  const o = _pat.flatOrder[i];
  if (o !== _pat.orderPos) {
    _pat.orderPos = o;
    _updateActiveChip();
    _renderPatternGrid();
  }
  let r = _pat.flatRow[i];
  if (!_pat.data.row_times) {
    // Order-start times only — interpolate the row inside this order.
    // Mid-pattern speed commands drift within the pattern and re-sync at
    // the next order boundary; the full map avoids this when available.
    const times = _pat.data.order_times;
    const start = times[o];
    const end   = (o + 1 < times.length) ? times[o + 1] : (_pat.data.duration || start + 1);
    const nRows = _pat.rowEls.length;
    if (end > start && nRows > 0) {
      r = Math.max(0, Math.min(nRows - 1, Math.floor((t - start) / (end - start) * nRows)));
    }
  }
  _setPlayheadRow(r);
}

Player.on('timeupdate', ({ current }) => { _patFollowTick(current); });

// ── Live VU → pattern-column backdrop ─────────────────────────────────────────
// app.js's VU tick (66 ms) calls this with the per-channel levels it just
// painted onto the meter bars; we mirror them onto the grid's per-column
// custom properties.  Alpha is clamped to ≤0.16 so cell text never fights
// the wash.  Self-guarding: costs one comparison when the grid isn't open.
window.__sbVuTap = function (levels, count) {
  if (!_pat || !_patSection || _patSection.hidden || !isOpen()) return;
  if (!Player.currentTrack || Player.currentTrack.id !== _pat.trackId) return;
  if (!_patGridEl) return;
  const n = Math.min(count, _pat.data.channels);
  for (let c = 0; c < n; c++) {
    _patGridEl.style.setProperty(`--vu${c}`, (levels[c] * 0.16).toFixed(3));
  }
};

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
if (scenePane) {
  scenePane.setAttribute('role', 'tabpanel');
  scenePane.setAttribute('aria-labelledby', 'ti-tab-scene');
  scenePane.tabIndex = 0;
}

function _switchTab(tab) {
  _activeTab = tab;
  tabInfo.classList.toggle('active', tab === 'info');
  tabLyrics.classList.toggle('active', tab === 'lyrics');
  if (tabScene) tabScene.classList.toggle('active', tab === 'scene');
  tabInfo.setAttribute('aria-selected',   tab === 'info'   ? 'true' : 'false');
  tabLyrics.setAttribute('aria-selected', tab === 'lyrics' ? 'true' : 'false');
  if (tabScene) tabScene.setAttribute('aria-selected', tab === 'scene' ? 'true' : 'false');
  // Single show/hide convention: every pane defaults to ``hidden`` and we
  // add ``active`` for the visible one.  Previously meta used ``hidden``
  // while lyrics used ``active``, so during the open animation both
  // panes could be visible (Visual-Test #1 caught the race).
  metaPane.classList.toggle('active', tab === 'info');
  metaPane.classList.toggle('hidden', tab !== 'info');
  lyricsPane.classList.toggle('active', tab === 'lyrics');
  lyricsPane.classList.toggle('hidden', tab !== 'lyrics');
  if (scenePane) {
    scenePane.classList.toggle('active', tab === 'scene');
    scenePane.classList.toggle('hidden', tab !== 'scene');
  }
  if (tab === 'lyrics') _loadLyrics(_queue[_idx]);
  else if (tab === 'scene') _loadScene(_queue[_idx]);
}

tabInfo.addEventListener('click',   () => _switchTab('info'));
tabLyrics.addEventListener('click', () => _switchTab('lyrics'));
if (tabScene) tabScene.addEventListener('click', () => _switchTab('scene'));

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
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
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
