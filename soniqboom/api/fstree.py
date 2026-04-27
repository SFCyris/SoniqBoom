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
import os
import uuid
import zipfile
from pathlib import Path

import asyncio

from fastapi import APIRouter, HTTPException, Query

from soniqboom.core.metadata import FORMAT_NAMES, SUPPORTED_EXTENSIONS
from soniqboom.core.scanner import _is_junk_filename
from soniqboom.models.track import TrackMeta

router = APIRouter(prefix="/fstree", tags=["fstree"])


def _is_remote(path: str) -> bool:
    return path.startswith(("smb://", "ftp://"))


def _has_audio(path: Path) -> bool:
    """Return True if the directory contains at least one supported audio file (shallow)."""
    try:
        return any(
            f.suffix.lower() in SUPPORTED_EXTENSIONS
            for f in path.iterdir()
            if f.is_file() and not _is_junk_filename(f.name)
        )
    except PermissionError:
        return False


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
    """Return immediate subdirectories of *path* (lazy expansion)."""
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
    return {"path": str(p), "children": children}


@router.get("/tracks")
async def tracks_in_dir(
    path: str = Query(..., description="Absolute directory path"),
    recursive: bool = Query(False),
):
    """Return track paths found directly in *path* (or recursively)."""
    p = Path(path).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"Directory not found: {path}")

    def _list_files() -> list[Path]:
        result: list[Path] = []
        if recursive:
            for ext in SUPPORTED_EXTENSIONS:
                result.extend(p.rglob(f"*{ext}"))
        else:
            result = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
        result = [f for f in result if not _is_junk_filename(f.name)]
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


@router.get("/tracks-with-meta")
async def tracks_with_meta(
    path: str = Query(..., description="Absolute directory path"),
    recursive: bool = Query(False),
):
    """Hybrid listing: filesystem for file discovery, store for metadata.

    Returns all audio files found on disk.  For each file, if metadata exists
    in the store (file was previously scanned), the full TrackMeta is returned.
    For unscanned files, a minimal stub derived from the filename is returned
    with ``_scanned: false``.
    """
    if _is_remote(path):
        return await _remote_tracks_with_meta(path, recursive)

    p = Path(path).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"Directory not found: {path}")

    # _discover_audio does os.walk + zipfile I/O — offload to thread-pool so
    # the event loop isn't blocked while scanning large directories.
    loop = asyncio.get_running_loop()
    files = await loop.run_in_executor(None, _discover_audio, p, recursive)
    if not files:
        return []

    # Build ID map — same deterministic uuid5 the scanner uses
    id_map: dict[str, Path] = {_track_id(f): f for f in files}
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

    return results


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
