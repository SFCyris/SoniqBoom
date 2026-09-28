# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Async directory scanner — discovers audio files, extracts metadata, upserts to store.

Design
──────
Extract metadata in parallel using a **ProcessPoolExecutor** so each worker
gets its own GIL and cannot block the main event loop.  Results are written
to the in-memory store in small chunked batches with ``asyncio.sleep(0)``
yields between chunks, keeping the API and WebSocket fully responsive during
even large scans.

Waveforms are generated on-demand (lazy) when first requested via the
tracks API, not during scanning.

Non-blocking notes
──────────────────
• Metadata extraction runs in **separate processes** (ProcessPoolExecutor)
  — ZIP decompression and mutagen parsing never compete with the event loop.
• Store writes go through upsert_tracks_batch() in sub-batches of
  WRITE_CHUNK tracks, with asyncio.sleep(0) between each chunk.
• The processing loop yields to the event loop every YIELD_EVERY results.
• A local hash cache avoids re-computing deterministic dir/root hashes.
"""
from __future__ import annotations

import asyncio
import base64
import errno
import heapq
import itertools
import json
import logging
import math
import os
import re
import stat
import sys
import weakref
import subprocess
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from concurrent.futures import (
    BrokenExecutor, ProcessPoolExecutor, ThreadPoolExecutor,
)
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Iterable

from soniqboom.core.metadata import (
    SUPPORTED_EXTENSIONS, extract, is_supported_music_name,
)


def prepare_worker_forkserver() -> None:
    """macOS: start the multiprocessing forkserver the scan pools fork their
    workers from (``_process_pool``) — at start-up, before the server uses
    the network.  It preloads only the ``soniqboom`` package (version
    strings — no settings), so every worker imports the app itself and reads
    the settings file as it is now, as a spawned worker does, and
    ``soniqboom._forkserver_guard`` (a client that sends nothing no longer
    kills it); a non-empty preload also puts the app's ``sys_path`` and
    ``'soniqboom'`` on the forkserver's command line, which
    ``main._reap_orphaned_forkservers`` matches.  A later start in the same
    process (the bundled app's Stop/Start) finds the forkserver a stop kept
    (``shutdown_worker_pools``) still running — ``ensure_running`` then
    starts nothing — and should it have died, it is started again without a
    fork (``_spawnv_passfds_nofork``).  A no-op outside macOS."""
    if sys.platform != "darwin":
        return
    import multiprocessing
    from multiprocessing import forkserver
    _install_nofork_spawn()
    try:
        multiprocessing.set_forkserver_preload(["soniqboom", "soniqboom._forkserver_guard"])
        forkserver.ensure_running()
    except Exception:                                       # noqa: BLE001
        log.warning("could not start the worker forkserver now — it starts "
                    "with the first scan", exc_info=True)


def _spawnv_passfds_nofork(path, args, passfds):
    """``multiprocessing.util.spawnv_passfds`` without a fork (macOS): the
    stdlib's forks the server before it execs, and once the server has used
    the network Network.framework's fork handler can crash that child — so
    starting the worker forkserver (or the resource tracker) again after one
    died must not fork.  ``posix_spawn`` passes every inheritable
    descriptor, so every other one is swept to close-on-exec first and
    ``passfds`` are made inheritable for the call (all under
    ``forksafe.spawn_lock``) and back again after it."""
    from soniqboom.core import forksafe
    fds = sorted({int(fd) for fd in passfds})
    made: list[int] = []
    with _spawn_lock:
        forksafe._cloexec_locked()              # nothing else passes to the forkserver
        try:
            for fd in fds:
                if not os.get_inheritable(fd):
                    os.set_inheritable(fd, True)
                    made.append(fd)
            return os.posix_spawn(path, list(args), dict(os.environ))
        finally:
            for fd in made:
                try:
                    os.set_inheritable(fd, False)
                except OSError:
                    pass


from soniqboom.core.forksafe import spawn_lock as _spawn_lock   # shared with forksafe._cloexec_all


def _install_nofork_spawn() -> None:
    from multiprocessing import util
    if hasattr(os, "posix_spawn") and util.spawnv_passfds is not _spawnv_passfds_nofork:
        util.spawnv_passfds = _spawnv_passfds_nofork


def _worker_init() -> None:
    """In each scan worker as it starts: its own process group, so
    ``_kill_pool`` kills a decoder it started (uade123, openmpt123 …) with
    it; and no descriptor above stderr left inheritable — the forkserver's
    "alive" pipe and the pool's pipes would otherwise pass to every decoder
    the worker starts, and one that outlives a killed worker would keep the
    forkserver from ever exiting."""
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    try:
        fds = [int(n) for n in os.listdir("/dev/fd")]
    except (OSError, ValueError):
        fds = []
    for fd in fds:
        if fd > 2:
            try:
                os.set_inheritable(fd, False)
            except OSError:
                pass


# Seconds with no extraction finishing before the workers count as hung.
_EXTRACT_STUCK_S = 90
# Once a file hung its worker alone, the source is probed (``_source_answers``,
# ``_PROBE_S`` a try) again with growing pauses (from ``_STALL_PAUSE_S``) for up
# to ``_STALL_GRACE_S`` — a share waking up or reconnecting — before the root is
# given up; while it answers, only the hung file is.  Files in a row that fail
# with a source error before the source is probed.
_PROBE_S = 10
_STALL_PAUSE_S = 5.0
_STALL_GRACE_S = 180.0
_MAX_SOURCE_ERRORS = 20
# Files ahead in the queue looked at for one the probe hasn't read yet.
_PROBE_LOOKAHEAD = 32
# Members of one file on disk in a row that hung alone (nothing extracted in
# between) before the rest of the deepest archive they all lie in is skipped
# (``_common_archive``): they share the cause — a nested archive re-read for
# every member, a share that answers from its cache — and each would cost
# another 90 s.
_MAX_ARCHIVE_HANGS = 5


def _common_archive(paths) -> str:
    """The deepest archive all of ``paths`` (members of one file on disk) lie
    in: ``a.zip::b.zip::x``, ``a.zip::b.zip::y`` → ``a.zip::b.zip``;
    ``a.zip::b.zip::x``, ``a.zip::c.zip::y`` → ``a.zip``."""
    chains = [str(p).split("::")[:-1] for p in paths]
    common: list[str] = []
    for parts in zip(*chains):
        if any(x != parts[0] for x in parts):
            break
        common.append(parts[0])
    return "::".join(common)


def _in_archives(path, archives) -> "str | None":
    """The one of ``archives`` that ``path`` lies in (at any depth), or None."""
    s = str(path)
    for a in archives:
        if s.startswith(a + "::"):
            return a
    return None
_SOURCE_ERRNOS = frozenset(getattr(errno, n) for n in (
    "EIO", "ENXIO", "ETIMEDOUT", "ENOTCONN", "ESTALE", "EHOSTDOWN", "EHOSTUNREACH",
    "ENETDOWN", "ENETUNREACH", "ECONNRESET", "ECONNABORTED") if hasattr(errno, n))
_ERRNO_RE = re.compile(r"\[Errno (\d+)\]")


def _is_source_error(msg: str) -> bool:
    """An extraction error text (``_extract_one``) that says the SOURCE
    failed — the share, not the file: an I/O error, a timeout, a lost
    connection."""
    m = _ERRNO_RE.search(msg or "")
    return (m is not None and int(m.group(1)) in _SOURCE_ERRNOS) or (msg or "").startswith("TimeoutError")


def _read_uncached(path: str) -> None:
    """64 KB from the middle of ``path``, past the file system cache where
    the platform allows it (macOS: ``F_NOCACHE``; elsewhere the range is
    dropped from the page cache first) — a file not read before, so a share
    that stopped answering can't answer from what it cached.  Only a SOURCE
    error (``_SOURCE_ERRNOS``: I/O, a timeout, a lost connection …) is
    raised: any other answer — the file gone, a permission, not a regular
    file (opened without blocking: a FIFO would wait for a writer) — is an
    answer of the file system."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        if exc.errno in _SOURCE_ERRNOS:
            raise
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return
        os.set_blocking(fd, True)
        off = max(0, st.st_size // 2 - 32768) & ~0xFFF
        try:
            if sys.platform == "darwin":
                import fcntl
                fcntl.fcntl(fd, getattr(fcntl, "F_NOCACHE", 48), 1)
            elif hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, off, 65536, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass                                # not supported here: a plain read
        os.pread(fd, 65536, off)
    except OSError as exc:
        if exc.errno in _SOURCE_ERRNOS:
            raise
    finally:
        os.close(fd)


def _source_answers(path, timeout: float = _PROBE_S, listing=None, fresh=None) -> bool:
    """Whether the file system holding ``path`` still answers: ``listing``'s
    first entry listed (when given — a folder that is gone doesn't answer),
    ``path``'s first 64 KB read and ``fresh``'s read past the cache
    (``_read_uncached``; each when given — an archive member's: its
    archive's; a file that is gone answers, it was deleted meanwhile),
    within ``timeout`` seconds together.  The reads run in a daemon thread —
    on a share that stopped answering they can block for good.  Any other
    error doesn't answer."""
    real = None if path is None else str(path).split("::", 1)[0]
    unread = None if fresh is None else str(fresh).split("::", 1)[0]
    done = threading.Event()
    ok: list[bool] = []

    def _read() -> None:
        try:
            if listing is not None:
                with os.scandir(listing) as it:
                    next(it, None)
            if real is not None:
                try:
                    with open(real, "rb") as fh:
                        fh.read(65536)
                except FileNotFoundError:
                    pass
            if unread is not None:
                _read_uncached(unread)
            ok.append(True)
        except OSError:
            ok.append(False)
        finally:
            done.set()
    threading.Thread(target=_read, daemon=True, name="scan-source-probe").start()
    return done.wait(timeout) and bool(ok) and ok[0]


# A scan pool being (or that was) killed → the daemon thread killing it
# (``_kill_pool_bg``).  A pool in here is never touched again: its manager
# thread may hold the lock ``submit`` and ``shutdown`` take for good — it
# joins the killed workers, and one stuck in an uninterruptible read on a
# hung share never exits.
_pool_killers: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_pool_killers_lock = threading.Lock()


def _kill_pool_bg(executor) -> threading.Thread:
    """``_kill_pool`` in a daemon thread, never the caller's (it may wait on
    the pool's lock for good; the thread never holds up the process's
    exit).  One per pool: a pool already being killed returns that
    thread."""
    with _pool_killers_lock:
        t = _pool_killers.get(executor)
        if t is None:
            t = threading.Thread(target=_kill_pool, args=(executor,), daemon=True,
                                 name="scan-pool-kill")
            _pool_killers[executor] = t
            t.start()
    return t


def _pool_stopped(executor) -> bool:
    """The pool was killed (``_kill_pool_bg``), broke or was shut down —
    read without its lock (``submit`` waits on it)."""
    return (executor in _pool_killers or bool(getattr(executor, "_broken", False))
            or bool(getattr(executor, "_shutdown_thread", False)))


def _release_pool(executor) -> None:
    """A scan pool whose work is done: shut down (its workers exit once
    idle) in a daemon thread — the caller is the event loop, and a pool a
    stop killed meanwhile may hold its lock for good.  A stopped pool is
    left alone."""
    if executor is None or _pool_stopped(executor):
        return
    threading.Thread(target=lambda: executor.shutdown(wait=False), daemon=True,
                     name="scan-pool-release").start()


def _unread_file(requeue: deque, file_iter, avoid=()) -> Path | None:
    """A file a root's scan hasn't read yet, for the source probe
    (``_source_answers``' ``fresh``): the first of the next ones to extract
    — ``requeue``, then ``file_iter`` (what is taken from it goes back, in
    order, at the end of ``requeue``, which runs first; up to
    ``_PROBE_LOOKAHEAD`` looked at) — whose file isn't one of ``avoid``'s
    (an archive member's file: its archive), so what the probe reads of it
    can't come from a cache.  None when there is none."""
    skip = {str(p).split("::", 1)[0] for p in avoid if p is not None}
    for p in list(itertools.islice(requeue, _PROBE_LOOKAHEAD)):
        if str(p).split("::", 1)[0] not in skip:
            return p
    for _ in range(max(0, _PROBE_LOOKAHEAD - len(requeue))):
        p = next(file_iter, None)
        if p is None:
            break
        requeue.append(p)
        if str(p).split("::", 1)[0] not in skip:
            return p
    return None


async def _kill_pool_async(executor, timeout: float = 10.0) -> None:
    """``_kill_pool_bg``, waited for up to ``timeout`` so the pool is gone
    before what comes next (a probe of the source, a new pool) — the scan
    goes on either way.  A pool already being killed isn't waited for
    again: one wait per pool."""
    if executor is None or executor in _pool_killers:
        return
    t = _kill_pool_bg(executor)
    deadline = time.monotonic() + timeout
    step = 0.005
    while t.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(step)
        step = min(step * 2, 0.1)
    if t.is_alive():
        log.warning("a scan pool is still shutting down (a worker can't exit)")


def _pool_canary() -> bool:
    """A no-op job: a new pool that runs it can start workers at all."""
    return True


def _ours(pid) -> bool:
    """``pid`` is a live scan worker of ours (a forkserver child running the
    app) — checked by its command line, never by the pid alone (it may have
    been reused)."""
    from soniqboom.core import procinfo
    return isinstance(pid, int) and pid > 0 and procinfo.is_our_forkserver_child(pid)


def _kill_worker(proc) -> None:
    """Kill one worker — with its process group (``_worker_init``) when it
    is verifiably ours and leads it, so what it started dies too."""
    import signal
    pid = getattr(proc, "pid", None)
    if sys.platform == "darwin":
        ours = _ours(pid)
    else:
        try:
            ours = bool(proc.is_alive())        # our child, not reaped: the pid is still its
        except Exception:                                   # noqa: BLE001
            ours = False
    if ours and isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except Exception:                                       # noqa: BLE001
        pass
    if sys.platform == "darwin" and _ours(pid):
        # ``proc.kill`` does nothing once the pool thinks the worker is gone
        # (its forkserver died); the process may still run.
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _kill_pool(executor) -> None:
    """Kill a dead or hung pool's workers, then shut it down.  Killing first
    lets the pool's manager thread — which may be waiting for a worker to
    exit, holding the lock ``shutdown`` takes — go on; the process dict is
    read again after the shutdown (which refuses new submits), so a worker a
    racing submit had just started is killed too (``shutdown`` drops the
    pool's reference to the dict, not the dict).  It may still wait on that
    lock for good (a worker that can't exit): the server calls it through
    ``_kill_pool_bg``."""
    procs = getattr(executor, "_processes", None)
    done: set[int] = set()

    def _kill_all() -> None:
        for proc in list((procs or {}).values()):
            if id(proc) not in done:
                done.add(id(proc))
                _kill_worker(proc)
    _kill_all()
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:                                       # noqa: BLE001
        pass
    _kill_all()


# Every scan pool alive → the server run (``begin_run`` epoch) that made it,
# so a stop can end its run's pools (``shutdown_worker_pools``).
_live_pools: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_run_epoch = 0
# Set by a stop: no new scan pool (a scan still running would replace the
# pool the stop just killed while the server exits).  ``begin_run`` clears
# it.  ``_pools_lock`` makes "check the flag + register the pool" and "set
# the flag + list the pools" atomic, so no pool is made after a stop listed
# them.
_pools_closed = False
_pools_lock = threading.Lock()


def begin_run() -> int:
    """A server run starts (``main.startup``): a new run epoch; pools a
    previous run in this process left are killed; the scan state it left
    reset (the bundled app stops and starts the server in one process — the
    old event loop, and every scan task, pause event and queued scan bound
    to it, are gone); scans may make pools again; the worker forkserver made
    ready.  Returns the epoch: ``shutdown_worker_pools(epoch)`` acts on this
    run alone, even if a previous run's stop ends after this start.  The
    stale pools are killed in the background (``_kill_pool_bg``) — one whose
    worker can't exit would hold this start up for good."""
    global _run_epoch, _pools_closed
    with _pools_lock:
        _run_epoch += 1
        epoch = _run_epoch
        _pools_closed = False
        stale = [pool for pool, e in list(_live_pools.items()) if e < epoch]
    for pool in stale:
        _kill_pool_bg(pool)
    _reset_scan_state()
    prepare_worker_forkserver()
    return epoch


def shutdown_worker_pools(epoch: int | None = None, timeout: float = 2.5) -> bool:
    """At a stop: no new scan pools from now on, and every scan pool's
    workers killed — a stop during a scan otherwise leaves them running.  A
    scan still running ends its root (the files left count as not indexed).
    With ``epoch`` (``begin_run``), only that run's pools — and pools stay
    open if a newer run has started meanwhile.  The pools are killed in
    daemon threads (``_kill_pool_bg``), waited for up to ``timeout`` seconds
    together: False when one is still ending then (a worker that can't
    exit — the stop goes on without it).

    The forkserver is KEPT: it exits by itself with the process (its
    "alive" pipe closes once the server and every process it forked are
    gone — the pools killed here, the merger stopped before), and a start
    in the same process (the bundled app's Stop/Start/Restart) reuses it.
    Stopping it would make that start fork a new one from a server that
    has used the network, which can crash in Network.framework's fork
    handler — every pool of every later scan with it."""
    global _pools_closed
    with _pools_lock:
        if epoch is None or epoch == _run_epoch:
            _pools_closed = True
        pools = [pool for pool, e in list(_live_pools.items()) if epoch is None or e <= epoch]
    threads = [_kill_pool_bg(pool) for pool in pools]
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    left = sum(t.is_alive() for t in threads)
    if left:
        log.warning("%d scan pool(s) still shutting down (a worker can't exit)", left)
    return not left


def _process_pool(max_workers: int) -> ProcessPoolExecutor:
    """A process pool for scan work.  On macOS its workers fork from the
    multiprocessing forkserver (``prepare_worker_forkserver``) — a small
    single-threaded process that never used the network — not from the
    server: ``spawn`` forks the server before it execs, and once the server
    has used the network Network.framework's fork handler
    (``nw_settings_child_has_forked``) can crash every new worker, which made
    scans index nothing after a TOSEC / Redump download (crash reports:
    "crashed on child side of fork pre-exec").  Elsewhere the platform's
    default start method, as before.  Each worker runs ``_worker_init``.
    Raises ``RuntimeError`` once the server is stopping
    (``shutdown_worker_pools``)."""
    with _pools_lock:
        if _pools_closed:
            raise RuntimeError("the server is stopping — no new scan workers")
        pool = None
        if sys.platform == "darwin":
            import multiprocessing
            try:
                pool = ProcessPoolExecutor(max_workers=max_workers,
                                           mp_context=multiprocessing.get_context("forkserver"),
                                           initializer=_worker_init)
            except (ValueError, RuntimeError):
                pool = None
        if pool is None:
            pool = ProcessPoolExecutor(max_workers=max_workers, initializer=_worker_init)
        _live_pools[pool] = _run_epoch
    return pool


from soniqboom.core import diskimage
from soniqboom.core import archive
from soniqboom.core.art_cache import store_full_art_batch, store_thumbs_batch
from soniqboom.core.data import (
    delete_track_ids,
    get_track_ids_for_scan_root,
    get_tracks_batch,
    path_hash,
    scan_all_tracks_meta,
    store_hash_lookups_batch,
    upsert_scan_dir,
    upsert_tracks_batch,
)
from soniqboom.core.store import get_store
from soniqboom.models.track import Track, TrackMeta

log = logging.getLogger(__name__)

# ── Tuning knobs ──────────────────────────────────────────────────────────────
# Default to half the available cores (rounded up, min 2, max 16) — a fixed 8
# over-subscribed 2-core Macs and under-utilised 10+-core M-series machines.
def _default_scan_workers() -> int:
    cores = os.cpu_count() or 4
    return max(2, min(16, (cores + 1) // 2))

SCAN_WORKERS   = _default_scan_workers()
WRITE_BATCH    = 500     # tracks buffered before a store flush
WRITE_CHUNK    = 25      # sub-batch size within a flush (yield between chunks)
INFLIGHT       = 200     # max futures in the asyncio.wait window (keeps wait() O(200) not O(n))
PROGRESS_EVERY = 100     # broadcast WS update every N files

# Formats where ffmpeg can't directly decode the source file.
# Waveforms for these are computed from the conversion cache WAV instead
# (see tracks.py waveform endpoint).
_SKIP_WAVEFORM_EXTS = {
    ".sid", ".psid",
    ".mid", ".midi",
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ahx", ".hvl", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}


# ── Progress state ────────────────────────────────────────────────────────────

@dataclass
class ScanProgress:
    total:        int   = 0
    processed:    int   = 0
    errors:       int   = 0
    running:      bool  = False
    embedding:    bool  = False
    current_file: str   = ""
    started_at:   float = field(default_factory=time.time)
    # Last-scan summary — populated on completion of a remote scan so
    # the UI can show "skipped 16027, refreshed 0, deleted 1928"
    # instead of bare "Scan complete".  Each completion overwrites,
    # so this reflects the most recent scan.  Empty dict pre-first-
    # scan.
    last_plan:    dict = field(default_factory=dict)
    # Resolved absolute paths covered by the most-recent scan.  Unlike
    # ``current_dirs`` (cleared the instant a scan finishes), this SURVIVES
    # completion so the ``running:false`` broadcast tells the frontend which
    # folder(s) just finished — letting it refresh the open folder IN PLACE
    # instead of resetting the whole tree.  Set at scan start.
    last_dirs:    list = field(default_factory=list)

    def pct(self) -> int:
        return min(100, int(self.processed / self.total * 100)) if self.total else 0

    def to_dict(self) -> dict:
        d = {
            "total":        self.total,
            "processed":    self.processed,
            "errors":       self.errors,
            "running":      self.running,
            "embedding":    self.embedding,
            "pct":          self.pct(),
            "current_file": self.current_file,
            "paused":       is_scan_paused(),
            "last_plan":    dict(self.last_plan),
            "last_dirs":    list(self.last_dirs),
        }
        # Append queue info (combine local + remote active dirs)
        all_active = set(_current_scan_dirs) | _current_remote_dirs
        d["current_dirs"] = sorted(all_active) if all_active else []
        d["queued"] = [sorted(q_dirs) for q_dirs, *_ in _scan_queue]
        d["queue_depth"] = len(_scan_queue)
        return d


_progress  = ScanProgress()
_scan_count: int = 0            # number of active scans (local + remote)
_scan_task: asyncio.Task | None = None

# Folder-art prefetch tasks detach from their parent scan (see comment
# in start_remote_scan).  asyncio holds only a WEAK ref to tasks created
# with create_task — without a strong ref here they could be garbage-
# collected mid-flight.  The done_callback discards on completion.
_art_prefetch_tasks: set[asyncio.Task] = set()


def _art_prefetch_done(task: asyncio.Task) -> None:
    """Log + discard finished folder-art prefetch tasks.

    Runs synchronously from the asyncio loop's done-callback dispatch
    (not a coroutine, no await).  Keep it cheap.

    Task name is set to ``scan.art_prefetch[{scan_root}]`` at create
    time so we can recover the scan_root for context in log lines
    without keeping a closure ref to it (which would chain into the
    task's locals and complicate cleanup).
    """
    _art_prefetch_tasks.discard(task)
    name = task.get_name()
    scan_root = "?"
    if name.startswith("scan.art_prefetch[") and name.endswith("]"):
        scan_root = name[len("scan.art_prefetch["):-1]
    if task.cancelled():
        log.info("Folder-art prefetch for %s cancelled", scan_root)
        return
    exc = task.exception()
    if exc is not None:
        log.warning("Folder-art prefetch for %s raised: %s", scan_root, exc)
        return
    try:
        stats = task.result() or {}
    except Exception as exc:  # pragma: no cover — already handled above
        log.warning(
            "Folder-art prefetch for %s result raise: %s", scan_root, exc,
        )
        return
    if stats.get("unique_dirs"):
        log.info(
            "Folder-art prefetch for %s: %d dirs (warmed=%d, cached=%d, "
            "no_art=%d, errors=%d)",
            scan_root,
            stats.get("unique_dirs", 0),
            stats.get("warmed", 0),
            stats.get("skipped_cached", 0),
            stats.get("no_art", 0),
            stats.get("errors", 0),
        )


# ── Pause / resume ──────────────────────────────────────────────────────────
#
# Scanner pause is a soft co-operative gate: workers check the event
# between files and ``await``-wait if it's cleared.  In-flight
# downloads / extractions complete normally; new ones don't start
# until resume.  Implemented as an asyncio.Event that defaults to
# *set* (= NOT paused, work proceeds).  ``pause_scan`` clears it,
# ``resume_scan`` sets it.
#
# Lazy event-loop binding: the Event needs to be attached to the
# running event loop at first use, not module-import time (which may
# happen before uvicorn creates its loop).  We construct on first
# access in ``_pause_event()``.
_pause_event: asyncio.Event | None = None


def _pause_event_or_init() -> asyncio.Event:
    """Return the module-level pause Event, creating it on first call."""
    global _pause_event
    if _pause_event is None:
        _pause_event = asyncio.Event()
        _pause_event.set()  # default: not paused
    return _pause_event


def pause_scan() -> bool:
    """Pause all active and future scans.  Idempotent — returns True
    if the call actually flipped the state (was running, now paused),
    False if it was already paused.
    """
    ev = _pause_event_or_init()
    was_running = ev.is_set()
    ev.clear()
    if was_running:
        log.info("Scan paused — workers will block between files")
    return was_running


def resume_scan() -> bool:
    """Resume all paused scans.  Idempotent — returns True if the
    call actually flipped the state, False if it was already running.
    """
    ev = _pause_event_or_init()
    was_paused = not ev.is_set()
    ev.set()
    if was_paused:
        log.info("Scan resumed — workers will pick up the next file")
    return was_paused


def is_scan_paused() -> bool:
    """True iff the scan is currently paused.  Cheap, lockless."""
    return _pause_event is not None and not _pause_event.is_set()


async def _await_resume() -> None:
    """Block this worker until the scan is un-paused.

    A no-op when the scan isn't paused.  Called at the top of each
    per-file step so a long scan can be quiesced quickly without
    cancelling in-flight downloads.
    """
    ev = _pause_event_or_init()
    if not ev.is_set():
        # The worker is about to yield to other tasks while it waits —
        # update the progress's current_file label so the UI shows the
        # paused state instead of the file we're about to start on.
        await ev.wait()


def get_progress() -> ScanProgress:
    return _progress


# NOTE: ``is_scanning`` is defined once, later in this module
# (``is_scanning(path=None)`` — running OR queued).  A second, earlier
# ``def is_scanning() -> bool: return _progress.running`` used to live here but
# was DEAD CODE: module-scope re-definition meant the later one always won and
# this one was unreachable.  Removed so the name has a single, unambiguous
# meaning and the integrity sweep's scan-gate can't silently change behaviour.


# ── File discovery ─────────────────────────────────────────────────────────────

# OS-generated junk filenames that may carry an audio-looking extension but
# contain no real audio data.  AppleDouble sidecars (``._foo.m4a``) are the
# common one — macOS auto-creates them whenever it writes extended attributes
# to a non-Apple filesystem (FTP / SMB / exFAT).  Indexing them produces
# ghost tracks with random temp-file titles, so we skip them at discovery.
_JUNK_BASENAMES_LOWER = {
    ".ds_store", "thumbs.db", "desktop.ini", "icon\r",
}

def _is_junk_filename(name: str) -> bool:
    """True if ``name`` is an OS metadata sidecar that should never be indexed."""
    if not name:
        return True
    # macOS AppleDouble metadata sidecars (``._<original>``)
    if name.startswith("._"):
        return True
    if name.lower() in _JUNK_BASENAMES_LOWER:
        return True
    return False


def _basename_of(path_str: str) -> str:
    """Last path component, regardless of forward/back slash."""
    if "/" in path_str:
        return path_str.rsplit("/", 1)[-1]
    if "\\" in path_str:
        return path_str.rsplit("\\", 1)[-1]
    return path_str


def _member_basename(path_str: str) -> str:
    """A track's own file name.  For an archive member (``a.zip::b.mod``,
    ``a.zip::n.zip::b.mod``, ``a.zip::SUB\\b.mod``) that is the name after the
    LAST ``::`` and its last ``/`` or ``\\``, never ``a.zip::b.mod``."""
    if "::" in path_str:
        return path_str.rsplit("::", 1)[1].replace("\\", "/").rsplit("/", 1)[-1]
    return _basename_of(path_str)


def _member_stem(path_str: str) -> str:
    """Stem of :func:`_member_basename` — the title fallback."""
    return Path(_member_basename(path_str)).stem


# ── HVSC auto-detection ──────────────────────────────────────────────────────
# When a scan turns up SID files and the user hasn't configured an HVSC
# DOCUMENTS folder yet, look for one up the directory tree (DOCUMENTS sits at
# the HVSC root, a sibling of MUSICIANS/DEMOS/GAMES — so walking UP from where
# SID files live always passes through it).  Works for local and remote roots.

_SID_DETECT_EXTS = (".sid", ".psid")

# Scan roots we've already probed for an HVSC DOCUMENTS folder this process —
# so a root that has SID files but NO HVSC tree isn't re-probed (which, on a
# remote share, would mean dozens of slow stat() round-trips) on every scan.
# Cleared on process restart, and by the admin "Re-extract SID metadata" path
# (which calls reset_hvsc_probe_cache()), so a freshly-dropped DOCUMENTS folder
# is re-detected on the next scan without a restart.
_hvsc_probed_roots: set[str] = set()


def reset_hvsc_probe_cache() -> None:
    """Forget which scan roots were probed for HVSC, so the next scan re-detects
    (e.g. after the user dropped a DOCUMENTS folder onto an already-scanned root)."""
    _hvsc_probed_roots.clear()


def _detect_hvsc_docs_local(sid_dirs: set[str]) -> str | None:
    """Walk up from each SID directory looking for ``DOCUMENTS/Songlengths*``.
    Returns the DOCUMENTS path (str) or None.  ``seen`` dedupes the shared
    ancestor chains so we probe each directory at most once."""
    from soniqboom.core.hvsc import SONGLENGTHS_NAMES, DOCS_DIR_NAME
    seen: set[str] = set()
    for d in sorted(sid_dirs, key=len):           # shallowest first → fewer hops
        p = Path(d)
        for _ in range(16):
            sp = str(p)
            if sp in seen:
                break
            seen.add(sp)
            docs = p / DOCS_DIR_NAME
            for nm in SONGLENGTHS_NAMES:
                try:
                    if (docs / nm).is_file():
                        return str(docs)
                except OSError:
                    pass
            if p.parent == p:
                break
            p = p.parent
    return None


def _remote_file_exists(source, path: str) -> bool:
    try:
        st = source.stat(path)
        return not getattr(st, "is_dir", False)
    except Exception:
        return False


def _detect_hvsc_docs_remote(source, scan_root: str, sid_rel_dirs: set[str]) -> str | None:
    """Remote analogue of :func:`_detect_hvsc_docs_local`, walking up the
    root-relative tree with ``source.stat`` probes.  Returns a
    ``scan_root:/DOCUMENTS`` path (same addressing as track paths) or None."""
    from soniqboom.core.hvsc import SONGLENGTHS_NAMES, DOCS_DIR_NAME
    seen: set[str] = set()
    # All HVSC SIDs share one root, so a handful of representative chains is
    # plenty — cap the network probing.
    for d in sorted(sid_rel_dirs, key=len)[:8]:
        p = d or "/"
        for _ in range(16):
            if p in seen:
                break
            seen.add(p)
            docs_rel = str(PurePosixPath(p) / DOCS_DIR_NAME)
            for nm in SONGLENGTHS_NAMES:
                if _remote_file_exists(source, str(PurePosixPath(docs_rel) / nm)):
                    return f"{scan_root}:{docs_rel}"
            parent = str(PurePosixPath(p).parent)
            if parent == p:
                break
            p = parent
    return None


async def _apply_hvsc_autoconfig(docs_path: str) -> bool:
    """Auto-configure HVSC from a detected DOCUMENTS path (no-op if a path is
    already set) and tell connected clients so the admin UI updates live.
    Returns whether it configured HVSC just now."""
    from soniqboom.core.hvsc import auto_configure
    loop = asyncio.get_event_loop()
    applied = await loop.run_in_executor(None, auto_configure, docs_path)
    if applied:
        log.info("HVSC auto-configured from scan → %s", docs_path)
        try:
            from soniqboom.api.library import _broadcast
            await _broadcast({"event": "hvsc_configured", "docs_path": docs_path})
        except Exception:
            pass
    return bool(applied)


def _scope_walk(paths, onerror, root: str):
    """``os.walk``-shaped iteration over a SCOPED scan's changed paths, with
    the full walk's semantics: a directory is walked recursively (symlinked
    directories are not followed), an existing file — or a symlink to one —
    yields just itself, a path that no longer exists yields nothing (its
    tracks are pruned by the scoped stale cleanup).

    "Exists" is checked with the EXACT on-disk spelling of every component
    below ``root``: on a case-insensitive filesystem a case-only rename
    (``song.mp3`` → ``Song.mp3``) reports both names and both would stat
    fine, so the old spelling would be indexed alongside the new one.  A path
    whose component is a symlinked directory is outside what the full walk
    indexes and counts as gone.  Any error other than "not found" goes to
    ``onerror`` (→ the root is not pruned) instead of reading as a deletion."""
    import os
    import stat as _stat
    listings: dict[str, "set[str] | None"] = {}
    root = root.rstrip(os.sep) or os.sep

    def _names(d: str) -> "set[str] | None":
        if d not in listings:
            try:
                listings[d] = set(os.listdir(d))
            except (FileNotFoundError, NotADirectoryError):
                listings[d] = None
        return listings[d]

    for sp in sorted(paths):
        try:
            rel = sp[len(root):].strip(os.sep)
            if not rel or not sp.startswith(root):
                continue
            cur, gone = root, False
            comps = rel.split(os.sep)
            for i, comp in enumerate(comps):
                names = _names(cur)
                if names is None or comp not in names:
                    gone = True
                    break
                cur = os.path.join(cur, comp)
                if i < len(comps) - 1 and os.path.islink(cur):
                    gone = True
                    break
            if gone:
                continue
            try:
                st = os.lstat(sp)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if _stat.S_ISLNK(st.st_mode):
                try:
                    if _stat.S_ISREG(os.stat(sp).st_mode):
                        yield os.path.dirname(sp), [], [os.path.basename(sp)]
                except (FileNotFoundError, NotADirectoryError):
                    pass                        # dangling link: gone
                continue
            if _stat.S_ISDIR(st.st_mode):
                yield from os.walk(sp, onerror=onerror)
            elif _stat.S_ISREG(st.st_mode):
                yield os.path.dirname(sp), [], [os.path.basename(sp)]
        except OSError as exc:
            onerror(exc)


def _is_gone(path: str) -> bool:
    """The path no longer exists (not found / a dangling link / a parent that
    became a file) — any OTHER stat error (EACCES on a folder without search
    permission, EIO, ESTALE) means "can't tell", never "deleted"."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _archive_has_members(store, archive_path: str) -> bool:
    """Does the store hold tracks inside ``archive_path`` (``archive::…``)?
    Via the per-folder index — the archive's own folder only."""
    return archive_path in _archives_with_members(store, [archive_path])


def _archives_with_members(store, archive_paths) -> "set[str]":
    """The top-level archives among ``archive_paths`` the store holds tracks
    inside (``archive::…``) — one pass over each archive folder's index, not
    one per archive (a folder of many damaged archives stays linear)."""
    from soniqboom.core.data import path_hash as _ph
    by_dir: dict[str, set[str]] = {}
    for a in archive_paths:
        by_dir.setdefault(os.path.dirname(a), set()).add(a)
    out: set[str] = set()
    for d, wanted in by_dir.items():
        found: set[str] = set()
        for tid in store._tag_dir_hash.get(_ph(d), ()):
            t = store._tracks.get(tid)
            path = (t.get("path") or "") if t is not None else ""
            if "::" in path:
                outer = path.split("::", 1)[0]
                if outer in wanted:
                    found.add(outer)
                    if len(found) == len(wanted):
                        break                   # every archive of this folder has members
        out |= found
    return out


def _in_failed_archive(path: str, failed: "set[str]") -> bool:
    """Is ``path`` (``a.zip::m`` / ``a.zip::n.zip::m``) inside an archive — or a
    nested archive — that couldn't be read this time?  An unreadable NESTED zip
    protects only its own members, not the outer zip's."""
    if not failed or "::" not in path:
        return False
    parts = path.split("::")
    return any("::".join(parts[:k]) in failed for k in range(1, len(parts)))


def _find_audio_files(directories: list[str], scan_zips: bool = True,
                      scope: "dict[str, frozenset[str]] | None" = None,
                      known_archives: "dict[str, tuple[float | None, list[str]]] | None" = None,
                      failed_archives: "set[str] | None" = None,
                      ) -> dict[str, list[Path]]:
    """Discover the audio files of each root.  With ``scope`` (root → changed
    paths, from the folder watcher) a root lists only the files under those
    paths, still attributed to the root.

    ``known_archives`` (archive path → (mtime its indexed members carry, their
    virtual paths)): an archive whose mtime is unchanged is listed from that,
    without being opened — an automatic rescan otherwise re-read every nested
    zip in full just to name its members.  Manual scans pass None.

    ``failed_archives`` (optional, filled in): archives that exist but could
    not be read (corrupt, encrypted, permission) — their already-indexed
    members must not be pruned as deleted."""
    import io
    import os
    import zipfile

    def _is_audio(name: str) -> bool:
        # Extension-supported OR a uade Amiga prefix-form name (mdat.song);
        # companion sample halves are excluded inside the helper.
        return is_supported_music_name(name)

    result: dict[str, list[Path]] = {}
    # Roots for which os.walk hit a read error (permission denied, a
    # disconnected network mount, an I/O fault).  os.walk swallows these
    # silently and yields a PARTIAL (or empty) file list that is indistinguishable
    # from "these files were deleted" — so stale-cleanup must NOT prune from such
    # an enumeration (it would delete tracks whose files are merely unreadable).
    walk_errors: set[str] = set()
    for d in directories:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            log.warning("Scan dir not found, skipping: %s", p)
            continue

        files: list[Path] = []
        skipped_junk = 0

        _root_key = str(p)
        def _on_walk_error(err, _root=_root_key):
            walk_errors.add(_root)
            log.warning("Scan walk error under %s: %s — file listing is "
                        "incomplete, stale-cleanup will not prune this root", _root, err)

        # Single-pass walk — much faster than N separate rglob calls
        _scoped = scope.get(str(p)) if scope else None
        _walk = (_scope_walk(_scoped, _on_walk_error, str(p)) if _scoped is not None
                 else os.walk(p, onerror=_on_walk_error))
        for dirpath, _dirs, filenames in _walk:
            for fn in filenames:
                if _is_junk_filename(fn):
                    skipped_junk += 1
                    continue
                full = os.path.join(dirpath, fn)
                lower = fn.lower()
                is_container = scan_zips and (lower.endswith((".zip", ".lha", ".lzh"))
                                              or diskimage.is_disk_image(lower))

                if is_container and known_archives is not None and full in known_archives:
                    k_mtime, k_members = known_archives[full]
                    try:
                        unchanged = (k_mtime is not None
                                     and abs(os.stat(full).st_mtime - k_mtime) < 1.0)
                    except OSError:
                        unchanged = False
                    if unchanged:
                        files.extend(Path(m) for m in k_members)
                        continue

                if scan_zips and lower.endswith(".zip"):
                    # Scan inside ZIP files.  Tried BEFORE the audio-name test:
                    # ``ST.zip`` / ``MA.zip`` match Amiga prefix tokens but are
                    # archives; a name that isn't a readable zip falls through.
                    _before = len(files)
                    try:
                        with zipfile.ZipFile(full, 'r') as zf:
                            for member in zf.namelist():
                                member_basename = _basename_of(member)
                                if _is_junk_filename(member_basename):
                                    skipped_junk += 1
                                    continue
                                if member.lower().endswith(".zip"):
                                    # Nested ZIP (e.g. modarchive: outer.zip → track.it.zip → track.it)
                                    try:
                                        inner_data = zf.read(member)
                                    except Exception as exc:    # bad CRC, zlib, encrypted
                                        log.warning("Cannot read nested ZIP %s::%s: %s",
                                                    full, member, exc)
                                        if failed_archives is not None:
                                            failed_archives.add(str(Path(f"{full}::{member}")))
                                        continue
                                    try:
                                        with zipfile.ZipFile(io.BytesIO(inner_data), 'r') as inner_zf:
                                            for inner_name in inner_zf.namelist():
                                                if _is_junk_filename(_basename_of(inner_name)):
                                                    skipped_junk += 1
                                                    continue
                                                if _is_audio(inner_name):
                                                    files.append(Path(f"{full}::{member}::{inner_name}"))
                                        continue
                                    except zipfile.BadZipFile:
                                        pass            # not a zip: maybe a module named *.zip
                                    except Exception as exc:    # corrupt / encrypted member
                                        log.warning("Cannot read nested ZIP %s::%s: %s",
                                                    full, member, exc)
                                        if failed_archives is not None:
                                            failed_archives.add(str(Path(f"{full}::{member}")))
                                        continue
                                if _is_audio(member):
                                    # Direct audio file in ZIP
                                    files.append(Path(f"{full}::{member}"))
                        continue
                    except zipfile.BadZipFile as exc:
                        del files[_before:]
                        # truncated / damaged: keep what was indexed from it
                        # (only ``full::…`` members are protected — a real
                        # module named like this still indexes below)
                        if failed_archives is not None and not _is_gone(full):
                            failed_archives.add(full)
                        if not _is_audio(fn):
                            log.warning("Cannot read ZIP %s: %s", full, exc)
                            continue
                    except Exception as exc:                # unreadable / corrupt: keep members
                        del files[_before:]
                        log.warning("Cannot read ZIP %s: %s", full, exc)
                        if failed_archives is not None and not _is_gone(full):
                            failed_archives.add(full)       # (vanished / dangling: a deletion)
                        continue

                elif scan_zips and diskimage.is_disk_image(lower):
                    # Crack open vintage disk images (C64 .d64/.d71/.d81,
                    # Amiga .adf) and surface embedded SID / tracker tunes as
                    # ``::``-members — exactly like ZIP entries.  Tried before
                    # the audio-name test (``MA.adf`` matches a prefix token).
                    try:
                        _members = list(diskimage.list_members(full, strict=True))
                    except Exception as exc:
                        if failed_archives is not None and not _is_gone(full):
                            failed_archives.add(full)        # keep its indexed members
                        if isinstance(exc, OSError) or not _is_audio(fn):
                            # (a game disk without a filesystem is routine: once
                            # discovery is done the scan warns only for one that
                            # holds indexed tracks)
                            (log.debug if isinstance(exc, diskimage.NotListable)
                             else log.warning)("Cannot read disk image %s: %s", full, exc)
                            continue
                        _members = []                        # not an image: maybe a module
                    if _members or not _is_audio(fn):
                        files.extend(Path(f"{full}::{m}") for m in _members)
                        continue

                elif scan_zips and lower.endswith((".lha", ".lzh")):
                    # Amiga LHA/LZH archives — surface the modules inside
                    # (handles the ``MOD.title`` Amiga prefix naming).  Tried
                    # before the audio-name test (``ST.lha``).
                    try:
                        _members = list(archive.list_members(full, strict=True))
                    except Exception as exc:
                        if failed_archives is not None and not _is_gone(full):
                            failed_archives.add(full)        # keep its indexed members
                        if isinstance(exc, OSError) or not _is_audio(fn):
                            log.warning("Cannot read LHA archive %s: %s", full, exc)
                            continue
                        _members = []                        # not an archive: maybe a module
                    if _members or not _is_audio(fn):
                        files.extend(Path(f"{full}::{m}") for m in _members)
                        continue

                if _is_audio(fn):
                    files.append(Path(full))

        result[str(p)] = sorted(set(files))
        if skipped_junk:
            log.info("Discovered %d audio files in %s (skipped %d OS junk file(s))",
                     len(files), p, skipped_junk)
        else:
            log.info("Discovered %d audio files in %s", len(files), p)
    return result, walk_errors


def _find_remote_audio_files(
    root_path: str, source: "FileSource",
) -> dict[str, list[str]]:
    """Discover audio files on a remote FileSource.

    Returns {root_path: [remote_path, ...]}.  Paths are strings (not Path
    objects) because remote paths aren't real filesystem paths.
    """
    from soniqboom.core.filesource import FileSource

    files: list[str] = []
    skipped_junk = 0
    try:
        for dirpath, _dirs, filenames in source.walk("/"):
            for fn in filenames:
                if _is_junk_filename(fn):
                    skipped_junk += 1
                    continue
                if is_supported_music_name(fn):
                    fpath = f"{dirpath}/{fn}" if dirpath != "/" else f"/{fn}"
                    files.append(fpath)
    except Exception as exc:
        log.error("Remote walk failed for %s: %s", root_path, exc)
    if skipped_junk:
        log.info("Discovered %d remote audio files in %s (skipped %d OS junk file(s))",
                 len(files), root_path, skipped_junk)
    else:
        log.info("Discovered %d remote audio files in %s", len(files), root_path)
    return {root_path: sorted(set(files))}


# ── One-shot library cleanup ─────────────────────────────────────────────────
#
# Two pre-existing data issues that older builds of SoniqBoom could let into
# the library:
#
#   1. Tracks whose path basename is an OS junk file (``._*`` AppleDouble
#      metadata sidecars on FTP/SMB, ``.DS_Store``, ``Thumbs.db``).  These
#      are not audio.  Discovery now filters them — this purge removes any
#      that already slipped in.
#
#   2. Tracks whose ``title`` is a leaked Python ``tempfile`` basename
#      (``tmp[a-z0-9_]{8}``) because the source had no title tag and the
#      pre-fix extractor used the temp file's stem as the fallback.  We
#      derive the correct title from the path basename instead — no rescan
#      (or network I/O) needed.

import re as _re
_TMP_TITLE_RE = _re.compile(r"^tmp[a-z0-9_]{6,12}$")


async def purge_junk_tracks() -> dict:
    """Remove ghost tracks created by AppleDouble files and repair leaked titles.

    Safe to run on every startup: idempotent and cheap (in-memory scan only).
    Returns counts for logging.
    """
    from soniqboom.core.data import delete_track_ids
    from soniqboom.core.store import get_store

    store = get_store()

    junk_ids: list[str] = []
    title_fixups: list[tuple[str, str]] = []  # (track_id, new_title)

    for t in store.all_tracks():
        path = t.get("path") or ""
        if not path:
            continue
        base = _basename_of(path)

        if _is_junk_filename(base):
            junk_ids.append(t["id"])
            continue

        title = (t.get("title") or "").strip()
        if title and _TMP_TITLE_RE.match(title):
            real_stem = _member_stem(path)
            if real_stem and real_stem != title:
                title_fixups.append((t["id"], real_stem))

    deleted = 0
    if junk_ids:
        deleted = await delete_track_ids(junk_ids)
        log.info("Purged %d ghost track(s) from OS metadata sidecars (._*, .DS_Store, ...)",
                 deleted)

    repaired = 0
    if title_fixups:
        for tid, new_title in title_fixups:
            if store.update_track_fields(tid, {"title": new_title}):
                repaired += 1
        log.info("Repaired %d track title(s) leaked from temp-file basenames", repaired)

    return {"deleted": deleted, "repaired_titles": repaired}


def _extract_one_remote(
    file_data: bytes, remote_path: str, track_id: str,
    pc_program_archive: bool = False,
) -> tuple[str, TrackMeta | str, None, None]:
    """Extract metadata from an already-downloaded file buffer.

    Runs in a worker process like _extract_one.  Writes data to a temp file
    because mutagen requires a seekable file handle for most formats.

    ``pc_program_archive`` is precomputed by the caller (the archive can't be
    re-opened cheaply here in the worker) — True when *remote_path* is a member
    of an archive that also holds a DOS ``MZ`` executable, which vetoes the
    uade lenient fallback (see :func:`soniqboom.core.metadata.extract`).
    """
    import shutil
    import tempfile
    try:
        # The member's own name for an archive member, like the local
        # ``_extract_from_zip`` — ``a.zip::SUB\b.bd`` named its temp file
        # ``a.zip::SUB\b.bd`` and was titled ``SUB\b``.
        real_base = _member_basename(remote_path)
        from soniqboom.core import uade_formats as _uade
        if _uade.classify(real_base) is not None:
            # Amiga prefix-form names (mdat.song) lose their identity in a
            # random-stem temp file (QA M2: tmpXXXX.song classifies as
            # nothing) — preserve the REAL basename in a private temp dir.
            tdir = Path(tempfile.mkdtemp(prefix="sq_remscan_"))
            tmp_path = tdir / real_base
            tmp_path.write_bytes(file_data)
            cleanup = lambda: shutil.rmtree(tdir, ignore_errors=True)  # noqa: E731
        else:
            ext = os.path.splitext(remote_path)[1]
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(file_data)
                tmp_path = Path(tmp.name)
            cleanup = lambda: tmp_path.unlink(missing_ok=True)  # noqa: E731
        try:
            meta = extract(
                tmp_path, track_id,
                pc_program_check=(lambda: True) if pc_program_archive else None,
            )
            meta.path = remote_path
            meta.mtime = 0.0
            meta.file_size = len(file_data)
            # Title fallback inside extract() used the temp file's basename
            # (e.g. ``tmpXXXXXXXX``) when the source had no title tag.  Replace
            # it with the real remote basename so the UI doesn't show garbage.
            real_stem = Path(real_base).stem
            if real_stem and meta.title == tmp_path.stem:
                meta.title = real_stem
            return remote_path, meta, None, None
        finally:
            cleanup()
    except Exception as exc:
        return remote_path, f"{type(exc).__name__}: {exc}", None, None


def _pc_memo_key(remote_path: str) -> tuple[str, str]:
    """``(archive_rel, member_dir)`` key for the PC-program-archive memo.

    Keyed by the member's DIRECTORY inside the archive, not the archive alone:
    the PC-executable signal is scoped to the module's own folder (see
    ``archive.has_dos_executable``), so a DOS tool in an unrelated subdir of a
    big compilation must not taint modules in other subdirs.  Both the writer
    (``_fetch_zip_member``) and reader (``_process_one``) derive the key from
    the same ``remote_path`` so they can never disagree.
    """
    arc, _, member = remote_path.partition("::")
    mnorm = member.replace("\\", "/")           # Amiga dirs use backslashes
    mdir = mnorm.rsplit("/", 1)[0] if "/" in mnorm else ""
    return (arc, mdir)


# ── Phase 1 helpers ───────────────────────────────────────────────────────────

def _read_from_zip_path(virtual_path: str) -> tuple[bytes, str]:
    """Read raw file bytes from a (possibly nested) ZIP virtual path.

    Supports paths like:
      /path/archive.zip::track.sid
      /path/outer.zip::inner.zip::track.mod
      /path/disk.d64::THE RUNNER.sid          (C64/Amiga disk images)

    Returns (file_bytes, final_member_name).
    """
    # Vintage disk images (C64 .d64/.d71/.d81, Amiga .adf) are read by the
    # diskimage module.  They don't nest, so the member is everything after
    # the first ``::``.
    outer = virtual_path.split("::", 1)[0]
    if diskimage.is_disk_image(outer) and "::" in virtual_path:
        member = virtual_path.split("::", 1)[1]
        return diskimage.read_member(outer, member), member
    # Amiga LHA/LZH (and the same generic path works for a local cached zip):
    # no nesting, so the member is everything after the first ``::``.
    if archive.is_lha_name(outer) and "::" in virtual_path:
        member = virtual_path.split("::", 1)[1]
        return archive.read_member(outer, member), member

    import io
    import zipfile

    parts = virtual_path.split("::")
    # First part is always the outer ZIP on disk.  For nested archives
    # we spill each intermediate level to a tempfile rather than keeping
    # the enclosing member in RAM — Audio-2 P1 found that nesting depth
    # > 1 produced a worst-case peak of "sum of all enclosing member
    # sizes" of RAM because we read each level into bytes + wrapped in
    # BytesIO.  For outer.zip(500MB) -> inner.zip(200MB) -> track.mod
    # that was a 700 MB transient.  Tempfile spill bounds it to one
    # member-size of disk IO instead.
    import tempfile, os as _os

    # For 1-deep nesting (common case) stay with the in-memory read —
    # avoids the disk syscall when the member is small.
    if len(parts) == 2:
        # Route the common single-level case through the cached archive reader
        # so a huge LOCAL zip doesn't re-open per member (the same O(n^2) the
        # FTP path hit on a 4491-member archive).
        return archive.read_member(parts[0], parts[1]), parts[-1]

    # Deeper nesting: pass through tempfiles.
    current_zip_path = parts[0]
    intermediates: list[str] = []
    try:
        for i, member in enumerate(parts[1:], 1):
            if i < len(parts) - 1:
                # Intermediate ZIP — extract to disk, open the next level
                # from the new file path so the prior level can be
                # released (zf.close in the with-block).
                with zipfile.ZipFile(current_zip_path, 'r') as zf:
                    with zf.open(member, 'r') as src:
                        tmp = tempfile.NamedTemporaryFile(
                            suffix='.zip', delete=False,
                        )
                        try:
                            # Stream the inner member in chunks so peak
                            # RAM is one chunk, not the whole file.
                            while True:
                                buf = src.read(1024 * 1024)
                                if not buf:
                                    break
                                tmp.write(buf)
                        finally:
                            tmp.close()
                        intermediates.append(tmp.name)
                current_zip_path = tmp.name
            else:
                # Final level — read into bytes for the caller.
                with zipfile.ZipFile(current_zip_path, 'r') as zf:
                    return zf.read(member), parts[-1]
    finally:
        for p in intermediates:
            try:
                _os.unlink(p)
            except OSError:
                pass

    # Should not reach here — the loop above always returns when it
    # processes the last part.  Defensive return so the function never
    # falls off the end with an undefined value.
    return b"", parts[-1]


def _extract_from_zip(virtual_path: str, track_id: str) -> TrackMeta:
    """Extract metadata from a file inside a (possibly nested) ZIP archive."""
    import shutil
    import tempfile

    data, member_name = _read_from_zip_path(virtual_path)
    real_base = _basename_of(member_name)

    # UADE Amiga members need their REAL basename (detection is name-based:
    # ``mdat.song``; the archive layer may have appended a routing extension
    # → strip it back off) and their companion sample halves extracted
    # alongside (TFMX ``smpl.X`` etc.), or ``uade123 -g`` rejects them.
    # Strip check runs FIRST (QA C2): classify("mdat.X.mdat") matches via the
    # prefix token, so a classify-first order never stripped and uade then
    # derived a nonexistent ``smpl.X.mdat`` companion.
    from soniqboom.core import uade_formats as _uade
    uade_name = real_base
    if "." in real_base:
        _stem, _, _last = real_base.rpartition(".")
        from soniqboom.core.metadata import _UADE_SUFFIX_EXTS
        if (f".{_last.lower()}" in _UADE_SUFFIX_EXTS
                and _uade.classify(_stem) is not None):
            uade_name = _stem
    is_uade_member = _uade.classify(uade_name) is not None

    if is_uade_member:
        tdir = Path(tempfile.mkdtemp(prefix="sq_uadescan_"))
        tmp_path = tdir / uade_name
        tmp_path.write_bytes(data)
        # Pull companion halves from the SAME archive directory, best-effort.
        # Amiga archives often use BACKSLASH separators (QA m2) — normalize.
        _member_norm = member_name.replace("\\", "/")
        member_dir = _member_norm.rsplit("/", 1)[0] if "/" in _member_norm else ""
        outer = virtual_path.rsplit("::", 1)[0]
        for sib in _uade.companion_sibling_names(uade_name):
            sib_member = f"{member_dir}/{sib}" if member_dir else sib
            try:
                sib_data, _ = _read_from_zip_path(f"{outer}::{sib_member}")
                (tdir / sib).write_bytes(sib_data)
            except Exception:
                continue                            # extras are optional
        try:
            # Veto a lenient uade admit when the archive is a PC program
            # bundle (a DOS ``MZ`` .exe/.com beside the module) — e.g. a
            # demo's ``X.dat`` that only matched PaulRobotham by extension.
            # Lazy: ``extract`` calls this ONLY on a ``-g`` rejection, so a
            # healthy archive never pays the raw-namelist scan.
            meta = extract(
                tmp_path, track_id,
                pc_program_check=lambda: archive.has_dos_executable(
                    outer, member_dir),
            )
            meta.path = virtual_path
            # Aegis Sonix modules pull samples from a sibling ``Instruments/``
            # subdir; when the archive is missing some, the play path fills them
            # with silence (a degraded render).  Flag the track ``partial`` here
            # — while we already hold the .smus bytes + the archive — so the UI
            # can badge it without a first play.  Same "missing" computation the
            # renderer uses (api/stream.py), so badge and render agree.
            if (_uade.classify(uade_name) or (None,))[0] in _uade.SONIX_PLAYERS:
                try:
                    _prefix = (f"{member_dir}/Instruments/"
                               if member_dir else "Instruments/").lower()
                    _present = {
                        r.replace("\\", "/").rsplit("/", 1)[-1].lower()
                        for r in archive.raw_namelist(outer)
                        if r.replace("\\", "/").lower().startswith(_prefix)
                    }
                    _missing = _uade.sonix_missing_instruments(data, _present)
                    if _missing:
                        _n = len(_missing)
                        _shown = ", ".join(_missing[:3]) + ("…" if _n > 3 else "")
                        meta.defect = "partial"
                        meta.defect_detail = (
                            f"{_n} instrument{'s' if _n != 1 else ''} "
                            f"substituted (silent): {_shown}")
                except Exception:
                    log.debug("Sonix instrument check failed for %s",
                              virtual_path, exc_info=True)
            return meta
        finally:
            shutil.rmtree(tdir, ignore_errors=True)

    suffix = Path(member_name).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        meta = extract(tmp_path, track_id)
        meta.path = virtual_path
        # Title fallback inside extract() used the temp file's basename when the
        # source had no title tag.  Substitute the real ZIP member name so the
        # UI doesn't show ``tmpXXXXXXXX``.
        real_stem = Path(real_base).stem
        if real_stem and meta.title == tmp_path.stem:
            meta.title = real_stem
        return meta
    finally:
        tmp_path.unlink(missing_ok=True)


def _dir_has_dos_executable(directory: Path) -> bool:
    """True iff *directory* holds a DOS ``MZ`` ``.exe``/``.com`` file — the
    loose-file analogue of :func:`archive.has_dos_executable` (see it for why
    an ``MZ`` sibling means "PC program data, not an Amiga module").  Reads
    only the 2-byte magic per candidate.  Best-effort: returns False on any
    OS error so it can never abort a scan.
    """
    try:
        for p in directory.glob("*"):
            if p.suffix.lower() not in (".exe", ".com"):
                continue
            try:
                if not p.is_file():
                    continue
                with open(p, "rb") as fh:
                    if fh.read(2) == b"MZ":
                        return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _extract_one(path: Path) -> tuple[Path, TrackMeta | str, bytes | None, bytes | None]:
    """Run in a **worker process** — extract metadata for a single file.

    Returns (path, meta_or_error_string, sm_thumb, lg_thumb).
    Errors are returned as strings (not Exception objects) because they must
    survive pickle serialization across process boundaries.
    """
    try:
        path_str = str(path)
        track_id = str(uuid.uuid5(uuid.NAMESPACE_URL, path_str))

        if '::' in path_str:
            meta = _extract_from_zip(path_str, track_id)
        else:
            # Loose local file: a PC data payload (a demo's ``X.dat`` matching
            # PaulRobotham by extension) sitting beside its ``X.EXE`` in an
            # unpacked demo folder gets the same veto as the archive case.
            # Lazy — ``extract`` calls this only on a ``-g`` rejection.
            meta = extract(
                path, track_id,
                pc_program_check=lambda: _dir_has_dos_executable(path.parent))

        # Stamp mtime — for ZIP files, use the outer archive's mtime
        actual_path = Path(path_str.split('::')[0]) if '::' in path_str else path
        meta.mtime = actual_path.stat().st_mtime

        return path, meta, None, None
    except Exception as exc:
        return path, f"{type(exc).__name__}: {exc}", None, None


def _build_track(
    meta: TrackMeta,
    scan_root: str,
    parent: str,
    hash_cache: dict[str, str],
) -> tuple[Track | None, str | None]:
    """Assemble a Track object; resolves dir/root hashes via the local cache.

    Returns (track, raw_art_data_uri).
    Thumbnails are generated in _extract_one (thread pool) to avoid blocking
    the async event loop.
    """
    from soniqboom.config import settings
    try:
        dir_h  = hash_cache[parent]
        root_h = hash_cache[scan_root]
        meta_dict = meta.model_dump()
        meta_dict["dir_hash"]        = dir_h
        meta_dict["scan_root_hash"]  = root_h

        # Strip the embedded base64 art; replace with a URL reference
        raw_art = meta_dict.pop("cover_art", None)
        meta_dict["cover_art"] = f"/api/art/{meta.id}" if raw_art else None

        # Don't store a huge zero embedding — leave empty, Phase 2 fills it
        track = Track(**meta_dict, embedding=[])

        # Persist embedded art during the scan ONLY for remote (FTP/SMB) tracks.
        # On-access extraction (api/art._resolve_full_art) can re-read a LOCAL
        # file at any time, so for local tracks we skip storing here to save
        # scan time + disk.  But a remote file is NOT on local disk unless it's
        # been played, so on-access extraction can't recover its embedded cover
        # — we must persist it now, while we still hold the bytes the scan just
        # fetched.  Without this, embedded-art-only remote tracks (e.g. iTunes
        # ``.m4a``, which carry no folder.jpg) show the placeholder forever.
        is_remote = (meta.path or "").startswith(("ftp://", "smb://"))
        return track, (raw_art if is_remote else None)
    except Exception as exc:
        log.error("Failed to build track %s: %s", meta.path, exc)
        return None, None



# Phase 2 (embedding computation) removed — Python-only mode has no vector search.


def _compute_incremental(
    files_strs: list[str],
    mtime_size_map: dict[str, tuple[float | None, int | None]],
) -> tuple[set[str], dict[str, str]]:
    """Determine which files need scanning.  **Runs in a worker process.**

    Receives only primitive data (strings, dicts of tuples) so it can be
    pickled across the process boundary.  Returns (fresh_path_strs,
    track_ids_for_files) where track_ids_for_files maps path_str → track_id.
    """
    # Cache stat results by the real filesystem path so that virtual paths
    # sharing the same outer ZIP (e.g. "archive.zip::inner.zip::track.mod")
    # only trigger ONE os.stat() call per unique outer file.  For a library
    # with 122K virtual paths across ~60K ZIPs on a network mount, this can
    # cut stat() calls by half and avoids minutes of blocking.
    _stat_cache: dict[str, os.stat_result | None] = {}
    path_stats: dict[str, tuple[float, int]] = {}
    for ps in files_strs:
        actual = ps.split("::")[0] if "::" in ps else ps
        if actual not in _stat_cache:
            try:
                _stat_cache[actual] = os.stat(actual)
            except OSError:
                _stat_cache[actual] = None
        st = _stat_cache[actual]
        if st is not None:
            path_stats[ps] = (st.st_mtime, st.st_size)

    track_ids_for_files = {
        ps: str(uuid.uuid5(uuid.NAMESPACE_URL, ps)) for ps in files_strs
    }

    fresh: set[str] = set()
    for ps, tid in track_ids_for_files.items():
        if ps not in path_stats:
            continue
        existing = mtime_size_map.get(tid)
        if not existing:
            continue
        stored_mtime, stored_size = existing
        actual_mtime, actual_size = path_stats[ps]
        if stored_mtime is None or abs(stored_mtime - actual_mtime) >= 1.0:
            continue
        # An archive member (``a.zip::x.mod``) stores the MEMBER's size but the
        # stat is the outer archive's, so sizes never match — every full scan
        # re-extracted every member.  The archive's mtime (what extraction
        # stamps on its members) decides: a rewritten archive gets a new one.
        if "::" in ps or (stored_size is not None and stored_size == actual_size):
            fresh.add(ps)

    return fresh, track_ids_for_files


# ── Phase 3 helper ────────────────────────────────────────────────────────────

def _compute_waveform(path: str, points: int = 200):
    """Extract a compact waveform (peaks + RMS) from an audio file.

    Uses ffmpeg to decode to mono 22.05 kHz 32-bit float PCM, then computes
    both peak-absolute and RMS amplitudes over evenly-sized chunks,
    normalised against the per-axis peak.  8 kHz was below the Nyquist for
    most musical content and lost transient detail; 22 kHz keeps everything
    up to the typical CD bandwidth half-rate while staying small.
    Return shape: ``{"peaks": [...], "rms": [...]}`` (numpy or the
    pure-Python fallback); ``[0.0] * points`` for an empty decode.
    NOTE: still a sync function; callers ``run_in_executor`` it from
    asyncio paths (see ``api/tracks.py`` ``_WAVEFORM_POOL``).  Going async
    would require rewriting every caller and the dedicated thread pool
    that exists precisely to keep this work off the default executor.
    """
    from soniqboom.config import settings

    cmd = [
        settings.ffmpeg_path, "-i", path,
        "-ac", "1", "-ar", "22050", "-f", "f32le", "-",
    ]
    # forksafe: runs in the dedicated _WAVEFORM_POOL threads — plain
    # subprocess.run forks the CF-initialised server from a worker thread.
    from soniqboom.core import forksafe
    proc = forksafe.run(
        cmd, capture_output=True, timeout=60,
    )
    return _pcm_to_waveform(proc.stdout, points)


def _pcm_to_waveform(raw: bytes, points: int = 200):
    """Crunch raw mono 22.05 kHz f32le PCM into a compact waveform.

    Split out from ``_compute_waveform`` so the ffmpeg DECODE can be driven by
    ``forksafe.spawn`` on the event loop (no fork on macOS —
    ``subprocess.run`` fork from a worker thread segfaults once the process has
    initialised Core Foundation, e.g. after the stations relay's outbound
    networking), while this CPU-bound crunch still runs in a worker thread.
    Return shape mirrors ``_compute_waveform``: ``{"peaks", "rms"}`` on both
    the numpy path and the pure-Python fallback (a flat zero list only for
    empty input).

    The fallback reads the samples through a zero-copy ``memoryview`` of the
    f32le bytes (an ``array('f')`` copy on big-endian hosts) — never a Python
    float object per sample, which cost ~1 MB of RAM per second of audio —
    and reduces each bin with C-level ``sum``/``max``/``min``.
    """
    if not raw:
        return [0.0] * points

    # Each sample is a 32-bit (4-byte) float
    n_samples = len(raw) // 4
    if n_samples == 0:
        return [0.0] * points

    # NumPy when available (it is not a declared dependency) — vectorised.
    # Otherwise the pure-Python fallback below, same output shape.
    try:
        import numpy as _np
        samples = _np.frombuffer(raw[: n_samples * 4], dtype=_np.float32)
        chunk_size = max(1, n_samples // points)
        usable = chunk_size * points
        # Truncate the tail < chunk_size and reshape to (points, chunk_size).
        # ``einsum("ij,ij->i", c, c)`` computes the per-row sum-of-squares
        # without materialising the full ``c * c`` intermediate (which was
        # a ~115 MB float32 array for a 60-minute track at 8 kHz mono).
        chunks = samples[:usable].reshape(points, chunk_size)
        sumsq = _np.einsum("ij,ij->i", chunks, chunks)
        rms = _np.sqrt(sumsq / chunk_size)
        peaks = _np.abs(chunks).max(axis=1)
        rms_peak = float(rms.max()) if rms.size else 0.0
        peak_peak = float(peaks.max()) if peaks.size else 0.0
        if rms_peak > 0:
            rms = rms / rms_peak
        if peak_peak > 0:
            peaks = peaks / peak_peak
        return {
            "peaks": peaks.astype(float).tolist(),
            "rms": rms.astype(float).tolist(),
        }
    except ImportError:
        pass

    import operator
    import sys
    if sys.byteorder == "little":
        # Zero-copy view of the f32le bytes (no second buffer the size of
        # the decode — ~317 MB for a 60-minute track).
        samples = memoryview(raw)[: n_samples * 4].cast("f")
    else:
        import array
        samples = array.array("f")
        samples.frombytes(raw[: n_samples * 4])
        samples.byteswap()                      # f32le on the wire
    chunk_size = max(1, n_samples // points)

    rms_values: list[float] = []
    peak_values: list[float] = []
    for i in range(points):
        chunk = samples[i * chunk_size:(i + 1) * chunk_size]
        if not chunk:
            rms_values.append(0.0)
            peak_values.append(0.0)
            continue
        rms_values.append(math.sqrt(sum(map(operator.mul, chunk, chunk)) / len(chunk)))
        peak_values.append(max(max(chunk), -min(chunk)))

    for vals in (rms_values, peak_values):
        top = max(vals) if vals else 0.0
        if top > 0:
            vals[:] = [v / top for v in vals]
    return {"peaks": peak_values, "rms": rms_values}


# ── Non-blocking helpers ──────────────────────────────────────────────────────

async def _sort_yielding(lst: list, run: int = 20_000, slice_: int = 8_000) -> list:
    """``sorted(lst)`` without one long stall: sort ``run``-sized slices, then
    merge them ``slice_`` items at a time with yields; the cyclic GC is paused
    inside each step (the tuples are acyclic — a full collection landing in a
    step was the stall).  Raises TypeError like ``list.sort`` (the caller's
    mixed-type fallback)."""
    import gc
    if len(lst) <= run:
        lst.sort()
        return lst

    runs = []
    for i in range(0, len(lst), run):
        was = gc.isenabled()
        gc.disable()
        try:
            r = lst[i : i + run]
            r.sort()
        finally:
            if was:
                gc.enable()
        runs.append(r)
        await asyncio.sleep(0)
    out: list = []
    merged = heapq.merge(*runs)
    while True:
        was = gc.isenabled()
        gc.disable()
        try:
            part = list(itertools.islice(merged, slice_))
            out.extend(part)
        finally:
            if was:
                gc.enable()
        if not part:
            break
        await asyncio.sleep(0)
    return out


async def _async_exit_batch_mode(store) -> None:
    """Exit batch mode with yield points between sorted-index rebuilds.

    ``_rebuild_sorted_indexes`` is O(n log n) and freezes the event loop for
    hundreds of milliseconds on large libraries.  By splitting the work into
    per-field sorts with ``asyncio.sleep(0)`` between them, HTTP requests can
    be served in the gaps.  Only the lists marked dirty (``store._dirty_sorted``)
    are rebuilt: all ten after a scan's inserts/deletes, but just the re-keyed
    ones after a field-update batch (a Modland/folder album pass re-sorts
    ``_sorted_album`` alone).

    Keep ``_batch_mode = True`` until the freshly-built lists are assigned —
    if we flipped to False first, any ``_index_track`` running concurrently
    during the ``await asyncio.sleep(0)`` points would have written into the
    OLD ``_sorted_*`` lists, only for the rebuilt lists to overwrite them.
    """
    # Reference-counted exit: only the OUTERMOST batch section rebuilds.  The
    # decrement + depth check run synchronously (no await between) so a
    # concurrent scan commit can't race the counter; a nested exit just unwinds
    # one level and leaves the outer section's deferred rebuild intact.
    store._batch_depth -= 1
    if store._batch_depth > 0:
        return
    store._batch_depth = 0
    if not store._dirty_sorted:
        store._batch_mode = False
        return

    # Single source of truth for the year-collapse rule — keeps this
    # async/yielding rebuild aligned with TrackStore._index_track and the
    # in-line _rebuild_sorted_indexes path.
    from soniqboom.core.store import (normalise_year, SORTED_LISTS, _game_fold_value,
                                      _sortable_duration)
    from soniqboom.core.data import _rebuild_lock_for_loop

    EMPTY_SORT_KEY = "\uffff"
    _numeric = frozenset(SORTED_LISTS[:5])
    try:
        # Serialize the actual rebuild against every OTHER index rebuild —
        # concurrent scan-commit exits AND data.rebuild_indexes — under the
        # shared rebuild lock.  The reference count alone can NOT serialize the
        # yielding rebuilds: two non-nested exits can each reach depth 0 and
        # rebuild concurrently off different snapshots, and a slow/stale one
        # could assign LAST and drop the fresher one's tracks (with
        # _sorted_dirty cleared → no repair).  Holding the lock makes rebuilds
        # run one-at-a-time; re-reading _tracks FRESH inside the lock plus a
        # _mutation_seq / _duration_seq generation guard (retry if a track write
        # lands during the build) guarantees the assigned lists match the live
        # track set.
        async with _rebuild_lock_for_loop():
            for _attempt in range(6):
                plan = store.sorted_rebuild_plan()
                if not plan:
                    # A prior lock holder already rebuilt everything up to date.
                    break
                names = frozenset(plan)
                # Duration-only writes (a render / probe backfill) bump only
                # ``_duration_seq`` but still re-key ``_sorted_duration``.
                gen0 = (store._mutation_seq, getattr(store, "_duration_seq", 0))
                built: dict[str, list] = {}
                if len(names) == len(SORTED_LISTS) and all(v is None for v in plan.values()):
                    # Build ALL sorted lists (numeric, lexical, the ``game:``
                    # fold side lists) in one pass, mirroring
                    # TrackStore._index_track / _rebuild_sorted_indexes exactly
                    # so this async rebuild produces byte-identical indexes.
                    year, added, added_primary, dur, bpm = [], [], [], [], []
                    title, artist_s, album_artist_s, album_s, fmt = [], [], [], [], []
                    title_fold = []
                    # Snapshot + chunks with yields (the build loop alone was ~2 s
                    # at 263K); a write landing meanwhile fails the generation
                    # guard below and the pass is retried.
                    _snap = list(store._tracks.items())
                    import gc as _gc
                    for _ci in range(0, len(_snap), 5_000):
                        if _ci:
                            await asyncio.sleep(0)
                        # GC paused per chunk: a full collection landing in one
                        # (the build allocates ~3M acyclic tuples) was the stall.
                        _was_gc = _gc.isenabled()
                        _gc.disable()
                        try:
                            for tid, t in _snap[_ci:_ci + 5_000]:
                                y = normalise_year(t.get("year"))
                                if y is not None:
                                    year.append((y, tid))
                                a = t.get("added_at", 0)
                                if a:
                                    added.append((a, tid))
                                    if t.get("is_duplicate_primary", True):
                                        added_primary.append((a, tid))
                                d = t.get("duration", 0.0)
                                if _sortable_duration(d):
                                    dur.append((d, tid))
                                b = t.get("bpm")
                                if b is not None:
                                    bpm.append((b, tid))
                                title.append(         ((t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
                                artist_s.append(      ((t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY, tid))
                                album_artist_s.append(((t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY, tid))
                                album_s.append(       ((t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
                                fmt.append(           ((t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY, tid))
                                tf = _game_fold_value(t.get("title"))
                                if tf is not None:
                                    title_fold.append((tf, tid))
                        finally:
                            if _was_gc:
                                _gc.enable()

                    # In ``SORTED_LISTS`` order (store._SORTED_SPECS).
                    built = dict(zip(SORTED_LISTS, (year, added, added_primary, dur, bpm,
                                                    title, artist_s, album_artist_s,
                                                    album_s, fmt, title_fold)))
                else:
                    # Field-update batches: merge only the re-keyed ids back
                    # into each list (a full build for lists an insert dirtied).
                    for name in SORTED_LISTS:
                        if name in names:
                            built[name] = store.build_sorted_list(name, plan[name])
                            await asyncio.sleep(0)
                await asyncio.sleep(0)

                # Sort each list separately, yielding between them.  ALL sorts
                # are TypeError-guarded so a stray mixed-type key can never abort
                # the rebuild mid-flight and strand the store in batch mode.
                for name, lst in list(built.items()):
                    try:
                        built[name] = await _sort_yielding(lst)
                        continue
                    except TypeError:
                        if name in _numeric:
                            for i, (val, tid) in enumerate(lst):
                                try:
                                    lst[i] = (float(val), tid)
                                except (ValueError, TypeError):
                                    lst[i] = (0.0, tid)
                            lst.sort()
                        else:
                            lst.sort(key=lambda kv: (str(kv[0]), kv[1]))
                    await asyncio.sleep(0)

                # Generation guard: if any track-set/field write landed while we
                # were building+sorting (the yield points above), our snapshot is
                # stale — DON'T assign it; loop and rebuild from the fresh set.
                # (Play/rating and cover-art-only writes deliberately don't bump
                # _mutation_seq, and touch no sorted list, so they don't force
                # needless retries.)
                if (store._mutation_seq, getattr(store, "_duration_seq", 0)) != gen0:
                    continue
                _replaced = [getattr(store, name) for name in built]
                for name, lst in built.items():
                    setattr(store, name, lst)
                store._sorted_rebuilt(names)
                log.info("Sorted indexes rebuilt: %s",
                         ", ".join(f"{n[len('_sorted_'):]} {len(lst)}"
                                   for n, lst in built.items()))
                # Free the replaced lists one per yield (all at once was a
                # ~0.2 s deallocation stall).
                for _i in range(len(_replaced)):
                    _replaced[_i] = None
                    await asyncio.sleep(0)
            if store._dirty_sorted:
                # Every yielding pass raced a concurrent write (pathological
                # continuous writer).  One atomic synchronous rebuild under the
                # lock — no yields means no write can interleave — guarantees the
                # sorted indexes match the live set with no further race.
                store._rebuild_sorted_indexes(set(store._dirty_sorted))
                store._sorted_dirty = False
                log.warning("Sorted rebuild raced concurrent writes; did one atomic sync pass")
    finally:
        # Never leave the store stranded in batch mode (a raise, a shielded
        # cancellation, or lock acquisition failing must all clear it).
        if store._batch_depth == 0:
            store._batch_mode = False
    # A large commit (a first scan, a full rescan) grew the long-lived heap:
    # freeze it so the cyclic GC stops re-walking the library.  The collect
    # only walks objects not frozen before, so it costs in proportion to the
    # delta — once per large commit, not on every gen-2 pass afterwards.
    if store._batch_depth == 0:
        from soniqboom.core.store import FREEZE_AFTER_UPSERTS, freeze_long_lived_heap
        if getattr(store, "_upserts_since_freeze", 0) >= FREEZE_AFTER_UPSERTS:
            store._upserts_since_freeze = 0
            freeze_long_lived_heap("a large scan commit")


def _compute_duplicates_in_process(all_tracks: list[dict]) -> dict:
    """Runs in a **subprocess** (own GIL) — no event-loop starvation."""
    from soniqboom.core.duplicates import compute_duplicate_groups
    return compute_duplicate_groups(all_tracks)


async def _run_duplicate_detection_async() -> None:
    """Detect duplicates: heavy compute in a subprocess, apply in batches."""
    store = get_store()
    # Only the fields the grouping reads, copied in chunks with yields (the
    # whole-track copy stalled the loop ~0.4 s and doubled the pickle).
    snap = list(store._tracks.values())
    if not snap:
        return
    all_tracks: list[dict] = []
    for i in range(0, len(snap), 20_000):
        all_tracks.extend({k: t.get(k) for k in _DUP_INPUT_FIELDS} for t in snap[i : i + 20_000])
        await asyncio.sleep(0)
    del snap

    # Run the CPU-heavy algorithm in its own process (separate GIL)
    dup_executor = _process_pool(1)
    try:
        loop = asyncio.get_event_loop()
        annotations = await loop.run_in_executor(
            dup_executor, _compute_duplicates_in_process, all_tracks,
        )
    finally:
        _release_pool(dup_executor)

    updated = await _apply_duplicate_annotations(annotations)
    dup_count = sum(1 for a in annotations.values() if a["duplicate_group_id"] is not None)
    log.info("Duplicate detection: annotated %d tracks (%d in duplicate groups)", updated, dup_count)


async def _refresh_browse_after_commit(store, old_versions: "dict[str, dict | None]") -> None:
    """Folder-browse caches are validated by track COUNT / folder mtime, which
    an in-place re-tag doesn't move: re-shape the re-extracted rows in place,
    and patch the per-root listing where tracks came or went
    (``fstree.patch_scan_root_rows``; an out-of-step entry is dropped)."""
    try:
        from soniqboom.api import fstree
        from soniqboom.core.folder_album import _drop_browse_disk_cache, refresh_album_caches
        existing: list[str] = []
        added: dict[str, list[dict]] = {}
        removed: dict[str, set[str]] = {}
        for tid, old in old_versions.items():
            new = store._tracks.get(tid)
            if old is not None and new is not None:
                existing.append(tid)
            elif new is not None and new.get("scan_root_hash"):
                added.setdefault(new["scan_root_hash"], []).append(new)
            elif old is not None and old.get("scan_root_hash"):
                removed.setdefault(old["scan_root_hash"], set()).add(old.get("path") or "")
        from soniqboom.core.folder_album import mark_browse_disk_stale
        same_count_change = False
        changed_roots = set(added) | set(removed)
        for h in changed_roots:
            n_in, n_out = len(added.get(h, ())), len(removed.get(h, ()))
            same_count_change = same_count_change or (n_in == n_out and n_in > 0)
            if not await fstree.patch_scan_root_rows(
                    h, removed.get(h, set()), added.get(h, []),
                    len(store._tag_scan_root_hash.get(h, ()))):
                fstree.invalidate_scan_root(h)
            await asyncio.sleep(0)
        if same_count_change:
            # the on-disk copy is validated by track count only: a same-count
            # change would restore stale rows at boot
            _drop_browse_disk_cache()
        elif changed_roots:
            # count moved (the disk copy invalidates itself at boot); the
            # in-memory rows are current, so a graceful shutdown re-saves them
            mark_browse_disk_stale()
        if existing:
            await refresh_album_caches(existing)
    except Exception:                                   # noqa: BLE001 — cosmetic
        log.debug("browse cache refresh after commit failed", exc_info=True)


async def _apply_duplicate_annotations(annotations: dict) -> int:
    """Write duplicate annotations, skipping unchanged ones; returns how many
    tracks changed (their folder-browse rows are refreshed: folder dedup reads
    the flags)."""
    store = get_store()
    _touched: list[str] = []
    # Apply annotations in batches with yield points.  Two anti-bloat
    # measures, both load-bearing on a 170K-track library:
    #   1. SKIP tracks whose annotation is UNCHANGED.  This pass runs at
    #      scan-end AND at shutdown; the shutdown run almost always finds the
    #      annotations already current (the last scan set them), so without
    #      this guard it rewrites all ~170K tracks for nothing — a 170K-entry
    #      AOF that the merger (SIGKILLed at shutdown) never truncates, which
    #      then makes the *next* startup's AOF replay pathological.
    #   2. Use the BATCHED writer (one AOF record per batch, not one per
    #      track) for the tracks that genuinely changed.
    items = list(annotations.items())
    APPLY_BATCH = 200
    updated = 0
    for i in range(0, len(items), APPLY_BATCH):
        batch = items[i : i + APPLY_BATCH]
        changed: list[tuple[str, dict]] = []
        for tid, ann in batch:
            new_fields = {
                "duplicate_group_id": ann["duplicate_group_id"],
                "format_score": ann["format_score"],
                "is_duplicate_primary": ann["is_duplicate_primary"],
            }
            cur = store.get_track(tid)
            if cur is not None and all(cur.get(k) == v for k, v in new_fields.items()):
                continue  # unchanged → no store mutation, no AOF entry
            changed.append((tid, new_fields))
        if changed:
            updated += store.update_track_fields_batch(changed)
            _touched.extend(tid for tid, _f in changed)
        await asyncio.sleep(0)
    if _touched:
        try:
            from soniqboom.core.folder_album import refresh_album_caches
            await refresh_album_caches(_touched)
        except Exception:                               # noqa: BLE001 — cosmetic
            log.debug("browse cache refresh after re-grouping failed", exc_info=True)
    return updated


# ── Main scan coroutine ────────────────────────────────────────────────────────

def _is_container_name(path: str) -> bool:
    """Names whose tracks are ``<file>::<member>`` — the containers
    ``_find_audio_files`` opens (ZIP, LHA/LZH, disk images): their track ids
    can't be derived from the path alone."""
    from soniqboom.core import diskimage
    lower = path.lower()
    return lower.endswith((".zip", ".lha", ".lzh")) or diskimage.is_disk_image(lower)


class _FolderPaths:
    """Sorted paths of every folder that holds tracks, kept in step with the
    store's per-folder index (``_tag_dir_hash``) by diffing its keys on each
    use — a scoped scan finds the folders under a changed path by binary
    search instead of visiting every folder of the library.  A folder's path
    comes from one of its tracks, never from ``_hash_lookups`` (persisted
    only by the clean-shutdown snapshot, and written from a worker thread)."""

    def __init__(self) -> None:
        self.by_hash: dict[str, str] = {}
        self.sorted: list[str] = []

    def sync(self, store) -> list[str]:
        import bisect
        import os
        cur = store._tag_dir_hash
        for h in self.by_hash.keys() - cur.keys():
            p = self.by_hash.pop(h)
            i = bisect.bisect_left(self.sorted, p)
            if i < len(self.sorted) and self.sorted[i] == p:
                del self.sorted[i]
        added = cur.keys() - self.by_hash.keys()
        if added:
            tracks = store._tracks
            fresh: list[str] = []
            for h in added:
                for tid in cur[h]:
                    t = tracks.get(tid)
                    if t is not None:
                        p = os.path.dirname((t.get("path") or "").split("::", 1)[0])
                        self.by_hash[h] = p
                        fresh.append(p)
                        break
            if len(fresh) > 64:
                self.sorted.extend(fresh)
                self.sorted.sort()
            else:
                for p in fresh:
                    bisect.insort(self.sorted, p)
        return self.sorted


_folder_paths = _FolderPaths()


def _scoped_track_ids(store, root: str, scoped: frozenset[str]) -> set[str]:
    """Ids of the root's tracks that live under a SCOPED scan's changed paths
    (a changed path may be a file — incl. an archive, whose members are
    ``<archive>::<member>`` — or a folder, possibly deleted).

    Touches only the folders that can hold such tracks: a changed file's own
    folder, and every known folder at or under a changed path (found by
    binary search in ``_folder_paths``)."""
    import bisect
    import os
    from soniqboom.core.data import path_hash
    sep = os.sep
    root = root.rstrip(sep) or sep
    scoped_set = set(scoped)
    rootlen = len(root)

    def _under(p: str) -> bool:
        while len(p) > rootlen:
            if p in scoped_set:
                return True
            p = os.path.dirname(p)
        return False

    folders = _folder_paths.sync(store)
    dirs: set[str] = set()
    out: set[str] = set()
    tracks = store._tracks
    for sp in scoped_set:
        # A plain file (not an archive, not a known folder): its only track is
        # uuid5(path) — no need to visit a folder of thousands.
        if (not _is_container_name(sp)
                and path_hash(sp) not in store._tag_dir_hash):
            tid = str(uuid.uuid5(uuid.NAMESPACE_URL, sp))
            if tid in tracks:
                out.add(tid)
            pre = sp.rstrip(sep) + sep
            i = bisect.bisect_left(folders, pre)
            if not (i < len(folders) and folders[i].startswith(pre)):
                continue                        # nothing below it either
        dirs.add(os.path.dirname(sp))
        dirs.add(sp)
        pre = sp.rstrip(sep) + sep
        i = bisect.bisect_left(folders, pre)
        while i < len(folders) and folders[i].startswith(pre):
            dirs.add(folders[i])
            i += 1
    root_ids = store._tag_scan_root_hash.get(path_hash(root), ())   # live set, not a copy
    out = {tid for tid in out if tid in root_ids}
    for d in dirs:
        for tid in store._tag_dir_hash.get(path_hash(d), ()):
            if tid not in root_ids:
                continue
            t = tracks.get(tid)
            if t is None:
                continue
            b = (t.get("path") or "").split("::", 1)[0]
            if b in scoped_set or _under(os.path.dirname(b)):
                out.add(tid)
    return out


# Fields a re-extraction never produces but a stored track carries on: kept
# across a re-extract (and ignored when deciding whether the file changed).
_KEEP_ON_REEXTRACT = ("added_at", "duplicate_group_id", "format_score", "is_duplicate_primary")
_REEXTRACT_VOLATILE = frozenset(_KEEP_ON_REEXTRACT) | {"mtime", "file_size"}
# Fields a later pass writes onto a stored track — art found for it, a rendered
# / probed / HVSC duration, STIL + tune lengths, a playback-detected defect,
# the default tune, content hashes.  A fresh extract differs from them without
# the file having changed, so while the file is the same (size, and hash when
# both sides have one) they don't count as a change — unless the stored track
# has no value at all and the extract has one (a checksum an older build
# didn't compute): that is new information and is written.
_POST_SCAN_FIELDS = frozenset(("cover_art", "duration", "stil", "hvsc_lengths",
                               "subsongs", "start_subsong", "sid_md5", "file_md5"))
_SMALL_COMMIT = 2500     # commit deltas up to this size merge into the live sorted indexes
_DRILL_WRITE_CHUNK = 25  # folder-click refresh: rows per store write (a loop turn after each)
_DUP_INPUT_FIELDS = ("id", "title", "artist", "album_artist", "duration", "format",
                     "bitrate", "added_at")   # what compute_duplicate_groups reads
_DUP_INCR_MAX = 20_000   # deltas up to this size re-group duplicates incrementally


def _same_track_content(old: dict, new: dict) -> bool:
    """Would upserting the freshly-extracted ``new`` leave the stored ``old``
    unchanged apart from mtime/size?  Compared after the upsert's own
    enrichment carry-over, over the union of both key sets (a field only the
    stored track has would be dropped by the upsert — that is a change; a
    field the stored track predates, extracted empty, is not)."""
    from soniqboom.core.store import _carry_enrichment
    cand = dict(new)
    _carry_enrichment(old, cand)
    same_file = (old.get("file_size") == new.get("file_size")
                 and not any(old.get(h) and new.get(h) and old[h] != new[h]
                             for h in ("file_md5", "sid_md5")))
    for k in cand.keys() | old.keys():
        if k in _REEXTRACT_VOLATILE or old.get(k) == cand.get(k):
            continue
        if k not in old and cand[k] in (None, ""):
            continue
        if same_file and k in _POST_SCAN_FIELDS and old.get(k):
            continue
        return False
    return True


def _in_bucket0(t: dict) -> bool:
    """Does ``t`` fall in duplicate-duration bucket 0 (under 5 s, unknown, or
    non-finite — what ``duplicates._duration_bucket`` maps to 0)?  A numeric
    test: normalising every track's title to find out cost ~0.7 s."""
    try:
        d = float(t.get("duration", 0) or 0)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(d) or d < 5


async def _run_duplicate_detection_incremental(delta: "dict[str, dict | None]") -> bool:
    """Re-group only the duplicate groups a small commit can have touched.

    A group key is title | artist | 5-second duration bucket, so the only
    groups whose membership can change are the old and new keys of the
    changed / removed tracks.  Their members are found in the maintained
    duration index (one bucket each; tracks under 5 s or of unknown length —
    not in that index — by one pass over the library) and re-annotated with
    the same ``compute_duplicate_groups`` the full pass uses.  Returns False
    (caller runs the full pass) when the duration index is mid-rebuild."""
    import bisect
    from soniqboom.core.duplicates import (compute_duplicate_groups, group_key_for,
                                           key_duration_bucket)
    store = get_store()
    if store._batch_mode or "_sorted_duration" in store._dirty_sorted:
        return False
    keys: set[str] = set()
    untitled: set[str] = set()
    for tid, old in delta.items():
        if old is not None:
            k = group_key_for(old)
            if k:
                keys.add(k)
        cur = store._tracks.get(tid)
        if cur is not None:
            k = group_key_for(cur)
            if k:
                keys.add(k)
            else:
                untitled.add(tid)
    members: dict[str, dict] = {}
    seen = 0
    for b in sorted({key_duration_bucket(k) for k in keys}):
        if b <= 0:
            snap = list(store._tracks.values())
            cands: list = []
            for i in range(0, len(snap), 20_000):
                cands.extend(t for t in snap[i : i + 20_000] if _in_bucket0(t))
                await asyncio.sleep(0)
        else:
            lst = store._sorted_duration
            lo = bisect.bisect_left(lst, (5 * b,))
            hi = bisect.bisect_left(lst, (5 * (b + 1),))
            cands = [store._tracks.get(tid) for _d, tid in lst[lo:hi]]
        for t in cands:
            if t is None:
                continue
            seen += 1
            if seen % 1000 == 0:
                await asyncio.sleep(0)
            if group_key_for(t) in keys:
                members[t["id"]] = t
    for tid in untitled:
        t = store._tracks.get(tid)
        if t is not None:
            members[tid] = t
    annotations = compute_duplicate_groups(list(members.values()))
    updated = await _apply_duplicate_annotations(annotations)
    log.info("Duplicate detection (incremental): %d group key(s), %d candidate(s), "
             "%d track(s) re-grouped, %d annotation(s) changed",
             len(keys), seen, len(members), updated)
    return True


_FP_KEY = "archive_listing_fp:"
_ARCHIVE_LISTING_VERSION = 1      # bump when archive discovery rules change


def _listing_fingerprint() -> str:
    """What decides which archive members discovery lists: the supported
    formats, the installed uade's name tokens, the owned-extension table and
    the discovery rules' version."""
    import hashlib
    from soniqboom.core import metadata as _md, uade_formats as _uf
    try:
        tokens = sorted(_uf.player_map())
    except Exception:                                   # noqa: BLE001
        tokens = []
    raw = repr((_ARCHIVE_LISTING_VERSION, sorted(_md.SUPPORTED_EXTENSIONS), tokens,
                sorted(_uf._SUFFIX_OWNED_ELSEWHERE)))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


async def _known_archive_members(store, roots: list[str]) -> "dict[str, tuple[float | None, list[str]]]":
    """archive path → (the mtime its indexed members carry — None when they
    disagree —, their virtual paths), for the archives under ``roots``.
    Built on the loop in chunks (store access), from the per-root id sets."""
    from soniqboom.core.data import path_hash
    out: dict[str, list] = {}
    for root in roots:
        ids = list(store._tag_scan_root_hash.get(path_hash(root), ()))
        for i in range(0, len(ids), 5_000):
            for tid in ids[i : i + 5_000]:
                t = store._tracks.get(tid)
                if t is None:
                    continue
                p = t.get("path") or ""
                if "::" not in p:
                    continue
                outer = p.split("::", 1)[0]
                m = t.get("mtime")
                e = out.get(outer)
                if e is None:
                    out[outer] = [m, [p]]
                else:
                    if e[0] is None or m is None or abs(e[0] - m) >= 1.0:
                        e[0] = None
                    e[1].append(p)
            await asyncio.sleep(0)
    return {k: (v[0], v[1]) for k, v in out.items()}


def _root_is_live(root: str) -> bool:
    """A scoped scan may prune tracks under a changed path only while the root
    itself is demonstrably mounted (readable and listing something) — the same
    guard as the full scan's "never prune a zero listing" rule, applied to the
    root rather than to the (possibly genuinely emptied) changed folder."""
    import os
    try:
        with os.scandir(root) as it:
            return next(it, None) is not None
    except OSError:
        return False


async def _run_scan(
    directories: list[str],
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
    scope: "dict[str, frozenset[str]] | None" = None,
    light: bool = False,
) -> bool:
    """Scan ``directories``; returns whether the library changed (tracks
    added, updated or removed).  ``scope`` (root → changed paths, from the
    folder watcher) limits a root to those files/folders: discovery, the
    unchanged-file check and orphan pruning then cost what changed, not the
    whole library."""
    global _progress, _scan_count, _prog_batch_entered
    _changed = False
    _hvsc_new = False          # HVSC auto-configured by THIS scan → apply even if nothing changed
    # {track id: its stored version before this scan (None = new)} for every
    # track the commit added, changed or removed — lets duplicate detection and
    # HVSC re-process just those.  None = unknown (first scan / progressive).
    _dup_delta: "dict[str, dict | None] | None" = None

    loop = asyncio.get_event_loop()

    _registered0 = set(get_store()._scan_dirs)
    if scope or light:
        # A watcher / automatic scan of a root the user removed meanwhile
        # (queued before the removal) must not re-register and re-index it.
        directories = [d for d in directories if str(Path(d).resolve()) in _registered0]
        if scope:
            scope = {r: v for r, v in scope.items() if r in _registered0}

    def _removed_now(root: str) -> bool:
        """Registered when this scan started, removed by the user since."""
        return root in _registered0 and root not in get_store()._scan_dirs

    def _drop_removed_roots(chunk: list) -> list:
        """A commit chunk minus the tracks of roots removed meanwhile (the
        removal's purge may already have run)."""
        gone = {path_hash(r) for r in _registered0 if r not in get_store()._scan_dirs}
        if not gone:
            return chunk
        return [t for t in chunk if t.get("scan_root_hash") not in gone]

    # ── Discover files ────────────────────────────────────────────────────────
    from soniqboom.config import settings as _settings
    _known_archives = None
    _fp = _listing_fingerprint()
    _full_roots = [] if scope else [str(Path(x).resolve()) for x in directories]
    _light: list[str] = []
    if light and _full_roots:
        # Only roots last enumerated in full under the SAME discovery rules
        # (supported formats, uade tokens) may be listed from the store — an
        # upgrade that learned a format re-enumerates once, automatically.
        _light = [d for d in _full_roots
                  if get_store().get_config(_FP_KEY + path_hash(d)) == _fp]
        if _light:
            _known_archives = await _known_archive_members(get_store(), _light)
    _failed_archives: set[str] = set()
    dir_files, walk_errors = await loop.run_in_executor(
        None, _find_audio_files, directories, _settings.scan_zips, scope, _known_archives,
        _failed_archives,
    )
    # Roots enumerated in full now with no read error: their fingerprint is
    # recorded once this scan has committed (an interrupted scan leaves the
    # old one → the next automatic scan enumerates again).
    _protecting = _archives_with_members(
        get_store(), [a for a in _failed_archives if "::" not in a])
    _images = sorted(a for a in _protecting if diskimage.is_disk_image(a.lower()))
    if _images:
        log.warning("%d unreadable disk image(s) keep their indexed tracks: %s%s",
                    len(_images), ", ".join(_images[:5]), " …" if len(_images) > 5 else "")
    _fp_record = [d for d in _full_roots
                  if d in dir_files and d not in walk_errors and d not in _light
                  and not any(a.startswith(d.rstrip(os.sep) + os.sep) for a in _protecting)]
    total = sum(len(v) for v in dir_files.values())

    # ProcessPoolExecutor: each worker has its own GIL so metadata
    # extraction (zipfile, mutagen) never competes with the event loop.
    # Sized to the work: a watcher scan of one file forks one worker, not
    # SCAN_WORKERS copies of the server (workers start on first submit).
    executor = _process_pool(max(1, min(SCAN_WORKERS, total)))

    # Additive progress: when a remote scan is already running, add to the
    # existing total instead of overwriting it.
    if _scan_count > 0 and _progress.running:
        _progress.total += total
    else:
        # A scoped (watcher) scan keeps the previous scan's summary.
        _progress = ScanProgress(total=total, running=True,
                                 last_plan=dict(_progress.last_plan) if scope else {})
    _scan_count += 1
    # Remember the resolved dirs this scan covers — survives completion (unlike
    # ``current_dirs``, cleared the instant the scan ends) so the scan-complete
    # WS event lets the frontend refresh the OPEN folder in place rather than
    # resetting the whole tree to root.
    for _d in directories:
        try:
            _rd = str(Path(_d).resolve())
        except OSError:
            _rd = str(_d)
        if _rd not in _progress.last_dirs:
            _progress.last_dirs.append(_rd)
    log.info("Scan started: %d files across %d root(s)", total, len(dir_files))

    dir_counts:    dict[str, int] = defaultdict(int)
    all_track_ids: list[str]      = []

    # ── Phase 1: parallel metadata + chunked store writes ─────────────────────
    store = get_store()
    # Non-blocking commit strategy: when the library is already populated (a
    # re-scan / re-index), DEFER the store writes — accumulate the scan's delta
    # (new/changed tracks + orphan ids) in memory and apply it to the live store
    # only at the very end, in one batch.  The live store is left completely
    # untouched for the whole (multi-minute) extract phase, so browse / search /
    # playback never thrash the live read caches while files are being processed.
    #
    # Crucially the delta is APPLIED to the live store via its own
    # ``upsert_tracks_batch`` / ``delete_track_ids`` (which touch only the scan's
    # own track ids), NOT swapped in over a stale snapshot — so any concurrent
    # add / delete / edit to OTHER tracks during the extract window (remote
    # freshness scan, drill-down refresh, single-track edit) is preserved, and
    # memory stays consistent with the AOF.  For the first-ever scan (empty
    # library) there's nothing to protect, so keep the progressive path (which
    # also gives progressive UX and avoids one giant end-of-scan AOF write).
    _deferred: bool = len(store._tracks) > 0
    _pending_tracks: list[dict] = []   # accumulated new/changed track dicts
    _pending_deleted: set[str]  = set()  # accumulated orphan ids to prune
    if not _deferred:
        store.enter_batch_mode()   # defer O(n) sorted-list rebuilds during scan
        # This enter has no try/finally over the Phase-1 span, so a mid-Phase-1
        # raise would leak the batch level — flag it for the queue crash handler
        # to unwind (see _drain_scan_queue).
        _prog_batch_entered = True

    for scan_root, files in dir_files.items():
        if _removed_now(scan_root):
            log.info("Scan: %s was removed while scanning — skipped", scan_root)
            continue
        await upsert_scan_dir(scan_root)

        # HVSC auto-detect: if this root has SID files and no HVSC DOCUMENTS
        # path is configured yet, look for one up the tree and auto-configure
        # (self-guards against overriding a user-set path).  Probe at most once
        # per scan_root per process, and never let detection break the scan.
        if (getattr(_settings, "hvsc_autodetect", True)
                and scan_root not in _hvsc_probed_roots):
            from soniqboom.core.hvsc import get_hvsc as _get_hvsc
            if not _get_hvsc().is_configured():
                try:
                    sid_dirs = {
                        str(Path(str(p).split("::")[0]).parent)
                        for p in files
                        if str(p).split("::")[0].lower().endswith(_SID_DETECT_EXTS)
                    }
                    if sid_dirs:
                        docs = await loop.run_in_executor(
                            None, _detect_hvsc_docs_local, sid_dirs,
                        )
                        # Mark probed only AFTER detection actually ran — a
                        # transient failure (raises) is caught below and leaves
                        # the root un-probed so the next scan retries.
                        _hvsc_probed_roots.add(scan_root)
                        if docs and await _apply_hvsc_autoconfig(docs):
                            _hvsc_new = True
                except Exception:
                    log.debug("HVSC local auto-detect failed", exc_info=True)

        def _parent_dir(fp: Path) -> str:
            return os.path.dirname(str(fp).split('::', 1)[0])

        def _unique_dirs(fl: list, root: str) -> list[str]:
            return list({os.path.dirname(str(p).split('::', 1)[0]) for p in fl} | {root})

        unique_dirs = (_unique_dirs(files, scan_root) if len(files) < 20_000
                       else await asyncio.to_thread(_unique_dirs, files, scan_root))
        hash_map = await store_hash_lookups_batch(unique_dirs)

        # ── Incremental scan: skip unchanged files ───────────────────────────
        # This involves stat() calls and uuid5 hashing for every file, which
        # is CPU-intensive for 100K+ files (181K uuid5 + 181K os.stat).
        # Running in a ThreadPoolExecutor still blocks the event loop via GIL
        # contention.  Instead we run in a *ProcessPoolExecutor* (own GIL) and
        # pass only a lightweight mtime/size map (10K entries) instead of the
        # full store, keeping pickle overhead trivial.
        #
        # OPTIMISATION: If the store has NO tracks for this scan root, skip
        # the incremental check entirely — every file needs scanning anyway,
        # and the stat() calls for 60-120K files over a network mount cost
        # 60-120+ seconds with zero benefit.

        _scoped = scope.get(scan_root) if scope else None
        if _scoped is not None:
            existing_ids = _scoped_track_ids(store, scan_root, _scoped)
            _scoped_existing = set(existing_ids)
        else:
            existing_ids = await get_track_ids_for_scan_root(scan_root)

        # Convert Path objects to strings for pickling across process boundary
        files_strs = ([str(p) for p in files] if len(files) < 20_000
                      else await asyncio.to_thread(lambda fl=files: [str(p) for p in fl]))

        # Only run incremental check if a meaningful fraction of files might
        # be unchanged.  With 1 existing track out of 60K files, stat-checking
        # all 60K (60+ seconds on a network mount) saves at most 1 extraction.
        # (A small root is always checked: skipping re-extracted every file
        # on every rescan, stamped them new and ran the post-scan passes.)
        _run_incr_check = bool(existing_ids) and len(existing_ids) > len(files) // 50
        if _scoped is not None and existing_ids:
            # A scoped scan touches few files: always compare mtime/size so a
            # touched-but-unchanged file isn't re-extracted and re-written.
            _run_incr_check = True
        if _run_incr_check:
            # Build a small {track_id: (mtime, file_size)} lookup — only
            # existing tracks matter, so bounded by store size, not file count.
            mtime_size_map: dict[str, tuple[float | None, int | None]] = {}
            _eids = list(existing_ids)
            for _ci in range(0, len(_eids), 20_000):
                for tid in _eids[_ci : _ci + 20_000]:
                    trk = store._tracks.get(tid)
                    if trk:
                        mtime_size_map[tid] = (trk.get("mtime"), trk.get("file_size"))
                await asyncio.sleep(0)

            log.info(
                "Incremental check for %s: stat-checking %d files (%d existing tracks) …",
                scan_root, len(files_strs), len(mtime_size_map),
            )
            _progress.current_file = f"Checking {len(files_strs):,} files for changes…"
            if on_progress:
                await on_progress(_progress)

            # Use multiple workers for the stat check to saturate network I/O
            INCR_WORKERS = min(4, max(1, len(files_strs) // 5000))
            # A small (scoped) check runs in a thread: spawning a process pool
            # costs more than stat-ing a handful of files.
            _small = len(files_strs) <= 2000
            incr_executor = None if _small else _process_pool(INCR_WORKERS)
            try:
                t0 = time.time()
                if _small:
                    fresh_strs, tid_map_strs = await asyncio.to_thread(
                        _compute_incremental, files_strs, mtime_size_map,
                    )
                elif INCR_WORKERS == 1:
                    fresh_strs, tid_map_strs = await loop.run_in_executor(
                        incr_executor, _compute_incremental, files_strs, mtime_size_map,
                    )
                else:
                    # Split file list into chunks and run in parallel
                    chunk_size = math.ceil(len(files_strs) / INCR_WORKERS)
                    chunks = [
                        files_strs[i : i + chunk_size]
                        for i in range(0, len(files_strs), chunk_size)
                    ]
                    chunk_futs = [
                        loop.run_in_executor(
                            incr_executor, _compute_incremental, chunk, mtime_size_map,
                        )
                        for chunk in chunks
                    ]
                    results = await asyncio.gather(*chunk_futs)
                    # Merge results from all chunks
                    fresh_strs: set[str] = set()
                    tid_map_strs: dict[str, str] = {}
                    for chunk_fresh, chunk_tids in results:
                        fresh_strs |= chunk_fresh
                        tid_map_strs.update(chunk_tids)
                log.info(
                    "Incremental check done for %s in %.1fs: %d fresh, %d total",
                    scan_root, time.time() - t0, len(fresh_strs), len(files_strs),
                )
            finally:
                _release_pool(incr_executor)

            # Map string results back to Path keys (a thread for a big root:
            # these per-file passes were seconds of loop time at 170K files)
            def _map_back(files=files, fresh_strs=fresh_strs, tid_map_strs=tid_map_strs):
                str_to_path = {str(p): p for p in files}
                fresh_paths = {str_to_path[s] for s in fresh_strs if s in str_to_path}
                tids = {str_to_path[s]: tid for s, tid in tid_map_strs.items()
                        if s in str_to_path}
                return fresh_paths, tids, [p for p in files if p not in fresh_paths]
            if len(files) < 20_000:
                fresh_paths, track_ids_for_files, files_to_scan = _map_back()
            else:
                fresh_paths, track_ids_for_files, files_to_scan = await asyncio.to_thread(_map_back)
            skipped = len(files) - len(files_to_scan)
            if skipped:
                log.info("Incremental scan: skipping %d unchanged files", skipped)
                _progress.processed += skipped
                # The bulk-add can push ``processed`` past one or more
                # PROGRESS_EVERY multiples without firing the per-file
                # broadcast.  Push one update so the user sees the jump
                # rather than the badge sitting at the prior value
                # while ``processed`` silently advances.
                if on_progress:
                    await on_progress(_progress)
        else:
            # Too few existing tracks to justify stat-checking all files
            log.info(
                "Skipping incremental check for %s: %d existing tracks vs %d files — scanning all",
                scan_root, len(existing_ids), len(files_strs),
            )
            files_to_scan = list(files)
            skipped = 0
            # Still map every discovered file to its uuid5 id so stale cleanup
            # below can prune orphans (deleted files) — previously this was left
            # empty, so a small scan root (≤ the incr-check threshold) NEVER had
            # its deleted files pruned.  Cheap: this branch only runs when the
            # root is small enough that the incremental check was skipped.
            track_ids_for_files = {
                p: str(uuid.uuid5(uuid.NAMESPACE_URL, str(p))) for p in files
            }

        log.info(
            "Extraction starting for %s: %d files to scan, %d skipped",
            scan_root, len(files_to_scan), skipped,
        )

        # ── Sliding window executor ──────────────────────────────────────────
        # Submit only INFLIGHT tasks at a time so asyncio.wait() operates on
        # a small set (~200) instead of all 170K+ futures.  This keeps each
        # wait() call at O(INFLIGHT) rather than O(total_files), preventing
        # the event loop from stalling for 700 ms+ per call.

        track_buffer: list[Track]       = []
        art_buffer:   dict[str, str]    = {}
        sm_thumbs:    dict[str, bytes]  = {}
        lg_thumbs:    dict[str, bytes]  = {}

        async def _flush_buffer():
            """Write buffered tracks in sub-batches, yielding between chunks."""
            nonlocal track_buffer, art_buffer, sm_thumbs, lg_thumbs
            if not track_buffer:
                return
            n = len(track_buffer)
            t0 = time.time()
            try:
                for i in range(0, n, WRITE_CHUNK):
                    chunk = track_buffer[i : i + WRITE_CHUNK]
                    if _deferred:
                        # Accumulate the delta off the live store — mirror the
                        # exact model_dump + zero-embedding strip that
                        # upsert_tracks_batch does, so the deferred apply is
                        # byte-identical to a progressive write.
                        for _t in chunk:
                            _td = _t.model_dump()
                            _emb = _td.get("embedding")
                            if not _emb or all(v == 0.0 for v in _emb):
                                _td.pop("embedding", None)
                            _pending_tracks.append(_td)
                    else:
                        await upsert_tracks_batch(chunk)
                    await asyncio.sleep(0)
                await store_full_art_batch(art_buffer)
                await store_thumbs_batch(sm_thumbs, lg_thumbs)
                log.debug("Flushed %d tracks in %.2fs", n, time.time() - t0)
            except Exception as exc:
                log.error("Flush error after %.2fs: %s", time.time() - t0, exc, exc_info=True)
            track_buffer = []
            art_buffer   = {}
            sm_thumbs    = {}
            lg_thumbs    = {}

        async def _handle_result(fut):
            """Process one completed extraction future; returns its result
            (the metadata, or the error text)."""
            nonlocal track_buffer
            path, result, sm_thumb, lg_thumb = await fut

            if isinstance(result, str):
                log.error("Metadata error %s: %s", path, result)
                _progress.errors += 1
            else:
                meta: TrackMeta = result
                track, raw_art = _build_track(
                    meta, scan_root, _parent_dir(path), hash_map,
                )
                if track:
                    track_buffer.append(track)
                    if raw_art:
                        art_buffer[track.id] = raw_art
                    if sm_thumb:
                        sm_thumbs[track.id] = sm_thumb
                    if lg_thumb:
                        lg_thumbs[track.id] = lg_thumb
                    dir_counts[scan_root] += 1
                    all_track_ids.append(meta.id)
                    if len(track_buffer) >= WRITE_BATCH:
                        await _flush_buffer()
                else:
                    _progress.errors += 1

            _progress.processed    += 1
            _progress.current_file  = path.name

            if on_progress and (
                _progress.processed % PROGRESS_EVERY == 0
                or _progress.processed == total
            ):
                await on_progress(_progress)
            return result

        file_iter = iter(files_to_scan)
        # future → (file, or None for a pool's canary; the pool generation)
        active: dict[asyncio.Future, tuple[Path | None, int]] = {}
        cfs: dict[asyncio.Future, object] = {}   # … its pool (concurrent) future
        # When the pool dies — its workers crashed (at start, or on a file
        # that kills them) or hung — the files it was extracting become
        # SUSPECTS (after a hang: those a worker ran; the ones only queued
        # behind them go back to the normal stream).  Every new pool first
        # runs a no-op canary; once that works, each suspect runs ALONE in
        # it, so one that kills or hangs its worker there is the culprit —
        # given up as an error — and the others and the rest of the root go
        # on.  Failures that reach us from an already-replaced pool (one
        # ``asyncio.wait`` round late) are requeued, never counted against
        # the new pool.  Three pools in a row that die without settling a
        # single file (workers that crash at start whatever the file, a
        # forkserver that can't be reached, a pool that starts but refuses
        # every file) give the rest of the root up as errors (Re-Index to
        # retry) — never silently, never an endless run of new pools, and no
        # executor error escapes (it would bypass this root's flush +
        # cleanup and leave the scan a zombie).  A suspect that hangs alone
        # may be the file — or the SOURCE stopped answering (a hung share):
        # the hung pool is killed, then the root, a file already read from
        # it and one not read yet are tried (``_source_down``); if they
        # don't answer within a grace period the rest of the root is given
        # up the same way, instead of 90 s per remaining file — while they
        # answer, only the hung file is; after ``_MAX_ARCHIVE_HANGS`` members
        # of one archive in a row hung alone, the rest of that archive's
        # members are skipped (not indexed, Re-Index to retry).  So is the rest of the root when
        # ``_MAX_SOURCE_ERRORS`` files in a row failed with a source error
        # (EIO, a timeout …) and the source doesn't answer.  A pool that
        # was killed is never touched again (``_pool_stopped``: its lock
        # may be held for good); the next root starts a new one.
        suspects: deque[Path] = deque()
        requeue: deque[Path] = deque()   # queued behind a hang: run again, normally
        gen = 0                 # the current pool's generation
        pool_ok = False         # the current pool's canary (or any file) worked
        settled = False         # the current pool settled a file (indexed, failed, or its culprit found)
        pool_dead = False       # a new pool is needed before the next submit
        pool_gone = False       # gave up: no more pools for this root
        bad_pools = 0           # pools in a row that died without settling a file
        source_errors = 0       # files in a row that failed with a source error
        stalled = False         # the source stopped answering — give the root up
        last_ok: Path | None = None     # a file of this root read fine (probed when one hangs)
        solo_path: Path | None = None   # the suspect running alone in this pool
        hang_run: list[str] = []        # members of one file on disk in a row that hung alone (nothing extracted since)
        skipped_archives: dict[str, int] = {}   # archive whose other members are skipped → how many

        async def _skip_member(path: Path, archive: str) -> None:
            skipped_archives[archive] += 1
            _progress.errors += 1
            _progress.processed += 1
            await _tick()

        async def _tick() -> None:
            if on_progress and (_progress.processed % PROGRESS_EVERY == 0
                                or _progress.processed == total):
                await on_progress(_progress)

        async def _fail(path: Path, why: str) -> None:
            log.error("Worker error for %s: %s", path, why)
            _progress.errors    += 1
            _progress.processed += 1
            await _tick()

        def _live() -> int:
            """Files (not canaries) in flight in the current pool."""
            return sum(1 for p, g in active.values() if g == gen and p is not None)

        def _submit(fn, *args) -> asyncio.Future:
            if _pool_stopped(executor):
                raise RuntimeError("the extraction pool was stopped")
            cf = executor.submit(fn, *args)
            fut = asyncio.wrap_future(cf, loop=loop)
            cfs[fut] = cf
            return fut

        def _submit_canary() -> None:
            active[_submit(_pool_canary)] = (None, gen)

        async def _source_down(avoid: Path | None = None) -> bool:
            """Whether the source stopped answering: the root's listing, a
            file already read from it and one not read yet (``_unread_file``;
            never the one that hung, ``avoid``) are tried — ``_PROBE_S``
            together — again with growing pauses for up to ``_STALL_GRACE_S``
            (a share that wakes up or reconnects) before the answer is
            "down".  A stop answers "down" at once."""
            fresh = _unread_file(requeue, file_iter, (avoid, last_ok))
            waited, pause = 0.0, _STALL_PAUSE_S
            while True:
                if await asyncio.to_thread(_source_answers, last_ok, _PROBE_S, scan_root,
                                           fresh=fresh):
                    return False
                if _pools_closed or waited >= _STALL_GRACE_S:
                    return True
                log.warning("%s doesn't answer — waiting for it (%.0f s so far)", scan_root, waited)
                _progress.current_file = f"Waiting for {Path(scan_root).name or scan_root} to answer…"
                if on_progress:
                    await on_progress(_progress)
                await asyncio.sleep(pause)
                waited += pause + _PROBE_S
                pause = min(pause * 2, 60.0)

        try:
            if executor is None or _pool_stopped(executor):
                # An earlier root gave its pool up (or a stop killed it): a
                # new one, never a submit to the old one.
                executor = None
                executor = _process_pool(max(1, min(SCAN_WORKERS, total)))
            _submit_canary()
        except (BrokenExecutor, RuntimeError, OSError):
            pool_dead = True

        while True:
            if pool_dead and not pool_gone:
                bad_pools = 0 if settled else bad_pools + 1
                await _kill_pool_async(executor)
                new_pool = None
                if bad_pools < 3 and not stalled:
                    try:
                        new_pool = _process_pool(max(1, min(SCAN_WORKERS, total)))
                    except Exception as exc:                    # noqa: BLE001
                        log.warning("No new extraction pool for %s: %s", scan_root, exc)
                if new_pool is None:
                    pool_gone = True
                    lost = len(suspects) + len(requeue) + sum(1 for _ in file_iter)
                    suspects.clear()
                    requeue.clear()
                    _progress.errors += lost
                    _progress.processed += lost
                    if _pools_closed:
                        log.info("Server stopping — %d file(s) of %s not indexed "
                                 "this time", lost, scan_root)
                    elif stalled:
                        log.error("Extraction stalled on %s — %d file(s) not indexed "
                                  "(Re-Index to retry)", scan_root, lost)
                    else:
                        log.error("Extraction pool died again while scanning %s — "
                                  "%d file(s) not indexed (Re-Index to retry)",
                                  scan_root, lost)
                    await _tick()
                else:
                    log.warning("Extraction pool died while scanning %s — "
                                "starting a new one", scan_root)
                    executor = new_pool
                    gen += 1
                    pool_ok = settled = pool_dead = False
                    solo_path = None
                    try:
                        _submit_canary()
                    except (BrokenExecutor, RuntimeError, OSError):
                        pool_dead = True
            if not pool_gone and not pool_dead:
                path = None
                try:
                    if suspects:
                        if pool_ok and not _live():     # a suspect runs alone
                            path = suspects.popleft()
                            active[_submit(_extract_one, path)] = (path, gen)
                            solo_path = path
                    elif pool_ok or gen == 0:
                        while _live() < INFLIGHT:
                            path = requeue.popleft() if requeue else next(file_iter, None)
                            if path is None:
                                break
                            if skipped_archives and (box := _in_archives(path, skipped_archives)):
                                await _skip_member(path, box)
                                path = None
                                continue
                            active[_submit(_extract_one, path)] = (path, gen)
                except (BrokenExecutor, RuntimeError, OSError) as exc:
                    # The pool can't take work: dead, or its forkserver gone.
                    log.debug("Extraction pool refused work: %s", exc)
                    if path is not None:
                        suspects.appendleft(path)
                    pool_dead = True
            if not active:
                if pool_dead and not pool_gone:
                    continue                    # a new pool, then submit again
                break

            done, _ = await asyncio.wait(
                active.keys(), return_when=asyncio.FIRST_COMPLETED,
                timeout=_EXTRACT_STUCK_S,
            )
            if not done:
                # Nothing finished for 90 s: the workers hang.  A suspect
                # running alone is the culprit; otherwise the files a worker
                # ran run again alone in a new pool, and the ones only queued
                # behind them go back to the normal stream.
                stuck = [(f, p, g) for f, (p, g) in active.items() if p is not None]
                solo_stuck = (pool_ok and solo_path is not None
                              and [p for _f, p, g in stuck if g == gen] == [solo_path])
                ran = [(p, g) for f, p, g in stuck if f in cfs and cfs[f].running()]
                if ran:
                    queued = [p for f, p, g in stuck if not (f in cfs and cfs[f].running())]
                else:                           # can't tell: every one is a suspect
                    ran, queued = [(p, g) for _f, p, g in stuck], []
                for fut in list(active):
                    fut.cancel()
                active.clear()
                cfs.clear()
                for p, g in ran:
                    if solo_stuck and g == gen:
                        await _fail(p, "extraction hung (> 90 s) — skipped")
                        settled = True
                    else:
                        suspects.append(p)
                requeue.extend(queued)
                pool_dead = True
                if solo_stuck:
                    await _kill_pool_async(executor)    # the hung worker goes before the source is probed
                    if await _source_down(avoid=solo_path):
                        stalled = True
                    else:
                        hung = str(solo_path)
                        outer = hung.split("::", 1)[0] if "::" in hung else None
                        if outer is None:
                            hang_run = []                   # a plain file never counts
                        elif hang_run and hang_run[0].split("::", 1)[0] == outer:
                            hang_run.append(hung)
                        else:
                            hang_run = [hung]
                        if len(hang_run) >= _MAX_ARCHIVE_HANGS:
                            box = _common_archive(hang_run)
                            skipped_archives.setdefault(box, 0)
                            for p in [p for p in suspects if _in_archives(p, (box,))]:
                                suspects.remove(p)
                                await _skip_member(p, box)
                            hang_run = []
                else:
                    log.error("Extraction timed out: %d file(s) ran — trying them one at a "
                              "time: %s", len(ran), [str(p) for p, _ in ran[:5]])
                continue

            for fut in done:
                fut_path, fut_gen = active.pop(fut)
                cfs.pop(fut, None)
                if fut_path is not None and fut_path == solo_path and fut_gen == gen:
                    solo_path = None
                    was_solo = True
                else:
                    was_solo = False
                if fut.cancelled():                 # its pool was killed
                    if fut_path is not None:
                        suspects.append(fut_path)
                    continue
                if fut_path is None:                # a canary
                    if fut.exception() is None:
                        if fut_gen == gen:
                            pool_ok = True
                    elif fut_gen == gen:
                        pool_dead = True
                    continue
                try:
                    res = await _handle_result(fut)
                    if fut_gen == gen:
                        pool_ok = settled = True
                    if isinstance(res, str) and _is_source_error(res):
                        source_errors += 1
                    else:
                        source_errors = 0                   # the source answers
                        if not isinstance(res, str):
                            last_ok = fut_path
                            hang_run = []
                except BrokenExecutor as exc:
                    if fut_gen != gen:
                        suspects.append(fut_path)   # late news from a replaced pool
                        continue
                    pool_dead = True
                    if pool_ok and was_solo:
                        # Alone in a pool that worked: it killed its worker.
                        await _fail(fut_path, f"{exc} — the file kills its worker; skipped")
                        settled = True
                    else:
                        suspects.append(fut_path)
                except Exception as exc:
                    # ``_handle_result`` broadcasts on the %PROGRESS_EVERY
                    # /``== total`` condition; the error path does the same
                    # (``_fail``), or the badge stalls at e.g. "99% (X/Y)".
                    await _fail(fut_path, str(exc))
                    if fut_gen == gen:
                        settled = True
            if source_errors >= _MAX_SOURCE_ERRORS and not stalled and not pool_gone:
                if await _source_down():
                    stalled = pool_dead = True  # the rest of the root is given up
                else:
                    source_errors = 0           # the source answers: these files fail

            # Honour the pause flag BEFORE submitting new work — paused
            # scans drain the in-flight window naturally and then idle
            # at this gate until the user clicks Resume.  In-flight
            # futures keep running; the gate only blocks new submissions.
            await _await_resume()
            await asyncio.sleep(0)  # yield to event loop every iteration

        for box, n in skipped_archives.items():
            log.error("Extraction hung on %d members of %s in a row — its other %d member(s) "
                      "were skipped, not indexed (Re-Index to retry)", _MAX_ARCHIVE_HANGS, box, n)
        if suspects or requeue:
            # Failures that reached us after the root was given up (late news
            # from a replaced pool): not indexed either — counted, not lost.
            more = len(suspects) + len(requeue)
            log.error("%d more file(s) of %s not indexed (Re-Index to retry)", more, scan_root)
            _progress.errors += more
            _progress.processed += more
            suspects.clear()
            requeue.clear()
            await _tick()

        # Flush any remaining tracks for this root
        await _flush_buffer()

        # ── Stale track cleanup ──────────────────────────────────────────────
        # ``track_ids_for_files`` maps EVERY discovered file to its uuid5 id (in
        # both the incremental and skip-incremental branches), so ``expected_ids``
        # is the authoritative set of tracks that still have a file under this
        # root.  Anything the store holds under the root that ISN'T in that set is
        # an orphan (its file was deleted) and gets pruned — the fix for small
        # roots whose incremental check was skipped, which the old
        # ``_run_incr_check and track_ids_for_files`` guard silently dropped,
        # leaving deleted files in the index forever.
        #
        # SAFETY — three layers, so a transient/degraded read never wipes tracks:
        #   1. An inaccessible root fails ``is_dir()`` at discovery and never
        #      reaches this loop (absent from ``dir_files``).
        #   2. A root whose walk hit a read error (``walk_errors``) has a PARTIAL
        #      listing → skip pruning entirely.
        #   3. A root that lists ZERO files is ambiguous — genuinely emptied vs a
        #      silent mount failure (SMB/NFS stale handle reads empty without
        #      raising) vs a healthy archive-only root after "scan zips" is turned
        #      off — so never mass-delete on that signal, at ANY count.  Only a
        #      readable root that STILL lists files can have its orphans pruned;
        #      its live siblings prove the enumeration is real.  A genuinely
        #      emptied root is cleared explicitly via "remove folder".
        expected_ids = set(track_ids_for_files.values())
        if _scoped is not None:
            # The deferred store is untouched since the lookup above; a
            # progressive write only ADDS this scan's own (expected) ids.
            existing_ids = _scoped_existing
        else:
            existing_ids = await get_track_ids_for_scan_root(scan_root)
        orphan_ids = existing_ids - expected_ids
        if orphan_ids and _failed_archives:
            # An archive that exists but couldn't be read this time: its
            # indexed members are not deletions (a later readable scan decides).
            orphan_ids = {tid for tid in orphan_ids
                          if not _in_failed_archive((store._tracks.get(tid) or {}).get("path") or "",
                                                    _failed_archives)}
        if _scoped is not None and orphan_ids and scan_root not in walk_errors and not stalled:
            # Scoped: the changed file / folder is gone (or lost files) while
            # the root is still mounted → a genuine deletion.  Never on a root
            # that reads empty (a dropped mount looks exactly like that).
            if await asyncio.to_thread(_root_is_live, scan_root):
                if _deferred:
                    _pending_deleted |= orphan_ids
                else:
                    await delete_track_ids(list(orphan_ids))
                    _changed = True
                log.info("Stale cleanup (scoped): %d track(s) removed under %d "
                         "changed path(s) of %s", len(orphan_ids), len(_scoped), scan_root)
            else:
                log.warning("Stale cleanup (scoped): %s reads empty or unreadable — "
                            "not pruning %d track(s)", scan_root, len(orphan_ids))
        elif _scoped is not None:
            pass
        elif scan_root in walk_errors:
            # os.walk hit a read error somewhere under this root, so ``files`` is
            # only a PARTIAL listing — pruning now would delete tracks whose
            # files are merely unreadable (permission denied, a flaky mount),
            # not deleted.  Skip; the next clean scan prunes genuine deletions.
            log.warning(
                "Stale cleanup: skipping %s — its scan hit read errors, so the "
                "file listing is incomplete (won't prune from a partial read).",
                scan_root,
            )
        elif not files:
            # ZERO files enumerated (no read error) is ambiguous — a genuinely
            # emptied folder looks identical to a silently-failed mount (an SMB/NFS
            # stale handle can read as empty rather than raising) or a healthy
            # archive-only root after "scan zips" is turned off.  Never mass-delete
            # on that signal, at ANY track count — this matches the remote
            # ghost-cleanup rule (never prune a zero-entry listing).  A root that
            # was genuinely emptied is cleared explicitly via "remove folder"
            # (DELETE /api/admin/dirs → delete_tracks_by_scan_root), not by a
            # scan that could just as easily be seeing a dropped mount.
            if existing_ids:
                log.info(
                    "Stale cleanup: %s listed 0 files — leaving its %d tracks in "
                    "place (won't prune a zero-listing root; remove the folder to "
                    "clear a genuinely-emptied one).", scan_root, len(existing_ids),
                )
        elif orphan_ids:
            # The root is readable and still lists files, so an orphan here is a
            # genuinely-deleted file (its live siblings prove the enumeration is
            # real).  Prune it — queue for the deferred delta apply, else straight
            # from the live store.
            if _deferred:
                _pending_deleted |= orphan_ids
                log.info("Stale cleanup: %d orphan tracks queued for removal for %s",
                         len(orphan_ids), scan_root)
            else:
                orphan_count = await delete_track_ids(list(orphan_ids))
                _changed = _changed or orphan_count > 0
                log.info("Stale cleanup: removed %d orphan tracks for %s", orphan_count, scan_root)
        else:
            log.debug("Stale cleanup: no orphans for %s", scan_root)

        # A scoped scan saw only part of the root: its count is recomputed from
        # the store once the delta is committed (below).
        if not _removed_now(scan_root):
            await upsert_scan_dir(scan_root, track_count_val=(
                None if _scoped is not None else skipped + dir_counts[scan_root]))

    # Publish the scan result.  Re-scan (deferred) path: apply the accumulated
    # delta to the LIVE store in one batch via its own concurrency-safe
    # upsert/delete — the live store was untouched for the whole extract phase,
    # so browse/search/playback stayed responsive; this brief batch at the end
    # is the only moment the live view changes.  Because we apply a delta (only
    # the scan's own ids) rather than overwrite the store, concurrent writes to
    # other tracks during the extract survive, and memory stays consistent with
    # the AOF.  First scan (progressive) path: exit batch mode, rebuilding the
    # live indexes in place with yield points.
    _gone = [r for r in dir_files if _removed_now(r)]
    if _gone:
        from soniqboom.core.data import path_hash as _ph_gone
        _gone_h = {_ph_gone(r) for r in _gone}
        _pending_tracks = [t for t in _pending_tracks if t.get("scan_root_hash") not in _gone_h]
        _pending_deleted = {tid for tid in _pending_deleted
                            if (store._tracks.get(tid) or {}).get("scan_root_hash") not in _gone_h}
    if _deferred:
        # Disjointness guard: _pending_tracks / _pending_deleted accumulate
        # across ALL roots, so with nested/overlapping registered roots a stale
        # orphan id from one root could otherwise delete a track another root
        # just (re-)extracted.  An upsert always wins over a delete.
        if _pending_deleted:
            _pending_deleted -= {t["id"] for t in _pending_tracks}
        # A re-extracted file whose metadata equals the stored track (touched,
        # rewritten identically, copied over itself) only needs its mtime/size
        # refreshed: no re-index, and it is not a library change.  A genuinely
        # changed one keeps the stored ``added_at`` (it is not a new track)
        # and duplicate annotation (re-grouped below).
        _touch: list[tuple[str, dict]] = []
        _material: list[dict] = []
        _old_versions: dict[str, "dict | None"] = {}
        for _i, _td in enumerate(_pending_tracks):
            _old = store._tracks.get(_td["id"])
            if _old is not None and _same_track_content(_old, _td):
                _touch.append((_td["id"], {"mtime": _td.get("mtime"),
                                           "file_size": _td.get("file_size")}))
            else:
                if _old is not None:
                    for _k in _KEEP_ON_REEXTRACT:
                        if _k in _old:
                            _td[_k] = _old[_k]
                _old_versions[_td["id"]] = _old
                _material.append(_td)
            if _i % 2000 == 1999:
                await asyncio.sleep(0)
        for _tid in _pending_deleted:
            _old_versions[_tid] = store._tracks.get(_tid)
        for i in range(0, len(_touch), 500):          # cheap: no index is touched
            store.update_track_fields_batch(_touch[i : i + 500])
            await asyncio.sleep(0)
        from soniqboom.api.fstree import root_commit as _root_commit
        # Browse clicks during the commit keep the pre-commit rows (no rebuild
        # from a half-committed store) until the patch below lands.
        with _root_commit({path_hash(r) for r in dir_files}):
            if _material or _pending_deleted:
                _changed = True
                _dup_delta = _old_versions
                if len(_material) + len(_pending_deleted) <= _SMALL_COMMIT:
                    # A small delta (a watcher scan, a few edited files) is merged
                    # into the live sorted indexes per track (~1 ms each) — batch
                    # mode would re-sort every sorted index of the whole library.
                    for i in range(0, len(_material), 25):
                        store.upsert_tracks_batch(_drop_removed_roots(_material[i : i + 25]))
                        await asyncio.sleep(0)
                    _del = list(_pending_deleted)
                    for i in range(0, len(_del), 50):
                        await delete_track_ids(_del[i : i + 50])
                else:
                    # Batch mode defers the O(n log n) sorted-index rebuild to a single
                    # pass in _async_exit_batch_mode; the per-chunk yields keep the loop
                    # responsive while the delta is applied.
                    #
                    # Self-heal a batch depth leaked by a crashed prior scan: local
                    # scans are queue-serialized, so a nonzero depth with no remote
                    # scan active (``_current_remote_dirs`` is populated at remote-scan
                    # start, before that scan enters batch mode) is stale and would
                    # otherwise wedge every future rebuild at the depth>0 early-return.
                    if store._batch_depth != 0 and not _current_remote_dirs:
                        store._batch_depth = 0
                    store.enter_batch_mode()
                    try:
                        for i in range(0, len(_material), WRITE_CHUNK):
                            store.upsert_tracks_batch(_drop_removed_roots(_material[i : i + WRITE_CHUNK]))
                            await asyncio.sleep(0)
                        if _pending_deleted:
                            _del = list(_pending_deleted)
                            for i in range(0, len(_del), 500):
                                await delete_track_ids(_del[i : i + 500])
                    finally:
                        # Mirror the remote-scan wrapper's proven guard: shield so a
                        # cancellation can't interrupt the rebuild mid-flight, and never
                        # let a raise leave the store stuck in batch mode (NEW-6).
                        try:
                            await asyncio.shield(_async_exit_batch_mode(store))
                        except BaseException:
                            store._batch_mode = False
                            log.exception("exit_batch_mode failed after scan commit")
                log.info("Scan commit: applied %d upserts + %d deletes to live store "
                         "(%d unchanged re-reads refreshed)",
                         len(_material), len(_pending_deleted), len(_touch))
                await _refresh_browse_after_commit(store, _old_versions)
            elif _touch:
                log.info("Scan commit: %d re-read file(s) unchanged — mtime refreshed only",
                         len(_touch))
            else:
                # Nothing changed (incremental re-scan, all files skipped) — the live
                # store is already correct, so do NO work at all: no rebuild, no swap.
                log.debug("Scan: no track changes — skipping commit (live unchanged)")
    else:
        await _async_exit_batch_mode(store)
        _prog_batch_entered = False   # progressive batch section closed cleanly
        _changed = _changed or bool(all_track_ids)

    if scope and _changed:
        from soniqboom.core.data import path_hash as _ph
        for _root in scope:
            if _root in dir_files and not _removed_now(_root):
                await upsert_scan_dir(_root, track_count_val=len(
                    store._tag_scan_root_hash.get(_ph(_root), ())))

    log.info(
        "Phase 1 complete: %d tracks written in %.1fs",
        len(all_track_ids), time.time() - _progress.started_at,
    )

    # ── Phase 1.5: Duplicate detection ──────────────────────────────────────
    # Heavy compute in a subprocess (own GIL — no event loop starvation).
    # Results applied back in small batches with yield points.
    #
    # On a 270K-track library this step takes ~4 min, during which the UI
    # would otherwise show stale state from the extract phase
    # ("Checking 795 files for changes…" with per-share paths listed).
    # Surface the phase via ``_progress.current_file`` and broadcast so
    # the progress label flips to "Detecting duplicates…" immediately —
    # the user sees that something specific is happening instead of
    # assuming the scan is stuck.
    # Nothing added, updated or removed → the duplicate groups, HVSC data and
    # aggregations are all still right: skip the (library-wide) passes.  The
    # folder watcher's rescans are usually exactly this.
    for d in _fp_record:
        if not _removed_now(d) and get_store().get_config(_FP_KEY + path_hash(d)) != _fp:
            get_store().set_config(_FP_KEY + path_hash(d), _fp)

    if not _changed:
        log.info("Scan: nothing changed — skipping duplicate detection and post-scan passes")

    # HVSC: apply per-tune durations + STIL to SID tracks now that they're
    # indexed.  SID extraction runs in worker processes whose HVSC singleton is
    # unconfigured (spawn), so this main-process join — keyed on the cached
    # ``sid_md5`` — is what actually updates the tracks.  Covers both a
    # pre-configured HVSC and one auto-detected during THIS scan.  Idempotent.
    try:
        from soniqboom.core.hvsc import get_hvsc
        if (_changed or _hvsc_new) and get_hvsc().is_configured() and any(
            str(p).split("::")[0].lower().endswith(_SID_DETECT_EXTS)
            for fl in dir_files.values() for p in fl
        ):
            from soniqboom.core.hvsc_apply import apply_hvsc_to_library
            _hvsc_ids = (set(_dup_delta) if (not _hvsc_new and _dup_delta is not None)
                         else None)
            await apply_hvsc_to_library(reload=False, ids=_hvsc_ids)
    except Exception:
        log.debug("HVSC post-scan apply failed", exc_info=True)

    # Duplicate groups: re-group whatever this scan (and the HVSC apply above,
    # and any edit since the last pass) changed — incrementally for a small
    # delta, the full subprocess pass for a big one.
    if _changed or _hvsc_new:
        _progress.current_file = "Detecting duplicates…"
        if on_progress:
            await on_progress(_progress)
        try:
            await _regroup_duplicates_now()
        except Exception as exc:
            log.error("Duplicate detection failed (non-fatal): %s", exc)

    if _changed:
        _progress.current_file = "Refreshing aggregations…"
        if on_progress:
            await on_progress(_progress)
        from soniqboom.api.library import invalidate_agg_cache
        invalidate_agg_cache()

    _scan_count = max(0, _scan_count - 1)
    if _scan_count == 0:
        _progress.running      = False
        _progress.embedding    = False
        _progress.current_file = ""

    if on_progress:
        await on_progress(_progress)

    _release_pool(executor)
    return _changed


# ── Public API ────────────────────────────────────────────────────────────────

# (dirs, on_progress, scope): ``scope`` None = full scan of every dir; a dict
# root → changed paths = a scoped (folder-watcher) scan of just those paths.
# 4th field ``light``: an AUTOMATIC full scan (startup reconcile, the
# watcher's full fallback) — unchanged archives are listed from the store.
_scan_queue: list[tuple[frozenset[str], Callable | None,
                        "dict[str, frozenset[str]] | None", bool]] = []
_current_scan_dirs: frozenset[str] = frozenset()
_current_scan_scoped: bool = False       # the running local scan is a scoped (watcher) one
_current_scan_light: bool = False        # … an automatic ("light") full one
_SCOPED_MERGE_MAX = 4096                  # merged queued scope per root beyond this → full scan

_current_remote_dirs: set[str] = set()   # active remote scan roots
# True while the PROGRESSIVE (first-scan) path holds an un-try/finally'd
# ``enter_batch_mode`` — lets the queue's crash handler unwind exactly that
# scan's one batch level without clobbering a concurrent remote scan's depth.
_prog_batch_entered: bool = False


def _reset_scan_state() -> None:
    """``begin_run``: the scan state a previous server run in this process
    left behind — its event loop closed under a running scan (cancelled, so
    no ``finally`` of the scan code counted it down), its pause event bound
    to that loop — back to idle; a batch level the progressive first-scan
    path left open is unwound (as ``_drain_scan_queue`` does after a crash)."""
    global _pause_event, _scan_count, _scan_task, _progress, _prog_batch_entered
    global _current_scan_dirs, _current_scan_scoped, _current_scan_light
    _pause_event = None
    _scan_task = None
    _scan_queue.clear()
    _current_scan_dirs = frozenset()
    _current_scan_scoped = False
    _current_scan_light = False
    _current_remote_dirs.clear()
    if _scan_count or _progress.running:
        _scan_count = 0
        _progress = ScanProgress()
    if _prog_batch_entered:
        try:
            store = get_store()
            if store._batch_depth > 0:
                store._batch_depth -= 1
                if store._batch_depth == 0:
                    store._batch_mode = False
                    store._rebuild_sorted_indexes()
                    store._sorted_dirty = False
        except Exception:                                   # noqa: BLE001
            log.exception("could not unwind a scan's batch state")
        _prog_batch_entered = False


# Post-scan scene enrichment — a single COALESCING runner so no scan's
# enrichment delta is ever dropped.  Every drain sets a ``pending`` flag; the
# runner loops while pending is set, so a scan that drains WHILE a prior apply
# is running (or right after one) still gets folded in on the next pass instead
# of being stranded until some unrelated future scan (QA round-1 MAJOR).  Strong
# refs keep the task alive; a settle sleep coalesces bursts so a folder-watch
# storm can't re-churn the whole library back-to-back on a small host.
#
# One runner, fixed order: Modland (fills/corrects the artist Demozoo's
# composer match reads, and sets game albums) → the UADE song database (fills
# what is still empty) → Demozoo → the one-time header
# game backfill → the folder-album pass (after Modland, so a Modland game
# wins over a folder guess without a replace round-trip).
_scene_autoapply_tasks: set = set()
_scene_autoapply_pending = False
_scene_autoapply_running = False
_SCENE_AUTOAPPLY_SETTLE_S = 10
# How long a post-scan pass waits for the local one-time header-game backfill
# to finish before the shares' backfills (``repair.wait_idle``).
_BACKFILL_WAIT_S = 10.0
# ``scan_root_hash`` of the remote roots whose one-time header-game backfill
# (``repair.run_remote_album_backfill``) waits for the runner.
_remote_backfill_pending: set[str] = set()


def _scene_enrichment_wanted() -> bool:
    from soniqboom.core import demozoo, scene_metadata, songdb
    if scene_metadata.has_index():
        return True
    if songdb.has_index() and songdb.auto_apply_enabled():
        return True
    return demozoo.has_index() and demozoo.auto_apply_enabled()


_dup_runner_task: "asyncio.Task | None" = None
_DUP_REGROUP_SETTLE_S = 3.0        # a burst of edits → one re-group
_DUP_FULL_MIN_INTERVAL_S = 600.0   # background full passes (big remote deltas) ≤ 1 / 10 min
_dup_full_last = float("-inf")


_DUP_PENDING_KEY = "dup_regroup_pending"   # persisted: changes not yet re-grouped
_dup_lock_pair: "tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None" = None


def _dup_lock() -> asyncio.Lock:
    """One re-group at a time (per event loop): a background full pass
    computed from an older snapshot must not land over a newer incremental
    result."""
    global _dup_lock_pair
    loop = asyncio.get_running_loop()
    if _dup_lock_pair is None or _dup_lock_pair[0] is not loop:
        _dup_lock_pair = (loop, asyncio.Lock())
    return _dup_lock_pair[1]


async def _regroup_duplicates_now(*, background: bool = False) -> None:
    """Re-group the duplicate groups touched by the store's pending changes
    (``TrackStore.take_dup_dirty``): incrementally for up to
    ``_DUP_INCR_MAX`` tracks, else one full pass.  While a batch section holds
    the sorted indexes (a remote scan) the changes stay pending for the
    runner.  A pass that fails puts the work back (as a full pass)."""
    global _dup_full_last
    store = get_store()
    if background:
        # A background FULL pass is rate-limited.  The wait happens outside the
        # lock and before taking the pending set (a scan ending meanwhile takes
        # it and re-groups itself), and scans are re-checked after it.
        if store._dup_dirty_overflow:
            wait = _dup_full_last + _DUP_FULL_MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        while (_scan_task is not None and not _scan_task.done()) or store._batch_mode:
            await asyncio.sleep(2)
    lock = _dup_lock()
    if not background and lock.locked():
        # A background pass is running: this scan's changes stay pending and
        # the runner (which loops while anything is pending) takes them —
        # the scan queue doesn't wait seconds behind a full pass.
        _ensure_dup_runner()
        return
    async with lock:
        if store._batch_mode or "_sorted_duration" in store._dirty_sorted:
            _ensure_dup_runner()
            return
        delta, overflow = store.take_dup_dirty()
        if not delta and not overflow:
            _clear_dup_pending(store)
            return
        try:
            done = False
            if not overflow and len(delta) <= _DUP_INCR_MAX:
                try:
                    done = await _run_duplicate_detection_incremental(delta)
                except Exception:                           # noqa: BLE001 — full pass below
                    log.warning("Incremental duplicate detection failed — running the "
                                "full pass", exc_info=True)
            if not done:
                _dup_full_last = time.monotonic()
                await _run_duplicate_detection_async()
        except BaseException:
            store._dup_dirty_overflow = True             # put the work back
            raise
        _clear_dup_pending(store)


def _clear_dup_pending(store) -> None:
    if not store._dup_dirty and not store._dup_dirty_overflow:
        if store.get_config(_DUP_PENDING_KEY):
            store.set_config(_DUP_PENDING_KEY, False)
        if store.get_config(_DUP_PENDING_DELTA_KEY):
            store.set_config(_DUP_PENDING_DELTA_KEY, None)


def _on_dup_dirty() -> None:
    """Store callback: duplicate-relevant changes are pending.  Persist that,
    so a shutdown before the re-group still gets one (a full pass) after the
    next start."""
    store = get_store()
    if not store.get_config(_DUP_PENDING_KEY):
        store.set_config(_DUP_PENDING_KEY, True)
    _ensure_dup_runner()


def _ensure_dup_runner() -> None:
    global _dup_runner_task
    if _dup_runner_alive():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                                  # not on the loop (replay, a thread)
    _dup_runner_task = loop.create_task(_dup_regroup_runner(), name="dup-regroup")


async def _dup_regroup_runner() -> None:
    """Background re-grouping for changes made outside a local scan (tag
    edits, duration backfills, enrichment, remote scans).  A running local
    scan re-groups at its own end; a remote scan's batch must close first."""
    store = get_store()
    while True:
        await asyncio.sleep(_DUP_REGROUP_SETTLE_S)
        while (_scan_task is not None and not _scan_task.done()) or store._batch_mode:
            await asyncio.sleep(2)
        if not store._dup_dirty and not store._dup_dirty_overflow:
            return
        try:
            await _regroup_duplicates_now(background=True)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("Background duplicate re-grouping failed")


def install_dup_regroup() -> None:
    """Called right after the library is loaded (the journal replay goes
    through ``bulk_load`` and records nothing): from here on every
    duplicate-relevant write schedules the runner.  Changes a previous run
    recorded but never re-grouped (shutdown inside the settle / rate-limit
    window, an interrupted first scan) are healed by one full pass."""
    store = get_store()
    store._on_dup_dirty = _on_dup_dirty
    if store.get_config(_DUP_PENDING_KEY):
        saved = store.get_config(_DUP_PENDING_DELTA_KEY)
        if saved:
            # consumed now: a crash later in THIS run must not re-use it
            store.set_config(_DUP_PENDING_DELTA_KEY, None)
        if isinstance(saved, dict) and saved:
            log.info("Duplicate groups: re-grouping %d change(s) left by the previous run",
                     len(saved))
            for tid, old in saved.items():
                store._dup_dirty.setdefault(tid, old if isinstance(old, dict) else None)
        else:
            log.info("Duplicate groups: changes from the previous run were not re-grouped — "
                     "scheduling a full pass")
            store._dup_dirty_overflow = True
    if store._dup_dirty or store._dup_dirty_overflow:
        _ensure_dup_runner()


def _dup_runner_alive() -> bool:
    t = _dup_runner_task
    return t is not None and not t.done() and not t.cancelling()


_DUP_PENDING_DELTA_KEY = "dup_regroup_pending_delta"


def finalize_dup_pending() -> None:
    """Last shutdown step before the journal is flushed: if a duplicate-relevant
    write landed after ``cancel_background_tasks`` saved the pending set, the
    saved set is incomplete — drop it so the next start runs a full pass."""
    try:
        store = get_store()
        saved = store.get_config(_DUP_PENDING_DELTA_KEY)
        busy = ((_dup_lock_pair is not None and _dup_lock_pair[1].locked())
                or (_scan_task is not None and not _scan_task.done()))
        if saved and (busy or store._dup_dirty_overflow
                      or not set(store._dup_dirty) <= set(saved)):
            store.set_config(_DUP_PENDING_DELTA_KEY, None)
    except Exception:                                   # noqa: BLE001
        log.debug("finalizing the pending duplicate re-group failed", exc_info=True)


def cancel_background_tasks() -> None:
    """Shutdown: stop the duplicate re-group runner and keep what it had not
    re-grouped yet — the ids + their previous values (a small set), so the
    next start re-groups just those; after a crash only the flag survives and
    the next start runs one full pass."""
    if _dup_runner_task is not None and not _dup_runner_task.done():
        _dup_runner_task.cancel()
    try:
        store = get_store()
        busy = _dup_lock_pair is not None and _dup_lock_pair[1].locked()
        if store._dup_dirty and not store._dup_dirty_overflow and not busy:
            # (a re-group in flight has taken part of the work: then only the
            # flag survives and the next start runs one full pass)
            store.set_config(_DUP_PENDING_DELTA_KEY, dict(store._dup_dirty))
    except Exception:                                   # noqa: BLE001
        log.debug("saving the pending duplicate re-group failed", exc_info=True)


def _spawn_scene_autoapply() -> None:
    """Mark the library dirty for scene enrichment — the Modland join (once an
    index is downloaded), the UADE song database and the Demozoo composer
    groups + canonical release years (each unless the user switched its
    auto-apply off) — and ensure the
    coalescing runner is going, so the manual Admin Apply steps are no longer
    required after a scan.  The runner finishes with the folder-album pass;
    without any index this schedules just that pass.

    Idempotent (a re-apply updates only what changed) and provenance survives
    rescans, so it never fights a user's manual edit."""
    try:
        wanted = _scene_enrichment_wanted()
    except Exception:                               # noqa: BLE001
        log.debug("scene enrichment check failed", exc_info=True)
        wanted = False
    if not wanted and not _remote_backfill_pending:
        _schedule_folder_album_pass()
        return
    global _scene_autoapply_pending
    _scene_autoapply_pending = True
    _ensure_scene_autoapply_runner()


def schedule_startup_reconcile() -> None:
    """One incremental rescan of the local roots after startup (unless
    switched off), so changes made while the server was stopped are indexed.
    Skipped when a full scan is already running or queued."""
    from soniqboom.config import settings
    if not getattr(settings, "startup_reconcile_scan", True):
        log.info("Startup reconcile: switched off in Settings — skipped")
        return

    async def _go() -> None:
        try:
            dirs = [d["path"] for d in get_store().list_scan_dirs()
                    if not str(d.get("path", "")).startswith(
                        ("smb://", "ftp://", "http://", "https://", "webdav://", "webdavs://"))]
            # (is_dir off the loop: a hung mount must not freeze it)
            live = await asyncio.to_thread(lambda: [d for d in dirs if Path(d).is_dir()])
            if live:
                log.info("Startup reconcile: incremental rescan of %d local folder(s)", len(live))
                await start_scan(live, light=True)
        except Exception:                               # noqa: BLE001
            log.exception("Startup reconcile scan failed")
    try:
        t = asyncio.get_running_loop().create_task(_go(), name="startup-reconcile")
        _scene_autoapply_tasks.add(t)
        t.add_done_callback(_scene_autoapply_tasks.discard)
    except RuntimeError:
        pass


def schedule_startup_enrichment() -> None:
    """Called once the library has loaded: an upgrade whose Modland apply
    learned something new (``scene_metadata.MODLAND_APPLY_VERSION``) or whose
    extractor now reads console-rip game names (the one-time header-game
    backfill) heals without a manual step or a rescan; an album revert that a
    restart interrupted (``folder_album.resume_pending_revert``) is resumed.
    Never blocks startup — it only schedules background work."""
    try:
        from soniqboom.core import folder_album
        folder_album.resume_pending_revert()
    except Exception:                               # noqa: BLE001
        log.debug("album revert resume failed", exc_info=True)
    try:
        from soniqboom.core import game_titles      # lists or matching may have changed
        game_titles.schedule()
    except Exception:                               # noqa: BLE001
        log.debug("game-title startup pass failed", exc_info=True)
    try:
        from soniqboom.core import scene_metadata, repair
        from soniqboom.core.store import get_store
        need_modland = scene_metadata.has_index() and not scene_metadata.apply_version_current()
        need_backfill = not get_store().get_config(repair.ALBUM_BACKFILL_CONFIG_KEY)
    except Exception:                               # noqa: BLE001
        log.debug("startup enrichment check failed", exc_info=True)
        return
    if need_modland or need_backfill:
        global _scene_autoapply_pending
        _scene_autoapply_pending = True
        _ensure_scene_autoapply_runner()


def _ensure_scene_autoapply_runner() -> None:
    global _scene_autoapply_running
    if _scene_autoapply_running:
        return                                  # a runner is already draining `pending`
    _scene_autoapply_running = True

    async def _run() -> None:
        from soniqboom.core import demozoo, repair, scene_metadata, songdb
        global _scene_autoapply_pending, _scene_autoapply_running
        lock_retries = 0
        try:
            while _scene_autoapply_pending:
                _scene_autoapply_pending = False
                # Never join a half-written library: wait out a running scan
                # (its own drain re-triggers this runner anyway).
                while is_scanning():
                    await asyncio.sleep(_SCENE_AUTOAPPLY_SETTLE_S or 0.05)
                retry = False
                if scene_metadata.has_index():
                    try:
                        res = await scene_metadata.apply_to_library(auto=True)
                        if res.get("error") == "apply already running":
                            retry = True
                        elif not res.get("skipped"):
                            la = res.get("last_apply") or {}
                            if la.get("updated"):
                                log.info("Post-scan Modland apply: %d track(s) "
                                         "updated (%d album changes)",
                                         la["updated"], la.get("albums", 0))
                    except Exception:           # noqa: BLE001 — best-effort
                        log.debug("Post-scan Modland apply failed", exc_info=True)
                if songdb.has_index() and songdb.auto_apply_enabled():
                    try:
                        res = await songdb.apply_to_library()
                        if res.get("error") == "apply already running":
                            retry = True
                        elif not res.get("skipped"):
                            la = res.get("last_apply") or {}
                            if la.get("updated"):
                                log.info("Post-scan song-database apply: %d track(s) "
                                         "updated", la["updated"])
                    except Exception:           # noqa: BLE001 — best-effort
                        log.debug("Post-scan song-database apply failed", exc_info=True)
                # Toggled OFF (e.g. a Reset) — must NOT re-enrich a just-reset
                # library, not even mid-coalesce.
                if demozoo.has_index() and demozoo.auto_apply_enabled():
                    try:
                        res = await demozoo.apply_to_library()
                        if res.get("error") == "apply already running":
                            retry = True
                        else:
                            updated = res.get("updated") or 0
                            if updated:
                                log.info("Post-scan Demozoo auto-apply: %d track(s) "
                                         "enriched", updated)
                    except Exception:           # noqa: BLE001 — best-effort
                        log.debug("Post-scan Demozoo auto-apply failed", exc_info=True)
                if retry:
                    # A manual Admin apply holds the lock — retry our delta
                    # once it clears (bounded, so a WEDGED manual apply can't
                    # spin this forever; a later scan re-triggers regardless).
                    lock_retries += 1
                    if lock_retries <= 5:
                        _scene_autoapply_pending = True
                else:
                    lock_retries = 0
                try:
                    if await repair.run_album_backfill_once():
                        # Let it finish (and settle) first, briefly: the
                        # shares' own backfills below would find the task busy
                        # and wait for their next scan.  Bounded — a read
                        # stuck on a hung mount must not hold every later
                        # pass (enrichment, folder pass, the shares).
                        await repair.wait_idle(_BACKFILL_WAIT_S)
                except Exception:               # noqa: BLE001 — best-effort
                    log.debug("Header-game album backfill failed to start", exc_info=True)
                await _run_remote_album_backfills()
                _schedule_folder_album_pass()
                if _scene_autoapply_pending:
                    # Coalesce a burst of drains (folder-watch) into fewer applies.
                    await asyncio.sleep(_SCENE_AUTOAPPLY_SETTLE_S)
        finally:
            _scene_autoapply_running = False

    try:
        t = asyncio.create_task(_run(), name="scene-autoapply")
        _scene_autoapply_tasks.add(t)
        t.add_done_callback(_scene_autoapply_tasks.discard)
    except RuntimeError:
        _scene_autoapply_running = False        # no running loop (shouldn't happen here)


def _queue_remote_album_backfill(scan_root: str, plan: dict) -> bool:
    """After a completed scan of remote root ``scan_root`` (its ``plan``):
    queue its one-time header-game backfill (``repair.run_remote_album_backfill``)
    for the enrichment runner, unless it already ran — one config read then.
    A full walk that extracted every file (a first scan) needs none: the
    current extractor just read them all.  Returns True when queued."""
    import hashlib
    try:
        from soniqboom.core import repair
        root_hash = hashlib.sha256(scan_root.encode()).hexdigest()[:16]
        if repair.remote_album_backfill_done(root_hash):
            return False
        if (plan.get("full_walk_ok") and not plan.get("skip")
                and not plan.get("mtime_refresh")):
            repair.mark_remote_album_backfill_done(root_hash)
            return False
    except Exception:                                   # noqa: BLE001
        log.debug("remote album backfill check failed", exc_info=True)
        return False
    _remote_backfill_pending.add(root_hash)
    return True


async def _run_remote_album_backfills() -> None:
    """The runner's step for the queued remote roots (after the local
    backfill, so the two never compete for the one repair task).  A root that
    has to wait — another repair is running, or the root went offline — stays
    queued for the next runner pass (a later scan of it queues it again)."""
    from soniqboom.core import repair
    for root_hash in sorted(_remote_backfill_pending):
        try:
            res = await repair.run_remote_album_backfill(root_hash)
        except Exception:                               # noqa: BLE001 — best-effort
            log.debug("Remote header-game album backfill failed to start", exc_info=True)
            res = True                                  # a later scan re-queues it
        if res is None:
            break                                       # the repair task is busy
        _remote_backfill_pending.discard(root_hash)


def _schedule_folder_album_pass() -> None:
    """Post-scan hook for the opt-in "album from folder name" pass
    (``core/folder_album.py``) — a no-op while the setting is off; otherwise a
    coalescing background runner that skips work when the library didn't
    change.  Never lets a failure escape into the scan path."""
    try:
        from soniqboom.core import folder_album
        folder_album.schedule_after_scan()
    except Exception:                                   # noqa: BLE001
        log.debug("folder-album post-scan hook failed", exc_info=True)
    try:
        from soniqboom.core import game_titles       # game from the archive name
        game_titles.schedule()
    except Exception:                                   # noqa: BLE001
        log.debug("game-title post-scan hook failed", exc_info=True)


async def _drain_scan_queue() -> None:
    """Run scans sequentially until the queue is empty."""
    global _scan_task, _current_scan_dirs, _current_scan_scoped, _current_scan_light
    global _scan_count, _progress, _prog_batch_entered
    any_changed = False
    while _scan_queue:
        dirs_set, cb, scope, light = _scan_queue.pop(0)
        _current_scan_dirs = dirs_set
        _current_scan_scoped = scope is not None
        _current_scan_light = bool(light) and scope is None
        log.info("Scan queue: starting %s scan of %d dir(s), %d remaining in queue",
                 "scoped" if scope else "full", len(dirs_set), len(_scan_queue))
        try:
            any_changed = bool(await _run_scan(list(dirs_set), cb, scope=scope,
                                               light=_current_scan_light)) or any_changed
        except Exception:
            log.exception("Scan failed with unhandled error")
            # Ensure progress state is always cleaned up so the UI
            # doesn't show "scanning" forever.
            _scan_count = max(0, _scan_count - 1)
            if _scan_count == 0:
                _progress.running      = False
                _progress.embedding    = False
                _progress.current_file = ""
            # The progressive first-scan path enters batch mode with no
            # try/finally over its Phase-1 span, so a mid-Phase-1 raise leaks
            # exactly ONE batch level (``_prog_batch_entered``), which would
            # wedge every future sorted-index rebuild at the depth>0
            # early-return.  Unwind precisely that one level — decrement (never
            # zero), so a CONCURRENT remote freshness scan's own batch level is
            # left intact.  Heal the index only once depth actually reaches 0
            # (i.e. no remote scan still holds it).
            try:
                store = get_store()
                if _prog_batch_entered and store._batch_depth > 0:
                    store._batch_depth -= 1
                    _prog_batch_entered = False
                    if store._batch_depth == 0:
                        store._batch_mode = False
                        store._rebuild_sorted_indexes()
                        store._sorted_dirty = False
            except Exception:
                log.exception("Failed to heal batch state after scan crash")
        _current_scan_dirs = frozenset()
        _current_scan_scoped = False
        _current_scan_light = False
    # Local scan queue drained — fold the results into the scene enrichment
    # (Modland, Demozoo, the header-game backfill) and then the folder-album
    # pass, in that order, in the background.  (Remote scans trigger the same
    # runner from ``start_remote_scan``; the runner waits out any scan still
    # running, local or remote.)  Skipped when no scan changed anything.
    if any_changed:
        _spawn_scene_autoapply()
    _scan_task = None


def forget_root(path: str) -> None:
    """A scan root was removed: drop it from every queued scan, so a scan
    queued before the removal doesn't re-register and re-index it."""
    norm = str(Path(path).resolve())
    for i in range(len(_scan_queue) - 1, -1, -1):
        dirs, cb, scope, light = _scan_queue[i]
        if norm not in dirs:
            continue
        rest = frozenset(dirs - {norm})
        if not rest:
            del _scan_queue[i]
        else:
            _scan_queue[i] = (rest, cb, None if scope is None
                              else {r: v for r, v in scope.items() if r != norm}, light)


def full_scan_active() -> bool:
    """A full (not watcher-scoped) scan is running or queued, local or remote —
    whether a manual "scan now" request would be redundant."""
    return ((bool(_current_scan_dirs) and not _current_scan_scoped and not _current_scan_light)
            or bool(_current_remote_dirs)
            or any(q_scope is None and not q_light for _d, _cb, q_scope, q_light in _scan_queue))


def is_scanning(path: str | None = None) -> bool:
    """Check if a path (or any path) is currently being scanned or queued."""
    if path is None:
        return bool(_current_scan_dirs) or bool(_current_remote_dirs) or bool(_scan_queue)
    # Remote paths (ftp://, smb://) aren't resolved via Path()
    if path.startswith(("ftp://", "smb://")):
        return path in _current_remote_dirs
    norm = str(Path(path).resolve())
    if norm in _current_scan_dirs:
        return True
    return any(norm in q_dirs for q_dirs, *_ in _scan_queue)


async def start_scan(
    directories: list[str],
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
    *,
    scope: "dict[str, Iterable[str]] | None" = None,
    rescan_if_running: bool = False,
    light: bool = False,
) -> asyncio.Task:
    """Queue a scan.  If one is already running the request is queued and will
    run automatically once the current scan finishes — duplicates are skipped.

    ``scope`` (root → changed file/folder paths) queues a SCOPED scan of just
    those paths (the folder watcher).  A queued full scan of the same roots
    absorbs it; queued scoped scans of the same roots are merged into one.  A
    scan already RUNNING never absorbs it — it may have walked past the change.
    ``rescan_if_running`` gives a full request the same rule (the watcher's
    full fallback): it queues behind a running full scan of the same roots
    instead of being dropped as a duplicate.  ``light`` marks an automatic
    full scan: archives unchanged since their members were indexed are listed
    from the store instead of re-read (a manual request clears it)."""
    global _scan_task
    norm_dirs = frozenset(str(Path(d).resolve()) for d in directories)

    if scope:
        # Changed paths are normalized LEXICALLY (never ``resolve()``d): a
        # symlinked file inside the root is indexed under its in-library path,
        # and resolving it would scan its target — possibly outside the root.
        nscope = {}
        full_roots: list[str] = []
        for r, paths in scope.items():
            nr = str(Path(r).resolve())
            pre = nr.rstrip(os.sep) + os.sep
            keep = frozenset(q for q in (os.path.normpath(os.path.abspath(x)) for x in paths)
                             if q == nr or q.startswith(pre))
            if nr in keep:
                full_roots.append(nr)           # the root itself changed → full scan
            elif keep:
                nscope[nr] = keep
        if full_roots:
            await start_scan(full_roots, on_progress, rescan_if_running=True, light=True)
        if not nscope:
            return _scan_task
        norm_dirs = frozenset(nscope)
        for i, (q_dirs, q_cb, q_scope, q_light) in enumerate(_scan_queue):
            if q_scope is None and norm_dirs.issubset(q_dirs):
                log.info("Scoped scan absorbed by a queued full scan: %s", sorted(norm_dirs))
                if _scan_task and not _scan_task.done():
                    return _scan_task
                break
            if q_scope is not None and q_dirs == norm_dirs and q_cb is on_progress:
                merged = {r: q_scope.get(r, frozenset()) | nscope.get(r, frozenset())
                          for r in norm_dirs}
                if any(len(v) > _SCOPED_MERGE_MAX for v in merged.values()):
                    # Too many changed paths piled up behind a running scan:
                    # one full incremental rescan is cheaper than the scope.
                    _scan_queue[i] = (q_dirs, q_cb, None, True)
                    log.info("Queued scoped scan grew past %d paths — full rescan of %s",
                             _SCOPED_MERGE_MAX, sorted(norm_dirs))
                else:
                    _scan_queue[i] = (q_dirs, q_cb, merged, False)
                log.info("Scoped scan merged into a queued one: %s", sorted(norm_dirs))
                if _scan_task and not _scan_task.done():
                    return _scan_task
                break
        else:
            _scan_queue.append((norm_dirs, on_progress, nscope, False))
        if _scan_task and not _scan_task.done():
            return _scan_task
        _scan_task = asyncio.create_task(_drain_scan_queue())
        return _scan_task

    # Skip if these dirs are already being FULLY scanned right now (a running
    # scoped scan covers only a few paths — a full request still queues; a
    # manual request never counts a running AUTOMATIC one as done)
    if (norm_dirs and norm_dirs.issubset(_current_scan_dirs) and not _current_scan_scoped
            and not rescan_if_running and not (_current_scan_light and not light)):
        log.info("Scan skipped — dirs already being scanned: %s", norm_dirs)
        if _scan_task and not _scan_task.done():
            return _scan_task
        # Shouldn't happen, but fall through to create task if needed

    # Skip if these dirs are already queued
    for i, (queued_dirs, q_cb, q_scope, q_light) in enumerate(_scan_queue):
        if q_scope is None and norm_dirs.issubset(queued_dirs):
            if q_light and not light:
                _scan_queue[i] = (queued_dirs, q_cb, None, False)   # manual wins
            log.info("Scan skipped — dirs already queued: %s", norm_dirs)
            if _scan_task and not _scan_task.done():
                return _scan_task
            break

    _scan_queue.append((norm_dirs, on_progress, None, light))

    if _scan_task and not _scan_task.done():
        log.info("Scan already running — queued %d dir(s) (queue depth: %d)",
                 len(norm_dirs), len(_scan_queue))
        return _scan_task
    _scan_task = asyncio.create_task(_drain_scan_queue())
    return _scan_task


# ── Remote scan (SMB / FTP) ─────────────────────────────────────────────────

# Archive-enumeration sidecar.  Every walk used to download EVERY remote
# archive in full just to list its members — 22 GB per rescan observed on
# one share — even though the skip logic then discarded almost all of the
# work.  Unchanged archives are now handled without a single byte:
#   * archives with indexed members → member entries are SYNTHESIZED from
#     the store (see _remote_scan_body's known_archives);
#   * archives with NO indexed members ("barren": zips of text files,
#     archives whose members all failed extraction) have nothing in the
#     store to synthesize from, so this sidecar remembers them keyed on
#     (scan_root, archive, mtime, size).
# The per-scan_root "schema" stamp guards the whole mechanism: member
# lists derived from the store are only valid while the supported-format
# map that produced them is unchanged.  A version upgrade that registers
# new formats changes the fingerprint, which disables the skip for ONE
# full walk per share (deep enumeration surfaces the newly-supported
# members), after which the stamp is refreshed.
_enum_sidecar_lock = threading.Lock()


class _PermanentMemberError(Exception):
    """A member failed to read out of a LOCALLY-present archive (bad CRC,
    unsupported compression method, name-vs-header mismatch).  Distinct
    from network fetch errors so the barren bookkeeping can tell "this
    archive's contents are genuinely unreadable" (eligible for the barren
    sidecar) apart from "the FTP hiccuped" (must never be blacklisted)."""


def _enum_sidecar_path() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "archive_enum.json"


def _member_schema_fingerprint() -> str:
    import hashlib as _h
    from soniqboom.core.archive import _AMIGA_PREFIX_EXT
    from soniqboom.core.metadata import SUPPORTED_EXTENSIONS
    blob = (",".join(sorted(e.lower() for e in SUPPORTED_EXTENSIONS))
            + "|" + ",".join(sorted(_AMIGA_PREFIX_EXT)))
    return _h.sha1(blob.encode()).hexdigest()[:12]


def _load_enum_sidecar() -> dict:
    try:
        d = json.loads(_enum_sidecar_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _update_enum_sidecar(
    scan_root: str,
    *,
    enum_report: dict,
    indexed_arcs: set,
    failed_fetch_arcs: set,
    stamp_schema: bool,
) -> None:
    """Fold one finished scan's enumeration results into the sidecar.

    ``enum_report`` holds every archive DOWNLOADED and enumerated this
    scan (arc_rel → (mtime, size, member_count)).  An arc is barren iff
    none of its members ended up indexed — but never mark one barren when
    its fetches failed (transient FTP outage must not blacklist a real
    archive until its mtime happens to change).
    """
    with _enum_sidecar_lock:
        d = _load_enum_sidecar()
        barren = d.setdefault("barren", {})
        for arc, (m, s, _n) in enum_report.items():
            key = f"{scan_root}|{arc}"
            if arc in indexed_arcs:
                barren.pop(key, None)
            elif arc not in failed_fetch_arcs:
                barren[key] = [m, s]
        if stamp_schema:
            d.setdefault("schemas", {})[scan_root] = _member_schema_fingerprint()
        path = _enum_sidecar_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(path)


def _list_remote_zip_members(root_path: str, zip_fe, source,
                             enum_report: dict | None = None) -> list:
    """Download a remote ``.zip`` (once, via the local remote-cache) and return
    a ``DirEntry`` per audio member.

    Each member carries the OUTER archive's ``size``+``mtime`` so the
    incremental-skip logic re-extracts a zip's members iff the archive itself
    changed.  Member ``path`` is ``<zip_rel>::<member>``; the caller prepends
    the ``ftp://host/share:`` prefix exactly as it does for a loose file.
    Best-effort: a broken/oversized archive logs + yields ``[]``.
    """
    from soniqboom.core.filesource import DirEntry
    from soniqboom.core.remote_cache import get_cache

    out: list = []
    # Walk-phase feedback: archive enumeration is the most expensive part
    # of a large scan (each un-skipped archive is a full download) and it
    # runs BEFORE total/processed are known — surface the current archive
    # name so /admin/scan/status shows life instead of a frozen counter.
    # Plain attribute write from the walk thread; readers only display it.
    _progress.current_file = (
        f"Reading archive {PurePosixPath(zip_fe.path).name}…"
    )
    try:
        # Changed-on-remote guard: drop a stale cached copy before
        # enumerating, or the members of the OLD archive get indexed.
        get_cache().validate_size(root_path, zip_fe.path, zip_fe.size)
        local_archive = get_cache().fetch(root_path, zip_fe.path, source,
                                          lane="scan")
    except Exception as exc:
        log.warning("Cannot fetch remote archive %s: %s", zip_fe.path, exc)
        return out
    # ``archive.list_members`` dispatches ZIP vs LHA, handles the Amiga
    # ``MOD.title`` prefix naming, and already filters to playable members.
    for member in archive.list_members(local_archive):
        out.append(DirEntry(
            name=_basename_of(member),
            path=f"{zip_fe.path}::{member}",
            is_dir=False,
            size=zip_fe.size,
            mtime=zip_fe.mtime,
        ))
    if enum_report is not None:
        # Record that this archive was actually downloaded + enumerated —
        # the scan's tail folds this into the enumeration sidecar (barren
        # archives get remembered; see _update_enum_sidecar).
        enum_report[zip_fe.path] = (zip_fe.mtime, zip_fe.size, len(out))
    return out


def _mtime_matches_tolerant(stored_mtime: float, listed_mtime: float) -> bool:
    """True iff a stored mtime and a freshly-listed mtime denote the SAME
    modification, tolerant of the MLSD(seconds)↔LIST(minutes) precision gap.

    MLSD carries seconds; a Unix LIST fallback carries only minutes (seconds
    truncated to :00).  So when a source flips MLSD↔LIST the same unchanged
    file's stored vs listed mtime can differ by up to 59 s — far past the 2 s
    tolerance — which would re-extract ~the whole share on every latch
    transition.  When EITHER value is minute-aligned (a LIST value, or an MLSD
    file modified exactly on the minute) we compare at MINUTE granularity so
    the transition is a no-op; otherwise full-second precision is kept so a
    genuine sub-minute re-tag is still detected.  Blind spot: a same-minute
    re-tag of a file whose mtime lands on :00 (~1-in-60 × same-minute).
    """
    if stored_mtime <= 0:
        return False
    a, b = stored_mtime, listed_mtime
    if int(stored_mtime) % 60 == 0 or int(listed_mtime) % 60 == 0:
        a -= int(a) % 60
        b -= int(b) % 60
    return abs(a - b) < 2.0


def _rel_under_any_dir(rel: str, dirs: set[str]) -> bool:
    """True iff root-relative file path *rel* lies within any directory in
    *dirs* (also root-relative).  A dir of "" or "/" (the whole root failed
    to list) matches everything.  Used by ghost cleanup to protect tracks
    under a subtree whose listing hard-failed — those files only LOOK deleted
    because their directory couldn't be listed, not because they were removed.
    """
    for d in dirs:
        f = d.rstrip("/")
        if f in ("", "/"):
            return True
        if rel == f or rel.startswith(f + "/"):
            return True
    return False


def _find_remote_audio_entries(
    root_path: str, source: "FileSource",
    *,
    dir_mtime_cap: float | None = None,
    scan_zips: bool = True,
    archive_skip: dict | None = None,
) -> tuple[list, int, bool, set]:
    """Discover audio files via ``walk_with_stat`` — yields DirEntry
    objects preserving ``size`` and ``mtime`` from the underlying
    directory-listing response.

    ``dir_mtime_cap`` (optional, Unix epoch seconds) enables the dir-
    mtime fast path: any subtree whose dir.mtime ≤ cap is pruned from
    the walk entirely.  On a stable 30 K-file share this turns a
    30 K-entry walk into ~50 dir-mtime checks.

    Caveat: not every FTP/SMB server updates parent-dir mtime when
    children change.  Servers that don't (mtime stays 0 or constant)
    are detected here: the cap-check is purely "is mtime > cap"; if
    mtime is 0 the check always returns False and the subtree is
    walked normally — i.e. correctness is preserved even when the
    optimization is unsupported.

    Returns ``(entries, pruned_subtree_count, walk_completed, failed_dirs)``.
    ``walk_completed`` is False only when the walk itself DIED midway (an
    exception escaped the whole walk) — the entries list is then PARTIAL in
    an unknown shape and ghost cleanup must be skipped entirely.
    ``failed_dirs`` is the set of ROOT-RELATIVE directory paths whose listing
    hard-failed (borrow timeout / socket / transient 5xx) while the rest of
    the walk succeeded; ghost cleanup must skip tracks under those subtrees
    (they only LOOK deleted) but may still purge cleanly-walked subtrees, so
    one flaky directory can't disable ghost cleanup for the whole share.

    Used by ``start_remote_scan`` to decide which files actually need
    re-extraction (mtime+size match → skip) vs. which need a fresh
    download.  Without this, every re-index re-downloads every byte.
    """
    from soniqboom.core.filesource import DirEntry, FileSource  # noqa
    entries = []
    skipped_junk = 0
    pruned = [0]  # mutable holder for closure
    synth_arcs = 0        # archives satisfied from the store (no download)
    barren_skips = 0      # archives skipped via the barren sidecar

    def _skip(dir_entry) -> bool:
        # Prune subtree iff dir.mtime exists AND is ≤ cap (i.e. hasn't
        # been touched since the cap timestamp).  mtime of 0 / unknown
        # → can't make a safe call → don't prune.
        if dir_mtime_cap is None or dir_mtime_cap <= 0:
            return False
        if dir_entry.mtime <= 0:
            return False
        if dir_entry.mtime > dir_mtime_cap:
            return False
        pruned[0] += 1
        return True

    listing_errors: list = []
    try:
        walk_kwargs: dict = {"error_sink": listing_errors}
        if dir_mtime_cap is not None and dir_mtime_cap > 0:
            walk_kwargs["skip_subtree_fn"] = _skip
        for _dirpath, _dir_entries, file_entries in source.walk_with_stat("/", **walk_kwargs):
            for fe in file_entries:
                if _is_junk_filename(fe.name):
                    skipped_junk += 1
                    continue
                if (is_supported_music_name(fe.name)
                        and not (scan_zips and fe.name.lower().endswith((".zip", ".lha", ".lzh")))):
                    entries.append(fe)
                elif scan_zips and fe.name.lower().endswith((".zip", ".lha", ".lzh")):
                    if archive_skip is not None:
                        ka = archive_skip["known"].get(fe.path)
                        if (ka and fe.size == ka[1]
                                and abs(fe.mtime - ka[0]) < 2.0):
                            # Archive unchanged since its members were
                            # indexed — synthesize the member entries from
                            # the store instead of downloading the archive
                            # just to re-list it (the old behaviour cost a
                            # full download per archive per walk).
                            for rel in ka[2]:
                                entries.append(DirEntry(
                                    name=_basename_of(rel.split("::", 1)[1]),
                                    path=rel,
                                    is_dir=False,
                                    size=fe.size,
                                    mtime=fe.mtime,
                                ))
                            synth_arcs += 1
                            continue
                        ba = archive_skip["barren"].get(fe.path)
                        if (ba and fe.size == ba[1]
                                and abs(fe.mtime - ba[0]) < 2.0):
                            # Known to contain no playable members at this
                            # (mtime, size) — nothing to download.
                            barren_skips += 1
                            continue
                    # Crack open the remote archive (ZIP or Amiga LHA/LZH) and
                    # surface its audio members as ``<archive_rel>::<member>``.
                    entries.extend(_list_remote_zip_members(
                        root_path, fe, source,
                        enum_report=(archive_skip or {}).get("report"),
                    ))
    except Exception as exc:
        log.error("Remote walk_with_stat failed for %s: %s", root_path, exc)
        walk_completed = False
    else:
        walk_completed = True
    # A per-directory listing that HARD-FAILED (borrow timeout / socket /
    # transient 5xx) is swallowed to [] inside the source so an interactive
    # browse renders empty instead of crashing — but for a WALK that means the
    # failed subtree's files are absent from ``entries`` and would look
    # deleted.  We surface the ROOT-RELATIVE paths that failed so the caller
    # can protect EXACTLY those subtrees from ghost cleanup (per-subtree),
    # rather than the old all-or-nothing suppression that let one reliably-
    # failing directory disable ghost cleanup for the whole share forever.
    failed_dirs = {p for p, _ in listing_errors}
    if failed_dirs:
        log.warning(
            "Remote walk of %s had %d directory listing failure(s) — "
            "ghost cleanup will SKIP those subtree(s) only; first: %s",
            root_path, len(failed_dirs), sorted(failed_dirs)[:3],
        )
    if synth_arcs or barren_skips:
        log.info(
            "Archive skip for %s: %d archive(s) synthesized from the store, "
            "%d barren archive(s) skipped — zero bytes downloaded for them",
            root_path, synth_arcs, barren_skips,
        )
    if skipped_junk:
        log.info(
            "Discovered %d remote audio entries in %s (skipped %d junk, pruned %d subtree(s))",
            len(entries), root_path, skipped_junk, pruned[0],
        )
    else:
        log.info(
            "Discovered %d remote audio entries in %s (pruned %d subtree(s))",
            len(entries), root_path, pruned[0],
        )
    return entries, pruned[0], walk_completed, failed_dirs


async def _prefetch_folder_art_remote(
    scan_root: str,
    source,
    entries: list,
    *,
    inflight: int = 1,
) -> dict:
    """Warm the shared folder-art cache for every unique parent directory
    observed during the walk.

    ``inflight`` defaults to 1.  Higher values let the prefetch
    contend with the main extract loop for FTP-pool slots.  Observed
    on a re-index of a large share: a 4-wide prefetch combined with
    the 6-wide extract window pushed an 8-slot pool to saturation
    (``in_use=8 idle=0 waiting_scan=8``), which throttled download
    throughput in the menubar (7.8 MB/s observed against a gigabit
    LAN).  Single-flight prefetch leaves at most 7/8 pool slots for
    the user-visible extract path and lets prefetch progress on the
    slack capacity that's left when extract workers escalate between
    partial-fetch stages.

    The art endpoint looks up ``folder:{dir_hash}`` before doing a fresh
    ``list_dir`` + ``read_file`` (see ``_try_folder_art`` in
    ``soniqboom/api/art.py``).  Lazy population works for the *second*
    track in a folder; for the *first* track the user still pays a
    full FTP round trip.  Warming here turns "first track is slow,
    rest are instant" into "every track is instant" — and at the
    cost of one extra MLSD per directory (we already pay one for the
    walk; this just adds a single RETR per dir on a small image).

    ``dir_hash`` MUST be computed the same way as ``_build_track``
    (which reads it from ``hash_cache`` populated by
    ``store_hash_lookups_batch``):
      * ``hashlib.sha256(parent_dir.encode()).hexdigest()[:16]``
      * ``parent_dir = str(PurePosixPath(remote_path).parent)`` —
        for entries from ``walk_with_stat`` (always slash-prefixed,
        e.g. ``/REOL/foo.flac``) this is ``/REOL`` for nested
        files and ``/`` for root-level files.

    Concurrency: bounded by ``inflight`` (default 4) so the prefetch
    doesn't starve the main extract loop that's competing for the
    same FTP pool.  All reads use ``lane='scan'`` for the same reason.

    Returns a stats dict: ``{unique_dirs, warmed, skipped_cached,
    no_art, errors}``.  Logged by the caller; not currently surfaced
    on the WebSocket.
    """
    from soniqboom.core import art_cache
    from soniqboom.core.data import get_config
    from soniqboom.api.art import _find_folder_art_remote, _parse_folder_art_names

    stats = {
        "unique_dirs": 0, "warmed": 0,
        "skipped_cached": 0, "no_art": 0, "errors": 0,
    }
    if not entries:
        return stats

    # Unique parent dirs in the form PurePosixPath produces on the
    # _process_one path — so the hash here matches what _build_track
    # stamps onto each track.
    parents: set[str] = set()
    for fe in entries:
        rel = fe.path or ""
        if not rel:
            continue
        if "::" in rel:
            # Archive members: ``…/x.zip::sub/track.mod``'s "parent" is a
            # pseudo-path inside the zip — LISTing it costs a 550 round
            # trip per archive.  The real on-disk folder that could hold
            # a folder.jpg is the archive's own parent.
            rel = rel.split("::", 1)[0]
        parents.add(str(PurePosixPath(rel).parent))
    stats["unique_dirs"] = len(parents)
    if not parents:
        return stats

    csv = await get_config("folder_art_names", "")
    priority = _parse_folder_art_names(csv if isinstance(csv, str) else "")
    if not priority:
        # No candidate filenames → nothing to prefetch.  Don't log
        # at INFO so this stays quiet on stripped-down installs.
        log.debug(
            "Folder-art prefetch skipped for %s: folder_art_names empty",
            scan_root,
        )
        return stats

    sem = asyncio.Semaphore(inflight)
    loop = asyncio.get_event_loop()
    stats_lock = asyncio.Lock()

    # ``_find_folder_art_remote`` defaults ``lane='stream'``; bind
    # ``lane='scan'`` via a partial so the run_in_executor call site
    # stays positional-args-only (the executor doesn't forward kwargs).
    import functools as _ft
    _find_with_scan_lane = _ft.partial(_find_folder_art_remote, lane="scan")

    async def _warm_one(parent_dir: str) -> None:
        dir_h = path_hash(parent_dir)
        cache_key = f"folder:{dir_h}"
        try:
            cached = await art_cache.get_art(cache_key, "full")
        except Exception:
            cached = None
        if cached:
            async with stats_lock:
                stats["skipped_cached"] += 1
            return
        await _await_resume()
        remote_dir = parent_dir if parent_dir else "/"
        async with sem:
            try:
                data, _mime = await loop.run_in_executor(
                    None, _find_with_scan_lane,
                    scan_root, remote_dir, source, priority,
                )
            except Exception as exc:
                log.debug(
                    "Folder-art prefetch list failed for %s: %s",
                    parent_dir, exc,
                )
                async with stats_lock:
                    stats["errors"] += 1
                return
        if data:
            try:
                from soniqboom.core.metadata import cap_full_cover
                loop = asyncio.get_running_loop()
                capped = await loop.run_in_executor(None, cap_full_cover, data)
                await art_cache.store_art(cache_key, capped, "full")
                async with stats_lock:
                    stats["warmed"] += 1
            except Exception as exc:
                log.debug(
                    "Folder-art cache store failed for %s: %s",
                    cache_key, exc,
                )
                async with stats_lock:
                    stats["errors"] += 1
        else:
            async with stats_lock:
                stats["no_art"] += 1

    await asyncio.gather(
        *(_warm_one(p) for p in parents),
        return_exceptions=True,
    )
    # Completion logging is handled by ``_art_prefetch_done`` (the
    # done_callback the caller attaches) so the log line appears
    # AFTER the task finishes from the event loop's perspective —
    # no double-log if the caller awaits this synchronously somewhere
    # in tests.
    return stats


async def start_remote_scan(
    share_id: str,
    scan_root: str,
    source: "FileSource",
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None = None,
    *,
    dir_mtime_cap: float | None = None,
) -> dict:
    """Scan a remote FileSource — download files, extract metadata, upsert.

    Returns this scan's plan dict (``scan_root``/``walked``/``extract``/
    ``new``/``skip``/``mtime_refresh``/``ghosts``), or an empty dict if the
    scan was deduped away OR raised at any point (including a crash mid-
    extraction, AFTER the plan was computed).  Returning {} on a crashed
    scan is deliberate: a scan that died part-way did not fully index its
    discoveries, so the freshness poller must not record its ``new`` count
    into the cadence or fire a "N new tracks" toast for tracks that may not
    all have landed.  Callers (the
    freshness poller) MUST use this return value rather than the process-
    global ``get_progress().last_plan`` — that global is last-writer-wins
    and a concurrent sibling-share scan overwrites it, which would attribute
    one share's new-track count (and its "N new tracks" toast) to another
    and mis-tighten the wrong share's poll cadence.

    ``dir_mtime_cap`` (optional, Unix epoch seconds) enables the fast-
    path walk: subtrees whose parent dir.mtime hasn't changed since
    the cap timestamp are skipped entirely.  Freshness loop passes
    ``last_check_ts - safety_buffer`` here.  Pass ``None`` for a full
    walk (manual re-index, first-time arming, periodic drift sweep).

    Per-scan_root dedupe
    --------------------
    Rapid Re-Index clicks used to fire concurrent scans on the same
    share.  Each finished fast (because the optimisation skipped
    most files), but they raced on the AOF flush and produced
    confusing "drift" items on the third or fourth scan.  Now: if
    this scan_root already appears in ``_current_remote_dirs``, log
    and return immediately — the in-flight scan will broadcast its
    own completion when done.

    Unlike the local scan which farms out file paths to worker processes,
    this downloads files via the source (which holds the connection) on a
    thread pool, then extracts metadata in worker processes.

    Incremental optimisation
    -------------------------
    Before the partial-fetch + mtime-skip work, this function blindly
    re-downloaded every file in the share on every re-index.  For a
    48K-file FLAC library on FTP, that's hours of network IO to
    re-read tag headers that hadn't changed.

    Now we:

      1. ``walk_with_stat`` returns ``(name, size, mtime)`` per file in
         the same MLSD response that already listed names — zero extra
         round trips.
      2. Pre-load the store's existing ``(mtime, size)`` per path under
         this scan_root.
      3. Classify each walked entry into one of three buckets:
           * **skip** — store mtime > 0 AND matches listing mtime+size
             → no work.
           * **mtime-refresh** — store mtime == 0 (legacy entry from
             before this change) AND size matches listing size → bump
             stored mtime so the NEXT scan can skip, no download.
           * **extract** — new file, or size/mtime drift → full path:
             partial fetch (if format budget allows) → mutagen → upsert.
      4. After processing, any store track under this scan_root whose
         path didn't appear in the walk is a ghost (file deleted on the
         remote) and gets purged.
    """
    global _progress, _scan_count

    # Dedupe: drop if this scan_root is already being scanned.  The
    # in-flight task will broadcast its own completion event when
    # done; the user's rapid clicks shouldn't spawn parallel scans
    # against the same share (they raced on the AOF flush and produced
    # phantom "drift" items on subsequent scans).
    if scan_root in _current_remote_dirs:
        log.info(
            "Remote scan for %s already in progress — ignoring duplicate trigger",
            scan_root,
        )
        if on_progress:
            # Send a synthetic broadcast so the UI's "Re-Index" button
            # gets feedback instead of looking unresponsive.
            await on_progress(_progress)
        # Deduped: the in-flight scan owns the plan.  Return an EMPTY plan so
        # a freshness poller records no change / fires no toast off a stale
        # global (the dedupe-skip stale-last_plan contamination path).
        return {}

    # Register BEFORE the walk.  The walk/enumeration phase — which
    # downloads remote archives just to list their members — is the most
    # expensive part of a large scan; registering only after it (the old
    # order) meant (a) /admin/scan/status showed the PREVIOUS scan's
    # final state for the entire phase ("not progressing"), and (b) the
    # dedupe gate above couldn't see a walk-phase scan, so a double
    # trigger started a parallel walk against the same share.
    _scan_count += 1
    _current_remote_dirs.add(scan_root)
    if not _progress.running:
        _progress = ScanProgress(total=0, running=True)
    _progress.current_file = f"Discovering files in {scan_root}…"
    if on_progress:
        await on_progress(_progress)

    executor = _process_pool(SCAN_WORKERS)
    batch_state = {"entered": False}
    plan: dict = {}
    try:
        plan = await _remote_scan_body(
            share_id, scan_root, source, on_progress,
            dir_mtime_cap=dir_mtime_cap, executor=executor,
            batch_state=batch_state,
        ) or {}
    except Exception:
        # A dead scan must NEVER leave zombie state.  Before this guard,
        # one unhandled BrokenProcessPool left running=true forever,
        # silently blocked every re-trigger of the share via the dedupe
        # gate, and orphaned thousands of sibling download tasks (the
        # 22 GB eviction storm of 2026-07-02).
        log.exception("Remote scan for %s aborted", scan_root)
    finally:
        # SYNCHRONOUS state cleanup FIRST.  If this task was CANCELLED,
        # the first ``await`` in this finally re-raises CancelledError at
        # its suspension point and everything after it would be skipped —
        # re-creating the exact zombie state this wrapper exists to
        # prevent.  Only after the flags are safe do we attempt the
        # (shielded) awaits, each swallowing BaseException so a
        # cancellation can't abort the remaining cleanup steps.
        _release_pool(executor)
        _current_remote_dirs.discard(scan_root)
        _scan_count = max(0, _scan_count - 1)
        if _scan_count == 0:
            _progress.running = False
            _progress.embedding = False
            _progress.current_file = ""
            # Snap processed up to total ONLY when the last scan finishes
            # (see the parallel-scan display bug note in _run_scan).
            if _progress.processed < _progress.total:
                _progress.processed = _progress.total
        if batch_state["entered"]:
            try:
                # shield: on cancellation the await raises immediately but
                # the exit itself still completes in the background.
                await asyncio.shield(_async_exit_batch_mode(get_store()))
            except BaseException:
                log.exception(
                    "exit_batch_mode failed after remote scan of %s", scan_root)
        if on_progress:
            try:
                await on_progress(_progress)
            except BaseException:
                pass
    # Reached on the normal and handled-exception paths (a cancellation
    # re-raises out of the finally above and never gets here).  ``plan`` is
    # this scan's own plan, or {} if the body died before building one.
    # A scan that extracted (new / changed files) or found removed ones feeds
    # the same post-scan enrichment runner as a local scan (Modland → Demozoo →
    # header-game backfill → folder pass); a no-change freshness poll (only
    # skips / mtime refreshes) just gets the cheap, seq-gated folder pass —
    # unless this share's one-time header-game backfill is still due (a
    # completed scan that listed files proves the share reachable).
    backfill_due = (bool(plan.get("walked"))
                    and _queue_remote_album_backfill(scan_root, plan))
    if plan.get("extract") or plan.get("ghosts") or backfill_due:
        _spawn_scene_autoapply()
    else:
        _schedule_folder_album_pass()
    return plan


async def _remote_scan_body(
    share_id: str,
    scan_root: str,
    source: "FileSource",
    on_progress: Callable[[ScanProgress], Awaitable[None]] | None,
    *,
    dir_mtime_cap: float | None,
    executor: ProcessPoolExecutor,
    batch_state: dict,
) -> dict:
    """The working half of :func:`start_remote_scan`.

    Returns this scan's plan dict (the same one stashed in
    ``_progress.last_plan`` for the admin UI) so the wrapper can hand it
    straight back to the caller without a racy global read.

    Split out so the wrapper can guarantee scan-state cleanup (progress
    flags, ``_current_remote_dirs``, ``_scan_count``, executor shutdown,
    store batch mode) in ONE ``finally`` no matter where this body dies.
    ``batch_state["entered"]`` tracks store batch mode across the split:
    set True right after ``enter_batch_mode`` and back to False after the
    body's own successful exit call, so the wrapper only force-exits it
    when the body died in between.
    """
    global _progress
    from soniqboom.core.filesource import FileSource
    from soniqboom.core.metadata import HEADER_BUDGET
    from soniqboom.core.remote_cache import get_cache

    loop = asyncio.get_event_loop()
    cache = get_cache()

    # ── Store map (BEFORE the walk — the walk needs it too) ─────────────
    #
    # Build the existing (path → (mtime, size, track_id)) map for this
    # scan_root so we can decide skip / refresh / extract in one in-
    # memory pass.  ``get_track_ids_for_scan_root`` is O(1) via the
    # scan_root_hash tag index, then we materialise the small subset.
    # Built ahead of the walk so unchanged archives can have their member
    # entries synthesized from the store instead of being re-downloaded.
    import hashlib
    from soniqboom.core.filesource import parse_remote_path

    scan_root_hash = hashlib.sha256(scan_root.encode()).hexdigest()[:16]
    store_local = get_store()
    existing_ids = store_local.get_track_ids_for_scan_root(scan_root_hash)
    existing_map: dict[str, tuple[float, int, str]] = {}
    if existing_ids:
        # Materialise track metas to read mtime + path + size.
        records = store_local.get_tracks_batch(list(existing_ids))
        for r in records:
            if not r:
                continue
            p = r.get("path") or ""
            # Stored path is the canonical ``ftp://host/share:/relative``.
            # Use parse_remote_path to extract the relative tail — the
            # earlier ``p.split(":", 1)[-1]`` mishandled this because
            # ``split(":", 1)`` peels off the SCHEME ("ftp"), leaving
            # ``//host/share:/relative`` — which can never match the
            # walked entry path ``/relative`` and so EVERY track
            # silently got re-extracted.  Local paths (no scheme) fall
            # through to ``p`` unchanged.
            try:
                _scan_root, rel = parse_remote_path(p)
            except ValueError:
                rel = p
            if not rel:
                # Bare share root with no file tail — skip; can't be a track.
                continue
            existing_map[rel] = (
                float(r.get("mtime", 0) or 0),
                int(r.get("file_size", 0) or 0),
                r.get("id", ""),
            )

    # ── Archive skip plumbing ────────────────────────────────────────────
    # known: arc_rel → (mtime, size, [member rel paths]) from the store;
    # members inherit the outer archive's stat at index time, so any one
    # member's (mtime, size) is the archive's.
    #
    # An arc is only skippable when EVERY stored member carries the same
    # valid stat.  Synthesizing a PARTIAL member list (e.g. dropping a
    # legacy mtime==0 member) would leave the dropped member out of
    # ``seen_rel_paths`` — the ghost cleanup below would then delete a
    # perfectly healthy track (QA 2026-07-02).  Mixed/invalid stats →
    # the arc simply enumerates like before.
    _arc_members: dict[str, list[str]] = {}
    _arc_stat: dict[str, tuple[float, int] | None] = {}
    for rel, (m, s, _tid) in existing_map.items():
        if "::" not in rel:
            continue
        arc = rel.split("::", 1)[0]
        _arc_members.setdefault(arc, []).append(rel)
        if m <= 0 or s <= 0:
            _arc_stat[arc] = None                    # invalid → never skip
        elif arc not in _arc_stat:
            _arc_stat[arc] = (m, s)
        else:
            prev = _arc_stat[arc]
            if prev is not None and (abs(prev[0] - m) >= 2.0 or prev[1] != s):
                _arc_stat[arc] = None                # inconsistent → never skip
    known_archives: dict[str, tuple[float, int, list[str]]] = {}
    for arc, members in _arc_members.items():
        st = _arc_stat.get(arc)
        if st is not None:
            known_archives[arc] = (st[0], st[1], members)

    _sidecar = await loop.run_in_executor(None, _load_enum_sidecar)
    _schema_now = _member_schema_fingerprint()
    _schema_ok = _sidecar.get("schemas", {}).get(scan_root) == _schema_now
    enum_report: dict[str, tuple[float, int, int]] = {}
    if not _schema_ok and known_archives:
        log.info(
            "Archive skip disabled for %s this pass: supported-format map "
            "changed — deep enumeration will surface newly-supported "
            "members (one-time cost after an upgrade)", scan_root,
        )
    archive_skip = {
        # Synthesis only while the format map that produced the stored
        # member lists is still current — otherwise force enumeration.
        "known": known_archives if _schema_ok else {},
        "barren": {
            k.split("|", 1)[1]: (float(v[0]), int(v[1]))
            for k, v in _sidecar.get("barren", {}).items()
            if k.startswith(scan_root + "|") and isinstance(v, list)
        } if _schema_ok else {},
        "report": enum_report,
    }

    # Walk with stat — preserves mtime+size per file from MLSD.
    #
    # ``dir_mtime_cap`` (when supplied by the freshness loop) prunes
    # subtrees whose dir.mtime is unchanged since the cap timestamp.
    # On the first walk OR when caller passes None, the full walk
    # runs unchanged.  Wrapped in ``functools.partial`` because
    # ``run_in_executor`` doesn't forward kwargs.
    import functools as _ft
    from soniqboom.config import settings as _settings   # local bind — not a module global
    _walk_fn = _ft.partial(
        _find_remote_audio_entries,
        dir_mtime_cap=dir_mtime_cap,
        scan_zips=_settings.scan_remote_zips,
        archive_skip=archive_skip,
    )
    entries, _pruned, walk_completed, walk_failed_dirs = await loop.run_in_executor(
        None, _walk_fn, scan_root, source,
    )
    # ``walk_ok`` (strict) gates the enum-sidecar schema stamp: a full, clean
    # walk with zero listing failures is the only walk that proves the barren/
    # schema verdicts.  Ghost cleanup uses the finer ``walk_completed`` +
    # ``walk_failed_dirs`` signal instead (per-subtree — see the gate below).
    walk_ok = walk_completed and not walk_failed_dirs
    if not walk_completed:
        log.warning(
            "Walk of %s died midway — the %d entries collected are "
            "PARTIAL: ghost cleanup and sidecar updates are disabled "
            "for this scan", scan_root, len(entries),
        )

    # HVSC auto-detect (remote): SID files on this share + no DOCUMENTS path
    # configured → probe up the tree for DOCUMENTS/Songlengths.* and
    # auto-configure (reads the DB through the FileSource).  Once per scan_root
    # per process (remote stat() probes are slow), and never breaks the scan.
    if (getattr(_settings, "hvsc_autodetect", True)
            and scan_root not in _hvsc_probed_roots):
        from soniqboom.core.hvsc import get_hvsc as _get_hvsc
        if not _get_hvsc().is_configured():
            try:
                sid_rel_dirs = {
                    str(PurePosixPath("/" + str(fe.path).lstrip("/")).parent)
                    for fe in entries
                    if str(fe.path).lower().endswith(_SID_DETECT_EXTS)
                }
                if sid_rel_dirs:
                    docs = await loop.run_in_executor(
                        None, _detect_hvsc_docs_remote, source, scan_root, sid_rel_dirs,
                    )
                    # Mark probed only AFTER the (slow, network) detection ran —
                    # a transient failure is caught below and leaves the root
                    # un-probed so the next scan retries.
                    _hvsc_probed_roots.add(scan_root)
                    if docs:
                        await _apply_hvsc_autoconfig(docs)
            except Exception:
                log.debug("HVSC remote auto-detect failed", exc_info=True)

    # ── Classify against the store ────────────────────────────────────────
    # (existing_map was built before the walk — see above.)
    to_extract: list = []                          # full extract path
    to_refresh: list[tuple[str, float]] = []       # (track_id, new_mtime)
    seen_rel_paths: set[str] = set()
    new_count = 0                                  # genuinely-new files (not yet in the store)
    for fe in entries:
        rel = fe.path  # already root-relative from walk_with_stat
        seen_rel_paths.add(rel)
        existing = existing_map.get(rel)
        if existing is None:
            to_extract.append(fe)
            new_count += 1
            continue
        stored_mtime, stored_size, tid = existing
        if stored_size == fe.size and _mtime_matches_tolerant(stored_mtime, fe.mtime):
            # Genuine match — skip entirely.  The tolerant compare absorbs the
            # MLSD(seconds)↔LIST(minutes) precision gap so a latch transition
            # doesn't re-extract the whole share (see _mtime_matches_tolerant);
            # the old EXACT float equality flipped an entire share (21,197
            # files) to full re-download on any precision shift.
            continue
        if stored_size == fe.size and stored_mtime == 0 and fe.mtime > 0:
            # Legacy entry: size matches, mtime never captured.  Bump
            # it without re-downloading so the NEXT scan can skip.
            to_refresh.append((tid, fe.mtime))
            continue
        # Drift — re-extract.
        to_extract.append(fe)

    # Ghosts: store paths not seen on the remote → delete after scan.  Keep
    # the rel with each id so ghost cleanup can protect the ones under a
    # subtree whose listing hard-failed (they only LOOK deleted).
    ghost_pairs: list[tuple[str, str]] = [
        (rel, tid) for rel, (_m, _s, tid) in existing_map.items()
        if rel not in seen_rel_paths and tid
    ]
    ghost_ids: list[str] = [tid for _rel, tid in ghost_pairs]

    total = len(to_extract)
    scan_plan = {
        "scan_root":     scan_root,
        "walked":        len(entries),
        "extract":       total,
        "new":           new_count,
        "mtime_refresh": len(to_refresh),
        "skip":          len(entries) - total - len(to_refresh),
        "ghosts":        len(ghost_ids),
        # True iff this was a FULL walk that completed and saw real ground
        # truth — i.e. the exact condition under which ghost cleanup is
        # authorized to run below.  The freshness poller stamps its full-walk
        # ceiling clock ONLY on this, never on "a full walk was requested":
        # a deduped/crashed/partial walk must not reset the clock (it ran no
        # cleanup) or ghost cleanup could be deferred indefinitely.
        "full_walk_ok":  (dir_mtime_cap is None and walk_completed
                          and len(entries) > 0),
    }
    log.info(
        "Remote scan plan for %s: walked=%d, extract=%d (new=%d), "
        "mtime_refresh=%d, skip=%d, ghosts_to_delete=%d",
        scan_root, scan_plan["walked"], scan_plan["extract"], scan_plan["new"],
        scan_plan["mtime_refresh"], scan_plan["skip"], scan_plan["ghosts"],
    )
    # Self-diagnosing anomaly check: a plan that wants to re-extract most
    # of an ALREADY-INDEXED share almost always means the listing mtimes
    # shifted en masse (MLSD→LIST precision change, server timezone jump)
    # rather than 20k files genuinely changing.  Log the delta histogram
    # so the operator can see the shift instead of a silent full re-download.
    if len(existing_map) > 50 and total > 0.5 * max(1, len(entries)):
        _deltas = Counter()
        for fe in to_extract:
            _ex = existing_map.get(fe.path)
            if _ex and _ex[1] == fe.size and _ex[0] > 0:
                _deltas[int(round(fe.mtime - _ex[0]))] += 1
        if _deltas:
            log.warning(
                "Scan plan for %s wants %d/%d re-extracts of indexed files — "
                "mtime delta histogram (s: count): %s",
                scan_root, total, len(entries),
                dict(_deltas.most_common(8)),
            )

    # Apply mtime-refresh in a single batched store call (no network).
    if to_refresh:
        from soniqboom.core.store import get_store as _gs
        _gs().update_track_fields_batch(
            [(tid, {"mtime": m}) for tid, m in to_refresh]
        )

    # Build remote_paths list (preserving order) for _process_one.
    remote_paths = [fe.path for fe in to_extract]
    # Map path → (size, mtime) so _process_one can stamp meta.mtime from
    # the listing without paying for an extra MDTM round trip.
    entry_stat_map: dict[str, tuple[int, float]] = {
        fe.path: (fe.size, fe.mtime) for fe in to_extract
    }

    # Registration (running=True, _scan_count, _current_remote_dirs)
    # happened in the wrapper BEFORE the walk — here we only add this
    # scan's share of the work to the aggregate total.
    _progress.total += total
    _progress.last_plan = scan_plan
    log.info("Remote scan started: %d files to extract in %s", total, scan_root)

    await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")
    hash_map = await store_hash_lookups_batch([scan_root])

    store = get_store()
    store.enter_batch_mode()
    batch_state["entered"] = True

    track_buffer: list[Track] = []
    art_buffer: dict[str, str] = {}
    track_count = 0

    async def _flush():
        nonlocal track_buffer, art_buffer
        if not track_buffer:
            return
        for i in range(0, len(track_buffer), WRITE_CHUNK):
            await upsert_tracks_batch(track_buffer[i : i + WRITE_CHUNK])
            await asyncio.sleep(0)
        await store_full_art_batch(art_buffer)
        track_buffer = []
        art_buffer = {}

    # Sliding-window concurrency for remote downloads + metadata extraction —
    # the local scan path already uses INFLIGHT, but the remote path was
    # strictly serial.  Limit to ``_REMOTE_INFLIGHT`` concurrent transfers
    # so a slow share doesn't get hammered, but still overlap network I/O
    # with metadata extraction.
    #
    # Size the semaphore to follow the share's configured scan budget so
    # raising the slider in the FTP-pool UI actually widens the in-flight
    # download window.  Falls back to a sane default (8) when the pool
    # config / source backend doesn't expose a scan-lane budget.
    try:
        from soniqboom.core.filesource import _resolve_pool_size as _rps
        host = getattr(source, "_host", "")
        port = int(getattr(source, "_port", 0))
        if host and port:
            _max, _min, configured_total, detected = _rps(host, port)
            # Scan workers ≈ configured_total minus the 2 reserved-stream
            # default; clamp to the live effective_max so a server-cap
            # detection doesn't push us above the actual pool ceiling.
            _REMOTE_INFLIGHT = max(2, min(_max, configured_total - 2 or _max))
        else:
            _REMOTE_INFLIGHT = 8
    except Exception:
        _REMOTE_INFLIGHT = 8
    sem = asyncio.Semaphore(_REMOTE_INFLIGHT)
    log.info(
        "Remote scan window: %d concurrent transfers (pool-derived)",
        _REMOTE_INFLIGHT,
    )
    flush_lock = asyncio.Lock()

    # Live concurrency counters — separate from the FTP pool's in_use
    # because the pool tracks borrow scope, but we want to know how
    # many workers are in each phase (download vs extract vs flush).
    # Snapshot logged every 15 s by a background task so the operator
    # can confirm parallelism without running py-spy.
    _phase_counts = {"download": 0, "extract": 0, "flush": 0}
    _phase_lock = threading.Lock()  # cheap — bumped from coroutines only

    async def _phase_logger() -> None:
        """Print pool + per-phase worker stats every 10 s during the
        scan so concurrency can be observed in the log.

        Without this it's hard to tell from a single UI snapshot
        whether the scanner is bottlenecked on download (FTP pool
        saturated, ``in_use=6``) or on extract (workers in
        ProcessPool, ``in_use=0``) or genuinely serialised
        (``in_use=1`` consistently).  First sample at 5 s so the
        operator sees feedback quickly; steady-state at 10 s.
        """
        try:
            await asyncio.sleep(5)
            while True:
                with _phase_lock:
                    phases = dict(_phase_counts)
                try:
                    pool_status = source._pool.status()  # FTP pool only
                    pool_str = (
                        f"pool[in_use={pool_status.get('in_use', '?')}"
                        f" idle={pool_status.get('idle', '?')}"
                        f" max={pool_status.get('max_size', '?')}"
                        f" waiting_scan={pool_status.get('waiting_scan', '?')}"
                        f" waiting_stream={pool_status.get('waiting_stream', '?')}]"
                    )
                except (AttributeError, Exception):
                    pool_str = "pool[n/a]"
                log.info(
                    "scan concurrency: workers[download=%d extract=%d flush=%d]"
                    " window=%d/%d %s  processed=%d/%d",
                    phases["download"], phases["extract"], phases["flush"],
                    _REMOTE_INFLIGHT - sem._value, _REMOTE_INFLIGHT,  # noqa: SLF001
                    pool_str,
                    _progress.processed, _progress.total,
                )
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

    # Compiled once.  Mutagen's title-fallback path uses
    # ``tempfile.NamedTemporaryFile`` whose stem is ``tmp[8 alnum]``;
    # observing that title means extract() FAILED and the fallback
    # kicked in — a definitive partial-fetch undershoot signal that
    # doesn't depend on guessing what "looks like" the filename stem.
    import re as _re_inc
    _TEMPFILE_TITLE_RE = _re_inc.compile(r"^tmp[a-zA-Z0-9_]{6,12}$")

    def _extract_looks_incomplete(meta, remote_path: str) -> bool:
        """Heuristic: did a partial fetch under-shoot the budget?

        Three signals, any of which means "the bytes we got weren't
        enough to read the tag block":

        1. ``meta.title`` matches ``tmp[alnum]+`` — that's mutagen's
           internal tempfile basename leaking through because the
           real tag parse failed and ``extract()`` fell back to
           ``path.stem``.  Definitive: a real song will never be
           titled that.

        2. ``meta.title`` equals the REMOTE-side filename stem
           combined with ``duration == 0`` — weaker signal but
           catches the case where mutagen returned cleanly but
           with no useful data.

        3. ``meta.title`` empty AND ``duration == 0`` — same idea.

        Only consulted on the partial-fetch path; a successful full
        fetch with the same minimal data means the source really has
        no tags, which is not a failure.
        """
        if _TEMPFILE_TITLE_RE.match(meta.title or ""):
            return True
        stem = os.path.splitext(os.path.basename(remote_path))[0]
        title_missing = (not meta.title) or (meta.title == stem)
        duration_missing = (meta.duration or 0) == 0
        return title_missing and duration_missing

    # Growing-budget escalation ladder.  We start at ``HEADER_BUDGET[ext]``
    # and on insufficient-data we step UP rather than jumping to a full
    # fetch.  4× the base is enough for the long-tail of Hi-Res FLACs
    # with 1–3 MB embedded cover art; anything past that is almost
    # certainly multi-MB art for which the full fetch wins anyway.
    _GROWING_BUDGET_MULTIPLIERS = (1, 4)

    async def _fetch_zip_member(remote_path: str) -> bytes:
        """Fetch one member of a remote archive (.zip/.lha/.lzh): download the
        outer archive via the local remote-cache (once per archive), then
        extract the member.  ``remote_path`` is ``<archive_rel>::<member>``."""
        arc_rel, member = remote_path.split("::", 1)

        def _read() -> bytes:
            from soniqboom.core.remote_cache import get_cache
            # Fetch errors (network) propagate as-is; a member that fails
            # to read out of the LOCAL archive copy is a permanent defect
            # of the archive and gets the distinctive wrapper type.
            # Members inherit the OUTER archive's listing size — use it to
            # drop a stale cached archive before extracting from it.
            arc_size = entry_stat_map.get(remote_path, (0, 0.0))[0]
            get_cache().validate_size(scan_root, arc_rel, arc_size)
            local_archive = get_cache().fetch(scan_root, arc_rel, source,
                                              lane="scan")
            # Memoize (per archive) whether this is a PC program bundle — a
            # DOS ``MZ`` .exe/.com in the module's own directory.  Computed
            # here where the archive is already local: one raw-namelist scan
            # per archive, not per member.  _process_one reads it to veto a
            # lenient uade admit for a PC data payload (e.g. a demo's X.dat
            # that matched PaulRobotham purely by extension).
            _pc_key = _pc_memo_key(remote_path)
            if _pc_key not in _arc_pc_exe:
                _arc_pc_exe[_pc_key] = archive.has_dos_executable(
                    local_archive, _pc_key[1])
            try:
                return archive.read_member(local_archive, member)
            except Exception as exc:
                raise _PermanentMemberError(str(exc)) from exc

        return await loop.run_in_executor(None, _read)

    async def _fetch_partial(remote_path: str, budget: int) -> bytes:
        """One stage of partial fetch.  Logs the actual bytes returned
        so the operator can see whether the file was smaller than the
        budget (early-EOF) or the server delivered the full request.
        """
        if "::" in remote_path:        # ZIP member — fetch the whole member
            return await _fetch_zip_member(remote_path)
        return await loop.run_in_executor(
            None, lambda: source.read_partial(
                remote_path, budget, lane="scan",
            ),
        )

    async def _fetch_full(remote_path: str) -> bytes:
        if "::" in remote_path:
            return await _fetch_zip_member(remote_path)
        return await loop.run_in_executor(
            None, lambda: source.read_file(remote_path, lane="scan"),
        )

    # One-way latch flipped when the extraction pool dies: remaining
    # queued tasks must not keep DOWNLOADING files nobody can extract
    # (pre-fix, ~19K orphaned siblings pulled 22 GB of archives against
    # a broken pool).  List-wrapped so the closure can assign it.
    _pool_dead = [False]
    # Sidecar bookkeeping: archives that produced at least one indexed
    # track this scan, and archives whose member fetches failed (the
    # latter must never be barren-marked off a transient outage).
    _indexed_arcs: set[str] = set()
    _failed_fetch_arcs: set[str] = set()
    # Per-(archive, member-dir) "is this a PC program bundle (holds a DOS MZ
    # .exe/.com in that dir)?" memo, populated in _fetch_zip_member (where the
    # archive is already local) and read in _process_one to veto a lenient
    # uade admit for a PC data payload misindexed by extension (e.g. a demo's
    # X.dat).  Keyed by member dir — see _pc_memo_key — so a DOS tool in one
    # subfolder can't taint modules in a sibling subfolder.
    _arc_pc_exe: dict[tuple[str, str], bool] = {}

    async def _process_one(remote_path: str) -> None:
        nonlocal track_count
        if _pool_dead[0]:
            _progress.errors += 1
            _progress.processed += 1
            return
        # Honour the pause flag BEFORE acquiring the semaphore so a
        # paused scan doesn't tie up the in-flight window with workers
        # parked on the gate — let other slots stay free for any
        # higher-priority work that comes in.
        await _await_resume()
        async with sem:
            # Re-check after sem acquisition: a pause could have been
            # requested while we were queueing.  Cheap; resumes
            # instantly when running.
            await _await_resume()
            # Decide the fetch strategy based on extension + listing size.
            #   * ``HEADER_BUDGET[ext] is None`` → must fetch full.
            #   * File smaller than ~2× base budget → cheaper to fetch
            #     full (ABOR overhead exceeds the bytes saved).
            #   * Else → growing partial: try base budget, then 4×,
            #     then full.  Each stage extracts and re-checks the
            #     "looks incomplete" heuristic; we only escalate if
            #     the current stage's bytes didn't satisfy mutagen.
            from soniqboom.core.metadata import HEADER_BUDGET as _HB
            ext = os.path.splitext(remote_path.lower())[1]
            base_budget = _HB.get(ext)
            listing_size = entry_stat_map.get(remote_path, (0, 0.0))[0]

            track_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scan_root}:{remote_path}"))
            parent_dir = str(PurePosixPath(remote_path).parent) if "/" in remote_path else scan_root

            # Build the budget ladder: a list of (label, budget_bytes
            # or None) to try in order.  ``None`` budget = full fetch.
            stages: list[tuple[str, int | None]] = []
            if "::" in remote_path:
                # Archive members have NO partial path: _fetch_partial
                # short-circuits to _fetch_zip_member, so every ladder
                # stage performed the identical full member read — and a
                # permanent zipfile error (bad CRC, unsupported
                # compression method, name-vs-header mismatch) was
                # "escalated" through three identical attempts, each able
                # to re-trigger a full archive download under cache
                # pressure.  One stage, one verdict.
                stages.append(("full", None))
            elif base_budget is None or (listing_size and listing_size <= base_budget * 2):
                stages.append(("full", None))
            else:
                for mult in _GROWING_BUDGET_MULTIPLIERS:
                    b = base_budget * mult
                    if listing_size and b >= listing_size:
                        # No point fetching more than the file size — promote
                        # this stage straight to full so we benefit from the
                        # simpler RETR (no ABOR dance).
                        stages.append(("full", None))
                        break
                    stages.append((f"{b//1024}KB", b))
                else:
                    stages.append(("full", None))

            file_data: bytes | None = None
            result = None
            was_partial = False
            for stage_idx, (label, stage_budget) in enumerate(stages):
                with _phase_lock:
                    _phase_counts["download"] += 1
                try:
                    if stage_budget is None:
                        file_data = await _fetch_full(remote_path)
                        was_partial = False
                    else:
                        file_data = await _fetch_partial(remote_path, stage_budget)
                        was_partial = True
                except Exception as exc:
                    with _phase_lock:
                        _phase_counts["download"] -= 1
                    # If a PARTIAL fetch fails (e.g. ABOR glitch), step
                    # to the next stage — don't drop the file.  If the
                    # FULL fetch fails, we're out of options.
                    if stage_budget is None:
                        log.error("Download failed %s: %s", remote_path, exc)
                        if ("::" in remote_path
                                and not isinstance(exc, _PermanentMemberError)):
                            # Network failure — shield this archive from
                            # barren marking.  Permanent member defects
                            # deliberately stay eligible: an archive whose
                            # every member is unreadable IS barren.
                            _failed_fetch_arcs.add(remote_path.split("::", 1)[0])
                        _progress.errors += 1
                        _progress.processed += 1
                        if on_progress and (
                            _progress.processed % PROGRESS_EVERY == 0
                            or _progress.processed == total
                        ):
                            await on_progress(_progress)
                        return
                    log.warning(
                        "Partial fetch %s failed for %s: %s — escalating",
                        label, remote_path, exc,
                    )
                    continue
                with _phase_lock:
                    _phase_counts["download"] -= 1
                    _phase_counts["extract"] += 1
                try:
                    _pc_arc = (
                        _arc_pc_exe.get(_pc_memo_key(remote_path), False)
                        if "::" in remote_path else False
                    )
                    _, result, _, _ = await loop.run_in_executor(
                        executor, _extract_one_remote,
                        file_data, remote_path, track_id, _pc_arc,
                    )
                except BrokenExecutor:
                    # Worker processes died (killed / crashed) — the pool
                    # is unusable for the rest of this scan.  Flip the
                    # latch so queued siblings stop downloading, count
                    # this file as errored, and let the scan drain to a
                    # clean, retriggerable completion.
                    if not _pool_dead[0]:
                        _pool_dead[0] = True
                        log.error(
                            "Extraction pool for %s died — aborting the "
                            "remaining files of this scan (re-Index the "
                            "share to retry)", scan_root,
                        )
                    _progress.errors += 1
                    _progress.processed += 1
                    return
                finally:
                    with _phase_lock:
                        _phase_counts["extract"] -= 1
                # Was that stage's data enough?  Only escalate on
                # PARTIAL fetches — a full fetch that returns minimal
                # data is the source's real metadata, not under-shoot.
                if not was_partial:
                    break
                bad = isinstance(result, str) or (
                    not isinstance(result, str)
                    and _extract_looks_incomplete(result, remote_path)
                )
                if not bad:
                    break
                # Escalate to next stage if any, else give up.
                if stage_idx < len(stages) - 1:
                    next_label = stages[stage_idx + 1][0]
                    log.info(
                        "Partial fetch %s insufficient for %s — escalating to %s",
                        label, remote_path, next_label,
                    )

            if isinstance(result, str):
                log.error("Metadata error %s: %s", remote_path, result)
                _progress.errors += 1
            else:
                meta: TrackMeta = result
                meta.path = f"{scan_root}:{remote_path}"
                if was_partial and meta.file_md5:
                    # A partial fetch that parsed "complete enough" still
                    # hashed TRUNCATED bytes — that md5 would be a
                    # permanently wrong Modland join key.  Only full
                    # fetches may carry one; the lazy scene-apply path
                    # simply skips tracks without an md5.
                    meta.file_md5 = None
                # Stamp mtime + file_size from the directory listing.
                # Without this, the NEXT scan would see ``stored mtime
                # == 0`` and re-extract every file, defeating the
                # whole point of the mtime-skip optimisation.  Listing
                # values are canonical (MLSD response) and don't drift
                # between the directory call and the per-file fetch.
                listing_size, listing_mtime = entry_stat_map.get(
                    remote_path, (0, 0.0),
                )
                if listing_mtime > 0:
                    meta.mtime = listing_mtime
                if listing_size > 0:
                    meta.file_size = listing_size
                if parent_dir not in hash_map:
                    new_hashes = await store_hash_lookups_batch([parent_dir])
                    hash_map.update(new_hashes)
                track, raw_art = _build_track(meta, scan_root, parent_dir, hash_map)
                if track:
                    if "::" in remote_path:
                        _indexed_arcs.add(remote_path.split("::", 1)[0])
                    # Serialise buffer growth + flush so two concurrent
                    # workers can't both observe ``len(track_buffer) ==
                    # WRITE_BATCH`` and double-flush the same chunk.
                    async with flush_lock:
                        track_buffer.append(track)
                        if raw_art:
                            art_buffer[track.id] = raw_art
                        track_count += 1
                        if len(track_buffer) >= WRITE_BATCH:
                            await _flush()

            _progress.processed += 1
            _progress.current_file = PurePosixPath(remote_path).name
            if on_progress and (
                _progress.processed % PROGRESS_EVERY == 0
                or _progress.processed == total
            ):
                await on_progress(_progress)

    # Kick off the phase-logger so we get periodic concurrency stats
    # in the log while the gather runs.  Cancel + await on exit so a
    # dangling task doesn't leak across scans.
    _phase_task = asyncio.create_task(_phase_logger(), name="scan.phase_logger")

    # Warm the shared folder-art cache for every unique parent
    # directory observed in the walk.  Runs CONCURRENTLY with the
    # main extract gather so we overlap idle FTP slots (a 1000-track
    # album walks one dir and produces one prefetch task, while the
    # extract loop saturates the pool with 1000 file fetches).
    # Bounded to a small in-flight window so we don't compete too
    # hard with the extract path for pool slots.
    #
    # DETACHED — we do NOT await this in the scan's finally block.
    # Reason: large shares can have thousands of dirs, and prefetching
    # all of them takes minutes.  Earlier code awaited with a 60 s
    # timeout, which blocked the scan-complete WebSocket broadcast
    # for 60 s on every re-index of a large share (observed on the
    # "Anime Music" share).  The prefetch is best-effort cache
    # warming; the scan should report "complete" as soon as the
    # extract work is done.  The prefetch task continues in the
    # background; if it's still running when the next scan starts,
    # both run in parallel (bounded by per-task semaphores) and the
    # cache writes are idempotent.
    #
    # We register the task in a module-level set so it isn't garbage-
    # collected while still running (asyncio holds only a weak ref to
    # tasks created by create_task).  The done_callback discards on
    # completion and logs at INFO so the stats land in the log.
    if entries:
        _art_task = asyncio.create_task(
            _prefetch_folder_art_remote(scan_root, source, entries),
            name=f"scan.art_prefetch[{scan_root}]",
        )
        _art_prefetch_tasks.add(_art_task)
        _art_task.add_done_callback(_art_prefetch_done)
    try:
        # return_exceptions so one dying task can't detach its ~20K
        # siblings from the coroutine (the pre-fix failure mode: the
        # first BrokenProcessPool killed the gather + this coroutine
        # while every sibling kept downloading archives nobody could
        # extract).  Individual failures are counted per-file inside
        # _process_one; anything that still leaks out is summarised.
        results = await asyncio.gather(
            *(_process_one(p) for p in remote_paths),
            return_exceptions=True,
        )
        leaked = [r for r in results if isinstance(r, BaseException)]
        if leaked:
            log.error(
                "Remote scan %s: %d file task(s) raised unexpectedly; "
                "first: %r", scan_root, len(leaked), leaked[0],
            )
    finally:
        _phase_task.cancel()
        try:
            await _phase_task
        except (asyncio.CancelledError, Exception):
            pass

    await _flush()
    await _async_exit_batch_mode(store)
    batch_state["entered"] = False

    # HVSC: apply per-tune durations + STIL to SID tracks now they're indexed —
    # a main-process join keyed on the cached ``sid_md5`` (worker processes run
    # an unconfigured HVSC singleton).  Covers pre-configured + auto-detected.
    try:
        from soniqboom.core.hvsc import get_hvsc
        if get_hvsc().is_configured() and any(
            str(fe.path).lower().endswith(_SID_DETECT_EXTS) for fe in entries
        ):
            from soniqboom.core.hvsc_apply import apply_hvsc_to_library
            await apply_hvsc_to_library(reload=False)
    except Exception:
        log.debug("HVSC post-scan apply failed (remote)", exc_info=True)

    # Ghost-track cleanup: any track in the store under this scan_root
    # whose path didn't appear in the live walk is a file that was
    # deleted (or moved out from under us) on the remote since the last
    # index.  Drop them so they don't show up in the library forever as
    # "phantom" entries that fail to play.
    #
    # SAFETY: only purge when the walk produced AT LEAST ONE entry.
    # An empty entries list usually means the share is unreachable
    # mid-scan (auth expired, FTP server bounced) and we'd otherwise
    # nuke the entire share's tracks.  ``len(entries) > 0`` is the
    # signal that we're looking at real ground truth, not a network
    # failure.
    #
    # SAFETY 2 (QA 2026-07-02): NEVER purge on a capped walk.  A
    # ``dir_mtime_cap`` walk PRUNES unchanged subtrees — their files are
    # absent from ``entries`` because they weren't visited, not because
    # they were deleted.  The production log showed capped freshness
    # polls planning ghosts_to_delete=44,792 on a healthy share; only
    # the scan crashing first prevented a mass index wipe.  Ghost truth
    # requires a FULL walk (manual re-index, cold start, drift sweep —
    # the latter runs every 5th poll, so real ghosts still clear fast).
    # SAFETY 3: a walk that DIED midway (``walk_completed`` False) returned
    # PARTIAL entries of unknown shape — skip entirely, same as the cap.
    # SAFETY 4 (per-subtree): a walk that completed but had per-directory
    # listing failures is trustworthy EXCEPT under the failed subtrees.  We
    # purge the ghosts that are NOT under any ``walk_failed_dirs`` path and
    # protect the rest — so one reliably-failing directory (a permission-
    # denied subdir, a symlink that 550s on LIST, a load-shed borrow timeout)
    # can no longer disable ghost cleanup for the WHOLE share indefinitely.
    if ghost_ids and len(entries) > 0 and (dir_mtime_cap is not None
                                           or not walk_completed):
        log.info(
            "Ghost cleanup for %s skipped (%s): %d absent path(s) are "
            "unverified (next clean full walk decides)",
            scan_root,
            "capped walk" if dir_mtime_cap is not None else "partial walk",
            len(ghost_ids),
        )
    if ghost_ids and len(entries) > 0 and dir_mtime_cap is None and walk_completed:
        if walk_failed_dirs:
            purge_ids = [tid for rel, tid in ghost_pairs
                         if not _rel_under_any_dir(rel, walk_failed_dirs)]
            protected = len(ghost_ids) - len(purge_ids)
        else:
            purge_ids = ghost_ids
            protected = 0
        try:
            removed = await delete_track_ids(purge_ids) if purge_ids else 0
            log.info(
                "Ghost cleanup for %s: removed %d track(s) whose remote "
                "files no longer exist%s",
                scan_root, removed,
                (f" ({protected} under failed subtree(s) protected)"
                 if protected else ""),
            )
        except Exception as exc:
            log.warning("Ghost cleanup for %s failed: %s", scan_root, exc)

    await upsert_scan_dir(scan_root, track_count_val=track_count,
                          network_share_id=share_id, status="ok")

    # Fold this scan's enumeration results into the archive sidecar:
    # newly-confirmed barren archives get remembered, revived ones get
    # unmarked, and a completed FULL walk stamps the schema fingerprint
    # (re-enabling store-synthesis after an upgrade's deep pass).
    # Skipped when the pool died mid-scan — members never got their
    # extraction attempt, so a "barren" verdict would be wrong, and the
    # deep pass must rerun.  ``known_archives`` counts as indexed: an
    # unchanged archive re-enumerated by the deep pass has its members
    # SKIPPED by classification, not re-indexed — it is not barren.
    # walk_ok gate: a partial walk enumerated who-knows-what — neither
    # barren verdicts nor the "deep pass done" schema stamp are valid.
    # ``entries`` must also be non-empty for the stamp: a full walk that
    # found nothing (share unreachable at connect) proved nothing.
    if ((enum_report or dir_mtime_cap is None)
            and walk_ok and not _pool_dead[0]):
        try:
            _upd = _ft.partial(
                _update_enum_sidecar, scan_root,
                enum_report=dict(enum_report),
                indexed_arcs=_indexed_arcs | set(known_archives),
                failed_fetch_arcs=set(_failed_fetch_arcs),
                stamp_schema=(dir_mtime_cap is None and len(entries) > 0),
            )
            await loop.run_in_executor(None, _upd)
        except Exception:
            log.debug("archive-enum sidecar update failed", exc_info=True)

    # De-registration, the final progress broadcast, and executor
    # shutdown all live in start_remote_scan's ``finally`` — they must
    # run on the abort path too, not just here on success.
    log.info("Remote scan complete: %d tracks from %s", track_count, scan_root)
    # Hand this scan's own plan back to the wrapper (and thence to the
    # freshness poller) so cadence/toast never read the racy global.
    return scan_plan


# ── Drill-down freshness ──────────────────────────────────────────────────────

async def refresh_subtree_under_root(
    root: str, subdir: str, max_files: int = 5000,
) -> dict:
    """Freshness pass for ONE folder subtree under an EXISTING scan root.

    The redesign the disabled drill-down refresh was waiting for: unlike
    ``start_scan([subdir])`` this never calls ``upsert_scan_dir`` (so browsed
    folders don't appear as top-level roots) and every track keeps its
    attribution to *root*.  New and changed files are extracted and upserted;
    index entries whose files vanished are removed.

    Safety rails:
      * subtrees above ``max_files`` are skipped (use Re-Index for those);
      * removals only happen when ``subdir`` still exists as a directory
        (an unmounted share must never mass-orphan its tracks), and are
        capped per pass — a partial walk on a flaky mount can't wipe a
        folder's index.
    """
    from soniqboom.config import settings as _settings

    loop = asyncio.get_event_loop()
    store = get_store()
    sub = str(Path(subdir).resolve())

    _failed_archives: set[str] = set()
    dir_files, walk_errors = await loop.run_in_executor(
        None, _find_audio_files, [sub], _settings.scan_zips, None, None, _failed_archives,
    )
    files_strs = [str(p) for fl in dir_files.values() for p in fl]
    _old_versions: dict[str, "dict | None"] = {}
    if len(files_strs) > max_files:
        return {"skipped": True, "checked": len(files_strs),
                "added": 0, "updated": 0, "removed": 0, "errors": 0}
    found = set(files_strs)

    # Existing index entries under this subtree (scoped to the parent root).
    prefix = sub.rstrip("/") + "/"
    existing: dict[str, dict] = {}
    # (from the root's cached sorted browse rows when they are in step — a
    # binary search instead of visiting every track of the root per click)
    from soniqboom.api.fstree import cached_ids_under
    _ids = cached_ids_under(store, path_hash(str(Path(root).resolve())), prefix)
    for tid in (_ids if _ids is not None else await get_track_ids_for_scan_root(root)):
        t = store._tracks.get(tid)
        if t:
            p_str = t.get("path") or ""
            if p_str.startswith(prefix):
                existing[p_str] = t

    mtime_size_map = {
        t["id"]: (t.get("mtime"), t.get("file_size")) for t in existing.values()
    }
    fresh, _tid_map = await loop.run_in_executor(
        None, _compute_incremental, files_strs, mtime_size_map,
    )
    to_scan = [s for s in files_strs if s not in fresh]

    added = updated = errors = 0
    _root_keys = {root, str(Path(root).resolve())}
    return await _drill_down_apply(store, root, sub, to_scan, existing, found, walk_errors,
                                   _failed_archives, _old_versions, len(files_strs), loop,
                                   registered=bool(_root_keys & set(store._scan_dirs)),
                                   root_keys=_root_keys)


async def _drill_down_apply(store, root, sub, to_scan, existing, found, walk_errors,
                            _failed_archives, _old_versions, n_checked, loop, *,
                            registered: bool = False, root_keys: "set[str] | None" = None) -> dict:
    """The second half of ``refresh_subtree_under_root``: extract, then write
    and patch the browse cache inside the root's commit window."""
    added = updated = errors = 0
    ready: list = []
    if to_scan:
        def _pdir(s: str) -> str:
            return str(Path(s.split("::")[0]).parent) if "::" in s else str(Path(s).parent)

        unique_dirs = list({_pdir(s) for s in to_scan} | {root})
        hash_map = await store_hash_lookups_batch(unique_dirs)
        for s in to_scan:
            _p, result, _sm, _lg = await loop.run_in_executor(None, _extract_one, Path(s))
            if isinstance(result, str):
                errors += 1
                continue
            track, _art = _build_track(result, root, _pdir(s), hash_map)
            if not track:
                errors += 1
                continue
            ready.append(track)

    gone = [t["id"] for p_str, t in existing.items()
            if p_str not in found and not _in_failed_archive(p_str, _failed_archives)]
    _sub_resolved = str(Path(sub).expanduser().resolve())
    prune = False
    if _sub_resolved in walk_errors:
        # Incomplete listing (a read error under this subtree) — don't prune, or
        # we'd delete tracks whose files are merely unreadable, not deleted.
        log.warning(
            "Drill-down refresh %s: scan hit read errors, file listing is "
            "incomplete — skipping prune (a clean scan will reconcile).", sub,
        )
    elif gone and Path(sub).is_dir():
        cap = max(20, len(existing) // 3)
        if len(gone) <= cap:
            prune = True
        else:
            log.warning(
                "Drill-down refresh %s: %d of %d tracks vanished — over the "
                "safety cap (%d), leaving the index untouched (full Re-Index "
                "will reconcile).", sub, len(gone), len(existing), cap,
            )

    # Writes + browse patch inside the root's commit window (clicks meanwhile
    # get the pre-commit rows, never a rebuild from a half-written store).
    from soniqboom.api.fstree import root_commit as _root_commit
    removed = 0

    def _root_removed() -> bool:
        # the folder was removed while this refresh ran: write nothing (more)
        return registered and not ((root_keys or set()) & set(store._scan_dirs))
    if _root_removed():
        return {"added": 0, "updated": 0, "removed": 0,
                "checked": n_checked, "errors": errors, "skipped": True}
    stopped = False
    with _root_commit({path_hash(root)}):
        # Small chunks with a loop turn after each: a store upsert doesn't
        # yield, so 200-row chunks blocked the loop ~1.9 s for 5,000 new files
        # (perf round 7).  The removal check runs before every chunk.
        for i in range(0, len(ready), _DRILL_WRITE_CHUNK):
            if _root_removed():
                stopped = True
                break
            chunk = ready[i : i + _DRILL_WRITE_CHUNK]
            for track in chunk:
                _prev = store.get_track(track.id)
                if _prev is None:
                    added += 1
                else:
                    updated += 1
                _old_versions.setdefault(track.id, _prev)
            await upsert_tracks_batch(chunk)
            await asyncio.sleep(0)
        if not stopped and _root_removed():
            stopped = True
        if prune and not stopped:
            from soniqboom.core.data import delete_track_ids
            for _tid in gone:
                _old_versions.setdefault(_tid, store._tracks.get(_tid))
            removed = await delete_track_ids(gone)
        if _old_versions and not stopped:
            await _refresh_browse_after_commit(store, _old_versions)
    return {"added": added, "updated": updated, "removed": removed,
            "checked": n_checked, "errors": errors, "skipped": stopped}
