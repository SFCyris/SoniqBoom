# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An ``artist:`` filter's first page resolves its candidates once: when the
album-artist split finds no split (every match is an album-artist match, or
none is), the plain order takes the candidates the split resolved."""
from __future__ import annotations

import threading

from soniqboom.core.store import TrackStore


def _store(split: bool) -> TrackStore:
    s = TrackStore()
    s._owner_thread = threading.get_ident()
    for i in range(300):
        a = f"Artist{i % 7} Band"
        s.upsert_track({"id": f"t{i:04d}", "path": f"/m/{a}/{i}.mp3", "title": f"Song {i}",
                        "artist": a, "album_artist": ("Various" if split and i % 3 == 0 else a),
                        "album": f"Al{i % 11}", "duration": 100 + i, "format": "MP3",
                        "added_at": 1_700_000_000 + i})
    return s


def _spy(s: TrackStore) -> list:
    calls = []
    real = s._candidate_ids

    def counted(**kw):
        calls.append(kw)
        return real(**kw)
    s._candidate_ids = counted
    return calls


def test_no_split_resolves_the_candidates_once_with_the_same_page():
    s = _store(split=False)
    ref = [t["id"] for t in _store(split=False).filter_tracks(artist_match="band", limit=40,
                                                              offset=5)]
    calls = _spy(s)
    page = [t["id"] for t in s.filter_tracks(artist_match="band", limit=40, offset=5)]
    assert page == ref and len(page) == 40
    assert len(calls) == 1
    # the hidden tracks (a drive offline) still left out
    hidden = frozenset(page[:3])
    calls.clear()
    s2 = _store(split=False)
    calls = _spy(s2)
    got = [t["id"] for t in s2.filter_tracks(artist_match="band", limit=40, offset=5,
                                             exclude=hidden)]
    assert not hidden & set(got) and len(calls) == 1


def test_a_split_still_puts_album_artist_matches_first():
    s = _store(split=True)
    page = s.filter_tracks(artist_match="band", limit=300)
    kinds = ["aa" if t["album_artist"] != "Various" else "other" for t in page]
    assert kinds == sorted(kinds, key=lambda k: k != "aa") and "other" in kinds
