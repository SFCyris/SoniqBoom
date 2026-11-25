// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * folders.js — the phone Library's Folders: names, roots and the filter (pure:
 * no DOM, no fetch; tests/js/m_folders.test.mjs).
 *
 * The Library's Folders key lists the music folders (the scan roots) and opens
 * one folder per screen (views/library.js).  What is shown of a folder comes
 * from here: its name (an archive's inner folder reads "pack.zip › inner"), a
 * root's name and kind, the line of folders above the open one, the text of
 * the Play / Shuffle header and the filter over a folder's subfolders.
 */

const _SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;

/** A folder inside an archive (the server lists ``pack.zip::inner``). */
export function isArchiveFolder(name) {
  return String(name || '').includes('::');
}

/** A folder's name as shown: an archive's inner folder reads "pack.zip › inner". */
export function folderLabel(name) {
  return String(name || '').split('::').filter(Boolean).join(' › ') || String(name || '');
}

/** What a music folder is: 'collection' (an online collection), 'share' (a
 *  network share) or 'drive' (a folder on the server's own disks). */
export function rootKind(dir) {
  if (dir && dir.catalogue) return 'collection';
  return _SCHEME.test(String((dir && dir.path) || '')) ? 'share' : 'drive';
}

/** A music folder's name: a collection's label, else the folder's alias, else
 *  its last folder name — never a share's address with a user name in it. */
export function rootLabel(dir, aliases = {}) {
  const path = String((dir && dir.path) || '');
  if (dir && dir.label) return String(dir.label);
  const alias = aliases && aliases[path];
  if (alias) return String(alias);
  let rest = path;
  if (_SCHEME.test(rest)) {
    // (user:password@host → host; a password may hold "/" or "@", so
    // everything up to the last "@" goes — a folder named with an "@" loses
    // its start, a password never shows)
    rest = rest.replace(_SCHEME, '');
    const at = rest.lastIndexOf('@');
    if (at >= 0) rest = rest.slice(at + 1);
  }
  const parts = rest.split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

/** Is folder ``p`` folder ``base`` or inside it? */
export function within(p, base) {
  p = String(p || ''); base = String(base || '');
  if (!base) return false;
  return p === base || p.startsWith(base.endsWith('/') ? base : `${base}/`);
}

/** The music folder (scan root) that holds folder ``path`` — the longest root
 *  it lies in — or null. */
export function rootOf(path, roots) {
  let best = null;
  for (const r of roots || []) {
    const base = r && r.path;
    if (base && within(path, base) && (!best || base.length > best.path.length)) best = r;
  }
  return best;
}

/** The line of folders above the open one ("FLAC › DAVID BOWIE"), its start
 *  left out when it holds more than ``keep`` names ("… › B › C"). */
export function parentsText(names, keep = 2) {
  const list = (names || []).map((n) => folderLabel(n)).filter(Boolean);
  if (!list.length) return '';
  const shown = list.length > keep ? ['…', ...list.slice(-keep)] : list;
  return shown.join(' › ');
}

/** "41:43", "1:02:05" */
export function totalTime(sec) {
  const s = Math.round(Number(sec) || 0);
  if (s <= 0) return '';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return `${h ? `${h}:` : ''}${mm}:${String(r).padStart(2, '0')}`;
}

const _n = (k, one, many = `${one}s`) => `${(Number(k) || 0).toLocaleString('en-US')} ${k === 1 ? one : many}`;

/**
 * What the Play / Shuffle keys of an open folder cover, and the line under
 * them.  ``direct``: its own tracks (null: not known yet); ``folders``: how
 * many subfolders it has; ``deep``: the tracks under it, subfolders
 * included (null: not known yet); ``seconds``: the length of its own tracks
 * when every one was read (else null).
 *   → { scope: 'direct' | 'deep' | 'none', play, shuffle, text }
 * A folder with tracks of its own plays those (the ⋯ menu offers the
 * subfolders too); a folder of folders plays everything under it.
 */
export function folderScope({ direct = null, folders = 0, deep = null, seconds = null } = {}) {
  if (direct > 0) {
    // (only what Play covers: its subfolders are listed above, and offered in ⋯)
    const parts = [_n(direct, 'track')];
    const t = totalTime(seconds);
    if (t) parts.push(t);
    return { scope: 'direct', play: 'Play', shuffle: 'Shuffle', text: parts.join(' · ') };
  }
  if (folders > 0) {
    const text = deep == null ? _n(folders, 'folder')
      : `${_n(deep, 'track')} in ${_n(folders, 'folder')}`;
    return { scope: 'deep', play: 'Play all', shuffle: 'Shuffle all', text };
  }
  return { scope: 'none', play: 'Play', shuffle: 'Shuffle', text: '' };
}

// ── The filter over a folder's subfolders ───────────────────────────────────

// Case and accents don't count ("ä" finds "a"); compatibility forms neither
// (full-width letters, ligatures).
export function foldText(s) {
  return String(s || '').normalize('NFKD').replace(/\p{M}+/gu, '').toLowerCase();
}

// Scripts written without spaces between words: a typed piece is looked for
// anywhere in the name.
const _NO_SPACES = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Script=Thai}]/u;
const _ALNUM = /[\p{L}\p{N}]/u;
const _DIGIT = /\p{N}/u;

// Does a word start at ``i`` of ``s``?  At the start, after a space or a sign,
// and where letters turn into digits or digits into letters ("CD10", "Track41").
function _wordStart(s, i) {
  if (i <= 0) return true;
  const a = s[i - 1], b = s[i];
  if (!_ALNUM.test(a)) return true;
  return _DIGIT.test(a) !== _DIGIT.test(b);
}

/** A test for folder names: what was typed must begin a word of the name
 *  ("pur" finds "Purple Motion" and "Ooo Pure", not "Spurt"), or — typed in
 *  a script without spaces (Japanese, Chinese, Korean, Thai) — appear
 *  anywhere in it.  Empty text: every name. */
export function folderMatcher(query) {
  const q = foldText(query).trim().replace(/\s+/g, ' ');
  if (!q) return () => true;
  const anywhere = _NO_SPACES.test(q);
  return (name) => {
    const s = foldText(folderLabel(name)).replace(/\s+/g, ' ');
    if (anywhere) return s.includes(q);
    for (let i = s.indexOf(q); i >= 0; i = s.indexOf(q, i + 1)) {
      if (_wordStart(s, i)) return true;
    }
    return false;
  };
}

// ── Where the Folders key was left (kept on the device) ─────────────────────
// Kept as the music folder's hash (``path_hash`` of GET /api/library/dirs) and
// the names of the folders below it — never a share's address, which may hold
// a user name and password.

/** A subfolder's path: its folder's path, "/", its name (the server's rule for
 *  local folders, shares and an archive's inner folder alike). */
export function childPath(parent, name) {
  return `${String(parent || '').replace(/\/+$/, '')}/${name}`;
}

/** What to keep of the open folders: { root: hash, names: [...] } (null: nothing). */
export function savePath(stack, roots) {
  if (!Array.isArray(stack) || !stack.length) return null;
  const r = (roots || []).find((x) => x && x.path === stack[0].path);
  if (!r || !r.hash) return null;
  return { root: r.hash, names: stack.slice(1).map((l) => String((l && l.name) || '')) };
}

/** The open folders again from what was kept — [{ path, name }], the music
 *  folder first — or [] when its music folder is gone or a name can't be one. */
export function restorePath(saved, roots) {
  if (!saved || typeof saved.root !== 'string' || !Array.isArray(saved.names)) return [];
  if (saved.names.length > 64) return [];
  const r = (roots || []).find((x) => x && x.hash && x.hash === saved.root);
  if (!r) return [];
  const out = [{ path: r.path, name: r.name || rootLabel(r) }];
  for (const n of saved.names) {
    if (typeof n !== 'string' || !n || n.includes('/') || n === '.' || n === '..') return [];
    out.push({ path: childPath(out[out.length - 1].path, n), name: n });
  }
  return out;
}
