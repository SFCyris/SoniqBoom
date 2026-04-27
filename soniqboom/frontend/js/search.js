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
import { artPlaceholderEmoji } from './utils.js';

const input  = document.getElementById('search-input');
const clear  = document.getElementById('search-clear');

let previewTimer     = null;
let _previewDropdown = null;
let _previewVisible  = false;
let _selectedPreview = -1;   // keyboard-navigated preview index

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

function _showPreview(tracks) {
  const dd = _ensureDropdown();
  dd.innerHTML = '';
  _selectedPreview = -1;

  if (!tracks.length) {
    dd.innerHTML = '<div class="sp-empty">No matches</div>';
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

    // Async art load — swaps emoji for real thumbnail once the image is ready
    if (t.cover_art) {
      const artBox = row.querySelector('.sp-art');
      const img = new Image();
      img.alt = '';
      img.onload = () => {
        artBox.innerHTML = '';
        artBox.appendChild(img);
      };
      img.src = t.cover_art;  // fires request; onload swaps content when ready
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

// ── Main search ─────────────────────────────────────────────────────────────
async function query(text) {
  _hidePreview();
  if (!text.trim()) { Library.showAll(); return; }
  const res = await fetch(`/api/search?q=${encodeURIComponent(text)}&limit=200`);
  const tracks = await res.json();
  Library.renderTracks(tracks);
}

async function _quickSearch(text) {
  if (!text.trim()) { _hidePreview(); return; }
  try {
    const res = await fetch(`/api/search/quick?q=${encodeURIComponent(text)}&limit=8`);
    const tracks = await res.json();
    _showPreview(tracks);
  } catch {
    _hidePreview();
  }
}

// ── Event handlers ──────────────────────────────────────────────────────────
input.addEventListener('input', () => {
  const val = input.value;
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
  }
});

clear.addEventListener('click', () => {
  input.value = '';
  clear.hidden = true;
  _hidePreview();
  Library.showAll();
  input.focus();
});

export const Search = { query };
