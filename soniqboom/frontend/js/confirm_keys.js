// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * confirm_keys.js — the keys of a yes / no confirm (pure: no DOM; tests in
 * tests/js/confirm_keys.test.mjs).  Shared by the playlist module's inline
 * confirm (playlist.js, the one in use until Settings has been opened once)
 * and Settings' ``styledConfirm`` (admin.js).
 *
 * The rule: a key answers only for the button that has the focus.  Enter or
 * Space on the focused OK confirms, on the focused Cancel cancels, and with
 * the focus on neither (the dialog itself, a stray click on its text) they
 * answer nothing — a confirm never says yes to "an Enter somewhere".  Escape
 * always cancels.  A destructive confirm (delete, remove) opens with the focus
 * on Cancel, so a stray Enter right after it opened changes nothing.
 */

/**
 * What a key does in a confirm: 'ok', 'cancel', 'none' (one of its keys, but
 * no answer — the focus is on neither button) or null (not a confirm key:
 * Tab and the rest go on as usual).
 */
export function confirmKeyAction(key, focused, okBtn, cancelBtn) {
  if (key === 'Escape' || key === 'Esc') return 'cancel';
  if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
    if (focused && focused === okBtn) return 'ok';
    if (focused && focused === cancelBtn) return 'cancel';
    return 'none';
  }
  return null;
}

/** The button a confirm opens with the focus on: Cancel when it is destructive. */
export function confirmInitialFocus(destructive, okBtn, cancelBtn) {
  return destructive ? cancelBtn : okBtn;
}
