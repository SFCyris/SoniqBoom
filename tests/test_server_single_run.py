# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One server run at a time in a process: the bundled app runs the server in
a thread of its own process, and a Start clicked again during a start-up
must not start a second one (both would act on the same process-wide state
— the scan pools and their run epoch, the AOF writer, the merger,
downloads — and the second one's stop would stop the first one's scans)."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from soniqboom import main


@pytest.fixture
def no_owner(monkeypatch):
    monkeypatch.setattr(main, "_server_owner", None)


def _hold(started: threading.Event, done: threading.Event, err: list) -> threading.Thread:
    def run():
        try:
            main._claim_server()
        except Exception as exc:                    # noqa: BLE001
            err.append(exc)
        started.set()
        done.wait(10)
    t = threading.Thread(target=run, name="server-A", daemon=True)
    t.start()
    started.wait(5)
    return t


def test_a_second_run_is_refused_while_the_first_one_runs(no_owner):
    started, done, err = threading.Event(), threading.Event(), []
    a = _hold(started, done, err)
    assert not err
    with pytest.raises(RuntimeError, match="already runs"):
        main._claim_server()
    assert main._server_owner is a and not main._owns_server()
    done.set()
    a.join(5)
    main._claim_server()                            # its thread ended: no longer counts
    assert main._owns_server()
    main._release_server()
    assert main._server_owner is None


def test_a_refused_start_up_and_its_stop_touch_nothing(no_owner, monkeypatch):
    from soniqboom.core import game_titles, scanner
    started, done, err = threading.Event(), threading.Event(), []
    _hold(started, done, err)
    touched = []
    monkeypatch.setattr(main, "get_data_dir", lambda: touched.append("data_dir"))
    monkeypatch.setattr(game_titles, "cancel_downloads", lambda *a, **k: touched.append("downloads") or [])
    monkeypatch.setattr(scanner, "shutdown_worker_pools", lambda *a, **k: touched.append("pools") or True)
    epoch, closed = scanner._run_epoch, scanner._pools_closed
    try:
        with pytest.raises(RuntimeError, match="already runs"):
            asyncio.run(main.startup())
        asyncio.run(main.shutdown())                # a stop of the refused run
        assert touched == []
        assert scanner._run_epoch == epoch and scanner._pools_closed == closed
    finally:
        done.set()


def test_the_stop_uses_the_epoch_of_its_own_run(no_owner, monkeypatch):
    """The stop reads its run's epoch once, first: a start that follows the
    stop can't make it stop the new run's downloads or pools."""
    import inspect
    src = inspect.getsource(main.shutdown)
    assert src.index("_owns_server()") < src.index("run_epoch = _run_epoch") < src.index("cancel_downloads(run_epoch)")
    assert "shutdown_worker_pools, run_epoch" in src and "_release_server()" in src


# ── The bundled app's Start / Stop ──────────────────────────────────────────

class _FakeServer:
    """uvicorn.Server stand-in: ``run`` is its start-up until ``ready`` is
    set (``started`` only then, as uvicorn's), then serves until
    ``should_exit``, then its shutdown takes ``stop_s``."""
    made: list = []

    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False
        self.ready = threading.Event()
        self.stop_s = 0.0
        _FakeServer.made.append(self)

    def run(self):
        self.ready.wait(10)
        self.started = True
        while not self.should_exit:
            time.sleep(0.01)
        time.sleep(self.stop_s)


@pytest.fixture
def app(monkeypatch):
    pytest.importorskip("rumps")
    if sys.platform != "darwin":
        pytest.skip("the bundled app is macOS only")
    path = os.environ.get("PATH", "")
    import rumps
    soniqboom_app = pytest.importorskip("soniqboom_app")    # local-only packaging file
    os.environ["PATH"] = path                       # the module adds Homebrew's paths
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 8080
    _FakeServer.made = []
    notes = []
    monkeypatch.setattr(soniqboom_app.uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(soniqboom_app.rumps, "notification", lambda **k: notes.append(k["message"]))
    a = object.__new__(soniqboom_app.SoniqBoomApp)
    a._host, a._port = "127.0.0.1", port
    a._server = a._thread = a._stopping = None
    a._start_queued = a._start_notify = a._notify_stopped = False
    a._port_deadline = None
    for name in ("open", "restart", "settings", "toggle"):
        setattr(a, f"_item_{name}", rumps.MenuItem(name))
    monkeypatch.setattr(a, "_startup_phase_chip", lambda: None, raising=False)
    a.notes = notes
    yield a
    for srv in _FakeServer.made:
        srv.ready.set()
        srv.should_exit = True


def _until(cond, app=None, limit=5.0):
    t0 = time.monotonic()
    while not cond():
        if app is not None:
            app._advance()                          # the status timer's tick
        assert time.monotonic() - t0 < limit
        time.sleep(0.01)


def test_start_clicked_again_during_the_start_up_starts_nothing(app):
    app._start_server()
    assert app._is_starting and not app._is_running
    app._start_server()                             # clicked again, still starting
    assert len(_FakeServer.made) == 1
    app._refresh_menu()
    assert app.title == "\U0001f50a  Starting…" and app._item_toggle.title == "Stop Server"
    _FakeServer.made[0].ready.set()
    _until(lambda: app._is_running)
    app._start_server()                             # and while it serves
    assert len(_FakeServer.made) == 1
    app._refresh_menu()
    assert app.title == "\U0001f50a" and app._item_toggle.title == "Stop Server"
    app._stop_server()
    assert not app._is_alive and app._item_toggle.title == "Start Server"
    _until(lambda: app._stopping is None, app)
    assert app.title == "\U0001f507"


def test_stop_never_waits_on_the_menu_and_says_stopped_when_it_is(app):
    app._start_server()
    srv = _FakeServer.made[0]
    t0 = time.monotonic()
    app._on_toggle(None)                            # Stop during the start-up
    assert time.monotonic() - t0 < 0.1 and srv.should_exit
    assert app.title == "\U0001f507  Stopping…" and app.notes == []
    srv.ready.set()                                 # the start-up ends, then its shutdown
    _until(lambda: app._stopping is None, app)
    assert app.notes == ["Server stopped."] and app.title == "\U0001f507"
    assert len(_FakeServer.made) == 1


def test_start_during_a_stop_is_queued_and_made_once_it_ended(app):
    app._start_server()
    old = _FakeServer.made[0]
    old.ready.set()
    _until(lambda: app._is_running)
    old.stop_s = 0.5                                # a slow shutdown
    app._on_toggle(None)                            # Stop
    t0 = time.monotonic()
    app._on_toggle(None)                            # Start, clicked while it stops
    assert time.monotonic() - t0 < 0.1
    assert len(_FakeServer.made) == 1 and app.title == "\U0001f507  Restarting…"
    _until(lambda: len(_FakeServer.made) == 2, app)
    assert not app._stopping and app._is_alive     # the new one after the old one ended
    assert app.notes == ["Server stopped.", "Server starting..."]


def test_restart_never_waits_on_the_menu(app):
    app._start_server()
    old = _FakeServer.made[0]
    old.ready.set()
    _until(lambda: app._is_running)
    old.stop_s = 0.3
    t0 = time.monotonic()
    app._on_restart(None)
    assert time.monotonic() - t0 < 0.1 and app.title == "\U0001f507  Restarting…"
    _until(lambda: len(_FakeServer.made) == 2, app)
    assert "Server stopped." not in app.notes


def test_a_start_waits_for_the_port_then_gives_up(app, monkeypatch):
    import soniqboom_app
    monkeypatch.setattr(soniqboom_app, "_port_available", lambda h, p: False)
    monkeypatch.setattr(soniqboom_app, "_PORT_WAIT_SECONDS", 0.2)
    app._on_toggle(None)                            # Start
    assert app._start_queued and app.title == "\U0001f507  Starting…"
    _until(lambda: not app._start_queued, app)
    assert _FakeServer.made == [] and app.notes == [f"Port {app._port} is already in use"]


def test_quit_waits_for_the_shutdown(app, monkeypatch):
    import soniqboom_app
    monkeypatch.setattr(soniqboom_app.rumps, "quit_application", lambda: None)
    app._start_server()
    srv = _FakeServer.made[0]
    srv.ready.set()
    _until(lambda: app._is_running)
    srv.stop_s = 0.3
    t0 = time.monotonic()
    app._on_quit(None)
    assert time.monotonic() - t0 >= 0.3 and app._stopping is None


def test_the_app_opens_its_pages_without_forking(app, monkeypatch):
    """``webbrowser`` forks the app's process (``os.popen`` of osascript) —
    it also runs the server; the app opens a URL with ``/usr/bin/open``
    through ``forksafe.run`` (posix_spawn) instead."""
    import soniqboom_app
    calls = []
    hold = threading.Event()

    def fake_run(cmd, **kw):
        hold.wait(5)                                # a slow open: the menu doesn't wait for it
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(soniqboom_app.forksafe, "run", fake_run)
    t0 = time.monotonic()
    app._on_open(None)
    app._on_source(None)
    assert time.monotonic() - t0 < 0.1
    hold.set()
    _until(lambda: len(calls) == 2)
    assert sorted(calls) == sorted([["/usr/bin/open", f"http://127.0.0.1:{app._port}"],
                                    ["/usr/bin/open", "https://github.com/SFCyris/SoniqBoom"]])


def test_a_failed_open_is_logged(app, monkeypatch, caplog):
    import logging
    import soniqboom_app
    monkeypatch.setattr(soniqboom_app.forksafe, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "LSOpenURLsWithRole() failed"))
    with caplog.at_level(logging.ERROR, logger="soniqboom.app"):
        soniqboom_app._open_url("http://127.0.0.1:1/")
        _until(lambda: any("Could not open" in r.getMessage() for r in caplog.records))
    assert "LSOpenURLsWithRole" in caplog.records[-1].getMessage()


def test_the_bundled_apps_merger_stops_without_a_final_merge(monkeypatch, tmp_path):
    """The bundled app's merger is an asyncio task (no ``is_alive``): the
    server's stop stops it cooperatively, with no final rewrite of the
    library — the AOF was just flushed and the next start replays it."""
    import inspect
    from soniqboom.core import merger
    merges = []
    monkeypatch.setattr(merger, "_do_merge_in_child", lambda d: merges.append(d) or 0)
    monkeypatch.setattr(merger, "_is_bundled", lambda: True)

    async def go(final):
        task = merger.start_merger(tmp_path, interval=3600)
        await asyncio.sleep(0.05)
        await merger.stop_merger(task, final=final)
        return task.done()
    assert asyncio.run(go(False)) and merges == []
    assert asyncio.run(go(True)) and len(merges) == 1
    src = inspect.getsource(main.shutdown)
    assert "isinstance(_merger_proc, asyncio.Task)" in src and "stop_merger(_merger_proc, final=False)" in src
    assert "merger" in main._WRITING_STEPS


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
@pytest.mark.parametrize("exiting", [False, True])
def test_a_job_stuck_on_a_hung_share_does_not_hold_the_stopped_server(loop, exiting, monkeypatch):
    """The loop's close waits up to 300 s for its thread-pool jobs; a job
    stuck on a share that stopped answering held the server's thread that
    long (and the app's Restart with it).  The stop releases the pool at
    once — only when the process exits after it (Quit) do running jobs get a
    moment to finish (a tag write)."""
    monkeypatch.setattr(main, "_POOL_GRACE_S", 0.5)
    monkeypatch.setattr(main, "_process_exiting", exiting)
    release = threading.Event()
    done = []

    async def run(results):
        main._install_thread_pool()
        loop_ = asyncio.get_running_loop()
        loop_.run_in_executor(None, release.wait, 30)                     # stuck
        loop_.run_in_executor(None, lambda: (time.sleep(0.2), done.append(1)))   # a short write
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        released = await main._release_thread_pool(results)
        return released, time.monotonic() - t0
    runner = asyncio.run if loop == "asyncio" else __import__("uvloop").run
    try:
        released, took = runner(run({"aof-flush": "ok", "watcher": "TIMEOUT"}))
        assert released is True
        if exiting:
            assert done == [1] and took < 0.8        # the short job finished, the stuck one isn't waited for
        else:
            assert took < 0.1                        # Stop / Restart: nothing waited for
    finally:
        release.set()


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_background_work_goes_on_while_the_pool_is_released(loop, monkeypatch):
    """The fresh pool is in place before the old one is shut down: a task
    still ending never gets "cannot schedule new futures after shutdown"."""
    monkeypatch.setattr(main, "_POOL_GRACE_S", 0.5)
    monkeypatch.setattr(main, "_process_exiting", True)
    errors, calls = [], []

    async def worker(stop):
        while not stop.is_set():
            try:
                await asyncio.to_thread(time.sleep, 0.01)
                calls.append(1)
            except RuntimeError as exc:                 # "cannot schedule new futures after shutdown"
                errors.append(exc)
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # a job still queued in the old pool when it was released: dropped, as meant

    async def run():
        main._install_thread_pool()
        stop = asyncio.Event()
        t = asyncio.create_task(worker(stop))
        asyncio.get_running_loop().run_in_executor(None, time.sleep, 0.4)   # keeps the grace busy
        await asyncio.sleep(0.05)
        await main._release_thread_pool({})
        await asyncio.to_thread(time.sleep, 0)      # right after: works
        stop.set()
        await t
    (asyncio.run if loop == "asyncio" else __import__("uvloop").run)(run())
    assert errors == [] and len(calls) > 5


@pytest.mark.parametrize("status", ["TIMEOUT", "FAIL"])
def test_a_writing_step_that_did_not_end_cleanly_is_still_waited_for(status):
    done = []

    async def run():
        main._install_thread_pool()
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, lambda: (time.sleep(0.4), done.append(1)))  # a late snapshot write
        await asyncio.sleep(0.05)
        return await main._release_thread_pool({"snapshot": status})
    assert asyncio.run(run()) is False and done == [1]             # the close waited for it
    src = __import__("inspect").getsource(main.shutdown)
    assert src.index("await _release_thread_pool(_results)") < src.index("_release_server()")
    assert src_startup_installs_pool()


def src_startup_installs_pool() -> bool:
    import inspect
    src = inspect.getsource(main.startup)
    return src.index("_claim_server()") < src.index("_install_thread_pool()")


def test_a_merge_of_a_stopped_run_finishes_before_the_next_run_loads(tmp_path, monkeypatch):
    """The library's files are rewritten (merge, snapshot) and loaded under
    one lock: a start never reads them while a stopped run's merge writes."""
    from soniqboom.core import persistence
    order = []
    started = threading.Event()

    def slow_merge():
        with persistence.library_files_lock:
            started.set()
            time.sleep(0.4)
            order.append("merge done")
    t = threading.Thread(target=slow_merge)
    t.start()
    started.wait(5)
    monkeypatch.setattr(persistence, "load_snapshot", lambda d: order.append("load") or {})
    monkeypatch.setattr(persistence, "replay_aof", lambda st, d: None)
    monkeypatch.setattr(persistence, "populate_store", lambda st: None)
    monkeypatch.setattr(persistence, "make_prev_backup", lambda d: None)
    persistence.init_persistence(tmp_path)
    t.join(5)
    assert order == ["merge done", "load"]
    from soniqboom.core import merger
    held = []

    def taken_elsewhere() -> bool:                  # another thread can't take it meanwhile
        got = []
        t = threading.Thread(target=lambda: got.append(persistence.library_files_lock.acquire(timeout=0.05)))
        t.start()
        t.join()
        if got[0]:
            persistence.library_files_lock.release()
        return not got[0]
    monkeypatch.setattr(merger, "_do_merge_locked", lambda d: held.append(("merge", taken_elsewhere())) or 0)
    monkeypatch.setattr(persistence, "_write_snapshot_sync", lambda d, *a: held.append(("snapshot", taken_elsewhere())))
    merger._do_merge(tmp_path)
    persistence.write_snapshot_sync(tmp_path)
    assert held == [("merge", True), ("snapshot", True)]


def test_stop_during_a_restart_cancels_the_queued_start(app):
    app._start_server()
    old = _FakeServer.made[0]
    old.ready.set()
    _until(lambda: app._is_running)
    old.stop_s = 0.3
    app._on_restart(None)
    assert app._item_toggle.title == "Stop Server" and app.title == "\U0001f507  Restarting…"
    app._on_toggle(None)                            # Stop: the restart's start is dropped
    assert app.title == "\U0001f507  Stopping…" and app._item_toggle.title == "Start Server"
    _until(lambda: app._stopping is None, app)
    time.sleep(0.1)
    app._advance()
    assert len(_FakeServer.made) == 1 and not app._is_alive and app.notes[-1] == "Server stopped."


def test_a_failed_start_up_is_announced(app, monkeypatch):
    import soniqboom_app

    class _Failing(_FakeServer):
        def run(self):
            return                                  # uvicorn's sys.exit(1) at a bind / start-up error
    monkeypatch.setattr(soniqboom_app.uvicorn, "Server", _Failing)
    app._on_toggle(None)
    _until(lambda: app._thread is None, app)
    assert app.notes == ["Server starting...", "The server could not start — see the log"]
    assert app.title == "\U0001f507" and app._item_toggle.title == "Start Server"


def test_the_status_timer_really_switches_its_interval(app):
    class _Timer:
        def __init__(self):
            self.interval, self.calls = 5, []

        def stop(self):
            self.calls.append("stop")

        def start(self):
            self.calls.append(("start", self.interval))
    app._timer = _Timer()
    app._start_server()                             # starting: 1 s polling
    assert app._timer.calls == ["stop", ("start", 1)]
    _FakeServer.made[0].ready.set()
    _until(lambda: app._is_running)
    app._advance()                                  # serving: back to 5 s
    assert app._timer.calls[-2:] == ["stop", ("start", 5)]


def test_the_writing_steps_and_the_queued_jobs():
    assert main._WRITING_STEPS == {"aof-flush", "snapshot", "browse-cache", "remote-freshness", "merger",
                                   "aof-seal"}
    ran = []

    async def run():
        main._install_thread_pool()
        loop = asyncio.get_running_loop()
        block = threading.Event()
        for _ in range(64):                          # more than the pool has threads: some stay queued
            loop.run_in_executor(None, lambda: (block.wait(0.3), ran.append(1)))
        await asyncio.sleep(0.05)
        released = await main._release_thread_pool({})
        block.set()
        return released
    assert asyncio.run(run()) is True
    time.sleep(0.5)
    assert len(ran) < 64                            # queued jobs were cancelled, not run after the stop


def test_quit_drops_a_queued_start(app, monkeypatch):
    import soniqboom_app
    monkeypatch.setattr(soniqboom_app.rumps, "quit_application", lambda: None)
    app._start_server()
    old = _FakeServer.made[0]
    old.ready.set()
    _until(lambda: app._is_running)
    old.stop_s = 0.3
    app._on_restart(None)                           # a start queued behind the stop
    app._on_quit(None)
    app._advance()
    assert len(_FakeServer.made) == 1 and not app._start_queued


def test_quit_tells_the_stop_that_the_process_exits(app, monkeypatch):
    import soniqboom_app
    monkeypatch.setattr(soniqboom_app.rumps, "quit_application", lambda: None)
    monkeypatch.setattr(main, "_process_exiting", False)
    app._on_quit(None)
    assert main._process_exiting is True
