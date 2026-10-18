# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Nothing the playback work started outlives the server's stop or its
in-place restart: renderer processes (and the processes below them) are
ended and waited for, their temp files removed (``core.playback_stop``,
``core.render_tmp``); the image an in-place restart starts waits for the
children the previous image left (``main._reap_exec_leftovers``)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from soniqboom.core import forksafe, render_tmp


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                         text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


@pytest.fixture
def own_tmp(monkeypatch, tmp_path):
    """The system temp folder is a fresh one for this test."""
    t = tmp_path / "T"
    t.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(t))
    monkeypatch.setattr(render_tmp, "_made", None)
    yield t
    forksafe.open_spawns()


async def test_stop_ends_renderers_their_children_and_temp_files(own_tmp):
    from soniqboom.core import conversion_cache as cc
    from soniqboom.core import playback_stop
    rdir = render_tmp.path()
    assert rdir.parent == own_tmp and rdir.name.startswith("soniqboom-render-")
    # 1. a render task (as conversion_cache runs one): its cancel ends it
    tmp_a = rdir / "tmpA.wav"
    tmp_a.write_bytes(b"RIFF....")
    started = asyncio.Event()

    async def _render():
        proc = await forksafe.spawn("/bin/sleep", "30")
        started.set()
        try:
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            tmp_a.unlink(missing_ok=True)
    task = asyncio.ensure_future(_render())
    cc._detached_renders.add(task)
    await started.wait()
    # 2. a renderer no task owns any more, with a process below it
    orphan = await forksafe.spawn("/bin/sh", "-c", "sleep 30 & echo $!; wait",
                                  stdout=asyncio.subprocess.PIPE)
    grandchild = int((await orphan.stdout.readline()).strip())
    # 3. one that ignores SIGTERM (so does its child): needs the SIGKILL
    stubborn = await forksafe.spawn("/bin/sh", "-c", "trap '' TERM; sleep 30 & wait")
    # 4. a temp file nothing deletes
    (rdir / "tmpB.wav").write_bytes(b"x" * 5000)
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    res = await playback_stop.stop_playback_work(task_wait=1.0, term_wait=0.5, kill_wait=1.5)
    took = time.monotonic() - t0
    cc._detached_renders.discard(task)
    assert task.done()
    assert orphan.returncode is not None and stubborn.returncode is not None
    assert res["killed"] >= 1 and res["still_running"] == 0, res
    await asyncio.sleep(0.2)
    assert not _alive(grandchild)
    assert not rdir.exists()
    assert not any(own_tmp.iterdir())
    assert took < 4.0
    # spawns stay closed until the next run opens them
    with pytest.raises(forksafe.SpawnsClosed):
        await forksafe.spawn("/bin/echo", "x")
    forksafe.open_spawns()
    p = await forksafe.spawn("/bin/echo", "x", stdout=asyncio.subprocess.DEVNULL)
    assert await p.wait() == 0
    assert render_tmp.path().is_dir()                   # made again on use


async def test_live_processes_forgets_ended_ones():
    p = await forksafe.spawn("/bin/sleep", "5")
    assert p in forksafe.live_processes()
    p.kill()
    await p.wait()
    assert p not in forksafe.live_processes()


def test_sweep_removes_only_folders_of_gone_images(own_tmp):
    mine = render_tmp.path()
    (mine / "tmpkeep.wav").write_bytes(b"k")
    me = os.getpid()
    h = render_tmp._HOST
    prev_image = own_tmp / f"soniqboom-render-{h}-{me}-00000000"   # this pid, earlier image
    prev_image.mkdir()
    (prev_image / "tmp1.wav").write_bytes(b"a" * 100)
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    gone = own_tmp / f"soniqboom-render-{h}-{dead.pid}-abcdef01"
    gone.mkdir()
    (gone / "tmp2.wav").write_bytes(b"b" * 50)
    other_live = own_tmp / f"soniqboom-render-{h}-{os.getppid()}-abcdef02"   # a running process
    other_host = own_tmp / f"soniqboom-render-{'0' * 8 if h != '0' * 8 else '1' * 8}-{dead.pid}-abcdef03"
    other_host.mkdir()                                  # another container sharing /tmp
    other_live.mkdir()
    lookalike = own_tmp / "soniqboom-render-notapid"
    lookalike.mkdir()
    generic = own_tmp / "tmpabcd.wav"                   # origin unknown: never removed
    generic.write_bytes(b"g" * 10)
    other_prog = own_tmp / "someone-else.wav"
    other_prog.write_bytes(b"o")
    folders, files, nbytes = render_tmp.sweep_orphans()
    assert (folders, files, nbytes) == (2, 2, 150)
    assert not prev_image.exists() and not gone.exists()
    assert mine.exists() and (mine / "tmpkeep.wav").exists()
    assert other_live.exists() and lookalike.exists() and other_host.exists()
    assert generic.exists() and other_prog.exists()
    assert render_tmp.stray_temp_wavs() == (1, 10)


def test_exec_image_reaps_and_ends_the_previous_images_children(monkeypatch):
    from soniqboom import main
    ended = subprocess.Popen(["/usr/bin/true"])          # will be a zombie (never waited)
    running = subprocess.Popen(["/bin/sleep", "30"])
    stubborn = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; while :; do sleep 0.1; done"])
    bystander = subprocess.Popen(["/bin/sleep", "30"])   # not recorded: left alone
    ours = {ended.pid, running.pid, stubborn.pid}
    time.sleep(0.3)
    real = main._own_children
    monkeypatch.setattr(main, "_own_children",
                        lambda: {p: v for p, v in real().items() if p in ours})
    try:
        assert main.note_exec_leftovers() == 3
        raw = os.environ.pop(main._EXEC_LEFTOVERS_ENV)       # what the exec'd image reads
        reaped, signalled = main._reap_exec_leftovers(raw)
        assert signalled == 2 and reaped == 3
        for pid in ours:
            with pytest.raises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)              # reaped: no zombie left
        assert bystander.poll() is None
        assert main._reap_exec_leftovers() == (0, 0)     # nothing recorded for this image
    finally:
        for p in (ended, running, stubborn, bystander):
            try:
                p.kill()
            except OSError:
                pass
        bystander.wait()
        os.environ.pop(main._EXEC_LEFTOVERS_ENV, None)


def test_a_recorded_pid_running_another_program_is_left_alone(monkeypatch):
    from soniqboom import main
    p = subprocess.Popen(["/bin/sleep", "30"])
    try:
        time.sleep(0.2)
        real = main._own_children
        monkeypatch.setattr(main, "_own_children",
                            lambda: {q: v for q, v in real().items() if q == p.pid})
        assert main._reap_exec_leftovers(f'[[{p.pid}, "/opt/other --program"]]') == (0, 0)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()
        os.environ.pop(main._EXEC_LEFTOVERS_ENV, None)


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_the_stop_counts_running_pool_jobs_not_idle_threads(loop, monkeypatch, caplog):
    """Many idle pool threads and one job still running: the stop's warning
    says 1, not the number of threads."""
    import logging
    import threading
    from soniqboom import main
    monkeypatch.setattr(main, "_process_exiting", False)
    release = threading.Event()

    async def run():
        main._install_thread_pool()
        lp = asyncio.get_running_loop()
        await asyncio.gather(*(lp.run_in_executor(None, time.sleep, 0.05) for _ in range(12)))
        lp.run_in_executor(None, release.wait, 30)               # one job still running
        await asyncio.sleep(0.05)
        assert main._thread_pool.running_jobs() == 1
        return await main._release_thread_pool({})
    caplog.set_level(logging.WARNING, logger="soniqboom")
    try:
        assert (asyncio.run if loop == "asyncio" else __import__("uvloop").run)(run()) is True
    finally:
        release.set()
    msgs = [r.getMessage() for r in caplog.records if "thread-pool job" in r.getMessage()]
    assert msgs and msgs[-1].startswith("Shutdown: 1 thread-pool job(s) still running"), msgs


def test_the_recorded_children_are_read_once_as_the_image_loads(tmp_path):
    """The record is taken out of the environment at import (no process the
    image starts inherits it); a start reads it once."""
    import json
    code = ("import os, sys; os.environ['SONIQBOOM_EXEC_LEFTOVERS'] = '[[1, \"x\"]]'\n"
            "from soniqboom import main\n"
            "print(os.environ.get('SONIQBOOM_EXEC_LEFTOVERS'), main._exec_leftovers_raw)\n"
            "main._own_children = lambda: {}\n"
            "print(main._reap_exec_leftovers(), main._exec_leftovers_raw)\n")
    env = {k: v for k, v in os.environ.items() if k != "SONIQBOOM_EXEC_LEFTOVERS"}
    (tmp_path / "home").mkdir()
    (tmp_path / "SoniqBoom.conf").write_text(json.dumps({"data_dir": str(tmp_path / "data")}))
    env.update(HOME=str(tmp_path / "home"), SONIQBOOM_CONF=str(tmp_path / "SoniqBoom.conf"),
               SONIQBOOM_DATA_DIR=str(tmp_path / "data"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=str(Path(__file__).resolve().parent.parent), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    lines = out.stdout.strip().splitlines()[-2:]
    assert lines[0] == 'None [[1, "x"]]' and lines[1] == "(0, 0) None", lines


def test_the_restart_writes_the_journal_before_it_refuses_requests(monkeypatch):
    """The wait for a merge holding the AOF happens while requests are still
    answered; the refusing window starts after it."""
    from soniqboom import main
    from soniqboom.api import admin
    seen = []

    class _W:
        def drain(self, budget):
            seen.append(("drain", main._exec_restarting, budget))
            return True
    monkeypatch.setenv(main._EXEC_LEFTOVERS_ENV, "")
    monkeypatch.setattr(main, "_aof_writer", _W())
    monkeypatch.setattr(main, "_exec_restarting", False)
    monkeypatch.setattr(admin, "_detect_app_bundle", lambda: None)
    monkeypatch.setattr(admin, "_graceful_pre_exec_flush",
                        lambda loop=None: seen.append(("flush", main._exec_restarting)))
    monkeypatch.setattr(main, "note_exec_leftovers", lambda: seen.append(("note", True)) or 0)
    monkeypatch.setattr(admin.os, "execv", lambda *a: seen.append(("exec", main._exec_restarting)))
    monkeypatch.setattr(main._core_users, "get_user_store",
                        lambda: type("S", (), {"close_sign_ins": lambda self: None})())
    admin._do_restart(None)
    assert seen == [("drain", False, admin._PRE_GATE_DRAIN_S), ("flush", True), ("note", True),
                    ("exec", True)]


def test_drain_waits_out_a_merge_and_keeps_the_journal_open(tmp_path):
    import fcntl
    import threading
    from soniqboom.core.aof import AOFWriter
    w = AOFWriter(tmp_path / "library.aof")
    w.append("set_rating", id="a", rating=3)
    held = open(tmp_path / "library.aof", "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)              # a merge holds the AOF
    threading.Timer(0.6, lambda: fcntl.flock(held.fileno(), fcntl.LOCK_UN)).start()
    t0 = time.monotonic()
    assert w.drain(5.0) is True
    assert 0.5 < time.monotonic() - t0 < 4.0
    assert b'"set_rating"' in (tmp_path / "library.aof").read_bytes()
    assert not w.sealed
    w.append("set_rating", id="b", rating=4)
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    t0 = time.monotonic()
    assert w.drain(0.3) is False                            # budget spent: the record stays buffered
    assert time.monotonic() - t0 < 3.5
    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    held.close()
    assert w.drain(1.0) is True
    w.stop()
