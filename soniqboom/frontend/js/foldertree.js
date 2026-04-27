// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

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
          // No subfolders — hide the chevron, nothing to expand
          chev.classList.add('leaf');
          chev.innerHTML = '&#9658;';
          return;  // don't open an empty children list
        }
      } catch {
        loaded = false;
        chev.innerHTML = '&#9658;';
        return;
      }
      chev.innerHTML = '&#9658;';
    }
    children.classList.add('open');
    chev.classList.add('open');
  }

  chev.addEventListener('click', (e) => { e.stopPropagation(); expand(); });

  // Click label → show tracks in this directory
  row.addEventListener('click', () => {
    document.querySelectorAll('.tree-node.active').forEach(n => n.classList.remove('active'));
    row.classList.add('active');
    _onSelect(path);
    if (!children.classList.contains('open')) expand();
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

function refresh() {
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(_doRefresh, 80);
}

async function _doRefresh() {
  treeEl.innerHTML = '';
  try {
    const { dirs } = await API('/library/dirs');
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
  }
  checkEmpty();
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
