# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The file checks a request makes on a music file (a stat, a realpath) run
off the event loop: on a network drive each is a round trip, and a drive that
stops answering must stall that request — never every request of the server.

Each case makes every stat under one folder take ``DELAY`` (``os.stat`` /
``os.lstat``, which ``pathlib`` and ``realpath`` call) and measures the
loop's longest pause while the request runs."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

DELAY = 0.4


@pytest.fixture
def slow(tmp_path, monkeypatch):
    root = tmp_path / "slowdrive"
    root.mkdir()
    prefix = str(root)
    real_stat, real_lstat = os.stat, os.lstat

    def slowed(fn):
        def wrapper(p, *a, **k):
            s = p if isinstance(p, (str, bytes)) else (os.fspath(p) if hasattr(p, "__fspath__") else "")
            if isinstance(s, bytes):
                s = s.decode(errors="replace")
            if s.startswith(prefix):
                time.sleep(DELAY)
            return fn(p, *a, **k)
        return wrapper
    monkeypatch.setattr(os, "stat", slowed(real_stat))
    monkeypatch.setattr(os, "lstat", slowed(real_lstat))
    return root


async def _run_measured(coro):
    """``(result, the loop's longest pause, elapsed)`` while ``coro`` runs."""
    gaps: list[float] = []
    done = False

    async def tick():
        last = time.perf_counter()
        while not done:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.02)
    t0 = time.perf_counter()
    try:
        res = await coro
    except Exception as exc:                 # (the HTTP answer: the test reads it)
        res = exc
    finally:
        elapsed = time.perf_counter() - t0
        done = True
        await ticker
    return res, max(gaps), elapsed


def _assert_loop_free(lag, elapsed):
    assert elapsed >= DELAY * 0.9, "the slow drive was never touched"
    assert lag < DELAY / 2, f"the loop stalled {lag * 1000:.0f} ms"


async def test_chapters_and_lyrics(slow, monkeypatch):
    from soniqboom.api import tracks as T
    f = slow / "song.mod"
    f.write_bytes(b"M.K." * 400)
    track = SimpleNamespace(path=str(f), format="ProTracker", artist="A", album_artist="",
                            title="T", album="", duration=60.0)

    async def _get(_tid):
        return track
    monkeypatch.setattr(T, "get_track", _get)
    monkeypatch.setattr(T, "_remember_lyrics_miss", lambda _tid: None)
    res, lag, el = await _run_measured(T.get_chapters("t1"))
    assert res == {"id": "t1", "chapters": []}
    _assert_loop_free(lag, el)
    res, lag, el = await _run_measured(T.resolve_lyrics("t1", writeback=False))
    assert res == {"lyrics": None, "synced": False, "source": None}
    _assert_loop_free(lag, el)
    # a file that is gone: the same answers as before
    track.path = str(slow / "gone.mod")
    res, lag, el = await _run_measured(T.get_chapters("t1"))
    assert res == {"id": "t1", "chapters": []}
    _assert_loop_free(lag, el)


async def test_sid_bytes_size_check(slow, monkeypatch):
    from soniqboom.api import tracks as T
    f = slow / "big.sid"
    f.write_bytes(b"PSID" + b"\0" * (1024 * 1024 + 10))
    track = SimpleNamespace(path=str(f), format="SID", id="s1")

    async def _get(_tid):
        return track
    monkeypatch.setattr(T, "get_track", _get)
    monkeypatch.setattr(T, "_is_sid_format", lambda _t: True)
    res, lag, el = await _run_measured(T.get_sid_bytes("s1", _user=None))
    assert getattr(res, "status_code", None) == 415
    _assert_loop_free(lag, el)


async def test_cover_art_of_a_file_on_the_drive(slow, monkeypatch):
    from soniqboom.api import art as A
    from soniqboom.core.store import TrackStore
    f = slow / "a.mp3"
    f.write_bytes(b"\0" * 64)
    store = TrackStore()

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "get_art", _none)
    monkeypatch.setattr(A, "get_track", lambda _tid: _ready(SimpleNamespace(path=str(f))))
    monkeypatch.setattr(A, "_source_mtime_for_async", _none)
    monkeypatch.setattr(A, "get_config", lambda *_a: _ready(False))
    monkeypatch.setattr(A, "get_store", lambda: store)
    monkeypatch.setattr(A, "_extract_cover", lambda p, raise_on_error=False: (None, None))
    marked = []
    monkeypatch.setattr(A, "_mark_art_absent_persisted", lambda tid: marked.append(tid))
    res, lag, el = await _run_measured(A._resolve_full_art("a1"))
    assert res == (None, None)
    assert marked == ["a1"]                  # present and read: the miss is remembered
    _assert_loop_free(lag, el)
    # gone: no miss remembered (an offline drive must not poison the art)
    marked.clear()
    monkeypatch.setattr(A, "get_track", lambda _tid: _ready(SimpleNamespace(path=str(slow / "x.mp3"))))
    res, lag, el = await _run_measured(A._resolve_full_art("a2"))
    assert res == (None, None) and marked == []
    _assert_loop_free(lag, el)


def _ready(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


async def test_folder_listing(slow, monkeypatch):
    from soniqboom.api import fstree as F
    d = slow / "album"
    d.mkdir()
    (d / "a.mod").write_bytes(b"x")

    async def _confine(path, hint_root=None):
        return Path(path), slow
    monkeypatch.setattr(F, "_confine_local_async", _confine)
    res, lag, el = await _run_measured(F.tracks_in_dir(path=str(d), recursive=False))
    assert not isinstance(res, Exception)
    _assert_loop_free(lag, el)
    res, lag, el = await _run_measured(F.tracks_in_dir(path=str(slow / "nope"), recursive=False))
    assert getattr(res, "status_code", None) == 404
    _assert_loop_free(lag, el)


async def test_scan_roots_are_resolved_off_the_loop(slow):
    from soniqboom.core import scanner
    deep = slow / "a" / "b"
    deep.mkdir(parents=True)
    res, lag, el = await _run_measured(scanner._resolve_all([str(deep)]))
    assert res == {str(deep): str(deep.resolve())}
    _assert_loop_free(lag, el)


async def test_midi_soundfont_lookup(slow, monkeypatch):
    from soniqboom import config
    sf = slow / "piano.sf2"
    sf.write_bytes(b"RIFF")
    monkeypatch.setattr(config.settings, "soundfont_path", str(sf))
    res, lag, el = await _run_measured(config.active_soundfont_async())
    assert res == sf
    _assert_loop_free(lag, el)


async def test_an_unscanned_folder_listing(slow, monkeypatch):
    """A folder of files the library doesn't hold yet: each listed file's
    stat (size, date) runs off the loop."""
    from soniqboom.api import fstree as F
    from soniqboom.core.store import TrackStore
    d = slow / "new"
    d.mkdir()
    for i in range(3):
        (d / f"{i}.mod").write_bytes(b"x" * (i + 1))
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)

    async def _confine(path, hint_root=None):
        return Path(path), slow
    monkeypatch.setattr(F, "_confine_local_async", _confine)
    F._TRACKS_META_CACHE.clear()
    res, lag, el = await _run_measured(F.tracks_with_meta(
        path=str(d), recursive=False, offset=0, limit=0, filter_duplicates=None,
        shuffle_seed=None, playable=False))
    assert not isinstance(res, Exception), res
    _assert_loop_free(lag, el)
