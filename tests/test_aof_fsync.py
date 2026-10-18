# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The AOF's "everysec" durability: written records are fsync'd about once a
second by a background thread — never on the event loop — and before the file
is closed (snapshot, shutdown, restart)."""
import asyncio
import os
import threading
import time

import pytest

from soniqboom.core import aof as aof_mod
from soniqboom.core.aof import AOFWriter


@pytest.fixture
def fsyncs(monkeypatch):
    """Every fsync of the AOF: (thread name, inode)."""
    calls = []
    real = os.fsync

    def spy(fd):
        st = os.fstat(fd)
        calls.append((threading.current_thread().name, st.st_ino))
        return real(fd)
    monkeypatch.setattr(aof_mod.os, "fsync", spy)
    monkeypatch.setattr(AOFWriter, "_FSYNC_INTERVAL", 0.1)
    return calls


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_written_records_are_fsynced_off_the_event_loop(tmp_path, fsyncs):
    path = tmp_path / "library.aof"
    w = AOFWriter(path, flush_interval=0.01)

    async def run():
        loop_thread = threading.current_thread().name
        await w.start_auto_flush()
        for i in range(20):
            w.append("record_play", track_id=f"t{i}")
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        w.cancel_flush_task()
        return loop_thread
    loop_thread = asyncio.run(run())
    ino = path.stat().st_ino
    assert _wait(lambda: any(i == ino for _, i in fsyncs))
    assert all(t == "aof-fsync" for t, i in fsyncs if i == ino)
    assert loop_thread not in {t for t, _ in fsyncs}
    assert w.fsync_count >= 1
    w.stop()


def test_nothing_written_means_no_fsync_and_the_thread_ends(tmp_path, fsyncs, monkeypatch):
    monkeypatch.setattr(AOFWriter, "_FSYNC_IDLE_EXIT", 3)
    w = AOFWriter(tmp_path / "library.aof")
    time.sleep(0.3)
    assert fsyncs == [] and w._fsync_thread is None      # never started
    w.append("x")
    w.flush_sync()
    assert _wait(lambda: w.fsync_count == 1)
    assert _wait(lambda: w._fsync_thread is None)        # idle → ended
    n = w.fsync_count
    w.append("y")
    w.flush_sync()                                        # restarted by the next write
    assert _wait(lambda: w.fsync_count == n + 1)
    w.stop()


def test_stop_and_seal_fsync_before_closing(tmp_path, fsyncs, monkeypatch):
    monkeypatch.setattr(AOFWriter, "_FSYNC_INTERVAL", 3600)   # the thread never fires
    path = tmp_path / "library.aof"
    w = AOFWriter(path)
    w.append("a")
    w.stop()                                             # the snapshot's / shutdown's stop
    assert [t for t, i in fsyncs if i == path.stat().st_ino] == [threading.current_thread().name]
    fsyncs.clear()
    w.append("b")
    w.close_intake()
    w.seal()
    assert len([1 for _, i in fsyncs if i == path.stat().st_ino]) == 1
    assert path.read_text().count("\n") == 2
    assert _wait(lambda: w._fsync_thread is None)        # sealed: the thread ends


def test_policy_no_never_fsyncs(tmp_path, fsyncs):
    w = AOFWriter(tmp_path / "library.aof", fsync_policy="no")
    w.append("a")
    w.flush_sync()
    time.sleep(0.3)
    assert w._fsync_thread is None and w.fsync_count == 0
    with pytest.raises(ValueError):
        AOFWriter(tmp_path / "x.aof", fsync_policy="always-maybe")


def test_a_write_racing_the_threads_idle_exit_is_still_synced(tmp_path, fsyncs, monkeypatch):
    """A write landing between the thread's idle check and its exit (the writer
    saw the thread alive and started none) must keep the thread going: the exit
    re-checks the write generation under the thread lock."""
    monkeypatch.setattr(AOFWriter, "_FSYNC_IDLE_EXIT", 1)
    w = AOFWriter(tmp_path / "library.aof")
    w.append("a")
    w.flush_sync()
    assert _wait(lambda: w._synced_gen == w._write_gen == 1)

    class RacyLock:
        """The thread's exit takes this lock: a write lands right before."""
        def __init__(self):
            self.lock, self.fired = threading.Lock(), False

        def __enter__(self):
            if threading.current_thread().name == "aof-fsync" and not self.fired:
                self.fired = True
                w._write_gen += 1                     # written; the writer saw the thread alive
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)
    w._fsync_tlock = RacyLock()
    assert _wait(lambda: w._fsync_tlock.fired)
    assert _wait(lambda: w._synced_gen == w._write_gen == 2)   # synced, not abandoned
    w.stop()


def test_a_failed_dup_does_not_end_fsyncing(tmp_path, fsyncs, monkeypatch):
    w = AOFWriter(tmp_path / "library.aof")
    real_dup, calls = os.dup, []

    def flaky_dup(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(24, "Too many open files")
        return real_dup(fd)
    monkeypatch.setattr(aof_mod.os, "dup", flaky_dup)
    w.append("a")
    w.flush_sync()
    assert _wait(lambda: w.fsync_count >= 1)              # retried in the next period
    assert w._synced_gen == w._write_gen
    w.stop()
