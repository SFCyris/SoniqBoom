# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A scan root's sorted path index (``fstree._get_or_build_scan_root_sorted``)
is also built in worker threads — by the folder tree for a share (``get_children``
→ ``_remote_children_from_store``) and by the Media Server (``dlna_upnp._root_rows``)
— while the event loop commits.  A build installs what it read only when
nothing refreshed or dropped the index in the meantime: a commit's patch
(``patch_scan_root_rows``, also one that found no index to patch), rows
re-shaped in place (``folder_album.refresh_album_caches``), a drop of the root's
index (``invalidate_scan_root``) or of every browse cache
(``invalidate_browse_cache``).  Before 2026-11-24 such a change that kept the
root's track count (a move, a re-tag) and landed during the build was lost:
every folder listing served the rows from before it until the count next
changed.

Each test holds the thread where it shapes one chosen track — its row values
and the root's track ids are read by then — runs the change on the loop, then
lets the thread finish.  (A commit's patch comes from a local scan, a folder
click's refresh, or a share scan's flush or ghost purge; the protocol is the
same for any root.)"""
from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict

import pytest

from soniqboom.api import dlna_upnp, fstree
from soniqboom.core import availability, filesource, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash
from soniqboom.core.folder_album import refresh_album_caches
from soniqboom.core.store import TrackStore

SHARE = "smb://nas.invalid/Music"


class _Stub:
    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO", "_CAT_SORTED", "_ROOT_GEN"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(fstree, "_NAT_LISTINGS", OrderedDict())
    monkeypatch.setattr(dlna_upnp, "_ROOT_IDS", {})
    shuffle_order.clear()
    availability.reset_for_tests()
    yield s
    shuffle_order.clear()
    availability.reset_for_tests()


class _Root:
    """A registered scan root holding ``Album/00.mod`` … ``Album/02.mod``: a
    network share (rows ``{root}:/rel``) or a local folder (``{root}/rel``).
    ``first`` numbers its track ids, so two roots can share a store."""

    def __init__(self, store, kind: str, tmp_path, first: int = 0):
        self.store, self.kind, self.first = store, kind, first
        if kind == "share":
            self.path, self.sep = SHARE, ":/"
            filesource.register_source(SHARE, _Stub())
        else:
            self.path, self.sep = str(tmp_path.resolve()), "/"
            (tmp_path / "Album").mkdir(exist_ok=True)
        self.hash = path_hash(self.path)
        store.upsert_scan_dir(self.path)
        for n in range(3):
            self.add(n, f"Album/0{n}.mod")

    def row_path(self, rel: str) -> str:
        return f"{self.path}{self.sep}{rel}"

    def add(self, n: int, rel: str, **extra) -> str:
        tid = f"t{self.first + n:05d}"
        self.store.upsert_track({"id": tid, "path": self.row_path(rel), "title": rel,
                                 "format": "MOD", "duration": 10.0 + n,
                                 "added_at": 1_700_000_000 + n,
                                 "scan_root_hash": self.hash, **extra})
        return tid

    def tid(self, rel: str) -> str:
        p = self.row_path(rel)
        return next(t for t, d in self.store._tracks.items() if d["path"] == p)

    def names(self) -> list[str]:
        """The rows of the root's index as the next read on the loop gets it."""
        paths, _d = fstree._get_or_build_scan_root_sorted(self.store, self.hash)
        return [p.rsplit("/", 1)[1] for p in paths]

    async def move(self, old_rel: str, new_rel: str, n: int = 900) -> None:
        """A scan commit that moves a file: one row out, one in (the count stays)."""
        s, h = self.store, self.hash
        with fstree.root_commit([h]):
            s.delete_track(self.tid(old_rel))
            new = self.add(n, new_rel)
            assert await fstree.patch_scan_root_rows(h, {self.row_path(old_rel)},
                                                     [s._tracks[new]],
                                                     len(s._tag_scan_root_hash[h]))

    async def retag(self, rel: str, album: str) -> str:
        """An enrichment write to one track, then its in-place browse refresh."""
        tid = self.tid(rel)
        self.store.update_track_fields_batch([(tid, {"album": album})])
        await refresh_album_caches([tid])
        return tid

    def close(self):
        if self.kind == "share":
            filesource.remove_source(SHARE)


@pytest.fixture(params=["share", "local"])
def root(request, store, tmp_path):
    r = _Root(store, request.param, tmp_path)
    yield r
    r.close()


@pytest.fixture
def share(store, tmp_path):
    r = _Root(store, "share", tmp_path)
    yield r
    r.close()


@pytest.fixture
def local(store, tmp_path):
    r = _Root(store, "local", tmp_path)
    yield r
    r.close()


class _Hold:
    """Stops a build in a worker thread where it shapes the row of ``path``
    (its values, and the root's track ids, are read by then) until
    ``release()`` — every such thread, or only the first (``first_only``).
    Builds on the loop thread pass."""

    def __init__(self, monkeypatch, path: str, first_only: bool = False):
        self.reached, self.go = threading.Event(), threading.Event()
        taken = threading.Lock()
        held: list = []
        real = fstree.public_track

        def make(d):
            kw = dict(d)                 # (the row's values, read before the hold)
            if (threading.current_thread() is not threading.main_thread()
                    and kw.get("path") == path):
                with taken:
                    hold = not (first_only and held)
                    held.append(1)
                if hold:
                    self.reached.set()
                    self.go.wait(10)
            return real(kw)
        monkeypatch.setattr(fstree, "public_track", make)

    async def reached_it(self) -> None:
        assert await asyncio.to_thread(self.reached.wait, 10), "the build never got there"

    def release(self) -> None:
        self.go.set()


async def _build_in_thread(root):
    return await asyncio.to_thread(fstree._get_or_build_scan_root_sorted, root.store, root.hash)


async def _own(root, rel: str = "Album") -> list[dict]:
    rows = await fstree.tracks_with_meta(path=f"{root.path}/{rel}", recursive=False, offset=0,
                                         limit=0, filter_duplicates=False, shuffle_seed=None,
                                         playable=False)
    return rows["tracks"] if isinstance(rows, dict) else rows


# ── the threaded builders as they run ────────────────────────────────────────

async def test_an_enrichment_during_the_trees_build_is_kept(share, monkeypatch):
    # The folder tree builds a share's index off the loop; an enrichment write
    # (and its in-place refresh, which found no rows to re-shape) lands meanwhile.
    hold = _Hold(monkeypatch, share.row_path("Album/01.mod"))
    tree = asyncio.ensure_future(fstree.get_children(path=SHARE, root=SHARE))
    await hold.reached_it()
    tid = await share.retag("Album/01.mod", "Re-tagged")
    hold.release()
    assert [c["name"] for c in (await tree)["children"]] == ["Album"]
    assert {r["id"]: r["album"] for r in await _own(share)}[tid] == "Re-tagged"


async def test_a_commit_during_the_media_servers_build_is_kept(local, monkeypatch):
    # The Media Server reads a folder's rows off the loop (``_root_rows``); a scan
    # commit moving 02.mod to 00b.mod lands meanwhile and finds no index to patch.
    hold = _Hold(monkeypatch, local.row_path("Album/00.mod"))
    rows = asyncio.ensure_future(asyncio.to_thread(
        dlna_upnp._root_rows, {"path": local.path, "hash": local.hash}))
    await hold.reached_it()
    await local.move("Album/02.mod", "Album/00b.mod")
    hold.release()
    paths, _ids = await rows
    assert len(paths) == 3                       # (the build still answers its own call)
    assert [r["path"].rsplit("/", 1)[1] for r in await _own(local)] == [
        "00.mod", "00b.mod", "01.mod"]


# ── a change during a build in a worker thread ───────────────────────────────

async def test_a_commit_during_a_threaded_build_is_kept(root, monkeypatch):
    hold = _Hold(monkeypatch, root.row_path("Album/00.mod"))
    built = asyncio.ensure_future(_build_in_thread(root))
    await hold.reached_it()
    await root.move("Album/02.mod", "Album/00b.mod")
    hold.release()
    paths, _dicts = await built
    assert len(paths) == 3                       # (the build still answers its own call)
    assert root.names() == ["00.mod", "00b.mod", "01.mod"]


async def test_a_retag_during_a_threaded_build_is_kept(root, monkeypatch):
    # A re-tag keeps the ids: its refresh re-shapes the cached rows in place
    # (``refresh_album_caches``) — there were none yet.
    hold = _Hold(monkeypatch, root.row_path("Album/01.mod"))
    built = asyncio.ensure_future(_build_in_thread(root))
    await hold.reached_it()
    tid = await root.retag("Album/01.mod", "Re-tagged")
    hold.release()
    await built
    _p, dicts = fstree._get_or_build_scan_root_sorted(root.store, root.hash)
    assert {d["id"]: d["album"] for d in dicts}[tid] == "Re-tagged"


@pytest.mark.parametrize("drop", ["root", "all"])
async def test_a_drop_during_a_threaded_build_is_not_undone(share, monkeypatch, drop):
    # A field-only bulk update drops the index so the next read sees it
    # (``invalidate_scan_root`` / ``invalidate_browse_cache``): a build that read
    # the old values meanwhile must not put them back.
    hold = _Hold(monkeypatch, share.row_path("Album/02.mod"))
    built = asyncio.ensure_future(_build_in_thread(share))
    await hold.reached_it()
    tid = share.tid("Album/02.mod")
    share.store.update_track_fields_batch([(tid, {"album": "NEW"})])
    if drop == "root":
        fstree.invalidate_scan_root(share.hash)
    else:
        fstree.invalidate_browse_cache(drop_disk=False)
    hold.release()
    await built
    assert {r["id"]: r["album"] for r in await _own(share)}[tid] == "NEW"


async def test_a_refresh_of_another_root_keeps_this_roots_build(share, monkeypatch, tmp_path):
    other = _Root(share.store, "local", tmp_path, first=500)
    hold = _Hold(monkeypatch, share.row_path("Album/00.mod"))
    built = asyncio.ensure_future(_build_in_thread(share))
    await hold.reached_it()
    await other.retag("Album/01.mod", "Elsewhere")
    hold.release()
    paths, _d = await built
    assert fstree._SCAN_ROOT_FULL_CACHE[share.hash]["paths"] is paths


# ── builds that nothing outdated, and their order against a commit ───────────

async def test_a_threaded_build_that_nothing_outdated_is_kept(root):
    paths, dicts = await _build_in_thread(root)                     # cold: built off the loop
    assert [p.rsplit("/", 1)[1] for p in paths] == ["00.mod", "01.mod", "02.mod"]
    again = fstree._get_or_build_scan_root_sorted(root.store, root.hash)  # warm: the kept one
    assert again[0] is paths and again[1] is dicts
    # and a commit after it is patched in, as on the loop
    await root.move("Album/01.mod", "Album/01b.mod")
    now = fstree._get_or_build_scan_root_sorted(root.store, root.hash)[0]
    assert now is not paths
    assert [p.rsplit("/", 1)[1] for p in now] == ["00.mod", "01b.mod", "02.mod"]


async def test_of_two_threaded_builds_the_one_that_finished_first_is_kept(share, monkeypatch):
    hold = _Hold(monkeypatch, share.row_path("Album/00.mod"), first_only=True)
    first = asyncio.ensure_future(_build_in_thread(share))
    await hold.reached_it()
    second_paths, _d = await _build_in_thread(share)                # passes the held one
    assert fstree._SCAN_ROOT_FULL_CACHE[share.hash]["paths"] is second_paths
    hold.release()
    first_paths, _d = await first
    assert first_paths is not second_paths
    assert fstree._get_or_build_scan_root_sorted(share.store, share.hash)[0] is second_paths


async def test_a_build_installed_between_a_commits_writes_and_its_patch_is_patched(root):
    s, h = root.store, root.hash
    with fstree.root_commit([h]):
        s.delete_track(root.tid("Album/02.mod"))
        new = root.add(900, "Album/00b.mod")
        await _build_in_thread(root)                 # (no index yet: it is built and kept)
        assert h in fstree._SCAN_ROOT_FULL_CACHE
        assert await fstree.patch_scan_root_rows(h, {root.row_path("Album/02.mod")},
                                                 [s._tracks[new]],
                                                 len(s._tag_scan_root_hash[h]))
    assert root.names() == ["00.mod", "00b.mod", "01.mod"]


async def test_a_build_of_a_half_committed_store_is_built_again(root):
    s, h = root.store, root.hash
    with fstree.root_commit([h]):
        s.delete_track(root.tid("Album/02.mod"))
        await _build_in_thread(root)                 # (between the commit's two writes)
        assert fstree._SCAN_ROOT_FULL_CACHE[h]["size"] == 2
        new = root.add(900, "Album/00b.mod")
        # The count it was built at isn't the commit's starting count: the
        # patch refuses, and the caller drops the root (as the scanner does).
        assert not await fstree.patch_scan_root_rows(h, {root.row_path("Album/02.mod")},
                                                     [s._tracks[new]],
                                                     len(s._tag_scan_root_hash[h]))
        fstree.invalidate_scan_root(h)
    assert root.names() == ["00.mod", "00b.mod", "01.mod"]


async def test_a_build_iterates_a_copy_of_each_store_row(share):
    # A build in a worker thread reads the store's live track dicts.  A field
    # write on the loop that adds a key while the build iterates one raised
    # "dictionary changed size during iteration", and the row was left out of
    # the index — for good, the count still matching.  The build copies each
    # row first (one step for the other threads); a row whose own iteration
    # raises stands in for that write.
    class _Busy(dict):
        def items(self):
            raise RuntimeError("dictionary changed size during iteration")
    tid = share.tid("Album/01.mod")
    share.store._tracks[tid] = _Busy(share.store._tracks[tid])
    paths, _dicts = await _build_in_thread(share)
    assert [p.rsplit("/", 1)[1] for p in paths] == ["00.mod", "01.mod", "02.mod"]
