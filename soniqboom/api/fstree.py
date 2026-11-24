# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Filesystem tree API — returns the directory tree under a scan root.

Nodes are directories only; the client can request children lazily (one level
at a time) or get the full recursive tree for small roots.

The ``tracks-with-meta`` endpoint provides a hybrid listing: the filesystem is
the source of truth for *which* files exist, and the store enriches them with
metadata when available.
"""
from __future__ import annotations

import io
import logging
import os
import pickle
import re
import threading
import time
import uuid
import zipfile
from bisect import bisect_left
from collections import OrderedDict
from itertools import compress
from operator import itemgetter, methodcaller, ne
from pathlib import Path

import asyncio

# Drill-down refresh debounce: path → monotonic timestamp of the last
# accepted refresh.  Bounded by the number of distinct folders browsed
# per server lifetime.
_REFRESH_RECENT: dict[str, float] = {}
# Running drill-down refresh jobs (strong refs; a server stop cancels them
# before its final journal flush — ``scanner.stop_background_writers``).
_DRILL_TASKS: set = set()

from fastapi import APIRouter, HTTPException, Query

from soniqboom.api.tracks import json_route
from soniqboom.core import cache_stats
from soniqboom.core.metadata import FORMAT_NAMES, SUPPORTED_EXTENSIONS
from soniqboom.core.scanner import _is_junk_filename
from soniqboom.models.track import TrackMeta

log = logging.getLogger(__name__)

# Register live entry-count providers so the cache-stats endpoint can show
# each tier's "fullness".  Cheap ``len()`` calls invoked once per poll.
cache_stats.register_size("browse",    lambda: len(_TRACKS_META_CACHE))
cache_stats.register_size("scan_root", lambda: len(_SCAN_ROOT_FULL_CACHE))
cache_stats.register_size("per_path",  lambda: len(_STORE_RECURSIVE_CACHE))

router = APIRouter(prefix="/fstree", tags=["fstree"])

# Disk-persisted browse cache: skips the ~5 s warmup on subsequent boots
# when nothing under any scan root has changed.  Lives next to
# ``library.json``.  Format is a pickled dict (see ``_load_browse_cache``
# for the schema and version-handling).  Pickle chosen over JSON for raw
# load speed: 80 MB of native Python dicts loads in ~300 ms via pickle
# vs. several seconds via the stdlib ``json`` module.
_BROWSE_CACHE_FILENAME = "browse_cache.pickle"
_BROWSE_CACHE_VERSION = 2          # 2: rows carry the game fields (game, game_aliases …)


def _is_remote(path: str) -> bool:
    # The shared scheme set — WebDAV roots are http(s):// (GitHub #18: an
    # smb/ftp-only copy sent them down the local branch → 404 on every browse).
    from soniqboom.core.filesource import is_remote_path
    return is_remote_path(path)


_DIGIT_RUN = re.compile(r"(\d+)")


def _folder_sort_key(name: str) -> tuple:
    """Order of folder names in every listing (the desktop tree and the phone's
    folder lists): case-insensitive, a run of digits read as a number — "CD2"
    before "CD10", "Disc 9" before "Disc 10".  The name itself breaks a tie
    ("CD01" / "CD1"), so the order is the same every time."""
    parts = _DIGIT_RUN.split(name.casefold())
    # (``split`` with one group alternates text, digits, text …: the digit
    # runs sit at the odd positions of every name, so numbers meet numbers)
    return tuple(int(x) if i % 2 else x for i, x in enumerate(parts)), name


# ── Folder order of a recursive listing ──────────────────────────────────────
# A recursive listing (``tracks-with-meta?recursive=true``: a branch folder's
# flat view and its play queue on the desktop, Play all / Shuffle all on the
# phone) lists its rows folder by folder in the order the folder lists show:
# every folder of a row's path compared by ``_folder_sort_key`` (CD2 before
# CD10), a folder's own tracks before its subfolders', each subfolder whole.
# The tracks of ONE folder keep their path order — the order the folder's own
# listing shows (a local, network-share or catalogue folder's), so Play all
# plays each album as opening it would (a folder not indexed yet is walked on
# disk, by lower-cased path, which both of its listings keep).  Paths are
# split at "/" only, as the folder tree splits them: an archive's top-level
# members are tracks of the folder that holds it, a folder inside an archive
# (``pack.zip::inner``) is a subfolder.  The scan roots' sorted path indexes
# stay in plain path order (every subtree lookup bisects them); a listing is
# put in this order once, when it is built, and cached with it
# (``_STORE_RECURSIVE_CACHE``, ``_NAT_LISTINGS``).

_GET_ID = methodcaller("get", "id")


def _folder_order(dirs: list[str], base: int = 0) -> list[int]:
    """Positions ``base … base + len(dirs) - 1`` in folder order, where
    ``dirs[i]`` is row ``i``'s folder ("/"-separated; "" = the listed folder
    itself).  Stable: the rows of one folder keep their order."""
    k = len(dirs)
    if k < 2:
        return list(range(base, base + k))
    # Runs of consecutive rows of one folder (path order can put a subfolder
    # between two of a folder's files: two runs of it).
    cuts = list(compress(range(1, k), map(ne, dirs[1:], dirs[:-1])))
    starts = [0, *cuts]
    ends = [*cuts, k]
    # The folders as a tree, walked depth first: a folder's own runs, then its
    # subfolders by ``_folder_sort_key`` of their NAMES.  It allocates a list
    # per parent folder and a key per distinct name, not a key per folder
    # (300K rows, five orderings: 24 young garbage collections, against 407
    # with a key tuple per folder sorted flat — the same order).
    first: dict[str, int] = {}       # folder -> its first run
    last: dict[str, int] = {}        # folder -> its last run so far
    nxt = [-1] * len(starts)         # run -> the next run of the same folder
    kids: dict[str, list[str]] = {}  # folder -> its subfolders
    linked = {""}                    # ("" = the listed folder itself)
    for r, s in enumerate(starts):
        d = dirs[s]
        prev = last.get(d)
        last[d] = r
        if prev is not None:
            nxt[prev] = r
            continue
        first[d] = r
        while d not in linked:       # it and its ancestors not in the tree yet
            linked.add(d)
            i = d.rfind("/")
            parent = d[:i] if i >= 0 else ""
            lst = kids.get(parent)
            if lst is None:
                kids[parent] = [d]
            else:
                lst.append(d)
            d = parent
    seg_keys: dict[str, tuple] = {}

    def _name_key(folder: str) -> tuple:
        name = folder[folder.rfind("/") + 1:]
        sk = seg_keys.get(name)
        if sk is None:
            sk = seg_keys[name] = _folder_sort_key(name)
        return sk

    order: list[int] = []
    add = order.extend
    stack = [""]
    while stack:
        d = stack.pop()
        r = first.get(d, -1)
        while r >= 0:                # its own rows, run by run (stable)
            add(range(base + starts[r], base + ends[r]))
            r = nxt[r]
        sub = kids.get(d)
        if sub:
            sub.sort(key=_name_key, reverse=True)     # (the stack pops the first one first)
            stack.extend(sub)
    return order


def _subtree_order(paths: list[str], lo: int, hi: int, plen: int) -> list[int]:
    """Indices ``lo … hi - 1`` of path-sorted ``paths`` — the rows under one
    folder, whose prefix (with its trailing "/") is ``plen`` long — in folder
    order (``_folder_order``)."""
    return _folder_order([p[plen:p.rfind("/")] for p in paths[lo:hi]], lo)


def _prefix_end(paths: list[str], prefix: str, lo: int) -> int:
    """End of the run of path-sorted ``paths`` that start with ``prefix``,
    which begins at ``lo`` (``bisect_left(paths, prefix)``)."""
    hi = bisect_left(paths, prefix + "\U0010FFFF", lo)
    n = len(paths)
    while hi < n and paths[hi].startswith(prefix):   # (a name starting with U+10FFFF)
        hi += 1
    return hi


def _in_folder_order(rows: list[dict]) -> list[dict]:
    """``rows`` (any listing, e.g. a file-system walk's) in folder order by each
    row's ``path``; rows of one folder keep their order."""
    if len(rows) < 2:
        return rows
    order = _folder_order([str(r.get("path") or "").rpartition("/")[0] for r in rows])
    return list(map(rows.__getitem__, order))


def _first_of_each_id(rows: list[dict]) -> list[dict]:
    """``rows`` with each track id once (its first row)."""
    ids = list(map(_GET_ID, rows))
    if len(set(ids)) == len(ids):
        return rows
    seen: set = set()
    out: list[dict] = []
    for r, tid in zip(rows, ids):
        if tid not in seen:
            seen.add(tid)
            out.append(r)
    return out


# ── Scan-root confinement ────────────────────────────────────────────────────
# Every browse endpoint that takes a folder path answers only for paths under a
# registered scan root (the server's own list — never a root the caller names).
# A path outside every root gets this one 403, judged before the path itself is
# looked at on disk: missing and existing paths outside the roots answer alike.

_OUTSIDE_DETAIL = "Path is not under any registered scan root"


def _outside() -> HTTPException:
    return HTTPException(403, _OUTSIDE_DETAIL)


def _under(path: str, root: str) -> bool:
    """Is normalised absolute ``path`` the normalised ``root`` or inside it
    (component-wise: ``/M/Foo`` is not under ``/M/Fo``)?"""
    if root == "/":
        return path.startswith("/")
    return path == root or path.startswith(root + "/")


def _norm_abs(path: str) -> str:
    """``os.path.normpath`` with POSIX's kept leading ``//`` collapsed."""
    n = os.path.normpath(path)
    return "/" + n.lstrip("/") if n.startswith("//") else n


def _local_roots() -> list[str]:
    """The registered LOCAL scan roots, normalised (read on the event loop:
    the store's scan-dir map is not for worker threads to iterate)."""
    from soniqboom.core.store import get_store
    return [_norm_abs(str(sd["path"])) for sd in get_store().list_scan_dirs()
            if sd.get("path") and not _is_remote(sd["path"])
            and str(sd["path"]).startswith("/")]


def _local_root_hashes(store, path: str) -> "tuple[list[str], list[str]]":
    """``(holding, below)``: the path hashes of the registered LOCAL scan roots
    whose indexes can hold tracks under folder ``path`` — the roots that hold
    it (it, or a folder above it) and the roots below it.

    Nested roots (``/M`` and ``/M/Box/CD2`` both registered) both cover the
    inner folder, and each of its tracks is tagged with ONE of them: the root
    whose scan read it last.  That tag moves.  A scan of a root counts only the
    tracks tagged with that root as indexed (``scanner._run_scan_body``,
    ``refresh_subtree_under_root``), so it reads the files under it that the
    other root holds again and tags them with itself — a scan of both roots
    swaps the inner folder's tags.  A folder's tracks can sit in any of these
    indexes: listing only the roots above it misses what the roots below hold."""
    from soniqboom.core.data import path_hash
    p = path.rstrip("/")
    pre = p + "/"
    holding: list[str] = []
    below: list[str] = []
    for sd in store.list_scan_dirs():
        root_path = sd.get("path", "")
        if not root_path or _is_remote(root_path):
            continue
        r = root_path.rstrip("/")
        if p == r or p.startswith(r + "/"):
            into = holding
        elif r.startswith(pre):
            into = below
        else:
            continue
        h = sd.get("path_hash") or path_hash(root_path)
        if h and h not in holding and h not in below:
            into.append(h)
    return holding, below


def _lexical_local(path: str) -> str:
    """``path`` normalised without touching the disk — or the shared 403 for
    anything that isn't an absolute POSIX path."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise _outside()
    return _norm_abs(path)


# Real path of each registered root (``os.path.realpath``), so a request
# doesn't resolve every root again: refreshed after _ROOT_REAL_TTL_S; a root
# no longer registered is dropped.  Bounded by the roots' number.
_ROOT_REAL: dict[str, tuple[str, float]] = {}
_ROOT_REAL_TTL_S = 60.0


def _root_real(n: str) -> str:
    hit = _ROOT_REAL.get(n)
    now = time.monotonic()
    if hit is not None and now - hit[1] < _ROOT_REAL_TTL_S:
        return hit[0]
    real = os.path.realpath(n)
    _ROOT_REAL[n] = (real, now)
    return real


def _confine_local(path: str, hint_root: str | None = None,
                   roots: list[str] | None = None) -> tuple[Path, Path]:
    """The real (symlink-free) path of local folder ``path`` and the real path
    of the registered scan root it is under — or the shared 403.  Does file
    system calls (``realpath``): run it in a worker thread with ``roots``
    from ``_local_roots()`` (``_confine_local_async``).

    Two checks.  First lexically: the normalised path must sit under a
    registered root (by its stored spelling, or by its real path for a root
    registered through a symlinked spelling, e.g. /tmp/… on macOS), so
    nothing outside the roots is ever looked up — only the roots' own paths
    are resolved (cached).  Then the path's ``realpath`` must still sit
    under that root's ``realpath``: a ``..`` or a symlink inside a root can't
    lead out of it.  The caller uses the returned real path — what was
    checked is what is read.  ``hint_root`` (the tree root the client
    browses under) only picks among the registered roots that hold the
    path; it never authorises one."""
    lexical = _lexical_local(path)
    if roots is None:
        roots = _local_roots()
    stale = [k for k in _ROOT_REAL if k not in roots]
    for k in stale:
        _ROOT_REAL.pop(k, None)
    try:
        cands = [(n, _root_real(n)) for n in roots if _under(lexical, n)]
        if not cands:
            cands = [(n, real_r) for n, real_r in ((n, _root_real(n)) for n in roots)
                     if _under(lexical, real_r)]
        if not cands:
            raise _outside()
        real = os.path.realpath(lexical)
    except (OSError, ValueError):
        raise _outside()
    hint = _norm_abs(hint_root) if isinstance(hint_root, str) and hint_root.startswith("/") else None
    # The client's tree root first (when it is one of the matches), then the
    # most specific root (nested roots).
    cands.sort(key=lambda c: (c[0] != hint and c[1] != hint, -len(c[1])))
    for _n, real_r in cands:
        if _under(real, real_r):
            return Path(real), Path(real_r)
    raise _outside()


async def _confine_local_async(path: str, hint_root: str | None = None) -> tuple[Path, Path]:
    """``_confine_local`` off the event loop (its ``realpath`` calls can stall
    on a network mount)."""
    lexical = _lexical_local(path)          # (malformed: refused without a thread hop)
    roots = _local_roots()
    if not any(_under(lexical, n) for n in roots) and not any(
            _under(lexical, h[0]) for n, h in list(_ROOT_REAL.items()) if n in roots):
        # Under no root by any spelling known so far: the only thing left to
        # look at is the real path of a root not resolved yet.
        if all(n in _ROOT_REAL for n in roots):
            raise _outside()
    return await asyncio.to_thread(_confine_local, path, hint_root, roots)


def _confine_remote(path: str) -> None:
    """403 unless remote ``path`` (a share or an online collection) sits under
    a registered remote scan root and has no ``.`` / ``..`` segment (which a
    share server would resolve above the root)."""
    if not isinstance(path, str) or any(c in path for c in ("\x00", "\r", "\n")):
        raise _outside()
    from soniqboom.core.store import get_store
    for sd in get_store().list_scan_dirs():
        r = str(sd.get("path") or "")
        if not r or not _is_remote(r):
            continue
        root = r.rstrip("/")
        if path == r or path.rstrip("/") == root or path.startswith(root + "/"):
            rest = path[len(root):]
            if any(seg in (".", "..") for seg in rest.replace("\\", "/").split("/")):
                raise _outside()
            return
    raise _outside()


def _has_audio(path: Path) -> bool:
    """Return True if the directory contains at least one supported audio file (shallow).

    Uses ``os.scandir`` which caches each entry's type — ``entry.is_file()``
    no longer triggers a separate ``stat()`` syscall per child, cutting
    syscall count in half on wide directories.
    """
    try:
        with os.scandir(path) as it:
            for entry in it:
                if _is_junk_filename(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTENSIONS:
                    return True
    except (PermissionError, FileNotFoundError, OSError):
        return False
    return False


def _subtree_has_indexed_audio(store, path: Path) -> bool:
    """O(log N) check: does any indexed track's stored path sit under ``path``?

    Used by the "Do not display empty folders" toggle to hide directories
    in the sidebar tree that contain no playable audio anywhere in their
    subtree (only images, videos, text files, or empty subdirectories).

    Walks every LOCAL scan root that contains ``path`` and every root below
    it (nested roots: a track is tagged with one of them —
    ``_local_root_hashes``), then bisects each root's warm sorted-by-path
    cache for the first entry ``>=`` ``path/``.  If that entry still has
    ``path/`` as a prefix, there's at least one indexed track somewhere
    under ``path``.

    Returns True (don't hide) when the path is outside every local scan
    root — we can't be sure what's there, and silently swallowing
    folders the user can still navigate to is more surprising than
    leaving them visible.  Remote paths likewise return True because the
    store-side per-scan-root index doesn't currently cover them in the
    same shape; the remote check would need a separate code path.
    """
    from bisect import bisect_left

    p_str = str(path)
    if _is_remote(p_str):
        # No store-side index in the same shape for remote shares.
        return True

    holding, below = _local_root_hashes(store, p_str)
    if not holding:
        # Path isn't under any registered local scan root — treat as
        # "no idea, show it".  Hiding it would silently amputate parts
        # of the tree the user can still navigate to via folder aliases.
        return True

    prefix = p_str.rstrip("/") + "/"
    for h in holding + below:
        sorted_paths, _dicts = _get_or_build_scan_root_sorted(store, h)
        if not sorted_paths:
            continue
        idx = bisect_left(sorted_paths, prefix)
        if idx < len(sorted_paths) and sorted_paths[idx].startswith(prefix):
            return True
        # Edge case: a track whose stored path IS exactly ``path``
        # (extremely rare — a directory-shaped path).  ``bisect_left``
        # would place it just before ``prefix``.
        if idx > 0 and sorted_paths[idx - 1] == p_str:
            return True
    return False


def _remote_subtree_has_indexed_audio(store, scan_root: str, child_rel: str) -> bool:
    """Remote counterpart of :func:`_subtree_has_indexed_audio`.

    Remote tracks are stored as ``{scan_root}:/{relative-path}`` and tagged
    with ``path_hash(scan_root)`` (see ``_remote_tracks_with_meta``).  Bisect
    the root's sorted-by-path cache for the first entry ``>=`` the child's
    ``{scan_root}:/{child_rel}/`` prefix; a hit means ≥1 indexed track lives
    somewhere under the child folder.

    Returns ``False`` (hide) when nothing under the child is indexed.  During
    an active scan a not-yet-indexed folder is hidden until its first track
    lands, then appears — the sorted cache invalidates on the root's
    bucket-size change.
    """
    from bisect import bisect_left
    from soniqboom.core.data import path_hash

    sorted_paths = _sorted_paths_for(store, scan_root)
    if not sorted_paths:
        return False
    prefix = f"{scan_root}:/{child_rel.strip('/')}/"
    idx = bisect_left(sorted_paths, prefix)
    return idx < len(sorted_paths) and sorted_paths[idx].startswith(prefix)


def _filter_remote_nonempty(store, scan_root: str, children: list[dict]) -> list[dict]:
    """Drop remote child folders that contain no indexed audio anywhere."""
    return [
        c for c in children
        if _remote_subtree_has_indexed_audio(store, scan_root, c.get("rel", ""))
    ]


def _dir_node(path: Path, root: Path) -> dict:
    """Build a single directory node."""
    rel = str(path.relative_to(root))
    return {
        "name": path.name,
        "path": str(path),
        "rel": rel,
        "has_audio": _has_audio(path),
        "children": [],
    }


def _children(path: Path, root: Path) -> list[dict]:
    """Return immediate subdirectories of path, in ``_folder_sort_key`` order.

    ``has_audio`` comes from the in-RAM index (one bisect per child) instead
    of reading every child directory off disk.  The old per-child ``scandir``
    made a folder click cost 1 + N directory reads — on scan roots that live
    on a network mount (smbfs/NFS), N extra round-trips per click, seconds
    when the attribute cache is cold.  The index answer is also deeper: a
    folder whose music sits in sub-subfolders now reports audio correctly.
    Not-yet-indexed folders read as no-audio until the next scan touches
    them — display-only, and self-heals.
    """
    root_s = str(root)
    found: list[Path] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir():
                        continue
                    # A symlinked folder that leads out of the scan root is not
                    # listed (browsing it is refused: ``_confine_local``).
                    if e.is_symlink() and not _under(os.path.realpath(e.path), root_s):
                        continue
                except OSError:
                    continue
                found.append(Path(e.path))
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return []
    dirs = sorted(found, key=lambda p: _folder_sort_key(p.name))
    from soniqboom.core.store import get_store
    store = get_store()
    return [
        {
            "name": d.name,
            "path": str(d),
            "rel": str(d.relative_to(root)),
            "has_audio": _subtree_has_indexed_audio(store, d),
            "children": [],
        }
        for d in dirs
    ]


def _local_children_from_store(store, path: str, root: str) -> list[dict] | None:
    """Immediate subdirectories of a LOCAL path, derived from the in-RAM index.

    The local counterpart of :func:`_remote_children_from_store`.  Bisect-skips
    the sorted-path caches of the scan roots that can hold tracks under ``path``
    (``root``, and with nested roots every root above or below ``path`` —
    ``_local_root_hashes``) to list child folders that contain indexed audio
    anywhere underneath — with ZERO filesystem I/O.  That matters because
    an SMB/NFS share mounted as a local path (``/Volumes/...``) otherwise costs a
    network ``os.scandir`` (plus a ``stat`` for ``exists``/``is_dir``) on every
    chevron click, which is the random "folder browsing is slow" stall.

    Returns ``None`` when nothing under ``path`` is indexed, so the caller falls
    back to a live ``os.scandir`` — covering not-yet-scanned corners and folders
    whose only contents are non-audio.  Path-safe by construction: it only ever
    returns children whose paths sit under the passed-in scan ``root``.
    """
    from bisect import bisect_left
    from soniqboom.core.data import path_hash

    if _is_remote(path) or _is_remote(root):
        return None
    p_clean = path.rstrip("/")
    root_clean = root.rstrip("/")
    if not (p_clean == root_clean or p_clean.startswith(root_clean + "/")):
        return None
    prefix = p_clean + "/"
    plen = len(prefix)
    names: list[str] = []
    seen: set[str] = set()
    indexed = False
    holding, below = _local_root_hashes(store, p_clean)
    # ``root``'s own index, then every other root that can hold tracks here.
    for h in dict.fromkeys([path_hash(root), *holding, *below]):
        sorted_paths, _dicts = _get_or_build_scan_root_sorted(store, h)
        n = len(sorted_paths)
        i = bisect_left(sorted_paths, prefix)
        if i < n and sorted_paths[i].startswith(prefix):
            indexed = True
        while i < n and sorted_paths[i].startswith(prefix):
            rest = sorted_paths[i][plen:]
            slash = rest.find("/")
            if slash > 0:
                child = rest[:slash]
                if child not in seen:
                    seen.add(child)
                    names.append(child)
                # Skip the whole child subtree in one bisect.  Sentinel is the
                # MAX code point (U+10FFFF): a plain U+FFFF would sort *before* a
                # grandchild dir whose name starts in the astral plane (emoji,
                # CJK-Ext), leaving the bisect pinned at ``i`` → infinite loop.
                # The ``max(i+1, …)`` guard makes progress unconditional regardless.
                nxt = bisect_left(sorted_paths, f"{prefix}{child}/\U0010FFFF", i)
                i = nxt if nxt > i else i + 1
            else:
                i += 1                        # a file directly inside this folder
    if not indexed:
        return None                           # nothing indexed here → caller goes live
    names.sort(key=_folder_sort_key)
    rel_base = "" if p_clean == root_clean else p_clean[len(root_clean) + 1:] + "/"
    return [
        {
            "name": nm,
            "path": f"{prefix}{nm}",
            "rel": f"{rel_base}{nm}",
            "has_audio": True,            # derived from indexed tracks, by construction
            "children": [],
        }
        for nm in names
    ]


def _remote_children_from_store(store, scan_root: str, remote_path: str) -> list[dict] | None:
    """Derive a remote folder's immediate subdirectories from the in-RAM index.

    Remote tracks are stored as ``{scan_root}:/{rel}``; bisect-skipping over
    the root's sorted-path cache yields the child folder names in
    O(children x log n) with zero network I/O — the live ``list_dir`` walk
    goes through the FTP browse lane (budget: 1 connection) and stalls behind
    scans.  Returns ``None`` when nothing under ``remote_path`` is indexed,
    so the caller can fall back to a live listing for unindexed corners.
    """
    from bisect import bisect_left
    from soniqboom.core.data import path_hash

    sorted_paths = _sorted_paths_for(store, scan_root)
    if not sorted_paths:
        return None
    rel = remote_path.strip("/")
    prefix = f"{scan_root}:/" + (f"{rel}/" if rel else "")
    i = bisect_left(sorted_paths, prefix)
    if not (i < len(sorted_paths) and sorted_paths[i].startswith(prefix)):
        return None                       # nothing indexed here → caller goes live
    names: list[str] = []
    plen = len(prefix)
    n = len(sorted_paths)
    while i < n and sorted_paths[i].startswith(prefix):
        rest = sorted_paths[i][plen:]
        slash = rest.find("/")
        if slash > 0:
            child = rest[:slash]
            names.append(child)
            # Skip the whole child subtree in one bisect.  Sentinel is the
            # MAX code point (U+10FFFF): a plain U+FFFF would sort *before* a
            # grandchild dir whose name starts in the astral plane (emoji,
            # CJK-Ext), leaving the bisect pinned at ``i`` → infinite loop.
            # The ``max(i+1, …)`` guard makes progress unconditional regardless.
            nxt = bisect_left(sorted_paths, f"{prefix}{child}/\U0010FFFF", i)
            i = nxt if nxt > i else i + 1
        else:
            i += 1                        # a file directly inside this folder
    names.sort(key=_folder_sort_key)
    base = scan_root.rstrip("/") + ("" if remote_path == "/" else remote_path)
    rel_base = f"{rel}/" if rel else ""
    return [
        {
            "name": nm,
            "path": f"{base}/{nm}",
            "rel": f"{rel_base}{nm}",
            "has_audio": True,            # derived from indexed tracks, by construction
            "children": [],
        }
        for nm in names
    ]


def _remote_list_children(scan_root: str, remote_path: str, source) -> list[dict]:
    """List subdirectories of a remote path via FileSource."""
    try:
        entries = source.list_dir(remote_path)
    except TimeoutError:
        # Browse-lane borrow timed out — surfaces FTP pool exhaustion (e.g. the
        # browse budget too small under heavy concurrent listing) rather than
        # silently rendering an empty folder.
        log.warning("Remote folder listing timed out (FTP browse lane busy): %s", remote_path)
        return []
    except Exception:
        return []
    dirs = sorted(
        [e for e in entries if e.is_dir and not e.name.startswith(".")],
        key=lambda e: _folder_sort_key(e.name),
    )
    base = scan_root.rstrip("/") + ("" if remote_path == "/" else remote_path)
    return [
        {
            "name": d.name,
            "path": f"{base}/{d.name}",
            "rel": f"{remote_path.lstrip('/')}/{d.name}".lstrip("/"),
            "has_audio": True,
            "children": [],
        }
        for d in dirs
    ]


@router.get("/children")
async def get_children(
    path: str = Query(..., description="Absolute directory path"),
    root: str | None = Query(None, description="Scan root the folder tree browses under"),
):
    """Return immediate subdirectories of *path* (lazy expansion).

    When the ``hide_empty_folders`` config flag is set, subdirectories
    whose entire subtree contains zero indexed audio tracks are dropped
    from the response — useful when a scan root is shared with non-audio
    content (photo dumps, build directories, video files) and the user
    just wants to navigate music.

    ``path`` must sit under a registered scan root (403 otherwise — the same
    answer whether or not it exists).
    """
    if _is_remote(path):
        _confine_remote(path)
        from soniqboom.core.filesource import find_source_for_path
        result = find_source_for_path(path)
        if not result:
            raise HTTPException(503, "Network share not connected")
        scan_root, remote_path, source = result
        loop = asyncio.get_running_loop()
        # Store-first: indexed shares answer from RAM instantly. Only paths
        # with nothing indexed underneath fall back to a live network walk.
        from soniqboom.core.store import get_store
        children = await loop.run_in_executor(
            None, _remote_children_from_store, get_store(), scan_root, remote_path,
        )
        if children is not None:
            return {"path": path, "children": children}
        from soniqboom.core import catalogue
        if catalogue.is_catalogue_root(scan_root):
            # A catalogue's folders are its tracks' folders (the index lists
            # more — companions, formats out of scope — that are no tracks).
            return {"path": path, "children": []}
        children = await loop.run_in_executor(
            None, _remote_list_children, scan_root, remote_path, source,
        )
        # Hide-empty filter (same toggle as local): drop remote subfolders with
        # zero indexed audio anywhere under them.  Store-based — during a scan a
        # folder appears once its first track is indexed.
        from soniqboom.core.data import get_config as _get_config
        if children and bool(await _get_config("hide_empty_folders", False)):
            from soniqboom.core.store import get_store
            children = await loop.run_in_executor(
                None, _filter_remote_nonempty, get_store(), scan_root, children,
            )
        return {"path": path, "children": children}

    # Store-first: derive children straight from the RAM index — no filesystem
    # touch at all, so a folder on an SMB/NFS mount (presented as /Volumes/...)
    # expands instantly instead of paying a network os.scandir + stat per click.
    # The helper only returns paths under the registered scan root, so it's
    # path-safe; it returns None for unindexed corners, which fall through to a
    # live listing.  hide_empty is a no-op on store children (all contain audio).
    loop = asyncio.get_running_loop()
    from soniqboom.core.store import get_store
    store = get_store()
    # Store-first answers from the index alone (no disk access) — so a lexical
    # check against the registered roots is all it needs: it can only name
    # folders that indexed tracks sit in.
    lexical = _lexical_local(path)
    for n in _local_roots():
        if _under(lexical, n):
            children = _local_children_from_store(store, lexical, n)
            if children is not None:
                return {"path": path, "children": children}
    p, r = await _confine_local_async(path, root)
    children = _local_children_from_store(store, str(p), str(r))
    if children is not None:
        return {"path": path, "children": children}

    # Nothing indexed under this path — fall back to a live listing.  Only now
    # do we touch the filesystem (exists/is_dir + iterdir), which is the slow
    # part on a cold network mount; it covers brand-new and non-audio folders.
    if not await asyncio.to_thread(p.is_dir):          # (a stat: off the loop)
        raise HTTPException(404, f"Directory not found: {path}")

    children = await loop.run_in_executor(None, _children, p, r)

    # Optional filter: drop subfolders with no indexed audio anywhere
    # under them.  Cheap because ``_subtree_has_indexed_audio`` is a
    # single bisect on the warm sorted scan-root cache.  Config is
    # read once per request, not per child, so the lookup overhead is
    # also O(1) amortised across the children list.
    from soniqboom.core.data import get_config as _get_config
    hide_empty = bool(await _get_config("hide_empty_folders", False))
    if hide_empty and children:
        children = [
            c for c in children
            if _subtree_has_indexed_audio(store, Path(c["path"]))
        ]
    return {"path": str(p), "children": children}


@router.post("/refresh")
async def refresh_subtree(body: dict):
    """Queue a lightweight background scan for the subtree rooted at *path*.

    Body: ``{"path": "/abs/dir"}``.

    Designed to be fired-and-forgotten by ``showFolder()`` in the
    frontend: the user clicks a folder, the UI renders instantly from
    the existing store (sub-100 ms perceived latency — see VU-D15), and
    in parallel this endpoint queues a freshness scan for that subtree
    so any files added since the last scan get indexed.

    The scanner uses the dir-mtime fast path (FRESHNESS-A) — when no
    descendant directory has been touched since the last scan, the
    walk short-circuits in ~50 ms.  Real work happens only when files
    actually changed, and concurrent requests deduplicate via
    ``start_scan``'s queue logic.

    Returns ``202 Accepted`` immediately; progress is broadcast on the
    existing ``scan_progress`` WS event.  Security: *path* must sit
    under a registered scan root — otherwise users could trigger
    arbitrary FS walks of the filesystem.
    """
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    # Remote paths are auto-polled by remote_freshness; the local
    # scanner can't drive them so a client-triggered refresh is a no-op.
    if _is_remote(raw):
        return {"queued": False, "reason": "remote — handled by freshness loop"}

    # Confine to registered scan roots — refusing arbitrary paths
    # closes a potential FS-walk DoS vector AND scopes the scan to
    # what's actually part of the library.  Judged BEFORE the path is looked
    # at on disk: a path outside every root gets the same 403 whether or not
    # it exists (no probing the server's file system).
    p, matched_root = await _confine_local_async(raw)
    if not await asyncio.to_thread(p.is_dir):          # (a stat: off the loop)
        raise HTTPException(404, f"Directory not found: {raw}")

    # One refresh per path per 30 s — back-and-forth clicking between
    # siblings must not stack walks on a network mount.
    now = time.monotonic()
    if now - _REFRESH_RECENT.get(str(p), 0.0) < 30.0:
        return {"queued": False, "reason": "recently refreshed"}
    _REFRESH_RECENT[str(p)] = now

    root_str, sub_str = str(matched_root), str(p)

    async def _job() -> None:
        try:
            from soniqboom.core.scanner import refresh_subtree_under_root
            res = await refresh_subtree_under_root(root_str, sub_str)
            if res.get("added") or res.get("updated") or res.get("removed"):
                # Same silent scan_progress completion the full scanner emits —
                # the frontend's in-place folder refresh keys off last_dirs.
                from soniqboom.api.library import _broadcast
                await _broadcast({
                    "event": "scan_progress", "running": False, "silent": True,
                    "total": res.get("checked", 0), "processed": res.get("checked", 0),
                    "errors": res.get("errors", 0), "pct": 100.0,
                    "current_file": "", "paused": False,
                    "last_plan": {}, "last_dirs": [sub_str],
                    "current_dirs": [], "queued": [],
                })
                log.info("Drill-down refresh %s: +%d added, %d updated, -%d removed",
                         sub_str, res["added"], res["updated"], res["removed"])
        except Exception:
            log.exception("Drill-down refresh failed for %s", sub_str)

    task = asyncio.create_task(_job(), name="drill-down-refresh")
    _DRILL_TASKS.add(task)
    task.add_done_callback(_DRILL_TASKS.discard)
    return {"queued": True}


@router.get("/tracks")
async def tracks_in_dir(
    path: str = Query(..., description="Absolute directory path"),
    recursive: bool = Query(False),
):
    """Return track paths found directly in *path* (or recursively).  Local
    folders under a registered scan root only (403 otherwise)."""
    if _is_remote(path):
        _confine_remote(path)
        raise HTTPException(404, f"Directory not found: {path}")
    p, _root = await _confine_local_async(path)
    if not await asyncio.to_thread(p.is_dir):          # (a stat: off the loop)
        raise HTTPException(404, f"Directory not found: {path}")

    ext_set = {e.lower() for e in SUPPORTED_EXTENSIONS}

    def _list_files() -> list[Path]:
        # Previous implementation ran ``rglob`` once per supported extension —
        # that's ~40 full recursive walks of the same tree per request.
        # One ``os.walk`` covers them all in a single pass.
        result: list[Path] = []
        if recursive:
            for dirpath, _dirs, filenames in os.walk(p):
                for fn in filenames:
                    if _is_junk_filename(fn):
                        continue
                    if os.path.splitext(fn)[1].lower() in ext_set:
                        result.append(Path(os.path.join(dirpath, fn)))
        else:
            try:
                with os.scandir(p) as it:
                    for entry in it:
                        if _is_junk_filename(entry.name):
                            continue
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if os.path.splitext(entry.name)[1].lower() in ext_set:
                            result.append(Path(entry.path))
            except (PermissionError, FileNotFoundError, OSError):
                return []
        result.sort(key=lambda f: f.name.lower())
        return result

    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, _list_files)
    return {"path": str(p), "tracks": [str(f) for f in files]}


# ── Hybrid listing — filesystem + store metadata ─────────────────────────────

_EXT_SET = {e.lower() for e in SUPPORTED_EXTENSIONS}


def _is_audio(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in _EXT_SET


def _zip_basename(member: str) -> str:
    """Last component of a (forward-slash) zip member name."""
    return member.rsplit("/", 1)[-1] if "/" in member else member


def _scan_zip(zip_path: str) -> list[Path]:
    """Enumerate audio files inside a ZIP (including nested ZIPs)."""
    from soniqboom.core import archive
    results: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            def _head_of(real, _zf=zf):
                with _zf.open(real) as fh:
                    return fh.read(archive.HEAD_BYTES)
            shown: set = set()
            plain: list = []
            for member in zf.namelist():
                if _is_junk_filename(_zip_basename(member)):
                    continue
                if _is_audio(member):
                    # (the scanner's name: ``mod.Opus`` as its bytes say)
                    name = archive.content_name(member, _head_of)
                    if archive.rejected_by_bytes(member, name, _head_of):
                        continue
                    shown.add(name)
                    results.append(Path(f"{zip_path}::{name}"))
                elif archive.sniffable(member):
                    plain.append(member)
                elif member.lower().endswith(".zip"):
                    # Nested ZIP (e.g. modarchive: outer.zip -> track.it.zip -> track.it)
                    try:
                        inner_data = zf.read(member)
                        with zipfile.ZipFile(io.BytesIO(inner_data), "r") as inner_zf:
                            def _inner_head(real, _zf=inner_zf):
                                with _zf.open(real) as fh:
                                    return fh.read(archive.HEAD_BYTES)
                            for inner_name in inner_zf.namelist():
                                if _is_junk_filename(_zip_basename(inner_name)):
                                    continue
                                if _is_audio(inner_name):
                                    shown = archive.content_name(inner_name, _inner_head)
                                    if archive.rejected_by_bytes(inner_name, shown, _inner_head):
                                        continue
                                    results.append(Path(f"{zip_path}::{member}::{shown}"))
                    except (zipfile.BadZipFile, OSError):
                        pass
            # (members only their bytes name, as the scanner lists them)
            for disp in archive.sniffed_members(plain, _head_of,
                                                taken=shown | set(zf.namelist())):
                results.append(Path(f"{zip_path}::{disp}"))
    except (zipfile.BadZipFile, OSError):
        pass
    return results


def _scan_disk_image(img_path: str) -> list[Path]:
    """Playable members inside a C64/Amiga disk image, as ``::`` virtual paths.

    Mirrors :func:`_scan_zip`.  Extension list is kept in step with
    ``soniqboom.core.diskimage.DISK_IMAGE_EXTS``.
    """
    from soniqboom.core import diskimage
    try:
        return [Path(f"{img_path}::{m}") for m in diskimage.list_members(img_path)]
    except OSError:
        return []


def _scan_archive(arc_path: str) -> list[Path]:
    """Playable members inside a ZIP/LHA/LZH archive, as ``::`` virtual paths.

    Used for ``.lha``/``.lzh`` (Amiga); ``.zip`` keeps its dedicated
    ``_scan_zip`` (nested-archive) path above.
    """
    from soniqboom.core import archive
    return [Path(f"{arc_path}::{m}") for m in archive.list_members(arc_path)]


def _discover_audio(p: Path, recursive: bool) -> list[Path]:
    """Return audio files from the filesystem, including inside ZIP archives."""
    files: list[Path] = []

    if recursive:
        for dirpath, _dirs, filenames in os.walk(p):
            for fn in filenames:
                if _is_junk_filename(fn):
                    continue
                full = os.path.join(dirpath, fn)
                if _is_audio(fn):
                    files.append(Path(full))
                elif fn.lower().endswith(".zip"):
                    files.extend(_scan_zip(full))
                elif fn.lower().endswith((".d64", ".d71", ".d81", ".adf")):
                    files.extend(_scan_disk_image(full))
                elif fn.lower().endswith((".lha", ".lzh")):
                    files.extend(_scan_archive(full))
    else:
        try:
            for e in os.scandir(p):
                if not e.is_file(follow_symlinks=False):
                    continue
                if _is_junk_filename(e.name):
                    continue
                if _is_audio(e.name):
                    files.append(Path(e.path))
                elif e.name.lower().endswith(".zip"):
                    files.extend(_scan_zip(e.path))
                elif e.name.lower().endswith((".d64", ".d71", ".d81", ".adf")):
                    files.extend(_scan_disk_image(e.path))
                elif e.name.lower().endswith((".lha", ".lzh")):
                    files.extend(_scan_archive(e.path))
        except PermissionError:
            return []

    files.sort(key=lambda f: str(f).lower())
    return files


def _track_id(filepath: Path) -> str:
    """Deterministic track ID — same algorithm as scanner.py."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(filepath)))


def _make_stub(filepath: Path) -> dict:
    """Minimal track info derived purely from the filesystem."""
    s = str(filepath)
    # For ZIP-contained files (path::member), stat the ZIP archive itself
    if "::" in s:
        parts = s.split("::")
        actual = Path(parts[0])
        # Use the innermost member name as title
        title = Path(parts[-1]).stem
        ext = Path(parts[-1]).suffix.lower()
    else:
        actual = filepath
        title = filepath.stem
        ext = filepath.suffix.lower()
    try:
        st = actual.stat()
        file_size = st.st_size
        mtime = st.st_mtime
    except OSError:
        file_size = None
        mtime = 0.0
    return {
        "id": _track_id(filepath),
        "path": s,
        "title": title,
        "artist": "",
        "album": "",
        "format": FORMAT_NAMES.get(ext, ext.lstrip(".").upper()),
        "duration": 0.0,
        "file_size": file_size,
        "mtime": mtime,
        "_scanned": False,
    }


# ── tracks-with-meta result cache ──────────────────────────────────────────
#
# The endpoint is hot — Files-app-style browsers poll it every time the user
# clicks into a directory.  Re-running ``_discover_audio`` (os.walk + maybe
# zipfile reads) plus 1000s of ``uuid5`` calls on every navigate is wasted
# work when the directory and the store haven't changed.
#
# Cache key: ``(absolute path, recursive flag)``.  Invalidation is keyed
# ONLY on the directory's own mtime.  Earlier we also mixed in the store's
# global ``_mutation_seq`` "for safety", but that caused a tail-wag bug:
# ANY mutation anywhere in the store (rating bump, a sibling folder's
# watcher scan, a freshness re-walk of a different share) bumped the
# global counter, invalidating EVERY folder's cache — read-path latency
# climbed because the cache effectively never served (VU-D19).  Dir mtime
# alone reflects whether THIS directory's file set changed, which is
# exactly what this cache layer protects.  We don't cache the final
# response because per-track metadata (rating, play count) may differ
# per request and is resolved fresh from the store on every call.
_TRACKS_META_CACHE: dict[tuple[str, bool], dict] = {}


# ── store-fast-path result cache ──────────────────────────────────────────
#
# ``_store_recursive_tracks_under`` is fast in absolute terms (~1.1 s for
# C64Music's 56K SID tracks, ~2.6 s for modarchive's 111K tracks) but every
# local folder listing calls it, once per page — a branch folder opens
# flattened, and a single folder takes its direct tracks from it — so each
# page and each revisit of the same root would otherwise pay that cost again.
# Caching here turns them into sub-millisecond responses.
#
# Invalidation policy: the cached entry is keyed by ``(absolute path)`` for
# ``recursive=True`` and validated against the hash, the *size* and the
# generation of every scan-root bucket the helper visited (the roots holding
# the folder and the roots below it).  When a scanner walk adds or removes
# tracks under one of those roots, the bucket size changes and we
# recompute; another set of roots is another fingerprint.  Rating bumps /
# play-count increments / sibling-folder writes do NOT change bucket sizes,
# so they correctly don't invalidate.  This
# avoids both the tail-wag of ``_mutation_seq`` (VU-D19) and the
# false-positives of pure directory mtime (which doesn't bump when the
# scanner upserts an existing path).
_STORE_RECURSIVE_CACHE: dict[str, dict] = {}
# Per-scan-root generation, part of every per-folder listing's fingerprint:
# ``invalidate_scan_root`` bumps it when a root's track SET changed without its
# COUNT changing (a move = one delete + one add), which the bucket-size
# fingerprint can't see.
_ROOT_GEN: dict[str, int] = {}

# Who may install a root's sorted index (``_SCAN_ROOT_FULL_CACHE``).  It is also
# built in worker threads — the folder tree (``get_children`` →
# ``_remote_children_from_store``, the local ``_children`` fallback) and the
# Media Server (``dlna_upnp._root_rows``) call ``_get_or_build_scan_root_sorted``
# off the loop — while the loop commits.  The events that refresh or drop the
# index move the root's change count (``_ROOT_CHANGES``) or the browse epoch: a
# commit's patch (``patch_scan_root_rows``, also one that finds no index to
# patch), rows re-shaped in place (``note_rows_reshaped``), a drop
# (``invalidate_scan_root``, ``invalidate_browse_cache``).  A build installs
# only when neither moved and the slot holds what it held when the build began
# (``_install_index``); else its rows answer its own call and the next call
# builds again.  The checks and the moves hold ``_INDEX_LOCK`` (never across an
# await), so neither slips between the other's steps.  (Not part of any
# listing's fingerprint: re-shaping rows in place must not rebuild listings.)
# Writes that refresh nothing reach no index, built on the loop or not: an
# mtime / size touch is never re-shaped.  (Every scan patches: a local scan's
# commit, a folder click's refresh, a share scan's flushes and ghost purge —
# ``scanner._refresh_browse_after_commit``.)
_INDEX_LOCK = threading.Lock()
_ROOT_CHANGES: dict[str, int] = {}
_BROWSE_EPOCH = 0


def _index_stamp(scan_root_hash: str) -> tuple:
    """What a build of the root's index began under — taken before it reads
    the store (``_install_index``)."""
    with _INDEX_LOCK:
        return (_ROOT_CHANGES.get(scan_root_hash, 0), _BROWSE_EPOCH,
                _SCAN_ROOT_FULL_CACHE.get(scan_root_hash))


def _install_index(scan_root_hash: str, stamp: tuple, entry: dict) -> bool:
    """Install ``entry`` as the root's index unless something outdated the rows
    it was built from since ``stamp`` (``_index_stamp``) was taken."""
    changes, epoch, slot = stamp
    with _INDEX_LOCK:
        if (_ROOT_CHANGES.get(scan_root_hash, 0) != changes or _BROWSE_EPOCH != epoch
                or _SCAN_ROOT_FULL_CACHE.get(scan_root_hash) is not slot):
            return False
        _SCAN_ROOT_FULL_CACHE[scan_root_hash] = entry
    return True


def _note_change(scan_root_hash: str) -> None:
    # (the caller holds ``_INDEX_LOCK``)
    _ROOT_CHANGES[scan_root_hash] = _ROOT_CHANGES.get(scan_root_hash, 0) + 1


def note_rows_reshaped(scan_root_hashes) -> None:
    """Rows of these roots are re-shaped in place after a field write
    (``folder_album.refresh_album_caches``): an index of them being built now
    may hold the values from before the write, so it is not installed."""
    with _INDEX_LOCK:
        for h in scan_root_hashes:
            if h:
                _note_change(h)


_PATCH_MAX = 20_000     # rows IN beyond this → drop and rebuild instead (rows out are cheap)
# Roots with a commit in flight: their cached listing is served as it was
# before the commit (never rebuilt from a half-committed store) until the
# commit patches it — ``root_commit``.
_ROOTS_COMMITTING: dict[str, int] = {}


class root_commit:
    """``with fstree.root_commit(hashes):`` around a commit's store writes and
    its browse patch."""

    def __init__(self, hashes) -> None:
        self._h = [h for h in hashes if h]

    def __enter__(self):
        for h in self._h:
            _ROOTS_COMMITTING[h] = _ROOTS_COMMITTING.get(h, 0) + 1
        return self

    def __exit__(self, *exc):
        for h in self._h:
            n = _ROOTS_COMMITTING.get(h, 0) - 1
            if n > 0:
                _ROOTS_COMMITTING[h] = n
            else:
                _ROOTS_COMMITTING.pop(h, None)
        return False


def _shape_row(d: dict) -> "dict | None":
    """A store track as a browse row — exactly what the root cache holds."""
    try:
        out = TrackMeta(**{k: v for k, v in d.items()
                           if k in TrackMeta.model_fields and k != "embedding"}
                        ).model_dump(exclude={"embedding"})
    except Exception:
        return None
    out["_scanned"] = True
    return out


async def patch_scan_root_rows(scan_root_hash: str, removed_paths: "set[str]",
                               added: "list[dict]", new_size: int) -> bool:
    """Apply a commit's rows OUT (by path) and rows IN (store dicts) to one
    scan root's cached sorted listing, then invalidate the listings derived
    from it.  Idempotent: every cached copy of a touched path is replaced.
    Rows are shaped in chunks with yields; the entry is replaced in one
    assignment (a reader never sees half an update).  Only an entry that was
    in step with the store before the commit is patched (else a write that
    bypassed this refresh would survive as ghost rows).  No cached entry →
    nothing to patch.  Either way the root's change count moves first, so an
    index a worker thread is building from the store as it was before this
    commit is not installed (``_install_index``).  Returns False when the
    caller should drop the root."""
    import asyncio
    with _INDEX_LOCK:
        entry = _SCAN_ROOT_FULL_CACHE.get(scan_root_hash)
        _note_change(scan_root_hash)
    if entry is None:
        return True
    if len(added) > _PATCH_MAX:
        return False
    if entry.get("size") != new_size - len(added) + len(removed_paths):
        return False
    from bisect import bisect_left, bisect_right
    shaped: list[tuple[str, dict]] = []
    for i in range(0, len(added), 1_000):
        shaped.extend((r["path"], r) for r in map(_shape_row, added[i : i + 1_000])
                      if r is not None)
        if len(added) > 1_000:
            await asyncio.sleep(0)
    if _SCAN_ROOT_FULL_CACHE.get(scan_root_hash) is not entry:
        return False                          # replaced meanwhile: let it rebuild
    shaped.sort(key=lambda x: x[0])
    touched = set(removed_paths) | {p for p, _d in shaped}
    old_paths, old_dicts = entry["paths"], entry["dicts"]
    # Cut out the [lo, hi) run of every touched path (all copies — idempotent),
    # copying the lists once: a reader in a worker thread keeps the old ones.
    paths: list[str] = []
    dicts: list[dict] = []
    if len(touched) * 16 > len(old_paths):
        # many paths: one membership pass beats a bisect per path
        from itertools import compress
        keep = [p not in touched for p in old_paths]
        paths = list(compress(old_paths, keep))
        dicts = list(compress(old_dicts, keep))
    else:
        pos = 0
        for p in sorted(touched):
            lo = bisect_left(old_paths, p)
            hi = bisect_right(old_paths, p, lo)
            if hi > lo:
                paths.extend(old_paths[pos:lo])
                dicts.extend(old_dicts[pos:lo])
                pos = hi
        paths.extend(old_paths[pos:])
        dicts.extend(old_dicts[pos:])
    if shaped:
        # Merge the two sorted runs by slices: a bisect per added row from the
        # previous position, then one slice copy per gap — linear in the root
        # (a one-by-one ``insert`` cost 56 ms for 512 rows landing at the head
        # of a 112K root, a re-sort ~40-50 ms; perf round 7).
        new_paths: list[str] = []
        new_dicts: list[dict] = []
        pos = 0
        for p, d in shaped:
            i = bisect_left(paths, p, pos)
            if i > pos:
                new_paths.extend(paths[pos:i])
                new_dicts.extend(dicts[pos:i])
                pos = i
            new_paths.append(p)
            new_dicts.append(d)
        new_paths.extend(paths[pos:])
        new_dicts.extend(dicts[pos:])
        paths, dicts = new_paths, new_dicts
    with _INDEX_LOCK:
        if _SCAN_ROOT_FULL_CACHE.get(scan_root_hash) is not entry:
            return False                      # (replaced while it merged)
        _SCAN_ROOT_FULL_CACHE[scan_root_hash] = {"size": new_size, "paths": paths,
                                                 "dicts": dicts}
    _bump_root_gen(scan_root_hash)
    return True


def _bump_root_gen(scan_root_hash: str) -> None:
    # (The listing memos hold their source list and check identity: listings
    # rebuilt under the new generation are new lists — nothing to clear.  The
    # root's kept recursive listings would never be served again either; they
    # are dropped so they don't hold the old rows.)
    _ROOT_GEN[scan_root_hash] = _ROOT_GEN.get(scan_root_hash, 0) + 1
    _nat_drop(scan_root_hash)


def cached_ids_under(store, scan_root_hash: str, prefix: str) -> "list[str] | None":
    """Track ids whose path starts with ``prefix``, from the root's cached
    sorted rows by binary search — or None when no in-step cache exists (the
    caller scans the root instead)."""
    e = _SCAN_ROOT_FULL_CACHE.get(scan_root_hash)
    if (e is None or scan_root_hash in _ROOTS_COMMITTING
            or e.get("size") != len(store._tag_scan_root_hash.get(scan_root_hash, ()))):
        return None
    from bisect import bisect_left
    paths, dicts = e["paths"], e["dicts"]
    i = bisect_left(paths, prefix)
    out: list[str] = []
    while i < len(paths) and paths[i].startswith(prefix):
        out.append(dicts[i].get("id"))
        i += 1
    return out


def invalidate_scan_root(scan_root_hash: str) -> None:
    """Drop one scan root's sorted cache and every per-folder listing / memo
    built from it (they hold its row dicts); other roots keep theirs.  An index
    of the root being built meanwhile is not installed (``_install_index``)."""
    with _INDEX_LOCK:
        _SCAN_ROOT_FULL_CACHE.pop(scan_root_hash, None)
        _note_change(scan_root_hash)
    _bump_root_gen(scan_root_hash)
    with _MEMO_LOCK:                          # frees the dropped rows' memos
        for memo in (_DEDUP_MEMO, _DIRECT_MEMO, _BYID_MEMO):
            memo.clear()


# ── per-scan-root sorted-by-path cache ──────────────────────────────────
#
# Each ``_store_recursive_tracks_under`` call USED to iterate every track
# in the matching scan-root bucket (56K for SID, 111K for tracker) and
# filter by path-prefix.  Cold compute = ~1.1 s and ~2.6 s respectively,
# paid PER subfolder.  Clicking five SID composer folders in a row was
# therefore 5 × 1.1 s of redundant work — each call recomputed the same
# 56K filter, just for a different prefix range.
#
# This cache amortises that cost: the first request under any scan root
# builds a sorted-by-path list of every track (shape-normalised through
# ``TrackMeta``), and every subsequent request — for ANY subtree of the
# same scan root — does an O(log N) binary search + O(matches) slice
# instead of an O(N) walk + reshape.  For SID/MUSICIANS/Hubbard_Rob
# (~95 tracks) this is microseconds instead of milliseconds.
#
# Invalidation: scan-root bucket size.  Same fingerprint
# ``_STORE_RECURSIVE_CACHE`` uses — a scanner walk that adds/removes
# tracks flips the count and forces a rebuild.  Rating bumps / play-count
# writes / unrelated mutations don't change the count so don't invalidate.
_SCAN_ROOT_FULL_CACHE: dict[str, dict] = {}


# Recursive listings of share and catalogue folders, in folder order (local
# folders keep theirs in ``_STORE_RECURSIVE_CACHE``), so a folder is put in
# order once, not on every page.  Keyed ``(kind, root hash, prefix)``; an entry
# holds the root index's path list it was cut from and is served only while
# the index still is that list (an index rebuilt or patched is a new list).
# Small listings are cheap to order again and are not kept, which leaves the
# slots to the big ones (a root's whole listing, counted by the phone with
# ``limit=1``).
_NAT_LISTINGS: "OrderedDict[tuple, tuple[list, list]]" = OrderedDict()
_NAT_LISTINGS_MAX = 16
_NAT_MIN_ROWS = 2000
_NAT_LOCK = threading.Lock()        # (catalogue listings are built in worker threads)


def _nat_listing(key: tuple, index_paths: list) -> "list | None":
    """The kept listing ``key`` when it was cut from ``index_paths``, else None."""
    with _NAT_LOCK:
        hit = _NAT_LISTINGS.get(key)
        if hit is None:
            return None
        if hit[0] is not index_paths:
            del _NAT_LISTINGS[key]
            return None
        _NAT_LISTINGS.move_to_end(key)
        return hit[1]


def _nat_keep(key: tuple, index_paths: list, listing: list) -> None:
    if len(listing) < _NAT_MIN_ROWS:
        return
    with _NAT_LOCK:
        _NAT_LISTINGS[key] = (index_paths, listing)
        _NAT_LISTINGS.move_to_end(key)
        while len(_NAT_LISTINGS) > _NAT_LISTINGS_MAX:
            _NAT_LISTINGS.popitem(last=False)


def _nat_drop(scan_root_hash: "str | None" = None) -> None:
    """Forget the kept listings of one root (all of them: None)."""
    with _NAT_LOCK:
        if scan_root_hash is None:
            _NAT_LISTINGS.clear()
        else:
            for k in [k for k in _NAT_LISTINGS if k[1] == scan_root_hash]:
                del _NAT_LISTINGS[k]


def invalidate_browse_cache(*, drop_disk: bool = True) -> None:
    """Clear the folder-browse caches so the next request re-reads track
    metadata from the store.

    The three caches above bake ``duration`` (and other TrackMeta fields) into
    their entries and are validated ONLY by scan-root bucket size + dir mtime
    — by design, so rating/play-count writes don't invalidate them.  But a
    BULK FIELD update that changes a cached field WITHOUT changing the track
    count (e.g. applying HVSC song lengths to existing SID tracks) leaves them
    serving stale values.  Callers doing such an update must invalidate here,
    and drop the on-disk pickle so a restart doesn't reload the stale copy
    (its validity check is bucket-size only, which is unchanged).  A root index
    being built meanwhile is not installed (the browse epoch moves:
    ``_install_index``)."""
    global _BROWSE_EPOCH
    _TRACKS_META_CACHE.clear()
    _STORE_RECURSIVE_CACHE.clear()
    with _INDEX_LOCK:
        _SCAN_ROOT_FULL_CACHE.clear()
        _BROWSE_EPOCH += 1
    _nat_drop()
    if drop_disk:
        try:
            from soniqboom.config import get_data_dir
            (get_data_dir() / _BROWSE_CACHE_FILENAME).unlink(missing_ok=True)
        except Exception:
            pass


def _dir_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _build_id_map(files: list[Path]) -> dict[str, Path]:
    """Compute deterministic uuid5 ids for ``files``.

    Wrapped in its own helper so the discover + uuid generation can be
    pushed onto a thread-pool together (UUID generation for a 5K-file
    directory is non-trivial CPU work).
    """
    return {_track_id(f): f for f in files}


def _store_recursive_tracks_under(store, p: Path) -> list[dict] | None:
    """Return scanned tracks under local directory ``p`` straight from the store.

    Mirrors the remote-path fast path in ``_remote_tracks_with_meta``: when
    a ``recursive=true`` listing is requested, we don't need to re-walk the
    FS because every track under every registered scan root has already
    been indexed by the scanner.

    Steps:

    1. Find every local scan root whose index can hold tracks under ``p``:
       every root that contains ``p`` AND every root below it
       (``_local_root_hashes``).  We union their buckets rather than
       picking one because nested scan roots are allowed (e.g.
       ``/.../C64Music`` and ``/.../C64Music/DEMOS`` may both be
       registered): a track is tagged with whichever root's scan read it
       last, so picking only the deepest, only the shallowest, or only the
       roots above ``p`` can miss tracks that were tagged with another.
    2. Bisect each root's sorted path index (``_get_or_build_scan_root_sorted``:
       every track of the root, TrackMeta-shaped once) for the run of
       paths under ``p/`` — O(log N), not a walk of the root's bucket.
    3. Put the rows in folder order (``_subtree_order``; the rows of nested
       roots merged in path order first), each track id once.  A track
       stored at ``p``'s own path (rare) lists first.
    4. Cache the list per path, validated by the roots' hashes, bucket
       sizes and generations (``_STORE_RECURSIVE_CACHE``).

    Returns
    -------
    None
        ``p`` is not under any local scan root — caller should fall back
        to ``_discover_audio``.
    list[dict]
        TrackMeta-shaped dicts with ``_scanned: True``, in folder order
        (the folder's own tracks first, in path order: what its
        non-recursive listing keeps).  May be empty when at least one scan
        root matches but no indexed tracks live under ``p`` — caller
        treats empty as "fall through to FS walk for correctness".  The
        list is the cached one: callers must not modify it.

    ``_store_recursive_tracks_under_async`` is the same for the endpoint,
    with a big folder's ordering done in a worker thread.
    """
    plan = _recursive_plan(store, p)
    if not isinstance(plan, _RecursivePlan):
        return plan                     # None (under no local root) or the cached list
    return _recursive_keep(plan, _recursive_rows(plan))


# A listing of at least this many rows is put in folder order in a worker
# thread, not on the event loop (``_store_recursive_tracks_under_async``,
# ``_remote_tracks_with_meta``): ~5-15 ms of ordering at this size, up to a
# few hundred ms for a whole big root.
_ORDER_IN_THREAD_ROWS = 10_000


async def _store_recursive_tracks_under_async(store, p: Path) -> list[dict] | None:
    """``_store_recursive_tracks_under`` for the endpoint: the index lookups run
    here on the loop; ordering ``_ORDER_IN_THREAD_ROWS`` rows or more runs in a
    worker thread (``_recursive_rows`` only reads the index lists, which nothing
    modifies: a patch or a rebuild installs new ones).  What the thread built
    is kept only while those indexes are still the roots' (``_recursive_keep``)."""
    plan = _recursive_plan(store, p)
    if not isinstance(plan, _RecursivePlan):
        return plan
    if plan.size >= _ORDER_IN_THREAD_ROWS:
        rows = await asyncio.to_thread(_recursive_rows, plan)
    else:
        rows = _recursive_rows(plan)
    return _recursive_keep(plan, rows)


class _RecursivePlan:
    """The index runs a recursive listing is built from (``_recursive_plan``),
    and the roots' index path lists they were read from (``indexes``)."""

    __slots__ = ("p_str", "fingerprint", "plen", "runs", "head", "size", "indexes")

    def __init__(self, p_str: str, fingerprint: tuple, plen: int,
                 runs: "list[tuple[list[str], list[dict], int, int]]",
                 head: "list[dict]", indexes: "list[tuple[str, list[str]]]") -> None:
        self.p_str, self.fingerprint, self.plen = p_str, fingerprint, plen
        self.runs, self.head, self.indexes = runs, head, indexes
        self.size = sum(hi - lo for _sp, _sd, lo, hi in runs)


def _recursive_plan(store, p: Path):
    """``p``'s cached recursive listing (a list), None when ``p`` is under no
    local scan root, else the ``_RecursivePlan`` to build it from."""
    p_str = str(p)

    # Every local scan root whose index can hold tracks under ``p``: the
    # roots that contain it and the roots below it (``_local_root_hashes`` —
    # nested roots tag a track with one of them).  Remote roots (smb://,
    # ftp://, WebDAV) are left out: their tracks have a different path
    # encoding and are handled by ``_remote_tracks_with_meta``.
    holding, below = _local_root_hashes(store, p_str)
    if not holding:
        return None
    root_hashes = holding + below

    # ── Cache check ──────────────────────────────────────────────────
    # Each of these roots' hash, bucket size and generation is the
    # validity fingerprint.  Adding/removing tracks under one of these
    # roots bumps the count, and another set of roots is another
    # fingerprint; rating bumps / play-count writes / unrelated scans
    # change none of it.
    def _served_size(h: str) -> int:
        if h in _ROOTS_COMMITTING:
            e = _SCAN_ROOT_FULL_CACHE.get(h)
            if e is not None:
                return e.get("size", -1)       # the rows a commit-window click is served
        return len(store._tag_scan_root_hash.get(h, ()))
    bucket_sizes = tuple((h, _served_size(h), _ROOT_GEN.get(h, 0)) for h in root_hashes)
    cached = _STORE_RECURSIVE_CACHE.get(p_str)
    if cached is not None and cached.get("bucket_sizes") == bucket_sizes:
        cache_stats.hit("per_path")
        return cached["results"]
    cache_stats.miss("per_path")

    # Prefix-match on the stored ``path``.  Adding the trailing slash
    # prevents matching siblings whose names share a prefix
    # (``/foo/bar`` should not match ``/foo/bar2/...``).
    p_norm = p_str.rstrip("/")
    prefix = p_norm + "/"
    plen = len(prefix)
    runs: list[tuple[list[str], list[dict], int, int]] = []
    head: list[dict] = []
    indexes: list[tuple[str, list[str]]] = []

    for h in root_hashes:
        # Per-scan-root sorted cache: build once, reuse for every path
        # under the same root.  Without this, every NEW subfolder click
        # paid the O(bucket-size) iterate + shape cost again (1.1 s for
        # SID's 56K bucket, even when the result was a 95-track composer
        # subfolder).  With it, the first click warms the sorted list,
        # and every subsequent click — for any other subfolder — does an
        # O(log N) bisect, then orders just the matches (``_recursive_rows``).
        sorted_paths, sorted_dicts = _get_or_build_scan_root_sorted(store, h)
        if not sorted_paths:
            continue
        indexes.append((h, sorted_paths))
        start = bisect_left(sorted_paths, prefix)
        end = _prefix_end(sorted_paths, prefix, start)
        if end > start:
            runs.append((sorted_paths, sorted_dicts, start, end))
        # Edge case: a track whose stored path equals ``p_norm`` itself
        # (rare — a directory-shaped track path) would sit just BEFORE
        # ``start`` because ``prefix`` adds a trailing slash.
        if start > 0 and sorted_paths[start - 1] == p_norm:
            head.append(sorted_dicts[start - 1])
    return _RecursivePlan(p_str, bucket_sizes, plen, runs, head, indexes)


def _recursive_rows(plan: _RecursivePlan) -> list[dict]:
    """``plan``'s rows in folder order, each track id once.  Only reads the
    index lists (safe in a worker thread)."""
    runs, plen = plan.runs, plan.plen
    if len(runs) == 1:
        # One scan root (the usual case): its rows under the folder are one
        # run of its index.
        sp, sd, lo, hi = runs[0]
        body = list(map(sd.__getitem__, _subtree_order(sp, lo, hi, plen)))
    elif runs:
        # Nested scan roots (above or below the folder) holding tracks under
        # it: their rows in path order together first (stable: the first
        # root's copy of a path leads), so the rows of one folder keep that order.
        merged = sorted(((sp[i], sd[i]) for sp, sd, lo, hi in runs for i in range(lo, hi)),
                        key=itemgetter(0))
        mpaths = [m[0] for m in merged]
        body = [merged[j][1] for j in _subtree_order(mpaths, 0, len(mpaths), plen)]
    else:
        body = []
    # (Each track once: nested roots can both hold one.)
    return _first_of_each_id(plan.head + body if plan.head else body)


def _recursive_keep(plan: _RecursivePlan, results: list[dict]) -> list[dict]:
    # Cache for revisits — invalidation is via the recorded fingerprint, so
    # a subsequent scanner walk that adds/removes tracks under any of the
    # roots it read (holding the folder or below it) flips one of the sizes
    # and forces a recompute.
    # Kept only while every root index it was read from is still that root's:
    # a listing ordered in a thread may come back after the index was dropped
    # (``invalidate_browse_cache`` moves no fingerprint), patched or rebuilt —
    # the request still gets it (as of when it started), the cache doesn't.
    for h, paths in plan.indexes:
        e = _SCAN_ROOT_FULL_CACHE.get(h)
        if e is None or e.get("paths") is not paths:
            return results
    _STORE_RECURSIVE_CACHE[plan.p_str] = {
        "bucket_sizes": plan.fingerprint,
        "results": results,
    }
    return results


def warmup_scan_root_caches(data_dir: Path | None = None) -> dict[str, int]:
    """Pre-build ``_SCAN_ROOT_FULL_CACHE`` for every LOCAL scan root.

    Called from startup (``main.py`` after ``init_persistence``) so the
    first user click on any folder under any local scan root lands warm
    — instead of paying the one-time ~1.2 s (SID, 56K tracks) /
    ~3 s (modarchive, 111K tracks) cold cost on the user's first
    interaction.  The startup splash is already a waiting indicator;
    paying the cost there turns "instant on later visits" into "instant
    every visit."

    When ``data_dir`` is supplied this also persists the warmed cache
    to ``{data_dir}/browse_cache.pickle`` and reloads from it on the
    next boot.  Per-root validity is checked via scan-root bucket-size
    fingerprint: roots whose bucket size still matches are restored from
    disk in milliseconds; roots whose count changed (scanner walked,
    files were added) are rebuilt fresh and the disk file is rewritten
    so the next boot picks them up.

    Skips remote scan roots: a share's index is built when the share is
    first browsed (``_remote_children_from_store``,
    ``_remote_tracks_with_meta``), not here.

    Returns ``{scan_root_path: track_count}`` for logging.  Phase
    progress is surfaced through ``startup_status.set_phase`` so the
    splash shows which root is being warmed.
    """
    from soniqboom.core.store import get_store
    from soniqboom.core.startup_status import set_phase as _ss_phase

    store = get_store()
    counts: dict[str, int] = {}
    local_roots = [
        sd for sd in store.list_scan_dirs()
        if sd.get("path") and not _is_remote(sd["path"])
    ]

    # Try to restore from disk before doing anything else.  ``restored``
    # is the set of scan-root hashes whose entry was loaded successfully
    # AND whose bucket size still matches the live store.  Anything not
    # in this set still needs a build.
    restored: set[str] = set()
    if data_dir is not None:
        restored, restored_tracks = _load_browse_cache(data_dir, store)
        if restored:
            log.info(
                "Browse cache: restored %d/%d local root(s) from disk (%d tracks)",
                len(restored), len(local_roots), restored_tracks,
            )

    total = len(local_roots)
    any_built = False
    for i, sd in enumerate(local_roots, 1):
        path = sd["path"]
        h = sd.get("path_hash")
        if not h:
            from soniqboom.core.data import path_hash
            h = path_hash(path)
        was_cached = h in restored
        _ss_phase(
            "warmup_browse",
            "Pre-warming folder browse cache",
            (f"{i}/{total}: {path} (restored)"
             if was_cached
             else f"{i}/{total}: {path}"),
        )
        # 1. Build the per-scan-root sorted index.  When the disk-cached
        #    entry is valid this is a no-op (returns the restored
        #    entry's data); otherwise it does the full O(bucket-size)
        #    Pydantic + sort pass.
        paths, _dicts = _get_or_build_scan_root_sorted(store, h)
        counts[path] = len(paths)
        if not was_cached:
            any_built = True
        # 2. Seed the per-path recursive cache for the scan root itself: one
        #    ordering of the root's rows (``_subtree_order``: ~20 ms per 60K
        #    rows of album folders measured, a few times that when most
        #    folders hold one or two files).
        try:
            _store_recursive_tracks_under(store, Path(path))
        except Exception:
            # Warmup is best-effort — a single root failing shouldn't
            # block the others from preloading.
            pass

    # 3. Persist whatever's now in ``_SCAN_ROOT_FULL_CACHE`` if anything
    #    was rebuilt.  No churn on boot N when nothing changed since
    #    boot N-1 (everything restored from disk → nothing rebuilt →
    #    no write).
    if any_built and data_dir is not None:
        n_saved = _save_browse_cache(data_dir)
        if n_saved:
            log.info("Browse cache: persisted %d root(s) to disk", n_saved)
    return counts


def _load_browse_cache(data_dir: Path, store) -> tuple[set[str], int]:
    """Restore valid entries from ``{data_dir}/browse_cache.pickle``.

    File format (pickle):
        {
            "version": int,
            "roots": {
                scan_root_hash: {
                    "size": int,                 # bucket size when saved
                    "scan_root_path": str,       # human-readable label
                    "paths": list[str],          # sorted by path
                    "dicts": list[dict],         # in same order
                },
                ...
            },
        }

    Per-root validation: ``entry["size"]`` must equal the live store's
    current bucket size for that scan root hash.  Entries that fail this
    check are dropped — they'll be rebuilt fresh by the warmup walk.

    Returns ``(set of restored scan_root_hashes, total tracks restored)``.
    A missing or corrupt file is silently treated as "nothing to restore".
    """
    cache_path = data_dir / _BROWSE_CACHE_FILENAME
    if not cache_path.exists():
        return set(), 0
    try:
        with open(cache_path, "rb") as f:
            payload = pickle.load(f)
    except (pickle.UnpicklingError, EOFError, AttributeError, ImportError,
            ValueError, KeyError, OSError) as exc:
        log.warning(
            "Browse cache file unusable (%s) — will rebuild and overwrite",
            exc,
        )
        return set(), 0

    if not isinstance(payload, dict) or payload.get("version") != _BROWSE_CACHE_VERSION:
        log.info(
            "Browse cache version mismatch (got %r, want %d) — rebuilding",
            payload.get("version") if isinstance(payload, dict) else "non-dict",
            _BROWSE_CACHE_VERSION,
        )
        return set(), 0

    restored_hashes: set[str] = set()
    total_tracks = 0
    for h, entry in (payload.get("roots") or {}).items():
        if not isinstance(entry, dict):
            continue
        cached_size = entry.get("size")
        # Live bucket size IS the validity fingerprint.  If the scanner
        # added or removed tracks under this root since the last save,
        # the size changes and we throw the stale entry away.
        try:
            current_size = len(store.get_track_ids_for_scan_root(h))
        except Exception:
            continue
        if cached_size != current_size:
            continue
        paths = entry.get("paths")
        dicts = entry.get("dicts")
        if not isinstance(paths, list) or not isinstance(dicts, list):
            continue
        if len(paths) != len(dicts):
            continue
        # Install directly into the in-memory cache.
        _SCAN_ROOT_FULL_CACHE[h] = {
            "size": current_size,
            "paths": paths,
            "dicts": dicts,
        }
        restored_hashes.add(h)
        total_tracks += len(paths)
    return restored_hashes, total_tracks


def _save_browse_cache(data_dir: Path) -> int:
    """Write ``_SCAN_ROOT_FULL_CACHE`` to disk.

    Atomic: writes to ``browse_cache.pickle.tmp`` then renames over the
    real file (POSIX rename is atomic on the same filesystem).  The tmp
    file is fsynced before rename so a power loss between rename and
    flush leaves either the OLD cache intact or the NEW cache fully on
    disk — never a partial pickle.

    Returns the number of root entries written, or 0 on failure.  Cache
    persistence is best-effort: a failure here just means the next boot
    cold-builds, same as before this feature existed.
    """
    if not _SCAN_ROOT_FULL_CACHE:
        return 0
    # Look up scan-root paths so the persisted entries carry a
    # human-readable label (only used for debugging — the validity
    # check is purely on bucket size).
    from soniqboom.core.store import get_store
    store = get_store()
    hash_to_path = {
        (sd.get("path_hash") or ""): sd.get("path", "")
        for sd in store.list_scan_dirs()
    }
    roots_payload: dict[str, dict] = {}
    for h, entry in _SCAN_ROOT_FULL_CACHE.items():
        roots_payload[h] = {
            "size": entry.get("size", 0),
            "scan_root_path": hash_to_path.get(h, ""),
            "paths": entry.get("paths", []),
            "dicts": entry.get("dicts", []),
        }
    payload = {
        "version": _BROWSE_CACHE_VERSION,
        "roots": roots_payload,
    }
    cache_path = data_dir / _BROWSE_CACHE_FILENAME
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, cache_path)
        return len(roots_payload)
    except Exception as exc:
        log.warning("Browse cache: failed to persist (%s)", exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return 0


def _get_or_build_scan_root_sorted(
    store, scan_root_hash: str,
) -> tuple[list[str], list[dict]]:
    """Return the cached sorted-by-path (paths, dicts) for this scan root.

    Pre-shapes each track through ``TrackMeta.model_dump`` so consumers
    never re-pay the Pydantic round-trip.  Cache validity is keyed on
    the scan-root's bucket size; ``store.get_track_ids_for_scan_root``
    is O(1), so the freshness check is essentially free.

    Safe in a worker thread (the folder tree and the Media Server build it
    there): each store row is copied before it is read, and what was built is
    installed only if no commit's patch, in-place re-shape or drop came
    meanwhile (``_install_index``) — else the rows answer this call alone.

    Returns ``([], [])`` when the bucket is empty.
    """
    from soniqboom.core import catalogue
    if scan_root_hash in catalogue.root_hashes():
        # Hundreds of thousands of rows: catalogue roots browse through the
        # lean (path, id) cache (``_catalogue_sorted``), never this one.
        return [], []
    current_size = len(store._tag_scan_root_hash.get(scan_root_hash, ()))
    cached = _SCAN_ROOT_FULL_CACHE.get(scan_root_hash)
    if cached is not None and (cached.get("size") == current_size
                               or scan_root_hash in _ROOTS_COMMITTING):
        # (a commit in flight: serve the pre-commit rows until it patches them)
        cache_stats.hit("scan_root")
        return cached["paths"], cached["dicts"]
    stamp = _index_stamp(scan_root_hash)      # (before the store is read)
    tids_set = store.get_track_ids_for_scan_root(scan_root_hash)
    cache_stats.miss("scan_root")
    _nat_drop(scan_root_hash)                 # (listings cut from the old rows)
    if not tids_set:
        _install_index(scan_root_hash, stamp, {"size": 0, "paths": [], "dicts": []})
        return [], []

    raw_dicts = [d for d in store.get_tracks_batch(list(tids_set)) if d]
    shaped: list[tuple[str, dict]] = []
    fields = TrackMeta.model_fields
    for d in raw_dicts:
        try:
            # (A copy first: the store's dicts are live, and the loop may write
            # one — a key added — while a worker thread iterates it, which
            # raised and lost the row; ``dict.copy`` is one step for the
            # other threads.)
            filtered = {
                k: v for k, v in d.copy().items()
                if k in fields and k != "embedding"
            }
            meta = TrackMeta(**filtered)
            out = meta.model_dump(exclude={"embedding"})
            out["_scanned"] = True
            shaped.append((out.get("path", ""), out))
        except Exception:
            continue
    shaped.sort(key=lambda x: x[0])
    paths = [s[0] for s in shaped]
    dicts = [s[1] for s in shaped]
    # (The size of the id snapshot these rows were built from.)
    _install_index(scan_root_hash, stamp,
                   {"size": len(tids_set), "paths": paths, "dicts": dicts})
    return paths, dicts


# ── Catalogue roots (core/catalogue.py) ─────────────────────────────────
#
# A catalogue root (the Modland catalogue) holds hundreds of thousands of
# tracks: the shaped per-root cache above would cost seconds of CPU and
# hundreds of MB for it.  Its cache keeps only the (path, id) pairs in path
# order; a listing bisects it and shapes just the rows a page returns.
_CAT_SORTED: dict[str, dict] = {}
_CAT_LOCK = threading.Lock()
# Beyond this many rows an un-windowed (``limit=0``) catalogue listing is cut.
_CAT_UNWINDOWED_MAX = 20_000


def _catalogue_sorted(store, scan_root_hash: str) -> tuple[list[str], list[str]]:
    """``(paths, ids)`` of a catalogue root's tracks in path order — cached
    until the root's track count changes (or ``invalidate_scan_root_cache``).
    Blocking (a sort of the whole root) — call it from a worker thread."""
    ids_set = store._tag_scan_root_hash.get(scan_root_hash) or ()
    size = len(ids_set)
    e = _CAT_SORTED.get(scan_root_hash)
    if e is not None and e["size"] == size:
        return e["paths"], e["ids"]
    with _CAT_LOCK:
        e = _CAT_SORTED.get(scan_root_hash)
        if e is not None and e["size"] == size:
            return e["paths"], e["ids"]
        snap: list[str] = []
        for _ in range(5):
            try:
                snap = list(ids_set)
                break
            except RuntimeError:                  # written meanwhile — again
                continue
        tracks = store._tracks
        pairs = [(t.get("path") or "", tid) for tid in snap
                 if (t := tracks.get(tid)) is not None]
        pairs.sort()
        e = {"size": size, "paths": [p for p, _ in pairs], "ids": [i for _, i in pairs]}
        _CAT_SORTED[scan_root_hash] = e
        _nat_drop(scan_root_hash)             # (listings cut from the old rows)
        return e["paths"], e["ids"]


def _sorted_paths_for(store, scan_root: str) -> list[str]:
    """The sorted track paths of remote root ``scan_root``: the lean cache for
    a catalogue root, the shaped root cache for any other."""
    from soniqboom.core import catalogue
    from soniqboom.core.data import path_hash
    h = path_hash(scan_root)
    if catalogue.is_catalogue_root(scan_root):
        return _catalogue_sorted(store, h)[0]
    return _get_or_build_scan_root_sorted(store, h)[0]


def invalidate_scan_root_cache(scan_root: str) -> None:
    """Drop every browse cache of ``scan_root`` (the catalogue after a build)."""
    from soniqboom.core.data import path_hash
    h = path_hash(scan_root)
    with _CAT_LOCK:
        _CAT_SORTED.pop(h, None)
    invalidate_scan_root(h)


def _catalogue_selection(store, scan_root: str, remote_path: str,
                         recursive: bool) -> list[str]:
    """Ids of the catalogue tracks a folder listing shows, in its order: folder
    order for a recursive listing (``_subtree_order``, kept per folder in
    ``_NAT_LISTINGS`` — don't modify the list), path order for the folder's
    own tracks.  Blocking."""
    from soniqboom.core.data import path_hash
    rh = path_hash(scan_root)
    if recursive:
        paths, ids = _catalogue_sorted(store, rh)
        rel = remote_path.strip("/")
        prefix = f"{scan_root}:/" + (f"{rel}/" if rel else "")
        key = ("ids", rh, prefix)
        sel = _nat_listing(key, paths)
        if sel is None:
            lo = bisect_left(paths, prefix)
            hi = _prefix_end(paths, prefix, lo)
            sel = list(map(ids.__getitem__, _subtree_order(paths, lo, hi, len(prefix))))
            _nat_keep(key, paths, sel)
        return sel
    # Direct files of the folder: its dir hash within this root.
    dh = path_hash("/" + remote_path.strip("/") if remote_path.strip("/") else "/")
    tids = store.filter_track_ids(dir_hash=dh, scan_root_hash=rh)
    tracks = store._tracks
    rows = sorted(((tracks[t].get("path") or "", t) for t in tids if t in tracks))
    return [t for _p, t in rows]


async def _catalogue_tracks_with_meta(path: str, recursive: bool, offset: int,
                                      limit: int, shuffle_seed: int | None,
                                      playable: bool = False):
    """``tracks-with-meta`` for a catalogue folder: the same response shapes,
    built from the lean cache — only the page's rows are shaped.  Tunes that
    can't play right now are flagged; a shuffle never deals them and a
    ``playable`` page skips them (``core.availability``)."""
    from soniqboom.core import availability
    from soniqboom.core.filesource import find_source_for_path
    from soniqboom.core.store import get_store
    result = find_source_for_path(path)
    if not result:
        raise HTTPException(503, "Network share not connected")
    scan_root, remote_path, _source = result
    store = get_store()
    sel = await asyncio.to_thread(_catalogue_selection, store, scan_root, remote_path,
                                  recursive)
    tracks = store._tracks
    if playable and shuffle_seed is None and limit > 0:
        def _fetch(off: int, lim: int) -> list[dict]:
            return [r for r in (_shape_row(tracks[t]) for t in sel[off:off + lim]
                                if t in tracks) if r]
        return availability.player_page(_fetch, offset, limit, store)
    total = len(sel)
    if shuffle_seed is not None and sel:
        from soniqboom.core import shuffle_order
        seed = shuffle_order.normalize_seed(shuffle_seed)
        key = ("folder", path.rstrip("/") or path, bool(recursive), False, seed, total)
        order = await asyncio.to_thread(shuffle_order.cached_order, key, lambda: list(sel), seed)
        sel = order
    # A shuffle's order is dealt over the whole listing (an availability change
    # never re-deals it mid-session); the tracks that can't play right now are
    # left out page by page — the queue reads on past an empty page.
    skip = availability.unplayable(store) if shuffle_seed is not None else None
    if shuffle_seed is not None and playable and skip and limit > 0:
        # (The queue's page: read on through the order, bounded — see ``_page``.)
        def _fetch_order(off: int, lim: int) -> list[dict]:
            return [r for r in (_shape_row(tracks[t]) for t in sel[off:off + lim]
                                if t in tracks) if r]
        # (``skip`` holds both kinds: the tracks that can't play right now
        # and the files the skip rules pass by.)
        page = availability.playable_page(_fetch_order, offset, limit, None, skip=skip)
        return {"total": total, **page}
    if limit > 0:
        page_ids = sel[offset:offset + limit] if offset < total else []
    else:
        page_ids = sel[:_CAT_UNWINDOWED_MAX]
    rows = [r for r in (_shape_row(tracks[t]) for t in page_ids
                        if t in tracks and not (skip and t in skip)) if r]
    rows = availability.mark_rows(rows, store)
    if limit > 0:
        return {"total": total, "tracks": rows}
    return rows


# Encoded with orjson (``tracks.json_route``), not ``jsonable_encoder`` +
# ``json.dumps``: the rows are already TrackMeta-shaped dicts (or stubs), and the
# old walk cost ~140 ms on the event loop for one 2000-row page.  The function
# still returns the plain list / ``{"total", "tracks"}`` dict to direct callers.
@json_route(router, "/tracks-with-meta")
async def tracks_with_meta(
    path: str = Query(..., description="Absolute directory path"),
    recursive: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
    filter_duplicates: bool | None = Query(None),
    shuffle_seed: int | None = Query(
        None,
        description=(
            "When set, the SAME listing is returned as a seeded pseudo-random "
            "permutation instead of fstree order — the shuffle play queue pages "
            "through it with offset/limit, so shuffle covers the whole folder."
        ),
    ),
    playable: bool = False,
):
    """Hybrid listing: filesystem for file discovery, store for metadata.

    Returns all audio files found on disk.  For each file, if metadata exists
    in the store (file was previously scanned), the full TrackMeta is returned.
    For unscanned files, a minimal stub derived from the filename is returned
    with ``_scanned: false``.

    Pagination (recursive or not): pass ``limit > 0`` to receive a sliced
    response of shape ``{"total": N, "tracks": [...]}`` instead of a plain
    array.  ``offset`` selects the start index.  This lets the frontend
    flatten a 60K-track subtree progressively — the first chunk comes back in
    one request, the rest stream in as the windowed virtual scroll pulls
    further chunks via ``offset`` — and page a single folder that holds
    thousands of tracks directly the same way: the desktop folder view asks
    for every folder windowed.  When ``limit == 0`` (default), the response
    is the plain array for backward compatibility with callers that aren't
    windowed.  (``playable=1`` pages have their own shape — see below.)

    Order: a folder's own tracks (``recursive=false``) in path order — a local,
    network-share or catalogue folder's alike, every one of them (a share's
    from its root's path index: ``_remote_tracks_with_meta``); a folder listed
    by a file-system walk by lower-cased path.  A recursive listing goes
    folder by folder as the folder lists show them (every folder of a row's
    path by ``_folder_sort_key``: CD2 before CD10), a folder's own tracks
    before its subfolders'; the tracks of one folder in path order (a walk's:
    by lower-cased path) — "Folder order of a recursive listing", above.
    Pages, ``playable=1`` pages and their ``_pos`` read that one order; a
    ``shuffle_seed`` permutation depends on the listing's track ids only
    (``shuffle_order.seeded_order``).

    ``filter_duplicates``: list one copy of a track that one folder holds in
    several encodings (members of one duplicate group — audio-fingerprint-
    clustered alternate encodings — side by side; ``_drop_duplicate_alternates``),
    server-side.  A copy whose other copies are in other folders is listed.  When
    OMITTED (the default — ``None``) it is resolved from the
    ``dedup_folders`` config toggle (Settings → "Hide duplicates when
    browsing folders"; default ON — see ``data.folder_dedup_enabled``).  An
    explicit ``true``/``false`` query param overrides the
    config.  Resolving the value server-side keeps the windowed total and
    its chunks consistent — every request reads the same setting, so the
    count can't disagree with the rows.

    Tracks that can't play right now (their drive / share is offline) are
    listed with ``offline`` — a folder view lists every file, so "Tracks on
    unavailable drives: Hide" marks them here too — a shuffle (``shuffle_seed``)
    never deals them, and ``playable=1`` (the play queue's ordered pages, with
    ``limit``) answers ``{tracks, next_offset, ended}`` without them, each row
    carrying its listing position as ``_pos`` (Mark / Hide; ``core.availability``).
    """
    from soniqboom.core import availability
    if filter_duplicates is None:
        from soniqboom.core.data import folder_dedup_enabled
        filter_duplicates = await folder_dedup_enabled()

    async def _page(res: list[dict], stable: bool = True):
        """Paginate ``res`` — in seeded shuffle order when ``shuffle_seed`` is set.

        The reorder is O(listing) per page (plus a hash+sort on the first page of
        a seed), so it runs in a worker thread; ``res`` is a finished list that
        nothing mutates.
        """
        if playable and shuffle_seed is None and limit > 0:
            return availability.player_page(
                lambda off, lim: res[off:off + lim], offset, limit)
        if shuffle_seed is not None and res:
            # The order is dealt over the whole listing (never re-dealt when a
            # drive comes or goes mid-session); the tracks that can't play right
            # now are left out page by page (the queue reads on past a gap).
            skip = availability.unplayable()
            if limit > 0 and playable and skip:
                # The queue's page (``playable=1``): read on through the order
                # (bounded, like an ordered list's) until ``limit`` tracks that
                # can play are found — one request, not one per 50 rows.
                box = {"total": 0}

                def _fetch(off: int, lim: int) -> list[dict]:
                    box["total"], rows = _shuffled_page(
                        res, path, recursive, bool(filter_duplicates), shuffle_seed,
                        off, lim, stable)
                    return rows
                page = await asyncio.to_thread(
                    lambda: availability.playable_page(_fetch, offset, limit, None, skip=skip))
                return {"total": box["total"] or len(res), **page}
            if limit > 0:
                # One page: only ITS rows are materialised (O(limit) once the
                # order and the id→row map are cached), not the whole listing.
                total, rows = await asyncio.to_thread(
                    _shuffled_page, res, path, recursive,
                    bool(filter_duplicates), shuffle_seed, offset, limit, stable)
                if skip:
                    rows = [r for r in rows if r.get("id") not in skip]
                return {"total": total, "tracks": availability.mark_rows(rows)}
            res = await asyncio.to_thread(
                _shuffled_listing, res, path, recursive,
                bool(filter_duplicates), shuffle_seed, stable)
            if skip:
                res = [r for r in res if r.get("id") not in skip]
        return _marked(_maybe_paginate(res, offset, limit, recursive))

    if _is_remote(path):
        _confine_remote(path)
        from soniqboom.core import catalogue
        if catalogue.is_catalogue_path(path):
            return await _catalogue_tracks_with_meta(path, recursive, offset, limit,
                                                     shuffle_seed, playable)
        result = await _remote_tracks_with_meta(path, recursive)
        if filter_duplicates:
            from soniqboom.core import availability
            result = _drop_duplicate_alternates(   # a fresh list per request: nothing to memoise on
                result, availability.unplayable())
        return await _page(result, stable=False)

    p, _root = await _confine_local_async(path)
    # A folder inside an archive (the tree lists ``…/pack.zip::inner``) is no
    # folder on disk: it is listed from the store alone, below.
    in_archive = "::" in str(p)
    if not in_archive and not await asyncio.to_thread(p.is_dir):   # (a stat: off the loop)
        raise HTTPException(404, f"Directory not found: {path}")

    from soniqboom.core.store import get_store
    store = get_store()

    # Fast path for ``recursive=true`` on local paths: use the in-memory
    # store directly instead of walking the FS.  The scanner has already
    # indexed every track under every registered scan root, so the
    # store-side filter (one hash-bucket lookup + a path-prefix string
    # check) is microseconds compared to seconds for ``_discover_audio``
    # on a 60K-file subtree like SID's C64Music.  We fall back to the
    # FS walk only when the store has no tracks under this path — that
    # means the subtree genuinely hasn't been indexed yet (rare in
    # steady state; the watcher + initial scan cover the normal case).
    if recursive:
        store_results = await _store_recursive_tracks_under_async(store, p)
        if store_results is not None and len(store_results) > 0:
            if filter_duplicates:
                store_results = _deduped(store_results)
            return await _page(store_results)
        # Empty store-side result: either the subtree truly has no tracks
        # (and the FS walk will agree, fast) or it's un-indexed.  Either
        # way we fall through to ``_discover_audio`` below for correctness.
    else:
        # Non-recursive store-first: the tracks DIRECTLY inside ``p`` (no
        # subfolder), straight from the RAM index.  A folder can hold thousands
        # of direct files — an archive bucket flattens ~4K module tracks into
        # one directory — and the old path did a live ``_discover_audio`` FS
        # walk (slow on an SMB mount) and returned the whole lot unwindowed.
        # ``_store_recursive_tracks_under`` returns everything under ``p``
        # (folder order: the folder's own tracks first, in path order) and
        # caches it, so the recursive listing a branch folder asks for next
        # is served from it; keep only rows whose path has no further ``/``
        # after the directory prefix (``::`` archive separators are NOT
        # subfolders, so an archive's tracks count as direct).  Only when
        # there are genuinely no indexed direct tracks do we fall through to
        # the FS walk (covers a pure branch folder — the frontend then
        # auto-flattens — and unindexed corners with on-disk-only files).
        store_results = await _store_recursive_tracks_under_async(store, p)
        if store_results is not None and len(store_results) > 0:
            prefix_len = len(str(p).rstrip("/")) + 1
            # Memoised on the (cached) recursive listing, so every page of a big
            # flat folder gets the SAME list back — which is what lets the dedup /
            # id-map memos hit instead of being rebuilt (and evicting the entries
            # that do help) on each 50-row page.
            direct = _memo(_DIRECT_MEMO, store_results, lambda rows: [
                t for t in rows
                if "/" not in t.get("path", "")[prefix_len:]
            ])
            if direct:
                if filter_duplicates:
                    direct = _deduped(direct)
                return await _page(direct)
    if in_archive:
        raise HTTPException(404, f"Directory not found: {path}")   # (nothing indexed in it)
    # NOTE: invalidation key is the directory's OWN mtime — not the
    # store's global ``_mutation_seq``.  The cache's job is to skip
    # re-walking THIS directory when its file set hasn't changed; the
    # dir mtime captures exactly that.  Mixing in the global seq
    # caused a tail wag where ANY mutation anywhere in the store
    # (rating bump on a different track, watcher upsert of a sibling
    # folder, a freshness scan finding new entries elsewhere) flushed
    # every folder's cache and forced a fresh FS walk — explaining the
    # "nothing seems to be cached" symptom (VU-D19).  If a write
    # genuinely changes the file list in this directory the FS mtime
    # bumps and the cache invalidates correctly.

    loop = asyncio.get_running_loop()
    cache_key = (str(p), recursive)
    cached = _TRACKS_META_CACHE.get(cache_key)
    mtime_now = await loop.run_in_executor(None, _dir_mtime, p)

    if (
        cached is not None
        and cached.get("mtime") == mtime_now
        and "results" in cached
    ):
        # Full-response cache hit — skip the discover walk AND the
        # ``get_tracks_batch`` + Pydantic round-trip that follows it.
        # For a 6K-track modarchive subfolder this was 60 ms warm; with
        # the response cached it drops to <1 ms.  Mtime-keyed
        # invalidation still applies: any FS change to this dir flushes
        # the cache, so file additions / removals re-fetch correctly.
        cache_stats.hit("browse")
        results = cached["results"]
    else:
        cache_stats.miss("browse")
        if (
            cached is not None
            and cached.get("mtime") == mtime_now
        ):
            files = cached["files"]
            id_map = cached["id_map"]
        else:
            # _discover_audio does os.walk + zipfile I/O — offload to
            # thread-pool so the event loop isn't blocked while scanning
            # large directories.  UUID generation joins the same thread
            # call so we don't bounce twice through the executor.
            def _discover_and_id() -> tuple[list[Path], dict[str, Path]]:
                files_local = _discover_audio(p, recursive)
                return files_local, _build_id_map(files_local)

            files, id_map = await loop.run_in_executor(None, _discover_and_id)
            if not files:
                _TRACKS_META_CACHE[cache_key] = {
                    "mtime": mtime_now,
                    "files": files,
                    "id_map": id_map,
                    "results": [],
                }
                # Route the empty result through _maybe_paginate so a
                # ``limit > 0`` caller gets the windowed ``{total:0}`` shape
                # consistently — the warm-cache path already does, and the
                # frontend's branch-folder auto-flatten keys on total === 0.
                return await _page([])
            _TRACKS_META_CACHE[cache_key] = {
                "mtime": mtime_now,
                "files": files,
                "id_map": id_map,
            }

        track_ids = list(id_map.keys())

        # Batch-fetch metadata from the in-memory store
        from soniqboom.core.data import get_tracks_batch
        tracks = await get_tracks_batch(track_ids)

        results: list = []
        unscanned: list[tuple[int, Path]] = []
        for tid, track in zip(track_ids, tracks):
            if track is not None:
                d = track.model_dump(exclude={"embedding"})
                d["_scanned"] = True
                results.append(d)
            else:
                unscanned.append((len(results), id_map[tid]))
                results.append(None)
        if unscanned:
            # (a stat per file — on a network drive a round trip each: off the loop)
            stubs = await asyncio.to_thread(lambda: [_make_stub(p) for _i, p in unscanned])
            for (i, _p), stub in zip(unscanned, stubs):
                results[i] = stub

        if recursive:
            # Folder order, like the store-side listing; the walk listed by
            # lower-cased path, which a folder's own listing keeps.
            results = _in_folder_order(results)
        # Stash the fully-shaped result alongside files/id_map so the
        # next revisit can skip the metadata fetch + Pydantic dump too.
        _TRACKS_META_CACHE[cache_key]["results"] = results

    if filter_duplicates:
        results = _deduped(results)
    return await _page(results)


def _shuffled_listing(results: list[dict], path: str, recursive: bool,
                      dedup: bool, seed: int, stable: bool = True) -> list[dict]:
    """``results`` reordered as the seeded shuffle permutation of its track ids.

    Same listing, different order — so a shuffled folder queue honours exactly
    what the folder view shows (unscanned stubs, remote paths, the
    ``dedup_folders`` setting).  The ordered id list is LRU-cached per
    (path, recursive, dedup, seed, listing size).  This FULL reorder is O(listing)
    and only serves un-paginated callers; pages go through ``_shuffled_page``.
    """
    from soniqboom.core import shuffle_order
    seed = shuffle_order.normalize_seed(seed)
    by_id = _by_id(results) if stable else {d.get("id"): d for d in results if d.get("id")}
    key = ("folder", path.rstrip("/") or path, bool(recursive), dedup, seed, len(by_id))
    order = shuffle_order.cached_order(key, lambda: list(by_id.keys()), seed)
    out = [by_id[i] for i in order if i in by_id]
    if len(out) != len(by_id):
        # Same folder + size but different ids (files renamed / swapped since the
        # order was dealt): it no longer covers the listing.  Re-deal from the
        # rows at hand and REPLACE the entry, so later pages hit it again.
        order = shuffle_order.cached_order(key, lambda: list(by_id.keys()), seed,
                                           refresh=True)
        out = [by_id[i] for i in order if i in by_id]
    return out


# Identity memos for per-request O(listing) work.  The LOCAL listings come out of
# caches that hand back the SAME list object until they are rebuilt (and are
# replaced wholesale, never mutated), so ``id(list)`` + an ``is`` check is a safe
# key.  Remote listings are rebuilt on every request — they bypass the memos
# (``stable=False``) rather than churn the slots.  Without them every 50-row page of a 126 K-row folder re-ran the dedup
# pass on the event loop (4.8 ms) and rebuilt the id→row map (11.7 ms).
# Small listings are cheap to redo and an un-cached FS walk makes a fresh list per
# request — memoising those would only pin garbage — so: big lists only, few slots.
_LISTING_MEMO_MAX = 4
_LISTING_MEMO_MIN_ROWS = 5000
_DEDUP_MEMO: "dict[int, tuple[list, list]]" = {}
_DIRECT_MEMO: "dict[int, tuple[list, list]]" = {}
_BYID_MEMO: "dict[int, tuple[list, dict]]" = {}


_MEMO_LOCK = threading.Lock()    # the loop (_deduped) and to_thread workers (_by_id) both come here


def _memo(memo: dict, src: list, build):
    # The entry holds ``src`` itself, so its id() cannot be recycled while it is
    # memoised; the ``is`` check covers an entry that was evicted and re-used.
    with _MEMO_LOCK:
        hit = memo.get(id(src))
        if hit is not None and hit[0] is src:
            memo[id(src)] = memo.pop(id(src))     # LRU: a live listing isn't evicted first
            return hit[1]
    value = build(src)                      # outside the lock: O(listing)
    if len(src) < _LISTING_MEMO_MIN_ROWS:
        return value
    with _MEMO_LOCK:
        while len(memo) >= _LISTING_MEMO_MAX:
            memo.pop(next(iter(memo)))
        memo[id(src)] = (src, value)
    return value


def _marked(out):
    """A listing / page with the tracks that can't play right now flagged
    (``offline``) — copies of those rows only; the cached listing is never
    touched."""
    from soniqboom.core import availability
    if isinstance(out, dict):
        rows = out.get("tracks")
        if isinstance(rows, list):
            return {**out, "tracks": availability.mark_rows(rows)}
        return out
    if isinstance(out, list):
        return availability.mark_rows(out)
    return out


def _deduped(results: list[dict]) -> list[dict]:
    """``_drop_duplicate_alternates`` memoised per listing object: the listing's
    contested copies (``_dedup_plan``) are worked out once, and the result is
    kept while the ones among them that can't play (``availability.unplayable``,
    compared by content) stay the same — the memo returns the same deduped LIST
    each time, which keeps ``_by_id`` warm too."""
    from soniqboom.core import availability
    skip = availability.unplayable()

    def _build(rows: list[dict]) -> dict:
        plan = _dedup_plan(rows)
        ids = frozenset(r.get("id") for same in plan for r in same)
        rel = (ids & skip) if (skip and ids) else frozenset()
        return {"plan": plan, "ids": ids, "rel": rel,
                "out": _dedup_apply(rows, plan, rel)}

    entry = _memo(_DEDUP_MEMO, results, _build)
    rel = (entry["ids"] & skip) if (skip and entry["ids"]) else frozenset()
    if rel == entry["rel"]:
        return entry["out"]
    out = _dedup_apply(results, entry["plan"], rel)       # (what can play changed)
    with _MEMO_LOCK:
        hit = _DEDUP_MEMO.get(id(results))
        if hit is not None and hit[0] is results:
            _DEDUP_MEMO[id(results)] = (results, {**entry, "rel": rel, "out": out})
    return out


def _by_id(results: list[dict]) -> dict:
    # Every listing row carries an id (scanned tracks their store id, unscanned
    # stubs ``_track_id(path)``), so nothing is dropped here.
    return _memo(_BYID_MEMO, results,
                 lambda rows: {d.get("id"): d for d in rows if d.get("id")})


def _shuffled_page(results: list[dict], path: str, recursive: bool, dedup: bool,
                   seed: int, offset: int, limit: int,
                   stable: bool = True) -> tuple[int, list[dict]]:
    """One page of the seeded shuffle of ``results`` → ``(total, rows)``.

    Same order and cache entry as :func:`_shuffled_listing`; only the requested
    slice is turned into rows.  A cached order that no longer covers the listing
    (a page id is gone: files renamed / swapped under the same size) is re-dealt
    and replaced.
    """
    from soniqboom.core import shuffle_order
    seed = shuffle_order.normalize_seed(seed)
    by_id = _by_id(results) if stable else {d.get("id"): d for d in results if d.get("id")}
    key = ("folder", path.rstrip("/") or path, bool(recursive), dedup, seed, len(by_id))
    order = shuffle_order.cached_order(key, lambda: list(by_id.keys()), seed)
    page = order[offset: offset + limit]
    # (The listing SIZE is part of the key, so a cached order always has the right
    # length; what can drift under it is WHICH ids — files renamed / swapped.)
    if any(i not in by_id for i in page):
        order = shuffle_order.cached_order(key, lambda: list(by_id.keys()), seed,
                                           refresh=True)
        page = order[offset: offset + limit]
    return len(order), [by_id[i] for i in page if i in by_id]


def _drop_duplicate_alternates(results: list[dict], skip=None) -> list[dict]:
    """The listing with the lesser copies of a track left out that sit in the
    SAME folder as a better one (one track in several encodings side by side:
    ``x.flac`` and ``x.mp3``).  Of a duplicate group's members in one folder
    the best is kept, in its place: a copy that can play (not in ``skip``,
    ``availability.unplayable``) before one that can't, then the best by
    ``duplicates.primary_sort_key`` (quality, bitrate — not the library-wide
    primary: that may be a copy in another folder, or one picked by hand).
    Another member there is left out when it can't play while the kept one
    can, or is another format, or scores lower
    (``duplicates.format_quality_score``) — two playable files of one format
    and score (a track an album holds twice) are both listed.  A copy whose
    other copies are all in other folders (another album, folder, archive or
    share — also inside a recursive listing) is always listed.

    Resolving it server-side lets windowed clients trust ``total`` to match
    what they'll actually receive across all chunks.
    """
    plan = _dedup_plan(results)
    if not plan:
        return list(results)
    return _dedup_apply(results, plan, skip)


def _row_folder(p: str) -> str:
    """The folder a listing row sits in: up to its last ``/`` — or its archive
    (``…/a.zip::tune.mod`` → ``…/a.zip``)."""
    return p[:max(p.rfind("/"), p.rfind("::"), 0)]


def _dedup_plan(results: list[dict]) -> list[list[dict]]:
    """The listing's contested copies: each duplicate group's members that
    share a folder, two or more of them (``_drop_duplicate_alternates``)."""
    by_group: dict = {}
    multi = False
    for d in results:
        g = d.get("duplicate_group_id")
        if g:
            rows = by_group.get(g)
            if rows is None:
                by_group[g] = d                 # (one member: the common case, no list)
            else:
                if type(rows) is dict:
                    by_group[g] = rows = [rows]
                rows.append(d)
                multi = True
    if not multi:
        return []
    plan: list[list[dict]] = []
    for rows in by_group.values():
        if type(rows) is dict:
            continue
        by_dir: dict[str, list[dict]] = {}
        for r in rows:
            by_dir.setdefault(_row_folder(str(r.get("path") or "")), []).append(r)
        plan.extend(same for same in by_dir.values() if len(same) > 1)
    return plan


def _dedup_apply(results: list[dict], plan: list[list[dict]], skip) -> list[dict]:
    """``results`` without the members of ``plan``'s folders a better copy
    beside them stands in for (``_drop_duplicate_alternates``)."""
    from soniqboom.core.duplicates import format_quality_score, primary_sort_key

    def _grade(d: dict) -> tuple:
        return ((d.get("format") or "").upper(),
                format_quality_score(d.get("format", ""), d.get("bitrate")))

    drop: set[int] = set()
    for same in plan:
        best = min(same, key=lambda r: (bool(skip) and r.get("id") in skip,
                                        primary_sort_key(r)))
        best_off = bool(skip) and best.get("id") in skip
        fmt, score = _grade(best)
        for r in same:
            if r is best:
                continue
            rf, rs = _grade(r)
            off = bool(skip) and r.get("id") in skip
            if (off and not best_off) or rf != fmt or rs < score:
                drop.add(id(r))
    if not drop:
        return list(results)
    return [d for d in results if id(d) not in drop]


def _maybe_paginate(results: list[dict], offset: int, limit: int, recursive: bool):
    """Apply ``offset``/``limit`` to a results list for the windowed-fetch path.

    Returns the plain array (legacy shape) when pagination isn't requested
    (``limit == 0``).  This is what keeps every non-windowed caller working —
    they pass no ``limit`` and get the bare array.

    A caller that DOES pass ``limit > 0`` gets the windowed
    ``{"total": N, "tracks": [...]}`` shape so it can size its windowed store
    from ``total`` and pull further chunks by ``offset`` without re-counting.
    This now applies to non-recursive listings too: a single folder can still
    hold thousands of tracks directly (e.g. an archive bucket that flattens
    ~4K module files into one directory), so shallow ≠ small.  The ``recursive``
    parameter is retained for call-site compatibility but no longer gates the
    shape — ``limit`` alone decides it.
    """
    if limit <= 0:
        return results
    total = len(results)
    end = min(offset + limit, total)
    if offset >= total:
        return {"total": total, "tracks": []}
    return {"total": total, "tracks": results[offset:end]}


def _own_rows(paths: list[str], dicts: list[dict], prefix: str,
              lo: int, hi: int) -> list[dict]:
    """The folder's own rows among ``lo … hi - 1`` of path-sorted ``paths``
    (``dicts`` alongside) — the run under the folder whose prefix, with its
    trailing "/", is ``prefix`` — in path order: the rows ``_folder_order``
    lists first, with nothing between the prefix and the row's last "/".  An
    archive's top-level members (``pack.zip::c.mod``) are the folder's own, a
    folder inside an archive (``pack.zip::inner``) is a subfolder, and a lone
    "/" right after the prefix (``Box//x.mod``: an old build's SMB paths,
    ``scanner._healed_smb_rel``) leaves a row in the folder its scan filed it
    under — but such a path a level deeper (``Box//sub/x.mod``, filed under
    ``Box/sub``) is no folder's own row.  Each subfolder's rows are skipped in
    one bisect: a folder costs its own rows and a step per subfolder, not its
    whole subtree."""
    plen = len(prefix)
    out: list[dict] = []
    add = out.append
    i = lo
    while i < hi:
        p = paths[i]
        cut = p.find("/", plen)
        if cut < 0 or (cut == plen and p.find("/", plen + 1) < 0):
            add(dicts[i])
            i += 1
        elif cut > plen:
            i = _prefix_end(paths, p[:cut + 1], i)    # past subfolder ``p[plen:cut]``
        else:
            i += 1                                    # (``Box//sub/x.mod``: not the folder's)
    return out


async def _remote_tracks_with_meta(path: str, recursive: bool) -> list[dict]:
    """The indexed tracks of a network-share folder (smb / ftp / WebDAV): every
    track under it (``recursive``) or its own, both from the scan root's sorted
    path index (``_get_or_build_scan_root_sorted``: the share's tracks, stored
    as ``{scan_root}:/{rel}``, shaped once) by a bisect for the folder's
    ``{scan_root}:/{rel}/`` prefix: unbounded, and independent of the root's
    size.

    * ``recursive``: every row under the prefix, in folder order
      (``_subtree_order`` — "Folder order of a recursive listing"), kept per
      folder and index version from ``_NAT_MIN_ROWS`` rows (``_NAT_LISTINGS``:
      the caller must not modify the list).
    * otherwise the folder's own rows, in path order (``_own_rows``): the rows
      its recursive listing starts with — for every path a share scan writes,
      the rows it filed under the folder (``dir_hash``: the parent folder of
      the file's share path).  An old build's SMB path a level below a doubled
      "/" (``Music//deep/x.mod``, ``scanner._healed_smb_rel``) is no folder's
      own row, only in the recursive listings, until a walk heals it."""
    from soniqboom.core.data import path_hash
    from soniqboom.core.filesource import find_source_for_path
    from soniqboom.core.store import get_store

    result = find_source_for_path(path)
    if not result:
        raise HTTPException(503, "Network share not connected")
    scan_root, remote_path, _source = result

    store = get_store()
    rh = path_hash(scan_root)

    if recursive:
        # Collect EVERY indexed track under the folder from the scan-root's
        # sorted-path cache (already TrackMeta-shaped) via a bisect over the
        # ``{scan_root}:/{rel}/`` prefix — the same O(log n + k) walk the
        # ``_remote_children_from_store`` / ``_subtree_has_indexed_audio``
        # helpers use.  The old ``filter_tracks(scan_root_hash, limit=10000)``
        # + Python prefix-filter fetched only the FIRST 10 000 tracks of the
        # whole scan root and filtered those, so a folder whose tracks fell
        # outside that window (e.g. an artist deep in a full iTunes library >
        # 10 000 tracks) came back EMPTY — the parent folder showed "No tracks
        # found" while its leaf album folders, queried directly by dir_hash
        # then, still worked.  The bisect is unbounded and scan-root-size
        # independent.  The rows (already model-dumped + ``_scanned``) are put
        # in folder order (``_subtree_order``) once per folder and index
        # version, kept in ``_NAT_LISTINGS`` — the caller must not modify it.
        sorted_paths, sorted_dicts = _get_or_build_scan_root_sorted(store, rh)
        rel = remote_path.strip("/")
        prefix = f"{scan_root}:/" + (f"{rel}/" if rel else "")
        key = ("rows", rh, prefix)
        out = _nat_listing(key, sorted_paths)
        if out is None:
            lo = bisect_left(sorted_paths, prefix)
            hi = _prefix_end(sorted_paths, prefix, lo)

            def _ordered() -> list[dict]:
                # (only reads the index lists: safe in a worker thread)
                return list(map(sorted_dicts.__getitem__,
                                _subtree_order(sorted_paths, lo, hi, len(prefix))))
            out = (await asyncio.to_thread(_ordered) if hi - lo >= _ORDER_IN_THREAD_ROWS
                   else _ordered())
            _nat_keep(key, sorted_paths, out)
        return out

    # Non-recursive: the folder's own rows, from the same index (until
    # 2026-11-24 ``filter_tracks(dir_hash, limit=5000)``: newest first, and cut
    # at 5,000 rows).  Built per request, not kept: an append per own row and a
    # bisect per subfolder (measured on a 106K-row index: 0.6 ms for 6,000 own
    # rows, 1.6 ms for 2,000 subfolders) — the ``_NAT_LISTINGS`` slots stay
    # with the recursive listings, which cost an ordering.
    sorted_paths, sorted_dicts = _get_or_build_scan_root_sorted(store, rh)
    rel = remote_path.strip("/")
    prefix = f"{scan_root}:/" + (f"{rel}/" if rel else "")
    lo = bisect_left(sorted_paths, prefix)
    return _own_rows(sorted_paths, sorted_dicts, prefix, lo,
                     _prefix_end(sorted_paths, prefix, lo))
