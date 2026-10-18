# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A batch section's inserts, deletes and re-keys are merged into the sorted
lists at its exit (``TrackStore.merged_sorted_list``) instead of a full
re-sort — through the sync ``exit_batch_mode`` and the scanner's yielding
``_async_exit_batch_mode`` alike, with writes landing during the latter's
yields — and the result equals a full rebuild every time.  Past the cap the
lists are rebuilt in full; a list whose recorded entries aren't where they
should be is built instead of merged."""
from __future__ import annotations

import asyncio
import copy
import random

import pytest

from soniqboom.core import scanner
from soniqboom.core import store as store_mod
from soniqboom.core.store import SORTED_LISTS, TrackStore

_TITLES = ["a", "B", "", "Pokémon x", "x  y", None, "Zork", "théme"]


def _mk(r, i, **over):
    t = {"id": f"t{i:05d}", "path": f"/r/{i}", "title": r.choice(_TITLES),
         "artist": r.choice(["A", "b", "", None, "A • B"]), "album": r.choice(["x", "Y", ""]),
         "format": r.choice(["SID", "MP3"]), "genre": r.choice([["g"], [], None]),
         "year": r.choice([None, 1999, "1987", 19950312, float("nan")]),
         "duration": r.choice([None, 0, 12.5, float("nan"), 300]),
         "added_at": r.choice([0, None, r.randint(1, 50)]), "bpm": r.choice([None, 120, "x"]),
         "is_duplicate_primary": r.choice([True, False, None]) if r.random() < .3 else True}
    t.update(over)
    return t


def _rebuild_equal(s: TrackStore) -> None:
    sh = TrackStore()
    sh._tracks = s._tracks
    sh._play_stats = s._play_stats
    sh.rebuild_indexes()
    rep = s._diff_indexes(sh)
    assert rep["index_ok"], rep["mismatches"]
    for name in SORTED_LISTS:
        assert getattr(s, name) == getattr(sh, name), name
    assert not s._dirty_sorted and not s._dirty_sorted_tids


def _ops(s, r, nxt):
    for _ in range(r.randint(1, 6)):
        ids = sorted(s._tracks)
        op = r.choice(["add", "re", "same", "del", "upd"])
        if op == "add":
            s.upsert_tracks_batch([_mk(r, nxt[0] + j) for j in range(r.randint(1, 5))])
            nxt[0] += 10
        elif op == "re" and ids:
            s.upsert_tracks_batch([_mk(r, int(t[1:])) for t in r.sample(ids, min(len(ids), 4))])
        elif op == "same" and ids:
            s.upsert_tracks_batch([dict(s._tracks[t]) for t in r.sample(ids, min(len(ids), 4))])
        elif op == "del" and ids:
            s.delete_track_ids(r.sample(ids, min(len(ids), 3)))
        elif op == "upd" and ids:
            f = r.choice(["title", "year", "duration", "added_at", "is_duplicate_primary"])
            v = {"title": r.choice(["z", "A b", None]), "year": r.choice([None, 2001, "1990"]),
                 "duration": r.choice([None, 5.5, 900]), "added_at": r.choice([None, 3, 99]),
                 "is_duplicate_primary": r.choice([True, False])}[f]
            s.update_track_fields(r.choice(ids), {f: v})


@pytest.mark.parametrize("seed", range(40))
def test_a_merged_exit_equals_a_full_rebuild(seed, monkeypatch):
    r = random.Random(seed)
    if seed % 3 == 0:
        monkeypatch.setattr(store_mod, "_SORTED_MERGE_INPLACE", 0)     # (the slice merge)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i) for i in range(r.randint(0, 200))])
    nxt = [1000]
    merged = []
    real = s.iter_merged_sorted_list

    def spy(name, changed, step=0):
        out = yield from real(name, changed, step)
        merged.append(out)
        return out
    monkeypatch.setattr(s, "iter_merged_sorted_list", spy)

    async def run():
        for _ in range(6):
            s.enter_batch_mode()
            if r.random() < 0.3:
                s.enter_batch_mode()
            _ops(s, r, nxt)
            during = r.random() < 0.3

            async def writer():
                for _ in range(3):
                    await asyncio.sleep(0)
                    _ops(s, r, nxt)
            while s._batch_depth:
                if r.random() < 0.5 or during:
                    await asyncio.gather(scanner._async_exit_batch_mode(s),
                                         writer() if during else asyncio.sleep(0))
                    during = False
                else:
                    s.exit_batch_mode()
            _rebuild_equal(s)
    asyncio.run(run())
    assert merged and not any(m is None for m in merged)      # (the merge path ran)


def test_past_the_cap_every_list_is_rebuilt(monkeypatch):
    monkeypatch.setattr(store_mod, "_SORTED_MERGE_MIN", 5)
    r = random.Random(1)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i) for i in range(100)])
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk(r, 500 + i) for i in range(20)])
    assert s._dirty_sorted == set(SORTED_LISTS) and not s._dirty_sorted_tids
    s.exit_batch_mode()
    _rebuild_equal(s)


def test_an_entry_out_of_place_makes_the_list_built_not_merged():
    r = random.Random(2)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i, title=f"title {i:03d}") for i in range(50)])
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk(r, 7, title="renamed")])
    # (the pre-batch entry is not where its recorded key says)
    i = next(i for i, e in enumerate(s._sorted_title) if e[1] == "t00007")
    s._sorted_title[i] = ("title 007 moved", "t00007")
    assert s.merged_sorted_list("_sorted_title", s.sorted_rebuild_plan()["_sorted_title"]) is None
    asyncio.run(scanner._async_exit_batch_mode(s))
    _rebuild_equal(s)


def test_a_batch_that_moves_no_key_keeps_the_lists():
    r = random.Random(3)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i) for i in range(50)])
    lists = {n: getattr(s, n) for n in SORTED_LISTS}
    s.enter_batch_mode()
    s.upsert_tracks_batch([dict(s._tracks[t]) for t in list(s._tracks)[:10]])   # identical rows
    s.update_track_fields("t00003", {"health": "broken"})                          # no sort key
    asyncio.run(scanner._async_exit_batch_mode(s))
    assert all(getattr(s, n) is lists[n] for n in SORTED_LISTS)
    _rebuild_equal(s)


def test_clearing_the_indexes_in_a_batch_rebuilds_in_full():
    r = random.Random(4)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i) for i in range(30)])
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk(r, 3)])
    s.clear_indexes()
    for tid, t in s._tracks.items():
        s._index_track(tid, t)
    s.exit_batch_mode()
    _rebuild_equal(s)


@pytest.mark.parametrize("exit_kind", ["sync", "async"])
def test_a_duplicate_the_merge_finds_is_rebuilt_away(exit_kind):
    """A stale entry for a track the batch adds again: the merge refuses it,
    and that list is rebuilt from every track — one entry, not two."""
    r = random.Random(5)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i, year=1990 + i % 20) for i in range(40)])
    s._sorted_year.append((2050, "ghost"))            # (pre-existing drift)
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk(r, 0, id="ghost", year=2050)])
    plan = s.sorted_rebuild_plan()
    assert s.merged_sorted_list("_sorted_year", plan["_sorted_year"]) is None
    if exit_kind == "sync":
        s.exit_batch_mode()
    else:
        asyncio.run(scanner._async_exit_batch_mode(s))
    assert [e for e in s._sorted_year if e[1] == "ghost"] == [(2050, "ghost")]
    _rebuild_equal(s)


@pytest.mark.parametrize("exit_kind", ["sync", "async"])
def test_a_field_batch_past_the_cap_rebuilds_only_its_list(monkeypatch, exit_kind):
    """Many tracks re-keyed in one list: that list is filtered (every entry of
    those tracks replaced, the rest kept — no merge of thousands of
    bisections), the other lists are not touched."""
    monkeypatch.setattr(store_mod, "_SORTED_MERGE_MIN", 10)
    monkeypatch.setattr(store_mod, "_SORTED_MERGE_DIV", 8)
    r = random.Random(6)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i) for i in range(120)])
    others = {n: getattr(s, n) for n in SORTED_LISTS if n != "_sorted_album"}
    kept = {e for e in s._sorted_album}
    used = []
    real_merge, real_filter = s.iter_merged_sorted_list, s.iter_filtered_sorted_list

    def merge(name, changed, step=0):
        used.append(("merge", name))
        return (yield from real_merge(name, changed, step))

    def filt(name, changed, step=0):
        used.append(("filter", name))
        return (yield from real_filter(name, changed, step))
    monkeypatch.setattr(s, "iter_merged_sorted_list", merge)
    monkeypatch.setattr(s, "iter_filtered_sorted_list", filt)
    s.enter_batch_mode()
    re_keyed = sorted(s._tracks)[:40]
    s.update_track_fields_batch([(t, {"album": f"re-keyed {i}"}) for i, t in enumerate(re_keyed)])
    assert s._dirty_sorted == {"_sorted_album"} and len(s._dirty_sorted_tids["_sorted_album"]) == 40
    assert s.sorted_merge_cap() < 40
    if exit_kind == "sync":
        s.exit_batch_mode()
    else:
        asyncio.run(scanner._async_exit_batch_mode(s))
    assert used == [("filter", "_sorted_album")]
    assert all(getattr(s, n) is lst for n, lst in others.items())    # (untouched lists kept)
    # (the untouched tracks' entries are the same tuple objects)
    same = {id(e) for e in s._sorted_album} & {id(e) for e in kept}
    assert len(same) == len(s._tracks) - 40
    _rebuild_equal(s)


def test_a_large_merge_yields_between_slices(monkeypatch):
    monkeypatch.setattr(scanner, "_MERGE_STEP", 64)
    r = random.Random(7)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i, title=f"title {i:05d}") for i in range(3000)])
    s.enter_batch_mode()
    s.update_track_fields_batch([(t, {"title": f"renamed {i:05d}"})
                                 for i, t in enumerate(sorted(s._tracks)[:1500])])
    turns = [0]

    async def run():
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                turns[0] += 1
                await asyncio.sleep(0)
        t = asyncio.create_task(ticker())
        await scanner._async_exit_batch_mode(s)
        stop.set()
        await t
    asyncio.run(run())
    assert turns[0] > 50                    # (1,500 re-keys in 64-change slices: many turns)
    _rebuild_equal(s)
