# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Async directory scanner — discovers audio files, extracts metadata, upserts to store.

Design
──────
Extract metadata in parallel using a **ProcessPoolExecutor** so each worker
gets its own GIL and cannot block the main event loop.  Results are written
to the in-memory store in small chunked batches with ``asyncio.sleep(0)``
yields between chunks, keeping the API and WebSocket fully responsive during
even large scans.

Waveforms are generated on-demand (lazy) when first requested via the
tracks API, not during scanning.

Non-blocking notes
──────────────────
• Metadata extraction runs in **separate processes** (ProcessPoolExecutor)
  — ZIP decompression and mutagen parsing never compete with the event loop.
• Store writes go through upsert_tracks_batch() in sub-batches of
  WRITE_CHUNK tracks, with asyncio.sleep(0) between each chunk.
• The processing loop yields to the event loop every YIELD_EVERY results.
• A local hash cache avoids re-computing deterministic dir/root hashes.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import math
import os
import struct
import subprocess
import time
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from soniqboom.core.metadata import SUPPORTED_EXTENSIONS, extract
from soniqboom.core.art_cache import store_full_art_batch, store_thumbs_batch
from soniqboom.core.data import (
    delete_track_ids,
    get_track_ids_for_scan_root,
    get_tracks_batch,
    path_hash,
    scan_all_tracks_meta,
    store_hash_lookups_batch,
    upsert_scan_dir,
    upsert_tracks_batch,
)
from soniqboom.core.store import get_store
from soniqboom.models.track import Track, TrackMeta

log = logging.getLogger(__name__)

# ── Tuning knobs ──────────────────────────────────────────────────────────────
SCAN_WORKERS   = 8       # worker *processes* (each has its own GIL)
                         # 8 gives good concurrency for network-mounted I/O
WRITE_BATCH    = 500     # tracks buffered before a store flush
WRITE_CHUNK    = 25      # sub-batch size within a flush (yield between chunks)
INFLIGHT       = 200     # max futures in the asyncio.wait window (keeps wait() O(200) not O(n))
PROGRESS_EVERY = 100     # broadcast WS update every N files

# Formats where ffmpeg can't directly decode the source file.
# Waveforms for these are computed from the conversion cache WAV instead
# (see tracks.py waveform endpoint).
_SKIP_WAVEFORM_EXTS = {
    ".sid", ".psid",
    ".mid", ".midi",
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ahx", ".hvl", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}


# ── Progress state ────────────────────────────────────────────────────────────

@dataclass
class ScanProgress:
    total:        int   = 0
    processed:    int   = 0
    errors:       int   = 0
    running:      bool  = False
    embedding:    bool  = False
    current_file: str   = ""
    started_at:   float = field(default_factory=time.time)

    def pct(self) -> int:
        return min(100, int(self.processed / self.total * 100)) if self.total else 0

    def to_dict(self) -> dict:
        d = {
            "total":        self.total,
            "processed":    self.processed,
            "errors":       self.errors,
            "running":      self.running,
            "embedding":    self.embedding,
            "pct":          self.pct(),
            "current_file": self.current_file,
        }
        # Append queue info (combine local + remote active dirs)
        all_active = set(_current_scan_dirs) | _current_remote_dirs
        d["current_dirs"] = sorted(all_active) if all_active else []
        d["queued"] = [sorted(q_dirs) for q_dirs, _ in _scan_queue]
        d["queue_depth"] = len(_scan_queue)
        return d


_progress  = ScanProgress()
_scan_count: int = 0            # number of active scans (local + remote)
_scan_task: asyncio.Task | None = None


def get_progress() -> ScanProgress:
    return _progress


def is_scanning() -> bool:
    return _progress.running


# ── File discovery ─────────────────────────────────────────────────────────────

# OS-generated junk filenames that may carry an audio-looking extension but
# contain no real audio data.  AppleDouble sidecars (``._foo.m4a``) are the
# common one — macOS auto-creates them whenever it writes extended attributes
# to a non-Apple filesystem (FTP / SMB / exFAT).  Indexing them produces
# ghost tracks with random temp-file titles, so we skip them at discovery.
_JUNK_BASENAMES_LOWER = {
    ".ds_store", "thumbs.db", "desktop.ini", "icon\r",
}

def _is_junk_filename(name: str) -> bool:
    """True if ``name`` is an OS metadata sidecar that should never be indexed."""
    if not name:
        return True
    # macOS AppleDouble metadata sidecars (``._<original>``)
    if name.startswith("._"):
        return True
    if name.lower() in _JUNK_BASENAMES_LOWER:
        return True
    return False


def _basename_of(path_str: str) -> str:
    """Last path component, regardless of forward/back slash."""
    if "/" in path_str:
        return path_str.rsplit("/", 1)[-1]
    if "\\" in path_str:
        return path_str.rsplit("\\", 1)[-1]
    return path_str


def _find_audio_files(directories: list[str], scan_zips: bool = True) -> dict[str, list[Path]]:
    import io
    import os
    import zipfile

    ext_set = {e.lower() for e in SUPPORTED_EXTENSIONS}

    def _is_audio(name: str) -> bool:
        return os.path.splitext(name)[1].lower() in ext_set

    result: dict[str, list[Path]] = {}
    for d in directories:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            log.warning("Scan dir not found, skipping: %s", p)
            continue

        files: list[Path] = []
        skipped_junk = 0

        # Single-pass walk — much faster than N separate rglob calls
        for dirpath, _dirs, filenames in os.walk(p):
            for fn in filenames:
                if _is_junk_filename(fn):
                    skipped_junk += 1
                    continue
                full = os.path.join(dirpath, fn)
                lower = fn.lower()

                if os.path.splitext(lower)[1] in ext_set:
                    files.append(Path(full))

                elif scan_zips and lower.endswith(".zip"):
                    # Scan inside ZIP files
                    try:
                        with zipfile.ZipFile(full, 'r') as zf:
                            for member in zf.namelist():
                                member_basename = _basename_of(member)
                                if _is_junk_filename(member_basename):
                                    skipped_junk += 1
                                    continue
                                if _is_audio(member):
                                    # Direct audio file in ZIP
                                    files.append(Path(f"{full}::{member}"))
                                elif member.lower().endswith(".zip"):
                                    # Nested ZIP (e.g. modarchive: outer.zip → track.it.zip → track.it)
                                    try:
                                        inner_data = zf.read(member)
                                        with zipfile.ZipFile(io.BytesIO(inner_data), 'r') as inner_zf:
                                            for inner_name in inner_zf.namelist():
                                                if _is_junk_filename(_basename_of(inner_name)):
                                                    skipped_junk += 1
                                                    continue
                                                if _is_audio(inner_name):
                                                    files.append(Path(f"{full}::{member}::{inner_name}"))
                                    except (zipfile.BadZipFile, OSError):
                                        pass
                    except (zipfile.BadZipFile, OSError) as exc:
                        log.warning("Cannot read ZIP %s: %s", full, exc)

        result[str(p)] = sorted(set(files))
        if skipped_junk:
            log.info("Discovered %d audio files in %s (skipped %d OS junk file(s))",
                     len(files), p, skipped_junk)
        else:
            log.info("Discovered %d audio files in %s", len(files), p)
    return result


def _find_remote_audio_files(
    root_path: str, source: "FileSource",
) -> dict[str, list[str]]:
    """Discover audio files on a remote FileSource.

    Returns {root_path: [remote_path, ...]}.  Paths are strings (not Path
    objects) because remote paths aren't real filesystem paths.
    """
    from soniqboom.core.filesource import FileSource

    ext_set = {e.lower() for e in SUPPORTED_EXTENSIONS}
    files: list[str] = []
    skipped_junk = 0
    try:
        for dirpath, _dirs, filenames in source.walk("/"):
            for fn in filenames:
                if _is_junk_filename(fn):
                    skipped_junk += 1
                    continue
                if os.path.splitext(fn.lower())[1] in ext_set:
                    fpath = f"{dirpath}/{fn}" if dirpath != "/" else f"/{fn}"
                    files.append(fpath)
    except Exception as exc:
        log.error("Remote walk failed for %s: %s", root_path, exc)
    if skipped_junk:
        log.info("Discovered %d remote audio files in %s (skipped %d OS junk file(s))",
                 len(files), root_path, skipped_junk)
    else:
        log.info("Discovered %d remote audio files in %s", len(files), root_path)
    return {root_path: sorted(set(files))}


# ── One-shot library cleanup ─────────────────────────────────────────────────
#
# Two pre-existing data issues that older builds of SoniqBoom could let into
# the library:
#
#   1. Tracks whose path basename is an OS junk file (``._*`` AppleDouble
#      metadata sidecars on FTP/SMB, ``.DS_Store``, ``Thumbs.db``).  These
#      are not audio.  Discovery now filters them — this purge removes any
#      that already slipped in.
#
#   2. Tracks whose ``title`` is a leaked Python ``tempfile`` basename
#      (``tmp[a-z0-9_]{8}``) because the source had no title tag and the
#      pre-fix extractor used the temp file's stem as the fallback.  We
#      derive the correct title from the path basename instead — no rescan
#      (or network I/O) needed.

import re as _re
_TMP_TITLE_RE = _re.compile(r"^tmp[a-z0-9_]{6,12}$")


async def purge_junk_tracks() -> dict:
    """Remove ghost tracks created by AppleDouble files and repair leaked titles.

    Safe to run on every startup: idempotent and cheap (in-memory scan only).
    Returns counts for logging.
    """
    from soniqboom.core.data import delete_track_ids
    from soniqboom.core.store import get_store

    store = get_store()

    junk_ids: list[str] = []
    title_fixups: list[tuple[str, str]] = []  # (track_id, new_title)

    for t in store.all_tracks():
        path = t.get("path") or ""
        if not path:
            continue
        base = _basename_of(path)

        if _is_junk_filename(base):
            junk_ids.append(t["id"])
            continue

        title = (t.get("title") or "").strip()
        if title and _TMP_TITLE_RE.match(title):
            real_stem = Path(base).stem
            if real_stem and real_stem != title:
                title_fixups.append((t["id"], real_stem))

    deleted = 0
    if junk_ids:
        deleted = await delete_track_ids(junk_ids)
        log.info("Purged %d ghost track(s) from OS metadata sidecars (._*, .DS_Store, ...)",
                 deleted)

    repaired = 0
    if title_fixups:
        for tid, new_title in title_fixups:
            if store.update_track_fields(tid, {"title": new_title}):
                repaired += 1
        log.info("Repaired %d track title(s) leaked from temp-file basenames", repaired)

    return {"deleted": deleted, "repaired_titles": repaired}


def _extract_one_remote(
    file_data: bytes, remote_path: str, track_id: str,
) -> tuple[str, TrackMeta | str, None, None]:
    """Extract metadata from an already-downloaded file buffer.

    Runs in a worker process like _extract_one.  Writes data to a temp file
    because mutagen requires a seekable file handle for most formats.
    """
    import tempfile
    try:
        ext = os.path.splitext(remote_path)[1]
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)
        try:
            meta = extract(tmp_path, track_id)
            meta.path = remote_path
            meta.mtime = 0.0
            meta.file_size = len(file_data)
            # Title fallback inside extract() used the temp file's basename
            # (e.g. ``tmpXXXXXXXX``) when the source had no title tag.  Replace
            # it with the real remote basename so the UI doesn't show garbage.
            real_stem = Path(_basename_of(remote_path)).stem
            if real_stem and meta.title == tmp_path.stem:
                meta.title = real_stem
            return remote_path, meta, None, None
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        return remote_path, f"{type(exc).__name__}: {exc}", None, None


# ── Phase 1 helpers ───────────────────────────────────────────────────────────

def _read_from_zip_path(virtual_path: str) -> tuple[bytes, str]:
    """Read raw file bytes from a (possibly nested) ZIP virtual path.

    Supports paths like:
      /path/archive.zip::track.sid
      /path/outer.zip::inner.zip::track.mod

    Returns (file_bytes, final_member_name).
    """
    import io
    import zipfile

    parts = virtual_path.split("::")
    # First part is always the outer ZIP on disk
    data: bytes = b""
    current_zip_path = parts[0]

    for i, member in enumerate(parts[1:], 1):
        if i == 1:
            with zipfile.ZipFile(current_zip_path, 'r') as zf:
                data = zf.read(member)
        else:
            with zipfile.ZipFile(io.BytesIO(data), 'r') as zf:
                data = zf.read(member)

    return data, parts[-1]


def _extract_from_zip(virtual_path: str, track_id: str) -> TrackMeta:
    """Extract metadata from a file inside a (possibly nested) ZIP archive."""
    import tempfile

    data, member_name = _read_from_zip_path(virtual_path)

    suffix = Path(member_name).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        meta = extract(tmp_path, track_id)
        meta.path = virtual_path
        # Title fallback inside extract() used the temp file's basename when the
        # source had no title tag.  Substitute the real ZIP member name so the
        # UI doesn't show ``tmpXXXXXXXX``.
        real_stem = Path(_basename_of(member_name)).stem
        if real_stem and meta.title == tmp_path.stem:
            meta.title = real_stem
        return meta
    finally:
        tmp_path.unlink(missing_ok=True)


def _extract_one(path: Path) -> tuple[Path, TrackMeta | str, bytes | None, bytes | None]:
    """Run in a **worker process** — extract metadata for a single file.

    Returns (path, meta_or_error_string, sm_thumb, lg_thumb).
    Errors are returned as strings (not Exception objects) because they must
    survive pickle serialization across process boundaries.
    """
    try:
        path_str = str(path)
        track_id = str(uuid.uuid5(uuid.NAMESPACE_URL, path_str))

        if '::' in path_str:
            meta = _extract_from_zip(path_str, track_id)
        else:
            meta = extract(path, track_id)

        # Stamp mtime — for ZIP files, use the outer archive's mtime
        actual_path = Path(path_str.split('::')[0]) if '::' in path_str else path
        meta.mtime = actual_path.stat().st_mtime

        return path, meta, None, None
    except Exception as exc:
        return path, f"{type(exc).__name__}: {exc}", None, None


def _build_track(
    meta: TrackMeta,
    scan_root: str,
    parent: str,
    hash_cache: dict[str, str],
) -> tuple[Track | None, str | None]:
    """Assemble a Track object; resolves dir/root hashes via the local cache.

    Returns (track, raw_art_data_uri).
    Thumbnails are generated in _extract_one (thread pool) to avoid blocking
    the async event loop.
    """
    from soniqboom.config import settings
    try:
        dir_h  = hash_cache[parent]
        root_h = hash_cache[scan_root]
        meta_dict = meta.model_dump()
        meta_dict["dir_hash"]        = dir_h
        meta_dict["scan_root_hash"]  = root_h

        # Strip the embedded base64 art; replace with a URL reference
        raw_art = meta_dict.pop("cover_art", None)
        meta_dict["cover_art"] = f"/api/art/{meta.id}" if raw_art else None

        # Don't store a huge zero embedding — leave empty, Phase 2 fills it
        track = Track(**meta_dict, embedding=[])
        # Art data is no longer stored during scan — on-access cache handles it
        return track, None
    except Exception as exc:
        log.error("Failed to build track %s: %s", meta.path, exc)
        return None, None



# Phase 2 (embedding computation) removed — Python-only mode has no vector search.


def _compute_incremental(
    files_strs: list[str],
    mtime_size_map: dict[str, tuple[float | None, int | None]],
) -> tuple[set[str], dict[str, str]]:
    """Determine which files need scanning.  **Runs in a worker process.**

    Receives only primitive data (strings, dicts of tuples) so it can be
    pickled across the process boundary.  Returns (fresh_path_strs,
    track_ids_for_files) where track_ids_for_files maps path_str → track_id.
    """
    # Cache stat results by the real filesystem path so that virtual paths
    # sharing the same outer ZIP (e.g. "archive.zip::inner.zip::track.mod")
    # only trigger ONE os.stat() call per unique outer file.  For a library
    # with 122K virtual paths across ~60K ZIPs on a network mount, this can
    # cut stat() calls by half and avoids minutes of blocking.
    _stat_cache: dict[str, os.stat_result | None] = {}
    path_stats: dict[str, tuple[float, int]] = {}
    for ps in files_strs:
        actual = ps.split("::")[0] if "::" in ps else ps
        if actual not in _stat_cache:
            try:
                _stat_cache[actual] = os.stat(actual)
            except OSError:
                _stat_cache[actual] = None
        st = _stat_cache[actual]
        if st is not None:
            path_stats[ps] = (st.st_mtime, st.st_size)

    track_ids_for_files = {
        ps: str(uuid.uuid5(uuid.NAMESPACE_URL, ps)) for ps in files_strs
    }

    fresh: set[str] = set()
    for ps, tid in track_ids_for_files.items():
        if ps not in path_stats:
            continue
        existing = mtime_size_map.get(tid)
        if not existing:
            continue
        stored_mtime, stored_size = existing
        actual_mtime, actual_size = path_stats[ps]
        if (stored_mtime is not None and stored_size is not None
                and abs(stored_mtime - actual_mtime) < 1.0
                and stored_size == actual_size):
            fresh.add(ps)

    return fresh, track_ids_for_files


# ── Phase 3 helper ────────────────────────────────────────────────────────────

def _compute_waveform(path: str, points: int = 200) -> list[float]:
    """Extract a compact waveform (amplitude envelope) from an audio file.

    Uses ffmpeg to decode to mono 8 kHz 32-bit float PCM, then computes RMS
    amplitudes over evenly-sized chunks and normalises to 0.0-1.0.
    """
    from soniqboom.config import settings

    cmd = [
        settings.ffmpeg_path, "-i", path,
        "-ac", "1", "-ar", "8000", "-f", "f32le", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, timeout=60,
    )
    raw = proc.stdout
    if not raw:
        return [0.0] * points

    # Each sample is a 32-bit (4-byte) float
    n_samples = len(raw) // 4
    if n_samples == 0:
        return [0.0] * points

    samples = struct.unpack(f"<{n_samples}f", raw[: n_samples * 4])
    chunk_size = max(1, n_samples // points)

    rms_values: list[float] = []
    for i in range(points):
        start = i * chunk_size
        end = min(start + chunk_size, n_samples)
        if start >= n_samples:
            rms_values.append(0.0)
            continue
        chunk = samples[start:end]
        mean_sq = sum(s * s for s in chunk) / len(chunk)
        rms_values.append(math.sqrt(mean_sq))

    peak = max(rms_values) if rms_values else 1.0
    if peak > 0:
        rms_values = [v / peak for v in rms_values]

    return rms_values


# ── Non-blocking helpers ──────────────────────────────────────────────────────

async def _async_exit_batch_mode(store) -> None:
    """Exit batch mode with yield points between sorted-index rebuilds.

    ``_rebuild_sorted_indexes`` is O(n log n) and freezes the event loop for
    hundreds of milliseconds on large libraries.  By splitting the work into
    per-field sorts with ``asyncio.sleep(0)`` between them, HTTP requests can
    be served in the gaps.
    """
    store._batch_mode = False
    if not store._sorted_dirty:
        return

    # Single pass to build the 4 lists (fast — just dict lookups)
    year, added, dur, bpm = [], [], [], []
    for tid, t in store._tracks.items():
        y = t.get("year")
        if y is not None:
            year.append((y, tid))
        a = t.get("added_at", 0)
        if a:
            added.append((a, tid))
        d = t.get("duration", 0.0)
        if d:
            dur.append((d, tid))
        b = t.get("bpm")
        if b is not None:
            bpm.append((b, tid))

    await asyncio.sleep(0)

    # Sort each list separately, yielding between them.  CPython's
    # list.sort() for tuples of (number, str) runs in C and partially
    # releases the GIL during comparisons.
    # Guard against mixed types (e.g. year as str vs int) which would
    # raise TypeError during sort.
    for lst in (year, added, dur, bpm):
        try:
            lst.sort()
        except TypeError:
            # Coerce all keys to float for a safe comparison
            for i, (val, tid) in enumerate(lst):
                try:
                    lst[i] = (float(val), tid)
                except (ValueError, TypeError):
                    lst[i] = (0.0, tid)
            lst.sort()
        await asyncio.sleep(0)

    store._sorted_year = year
    store._sorted_added_at = added
    store._sorted_duration = dur
    store._sorted_bpm = bpm
    store._sorted_dirty = False
    log.info("Sorted indexes rebuilt: %d year, %d added, %d dur, %d bpm",
             len(year), len(added), len(dur), len(bpm))


def _compute_duplicates_in_process(all_tracks: list[dict]) -> dict:
    """Runs in a **subprocess** (own GIL) — no event-loop starvation."""
    from soniqboom.core.duplicates import compute_duplicate_groups
    return compute_duplicate_groups(all_tracks)


async def _run_duplicate_detection_async() -> None:
    """Detect duplicates: heavy compute in a subprocess, apply in batches."""
    store = get_store()
    all_tracks = store.all_track_metas()
    if not all_tracks:
        return

    # Run the CPU-heavy algorithm in its own process (separate GIL)
    dup_executor = ProcessPoolExecutor(max_workers=1)
    try:
        loop = asyncio.get_event_loop()
        annotations = await loop.run_in_executor(
            dup_executor, _compute_duplicates_in_process, all_tracks,
        )
    finally:
        dup_executor.shutdown(wait=False)

    # Apply annotations in small batches with yield points
    items = list(annotations.items())
    APPLY_BATCH = 200
    updated = 0
    for i in range(0, len(items), APPLY_BATCH):
        batch = items[i : i + APPLY_BATCH]
        for tid, ann in batch:
            store.update_track_fields(tid, {
                "duplicate_group_id": ann["duplicate_group_id"],
                "format_score": ann["format_score"],
                "is_duplicate_primary": ann["is_duplicate_primary"],
            })
            updated += 1
        await asyncio.sleep(0)

    dup_count = sum(1 for a in annotations.values() if a["duplicate_group_id"] is not None)
    log.info("Duplicate detection: annotated %d tracks (%d in duplicate groups)", updated, dup_count)


# ── Main scan coroutine ────────────────────────────────────────────────────────

async def _run_scan(
    directories: list[str],
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
) -> None:
    global _progress, _scan_count

    loop = asyncio.get_event_loop()

    # ProcessPoolExecutor: each worker has its own GIL so metadata
    # extraction (zipfile, mutagen) never competes with the event loop.
    executor = ProcessPoolExecutor(max_workers=SCAN_WORKERS)

    # ── Discover files ────────────────────────────────────────────────────────
    from soniqboom.config import settings as _settings
    dir_files = await loop.run_in_executor(
        None, _find_audio_files, directories, _settings.scan_zips
    )
    total = sum(len(v) for v in dir_files.values())

    # Additive progress: when a remote scan is already running, add to the
    # existing total instead of overwriting it.
    if _scan_count > 0 and _progress.running:
        _progress.total += total
    else:
        _progress = ScanProgress(total=total, running=True)
    _scan_count += 1
    log.info("Scan started: %d files across %d root(s)", total, len(dir_files))

    dir_counts:    dict[str, int] = defaultdict(int)
    all_track_ids: list[str]      = []

    # ── Phase 1: parallel metadata + chunked store writes ─────────────────────
    store = get_store()
    store.enter_batch_mode()   # defer O(n) sorted-list rebuilds during scan

    for scan_root, files in dir_files.items():
        await upsert_scan_dir(scan_root)

        def _parent_dir(fp: Path) -> str:
            s = str(fp)
            if '::' in s:
                return str(Path(s.split('::')[0]).parent)
            return str(fp.parent)

        unique_dirs = list({_parent_dir(p) for p in files} | {scan_root})
        hash_map = await store_hash_lookups_batch(unique_dirs)

        # ── Incremental scan: skip unchanged files ───────────────────────────
        # This involves stat() calls and uuid5 hashing for every file, which
        # is CPU-intensive for 100K+ files (181K uuid5 + 181K os.stat).
        # Running in a ThreadPoolExecutor still blocks the event loop via GIL
        # contention.  Instead we run in a *ProcessPoolExecutor* (own GIL) and
        # pass only a lightweight mtime/size map (10K entries) instead of the
        # full store, keeping pickle overhead trivial.
        #
        # OPTIMISATION: If the store has NO tracks for this scan root, skip
        # the incremental check entirely — every file needs scanning anyway,
        # and the stat() calls for 60-120K files over a network mount cost
        # 60-120+ seconds with zero benefit.

        existing_ids = await get_track_ids_for_scan_root(scan_root)

        # Convert Path objects to strings for pickling across process boundary
        files_strs = [str(p) for p in files]

        # Only run incremental check if a meaningful fraction of files might
        # be unchanged.  With 1 existing track out of 60K files, stat-checking
        # all 60K (60+ seconds on a network mount) saves at most 1 extraction.
        _run_incr_check = len(existing_ids) > max(100, len(files) // 50)
        if _run_incr_check:
            # Build a small {track_id: (mtime, file_size)} lookup — only
            # existing tracks matter, so bounded by store size, not file count.
            mtime_size_map: dict[str, tuple[float | None, int | None]] = {}
            for tid in existing_ids:
                trk = store._tracks.get(tid)
                if trk:
                    mtime_size_map[tid] = (trk.get("mtime"), trk.get("file_size"))

            log.info(
                "Incremental check for %s: stat-checking %d files (%d existing tracks) …",
                scan_root, len(files_strs), len(mtime_size_map),
            )
            _progress.current_file = f"Checking {len(files_strs):,} files for changes…"
            if on_progress:
                await on_progress(_progress)

            # Use multiple workers for the stat check to saturate network I/O
            INCR_WORKERS = min(4, max(1, len(files_strs) // 5000))
            incr_executor = ProcessPoolExecutor(max_workers=INCR_WORKERS)
            try:
                t0 = time.time()
                if INCR_WORKERS == 1:
                    fresh_strs, tid_map_strs = await loop.run_in_executor(
                        incr_executor, _compute_incremental, files_strs, mtime_size_map,
                    )
                else:
                    # Split file list into chunks and run in parallel
                    chunk_size = math.ceil(len(files_strs) / INCR_WORKERS)
                    chunks = [
                        files_strs[i : i + chunk_size]
                        for i in range(0, len(files_strs), chunk_size)
                    ]
                    chunk_futs = [
                        loop.run_in_executor(
                            incr_executor, _compute_incremental, chunk, mtime_size_map,
                        )
                        for chunk in chunks
                    ]
                    results = await asyncio.gather(*chunk_futs)
                    # Merge results from all chunks
                    fresh_strs: set[str] = set()
                    tid_map_strs: dict[str, str] = {}
                    for chunk_fresh, chunk_tids in results:
                        fresh_strs |= chunk_fresh
                        tid_map_strs.update(chunk_tids)
                log.info(
                    "Incremental check done for %s in %.1fs: %d fresh, %d total",
                    scan_root, time.time() - t0, len(fresh_strs), len(files_strs),
                )
            finally:
                incr_executor.shutdown(wait=False)

            # Map string results back to Path keys
            str_to_path = {str(p): p for p in files}
            fresh_paths = {str_to_path[s] for s in fresh_strs if s in str_to_path}
            track_ids_for_files = {
                str_to_path[s]: tid for s, tid in tid_map_strs.items()
                if s in str_to_path
            }

            files_to_scan = [p for p in files if p not in fresh_paths]
            skipped = len(files) - len(files_to_scan)
            if skipped:
                log.info("Incremental scan: skipping %d unchanged files", skipped)
                _progress.processed += skipped
        else:
            # Too few existing tracks to justify stat-checking all files
            log.info(
                "Skipping incremental check for %s: %d existing tracks vs %d files — scanning all",
                scan_root, len(existing_ids), len(files_strs),
            )
            files_to_scan = list(files)
            skipped = 0
            # track_ids_for_files only needed for stale cleanup, which is a
            # no-op when there are no existing tracks.  Set to empty dict.
            track_ids_for_files = {}

        log.info(
            "Extraction starting for %s: %d files to scan, %d skipped",
            scan_root, len(files_to_scan), skipped,
        )

        # ── Sliding window executor ──────────────────────────────────────────
        # Submit only INFLIGHT tasks at a time so asyncio.wait() operates on
        # a small set (~200) instead of all 170K+ futures.  This keeps each
        # wait() call at O(INFLIGHT) rather than O(total_files), preventing
        # the event loop from stalling for 700 ms+ per call.

        track_buffer: list[Track]       = []
        art_buffer:   dict[str, str]    = {}
        sm_thumbs:    dict[str, bytes]  = {}
        lg_thumbs:    dict[str, bytes]  = {}

        async def _flush_buffer():
            """Write buffered tracks in sub-batches, yielding between chunks."""
            nonlocal track_buffer, art_buffer, sm_thumbs, lg_thumbs
            if not track_buffer:
                return
            n = len(track_buffer)
            t0 = time.time()
            try:
                for i in range(0, n, WRITE_CHUNK):
                    chunk = track_buffer[i : i + WRITE_CHUNK]
                    await upsert_tracks_batch(chunk)
                    await asyncio.sleep(0)
                await store_full_art_batch(art_buffer)
                await store_thumbs_batch(sm_thumbs, lg_thumbs)
                log.debug("Flushed %d tracks in %.2fs", n, time.time() - t0)
            except Exception as exc:
                log.error("Flush error after %.2fs: %s", time.time() - t0, exc, exc_info=True)
            track_buffer = []
            art_buffer   = {}
            sm_thumbs    = {}
            lg_thumbs    = {}

        async def _handle_result(fut):
            """Process one completed extraction future."""
            nonlocal track_buffer
            path, result, sm_thumb, lg_thumb = await fut

            if isinstance(result, str):
                log.error("Metadata error %s: %s", path, result)
                _progress.errors += 1
            else:
                meta: TrackMeta = result
                track, raw_art = _build_track(
                    meta, scan_root, _parent_dir(path), hash_map,
                )
                if track:
                    track_buffer.append(track)
                    if raw_art:
                        art_buffer[track.id] = raw_art
                    if sm_thumb:
                        sm_thumbs[track.id] = sm_thumb
                    if lg_thumb:
                        lg_thumbs[track.id] = lg_thumb
                    dir_counts[scan_root] += 1
                    all_track_ids.append(meta.id)
                    if len(track_buffer) >= WRITE_BATCH:
                        await _flush_buffer()
                else:
                    _progress.errors += 1

            _progress.processed    += 1
            _progress.current_file  = path.name

            if on_progress and (
                _progress.processed % PROGRESS_EVERY == 0
                or _progress.processed == total
            ):
                await on_progress(_progress)

        file_iter = iter(files_to_scan)
        active: dict[asyncio.Future, Path] = {}

        # Seed initial window
        for path in itertools.islice(file_iter, INFLIGHT):
            fut = loop.run_in_executor(executor, _extract_one, path)
            active[fut] = path
        while active:
            done, _ = await asyncio.wait(
                active.keys(), return_when=asyncio.FIRST_COMPLETED,
                timeout=90,  # seconds — skip stuck workers
            )

            if not done:
                # Every remaining future is stuck (corrupted file, hung worker).
                stuck_paths = [str(p) for p in active.values()]
                log.error(
                    "Extraction timed out for %d file(s), skipping: %s",
                    len(stuck_paths),
                    stuck_paths[:5],  # log first 5 to avoid flooding
                )
                for fut in list(active):
                    fut.cancel()
                _progress.errors    += len(active)
                _progress.processed += len(active)
                active.clear()
                if on_progress:
                    await on_progress(_progress)
                break

            for fut in done:
                fut_path = active.pop(fut)
                try:
                    await _handle_result(fut)
                except Exception as exc:
                    log.error("Worker error for %s: %s", fut_path, exc)
                    _progress.errors    += 1
                    _progress.processed += 1

            # Refill the window with new tasks
            for path in itertools.islice(file_iter, len(done)):
                new_fut = loop.run_in_executor(executor, _extract_one, path)
                active[new_fut] = path

            await asyncio.sleep(0)  # yield to event loop every iteration

        # Flush any remaining tracks for this root
        await _flush_buffer()

        # ── Stale track cleanup ──────────────────────────────────────────────
        # Reuse track_ids_for_files from the incremental check — it already
        # maps every path to its uuid5 track ID.  No need to recompute 181K
        # uuid5 values on the event loop.
        #
        # IMPORTANT: When the incremental check was skipped (_run_incr_check
        # is False), track_ids_for_files is empty — so expected_ids would be
        # empty and ALL just-added tracks would be deleted as "orphans".
        # Skip stale cleanup in that case; there were essentially no existing
        # tracks to clean up anyway (that was the reason the incr check was
        # skipped in the first place).
        if _run_incr_check and track_ids_for_files:
            expected_ids = set(track_ids_for_files.values())
            existing_ids = await get_track_ids_for_scan_root(scan_root)
            orphan_ids = existing_ids - expected_ids
            if orphan_ids:
                orphan_count = await delete_track_ids(list(orphan_ids))
                log.info("Stale cleanup: removed %d orphan tracks for %s", orphan_count, scan_root)
            else:
                log.debug("Stale cleanup: no orphans for %s", scan_root)
        else:
            log.debug("Stale cleanup: skipped for %s (incremental check was not run)", scan_root)

        await upsert_scan_dir(scan_root, track_count_val=skipped + dir_counts[scan_root])

    # Exit batch mode — rebuild sorted indexes with yield points so the
    # event loop stays responsive during the O(n log n) sorts.
    await _async_exit_batch_mode(store)

    log.info(
        "Phase 1 complete: %d tracks written in %.1fs",
        len(all_track_ids), time.time() - _progress.started_at,
    )

    # ── Phase 1.5: Duplicate detection ──────────────────────────────────────
    # Heavy compute in a subprocess (own GIL — no event loop starvation).
    # Results applied back in small batches with yield points.
    try:
        await _run_duplicate_detection_async()
    except Exception as exc:
        log.error("Duplicate detection failed (non-fatal): %s", exc)

    # Invalidate aggregation caches now that new tracks are in the store
    from soniqboom.api.library import invalidate_agg_cache
    invalidate_agg_cache()

    _scan_count = max(0, _scan_count - 1)
    if _scan_count == 0:
        _progress.running      = False
        _progress.embedding    = False
        _progress.current_file = ""

    if on_progress:
        await on_progress(_progress)

    executor.shutdown(wait=False)


# ── Public API ────────────────────────────────────────────────────────────────

_scan_queue: list[tuple[frozenset[str], Callable | None]] = []
_current_scan_dirs: frozenset[str] = frozenset()
_current_remote_dirs: set[str] = set()   # active remote scan roots


async def _drain_scan_queue() -> None:
    """Run scans sequentially until the queue is empty."""
    global _scan_task, _current_scan_dirs, _scan_count, _progress
    while _scan_queue:
        dirs_set, cb = _scan_queue.pop(0)
        _current_scan_dirs = dirs_set
        log.info("Scan queue: starting scan of %d dir(s), %d remaining in queue",
                 len(dirs_set), len(_scan_queue))
        try:
            await _run_scan(list(dirs_set), cb)
        except Exception:
            log.exception("Scan failed with unhandled error")
            # Ensure progress state is always cleaned up so the UI
            # doesn't show "scanning" forever.
            _scan_count = max(0, _scan_count - 1)
            if _scan_count == 0:
                _progress.running      = False
                _progress.embedding    = False
                _progress.current_file = ""
        _current_scan_dirs = frozenset()
    _scan_task = None


def is_scanning(path: str | None = None) -> bool:
    """Check if a path (or any path) is currently being scanned or queued."""
    if path is None:
        return bool(_current_scan_dirs) or bool(_current_remote_dirs) or bool(_scan_queue)
    # Remote paths (ftp://, smb://) aren't resolved via Path()
    if path.startswith(("ftp://", "smb://")):
        return path in _current_remote_dirs
    norm = str(Path(path).resolve())
    if norm in _current_scan_dirs:
        return True
    return any(norm in q_dirs for q_dirs, _ in _scan_queue)


async def start_scan(
    directories: list[str],
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
) -> asyncio.Task:
    """Queue a scan.  If one is already running the request is queued and will
    run automatically once the current scan finishes — duplicates are skipped."""
    global _scan_task
    norm_dirs = frozenset(str(Path(d).resolve()) for d in directories)

    # Skip if these dirs are already being scanned right now
    if norm_dirs and norm_dirs.issubset(_current_scan_dirs):
        log.info("Scan skipped — dirs already being scanned: %s", norm_dirs)
        if _scan_task and not _scan_task.done():
            return _scan_task
        # Shouldn't happen, but fall through to create task if needed

    # Skip if these dirs are already queued
    for queued_dirs, _ in _scan_queue:
        if norm_dirs.issubset(queued_dirs):
            log.info("Scan skipped — dirs already queued: %s", norm_dirs)
            if _scan_task and not _scan_task.done():
                return _scan_task
            break

    _scan_queue.append((norm_dirs, on_progress))

    if _scan_task and not _scan_task.done():
        log.info("Scan already running — queued %d dir(s) (queue depth: %d)",
                 len(norm_dirs), len(_scan_queue))
        return _scan_task
    _scan_task = asyncio.create_task(_drain_scan_queue())
    return _scan_task


# ── Remote scan (SMB / FTP) ─────────────────────────────────────────────────

async def start_remote_scan(
    share_id: str,
    scan_root: str,
    source: "FileSource",
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
) -> None:
    """Scan a remote FileSource — download files, extract metadata, upsert.

    Unlike the local scan which farms out file paths to worker processes,
    this downloads files via the source (which holds the connection) on a
    thread pool, then extracts metadata in worker processes.
    """
    global _progress, _scan_count
    from soniqboom.core.filesource import FileSource
    from soniqboom.core.remote_cache import get_cache

    loop = asyncio.get_event_loop()
    executor = ProcessPoolExecutor(max_workers=SCAN_WORKERS)
    cache = get_cache()

    dir_files = await loop.run_in_executor(None, _find_remote_audio_files, scan_root, source)
    remote_paths = dir_files.get(scan_root, [])
    total = len(remote_paths)

    # Additive progress: when a local scan is already running, add to the
    # existing total instead of overwriting it.
    if _scan_count > 0 and _progress.running:
        _progress.total += total
    else:
        _progress = ScanProgress(total=total, running=True)
    _scan_count += 1
    _current_remote_dirs.add(scan_root)
    log.info("Remote scan started: %d files in %s", total, scan_root)

    await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")
    hash_map = await store_hash_lookups_batch([scan_root])

    store = get_store()
    store.enter_batch_mode()

    track_buffer: list[Track] = []
    art_buffer: dict[str, str] = {}
    track_count = 0

    async def _flush():
        nonlocal track_buffer, art_buffer
        if not track_buffer:
            return
        for i in range(0, len(track_buffer), WRITE_CHUNK):
            await upsert_tracks_batch(track_buffer[i : i + WRITE_CHUNK])
            await asyncio.sleep(0)
        await store_full_art_batch(art_buffer)
        track_buffer = []
        art_buffer = {}

    for remote_path in remote_paths:
        try:
            file_data = await loop.run_in_executor(None, source.read_file, remote_path)
        except Exception as exc:
            log.error("Download failed %s: %s", remote_path, exc)
            _progress.errors += 1
            _progress.processed += 1
            continue

        track_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scan_root}:{remote_path}"))
        parent_dir = str(PurePosixPath(remote_path).parent) if "/" in remote_path else scan_root

        _, result, _, _ = await loop.run_in_executor(
            executor, _extract_one_remote, file_data, remote_path, track_id,
        )

        if isinstance(result, str):
            log.error("Metadata error %s: %s", remote_path, result)
            _progress.errors += 1
        else:
            meta: TrackMeta = result
            meta.path = f"{scan_root}:{remote_path}"
            if parent_dir not in hash_map:
                new_hashes = await store_hash_lookups_batch([parent_dir])
                hash_map.update(new_hashes)
            track, raw_art = _build_track(meta, scan_root, parent_dir, hash_map)
            if track:
                track_buffer.append(track)
                if raw_art:
                    art_buffer[track.id] = raw_art
                track_count += 1
                if len(track_buffer) >= WRITE_BATCH:
                    await _flush()

        _progress.processed += 1
        _progress.current_file = PurePosixPath(remote_path).name
        if on_progress and (_progress.processed % PROGRESS_EVERY == 0 or _progress.processed == total):
            await on_progress(_progress)

    await _flush()
    await _async_exit_batch_mode(store)
    await upsert_scan_dir(scan_root, track_count_val=track_count,
                          network_share_id=share_id, status="ok")

    _current_remote_dirs.discard(scan_root)
    _scan_count = max(0, _scan_count - 1)
    if _scan_count == 0:
        _progress.running = False
    executor.shutdown(wait=False)
    log.info("Remote scan complete: %d tracks from %s", track_count, scan_root)
