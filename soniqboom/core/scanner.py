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
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from soniqboom.core.metadata import SUPPORTED_EXTENSIONS, extract
from soniqboom.core import diskimage
from soniqboom.core import archive
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
# Default to half the available cores (rounded up, min 2, max 16) — a fixed 8
# over-subscribed 2-core Macs and under-utilised 10+-core M-series machines.
def _default_scan_workers() -> int:
    cores = os.cpu_count() or 4
    return max(2, min(16, (cores + 1) // 2))

SCAN_WORKERS   = _default_scan_workers()
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
    # Last-scan summary — populated on completion of a remote scan so
    # the UI can show "skipped 16027, refreshed 0, deleted 1928"
    # instead of bare "Scan complete".  Each completion overwrites,
    # so this reflects the most recent scan.  Empty dict pre-first-
    # scan.
    last_plan:    dict = field(default_factory=dict)
    # Resolved absolute paths covered by the most-recent scan.  Unlike
    # ``current_dirs`` (cleared the instant a scan finishes), this SURVIVES
    # completion so the ``running:false`` broadcast tells the frontend which
    # folder(s) just finished — letting it refresh the open folder IN PLACE
    # instead of resetting the whole tree.  Set at scan start.
    last_dirs:    list = field(default_factory=list)

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
            "paused":       is_scan_paused(),
            "last_plan":    dict(self.last_plan),
            "last_dirs":    list(self.last_dirs),
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

# Folder-art prefetch tasks detach from their parent scan (see comment
# in start_remote_scan).  asyncio holds only a WEAK ref to tasks created
# with create_task — without a strong ref here they could be garbage-
# collected mid-flight.  The done_callback discards on completion.
_art_prefetch_tasks: set[asyncio.Task] = set()


def _art_prefetch_done(task: asyncio.Task) -> None:
    """Log + discard finished folder-art prefetch tasks.

    Runs synchronously from the asyncio loop's done-callback dispatch
    (not a coroutine, no await).  Keep it cheap.

    Task name is set to ``scan.art_prefetch[{scan_root}]`` at create
    time so we can recover the scan_root for context in log lines
    without keeping a closure ref to it (which would chain into the
    task's locals and complicate cleanup).
    """
    _art_prefetch_tasks.discard(task)
    name = task.get_name()
    scan_root = "?"
    if name.startswith("scan.art_prefetch[") and name.endswith("]"):
        scan_root = name[len("scan.art_prefetch["):-1]
    if task.cancelled():
        log.info("Folder-art prefetch for %s cancelled", scan_root)
        return
    exc = task.exception()
    if exc is not None:
        log.warning("Folder-art prefetch for %s raised: %s", scan_root, exc)
        return
    try:
        stats = task.result() or {}
    except Exception as exc:  # pragma: no cover — already handled above
        log.warning(
            "Folder-art prefetch for %s result raise: %s", scan_root, exc,
        )
        return
    if stats.get("unique_dirs"):
        log.info(
            "Folder-art prefetch for %s: %d dirs (warmed=%d, cached=%d, "
            "no_art=%d, errors=%d)",
            scan_root,
            stats.get("unique_dirs", 0),
            stats.get("warmed", 0),
            stats.get("skipped_cached", 0),
            stats.get("no_art", 0),
            stats.get("errors", 0),
        )


# ── Pause / resume ──────────────────────────────────────────────────────────
#
# Scanner pause is a soft co-operative gate: workers check the event
# between files and ``await``-wait if it's cleared.  In-flight
# downloads / extractions complete normally; new ones don't start
# until resume.  Implemented as an asyncio.Event that defaults to
# *set* (= NOT paused, work proceeds).  ``pause_scan`` clears it,
# ``resume_scan`` sets it.
#
# Lazy event-loop binding: the Event needs to be attached to the
# running event loop at first use, not module-import time (which may
# happen before uvicorn creates its loop).  We construct on first
# access in ``_pause_event()``.
_pause_event: asyncio.Event | None = None


def _pause_event_or_init() -> asyncio.Event:
    """Return the module-level pause Event, creating it on first call."""
    global _pause_event
    if _pause_event is None:
        _pause_event = asyncio.Event()
        _pause_event.set()  # default: not paused
    return _pause_event


def pause_scan() -> bool:
    """Pause all active and future scans.  Idempotent — returns True
    if the call actually flipped the state (was running, now paused),
    False if it was already paused.
    """
    ev = _pause_event_or_init()
    was_running = ev.is_set()
    ev.clear()
    if was_running:
        log.info("Scan paused — workers will block between files")
    return was_running


def resume_scan() -> bool:
    """Resume all paused scans.  Idempotent — returns True if the
    call actually flipped the state, False if it was already running.
    """
    ev = _pause_event_or_init()
    was_paused = not ev.is_set()
    ev.set()
    if was_paused:
        log.info("Scan resumed — workers will pick up the next file")
    return was_paused


def is_scan_paused() -> bool:
    """True iff the scan is currently paused.  Cheap, lockless."""
    return _pause_event is not None and not _pause_event.is_set()


async def _await_resume() -> None:
    """Block this worker until the scan is un-paused.

    A no-op when the scan isn't paused.  Called at the top of each
    per-file step so a long scan can be quiesced quickly without
    cancelling in-flight downloads.
    """
    ev = _pause_event_or_init()
    if not ev.is_set():
        # The worker is about to yield to other tasks while it waits —
        # update the progress's current_file label so the UI shows the
        # paused state instead of the file we're about to start on.
        await ev.wait()


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

                elif scan_zips and diskimage.is_disk_image(lower):
                    # Crack open vintage disk images (C64 .d64/.d71/.d81,
                    # Amiga .adf) and surface embedded SID / tracker tunes as
                    # ``::``-members — exactly like ZIP entries.
                    try:
                        for member in diskimage.list_members(full):
                            files.append(Path(f"{full}::{member}"))
                    except OSError as exc:
                        log.warning("Cannot read disk image %s: %s", full, exc)

                elif scan_zips and lower.endswith((".lha", ".lzh")):
                    # Amiga LHA/LZH archives — surface the modules inside
                    # (handles the ``MOD.title`` Amiga prefix naming).
                    for member in archive.list_members(full):
                        files.append(Path(f"{full}::{member}"))

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
      /path/disk.d64::THE RUNNER.sid          (C64/Amiga disk images)

    Returns (file_bytes, final_member_name).
    """
    # Vintage disk images (C64 .d64/.d71/.d81, Amiga .adf) are read by the
    # diskimage module.  They don't nest, so the member is everything after
    # the first ``::``.
    outer = virtual_path.split("::", 1)[0]
    if diskimage.is_disk_image(outer) and "::" in virtual_path:
        member = virtual_path.split("::", 1)[1]
        return diskimage.read_member(outer, member), member
    # Amiga LHA/LZH (and the same generic path works for a local cached zip):
    # no nesting, so the member is everything after the first ``::``.
    if archive.is_lha_name(outer) and "::" in virtual_path:
        member = virtual_path.split("::", 1)[1]
        return archive.read_member(outer, member), member

    import io
    import zipfile

    parts = virtual_path.split("::")
    # First part is always the outer ZIP on disk.  For nested archives
    # we spill each intermediate level to a tempfile rather than keeping
    # the enclosing member in RAM — Audio-2 P1 found that nesting depth
    # > 1 produced a worst-case peak of "sum of all enclosing member
    # sizes" of RAM because we read each level into bytes + wrapped in
    # BytesIO.  For outer.zip(500MB) -> inner.zip(200MB) -> track.mod
    # that was a 700 MB transient.  Tempfile spill bounds it to one
    # member-size of disk IO instead.
    import tempfile, os as _os

    # For 1-deep nesting (common case) stay with the in-memory read —
    # avoids the disk syscall when the member is small.
    if len(parts) == 2:
        # Route the common single-level case through the cached archive reader
        # so a huge LOCAL zip doesn't re-open per member (the same O(n^2) the
        # FTP path hit on a 4491-member archive).
        return archive.read_member(parts[0], parts[1]), parts[-1]

    # Deeper nesting: pass through tempfiles.
    current_zip_path = parts[0]
    intermediates: list[str] = []
    try:
        for i, member in enumerate(parts[1:], 1):
            if i < len(parts) - 1:
                # Intermediate ZIP — extract to disk, open the next level
                # from the new file path so the prior level can be
                # released (zf.close in the with-block).
                with zipfile.ZipFile(current_zip_path, 'r') as zf:
                    with zf.open(member, 'r') as src:
                        tmp = tempfile.NamedTemporaryFile(
                            suffix='.zip', delete=False,
                        )
                        try:
                            # Stream the inner member in chunks so peak
                            # RAM is one chunk, not the whole file.
                            while True:
                                buf = src.read(1024 * 1024)
                                if not buf:
                                    break
                                tmp.write(buf)
                        finally:
                            tmp.close()
                        intermediates.append(tmp.name)
                current_zip_path = tmp.name
            else:
                # Final level — read into bytes for the caller.
                with zipfile.ZipFile(current_zip_path, 'r') as zf:
                    return zf.read(member), parts[-1]
    finally:
        for p in intermediates:
            try:
                _os.unlink(p)
            except OSError:
                pass

    # Should not reach here — the loop above always returns when it
    # processes the last part.  Defensive return so the function never
    # falls off the end with an undefined value.
    return b"", parts[-1]


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

def _compute_waveform(path: str, points: int = 200):
    """Extract a compact waveform (peaks + RMS) from an audio file.

    Uses ffmpeg to decode to mono 22.05 kHz 32-bit float PCM, then computes
    both peak-absolute and RMS amplitudes over evenly-sized chunks,
    normalised against the per-axis peak.  8 kHz was below the Nyquist for
    most musical content and lost transient detail; 22 kHz keeps everything
    up to the typical CD bandwidth half-rate while staying small.
    Return shape: ``{"peaks": [...], "rms": [...]}`` when numpy is
    available.  Plain RMS list returned on the pure-Python fallback so the
    public API keeps a JSON-array shape for older clients.
    NOTE: still a sync function; callers ``run_in_executor`` it from
    asyncio paths (see ``api/tracks.py`` ``_WAVEFORM_POOL``).  Going async
    would require rewriting every caller and the dedicated thread pool
    that exists precisely to keep this work off the default executor.
    """
    from soniqboom.config import settings

    cmd = [
        settings.ffmpeg_path, "-i", path,
        "-ac", "1", "-ar", "22050", "-f", "f32le", "-",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, timeout=60,
    )
    return _pcm_to_waveform(proc.stdout, points)


def _pcm_to_waveform(raw: bytes, points: int = 200):
    """Crunch raw mono 22.05 kHz f32le PCM into a compact waveform.

    Split out from ``_compute_waveform`` so the ffmpeg DECODE can be driven by
    ``asyncio.create_subprocess_exec`` on the event loop (fork-safe on macOS —
    ``subprocess.run`` fork from a worker thread segfaults once the process has
    initialised Core Foundation, e.g. after the stations relay's outbound
    networking), while this CPU-bound crunch still runs in a worker thread.
    Return shape mirrors ``_compute_waveform``: ``{"peaks", "rms"}`` on the
    numpy path, a flat RMS list on the pure-Python fallback.
    """
    if not raw:
        return [0.0] * points

    # Each sample is a 32-bit (4-byte) float
    n_samples = len(raw) // 4
    if n_samples == 0:
        return [0.0] * points

    # NumPy when available — vectorised RMS is order-of-magnitude faster than
    # the pure-Python ``sum(s*s for s in chunk)`` over million-sample chunks
    # (typical for any track over a few minutes).  Falls back to struct so
    # this module still imports cleanly on systems without numpy.
    try:
        import numpy as _np
        samples = _np.frombuffer(raw[: n_samples * 4], dtype=_np.float32)
        chunk_size = max(1, n_samples // points)
        usable = chunk_size * points
        # Truncate the tail < chunk_size and reshape to (points, chunk_size).
        # ``einsum("ij,ij->i", c, c)`` computes the per-row sum-of-squares
        # without materialising the full ``c * c`` intermediate (which was
        # a ~115 MB float32 array for a 60-minute track at 8 kHz mono).
        chunks = samples[:usable].reshape(points, chunk_size)
        sumsq = _np.einsum("ij,ij->i", chunks, chunks)
        rms = _np.sqrt(sumsq / chunk_size)
        peaks = _np.abs(chunks).max(axis=1)
        rms_peak = float(rms.max()) if rms.size else 0.0
        peak_peak = float(peaks.max()) if peaks.size else 0.0
        if rms_peak > 0:
            rms = rms / rms_peak
        if peak_peak > 0:
            peaks = peaks / peak_peak
        return {
            "peaks": peaks.astype(float).tolist(),
            "rms": rms.astype(float).tolist(),
        }
    except ImportError:
        pass

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

    Keep ``_batch_mode = True`` until the freshly-built lists are assigned —
    if we flipped to False first, any ``_index_track`` running concurrently
    during the ``await asyncio.sleep(0)`` points would have written into the
    OLD ``_sorted_*`` lists, only for the rebuilt lists to overwrite them.
    """
    if not store._sorted_dirty:
        store._batch_mode = False
        return

    # Single source of truth for the year-collapse rule — keeps this
    # async/yielding rebuild aligned with TrackStore._index_track and the
    # in-line _rebuild_sorted_indexes path.
    from soniqboom.core.store import normalise_year

    # Single pass to build the 4 lists (fast — just dict lookups)
    year, added, dur, bpm = [], [], [], []
    for tid, t in store._tracks.items():
        y = normalise_year(t.get("year"))
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
    # Only now is it safe to drop out of batch mode — any in-flight
    # _index_track that happened during the yields ran in batch mode and
    # set _sorted_dirty (handled on the next rebuild).
    store._batch_mode = False
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

    # Apply annotations in batches with yield points.  Two anti-bloat
    # measures, both load-bearing on a 170K-track library:
    #   1. SKIP tracks whose annotation is UNCHANGED.  This pass runs at
    #      scan-end AND at shutdown; the shutdown run almost always finds the
    #      annotations already current (the last scan set them), so without
    #      this guard it rewrites all ~170K tracks for nothing — a 170K-entry
    #      AOF that the merger (SIGKILLed at shutdown) never truncates, which
    #      then makes the *next* startup's AOF replay pathological.
    #   2. Use the BATCHED writer (one AOF record per batch, not one per
    #      track) for the tracks that genuinely changed.
    items = list(annotations.items())
    APPLY_BATCH = 200
    updated = 0
    for i in range(0, len(items), APPLY_BATCH):
        batch = items[i : i + APPLY_BATCH]
        changed: list[tuple[str, dict]] = []
        for tid, ann in batch:
            new_fields = {
                "duplicate_group_id": ann["duplicate_group_id"],
                "format_score": ann["format_score"],
                "is_duplicate_primary": ann["is_duplicate_primary"],
            }
            cur = store.get_track(tid)
            if cur is not None and all(cur.get(k) == v for k, v in new_fields.items()):
                continue  # unchanged → no store mutation, no AOF entry
            changed.append((tid, new_fields))
        if changed:
            updated += store.update_track_fields_batch(changed)
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
    # Remember the resolved dirs this scan covers — survives completion (unlike
    # ``current_dirs``, cleared the instant the scan ends) so the scan-complete
    # WS event lets the frontend refresh the OPEN folder in place rather than
    # resetting the whole tree to root.
    for _d in directories:
        try:
            _rd = str(Path(_d).resolve())
        except OSError:
            _rd = str(_d)
        if _rd not in _progress.last_dirs:
            _progress.last_dirs.append(_rd)
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
                # The bulk-add can push ``processed`` past one or more
                # PROGRESS_EVERY multiples without firing the per-file
                # broadcast.  Push one update so the user sees the jump
                # rather than the badge sitting at the prior value
                # while ``processed`` silently advances.
                if on_progress:
                    await on_progress(_progress)
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
                    # ``_handle_result`` broadcasts on the %PROGRESS_EVERY
                    # /``== total`` condition; the error path needs the
                    # same check or the user-visible badge stalls at the
                    # last successful broadcast.  If the LAST N files all
                    # error here (e.g. corrupt frames in a bulk-failed
                    # batch), missing this broadcast leaves the badge
                    # stuck at e.g. "99% (X/Y)" with running=true.
                    if on_progress and (
                        _progress.processed % PROGRESS_EVERY == 0
                        or _progress.processed == total
                    ):
                        await on_progress(_progress)

            # Honour the pause flag BEFORE submitting new work — paused
            # scans drain the in-flight window naturally and then idle
            # at this gate until the user clicks Resume.  In-flight
            # futures keep running; the gate only blocks new submissions.
            await _await_resume()

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
    #
    # On a 270K-track library this step takes ~4 min, during which the UI
    # would otherwise show stale state from the extract phase
    # ("Checking 795 files for changes…" with per-share paths listed).
    # Surface the phase via ``_progress.current_file`` and broadcast so
    # the progress label flips to "Detecting duplicates…" immediately —
    # the user sees that something specific is happening instead of
    # assuming the scan is stuck.
    _progress.current_file = "Detecting duplicates…"
    if on_progress:
        await on_progress(_progress)
    try:
        await _run_duplicate_detection_async()
    except Exception as exc:
        log.error("Duplicate detection failed (non-fatal): %s", exc)

    # Invalidate aggregation caches now that new tracks are in the store
    _progress.current_file = "Refreshing aggregations…"
    if on_progress:
        await on_progress(_progress)
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

def _list_remote_zip_members(root_path: str, zip_fe, source) -> list:
    """Download a remote ``.zip`` (once, via the local remote-cache) and return
    a ``DirEntry`` per audio member.

    Each member carries the OUTER archive's ``size``+``mtime`` so the
    incremental-skip logic re-extracts a zip's members iff the archive itself
    changed.  Member ``path`` is ``<zip_rel>::<member>``; the caller prepends
    the ``ftp://host/share:`` prefix exactly as it does for a loose file.
    Best-effort: a broken/oversized archive logs + yields ``[]``.
    """
    from soniqboom.core.filesource import DirEntry
    from soniqboom.core.remote_cache import get_cache

    out: list = []
    try:
        local_archive = get_cache().fetch(root_path, zip_fe.path, source)
    except Exception as exc:
        log.warning("Cannot fetch remote archive %s: %s", zip_fe.path, exc)
        return out
    # ``archive.list_members`` dispatches ZIP vs LHA, handles the Amiga
    # ``MOD.title`` prefix naming, and already filters to playable members.
    for member in archive.list_members(local_archive):
        out.append(DirEntry(
            name=_basename_of(member),
            path=f"{zip_fe.path}::{member}",
            is_dir=False,
            size=zip_fe.size,
            mtime=zip_fe.mtime,
        ))
    return out


def _find_remote_audio_entries(
    root_path: str, source: "FileSource",
    *,
    dir_mtime_cap: float | None = None,
    scan_zips: bool = True,
) -> tuple[list, int]:
    """Discover audio files via ``walk_with_stat`` — yields DirEntry
    objects preserving ``size`` and ``mtime`` from the underlying
    directory-listing response.

    ``dir_mtime_cap`` (optional, Unix epoch seconds) enables the dir-
    mtime fast path: any subtree whose dir.mtime ≤ cap is pruned from
    the walk entirely.  On a stable 30 K-file share this turns a
    30 K-entry walk into ~50 dir-mtime checks.

    Caveat: not every FTP/SMB server updates parent-dir mtime when
    children change.  Servers that don't (mtime stays 0 or constant)
    are detected here: the cap-check is purely "is mtime > cap"; if
    mtime is 0 the check always returns False and the subtree is
    walked normally — i.e. correctness is preserved even when the
    optimization is unsupported.

    Returns ``(entries, pruned_subtree_count)`` so callers can log how
    much the dir-mtime cap actually saved.

    Used by ``start_remote_scan`` to decide which files actually need
    re-extraction (mtime+size match → skip) vs. which need a fresh
    download.  Without this, every re-index re-downloads every byte.
    """
    from soniqboom.core.filesource import FileSource  # noqa
    from soniqboom.core.metadata import SUPPORTED_EXTENSIONS

    ext_set = {e.lower() for e in SUPPORTED_EXTENSIONS}
    entries = []
    skipped_junk = 0
    pruned = [0]  # mutable holder for closure

    def _skip(dir_entry) -> bool:
        # Prune subtree iff dir.mtime exists AND is ≤ cap (i.e. hasn't
        # been touched since the cap timestamp).  mtime of 0 / unknown
        # → can't make a safe call → don't prune.
        if dir_mtime_cap is None or dir_mtime_cap <= 0:
            return False
        if dir_entry.mtime <= 0:
            return False
        if dir_entry.mtime > dir_mtime_cap:
            return False
        pruned[0] += 1
        return True

    try:
        walk_kwargs: dict = {}
        if dir_mtime_cap is not None and dir_mtime_cap > 0:
            walk_kwargs["skip_subtree_fn"] = _skip
        for _dirpath, _dir_entries, file_entries in source.walk_with_stat("/", **walk_kwargs):
            for fe in file_entries:
                if _is_junk_filename(fe.name):
                    skipped_junk += 1
                    continue
                if os.path.splitext(fe.name.lower())[1] in ext_set:
                    entries.append(fe)
                elif scan_zips and fe.name.lower().endswith((".zip", ".lha", ".lzh")):
                    # Crack open the remote archive (ZIP or Amiga LHA/LZH) and
                    # surface its audio members as ``<archive_rel>::<member>``.
                    entries.extend(_list_remote_zip_members(root_path, fe, source))
    except Exception as exc:
        log.error("Remote walk_with_stat failed for %s: %s", root_path, exc)
    if skipped_junk:
        log.info(
            "Discovered %d remote audio entries in %s (skipped %d junk, pruned %d subtree(s))",
            len(entries), root_path, skipped_junk, pruned[0],
        )
    else:
        log.info(
            "Discovered %d remote audio entries in %s (pruned %d subtree(s))",
            len(entries), root_path, pruned[0],
        )
    return entries, pruned[0]


async def _prefetch_folder_art_remote(
    scan_root: str,
    source,
    entries: list,
    *,
    inflight: int = 1,
) -> dict:
    """Warm the shared folder-art cache for every unique parent directory
    observed during the walk.

    ``inflight`` defaults to 1.  Higher values let the prefetch
    contend with the main extract loop for FTP-pool slots.  Observed
    on a re-index of a large share: a 4-wide prefetch combined with
    the 6-wide extract window pushed an 8-slot pool to saturation
    (``in_use=8 idle=0 waiting_scan=8``), which throttled download
    throughput in the menubar (7.8 MB/s observed against a gigabit
    LAN).  Single-flight prefetch leaves at most 7/8 pool slots for
    the user-visible extract path and lets prefetch progress on the
    slack capacity that's left when extract workers escalate between
    partial-fetch stages.

    The art endpoint looks up ``folder:{dir_hash}`` before doing a fresh
    ``list_dir`` + ``read_file`` (see ``_try_folder_art`` in
    ``soniqboom/api/art.py``).  Lazy population works for the *second*
    track in a folder; for the *first* track the user still pays a
    full FTP round trip.  Warming here turns "first track is slow,
    rest are instant" into "every track is instant" — and at the
    cost of one extra MLSD per directory (we already pay one for the
    walk; this just adds a single RETR per dir on a small image).

    ``dir_hash`` MUST be computed the same way as ``_build_track``
    (which reads it from ``hash_cache`` populated by
    ``store_hash_lookups_batch``):
      * ``hashlib.sha256(parent_dir.encode()).hexdigest()[:16]``
      * ``parent_dir = str(PurePosixPath(remote_path).parent)`` —
        for entries from ``walk_with_stat`` (always slash-prefixed,
        e.g. ``/REOL/foo.flac``) this is ``/REOL`` for nested
        files and ``/`` for root-level files.

    Concurrency: bounded by ``inflight`` (default 4) so the prefetch
    doesn't starve the main extract loop that's competing for the
    same FTP pool.  All reads use ``lane='scan'`` for the same reason.

    Returns a stats dict: ``{unique_dirs, warmed, skipped_cached,
    no_art, errors}``.  Logged by the caller; not currently surfaced
    on the WebSocket.
    """
    from soniqboom.core import art_cache
    from soniqboom.core.data import get_config
    from soniqboom.api.art import _find_folder_art_remote, _parse_folder_art_names

    stats = {
        "unique_dirs": 0, "warmed": 0,
        "skipped_cached": 0, "no_art": 0, "errors": 0,
    }
    if not entries:
        return stats

    # Unique parent dirs in the form PurePosixPath produces on the
    # _process_one path — so the hash here matches what _build_track
    # stamps onto each track.
    parents: set[str] = set()
    for fe in entries:
        rel = fe.path or ""
        if not rel:
            continue
        parents.add(str(PurePosixPath(rel).parent))
    stats["unique_dirs"] = len(parents)
    if not parents:
        return stats

    csv = await get_config("folder_art_names", "")
    priority = _parse_folder_art_names(csv if isinstance(csv, str) else "")
    if not priority:
        # No candidate filenames → nothing to prefetch.  Don't log
        # at INFO so this stays quiet on stripped-down installs.
        log.debug(
            "Folder-art prefetch skipped for %s: folder_art_names empty",
            scan_root,
        )
        return stats

    sem = asyncio.Semaphore(inflight)
    loop = asyncio.get_event_loop()
    stats_lock = asyncio.Lock()

    # ``_find_folder_art_remote`` defaults ``lane='stream'``; bind
    # ``lane='scan'`` via a partial so the run_in_executor call site
    # stays positional-args-only (the executor doesn't forward kwargs).
    import functools as _ft
    _find_with_scan_lane = _ft.partial(_find_folder_art_remote, lane="scan")

    async def _warm_one(parent_dir: str) -> None:
        dir_h = path_hash(parent_dir)
        cache_key = f"folder:{dir_h}"
        try:
            cached = await art_cache.get_art(cache_key, "full")
        except Exception:
            cached = None
        if cached:
            async with stats_lock:
                stats["skipped_cached"] += 1
            return
        await _await_resume()
        remote_dir = parent_dir if parent_dir else "/"
        async with sem:
            try:
                data, _mime = await loop.run_in_executor(
                    None, _find_with_scan_lane,
                    scan_root, remote_dir, source, priority,
                )
            except Exception as exc:
                log.debug(
                    "Folder-art prefetch list failed for %s: %s",
                    parent_dir, exc,
                )
                async with stats_lock:
                    stats["errors"] += 1
                return
        if data:
            try:
                await art_cache.store_art(cache_key, data, "full")
                async with stats_lock:
                    stats["warmed"] += 1
            except Exception as exc:
                log.debug(
                    "Folder-art cache store failed for %s: %s",
                    cache_key, exc,
                )
                async with stats_lock:
                    stats["errors"] += 1
        else:
            async with stats_lock:
                stats["no_art"] += 1

    await asyncio.gather(
        *(_warm_one(p) for p in parents),
        return_exceptions=True,
    )
    # Completion logging is handled by ``_art_prefetch_done`` (the
    # done_callback the caller attaches) so the log line appears
    # AFTER the task finishes from the event loop's perspective —
    # no double-log if the caller awaits this synchronously somewhere
    # in tests.
    return stats


async def start_remote_scan(
    share_id: str,
    scan_root: str,
    source: "FileSource",
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
    *,
    dir_mtime_cap: float | None = None,
) -> None:
    """Scan a remote FileSource — download files, extract metadata, upsert.

    ``dir_mtime_cap`` (optional, Unix epoch seconds) enables the fast-
    path walk: subtrees whose parent dir.mtime hasn't changed since
    the cap timestamp are skipped entirely.  Freshness loop passes
    ``last_check_ts - safety_buffer`` here.  Pass ``None`` for a full
    walk (manual re-index, first-time arming, periodic drift sweep).

    Per-scan_root dedupe
    --------------------
    Rapid Re-Index clicks used to fire concurrent scans on the same
    share.  Each finished fast (because the optimisation skipped
    most files), but they raced on the AOF flush and produced
    confusing "drift" items on the third or fourth scan.  Now: if
    this scan_root already appears in ``_current_remote_dirs``, log
    and return immediately — the in-flight scan will broadcast its
    own completion when done.

    Unlike the local scan which farms out file paths to worker processes,
    this downloads files via the source (which holds the connection) on a
    thread pool, then extracts metadata in worker processes.

    Incremental optimisation
    -------------------------
    Before the partial-fetch + mtime-skip work, this function blindly
    re-downloaded every file in the share on every re-index.  For a
    48K-file FLAC library on FTP, that's hours of network IO to
    re-read tag headers that hadn't changed.

    Now we:

      1. ``walk_with_stat`` returns ``(name, size, mtime)`` per file in
         the same MLSD response that already listed names — zero extra
         round trips.
      2. Pre-load the store's existing ``(mtime, size)`` per path under
         this scan_root.
      3. Classify each walked entry into one of three buckets:
           * **skip** — store mtime > 0 AND matches listing mtime+size
             → no work.
           * **mtime-refresh** — store mtime == 0 (legacy entry from
             before this change) AND size matches listing size → bump
             stored mtime so the NEXT scan can skip, no download.
           * **extract** — new file, or size/mtime drift → full path:
             partial fetch (if format budget allows) → mutagen → upsert.
      4. After processing, any store track under this scan_root whose
         path didn't appear in the walk is a ghost (file deleted on the
         remote) and gets purged.
    """
    global _progress, _scan_count
    from soniqboom.core.filesource import FileSource
    from soniqboom.core.metadata import HEADER_BUDGET
    from soniqboom.core.remote_cache import get_cache

    # Dedupe: drop if this scan_root is already being scanned.  The
    # in-flight task will broadcast its own completion event when
    # done; the user's rapid clicks shouldn't spawn parallel scans
    # against the same share (they raced on the AOF flush and produced
    # phantom "drift" items on subsequent scans).
    if scan_root in _current_remote_dirs:
        log.info(
            "Remote scan for %s already in progress — ignoring duplicate trigger",
            scan_root,
        )
        if on_progress:
            # Send a synthetic broadcast so the UI's "Re-Index" button
            # gets feedback instead of looking unresponsive.
            await on_progress(_progress)
        return

    loop = asyncio.get_event_loop()
    executor = ProcessPoolExecutor(max_workers=SCAN_WORKERS)
    cache = get_cache()

    # Walk with stat — preserves mtime+size per file from MLSD.
    #
    # ``dir_mtime_cap`` (when supplied by the freshness loop) prunes
    # subtrees whose dir.mtime is unchanged since the cap timestamp.
    # On the first walk OR when caller passes None, the full walk
    # runs unchanged.  Wrapped in ``functools.partial`` because
    # ``run_in_executor`` doesn't forward kwargs.
    import functools as _ft
    from soniqboom.config import settings as _settings   # local bind — not a module global
    _walk_fn = _ft.partial(
        _find_remote_audio_entries,
        dir_mtime_cap=dir_mtime_cap,
        scan_zips=_settings.scan_remote_zips,
    )
    entries, _pruned = await loop.run_in_executor(
        None, _walk_fn, scan_root, source,
    )

    # ── Classify against the store ────────────────────────────────────────
    #
    # Build the existing (path → (mtime, size, track_id)) map for this
    # scan_root so we can decide skip / refresh / extract in one in-
    # memory pass.  ``get_track_ids_for_scan_root`` is O(1) via the
    # scan_root_hash tag index, then we materialise the small subset.
    import hashlib
    from soniqboom.core.filesource import parse_remote_path

    scan_root_hash = hashlib.sha256(scan_root.encode()).hexdigest()[:16]
    store_local = get_store()
    existing_ids = store_local.get_track_ids_for_scan_root(scan_root_hash)
    existing_map: dict[str, tuple[float, int, str]] = {}
    if existing_ids:
        # Materialise track metas to read mtime + path + size.
        records = store_local.get_tracks_batch(list(existing_ids))
        for r in records:
            if not r:
                continue
            p = r.get("path") or ""
            # Stored path is the canonical ``ftp://host/share:/relative``.
            # Use parse_remote_path to extract the relative tail — the
            # earlier ``p.split(":", 1)[-1]`` mishandled this because
            # ``split(":", 1)`` peels off the SCHEME ("ftp"), leaving
            # ``//host/share:/relative`` — which can never match the
            # walked entry path ``/relative`` and so EVERY track
            # silently got re-extracted.  Local paths (no scheme) fall
            # through to ``p`` unchanged.
            try:
                _scan_root, rel = parse_remote_path(p)
            except ValueError:
                rel = p
            if not rel:
                # Bare share root with no file tail — skip; can't be a track.
                continue
            existing_map[rel] = (
                float(r.get("mtime", 0) or 0),
                int(r.get("file_size", 0) or 0),
                r.get("id", ""),
            )

    to_extract: list = []                          # full extract path
    to_refresh: list[tuple[str, float]] = []       # (track_id, new_mtime)
    seen_rel_paths: set[str] = set()
    for fe in entries:
        rel = fe.path  # already root-relative from walk_with_stat
        seen_rel_paths.add(rel)
        existing = existing_map.get(rel)
        if existing is None:
            to_extract.append(fe)
            continue
        stored_mtime, stored_size, tid = existing
        if stored_size == fe.size and stored_mtime > 0 and stored_mtime == fe.mtime:
            # Genuine match — skip entirely.
            continue
        if stored_size == fe.size and stored_mtime == 0 and fe.mtime > 0:
            # Legacy entry: size matches, mtime never captured.  Bump
            # it without re-downloading so the NEXT scan can skip.
            to_refresh.append((tid, fe.mtime))
            continue
        # Drift — re-extract.
        to_extract.append(fe)

    # Ghosts: store paths not seen on the remote → delete after scan.
    ghost_ids: list[str] = [
        tid for rel, (_m, _s, tid) in existing_map.items()
        if rel not in seen_rel_paths and tid
    ]

    total = len(to_extract)
    scan_plan = {
        "scan_root":     scan_root,
        "walked":        len(entries),
        "extract":       total,
        "mtime_refresh": len(to_refresh),
        "skip":          len(entries) - total - len(to_refresh),
        "ghosts":        len(ghost_ids),
    }
    log.info(
        "Remote scan plan for %s: walked=%d, extract=%d, mtime_refresh=%d, "
        "skip=%d, ghosts_to_delete=%d",
        scan_root, scan_plan["walked"], scan_plan["extract"],
        scan_plan["mtime_refresh"], scan_plan["skip"], scan_plan["ghosts"],
    )

    # Apply mtime-refresh in a single batched store call (no network).
    if to_refresh:
        from soniqboom.core.store import get_store as _gs
        _gs().update_track_fields_batch(
            [(tid, {"mtime": m}) for tid, m in to_refresh]
        )

    # Build remote_paths list (preserving order) for _process_one.
    remote_paths = [fe.path for fe in to_extract]
    # Map path → (size, mtime) so _process_one can stamp meta.mtime from
    # the listing without paying for an extra MDTM round trip.
    entry_stat_map: dict[str, tuple[int, float]] = {
        fe.path: (fe.size, fe.mtime) for fe in to_extract
    }

    # Additive progress: when a local scan is already running, add to the
    # existing total instead of overwriting it.
    if _scan_count > 0 and _progress.running:
        _progress.total += total
    else:
        _progress = ScanProgress(total=total, running=True)

    # Publish the plan via the shared ScanProgress object — MUST come
    # AFTER the conditional reset above, otherwise the ``_scan_count == 0``
    # branch replaces _progress with a fresh ScanProgress and wipes the
    # plan we just set.  (Pre-existing bug: the freshness loop reads
    # last_plan to count newly-extracted tracks; with the wipe in place
    # it always saw an empty plan and never logged anything as "fresh".)
    _progress.last_plan = scan_plan

    _scan_count += 1
    _current_remote_dirs.add(scan_root)
    log.info("Remote scan started: %d files to extract in %s", total, scan_root)

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

    # Sliding-window concurrency for remote downloads + metadata extraction —
    # the local scan path already uses INFLIGHT, but the remote path was
    # strictly serial.  Limit to ``_REMOTE_INFLIGHT`` concurrent transfers
    # so a slow share doesn't get hammered, but still overlap network I/O
    # with metadata extraction.
    #
    # Size the semaphore to follow the share's configured scan budget so
    # raising the slider in the FTP-pool UI actually widens the in-flight
    # download window.  Falls back to a sane default (8) when the pool
    # config / source backend doesn't expose a scan-lane budget.
    try:
        from soniqboom.core.filesource import _resolve_pool_size as _rps
        host = getattr(source, "_host", "")
        port = int(getattr(source, "_port", 0))
        if host and port:
            _max, _min, configured_total, detected = _rps(host, port)
            # Scan workers ≈ configured_total minus the 2 reserved-stream
            # default; clamp to the live effective_max so a server-cap
            # detection doesn't push us above the actual pool ceiling.
            _REMOTE_INFLIGHT = max(2, min(_max, configured_total - 2 or _max))
        else:
            _REMOTE_INFLIGHT = 8
    except Exception:
        _REMOTE_INFLIGHT = 8
    sem = asyncio.Semaphore(_REMOTE_INFLIGHT)
    log.info(
        "Remote scan window: %d concurrent transfers (pool-derived)",
        _REMOTE_INFLIGHT,
    )
    flush_lock = asyncio.Lock()

    # Live concurrency counters — separate from the FTP pool's in_use
    # because the pool tracks borrow scope, but we want to know how
    # many workers are in each phase (download vs extract vs flush).
    # Snapshot logged every 15 s by a background task so the operator
    # can confirm parallelism without running py-spy.
    _phase_counts = {"download": 0, "extract": 0, "flush": 0}
    _phase_lock = threading.Lock()  # cheap — bumped from coroutines only

    async def _phase_logger() -> None:
        """Print pool + per-phase worker stats every 10 s during the
        scan so concurrency can be observed in the log.

        Without this it's hard to tell from a single UI snapshot
        whether the scanner is bottlenecked on download (FTP pool
        saturated, ``in_use=6``) or on extract (workers in
        ProcessPool, ``in_use=0``) or genuinely serialised
        (``in_use=1`` consistently).  First sample at 5 s so the
        operator sees feedback quickly; steady-state at 10 s.
        """
        try:
            await asyncio.sleep(5)
            while True:
                with _phase_lock:
                    phases = dict(_phase_counts)
                try:
                    pool_status = source._pool.status()  # FTP pool only
                    pool_str = (
                        f"pool[in_use={pool_status.get('in_use', '?')}"
                        f" idle={pool_status.get('idle', '?')}"
                        f" max={pool_status.get('max_size', '?')}"
                        f" waiting_scan={pool_status.get('waiting_scan', '?')}"
                        f" waiting_stream={pool_status.get('waiting_stream', '?')}]"
                    )
                except (AttributeError, Exception):
                    pool_str = "pool[n/a]"
                log.info(
                    "scan concurrency: workers[download=%d extract=%d flush=%d]"
                    " window=%d/%d %s  processed=%d/%d",
                    phases["download"], phases["extract"], phases["flush"],
                    _REMOTE_INFLIGHT - sem._value, _REMOTE_INFLIGHT,  # noqa: SLF001
                    pool_str,
                    _progress.processed, _progress.total,
                )
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

    # Compiled once.  Mutagen's title-fallback path uses
    # ``tempfile.NamedTemporaryFile`` whose stem is ``tmp[8 alnum]``;
    # observing that title means extract() FAILED and the fallback
    # kicked in — a definitive partial-fetch undershoot signal that
    # doesn't depend on guessing what "looks like" the filename stem.
    import re as _re_inc
    _TEMPFILE_TITLE_RE = _re_inc.compile(r"^tmp[a-zA-Z0-9_]{6,12}$")

    def _extract_looks_incomplete(meta, remote_path: str) -> bool:
        """Heuristic: did a partial fetch under-shoot the budget?

        Three signals, any of which means "the bytes we got weren't
        enough to read the tag block":

        1. ``meta.title`` matches ``tmp[alnum]+`` — that's mutagen's
           internal tempfile basename leaking through because the
           real tag parse failed and ``extract()`` fell back to
           ``path.stem``.  Definitive: a real song will never be
           titled that.

        2. ``meta.title`` equals the REMOTE-side filename stem
           combined with ``duration == 0`` — weaker signal but
           catches the case where mutagen returned cleanly but
           with no useful data.

        3. ``meta.title`` empty AND ``duration == 0`` — same idea.

        Only consulted on the partial-fetch path; a successful full
        fetch with the same minimal data means the source really has
        no tags, which is not a failure.
        """
        if _TEMPFILE_TITLE_RE.match(meta.title or ""):
            return True
        stem = os.path.splitext(os.path.basename(remote_path))[0]
        title_missing = (not meta.title) or (meta.title == stem)
        duration_missing = (meta.duration or 0) == 0
        return title_missing and duration_missing

    # Growing-budget escalation ladder.  We start at ``HEADER_BUDGET[ext]``
    # and on insufficient-data we step UP rather than jumping to a full
    # fetch.  4× the base is enough for the long-tail of Hi-Res FLACs
    # with 1–3 MB embedded cover art; anything past that is almost
    # certainly multi-MB art for which the full fetch wins anyway.
    _GROWING_BUDGET_MULTIPLIERS = (1, 4)

    async def _fetch_zip_member(remote_path: str) -> bytes:
        """Fetch one member of a remote archive (.zip/.lha/.lzh): download the
        outer archive via the local remote-cache (once per archive), then
        extract the member.  ``remote_path`` is ``<archive_rel>::<member>``."""
        arc_rel, member = remote_path.split("::", 1)

        def _read() -> bytes:
            from soniqboom.core.remote_cache import get_cache
            local_archive = get_cache().fetch(scan_root, arc_rel, source)
            return archive.read_member(local_archive, member)

        return await loop.run_in_executor(None, _read)

    async def _fetch_partial(remote_path: str, budget: int) -> bytes:
        """One stage of partial fetch.  Logs the actual bytes returned
        so the operator can see whether the file was smaller than the
        budget (early-EOF) or the server delivered the full request.
        """
        if "::" in remote_path:        # ZIP member — fetch the whole member
            return await _fetch_zip_member(remote_path)
        return await loop.run_in_executor(
            None, lambda: source.read_partial(
                remote_path, budget, lane="scan",
            ),
        )

    async def _fetch_full(remote_path: str) -> bytes:
        if "::" in remote_path:
            return await _fetch_zip_member(remote_path)
        return await loop.run_in_executor(
            None, lambda: source.read_file(remote_path, lane="scan"),
        )

    async def _process_one(remote_path: str) -> None:
        nonlocal track_count
        # Honour the pause flag BEFORE acquiring the semaphore so a
        # paused scan doesn't tie up the in-flight window with workers
        # parked on the gate — let other slots stay free for any
        # higher-priority work that comes in.
        await _await_resume()
        async with sem:
            # Re-check after sem acquisition: a pause could have been
            # requested while we were queueing.  Cheap; resumes
            # instantly when running.
            await _await_resume()
            # Decide the fetch strategy based on extension + listing size.
            #   * ``HEADER_BUDGET[ext] is None`` → must fetch full.
            #   * File smaller than ~2× base budget → cheaper to fetch
            #     full (ABOR overhead exceeds the bytes saved).
            #   * Else → growing partial: try base budget, then 4×,
            #     then full.  Each stage extracts and re-checks the
            #     "looks incomplete" heuristic; we only escalate if
            #     the current stage's bytes didn't satisfy mutagen.
            from soniqboom.core.metadata import HEADER_BUDGET as _HB
            ext = os.path.splitext(remote_path.lower())[1]
            base_budget = _HB.get(ext)
            listing_size = entry_stat_map.get(remote_path, (0, 0.0))[0]

            track_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scan_root}:{remote_path}"))
            parent_dir = str(PurePosixPath(remote_path).parent) if "/" in remote_path else scan_root

            # Build the budget ladder: a list of (label, budget_bytes
            # or None) to try in order.  ``None`` budget = full fetch.
            stages: list[tuple[str, int | None]] = []
            if base_budget is None or (listing_size and listing_size <= base_budget * 2):
                stages.append(("full", None))
            else:
                for mult in _GROWING_BUDGET_MULTIPLIERS:
                    b = base_budget * mult
                    if listing_size and b >= listing_size:
                        # No point fetching more than the file size — promote
                        # this stage straight to full so we benefit from the
                        # simpler RETR (no ABOR dance).
                        stages.append(("full", None))
                        break
                    stages.append((f"{b//1024}KB", b))
                else:
                    stages.append(("full", None))

            file_data: bytes | None = None
            result = None
            was_partial = False
            for stage_idx, (label, stage_budget) in enumerate(stages):
                with _phase_lock:
                    _phase_counts["download"] += 1
                try:
                    if stage_budget is None:
                        file_data = await _fetch_full(remote_path)
                        was_partial = False
                    else:
                        file_data = await _fetch_partial(remote_path, stage_budget)
                        was_partial = True
                except Exception as exc:
                    with _phase_lock:
                        _phase_counts["download"] -= 1
                    # If a PARTIAL fetch fails (e.g. ABOR glitch), step
                    # to the next stage — don't drop the file.  If the
                    # FULL fetch fails, we're out of options.
                    if stage_budget is None:
                        log.error("Download failed %s: %s", remote_path, exc)
                        _progress.errors += 1
                        _progress.processed += 1
                        if on_progress and (
                            _progress.processed % PROGRESS_EVERY == 0
                            or _progress.processed == total
                        ):
                            await on_progress(_progress)
                        return
                    log.warning(
                        "Partial fetch %s failed for %s: %s — escalating",
                        label, remote_path, exc,
                    )
                    continue
                with _phase_lock:
                    _phase_counts["download"] -= 1
                    _phase_counts["extract"] += 1
                try:
                    _, result, _, _ = await loop.run_in_executor(
                        executor, _extract_one_remote,
                        file_data, remote_path, track_id,
                    )
                finally:
                    with _phase_lock:
                        _phase_counts["extract"] -= 1
                # Was that stage's data enough?  Only escalate on
                # PARTIAL fetches — a full fetch that returns minimal
                # data is the source's real metadata, not under-shoot.
                if not was_partial:
                    break
                bad = isinstance(result, str) or (
                    not isinstance(result, str)
                    and _extract_looks_incomplete(result, remote_path)
                )
                if not bad:
                    break
                # Escalate to next stage if any, else give up.
                if stage_idx < len(stages) - 1:
                    next_label = stages[stage_idx + 1][0]
                    log.info(
                        "Partial fetch %s insufficient for %s — escalating to %s",
                        label, remote_path, next_label,
                    )

            if isinstance(result, str):
                log.error("Metadata error %s: %s", remote_path, result)
                _progress.errors += 1
            else:
                meta: TrackMeta = result
                meta.path = f"{scan_root}:{remote_path}"
                # Stamp mtime + file_size from the directory listing.
                # Without this, the NEXT scan would see ``stored mtime
                # == 0`` and re-extract every file, defeating the
                # whole point of the mtime-skip optimisation.  Listing
                # values are canonical (MLSD response) and don't drift
                # between the directory call and the per-file fetch.
                listing_size, listing_mtime = entry_stat_map.get(
                    remote_path, (0, 0.0),
                )
                if listing_mtime > 0:
                    meta.mtime = listing_mtime
                if listing_size > 0:
                    meta.file_size = listing_size
                if parent_dir not in hash_map:
                    new_hashes = await store_hash_lookups_batch([parent_dir])
                    hash_map.update(new_hashes)
                track, raw_art = _build_track(meta, scan_root, parent_dir, hash_map)
                if track:
                    # Serialise buffer growth + flush so two concurrent
                    # workers can't both observe ``len(track_buffer) ==
                    # WRITE_BATCH`` and double-flush the same chunk.
                    async with flush_lock:
                        track_buffer.append(track)
                        if raw_art:
                            art_buffer[track.id] = raw_art
                        track_count += 1
                        if len(track_buffer) >= WRITE_BATCH:
                            await _flush()

            _progress.processed += 1
            _progress.current_file = PurePosixPath(remote_path).name
            if on_progress and (
                _progress.processed % PROGRESS_EVERY == 0
                or _progress.processed == total
            ):
                await on_progress(_progress)

    # Kick off the phase-logger so we get periodic concurrency stats
    # in the log while the gather runs.  Cancel + await on exit so a
    # dangling task doesn't leak across scans.
    _phase_task = asyncio.create_task(_phase_logger(), name="scan.phase_logger")

    # Warm the shared folder-art cache for every unique parent
    # directory observed in the walk.  Runs CONCURRENTLY with the
    # main extract gather so we overlap idle FTP slots (a 1000-track
    # album walks one dir and produces one prefetch task, while the
    # extract loop saturates the pool with 1000 file fetches).
    # Bounded to a small in-flight window so we don't compete too
    # hard with the extract path for pool slots.
    #
    # DETACHED — we do NOT await this in the scan's finally block.
    # Reason: large shares can have thousands of dirs, and prefetching
    # all of them takes minutes.  Earlier code awaited with a 60 s
    # timeout, which blocked the scan-complete WebSocket broadcast
    # for 60 s on every re-index of a large share (observed on the
    # "Anime Music" share).  The prefetch is best-effort cache
    # warming; the scan should report "complete" as soon as the
    # extract work is done.  The prefetch task continues in the
    # background; if it's still running when the next scan starts,
    # both run in parallel (bounded by per-task semaphores) and the
    # cache writes are idempotent.
    #
    # We register the task in a module-level set so it isn't garbage-
    # collected while still running (asyncio holds only a weak ref to
    # tasks created by create_task).  The done_callback discards on
    # completion and logs at INFO so the stats land in the log.
    if entries:
        _art_task = asyncio.create_task(
            _prefetch_folder_art_remote(scan_root, source, entries),
            name=f"scan.art_prefetch[{scan_root}]",
        )
        _art_prefetch_tasks.add(_art_task)
        _art_task.add_done_callback(_art_prefetch_done)
    try:
        await asyncio.gather(*(_process_one(p) for p in remote_paths))
    finally:
        _phase_task.cancel()
        try:
            await _phase_task
        except (asyncio.CancelledError, Exception):
            pass

    await _flush()
    await _async_exit_batch_mode(store)

    # Ghost-track cleanup: any track in the store under this scan_root
    # whose path didn't appear in the live walk is a file that was
    # deleted (or moved out from under us) on the remote since the last
    # index.  Drop them so they don't show up in the library forever as
    # "phantom" entries that fail to play.
    #
    # SAFETY: only purge when the walk produced AT LEAST ONE entry.
    # An empty entries list usually means the share is unreachable
    # mid-scan (auth expired, FTP server bounced) and we'd otherwise
    # nuke the entire share's tracks.  ``len(entries) > 0`` is the
    # signal that we're looking at real ground truth, not a network
    # failure.
    if ghost_ids and len(entries) > 0:
        try:
            removed = await delete_track_ids(ghost_ids)
            log.info(
                "Ghost cleanup for %s: removed %d track(s) whose remote "
                "files no longer exist",
                scan_root, removed,
            )
        except Exception as exc:
            log.warning("Ghost cleanup for %s failed: %s", scan_root, exc)

    await upsert_scan_dir(scan_root, track_count_val=track_count,
                          network_share_id=share_id, status="ok")

    _current_remote_dirs.discard(scan_root)
    _scan_count = max(0, _scan_count - 1)
    is_last_scan = (_scan_count == 0)
    if is_last_scan:
        _progress.running = False
        _progress.embedding = False
        _progress.current_file = ""
        # Snap processed up to total ONLY when this is the last scan
        # finishing.  Earlier code did this unconditionally, which broke
        # parallel scans: the first task to finish would snap processed
        # to the aggregate total (since _progress.processed/total are
        # globals shared across all in-flight scans), then the other
        # tasks' continued increments pushed processed PAST total →
        # the "100% — 137,034 / 76,952" display the user reported.
        if _progress.processed < _progress.total:
            _progress.processed = _progress.total

    # Always broadcast the (possibly partial) state so the badge gets
    # an updated count.  When this is the last scan we just guaranteed
    # processed == total above; when it isn't, the per-scan share of
    # the work is reflected in ``_progress.processed`` already from
    # the per-file increments.
    if on_progress:
        await on_progress(_progress)

    executor.shutdown(wait=False)
    log.info("Remote scan complete: %d tracks from %s", track_count, scan_root)


# ── Drill-down freshness ──────────────────────────────────────────────────────

async def refresh_subtree_under_root(
    root: str, subdir: str, max_files: int = 5000,
) -> dict:
    """Freshness pass for ONE folder subtree under an EXISTING scan root.

    The redesign the disabled drill-down refresh was waiting for: unlike
    ``start_scan([subdir])`` this never calls ``upsert_scan_dir`` (so browsed
    folders don't appear as top-level roots) and every track keeps its
    attribution to *root*.  New and changed files are extracted and upserted;
    index entries whose files vanished are removed.

    Safety rails:
      * subtrees above ``max_files`` are skipped (use Re-Index for those);
      * removals only happen when ``subdir`` still exists as a directory
        (an unmounted share must never mass-orphan its tracks), and are
        capped per pass — a partial walk on a flaky mount can't wipe a
        folder's index.
    """
    from soniqboom.config import settings as _settings

    loop = asyncio.get_event_loop()
    store = get_store()
    sub = str(Path(subdir).resolve())

    dir_files = await loop.run_in_executor(
        None, _find_audio_files, [sub], _settings.scan_zips,
    )
    files_strs = [str(p) for fl in dir_files.values() for p in fl]
    if len(files_strs) > max_files:
        return {"skipped": True, "checked": len(files_strs),
                "added": 0, "updated": 0, "removed": 0, "errors": 0}
    found = set(files_strs)

    # Existing index entries under this subtree (scoped to the parent root).
    prefix = sub.rstrip("/") + "/"
    existing: dict[str, dict] = {}
    for tid in await get_track_ids_for_scan_root(root):
        t = store._tracks.get(tid)
        if t:
            p_str = t.get("path") or ""
            if p_str.startswith(prefix):
                existing[p_str] = t

    mtime_size_map = {
        t["id"]: (t.get("mtime"), t.get("file_size")) for t in existing.values()
    }
    fresh, _tid_map = await loop.run_in_executor(
        None, _compute_incremental, files_strs, mtime_size_map,
    )
    to_scan = [s for s in files_strs if s not in fresh]

    added = updated = errors = 0
    if to_scan:
        def _pdir(s: str) -> str:
            return str(Path(s.split("::")[0]).parent) if "::" in s else str(Path(s).parent)

        unique_dirs = list({_pdir(s) for s in to_scan} | {root})
        hash_map = await store_hash_lookups_batch(unique_dirs)
        buf: list = []
        for s in to_scan:
            _p, result, _sm, _lg = await loop.run_in_executor(None, _extract_one, Path(s))
            if isinstance(result, str):
                errors += 1
                continue
            track, _art = _build_track(result, root, _pdir(s), hash_map)
            if not track:
                errors += 1
                continue
            if store.get_track(track.id) is None:
                added += 1
            else:
                updated += 1
            buf.append(track)
            if len(buf) >= 200:
                await upsert_tracks_batch(buf)
                buf = []
        if buf:
            await upsert_tracks_batch(buf)

    removed = 0
    gone = [t["id"] for p_str, t in existing.items() if p_str not in found]
    if gone and Path(sub).is_dir():
        cap = max(20, len(existing) // 3)
        if len(gone) <= cap:
            from soniqboom.core.data import delete_track_ids
            removed = await delete_track_ids(gone)
        else:
            log.warning(
                "Drill-down refresh %s: %d of %d tracks vanished — over the "
                "safety cap (%d), leaving the index untouched (full Re-Index "
                "will reconcile).", sub, len(gone), len(existing), cap,
            )

    return {"added": added, "updated": updated, "removed": removed,
            "checked": len(files_strs), "errors": errors, "skipped": False}
