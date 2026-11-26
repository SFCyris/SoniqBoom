# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AirPlay now-playing covers (Cast is Beta): the kept cover of a retro track is
keyed by its generated cover's own key, so an edit or an in-place change that
alters that cover asks again instead of pushing the old picture until a
restart.  A track without a generated cover keeps the plain track-id key."""
from __future__ import annotations

import pytest

from soniqboom.core import cast_session
from soniqboom.core.store import TrackStore


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    cast_session._art_cache.clear()
    yield s
    cast_session._art_cache.clear()


def _sid(**kw):
    t = {"id": "sid1", "path": "/music/C64/Commando.sid", "format": "SID",
         "title": "Commando", "artist": "Rob Hubbard", "game": "Commando", "duration": 180}
    t.update(kw)
    return t


async def test_a_changed_generated_cover_is_asked_again(store, monkeypatch):
    calls = []

    async def resolve(track_id):
        calls.append(track_id)
        return f"cover-{len(calls)}".encode()
    monkeypatch.setattr(cast_session, "_resolve_and_cap_art", resolve)
    store.upsert_track(_sid())
    first = await cast_session._airplay_artwork_bytes("sid1")
    assert first == b"cover-1"
    assert await cast_session._airplay_artwork_bytes("sid1") == b"cover-1"   # kept: warm
    assert len(calls) == 1
    # an edit that changes what the cover prints (the game) → a miss
    store.upsert_track(_sid(game="Commando (Remix)"))
    assert await cast_session._airplay_artwork_bytes("sid1") == b"cover-2"
    assert len(calls) == 2


async def test_a_track_without_a_generated_cover_keeps_the_track_id_key(store, monkeypatch):
    async def resolve(track_id):
        return b"own"
    monkeypatch.setattr(cast_session, "_resolve_and_cap_art", resolve)
    store.upsert_track({"id": "mp3", "path": "/music/a.mp3", "format": "MP3", "title": "A"})
    assert await cast_session._airplay_artwork_bytes("mp3") == b"own"
    assert "mp3" in cast_session._art_cache
