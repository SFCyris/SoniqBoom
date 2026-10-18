# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A root the folder watcher can't watch (Linux: the inotify watch limit,
``ENOSPC``) never stops the others: it is retried with back-off, rescanned on
a poll cadence meanwhile, watched again once the limit allows (with one
catch-up rescan), and its state is reported.

The first tests run the real watchdog observer of this platform with its
emitter made to fail the way inotify does for one root.  The last one uses
real inotify with a lowered per-user-namespace watch limit; it runs only on
Linux as root of its own user namespace (``unshare -U -r``)."""
from __future__ import annotations

import asyncio
import errno
import os
import sys
import time
from pathlib import Path

import pytest

from soniqboom.core import scanner, watcher

pytest.importorskip("watchdog")


def _reset_state(monkeypatch):
    for attr, val in (("enabled", False), ("observer", None), ("watches", {}),
                      ("pending", {}), ("debounce_task", None), ("loop", None),
                      ("supervisor", None), ("arm_lock", None), ("roots", set()),
                      ("status", {}), ("handlers", {})):
        monkeypatch.setattr(watcher._state, attr, val)


@pytest.fixture
def fast(monkeypatch):
    _reset_state(monkeypatch)
    monkeypatch.setattr(watcher, "_QUIET_SEC", 0.3)
    monkeypatch.setattr(watcher, "_RETRY_BASE_SEC", 0.4)
    monkeypatch.setattr(watcher, "_RETRY_MAX_SEC", 0.8)
    monkeypatch.setattr(watcher, "_FALLBACK_POLL_SEC", 0.6)
    monkeypatch.setattr(watcher, "_SUPERVISE_SEC", 0.1)
    calls: list = []

    async def spy(dirs, on_progress=None, *, scope=None, rescan_if_running=False, light=False):
        calls.append({"dirs": sorted(str(d) for d in dirs),
                      "scope": {k: sorted(v) for k, v in (scope or {}).items()} or None,
                      "light": light})
    monkeypatch.setattr(scanner, "start_scan", spy)
    return calls


def _limited_observer(monkeypatch, fail_root: Path, limit: dict):
    """The platform's real observer, whose emitter for ``fail_root`` fails to
    start the way watchdog's inotify does at the watch limit while
    ``limit["on"]``."""
    real_observer = watcher.Observer

    def _make():
        obs = real_observer()
        base = obs._emitter_class

        class _Limited(base):
            def on_thread_start(self):
                if limit["on"] and os.path.realpath(self.watch.path) == str(fail_root):
                    limit["refusals"] += 1
                    raise OSError(errno.ENOSPC, "inotify watch limit reached")
                super().on_thread_start()
        obs._emitter_class = _Limited
        return obs
    monkeypatch.setattr(watcher, "Observer", _make)


async def _until(pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def _scoped_for(calls, root: Path) -> list:
    return [c for c in calls if c["scope"] and str(root) in c["scope"]]


def _full_for(calls, root: Path) -> list:
    return [c for c in calls if not c["scope"] and c["dirs"] == [str(root)] and c["light"]]


async def test_one_root_over_the_limit_never_stops_the_others(fast, monkeypatch, tmp_path):
    calls = fast
    a = (tmp_path / "A").resolve()
    b = (tmp_path / "B").resolve()
    for d in (a, b):
        (d / "Album").mkdir(parents=True)
    limit = {"on": True, "refusals": 0}
    _limited_observer(monkeypatch, b, limit)
    await watcher.start([str(a), str(b)])
    try:
        assert watcher.root_status(str(a))["state"] == "watching"
        st = watcher.root_status(str(b))
        assert st["state"] == "retrying" and "watch limit" in st["reason"], st
        assert st["polling"] and st["retry_in_s"] is not None
        assert watcher.summary() == {"state": "degraded", "roots": 2, "watching": 1,
                                     "not_watching": 1, "missing": 0}
        # The observer runs: root A keeps delivering events.
        await asyncio.sleep(1.0)                 # a fresh FSEvents stream can miss the first ~0.5 s
        (a / "Album" / "new.mod").write_bytes(b"x" * 64)
        assert await _until(lambda: _scoped_for(calls, a)), calls
        # Root B is rescanned on the poll cadence while it isn't watched…
        assert await _until(lambda: _full_for(calls, b)), calls
        # …and re-armed with back-off (more than one refusal so far).
        assert await _until(lambda: limit["refusals"] >= 2)
        assert watcher.root_status(str(b))["attempts"] >= 2
    finally:
        await watcher.stop()


async def test_a_root_is_watched_again_once_the_limit_is_lifted(fast, monkeypatch, tmp_path):
    calls = fast
    a = (tmp_path / "A").resolve()
    b = (tmp_path / "B").resolve()
    for d in (a, b):
        (d / "Album").mkdir(parents=True)
    limit = {"on": True, "refusals": 0}
    _limited_observer(monkeypatch, b, limit)
    await watcher.start([str(a), str(b)])
    try:
        assert watcher.root_status(str(b))["state"] == "retrying"
        limit["on"] = False                                   # the limit is raised
        assert await _until(lambda: watcher.root_status(str(b))["state"] == "watching")
        # One catch-up rescan of B for what changed while it wasn't watched.
        assert await _until(lambda: _full_for(calls, b)), calls
        assert watcher.summary()["state"] == "ok"
        calls.clear()
        await asyncio.sleep(1.0)                 # a fresh FSEvents stream can miss the first ~0.5 s
        (b / "Album" / "late.mod").write_bytes(b"y" * 64)
        assert await _until(lambda: _scoped_for(calls, b)), calls
        # Watched again: no more fallback polls of B.
        n = len(_full_for(calls, b))
        await asyncio.sleep(1.0)
        assert len(_full_for(calls, b)) == n
    finally:
        await watcher.stop()


async def test_a_root_added_at_runtime_over_the_limit_is_isolated(fast, monkeypatch, tmp_path):
    calls = fast
    a = (tmp_path / "A").resolve()
    b = (tmp_path / "B").resolve()
    for d in (a, b):
        d.mkdir()
    limit = {"on": True, "refusals": 0}
    _limited_observer(monkeypatch, b, limit)
    await watcher.start([str(a)])
    try:
        await watcher.add_root(str(b))
        assert watcher.root_status(str(b))["state"] == "retrying"
        # The failed schedule left no handler behind on the observer.
        obs = watcher._state.observer
        assert not [w for w in obs._handlers if w.path == str(b)]
        await asyncio.sleep(1.0)                 # a fresh FSEvents stream can miss the first ~0.5 s
        (a / "x.mod").write_bytes(b"x" * 64)
        assert await _until(lambda: _scoped_for(calls, a)), calls
        # Removing B stops its retries and polls (an attempt already running
        # when it was removed may still finish, and leaves nothing behind).
        await watcher.remove_root(str(b))
        assert watcher.root_status(str(b)) is None
        await asyncio.sleep(0.3)
        n, r = len(_full_for(calls, b)), limit["refusals"]
        await asyncio.sleep(1.2)
        assert len(_full_for(calls, b)) == n and limit["refusals"] == r
        assert watcher.root_status(str(b)) is None and str(b) not in watcher._state.watches
    finally:
        await watcher.stop()


async def test_a_missing_root_is_armed_when_it_appears(fast, monkeypatch, tmp_path):
    calls = fast
    a = (tmp_path / "Later").resolve()
    await watcher.start([str(a)])
    try:
        st = watcher.root_status(str(a))
        assert st["state"] == "missing" and not st["polling"]
        assert watcher.summary()["state"] == "ok"          # an unplugged drive isn't a fault
        a.mkdir()
        assert await _until(lambda: watcher.root_status(str(a))["state"] == "watching")
        assert await _until(lambda: _full_for(calls, a)), calls
    finally:
        await watcher.stop()


async def test_a_watch_whose_thread_stops_is_rearmed(fast, monkeypatch, tmp_path):
    calls = fast
    a = (tmp_path / "A").resolve()
    a.mkdir()
    await watcher.start([str(a)])
    try:
        obs = watcher._state.observer
        em = obs._emitter_for_watch[watcher._state.watches[str(a)]]
        em.stop()
        await asyncio.to_thread(em.join, 5)
        # Noticed, rescanned, re-armed on a fresh emitter.
        assert await _until(lambda: _full_for(calls, a)), calls
        assert await _until(lambda: watcher.root_status(str(a))["state"] == "watching"
                            and obs._emitter_for_watch.get(watcher._state.watches.get(str(a)))
                            is not em)
    finally:
        await watcher.stop()


async def test_the_supervisor_survives_an_error(fast, monkeypatch, tmp_path):
    a = (tmp_path / "A").resolve()
    a.mkdir()
    real = watcher._supervise_once
    n = {"calls": 0}

    async def _flaky():
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("boom")
        await real()
    monkeypatch.setattr(watcher, "_supervise_once", _flaky)
    await watcher.start([str(a)])
    try:
        assert await _until(lambda: n["calls"] >= 3)
        assert not watcher._state.supervisor.done()
    finally:
        await watcher.stop()
    assert watcher._state.supervisor is None


async def test_a_partly_watched_root_keeps_its_watch_and_is_completed(fast, monkeypatch, tmp_path):
    """A folder moved in that hit the watch limit leaves the root partly
    watched: it is rescanned on the poll cadence, and the retry adds the
    missing folders to the SAME watch (never drops it)."""
    calls = fast
    a = (tmp_path / "A").resolve()
    a.mkdir()
    await watcher.start([str(a)])
    try:
        watch = watcher._state.watches[str(a)]
        handler = watcher._state.handlers[str(a)]
        results = [False, True]
        monkeypatch.setattr(handler, "_cover_subtree",
                            lambda d, notify=True: results.pop(0) if results else True)
        watcher._note_partial(str(a), "inotify watch limit reached")
        st = watcher.root_status(str(a))
        assert st["state"] == "partial" and st["polling"]
        assert watcher.summary()["state"] == "degraded"
        assert await _until(lambda: _full_for(calls, a)), calls          # rescanned now
        assert await _until(lambda: watcher.root_status(str(a))["state"] == "watching")
        assert not results                                              # retried twice
        assert watcher._state.watches[str(a)] is watch                  # never dropped
    finally:
        await watcher.stop()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify only")
async def test_an_inotify_reader_that_stopped_is_rearmed(fast, monkeypatch, tmp_path):
    """The inotify reader thread ends on its own when the watched folder is
    unmounted, while the emitter thread keeps waiting: re-arm it."""
    calls = fast
    a = (tmp_path / "A").resolve()
    a.mkdir()
    await watcher.start([str(a)])
    try:
        obs = watcher._state.observer
        em = obs._emitter_for_watch[watcher._state.watches[str(a)]]
        buf = em._inotify
        buf.stop()
        await asyncio.to_thread(buf.join, 5)
        assert em.is_alive() and not buf.is_alive()
        assert await _until(lambda: _full_for(calls, a)), calls
        assert await _until(lambda: watcher.root_status(str(a))["state"] == "watching"
                            and obs._emitter_for_watch.get(
                                watcher._state.watches.get(str(a))) is not em)
        calls.clear()
        await asyncio.sleep(1.0)                 # a fresh FSEvents stream can miss the first ~0.5 s
        (a / "n.mod").write_bytes(b"x" * 64)
        assert await _until(lambda: _scoped_for(calls, a)), calls
    finally:
        await watcher.stop()


# ── real inotify, real limit (Linux, own user namespace) ────────────────────

def _inotify_watch_counts() -> dict[int, int]:
    """fd → number of inotify watches, for this process's inotify instances."""
    out: dict[int, int] = {}
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}") != "anon_inode:inotify":
                continue
            with open(f"/proc/self/fdinfo/{fd}") as fh:
                out[int(fd)] = sum(1 for line in fh if line.startswith("inotify wd:"))
        except OSError:
            continue
    return out


_LIMIT_FILE = "/proc/sys/user/max_inotify_watches"


def _can_set_limit() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open(_LIMIT_FILE, "r+") as fh:
            fh.write(fh.read().strip())
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _can_set_limit(),
                    reason="needs Linux and a writable per-user-namespace inotify limit "
                           "(run under `unshare -U -r`)")
async def test_real_inotify_limit_is_per_root_and_recovers(fast, monkeypatch, tmp_path):
    calls = fast
    with open(_LIMIT_FILE) as fh:
        saved = fh.read().strip()
    a = (tmp_path / "A").resolve()
    b = (tmp_path / "B").resolve()
    (a / "Album").mkdir(parents=True)                    # 2 watches
    for i in range(120):                                  # 121 watches
        (b / f"Artist{i:03d}").mkdir(parents=True)
    before = _inotify_watch_counts()
    try:
        with open(_LIMIT_FILE, "w") as fh:
            fh.write(str(sum(before.values()) + 60))
        await watcher.start([str(a), str(b)])
        assert watcher._state.observer.__class__.__name__ == "InotifyObserver"
        assert watcher.root_status(str(a))["state"] == "watching"
        st = watcher.root_status(str(b))
        assert st["state"] == "retrying" and "max_user_watches" in st["reason"], st
        # The half-built instance for B was closed: only A's (2 watches) is new.
        now = _inotify_watch_counts()
        new = {fd: n for fd, n in now.items() if fd not in before}
        assert sorted(new.values()) == [2], new
        # A delivers events while B is over the limit.
        await asyncio.sleep(1.0)                 # a fresh FSEvents stream can miss the first ~0.5 s
        (a / "Album" / "new.mod").write_bytes(b"x" * 64)
        assert await _until(lambda: _scoped_for(calls, a)), calls
        assert await _until(lambda: _full_for(calls, b)), calls      # fallback poll
        # Retries don't leak instances either.
        assert await _until(lambda: watcher.root_status(str(b))["attempts"] >= 3)
        assert sorted(n for fd, n in _inotify_watch_counts().items()
                      if fd not in before) == [2]
        # Lift the limit: B is watched again and delivers events.
        with open(_LIMIT_FILE, "w") as fh:
            fh.write("100000")
        assert await _until(lambda: watcher.root_status(str(b))["state"] == "watching")
        calls.clear()
        await asyncio.sleep(1.0)                 # a fresh FSEvents stream can miss the first ~0.5 s
        (b / "Artist007" / "late.mod").write_bytes(b"y" * 64)
        assert await _until(lambda: _scoped_for(calls, b)), calls
        assert watcher.summary()["state"] == "ok"
    finally:
        await watcher.stop()
        with open(_LIMIT_FILE, "w") as fh:
            fh.write(saved)
