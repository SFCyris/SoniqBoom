# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Free text keeps its backslashes; ``year:`` stays a number; range terms parse
in linear time.

* ``search._escape_text`` escapes ``\\`` (before ``-``, ``:`` and ``/``):
  ``data._parse_tag_query`` reads every ``\\x`` back as ``x``, so a plain
  search for ``AC\\DC`` used to reach the store as ``ACDC`` and find nothing.
  One helper serves the plain search, the free text next to an operator,
  smart playlists, ``/api/tracks/shuffled?q=`` and Subsonic search3.
* A ``year:`` value that is no number or range stays free text — it can't
  carry tag syntax (``year:@scan_root_hash:{…}``) into the query.
* ``@field:[…]`` stops at a ``[`` (a range's inside never holds one), so an
  unterminated run of ``@year:[`` no longer scans to the end of the string
  from every start.
"""
from __future__ import annotations

import json
import time

import pytest

from soniqboom.api import search as search_api
from soniqboom.api import tracks as tracks_api
from soniqboom.core import shuffle_order
from soniqboom.core.data import _parse_tag_query
from soniqboom.core.store import TrackStore

from test_subsonic_parity import _tr, env  # noqa: F401 — fixture


def _text(q: str):
    return search_api.search_predicates(q).get("query")


# ── free-text backslashes ────────────────────────────────────────────────────

@pytest.mark.parametrize("q,text", [
    ("AC\\DC", "AC\\DC"),
    ("\\\\", "\\\\"),                          # two backslashes stay two
    ("x\\", "x\\"),                            # a trailing one
    ("\\-\\:\\/", "\\-\\:\\/"),                 # a backslash before each escaped character
    ("a\\-b c:d e/f", "a\\-b c:d e/f"),
    ("AC\\DC live", "AC\\DC live"),
])
def test_plain_text_keeps_its_backslashes(q, text):
    assert search_api._parse_advanced_query(q) is None       # (the plain path)
    assert _text(q) == text


@pytest.mark.parametrize("q,preds", [
    ("format:MP3 AC\\DC", {"format_": "MP3", "query": "AC\\DC"}),
    ("AC\\DC year:1990", {"year_min": 1990, "year_max": 1990, "query": "AC\\DC"}),
    ("artist:x a\\\\b", {"artist_match": "x", "query": "a\\\\b"}),
    # (an exclusion and a quoted phrase were always escaped with _esc_tag)
    ("-AC\\DC", {"negate": ((("query", "AC\\DC"),),)}),
])
def test_free_text_next_to_an_operator_keeps_its_backslashes(q, preds):
    assert search_api.search_predicates(q) == preds


def test_a_phrase_and_its_free_text_agree():
    preds = search_api.search_predicates('"AC\\DC live"')
    assert preds["phrase"] == ("AC\\DC live",)
    assert preds["query"] == '"AC\\DC live"'


@pytest.mark.parametrize("q,preds", [
    ("@scan_root_hash:{x}", {"query": "@scan_root_hash:{x}"}),
    ("x @artist_tag:{Queen}", {"query": "x @artist_tag:{Queen}"}),
    ("x\\ @artist_tag:{Queen}", {"query": "x\\ @artist_tag:{Queen}"}),
    ("@year:[1 2]", {"query": "@year:[1 2]"}),
    ("@genre\\:{rock}", {"query": "@genre\\:{rock}"}),
    # ``genre:`` / ``format:`` after the ``@`` are the box's own operators,
    # their values the literal "{rock}" / "{MP3}" — no tag syntax.
    ("@genre:{rock}", {"genre_match": "{rock}", "query": "@"}),
    ("\\@genre:{rock}", {"genre_match": "{rock}", "query": "\\@"}),
    ("-@format:{MP3}", {"format_": "{MP3}", "query": "-@"}),
])
def test_typed_tag_syntax_is_never_a_tag_term(q, preds):
    """Tag syntax typed into the search box — with or without a backslash
    before the ``@`` / ``:`` — never reaches the store as a tag term
    (``scan_root_hash``, the exact ``artist`` / ``genre``, a ``negate``)."""
    assert search_api.search_predicates(q) == preds


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    s.upsert_tracks_batch([dict(t, path=f"/m/{t['id']}", duration=100.0) for t in [
        {"id": "acdc", "title": "Thunderstruck", "artist": "AC\\DC", "format": "MP3",
         "year": 1990, "added_at": 1},
        # A decoy: what the old parse asked for ("ACDC").
        {"id": "plain", "title": "Tribute", "artist": "ACDC", "format": "MP3",
         "year": 1999, "added_at": 2},
        {"id": "old", "title": "Commando", "artist": "Rob Hubbard", "format": "SID",
         "year": 1985, "added_at": 3},
        {"id": "new", "title": "Remix", "artist": "Rob Hubbard", "format": "MP3",
         "year": 2020, "added_at": 4},
    ]])
    return s


def _ids(rows) -> set[str]:
    return {r["id"] if isinstance(r, dict) else r.id for r in rows}


@pytest.mark.parametrize("q,expect", [
    ("AC\\DC", {"acdc"}),
    ("format:MP3 AC\\DC", {"acdc"}),
    ("ACDC", {"plain"}),
    ("year:<2000 format:MP3", {"acdc", "plain"}),
    ("year:< 1990", {"old"}),
    ("year:>=2020", {"new"}),
    ("year:-1990", {"acdc", "old"}),
])
async def test_search_and_smart_playlists_find_them(store, q, expect):
    assert _ids(await search_api.run_search_dicts(q)) == expect
    assert _ids(await search_api.run_search(q)) == expect                # smart playlists


@pytest.mark.parametrize("q,expect", [
    ("AC\\DC", {"acdc"}),
    ("format:MP3 AC\\DC", {"acdc"}),
    ("year:<2000", {"acdc", "plain", "old"}),
])
async def test_shuffled_order_finds_them(store, q, expect):
    shuffle_order.clear()
    resp = await tracks_api.shuffled_tracks(
        seed=4, offset=0, limit=500, q=q, artist=None, album_artist=None, album=None,
        genre=None, scene_group=None, format=None, year_min=None, year_max=None,
        untagged=None)
    body = json.loads(resp.body)
    assert body["total"] == len(expect) and _ids(body["tracks"]) == expect


def test_subsonic_search3_free_text_next_to_an_operator(env):
    env.store.upsert_track(_tr("sb1", title="Thunderstruck", artist="AC\\DC",
                               store=env.store, added=9000))
    env.store.upsert_track(_tr("sb2", title="Tribute", artist="ACDC",
                               store=env.store, added=9001))
    res = env.ok("search3.view", query="format:mp3 ac\\dc")["searchResult3"]
    assert [s["id"] for s in res["song"]] == ["sb1"]
    # The free text also names the artists (``adv_kw["query"]``), word-wise:
    # "ac", "dc" — so "ACDC" too, as for any punctuated name; the old "acdc"
    # missed "AC\DC".
    assert "AC\\DC" in [a["name"] for a in res["artist"]]


# ── year: values ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "year:@scan_root_hash:{deadbeef}",
    "year:@year:[1 2]",
    "year:1990@dir_hash:{x}",
    "year:(1990",
])
def test_a_year_value_cannot_carry_tag_syntax(q):
    preds = search_api.search_predicates(q)
    assert preds == {"query": q}            # the whole term stays free text


# ── linear-time range terms ──────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "@year:[" * 20_000,                                   # 140 KB, no closing bracket
    "-@year:[" * 20_000,
    "(@year:[1 2] | @year:[" * 8_000,                     # an unclosed several-values group
], ids=["open-only", "negated", "group"])
def test_unterminated_range_terms_parse_in_linear_time(query):
    """Each ``@x:[`` scan stops at the next ``[`` (~10–25 ms here); running to
    the end of the string from every start took seconds (about 8 s for the
    first query, 4.5 s for the group)."""
    t0 = time.perf_counter()
    _parse_tag_query(query)
    assert time.perf_counter() - t0 < 1.0


def test_well_formed_ranges_still_parse():
    assert _parse_tag_query("@year:[(1990 +inf] @track:[-inf (5]") == {
        "year_min": 1991, "track_max": 4}
    assert _parse_tag_query("(@year:[1990 1990] | @year:[2000 2000])") == {
        "anyof": (((("year_max", 1990), ("year_min", 1990)),
                   (("year_max", 2000), ("year_min", 2000))),)}
