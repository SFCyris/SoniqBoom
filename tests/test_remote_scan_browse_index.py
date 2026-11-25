# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A network share's rescan keeps the share's folder index
(``fstree._SCAN_ROOT_FULL_CACHE`` — what the folder tree, the recursive
listings and a share folder's own listing read) in step with what it wrote, as
a local scan's commit does: each flush of re-read files and the ghost purge
patch it (``root_commit`` + ``scanner._refresh_browse_after_commit``).  Before
2026-11-24 a change that kept the share's track count — a file re-tagged on the
share, or moved (one add plus one ghost purge) — stayed out of the folder views
until the count next changed; after a crash the on-disk copy could bring the
old rows back at the next boot.  Real scans of the WebDAV and FTP test
servers (listings + downloads of a local folder)."""
from __future__ import annotations

import sys
import time
from collections import OrderedDict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_remote_identical_reread import (  # noqa: E402,F401  (fixtures)
    _mod, _scan, _touch, fresh, share,
)

from soniqboom.api import fstree  # noqa: E402
from soniqboom.core.data import path_hash  # noqa: E402


@pytest.fixture(autouse=True)
def _browse_caches(monkeypatch):
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO", "_CAT_SORTED", "_ROOT_GEN"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(fstree, "_NAT_LISTINGS", OrderedDict())


async def _list(path: str, recursive: bool = False) -> list[dict]:
    rows = await fstree.tracks_with_meta(path=path, recursive=recursive, offset=0, limit=0,
                                         filter_duplicates=False, shuffle_seed=None,
                                         playable=False)
    return rows["tracks"] if isinstance(rows, dict) else rows


def _rels(rows, root: str) -> list[str]:
    return [r["path"][len(root) + 2:] for r in rows]           # (after "{root}:/")


def _indexed(store, root: str) -> "list[str] | None":
    """The rows of the share's kept index, right now (None: no index kept)."""
    e = fstree._SCAN_ROOT_FULL_CACHE.get(path_hash(root))
    return None if e is None else [p[len(root) + 2:] for p in e["paths"]]


def _stored(store, root: str) -> list[str]:
    h = path_hash(root)
    return sorted(t["path"][len(root) + 2:] for t in store._tracks.values()
                  if t.get("scan_root_hash") == h)


async def test_a_retag_and_a_move_on_the_share_show_in_its_folders_after_a_rescan(share):
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    album, other = f"{sh.root}/Artist/Album", f"{sh.root}/Other"
    assert _rels(await _list(album), sh.root) == ["Artist/Album/x1.mod", "Artist/Album/x2.mod"]
    count = len(store._tag_scan_root_hash[path_hash(sh.root)])     # (the folder's index is built)
    p = lib / "Artist/Album/x2.mod"
    p.write_bytes(_mod(b"X renamed"))                 # re-tagged on the share (same size)
    _touch(p, time.time() - 60)
    (lib / "Artist/Album/x1.mod").rename(lib / "Other/x1.mod")     # moved: one add, one purge
    await _scan(sh)
    assert len(store._tag_scan_root_hash[path_hash(sh.root)]) == count     # (the count stays)
    own = await _list(album)
    assert _rels(own, sh.root) == ["Artist/Album/x2.mod"] and own[0]["title"] == "X renamed"
    assert "Other/x1.mod" in _rels(await _list(other), sh.root)
    assert _rels(await _list(f"{sh.root}/Artist", recursive=True), sh.root) == [
        "Artist/Album/x2.mod"]
    # The kept index was patched to what the store holds — no rebuild needed.
    assert _indexed(store, sh.root) == _stored(store, sh.root)


async def test_a_move_drops_the_browse_disk_copy(share):
    # A move is two writes (the add in a flush, the purge after the walk) that
    # leave the count as it was: the on-disk copy, checked by count alone at
    # the next boot, would bring the old rows back — it is dropped.
    sh, lib, store, _calls, _ends = share
    from soniqboom.config import get_data_dir
    await _scan(sh)
    await _list(f"{sh.root}/Artist/Album")
    disk = get_data_dir() / fstree._BROWSE_CACHE_FILENAME
    assert fstree._save_browse_cache(get_data_dir()) and disk.exists()
    (lib / "Artist/Album/x1.mod").rename(lib / "Other/x1.mod")
    await _scan(sh)
    assert not disk.exists()
    assert _indexed(store, sh.root) == _stored(store, sh.root)


async def test_a_new_file_and_a_removed_one_are_patched_in(share):
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    await _list(f"{sh.root}/Other")
    (lib / "Other" / "new.mod").write_bytes(_mod(b"New one"))
    (lib / "Other" / "control.mod").unlink()
    (lib / "Other" / "new2.mod").write_bytes(_mod(b"New two"))
    await _scan(sh)
    assert _indexed(store, sh.root) == _stored(store, sh.root)
    assert _rels(await _list(f"{sh.root}/Other"), sh.root) == [
        "Other/new.mod", "Other/new2.mod", "Other/y1.mod"]


def _album_titles(store) -> dict[str, str]:
    return {t["path"].rsplit("/", 1)[1]: t["title"] for t in store._tracks.values()
            if "/Artist/Album/" in t["path"]}


async def _boot_reads_the_disk_copy(store) -> None:
    """A restart without a graceful re-save: the in-memory index is gone, and
    whatever the on-disk browse copy holds (checked by count) comes back."""
    from soniqboom.config import get_data_dir
    fstree._SCAN_ROOT_FULL_CACHE.clear()
    fstree._load_browse_cache(get_data_dir(), store)


async def test_a_flush_cut_short_drops_the_index_and_the_disk_copy(share, monkeypatch):
    # A flush cut short by a write error has stored rows it can't patch in: the
    # share's index and the on-disk browse copy are dropped, so the next
    # listing — and the next boot — read the store, not the rows from before
    # (the count is the same).
    from soniqboom.config import get_data_dir
    from soniqboom.core import folder_album, scanner
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    album = f"{sh.root}/Artist/Album"
    await _list(album)                                          # (its index built)
    disk = get_data_dir() / fstree._BROWSE_CACHE_FILENAME
    assert fstree._save_browse_cache(get_data_dir()) and disk.exists()
    monkeypatch.setattr(folder_album, "_browse_disk_stale", False)
    for name, title in (("x1.mod", b"X one again"), ("x2.mod", b"X two again")):
        p = lib / "Artist/Album" / name
        p.write_bytes(_mod(title))                              # re-tagged (same size)
        _touch(p, time.time() - 60)
    monkeypatch.setattr(scanner, "WRITE_CHUNK", 1)              # (one write per track)
    real, writes = scanner.upsert_tracks_batch, []

    async def _second_fails(tracks):
        writes.append(1)
        if len(writes) == 2:
            raise RuntimeError("cut short")
        return await real(tracks)
    monkeypatch.setattr(scanner, "upsert_tracks_batch", _second_fails)
    await _scan(sh)                                             # (aborted: logged, not raised)
    assert len(writes) == 2
    stored = _album_titles(store)
    assert sorted(stored.values()) not in (["X one", "X two"],
                                           ["X one again", "X two again"])   # (one landed)
    assert not disk.exists() and folder_album.browse_disk_stale()
    listed = {r["path"].rsplit("/", 1)[1]: r["title"] for r in await _list(album)}
    assert listed == stored
    await _boot_reads_the_disk_copy(store)
    assert {r["path"].rsplit("/", 1)[1]: r["title"] for r in await _list(album)} == stored


async def test_a_cancel_during_a_flushs_patch_drops_the_index_and_the_disk_copy(share,
                                                                              monkeypatch):
    # A cancel (shutdown, the share removed) can land while a flush is being
    # patched in: here after its add went in and before its re-tag was
    # re-shaped — the count would match, the old row would be served (and
    # saved at shutdown).  The index and the disk copy are dropped instead.
    import asyncio
    from soniqboom.config import get_data_dir
    from soniqboom.core import folder_album
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    album = f"{sh.root}/Artist/Album"
    await _list(album)
    disk = get_data_dir() / fstree._BROWSE_CACHE_FILENAME
    assert fstree._save_browse_cache(get_data_dir()) and disk.exists()
    p = lib / "Artist/Album/x2.mod"
    p.write_bytes(_mod(b"X renamed"))                           # a re-tag …
    _touch(p, time.time() - 60)
    (lib / "Artist/Album/x3.mod").write_bytes(_mod(b"X three"))  # … and an add, one flush

    async def _cancelled(ids):
        raise asyncio.CancelledError()
    monkeypatch.setattr(folder_album, "refresh_album_caches", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _scan(sh)
    assert path_hash(sh.root) not in fstree._ROOTS_COMMITTING
    assert path_hash(sh.root) not in fstree._SCAN_ROOT_FULL_CACHE and not disk.exists()
    stored = _album_titles(store)
    assert stored["x2.mod"] == "X renamed" and "x3.mod" in stored
    listed = {r["path"].rsplit("/", 1)[1]: r["title"] for r in await _list(album)}
    assert listed == stored
    # and what a graceful shutdown saves is what the store holds
    assert fstree._save_browse_cache(get_data_dir())
    await _boot_reads_the_disk_copy(store)
    assert {r["path"].rsplit("/", 1)[1]: r["title"] for r in await _list(album)} == stored


async def test_a_failed_refresh_drops_the_index_and_the_disk_copy(share, monkeypatch):
    # The browse refresh after a flush catches its own errors; one that stops
    # it part-way drops the roots it was refreshing and the disk copy, so no
    # row it didn't get to is served (or saved) as it was.
    from soniqboom.config import get_data_dir
    from soniqboom.core import folder_album
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    album = f"{sh.root}/Artist/Album"
    await _list(album)
    disk = get_data_dir() / fstree._BROWSE_CACHE_FILENAME
    assert fstree._save_browse_cache(get_data_dir()) and disk.exists()
    p = lib / "Artist/Album/x2.mod"
    p.write_bytes(_mod(b"X renamed"))
    _touch(p, time.time() - 60)

    async def _fails(ids):
        raise RuntimeError("refresh failed")
    monkeypatch.setattr(folder_album, "refresh_album_caches", _fails)
    await _scan(sh)                                     # (completes: the error is logged)
    assert path_hash(sh.root) not in fstree._SCAN_ROOT_FULL_CACHE and not disk.exists()
    stored = _album_titles(store)
    assert stored["x2.mod"] == "X renamed"
    assert {r["path"].rsplit("/", 1)[1]: r["title"] for r in await _list(album)} == stored


async def test_a_move_whose_purge_is_cut_short_leaves_no_stale_disk_copy(share, monkeypatch):
    # A move's purge brings the count back to the disk copy's: the copy is
    # dropped before the delete, so a crash (here: an error) between the
    # delete and the scan's last check doesn't bring the old rows back at boot.
    from soniqboom.config import get_data_dir
    from soniqboom.core import scanner
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    await _list(f"{sh.root}/Artist/Album")
    disk = get_data_dir() / fstree._BROWSE_CACHE_FILENAME
    assert fstree._save_browse_cache(get_data_dir()) and disk.exists()
    (lib / "Artist/Album/x1.mod").rename(lib / "Other/x1.mod")
    real = scanner.delete_track_ids

    async def _then_fails(ids):
        await real(ids)
        raise RuntimeError("cut short after the delete")
    monkeypatch.setattr(scanner, "delete_track_ids", _then_fails)
    await _scan(sh)                                     # (the purge's error is logged)
    assert not disk.exists()
    names = sorted(_stored(store, sh.root))
    assert "Other/x1.mod" in names and "Artist/Album/x1.mod" not in names
    await _boot_reads_the_disk_copy(store)
    assert _rels(await _list(f"{sh.root}/Artist/Album"), sh.root) == ["Artist/Album/x2.mod"]


async def test_an_identical_reread_leaves_the_index_as_it_is(share):
    sh, lib, store, _calls, _ends = share
    await _scan(sh)
    await _list(f"{sh.root}/Artist/Album")
    kept = fstree._SCAN_ROOT_FULL_CACHE[path_hash(sh.root)]
    _touch(lib / "Artist/Album/x1.mod", time.time() - 120)        # touched: mtime only
    plan = await _scan(sh)
    assert plan.get("changed") == 0 and plan.get("unchanged_rereads") == 1, plan
    assert fstree._SCAN_ROOT_FULL_CACHE[path_hash(sh.root)] is kept
