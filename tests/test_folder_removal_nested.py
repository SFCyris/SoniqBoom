# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Removing a music folder that another registered folder still covers
(nested scan roots: ``Music`` and ``Music/Box/CD2`` both registered).

A track carries the tag of ONE root, the one whose scan read it last, so the
tracks of the inner folder can be tagged with either root.  Removing (or
purging) a root never deletes a track another registered root still covers:
those tracks are handed over to the innermost root still covering them, at once
when the removal begins (Undo hands them back), and again when the root
leaves the scan list or is purged.  A root nothing else covers purges its
tracks as before."""
from __future__ import annotations

import asyncio
from collections import OrderedDict

import httpx
import pytest

from soniqboom.api import fstree
from soniqboom.core import availability, catalogue_view, removals
from soniqboom.core import store as store_mod
from soniqboom.core.data import delete_scan_dir, delete_tracks_by_scan_root, path_hash

INNER_OWN = {f"i{i}" for i in range(4)}       # the inner root's tracks
INNER_OUTER = {"m0", "m1"}                     # inside the inner root, tagged with the outer one
OUTER_ONLY = {f"o{i}" for i in range(3)}       # the outer root's, outside the inner root
SOLE = {f"s{i}" for i in range(3)}             # a separate root's


@pytest.fixture()
def lib(tmp_data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SONIQBOOM_CONF", str(tmp_data_dir / "SoniqBoom.conf"))
    s = store_mod.TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO", "_CAT_SORTED", "_ROOT_GEN"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(fstree, "_NAT_LISTINGS", OrderedDict())
    removals.reset_for_tests()
    availability.reset_for_tests()
    base = (tmp_path / "lib").resolve()
    roots = {"outer": base / "Music", "inner": base / "Music" / "Box" / "CD2",
             "sole": base / "Other"}
    for p in roots.values():
        p.mkdir(parents=True)
    outer, inner, sole = (str(roots[k]) for k in ("outer", "inner", "sole"))
    for r in (outer, inner, sole):
        s.upsert_scan_dir(r)
    rows = []

    def add(tid, path, root):
        rows.append({"id": tid, "path": path, "title": tid, "artist": "A", "album": "B",
                     "duration": 100 + len(rows), "format": "FLAC", "scan_root_hash": path_hash(root),
                     "added_at": 1_700_000_000 + len(rows)})
    for i in range(4):
        add(f"i{i}", f"{inner}/0{i}.flac", inner)
    for i in range(2):
        add(f"m{i}", f"{inner}/Disc 1/0{i}.flac", outer)
    for i in range(3):
        add(f"o{i}", f"{outer}/CD1/0{i}.flac", outer)
    for i in range(3):
        add(f"s{i}", f"{sole}/A/0{i}.flac", sole)
    s.upsert_tracks_batch(rows)
    s.rebuild_indexes()
    s.paths = {"outer": outer, "inner": inner, "sole": sole}
    yield s
    removals.reset_for_tests()
    availability.reset_for_tests()


def _tagged(s, root: str) -> set:
    return set(s.get_track_ids_for_scan_root(path_hash(root)))


async def _finished(s) -> None:
    for _ in range(250):
        if not removals.registry(s):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"removal did not finish: {removals.registry(s)}")


async def _until(cond, turns: int = 500) -> None:
    for _ in range(turns):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def _waited(cond, secs: float = 3.0) -> None:
    for _ in range(int(secs / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _hidden(s) -> frozenset:
    return frozenset(availability.hidden(s) or ())


def _gate(monkeypatch, root: str, calls: int = 1, fail: bool = False):
    """The first ``calls`` hand-overs of ``root`` each move one track, then
    pause until their gate is set (and then go on, or raise with ``fail``):
    ``[(paused, gate), …]``."""
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    real = data.hand_over_covered
    gates = [(asyncio.Event(), asyncio.Event()) for _ in range(calls)]
    seen: list = []

    async def gated(store, root_path, exclude=(), *, moved=None, keep_going=None):
        if root_path != root or len(seen) >= calls:
            return await real(store, root_path, exclude, moved=moved, keep_going=keep_going)
        paused, gate = gates[len(seen)]
        seen.append(root_path)
        moved = {} if moved is None else moved
        n0, go = len(moved), keep_going or (lambda: True)
        await real(store, root_path, exclude, moved=moved,
                   keep_going=lambda: go() and len(moved) - n0 < 1)
        paused.set()
        await gate.wait()
        if fail:
            raise RuntimeError("disk full")
        return await real(store, root_path, exclude, moved=moved, keep_going=keep_going)
    monkeypatch.setattr(data, "hand_over_covered", gated)
    return gates


async def _sides() -> None:
    """Wait for the hand-over tasks (``removals.spawn_side``) to end."""
    while removals._side:
        await asyncio.gather(*list(removals._side), return_exceptions=True)


async def _listed(folder: str) -> set:
    rows = await fstree.tracks_with_meta(path=folder, recursive=True, offset=0, limit=0,
                                         filter_duplicates=False, shuffle_seed=None,
                                         playable=False)
    return {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_purging_an_inner_root_keeps_what_the_outer_root_covers(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = set(lib._tracks)
    await removals.begin(inner)
    await _finished(lib)
    assert inner not in lib._scan_dirs
    assert set(lib._tracks) == before                               # nothing deleted
    assert not _tagged(lib, inner)
    assert _tagged(lib, outer) == INNER_OWN | INNER_OUTER | OUTER_ONLY
    assert await _listed(f"{outer}/Box") == INNER_OWN | INNER_OUTER
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_purging_an_outer_root_keeps_what_an_inner_root_covers(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await removals.begin(outer)
    await _finished(lib)
    assert outer not in lib._scan_dirs
    assert set(lib._tracks) == INNER_OWN | INNER_OUTER | SOLE        # the outer-only ones: purged
    assert _tagged(lib, inner) == INNER_OWN | INNER_OUTER            # handed to the inner root
    assert await _listed(inner) == INNER_OWN | INNER_OUTER
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_purging_a_root_nothing_else_covers_deletes_its_tracks(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    await removals.begin(lib.paths["sole"])
    await _finished(lib)
    assert set(lib._tracks) == INNER_OWN | INNER_OUTER | OUTER_ONLY
    assert not _tagged(lib, lib.paths["sole"])


@pytest.mark.asyncio
async def test_removing_both_nested_roots_deletes_all_their_tracks(lib, monkeypatch):
    # (a root being removed covers nothing)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    await removals.begin(lib.paths["inner"])
    await removals.begin(lib.paths["outer"])
    await _finished(lib)
    assert set(lib._tracks) == SOLE


@pytest.mark.asyncio
async def test_an_inner_roots_tracks_stay_listed_while_its_removal_waits(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await removals.begin(inner)
    assert inner in lib._scan_dirs                                   # (waits for its undo window)
    covered = INNER_OWN | INNER_OUTER
    assert not (availability.hidden(lib) or frozenset()) & covered   # lists, counts, searches
    assert not (availability.unplayable(lib) or frozenset()) & covered   # auto-play
    view = catalogue_view.subsonic_store(lib)                        # Subsonic / DLNA
    assert all(view.get_track(t) is not None for t in covered)
    assert await _listed(f"{outer}/Box") == covered
    [row] = removals.status(lib)
    assert row["state"] == "waiting" and row["total"] == 0 and row["left"] == 0


@pytest.mark.asyncio
async def test_undo_hands_an_inner_roots_tracks_back(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = {r: _tagged(lib, r) for r in (outer, inner)}
    await removals.begin(inner)
    assert removals.undo(inner)
    assert {r: _tagged(lib, r) for r in (outer, inner)} == before
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_an_undo_while_the_tracks_are_handed_over_puts_them_all_back(lib, monkeypatch):
    # The hand-over works in slices of the loop: an Undo can come in between.
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = {r: _tagged(lib, r) for r in (outer, inner)}
    task = asyncio.ensure_future(removals.begin(inner))
    await _until(lambda: len(_tagged(lib, inner)) < len(before[inner]))   # (some moved)
    assert _tagged(lib, inner)                                       # (not all yet)
    assert await removals.begin(inner) == removals.registry(lib)[inner]   # (one removal)
    assert removals.undo(inner)
    assert await task == {}
    assert {r: _tagged(lib, r) for r in (outer, inner)} == before
    assert not removals.registry(lib) and not removals.tasks() and inner in lib._scan_dirs
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_remove_undo_remove_while_handing_over_still_undoes_exactly(lib, monkeypatch):
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = {r: _tagged(lib, r) for r in (outer, inner)}
    first = asyncio.ensure_future(removals.begin(inner))
    await _until(lambda: len(_tagged(lib, inner)) < len(before[inner]))
    assert removals.undo(inner)
    second = asyncio.ensure_future(removals.begin(inner))
    assert await first == {}                                         # (superseded: touches nothing)
    rec = await second
    assert rec and removals.registry(lib)[inner] == rec
    assert _tagged(lib, inner) == set()                              # (all four handed over)
    assert removals.undo(inner)
    assert {r: _tagged(lib, r) for r in (outer, inner)} == before


@pytest.mark.asyncio
async def test_undoing_one_of_two_nested_removals_lists_what_the_root_covers(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await removals.begin(inner)
    await removals.begin(outer)                       # (the inner one's tracks: nothing covers them)
    assert INNER_OWN | INNER_OUTER | OUTER_ONLY <= _hidden(lib)
    assert removals.undo(inner)
    await _until(lambda: not removals._side and not (_hidden(lib) & (INNER_OWN | INNER_OUTER)))
    assert _hidden(lib) & (INNER_OWN | INNER_OUTER | OUTER_ONLY) == OUTER_ONLY
    assert _tagged(lib, inner) == INNER_OWN | INNER_OUTER            # (m0, m1 too: it covers them)
    assert removals.undo(outer)                                      # (and back as they were)
    await _until(lambda: not _hidden(lib))
    assert _tagged(lib, inner) == INNER_OWN and _tagged(lib, outer) == INNER_OUTER | OUTER_ONLY


@pytest.mark.asyncio
async def test_undoing_the_outer_of_two_nested_removals_lists_what_it_covers(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await removals.begin(outer)                       # (m0, m1: handed to the inner root)
    await removals.begin(inner)
    assert INNER_OWN | INNER_OUTER | OUTER_ONLY <= _hidden(lib)
    assert removals.undo(outer)
    await _until(lambda: not (_hidden(lib) & (INNER_OWN | INNER_OUTER | OUTER_ONLY)))
    assert _tagged(lib, outer) == INNER_OWN | INNER_OUTER | OUTER_ONLY
    [row] = removals.status(lib)
    assert row["path"] == inner and row["left"] == 0


@pytest.mark.asyncio
async def test_a_root_added_during_a_purge_keeps_what_it_covers(lib, monkeypatch):
    # The outer root is purged; meanwhile the inner folder is added as a root
    # and its scan tags some of the files with it: none of them is deleted.
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    lib.delete_scan_dir(inner)                                       # (not a root when the purge starts)
    lib.update_track_fields_batch([(t, {"scan_root_hash": path_hash(outer)}) for t in INNER_OWN])
    lib.delete_scan_dir(outer)
    purge = asyncio.ensure_future(delete_tracks_by_scan_root(outer))
    await _until(lambda: len(_tagged(lib, outer)) <= 7)              # (two deleted)
    lib.upsert_scan_dir(inner)                                       # (added again …
    lib.update_track_fields_batch([(t, {"scan_root_hash": path_hash(inner)})
                                   for t in ("i0", "i1") if t in lib._tracks])   # … its scan tags two)
    await purge
    survived = set(lib._tracks) - SOLE
    under_inner = INNER_OWN | INNER_OUTER
    assert not OUTER_ONLY & survived and not _tagged(lib, outer)    # the outer-only ones: purged
    assert survived <= under_inner and survived <= _tagged(lib, inner)   # the rest: the inner root's
    assert len(under_inner - survived) <= 2                          # (only the two before the add)


@pytest.mark.asyncio
async def test_a_superseded_begin_touches_nothing_while_the_next_one_hands_over(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = {r: _tagged(lib, r) for r in (outer, inner)}
    (p1, g1), (p2, g2) = _gate(monkeypatch, inner, calls=2)
    first = asyncio.ensure_future(removals.begin(inner))
    await p1.wait()                                                  # (one moved, paused)
    assert removals.undo(inner)
    second = asyncio.ensure_future(removals.begin(inner))
    await p2.wait()                                                  # (the next one: likewise)
    g1.set()
    assert await first == {}                                         # (superseded: it stops)
    g2.set()
    rec = await second
    assert rec and removals.registry(lib)[inner] == rec and not _tagged(lib, inner)
    assert removals.undo(inner)
    assert {r: _tagged(lib, r) for r in (outer, inner)} == before


@pytest.mark.asyncio
async def test_a_purge_waits_while_a_root_above_it_can_still_be_undone(lib, monkeypatch):
    monkeypatch.setattr(removals, "_NEIGHBOUR_POLL_S", 0.01)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    everything = INNER_OWN | INNER_OUTER | OUTER_ONLY | SOLE
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    await removals.begin(outer)                       # (m0, m1: handed to the inner root)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    await removals.begin(inner)                       # (its window ends first)
    await _waited(lambda: inner in removals._purging)
    await asyncio.sleep(0.05)
    assert set(lib._tracks) == everything             # (nothing deleted: the outer one may come back)
    assert removals.undo(outer)
    await _finished(lib)
    assert set(lib._tracks) == everything and inner not in lib._scan_dirs
    assert _tagged(lib, outer) == INNER_OWN | INNER_OUTER | OUTER_ONLY


@pytest.mark.asyncio
async def test_a_purge_waits_while_a_root_above_it_is_still_handing_over(lib, monkeypatch):
    monkeypatch.setattr(removals, "_NEIGHBOUR_POLL_S", 0.01)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    everything = INNER_OWN | INNER_OUTER | OUTER_ONLY | SOLE
    [(paused, gate)] = _gate(monkeypatch, outer)
    first = asyncio.ensure_future(removals.begin(outer))
    await paused.wait()                               # (the outer one: handing over)
    await removals.begin(inner)                       # (its window ends at once)
    await _waited(lambda: inner in removals._purging)
    await asyncio.sleep(0.05)
    assert set(lib._tracks) == everything             # (the outer one may still be undone)
    assert removals.undo(outer)
    gate.set()
    assert await first == {}
    await _finished(lib)
    assert set(lib._tracks) == everything and INNER_OWN <= _tagged(lib, outer)


@pytest.mark.asyncio
async def test_adding_a_folder_again_while_its_purge_waits_then_removing_it_again(lib, monkeypatch):
    monkeypatch.setattr(removals, "_NEIGHBOUR_POLL_S", 0.01)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    await removals.begin(outer)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    await removals.begin(inner)
    await _waited(lambda: inner in removals._purging)  # (waits for the outer one)
    removals.cancel(inner)                            # (added again: the removal is dropped …)
    assert inner not in removals.registry(lib) and inner in lib._scan_dirs
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    rec = await removals.begin(inner)                 # (… and it is removed again)
    await asyncio.sleep(0.05)
    assert rec and removals.registry(lib).get(inner) == rec


@pytest.mark.asyncio
async def test_a_root_added_while_a_purge_starts_keeps_what_it_covers(lib, monkeypatch):
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    lib.delete_scan_dir(outer)                        # (not a root when the purge starts)
    lib.delete_scan_dir(inner)
    purge = asyncio.ensure_future(delete_tracks_by_scan_root(inner))
    await asyncio.sleep(0)                            # (its first hand-over is under way)
    lib.upsert_scan_dir(outer)                        # (the outer folder is added)
    assert await purge == 0
    assert INNER_OWN <= set(lib._tracks) and INNER_OWN <= _tagged(lib, outer)


@pytest.mark.asyncio
async def test_a_folder_added_again_while_its_drop_hands_over_keeps_its_watcher(lib, monkeypatch):
    from soniqboom.core import scanner, watcher
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    inner = lib.paths["inner"]
    events: list = []
    monkeypatch.setattr(scanner, "forget_root", lambda p: events.append("forget_root"))

    async def _unwatch(_p):
        events.append("remove_root")
    monkeypatch.setattr(watcher, "remove_root", _unwatch)
    (_p1, g1), (p2, g2) = _gate(monkeypatch, inner, calls=2)   # (begin's, then the drop's)
    g1.set()
    task = asyncio.ensure_future(removals.begin(inner))
    await p2.wait()                                   # (the drop step is handing over)
    removals.cancel(inner)                            # (added again)
    lib.upsert_scan_dir(inner)
    events.append("readded")
    g2.set()
    await task
    await _finished(lib)
    assert events == ["forget_root", "remove_root", "readded"]   # (the re-add's setup stays)
    assert inner in lib._scan_dirs
    assert _tagged(lib, inner) == INNER_OWN                      # (and its tracks: handed back)


@pytest.mark.asyncio
async def test_a_failed_begin_hides_nothing_after_another_removal_listed_it(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = {r: _tagged(lib, r) for r in (outer, inner)}
    [(paused, gate)] = _gate(monkeypatch, inner, fail=True)
    task = asyncio.ensure_future(removals.begin(inner))
    await paused.wait()
    await removals.begin(lib.paths["sole"])           # (its refresh lists the inner one as removing)
    gate.set()
    with pytest.raises(RuntimeError):
        await task
    assert inner not in removals.registry(lib) and inner in lib._scan_dirs
    assert not _hidden(lib) & (INNER_OWN | INNER_OUTER) and SOLE <= _hidden(lib)
    assert {r: _tagged(lib, r) for r in (outer, inner)} == before


@pytest.mark.asyncio
async def test_a_failed_purge_hides_nothing_it_handed_over(lib, monkeypatch):
    from soniqboom.core import data
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await removals.begin(outer)                       # (m0, m1: the inner root's now)
    lib.update_track_fields_batch([("m0", {"scan_root_hash": path_hash(outer)})])   # (a scan, meanwhile)
    lib._catalog_seq += 1
    assert "m0" in (removals.hidden_ids()[0] or ())  # (joined the removal)

    async def _fails(*_a, **_k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(data, "delete_tracks_by_scan_root", _fails)
    removals._spawn(outer, 0.0)                       # (its window ends)
    await _waited(lambda: (removals.registry(lib).get(outer) or {}).get("error"))
    assert "m0" in _tagged(lib, inner) and "m0" not in _hidden(lib)


@pytest.mark.asyncio
async def test_a_purge_drops_the_cached_art_of_the_deleted_tracks_only(lib, monkeypatch):
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    dropped: list = []

    async def _record(ids, _deleted):
        dropped.extend(ids)
    monkeypatch.setattr(data, "_drop_art", _record)
    outer, sole = lib.paths["outer"], lib.paths["sole"]
    lib.delete_scan_dir(outer)
    purge = asyncio.ensure_future(delete_tracks_by_scan_root(outer))   # (m0, m1: handed over)
    await _until(lambda: len(_tagged(lib, outer)) == 2)             # (one deleted)
    keep = sorted(_tagged(lib, outer))[0]
    lib.update_track_fields_batch([(keep, {"scan_root_hash": path_hash(sole)})])   # (tagged elsewhere)
    assert await purge == 2
    assert keep in lib._tracks and keep not in dropped
    assert set(dropped) == OUTER_ONLY - {keep}


@pytest.mark.asyncio
async def test_a_hand_over_makes_the_subsonic_folders_follow(lib):
    seq = lib._catalog_seq
    assert await delete_scan_dir(lib.paths["inner"])
    await _sides()
    assert lib._catalog_seq > seq


@pytest.mark.asyncio
async def test_a_hand_over_and_a_hand_back_that_both_fail_leave_no_removal(lib, monkeypatch):
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    real, calls = lib.update_track_fields_batch, []

    def _fails_from_the_third(items):
        calls.append(items)
        if len(calls) >= 3:
            raise RuntimeError("disk full")
        return real(items)
    monkeypatch.setattr(lib, "update_track_fields_batch", _fails_from_the_third)
    with pytest.raises(RuntimeError):
        await removals.begin(lib.paths["inner"])
    assert not removals.registry(lib) and not removals.tasks()


@pytest.mark.asyncio
async def test_a_hand_over_that_fails_leaves_no_removal_behind(lib, monkeypatch):
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = {r: _tagged(lib, r) for r in (outer, inner)}
    real, calls = lib.update_track_fields_batch, []

    def _fails_on_the_third(items):
        calls.append(items)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return real(items)
    monkeypatch.setattr(lib, "update_track_fields_batch", _fails_on_the_third)
    with pytest.raises(RuntimeError):
        await removals.begin(inner)
    assert not removals.registry(lib) and not removals.tasks() and inner in lib._scan_dirs
    assert {r: _tagged(lib, r) for r in (outer, inner)} == before     # (the two moved: back)


@pytest.mark.asyncio
async def test_removing_an_inner_root_without_purge_hands_its_tracks_to_the_outer_root(lib):
    from soniqboom.api import admin, library
    import fastapi
    app = fastapi.FastAPI()
    for mod in (library, admin):
        app.include_router(mod.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    before = set(lib._tracks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.request("DELETE", "/api/admin/dirs",
                            json={"path": inner, "purge_tracks": False})
    assert r.status_code == 200, r.text
    await _sides()                                                   # (the hand-over's task)
    assert inner not in lib._scan_dirs and set(lib._tracks) == before
    assert not _tagged(lib, inner)
    assert _tagged(lib, outer) == INNER_OWN | INNER_OUTER | OUTER_ONLY
    assert await _listed(f"{outer}/Box") == INNER_OWN | INNER_OUTER


@pytest.mark.asyncio
async def test_purging_an_unlisted_root_keeps_what_another_root_covers(lib):
    # (the admin purge of a folder no longer listed: ``_bg_purge_tracks``)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    lib.delete_scan_dir(inner)                                       # (store level: tags left as they were)
    assert await delete_tracks_by_scan_root(inner) == 0
    assert set(lib._tracks) == INNER_OWN | INNER_OUTER | OUTER_ONLY | SOLE
    assert _tagged(lib, outer) == INNER_OWN | INNER_OUTER | OUTER_ONLY
    lib.delete_scan_dir(lib.paths["sole"])
    assert await delete_tracks_by_scan_root(lib.paths["sole"]) == len(SOLE)


@pytest.mark.asyncio
async def test_leaving_the_scan_list_hands_covered_tracks_over(lib):
    # ``data.delete_scan_dir``: the "this folder is no longer a root" paths
    # without a purge (Settings, the library API, the HVSC clean-up).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    assert await delete_scan_dir(inner)
    await _sides()
    assert not _tagged(lib, inner) and _tagged(lib, outer) == INNER_OWN | INNER_OUTER | OUTER_ONLY
    assert await delete_scan_dir(lib.paths["sole"])
    await _sides()
    assert _tagged(lib, lib.paths["sole"]) == SOLE                   # (nothing covers them: as before)


@pytest.mark.asyncio
async def test_leaving_the_scan_list_returns_at_once_and_a_re_add_keeps_the_tracks(lib):
    # The callers' teardown (alias, queued scans, watcher) follows the call: it
    # must not yield first, or a re-add in between would lose its set-up.
    inner = lib.paths["inner"]
    coro = delete_scan_dir(inner)
    with pytest.raises(StopIteration):
        coro.send(None)                                              # (done without yielding)
    lib.upsert_scan_dir(inner)                                       # (added again at once)
    await _sides()
    assert _tagged(lib, inner) == INNER_OWN                          # (its hand-over stopped)


@pytest.mark.asyncio
async def test_a_purge_follows_a_library_replaced_meanwhile(lib, monkeypatch):
    # A backup imported during a purge replaces the library: only tracks the
    # new one still tags with the purged root are deleted.
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 1)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    outer, sole = lib.paths["outer"], lib.paths["sole"]
    lib.delete_scan_dir(outer)
    purge = asyncio.ensure_future(delete_tracks_by_scan_root(outer))   # (m0, m1: handed over)
    await _until(lambda: len(_tagged(lib, outer)) == 2)             # (one deleted)
    imported = {tid: ({**t, "scan_root_hash": path_hash(sole)} if tid in OUTER_ONLY else dict(t))
                for tid, t in lib._tracks.items()}
    lib.bulk_load(imported, {}, {}, {}, {}, {}, dict(lib._scan_dirs), {}, {})
    lib.rebuild_indexes()
    assert await purge == 1
    assert OUTER_ONLY & set(lib._tracks) == OUTER_ONLY & set(imported)


@pytest.mark.asyncio
async def test_a_begin_stopped_at_shutdown_starts_no_new_writer(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    await removals.begin(lib.paths["sole"])                          # (another removal waits)
    [(paused, _gate_set)] = _gate(monkeypatch, lib.paths["inner"])
    task = asyncio.ensure_future(removals.begin(lib.paths["inner"]))
    await paused.wait()
    task.cancel()                                                    # (the shutdown)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lib.paths["inner"] not in removals.registry(lib)
    assert not any(t.get_name() == "removal-rebalance" for t in removals.tasks())
