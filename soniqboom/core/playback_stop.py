# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The stop of the playback work: renders, transcodes, pumps, prewarms.

Run by the server's stop (``main.shutdown``) and before the source install's
in-place restart (``main.quiesce_for_exec``) — the process ends right after
either (a signal re-raised once the stop returns, or ``os.execv``), and
nothing it started may outlive it: a renderer left running is re-parented to
init and goes on writing its temp file.  ``stop_playback_work``:

  1. closes ``forksafe.spawn`` (a task still ending can't start a renderer);
  2. cancels the playback tasks (their own cleanup kills their renderer and
     deletes their temp file) and waits briefly for them;
  3. sends SIGTERM to every process ``forksafe.spawn`` started on this loop
     that still runs, and to the processes below each (``uade123``'s
     ``uadecore``), waits, then SIGKILLs what is left (a process below one
     only while it still runs the same command) — and waits for each it
     started, so none stays a zombie;
  4. removes this image's render temp folder (``render_tmp.clear_own``).

Bounded: ``task_wait + term_wait + kill_wait`` seconds at most, plus one
process listing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from soniqboom.core import forksafe, render_tmp

log = logging.getLogger(__name__)


def _playback_tasks() -> "set[asyncio.Task]":
    """The tasks of renders, transcodes, pumps and prewarms on this loop."""
    found: set = set()
    st = sys.modules.get("soniqboom.api.stream")
    if st is not None:
        found.update(getattr(st, "_PREEMPTIBLE", {}).keys())
        for e in list(getattr(st, "_INFLIGHT_TRANSCODES", {}).values()):
            found.add(e.get("pump_task") if isinstance(e, dict) else None)
        found.update(getattr(st, "_SID_PREWARM_RENDERS", {}).values())
        found.update(getattr(st, "_prewarm_tasks", {}).values())
        found.update(getattr(st, "_TRANSCODE_FILLS", {}).values())
        found.update(getattr(st, "_BG_TASKS", ()))
    cc = sys.modules.get("soniqboom.core.conversion_cache")
    if cc is not None:
        found.update(getattr(cc, "_detached_renders", ()))
        found.update(getattr(cc, "_bg_render_strong", ()))
    try:
        loop = asyncio.get_running_loop()
        me = asyncio.current_task()
    except RuntimeError:
        return set()
    return {t for t in found
            if isinstance(t, asyncio.Future) and not t.done() and t is not me
            and t.get_loop() is loop}


def _descendants(pids: "list[int]") -> "dict[int, str]":
    """Every process below ``pids``: ``{pid: command line}``
    (``procinfo.process_table`` — ``/proc`` on Linux, else a fork-free ``ps``)."""
    if not pids:
        return {}
    from soniqboom.core import procinfo
    table = procinfo.process_table()
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _st, _cmd) in table.items():
        kids.setdefault(ppid, []).append(pid)
    seen: set[int] = set(pids)
    stack, below = list(pids), {}
    while stack:
        for c in kids.get(stack.pop(), ()):
            if c not in seen:
                seen.add(c)
                below[c] = table[c][2]
                stack.append(c)
    return below


def _still_same(procs: "dict[int, str]") -> "list[int]":
    """Those of ``procs`` (pid → command line when listed) still running the
    same command — a pid that ended meanwhile, maybe reused, is left alone."""
    if not procs:
        return []
    from soniqboom.core import procinfo
    table = procinfo.process_table()
    return [pid for pid, cmd in procs.items()
            if pid in table and table[pid][1] != "Z" and table[pid][2] == cmd]


def _signal(pids, sig) -> int:
    n = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            n += 1
        except OSError:
            pass
    return n


async def _wait_procs(procs: list, timeout: float) -> list:
    """Wait (reap) up to ``timeout`` s; returns the processes still running."""
    if not procs:
        return []
    waits = {}
    for p in procs:
        try:
            waits[asyncio.ensure_future(p.wait())] = p
        except Exception:                               # noqa: BLE001 (another loop)
            pass
    if waits:
        done, pending = await asyncio.wait(list(waits), timeout=max(0.0, timeout))
        for f in pending:
            f.cancel()
        for f in done:
            if not f.cancelled():
                f.exception()
    return [p for p in procs if getattr(p, "returncode", None) is None]


async def stop_playback_work(*, task_wait: float = 1.0, term_wait: float = 1.5,
                             kill_wait: float = 1.0) -> dict:
    """Stop every render, transcode, pump and prewarm (see the module
    docstring).  Returns what it did, for the log."""
    t0 = time.monotonic()
    forksafe.close_spawns()
    tasks = _playback_tasks()
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=task_wait)
    procs = [p for p in forksafe.live_processes() if getattr(p, "returncode", None) is None]
    pids = [p.pid for p in procs if isinstance(getattr(p, "pid", None), int)]
    below = await asyncio.to_thread(_descendants, pids) if pids else {}
    termed = _signal([*pids, *below], signal.SIGTERM)
    left = await _wait_procs(procs, term_wait)
    killed = 0
    if left:
        left_pids = [p.pid for p in left]
        more = await asyncio.to_thread(_descendants, left_pids)
        below.update(more)
        killed = _signal([*left_pids, *more], signal.SIGKILL)
        left = await _wait_procs(left, kill_wait)
    # Processes below a renderer that outlived its SIGTERM (its parent gone,
    # they are init's now) and still run what they ran: SIGKILL — they are
    # this server's.
    if below:
        killed += _signal(await asyncio.to_thread(_still_same, below), signal.SIGKILL)
    # The tasks the ended renderers released finish their cleanup now.
    rest = {t for t in tasks if not t.done()}
    if rest:
        await asyncio.wait(rest, timeout=0.5)
    files, nbytes = await asyncio.to_thread(render_tmp.clear_own)
    res = {"tasks": len(tasks), "processes": len(procs) + len(below), "signalled": termed,
           "killed": killed, "still_running": len(left), "temp_files": files,
           "temp_bytes": nbytes, "ms": int((time.monotonic() - t0) * 1000)}
    log.info("Playback work stopped: %d task(s), %d renderer process(es) (%d SIGKILLed, "
             "%d still running), %d temp file(s) (%.1f MB) removed in %d ms",
             res["tasks"], res["processes"], killed, res["still_running"], files,
             nbytes / 1e6, res["ms"])
    return res
