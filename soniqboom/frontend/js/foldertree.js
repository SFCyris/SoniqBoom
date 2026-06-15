// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Toast } from './utils.js';

/**
 * foldertree.js — Lazy-loading folder tree in the sidebar.
 *
 * Each registered scan directory is a collapsed root node.
 * Clicking a chevron lazily fetches its children from GET /api/fstree/children.
 * Clicking a folder name shows its tracks in the main view via a callback.
 */

const API = (path, q = {}) => {
  const qs = new URLSearchParams(q).toString();
  return fetch(`/api${path}${qs ? '?' + qs : ''}`).then(r => r.json());
};

const treeEl   = document.getElementById('folder-tree');
const emptyEl  = document.getElementById('folder-tree-empty');
const toggle   = document.getElementById('btn-folders-toggle');
const wrap     = document.getElementById('folder-tree-wrap');

// Callback set by app.js: (path) => void
let _onSelect = () => {};

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function basename(p) {
  return p.replace(/\\/g, '/').split('/').filter(Boolean).pop() || p;
}

// ── Node construction ─────────────────────────────────────────────────────────

/**
 * Build a tree-node <li> element.
 * @param {string} path    — absolute path
 * @param {string} root    — scan root this node belongs to
 * @param {boolean} isRoot — whether this is a scan root (top-level)
 * @param {boolean} hasAudio — show accent dot
 * @param {boolean} hasChildren — whether children may exist
 */
function makeNode(path, root, { isRoot = false, hasAudio = false, hasChildren = true, alias = '', unavailable = false } = {}) {
  const li = document.createElement('li');
  li.className = (isRoot ? 'tree-root' : '') + (unavailable ? ' tree-unavailable' : '');
  li.dataset.path = path;
  li.dataset.root = root;

  const row = document.createElement('div');
  row.className = 'tree-node';
  // Keyboard-reach the tree row.  ``role="treeitem"`` would be more
  // accurate, but the surrounding markup isn't a proper ARIA tree —
  // ``button`` semantics are accurate enough that screen readers announce
  // the activation gesture without lying about a tree relationship.
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  // Start collapsed; the helper below keeps aria-expanded synced whenever
  // we toggle the open class.
  if (hasChildren) row.setAttribute('aria-expanded', 'false');

  // Chevron
  const chev = document.createElement('span');
  chev.className = `tree-chevron ${hasChildren ? '' : 'leaf'}`;
  chev.innerHTML = '&#9658;'; // ▶
  row.appendChild(chev);

  // Icon — network icon for remote shares, folder for local
  const icon = document.createElement('span');
  icon.className = 'tree-icon';
  const isRemote = path.startsWith('smb://') || path.startsWith('ftp://');
  icon.textContent = isRemote ? '🌐' : '📁';
  row.appendChild(icon);

  // Label
  const label = document.createElement('span');
  label.className = 'tree-label';
  label.textContent = isRoot ? (alias || path) : basename(path);
  label.title = path;
  row.appendChild(label);

  // Audio indicator
  if (hasAudio) {
    const dot = document.createElement('span');
    dot.className = 'tree-audio-dot';
    dot.title = 'Contains audio files';
    row.appendChild(dot);
  }

  // Remove button moved to Admin page only

  // Children container
  const children = document.createElement('ul');
  children.className = 'tree-children';
  let loaded = false;

  // Click chevron or row to expand/collapse
  async function expand() {
    const isOpen = children.classList.contains('open');
    if (isOpen) {
      children.classList.remove('open');
      chev.classList.remove('open');
      row.setAttribute('aria-expanded', 'false');
      return;
    }
    // Lazy-load children on first open
    if (!loaded) {
      loaded = true;
      chev.innerHTML = '⏳';
      try {
        const res = await API('/fstree/children', { path, root });
        children.innerHTML = '';
        if (res.children && res.children.length) {
          res.children.forEach(child => {
            const childLi = makeNode(child.path, root, {
              isRoot: false,
              hasAudio: child.has_audio,
              hasChildren: true,
            });
            children.appendChild(childLi);
          });
          chev.innerHTML = '&#9658;';
        } else {
          // No subfolders — hide the chevron, nothing to expand.
          // Drop ``aria-expanded`` so screen readers stop announcing
          // "collapsed" for a row that isn't actually expandable.
          chev.classList.add('leaf');
          chev.innerHTML = '&#9658;';
          row.removeAttribute('aria-expanded');
          return;  // don't open an empty children list
        }
      } catch (err) {
        loaded = false;
        chev.innerHTML = '&#9658;';
        console.warn('Folder tree: expansion failed for', path, err);
        Toast.error("Couldn't list folder — the share or path may be unavailable.");
        return;
      }
      chev.innerHTML = '&#9658;';
    }
    children.classList.add('open');
    chev.classList.add('open');
    row.setAttribute('aria-expanded', 'true');
  }

  // Expose expand() so a tree rebuild can restore the user's open folders.
  row.__expand = expand;
  row.__isOpen = () => children.classList.contains('open');

  chev.addEventListener('click', (e) => { e.stopPropagation(); expand(); });

  // Click label → show tracks in this directory
  function selectAndExpand() {
    document.querySelectorAll('.tree-node.active').forEach(n => n.classList.remove('active'));
    row.classList.add('active');
    _onSelect(path);
    if (!children.classList.contains('open')) expand();
  }
  row.addEventListener('click', selectAndExpand);
  // Keyboard:
  //   Enter / Space → select+expand (same as click)
  //   ArrowRight   → expand without selecting (if collapsed)
  //   ArrowLeft    → collapse if open
  //   ArrowDown / ArrowUp → move focus to next/prev visible tree row
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); e.stopPropagation();
      selectAndExpand();
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      if (!children.classList.contains('open')) expand();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      if (children.classList.contains('open')) {
        children.classList.remove('open');
        chev.classList.remove('open');
        row.setAttribute('aria-expanded', 'false');
      }
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const rows = Array.from(treeEl.querySelectorAll('.tree-node'));
      const idx = rows.indexOf(row);
      const next = rows[idx + (e.key === 'ArrowDown' ? 1 : -1)];
      if (next) next.focus();
    }
  });

  li.appendChild(row);
  li.appendChild(children);
  return li;
}

// ── Public API ────────────────────────────────────────────────────────────────

function checkEmpty() {
  const hasRoots = treeEl.querySelector('li.tree-root') !== null;
  emptyEl.hidden = hasRoots;
  treeEl.hidden = !hasRoots;
}

// Debounce multiple rapid refresh() calls (WS + dirs-changed fire together)
let _refreshTimer = null;
// Generation token: a refresh that starts while an earlier one is still
// awaiting (dirs fetch / re-expand) supersedes it, so the older run bails out
// instead of wiping nodes the newer run is populating.
let _refreshGen = 0;

function refresh() {
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(_doRefresh, 80);
}

async function _doRefresh() {
  const gen = ++_refreshGen;
  // Snapshot what the user has open + selected so a rebuild doesn't collapse
  // the tree they're browsing.  We restore by re-expanding the same paths
  // (parents first) after the roots render.
  const expanded = [];
  treeEl.querySelectorAll('li[data-path]').forEach(li => {
    const ul = li.querySelector(':scope > .tree-children');
    if (ul && ul.classList.contains('open')) expanded.push(li.dataset.path);
  });
  const activePath = treeEl.querySelector('.tree-node.active')
    ?.closest('li[data-path]')?.dataset.path || null;
  const wasScanning = treeEl.querySelector('.tree-root-scanning') !== null;

  treeEl.innerHTML = '';
  try {
    const { dirs } = await API('/library/dirs');
    if (gen !== _refreshGen) return;       // superseded by a newer refresh
    // Clear again after the async wait — a concurrent call may have rendered already
    treeEl.innerHTML = '';
    const aliases = (window.__sbConfig && window.__sbConfig.folder_aliases) || {};
    dirs.forEach(d => {
      const alias = aliases[d.path] || '';
      const isNet = !!d.network_share_id;
      const unavail = isNet && d.status === 'unavailable';
      const li = makeNode(d.path, d.path, { isRoot: true, hasAudio: true, alias, unavailable: unavail });
      treeEl.appendChild(li);
    });
  } catch {
    // Store not ready yet — silently skip
    return;
  }
  checkEmpty();
  if (wasScanning) _setScanningClass(true);
  if (expanded.length || activePath) _restoreExpansion(expanded, activePath, gen);
}

// Re-open the previously-expanded paths after a rebuild.  Sorted shallow→deep
// so each parent is expanded (and its children lazy-loaded into the DOM)
// before its descendants are looked up.  Best-effort: a path that no longer
// exists is simply skipped.
async function _restoreExpansion(expandedPaths, activePath, gen) {
  const ordered = [...expandedPaths].sort(
    (a, b) => a.split('/').length - b.split('/').length,
  );
  for (const p of ordered) {
    if (gen !== undefined && gen !== _refreshGen) return;   // a newer refresh won
    let li;
    try { li = treeEl.querySelector(`li[data-path="${CSS.escape(p)}"]`); } catch { li = null; }
    if (!li) continue;
    const row = li.querySelector(':scope > .tree-node');
    if (row && row.__expand && !row.__isOpen()) {
      try { await row.__expand(); } catch { /* share offline etc. — skip */ }
    }
  }
  if (activePath) {
    let li;
    try { li = treeEl.querySelector(`li[data-path="${CSS.escape(activePath)}"]`); } catch { li = null; }
    const row = li && li.querySelector(':scope > .tree-node');
    if (row) {
      document.querySelectorAll('.tree-node.active').forEach(n => n.classList.remove('active'));
      row.classList.add('active');
    }
  }
}

function addRoot(path, alias = '') {
  // Remove existing root with same path to avoid duplicates
  treeEl.querySelectorAll('li.tree-root').forEach(li => {
    if (li.dataset.path === path) li.remove();
  });
  const li = makeNode(path, path, { isRoot: true, hasAudio: true, alias });
  treeEl.appendChild(li);
  checkEmpty();
}

function onSelect(fn) {
  _onSelect = fn;
}

// Collapse/expand whole folders section
toggle.addEventListener('click', () => {
  const collapsed = wrap.style.display === 'none';
  wrap.style.display = collapsed ? '' : 'none';
  toggle.classList.toggle('collapsed', !collapsed);
});

// ── Scan indicator — pulse root folders while a scan is active ────────────────
// Driven by WebSocket events in app.js — no polling needed.

function _setScanningClass(on) {
  treeEl.querySelectorAll('li.tree-root').forEach(li => {
    li.classList.toggle('tree-root-scanning', on);
  });
}

function setScanActive(on) {
  _setScanningClass(on);
}

export const FolderTree = { refresh, addRoot, onSelect, setScanActive };
