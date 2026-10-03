# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Play a member of a ZIP on a network share without downloading the archive.

A 40 KB module inside a 500 MB pack used to need the whole pack locally
before the first note.  Instead, :func:`archive_for_member` reads the remote
ZIP through :class:`RangeFile` — a seekable file over ``FileSource.read_at``
with a shared block cache — so ``zipfile`` touches only the end-of-archive
record, the central directory and the member's own bytes, and writes a small
local ZIP holding just the member (plus the companion files a renderer looks
for beside it: TFMX ``smpl.*`` halves, AdLib banks, PSF libs, a Sonix
``Instruments/`` folder, other non-music files in the member's folder).  That
"subset" archive is kept in the remote cache under a key carrying the
archive's size + mtime, so the existing extraction code (display names,
companions, nested archives) runs on it unchanged.

The block cache keeps the central directory of recently used archives in
memory (keyed by share, path, size, mtime): the next member of the same pack
costs one listing ``stat`` plus the member's bytes.  A member whose subset is
cached costs nothing on the network at all: the cache finds it by name
(``RemoteCache.find_subset``) without asking the share for the archive's size
first, so it plays while the share is down too; a throttled background
``stat`` (``_revalidate_later``) retires subsets of an archive that changed.
Every caller gets the same subset for a member: it always carries the
member's companions (``_archive_companion_filter``), so a companion-less
first build (waveform, art) can't leave playback without its AdLib bank.

Falls back to fetching the whole archive (the old behaviour) when the outer
isn't a ZIP (LHA, disk images), the archive is small anyway, the member and
its companions are most of the archive, or anything about the ranged read
fails.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import threading
import weakref
import zipfile
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Callable

log = logging.getLogger(__name__)

_BLOCK = 256 * 1024
# Shared in-memory block budget (central directories + recent member data).
_BLOCK_BUDGET = 128 * 1024 * 1024
# A miss inside an announced member reads ahead up to this much in one request.
_READ_AHEAD = 16 * 1024 * 1024
# Archives below this size are simply fetched whole.
_SUBSET_MIN_ARCHIVE = 4 * 1024 * 1024
# A subset needing more than this share of the archive fetches it whole.
_SUBSET_MAX_SHARE = 0.5
# Non-music siblings bigger than this are left out of a subset.
_SIBLING_MAX = 16 * 1024 * 1024

_blocks: "OrderedDict[tuple, bytes]" = OrderedDict()
_blocks_bytes = 0
_blocks_lock = threading.Lock()
# One subset build per (share, archive, member) at a time — concurrent
# requests for the same cold member (stream + waveform + VU) share it.
_build_locks: "weakref.WeakValueDictionary[tuple, threading.Lock]" = weakref.WeakValueDictionary()
_build_locks_guard = threading.Lock()


def _block_get(key: tuple) -> bytes | None:
    with _blocks_lock:
        b = _blocks.get(key)
        if b is not None:
            _blocks.move_to_end(key)
        return b


def _block_put(key: tuple, data: bytes) -> None:
    global _blocks_bytes
    with _blocks_lock:
        old = _blocks.pop(key, None)
        if old is not None:
            _blocks_bytes -= len(old)
        _blocks[key] = data
        _blocks_bytes += len(data)
        while _blocks_bytes > _BLOCK_BUDGET and len(_blocks) > 1:
            _k, v = _blocks.popitem(last=False)
            _blocks_bytes -= len(v)


def clear_blocks() -> None:
    """Drop the in-memory block cache (tests / cache clears)."""
    global _blocks_bytes
    with _blocks_lock:
        _blocks.clear()
        _blocks_bytes = 0


class RangeFile(io.RawIOBase):
    """Read-only, seekable view of a remote file through ``source.read_at``.

    Reads are served from 256 KB blocks cached process-wide under ``ident``
    (share, path, size, mtime — a changed file is a different identity).
    Missing blocks are fetched with one ``read_at`` per contiguous run; inside
    a window announced with :meth:`expect` a miss reads ahead to the window's
    end (≤ ``_READ_AHEAD``), so a member streams in a request or two."""

    def __init__(self, source, path: str, size: int, ident: tuple, lane: str = "stream"):
        super().__init__()
        self._source = source
        self._path = path
        self._size = int(size)
        self._ident = ident
        self._lane = lane
        self._pos = 0
        self._window: tuple[int, int] | None = None
        self.requests = 0                     # read_at calls made (diagnostics)
        self.fetched = 0                      # bytes they returned

    # ── io plumbing ───────────────────────────────────────────────────
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self._pos + offset
        elif whence == os.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"bad whence {whence}")
        if pos < 0:
            raise OSError("negative seek position")
        self._pos = pos
        return pos

    def readinto(self, b) -> int:
        n = min(len(b), self._size - self._pos)
        if n <= 0:
            return 0
        data = self._read(self._pos, n)
        b[:len(data)] = data
        self._pos += len(data)
        return len(data)

    # ── block cache ───────────────────────────────────────────────────
    def expect(self, start: int, end: int) -> None:
        """Announce that bytes ``[start, end)`` will be read sequentially."""
        self._window = (max(0, start), min(self._size, end))

    def _fetch_run(self, first: int, last: int) -> None:
        off = first * _BLOCK
        want = min(self._size, (last + 1) * _BLOCK) - off
        data = self._source.read_at(self._path, off, want, lane=self._lane)
        self.requests += 1
        self.fetched += len(data)
        if len(data) < want:
            raise OSError(f"short ranged read of {self._path} at {off}: "
                          f"{len(data)} of {want} bytes")
        for i in range(first, last + 1):
            s = (i - first) * _BLOCK
            _block_put((*self._ident, i), data[s:s + _BLOCK])

    def _read(self, off: int, n: int) -> bytes:
        first = off // _BLOCK
        last = (off + n - 1) // _BLOCK
        parts: list[bytes] = []
        i = first
        while i <= last:
            blk = _block_get((*self._ident, i))
            if blk is None:
                # Fetch the missing run [i, j] in one request (read ahead
                # inside an announced member window).
                j = i
                end_block = last
                if self._window and self._window[0] <= off < self._window[1]:
                    end_block = max(last, min((self._window[1] - 1) // _BLOCK,
                                              i + _READ_AHEAD // _BLOCK - 1))
                while j < end_block and _block_get((*self._ident, j + 1)) is None:
                    j += 1
                self._fetch_run(i, j)
                blk = _block_get((*self._ident, i))
                if blk is None:               # evicted between put and get
                    blk = self._source.read_at(self._path, i * _BLOCK, _BLOCK,
                                               lane=self._lane)
            parts.append(blk)
            i += 1
        buf = b"".join(parts)
        s = off - first * _BLOCK
        return buf[s:s + n]


def _companion_default(_base_lower: str) -> bool:
    return False


def build_subset(source, zip_rel: str, size: int, mtime: float, member_chain: str,
                 dest: Path, *, lane: str = "stream", scan_root: str = "",
                 companion: Callable[[str], bool] | None = None) -> bool:
    """Write to *dest* a ZIP holding the first member of *member_chain* (its
    raw name, stored) plus its companions, read by ranges from the remote ZIP.
    Returns False (nothing written) when a subset can't stand in for the
    archive: unknown member, an ambiguous display name, or too big a share of
    the archive.  Network / format errors propagate."""
    from soniqboom.core import archive
    companion = companion or _companion_default
    ident = (scan_root, zip_rel, int(size), float(mtime or 0))
    rf = RangeFile(source, zip_rel, size, ident, lane)
    first = member_chain.split("::", 1)[0]
    with zipfile.ZipFile(rf) as zf:
        infos = zf.infolist()
        real = archive._build_map([i.filename for i in infos]).get(first, first)
        info = zf.NameToInfo.get(real)
        if info is None or info.is_dir():
            return False
        needed = [info]
        if "::" not in member_chain:
            # A single-level member: bring what a renderer looks for beside it.
            mdir = str(PurePosixPath(real.replace("\\", "/")).parent)
            mdir = "" if mdir == "." else mdir
            instr = (f"{mdir}/instruments/" if mdir else "instruments/").lower()
            for i in infos:
                if i is info or i.is_dir():
                    continue
                clean = i.filename.replace("\\", "/")
                parent = str(PurePosixPath(clean).parent)
                parent = "" if parent == "." else parent
                base = clean.rsplit("/", 1)[-1].lower()
                if clean.lower().startswith(instr):
                    needed.append(i)               # Sonix Instruments/ folder
                elif parent == mdir and (
                        companion(base)
                        or (archive._display_name(i.filename) is None
                            and i.file_size <= _SIBLING_MAX)):
                    needed.append(i)
                elif (companion(base) and (parent == ""
                                           or mdir.startswith(parent + "/"))):
                    # An AdLib bank kept in a parent folder of the archive
                    # (the extractor takes the closest one that has a bank).
                    needed.append(i)
        # The subset must give the member the SAME display name the track path
        # uses (display names are de-duplicated per archive).
        if archive._build_map([i.filename for i in needed]).get(first, first) != real:
            return False
        if sum(i.compress_size for i in needed) > size * _SUBSET_MAX_SHARE:
            return False
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED, allowZip64=True) as out:
                for i in sorted(needed, key=lambda x: x.header_offset):
                    # Local header (30 B + name + extra) precedes the data;
                    # its extra field may differ from the central one.
                    rf.expect(i.header_offset,
                              i.header_offset + i.compress_size + 30 + 2 * 65536)
                    zi = zipfile.ZipInfo(i.filename, date_time=i.date_time)
                    zi.external_attr = i.external_attr
                    zi.compress_type = zipfile.ZIP_STORED
                    zi.file_size = i.file_size
                    with zf.open(i) as src, out.open(
                            zi, "w", force_zip64=i.file_size >= 0x7FFFFFFF) as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
        except BaseException:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    log.info("Remote archive %s: %d of %d member(s) read by range (%d request(s), "
             "%.1f KB of %.1f MB)", zip_rel, len(needed), len(infos), rf.requests,
             rf.fetched / 1024, size / 1e6)
    return True


def _default_companion(member_chain: str) -> Callable[[str], bool] | None:
    """The companion filter playback uses for *member_chain*."""
    try:
        from soniqboom.api.stream import _archive_companion_filter
    except Exception:                       # pragma: no cover - import-light contexts
        return None
    try:
        return _archive_companion_filter(member_chain)
    except Exception:
        return None


def cached_subset(scan_root: str, zip_rel: str, member_chain: str) -> Path | None:
    """The cached subset holding *member_chain* of ``scan_root:zip_rel``, if
    any — a cache lookup only (no network, no statistics)."""
    from soniqboom.core.remote_cache import get_cache
    return get_cache().find_subset(scan_root, zip_rel, member_chain.split("::", 1)[0],
                                   peek=True)


def archive_for_member(scan_root: str, zip_rel: str, member_chain: str, source, *,
                       lane: str = "stream",
                       companion: Callable[[str], bool] | None = None) -> Path:
    """A LOCAL archive that holds remote member *member_chain* of
    ``scan_root:zip_rel`` — the whole archive when it is cached already (or
    small, or not a ZIP, or its size unknown), else a subset built by ranged
    reads (above): the cached one without touching the share (also with
    ``source=None``, a share that isn't connected), or a new one.
    ``companion`` defaults to the member's own companion filter (the one
    playback passes) so every caller builds and shares the same subset.
    Blocking.  Raises what ``RemoteCache.fetch`` raises — and what the
    archive's ``stat`` raised when the share can't answer it (no whole-archive
    download for that)."""
    from soniqboom.core.remote_cache import get_cache
    cache = get_cache()
    hit = cache.get_cached(scan_root, zip_rel)
    if hit is not None:
        return hit
    is_zip = zip_rel.lower().endswith(".zip")
    if is_zip:
        sub = cache.find_subset(scan_root, zip_rel, member_chain.split("::", 1)[0])
        if sub is not None:
            if source is not None:
                _revalidate_later(cache, scan_root, zip_rel, source)
            return sub
    if source is None:
        raise ConnectionError(f"{scan_root} isn't connected")
    if is_zip and hasattr(source, "read_at"):
        st = _stat(source, zip_rel, lane)       # raises when the share can't answer
        if getattr(st, "is_dir", False):
            raise FileNotFoundError(zip_rel)
        if companion is None:
            companion = _default_companion(member_chain)
        try:
            sub = _subset_for(cache, scan_root, zip_rel, member_chain, source, st,
                              lane=lane, companion=companion)
            if sub is not None:
                return sub
        except FileNotFoundError:
            raise
        except Exception as exc:
            log.info("Ranged read of remote archive %s failed (%s: %s) — "
                     "fetching it whole", zip_rel, type(exc).__name__, exc)
    return cache.fetch(scan_root, zip_rel, source, lane=lane)


def _stat(source, path: str, lane: str):
    """``source.stat`` on the caller's priority lane where the backend has
    lanes (FTP) — a play must not queue behind a scan for a SIZE probe — and
    strict where the backend would otherwise hide a failure behind an empty
    answer (FTP: size 0 read as "too small for a subset" → whole download)."""
    try:
        return source.stat(path, lane=lane, strict=True)
    except TypeError:
        pass
    try:
        return source.stat(path, lane=lane)
    except TypeError:
        return source.stat(path)


# Background re-check of an archive whose cached subset was just served
# without a ``stat``: at most once per archive per ``_REVALIDATE_S``, never
# more than ``_REVALIDATE_MAX`` at a time, on the scan lane.  A changed archive
# (another size / mtime) has its subsets retired (``find_subset`` skips them,
# eviction removes the files — the play that found one may still be using
# it) — the NEXT play builds a new one.
_REVALIDATE_S = 600.0
_REVALIDATE_MAX = 2
_revalidated: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_revalidate_lock = threading.Lock()
_revalidate_slots = threading.BoundedSemaphore(_REVALIDATE_MAX)


def _mark_revalidated(scan_root: str, zip_rel: str) -> None:
    import time
    with _revalidate_lock:
        _revalidated[(scan_root, zip_rel)] = time.monotonic()
        _revalidated.move_to_end((scan_root, zip_rel))
        while len(_revalidated) > 4096:
            _revalidated.popitem(last=False)


def _revalidate_later(cache, scan_root: str, zip_rel: str, source) -> None:
    import time
    key = (scan_root, zip_rel)
    with _revalidate_lock:
        last = _revalidated.get(key)
        if last is not None and time.monotonic() - last < _REVALIDATE_S:
            return
    if not _revalidate_slots.acquire(blocking=False):
        return
    _mark_revalidated(scan_root, zip_rel)

    def _run() -> None:
        try:
            st = _stat(source, zip_rel, "scan")
            size = int(getattr(st, "size", 0) or 0)
            if size > 0 and not getattr(st, "is_dir", False):
                n = cache.drop_stale_subsets(scan_root, zip_rel, size,
                                             float(getattr(st, "mtime", 0) or 0))
                if n:
                    log.info("Remote archive %s changed on the share — %d cached "
                             "subset(s) retired", zip_rel, n)
        except Exception as exc:
            # The share can't answer: keep serving what is cached.
            log.debug("Re-check of remote archive %s failed: %s", zip_rel, exc)
        finally:
            _revalidate_slots.release()

    try:
        threading.Thread(target=_run, name="remote-zip-recheck", daemon=True).start()
    except Exception:
        _revalidate_slots.release()


def _subset_for(cache, scan_root: str, zip_rel: str, member_chain: str, source, st, *,
                lane: str, companion) -> Path | None:
    from soniqboom.core.remote_cache import subset_name
    size = int(getattr(st, "size", 0) or 0)
    if size < _SUBSET_MIN_ARCHIVE:
        return None                     # small — or the server can't tell its size
    mtime = float(getattr(st, "mtime", 0) or 0)
    first = member_chain.split("::", 1)[0]
    key = subset_name(zip_rel, first, size, mtime)
    with _build_locks_guard:
        lock = _build_locks.get((scan_root, key))
        if lock is None:
            lock = _build_locks[(scan_root, key)] = threading.Lock()
    with lock:
        hit = cache.get_cached(scan_root, key)
        if hit is not None:
            cache.unretire(scan_root, key)          # the archive is this version (again)
            return hit
        dest = cache.temp_path(".zip")
        if not build_subset(source, zip_rel, size, mtime, member_chain, dest, lane=lane,
                            scan_root=scan_root, companion=companion):
            return None
        out = cache.put_file(scan_root, key, dest)
    _mark_revalidated(scan_root, zip_rel)       # just stat-ed: no re-check now
    return out
