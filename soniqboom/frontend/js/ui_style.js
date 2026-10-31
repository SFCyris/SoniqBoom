// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * ui_style.js — the one look ("style") shared by the Equalizer, Stations,
 * Playlists and Queue panels, the player bar (css/player-styles.css, the VU
 * meter's needles), Track Info (css/trackinfo-styles.css), the lyrics panel
 * (css/lyrics-styles.css) and the phone page's Now Playing (mobile.css):
 * 'modern' or 'retro' (Retro hi-fi).  Switching it
 * in any panel switches every panel, and the choice is kept.
 *
 *   getUiStyle()          → 'modern' | 'retro' (default 'retro')
 *   setUiStyle(style)     keeps it (localStorage 'sb_ui_style'), sets
 *                         <html data-ui-style>, tells every listener; an
 *                         unknown name is ignored (returns false)
 *   onUiStyleChange(cb)   cb(style) on every change (this tab or another);
 *                         returns the unsubscribe function
 *   bindUiStyleSwitch(el) wires a radiogroup of button[data-ui-style] (the
 *                         "Modern / Retro hi-fi" switch): click / arrows /
 *                         Home / End choose, roving tabindex, reflects the
 *                         current style; returns the unbind function
 *
 * Every change also fires CustomEvent('sb:ui-style', {detail:{style}}) on
 * document.  Another open tab follows through the 'storage' event.
 *
 * First run: when 'sb_ui_style' is absent, the equalizer's own earlier choice
 * (sb_eq.style, 2.0.0 pre-release) seeds it; with neither, Retro hi-fi.
 *
 * A page with one look only — the phone page, <html data-ui-style-fixed="retro">
 * — keeps that look: getUiStyle() returns it, setUiStyle() changes and writes
 * nothing, and another tab's switch is not followed.
 */

const KEY = 'sb_ui_style';
const STYLES = ['modern', 'retro'];
const DEFAULT = 'retro';

const _valid = (s) => STYLES.includes(s);

function _read() {
  let s = null;
  try { s = localStorage.getItem(KEY); } catch (_) { s = null; }
  if (_valid(s)) return s;
  // Seed from the equalizer's style (it kept its own before the switch was shared).
  let seed = null;
  try {
    const eq = JSON.parse(localStorage.getItem('sb_eq') || 'null');
    if (eq && !Array.isArray(eq) && _valid(eq.style)) seed = eq.style;
  } catch (_) { seed = null; }
  const v = seed || DEFAULT;
  if (seed) { try { localStorage.setItem(KEY, v); } catch (_) { /* private mode */ } }
  return v;
}

// The look a page is fixed to (data-ui-style-fixed on <html>), or null.
const _fixed = (() => {
  try {
    const f = document.documentElement.dataset.uiStyleFixed;
    return _valid(f) ? f : null;
  } catch (_) { return null; }
})();

let _style = _fixed || _read();
const _subs = new Set();

function _apply() {
  try { document.documentElement.dataset.uiStyle = _style; } catch (_) {}
}

function _notify() {
  _apply();
  for (const cb of Array.from(_subs)) {
    try { cb(_style); } catch (e) { console.warn('ui-style listener failed:', e); }
  }
  try { document.dispatchEvent(new CustomEvent('sb:ui-style', { detail: { style: _style } })); } catch (_) {}
}

export function getUiStyle() { return _style; }

export function setUiStyle(style) {
  if (!_valid(style)) return false;
  if (_fixed) return style === _fixed;            // (a one-look page: nothing switches it)
  try { localStorage.setItem(KEY, style); } catch (_) { /* quota / private mode: this tab still switches */ }
  if (style === _style) return true;
  _style = style;
  _notify();
  return true;
}

export function onUiStyleChange(cb) {
  if (typeof cb !== 'function') return () => {};
  _subs.add(cb);
  return () => { _subs.delete(cb); };
}

// Another tab chose a style (or cleared the key): follow it.
window.addEventListener('storage', (e) => {
  if (_fixed) return;
  if (e.key !== KEY && e.key !== null) return;     // null = storage cleared
  const next = _read();
  if (next === _style) return;
  _style = next;
  _notify();
});

/** Wire a "Modern / Retro hi-fi" radiogroup: its buttons carry data-ui-style. */
export function bindUiStyleSwitch(group) {
  if (!group) return () => {};
  const btns = () => Array.from(group.querySelectorAll('button[data-ui-style]'));
  const render = (s) => {
    for (const b of btns()) {
      const on = b.dataset.uiStyle === s;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    }
  };
  const onClick = (e) => {
    const b = e.target.closest && e.target.closest('button[data-ui-style]');
    if (b && group.contains(b)) setUiStyle(b.dataset.uiStyle);
  };
  // A radiogroup's keys (APG).  stopPropagation: the arrows and Home / End are
  // app-wide shortcuts too (volume, seek).
  const onKey = (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const list = btns();
    const at = list.indexOf(document.activeElement);
    if (at < 0) return;
    const n = list.length;
    let to;
    switch (e.key) {
      case 'ArrowRight': case 'ArrowDown': to = (at + 1) % n; break;
      case 'ArrowLeft':  case 'ArrowUp':   to = (at - 1 + n) % n; break;
      case 'Home': to = 0; break;
      case 'End':  to = n - 1; break;
      case ' ': case 'Enter': e.stopPropagation(); return;   // the button's own press
      default: return;
    }
    e.preventDefault();
    e.stopPropagation();
    list[to].focus();
    setUiStyle(list[to].dataset.uiStyle);
  };
  group.addEventListener('click', onClick);
  group.addEventListener('keydown', onKey);
  const off = onUiStyleChange(render);
  render(_style);
  return () => { off(); group.removeEventListener('click', onClick); group.removeEventListener('keydown', onKey); };
}

_apply();
