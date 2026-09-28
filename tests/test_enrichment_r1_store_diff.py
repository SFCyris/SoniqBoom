# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Diff-based field updates in the TrackStore.

``update_track_fields[_batch]`` maintains only the derived indexes whose input
fields changed, skips no-op writes entirely, keeps ``_mutation_seq`` still for
``cover_art``-only writes, and — inside a batch section — marks only the
re-keyed sorted lists for the deferred rebuild.  The parity tests apply random
mixed patches and compare every index against a fresh ``rebuild_indexes``."""
from __future__ import annotations

import asyncio
import random

import pytest

from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore

_WORDS = ["gold", "aztecs", "turrican", "uridium", "zombie", "attack", "rock",
          "chip", "demo", "Gold", "ROCK", "  spaced  ", "", "x", "fairlight"]


def _rand_track(r: random.Random, i: int) -> dict:
    def phrase():
        return " ".join(r.choice(_WORDS) for _ in range(r.randint(0, 3)))
    return {
        "id": f"t{i}", "path": f"/m/{i}.mod", "title": phrase(), "artist": phrase(),
        "album_artist": phrase(), "album": phrase(), "composer": phrase(),
        "scene_group": r.choice(["", "Fairlight", "Fairlight • Maniacs of Noise", None]),
        "format": r.choice(["ProTracker", "SID", "MP3", "", "sid"]),
        "dir_hash": r.choice(["d1", "d2", ""]), "scan_root_hash": r.choice(["r1", "r2"]),
        "duplicate_group_id": r.choice([None, "", "g1", "g2"]),
        "genre": r.sample(["Rock", "rock", "Chip", "Demo"], r.randint(0, 2)),
        "year": r.choice([None, 1990, 19910304, 2001]),
        "added_at": r.choice([0, 1000 + i, 5000]),
        "duration": r.choice([0.0, 12.5, 180.0]),
        "bpm": r.choice([None, 120.0, 0.0]),
        "is_duplicate_primary": r.choice([True, False]),
        "bitrate": r.choice([128, 320]), "cover_art": None,
    }


def _rand_patch(r: random.Random, t: dict) -> dict:
    fields = ["title", "artist", "album_artist", "album", "composer", "scene_group",
              "format", "dir_hash", "scan_root_hash", "duplicate_group_id", "genre",
              "year", "added_at", "duration", "bpm", "is_duplicate_primary",
              "bitrate", "cover_art", "album_source", "track_number"]
    src = _rand_track(r, r.randint(0, 10_000))
    patch = {}
    for f in r.sample(fields, r.randint(1, 5)):
        if r.random() < 0.25:
            patch[f] = t.get(f)                 # an unchanged value
        elif f == "cover_art":
            patch[f] = r.choice([None, f"/api/art/{t['id']}"])
        elif f == "album_source":
            patch[f] = r.choice([None, "tag", "folder"])
        elif f == "track_number":
            patch[f] = r.randint(1, 9)
        else:
            patch[f] = src[f]
    return patch


def _assert_parity(s: TrackStore) -> None:
    rep = s.verify_indexes()
    assert rep["index_ok"], rep["mismatches"]
    s._rebuild_word_list()
    assert s._word_list == sorted(s._word_index)


def _seeded_store(r: random.Random, n: int = 250) -> TrackStore:
    s = TrackStore()
    s.upsert_tracks_batch([_rand_track(r, i) for i in range(n)])
    return s


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_patches_keep_every_index_equal_to_a_rebuild(seed):
    r = random.Random(seed)
    s = _seeded_store(r)
    ids = list(s._tracks)
    for _ in range(400):
        tid = r.choice(ids)
        s.update_track_fields(tid, _rand_patch(r, s.get_track(tid)))
    _assert_parity(s)
    for _ in range(40):
        batch = [(tid, _rand_patch(r, s.get_track(tid))) for tid in r.sample(ids, 12)]
        s.update_track_fields_batch(batch)
    _assert_parity(s)


@pytest.mark.parametrize("seed", [4, 5])
def test_random_patches_in_batch_mode_rebuild_only_what_changed(seed):
    r = random.Random(seed)
    s = _seeded_store(r)
    ids = list(s._tracks)
    s.enter_batch_mode()
    for _ in range(300):
        tid = r.choice(ids)
        s.update_track_fields(tid, _rand_patch(r, s.get_track(tid)))
    s.update_track_fields_batch([(tid, _rand_patch(r, s.get_track(tid)))
                                 for tid in r.sample(ids, 50)])
    s.exit_batch_mode()
    assert not s._dirty_sorted and not s._batch_mode
    _assert_parity(s)


async def test_async_batch_exit_rebuilds_only_the_dirty_lists(monkeypatch):
    from soniqboom.core import scanner
    r = random.Random(7)
    s = _seeded_store(r)
    ids = list(s._tracks)
    built: list[str] = []
    real = s.build_sorted_list
    monkeypatch.setattr(s, "build_sorted_list",
                        lambda name, changed=None: built.append((name, changed)) or real(name, changed))
    s.enter_batch_mode()
    s.update_track_fields_batch([(tid, {"album": f"New Album {i % 5}"})
                                 for i, tid in enumerate(ids[:60])])
    assert s._dirty_sorted == {"_sorted_album"}
    assert set(s._dirty_sorted_tids) == {"_sorted_album"}
    assert set(s._dirty_sorted_tids["_sorted_album"]) == set(ids[:60])
    await scanner._async_exit_batch_mode(s)
    assert sorted((n, set(c)) for n, c in built) == [       # merges
        ("_sorted_album", set(ids[:60]))]
    assert not s._dirty_sorted and not s._batch_mode and s._batch_depth == 0
    _assert_parity(s)
    # An insert still marks (and rebuilds) every list.
    built.clear()
    s.enter_batch_mode()
    s.upsert_tracks_batch([_rand_track(r, 9999)])
    s.update_track_fields("t1", {"album": "Another"})
    assert s._dirty_sorted == set(store_mod.SORTED_LISTS)
    await scanner._async_exit_batch_mode(s)
    assert built == []                           # the single-pass full rebuild
    _assert_parity(s)


def test_cover_art_only_update_is_cheap_and_does_not_bump_the_seq():
    s = _seeded_store(random.Random(8), 20)
    s._rebuild_word_list()
    aof: list = []
    s._aof_append = lambda op, **kw: aof.append((op, kw))
    seq, cat = s._mutation_seq, s._catalog_seq
    assert s.update_track_fields("t3", {"cover_art": "/api/art/t3"}) is True
    assert s.get_track("t3")["cover_art"] == "/api/art/t3"
    assert s._word_list_dirty is False
    assert (s._mutation_seq, s._catalog_seq) == (seq, cat)
    assert aof == [("update_track_fields", {"id": "t3", "data": {"cover_art": "/api/art/t3"}})]
    # The same value again: no work, no AOF record at all.
    assert s.update_track_fields("t3", {"cover_art": "/api/art/t3"}) is True
    assert len(aof) == 1 and s._mutation_seq == seq
    assert s.update_track_fields("nope", {"cover_art": "x"}) is False


@pytest.mark.parametrize("field,value", [("bitrate", 999), ("track_number", 7),
                                         ("album", "Brand New"), ("defect", "corrupt")])
def test_other_real_changes_still_bump_the_seq(field, value):
    s = _seeded_store(random.Random(9), 20)
    seq = s._mutation_seq
    s.update_track_fields("t2", {field: value})
    assert s._mutation_seq == seq + 1
    _assert_parity(s)


def test_catalog_seq_tracks_only_catalogue_fields():
    s = _seeded_store(random.Random(10), 20)
    cat = s._catalog_seq
    s.update_track_fields("t2", {"bitrate": 1})
    assert s._catalog_seq == cat
    s.update_track_fields("t2", {"track_number": 3})
    assert s._catalog_seq == cat + 1
    s.update_track_fields_batch([("t2", {"title": "Renamed"}), ("t3", {"bitrate": 2})])
    assert s._catalog_seq == cat + 2
    s.delete_track("t4")
    assert s._catalog_seq == cat + 3


def test_existing_token_does_not_dirty_the_word_list():
    s = TrackStore()
    s.upsert_tracks_batch([{"id": "a", "title": "gold", "album": ""},
                           {"id": "b", "title": "silver", "album": "gold"}])
    s._rebuild_word_list()
    s.update_track_fields("a", {"album": "gold"})      # token "gold" already a key
    assert s._word_list_dirty is False
    s.update_track_fields("a", {"album": "bronze"})    # a new key
    assert s._word_list_dirty is True
    _assert_parity(s)


def test_batch_returns_real_changes_and_journals_only_them():
    s = _seeded_store(random.Random(11), 10)
    aof: list = []
    s._aof_append = lambda op, **kw: aof.append((op, kw))
    t1 = s.get_track("t1")
    n = s.update_track_fields_batch([("t1", {"album": t1["album"]}),         # unchanged
                                     ("t2", {"album": "X", "bitrate": s.get_track("t2")["bitrate"]}),
                                     ("missing", {"album": "Y"})])
    assert n == 1
    assert aof == [("update_track_fields_batch", {"data": [{"id": "t2", "data": {"album": "X"}}]})]


def test_an_in_place_mutated_list_still_reaches_the_journal():
    s = TrackStore()
    s.upsert_track({"id": "a", "title": "t", "user_edited": ["title"]})
    aof: list = []
    s._aof_append = lambda op, **kw: aof.append((op, kw))
    ue = s.get_track("a")["user_edited"]
    ue.append("album")
    s.update_track_fields("a", {"user_edited": ue})
    assert aof and aof[0][1]["data"] == {"user_edited": ["title", "album"]}


def test_sorted_dirty_flag_stays_a_boolean_view():
    """data.rebuild_indexes / the scan-crash healer still write the boolean."""
    s = TrackStore()
    assert s._sorted_dirty is False
    s._sorted_dirty = True
    assert s._dirty_sorted == set(store_mod.SORTED_LISTS) and s._sorted_dirty is True
    s._sorted_dirty = False
    assert not s._dirty_sorted


def test_update_cost_is_flat_for_non_indexed_fields():
    """Guard against a regression to the full unindex/index path: a
    non-indexed update must not touch the word index at all."""
    s = _seeded_store(random.Random(12), 50)
    calls = []
    s._index_track = lambda *a: calls.append(a)          # type: ignore[method-assign]
    s._unindex_track = lambda *a: calls.append(a)        # type: ignore[method-assign]
    s.update_track_fields("t1", {"cover_art": "/x", "file_md5": "f" * 32, "defect": None})
    s.update_track_fields("t1", {"album": "Only Album"})
    assert calls == []


# ── record_play(at=) ─────────────────────────────────────────────────────────

def test_record_play_at_a_past_time_never_moves_last_played_backwards():
    s = TrackStore()
    s.upsert_track({"id": "a", "title": "t"})
    aof: list = []
    s._aof_append = lambda op, **kw: aof.append((op, kw))
    st = s.record_play("a", at=1600000100)
    assert st == {"count": 1, "last_played": 1600000100}
    st = s.record_play("a", at=1500000000)                # an older offline play
    assert st == {"count": 2, "last_played": 1600000100}
    assert [kw["ts"] for _op, kw in aof] == [1600000100, 1500000000]
    st = s.record_play("a", at=99999999999)                 # future → clamped to now
    assert st["count"] == 3 and st["last_played"] <= int(__import__("time").time())
    assert "a" not in s._unplayed_ids


def test_record_play_default_is_now():
    import time
    s = TrackStore()
    s.upsert_track({"id": "a", "title": "t"})
    before = int(time.time())
    assert s.record_play("a")["last_played"] >= before


def test_async_exit_does_not_run_with_an_open_outer_section():
    """Nested sections: only the outermost exit rebuilds (unchanged contract)."""
    from soniqboom.core import scanner
    s = _seeded_store(random.Random(13), 30)
    s.enter_batch_mode()
    s.enter_batch_mode()
    s.update_track_fields("t1", {"title": "zzz"})
    asyncio.run(scanner._async_exit_batch_mode(s))
    assert s._batch_mode and s._dirty_sorted == {"_sorted_title"}
    asyncio.run(scanner._async_exit_batch_mode(s))
    assert not s._batch_mode and not s._dirty_sorted
    _assert_parity(s)


def test_merge_rebuild_falls_back_when_an_old_entry_is_missing():
    """The merge locates re-keyed entries by bisect on their pre-batch key; a
    list that already drifted (entry absent) must still come out exact."""
    s = _seeded_store(random.Random(14), 40)
    s._sorted_album.remove(next(e for e in s._sorted_album if e[1] == "t5"))   # drift
    s.enter_batch_mode()
    s.update_track_fields_batch([("t5", {"album": "zzz new"}), ("t6", {"album": "aaa new"})])
    s.exit_batch_mode()
    assert s._sorted_album == sorted(s.build_sorted_list("_sorted_album"))
    assert [e for e in s._sorted_album if e[1] in ("t5", "t6")] == \
        [("aaa new", "t6"), ("zzz new", "t5")]


@pytest.mark.parametrize("seed", [21, 22])
async def test_random_patches_through_the_async_exit_keep_parity(seed):
    from soniqboom.core import scanner
    r = random.Random(seed)
    s = _seeded_store(r)
    ids = list(s._tracks)
    for _round in range(3):
        s.enter_batch_mode()
        for _ in range(120):
            tid = r.choice(ids)
            s.update_track_fields(tid, _rand_patch(r, s.get_track(tid)))
        await scanner._async_exit_batch_mode(s)
        assert not s._dirty_sorted and not s._dirty_sorted_tids and not s._batch_mode
        _assert_parity(s)
