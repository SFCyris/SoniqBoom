# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Backslash escapes inside ``@field:{…}`` tag terms round-trip.

``search._esc_tag`` backslash-escapes the specials in a field-operator value
(``game:"a{b}"`` → ``@game_tag:{a\\{b\\}}``); ``data._parse_tag_query`` must end
the value at the first UNESCAPED ``}`` and unescape it the same way for every
tag field.  It used to stop at the escaped brace: ``a{b}`` parsed as the value
"a{b\\" plus a stray free-text "}", and ``artist:"}"`` as the artist "\\".
One parse path feeds /api/search, /api/search/filter, /api/tracks?format=,
/api/tracks/shuffled and Subsonic search3 — each is checked against a real
store (the web handlers called directly, search3 through its router).
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

# Search-box operator → filter_tracks kwarg.
_OPERATORS = (
    ("artist", "artist"),
    ("album_artist", "album_artist"),
    ("album", "album"),
    ("game", "game"),
    ("genre", "genre"),
    ("format", "format_"),
)
_VALUES = ("}", "{", "\\", "a{b}", "{x}", "\\}", "}\\", "{{}}", "x\\", "a\\b",
           "a{b}\\c|d: e", "Pokémon {Gold}")


def _roundtrip(q: str) -> dict:
    return _parse_tag_query(search_api._parse_advanced_query(q))


@pytest.mark.parametrize("op,kwarg", _OPERATORS)
@pytest.mark.parametrize("value", _VALUES)
def test_every_operator_round_trips_escaped_values(op, kwarg, value):
    expect = value.upper() if op == "format" else value     # format: is upper-cased
    assert _roundtrip(f'{op}:"{value}"') == {kwarg: expect}


def test_the_reported_case():
    q = search_api._parse_advanced_query('game:"a{b}"')
    assert q == r"@game_tag:{a\{b\}}"
    assert _parse_tag_query(q) == {"game": "a{b}"}


def test_mixed_operators_with_escapes_and_free_text():
    assert _roundtrip(
        r'artist:"Brace {Band}" album_artist:"Back\Slash" album:"}" game:"{Curly} Quest" '
        r'genre:"R{&}B" format:"x{y}" year:1990-1999 some loader') == {
        "artist": "Brace {Band}",
        "album_artist": "Back\\Slash",
        "album": "}",
        "game": "{Curly} Quest",
        "genre": "R{&}B",
        "format_": "X{Y}",
        "year_min": 1990,
        "year_max": 1999,
        "query": "some loader",
    }
    # A value ending in a backslash does not swallow the next term's brace.
    assert _roundtrip(r'artist:"x\" album:"{y}"') == {"artist": "x\\", "album": "{y}"}
    # Unquoted values escape the same way.
    assert _roundtrip("game:a}b genre:{c") == {"game": "a}b", "genre": "{c"}


@pytest.mark.parametrize("field,kwarg", [
    ("artist_tag", "artist"), ("album_artist_tag", "album_artist"), ("album_tag", "album"),
    ("game_tag", "game"), ("genre", "genre"), ("format", "format_"),
    ("dir_hash", "dir_hash"), ("scan_root_hash", "scan_root_hash"),
])
def test_every_tag_field_unescapes_the_builder_output(field, kwarg):
    """``_esc_tag`` is the one escaper; every field the parser maps reads back
    the exact value (including a newline, which ``_esc_tag`` leaves as is)."""
    for value in ("}", "{", "\\", "a{b}c", "\\\\}", "a\nb", "@artist_tag:{x}"):
        q = f"@{field}:{{{search_api._esc_tag(value)}}} @year:[1 2]"
        assert _parse_tag_query(q) == {kwarg: value, "year_min": 1, "year_max": 2}


def test_an_unescaped_brace_still_closes_the_value():
    assert _roundtrip("artist:x bar}") == {"artist": "x", "query": "bar}"}
    assert _parse_tag_query(r"@artist_tag:{x} a\-b") == {"artist": "x", "query": "a-b"}
    # Malformed input (unterminated, or a raw "{" _esc_tag never emits) is
    # left as free text rather than parsed as a tag.
    assert "artist" not in _parse_tag_query(r"@artist_tag:{abc\}")
    assert "artist" not in _parse_tag_query("@artist_tag:{a{b}")
    assert "artist" not in _parse_tag_query("@artist_tag:{" + "\\x" * 20_000)


def test_escaped_newline_reads_back_as_a_newline():
    """``\\x`` means ``x`` for every character, a newline included, in a tag
    value and in the free text alike."""
    assert _parse_tag_query("@artist_tag:{a\\\nb}") == {"artist": "a\nb"}
    assert _parse_tag_query("@artist_tag:{x} a\\\nb") == {"artist": "x", "query": "a\nb"}


@pytest.mark.parametrize("query", [
    "@a:{\\}" * 10_000,                                 # 60 KB, no closing brace
    "@a:{" * 10_000,
    search_api._parse_advanced_query("year:" + "@a:{\\}" * 10_000),   # year: is not escaped
], ids=["escaped-close", "open-only", "via-year-operator"])
def test_unterminated_tag_terms_parse_in_linear_time(query):
    """Each ``@x:{`` scan stops at the next unescaped brace; letting it run to
    the end of the string made these take seconds (9.3 s for the first)."""
    t0 = time.perf_counter()
    _parse_tag_query(query)
    assert time.perf_counter() - t0 < 1.0


# ── end to end: the store behind every consumer of the parser ────────────────

@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    s.upsert_tracks_batch([dict(t, path=f"/m/{t['id']}", year=1990, duration=100.0) for t in [
        {"id": "brace", "title": "One", "artist": "Brace {Band}", "album_artist": "Back\\Slash",
         "album": "{Curly} Quest", "album_source": "tag", "genre": ["R{&}B"], "format": "SPC",
         "added_at": 1},
        # Decoys: what the old truncated parse asked for ("Brace {Band\\", "\\", …).
        {"id": "curly", "title": "Two", "artist": "Brace", "album_artist": "Back",
         "album": "{Curly}", "album_source": "tag", "genre": ["R"], "format": "SPC", "added_at": 2},
        {"id": "bslash", "title": "Three", "artist": "\\", "album": "\\", "format": "MP3",
         "added_at": 3},
        {"id": "close", "title": "Four", "artist": "}", "album": "}", "format": "Tracker{X}",
         "added_at": 4},
    ]])
    assert s.get_track("brace")["game"] == "{Curly} Quest"
    return s


def _ids(rows) -> set[str]:
    return {r["id"] if isinstance(r, dict) else r.id for r in rows}


@pytest.mark.parametrize("q,expect", [
    ('artist:"Brace {Band}"', {"brace"}),
    ('artist:"}"', {"close"}),
    ('artist:"\\"', {"bslash"}),
    ('album_artist:"Back\\Slash"', {"brace"}),
    ('album:"}"', {"close"}),
    ('album:"{Curly}"', {"curly"}),
    ('game:"{curly} q"', {"brace"}),                          # prefix
    ('game:"{curly}"', {"brace", "curly"}),
    ('genre:"r{&}b"', {"brace"}),
    ('format:"tracker{x}"', {"close"}),
    ('artist:"Brace {Band}" genre:"R{&}B" format:SPC', {"brace"}),
    ('album:"}" format:"Tracker{X}"', {"close"}),
    ('album:"}" format:SPC', set()),
])
async def test_search_endpoints_find_escaped_values(store, q, expect):
    assert _ids(await search_api.run_search_dicts(q)) == expect
    assert _ids(await search_api.run_search(q)) == expect                # smart playlists


async def test_structured_filter_and_format_list_use_the_same_escapes(store):
    common = dict(album_artist=None, album=None, genre=None, scene_group=None, format=None,
                  year_min=None, year_max=None, limit=200, offset=0)
    got = await search_api.filter_tracks(**dict(common, artist="Brace {Band}", genre="R{&}B"))
    assert _ids(got) == {"brace"}
    got = await search_api.filter_tracks(**dict(common, artist="}", album="}"))
    assert _ids(got) == {"close"}
    got = await search_api.filter_tracks(**dict(common, album_artist="Back\\Slash"))
    assert _ids(got) == {"brace"}
    resp = await tracks_api.list_tracks(limit=50, offset=0, format="Tracker{X}",
                                        sort=None, order=None)
    assert _ids(json.loads(resp.body)) == {"close"}


@pytest.mark.parametrize("q,expect", [
    ('game:"{curly} quest"', {"brace"}),
    ('artist:"}"', {"close"}),
    ('artist:"\\"', {"bslash"}),
    ('album:"{Curly}" genre:R', {"curly"}),
    ('artist:"Brace {Band}" album_artist:"Back\\Slash" format:SPC', {"brace"}),
])
async def test_shuffled_order_resolves_escaped_values(store, q, expect):
    shuffle_order.clear()
    resp = await tracks_api.shuffled_tracks(
        seed=4, offset=0, limit=500, q=q, artist=None, album_artist=None, album=None,
        genre=None, scene_group=None, format=None, year_min=None, year_max=None,
        untagged=None)
    body = json.loads(resp.body)
    assert body["total"] == len(expect) and _ids(body["tracks"]) == expect


def test_subsonic_search3_resolves_escaped_values(env):
    for tid, artist, album in (("sb1", "Brace {Band}", "{Curly} Quest"),
                               ("sb2", "Brace", "{Curly}"),
                               ("sb3", "\\", "\\"),
                               ("sb4", "}", "}")):
        env.store.upsert_track(_tr(tid, title=tid, artist=artist, album=album,
                                   store=env.store, added=9000))
    for q, expect in (('artist:"Brace {Band}"', ["sb1"]),
                      ('artist:"}"', ["sb4"]),
                      ('album:"{Curly}"', ["sb2"]),
                      ('artist:"\\" album:"\\"', ["sb3"])):
        res = env.ok("search3.view", query=q)["searchResult3"]
        assert sorted(s["id"] for s in res["song"]) == expect, q
