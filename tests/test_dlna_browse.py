# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DLNA Media Server browsing (``api/dlna_upnp``): the Genres, Folders and
Playlists containers, covers in listings, and how items are offered — a file
in a codec renderers play as it is, anything else (rendered formats) as FLAC
then MP3, through links the byte server (``api/cast_stream``) answers with
the stream endpoint and the item's own tune."""
from __future__ import annotations

import base64
import html
import json
import re
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from soniqboom.core.data import path_hash
from soniqboom.core.store import TrackStore

NS = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
      "dc": "http://purl.org/dc/elements/1.1/",
      "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
ROOT = "/music/lib"


def _row(tid, rel, fmt, **kw):
    t = {"id": tid, "path": f"{ROOT}/{rel}", "title": tid, "artist": "A",
         "album": "", "format": fmt, "duration": 120.0,
         "scan_root_hash": path_hash(ROOT),
         "dir_hash": path_hash(f"{ROOT}/{rel}".rsplit("/", 1)[0])}
    t.update(kw)
    return t


@pytest.fixture
def store(monkeypatch, tmp_data_dir):
    from soniqboom.config import settings
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(settings, "folder_aliases", {ROOT: "My music"})
    s.upsert_scan_dir(ROOT)
    s.upsert_tracks_batch([
        _row("sid1", "C64/a.sid", "SID", genre=["Chiptune"]),
        _row("mp31", "Pop/b.mp3", "MP3", genre=["Pop"], album="Hits"),
        _row("flac1", "Pop/Live/c.flac", "FLAC", genre=["Pop"], album="Hits"),
        _row("mod1", "Pop/d.mod", "ProTracker", duration=0),
        _row("zip1", "Pop/pack.zip::inner/e.xm", "FastTracker 2"),
        _row("zip2", "Pop/pack.zip::f.mod", "ProTracker"),
    ])
    return s


@pytest.fixture
def browse(store):
    from soniqboom.api import dlna_upnp
    app = FastAPI()
    app.include_router(dlna_upnp.router)
    c = TestClient(app)

    def call(obj, flag="BrowseDirectChildren", start=0, count=0):
        env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
               '<s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
               f'<ObjectID>{html.escape(obj)}</ObjectID><BrowseFlag>{flag}</BrowseFlag>'
               f'<Filter>*</Filter><StartingIndex>{start}</StartingIndex>'
               f'<RequestedCount>{count}</RequestedCount><SortCriteria></SortCriteria>'
               '</u:Browse></s:Body></s:Envelope>')
        r = c.post("/dlna/cds/control", content=env,
                   headers={"Content-Type": 'text/xml; charset="utf-8"',
                            "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'})
        assert r.status_code == 200, r.text
        res = html.unescape(re.search(r"<Result>(.*?)</Result>", r.text, re.S).group(1))
        rows = []
        for el in ET.fromstring(res):
            rows.append({
                "kind": el.tag.split("}")[1], "id": el.get("id"), "parent": el.get("parentID"),
                "count": el.get("childCount"), "title": el.findtext("dc:title", namespaces=NS),
                "class": el.findtext("upnp:class", namespaces=NS),
                "art": el.findtext("upnp:albumArtURI", namespaces=NS),
                "res": [(x.get("protocolInfo").split(":")[2], x.text, x.get("duration"))
                        for x in el.findall("d:res", NS)]})
        total = int(re.search(r"<TotalMatches>(\d+)</TotalMatches>", r.text).group(1))
        returned = int(re.search(r"<NumberReturned>(\d+)</NumberReturned>", r.text).group(1))
        assert returned == len(rows)
        return rows, total
    return call


def _claims(url):
    token = url.split("/cast/", 1)[1].split("/", 1)[0]
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_music_lists_the_six_containers_with_counts(browse, store):
    store.create_playlist("p1", "Shared", ["mp31"], owner_user_id="u1")
    store.update_playlist("p1", {"shared": True})
    store.create_playlist("p2", "Mine", ["sid1"], owner_user_id="u1")
    rows, total = browse("music")
    assert total == 6
    assert [(r["id"], r["count"]) for r in rows] == [
        ("music/all", "6"), ("music/albums", "1"), ("music/artists", "0"),
        ("music/genres", "2"), ("music/folders", "1"), ("music/playlists", "1")]


def test_rendered_tracks_offer_flac_then_mp3_and_playable_files_themselves(browse):
    items = {r["id"]: r for r in browse("music/all")[0]}
    sid = items["sid1"]["res"]
    assert [m for m, _, _ in sid] == ["audio/flac", "audio/mpeg"]
    flac, mp3 = _claims(sid[0][1]), _claims(sid[1][1])
    assert (flac.get("c"), flac.get("o"), "sn" in flac, "uid" in flac) == ("flac", "dlna", False, False)
    assert (mp3.get("c"), mp3.get("br"), mp3.get("o")) == ("mp3", 320, "dlna")
    assert sid[0][1].endswith(".flac") and sid[1][1].endswith(".mp3")
    assert sid[0][2] == "0:02:00.000"
    native = items["mp31"]["res"]
    assert [m for m, _, _ in native] == ["audio/mpeg"]
    assert "c" not in _claims(native[0][1]) and _claims(native[0][1])["o"] == "dlna"
    assert items["mod1"]["res"][0][2] is None            # no length known: no duration


def test_folders_list_subfolders_then_tracks_with_archive_members(browse):
    roots, total = browse("music/folders")
    assert total == 1 and roots[0]["title"] == "My music" and roots[0]["count"] == "2"
    assert roots[0]["class"] == "object.container.storageFolder"
    root = roots[0]["id"]
    rows, total = browse(root)
    assert [(r["kind"], r["title"], r["count"]) for r in rows] == [
        ("container", "C64", "1"), ("container", "Pop", "5")]
    pop = rows[1]["id"]
    rows, total = browse(pop)
    # the subfolder first; the archive's tunes (``inner/`` too) in the
    # folder that holds the archive, in path order
    assert [(r["kind"], r["id"] if r["kind"] == "item" else r["title"]) for r in rows] == [
        ("container", "Live"), ("item", "mp31"), ("item", "mod1"),
        ("item", "zip2"), ("item", "zip1")]
    assert all(r["parent"] == pop for r in rows)
    # paging runs on across the subfolders into the tracks
    assert [r["title"] for r in browse(pop, start=0, count=2)[0]] == ["Live", "mp31"]
    assert [r["id"] for r in browse(pop, start=3, count=5)[0]] == ["zip2", "zip1"]
    live = rows[0]["id"]
    assert [r["id"] for r in browse(live)[0]] == ["flac1"]
    meta = browse(live, flag="BrowseMetadata")[0][0]
    assert (meta["title"], meta["parent"]) == ("Live", pop)
    assert browse(root, flag="BrowseMetadata")[0][0]["parent"] == "music/folders"
    # an ObjectID naming no library folder lists nothing
    assert browse("fo:0123456789abcdef") == ([], 0)
    assert browse("fo:" + root[3:] + ":" + base64.urlsafe_b64encode(b"Nope").decode()) == ([], 0)


def test_genres_list_their_tracks(browse):
    rows, total = browse("music/genres")
    assert [(r["title"], r["count"]) for r in rows] == [("Chiptune", "1"), ("Pop", "2")]
    assert rows[0]["class"] == "object.container.genre.musicGenre"
    pop, total = browse(rows[1]["id"])
    assert total == 2 and sorted(r["id"] for r in pop) == ["flac1", "mp31"]
    assert browse(rows[1]["id"], flag="BrowseMetadata")[0][0]["parent"] == "music/genres"


def test_only_shared_playlists_are_listed_and_entries_keep_their_tunes(browse, store):
    store.create_playlist("p1", "Shared", ["mp31", {"id": "sid1", "subsong": 2},
                                           {"id": "sid1", "subsong": 0}, "sid1", "mp31"],
                          owner_user_id="u1")
    store.update_playlist("p1", {"shared": True})
    store.create_playlist("p2", "Private", ["sid1"], owner_user_id="u1")
    rows, total = browse("music/playlists")
    assert [(r["id"], r["title"], r["count"], r["class"]) for r in rows] == [
        ("pl:p1", "Shared", "5", "object.container.playlistContainer")]
    entries, total = browse("pl:p1")
    assert total == 5
    assert [r["id"] for r in entries] == [f"pi:p1:{i}" for i in range(5)]
    assert [r["title"] for r in entries] == ["mp31", "sid1 (tune 3)", "sid1 (tune 1)", "sid1", "mp31"]
    sn = [_claims(r["res"][0][1]).get("sn") for r in entries]
    assert sn == [None, 2, 0, None, None]
    assert browse("pl:p1", start=1, count=2)[0][0]["id"] == "pi:p1:1"
    meta = browse("pi:p1:1", flag="BrowseMetadata")[0][0]
    assert (meta["title"], meta["parent"]) == ("sid1 (tune 3)", "pl:p1")
    # an unshared playlist is not reachable by its id either
    assert browse("pl:p2") == ([], 0)
    assert browse("pl:p2", flag="BrowseMetadata") == ([], 0)
    assert browse("pi:p2:0", flag="BrowseMetadata") == ([], 0)


def test_covers_are_offered_only_when_one_is_known(browse, store, monkeypatch):
    from soniqboom.api.art import _folder_art_cache_key
    from soniqboom.core import art_cache, catalogue
    monkeypatch.setattr(catalogue, "is_catalogue_path",
                        lambda p: str(p).endswith((".sid", ".xm")))
    cached = {("flac1", "lg"), (_folder_art_cache_key(path_hash(f"{ROOT}/Pop")), "full"),
              ("sid1", "lg")}
    monkeypatch.setattr(art_cache, "art_exists", lambda tid, size: (tid, size) in cached)
    store.update_track_fields("mp31", {"cover_art": "/api/art/mp31"})   # embedded, scanned
    store.update_track_fields("zip1", {"cover_art": "/api/art/zip1"})
    store.mark_art_absent("zip2")
    items = {r["id"]: r for r in browse("music/all")[0]}
    assert items["mp31"]["art"] == "http://testserver/dlna/art/mp31.jpg"
    assert items["flac1"]["art"] == "http://testserver/dlna/art/flac1.jpg"   # its own, cached
    assert items["mod1"]["art"] == "http://testserver/dlna/art/mod1.jpg"     # its folder's
    assert items["zip2"]["art"] is None                  # known to have none
    assert items["sid1"]["art"] is None                  # collection tune: never from a cache peek
    assert items["zip1"]["art"] == "http://testserver/dlna/art/zip1.jpg"     # (cover known)
    album = browse("music/albums")[0][0]
    assert album["class"] == "object.container.album.musicAlbum"
    assert album["art"] and album["art"].startswith("http://testserver/dlna/art/")


@pytest.mark.parametrize("path, expect_resolve", [("/music/lib/Pop/b.mp3", True),
                                                  ("/music/lib/C64/a.sid", False)])
def test_cover_route_serves_a_shown_tracks_cover(store, monkeypatch, path, expect_resolve):
    from soniqboom.api import art, dlna_upnp
    from soniqboom.core import catalogue, users
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: str(p).endswith(".sid"))
    seen = []

    async def thumb(tid, bucket, *, resolve=True):
        seen.append((tid, bucket, resolve))
        return b"\xff\xd8jpeg" if tid != "flac1" else None
    monkeypatch.setattr(art, "cover_thumb", thumb)
    c = TestClient(_app(dlna_upnp))
    tid = "mp31" if path.endswith(".mp3") else "sid1"
    r = c.get(f"/dlna/art/{tid}.jpg")
    assert (r.status_code, r.headers["content-type"], r.content) == (200, "image/jpeg", b"\xff\xd8jpeg")
    assert seen == [(tid, "lg", expect_resolve)]
    assert c.get("/dlna/art/flac1.jpg").status_code == 404        # no cover
    assert c.get("/dlna/art/nope.jpg").status_code == 404         # no such track
    monkeypatch.setattr(users, "accounts_locked", lambda: True)
    assert c.get(f"/dlna/art/{tid}.jpg").status_code == 503


def _app(mod):
    app = FastAPI()
    app.include_router(mod.router)
    return app


# ── the links ──────────────────────────────────────────────────────────────

def test_token_origin_carries_the_tune_as_given():
    from soniqboom.core import cast_tokens

    def claims(**kw):
        return cast_tokens.verify_token(cast_tokens.issue_token(track_id="t", **kw))
    assert claims(origin="dlna")["o"] == "dlna" and "sn" not in claims(origin="dlna")
    assert claims(origin="dlna", subsong=None).get("sn") is None
    assert claims(origin="dlna", subsong=0)["sn"] == 0
    assert claims(origin="dlna", subsong=3)["sn"] == 3
    # links without an origin: unchanged (no ``o``; tune 0 not written)
    assert "o" not in claims(subsong=3) and claims(subsong=3)["sn"] == 3
    assert "sn" not in claims(subsong=0)


def test_dlna_links_take_the_stream_endpoint_with_their_tune():
    from soniqboom.api import cast_stream
    track = type("T", (), {"path": "/m/a.sid", "format": "SID"})()
    dlna = {"o": "dlna", "c": "flac"}
    assert cast_stream._cast_delivery(dlna, track)[5] is True
    assert cast_stream._cast_delivery({"c": "flac"}, track)[5] is False   # the sender's path
    assert cast_stream._stream_subsong(dlna) is None                      # the default tune
    assert cast_stream._stream_subsong({**dlna, "sn": 0}) == 0
    assert cast_stream._stream_subsong({**dlna, "sn": 4}) == 4
    assert cast_stream._stream_subsong({"sn": 4}) == 0                    # unchanged


@pytest.mark.parametrize("claims, length, seekable", [
    ({"o": "dlna"}, None, False), ({"o": "dlna"}, "100", True), ({}, None, True)])
def test_a_dlna_answer_without_length_offers_no_seeking(claims, length, seekable):
    from soniqboom.api import cast_stream
    r = Response(content=b"")
    del r.headers["content-length"]
    if length:
        r.headers["content-length"] = length
    cast_stream._add_dlna_headers(r, "flac", claims)
    op = re.search(r"DLNA\.ORG_OP=(\d\d)", r.headers["contentFeatures.dlna.org"]).group(1)
    assert op == ("01" if seekable else "00")
    assert (r.headers.get("accept-ranges") == "none") is (not seekable)


# ── review round (2026-10-28) ──────────────────────────────────────────────

def test_playlists_kept_aside_or_ownerless_are_not_offered(browse, store):
    for pid, owner in (("p1", "u1"), ("p2", "import:old"), ("p3", "")):
        store.create_playlist(pid, pid, ["mp31"], owner_user_id=owner)
        store.update_playlist(pid, {"shared": True})
    assert [r["id"] for r in browse("music/playlists")[0]] == ["pl:p1"]
    assert browse("pl:p2") == ([], 0) and browse("pl:p3", flag="BrowseMetadata") == ([], 0)


def test_a_playlist_entry_whose_track_is_gone_is_not_counted_or_repeated(browse, store):
    store.create_playlist("p1", "P", ["mp31", "gone", "flac1", "sid1"], owner_user_id="u1")
    store.update_playlist("p1", {"shared": True})
    assert store.get_playlist("p1")["track_ids"][1] == "gone"
    assert browse("music/playlists")[0][0]["count"] == "3"
    seen, start = [], 0
    while True:                                   # a controller paging by NumberReturned
        page, total = browse("pl:p1", start=start, count=2)
        if not page:
            break
        seen += [r["id"] for r in page]
        start += len(page)
    assert total == 3 and seen == ["pi:p1:0", "pi:p1:2", "pi:p1:3"]


def test_an_album_lists_in_disc_and_track_order(browse, store):
    store.upsert_tracks_batch([
        _row(f"x{n}", f"Alb/{n}.mp3", "MP3", album="Disc Album", title=f"T{n}",
             disc_number=d, track_number=t)
        for n, (d, t) in enumerate([(2, 1), (1, 3), (1, 1), (1, 2)])])
    al = [r for r in browse("music/albums")[0] if r["title"] == "Disc Album"][0]
    rows, total = browse(al["id"])
    assert total == 4 and [r["title"] for r in rows] == ["T2", "T3", "T1", "T0"]
    assert [r["title"] for r in browse(al["id"], start=1, count=2)[0]] == ["T3", "T1"]


def test_metadata_carries_the_real_child_count(browse, store):
    assert browse("0", flag="BrowseMetadata")[0][0]["count"] == "1"
    assert browse("music", flag="BrowseMetadata")[0][0]["count"] == "6"
    assert browse("music/all", flag="BrowseMetadata")[0][0]["count"] == "6"
    assert browse("music/genres", flag="BrowseMetadata")[0][0]["count"] == "2"
    assert browse("0")[0][0]["count"] == "6"                 # Music's children
    pop = [r for r in browse("music/genres")[0] if r["title"] == "Pop"][0]["id"]
    assert browse(pop, flag="BrowseMetadata")[0][0]["count"] == "2"
    root = browse("music/folders")[0][0]["id"]
    assert browse(root, flag="BrowseMetadata")[0][0]["count"] == "2"


def test_an_empty_name_lists_the_untagged_tracks_not_the_library(browse, store):
    rows, total = browse("ge:")
    assert sorted(r["id"] for r in rows) == ["mod1", "zip1", "zip2"]
    assert sorted(r["id"] for r in browse("al:")[0]) == ["mod1", "sid1", "zip1", "zip2"]


def test_tune_entries_are_numbered_and_timed_as_the_web_player_does(browse, store):
    store.update_track_fields("sid1", {"subsongs": 4, "start_subsong": 2,
                                       "hvsc_lengths": [10.0, 20.0, 30.0, 40.0]})
    store.create_playlist("p1", "P", [{"id": "sid1", "subsong": 0}, {"id": "sid1", "subsong": 2},
                                      {"id": "sid1", "subsong": 3}], owner_user_id="u1")
    store.update_playlist("p1", {"shared": True})
    rows = browse("pl:p1")[0]
    assert [r["title"] for r in rows] == ["sid1 (tune 3)", "sid1 (tune 1)", "sid1 (tune 4)"]
    assert [r["res"][0][2] for r in rows] == ["0:00:30.000", "0:00:10.000", "0:00:40.000"]


def test_a_link_naming_tune_0_reaches_the_stream_endpoint_as_that_tune():
    from starlette.requests import Request
    from soniqboom.api import cast_stream
    from soniqboom.api.stream import explicit_wire
    scope = {"type": "http", "method": "GET", "path": "/cast/t/x.flac", "query_string": b"",
             "headers": [(b"range", b"bytes=0-")]}
    req = Request(scope)
    for claims, want in (({"o": "dlna", "sn": 0}, 0), ({"o": "dlna", "sn": 2}, 2),
                         ({"o": "dlna"}, None), ({"sn": 0}, None)):
        r2 = cast_stream._stream_request(req, claims)
        assert explicit_wire(cast_stream._stream_subsong(claims), r2) == want
        assert r2.headers.get("range") == "bytes=0-"


def test_mp4_and_wav_are_offered_as_the_stream_endpoint_serves_them(browse, store):
    store.upsert_tracks_batch([_row("m4a1", "Pop/g.m4a", "AAC"), _row("wav1", "Pop/h.wav", "WAV")])
    items = {r["id"]: r for r in browse("music/all")[0]}
    assert [m for m, _, _ in items["m4a1"]["res"]] == ["audio/flac", "audio/mpeg"]
    assert [m for m, _, _ in items["wav1"]["res"]] == ["audio/wav"]
