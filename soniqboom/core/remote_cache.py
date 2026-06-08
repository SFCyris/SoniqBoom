# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LRU file cache for remote audio tracks.

Files fetched from SMB/FTP sources are cached locally so that playback is
instant on subsequent access.  The cache is size-limited and evicts
least-recently-accessed files when the limit is exceeded.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soniqboom.core.filesource import FileSource

log = logging.getLogger(__name__)

_DEFAULT_MAX_MB = 2048


def _flac_has_seektable(path: Path) -> bool | None:
    """Walk the FLAC metadata block chain to see whether a SEEKTABLE block
    is present.

    Returns True / False, or None if the file isn't a recognisable FLAC.
    No external tool required; just parses the metadata header bytes.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return None
            while True:
                hdr = f.read(4)
                if len(hdr) < 4:
                    return False
                is_last = (hdr[0] & 0x80) != 0
                block_type = hdr[0] & 0x7F
                block_len = (hdr[1] << 16) | (hdr[2] << 8) | hdr[3]
                if block_type == 3:        # SEEKTABLE
                    return True
                if is_last:
                    return False
                f.seek(block_len, os.SEEK_CUR)
    except OSError:
        return None


def _add_flac_seektable_best_effort(path: Path) -> None:
    """Insert a SEEKTABLE block into ``path`` via ``metaflac`` if absent.

    Why: browsers seek FLAC by computing a byte offset from the SEEKTABLE.
    A FLAC without one forces the demuxer to scan from the start on every
    seek, which Chromium can't do — it bails with
    ``DEMUXER_ERROR_COULD_NOT_PARSE: PTS is not defined``.  Adding a
    SEEKTABLE is a metadata-only operation (no re-encoding), preserves
    the audio bit-exact, and costs ~1 KB per minute of audio.

    Best-effort: any failure is logged and ignored — the file is still
    playable end-to-end, just not seekable.
    """
    has_st = _flac_has_seektable(path)
    if has_st is None or has_st:
        return
    metaflac = shutil.which("metaflac")
    if not metaflac:
        log.debug("metaflac not found on PATH — FLAC %s will not be seekable", path)
        return
    try:
        # 10 s spacing → ~6 seekpoints per minute, ~360 for an hour-long
        # album track.  At ~18 bytes per point this is < 7 KB of metadata.
        subprocess.run(
            [metaflac, "--add-seekpoint=10s", "--no-utf8-convert", str(path)],
            check=True, timeout=30, capture_output=True,
        )
        log.info("Added SEEKTABLE to FLAC cache: %s", path.name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("metaflac --add-seekpoint failed for %s: %s", path, exc)


class RemoteCache:
    """Download-and-cache layer between FileSource and the rest of the app."""

    def __init__(self, cache_root: Path, max_mb: int = _DEFAULT_MAX_MB):
        # Constructor validation mirrors ``set_max_mb`` — a misconfigured
        # ``remote_cache_max_mb: 0`` in the config file would otherwise
        # silently evict every cached file on first fetch.
        if not isinstance(max_mb, (int, float)) or max_mb <= 0:
            raise ValueError(
                f"RemoteCache max_mb must be a positive number, got {max_mb!r}",
            )
        self._root = cache_root
        self._max_bytes = int(max_mb) * 1024 * 1024
        self._index_path = cache_root / "_cache_index.json"
        self._index: dict[str, dict] = {}
        # Running total avoids the O(N) ``sum(...)`` over every entry on
        # each eviction check (previously called once per fetch).
        self._total_bytes = 0
        # ``fetch`` runs under ``run_in_executor`` from multiple concurrent
        # stream endpoints, so any read-modify-write on ``_index`` /
        # ``_total_bytes`` needs serialising — otherwise the running total
        # drifts and eviction misbehaves.
        self._mutex = threading.Lock()
        # Per-key fetch lock so two concurrent stream requests for the
        # same remote file don't both download it in parallel (each
        # eating SMB bandwidth + writing the same local path with both
        # workers double-counting ``_total_bytes``).  Mirrors the per-key
        # asyncio.Lock pattern in conversion_cache._lock_for; uses a
        # ``WeakValueDictionary`` so an idle key's lock is GC'd once no
        # caller still holds it (otherwise the dict would grow once per
        # distinct remote path forever).
        self._fetch_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = (
            weakref.WeakValueDictionary()
        )
        self._fetch_locks_guard = threading.Lock()
        self._load_index()
        self._total_bytes = sum(e.get("size", 0) for e in self._index.values())

    def _load_index(self) -> None:
        if self._index_path.exists():
            try:
                loaded = json.loads(self._index_path.read_text())
            except (json.JSONDecodeError, OSError):
                loaded = {}
            # A corrupt index that parsed to ``[]`` / ``42`` / ``null``
            # would have crashed the next ``.values()`` call — coerce to
            # an empty dict instead so init can't blow up here.
            self._index = loaded if isinstance(loaded, dict) else {}

    def _save_index(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index))
        tmp.replace(self._index_path)

    @staticmethod
    def _cache_key(share_id: str, remote_path: str) -> str:
        return hashlib.sha256(f"{share_id}:{remote_path}".encode()).hexdigest()[:24]

    def _cache_path(self, key: str, remote_path: str) -> Path:
        ext = Path(remote_path).suffix
        return self._root / f"{key}{ext}"

    def get_cached(self, share_id: str, remote_path: str) -> Path | None:
        from soniqboom.core import cache_stats
        key = self._cache_key(share_id, remote_path)
        with self._mutex:
            entry = self._index.get(key)
            if entry is None:
                cache_stats.miss("remote")
                return None
            local = Path(entry["local"])
        if not local.exists():
            with self._mutex:
                stale = self._index.pop(key, None)
                if stale:
                    self._total_bytes = max(
                        0, self._total_bytes - stale.get("size", 0),
                    )
            cache_stats.miss("remote")
            return None
        # Update access time in memory only.  The previous code wrote the
        # entire JSON index to disk on every cache *hit*, hammering the
        # filesystem during steady playback — last_access is best-effort
        # LRU bookkeeping, not durable data.
        with self._mutex:
            entry["last_access"] = time.time()
        cache_stats.hit("remote")
        return local

    def _lock_for_key(self, key: str) -> threading.Lock:
        """Get (or lazily create) the per-key fetch lock.

        Mutex-protected creation so two callers colliding here can't end up
        with two locks that don't serialise against each other.  The lock
        is stored weakly — once both callers release their local reference
        the entry vanishes from the dict (no unbounded growth).
        """
        with self._fetch_locks_guard:
            lock = self._fetch_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._fetch_locks[key] = lock
        return lock

    def fetch(self, share_id: str, remote_path: str, source: FileSource) -> Path:
        cached = self.get_cached(share_id, remote_path)
        if cached is not None:
            return cached

        key = self._cache_key(share_id, remote_path)
        # Per-key lock prevents two parallel fetches of the same SMB/FTP
        # path from both pulling the file across the wire and racing on the
        # local write.  Double-checked locking: the cache may have filled
        # while we awaited the lock.
        lock = self._lock_for_key(key)
        with lock:
            cached = self.get_cached(share_id, remote_path)
            if cached is not None:
                return cached

            local = self._cache_path(key, remote_path)
            local.parent.mkdir(parents=True, exist_ok=True)

            # Heavy I/O happens outside the index mutex; only the
            # dict-update is serialised against eviction / total-byte
            # accounting.  Two concurrent fetches of *different* remote
            # files used to race on ``+=`` against ``_total_bytes`` and
            # the ``_save_index`` write — the global mutex still covers
            # that, on top of the per-key serialisation we added here.
            data = source.read_file(remote_path)
            local.write_bytes(data)

            # Post-fetch: FLAC files without a SEEKTABLE block can't be
            # seeked reliably by the browser's audio element — Chromium's
            # FFmpegDemuxer fails with "PTS is not defined" when the Range
            # request lands mid-frame.  Inject a SEEKTABLE losslessly with
            # ``metaflac`` so seeking just works.  Best-effort: skipped if
            # the file isn't FLAC, ``metaflac`` isn't installed, or the
            # file already has a SEEKTABLE.  Modifies ONLY the cache copy;
            # the source on the FTP/SMB share is untouched.
            if remote_path.lower().endswith(".flac"):
                _add_flac_seektable_best_effort(local)

            size = len(data)
            with self._mutex:
                self._index[key] = {
                    "share_id": share_id,
                    "remote": remote_path,
                    "local": str(local),
                    "size": size,
                    "fetched": time.time(),
                    "last_access": time.time(),
                }
                self._total_bytes += size
                self._save_index()
        self._evict_if_needed()
        return local

    def _evict_if_needed(self) -> None:
        # Snapshot under lock so a parallel fetch can't mutate during the
        # sort/scan, then drop the lock to do the actual file unlinks.
        with self._mutex:
            if self._total_bytes <= self._max_bytes:
                return
            by_access = sorted(
                self._index.items(),
                key=lambda kv: kv[1].get("last_access", 0),
            )
        removed = 0
        evicted_keys: list[str] = []
        for key, entry in by_access:
            if self._total_bytes - removed <= self._max_bytes:
                break
            path = Path(entry["local"])
            try:
                sz = entry.get("size", 0)
                path.unlink(missing_ok=True)
                removed += sz
                evicted_keys.append(key)
            except OSError:
                continue
        if evicted_keys:
            with self._mutex:
                for k in evicted_keys:
                    self._index.pop(k, None)
                self._total_bytes = max(0, self._total_bytes - removed)
                self._save_index()
            log.info("Cache evicted %d bytes", removed)

    def invalidate_share(self, share_id: str) -> int:
        with self._mutex:
            keys = [k for k, v in self._index.items() if v.get("share_id") == share_id]
            entries: list[dict] = []
            for key in keys:
                e = self._index.pop(key, None)
                if e:
                    entries.append(e)
                    self._total_bytes = max(0, self._total_bytes - e.get("size", 0))
            if keys:
                self._save_index()
        # Unlink outside the lock — slow filesystem mustn't stall other
        # concurrent fetches.
        removed = 0
        for e in entries:
            try:
                Path(e["local"]).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return removed

    def total_size(self) -> int:
        # Use the cached running total — admin/disk-usage hits this on every
        # poll, and a full ``sum(...)`` over the index defeated the W3-A
        # optimisation that introduced the counter.  Atomic read under
        # the mutex so a concurrent fetch can't show a torn value.
        with self._mutex:
            return self._total_bytes

    def entry_count(self) -> int:
        with self._mutex:
            return len(self._index)

    @property
    def max_mb(self) -> int:
        return self._max_bytes // (1024 * 1024)

    def set_max_mb(self, mb: int) -> None:
        """Update the cache size limit and evict if now over budget.

        Validates the input so direct callers (not just the admin endpoint
        that already clamps) can't accidentally pass zero/negative and
        silently wipe the entire cache.
        """
        if not isinstance(mb, (int, float)) or mb <= 0:
            raise ValueError(f"max_mb must be a positive number, got {mb!r}")
        self._max_bytes = int(mb) * 1024 * 1024
        self._evict_if_needed()

    def clear_all(self) -> int:
        """Remove every cached file and return the count removed."""
        with self._mutex:
            entries = list(self._index.values())
            self._index.clear()
            # Reset the running counter too — otherwise total_size() keeps
            # reporting the pre-clear size and the next fetch trips eviction
            # immediately because the stale total exceeds _max_bytes.
            self._total_bytes = 0
            self._save_index()
        removed = 0
        for entry in entries:
            try:
                Path(entry["local"]).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return removed


_cache: RemoteCache | None = None


def get_cache() -> RemoteCache:
    global _cache
    if _cache is None:
        from soniqboom.config import get_data_dir
        root = Path(get_data_dir()) / "cache" / "remote"
        _cache = RemoteCache(root)
    return _cache


def init_cache(cache_root: Path, max_mb: int = _DEFAULT_MAX_MB) -> RemoteCache:
    global _cache
    _cache = RemoteCache(cache_root, max_mb)
    return _cache
