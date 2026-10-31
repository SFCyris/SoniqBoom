# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scoped Best match (``GET /api/search/page?rank_by=``): the phone's TITLE /
ARTIST / ALBUM / GENRE / GAME keys send their words as an operator
(``title:"space"``) plus ``rank_by=<operator>``; the set is then listed Best
match for those words, judged on that operator's fields only
(``search_rank.SCOPES``) — same matches, same total.  Without ``rank_by`` an
operator-only search keeps its order (newest first; ``artist:`` album-artist
matches first — the desktop's typed operators).  Filters alone, free text, an
unknown ``rank_by``: unchanged.  The scope is part of the set's key.

Hand-built libraries where the newest track is the one that should come LAST
by relevance, so newest first would fail every expectation.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search
from soniqboom.api import tracks as tracks_api
from soniqboom.api.search import search_predicates
from soniqboom.core import availability, search_rank, search_sets, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    for mod in (tracks_api, search, library):
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


async def _get(c, q: str, **kw) -> dict:
    j = (await c.get("/api/search/page", params={"q": q, "limit": 5000, **kw})).json()
    assert j["total"] == len(j["tracks"])
    return j


def _ids(j: dict) -> list[str]:
    return [t["id"] for t in j["tracks"]]


def _newest_first(s: TrackStore, ids) -> list[str]:
    return sorted(ids, key=lambda i: (s._tracks[i]["added_at"], i), reverse=True)


async def test_title_scope_exact_then_whole_word_then_prefix_same_matches(store):
    s = store
    _add(s, "exact", 1, title="Space")
    _add(s, "exact2", 2, title="space")
    _add(s, "word", 3, title="Lost in Space")
    _add(s, "word-start", 4, title="Space Oddity")
    _add(s, "prefix", 5, title="Spacebaby")
    _add(s, "prefix2", 6, title="Space-Metroidvania")      # (a word "space": whole word)
    _add(s, "artist-only", 7, title="Song", artist="Space")  # not a title match: not listed
    _add(s, "newest", 9, title="Giant Stone Spaceship")
    async with _client() as c:
        plain = await _get(c, 'title:"space"')
        scoped = await _get(c, 'title:"space"', rank_by="title")
        # the matches don't change
        assert plain["total"] == scoped["total"] == 7
        assert sorted(_ids(plain)) == sorted(_ids(scoped))
        assert "artist-only" not in _ids(scoped)
        # without rank_by: newest first, not ranked (as before)
        assert plain["ranked"] is False and plain["rank_by"] is None
        assert _ids(plain) == _newest_first(s, _ids(plain))
        # with it: Best match on the title — exact, then whole word, then prefix,
        # ties newest first
        assert scoped["ranked"] is True and scoped["rank_by"] == "title"
        assert scoped["artist_first"] is False and scoped["order"] is None
        assert _ids(scoped) == ["exact2", "exact", "prefix2", "word-start", "word", "newest", "prefix"]
        # the ranker's own order for these terms and fields
        assert _ids(scoped) == search_rank.rank(s, set(_ids(plain)), ["space"], "title")


async def test_artist_scope_exact_artist_before_longer_names(store):
    s = store
    _add(s, "beek", 1, title="A", artist="Beek", album_artist="Beek")
    _add(s, "aa-beek", 2, title="B", artist="Ronan", album_artist="Beek")
    _add(s, "dutch", 3, title="C", artist="Dutch Beek", album_artist="Dutch Beek")
    _add(s, "beekman", 4, title="D", artist="Beekman")
    _add(s, "tron", 5, title="Beek", artist="Tron Beekveld Erwin")          # title "Beek" adds nothing
    _add(s, "tron2", 6, title="E", artist="Tron Beekveld Erwin")
    async with _client() as c:
        plain = await _get(c, 'artist:"beek"')
        scoped = await _get(c, 'artist:"beek"', rank_by="artist")
        assert sorted(_ids(plain)) == sorted(_ids(scoped)) and scoped["total"] == 6
        # exact (artist or album artist — an album artist equal to the artist
        # counts once: a tie, newest first), then whole word, then prefixes
        assert _ids(scoped) == ["aa-beek", "beek", "dutch", "tron2", "tron", "beekman"]
        assert scoped["ranked"] is True and scoped["artist_first"] is False
        # the desktop's typed operator (no rank_by): album-artist matches first,
        # each part newest first — unchanged
        assert plain["ranked"] is False and plain["artist_first"] is True
        head = _newest_first(s, ["beek", "aa-beek", "dutch"])
        assert _ids(plain) == head + _newest_first(s, ["beekman", "tron", "tron2"])


async def test_album_scope_exact_before_prefix(store):
    s = store
    _add(s, "exact", 1, title="X", album="Imaginaerum")
    _add(s, "word", 2, title="Y", album="Imaginaerum Covers")
    _add(s, "prefix", 3, title="Z", album="Imaginaerums")
    async with _client() as c:
        plain = await _get(c, 'album:"imaginaerum"')
        scoped = await _get(c, 'album:"imaginaerum"', rank_by="album")
        assert _ids(plain) == ["prefix", "word", "exact"]
        assert _ids(scoped) == ["exact", "word", "prefix"]


async def test_scope_scores_only_its_own_fields(store):
    s = store
    # both titles hold "ghost" as a prefix only; the older one's ARTIST is
    # "Ghost" — Best match over every field would put it first, the title
    # scope must not (a tie: newest first)
    _add(s, "artist-ghost", 1, title="Ghosts of Mars", artist="Ghost")
    _add(s, "plain", 2, title="Ghostbusters")
    async with _client() as c:
        scoped = await _get(c, 'title:"ghost"', rank_by="title")
        assert _ids(scoped) == ["plain", "artist-ghost"]
        assert search_rank.rank(s, {"plain", "artist-ghost"}, ["ghost"]) == ["artist-ghost", "plain"]


async def test_genre_and_game_scopes(store):
    s = store
    _add(s, "g-exact", 1, title="A", genre=["Synth"])
    _add(s, "g-prefix", 2, title="B", genre=["Synthpop"])
    _add(s, "g-derived", 3, title="C", genre=[], genre_derived=["Synth"])
    _add(s, "u-exact", 1, title="D", game="Uridium")
    _add(s, "u-more", 2, title="E", game="Uridium 2")
    async with _client() as c:
        plain = await _get(c, 'genre:"synth"')
        scoped = await _get(c, 'genre:"synth"', rank_by="genre")
        assert sorted(_ids(plain)) == sorted(_ids(scoped))
        assert _ids(scoped) == ["g-derived", "g-exact", "g-prefix"]
        plain = await _get(c, 'game:"uridium"')
        scoped = await _get(c, 'game:"uridium"', rank_by="game")
        assert sorted(_ids(plain)) == sorted(_ids(scoped)) == ["u-exact", "u-more"]
        assert _ids(scoped) == ["u-exact", "u-more"]
        assert _ids(plain) == ["u-more", "u-exact"]
    # the game's other names weigh as the game; the album doesn't count
    r = search_rank.Ranker(["uridium"], "game")
    assert r.key({"game": "Other", "game_aliases": ["Uridium"]}) == r.key({"game": "Uridium"})
    assert r.key({"game": "Uridium"}) > r.key({"game": "Uridium 2"}) > r.key({"title": "Uridium"})
    assert r.key({"album": "Uridium"}) == 0


async def test_filters_only_free_text_and_unknown_scope_unchanged(store):
    s = store
    _add(s, "a", 1, title="Space", year=1990)
    _add(s, "b", 2, title="Spacebaby", year=1991)
    _add(s, "c", 3, title="Other", year=1992)
    async with _client() as c:
        # filters alone (the phone sends no rank_by then; sent anyway: ignored)
        for kw in ({}, {"rank_by": "title"}):
            j = await _get(c, 'year:"1990-1999"', **kw)
            assert j["ranked"] is False and j["rank_by"] is None and _ids(j) == ["c", "b", "a"]
        # free text ranks on every field, as without rank_by
        base = await _get(c, "space")
        j = await _get(c, "space", rank_by="title")
        assert _ids(j) == _ids(base) and j["ranked"] is True and j["rank_by"] is None
        # an unknown scope / another operator's: ignored
        for bad in ("bogus", "year", ""):
            j = await _get(c, 'title:"space"', rank_by=bad)
            assert j["ranked"] is False and _ids(j) == ["b", "a"]
        # the scope of another operator than the one in q: ignored
        j = await _get(c, 'title:"space"', rank_by="artist")
        assert j["ranked"] is False and _ids(j) == ["b", "a"]


def test_scope_terms_reads_every_value_of_its_operator():
    st = search_rank.scope_terms
    assert st(search_predicates('title:"space odyssey"'), "title") == ["space", "odyssey"]
    assert st(search_predicates('title:"lost" title:"in space" year:"1990-1999"'), "title") == \
        ["lost", "in", "space"]
    # alternatives rank nothing; the scoped words beside them do
    assert st(search_predicates("title:a1,b2"), "title") == []
    assert st(search_predicates('title:"space" title:a1,b2'), "title") == ["space"]
    # words cut as the operator cuts them; the game's article dropped
    assert st(search_predicates('title:"rock’n’roll"'), "title") == ["rock", "n", "roll"]
    assert st(search_predicates('title:"Pokémon"'), "title") == ["pokemon"]
    assert st(search_predicates('game:"the last ninja"'), "game") == ["last", "ninja"]
    assert st(search_predicates('title:"space" -title:"baby"'), "title") == ["space"]
    assert st(search_predicates('title:"space" love'), "title") == []       # (free text ranks)
    assert st(search_predicates('artist:"beek"'), "title") == []
    assert st(search_predicates('artist:"beek"'), None) == []
    assert st(search_predicates('game:"last ninja"'), "game") == ["last", "ninja"]


def test_scope_is_part_of_the_set_key(store):
    s = store
    _add(s, "exact", 1, title="Space")
    _add(s, "prefix", 2, title="Spacebaby")
    preds = search_predicates('title:"space"')
    st = search_sets.State(s)
    plain, _ = search_sets.resolve(s, preds, state=st)
    scoped, _ = search_sets.resolve(s, preds, state=st, rank_by="title")
    assert plain.rs != scoped.rs and plain.key != scoped.key
    assert plain.scope is None and scoped.scope == "title"
    assert plain.attr_for(s, None) == plain.default_attr
    assert scoped.attr_for(s, None) == search_sets.RANK
    assert scoped.ids == plain.ids
    # each is kept under its own key: asked again, the same set
    again, renewed = search_sets.resolve(s, preds, state=search_sets.State(s), rank_by="title")
    assert again is scoped and not renewed
    again, renewed = search_sets.resolve(s, preds, state=search_sets.State(s))
    assert again is plain and not renewed
    # a handle of the other order is not used for this one
    other, renewed = search_sets.resolve(s, preds, rs=plain.rs, state=search_sets.State(s),
                                         rank_by="title")
    assert other is scoped and renewed
    # rank_by that doesn't apply: the plain key (no extra set)
    same, _ = search_sets.resolve(s, preds, state=search_sets.State(s), rank_by="bogus")
    assert same is plain
    assert search_sets.search_key(preds, False) == search_sets.search_key(preds, False, None)
    # a renewed copy keeps the scope
    assert scoped.clone(search_sets.State(s), "x").scope == "title"


async def test_queue_pages_follow_the_scoped_order(store):
    s = store
    for i in range(30):
        _add(s, f"p{i:02d}", 100 + i, title=f"Spacey {i}")         # prefixes, newest
    _add(s, "exact", 1, title="Space")
    _add(s, "word", 2, title="Lost in Space")
    async with _client() as c:
        first = await _get(c, 'title:"space"', rank_by="title", fields="list")
        order = _ids(first)
        assert order[:2] == ["exact", "word"]
        # the player's playable pages of that set (its rs + the same rank_by)
        got, off = [], 0
        while True:
            j = (await c.get("/api/search/page", params={
                "q": 'title:"space"', "rs": first["rs"], "rank_by": "title", "playable": 1,
                "offset": off, "limit": 7, "fields": "list"})).json()
            assert j["rs"] == first["rs"] and j["renewed"] is False and j["ranked"] is True
            got += [t["id"] for t in j["tracks"]]
            if j["ended"]:
                break
            off = j["next_offset"]
        assert got == order
        # resuming after a track: right after it in the same order
        j = (await c.get("/api/search/page", params={
            "q": 'title:"space"', "rs": first["rs"], "rank_by": "title", "playable": 1,
            "offset": 0, "limit": 3, "after": "word"})).json()
        assert [t["id"] for t in j["tracks"]] == order[2:5]
        # the shuffle of the same search deals every match
        sh = (await c.get("/api/tracks/shuffled", params={
            "q": 'title:"space"', "seed": 7, "offset": 0, "limit": 100})).json()
        rows = sh["tracks"] if isinstance(sh, dict) else sh
        assert sorted(t["id"] for t in rows) == sorted(order)


def test_phone_scope_operators_are_the_rank_scopes():
    """Every scope key of the phone (js/mobile/search_query.js ``SCOPES``) but
    ALL names an operator the server ranks by, and its predicate is the one
    that operator parses to."""
    import re
    from pathlib import Path
    src = (Path(search.__file__).parent.parent / "frontend/js/mobile/search_query.js").read_text()
    block = src[src.index("export const SCOPES"):]
    block = block[:block.index("];")]
    ops = {m.group(1) for m in re.finditer(r"\['\w+', '[^']+', '(\w+)'\]", block)}
    assert ops == set(search_rank.SCOPES)
    for op, (pred, _fields, _lists) in search_rank.SCOPES.items():
        assert pred in search_predicates(f'{op}:"zz"'), op


async def test_scope_words_cut_like_the_operator(store):
    s = store
    _add(s, "curly", 1, title="Rock’n’Roll")
    _add(s, "dot", 2, title="Ghost・Roll")
    _add(s, "prefix", 9, title="Rollercoaster")
    async with _client() as c:
        j = await _get(c, 'title:"roll"', rank_by="title")
        assert sorted(_ids(j)) == sorted(_ids(await _get(c, 'title:"roll"')))
        assert _ids(j) == ["dot", "curly", "prefix"]          # (whole words first)


async def test_game_scope_title_of_a_tune_without_a_game_and_the_article(store):
    s = store
    _add(s, "sidtitle", 9, title="Uridium", format="SID", path=f"{s.test_root}/u.sid")
    _add(s, "game2", 2, title="Main", game="Uridium 2", format="SID", path=f"{s.test_root}/u2.sid")
    _add(s, "gexact", 1, title="Main", game="Uridium", format="SID", path=f"{s.test_root}/u3.sid")
    # a track with a sure game: its title is not the game
    _add(s, "titled", 10, title="Uridium", game="Paradroid", format="SID", path=f"{s.test_root}/p.sid")
    _add(s, "ninja", 3, title="X", game="The Last Ninja")
    _add(s, "ninja2", 8, title="Y", game="Last Ninja 2")
    async with _client() as c:
        plain = await _get(c, 'game:"uridium"')
        j = await _get(c, 'game:"uridium"', rank_by="game")
        assert sorted(_ids(j)) == sorted(_ids(plain)) == ["game2", "gexact", "sidtitle"]
        assert _ids(j) == ["sidtitle", "gexact", "game2"]     # (exact: newest first)
        j = await _get(c, 'game:"last ninja"', rank_by="game")
        assert _ids(j) == ["ninja", "ninja2"]                  # ("The Last Ninja" is exact)


async def test_genre_held_twice_counts_once(store):
    s = store
    _add(s, "dup", 1, title="A", genre=["Synth"], genre_derived=["synth"])
    _add(s, "own", 2, title="B", genre=["Synth"])
    async with _client() as c:
        j = await _get(c, 'genre:"synth"', rank_by="genre")
        assert _ids(j) == ["own", "dup"]                       # (a tie: newest first)


async def test_a_stale_handle_is_renewed_in_the_scoped_order(store):
    s = store
    _add(s, "exact", 1, title="Space")
    _add(s, "prefix", 5, title="Spacebaby")
    async with _client() as c:
        first = await _get(c, 'title:"space"', rank_by="title")
        _add(s, "word", 9, title="Lost in Space")             # (a browse write: the set is stale)
        j = (await c.get("/api/search/page", params={
            "q": 'title:"space"', "rs": first["rs"], "rank_by": "title", "playable": 1,
            "offset": 0, "limit": 50})).json()
        assert j["renewed"] is True and j["rs"] != first["rs"] and j["ranked"] is True
        assert [t["id"] for t in j["tracks"]] == ["exact", "word", "prefix"]
        assert j["rank_by"] == "title"
