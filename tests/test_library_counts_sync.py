# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The sidebar counts, the library generation, the static-file cache policy and
the Radio Browser name-search cache.

  * ``GET /api/library/counts`` gives, in one small ETag'd reply, the numbers
    the page used to derive from five whole lists: the same numbers (rows minus
    the "[No Artist]"-style bucket), in every mode, and a 304 while nothing
    changed;
  * ``library_generation()`` moves on a track write, an aggregate-cache
    invalidation and a restart, and stays put otherwise; the WebSocket's
    connect snapshot is marked (``snapshot``) and carries it, and so does every
    ``scan_progress`` frame that ends a scan;
  * the frontend's files and HTML shells are ``no-cache`` (revalidated → 304),
    everything else keeps its old policy;
  * ``radiodir.search_stations`` asks the directory once per name while its
    answer is fresh.
"""
from __future__ import annotations

import copy
from collections import OrderedDict

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import library
from soniqboom.api import tracks as tracks_api
from soniqboom.core import radiodir
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _row(i: int, **kw) -> dict:
    row = {"id": f"t{i}", "path": f"/m/a{i % 7}/t{i}.mp3", "title": f"Song {i}",
           "artist": f"Artist {i % 7}", "album_artist": f"AA {i % 3}", "album": f"Album {i % 5}",
           "genre": [f"Genre {i % 4}"], "year": 1980 + i % 6, "format": "MP3", "duration": 60.0,
           "added_at": 100 + i}
    row.update(kw)
    return row


@pytest.fixture
def lib(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    rows = [_row(i) for i in range(40)]
    rows += [_row(100, artist="", album_artist=""),            # "[No Artist]" / "[No Album Artist]"
             _row(101, artist="", album_artist="", scene_group="Fairlight")]
    rows += [_row(200, title="Dup", duplicate_group_id="g1", is_duplicate_primary=True, artist="Solo"),
             _row(201, title="Dup", duplicate_group_id="g1", is_duplicate_primary=False,
                  artist="Only In Copy", album="Copy Album")]
    for r in rows:
        s.upsert_track(copy.deepcopy(r))
    return s


def _app() -> FastAPI:
    app = FastAPI()
    for mod in (tracks_api, library):
        app.include_router(mod.router, prefix="/api")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t")


_LISTS = {"artists": "/api/library/artists", "album_artists": "/api/library/album-artists",
          "albums": "/api/library/albums", "genres": "/api/library/genres",
          "years": "/api/library/years"}


async def _expected(c) -> dict:
    """What the page derived before: the lists, minus their bucket rows."""
    out = {}
    for k, url in _LISTS.items():
        rows = (await c.get(url)).json()
        out[k] = sum(1 for r in rows if not r.get("label"))
    out["scene_groups"] = len((await c.get("/api/library/scene-groups")).json())
    cnt = (await c.get("/api/tracks/count")).json()
    out["tracks"], out["visible"] = cnt["count"], cnt["visible"]
    return out


@pytest.mark.parametrize("dedup", [False, True])
async def test_counts_equal_what_the_lists_gave(lib, dedup):
    lib.set_config("filter_duplicates", dedup)
    async with _client() as c:
        counts = (await c.get("/api/library/counts")).json()
        want = await _expected(c)
    assert {k: counts[k] for k in want} == want
    # the fixture really has bucket rows and a dedup difference to get right
    async with _client() as c:
        arts = (await c.get("/api/library/artists")).json()
    assert any(r.get("label") for r in arts)
    if dedup:
        assert counts["visible"] < counts["tracks"]


async def test_counts_follow_a_write_and_answer_304_meanwhile(lib):
    async with _client() as c:
        r1 = await c.get("/api/library/counts")
        etag = r1.headers["etag"]
        assert r1.headers["cache-control"] == "private, max-age=0, must-revalidate"
        r2 = await c.get("/api/library/counts", headers={"If-None-Match": etag})
        assert r2.status_code == 304 and r2.content == b""
        lib.upsert_track(_row(500, artist="Brand New Artist"))
        r3 = await c.get("/api/library/counts", headers={"If-None-Match": etag})
        assert r3.status_code == 200 and r3.headers["etag"] != etag
        assert r3.json()["artists"] == r1.json()["artists"] + 1
        assert r3.json()["tracks"] == r1.json()["tracks"] + 1
        want = await _expected(c)
    assert {k: r3.json()[k] for k in want} == want


def test_library_generation_moves_only_on_change(lib, monkeypatch):
    monkeypatch.setattr(library, "_AGG_EPOCH", 0)
    g0 = library.library_generation()
    assert library.library_generation() == g0                 # nothing happened
    lib.record_play("t1", uid="")
    lib.set_rating("t2", 5)
    assert library.library_generation() == g0                 # plays / ratings change no list
    lib.upsert_track(_row(600))
    g1 = library.library_generation()
    assert g1 != g0                                            # a track write
    library.invalidate_agg_cache()
    g2 = library.library_generation()
    assert g2 not in (g0, g1)                                  # a scan / reindex / dedup toggle
    monkeypatch.setattr(library, "_BOOT_ID", "another-process")
    assert library.library_generation() != g2                  # a restart


@pytest.mark.parametrize("updates", [
    {"health": "broken"}, {"defect": "partial"}, {"subsongs": 7},
    {"track_number": 9}, {"disc_number": 2}, {"channels": 6},
])
def test_library_generation_moves_for_fields_the_rows_show(lib, updates):
    """Fields the track rows render but the browse sequence leaves out (a
    badge, the Track column, the surround badge) still move the generation, so
    a page that reconnects re-reads its rows."""
    g0 = library.library_generation()
    assert lib.update_track_fields("t3", dict(updates))
    assert library.library_generation() != g0


def test_scan_frames_that_end_a_scan_carry_the_generation():
    gen = library.library_generation()
    done = library._with_lib_gen({"event": "scan_progress", "running": False, "processed": 3})
    assert done["lib_gen"] == gen
    silent = library._with_lib_gen({"event": "scan_progress", "running": False, "silent": True})
    assert silent["lib_gen"] == gen
    running = {"event": "scan_progress", "running": True}
    assert "lib_gen" not in library._with_lib_gen(running)
    other = {"event": "art_ready", "track_id": "x"}
    assert library._with_lib_gen(other) is other
    keep = {"event": "scan_progress", "running": False, "lib_gen": "given"}
    assert library._with_lib_gen(keep)["lib_gen"] == "given"


def test_the_connect_snapshot_is_marked_and_carries_the_generation(lib, monkeypatch):
    from soniqboom.core import users as core_users

    def no_store():
        raise RuntimeError("UserStore not initialised")
    monkeypatch.setattr(core_users, "get_user_store", no_store)
    with TestClient(_app()) as c, c.websocket_connect("/api/library/ws") as ws:
        first = ws.receive_json()
    assert first["event"] == "scan_progress"
    assert first["snapshot"] is True and first["running"] is False
    assert first["lib_gen"] == library.library_generation()


# ── static files: revalidated, not re-downloaded ─────────────────────────────

@pytest.fixture(scope="module")
def site():
    from soniqboom import main
    return TestClient(main.app)          # (no lifespan: only the routes + middleware)


@pytest.mark.parametrize("path", ["/assets/js/app.js?v=192", "/assets/js/library.js",
                                  "/assets/css/app.css", "/", "/m", "/m/queue"])
def test_frontend_files_revalidate_with_a_304(site, path):
    r = site.get(path, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    tag = r.headers["etag"]
    again = site.get(path, headers={"User-Agent": "Mozilla/5.0 (Macintosh)", "If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["cache-control"] == "no-cache"


def test_shells_and_service_worker(site):
    assert site.get("/m").headers["cache-control"] == "no-cache"
    assert site.get("/sw.js").headers["cache-control"] == "no-cache"    # the route's own
    # an API answer keeps no-store
    assert site.get("/api/health").headers["cache-control"].startswith("no-store")


def test_revalidated_paths_are_exactly_the_frontend(site):
    from soniqboom import main
    yes = ["/", "/m", "/m/queue", "/multiroom", "/multiroom/x", "/assets/js/a.js", "/manual", "/manual/x.html"]
    no = ["/api/x", "/rest/ping", "/dlna/desc.xml", "/cast/tok/a.mp3", "/mx", "/assetsx", "/manualx",
          "/multiroomx", "/sw.js"]
    assert all(main._revalidated_static(p) for p in yes)
    assert not any(main._revalidated_static(p) for p in no)


# ── Radio Browser name search: one directory request per fresh name ───────────

@pytest.fixture
def rb(monkeypatch):
    calls: list[dict] = []

    async def fake_rb_get(path, params=None):
        calls.append(dict(params or {}))
        if params and params.get("name") == "boom":
            raise RuntimeError("mirror down")
        return [{"stationuuid": f"00000000-0000-0000-0000-00000000000{len(calls) % 10}",
                 "name": f"{params['name']} Radio", "url_resolved": "http://198.51.100.7/s",
                 "votes": 5, "clickcount": 1, "tags": "chiptune", "countrycode": "DE"}]
    monkeypatch.setattr(radiodir, "_rb_get", fake_rb_get)
    monkeypatch.setattr(radiodir, "all_favorites", lambda: [])
    monkeypatch.setattr(radiodir, "_search_cache", OrderedDict())
    return calls


async def test_station_search_cold_then_warm_with_different_names(rb):
    a1 = await radiodir.search_stations("zzchip", 4)
    assert len(rb) == 1 and a1
    a2 = await radiodir.search_stations("zzchip", 4)
    assert len(rb) == 1 and a2 == a1                      # warm: no directory request
    b1 = await radiodir.search_stations("zzamiga", 4)
    assert len(rb) == 2 and b1 != a1                      # a different name is cold
    await radiodir.search_stations("zzchip", 100)       # a different size is its own answer
    assert len(rb) == 3
    assert a1[0]["sid"].startswith("rb:")                 # a directory row
    a1[0]["name"] = "changed by a caller"                 # answers are copies
    assert (await radiodir.search_stations("zzchip", 4))[0]["name"] != "changed by a caller"


async def test_station_search_expires_and_does_not_keep_failures(rb, monkeypatch):
    await radiodir.search_stations("chiptune", 4)
    key = next(iter(radiodir._search_cache))
    at, rows = radiodir._search_cache[key]
    radiodir._search_cache[key] = (at - radiodir._SEARCH_CACHE_TTL - 1, rows)
    await radiodir.search_stations("chiptune", 4)
    assert len(rb) == 2                                   # stale → asked again
    assert await radiodir.search_stations("boom", 4) == []
    assert await radiodir.search_stations("boom", 4) == []
    assert len(rb) == 4                                   # a failure is asked again
    monkeypatch.setattr(radiodir, "_SEARCH_CACHE_MAX", 2)
    for q in ("x1", "x2", "x3"):
        await radiodir.search_stations(q, 4)
    assert len(radiodir._search_cache) == 2


def test_a_share_checks_new_tracks_event_carries_the_generation():
    """``remote_new_tracks`` follows its share check's end frame: with the
    same ``lib_gen`` a page that re-read at that frame doesn't re-read the
    counts again (app.js ``_scanEndGen``)."""
    gen = library.library_generation()
    ev = library._with_lib_gen({"event": "remote_new_tracks", "scan_root": "ftp://h/x", "count": 2})
    assert ev["lib_gen"] == gen
    end = library._with_lib_gen({"event": "scan_progress", "running": False, "silent": True})
    assert end["lib_gen"] == ev["lib_gen"]
