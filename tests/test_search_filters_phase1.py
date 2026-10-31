# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search filters, phase 1 (internal/SEARCH-FILTERS-PLAN.md).

* A1 ``-`` exclusions: operators and free-text words; hyphenated words,
  ``year:-1990`` and the existing escapes stay as they were.
* A2 ``format:`` by exact name, else extension, else family.
* A3 ``album:`` a word start in the search box (the album page stays exact).
* A4 an unreadable ``year:`` value stays free text.
* B1 ``length:``, B2 ``added:``, B3 ``rating:``, B4 ``plays:`` / ``played:``
  (the signed-in user's plays — never another user's, in results or caches).

Every form parses through the one path (``search_predicates`` →
``_parse_tag_query``), and the search page, the shuffle, ``filter_track_ids``,
``result_ids``, Save search (``run_search``), ``/search/filter`` and Subsonic
``search3`` all agree with a brute force.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.api.search import _parse_advanced_query, run_search, search_predicates
from soniqboom.api.users import optional_listener_uid
from soniqboom.core import availability, search_sets, search_values, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.data import _parse_tag_query, needs_uid, with_uid
from soniqboom.core.store import TrackStore

from test_subsonic_parity import _tr, env  # noqa: F401 — fixture

ROOT = Path(__file__).resolve().parent.parent


def _ts(y, m=1, d=1) -> float:
    return datetime(y, m, d).timestamp()


# ── parse: every form of every new key ───────────────────────────────────────

@pytest.mark.parametrize("q,want", [
    # B1 length: — whole seconds, the forms of year:
    ("length:90", {"length_min": 90, "length_max": 90}),
    ("length:90s", {"length_min": 90, "length_max": 90}),
    ("length:5m", {"length_min": 300, "length_max": 300}),
    ("length:1h", {"length_min": 3600, "length_max": 3600}),
    ("length:1:30", {"length_min": 90, "length_max": 90}),
    ("length:1:02:03", {"length_min": 3723, "length_max": 3723}),
    ("length:>5:00", {"length_min": 301}),
    ("length:<30s", {"length_max": 29}),
    ("length:>=1h", {"length_min": 3600}),
    ("length:<=2m", {"length_max": 120}),
    ("length:3:00-4:00", {"length_min": 180, "length_max": 240}),
    ("length:3:00-", {"length_min": 180}),
    ("length:-4:00", {"length_max": 240}),
    ("length:3:00 - 4:00", {"length_min": 180, "length_max": 240}),
    ("length: > 5:00", {"length_min": 301}),
    ("Length:<0:30", {"length_max": 29}),
    # B2 added: — canonical spec strings (resolved by the store)
    ("added:<30d", {"added": "<30d"}),
    ("added:>2w", {"added": ">2w"}),
    ("added:30d", {"added": "30d"}),
    ("added:<6m", {"added": "<6m"}),
    ("added:<1y", {"added": "<1y"}),
    ("added:<12h", {"added": "<12h"}),
    ("added:2026", {"added": "2026"}),
    ("added:2026-10", {"added": "2026-10"}),
    ("added:2026-10-23", {"added": "2026-10-23"}),
    ("added:2026-01-2026-06", {"added": "2026-01-2026-06"}),
    ("added:2026-", {"added": "2026-"}),
    ("added:-2025", {"added": "-2025"}),
    ("added:>2026-06-01", {"added": ">2026-06-01"}),
    ("added:<=2026-06", {"added": "<=2026-06"}),
    ("added: <30d", {"added": "<30d"}),
    ("ADDED:<30D", {"added": "<30d"}),
    # B3 rating:
    ("rating:4", {"rating_min": 4, "rating_max": 4}),
    ("rating:>=4", {"rating_min": 4}),
    ("rating:1-3", {"rating_min": 1, "rating_max": 3}),
    ("rating:0", {"rating_min": 0, "rating_max": 0}),
    ("rating:unrated", {"rating_min": 0, "rating_max": 0}),
    ("rating:Unrated", {"rating_min": 0, "rating_max": 0}),
    ("rating:<3", {"rating_max": 2}),
    # B4 plays: / played: (no user given → no uid in the predicates)
    ("plays:0", {"plays_min": 0, "plays_max": 0}),
    ("plays:>10", {"plays_min": 11}),
    ("plays:1-3", {"plays_min": 1, "plays_max": 3}),
    ("played:<7d", {"played": "<7d"}),
    ("played:2026-10", {"played": "2026-10"}),
    # A2 format: (the value goes to the store, which resolves it)
    ("format:mod", {"format_": "MOD"}),
    ("type:chiptune", {"format_": "CHIPTUNE"}),
    # A3 album: — a word start
    ("album:imagin", {"album_match": "imagin"}),
    ('album:"once more"', {"album_match": "once more"}),
    # combined
    ("format:sid year:1985-1989 -genre:game",
     {"format_": "SID", "year_min": 1985, "year_max": 1989,
      "negate": ((("genre_match", "game"),),)}),
    ("rating:>=4 plays:0 length:<0:30 added:<7d",
     {"rating_min": 4, "plays_min": 0, "plays_max": 0, "length_max": 29, "added": "<7d"}),
])
def test_parse_forms(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q,want", [
    # A4 / unreadable values stay free text — never "no filter"
    ("year:abc", {"query": "year:abc"}),
    ("year:19x", {"query": "year:19x"}),
    ("year:-", {"query": "year:-"}),
    ("year:2.5", {"query": "year:2.5"}),
    ("length:long", {"query": "length:long"}),
    ("length:1:75", {"query": "length:1:75"}),
    ("length:5x", {"query": "length:5x"}),
    ("length:1.5", {"query": "length:1.5"}),
    ("added:yesterday", {"query": "added:yesterday"}),
    ("added:2026-13", {"query": "added:2026-13"}),
    ("added:2026-02-30", {"query": "added:2026-02-30"}),
    ("added:30x", {"query": "added:30x"}),
    ("rating:great", {"query": "rating:great"}),
    ("plays:many", {"query": "plays:many"}),
    ("played:never", {"query": "played:never"}),
    ("artist:x year:abc", {"artist_match": "x", "query": "year:abc"}),
])
def test_unreadable_values_stay_text(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q,want", [
    ("-format:mp3", {"negate": ((("format_", "MP3"),),)}),
    ("-Format:MP3", {"negate": ((("format_", "MP3"),),)}),
    ("-genre:podcast", {"negate": ((("genre_match", "podcast"),),)}),
    ("-live", {"negate": ((("query", "live"),),)}),
    ("nightwish -live", {"query": "nightwish", "negate": ((("query", "live"),),)}),
    ("-live -demo", {"negate": ((("query", "live"),), (("query", "demo"),))}),
    ("-format:mp3 -format:sid",
     {"negate": ((("format_", "MP3"),), (("format_", "SID"),))}),
    ("format:flac -format:mp3", {"format_": "FLAC", "negate": ((("format_", "MP3"),),)}),
    ('-album:"live at"', {"negate": ((("album_match", "live at"),),)}),
    ("-year:1990-2000", {"negate": ((("year_max", 2000), ("year_min", 1990)),)}),
    ("-year:-1990", {"negate": ((("year_max", 1990),),)}),
    ("-rating:0", {"negate": ((("rating_max", 0), ("rating_min", 0)),)}),
    ("-length:<30s", {"negate": ((("length_max", 29),),)}),
    ("-added:<7d", {"negate": ((("added", "<7d"),),)}),
    # an unreadable value: the whole term is an excluded word
    ("-year:abc", {"negate": ((("query", "year:abc"),),)}),
    ("-remix:2020", {"negate": ((("query", "remix:2020"),),)}),
    # a "-" inside a word or a value is no exclusion
    ("a-ha", {"query": "a-ha"}),
    ("x-ray -live", {"query": "x-ray", "negate": ((("query", "live"),),)}),
    ("year:-1990", {"year_max": 1990}),
    ("year:1990 -2000", {"year_min": 1990, "year_max": 2000}),     # (a range, as before)
    ("track:1 -5", {"track_min": 1, "track_max": 5}),
    ("a - b", {"query": "a - b"}),
    ("--x", {"query": "--x"}),
    ("-", {"query": "-"}),
    ("x-format:mp3", {"format_": "MP3", "query": "x-"}),         # (no exclusion)
])
def test_exclusions_parse(q, want):
    assert search_predicates(q) == want


def test_exclusion_escapes_round_trip():
    for value in ("a{b}", "}", "\\", "x:y", "Pokémon {Gold}"):
        q = _parse_advanced_query(f'-artist:"{value}"')
        assert q.startswith("-@artist_match:{")
        assert _parse_tag_query(q) == {"negate": ((("artist_match", value),),)}
    # a free-text word with the escaped specials
    assert search_predicates("-x:y/z") == {"negate": ((("query", "x:y/z"),),)}
    # an escaped "-" in free text is never an exclusion
    assert _parse_tag_query(r"a\-b \-c") == {"query": "a-b -c"}
    # (a raw tag term after an escaped "-" is a plain term, not an exclusion)
    assert _parse_tag_query(r"@artist_tag:{x} \-@format:{y}") == {"artist": "x", "format_": "y",
                                                                  "query": "-"}


def test_plain_text_is_unchanged():
    assert _parse_advanced_query("love song") is None
    assert _parse_advanced_query("a-ha take on me") is None
    assert search_predicates("a-ha take on me") == {"query": "a-ha take on me"}


def test_uid_only_with_plays_keys():
    assert search_predicates("plays:0", uid="u1") == {"plays_min": 0, "plays_max": 0, "uid": "u1"}
    assert search_predicates("played:<7d", uid="") == {"played": "<7d", "uid": ""}
    assert search_predicates("-plays:0", uid="u1")["uid"] == "u1"
    assert "uid" not in search_predicates("rating:5 format:sid", uid="u1")
    assert "uid" not in search_predicates("plays:0", uid=None)
    assert needs_uid({"negate": ((("played", "<1d"),),)})
    assert not needs_uid({"negate": ((("query", "x"),),)})
    assert with_uid({"query": "x"}, "u1") == {"query": "x"}


# ── values ───────────────────────────────────────────────────────────────────

def test_when_range_relative_and_periods():
    now = _ts(2026, 10, 23) + 12 * 3600
    assert search_values.when_range("<30d", now) == (now - 30 * 86400, None)
    assert search_values.when_range("30d", now) == (now - 30 * 86400, None)
    assert search_values.when_range("<=2w", now) == (now - 14 * 86400, None)
    assert search_values.when_range(">2w", now) == (None, now - 14 * 86400)
    assert search_values.when_range("<6m", now) == (now - 180 * 86400, None)
    assert search_values.when_range("<1y", now) == (now - 365 * 86400, None)
    assert search_values.when_range("<12h", now) == (now - 12 * 3600, None)
    assert search_values.when_range("2026", now) == (_ts(2026), _ts(2027))
    assert search_values.when_range("2026-10", now) == (_ts(2026, 10), _ts(2026, 11))
    assert search_values.when_range("2026-12", now) == (_ts(2026, 12), _ts(2027))
    assert search_values.when_range("2026-10-23", now) == (_ts(2026, 10, 23), _ts(2026, 10, 24))
    assert search_values.when_range("2026-01-2026-06", now) == (_ts(2026), _ts(2026, 7))
    assert search_values.when_range("2026-", now) == (_ts(2026), None)
    assert search_values.when_range("-2025", now) == (None, _ts(2026))
    assert search_values.when_range(">2026-06-01", now) == (_ts(2026, 6, 2), None)
    assert search_values.when_range(">=2026-06-01", now) == (_ts(2026, 6, 1), None)
    assert search_values.when_range("<2026-06-01", now) == (None, _ts(2026, 6, 1))
    assert search_values.when_range("<=2026-06", now) == (None, _ts(2026, 7))
    for bad in ("", "x", "2026-13", "2026-02-30", "1800", "<", "30", "-", "2026--1"):
        assert search_values.when_range(bad, now) is None, bad
    assert search_values.is_relative("<30d") and not search_values.is_relative("2026")


def test_length_value_forms():
    lv = search_values.length_value
    assert [lv(v) for v in ("90", "90s", "5m", "1h", "1:30", "1:02:03", ">5:00",
                            "<=2m", "3:00-4:00", "3:00-", "-4:00")] == \
        ["90", "90", "300", "3600", "90", "3723", ">300", "<=120", "180-240", "180-", "-240"]
    assert [lv(v) for v in ("", "-", "x", "1:75", "1:00:60", "5x", "1.5", "3:00-4:xx")] == [None] * 8


def test_format_names_for_ext():
    assert search_values.format_names_for_ext("mod") == {"protracker"}
    assert search_values.format_names_for_ext(".XM") == {"fasttracker 2"}
    assert search_values.format_names_for_ext("m4a") == {"m4a", "aac", "alac"}
    assert search_values.format_names_for_ext("dsf") == {"dsd", "dsf (dreamcast)"}
    assert search_values.format_names_for_ext("imf") == {"imago orpheus", "adlib imf"}
    assert search_values.format_names_for_ext("nosuchext") is None


# ── store: semantics against a brute force ───────────────────────────────────

NOW = time.time()
FORMATS = ["MP3", "FLAC", "ProTracker", "FastTracker 2", "SID", "AAC", "ALAC", "DSD64",
           "DSD", "MIDI", "Imago Orpheus", "AdLib IMF", "Ogg Vorbis"]
TITLES = ["Live at Wacken", "intro", "Love Song", "a-ha cover", "Ghost Love Score", "x-ray"]
ALBUMS = ["Imaginaerum", "Once More with Feeling", "Live Imagination", "Dark Passion Play",
          "", "the imagine sessions"]


def _row(i: int, rnd: random.Random) -> dict:
    days = rnd.choice([0.4, 3.5, 10.5, 40.5, 200.5, 400.5, None])
    return {
        "id": f"t{i:05d}", "path": f"/m/{i}.x",
        "title": rnd.choice(TITLES), "artist": rnd.choice(["Nightwish", "Unknown", "Rob Hubbard"]),
        "album": rnd.choice(ALBUMS), "format": rnd.choice(FORMATS),
        "genre": [rnd.choice(["Metal", "Game", "Podcast"])],
        "duration": rnd.choice([0, 0.0, 29.9, 30.0, 90.4, 179.99, 180.0, 240.7, 241.0, 300.5,
                                301.0, 3600.0]),
        "added_at": 0 if days is None else int(NOW - days * 86400),
        "year": rnd.choice([None, 1985, 1989, 1990, 2011]),
    }


@pytest.fixture(scope="module")
def big():
    rnd = random.Random(20261023)
    s = TrackStore()
    for i in range(3000):
        s.upsert_track(_row(i, rnd))
    for tid in rnd.sample(sorted(s._tracks), 900):
        s.set_rating(tid, rnd.randint(1, 5))
    for uid, n in (("u1", 700), ("u2", 500), ("", 50)):
        for tid in rnd.sample(sorted(s._tracks), n):
            for _ in range(rnd.choice([1, 1, 2, 12])):
                s.record_play(tid, at=int(NOW - rnd.choice([0.5, 5.5, 20.5, 100.5]) * 86400),
                              uid=uid)
    return s


def _fam(fmt):
    from soniqboom.core.retro import coarse_family
    return coarse_family(fmt)


def _floor_len(t):
    d = t.get("duration")
    return math.floor(d) if isinstance(d, (int, float)) and d and d > 0 else None


def _words(t) -> str:
    return " ".join(str(t.get(k) or "") for k in ("title", "artist", "album")).lower()


def _match(s: TrackStore, t: dict, cond: dict, uid) -> bool:
    """One condition, brute force (the documented meaning, not the store's code)."""
    k, v = next(iter(cond.items()))
    rating = s._ratings.get(t["id"], 0)
    st = (s._play_stats.get(uid) or {}).get(t["id"], {}) if uid is not None else {}
    if k == "len":
        n = _floor_len(t)
        return n is not None and v[0] <= n <= v[1]
    if k == "added":
        a = t.get("added_at") or 0
        return a > 0 and v[0] <= a < v[1]
    if k == "rating":
        return v[0] <= rating <= v[1]
    if k == "plays":
        return v[0] <= st.get("count", 0) <= v[1]
    if k == "played":
        lp = st.get("last_played") or 0
        return lp > 0 and v[0] <= lp < v[1]
    if k == "fmt":
        return t.get("format") in v
    if k == "family":
        return _fam(t.get("format")) == v
    if k == "genre":
        return v in [g.lower() for g in t.get("genre") or ()]
    if k == "word":
        return re.search(r"(?<![a-z0-9])" + re.escape(v), _words(t)) is not None
    if k == "album":
        return re.search(r"(?<![a-z0-9])" + re.escape(v), (t.get("album") or "").lower()) is not None
    if k == "artist":
        return re.search(r"(?<![a-z0-9])" + re.escape(v), (t.get("artist") or "").lower()) is not None
    if k == "year":
        y = t.get("year")
        return isinstance(y, int) and v[0] <= y <= v[1]
    raise AssertionError(k)


def _brute(s: TrackStore, pos: list, neg: list = (), uid=None) -> set:
    return {tid for tid, t in s._tracks.items()
            if all(_match(s, t, c, uid) for c in pos)
            and not any(_match(s, t, c, uid) for c in neg)}


INF = math.inf
D = 86400
CASES = [
    ("length:>5:00", [{"len": (301, INF)}], []),
    ("length:<30s", [{"len": (-INF, 29)}], []),
    ("length:3:00", [{"len": (180, 180)}], []),
    ("length:3:00-4:00", [{"len": (180, 240)}], []),
    ("length:>=1h", [{"len": (3600, INF)}], []),
    ("added:<7d", [{"added": (NOW - 7 * D, INF)}], []),
    ("added:>30d", [{"added": (-INF, NOW - 30 * D)}], []),
    ("added:<1y -added:<7d", [{"added": (NOW - 365 * D, INF)}], [{"added": (NOW - 7 * D, INF)}]),
    ("rating:>=4", [{"rating": (4, INF)}], []),
    ("rating:0", [{"rating": (0, 0)}], []),
    ("rating:unrated", [{"rating": (0, 0)}], []),
    ("rating:<3", [{"rating": (-INF, 2)}], []),
    ("rating:1-2", [{"rating": (1, 2)}], []),
    ("-rating:0", [], [{"rating": (0, 0)}]),
    ("format:protracker", [{"fmt": {"ProTracker"}}], []),
    ("format:mod", [{"fmt": {"ProTracker"}}], []),
    ("format:xm", [{"fmt": {"FastTracker 2"}}], []),
    ("format:m4a", [{"fmt": {"AAC", "ALAC"}}], []),
    ("format:dsf", [{"fmt": {"DSD", "DSD64"}}], []),
    ("format:imf", [{"fmt": {"Imago Orpheus", "AdLib IMF"}}], []),
    ("format:ogg", [{"fmt": {"Ogg Vorbis"}}], []),
    ("format:tracker", [{"family": "trackers"}], []),
    ("format:trackers", [{"family": "trackers"}], []),
    ("format:chiptune", [{"family": "chiptune"}], []),
    ("format:lossless", [{"family": "lossless"}], []),
    ("format:lossy", [{"family": "lossy"}], []),
    ("format:other", [{"family": "other"}], []),
    ("format:nosuchthing", [{"fmt": set()}], []),
    ("album:imagin", [{"album": "imagin"}], []),
    ('album:"once more"', [{"album": "once more"}], []),
    ("album:imagin -live", [{"album": "imagin"}], [{"word": "live"}]),
    ("-live", [], [{"word": "live"}]),
    ("nightwish -live", [{"word": "nightwish"}], [{"word": "live"}]),
    ("-format:mp3 -format:sid", [], [{"fmt": {"MP3"}}, {"fmt": {"SID"}}]),
    ("format:sid year:1985-1989 -genre:game",
     [{"fmt": {"SID"}}, {"year": (1985, 1989)}], [{"genre": "game"}]),
    ("format:mod -artist:unknown", [{"fmt": {"ProTracker"}}], [{"artist": "unknown"}]),
    ("length:<0:30 format:chiptune", [{"len": (-INF, 29)}, {"family": "chiptune"}], []),
    ("x-ray", [{"word": "x-ray"}], []),
    ("-zzzz", [], []),
]


@pytest.mark.parametrize("q,pos,neg", CASES, ids=[c[0] for c in CASES])
def test_store_against_brute_force(big, q, pos, neg):
    want = _brute(big, pos, neg)
    preds = search_predicates(q)
    got_page = {r["id"] for r in big.filter_tracks(**preds, limit=5000)}
    assert got_page == want
    assert set(big.filter_track_ids(**preds)) == want
    assert big.result_ids(**preds) == want
    # paging: two pages of 50 are disjoint, ordered and part of the set
    p1 = [r["id"] for r in big.filter_tracks(**preds, limit=50, offset=0)]
    p2 = [r["id"] for r in big.filter_tracks(**preds, limit=50, offset=50)]
    assert not set(p1) & set(p2) and set(p1 + p2) <= want
    assert len(p1) == min(50, len(want))


PLAY_CASES = [
    ("plays:0", [{"plays": (0, 0)}], []),
    ("plays:>10", [{"plays": (11, INF)}], []),
    ("plays:1-2", [{"plays": (1, 2)}], []),
    ("played:<7d", [{"played": (NOW - 7 * D, INF)}], []),
    ("played:>30d", [{"played": (-INF, NOW - 30 * D)}], []),
    ("-plays:0", [], [{"plays": (0, 0)}]),
    ("rating:>=4 plays:0", [{"rating": (4, INF)}, {"plays": (0, 0)}], []),
    ("format:tracker played:<30d", [{"family": "trackers"}, {"played": (NOW - 30 * D, INF)}], []),
]


@pytest.mark.parametrize("uid", ["u1", "u2", "", None])
@pytest.mark.parametrize("q,pos,neg", PLAY_CASES, ids=[c[0] for c in PLAY_CASES])
def test_plays_per_user_against_brute_force(big, q, pos, neg, uid):
    want = _brute(big, pos, neg, uid=uid)
    preds = search_predicates(q, uid=uid)
    assert {r["id"] for r in big.filter_tracks(**preds, limit=5000)} == want
    assert set(big.filter_track_ids(**preds)) == want
    assert big.result_ids(**preds) == want


def test_users_see_different_plays(big):
    a = big.result_ids(**search_predicates("plays:0", uid="u1"))
    b = big.result_ids(**search_predicates("plays:0", uid="u2"))
    assert a != b
    # no user: every count is 0, nothing was played
    assert big.result_ids(**search_predicates("plays:0")) == set(big._tracks)
    assert big.result_ids(**search_predicates("played:<1y")) == set()


def test_unknown_length_and_added_never_match(big):
    zero = {tid for tid, t in big._tracks.items() if not _floor_len(t)}
    assert zero and not zero & big.result_ids(**search_predicates("length:0-99999"))
    never = {tid for tid, t in big._tracks.items() if not t.get("added_at")}
    assert never and not never & big.result_ids(**search_predicates("added:-9998"))


def test_format_exact_name_wins():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a.xm", "format": "FastTracker 2", "added_at": 1})
    s.upsert_track({"id": "b", "path": "/m/b.x", "format": "XM", "added_at": 2})
    s.upsert_track({"id": "c", "path": "/m/c.sid", "format": "SID", "added_at": 3})
    assert s.result_ids(**search_predicates("format:xm")) == {"b"}          # the name, not .xm
    assert s.result_ids(**search_predicates("format:sid")) == {"c"}
    assert s.result_ids(**search_predicates('format:"fasttracker 2"')) == {"a"}
    # the exact name is the live index set (as before); an alias builds a new one
    assert s._format_ids("sid") == (s._tag_format["sid"], True)
    assert s._format_ids("trackers")[1] is False


def test_album_page_stays_exact():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "album": "Imaginaerum", "added_at": 1})
    s.upsert_track({"id": "b", "path": "/m/b", "album": "Imaginaerum (Live)", "added_at": 2})
    assert s.result_ids(album="Imaginaerum") == {"a"}                      # the page
    assert s.result_ids(**search_predicates("album:imaginaerum")) == {"a", "b"}
    assert s.result_ids(**search_predicates('album:"imaginaerum (live)"')) == {"b"}
    # /search/filter?album= (the drill-down) stays exact
    assert _parse_tag_query(r"@album_tag:{Imaginaerum}") == {"album": "Imaginaerum"}


def test_invalid_year_is_text_not_everything():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "title": "year abc", "year": 1990, "added_at": 1})
    s.upsert_track({"id": "b", "path": "/m/b", "title": "other", "year": 1990, "added_at": 2})
    assert s.result_ids(**search_predicates("year:abc")) == {"a"}


# ── caches: per user and per rating / play / clock ───────────────────────────

def test_walk_cache_follows_plays_and_ratings():
    """Large candidate sets keep their walk per predicates: a play or a
    rating must not serve the walk made before it, nor one user's to another."""
    s = TrackStore()
    for i in range(7000):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{i}", "added_at": i})
    q = dict(plays_min=0, plays_max=0)
    first = s.filter_tracks(**q, uid="u1", limit=50)
    assert first[0]["id"] == "t6999"
    s.record_play("t6999", uid="u1")
    again = s.filter_tracks(**q, uid="u1", limit=50)
    assert again[0]["id"] == "t6998"
    assert s.filter_tracks(**q, uid="u2", limit=50)[0]["id"] == "t6999"   # u2 never played it
    r = dict(rating_min=0, rating_max=0)
    assert s.filter_tracks(**r, limit=1)[0]["id"] == "t6999"
    s.set_rating("t6999", 5)
    assert s.filter_tracks(**r, limit=1)[0]["id"] == "t6998"
    assert s.volatile_key(dict(q, uid="u1"))[1] == s._play_seq
    assert s.volatile_key({"query": "x"}) == (None,) * 6


def test_search_set_keys_and_freshness(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    for i in range(30):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{i}", "added_at": i + 1})
    st = search_sets.State(s)
    p1 = search_predicates("plays:0", uid="u1")
    p2 = search_predicates("plays:0", uid="u2")
    assert search_sets.search_key(p1, False) != search_sets.search_key(p2, False)
    r1, _ = search_sets.resolve(s, p1, state=st)
    r2, _ = search_sets.resolve(s, p2, state=st)
    assert r1 is not r2
    # u2 naming u1's handle gets its own set (another search's handle)
    r2b, renewed = search_sets.resolve(s, p2, rs=r1.rs, state=st)
    assert r2b is r2 and renewed
    s.record_play("t5", uid="u1")
    # the handle keeps its snapshot (the list / queue do not shift) …
    same, renewed = search_sets.resolve(s, p1, rs=r1.rs, state=search_sets.State(s))
    assert same is r1 and not renewed and "t5" in same.ids
    # … a new search sees the play
    fresh, _ = search_sets.resolve(s, p1, state=search_sets.State(s))
    assert fresh is not r1 and "t5" not in fresh.ids
    # u2's set was made before the play too: still the same (u2 didn't play)
    # — but its key's dyn is the play clock, so a new search rebuilds it
    r2c, _ = search_sets.resolve(s, p2, state=search_sets.State(s))
    assert r2c.ids == r2.ids
    availability.reset_for_tests()


def test_relative_time_freshness(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    t0 = 1_800_000_000.0
    clock = [t0]
    monkeypatch.setattr(store_mod.time, "time", lambda: clock[0])
    s.upsert_track({"id": "new", "path": "/m/n", "added_at": int(t0 - 1800)})
    s.upsert_track({"id": "old", "path": "/m/o", "added_at": int(t0 - 30 * 86400)})
    p = search_predicates("added:<1h")
    r1, _ = search_sets.resolve(s, p, state=search_sets.State(s))
    assert r1.ids == {"new"}
    clock[0] = t0 + 7200                                  # two hours later
    kept, renewed = search_sets.resolve(s, p, rs=r1.rs, state=search_sets.State(s))
    assert kept is r1 and not renewed                     # the open list keeps paging
    fresh, _ = search_sets.resolve(s, p, state=search_sets.State(s))
    assert fresh.ids == set()                             # a new search: now
    assert s.result_ids(**p) == set()
    availability.reset_for_tests()


# ── every surface agrees (HTTP) ──────────────────────────────────────────────

def _app(uid=None) -> FastAPI:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library):
        app.include_router(mod.router, prefix="/api")
    app.dependency_overrides[optional_listener_uid] = lambda: uid
    return app


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


SURFACE_CASES = [
    ("format:sid year:1985-1989 -genre:game", None),
    ("added:<7d -format:mp3", None),
    ("rating:>=4 plays:0", "u1"),
    ("rating:>=4 plays:0", "u2"),
    ("length:<0:30 format:chiptune", None),
    ("album:imagin -live", None),
    ("format:tracker played:<30d", "u1"),
    ("format:mod -artist:unknown", None),
    ("-format:mp3", None),
    ("plays:>10", "u2"),
]


async def _all_shuffled(c, q: str) -> tuple[int, set]:
    sh = (await c.get("/api/tracks/shuffled", params={"q": q, "seed": 42, "limit": 500})).json()
    got = {t["id"] for t in sh["tracks"]}
    off = 500
    while off < sh["total"]:
        got |= {t["id"] for t in (await c.get(
            "/api/tracks/shuffled",
            params={"q": q, "seed": 42, "limit": 500, "offset": off})).json()["tracks"]}
        off += 500
    return sh["total"], got


@pytest.mark.parametrize("q,uid", SURFACE_CASES, ids=[f"{q}|{u}" for q, u in SURFACE_CASES])
async def test_every_surface_agrees(lib, q, uid):
    preds = search_predicates(q, uid=uid)
    # (brute force through the documented meaning: CASES / PLAY_CASES' helpers)
    want = _brute_any(lib, q, uid)
    assert want, q
    assert lib.result_ids(**preds) == want
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(uid)),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/page", params={"q": q, "limit": 5000})).json()
        assert r["total"] == len(want)
        assert {t["id"] for t in r["tracks"]} == want
        total, got = await _all_shuffled(c, q)
        assert total == len(want) and got == want
        prev = (await c.get("/api/search", params={"q": q, "limit": 200})).json()
        assert {t["id"] for t in prev} <= want and len(prev) == min(200, len(want))
        quick = (await c.get("/api/search/quick", params={"q": q, "limit": 8})).json()
        assert {t["id"] for t in quick} <= want and len(quick) == min(8, len(want))
    assert set(lib.filter_track_ids(**preds)) == want
    assert {t.id for t in await run_search(q, limit=5000, uid=uid)} == want


def _brute_any(s, q, uid):
    for cq, pos, neg in CASES + PLAY_CASES:
        if cq == q:
            return _brute(s, pos, neg, uid=uid)
    extra = {
        "added:<7d -format:mp3": ([{"added": (NOW - 7 * D, INF)}], [{"fmt": {"MP3"}}]),
        "-format:mp3": ([], [{"fmt": {"MP3"}}]),
    }
    pos, neg = extra[q]
    return _brute(s, pos, neg, uid=uid)


async def test_users_never_share_search_results(lib):
    q = "plays:0"
    want1 = _brute(lib, [{"plays": (0, 0)}], uid="u1")
    want2 = _brute(lib, [{"plays": (0, 0)}], uid="u2")
    assert want1 != want2
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app("u1")),
                                 base_url="http://t") as c1, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=_app("u2")),
                              base_url="http://t") as c2:
        a = (await c1.get("/api/search/page", params={"q": q, "limit": 5000})).json()
        b = (await c2.get("/api/search/page", params={"q": q, "limit": 5000})).json()
        assert {t["id"] for t in a["tracks"]} == want1
        assert {t["id"] for t in b["tracks"]} == want2
        # u2 using u1's handle still gets u2's own result
        b2 = (await c2.get("/api/search/page",
                           params={"q": q, "rs": a["rs"], "limit": 5000})).json()
        assert {t["id"] for t in b2["tracks"]} == want2 and b2["rs"] != a["rs"]
        # the shuffle order of one seed is per user too
        t1, g1 = await _all_shuffled(c1, q)
        t2, g2 = await _all_shuffled(c2, q)
        assert (t1, g1) == (len(want1), want1) and (t2, g2) == (len(want2), want2)
    # anonymous (accounts exist, nobody signed in): no plays at all
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(None)),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/page", params={"q": "played:<1y", "limit": 10})).json()
        assert r["total"] == 0


async def test_structured_filter_and_shuffle_params(lib):
    want = _brute(lib, [{"len": (180, 240)}, {"rating": (4, INF)}, {"plays": (0, 0)},
                        {"added": (NOW - 365 * D, INF)}], uid="u1")
    params = {"length_min": 180, "length_max": 240, "rating_min": 4, "plays_min": 0,
              "plays_max": 0, "added": "<1y"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app("u1")),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/filter", params=dict(params, limit=2000))).json()
        assert {t["id"] for t in r} == want
        sh = (await c.get("/api/tracks/shuffled",
                          params=dict(params, seed=1, limit=500))).json()
        assert sh["total"] == len(want) and {t["id"] for t in sh["tracks"]} == want
        pl = (await c.get("/api/search/filter",
                          params={"played": "<7d", "limit": 2000})).json()
        assert {t["id"] for t in pl} == _brute(lib, [{"played": (NOW - 7 * D, INF)}], uid="u1")


def test_subsonic_search3_plays_are_the_authenticated_users(env):
    now = int(time.time())
    for tid in ("p1", "p2", "p3"):
        env.store.upsert_track(_tr(tid, title=f"zz {tid}", artist="Zed", store=env.store,
                                   added=9000))
    env.store.record_play("p1", at=now, uid="u1")
    env.store.record_play("p2", at=now, uid="u2")
    ids = lambda q: sorted(s["id"] for s in env.ok("search3.view", query=q)  # noqa: E731
                           ["searchResult3"].get("song", []))
    assert ids("zz plays:0") == ["p2", "p3"]
    assert ids("zz played:<1d") == ["p1"]
    assert ids("zz -p1") == ["p2", "p3"]
    assert ids("zz -format:mp3") == []
    env.user = type(env.user)(id="u2", username="bob", role="edit", enabled=True)
    assert ids("zz plays:0") == ["p1", "p3"]
    assert ids("zz played:<1d") == ["p2"]


def test_frontend_knows_every_operator():
    src = (ROOT / "soniqboom/frontend/js/search.js").read_text()
    body = re.search(r"const _SUPPORTED_OPS = new Set\(\[(.*?)\]\);", src, re.S).group(1)
    ops = set(re.findall(r"'([a-z_]+)'", body))
    assert ops == set(search._OPERATORS)


# ── review round: edge cases ─────────────────────────────────────────────────

@pytest.mark.parametrize("q,want", [
    ("format:mp3 -format:mp3", {"format_": "MP3", "negate": ((("format_", "MP3"),),)}),
    # (phase 3, A8: a quoted run of two or more words is a phrase)
    ('-"live at" nightwish', {"query": "nightwish", "negate": ((("phrase", ("live at",)),),)}),
    ('"foo -bar"', {"query": '"foo -bar"', "phrase": ("foo -bar",)}),   # (no exclusion inside)
    ("-!!!", {"query": "-!!!"}),                        # nothing to search: no exclusion
    ("Year: 1990 - 2000", {"year_min": 1990, "year_max": 2000}),
    ("YEAR:1990 - 2000", {"year_min": 1990, "year_max": 2000}),
    ("Length: >5:00", {"length_min": 301}),
    ("length:5m - 6m", {"length_min": 300, "length_max": 360}),
    ("length:90s - 2m", {"length_min": 90, "length_max": 120}),
    ("multi-track: 2", {"query": "multi-track: 2"}),     # (not an operator mid-word)
    ("-year: 1990", {"negate": ((("year_max", 1990), ("year_min", 1990)),)}),
])
def test_review_edge_forms(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q", [
    "year:" + "9" * 5000, "track:" + "9" * 5000, "rating:" + "9" * 20, "plays:>" + "9" * 5000,
    "added:<" + "9" * 400 + "d", "played:<" + "9" * 5000 + "d",
    "length:" + "9" * 5000, "length:<" + "9" * 400 + "m",
])
def test_huge_numbers_stay_text(q):
    preds = search_predicates(q)                         # (no OverflowError / ValueError)
    assert preds == {"query": q}


def test_format_dsd_names_every_tier():
    s = TrackStore()
    for i, f in enumerate(("DSD", "DSD64", "DSD128", "DSF (Dreamcast)", "FLAC")):
        s.upsert_track({"id": f"d{i}", "path": f"/m/{i}", "format": f, "added_at": i + 1})
    assert s.result_ids(**search_predicates("format:dsd")) == {"d0", "d1", "d2"}
    assert s.result_ids(**search_predicates("format:dsd64")) == {"d1"}
    assert s.result_ids(**search_predicates("format:dsf")) == {"d0", "d1", "d2", "d3"}


def test_length_results_follow_a_duration_backfill(monkeypatch):
    """A first-play length backfill moves only ``_duration_seq``: the kept
    walk and a new search must still see the new length."""
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    for i in range(7000):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{i}", "duration": 60.0, "added_at": i})
    q = search_predicates("length:<1:50")
    assert s.filter_tracks(**q, limit=1)[0]["id"] == "t6999"
    r1, _ = search_sets.resolve(s, q, state=search_sets.State(s))
    b, d = s._browse_seq, s._duration_seq
    s.update_track_fields("t6999", {"duration": 600.0})
    assert s._browse_seq == b and s._duration_seq != d      # (the backfill's shape)
    assert s.filter_tracks(**q, limit=1)[0]["id"] == "t6998"
    fresh, _ = search_sets.resolve(s, q, state=search_sets.State(s))
    assert fresh is not r1 and "t6999" not in fresh.ids
    availability.reset_for_tests()


def test_word_hits_follow_new_names():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "album": "Imaginaerum", "added_at": 1})
    assert s.result_ids(**search_predicates("album:imagin")) == {"a"}
    s.enter_batch_mode()
    try:
        s.upsert_track({"id": "b", "path": "/m/b", "album": "Imagine", "added_at": 2})
        assert s.result_ids(**search_predicates("album:imagin")) == {"a", "b"}
    finally:
        s.exit_batch_mode()
    s.upsert_track({"id": "c", "path": "/m/c", "artist": "Imagine Dragons", "added_at": 3})
    assert s.result_ids(**search_predicates("artist:dragons")) == {"c"}
