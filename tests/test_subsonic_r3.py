# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subsonic round-3 review fixes (GitHub #14 follow-ups).

Same harness as tests/test_subsonic_parity.py: the real ``/rest`` router over
ASGI against a hand-built ``TrackStore`` with per-user state in a tmp file.
Covers the multi-tune wire mapping (default tune, ``~0``), clean stream
errors, letter-bucket folder groups, header / list agreement, tune-aware
transcode tokens and now-playing, and the folder-name fixes.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.responses import Response

from soniqboom.api import subsonic
from soniqboom.core import subsonic_index as sx
from soniqboom.core import subsonic_state
from soniqboom.core.store import TrackStore

from test_subsonic_parity import _tr, env, radio_favs, users  # noqa: F401 — fixtures


def _add(env, **kw):
    kw.setdefault("store", env.store)
    env.store.upsert_track(_tr(**kw))


def _tune_file(env, tid="t4", subsongs=4, lengths=(100.0, 61.0, 42.0, 30.0), start=None,
               d="/music/C64/MUSICIANS/Tel_Jeroen", added=900):
    _add(env, tid=tid, title="Tune", artist="Jeroen Tel", dir_path=d, added=added, fmt="SID")
    t = dict(env.store.get_track(tid), subsongs=subsongs)
    if lengths is not None:
        t["hvsc_lengths"] = list(lengths)
    if start is not None:
        t["start_subsong"] = start
    env.store.upsert_track(t)
    return "fa:" + env.store.store_hash_lookup(d)


def _capture_streams(monkeypatch):
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
    return seen


# ── r3-sub-1: tune ids — 0-based wire, the bare id is the default tune ───────

def test_tune_ids_cover_every_tune_start_one(env, monkeypatch):
    fa = _tune_file(env)
    rows = env.ok("getAlbum.view", id=fa)["album"]["song"]
    assert [r["id"] for r in rows] == ["t4", "t4~1", "t4~2", "t4~3"]
    assert [r["title"] for r in rows] == ["Tune", "Tune (Tune 2/4)", "Tune (Tune 3/4)",
                                          "Tune (Tune 4/4)"]
    assert [r["duration"] for r in rows] == [100, 61, 42, 30]
    seen = _capture_streams(monkeypatch)
    for i in ("t4", "t4~0", "t4~3"):
        env.client.get("/rest/stream.view", params={"id": i})
    # The last tune is reachable; ``~0`` is tune 1 = the bare id's tune.
    assert seen == [("t4", 0), ("t4", 0), ("t4", 3)]
    assert subsonic._split_id(env.store, "t4~0") == ("t4", 0)
    assert subsonic._split_id(env.store, "t4~4") == ("t4~4", 0)       # out of range
    assert env.fail("getSong.view", id="t4~4")["code"] == 70
    # ``~0`` is the bare id's own tune: it lists as the bare id.
    assert env.ok("getSong.view", id="t4~0")["song"]["id"] == "t4"


def test_tune_ids_with_a_recorded_start_song(env, monkeypatch):
    """The web player's wire mapping (utils.js subsongWireToTune): wire 0 is
    the start song, wire s is tune 1, every other wire w is tune w+1."""
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: env.store)
    # PSID start song 3 of 4 → start_subsong 2.
    fa = _tune_file(env, start=2)
    rows = env.ok("getAlbum.view", id=fa)["album"]["song"]
    # Bare id = the default tune (3); then tunes 1, 2, 4 in tune order.
    assert [r["id"] for r in rows] == ["t4", "t4~2", "t4~1", "t4~3"]
    assert [r["title"] for r in rows] == ["Tune", "Tune (Tune 1/4)", "Tune (Tune 2/4)",
                                          "Tune (Tune 4/4)"]
    assert [r["duration"] for r in rows] == [42, 100, 61, 30]       # lengths by tune
    assert env.ok("getSong.view", id="t4")["song"]["duration"] == 42
    assert env.ok("getSong.view", id="t4~2")["song"]["title"] == "Tune (Tune 1/4)"
    seen = _capture_streams(monkeypatch)
    for i in ("t4", "t4~0", "t4~2", "t4~3"):
        env.client.get("/rest/stream.view", params={"id": i})
    env.client.get("/rest/download.view", params={"id": "t4~2"})    # a tune → stream path
    # Every id reaches the renderer as its wire, as the web player sends it.
    assert seen == [("t4", 0), ("t4", 0), ("t4", 2), ("t4", 3), ("t4", 2)]
    # ``~0`` is the bare id's own tune: normalised to the bare id everywhere.
    assert env.ok("getSong.view", id="t4~0")["song"]["id"] == "t4"
    pl = env.ok("createPlaylist.view", name="P", songId=["t4~0", "t4~2"])["playlist"]
    assert [e["id"] for e in pl["entry"]] == ["t4", "t4~2"]
    assert env.store._playlists[pl["id"]]["track_ids"] == ["t4", {"id": "t4", "subsong": 2}]
    env.ok("savePlayQueue.view", id=["t4~0", "t4~1"], current="t4~0", position=0)
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert [e["id"] for e in pq["entry"]] == ["t4", "t4~1"] and pq["current"] == "t4"
    # The album header counts the same four entries, in lists too.
    alb = env.ok("getAlbum.view", id=fa)["album"]
    assert alb["songCount"] == 4 and alb["duration"] == 42 + 100 + 61 + 30
    env.folder_albums(True)
    listed = {a["id"]: a for a in env.ok("getAlbumList2.view", type="newest",
                                         size=50)["albumList2"]["album"]}
    assert listed[fa]["songCount"] == 4 and listed[fa]["duration"] == 233


@pytest.mark.parametrize("n,s", [(n, s) for n in range(1, 8) for s in range(0, n)])
def test_wire_mapping_matches_the_web_players(n, s):
    """Mirror of tests/js/subsong_map.test.mjs: every tune has exactly one
    wire, wire 0 is the default tune, and the listing (bare + listed wires)
    covers each tune once, in tune order after the default."""
    t = {"id": "x", "subsongs": n, "start_subsong": s}

    def js(w):                                   # utils.js subsongWireToTune (1-based)
        start = s + 1
        if w == 0:
            return start
        if start != 1 and w == start - 1:
            return 1
        return w + 1
    assert [sx.wire_tune(t, w) + 1 for w in range(n)] == [js(w) for w in range(n)]
    wires = [0] + sx.listed_tunes(t)
    tunes = [sx.wire_tune(t, w) for w in wires]
    assert sorted(tunes) == list(range(n))
    assert tunes[1:] == sorted(tunes[1:])


def test_bookmark_on_the_default_tune_is_the_bare_ids(env):
    _tune_file(env, start=2)
    env.ok("createBookmark.view", id="t4~0", position=1000)
    marks = subsonic_state.get_state().bookmarks("u1")
    assert set(marks) == {"t4"}
    env.ok("deleteBookmark.view", id="t4~0")
    assert subsonic_state.get_state().bookmarks("u1") == {}


# ── r3-sub-7: list views and getAlbum agree ──────────────────────────────────

def test_tune_children_without_lengths_report_zero_and_headers_agree(env):
    env.folder_albums(True)
    fa = _tune_file(env, subsongs=3, lengths=None)
    rows = env.ok("getAlbum.view", id=fa)["album"]["song"]
    assert [r["duration"] for r in rows] == [100, 0, 0]              # never the file's repeated
    alb = env.ok("getAlbum.view", id=fa)["album"]
    lst = {a["id"]: a for a in env.ok("getAlbumList2.view", type="alphabeticalByName",
                                      size=100)["albumList2"]["album"]}[fa]
    art = {a["id"]: a for a in env.ok("getArtist.view", id=alb["artistId"])["artist"]["album"]}[fa]
    for key in ("songCount", "duration", "coverArt"):
        assert alb[key] == lst[key] == art[key], key
    assert alb["songCount"] == 3 == len(rows)
    d = env.ok("getMusicDirectory.view", id=fa)["directory"]
    assert len(d["child"]) == alb["songCount"]


def test_sample_ties_pick_the_same_cover_everywhere(env):
    env.folder_albums(True)
    d = "/music/demo/same_time"
    for tid in ("s1", "s3", "s2"):
        _add(env, tid=tid, title=tid, artist="Tie Band", dir_path=d, added=500)
    fa = "fa:" + env.store.store_hash_lookup(d)
    alb = env.ok("getAlbum.view", id=fa)["album"]
    lst = {a["id"]: a for a in env.ok("getAlbumList2.view", type="newest",
                                      size=100)["albumList2"]["album"]}[fa]
    assert alb["coverArt"] == lst["coverArt"] == "s3"                   # (added, id) max
    for tid in ("w1", "w3", "w2"):
        _add(env, tid=tid, title=tid, artist="Tie Band", album="Tied", added=600)
    al = sx.album_id("tie band", "tied")
    alb = env.ok("getAlbum.view", id=al)["album"]
    lst = {a["id"]: a for a in env.ok("getAlbumList2.view", type="newest",
                                      size=100)["albumList2"]["album"]}[al]
    assert alb["coverArt"] == lst["coverArt"] == "w3"


# ── r3-sub-6: tune ids in the transcoding extension and now playing ──────────

def test_transcode_decision_and_stream_carry_the_tune(env, monkeypatch):
    _tune_file(env, tid="sid1", subsongs=3, lengths=(100.0, 61.0, 42.0))
    seen = _capture_streams(monkeypatch)
    d = env.ok("getTranscodeDecision.view", mediaId="sid1~2")["transcodeDecision"]
    assert d["canTranscode"] is True and d["track_id"] == "sid1~2"
    tok = d["transcodeParams"]
    assert subsonic._verify_token(tok)["sub"] == 2
    r = env.client.get("/rest/getTranscodeStream.view",
                       params={"mediaId": "sid1~2", "mediaType": "song", "transcodeParams": tok})
    assert r.status_code == 200 and seen == [("sid1", 2)]
    bad = env.client.get("/rest/getTranscodeStream.view",
                         params={"mediaId": "sid1", "mediaType": "song", "transcodeParams": tok})
    assert bad.status_code == 400 and seen == [("sid1", 2)]
    # Legacy form reports it as an envelope, code 70.
    leg = env.client.get("/rest/getTranscodeStream.view",
                         params={"token": tok, "mediaId": "sid1~1", "f": "json"})
    assert leg.status_code == 400 or leg.json()["subsonic-response"]["error"]["code"] == 70


def test_now_playing_reports_the_streamed_tune(env, monkeypatch):
    _tune_file(env, tid="sid1", subsongs=3, lengths=(100.0, 61.0, 42.0))
    _capture_streams(monkeypatch)
    subsonic._NOW_PLAYING.clear()
    env.client.get("/rest/stream.view", params={"id": "sid1", "c": "x"})
    env.client.get("/rest/stream.view", params={"id": "sid1~2", "c": "x"})   # within 60 s
    np = env.ok("getNowPlaying.view")["nowPlaying"]["entry"]
    assert [(e["id"], e["title"]) for e in np] == [("sid1~2", "Tune (Tune 3/3)")]
    subsonic._NOW_PLAYING.clear()


# ── r3-sub-2: stream / download errors are clean Subsonic envelopes ──────────

@pytest.mark.parametrize("status,detail,code", [
    (503, "Source offline: ftp://10.0.0.88/Music/Demo isn't connected", 0),
    (410, "File not found on disk: /music/C64/secret/q1", 70),
    (404, "File not found on disk: /music/C64/secret/q1", 70),
    (502, "Render failed for /Volumes/Music/x.sid", 0),
])
def test_stream_errors_hide_server_paths(env, monkeypatch, status, detail, code):
    from soniqboom.api import stream as stream_mod

    async def _boom(**kw):
        raise HTTPException(status, detail)

    monkeypatch.setattr(stream_mod, "stream_track", _boom)
    for fmt in ("json", "xml"):
        r = env.client.get("/rest/stream.view", params={"id": "q1", "f": fmt})
        assert r.status_code == 200, r.text
        assert "10.0.0.88" not in r.text and "/music/" not in r.text and "Volumes" not in r.text
    body = env.client.get("/rest/stream.view", params={"id": "q1", "f": "json"}).json()
    assert body["subsonic-response"]["error"]["code"] == code


def test_stream_416_stays_http(env, monkeypatch):
    from soniqboom.api import stream as stream_mod

    async def _range(**kw):
        raise HTTPException(416, "Requested range not satisfiable")

    monkeypatch.setattr(stream_mod, "stream_track", _range)
    assert env.client.get("/rest/stream.view", params={"id": "q1"}).status_code == 416


def test_download_errors_hide_server_paths(env, monkeypatch):
    from soniqboom.api import stream as stream_mod
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: env.store)

    async def _offline(tid, model, *a, **k):
        raise HTTPException(503, "Source offline: ftp://10.0.0.88/Music/Demo isn't connected")

    monkeypatch.setattr(stream_mod, "_resolve_play_source", _offline)
    r = env.client.get("/rest/download.view", params={"id": "q1", "f": "json"})
    body = r.json()["subsonic-response"]
    assert r.status_code == 200 and body["error"]["code"] == 0
    assert "10.0.0.88" not in r.text


def test_transcode_stream_spec_form_errors_are_clean_text(env, monkeypatch):
    from soniqboom.api import stream as stream_mod

    async def _boom(**kw):
        raise HTTPException(503, "Source offline: ftp://10.0.0.88/Music/Demo")

    _tune_file(env, tid="sid1", subsongs=1, lengths=None)
    tok = env.ok("getTranscodeDecision.view", mediaId="sid1")["transcodeDecision"]["transcodeParams"]
    monkeypatch.setattr(stream_mod, "stream_track", _boom)
    r = env.client.get("/rest/getTranscodeStream.view",
                       params={"mediaId": "sid1", "mediaType": "song", "transcodeParams": tok})
    assert r.status_code == 503 and r.headers["content-type"].startswith("text/plain")
    assert r.text == "The music source for this song is offline."


def test_head_on_an_offline_source_is_the_error_envelope(env, monkeypatch):
    from soniqboom.api import tracks as tracks_mod
    seen = _capture_streams(monkeypatch)
    monkeypatch.setattr(tracks_mod, "_source_unreachable", lambda p: True)
    r = env.client.head("/rest/stream.view", params={"id": "q1", "f": "json"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert seen == []                                          # never reached the stream probe
    monkeypatch.setattr(tracks_mod, "_source_unreachable", lambda p: False)
    r = env.client.head("/rest/stream.view", params={"id": "q1"})
    assert r.headers["content-type"].startswith("audio/") and seen == [("HEAD", "q1", 0)]


# ── r3-sub-3: letter-bucket folders group per owner under their ancestor ─────

ROOT = "/music/mods"
COLL = ROOT + "/modarchive_2007"


def _buckets(env, base=COLL, letters=("A", "B", "C")):
    """Three mixed letter buckets: Alpha / Beta / Gamma a third each, plus
    one owner-less tune per bucket."""
    env.store._aof = lambda *a, **k: None
    env.store.upsert_scan_dir(ROOT)
    n = 0
    for L in letters:
        d = f"{base}/{L}"
        for owner in ("Alpha", "Beta", "Gamma"):
            _add(env, tid=f"{L}-{owner}", title=f"{owner} {L}", artist=owner, dir_path=d,
                 added=1000 + n, fmt="MOD", genre=[f"G{owner}"])
            n += 1
        _add(env, tid=f"{L}-none", title=f"none {L}", dir_path=d, added=1000 + n, fmt="MOD")
        n += 1


def test_letter_buckets_group_per_owner(env):
    _buckets(env)
    env.folder_albums(True)
    anc = sx._path_hash(COLL)
    albums = env.ok("getAlbumList2.view", type="alphabeticalByName", size=500)["albumList2"]["album"]
    ours = [a for a in albums if a["id"].startswith(f"fa:{anc}~g")]
    assert sorted(a["artist"] for a in ours) == ["Alpha", "Beta", "Gamma", "[Unknown Artist]"]
    # Named like the ancestor folder itself ("modarchive 2007" is a generic
    # name, so it is qualified by its parent inside the root) — no letter.
    name = "mods / modarchive 2007"
    assert all(a["name"] == name and a["songCount"] == 3 for a in ours)
    assert not any(a["name"].startswith(name + " / ") for a in albums)
    gid = sx.group_album_id(anc, "alpha")
    got = env.ok("getAlbum.view", id=gid)["album"]
    assert [s["id"] for s in got["song"]] == ["A-Alpha", "B-Alpha", "C-Alpha"]
    assert all(s["albumId"] == gid and s["album"] == name for s in got["song"])
    assert env.ok("getSong.view", id="B-Alpha")["song"]["albumId"] == gid
    # Genre membership and the owner's own page.
    by_genre = env.ok("getAlbumList2.view", type="byGenre", genre="GAlpha")["albumList2"]["album"]
    assert [a["id"] for a in by_genre] == [gid]
    alpha = next(a for idx in env.ok("getArtists.view")["artists"]["index"]
                 for a in idx["artist"] if a["name"] == "Alpha")
    art = env.ok("getArtist.view", id=alpha["id"])["artist"]
    assert [a["id"] for a in art["album"]] == [gid] and alpha["albumCount"] == 1
    # [Unknown Artist] lists the owner-less group once.
    unk = env.ok("getMusicDirectory.view", id=sx.artist_id(""))["directory"]["child"]
    assert [c["id"] for c in unk if c["id"].startswith(f"fa:{anc}")] == [sx.group_album_id(anc, "")]
    # Cover art and stars accept the id.
    assert env.client.get("/rest/getCoverArt.view", params={"id": gid}).status_code == 200
    env.ok("star.view", albumId=gid)
    assert gid in subsonic_state.get_state().starred("u1", "album")


def test_buckets_right_under_the_root_take_the_roots_name(env):
    _buckets(env, base=ROOT)
    env.folder_albums(True)
    anc = sx._path_hash(ROOT)
    got = env.ok("getAlbum.view", id=sx.group_album_id(anc, "beta"))["album"]
    assert got["name"] == "mods" and got["songCount"] == 3


def test_legacy_bucket_ids_still_resolve(env):
    _buckets(env)
    env.folder_albums(True)
    bdh = env.store.store_hash_lookup(COLL + "/B")
    share = env.ok("getAlbum.view", id=sx.folder_album_id(bdh, "gamma"))["album"]
    assert [s["id"] for s in share["song"]] == ["B-Gamma"]
    whole = env.ok("getAlbum.view", id="fa:" + bdh)["album"]
    assert len(whole["song"]) == 4


def test_group_id_never_collides_with_the_ancestors_own_album(env):
    _buckets(env)
    for owner in ("Alpha", "Beta", "Gamma"):          # loose tracks in the ancestor itself
        _add(env, tid=f"own-{owner}", title=f"own {owner}", artist=owner, dir_path=COLL,
             added=2000, fmt="MOD")
    env.folder_albums(True)
    anc = env.store.store_hash_lookup(COLL)
    assert anc == sx._path_hash(COLL)
    direct = env.ok("getAlbum.view", id=sx.folder_album_id(anc, "alpha"))["album"]
    group = env.ok("getAlbum.view", id=sx.group_album_id(anc, "alpha"))["album"]
    assert [s["id"] for s in direct["song"]] == ["own-Alpha"]
    assert [s["id"] for s in group["song"]] == ["A-Alpha", "B-Alpha", "C-Alpha"]


def test_group_ids_resolve_with_folder_albums_off(env):
    _buckets(env)
    env.folder_albums(True)
    anc = sx._path_hash(COLL)
    on = env.ok("getAlbum.view", id=sx.group_album_id(anc, "gamma"))["album"]
    env.folder_albums(False)
    off = env.ok("getAlbum.view", id=sx.group_album_id(anc, "gamma"))["album"]
    assert [s["id"] for s in off["song"]] == [s["id"] for s in on["song"]]
    assert off["name"] == on["name"] and off["songCount"] == on["songCount"]
    none = env.ok("getAlbum.view", id=sx.group_album_id(anc, ""))["album"]
    assert [s["id"] for s in none["song"]] == ["A-none", "B-none", "C-none"]
    assert env.fail("getAlbum.view", id=sx.group_album_id("0" * 16, "gamma"))["code"] == 70


def test_single_owner_bucket_stays_its_own_album(env):
    _buckets(env)
    d = COLL + "/Q"
    _add(env, tid="Q1", title="q1", artist="Alpha", dir_path=d, added=3000, fmt="MOD")
    _add(env, tid="Q2", title="q2", artist="Alpha", dir_path=d, added=3001, fmt="MOD")
    _add(env, tid="Q3", title="q3", artist="Beta", dir_path=d, added=3002, fmt="MOD")
    env.folder_albums(True)
    qdh = env.store.store_hash_lookup(d)
    q = env.ok("getAlbum.view", id="fa:" + qdh)["album"]
    assert q["name"] == "modarchive 2007 / Q" and q["songCount"] == 3
    grp = env.ok("getAlbum.view", id=sx.group_album_id(sx._path_hash(COLL), "alpha"))["album"]
    assert "Q1" not in {s_["id"] for s_ in grp["song"]}


@pytest.mark.parametrize("folder_on", [False, True])
def test_grouped_build_chunked_equals_one_shot(env, folder_on):
    _buckets(env)
    one = sx.build_catalogue(env.store, folder_on=folder_on)
    chunked = asyncio.run(sx.build_catalogue_async(env.store, folder_on=folder_on, chunk=2))

    def shape(cat):
        return sorted((e.id, e.name, e.artist, e.song_count, e.duration, e.sample["id"],
                       tuple(sorted(e.genres_l)), tuple(e.dir_hashes)) for e in cat.entries)
    assert shape(one) == shape(chunked)
    assert one.dir_group == chunked.dir_group


# ── r3-sub-20 / r3-sub-22: folder naming ─────────────────────────────────────

@pytest.mark.parametrize("folder,owner", [
    ("Huelsbeck Chris", "Chris Hülsbeck"),
    ("Bjoernerud Sebastian", "Sebastian Bjørnerud"),
    ("Haard Lars", "Lars Hård"),
    ("Mueller Markus", "Markus Müller"),
    ("Hulsbeck Chris", "Chris Hülsbeck"),
])
def test_transliterated_composer_folders_take_the_composers_spelling(folder, owner):
    assert sx.owner_named(folder, owner.lower(), owner) == owner


def test_transliteration_does_not_match_other_people():
    assert sx.owner_named("Mueller Markus", "markus maier", "Markus Maier") == "Mueller Markus"


def test_person_index_folders_keep_their_own_name():
    st = TrackStore()
    st._aof = lambda *a, **k: None
    st.upsert_scan_dir("/m/C64Music")

    def named(p):
        dh = st.store_hash_lookup(p)
        sx._FOLDER_NAMES.pop(dh, None)
        return sx.folder_album_name(st, dh)
    assert named("/m/C64Music/MUSICIANS/D/Data") == "Data"
    assert named("/m/C64Music/MUSICIANS/0-9/505") == "505"
    assert named("/m/C64Music/MUSICIANS/T/Tracker") == "Tracker"
    assert named("/m/C64Music/GAMES/A-F") == "GAMES / A-F"
    assert named("/m/C64Music/MUSICIANS/L/Link/unreleased") == "Link / unreleased"


# ── r3-sub-5 / r3-sub-14: search3 folds accents, pages without full lists ────

def _old_search(env, q, *, artistOffset=0, artistCount=20, albumOffset=0, albumCount=20,
                root=None):
    """The pre-r3 search3 artist / album matching (plain lower-case
    substring over the full name-sorted lists, then a slice)."""
    cat = subsonic._catalogue(env.store)
    union = cat.union
    arts = sorted(union.display.items(), key=lambda kv: (kv[1].lower(), kv[0]))
    fview = subsonic._folder_view(env.store, cat, root) if root else None
    m = [kv for kv in arts if not q or q in kv[0]]
    if fview is not None:
        m = [kv for kv in m if kv[0] in fview[1]]
    by_name = subsonic._in_folder(cat, ("sorted", "alphabeticalByName"),
                                  subsonic._sorted_entries(cat, "alphabeticalByName"),
                                  root, env.store)
    al = [e for e in by_name if not q or q in e.name.lower()]
    return ([union.artist_id(low) for low, _n in m[artistOffset:artistOffset + artistCount]],
            [e.id for e in al[albumOffset:albumOffset + albumCount]])


@pytest.mark.parametrize("q,expect", [
    ("oorni", "Lasse Öörni"), ("beyonce", "Beyoncé"), ("motörhead", "Motorhead"),
    ("odegaard", "Ødegaard"), ("öörni", "Lasse Öörni"),
])
def test_search3_artist_matching_ignores_accents(env, q, expect):
    for i, name in enumerate(("Lasse Öörni", "Beyoncé", "Motorhead", "Ødegaard")):
        _add(env, tid=f"acc{i}", title=f"Song {i}", artist=name, album=f"Album {name}",
             added=4000 + i)
    res = env.ok("search3.view", query=q, songCount=0)["searchResult3"]
    assert expect in [a["name"] for a in res["artist"]]
    assert f"Album {expect}" in [a["name"] for a in res["album"]]


@pytest.mark.parametrize("q", ["", "q", "qu", "que", "o", "rob", "zzz"])
@pytest.mark.parametrize("offs", [(0, 0), (1, 1), (2, 0)])
@pytest.mark.parametrize("with_root", [False, True])
def test_search3_pages_match_the_full_list_behaviour(env, q, offs, with_root):
    env.store._aof = lambda *a, **k: None
    env.store.upsert_scan_dir("/music/C64")
    env.folder_albums(True)
    root = subsonic._folder_hash(env.store, None)
    mf = None
    if with_root:
        mf = next(f["id"] for f in env.ok("getMusicFolders.view")["musicFolders"]["musicFolder"])
        root = subsonic._folder_hash(env.store, mf)
    ao, bo = offs
    params = dict(query=q, songCount=0, artistOffset=ao, artistCount=2,
                  albumOffset=bo, albumCount=2)
    if mf is not None:
        params["musicFolderId"] = mf
    res = env.ok("search3.view", **params)["searchResult3"]
    arts, albs = _old_search(env, q, artistOffset=ao, artistCount=2, albumOffset=bo,
                             albumCount=2, root=root)
    assert [a["id"] for a in res["artist"]] == arts
    assert [a["id"] for a in res["album"]] == albs


# ── r3-sub-17: field operators in search3 ────────────────────────────────────

def test_search3_understands_field_operators(env, monkeypatch):
    for i, (t, al, y) in enumerate((("Uridium", "Uridium", 1986), ("Uridium 2", "Uridium 2", 1993),
                                    ("Paradroid", "Paradroid", 1985))):
        _add(env, tid=f"gm{i}", title=t, artist="Andrew Braybrook", album=al, added=5000 + i,
             fmt="SID")
        tr = dict(env.store.get_track(f"gm{i}"), year=y)
        env.store.upsert_track(tr)
    res = env.ok("search3.view", query="game:uridium")["searchResult3"]
    assert sorted(s["id"] for s in res["song"]) == ["gm0", "gm1"]
    # An operator-only query matches no artists / albums (never all of them).
    assert res["artist"] == [] and res["album"] == []
    res = env.ok("search3.view", query='artist:"andrew braybrook" year:>1986')["searchResult3"]
    assert [s["id"] for s in res["song"]] == ["gm1"]
    # Free text next to an operator still matches artist / album names.
    res = env.ok("search3.view", query="game:uridium braybrook")["searchResult3"]
    assert [a["name"] for a in res["artist"]] == ["Andrew Braybrook"]
    # Plain text keeps the old path.
    assert "gm2" in [s["id"] for s in env.ok("search3.view", query="paradroid")["searchResult3"]["song"]]


# ── r3-sub-12: no single long GIL hold for big sorts / the index JSON ────────

def test_sort_runs_is_a_stable_sort():
    import random
    rnd = random.Random(7)
    items = [(rnd.randrange(50), i) for i in range(10_007)]
    key = lambda x: x[0]                                         # noqa: E731 — many ties
    assert subsonic._sort_runs(items, key, chunk=997) == sorted(items, key=key)
    assert subsonic._sort_runs(items[:10], key) == sorted(items[:10], key=key)


@pytest.mark.parametrize("fmt", ["json", "jsonp", "xml"])
def test_artist_index_bytes_unchanged_by_the_spliced_encoder(env, fmt):
    for i in range(30):
        _add(env, tid=f"ix{i}", title=f"T{i}", artist=f"Árté {i} \"q\"", added=6000 + i)
    payload = {"artists": {"ignoredArticles": subsonic._IGNORED_ARTICLES,
                           "index": subsonic._artist_index(subsonic._catalogue(env.store),
                                                          subsonic._catalogue(env.store).union)}}
    assert subsonic._json_spliced(payload, "artists", "index") == \
        subsonic._ok(payload, fmt="json").body
    r = env.client.get("/rest/getArtists.view", params={"f": fmt, "callback": "cb"})
    body = r.content
    if fmt == "xml":
        assert body.startswith(b"<?xml") or body.startswith(b"<subsonic-response")
    else:
        inner = body[3:-2] if fmt == "jsonp" else body
        import json as _json
        assert _json.loads(inner)["subsonic-response"]["artists"]["index"] == \
            _json.loads(subsonic._ok(payload, fmt="json").body)["subsonic-response"]["artists"]["index"]


# ── r3-sub-13: getSongsByGenre builds big genres in steps ────────────────────

def _genre_ids_old(store, genre, root=None):
    ids = store.filter_track_ids(genre=genre)
    tracks = store._tracks
    ids.sort(key=lambda tid: ((tracks.get(tid) or {}).get("added_at") or 0, tid), reverse=True)
    if root:
        in_root = subsonic._root_track_ids(store, root)
        ids = [i for i in ids if i in in_root]
    return ids


@pytest.mark.parametrize("with_root", [False, True])
def test_genre_song_order_is_unchanged(env, monkeypatch, with_root):
    env.store._aof = lambda *a, **k: None
    env.store.upsert_scan_dir("/music/C64")
    rh = env.store.store_hash_lookup("/music/C64")
    for i in range(60):                                   # a "big" genre (> 1/16 of the library)
        _add(env, tid=f"gg{i:03d}", title=f"G{i}", artist="Big", genre=["Chiptune"],
             added=7000 + (i % 7), dir_path="/music/C64/big" if i % 2 else "/music/other")
        if i % 2:
            env.store.upsert_track(dict(env.store.get_track(f"gg{i:03d}"), scan_root_hash=rh))
    for tid in ("h1", "h2"):
        env.store.upsert_track(dict(env.store.get_track(tid), scan_root_hash=rh))
    monkeypatch.setattr(subsonic, "_GENRE_STEP", 7)       # many steps
    root = mf = None
    if with_root:
        mf = next(f for f in env.ok("getMusicFolders.view")["musicFolders"]["musicFolder"]
                  if f["name"] == "C64")["id"]
        root = subsonic._folder_hash(env.store, mf)
    for genre in ("Chiptune", "Rock"):                    # big and small
        want = _genre_ids_old(env.store, genre, root)
        got, off = [], 0
        while True:
            params = {"genre": genre, "count": 13, "offset": off}
            if mf is not None:
                params["musicFolderId"] = mf
            page = env.ok("getSongsByGenre.view", **params)["songsByGenre"]["song"]
            if not page:
                break
            got += [s_["id"] for s_ in page]
            off += 13
        assert got == want and (want or genre == "Rock")
        if with_root and genre == "Chiptune":
            assert set(want) <= {f"gg{i:03d}" for i in range(1, 60, 2)} | {"h1", "h2", "h3"}


def test_genre_build_racing_a_new_snapshot_writes_nothing_stale(env, monkeypatch):
    for i in range(40):
        _add(env, tid=f"rc{i:03d}", title=f"R{i}", artist="Race", genre=["Chiptune"], added=8000 + i)
    monkeypatch.setattr(subsonic, "_GENRE_STEP", 5)
    c = subsonic._GENRE_SONGS_CACHE
    c.update(store=None)

    async def _run():
        task = asyncio.ensure_future(subsonic._genre_song_ids(env.store, "Chiptune"))
        await asyncio.sleep(0)                  # the build is between steps now
        old = c["lists"]
        c.update(store=env.store, seq=-1, built_at=0.0, lists={}, inflight={})   # a reset
        ids = await task
        return ids, old
    ids, old = asyncio.run(_run())
    assert ids and "chiptune" not in c["lists"]            # the new generation stays empty
    c.update(store=None)


# ── r3-sub-15: big listings are gzipped off the event loop ───────────────────

def test_big_listings_are_gzipped_by_the_route(env, monkeypatch):
    import gzip as _gzip
    monkeypatch.setattr(subsonic, "_GZIP_OFFLOAD_MIN_BYTES", 2000)
    calls = []
    real = subsonic.asyncio.to_thread

    async def _spy(fn, *a, **k):
        calls.append(getattr(fn, "__name__", ""))
        return await real(fn, *a, **k)
    monkeypatch.setattr(subsonic.asyncio, "to_thread", _spy)
    for i in range(30):
        _add(env, tid=f"gz{i}", title=f"Gz {i}", artist="Gzip", added=9000 + i)
    params = {"size": 40, "f": "json"}
    plain = env.client.get("/rest/getRandomSongs.view", params=params,
                           headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    raw = env.client.get("/rest/getRandomSongs.view", params=params,
                         headers={"Accept-Encoding": "gzip"}, )
    assert raw.headers.get("content-encoding") == "gzip" and "compress" in calls
    assert raw.headers.get("vary") == "Accept-Encoding"
    assert raw.json()["subsonic-response"]["status"] == "ok"        # httpx decodes it
    # JSONP: the wrapped body is what gets compressed.
    jp = env.client.get("/rest/getRandomSongs.view", params={"size": 40, "f": "jsonp",
                                                             "callback": "cb"},
                        headers={"Accept-Encoding": "gzip"})
    assert jp.headers.get("content-encoding") == "gzip" and jp.content.startswith(b"cb(")
    # Small bodies are left to the middleware.
    calls.clear()
    small = env.client.get("/rest/ping.view", params={"f": "json"},
                           headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers and "compress" not in calls
    assert _gzip.decompress(_gzip.compress(b"x")) == b"x"


# ── r3-sub-11: estimateContentLength, unknown list types ─────────────────────

def test_estimate_content_length_reaches_the_stream_module(env, monkeypatch):
    """``estimateContentLength=true`` sets the stream module's estimate (the
    song's own length; a tune's HVSC length) around the stream call; without
    it none is set.  The byte-exact behaviour is the stream module's
    (tests/test_render_r3_misc.py)."""
    from soniqboom.api import stream as stream_mod
    seen = []

    async def _fake(**kw):
        seen.append((kw["track_id"], kw["subsong"], stream_mod._estimate_len_ctx.get()))
        return Response(b"x", media_type="audio/mpeg")
    monkeypatch.setattr(stream_mod, "stream_track", _fake)
    _tune_file(env, tid="sid1", subsongs=3, lengths=(100.0, 61.0, 42.0))
    base = {"format": "mp3", "maxBitRate": "96"}
    env.client.get("/rest/stream.view", params={**base, "id": "q1"})
    env.client.get("/rest/stream.view", params={**base, "id": "q1",
                                                "estimateContentLength": "true"})
    env.client.get("/rest/stream.view", params={**base, "id": "sid1~2",
                                                "estimateContentLength": "true"})
    env.client.get("/rest/stream.view", params={**base, "id": "q1",
                                                "estimateContentLength": "false"})
    assert seen == [("q1", 0, None), ("q1", 0, 100.0), ("sid1", 2, 42.0), ("q1", 0, None)]
    assert stream_mod._estimate_len_ctx.get() is None          # reset after the call


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg missing")
def test_estimated_length_on_the_wire_for_a_cold_mp3_transcode(env, monkeypatch, tmp_path):
    """End to end through /rest/stream: the stream module's live-transcode
    response for a cold MP3 transcode of a real (noise) WAV — exact
    Content-Length == body with the flag, chunked without it."""
    import random
    from starlette.responses import StreamingResponse  # noqa: F401
    from soniqboom.api import stream as stream_mod
    src = tmp_path / "n.wav"
    rng = random.Random(1)
    frames = 6 * 44100
    src.write_bytes(stream_mod._build_wav_header(44100, 2, frames, bits_per_sample=16)
                    + rng.randbytes(frames * 4))

    async def _real_live(**kw):
        xf = stream_mod._Xform("mp3", kw["max_bitrate_kbps"], float(kw["seek"] or 0))
        return stream_mod._live_transcode_response(src, xf, {"X-Transcoded": "1"}, None)
    monkeypatch.setattr(stream_mod, "stream_track", _real_live)
    base = {"id": "q1", "format": "mp3", "maxBitRate": "128"}
    r = env.client.get("/rest/stream.view", params={**base, "estimateContentLength": "true"})
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(r.content) > 90_000
    r = env.client.get("/rest/stream.view", params=base)
    assert "content-length" not in r.headers and len(r.content) > 90_000


@pytest.mark.parametrize("method", ["getAlbumList.view", "getAlbumList2.view"])
def test_unknown_album_list_type_is_an_error(env, method):
    err = env.fail(method, type="bogus")
    assert err["code"] == 0 and "bogus" in err["message"]
    key = "albumList2" if "2" in method else "albumList"
    assert env.ok(method)[key]["album"]                    # missing type: newest


# ── r3-sub-9: placeholder art TTL follows whether art may still come ─────────

def test_placeholder_ttl(env, monkeypatch):
    from soniqboom.api import tracks as tracks_mod
    monkeypatch.setattr(tracks_mod, "_source_unreachable", lambda p: p.startswith("/music/off"))
    _add(env, tid="off1", title="Off", artist="Off", dir_path="/music/offline")
    r = env.client.get("/rest/getCoverArt.view", params={"id": "off1"})
    assert r.headers["cache-control"].endswith("max-age=300")
    r = env.client.get("/rest/getCoverArt.view", params={"id": "q1x" if False else "q3"})
    assert r.headers["cache-control"].endswith("max-age=86400")     # tagless, reachable
    _add(env, tid="rem1", title="Remote", artist="R", dir_path="ftp://nas/Music/x")
    env.store.upsert_track(dict(env.store.get_track("rem1"), cover_art=True))
    r = env.client.get("/rest/getCoverArt.view", params={"id": "rem1"})
    assert r.headers["cache-control"].endswith("max-age=300")       # backfill pending
    env.store.mark_art_absent("rem1")
    r = env.client.get("/rest/getCoverArt.view", params={"id": "rem1"})
    assert r.headers["cache-control"].endswith("max-age=86400")


# ── r3-sub-10: updating a station keeps its place ────────────────────────────

def test_update_station_keeps_its_position_with_one_write(env, radio_favs, monkeypatch):
    from soniqboom.api import stations as stations_api

    async def _public(url):
        return None
    monkeypatch.setattr(stations_api, "_assert_public_url", _public)
    for n in ("One", "Two", "Three"):
        env.ok("createInternetRadioStation.view", streamUrl=f"http://{n.lower()}.example/s", name=n)
    custom = [x["sid"] for x in radio_favs.get_favorites() if x["sid"].startswith("custom:")]
    writes = []
    real = radio_favs._write_json
    monkeypatch.setattr(radio_favs, "_write_json", lambda *a: (writes.append(a[0]), real(*a)))
    env.ok("updateInternetRadioStation.view", id=custom[0], name="Uno")
    rows = env.ok("getInternetRadioStations.view")["internetRadioStations"]["internetRadioStation"]
    mine = [r["name"] for r in rows if r["id"].startswith("custom:")]
    assert mine == ["Uno", "Two", "Three"] and writes == [radio_favs._FAVS_FILE]
    assert radio_favs.update_favorite("custom:nope", {"sid": "custom:nope"}) is None


# ── r3-sub-8: artist photos are served by this server ────────────────────────

def test_artist_photo_is_stored_and_served_by_get_cover_art(env, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from soniqboom.core import artistinfo
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (960, 960), (200, 10, 10)).save(buf, format="JPEG")
    jpeg = buf.getvalue()
    fetched = []

    async def _photo(url):
        fetched.append(url)
        return jpeg if "960px" in url else None

    async def _card(name, album=None, track=None, *, is_retro=False):
        return {"found": True, "bio": "b", "url": "",
                "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Q.jpg/330px-Q.jpg"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _card)
    aid = sx.artist_id("Queen")
    # Before any info call: no photo → a sample track's cover, no network.
    monkeypatch.setattr(subsonic, "_fetch_photo_bytes", _photo)
    r = env.client.get("/rest/getCoverArt.view", params={"id": aid})
    assert r.status_code == 200 and fetched == []
    info = env.ok("getArtistInfo2.view", id=aid)["artistInfo2"]
    assert fetched == ["https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Q.jpg/960px-Q.jpg"]
    urls = [info[k] for k in ("smallImageUrl", "mediumImageUrl", "largeImageUrl")]
    assert all("wikimedia" not in u and "getCoverArt" in u for u in urls)
    sizes = [parse_qs(urlsplit(u).query)["size"][0] for u in urls]
    assert sizes == ["200", "550", "1200"]          # exactly the cached renditions
    # The signed link works without credentials, and each bucket is its own size.
    monkeypatch.setattr(subsonic, "_require_user",
                        lambda *a, **k: (_ for _ in ()).throw(subsonic._SubsonicError(10, "no")))
    monkeypatch.setattr(subsonic, "_link_owner_ok", lambda claims: env.user)
    dims = []
    for u in urls:
        q = parse_qs(urlsplit(u).query)
        rr = env.client.get("/rest/getCoverArt.view", params={"id": q["id"][0], "size": q["size"][0],
                                                              "tok": q["tok"][0]})
        assert rr.status_code == 200 and rr.headers["content-type"] == "image/jpeg"
        dims.append(Image.open(io.BytesIO(rr.content)).size[0])
        etag = rr.headers["etag"]
        again = env.client.get("/rest/getCoverArt.view", headers={"If-None-Match": etag},
                               params={"id": q["id"][0], "size": q["size"][0], "tok": q["tok"][0]})
        assert again.status_code == 304
    assert dims == [200, 550, 960] and len(fetched) == 1          # never fetched again
    assert (env.photo_dir / "queen.img").exists()


def test_artist_photo_fetch_failure_omits_the_fields(env, monkeypatch):
    from soniqboom.core import artistinfo

    async def _card(name, album=None, track=None, *, is_retro=False):
        return {"found": True, "bio": "b", "url": "", "image": "https://x.example/p.jpg"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _card)
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Queen"))["artistInfo2"]
    assert "largeImageUrl" not in info and info["biography"] == "b"
    assert (env.photo_dir / "queen.noimg").exists()


# ── r3-sub-18: the next track's render is prewarmed for Subsonic clients ─────

def _prewarm_env(env, monkeypatch):
    from soniqboom.api import stream as stream_mod
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: env.store)
    _capture_streams(monkeypatch)
    started = []

    async def _do(track_id, track, subsong, priority=None):
        started.append((track_id, subsong, priority))

    monkeypatch.setattr(stream_mod, "_do_prewarm", _do)
    stream_mod._prewarm_tasks.clear()
    stream_mod._prewarm_owner.clear()
    return started


def test_next_queue_entry_is_prewarmed(env, monkeypatch):
    started = _prewarm_env(env, monkeypatch)
    _tune_file(env, tid="sidA", subsongs=1, lengths=None)
    _tune_file(env, tid="sidB", subsongs=3, lengths=None, d="/music/C64/MUSICIANS/Other")
    _add(env, tid="nat.mp3", title="Native", artist="Queen", album="Hits")
    env.ok("savePlayQueue.view", id=["sidA", "sidB~1", "nat.mp3"], current="sidA")
    env.client.get("/rest/stream.view", params={"id": "sidA"})
    assert started == [("sidB", 1, 1)]
    # A native next track, a Range continuation and a HEAD schedule nothing.
    env.client.get("/rest/stream.view", params={"id": "sidB~1"})
    env.client.get("/rest/stream.view", params={"id": "sidA"}, headers={"Range": "bytes=1000-"})
    env.client.head("/rest/stream.view", params={"id": "sidA"})
    assert started == [("sidB", 1, 1)]


def test_album_order_prewarm_and_the_setting(env, monkeypatch):
    started = _prewarm_env(env, monkeypatch)
    fa = _tune_file(env, tid="sidA", subsongs=3, lengths=None)
    # A real ``.sid`` name: the setting holds back RENDERED formats only.
    t = env.store.get_track("sidA")
    env.store.upsert_track(dict(t, path=t["path"] + ".sid"))
    env.ok("getAlbum.view", id=fa)                                  # a catalogue snapshot exists
    env.client.get("/rest/stream.view", params={"id": "sidA"})     # no queue → album order
    assert started == [("sidA", 1, 1)]
    env.client.get("/rest/stream.view", params={"id": "sidA~2"})   # last entry: nothing
    assert len(started) == 1
    env.store.set_config("render_prewarm", False)
    env.client.get("/rest/stream.view", params={"id": "sidA"})
    assert len(started) == 1
    assert fa


# ── r3-sub-19: the web queue and the Subsonic queue are one record ───────────

def test_web_play_queue_endpoint_shares_the_subsonic_queue(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from soniqboom.api import users as users_api
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    app.dependency_overrides[users_api.require_user] = lambda: env.user
    web = TestClient(app)
    assert web.get("/api/me/play-queue").json() == {}
    r = web.put("/api/me/play-queue", json={"ids": ["q1", "", "q2", "h1"], "current_index": 2,
                                            "position": 1500, "client": "SoniqBoom Mobile"})
    assert r.status_code == 200
    # The PUT answers the new ``changed`` stamp — no follow-up GET needed.
    assert r.json()["changed"] > 0
    assert r.json()["changed"] == web.get("/api/me/play-queue").json()["changed"]
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert [e["id"] for e in pq["entry"]] == ["q1", "q2", "h1"]
    assert pq["current"] == "q2" and pq["position"] == 1500 and pq["changedBy"] == "SoniqBoom Mobile"
    env.ok("savePlayQueue.view", id=["h2", "h3"], current="h3", position=42, c="DSub")
    got = web.get("/api/me/play-queue").json()
    assert got["ids"] == ["h2", "h3"] and got["current"] == "h3" and got["current_index"] == 1
    assert got["position"] == 42 and got["changed_by"] == "DSub"
    assert web.put("/api/me/play-queue", json={"ids": []}).json() == {"ok": True, "changed": None}
    assert web.get("/api/me/play-queue").json() == {}


# ── r3-sub-16: the catalogue is prewarmed after startup ──────────────────────

def test_startup_prewarm_builds_the_catalogue_once(env, monkeypatch):
    from soniqboom import main
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(store_mod, "get_store", lambda: env.store)
    builds = []
    real = sx.build_catalogue_async

    async def _count(*a, **k):
        builds.append(1)
        await asyncio.sleep(0.05)
        return await real(*a, **k)
    monkeypatch.setattr(sx, "build_catalogue_async", _count)
    monkeypatch.setitem(subsonic._ALBUM_LIST_CACHE, "cat", None)
    monkeypatch.setitem(subsonic._ALBUM_LIST_CACHE, "task", None)

    async def _run():
        main._prewarm_subsonic_catalogue()
        task = subsonic._ALBUM_LIST_CACHE["task"]
        assert task is not None and not task.done()
        main._prewarm_subsonic_catalogue()                    # joins, never a second build
        await task
        # ... then the artist index is built in the background as well.
        for _ in range(100):
            if not subsonic._STARTUP_BG:
                break
            await asyncio.sleep(0.01)
        return subsonic._ALBUM_LIST_CACHE["cat"]
    cat = asyncio.run(_run())
    assert cat is not None and builds == [1]
    hit = cat.memo.get("artist_index")
    assert hit is not None and hit[0] is cat.union and hit[1]
    main._prewarm_subsonic_catalogue()                        # built already: nothing
    assert builds == [1]


# ── r3-sub-21: one source for the folder-albums default ──────────────────────

def test_folder_albums_flag_uses_the_shared_constants(monkeypatch):
    from soniqboom.core import folder_album as fa
    assert subsonic._folder_albums_on(object()) is fa.SUBSONIC_FOLDER_ALBUMS_DEFAULT
    seen = []
    st = types_ns(get_config=lambda k, d=None: (seen.append((k, d)), d)[1])
    assert subsonic._folder_albums_on(st) is bool(fa.SUBSONIC_FOLDER_ALBUMS_DEFAULT)
    assert seen == [(fa.SUBSONIC_FOLDER_ALBUMS_KEY, fa.SUBSONIC_FOLDER_ALBUMS_DEFAULT)]


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)
