# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Threads for the blocking file calls a listener's response waits on.

``api.stream`` runs here, never on the event loop nor on the loop's default
executor (which also carries remote scans' transfers, catalogue downloads and
the background passes): the stat, open, Range reads and close of a file a
response serves (whole files included), the sniffs of a source file before
its render, the in-flight transcode's writes, conversion-cache and
remote-cache lookups, and the libgme emulation that feeds a live render.
Multi-range (multipart) answers are still read by Starlette on its own
threads, and some renderer helpers that only touch local temp files run on
the loop.

The pool is sized for listeners, not CPUs (``SONIQBOOM_PLAYBACK_IO_THREADS``).
A mount that stops answering can hold only its share of the threads: jobs on
one mount (``mount_key``: the library folder — or data / cache folder — a
path lies in, as ``set_roots`` registers them) wait for a turn on the loop
beyond ``per_mount_limit`` running at once, and the turn is held until the
job's thread is free again, so the other mounts' plays go on.  Descriptor
closes run on a few threads of their own (``close_detached``): a close stuck
on a dead mount never takes a reader's thread.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from pathlib import PurePosixPath, PureWindowsPath

from soniqboom.core import procinfo as _procinfo

_POOL: "concurrent.futures.ThreadPoolExecutor | None" = None
_CLOSE_POOL: "concurrent.futures.ThreadPoolExecutor | None" = None
_LOCK = threading.Lock()
# Per-mount turns: mount key → [asyncio.Semaphore, its loop].
_GATES: "dict[str, list]" = {}
_GATES_MAX = 1024
# The folders a path's mount is told by (library roots, data and cache dirs):
# a callable returning them, and its last answer (longest first) + its time.
_ROOTS_PROVIDER = None
_ROOTS: "tuple[float, tuple[str, ...]]" = (0.0, ())
_ROOTS_TTL_S = 30.0
_CLOSE_THREADS = 4


def threads() -> int:
    return _procinfo.env_count("SONIQBOOM_PLAYBACK_IO_THREADS",
                               min(128, max(32, 4 * _procinfo.usable_cpus())),
                               minimum=4, maximum=256)


def per_mount_limit() -> int:
    """Jobs one mount may run at once (a quarter of the threads, at least 4)."""
    return max(4, threads() // 4)


def pool() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    p = _POOL
    if p is None:
        with _LOCK:
            if _POOL is None:
                _POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=threads(), thread_name_prefix="sb-playback-io")
            p = _POOL
    return p


def _close_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _CLOSE_POOL
    p = _CLOSE_POOL
    if p is None:
        with _LOCK:
            if _CLOSE_POOL is None:
                _CLOSE_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_CLOSE_THREADS, thread_name_prefix="sb-playback-io-close")
            p = _CLOSE_POOL
    return p


def release() -> None:
    """At the server's stop: drop the jobs still queued and stop the threads
    without waiting for running ones (a read stuck on a share that stopped
    answering).  A later call starts a new pool."""
    global _POOL, _CLOSE_POOL
    with _LOCK:
        p, _POOL = _POOL, None
        cp, _CLOSE_POOL = _CLOSE_POOL, None
        _GATES.clear()
    if p is not None:
        p.shutdown(wait=False, cancel_futures=True)
    if cp is not None:
        cp.shutdown(wait=False)          # (queued closes still run)


def submit(fn, *args) -> concurrent.futures.Future:
    """``fn(*args)`` on the pool, no mount turn (the caller holds one, or the
    job never touches a mount)."""
    try:
        return pool().submit(fn, *args)
    except RuntimeError:                 # stopped just now: a fresh pool
        release()
        return pool().submit(fn, *args)


def set_roots(provider) -> None:
    """``provider()`` → the folders that tell a path's mount: the library's
    scan roots, the data and cache folders (asked at most every
    ``_ROOTS_TTL_S``; never a filesystem call)."""
    global _ROOTS_PROVIDER, _ROOTS
    _ROOTS_PROVIDER = provider
    _ROOTS = (0.0, ())


def _roots() -> "tuple[str, ...]":
    global _ROOTS
    t, roots = _ROOTS
    now = time.monotonic()
    if _ROOTS_PROVIDER is not None and now - t > _ROOTS_TTL_S:
        try:
            got = [os.fspath(r).rstrip("/\\") for r in (_ROOTS_PROVIDER() or ()) if r]
        except Exception:                # noqa: BLE001 — keep the last answer
            got = list(roots)
        roots = tuple(sorted({r for r in got if r and "://" not in r}, key=len, reverse=True))
        _ROOTS = (now, roots)
    return roots


def mount_key(path) -> str:
    """The mount a path most likely lives on: the longest registered root it
    lies in (``set_roots`` — a library folder bind-mounted at ``/music`` is
    one key for all its artists), else its first two components
    (``/Volumes/Music``, ``/mnt/nas``) — never a filesystem call, which is
    what may hang."""
    s = os.fspath(path) if path is not None else ""
    if not s:
        return ""
    for r in _roots():
        if s == r or s.startswith(r + "/") or s.startswith(r + "\\"):
            return r
    cls = PureWindowsPath if (len(s) > 1 and s[1] == ":") else PurePosixPath
    return str(cls(*cls(s).parts[:3]))


def _gate(key: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    g = _GATES.get(key)
    if g is None or g[1] is not loop:
        if len(_GATES) >= _GATES_MAX:
            for k in [k for k, (s, lp) in _GATES.items()
                      if lp is not loop or (not s.locked() and s._value == per_mount_limit())]:
                _GATES.pop(k, None)
        g = [asyncio.Semaphore(per_mount_limit()), loop]
        _GATES[key] = g
    return g[0]


async def turn(key: str):
    """Wait for a turn on mount ``key``; returns its release — pass it to
    ``hold_until_done`` with the job, so the turn is held exactly as long as
    the job occupies a thread (a cancelled caller doesn't free it early)."""
    sem = _gate(key)
    await sem.acquire()
    loop = asyncio.get_running_loop()
    done = [False]

    def _release(_f=None) -> None:
        if done[0]:
            return
        done[0] = True
        try:
            if loop.is_closed():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                sem.release()
            else:
                loop.call_soon_threadsafe(sem.release)
        except RuntimeError:             # loop closed meanwhile
            pass
    return _release


def hold_until_done(fut: concurrent.futures.Future, release) -> concurrent.futures.Future:
    fut.add_done_callback(release)
    return fut


async def run(key: str, fn, *args):
    """Await ``fn(*args)`` on the pool, after a turn on mount ``key``.  A
    caller cancelled while it waits for a turn never starts the job; one
    cancelled while the job runs leaves it running (its result dropped) and
    its turn taken until it ends."""
    release = await turn(key)
    try:
        fut = submit(fn, *args)
    except BaseException:
        release()
        raise
    hold_until_done(fut, release)
    return await asyncio.wrap_future(fut)


def close_quiet(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def close_detached(fd: int) -> None:
    """Close ``fd`` without waiting for it: on the close threads (a close on a
    network mount is a round trip), never awaited — a cancelled request can't
    cancel it, as it cancelled an awaited ``to_thread(os.close)``;
    synchronously once the threads are gone."""
    try:
        _close_pool().submit(close_quiet, fd)
    except Exception:                    # (pool stopped; interpreter exit)
        close_quiet(fd)
