# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search result sets (core/search_sets.py, ``GET /api/search/page``).

* Differential, seeded and randomized: for random queries over a generated
  library, every page of a set concatenated — in every sort key, both
  directions, and the default order — equals a brute-force filter-then-sort
  with All Tracks' keys and tie-break, and equals the All Tracks list
  (``/api/tracks?sort=``) restricted to the same ids.  With duplicates hidden
  or not, offline Mark / Hide, "can't be played" Mark / Hide; walked orders
  (large sets) and sorted ones (small sets); slices with the event loop
  turning between them.
* Writes between pages: a handle that no longer holds is answered from a
  fresh set (``renewed``) — never rows of the two sets mixed — and paging the
  fresh set equals the brute force over the new state.
* The legacy endpoints (``/api/search``, ``/api/search/quick``) answer what
  they always answered; bounds, owner thread, slim rows, playable pages.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import random
import threading
import uuid

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.api.tracks import public_tracks
from soniqboom.core import availability, search_sets, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.store import (TrackStore, _SORTED_SPECS, _sorted_range, _tokenize_text,
                                  _tokenize_track, fold_token)
from soniqboom.core.data import _parse_tag_query

SORT_KEYS = ("added", "year", "duration", "bpm", "title", "artist", "album_artist", "album", "format")
ATTR = {"added": None, "year": "_sorted_year", "duration": "_sorted_duration", "bpm": "_sorted_bpm",
        "title": "_sorted_title", "artist": "_sorted_artist", "album_artist": "_sorted_album_artist",
        "album": "_sorted_album", "format": "_sorted_format"}
WORDS = ["crack", "cracktro", "crackle", "the", "theme", "a", "love", "lovely", "sid", "remix",
         "intro", "Öörni", "oorni", "beyoncé", "x", "zz", "Mega", "mega"]


def ph(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _rand_track(rnd: random.Random, i: int, root: str) -> dict:
    title = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(0, 3)))
    if rnd.random() < 0.08:
        title = rnd.choice(["", "  ", title.upper(), " " + title + " "])
    t = {
        "id": f"t{i:05d}-{rnd.randrange(10 ** 6):06d}",
        "path": f"{root}/d{i % 9}/f{i}.mod",
        "title": title,
        "artist": rnd.choice(["Ab", "ab", "Zed", "", None, "Öörni", "the crack", "Rob • Ben"]),
        "album_artist": rnd.choice(["", "AA", "aa", None, "Mega"]),
        "album": rnd.choice(["", "X", "y", "Love Songs", None]),
        "year": rnd.choice([None, 1990, 1991, "1992", 19930101, 1994.0, 2001, "x"]),
        "duration": rnd.choice([0, None, math.nan, 100.0, 50.5, 50.5, 7, 300]),
        "bpm": rnd.choice([None, 120, 120.0, "90", 0, 175.5]),
        "format": rnd.choice(["SID", "MP3", "ProTracker", "FLAC", "", "sid"]),
        "added_at": rnd.choice([None, 1, 2, 3, 3, 3, 4, 5, 100]),
        "genre": rnd.choice([[], ["Chip"], ["Rock", "Chip"]]),
        "dir_hash": ph(f"{root}/d{i % 9}"),
        "scan_root_hash": ph(root),
        "stil": "STIL text " * rnd.randint(0, 3),
        "sid_md5": f"{i:032x}",
    }
    if rnd.random() < 0.05:
        t["health"] = "broken"
    elif rnd.random() < 0.03:
        t["defect"] = "corrupt"
    return t


def _app() -> FastAPI:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library):
        app.include_router(mod.router, prefix="/api")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A seeded random library of 1,200 tracks on two roots (B can go offline),
    with duplicate groups (some primaries on B), installed as THE store."""
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    shuffle_order.clear()
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    ra, rb = str(a), str(b)
    s.upsert_scan_dir(ra)
    s.upsert_scan_dir(rb)
    rnd = random.Random(20261018)
    rows = [_rand_track(rnd, i, ra if i % 3 else rb) for i in range(1200)]
    # duplicate groups: pairs across the roots — the primary on B for a third
    # of them (while B is offline its copy on A stands in: a PrimarySwap)
    for g in range(80):
        p, c = rows[g * 7], rows[g * 7 + 1]
        gid = f"g{g}"
        p["duplicate_group_id"] = c["duplicate_group_id"] = gid
        p["is_duplicate_primary"], c["is_duplicate_primary"] = True, False
        c["title"], c["artist"], c["format"] = p["title"], p["artist"], "MP3"
    for r in rows:
        s.upsert_track(copy.deepcopy(r))

    class Lib:
        store = s
        root_a, root_b = ra, rb
        rand = rnd

        def offline(self, on=True):
            s.set_scan_dir_status(rb, "unavailable" if on else "ok")

        def mode(self, m):
            s.set_config(availability.MODE_KEY, m)

        def unplay(self, m):
            s.set_config(availability.UNPLAY_KEY, m)

        def dedup(self, on):
            s.set_config("filter_duplicates", bool(on))
    yield Lib()
    availability.reset_for_tests()


# ── Brute force ──────────────────────────────────────────────────────────────

def bf_text(s: TrackStore, q_text: str, t: dict) -> bool:
    toks = [fold_token(x) for x in _tokenize_text(q_text)]
    words = _tokenize_track(t)
    for qt in toks:
        if len(qt) < 2 and s._word_index.get(qt):
            if qt not in words:
                return False
        elif not any(w.startswith(qt) for w in words):
            return False
    return True


def bf_ids(s: TrackStore, q: str) -> set:
    """The ids search ``q`` lists, by testing every track (text: every word a
    word start / a lone character's exact word; ``year:`` / ``format:``)."""
    preds = search.search_predicates(q)
    dedup = bool(s.get_config("filter_duplicates", False))
    excl = availability.hidden(s) or frozenset()
    swap = availability.primary_swap(s, dedup=dedup) if dedup else None
    out = set()
    for tid, t in s._tracks.items():
        if tid in excl:
            continue
        if dedup and not (swap.is_primary(tid, t) if swap is not None
                          else t.get("is_duplicate_primary", True)):
            continue
        if "query" in preds and not bf_text(s, preds["query"], t):
            continue
        if "format_" in preds and s._tag_key(t.get("format", "")) != str(preds["format_"]).strip().lower():
            continue
        if "year_min" in preds or "year_max" in preds:
            y = _SORTED_SPECS["_sorted_year"][1](t)
            lo = preds.get("year_min", -999999)
            hi = preds.get("year_max", 999999)
            if not (lo <= y <= hi):
                continue
        out.add(tid)
    return out


# Best match, written out from its rule (core/search_rank's docstring) — not
# from its code: per field, every term a whole word (2) / a word's prefix (1).
RANK_WEIGHTS = {"title": 8, "artist": 4, "album_artist": 4, "album": 2, "composer": 1,
                "scene_group": 1, "game": 1}


def bf_rank_key(terms: list, t: dict) -> tuple:
    vals = [(f, w, t.get(f)) for f, w in RANK_WEIGHTS.items()]
    vals += [("alias", 1, a) for a in (t.get("game_aliases") or ())]
    uniq = list(dict.fromkeys(terms))
    full_title = whole = phrase = full = total = 0
    cover = (0, 0)
    for f, w, v in vals:
        if not isinstance(v, str) or not v:
            continue
        words = [fold_token(x) for x in _tokenize_text(v)]
        m = [2 if q in words else 1 if any(x.startswith(q) for x in words) else 0 for q in uniq]
        if not any(m):
            continue
        if words == terms:
            full = max(full, w)
            full_title = full_title or f == "title"
        k = len(terms)
        if k >= 2 and any(words[i:i + k - 1] == terms[:-1] and words[i + k - 1].startswith(terms[-1])
                          for i in range(len(words) - k + 1)):
            phrase = max(phrase, w)
        if all(m):
            cover = max(cover, (w, m.count(2)))
        if all(x == 2 for x in m):
            whole = max(whole, w)
        total += w * sum(m)
    return (int(full_title), whole, phrase, cover[0], cover[1], full, total)


def bf_rank(s: TrackStore, ids, q_text: str) -> list:
    terms = [fold_token(x) for x in _tokenize_text(q_text)]
    added = _SORTED_SPECS["_sorted_added_at"][1]
    return [tid for _k, _a, tid in sorted(((bf_rank_key(terms, s._tracks[tid]), added(s._tracks[tid]), tid)
                                            for tid in ids), reverse=True)]


def bf_order(s: TrackStore, ids, sort: str | None, order: str | None, q: str | None = None) -> list:
    """The order a search lists ``ids`` in: a column's, newest first
    (``added``), or — no column — Best match when ``q`` has free text."""
    if sort in (None, "rank") and q is not None:
        text = search.search_predicates(q).get("query")
        if text and _tokenize_text(text):
            return bf_rank(s, ids, text)
    dedup = bool(s.get_config("filter_duplicates", False))
    swap = availability.primary_swap(s, dedup=dedup) if dedup else None
    attr = ATTR.get(sort) if sort and sort != "rank" else None
    if not attr:
        attr = "_sorted_added_at_primary" if dedup and swap is None else "_sorted_added_at"
    desc = s._is_descending(sort, order)
    value = _SORTED_SPECS[attr][1]
    return [tid for _k, tid in sorted(((value(s._tracks[tid]), tid) for tid in ids), reverse=desc)]


async def page_all(c, q, sort=None, order=None, sizes=None, rnd=None, fields=None):
    """Every page of the set, concatenated: (rs, total, ids)."""
    rnd = rnd or random.Random(1)
    p = {"q": q, "limit": rnd.randint(1, 97)}
    if sort:
        p.update(sort=sort, order=order or "asc")
    if fields:
        p["fields"] = fields
    r = await c.get("/api/search/page", params=p)
    assert r.status_code == 200, r.text
    j = r.json()
    rs, total, ids = j["rs"], j["total"], [t["id"] for t in j["tracks"]]
    while len(ids) < total:
        p.update(rs=rs, offset=len(ids), limit=rnd.randint(1, 97))
        k = (await c.get("/api/search/page", params=p)).json()
        assert k["rs"] == rs and not k["renewed"] and k["total"] == total
        assert k["tracks"], "an empty page inside the set"
        ids += [t["id"] for t in k["tracks"]]
    p.update(offset=total, limit=5)
    tail = (await c.get("/api/search/page", params=p)).json()
    assert tail["tracks"] == [] and tail["rs"] == rs
    return rs, total, ids


async def all_tracks_order(c, sort, order):
    """All Tracks' own order (``GET /api/tracks``) of the whole library."""
    ids, off = [], 0
    while True:
        p = {"limit": 500, "offset": off}
        if sort:
            p.update(sort=sort, order=order)
        rows = (await c.get("/api/tracks", params=p)).json()
        ids += [r["id"] for r in rows]
        if len(rows) < 500:
            return ids
        off += 500


QUERIES = ["crack", "the", "a", "x", "love", "oorni", "Öö", "mega the", "zz crack", "sid",
           "beyonce", "c", "*", "-", "format:SID", "format:sid crack", "year:>1991",
           "year:1990-1992 the", "nothingmatches", "…"]


# ── Differential tests ───────────────────────────────────────────────────────

@pytest.mark.parametrize("walk", [False, True], ids=["sorted-small-sets", "walked-orders"])
@pytest.mark.parametrize("mode", ["dedup-mark", "dedup-hide-unplay-hide", "plain-hide", "plain-mark-unplay-hide"])
async def test_every_order_of_every_set_equals_brute_force(lib, monkeypatch, walk, mode):
    if walk:
        # every set walks the sorted index (the large-set path), in slices of 7
        # rows with the event loop turning between them
        monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
        monkeypatch.setattr(search_sets, "SLICE_ROWS", 7)
    lib.dedup(mode.startswith("dedup"))
    lib.offline(True)
    if mode.startswith("dedup"):
        assert availability.primary_swap(lib.store) is not None      # (stand-ins are listed)
    lib.mode("hide" if "-hide" in mode.replace("unplay-hide", "") else "mark")
    lib.unplay("hide" if "unplay-hide" in mode else "mark")
    rnd = random.Random(hash(mode) & 0xFFFF)
    s = lib.store
    async with _client() as c:
        at = {}
        for q in QUERIES:
            want_ids = bf_ids(s, q)
            # the default order
            rs, total, got = await page_all(c, q, rnd=rnd)
            assert total == len(want_ids) == len(got) == len(set(got)), q
            assert got == bf_order(s, want_ids, None, None, q), q
            # Best match asked for by name: the same order; newest first: ``added``
            if q in ("crack", "mega the", "year:1990-1992 the"):
                _rs, _n, named = await page_all(c, q, "rank", "desc", rnd=rnd)
                assert named == got, q
            # every column, both directions
            for key in SORT_KEYS:
                for order in ("asc", "desc"):
                    _rs, n, got = await page_all(c, q, key, order, rnd=rnd)
                    want = bf_order(s, want_ids, key, order)
                    assert n == len(want) and got == want, (q, key, order)
                    if (key, order) not in at:
                        at[(key, order)] = await all_tracks_order(c, key, order)
                    # == a sorted All Tracks filtered to the same ids
                    assert got == [t for t in at[(key, order)] if t in want_ids], (q, key, order)


async def test_writes_between_pages_renew_the_set_never_mixing_rows(lib, monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(search_sets, "SLICE_ROWS", 11)
    lib.dedup(True)
    s = lib.store
    rnd = random.Random(7)

    def w_insert():
        t = _rand_track(rnd, 5000 + rnd.randrange(10 ** 5), lib.root_a)
        t["title"] = "crack the love"
        s.upsert_track(t)

    def w_retag():
        tid = rnd.choice(sorted(bf_ids(s, "crack")))
        s.update_track_fields(tid, {"title": "nothing to see"})

    def w_delete():
        s.delete_track(rnd.choice(sorted(bf_ids(s, "crack"))))

    def w_offline_hide():
        lib.offline(True)
        lib.mode("hide")

    def w_back_online():
        lib.offline(False)

    def w_unplay_hide():
        lib.unplay("hide")

    def w_mark_broken():
        tid = rnd.choice(sorted(bf_ids(s, "crack")))
        s.update_track_fields(tid, {"health": "broken"})

    def w_dedup_off():
        lib.dedup(False)

    writes = [w_insert, w_retag, w_delete, w_offline_hide, w_back_online, w_unplay_hide,
              w_mark_broken, w_dedup_off, w_insert]
    async with _client() as c:
        for w in writes:
            for sort, order in ((None, None), ("title", "desc"), ("year", "asc")):
                p = {"q": "crack", "limit": 13}
                if sort:
                    p.update(sort=sort, order=order)
                j = (await c.get("/api/search/page", params=p)).json()
                rs, before = j["rs"], [t["id"] for t in j["tracks"]]
                old_total = j["total"]
                w()
                # the old handle: answered from a fresh set, flagged — or (no
                # change a list shows) the same set, still exactly right
                k = (await c.get("/api/search/page", params={**p, "rs": rs, "offset": 13})).json()
                want_ids = bf_ids(s, "crack")
                want = bf_order(s, want_ids, sort, order, "crack")
                if k["renewed"]:
                    assert k["rs"] != rs and k["total"] == len(want_ids)
                    assert [t["id"] for t in k["tracks"]] == want[13:26]
                else:
                    assert k["rs"] == rs and k["total"] == old_total == len(want_ids)
                    assert before + [t["id"] for t in k["tracks"]] == want[:26]
                # the client re-queries from the top with the new handle: all pages = brute force
                _rs, n, got = await page_all(c, "crack", sort, order, rnd=rnd)
                assert n == len(want) and got == want, (w.__name__, sort)


async def test_a_browse_write_always_renews_and_a_length_backfill_only_the_duration_order(lib):
    s = lib.store
    async with _client() as c:
        j = (await c.get("/api/search/page", params={"q": "the", "limit": 5, "sort": "title"})).json()
        d = (await c.get("/api/search/page", params={"q": "the", "limit": 5, "sort": "duration",
                                                     "rs": j["rs"]})).json()
        assert d["rs"] == j["rs"] and not d["renewed"]
        tid = d["tracks"][0]["id"]
        # a play-time length backfill: duration only — re-keys the duration list alone
        assert s.update_track_fields(tid, {"duration": 9999.0})
        t = (await c.get("/api/search/page", params={"q": "the", "limit": 5, "sort": "title", "rs": j["rs"]})).json()
        assert t["rs"] == j["rs"] and not t["renewed"]            # (the ids and the title order hold)
        d2 = (await c.get("/api/search/page", params={"q": "the", "limit": 5, "sort": "duration",
                                                      "rs": j["rs"]})).json()
        assert d2["renewed"] and d2["rs"] != j["rs"]
        want = bf_order(s, bf_ids(s, "the"), "duration", "asc")
        assert [r["id"] for r in d2["tracks"]] == want[:5]
        # the renewed set serves the duration order from now on, the old handle
        # is answered from it (no new set each time)
        d3 = (await c.get("/api/search/page", params={"q": "the", "limit": 5, "sort": "duration",
                                                      "rs": j["rs"]})).json()
        assert d3["rs"] == d2["rs"] and d3["renewed"]
        # any browse write: every handle stale
        s.update_track_fields(tid, {"title": "the renamed one"})
        t2 = (await c.get("/api/search/page", params={"q": "the", "limit": 5, "rs": d2["rs"]})).json()
        assert t2["renewed"] and t2["rs"] != d2["rs"]


async def test_a_write_while_a_deep_read_yields_is_answered_from_the_new_set(lib, monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(search_sets, "SLICE_ROWS", 3)          # many slices: many turns of the loop
    s = lib.store
    async with _client() as c:
        j = (await c.get("/api/search/page", params={"q": "the", "limit": 3, "sort": "artist"})).json()
        writes = []

        async def writer():
            for _ in range(5):
                await asyncio.sleep(0)
            t = _rand_track(random.Random(3), 9999, lib.root_a)
            t["title"] = "the newest"
            s.upsert_track(t)
            writes.append(t["id"])
        task = asyncio.create_task(writer())
        deep = (await c.get("/api/search/page", params={"q": "the", "rs": j["rs"], "sort": "artist",
                                                        "offset": 300, "limit": 20})).json()
        await task
        assert writes and deep["renewed"]
        want = bf_order(s, bf_ids(s, "the"), "artist", "asc")
        assert deep["total"] == len(want) and [r["id"] for r in deep["tracks"]] == want[300:320]


# ── Legacy endpoints, bounds, details ────────────────────────────────────────

def _wire(obj):
    """``obj`` as a client reads it back (NaN → null, as ``json_bytes`` sends it)."""
    import json
    return json.loads(tracks_api.json_bytes(obj))


@pytest.mark.parametrize("dedup", [False, True])
@pytest.mark.parametrize("mode", ["mark", "hide"])
async def test_legacy_search_and_quick_answer_what_they_always_did(lib, dedup, mode):
    lib.dedup(dedup)
    lib.offline(True)
    lib.mode(mode)
    s = lib.store
    excl = availability.hidden(s)
    swap = availability.primary_swap(s, dedup=dedup)
    async with _client() as c:
        for q in ["crack", "the", "a", "format:SID", "zz", "*"]:
            preds = _parse_tag_query(search._parse_advanced_query(q)
                                     or q.replace("-", "\\-").replace(":", "\\:").replace("/", "\\/"))
            old = availability.mark_rows(s.filter_tracks(**preds, limit=200, filter_duplicates=dedup,
                                                         exclude=excl, swap=swap), s)
            got = (await c.get("/api/search", params={"q": q, "limit": 200})).json()
            assert got == _wire(old) and got, q
            oldq = availability.mark_rows(public_tracks(s.filter_tracks(
                **preds, limit=8, filter_duplicates=dedup, exclude=excl, swap=swap)), s)
            assert (await c.get("/api/search/quick", params={"q": q, "limit": 8})).json() == _wire(oldq), q
            # the page endpoint's newest-first rows are the same rows (offline
            # flags included), in the same order
            pg = (await c.get("/api/search/page", params={"q": q, "limit": 200, "sort": "added"})).json()
            assert pg["tracks"] == got and not pg["ranked"], q
            # its default (Best match) lists the same set
            best = (await c.get("/api/search/page", params={"q": q, "limit": 5000})).json()
            assert best["ranked"] == (q not in ("format:SID", "*"))
            pg = (await c.get("/api/search/page", params={"q": q, "limit": 5000, "sort": "added"})).json()
            assert sorted(r["id"] for r in best["tracks"]) == sorted(r["id"] for r in pg["tracks"]), q
            if mode == "mark" and q == "the":
                assert any(r.get("offline") for r in got)          # (flags are there to compare)


async def test_slim_rows_leave_out_only_the_fields_no_list_reads(lib):
    async with _client() as c:
        full = (await c.get("/api/search/page", params={"q": "the", "limit": 50})).json()
        slim = (await c.get("/api/search/page", params={"q": "the", "limit": 50, "fields": "list",
                                                        "rs": full["rs"]})).json()
    assert [r["id"] for r in full["tracks"]] == [r["id"] for r in slim["tracks"]]
    for f, l in zip(full["tracks"], slim["tracks"]):
        assert not (set(l) & search_sets.LIST_DROP)
        assert {k: v for k, v in f.items() if k not in search_sets.LIST_DROP} == l
        assert "stil" in f and "sid_md5" in f and "title" in l and "path" in l


async def test_playable_pages_skip_what_cant_play_with_exact_positions(lib):
    lib.offline(True)
    lib.mode("mark")
    s = lib.store
    async with _client() as c:
        _rs, total, order = await page_all(c, "the", "title", "asc")
        dead = availability.unplayable(s) or frozenset()
        got, off = [], 0
        while True:
            pg = (await c.get("/api/search/page", params={"q": "the", "sort": "title", "order": "asc",
                                                          "offset": off, "limit": 17, "playable": 1})).json()
            for r in pg["tracks"]:
                assert order[r["_pos"]] == r["id"] and r["id"] not in dead
                got.append(r["id"])
            off = pg["next_offset"]
            if pg["ended"]:
                break
        assert got == [t for t in order if t not in dead]
        # after=: right after the anchor
        anchor = got[10]
        pg = (await c.get("/api/search/page", params={"q": "the", "sort": "title", "order": "asc",
                                                      "offset": 0, "limit": 3, "playable": 1,
                                                      "after": anchor})).json()
        assert [r["id"] for r in pg["tracks"]] == got[11:14]


async def test_sets_are_bounded_and_kept_by_the_owner_thread_only(lib, monkeypatch):
    s = lib.store
    monkeypatch.setattr(search_sets, "MAX_SETS", 3)
    preds = [search.search_predicates(q) for q in ("crack", "the", "love", "sid", "a")]
    for p in preds:
        search_sets.head(s, p, 5)
    assert len(s._result_sets) == 3
    assert set(s._result_set_keys.values()) <= set(s._result_sets)
    # the id budget: the least recently used go first, never the one in use
    monkeypatch.setattr(search_sets, "MAX_IDS", 1)
    rset, _ids = search_sets.head(s, search.search_predicates("x"), 5)
    assert list(s._result_sets) == [rset.rs]
    # another thread computes privately
    out = {}

    def other():
        out["r"] = search_sets.head(s, search.search_predicates("theme"), 5)
    th = threading.Thread(target=other)
    th.start()
    th.join()
    assert out["r"][1] and out["r"][0].rs not in s._result_sets
    assert list(s._result_sets) == [rset.rs]
    # the library replaced: every set dropped
    s.browse_changed()
    assert not s._result_sets and not s._result_set_keys


async def test_case_and_spacing_share_a_set_and_a_foreign_handle_is_not_used(lib):
    async with _client() as c:
        a = (await c.get("/api/search/page", params={"q": "Crack  The", "limit": 5})).json()
        b = (await c.get("/api/search/page", params={"q": "crack the", "limit": 5})).json()
        assert a["rs"] == b["rs"] and a["tracks"] == b["tracks"]
        # the handle of ANOTHER search is not this one's
        o = (await c.get("/api/search/page", params={"q": "love", "limit": 5, "rs": a["rs"]})).json()
        assert o["rs"] != a["rs"] and o["renewed"]
        assert {r["id"] for r in o["tracks"]} <= bf_ids(lib.store, "love")


async def test_a_list_page_finishes_its_order_in_the_background(lib, monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(search_sets, "SLICE_ROWS", 50)
    s = lib.store
    async with _client() as c:
        j = (await c.get("/api/search/page", params={"q": "the", "limit": 100, "sort": "album"})).json()
        rset = s._result_sets[j["rs"]]
        o = rset.orders[("_sorted_album", False)]
        assert o.ids is None and o.task is not None             # (a walk, read so far)
        for _ in range(2000):
            if o.ids is not None:
                break
            await asyncio.sleep(0.003)
        assert o.ids == bf_order(s, bf_ids(s, "the"), "album", "asc") and o.task is None
        # descending is the finished order reversed: no second walk
        d = (await c.get("/api/search/page", params={"q": "the", "rs": j["rs"], "limit": 7,
                                                     "sort": "album", "order": "desc"})).json()
        assert [r["id"] for r in d["tracks"]] == o.ids[::-1][:7]
        assert ("_sorted_album", True) not in rset.orders
        # a preview-sized page starts no background read
        p = (await c.get("/api/search/page", params={"q": "the", "limit": 8, "sort": "artist",
                                                     "rs": j["rs"]})).json()
        assert s._result_sets[p["rs"]].orders[("_sorted_artist", False)].task is None


def test_filter_walk_advance_reads_bounded_slices_like_upto():
    lst = sorted((random.Random(5).random(), f"id{i}") for i in range(1000))
    member = {tid for k, (_v, tid) in enumerate(lst) if k % 3 == 0}
    for desc in (False, True):
        a = store_mod._FilterWalk(lst, (0, 0), None, None, member, desc)
        b = store_mod._FilterWalk(lst, (0, 0), None, None, member, desc)
        while not b.done:
            pos = b.pos
            b.advance(37)
            assert abs(b.pos - pos) <= 37
        assert a.full() == b.out == [tid for _v, tid in (reversed(lst) if desc else lst) if tid in member]


def test_result_ids_is_never_a_live_index_set(lib):
    s = lib.store
    live = s._tag_format["sid"]
    got = s.result_ids(format_="SID")
    assert got == live and got is not live
    assert s._candidate_ids(format_="SID") is live                 # (other callers: unchanged)
    # several predicates, or text: computed sets anyway
    assert s.result_ids(query="crack") == s._candidate_ids(query="crack")


async def test_a_drive_going_offline_in_mark_mode_renews_a_deduped_set(lib):
    """With duplicates hidden, Mark mode: a drive going away changes only which
    copy stands in for a group (``availability.primary_swap``) — no track is
    written, no ``_browse_seq`` move — and the set must still be renewed."""
    lib.dedup(True)
    lib.mode("mark")
    s = lib.store
    async with _client() as c:
        j = (await c.get("/api/search/page", params={"q": "crack", "limit": 10})).json()
        seq = s._browse_seq
        lib.offline(True)                          # the primaries on B can't play now
        assert s._browse_seq == seq
        assert availability.primary_swap(s) is not None
        k = (await c.get("/api/search/page", params={"q": "crack", "limit": 10, "rs": j["rs"],
                                                     "offset": 10})).json()
        assert k["renewed"] and k["rs"] != j["rs"]
        want = bf_order(s, bf_ids(s, "crack"), None, None, "crack")
        _rs, n, got = await page_all(c, "crack")
        assert got == want and n == len(want)
        stand_ins = availability.primary_swap(s).promoted
        assert stand_ins & set(got)                # (the stand-ins are listed in their primaries' place)


async def test_a_scan_in_progress_lists_every_member_in_every_order(lib, monkeypatch):
    """Batch mode (a scan commit) defers the sorted lists: tracks it added are
    in the set but not yet in the index a walk reads.  Every order still lists
    each member exactly once (the missing ones after the walk, by the index's
    keys), the total holds, and the batch exit makes the set stale."""
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(search_sets, "SLICE_ROWS", 9)
    s = lib.store
    rnd = random.Random(11)
    s.enter_batch_mode()
    try:
        added = []
        for k in range(25):
            t = _rand_track(rnd, 7000 + k, lib.root_a)
            t["title"] = "crack batch " + rnd.choice(WORDS)
            s.upsert_track(t)
            added.append(t["id"])
        assert s._dirty_sorted                       # (the lists wait for the batch exit)
        want_ids = bf_ids(s, "crack")
        assert set(added) <= want_ids
        async with _client() as c:
            for sort, order in ((None, None), ("title", "asc"), ("title", "desc"), ("year", "asc"),
                                ("year", "desc")):
                _rs, n, got = await page_all(c, "crack", sort, order, rnd=rnd)
                assert n == len(want_ids) == len(got) == len(set(got)) and set(got) == want_ids, sort
                if sort is None:
                    # (Best match scores the tracks themselves: no index involved)
                    assert got == bf_order(s, want_ids, None, None, "crack")
                    continue
                # the index doesn't hold the batch's tracks yet: they come last in
                # EVERY direction (one direction is not the other reversed then)
                assert set(got[-len(added):]) == set(added), (sort, order)
            j = (await c.get("/api/search/page", params={"q": "crack", "limit": 5})).json()
    finally:
        s.exit_batch_mode()
    async with _client() as c:
        k = (await c.get("/api/search/page", params={"q": "crack", "limit": 5, "rs": j["rs"]})).json()
        assert k["renewed"]                          # (the lists were rebuilt: a new set)
        for sort, order in ((None, None), ("title", "asc"), ("year", "desc")):
            _rs, n, got = await page_all(c, "crack", sort, order, rnd=rnd)
            assert got == bf_order(s, bf_ids(s, "crack"), sort, order, "crack"), sort



async def test_a_library_written_on_every_turn_of_the_loop_still_answers(lib, monkeypatch):
    """A scan commit / an import writes on (almost) every turn of the event
    loop: a read that gives the loop a turn finds its set stale every time.
    Every request still answers 200 — the last attempt reads without a turn —
    each answer is one set's (no id twice, the page as long as the set says),
    and once the writes stop, paging equals the brute force."""
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(search_sets, "SLICE_ROWS", 5)
    s = lib.store
    rnd = random.Random(23)
    stop = asyncio.Event()
    n_writes = [0]

    async def writer():
        k = 0
        while not stop.is_set():
            batch = []
            for _ in range(3):
                t = _rand_track(rnd, 20000 + k, lib.root_a)
                t["title"] = "crack " + rnd.choice(WORDS)
                batch.append(t)
                k += 1
            s.upsert_tracks_batch(batch)
            n_writes[0] += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(writer())
    try:
        async with _client() as c:
            rs = None
            for i in range(25):
                p = {"q": "crack", "limit": rnd.choice([8, 50, 200]), "offset": rnd.choice([0, 37, 400]),
                     "sort": rnd.choice([None, "title", "year", "artist"]), "order": rnd.choice(["asc", "desc"])}
                if rs:
                    p["rs"] = rs
                if i % 5 == 4:
                    p["playable"] = 1
                r = await c.get("/api/search/page", params={k: v for k, v in p.items() if v is not None})
                assert r.status_code == 200, r.text
                j = r.json()
                ids = [t["id"] for t in j["tracks"]]
                assert len(ids) == len(set(ids))
                if not p.get("playable"):
                    assert len(ids) == max(0, min(p["limit"], j["total"] - p["offset"]))
                rs = j["rs"]
    finally:
        stop.set()
        await task
    assert n_writes[0] > 25                          # (the writes really interleaved)
    async with _client() as c:
        for sort, order in ((None, None), ("title", "desc")):
            _rs, n, got = await page_all(c, "crack", sort, order, rnd=rnd)
            assert got == bf_order(s, bf_ids(s, "crack"), sort, order, "crack")


async def test_the_legacy_endpoints_keep_no_result_sets(lib):
    s = lib.store
    async with _client() as c:
        for q in ("crack", "the", "*"):
            assert (await c.get("/api/search", params={"q": q, "limit": 20})).status_code == 200
            assert (await c.get("/api/search/quick", params={"q": q, "limit": 8})).status_code == 200
    assert not s._result_sets and not s._result_set_keys
