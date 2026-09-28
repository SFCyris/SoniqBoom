# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-2 review fixes around re-extraction (core/repair.py):

* the one-time header-game backfill no longer marks itself done while files
  were unreachable (an offline scan root, a missing file) — it retries only
  the tracks it has not read yet;
* a re-extract never clears an artist the Modland join filled (the exact
  credit of the track's ``scene_path``) when the file names nobody;
* a re-extract diffs against the LIVE track, so a rescan + hand edit that
  lands while the extract runs is never overwritten."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import repair
from soniqboom.core.store import TrackStore, modland_artist_kept


async def _no_refresh(_ids):
    """Stand-in for ``folder_album.refresh_album_caches`` (which would touch
    the real data dir's browse cache file)."""
    return None


def _nsf(path: Path, name: bytes = b"AXEL F") -> Path:
    b = bytearray(0x80)
    b[0:5] = b"NESM\x1a"
    b[0x0E:0x0E + len(name)] = name
    b[0x2E:0x2E + 9] = b"Nullsleep"
    path.write_bytes(bytes(b) + b"\x00" * 64)
    return path


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)

    async def _nb(_p):
        return None
    monkeypatch.setattr(repair, "_broadcast_progress", _nb)
    repair._progress = repair.RepairProgress()
    repair._backfill_settled.clear()
    yield s
    repair._backfill_settled.clear()


async def _wait_repair():
    for _ in range(500):
        # the task itself done (its finish — cache refresh, then the
        # done-callbacks that settle a one-time backfill — included)
        if not repair.is_running() and (repair._task is None or repair._task.done()):
            await asyncio.sleep(0)              # let the done-callback run
            return
        await asyncio.sleep(0.01)
    raise AssertionError("repair did not finish")


def _row(tid, path, **kw):
    t = {"id": tid, "path": str(path), "title": "x", "artist": "", "album": "",
         "album_source": None, "format": "NSF"}
    t.update(kw)
    return t


# ── r2-enr-3: the one-time backfill waits for unreachable sources ────────────

async def test_unreachable_files_leave_the_marker_unset_and_retry(store, tmp_path):
    store.upsert_tracks_batch([_row(f"t{i}", tmp_path / "gone" / f"x{i}.nsf")
                               for i in range(3)])
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    # their folder is gone too: the volume, waited for (``local-offline``)
    assert repair._progress.error_reasons == {"local-offline": 3}
    assert not store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY)
    # The files come back: the next runner pass reads them and finishes.
    (tmp_path / "gone").mkdir()
    for i in range(3):
        _nsf(tmp_path / "gone" / f"x{i}.nsf")
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert {store.get_track(f"t{i}")["album"] for i in range(3)} == {"AXEL F"}


async def test_offline_scan_root_is_deferred_without_touching_its_files(store, tmp_path,
                                                                        monkeypatch):
    root = "/Volumes/OfflineNAS/Music"
    store.upsert_scan_dir(root, status="unavailable")
    store.upsert_tracks_batch([_row("a", f"{root}/a.nsf"),
                               _row("z", f"{root}/pack.zip::b.spc", format="SPC")])
    seen = []
    monkeypatch.setattr(repair, "_re_extract_local_sync",
                        lambda p, tid: seen.append(p) or (None, "local-missing"))
    assert await repair.run_album_backfill_once() is False       # nothing to start
    assert seen == [] and not repair.is_running()
    # done but for the folder away, which is remembered
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert store.get_config(repair.ALBUM_BACKFILL_WAITING_CONFIG_KEY) == [root]
    # Matched by scan_root_hash too (a path outside every root prefix).
    store.update_track_fields("a", {"path": "/elsewhere/a.nsf",
                                    "scan_root_hash": store.list_scan_dirs()[0]["path_hash"]})
    assert await repair.run_album_backfill_once() is False
    assert seen == []


async def test_mixed_online_offline_updates_online_and_keeps_waiting(store, tmp_path,
                                                                     monkeypatch):
    online = tmp_path / "online"
    online.mkdir()
    nas = tmp_path / "OfflineNAS"                     # not mounted yet
    store.upsert_scan_dir(str(online), status="ok")
    store.upsert_scan_dir(str(nas), status="unavailable")
    store.upsert_tracks_batch([
        _row("on", _nsf(online / "a.nsf")),
        _row("off", nas / "b.nsf"),
    ])
    reads = []
    real = repair._re_extract_local_sync
    monkeypatch.setattr(repair, "_re_extract_local_sync",
                        lambda p, tid: reads.append(tid) or real(p, tid))
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    assert store.get_track("on")["album"] == "AXEL F"
    assert store.get_track("off")["album"] == ""
    assert reads == ["on"]
    # Done but for the folder away, which is remembered.
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert store.get_config(repair.ALBUM_BACKFILL_WAITING_CONFIG_KEY) == [str(nas)]
    # A later pass while it is still away costs nothing; the online track is
    # not read again (only the waiting folder's tracks are candidates now).
    assert await repair.run_album_backfill_once() is False
    assert reads == ["on"]
    # The root comes back (status refreshed by its rescan): only the deferred
    # track is read, then the backfill is done for good.
    nas.mkdir()
    _nsf(nas / "b.nsf")
    store.set_scan_dir_status(str(nas), "ok")
    monkeypatch.setattr(repair, "_re_extract_local_sync",
                        lambda p, tid: reads.append(tid) or ({"album": "Game",
                                                              "album_source": "tag"}, None))
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    assert reads == ["on", "off"]
    assert store.get_track("off")["album"] == "Game"
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert store.get_config(repair.ALBUM_BACKFILL_WAITING_CONFIG_KEY) == []


async def test_a_corrupt_file_is_terminal_not_retried(store, tmp_path, monkeypatch):
    store.upsert_tracks_batch([_row("bad", tmp_path / "bad.nsf")])
    monkeypatch.setattr(repair, "_re_extract_local_sync",
                        lambda p, tid: (None, "local-error: ValueError: junk"))
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True


# ── r2-enr-4: a Modland-filled artist survives a re-extract ──────────────────

_SCENE = "Nintendo SPC/Yoko Shimomura/Super Mario RPG/01 x.spc"


def _modland_track(**kw):
    t = {"id": "s", "path": "/m/x.spc", "title": "x", "artist": "Yoko Shimomura",
         "album": "Super Mario RPG", "album_source": "modland",
         "file_md5": "a" * 32, "scene_path": _SCENE, "format": "SPC"}
    t.update(kw)
    return t


@pytest.mark.parametrize("new_kw,expect", [
    ({"artist": ""}, {}),                                     # credit kept
    ({"artist": "", "file_md5": None}, {}),                   # md5 not computed
    ({"artist": "Real Composer"}, {"artist": "Real Composer"}),   # a header artist wins
    ({"artist": "", "file_md5": "b" * 32}, {"artist": ""}),   # a different file
])
def test_re_extract_keeps_the_modland_credit(new_kw, expect):
    new = {"title": "x", "album": ""}
    new.update(new_kw)
    assert repair._changed_fields(_modland_track(), new) == expect


def test_a_non_modland_artist_is_still_cleared():
    old = _modland_track(artist="Someone Else")
    assert repair._changed_fields(old, {"artist": ""}) == {"artist": ""}
    old = _modland_track(scene_path=None)
    assert repair._changed_fields(old, {"artist": ""}) == {"artist": ""}


def test_the_rescan_carry_keeps_its_strict_md5_rule():
    old = _modland_track()
    assert modland_artist_kept(old, {"artist": "", "file_md5": "a" * 32})
    assert not modland_artist_kept(old, {"artist": "", "file_md5": None})
    assert modland_artist_kept(old, {"artist": "", "file_md5": None}, absent_md5_is_same=True)


async def test_backfill_does_not_undo_the_modland_artist_or_recompute_dups(store,
                                                                          monkeypatch):
    store.upsert_tracks_batch([_modland_track(album="", album_source=None)])
    recomputes = []

    async def _recompute():
        recomputes.append(1)
        return 0
    monkeypatch.setattr(repair, "recompute_duplicate_groups_now", _recompute)
    monkeypatch.setattr(repair, "_re_extract_local_sync",
                        lambda p, tid: ({"title": "x", "artist": "", "album": "Super Mario RPG",
                                         "album_source": "tag", "file_md5": "a" * 32}, None))
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    t = store.get_track("s")
    assert t["artist"] == "Yoko Shimomura"
    assert (t["album"], t["album_source"]) == ("Super Mario RPG", "tag")
    assert recomputes == []


# ── r2-enr-5: the diff baseline is the live track ────────────────────────────

async def _race(store, monkeypatch, process):
    base = {"id": "t1", "path": "/x/game.nsf", "title": "Song", "artist": "A",
            "album": "", "album_source": None, "format": "NSF"}
    store.upsert_track(dict(base))
    snap = store.get_track("t1")
    gate = threading.Event()
    extracted = {"title": "Song", "artist": "A", "album": "AXEL F", "album_source": "tag"}

    def fake_extract(*_a):
        gate.wait(5)
        return dict(extracted), None
    monkeypatch.setattr(repair, "_re_extract_local_sync", fake_extract)
    monkeypatch.setattr(repair, "_re_extract_remote_sync", fake_extract)
    task = asyncio.ensure_future(process(snap))
    await asyncio.sleep(0.05)
    store.upsert_tracks_batch([dict(base)])                  # a rescan replaces the dict
    store.update_track_fields("t1", {"album": "My Album", "album_source": None,
                                     "user_edited": ["album"]})
    gate.set()
    return await task


async def test_local_re_extract_never_overwrites_an_edit_made_meanwhile(store, monkeypatch):
    ok, applied, err = await _race(store, monkeypatch, repair._process_local)
    assert (ok, applied, err) == (True, False, None)
    t = store.get_track("t1")
    assert (t["album"], t["album_source"], t["user_edited"]) == ("My Album", None, ["album"])


async def test_remote_re_extract_never_overwrites_an_edit_made_meanwhile(store, monkeypatch):
    class _Src:
        def read_file(self, _p, lane=None):
            return b"NESM"
    monkeypatch.setattr("soniqboom.core.filesource.parse_remote_path",
                        lambda p: ("ftp://h/share", "/game.nsf"))
    monkeypatch.setattr("soniqboom.core.filesource.get_source", lambda root: _Src())
    ok, applied, err = await _race(store, monkeypatch,
                                   lambda t: repair._process_remote(t, None))
    assert (ok, applied, err) == (True, False, None)
    assert store.get_track("t1")["album"] == "My Album"


async def test_a_track_deleted_meanwhile_is_skipped(store, monkeypatch):
    store.upsert_track({"id": "g", "path": "/x/g.nsf", "title": "t", "album": ""})
    snap = store.get_track("g")
    gate = threading.Event()
    monkeypatch.setattr(repair, "_re_extract_local_sync",
                        lambda *_a: gate.wait(5) and ({"album": "X"}, None))
    task = asyncio.ensure_future(repair._process_local(snap))
    await asyncio.sleep(0.05)
    store.delete_track("g")
    gate.set()
    assert await task == (True, False, None)
    assert store.get_track("g") is None
