# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subsonic getStarred2 / getPlaylist with tens of thousands of songs, and
the song memo on a library bigger than 100k (v2.2.0 review, "also worth
fixing").

At 50,000 starred songs getStarred2 held the event loop for 0.9-1.6 s (JSON)
and getPlaylist of a 50,000-entry playlist for ~0.7-0.9 s: every song was
mapped in one go and the JSON body serialised on the loop.  Both are mapped
in time slices now (``subsonic._sliced_map``, GC paused within a slice) and
serialised off the loop.  The song memo was cleared whenever it reached
100,000 entries, so on a 506k-track library a re-sync never hit it; its cap
now scales with the library.

In-process only: bare Subsonic router, throw-away TrackStore, no server.
"""
from __future__ import annotations

import asyncio
import gc
import json
import time
import types
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import subsonic as S
from soniqboom.core import subsonic_state
from soniqboom.core.store import TrackStore

N = 40_000
USER = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)


def _rows(n: int) -> list[dict]:
    return [{"id": f"t{i:06d}", "title": f"Song {i}", "artist": f"Artist {i % 300}",
             "album_artist": f"Artist {i % 300}", "album": f"Album {i % 2000}",
             "genre": ["Chiptune"], "year": 1990, "added_at": 1_700_000_000 + i,
             "duration": 100.0, "format": "SID", "path": f"/music/d{i % 900}/t{i}.sid",
             "track_number": i % 20, "subsongs": 3 if i % 1000 == 0 else 1}
            for i in range(n)]


@pytest.fixture
def big(monkeypatch, tmp_path):
    st = TrackStore()
    st._aof = lambda *a, **k: None
    st.upsert_tracks_batch(_rows(N))
    st.set_config("subsonic_folder_albums", False)
    monkeypatch.setattr(S, "get_store", lambda: st)
    monkeypatch.setattr(S, "_require_user", lambda *a, **k: USER)
    monkeypatch.setattr(S, "_CACHE_DEBOUNCE_SEC", 0.0)
    subsonic_state.reset_state(tmp_path / "state.json")
    doc = subsonic_state.get_state()._ensure_loaded()
    now = time.time()
    # newest star first = t000000 … ; one star of a track that is gone
    stars = {f"t{i:06d}": now - i for i in range(N)}
    stars["gone"] = now + 5
    doc["users"]["u1"] = {"starred": {"song": stars}}
    entries = [f"t{i:06d}" for i in range(N)]
    entries[5] = {"id": "t001000", "subsong": 2}          # a tune entry
    st._playlists["pl1"] = {"id": "pl1", "name": "big", "track_ids": entries,
                            "owner_user_id": "u1", "created_at": now, "updated_at": now}
    app = FastAPI()
    app.include_router(S.router)
    S._song_memo_clear()
    # The server freezes its long-lived heap after loading the library
    # (store.freeze_long_lived_heap): a full collection then walks only what
    # a request allocates — measured as it runs.
    gc.collect()
    gc.freeze()
    try:
        yield st, app
    finally:
        gc.unfreeze()
        S._song_memo_clear()


async def _get_with_max_stall(app, url: str, **params):
    gaps = [0.0]
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps[0] = max(gaps[0], now - last)
            last = now

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t", timeout=300) as c:
        await c.get("/rest/ping.view")                 # (one-time imports out of the way)
        tk = asyncio.create_task(ticker())
        await asyncio.sleep(0.01)
        r = await c.get(url, params=params)
        stop.set()
        await tk
    assert r.status_code == 200
    return r, gaps[0]


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_get_starred2_of_many_songs_keeps_the_loop_responsive(big, fmt):
    _st, app = big
    r, stall = asyncio.run(_get_with_max_stall(app, "/rest/getStarred2.view", f=fmt))
    if fmt == "json":
        songs = json.loads(r.content)["subsonic-response"]["starred2"]["song"]
        ids = [s["id"] for s in songs]
    else:
        root = ET.fromstring(r.content)
        ids = [e.get("id") for e in root.iter() if e.tag.endswith("song")]
    assert ids == [f"t{i:06d}" for i in range(N)]          # newest first, "gone" skipped
    # v2.2.0 at 40,000 songs: 0.32-0.93 s in one block (JSON and XML); sliced: 54-90 ms
    assert stall < 0.2, f"event loop stalled {stall * 1000:.0f} ms"


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_get_playlist_of_many_entries_keeps_the_loop_responsive(big, fmt):
    _st, app = big
    r, stall = asyncio.run(_get_with_max_stall(app, "/rest/getPlaylist.view", id="pl1", f=fmt))
    if fmt == "json":
        pl = json.loads(r.content)["subsonic-response"]["playlist"]
        ids = [e["id"] for e in pl["entry"]]
        assert pl["songCount"] == N and pl["duration"] == 100 * (N - 1) + pl["entry"][5]["duration"]
    else:
        root = ET.fromstring(r.content)
        ids = [e.get("id") for e in root.iter() if e.tag.endswith("entry")]
    want = [f"t{i:06d}" for i in range(N)]
    want[5] = "t001000~2"
    assert ids == want
    assert stall < 0.2, f"event loop stalled {stall * 1000:.0f} ms"


def test_sliced_map_yields_between_slices_and_keeps_order(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def spy(d, *a):
        sleeps.append(d)
        await real_sleep(0)

    monkeypatch.setattr(S.asyncio, "sleep", spy)

    def slow(x):
        t = time.perf_counter()
        while time.perf_counter() - t < 0.0002:
            pass
        return None if x % 7 == 0 else x * 2

    got = asyncio.run(S._sliced_map(list(range(400)), slow))
    assert got == [x * 2 for x in range(400) if x % 7]
    assert len(sleeps) >= 5                              # ~80 ms of work, 6 ms slices


def test_song_memo_holds_the_whole_library(monkeypatch):
    """A cap below the library's size used to clear the memo every cap-many
    songs: a second listing of the whole library rebuilt every song."""
    st = TrackStore()
    st._aof = lambda *a, **k: None
    st.upsert_tracks_batch(_rows(600))
    monkeypatch.setattr(S, "_SONG_MEMO_MAX", 100)
    S._song_memo_clear()
    built = []
    real = S._song_static
    monkeypatch.setattr(S, "_song_static", lambda t, ctx: built.append(t["id"]) or real(t, ctx))
    for _ in range(2):
        ctx = S._song_ctx(st, USER)
        for tid in list(st._tracks):
            S._track_to_song(st.get_track(tid), ctx)
    assert len(built) == 600                     # the second pass: all memo hits
    assert len(S._SONG_META) == 600
    assert S._song_memo_cap(st) == 660           # the library + 10 %
    assert S._song_memo_cap(types.SimpleNamespace()) == 100     # (no count: the floor)
    S._song_memo_clear()
