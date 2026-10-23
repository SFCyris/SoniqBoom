# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → "Tracks that can't be played" (core/availability.py,
``TrackStore._cant_play_ids``).

* Which tracks count: reported broken, ``defect`` corrupt or unplayable —
  never glitched, partial or empty ones.
* The store keeps their ids on every write path (upsert, batch, field
  update, delete, rebuild, index swap); a content version moves only when the
  ids change.
* Mark lists them; Hide leaves them out of the lists, searches, counts and
  legends — through the same id set as offline Hide — while playlists,
  folders, the rows asked for by id and the duplicates view list them.  A
  mark made while Hide is on moves every list and count at once although it
  is no browse write (``_browse_seq`` stays).  With "Hide duplicates" on, a
  group whose primary can't be played is listed by its copy that plays.
* The setting (admin API, ui-config, the state endpoint), the notification,
  Subsonic (whose listings follow no Hide setting).
* A seeded randomized run of marks, defects, drives and writes: with every
  combination of both settings and "Hide duplicates", the lists (every page),
  counts, legends and search results equal a brute-force filter of the store.
"""
from __future__ import annotations

import asyncio
import copy
import json
import random

import httpx
import pytest

from soniqboom.api import library
from soniqboom.core import availability, skip_rules
from soniqboom.core import store as store_mod
from soniqboom.core.duplicates import primary_sort_key
from soniqboom.core.store import TrackStore, _agg_year, track_genres
from tests.test_offline_tracks import _app, _row, lib, ph  # noqa: F401  (fixture)

CANT = ("corrupt", "unplayable")


@pytest.fixture
def ulib(lib, monkeypatch):  # noqa: F811
    monkeypatch.setattr(skip_rules, "_STALE_OK_S", 0.0)
    monkeypatch.setattr(skip_rules, "_BACKGROUND", False)
    skip_rules.reset_for_tests()

    def unplay(m):
        lib.store.set_config(availability.UNPLAY_KEY, m)
    lib.unplay = unplay
    yield lib
    skip_rules.reset_for_tests()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t")


async def _j(c, url):
    r = await c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return r.json()


def _brute_cant_play(s) -> set:
    return {tid for tid, t in s._tracks.items()
            if t.get("health") == "broken" or t.get("defect") in CANT}


# ── which tracks count, and the store keeps them ─────────────────────────────

def test_which_states_count():
    s = TrackStore()
    rows = {"br": {"health": "broken"}, "gl": {"health": "glitched"},
            "co": {"defect": "corrupt"}, "un": {"defect": "unplayable"},
            "pa": {"defect": "partial"}, "em": {"defect": "empty"}, "ok": {},
            "both": {"health": "glitched", "defect": "unplayable"}}
    for tid, kw in rows.items():
        s.upsert_track({"id": tid, "title": tid, "path": f"/m/{tid}.mod", **kw})
    assert s.cant_play_ids()[1] == {"br", "co", "un", "both"}
    assert availability.cant_play_ids(s)[0] == frozenset({"br", "co", "un", "both"})
    # the same test the skip rules use
    assert {t for t in rows if skip_rules.cant_play(s.get_track(t))} == {"br", "co", "un", "both"}


def test_the_store_keeps_the_ids_on_every_write_path():
    r = random.Random(5)
    s = TrackStore()

    def mk(i):
        return {"id": f"t{i}", "title": f"T{i}", "artist": r.choice(["A", "B", ""]),
                "path": f"/m/{i}.mod", "health": r.choice([None, None, "broken", "glitched"]),
                "defect": r.choice([None, None, "corrupt", "unplayable", "partial", "empty"])}
    s.upsert_tracks_batch([mk(i) for i in range(120)])
    assert s._cant_play_ids == _brute_cant_play(s)
    nxt = 1000
    for step in range(120):
        ids = sorted(s._tracks)
        op = r.choice(["upd", "upd2", "batch", "add", "one", "del", "del1", "batch_mode",
                       "rebuild", "mark", "unmark"])
        gen = s._cant_play_gen
        before = set(s._cant_play_ids)
        if op == "upd":
            s.update_track_fields(r.choice(ids), {"health": r.choice(["broken", "glitched", None])})
        elif op == "upd2":
            s.update_track_fields(r.choice(ids), {"defect": r.choice([*CANT, "partial", None]),
                                                  "title": f"x{step}"})
        elif op == "batch":
            s.update_track_fields_batch([(t, {"defect": r.choice([*CANT, None]),
                                              "artist": r.choice(["A", "C"])})
                                         for t in r.sample(ids, 5)])
        elif op == "add":
            s.upsert_tracks_batch([mk(nxt + j) for j in range(3)])
            nxt += 3
        elif op == "one":
            t = mk(int(r.choice(ids)[1:]))                  # a re-upsert with other fields
            s.upsert_track(t)
        elif op == "del":
            s.delete_track_ids(r.sample(ids, 3))
        elif op == "del1":
            s.delete_track(r.choice(ids))
        elif op == "batch_mode":
            s.enter_batch_mode()
            s.upsert_tracks_batch([mk(int(t[1:])) for t in r.sample(ids, 4)])
            s.update_track_fields(r.choice(sorted(s._tracks)), {"health": "broken"})
            assert s._cant_play_ids == _brute_cant_play(s)     # eager inside a batch too
            s.exit_batch_mode()
        elif op == "rebuild":
            s.rebuild_indexes()
        elif op == "mark":
            skip_rules.set_mark(s, r.choice(ids), "broken")
        else:
            skip_rules.set_mark(s, r.choice(ids), None)
        assert s._cant_play_ids == _brute_cant_play(s), (step, op)
        if s._cant_play_ids != before:
            assert s._cant_play_gen != gen, (step, op)        # a change always moves the counter
    assert s.verify_indexes()["index_ok"]                     # (and the shadow rebuild agrees)
    # another library loaded over this one (a backup import): only its own ids
    other = {f"n{i}": {"id": f"n{i}", "title": "n", "path": f"/n/{i}.mod",
                       "defect": "corrupt" if i < 3 else None} for i in range(10)}
    gen = s._cant_play_gen
    s.bulk_load(other, {}, {}, {}, {}, [], {}, {}, {})
    s.rebuild_indexes()
    assert s._cant_play_ids == {"n0", "n1", "n2"} and s._cant_play_gen != gen


@pytest.mark.asyncio
async def test_an_index_swap_installs_the_ids_and_moves_the_counter(monkeypatch):
    from soniqboom.core import data
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_tracks_batch([{"id": f"t{i}", "title": "x", "path": f"/m/{i}.mod",
                            "health": "broken" if i % 3 == 0 else None} for i in range(30)])
    s._cant_play_ids.add("ghost")                             # drift the swap heals
    gen = s._cant_play_gen
    report = await data.rebuild_indexes()
    assert any(m["index"] == "_cant_play_ids" for m in report["mismatches"])
    assert s._cant_play_ids == _brute_cant_play(s) and s._cant_play_gen != gen
    ids, _ver = availability.cant_play_ids(s)
    assert "ghost" not in ids and len(ids) == 10


def test_the_version_moves_only_when_the_ids_change():
    availability.reset_for_tests()
    s = TrackStore()
    s.upsert_tracks_batch([{"id": f"t{i}", "title": "x", "path": f"/m/{i}.mod"} for i in range(5)])
    s.update_track_fields("t1", {"health": "broken"})
    a, va = availability.cant_play_ids(s)
    assert a == {"t1"}
    assert availability.cant_play_ids(s) == (a, va) and availability.cant_play_ids(s)[0] is a
    # a re-upsert of the same broken track (a rescan) moves the counter, not the ids
    gen = s._cant_play_gen
    s.upsert_track({"id": "t1", "title": "x2", "path": "/m/1.mod", "health": "broken"})
    assert s._cant_play_gen != gen
    b, vb = availability.cant_play_ids(s)
    assert b is a and vb == va
    s.update_track_fields("t2", {"defect": "corrupt"})
    c, vc = availability.cant_play_ids(s)
    assert c == {"t1", "t2"} and vc != va
    s.update_track_fields("t2", {"defect": "partial"})       # partial plays
    assert availability.cant_play_ids(s)[0] == {"t1"}


# ── Mark / Hide on the lists ─────────────────────────────────────────────────

def _mark_some(lib):
    """Five tracks of root A that can't be played (two broken, one corrupt, one
    refused twice, one glitched — which plays) and one partial of root B."""
    s = lib.store
    a = lib.ids_a
    s.update_track_fields(a[0], {"health": "broken"})
    skip_rules.set_mark(s, a[1], "broken", note="crackles then stops")
    s.update_track_fields(a[2], {"defect": "corrupt", "defect_detail": "no engine decodes it"})
    s.update_track_fields(a[3], {"defect": "unplayable", "defect_detail": "uade stops"})
    s.update_track_fields(a[4], {"health": "glitched"})
    s.update_track_fields(lib.ids_b[0], {"defect": "partial"})
    return {a[0], a[1], a[2], a[3]}


@pytest.mark.asyncio
async def test_mark_lists_them_hide_leaves_them_out_with_exact_totals(ulib):
    cant = _mark_some(ulib)
    async with _client() as c:
        ulib.unplay("mark")                                    # the default: as before
        assert len(await _j(c, "/api/tracks?limit=500")) == 100
        assert (await _j(c, "/api/tracks/count"))["visible"] == 100
        assert len(await _j(c, "/api/search?q=zork&limit=200")) == 100
        ulib.unplay("hide")
        cnt = await _j(c, "/api/tracks/count")
        assert cnt == {"count": 100, "visible": 96}
        for sort in ("", "&sort=title&order=asc", "&sort=year&order=desc"):
            got = []
            for off in range(0, 110, 7):                       # odd page size: no holes, no repeats
                got += [r["id"] for r in await _j(c, f"/api/tracks?limit=7&offset={off}{sort}")]
            assert len(got) == 96 and not set(got) & cant, sort
        assert not {r["id"] for r in await _j(c, "/api/search?q=zork&limit=200")} & cant
        assert not {r["id"] for r in await _j(c, "/api/search/quick?q=song&limit=20")} & cant
        flt = await _j(c, "/api/search/filter?genre=Genre%200&limit=2000")
        assert flt and not {r["id"] for r in flt} & cant
        for view in ("recently-added", "unplayed"):
            rows = await _j(c, f"/api/smart/{view}?limit=500")
            assert len(rows) == 96 and not {r["id"] for r in rows} & cant, view
        # glitched and partial tracks play: listed with their badge fields
        rows = {r["id"]: r for r in await _j(c, "/api/tracks?limit=500")}
        assert rows[ulib.ids_a[4]]["health"] == "glitched"
        assert rows[ulib.ids_b[0]]["defect"] == "partial"
        # asked for by id (the queue, history, Track Info): still served, marked
        batch = (await c.post("/api/tracks/meta/batch", json={"ids": sorted(cant)})).json()
        assert {r["id"] for r in batch} == cant and all(r.get("health") or r.get("defect")
                                                        for r in batch)
        one = await _j(c, f"/api/tracks/{ulib.ids_a[2]}")
        assert one["defect"] == "corrupt"
        # and both Hide settings together leave out both sets
        ulib.offline()
        ulib.mode("hide")
        assert (await _j(c, "/api/tracks/count"))["visible"] == 56
        got = {r["id"] for r in await _j(c, "/api/tracks?limit=500")}
        assert got == set(ulib.ids_a) - cant


@pytest.mark.asyncio
async def test_a_mark_while_hide_is_on_moves_lists_and_counts_without_a_browse_write(ulib):
    """Regression: ``health`` / ``defect`` are browse-exempt — a mark moves no
    ``_browse_seq`` — so the warm list, count and legend caches must follow the
    hidden ids' version instead."""
    s = ulib.store
    ulib.unplay("hide")
    async with _client() as c:
        counts0 = await _j(c, "/api/library/counts")
        genres0 = await _j(c, "/api/library/genres")
        page0 = await _j(c, "/api/tracks?limit=500")
        tok0 = (await _j(c, "/api/library/availability"))["token"]
        assert counts0["visible"] == 100 == len(page0)
        seq = s._browse_seq
        target = ulib.ids_a[10]
        genre = s.get_track(target)["genre"][0].lower()
        skip_rules.set_mark(s, target, "broken")
        assert s._browse_seq == seq                            # no browse write…
        counts1 = await _j(c, "/api/library/counts")
        assert counts1["visible"] == 99 and counts1["tracks"] == 100   # …yet the counts follow
        genres1 = {g["genre"].lower(): g["count"] for g in await _j(c, "/api/library/genres")}
        assert genres1[genre] == {g["genre"].lower(): g["count"] for g in genres0}[genre] - 1
        page1 = await _j(c, "/api/tracks?limit=500")
        assert len(page1) == 99 and target not in {r["id"] for r in page1}
        st = await _j(c, "/api/library/availability")
        assert st["token"] != tok0 and st["cant_play"] == 1 and st["unplayable_mode"] == "hide"
        # an automatic "can't play" mark found at play time, then cleared by a play
        s.update_track_fields(ulib.ids_a[11], {"defect": "unplayable"})
        assert (await _j(c, "/api/library/counts"))["visible"] == 98
        s.update_track_fields(ulib.ids_a[11], {"defect": None})
        skip_rules.set_mark(s, target, None)
        assert (await _j(c, "/api/library/counts")) == counts0
        assert (await _j(c, "/api/library/availability"))["token"] != st["token"]
        # Mark mode: a mark changes no list — the token stays (only ``marks`` moves)
        ulib.unplay("mark")
        tok_m = (await _j(c, "/api/library/availability"))["token"]
        marks = (await _j(c, "/api/library/availability"))["marks"]
        skip_rules.set_mark(s, target, "broken")
        st_m = await _j(c, "/api/library/availability")
        assert st_m["token"] == tok_m and st_m["marks"] != marks
        assert (await _j(c, "/api/library/counts"))["visible"] == 100


@pytest.mark.asyncio
async def test_playlists_folders_and_the_duplicates_view_still_list_them_marked(ulib, monkeypatch):
    from soniqboom.api import playlist as playlist_api
    from soniqboom.api import users as users_api
    cant = _mark_some(ulib)
    s = ulib.store
    entries = [ulib.ids_a[0], ulib.ids_a[5], ulib.ids_a[2]]
    s.create_playlist("pl1", "Mixed", track_ids=entries)
    s.create_playlist("pl2", "Smart", query="zork")
    # a duplicate group whose primary is broken
    s.update_track_fields_batch([(ulib.ids_a[0], {"duplicate_group_id": "gx",
                                                  "is_duplicate_primary": True}),
                                 (ulib.ids_a[6], {"duplicate_group_id": "gx",
                                                  "is_duplicate_primary": False})])
    ulib.unplay("hide")
    app = _app()
    app.dependency_overrides[users_api.current_user] = lambda: None
    monkeypatch.setattr(playlist_api, "_can_read", lambda pl, u: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        pl = (await c.get("/api/playlists/pl1")).json()
        assert [t["id"] for t in pl["tracks"]] == entries
        assert [t.get("health") or t.get("defect") for t in pl["tracks"]] == ["broken", None, "corrupt"]
        sp = (await c.get("/api/playlists/pl2")).json()
        assert len(sp["tracks"]) == 100                         # a smart playlist hides nothing
        d = f"{ulib.root_a}/d0"
        rows = (await c.get(f"/api/fstree/tracks-with-meta?path={d}&recursive=false")).json()
        assert {r["id"] for r in rows} >= cant                  # the open folder lists them
        dup = (await c.get("/api/smart/duplicates")).json()
        flat = json.dumps(dup)
        assert ulib.ids_a[0] in flat and ulib.ids_a[6] in flat


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mark", "hide"])
async def test_dedup_lists_the_copy_that_plays_in_place_of_a_broken_primary(ulib, mode):
    s = ulib.store
    p, cp = ulib.ids_a[20], ulib.ids_a[21]
    s.update_track_fields_batch([(p, {"duplicate_group_id": "g1", "is_duplicate_primary": True}),
                                 (cp, {"duplicate_group_id": "g1", "is_duplicate_primary": False})])
    s.set_config("filter_duplicates", True)
    library.invalidate_agg_cache()
    ulib.unplay(mode)
    async with _client() as c:
        before = await _j(c, "/api/library/counts")
        assert before["visible"] == 99
        s.update_track_fields(p, {"defect": "corrupt"})
        ids = {r["id"] for r in await _j(c, "/api/tracks?limit=500")}
        cnt = await _j(c, "/api/library/counts")
        if mode == "hide":
            assert p not in ids and cp in ids                    # the copy that plays stands in
        else:
            assert p in ids and cp not in ids                    # Mark: as before
        assert len(ids) == cnt["visible"] == 99
        hits = {r["id"] for r in await _j(c, "/api/search?q=zork&limit=200")}
        assert hits == ids
        # every copy broken: the group keeps its primary out of sight in Hide
        s.update_track_fields(cp, {"health": "broken"})
        ids2 = {r["id"] for r in await _j(c, "/api/tracks?limit=500")}
        if mode == "hide":
            assert not {p, cp} & ids2 and len(ids2) == 98
        else:
            assert p in ids2 and len(ids2) == 99
        assert (await _j(c, "/api/library/counts"))["visible"] == len(ids2)


@pytest.mark.asyncio
async def test_dedup_a_hidden_broken_primary_hands_over_to_a_copy_on_an_offline_drive(ulib):
    """Regression (independent review): offline Mark lists a copy on an
    offline drive, dimmed; with its group's primary broken and hidden, the
    deduped lists show that copy — the group never vanishes."""
    s = ulib.store
    p, cp = ulib.ids_a[30], ulib.ids_b[30]                      # primary on A, its copy on B
    s.update_track_fields_batch([(p, {"duplicate_group_id": "gq", "is_duplicate_primary": True}),
                                 (cp, {"duplicate_group_id": "gq", "is_duplicate_primary": False})])
    s.update_track_fields(p, {"health": "broken"})
    ulib.offline()                                               # B: the copy can't play either
    ulib.mode("mark")
    ulib.unplay("hide")
    async with _client() as c:
        for dedup in (False, True):
            s.set_config("filter_duplicates", dedup)
            library.invalidate_agg_cache()
            rows = {r["id"]: r for r in await _j(c, "/api/tracks?limit=500")}
            assert p not in rows and rows[cp]["offline"] == "drive", dedup
            assert (await _j(c, "/api/library/counts"))["visible"] == len(rows), dedup
            hits = {r["id"] for r in await _j(c, "/api/search?q=zork&limit=200")}
            assert cp in hits and p not in hits, dedup
        # offline Hide too: the whole group is hidden (both members are)
        ulib.mode("hide")
        rows = {r["id"] for r in await _j(c, "/api/tracks?limit=500")}
        assert not {p, cp} & rows
        # the drive back: the copy that plays stands in, listed unmarked
        ulib.online()
        rows = {r["id"]: r for r in await _j(c, "/api/tracks?limit=500")}
        assert p not in rows and cp in rows and "offline" not in rows[cp]


def test_the_incremental_swap_with_a_hidden_set_equals_a_full_build():
    """``TrackStore.primary_swap(unplayable, hidden)`` after writes (only the
    touched groups worked out again) equals a full build, and a new hidden
    set is never answered from the memo of another."""
    r = random.Random(4)
    s = TrackStore()
    rows = []
    for g in range(60):
        k = r.randint(2, 4)
        prim = r.randrange(k)
        for j in range(k):
            rows.append({"id": f"g{g}m{j}", "title": f"T{g}", "path": f"/m/{g}/{j}.x",
                         "scan_root_hash": r.choice(["ra", "rb"]), "duplicate_group_id": f"G{g}",
                         "is_duplicate_primary": j == prim, "format": r.choice(["MP3", "FLAC"]),
                         "bitrate": r.choice([128, 900]), "added_at": r.randint(0, 99)})
    s.upsert_tracks_batch(rows)

    def sets():
        off = frozenset(s._tag_scan_root_hash.get("rb", set()))
        cant = frozenset(s._cant_play_ids)
        # (offline Mark, can't be played Hide: hidden ⊂ unplayable)
        return off | cant, cant
    un, hid = sets()
    for step in range(60):
        ids = sorted(s._tracks)
        op = r.choice(["mark", "unmark", "flip", "fields", "add", "del"])
        if op == "mark":
            s.update_track_fields(r.choice(ids), {"health": "broken"})
        elif op == "unmark":
            s.update_track_fields(r.choice(ids), {"health": None})
        elif op == "flip":
            gid = r.choice(sorted(s._tag_dup_group))
            members = sorted(s._tag_dup_group[gid])
            win = r.choice(members)
            s.update_track_fields_batch([(m, {"is_duplicate_primary": m == win}) for m in members])
        elif op == "fields":
            s.update_track_fields(r.choice(ids), {"bitrate": r.choice([1, 999]),
                                                  "added_at": r.randint(0, 999)})
        elif op == "add":
            g = r.randrange(60)
            s.upsert_track({"id": f"x{step}", "title": f"T{g}", "path": f"/m/x{step}.x",
                            "scan_root_hash": r.choice(["ra", "rb"]), "duplicate_group_id": f"G{g}",
                            "is_duplicate_primary": False, "format": "MP3", "bitrate": 1,
                            "added_at": step, "health": r.choice([None, "broken"])})
        else:
            s.delete_track_ids(r.sample(ids, 1))
        if r.random() < 0.5:                                    # sometimes the same sets stay
            un, hid = sets()
        inc = s.primary_swap(un, hid)
        full = TrackStore()
        full._tracks = s._tracks
        full._tag_dup_group = s._tag_dup_group
        ref = full.primary_swap(un, hid)
        assert (inc.stand_in if inc else None) == (ref.stand_in if ref else None), (step, op)
        # every swapped-in member is listed: plays, or at least is not hidden
        for prim, stand in (inc.stand_in.items() if inc else ()):
            assert prim in hid or prim in un
            assert stand not in hid, (step, op, prim, stand)
    # the same unplayable set with another hidden set: never the memo's answer
    s.primary_swap(un, hid)
    for other in (None, frozenset(), frozenset(list(hid)[:1])):
        full = TrackStore()
        full._tracks = s._tracks
        full._tag_dup_group = s._tag_dup_group
        got, ref = s.primary_swap(un, other), full.primary_swap(un, other)
        assert (got.stand_in if got else None) == (ref.stand_in if ref else None), other


# ── the setting, the state, the notification, Subsonic ──────────────────────

@pytest.mark.asyncio
async def test_the_setting_through_the_admin_api_ui_config_and_state(ulib, monkeypatch):
    from soniqboom import main
    from soniqboom.api import admin
    calls = []
    monkeypatch.setattr(availability, "notify_changed", lambda: calls.append(1))
    got = await admin.get_settings(_tok="t")
    assert got["unplayable_tracks"] == "mark"                   # the default
    assert (await main.ui_config())["unplayable_tracks"] == "mark"
    await admin.update_settings({"unplayable_tracks": "hide"}, _tok="t")
    assert ulib.store.get_config(availability.UNPLAY_KEY) == "hide" and calls == [1]
    for bogus in ("show", "bogus", None, 3):                   # ignored (no Show for this one)
        await admin.update_settings({"unplayable_tracks": bogus}, _tok="t")
    assert availability.unplay_mode() == "hide" and calls == [1]
    await admin.update_settings({"unplayable_tracks": "hide"}, _tok="t")   # no change, no event
    assert calls == [1]
    assert (await main.ui_config())["unplayable_tracks"] == "hide"
    await admin.update_settings({"unplayable_tracks": "MARK"}, _tok="t")
    assert availability.unplay_mode() == "mark" and calls == [1, 1]
    # the offline setting is untouched by it, and vice versa
    assert availability.mode() == "mark"
    ulib.store.set_config(availability.UNPLAY_KEY, "garbage")
    assert availability.unplay_mode() == "mark"


@pytest.mark.asyncio
async def test_a_mark_while_hide_is_on_is_broadcast_once_things_settle(ulib, monkeypatch):
    sent = []

    async def fake_broadcast(data):
        sent.append(data)
    monkeypatch.setattr(library, "_broadcast", fake_broadcast)
    monkeypatch.setattr(availability, "_NOTIFY_DELAY_S", 0.01)
    monkeypatch.setattr(availability, "WATCH_INTERVAL_S", 3600)
    s = ulib.store
    availability.install(s)
    try:
        skip_rules.set_mark(s, ulib.ids_a[0], "broken")       # Mark mode: no list changes
        await asyncio.sleep(0.1)
        assert sent == []
        ulib.unplay("hide")
        availability.notify_changed()                          # (as the admin API does)
        await asyncio.sleep(0.1)
        assert len(sent) == 1 and sent[0]["event"] == "availability_changed"
        skip_rules.set_mark(s, ulib.ids_a[1], "broken")
        s.update_track_fields(ulib.ids_a[2], {"defect": "unplayable"})    # a burst: one event
        await asyncio.sleep(0.1)
        assert len(sent) == 2 and sent[1]["token"] == availability.token()
        # a rescan that keeps the ids (the same broken track re-upserted): nothing
        t = copy.deepcopy(s.get_track(ulib.ids_a[1]))
        s.upsert_track(t)
        await asyncio.sleep(0.1)
        assert len(sent) == 2
        s.update_track_fields(ulib.ids_a[2], {"defect": None})
        await asyncio.sleep(0.1)
        assert len(sent) == 3
    finally:
        availability.uninstall()
        s._on_scan_dirs_changed = None
        s._on_cant_play_changed = None


@pytest.mark.asyncio
async def test_during_a_scan_only_its_end_is_told(ulib, monkeypatch):
    """A scan (batch mode) that finds files that can't be played moves the
    token, but open pages are told once the batch is over — not after every
    commit."""
    sent = []

    async def fake_broadcast(data):
        sent.append(data)
    monkeypatch.setattr(library, "_broadcast", fake_broadcast)
    monkeypatch.setattr(availability, "_NOTIFY_DELAY_S", 0.01)
    monkeypatch.setattr(availability, "WATCH_INTERVAL_S", 3600)
    s = ulib.store
    ulib.unplay("hide")
    availability.install(s)
    try:
        tok0 = availability.token()
        s.enter_batch_mode()
        for i in range(3):
            s.upsert_tracks_batch([_row(f"{ulib.root_a}/scan/{i}.mod", ulib.root_a, 700 + i,
                                        defect="corrupt")])
            await asyncio.sleep(0.05)
        assert availability.token() != tok0 and sent == []     # moved, not told yet
        s.exit_batch_mode()
        await availability._broadcast_if_changed()               # (the watcher's next round)
        assert len(sent) == 1 and sent[0]["token"] == availability.token()
        # outside a scan a mark is told at once
        skip_rules.set_mark(s, ulib.ids_a[0], "broken")
        await asyncio.sleep(0.1)
        assert len(sent) == 2
        # a drive going away during a scan is still told at once
        s.enter_batch_mode()
        ulib.offline()
        await asyncio.sleep(0.1)
        s.exit_batch_mode()
        assert len(sent) == 3
    finally:
        availability.uninstall()
        s._on_scan_dirs_changed = None
        s._on_cant_play_changed = None


def test_a_reader_that_froze_an_older_state_never_overwrites_a_newer_one(monkeypatch):
    """Two readers: the one that read the counter first finishes last — its
    older copy must not replace the newer state (nor take a new version)."""
    availability.reset_for_tests()
    s = TrackStore()
    s.upsert_tracks_batch([{"id": f"t{i}", "title": "x", "path": f"/m/{i}.mod"} for i in range(4)])
    s.update_track_fields("t1", {"health": "broken"})
    old_gen, stale = s._cant_play_gen, set(s._cant_play_ids)   # what the slow reader saw
    s.update_track_fields("t2", {"health": "broken"})
    new_ids, new_ver = availability.cant_play_ids(s)            # the fast reader: newer state
    assert new_ids == {"t1", "t2"}
    monkeypatch.setattr(s, "cant_play_ids", lambda: (old_gen, stale))
    assert availability.cant_play_ids(s) == (new_ids, new_ver)  # the slow one finishing
    monkeypatch.undo()
    assert availability.cant_play_ids(s) == (new_ids, new_ver)


from test_subsonic_parity import env  # noqa: E402,F401 — fixture


def test_subsonic_lists_follow_no_hide_setting(env, monkeypatch):  # noqa: F811
    """As with "Tracks on unavailable drives": Subsonic listings show every
    track (a client can still pick one on purpose); its generated lists
    (random songs) already pass them by (core/skip_rules)."""
    monkeypatch.setattr(store_mod, "_store", env.store)
    skip_rules.reset_for_tests()
    availability.reset_for_tests()
    env.store.set_config(availability.UNPLAY_KEY, "hide")
    env.store.update_track_fields("q2", {"health": "broken"})
    env.store.update_track_fields("q3", {"defect": "corrupt"})
    alb_id = env.ok("getSong.view", id="q2")["song"]["albumId"]
    assert {"q2", "q3"} <= {s["id"] for s in env.ok("getAlbum.view", id=alb_id)["album"]["song"]}
    hits = env.ok("search3.view", query="", songCount=500)["searchResult3"].get("song", [])
    assert {"q2", "q3"} <= {s["id"] for s in hits}
    songs = env.ok("getRandomSongs.view", size=500)["randomSongs"]["song"]
    assert not {"q2", "q3"} & {s["id"] for s in songs}
    skip_rules.reset_for_tests()
    availability.reset_for_tests()


# ── seeded randomized: every combination vs a brute-force filter ─────────────

def _expected_visible(s, dead: set, off_mode: str, un_mode: str, dedup: bool) -> set:
    """What the web lists show, worked out over the tracks alone."""
    off = {tid for tid, t in s._tracks.items() if t.get("scan_root_hash") in dead}
    cant = _brute_cant_play(s)
    hidden = (off if off_mode == "hide" else set()) | (cant if un_mode == "hide" else set())
    if not dedup:
        return set(s._tracks) - hidden
    stand = (off if off_mode != "show" else set()) | (cant if un_mode == "hide" else set())
    groups: dict = {}
    for tid, t in s._tracks.items():
        if t.get("duplicate_group_id"):
            groups.setdefault(t["duplicate_group_id"].strip().lower(), []).append(tid)
    promoted, demoted = set(), set()
    for members in groups.values():
        if len(members) < 2:
            continue
        prims = [m for m in members if s._tracks[m].get("is_duplicate_primary", True)]
        if not prims or not all(p in stand for p in prims):
            continue
        alts = [m for m in members if m not in stand]               # a copy that plays…
        if not alts and all(p in hidden for p in prims):
            alts = [m for m in members if m not in hidden]          # …else one still listed
        if alts:
            demoted.update(prims)
            promoted.add(min(alts, key=lambda m: primary_sort_key(s._tracks[m])))
    shown = {tid for tid, t in s._tracks.items()
             if tid in promoted or (t.get("is_duplicate_primary", True) and tid not in demoted)}
    return shown - hidden


def _legends_of(tracks: list[dict]) -> dict:
    """The legends of a store holding exactly ``tracks`` (nothing hidden)."""
    f = TrackStore()
    f.upsert_tracks_batch([copy.deepcopy(t) for t in tracks])
    low = lambda rows, k: {str(r[k]).lower(): r["count"] for r in rows}  # noqa: E731
    return {
        "artists": low(f.aggregate_artists(), "artist"),
        "genres": low(f.aggregate_genres(), "genre"),
        "years": {r["year"]: r["count"] for r in f.aggregate_years()},
        "formats": low(f.aggregate_formats(), "format"),
        "albums": low(f.aggregate_albums(), "album"),
        "no_artist": f.untagged_count("artist"),
    }


async def _check(c, s, dead, off_mode, un_mode, dedup, tag):
    want = _expected_visible(s, dead, off_mode, un_mode, dedup)
    full = [r["id"] for r in await _j(c, "/api/tracks?limit=1000")]
    assert set(full) == want and len(full) == len(want), (tag, set(full) ^ want)
    paged = []
    for off in range(0, len(want) + 13, 13):
        paged += [r["id"] for r in await _j(c, f"/api/tracks?limit=13&offset={off}&sort=title")]
    assert set(paged) == want and len(paged) == len(want), tag
    assert (await _j(c, "/api/tracks/count"))["visible"] == len(want), tag
    hits = [r["id"] for r in await _j(c, "/api/search?q=zork&limit=200")]
    assert set(hits) == {t for t in want if "zork" in (s._tracks[t].get("title") or "")}, tag
    counts = await _j(c, "/api/library/counts")
    exp = _legends_of([s._tracks[t] for t in want])
    arts = await _j(c, "/api/library/artists")
    assert {a["artist"].lower(): a["count"] for a in arts if not a.get("label")} == exp["artists"], tag
    assert sum(a["count"] for a in arts if a.get("label")) == exp["no_artist"], tag
    gen = await _j(c, "/api/library/genres")
    assert {g["genre"].lower(): g["count"] for g in gen} == exp["genres"], tag
    yrs = await _j(c, "/api/library/years")
    assert {y["year"]: y["count"] for y in yrs} == exp["years"], tag
    fmts = await _j(c, "/api/library/formats")
    assert {f["format"].lower(): f["count"] for f in fmts} == exp["formats"], tag
    albs = await _j(c, "/api/library/albums")
    assert {a["album"].lower(): a["count"] for a in albs} == exp["albums"], tag
    assert counts["visible"] == len(want) and counts["tracks"] == len(s._tracks), tag
    assert counts["artists"] == len(exp["artists"]) and counts["genres"] == len(exp["genres"]), tag
    assert counts["years"] == len(exp["years"]) and counts["albums"] == len(exp["albums"]), tag
    st = await _j(c, "/api/library/availability")
    assert st["cant_play"] == len(_brute_cant_play(s)) and st["unplayable_mode"] == un_mode, tag
    return want


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [21, 22, 23])
async def test_seeded_random_marks_under_writes_match_brute_force(ulib, monkeypatch, seed):
    r = random.Random(seed)
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", r.choice([5, 20]))
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", r.choice([0, 15]))
    s = ulib.store
    roots = (ulib.root_a, ulib.root_b)
    # duplicate groups spanning both drives
    nxt = 2000
    for g in range(8):
        k = r.randint(2, 3)
        prim = r.randrange(k)
        for j in range(k):
            root = r.choice(roots)
            s.upsert_track(_row(f"{root}/dup/g{g}_{j}.mod", root, nxt, title=f"Dup {g} zork",
                                artist=f"Dup Artist {g}", duplicate_group_id=f"G{g}",
                                is_duplicate_primary=(j == prim),
                                format=r.choice(["MP3", "FLAC", "ProTracker"]),
                                bitrate=r.choice([128, 320, 900])))
            nxt += 1
    # a few tracks that can't be played from the start, a duplicate primary among them
    prims = [t for t, x in s._tracks.items() if x.get("duplicate_group_id")
             and x.get("is_duplicate_primary")]
    s.update_track_fields(r.choice(prims), {"defect": "corrupt"})
    for t in r.sample(ulib.ids_a + ulib.ids_b, 4):
        skip_rules.set_mark(s, t, "broken")
    dead: set = set()
    stats = {"cant_hidden": 0, "swapped": 0, "both_hidden": 0}
    async with _client() as c:
        for step in range(18):
            ids = sorted(s._tracks)
            op = r.choice(["mark", "unmark", "defect", "undefect", "drive", "add", "del",
                           "reupsert", "retag", "rebuild", "prim", "drive", "prim"])
            if op == "prim":                                   # a duplicate's primary (or copy)
                grouped = [t for t in ids if s._tracks[t].get("duplicate_group_id")]
                t = r.choice(grouped)
                if r.random() < 0.5:
                    skip_rules.set_mark(s, t, r.choice(["broken", None]))
                else:
                    s.update_track_fields(t, {"defect": r.choice([*CANT, None])})
            elif op == "mark":
                skip_rules.set_mark(s, r.choice(ids), r.choice(["broken", "broken", "glitched"]))
            elif op == "unmark":
                marked = [t for t in ids if s._tracks[t].get("health")] or ids
                skip_rules.set_mark(s, r.choice(marked), None)
            elif op == "defect":
                s.update_track_fields_batch([(t, {"defect": r.choice([*CANT, "partial", "empty"])})
                                             for t in r.sample(ids, 3)])
            elif op == "undefect":
                bad = [t for t in ids if s._tracks[t].get("defect")] or ids
                s.update_track_fields(r.choice(bad), {"defect": None, "defect_detail": None})
            elif op == "drive":
                if dead:
                    ulib.online()
                    dead = set()
                else:
                    ulib.offline()
                    dead = {ph(ulib.root_b)}
            elif op == "add":
                root = r.choice(roots)
                s.upsert_tracks_batch([_row(f"{root}/new/{nxt + j}.mod", root, nxt + j,
                                            health=r.choice([None, "broken"]),
                                            defect=r.choice([None, "corrupt"]))
                                       for j in range(3)])
                nxt += 3
            elif op == "del":
                s.delete_track_ids(r.sample(ids, 2))
            elif op == "reupsert":
                t = copy.deepcopy(s.get_track(r.choice(ids)))
                t["health"] = r.choice([t.get("health"), "broken", None])
                s.upsert_track(t)
            elif op == "retag":
                s.update_track_fields(r.choice(ids), {"artist": r.choice(["Artist 1", "", "Zed"]),
                                                      "genre": [r.choice(["Genre 0", "New"])]})
            else:
                s.rebuild_indexes()
            assert s._cant_play_ids == _brute_cant_play(s), (step, op)
            # every combination, with the caches warm from the step before
            combos = [(o, u, d) for o in ("mark", "hide", "show") for u in ("mark", "hide")
                      for d in (False, True)]
            r.shuffle(combos)
            for off_mode, un_mode, dedup in combos:
                ulib.mode(off_mode)
                ulib.unplay(un_mode)
                if bool(s.get_config("filter_duplicates", False)) != dedup:
                    s.set_config("filter_duplicates", dedup)
                    library.invalidate_agg_cache()             # (as the admin API does)
                want = await _check(c, s, dead, off_mode, un_mode, dedup,
                                    f"seed{seed} step{step}:{op} {off_mode}/{un_mode}/dedup={dedup}")
                if un_mode == "hide" and _brute_cant_play(s):
                    stats["cant_hidden"] += 1
                    stats["both_hidden"] += bool(off_mode == "hide" and dead)
                plain = {t for t, x in s._tracks.items() if x.get("is_duplicate_primary", True)}
                stats["swapped"] += bool(dedup and un_mode == "hide" and want - plain)
    # (the run went through what it is about)
    assert stats["cant_hidden"] >= 50 and stats["swapped"] >= 3 and stats["both_hidden"] >= 3, stats
