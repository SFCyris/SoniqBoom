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
a storm.  Waiting recoveries are served NEWEST FIRST (``_pending``, as the
folder covers in ``core/folder_covers``): a fast scroll through a list asks
hundreds of covers, and the rows on screen now are the ones asked last — with
the old first-come order they waited behind every row scrolled past.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)

_MP4_EXTS = {".m4a", ".mp4", ".aac", ".m4b", ".m4p"}
_DEFAULT_FRONT = 1024 * 1024      # fallback front budget for non-MP4 formats

_MAX_CONCURRENCY = 4              # a share whose link is fast or not measured
# The share's lane for the range reads: a cover is asked for by what is on
# screen (a list row, the player, Track Info), so they go before the
# background passes' reads ("scan": length fill-in, enrichment) and after
# playback ("stream").  (An archive member's whole-file fetch stays "scan".)
_LANE = "browse"
_NEG_COOLDOWN_S = 300.0           # don't retry a failed backfill for 5 minutes
_NEG_MAX = 1024                   # prune the failure map past this many entries

_inflight: set[str] = set()       # queued or running
_neg: dict[str, float] = {}       # track_id -> monotonic time of last failure
_tasks: set[asyncio.Task] = set()
# Waiting recoveries, newest last (served from the end); each running one's
# share.  How many covers a share recovers at once follows its measured link
# (``core/link_speed.limits().backfill``): each recovery is a few small ranged
# reads, so on a slow link it is mostly waiting — 8 at once there, the old 4
# on a fast or unmeasured link.  All shares together stay within
# ``_TOTAL_MAX`` (several shares no longer multiply the old global 4 freely).
_pending: "OrderedDict[str, object]" = OrderedDict()
# The same requests by share, each newest last, and when each share was last
# asked — ``_pump`` (on the event loop, at every request and every end)
# looks at a few shares, never at every waiting request: with 4000 waiting
# it walked all of them each time (22 s of loop time for 4000 requests).
_by_root: "dict[str, OrderedDict[str, object]]" = {}
_asked_at: "dict[str, float]" = {}
_running: "dict[str, str]" = {}   # track_id -> share root
_running_by_root: "dict[str, int]" = {}
_TOTAL_MAX = 12
_MAX_PENDING = 4000               # the oldest waiting request drops out past this


def _is_remote(path_str: str) -> bool:
    from soniqboom.core.filesource import is_remote_path
    return is_remote_path(path_str)


def _root_of(path_str: str) -> str:
    try:
        from soniqboom.core.filesource import parse_remote_path
        return parse_remote_path(path_str)[0] or ""
    except Exception:                               # noqa: BLE001
        return ""


def _width(scan_root: str) -> int:
    """How many covers share ``scan_root`` recovers at once (its link)."""
    if not scan_root:
        return _MAX_CONCURRENCY
    from soniqboom.core import link_speed
    from soniqboom.core.filesource import get_source
    return link_speed.limits(get_source(scan_root)).backfill


def _pump() -> None:
    """Start the newest waiting recoveries the shares have room for — the
    share asked most recently first, within it the newest request first."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if not _pending or len(_running) >= _TOTAL_MAX:
        return
    for root in sorted(_by_root, key=lambda r: _asked_at.get(r, 0.0), reverse=True):
        q = _by_root.get(root)
        if not q:
            _by_root.pop(root, None)
            continue
        width = _width(root)
        while q and _running_by_root.get(root, 0) < width and len(_running) < _TOTAL_MAX:
            tid, track = q.popitem(last=True)
            _pending.pop(tid, None)
            _running[tid] = root
            _running_by_root[root] = _running_by_root.get(root, 0) + 1
            try:
                t = loop.create_task(_run(track))
            except Exception:                       # noqa: BLE001 — never leave it stuck
                _done_running(tid)
                _inflight.discard(tid)
                continue
            _tasks.add(t)
            t.add_done_callback(_tasks.discard)
        if not q:
            _by_root.pop(root, None)
        if len(_running) >= _TOTAL_MAX:
            break


def _done_running(tid: str) -> None:
    root = _running.pop(tid, None)
    if root is None:
        return
    n = _running_by_root.get(root, 1) - 1
    if n > 0:
        _running_by_root[root] = n
    else:
        _running_by_root.pop(root, None)


def pending(tid: str) -> bool:
    """Is the embedded cover of ``tid`` waiting or being recovered?"""
    return tid in _inflight


def failed_recently(tid: str) -> bool:
    """Did recovering ``tid``'s embedded cover find none (or fail) within
    the cooldown?  (``api/art`` then lets the folder image stand in.)"""
    last = _neg.get(tid)
    return last is not None and (time.monotonic() - last) < _NEG_COOLDOWN_S


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
        from soniqboom.core import catalogue
        if catalogue.is_catalogue_path(path):
            return                                    # fetched only when played
        if not getattr(track, "cover_art", None):     # scan saw no embedded cover
            return
        if tid in _inflight:
            if tid in _pending:
                _pending.move_to_end(tid)             # asked again: on screen now
                q = _by_root.get(_root_of(path))
                if q is not None and tid in q:
                    q.move_to_end(tid)
                _asked_at[_root_of(path)] = time.monotonic()
            return
        last = _neg.get(tid)
        if last is not None and (time.monotonic() - last) < _NEG_COOLDOWN_S:
            return
        asyncio.get_running_loop()                    # RuntimeError off-loop
    except RuntimeError:
        return
    root = _root_of(path)
    _inflight.add(tid)
    _pending[tid] = track
    _by_root.setdefault(root, OrderedDict())[tid] = track
    _asked_at[root] = time.monotonic()
    while len(_pending) > _MAX_PENDING:
        old, old_track = _pending.popitem(last=False)  # the oldest waiting request
        _inflight.discard(old)
        q = _by_root.get(_root_of(getattr(old_track, "path", "") or ""))
        if q is not None:
            q.pop(old, None)
    _pump()


async def _run(track) -> None:
    tid = track.id
    try:
        ext = os.path.splitext(track.path)[1].lower()
        file_size = int(getattr(track, "file_size", 0) or 0)
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
            # (the file's own picture: not a copy of its folder's image)
            await asyncio.to_thread(art_cache.unmark_from_folder, tid)
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
            # (no picture found in the file: its row asks again and gets the
            # folder's image, which ``api/art`` holds back while an embedded
            # cover is being recovered)
            try:
                from soniqboom.api.library import _broadcast
                await _broadcast({"event": "art_ready", "track_id": tid})
            except Exception:                       # noqa: BLE001
                pass
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
        _done_running(tid)
        _pump()


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
    front = source.read_partial(remote_path, budget, lane=_LANE)
    return _cover_from_bytes(front, ext)


def _extract_mp4_cover(source, remote_path: str, file_size: int):
    """Locate the moov atom (front or end), fetch just it, extract covr."""
    from soniqboom.core.tag_window import mp4_window
    compact = mp4_window(source, remote_path, file_size, lane=_LANE)
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
