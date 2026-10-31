# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A start builds the search box's own indexes (``folder:``, ``title:``,
``status:`` …) after the library is served
(``rebuild_indexes(defer_search=True)``): in slices on the loop, or at once
by whatever reads them first.  Every answer is the one a full build gives —
writes made while they build included."""
from __future__ import annotations

import asyncio
import random
import threading

import pytest

from soniqboom.core import store as S
from soniqboom.core.store import TrackStore

FORMATS = ["FLAC", "MP3", "SID", "ProTracker", "AHX", "WAV"]


def _row(i: int, rnd: random.Random) -> dict:
    fmt = rnd.choice(FORMATS)
    folder = rnd.choice(["/m/Rock/Album A", "/m/Chip/Hubbard", "/m/Démos/Revision 2024",
                         "smb://nas/music:/Mods/Party", "/m/Rock/Live at Wembley"])
    t = {"id": f"t{i:05d}", "path": f"{folder}/{i:03d} Song of {rnd.choice(['Life', 'Time', 'Space'])}.x",
         "title": rnd.choice(["Song of Life", "Space Song", "Live at Home", "Intro"]),
         "artist": rnd.choice(["Rob Hubbard", "Band", "Ünïcode Artist"]), "format": fmt,
         "duration": 60.0 + i % 300, "added_at": 1_700_000_000 + i,
         "bitrate": rnd.choice([0, 128_000, 320_000, 900_000]),
         "sample_rate": rnd.choice([None, 44100, 48000, 96000]),
         "bit_depth": rnd.choice([None, 16, 24]), "channels": rnd.choice([None, 1, 2, 4, 8])}
    if rnd.random() < 0.3:
        t["scene_party"] = rnd.choice(["Revision 2024", "Assembly"])
    if rnd.random() < 0.2:
        t["health"] = "broken"
    if rnd.random() < 0.3:
        t["composer"] = rnd.choice(["Rob Hubbard", "Martin Galway"])
    if fmt == "SID":
        t["sid_model"] = rnd.choice(["6581", "8580"])
        t["subsongs"] = rnd.choice([1, 3])
        if rnd.random() < 0.5:
            t["stil"] = "a note"
    if rnd.random() < 0.2:
        t["is_lossless"] = rnd.choice([True, False])
    if rnd.random() < 0.2:
        t["game"] = "Commando"
    return t


def _rows(n=600, seed=7):
    rnd = random.Random(seed)
    return [_row(i, rnd) for i in range(n)]


def _store(rows, *, defer):
    s = TrackStore()
    s._tracks = {r["id"]: dict(r) for r in rows}
    s.rebuild_indexes(defer_search=defer)
    return s


QUERIES = [
    {"party": "revision"}, {"composer_match": "hubbard"}, {"folder": "rock"},
    {"folder": "demos/revision"}, {"title_match": "song"}, {"path_match": ".x"},
    {"status": "broken"}, {"lossless": "yes"}, {"lossless": "no"},
    {"bitdepth_min": 24}, {"samplerate_min": 48000}, {"bitrate_max": 192},
    {"tunes_min": 2}, {"chip": "6581"}, {"channels_min": 4, "channels_max": 8},
    {"has": "stil"}, {"anyof": [[(("folder", "chip"),), (("party", "assembly"),)]]},
]


def _ids(s, q):
    return {t["id"] for t in s.filter_tracks(limit=100_000, **q)}


def _search_indexes(s):
    return {a: getattr(s, a) for a in S._SEARCH_INDEX_ATTRS}


def test_a_deferring_rebuild_leaves_them_to_build_and_the_first_search_finishes_them():
    rows = _rows()
    full = _store(rows, defer=False)
    lazy = _store(rows, defer=True)
    assert lazy._sx_todo is not None and not lazy._tag_party and not lazy._path_text
    # browsing / plain search needs none of them: nothing is built for it
    assert _ids(lazy, {"artist": "Band"}) == _ids(full, {"artist": "Band"})
    assert lazy._sx_todo is not None
    for q in QUERIES:
        assert _ids(lazy, q) == _ids(full, q), q
    assert lazy._sx_todo is None
    assert _search_indexes(lazy) == _search_indexes(full)


async def test_slices_with_writes_meanwhile_end_as_a_full_build():
    rows = _rows(900, seed=11)
    s = _store(rows, defer=True)
    rnd = random.Random(3)
    extra = _rows(60, seed=99)
    task = asyncio.ensure_future(s.build_search_indexes_async(chunk=37))
    n = 0
    while not task.done():
        # writes while the slices run: tracks not reached yet and reached
        tid = f"t{rnd.randrange(900):05d}"
        if s.get_track(tid) is not None:
            op = rnd.random()
            if op < 0.3:
                s.update_track_fields(tid, {"health": rnd.choice(["broken", None]),
                                            "title": "Space Song " + str(n)})
            elif op < 0.5:
                s.update_track_fields(tid, {"path": f"/m/Moved/{tid}.x", "composer": "New One"})
            elif op < 0.65:
                s.delete_track(tid)
            else:
                s.upsert_track({**s.get_track(tid), "scene_party": "Evoke", "channels": 8})
        if n < len(extra):
            r = dict(extra[n])
            r["id"] = "new" + r["id"]
            s.upsert_track(r)
        n += 1
        await asyncio.sleep(0)
    await task
    assert s._sx_todo is None and n > 10
    shadow = TrackStore()
    shadow._tracks = s._tracks
    shadow.rebuild_indexes()
    assert _search_indexes(s) == _search_indexes(shadow)
    assert s.verify_indexes()["index_ok"]


async def test_another_thread_waits_for_the_slices():
    rows = _rows(500, seed=5)
    full = _store(rows, defer=False)
    s = _store(rows, defer=True)
    got = {}

    def reader():
        got["ids"] = _ids(s, {"folder": "chip"})     # (not the loop's thread)
    th = threading.Thread(target=reader)
    th.start()
    await asyncio.sleep(0.05)
    assert th.is_alive()                              # waiting, not building
    assert s._sx_todo is not None
    await s.build_search_indexes_async(chunk=50)
    await asyncio.to_thread(th.join, 10)
    assert got["ids"] == _ids(full, {"folder": "chip"})


async def test_the_index_check_and_the_sweep_see_complete_indexes():
    rows = _rows(400, seed=2)
    s = _store(rows, defer=True)
    assert s.verify_indexes()["index_ok"]             # (finished first, then compared)
    assert s._sx_todo is None
    s2 = _store(rows, defer=True)
    await s2.search_indexes_ready(timeout=0.05)      # (no slices running: finishes them)
    assert s2._sx_todo is None


async def test_the_start_defers_them_and_builds_them_on_the_loop(monkeypatch):
    from soniqboom.core import persistence
    s = TrackStore()
    monkeypatch.setattr(persistence, "get_store", lambda: s, raising=False)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    persistence.populate_store({"tracks": {r["id"]: r for r in _rows(300, seed=4)}})
    assert s._sx_todo is not None and s._sx_task is not None
    await asyncio.wait_for(s._sx_task, 10)
    assert s._sx_todo is None and s._tag_folder


def test_a_tool_without_a_loop_gets_them_at_once(monkeypatch):
    from soniqboom.core import persistence
    s = TrackStore()
    monkeypatch.setattr(persistence, "get_store", lambda: s, raising=False)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    persistence.populate_store({"tracks": {r["id"]: r for r in _rows(100, seed=8)}})
    assert s._sx_todo is None and s._tag_folder


async def test_a_search_handler_waits_for_the_slices_without_freezing_the_loop(monkeypatch):
    """An operator search right after a start awaits the slices (the loop
    serves meanwhile) instead of finishing the whole build in one go."""
    import time
    from soniqboom.core import data
    s = _store(_rows(3000, seed=21), defer=True)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(data, "get_store", lambda: s, raising=False)
    assert s.needs_search_indexes({"folder": "rock"})
    assert not s.needs_search_indexes({"artist": "Band"})
    builder = asyncio.ensure_future(s.build_search_indexes_async(chunk=50))
    gaps, done = [], False

    async def tick():
        last = time.perf_counter()
        while not done:
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    t = asyncio.ensure_future(tick())
    await data.search_indexes_for({"folder": "rock"})
    done = True
    await t
    await builder
    assert s._sx_todo is None
    assert len(gaps) > 20                      # the loop ran while it waited
    assert not s.needs_search_indexes({"folder": "rock"})
