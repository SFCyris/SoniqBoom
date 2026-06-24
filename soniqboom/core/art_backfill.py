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
    dominate over FTP, so we minimise reads.

Orchestration: coalesced (one in-flight task per track), bounded concurrency,
and a short cooldown on failure so a genuinely-unreadable file isn't retried in
a storm.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

_MP4_EXTS = {".m4a", ".mp4", ".aac", ".m4b", ".m4p"}
_MP4_FRONT = 256 * 1024           # ftyp + a fast-start moov header walk
_MOOV_CAP = 32 * 1024 * 1024      # refuse a moov larger than this (covers long m4b)
_DEFAULT_FRONT = 1024 * 1024      # fallback front budget for non-MP4 formats
_MAX_HDR_READS = 16               # cap atom-header range reads (corrupt-file guard)

_MAX_CONCURRENCY = 4
_NEG_COOLDOWN_S = 300.0           # don't retry a failed backfill for 5 minutes
_NEG_MAX = 1024                   # prune the failure map past this many entries

# A permissive, valid ftyp, used only if the real one isn't in the front read
# (rare — ftyp is the first atom).  Broad compatible-brands so mutagen accepts
# it regardless of the source file's brand.
_GENERIC_FTYP = struct.pack(">I", 32) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isomiso2mp41M4A "

_inflight: set[str] = set()
_neg: dict[str, float] = {}       # track_id -> monotonic time of last failure
_tasks: set[asyncio.Task] = set()
_sem: asyncio.Semaphore | None = None


def _is_remote(path_str: str) -> bool:
    return path_str.startswith(("ftp://", "smb://"))


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
    front = source.read_at(remote_path, 0, _MP4_FRONT, lane="scan")
    if not front or len(front) < 8:
        return None, None
    if not file_size:
        try:
            file_size = max(len(front), source.stat(remote_path).size or len(front))
        except Exception:
            file_size = len(front)

    hdr_reads = [0]

    def read_hdr(off: int) -> bytes:
        if off + 16 <= len(front):
            return front[off:off + 16]
        hdr_reads[0] += 1
        if hdr_reads[0] > _MAX_HDR_READS:      # corrupt / pathological layout
            return b""
        return source.read_at(remote_path, off, 16, lane="scan")

    loc = _locate_moov(read_hdr, file_size)
    if loc is None:
        return None, None
    moov_off, moov_size = loc
    if moov_size <= 0:
        return None, None
    if moov_size > _MOOV_CAP:
        # Refuse a pathologically large moov rather than CLAMP it — a truncated
        # moov is a corrupt atom mutagen can't parse, which would just fail and
        # poison the negative cache.  (Legit covers live in a moov well under
        # the cap; only corrupt files or extreme sample tables exceed it.)
        log.debug("art-backfill: moov %d bytes > cap for %s — skipping",
                  moov_size, remote_path)
        return None, None

    if moov_off + moov_size <= len(front):
        moov_bytes = front[moov_off:moov_off + moov_size]
    else:
        moov_bytes = source.read_at(remote_path, moov_off, moov_size, lane="scan")
    if not moov_bytes or len(moov_bytes) < 8:
        return None, None

    # ftyp (the first atom) taken from the front read — but only if it really IS
    # an ftyp; otherwise fall back to a generic one rather than slicing garbage.
    ftyp_size = int.from_bytes(front[0:4], "big")
    if 8 <= ftyp_size <= len(front) and front[4:8] == b"ftyp":
        ftyp = front[0:ftyp_size]
    else:
        ftyp = _GENERIC_FTYP

    # Compact, valid MP4 for mutagen: ftyp + EMPTY mdat (8-byte header) + moov.
    # mutagen walks atoms sequentially and only reads ftyp + moov for tags, so
    # the audio (mdat) is irrelevant — no sparse file, no multi-MB temp.
    compact = ftyp + b"\x00\x00\x00\x08mdat" + moov_bytes
    return _cover_from_bytes(compact, ".m4a")


def _locate_moov(read_hdr, file_size: int):
    """Walk top-level MP4 atoms via ``read_hdr(offset) -> up to 16 bytes``.

    Returns (moov_offset, moov_size) or None.  Reads only atom HEADERS, never
    atom bodies — so finding a ``moov`` at the END of the file costs a handful
    of 16-byte range reads, not the whole ``mdat``.
    """
    off = 0
    guard = 0
    while off + 8 <= file_size and guard < 256:
        guard += 1
        hdr = read_hdr(off)
        if len(hdr) < 8:
            return None
        size = int.from_bytes(hdr[0:4], "big")
        typ = hdr[4:8]
        hlen = 8
        if size == 1:                           # 64-bit extended size
            if len(hdr) < 16:
                return None
            size = int.from_bytes(hdr[8:16], "big")
            hlen = 16
        elif size == 0:                         # atom extends to EOF
            size = file_size - off
        if typ == b"moov":
            return off, size
        if size < hlen:
            return None                         # malformed (also kills size==0 walks)
        off += size
    return None


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
