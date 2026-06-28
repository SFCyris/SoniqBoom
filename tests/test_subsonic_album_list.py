# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``getAlbumList`` / ``getAlbumList2`` per-album ``songCount``.

Regression guard for the case-mismatch bug: the album sub-counters in the
store are keyed on ``album.lower()`` (see ``store._index_track``), but the
endpoint looked them up with the *raw-case* album name from
``album_sample_index()``.  Any album whose title contained an uppercase
letter therefore missed the counter and reported ``songCount: 0`` — the
all-lowercase albums happened to work, which is exactly why it went unnoticed.

We drive the real handler (auth stubbed, store swapped for a hand-built one)
and assert exact counts for:

  * a mixed-case album resolved via the *album_artist* counter (the bug),
  * a mixed-case album with no album_artist, resolved via the *artist*
    fallback counter (same bug on the fallback path),
  * an all-lowercase control that worked before and must still work.
"""
from __future__ import annotations

import json
import types

import pytest

from soniqboom.api import subsonic
from soniqboom.core.store import TrackStore


def _track(tid: str, *, artist: str, album_artist: str, album: str) -> dict:
    """Minimal track dict carrying every field the endpoint reads back."""
    return {
        "id": tid,
        "title": f"Track {tid}",
        "artist": artist,
        "album_artist": album_artist,
        "album": album,
        "genre": ["Rock"],
        "year": 1969,
        "added_at": 1_700_000_000 + int(tid[-2:] or 0),
        "duration": 180.0,
    }


def _build_store() -> TrackStore:
    store = TrackStore()
    # Mixed-case album resolved ONLY via the album_artist counter.  The
    # album_artist ("The Beatles") deliberately differs from the per-track
    # artist ("George Harrison") so the artist-counter fallback — keyed on the
    # album_artist value — finds no "the beatles" bucket and returns 0.  The
    # count therefore comes purely from the primary lookup, isolating that path.
    store.upsert_track(_track("01", artist="George Harrison", album_artist="The Beatles", album="Abbey Road"))
    store.upsert_track(_track("02", artist="John Lennon", album_artist="The Beatles", album="Abbey Road"))
    store.upsert_track(_track("03", artist="Paul McCartney", album_artist="The Beatles", album="Abbey Road"))
    # Mixed-case album with NO album_artist — the primary counter has no bucket
    # for it (the store only records album_artist albums when album_artist is
    # truthy), so this resolves ONLY via the artist fallback counter, isolating
    # the fallback path under the same lowercase keying.
    store.upsert_track(_track("04", artist="Pink Floyd", album_artist="", album="The Wall"))
    store.upsert_track(_track("05", artist="Pink Floyd", album_artist="", album="The Wall"))
    # All-lowercase control — worked before the fix, must still work.
    store.upsert_track(_track("06", artist="aphex twin", album_artist="aphex twin", album="drukqs"))
    return store


async def _call_album_list(monkeypatch, store: TrackStore, *, view: str) -> list[dict]:
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: None)
    # Reset the module-level debounced cache so a sibling test's build can't
    # leak in (the debounce returns a stale 'seen' across differing seqs).
    monkeypatch.setattr(
        subsonic, "_ALBUM_LIST_CACHE",
        {"seq": None, "seen": {}, "built_at": 0.0},
    )
    request = types.SimpleNamespace(url=types.SimpleNamespace(path=f"/rest/{view}"))
    resp = await subsonic.get_album_list(
        request=request,
        type="alphabeticalByName",
        size=500,
        offset=0,
        fromYear=None,
        toYear=None,
        genre=None,
        sb_session=None,
        u=None, p=None, s=None, t=None,
        f="json",
    )
    body = json.loads(resp.body)
    key = "albumList2" if view.startswith("getAlbumList2") else "albumList"
    return body["subsonic-response"][key]["album"]


@pytest.mark.parametrize("view", ["getAlbumList2", "getAlbumList"])
async def test_song_count_mixed_case_album(monkeypatch, view):
    albums = await _call_album_list(monkeypatch, _build_store(), view=view)
    counts = {a["name"]: a["songCount"] for a in albums}

    # The regression: mixed-case title via the album_artist counter.
    # Pre-fix this was 0 because "Abbey Road" missed the "abbey road" key.
    assert counts["Abbey Road"] == 3
    # Same bug on the album_artist-empty → artist fallback path.
    assert counts["The Wall"] == 2
    # Control that never broke.
    assert counts["drukqs"] == 1


async def test_song_count_inversion_no_album_reports_zero(monkeypatch):
    # Falsifying companion: an album that genuinely has no indexed tracks
    # under either counter must still report 0 — proves a non-zero count
    # reflects a real lookup hit, not an unconditional value.
    albums = await _call_album_list(monkeypatch, _build_store(), view="getAlbumList2")
    assert all(a["songCount"] > 0 for a in albums)
    assert "Nonexistent Album" not in {a["name"] for a in albums}
