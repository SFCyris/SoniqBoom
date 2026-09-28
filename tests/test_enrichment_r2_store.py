# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-2 review fixes in the TrackStore:

* a duration-only write (the render / probe backfill of a render-only
  format's real length, on every first play) bumps ``_duration_seq`` only, so
  the web aggregates and the Subsonic catalogue stay warm — while the scanner's
  yielding sorted-index rebuild still notices it;
* ``filter_tracks`` sorts a small candidate set directly instead of walking
  the whole library's sorted index — with the identical page."""
from __future__ import annotations

import random

import pytest

from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _track(r: random.Random, i: int) -> dict:
    return {
        "id": f"t{i:04d}", "path": f"/m/{i}.mod",
        "title": r.choice(["Gold", "aztecs", "", "Uridium", "zombie", "ROCK"]),
        "artist": r.choice(["A", "b", "", "Hubbard"]),
        "album_artist": r.choice(["", "X"]), "album": r.choice(["", "Alb", "alb"]),
        "format": r.choice(["ProTracker", "SID", "MP3", ""]),
        "year": r.choice([None, 1990, 19910304, 2001]),
        "added_at": r.choice([0, 1000 + i, 5000]),
        "duration": r.choice([0.0, 12.5, 180.0]),
        "bpm": r.choice([None, 120.0, 0.0]),
        "is_duplicate_primary": r.choice([True, False]),
        "genre": [],
    }


def _store(n=300, seed=5) -> TrackStore:
    r = random.Random(seed)
    s = TrackStore()
    s.upsert_tracks_batch([_track(r, i) for i in range(n)])
    return s


# ── r2-enr-8: duration-only writes ───────────────────────────────────────────

def test_duration_only_write_keeps_the_library_caches_warm():
    s = _store()
    artists = s.aggregate_artists()                    # memoised on _mutation_seq
    seq, cat, dur = s._mutation_seq, s._catalog_seq, s._duration_seq
    assert s.update_track_fields("t0001", {"duration": 99.5}) is True
    assert (s._mutation_seq, s._catalog_seq) == (seq, cat)
    assert s._duration_seq == dur + 1
    assert s.aggregate_artists() is artists            # no recompute
    assert s.verify_indexes()["index_ok"]              # _sorted_duration re-keyed
    # With seq-exempt cover art it is still duration-only.
    s.update_track_fields("t0002", {"duration": 1.5, "cover_art": "/api/art/t0002"})
    assert (s._mutation_seq, s._duration_seq) == (seq, dur + 2)
    # Batch form.
    s.update_track_fields_batch([("t0003", {"duration": 7.0}), ("t0004", {"duration": 8.0})])
    assert (s._mutation_seq, s._catalog_seq, s._duration_seq) == (seq, cat, dur + 3)


def test_duration_with_another_field_still_bumps_both_sequences():
    s = _store()
    seq, cat, dur = s._mutation_seq, s._catalog_seq, s._duration_seq
    s.update_track_fields("t0001", {"duration": 42.0, "title": "Renamed"})
    assert (s._mutation_seq, s._catalog_seq, s._duration_seq) == (seq + 1, cat + 1, dur)
    s.update_track_fields_batch([("t0002", {"duration": 43.0}),
                                 ("t0003", {"bitrate": 1})])
    assert s._mutation_seq == seq + 2 and s._catalog_seq == cat + 1
    assert s._duration_seq == dur + 1


async def test_async_rebuild_retries_when_a_duration_write_lands_mid_build(monkeypatch):
    from soniqboom.core import scanner
    s = _store()
    s.enter_batch_mode()
    s.update_track_fields_batch([(f"t{i:04d}", {"duration": 500.0 + i}) for i in range(20)])
    assert "_sorted_duration" in s._dirty_sorted
    real = s.build_sorted_list
    fired = []

    def build_then_write(name, changed=None):
        out = real(name, changed)
        if name == "_sorted_duration" and not fired:
            fired.append(1)
            # The first-play backfill of another track lands during the
            # rebuild's yields — after this list was built.
            s.update_track_fields("t0100", {"duration": 777.0})
        return out
    monkeypatch.setattr(s, "build_sorted_list", build_then_write)
    await scanner._async_exit_batch_mode(s)
    assert fired and not s._dirty_sorted and not s._batch_mode
    assert (777.0, "t0100") in s._sorted_duration
    assert s.verify_indexes()["index_ok"]


# ── r2-enr-15: small candidate sets are sorted directly ─────────────────────

@pytest.mark.parametrize("sort_by", [None, "added", "year", "duration", "bpm", "title",
                                     "artist", "album_artist", "album", "format"])
@pytest.mark.parametrize("order", [None, "asc", "desc"])
@pytest.mark.parametrize("dups", [False, True])
def test_small_candidate_page_equals_the_index_walk(monkeypatch, sort_by, order, dups):
    s = _store(n=600, seed=11)
    kw = dict(sort_by=sort_by, sort_order=order, filter_duplicates=dups)
    queries = [dict(artist="Hubbard"), dict(format_="SID", artist="A"),
               dict(game="uridium"), dict(album="alb"), dict(query="gold")]
    fast = [s.filter_tracks(**q, **kw, limit=lim, offset=off)
            for q in queries for lim, off in ((10, 0), (7, 5), (500, 0))]
    walked_calls = []
    monkeypatch.setattr(s, "_sort_candidates",
                        lambda *a: walked_calls.append(1) and None)
    walked = [s.filter_tracks(**q, **kw, limit=lim, offset=off)
              for q in queries for lim, off in ((10, 0), (7, 5), (500, 0))]
    assert walked_calls                                 # the fast path was in use
    assert [[t["id"] for t in p] for p in fast] == [[t["id"] for t in p] for p in walked]


def test_large_candidate_sets_keep_walking_the_index(monkeypatch):
    s = _store(n=600, seed=12)
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 50)
    thr = max(50, len(s._sorted_added_at) // 64)
    calls = []
    real = s._sort_candidates
    monkeypatch.setattr(s, "_sort_candidates", lambda *a: calls.append(1) or real(*a))
    sizes = []
    for q in (dict(format_="SID"), dict(artist="Hubbard", format_="SID")):
        n = len(s._candidate_ids(**q))
        sizes.append(n)
        calls.clear()
        s.filter_tracks(**q, limit=10)
        assert bool(calls) == (n <= thr), (q, n, thr)
    assert min(sizes) <= thr < max(sizes)               # both paths exercised


def test_mixed_type_keys_fall_back_to_the_walk():
    s = TrackStore()
    s.upsert_tracks_batch([
        {"id": "a", "title": "x", "bpm": 120.0, "added_at": 1},
        {"id": "b", "title": "x", "bpm": 90.0, "added_at": 2},
    ])
    s._tracks["b"]["bpm"] = "fast"          # a raw value no index insert accepts
    assert s._sort_candidates({"a", "b"}, "_sorted_bpm", False) is None
    assert [t["id"] for t in s.filter_tracks(query="x", sort_by="bpm")] == ["b", "a"]
