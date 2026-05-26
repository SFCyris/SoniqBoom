# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stuck-request watchdog — dumps every thread's stack trace when an
HTTP request runs longer than the configured threshold.

Why
---
During an FTP-pool change in an earlier session, the server became
unresponsive: every thread parked on ``_pthread_cond_wait`` and no
request completed.  Without a stack dump we could not identify the
lock-holder, so the only path forward was ``kill -9``.

This module fixes that for the next occurrence.  It tracks every
in-flight HTTP request with its start timestamp.  A background task
polls the dict every ``POLL_INTERVAL`` seconds, and the first time it
sees an entry older than ``STUCK_THRESHOLD`` seconds it writes a
``faulthandler.dump_traceback_later``-equivalent block to the log:
every Python thread's full traceback, plus the offending request URL
and how long it's been stuck.

Tuning
------
Stream + range-GET media requests can legitimately run for tens of
seconds (cold-transcode of a 40-min DSF), so the default threshold is
**90 s** — generous enough to never alarm on real work, short enough
to catch a real deadlock before the operator kills the process.

The watchdog only fires *once per stuck-request session*; once it has
dumped, it marks that request id and waits for either completion or a
new stuck request before dumping again.  No log spam on a permanent
hang.
"""
from __future__ import annotations

import asyncio
import faulthandler
import io
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass

log = logging.getLogger("soniqboom.watchdog")

# Tunables.  Override via env for ops debugging.
POLL_INTERVAL = float(os.environ.get("SONIQBOOM_WATCHDOG_POLL", "10"))
STUCK_THRESHOLD = float(os.environ.get("SONIQBOOM_WATCHDOG_STUCK_S", "90"))

# Paths we don't watch — they're inherently long-running.  WebSocket
# upgrades stay open for the lifetime of the session, audio streams
# can run for hours.  Watching them would produce constant false
# positives and obscure a real deadlock.
_IGNORE_PREFIXES = (
    "/api/stream",
    "/api/rest/stream",   # Subsonic stream
    "/api/library/ws",
    "/api/admin/cache/conversion/stream",  # SSE
)


@dataclass
class _InflightEntry:
    request_id: int
    path: str
    method: str
    start_ts: float
    client: str
    dumped: bool = False        # have we already logged a stack for this?


# Module state.  The dict + lock pair is cheap — request rate is in
# the tens-per-second range at most, far below contention territory.
_inflight: dict[int, _InflightEntry] = {}
_inflight_lock = threading.Lock()
_next_id = 0
_task: asyncio.Task | None = None


def _alloc_id() -> int:
    global _next_id
    with _inflight_lock:
        _next_id += 1
        return _next_id


def begin_request(method: str, path: str, client: str = "") -> int | None:
    """Register an incoming request.  Returns a token to pass to
    :func:`end_request`, or ``None`` if the path is on the ignore list.
    """
    if any(path.startswith(p) for p in _IGNORE_PREFIXES):
        return None
    rid = _alloc_id()
    entry = _InflightEntry(
        request_id=rid, path=path, method=method,
        start_ts=time.monotonic(), client=client,
    )
    with _inflight_lock:
        _inflight[rid] = entry
    return rid


def end_request(token: int | None) -> None:
    """Release the watchdog slot for a completed request.

    Safe to call with ``None`` (matches the begin contract).
    """
    if token is None:
        return
    with _inflight_lock:
        _inflight.pop(token, None)


def _dump_all_thread_stacks() -> str:
    """Capture every Python thread's stack trace as a single string."""
    frames = sys._current_frames()  # noqa: SLF001 — diagnostic only
    name_by_id = {t.ident: t.name for t in threading.enumerate()}
    buf = io.StringIO()
    for tid, frame in frames.items():
        name = name_by_id.get(tid, "?")
        buf.write(f"\n--- Thread {name} (id={tid}) ---\n")
        traceback.print_stack(frame, file=buf)
    return buf.getvalue()


def _scan_for_stuck_requests() -> list[_InflightEntry]:
    """Return inflight entries older than the threshold that we
    haven't yet dumped a stack for.
    """
    now = time.monotonic()
    stuck: list[_InflightEntry] = []
    with _inflight_lock:
        for entry in _inflight.values():
            age = now - entry.start_ts
            if age >= STUCK_THRESHOLD and not entry.dumped:
                entry.dumped = True
                stuck.append(entry)
    return stuck


async def _watchdog_loop() -> None:
    """Background poller.  Logs full thread dump when a request has
    been in-flight longer than the threshold.

    Runs forever — the lifespan shutdown handler cancels the task.
    """
    log.info(
        "deadlock watchdog armed: poll=%.1fs, threshold=%.1fs",
        POLL_INTERVAL, STUCK_THRESHOLD,
    )
    # faulthandler writes to stderr by default; if the server runs
    # detached (no terminal) those bytes go to /dev/null.  We still
    # call it because it can produce a C-level traceback for native
    # extensions that Python's ``sys._current_frames`` misses (e.g.
    # threads blocked inside ``pyatv`` or ``pyftpd`` C accelerators).
    try:
        faulthandler.enable()
    except Exception:  # pragma: no cover — already enabled, or no stderr
        pass

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            stuck = _scan_for_stuck_requests()
            if not stuck:
                continue
            # Log one consolidated event per scan rather than one
            # per stuck request — keeps the log compact when the
            # whole server is wedged (which is exactly when we want
            # to read the log).
            ages = sorted(
                (time.monotonic() - e.start_ts) for e in stuck
            )
            lines = [
                f"DEADLOCK SUSPECTED: {len(stuck)} request(s) stuck "
                f">{int(STUCK_THRESHOLD)}s",
            ]
            for e in stuck:
                age = time.monotonic() - e.start_ts
                lines.append(
                    f"  [{e.request_id}] {e.method} {e.path}  "
                    f"client={e.client}  age={age:.1f}s"
                )
            lines.append("--- All-thread stack dump ---")
            lines.append(_dump_all_thread_stacks())
            log.error("\n".join(lines))
        except asyncio.CancelledError:
            log.info("deadlock watchdog stopped")
            return
        except Exception as exc:  # pragma: no cover — paranoia
            log.exception("watchdog loop error (continuing): %s", exc)


def start() -> None:
    """Spin up the background watchdog task.  Idempotent."""
    global _task
    if _task is not None and not _task.done():
        return
    loop = asyncio.get_running_loop()
    _task = loop.create_task(_watchdog_loop(), name="soniqboom.watchdog")


def stop() -> None:
    """Cancel the watchdog task — called from lifespan shutdown."""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None


def get_inflight_snapshot() -> list[dict]:
    """Return a snapshot of currently in-flight requests for the
    admin status endpoint.  Read-only; safe to call from any thread.
    """
    now = time.monotonic()
    with _inflight_lock:
        return [
            {
                "id": e.request_id,
                "method": e.method,
                "path": e.path,
                "client": e.client,
                "age_s": round(now - e.start_ts, 2),
                "dumped": e.dumped,
            }
            for e in _inflight.values()
        ]
