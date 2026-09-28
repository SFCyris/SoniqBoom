# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scan whose extraction pool dies (its workers crashed at start, as
Network.framework's fork handler made them after a list download on macOS)
goes on in a new pool; a file that kills or hangs its worker is the only one
lost; pools that die with nothing extracted in between (or can't be reached)
end the root with the rest counted as errors — never "nothing changed"."""
from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
import struct
import sys
import threading
import time
import wave
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from soniqboom.core import scanner


def _wav(path: Path, freq: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
                               for i in range(8000)))


class _DyingPool:
    """Accepts one job, which fails as a crashed worker's does; every later
    submit raises, as a broken ``ProcessPoolExecutor``'s does."""

    def __init__(self) -> None:
        self.broken = False
        self.shut = False

    def submit(self, fn, *args):
        if self.broken:
            raise BrokenProcessPool("the pool is dead")
        self.broken = True
        f: concurrent.futures.Future = concurrent.futures.Future()
        f.set_exception(BrokenProcessPool("a worker crashed at start"))
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut = True


@pytest.fixture
def library(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def no_dups(*a, **kw):
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", no_dups)
    root = tmp_path / "lib"
    root.mkdir()
    for i, f in enumerate((330, 440, 550, 660)):
        _wav(root / f"t{i}.wav", f)
    return root.resolve(), store_mod._store


async def test_a_dead_pool_is_replaced_once_and_every_file_indexed(library, monkeypatch):
    root, store = library
    first = _DyingPool()
    second = concurrent.futures.ThreadPoolExecutor(2)
    pools = [first, second]
    monkeypatch.setattr(scanner, "_process_pool", lambda n: pools.pop(0))
    assert await scanner._run_scan([str(root)]) is True
    assert sorted(Path(t["path"]).name for t in store._tracks.values()) == [
        "t0.wav", "t1.wav", "t2.wav", "t3.wav"]
    assert first.shut and pools == []
    assert scanner._progress.errors == 0 and scanner._progress.processed == 4


async def test_pools_that_always_die_give_the_root_up_after_three(library, monkeypatch):
    root, store = library
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_DyingPool()) or made[-1])
    assert await scanner._run_scan([str(root)]) is False
    assert not store._tracks
    assert scanner._progress.errors == 4 and scanner._progress.processed == 4
    assert scanner._progress.running is False
    assert len(made) == 3 and all(p.shut for p in made)


class _Refusing:
    """A pool whose forkserver can't be reached: every submit raises."""
    def submit(self, fn, *args):
        raise RuntimeError("did not receive acknowledgement of fd")

    def shutdown(self, wait=True, cancel_futures=False):
        pass


async def test_a_pool_that_cannot_be_reached_ends_the_root_without_raising(library, monkeypatch):
    root, store = library
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_Refusing()) or made[-1])
    assert await scanner._run_scan([str(root)]) is False
    assert scanner._progress.errors == 4 and scanner._progress.processed == 4
    assert len(made) == 3


class _PoisonPool:
    """Runs jobs in threads; submitting a ``poison`` file (space-separated
    names) kills the pool
    (it and every job still running fail, later submits raise) — or, with
    ``hang``, the poison job never finishes.  ``canary_delay``: the pool's
    first no-op job takes that long (workers slow to start); ``late``: the
    other jobs' failures arrive that many seconds after the death."""

    def __init__(self, poison: str, hang: bool = False, canary_delay: float = 0.05,
                 late: float = 0.0) -> None:
        self.poison, self.hang = poison, hang
        self.canary_delay, self.late = canary_delay, late
        self.broken = self.shut = False
        self.pending: list = []
        self.lock = threading.Lock()

    def _settle(self, f, result=None, exc=None):
        with self.lock:
            if f.done():
                return
            f.set_exception(exc) if exc is not None else f.set_result(result)

    def _fail_pending(self):
        for p in self.pending:
            self._settle(p, exc=BrokenProcessPool("a worker died"))

    def submit(self, fn, *args):
        if self.broken:
            raise BrokenProcessPool("the pool is dead")
        f: concurrent.futures.Future = concurrent.futures.Future()
        path = args[0] if args else None
        if path is not None and path.name in self.poison.split():
            if self.hang:
                return f                                    # never finishes
            self.broken = True
            if self.late:
                threading.Timer(self.late, self._fail_pending).start()
            else:
                self._fail_pending()
            self._settle(f, exc=BrokenProcessPool("a worker died"))
            return f
        self.pending.append(f)

        def run():
            time.sleep(0.05 if args else self.canary_delay)
            if self.broken:
                if not self.late:
                    self._settle(f, exc=BrokenProcessPool("a worker died"))
                return
            self._settle(f, result=fn(*args))
        threading.Thread(target=run, daemon=True).start()
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut = True


async def _scan(root, limit=60):
    """``_run_scan`` with a time limit: a loop that never ends fails the test."""
    return await asyncio.wait_for(scanner._run_scan([str(root)]), limit)


@pytest.mark.parametrize("hang", [False, True])
async def test_a_file_that_kills_or_hangs_its_worker_is_the_only_one_lost(library, monkeypatch, hang):
    root, store = library
    for i, f in enumerate((770, 880)):
        _wav(root / f"t{4 + i}.wav", f)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t2.wav", hang)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.5)
    await _scan(root)
    assert sorted(Path(t["path"]).name for t in store._tracks.values()) == [
        "t0.wav", "t1.wav", "t3.wav", "t4.wav", "t5.wav"]
    assert scanner._progress.errors == 1 and scanner._progress.processed == 6


def _six(root):
    for i, f in enumerate((770, 880)):
        _wav(root / f"t{4 + i}.wav", f)


@pytest.mark.parametrize("hang", [False, True])
async def test_culprits_found_one_after_another_do_not_give_the_root_up(library, monkeypatch, hang):
    """Each culprit found alone is progress: three in a row (with files still
    to come after them) are three errors, not the end of the root."""
    root, store = library
    _six(root)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t1.wav t2.wav t3.wav", hang)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.5)
    monkeypatch.setattr(scanner, "INFLIGHT", 3)
    await _scan(root)
    assert sorted(Path(t["path"]).name for t in store._tracks.values()) == ["t0.wav", "t4.wav", "t5.wav"]
    assert scanner._progress.errors == 3 and scanner._progress.processed == 6


async def test_every_file_that_kills_its_worker_is_found_alone(library, monkeypatch, caplog):
    """Every file of the folder kills its worker: each is tried alone and
    counted as the culprit — finding one is progress, so the folder is not
    given up after three pools (order-independent: all files alike)."""
    import logging
    root, store = library
    _six(root)
    made = []
    everything = " ".join(f"t{i}.wav" for i in range(6))
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool(everything)) or made[-1])
    with caplog.at_level(logging.ERROR, logger=scanner.log.name):
        await _scan(root)
    assert not store._tracks
    assert scanner._progress.errors == 6 and scanner._progress.processed == 6
    assert sum("kills its worker" in r.getMessage() for r in caplog.records) == 6
    assert not any("died again" in r.getMessage() for r in caplog.records)


async def test_a_suspect_runs_alone_only_once_the_new_pools_workers_start(library, monkeypatch):
    """Workers slow to start: a suspect sent before the canary ran would die
    with it, unblamed, pool after pool — until the root was given up."""
    root, store = library
    _six(root)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t2.wav", canary_delay=0.3)) or made[-1])
    await _scan(root)
    assert len(store._tracks) == 5
    assert scanner._progress.errors == 1 and scanner._progress.processed == 6


async def test_failures_from_a_replaced_pool_do_not_kill_the_new_one(library, monkeypatch):
    """The dead pool's other jobs report their failure late: they are
    requeued, never counted as the new pool dying — one pool per death."""
    root, store = library
    _six(root)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t2.wav", late=0.3)) or made[-1])
    await _scan(root)
    assert len(store._tracks) == 5
    assert scanner._progress.errors == 1 and scanner._progress.processed == 6
    assert len(made) == 3           # the first pool, the one t2 killed alone, the last


class _RefuseFiles:
    """Its workers start (the canary runs); it takes ``take`` files, then
    every file submit raises."""
    def __init__(self, exc=BrokenProcessPool, take: int = 0) -> None:
        self.exc, self.take = exc, take

    def submit(self, fn, *args):
        if args:
            if self.take <= 0:
                raise self.exc("no")
            self.take -= 1
        f: concurrent.futures.Future = concurrent.futures.Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@pytest.mark.parametrize("exc", [RuntimeError, OSError, BrokenProcessPool])
async def test_pools_that_start_but_take_no_file_give_the_root_up(library, monkeypatch, exc):
    root, store = library
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_RefuseFiles(exc)) or made[-1])
    await _scan(root, limit=20)
    assert not store._tracks
    assert scanner._progress.errors == 4 and scanner._progress.processed == 4
    assert len(made) == 3


async def test_pools_that_die_after_indexing_files_do_not_give_the_root_up(library, monkeypatch):
    """Each pool indexes three files, then dies: that is progress every time."""
    root, store = library
    for i in range(8):
        _wav(root / f"u{i}.wav", 300 + 20 * i)
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_RefuseFiles(take=3)) or made[-1])
    await _scan(root, limit=20)
    assert len(store._tracks) == 12
    assert scanner._progress.errors == 0 and scanner._progress.processed == 12
    assert len(made) == 4


class _QueuePool:
    """Takes ``take`` files, each queued for 0.2 s before it runs, then
    refuses; ``shutdown(cancel_futures=True)`` cancels what is still queued —
    as a real pool's does."""
    def __init__(self, take: int = 2) -> None:
        self.take, self.queued = take, []

    def submit(self, fn, *args):
        f: concurrent.futures.Future = concurrent.futures.Future()
        if not args:
            f.set_result(fn())
            return f
        if self.take <= 0:
            raise BrokenProcessPool("no")
        self.take -= 1
        self.queued.append(f)

        def run():
            time.sleep(0.2)
            if f.set_running_or_notify_cancel():
                f.set_result(fn(*args))
        threading.Thread(target=run, daemon=True).start()
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        if cancel_futures:
            for f in self.queued:
                f.cancel()


async def test_queued_files_of_a_killed_pool_run_again(library, monkeypatch):
    root, store = library
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_QueuePool()) or made[-1])
    await _scan(root, limit=20)
    assert any(f.cancelled() for pool in made for f in pool.queued)    # the case under test happened
    assert len(store._tracks) == 4
    assert scanner._progress.errors == 0 and scanner._progress.processed == 4


async def test_progress_in_an_earlier_pool_does_not_keep_later_ones_going(library, monkeypatch):
    """The first pool indexes two files; the next three take none — the
    count starts again with each new pool, so the root is given up."""
    root, store = library
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_RefuseFiles(take=2 if not made else 0)) or made[-1])
    await _scan(root, limit=20)
    assert len(store._tracks) == 2
    assert scanner._progress.errors == 2 and scanner._progress.processed == 4
    assert len(made) == 4


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_on_macos_scan_workers_fork_from_the_forkserver():
    pool = scanner._process_pool(1)
    try:
        assert pool._mp_context.get_start_method() == "forkserver"
        assert pool.submit(sum, [1, 2]).result(timeout=60) == 3
    finally:
        pool.shutdown()


# ── real worker processes ─────────────────────────────────────────────────────

@pytest.fixture
def real_pools(library, monkeypatch):
    """Real process pools (the scan's own ``_process_pool`` — the forkserver
    on macOS) whose jobs go through ``_scan_crash.extract``; yields the
    library and every worker process the pools started."""
    import _scan_crash
    real = scanner._process_pool
    workers: dict = {}

    def wrapped(n):
        pool = real(n)
        submit = pool.submit

        def via_crash(fn, *args, **kw):
            fut = submit(_scan_crash.extract if fn is scanner._extract_one else fn, *args, **kw)
            workers.update(getattr(pool, "_processes", None) or {})
            return fut
        pool.submit = via_crash
        return pool
    monkeypatch.setattr(scanner, "_process_pool", wrapped)
    yield library, workers
    for p in workers.values():          # not via _kill_pool: a broken one would hang the run
        try:
            p.kill()
        except ValueError:              # closed
            pass


@pytest.mark.parametrize("n_crash", [1, 2, 3, 3])
async def test_files_that_crash_real_workers_are_the_only_ones_lost(real_pools, n_crash):
    (root, store), workers = real_pools
    for i in range(n_crash):
        (root / f"crash{i}.wav").write_bytes(b"x")
    for i in range(20):
        _wav(root / f"good{i:02d}.wav", 200 + 10 * i)
    await _scan(root, limit=120)
    names = sorted(Path(t["path"]).name for t in store._tracks.values())
    assert names == sorted(["t0.wav", "t1.wav", "t2.wav", "t3.wav"] + [f"good{i:02d}.wav" for i in range(20)])
    assert scanner._progress.errors == n_crash
    assert scanner._progress.processed == scanner._progress.total == 24 + n_crash


async def test_a_file_that_hangs_a_real_worker_is_the_only_one_lost(real_pools, monkeypatch):
    (root, store), workers = real_pools
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 3)
    (root / "hang0.wav").write_bytes(b"x")
    await _scan(root, limit=120)
    assert len(store._tracks) == 4
    assert scanner._progress.errors == 1 and scanner._progress.processed == 5
    time.sleep(0.5)
    assert workers and not [p for p in workers.values() if p.is_alive()]    # the hung one was killed


# ── process lifecycle ─────────────────────────────────────────────────────────

_FS = ("/usr/bin/python -c from multiprocessing.forkserver import main; "
       "main(3, 5, ['soniqboom'], **{'sys_path': ['/Applications/SoniqBoom.app']})")


def test_the_reaper_takes_orphaned_forkserver_trees_only():
    from soniqboom import main
    procs = {
        10: (1, _FS), 11: (10, _FS), 12: (11, _FS),          # an orphaned tree (and a grandchild)
        20: (5, _FS), 21: (20, _FS),                          # a live instance's forkserver
        30: (1, "/usr/bin/some-daemon"),                      # an unrelated orphan
        40: (1, "python -c from multiprocessing.forkserver import main; main(3, 5, ['other'])"),
        99: (1, _FS),                                         # ourselves
    }
    assert sorted(main._orphan_tree_targets(procs, me=99)) == [10, 11, 12]


def test_the_reaper_escalates_to_sigkill(monkeypatch):
    import signal
    import subprocess
    from soniqboom import main
    from soniqboom.core import forksafe
    out = f"   10     1 {_FS}\n   11    10 {_FS}\n"
    monkeypatch.setattr(forksafe, "run", lambda *a, **kw: type("R", (), {"stdout": out})())
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        if pid == 11 and sig == 0:
            raise OSError("gone")                             # 11 obeyed SIGTERM
    monkeypatch.setattr(main.os, "kill", fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert main._reap_orphaned_forkservers() == 2
    assert (10, signal.SIGTERM) in sent and (11, signal.SIGTERM) in sent
    assert (10, signal.SIGKILL) in sent and (11, signal.SIGKILL) not in sent


def test_the_reaper_runs_at_every_start_and_first_takes_leftovers_of_an_exec(monkeypatch):
    """Every start reaps (fork-free); the first start of a process image also
    takes its own forkserver children — left by the image before an
    ``os.execv`` restart — and later starts in the image leave its own
    (kept) forkserver alone."""
    import ast
    import inspect
    from soniqboom import main
    calls = []
    monkeypatch.setattr(main, "_reap_orphaned_forkservers", lambda own_leftovers=False: calls.append(own_leftovers) or 1)
    monkeypatch.setattr(main, "_started_in_this_image", False)
    assert main._reap_at_start() == 1 and main._reap_at_start() == 1
    assert calls == [True, False]
    src = inspect.getsource(main.startup)
    called = {n.func.id for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"_reap_at_start", "begin_run"} <= called


def test_the_reaper_lists_processes_without_forking(monkeypatch):
    from soniqboom import main
    from soniqboom.core import forksafe
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return type("R", (), {"stdout": ""})()
    monkeypatch.setattr(forksafe, "run", fake_run)
    assert main._reap_orphaned_forkservers() == 0
    assert seen and seen[0][0] == "ps"


def test_own_forkserver_children_are_taken_only_as_leftovers():
    from soniqboom import main
    procs = {10: (99, _FS), 11: (10, _FS), 20: (1, _FS)}
    assert sorted(main._orphan_tree_targets(procs, me=99)) == [20]
    assert sorted(main._orphan_tree_targets(procs, me=99, own_leftovers=True)) == [10, 11, 20]


def test_kill_pool_kills_the_worker_processes():
    killed = []

    class _Proc:
        def kill(self):
            killed.append(1)

    class _Pool:
        _processes = {1: _Proc(), 2: _Proc()}

        def shutdown(self, wait=True, cancel_futures=False):
            pass
    scanner._kill_pool(_Pool())
    assert killed == [1, 1]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_a_forkserver_worker_is_recognised_without_forking_and_shutdown_ends_it(monkeypatch):
    import os
    from soniqboom import main
    monkeypatch.setattr(scanner, "_pools_closed", False)    # restored after the test
    scanner.prepare_worker_forkserver()
    pool = scanner._process_pool(1)
    pid = pool.submit(os.getpid).result(timeout=60)
    assert main._is_our_forkserver_child(pid) is True
    assert main._is_our_forkserver_child(os.getpid()) is False
    scanner.shutdown_worker_pools()
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive


@pytest.mark.parametrize("exc", [RuntimeError, OSError, BrokenProcessPool])
async def test_a_pool_that_refuses_work_with_any_error_ends_the_root(library, monkeypatch, exc):
    root, store = library

    class _Refuse:
        def submit(self, fn, *args):
            raise exc("no")

        def shutdown(self, wait=True, cancel_futures=False):
            pass
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_Refuse()) or made[-1])
    await _scan(root, limit=20)
    assert scanner._progress.errors == 4 and len(made) == 3


def test_shutdown_kills_every_live_pool(monkeypatch):
    monkeypatch.setattr(scanner, "_pools_closed", False)    # restored after the test
    killed = []

    class _Proc:
        def kill(self):
            killed.append(1)

    class _Pool:
        def __init__(self):
            self._processes = {1: _Proc()}

        def shutdown(self, wait=True, cancel_futures=False):
            pass
    pools = [_Pool(), _Pool()]
    for pool in pools:
        scanner._live_pools[pool] = scanner._run_epoch
    scanner.shutdown_worker_pools()
    assert killed == [1, 1]


def test_kill_pool_kills_a_worker_a_racing_submit_started():
    """A submit that got in just before the shutdown started a worker: it is
    in the pool's process dict by the time the shutdown returns — killed."""
    killed = []

    class _Proc:
        def __init__(self, n):
            self.n = n

        def kill(self):
            killed.append(self.n)

    class _Pool:
        def __init__(self):
            self._processes = {1: _Proc(1)}

        def shutdown(self, wait=True, cancel_futures=False):
            self._processes[2] = _Proc(2)           # the racing submit's worker
            self._processes = None                  # as ProcessPoolExecutor.shutdown does
    scanner._kill_pool(_Pool())
    assert sorted(killed) == [1, 2]


def _sleep_long():
    time.sleep(120)


def _gone(pid: int, within: float = 5.0) -> bool:
    """``pid`` has exited (and been reaped) within ``within`` seconds."""
    import os
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_a_stop_keeps_the_forkserver_and_the_next_start_reuses_it(monkeypatch):
    """The bundled app stops and starts the server in one process: a stop
    kills the pools but keeps the forkserver — forking a new one from a
    server that used the network can crash every pool after it — so the
    next start forks none; nor does the stop wait for another forkserver
    child that lives on (the merger is one)."""
    import multiprocessing
    import os
    import signal
    from multiprocessing import forkserver
    monkeypatch.setattr(scanner, "_pools_closed", False)    # restored after the test
    scanner.prepare_worker_forkserver()
    fs_pid = forkserver._forkserver._forkserver_pid
    assert fs_pid
    other = multiprocessing.get_context("forkserver").Process(target=_sleep_long, daemon=True)
    other.start()
    try:
        pool = scanner._process_pool(1)
        worker = pool.submit(os.getpid).result(timeout=60)
        t0 = time.monotonic()
        scanner.shutdown_worker_pools()
        assert time.monotonic() - t0 < 2
        assert _gone(worker)                        # the pool's worker is killed
        os.kill(fs_pid, 0)                          # the forkserver is kept
        scanner.begin_run()                         # the next start in this process
        assert forkserver._forkserver._forkserver_pid == fs_pid
        pool = scanner._process_pool(1)
        try:
            assert pool.submit(sum, [2, 3]).result(timeout=60) == 5
        finally:
            scanner._kill_pool(pool)
        assert forkserver._forkserver._forkserver_pid == fs_pid    # nothing forked a new one
    finally:
        try:
            os.kill(other.pid, signal.SIGKILL)      # not Process.kill: it may think it's done
        except OSError:
            pass


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_the_kept_forkserver_ends_with_the_process(tmp_path):
    """Kept at a stop, the forkserver still leaves with the server: once the
    process has exited, it and every worker it forked are gone."""
    import os
    import subprocess
    import textwrap
    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent("""
        import os
        from multiprocessing import forkserver
        from soniqboom.core import scanner

        if __name__ == "__main__":
            scanner.prepare_worker_forkserver()
            pool = scanner._process_pool(2)
            workers = {pool.submit(os.getpid).result(timeout=60) for _ in range(4)}
            scanner.shutdown_worker_pools()
            print(forkserver._forkserver._forkserver_pid, *workers, flush=True)
    """))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         timeout=120, env=dict(os.environ))
    assert out.returncode == 0, out.stderr[-2000:]
    pids = [int(x) for x in out.stdout.split()]
    assert len(pids) >= 2
    assert all(_gone(p, within=10) for p in pids)


def test_after_a_stop_no_scan_pool_starts_until_the_next_start_up(monkeypatch):
    monkeypatch.setattr(scanner, "_pools_closed", False)    # restored after the test
    scanner.shutdown_worker_pools()
    with pytest.raises(RuntimeError):
        scanner._process_pool(1)
    scanner.begin_run()                             # the bundled app restarts in-process
    pool = scanner._process_pool(1)
    try:
        assert pool.submit(sum, [1, 1]).result(timeout=60) == 2
    finally:
        scanner._kill_pool(pool)


async def test_a_scan_that_can_get_no_new_pool_ends_the_root_without_raising(library, monkeypatch):
    """A stop began while the root scanned: its pool was killed and no new
    one may start — the root ends at once, the files left not indexed."""
    root, store = library
    made = []

    def factory(n):
        if made:
            raise RuntimeError("the server is stopping — no new scan workers")
        made.append(_DyingPool())
        return made[-1]
    monkeypatch.setattr(scanner, "_process_pool", factory)
    assert await _scan(root, limit=20) is False
    assert scanner._progress.errors == 4 and scanner._progress.processed == 4
    assert len(made) == 1 and made[0].shut


class _DeadAtStart:
    """Workers that crash at start: the canary fails at once, the files'
    failures arrive ``late`` seconds later."""
    def __init__(self, late: float = 0.3) -> None:
        self.late = late

    def submit(self, fn, *args):
        f: concurrent.futures.Future = concurrent.futures.Future()
        if not args:
            f.set_exception(BrokenProcessPool("a worker crashed at start"))
        else:
            threading.Timer(self.late, lambda: f.done() or f.set_exception(
                BrokenProcessPool("a worker crashed at start"))).start()
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        pass


async def test_failures_arriving_after_the_give_up_are_counted(library, monkeypatch):
    root, store = library
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_DeadAtStart()) or made[-1])
    await _scan(root, limit=20)
    assert not store._tracks and len(made) == 3
    assert scanner._progress.errors == 4 and scanner._progress.processed == 4


# ── a stalled source ──────────────────────────────────────────────────────────

def test_a_readable_or_vanished_file_answers(tmp_path):
    f = tmp_path / "a.zip"
    f.write_bytes(b"x" * 100_000)
    assert scanner._source_answers(f, timeout=5) is True
    assert scanner._source_answers(f"{f}::inner/tune.mod", timeout=5) is True   # its archive is read
    assert scanner._source_answers(tmp_path / "gone.mod", timeout=5) is True


def test_a_read_that_blocks_does_not_answer(tmp_path):
    """A FIFO nobody writes blocks its reader as a hung share does."""
    import os
    fifo = tmp_path / "hung.mod"
    os.mkfifo(fifo)
    t0 = time.monotonic()
    assert scanner._source_answers(fifo, timeout=0.5) is False
    assert scanner._source_answers(f"{fifo}::inner/tune.mod", timeout=0.5) is False   # its archive is read
    assert time.monotonic() - t0 < 3
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)         # let the probe threads end
    os.close(fd)


def test_a_read_error_does_not_answer(tmp_path):
    d = tmp_path / "dir.mod"
    d.mkdir()                                           # reading a folder: IsADirectoryError
    assert scanner._source_answers(d, timeout=5) is False


async def test_a_stalled_source_gives_the_root_up_after_one_file_hangs_alone(library, monkeypatch, caplog):
    """Every file hangs because the share stopped answering: the first file
    that hangs alone can't be read either — the rest of the root is given up
    at once, not tried file by file."""
    import logging
    root, store = library
    _six(root)
    made = []
    everything = " ".join(f"t{i}.wav" for i in range(6))
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool(everything, hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    probed = []
    monkeypatch.setattr(scanner, "_STALL_GRACE_S", 0)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: probed.append(a) or False)
    with caplog.at_level(logging.ERROR, logger=scanner.log.name):
        await _scan(root, limit=20)
    assert not store._tracks
    assert scanner._progress.errors == 6 and scanner._progress.processed == 6
    assert len(probed) == 1 and len(made) == 2
    assert any("stalled" in r.getMessage() for r in caplog.records)


async def test_files_that_hang_while_the_source_answers_are_each_skipped(library, monkeypatch, caplog):
    """Nine files in a row hang their worker while the source reads fine
    (files each too slow, or an extractor that hangs on them): each is tried
    alone and skipped — never the rest of the root; the files after them
    are indexed."""
    import logging
    root, store = library
    for i in range(4, 12):
        _wav(root / f"t{i}.wav", 300 + 40 * i)
    made = []
    hang = " ".join(f"t{i}.wav" for i in range(9))
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool(hang, hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "INFLIGHT", 2)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)
    with caplog.at_level(logging.ERROR, logger=scanner.log.name):
        await _scan(root, limit=60)
    assert sorted(Path(t["path"]).name for t in store._tracks.values()) == ["t10.wav", "t11.wav", "t9.wav"]
    assert scanner._progress.errors == 9 and scanner._progress.processed == 12
    assert sum("hung" in r.getMessage() and "skipped" in r.getMessage() for r in caplog.records) == 9
    assert not any("stalled" in r.getMessage() for r in caplog.records)


async def test_a_file_that_settles_between_hangs_resets_the_count(library, monkeypatch):
    """Hangs separated by files that index are the files' fault: no give-up."""
    root, store = library
    for i in range(4, 12):
        _wav(root / f"t{i}.wav", 300 + 40 * i)
    made = []
    hangs = " ".join(f"t{i}.wav" for i in (1, 3, 5, 7, 9, 11))      # six hangs, never two adjacent
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool(hangs, hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "INFLIGHT", 1)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)
    await _scan(root, limit=60)
    assert len(store._tracks) == 6
    assert scanner._progress.errors == 6 and scanner._progress.processed == 12



class _BusyPool:
    """Like a real pool: ``workers`` threads take the jobs in order (a
    job's future is ``running()`` once taken); a job for a ``hang`` file
    never ends — the thread stays stuck, as a worker would.  Records the
    most file jobs it had unfinished at once (``max_live``)."""

    def __init__(self, hang: str = "", workers: int = 2) -> None:
        import queue
        self.hang = set(hang.split())
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.live = self.max_live = 0
        self.shut = False
        self.workers = workers
        for _ in range(workers):
            threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            f, fn, args = item
            if not f.set_running_or_notify_cancel():
                continue
            if args and args[0].name in self.hang:
                threading.Event().wait()            # stuck for good
            time.sleep(0.02)
            try:
                f.set_result(fn(*args))
            except BaseException as exc:            # noqa: BLE001
                f.set_exception(exc)
            if args:
                with self.lock:
                    self.live -= 1

    def submit(self, fn, *args):
        if self.shut:
            raise RuntimeError("cannot schedule new futures after shutdown")
        f: concurrent.futures.Future = concurrent.futures.Future()
        if args:
            with self.lock:
                self.live += 1
                self.max_live = max(self.max_live, self.live)
        self.q.put((f, fn, args))
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        import queue
        self.shut = True
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()
        for _ in range(self.workers):
            self.q.put(None)


async def test_files_only_queued_behind_a_hang_run_again_together(library, monkeypatch):
    """Two workers hang; the files queued behind them never ran — they go
    back to the normal stream (together), only the two that ran are tried
    alone."""
    root, store = library
    _six(root)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_BusyPool("t0.wav t1.wav")) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.4)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)
    await _scan(root)
    assert sorted(Path(t["path"]).name for t in store._tracks.values()) == [
        "t2.wav", "t3.wav", "t4.wav", "t5.wav"]
    assert scanner._progress.errors == 2 and scanner._progress.processed == 6
    assert made[-1].max_live == 4                   # the four ran together, not one by one


async def test_a_share_that_answers_again_within_the_grace_is_not_given_up(library, monkeypatch):
    root, store = library
    _six(root)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t2.wav", hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "_STALL_PAUSE_S", 0.01)
    answers = iter([False, False, True])            # waking up
    probes = []
    killed_first = []
    monkeypatch.setattr(scanner, "_source_answers",
                        lambda *a, **k: probes.append(a) or killed_first.append(made[-1].shut)
                        or next(answers, True))
    await _scan(root)
    assert len(store._tracks) == 5 and scanner._progress.errors == 1
    assert len(probes) == 3 and killed_first == [True, True, True]   # the hung pool went first
    last_ok, _timeout, listing = probes[0]
    assert listing == str(root) and last_ok is not None and last_ok.name != "t2.wav"   # never the hung file


async def test_files_that_fail_with_source_errors_stop_the_root_when_it_does_not_answer(
        library, monkeypatch, caplog):
    import logging
    root, store = library
    for i in range(4, 30):
        _wav(root / f"t{i}.wav", 200 + 10 * i)
    calls = []

    def io_error(path):
        calls.append(path)
        return path, "OSError: [Errno 5] Input/output error", None, None
    monkeypatch.setattr(scanner, "_extract_one", io_error)
    monkeypatch.setattr(scanner, "_process_pool", lambda n: concurrent.futures.ThreadPoolExecutor(2))
    monkeypatch.setattr(scanner, "INFLIGHT", 2)
    monkeypatch.setattr(scanner, "_STALL_GRACE_S", 0)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: False)
    with caplog.at_level(logging.ERROR, logger=scanner.log.name):
        await _scan(root)
    assert scanner._progress.errors == scanner._progress.processed == 30
    assert len(calls) < 30 and any("stalled" in r.getMessage() for r in caplog.records)
    calls.clear()
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)       # the files fail, not the share
    await _scan(root)
    assert len(calls) == 30


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_a_worker_leads_its_group_and_passes_no_descriptor_on(monkeypatch):
    import _scan_crash
    monkeypatch.setattr(scanner, "_pools_closed", False)
    scanner.prepare_worker_forkserver()
    pool = scanner._process_pool(1)
    try:
        leader, inheritable = pool.submit(_scan_crash.worker_facts).result(timeout=60)
        assert leader is True and inheritable == []
    finally:
        scanner._kill_pool(pool)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_killing_a_pool_kills_what_its_workers_started(monkeypatch):
    import _scan_crash
    monkeypatch.setattr(scanner, "_pools_closed", False)
    scanner.prepare_worker_forkserver()
    pool = scanner._process_pool(1)
    child = pool.submit(_scan_crash.start_child).result(timeout=60)
    import os
    os.kill(child, 0)
    scanner._kill_pool(pool)
    assert _gone(child)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_a_client_that_sends_nothing_does_not_kill_the_forkserver(monkeypatch):
    import os
    import socket
    from multiprocessing import forkserver
    from soniqboom.core import procinfo
    monkeypatch.setattr(scanner, "_pools_closed", False)
    fs = forkserver._forkserver
    if fs._forkserver_pid:                          # one another test started with another preload
        os.kill(fs._forkserver_pid, 9)
        time.sleep(0.3)
    scanner.prepare_worker_forkserver()
    pid, address = fs._forkserver_pid, fs._forkserver_address
    assert "soniqboom._forkserver_guard" in procinfo.cmdline(pid)
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(address)                          # and closed at once, sending nothing
    time.sleep(0.5)
    os.kill(pid, 0)
    pool = scanner._process_pool(1)
    try:
        assert pool.submit(sum, [3, 4]).result(timeout=60) == 7
    finally:
        scanner._kill_pool(pool)
    assert fs._forkserver_pid == pid


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_a_dead_forkserver_is_started_again_without_a_fork(monkeypatch):
    import _posixsubprocess
    import os
    import signal
    from multiprocessing import forkserver
    monkeypatch.setattr(scanner, "_pools_closed", False)
    scanner.prepare_worker_forkserver()
    fs = forkserver._forkserver
    old = fs._forkserver_pid
    os.kill(old, signal.SIGKILL)
    time.sleep(0.3)

    def no_fork(*a, **k):
        raise AssertionError("forked")
    monkeypatch.setattr(_posixsubprocess, "fork_exec", no_fork)
    pool = scanner._process_pool(1)
    try:
        assert pool.submit(sum, [1, 2]).result(timeout=60) == 3
    finally:
        scanner._kill_pool(pool)
    assert fs._forkserver_pid not in (None, old)


def test_a_new_run_resets_what_the_previous_run_left(monkeypatch):
    """The bundled app restarts the server in one process: the previous
    run's event loop closed under a running, paused scan."""
    killed = []

    class _Old:
        _processes = {}

        def shutdown(self, wait=True, cancel_futures=False):
            killed.append(1)
    old = _Old()
    for name in ("_run_epoch", "_pools_closed", "_scan_count", "_progress", "_pause_event",
                 "_scan_task", "_current_scan_dirs"):
        monkeypatch.setattr(scanner, name, getattr(scanner, name))
    monkeypatch.setattr(scanner, "prepare_worker_forkserver", lambda: None)
    scanner._live_pools[old] = scanner._run_epoch
    scanner._scan_count = 1
    scanner._progress.running = True
    scanner._current_scan_dirs = frozenset({"/music"})
    scanner._pause_event = asyncio.Event()
    scanner._pools_closed = True
    try:
        scanner.begin_run()
        scanner._pool_killers[old].join(5)          # killed in the background
        assert killed == [1]
        assert scanner._scan_count == 0 and scanner._progress.running is False
        assert not scanner.is_scanning() and scanner._pause_event is None
        assert scanner._pools_closed is False
    finally:
        scanner._progress = scanner.ScanProgress()


def test_a_late_stop_of_the_previous_run_leaves_the_new_run_alone(monkeypatch):
    killed = []

    class _Pool:
        def __init__(self, name):
            self.name = name
            self._processes = {}

        def shutdown(self, wait=True, cancel_futures=False):
            killed.append(self.name)
    monkeypatch.setattr(scanner, "_run_epoch", scanner._run_epoch)
    monkeypatch.setattr(scanner, "_pools_closed", False)
    monkeypatch.setattr(scanner, "prepare_worker_forkserver", lambda: None)
    first = scanner.begin_run()
    old = _Pool("old")
    scanner._live_pools[old] = first
    second = scanner.begin_run()                    # the next start, before the stop ended
    scanner._pool_killers[old].join(5)              # killed in the background
    assert killed == ["old"]
    new = _Pool("new")
    scanner._live_pools[new] = second
    assert scanner.shutdown_worker_pools(epoch=first) is True     # the previous run's stop, late
    assert scanner._pools_closed is False and killed == ["old"]   # "old" is killed once, "new" not
    assert scanner.shutdown_worker_pools(epoch=second) is True
    assert scanner._pools_closed is True and killed == ["old", "new"]


async def test_a_stalled_root_is_not_touched_again_after_it_is_given_up(library, monkeypatch):
    """A folder-watcher scan: a file was deleted, a new one hangs and the
    share doesn't answer — the root is given up and not listed again (its
    liveness check would block on the hung share), so nothing is pruned."""
    root, store = library
    monkeypatch.setattr(scanner, "_process_pool", lambda n: concurrent.futures.ThreadPoolExecutor(2))
    await _scan(root)
    assert len(store._tracks) == 4
    (root / "t3.wav").unlink()
    _wav(root / "t4.wav", 880)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t4.wav", hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "_STALL_GRACE_S", 0)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: False)
    touched = []
    monkeypatch.setattr(scanner, "_root_is_live", lambda r: touched.append(r) or True)
    scope = {str(root): frozenset({str(root / "t3.wav"), str(root / "t4.wav")})}
    await asyncio.wait_for(scanner._run_scan([str(root)], scope=scope), 30)
    assert touched == []
    assert any(t["path"].endswith("t3.wav") for t in store._tracks.values())    # not pruned


async def test_the_probe_reads_a_file_the_scan_has_not_read(library, monkeypatch):
    """A file hangs alone: the probe reads the next file in line (never the
    hung one, never the one read last — a cache could answer for those),
    which is still indexed afterwards."""
    root, store = library
    _six(root)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t2.wav", hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "INFLIGHT", 1)
    probes = []
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: probes.append((a, k)) or True)
    avoided = []
    real_unread = scanner._unread_file
    monkeypatch.setattr(scanner, "_unread_file",
                        lambda rq, it, avoid=(): avoided.append(avoid) or real_unread(rq, it, avoid))
    await _scan(root)
    assert [tuple(p.name for p in a) for a in avoided] == [("t2.wav", "t1.wav")]    # the hung one, the last good
    assert sorted(Path(t["path"]).name for t in store._tracks.values()) == [
        "t0.wav", "t1.wav", "t3.wav", "t4.wav", "t5.wav"]
    assert scanner._progress.errors == 1 and scanner._progress.processed == 6
    (last_ok, _timeout, listing), kw = probes[0]
    assert last_ok.name == "t1.wav" and listing == str(root)
    assert kw["fresh"].name == "t3.wav"


def test_the_unread_file_is_not_of_an_archive_already_read():
    from collections import deque
    requeue = deque([Path("/m/a.zip::x.mod")])
    rest = iter([Path("/m/a.zip::y.mod"), Path("/m/last.mod"), Path("/m/b.zip::z.mod"), Path("/m/c.mod")])
    got = scanner._unread_file(requeue, rest, (Path("/m/a.zip::w.mod"), Path("/m/last.mod")))
    assert got == Path("/m/b.zip::z.mod")
    # what was looked at goes back in order; nothing else is taken
    assert list(requeue) == [Path("/m/a.zip::x.mod"), Path("/m/a.zip::y.mod"), Path("/m/last.mod"),
                             Path("/m/b.zip::z.mod")]
    assert list(rest) == [Path("/m/c.mod")]
    assert scanner._unread_file(deque([Path("/m/a.zip::q")]), iter([]), (Path("/m/a.zip::w"),)) is None
    assert scanner._unread_file(deque(), iter([Path("/m/d.mod")]), (None,)) == Path("/m/d.mod")


def test_the_unread_file_is_read_past_the_cache(tmp_path, monkeypatch):
    f = tmp_path / "unread.mod"
    f.write_bytes(bytes(range(256)) * 1024)                 # 256 KB
    assert scanner._source_answers(None, timeout=5, fresh=f) is True
    assert scanner._source_answers(None, timeout=5, fresh=f"{f}::inner/tune.mod") is True
    assert scanner._source_answers(None, timeout=5, fresh=tmp_path / "gone.mod") is True
    d = tmp_path / "adir"
    d.mkdir()
    assert scanner._source_answers(None, timeout=5, fresh=d) is True     # not a regular file: an answer
    locked = tmp_path / "locked.mod"
    locked.write_bytes(b"x" * 4096)
    locked.chmod(0)
    try:
        assert scanner._source_answers(None, timeout=5, fresh=locked) is True   # "permission denied": an answer
    finally:
        locked.chmod(0o644)
    reads = []
    real_pread = os.pread
    monkeypatch.setattr(scanner.os, "pread", lambda fd, n, off: reads.append((n, off)) or real_pread(fd, n, off))
    if sys.platform == "darwin":
        import fcntl
        flags = []
        real_fcntl = fcntl.fcntl
        monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd, arg=0: flags.append((cmd, arg)) or real_fcntl(fd, cmd, arg))
        scanner._read_uncached(str(f))
        assert (fcntl.F_NOCACHE, 1) in flags
    else:
        scanner._read_uncached(str(f))
    assert reads == [(65536, 131072 - 32768)]              # 64 KB from the middle, page-aligned


@pytest.mark.skipif(sys.platform == "win32", reason="FIFO")
def test_an_unread_file_whose_read_blocks_or_fails_does_not_answer(tmp_path, monkeypatch):
    import errno as _errno
    f = tmp_path / "hung.mod"
    f.write_bytes(b"x" * 200000)
    fifo = tmp_path / "pipe.mod"
    os.mkfifo(fifo)
    assert scanner._source_answers(None, timeout=2, fresh=fifo) is True      # never waits for a writer
    release = threading.Event()
    real = os.pread

    def pread(fd, n, off):
        if release.is_set():
            return real(fd, n, off)
        release.wait(10)                                # a share that stopped answering
        return b""
    monkeypatch.setattr(scanner.os, "pread", pread)
    try:
        assert scanner._source_answers(None, timeout=0.5, fresh=f) is False
    finally:
        release.set()

    def eio(fd, n, off):
        raise OSError(_errno.EIO, "Input/output error")
    monkeypatch.setattr(scanner.os, "pread", eio)
    assert scanner._source_answers(None, timeout=2, fresh=f) is False       # a source error


async def test_an_unreadable_file_after_a_hang_does_not_give_the_root_up(library, monkeypatch, caplog):
    """QA8 MJ-1: the file the probe reads next can't be opened (permission
    denied) — the file system answered, so only the hung file is skipped;
    the real probe, no grace needed."""
    import logging
    root, store = library
    _six(root)
    for i in range(6, 8):
        _wav(root / f"t{i}.wav", 300 + 40 * i)
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_PoisonPool("t2.wav", hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "_STALL_GRACE_S", 0.5)
    monkeypatch.setattr(scanner, "_STALL_PAUSE_S", 0.05)
    monkeypatch.setattr(scanner, "INFLIGHT", 1)
    (root / "t3.wav").chmod(0)
    try:
        with caplog.at_level(logging.WARNING, logger=scanner.log.name):
            await _scan(root)
    finally:
        (root / "t3.wav").chmod(0o644)
    names = {Path(t["path"]).name for t in store._tracks.values()}
    assert {"t0.wav", "t1.wav", "t4.wav", "t5.wav", "t6.wav", "t7.wav"} <= names and "t2.wav" not in names
    assert scanner._progress.processed == 8
    assert not any("stalled" in r.getMessage() or "doesn't answer" in r.getMessage() for r in caplog.records)


class _LockHeldPool:
    """Like a real pool killed while one worker can't exit (stuck in an
    uninterruptible read on a hung share): once a worker is killed, its
    manager thread holds the pool's lock for good (``release`` ends it) —
    ``submit`` and ``shutdown`` wait on it.  Files under ``hang_root`` never
    finish; others are extracted in a thread."""

    class _Proc:
        pid = None

        def __init__(self, pool):
            self.pool = pool

        def is_alive(self):
            return True

        def kill(self):
            pool = self.pool
            if pool.killed:
                return
            pool.killed = True
            held = threading.Event()

            def manager():
                with pool._shutdown_lock:
                    held.set()
                    pool.release.wait(pool.hold)
            threading.Thread(target=manager, daemon=True).start()
            held.wait(5)

    def __init__(self, hang_root, release, hold=60.0, delay=0.0):
        self.hang_root, self.release, self.hold, self.delay = str(hang_root), release, hold, delay
        self._shutdown_lock = threading.Lock()
        self._processes = {1: self._Proc(self)}
        self.killed = False
        self.submits_after_kill = 0

    def submit(self, fn, *args):
        if self.killed:
            self.submits_after_kill += 1
        with self._shutdown_lock:
            f: concurrent.futures.Future = concurrent.futures.Future()
            if not args:
                f.set_result(fn())                          # the canary
            elif not str(args[0]).startswith(self.hang_root):
                def run():
                    time.sleep(self.delay)
                    f.set_result(fn(*args))
                threading.Thread(target=run, daemon=True).start()
            return f

    def shutdown(self, wait=True, cancel_futures=False):
        with self._shutdown_lock:
            pass


async def test_a_pool_whose_lock_is_held_for_good_is_never_touched_again(library, tmp_path, monkeypatch, caplog):
    """The first root's share hangs: its pools are killed with a worker that
    can't exit, which holds their lock for good.  The scan never waits on
    that lock again — the next root gets a new pool and is indexed, and the
    scan ends."""
    root, store = library
    other = tmp_path / "other"
    other.mkdir()
    for i, f in enumerate((300, 350, 400)):
        _wav(other / f"o{i}.wav", f)
    release = threading.Event()
    made = []
    monkeypatch.setattr(scanner, "_process_pool",
                        lambda n: made.append(_LockHeldPool(root, release)) or made[-1])
    monkeypatch.setattr(scanner._kill_pool_async, "__defaults__", (0.3,))
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "_STALL_GRACE_S", 0)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: False)
    import logging
    try:
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=scanner.log.name):
            await asyncio.wait_for(scanner._run_scan([str(root), str(other.resolve())]), 30)
        took = time.monotonic() - t0
        assert not [r for r in caplog.records if "died" in r.getMessage() and "other" in r.getMessage()]
        assert sorted(Path(t["path"]).name for t in store._tracks.values()) == ["o0.wav", "o1.wav", "o2.wav"]
        assert scanner._progress.errors == 4 and scanner._progress.processed == 7
        killed = [p for p in made if p.killed]
        assert len(killed) == 2 and all(p.submits_after_kill == 0 for p in killed)
        assert not made[-1].killed and took < 10
    finally:
        release.set()


def test_a_start_and_a_stop_do_not_wait_on_a_pool_whose_lock_is_held(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(scanner, "_run_epoch", scanner._run_epoch)
    monkeypatch.setattr(scanner, "_pools_closed", False)
    monkeypatch.setattr(scanner, "prepare_worker_forkserver", lambda: None)
    stale = _LockHeldPool("/", release)
    scanner._live_pools[stale] = scanner._run_epoch
    try:
        t0 = time.monotonic()
        epoch = scanner.begin_run()                 # the old run's pool: killed in the background
        assert time.monotonic() - t0 < 0.5
        assert stale.killed or scanner._pool_killers[stale].is_alive()
        stuck = _LockHeldPool("/", release)
        scanner._live_pools[stuck] = epoch
        stuck._processes[1].kill()                  # its manager holds the lock now
        t0 = time.monotonic()
        assert scanner.shutdown_worker_pools(epoch, timeout=0.3) is False
        assert time.monotonic() - t0 < 1.0
        assert scanner._pool_stopped(stuck) and scanner._pools_closed is True
    finally:
        release.set()


def test_the_restart_stops_pools_and_downloads_before_the_journal_flush(monkeypatch):
    from soniqboom import main
    from soniqboom.api import admin
    from soniqboom.core import game_titles
    order = []

    class _Writer:
        def stop(self):
            order.append("aof")
    monkeypatch.setattr(main, "_aof_writer", _Writer())
    monkeypatch.setattr(main, "_merger_proc", None)
    monkeypatch.setattr(game_titles, "cancel_downloads", lambda *a, **k: order.append("downloads") or [])
    monkeypatch.setattr(scanner, "shutdown_worker_pools", lambda *a, **k: order.append("pools") or True)
    admin._graceful_pre_exec_flush()
    assert order == ["downloads", "pools", "aof"]


async def test_a_stop_during_a_scan_never_waits_on_the_pool_it_killed(library, tmp_path, monkeypatch):
    """A stop kills the scan's pool while files are still coming back (one
    worker can't exit, so the pool's lock stays held): the scan never submits
    to that pool again — it ends the root ("server stopping") at once instead
    of blocking the event loop on the lock."""
    root, store = library
    for i in range(4, 40):
        _wav(root / f"t{i}.wav", 200 + 10 * i)
    release = threading.Event()
    made = []
    monkeypatch.setattr(scanner, "_run_epoch", scanner._run_epoch)
    monkeypatch.setattr(scanner, "_pools_closed", False)

    def factory(n):
        if scanner._pools_closed:
            raise RuntimeError("the server is stopping — no new scan workers")
        made.append(_LockHeldPool("/nothing-hangs", release, hold=5.0, delay=0.1))
        scanner._live_pools[made[-1]] = scanner._run_epoch
        return made[-1]
    monkeypatch.setattr(scanner, "_process_pool", factory)
    monkeypatch.setattr(scanner, "INFLIGHT", 2)

    async def stop_soon():
        await asyncio.sleep(0.5)
        return await asyncio.to_thread(scanner.shutdown_worker_pools, scanner._run_epoch, 0.2)
    try:
        t0 = time.monotonic()
        stopped, _ = await asyncio.wait_for(
            asyncio.gather(stop_soon(), scanner._run_scan([str(root)])), 30)
        took = time.monotonic() - t0
        assert stopped is False                     # the pool's worker can't exit
        assert len(made) == 1 and made[0].killed and made[0].submits_after_kill == 0
        assert took < 3.0
        assert scanner._progress.processed == 40 and 0 < len(store._tracks) < 40
        assert scanner._progress.errors == 40 - len(store._tracks)
    finally:
        release.set()


async def test_ending_or_killing_a_pool_never_waits_on_its_lock():
    release = threading.Event()
    try:
        held = _LockHeldPool("/", release, hold=3.0)
        held._processes[1].kill()                   # its manager holds the lock now
        before = {t.ident for t in threading.enumerate()}
        t0 = time.monotonic()
        scanner._release_pool(held)                 # not known as stopped: shut down off the caller
        assert time.monotonic() - t0 < 0.2
        killed = _LockHeldPool("/", release)
        await scanner._kill_pool_async(killed, timeout=0.3)     # its kill can't finish
        started = {t.ident for t in threading.enumerate()} - before
        t0 = time.monotonic()
        await scanner._kill_pool_async(killed, timeout=5)       # killed already: not waited for again
        assert time.monotonic() - t0 < 0.1
        n = len(threading.enumerate())
        scanner._release_pool(killed)               # a stopped pool is left alone
        assert len(threading.enumerate()) == n and scanner._pool_stopped(killed)
        assert started                              # the kill ran in its own thread
    finally:
        release.set()


def _zip_of_wavs(zip_path: Path, names: list[str], tmp: Path) -> None:
    import zipfile
    with zipfile.ZipFile(zip_path, "w") as z:
        for i, n in enumerate(names):
            w = tmp / f"_member_{i}.wav"
            _wav(w, 300 + 20 * i)
            z.write(w, n)
            w.unlink()


async def _scan_members(root, monkeypatch, caplog, hang: str):
    import logging
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_PoisonPool(hang, hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "INFLIGHT", 2)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)
    with caplog.at_level(logging.WARNING, logger=scanner.log.name):
        await _scan(root, limit=90)
    return {Path(t["path"]).name for t in scanner.get_store()._tracks.values()}


async def test_members_of_one_archive_that_hang_in_a_row_skip_the_rest_of_it(library, tmp_path, monkeypatch, caplog):
    """Five members of one archive hang one after another (the share
    answers): the rest of that archive's members are skipped — not tried at
    90 s each — even ones that would extract (the trade-off); files outside
    it are indexed."""
    root, store = library
    for f in root.iterdir():
        f.unlink()
    _zip_of_wavs(root / "pack.zip", [f"m{i}.wav" for i in range(10)], tmp_path)
    _wav(root / "z.wav", 500)
    names = await _scan_members(root, monkeypatch, caplog,
                                " ".join(f"pack.zip::m{i}.wav" for i in range(7)))
    assert names == {"z.wav"}
    assert scanner._progress.errors == 10 and scanner._progress.processed == 11
    hung = [r for r in caplog.records if "extraction hung" in r.getMessage()]
    assert len(hung) == 5
    assert any("its other 5 member(s) were skipped" in r.getMessage() for r in caplog.records)


async def test_hangs_split_over_two_archives_skip_nothing(library, tmp_path, monkeypatch, caplog):
    root, store = library
    for f in root.iterdir():
        f.unlink()
    _zip_of_wavs(root / "a.zip", ["a0.wav", "a1.wav", "a2.wav"], tmp_path)
    _zip_of_wavs(root / "b.zip", ["b0.wav", "b1.wav", "b2.wav", "b3.wav"], tmp_path)
    hang = "a.zip::a0.wav a.zip::a1.wav a.zip::a2.wav b.zip::b0.wav b.zip::b1.wav b.zip::b2.wav"
    names = await _scan_members(root, monkeypatch, caplog, hang)    # six in a row, nothing extracted between
    assert names == {"b.zip::b3.wav"}
    assert scanner._progress.errors == 6 and scanner._progress.processed == 7
    assert not any("skipped, not indexed" in r.getMessage() for r in caplog.records)


async def test_a_member_extracted_between_hangs_restarts_the_count(library, tmp_path, monkeypatch, caplog):
    root, store = library
    for f in root.iterdir():
        f.unlink()
    members = [f"m{i}.wav" for i in range(10)]
    _zip_of_wavs(root / "pack.zip", members, tmp_path)
    hang = " ".join(f"pack.zip::m{i}.wav" for i in (0, 1, 2, 3, 5, 6, 7, 8))   # m4 extracts in between
    names = await _scan_members(root, monkeypatch, caplog, hang)
    assert names == {"pack.zip::m4.wav", "pack.zip::m9.wav"}
    assert scanner._progress.errors == 8 and scanner._progress.processed == 10
    assert not any("skipped, not indexed" in r.getMessage() for r in caplog.records)


def _nested_zip(zip_path: Path, inner: dict, tmp: Path) -> None:
    """``zip_path`` holding inner zips {name: [member, …]} of WAVs."""
    import io
    import zipfile
    with zipfile.ZipFile(zip_path, "w") as outer:
        for name, members in inner.items():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for i, m in enumerate(members):
                    w = tmp / f"_n_{name}_{i}.wav"
                    _wav(w, 300 + 15 * i)
                    z.write(w, m)
                    w.unlink()
            outer.writestr(name, buf.getvalue())


async def test_hangs_across_small_inner_archives_skip_the_rest_of_their_outer_file(
        library, tmp_path, monkeypatch, caplog):
    """A hung outer archive made of small inner archives (fewer than five
    members each): the run of hangs is counted per file on disk, and the
    rest of the deepest archive they all lie in — here the outer file — is
    skipped."""
    root, store = library
    for f in root.iterdir():
        f.unlink()
    _nested_zip(root / "outer.zip", {"a.zip": ["a0.wav", "a1.wav", "a2.wav"],
                                     "b.zip": ["b0.wav", "b1.wav", "b2.wav", "b3.wav"]}, tmp_path)
    _wav(root / "z.wav", 500)
    monkeypatch.setattr(scanner, "INFLIGHT", 1)
    hang = " ".join(["outer.zip::a.zip::a0.wav", "outer.zip::a.zip::a1.wav", "outer.zip::a.zip::a2.wav",
                     "outer.zip::b.zip::b0.wav", "outer.zip::b.zip::b1.wav"])
    import logging
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_PoisonPool(hang, hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)
    with caplog.at_level(logging.WARNING, logger=scanner.log.name):
        await _scan(root, limit=90)
    names = {Path(t["path"]).name for t in store._tracks.values()}
    assert names == {"z.wav"}
    assert scanner._progress.errors == 7 and scanner._progress.processed == 8
    msg = [r.getMessage() for r in caplog.records if "were skipped" in r.getMessage()]
    assert len(msg) == 1 and msg[0].rstrip().split(" in a row")[0].endswith("outer.zip")


async def test_hangs_inside_one_inner_archive_skip_only_that_archive(library, tmp_path, monkeypatch, caplog):
    root, store = library
    for f in root.iterdir():
        f.unlink()
    _nested_zip(root / "outer.zip", {"a.zip": [f"a{i}.wav" for i in range(7)], "b.zip": ["b0.wav"]}, tmp_path)
    monkeypatch.setattr(scanner, "INFLIGHT", 1)
    hang = " ".join(f"outer.zip::a.zip::a{i}.wav" for i in range(5))
    made = []
    monkeypatch.setattr(scanner, "_process_pool", lambda n: made.append(_PoisonPool(hang, hang=True)) or made[-1])
    monkeypatch.setattr(scanner, "_EXTRACT_STUCK_S", 0.3)
    monkeypatch.setattr(scanner, "_source_answers", lambda *a, **k: True)
    await _scan(root, limit=90)
    names = {Path(t["path"]).name for t in store._tracks.values()}
    assert names == {"outer.zip::b.zip::b0.wav"}                     # the sibling archive is indexed
    assert scanner._progress.errors == 7 and scanner._progress.processed == 8     # 5 hung + a5, a6 skipped


def test_the_common_archive_of_hung_members():
    assert scanner._common_archive(["o.zip::a.zip::x", "o.zip::a.zip::y"]) == "o.zip::a.zip"
    assert scanner._common_archive(["o.zip::a.zip::x", "o.zip::b.zip::y"]) == "o.zip"
    assert scanner._common_archive(["o.zip::x", "o.zip::a.zip::y"]) == "o.zip"


def test_a_skipped_archive_covers_its_own_members_only():
    assert scanner._in_archives("/m/a.zip::x.mod", ["/m/a.zip"]) == "/m/a.zip"
    assert scanner._in_archives("/m/a.zip::b.zip::x.mod", ["/m/a.zip"]) == "/m/a.zip"
    assert scanner._in_archives("/m/a.zip2::x.mod", ["/m/a.zip"]) is None
    assert scanner._in_archives("/m/a.zip", ["/m/a.zip"]) is None
