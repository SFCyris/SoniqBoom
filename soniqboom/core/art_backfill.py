# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remote embedded-album-art backfill.

The scanner records, per track, whether the file carried an embedded cover
(``cover_art`` becomes ``/api/art/<id>`` vs ``None``), but for tracks scanned
before art was persisted for remote sources the cover BYTES may not be in the
art cache.  Re-reading a 50 MB FTP file just to recover a 200 KB cover on every
art request would be unusable, and a full re-scan is the blunt alternative.

This module does the surgical thing instead: fetch only the bytes where the
cover actually lives, lazily and in the background, then push an ``art_ready``
event so the UI fills in the placeholder without a reload.

Where the cover lives is format-deterministic:
  * MP3 (ID3v2) / FLAC (PICTURE) / Ogg/Opus (METADATA_BLOCK_PICTURE) → the
    FRONT.  One ``read_partial`` of the tag-header budget captures it.
  * MP4 / M4A / AAC → the ``moov`` atom (carrying ``udta.meta.ilst.covr``),
    which can sit at the FRONT (fast-start, e.g. iTunes) or the END.  We walk
    the top-level atom table reading only 8/16-byte atom HEADERS (cheap range
    reads, never the ``mdat`` audio), find ``moov``'s offset + size, fetch just
    that atom, and hand mutagen a COMPACT ``ftyp + empty-mdat + moov`` file —
    mutagen walks atoms sequentially and only needs ``ftyp`` + ``moov`` for
    tags, so we never reconstruct the multi-MB audio.  Round-trips, not bytes,
    dominate over FTP, so we minimise reads (``tag_window.mp4_window``).

Orchestration: coalesced (one in-flight task per track), bounded concurrency,
and a short cooldown on failure so a genuinely-unreadable file isn't retried in
a storm.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

_MP4_EXTS = {".m4a", ".mp4", ".aac", ".m4b", ".m4p"}
_DEFAULT_FRONT = 1024 * 1024      # fallback front budget for non-MP4 formats

_MAX_CONCURRENCY = 4
_NEG_COOLDOWN_S = 300.0           # don't retry a failed backfill for 5 minutes
_NEG_MAX = 1024                   # prune the failure map past this many entries

_inflight: set[str] = set()
_neg: dict[str, float] = {}       # track_id -> monotonic time of last failure
_tasks: set[asyncio.Task] = set()
_sem: asyncio.Semaphore | None = None


def _is_remote(path_str: str) -> bool:
    from soniqboom.core.filesource import is_remote_path
    return is_remote_path(path_str)


def _get_sem() -> asyncio.Semaphore:
    # Created lazily, inside the running loop, so it always binds to the right
    # event loop (bullet-proofing beyond the 3.11+ lazy-binding behaviour).
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    return _sem


def _record_failure(tid: str) -> None:
    now = time.monotonic()
    _neg[tid] = now
    if len(_neg) > _NEG_MAX:
        # Drop expired entries first; if a burst of distinct failures still
        # leaves us over the cap, evict the oldest so the map stays bounded.
        for k in [k for k, t in _neg.items() if (now - t) >= _NEG_COOLDOWN_S]:
            _neg.pop(k, None)
        if len(_neg) > _NEG_MAX:
            for k in sorted(_neg, key=_neg.__getitem__)[: len(_neg) - _NEG_MAX]:
                _neg.pop(k, None)


# ── Public entry point ──────────────────────────────────────────────────────

def request_backfill(track) -> None:
    """Fire-and-forget: schedule a background cover backfill for ``track``.

    Call when the art cache MISSES for a remote track the index says HAS
    embedded art (``track.cover_art`` is the ``/api/art/...`` URL).  Safe to
    call repeatedly — coalesced (one task per track), concurrency-bounded, and
    backed off for ``_NEG_COOLDOWN_S`` after a failure.  No-op off the event
    loop or for local tracks.
    """
    try:
        tid = getattr(track, "id", None)
        path = getattr(track, "path", "") or ""
        if not tid or not _is_remote(path):
            return
        if not getattr(track, "cover_art", None):     # scan saw no embedded cover
            return
        if tid in _inflight:
            return
        last = _neg.get(tid)
        if last is not None and (time.monotonic() - last) < _NEG_COOLDOWN_S:
            return
        loop = asyncio.get_running_loop()             # RuntimeError off-loop
    except RuntimeError:
        return
    # Mark in-flight only after we know we can schedule, and back it out if the
    # task can't be created — so a track can never get permanently stuck.
    _inflight.add(tid)
    try:
        t = loop.create_task(_run(track))
    except Exception:
        _inflight.discard(tid)
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _run(track) -> None:
    tid = track.id
    try:
        ext = os.path.splitext(track.path)[1].lower()
        file_size = int(getattr(track, "file_size", 0) or 0)
        async with _get_sem():
            data, mime = await asyncio.to_thread(
                _fetch_remote_cover, track.path, file_size, ext,
            )
        if data:
            from soniqboom.core import art_cache
            from soniqboom.core.metadata import cap_full_cover
            # Cap the stored 'full' (off-loop); thumbs derive from the same
            # capped source — see cap_full_cover / _persist_and_notify.
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, cap_full_cover, data)
            await art_cache.store_art(tid, data, "full")
            # Generate thumbs now so the first grid view is instant.
            try:
                from soniqboom.api.art import _generate_and_cache_thumbs
                await _generate_and_cache_thumbs(tid, data)
            except Exception:
                pass
            # Clear any stale negative sentinel, then tell every client the art
            # is ready so the placeholder <img> swaps in without a reload.
            try:
                from soniqboom.api.art import _clear_art_absent_persisted
                _clear_art_absent_persisted(tid)
            except Exception:
                pass
            try:
                from soniqboom.api.library import _broadcast
                await _broadcast({"event": "art_ready", "track_id": tid})
            except Exception:
                # Art IS cached now, so a page reload will still show it; the
                # only loss is the live no-reload swap.  Log for visibility.
                log.debug("art-backfill: art_ready broadcast failed for %s",
                          tid, exc_info=True)
            log.debug("art-backfill: recovered embedded cover for %s (%d bytes)",
                      tid, len(data))
        else:
            _record_failure(tid)
    except (TimeoutError, ConnectionError, OSError) as exc:
        # Transient network / FTP-pool-contention failure — do NOT poison the
        # 5-minute cooldown, or a single slow moment locks a track's art out
        # for the whole browse session.  Let the next request retry.  (A file
        # that genuinely has no cover returns data=None above and DOES cool
        # down, so we don't hammer coverless files.)
        log.debug("art-backfill transient error for %s: %s", tid, exc)
    except Exception:
        _record_failure(tid)
        log.debug("art-backfill failed for %s", tid, exc_info=True)
    finally:
        _inflight.discard(tid)


# ── Format-aware fetch (runs in a worker thread) ────────────────────────────

def _fetch_remote_cover(path_str: str, file_size: int, ext: str):
    """Return (cover_bytes, mime) or (None, None).  Blocking — call via thread."""
    if "::" in path_str:
        # Composite remote-archive member (``…archive.zip::member.ext``): the
        # ``::`` tail must be partitioned BEFORE the remote scheme, or the
        # partial/atom range reads below ask the FTP/SMB server for the literal
        # ``archive.zip::member`` filename (no such file → cover lost) or read
        # the raw container without extracting the member.  A range read can't
        # reach inside a zip member anyway, so pull the whole member — the outer
        # archive is reused from the remote-cache, not re-fetched — via the
        # shared ``::``-before-remote resolver and read its cover from bytes.
        from soniqboom.core.source_bytes import read_source_bytes
        # ``lane="scan"`` — cover backfill is a background pass; keep it off the
        # playback stream pool.  (Note: a transient network failure returns
        # None here → the caller records a short negative-cooldown, delaying
        # this background pre-fetch; the interactive art path (_resolve_full_art)
        # still resolves the cover from the cached archive independently.)
        return _cover_from_bytes(read_source_bytes(path_str, lane="scan"), ext)

    from soniqboom.core.filesource import get_source, parse_remote_path
    try:
        scan_root, remote_path = parse_remote_path(path_str)
    except Exception:
        return None, None
    source = get_source(scan_root)
    if source is None:
        return None, None

    if ext in _MP4_EXTS:
        return _extract_mp4_cover(source, remote_path, file_size)

    # Front-cover formats: ID3v2 (MP3) / FLAC PICTURE / Ogg comment header.
    from soniqboom.core.metadata import HEADER_BUDGET
    budget = HEADER_BUDGET.get(ext) or _DEFAULT_FRONT
    front = source.read_partial(remote_path, budget, lane="scan")
    return _cover_from_bytes(front, ext)


def _extract_mp4_cover(source, remote_path: str, file_size: int):
    """Locate the moov atom (front or end), fetch just it, extract covr."""
    from soniqboom.core.tag_window import mp4_window
    compact = mp4_window(source, remote_path, file_size)
    if compact is None:
        return None, None
    return _cover_from_bytes(compact, ".m4a")


def _cover_from_bytes(buf: bytes, ext: str):
    """Write ``buf`` to a temp file with ``ext`` and pull the cover via mutagen."""
    if not buf:
        return None, None
    from soniqboom.api.art import _extract_cover
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext or ".bin", delete=False) as tmp:
            tmp.write(buf)
            tmp_path = Path(tmp.name)
        return _extract_cover(tmp_path)
    except Exception:
        return None, None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
