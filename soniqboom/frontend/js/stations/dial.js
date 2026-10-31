// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * stations/dial.js — the tuner dial: the shown list's stations along a
 * frequency-style scale (87.5 … 108 "MHz", evenly spaced, first to last), a
 * needle on the station that plays.
 *
 * Tuning: click a name (or anywhere on the scale: the nearest station); drag
 * the needle with a mouse or pen (it tunes where you let go); a sideways wheel
 * / trackpad swipe over the dial (an up / down wheel only while the dial has
 * focus — otherwise it scrolls the page) and the arrow keys move the needle
 * one station per step and tune once it rests (SETTLE_MS) — Enter / Space
 * tune at once.  A press on the glass's own scrollbar scrolls, never tunes.  ‹ / › beside the
 * glass tune the neighbouring station.  The glass is a role="slider"
 * (value = the station's position in the list, value text = its name); the
 * names are pointer targets only (aria-hidden).  The needle sweeps to a new
 * station (CSS transition on transform; none with reduced motion).  A long
 * list scrolls sideways; the needle is kept in view.
 *
 *   const dial = createDial(host, { onTune(station, index, list) {} });
 *   dial.setStations(list, label)   dial.setCurrent(sid | null)
 *   dial.layout()                   dial.selected  dial.current
 */
import { reducedMotion } from './live.js';

const SETTLE_MS = 450;
const PAD = 56;               // px kept free at each end of the scale
const WHEEL_STEP_PX = 50;
const F_LO = 87.5, F_HI = 108;

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function createDial(host, { onTune }) {
  host.innerHTML = `
    <button type="button" class="st-dial-step" data-dir="-1" aria-label="Tune the previous station on the dial">‹</button>
    <div class="st-dial-glass" role="slider" tabindex="0" aria-orientation="horizontal"
         aria-label="Tuner" aria-valuemin="1" aria-valuemax="1" aria-valuenow="1">
      <div class="st-dial-track">
        <div class="st-dial-scale" aria-hidden="true"></div>
        <div class="st-dial-needle" aria-hidden="true"></div>
        <div class="st-dial-names" aria-hidden="true"></div>
      </div>
      <div class="st-dial-empty" aria-hidden="true">Open a station list to tune across it</div>
    </div>
    <button type="button" class="st-dial-step" data-dir="1" aria-label="Tune the next station on the dial">›</button>`;
  const glass = host.querySelector('.st-dial-glass');
  const track = host.querySelector('.st-dial-track');
  const scale = host.querySelector('.st-dial-scale');
  const names = host.querySelector('.st-dial-names');
  const needle = host.querySelector('.st-dial-needle');
  const steps = Array.from(host.querySelectorAll('.st-dial-step'));

  let list = [], label = '';
  let cur = -1;               // the playing station's index in the list (-1: not in it)
  let sel = -1;               // where the needle points
  let xs = [];                // each station's x on the track
  let trackW = 0;
  let settleT = 0;
  let drag = null;            // { id, moved, x0 }
  let wheelAcc = 0, wheelAt = 0;

  const freqOf = (i) => (list.length <= 1 ? 98.0 : F_LO + ((F_HI - F_LO) * i) / (list.length - 1));
  const fmtF = (f) => f.toFixed(1);

  function layout() {
    const n = list.length;
    host.classList.toggle('st-dial-none', !n);
    const W = glass.clientWidth || 0;
    const gap = parseFloat(getComputedStyle(host).getPropertyValue('--st-dial-gap')) || 120;
    trackW = Math.max(W, n > 1 ? 2 * PAD + (n - 1) * gap : W);
    track.style.width = trackW + 'px';
    const span = Math.max(1, trackW - 2 * PAD);
    const xf = (f) => PAD + ((f - F_LO) / (F_HI - F_LO)) * span;
    xs = list.map((_, i) => (n <= 1 ? trackW / 2 : xf(freqOf(i))));
    // The printed scale: a mark every 0.5 MHz, numbers every 2 MHz.
    scale.style.setProperty('--st-tick', (span / ((F_HI - F_LO) * 2)) + 'px');
    scale.style.left = PAD + 'px';
    scale.style.width = (span + 1) + 'px';
    let nums = '';
    // (the scale spans PAD … trackW − PAD, so its children sit at x − PAD)
    for (let f = 88; f <= 108; f += 2) nums += `<span class="st-dial-mhz" style="left:${(xf(f) - PAD).toFixed(1)}px">${f}</span>`;
    scale.innerHTML = nums + '<span class="st-dial-unit">MHz</span>';
    names.innerHTML = list.map((s, i) =>
      `<span class="st-dial-name${i % 2 ? ' alt' : ''}" data-i="${i}" style="left:${xs[i].toFixed(1)}px">${esc(s.name)}</span>`).join('');
    _paint(false);
  }

  function _paint(animate = true) {
    const n = list.length;
    if (cur >= n) cur = -1;
    if (sel >= n) sel = -1;
    const at = sel >= 0 ? sel : cur;
    for (const el of names.children) {
      const i = +el.dataset.i;
      el.classList.toggle('on', i === cur);
      el.classList.toggle('sel', i === at);
    }
    host.classList.toggle('st-dial-tuned', cur >= 0);
    if (!animate || reducedMotion()) needle.classList.add('st-instant');
    const x = at >= 0 && xs[at] != null ? xs[at] : PAD;
    needle.style.transform = `translateX(${x.toFixed(1)}px)`;
    if (!animate || reducedMotion()) { void needle.offsetWidth; needle.classList.remove('st-instant'); }
    needle.classList.toggle('st-parked', at < 0);
    // The slider contract
    glass.setAttribute('aria-label', label ? `Tuner — ${label}` : 'Tuner');
    if (!n) {
      glass.setAttribute('aria-disabled', 'true');
      glass.setAttribute('aria-valuemax', '1');
      glass.setAttribute('aria-valuenow', '1');
      glass.setAttribute('aria-valuetext', 'No stations on the dial');
    } else {
      glass.removeAttribute('aria-disabled');
      glass.setAttribute('aria-valuemax', String(n));
      glass.setAttribute('aria-valuenow', String((at >= 0 ? at : 0) + 1));
      glass.setAttribute('aria-valuetext', at >= 0
        ? `${list[at].name}, ${fmtF(freqOf(at))} MHz, ${at + 1} of ${n}${at === cur ? ', playing' : ''}`
        : `Not tuned — ${n} station${n === 1 ? '' : 's'}`);
    }
    steps[0].disabled = !n || (at >= 0 ? at <= 0 : false);
    steps[1].disabled = !n || (at >= 0 ? at >= n - 1 : false);
    if (at >= 0) _keepInView(xs[at], animate);
  }

  function _keepInView(x, animate) {
    if (x == null || drag) return;
    const W = glass.clientWidth;
    if (trackW <= W + 1) return;
    const left = glass.scrollLeft;
    if (x > left + 40 && x < left + W - 40) return;
    const to = Math.max(0, Math.min(trackW - W, x - W / 2));
    try { glass.scrollTo({ left: to, behavior: animate && !reducedMotion() ? 'smooth' : 'auto' }); }
    catch (_) { glass.scrollLeft = to; }
  }

  function _nearest(x) {
    let best = -1, bd = Infinity;
    for (let i = 0; i < xs.length; i++) { const d = Math.abs(xs[i] - x); if (d < bd) { bd = d; best = i; } }
    return best;
  }

  function _tune(i) {
    clearTimeout(settleT); settleT = 0;
    if (i < 0 || i >= list.length) return;
    sel = i;
    _paint();
    if (i !== cur) onTune(list[i], i, list);
  }

  /** Move the needle by ``d`` stations; tune once it rests. */
  function _nudge(d, abs = null) {
    if (!list.length) return;
    const from = sel >= 0 ? sel : (cur >= 0 ? cur : (d > 0 ? -1 : list.length));
    const to = Math.max(0, Math.min(list.length - 1, abs != null ? abs : from + d));
    sel = to;
    _paint();
    clearTimeout(settleT);
    settleT = setTimeout(() => { settleT = 0; _tune(sel); }, SETTLE_MS);
  }

  // ── Input ───────────────────────────────────────────────────────────────
  const _trackX = (clientX) => clientX - track.getBoundingClientRect().left;

  glass.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || !list.length) return;
    switch (e.key) {
      case 'ArrowRight': case 'ArrowUp':   _nudge(1); break;
      case 'ArrowLeft':  case 'ArrowDown': _nudge(-1); break;
      case 'PageUp':   _nudge(5); break;
      case 'PageDown': _nudge(-5); break;
      case 'Home': _nudge(0, 0); break;
      case 'End':  _nudge(0, list.length - 1); break;
      case 'Enter': case ' ': _tune(sel >= 0 ? sel : Math.max(0, cur)); break;
      default: return;
    }
    e.preventDefault();
    e.stopPropagation();     // the arrows / Space are app-wide shortcuts too
  });

  // A press below the glass's content box is on its horizontal scrollbar.
  const _onScrollbar = (e) => e.clientY - glass.getBoundingClientRect().top - glass.clientTop >= glass.clientHeight;

  glass.addEventListener('pointerdown', (e) => {
    if (!list.length || e.pointerType === 'touch' || (e.button !== undefined && e.button !== 0)) return;
    if (_onScrollbar(e)) return;
    // (Pointer capture retargets the pointerup to the glass: note the name now.)
    const nm = e.target.closest && e.target.closest('.st-dial-name');
    drag = { id: e.pointerId, moved: false, x0: e.clientX, nameI: nm ? +nm.dataset.i : -1 };
    try { glass.setPointerCapture(e.pointerId); } catch (_) {}
    try { glass.focus({ preventScroll: true }); } catch (_) {}
  });
  glass.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.abs(e.clientX - drag.x0) < 4) return;
    if (!drag.moved) { drag.moved = true; needle.classList.add('st-dragging'); clearTimeout(settleT); }
    // Near an edge of a long dial: scroll along.
    const r = glass.getBoundingClientRect();
    if (e.clientX < r.left + 28) glass.scrollLeft -= 14;
    else if (e.clientX > r.right - 28) glass.scrollLeft += 14;
    const x = Math.max(PAD / 2, Math.min(trackW - PAD / 2, _trackX(e.clientX)));
    needle.style.transform = `translateX(${x.toFixed(1)}px)`;
    const i = _nearest(x);
    if (i !== sel) {
      sel = i;
      for (const el of names.children) el.classList.toggle('sel', +el.dataset.i === i);
    }
  });
  function _endDrag(e) {
    if (!drag || (e && e.pointerId !== drag.id)) return;
    const d = drag;
    drag = null;
    needle.classList.remove('st-dragging');
    if (e && e.type === 'pointercancel') { sel = cur; _paint(); return; }
    // A plain click: on a name → that station (a long name reaches past its
    // neighbours' x), elsewhere → the nearest.
    const i = !d.moved && d.nameI >= 0 ? d.nameI : _nearest(_trackX(e.clientX));
    _tune(i);
  }
  glass.addEventListener('pointerup', _endDrag);
  glass.addEventListener('pointercancel', _endDrag);
  // Touch: the strip scrolls natively; a tap tunes.
  glass.addEventListener('click', (e) => {
    if (e.pointerType !== 'touch' && !(e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents)) return;
    if (!list.length || _onScrollbar(e)) return;
    const nameEl = e.target.closest && e.target.closest('.st-dial-name');
    _tune(nameEl ? +nameEl.dataset.i : _nearest(_trackX(e.clientX)));
  });
  glass.addEventListener('wheel', (e) => {
    if (!list.length) return;
    const sideways = Math.abs(e.deltaX) > Math.abs(e.deltaY);
    // An up / down wheel scrolls the page past the dial unless the dial has focus.
    if (!sideways && document.activeElement !== glass) return;
    e.preventDefault();
    const now = performance.now();
    if (now - wheelAt > 300) wheelAcc = 0;          // a new gesture starts from zero
    wheelAt = now;
    const raw = sideways ? e.deltaX : e.deltaY;
    const px = e.deltaMode === 1 ? raw * 16 : e.deltaMode === 2 ? raw * 400 : raw;
    wheelAcc += px;
    if (Math.abs(wheelAcc) < WHEEL_STEP_PX && e.deltaMode === 0) return;
    const dir = wheelAcc > 0 ? 1 : -1;
    wheelAcc = 0;
    _nudge(dir);
  }, { passive: false });
  steps.forEach((b) => b.addEventListener('click', () => {
    if (!list.length) return;
    const at = sel >= 0 ? sel : cur;
    const dir = +b.dataset.dir;
    _tune(at < 0 ? (dir > 0 ? 0 : list.length - 1) : at + dir);
  }));

  if (window.ResizeObserver) new ResizeObserver(() => layout()).observe(glass);

  return {
    setStations(next, lbl = '') {
      const same = next === list && lbl === label;
      list = Array.isArray(next) ? next : [];
      label = lbl;
      if (!same) { clearTimeout(settleT); settleT = 0; cur = -1; sel = -1; }   // (setCurrent places the needle)
      layout();
    },
    /** The playing station (its sid), or null.  The needle sweeps to it. */
    setCurrent(sid) {
      const i = sid ? list.findIndex((s) => s.sid === sid) : -1;
      if (i === cur && (settleT || drag)) return;   // the listener is mid-tune
      cur = i;
      sel = i;
      _paint(true);
    },
    layout,
    get selected() { return sel; },
    get current() { return cur; },
    get stations() { return list; },
    get label() { return label; },
    get needleX() {
      const m = /translateX\(([-\d.]+)px\)/.exec(needle.style.transform || '');
      return m ? +m[1] : null;
    },
    xOf(i) { return xs[i]; },
  };
}
