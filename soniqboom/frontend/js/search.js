// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * search.js — Search bar with debounce, inline preview dropdown, and advanced syntax.
 * Exports: Search singleton
 *
 * Supports advanced syntax: artist:Ghost album:Impera year:>2020 format:FLAC
 * Values with spaces: artist:"The Ghost Inside"
 */
import { Library }             from './library.js';
import { Player }              from './player.js';
import { artPlaceholderEmoji, Toast } from './utils.js';

const input  = document.getElementById('search-input');
const clear  = document.getElementById('search-clear');

let previewTimer     = null;
let _previewDropdown = null;
let _previewVisible  = false;
let _selectedPreview = -1;   // keyboard-navigated preview index

// Preview art elements have no AbortController — once an Image src is set
// the browser queues the GET and we can't cancel it.  Track every image we
// kick off so the next keystroke can abort outstanding loads by clearing
// `src` (treated as "no longer interested" by the browser).
let _previewArtImgs = [];

// ── Preview dropdown ────────────────────────────────────────────────────────
function _ensureDropdown() {
  if (_previewDropdown) return _previewDropdown;
  const dd = document.createElement('div');
  dd.id = 'search-preview';
  dd.className = 'search-preview';
  dd.addEventListener('mousedown', (e) => e.preventDefault()); // prevent blur
  input.parentElement.appendChild(dd);
  _previewDropdown = dd;
  return dd;
}

function _cancelInflightArt() {
  // Setting `src = ''` tells the browser we're no longer interested.  This
  // doesn't always cancel an in-flight network request, but it does stop
  // the browser from decoding the response and prevents the onload swap
  // from firing on a stale preview row.
  for (const img of _previewArtImgs) {
    try {
      img.onload = null;
      img.onerror = null;
      img.src = '';
    } catch (_) {}
  }
  _previewArtImgs = [];
}

function _showPreview(tracks) {
  const dd = _ensureDropdown();
  // Drop refs to art images from the prior preview before we rebuild — any
  // loads still in flight no longer have a target row.
  _cancelInflightArt();
  dd.innerHTML = '';
  _selectedPreview = -1;

  // Flag tokens that look like field operators but aren't supported — this
  // is the most common reason a "syntactically valid" query returns nothing.
  const badOps = _detectBadOperators(input.value);
  _renderBadOpsWarning(dd, badOps);

  if (!tracks.length) {
    const empty = document.createElement('div');
    empty.className = 'sp-empty';
    empty.textContent = 'No matches';
    dd.appendChild(empty);
    dd.classList.add('visible');
    _previewVisible = true;
    return;
  }

  tracks.slice(0, 8).forEach((t, i) => {
    const row = document.createElement('div');
    row.className = 'sp-row';
    row.dataset.idx = i;

    // Always show emoji placeholder immediately; swap to real art async if available
    row.innerHTML = `
      <div class="sp-art"><span class="sp-art-ph">${artPlaceholderEmoji(t)}</span></div>
      <div class="sp-info">
        <div class="sp-title">${_esc(t.title || '—')}</div>
        <div class="sp-sub">${_esc(t.artist || t.album_artist || '')}${t.album ? ' — ' + _esc(t.album) : ''}</div>
      </div>
      <div class="sp-dur">${_fmtDur(t.duration)}</div>`;

    // Async art load — swaps emoji for real thumbnail once the image is ready.
    // ``track.cover_art`` is only populated when the scanner extracted art
    // at scan time; for FTP/SMB/ZIP tracks it's null, but ``/api/art/{id}``
    // extracts on demand.  Try ``cover_art`` first (free if non-null) and
    // fall back to the on-demand endpoint so search previews show art
    // consistently with the bottom-left player and row covers.
    // ``fetchpriority=low`` keeps these preview thumbnails from competing
    // with the actual /api/search request that produced the dropdown, and
    // we stash a ref so the next keystroke can cancel by clearing ``src``.
    const _previewSrc = t.cover_art || (t.id ? `/api/art/${t.id}?size=sm&fallback=404` : '');
    if (_previewSrc) {
      const artBox = row.querySelector('.sp-art');
      const img = new Image();
      img.alt = '';
      if ('fetchPriority' in img) img.fetchPriority = 'low';
      else img.setAttribute('fetchpriority', 'low');
      img.onload = () => {
        if (!img.src) return;     // cancelled before load completed
        artBox.innerHTML = '';
        artBox.appendChild(img);
      };
      // No onerror handler needed — the placeholder remains in the DOM
      // until ``onload`` swaps it; a 404 just leaves the emoji visible.
      _previewArtImgs.push(img);
      img.src = _previewSrc;  // fires request; onload swaps content when ready
    }

    row.addEventListener('click', () => {
      Player.setQueue([t], 0);
      _hidePreview();
      // Also do a full search to populate the library
      query(input.value);
    });
    dd.appendChild(row);
  });

  // Always show "press Enter" hint so behaviour is discoverable
  const hint = document.createElement('div');
  hint.className = tracks.length > 8 ? 'sp-more' : 'sp-hint';
  hint.textContent = tracks.length > 8
    ? `${tracks.length - 8} more — press ↵ for full results`
    : 'Press ↵ for full results';
  dd.appendChild(hint);

  dd.classList.add('visible');
  _previewVisible = true;
}

function _hidePreview() {
  if (_previewDropdown) {
    _previewDropdown.classList.remove('visible');
    _previewVisible = false;
    _selectedPreview = -1;
  }
  // Cancel any pending art loads — the rows they were targeting are about
  // to be replaced or removed.
  _cancelInflightArt();
}

function _navigatePreview(delta) {
  if (!_previewVisible || !_previewDropdown) return;
  const rows = _previewDropdown.querySelectorAll('.sp-row');
  if (!rows.length) return;
  rows.forEach(r => r.classList.remove('sp-highlighted'));
  _selectedPreview = Math.max(-1, Math.min(rows.length - 1, _selectedPreview + delta));
  if (_selectedPreview >= 0) rows[_selectedPreview].classList.add('sp-highlighted');
}

function _playHighlighted() {
  if (_selectedPreview < 0 || !_previewDropdown) return false;
  const row = _previewDropdown.querySelector(`.sp-row[data-idx="${_selectedPreview}"]`);
  if (row) { row.click(); return true; }
  return false;
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function _esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _fmtDur(sec) {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ── Syntax helper / operator hints ──────────────────────────────────────────
//
// The first time the user focuses an empty search box we surface the
// supported field operators (artist:, album:, year:, format:, …) in a
// dismissable hint inside the preview dropdown.  When a query contains
// what looks like an operator (``word:value``) that isn't one we know
// about, we highlight that token red so the user understands why the
// preview is empty.
const _SUPPORTED_OPS = new Set([
  'artist', 'album_artist', 'albumartist', 'album',
  'year', 'genre', 'format', 'title', 'composer',
]);
let _syntaxHintShown = false;

function _detectBadOperators(text) {
  // Match tokens of shape ``word:value`` — quoted values are OK, year:>NNN
  // and year:<NNN are OK because we accept the operator name (year).  We
  // strip comparison operators for the lookup.
  const tokens = text.match(/(\b[a-zA-Z_]+):/g) || [];
  const bad = [];
  for (const tk of tokens) {
    const name = tk.slice(0, -1).toLowerCase();
    if (!_SUPPORTED_OPS.has(name)) bad.push(name);
  }
  // Dedupe while preserving order
  return [...new Set(bad)];
}

function _renderSyntaxHint(container) {
  const hint = document.createElement('div');
  hint.className = 'sp-syntax-hint';
  hint.innerHTML = `
    <div class="sp-hint-title">Try field operators:</div>
    <div class="sp-hint-ops">
      <code>artist:</code> <code>album:</code> <code>year:&gt;2020</code> <code>format:FLAC</code>
    </div>`;
  container.appendChild(hint);
}

function _renderBadOpsWarning(container, badOps) {
  if (!badOps.length) return;
  const warn = document.createElement('div');
  warn.className = 'sp-bad-ops';
  // Build content manually so the user-supplied bad-op names go through
  // textContent rather than innerHTML interpolation.
  const prefix = document.createElement('span');
  prefix.textContent = `Unknown field${badOps.length > 1 ? 's' : ''}: `;
  warn.appendChild(prefix);
  badOps.forEach((op, i) => {
    if (i > 0) warn.appendChild(document.createTextNode(', '));
    const tag = document.createElement('span');
    tag.className = 'sp-bad-op';
    tag.textContent = `${op}:`;
    warn.appendChild(tag);
  });
  container.appendChild(warn);
}

// ── Main search ─────────────────────────────────────────────────────────────
async function query(text) {
  _hidePreview();
  if (!text.trim()) { Library.showAll(); return; }
  // Previously an uncaught fetch/json error here left the library blank with
  // no explanation.  Surface failures and keep the prior view intact.
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(text)}&limit=200`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const tracks = await res.json();
    Library.renderTracks(tracks);
  } catch (err) {
    console.error('Search failed:', err);
    Toast.error('Search failed — check the server log.');
  }
}

// Abort the previous in-flight request when the user keeps typing — Perf
// #2 caught a race where stale responses from earlier keystrokes could
// arrive after newer ones, briefly showing the wrong preview.
let _quickAbort = null;
async function _quickSearch(text) {
  if (!text.trim()) { _hidePreview(); return; }
  if (_quickAbort) {
    try { _quickAbort.abort(); } catch {}
  }
  const ctl = new AbortController();
  _quickAbort = ctl;
  try {
    const res = await fetch(
      `/api/search/quick?q=${encodeURIComponent(text)}&limit=8`,
      { signal: ctl.signal },
    );
    const tracks = await res.json();
    // Only render if we're still the latest request (another keystroke
    // could have replaced _quickAbort while we were awaiting json).
    if (_quickAbort === ctl) _showPreview(tracks);
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    _hidePreview();
  } finally {
    if (_quickAbort === ctl) _quickAbort = null;
  }
}

// ── Event handlers ──────────────────────────────────────────────────────────
input.addEventListener('input', () => {
  const val = input.value;
  // If the clear button currently has focus and we're about to hide it,
  // move focus back to the input so it doesn't drop to body.
  if (!val && !clear.hidden && document.activeElement === clear) {
    input.focus();
  }
  clear.hidden = !val;

  // Show preview dropdown while typing — no automatic full search.
  // Full search only fires on Enter (keydown handler) or dropdown item click.
  clearTimeout(previewTimer);
  if (val.trim()) {
    previewTimer = setTimeout(() => _quickSearch(val), 150);
  } else {
    _hidePreview();
    Library.showAll();
  }
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _navigatePreview(1);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    _navigatePreview(-1);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (!_playHighlighted()) {
      _hidePreview();
      query(input.value);
    }
  } else if (e.key === 'Escape') {
    if (_previewVisible) {
      e.stopPropagation();
      _hidePreview();
    } else {
      input.blur();
    }
  }
});

input.addEventListener('blur', () => {
  // Small delay to allow click on preview items
  setTimeout(_hidePreview, 200);
});

input.addEventListener('focus', () => {
  if (input.value.trim() && _previewDropdown?.children.length) {
    _previewDropdown.classList.add('visible');
    _previewVisible = true;
    return;
  }
  // Empty input + first focus this session → show the operator hint.
  if (!input.value.trim() && !_syntaxHintShown) {
    const dd = _ensureDropdown();
    dd.innerHTML = '';
    _renderSyntaxHint(dd);
    dd.classList.add('visible');
    _previewVisible  = true;
    _syntaxHintShown = true;
  }
});

clear.addEventListener('click', () => {
  input.value = '';
  clear.hidden = true;
  input.focus();  // clear is now hidden — keep keyboard focus on the search field
  _hidePreview();
  Library.showAll();
  input.focus();
});

export const Search = { query };
