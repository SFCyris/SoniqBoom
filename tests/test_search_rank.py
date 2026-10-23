# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Best match (core/search_rank.py): the default order of a search's result
set (``GET /api/search/page``).

Hand-built libraries where the expected order is written out: the exact
title first, title > artist / album artist > album > the other fields, a
whole word over a prefix, every term in one field over terms spread, the
phrase; ties newest first, then by id.  Field operators still filter and a
search without free text lists newest first.  The order is computed once per
set — in slices, the event loop turning between them — is the same on every
page and for every page size, and is renewed with the set after a write.
(The randomized differential against a brute force of the rule is in
tests/test_search_sets.py.)
"""
from __future__ import annotations

import asyncio
import random

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.core import availability, search_rank, search_sets, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library):
        app.include_router(mod.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    shuffle_order.clear()
    root = tmp_path / "A"
    root.mkdir()
    s.upsert_scan_dir(str(root))
    s.test_root = str(root)
    yield s
    availability.reset_for_tests()


def _add(s: TrackStore, tid: str, added: float, **fields) -> None:
    t = {"id": tid, "path": f"{s.test_root}/{tid}.mod", "title": "", "artist": "", "album": "",
         "album_artist": "", "format": "MOD", "added_at": added}
    t.update(fields)
    s.upsert_track(t)


async def _order(c, q: str, **kw) -> list[str]:
    j = (await c.get("/api/search/page", params={"q": q, "limit": 5000, **kw})).json()
    assert j["total"] == len(j["tracks"])
    return [t["id"] for t in j["tracks"]]


# The newest track is the one that should come LAST by relevance, so a list
# in newest-first order would fail every expectation below.

async def test_one_word_exact_title_then_field_weights_then_prefix(store):
    s = store
    _add(s, "f-other", 9, title="Song", composer="Ghost")              # other field
    _add(s, "e-album", 8, title="Song", album="Ghost Stories")          # album
    _add(s, "d2-aartist", 7, title="Song", album_artist="Ghost")        # album artist (= artist)
    _add(s, "d-artist", 6, title="Song", artist="Ghost")                # artist
    _add(s, "c-prefix", 5, title="Ghosts of Mars")                      # title, prefix only
    _add(s, "b-word", 4, title="The Ghost Town")                        # title, whole word
    _add(s, "a-exact", 3, title="Ghost")                                # the exact title
    _add(s, "z-none", 10, title="Nothing")                              # not a match
    async with _client() as c:
        got = await _order(c, "ghost")
        # a whole word in any field ranks above a prefix in the title; among
        # whole words, field weight (artist / album artist tie: newer first)
        assert got == ["a-exact", "b-word", "d2-aartist", "d-artist", "e-album", "f-other", "c-prefix"]
        # case, accents and punctuation as the search reads them
        assert await _order(c, "GHÓST!") == got
        # newest first is still there by name
        assert await _order(c, "ghost", sort="added") == ["f-other", "e-album", "d2-aartist", "d-artist",
                                                          "c-prefix", "b-word", "a-exact"]


async def test_whole_word_beats_prefix_in_the_same_field(store):
    s = store
    _add(s, "lovely", 5, title="Lovely Day")
    _add(s, "love", 1, title="Love Me Do")
    _add(s, "loveless", 9, title="Loveless")
    async with _client() as c:
        got = await _order(c, "love")
        assert got[0] == "love"
        assert got[1:] == ["loveless", "lovely"]                       # (both prefixes: newest first)


async def test_several_words_phrase_then_one_field_then_spread(store):
    s = store
    _add(s, "spread", 9, title="Ghost", artist="Love")                  # terms over two fields
    _add(s, "both-title", 8, title="Love the Ghost")                    # both in the title, no phrase
    _add(s, "artist-phrase", 7, title="X", artist="Ghost Love")         # a phrase in the artist
    _add(s, "title-phrase", 6, title="Ghost Love Score")                # a phrase in the title
    _add(s, "exact", 5, title="Ghost Love")                             # the exact title
    _add(s, "album-both", 4, title="Y", album="Love Ghost")             # both in the album
    async with _client() as c:
        got = await _order(c, "ghost love")
        # whole words first (title > artist > album), then phrase / one field
        assert got == ["exact", "title-phrase", "both-title", "artist-phrase", "album-both", "spread"]
        # the last word of a phrase may be a prefix (typing on)
        # (not the exact title now: "Ghost Love" and "Ghost Love Score" tie —
        # newest first)
        got = await _order(c, "ghost lo")
        assert got[:3] == ["title-phrase", "exact", "artist-phrase"]


async def test_ties_newest_first_then_id_fully_deterministic(store):
    s = store
    for tid, added in (("t-b", 5), ("t-a", 5), ("t-c", 5), ("t-old", 1), ("t-new", 9)):
        _add(s, tid, added, title="Crack Intro")
    async with _client() as c:
        got = await _order(c, "crack")
        assert got == ["t-new", "t-c", "t-b", "t-a", "t-old"]           # same score: newest, then id (desc)
        assert got == await _order(c, "crack")                          # (the kept order)
    s2 = TrackStore()
    for tid in reversed(got):                                           # inserted in another order
        s2.upsert_track(dict(s._tracks[tid]))
    assert search_rank.rank(s2, set(got), ["crack"]) == got


async def test_field_operators_filter_and_a_pure_filter_lists_newest_first(store):
    s = store
    _add(s, "g1", 1, title="Love", artist="Ghost", year=2021, format="SID")
    _add(s, "g2", 2, title="Lovers", artist="Ghost", year=2022, format="SID")
    _add(s, "o1", 3, title="Love", artist="Other", year=2023, format="SID")
    _add(s, "g3", 4, title="Nothing", album="Love Songs", artist="Ghost", year=2019, format="MP3")
    async with _client() as c:
        # ``artist:`` filters, the free text ranks
        # (a whole word in the album beats a title that only starts with it)
        assert await _order(c, "artist:Ghost love") == ["g1", "g3", "g2"]
        assert await _order(c, "artist:Ghost love year:>2020") == ["g1", "g2"]
        # no free text: nothing to rank — newest first, said so
        j = (await c.get("/api/search/page", params={"q": "format:SID", "limit": 10})).json()
        assert [t["id"] for t in j["tracks"]] == ["o1", "g2", "g1"] and j["ranked"] is False
        j = (await c.get("/api/search/page", params={"q": "love", "limit": 10})).json()
        assert j["ranked"] is True and j["order"] is None and j["sort"] is None
        # Best match by name is the default order — for a pure filter too (newest first)
        assert await _order(c, "format:SID", sort="rank") == ["o1", "g2", "g1"]
        assert await _order(c, "love", sort="rank", order="asc") == await _order(c, "love")


async def test_the_order_is_computed_once_in_slices_and_stable_across_pages(store, monkeypatch):
    s = store
    rnd = random.Random(5)
    words = ["crack", "cracktro", "intro", "the", "love", "mega"]
    for i in range(400):
        _add(s, f"t{i:04d}", rnd.choice([1, 2, 3, 4, 5]),
             title=" ".join(rnd.choice(words) for _ in range(rnd.randint(1, 3))),
             artist=rnd.choice(["Crack", "Mega", "", "The Crackers"]),
             album=rnd.choice(["", "Crack Album", "Intro"]))
    monkeypatch.setattr(search_sets, "RANK_SLICE", 7)                   # many slices
    turns = [0]
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            turns[0] += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(ticker())
    scored = []
    real = search_rank.Ranker.sort_key
    monkeypatch.setattr(search_rank.Ranker, "sort_key",
                        lambda self, tid, t: scored.append(tid) or real(self, tid, t))
    try:
        async with _client() as c:
            j = (await c.get("/api/search/page", params={"q": "crack", "limit": 50})).json()
            n = j["total"]
            assert n > 200
            # every member scored once, the loop turning between the slices
            assert len(scored) == n and turns[0] >= n // 7 - 1
            full = await _order(c, "crack")
            rs = j["rs"]
            got, off = [t["id"] for t in j["tracks"]], 50
            while off < n:
                lim = rnd.randint(1, 61)
                k = (await c.get("/api/search/page",
                                 params={"q": "crack", "rs": rs, "offset": off, "limit": lim})).json()
                assert k["rs"] == rs and not k["renewed"]
                got += [t["id"] for t in k["tracks"]]
                off += lim
            assert len(scored) == n                                     # (never scored again)
            # a column sort and back: the same Best match, still not re-scored
            await _order(c, "crack", sort="title", order="desc", rs=rs)
            assert await _order(c, "crack", rs=rs) == full and len(scored) == n
            assert got == full == search_rank.rank(s, set(full), ["crack"])
    finally:
        stop.set()
        await task


async def test_a_write_renews_the_ranked_order(store, monkeypatch):
    s = store
    for i in range(60):
        _add(s, f"t{i:03d}", i, title=f"crack part {i}")
    monkeypatch.setattr(search_sets, "RANK_SLICE", 5)
    async with _client() as c:
        j = (await c.get("/api/search/page", params={"q": "crack", "limit": 5})).json()
        assert j["tracks"][0]["id"] == "t059"
        # the oldest track becomes the exact title: it moves to the top of a fresh set
        s.update_track_fields("t000", {"title": "Crack"})
        k = (await c.get("/api/search/page", params={"q": "crack", "limit": 5, "rs": j["rs"]})).json()
        assert k["renewed"] and k["rs"] != j["rs"]
        assert [t["id"] for t in k["tracks"]] == ["t000", "t059", "t058", "t057", "t056"]

        # a write while the order is being scored (the loop turns between slices):
        # the answer comes from the new set, with the new title's place
        async def writer():
            for _ in range(3):
                await asyncio.sleep(0)
            s.update_track_fields("t001", {"title": "crack"})
        search_sets.drop_all(s)
        task = asyncio.create_task(writer())
        r = (await c.get("/api/search/page", params={"q": "crack", "limit": 3})).json()
        await task
        assert [t["id"] for t in r["tracks"]] == search_rank.rank(s, set(_ids_of(s)), ["crack"])[:3]
        assert {t["id"] for t in r["tracks"][:2]} == {"t000", "t001"}


def _ids_of(s: TrackStore) -> list[str]:
    return [tid for tid, t in s._tracks.items() if "crack" in (t.get("title") or "").lower()]


async def test_the_preview_and_the_player_read_best_match(store):
    s = store
    _add(s, "b", 9, title="Ghostbusters")
    _add(s, "a", 1, title="Ghost")
    async with _client() as c:
        # the search box's preview: the first rows of the same order
        j = (await c.get("/api/search/page", params={"q": "ghost", "limit": 8, "fields": "list"})).json()
        assert [t["id"] for t in j["tracks"]] == ["a", "b"]
        # the play queue's pages of the search
        p = (await c.get("/api/search/page", params={"q": "ghost", "rs": j["rs"], "playable": 1,
                                                      "limit": 10})).json()
        assert [t["id"] for t in p["tracks"]] == ["a", "b"] and p["ranked"]
        p = (await c.get("/api/search/page", params={"q": "ghost", "rs": j["rs"], "playable": 1,
                                                      "limit": 10, "after": "a"})).json()
        assert [t["id"] for t in p["tracks"]] == ["b"]


def _lib(s: TrackStore, n: int) -> None:
    rnd = random.Random(9)
    words = ["crack", "cracktro", "intro", "love", "mega"]
    for i in range(n):
        _add(s, f"t{i:04d}", rnd.choice([1, 2, 3]),
             title=" ".join(rnd.choice(words) for _ in range(rnd.randint(1, 3))),
             artist=rnd.choice(["Crack", "Mega", ""]))


async def test_a_first_page_reads_only_as_far_as_it_needs_and_the_rest_follows(store, monkeypatch):
    s = store
    _lib(s, 300)
    monkeypatch.setattr(search_sets, "RANK_SLICE", 16)
    monkeypatch.setattr(search_sets, "MERGE_SLICE", 10)
    async with _client() as c:
        # a preview-sized page: every member scored, the merged order read 10 rows deep
        j = (await c.get("/api/search/page", params={"q": "crack", "limit": 8})).json()
        rset = s._result_sets[j["rs"]]
        o = rset.orders[(search_sets.RANK, False)]
        n = j["total"]
        assert n > 100 and o.pos == n and o.ids is None and len(o.out) == 10
        want = search_rank.rank(s, set(rset.ids), ["crack"])
        assert [t["id"] for t in j["tracks"]] == want[:8]
        # a deeper page reads on; a list page finishes the order in the background
        k = (await c.get("/api/search/page", params={"q": "crack", "rs": j["rs"], "offset": 40,
                                                      "limit": 5})).json()
        assert [t["id"] for t in k["tracks"]] == want[40:45] and o.ids is None and len(o.out) >= 45
        k = (await c.get("/api/search/page", params={"q": "crack", "rs": j["rs"], "limit": 100})).json()
        for _ in range(200):
            if o.ids is not None:
                break
            await asyncio.sleep(0.005)
        assert o.ids == want and o.task is None
        assert await _order(c, "crack", rs=j["rs"]) == want


async def test_a_library_written_on_every_turn_still_gets_a_ranked_answer_without_a_long_block(
        store, monkeypatch):
    """The last attempt of a Best match read keeps its turns (no one-go ranking
    of the whole set) and answers from the set it started in."""
    s = store
    _lib(s, 400)
    monkeypatch.setattr(search_sets, "RANK_SLICE", 8)
    monkeypatch.setattr(search_sets, "RETRIES", 0)                    # (the first attempt is the last)
    stop = asyncio.Event()
    turns = [0]

    async def writer():
        k = 0
        while not stop.is_set():
            _add(s, f"w{k:05d}", 50 + k, title="crack written")
            k += 1
            turns[0] += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(writer())
    try:
        async with _client() as c:
            r = await c.get("/api/search/page", params={"q": "crack", "limit": 20})
    finally:
        stop.set()
        await task
    assert r.status_code == 200
    j = r.json()
    ids = [t["id"] for t in j["tracks"]]
    assert j["ranked"] and len(ids) == 20 == len(set(ids))
    assert turns[0] >= j["total"] // 8 - 1                         # (the loop turned between the slices)


async def test_a_client_gone_mid_ranking_stops_the_read_and_keeps_its_progress(store, monkeypatch):
    s = store
    _lib(s, 300)
    monkeypatch.setattr(search_sets, "RANK_SLICE", 8)
    asked = [0]

    class _Gone:
        async def is_disconnected(self):
            asked[0] += 1
            return True
    r = await search.search_page(q="crack", rs=None, offset=0, limit=50, sort=None, order=None,
                                 fields="list", playable=False, after=None, request=_Gone())
    assert getattr(r, "status_code", None) == 499 and asked[0] == 1
    (rset,) = s._result_sets.values()
    o = rset.orders[(search_sets.RANK, False)]
    done = o.pos
    assert 0 < done < rset.total                                   # (stopped early, the slices kept)
    j = await search.search_page(q="crack", rs=rset.rs, offset=0, limit=50, sort=None, order=None,
                                 fields="list", playable=False, after=None)
    assert j["rs"] == rset.rs and [t["id"] for t in j["tracks"]] == \
        search_rank.rank(s, set(rset.ids), ["crack"])[:50]
