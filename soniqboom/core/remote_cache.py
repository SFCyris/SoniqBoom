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
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soniqboom.core.filesource import FileSource

log = logging.getLogger(__name__)

_DEFAULT_MAX_MB = 2048


class RemoteCache:
    """Download-and-cache layer between FileSource and the rest of the app."""

    def __init__(self, cache_root: Path, max_mb: int = _DEFAULT_MAX_MB):
        self._root = cache_root
        self._max_bytes = max_mb * 1024 * 1024
        self._index_path = cache_root / "_cache_index.json"
        self._index: dict[str, dict] = {}
        self._load_index()

    def _load_index(self) -> None:
        if self._index_path.exists():
            try:
                self._index = json.loads(self._index_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._index = {}

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
        key = self._cache_key(share_id, remote_path)
        entry = self._index.get(key)
        if entry is None:
            return None
        local = Path(entry["local"])
        if not local.exists():
            self._index.pop(key, None)
            return None
        entry["last_access"] = time.time()
        self._save_index()
        return local

    def fetch(self, share_id: str, remote_path: str, source: FileSource) -> Path:
        cached = self.get_cached(share_id, remote_path)
        if cached is not None:
            return cached

        key = self._cache_key(share_id, remote_path)
        local = self._cache_path(key, remote_path)
        local.parent.mkdir(parents=True, exist_ok=True)

        data = source.read_file(remote_path)
        local.write_bytes(data)

        self._index[key] = {
            "share_id": share_id,
            "remote": remote_path,
            "local": str(local),
            "size": len(data),
            "fetched": time.time(),
            "last_access": time.time(),
        }
        self._save_index()
        self._evict_if_needed()
        return local

    def _evict_if_needed(self) -> None:
        total = sum(e.get("size", 0) for e in self._index.values())
        if total <= self._max_bytes:
            return
        by_access = sorted(self._index.items(), key=lambda kv: kv[1].get("last_access", 0))
        removed = 0
        for key, entry in by_access:
            if total - removed <= self._max_bytes:
                break
            path = Path(entry["local"])
            try:
                sz = entry.get("size", 0)
                path.unlink(missing_ok=True)
                removed += sz
                del self._index[key]
            except OSError:
                continue
        self._save_index()
        if removed:
            log.info("Cache evicted %d bytes", removed)

    def invalidate_share(self, share_id: str) -> int:
        keys = [k for k, v in self._index.items() if v.get("share_id") == share_id]
        removed = 0
        for key in keys:
            entry = self._index.pop(key, None)
            if entry:
                Path(entry["local"]).unlink(missing_ok=True)
                removed += 1
        if keys:
            self._save_index()
        return removed

    def total_size(self) -> int:
        return sum(e.get("size", 0) for e in self._index.values())

    def entry_count(self) -> int:
        return len(self._index)

    @property
    def max_mb(self) -> int:
        return self._max_bytes // (1024 * 1024)

    def set_max_mb(self, mb: int) -> None:
        """Update the cache size limit and evict if now over budget."""
        self._max_bytes = mb * 1024 * 1024
        self._evict_if_needed()

    def clear_all(self) -> int:
        """Remove every cached file and return the count removed."""
        removed = 0
        for key, entry in list(self._index.items()):
            try:
                Path(entry["local"]).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        self._index.clear()
        self._save_index()
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
