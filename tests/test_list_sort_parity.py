# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One order per column: a small All Tracks (sorted in the browser —
library.js ``[list-sort]``, run under Node), a large All Tracks
(``GET /api/tracks?sort=``, the server's sorted indexes) and a search
(``GET /api/search/page?sort=``) list the same tracks in the same order, for
every column and both directions.

The library is built to break a naive sort: mixed case, blanks (empty, only
whitespace — Python's whitespace, not JavaScript's — missing), accents,
characters past U+FFFF next to U+E000–U+FFFF, numeric-string and YYYYMMDD
years, no / zero / NaN lengths, and many equal keys (ties go by id).  The
columns without a server index (Track, Location) follow the same rule —
checked against it written out here.  Skipped when Node is not installed.
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.core import availability, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SERVER_KEYS = ("title", "artist", "album_artist", "album", "year", "duration", "format")
BROWSER_ONLY = ("track_number", "path")

_RUNNER = r"""
import { readFileSync } from 'node:fs';
const src = readFileSync(process.argv[process.argv.length - 1], 'utf8');
const m = src.match(/\/\/ \[list-sort:begin\][^\n]*\n([\s\S]*?)\/\/ \[list-sort:end\]/);
if (!m) throw new Error('list-sort region not found');
const { _sortRows } = new Function(`${m[1]}; return { _sortRows };`)();
const { rows, keys } = JSON.parse(readFileSync(0, 'utf8'));
const out = {};
for (const k of keys) for (const asc of [true, false]) {
  out[`${k}:${asc ? 'asc' : 'desc'}`] = _sortRows(rows, k, asc).map((t) => t.id);
}
process.stdout.write(JSON.stringify(out));
"""

TEXT = ["Alpha", "alpha", "ALPHA", "beta", "Beta ", " beta", "Émile", "emile", "Zeta", "zeta",
        "", "   ", "\u00a0", "\x1c", "\ufeffalpha", "\u2003Gamma", "\ufb00", "\U0001F600 smile",
        "\ue000 private", "\uffef last", "ß", "SS", "İstanbul", "istanbul", "10", "9", "_x", "~", None]
YEARS = [None, 1990, 1991, "1992", " 1993 ", 19930101, 1994.0, "x", "", "1_995", "1e3", "inf", 0, -5,
         True, 2001, 2001, "nan", "19940101"]
DURS = [None, 0, 0.0, math.nan, 100.0, 50.5, 50.5, 7, 300, "120", "abc", -1]


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library):
        app.include_router(mod.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    shuffle_order.clear()
    root = tmp_path / "A"
    root.mkdir()
    s.upsert_scan_dir(str(root))
    rnd = random.Random(20261018)
    for i in range(900):
        s.upsert_track({
            "id": f"{rnd.choice('abcdef')}{i:04d}",
            "path": f"{root}/{rnd.choice(['Dir', 'dir', 'Ärger', 'z'])}/{rnd.choice(TEXT) or 'f'}{i % 7}.mod",
            "title": rnd.choice(TEXT),
            "artist": rnd.choice(TEXT),
            "album_artist": rnd.choice(TEXT),
            "album": rnd.choice(TEXT),
            "format": rnd.choice(["SID", "sid", "MP3", "", "ProTracker", "protracker", None]),
            "year": rnd.choice(YEARS),
            "duration": rnd.choice(DURS),
            "track_number": rnd.choice([None, 1, 2, 2, 10, "3", 0]),
            "disc_number": rnd.choice([None, 1, 2, "2"]),
            "added_at": rnd.choice([1, 2, 3]),
            "composer": "everyone",                 # (one word every track has: the search lists all)
        })
    yield s
    availability.reset_for_tests()


def _browser_orders(rows: list[dict], keys) -> dict:
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", _RUNNER,
         os.environ.get("SB_LIBRARY_JS") or str(ROOT / "soniqboom/frontend/js/library.js")],
        input=json.dumps({"rows": rows, "keys": list(keys)}), capture_output=True, text=True,
        timeout=120, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout)


async def _all_tracks(c, sort, order) -> list[str]:
    ids, off = [], 0
    while True:
        rows = (await c.get("/api/tracks", params={"limit": 200, "offset": off, "sort": sort,
                                                   "order": order})).json()
        ids += [r["id"] for r in rows]
        if len(rows) < 200:
            return ids
        off += 200


async def _search(c, sort, order) -> list[str]:
    ids, rs = [], None
    while True:
        p = {"q": "everyone", "offset": len(ids), "limit": 250, "sort": sort, "order": order,
             "fields": "list"}
        if rs:
            p["rs"] = rs
        j = (await c.get("/api/search/page", params=p)).json()
        rs = j["rs"]
        ids += [t["id"] for t in j["tracks"]]
        if len(ids) >= j["total"]:
            return ids


async def test_small_all_tracks_large_all_tracks_and_search_sort_alike(lib):
    async with _client() as c:
        # the small All Tracks' rows, as the browser fetches them (the default order)
        rows = (await c.get("/api/tracks", params={"limit": 5000})).json()
        assert len(rows) == len(lib._tracks)
        browser = _browser_orders(rows, SERVER_KEYS + BROWSER_ONLY)
        for key in SERVER_KEYS:
            for order in ("asc", "desc"):
                server = await _all_tracks(c, key, order)
                assert browser[f"{key}:{order}"] == server, (key, order)
                assert await _search(c, key, order) == server, (key, order)
        # (the orders are not trivially equal: case and blanks really interleave)
        keys = [_lex(lib._tracks[t]["title"]) for t in browser["title:asc"]]
        blank = [i for i, k in enumerate(keys) if k == "\uffff"]
        assert blank and blank == list(range(blank[0], blank[-1] + 1))      # (one run of blanks,
        assert all(k.startswith("\U0001F600") for k in keys[blank[-1] + 1:])  # after all but U+1F600)
        assert keys[blank[0] - 1].startswith("\uffef")
        near = {lib._tracks[t]["title"] for t in browser["title:asc"][blank[0] - 400:blank[0]]}
        assert keys.index("alpha") < keys.index("beta") and {"ALPHA", "alpha", "Alpha"} <= {
            lib._tracks[t]["title"] for t in browser["title:asc"]} and near


def _lex(v) -> str:
    return (v or "").strip().lower() or "\uffff"


def _num(v):
    return store_mod._num_key(v)


def _track_key(t):
    n = _num(t.get("track_number"))
    if n == math.inf:
        return (math.inf, math.inf)
    d = _num(t.get("disc_number"))
    return (0 if d == math.inf else d, n)


async def test_track_and_location_follow_the_same_rule(lib):
    async with _client() as c:
        rows = (await c.get("/api/tracks", params={"limit": 5000})).json()
    browser = _browser_orders(rows, BROWSER_ONLY)
    tracks = lib._tracks
    for key, keyof in (("path", lambda t: _lex(t.get("path"))), ("track_number", _track_key)):
        want = [tid for _k, tid in sorted((keyof(tracks[tid]), tid) for tid in tracks)]
        assert browser[f"{key}:asc"] == want, key
        assert browser[f"{key}:desc"] == want[::-1], key
