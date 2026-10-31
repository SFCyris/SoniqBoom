# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The online-collection twins are recomputed before a duplicate re-grouping
only when they may have changed (``source_twins.refresh_async(if_stale=True)``):
a write that may change them moves the store's ``_twin_gen``
(``TrackStore._twin_touch``), and a write that no re-grouping follows still
gets them recomputed (``scanner._on_twin_dirty``)."""
from __future__ import annotations

import asyncio

import pytest

from soniqboom.core import catalogue, modland_catalog, scanner, source_twins
from soniqboom.core.store import TrackStore

ML = modland_catalog.ROOT


def _own(tid, path, **kw):
    t = {"id": tid, "path": path, "title": tid, "artist": "A", "duration": 100.0,
         "format": "ProTracker", "bitrate": 0, "added_at": 10}
    t.update(kw)
    return t


def _tune(rel, **kw):
    t = {"id": modland_catalog.track_id(rel), "path": modland_catalog.track_path(rel),
         "title": rel.rsplit("/", 1)[-1], "artist": "Somebody", "duration": 99.0,
         "format": "ProTracker", "bitrate": 0, "added_at": 1}
    t.update(kw)
    return t


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(scanner, "get_store", lambda: s)
    catalogue.register_root(ML, key="modland", label="Modland")
    source_twins.reset_for_tests()
    yield s
    source_twins.reset_for_tests()
    catalogue.unregister_root(ML)


def _moves(store, write) -> bool:
    before = store._twin_gen
    write()
    return store._twin_gen != before


def test_writes_that_may_change_the_twins_move_the_counter(store):
    tune = _tune("Protracker/X/song.mod")
    store.upsert_tracks_batch([_own("plain", "/m/plain.flac", format="FLAC"),
                               _own("own", "/m/song.mod", scene_path="Protracker/X/song.mod")])
    assert _moves(store, lambda: store.upsert_track(dict(tune)))           # a tune came
    assert _moves(store, lambda: store.update_track_fields("plain", {"file_md5": "f1"}))
    assert _moves(store, lambda: store.update_track_fields("plain", {"sid_md5": "s1"}))
    assert _moves(store, lambda: store.update_track_fields("own", {"bitrate": 128}))
    assert _moves(store, lambda: store.update_track_fields("own", {"added_at": 99}))
    assert _moves(store, lambda: store.update_track_fields(tune["id"], {"file_md5": "t1"}))
    store.update_track_fields(tune["id"], {"is_duplicate_primary": False})
    # a twin's hidden-copy mark lost (a first-play merge rewrote its row)
    assert _moves(store, lambda: store.upsert_track({**tune, "file_md5": "t1"}))
    assert _moves(store, lambda: store.delete_track("own"))                # a named twin went
    assert _moves(store, lambda: store.delete_track(tune["id"]))            # a tune went


def test_writes_that_cannot_change_the_twins_leave_it(store):
    tune = _tune("Protracker/X/song.mod")
    store.upsert_tracks_batch([_own("plain", "/m/plain.flac", format="FLAC"),
                               _own("own", "/m/song.mod", scene_path="Protracker/X/song.mod"),
                               tune])
    # a tag edit, a length fill-in, a play stat, a health mark on a twin's file
    assert not _moves(store, lambda: store.update_track_fields("own", {"title": "New"}))
    assert not _moves(store, lambda: store.update_track_fields("own", {"duration": 123.0}))
    assert not _moves(store, lambda: store.update_track_fields("own", {"health": "broken"}))
    assert not _moves(store, lambda: store.update_track_fields(
        "own", {"is_duplicate_primary": False, "duplicate_group_id": "g"}))
    # a file that names no twin
    assert not _moves(store, lambda: store.update_track_fields("plain", {"bitrate": 900}))
    assert not _moves(store, lambda: store.upsert_track(_own("new", "/m/new.flac")))
    # the re-grouping marking a twin a hidden copy
    assert not _moves(store, lambda: store.update_track_fields(
        tune["id"], {"is_duplicate_primary": False}))
    # a rescan that finds the same file
    assert not _moves(store, lambda: store.upsert_track(
        _own("own", "/m/song.mod", scene_path="Protracker/X/song.mod", title="Re")))


async def test_the_regrouping_recomputes_only_when_the_twins_may_have_changed(store, monkeypatch):
    tune = _tune("Protracker/X/song.mod")
    store.upsert_tracks_batch([_own("own", "/m/song.mod", scene_path="Protracker/X/song.mod"),
                               tune])
    calls = []
    real = source_twins.compute
    monkeypatch.setattr(source_twins, "compute", lambda s: calls.append(1) or real(s))

    async def gated():
        return await source_twins.refresh_async(store, regroup=False, if_stale=True)

    await gated()
    assert len(calls) == 1 and source_twins.own_of(tune["id"]) == "own"
    await gated()
    store.update_track_fields("own", {"title": "Edited", "duration": 101.0})
    await gated()
    assert len(calls) == 1                       # nothing they read moved
    store.upsert_track(_own("own2", "/m/b/song.mod", scene_path="Protracker/X/song.mod",
                            format="MOD", bitrate=0, added_at=1))
    await gated()
    assert len(calls) == 2                       # a second copy of the tune
    catalogue.register_root("https://elsewhere.example/x", key="t-gate", label="T")
    try:
        await gated()
        assert len(calls) == 3                   # the collections changed
    finally:
        catalogue.unregister_root("https://elsewhere.example/x")
    monkeypatch.setattr(source_twins, "_MAX_AGE_S", 0.0)
    await gated()
    assert len(calls) >= 4                       # at the latest after _MAX_AGE_S
    # the plain refresh (collection jobs, first-play merges) always computes
    monkeypatch.setattr(source_twins, "_MAX_AGE_S", 600.0)
    n = len(calls)
    await source_twins.refresh_async(store, regroup=False)
    assert len(calls) == n + 1


async def test_a_checksum_found_later_makes_a_twin_without_a_regrouping(store, monkeypatch):
    """A file's checksum written after its scan (no duplicate field changes,
    so no re-grouping follows) still makes the tune its twin."""
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    monkeypatch.setattr(scanner, "_twin_task", None)
    regroups = []
    monkeypatch.setattr(scanner, "_ensure_dup_runner", lambda: regroups.append(1))
    store._on_twin_dirty = scanner._on_twin_dirty
    tune = _tune("Protracker/Z/x.mod", file_md5="abc")
    store.upsert_tracks_batch([_own("own", "/m/x.mod"), tune])
    await source_twins.refresh_async(store, regroup=False)
    assert source_twins.own_of(tune["id"]) is None
    store.update_track_fields("own", {"file_md5": "abc"})
    task = scanner._twin_task
    assert task is not None
    await asyncio.wait_for(task, 5)
    assert source_twins.own_of(tune["id"]) == "own"
    assert regroups                              # its group takes the tune in
    # one ask per recompute: a burst of writes starts no second task
    store.update_track_fields("own", {"bitrate": 1})
    store.update_track_fields("own", {"bitrate": 2})
    assert scanner._twin_task is not task and not scanner._twin_task.done()
    t2 = scanner._twin_task
    store.update_track_fields("own", {"bitrate": 3})
    assert scanner._twin_task is t2
    await asyncio.wait_for(t2, 5)


async def test_the_recheck_asks_again_after_it_ends(store, monkeypatch):
    """However the re-check ends (fresh, its compute failing), the next write
    that may change the twins starts another."""
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    monkeypatch.setattr(scanner, "_twin_task", None)
    monkeypatch.setattr(scanner, "_ensure_dup_runner", lambda: None)
    store._on_twin_dirty = scanner._on_twin_dirty
    store.upsert_tracks_batch([_own("own", "/m/x.mod")])
    await source_twins.refresh_async(store, regroup=False)

    def broken(_s):
        raise RuntimeError("no")
    monkeypatch.setattr(source_twins, "compute", broken)
    store.update_track_fields("own", {"file_md5": "a1"})
    await asyncio.wait_for(scanner._twin_task, 5)     # (failed: logged, no retry loop)
    assert store._twin_told is False
    first = scanner._twin_task
    store.update_track_fields("own", {"file_md5": "a2"})
    assert scanner._twin_task is not first           # asked again
    scanner._twin_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scanner._twin_task
    assert store._twin_told is False
