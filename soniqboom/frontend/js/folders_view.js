// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * folders_view.js — the names and the way up of the desktop Folders view
 * (library.js showFolders; pure — tests/js/folders_view.test.mjs).  The
 * folders' own names, a music folder's name and kind come from the phone's
 * helpers (mobile/folders.js: folderLabel, rootLabel, rootKind, rootOf).
 *
 * A folder's path is its music folder's path, "/", its subfolders' names (the
 * server's rule for local folders, shares and an archive's inner folder alike:
 * ``…/Mixed/pack.zip::inner`` is a child of ``…/Mixed``).
 */
import { folderLabel } from './mobile/folders.js';

/** The folder above ``path`` — null when ``path`` is its music folder (or not in it). */
export function folderParent(path, rootPath) {
  const p = String(path || '').replace(/\/+$/, '');
  const root = String(rootPath || '').replace(/\/+$/, '');
  if (!p || !root || p === root || !p.startsWith(`${root}/`)) return null;
  const at = p.lastIndexOf('/');
  const up = p.slice(0, at);
  return up.length >= root.length ? up : null;
}

/** The folders from the music folder down to ``path``: [{ path, name }]. */
export function folderTrail(path, rootPath, rootName) {
  const root = String(rootPath || '').replace(/\/+$/, '');
  const p = String(path || '').replace(/\/+$/, '');
  const out = [{ path: root, name: rootName || folderLabel(root.split('/').filter(Boolean).pop() || root) }];
  if (!p.startsWith(`${root}/`)) return out;
  let at = root;
  for (const n of p.slice(root.length + 1).split('/').filter(Boolean)) {
    at = `${at}/${n}`;
    out.push({ path: at, name: folderLabel(n) });
  }
  return out;
}

/** The crumb: "Folders › Music › … › B › C" (at most ``keep`` names below Folders). */
export function foldersCrumb(trail, keep = 3) {
  const names = (trail || []).map((t) => t && t.name).filter(Boolean);
  const shown = names.length > keep ? ['…', ...names.slice(-keep)] : names;
  return ['Folders', ...shown].join(' › ');
}

/** What a music folder that can't be reached says (as the phone and the tree). */
export function rootBlockedText(r) {
  const name = (r && r.name) || 'This folder';
  if (r && r.catalogue) {
    return `${name} can’t be reached right now. Tunes played before still play; the others play once it is reachable again.`;
  }
  const what = r && r.kind === 'drive' ? 'drive' : 'share';
  return `${name} can’t be reached right now. Its tracks are kept — reconnect the ${what} to browse it.`;
}
