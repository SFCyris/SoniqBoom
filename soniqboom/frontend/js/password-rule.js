// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * password-rule.js — the password rule new passwords are checked against
 * before they're sent (the server checks the same rule: core/users.py
 * validate_password).  "Require strong passwords" (Settings → Users) adds
 * the complexity rule; /api/auth/status reports whether it's on.
 */

export const STRONG_MIN_LEN = 10;
export const STRONG_RULE =
  `At least ${STRONG_MIN_LEN} characters, with an uppercase letter, a lowercase letter, a number and a symbol.`;

let _strong = null;   // last known value of the setting

/** Whether "Require strong passwords" is on (asks the server once per call
 *  site that passes ``refresh``; otherwise reuses the last answer). */
export async function strongPasswordsRequired({ refresh = false } = {}) {
  if (_strong !== null && !refresh) return _strong;
  try {
    const r = await fetch('/api/auth/status', { credentials: 'same-origin' });
    const j = await r.json();
    _strong = !!j.strong_passwords;
  } catch {
    _strong = _strong ?? false;   // the server still enforces the rule
  }
  return _strong;
}

/** Record the setting after an admin changes it. */
export function setStrongPasswordsKnown(on) { _strong = !!on; }

/** Short hint for a password field. */
export function passwordHint(strong) {
  return strong ? `${STRONG_MIN_LEN}+ chars: Aa, 0–9, symbol` : '';
}

/** The reason ``pw`` is not acceptable, or null. */
export function passwordProblem(pw, strong) {
  if (!pw) return "Password can't be empty.";
  if (!strong) return null;
  const ok = pw.length >= STRONG_MIN_LEN
    && /\p{Ll}/u.test(pw) && /\p{Lu}/u.test(pw) && /\p{N}/u.test(pw)
    && /[^\p{L}\p{N}\s]/u.test(pw);
  return ok ? null : `Password must be at least ${STRONG_MIN_LEN} characters and include an uppercase letter, a lowercase letter, a number and a symbol.`;
}
