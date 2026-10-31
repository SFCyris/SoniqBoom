# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Online-collection tunes that are files of the library ("twins",
``core/source_twins.py``): matched exactly, grouped with the library file
(which stays the primary), and played from the library's copy."""
from __future__ import annotations

import pytest

from soniqboom.core import catalogue, modland_catalog, source_twins
from soniqboom.core.duplicates import compute_duplicate_groups, primary_sort_key
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
    catalogue.register_root(ML, key="modland", label="Modland")
    source_twins.reset_for_tests()
    yield s
    source_twins.reset_for_tests()
    catalogue.unregister_root(ML)


def test_a_library_file_and_its_modland_tune_are_twins_by_scene_path(store):
    a = _tune("Protracker/X/song.mod")
    b = _tune("Protracker/X/other.mod")
    store.upsert_tracks_batch([
        _own("own1", "/m/song.mod", scene_path="Protracker/X/song.mod"),
        _own("own2", "/m/elsewhere/song.mod", scene_path="Protracker/X/song.mod", format="MOD"),
        _own("own3", "/m/nothing.mod"), a, b])
    twins = source_twins.compute(store)
    # one library copy per tune: the one primary_sort_key picks first
    best = min((store.get_track("own1"), store.get_track("own2")), key=primary_sort_key)
    assert twins == {a["id"]: best["id"]}


def test_a_played_tune_is_a_twin_by_its_file_md5(store):
    t = _tune("Protracker/Y/x.mod", file_md5="abc123")
    store.upsert_tracks_batch([_own("own1", "/m/x.mod", file_md5="abc123"),
                               _own("own2", "/m/z.mod", file_md5="zzz"), t])
    assert source_twins.compute(store) == {t["id"]: "own1"}


def test_an_hvsc_tune_is_a_twin_by_the_sid_md5(store, monkeypatch):
    from soniqboom.core import hvsc, online_hvsc
    hv = online_hvsc.HVSC_ONLINE
    monkeypatch.setattr(hv, "root", "https://hv.example/C64Music")
    catalogue.register_root(hv.root, key="hvsc-test", label="HVSC")
    try:
        rel = "MUSICIANS/H/Hubbard_Rob/Commando.sid"
        tune = {"id": hv.track_id(rel), "path": hv.track_path(rel), "title": "Commando",
                "artist": "Rob Hubbard", "duration": 200.0, "format": "SID",
                "scan_root_hash": hv.root_hash()}
        own = _own("sid1", "/m/Commando.sid", format="SID", sid_md5="m" * 32)
        store.upsert_tracks_batch([own, tune])

        class _DB:
            def path_for_md5(self, md5):
                return "/" + rel if md5 == "m" * 32 else None
        monkeypatch.setattr(hvsc, "get_hvsc", lambda: _DB())
        assert source_twins.compute(store) == {tune["id"]: "sid1"}
    finally:
        catalogue.unregister_root(hv.root)


def test_the_library_copy_is_the_primary_and_the_tune_joins_its_group():
    own = _own("own1", "/m/song.mod", added_at=50)
    tune = {**_tune("Protracker/X/song.mod", added_at=1), "twin_of": "own1"}
    catalogue.register_root(ML, key="modland", label="Modland")
    try:
        ann = compute_duplicate_groups([own, tune])
    finally:
        catalogue.unregister_root(ML)
    assert ann["own1"]["duplicate_group_id"] and \
        ann["own1"]["duplicate_group_id"] == ann[tune["id"]]["duplicate_group_id"]
    # the tune was added first, but an online collection ranks last
    assert ann["own1"]["is_duplicate_primary"] is True
    assert ann[tune["id"]]["is_duplicate_primary"] is False


def test_a_tune_joins_an_existing_title_group_of_library_copies():
    a = _own("a", "/m/a/song.mod", title="Song")
    b = _own("b", "/m/b/song.mod", title="Song", format="FLAC")
    tune = {**_tune("Protracker/X/song.mod"), "twin_of": "a"}
    catalogue.register_root(ML, key="modland", label="Modland")
    try:
        ann = compute_duplicate_groups([a, b, tune])
    finally:
        catalogue.unregister_root(ML)
    gid = ann["a"]["duplicate_group_id"]
    assert gid and ann["b"]["duplicate_group_id"] == gid == ann[tune["id"]]["duplicate_group_id"]
    assert [t for t, v in ann.items() if v["is_duplicate_primary"]] == ["b"]   # FLAC wins


@pytest.mark.asyncio
async def test_twins_are_regrouped_incrementally_and_let_go(store, monkeypatch):
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "get_store", lambda: store)
    tune = _tune("Protracker/X/song.mod")
    store.upsert_tracks_batch([_own("own1", "/m/song.mod", scene_path="Protracker/X/song.mod"),
                               tune])
    store.take_dup_dirty()
    assert source_twins.refresh(store) >= 1
    delta, _over = store.take_dup_dirty()
    assert tune["id"] in delta and "own1" in delta
    assert await scanner._run_duplicate_detection_incremental(delta)
    assert store.get_track(tune["id"])["is_duplicate_primary"] is False
    assert store.get_track("own1")["is_duplicate_primary"] is True
    gid = store.get_track("own1")["duplicate_group_id"]
    assert gid and store.get_track(tune["id"])["duplicate_group_id"] == gid
    # nothing changed: nothing to re-group
    assert source_twins.refresh(store) == 0
    # the library file goes: the tune stands on its own again
    store.delete_track("own1")
    store.take_dup_dirty()
    assert source_twins.refresh(store) >= 1
    delta, _over = store.take_dup_dirty()
    assert await scanner._run_duplicate_detection_incremental(delta)
    t = store.get_track(tune["id"])
    assert t["is_duplicate_primary"] is True and t["duplicate_group_id"] is None


def test_a_twin_plays_the_library_copy_only_while_that_can_play(store, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core import availability
    tune = _tune("Protracker/X/song.mod")
    store.upsert_tracks_batch([_own("own1", "/m/song.mod", scene_path="Protracker/X/song.mod"),
                               tune])
    source_twins.refresh(store)
    monkeypatch.setattr(availability, "offline_kinds", lambda ids, store=None: {})
    assert stream._library_twin(tune["id"]) == "own1"
    assert stream._library_twin("own1") is None
    monkeypatch.setattr(availability, "offline_kinds", lambda ids, store=None: {"own1": "drive"})
    assert stream._library_twin(tune["id"]) is None


def test_grouping_needs_no_catalogue_roots_to_rank_and_keep_tunes_apart():
    """A scan worker (macOS forkserver) has no catalogue roots: rows marked
    ``online`` / ``twin_of`` by the caller rank last and are grouped only with
    the file they are — never by their tags (QA 2026-10-28)."""
    from soniqboom.core.duplicates import compute_duplicate_groups
    own = {"id": "own", "title": "Song", "artist": "A", "duration": 100, "format": "ProTracker",
           "bitrate": 0, "added_at": 50, "path": "/music/song.mod"}
    tune = {"id": "zz-tune", "title": "Song", "artist": "A", "duration": 100, "format": "ProTracker",
            "bitrate": 0, "added_at": 1, "path": "x://not-a-registered-root/song.mod"}
    other = {**own, "id": "other", "path": "/music/other/song.mod", "added_at": 60}
    ann = compute_duplicate_groups([own, {**tune, "twin_of": "own", "online": True}])
    assert ann["own"]["is_duplicate_primary"] and not ann["zz-tune"]["is_duplicate_primary"]
    assert ann["own"]["duplicate_group_id"] == ann["zz-tune"]["duplicate_group_id"] is not None
    # a former twin (online, no twin_of) is never grouped by its tags
    ann = compute_duplicate_groups([own, other, {**tune, "online": True}])
    assert ann["zz-tune"] == {"duplicate_group_id": None, "format_score": ann["zz-tune"]["format_score"],
                              "is_duplicate_primary": True}
    assert ann["own"]["duplicate_group_id"] == ann["other"]["duplicate_group_id"] is not None


async def test_the_full_pass_agrees_and_lets_go_of_a_former_twin(store, monkeypatch):
    """The full duplicate pass runs in a scan worker (on macOS a forkserver
    one, without the catalogue roots): the library copy is still the primary,
    and a tune that stops being a twin is annotated on its own again
    (QA 2026-10-28)."""
    import asyncio
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "get_store", lambda: store)
    monkeypatch.setattr(scanner, "_pools_closed", False)
    tune = _tune("Protracker/X/song.mod")          # added before the library copy
    store.upsert_tracks_batch([_own("own1", "/m/song.mod", scene_path="Protracker/X/song.mod"),
                               tune])
    source_twins.refresh(store)
    await asyncio.wait_for(scanner._run_duplicate_detection_async(), 120)
    own, t = store.get_track("own1"), store.get_track(tune["id"])
    assert own["is_duplicate_primary"] is True and t["is_duplicate_primary"] is False
    assert own["duplicate_group_id"] and own["duplicate_group_id"] == t["duplicate_group_id"]
    store.delete_track("own1")
    source_twins.refresh(store)
    await asyncio.wait_for(scanner._run_duplicate_detection_async(), 120)
    t = store.get_track(tune["id"])
    assert t["is_duplicate_primary"] is True and t["duplicate_group_id"] is None
