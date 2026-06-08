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
import threading
import time
import weakref
from collections import OrderedDict
from pathlib import Path

from fastapi import HTTPException

from soniqboom.config import get_conversion_cache_dir, settings
# Aliased: this module already defines a public ``async def cache_stats()``
# (imported by main.py / admin.py).  Without the alias that function shadows
# this telemetry module at module scope, so ``cache_stats.miss`` resolves to
# the function → AttributeError on every cold render (MIDI/SID/MOD/GME).
from soniqboom.core import cache_stats as _cstats

log = logging.getLogger(__name__)

# ── Thundering-herd prevention ──────────────────────────────────────────────
_inflight: dict[str, asyncio.Event] = {}
# Per-key asyncio.Lock so the get_cached → _inflight check → render → store
# sequence is atomic for any one cache key.  Without this lock two coroutines
# (e.g. a live stream + a prewarm) arriving cold for the same key both saw
# ``None`` from get_cached, both saw the key missing from _inflight, both
# registered their own event, both ran ffmpeg, and both ``os.replace`` raced
# onto the same destination — double-counting ``_total_bytes`` and corrupting
# a FileResponse that was mid-reading the older write.
#
# ``WeakValueDictionary`` so an entry disappears automatically once no caller
# still holds the lock — without this the dict grew once per distinct cache
# key for the lifetime of the process (Perf #7).
_keyed_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)
_keyed_locks_lock = threading.Lock()


def _lock_for(key: str) -> asyncio.Lock:
    """Get (or lazily create) the asyncio.Lock for ``key``.  Mutex-protected
    creation so two coroutines colliding here don't end up with two locks.

    Callers must keep a local reference to the returned lock for the duration
    of their critical section — the underlying dict is weak-valued, so
    dropping the reference is what allows GC to reclaim the entry.
    """
    with _keyed_locks_lock:
        lock = _keyed_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _keyed_locks[key] = lock
    return lock

# ── Eviction-pin set — keys protected from LRU eviction ─────────────────────
# Populated/cleared by the stream endpoint at playback start/end so a
# speculative N+2 prewarm can't evict the currently-playing track and force
# a re-render under the user's nose.  Pins are advisory: the eviction loop
# skips pinned keys when picking victims, but if every entry is pinned and
# we're over budget we still evict the oldest (correctness wins over polish).
#
# Pins are *reference-counted* — every ``pin(key)`` call increments a counter
# and every matching ``unpin(key)`` decrements it.  The key only loses its
# pinned status once the count reaches zero.  This replaces the previous
# FIFO-ageing model which could silently age out a still-playing track when
# enough new prewarms arrived (Perf #9): an active streaming response now
# always wins over LRU/eviction regardless of how many concurrent readers
# arrive afterwards.  Pending purges (``_purge_entry``) are deferred while
# the count is non-zero so an in-flight ``FileResponse`` can't race against
# an ``unlink`` of the file it's still reading from disk.
_pin_refs: dict[str, int] = {}
# Cache keys whose unlink is deferred until the refcount hits zero.  Maps
# ``key → on-disk path`` so ``unpin`` can perform the unlink without
# needing the now-removed ``_meta`` entry.
_pending_purge: dict[str, str] = {}

# ── In-memory LRU state ────────────────────────────────────────────────────
_meta: dict[str, dict] = {}      # key → {path, size_bytes, format_type, created_at}

# Expose live entry count to the cache-stats telemetry (cascade viz).
_cstats.register_size("conversion", lambda: len(_meta))
# OrderedDict gives us O(1) eviction (popitem from the front) and O(1)
# touch-on-access (move_to_end) instead of the previous ``min(_lru, key=...)``
# which scanned every entry per eviction.
_lru: "OrderedDict[str, float]" = OrderedDict()
_total_bytes: int = 0
# Mutex covering _meta / _lru / _total_bytes.  RLock so nested acquisition
# is safe — e.g. ``purge_sid_entries_for`` snapshots victim keys under the
# lock, then calls ``_purge_entry`` (which also acquires the lock) in a
# loop.  Worker-thread paths (``_maybe_evict``, ``_purge_entry`` via
# ``asyncio.to_thread``) and loop-thread paths (``find_shorter_sid_entry``)
# both honour it.
_state_lock = threading.RLock()


# ── Key / path helpers ──────────────────────────────────────────────────────

def _cache_key(
    track_id: str,
    format_type: str,
    subsong: int = 0,
    soundfont_path: str | None = None,
    duration: int | None = None,
    codec: str | None = None,
    target_rate: int | None = None,
) -> str:
    """Build the cache key.  ``duration`` overrides the SID default so a
    HVSC-supplied per-tune length yields a distinct key — without this
    override, a SID rendered once at the 5 min default would forever
    serve from cache even after HVSC delivered the real 3:27.

    For the ``transcoded`` format type, ``codec`` is the output codec
    (typically ``settings.transcode_format`` — flac/mp3/ogg) and
    ``target_rate`` is an optional resample target.  Distinct codec or
    rate yields distinct entries so a user changing transcode_format from
    flac to mp3 doesn't accidentally serve a stale flac WAV."""
    parts = [track_id]
    if format_type == "sid":
        parts.append(f"sub{subsong}")
        dur = int(duration if duration is not None else settings.sid_default_duration)
        parts.append(f"dur{dur}")
    elif format_type == "midi":
        sf_hash = hashlib.sha256((soundfont_path or "").encode()).hexdigest()[:8]
        parts.append(f"sf{sf_hash}")
    elif format_type in ("tracker", "uade", "hvl"):
        # All carry sub-songs (HVL/AHX especially) — without ``sub{N}`` in the
        # key, different subsongs of the same module collide and the first
        # render wins.
        parts.append(f"sub{subsong}")
    elif format_type == "gme":
        # libgme containers (NSF/SPC/GBS/VGM/AY/KSS/SAP/GYM/HES) carry
        # multiple sub-songs — without ``sub{N}`` in the key, Mega Man 1
        # subsong 0 and subsong 5 collide and the first render wins.
        parts.append(f"sub{subsong}")
    elif format_type == "transcoded":
        parts.append(f"c{codec or 'flac'}")
        # ar=0 means "preserve source sample rate" — distinct from a literal
        # 96 kHz target (used for DSD) so we never reuse a DSD-downsampled
        # 96 kHz FLAC when serving a native-rate ALAC source.
        parts.append(f"ar{int(target_rate or 0)}")
    return "__".join(parts)


def _cache_path(cache_key: str, format_type: str) -> Path:
    base = get_conversion_cache_dir() / format_type
    shard = cache_key[:2]
    return base / shard / f"{cache_key}.wav"


def get_vu_sidecar_path(track_id: str) -> Path | None:
    """Return the on-disk path to the VU sidecar for *track_id*, or
    None if no tracker render has produced one yet.

    Walks the in-memory ``_meta`` looking for any cached tracker entry
    whose key starts with *track_id*.  The cache key format is
    ``"<track_id>__sub<N>"`` so a startswith match is unambiguous —
    different subsongs would be different VU sidecars, but the
    frontend currently only requests the default subsong's VU.

    Returns the FIRST matching sidecar (lowest subsong) so the
    frontend's default-subsong playback gets the right meters.  If we
    later add per-subsong selection in the UI, the endpoint can grow a
    ``?subsong=`` query param.
    """
    with _state_lock:
        for cache_key, entry in _meta.items():
            if not cache_key.startswith(f"{track_id}__"):
                continue
            if entry.get("format_type") != "tracker":
                continue
            wav_path = Path(entry["path"])
            sidecar  = wav_path.with_suffix(".vu")
            if sidecar.exists():
                return sidecar
    return None


# ── Core cache operations ───────────────────────────────────────────────────

async def get_cached(cache_key: str) -> Path | None:
    """Look up a cache entry; return its disk path or None.

    On a hit, updates the LRU timestamp.
    Self-heals if the metadata exists but the file is missing on disk.
    """
    # Single lock acquisition for the read + LRU touch.  The conversion
    # cache lives on local disk so ``path.exists`` is a microsecond
    # syscall — running it under the lock is cheaper than the
    # acquire/release pair Perf #1 flagged.
    with _state_lock:
        entry = _meta.get(cache_key)
        if not entry:
            _lru.pop(cache_key, None)
            return None
        path = Path(entry["path"])
        exists_now = path.exists()
        if exists_now:
            _lru[cache_key] = time.time()
            _lru.move_to_end(cache_key)

    if not exists_now:
        # Off-loop purge so a slow filesystem doesn't block the request.
        await asyncio.to_thread(_purge_entry, cache_key)
        return None
    return path


async def store_cached(
    cache_key: str,
    format_type: str,
    source_path: Path,
) -> Path:
    """Move a rendered temp WAV into the cache and register it.

    Uses os.replace() for atomic placement when possible, falling back to
    shutil.move() across filesystems.  Triggers LRU eviction if over quota.

    Sidecar handling
    ----------------
    Tracker renders may produce a ``<source>.vu`` per-channel VU sidecar
    next to the WAV (see ``soniqboom/core/openmpt_vu.py``).  If present,
    we move it alongside so the cached WAV always has its matching VU
    sidecar — the frontend looks for ``<wav>.vu`` to drive per-channel
    meters.  Sidecar bytes don't count towards the cache LRU budget
    because they're trivially small (~10–100 KB) compared to the WAV.
    """
    global _total_bytes

    dest = _cache_path(cache_key, format_type)
    src_sidecar  = source_path.with_suffix(".vu")
    dest_sidecar = dest.with_suffix(".vu")

    def _place_and_size() -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(str(source_path), str(dest))
        except OSError:
            shutil.move(str(source_path), str(dest))
        # Move the sidecar too if it exists — non-fatal if it doesn't.
        if src_sidecar.exists():
            try:
                os.replace(str(src_sidecar), str(dest_sidecar))
            except OSError:
                try:
                    shutil.move(str(src_sidecar), str(dest_sidecar))
                except Exception:
                    pass
        return dest.stat().st_size

    size_bytes = await asyncio.to_thread(_place_and_size)
    now = time.time()

    with _state_lock:
        _meta[cache_key] = {
            "path": str(dest),
            "size_bytes": size_bytes,
            "format_type": format_type,
            "created_at": now,
        }
        _lru[cache_key] = now
        _lru.move_to_end(cache_key)
        _total_bytes += size_bytes

    # Eviction may unlink several files; do it off the event loop.
    await asyncio.to_thread(_maybe_evict)
    return dest


async def register_existing(
    cache_key: str,
    format_type: str,
    dest_path: Path,
) -> Path:
    """Register a file already placed at ``dest_path`` as a cache entry.

    The stream-as-render path (cast_pipe) writes to a ``.partial`` file
    and atomically renames it to the final cache path itself — at that
    point the bytes are on disk but the in-memory ``_meta``/``_lru``
    index doesn't know.  Without registration, the next ``get_cached``
    for the same key returns ``None`` until the warmup-from-disk pass at
    the next server restart — every "second play" therefore re-runs the
    full ffmpeg transcode instead of range-serving the warm WAV.

    This helper repairs that gap.  Idempotent: re-registering an
    existing entry just refreshes the LRU timestamp.
    """
    global _total_bytes

    def _stat() -> int:
        try:
            return dest_path.stat().st_size
        except OSError:
            return 0
    size_bytes = await asyncio.to_thread(_stat)
    if size_bytes <= 0:
        # Disk file vanished before we could register it.  Caller will
        # treat this as a cache miss next read.
        return dest_path

    now = time.time()
    with _state_lock:
        old = _meta.get(cache_key)
        if old:
            # Idempotent re-register — only touch LRU.
            _lru[cache_key] = now
            _lru.move_to_end(cache_key)
            return dest_path
        _meta[cache_key] = {
            "path":        str(dest_path),
            "size_bytes":  size_bytes,
            "format_type": format_type,
            "created_at":  now,
        }
        _lru[cache_key] = now
        _lru.move_to_end(cache_key)
        _total_bytes += size_bytes
    # Run eviction off the event loop — bounded by the entry count above
    # the LRU budget, so a one-off register won't stall callers.
    await asyncio.to_thread(_maybe_evict)
    return dest_path


def _purge_entry(cache_key: str) -> None:
    """Remove a single cache entry from metadata.

    If the entry is currently pinned (``_pin_refs[key] > 0``) the file unlink
    is deferred — the entry stays out of ``_meta`` / ``_lru`` so future
    callers won't find it, but the disk file lives until the last in-flight
    reader releases its pin.  ``unpin`` then completes the purge.
    """
    global _total_bytes
    deferred = False
    with _state_lock:
        entry = _meta.pop(cache_key, None)
        _lru.pop(cache_key, None)
        if entry:
            size = entry.get("size_bytes", 0)
            if size > 0:
                _total_bytes = max(0, _total_bytes - size)
        if entry and _pin_refs.get(cache_key, 0) > 0:
            # Defer the unlink — an in-flight FileResponse is still reading
            # this file.  ``unpin`` will do the unlink once the refcount
            # drops to zero.
            _pending_purge[cache_key] = entry["path"]
            deferred = True
    if entry and not deferred:
        try:
            Path(entry["path"]).unlink(missing_ok=True)
        except Exception:
            pass


def _maybe_evict() -> None:
    """Evict oldest entries until total size is within the configured limit.

    Eviction is O(1) per item now that ``_lru`` is an ``OrderedDict`` —
    ``popitem(last=False)`` removes the least-recently-used key without
    scanning every entry, which was an O(N) cost per eviction previously.

    Honours ``_pinned`` — a pinned key (typically the currently-playing
    track) is skipped while looking for an eviction victim, so a
    speculative N+2 prewarm can't evict the user's current playback and
    force a re-render.  If every entry is pinned and we're still over
    budget we fall back to oldest-first (correctness over politeness).

    Serialises under ``_state_lock`` because two concurrent stream requests
    can both schedule this via ``asyncio.to_thread`` — without the lock,
    two threads doing ``_lru.popitem(last=False)`` would race on the same
    OrderedDict and could ``KeyError`` or double-evict.
    """
    global _total_bytes
    max_bytes = settings.conversion_cache_max_bytes
    victims: list[tuple[str, dict]] = []
    with _state_lock:
        while _total_bytes > max_bytes and _lru:
            evict_key: str | None = None
            # First pass: pick the oldest unpinned key.  Refcount==0 means
            # no in-flight reader is currently using this entry.
            for k in _lru:
                if _pin_refs.get(k, 0) == 0:
                    evict_key = k
                    break
            # Fallback: every entry is pinned — evict the oldest anyway so
            # we don't blow past the disk budget indefinitely.  The unlink
            # below will be deferred for any key still actually pinned.
            if evict_key is None:
                evict_key = next(iter(_lru))
                log.warning("ConvCache over budget but all entries pinned — evicting %s anyway",
                            evict_key)
            _lru.pop(evict_key, None)
            entry = _meta.pop(evict_key, None)
            if entry:
                size = entry.get("size_bytes", 0)
                _total_bytes = max(0, _total_bytes - size)
                victims.append((evict_key, entry))
                log.debug("ConvCache evicted %s (%d bytes)", evict_key, size)
        if not _lru:
            _total_bytes = 0
        # Mark any still-pinned victims for deferred unlink — only the
        # unpinned ones get unlinked outside the lock below.
        immediate_unlinks: list[dict] = []
        for k, entry in victims:
            if _pin_refs.get(k, 0) > 0:
                _pending_purge[k] = entry["path"]
            else:
                immediate_unlinks.append(entry)
    # Unlink outside the lock so a slow disk doesn't block other callers.
    for entry in immediate_unlinks:
        try:
            Path(entry["path"]).unlink(missing_ok=True)
        except Exception:
            pass


def pin(cache_key: str) -> int:
    """Acquire a pin on ``cache_key`` — increments the refcount.

    Every call must be paired with exactly one ``unpin(cache_key)`` once the
    caller no longer needs the entry held immobile (typically the streaming
    response close handler).  While the refcount is non-zero the LRU eviction
    loop will skip this key, and ``_purge_entry`` defers the actual file
    unlink so an in-flight ``FileResponse`` doesn't lose its open file out
    from under it.

    Returns the new refcount (mostly useful for assertions in tests).
    """
    with _state_lock:
        n = _pin_refs.get(cache_key, 0) + 1
        _pin_refs[cache_key] = n
        return n


def unpin(cache_key: str) -> int:
    """Release one pin on ``cache_key``.

    Matched with ``pin`` 1:1.  When the refcount falls to zero any deferred
    file unlink (queued by ``_purge_entry`` / ``_maybe_evict`` while the
    entry was pinned) is performed.

    Returns the new refcount.
    """
    pending_path: str | None = None
    with _state_lock:
        n = _pin_refs.get(cache_key, 0) - 1
        if n <= 0:
            _pin_refs.pop(cache_key, None)
            pending_path = _pending_purge.pop(cache_key, None)
            n = 0
        else:
            _pin_refs[cache_key] = n
    if pending_path:
        try:
            Path(pending_path).unlink(missing_ok=True)
        except Exception:
            pass
    return n


# ── Main entry point ────────────────────────────────────────────────────────

async def get_or_render(
    track_id: str,
    format_type: str,
    subsong: int,
    render_fn,
    soundfont_path: str | None = None,
    duration: int | None = None,
    codec: str | None = None,
    target_rate: int | None = None,
) -> tuple[Path, bool]:
    """Look up cache; on miss, render and store.  Returns (path, cache_hit).

    Handles thundering-herd: if another coroutine is already rendering the
    same key, waits for it to finish then serves from cache.

    ``duration`` is forwarded into the cache key for SID (so HVSC-supplied
    per-tune lengths produce distinct cache entries instead of colliding
    with the global-default render).  ``codec`` and ``target_rate`` are
    used by the ``transcoded`` format type so a DSD-as-96 kHz-FLAC entry
    doesn't collide with an ALAC-as-source-rate-FLAC entry.
    """
    key = _cache_key(
        track_id, format_type, subsong, soundfont_path,
        duration=duration, codec=codec, target_rate=target_rate,
    )

    # Fast path: cache hit without acquiring the per-key lock.
    cached = await get_cached(key)
    if cached:
        _cstats.hit("conversion")
        return cached, True

    # Slow path: serialise cold callers for this key.  Inside the lock the
    # cache-check + inflight-register sequence is atomic, so two coroutines
    # arriving cold can't both decide "no one else is rendering" and both
    # kick off ffmpeg.
    lock = _lock_for(key)
    is_renderer = False
    async with lock:
        # Re-check now we hold the lock — the cache may have filled while
        # we awaited acquisition.
        cached = await get_cached(key)
        if cached:
            _cstats.hit("conversion")
            return cached, True
        event = _inflight.get(key)
        if event is None:
            event = asyncio.Event()
            _inflight[key] = event
            is_renderer = True
            # Cold path — this caller will actually render.  Count one miss
            # per logical render (the waiters below served from cache are
            # separate requests counted as hits when they wake).
            _cstats.miss("conversion")

    if not is_renderer:
        # Another coroutine is already rendering — wait for its event and
        # serve from cache when it completes.
        await event.wait()
        cached = await get_cached(key)
        if cached:
            _cstats.hit("conversion")
            return cached, True
        # The renderer failed (event set without storing).  We don't retry
        # — surface the error to the caller so the original render's
        # HTTPException propagates as expected.
        raise HTTPException(
            502, "Cache fill failed in a concurrent render — try again",
        )

    try:
        tmp_path = await render_fn()
        dest = await store_cached(key, format_type, tmp_path)
        return dest, False
    finally:
        event.set()
        _inflight.pop(key, None)


# ── SID progressive playback helpers ─────────────────────────────────────────

# Background-render tasks keyed by cache key.  Weak-valued so finished tasks
# vanish automatically once asyncio releases its final reference — the
# previous strong-valued dict relied on the ``finally`` block popping the
# key, but a task cancelled / collected before reaching the ``finally``
# block (e.g. during shutdown) would leave a permanent dict entry behind.
# Callers in ``start_background_render`` keep their own strong reference to
# the new task in module state (``_bg_render_strong``) until its done-callback
# clears the entry — without that, Python 3.10+ may GC a pending task whose
# only reference was the weak-valued dict.
_bg_renders: "weakref.WeakValueDictionary[str, asyncio.Task]" = (
    weakref.WeakValueDictionary()
)
# Strong-ref set for live tasks — populated alongside ``_bg_renders`` and
# pruned via ``add_done_callback``.  This way the WeakValueDictionary frees
# entries immediately on task completion (no orphan keys) while the asyncio
# task itself stays alive for the duration of its execution.
_bg_render_strong: set[asyncio.Task] = set()


async def find_shorter_sid_entry(
    track_id: str, subsong: int, target_dur: int,
) -> tuple[Path, int] | None:
    """Find any cached SID entry for this track with a shorter duration."""
    prefix = f"{track_id}__sub{subsong}__dur"
    # Snapshot under the state lock — otherwise an in-flight
    # ``_maybe_evict`` on a worker thread can mutate ``_meta`` mid-iteration
    # and raise ``RuntimeError: dictionary changed size during iteration``.
    with _state_lock:
        candidates = [
            (key, entry) for key, entry in _meta.items()
            if key.startswith(prefix)
        ]
    best: tuple[Path, int] | None = None
    for key, entry in candidates:
        try:
            dur = int(key.split("__dur")[-1])
        except (ValueError, IndexError):
            continue
        if dur >= target_dur:
            continue
        # path.exists() is a sync syscall; the cache lives locally so this
        # is microseconds, but if it ever moves to a slow share this is the
        # spot to switch to asyncio.to_thread.
        path = Path(entry["path"])
        if path.exists():
            if best is None or dur > best[1]:
                best = (path, dur)

    if best:
        best_key = f"{track_id}__sub{subsong}__dur{best[1]}"
        with _state_lock:
            if best_key in _lru:
                _lru[best_key] = time.time()
                _lru.move_to_end(best_key)
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

    task = asyncio.create_task(_do())
    # Strong-ref the live task so the WeakValueDictionary entry survives
    # until the task is actually done; the done-callback prunes both.
    _bg_render_strong.add(task)
    _bg_renders[cache_key] = task
    task.add_done_callback(_bg_render_strong.discard)


async def is_cache_ready(cache_key: str) -> bool:
    if cache_key in _bg_renders or cache_key in _inflight:
        return False
    cached = await get_cached(cache_key)
    return cached is not None


# ── Admin / stats ───────────────────────────────────────────────────────────

async def cache_stats() -> dict:
    with _state_lock:
        by_type: dict[str, int] = {"sid": 0, "midi": 0, "tracker": 0, "uade": 0, "hvl": 0, "gme": 0, "transcoded": 0}
        by_type_bytes: dict[str, int] = dict.fromkeys(by_type, 0)
        for entry in _meta.values():
            ft = entry.get("format_type", "")
            if ft in by_type:
                by_type[ft] += 1
                by_type_bytes[ft] += int(entry.get("size_bytes", 0) or 0)
        return {
            "total_bytes": _total_bytes,
            "entry_count": len(_meta),
            "max_bytes": settings.conversion_cache_max_bytes,
            "pinned_count": len(_pin_refs),
            "by_type_bytes": by_type_bytes,
            "by_type": by_type,
        }


def warmup_from_disk() -> int:
    """Rebuild ``_meta`` / ``_lru`` / ``_total_bytes`` from on-disk WAVs.

    The cache state is in-memory; without this, every server restart leaves
    previously-rendered WAVs orphaned on disk, so the next play re-renders
    even though the file already exists.  Run once at startup.

    Returns the number of entries adopted."""
    global _total_bytes
    base = get_conversion_cache_dir()
    if not base.exists():
        return 0
    adopted = 0
    with _state_lock:
        for fmt in ("sid", "midi", "tracker", "uade", "hvl", "gme", "transcoded"):
            sub = base / fmt
            if not sub.exists():
                continue
            for wav in sub.rglob("*.wav"):
                # ``foo.partial.wav`` is a half-written render left behind by
                # a crashed ffmpeg / sidplayfp.  Adopting it as a real cache
                # entry would serve a truncated/corrupt stream to the next
                # caller.  Unlink and skip — the next playback will trigger
                # a fresh render.
                if wav.name.endswith(".partial.wav"):
                    try:
                        wav.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                key = wav.stem
                if key in _meta:
                    continue  # already adopted (idempotent)
                try:
                    st = wav.stat()
                except OSError:
                    continue
                _meta[key] = {
                    "path": str(wav),
                    "size_bytes": st.st_size,
                    "format_type": fmt,
                    # mtime is the closest proxy to "last-touched"; the LRU
                    # entry below uses the same value so subsequent eviction
                    # decisions are sensible from the first request.
                    "created_at": st.st_mtime,
                }
                _lru[key] = st.st_mtime
                _total_bytes += st.st_size
                adopted += 1
        # Order _lru by mtime so the oldest entries are first (LRU candidates).
        if adopted:
            ordered = sorted(_lru.items(), key=lambda kv: kv[1])
            _lru.clear()
            for k, t in ordered:
                _lru[k] = t
    return adopted


async def purge_sid_entries_for(
    track_ids: list[str],
    keep_duration: dict[str, int | list[int] | set[int]] | None = None,
) -> int:
    """Remove SID cache entries for the given track_ids.

    ``keep_duration`` maps each track id to a single duration or an iterable of
    valid durations — entries whose embedded ``__dur{N}`` matches any value are
    preserved.  Useful after HVSC rescan: pass the post-rescan length so the
    correct-duration WAV survives while stale renders (e.g. the pre-HVSC global
    default) are evicted.  Multi-subsong tracks pass the set of all
    ``hvsc_lengths`` so every still-relevant subsong render survives.
    """
    if not track_ids:
        return 0
    raw = keep_duration or {}
    keep: dict[str, set[int]] = {}
    for tid, v in raw.items():
        if isinstance(v, (int, float)):
            keep[tid] = {int(v)}
        else:
            keep[tid] = {int(x) for x in v if isinstance(x, (int, float))}
    prefixes = {tid: f"{tid}__sub" for tid in track_ids}
    victims: list[str] = []
    with _state_lock:
        for key in list(_meta.keys()):
            for tid, prefix in prefixes.items():
                if not key.startswith(prefix):
                    continue
                # Preserve entries whose embedded duration is still valid.
                if tid in keep:
                    try:
                        dur_str = key.rsplit("__dur", 1)[-1]
                        if int(dur_str) in keep[tid]:
                            break  # keep this entry; stop matching tids
                    except (ValueError, IndexError):
                        pass
                victims.append(key)
                break
    for key in victims:
        await asyncio.to_thread(_purge_entry, key)
    return len(victims)


async def clear_cache(types: list[str] | None = None) -> dict:
    """Wipe cached conversion files + in-memory metadata.

    When ``types`` is None (default), wipe everything.  Otherwise wipe only
    the named format types (any of ``sid`` / ``midi`` / ``tracker`` /
    ``transcoded``) — useful for the Settings UI "Clear transcode cache"
    button so a user can reclaim DSD/ALAC disk without losing their
    sidplayfp / fluidsynth / openmpt renders (which are expensive to
    regenerate from scratch)."""
    global _total_bytes

    all_types = {"sid", "midi", "tracker", "uade", "hvl", "gme", "transcoded"}
    selected = set(types) if types else all_types
    selected &= all_types
    if not selected:
        return {"deleted_files": 0, "types": []}

    cache_dir = get_conversion_cache_dir()

    def _purge_disk() -> int:
        n = 0
        for sub in selected:
            d = cache_dir / sub
            if d.exists():
                for f in d.rglob("*.wav"):
                    try:
                        f.unlink(missing_ok=True)
                        n += 1
                    except OSError:
                        pass
        return n

    deleted = await asyncio.to_thread(_purge_disk)
    with _state_lock:
        if selected == all_types:
            _meta.clear()
            _lru.clear()
            _total_bytes = 0
        else:
            # Surgical purge: only remove meta entries whose format_type
            # matches the selection.  Recompute _total_bytes to match.
            for key in list(_meta.keys()):
                if _meta[key].get("format_type") in selected:
                    _meta.pop(key, None)
                    _lru.pop(key, None)
            _total_bytes = sum(int(e.get("size_bytes", 0) or 0)
                               for e in _meta.values())
    log.info("Conversion cache cleared (%s): %d files",
             ",".join(sorted(selected)), deleted)
    return {"deleted_files": deleted, "types": sorted(selected)}
