# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search filters, phase 3 (internal/SEARCH-FILTERS-PLAN.md).

* A7 an ``artist:`` search without free text lists its album-artist matches
  first (each part newest first) — the search page's default order and
  ``filter_tracks``' (``/search``, ``/quick``, saved searches, Subsonic).
* A8 a quoted run of words in the free text is a phrase (one field,
  consecutive words); ``-"live at"`` leaves the phrase out.
* B11 ``bpm:``, B12 ``tunes:`` / ``chip:``, B14 ``dup:`` ``channels:``
  ``has:`` (stil, game; ``has:cover`` / ``has:lyrics`` are free text — not
  stored per track).  ``instrument:`` / ``label:`` / ``comment:`` (B13 and
  part of B14) were dropped: free text.

Brute force from the documented meaning, every list surface, the indexes
following writes that move no browse state.
"""
from __future__ import annotations

import copy
import math
import random
import re
import unicodedata

import httpx
import pytest

from soniqboom.api import library
from soniqboom.api.search import run_search, search_predicates
from soniqboom.core import availability, search_sets, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore

from test_search_filters_phase1 import _all_shuffled, _app
from test_subsonic_parity import _tr, env  # noqa: F401 — fixture


# ── parse ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,want", [
    # A8
    ('"song of myself"', {"phrase": ("song of myself",), "query": '"song of myself"'}),
    ('"song of myself" whitman', {"phrase": ("song of myself",),
                                  "query": '"song of myself" whitman'}),
    ('x "a b" "c d"', {"phrase": ("a b", "c d"), "query": 'x "a b" "c d"'}),
    ('"love"', {"query": '"love"'}),                        # (one word: no phrase)
    ('"song of', {"phrase": ("song of",), "query": '"song of'}),     # (typing)
    ('-"live at" nightwish', {"query": "nightwish", "negate": ((("phrase", ("live at",)),),)}),
    ('-"live"', {"negate": ((("query", "live"),),)}),
    ('artist:x "a b"', {"artist_match": "x", "phrase": ("a b",), "query": '"a b"'}),
    # B11 / B12 / B14 numbers
    ("bpm:120", {"bpm_min": 120, "bpm_max": 120}),
    ("bpm:120-130", {"bpm_min": 120, "bpm_max": 130}),
    ("BPM: > 140", {"bpm_min": 141}),
    ("tunes:>1", {"tunes_min": 2}),
    ("tunes:1", {"tunes_min": 1, "tunes_max": 1}),
    ("channels:>4", {"channels_min": 5}),
    ("channels:2,6", {"anyof": (((("channels_max", 2), ("channels_min", 2)),
                                 (("channels_max", 6), ("channels_min", 6))),)}),
    # words
    ("chip:6581", {"chip": "6581"}),
    ("chip:2SID", {"chip": "2sid"}),
    ("chip:6581,8580", {"anyof": (((("chip", "6581"),), (("chip", "8580"),)),)}),
    ("has:stil", {"has": "stil"}),
    ("-has:stil", {"negate": ((("has", "stil"),),)}),
    ("dup:yes", {"dup": "yes"}),
    ("dup:No", {"dup": "no"}),
])
def test_parse_forms(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q", ["bpm:fast", "tunes:many", "chip:sid", "has:lyrics", "has:cover",
                               "has:art", "dup:maybe", "channels:stereo", "chip:6581,foo"])
def test_unreadable_values_stay_text(q):
    assert search_predicates(q) == {"query": q}


@pytest.mark.parametrize("q", ["instrument:bass", "label:warp", "comment:ripped"])
def test_dropped_operators_are_free_text(big, q):
    # (dropped 2026-10-23: rare, and their word indexes slowed an index rebuild)
    assert search_predicates(q) == {"query": q}
    assert big.result_ids(**search_predicates(q)) == big.result_ids(query=q)
    key, value = q.split(":")
    field = {"instrument": "instruments"}.get(key, key)
    by_field = {tid for tid, t in big._tracks.items()
                if value in " ".join(t.get(field) or () if key == "instrument"
                                     else [t.get(field) or ""]).lower()}
    assert by_field and big.result_ids(**search_predicates(q)) != by_field


# ── store: semantics against a brute force ───────────────────────────────────

TITLES = ["Song of Myself", "Songs of Myself", "Myself and Song", "A Song of Ice",
          "Live at Wacken", "Alive at Night", "Ghost Love Score"]
ARTISTS = ["Nightwish", "Rob Hubbard", "Live", ""]
INSTR = [["Bass Drum", "snare"], ["bassdrum 1", "Hi-Hat"], ["Kick", "Drum Bass"], None, []]
COMMENTS = ["Ripped by Bob", "unripped demo", "Visit www.ripper.com", ""]
LABELS = ["Warp Records", "Nuclear Blast", "", "Rephlex • Warp"]


def _row(i: int, rnd: random.Random) -> dict:
    fmt = rnd.choice(["SID", "SID", "MP3", "ProTracker", "FLAC"])
    row = {
        "id": f"t{i:05d}", "path": f"/m/{i}.x", "title": rnd.choice(TITLES),
        "artist": rnd.choice(ARTISTS), "album_artist": rnd.choice(ARTISTS),
        "album": rnd.choice(["Imaginaerum", "Live Imagination", ""]),
        "composer": rnd.choice(["", "Tuomas Holopainen"]),
        "game": rnd.choice(["", "", "Commando", "Song of Myself Quest"]),
        "format": fmt, "added_at": i + 1,
        "bpm": rnd.choice([None, 0, 119.4, 119.5, 120.0, 130.49, 130.5, 140.2]),
        "subsongs": rnd.choice([None, 1, 2, 7, 30]),
        "channels": rnd.choice([None, 1, 2, 3, 4, 8]),
        "sid_model": rnd.choice([None, "6581", "8580", "6581/8580"]) if fmt == "SID" else None,
        "instruments": rnd.choice(INSTR),
        "comment": rnd.choice(COMMENTS), "label": rnd.choice(LABELS),
        "cover_art": rnd.choice([None, f"/api/art/t{i:05d}"]),
        "stil": rnd.choice([None, "", "STIL text"]),
        "duplicate_group_id": rnd.choice([None, None, None, f"g{i % 40}", f"solo{i}"]),
    }
    return row


@pytest.fixture(scope="module")
def big():
    rnd = random.Random(20261023)
    s = TrackStore()
    for i in range(2500):
        s.upsert_track(_row(i, rnd))
    return s


def _fold(v: str) -> str:
    v = " ".join((v or "").split()).lower()
    out = "".join(ch for ch in unicodedata.normalize("NFKD", v) if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", out)


def _word_start(text: str, q: str) -> bool:
    return re.search(r"(?<![^\W_])" + re.escape(_fold(q)), _fold(text or "")) is not None


def _words(v: str) -> list[str]:
    return [w for w in re.split(r"[^\w]+|_", (v or "").lower()) if w]


def _has_phrase(t: dict, phrase: str) -> bool:
    terms = _words(phrase)
    fields = [t.get(f) for f in ("title", "artist", "album_artist", "album", "composer",
                                 "scene_group", "game")] + list(t.get("game_aliases") or ())
    for v in fields:
        w = _words(v)
        for i in range(len(w) - len(terms) + 1):
            if w[i:i + len(terms) - 1] == terms[:-1] and w[i + len(terms) - 1].startswith(terms[-1]):
                return True
    return False


def _dups(s):
    from collections import Counter
    c = Counter(t.get("duplicate_group_id") for t in s._tracks.values() if t.get("duplicate_group_id"))
    return {tid for tid, t in s._tracks.items() if c.get(t.get("duplicate_group_id"), 0) >= 2}


def _match(s, t: dict, cond: dict) -> bool:
    (k, v), = cond.items()
    if k == "phrase":
        return _has_phrase(t, v)
    if k == "words":           # every word a prefix of a word somewhere (the free text)
        hay = " ".join(str(t.get(f) or "") for f in ("title", "artist", "album_artist", "album",
                                                     "composer", "game"))
        return all(_word_start(hay, w) for w in v.split())
    if k == "bpm":
        b = t.get("bpm")
        return bool(b) and v[0] <= math.floor(b + 0.5) <= v[1]
    if k == "tunes":
        n = t.get("subsongs") or 1
        return v[0] <= n <= v[1]
    if k == "channels":
        n = t.get("channels")
        return n is not None and v[0] <= n <= v[1]
    if k == "chip":
        m = (t.get("sid_model") or "").split("/")
        if v in ("2sid", "3sid"):
            return t.get("format") == "SID" and t.get("channels") == int(v[0])
        return v in m
    if k == "has":
        return bool({"stil": t.get("stil"), "game": (t.get("game") or "").strip()}[v])
    if k == "dup":
        return (t["id"] in _dups(s)) == (v == "yes")
    if k == "artist":
        return _word_start(t.get("artist") or "", v) or _word_start(t.get("album_artist") or "", v)
    if k == "any":
        return any(_match(s, t, c) for c in v)
    raise AssertionError(k)


def _brute(s, pos, neg=()):
    return {tid for tid, t in s._tracks.items()
            if all(_match(s, t, c) for c in pos) and not any(_match(s, t, c) for c in neg)}


INF = math.inf
CASES = [
    ('"song of myself"', [{"words": "song of myself"}, {"phrase": "song of myself"}], []),
    ('"song of"', [{"words": "song of"}, {"phrase": "song of"}], []),
    ('"song of" ice', [{"words": "song of ice"}, {"phrase": "song of"}], []),
    ('"live at"', [{"words": "live at"}, {"phrase": "live at"}], []),
    ('-"live at"', [], [{"phrase": "live at"}]),
    ('"myself quest"', [{"words": "myself quest"}, {"phrase": "myself quest"}], []),
    ("bpm:120", [{"bpm": (120, 120)}], []),
    ("bpm:120-130", [{"bpm": (120, 130)}], []),
    ("bpm:>130", [{"bpm": (131, INF)}], []),
    ("bpm:<120", [{"bpm": (-INF, 119)}], []),
    ("-bpm:120", [], [{"bpm": (120, 120)}]),
    ("tunes:>1", [{"tunes": (2, INF)}], []),
    ("tunes:1", [{"tunes": (1, 1)}], []),
    ("tunes:2-10", [{"tunes": (2, 10)}], []),
    ("channels:>2", [{"channels": (3, INF)}], []),
    ("channels:4,8", [{"any": [{"channels": (4, 4)}, {"channels": (8, 8)}]}], []),
    ("chip:6581", [{"chip": "6581"}], []),
    ("chip:8580", [{"chip": "8580"}], []),
    ("chip:2sid", [{"chip": "2sid"}], []),
    ("chip:3sid", [{"chip": "3sid"}], []),
    ("-chip:6581", [], [{"chip": "6581"}]),
    ("has:stil", [{"has": "stil"}], []),
    ("has:game", [{"has": "game"}], []),
    ("-has:stil", [], [{"has": "stil"}]),
    ("dup:yes", [{"dup": "yes"}], []),
    ("dup:no", [{"dup": "no"}], []),
    ("bpm:120-130 -has:stil", [{"bpm": (120, 130)}], [{"has": "stil"}]),
    ("chip:6581 tunes:>1", [{"chip": "6581"}, {"tunes": (2, INF)}], []),
    ("dup:yes has:game,stil", [{"dup": "yes"}, {"any": [{"has": "game"}, {"has": "stil"}]}], []),
    ('"song of" -"song of myself"', [{"words": "song of"}, {"phrase": "song of"}],
     [{"phrase": "song of myself"}]),
    ('artist:nightwish "song of"', [{"artist": "nightwish"}, {"words": "song of"},
                                    {"phrase": "song of"}], []),
]


@pytest.mark.parametrize("q,pos,neg", CASES, ids=[c[0] for c in CASES])
def test_store_against_brute_force(big, q, pos, neg):
    want = _brute(big, pos, neg)
    assert 0 < len(want) < len(big._tracks), q
    preds = search_predicates(q)
    assert big.result_ids(**preds) == want
    assert {r["id"] for r in big.filter_tracks(**preds, limit=5000)} == want
    assert set(big.filter_track_ids(**preds)) == want


def test_phrase_differs_from_its_words(big):
    words = big.result_ids(**search_predicates("song of myself"))
    phrase = big.result_ids(**search_predicates('"song of myself"'))
    assert phrase < words                       # ("Myself and Song" has the words only)


# ── A7: album-artist matches first ───────────────────────────────────────────

def _first_store():
    s = TrackStore()
    rows = []
    for i in range(900):
        aa = "Nightwish" if i % 3 == 0 else "Various"
        a = "Nightwish" if i % 2 == 0 else "Other"
        rows.append({"id": f"t{i:04d}", "path": f"/m/{i}", "artist": a, "album_artist": aa,
                     "title": f"t {i}", "format": "MP3", "added_at": i + 1})
    for r in rows:
        s.upsert_track(r)
    return s


def _expected_first(s, q):
    preds = search_predicates(q)
    ids = s.result_ids(**preds)
    aa = {t for t in ids if re.search(r"(?<!\w)nightwish", (s._tracks[t].get("album_artist") or "").lower())}
    key = lambda t: (s._tracks[t]["added_at"], t)                  # noqa: E731
    return sorted(aa, key=key, reverse=True) + sorted(ids - aa, key=key, reverse=True)


def test_artist_first_in_filter_tracks_pages():
    s = _first_store()
    want = _expected_first(s, "artist:nightwish")
    assert want[0] in {t for t, r in s._tracks.items() if r["album_artist"] == "Nightwish"}
    preds = search_predicates("artist:nightwish")
    got = []
    for off in range(0, len(want) + 50, 50):
        got += [r["id"] for r in s.filter_tracks(**preds, limit=50, offset=off)]
    assert got == want
    # a column sort / sort=added is the plain order
    plain = [r["id"] for r in s.filter_tracks(**preds, limit=2000, sort_by="added")]
    assert plain == sorted(want, key=lambda t: (s._tracks[t]["added_at"], t), reverse=True)
    # free text: no grouping (Best match ranks it)
    assert s.album_artist_first(search_predicates("artist:nightwish t")) is None
    # several values group too
    assert s.album_artist_first(search_predicates("artist:nightwish,other")) is not None


def test_artist_first_big_set_walks():
    """A set large enough to be walked (not sorted directly) gives the same
    order, and a deeper page resumes it."""
    s = TrackStore()
    for i in range(9000):
        s.upsert_track({"id": f"t{i:05d}", "path": f"/m/{i}", "artist": "Nightwish",
                        "album_artist": "Nightwish" if i % 4 == 0 else "", "added_at": i + 1})
    preds = search_predicates("artist:nightwish")
    want = _expected_first(s, "artist:nightwish")
    got = []
    for off in range(0, 9000, 1000):
        got += [r["id"] for r in s.filter_tracks(**preds, limit=1000, offset=off)]
    assert got == want


def test_artist_first_in_search_sets(monkeypatch):
    s = _first_store()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    preds = search_predicates("artist:nightwish")
    want = _expected_first(s, "artist:nightwish")
    rset, _ = search_sets.resolve(s, preds, state=search_sets.State(s))
    attr = rset.attr_for(s, None)
    assert attr == search_sets.FIRST
    assert search_sets.page_ids_sync(s, rset, attr, True, 0, 40) == want[:40]
    assert search_sets.complete_sync(s, rset, attr, True) == want
    assert rset.attr_for(s, "added") == rset.default_attr          # (sort=added: plain)
    # every match an album-artist one: the plain order
    only = search_predicates("album_artist:nightwish")
    r2, _ = search_sets.resolve(s, only, state=search_sets.State(s))
    assert r2.attr_for(s, None) == r2.default_attr
    availability.reset_for_tests()


async def test_artist_first_over_http(monkeypatch):
    s = _first_store()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    want = _expected_first(s, "artist:nightwish")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(None)),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/page", params={"q": "artist:nightwish", "limit": 5000})).json()
        assert r["artist_first"] is True and r["ranked"] is False
        assert [t["id"] for t in r["tracks"]] == want
        p = (await c.get("/api/search/page", params={"q": "artist:nightwish", "limit": 50,
                                                     "sort": "added"})).json()
        assert p["artist_first"] is False
        prev = (await c.get("/api/search", params={"q": "artist:nightwish", "limit": 30})).json()
        assert [t["id"] for t in prev] == want[:30]
        pl = (await c.get("/api/search/page", params={"q": "artist:nightwish", "limit": 20,
                                                      "playable": 1})).json()
        assert [t["id"] for t in pl["tracks"]] == want[:20]
        r = (await c.get("/api/search/page", params={"q": "format:mp3", "limit": 5})).json()
        assert r["artist_first"] is False
    assert [t.id for t in await run_search("artist:nightwish", limit=40)] == want[:40]
    availability.reset_for_tests()


# ── indexes follow writes; caches follow writes that move no browse state ────

def test_indexes_follow_writes():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "format": "SID", "sid_model": "6581",
                    "channels": 1, "subsongs": 3, "added_at": 1})
    q = lambda text: s.result_ids(**search_predicates(text))           # noqa: E731
    assert q("chip:6581") == {"a"} and q("tunes:3") == {"a"}
    b, m = s._browse_seq, s._mutation_seq
    s.update_track_fields("a", {"sid_model": "8580", "channels": 2, "subsongs": None})
    assert s._browse_seq == b                                           # (browse-exempt)
    assert q("chip:6581") == set() and q("chip:8580") == {"a"} and q("chip:2sid") == {"a"}
    assert q("tunes:1") == {"a"}
    assert s.verify_indexes()["index_ok"]
    vk = s.volatile_key(search_predicates("has:stil"))
    s.update_track_fields("a", {"stil": "STIL text"})                    # (browse-exempt)
    assert s.volatile_key(search_predicates("has:stil")) != vk and q("has:stil") == {"a"}
    assert s.verify_indexes()["index_ok"]


def test_kept_walk_follows_a_stil_write():
    s = TrackStore()
    for i in range(7000):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{i}", "added_at": i})
    q = search_predicates("-has:stil")
    assert s.filter_tracks(**q, limit=1)[0]["id"] == "t6999"
    b = s._browse_seq
    s.update_track_fields("t6999", {"stil": "x"})
    assert s._browse_seq == b
    assert s.filter_tracks(**q, limit=1)[0]["id"] == "t6998"


def test_bpm_during_a_scan_batch():
    s = TrackStore()
    for i in range(5):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{i}", "bpm": 120.0, "added_at": i + 1})
    s.upsert_track({"id": "z", "path": "/m/z", "bpm": 0.3, "added_at": 9})
    q = lambda text: s.result_ids(**search_predicates(text))           # noqa: E731
    assert q("bpm:<1") == set() and q("bpm:0") == set()             # (rounds to 0: no BPM)
    s.enter_batch_mode()
    try:
        s.delete_track("t1")
        s.update_track_fields("t2", {"bpm": 90.0})
        assert q("bpm:120") == {"t0", "t3", "t4"}
        assert q("bpm:90") == {"t2"}
    finally:
        s.exit_batch_mode()
    assert q("bpm:120") == {"t0", "t3", "t4"}


def test_operator_inside_a_quoted_phrase_is_phrase_text():
    assert search_predicates('"song year:1999 remaster"') == {
        "phrase": ("song year:1999 remaster",), "query": '"song year:1999 remaster"'}
    assert search_predicates('album:"back on track: 2" x') == {"album_match": "back on track: 2",
                                                               "query": "x"}


def test_phrase_and_split_are_kept_per_browse_state():
    s = _first_store()
    p = search_predicates("artist:nightwish")
    s.filter_tracks(**p, limit=10)
    assert len(s._first_split_memo) == 1
    s.filter_tracks(**p, limit=10, offset=10)
    assert len(s._first_split_memo) == 1
    s.upsert_track({"id": "new", "path": "/m/new", "artist": "Nightwish",
                    "album_artist": "Nightwish", "added_at": 99999})
    assert s.filter_tracks(**p, limit=1)[0]["id"] == "new"           # (a browse write: fresh)
    q = search_predicates('"t 1"')
    first = s.result_ids(**q)
    assert s._agg_cache_get("phrase:t 1") is not None
    assert s.result_ids(**q) == first


def test_dup_follows_regrouping():
    s = TrackStore()
    for tid in "abc":
        s.upsert_track({"id": tid, "path": f"/m/{tid}", "added_at": 1})
    q = lambda text: s.result_ids(**search_predicates(text))           # noqa: E731
    assert q("dup:yes") == set() and q("dup:no") == {"a", "b", "c"}
    s.update_track_fields("a", {"duplicate_group_id": "g"})
    assert q("dup:yes") == set()                                        # (a group of one)
    s.update_track_fields("b", {"duplicate_group_id": "g"})
    assert q("dup:yes") == {"a", "b"} and q("dup:no") == {"c"}


# ── every surface agrees ─────────────────────────────────────────────────────

@pytest.fixture
def lib(monkeypatch, big):
    s = copy.deepcopy(big)
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    shuffle_order.clear()
    yield s
    availability.reset_for_tests()
    shuffle_order.clear()


SURFACE = ['"song of"', '-"live at"', "bpm:120-130", "tunes:>1", "chip:6581", "chip:2sid",
           "has:stil", "dup:yes", "channels:4,8", "chip:6581 tunes:>1", "bpm:120-130 -has:stil"]


@pytest.mark.parametrize("q", SURFACE)
async def test_every_surface_agrees(lib, q):
    want = next(_brute(lib, pos, neg) for cq, pos, neg in CASES if cq == q)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(None)),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/page", params={"q": q, "limit": 5000})).json()
        assert r["total"] == len(want) and {t["id"] for t in r["tracks"]} == want
        deal = want - (availability.unplayable(lib) or set())
        total, got = await _all_shuffled(c, q)
        assert total == len(deal) and got == deal
        prev = (await c.get("/api/search", params={"q": q, "limit": 200})).json()
        assert {t["id"] for t in prev} <= want and len(prev) == min(200, len(want))
        quick = (await c.get("/api/search/quick", params={"q": q, "limit": 8})).json()
        assert {t["id"] for t in quick} <= want and len(quick) == min(8, len(want))
        f = (await c.get("/api/search/filter", params={"q": q, "limit": 2000})).json()
        assert {t["id"] for t in f} <= want and len(f) == min(2000, len(want))
    assert {t.id for t in await run_search(q, limit=5000)} == want


def test_subsonic_search3_reads_phrases_and_the_new_operators(env):
    for tid, title in (("p1", "zz song of myself"), ("p2", "zz myself and song"),
                       ("p3", "zz other")):
        env.store.upsert_track(_tr(tid, title=title, artist="Zed", store=env.store, added=9000))
    env.store.update_track_fields("p3", {"bpm": 128.0, "stil": "STIL"})
    ids = lambda q: sorted(s["id"] for s in env.ok("search3.view", query=q)  # noqa: E731
                           ["searchResult3"].get("song", []))
    assert ids('"song of myself"') == ["p1"]
    assert ids("song myself") == ["p1", "p2"]
    assert ids('zz -"song of"') == ["p2", "p3"]
    assert ids("zz bpm:120-130") == ["p3"]
    assert ids("zz has:stil") == ["p3"]
