// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * seek_gesture.js — a finger (or a mouse) on the Now Playing seek slot.
 *
 * The slot is one rectangle — the ribbed groove and the waveform drawn in it
 * (np_visuals.js) — and anywhere in it seeks by x alone: a tap seeks to the
 * spot tapped, a drag follows the finger and seeks where it is lifted.  A
 * gesture the browser takes away (a vertical scroll: ``pointercancel``), a
 * lost capture without a lift, or the window losing focus ends it WITHOUT a
 * seek, and the clock goes back to following playback.  A gesture whose end
 * was never told (its pointer capture gone) is ended by the next touch, so
 * nothing stays locked (a tap on the old native slider set
 * "scrubbing" on pointerdown and only its ``change`` cleared it, which a tap
 * without movement never fired: the clock and the slider froze).
 *
 * The native range input under the slot stays for the keyboard and screen
 * readers (its own input / change); pointers go to the ``surface`` laid over
 * it, so the engine's own range handling (iOS does not jump to a tapped spot)
 * never mixes in.  Runs outside a browser too (tests/js/m_seek_gesture.test.mjs).
 */

/** Where ``clientX`` falls in a rectangle ``{left, width}``, 0…100. */
export function pctAtX(clientX, rect) {
  const w = Number(rect && rect.width) || 0;
  if (!(w > 0)) return 0;
  const p = ((Number(clientX) - (Number(rect.left) || 0)) / w) * 100;
  return Math.min(100, Math.max(0, Number.isFinite(p) ? p : 0));
}

/**
 * Wire the gesture on ``surface``.
 *   enabled()     may a gesture start now (a station has no position)
 *   preview(pct)  the finger is at ``pct``: show it, nothing seeks
 *   commit(pct)   the finger was lifted at ``pct``: seek there
 *   cancel()      the gesture ended without a seek
 *   win           the window (its 'blur'); defaults to globalThis
 * Returns { active, cancel(), detach() }.
 */
export function attachSeekGesture(surface, { enabled = () => true, preview, commit, cancel, win } = {}) {
  const w = win || (typeof window !== 'undefined' ? window : null);
  let pointer = null;        // the pointerId of the gesture under way
  let pct = 0;

  const at = (e) => pctAtX(e.clientX, surface.getBoundingClientRect());
  const call = (fn, ...a) => { if (fn) { try { fn(...a); } catch (err) { console.error(err); } } };

  function end(ok) {
    if (pointer === null) return;
    const id = pointer;
    pointer = null;
    try { if (surface.hasPointerCapture && surface.hasPointerCapture(id)) surface.releasePointerCapture(id); }
    catch (_) { /* ignore */ }
    if (ok) call(commit, pct); else call(cancel);
  }

  function onDown(e) {
    // A second finger: the first one leads — unless that one's end was never
    // told (its capture is gone): then it is over, and this one starts.
    if (pointer !== null) {
      let held = true;
      try { held = !surface.hasPointerCapture || surface.hasPointerCapture(pointer); } catch (_) { held = false; }
      if (held) return;
      end(false);
    }
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (!enabled()) return;
    pointer = e.pointerId;
    try { surface.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    if (e.cancelable && e.pointerType === 'mouse') e.preventDefault();   // (no text selection)
    pct = at(e);
    call(preview, pct);
  }
  function onMove(e) {
    if (e.pointerId !== pointer) return;
    pct = at(e);
    call(preview, pct);
  }
  function onUp(e) {
    if (e.pointerId !== pointer) return;
    pct = at(e);
    end(true);
  }
  function onCancel(e) {
    if (e.pointerId !== pointer) return;
    end(false);
  }
  const onBlur = () => end(false);

  surface.addEventListener('pointerdown', onDown);
  surface.addEventListener('pointermove', onMove);
  surface.addEventListener('pointerup', onUp);
  surface.addEventListener('pointercancel', onCancel);
  // (after a lift the capture goes too — the gesture already ended then)
  surface.addEventListener('lostpointercapture', onCancel);
  if (w) w.addEventListener('blur', onBlur);

  return {
    get active() { return pointer !== null; },
    cancel: () => end(false),
    detach() {
      end(false);
      surface.removeEventListener('pointerdown', onDown);
      surface.removeEventListener('pointermove', onMove);
      surface.removeEventListener('pointerup', onUp);
      surface.removeEventListener('pointercancel', onCancel);
      surface.removeEventListener('lostpointercapture', onCancel);
      if (w) w.removeEventListener('blur', onBlur);
    },
  };
}
