// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * radio.js — Radio mode: the foreground listening experience.
 *
 * Starting a radio opens a centered overlay that IS the radio: the cover art
 * large on the left with a live oscilloscope playing over it, the track info
 * and what's coming next beside it.  Leaving the view — ×, Esc, backdrop or
 * "Stop radio" — ends the radio session: the 📻 button unlights and refilling
 * stops, while the queued tracks keep playing.  While the radio is on, the
 * queue refills itself: when fewer than REFILL_AT tracks remain, more similar
 * tracks are fetched seeded from what's playing now.
 */
import { Player } from './player.js';
import { Playlist } from './playlist.js';
import { Toast } from './utils.js';

const REFILL_AT = 8;       // refill when this few tracks remain after current
const REFILL_BATCH = 30;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let _active = false;
let _seedLabel = '';
let _refillBusy = false;
let _raf = null;
let _scopeBuf = null;
let _focusReturn = null;

// ── Overlay rendering ─────────────────────────────────────────────────────────

function _overlayOpen() {
  return !$('radio-overlay')?.classList.contains('hidden');
}

function _renderNow() {
  const t = Player.currentTrack;
  if (!t) return;
  const img = $('radio-art');
  if (img) {
    img.src = `/api/art/${encodeURIComponent(t.id)}?size=lg&fallback=404`;
    img.onerror = () => { img.removeAttribute('src'); };
  }
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v || ''; };
  set('radio-title', t.title || '—');
  set('radio-artist', [t.artist, t.album].filter(Boolean).join(' · '));
  set('radio-seed-label', _seedLabel);
  const play = $('radio-play');
  if (play) play.textContent = Player.playing ? '⏸' : '▶';
  _renderUpNext();
}

function _renderUpNext() {
  const ul = $('radio-upnext-list');
  if (!ul) return;
  ul.innerHTML = '';
  const q = Player.queue, idx = Player.queueIdx;
  const next = q.slice(idx + 1, idx + 13);
  if (!next.length) {
    ul.innerHTML = '<li class="radio-upnext-empty">Fetching more similar tracks…</li>';
    return;
  }
  next.forEach((t, i) => {
    const li = document.createElement('li');
    li.innerHTML =
      `<img class="radio-upnext-art" alt="" loading="lazy" decoding="async"
            src="/api/art/${encodeURIComponent(t.id)}?size=sm&fallback=404">` +
      `<span class="radio-upnext-title">${esc(t.title || '—')}</span>` +
      `<span class="radio-upnext-artist">${esc(t.artist || '')}</span>`;
    const im = li.querySelector('img');
    im.onerror = () => { im.classList.add('noart'); im.removeAttribute('src'); };
    li.title = 'Play now';
    li.addEventListener('click', () => { Player.setQueue(q, idx + 1 + i); });
    ul.appendChild(li);
  });
}

// ── Oscilloscope over the art ────────────────────────────────────────────────

function _sizeScope() {
  const cv = $('radio-scope'), wrap = $('radio-art-wrap');
  if (!cv || !wrap) return;
  const dpr = window.devicePixelRatio || 1;
  const r = wrap.getBoundingClientRect();
  cv.width = Math.max(2, Math.round(r.width * dpr));
  cv.height = Math.max(2, Math.round(r.height * dpr));
}

function _drawScope() {
  _raf = null;
  if (!_overlayOpen()) return;
  const cv = $('radio-scope');
  const an = Player.analyser;
  if (cv) {
    const g = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    g.clearRect(0, 0, W, H);
    g.lineWidth = Math.max(2, H / 220);
    g.strokeStyle = 'rgba(80,255,140,0.9)';        // phosphor green
    g.shadowColor = 'rgba(80,255,140,0.55)';
    g.shadowBlur = Math.max(6, H / 60);
    g.beginPath();
    if (an) {
      if (!_scopeBuf || _scopeBuf.length !== an.fftSize) {
        _scopeBuf = new Uint8Array(an.fftSize);
      }
      an.getByteTimeDomainData(_scopeBuf);
      const n = _scopeBuf.length;
      for (let i = 0; i < n; i++) {
        const x = (i / (n - 1)) * W;
        const y = H / 2 + ((_scopeBuf[i] - 128) / 128) * (H * 0.38);
        if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
      }
    } else {
      g.moveTo(0, H / 2); g.lineTo(W, H / 2);      // no audio chain yet
    }
    g.stroke();
  }
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  _raf = requestAnimationFrame(_drawScope);
}

function _startScope() {
  if (_raf) cancelAnimationFrame(_raf);
  _sizeScope();
  _raf = requestAnimationFrame(_drawScope);
}

function _stopScope() {
  if (_raf) { cancelAnimationFrame(_raf); _raf = null; }
}

// ── Public API ────────────────────────────────────────────────────────────────

function start(seedTrack) {
  _active = true;
  _seedLabel = seedTrack?.artist || seedTrack?.title || 'this track';
  $('radio-seed-label') && ($('radio-seed-label').textContent = _seedLabel);
  $('btn-radio')?.classList.add('on');
  // The radio's curated order replaces shuffle while the session runs, so
  // "next" follows the mix instead of random-jumping to an unrelated artist.
  try { Player.setRadioActive(true); } catch (_) {}
  openOverlay();
}

function stop() {
  if (!_active && $('radio-overlay')?.classList.contains('hidden')) return;
  _active = false;
  _refillBusy = false;
  $('btn-radio')?.classList.remove('on');
  // Radio over — the queue keeps playing, and the shuffle toggle resumes effect.
  try { Player.setRadioActive(false); } catch (_) {}
  closeOverlay();
  Toast?.info?.('Radio stopped — the queue keeps playing.');
}

function openOverlay() {
  const ov = $('radio-overlay');
  if (!ov) return;
  _focusReturn = document.activeElement;
  ov.classList.remove('hidden');
  _renderNow();
  _startScope();
  $('radio-close')?.focus();
}

function closeOverlay() {
  const ov = $('radio-overlay');
  if (!ov || ov.classList.contains('hidden')) return;
  ov.classList.add('hidden');
  _stopScope();
  try { _focusReturn?.focus?.(); } catch (_) {}
}

async function saveAsPlaylist() {
  const ids = Player.queue.slice(Player.queueIdx).map(t => t.id).filter(Boolean);
  if (!ids.length) { Toast?.info?.('Nothing to save yet.'); return; }
  try {
    const r = await fetch('/api/playlists', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: `Radio · ${_seedLabel}`, track_ids: ids }),
    });
    if (!r.ok) {
      let msg = 'Could not save the playlist.';
      try { msg = (await r.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    (Toast?.ok || Toast?.info)?.(`Saved as “Radio · ${_seedLabel}” (${ids.length} tracks).`);
    try { Playlist.refresh(); } catch (_) {}
  } catch (e) {
    Toast?.error?.(e.message || 'Could not save the playlist.');
  }
}

async function maybeRefill(force = false) {
  if (!_active || _refillBusy) return 0;
  const remaining = Player.queue.length - Player.queueIdx - 1;
  if (!force && remaining >= REFILL_AT) return 0;
  const seed = Player.currentTrack;
  if (!seed || !seed.id) return 0;
  _refillBusy = true;
  try {
    const r = await fetch(`/api/smart/radio?seed=${encodeURIComponent(seed.id)}&limit=${REFILL_BATCH}`,
                          { cache: 'no-store' });
    if (!r.ok) return 0;
    const mix = await r.json();
    const have = new Set(Player.queue.map(t => t.id));
    let added = 0;
    for (const t of (Array.isArray(mix) ? mix.slice(1) : [])) {
      if (t && t.id && !have.has(t.id)) { Player.addToQueue(t); have.add(t.id); added++; }
    }
    if (added && _overlayOpen()) _renderUpNext();
    return added;
  } catch (_) {
    return 0;
  } finally {
    _refillBusy = false;
  }
}

// ── Wiring ────────────────────────────────────────────────────────────────────

function _bind() {
  $('radio-close')?.addEventListener('click', stop);
  $('radio-overlay')?.addEventListener('click', (e) => {
    if (e.target === $('radio-overlay')) stop();           // backdrop click = exit radio
  });
  $('radio-stop')?.addEventListener('click', stop);
  $('radio-save')?.addEventListener('click', saveAsPlaylist);
  $('radio-prev')?.addEventListener('click', () => Player.prev());
  $('radio-next')?.addEventListener('click', () => Player.next());
  $('radio-play')?.addEventListener('click', () => { Player.playPause(); _renderNow(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _overlayOpen()) { e.stopPropagation(); stop(); }
  }, true);
  window.addEventListener('resize', () => { if (_overlayOpen()) _sizeScope(); });
  Player.on('trackchange', () => {
    if (!_active) return;
    if (_overlayOpen()) _renderNow();
    maybeRefill();
  });
  Player.on('statechange', () => { if (_overlayOpen()) _renderNow(); });
  Player.on('queuechange', () => { if (_overlayOpen()) _renderUpNext(); });
}
_bind();

export const RadioMode = {
  get active() { return _active; },
  start, stop, openOverlay, closeOverlay, saveAsPlaylist, maybeRefill,
};
// Test/debug escape hatch — same pattern as window.Toast.
window.RadioMode = RadioMode;
