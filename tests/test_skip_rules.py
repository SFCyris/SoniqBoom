# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Skip rules (core/skip_rules.py) — GitHub #36 and #37.

* #36: a listener with edit rights reports a track broken or glitched; a
  broken track (and an undecodable one) is passed by whenever auto-play
  moves on by itself — shuffles, ``playable=1`` pages, radio, Subsonic's
  generated lists — while albums / searches still list it; the report is
  carried over rescans and set on identical copies.
* #37: "Skip short tunes" passes by a tune whose own length is KNOWN (HVSC)
  to be shorter than the setting — a short default tune skips the file,
  Subsonic's per-tune listings and album counts leave short tunes out; an
  unknown length is never skipped.
"""
from __future__ import annotations

import types

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import admin, tracks as tracks_api, users as users_api
from soniqboom.core import availability, skip_rules
from soniqboom.core import store as store_mod
from soniqboom.core import subsonic_index as sx
from soniqboom.core.store import TrackStore

EDITOR = types.SimpleNamespace(id="e1", username="ed", display_name="Ed", role="edit", enabled=True)
READER = types.SimpleNamespace(id="r1", username="ro", display_name=None, role="readonly", enabled=True)


def _t(tid, **kw):
    d = {"id": tid, "title": tid.upper(), "artist": "A", "album": "Al", "genre": [],
         "duration": 100.0, "format": "MOD", "path": f"/music/{tid}.mod", "added_at": 1}
    d.update(kw)
    return d


def _sid(tid, lengths, *, subsongs=None, start=None, **kw):
    return _t(tid, format="SID", path=f"/music/{tid}.sid", hvsc_lengths=lengths,
              subsongs=subsongs if subsongs is not None else len(lengths),
              start_subsong=start, **kw)


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(skip_rules, "_STALE_OK_S", 0.0)       # (see the staleness test)
    monkeypatch.setattr(skip_rules, "_BACKGROUND", False)
    availability.reset_for_tests()
    skip_rules.reset_for_tests()
    yield s
    availability.reset_for_tests()
    skip_rules.reset_for_tests()


# ── which files auto-play passes by ──────────────────────────────────────────

def test_broken_and_corrupt_files_are_passed_by_glitched_and_partial_are_not(store):
    for t in (_t("ok"), _t("br", health="broken"), _t("gl", health="glitched"),
              _t("co", defect="corrupt"), _t("pa", defect="partial")):
        store.upsert_track(t)
    assert skip_rules.skip_ids(store) == {"br", "co"}


def test_short_default_tunes_need_the_setting_and_a_known_length(store):
    store.upsert_track(_sid("sfx", [3.0]))                     # a 3 s sound effect
    store.upsert_track(_sid("song", [180.0]))
    store.upsert_track(_sid("nolen", [0.0]))                   # HVSC has no length for it
    store.upsert_track(_t("raw", format="SID", path="/m/raw.sid", duration=180.0))  # placeholder
    assert skip_rules.skip_ids(store) == frozenset()           # off by default
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert skip_rules.skip_ids(store) == {"sfx"}               # unknown lengths never skip
    store.set_config(skip_rules.SHORT_KEY, 5)
    assert skip_rules.skip_ids(store) == {"sfx"}
    store.set_config(skip_rules.SHORT_KEY, 0)
    assert skip_rules.skip_ids(store) == frozenset()


def test_the_default_tune_decides_for_the_file(store):
    # Tune 2 (index 1) is the start song: wire 0 plays it.
    store.upsert_track(_sid("multi", [200.0, 4.0, 60.0], start=1))
    store.upsert_track(_sid("multi2", [200.0, 4.0, 60.0]))   # default tune 1: long
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert skip_rules.skip_ids(store) == {"multi"}


def test_the_skip_set_follows_writes_and_the_setting(store):
    store.upsert_track(_t("a"))
    assert skip_rules.skip_ids(store) == frozenset()
    store.update_track_fields("a", {"health": "broken"})        # warm memo, then a write
    assert skip_rules.skip_ids(store) == {"a"}
    store.update_track_fields("a", {"health": None})
    assert skip_rules.skip_ids(store) == frozenset()
    # An HVSC length arriving later (a duration-only-style write) counts too.
    store.upsert_track(_sid("s", [0.0]))
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert skip_rules.skip_ids(store) == frozenset()
    store.update_track_fields("s", {"hvsc_lengths": [2.0]})
    assert skip_rules.skip_ids(store) == {"s"}


def test_library_writes_are_seen_within_the_window_marks_and_the_setting_at_once(store, monkeypatch):
    monkeypatch.setattr(skip_rules, "_STALE_OK_S", 60.0)
    store.upsert_track(_t("a"))
    store.upsert_track(_t("b"))
    assert skip_rules.skip_ids(store) == frozenset()
    store.update_track_fields("a", {"defect": "corrupt"})       # a scan write: may wait
    assert skip_rules.skip_ids(store) == frozenset()
    skip_rules.set_mark(store, "b", "broken")                  # a mark: at once
    assert skip_rules.skip_ids(store) == {"a", "b"}
    store.upsert_track(_sid("s", [2.0]))
    store.set_config(skip_rules.SHORT_KEY, 10)                 # the setting: at once
    assert skip_rules.skip_ids(store) == {"a", "b", "s"}


def test_after_library_writes_the_old_set_serves_while_a_thread_rebuilds(store, monkeypatch):
    import time as _time
    monkeypatch.setattr(skip_rules, "_BACKGROUND", True)
    store.upsert_track(_t("a"))
    assert skip_rules.skip_ids(store) == frozenset()
    store.update_track_fields("a", {"defect": "corrupt"})       # a scan write
    assert skip_rules.skip_ids(store) == frozenset()            # served at once, as it was
    for _ in range(100):
        if skip_rules.skip_ids(store) == {"a"}:
            break
        _time.sleep(0.01)
    assert skip_rules.skip_ids(store) == {"a"}                  # the thread's rebuild


def test_normalize_short_only_takes_the_offered_choices():
    assert [skip_rules.normalize_short(v) for v in (0, 5, "10", 20, 30)] == [0, 5, 10, 20, 30]
    assert [skip_rules.normalize_short(v) for v in (7, -5, "x", None, 3600)] == [0, 0, 0, 0, 0]


# ── availability: shuffles, pages, radio ─────────────────────────────────────

def test_unplayable_adds_the_skipped_files_in_every_mode(store):
    for i in range(5):
        store.upsert_track(_t(f"t{i}"))
    store.update_track_fields("t2", {"health": "broken"})
    for mode in ("mark", "hide", "show"):
        store.set_config(availability.MODE_KEY, mode)
        assert availability.unplayable(store) == {"t2"}, mode


def test_a_playable_page_leaves_them_out_with_exact_positions(store):
    rows = [_t(f"t{i}") for i in range(6)]
    for r in rows:
        store.upsert_track(dict(r))
    store.update_track_fields("t1", {"health": "broken"})
    store.update_track_fields("t2", {"defect": "corrupt"})
    live = [store.get_track(r["id"]) for r in rows]
    page = availability.player_page(lambda off, lim: live[off:off + lim], 0, 3, store)
    assert [(r["id"], r["_pos"]) for r in page["tracks"]] == [("t0", 0), ("t3", 3), ("t4", 4)]
    assert page["next_offset"] == 5 and page["ended"] is False
    # Nothing skipped: the plain page.
    store.update_track_fields("t1", {"health": None})
    store.update_track_fields("t2", {"defect": None})
    page = availability.player_page(lambda off, lim: live[off:off + lim], 0, 3, store)
    assert [r["id"] for r in page["tracks"]] == ["t0", "t1", "t2"]


def test_a_mark_moves_the_marks_generation_the_setting_moves_the_token(store):
    store.upsert_track(_t("a"))
    t0, m0 = availability.token(store), availability.state(store)["marks"]
    skip_rules.set_mark(store, "a", "broken", by="Ed")
    # (A mark changes no list: open pages only re-check the rows they hold.)
    assert availability.token(store) == t0 and availability.state(store)["marks"] != m0
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert availability.token(store) != t0
    assert availability.state(store)["skip_short_tunes"] == 10
    assert availability.state(store)["token"] == availability.token(store)


# ── marks ────────────────────────────────────────────────────────────────────

def test_a_mark_is_set_on_identical_copies_only(store):
    store.upsert_track(_t("a", file_md5="m1", duplicate_group_id="g"))
    store.upsert_track(_t("b", file_md5="m1", duplicate_group_id="g", path="/other/a.mod"))
    store.upsert_track(_t("c", file_md5="m2", duplicate_group_id="g", path="/x/a.xm"))  # same song, other file
    ids = skip_rules.set_mark(store, "a", "glitched", note="  clicks   at 1:23 ", by="Ed",
                              renderer="openmpt123 0.8.9")
    assert sorted(ids) == ["a", "b"]
    a = store.get_track("a")
    assert a["health"] == "glitched" and a["health_note"] == "clicks at 1:23"
    assert a["health_by"] == "Ed" and a["health_renderer"] == "openmpt123 0.8.9" and a["health_at"] > 0
    assert store.get_track("b")["health"] == "glitched" and not store.get_track("c").get("health")
    skip_rules.set_mark(store, "a", None)
    assert not store.get_track("a").get("health") and not store.get_track("b").get("health")
    assert store.get_track("a").get("health_note") is None


def test_a_sid_copy_matches_by_its_sid_md5(store):
    store.upsert_track(_sid("s1", [100.0], sid_md5="x", duplicate_group_id="g"))
    store.upsert_track(_sid("s2", [100.0], sid_md5="x", duplicate_group_id="g"))
    assert sorted(skip_rules.set_mark(store, "s1", "broken")) == ["s1", "s2"]


def test_a_mark_survives_a_rescan(store):
    old = _t("a", health="broken", health_note="silent", health_by="Ed", health_at=5.0,
             health_renderer="uade123 3.05")
    new = _t("a")                                               # the fresh extract
    store_mod._carry_enrichment(old, new)
    assert (new["health"], new["health_note"], new["health_by"]) == ("broken", "silent", "Ed")
    # The file's bytes changed (a fixed copy at the same path): the mark goes.
    old_m = _t("m", file_md5="aaa", health="broken", health_by="Ed")
    fixed = _t("m", file_md5="bbb")
    store_mod._carry_enrichment(old_m, fixed)
    assert not fixed.get("health")
    same = _t("m", file_md5="aaa")
    store_mod._carry_enrichment(old_m, same)
    assert same["health"] == "broken"
    fresh = _t("a", health="glitched")                          # (never overwritten)
    store_mod._carry_enrichment(old, fresh)
    assert fresh["health"] == "glitched"


def test_the_note_is_bounded():
    f = skip_rules.mark_fields("broken", "x" * 500, "Ed", None)
    assert len(f["health_note"]) == skip_rules.NOTE_MAX
    assert skip_rules.mark_fields("bogus", "n", "Ed", "r") == {k: None for k in skip_rules.HEALTH_FIELDS}


# ── the API ──────────────────────────────────────────────────────────────────

def _client(who):
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")

    async def _cur():
        return who["u"]
    app.dependency_overrides[users_api.current_user] = _cur
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_reporting_needs_edit_rights_and_records_who_and_which_renderer(store, monkeypatch):
    store.upsert_track(_t("a"))
    store.upsert_track(_t("b"))

    async def _label(key):
        return f"{key} 9.9"
    monkeypatch.setattr(admin, "renderer_label", _label)
    who = {"u": READER}
    async with _client(who) as c:
        r = await c.put("/api/tracks/a/health", json={"state": "broken"})
        assert r.status_code == 403 and not store.get_track("a").get("health")
        who["u"] = EDITOR
        r = await c.put("/api/tracks/a/health", json={"state": "broken", "note": "only noise"})
        assert r.status_code == 200
        j = r.json()
        assert j["ids"] == ["a"] and j["fields"]["health"] == "broken"
        assert j["fields"]["health_by"] == "Ed" and j["fields"]["health_renderer"] == "openmpt123 9.9"
        assert (await c.put("/api/tracks/a/health", json={"state": "meh"})).status_code == 422
        assert (await c.put("/api/tracks/zz/health", json={"state": "broken"})).status_code == 404
        # The queue's re-check reads the marks back with the offline flags.
        r = await c.post("/api/tracks/meta/availability", json={"ids": ["a", "b"]})
        h = r.json()["health"]
        assert set(h) == {"a"} and h["a"]["health"] == "broken" and h["a"]["health_note"] == "only noise"
        r = await c.put("/api/tracks/a/health", json={"state": None})
        assert r.status_code == 200 and r.json()["fields"]["health"] is None
    assert skip_rules.skip_ids(store) == frozenset()


def test_renderer_key_follows_the_file_suffix():
    from soniqboom.api import stream
    k = stream.renderer_key_for
    assert k({"path": "/m/a.sid"}) == "sidplayfp"
    assert k({"path": "/m/a.mod"}) == "openmpt123"
    assert k({"path": "/m/a.spc"}) == "gme"
    assert k({"path": "/m/a.minigsf"}) == "zxtune123"
    assert k({"path": "/m/a.sndh"}) == "psgplay"
    assert k({"path": "/m/a.hvl"}) == "hvl2wav"
    assert k({"path": "/m/a.mid"}) == "fluidsynth"
    assert k({"path": "/m/a.zip::x/b.ahx"}) == "uade123"
    assert k({"path": "/m/a.mp3"}) is None
    assert k(None) is None


# ── settings ─────────────────────────────────────────────────────────────────

def test_coverage_counts_known_tunes_of_every_retro_format():
    tracks = [_sid("a", [3.0, 120.0, 8.0]), _sid("b", [0.0, 25.0], subsongs=2, duration=180.0),
              _t("m"),                                          # a 100 s module: known
              _t("raw", format="SID", path="/m/raw.sid", subsongs=4, duration=180.0),
              _t("mp3", format="MP3", path="/m/a.mp3", duration=4.0)]   # ordinary audio: not a tune
    c = skip_rules.coverage(tracks)
    assert c["tunes"] == 3 + 2 + 1 + 4 and c["known"] == 3 + 1 + 1
    assert c["short"] == {5: 1, 10: 2, 20: 2, 30: 3}


def test_every_retro_format_counts_ordinary_audio_never(store):
    store.set_config(skip_rules.SHORT_KEY, 10)
    for t in (_t("mod", duration=4.0),                                      # a jingle module
              _t("ym", path="/m/a.ym", format="YM", duration=6.5),
              _t("psf", path="/m/a.minipsf", format="PSF", duration=3.0),
              _t("uade", path="/m/mod.jingle", format="Pro", genre=["Amiga", "Module"], duration=2.0),
              _t("dc", path="/m/a.dsf", format="DSF (Dreamcast)", duration=5.0),
              _t("mp3", path="/m/skit.mp3", format="MP3", duration=8.0),      # an album interlude
              _t("dsd", path="/m/a.dsf", format="DSD", duration=8.0),
              _t("adl", path="/m/a.rad", format="AdLib", duration=180.0),     # placeholder
              _t("long", duration=200.0)):
        store.upsert_track(t)
    assert skip_rules.skip_ids(store) == {"mod", "ym", "psf", "uade", "dc"}


def test_stated_per_tune_lengths_count_like_hvsc(store):
    # An SNDH: tune 2 (wire 1) is a 3 s jingle; the default tune 40 s.
    t = _t("sn", path="/m/a.sndh", format="SNDH", duration=40.0, subsongs=3,
           tune_lengths=[40.0, 3.0, 0.0])
    store.upsert_track(t)
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert skip_rules.known_len(t, 1) == 3.0 and skip_rules.known_len(t, 2) == 0.0
    assert sx.listed_tunes(t) == [2] and sx.bare_listed(t)
    assert sx.wire_length(t, 1) == 3.0                           # Subsonic reports it too
    assert "sn" not in skip_rules.skip_ids(store)


# ── Subsonic (#37: the client queues what it is shown) ──────────────────────

def test_short_tunes_are_left_out_of_the_per_tune_listing(store):
    t = _sid("s", [120.0, 3.0, 90.0, 2.0, 0.0])
    store.upsert_track(t)
    assert sx.listed_tunes(t) == [1, 2, 3, 4]
    assert sx.tune_count(t) == 5
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert sx.listed_tunes(t) == [2, 4]                         # (wire 4: unknown length — kept)
    assert sx.tune_count(t) == 3
    # (the bare id's own duration — the file's stored 100 s — plus tune 3's)
    assert sx.tune_duration_sum(t) == pytest.approx(100.0 + 90.0)


def test_a_short_default_tune_leaves_the_files_own_entry_out(store):
    t = _sid("d", [3.0, 120.0, 2.0, 90.0])                     # default tune 1: 3 s
    store.upsert_track(t)
    assert sx.bare_listed(t) and sx.tune_count(t) == 4
    store.set_config(skip_rules.SHORT_KEY, 10)
    assert sx.listed_tunes(t) == [1, 3] and not sx.bare_listed(t)
    assert sx.tune_count(t) == 2
    assert sx.tune_duration_sum(t) == pytest.approx(120.0 + 90.0)
    # Every tune short: the file keeps its entry (it never vanishes).
    allshort = _sid("e", [3.0, 2.0])
    store.upsert_track(allshort)
    assert sx.listed_tunes(allshort) == [] and sx.bare_listed(allshort)
    assert sx.tune_count(allshort) == 1


def test_the_short_filter_runs_before_the_cap(store):
    lengths = [100.0] + [2.0] * 70 + [100.0] * 80
    t = _sid("big", lengths)
    store.upsert_track(t)
    store.set_config(skip_rules.SHORT_KEY, 10)
    ws = sx.listed_tunes(t)
    assert len(ws) == sx.SUB_CAP - 1 and min(ws) == 71        # the first long ones after the SFX


# ── Subsonic endpoints ───────────────────────────────────────────────────────

from test_subsonic_parity import _tr, env, users  # noqa: E402,F401 — fixtures


@pytest.fixture
def senv(env, monkeypatch):
    # (``listed_tunes`` reads the setting from THE store.)
    monkeypatch.setattr(store_mod, "_store", env.store)
    skip_rules.reset_for_tests()
    yield env
    skip_rules.reset_for_tests()


def test_generated_lists_leave_out_broken_files_albums_still_list_them(senv):
    env = senv
    env.store.update_track_fields("q2", {"health": "broken"})
    env.store.update_track_fields("q3", {"defect": "corrupt"})
    for _ in range(5):
        songs = env.ok("getRandomSongs.view", size=500)["randomSongs"]["song"]
        ids = {s["id"] for s in songs}
        assert "q2" not in ids and "q3" not in ids and "q1" in ids
        g = env.ok("getRandomSongs.view", size=500, genre="Rock")["randomSongs"]["song"]
        assert {s["id"] for s in g} & {"q2", "q3"} == set() and g
    top = env.ok("getTopSongs.view", artist="Queen", count=50)["topSongs"]["song"]
    assert {s["id"] for s in top} & {"q2", "q3"} == set() and top
    # Picking it on purpose still works: the album and a search list it.
    alb_id = env.ok("getSong.view", id="q2")["song"]["albumId"]
    assert "q2" in [s["id"] for s in env.ok("getAlbum.view", id=alb_id)["album"]["song"]]


def test_album_tune_listing_and_counts_follow_the_setting_at_once(senv):
    env = senv
    d = "/music/C64/MUSICIANS/Sfx_Composer"
    t = _tr("sx1", title="Game", artist="Sfx Composer", album="Game", dir_path=d,
            store=env.store, fmt="SID")
    t.update(subsongs=4, hvsc_lengths=[100.0, 3.0, 80.0, 2.0])
    d2 = "/music/C64/MUSICIANS/Sfx_Composer"
    t2 = _tr("sx2", title="Jingles", artist="Sfx Composer", album="Game", dir_path=d2,
             store=env.store, fmt="SID")
    t2.update(subsongs=3, hvsc_lengths=[2.0, 60.0, 4.0])        # default tune: 2 s
    env.store.upsert_track(t2)
    env.store.upsert_track(t)
    alb = env.ok("getSong.view", id="sx1")["song"]["albumId"]

    def listing():
        a = env.ok("getAlbum.view", id=alb)["album"]
        return a["songCount"], [s["id"] for s in a["song"]]
    assert listing() == (7, ["sx1", "sx1~1", "sx1~2", "sx1~3", "sx2", "sx2~1", "sx2~2"])
    env.store.set_config(skip_rules.SHORT_KEY, 10)               # no rebuild delay needed
    assert listing() == (3, ["sx1", "sx1~2", "sx2~1"])
    # The file stays reachable by its own id (playlists, stars, a saved queue).
    assert env.ok("getSong.view", id="sx2")["song"]["id"] == "sx2"
    env.store.set_config(skip_rules.SHORT_KEY, 0)
    assert listing()[0] == 7


@pytest.mark.asyncio
async def test_the_setting_takes_only_the_offered_choices(store, monkeypatch):
    saved = {}

    async def _set(key, value):
        store.set_config(key, value)
        saved[key] = value
    from soniqboom.core import data
    monkeypatch.setattr(data, "set_config", _set)
    await admin.update_settings({"skip_short_tunes": 10}, _tok="x")
    assert skip_rules.short_secs(store) == 10
    for bad in (7, "x", None, -5):
        await admin.update_settings({"skip_short_tunes": bad}, _tok="x")
        assert skip_rules.short_secs(store) == 10, bad
    await admin.update_settings({"skip_short_tunes": 0}, _tok="x")
    assert skip_rules.short_secs(store) == 0


def test_a_file_that_went_away_and_came_back_keeps_its_mark(store):
    store.upsert_track(_t("a"))                                  # no checksum: by id
    store.upsert_track(_t("m", file_md5="mm"))
    skip_rules.set_mark(store, "a", "broken", note="n", by="Ed")
    skip_rules.set_mark(store, "m", "glitched", by="Ed")
    store.delete_track("a")
    store.delete_track("m")
    store.upsert_tracks_batch([_t("a")])                         # the scan finds it again
    assert store.get_track("a")["health"] == "broken" and store.get_track("a")["health_note"] == "n"
    # Moved: a new id, the same bytes.
    store.upsert_track(_t("m2", file_md5="mm", path="/elsewhere/m.mod"))
    assert store.get_track("m2")["health"] == "glitched"
    # A cleared mark stays cleared.
    skip_rules.set_mark(store, "a", None)
    store.delete_track("a")
    store.upsert_tracks_batch([_t("a")])
    assert not store.get_track("a").get("health")
    # A fresh extract that carries its own state is never overwritten.
    store.upsert_track(_t("m3", file_md5="mm", health="broken", path="/x/m.mod"))
    assert store.get_track("m3")["health"] == "broken"


def test_a_changed_file_that_comes_back_is_not_marked_again(store):
    store.upsert_track(_t("m", file_md5="aaa"))
    skip_rules.set_mark(store, "m", "broken")
    store.delete_track("m")
    store.upsert_tracks_batch([_t("m", file_md5="fixed")])      # same path, new bytes
    assert not store.get_track("m").get("health")


def test_a_new_note_keeps_who_reported_it_and_when(store):
    store.upsert_track(_t("a"))
    skip_rules.set_mark(store, "a", "glitched", note="one", by="Alice", renderer="uade123 3.05")
    first = dict(store.get_track("a"))
    skip_rules.set_mark(store, "a", "glitched", note="two", by="Bob", renderer="uade123 3.06")
    t = store.get_track("a")
    assert t["health_note"] == "two"
    assert (t["health_by"], t["health_at"], t["health_renderer"]) == (
        first["health_by"], first["health_at"], first["health_renderer"])
    skip_rules.set_mark(store, "a", "broken", by="Bob")         # another report: Bob's
    assert store.get_track("a")["health_by"] == "Bob"


def test_random_songs_stay_full_when_many_files_are_skipped(senv):
    env = senv
    for i in range(30):
        env.store.upsert_track(_tr(f"z{i}", title=f"Z{i}", artist="Zed", genre=["Rock"], store=env.store))
    for i in range(25):
        skip_rules.set_mark(env.store, f"z{i}", "broken")
    rock = [t for t in env.store._tracks.values() if "Rock" in (t.get("genre") or [])
            and t.get("health") != "broken" and t.get("defect") != "corrupt"]
    size = min(8, len(rock))
    for _ in range(5):
        g = env.ok("getRandomSongs.view", size=size, genre="Rock")["randomSongs"]["song"]
        assert len(g) == size and not any(s["id"].startswith("z") and int(s["id"][1:]) < 25 for s in g)
        u = env.ok("getRandomSongs.view", size=size)["randomSongs"]["song"]
        assert len(u) == size


# ── per-tune lengths the file states (all retro formats) ─────────────────────

from pathlib import Path  # noqa: E402

_SQ = Path(__file__).resolve().parent.parent / "internal/testdata/atari/sndh/Crowe_Mark__Space_Quest_Chapter_1.sndh"


def _psgplay():
    from soniqboom.core.metadata import _find_atari_binary
    return _find_atari_binary("psgplay_path", "psgplay")


@pytest.mark.skipif(not _SQ.exists(), reason="local SNDH fixture missing")
def test_the_scan_records_an_sndhs_tune_lengths():
    if not _psgplay():
        pytest.skip("psgplay missing")
    from soniqboom.core import metadata
    d = metadata._extract_sndh(_SQ, "sq")
    lens = d["tune_lengths"]
    assert d["subsongs"] == 46 == len(lens)
    assert lens[d["start_subsong"]] == d["duration"] == 48.0          # the default tune
    assert sum(1 for v in lens if 0 < v < 10) >= 40                    # its sound effects


@pytest.mark.asyncio
@pytest.mark.skipif(not _SQ.exists(), reason="local SNDH fixture missing")
async def test_files_scanned_before_get_their_tune_lengths_once(store):
    if not _psgplay():
        pytest.skip("psgplay missing")
    from soniqboom.core import repair
    store.upsert_track(_t("sq", path=str(_SQ), format="SNDH", subsongs=46, start_subsong=9, duration=48.0))
    store.upsert_track(_t("one", path="/m/x.sndh", format="SNDH"))      # one tune: nothing to read
    store.upsert_track(_t("gone", path="/nowhere/y.sndh", format="SNDH", subsongs=3))  # unreadable
    r = await repair.fill_tune_lengths(store)
    assert r["checked"] == 2 and r["updated"] == 1
    assert len(store.get_track("sq")["tune_lengths"]) == 46
    assert store.get_track("gone").get("tune_lengths") is None          # tried again next start
    assert (await repair.fill_tune_lengths(store))["checked"] == 1
    store.set_config(skip_rules.SHORT_KEY, 10)
    t = store.get_track("sq")
    assert not skip_rules.tune_is_short(t, 0, 10)                       # the 48 s default tune plays
    assert len(sx.listed_tunes(t)) < 10                                 # most sound effects left out


def test_a_tune_is_judged_before_its_fade_and_a_plain_play_by_the_tune_it_plays(store):
    store.set_config(skip_rules.SHORT_KEY, 10)
    nsfe = _t("n", format="NSFe", path="/m/a.nsfe", subsongs=3, duration=128.0,
              tune_lengths=[128.0, 10.0, 9.0])
    assert skip_rules.tune_is_short(nsfe, 1, 10)                  # 2 s + the 8 s fade
    assert skip_rules.known_len(nsfe, 2) == 1.0
    psf = _t("p", format="PSF", path="/m/a.minipsf", duration=12.0, fade_s=10.0)
    assert skip_rules.known_len(psf, 0) == 2.0
    gme = _t("g", format="AY", path="/m/a.ay", subsongs=3, default_subsong=1, duration=136.0,
             tune_lengths=[13.0, 136.0, 9.0])
    assert not skip_rules.tune_is_short(gme, 0, 10)               # the plain play: tune 2
    remix = _t("x", format="MP3", path="/m/remix.mp3", genre=["Amiga", "Module"], duration=5.0)
    assert not skip_rules.is_tune_file(remix)
    for t in (nsfe, psf, gme, remix):
        store.upsert_track(t)
    assert skip_rules.skip_ids(store) == {"p"}


def test_a_default_tune_length_arriving_later_refreshes_subsonic_counts(store):
    t = _t("u", path="/m/mod.x", genre=["Amiga", "Module"], subsongs=3, duration=0.0)
    store.upsert_track(t)
    before = store._catalog_seq
    store.update_track_fields("u", {"duration": 6.0})             # a render backfill
    assert store._catalog_seq > before
    before = store._catalog_seq
    store.update_track_fields("u", {"tune_lengths": [6.0, 90.0, 0.0]})
    assert store._catalog_seq > before



@pytest.mark.asyncio
async def test_a_psf_rips_fade_is_recorded_for_files_scanned_before(store, tmp_path):
    from soniqboom.core import repair
    f = tmp_path / "a.minipsf"
    f.write_bytes(b"PSF\x01" + b"\x00" * 12 + b"[TAG]length=0:02\nfade=0:10\n")
    store.upsert_track(_t("p", path=str(f), format="PSF", duration=12.0))
    await repair.fill_tune_lengths(store)
    assert store.get_track("p")["fade_s"] == 10.0
    store.set_config(skip_rules.SHORT_KEY, 5)
    assert "p" in skip_rules.skip_ids(store)                      # 2 s before its fade


# ── sub-second console effects; earlier default render lengths ──────────────

import struct  # noqa: E402


def _nsfe(path, times_ms):
    def chunk(cid, data):
        return struct.pack("<I", len(data)) + cid + data
    info = struct.pack("<HHHBBBB", 0x8000, 0x8000, 0x8001, 0, 0, len(times_ms), 0)
    code = bytes([0x60, 0x60]) + bytes(254)
    times = struct.pack(f"<{len(times_ms)}i", *times_ms)
    path.write_bytes(b"NSFE" + chunk(b"INFO", info) + chunk(b"DATA", code)
                     + chunk(b"time", times) + chunk(b"NEND", b""))
    return path


def _gme():
    from soniqboom.core import gme_render
    if not gme_render.is_available():
        pytest.skip("libgme missing")
    return gme_render


def test_a_sub_second_stated_length_is_known(store, tmp_path):
    gr = _gme()
    f = _nsfe(tmp_path / "fx.nsfe", [2000, 600, 120000])
    lens = gr.tune_lengths(f.read_bytes(), 3)
    assert lens == [10.0, 8.6, 128.0]                              # (+ the 8 s fade)
    t = _t("fx", path=str(f), format="NSFe", subsongs=3, duration=10.0, tune_lengths=lens)
    assert skip_rules.known_len(t, 1) == pytest.approx(0.6)
    assert skip_rules.tune_is_short(t, 1, 5)


@pytest.mark.asyncio
async def test_libgme_files_read_before_are_read_again_once(store, tmp_path, monkeypatch):
    _gme()
    from soniqboom.core import conversion_cache, repair
    purged = []

    async def _purge(ids, fmt):
        purged.append((sorted(ids), fmt))
        return 0
    monkeypatch.setattr(conversion_cache, "purge_entries_for", _purge)
    multi = _nsfe(tmp_path / "m.nsfe", [600, 2000, 120000])
    single = _nsfe(tmp_path / "s.nsfe", [400])
    # As the old floor recorded them: the 0.6 s tune unknown, the default
    # tune (that one) left at the default length; the single one unread.
    store.upsert_track(_t("m", path=str(multi), format="NSFe", subsongs=3, duration=180.0,
                          tune_lengths=[0.0, 10.0, 128.0]))
    store.upsert_track(_t("s", path=str(single), format="NSFe", duration=180.0))
    r = await repair.fill_tune_lengths(store)
    assert r["rerender"] == 2
    m, s1 = store.get_track("m"), store.get_track("s")
    assert m["tune_lengths"] == [8.6, 10.0, 128.0] and m["duration"] == 8.6
    assert s1["duration"] == 8.4 and s1["tune_lengths"] == [8.4]
    assert purged == [(["m", "s"], "gme")]
    # Once: the next start reads nothing again.
    assert (await repair.fill_tune_lengths(store))["checked"] == 0
    store.set_config(skip_rules.SHORT_KEY, 5)
    assert {"m", "s"} <= skip_rules.skip_ids(store)


def test_every_default_render_length_used_so_far_means_unknown(store, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 25)
    skip_rules.note_default_length(store)
    monkeypatch.setattr(settings, "sid_default_duration", 60)      # changed in Settings
    skip_rules.note_default_length(store, previous=25.0)
    assert {25.0, 60.0, 180.0} <= set(skip_rules.placeholder_seconds(store))
    store.set_config(skip_rules.SHORT_KEY, 30)
    store.upsert_track(_t("old", path="/m/a.spc", format="SPC", duration=25.0))   # stored at the old default
    store.upsert_track(_t("real", path="/m/b.mod", duration=25.3))               # a genuine 25.3 s tune
    assert skip_rules.skip_ids(store) == {"real"}
    # A hand edit of the conf while the server was stopped: the last run's
    # default is remembered at the next start.
    monkeypatch.setattr(settings, "sid_default_duration", 90)
    skip_rules.note_default_length(store)
    assert {25.0, 60.0, 90.0} <= set(skip_rules.placeholder_seconds(store))


def test_a_played_libgme_files_render_length_is_not_reduced_by_a_fade(store):
    # Its first play stored the render's length (35.11 s — it ended by itself).
    t = _t("v", path="/m/a.vgz", format="VGM", duration=35.11)
    assert skip_rules.known_len(t, 0) == pytest.approx(35.11)
    assert not skip_rules.tune_is_short(t, 0, 30)
    # Its stated length (recorded at scan / by the start pass) wins, fade off.
    t["tune_lengths"] = [13.0]
    assert skip_rules.known_len(t, 0) == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_single_libgme_files_record_their_stated_length_keeping_a_played_duration(store, tmp_path, monkeypatch):
    _gme()
    from soniqboom.core import conversion_cache, repair
    monkeypatch.setattr(conversion_cache, "purge_entries_for", lambda ids, fmt: _noop_purge())
    f = _nsfe(tmp_path / "p.nsfe", [5000])
    store.upsert_track(_t("p", path=str(f), format="NSFe", duration=5.11))   # played: its render's length
    await repair.fill_tune_lengths(store)
    t = store.get_track("p")
    assert t["tune_lengths"] == [13.0] and t["duration"] == 5.11
    assert skip_rules.known_len(t, 0) == pytest.approx(5.0)


async def _noop_purge():
    return 0


@pytest.mark.asyncio
async def test_the_libgme_reread_is_done_once_even_with_a_file_offline(store, tmp_path, monkeypatch):
    _gme()
    from soniqboom.core import repair
    ok = _nsfe(tmp_path / "ok.nsfe", [600, 2000])
    store.upsert_track(_t("ok", path=str(ok), format="NSFe", subsongs=2, duration=180.0,
                          tune_lengths=[0.0, 10.0]))
    store.upsert_track(_t("gone", path="/Volumes/NotMounted/x.nsfe", format="NSFe", subsongs=2,
                          duration=180.0, tune_lengths=[0.0, 10.0]))
    await repair.fill_tune_lengths(store)
    assert store.get_config(repair.GME_REREAD_KEY) == repair.GME_REREAD_REV
    assert (await repair.fill_tune_lengths(store))["checked"] == 0


@pytest.mark.asyncio
async def test_no_libgme_file_is_read_without_libgme(store, tmp_path, monkeypatch):
    from soniqboom.core import gme_render, repair
    monkeypatch.setattr(gme_render, "is_available", lambda: False)
    f = tmp_path / "a.spc"
    f.write_bytes(b"x" * 100)
    store.upsert_track(_t("s", path=str(f), format="SPC", duration=180.0))
    assert (await repair.fill_tune_lengths(store))["checked"] == 0
