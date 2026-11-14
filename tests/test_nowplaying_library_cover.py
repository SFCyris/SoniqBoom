# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The library step of the now-playing cover lookup (``nowplaying_art``): a
matching track counts only when its cover is really there — the art route
answers a grey placeholder for a track without one, which would hide the
station's logo — and a library hit is never remembered."""
from __future__ import annotations

import pytest

from soniqboom.api import art as A
from soniqboom.core import netpolicy, nowplaying_art as N
from soniqboom.core import store as store_mod


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.asked = 0

    def search(self, _q, limit=25):
        self.asked += 1
        return list(self.rows)[:limit]


def _row(tid, artist="The Artist", title="A Song", **kw):
    return {"id": tid, "artist": artist, "title": title, **kw}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A library (rows), the covers its tracks hold, and one external source."""
    w = type("W", (), {})()
    w.store = _Store([])
    w.covers = set()            # track ids whose cover resolves
    w.thumb_asked = []
    w.external = None           # what MusicBrainz finds
    w.external_asked = 0
    monkeypatch.setattr(store_mod, "get_store", lambda: w.store)
    monkeypatch.setattr(N, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(N, "_mem", {})
    monkeypatch.setattr(N, "_locks", {})
    monkeypatch.setattr(netpolicy, "blocked", lambda: False)

    async def thumb(tid, bucket, *, resolve=True):
        w.thumb_asked.append((tid, bucket))
        return b"\xff\xd8jpeg" if tid in w.covers else None

    async def brainz(_artist, _title):
        w.external_asked += 1
        return w.external

    async def discogs(*_a):
        return None

    monkeypatch.setattr(A, "cover_thumb", thumb)
    monkeypatch.setattr(N, "_musicbrainz_cover", brainz)
    monkeypatch.setattr(N, "_discogs_cover", discogs)
    return w


FOUND = {"cover_bytes": b"\xff\xd8" + b"x" * 300, "album": "Alb", "year": 1990,
         "label": "Lab", "source": "musicbrainz"}


async def test_a_match_with_a_cover_is_the_library_cover(world):
    world.store.rows = [_row("t1", cover_art="x")]
    world.covers = {"t1"}
    res = await N.lookup("The Artist", "A Song")
    assert res == {"cover_url": "/api/art/t1/lg.jpg", "album": "", "year": None,
                   "label": "", "source": "library"}
    assert world.thumb_asked == [("t1", "lg")]
    assert world.external_asked == 0
    assert list(N._mem) == []                         # not remembered


async def test_a_match_without_a_cover_goes_on_to_the_external_sources(world):
    world.store.rows = [_row("t1")]                   # no embedded cover, none in its folder
    world.external = dict(FOUND)
    res = await N.lookup("The Artist", "A Song")
    assert res["cover_url"] == "/api/stations/nowplaying-art/the-artist-a-song"
    assert res["source"] == "musicbrainz" and res["album"] == "Alb"
    assert world.thumb_asked == [("t1", "lg")]
    assert world.external_asked == 1


async def test_a_match_without_a_cover_and_nothing_found_is_no_cover(world):
    world.store.rows = [_row("t1")]
    assert await N.lookup("The Artist", "A Song") is None
    assert await N.lookup("The Artist", "A Song") is None
    assert world.external_asked == 1                  # the miss is remembered …
    assert world.store.asked == 2                     # … the library asked again


async def test_the_copy_that_holds_the_cover_is_taken(world):
    world.store.rows = [_row("bare"), _row("folder"), _row("tagged", cover_art="x"),
                        _row("other", title="Another Song", cover_art="x")]
    world.covers = {"folder"}                         # the tagged one's cover is gone
    res = await N.lookup("the artist", "a song!")
    assert res["cover_url"] == "/api/art/folder/lg.jpg"
    # the track known to hold one is asked first, a different song never
    assert world.thumb_asked == [("tagged", "lg"), ("bare", "lg"), ("folder", "lg")]


async def test_only_a_few_copies_are_asked(world):
    world.store.rows = [_row(f"t{i}") for i in range(9)]
    world.covers = {"t8"}
    assert await N.lookup("The Artist", "A Song") is None
    assert len(world.thumb_asked) == N._LOCAL_TRIES == 4


async def test_a_sid_counts_only_with_a_cover(world):
    world.store.rows = [_row("sid", sid_md5="abc")]
    world.covers = {"sid"}
    assert await N.lookup("The Artist", "A Song") is None
    assert world.thumb_asked == []
    world.store.rows = [_row("sid", sid_md5="abc", cover_art="x")]
    assert (await N.lookup("The Artist", "A Song"))["cover_url"] == "/api/art/sid/lg.jpg"


async def test_the_library_is_asked_before_a_remembered_external_cover(world):
    world.external = dict(FOUND)
    first = await N.lookup("The Artist", "A Song")
    assert first["source"] == "musicbrainz"
    world.store.rows = [_row("t1", cover_art="x")]    # the song joins the library
    world.covers = {"t1"}
    assert (await N.lookup("The Artist", "A Song"))["source"] == "library"
    world.covers = set()                              # … and its cover goes again
    again = await N.lookup("The Artist", "A Song")
    assert again == first and world.external_asked == 1


async def test_a_library_hit_remembered_by_an_earlier_version_is_asked_anew(world):
    N._write_cache(N._key("The Artist", "A Song"), {
        "found": True,
        "result": {"cover_url": "/api/art/gone/lg.jpg", "album": "", "year": None,
                   "label": "", "source": "library"}})
    world.external = dict(FOUND)
    res = await N.lookup("The Artist", "A Song")
    assert res["source"] == "musicbrainz" and world.external_asked == 1


async def test_a_cover_that_cannot_be_read_is_no_cover(world, monkeypatch):
    async def boom(tid, bucket, *, resolve=True):
        raise OSError("share away")
    monkeypatch.setattr(A, "cover_thumb", boom)
    world.store.rows = [_row("t1", cover_art="x")]
    assert await N.lookup("The Artist", "A Song") is None
