// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * queue.js — Queue panel module.
 *
 * A header ("Up next": how many tracks follow the current one, the time left,
 * where the queue came from when the player knows — Player.queueSource — and
 * Shuffle / Repeat), then the queue's rows: cover, title + artist, a format
 * chip, a mini waveform (the stored one — playlist/waves.js; the playing row
 * shows its progress in it) and the length.  Two looks, Modern and Retro hi-fi
 * (a programme list on a faceplate: numbered keys whose lamp is lit for the
 * playing track, the header as a VFD with the time left on a tape counter),
 * shared with the equalizer, Stations and Playlists (ui_style.js) — the CSS is
 * css/queue-styles.css.
 *
 * The rows are a pooled virtual list (vlist.js): only the rows around the
 * viewport exist, so a queue of thousands opens and scrolls like a short one.
 * Every row event is delegated to the list.  Drag a row by its handle to move
 * it (a drop line shows where, the rows glide to their new places); × removes
 * it; a click plays it.  The waveform painter, the glide, the drop line and the
 * touch long-press are the playlist view's (playlist/listkit.js).
 *
 * Exports: Queue singleton with toggle, refresh, open, close.
 */
import { Player } from './player.js';
import { artPlaceholderEmoji, subsongWireToTune, subsongStartOf } from './utils.js';
import { offlineKind, offlineBadgeHTML } from './offline.js';
import { healthBadgeHTML, skipWord, skipReasonOf } from './health.js';
import { createVirtualList } from './vlist.js';
import { onUiStyleChange } from './ui_style.js';
import { vizGroupEnabled } from './viz/engine.js';
import { fmtShort, fmtFamily, fmtTotal, counterDigits } from './playlist/hero.js';
import { createWaveStore } from './playlist/waves.js';
import {
  createWavePainter, rowTops, flipFrom, createDropLine, gapAt, bindLongPressDrag, moveTarget, aheadOf,
  fmtDur, esc, NARROW_WAVES,
} from './playlist/listkit.js';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const panel    = document.getElementById('queue-panel');
const listEl   = document.getElementById('queue-list');      // the scroller
const rowsEl   = document.getElementById('queue-rows');      // the virtual list's host
const statusEl = document.getElementById('queue-status');    // "No tracks queued." / "Loading…"
const countEl  = document.getElementById('queue-count');
const dropZone = document.getElementById('queue-drop-zone');
const sumEl    = document.getElementById('queue-summary');
const upEl     = document.getElementById('queue-upnext');
const leftEl   = document.getElementById('queue-left');
const counterEl = document.getElementById('queue-counter');
const fromEl   = document.getElementById('queue-from');
const fromTxt  = document.getElementById('queue-from-text');
const annShuf  = document.getElementById('queue-ann-shuffle');
const annRep   = document.getElementById('queue-ann-repeat');

document.getElementById('btn-queue-close').addEventListener('click', () => close());

// ── A queue saved on another device ───────────────────────────────────────────
// The "Resume the queue from …?" toast times out; this row keeps the offer
// reachable until something plays or the queue is resumed (Player 'queueoffer').
const offerEl     = document.getElementById('queue-offer');
const offerText   = document.getElementById('queue-offer-text');
const offerBtn    = document.getElementById('btn-queue-offer');
function _paintOffer() {
  if (!offerEl) return;
  const who = Player.queueOffer || null;
  if (who) {
    offerText.textContent = `Queue saved on ${who}`;
    offerBtn.setAttribute('aria-label', `Resume the queue from ${who}`);
  }
  if (offerEl.hidden === !who) return;
  // Hiding the row under keyboard focus would drop focus to <body>.
  if (!who && offerEl.contains(document.activeElement)) {
    try { document.getElementById('btn-queue-close').focus({ preventScroll: true }); } catch (_) {}
  }
  offerEl.hidden = !who;
}
if (offerBtn) offerBtn.addEventListener('click', () => { Player.resumeQueueOffer?.(); });
Player.on('queueoffer', _paintOffer);
// A start that failed / one that plays after all: the playing marker follows.
Player.on('statechange', () => refresh());
_paintOffer();

// ── Clear queue with confirmation + undo ──────────────────────────────────────
//
// Clicking ``Clear`` while the queue has ≥5 tracks pops a styled confirm
// modal — small queues clear silently because the user can rebuild them
// trivially.  After a successful clear we show a 5-second undo toast that
// restores the previous queue and play position from a swap variable.
const CLEAR_CONFIRM_MIN  = 5;
const UNDO_WINDOW_MS     = 5000;
let _undoQueue   = null;          // Player.snapshotQueue() from the last clear
let _undoTimer   = null;
let _undoToast   = null;
let _restoring   = false;         // Undo is putting the old queue back right now

function _renderUndoToast(count) {
  // Lightweight inline toast — we avoid utils' Toast for this because it
  // doesn't expose an action button.  Positioning matches Toast (bottom-
  // right) so it co-locates with the rest of the notification stack.
  const el = document.createElement('div');
  el.className = 'queue-undo-toast';
  el.style.cssText = (
    'position:fixed;right:18px;bottom:18px;z-index:100001;'
    + 'background:var(--bg3,#222);color:var(--text1,#eee);'
    + 'border:1px solid var(--border-bright,#444);border-radius:8px;'
    + 'padding:10px 14px;display:flex;align-items:center;gap:12px;'
    + 'font-size:13px;box-shadow:var(--glass-shadow,0 4px 12px rgba(0,0,0,.4));'
  );
  el.innerHTML = `
    <span>Cleared ${count} track${count === 1 ? '' : 's'}.</span>
    <button type="button" class="queue-undo-btn"
            style="background:transparent;border:1px solid currentColor;color:inherit;
                   padding:3px 10px;border-radius:5px;cursor:pointer;font:inherit">
      Undo
    </button>`;
  el.querySelector('.queue-undo-btn').addEventListener('click', () => {
    _applyUndo();
  });
  document.body.appendChild(el);
  return el;
}

function _dismissUndoToast() {
  if (_undoTimer) { clearTimeout(_undoTimer); _undoTimer = null; }
  if (_undoToast) { _undoToast.remove(); _undoToast = null; }
}

function _applyUndo() {
  if (!_undoQueue) { _dismissUndoToast(); return; }
  _restoring = true;
  try { Player.restoreQueue(_undoQueue); } finally { _restoring = false; }
  _undoQueue = null;
  _dismissUndoToast();
  refresh();
}

function _showClearConfirm(count) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'pl-modal-backdrop';
    const dialog = document.createElement('div');
    dialog.className = 'pl-modal-dialog';
    dialog.innerHTML = `
      <div class="pl-modal-title">Clear queue</div>
      <div class="pl-modal-body" style="padding:6px 0 14px;color:var(--text2,#bbb);font-size:13px">
        Clear ${count} tracks from the queue?
      </div>
      <div class="pl-modal-actions">
        <button class="pl-modal-btn pl-modal-cancel">Cancel</button>
        <button class="pl-modal-btn pl-modal-ok">Clear</button>
      </div>
    `;
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    const btnOk  = dialog.querySelector('.pl-modal-ok');
    const btnCan = dialog.querySelector('.pl-modal-cancel');
    function finish(value) {
      backdrop.classList.add('pl-modal-out');
      setTimeout(() => backdrop.remove(), 150);
      resolve(value);
    }
    btnOk.focus();
    btnOk.addEventListener('click',  () => finish(true));
    btnCan.addEventListener('click', () => finish(false));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish(false); });
    dialog.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') finish(false);
      if (e.key === 'Enter')  finish(true);
    });
  });
}

document.getElementById('btn-queue-clear').addEventListener('click', async () => {
  const q   = Player.queue;
  if (!q || !q.length) return;

  if (q.length >= CLEAR_CONFIRM_MIN) {
    const ok = await _showClearConfirm(q.length);
    if (!ok) return;
  }

  // Snapshot for undo BEFORE we clear — the queue plus its source / shuffle
  // state, so Undo brings back exactly what was there.
  _undoQueue = Player.snapshotQueue();

  Player.setQueue([], 0);

  _dismissUndoToast();
  _undoToast = _renderUndoToast(q.length);
  _undoTimer = setTimeout(() => {
    _undoQueue = null;
    _dismissUndoToast();
  }, UNDO_WINDOW_MS);
});

// A subsong entry's wire index (0-based), or null for the file's default tune.
function _subKey(t) { const s = t && t.subsong; return (Number.isInteger(s) && s >= 0) ? s : null; }

// The row the queue marks as playing: the current one — none while its start
// has failed (nothing plays; its badge says why).
function _playingIdx() {
  return Player.startFailed ? -1 : Player.queueIdx;
}

// ── Rows: a pooled virtual list ───────────────────────────────────────────────
// Rows are keyed by queue index; a refresh (any queue change) repaints the
// rows on screen in place — the cover is re-asked only when a row shows
// another track than before, the title line only rebuilt when it changed.
let _dragFrom = -1;               // index being dragged (reorder), -1 = none
let _dragTrack = null;            // …and the entry it was when the drag began

function _makeRow() {
  const row = document.createElement('div');
  row.className = 'queue-row qx-row';
  // Drag begins from the handle only (it is draggable) — a touch long-press
  // makes the row itself draggable for a moment (below).
  row.draggable = false;
  row.innerHTML = `
      <span class="queue-drag-handle" draggable="true" title="Drag to reorder">&#10783;</span>
      <span class="queue-playing-icon"></span>
      <span class="qx-num" aria-hidden="true"></span>
      <div class="queue-row-art"><span class="qr-art-ph"></span><img class="qr-art-img" decoding="async" alt=""></div>
      <div class="queue-track-info">
        <div class="queue-track-titlerow"><span class="queue-track-title"></span></div>
        <span class="queue-track-artist"></span>
      </div>
      <span class="plx-fmt"></span>
      <span class="plx-wave-wrap qx-wave-wrap" aria-hidden="true"><canvas class="plx-wave"></canvas></span>
      <span class="queue-track-dur"></span>
      <button class="queue-remove-btn" title="Remove from queue">&times;</button>`;
  row.__wave = row.querySelector('.plx-wave');
  row.__fmt = row.querySelector('.plx-fmt');
  row.__num = row.querySelector('.qx-num');
  row.__icon = row.querySelector('.queue-playing-icon');
  row.__dur = row.querySelector('.queue-track-dur');
  // The cover fades in on a successful decode; on error the <img> stays (it is
  // opacity:0, the format placeholder shows through) — app.js re-busts every
  // /api/art/ <img> in the DOM when a remote cover arrives later (art_ready).
  const img = row.querySelector('.qr-art-img');
  img.onload  = () => img.classList.add('loaded');
  img.onerror = () => img.classList.remove('loaded');
  return row;
}

// Pool callback: paint row ``i`` (text, badges, state, waveform, cover).
function _fillRow(row, i) {
  const q = Player.queue;
  const track = q[i];
  if (!track) return;
  const idx = _playingIdx();
  const playing = i === idx;
  const offKind = offlineKind(track);
  // A row the queue will pass by when it moves on by itself (not the one
  // playing): dimmed, with the reason (health.js skipWord).
  // (An offline row has its own look; a broken / corrupt one is dimmed and
  // its badge says why; a short tune says "short · skipped".)
  const reason = skipReasonOf(track);
  const dim = !!reason && reason !== 'offline';
  const skip = skipWord(track);
  // (Only what changed is written: a scroll step refills a few recycled rows,
  // and every write costs a style recalc.)
  const cls = 'queue-row qx-row' + (playing ? ' playing' : '') + (offKind ? ' track-offline' : '')
    + (dim && !playing ? ' queue-skipped' : '') + (_dragFrom === i && track === _dragTrack ? ' dragging' : '');
  if (row.className !== cls) row.className = cls;
  const sk = dim ? reason : undefined;
  if (row.dataset.skip !== sk) { if (sk) row.dataset.skip = sk; else delete row.dataset.skip; }
  if (row.dataset.idx !== String(i)) row.dataset.idx = i;
  if (row.__playing !== playing) {
    row.__playing = playing;
    if (playing) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
    row.__icon.textContent = playing ? '▶' : '';
  }
  const num = String(i + 1).padStart(2, '0');
  if (row.__num.textContent !== num) row.__num.textContent = num;

  // Title line: title, "Tune n" for a subsong entry, playback / offline badges
  // and the skip reason (never on the playing row).
  const sub = _subKey(track);
  const tune = sub !== null
    ? (() => { const n = subsongWireToTune(sub, subsongStartOf(track), track.subsongs); return `<span class="qr-subsong" title="Tune ${n}">Tune ${n}</span>`; })()
    : '';
  const titleHtml = `<span class="queue-track-title" title="${esc(track.title)}">${esc(track.title || '—')}</span>${tune}`
    + `${healthBadgeHTML(track)}${offKind ? offlineBadgeHTML(offKind) : ''}`
    + `${skip && !playing ? `<span class="queue-skip-word">${esc(skip)}</span>` : ''}`;
  if (row.__titleHtml !== titleHtml) {
    row.querySelector('.queue-track-titlerow').innerHTML = titleHtml;
    row.__titleHtml = titleHtml;
  }
  const artist = track.artist || track.album_artist || '';
  if (row.__artist !== artist) { row.querySelector('.queue-track-artist').textContent = artist; row.__artist = artist; }
  // (A rendered format's length arrives after the row was built: the track
  // object is patched in place.)
  const dur = fmtDur(track.duration);
  if (row.__dur.textContent !== dur) row.__dur.textContent = dur;

  const fl = fmtShort(track), fam = fmtFamily(track);
  if (row.__fmtTxt !== fl || row.__fmtFam !== fam) {
    row.__fmt.textContent = fl;
    row.__fmt.className = `plx-fmt plx-fam-${fam}`;
    row.__fmt.title = track.format || fl;
    row.__fmtTxt = fl; row.__fmtFam = fam;
  }
  row.__trk = track;
  _paintWave(row, track, playing ? _curPct : -1);

  // Cover: always ask /api/art (it extracts on demand); ``fallback=none`` so
  // an art-less track gets a 204 and the format placeholder shows.
  if (row.__artId !== track.id) {
    row.__artId = track.id;
    row.querySelector('.qr-art-ph').textContent = artPlaceholderEmoji(track);
    const img = row.querySelector('.qr-art-img');
    img.classList.remove('loaded');
    if (track.id) {
      const ep = window.__sbArtEpoch ? `&_t=${window.__sbArtEpoch}` : '';
      img.src = `/api/art/${track.id}/sm.jpg?fallback=none${ep}`;
    } else {
      img.removeAttribute('src');
    }
  }
}

const _vl = createVirtualList({
  scroller: listEl, host: rowsEl, buffer: 8, rowHeight: 49, autoScroll: true,
  make: _makeRow,
  fill: _fillRow,
  keyOf: (i) => Player.queue[i],
  spacer: () => { const d = document.createElement('div'); d.className = 'vl-spacer'; return d; },
  setSpacer: (el, px) => { el.style.height = px + 'px'; },
  onResize: () => _waveRelayout(),
});

// ── Mini waveforms ────────────────────────────────────────────────────────────
// The stored waveforms of the rows on screen, asked in one batch; a row whose
// answer arrives is repainted in place.
const _waves = createWaveStore({
  onReady(ids) {
    if (!_vl.active) return;
    const got = new Set(ids);
    const pi = _playingIdx();
    for (const row of _vl.rows()) {
      const i = +row.dataset.idx;
      const t = Player.queue[i];
      if (t && got.has(t.id)) _paintWave(row, t, i === pi ? _curPct : -1);
    }
  },
});
let _curPct = 0;              // the playing entry's progress 0..1 (player timeupdate)
let _curLeft = null;          // seconds left in it (null: not known from the player yet)
// A subsong entry gets the placeholder: the stored waveform is the file's
// default tune's.
const _painter = createWavePainter({ store: _waves, colorsFrom: panel, prefix: 'qx', stored: (t) => _subKey(t) === null });
function _paintWave(row, track, progress = -1) { _painter.paint(row, track, progress); }

// Layout / look / theme changed: measure + read the colours again, repaint
// (the rows of a closed panel are repainted when it opens).
function _waveRelayout() {
  _painter.invalidate(_vl.active ? _vl.rows() : []);
  if (!_vl.active || panel.classList.contains('hidden')) return;
  const pi = _playingIdx();
  for (const row of _vl.rows()) {
    const i = +row.dataset.idx;
    const t = Player.queue[i];
    if (t) _paintWave(row, t, i === pi ? _curPct : -1);
  }
}

// Animated touches (the progress sheen) only while visualizations are on and
// motion is welcome; and only while it plays.  The progress itself always
// advances.
const _rmq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
function _syncMotion() {
  panel.classList.toggle('qx-anim', vizGroupEnabled('nowPlaying') && !(_rmq && _rmq.matches));
  panel.classList.toggle('qx-is-playing', !!Player.playing && !Player.startFailed && !Player.stationMode);
}
window.addEventListener('sb:viz-settings', _syncMotion);
if (_rmq && _rmq.addEventListener) _rmq.addEventListener('change', _syncMotion);

// ── Header: up next, time left, source, Shuffle / Repeat ──────────────────────
// What follows the current entry and will play (the rows the queue passes by
// are not counted) — kept until the queue or its position changes.
let _ahead = null;            // {n, sec, unknown, q, at, len}

function _aheadNow(q, at) {
  if (!_ahead || _ahead.q !== q || _ahead.at !== at || _ahead.len !== q.length) {
    _ahead = { ...aheadOf(q, at, (t) => !!skipReasonOf(t)), q, at, len: q.length };
  }
  return _ahead;
}

function _setText(el, txt) { if (el.textContent !== txt) el.textContent = txt; }
function _setAttr(el, k, v) { if (el.getAttribute(k) !== v) el.setAttribute(k, v); }

function _paintSummary() {
  const q = Player.queue || [];
  if (!q.length) { sumEl.hidden = true; return; }
  sumEl.hidden = false;
  const at = Player.queueIdx;
  const up = _aheadNow(q, at);
  const src = Player.queueSource;
  const more = !!(src && src.hasMore);
  const cur = at >= 0 ? q[at] : null;
  // Time left: what is left of the current entry + the lengths after it.
  let left = up.sec, unknown = up.unknown;
  if (cur && !Player.stationMode) {
    if (_curLeft != null) left += _curLeft;
    else if (cur.duration > 0) left += cur.duration;
    else unknown++;
  }
  const plus = (unknown || more) ? '+' : '';
  _setText(upEl, up.n
    ? `${up.n.toLocaleString()}${more ? '+' : ''} track${up.n === 1 && !more ? '' : 's'}`
    : (more ? 'more to come' : 'last track'));
  const leftTxt = left > 0 ? `${fmtTotal(left)}${plus} left` : '';
  _setText(leftEl, leftTxt);
  leftEl.hidden = !leftTxt;
  // Retro: the time left on a tape counter (aria-hidden — #queue-left says it),
  // with a "+" when it is a lower bound.
  const digits = left > 0 ? counterDigits(left) : '';
  if (counterEl.__d !== digits) {
    counterEl.__d = digits;
    counterEl.replaceChildren(...[...digits].map((ch) => {
      const s = document.createElement('span');
      s.className = ch === ':' ? 'plx-cnt-sep' : 'plx-cnt-dig';
      s.textContent = ch;
      return s;
    }));
  }
  counterEl.hidden = !digits;
  counterEl.classList.toggle('qx-more', !!plus);
  const label = (src && src.desc && src.desc.label) || '';
  _setText(fromTxt, label);
  fromEl.hidden = !label;
  const shuf = !!Player.shuffle;
  annShuf.classList.toggle('on', shuf);
  _setAttr(annShuf, 'aria-label', shuf ? 'Shuffle on' : 'Shuffle off');
  const rep = Player.repeatMode || 'none';
  annRep.classList.toggle('on', rep !== 'none');
  _setText(annRep, rep === 'one' ? 'Repeat 1' : 'Repeat');
  _setAttr(annRep, 'aria-label', { none: 'Repeat off', all: 'Repeat all', one: 'Repeat one' }[rep] || 'Repeat off');
}

// ── Render ────────────────────────────────────────────────────────────────────
function refresh() {
  const q   = Player.queue;
  const idx = _playingIdx();

  // Update count badge
  countEl.textContent = q.length ? `(${q.length.toLocaleString()})` : '';

  // A queue the listener started AFTER clearing makes "Undo" a trap: it would
  // replace what is playing now with the old queue.
  // (Rows Radio Mode appends on its own are not "a new queue the listener started".)
  if (_undoQueue && q.length && !_restoring && Player.autoAhead < q.length) { _undoQueue = null; _dismissUndoToast(); }

  // The panel is closed most of the time, and 'queuechange' fires on every
  // track change and every page the queue reads on: open() renders.
  if (panel.classList.contains('hidden')) return;

  _syncMotion();
  _ahead = null;                    // (lengths / marks changed in place since: count again)
  // The queue moved under a drag: follow the dragged entry (gone → no drop).
  if (_dragTrack && Player.queue[_dragFrom] !== _dragTrack) _dragFrom = Player.queue.indexOf(_dragTrack);
  _paintSummary();
  rowsEl.style.setProperty('--qx-numw', `${Math.max(2, String(q.length).length)}ch`);

  if (!q.length) {
    if (_vl.active) { _vl.reset(); rowsEl.replaceChildren(); }
    _showStatus('No tracks queued.');
    return;
  }
  _vl.setCount(q.length);
  _vl.render(true);

  // A view-backed queue refills itself a page at a time.  Right after shuffle is
  // switched on there is nothing behind the current track for a moment — say so,
  // or the toggle looks like it wiped the queue.
  const src = Player.queueSource;
  const loading = !!(src && src.hasMore && idx >= q.length - 1);   // hasMore: not ended AND not parked by Radio Mode
  _showStatus(loading ? (src.mode === 'shuffled' ? 'Shuffling the rest of the list…' : 'Loading the next tracks…') : '');
}

// The line under the rows (visual only: no live region, it is rewritten often).
function _showStatus(text) {
  if (statusEl.textContent !== text) statusEl.textContent = text;
  statusEl.hidden = !text;
}

// ── Delegated row events ──────────────────────────────────────────────────────
function _rowOf(e) {
  const row = e.target.closest && e.target.closest('.queue-row');
  return row && rowsEl.contains(row) && row.dataset.idx != null ? row : null;
}

// Rows glide to their new places after a move or a removal (FLIP).
const _rowTops = () => rowTops(_vl.rows());
const _flipFrom = (before) => flipFrom(_vl.rows(), before, rowsEl, !!(_rmq && _rmq.matches));

rowsEl.addEventListener('click', (e) => {
  const row = _rowOf(e);
  if (!row) return;
  const i = +row.dataset.idx;
  if (!Player.queue[i]) return;
  if (e.target.closest('.queue-remove-btn')) {
    e.stopPropagation();
    const tops = _rowTops();
    Player.removeFromQueue(i);       // queuechange → refresh() repaints the rows
    _flipFrom(tops);
    return;
  }
  Player.setQueue(Player.queue, i);
});

// ── Drag to reorder ───────────────────────────────────────────────────────────
// The handle starts the drag; the list may scroll (and recycle the dragged row)
// meanwhile, so the drop goes by the entry noted at dragstart.
rowsEl.addEventListener('dragstart', (e) => {
  const row = _rowOf(e);
  if (!row) return;
  _dragFrom = +row.dataset.idx;
  _dragTrack = Player.queue[_dragFrom] || null;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('application/x-soniqboom-queue-idx', String(_dragFrom));
  row.classList.add('dragging');
  // (The list may recycle the dragged row while it scrolls: its 'dragend' then
  // no longer bubbles to the list, so listen on the drag source itself.)
  e.target.addEventListener('dragend', _endDrag, { once: true });
});

// The drop line: where the dragged row will land (a gap between two rows).
const _dropLine = createDropLine(panel, 'qx-dropline');
let _dropGap = -1;            // insert before this index (0..count); -1 = none

function _hideDropLine() { _dropLine.hide(); _dropGap = -1; }

// The gap under the pointer (null = not over the rows).
function _gapAt(e) {
  const n = Player.queue.length;
  return gapAt(e, _rowOf(e), _vl.rowFor(n - 1), n);
}

function _showDropLine(g) {
  if (!g || moveTarget(_dragFrom, g.gap) < 0) { _hideDropLine(); return; }
  _dropGap = g.gap;
  _dropLine.show(g.y, listEl);
}

function _endDrag() {
  _dragFrom = -1;
  _dragTrack = null;
  _hideDropLine();
  rowsEl.querySelectorAll('.queue-row.dragging').forEach(r => r.classList.remove('dragging'));
  // Drop the transient draggable flag (set during a touch long-press) so the
  // next tap on the row isn't treated as a drag-handle gesture.
  rowsEl.querySelectorAll('.queue-row[draggable="true"]').forEach(r => { r.draggable = false; });
}
rowsEl.addEventListener('dragend', _endDrag);

// ── Touch: a long press on the handle starts a drag (playlist/listkit.js) ─────
bindLongPressDrag(rowsEl, _rowOf);

// ── Drops on the list: a queue row → move it; library tracks → add them ──────
const _isQueueDrag = (dt) => dt.types.includes('application/x-soniqboom-queue-idx');
const _isTrackDrag = (dt) => dt.types.includes('application/x-soniqboom-track') && !_isQueueDrag(dt);

function _addDropped(e) {
  try {
    const data = JSON.parse(e.dataTransfer.getData('application/x-soniqboom-track'));
    const tracks = Array.isArray(data) ? data : [data];
    Player.addAllToQueue(tracks.filter(t => t?.id));
    if (tracks.length) refresh();
  } catch (_) {}
}

listEl.addEventListener('dragover', (e) => {
  if (_isQueueDrag(e.dataTransfer)) {
    if (_dragFrom < 0) return;                // (a row of another tab's queue)
    const g = _gapAt(e);
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    _showDropLine(g);
    return;
  }
  if (_isTrackDrag(e.dataTransfer)) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dropZone.classList.add('drag-active');
  }
});

listEl.addEventListener('dragleave', (e) => {
  // (Chromium often leaves relatedTarget null between two rows: go by where the
  // pointer is, so the line doesn't flicker from row to row.)
  const r = listEl.getBoundingClientRect();
  const out = e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom;
  if (out || (e.relatedTarget && !listEl.contains(e.relatedTarget) && !dropZone.contains(e.relatedTarget))) {
    dropZone.classList.remove('drag-active');
    _hideDropLine();
  }
});

listEl.addEventListener('drop', (e) => {
  if (_isQueueDrag(e.dataTransfer)) {
    e.preventDefault();
    const g = _gapAt(e);
    const gap = g ? g.gap : _dropGap;
    // (refresh() keeps _dragFrom on the dragged entry when the queue changed
    // under the drag — a re-deal, an extension, a removal; -1 once it is gone.)
    const from = _dragTrack && Player.queue[_dragFrom] === _dragTrack ? _dragFrom : -1;
    const to = moveTarget(from, gap);
    _endDrag();
    if (to < 0) return;
    const tops = _rowTops();
    Player.moveInQueue(from, to);   // queuechange → refresh() repaints the rows
    _flipFrom(tops);
    return;
  }
  if (!_isTrackDrag(e.dataTransfer)) return;
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  _addDropped(e);
});

// ── Drop zone — receives library track drops ──────────────────────────────────
dropZone.addEventListener('dragover', (e) => {
  if (_isTrackDrag(e.dataTransfer)) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dropZone.classList.add('drag-active');
  }
});

dropZone.addEventListener('dragleave', (e) => {
  if (!dropZone.contains(e.relatedTarget)) {
    dropZone.classList.remove('drag-active');
  }
});

dropZone.addEventListener('drop', (e) => {
  if (!_isTrackDrag(e.dataTransfer)) return;
  e.preventDefault();
  dropZone.classList.remove('drag-active');
  _addDropped(e);
});

// ── Look, theme, width ────────────────────────────────────────────────────────
// The look (Modern / Retro hi-fi) is shared with the equalizer, Stations and
// Playlists; the CSS keys off <html data-ui-style>.  Rows change height with it.
onUiStyleChange(() => {
  _waveRelayout();
  if (_vl.active && !panel.classList.contains('hidden')) _vl.remeasure();
});
// The narrow-window rule resizes the waveform while the list keeps its width.
try { window.matchMedia(NARROW_WAVES).addEventListener('change', () => _waveRelayout()); } catch (_) { /* old engine */ }
// The theme (light / dark) changes the waveform colours.
new MutationObserver(() => _waveRelayout())
  .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
// Repeat has no player event: follow the player bar's button.
const _btnRepeat = document.getElementById('btn-repeat');
if (_btnRepeat) {
  new MutationObserver(() => { if (!panel.classList.contains('hidden')) _paintSummary(); })
    .observe(_btnRepeat, { attributes: true, attributeFilter: ['title', 'class'] });
}
Player.on('shufflechange', () => { if (!panel.classList.contains('hidden')) _paintSummary(); });

// ── Playback: the playing row's progress, the time left ───────────────────────
let _waveRecheck = [];
Player.on('trackchange', () => {
  _curPct = 0;
  _curLeft = null;
  // A track's first play stores its waveform: ask again for it a little later.
  _waveRecheck.forEach(clearTimeout);
  const id = Player.currentTrack && Player.currentTrack.id;
  if (!id) return;
  // (The same steps as the playlist view's retries, ~1 min in all.)
  _waveRecheck = [5000, 15000, 30000, 60000].map(ms => setTimeout(() => {
    if (_waves.state(id) !== null) return;
    _waves.forget(id);
    if (panel.classList.contains('hidden')) return;
    const row = _vl.rowFor(_playingIdx());
    if (row && row.__trk && row.__trk.id === id) _paintWave(row, row.__trk, _curPct);
  }, ms));
});
// The playing row's progress inside its mini waveform (≈4 Hz, no timer of its own).
Player.on('timeupdate', (d) => {
  _curPct = d && d.duration ? Math.max(0, Math.min(1, d.current / d.duration)) : 0;
  _curLeft = d && Number.isFinite(d.duration) && d.duration > 0 ? Math.max(0, d.duration - (d.current || 0)) : null;
  if (panel.classList.contains('hidden')) return;
  const row = _vl.active ? _vl.rowFor(_playingIdx()) : null;
  if (row && row.__trk) _paintWave(row, row.__trk, _curPct);
  _paintSummary();
});

// ── Panel visibility ──────────────────────────────────────────────────────────
function open() {
  document.dispatchEvent(new CustomEvent('panelopen', { detail: { panel: 'queue' } }));
  const wasHidden = panel.classList.contains('hidden');
  panel.classList.remove('hidden');
  if (wasHidden) _waves.forgetMissing();   // (tracks played since may have a waveform now)
  refresh();
  // The playing row in view (a long queue opens where it plays).
  const at = _playingIdx();
  if (wasHidden && at >= 0 && _vl.active) {
    const v = _vl.firstVisible();
    const rows = Math.floor(listEl.clientHeight / Math.max(1, _vl.rowHeight));
    if (at < v.idx || at >= v.idx + rows) _vl.scrollToIndex(at, 'center');
  }
}

function close() {
  panel.classList.add('hidden');
  _endDrag();
}

function toggle() {
  if (panel.classList.contains('hidden')) {
    open();
  } else {
    close();
  }
}

// Close when another panel opens
document.addEventListener('panelopen', (e) => {
  if (e.detail?.panel !== 'queue') close();
});

_syncMotion();

export const Queue = { toggle, refresh, open, close };
