# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Filesystem-based cover art cache.

Directory structure:
  <cache_dir>/sm/<id[:2]>/<id[2:4]>/<id>.jpg   -- 200px thumbnail
  <cache_dir>/lg/<id[:2]>/<id[2:4]>/<id>.jpg   -- 550px thumbnail
  <cache_dir>/full/<id[:2]>/<id[2:4]>/<id>.dat  -- original art bytes
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from soniqboom.config import get_art_cache_dir

log = logging.getLogger(__name__)


def _art_path(track_id: str, size: str) -> Path:
    """Build the nested cache path for a given track and size.

    Size is "sm", "lg", or "full".
    Extension is .jpg for sm/lg, .dat for full.
    Nesting uses the first two and next two characters of the track_id
    (before the first dash).
    """
    ext = ".dat" if size == "full" else ".jpg"
    prefix_a = track_id[:2]
    prefix_b = track_id[2:4]
    return get_art_cache_dir() / size / prefix_a / prefix_b / f"{track_id}{ext}"


def _write_art_sync(track_id: str, data: bytes, size: str) -> None:
    """Synchronous art write — runs in a thread to avoid blocking the event loop."""
    if not data:
        return
    p = _art_path(track_id, size)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


async def store_art(track_id: str, data: bytes, size: str) -> None:
    """Write art bytes to the filesystem cache (non-blocking)."""
    if not data:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_art_sync, track_id, data, size)


def _read_art_sync(track_id: str, size: str) -> bytes | None:
    """Synchronous art read — runs in a thread to avoid blocking the event loop."""
    p = _art_path(track_id, size)
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


async def get_art(track_id: str, size: str) -> bytes | None:
    """Read art bytes from the filesystem cache (non-blocking)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_art_sync, track_id, size)


async def store_art_batch(mapping: dict[str, bytes], size: str) -> None:
    """Batch-write {track_id: raw_bytes} to disk for a single size."""
    if not mapping:
        return
    loop = asyncio.get_event_loop()
    await asyncio.gather(*(
        loop.run_in_executor(None, _write_art_sync, tid, data, size)
        for tid, data in mapping.items()
    ))


async def store_thumbs_batch(
    sm_mapping: dict[str, bytes],
    lg_mapping: dict[str, bytes],
) -> None:
    """Convenience wrapper: write sm and lg thumbnail batches concurrently."""
    if not sm_mapping and not lg_mapping:
        return
    loop = asyncio.get_event_loop()
    tasks = []
    for track_id, data in sm_mapping.items():
        tasks.append(loop.run_in_executor(None, _write_art_sync, track_id, data, "sm"))
    for track_id, data in lg_mapping.items():
        tasks.append(loop.run_in_executor(None, _write_art_sync, track_id, data, "lg"))
    if tasks:
        await asyncio.gather(*tasks)


async def store_full_art_batch(mapping: dict[str, str]) -> None:
    """Write full-size art from {track_id: data_uri_string}.

    Decodes the base64 data-URI and writes raw bytes to full/ concurrently.
    """
    if not mapping:
        return

    def _decode_and_write(track_id: str, data_uri: str) -> None:
        if not data_uri:
            return
        try:
            _header, b64_data = data_uri.split(",", 1)
            raw_bytes = base64.b64decode(b64_data)
            _write_art_sync(track_id, raw_bytes, "full")
        except Exception as exc:
            log.debug("Failed to decode/store full art for %s: %s", track_id, exc)

    loop = asyncio.get_event_loop()
    await asyncio.gather(*(
        loop.run_in_executor(None, _decode_and_write, tid, uri)
        for tid, uri in mapping.items()
    ))


def art_exists(track_id: str, size: str) -> bool:
    """Synchronous check whether cached art exists on disk."""
    return _art_path(track_id, size).exists()


# ── Deletion ────────────────────────────────────────────────────────────────
#
# The art cache is filesystem-backed with no time-based eviction, so we rely
# on track-deletion to reclaim space.  Without this, thumbnails for removed
# tracks orphan forever.

_ART_SIZES = ("sm", "lg", "full")


def _delete_art_sync(track_id: str) -> int:
    """Remove every cached art variant for one track.  Returns bytes freed."""
    freed = 0
    for size in _ART_SIZES:
        p = _art_path(track_id, size)
        try:
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
        except OSError:
            pass
    return freed


async def delete_art(track_id: str) -> int:
    """Remove cached art (sm/lg/full) for a single track.  Returns bytes freed."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_art_sync, track_id)


async def delete_art_batch(track_ids: list[str]) -> tuple[int, int]:
    """Remove cached art for many tracks in parallel.

    Returns ``(tracks_touched, bytes_freed)``.  A track counts as touched
    even if it had no cached art (call is still cheap — three stat() calls).
    """
    if not track_ids:
        return 0, 0
    loop = asyncio.get_event_loop()
    freed_list = await asyncio.gather(*(
        loop.run_in_executor(None, _delete_art_sync, tid) for tid in track_ids
    ))
    return len(track_ids), sum(freed_list)
