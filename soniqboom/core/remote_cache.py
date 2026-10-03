# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LRU file cache for remote audio tracks.

Files fetched from SMB/FTP/WebDAV sources are cached locally so that playback
is instant on subsequent access.  The cache is size-limited and evicts
least-recently-accessed files when the limit is exceeded.

Downloads stream into a ``.part`` file chunk by chunk.  While one runs, it is
published as an in-flight download (path, final size, bytes so far, a
progress signal) so a player can start on the head of the file while the tail
is still arriving (``open_progressive``); the file is moved into place and
indexed — counted against the budget — only once complete.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

from soniqboom.core import forksafe

if TYPE_CHECKING:
    from soniqboom.core.filesource import FileSource

log = logging.getLogger(__name__)

_DEFAULT_MAX_MB = 2048

# Entries younger than this are never evicted: fetch() hands out a path
# and the caller opens it moments later — evicting inside that window
# turns a cache hit into FileNotFoundError.  A scan that floods the
# cache with fresh entries may transiently overshoot the byte budget by
# the grace window's working set; that's disk headroom, not corruption.
_EVICTION_GRACE_S = 60
# How long a playback (stream-lane) fetch waits for a background fetch of the
# same file before downloading it on its own lane (see ``RemoteCache.fetch``).
_STREAM_LOCK_WAIT_S = 0.5

# Download read size, and how much new data wakes a progressive reader.
_DL_CHUNK = 1024 * 1024
_DL_NOTIFY_BYTES = 256 * 1024
# A download that fails part-way is resumed from where it stopped (REST /
# Range / seek) this many times before the fetch fails.
_DL_ATTEMPTS = 3
# Background downloads started for progressive playback (they outlive the
# request that started them so the file still lands in the cache).
_DL_WORKERS = 32
# How long ``open_progressive`` waits for a download to become attachable
# before it simply waits for the whole file.
_PROGRESSIVE_WAIT_S = 30.0

# Eviction telemetry — during a scan whose working set exceeds the cache
# limit, EVERY insert evicts (1,489 per-batch INFO lines / 22 GB churn
# observed 2026-07-02).  Per-batch detail goes to DEBUG; INFO gets one
# aggregated line per minute.
_evict_stats = {"bytes": 0, "entries": 0, "last_emit": 0.0}
_evict_stats_lock = threading.Lock()


def _note_eviction(nbytes: int, nentries: int, cache: "RemoteCache") -> None:
    with _evict_stats_lock:
        _evict_stats["bytes"] += nbytes
        _evict_stats["entries"] += nentries
        now = time.time()
        if now - _evict_stats["last_emit"] < 60:
            return
        agg_b, agg_n = _evict_stats["bytes"], _evict_stats["entries"]
        _evict_stats.update(bytes=0, entries=0, last_emit=now)
    log.info(
        "Cache evicted %.2f GB in %d entries over the last minute "
        "(cache %d/%d MB)",
        agg_b / 1e9, agg_n, cache.total_size() // (1024 * 1024), cache.max_mb,
    )


def _flac_seektable_in_prefix(path, available: int) -> bool | str | None:
    """SEEKTABLE verdict for a FLAC whose first ``available`` bytes are on disk
    (a download in progress): True / False once the metadata chain answers,
    ``"not-flac"`` when the file doesn't start with ``fLaC`` (the cache then
    leaves it alone), None while more bytes are needed.  Same walk as
    :func:`_flac_has_seektable`, which decides whether the finished copy gets
    a SEEKTABLE inserted (which would shift every audio byte)."""
    try:
        with open(path, "rb") as f:
            if available < 4:
                return None
            if f.read(4) != b"fLaC":
                return "not-flac"
            pos = 4
            while True:
                if pos + 4 > available:
                    return None
                f.seek(pos)
                hdr = f.read(4)
                if len(hdr) < 4:
                    return None
                if hdr[0] & 0x7F == 3:
                    return True
                if hdr[0] & 0x80:
                    return False
                pos += 4 + ((hdr[1] << 16) | (hdr[2] << 8) | hdr[3])
    except OSError:
        return None


class _SourceChanged(OSError):
    """The remote file changed size while it was being downloaded."""


class _DownloadCancelled(OSError):
    """The download was stopped (its share was removed)."""


# A subset of a remote ZIP (``core.remote_zip``) is cached under
# ``"<zip>::<first member>#subset2-<archive size>-<archive mtime>.zip"``
# (``#subset2-``: always with the member's companions — subsets an earlier
# build cached without them are never found again and age out).
_SUBSET_TAG = "#subset2-"


def subset_name(zip_rel: str, first_member: str, size: int, mtime: float) -> str:
    return (f"{zip_rel}::{first_member}{_SUBSET_TAG}{int(size)}-"
            f"{max(0, int(mtime or 0))}.zip")


def _subset_parts(remote_path: str) -> tuple[str, str, int, int] | None:
    """``(zip_rel, first_member, archive_size, archive_mtime)`` of a subset
    cache name (None for any other cache entry)."""
    head, sep, tail = str(remote_path).rpartition(_SUBSET_TAG)
    if not sep or "::" not in head or not tail.endswith(".zip"):
        return None
    size, _, mtime = tail[:-4].partition("-")
    if not size.isdigit():
        return None
    try:
        mt = int(mtime or 0)
    except ValueError:
        return None
    zip_rel, first = head.split("::", 1)
    return zip_rel, first, int(size), mt


def _resumable(exc: BaseException) -> bool:
    """A download error worth resuming from where it stopped — not a missing
    file, a refused permission or a file that changed under us (those won't
    change on a retry)."""
    if isinstance(exc, (FileNotFoundError, PermissionError, _SourceChanged,
                        _DownloadCancelled)):
        return False
    if str(exc)[:3] in ("550", "530", "553", "501", "504"):   # ftplib.error_perm replies
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:        # HTTP client errors
        return False
    return isinstance(exc, Exception)


class _Download:
    """A download into the cache that is running right now."""

    __slots__ = ("key", "lane", "tmp", "size", "written", "started", "done",
                 "ok", "error", "final", "listeners", "share_id", "cancelled")

    def __init__(self, key: str, lane: str, tmp: Path, share_id: str = "") -> None:
        self.key = key
        self.lane = lane
        self.tmp = tmp
        self.share_id = share_id
        self.cancelled = False
        self.size: int | None = None      # the whole file's size, once known
        self.written = 0
        self.started = time.monotonic()
        self.done = False
        self.ok = False
        self.error: BaseException | None = None
        self.final: Path | None = None    # the cache path once promoted
        self.listeners: list = []


class ProgressiveRead:
    """A reader attached to an in-flight download (``open_progressive``).

    ``fd`` is a read descriptor on the growing file, opened while it still had
    its ``.part`` name — it stays readable after the file is moved into the
    cache, so the caller owns it (close it when done).  ``size`` is the final
    size; ``written`` grows as data lands; ``done`` / ``ok`` / ``final`` say
    how the download ended.  ``add_listener(cb)`` registers a no-argument
    callback run (on the download thread) on progress and at the end."""

    def __init__(self, cache: "RemoteCache", dl: _Download, fd: int) -> None:
        self._cache = cache
        self._dl = dl
        self.fd = fd
        self.path = dl.tmp
        self.size: int = int(dl.size or 0)

    @property
    def written(self) -> int:
        return self._dl.size if (self._dl.done and self._dl.ok) else self._dl.written

    @property
    def done(self) -> bool:
        return self._dl.done

    @property
    def ok(self) -> bool:
        return self._dl.ok

    @property
    def final(self) -> Path | None:
        return self._dl.final

    @property
    def rate(self) -> float:
        """Bytes per second so far (0 before the first bytes)."""
        el = time.monotonic() - self._dl.started
        return self._dl.written / el if el > 0 else 0.0

    def add_listener(self, cb) -> None:
        with self._cache._dl_cond:
            if not self._dl.done:
                self._dl.listeners.append(cb)
                return
        cb()                               # already finished — fire once now

    def remove_listener(self, cb) -> None:
        with self._cache._dl_cond:
            try:
                self._dl.listeners.remove(cb)
            except ValueError:
                pass


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
        # forksafe: fetch() runs in executor threads of the CF-initialised
        # server — a plain subprocess.run fork here is the macOS segfault
        # hazard (see core/forksafe.py).
        forksafe.run(
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
        # Stream-lane fetches of ONE key additionally queue on this lock, so
        # at most one of them ever bypasses a background holder of the main
        # lock (see ``fetch``) — concurrent playback requests for the same
        # file (stream GET + waveform + VU + Range re-requests) must share a
        # single download, not each pull the whole file into RAM.
        self._stream_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = (
            weakref.WeakValueDictionary()
        )
        # Serialises index-file writes that happen OUTSIDE _mutex (the
        # eviction hot path snapshots under _mutex, writes under this).
        self._save_lock = threading.Lock()
        # In-flight downloads (key → _Download; a playback-lane download wins
        # the slot over a background one) and the background fetches
        # ``open_progressive`` started, guarded by one condition that every
        # bit of download progress notifies.
        self._downloads: dict[str, _Download] = {}
        # Every download running right now (``_downloads`` holds one per key,
        # and only once it has started) — what ``cancel_share_downloads`` stops.
        self._active_dls: set[_Download] = set()
        self._bg_fetches: dict[str, concurrent.futures.Future] = {}
        # Cached subsets of remote ZIPs per (share, archive) — their names, so
        # a play finds its member's subset without asking the share for the
        # archive's size first (``find_subset``).  Built from the index at
        # start; names whose entry left the index are dropped on lookup.
        self._subsets: dict[tuple[str, str], set[str]] = {}
        self._dl_cond = threading.Condition()
        self._dl_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._load_index()
        self._total_bytes = sum(e.get("size", 0) for e in self._index.values())
        for e in self._index.values():
            self._note_subset(e.get("share_id", ""), e.get("remote", ""))
        self._sweep_part_files()

    def _note_subset(self, share_id: str, remote_path: str) -> None:
        parts = _subset_parts(remote_path) if share_id and remote_path else None
        if parts is not None:
            self._subsets.setdefault((share_id, parts[0]), set()).add(remote_path)

    def _sweep_part_files(self) -> None:
        """Remove ``*.part`` temps and ``*.evicted-*`` throwaways a killed
        process left behind — never indexed, so nothing else would remove them."""
        try:
            for p in [*self._root.rglob("*.part"), *self._root.rglob("*.evicted-*")]:
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

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
        """Persist the index — best-effort, NEVER raises.

        A raise here used to propagate out of eviction BEFORE the victim
        unlink loop ran: entries already popped from the index stayed on
        disk as permanently untracked orphans — and the most likely cause
        (ENOSPC on the cache volume) is exactly the condition eviction
        exists to relieve.  The index is advisory LRU bookkeeping; losing
        one write is recoverable, skipping unlinks is not.
        """
        self._write_index_payload(json.dumps(self._index))

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
                    # Persist the pop — an unpersisted stale entry came
                    # back after restart as phantom bytes in _total_bytes
                    # and triggered spurious eviction of live entries.
                    self._save_index()
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

    def peek_cached(self, share_id: str, remote_path: str) -> bool:
        """Is *remote_path* indexed?  No hit/miss statistics, no LRU bump —
        for a pre-check followed by a real lookup."""
        key = self._cache_key(share_id, remote_path)
        with self._mutex:
            return key in self._index

    def validate_size(self, share_id: str, remote_path: str,
                      expected_size: int) -> None:
        """Drop the cached copy of *remote_path* — and the cached subsets of
        it, when it is a ZIP — when its size no longer matches the live
        directory listing.

        The cache is keyed on path alone with no mtime/size validation —
        a remote file that CHANGED (e.g. a scene archive updated on the
        share) would otherwise be served from the stale local copy
        forever.  The scanner calls this with the listing size before
        enumerating/extracting an archive; playback paths don't (a track
        mid-stream keeps its bytes).  Size-only: cheap, catches the
        overwhelmingly common case; a same-size content change slips
        through until the entry is evicted naturally.
        """
        if expected_size <= 0:
            return
        key = self._cache_key(share_id, remote_path)
        doomed: list[str] = []
        with self._mutex:
            entry = self._index.get(key)
            whole = entry is not None and entry.get("size") != expected_size
            if whole:
                self._index.pop(key)
                self._total_bytes = max(0, self._total_bytes - entry.get("size", 0))
                doomed.append(entry.get("local") or "")
            stale = self._mark_subsets_stale_locked(
                share_id, remote_path, lambda p: p[2] != expected_size)
            if doomed or stale:
                self._save_index()
        for local in doomed:
            if local:
                try:
                    Path(local).unlink(missing_ok=True)
                except OSError:
                    pass
        if doomed:
            log.info("Remote cache: dropped stale copy of %s (size changed)", remote_path)
        if stale:
            log.info("Remote cache: %d cached subset(s) of %s are stale (size changed)",
                     stale, remote_path)

    def _mark_subsets_stale_locked(self, share_id: str, zip_rel: str, pred) -> int:
        """Flag the cached subsets of ``share_id:zip_rel`` whose name parts
        match *pred* as stale: ``find_subset`` skips them from now on, while
        their files stay (a play that found one a moment ago may be about to
        extract from it) until LRU eviction removes them.  Caller holds
        ``_mutex``; returns how many were newly flagged."""
        n = 0
        for name in list(self._subsets.get((share_id, zip_rel), ())):
            parts = _subset_parts(name)
            entry = self._index.get(self._cache_key(share_id, name))
            if parts is None or entry is None or entry.get("stale") or not pred(parts):
                continue
            entry["stale"] = True
            n += 1
        return n

    def find_subset(self, share_id: str, zip_rel: str, first_member: str, *,
                    peek: bool = False) -> Path | None:
        """The newest cached subset of ``share_id:zip_rel`` built for
        *first_member* (``core.remote_zip``) — found without asking the share
        anything.  ``peek``: no hit/miss statistics, no LRU bump."""
        best = None
        with self._mutex:
            names = self._subsets.get((share_id, zip_rel))
            best_t = -1.0
            for name in list(names or ()):
                entry = self._index.get(self._cache_key(share_id, name))
                if entry is None:
                    names.discard(name)            # evicted / dropped meanwhile
                    continue
                parts = _subset_parts(name)
                if parts is None or parts[1] != first_member or entry.get("stale"):
                    continue
                t = float(entry.get("fetched", 0) or 0)
                if t > best_t:
                    best, best_t, best_local = name, t, entry.get("local")
            if names is not None and not names:
                self._subsets.pop((share_id, zip_rel), None)
        if best is None:
            return None
        if peek:
            local = Path(best_local) if best_local else None
            return local if local is not None and local.exists() else None
        return self.get_cached(share_id, best)

    def unretire(self, share_id: str, remote_path: str) -> None:
        """Clear the stale flag of a subset that matches the archive's current
        version again (it reverted) — ``find_subset`` serves it again."""
        with self._mutex:
            entry = self._index.get(self._cache_key(share_id, remote_path))
            if entry is not None and entry.pop("stale", None):
                self._save_index()

    def drop_stale_subsets(self, share_id: str, zip_rel: str, size: int,
                           mtime: float) -> int:
        """Retire the cached subsets of ``share_id:zip_rel`` built from another
        version of the archive than ``(size, mtime)`` (see
        ``_mark_subsets_stale_locked``); returns how many."""
        want = (int(size), max(0, int(mtime or 0)))
        with self._mutex:
            n = self._mark_subsets_stale_locked(
                share_id, zip_rel, lambda p: (p[2], p[3]) != want)
            if n:
                self._save_index()
        return n

    def cancel_share_downloads(self, share_id: str) -> int:
        """Stop the downloads running for *share_id* (its share was removed:
        their files would land in a cache that was just cleared of it).  They
        end at their next chunk; returns how many were told to."""
        with self._dl_cond:
            hits = [dl for dl in self._active_dls if dl.share_id == share_id]
            for dl in hits:
                dl.cancelled = True
        return len(hits)

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

    def _stream_lock_for_key(self, key: str) -> threading.Lock:
        with self._fetch_locks_guard:
            lock = self._stream_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._stream_locks[key] = lock
        return lock

    def fetch(self, share_id: str, remote_path: str, source: FileSource,
              *, lane: str = "stream") -> Path:
        """Download *remote_path* into the cache (or return the cached copy).

        ``lane`` is forwarded to ``FileSource.open_stream`` so pooled-FTP
        backends can prioritise correctly: playback callers keep the
        default ``"stream"`` priority lane; the scanner passes
        ``lane="scan"`` so multi-hundred-MB archive pulls don't starve a
        concurrent play request (observed: 22 GB of scan traffic riding
        the stream lane of a 2-connection pool).
        """
        cached = self.get_cached(share_id, remote_path)
        if cached is not None:
            return cached

        key = self._cache_key(share_id, remote_path)
        # Per-key lock prevents two parallel fetches of the same SMB/FTP
        # path from both pulling the file across the wire and racing on the
        # local write.  Double-checked locking: the cache may have filled
        # while we awaited the lock.
        lock = self._lock_for_key(key)
        if lane != "stream":
            with lock:
                # Double-checked: the cache may have filled while we waited.
                cached = self.get_cached(share_id, remote_path)
                if cached is not None:
                    return cached
                local = self._download_to_cache(key, share_id, remote_path, source, lane)
        else:
            # A PLAYBACK fetch never queues behind a BACKGROUND (scan /
            # prewarm) fetch of the same file for longer than
            # ``_STREAM_LOCK_WAIT_S``: that one may sit on the pool's
            # low-priority lane for many seconds, so after the wait it
            # downloads on its own (stream) lane into a private temp.  Stream
            # fetches of one key queue on ``_stream_lock_for_key`` first, so
            # only ONE of them can bypass; the rest find the file cached.
            slock = self._stream_lock_for_key(key)
            # Queue behind the stream fetch ahead of us, but keep checking the
            # cache: whichever download (the background one or the bypass)
            # lands first serves every waiting playback request.
            while not slock.acquire(timeout=0.1):
                cached = self.get_cached(share_id, remote_path)
                if cached is not None:
                    return cached
            try:
                cached = self.get_cached(share_id, remote_path)
                if cached is not None:
                    return cached
                got = lock.acquire(timeout=_STREAM_LOCK_WAIT_S)
                try:
                    cached = self.get_cached(share_id, remote_path)
                    if cached is not None:
                        return cached
                    local = self._download_to_cache(key, share_id, remote_path, source, lane)
                finally:
                    if got:
                        lock.release()
            finally:
                slock.release()
        # Evict + persist in one pass (a single index write per fetch —
        # the old insert-save + evict-save double write hammered the disk
        # at scan rates).  ``protect_key`` keeps the entry we are about to
        # return: the previous code could evict the just-inserted file
        # before the caller ever opened it — guaranteed when a single
        # entry exceeded the cache limit (fetch returned an already-
        # unlinked path → FileNotFoundError downstream).
        self._evict_if_needed(protect_key=key)
        return local

    def _download_to_cache(self, key: str, share_id: str, remote_path: str,
                           source: "FileSource", lane: str) -> Path:
        """Stream into a private temp file, finish it (FLAC seektable), move it
        into place atomically and index it — counting its bytes only if no
        concurrent fetch of the same key indexed it first.

        While it runs the download is published (``_downloads``) so
        ``open_progressive`` readers can play the head of the file before
        the tail arrives.  Nothing is held in RAM beyond one chunk."""
        local = self._cache_path(key, remote_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + f".{uuid.uuid4().hex}.part")
        dl = _Download(key, lane, tmp, share_id)
        with self._dl_cond:
            self._active_dls.add(dl)
        try:
            # Heavy I/O happens outside the index mutex; only the dict update
            # is serialised against eviction / total-byte accounting.
            self._stream_into(dl, source, remote_path, lane)
            size = dl.written
            if dl.cancelled:                  # its share went while it finished
                raise _DownloadCancelled(f"{remote_path}: download cancelled")
            # FLAC files without a SEEKTABLE can't be seeked reliably by the
            # browser (Chromium "PTS is not defined"); inject one losslessly on
            # the cache copy only (best-effort; the share is untouched).  A
            # progressive reader is never attached to such a file (inserting
            # the block shifts every audio byte) — see ``open_progressive``.
            if remote_path.lower().endswith(".flac"):
                _add_flac_seektable_best_effort(tmp)
                try:
                    size = tmp.stat().st_size      # the seektable grew the file
                except OSError:
                    pass
        except BaseException as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._end_download(dl, error=exc)
            raise
        now = time.time()
        with self._mutex:
            # Move into place and index in ONE hold: an eviction that claimed
            # this key can't delete the fresh file in between (it re-checks the
            # index under the same mutex right before its unlink).
            try:
                if dl.cancelled:              # (after the FLAC rewrite too)
                    raise _DownloadCancelled(f"{remote_path}: download cancelled")
                os.replace(tmp, local)
            except BaseException as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._end_download(dl, error=exc)
                raise
            prev = self._index.get(key)
            self._index[key] = {
                "share_id": share_id,
                "remote": remote_path,
                "local": str(local),
                "size": size,
                "fetched": now,
                "last_access": now,
            }
            self._total_bytes += size - (int(prev.get("size", 0) or 0) if prev else 0)
        self._end_download(dl, final=local)
        return local

    def _stream_into(self, dl: _Download, source: "FileSource",
                     remote_path: str, lane: str) -> None:
        """Copy the remote file into ``dl.tmp`` chunk by chunk, publishing
        progress.  A transfer that breaks part-way resumes at the byte it
        stopped (``open_stream(offset=…)``) up to ``_DL_ATTEMPTS`` times."""
        attempt = 0
        registered = False
        opener = getattr(source, "open_stream", None)
        if opener is None:             # a duck-typed source with only read_file
            from soniqboom.core.filesource import _BytesStream

            def opener(path, *, offset=0, lane="stream"):
                return _BytesStream(source.read_file(path, lane=lane), offset)
        with open(dl.tmp, "wb", buffering=0) as fh:
            while True:
                # Resume exactly at the last byte counted: a write that failed
                # part-way (ENOSPC …) may have moved the file position past it.
                fh.seek(dl.written)
                fh.truncate()
                if dl.cancelled:
                    raise _DownloadCancelled(f"{remote_path}: download cancelled")
                try:
                    st = opener(remote_path, offset=dl.written, lane=lane)
                except BaseException as exc:
                    attempt += 1
                    if attempt >= _DL_ATTEMPTS or not _resumable(exc):
                        raise
                    time.sleep(0.3)
                    continue
                try:
                    if st.size is not None:
                        if dl.size is None:
                            dl.size = int(st.size)
                        elif int(st.size) != dl.size:
                            raise _SourceChanged(
                                f"{remote_path} changed size during the download "
                                f"({dl.size} → {st.size})")
                    if not registered:
                        self._register_download(dl)
                        registered = True
                    since = 0
                    while True:
                        if dl.cancelled:
                            raise _DownloadCancelled(
                                f"{remote_path}: download cancelled")
                        chunk = st.read(_DL_CHUNK)
                        if not chunk:
                            break
                        if dl.size is not None and dl.written + len(chunk) > dl.size:
                            raise _SourceChanged(
                                f"{remote_path} grew during the download")
                        view = memoryview(chunk)
                        while view:                # unbuffered: writes may be short
                            view = view[fh.write(view):]
                        dl.written += len(chunk)
                        since += len(chunk)
                        if since >= _DL_NOTIFY_BYTES:
                            since = 0
                            self._notify_download(dl)
                    if dl.size is not None and dl.written < dl.size:
                        raise OSError(f"{remote_path}: transfer ended at "
                                      f"{dl.written} of {dl.size} bytes")
                    break
                except BaseException as exc:
                    attempt += 1
                    if attempt >= _DL_ATTEMPTS or not _resumable(exc):
                        raise
                    log.info("Remote download of %s broke at %d bytes (%s) — resuming",
                             remote_path, dl.written, exc)
                    time.sleep(0.3)
                finally:
                    st.close()
        if dl.size is None:
            dl.size = dl.written

    def _register_download(self, dl: _Download) -> None:
        with self._dl_cond:
            cur = self._downloads.get(dl.key)
            # A playback-lane download takes the slot from a background one
            # (progressive readers follow the fast lane); never the reverse.
            if cur is None or cur.lane != "stream" or dl.lane == "stream":
                self._downloads[dl.key] = dl
            self._dl_cond.notify_all()

    def _notify_download(self, dl: _Download) -> None:
        with self._dl_cond:
            listeners = list(dl.listeners)
            self._dl_cond.notify_all()
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.debug("download listener failed", exc_info=True)

    def _end_download(self, dl: _Download, *, final: Path | None = None,
                      error: BaseException | None = None) -> None:
        with self._dl_cond:
            self._active_dls.discard(dl)
            dl.final = final
            dl.error = error
            dl.ok = final is not None
            dl.done = True
            if self._downloads.get(dl.key) is dl:
                del self._downloads[dl.key]
            listeners, dl.listeners = dl.listeners, []
            self._dl_cond.notify_all()
        for cb in listeners:
            try:
                cb()
            except Exception:
                log.debug("download listener failed", exc_info=True)

    def _background_fetch(self, key: str, share_id: str, remote_path: str,
                          source: "FileSource", lane: str) -> concurrent.futures.Future:
        """The running background ``fetch`` of *key* started for progressive
        playback, or a new one.  It outlives the request that started it, so
        a listener who skips away still leaves the file in the cache."""
        with self._dl_cond:
            fut = self._bg_fetches.get(key)
            if fut is not None and not fut.done():
                return fut
            if self._dl_pool is None:
                self._dl_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_DL_WORKERS, thread_name_prefix="remote-dl")
            fut = self._dl_pool.submit(self.fetch, share_id, remote_path, source, lane=lane)
            self._bg_fetches[key] = fut

        def _done(f, k=key):
            with self._dl_cond:
                if self._bg_fetches.get(k) is f:
                    del self._bg_fetches[k]
                self._dl_cond.notify_all()
        fut.add_done_callback(_done)
        return fut

    def open_progressive(self, share_id: str, remote_path: str, source: "FileSource",
                         *, lane: str = "stream",
                         wait_s: float = _PROGRESSIVE_WAIT_S) -> "Path | ProgressiveRead":
        """The cached copy of *remote_path*, or a :class:`ProgressiveRead` on
        its download in progress, so playback can start on the head of the
        file.  Starts the download (a normal ``fetch`` — same per-key /
        playback-lane locking — in a background thread) when none runs.

        Falls back to waiting for the whole file (returning its ``Path``)
        when the download's final size is unknown (the server didn't say),
        and for a FLAC without a SEEKTABLE: the cache inserts one into the
        finished copy, which shifts every audio byte a reader already got.
        Blocking — call from an executor.  Raises what the fetch raised."""
        cached = self.get_cached(share_id, remote_path)
        if cached is not None:
            return cached
        key = self._cache_key(share_id, remote_path)
        fut = self._background_fetch(key, share_id, remote_path, source, lane)
        # Only a FLAC the cache will rewrite (a SEEKTABLE inserted — metaflac
        # present) has to wait for the finished copy.
        is_flac = (remote_path.lower().endswith(".flac")
                   and shutil.which("metaflac") is not None)
        deadline = time.monotonic() + wait_s
        while not fut.done():
            with self._dl_cond:
                dl = self._downloads.get(key)
                if dl is None or dl.lane != lane or dl.done or dl.size is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._dl_cond.wait(min(left, 0.5))
                    continue
            verdict = (_flac_seektable_in_prefix(dl.tmp, dl.written)
                       if is_flac else True)
            if verdict is False:
                break                      # no SEEKTABLE → wait for the finished copy
            if verdict is not None:
                try:
                    fd = os.open(dl.tmp, os.O_RDONLY)
                except FileNotFoundError:
                    continue               # promoted in between → fut is (about to be) done
                if dl.done and not dl.ok:
                    os.close(fd)
                    break
                return ProgressiveRead(self, dl, fd)
            with self._dl_cond:
                if not dl.done and time.monotonic() < deadline:
                    self._dl_cond.wait(0.5)
            if time.monotonic() >= deadline:
                break
        return fut.result()

    def temp_path(self, suffix: str = "") -> Path:
        """A fresh ``.part`` path inside the cache (swept at start-up if a
        crash leaves it behind) for a file to hand to :meth:`put_file`."""
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root / f"{uuid.uuid4().hex}{suffix}.part"

    def put_file(self, share_id: str, remote_path: str, src: Path) -> Path:
        """Move a locally produced file (e.g. a subset of a remote archive)
        into the cache under ``(share_id, remote_path)`` and index it."""
        key = self._cache_key(share_id, remote_path)
        local = self._cache_path(key, remote_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        size = src.stat().st_size
        now = time.time()
        with self._mutex:
            os.replace(src, local)
            prev = self._index.get(key)
            self._index[key] = {
                "share_id": share_id, "remote": remote_path, "local": str(local),
                "size": size, "fetched": now, "last_access": now,
            }
            self._total_bytes += size - (int(prev.get("size", 0) or 0) if prev else 0)
            self._note_subset(share_id, remote_path)
        self._evict_if_needed(protect_key=key)
        return local

    def _evict_if_needed(self, protect_key: str | None = None) -> None:
        """Evict LRU entries over budget, then persist the index.

        Claim-then-delete: victims are popped from ``_index`` and their
        sizes subtracted from ``_total_bytes`` in ONE mutex hold, so two
        concurrent evictions can never double-count the same entry (the
        old snapshot-unlink-subtract dance let both threads count the
        same file and corrupt the running total).  The slow unlinks and
        the index write both happen outside the lock — the write used to
        hold ``_mutex`` through a disk flush on EVERY fetch, stalling
        playback ``get_cached`` calls behind it during scan floods.
        """
        now = time.time()
        victims: list[tuple[str, dict]] = []
        with self._mutex:
            if self._total_bytes > self._max_bytes:
                by_access = sorted(
                    self._index.items(),
                    key=lambda kv: kv[1].get("last_access", 0),
                )
                claimed = 0
                excess = self._total_bytes - self._max_bytes
                # Grace protects both freshly-FETCHED entries and
                # freshly-ACCESSED ones: get_cached hands out paths and
                # bumps only last_access — keying grace on "fetched"
                # alone let a just-served cache hit be unlinked before
                # the caller opened it.  BUT grace is advisory: past the
                # hard ceiling (2x budget) it is ignored, or a scan
                # flooding >budget bytes per minute would grow the cache
                # without bound (grace-skip 'continue' removed nothing).
                hard_ceiling = self._total_bytes > 2 * self._max_bytes
                for k, entry in by_access:
                    if claimed >= excess:
                        break
                    if k == protect_key:
                        continue
                    last_touch = max(entry.get("fetched", 0),
                                     entry.get("last_access", 0))
                    if (not hard_ceiling
                            and now - last_touch < _EVICTION_GRACE_S):
                        continue
                    self._index.pop(k)
                    claimed += entry.get("size", 0)
                    victims.append((k, entry))
                self._total_bytes = max(0, self._total_bytes - claimed)
            # Snapshot the payload under the mutex; write it outside.
            payload = json.dumps(self._index)
        self._write_index_payload(payload)
        if victims:
            removed = 0
            for k, entry in victims:
                # A fetch may have re-downloaded and re-indexed this very
                # key since it was claimed (a bypass download finishing
                # late): its fresh file must not be deleted from under it.
                try:
                    # Under the mutex only the check and a RENAME (atomic,
                    # fast): a re-indexed file is never touched, and the slow
                    # unlink of a big archive happens outside, so cache
                    # lookups (some on the event loop) never wait for it.
                    with self._mutex:
                        if k in self._index:
                            continue
                        src = Path(entry["local"])
                        doomed = src.with_name(f"{src.name}.evicted-{uuid.uuid4().hex}")
                        try:
                            os.replace(src, doomed)
                        except FileNotFoundError:
                            doomed = None
                    if doomed is not None:
                        doomed.unlink(missing_ok=True)
                    removed += entry.get("size", 0)
                except OSError as exc:
                    # File stays on disk but left the index — surface it
                    # so untracked orphans don't accumulate silently.
                    log.warning("Cache eviction could not unlink %s: %s",
                                entry.get("local"), exc)
            log.debug("Cache evicted %d bytes in %d entries",
                      removed, len(victims))
            _note_eviction(removed, len(victims), self)

    def _write_index_payload(self, payload: str) -> None:
        """Write a pre-serialised index snapshot — best-effort, never
        raises.  ``_save_lock`` serialises concurrent writers (two racing
        ``tmp.replace`` calls on the same tmp path corrupt the file);
        last writer wins, which is fine for advisory LRU bookkeeping."""
        try:
            with self._save_lock:
                self._root.mkdir(parents=True, exist_ok=True)
                tmp = self._index_path.with_suffix(".tmp")
                tmp.write_text(payload)
                tmp.replace(self._index_path)
        except OSError as exc:
            log.warning("Remote-cache index save failed (%s) — continuing; "
                        "the index is best-effort", exc)

    def invalidate_share(self, share_id: str) -> int:
        self.cancel_share_downloads(share_id)
        with self._mutex:
            for sk in [sk for sk in self._subsets if sk[0] == share_id]:
                del self._subsets[sk]
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
            self._subsets.clear()
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
        from soniqboom.config import get_data_dir, load_local_conf
        root = Path(get_data_dir()) / "cache" / "remote"
        # Honour the configured limit even on the lazy-fallback path — a
        # caller racing ahead of _init_network_shares used to freeze the
        # singleton at the 2048 MB default, silently evicting most of a
        # larger configured cache.  load_local_conf is mtime-cached.
        try:
            max_mb = int(load_local_conf().get("remote_cache_max_mb",
                                               _DEFAULT_MAX_MB))
        except Exception:
            max_mb = _DEFAULT_MAX_MB
        _cache = RemoteCache(root, max_mb if max_mb > 0 else _DEFAULT_MAX_MB)
    return _cache


def init_cache(cache_root: Path, max_mb: int = _DEFAULT_MAX_MB) -> RemoteCache:
    global _cache
    _cache = RemoteCache(cache_root, max_mb)
    return _cache
