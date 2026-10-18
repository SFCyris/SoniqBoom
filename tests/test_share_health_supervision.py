# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The share health monitor keeps running through errors: a pass that raises
is logged and retried, one share's failure never ends the pass for the
others, a monitor that dies is restarted by its supervisor, and shutdown
cancels it without a restart."""
from __future__ import annotations

import asyncio
import types

import pytest

from soniqboom import main
from soniqboom.core import filesource
from soniqboom.core.store import TrackStore

ROOT = "ftp://127.0.0.1/health-sup"
_real_sleep = asyncio.sleep


class _Src:
    """A share whose server is up or down as the test says."""

    def __init__(self, up: bool):
        self.up = up
        self.last_error = None
        self.last_error_auth = False
        self.auth_key = None

    def is_dir(self, path):
        return self.up

    def reconnect(self):
        return False

    def close(self):
        pass


@pytest.fixture
def env(tmp_data_dir, monkeypatch):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr(main, "_reconnect_pending_shares", _no_pending)
    monkeypatch.setattr(main, "_health_task", None)
    monkeypatch.setattr(main, "_health_stopping", False)
    yield store
    filesource.remove_source(ROOT)


async def _no_pending():
    return False


def _fast_asyncio(monkeypatch, passes: list, limit: int):
    """main's own view of asyncio with an instant ``sleep`` that cancels the
    caller after ``limit`` sleeps (the event loop's sleeps stay real)."""
    async def _fast_sleep(t):
        passes.append(t)
        if len(passes) > limit:
            raise asyncio.CancelledError
        await _real_sleep(0)
    fake = types.SimpleNamespace(**{n: getattr(asyncio, n) for n in dir(asyncio)
                                    if not n.startswith("__")})
    fake.sleep = _fast_sleep
    monkeypatch.setattr(main, "asyncio", fake)


def _status(store):
    return {d["path"]: d.get("status") for d in store.list_scan_dirs()}


async def test_a_pass_that_raises_once_does_not_end_recovery(env, monkeypatch):
    from soniqboom.core.data import upsert_scan_dir
    store = env
    await upsert_scan_dir(ROOT, network_share_id="s1", status="unavailable")
    src = _Src(up=True)                      # the server is back
    filesource.register_source(ROOT, src)
    real_all = filesource.all_sources
    calls = {"n": 0}

    def _flaky_all_sources():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("registry snapshot failed")
        return real_all()
    monkeypatch.setattr(filesource, "all_sources", _flaky_all_sources)
    passes: list = []
    _fast_asyncio(monkeypatch, passes, limit=3)
    with pytest.raises(asyncio.CancelledError):
        await main._share_health_monitor()
    assert calls["n"] >= 2, "the monitor stopped after the failing pass"
    assert _status(store)[ROOT] == "ok", "recovery never happened after the error"
    # The failed pass backed off (a longer wait than the healthy cadence).
    assert passes[1] > passes[0]


async def test_a_status_write_that_raises_is_retried_next_pass(env, monkeypatch):
    from soniqboom.core.data import upsert_scan_dir
    store = env
    await upsert_scan_dir(ROOT, network_share_id="s1", status="ok")
    src = _Src(up=False)
    filesource.register_source(ROOT, src)
    real_set = main._set_share_status
    seen: list = []

    async def _flaky_set(root, status):
        seen.append(status)
        if len(seen) == 1:
            raise OSError("disk full")          # the first flip fails
        await real_set(root, status)
    monkeypatch.setattr(main, "_set_share_status", _flaky_set)
    passes: list = []

    async def _fast_sleep(t):
        passes.append(t)
        if len(passes) == 5:
            src.up = True                        # the server comes back
        if len(passes) > 7:
            raise asyncio.CancelledError
        await _real_sleep(0)
    fake = types.SimpleNamespace(**{n: getattr(asyncio, n) for n in dir(asyncio)
                                    if not n.startswith("__")})
    fake.sleep = _fast_sleep
    monkeypatch.setattr(main, "asyncio", fake)
    with pytest.raises(asyncio.CancelledError):
        await main._share_health_monitor()
    # failed write, its retry on the next pass, then the recovery
    assert seen == ["unavailable", "unavailable", "ok"], seen
    assert _status(store)[ROOT] == "ok"


async def test_the_supervisor_restarts_a_monitor_that_died(env, monkeypatch):
    runs = {"n": 0}
    real_monitor = main._share_health_monitor

    async def _dies_once():
        runs["n"] += 1
        if runs["n"] == 1:
            raise RuntimeError("boom")
        await real_monitor()
    monkeypatch.setattr(main, "_share_health_monitor", _dies_once)
    passes: list = []
    _fast_asyncio(monkeypatch, passes, limit=3)
    with pytest.raises(asyncio.CancelledError):
        await main._share_health_supervisor()
    assert runs["n"] == 2
    assert passes[0] == main._HEALTH_RESTART_BASE_S      # the restart delay


async def test_a_cancellation_from_inside_the_monitor_restarts_it(env, monkeypatch):
    """A CancelledError raised by something the monitor awaited (not a
    cancellation of the task) must not end supervision."""
    runs = {"n": 0}

    async def _cancelled_once():
        runs["n"] += 1
        if runs["n"] == 1:
            raise asyncio.CancelledError()
        await _real_sleep(3600)
    monkeypatch.setattr(main, "_share_health_monitor", _cancelled_once)
    monkeypatch.setattr(main, "_HEALTH_RESTART_BASE_S", 0.01)
    t = main._start_share_health_monitor()
    for _ in range(100):
        await _real_sleep(0.02)
        if runs["n"] >= 2:
            break
    assert runs["n"] == 2 and not t.done()
    await main._stop_share_health_monitor(timeout=1.0)
    assert t.cancelled()


async def test_a_task_that_ends_is_started_again_and_shutdown_stops_it(env, monkeypatch):
    monkeypatch.setattr(main, "_HEALTH_RESTART_BASE_S", 0.05)
    started = {"n": 0}

    async def _sup():
        started["n"] += 1
        if started["n"] == 1:
            raise RuntimeError("supervisor died")
        await _real_sleep(3600)
    monkeypatch.setattr(main, "_share_health_supervisor", _sup)
    first = main._start_share_health_monitor()
    for _ in range(100):
        await _real_sleep(0.02)
        if started["n"] >= 2:
            break
    assert started["n"] == 2, "the dead monitor task was not restarted"
    second = main._health_task
    assert second is not first and not second.done()
    await main._stop_share_health_monitor(timeout=1.0)
    assert second.cancelled()
    await _real_sleep(0.2)                   # longer than the restart delay
    assert main._health_task is second and started["n"] == 2, "restarted after shutdown"


async def test_shutdown_cancels_the_real_monitor_cleanly(env, monkeypatch):
    t = main._start_share_health_monitor()
    await _real_sleep(0.05)
    assert not t.done()
    await main._stop_share_health_monitor(timeout=1.0)
    assert t.cancelled()
