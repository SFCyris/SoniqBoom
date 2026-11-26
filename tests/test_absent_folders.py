# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A library subfolder that is missing for now is not a deletion (opportunity
review v2.2.0, defect 1).

An unmounted NAS or USB drive mounted inside a library folder reads as an
empty folder (or none), while the library folder itself still lists files.
Its tracks were deleted at once, and a playlist edit made meanwhile dropped
their entries for good.  Now (``scanner._settle_orphans``) they stay in the
library, marked ``absent_since`` and listed as unavailable on a drive
(``core.availability``), until a scan lists them again — with every bit of
their user data — or a scan still finds them missing ``ABSENT_GRACE_S``
after the first one did (then they are deleted like any removed file).  A
file deleted from a folder that still holds files is deleted at once, as
before.

Generated WAV fixtures."""
from __future__ import annotations

import asyncio
import math
import os
import shutil
import struct
import time
import wave
from pathlib import Path

import pytest

from soniqboom.core import scanner


def make_wav(path: Path, freq: float, secs: float = 1.0, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(int(secs * rate))))
    return path


@pytest.fixture
def nas(tmp_path, monkeypatch, tmp_data_dir):
    """The review's repro (r5): ``lib/Local`` (a, b) and ``lib/NAS`` (c, d) —
    NAS stands for a nested mount point; ``aside`` is where its files go
    while it is "unmounted"."""
    from soniqboom.core import store as store_mod, subsonic_state
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(subsonic_state, "_state", None, raising=False)
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    base = tmp_path
    make_wav(base / "lib/Local/a.wav", 330)
    make_wav(base / "lib/Local/b.wav", 440)
    make_wav(base / "lib/NAS/c.wav", 550)
    make_wav(base / "lib/NAS/d.wav", 660)
    (base / "aside").mkdir()
    root = (base / "lib").resolve()

    def unmount():
        for f in ("c.wav", "d.wav"):
            shutil.move(str(root / "NAS" / f), str(base / "aside" / f))

    def mount():
        for f in ("c.wav", "d.wav"):
            shutil.move(str(base / "aside" / f), str(root / "NAS" / f))
    return root, store_mod._store, unmount, mount


def _ids(store) -> dict:
    return {Path(t["path"]).name: tid for tid, t in store._tracks.items()}


@pytest.mark.asyncio
async def test_an_unmounted_folder_keeps_its_tracks_and_a_playlist_edit_keeps_them(nas):
    import soniqboom.api.playlist as api_pl
    from soniqboom.core import availability
    root, store, unmount, mount = nas
    await scanner._run_scan([str(root)])
    ids = _ids(store)
    c = ids["c.wav"]
    store.set_rating(c, 5)
    store.record_play(c, uid="u1")
    store.update_track_fields(c, {"title": "My own title", "user_edited": ["title"]})
    added_c = store.get_track(c)["added_at"]
    store.create_playlist("p1", "Mix", [ids["a.wav"], c, ids["d.wav"]], owner_user_id="u1")

    unmount()
    await scanner._run_scan([str(root)])
    assert sorted(_ids(store)) == ["a.wav", "b.wav", "c.wav", "d.wav"], "the folder's tracks went"
    assert store._absent_ids == {c, ids["d.wav"]}
    # … listed as unavailable on a drive, skipped by auto-play
    snap = availability.snapshot(store)
    assert snap and snap.kind_of(store.get_track(c)) == "drive"
    assert snap.kind_of(store.get_track(ids["a.wav"])) is None
    assert {c, ids["d.wav"]} <= (availability.unplayable(store) or set())
    rows = availability.mark_rows([store.get_track(c), store.get_track(ids["a.wav"])], store)
    assert [r.get("offline") for r in rows] == ["drive", None]
    # the listener adds a track meanwhile (the web "Add to playlist")
    await api_pl.add_tracks("p1", api_pl.TrackIds(track_ids=[ids["b.wav"]]), uid="u1")
    assert [Path(store.get_track(t)["path"]).name for t in store.get_playlist("p1")["track_ids"]] \
        == ["a.wav", "c.wav", "d.wav", "b.wav"]

    mount()
    await scanner._run_scan([str(root)])
    now = _ids(store)
    assert now["c.wav"] == c and not store._absent_ids
    assert not availability.snapshot(store)
    t = store.get_track(c)
    assert store.get_rating(c) == 5 and store.get_play_stats(c, uid="u1")["count"] == 1
    assert t["title"] == "My own title" and t["user_edited"] == ["title"]
    assert t["added_at"] == added_c and not t.get("absent_since")
    assert [Path(store.get_track(x)["path"]).name for x in store.get_playlist("p1")["track_ids"]] \
        == ["a.wav", "c.wav", "d.wav", "b.wav"]
    assert store.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_a_folder_missing_past_the_grace_period_is_deleted(nas, monkeypatch):
    root, store, unmount, _mount = nas
    await scanner._run_scan([str(root)])
    c = _ids(store)["c.wav"]
    unmount()
    await scanner._run_scan([str(root)])                    # cold: first seen missing
    since = store.get_track(c)["absent_since"]
    assert since and c in store._absent_ids
    await scanner._run_scan([str(root)])                    # warm, within the period: waits
    assert c in store._tracks and store.get_track(c)["absent_since"] == since
    store.update_track_fields(c, {"absent_since": since - scanner.ABSENT_GRACE_S - 1})
    await scanner._run_scan([str(root)])                    # still missing, period over
    d = _ids(store)["d.wav"]                                # (its period isn't over)
    assert c not in store._tracks and sorted(_ids(store)) == ["a.wav", "b.wav", "d.wav"]
    assert store._absent_ids == {d}


@pytest.mark.asyncio
async def test_a_deleted_file_in_a_folder_that_holds_files_goes_at_once(nas):
    root, store, _u, _m = nas
    await scanner._run_scan([str(root)])
    (root / "NAS" / "c.wav").unlink()
    await scanner._run_scan([str(root)])
    assert sorted(_ids(store)) == ["a.wav", "b.wav", "d.wav"] and not store._absent_ids


@pytest.mark.asyncio
async def test_a_folder_check_that_hangs_keeps_the_tracks(nas, monkeypatch):
    root, store, unmount, _m = nas
    await scanner._run_scan([str(root)])
    (root / "NAS" / "c.wav").unlink()                       # would be a deletion…

    async def _hang(fn, *a, timeout):
        raise asyncio.TimeoutError
    monkeypatch.setattr(scanner, "_in_daemon_thread", _hang)
    await scanner._run_scan([str(root)])                    # … but the folder didn't answer
    assert "c.wav" in _ids(store)


@pytest.mark.asyncio
async def test_a_scoped_scan_and_a_folder_refresh_keep_a_missing_folder_too(nas):
    """The watcher's scoped scan and a folder click's refresh follow the same
    rule as a full scan (other inputs: a NAS folder that vanished whole)."""
    root, store, _u, _m = nas
    await scanner._run_scan([str(root)])
    ids = _ids(store)
    shutil.rmtree(root / "NAS")
    await scanner._run_scan([str(root)], scope={str(root): frozenset({str(root / "NAS")})})
    assert {ids["c.wav"], ids["d.wav"]} == store._absent_ids
    # a folder click's refresh of the library folder: still kept
    make_wav(root / "Local" / "e.wav", 770)
    await scanner.refresh_subtree_under_root(str(root), str(root))
    assert {"c.wav", "d.wav", "e.wav"} <= set(_ids(store))
    assert {ids["c.wav"], ids["d.wav"]} == store._absent_ids


@pytest.mark.asyncio
async def test_the_mark_is_kept_over_a_restart_and_an_index_swap(nas, tmp_data_dir):
    """``absent_since`` is an ordinary (journaled) field; the missing set
    (``store._absent_ids``) is rebuilt with the indexes."""
    from soniqboom.core import data, persistence as P, store as store_mod
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import TrackStore
    root, store, unmount, _m = nas
    w = AOFWriter(tmp_data_dir / "library.aof")
    store._aof_append = w.append
    await scanner._run_scan([str(root)])
    unmount()
    await scanner._run_scan([str(root)])
    missing = set(store._absent_ids)
    assert len(missing) == 2
    report = await data.rebuild_indexes()
    assert report["index_ok"] and not report["mismatches"] and store._absent_ids == missing
    w.flush_sync()
    w.stop()
    w.seal()
    store_mod._store = TrackStore()
    P.init_persistence(tmp_data_dir)
    assert store_mod._store._absent_ids == missing
    # (a row upserted without the field leaves the set; a field write joins it)
    tid = next(iter(missing))
    row = dict(store_mod._store.get_track(tid))
    row.pop("absent_since")
    store_mod._store.upsert_tracks_batch([row])
    assert tid not in store_mod._store._absent_ids
    store_mod._store.update_track_fields(tid, {"absent_since": int(time.time())})
    assert tid in store_mod._store._absent_ids
    store_mod._store.delete_track_ids([tid])
    assert tid not in store_mod._store._absent_ids
    assert store_mod._store.verify_indexes()["index_ok"]
