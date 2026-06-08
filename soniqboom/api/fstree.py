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
import uuid
import zipfile
from pathlib import Path

import asyncio

from fastapi import APIRouter, HTTPException, Query

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
_BROWSE_CACHE_VERSION = 1


def _is_remote(path: str) -> bool:
    return path.startswith(("smb://", "ftp://"))


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

    Walks every LOCAL scan root that contains ``path``, then bisects each
    root's warm sorted-by-path cache for the first entry ``>=`` ``path/``.
    If that entry still has ``path/`` as a prefix, there's at least one
    indexed track somewhere under ``path``.

    Returns True (don't hide) when the path is outside every local scan
    root — we can't be sure what's there, and silently swallowing
    folders the user can still navigate to is more surprising than
    leaving them visible.  Remote paths likewise return True because the
    store-side per-scan-root index doesn't currently cover them in the
    same shape; the remote check would need a separate code path.
    """
    from bisect import bisect_left
    from soniqboom.core.data import path_hash

    p_str = str(path)
    if _is_remote(p_str):
        # No store-side index in the same shape for remote shares.
        return True

    ancestor_hashes: list[str] = []
    for sd in store.list_scan_dirs():
        root_path = sd.get("path", "")
        if not root_path or _is_remote(root_path):
            continue
        root_clean = root_path.rstrip("/")
        if p_str == root_clean or p_str.startswith(root_clean + "/"):
            h = sd.get("path_hash") or path_hash(root_path)
            if h:
                ancestor_hashes.append(h)

    if not ancestor_hashes:
        # Path isn't under any registered local scan root — treat as
        # "no idea, show it".  Hiding it would silently amputate parts
        # of the tree the user can still navigate to via folder aliases.
        return True

    prefix = p_str.rstrip("/") + "/"
    for h in ancestor_hashes:
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

    sorted_paths, _dicts = _get_or_build_scan_root_sorted(store, path_hash(scan_root))
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
    """Return immediate subdirectories of path, sorted alphabetically."""
    try:
        dirs = sorted(
            (p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
    except PermissionError:
        return []
    return [_dir_node(d, root) for d in dirs]


def _remote_list_children(scan_root: str, remote_path: str, source) -> list[dict]:
    """List subdirectories of a remote path via FileSource."""
    try:
        entries = source.list_dir(remote_path)
    except Exception:
        return []
    dirs = sorted(
        [e for e in entries if e.is_dir and not e.name.startswith(".")],
        key=lambda e: e.name.lower(),
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
    root: str = Query(..., description="Scan root this path belongs to"),
):
    """Return immediate subdirectories of *path* (lazy expansion).

    When the ``hide_empty_folders`` config flag is set, subdirectories
    whose entire subtree contains zero indexed audio tracks are dropped
    from the response — useful when a scan root is shared with non-audio
    content (photo dumps, build directories, video files) and the user
    just wants to navigate music.
    """
    if _is_remote(path):
        from soniqboom.core.filesource import find_source_for_path
        result = find_source_for_path(path)
        if not result:
            raise HTTPException(503, "Network share not connected")
        scan_root, remote_path, source = result
        loop = asyncio.get_running_loop()
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

    p = Path(path).resolve()
    r = Path(root).resolve()

    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"Directory not found: {path}")

    # Safety: path must be inside root
    try:
        p.relative_to(r)
    except ValueError:
        raise HTTPException(403, "Path is outside scan root")

    # iterdir + per-dir _has_audio scans are blocking FS calls
    loop = asyncio.get_running_loop()
    children = await loop.run_in_executor(None, _children, p, r)

    # Optional filter: drop subfolders with no indexed audio anywhere
    # under them.  Cheap because ``_subtree_has_indexed_audio`` is a
    # single bisect on the warm sorted scan-root cache.  Config is
    # read once per request, not per child, so the lookup overhead is
    # also O(1) amortised across the children list.
    from soniqboom.core.data import get_config as _get_config
    hide_empty = bool(await _get_config("hide_empty_folders", False))
    if hide_empty and children:
        from soniqboom.core.store import get_store
        store = get_store()
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

    try:
        p = Path(raw).resolve()
    except OSError:
        raise HTTPException(404, f"Path not found: {raw}")
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"Directory not found: {raw}")

    # Confine to registered scan roots — refusing arbitrary paths
    # closes a potential FS-walk DoS vector AND scopes the scan to
    # what's actually part of the library.
    from soniqboom.core.store import get_store
    store = get_store()
    scan_dirs = [Path(sd["path"]).resolve()
                 for sd in store.list_scan_dirs()
                 if not _is_remote(sd["path"])]
    inside_root = False
    for root in scan_dirs:
        try:
            p.relative_to(root)
            inside_root = True
            break
        except ValueError:
            continue
    if not inside_root:
        raise HTTPException(403, "Path is not under any registered scan root")

    # DISABLED pending redesign.  This routed freshness through
    # ``start_scan([folder])``, but ``_run_scan`` treats its argument as a scan
    # ROOT: it calls ``upsert_scan_dir(folder)`` (so every browsed folder wrongly
    # appeared as a top-level entry in the FOLDERS tree) and re-associates that
    # subtree's tracks to the folder as a new root — polluting the tree and
    # churning the store on every navigation.  Drill-down freshness needs a
    # dedicated "rescan a subtree, keep it under its EXISTING root, register
    # nothing" path; until that exists this endpoint is a safe no-op.
    return {"queued": False, "reason": "drill-down refresh disabled pending redesign"}


@router.get("/tracks")
async def tracks_in_dir(
    path: str = Query(..., description="Absolute directory path"),
    recursive: bool = Query(False),
):
    """Return track paths found directly in *path* (or recursively)."""
    p = Path(path).resolve()
    if not p.exists() or not p.is_dir():
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
    results: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                if _is_junk_filename(_zip_basename(member)):
                    continue
                if _is_audio(member):
                    results.append(Path(f"{zip_path}::{member}"))
                elif member.lower().endswith(".zip"):
                    # Nested ZIP (e.g. modarchive: outer.zip -> track.it.zip -> track.it)
                    try:
                        inner_data = zf.read(member)
                        with zipfile.ZipFile(io.BytesIO(inner_data), "r") as inner_zf:
                            for inner_name in inner_zf.namelist():
                                if _is_junk_filename(_zip_basename(inner_name)):
                                    continue
                                if _is_audio(inner_name):
                                    results.append(Path(f"{zip_path}::{member}::{inner_name}"))
                    except (zipfile.BadZipFile, OSError):
                        pass
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
# C64Music's 56K SID tracks, ~2.6 s for modarchive's 111K tracks) but the
# user clicks "Show all tracks recursively" once per visit, and revisiting
# the same root would otherwise pay that cost again.  Caching here turns
# revisits into sub-millisecond responses.
#
# Invalidation policy: the cached entry is keyed by ``(absolute path)`` for
# ``recursive=True`` and validated against the *sizes* of every ancestor
# scan-root bucket the helper visited.  When a scanner walk adds or removes
# tracks under one of those roots, the bucket size changes and we
# recompute.  Rating bumps / play-count increments / sibling-folder writes
# do NOT change bucket sizes, so they correctly don't invalidate.  This
# avoids both the tail-wag of ``_mutation_seq`` (VU-D19) and the
# false-positives of pure directory mtime (which doesn't bump when the
# scanner upserts an existing path).
_STORE_RECURSIVE_CACHE: dict[str, dict] = {}


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

    1. Find every local scan root that contains ``p``.  We union the
       buckets of ALL ancestors rather than picking one because nested
       scan roots are allowed (e.g. ``/.../C64Music`` and
       ``/.../C64Music/DEMOS`` may both be registered): a track gets
       tagged with whichever root last upserted it (see scanner
       ``build_track``), so picking only the deepest or only the
       shallowest can miss tracks that were tagged with the other.
    2. ``get_track_ids_for_scan_root`` returns the scan-root's tid set
       in O(1) (straight from ``_tag_scan_root_hash``); ``get_tracks_batch``
       then does direct dict lookups.  We deliberately avoid
       ``filter_tracks`` here because its sorted-index walk would
       traverse the entire library to filter back down to one scan
       root, costing ~270K iterations to find ~60K tracks.
    3. Filter by string-prefix on the track's stored ``path`` so only
       descendants of ``p`` come back.
    4. Shape each row through ``TrackMeta`` so the response matches what
       ``tracks_with_meta`` returns for scanned tracks.

    Returns
    -------
    None
        ``p`` is not under any local scan root — caller should fall back
        to ``_discover_audio``.
    list[dict]
        TrackMeta-shaped dicts with ``_scanned: True``.  May be empty
        when at least one scan root matches but no indexed tracks live
        under ``p`` — caller treats empty as "fall through to FS walk
        for correctness".
    """
    from soniqboom.core.data import path_hash

    p_str = str(p)

    # Collect every local scan root that contains ``p``.  Skip remote
    # roots (smb://, ftp://) — their tracks have a different path
    # encoding and are handled by ``_remote_tracks_with_meta``.
    ancestor_hashes: list[str] = []
    for sd in store.list_scan_dirs():
        root_path = sd.get("path", "")
        if not root_path or _is_remote(root_path):
            continue
        root_clean = root_path.rstrip("/")
        if p_str == root_clean or p_str.startswith(root_clean + "/"):
            h = sd.get("path_hash") or path_hash(root_path)
            if h:
                ancestor_hashes.append(h)

    if not ancestor_hashes:
        return None

    # ── Cache check ──────────────────────────────────────────────────
    # Use each ancestor scan-root's bucket size as the validity
    # fingerprint.  Adding/removing tracks under one of these roots
    # bumps the count; rating bumps / play-count writes / unrelated
    # scans do not.
    bucket_sizes = tuple(
        len(store.get_track_ids_for_scan_root(h)) for h in ancestor_hashes
    )
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
    seen_ids: set[str] = set()
    results: list[dict] = []

    from bisect import bisect_left

    for h in ancestor_hashes:
        # Per-scan-root sorted cache: build once, reuse for every path
        # under the same root.  Without this, every NEW subfolder click
        # paid the O(bucket-size) iterate + shape cost again (1.1 s for
        # SID's 56K bucket, even when the result was a 95-track composer
        # subfolder).  With it, the first click warms the sorted list,
        # and every subsequent click — for any other subfolder — does an
        # O(log N) bisect + O(matches) walk.
        sorted_paths, sorted_dicts = _get_or_build_scan_root_sorted(store, h)
        if not sorted_paths:
            continue
        start = bisect_left(sorted_paths, prefix)
        n = len(sorted_paths)
        # Walk forward while paths share the prefix.
        i = start
        while i < n and sorted_paths[i].startswith(prefix):
            d = sorted_dicts[i]
            tid = d.get("id")
            if tid not in seen_ids:
                seen_ids.add(tid)
                results.append(d)
            i += 1
        # Edge case: a track whose stored path equals ``p_norm`` itself
        # (rare — a directory-shaped track path) would sit just BEFORE
        # ``start`` because ``prefix`` adds a trailing slash.
        if start > 0 and sorted_paths[start - 1] == p_norm:
            d = sorted_dicts[start - 1]
            tid = d.get("id")
            if tid not in seen_ids:
                seen_ids.add(tid)
                results.append(d)
    # Cache for revisits — invalidation is via the recorded bucket sizes,
    # so a subsequent scanner walk that adds/removes tracks under any of
    # the ancestor roots flips one of the sizes and forces a recompute.
    _STORE_RECURSIVE_CACHE[p_str] = {
        "bucket_sizes": bucket_sizes,
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

    Skips remote scan roots: ``_remote_tracks_with_meta`` is the path
    for those (uses ``store.filter_tracks`` directly, no per-scan-root
    sorted index needed), and we'd be paying the shape cost without any
    reuse benefit.

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
        # 2. Seed the per-path recursive cache for the scan root itself.
        #    Always cheap once the scan-root sorted cache is hot.
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

    Returns ``([], [])`` when the bucket is empty.
    """
    tids_set = store.get_track_ids_for_scan_root(scan_root_hash)
    current_size = len(tids_set)
    cached = _SCAN_ROOT_FULL_CACHE.get(scan_root_hash)
    if cached is not None and cached.get("size") == current_size:
        cache_stats.hit("scan_root")
        return cached["paths"], cached["dicts"]
    cache_stats.miss("scan_root")
    if not tids_set:
        _SCAN_ROOT_FULL_CACHE[scan_root_hash] = {
            "size": 0, "paths": [], "dicts": [],
        }
        return [], []

    raw_dicts = [d for d in store.get_tracks_batch(list(tids_set)) if d]
    shaped: list[tuple[str, dict]] = []
    for d in raw_dicts:
        try:
            filtered = {
                k: v for k, v in d.items()
                if k in TrackMeta.model_fields and k != "embedding"
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
    _SCAN_ROOT_FULL_CACHE[scan_root_hash] = {
        "size": current_size, "paths": paths, "dicts": dicts,
    }
    return paths, dicts


@router.get("/tracks-with-meta")
async def tracks_with_meta(
    path: str = Query(..., description="Absolute directory path"),
    recursive: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
    filter_duplicates: bool | None = Query(None),
):
    """Hybrid listing: filesystem for file discovery, store for metadata.

    Returns all audio files found on disk.  For each file, if metadata exists
    in the store (file was previously scanned), the full TrackMeta is returned.
    For unscanned files, a minimal stub derived from the filename is returned
    with ``_scanned: false``.

    Pagination (``recursive=true`` only): pass ``limit > 0`` to receive a
    sliced response of shape ``{"total": N, "tracks": [...]}`` instead of a
    plain array.  ``offset`` selects the start index.  This lets the frontend
    flatten a 60K-track subtree progressively — first 500 land in <200 ms
    perceived, the rest stream in as the windowed virtual scroll pulls
    further chunks via ``offset``.  When ``limit == 0`` (default), the
    response is the plain array for backward compatibility with callers
    that aren't windowed.

    ``filter_duplicates``: drop non-primary members of duplicate groups
    (audio-fingerprint-clustered alternate encodings) server-side.  When
    OMITTED (the default — ``None``) it is resolved from the
    ``dedup_folders`` config toggle (Settings → "Hide duplicates when
    browsing folders"; default off, so folder views show every file on
    disk).  An explicit ``true``/``false`` query param overrides the
    config.  Resolving the value server-side keeps the windowed total and
    its chunks consistent — every request reads the same setting, so the
    count can't disagree with the rows.
    """
    if filter_duplicates is None:
        from soniqboom.core.data import get_config as _get_config
        filter_duplicates = bool(await _get_config("dedup_folders", False))

    if _is_remote(path):
        result = await _remote_tracks_with_meta(path, recursive)
        if filter_duplicates:
            result = _drop_duplicate_alternates(result)
        return _maybe_paginate(result, offset, limit, recursive)

    p = Path(path).resolve()
    if not p.exists() or not p.is_dir():
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
        store_results = _store_recursive_tracks_under(store, p)
        if store_results is not None and len(store_results) > 0:
            if filter_duplicates:
                store_results = _drop_duplicate_alternates(store_results)
            return _maybe_paginate(store_results, offset, limit, recursive)
        # Empty store-side result: either the subtree truly has no tracks
        # (and the FS walk will agree, fast) or it's un-indexed.  Either
        # way we fall through to ``_discover_audio`` below for correctness.
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
                return []
            _TRACKS_META_CACHE[cache_key] = {
                "mtime": mtime_now,
                "files": files,
                "id_map": id_map,
            }

        track_ids = list(id_map.keys())

        # Batch-fetch metadata from the in-memory store
        from soniqboom.core.data import get_tracks_batch
        tracks = await get_tracks_batch(track_ids)

        results: list[dict] = []
        for tid, track in zip(track_ids, tracks):
            if track is not None:
                d = track.model_dump(exclude={"embedding"})
                d["_scanned"] = True
                results.append(d)
            else:
                results.append(_make_stub(id_map[tid]))

        # Stash the fully-shaped result alongside files/id_map so the
        # next revisit can skip the metadata fetch + Pydantic dump too.
        _TRACKS_META_CACHE[cache_key]["results"] = results

    if filter_duplicates:
        results = _drop_duplicate_alternates(results)
    return _maybe_paginate(results, offset, limit, recursive)


def _drop_duplicate_alternates(results: list[dict]) -> list[dict]:
    """Drop non-primary members of audio-fingerprint duplicate groups.

    Frontend's old library.js filter was
    ``!t.duplicate_group_id || t.is_duplicate_primary !== false`` —
    i.e. keep tracks that are not in a duplicate group OR are the
    primary.  Replicating it server-side lets windowed clients trust
    ``total`` to match what they'll actually receive across all chunks
    (otherwise the count would over-report and the last chunk would
    come back short of ``limit`` for confusing reasons).
    """
    out: list[dict] = []
    for d in results:
        if d.get("duplicate_group_id") and d.get("is_duplicate_primary") is False:
            continue
        out.append(d)
    return out


def _maybe_paginate(results: list[dict], offset: int, limit: int, recursive: bool):
    """Apply ``offset``/``limit`` to a results list for the windowed-fetch path.

    Returns the plain array (legacy shape) when pagination isn't requested
    — ``limit == 0`` — OR when ``recursive`` is false (shallow listings are
    small by construction and don't benefit from windowing; keeping the
    response shape stable for those callers avoids churning every
    non-windowed code path).

    For paginated requests returns ``{"total": N, "tracks": [...]}`` so the
    frontend can size its windowed store from ``total`` and then pull
    additional chunks at higher offsets without re-fetching the count.  The
    ``recursive`` gate makes the response shape predictable per query:
    callers that pass ``limit > 0`` ALWAYS get the windowed shape when
    ``recursive`` is true.
    """
    if not recursive or limit <= 0:
        return results
    total = len(results)
    end = min(offset + limit, total)
    if offset >= total:
        return {"total": total, "tracks": []}
    return {"total": total, "tracks": results[offset:end]}


async def _remote_tracks_with_meta(path: str, recursive: bool) -> list[dict]:
    """Return scanned tracks for a remote directory from the store."""
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
        dicts = store.filter_tracks(scan_root_hash=rh, limit=10000)
        prefix = remote_path.rstrip("/")
        if prefix != "/":
            dicts = [
                d for d in dicts
                if d.get("path", "").startswith(f"{scan_root}:{prefix}/")
            ]
    else:
        dh = path_hash(remote_path)
        dicts = store.filter_tracks(dir_hash=dh, scan_root_hash=rh, limit=5000)

    results: list[dict] = []
    for d in dicts:
        try:
            filtered = {
                k: v for k, v in d.items()
                if k in TrackMeta.model_fields and k != "embedding"
            }
            meta = TrackMeta(**filtered)
            out = meta.model_dump(exclude={"embedding"})
            out["_scanned"] = True
            results.append(out)
        except Exception:
            continue
    return results
