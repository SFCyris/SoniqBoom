# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Repair task — re-extracts metadata for tracks whose titles/artists/albums
were corrupted by the old ``decode("ascii", errors="replace")`` path in
``metadata.py``.

Background
----------
The tracker / chiptune extractor used to decode fixed-size header bytes
as strict ASCII with ``errors='replace'``.  Every byte ≥ 0x80 (very
common in CP437 / Latin-1 / Shift-JIS demoscene files) was rewritten as
U+FFFD (the diamond ``�``) and persisted to the index.  The decoder is
fixed (`metadata._decode_tracker_str` does UTF-8 → CP437 → Latin-1) but
the index still holds the garbled strings — the scanner's incremental
mtime check would otherwise skip these files forever.

This module finds those tracks and re-runs the extractor in-place so
the corruption clears without forcing a full destructive rescan.  It
broadcasts progress as ``repair_progress`` WS events so the admin UI
can render the same kind of badge the scanner uses.

Identification heuristic
------------------------
A track is a candidate when any of ``title``, ``artist``, ``album``,
``album_artist`` contains the U+FFFD replacement character (a string
that practically never occurs in legitimate audio metadata — when it
does, the entry is almost certainly mojibake from a bad decode).

Scope
-----
The decoder fix only affects tracker / chiptune containers — but the
caller may choose to filter by extension to avoid the network cost of
re-downloading remote FLAC / DSD files that the fix would not change.
Extension filter is opt-in; default is "any U+FFFD-tainted track".
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("soniqboom.repair")

# Sentinel char inserted by the broken ASCII-replace decode.
_FFFD = "�"

# Extensions where the bad decoder lived.  Useful as an optional filter
# — for everything else the corruption signal would have to come from a
# different bug, and re-extracting wouldn't help.
TRACKER_LIKE_EXTS: frozenset[str] = frozenset({
    # Chiptune / SID family
    ".sid", ".psid", ".rsid",
    # GME family (Game Music Emu containers)
    ".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz", ".ay", ".kss",
    ".sap", ".gym", ".hes",
    # Tracker formats
    ".mod", ".s3m", ".it", ".xm", ".mtm", ".669", ".med",
})


# ── Progress state ───────────────────────────────────────────────────────────

# Cap the per-run error sample so a 100K-track library with a million
# stale paths doesn't balloon the progress dict.  The UI only needs
# enough to show the operator a representative slice.
_ERROR_SAMPLE_CAP = 50


@dataclass
class RepairProgress:
    running:    bool  = False
    total:      int   = 0
    processed:  int   = 0
    repaired:   int   = 0      # tracks where at least one field changed
    errors:     int   = 0
    # Counts of each error reason — surfaced in the UI so the operator
    # can see "ah, 869 zip-virtual, 2 ftp" rather than just "871".
    error_reasons: dict[str, int] = field(default_factory=dict)
    # First N (path, reason) tuples for the operator to inspect.
    error_samples: list[tuple[str, str]] = field(default_factory=list)
    current_file: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancelled:  bool  = False

    def pct(self) -> int:
        return min(100, int(self.processed / self.total * 100)) if self.total else 0

    def record_error(self, path: str, reason: str) -> None:
        """Bump counters and append to the sample list (capped)."""
        self.errors += 1
        # Take just the prefix before the colon so similar errors group
        # together (e.g. "zip-error: KeyError: 'foo'" all roll up under
        # "zip-error").
        key = reason.split(":", 1)[0] if ":" in reason else reason
        self.error_reasons[key] = self.error_reasons.get(key, 0) + 1
        if len(self.error_samples) < _ERROR_SAMPLE_CAP:
            self.error_samples.append((path, reason))

    def to_dict(self) -> dict:
        return {
            "running":      self.running,
            "total":        self.total,
            "processed":    self.processed,
            "repaired":     self.repaired,
            "errors":       self.errors,
            "error_reasons": dict(self.error_reasons),
            "error_samples": [
                {"path": p, "reason": r} for p, r in self.error_samples
            ],
            "pct":          self.pct(),
            "current_file": self.current_file,
            "started_at":   self.started_at,
            "finished_at":  self.finished_at,
            "cancelled":    self.cancelled,
        }


_progress = RepairProgress()
_task: asyncio.Task | None = None
_cancel_event: asyncio.Event | None = None


def get_progress() -> RepairProgress:
    return _progress


def is_running() -> bool:
    return _progress.running


def request_cancel() -> bool:
    """Ask the running repair task to stop after the current file.
    Returns True if a task was running, False otherwise."""
    if _cancel_event is None or not _progress.running:
        return False
    _cancel_event.set()
    log.info("Repair cancel requested — will stop after current file")
    return True


# ── Identification ───────────────────────────────────────────────────────────

def _has_replacement_char(track: dict) -> bool:
    """True iff title/artist/album/album_artist contains U+FFFD."""
    for key in ("title", "artist", "album", "album_artist"):
        v = track.get(key)
        if isinstance(v, str) and _FFFD in v:
            return True
    return False


def find_corrupt_tracks(*, tracker_only: bool = False) -> list[dict]:
    """Walk the in-memory store and return candidate track dicts.

    ``tracker_only`` filters to extensions in :data:`TRACKER_LIKE_EXTS`
    so the operator can avoid network I/O on FLAC / DSD shares that the
    decoder fix wouldn't help anyway.
    """
    from soniqboom.core.store import get_store

    store = get_store()
    out: list[dict] = []
    for t in store.all_tracks():
        if not _has_replacement_char(t):
            continue
        if tracker_only:
            ext = os.path.splitext((t.get("path") or "").lower())[1]
            if ext not in TRACKER_LIKE_EXTS:
                continue
        out.append(t)
    return out


# ── Per-track re-extraction ──────────────────────────────────────────────────

# Fields the decoder fix can actually improve.  Restricted to the
# text fields the tracker / chiptune extractors run through
# ``_decode_tracker_str``: title, artist, album / album_artist
# (filled from copyright-line parsing), composer / comment / label
# (occasional secondary text headers), year (parsed from copyright
# text), and ``instruments`` (tracker per-instrument name list).
#
# We deliberately do NOT overwrite numeric audio fields
# (``duration``, ``bitrate``, ``sample_rate``, ``channels`` …).
# The decoder fix can't affect them, and rewriting them with a
# fresh extraction risks reverting any user / scanner-side
# corrections that landed after the original ingest.
_REPAIRABLE_FIELDS: tuple[str, ...] = (
    "title", "artist", "album_artist", "album",
    "year",
    "composer", "comment", "label",
    "instruments",
)


def _changed_fields(old: dict, new: dict) -> dict:
    """Return a sub-dict of *new* whose values differ from *old*.

    Only considers keys in :data:`_REPAIRABLE_FIELDS`.  The point is to
    avoid emitting a no-op AOF record (and busting indexes) for files
    where the fixed decoder produced the same string anyway.
    """
    out: dict = {}
    for k in _REPAIRABLE_FIELDS:
        if k not in new:
            continue
        if old.get(k) != new[k]:
            out[k] = new[k]
    return out


def _re_extract_local_sync(
    path_str: str, track_id: str,
) -> tuple[dict | None, str | None]:
    """Run mutagen / format extractor on a local path.  Sync, blocking.

    Returns ``(extracted_dict, error_reason)``.  Exactly one of the two
    is non-None.  Caller filters the dict down to changed fields.

    Handles three local-style paths:

      * Plain file on disk: ``/Volumes/Music/foo.sid``
      * Zip-contained virtual path: ``/path/a.zip::inner.it``
      * Nested-zip virtual path: ``/path/a.zip::b.zip::track.s3m``

    The previous version called ``Path(path).exists()`` first and
    returned silently when the path didn't exist on the filesystem —
    which made every zip-virtual path look like a missing file (869
    of 871 errors from the first repair run were exactly this case).
    """
    from soniqboom.core.metadata import extract

    # Zip-virtual path: delegate to the scanner's zip extractor which
    # peels off ``::`` segments, reads the innermost member into a
    # tempfile, and calls ``extract()`` on that.
    if "::" in path_str:
        try:
            from soniqboom.core.scanner import _extract_from_zip
            meta = _extract_from_zip(path_str, track_id)
            d = meta.model_dump()
            # ``_extract_from_zip`` already rewrites ``path`` to the
            # virtual path, so we don't need to fix it up here.
            return d, None
        except FileNotFoundError as exc:
            return None, f"zip-missing: {exc}"
        except Exception as exc:
            log.warning("Repair: zip extract failed for %s: %s", path_str, exc)
            return None, f"zip-error: {type(exc).__name__}: {exc}"

    # Plain file on disk.
    p = Path(path_str)
    if not p.exists():
        return None, "local-missing"
    try:
        meta = extract(p, track_id)
        return meta.model_dump(), None
    except Exception as exc:  # pragma: no cover — extractor catches its own
        log.warning("Repair: local extract failed for %s: %s", path_str, exc)
        return None, f"local-error: {type(exc).__name__}: {exc}"


def _re_extract_remote_sync(
    file_data: bytes, remote_path: str, track_id: str,
) -> tuple[dict | None, str | None]:
    """Re-run the extractor against an already-downloaded byte buffer.

    Returns ``(extracted_dict, error_reason)`` — same contract as
    :func:`_re_extract_local_sync`.
    """
    from soniqboom.core.metadata import extract

    ext = os.path.splitext(remote_path)[1]
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)
        meta = extract(tmp_path, track_id)
        d = meta.model_dump()
        d["path"] = remote_path
        d["file_size"] = len(file_data)
        if meta.title == tmp_path.stem:
            real_stem = Path(os.path.basename(remote_path)).stem
            if real_stem:
                d["title"] = real_stem
        return d, None
    except Exception as exc:
        log.warning("Repair: remote extract failed for %s: %s", remote_path, exc)
        return None, f"remote-error: {type(exc).__name__}: {exc}"
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def _broadcast_progress(p: RepairProgress) -> None:
    """Best-effort broadcast — never raises."""
    try:
        from soniqboom.api.library import _broadcast
        await _broadcast({"event": "repair_progress", **p.to_dict()})
    except Exception as exc:  # pragma: no cover
        log.debug("repair_progress broadcast failed: %s", exc)


async def _process_local(t: dict) -> tuple[bool, bool, str | None]:
    """Re-extract a local track.

    Returns ``(success, applied_change, error_reason)``.  ``success`` is
    False only on hard error; ``applied_change`` says whether we wrote
    anything to the store; ``error_reason`` carries a short tag the
    progress sampler can group on.
    """
    from soniqboom.core.store import get_store

    tid = t["id"]
    path_str = t.get("path") or ""

    loop = asyncio.get_running_loop()
    new_meta, err = await loop.run_in_executor(
        None, _re_extract_local_sync, path_str, tid,
    )
    if new_meta is None:
        return False, False, err

    delta = _changed_fields(t, new_meta)
    if not delta:
        return True, False, None

    store = get_store()
    if store.update_track_fields(tid, delta):
        return True, True, None
    return True, False, "store-rejected"


async def _process_remote(t: dict, source_lookup) -> tuple[bool, bool, str | None]:
    """Re-extract a remote track via FTP/SMB/WebDAV.

    Same ``(success, applied_change, error_reason)`` shape as
    :func:`_process_local`.

    ``source_lookup`` is kept as a parameter for test stub-ability but
    is no longer used — the canonical lookup pattern in the rest of
    the codebase (stream.py / tracks.py / art.py) is
    ``parse_remote_path`` → ``get_source(scan_root)``.  The earlier
    ``find_source_for_path`` prefix-match treated the share/path
    separator (``:``) as part of the URL host+path, so FTP paths of
    the form ``ftp://h/share:/relative`` never matched the
    registered scan-root key ``ftp://h/share``.  This was the
    "remote-no-source" failure for the 2 Suara DSD tracks.
    """
    from soniqboom.core.store import get_store
    from soniqboom.core.filesource import parse_remote_path, get_source

    tid = t["id"]
    path_str = t.get("path") or ""

    try:
        scan_root, remote_subpath = parse_remote_path(path_str)
    except ValueError:
        # Not a recognised remote URL — shouldn't reach this branch
        # because the caller already checked _is_remote(), but guard
        # anyway so the repair task doesn't crash on a malformed row.
        return False, False, "remote-malformed-url"

    if not remote_subpath:
        # URL points at the share root, not a file.  No-op.
        return False, False, "remote-share-root-only"

    source = get_source(scan_root)
    if source is None:
        return False, False, "remote-no-source"

    loop = asyncio.get_running_loop()
    try:
        # ``lane='scan'`` so this borrows from the scan pool, not the
        # streaming pool — keeps audio playback responsive while the
        # repair churns through hundreds of files.
        data: bytes = await loop.run_in_executor(
            None, lambda: source.read_file(remote_subpath, lane="scan"),
        )
    except Exception as exc:
        log.warning("Repair: download failed for %s: %s", path_str, exc)
        return False, False, f"remote-download: {type(exc).__name__}: {exc}"

    new_meta, err = await loop.run_in_executor(
        None, _re_extract_remote_sync, data, path_str, tid,
    )
    if new_meta is None:
        return False, False, err

    delta = _changed_fields(t, new_meta)
    if not delta:
        return True, False, None

    store = get_store()
    if store.update_track_fields(tid, delta):
        return True, True, None
    return True, False, "store-rejected"


def _is_remote(path: str) -> bool:
    return path.startswith(("ftp://", "ftps://", "smb://", "webdav://",
                            "webdavs://", "http://", "https://"))


async def _run_repair(
    candidates: list[dict],
    *,
    cancel_event: asyncio.Event,
    on_progress: Callable[[RepairProgress], Awaitable[None]] | None = None,
    progress_every: int = 10,
) -> None:
    """Inner driver — must be called only from :func:`start_repair`.

    Note: the global ``_progress`` is initialised by :func:`start_repair`
    *before* the task is scheduled — that way callers polling
    ``is_running()`` immediately after ``start_repair`` see the right
    state without having to await the task's first instruction.
    """
    from soniqboom.core.filesource import find_source_for_path

    if on_progress:
        await on_progress(_progress)

    if not candidates:
        _progress.running = False
        _progress.finished_at = time.time()
        if on_progress:
            await on_progress(_progress)
        return

    try:
        for t in candidates:
            if cancel_event.is_set():
                _progress.cancelled = True
                break

            path_str = t.get("path") or ""
            _progress.current_file = os.path.basename(path_str) or path_str

            try:
                if _is_remote(path_str):
                    ok, applied, err = await _process_remote(t, find_source_for_path)
                else:
                    ok, applied, err = await _process_local(t)
            except Exception as exc:
                log.exception("Repair: unexpected error for %s: %s",
                              path_str, exc)
                ok, applied, err = False, False, f"unexpected: {type(exc).__name__}: {exc}"

            if not ok:
                _progress.record_error(path_str, err or "unknown")
            if applied:
                _progress.repaired += 1
            _progress.processed += 1

            if on_progress and (
                _progress.processed % progress_every == 0
                or _progress.processed == _progress.total
            ):
                await on_progress(_progress)
    finally:
        _progress.running = False
        _progress.finished_at = time.time()
        if on_progress:
            await on_progress(_progress)
        log.info(
            "Repair finished: total=%d processed=%d repaired=%d errors=%d cancelled=%s",
            _progress.total, _progress.processed, _progress.repaired,
            _progress.errors, _progress.cancelled,
        )


async def start_repair(candidates: list[dict]) -> bool:
    """Start the repair task in the background.

    Returns False if a repair is already running (the caller should
    cancel first or wait for completion).  True if we kicked off a
    fresh task.

    The global ``_progress`` is initialised *before* ``create_task``
    returns — so callers that immediately poll :func:`is_running` see
    ``True`` rather than racing the task's first ``await``.
    """
    global _task, _cancel_event, _progress

    if _progress.running:
        return False

    _progress = RepairProgress(
        running=True,
        total=len(candidates),
        started_at=time.time(),
    )

    _cancel_event = asyncio.Event()
    _task = asyncio.create_task(
        _run_repair(
            candidates,
            cancel_event=_cancel_event,
            on_progress=_broadcast_progress,
        ),
        name="soniqboom.repair",
    )
    return True
