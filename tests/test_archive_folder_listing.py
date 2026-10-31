# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A folder inside an archive (the folder tree lists ``…/pack.zip::inner``)
lists its tracks from the library index — it is no folder on disk."""
from __future__ import annotations

import zipfile

import pytest
from fastapi import HTTPException

from soniqboom.api import fstree
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = str(tmp_path.resolve())
    (tmp_path / "Mixed").mkdir()
    with zipfile.ZipFile(tmp_path / "Mixed" / "pack.zip", "w") as z:
        z.writestr("c.mod", b"x")
        z.writestr("inner/d.mod", b"x")
        z.writestr("inner/e.mod", b"x")
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_scan_dir(root)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO"):
        monkeypatch.setattr(fstree, name, {})
    from soniqboom.core.data import path_hash
    zp = f"{root}/Mixed/pack.zip"
    for i, member in enumerate(("c.mod", "inner/d.mod", "inner/e.mod", "inner/deep/f.mod")):
        s.upsert_track({"id": f"t{i}", "path": f"{zp}::{member}", "title": member, "format": "ProTracker",
                        "duration": 60.0, "added_at": i, "scan_root_hash": path_hash(root)})
    return root, zp


async def _ids(path):
    page = await fstree.tracks_with_meta(path=path, recursive=False, offset=0, limit=0,
                                         filter_duplicates=False, shuffle_seed=None)
    rows = page["tracks"] if isinstance(page, dict) else page
    return sorted(r["id"] for r in rows)


async def test_a_folder_inside_an_archive_lists_its_own_tracks(lib):
    root, zp = lib
    assert await _ids(f"{zp}::inner") == ["t1", "t2"]          # not deep/f.mod, not c.mod
    assert await _ids(f"{zp}::inner/deep") == ["t3"]
    assert await _ids(f"{root}/Mixed") == ["t0"]               # the archive's top: in its folder


async def test_an_archive_folder_with_nothing_indexed_is_still_not_found(lib):
    root, zp = lib
    with pytest.raises(HTTPException) as e:
        await _ids(f"{zp}::nothing")
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await _ids(f"{root}/NoSuchFolder")
    assert e.value.status_code == 404
