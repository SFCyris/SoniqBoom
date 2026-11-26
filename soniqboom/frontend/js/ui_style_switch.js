// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * ui_style_switch.js — the words of the top bar's Retro hi-fi / Modern switch
 * (app.js #btn-ui-style; pure — tests/js/desktop_switches.test.mjs).  The icon
 * shows the style in use; its accessible name and tooltip name the style in
 * use and the one a press switches to.
 */

const NAMES = { retro: 'Retro hi-fi', modern: 'Modern' };

/** → { label, title, item } for the style in use ('retro' | 'modern'). */
export function styleSwitchText(style) {
  const cur = style === 'modern' ? 'modern' : 'retro';
  const next = cur === 'retro' ? 'modern' : 'retro';
  return {
    label: `Switch to ${NAMES[next]} style (now ${NAMES[cur]})`,
    title: `Style: ${NAMES[cur]} — click for ${NAMES[next]}`,
    item: `Switch to ${NAMES[next]} style`,
    next,
  };
}
