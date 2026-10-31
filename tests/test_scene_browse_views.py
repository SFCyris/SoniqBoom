# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The scene browse views — ``/api/library/composers`` / ``games`` /
``platforms`` / ``parties``: each row's count is what its drill-down
(``/api/search/filter?<facet>=name``) lists and what the sidebar counts say,
with "Hide duplicates" off and on, with a drive's tracks hidden, and after a
write the counts' key does not see (a party found later)."""
from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library, search
from soniqboom.api import tracks as tracks_api
from tests.test_offline_tracks import _row, lib  # noqa: F401  (fixture)

_VIEWS = {"composer": "composers", "game": "games", "platform": "platforms", "party": "parties"}


def _client():
    app = FastAPI()
    for mod in (tracks_api, library, search):
        app.include_router(mod.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _add_scene_rows(lib):  # noqa: F811
    ra, rb = lib.root_a, lib.root_b
    rows = [
        _row(f"{ra}/s/hub1.sid", ra, 500, format="SID", composer="Rob Hubbard",
             scene_party="Revision 2019", game="Commando"),
        _row(f"{ra}/s/hub2.sid", ra, 501, format="SID", composer="Rob Hubbard • Ben Daglish",
             scene_party="revision 2019", game="Commando", game_aliases=["Commando (C64)"]),
        _row(f"{ra}/s/dag.sid", ra, 502, format="SID", composer="ben  daglish",
             game="Gauntlet"),
        _row(f"{rb}/s/nes.nsf", rb, 503, format="NSF", composer="Koji Kondo", game="Zelda"),
        _row(f"{rb}/s/spc.spc", rb, 504, format="SPC", composer="Koji Kondo",
             scene_party="Assembly 1995"),
        # a duplicate pair across the drives (the copy on B is the primary)
        _row(f"{ra}/s/dupA.sid", ra, 505, format="SID", composer="Martin Galway",
             title="Dup", duplicate_group_id="g7", is_duplicate_primary=False),
        _row(f"{rb}/s/dupB.sid", rb, 506, format="SID", composer="Martin Galway",
             title="Dup", duplicate_group_id="g7", is_duplicate_primary=True),
    ]
    for r in rows:
        lib.store.upsert_track(copy.deepcopy(r))
    return rows


async def _check(c):
    """Every view: rows' counts == drill-down lengths == the sidebar counts."""
    counts = (await c.get("/api/library/counts")).json()
    seen = {}
    for facet, name in _VIEWS.items():
        rows = (await c.get(f"/api/library/{name}")).json()
        assert counts[name] == len(rows), name
        for r in rows:
            got = (await c.get("/api/search/filter",
                               params={facet: r[facet], "limit": 2000})).json()
            assert len(got) == r["count"], (name, r)
        seen[name] = {r[facet]: r["count"] for r in rows}
    return seen


@pytest.mark.parametrize("dedup", [False, True])
async def test_rows_counts_and_drilldowns_agree(lib, dedup):  # noqa: F811
    _add_scene_rows(lib)
    lib.store.set_config("filter_duplicates", dedup)
    async with _client() as c:
        seen = await _check(c)
        # a "A • B" credit counts under each name, never as the joined name;
        # two spellings that differ in their spaces are one composer
        comp = seen["composers"]
        assert comp["Rob Hubbard"] == 2 and comp["Ben Daglish"] == 2
        assert not any("•" in k for k in comp)
        assert comp["Martin Galway"] == (1 if dedup else 2)
        assert seen["games"] == {"Commando": 2, "Gauntlet": 1, "Zelda": 1}   # no alias row
        assert seen["parties"] == {"Assembly 1995": 1, "Revision 2019": 2}
        plat = seen["platforms"]
        assert plat["NES"] == 1 and plat["SNES"] == 1
        assert plat["SID"] == 34 + 3 + (1 if dedup else 2)    # (the lib's 34 SID rows)
        lib.mode("hide")
        lib.offline()                                   # drive B's tracks hidden
        hidden = await _check(c)
        assert "Zelda" not in hidden["games"] and "Koji Kondo" not in hidden["composers"]
        # (the pair's primary is on B: its copy on A stands in)
        assert hidden["composers"]["Martin Galway"] == 1
        lib.online()
        assert await _check(c) == seen


async def test_a_party_found_later_shows_at_once(lib):  # noqa: F811
    rows = _add_scene_rows(lib)
    async with _client() as c:
        before = await _check(c)
        # (``scene_party`` moves no browse counter: the views follow it anyway)
        lib.store.update_track_fields(rows[2]["id"], {"scene_party": "X'2000"})
        after = await _check(c)
    assert "X'2000" not in before["parties"] and after["parties"]["X'2000"] == 1


async def test_counts_never_wait_for_the_search_indexes(lib):  # noqa: F811
    """A start that is still building the search indexes: the counts answer
    at once (composers / parties unknown), the list waits and answers."""
    _add_scene_rows(lib)
    s = lib.store
    s.rebuild_indexes(defer_search=True)
    assert s._sx_todo is not None
    async with _client() as c:
        counts = (await c.get("/api/library/counts")).json()
        assert counts["composers"] is None and counts["parties"] is None
        assert counts["games"] == 3 and counts["platforms"] >= 3
        assert s._sx_todo is not None                   # (the counts built nothing)
        build = asyncio.create_task(s.build_search_indexes_async())   # (as a start does)
        comp = (await c.get("/api/library/composers")).json()
        await build
        assert {r["composer"] for r in comp} >= {"Rob Hubbard", "Koji Kondo"}
        counts = (await c.get("/api/library/counts")).json()
        assert counts["composers"] == len(comp)


async def test_edits_and_a_rebuild_move_the_lists(lib):  # noqa: F811
    """A composer / game / format edit (a field update no other counter
    sees), a deleted track and an index rebuild: each list follows."""
    rows = _add_scene_rows(lib)
    s = lib.store
    async with _client() as c:
        await _check(c)
        s.update_track_fields(rows[2]["id"], {"composer": "Jeroen Tel", "game": "Cybernoid"})
        s.update_track_fields(rows[3]["id"], {"format": "SPC"})       # NES → SNES
        after = await _check(c)
        assert after["composers"]["Jeroen Tel"] == 1 and after["composers"]["Ben Daglish"] == 1
        assert "Gauntlet" not in after["games"] and after["games"]["Cybernoid"] == 1
        assert "NES" not in after["platforms"] and after["platforms"]["SNES"] == 2
        s.delete_track(rows[0]["id"])
        gone = await _check(c)
        assert gone["composers"]["Rob Hubbard"] == 1 and gone["games"]["Commando"] == 1
        s.rebuild_indexes()
        assert await _check(c) == gone


async def test_a_partys_tracks_come_compo_by_compo_in_placing_order(lib):  # noqa: F811
    ra = lib.root_a
    spec = [("p1", "Zeta", "Tracked Music", 2), ("p2", "Alpha", "Oldskool Music", 3),
            ("p3", "Beta", "Oldskool Music", 1), ("p4", "Gamma", None, None),
            ("p5", "Delta", "Tracked Music", 1), ("p6", "Eps", "Oldskool Music", None)]
    for i, (name, title, compo, place) in enumerate(spec):
        lib.store.upsert_track(copy.deepcopy(_row(
            f"{ra}/rev/{name}.mod", ra, 700 + i, title=title, scene_party="Revision 2019",
            scene_compo=compo, scene_place=place, scene_compo_entries=9 if compo else None)))
    async with _client() as c:
        rows = (await c.get("/api/search/filter", params={"party": "Revision 2019",
                                                          "limit": 2000})).json()
        assert [(r["scene_compo"], r["scene_place"], r["title"]) for r in rows] == [
            ("Oldskool Music", 1, "Beta"), ("Oldskool Music", 3, "Alpha"),
            ("Oldskool Music", None, "Eps"), ("Tracked Music", 1, "Delta"),
            ("Tracked Music", 2, "Zeta"), (None, None, "Gamma")]
        page = (await c.get("/api/search/filter", params={"party": "Revision 2019",
                                                          "limit": 2, "offset": 2})).json()
        assert [r["title"] for r in page] == ["Eps", "Delta"]
        # (list rows carry the party and compo — the Compo column reads them)
        assert rows[0]["scene_party"] == "Revision 2019" and rows[0]["scene_compo_entries"] == 9
        # one competition (the Compo column's link): its entries by place —
        # and its shuffle deals only them
        one = (await c.get("/api/search/filter", params={
            "party": "Revision 2019", "compo": "oldskool  music", "limit": 2000})).json()
        assert [r["title"] for r in one] == ["Beta", "Alpha", "Eps"]
        sh = (await c.get("/api/tracks/shuffled", params={
            "party": "Revision 2019", "compo": "Oldskool Music", "seed": 7, "limit": 50})).json()
        assert sh["total"] == 3 and {t["title"] for t in sh["tracks"]} == {"Beta", "Alpha", "Eps"}
