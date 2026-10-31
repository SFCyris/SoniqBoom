# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playlist files (``core/playlist_files``, ``POST /playlists/import``,
``GET /playlists/{id}/export``): M3U / M3U8 / XSPF read and written; each
entry matched by its path, its file name and folders, or its artist and
title — never a guess between several tracks; a file written here reads back
to the same entries (tunes included); a share's password never leaves in an
export; an XML bomb is refused."""
from __future__ import annotations

import types

import pytest

from soniqboom.core import playlist_files as pf
from soniqboom.core.playlist_files import Entry, LibraryRow, Matcher
from soniqboom.core.store import TrackStore
from tests.test_per_user_stations_playlists import _playlist_app

LIB = [
    LibraryRow("a", "/srv/music/Kitaro/Silk Road/07 Taklamakan Desert.flac", "Kitaro",
               "Taklamakan Desert", 303.0),
    LibraryRow("b", "/srv/music/Kitaro/Silk Road/01 Theme from Silk Road.flac", "Kitaro",
               "Theme from Silk Road", 324.0),
    # the same file name in two albums: only the folders tell them apart
    LibraryRow("c1", "/srv/music/Band/Live/01 Intro.mp3", "Band", "Intro", 60.0),
    LibraryRow("c2", "/srv/music/Band/Studio/01 Intro.mp3", "Band", "Intro", 95.0),
    # a duplicate pair (one hidden copy) — and an online-collection twin
    LibraryRow("d1", "/srv/music/C64/Commando.sid", "Rob Hubbard", "Commando", 200.0,
               primary=True, tunes=19, group="g7"),
    LibraryRow("d2", "/srv/backup/C64/Commando.sid", "Rob Hubbard", "Commando", 200.0,
               primary=False, tunes=19, group="g7"),
    # an unrelated file of the same name (no folder in common with the entries below)
    LibraryRow("o", "/m/Other Band/Greatest/01 - Intro.mp3", "Other Band", "Intro", 70.0),
    LibraryRow("e", "ftp://nas.local/music:/Chip/sleepless.xm", "", "sleepless", 189.0),
    LibraryRow("f", "/srv/music/Mixed/pack.zip::inner/d.mod", "", "d", 30.0),
]


def _match(entry):
    r, how = Matcher(LIB).match(entry)
    return (r.id if r else None), how


def test_m3u_reading():
    text = ("\ufeff#EXTM3U\n#PLAYLIST:Road trip\n"
            "#EXTINF:303,Kitaro - Taklamakan Desert\n"
            "D:\\Music\\Kitaro\\Silk Road\\07 Taklamakan Desert.flac\n\n"
            "#EXTINF:-1 tvg-id=\"x\",Some Radio\nhttp://radio.example/stream\n"
            "#EXTSB-TUNE:3\n../C64/Commando.sid\n")
    name, entries, truncated = pf.parse(text, "trip.m3u8")
    assert name == "Road trip" and not truncated
    assert [(e.line, e.duration, e.artist, e.title, e.tune) for e in entries] == [
        (4, 303.0, "Kitaro", "Taklamakan Desert", None),
        (7, None, "", "Some Radio", None),
        (9, None, "", "", 3)]
    assert pf.location_path(entries[0].location) == "D:/Music/Kitaro/Silk Road/07 Taklamakan Desert.flac"


def test_matching_rules():
    # the stored path / a file:// URI of it
    assert _match(Entry(location=LIB[0].path)) == ("a", "path")
    assert _match(Entry(location="file:///srv/music/Kitaro/Silk%20Road/01%20Theme%20from%20Silk%20Road.flac")) == ("b", "path")
    # another disk / Windows / relative: the file name and folders
    assert _match(Entry(location="E:\\Kitaro\\Silk Road\\07 Taklamakan Desert.flac")) == ("a", "folder")
    assert _match(Entry(location="../Live/01 Intro.mp3")) == ("c1", "folder")
    assert _match(Entry(location="/Volumes/X/Studio/01 Intro.mp3")) == ("c2", "folder")
    # the file name alone can't tell the albums apart — the length can
    assert _match(Entry(location="01 Intro.mp3", duration=94)) == ("c2", "folder")
    assert _match(Entry(location="01 Intro.mp3", title="Intro", artist="Band")) == (
        None, "several tracks match")
    # the file name alone is no proof: another band's "01 - Intro.mp3" is never taken
    assert _match(Entry(location="D:\\Music\\My Band\\Album\\01 - Intro.mp3",
                        artist="My Band", title="Intro")) == (None, "only the file name matches")
    assert _match(Entry(location="01 - Intro.mp3")) == (None, "only the file name matches")
    assert _match(Entry(location="x/01 - Intro.mp3", artist="Other Band", title="intro")) == ("o", "folder")
    # a primary copy before its hidden duplicate (copies of one track)
    assert _match(Entry(location="/elsewhere/C64/Commando.sid")) == ("d1", "folder")
    # artist + title (case, accents and spacing ignored), the length within 5 s
    assert _match(Entry(location="x/unknown.flac", artist="KITARO", title="taklamakan  desert",
                        duration=300)) == ("a", "tags")
    assert _match(Entry(title="Taklamakan Desert", artist="Kitaro", duration=250)) == (None, "not in the library")
    # a share's file by its URL; an archive member
    assert _match(Entry(location="ftp://nas.local/music/Chip/sleepless.xm")) == ("e", "folder")
    assert _match(Entry(location="/other/pack.zip::inner/d.mod")) == ("f", "folder")
    assert _match(Entry(location="http://radio.example/stream")) == (
        None, "a web address")


def test_import_keeps_order_duplicates_and_tunes():
    entries = [Entry(location=LIB[1].path, line=1), Entry(location="nope.mp3", line=2),
               Entry(location=LIB[4].path, tune=3, line=3), Entry(location=LIB[1].path, line=4),
               Entry(location=LIB[4].path, tune=40, line=5)]        # (no tune 40: the file)
    res = pf.import_entries("x", entries, Matcher(LIB))
    assert res.entries == ["b", {"id": "d1", "subsong": 2}, "b", "d1"]
    assert res.by == {"path": 4, "folder": 0, "tags": 0} and res.unmatched_count == 1
    assert res.unmatched == [{"line": 2, "entry": "nope.mp3", "reason": "not in the library"}]


@pytest.mark.parametrize("fmt", ["m3u", "xspf"])
def test_a_file_written_here_reads_back_the_same(fmt):
    tracks = {r.id: {"id": r.id, "path": r.path, "artist": r.artist, "title": r.title,
                     "duration": r.duration, "album": "Al"} for r in LIB}
    rows = [(tracks["a"], None), (tracks["e"], None), (tracks["d1"], 3), (tracks["f"], None),
            (tracks["c2"], None)]
    text = (pf.export_m3u if fmt == "m3u" else pf.export_xspf)("Mix «1» & co", rows)
    name, entries, _ = pf.parse(text, f"x.{fmt}")
    assert name == "Mix «1» & co"
    res = pf.import_entries(name, entries, Matcher(LIB))
    assert res.entries == ["a", "e", {"id": "d1", "subsong": 2}, "f", "c2"]
    assert res.unmatched_count == 0


def test_an_export_never_carries_a_share_password():
    t = {"path": "ftp://bob:s3cret@nas.local:2121/music:/Chip/a.xm", "title": "a"}
    for text in (pf.export_m3u("p", [(t, None)]), pf.export_xspf("p", [(t, None)])):
        assert "s3cret" not in text and "bob@" not in text
        assert "ftp://nas.local:2121/music/Chip/a.xm" in text
    # a password holding / @ ? # :/ — the configured root says where it ends;
    # an @ in a file name stays
    for root in ("smb://user:pa/ss@host/share", "ftp://user:/x@host/r", "smb://u:p?w#d@h/s",
                 "smb://u:a:/b@h/s"):
        t = {"path": root + ":/Dir/me@home.mp3", "title": "x"}
        for text in (pf.export_m3u("p", [(t, None)], [root]), pf.export_xspf("p", [(t, None)], [root])):
            secret = root.split("//", 1)[1].rsplit("@", 1)[0]
            assert secret not in text, (root, text)
            assert "/Dir/me@home.mp3" in text
    assert pf.public_location("http://h:8150:/Covers/x.m4a") == "http://h:8150/Covers/x.m4a"
    assert pf.public_location("/a/pack.zip::in/x.mod") == "/a/pack.zip::in/x.mod"


def test_encodings_and_spellings():
    # UTF-16 files; percent-encoded XSPF / URL locations; NFD vs NFC names
    m3u = "#EXTM3U\r\n#EXTINF:303,Kitaro - Taklamakan Desert\r\nX/07 Taklamakan Desert.flac\r\n"
    assert pf.parse(pf.decode(m3u.encode("utf-16")), "a.m3u")[1][0].title == "Taklamakan Desert"
    lib = LIB + [LibraryRow("n", "/srv/music/Bj\u00f6rk/Song one.flac", "Bj\u00f6rk", "Song one", 10.0)]
    for loc, uri in (("Bjo\u0308rk/Song%20one.flac", True), ("http://h/Bj%C3%B6rk/Song%20one.flac", False),
                     ("/x/Bjo\u0308rk/Song one.flac", False)):
        r, how = Matcher(lib).match(Entry(location=loc, uri=uri))
        assert (r and r.id, how) == ("n", "folder"), loc
    # an EXTINF's artist never carries over to the next entry
    _, entries, _ = pf.parse("#EXTINF:100,X - Song\n\n#EXTINF:-1,\nb.mp3\n", "a.m3u")
    assert entries[0].artist == "" and entries[0].title == ""


def test_hostile_and_broken_files_are_refused():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
            '<playlist xmlns="http://xspf.org/ns/0/"><title>&lol2;</title></playlist>')
    for text in (bomb, "<playlist><trackList><track>", "<html><body>x</body></html> <playlist"):
        with pytest.raises(ValueError):
            pf.parse(text, "x.xspf")
    with pytest.raises(ValueError):
        pf.parse("<not xml", "x.xspf")
    assert pf.decode(b"\xef\xbb\xbfA\xc3\xa9") == "Aé"
    assert pf.decode(b"A\xe9") == "Aé"                   # (an old Latin-1 M3U)
    assert pf.playlist_name(None, "C:\\x\\Road Trip.M3U8") == "Road Trip"
    assert pf.playlist_name("  ", "") == "Imported playlist"
    assert pf.safe_filename('a/b:c*"d', "m3u8") == "a b c d.m3u8"
    assert pf.safe_filename("x\x7fy", "xspf") == "x y.xspf"


def test_import_and_export_over_http(monkeypatch):
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    for r in LIB:
        st.upsert_track({"id": r.id, "path": r.path, "title": r.title, "artist": r.artist,
                         "duration": r.duration, "subsongs": r.tunes,
                         **({"duplicate_group_id": r.group,
                             "is_duplicate_primary": r.primary} if r.group else {})})
    who = {"A": types.SimpleNamespace(id="ann", role="edit", enabled=True),
           "B": types.SimpleNamespace(id="bob", role="readonly", enabled=True)}
    names = {"ann": types.SimpleNamespace(username="ann", display_name=None)}
    c = _playlist_app(monkeypatch, st, who, names)
    A = {"sb_session": "A"}
    text = ("#EXTM3U\n#EXTINF:303,Kitaro - Taklamakan Desert\nE:\\K\\Silk Road\\07 Taklamakan Desert.flac\n"
            "#EXTSB-TUNE:2\n/x/C64/Commando.sid\nmissing.ogg\n")
    r = c.post("/playlists/import", json={"filename": "Trip.m3u", "content": text}, cookies=A)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["playlist"]["name"] == "Trip" and got["matched"] == 2 and got["total"] == 3
    assert got["by"] == {"path": 0, "folder": 2, "tags": 0} and got["unmatched_count"] == 1
    pid = got["playlist"]["id"]
    assert st.get_playlist(pid)["track_ids"] == ["a", {"id": "d1", "subsong": 1}]
    assert st.playlist_owner(st.get_playlist(pid)) == "ann"
    # readonly accounts import too (their own playlists)
    assert c.post("/playlists/import", json={"filename": "x.m3u", "content": text},
                  cookies={"sb_session": "B"}).status_code == 201
    # nothing found / not a playlist: no playlist made
    before = len(st.list_playlists_for_user("ann"))
    assert c.post("/playlists/import", json={"filename": "n.m3u", "content": "nope.mp3\n"},
                  cookies=A).status_code == 422
    assert c.post("/playlists/import", json={"filename": "n.xspf", "content": "<x"},
                  cookies=A).status_code == 422
    assert len(st.list_playlists_for_user("ann")) == before
    # export: an attachment, read back to the same entries
    for fmt, mime in (("m3u8", "audio/x-mpegurl"), ("xspf", "application/xspf+xml")):
        e = c.get(f"/playlists/{pid}/export", params={"format": fmt}, cookies=A)
        assert e.status_code == 200 and e.headers["content-type"].startswith(mime)
        assert e.headers["content-disposition"].startswith('attachment; filename="Trip.')
        back = c.post("/playlists/import", json={"filename": f"t.{fmt}", "content": e.text},
                      cookies=A).json()
        assert st.get_playlist(back["playlist"]["id"])["track_ids"] == ["a", {"id": "d1", "subsong": 1}]
        assert back["by"]["path"] == 2
    # someone else's unshared playlist: no export
    assert c.get(f"/playlists/{pid}/export", cookies={"sb_session": "B"}).status_code == 404
    assert c.get(f"/playlists/{pid}/export", params={"format": "exe"}, cookies=A).status_code == 422
