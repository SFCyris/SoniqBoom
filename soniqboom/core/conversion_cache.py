# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Conversion cache for rendered audio (SID, MIDI, tracker modules).

Caches the WAV output of sidplayfp / fluidsynth / openmpt123 so repeat
playback of the same track serves instantly from disk rather than
re-rendering every time.

Directory layout:
  <cache_dir>/sid/<key[:2]>/<key>.wav
  <cache_dir>/midi/<key[:2]>/<key>.wav
  <cache_dir>/tracker/<key[:2]>/<key>.wav

LRU metadata is kept in-memory.  WAV files live on disk.
Eviction runs after every cache write: while total > max, pop the
oldest entry and delete its file.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

from soniqboom.config import get_conversion_cache_dir, settings

log = logging.getLogger(__name__)

# ── Thundering-herd prevention ──────────────────────────────────────────────
_inflight: dict[str, asyncio.Event] = {}

# ── In-memory LRU state ────────────────────────────────────────────────────
_meta: dict[str, dict] = {}      # key → {path, size_bytes, format_type, created_at}
_lru: dict[str, float] = {}      # key → last_access_timestamp
_total_bytes: int = 0


# ── Key / path helpers ──────────────────────────────────────────────────────

def _cache_key(
    track_id: str,
    format_type: str,
    subsong: int = 0,
    soundfont_path: str | None = None,
) -> str:
    parts = [track_id]
    if format_type == "sid":
        parts.append(f"sub{subsong}")
        parts.append(f"dur{settings.sid_default_duration}")
    elif format_type == "midi":
        sf_hash = hashlib.sha256((soundfont_path or "").encode()).hexdigest()[:8]
        parts.append(f"sf{sf_hash}")
    elif format_type == "tracker":
        parts.append(f"sub{subsong}")
    return "__".join(parts)


def _cache_path(cache_key: str, format_type: str) -> Path:
    base = get_conversion_cache_dir() / format_type
    shard = cache_key[:2]
    return base / shard / f"{cache_key}.wav"


# ── Core cache operations ───────────────────────────────────────────────────

async def get_cached(cache_key: str) -> Path | None:
    """Look up a cache entry; return its disk path or None.

    On a hit, updates the LRU timestamp.
    Self-heals if the metadata exists but the file is missing on disk.
    """
    global _total_bytes

    entry = _meta.get(cache_key)
    if not entry:
        _lru.pop(cache_key, None)
        return None

    path = Path(entry["path"])
    if not path.exists():
        _purge_entry(cache_key)
        return None

    _lru[cache_key] = time.time()
    return path


async def store_cached(
    cache_key: str,
    format_type: str,
    source_path: Path,
) -> Path:
    """Move a rendered temp WAV into the cache and register it.

    Uses os.replace() for atomic placement when possible, falling back to
    shutil.move() across filesystems.  Triggers LRU eviction if over quota.
    """
    global _total_bytes

    dest = _cache_path(cache_key, format_type)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.replace(str(source_path), str(dest))
    except OSError:
        shutil.move(str(source_path), str(dest))

    size_bytes = dest.stat().st_size
    now = time.time()

    _meta[cache_key] = {
        "path": str(dest),
        "size_bytes": size_bytes,
        "format_type": format_type,
        "created_at": now,
    }
    _lru[cache_key] = now
    _total_bytes += size_bytes

    _maybe_evict()
    return dest


def _purge_entry(cache_key: str) -> None:
    """Remove a single cache entry from metadata."""
    global _total_bytes
    entry = _meta.pop(cache_key, None)
    _lru.pop(cache_key, None)
    if entry:
        size = entry.get("size_bytes", 0)
        if size > 0:
            _total_bytes = max(0, _total_bytes - size)
        try:
            Path(entry["path"]).unlink(missing_ok=True)
        except Exception:
            pass


def _maybe_evict() -> None:
    """Evict oldest entries until total size is within the configured limit."""
    global _total_bytes
    max_bytes = settings.conversion_cache_max_bytes

    while _total_bytes > max_bytes and _lru:
        evict_key = min(_lru, key=_lru.get)
        entry = _meta.pop(evict_key, None)
        _lru.pop(evict_key, None)

        if entry:
            size = entry.get("size_bytes", 0)
            _total_bytes = max(0, _total_bytes - size)
            try:
                Path(entry["path"]).unlink(missing_ok=True)
            except Exception:
                pass
            log.debug("ConvCache evicted %s (%d bytes)", evict_key, size)

    if not _lru:
        _total_bytes = 0


# ── Main entry point ────────────────────────────────────────────────────────

async def get_or_render(
    track_id: str,
    format_type: str,
    subsong: int,
    render_fn,
    soundfont_path: str | None = None,
) -> tuple[Path, bool]:
    """Look up cache; on miss, render and store.  Returns (path, cache_hit).

    Handles thundering-herd: if another coroutine is already rendering the
    same key, waits for it to finish then serves from cache.
    """
    key = _cache_key(track_id, format_type, subsong, soundfont_path)

    cached = await get_cached(key)
    if cached:
        return cached, True

    if key in _inflight:
        event = _inflight[key]
        await event.wait()
        cached = await get_cached(key)
        if cached:
            return cached, True

    event = asyncio.Event()
    _inflight[key] = event
    try:
        tmp_path = await render_fn()
        dest = await store_cached(key, format_type, tmp_path)
        return dest, False
    finally:
        event.set()
        _inflight.pop(key, None)


# ── SID progressive playback helpers ─────────────────────────────────────────

_bg_renders: dict[str, asyncio.Task] = {}


async def find_shorter_sid_entry(
    track_id: str, subsong: int, target_dur: int,
) -> tuple[Path, int] | None:
    """Find any cached SID entry for this track with a shorter duration."""
    prefix = f"{track_id}__sub{subsong}__dur"
    best: tuple[Path, int] | None = None

    for key, entry in _meta.items():
        if not key.startswith(prefix):
            continue
        try:
            dur = int(key.split("__dur")[-1])
        except (ValueError, IndexError):
            continue
        if dur >= target_dur:
            continue
        path = Path(entry["path"])
        if path.exists():
            if best is None or dur > best[1]:
                best = (path, dur)

    if best:
        best_key = f"{track_id}__sub{subsong}__dur{best[1]}"
        _lru[best_key] = time.time()
    return best


async def start_background_render(
    cache_key: str, format_type: str, render_fn,
) -> None:
    """Fire-and-forget: render in background and store when done."""
    if cache_key in _bg_renders or cache_key in _inflight:
        return

    async def _do():
        event = asyncio.Event()
        _inflight[cache_key] = event
        try:
            tmp_path = await render_fn()
            await store_cached(cache_key, format_type, tmp_path)
            log.info("Background render complete: %s", cache_key)
        except Exception as exc:
            log.error("Background render failed for %s: %s", cache_key, exc)
        finally:
            event.set()
            _inflight.pop(cache_key, None)
            _bg_renders.pop(cache_key, None)

    _bg_renders[cache_key] = asyncio.create_task(_do())


async def is_cache_ready(cache_key: str) -> bool:
    if cache_key in _bg_renders or cache_key in _inflight:
        return False
    cached = await get_cached(cache_key)
    return cached is not None


# ── Admin / stats ───────────────────────────────────────────────────────────

async def cache_stats() -> dict:
    by_type: dict[str, int] = {"sid": 0, "midi": 0, "tracker": 0}
    for entry in _meta.values():
        ft = entry.get("format_type", "")
        if ft in by_type:
            by_type[ft] += 1
    return {
        "total_bytes": _total_bytes,
        "entry_count": len(_meta),
        "max_bytes": settings.conversion_cache_max_bytes,
        "by_type": by_type,
    }


async def clear_cache() -> dict:
    """Wipe all cached conversion files and in-memory metadata."""
    global _total_bytes

    cache_dir = get_conversion_cache_dir()
    deleted = 0
    for sub in ("sid", "midi", "tracker"):
        d = cache_dir / sub
        if d.exists():
            for f in d.rglob("*.wav"):
                f.unlink(missing_ok=True)
                deleted += 1

    _meta.clear()
    _lru.clear()
    _total_bytes = 0
    log.info("Conversion cache cleared: %d files deleted", deleted)
    return {"deleted_files": deleted}
