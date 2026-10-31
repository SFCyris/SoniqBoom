# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The search box's ``track:`` / ``disc:`` operators (``disk:`` = ``disc:``).

* Parse: every form ``year:`` takes (``N``, ``A-B``, ``A-``, ``-B``, ``>N``,
  ``<N``, ``>=N``, ``<=N``, spaces around the operator or the dash) becomes
  ``track_min`` / ``track_max`` (``disc_min`` / ``disc_max``) through the one
  parse path, combined with other operators and free text; ``track:3`` alone
  is no longer free text ("track 3").
* Store: the maintained number → ids indexes (``_tag_track_number`` /
  ``_tag_disc_number``) equal a brute force through inserts, field updates and
  deletes; a track without a number never matches; the index audit agrees.
* Every surface agrees: ``filter_tracks`` pages, ``filter_track_ids``,
  ``result_ids``, ``/api/search/page`` totals, the search shuffle and Save
  search (``run_search``).
"""
from __future__ import annotations

import copy
import random

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.api.search import _parse_advanced_query, run_search, search_predicates
from soniqboom.core import availability, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore, _ordinal


# ── parse ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,want", [
    ("track:3", {"track_min": 3, "track_max": 3}),
    ("track:1-5", {"track_min": 1, "track_max": 5}),
    ("track:5-", {"track_min": 5}),
    ("track:-3", {"track_max": 3}),
    ("track:>10", {"track_min": 11}),
    ("track:<10", {"track_max": 9}),
    ("track:>=10", {"track_min": 10}),
    ("track:<=4", {"track_max": 4}),
    ("track: 3", {"track_min": 3, "track_max": 3}),
    ("track:> 10", {"track_min": 11}),
    ("track: <= 4", {"track_max": 4}),
    ("track:1 - 5", {"track_min": 1, "track_max": 5}),
    ("track:1 -5", {"track_min": 1, "track_max": 5}),
    ("disc:2", {"disc_min": 2, "disc_max": 2}),
    ("disk:2", {"disc_min": 2, "disc_max": 2}),
    ("disc:>=2", {"disc_min": 2}),
    ("disc: 1 - 2", {"disc_min": 1, "disc_max": 2}),
    ("disc:2 track:3", {"disc_min": 2, "disc_max": 2, "track_min": 3, "track_max": 3}),
    ("artist:Nightwish track:12",
     {"artist_match": "Nightwish", "track_min": 12, "track_max": 12}),
    ("track:3 love song", {"track_min": 3, "track_max": 3, "query": "love song"}),
    ("year: 1990 - 2000 track:2", {"year_min": 1990, "year_max": 2000,
                                   "track_min": 2, "track_max": 2}),
    ("format:SID disc:1 track:<3", {"format_": "SID", "disc_min": 1, "disc_max": 1,
                                    "track_max": 2}),
])
def test_parse_forms(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q,want", [
    ("track:3/12", {"track_min": 3, "track_max": 3}),
    ("track: -3", {"track_max": 3}),
    ("track:- 3", {"track_max": 3}),
    ("year: -1990", {"year_max": 1990}),
    # no number or range: free text, as before the operators — never "every track"
    ("track:abc", {"query": "track:abc"}),
    ("track:-", {"query": "track:-"}),
    ("disk:one", {"query": "disk:one"}),
    ("track:3.5", {"query": "track:3.5"}),
    ("back on track:remix", {"query": "back on track:remix"}),
    ("artist:Ab track:remix", {"artist_match": "Ab", "query": "track:remix"}),
    # quoted values of other fields are left as typed
    ('album:"back on track: 2"', {"album_match": "back on track: 2"}),
    ('genre:"disc: 1 mix" track: 2', {"genre_match": "disc: 1 mix", "track_min": 2, "track_max": 2}),
])
def test_edge_forms(q, want):
    assert search_predicates(q) == want


def test_no_number_value_never_lists_the_whole_library(small):
    for q, want in (("track:abc", set()), ("disk:one", set()),
                    ("track:-", {"t12"})):              # (the text "track": "Track03")
        assert _ids(small.filter_tracks(**search_predicates(q), limit=100)) == want, q


def test_track_alone_is_an_operator_not_free_text():
    assert _parse_advanced_query("track:3") == "@track:[3 3]"
    assert "query" not in search_predicates("track:3")
    # a word that merely ends in "track" is no operator
    assert search_predicates("soundtrack 3") == {"query": "soundtrack 3"}


def test_ordinal_values():
    assert [_ordinal(v) for v in (3, 3.0, "3", " 3/12 ", "03", 0, -1, None, "", "x",
                                  "3a", 2.5, True, "٣", "0", "0/12", 0.0)] == \
        [3, 3, 3, 3, 3, None, None, None, None, None, None, None, None, None,
         None, None, None]


# ── store ────────────────────────────────────────────────────────────────────

def _t(i, track=None, disc=None, **kw):
    return {"id": f"t{i}", "path": f"/m/{i}.mp3", "title": kw.pop("title", f"Song {i}"),
            "artist": kw.pop("artist", "Band"), "album": "A", "format": "MP3",
            "added_at": i, "track_number": track, "disc_number": disc, **kw}


def _ids(rows):
    return {r["id"] for r in rows}


@pytest.fixture
def small():
    s = TrackStore()
    rows = [
        _t(1, 1, 1), _t(2, 2, 1), _t(3, 3, 1), _t(4, 3, 2), _t(5, 12, 2),
        _t(6, None, None), _t(7, 0, 0), _t(8, "3", None), _t(9, "3/12", 2),
        _t(10, 3.0, 1, artist="Nightwish"), _t(11, 12, 1, artist="Nightwish"),
        _t(12, None, 2, title="Track03"), _t(13, 5, None, title="love song"),
    ]
    for r in rows:
        s.upsert_track(r)
    return s


@pytest.mark.parametrize("kw,want", [
    ({"track_min": 3, "track_max": 3}, {"t3", "t4", "t8", "t9", "t10"}),
    ({"track_min": 1, "track_max": 3}, {"t1", "t2", "t3", "t4", "t8", "t9", "t10"}),
    ({"track_min": 5}, {"t5", "t11", "t13"}),
    ({"track_max": 2}, {"t1", "t2"}),
    ({"disc_min": 2, "disc_max": 2}, {"t4", "t5", "t9", "t12"}),
    ({"disc_min": 2, "disc_max": 2, "track_min": 3, "track_max": 3}, {"t4", "t9"}),
    ({"artist_match": "Nightwish", "track_min": 12, "track_max": 12}, {"t11"}),
    ({"track_min": 5, "query": "love"}, {"t13"}),
    ({"track_min": 1}, {"t1", "t2", "t3", "t4", "t5", "t8", "t9", "t10", "t11", "t13"}),
    ({"track_min": 0, "track_max": 0}, set()),      # 0 is no number
    ({"track_min": 99}, set()),
])
def test_store_filtering(small, kw, want):
    assert _ids(small.filter_tracks(**kw, limit=100)) == want
    assert set(small.filter_track_ids(**kw)) == want
    assert small.result_ids(**kw) == want


def test_query_text_reaches_the_store(small):
    for q, want in (("track:3", {"t3", "t4", "t8", "t9", "t10"}),
                    ("disc:2 track:3", {"t4", "t9"}),
                    ("disk:2 track:3", {"t4", "t9"}),
                    ("artist:Nightwish track:12", {"t11"}),
                    ("track:>4 love", {"t13"}),
                    ("track:<=2", {"t1", "t2"})):
        assert _ids(small.filter_tracks(**search_predicates(q), limit=100)) == want, q
    # Track03 has no track number: it never matches track:3 any more
    assert "t12" not in _ids(small.filter_tracks(**search_predicates("track:3"), limit=100))


def test_exact_number_is_the_live_set_but_result_ids_owns_its_copy(small):
    live = small._tag_track_number[3]
    assert small._candidate_ids(track_min=3, track_max=3) is live
    got = small.result_ids(track_min=3, track_max=3)
    assert got == live and got is not live


def _brute(s: TrackStore, field: str) -> dict:
    out: dict = {}
    for tid, t in s._tracks.items():
        n = _ordinal(t.get(field))
        if n is not None:
            out.setdefault(n, set()).add(tid)
    return out


def test_indexes_follow_inserts_updates_and_deletes():
    rnd = random.Random(7)
    s = TrackStore()
    vals = [None, 0, 1, 2, 3, "3", "4/9", 5.0, 12, "x"]
    for i in range(300):
        s.upsert_track(_t(i, rnd.choice(vals), rnd.choice(vals)))
    for step in range(600):
        tid = f"t{rnd.randrange(320)}"
        op = rnd.random()
        if op < 0.5 and tid in s._tracks:
            b = s._browse_seq
            patch = {rnd.choice(["track_number", "disc_number"]): rnd.choice(vals)}
            field, val = next(iter(patch.items()))
            changed = s._tracks[tid].get(field) != val
            s.update_track_fields(tid, patch)
            if changed:
                assert s._browse_seq != b          # a list can show it now
        elif op < 0.65 and tid in s._tracks:
            s.delete_track(tid)
        elif op < 0.8:
            s.update_track_fields_batch([(tid, {"track_number": rnd.choice(vals)})])
        else:
            s.upsert_track(_t(int(tid[1:]), rnd.choice(vals), rnd.choice(vals)))
        if step % 50 == 0 or step == 599:
            assert s._tag_track_number == _brute(s, "track_number"), step
            assert s._tag_disc_number == _brute(s, "disc_number"), step
    rep = s.verify_indexes()
    assert rep["index_ok"], rep["mismatches"]
    assert {"_tag_track_number", "_tag_disc_number"} <= rep["indexes"].keys()
    # a scan batch keeps them too
    s.enter_batch_mode()
    try:
        for i in range(400, 450):
            s.upsert_track(_t(i, rnd.choice(vals), rnd.choice(vals)))
    finally:
        s.exit_batch_mode()
    assert s._tag_track_number == _brute(s, "track_number")
    assert s.verify_indexes()["index_ok"]


def test_drift_is_reported_by_the_audit():
    s = TrackStore()
    s.upsert_track(_t(1, 3, 1))
    s._tag_track_number[3].add("ghost")
    rep = s.verify_indexes()
    assert any(m["index"] == "_tag_track_number" for m in rep["mismatches"])


def test_walk_cache_keys_on_the_numbers():
    """Large candidate sets walk the sorted index and keep the walk per
    predicates: track:3 then track:4 must not share one."""
    s = TrackStore()
    for i in range(6000):
        s.upsert_track(_t(i, 3 + i % 2, 1 + i % 3))
    a = s.filter_tracks(track_min=3, track_max=3, limit=50, offset=0)
    b = s.filter_tracks(track_min=4, track_max=4, limit=50, offset=0)
    a2 = s.filter_tracks(track_min=3, track_max=3, limit=50, offset=50)
    assert all(r["track_number"] == 3 for r in a + a2)
    assert all(r["track_number"] == 4 for r in b)
    c = s.filter_tracks(track_min=3, track_max=3, disc_min=2, disc_max=2, limit=50, offset=50)
    assert c and all(r["track_number"] == 3 and r["disc_number"] == 2 for r in c)


# ── every surface agrees (HTTP) ──────────────────────────────────────────────

def _app() -> FastAPI:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library):
        app.include_router(mod.router, prefix="/api")
    return app


@pytest.fixture
def lib(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    shuffle_order.clear()
    rnd = random.Random(20261023)
    for i in range(900):
        s.upsert_track(copy.deepcopy(_t(
            i, rnd.choice([None, 1, 2, 3, 3, "3", 4, 12, 0]), rnd.choice([None, 1, 2]),
            title=rnd.choice(["Track03", "love song", "intro", "Love"]),
            artist=rnd.choice(["Nightwish", "Other"]))))
    yield s
    availability.reset_for_tests()


def _brute_q(s: TrackStore, tmin=None, tmax=None, dmin=None, dmax=None, artist=None) -> set:
    out = set()
    for tid, t in s._tracks.items():
        tn, dn = _ordinal(t.get("track_number")), _ordinal(t.get("disc_number"))
        if tmin is not None or tmax is not None:
            if tn is None or (tmin is not None and tn < tmin) or (tmax is not None and tn > tmax):
                continue
        if dmin is not None or dmax is not None:
            if dn is None or (dmin is not None and dn < dmin) or (dmax is not None and dn > dmax):
                continue
        if artist and t.get("artist") != artist:
            continue
        out.add(tid)
    return out


@pytest.mark.parametrize("q,bf", [
    ("track:3", dict(tmin=3, tmax=3)),
    ("track:2-4", dict(tmin=2, tmax=4)),
    ("track:>3", dict(tmin=4)),
    ("track:-2", dict(tmax=2)),
    ("disc:2 track:3", dict(tmin=3, tmax=3, dmin=2, dmax=2)),
    ("artist:Nightwish track:12", dict(tmin=12, tmax=12, artist="Nightwish")),
])
async def test_search_page_shuffle_and_saved_search_agree(lib, q, bf):
    want = _brute_q(lib, **bf)
    assert want                                       # (the library has some)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/page", params={"q": q, "limit": 5000})).json()
        assert r["total"] == len(want)
        assert {t["id"] for t in r["tracks"]} == want
        sh = (await c.get("/api/tracks/shuffled",
                          params={"q": q, "seed": 42, "limit": 500})).json()
        assert sh["total"] == len(want)
        got = {t["id"] for t in sh["tracks"]}
        off = 500
        while off < sh["total"]:
            got |= {t["id"] for t in (await c.get(
                "/api/tracks/shuffled",
                params={"q": q, "seed": 42, "limit": 500, "offset": off})).json()["tracks"]}
            off += 500
        assert got == want
        prev = (await c.get("/api/search", params={"q": q, "limit": 200})).json()
        assert {t["id"] for t in prev} <= want
        assert len(prev) == min(200, len(want))
    assert set(lib.filter_track_ids(**search_predicates(q))) == want
    assert {t.id for t in await run_search(q, limit=5000)} == want


async def test_structured_filter_and_shuffle_params(lib):
    want = _brute_q(lib, tmin=3, tmax=3, dmin=2, dmax=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/filter", params={
            "track_min": 3, "track_max": 3, "disc_min": 2, "disc_max": 2,
            "limit": 2000})).json()
        assert {t["id"] for t in r} == want
        sh = (await c.get("/api/tracks/shuffled", params={
            "seed": 1, "limit": 500, "track_min": 3, "track_max": 3,
            "disc_min": 2, "disc_max": 2})).json()
        assert sh["total"] == len(want)
        assert {t["id"] for t in sh["tracks"]} == want
