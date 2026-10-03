# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Append-Only File writer for durability.

Every mutating operation on the TrackStore is recorded as a single JSON line
appended to ``library.aof``.  A background merger process periodically
consolidates the AOF into the full snapshot (``library.json``).

The main process never serialises the full state — only one-line appends.
"""
from __future__ import annotations

import asyncio
import atexit
import fcntl
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class _JournalEnded(OSError):
    """The journal ended (``end_journal``) while a write waited for the AOF's lock."""


class AOFWriter:
    """Buffered AOF writer.  Flushes to disk periodically or on demand."""

    # Cap on the number of consecutive flush failures we tolerate silently
    # before logging at error level — the previous code would happily retain
    # the buffer forever, masking a disk-full / permission problem until the
    # next process restart.
    _RETENTION_WARN_THRESHOLD = 3

    def __init__(self, path: Path, flush_interval: float = 0.1) -> None:
        self._path = path
        self._flush_interval = flush_interval
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._fd: int | None = None
        # In-process write lock: fcntl.flock(LOCK_EX) on the *same* fd is
        # re-entrant within a process, so the executor thread that drives
        # async ``flush()`` and the main thread that runs ``flush_sync()``
        # could otherwise race on ``os.write``.  Cross-process serialisation
        # against the merger still rides on flock as before.  Held across a
        # whole flush — the copy of the buffer, the write AND the removal of
        # what was written (``_flush_locked``) — and by every other removal
        # (``discard_buffered``, ``seal``), so two flushes never write the
        # same records (a timed-out stop and the snapshot's, a cancelled
        # auto-flush and the stop's).  Re-entrant: ``stop`` / ``seal`` hold
        # it around their flush.  ``append`` takes no lock (on the loop; a
        # list append is atomic and removals only take from the front).
        self._write_lock = threading.RLock()
        # ``close_intake``: new records go to ``_late_path``; ``seal`` /
        # ``end_journal``: nothing more is written to the AOF at all.
        self._intake_closed = False
        self._sealed = False
        self._late_path: Path | None = None
        self._late_count = 0
        self._late_lock = threading.Lock()  # (never held across a flock wait)
        # Event-driven auto-flush: ``append`` sets this so the background
        # loop wakes immediately instead of polling every ``_flush_interval``
        # seconds.  Idle servers no longer burn 10 wake-ups/second checking
        # an empty buffer.
        self._wake = asyncio.Event()
        # Track how many consecutive flush attempts have failed.  Surfaced
        # via ``buffer_depth`` so an admin endpoint can detect a stuck AOF.
        self._consecutive_failures = 0
        atexit.register(self._atexit_flush)

    @property
    def buffer_depth(self) -> int:
        """Number of AOF records currently queued in-memory.

        Tracked so admin/health endpoints can detect a stalled writer —
        usually a sign the disk is full or the merger has the flock stuck.
        """
        return len(self._buffer)

    def _ensure_fd(self) -> int:
        if self._fd is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(
                str(self._path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o644,
            )
        return self._fd

    def append(self, op: str, **kwargs: Any) -> None:
        """Buffer a single AOF record.  Non-blocking.  After ``seal`` the
        record is set aside instead (``_set_aside``)."""
        record = {"op": op, "ts": time.time(), **kwargs}
        line = json.dumps(record, default=str) + "\n"
        if self._sealed or self._intake_closed:
            self._set_aside([line])
            return
        self._buffer.append(line)
        # Wake the auto-flush loop so latency-sensitive writes (e.g.
        # ``record_play``) don't have to wait the full ``flush_interval``.
        # Setting an already-set Event is a no-op so this is safe to call
        # on every append; the loop batches whatever's buffered when it
        # actually runs.
        try:
            self._wake.set()
        except RuntimeError:
            # ``self._wake`` is bound to the loop that created it (FastAPI
            # startup).  If we get called from a context without that loop
            # the polling fallback covers us; not fatal.
            pass

    async def flush(self) -> None:
        """Write buffered records to disk in a thread to avoid blocking.

        Buffer is only drained for entries that successfully reach disk; a
        transient I/O error keeps the data queued for the next attempt.
        After ``_RETENTION_WARN_THRESHOLD`` consecutive failures the
        condition is logged at error level instead of debug, so a stuck
        AOF surfaces in production logs instead of being hidden in a
        per-attempt exception trace.  The thread both writes and drops what
        it wrote (``_flush_locked``): cancelling the awaiting task — the
        stop's ``cancel_flush_task`` while a slow write is under way — can't
        leave written records in the buffer for the stop to write again.
        """
        if not self._buffer:
            return
        # ``get_running_loop()`` over ``get_event_loop()`` — the latter
        # emits DeprecationWarning on 3.12+ when called from inside a task.
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._flush_locked)
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures > self._RETENTION_WARN_THRESHOLD:
                log.error(
                    "AOF flush failed %d consecutive times — buffer depth %d, "
                    "check disk space / permissions on %s",
                    self._consecutive_failures, len(self._buffer), self._path,
                    exc_info=True,
                )
            else:
                log.exception("AOF flush failed — retaining buffer for retry")
            return
        self._consecutive_failures = 0

    def _flush_locked(self) -> int:
        """Write the buffered records and drop them from the buffer, both
        under ``_write_lock`` in the calling thread — anything appended
        meanwhile stays for the next flush.  Raises (the records stay
        buffered) when the write fails.  A sealed writer sets them aside
        instead (``seal``).  Returns how many records were written."""
        with self._write_lock:
            n = len(self._buffer)
            if not n:
                return 0
            lines = self._buffer[:n]
            if not self._sealed:
                try:
                    self._write_sync("".join(lines))
                except _JournalEnded:
                    pass                    # ended while waiting for the AOF's lock
                else:
                    del self._buffer[:n]
                    return n
            del self._buffer[:n]
            self._set_aside(lines)
            return 0

    # Maximum time we'll wait for the merger to release its AOF flock
    # before giving up.  A blocking ``fcntl.flock(LOCK_EX)`` here would
    # hang shutdown indefinitely if the merger was mid-merge — we'd
    # rather report the failure (records stay in the buffer for the next
    # try) than block the entire process forever.
    _FLOCK_BUDGET_SECONDS = 2.5

    def _acquire_flock(self, fd: int, budget: float | None = None) -> bool:
        """Non-blocking flock with a bounded retry.  Returns True on
        success, False on budget exhaustion.  Lets shutdown stay
        responsive even when the merger has the AOF lock."""
        deadline = time.monotonic() + (budget or self._FLOCK_BUDGET_SECONDS)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.025)

    def _follow_replaced(self, held: list) -> int:
        """The locked descriptor of the file now at the AOF's path: ``held[0]``
        (flock held) if it still is that file, else that file, opened and
        locked (``held[0]`` tracks the descriptor locked at any moment, None
        while none is).  A merge drops what it folded into the snapshot by
        swapping in a new file (``persistence.drop_aof_prefix``: the rest
        written aside, then ``os.replace`` — no half-dropped AOF after a
        crash); a writer that waited on the old file's lock meanwhile would
        otherwise append to the replaced, unlinked file."""
        fd = held[0]
        for _ in range(8):
            try:
                st = os.stat(self._path)
            except FileNotFoundError:
                st = None
            cur = os.fstat(fd)
            if st is not None and (st.st_ino, st.st_dev) == (cur.st_ino, cur.st_dev):
                return fd
            fcntl.flock(fd, fcntl.LOCK_UN)
            held[0] = None
            os.close(fd)
            self._fd = None
            fd = self._ensure_fd()
            if not self._acquire_flock(fd):
                raise OSError("AOF flock contention — merger busy")
            held[0] = fd
        return fd

    def _write_sync(self, data: str) -> None:
        # In-process Lock + cross-process flock together cover both racing
        # threads (executor + main) and the separate merger process.
        # ``os.write`` may return short on slow / network-backed paths —
        # loop until the buffer is fully drained so we don't silently drop
        # the tail.
        payload = data.encode()
        with self._write_lock:
            fd = self._ensure_fd()
            if not self._acquire_flock(fd):
                # The merger has been holding the AOF flock for >2.5 s.
                # Surface this as an OSError so the caller (``flush`` /
                # ``flush_sync``) retains the buffer for the next attempt.
                raise OSError("AOF flock contention — merger busy")
            held = [fd]
            try:
                fd = self._follow_replaced(held)
                if self._sealed:
                    raise _JournalEnded("the journal ended")
                view = memoryview(payload)
                while view:
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError("short write to AOF")
                    view = view[n:]
            finally:
                if held[0] is not None:
                    fcntl.flock(held[0], fcntl.LOCK_UN)

    def discard_buffered(self, n: int) -> None:
        """Drop the first ``n`` buffered records unwritten: a snapshot that
        holds their writes was just saved (``persistence.write_snapshot_sync``
        with this writer) — appended later (the exit flush), the next start
        would apply them on top of it a second time."""
        if n > 0:
            with self._write_lock:
                del self._buffer[:n]

    def flush_sync(self) -> None:
        """Synchronous flush — used during shutdown.

        Mirrors :meth:`flush`: keeps the buffer intact if the underlying
        write raises so a failed final flush doesn't silently drop records.
        """
        if not self._buffer:
            return
        try:
            self._flush_locked()
        except Exception:
            log.exception("AOF flush_sync failed — buffer retained")

    def close_intake(self) -> None:
        """No new record into the buffer from now on — set aside instead
        (``_set_aside``); what is buffered can still be written (``seal``).
        Called on the event loop right before the stop's ``seal`` job, so a
        write landing while that job waits is never appended."""
        self._intake_closed = True

    def end_journal(self) -> None:
        """Nothing more is written to the AOF, whatever is still running: a
        flush (``seal``'s own, timed out and still waiting for a lock) sets
        its records aside instead — checked again once it holds the AOF's
        lock.  Called on the event loop before the stop releases the data
        directory."""
        self._intake_closed = True
        self._sealed = True

    def seal(self, budget: float = 0.0) -> int:
        """End this run's journal — the server's stop, right before it
        releases the data directory (``persistence.release_library``): the
        buffered records are written — a failed write is tried again for up
        to ``budget`` seconds (a merge holding the AOF) — and the descriptor
        closed; from now on nothing is written to the AOF.  A write still
        arriving (a pass or thread-pool job still ending) would otherwise
        reach the AOF only at the process's exit — after the next instance
        (or, in the bundled app, the next run) loaded it and wrote its own
        records, and replayed over them.  Such records — and any the final
        write couldn't place — go to ``library.aof.late-<time>`` beside the
        AOF instead (logged; an operator can look at them, nothing replays
        them).  Idempotent.  Returns the records set aside here."""
        deadline = time.monotonic() + max(0.0, budget)
        with self._write_lock:
            while self._buffer and not self._sealed:
                try:
                    self._flush_locked()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        log.warning("AOF: the final flush failed — %d record(s) set aside",
                                    len(self._buffer), exc_info=True)
                        break
                    time.sleep(0.1)
            with self._late_lock:
                if self._late_path is None:
                    self._late_path = (self._path.parent
                                       / f"{self._path.name}.late-{int(time.time())}")
            self._intake_closed = True
            self._sealed = True
            left = self._buffer[:]
            del self._buffer[:len(left)]
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        if left:
            self._set_aside(left)
        return len(left)

    @property
    def sealed(self) -> bool:
        return self._sealed

    def exclusive(self):
        """The writer's lock, as a context manager: while it is held no flush
        writes and nothing is removed from the buffer (other threads; it is
        re-entrant) — the shutdown snapshot holds it from its flush to the
        drop of the buffered records it holds.  ``append`` doesn't wait."""
        return self._write_lock

    def _set_aside(self, lines: list[str]) -> None:
        """Append records that must not reach the AOF any more (``seal``) to
        ``library.aof.late-<time>`` (one file per sealed writer)."""
        with self._late_lock:
            if self._late_path is None:
                self._late_path = self._path.parent / f"{self._path.name}.late-{int(time.time())}"
            first = self._late_count == 0
            self._late_count += len(lines)
            try:
                with open(self._late_path, "a", encoding="utf-8") as f:
                    f.writelines(lines)
            except OSError:
                log.exception("AOF: %d record(s) written after the journal ended were lost",
                              len(lines))
                return
        if first:
            log.warning("AOF: store write(s) after this run's journal ended (the server is "
                        "stopping) — set aside in %s, not replayed", self._late_path.name)
        else:
            log.debug("AOF: %d more late record(s) set aside in %s",
                      len(lines), self._late_path.name)

    async def start_auto_flush(self) -> None:
        """Start the event-driven flush loop.

        Each ``append()`` sets ``self._wake``; the loop waits on that event
        with a ``_flush_interval`` timeout (so a missed wake — e.g.
        cross-thread append — still gets flushed within one interval, and
        we don't block forever during shutdown).  An idle process makes
        zero wake-ups; a busy one batches whatever's buffered when the
        loop reaches the flush call.
        """
        if self._flush_task is not None:
            return

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._flush_interval,
                    )
                except asyncio.TimeoutError:
                    pass  # periodic safety wake; flush if anything queued
                # Clear before flush so any append that fires *during*
                # ``flush`` still re-wakes us for the next iteration.
                self._wake.clear()
                try:
                    await self.flush()
                except Exception:
                    log.exception("AOF flush error")

        # Recreate the wake event in case ``stop`` was called previously —
        # an old Event bound to a finished loop would raise RuntimeError on
        # ``set()`` from the new event-loop generation.
        self._wake = asyncio.Event()
        self._flush_task = asyncio.create_task(_loop())

    def stop(self) -> None:
        """Cancel the auto-flush task and close the file descriptor.

        Acquires ``_write_lock`` before closing the fd so an in-flight
        executor-thread ``_write_sync`` can't be left writing to a
        now-closed (or worse, kernel-recycled) descriptor.

        Safe to call from either the event loop or a worker thread —
        cancelling a task across threads is undefined behaviour, so
        we only cancel when we know we're on the loop; otherwise the
        caller should have cancelled it first via ``cancel_flush_task``.
        """
        # Best-effort cancel — works when on-loop, no-op (with a
        # silently-caught error) when called from a worker thread.
        if self._flush_task:
            try:
                self._flush_task.cancel()
            except RuntimeError:
                pass
            self._flush_task = None
        # The flush and the close under one hold of the lock: a second stop
        # (the snapshot's, while a timed-out first one still waits on the
        # merger's flock) waits for it and finds only what came after.
        with self._write_lock:
            self.flush_sync()
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def cancel_flush_task(self) -> None:
        """Cancel the periodic flush task without doing the sync flush.

        Call this from the event loop *before* invoking ``stop()`` from
        a worker thread — keeps Task.cancel() on the loop where it
        belongs.  Idempotent."""
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None

    def _atexit_flush(self) -> None:
        # Best-effort: if the interpreter is shutting down abnormally (signal,
        # unhandled exception) FastAPI's shutdown hook may never run, so the
        # buffered AOF records would be lost.  Drop nothing silently.
        try:
            self.flush_sync()
        except Exception:
            pass
        # If anything is still buffered (flush failed permanently — disk
        # full, EROFS, merger flock contention beyond budget) write the
        # un-drained records to ``library.aof.dropped-<ts>`` next to the
        # primary AOF so an operator can recover them later instead of
        # losing the data when the interpreter exits.
        with self._write_lock:
            left = self._buffer[:]
            del self._buffer[:len(left)]
        if left:
            try:
                tail = self._path.parent / f"{self._path.name}.dropped-{int(time.time())}"
                with open(tail, "a") as f:
                    for line in left:
                        f.write(line)
                log.error(
                    "AOF shutdown wrote %d unflushed records to %s",
                    len(left), tail,
                )
            except Exception:
                log.exception("AOF shutdown could not write dropped records")
        with self._write_lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    @property
    def path(self) -> Path:
        return self._path
