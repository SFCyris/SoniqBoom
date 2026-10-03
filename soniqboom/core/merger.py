# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background merger — consolidates the AOF into the snapshot.

Checks every ``merger_interval`` seconds; a merge runs once the AOF holds
``merger_max_aof_mb`` MB or its oldest change is ``merger_max_age`` seconds
old (``_MergeDue``) — not for every play — and (the merger process) once
more at its stop.  A merge:
  1. Reads library.aof
  2. Loads library.json (last full snapshot)
  3. Applies the AOF entries to the snapshot (skipping any it already holds)
  4. Writes library.json.new
  5. Rotates: library.json → library.json.bak, library.json.new → library.json
  6. Drops the merged bytes from library.aof

A daemon process outside the bundled app; inside it, a task of the server
whose merges each run in a child process (``_do_merge_in_child``).
"""
from __future__ import annotations

import json
import logging
import re
import signal
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _apply_entry(state: dict, entry: dict) -> None:
    """Apply a single AOF record to the snapshot state dict."""
    op = entry.get("op")

    if op == "upsert_track":
        tid = entry["id"]
        state.setdefault("tracks", {})[tid] = entry["data"]

    elif op == "batch_upsert_tracks":
        tracks = state.setdefault("tracks", {})
        for t in entry.get("data", []):
            tracks[t["id"]] = t

    elif op == "delete_tracks":
        tracks = state.get("tracks", {})
        waveforms = state.get("waveforms", {})
        for tid in entry.get("ids", []):
            tracks.pop(tid, None)
            waveforms.pop(tid, None)

    elif op == "update_track_fields":
        t = state.get("tracks", {}).get(entry["id"])
        if t:
            t.update(entry.get("data", {}))

    elif op == "update_track_fields_batch":
        tracks = state.get("tracks", {})
        for rec in entry.get("data", []):
            tid = rec.get("id")
            t = tracks.get(tid)
            if t and isinstance(rec.get("data"), dict):
                t.update(rec["data"])

    elif op == "set_rating":
        ratings = state.setdefault("ratings", {})
        rating = entry.get("rating", 0)
        if rating <= 0:
            ratings.pop(entry["id"], None)
        else:
            ratings[entry["id"]] = rating

    elif op == "record_play":
        stats = state.setdefault("play_stats", {})
        tid = entry["id"]
        ts = entry.get("ts", int(time.time()))
        existing = stats.get(tid)
        if existing:
            existing["count"] = existing.get("count", 0) + 1
            # Same rule as TrackStore.record_play: a back-dated play (an
            # offline client's scrobble) never moves last_played backwards.
            existing["last_played"] = max(existing.get("last_played", 0) or 0, ts)
        else:
            stats[tid] = {"count": 1, "last_played": ts}

    elif op == "upsert_playlist":
        state.setdefault("playlists", {})[entry["id"]] = entry["data"]

    elif op == "delete_playlist":
        state.get("playlists", {}).pop(entry["id"], None)

    elif op == "push_history":
        history = state.setdefault("history", [])
        # Play order, not arrival order (same rule as TrackStore.push_history).
        data = entry["data"]
        ts = (data or {}).get("ts") or 0
        if history and ts < (history[-1].get("ts") or 0):
            import bisect
            history.insert(bisect.bisect_right([e.get("ts") or 0 for e in history], ts), data)
        else:
            history.append(data)
        max_h = 500
        if len(history) > max_h:
            state["history"] = history[-max_h:]

    elif op == "upsert_scan_dir":
        state.setdefault("scan_dirs", {})[entry["path"]] = entry["data"]

    elif op == "delete_scan_dir":
        state.get("scan_dirs", {}).pop(entry.get("path"), None)

    elif op == "set_config":
        state.setdefault("config", {})[entry["key"]] = entry.get("value")

    elif op == "delete_config":
        state.get("config", {}).pop(entry.get("key"), None)


def _do_merge(data_dir: Path) -> int:
    """``_do_merge_locked`` under ``persistence.library_files_locked`` — a
    merge never interleaves with a snapshot write or a start's load, in this
    process or another."""
    from soniqboom.core.persistence import library_files_locked
    with library_files_locked(data_dir):
        return _do_merge_locked(data_dir)


def _parse_aof(data: bytes) -> list[tuple[int, dict]]:
    """The AOF records in ``data`` as ``(end offset, record)`` — a corrupt
    line is skipped with a warning."""
    from soniqboom.core.persistence import loads_json
    out: list[tuple[int, dict]] = []
    pos = 0
    for raw_line in data.split(b"\n"):
        pos += len(raw_line) + 1
        line = raw_line.strip()
        if not line:
            continue
        try:
            out.append((pos, loads_json(line)))
        except json.JSONDecodeError:
            log.warning("Skipping corrupt AOF line: %s",
                        line[:80].decode("utf-8", errors="replace"))
    return out


def _do_merge_locked(data_dir: Path) -> int:
    """Run one merge cycle.  Returns number of entries applied.

    Robust against network-volume quirks and against dying half-way:
      • the new snapshot is written + rotated by
        ``persistence.write_library_file`` (fsync'd temp, verified, the old
        snapshot kept as the .bak, atomic ``os.replace``)
      • if the snapshot is missing (e.g. after a prior crash), fall back to
        the .bak file so we never start from an empty state
      • the snapshot records the AOF bytes it now holds
        (``persistence.AOF_MARK``) before they are dropped from the AOF — a
        merge killed in between leaves a prefix the next merge / start
        recognises and doesn't apply twice
    """
    from soniqboom.core import persistence as _p

    snapshot_path = data_dir / "library.json"
    backup_path   = data_dir / "library.json.bak"
    aof_path      = data_dir / "library.aof"

    if not aof_path.exists() or aof_path.stat().st_size == 0:
        return 0

    # Read under flock so the writer (AOFWriter._write_sync) can't append
    # between our read and the later shift+truncate.  We record the exact
    # byte length we consumed; any bytes the writer appends *after* we
    # release the lock will be preserved verbatim during the truncate step.
    initial_data = _p.read_aof(aof_path)
    size_consumed = len(initial_data)

    entries = _parse_aof(initial_data)
    if not entries:
        return 0

    # Load the current snapshot — fall back to backup if the primary is
    # missing or empty (can happen if a previous merge/shutdown wrote an
    # empty state).  Prefer whichever file has more tracks so we never
    # regress from a populated snapshot to an empty one.
    state: dict = {}
    for src in (snapshot_path, backup_path):
        candidate = _p._try_load_json(src)
        if not isinstance(candidate, dict):
            if src.exists():
                log.warning("Merger: could not read %s, trying next", src.name)
            continue
        cand_tracks = len(candidate.get("tracks", {}))
        curr_tracks = len(state.get("tracks", {}))
        if cand_tracks >= curr_tracks:
            state = candidate
            if cand_tracks > 0:
                break  # found a populated snapshot, use it

    # Entries the snapshot already holds (a merge that died after its
    # rotation, before dropping them from the AOF) are not applied again.
    held = _p.aof_merged_prefix(state, initial_data)
    todo = [e for end, e in entries if end > held]
    if not todo:
        _p.drop_aof_prefix(aof_path, held)
        log.info("Merger: dropped %d AOF bytes the snapshot already held", held)
        return 0

    for entry in todo:
        _apply_entry(state, entry)
    state[_p.AOF_MARK] = _p.aof_mark(initial_data)

    try:
        if not _p.write_library_file(data_dir, state, "Merger"):
            return 0
    except Exception as exc:
        log.error("Merger: failed to write temp snapshot: %s", exc)
        return 0
    del state

    # Remove only the prefix we consumed.  Anything the writer appended after
    # our initial read is shifted to the front of the file so it's processed
    # in the next merge cycle rather than lost.
    _p.drop_aof_prefix(aof_path, size_consumed)
    return len(todo)


# ── When a periodic merge runs ───────────────────────────────────────────────
# A merge rewrites the whole library (hundreds of MB on a six-figure library:
# a parse, an encode, an fsync'd write), so it doesn't run for every few plays:
# only once the AOF holds ``max_aof_mb`` MB (a boot replays it, so that bounds
# the replay) or its oldest change has waited ``max_age`` seconds.  The stop's
# final merge (``merger_loop``, ``stop_merger(final=True)``) runs regardless.
_MERGE_MAX_AOF_MB = 32.0
_MERGE_MAX_AGE_S = 1800.0


def _merge_limits(max_aof_mb: float | None, max_age: float | None) -> tuple[int, float]:
    """``(max AOF bytes, max age s)`` — the given values, else the settings'
    (``merger_max_aof_mb`` / ``merger_max_age``), else the defaults."""
    if max_aof_mb is None or max_age is None:
        try:
            from soniqboom.config import settings
            if max_aof_mb is None:
                max_aof_mb = getattr(settings, "merger_max_aof_mb", None)
            if max_age is None:
                max_age = getattr(settings, "merger_max_age", None)
        except Exception:                                   # noqa: BLE001
            pass
    try:
        mb = max(0.0, float(_MERGE_MAX_AOF_MB if max_aof_mb is None else max_aof_mb))
    except (TypeError, ValueError):
        mb = _MERGE_MAX_AOF_MB
    try:
        age = max(0.0, float(_MERGE_MAX_AGE_S if max_age is None else max_age))
    except (TypeError, ValueError):
        age = _MERGE_MAX_AGE_S
    return int(mb * 1024 * 1024), age


# The head of an AOF record: ``{"op": "<op>", "ts": <epoch seconds>`` (the
# writer's key order — ``AOFWriter.append``).
_AOF_HEAD_TS = re.compile(rb'\{"op": "[^"]*", "ts": (-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)')


def _aof_first_age(aof_path: Path) -> float:
    """Seconds since the AOF's first record was written (its ``ts``) — 0 when
    it can't be read.  (A play's ``ts`` is when it was played: an offline
    client's back-dated scrobble just makes the next merge come sooner.)"""
    try:
        with open(aof_path, "rb") as f:
            head = f.read(256)
    except OSError:
        return 0.0
    m = _AOF_HEAD_TS.match(head)
    if not m:
        return 0.0
    try:
        return max(0.0, time.time() - float(m.group(1)))
    except (ValueError, OverflowError):
        return 0.0


class _MergeDue:
    """Is a periodic merge due?  Called at every check of the merger loop.
    The AOF's oldest change is dated by the check that first saw the AOF
    non-empty (within one interval — no file parse) — except an AOF already
    there at this run's first check: a previous run left it (the bundled
    app's stop merges nothing, so it can outlive many short sessions), and it
    is as old as its first record (``_aof_first_age``), not as this run."""

    def __init__(self, aof_path: Path, max_bytes: int, max_age: float,
                 clock=time.monotonic, first_age=_aof_first_age) -> None:
        self.aof_path = aof_path
        self.max_bytes = max_bytes
        self.max_age = max_age
        self.clock = clock
        self.first_age = first_age
        self.first_check = True
        self.pending_since: float | None = None

    def __call__(self) -> bool:
        first, self.first_check = self.first_check, False
        try:
            size = self.aof_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            self.pending_since = None
            return False
        now = self.clock()
        if self.pending_since is None:
            self.pending_since = now - (self.first_age(self.aof_path) if first else 0.0)
        return size >= self.max_bytes or now - self.pending_since >= self.max_age

    def merged(self) -> None:
        """A merge ran — what it left (written meanwhile) is dated from the
        next check."""
        self.pending_since = None


def merger_loop(data_dir_str: str, interval: int = 120,
                max_aof_mb: float | None = None, max_age: float | None = None) -> None:
    """Main loop for the background merger process.

    Designed to be the target of ``multiprocessing.Process``.  Checks every
    ``interval`` seconds and merges when one is due (``_MergeDue``); merges
    once more at its stop.

    Uses a ``threading.Event`` for the wait between merges so SIGTERM /
    SIGINT / SIGHUP can wake us immediately — ``time.sleep()`` is NOT
    interrupted by signals on Python 3.5+ (PEP 475: EINTR is auto-retried
    transparently).  The previous implementation sat in ``time.sleep(120)``
    ignoring SIGTERM, so the parent's ``join(timeout=3)`` would always
    time out and we'd be SIGKILL'd a few seconds later — the final merge
    promised in the comment below never ran.
    """
    import threading as _threading

    data_dir = Path(data_dir_str)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

    stop_event = _threading.Event()

    def _handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    # macOS terminal-close sends SIGHUP — previously this killed the merger
    # without the final merge running, which left the AOF un-applied.
    signal.signal(signal.SIGHUP, _handle_signal)

    max_bytes, age = _merge_limits(max_aof_mb, max_age)
    due = _MergeDue(data_dir / "library.aof", max_bytes, age)
    log.info("Merger started (dir=%s, interval=%ds, merges at %.0f MB or %.0f s)",
             data_dir, interval, max_bytes / 1048576, age)

    while not stop_event.is_set():
        # ``Event.wait`` returns True the moment the signal handler sets
        # the flag; otherwise it returns False after ``interval`` seconds.
        if stop_event.wait(interval):
            break
        if not due():
            continue
        try:
            n = _do_merge(data_dir)
            due.merged()
            if n:
                log.info("Merger: applied %d AOF entries", n)
        except Exception:
            log.exception("Merger error")

    try:
        n = _do_merge(data_dir)
        if n:
            log.info("Merger final: applied %d AOF entries", n)
    except Exception:
        log.exception("Merger final merge error")

    log.info("Merger stopped")


def _is_bundled() -> bool:
    """True when running inside the Nuitka-built .app bundle.

    There the merger is a task of the server (``merger_loop_async``) rather
    than its own long-lived process: its schedule and its stop are the
    server's (the app stops and starts the server in one process,
    ``stop_merger(final=False)``).  Each merge still runs in a child process
    (``_do_merge_in_child``) forked from the scan workers' forkserver.
    """
    import sys
    return (
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or "Contents/MacOS" in (sys.executable or "")
    )


# ── The bundled app's merges, in a child process ─────────────────────────────
# A merge parses and re-encodes the whole library.json.  Both are single C
# calls that hold the GIL from start to end, so in a thread of the server they
# froze its event loop — every request, every stream chunk — for their whole
# length (a 125 MB library: one 0.6 s stall per merge; ~1.5 s at 280 MB).  A
# child process has its own GIL.  It is forked from the multiprocessing
# forkserver the scan pools use (``scanner.prepare_worker_forkserver`` — a
# small process that never used the network; forking the server itself can
# crash in Network.framework's fork handler, see ``core/forksafe.py``), one
# child per merge, gone (with its memory) when the merge is done.  The server
# holds ``library_files_lock`` throughout, the child the cross-process
# ``_library_flock``.  Should no child run (the forkserver can't be reached),
# the merge runs in the server's thread as before.

class _CollectLog(logging.Handler):
    """The merge child's log records, sent back to the server's log."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + logging.Formatter().formatException(record.exc_info)
        self.records.append((record.name, record.levelno, msg))


def _merge_child(data_dir_str: str) -> tuple[int, list[tuple[str, int, str]]]:
    """The merge child's work: ``_do_merge_locked`` under the cross-process
    lock.  Returns the entries applied and the log records it made."""
    from soniqboom.core.persistence import _library_flock
    data_dir = Path(data_dir_str)
    handler = _CollectLog()
    logger = logging.getLogger("soniqboom")
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    n = 0
    try:
        with _library_flock(data_dir):
            n = _do_merge_locked(data_dir)
    except Exception:                                       # noqa: BLE001
        log.exception("Merger (child) error")
    finally:
        logger.removeHandler(handler)
    return n, handler.records


# A merge child that hasn't finished after this long is stuck (a share that
# stopped answering, a wedged forkserver): it is killed and the cycle skipped —
# safe at any point (``persistence.AOF_MARK``, ``drop_aof_prefix``) — rather
# than holding ``library_files_lock`` (and with it the app's Stop/Start) for
# good.  A merge of a 125 MB library takes ~1 s.
_MERGE_CHILD_TIMEOUT_S = 600.0


def _run_merge_child(data_dir: Path) -> "tuple[int, list] | None":
    """``_merge_child`` in a child process forked from the forkserver; None
    when no child could run it."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as _Timeout
    try:
        pool = ProcessPoolExecutor(max_workers=1,
                                   mp_context=multiprocessing.get_context("forkserver"))
    except Exception as exc:                                # noqa: BLE001
        log.warning("Merger: no child process for the merge (%s) — merging in the server", exc)
        return None
    wait = True
    try:
        return pool.submit(_merge_child, str(data_dir)).result(timeout=_MERGE_CHILD_TIMEOUT_S)
    except _Timeout:
        log.warning("Merger: the merge's child process is stuck after %.0f s — killed, "
                    "merge skipped", _MERGE_CHILD_TIMEOUT_S)
        for proc in list((getattr(pool, "_processes", None) or {}).values()):
            try:
                proc.kill()
            except Exception:                               # noqa: BLE001
                pass
        wait = False
        return 0, []
    except Exception as exc:                                # noqa: BLE001
        log.warning("Merger: the merge's child process failed (%r) — merging in the server", exc)
        return None
    finally:
        pool.shutdown(wait=wait, cancel_futures=True)


def _do_merge_in_child(data_dir: Path) -> int:
    """One merge of the bundled app's merger (see above).  Returns the
    entries applied."""
    from soniqboom.core import persistence as _p
    try:
        if (data_dir / "library.aof").stat().st_size == 0:
            return 0
    except OSError:
        return 0
    with _p.library_files_lock:
        res = _run_merge_child(data_dir)
        if res is None:
            with _p._library_flock(data_dir):
                return _do_merge_locked(data_dir)
    n, records = res
    for name, level, msg in records:
        logging.getLogger(name).log(level, "%s", msg)
    return n


async def merger_loop_async(data_dir: Path, interval: int = 120, stop_event=None,
                            max_aof_mb: float | None = None, max_age: float | None = None):
    """Run the merger inside the parent process as an asyncio task.

    Used in the bundled (Nuitka) deployment (``_is_bundled``).  Checks every
    ``interval`` seconds and merges when one is due (``_MergeDue``); each
    merge runs in a child process (``_do_merge_in_child``), waited for in a
    thread, so the event loop stays responsive.
    """
    import asyncio as _aio
    max_bytes, age = _merge_limits(max_aof_mb, max_age)
    due = _MergeDue(data_dir / "library.aof", max_bytes, age)
    log.info("Merger (async) started (dir=%s, interval=%ds, merges at %.0f MB or %.0f s)",
             data_dir, interval, max_bytes / 1048576, age)

    async def _sleep_or_stop(secs: float) -> bool:
        if stop_event is None:
            await _aio.sleep(secs)
            return False
        try:
            await _aio.wait_for(stop_event.wait(), timeout=secs)
            return True  # stop requested
        except _aio.TimeoutError:
            return False

    try:
        while True:
            if await _sleep_or_stop(interval):
                break
            if not due():
                continue
            try:
                n = await _aio.to_thread(_do_merge_in_child, data_dir)
                due.merged()
                if n:
                    log.info("Merger (async): applied %d AOF entries", n)
            except Exception:
                log.exception("Merger (async) error")
    finally:
        # Final merge so no AOF entries are left unmerged — unless the stop
        # asked for none (``stop_merger(final=False)``: the server's stop,
        # whose AOF flush the next start replays anyway).
        me = _aio.current_task()
        if me is None or getattr(me, "_sb_final_merge", True):
            try:
                n = await _aio.to_thread(_do_merge_in_child, data_dir)
                if n:
                    log.info("Merger (async) final: applied %d AOF entries", n)
            except Exception:
                log.exception("Merger (async) final merge error")
        log.info("Merger (async) stopped")


def start_merger(data_dir: Path, interval: int = 120, *,
                 max_aof_mb: float | None = None, max_age: float | None = None):
    """Spawn the background merger.

    Returns either a ``multiprocessing.Process`` (non-bundled) or an
    ``asyncio.Task`` (bundled).  Callers should rely on
    ``stop_merger(handle)`` / inspecting ``.is_alive()`` rather than
    type-checking.  ``max_aof_mb`` / ``max_age``: when a periodic merge runs
    (``_MergeDue``) — default: the settings.
    """
    max_bytes, max_age = _merge_limits(max_aof_mb, max_age)
    max_aof_mb = max_bytes / (1024 * 1024)
    if _is_bundled():
        import asyncio as _aio
        stop_event = _aio.Event()
        task = _aio.create_task(
            merger_loop_async(data_dir, interval, stop_event, max_aof_mb, max_age),
            name="soniqboom-merger",
        )
        task._sb_stop_event = stop_event  # type: ignore[attr-defined]
        log.info("Merger started as asyncio task (bundled mode)")
        return task

    import multiprocessing
    # macOS Python 3.8+ defaults to ``spawn`` — outside the bundle that's
    # safe, but ``forkserver`` is the gold standard for safety from a
    # multithreaded uvicorn parent (the helper itself is single-threaded
    # so its fork is safe).  Fall back to default if neither works.
    try:
        ctx = multiprocessing.get_context("forkserver")
    except (ValueError, RuntimeError):
        ctx = multiprocessing
    proc = ctx.Process(
        target=merger_loop,
        args=(str(data_dir), interval, max_aof_mb, max_age),
        daemon=True,
        name="soniqboom-merger",
    )
    proc.start()
    log.info("Merger process started (pid=%d)", proc.pid)
    return proc


async def stop_merger(handle, final: bool = True) -> None:
    """Stop a merger handle returned by ``start_merger`` cooperatively.  The
    asyncio-task merger (bundled app) runs one last merge first unless
    ``final`` is False."""
    import asyncio as _aio

    if handle is None:
        return
    # asyncio.Task path
    if isinstance(handle, _aio.Task):
        handle._sb_final_merge = final  # type: ignore[attr-defined]
        stop_event = getattr(handle, "_sb_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        try:
            await _aio.wait_for(handle, timeout=10)
        except _aio.TimeoutError:
            handle.cancel()
        except Exception:
            log.exception("Async merger shutdown error")
        return
    # multiprocessing.Process path — preserve legacy behaviour
    try:
        handle.terminate()
        handle.join(timeout=5)
        if handle.is_alive():
            handle.kill()
            handle.join(timeout=2)
    except Exception:
        log.exception("Merger process shutdown error")
