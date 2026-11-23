# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Folder covers of network-share tracks, fetched in the background — one job
per folder, the folders on screen first.

Why (opportunities "C", "D" and "H")
------------------------------------
A network-share track without an embedded cover shows its folder's image.
That used to be looked up INSIDE the cover request: the request listed the
folder and downloaded the image on the playback lane, so

* a row waited for the share (on the internet test rig ~0.65 s per cover,
  ~11 s while a scan held every FTP connection), and
* every row of a folder that was requested at the same moment did the same
  work: 6 rows → 6 listings and 6 downloads of one ``folder.jpg``.

Now the request answers at once — "no cover yet", not to be cached
(``api/art``) — and asks this module for the folder.  One job per folder
(``_Job``) lists it once and downloads the image once; every track that
asked meanwhile is a waiter and gets the cover cached and ``art_ready`` sent
(its list row repaints).  Jobs run on the "browse" lane, behind playback.

Order (D): the newest request is served first — what the user scrolled to
or opened last is on screen now; folders left behind wait.  How many jobs a
share runs at once follows its measured link (``link_speed.limits``): two on
a fast link, four on a slow one.

H: a listing is kept ``_LIST_TTL_S`` — longer than the retry cooldown — so a
job retried after a failed image read, or after the settings changed, does
not list the folder again.

A folder found to hold no image is recorded as such
(``core/folder_art_index``) and its waiters go on to the online lookup (when
that applies).  A job that fails (share unreachable, listing error) records
nothing — the folder's image is unknown, never "none" — and the folder is
not tried again for ``_FAIL_COOLDOWN_S``.  Its tracks count as having no
reachable cover of their own for ``_UNREACHABLE_S`` (``unreachable``), so
the online cover can stand in, as it did before folder images were fetched
in the background; once the share answers, its own image takes over.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

_MAX_PENDING = 2000            # folders waiting; the oldest drop out first
_MAX_WAITERS = 400             # tracks told per job (a list window is ~100)
_MAX_RUNNING = 8               # jobs at once, all shares together
_FAIL_COOLDOWN_S = 60.0         # a failed folder is not asked again for this long
_UNREACHABLE_S = 3600.0         # … and counts as "no reachable image" this long
_LIST_TTL_S = 180.0             # (longer than the cooldown: a retry reuses the listing)
_LIST_MAX = 2000


class _Job:
    __slots__ = ("key", "scan_root", "remote_dir", "dir_hash", "waiters", "gen", "done", "mtime")

    def __init__(self, key, scan_root, remote_dir, dir_hash, gen):
        self.key = key
        self.scan_root = scan_root
        self.remote_dir = remote_dir
        self.dir_hash = dir_hash
        self.waiters: set[str] = set()
        self.gen = gen
        self.done = asyncio.Event()            # set when the job ends (``wait``)
        self.mtime: "float | None" = None       # the image's, from the listing


_pending: "OrderedDict[str, _Job]" = OrderedDict()     # newest last; served from the end
# The same jobs by share (each newest last), and when each share was last
# asked: ``_pump`` looks at a few shares, not at every waiting job — a fast
# scroll queues thousands, and the pump runs on the event loop at every
# request and every job's end.
_by_root: "dict[str, OrderedDict[str, _Job]]" = {}
_asked_at: "dict[str, float]" = {}
_running: "dict[str, _Job]" = {}
_running_by_root: "dict[str, int]" = {}
_failed_at: "dict[str, float]" = {}
_busy_at: "dict[str, float]" = {}               # folders whose job found the pool busy
_tasks: "set[asyncio.Task]" = set()
_listings: "dict[tuple[str, str], tuple[float, list]]" = {}
_list_lock = threading.Lock()


def forget_folder(key: str) -> None:
    """Folder ``key``'s image changed (``api/art.refresh_folder``): forget
    its kept listing and its failure / busy marks — a listing from before
    the change would name an image that is gone, or an old date."""
    from soniqboom.core import folder_art_index as fai
    _failed_at.pop(key, None)
    _busy_at.pop(key, None)
    with _list_lock:
        for k in [k for k in _listings if fai.key_for_remote_dir(k[0], k[1]) == key]:
            _listings.pop(k, None)


def _key_of(track) -> "tuple[str, str, str] | None":
    """``(folder key, share root, folder in the share)`` of a network-share
    track, or None."""
    from soniqboom.core import folder_art_index as fai
    from soniqboom.core.filesource import is_remote_path, parse_remote_path
    path = getattr(track, "path", "") or ""
    if not is_remote_path(path):
        return None
    try:
        scan_root, remote_path = parse_remote_path(path)
    except Exception:                              # noqa: BLE001
        return None
    if not remote_path:
        return None
    remote_dir = fai.remote_dir_of(remote_path)
    return fai.key_for_remote_dir(scan_root, remote_dir), scan_root, remote_dir


def unreachable(track) -> bool:
    """Did the last job for ``track``'s folder fail (share unreachable, a
    listing error) within ``_UNREACHABLE_S``?"""
    k = _key_of(track)
    if k is None:
        return False
    failed = _failed_at.get(k[0])
    return failed is not None and time.monotonic() - failed < _UNREACHABLE_S


def pending(track) -> bool:
    """Is the folder cover of ``track`` queued or being fetched?"""
    k = _key_of(track)
    return k is not None and (k[0] in _pending or k[0] in _running)


def request(track) -> bool:
    """Ask for the folder cover of network-share ``track`` (call on the event
    loop).  True when a job for its folder is now queued or running —
    ``art_ready`` follows for the track when a cover is found.  False: not
    a network-share track, an online collection's (those folders hold no
    art we may fetch), or its folder failed a moment ago."""
    from soniqboom.core import catalogue
    from soniqboom.core import folder_art_index as fai
    tid = getattr(track, "id", None)
    k = _key_of(track)
    if not tid or k is None or catalogue.is_catalogue_path(track.path):
        return False
    key, scan_root, remote_dir = k
    job = _running.get(key) or _pending.get(key)
    if job is None:
        now = time.monotonic()
        for at in (_failed_at.get(key), _busy_at.get(key)):
            if at is not None and now - at < _FAIL_COOLDOWN_S:
                return False
        job = _pending[key] = _Job(key, scan_root, remote_dir,
                                   getattr(track, "dir_hash", None) or None, fai.generation())
        _by_root.setdefault(scan_root, OrderedDict())[key] = job
        while len(_pending) > _MAX_PENDING:
            old_key, old = _pending.popitem(last=False)  # the oldest request
            q = _by_root.get(old.scan_root)
            if q is not None:
                q.pop(old_key, None)
    elif key in _pending:
        _pending.move_to_end(key)                       # asked again: on screen now
        q = _by_root.get(scan_root)
        if q is not None and key in q:
            q.move_to_end(key)
    _asked_at[scan_root] = time.monotonic()
    if len(job.waiters) < _MAX_WAITERS:
        job.waiters.add(tid)
    _pump()
    return True


def _pump() -> None:
    """Start the newest pending jobs the shares have room for — the share
    asked most recently first, within it the newest job first."""
    from soniqboom.core import link_speed
    from soniqboom.core.filesource import get_source
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if not _pending or len(_running) >= _MAX_RUNNING:
        return
    for root in sorted(_by_root, key=lambda r: _asked_at.get(r, 0.0), reverse=True):
        q = _by_root.get(root)
        if not q:
            _by_root.pop(root, None)
            continue
        source = get_source(root)
        width = link_speed.limits(source).folder_jobs if source is not None else 1
        while q and _running_by_root.get(root, 0) < width and len(_running) < _MAX_RUNNING:
            key, job = q.popitem(last=True)
            _pending.pop(key, None)
            _running[key] = job
            _running_by_root[root] = _running_by_root.get(root, 0) + 1
            t = loop.create_task(_run(job, source))
            _tasks.add(t)
            t.add_done_callback(_done)
        if not q:
            _by_root.pop(root, None)
        if len(_running) >= _MAX_RUNNING:
            break


def _done(t: "asyncio.Task") -> None:
    _tasks.discard(t)
    if not t.cancelled() and t.exception() is not None:
        log.debug("folder cover job failed: %s", t.exception())


def _listing(source, scan_root: str, remote_dir: str) -> list:
    """The folder's entries — kept ``_LIST_TTL_S`` (H).  Raises when the
    listing fails (never kept)."""
    now = time.monotonic()
    k = (scan_root, remote_dir)
    with _list_lock:
        hit = _listings.get(k)
        if hit is not None and now - hit[0] < _LIST_TTL_S:
            return hit[1]
    entries = list(source.list_dir(remote_dir))
    with _list_lock:
        if len(_listings) >= _LIST_MAX:
            _listings.clear()
        _listings[k] = (time.monotonic(), entries)     # (stamped when the listing is in)
    return entries


def _fetch(source, job: _Job) -> "tuple[str, bytes | None, str | None]":
    """In a worker thread: ``("image", bytes, name)``, ``("none", None,
    None)`` after a complete listing without one of the configured names,
    or raises."""
    from soniqboom.core import folder_art_index as fai
    entries = _listing(source, job.scan_root, job.remote_dir)
    files = {e.name: e for e in entries if not getattr(e, "is_dir", False)}
    by_lower: dict = {}
    for n in files:
        by_lower.setdefault(n.lower(), n)
    names = [by_lower[p] for p in fai.folder_image_names() if p in by_lower]
    if not names:
        return "none", None, None
    last: "Exception | None" = None
    for name in names:                     # an unreadable or empty image: the next name
        try:
            data = source.read_file(files[name].path, lane="browse")
        except Exception as exc:           # noqa: BLE001 — tried in turn, the last raised below
            last = exc
            continue
        if data:
            job.mtime = getattr(files[name], "mtime", None) or None
            return "image", data, name
        last = OSError(f"empty folder image {name!r}")
    raise last or OSError("no folder image could be read")


async def _run(job: _Job, source) -> None:
    from soniqboom.core import folder_art_index as fai
    try:
        if source is None:
            raise OSError("share not connected")
        status, data, name = await asyncio.to_thread(_fetch, source, job)
    except Exception as exc:                       # noqa: BLE001 — recorded as "unknown", never "none"
        log.debug("folder cover of %s%s not fetched: %s", job.scan_root, job.remote_dir, exc)
        if isinstance(exc, TimeoutError) and "acquire timed out" in str(exc):
            # The share answered but every connection was busy: not
            # "unreachable" (no online cover stands in) — asked again on the
            # next request after the cooldown.
            _busy_at[job.key] = time.monotonic()
            _finish(job)
            return
        now = time.monotonic()
        if len(_failed_at) > _MAX_PENDING:
            for k in [k for k, t in _failed_at.items() if now - t >= _UNREACHABLE_S]:
                del _failed_at[k]
            if len(_failed_at) > _MAX_PENDING:
                _failed_at.clear()
        _failed_at[job.key] = now
        try:
            await _no_cover(job)                # (``unreachable``: the online cover may stand in)
        finally:
            _finish(job)
        return
    _failed_at.pop(job.key, None)              # the share answered
    _busy_at.pop(job.key, None)
    try:
        if status == "image":
            await _cover_found(job, data, name)
        else:
            fai.record(job.key, None, dir_hash=job.dir_hash, gen=job.gen)
            await _no_cover(job)
    finally:
        _finish(job)


def _finish(job: _Job) -> None:
    if _running.pop(job.key, None) is not None:
        n = _running_by_root.get(job.scan_root, 1) - 1
        if n > 0:
            _running_by_root[job.scan_root] = n
        else:
            _running_by_root.pop(job.scan_root, None)
    job.done.set()
    _pump()
    # (a folder this job found changed — another image, or none any more —
    # has its old copies dropped and its tracks told: ``announce_folder_changes``)
    try:
        from soniqboom.api import art as art_api
        t = asyncio.get_running_loop().create_task(art_api.announce_folder_changes())
        _tasks.add(t)
        t.add_done_callback(_done)
    except RuntimeError:
        pass


async def wait(track, timeout: float) -> None:
    """Wait up to ``timeout`` s for the job of ``track``'s folder to end (no
    job: at once).  For the callers that cannot be told later — Subsonic,
    DLNA, cast get no ``art_ready``, so they wait a moment for the folder's
    image instead of answering "no cover" for it."""
    k = _key_of(track)
    job = None if k is None else (_running.get(k[0]) or _pending.get(k[0]))
    if job is None:
        return
    try:
        await asyncio.wait_for(job.done.wait(), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        pass


async def _cover_found(job: _Job, data: bytes, name: str) -> None:
    """Cache the folder's image (once, for every track of the folder) and the
    cover of each waiting track, which sends its ``art_ready``."""
    from soniqboom.api import art as art_api
    from soniqboom.core import art_cache
    from soniqboom.core import folder_art_index as fai
    from soniqboom.core.metadata import cap_full_cover
    if job.gen != fai.generation() or not art_api._use_folder_art_now():
        return                  # a cover setting changed (or the art cache was cleared) meanwhile
    shared = f"{art_api._FOLDER_ART_KEY_PREFIX}{job.key}"
    fai.record(job.key, name, mtime=job.mtime, dir_hash=job.dir_hash, gen=job.gen)
    changed = fai.take_change(job.key)
    old = None
    if changed is not None:
        # This job found the image replaced: the copies of the OLD image are
        # dropped now, with it in hand — before the new one takes its place
        # and before the waiters get their copies of the new one.
        for cid in (shared, art_api._folder_art_cache_key(changed)):
            old = old or await art_cache.get_art(cid, "full")
        await art_cache.delete_art_batch([shared, art_api._folder_art_cache_key(changed)])
        await art_api.refresh_folder(job.key, changed, old=old or b"")
    capped = await asyncio.to_thread(cap_full_cover, data)
    if job.gen != fai.generation():
        return
    # (the shared cache id of a share folder is its index key — share root
    # included; ``api/art._folder_cache_key``)
    await art_cache.store_art(shared, capped, "full")
    for tid in list(job.waiters):
        art_api._clear_art_absent_persisted(tid)
        art_api._persist_and_notify(tid, capped, from_folder=True)


async def _no_cover(job: _Job) -> None:
    """The folder holds no image (or could not be read): its waiting tracks
    have no cover of their own to show — ask the online lookup for each
    (when it applies; ``art_ready`` follows a cover it finds)."""
    from soniqboom.api import art as art_api
    for tid in list(job.waiters):
        try:
            await art_api.ask_online_for(tid)
        except Exception:                          # noqa: BLE001
            log.debug("online cover request failed for %s", tid, exc_info=True)


def _reset_for_tests() -> None:
    _pending.clear()
    _by_root.clear()
    _asked_at.clear()
    _running.clear()
    _running_by_root.clear()
    _failed_at.clear()
    _busy_at.clear()
    with _list_lock:
        _listings.clear()
