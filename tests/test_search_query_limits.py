# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One long search query no longer freezes the server (defect 6 of the
v2.2.0 review).

The search box's parser (``api/search._parse_advanced_query``) was quadratic
in the query's length — 1.05 s for a 16,000-letter word, 15 s for 60,000,
all of it on the event loop — in three patterns: ``_SCAN_RE`` (a long word),
``_RANGE_OP_SPACE_RE`` (``year:`` + many spaces) and ``_UNIT_SPACE_RE``
(``samplerate:samplerate:…``).  Each is linear now and reads every query
exactly as before (checked here against the old patterns), and every search
entry point refuses a query longer than ``search_values.QUERY_MAX_LEN``.

In-process only: bare routers, a throw-away TrackStore, no live server.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import dlna_upnp
from soniqboom.api import playlist as playlist_api
from soniqboom.api import search as S
from soniqboom.api import stations as stations_api
from soniqboom.api import tracks as tracks_api
from soniqboom.api import users as users_api
from soniqboom.core.search_values import QUERY_MAX_LEN
from soniqboom.core.store import TrackStore

from test_subsonic_parity import env  # noqa: F401 — fixture

# ── the patterns as they were (v2.2.0): the reference for "reads the same" ──
_OLD_VALUE_RUN = r'(?:"[^"]*"|[^\s"])+'
_OLD_TERM = (r'(?:(?<!\S)(-))?(\w+):(' + _OLD_VALUE_RUN
             + r'(?:(?<=,)\s+(?!-[^\W\d_])' + _OLD_VALUE_RUN + r')*|\S+)')
_OLD_SCAN_RE = re.compile(_OLD_TERM + r'|(?<!\S)-?"[^"]*"?')
_OLD_RANGE_OP_SPACE_RE = re.compile(
    r'(?<!\w)(?<!\w-)(' + S._RANGE_FIELDS + r'):\s*([<>]=?|-)?\s*(?=\d)', re.IGNORECASE)
_OLD_UNIT_SPACE_RE = re.compile(
    r'(?<!\w)(?<!\w-)((?:samplerate|bitrate):\S*?\d)\s+(khz|kbps|hz|k)\b', re.IGNORECASE)


def _old_tighten(q: str) -> str:
    pieces = S._QUOTED_RE.split(q)
    for i in range(0, len(pieces), 2):
        p = _OLD_RANGE_OP_SPACE_RE.sub(lambda m: m.group(1) + ':' + (m.group(2) or ''),
                                       pieces[i])
        p = _OLD_UNIT_SPACE_RE.sub(r'\1\2', p)
        p = S._RANGE_RANGE_SPACE_RE.sub(r'\1-', p)
        pieces[i] = _OLD_UNIT_SPACE_RE.sub(r'\1\2', p)
    return "".join(pieces)


_FIELDS = ["year:", "track:", "samplerate:", "bitrate:", "length:", "added:", "x-year:",
           "-year:", "Samplerate:", "format:", "artist:", "a", "-", "foo:"]
_PIECES = [" ", "  ", "", "-", " - ", ">", ">=", "<", "1", "44", "44.1", "2000", "3:00",
           "khz", " khz", " k", " kbps", " hz", "kx", ",", '"', "x", ":", ".", "-1", "é",
           "\t", "(", "|", "@", "{", "}", "\\", "mp3", "flac"]


def _corpus(seed: int, n: int):
    rng = random.Random(seed)
    for _ in range(n):
        yield "".join(rng.choice(_FIELDS) + "".join(rng.choice(_PIECES) for _ in
                                                    range(rng.randint(0, 6)))
                      + rng.choice([" ", "", ",", "  "]) for _ in range(rng.randint(1, 5)))


def test_the_linear_patterns_read_every_query_as_before():
    joined = scanned = 0
    for q in _corpus(20261125, 30_000):
        assert [m.groups() + (m.span(),) for m in S._SCAN_RE.finditer(q)] == \
               [m.groups() + (m.span(),) for m in _OLD_SCAN_RE.finditer(q)], q
        assert [m.group(0, 1, 2) for m in S._RANGE_OP_SPACE_RE.finditer(q)] == \
               [m.group(0, 1, 2) for m in _OLD_RANGE_OP_SPACE_RE.finditer(q)], q
        assert S._join_units(q) == _OLD_UNIT_SPACE_RE.sub(r'\1\2', q), q
        old = _old_tighten(q)
        assert S._tighten_ranges(q) == old, q
        joined += old != q
        scanned += bool(_OLD_SCAN_RE.search(q))
    assert joined > 1000 and scanned > 20_000        # (the corpus reaches both paths)


@pytest.mark.parametrize("q,expect", [
    ("samplerate:44.1 khz", "samplerate:44.1khz"),
    ("bitrate:128 kbps x", "bitrate:128kbps x"),
    ("samplerate:44 k,samplerate:48 k", "samplerate:44k,samplerate:48k"),
    ("samplerate:samplerate:44 khz", "samplerate:samplerate:44khz"),
    ("samplerate:x samplerate:4 hz", "samplerate:x samplerate:4hz"),
    ("samplerate:44 khzx", "samplerate:44 khzx"),
    ("year: >= 2000", "year:>=2000"), ("year:   x", "year:   x"),
    ("multi-track: 2", "multi-track: 2"), ('album:"back on track: 2"', 'album:"back on track: 2"'),
])
def test_tightening_examples(q, expect):
    assert S._tighten_ranges(q) == expect == _old_tighten(q)


def _parse_s(q: str) -> float:
    t = time.perf_counter()
    S.search_predicates(q)
    return time.perf_counter() - t


@pytest.mark.parametrize("make", [
    lambda n: "year:1 " + "a" * n,                         # a long word (_SCAN_RE)
    lambda n: "year:" + " " * n + "x",                     # spaces after an operator
    lambda n: ("samplerate:" * (n // 11 + 1))[:n],         # one long run (_UNIT_SPACE_RE)
    lambda n: ("ab " * (n // 3 + 1))[:n],
    lambda n: ('a:"' * (n // 3 + 1))[:n],
    lambda n: "format:" + ("a, " * (n // 3 + 1))[:n],
], ids=["word", "spaces", "samplerate-run", "words", "quotes", "commas"])
def test_the_parse_is_linear_in_the_query_length(make):
    # v2.2.0: 1.05 s / 1.4 s / 0.26 s at 16,000 characters, 15 s / 20 s / 3.6 s
    # at 60,000 for the first three.
    small = min(_parse_s(make(8_000)) for _ in range(3))
    big = min(_parse_s(make(64_000)) for _ in range(3))
    assert big < 1.0, big
    # linear: ~8x; quadratic: ~64x.  32x leaves room for a loaded machine (the
    # parallel full run measured 20.4x once on a linear parse) and still fails
    # a quadratic one.
    assert big < max(small, 0.003) * 32, (small, big)


# ── the limit at every entry point ────────────────────────────────────────────

LONG = "a" * (QUERY_MAX_LEN + 1)
OK = "a" * QUERY_MAX_LEN


@pytest.fixture
def web(monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    s.upsert_tracks_batch([{"id": "t1", "title": "One", "artist": "Somebody", "path": "/m/t1",
                            "format": "MP3", "duration": 100.0, "added_at": 1}])
    app = FastAPI()
    for r in (S.router, tracks_api.router, playlist_api.router, stations_api.router):
        app.include_router(r, prefix="/api")
    app.dependency_overrides[users_api.current_user] = lambda: None
    app.dependency_overrides[users_api.listener_uid] = lambda: "u1"
    app.dependency_overrides[users_api.optional_listener_uid] = lambda: None
    return TestClient(app)


@pytest.mark.parametrize("url", ["/api/search?q={}", "/api/search/quick?q={}",
                                 "/api/search/page?q={}", "/api/search/filter?q={}",
                                 "/api/search/query/semantic?q={}",
                                 "/api/tracks/shuffled?seed=1&q={}", "/api/stations/search?q={}"])
def test_web_search_entry_points_refuse_a_long_query(web, url):
    r = web.get(url.format(LONG))
    assert r.status_code == 422, (url, r.status_code)
    assert "at most" in r.text or "string_too_long" in r.text


@pytest.mark.parametrize("url", ["/api/search?q={}", "/api/search/quick?q={}",
                                 "/api/search/page?q={}", "/api/search/filter?q={}",
                                 "/api/tracks/shuffled?seed=1&q={}"])
def test_web_search_entry_points_take_a_query_at_the_limit(web, url):
    assert web.get(url.format(OK)).status_code == 200, url


def test_smart_playlist_queries_are_limited(web):
    r = web.post("/api/playlists", json={"name": "x", "query": LONG})
    assert r.status_code == 422
    r = web.put("/api/playlists/whatever", json={"query": LONG})
    assert r.status_code == 422


@pytest.mark.parametrize("view", ["search3.view", "search2.view"])
def test_subsonic_search2_search3_refuse_a_long_query(env, view):  # noqa: F811
    got = env.fail(view, query=LONG)
    assert got["code"] == 10 and "query" in got["message"]
    # (a form POST — the OpenSubsonic formPost body — is checked the same way)
    r = env.client.post(f"/rest/{view}", content=f"query={LONG}&f=json".encode(),
                        headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.json()["subsonic-response"]["error"]["code"] == 10
    env.ok(view, query=OK)


def test_subsonic_search_v1_refuses_a_long_query(env):  # noqa: F811
    for field in ("any", "artist", "album", "title"):
        assert env.fail("search.view", **{field: LONG})["code"] == 10
    env.ok("search.view", any=OK)


def test_dlna_search_refuses_long_criteria():
    req = types.SimpleNamespace(base_url="http://h:8080/")
    crit = 'dc:title contains "' + "a" * dlna_upnp._SEARCH_CRITERIA_MAX + '"'
    resp = asyncio.run(dlna_upnp._do_search({"SearchCriteria": crit, "RequestedCount": "5"},
                                            req))
    assert resp.status_code == 500 and b"<errorCode>708</errorCode>" in resp.body
