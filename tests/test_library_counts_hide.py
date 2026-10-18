# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``GET /api/library/counts`` with "Tracks on unavailable drives: Hide": the
same numbers the lists give (rows minus the bucket row), drive offline and back
online, with "Hide duplicates" off and on — and the counts follow the drive."""
from __future__ import annotations

import copy

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library
from soniqboom.api import tracks as tracks_api
from tests.test_offline_tracks import _row, lib  # noqa: F401  (fixture)

_LISTS = {"artists": "/api/library/artists", "album_artists": "/api/library/album-artists",
          "albums": "/api/library/albums", "genres": "/api/library/genres",
          "years": "/api/library/years"}


def _client():
    app = FastAPI()
    for mod in (tracks_api, library):
        app.include_router(mod.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _both(c):
    counts = (await c.get("/api/library/counts")).json()
    want = {}
    for k, url in _LISTS.items():
        want[k] = sum(1 for r in (await c.get(url)).json() if not r.get("label"))
    want["scene_groups"] = len((await c.get("/api/library/scene-groups")).json())
    cnt = (await c.get("/api/tracks/count")).json()
    want["tracks"], want["visible"] = cnt["count"], cnt["visible"]
    return {k: counts[k] for k in want}, want


@pytest.mark.parametrize("dedup", [False, True])
async def test_counts_match_the_lists_in_hide_mode(lib, dedup):  # noqa: F811
    s = lib.store
    # tags only the offline drive carries (so hiding it moves every count), an
    # untagged track there (a "[No Artist]" bucket), and a duplicate pair across drives
    extra = [_row(f"{lib.root_b}/x/only{i}.mod", lib.root_b, 900 + i, artist=f"B-only {i}",
                  album_artist=f"B-AA {i}", album=f"B-album {i}", genre=[f"B-genre {i}"],
                  year=2050 + i, scene_group=f"B-group {i}") for i in range(4)]
    extra.append(_row(f"{lib.root_b}/x/untagged.mod", lib.root_b, 950, artist="", album_artist=""))
    extra.append(_row(f"{lib.root_a}/d0/dupA.mod", lib.root_a, 960, title="Dup", artist="Dup Artist",
                      duplicate_group_id="g9", is_duplicate_primary=False))
    extra.append(_row(f"{lib.root_b}/x/dupB.mod", lib.root_b, 961, title="Dup", artist="Dup Artist",
                      duplicate_group_id="g9", is_duplicate_primary=True))
    for r in extra:
        s.upsert_track(copy.deepcopy(r))
    s.set_config("filter_duplicates", dedup)
    lib.mode("hide")
    async with _client() as c:
        online, want = await _both(c)
        assert online == want
        lib.offline()                                    # root B unavailable → hidden
        hidden, want_h = await _both(c)
        assert hidden == want_h
        lib.online()
        back, want_b = await _both(c)
        assert back == want_b
    assert hidden["visible"] < online["visible"]
    for k in ("artists", "album_artists", "albums", "genres", "years", "scene_groups"):
        assert hidden[k] < online[k], k                  # hiding the drive moved every count
    assert back == online
