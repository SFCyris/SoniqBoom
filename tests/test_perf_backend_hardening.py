# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance guard rails for the 2.3.0 backend hardening (search parser,
sign-in lockout, Subsonic auth / search / memo): the paths a listener waits
on every time must stay as fast as before the hardening.

Each budget sits far above what 2.3.0 measures on the dev Mac (in the
comment beside it, with v2.2.0's figure) and far below what a user would
notice, so a load-induced slow run doesn't fail it but an accidental
quadratic / per-request scrypt / lock convoy does.  The heap is frozen after
the library is built, as the server does (``store.freeze_long_lived_heap``).

In-process only: throw-away stores, bare routers, no server.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import time
import types

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import search as S
from soniqboom.api import subsonic
from soniqboom.api import users as users_api
from soniqboom.core import subsonic_state
from soniqboom.core import users as users_mod
from soniqboom.core.store import TrackStore

TYPICAL = ["hubbard", "rob hubbard", "commando", 'artist:"rob hubbard" year:>1986',
           "format:sid commando", "format:flac,alac -live", "samplerate:44.1 khz bitrate:>256",
           '"song of myself"', "year:1990 - 1999 game:uridium", "nightwish -live -remix",
           "length:>5:00 added:<30d", "a-ha take on me"]


def _best_us(fn, reps: int) -> float:
    best = float("inf")
    for _ in range(5):
        t = time.perf_counter()
        for _ in range(reps):
            fn()
        best = min(best, (time.perf_counter() - t) / reps * 1e6)
    return best


@pytest.mark.parametrize("q", TYPICAL)
def test_typical_queries_parse_in_microseconds(q):
    # 2.3.0: 2.3-19.5 µs (v2.2.0: 2.3-20.8 µs) — budget 500 µs
    assert _best_us(lambda: S.search_predicates(q), 300) < 500


@pytest.fixture
def auth_store(monkeypatch, tmp_path):
    st = users_mod.UserStore(tmp_path)
    st.create("admin", "Correct-horse-9!", "admin")
    st.get_by_username("admin").subsonic_password = "app-secret"
    monkeypatch.setattr(users_mod, "_instance", st)
    monkeypatch.setattr(subsonic, "get_user_store", lambda: st)
    monkeypatch.setattr(users_api, "get_user_store", lambda: st)
    return st


def test_subsonic_per_request_auth_stays_o1(auth_store):
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.11"), headers={},
                                query_params={})
    salt = "abc"
    tok = hashlib.md5(("app-secret" + salt).encode()).hexdigest()
    assert subsonic._resolve_user(req, None, "admin", None, salt, tok) is not None
    assert subsonic._resolve_user(req, None, "admin", "app-secret", None, None) is not None
    # 2.3.0: 2.3 / 2.5 µs (v2.2.0: 1.3 / 1.0 µs) — budget 100 µs (no scrypt,
    # no users.json write, no lock convoy per request)
    assert _best_us(lambda: subsonic._resolve_user(req, None, "admin", None, salt, tok),
                    3000) < 100
    assert _best_us(lambda: subsonic._resolve_user(req, None, "admin", "app-secret",
                                                   None, None), 3000) < 100


def test_web_sign_in_costs_one_scrypt(auth_store):
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")

    async def run():
        tr = httpx.ASGITransport(app=app, client=("10.0.0.10", 40000))
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            ts = []
            for _ in range(3):
                t = time.perf_counter()
                r = await c.post("/api/auth/login",
                                 json={"username": "admin", "password": "Correct-horse-9!"})
                ts.append(time.perf_counter() - t)
                assert r.status_code == 200
                c.cookies.clear()
            return min(ts)
    # 2.3.0: 61-67 ms (v2.2.0: 69 ms), one scrypt — budget 1 s
    assert asyncio.run(run()) < 1.0


@pytest.fixture
def library(monkeypatch, tmp_path):
    n = 30_000
    st = TrackStore()
    st._aof = lambda *a, **k: None
    names = ["Rob Hubbard", "Martin Galway", "Ben Daglish", "Chris Huelsbeck", "Jeroen Tel"]
    words = ["commando", "monty", "run", "delta", "wizball", "arkanoid", "live", "remix",
             "theme", "intro", "song", "of", "myself", "night"]
    st.upsert_tracks_batch([{
        "id": f"t{i:06d}", "title": f"{words[i % 14]} {words[(i // 14) % 14]} {i}",
        "artist": names[i % 5], "album_artist": names[i % 5], "album": f"Album {i % 900}",
        "genre": ["Chiptune"], "year": 1980 + i % 40, "added_at": 1_700_000_000 + i,
        "duration": 120.0, "format": ("SID", "MP3", "FLAC")[i % 3],
        "path": f"/music/d{i % 700}/t{i}.x", "track_number": i % 20} for i in range(n)])
    st.set_config("subsonic_folder_albums", False)
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: st)
    monkeypatch.setattr(subsonic, "get_store", lambda: st)
    user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: user)
    subsonic_state.reset_state(tmp_path / "state.json")
    app = FastAPI()
    app.include_router(S.router, prefix="/api")
    app.include_router(subsonic.router)
    app.dependency_overrides[users_api.current_user] = lambda: None
    app.dependency_overrides[users_api.optional_listener_uid] = lambda: None
    gc.collect()
    gc.freeze()
    try:
        yield app, st, user
    finally:
        gc.unfreeze()


def test_search_and_search3_end_to_end(library):
    app, _st, _user = library

    async def run():
        out = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", timeout=120) as c:
            await c.get("/api/search", params={"q": "warmup"})
            await c.get("/rest/search3.view", params={"query": "warmup", "f": "json"})
            for q in ("hubbard", "rob hubbard", "commando monty",
                      'artist:"rob hubbard" year:>1986', "night -live"):
                for name, url, p in (("web", "/api/search", {"q": q}),
                                     ("search3", "/rest/search3.view",
                                      {"query": q, "f": "json"})):
                    best = float("inf")
                    for _ in range(5):
                        t = time.perf_counter()
                        r = await c.get(url, params=p)
                        best = min(best, time.perf_counter() - t)
                        assert r.status_code == 200
                    out[(name, q)] = best
        return out
    # 2.3.0 at 120k tracks: 0.3-15 ms per query (v2.2.0: the same, within
    # noise) — budget 250 ms at 30k
    worst = max(asyncio.run(run()).items(), key=lambda kv: kv[1])
    assert worst[1] < 0.25, worst


def test_the_song_memo_is_hit_on_a_second_listing(library):
    _app, st, user = library
    subsonic._song_memo_clear()
    ids = list(st._tracks)

    def listing():
        ctx = subsonic._song_ctx(st, user)
        t = time.perf_counter()
        for tid in ids:
            subsonic._track_to_song(st.get_track(tid), ctx)
        return (time.perf_counter() - t) / len(ids) * 1e6
    first, second = listing(), listing()
    subsonic._song_memo_clear()
    # 2.3.0 at 130k tracks: 12 → 1.5 µs a song (v2.2.0: 13 → 13, the memo
    # cleared at 100k) — budget: a warm listing at least twice as fast, < 50 µs
    assert second < 50 and second * 2 < first, (first, second)
