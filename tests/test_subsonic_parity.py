# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subsonic client parity (GitHub #14).

Drives the real ``/rest`` router over ASGI against a hand-built ``TrackStore``
(auth stubbed, per-user state pointed at a tmp file) and pins the behaviours
real clients (DSub, Symfonium, Substreamer, Amperfy) depend on:

  * unknown methods get a Subsonic failed envelope at HTTP 404 — not the SPA;
  * getArtists / getIndexes list album artists ∪ track artists, and every
    ``albumCount`` equals what ``getArtist`` actually returns;
  * genre album counts and ``byGenre`` are by track membership;
  * folder albums (``subsonic_folder_albums``) on/off, ids round-tripping
    through getAlbum / getCoverArt / getMusicDirectory / lists / search;
  * getMusicDirectory for every id kind, getSongsByGenre paging, stars +
    play queue persisted across a state reload, ratings, top songs, scan
    status, radio stations, cover-art placeholder, formPost.
"""
from __future__ import annotations

import json
import time
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import subsonic
from soniqboom.core import subsonic_state
from soniqboom.core import subsonic_index as sx
from soniqboom.core.store import TrackStore

# Captured at import, before any fixture patches them.
_REAL_REQUIRE_USER = subsonic._require_user
_REAL_REQUIRE_NO_COOKIE = subsonic._require_user_no_cookie

HUBBARD_DIR = "/music/C64/MUSICIANS/Hubbard_Rob"
GALWAY_DIR = "/music/C64/MUSICIANS/Galway_Martin"
MODS_DIR = "/music/mods/unsorted"


def _tr(tid: str, *, title: str, artist: str = "", album_artist: str = "",
        album: str = "", genre: list[str] | None = None, added: int = 0,
        dir_path: str = "/music/misc", store: TrackStore | None = None,
        track_number: int | None = None, fmt: str = "MP3") -> dict:
    dh = store.store_hash_lookup(dir_path) if store is not None else ""
    return {
        "id": tid, "title": title, "artist": artist, "album_artist": album_artist,
        "album": album, "genre": list(genre or []), "year": 1990,
        "added_at": 1_700_000_000 + added, "duration": 100.0,
        "format": fmt, "path": f"{dir_path}/{tid}", "dir_hash": dh,
        "track_number": track_number,
    }


def _build_store() -> TrackStore:
    st = TrackStore()
    add = lambda **kw: st.upsert_track(_tr(store=st, **kw))   # noqa: E731
    # Album-artist tagged album; ONE track carries the extra "Opera" genre.
    add(tid="q1", title="Death on Two Legs", artist="Queen", album_artist="Queen",
        album="A Night at the Opera", genre=["Rock"], added=1, track_number=1)
    add(tid="q2", title="Bohemian Rhapsody", artist="Queen", album_artist="Queen",
        album="A Night at the Opera", genre=["Rock", "Opera"], added=2, track_number=2)
    add(tid="q3", title="Love of My Life", artist="Queen", album_artist="Queen",
        album="A Night at the Opera", genre=["Rock"], added=3, track_number=3)
    # Same artist, NO album-artist tag (the mixed-tag library case).
    add(tid="q4", title="Innuendo", artist="Queen", album="Innuendo",
        genre=["Rock"], added=4)
    # Compilation: Madonna only ever appears on someone else's album.
    add(tid="c1", title="Vogue", artist="Madonna", album_artist="Various Artists",
        album="Hits 1990", genre=["Pop"], added=5, track_number=1)
    add(tid="c2", title="Innuendo (edit)", artist="Queen", album_artist="Various Artists",
        album="Hits 1990", genre=["Pop"], added=6, track_number=2)
    # Membership vs sample: the NEWEST track (the sample) is Pop, the older
    # one is Jazz — byGenre Jazz must still list the album.
    add(tid="m1", title="Old", artist="Mixed Band", album="Mixed", genre=["Jazz"], added=7)
    add(tid="m2", title="New", artist="Mixed Band", album="Mixed", genre=["Pop"], added=8)
    # Retro: album-less SID files, one directory per composer.  One Galway
    # tune sits in Hubbard's directory (Hubbard dominates that folder).
    add(tid="h1", title="Commando", artist="Rob Hubbard", genre=["Chiptune"],
        added=9, dir_path=HUBBARD_DIR, fmt="SID")
    add(tid="h2", title="Monty on the Run", artist="Rob Hubbard", genre=["Chiptune"],
        added=10, dir_path=HUBBARD_DIR, fmt="SID")
    add(tid="h3", title="Delta", artist="Rob Hubbard", genre=["Chiptune"],
        added=11, dir_path=HUBBARD_DIR, fmt="SID")
    add(tid="g0", title="Guest Tune", artist="Martin Galway", added=12,
        dir_path=HUBBARD_DIR, fmt="SID")
    add(tid="g1", title="Wizball", artist="Martin Galway", added=13,
        dir_path=GALWAY_DIR, fmt="SID")
    add(tid="g2", title="Arkanoid", artist="Martin Galway", added=14,
        dir_path=GALWAY_DIR, fmt="SID")
    # Untagged module — no artist, no album.
    add(tid="x1", title="untitled.mod", added=15, dir_path=MODS_DIR, fmt="MOD")
    return st


class _Env:
    def __init__(self, store, client, state_path):
        self.store = store
        self.client = client
        self.state_path = state_path
        self.user = types.SimpleNamespace(id="u1", username="alice", role="admin",
                                          enabled=True)

    def get(self, method: str, **params):
        params.setdefault("f", "json")
        r = self.client.get(f"/rest/{method}", params=params)
        return r

    def ok(self, method: str, **params) -> dict:
        r = self.get(method, **params)
        assert r.status_code == 200, r.text
        body = r.json()["subsonic-response"]
        assert body["status"] == "ok", body
        return body

    def fail(self, method: str, **params) -> dict:
        r = self.get(method, **params)
        body = r.json()["subsonic-response"]
        assert body["status"] == "failed", body
        return body["error"]

    def folder_albums(self, on: bool) -> None:
        self.store.set_config("subsonic_folder_albums", on)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    store = _build_store()
    # Most tests pin the folder-albums-OFF shape explicitly (the setting now
    # defaults ON — covered by the test_default_* tests, which clear it).
    store.set_config("subsonic_folder_albums", False)
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 0.0)
    state_path = tmp_path / "subsonic_state.json"
    subsonic_state.reset_state(state_path)
    app = FastAPI()
    app.include_router(subsonic.router)
    e = _Env(store, TestClient(app), state_path)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: e.user)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: e.user)
    # Cover art: never touch the real art pipeline / data dir.
    import soniqboom.api.art as art_mod
    from soniqboom.core import art_cache
    e.art_requests = []

    async def _no_thumb(tid, bucket):
        return None

    async def _full(tid):
        e.art_requests.append(tid)
        return (b"\xff\xd8JPEG" + tid.encode(), "image/jpeg") if tid == "q2" else (None, None)

    async def _thumbs(tid, data):
        return {"sm": data, "lg": data}

    monkeypatch.setattr(art_cache, "get_art", _no_thumb)
    monkeypatch.setattr(art_mod, "_resolve_full_art", _full)
    monkeypatch.setattr(art_mod, "_generate_and_cache_thumbs", _thumbs)
    # Artist photos: a tmp cache dir and no network (a test that wants a
    # photo replaces ``_fetch_photo_bytes``).
    from soniqboom.core import artistinfo
    photo_dir = tmp_path / "artistinfo"
    photo_dir.mkdir()
    monkeypatch.setattr(artistinfo, "_cache_dir", lambda: photo_dir)
    e.photo_dir = photo_dir

    async def _no_photo(url):
        return None

    monkeypatch.setattr(subsonic, "_fetch_photo_bytes", _no_photo)
    monkeypatch.setitem(subsonic._PHOTO_INDEX, "slugs", None)
    subsonic._PHOTO_TASKS.clear()
    yield e
    subsonic_state.reset_state()


def _all_artists(body: dict, key: str = "artists") -> dict[str, dict]:
    return {a["name"]: a for idx in body[key]["index"] for a in idx["artist"]}


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


# ── A. Unknown-method catch-all ──────────────────────────────────────────────

@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_unknown_method_is_a_subsonic_404_envelope(env, fmt):
    r = env.client.get("/rest/getBogusThing.view", params={"f": fmt})
    assert r.status_code == 404
    if fmt == "json":
        body = r.json()["subsonic-response"]
        assert body["status"] == "failed"
        assert body["error"]["code"] == 0
        assert "getBogusThing" in body["error"]["message"]
    else:
        assert r.headers["content-type"].startswith("text/xml")
        root = ET.fromstring(r.content)
        assert root.get("status") == "failed"
        err = root.find("{*}error")
        assert err.get("code") == "0" and "getBogusThing" in err.get("message")


def test_unknown_method_post_form_honours_format(env):
    r = env.client.post("/rest/nope", data={"f": "json", "u": "x"})
    assert r.status_code == 404
    assert r.json()["subsonic-response"]["error"]["code"] == 0


def test_known_route_is_not_shadowed_by_catch_all(env):
    # Falsifying companion: the catch-all is LAST, so real routes still answer.
    assert env.ok("ping.view")["status"] == "ok"
    assert subsonic.router.routes[-1].path == "/rest/{rest:path}"


def test_main_app_routes_unknown_rest_before_spa_fallback():
    from soniqboom.main import app
    r = TestClient(app).get("/rest/definitelyNotAMethod.view", params={"f": "json"})
    assert r.status_code == 404
    assert "text/html" not in r.headers.get("content-type", "")
    assert r.json()["subsonic-response"]["status"] == "failed"


# ── B. Artist union + consistent album counts ───────────────────────────────

def test_artists_union_lists_track_artists_too(env):
    arts = _all_artists(env.ok("getArtists.view"))
    # Only Queen + Various Artists carry an album-artist tag; the rest are
    # track-artist-only and were previously missing.
    for name in ("Queen", "Various Artists", "Madonna", "Mixed Band",
                 "Rob Hubbard", "Martin Galway"):
        assert name in arts, name
    assert arts["Queen"]["albumCount"] == 2           # tagged + untagged album
    assert arts["Madonna"]["albumCount"] == 1         # appears on the compilation
    assert arts["Rob Hubbard"]["albumCount"] == 0     # album-less, folder albums off
    assert "coverArt" in arts["Queen"] and arts["Queen"]["coverArt"]


@pytest.mark.parametrize("folder_on", [False, True])
def test_every_album_count_matches_get_artist(env, folder_on):
    env.folder_albums(folder_on)
    arts = _all_artists(env.ok("getArtists.view"))
    for name, row in arts.items():
        got = env.ok("getArtist.view", id=row["id"])["artist"]
        assert got["albumCount"] == row["albumCount"] == len(got.get("album", [])), name


def test_artist_names_deduplicate_case_insensitively(env):
    env.store.upsert_track(_tr("q9", title="Extra", artist="QUEEN", album="Live",
                               store=env.store, added=30))
    arts = _all_artists(env.ok("getArtists.view"))
    assert "QUEEN" not in arts and arts["Queen"]["albumCount"] == 3


def test_get_indexes_last_modified_is_stable_and_if_modified_since(env):
    a = env.ok("getIndexes.view")["indexes"]
    b = env.ok("getIndexes.view")["indexes"]
    assert a["lastModified"] == b["lastModified"]         # not the request time
    assert a["index"]
    unchanged = env.ok("getIndexes.view", ifModifiedSince=a["lastModified"])["indexes"]
    assert "index" not in unchanged
    older = env.ok("getIndexes.view", ifModifiedSince=a["lastModified"] - 1)["indexes"]
    assert older["index"]
    env.ok("getIndexes.view", musicFolderId="0")          # accepted
    time.sleep(0.01)
    env.store.upsert_track(_tr("n1", title="New", artist="Newcomer", album="Debut",
                               store=env.store, added=40))
    c = env.ok("getIndexes.view", ifModifiedSince=a["lastModified"])["indexes"]
    assert c["lastModified"] > a["lastModified"]
    assert "Newcomer" in _all_artists({"indexes": c}, "indexes")


# ── C. Genres by membership ──────────────────────────────────────────────────

def test_genre_album_counts_by_membership(env):
    genres = {g["value"]: g for g in env.ok("getGenres.view")["genres"]["genre"]}
    assert genres["Opera"]["albumCount"] == 1       # one track of one album
    assert genres["Rock"]["albumCount"] == 2        # Opera album + Innuendo
    assert genres["Jazz"]["albumCount"] == 1        # the OLDER track's genre
    assert genres["Pop"]["albumCount"] == 2         # compilation + Mixed
    assert genres["Chiptune"]["albumCount"] == 0    # album-less, folder albums off


def test_by_genre_lists_albums_whose_sample_lacks_the_genre(env):
    albums = env.ok("getAlbumList2.view", type="byGenre", genre="jazz",
                    size=50)["albumList2"]["album"]
    assert [a["name"] for a in albums] == ["Mixed"]
    opera = env.ok("getAlbumList2.view", type="byGenre", genre="Opera")["albumList2"]["album"]
    assert [a["name"] for a in opera] == ["A Night at the Opera"]
    assert env.fail("getAlbumList2.view", type="byGenre")["code"] == 10


def test_genre_xml_carries_name_as_element_text(env):
    r = env.get("getGenres.view", f="xml")
    root = ET.fromstring(r.content)
    names = {g.text for g in root.iter("{http://subsonic.org/restapi}genre")}
    assert {"Rock", "Opera", "Jazz"} <= names


# ── D. Folder albums on / off ────────────────────────────────────────────────

def test_folder_albums_off_songs_have_no_dangling_album_id(env):
    d = env.ok("getMusicDirectory.view", id=sx.artist_id("Rob Hubbard"))["directory"]
    kids = d["child"]
    assert {c["title"] for c in kids} == {"Commando", "Monty on the Run", "Delta"}
    for c in kids:
        assert c["isDir"] is False
        assert c["parent"] == d["id"]
        assert "albumId" not in c
    galway = env.ok("getMusicDirectory.view", id=sx.artist_id("Martin Galway"))["directory"]
    assert {c["title"] for c in galway["child"]} == {"Guest Tune", "Wizball", "Arkanoid"}
    lst = env.ok("getAlbumList2.view", type="alphabeticalByName", size=500)["albumList2"]["album"]
    assert not any(a["id"].startswith("fa:") for a in lst)


def test_folder_albums_on_round_trip(env):
    env.folder_albums(True)
    hub_dh = env.store.store_hash_lookup(HUBBARD_DIR)
    fa = "fa:" + hub_dh
    art = env.ok("getArtist.view", id=sx.artist_id("Rob Hubbard"))["artist"]
    assert [a["id"] for a in art["album"]] == [fa]
    # An HVSC "Surname_Firstname" folder takes its composer's own spelling.
    assert art["album"][0]["name"] == "Rob Hubbard"
    assert art["album"][0]["songCount"] == 4           # 3 Hubbard + Galway's guest tune
    album = env.ok("getAlbum.view", id=fa)["album"]
    assert album["name"] == "Rob Hubbard" and album["artist"] == "Rob Hubbard"
    assert len(album["song"]) == 4
    for sng in album["song"]:
        assert sng["albumId"] == fa and sng["parent"] == fa and sng["album"] == "Rob Hubbard"
    # getMusicDirectory: same membership as getAlbum.
    d = env.ok("getMusicDirectory.view", id=fa)["directory"]
    assert {c["id"] for c in d["child"]} == {s["id"] for s in album["song"]}
    assert d["parent"] == sx.artist_id("Rob Hubbard")
    # Hubbard's directory now shows the folder album instead of loose songs.
    hd = env.ok("getMusicDirectory.view", id=sx.artist_id("Rob Hubbard"))["directory"]
    assert [c["id"] for c in hd["child"]] == [fa] and hd["child"][0]["isDir"] is True
    # Galway dominates his own folder; his tune in Hubbard's folder stays a
    # loose song under him so it remains reachable.
    gd = env.ok("getMusicDirectory.view", id=sx.artist_id("Martin Galway"))["directory"]
    dirs = [c for c in gd["child"] if c["isDir"]]
    songs = [c for c in gd["child"] if not c["isDir"]]
    assert [c["name"] for c in dirs] == ["Martin Galway"]
    assert [c["title"] for c in songs] == ["Guest Tune"]
    # Lists + search include folder albums; cover art resolves via a member.
    lst = env.ok("getAlbumList2.view", type="alphabeticalByName", size=500)["albumList2"]["album"]
    assert fa in {a["id"] for a in lst}
    found = env.ok("search3.view", query="rob hubbard", artistCount=0, songCount=0)
    assert [a["id"] for a in found["searchResult3"]["album"]] == [fa]
    r = env.client.get("/rest/getCoverArt.view", params={"id": fa})
    assert r.status_code == 200
    assert env.art_requests[-1] == "g0"                # newest member track
    # Chiptune genre now has the folder album.
    genres = {g["value"]: g for g in env.ok("getGenres.view")["genres"]["genre"]}
    assert genres["Chiptune"]["albumCount"] == 1
    # The untagged module's folder album files under the reserved
    # [Unknown Artist], whose id resolves.
    mods = [a for a in lst if a["name"] == "unsorted"]
    assert len(mods) == 1 and mods[0]["artist"] == "[Unknown Artist]"
    unk = env.ok("getArtist.view", id=mods[0]["artistId"])["artist"]
    assert [a["id"] for a in unk["album"]] == [mods[0]["id"]]


def test_folder_albums_toggle_takes_effect_without_restart(env):
    fa = "fa:" + env.store.store_hash_lookup(GALWAY_DIR)
    ids = lambda: {a["id"] for a in env.ok("getAlbumList2.view", type="newest",  # noqa: E731
                                          size=500)["albumList2"]["album"]}
    assert fa not in ids()
    env.folder_albums(True)
    assert fa in ids()
    env.folder_albums(False)
    assert fa not in ids()
    # A cached fa: id still resolves while the setting is off.
    assert env.ok("getAlbum.view", id=fa)["album"]["songCount"] == 2


# ── E. getMusicDirectory for every id kind; getSongsByGenre ──────────────────

def test_music_directory_artist_album_and_errors(env):
    q = sx.artist_id("Queen")
    d = env.ok("getMusicDirectory.view", id=q)["directory"]
    assert d["name"] == "Queen"
    assert all(c["isDir"] for c in d["child"])
    names = {c["title"] for c in d["child"]}
    assert names == {"A Night at the Opera", "Innuendo"}
    opera = next(c for c in d["child"] if c["title"] == "A Night at the Opera")
    assert opera["parent"] == q
    ad = env.ok("getMusicDirectory.view", id=opera["id"])["directory"]
    assert [c["title"] for c in ad["child"]] == ["Death on Two Legs", "Bohemian Rhapsody",
                                                "Love of My Life"]
    assert ad["parent"] == q
    assert all(c["parent"] == opera["id"] for c in ad["child"])
    assert env.fail("getMusicDirectory.view", id="ar:0000000000000000")["code"] == 70
    assert env.fail("getMusicDirectory.view", id="q1")["code"] == 70


def test_get_songs_by_genre_paging(env):
    for i in range(25):
        env.store.upsert_track(_tr(f"s{i:02d}", title=f"Tune {i}", artist="Paging Test",
                                   genre=["Demo"], added=100 + i, store=env.store))
    pages = [env.ok("getSongsByGenre.view", genre="demo", count=10, offset=o)
             ["songsByGenre"]["song"] for o in (0, 10, 20)]
    assert [len(p) for p in pages] == [10, 10, 5]
    ids = [s["id"] for p in pages for s in p]
    assert len(set(ids)) == 25                          # disjoint, complete
    assert ids == [f"s{i:02d}" for i in range(24, -1, -1)]   # newest first
    assert env.ok("getSongsByGenre.view", genre="Nope")["songsByGenre"]["song"] == []


# ── F. Per-user state ────────────────────────────────────────────────────────

def test_star_unstar_get_starred2_persist_across_reload(env):
    album_id = sx.album_id("Queen", "A Night at the Opera")
    artist_id = sx.artist_id("Madonna")
    env.ok("star.view", id=["q1", "c1", "does-not-exist"], albumId=album_id,
           artistId=artist_id)
    s2 = env.ok("getStarred2.view")["starred2"]
    assert {s["id"] for s in s2["song"]} == {"q1", "c1"}
    assert [a["id"] for a in s2["album"]] == [album_id]
    assert [a["id"] for a in s2["artist"]] == [artist_id]
    assert "starred" in env.ok("getSong.view", id="q1")["song"]
    assert "starred" not in env.ok("getSong.view", id="q2")["song"]
    # Persisted: a fresh state object reading the same file sees the stars.
    subsonic_state.reset_state(env.state_path)
    s2 = env.ok("getStarred2.view")["starred2"]
    assert {s["id"] for s in s2["song"]} == {"q1", "c1"}
    # v1 shape carries directory-style albums.
    s1 = env.ok("getStarred.view")["starred"]
    assert s1["album"][0]["isDir"] is True and s1["album"][0]["title"] == "A Night at the Opera"
    # A deleted track is skipped, not an error — and so is its artist once
    # the artist has no tracks left (c1 was Madonna's only one).
    env.store.delete_track("c1")
    s2 = env.ok("getStarred2.view")["starred2"]
    assert {s["id"] for s in s2["song"]} == {"q1"}
    assert s2["artist"] == []
    env.ok("unstar.view", id="q1", albumId=album_id)
    s2 = env.ok("getStarred2.view")["starred2"]
    assert s2["song"] == [] and s2["album"] == []
    starred_list = env.ok("getAlbumList2.view", type="starred")["albumList2"]["album"]
    assert starred_list == []
    assert env.fail("star.view")["code"] == 10


def test_star_is_per_user(env):
    env.ok("star.view", id="q1")
    env.user = types.SimpleNamespace(id="u2", username="bob", role="edit", enabled=True)
    assert env.ok("getStarred2.view")["starred2"]["song"] == []


def test_save_and_get_play_queue(env):
    r = env.client.post("/rest/savePlayQueue.view",
                        data={"id": ["q1", "q2", "h1"], "current": "q2",
                              "position": "4321", "c": "Symfonium", "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert [e["id"] for e in pq["entry"]] == ["q1", "q2", "h1"]
    assert pq["current"] == "q2" and pq["position"] == 4321
    assert pq["changedBy"] == "Symfonium" and pq["username"] == "alice"
    subsonic_state.reset_state(env.state_path)          # persisted
    assert env.ok("getPlayQueue.view")["playQueue"]["current"] == "q2"
    env.ok("savePlayQueue.view")                         # no ids → clears
    empty = env.ok("getPlayQueue.view")["playQueue"]     # required element, no entries
    assert empty == {"username": "alice", "changed": "1970-01-01T00:00:00Z", "changedBy": ""}


def test_set_rating(env):
    env.ok("setRating.view", id="q3", rating=4)
    assert env.store.get_rating("q3") == 4
    assert env.ok("getSong.view", id="q3")["song"]["userRating"] == 4
    env.ok("setRating.view", id="q3", rating=0)
    assert env.store.get_rating("q3") == 0
    assert "userRating" not in env.ok("getSong.view", id="q3")["song"]
    assert env.fail("setRating.view", id="q3", rating=7)["code"] == 10
    # Album / artist ids: the caller's own rating (see test_album_artist_ratings).
    assert env.fail("setRating.view", id="al:0123456789abcdef", rating=3)["code"] == 70
    env.user = types.SimpleNamespace(id="u3", username="ro", role="readonly", enabled=True)
    assert env.fail("setRating.view", id="q3", rating=3)["code"] == 50


# ── G. Info / top songs / scan / radio ──────────────────────────────────────

def test_get_top_songs_ordering(env):
    for _ in range(3):
        env.store.record_play("q2")
    env.store.record_play("q1")
    env.store.set_rating("q4", 5)
    top = env.ok("getTopSongs.view", artist="queen", count=10)["topSongs"]["song"]
    ids = [s["id"] for s in top]
    # plays desc, then rating desc, then title.
    assert ids[:3] == ["q2", "q1", "q4"]
    assert set(ids) == {"q1", "q2", "q3", "q4", "c2"}
    assert [s["id"] for s in env.ok("getTopSongs.view", artist="queen", count=2)
            ["topSongs"]["song"]] == ["q2", "q1"]
    assert env.ok("getTopSongs.view", artist="nobody")["topSongs"]["song"] == []


def test_artist_info_never_blocks_on_a_slow_lookup(env, monkeypatch):
    from soniqboom.core import artistinfo
    calls = []

    async def _slow(name, album=None, track=None, *, is_retro=False):
        calls.append((name, is_retro))
        import asyncio
        await asyncio.sleep(30)
        return {"found": True, "bio": "late"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _slow)
    monkeypatch.setattr(subsonic, "_INFO_TIMEOUT_SEC", 0.05)
    t0 = time.monotonic()
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Queen"))["artistInfo2"]
    assert time.monotonic() - t0 < 2.0
    assert "biography" not in info
    assert calls == [("Queen", False)]


def test_artist_info_maps_cached_card(env, monkeypatch):
    from soniqboom.core import artistinfo

    async def _card(name, album=None, track=None, *, is_retro=False):
        return {"found": True, "bio": f"About {name}", "image": "https://img/x.jpg",
                "url": "https://musicbrainz.org/artist/0383dadf-2a4e-4d10-a46a-e9e041da8eb3"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _card)
    fetched = []

    async def _photo(url):
        fetched.append(url)
        return b"\xff\xd8" + b"J" * 400

    monkeypatch.setattr(subsonic, "_fetch_photo_bytes", _photo)
    info = env.ok("getArtistInfo.view", id="h1")["artistInfo"]     # song id → its artist
    assert info["biography"] == "About Rob Hubbard"
    assert info["musicBrainzId"] == "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"
    # The photo is served by this server (signed getCoverArt link), never
    # the third-party URL.
    assert info["largeImageUrl"].startswith("http") and "getCoverArt" in info["largeImageUrl"]
    assert "img/x.jpg" not in info["largeImageUrl"] and fetched == ["https://img/x.jpg"]
    assert info["lastFmUrl"] == "https://www.last.fm/music/Rob%20Hubbard"
    assert env.fail("getArtistInfo2.view", id="ar:0000000000000000")["code"] == 70
    ai = env.ok("getAlbumInfo2.view", id=sx.album_id("Queen", "Innuendo"))["albumInfo"]
    assert set(ai) == {"smallImageUrl", "mediumImageUrl", "largeImageUrl",   # no notes / mbid
                       "lastFmUrl"}
    assert ai["lastFmUrl"] == "https://www.last.fm/music/Queen/Innuendo"


def test_scan_status_and_start_scan_admin_only(env, monkeypatch):
    st = env.ok("getScanStatus.view")["scanStatus"]
    assert st["scanning"] is False and st["count"] == env.store.track_count()
    triggered = []

    async def _fake_trigger():
        triggered.append(True)

    monkeypatch.setattr(subsonic, "_trigger_scan", _fake_trigger)
    env.user = types.SimpleNamespace(id="u2", username="bob", role="edit", enabled=True)
    assert env.fail("startScan.view")["code"] == 50
    assert triggered == []
    env.user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    assert "scanStatus" in env.ok("startScan.view")
    assert triggered == [True]


@pytest.fixture()
def radio_favs(monkeypatch, tmp_path):
    """Station favorites in a tmp dir (never the real data dir)."""
    from soniqboom.core import radiodir
    rdir = tmp_path / "radio"
    rdir.mkdir()
    monkeypatch.setattr(radiodir, "_dir", lambda: rdir)
    favs = [
        {"sid": "scene:a", "name": "A", "homepage": "https://a.example/",
         "streams": [{"url": "https://a.example/live.m3u8", "codec": "AAC", "hls": 1},
                     {"url": "http://a.example:8000/aac", "codec": "AAC+"},
                     {"url": "http://a.example:8000/mp3", "codec": "MP3"}]},
        {"sid": "rb:b", "name": "B", "streams": [{"url": "https://b.example/x.m3u8"}]},
        {"sid": "rb:c", "name": "C", "streams": []},
    ]
    radiodir._write_json(radiodir._FAVS_FILE, favs)
    return radiodir


def test_internet_radio_stations_point_at_the_subsonic_relay(env, radio_favs):
    rows = env.ok("getInternetRadioStations.view")["internetRadioStations"]["internetRadioStation"]
    assert [r["id"] for r in rows] == ["scene:a", "rb:b"]            # no-stream station skipped
    assert rows[0]["homePageUrl"] == "https://a.example/" and "homePageUrl" not in rows[1]
    from urllib.parse import parse_qs, urlsplit
    for r in rows:
        parts = urlsplit(r["streamUrl"])
        assert parts.path == "/rest/radioStream.view"
        q = parse_qs(parts.query)
        assert q["id"] == [r["id"]] and q["token"][0]
        assert "/api/stations/relay" not in r["streamUrl"]            # cookie-gated for clients
        assert "p=" not in parts.query and "u=" not in parts.query    # no credentials in URLs
    # The Subsonic relay prefers the plain MP3 mount of a multi-stream station.
    assert subsonic._station_stream_index(radio_favs.get_favorites()[0]) == 2


def test_radio_stream_token_checks_and_ssrf_guard(env, radio_favs, monkeypatch):
    monkeypatch.setattr(subsonic, "get_user_store",
                        lambda: types.SimpleNamespace(get=lambda uid: env.user if uid == "u1" else None))
    rows = env.ok("getInternetRadioStations.view")["internetRadioStations"]["internetRadioStation"]
    from urllib.parse import parse_qs, urlsplit
    tok = parse_qs(urlsplit(rows[0]["streamUrl"]).query)["token"][0]
    # Token for station A can't open station B; garbage tokens are refused.
    assert env.fail("radioStream.view", id="rb:b", token=tok)["code"] == 40
    assert env.fail("radioStream.view", id="scene:a", token="x.y.z")["code"] == 40
    # A station whose stream resolves to a private address: the relay's SSRF
    # guard (not bypassed) refuses it — error envelope, no audio.
    favs = radio_favs.get_favorites() + [{"sid": "custom:lan", "name": "LAN",
                                          "streams": [{"url": "http://127.0.0.1:9/x"}]}]
    radio_favs._write_json(radio_favs._FAVS_FILE, favs)
    rows = env.ok("getInternetRadioStations.view")["internetRadioStations"]["internetRadioStation"]
    lan = next(r for r in rows if r["id"] == "custom:lan")
    r = env.client.get(lan["streamUrl"] + "&f=json")
    body = r.json()["subsonic-response"]
    assert body["status"] == "failed" and "non-public" in body["error"]["message"]


def test_radio_station_create_update_delete(env, radio_favs, monkeypatch):
    from soniqboom.api import stations as stations_api

    async def _public(url):
        if "127.0.0.1" in url:
            from fastapi import HTTPException
            raise HTTPException(403, "Station stream resolves to a non-public address")

    monkeypatch.setattr(stations_api, "_assert_public_url", _public)
    env.user = types.SimpleNamespace(id="u3", username="ro", role="readonly", enabled=True)
    assert env.fail("createInternetRadioStation.view", streamUrl="http://x.example/s",
                    name="X")["code"] == 50
    env.user = types.SimpleNamespace(id="u2", username="ed", role="edit", enabled=True)
    assert env.fail("createInternetRadioStation.view", streamUrl="ftp://x", name="X")["code"] == 10
    assert env.fail("createInternetRadioStation.view", streamUrl="http://127.0.0.1/s",
                    name="LAN")["code"] == 50
    env.ok("createInternetRadioStation.view", streamUrl="http://x.example/s", name="X Radio",
           homepageUrl="https://x.example/")
    # A curated scene-pack URL adds THAT station (trusted id + streams).
    scene = radio_favs.SCENE_PACK[0]
    env.ok("createInternetRadioStation.view", streamUrl=scene["streams"][0]["url"], name="whatever")
    ids = [x["sid"] for x in radio_favs.get_favorites()]
    custom = next(i for i in ids if i.startswith("custom:"))
    assert scene["sid"] in ids
    env.ok("updateInternetRadioStation.view", id=custom, streamUrl="http://y.example/s", name="Y")
    cur = next(x for x in radio_favs.get_favorites() if x["sid"] == custom)
    assert cur["name"] == "Y" and cur["streams"][0]["url"] == "http://y.example/s"
    assert env.fail("updateInternetRadioStation.view", id="scene:a",
                    streamUrl="http://z.example/s", name="Z")["code"] == 50
    env.ok("deleteInternetRadioStation.view", id=custom)
    assert custom not in [x["sid"] for x in radio_favs.get_favorites()]
    assert env.fail("deleteInternetRadioStation.view", id=custom)["code"] == 70


def test_radio_stream_plays_end_to_end_over_http(monkeypatch, tmp_path):
    """A real HTTP client fetches getInternetRadioStations, opens the returned
    streamUrl (no credentials of its own) and receives audio relayed from a
    local ICY server — with the in-band ICY metadata stripped out.  Only the
    SSRF guard's "public address" rule is relaxed, for 127.0.0.1, so the test
    can reach its own local upstream."""
    import socket
    import threading

    import httpx
    import uvicorn

    from soniqboom.api import stations as stations_api
    from soniqboom.config import settings
    from soniqboom.core import radiodir

    META = 1024
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(4)
    up_port = up.getsockname()[1]
    stop = threading.Event()

    def _client(conn):
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n"
                         b"icy-metaint: 1024\r\nicy-name: Test FM\r\n\r\n")
            title = b"StreamTitle='Test Artist - Test Song';"
            block = title + b"\0" * (-len(title) % 16)
            first = True
            while not stop.is_set():
                conn.sendall(b"\xAA" * META)
                conn.sendall(bytes([len(block) // 16]) + block if first else b"\x00")
                first = False
                time.sleep(0.005)
        except OSError:
            pass
        finally:
            conn.close()

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = up.accept()
            except OSError:
                return
            threading.Thread(target=_client, args=(conn,), daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()

    rdir = tmp_path / "radio"
    rdir.mkdir()
    monkeypatch.setattr(radiodir, "_dir", lambda: rdir)
    radiodir._write_json(radiodir._FAVS_FILE, [{
        "sid": "custom:local", "name": "Local", "homepage": "",
        "streams": [{"url": f"http://127.0.0.1:{up_port}/live", "codec": "MP3", "hls": 0}]}])
    real_guard = stations_api._assert_public_url

    async def _guard(url):
        if url.startswith(f"http://127.0.0.1:{up_port}/"):
            return                                  # the test's own upstream only
        await real_guard(url)

    monkeypatch.setattr(stations_api, "_assert_public_url", _guard)
    titles = []

    async def _meta(payload):
        titles.append(payload.get("title"))

    monkeypatch.setattr(stations_api, "_broadcast_meta", _meta)
    monkeypatch.setattr(settings, "radio_art_lookup", False)
    user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: user)
    monkeypatch.setattr(subsonic, "get_user_store",
                        lambda: types.SimpleNamespace(get=lambda uid: user if uid == "u1" else None))

    app = FastAPI()
    app.include_router(subsonic.router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started
        with httpx.Client(timeout=10) as cl:
            rows = cl.get(f"http://127.0.0.1:{port}/rest/getInternetRadioStations.view",
                          params={"f": "json", "u": "alice", "p": "x"}).json()
            url = rows["subsonic-response"]["internetRadioStations"]["internetRadioStation"][0]["streamUrl"]
            assert url.startswith(f"http://127.0.0.1:{port}/rest/radioStream.view?")

            def _no_creds(*a, **k):
                raise subsonic._SubsonicError(40, "Wrong username or password.")

            # From here on, ONLY the signed token in streamUrl can authorise.
            monkeypatch.setattr(subsonic, "_require_user", _no_creds)
            bare = cl.get(url.split("&token=")[0] + "&f=json").json()["subsonic-response"]
            assert bare["status"] == "failed" and bare["error"]["code"] == 40
            got = b""
            with cl.stream("GET", url) as r:                # no u/p: the token authorises
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("audio/mpeg")
                for chunk in r.iter_bytes():
                    got += chunk
                    if len(got) >= 8 * META:
                        break
        assert len(got) >= 8 * META
        assert set(got) == {0xAA}                     # audio only — metadata stripped
        assert "Test Artist - Test Song" in titles     # …and parsed server-side
    finally:
        server.should_exit = True
        th.join(timeout=10)
        stop.set()
        up.close()


# ── H. Cover art ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("folder_on", [False, True])
def test_cover_art_is_never_emitted_empty(env, folder_on):
    env.folder_albums(folder_on)
    bodies = [
        env.ok("getAlbumList2.view", type="newest", size=500),
        env.ok("getAlbumList.view", type="alphabeticalByName", size=500),
        env.ok("search3.view", query=""),
        env.ok("getArtists.view"),
        env.ok("getStarred2.view"),
    ]
    for name in ("Queen", "Rob Hubbard", "Martin Galway", "Madonna"):
        bodies.append(env.ok("getArtist.view", id=sx.artist_id(name)))
        bodies.append(env.ok("getMusicDirectory.view", id=sx.artist_id(name)))
    for body in bodies:
        for k, v in _walk(body):
            if k in ("coverArt", "albumId", "artistId", "parent"):
                assert v, (k, body)


def test_cover_art_placeholder_on_miss_404_only_when_malformed(env):
    hit = env.client.get("/rest/getCoverArt.view", params={"id": "q2"})
    assert hit.status_code == 200 and hit.content.startswith(b"\xff\xd8")
    for miss_id in ("q1", "no-such-track", "al:0123456789abcdef", sx.artist_id("Madonna")):
        r = env.client.get("/rest/getCoverArt.view", params={"id": miss_id, "size": 300})
        assert r.status_code == 200, miss_id
        assert r.headers["content-type"] == "image/png"
        assert "max-age=" in r.headers.get("cache-control", "")
        assert r.content.startswith(b"\x89PNG")
    # Unknown ids never reach the art pipeline (no negative-cache pollution).
    assert "no-such-track" not in env.art_requests
    for bad in ("../../etc/passwd", "al:nothex", "a b", "x" * 300):
        # Malformed: a Subsonic error document (code 70), in the requested format.
        assert env.fail("getCoverArt.view", id=bad)["code"] == 70
        r = env.client.get("/rest/getCoverArt.view", params={"id": bad})
        assert r.status_code == 200 and ET.fromstring(r.content).find("{*}error").get("code") == "70"


# ── I. Extensions + formPost ─────────────────────────────────────────────────

def test_open_subsonic_extensions_declared(env):
    exts = {e["name"] for e in env.ok("getOpenSubsonicExtensions.view")["openSubsonicExtensions"]}
    assert exts == {"songLyrics", "formPost", "indexBasedQueue", "apiKeyAuthentication",
                    "topSongsByArtistId", "transcoding", "transcodeOffset",
                    "playbackReport", "sonicSimilarity"}


def test_form_post_parameters_reach_every_handler(env):
    r = env.client.post("/rest/getArtist.view",
                        data={"id": sx.artist_id("Queen"), "f": "json"})
    assert r.status_code == 200
    assert r.json()["subsonic-response"]["artist"]["name"] == "Queen"
    # Missing required param over POST → Subsonic error 10 in the POSTed format.
    r = env.client.post("/rest/getArtist.view", data={"f": "json"})
    assert r.json()["subsonic-response"]["error"]["code"] == 10


def test_state_file_is_written_atomically(env):
    env.ok("star.view", id="q1")
    doc = json.loads(env.state_path.read_text())
    assert "q1" in doc["users"]["u1"]["starred"]["song"]
    assert not env.state_path.with_name(env.state_path.name + ".tmp").exists()


def test_artist_info_xml_uses_child_elements(env, monkeypatch):
    from soniqboom.core import artistinfo

    async def _card(name, album=None, track=None, *, is_retro=False):
        return {"found": True, "bio": "Bio text"}

    monkeypatch.setattr(artistinfo, "get_artist_info", _card)
    r = env.get("getArtistInfo2.view", id=sx.artist_id("Queen"), f="xml")
    root = ET.fromstring(r.content)
    info = root.find("{*}artistInfo2")
    assert info.get("biography") is None
    assert info.find("{*}biography").text == "Bio text"


def test_mutations_do_not_ride_a_session_cookie_with_bogus_u(monkeypatch):
    # _require_user_no_cookie must make explicit credentials authenticate on
    # their own — a cookie plus ``u=anything`` is not enough for a mutation.
    seen = {}

    def _fake_require(request, sb_session, u, p, s, t):
        seen["cookie"] = sb_session
        return types.SimpleNamespace(id="u1")

    monkeypatch.setattr(subsonic, "_require_user", _fake_require)
    req = types.SimpleNamespace(headers={}, url=types.SimpleNamespace(scheme="http", netloc="h"))
    subsonic._require_user_no_cookie(req, "cookie-token", "x", None, None, None)
    assert seen["cookie"] is None
    with pytest.raises(subsonic._SubsonicError):
        subsonic._require_user_no_cookie(req, "cookie-token", None, None, None, None)
    same = types.SimpleNamespace(headers={"origin": "http://h"},
                                 url=types.SimpleNamespace(scheme="http", netloc="h"))
    subsonic._require_user_no_cookie(same, "cookie-token", None, None, None, None)
    assert seen["cookie"] == "cookie-token"


def test_songs_by_genre_big_genre_path_matches_small_genre_order(env):
    # A genre covering most of the library takes the sorted-index walk; its
    # order must equal the direct-sort path's (added desc, id desc), and a
    # track the index doesn't hold (added_at 0) must still be listed.
    for i in range(40):
        env.store.upsert_track(_tr(f"b{i:02d}", title=f"B {i}", artist="Bulk",
                                   genre=["Bulk"], added=200 + (i // 2), store=env.store))
    t = _tr("b99", title="No date", artist="Bulk", genre=["Bulk"], store=env.store)
    t["added_at"] = 0
    env.store.upsert_track(t)
    songs = env.ok("getSongsByGenre.view", genre="Bulk", count=500)["songsByGenre"]["song"]
    ids = [s["id"] for s in songs]
    tracks = env.store._tracks
    expected = sorted((tid for tid in tracks if tracks[tid]["genre"] == ["Bulk"]),
                      key=lambda tid: (tracks[tid]["added_at"] or 0, tid), reverse=True)
    assert ids == expected and ids[-1] == "b99"
    # Small genre (3 of 56 tracks) takes the direct-sort path — same order rule.
    chip = [s["id"] for s in env.ok("getSongsByGenre.view", genre="chiptune",
                                    count=10)["songsByGenre"]["song"]]
    assert len(chip) * 16 <= len(tracks)
    assert chip == ["h3", "h2", "h1"]


# ── QA follow-ups ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("folder_on", [False, True])
def test_untagged_tracks_browsable_under_unknown_artist(env, folder_on):
    env.folder_albums(folder_on)
    arts = _all_artists(env.ok("getIndexes.view"), "indexes")
    unk = arts["[Unknown Artist]"]
    assert unk["id"] == sx.artist_id("")
    d = env.ok("getMusicDirectory.view", id=unk["id"])["directory"]
    # Owner-less album-less tracks are grouped by folder (fa: dirs) in both
    # modes — a real library has tens of thousands of them.
    assert [c["title"] for c in d["child"]] == ["unsorted"] and d["child"][0]["isDir"]
    assert d["child"][0]["id"] == "fa:" + env.store.store_hash_lookup(MODS_DIR)
    d = env.ok("getMusicDirectory.view", id=d["child"][0]["id"])["directory"]
    assert [c["id"] for c in d["child"]] == ["x1"]
    song = env.ok("getSong.view", id="x1")["song"]
    assert song["artistId"] == unk["id"]


def test_unknown_artist_absent_when_everything_is_tagged(env):
    env.store.delete_track("x1")
    arts = _all_artists(env.ok("getArtists.view"))
    assert "[Unknown Artist]" not in arts
    assert env.fail("getArtist.view", id=sx.artist_id(""))["code"] == 70


def test_last_modified_tracks_index_content_not_rebuilds(env):
    a = env.ok("getIndexes.view")["indexes"]["lastModified"]
    # A mutation that leaves the artist index unchanged (retitle a song) forces
    # a catalogue rebuild but must not move lastModified…
    t = dict(env.store.get_track("q1"), title="Renamed")
    time.sleep(0.01)
    env.store.upsert_track(t)
    assert env.ok("getIndexes.view")["indexes"]["lastModified"] == a
    # …and neither does a restart (stamp persisted with the Subsonic state).
    subsonic_state.reset_state(env.state_path)
    subsonic._ALBUM_LIST_CACHE.update(cat=None)
    assert env.ok("getIndexes.view")["indexes"]["lastModified"] == a
    # A starred artist moves it for that user only.
    time.sleep(0.01)
    env.ok("star.view", artistId=sx.artist_id("Queen"))
    assert env.ok("getIndexes.view")["indexes"]["lastModified"] > a


def test_debounced_snapshot_keeps_artists_and_albums_consistent(env, monkeypatch):
    # With the scan debounce active a rebuild may be deferred — but artists and
    # albums come from ONE snapshot (every album's artistId resolves), and an
    # id a fresh read hands out (a new song's albumId) forces one rate-limited
    # rebuild on a miss instead of answering "not found" for 5 s.
    import time as _time
    now = [1000.0]

    class _Clock:
        def __getattr__(self, name):
            return getattr(_time, name)

        def monotonic(self):
            return now[0]

    monkeypatch.setattr(subsonic, "time", _Clock())
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 5.0)
    env.ok("getAlbumList2.view", type="newest", size=500)
    env.store.upsert_track(_tr("z1", title="Z", artist="Zed Newcomer", album="Zed Album",
                               store=env.store, added=90))

    def _consistent():
        albums = env.ok("getAlbumList2.view", type="newest", size=500)["albumList2"]["album"]
        for alb in albums:
            assert env.ok("getArtist.view", id=alb["artistId"])["artist"]["albumCount"] >= 1
        return {a["name"] for a in albums}

    now[0] += 0.5                       # inside debounce AND the miss rate limit
    assert "Zed Album" not in _consistent()          # stale, but coherent
    song = env.ok("getSong.view", id="z1")["song"]   # fresh read
    assert env.fail("getAlbum.view", id=song["albumId"])["code"] == 70
    now[0] += 1.0                       # still debounced, past the miss rate limit
    assert env.ok("getAlbum.view", id=song["albumId"])["album"]["name"] == "Zed Album"
    assert env.ok("getArtist.view", id=song["artistId"])["artist"]["name"] == "Zed Newcomer"
    assert "Zed Album" in _consistent()              # the forced rebuild is shared


def test_write_endpoints_refuse_cookie_only_over_http(env, monkeypatch):
    real_user = env.user

    def _fake_resolve(request, sb_session, u, p, s, t):
        if sb_session == "good-cookie":
            return real_user
        if u == "alice" and p == "pw":
            return real_user
        return None

    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", _REAL_REQUIRE_NO_COOKIE)
    monkeypatch.setattr(subsonic, "_resolve_user", _fake_resolve)
    env.client.cookies.set("sb_session", "good-cookie")
    # Reads accept the cookie.
    assert env.ok("getStarred2.view")["starred2"]["song"] == []
    for method, params in (("star.view", {"id": "q1"}), ("unstar.view", {"id": "q1"}),
                           ("setRating.view", {"id": "q1", "rating": 3}),
                           ("savePlayQueue.view", {"id": "q1"}), ("startScan.view", {})):
        # No explicit credentials (cookie only / a bare ``u``): the spec's
        # "required parameter missing" (10), never a mutation.
        assert env.fail(method, **params)["code"] == 10, method                 # cookie only
        assert env.fail(method, u="alice", **params)["code"] == 10, method      # cookie + bogus u
        assert env.fail(method, u="alice", p="bad", **params)["code"] == 40, method  # wrong pw
        r = env.client.post(f"/rest/{method}", data={"f": "json", **params})
        assert r.json()["subsonic-response"]["error"]["code"] == 10, method     # cross-site form
    assert env.ok("star.view", id="q1", u="alice", p="pw")
    assert "starred" in env.ok("getSong.view", id="q1")["song"]


@pytest.mark.parametrize("method, params, root, child", [
    ("getIndexes.view", {}, "indexes", "index"),
    ("getMusicDirectory.view", {"id": sx.artist_id("Queen")}, "directory", "child"),
    ("getStarred.view", {}, "starred", "song"),
    ("getPlayQueue.view", {}, "playQueue", "entry"),
])
def test_xml_shapes(env, method, params, root, child):
    env.ok("star.view", id="q1")
    env.client.post("/rest/savePlayQueue.view", data={"id": ["q1", "q2"], "current": "q2",
                                                      "position": "10", "f": "json"})
    r = env.get(method, f="xml", **params)
    doc = ET.fromstring(r.content)
    assert doc.get("status") == "ok"
    el = doc.find(f"{{*}}{root}")
    assert el is not None
    kids = el.findall(f"{{*}}{child}")
    assert kids, (method, r.text[:300])
    if root == "indexes":
        assert el.get("lastModified").isdigit()
        assert kids[0].find("{*}artist").get("id").startswith("ar:")
    if root == "directory":
        assert el.get("name") == "Queen" and kids[0].get("isDir") == "true"
    if root == "playQueue":
        assert el.get("current") == "q2" and [k.get("id") for k in kids] == ["q1", "q2"]


def test_state_write_failure_keeps_previous_file(env, monkeypatch):
    env.ok("star.view", id="q1")
    before = env.state_path.read_text()

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(subsonic_state.os, "replace", _boom)
    env.ok("star.view", id="q2")                       # write fails, request doesn't
    assert env.state_path.read_text() == before       # old document intact


def test_star_ignores_ids_that_do_not_resolve(env):
    env.ok("star.view", albumId="al:0123456789abcdef", artistId="ar:0123456789abcdef")
    doc = json.loads(env.state_path.read_text()) if env.state_path.exists() else {"users": {}}
    assert not (doc["users"].get("u1") or {}).get("starred", {}).get("album")
    assert not (doc["users"].get("u1") or {}).get("starred", {}).get("artist")


def test_search3_song_paging_uses_store_offset(env):
    page = lambda o: [s["id"] for s in env.ok("search3.view", query="", artistCount=0,  # noqa: E731
                                              albumCount=0, songCount=5,
                                              songOffset=o)["searchResult3"]["song"]]
    p0, p1, p2 = page(0), page(5), page(10)
    assert len(p0) == len(p1) == 5 and not set(p0) & set(p1) and not set(p1) & set(p2)
    everything = [s["id"] for s in env.ok("search3.view", query="", artistCount=0, albumCount=0,
                                          songCount=500)["searchResult3"]["song"]]
    assert everything[:15] == p0 + p1 + p2


def test_unknown_method_with_control_chars_is_valid_xml(env):
    r = env.client.get("/rest/bad%01name.view")
    assert r.status_code == 404
    root = ET.fromstring(r.content)                    # must parse
    assert "badname" in root.find("{*}error").get("message")


def test_folder_guest_without_own_albums_gets_appears_on(env):
    # An artist whose ONLY tracks sit in a folder another artist dominates:
    # with folder albums on, that folder album is their appears-on album in
    # both ID3 and folder browsing (and their song isn't listed twice).
    env.store.upsert_track(_tr("d1", title="Guest 2", artist="Ben Daglish",
                               dir_path=HUBBARD_DIR, store=env.store, added=50, fmt="SID"))
    daglish = sx.artist_id("Ben Daglish")
    assert env.ok("getArtist.view", id=daglish)["artist"]["albumCount"] == 0
    assert [c["id"] for c in env.ok("getMusicDirectory.view", id=daglish)
            ["directory"]["child"]] == ["d1"]
    env.folder_albums(True)
    fa = "fa:" + env.store.store_hash_lookup(HUBBARD_DIR)
    art = env.ok("getArtist.view", id=daglish)["artist"]
    assert [a["id"] for a in art["album"]] == [fa] and art["albumCount"] == 1
    assert _all_artists(env.ok("getArtists.view"))["Ben Daglish"]["albumCount"] == 1
    d = env.ok("getMusicDirectory.view", id=daglish)["directory"]
    assert [c["id"] for c in d["child"]] == [fa]


# ── Second QA follow-ups ─────────────────────────────────────────────────────

_SEED_PROBE = r"""
import json, sys
from soniqboom.core.store import TrackStore
from soniqboom.core import subsonic_index as sx
st = TrackStore()
for i, name in enumerate(["4-Mat", "4-MAT", "4-mat", "Jeroen Tel", "JEROEN TEL"] * 3):
    st.upsert_track({"id": f"t{i:02d}", "title": str(i), "artist": name, "album": "",
                     "album_artist": "", "genre": [], "added_at": 1, "duration": 1.0})
cat = sx.build_catalogue(st, folder_on=False)
print(json.dumps(sorted(cat.union.display.items())))
"""


def test_artist_display_casing_is_independent_of_hash_seed(repo_root):
    import os
    import subprocess
    import sys
    outs = set()
    for seed in ("1", "2", "3", "4"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", _SEED_PROBE], cwd=repo_root, env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip().splitlines()[-1])
    assert len(outs) == 1, outs


def test_index_render_cache_is_per_user_and_bounded(env):
    alice = env.user
    env.ok("star.view", artistId=sx.artist_id("Queen"))
    for fmt in ("json", "xml"):
        assert "starred" in env.get("getArtists.view", f=fmt).text
    bob = types.SimpleNamespace(id="u2", username="bob", role="edit", enabled=True)
    env.user = bob
    for fmt in ("json", "xml"):
        assert "starred" not in env.get("getArtists.view", f=fmt).text   # no leak
    env.user = alice
    for i in range(20):                                   # many star changes
        env.ok("star.view" if i % 2 else "unstar.view", id="q1")
        env.ok("getIndexes.view")
        env.ok("getArtists.view")
    cat = subsonic._ALBUM_LIST_CACHE["cat"]
    renders = [k for k in cat.memo if isinstance(k, tuple) and k[0] == "render"]
    assert len(renders) <= 8, renders                     # slots, not one per change


def test_song_stars_do_not_move_index_last_modified(env):
    a = env.ok("getIndexes.view")["indexes"]["lastModified"]
    time.sleep(0.01)
    env.ok("star.view", id="q1", albumId=sx.album_id("Queen", "Innuendo"))
    assert env.ok("getIndexes.view")["indexes"]["lastModified"] == a


def test_artist_name_casing_change_moves_last_modified(env):
    a = env.ok("getIndexes.view")["indexes"]["lastModified"]
    time.sleep(0.01)
    for tid in ("h1", "h2", "h3"):
        env.store.upsert_track(dict(env.store.get_track(tid), artist="ROB HUBBARD"))
    body = env.ok("getIndexes.view")["indexes"]
    assert body["lastModified"] > a
    assert "ROB HUBBARD" in _all_artists({"indexes": body}, "indexes")


def test_unstar_removes_star_on_deleted_track(env):
    env.ok("star.view", id="q1")
    env.store.delete_track("q1")
    env.ok("unstar.view", id="q1")
    doc = json.loads(env.state_path.read_text())
    assert doc["users"]["u1"]["starred"]["song"] == {}


def test_star_versions_strictly_increase(env):
    st = subsonic_state.get_state()
    seen = []
    for tid in ("q1", "q2", "q3", "q4"):
        env.ok("star.view", id=tid)                    # same millisecond is likely
        seen.append(st.starred_changed_ms("u1", "song"))
    assert seen == sorted(set(seen))


def test_cover_art_pipeline_error_degrades_to_short_placeholder(env, monkeypatch):
    import soniqboom.api.art as art_mod

    async def _boom(tid):
        raise OSError("transient")

    monkeypatch.setattr(art_mod, "_resolve_full_art", _boom)
    r = env.client.get("/rest/getCoverArt.view", params={"id": "q1"})
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert "max-age=300" in r.headers["cache-control"]
    r = env.client.get("/rest/getCoverArt.view", params={"id": "al:0123456789abcdef"})
    assert "max-age=300" in r.headers["cache-control"]


def test_state_file_with_nan_and_junk_values_is_tolerated(env):
    env.state_path.write_text(
        '{"version": 1, "users": {"u1": {"starred": {"song": {"q1": NaN}, "album": []},'
        ' "starred_changed": {"artist": Infinity, "song": "x"},'
        ' "queue": {"ids": ["q1", 5], "current": 7, "position": "abc"}}},'
        ' "index_stamp": {"fp": "x", "ms": NaN}}')
    subsonic_state.reset_state(env.state_path)
    assert env.ok("getIndexes.view")["indexes"]["lastModified"] > 0
    assert [s["id"] for s in env.ok("getStarred2.view")["starred2"]["song"]] == ["q1"]
    pq = env.ok("getPlayQueue.view")["playQueue"]
    assert [e["id"] for e in pq["entry"]] == ["q1"] and pq["position"] == 0
    env.ok("star.view", albumId=sx.album_id("Queen", "Innuendo"))


def test_folder_directory_songs_point_at_the_folder_even_when_off(env):
    fa = "fa:" + env.store.store_hash_lookup(GALWAY_DIR)
    for body, key in ((env.ok("getMusicDirectory.view", id=fa)["directory"], "child"),
                      (env.ok("getAlbum.view", id=fa)["album"], "song")):
        for sng in body[key]:
            assert sng["parent"] == fa and sng["albumId"] == fa and sng["album"] == "Martin Galway"


def test_search3_punctuation_only_query_matches_no_songs(env):
    r = env.ok("search3.view", query="!!!", artistCount=5, albumCount=5, songCount=50)
    assert r["searchResult3"]["song"] == []


def test_legacy_get_lyrics_finds_track_via_indexes(env, monkeypatch):
    async def _fake_lyrics(tid):
        return {"text": f"lyrics of {tid}", "synced": False, "artist": "Queen", "title": "x"}

    monkeypatch.setattr(subsonic, "_lyrics_for_track_id", _fake_lyrics)
    body = env.ok("getLyrics.view", artist="queen", title="bohemian rhapsody")
    assert body["lyrics"]["value"] == "lyrics of q2"
    body = env.ok("getLyrics.view", title="Love of My Life")
    assert body["lyrics"]["value"] == "lyrics of q3"
    assert env.ok("getLyrics.view", title="!!!")["lyrics"]["value"] == ""


# ── Full-spec coverage: bookmarks, now playing, users, queue-by-index, … ─────

def test_bookmarks_round_trip(env):
    env.ok("createBookmark.view", id="q2", position=61000, comment="chorus")
    env.ok("createBookmark.view", id="h1", position=5)
    marks = env.ok("getBookmarks.view")["bookmarks"]["bookmark"]
    by_id = {m["entry"]["id"]: m for m in marks}
    assert by_id["q2"]["position"] == 61000 and by_id["q2"]["comment"] == "chorus"
    assert by_id["q2"]["username"] == "alice" and by_id["q2"]["created"]
    env.ok("createBookmark.view", id="q2", position=62000)        # update in place
    subsonic_state.reset_state(env.state_path)                    # persisted
    marks = {m["entry"]["id"]: m for m in env.ok("getBookmarks.view")["bookmarks"]["bookmark"]}
    assert marks["q2"]["position"] == 62000 and set(marks) == {"q2", "h1"}
    env.ok("deleteBookmark.view", id="q2")
    env.ok("deleteBookmark.view", id="q2")                        # idempotent
    assert [m["entry"]["id"] for m in env.ok("getBookmarks.view")["bookmarks"]["bookmark"]] == ["h1"]
    assert env.fail("createBookmark.view", id="nope", position=1)["code"] == 70
    r = env.get("getBookmarks.view", f="xml")
    assert ET.fromstring(r.content).find("{*}bookmarks/{*}bookmark/{*}entry") is not None


def test_now_playing_from_scrobble_and_expiry(env, monkeypatch):
    subsonic._NOW_PLAYING.clear()
    env.ok("scrobble.view", id="q1", submission="false", c="DSub")
    rows = env.ok("getNowPlaying.view")["nowPlaying"]["entry"]
    assert [(r["id"], r["username"], r["playerName"], r["minutesAgo"]) for r in rows] == \
        [("q1", "alice", "DSub", 0)]
    # Expires after the track's duration (100 s here) + grace.
    real = time.time
    monkeypatch.setattr(subsonic.time, "time", lambda: real() + 100 + 301)
    assert env.ok("getNowPlaying.view")["nowPlaying"]["entry"] == []
    monkeypatch.setattr(subsonic.time, "time", real)
    subsonic._NOW_PLAYING.clear()


def test_play_queue_by_index_handles_duplicates(env):
    r = env.client.post("/rest/savePlayQueueByIndex.view",
                        data={"id": ["q1", "q2", "q1"], "currentIndex": "2",
                              "position": "1500", "c": "Symfonium", "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"
    pq = env.ok("getPlayQueueByIndex.view")["playQueueByIndex"]
    assert [e["id"] for e in pq["entry"]] == ["q1", "q2", "q1"]
    assert pq["currentIndex"] == 2 and pq["position"] == 1500      # the SECOND q1
    assert env.ok("getPlayQueue.view")["playQueue"]["current"] == "q1"
    # A deleted track drops out and the index is re-mapped.
    env.store.delete_track("q2")
    pq = env.ok("getPlayQueueByIndex.view")["playQueueByIndex"]
    assert [e["id"] for e in pq["entry"]] == ["q1", "q1"] and pq["currentIndex"] == 1
    assert env.fail("savePlayQueueByIndex.view", id=["q1"], currentIndex=5)["code"] == 10
    env.ok("savePlayQueueByIndex.view")                            # no ids → clears
    empty = env.ok("getPlayQueueByIndex.view")["playQueueByIndex"]
    assert "entry" not in empty and "currentIndex" not in empty and empty["username"] == "alice"


@pytest.fixture()
def users(env, monkeypatch, tmp_path):
    from soniqboom.core.users import UserStore
    ustore = UserStore(tmp_path)
    ustore.create(username="admin", password="adminpass1", role="admin")
    ustore.create(username="bob", password="bobpass12", role="edit")
    monkeypatch.setattr(subsonic, "get_user_store", lambda: ustore)
    env.user = ustore.get_by_username("admin")
    return ustore


def test_user_management(env, users):
    A = {"u": "admin", "p": "adminpass1"}                  # main password: required
    names = [x["username"] for x in env.ok("getUsers.view")["users"]["user"]]
    assert names == ["admin", "bob"]
    assert "password" not in json.dumps(env.ok("getUsers.view"))
    env.ok("createUser.view", username="carol", password="enc:" + "carolpass1".encode().hex(),
           email="c@example.com", downloadRole="true", **A)
    carol = users.get_by_username("carol")
    assert carol.role == "edit" and users.authenticate("carol", "carolpass1")
    # A client's default flags (settingsRole / streamRole true) → a listener.
    env.ok("createUser.view", username="dave", password="davepass12", settingsRole="true",
           streamRole="true", **A)
    assert users.get_by_username("dave").role == "readonly"
    assert env.fail("createUser.view", username="dave", password="davepass12", **A)["code"] == 0
    assert env.fail("createUser.view", username="x", password="short", **A)["code"] == 10
    env.ok("updateUser.view", username="dave", adminRole="true", **A)
    assert users.get_by_username("dave").role == "admin"
    # Sending ONE flag never flips the others: an admin stays admin.
    env.ok("updateUser.view", username="dave", downloadRole="false", **A)
    assert users.get_by_username("dave").role == "admin"
    # Demote to a listener: every content-changing flag must end up false
    # (the admin's current upload / cover-art flags would otherwise keep "edit").
    env.ok("updateUser.view", username="dave", adminRole="false",
           **{fl: "false" for fl in subsonic._EDIT_FLAGS}, **A)
    assert users.get_by_username("dave").role == "readonly"
    # Atomic: a rejected password leaves the role untouched.
    assert env.fail("updateUser.view", username="dave", adminRole="true", password="short",
                    **A)["code"] == 10
    assert users.get_by_username("dave").role == "readonly"
    env.ok("updateUser.view", username="dave", password="newdavepass", **A)
    assert users.authenticate("dave", "newdavepass")
    u = env.ok("getUser.view", username="dave")["user"]
    assert u["adminRole"] is False and u["playlistRole"] is False       # readonly
    assert env.fail("getUser.view", username="nobody")["code"] == 70
    # Non-admins: refused everywhere except their own password.
    env.user = users.get_by_username("bob")
    B = {"u": "bob", "p": "bobpass12"}
    assert env.fail("getUsers.view")["code"] == 50
    assert env.fail("createUser.view", username="eve", password="evepass123", **B)["code"] == 50
    assert env.fail("changePassword.view", username="dave", password="hacked123", **B)["code"] == 50
    env.ok("changePassword.view", username="bob", password="bobnewpass1", **B)
    assert users.authenticate("bob", "bobnewpass1")
    # Deleting: never yourself, never the last admin; cleans up their state.
    env.user = users.get_by_username("admin")
    assert env.fail("deleteUser.view", username="admin", **A)["code"] == 50
    env.ok("deleteUser.view", username="carol", **A)
    assert users.get_by_username("carol") is None
    assert env.fail("deleteUser.view", username="carol", **A)["code"] == 70
    r = env.client.get("/rest/getAvatar.view", params={"username": "bob"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    r2 = env.client.get("/rest/getAvatar.view", params={"username": "nobody"})
    assert r2.content == r.content                     # no account enumeration


def test_account_changes_refuse_token_and_api_password_auth(env, users):
    import hashlib as _h
    users.update(users.get_by_username("bob").id, subsonic_password="apiOnlyPw1")
    tok = {"u": "bob", "s": "salt", "t": _h.md5(b"apiOnlyPw1salt").hexdigest()}
    # Token mode (replayable from any captured URL): code 42.
    assert env.fail("changePassword.view", username="bob", password="takeover1", **tok)["code"] == 42
    assert env.fail("createUser.view", username="evil", password="evilpass1", adminRole="true",
                    **tok)["code"] == 42
    # The Subsonic API password is not the main password: code 40.
    assert env.fail("changePassword.view", username="bob", password="takeover1", u="bob",
                    p="apiOnlyPw1")["code"] == 40
    assert env.fail("changePassword.view", username="bob", password="takeover1")["code"] == 42
    assert users.authenticate("bob", "bobpass12")      # unchanged
    assert users.get_by_username("evil") is None


def test_search_v1(env):
    body = env.ok("search.view", any="queen", count=2)["searchResult"]
    assert body["totalHits"] == 5 and len(body["match"]) == 2
    assert all(m["isDir"] is False for m in body["match"])
    assert env.ok("search.view", title="!!!")["searchResult"]["totalHits"] == 0


@pytest.mark.parametrize("method, root, child", [
    ("getShares.view", "shares", "share"),
    ("getPodcasts.view", "podcasts", "channel"),
    ("getNewestPodcasts.view", "newestPodcasts", "episode"),
    ("getVideos.view", "videos", "video"),
    ("getChatMessages.view", "chatMessages", "chatMessage"),
])
def test_unsupported_feature_lists_are_empty_not_html(env, method, root, child):
    assert env.ok(method)[root] == {child: []}
    r = env.get(method, f="xml")
    assert r.headers["content-type"].startswith("text/xml")
    assert ET.fromstring(r.content).find(f"{{*}}{root}") is not None


@pytest.mark.parametrize("method, code", [
    ("createShare.view", 0), ("updateShare.view", 0), ("deleteShare.view", 0),
    ("refreshPodcasts.view", 0), ("createPodcastChannel.view", 0),
    ("deletePodcastChannel.view", 0), ("deletePodcastEpisode.view", 0),
    ("downloadPodcastEpisode.view", 0), ("getPodcastEpisode.view", 70),
    ("getVideoInfo.view", 70), ("getCaptions.view", 70), ("hls.m3u8", 0),
    ("addChatMessage.view", 0),
])
def test_unsupported_feature_mutations_give_clear_errors(env, method, code):
    r = env.get(method, id="x")
    assert r.status_code == 200
    err = r.json()["subsonic-response"]["error"]
    assert err["code"] == code and "not" in err["message"].lower()


def test_api_key_invalid_is_44_and_conflicts_are_43(env, users, monkeypatch):
    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    assert env.fail("ping.view", apiKey="abc")["code"] == 44
    assert env.fail("ping.view", apiKey="abc", u="admin")["code"] == 43


# ── Third QA follow-ups ──────────────────────────────────────────────────────

def test_new_write_endpoints_refuse_cookie_only_over_http(env, radio_favs, monkeypatch):
    real_user = env.user

    def _fake_resolve(request, sb_session, u, p, s, t):
        return real_user if sb_session == "good-cookie" else None

    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", _REAL_REQUIRE_NO_COOKIE)
    monkeypatch.setattr(subsonic, "_resolve_user", _fake_resolve)
    env.client.cookies.set("sb_session", "good-cookie")
    before = json.dumps(radio_favs.get_favorites())
    cases = [
        ("createInternetRadioStation.view", {"streamUrl": "http://x.example/s", "name": "X"}, 10),
        ("updateInternetRadioStation.view", {"id": "scene:a", "name": "Z"}, 10),
        ("deleteInternetRadioStation.view", {"id": "scene:a"}, 10),
        ("createBookmark.view", {"id": "q1", "position": 5}, 10),
        ("deleteBookmark.view", {"id": "q1"}, 10),
        ("savePlayQueueByIndex.view", {"id": "q1", "currentIndex": 0}, 10),
        # Account changes need u + p — a cookie never suffices.
        ("createUser.view", {"username": "evil", "password": "evilpass1"}, 42),
        ("updateUser.view", {"username": "admin", "adminRole": "true"}, 42),
        ("deleteUser.view", {"username": "bob"}, 42),
        ("changePassword.view", {"username": "alice", "password": "newpass123"}, 42),
    ]
    for method, params, code in cases:
        assert env.fail(method, **params)["code"] == code, method              # cookie only
        r = env.client.post(f"/rest/{method}", data={"f": "json", **params})    # cross-site form
        assert r.json()["subsonic-response"]["error"]["code"] == code, method
    assert json.dumps(radio_favs.get_favorites()) == before                    # nothing changed
    assert env.ok("getBookmarks.view")["bookmarks"]["bookmark"] == []


def test_radio_token_expiry_disabled_deleted_and_password_change(env, radio_favs, monkeypatch):
    owner = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True,
                                  password_hash="scrypt$1")
    env.user = owner
    monkeypatch.setattr(subsonic, "get_user_store",
                        lambda: types.SimpleNamespace(get=lambda uid: owner if uid == "u1" else None))
    calls = []

    async def _fake_relay(sid, v=0):
        calls.append((sid, v))
        from fastapi.responses import Response as _R
        return _R(b"audio", media_type="audio/mpeg")

    from soniqboom.api import stations as stations_api
    monkeypatch.setattr(stations_api, "relay", _fake_relay)

    def _url():
        rows = env.ok("getInternetRadioStations.view")["internetRadioStations"]["internetRadioStation"]
        return rows[0]["streamUrl"].split("/rest/", 1)[1]

    ok_url = _url()
    assert env.client.get("/rest/" + ok_url).content == b"audio"
    assert calls == [("scene:a", 2)]                   # the preferred MP3 mount
    # Password change revokes links issued before it.
    owner.password_hash = "scrypt$2"
    assert env.client.get("/rest/" + ok_url + "&f=json").json()["subsonic-response"]["error"]["code"] == 40
    fresh = _url()
    owner.enabled = False                              # disabled owner
    assert env.client.get("/rest/" + fresh + "&f=json").json()["subsonic-response"]["error"]["code"] == 40
    owner.enabled = True
    real_time = time.time
    monkeypatch.setattr(subsonic.time, "time", lambda: real_time() + 31 * 24 * 3600)
    assert env.client.get("/rest/" + fresh + "&f=json").json()["subsonic-response"]["error"]["code"] == 40
    monkeypatch.setattr(subsonic.time, "time", real_time)
    monkeypatch.setattr(subsonic, "get_user_store",
                        lambda: types.SimpleNamespace(get=lambda uid: None))   # deleted owner
    assert env.client.get("/rest/" + fresh + "&f=json").json()["subsonic-response"]["error"]["code"] == 40
    # A transcode-shaped token signed with the same key is not a radio token.
    other = subsonic._sign_token({"tid": "q1", "exp": int(time.time()) + 60})
    assert env.fail("radioStream.view", id="scene:a", token=other)["code"] == 40


def test_bookmarks_are_per_user(env):
    env.ok("createBookmark.view", id="q1", position=10)
    env.user = types.SimpleNamespace(id="u2", username="bob", role="edit", enabled=True)
    assert env.ok("getBookmarks.view")["bookmarks"]["bookmark"] == []
    env.ok("deleteBookmark.view", id="q1")             # bob can't delete alice's
    env.user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    assert len(env.ok("getBookmarks.view")["bookmarks"]["bookmark"]) == 1


def test_now_playing_cap_download_and_scrobble_precedence(env):
    subsonic._NOW_PLAYING.clear()
    for i in range(subsonic._NOW_PLAYING_MAX + 40):
        subsonic._note_now_playing(env.user, "q1", f"player{i}")
    assert len(subsonic._NOW_PLAYING) == subsonic._NOW_PLAYING_MAX
    subsonic._NOW_PLAYING.clear()
    env.ok("scrobble.view", id="q1", submission="false", c="Symfonium")
    # A later stream of another track from the same player is a prefetch.
    subsonic._note_now_playing(env.user, "q2", "Symfonium")
    rows = env.ok("getNowPlaying.view")["nowPlaying"]["entry"]
    assert [r["id"] for r in rows] == ["q1"]
    env.ok("scrobble.view", id="q3", submission="false", c="Symfonium")
    assert [r["id"] for r in env.ok("getNowPlaying.view")["nowPlaying"]["entry"]] == ["q3"]
    subsonic._NOW_PLAYING.clear()


def test_xml_strips_illegal_control_characters(env):
    t = dict(env.store.get_track("q1"), title="Bad\x01Title\x1f", artist="Queen")
    env.store.upsert_track(t)
    subsonic._note_now_playing(env.user, "q1", "evil\x01player")
    for method, params in (("getSong.view", {"id": "q1"}), ("getNowPlaying.view", {})):
        r = env.get(method, f="xml", **params)
        root = ET.fromstring(r.content)                 # parses
        assert root.get("status") == "ok"
    assert ET.fromstring(env.get("getSong.view", f="xml", id="q1").content) \
        .find("{*}song").get("title") == "BadTitle"
    subsonic._NOW_PLAYING.clear()


def test_radio_station_input_hardening(env, radio_favs, monkeypatch):
    from soniqboom.api import stations as stations_api

    async def _public(url):
        return None

    monkeypatch.setattr(stations_api, "_assert_public_url", _public)
    assert env.fail("createInternetRadioStation.view", streamUrl="http://x.example:99999/s",
                    name="X")["code"] == 10
    env.ok("createInternetRadioStation.view", streamUrl="http://x.example/s", name="X",
           homepageUrl="javascript:alert(1)")
    cur = next(x for x in radio_favs.get_favorites() if x["sid"].startswith("custom:"))
    assert cur["homepage"] == ""
    # Editing the URL, then re-adding the OLD URL, creates a second station.
    env.ok("updateInternetRadioStation.view", id=cur["sid"], streamUrl="http://y.example/s")
    env.ok("createInternetRadioStation.view", streamUrl="http://x.example/s", name="X again")
    assert sum(1 for x in radio_favs.get_favorites() if x["sid"].startswith("custom:")) == 2
