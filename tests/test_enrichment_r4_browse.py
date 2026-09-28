# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-4: an enrichment write refreshes the changed tracks' folder-browse
rows IN PLACE (``folder_album.refresh_album_caches``) instead of dropping every
browse cache — which made the next folder click rebuild a whole scan root on
the loop (1.6-3.6 s at 263K tracks) and the next boot rebuild the on-disk
cache (~6 s)."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from soniqboom.api import fstree
from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore


def _h(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()[:16]


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("soniqboom.config.get_data_dir", lambda: data)
    # Private browse caches for this test (the module-level ones are shared).
    for name in ("_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE", "_TRACKS_META_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(fa, "_browse_disk_stale", False)
    root = str(tmp_path / "music")
    s.upsert_scan_dir(root)
    rows = []
    for i, (d, title) in enumerate([("Game", "intro"), ("Game", "level 1"),
                                    ("Other", "tune"), ("Game", "end")]):
        rows.append({"id": f"t{i}", "path": f"{root}/{d}/{title}.mod", "title": title,
                     "artist": "", "album": "", "format": "ProTracker", "genre": ["Amiga"],
                     "scan_root_hash": _h(root), "dir_hash": _h(f"{root}/{d}"),
                     "file_md5": f"{i}" * 32})
    s.upsert_tracks_batch(rows)
    return s, root, data


def _warm(s, root):
    paths, dicts = fstree._get_or_build_scan_root_sorted(s, _h(root))
    listing = fstree._store_recursive_tracks_under(s, Path(root) / "Game")
    return dicts, listing


async def test_rows_are_patched_in_place_and_nothing_is_rebuilt(env):
    s, root, data = env
    dicts, listing = _warm(s, root)
    entry = fstree._SCAN_ROOT_FULL_CACHE[_h(root)]
    (data / fstree._BROWSE_CACHE_FILENAME).write_bytes(b"stale")
    written: list[str] = []
    await fa.commit_album_updates(
        [("t0", {"album": "Gold", "album_source": fa.SOURCE_FOLDER}, None),
         ("t1", {"album": "Gold", "album_source": fa.SOURCE_FOLDER}, None)],
        written=written)
    assert written == ["t0", "t1"]
    await fa.refresh_album_caches(written)
    # The same cache entry and row dicts serve — now with the new album.
    assert fstree._SCAN_ROOT_FULL_CACHE[_h(root)] is entry
    again = fstree._store_recursive_tracks_under(s, Path(root) / "Game")
    assert again is listing                                   # per-path cache kept
    by_id = {d["id"]: d for d in again}
    assert (by_id["t0"]["album"], by_id["t0"]["album_source"]) == ("Gold", "folder")
    assert by_id["t3"]["album"] == ""
    assert any(d is by_id["t0"] for d in dicts)
    # The disk copy (restored by a size-only check at boot) is gone.
    assert not (data / fstree._BROWSE_CACHE_FILENAME).exists()
    assert fa.browse_disk_stale()


async def test_unchanged_rows_leave_the_disk_copy_alone(env):
    s, root, data = env
    _warm(s, root)
    (data / fstree._BROWSE_CACHE_FILENAME).write_bytes(b"ok")
    await fa.refresh_album_caches(["t0", "t2"])               # nothing changed
    assert (data / fstree._BROWSE_CACHE_FILENAME).exists()
    assert not fa.browse_disk_stale()


async def test_without_a_warm_cache_nothing_is_touched(env):
    s, root, data = env
    (data / fstree._BROWSE_CACHE_FILENAME).write_bytes(b"ok")
    s.update_track_fields("t0", {"album": "Gold"})
    await fa.refresh_album_caches(["t0"])
    assert (data / fstree._BROWSE_CACHE_FILENAME).exists()
    assert fstree._SCAN_ROOT_FULL_CACHE == {}


async def test_fs_walk_listing_and_dedup_memos(env):
    s, root, _data = env
    _, listing = _warm(s, root)
    fstree._TRACKS_META_CACHE[(f"{root}/Game", False)] = {
        "mtime": 1.0, "files": [], "id_map": {"t0": Path("x")}, "results": [{"id": "t0"}]}
    fstree._TRACKS_META_CACHE[(f"{root}/Other", False)] = {
        "mtime": 1.0, "files": [], "id_map": {"t2": Path("y")}, "results": [{"id": "t2"}]}
    fstree._DEDUP_MEMO[id(listing)] = (listing, listing)
    s.update_track_fields("t0", {"album": "Gold"})
    await fa.refresh_album_caches(["t0"])
    # The FS listing holding t0 re-shapes its rows next time; the other keeps its own.
    assert "results" not in fstree._TRACKS_META_CACHE[(f"{root}/Game", False)]
    assert "results" in fstree._TRACKS_META_CACHE[(f"{root}/Other", False)]
    assert fstree._DEDUP_MEMO                                 # album-only: memo kept
    s.update_track_fields("t1", {"is_duplicate_primary": False, "duplicate_group_id": "g"})
    await fa.refresh_album_caches(["t1"])
    assert fstree._DEDUP_MEMO == {}                           # dedup input changed
    assert {d["id"]: d for d in listing}["t1"]["is_duplicate_primary"] is False


async def test_a_row_not_found_falls_back_to_a_full_drop(env):
    s, root, data = env
    _warm(s, root)
    entry = fstree._SCAN_ROOT_FULL_CACHE[_h(root)]
    i = entry["paths"].index(s.get_track("t0")["path"])
    del entry["paths"][i], entry["dicts"][i]
    s.update_track_fields("t0", {"album": "Gold"})
    await fa.refresh_album_caches(["t0"])
    assert fstree._SCAN_ROOT_FULL_CACHE == {} and fstree._STORE_RECURSIVE_CACHE == {}


# ── wiring: the Modland apply refreshes the rows it wrote ────────────────────

async def test_modland_apply_refreshes_browse_rows(env, tmp_path, monkeypatch):
    s, root, _data = env
    db = tmp_path / "modland.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.execute("INSERT INTO mods VALUES (?,?)",
                ("0" * 32, "Protracker/Someone/Gold of the Aztecs/intro.mod"))
    con.commit()
    con.close()
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_siblings_cache", None)
    monkeypatch.setitem(sm._status, "applying", False)
    _, listing = _warm(s, root)
    res = await sm.apply_to_library()
    assert res["error"] is None
    row = {d["id"]: d for d in listing}["t0"]
    assert (row["album"], row["album_source"], row["artist"]) == \
        ("Gold of the Aztecs", "modland", "Someone")
    assert fstree._SCAN_ROOT_FULL_CACHE                       # not dropped
