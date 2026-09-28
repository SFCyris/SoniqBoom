# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subsonic round-2 review fixes (GitHub #14 follow-ups).

Same harness as tests/test_subsonic_parity.py: the real ``/rest`` router over
ASGI against a hand-built ``TrackStore`` with per-user state in a tmp file.
Covers mixed-folder albums split per owner, folder names bounded by the scan
root, placeholder artists, the ``p=`` lockout scope, reportPlayback,
sonicSimilarity, per-tune ids of multi-tune files, music folders, JSONP
without the cookie, playlist fixes and the protocol edge cases.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import subsonic
from soniqboom.core import subsonic_index as sx
from soniqboom.core import subsonic_state
from soniqboom.core.store import TrackStore

from test_subsonic_parity import (  # noqa: F401 — env / users are fixtures
    GALWAY_DIR, MODS_DIR, _REAL_REQUIRE_NO_COOKIE, _REAL_REQUIRE_USER,
    _all_artists, _tr, env, users,
)

MIXED = "/music/mods/letters/T"


def _real_auth(monkeypatch):
    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", _REAL_REQUIRE_NO_COOKIE)


def _add(env, **kw):
    kw.setdefault("store", env.store)
    env.store.upsert_track(_tr(**kw))


def _mixed(env) -> str:
    """A letter bucket: Alpha 3, Beta 3, Gamma 4 (33 %), 2 owner-less."""
    for i in range(3):
        _add(env, tid=f"ta{i}", title=f"A{i}", artist="Alpha", added=200 + i,
             dir_path=MIXED, fmt="MOD")
        _add(env, tid=f"tb{i}", title=f"B{i}", artist="Beta", added=210 + i,
             dir_path=MIXED, fmt="MOD")
    for i in range(4):
        _add(env, tid=f"tc{i}", title=f"C{i}", artist="Gamma", added=220 + i,
             dir_path=MIXED, fmt="MOD")
    for i in range(2):
        _add(env, tid=f"tx{i}", title=f"X{i}", added=230 + i, dir_path=MIXED, fmt="MOD")
    return env.store.store_hash_lookup(MIXED)


# ── r2-sub-1: mixed folders split per owner ─────────────────────────────────

def test_mixed_folder_is_one_album_per_owner(env):
    dh = _mixed(env)
    env.folder_albums(True)
    slices = {o: sx.folder_album_id(dh, o) for o in ("alpha", "beta", "gamma", "")}
    total = 0
    for name, low in (("Alpha", "alpha"), ("Beta", "beta"), ("Gamma", "gamma")):
        art = env.ok("getArtist.view", id=sx.artist_id(name))["artist"]
        assert [a["id"] for a in art["album"]] == [slices[low]], name
        alb = env.ok("getAlbum.view", id=slices[low])["album"]
        assert alb["artist"] == name and {s["artist"] for s in alb["song"]} == {name}
        total += alb["songCount"]
        # Folder browsing: the artist's slice, none of the others' songs.
        d = env.ok("getMusicDirectory.view", id=sx.artist_id(name))["directory"]
        assert [c["id"] for c in d["child"]] == [slices[low]]
    unk = env.ok("getArtist.view", id=sx.artist_id(""))["artist"]
    assert slices[""] in [a["id"] for a in unk["album"]]
    total += env.ok("getAlbum.view", id=slices[""])["album"]["songCount"]
    assert total == 12                                  # the slices partition the folder
    # Nobody's page lists the whole folder; the bare id isn't in the lists.
    lst = env.ok("getAlbumList2.view", type="alphabeticalByName", size=500)["albumList2"]["album"]
    ids = {a["id"] for a in lst}
    assert set(slices.values()) <= ids and "fa:" + dh not in ids
    # A song points at its own slice, which lists it.
    song = env.ok("getSong.view", id="ta0")["song"]
    assert song["albumId"] == slices["alpha"] and song["parent"] == slices["alpha"]
    assert "ta0" in [s["id"] for s in env.ok("getAlbum.view", id=song["albumId"])["album"]["song"]]
    # getIndexes albumCount == what getArtist returns.
    arts = _all_artists(env.ok("getArtists.view"))
    assert arts["Alpha"]["albumCount"] == 1


def test_mixed_folder_ids_resolve_cold_and_with_folder_albums_off(env):
    dh = _mixed(env)
    sid = sx.folder_album_id(dh, "beta")
    env.folder_albums(True)
    subsonic._ALBUM_LIST_CACHE.update(cat=None)          # cold: no catalogue yet
    alb = env.ok("getAlbum.view", id=sid)["album"]
    assert alb["songCount"] == 3 and alb["artist"] == "Beta"
    env.ok("getAlbum.view", id=sid)                      # warm
    env.folder_albums(False)                             # a cached id still resolves
    alb = env.ok("getAlbum.view", id=sid)["album"]
    assert [s["id"] for s in alb["song"]] == ["tb0", "tb1", "tb2"]
    assert all(s["albumId"] == sid for s in alb["song"])
    # The bare id keeps meaning the whole folder (starred / cached before).
    whole = env.ok("getAlbum.view", id="fa:" + dh)["album"]
    assert whole["songCount"] == 12
    r = env.client.get("/rest/getCoverArt.view", params={"id": sid})
    assert r.status_code == 200
    # Stars / ratings accept the slice id.
    env.ok("star.view", albumId=sid)
    assert [a["id"] for a in env.ok("getStarred2.view")["starred2"]["album"]] == [sid]


def test_single_owner_folder_keeps_one_album_and_appears_on(env):
    d = "/music/mods/maniacs"
    for i in range(3):
        _add(env, tid=f"m{i}", title=f"M{i}", artist="Maniacs", dir_path=d, added=300 + i)
    _add(env, tid="o1", title="O1", artist="Other One", dir_path=d, added=310)
    _add(env, tid="o2", title="O2", artist="Other Two", dir_path=d, added=311)
    env.folder_albums(True)
    fa = "fa:" + env.store.store_hash_lookup(d)                  # 60 %: one album
    assert [a["id"] for a in env.ok("getArtist.view", id=sx.artist_id("Maniacs"))
            ["artist"]["album"]] == [fa]
    for other in ("Other One", "Other Two"):
        assert [a["id"] for a in env.ok("getArtist.view", id=sx.artist_id(other))
                ["artist"]["album"]] == [fa]                      # appears-on kept


def test_catalogue_track_entry_matches_listing_for_every_album_less_track(env):
    _mixed(env)
    env.folder_albums(True)
    cat = subsonic._catalogue(env.store)
    for t in env.store._tracks.values():
        if (t.get("album") or "").strip() or not t.get("dir_hash"):
            continue
        e = cat.track_entry(t)
        assert e is not None, t["id"]
        tracks = subsonic._album_and_tracks(env.store, e.id)[1]
        assert t["id"] in [x["id"] for x in tracks], (t["id"], e.id)


# ── r2-sub-3 / r2-sub-33: folder names ───────────────────────────────────────

def _named(store, path):
    dh = store.store_hash_lookup(path)
    sx._FOLDER_NAMES.pop(dh, None)
    return sx.folder_album_name(store, dh)


def test_folder_names_stay_below_the_scan_root():
    st = TrackStore()
    st._aof = lambda *a, **k: None
    for root in ("/Volumes/Music/Tracker_SID", "/Volumes/Music/Tracker_SID/C64Music",
                 "ftp://10.0.0.88/Music/Demo"):
        st.upsert_scan_dir(root)
    assert _named(st, "/Volumes/Music/Tracker_SID/modarchive_2007/R") == "modarchive 2007 / R"
    games = _named(st, "/Volumes/Music/Tracker_SID/C64Music/GAMES/A-F")
    demos = _named(st, "/Volumes/Music/Tracker_SID/C64Music/DEMOS/A-F")
    assert games == "GAMES / A-F" and demos == "DEMOS / A-F"
    assert _named(st, "/Volumes/Music/Tracker_SID/C64Music") == "C64Music"   # the root itself
    assert _named(st, "/Volumes/Music/Tracker_SID/C64Music/MUSICIANS/H/Hubbard_Rob") \
        == "Hubbard Rob"
    # Archive-member dirs and remote roots name from below the root too.
    assert _named(st, "ftp://10.0.0.88/Music/Demo:/demos/pm_nost.zip::music/Title Tune") \
        == "Title Tune"
    assert "Volumes" not in _named(st, "/Volumes/Music/Tracker_SID/2")
    # The name memo follows the scan-root set.
    st.delete_scan_dir("/Volumes/Music/Tracker_SID/C64Music")
    assert sx.folder_album_name(st, st.store_hash_lookup(
        "/Volumes/Music/Tracker_SID/C64Music/GAMES/A-F")) == "GAMES / A-F"


def test_folder_owner_spelling_only_when_the_names_match(env):
    env.folder_albums(True)
    d1, d2 = "/music/C64/MUSICIANS/Hulsbeck_Chris", "/music/C64/MUSICIANS/Tel_Jeroen"
    _add(env, tid="c1x", title="Turrican", artist="Chris Hülsbeck", dir_path=d1, added=400)
    _add(env, tid="j1x", title="Cybernoid", artist="Rob Hubbard", dir_path=d2, added=401)
    fa1 = "fa:" + env.store.store_hash_lookup(d1)
    fa2 = "fa:" + env.store.store_hash_lookup(d2)
    assert env.ok("getAlbum.view", id=fa1)["album"]["name"] == "Chris Hülsbeck"
    assert env.ok("getSong.view", id="c1x")["song"]["album"] == "Chris Hülsbeck"
    assert env.ok("getAlbum.view", id=fa2)["album"]["name"] == "Tel Jeroen"   # not his folder
    unk = env.ok("getAlbum.view", id="fa:" + env.store.store_hash_lookup(MODS_DIR))["album"]
    assert unk["name"] == "unsorted"                                          # owner-less


# ── r2-sub-15: placeholder artists fold into [Unknown Artist] ────────────────

def test_placeholder_artists_are_unknown_artist(env):
    _add(env, tid="p1", title="Game Tune", artist="<?>", dir_path="/music/C64/GAMES/A-F",
         added=500, fmt="SID")
    _add(env, tid="p2", title="Other", artist="Unknown artist", album="Lost Album", added=501)
    arts = _all_artists(env.ok("getIndexes.view"), "indexes")
    assert "<?>" not in arts and "Unknown artist" not in arts
    unk = arts["[Unknown Artist]"]
    song = env.ok("getSong.view", id="p1")["song"]
    assert song["artist"] == "<?>" and song["artistId"] == unk["id"]
    assert song["albumArtists"] == [{"id": unk["id"], "name": "[Unknown Artist]"}]
    albums = env.ok("getArtist.view", id=unk["id"])["artist"]["album"]
    assert "Lost Album" in [a["name"] for a in albums]
    assert sx.norm_owner("  ??? ") == "" and sx.norm_owner("Various Artists") == "Various Artists"


# ── r2-sub-29: a track artist in someone's folder album appears on it ────────

def test_feat_track_artist_appears_on_folder_album(env):
    d = "/music/edm/alesso"
    _add(env, tid="al1", title="Heroes", artist="Alesso ft. Tove Lo", album_artist="Alesso",
         dir_path=d, added=600)
    _add(env, tid="al2", title="Years", artist="Alesso", album_artist="Alesso", dir_path=d,
         added=601)
    env.folder_albums(True)
    fa = "fa:" + env.store.store_hash_lookup(d)
    feat = sx.artist_id("alesso ft. tove lo")
    art = env.ok("getArtist.view", id=feat)["artist"]
    assert [a["id"] for a in art["album"]] == [fa] and art["albumCount"] == 1
    assert _all_artists(env.ok("getArtists.view"))["Alesso ft. Tove Lo"]["albumCount"] == 1
    assert [a["id"] for a in env.ok("getArtist.view", id=sx.artist_id("Alesso"))
            ["artist"]["album"]] == [fa]


# ── r2-sub-30: chunked build == one-shot build ───────────────────────────────

@pytest.mark.parametrize("folder_on", [False, True])
def test_chunked_build_is_identical_to_one_shot(env, folder_on):
    _mixed(env)

    def shape(cat):
        return ([e.id for e in cat.entries], sorted(cat.by_id), sorted(cat.by_key),
                {k: [e.id for e in v] for k, v in cat.artist_albums.items()},
                {k: [e.id for e in v] for k, v in cat.appears_on.items()},
                {k: sorted(e.id for e in v) for k, v in cat.genre_albums.items()},
                sorted(cat.union.display.items()), sorted(cat.unknown_loose))

    one = sx.build_catalogue(env.store, folder_on=folder_on)
    two = asyncio.run(sx.build_catalogue_async(env.store, folder_on=folder_on, chunk=3))
    assert shape(one) == shape(two)


# ── r2-sub-5: big listings serialise off the event loop ──────────────────────

@pytest.mark.parametrize("fmt", ["xml", "json", "jsonp"])
def test_ok_async_same_bytes_as_ok(fmt):
    payload = {"album": {"id": "al:x", "song": [{"id": str(i), "title": f"T{i}"}
                                                for i in range(600)]}}
    lyr = {"lyrics": {"artist": "a", "title": "t", "_text": "la la"}}
    for p in (payload, lyr):
        a = subsonic._ok(p, fmt=fmt).body
        b = asyncio.run(subsonic._ok_async(p, fmt=fmt, n_hint=10_000)).body
        assert a == b


def test_ok_async_offloads_big_payloads(monkeypatch):
    seen = []
    real = subsonic._envelope_to_xml
    monkeypatch.setattr(subsonic, "_envelope_to_xml",
                        lambda env_: seen.append(threading.current_thread().name) or real(env_))
    asyncio.run(subsonic._ok_async({"x": {"y": 1}}, fmt="xml", n_hint=10))
    asyncio.run(subsonic._ok_async({"x": {"y": 1}}, fmt="xml", n_hint=5000))
    assert seen[0] == threading.main_thread().name and seen[1] != threading.main_thread().name


def test_index_render_is_identical_after_the_threaded_build(env):
    r1 = env.get("getIndexes.view", f="xml").content
    subsonic._ALBUM_LIST_CACHE["cat"].memo.clear()
    r2 = env.get("getIndexes.view", f="xml").content
    assert r1 == r2 and b"Queen" in r1


# ── r2-sub-6: p= failures have their own lockout scope ───────────────────────

def test_stale_app_password_never_locks_the_web_login(env, users, monkeypatch):
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    users.update(bob.id, subsonic_password="oldAppPass9")
    users.update(bob.id, subsonic_password="newAppPass9")
    for _ in range(15):
        assert env.fail("ping.view", u="bob", p="oldAppPass9")["code"] == 40
    assert not users.is_locked("bob")                     # main scope untouched
    assert users.is_locked("bob", "p")
    assert users.authenticate("bob", "bobpass12") is not None       # web login fine
    tok = {"u": "bob", "s": "s1", "t": hashlib.md5(b"newAppPass9s1").hexdigest()}
    env.ok("ping.view", **tok)                            # token apps unaffected
    # While the p scope is locked its fast paths are refused too (they must
    # not become a guessing oracle) …
    assert env.fail("ping.view", u="bob", p="newAppPass9")["code"] == 40
    users._clear_failed_logins(users._lock_key("bob", "p"))
    env.ok("ping.view", u="bob", p="newAppPass9")         # … and work again after it


def test_p_scope_below_threshold_keeps_the_new_password_working(env, users, monkeypatch):
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    users.update(bob.id, subsonic_password="newAppPass9")
    for _ in range(5):
        env.fail("ping.view", u="bob", p="oldAppPass9")
    env.ok("ping.view", u="bob", p="newAppPass9")
    env.ok("ping.view", u="bob", p="bobpass12")           # login password via p= too
    assert not users.is_locked("bob")


# ── r2-sub-7: reportPlayback ─────────────────────────────────────────────────

def test_report_playback_drives_now_playing_and_counts_one_play(env, monkeypatch):
    subsonic._NOW_PLAYING.clear()
    base = env.store.get_play_stats("q1").get("count", 0) if hasattr(
        env.store, "get_play_stats") else (env.store._play_stats.get("q1") or {}).get("count", 0)
    clock = [time.time()]
    monkeypatch.setattr(subsonic.time, "time", lambda: clock[0])
    env.ok("reportPlayback.view", mediaId="q1", mediaType="song", positionMs=0,
           state="starting", c="Feishin")
    env.ok("reportPlayback.view", mediaId="q1", mediaType="song", positionMs=10_000,
           state="playing", playbackRate=1.0, c="Feishin")
    clock[0] += 5
    row = env.ok("getNowPlaying.view")["nowPlaying"]["entry"][0]
    assert row["state"] == "playing" and row["playbackRate"] == 1.0
    assert 14_900 <= row["positionMs"] <= 15_100             # projected
    plays = lambda: (env.store._play_stats.get("q1") or {}).get("count", 0)   # noqa: E731
    assert plays() == base                                   # 10 s: not yet a play
    env.ok("reportPlayback.view", mediaId="q1", mediaType="song", positionMs=31_000,
           state="paused", c="Feishin")
    assert plays() == base + 1
    clock[0] += 60
    row = env.ok("getNowPlaying.view")["nowPlaying"]["entry"][0]
    assert row["state"] == "paused" and row["positionMs"] == 31_000     # frozen
    env.ok("reportPlayback.view", mediaId="q1", mediaType="song", positionMs=60_000,
           state="playing", c="Feishin")
    env.ok("reportPlayback.view", mediaId="q1", mediaType="song", positionMs=90_000,
           state="stopped", c="Feishin")
    assert plays() == base + 1                                # once per start
    assert env.ok("getNowPlaying.view")["nowPlaying"]["entry"] == []
    # ignoreScrobble: now-playing only.
    env.ok("reportPlayback.view", mediaId="q2", mediaType="song", positionMs=40_000,
           state="playing", ignoreScrobble="true", c="Feishin")
    assert (env.store._play_stats.get("q2") or {}).get("count", 0) == 0
    assert env.fail("reportPlayback.view", mediaId="q2", mediaType="song", positionMs=1,
                    state="bogus")["code"] == 10
    assert env.fail("reportPlayback.view", mediaId="nope", mediaType="song", positionMs=1,
                    state="playing")["code"] == 70
    subsonic._NOW_PLAYING.clear()


def test_report_playback_refuses_cookie_only(env, monkeypatch):
    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", _REAL_REQUIRE_NO_COOKIE)
    monkeypatch.setattr(subsonic, "_resolve_user",
                        lambda request, sb_session, *a: env.user if sb_session == "c" else None)
    env.client.cookies.set("sb_session", "c")
    assert env.fail("reportPlayback.view", mediaId="q1", mediaType="song", positionMs=0,
                    state="starting")["code"] == 10


# ── r2-sub-8: sonicSimilarity ────────────────────────────────────────────────

def test_sonic_similar_tracks_and_path(env):
    m = env.ok("getSonicSimilarTracks.view", id="q2", count=5)["sonicMatch"]
    # The engine's score is rank-relative (top hit 1.0), not the spec's
    # absolute measure: every match says -1 ("not supported"); the ORDER
    # carries the ranking.
    sims = [x["similarity"] for x in m]
    assert m and all(s == -1 for s in sims)
    assert all("id" in x["entry"] for x in m) and len(m) <= 5
    # getSimilarSongs is the same engine, same order.
    same = env.ok("getSimilarSongs2.view", id="q2", count=5)["similarSongs2"]["song"]
    assert [s["id"] for s in same] == [x["entry"]["id"] for x in m]
    assert env.fail("getSonicSimilarTracks.view", id="nope")["code"] == 70
    assert env.fail("getSonicSimilarTracks.view", id=sx.artist_id("Queen"))["code"] == 70
    path = env.ok("findSonicPath.view", startSongId="q1", endSongId="h1", count=5)["sonicMatch"]
    ids = [x["entry"]["id"] for x in path]
    assert ids[0] == "q1" and ids[-1] == "h1" and len(ids) == len(set(ids)) <= 5
    assert all(x["similarity"] == -1 for x in path)
    assert env.fail("findSonicPath.view", startSongId="q1", endSongId="nope")["code"] == 70
    r = env.get("getSonicSimilarTracks.view", id="q2", f="xml")
    root = ET.fromstring(r.content)
    assert root.get("status") == "ok"
    assert {e.get("similarity") for e in root.findall("{*}sonicMatch")} == {"-1"}


# ── r2-sub-9: one Child per tune of a multi-tune file ────────────────────────

def _sid(env, subsongs=3):
    d = "/music/C64/MUSICIANS/Daglish_Ben"
    _add(env, tid="sid1", title="Trap", artist="Ben Daglish", dir_path=d, added=700, fmt="SID")
    t = dict(env.store.get_track("sid1"), subsongs=subsongs,
             hvsc_lengths=[100.0, 61.0, 42.0] + [30.0] * max(0, subsongs - 3))
    env.store.upsert_track(t)
    return "fa:" + env.store.store_hash_lookup(d)


def test_tunes_are_listed_in_album_and_directory(env):
    fa = _sid(env)
    for body, key in ((env.ok("getAlbum.view", id=fa)["album"], "song"),
                      (env.ok("getMusicDirectory.view", id=fa)["directory"], "child")):
        rows = body[key]
        assert [r["id"] for r in rows] == ["sid1", "sid1~1", "sid1~2"]
        assert [r["title"] for r in rows] == ["Trap", "Trap (Tune 2/3)", "Trap (Tune 3/3)"]
        assert [r["duration"] for r in rows] == [100, 61, 42]
        assert all(r["coverArt"] == "sid1" and r["albumId"] == fa for r in rows)
    assert env.ok("getSong.view", id="sid1~2")["song"]["title"] == "Trap (Tune 3/3)"
    assert env.fail("getSong.view", id="sid1~3")["code"] == 70        # out of range
    assert env.fail("getSong.view", id="sid1~x")["code"] == 70        # malformed
    # Searches are never expanded.
    found = env.ok("search3.view", query="trap", artistCount=0, albumCount=0)["searchResult3"]
    assert [s["id"] for s in found["song"]] == ["sid1"]


def test_tune_expansion_is_capped(env):
    fa = _sid(env, subsongs=200)
    rows = env.ok("getAlbum.view", id=fa)["album"]["song"]
    assert len(rows) == subsonic._SUB_CAP and rows[-1]["id"] == f"sid1~{subsonic._SUB_CAP - 1}"


def test_tune_id_streams_that_subsong(env, monkeypatch):
    _sid(env)
    from fastapi.responses import Response
    from soniqboom.api import stream as stream_mod
    seen = []

    async def _fake(**kw):
        seen.append((kw["track_id"], kw["subsong"]))
        return Response(b"wav", media_type="audio/wav")

    async def _fake_head(**kw):
        seen.append(("HEAD", kw["track_id"], kw["subsong"]))
        return Response(b"", media_type="audio/wav")

    monkeypatch.setattr(stream_mod, "stream_track", _fake)
    monkeypatch.setattr(stream_mod, "stream_track_head", _fake_head)
    assert env.client.get("/rest/stream.view", params={"id": "sid1~2"}).content == b"wav"
    env.client.get("/rest/stream.view", params={"id": "sid1"})
    env.client.head("/rest/stream.view", params={"id": "sid1~1"})
    env.client.get("/rest/download.view", params={"id": "sid1~1"})
    assert seen == [("sid1", 2), ("sid1", 0), ("HEAD", "sid1", 1), ("sid1", 1)]
    r = env.client.get("/rest/stream.view", params={"id": "sid1~x", "f": "json"})
    assert r.json()["subsonic-response"]["error"]["code"] == 70


def test_tune_ids_in_playlists_queue_stars_and_scrobbles(env, monkeypatch):
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: env.store)
    _sid(env)
    pl = env.ok("createPlaylist.view", name="Tunes", songId=["sid1~2", "q1", "nope"])["playlist"]
    assert [e["id"] for e in pl["entry"]] == ["sid1~2", "q1"]
    assert env.store._playlists[pl["id"]]["track_ids"] == [{"id": "sid1", "subsong": 2}, "q1"]
    # updatePlaylist (index-based removal of what getPlaylist showed; adds a tune).
    env.ok("updatePlaylist.view", playlistId=pl["id"], songIndexToRemove=1,
           songIdToAdd="sid1~1", name="Renamed")
    got = env.ok("getPlaylist.view", id=pl["id"])["playlist"]
    assert got["name"] == "Renamed" and [e["id"] for e in got["entry"]] == ["sid1~2", "sid1~1"]
    lists = {p["id"]: p for p in env.ok("getPlaylists.view")["playlists"]["playlist"]}
    assert lists[pl["id"]]["songCount"] == 2 and lists[pl["id"]]["duration"] == 42 + 61
    # createPlaylist with a playlistId replaces the songs (was a 500-style error).
    env.ok("createPlaylist.view", playlistId=pl["id"], songId=["q2"])
    assert [e["id"] for e in env.ok("getPlaylist.view", id=pl["id"])["playlist"]["entry"]] == ["q2"]
    # Queue keeps the tune; star / scrobble apply to the file.
    env.ok("savePlayQueue.view", id=["sid1~1", "q1"], current="sid1~1", position=5)
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert [e["id"] for e in pq["entry"]] == ["sid1~1", "q1"] and pq["current"] == "sid1~1"
    env.ok("star.view", id="sid1~1")
    assert "sid1" in subsonic_state.get_state().starred("u1", "song")
    env.ok("scrobble.view", id="sid1~2")
    assert env.store._play_stats["sid1"]["count"] == 1


# ── r2-sub-10: is the token secret a generated app password? ─────────────────

def test_subsonic_password_custom_flag(users, tmp_path):
    bob = users.get_by_username("bob")
    assert users.subsonic_password_custom(bob) is False            # seeded at creation
    users.update(bob.id, subsonic_password="AppPw12345678", subsonic_password_custom=True)
    assert users.public(bob)["subsonic_password_custom"] is True
    users.set_password(bob.id, "bobNewPass9")                       # app password survives
    assert bob.subsonic_password == "AppPw12345678"
    admin = users.get_by_username("admin")
    users.set_password(admin.id, "adminNew123")                     # a seeded copy follows
    assert admin.subsonic_password == "adminNew123"
    assert users.subsonic_password_custom(admin) is False
    # Legacy record (flag unknown): learned at the next login — both ways.
    users._ss_custom.clear()
    assert users.subsonic_password_custom(bob) is None
    users.authenticate("bob", "bobNewPass9")
    users.authenticate("admin", "adminNew123")
    assert users.subsonic_password_custom(bob) is True
    assert users.subsonic_password_custom(admin) is False
    # Persisted.
    from soniqboom.core.users import UserStore
    again = UserStore(tmp_path)
    assert again.subsonic_password_custom(again.get_by_username("bob")) is True


def test_token_error_names_the_login_password(env, users, monkeypatch):
    _real_auth(monkeypatch)
    msg = env.fail("ping.view", u="bob", s="x", t="0" * 32)["message"]
    assert "SoniqBoom password" in msg and "app password if you generated one" in msg


def test_me_subsonic_password_sets_the_flag(users, monkeypatch):
    from soniqboom.api import users as users_api
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    bob = users.get_by_username("bob")
    app.dependency_overrides[users_api.require_user] = lambda: bob
    monkeypatch.setattr(users_api, "get_user_store", lambda: users)
    c = TestClient(app)
    u = c.put("/api/me/subsonic-password", json={"password": "GenPw1234567"}).json()["user"]
    assert u["subsonic_password"] is True and u["subsonic_password_custom"] is True
    u = c.put("/api/me/subsonic-password", json={"password": ""}).json()["user"]
    assert u["subsonic_password"] is False and u["subsonic_password_custom"] is False


# ── r2-sub-12: unknown /api paths are a JSON 404 ─────────────────────────────

def test_spa_fallback_refuses_api_paths():
    from fastapi import HTTPException
    from soniqboom import main
    fallback = next(r.endpoint for r in main.app.routes
                    if getattr(r, "path", "") == "/{full_path:path}")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fallback("api/does-not-exist"))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(fallback("api"))
    resp = asyncio.run(fallback("some/spa/route"))
    assert str(resp.path).endswith("index.html")


def test_main_schedules_startup_enrichment(repo_root):
    src = (repo_root / "soniqboom" / "main.py").read_text()
    assert "schedule_startup_enrichment" in src and "call_later(_STARTUP_ENRICHMENT_DELAY_S" in src
    from soniqboom import main
    assert main._STARTUP_ENRICHMENT_DELAY_S > 0


# ── r2-sub-13: highest = the caller's album ratings first ────────────────────

def test_highest_album_list_uses_album_ratings(env):
    opera = sx.album_id("Queen", "A Night at the Opera")
    innuendo = sx.album_id("Queen", "Innuendo")
    env.store.set_rating("m1", 5)                      # "Mixed" via track rating
    env.ok("setRating.view", id=innuendo, rating=4)
    env.ok("setRating.view", id=opera, rating=5)
    ids = [a["id"] for a in env.ok("getAlbumList2.view", type="highest")["albumList2"]["album"]]
    assert ids[:2] == [opera, innuendo] and len(ids) == len(set(ids))
    assert sx.album_id("mixed band", "mixed") in ids
    env.ok("setRating.view", id=opera, rating=0)        # removed
    ids = [a["id"] for a in env.ok("getAlbumList2.view", type="highest")["albumList2"]["album"]]
    assert opera not in ids and ids[0] == innuendo
    env.user = types.SimpleNamespace(id="u9", username="zed", role="admin", enabled=True)
    ids = [a["id"] for a in env.ok("getAlbumList2.view", type="highest")["albumList2"]["album"]]
    assert innuendo not in ids                         # another user's rating


# ── r2-sub-14 / r2-sub-32: artist info universes and wait budget ─────────────

def test_artist_info_decides_by_majority_and_refuses_mixed(env, monkeypatch):
    from soniqboom.core import artistinfo
    calls = []

    async def _card(name, album=None, track=None, *, is_retro=False):
        calls.append((name, is_retro))
        return {"found": True, "bio": f"bio {name} {is_retro}", "url": ""}

    monkeypatch.setattr(artistinfo, "get_artist_info", _card)
    for i in range(10):
        _add(env, tid=f"pm{i}", title=f"Hit {i}", artist="Pac", album="Album", added=800 + i)
    for i in range(1):
        _add(env, tid=f"pr{i}", title=f"Mod {i}", artist="Pac", added=820 + i, fmt="SID")
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Pac"))["artistInfo2"]
    assert calls[-1] == ("Pac", False) and "similarArtist" not in info
    env.ok("getArtistInfo2.view", id="pr0")                  # a SID song decides by itself
    assert calls[-1] == ("Pac", True)
    for i in range(1, 6):                                    # now 6 SID vs 10 MP3: mixed
        _add(env, tid=f"pr{i}", title=f"Mod {i}", artist="Pac", added=820 + i, fmt="SID")
    n = len(calls)
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Pac"))["artistInfo2"]
    assert len(calls) == n and "biography" not in info and "lastFmUrl" in info


def test_artist_info_short_wait_for_musicbrainz_only(env, monkeypatch):
    from soniqboom.core import artistinfo
    subsonic._INFO_TASKS.clear()

    async def _slow(name, album=None, track=None, *, is_retro=False):
        await asyncio.sleep(0.8)
        return {"found": True, "bio": f"late {is_retro}"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _slow)
    t0 = time.monotonic()
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Queen"))["artistInfo2"]
    assert time.monotonic() - t0 < 0.7 and "biography" not in info
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Rob Hubbard"))["artistInfo2"]
    assert info.get("biography") == "late True"              # Demozoo path: full budget


def test_artist_info_xml_child_order(env, monkeypatch):
    from soniqboom.core import artistinfo

    async def _card(name, album=None, track=None, *, is_retro=False):
        return {"found": True, "bio": "b", "image": "https://i/x.jpg",
                "url": "https://musicbrainz.org/artist/0383dadf-2a4e-4d10-a46a-e9e041da8eb3"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _card)

    async def _photo(url):
        return b"\xff\xd8" + b"J" * 400

    monkeypatch.setattr(subsonic, "_fetch_photo_bytes", _photo)
    root = ET.fromstring(env.get("getArtistInfo2.view", id=sx.artist_id("Queen"), f="xml").content)
    tags = [c.tag.split("}")[1] for c in root.find("{*}artistInfo2")]
    assert tags == ["biography", "musicBrainzId", "lastFmUrl", "smallImageUrl",
                    "mediumImageUrl", "largeImageUrl"]


# ── r2-sub-16: JSONP never rides the cookie ──────────────────────────────────

def test_jsonp_needs_explicit_credentials(env, users, monkeypatch):
    _real_auth(monkeypatch)
    admin = users.get_by_username("admin")
    tok, _ = users.issue_session(admin.id)
    env.client.cookies.set("sb_session", tok)
    r = env.client.get("/rest/getUsers.view", params={"f": "jsonp", "callback": "steal"})
    assert b"bob" not in r.content and b'"code":10' in r.content.replace(b" ", b"")
    assert env.ok("getUsers.view")["users"]["user"]              # f=json + cookie still fine
    env.client.cookies.clear()
    r = env.client.get("/rest/getUsers.view", params={"f": "jsonp", "callback": "cb",
                                                       "u": "admin", "p": "adminpass1"})
    assert r.content.startswith(b"cb(") and b"bob" in r.content


# ── r2-sub-18: legacy getLyrics is plain text ────────────────────────────────

def test_legacy_lyrics_drop_lrc_timestamps(env, monkeypatch):
    lrc = "[ar:Queen]\n[ti:Bohemian]\n[00:01.00]Is this the real life\n[00:05.50]Is this just fantasy"

    async def _fake(tid):
        return {"text": lrc if tid == "q2" else "plain words\nsecond line", "synced": tid == "q2",
                "artist": "Queen", "title": "x"}

    monkeypatch.setattr(subsonic, "_lyrics_for_track_id", _fake)
    v = env.ok("getLyrics.view", artist="queen", title="bohemian rhapsody")["lyrics"]["value"]
    assert v == "Is this the real life\nIs this just fantasy"
    v = env.ok("getLyrics.view", title="Love of My Life")["lyrics"]["value"]
    assert v == "plain words\nsecond line"
    lines = subsonic._parse_lrc(lrc)[1]
    assert [ln["_text"] for ln in lines] == ["Is this the real life", "Is this just fantasy"]


# ── r2-sub-19: a web-UI delete drops the Subsonic state too ──────────────────

def test_web_delete_user_forgets_subsonic_state(env, users, monkeypatch):
    from soniqboom.api import users as users_api
    bob = users.get_by_username("bob")
    env.user = bob
    env.ok("star.view", id="q1")
    env.ok("scrobble.view", id="q1", submission="false", c="DSub")
    assert any(k[0] == bob.id for k in subsonic._NOW_PLAYING)
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    admin = users.get_by_username("admin")
    app.dependency_overrides[users_api.require_admin] = lambda: admin
    monkeypatch.setattr(users_api, "get_user_store", lambda: users)
    assert TestClient(app).delete(f"/api/users/{bob.id}").json() == {"ok": True}
    assert not any(k[0] == bob.id for k in subsonic._NOW_PLAYING)
    assert subsonic_state.get_state().starred(bob.id, "song") == {}
    assert bob.id not in json.loads(env.state_path.read_text()).get("users", {})


# ── r2-sub-20: getAlbumInfo lastFmUrl ────────────────────────────────────────

def test_album_info_last_fm_url_only_for_tagged_albums(env):
    ai = env.ok("getAlbumInfo2.view", id="q2")["albumInfo"]            # song → its album
    assert ai["lastFmUrl"] == "https://www.last.fm/music/Queen/A%20Night%20at%20the%20Opera"
    fa = "fa:" + env.store.store_hash_lookup(GALWAY_DIR)
    assert "lastFmUrl" not in env.ok("getAlbumInfo2.view", id=fa)["albumInfo"]
    root = ET.fromstring(env.get("getAlbumInfo.view", id="q2", f="xml").content)
    el = root.find("{*}albumInfo/{*}lastFmUrl")
    assert el is not None and el.text.startswith("https://www.last.fm/music/Queen/")


# ── r2-sub-21: protocol edge cases ───────────────────────────────────────────

def test_missing_credentials_is_code_10_wrong_ones_40(env, users, monkeypatch):
    _real_auth(monkeypatch)
    assert env.fail("ping.view")["code"] == 10
    assert env.fail("ping.view", u="bob")["code"] == 10
    assert env.fail("ping.view", u="bob", s="salt")["code"] == 10
    assert env.fail("ping.view", u="bob", p="wrongpass1")["code"] == 40


def test_head_and_bare_rest_and_license(env):
    r = env.client.head("/rest/getAlbumList2.view")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/xml")
    assert env.client.head("/rest/nope.view").status_code == 404
    r = env.client.get("/rest", params={"f": "json"})
    assert r.status_code == 404 and r.json()["subsonic-response"]["status"] == "failed"
    assert env.ok("getLicense.view")["license"]["licenseExpires"].endswith("Z")


def test_oversized_page_sizes_are_clamped_not_errors(env):
    assert len(env.ok("getAlbumList2.view", type="newest", size=1000)["albumList2"]["album"]) > 0
    assert env.ok("search3.view", query="", songCount=5000, artistCount=0, albumCount=0)
    assert env.ok("getRandomSongs.view", size=900)["randomSongs"]["song"]
    assert env.ok("getSongsByGenre.view", genre="Rock", count=1000)["songsByGenre"]["song"]
    assert env.ok("getTopSongs.view", artist="Queen", count=900)["topSongs"]["song"]


# ── r2-sub-22: client paths + OpenSubsonic artist fields ─────────────────────

def test_song_path_is_relative_to_its_scan_root(env):
    env.store.upsert_scan_dir("/music/C64")
    env.store.upsert_scan_dir("ftp://10.0.0.88/Music/Demo")
    assert env.ok("getSong.view", id="h1")["song"]["path"] == "MUSICIANS/Hubbard_Rob/h1"
    t = dict(env.store.get_track("q1"),
             path="ftp://10.0.0.88/Music/Demo:/demos/pack.zip::tune.mod")
    env.store.upsert_track(t)
    assert env.ok("getSong.view", id="q1")["song"]["path"] == "demos/pack.zip/tune.mod"
    s = env.ok("getSong.view", id="q2")["song"]                     # outside every root
    assert s["path"] == "Queen/A Night at the Opera/q2"
    for sng in (env.ok("getSong.view", id=i)["song"] for i in ("q1", "q2", "c1", "x1")):
        p = sng["path"]
        assert not p.startswith("/") and "::" not in p and "://" not in p and "\\" not in p
    c1 = env.ok("getSong.view", id="c1")["song"]
    assert c1["displayArtist"] == "Madonna" and c1["displayAlbumArtist"] == "Various Artists"
    assert c1["artists"] == [{"id": sx.artist_id("Madonna"), "name": "Madonna"}]
    assert c1["albumArtists"] == [{"id": sx.artist_id("Various Artists"),
                                   "name": "Various Artists"}]
    root = ET.fromstring(env.get("getSong.view", id="c1", f="xml").content)
    song = root.find("{*}song")
    assert song.find("{*}artists").get("name") == "Madonna"
    assert song.find("{*}albumArtists").get("name") == "Various Artists"


# ── r2-sub-23: download needs the download role ──────────────────────────────

def test_download_refused_for_readonly(env):
    env.user = types.SimpleNamespace(id="ro", username="ro", role="readonly", enabled=True)
    for method in ("GET", "HEAD"):
        r = env.client.request(method, "/rest/download.view", params={"id": "q1", "f": "json"})
        if method == "GET":
            assert r.json()["subsonic-response"]["error"]["code"] == 50
        else:
            assert r.status_code == 200 and not r.headers.get("content-disposition")
    assert subsonic._user_payload(types.SimpleNamespace(
        username="ro", role="readonly", lastfm_session_key=None,
        listenbrainz_token=None))["downloadRole"] is False


# ── credentials: the machine key is derived once ─────────────────────────────

def test_derive_key_is_memoised():
    from soniqboom.core import credentials
    credentials._derive_key()
    t0 = time.perf_counter()
    for _ in range(50):
        credentials._derive_key()
    assert time.perf_counter() - t0 < 0.05


# ── r2-sub-26: signed links die with the app password / API key ──────────────

def test_links_revoked_by_app_password_change_and_key_revocation(env, users, monkeypatch):
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    key, ent = users.create_api_key(bob.id, "phone")
    key2, _ent2 = users.create_api_key(bob.id, "tablet")
    from urllib.parse import parse_qs, urlsplit

    def _cover(k):
        ai = env.ok("getAlbumInfo.view", id="q2", apiKey=k)["albumInfo"]
        q = parse_qs(urlsplit(ai["smallImageUrl"]).query)
        return q["id"][0], q["tok"][0]

    cid, tok1 = _cover(key)
    _cid, tok2 = _cover(key2)
    assert subsonic._cover_token_ok(tok1, cid) and subsonic._cover_token_ok(tok2, cid)
    users.revoke_api_key(bob.id, ent["id"])
    assert not subsonic._cover_token_ok(tok1, cid)                   # minted with the key
    assert subsonic._cover_token_ok(tok2, cid)                       # another key: fine
    users.update(bob.id, subsonic_password="BrandNewApp1")          # app password change
    assert not subsonic._cover_token_ok(tok2, cid)


# ── r2-sub-28: music folders = scan roots ────────────────────────────────────

def test_music_folders_filter_lists(env):
    env.store.upsert_scan_dir("/music/C64")
    env.store.upsert_scan_dir("/music/mods")
    for t in list(env.store._tracks.values()):
        p = t.get("path") or ""
        root = "/music/C64" if p.startswith("/music/C64") else \
            "/music/mods" if p.startswith("/music/mods") else ""
        if root:
            env.store.upsert_track(dict(t, scan_root_hash=env.store.store_hash_lookup(root)))
    env.folder_albums(True)
    folders = env.ok("getMusicFolders.view")["musicFolders"]["musicFolder"]
    assert sorted(f["name"] for f in folders) == ["C64", "mods"]
    ids = {f["name"]: f["id"] for f in folders}
    assert all(isinstance(i, int) and 0 < i < 2 ** 31 for i in ids.values())
    assert env.ok("getMusicFolders.view")["musicFolders"]["musicFolder"] == folders   # stable
    c64 = _all_artists(env.ok("getArtists.view", musicFolderId=ids["C64"]))
    mods = _all_artists(env.ok("getArtists.view", musicFolderId=ids["mods"]))
    assert set(c64) == {"Rob Hubbard", "Martin Galway"} and set(mods) == {"[Unknown Artist]"}
    idx = env.ok("getIndexes.view", musicFolderId=ids["C64"])["indexes"]
    assert set(_all_artists({"indexes": idx}, "indexes")) == set(c64)
    al_c64 = {a["id"] for a in env.ok("getAlbumList2.view", type="newest", size=500,
                                      musicFolderId=ids["C64"])["albumList2"]["album"]}
    al_mods = {a["id"] for a in env.ok("getAlbumList2.view", type="newest", size=500,
                                       musicFolderId=ids["mods"])["albumList2"]["album"]}
    assert al_c64 and al_mods and not (al_c64 & al_mods)
    songs = env.ok("getRandomSongs.view", size=50, musicFolderId=ids["mods"])["randomSongs"]["song"]
    assert [s["id"] for s in songs] == ["x1"]
    found = env.ok("search3.view", query="", songCount=50, musicFolderId=ids["C64"])
    assert {s["id"] for s in found["searchResult3"]["song"]} == {"h1", "h2", "h3", "g0", "g1", "g2"}
    everything = env.ok("getAlbumList2.view", type="newest", size=500)["albumList2"]["album"]
    legacy = env.ok("getAlbumList2.view", type="newest", size=500, musicFolderId="0")
    assert len(legacy["albumList2"]["album"]) == len(everything) > len(al_c64)
    me = types.SimpleNamespace(username="alice", role="admin", lastfm_session_key=None,
                               listenbrainz_token=None)
    assert sorted(subsonic._user_payload(me)["folder"]) == sorted(ids.values())


# ── r2-sub-31: multi-seed similar songs yield between seeds ──────────────────

def test_similar_songs_yield_between_seeds(env, monkeypatch):
    events = []
    real = env.store.similar_candidates
    monkeypatch.setattr(env.store, "similar_candidates",
                        lambda seed: events.append(("seed", seed["id"])) or real(seed))

    async def _run():
        loop = asyncio.get_running_loop()
        for _ in range(3):
            loop.call_soon(lambda: events.append(("other",)))
        await subsonic._similar_song_tracks(sx.album_id("Queen", "A Night at the Opera"), 5)

    asyncio.run(_run())
    first = events.index(("seed", events[0][1]))
    seeds = [i for i, e in enumerate(events) if e[0] == "seed"]
    assert len(seeds) == 3 and any(e == ("other",) for e in events[first + 1:seeds[1]])


# ── r2-sub-34 (Subsonic part): scrobbles land in the listening history ───────

def test_scrobble_appends_listening_history(env, monkeypatch):
    from soniqboom.api import smart
    monkeypatch.setattr(smart, "get_store", lambda: env.store)
    before = len(env.store.get_history(500))
    t1 = int(time.time()) - 3600
    env.ok("scrobble.view", id=["q1", "q2"], time=[t1 * 1000, (t1 + 200) * 1000])
    hist = env.store.get_history(500)
    assert len(hist) == before + 2
    assert {h["track_id"] for h in hist[:2]} == {"q1", "q2"}


# ── Once-per-snapshot sorts / folder views prepared off the loop ─────────────

def test_offloaded_sorts_and_folder_view_give_the_same_lists(env, monkeypatch):
    _mixed(env)
    env.folder_albums(True)
    env.store.upsert_scan_dir("/music/mods")
    for t in list(env.store._tracks.values()):
        if (t.get("path") or "").startswith("/music/mods"):
            env.store.upsert_track(dict(t, scan_root_hash=env.store.store_hash_lookup("/music/mods")))
    fid = env.ok("getMusicFolders.view")["musicFolders"]["musicFolder"][0]["id"]

    def lists():
        out = {}
        for ty in ("newest", "alphabeticalByName", "alphabeticalByArtist", "byYear"):
            out[ty] = [a["id"] for a in env.ok("getAlbumList2.view", type=ty, size=500)
                       ["albumList2"]["album"]]
            out[ty + "@"] = [a["id"] for a in env.ok("getAlbumList2.view", type=ty, size=500,
                                                     musicFolderId=fid)["albumList2"]["album"]]
        out["genre"] = [a["id"] for a in env.ok("getAlbumList2.view", type="byGenre",
                                                genre="Rock", size=500)["albumList2"]["album"]]
        out["search"] = [a["id"] for a in env.ok("search3.view", query="", albumCount=500,
                                                 songCount=0, artistCount=0)
                         ["searchResult3"]["album"]]
        return out

    inline = lists()
    subsonic._ALBUM_LIST_CACHE["cat"].memo.clear()
    monkeypatch.setattr(subsonic, "_SORT_OFFLOAD_MIN", 1)
    assert lists() == inline
    memo = subsonic._ALBUM_LIST_CACHE["cat"].memo
    assert ("sorted", "newest") in memo and any(k[0] == "folder_view" for k in memo
                                                if isinstance(k, tuple))


def test_xml_illegal_strip_matches_the_regex():
    samples = [b"plain", b"a\x01b\x1fc\td\ne\rf", "x￾y￿z".encode(),
               "café ☃ \x0b\x0c".encode(), b""]
    for raw in samples:
        assert subsonic._xml_strip_illegal(raw) == subsonic._XML_ILLEGAL.sub(b"", raw)


def test_unknown_genre_is_never_memoised(env):
    env.ok("getAlbumList2.view", type="byGenre", genre="no-such-genre-xyz")
    memo = subsonic._ALBUM_LIST_CACHE["cat"].memo
    assert not any("no-such-genre-xyz" in str(k) for k in memo)
