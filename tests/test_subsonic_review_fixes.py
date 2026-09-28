# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subsonic review fixes (round 1): auth cost + lockout, API keys, Child
fields, default folder albums, catalogue caching, gzip memo, formPost scope,
JSONP, index sorting, HEAD, download, transcoding extension, cover art.

Same harness as tests/test_subsonic_parity.py: the real ``/rest`` router over
ASGI, a hand-built ``TrackStore``, per-user state in a tmp file.  Nothing here
touches the real data dir or network.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
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

from test_subsonic_parity import (  # noqa: F401 — env / radio_favs / users are fixtures
    HUBBARD_DIR, MODS_DIR, _REAL_REQUIRE_NO_COOKIE, _REAL_REQUIRE_USER, _tr, env, radio_favs,
    users,
)


def _real_auth(monkeypatch):
    monkeypatch.setattr(subsonic, "_require_user", _REAL_REQUIRE_USER)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", _REAL_REQUIRE_NO_COOKIE)


# ── r1-sub-1: p= auth is O(1), never locks out, never rewrites users.json ────

def test_plain_password_uses_the_fast_path_not_scrypt(env, users, monkeypatch):
    _real_auth(monkeypatch)
    calls = []
    real = users.authenticate_subsonic_password
    monkeypatch.setattr(users, "authenticate_subsonic_password",
                        lambda u, p: calls.append(threading.current_thread().name) or real(u, p))
    for _ in range(5):
        env.ok("ping.view", u="bob", p="bobpass12")
        env.ok("ping.view", u="bob", p="enc:" + "bobpass12".encode().hex())
    assert calls == []                              # seeded copy → no scrypt at all
    # A wrong password DOES run the scrypt check (lockout accounting) — in a
    # worker thread, never on the event loop.
    assert env.fail("ping.view", u="bob", p="wrongpass1")["code"] == 40
    assert len(calls) == 1 and calls[0] != threading.main_thread().name


def test_separate_subsonic_password_never_locks_the_account(env, users, monkeypatch):
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    users.update(bob.id, subsonic_password="clientOnly9")
    for _ in range(20):
        env.ok("ping.view", u="bob", p="clientOnly9")
    assert not users.is_locked("bob")
    assert users.authenticate("bob", "bobpass12") is not None     # web login still fine


def test_locked_account_refuses_even_the_correct_fast_path_password(env, users, monkeypatch):
    _real_auth(monkeypatch)
    for _ in range(15):
        users.note_failed_attempt("bob")
    assert users.is_locked("bob")
    assert env.fail("ping.view", u="bob", p="bobpass12")["code"] == 40


def test_successful_auth_does_not_rewrite_users_json(users):
    path = users._path
    users.authenticate("bob", "bobpass12")          # may persist (first login stamp)
    before = os.stat(path).st_mtime_ns
    time.sleep(0.02)
    for _ in range(3):
        assert users.authenticate("bob", "bobpass12") is not None
    assert os.stat(path).st_mtime_ns == before


def test_token_mode_failures_lock_only_token_mode(env, users, monkeypatch):
    import hashlib
    _real_auth(monkeypatch)
    for i in range(15):
        env.fail("ping.view", u="bob", s=f"salt{i}", t="0" * 32)
    good = {"u": "bob", "s": "abc", "t": hashlib.md5(b"bobpass12abc").hexdigest()}
    assert env.fail("ping.view", **good)["code"] == 40          # token scope locked
    assert not users.is_locked("bob")                            # main login untouched
    env.ok("ping.view", u="bob", p="bobpass12")
    msg = env.fail("ping.view", u="nobody", s="x", t="y")["message"]
    assert "Subsonic app password" in msg and "My Account" in msg


def test_rest_stream_authenticates_once_and_preauths_stream_track(env, users, monkeypatch):
    _real_auth(monkeypatch)
    from fastapi.responses import Response
    from soniqboom.api import stream as stream_mod
    seen = {}

    async def _fake_stream_track(**kw):
        seen["bypass"] = stream_mod._cast_internal_bypass_ctx.get()
        seen["seek"] = kw["seek"]
        return Response(b"audio", media_type="audio/mpeg")

    monkeypatch.setattr(stream_mod, "stream_track", _fake_stream_track)
    calls = []
    real = users.authenticate
    monkeypatch.setattr(users, "authenticate", lambda u, p: calls.append(u) or real(u, p))
    r = env.client.get("/rest/stream.view", params={"id": "q1", "u": "bob", "p": "bobpass12",
                                                    "timeOffset": 30})
    assert r.status_code == 200 and r.content == b"audio"
    assert seen == {"bypass": True, "seek": 30.0}
    assert calls == []
    assert r.headers["x-accel-buffering"] == "no"
    assert stream_mod._cast_internal_bypass_ctx.get() is False    # reset afterwards


# ── r1-sub-11: OpenSubsonic API keys ─────────────────────────────────────────

def test_api_keys_authenticate_and_tokeninfo(env, users, monkeypatch):
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    key, ent = users.create_api_key(bob.id, "phone")
    assert env.ok("ping.view", apiKey=key)
    assert env.ok("tokenInfo.view", apiKey=key)["tokenInfo"] == {"username": "bob"}
    assert env.fail("ping.view", apiKey=key, u="bob")["code"] == 43
    assert env.fail("ping.view", apiKey=key, p="x")["code"] == 43
    assert env.fail("ping.view", apiKey="nope")["code"] == 44
    # formPost: the key in a POST body.
    r = env.client.post("/rest/ping.view", data={"apiKey": key, "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"
    # A mutation endpoint accepts a key without any cookie (no CSRF refusal).
    env.ok("star.view", id="q1", apiKey=key)
    assert "q1" in subsonic_state.get_state().starred(bob.id, "song")
    # Account changes still demand the main password.
    assert env.fail("changePassword.view", username="bob", password="x" * 9,
                    apiKey=key)["code"] == 42
    # Stored hashed only; survive a reload; revocation is immediate.
    raw = users._path.read_text()
    assert key not in raw and ent["id"] in raw
    users.reload()
    env.ok("ping.view", apiKey=key)
    assert [k["name"] for k in users.list_api_keys(bob.id)] == ["phone"]
    assert users.revoke_api_key(bob.id, ent["id"])
    assert env.fail("ping.view", apiKey=key)["code"] == 44
    # A disabled account's key stops working; a deleted user's keys are gone.
    key2, _ = users.create_api_key(bob.id, "tablet")
    users.update(bob.id, enabled=False)
    assert env.fail("ping.view", apiKey=key2)["code"] == 44
    users.update(bob.id, enabled=True)
    users.delete(bob.id)
    assert users.lookup_api_key(key2) is None


def test_app_password_survives_login_and_password_change(env, users, monkeypatch):
    import hashlib
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    tok = lambda pw: {"u": "bob", "s": "salt", "t": hashlib.md5((pw + "salt").encode()).hexdigest()}
    env.ok("ping.view", **tok("bobpass12"))            # seeded with the login password
    users.update(bob.id, subsonic_password="AppPw12345678")
    assert users.authenticate("bob", "bobpass12")       # a web login …
    env.ok("ping.view", **tok("AppPw12345678"))         # … keeps the app password
    assert env.fail("ping.view", **tok("bobpass12"))["code"] == 40
    users.set_password(bob.id, "bobNewPass9")           # … and so does a password change
    env.ok("ping.view", **tok("AppPw12345678"))
    env.ok("ping.view", u="bob", p="bobNewPass9")       # p= takes the login password …
    env.ok("ping.view", u="bob", p="AppPw12345678")     # … or the app password
    assert env.fail("ping.view", u="bob", p="bobpass12")["code"] == 40
    users.update(bob.id, subsonic_password="")           # removed: token sign-in off
    assert users.authenticate("bob", "bobNewPass9")
    assert users.get_by_username("bob").subsonic_password == ""     # never re-seeded
    assert env.fail("ping.view", **tok("bobNewPass9"))["code"] == 40
    # An account that never customised it follows the login password.
    admin = users.get_by_username("admin")
    users.set_password(admin.id, "adminNew123")
    assert users.get_by_username("admin").subsonic_password == "adminNew123"


def test_main_password_p_auth_is_o1_after_the_first_scrypt(env, users, monkeypatch):
    _real_auth(monkeypatch)
    bob = users.get_by_username("bob")
    users.update(bob.id, subsonic_password="AppPw12345678")
    calls = []
    real = users.authenticate_subsonic_password
    monkeypatch.setattr(users, "authenticate_subsonic_password",
                        lambda u, p: calls.append(u) or real(u, p))
    for _ in range(5):
        env.ok("ping.view", u="bob", p="bobpass12")
    assert calls == ["bob"]                              # one scrypt, then the O(1) cache
    users.set_password(bob.id, "bobNewPass9")            # a password change drops it
    assert env.fail("ping.view", u="bob", p="bobpass12")["code"] == 40


def test_api_key_endpoints_under_api_me(users, monkeypatch):
    from soniqboom.api import users as users_api
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    bob = users.get_by_username("bob")
    app.dependency_overrides[users_api.require_user] = lambda: bob
    monkeypatch.setattr(users_api, "get_user_store", lambda: users)
    c = TestClient(app)
    made = c.post("/api/me/api-keys", json={"name": "DSub"}).json()
    assert made["name"] == "DSub" and len(made["key"]) >= 40
    listed = c.get("/api/me/api-keys").json()["keys"]
    assert [k["id"] for k in listed] == [made["id"]] and "key" not in listed[0]
    assert "sha256" not in listed[0]
    assert c.delete(f"/api/me/api-keys/{made['id']}").json() == {"ok": True}
    assert c.delete(f"/api/me/api-keys/{made['id']}").status_code == 404
    # The Subsonic app password endpoint: set, then remove (kept as "").
    assert c.put("/api/me/subsonic-password", json={"password": "Gen3rated99"}).json()[
        "user"]["subsonic_password"] is True
    assert users.get_by_username("bob").subsonic_password == "Gen3rated99"
    assert c.put("/api/me/subsonic-password", json={"password": ""}).json()[
        "user"]["subsonic_password"] is False
    assert users.get_by_username("bob").subsonic_password == ""


# ── r1-sub-2 / r1-sub-13 / r1-sub-17 / r1-sub-29: the song Child ─────────────

def _song(t: dict, ctx=None) -> dict:
    t = {"id": "x", "title": "T", "artist": "A", "album": "", "genre": [], **t}
    return subsonic._track_to_song(t, ctx)


@pytest.mark.parametrize("path, fmt, suffix, ctype, tsuffix", [
    ("/m/legendcrack.fc", "FutureComposer 1.4", "fc", "application/octet-stream", "wav"),
    ("/m/a.ogg", "Ogg Vorbis", "ogg", "audio/ogg", None),
    ("/m/a.mpc", "Musepack", "mpc", "audio/x-musepack", "wav"),
    ("/m/a.mp3", "MP3", "mp3", "audio/mpeg", None),
    ("/m/a.m4a", "AAC", "m4a", "audio/mp4", None),
    ("/m/b.m4a", "ALAC", "m4a", "audio/mp4", "wav"),
    ("/m/tune.xm", "FastTracker 2", "xm", "audio/x-mod", "wav"),
    ("/m/pack.zip::inner/Commando.sid", "SID", "sid", "audio/prs.sid", "wav"),
    ("/m/a.dsf", "DSD", "dsf", "audio/x-dsd", "wav"),
    ("/m/noext", "TFMX Pro", "tfmxpro", "application/octet-stream", "wav"),
])
def test_suffix_content_type_and_transcoded_fields(path, fmt, suffix, ctype, tsuffix):
    s = _song({"path": path, "format": fmt})
    assert s["suffix"] == suffix and s["contentType"] == ctype
    assert " " not in s["contentType"]
    if tsuffix:
        assert (s["transcodedSuffix"], s["transcodedContentType"]) == ("wav", "audio/wav")
    else:
        assert "transcodedSuffix" not in s and "transcodedContentType" not in s


def test_child_carries_play_count_bpm_comment_isrc_and_bookmark(env):
    env.store.record_play("q2")
    env.store.update_track_fields("q2", {"bpm": 72.4, "comment": "live", "composer": "Mercury",
                                         "isrc": "GBUM71029604"})
    env.ok("createBookmark.view", id="q2", position=61000)
    s = env.ok("getSong.view", id="q2")["song"]
    assert s["playCount"] == 1 and s["played"].endswith("Z")
    assert s["bpm"] == 72 and s["comment"] == "live" and s["displayComposer"] == "Mercury"
    assert s["isrc"] == ["GBUM71029604"] and s["bookmarkPosition"] == 61000
    other = env.ok("getSong.view", id="q1")["song"]
    assert other["playCount"] == 0 and "played" not in other and other["isrc"] == []
    assert other["replayGain"] == {} and other["genres"] == [{"name": "Rock"}]
    # A NaN bpm never breaks the listing; XML with isrc parses.
    env.store.update_track_fields("q3", {"bpm": float("nan")})
    assert env.ok("getSong.view", id="q3")["song"]["bpm"] == 0
    root = ET.fromstring(env.get("getSong.view", id="q2", f="xml").content)
    assert root.find("{*}song").find("{*}isrc").text == "GBUM71029604"


def test_compilation_song_links_its_own_artist(env):
    s = env.ok("getSong.view", id="c1")["song"]
    assert s["artist"] == "Madonna" and s["artistId"] == sx.artist_id("Madonna")
    assert s["albumArtist"] == "Various Artists"
    assert s["parent"] == s["albumId"] == sx.album_id("Various Artists", "Hits 1990")
    art = env.ok("getArtist.view", id=s["artistId"])["artist"]
    assert [a["id"] for a in art["album"]] == [s["albumId"]]


def test_ownerless_song_parent_matches_its_folder_listing_when_off(env):
    fa = "fa:" + env.store.store_hash_lookup(MODS_DIR)
    listed = env.ok("getMusicDirectory.view", id=fa)["directory"]["child"]
    song = env.ok("getSong.view", id="x1")["song"]
    row = next(c for c in listed if c["id"] == "x1")
    assert (song["parent"], song["albumId"], song["album"]) == \
        (row["parent"], row["albumId"], row["album"]) == (fa, fa, row["album"])
    # An OWNED loose track keeps its artist as parent, no albumId (folder off).
    h = env.ok("getSong.view", id="h1")["song"]
    assert h["parent"] == sx.artist_id("Rob Hubbard") and "albumId" not in h


# ── r1-sub-3: folder albums are the default ──────────────────────────────────

def test_default_folder_albums_give_id3_clients_every_artist(env):
    env.store._config.pop("subsonic_folder_albums", None)       # never configured
    genres = {g["value"]: g for g in env.ok("getGenres.view")["genres"]["genre"]}
    assert genres["Chiptune"]["albumCount"] > 0
    by = env.ok("getAlbumList2.view", type="byGenre", genre="Chiptune")["albumList2"]["album"]
    assert by
    art = env.ok("getArtist.view", id=sx.artist_id("Rob Hubbard"))["artist"]
    assert art["album"]
    for a in art["album"]:
        album = env.ok("getAlbum.view", id=a["id"])["album"]
        for sng in album["song"]:
            assert env.ok("getAlbum.view", id=sng["albumId"])["album"]["id"] == a["id"]
    # Explicit opt-out restores the album-less shape.
    env.folder_albums(False)
    assert env.ok("getArtist.view", id=sx.artist_id("Rob Hubbard"))["artist"]["album"] == []


# ── r1-sub-33: folder album names ─────────────────────────────────────────────

@pytest.mark.parametrize("path, name", [
    ("/music/C64/MUSICIANS/Gold_of_the_Aztecs", "Gold of the Aztecs"),
    ("/music/Amiga/UR.zip", "UR"),
    ("/music/mods/uridium2.mod.zip", "uridium2"),
    ("/music/Amiga/Turrican.II.zip", "Turrican II"),
    ("/music/Turrican 2/S-Z", "Turrican 2 / S-Z"),
    ("/music/Turner_Steve/games", "Turner Steve / games"),
    ("/music/mods/unsorted", "unsorted"),
    ("/music/Demo.lha::music/Title Tune", "Title Tune"),
])
def test_folder_album_names_are_cleaned(path, name):
    store = types.SimpleNamespace(resolve_hash=lambda h: path)
    sx._FOLDER_NAMES.pop("feedfacefeedface", None)
    try:
        assert sx.folder_album_name(store, "feedfacefeedface") == name
    finally:
        sx._FOLDER_NAMES.pop("feedfacefeedface", None)


# A wrapper archive's music extension is dropped with the archive's; any other
# dotted word is part of the name.
@pytest.mark.parametrize("comp, name", [
    ("tune.mod.zip", "tune"),
    ("TUNE.MOD.ZIP", "TUNE"),
    ("piano2.xm.lha", "piano2"),
    ("transfer.669.zip", "transfer"),
    ("Gold.of.the.Aztecs.mod.zip", "Gold of the Aztecs"),
    ("Turrican.II.zip", "Turrican II"),
    ("Dr.Who.lha", "Dr Who"),
    ("Album.v2.zip", "Album v2"),
    ("Turrican.II", "Turrican II"),                 # not an archive
])
def test_only_a_music_extension_is_dropped_from_an_archive_name(comp, name):
    assert sx._clean_component(comp) == name


def test_folder_album_name_is_identical_everywhere(env):
    fa = "fa:" + env.store.store_hash_lookup(MODS_DIR)
    unknown = env.ok("getMusicDirectory.view", id=sx.artist_id(""))["directory"]["child"]
    title = next(c["title"] for c in unknown if c["id"] == fa)
    assert env.ok("getAlbum.view", id=fa)["album"]["name"] == title
    assert env.ok("getSong.view", id="x1")["song"]["album"] == title


# ── r1-sub-4 / r1-sub-31: catalogue keyed on the catalogue seq, SWR ──────────

def test_cover_art_only_write_keeps_the_catalogue(env):
    cat = subsonic._catalogue(env.store)
    env.store.update_track_fields("q1", {"cover_art": "/api/art/q1"})
    assert subsonic._catalogue(env.store) is cat
    env.store.update_track_fields("q1", {"album": "Renamed"})
    assert subsonic._catalogue(env.store) is not cat


def test_large_library_rebuilds_in_the_background(env, monkeypatch):
    monkeypatch.setattr(subsonic, "_SWR_MIN_BUILD_SEC", 0.0)

    async def _run():
        first = subsonic._catalogue(env.store)
        env.store.upsert_track(_tr(store=env.store, tid="n1", title="New", artist="Newbie",
                                   album="Fresh", added=99))
        stale = subsonic._catalogue(env.store)
        assert stale is first                                    # served stale …
        task = subsonic._ALBUM_LIST_CACHE["task"]
        assert task is not None and not task.done()             # … while rebuilding
        await task
        fresh = subsonic._catalogue(env.store)
        assert fresh is not first and sx.album_id("Newbie", "Fresh") in fresh.by_id
        # The chunked build equals the one-shot build.
        one = sx.build_catalogue(env.store, folder_on=fresh.folder_on)
        assert {e.id: (e.song_count, e.name, e.artist_id) for e in one.entries} == \
            {e.id: (e.song_count, e.name, e.artist_id) for e in fresh.entries}
        assert one.union.display == fresh.union.display
    asyncio.run(_run())


def test_stale_miss_awaits_a_rebuild_instead_of_failing(env, monkeypatch):
    monkeypatch.setattr(subsonic, "_SWR_MIN_BUILD_SEC", 0.0)
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 60.0)
    monkeypatch.setattr(subsonic, "_MISS_REBUILD_MIN_SEC", 0.0)
    env.ok("getArtists.view")                                    # snapshot built
    env.store.upsert_track(_tr(store=env.store, tid="n2", title="New", artist="Late Band",
                               album="Late Album", added=98))
    album = env.ok("getAlbum.view", id=sx.album_id("Late Band", "Late Album"))["album"]
    assert [s["id"] for s in album["song"]] == ["n2"]


def test_unknown_cover_ids_never_rebuild_the_catalogue(env, monkeypatch):
    env.ok("getArtists.view")
    builds = []
    real = sx.build_catalogue
    monkeypatch.setattr(sx, "build_catalogue", lambda *a, **k: builds.append(1) or real(*a, **k))
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 60.0)
    monkeypatch.setattr(subsonic, "_MISS_REBUILD_MIN_SEC", 0.0)
    env.store.upsert_track(_tr(store=env.store, tid="n3", title="x", artist="Z", added=97))
    for _ in range(5):
        r = env.client.get("/rest/getCoverArt.view", params={"id": "al:0123456789abcdef"})
        assert r.headers["content-type"] == "image/png"
    assert builds == []
    # …whereas a brand-new album id on getAlbum still gets one rebuild.
    env.store.upsert_track(_tr(store=env.store, tid="n4", title="y", artist="Z",
                               album="Zed", added=96))
    env.ok("getAlbum.view", id=sx.album_id("Z", "Zed"))
    assert len(builds) == 1


# ── r1-sub-5: gzip body memoised ─────────────────────────────────────────────

def test_get_artists_gzip_is_compressed_once(env, monkeypatch):
    for i in range(60):                                  # > the 1000-byte threshold
        env.store.upsert_track(_tr(store=env.store, tid=f"g{i}", title="t",
                                   artist=f"Gzip Artist {i}", added=200 + i))
    calls = []
    real = gzip.compress
    monkeypatch.setattr(gzip, "compress", lambda *a, **k: calls.append(1) or real(*a, **k))
    plain = env.client.get("/rest/getArtists.view", params={"f": "json"},
                           headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    for _ in range(3):
        r = env.client.get("/rest/getArtists.view", params={"f": "json"},
                           headers={"Accept-Encoding": "gzip"})
        assert r.headers["content-encoding"] == "gzip"
        assert r.headers["vary"] == "Accept-Encoding"
        assert r.content == plain.content                 # httpx decoded it
    assert len(calls) == 1
    env.ok("star.view", artistId=sx.artist_id("Queen"))   # per-user body → new memo
    r = env.client.get("/rest/getArtists.view", params={"f": "json"},
                       headers={"Accept-Encoding": "gzip"})
    starred = [a for i in r.json()["subsonic-response"]["artists"]["index"]
               for a in i["artist"] if a.get("starred")]
    assert [a["name"] for a in starred] == ["Queen"]


# ── r1-sub-6: formPost never writes credentials into the ASGI scope ─────────

def test_form_post_does_not_mutate_the_server_scope(env):
    seen = {}
    inner = env.client.app

    async def _spy(scope, receive, send):
        seen["scope"] = scope
        await inner(scope, receive, send)

    r = TestClient(_spy).post("/rest/ping.view", data={"u": "alice", "p": "TopSecretPw",
                                                       "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"
    assert seen["scope"]["query_string"] == b""


# ── r1-sub-19: JSONP ──────────────────────────────────────────────────────────

def test_jsonp_wraps_the_callback(env):
    r = env.client.get("/rest/ping.view", params={"f": "jsonp", "callback": "cb"})
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.content.startswith(b"cb(") and r.content.endswith(b");")
    body = json.loads(r.content[3:-2])
    assert body["subsonic-response"]["status"] == "ok"
    bad = env.client.get("/rest/ping.view", params={"f": "jsonp", "callback": "alert(1)//"})
    assert bad.json()["subsonic-response"]["error"]["code"] == 10
    bare = env.client.get("/rest/ping.view", params={"f": "jsonp"})
    assert bare.json()["subsonic-response"]["status"] == "ok"
    post = env.client.post("/rest/getArtist.view",
                           data={"id": sx.artist_id("Queen"), "f": "jsonp", "callback": "x.y"})
    assert post.content.startswith(b"x.y(")
    err = env.client.get("/rest/getArtist.view", params={"f": "jsonp", "callback": "cb"})
    assert err.content.startswith(b"cb(") and b'"code":10' in err.content


# ── r1-sub-20: ignored articles + accent folding in the index ────────────────

def test_index_honours_ignored_articles_and_accents(env):
    for i, name in enumerate(("The Beatles", "Émilie", "Ødegaard", "The", "2Pac",
                              "Bauhaus", "Blur")):
        env.store.upsert_track(_tr(store=env.store, tid=f"ix{i}", title="t", artist=name,
                                   added=300 + i))
    idx = {b["name"]: [a["name"] for a in b["artist"]]
           for b in env.ok("getIndexes.view")["indexes"]["index"]}
    assert "The Beatles" in idx["B"] and "Émilie" in idx["E"] and "Ødegaard" in idx["O"]
    assert "The" in idx["T"] and "2Pac" in idx["#"]
    b = idx["B"]
    assert b.index("Bauhaus") < b.index("The Beatles") < b.index("Blur")


# ── r1-sub-9 / r1-sub-22 / r1-sub-23 / r1-sub-24: small spec fixes ───────────

def test_timestamps_have_a_zone_and_empty_created_is_omitted(env):
    env.ok("savePlayQueue.view", id=["q1"], current="q1")
    assert env.ok("getPlayQueue.view")["playQueue"]["changed"].endswith("Z")
    env.store.update_track_fields("q4", {"added_at": 0})
    assert "created" not in env.ok("getSong.view", id="q4")["song"]


def test_music_folder_id_is_an_int_and_envelope_is_lean(env):
    mf = env.ok("getMusicFolders.view")["musicFolders"]["musicFolder"][0]
    assert mf["id"] == 0 and isinstance(mf["id"], int)
    root = ET.fromstring(env.get("getMusicFolders.view", f="xml").content)
    assert root.find("{*}musicFolders").get("id") is None
    assert list(root)[0].tag.endswith("musicFolders")
    assert "openSubsonicExtensions" not in env.ok("ping.view")
    ping_xml = ET.fromstring(env.get("ping.view", f="xml").content)
    assert ping_xml.find("{*}openSubsonicExtensions") is None
    names = {e["name"] for e in env.ok("getOpenSubsonicExtensions.view")["openSubsonicExtensions"]}
    assert {"songLyrics", "formPost", "indexBasedQueue"} <= names


def test_player_id_is_stable():
    assert subsonic._player_id("u1", "DSub") == subsonic._player_id("u1", "DSub")
    assert subsonic._player_id("u1", "DSub") == 94029277     # sha1-derived, not hash()
    assert subsonic._player_id("u1", "DSub") != subsonic._player_id("u1", "Amperfy")


# ── r1-sub-7: similar songs from album / artist ids ──────────────────────────

def test_similar_songs_accepts_album_and_artist_ids(env, monkeypatch):
    from soniqboom.core import data as data_mod
    from soniqboom.core import similar as similar_mod
    seeds_seen = []

    async def _no_ratings():
        return {}

    def _fake(seed, cands, waves, *, ratings, k, sample_jaccard):
        seeds_seen.append(seed["id"])
        out = [{"track": env.store.get_track(t), "score": sc}
               for t, sc in (("m1", 0.2), ("g1", 0.9), ("q3", 0.5))]
        if seed["id"] == "q1":
            out.append({"track": env.store.get_track("m2"), "score": 0.95})
        return out

    monkeypatch.setattr(data_mod, "get_all_ratings", _no_ratings)
    monkeypatch.setattr(similar_mod, "find_similar", _fake)
    got = env.ok("getSimilarSongs2.view", id=sx.artist_id("Queen"), count=3)
    ids = [s["id"] for s in got["similarSongs2"]["song"]]
    assert ids == ["m2", "g1", "m1"] or ids[:2] == ["m2", "g1"]
    assert not set(ids) & set(seeds_seen)                      # seeds excluded
    got = env.ok("getSimilarSongs.view", id=sx.album_id("Queen", "A Night at the Opera"))
    assert set(seeds_seen[-3:]) == {"q1", "q2", "q3"}
    assert {s["id"] for s in got["similarSongs"]["song"]} >= {"g1", "m1"}
    seeds_seen.clear()
    single = env.ok("getSimilarSongs.view", id="q1", count=5)["similarSongs"]["song"]
    assert seeds_seen == ["q1"] and "m2" in {s["id"] for s in single}
    assert env.fail("getSimilarSongs2.view", id="ar:0000000000000000")["code"] == 70
    assert env.fail("getSimilarSongs.view", id="al:0000000000000000")["code"] == 70


# ── r1-sub-8: scrobble batches ────────────────────────────────────────────────

def test_scrobble_records_every_id_with_its_time(env):
    t1, t2 = 1_700_000_100_000, 1_700_000_200_000
    env.ok("scrobble.view", id=["q1", "nope", "q2"], time=[t1, 5, t2])
    assert env.store._play_stats["q1"] == {"count": 1, "last_played": t1 // 1000}
    assert env.store._play_stats["q2"] == {"count": 1, "last_played": t2 // 1000}
    assert "nope" not in env.store._play_stats
    before = int(time.time())
    env.ok("scrobble.view", id="q3")
    assert env.store._play_stats["q3"]["last_played"] >= before
    assert env.fail("scrobble.view")["code"] == 10


# ── r1-sub-14: album / artist ratings, lastFmUrl, albumInfo images ──────────

def test_album_and_artist_ratings_are_per_user(env):
    al = sx.album_id("Queen", "Innuendo")
    ar = sx.artist_id("Queen")
    env.ok("setRating.view", id=al, rating=4)
    env.ok("setRating.view", id=ar, rating=5)
    assert env.ok("getAlbum.view", id=al)["album"]["userRating"] == 4
    art = env.ok("getArtist.view", id=ar)["artist"]
    assert art["userRating"] == 5
    assert next(a for a in art["album"] if a["id"] == al)["userRating"] == 4
    idx = [a for i in env.ok("getArtists.view")["artists"]["index"] for a in i["artist"]]
    assert next(a for a in idx if a["id"] == ar)["userRating"] == 5
    other = types.SimpleNamespace(id="u9", username="z", role="admin", enabled=True)
    env.user = other
    assert "userRating" not in env.ok("getAlbum.view", id=al)["album"]
    env.user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    env.ok("setRating.view", id=al, rating=0)
    assert "userRating" not in env.ok("getAlbum.view", id=al)["album"]
    assert env.fail("setRating.view", id="al:0000000000000000", rating=2)["code"] == 70
    assert env.fail("setRating.view", id="ar:0000000000000000", rating=2)["code"] == 70


def test_last_fm_url_in_json_and_xml(env, monkeypatch):
    from soniqboom.core import artistinfo

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(artistinfo, "get_artist_info", _none)
    info = env.ok("getArtistInfo2.view", id=sx.artist_id("Mixed Band"))["artistInfo2"]
    assert info["lastFmUrl"] == "https://www.last.fm/music/Mixed%20Band"
    root = ET.fromstring(env.get("getArtistInfo2.view", id=sx.artist_id("Mixed Band"),
                                 f="xml").content)
    assert root.find("{*}artistInfo2").find("{*}lastFmUrl").text.endswith("Mixed%20Band")


def test_album_info_image_urls_load_without_credentials(env, users, monkeypatch):
    env.user = users.get_by_username("bob")
    ai = env.ok("getAlbumInfo.view", id=sx.album_id("Queen", "A Night at the Opera"))["albumInfo"]
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(ai["largeImageUrl"]).query)
    assert q["id"] == ["q3"] and q["size"] == ["1200"]
    _real_auth(monkeypatch)                          # no credentials on the next call
    r = env.client.get("/rest/getCoverArt.view", params={k: v[0] for k, v in q.items()})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    q["id"] = ["q2"]                                 # token bound to its own id only
    r = env.client.get("/rest/getCoverArt.view",
                       params={"id": "q2", "tok": q["tok"][0], "f": "json"})
    assert r.json()["subsonic-response"]["error"]["code"] == 40


# ── r1-sub-15 / r1-sub-16: stream errors, getTopSongs id ────────────────────

@pytest.mark.parametrize("method", ["stream.view", "download.view"])
def test_stream_unknown_id_is_an_error_document(env, method):
    assert env.fail(method, id="doesnotexist")["code"] == 70
    root = ET.fromstring(env.get(method, id="doesnotexist", f="xml").content)
    assert root.find("{*}error").get("code") == "70"


def test_top_songs_by_artist_id(env):
    env.store.record_play("q2")
    by_name = [s["id"] for s in env.ok("getTopSongs.view", artist="queen")["topSongs"]["song"]]
    by_id = [s["id"] for s in env.ok("getTopSongs.view", id=sx.artist_id("Queen"))["topSongs"]["song"]]
    assert by_id == by_name and by_id[0] == "q2"
    assert [s["id"] for s in env.ok("getTopSongs.view", id=sx.artist_id("Madonna"),
                                    artist="queen")["topSongs"]["song"]] == ["c1"]
    assert env.fail("getTopSongs.view", id="ar:0000000000000000")["code"] == 70
    assert env.fail("getTopSongs.view")["code"] == 10


# ── r1-sub-18: HEAD on media routes ──────────────────────────────────────────

def test_head_stream_never_renders(env, monkeypatch, tmp_path):
    from soniqboom.api import stream as stream_mod

    async def _boom(**kw):
        raise AssertionError("HEAD must not reach the render / transcode path")

    async def _gt(tid):
        tr = env.store.get_track(tid)
        return types.SimpleNamespace(**tr) if tr else None

    monkeypatch.setattr(stream_mod, "stream_track", _boom)
    monkeypatch.setattr(stream_mod, "get_track", _gt)
    f = tmp_path / "song.mp3"
    f.write_bytes(b"\xff\xfb" + b"\0" * 4094)
    env.store.upsert_track({**_tr(store=env.store, tid="loc", title="Local", artist="L",
                                  added=400), "path": str(f), "format": "MP3"})
    r = env.client.head("/rest/stream.view", params={"id": "loc"})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == "4096" and r.headers["content-type"] == "audio/mpeg"
    r = env.client.head("/rest/stream.view", params={"id": "h1"})         # SID: rendered
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert "content-length" not in r.headers
    assert subsonic._NOW_PLAYING == {} or all(v["track"] not in ("loc", "h1")
                                              for v in subsonic._NOW_PLAYING.values())
    # Non-media methods answer HEAD with headers only (no handler run).
    r = env.client.head("/rest/ping.view", params={"f": "json"})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-type"].startswith("application/json")
    assert env.client.head("/rest/getBogus.view").status_code == 404
    assert env.client.head("/rest/getAvatar.view", params={"username": "x"}).status_code == 200


def test_head_radio_opens_no_upstream(env, radio_favs, monkeypatch):
    from soniqboom.api import stations

    async def _boom(**kw):
        raise AssertionError("HEAD must not open the relay")

    monkeypatch.setattr(stations, "relay", _boom)
    r = env.client.head("/rest/radioStream.view", params={"id": "scene:a"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"


def test_radio_homepage_is_sanitised_and_relay_is_unbuffered(env, radio_favs, monkeypatch):
    favs = radio_favs.get_favorites()
    favs[1]["homepage"] = "javascript:alert(1)"
    radio_favs._write_json(radio_favs._FAVS_FILE, favs)
    rows = env.ok("getInternetRadioStations.view")["internetRadioStations"]["internetRadioStation"]
    assert rows[0]["homePageUrl"] == "https://a.example/" and "homePageUrl" not in rows[1]
    assert radio_favs._trim_station({"homepage": "javascript:x", "url": "u"})["homepage"] == ""
    # The relay response tells nginx not to buffer.
    from soniqboom.api import stations

    class _Hub:
        media_type = "audio/mpeg"
        headers = {"icy-name": "A"}

        async def stream(self):
            yield b"x"

    async def _goc(key, build):
        return _Hub()

    async def _ok_url(url):
        return None

    monkeypatch.setattr(stations._hub_registry, "get_or_create", _goc)
    monkeypatch.setattr(stations, "_assert_public_url", _ok_url)
    resp = asyncio.run(stations.relay(sid="scene:a", v=2))
    assert resp.headers["x-accel-buffering"] == "no" and resp.headers["icy-name"] == "A"


# ── r1-sub-28: download = the original file ──────────────────────────────────

def test_download_serves_the_source_bytes(env, monkeypatch, tmp_path):
    from fastapi.responses import Response
    from soniqboom.api import stream as stream_mod
    from soniqboom.core import data as data_mod
    src = tmp_path / "Commando.sid"
    payload = b"PSID" + bytes(range(256)) * 8
    src.write_bytes(payload)
    t = {**_tr(store=env.store, tid="dl", title="Commando", artist="Rob Hubbard", added=500),
         "path": str(src), "format": "SID"}
    env.store.upsert_track(t)

    async def _gt(tid):
        return types.SimpleNamespace(path=str(src)) if tid == "dl" else None

    monkeypatch.setattr(data_mod, "get_track", _gt)
    r = env.client.get("/rest/download.view", params={"id": "dl"})
    assert r.status_code == 200 and r.content == payload
    assert r.headers["content-type"] == "audio/prs.sid"
    assert "Commando.sid" in r.headers["content-disposition"]
    assert "x-rendered" not in r.headers
    seen = {}

    async def _fake(**kw):
        seen.update(kw)
        return Response(b"mp3", media_type="audio/mpeg")

    monkeypatch.setattr(stream_mod, "stream_track", _fake)
    r = env.client.get("/rest/download.view", params={"id": "dl", "format": "mp3"})
    assert r.content == b"mp3" and seen["target_format"] == "mp3"
    h = env.client.head("/rest/download.view", params={"id": "dl"})
    assert h.headers["content-length"] == str(len(payload)) and h.content == b""


# ── r1-sub-30: OpenSubsonic transcoding extension ────────────────────────────

def _decide(env, media_id, info, **params):
    r = env.client.post("/rest/getTranscodeDecision.view",
                        params={"mediaId": media_id, "mediaType": "song", "f": "json", **params},
                        json=info)
    body = r.json()["subsonic-response"]
    assert body["status"] == "ok", body
    return body["transcodeDecision"]


def test_transcode_decision_spec_form(env, monkeypatch):
    from fastapi.responses import Response
    from soniqboom.api import stream as stream_mod
    mp3_ok = {"directPlayProfiles": [{"containers": ["mp3"], "audioCodecs": ["mp3"],
                                      "protocols": ["http"]}],
              "transcodingProfiles": [{"container": "mp3", "audioCodec": "mp3",
                                       "protocol": "http"}],
              "maxAudioBitrate": 320000}
    d = _decide(env, "q1", mp3_ok)
    assert d["canDirectPlay"] is True and "transcodeParams" not in d
    assert d["sourceStream"]["container"] == "mp3"
    flac_only = {"directPlayProfiles": [{"containers": ["flac"], "audioCodecs": ["flac"]}],
                 "transcodingProfiles": [{"container": "ogg", "audioCodec": "vorbis"},
                                         {"container": "mp3", "audioCodec": "mp3"}],
                 "maxTranscodingAudioBitrate": 192000}
    d = _decide(env, "q1", flac_only)
    assert d["canDirectPlay"] is False and d["canTranscode"] is True
    assert d["transcodeReason"] == ["audio codec not supported"]
    assert d["transcodeStream"]["container"] == "ogg" and d["transcodeStream"]["codec"] == "vorbis"
    assert d["transcodeStream"]["audioBitrate"] == 192000
    tp = d["transcodeParams"]
    # A rendered source is never direct: the client's first producible
    # profile (ogg here), else WAV.
    sid = _decide(env, "h1", flac_only)
    assert sid["transcodeStream"]["container"] == "ogg" and "container not supported" in \
        sid["transcodeReason"]
    sid = _decide(env, "h1", {"directPlayProfiles": [{"audioCodecs": ["mp3"]}]})
    assert sid["transcodeStream"]["container"] == "wav" and sid["canTranscode"] is True
    # A bitrate-capped mp3 source is re-encoded in its own codec, at the cap.
    capped = _decide(env, "q1", {"directPlayProfiles": [{"audioCodecs": ["mp3"]}],
                                 "transcodingProfiles": [{"audioCodec": "flac"},
                                                         {"audioCodec": "mp3"}],
                                 "maxAudioBitrate": 128000})
    env.store.update_track_fields("q1", {"bitrate": 320000})
    capped = _decide(env, "q1", {"directPlayProfiles": [{"audioCodecs": ["mp3"]}],
                                 "transcodingProfiles": [{"audioCodec": "flac"},
                                                         {"audioCodec": "mp3"}],
                                 "maxAudioBitrate": 128000})
    assert capped["transcodeReason"] == ["audio bitrate not supported"]
    assert capped["transcodeStream"]["codec"] == "mp3"
    assert capped["transcodeStream"]["audioBitrate"] == 128000
    seen = {}

    async def _fake(**kw):
        seen.update(kw)
        return Response(b"ogg", media_type="audio/ogg")

    monkeypatch.setattr(stream_mod, "stream_track", _fake)
    r = env.client.get("/rest/getTranscodeStream.view",
                       params={"mediaId": "q1", "mediaType": "song", "transcodeParams": tp,
                               "offset": 12})
    assert r.status_code == 200 and r.content == b"ogg"
    assert (seen["target_format"], seen["max_bitrate_kbps"], seen["seek"]) == ("ogg", 192, 12.0)
    bad = env.client.get("/rest/getTranscodeStream.view",
                         params={"mediaId": "q2", "mediaType": "song", "transcodeParams": tp})
    assert bad.status_code == 400 and bad.headers["content-type"].startswith("text/plain")
    gone = env.client.get("/rest/getTranscodeStream.view",
                          params={"mediaId": "q1", "mediaType": "song", "transcodeParams": "x.y.z"})
    assert gone.status_code == 410
    # Legacy form still works (id + clientCodecs, token=) and errors keep ``f``.
    legacy = env.ok("getTranscodeDecision.view", id="h1", clientCodecs="mp3,flac")
    assert legacy["transcodeDecision"]["transcoded"] is True
    tok = legacy["transcodeDecision"]["token"]
    r = env.client.get("/rest/getTranscodeStream.view", params={"token": tok})
    assert r.status_code == 200
    err = env.fail("getTranscodeStream.view", token="x.y.z")
    assert err["code"] == 70
    assert env.fail("getTranscodeDecision.view", mediaId="nope")["code"] == 70
    assert env.fail("getTranscodeDecision.view", mediaId="q1", mediaType="podcast")["code"] == 70


def test_transcode_stream_stale_file_is_code_0(env, monkeypatch, tmp_path):
    f = tmp_path / "a.dsf"
    f.write_bytes(b"DSD " + b"\0" * 100)
    env.store.upsert_track({**_tr(store=env.store, tid="dsd", title="D", artist="D",
                                  added=600), "path": str(f), "format": "DSD"})
    tok = env.ok("getTranscodeDecision.view", id="dsd")["transcodeDecision"]["token"]
    f.write_bytes(b"DSD " + b"\1" * 200)                  # the file changed
    err = env.fail("getTranscodeStream.view", token=tok)
    assert err["code"] == 0 and "changed" in err["message"]


# ── r1-sub-32: cover art caching ─────────────────────────────────────────────

def test_cover_art_cache_headers_etag_and_single_resize(env, monkeypatch):
    import soniqboom.api.art as art_mod
    from soniqboom.core import art_cache
    from soniqboom.core import metadata
    resizes, stored = [], []

    def _resize(data, size, quality=85):
        resizes.append(size)
        return b"\xff\xd8thumb%d" % size

    async def _store(tid, data, bucket):
        stored.append((tid, bucket))

    monkeypatch.setattr(metadata, "resize_cover", _resize)
    monkeypatch.setattr(art_cache, "store_art", _store)
    r = env.client.get("/rest/getCoverArt.view", params={"id": "q2", "size": 200})
    assert r.content == b"\xff\xd8thumb200" and resizes == [200]
    assert r.headers["cache-control"] == "private, max-age=86400"
    assert stored == [("q2", "sm")]
    resizes.clear()
    full = env.client.get("/rest/getCoverArt.view", params={"id": "q2", "size": 1000})
    assert full.content.startswith(b"\xff\xd8JPEG") and resizes == []
    monkeypatch.setattr(art_mod, "_art_cached_mtime", lambda tid, size: 1234.0)
    r = env.client.get("/rest/getCoverArt.view", params={"id": "q2", "size": 1000})
    etag = r.headers["etag"]
    r304 = env.client.get("/rest/getCoverArt.view", params={"id": "q2", "size": 1000},
                          headers={"If-None-Match": etag})
    assert r304.status_code == 304 and r304.content == b""


# ── r1-sub-10: CORS for browser Subsonic clients ─────────────────────────────

def test_rest_cors_is_open_but_never_credentialed(env):
    from fastapi.middleware.cors import CORSMiddleware
    from soniqboom.main import _SubsonicCORSMiddleware
    app = FastAPI()
    app.include_router(subsonic.router)

    @app.get("/api/health")
    async def _health():
        return {"ok": True}

    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(_SubsonicCORSMiddleware)
    c = TestClient(app)
    r = c.get("/rest/ping.view", params={"f": "json"}, headers={"Origin": "https://x.example"})
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers
    assert "Content-Range" in r.headers["access-control-expose-headers"]
    pre = c.options("/rest/stream.view", headers={"Origin": "https://x.example",
                                                  "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    loc = c.get("/rest/ping.view", headers={"Origin": "http://localhost:8080"})
    assert loc.headers.get_list("access-control-allow-origin") == ["*"]
    assert "access-control-allow-credentials" not in loc.headers
    api = c.get("/api/health", headers={"Origin": "https://x.example"})
    assert "access-control-allow-origin" not in api.headers


# ── r1-sub-21: no online lyrics lookups for chip / tracker formats ───────────

def test_lyrics_skip_online_lookup_for_retro(env, monkeypatch):
    from soniqboom.api import tracks as tracks_mod
    calls = []

    async def _impl(tid):
        calls.append(tid)
        return {"lyrics": None}

    monkeypatch.setattr(tracks_mod, "get_lyrics", _impl)
    assert env.ok("getLyricsBySongId.view", id="h1")["lyricsList"] == {"structuredLyrics": []}
    assert calls == []
    env.ok("getLyricsBySongId.view", id="q1")
    assert calls == ["q1"]


def test_first_build_and_flag_flip_run_off_the_event_loop(env, monkeypatch):
    def _no_inline(*a, **k):
        raise AssertionError("a request handler must never build the catalogue inline")

    monkeypatch.setitem(subsonic._ALBUM_LIST_CACHE, "cat", None)
    monkeypatch.setattr(sx, "build_catalogue", _no_inline)
    assert env.ok("getArtists.view")["artists"]["index"]           # first build: chunked
    env.folder_albums(True)                                          # flag flip: chunked too
    art = env.ok("getArtist.view", id=sx.artist_id("Rob Hubbard"))["artist"]
    assert [a["id"] for a in art["album"]] == ["fa:" + env.store.store_hash_lookup(HUBBARD_DIR)]


def test_advertised_transcoded_format_matches_the_stream_handler():
    """The song Child's transcoded* fields and api/stream.delivered_format
    (what stream_track really sends, no ``format``) must never drift apart."""
    from soniqboom.api.stream import delivered_format
    cases = [("/m/a.mp3", "MP3"), ("/m/a.flac", "FLAC"), ("/m/a.ogg", "Ogg Vorbis"),
             ("/m/a.opus", "Opus"), ("/m/a.wav", "WAV"), ("/m/a.m4a", "AAC"),
             ("/m/a.m4a", "ALAC"), ("/m/a.aiff", "AIFF"), ("/m/a.dsf", "DSD"),
             ("/m/a.mpc", "Musepack"), ("/m/a.wv", "WavPack"), ("/m/a.ape", "APE"),
             ("/m/a.sid", "SID"), ("/m/a.mod", "MOD"), ("/m/a.xm", "XM"),
             ("/m/a.mid", "MIDI"), ("/m/a.nsf", "NSF"), ("/m/a.spc", "SPC"),
             ("/m/a.vgz", "VGM"), ("/m/a.ahx", "AHX"), ("/m/a.hvl", "HVL"),
             ("/m/a.ym", "YM"), ("/m/a.sndh", "SNDH"), ("/m/p.zip::x/legendcrack.fc", "FC"),
             ("/m/mdat.acieed1", "TFMX"), ("/m/a.psf", "PSF"), ("/m/a.imf", "IMF")]
    for path, label in cases:
        mine = subsonic._delivery({"path": path, "format": label})[2:]
        d = delivered_format(path, label, None)
        assert mine == ((None, None) if d is None else d), (path, label, mine, d)


# ── r1-sub-12: timeOffset end to end (real ffmpeg) ──────────────────────────

def _ffprobe_seconds(path) -> float:
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg missing")
def test_time_offset_starts_a_transcode_later(env, monkeypatch, tmp_path):
    import subprocess
    from soniqboom.api import stream as stream_mod
    from soniqboom.config import settings
    src = tmp_path / "src.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=10", "-ac", "2", str(src)], check=True)
    rec = {**_tr(store=env.store, tid="tx1", title="Tone", artist="T", added=700),
           "path": str(src), "format": "FLAC", "duration": 10.0}
    env.store.upsert_track(rec)
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))

    async def _gt(tid):
        tr = env.store.get_track(tid)
        return types.SimpleNamespace(**tr) if tr else None

    monkeypatch.setattr(stream_mod, "get_track", _gt)
    full = env.client.get("/rest/stream.view", params={"id": "tx1", "format": "mp3"})
    late = env.client.get("/rest/stream.view", params={"id": "tx1", "format": "mp3",
                                                       "timeOffset": 4})
    zero = env.client.get("/rest/stream.view", params={"id": "tx1", "format": "mp3",
                                                       "timeOffset": 0})
    assert full.headers["content-type"] == late.headers["content-type"] == "audio/mpeg"
    (tmp_path / "full.mp3").write_bytes(full.content)
    (tmp_path / "late.mp3").write_bytes(late.content)
    d_full = _ffprobe_seconds(tmp_path / "full.mp3")
    d_late = _ffprobe_seconds(tmp_path / "late.mp3")
    assert abs(d_full - 10.0) < 0.3 and abs(d_late - 6.0) < 0.3, (d_full, d_late)
    assert zero.content == full.content                      # 0 = no offset
    # A native file with no transcode: timeOffset is ignored (clients seek by Range).
    plain = env.client.get("/rest/stream.view", params={"id": "tx1", "timeOffset": 4})
    assert plain.content == src.read_bytes()
