// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * welcome_lead.js — what the welcome guide leads with (app.js _openWelcome;
 * pure — tests/js/desktop_switches.test.mjs).  An empty library (or one not
 * counted yet) is led by "Add your music"; a library that has music already
 * is led by "Start listening", with adding more music the second key.
 */

/** ``tracks``: the library's track count (null / undefined: not known). */
export function welcomeLead(tracks) {
  const n = Number(tracks);
  if (Number.isFinite(n) && n > 0) {
    return { lead: 'dismiss', add: 'Add more music', dismiss: 'Start listening' };
  }
  return { lead: 'add', add: 'Add your music', dismiss: 'Explore on my own' };
}
