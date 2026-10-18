# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fast ("capped") freshness polls of a network share find a new file two
folders deep, a same-size in-place rewrite and deletions — not only the
full-walk polls.  A folder's mtime only moves when an entry directly inside
it changes, so the fast walk keeps per-folder state from earlier walks
(core/remote_dirstate.py) instead of pruning by "this folder's mtime is old".

Runs against the WebDAV and FTP test servers (real listings of a local
folder, POSIX folder mtimes) and an in-memory source on the base-class walk
(the walk SMB uses)."""
from __future__ import annotations

import os
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402
from _webdav_server import DavServer  # noqa: E402

from soniqboom.core import scanner  # noqa: E402

try:                                         # (absent before the fix: the behaviour
    from soniqboom.core import remote_dirstate  # noqa: E402  tests still run there)
except ImportError:                          # pragma: no cover
    remote_dirstate = None
from soniqboom.core.filesource import DirEntry, FileSource  # noqa: E402

OLD = time.time() - 7200


def _mod(title: bytes, fill: int = 0) -> bytes:
    """A minimal 4-channel ProTracker module; ``fill`` varies the pattern
    bytes without changing the size."""
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 512)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, fill & 0xFF])
    return (title[:20].ljust(20, b"\0") + samples + bytes([1, 127]) + bytes(128)
            + b"M.K." + bytes(pat) + bytes(([100] * 16 + [156] * 16) * 32))


def _age(root: Path) -> None:
    for p in sorted(root.rglob("*"), key=lambda q: -len(q.parts)):
        os.utime(p, (OLD, OLD))
    os.utime(root, (OLD, OLD))


def _tree(root: Path) -> None:
    files = {
        "Artist/Album/x1.mod": b"X one",
        "Artist/Album/x3.mod": b"X three",
        "Artist2/AlbumB/z1.mod": b"Z one",
        "Artist2/AlbumB/z2.mod": b"Z two",
        "Artist2/AlbumC/c1.mod": b"C one",
        "Other/y1.mod": b"Y one",
        "Other/y2.mod": b"Y two",
    }
    for rel, title in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_mod(title))
    _age(root)


@pytest.fixture
def fresh(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _none():
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda *a, **k: None)
    if remote_dirstate is not None:
        with remote_dirstate._states_lock:
            remote_dirstate._states.clear()
    lib = tmp_path / "lib"
    lib.mkdir()
    _tree(lib)
    return lib, store_mod._store


class _Dav:
    def __init__(self, lib: Path):
        from soniqboom.core.filesource_webdav import WebDAVFileSource
        self.srv = DavServer(lib).start()
        self.root = self.srv.url
        self.src = WebDAVFileSource(self.root + "/")

    def listings(self) -> int:
        return sum(1 for m, _p, _r in self.srv.requests if m == "PROPFIND")

    def stop(self):
        self.srv.stop()


class _Ftp:
    def __init__(self, lib: Path):
        from soniqboom.core.filesource import FTPFileSource
        self.srv = FtpServer(lib.parent).start()
        self.root = f"ftp://127.0.0.1/{lib.name}"
        self.src = FTPFileSource("127.0.0.1", port=self.srv.port, remote_path=f"/{lib.name}")

    def listings(self) -> int:
        return len(self.srv.listed)

    def stop(self):
        from soniqboom.core import filesource
        with filesource._FTP_POOLS_LOCK:
            pools = [filesource._FTP_POOLS.pop(k) for k in list(filesource._FTP_POOLS)
                     if k[1] == self.srv.port]
        for p in pools:
            p.close_all()
        self.srv.stop()


@pytest.fixture(params=["webdav", "ftp"])
def share(request, fresh):
    from soniqboom.core.filesource import register_source, remove_source
    lib, store = fresh
    sh = _Dav(lib) if request.param == "webdav" else _Ftp(lib)
    register_source(sh.root, sh.src)
    try:
        yield sh, lib, store
    finally:
        remove_source(sh.root)
        sh.stop()


def _names(store) -> dict[str, dict]:
    return {t["path"].rsplit("/", 1)[-1]: t for t in store._tracks.values()}


async def _poll(sh, *, fast: bool) -> tuple[dict, int]:
    """One freshness-style poll; returns (plan, listings it made)."""
    n0 = sh.listings()
    cap = (time.time() - 15 * 60) if fast else None
    plan = await scanner.start_remote_scan("", sh.root, sh.src, dir_mtime_cap=cap)
    return plan, sh.listings() - n0


async def test_fast_polls_find_deep_new_files_inplace_edits_and_deletions(share):
    sh, lib, store = share
    plan, _ = await _poll(sh, fast=False)                  # full walk: records the folders
    assert set(_names(store)) >= {"x1.mod", "z2.mod"}
    plan, idle = await _poll(sh, fast=True)
    assert plan["extract"] == 0
    # An idle fast poll confirms no deletion (it used to report every track
    # it didn't visit as a ghost, which started the post-scan enrichment).
    assert plan["ghosts"] == 0

    # 1. A new file two folders deep; ``Artist`` itself is unchanged.
    (lib / "Artist" / "Album" / "x2.mod").write_bytes(_mod(b"X two"))
    assert os.stat(lib / "Artist").st_mtime == OLD
    plan, n = await _poll(sh, fast=True)
    assert "x2.mod" in _names(store), plan
    assert plan["new"] == 1

    # 2. A same-size in-place rewrite (no folder mtime moves).
    p = lib / "Artist2" / "AlbumC" / "c1.mod"
    before = p.stat().st_size
    with open(p, "r+b") as fh:
        fh.write(b"C renamed".ljust(20, b"\0"))
    assert p.stat().st_size == before
    assert os.stat(lib / "Artist2" / "AlbumC").st_mtime == OLD
    plan, _ = await _poll(sh, fast=True)
    assert plan["extract"] == 1, plan
    assert _names(store)["c1.mod"]["title"] == "C renamed"

    # 3. A deleted file, and a deleted folder.
    (lib / "Other" / "y2.mod").unlink()
    shutil.rmtree(lib / "Artist2" / "AlbumB")
    plan, _ = await _poll(sh, fast=True)
    names = _names(store)
    assert "y2.mod" not in names and "z1.mod" not in names and "z2.mod" not in names, plan
    assert {"y1.mod", "c1.mod", "x1.mod", "x2.mod", "x3.mod"} <= set(names)

    # An idle fast poll never lists more than a full walk (on a share this
    # small the rotation re-lists every leaf folder; see the rotation test
    # for the bound on a bigger one).
    plan_full, full = await _poll(sh, fast=False)
    plan, idle = await _poll(sh, fast=True)
    assert plan["extract"] == 0 and plan["ghosts"] == 0
    assert idle <= full


async def test_unvisited_files_are_never_deleted_by_a_fast_poll(share, monkeypatch):
    """Rotation off: a fast poll prunes leaf folders it didn't list, and the
    tracks under them are never treated as deleted."""
    sh, lib, store = share
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 0)
    monkeypatch.setattr(remote_dirstate, "_ROTATE_SQRT", 0.0)
    await _poll(sh, fast=False)
    n = len(store._tracks)
    plan, listed = await _poll(sh, fast=True)
    assert plan["walk"]["pruned"] >= 3 and plan["ghosts"] == 0
    assert len(store._tracks) == n
    # Only the root and the folders with subfolders (Artist, Artist2) were listed.
    assert listed == 3, listed


async def test_a_fast_poll_without_folder_state_walks_fully(share):
    sh, lib, store = share
    plan, _ = await _poll(sh, fast=True)                   # first walk of this process
    assert plan["walk"]["mode"] == "full" and plan["full_walk_ok"]
    plan, _ = await _poll(sh, fast=True)
    assert plan["walk"]["mode"] == "fast"


async def test_rotation_reaches_every_leaf_folder(fresh, monkeypatch):
    """A share with more leaf folders than one fast poll re-lists: an in-place
    rewrite is found within ceil(leaves / budget) fast polls, and each fast
    poll lists at most the folders with subfolders + the budget."""
    from soniqboom.core.filesource import register_source, remove_source
    lib, store = fresh
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 4)
    for i in range(12):
        d = lib / "Many" / f"Album{i:02d}"
        d.mkdir(parents=True)
        (d / "t.mod").write_bytes(_mod(b"T%d" % i))
    _age(lib)
    sh = _Dav(lib)
    register_source(sh.root, sh.src)
    try:
        await _poll(sh, fast=False)
        p = lib / "Many" / "Album11" / "t.mod"
        with open(p, "r+b") as fh:
            fh.write(b"Rewritten".ljust(20, b"\0"))
        found_at = None
        for k in range(1, 6):
            plan, listed = await _poll(sh, fast=True)
            interior = 4        # /, Artist, Artist2, Many (the rest are leaves)
            assert listed <= interior + 4, (k, listed)
            if plan["extract"]:
                found_at = k
                break
        leaves = remote_dirstate.snapshot(sh.root)["leaves"]
        assert found_at is not None and found_at <= -(-leaves // 4), (found_at, leaves)
    finally:
        remove_source(sh.root)
        sh.stop()


# ── base-class walk (the walk SMB uses), in memory ──────────────────────────

class _MemSource(FileSource):
    """A tree in memory.  ``propagate`` bumps every ancestor folder's mtime
    on a change (Nextcloud-style); otherwise only the direct parent's."""

    def __init__(self, propagate: bool):
        self.propagate = propagate
        self.files: dict[str, tuple[int, float]] = {}
        self.dir_mtime: dict[str, float] = {"/": OLD}
        self.listed: list[str] = []
        self.fail: set[str] = set()

    def _touch_dirs(self, path: str, t: float) -> None:
        d = path.rsplit("/", 1)[0] or "/"
        self.dir_mtime[d] = t
        while self.propagate and d != "/":
            d = d.rsplit("/", 1)[0] or "/"
            self.dir_mtime[d] = t

    def add(self, path: str, t: float, size: int = 100) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            d = "/" + "/".join(parts[:i])
            if d not in self.dir_mtime:
                self.dir_mtime[d] = t
                self._touch_dirs(d, t)
        self.files[path] = (size, t)
        self._touch_dirs(path, t)

    def list_dir(self, path):
        path = "/" + path.strip("/") if path.strip("/") else "/"
        self.listed.append(path)
        if path in self.fail:
            raise OSError("listing failed")
        pre = path.rstrip("/") + "/"
        out = {}
        for d, m in self.dir_mtime.items():
            if d != "/" and d.startswith(pre) and "/" not in d[len(pre):]:
                out[d] = DirEntry(d.rsplit("/", 1)[1], d, True, 0, m)
        for f, (size, m) in self.files.items():
            if f.startswith(pre) and "/" not in f[len(pre):]:
                out[f] = DirEntry(f.rsplit("/", 1)[1], f, False, size, m)
        return list(out.values())

    def walk(self, top):
        raise NotImplementedError

    def read_file(self, path, *, lane="stream"):
        raise FileNotFoundError(path)

    def stat(self, path, *, lane="scan"):
        raise FileNotFoundError(path)

    def is_dir(self, path):
        return True


def _walk(src, root, *, fast: bool):
    plan = remote_dirstate.WalkPlan(root, cap=(time.time() - 900) if fast else None)
    n0 = len(src.listed)
    entries, _p, ok, failed = scanner._find_remote_audio_entries(
        root, src, dir_mtime_cap=plan.cap if plan.fast else None,
        scan_zips=False, walk_plan=plan)
    return plan, {e.path for e in entries}, len(src.listed) - n0, failed


def _mem_tree(propagate: bool) -> _MemSource:
    src = _MemSource(propagate)
    for a in range(4):
        for b in range(3):
            src.add(f"/A{a}/B{b}/t.mod", OLD)
    return src


def test_base_walk_finds_a_deep_new_file_on_a_posix_like_server():
    root = "smb://mem/posix"
    remote_dirstate.forget(root)
    src = _mem_tree(propagate=False)
    _walk(src, root, fast=False)
    src.add("/A2/B1/new.mod", time.time())
    plan, seen, listed, _ = _walk(src, root, fast=True)
    assert plan.fast and "/A2/B1/new.mod" in seen
    assert not remote_dirstate.snapshot(root)["propagates"]
    assert remote_dirstate.snapshot(root)["prop_no"] >= 1


def test_a_propagating_server_lets_fast_walks_prune_folders_with_subfolders(monkeypatch):
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 1)
    monkeypatch.setattr(remote_dirstate, "_ROTATE_SQRT", 0.0)
    root = "smb://mem/prop"
    remote_dirstate.forget(root)
    src = _mem_tree(propagate=True)
    _walk(src, root, fast=False)
    _p, _s, listed_before, _ = _walk(src, root, fast=True)
    for i in range(remote_dirstate._PROPAGATION_MIN_YES):
        src.add(f"/A{i}/B0/n{i}.mod", time.time() - 1000 + i)   # older than the cap
        plan, seen, _l, _ = _walk(src, root, fast=True)
        assert f"/A{i}/B0/n{i}.mod" in seen
    assert remote_dirstate.snapshot(root)["propagates"]
    src.add("/A3/B2/deep.mod", time.time() - 1000)
    plan, seen, listed_after, _ = _walk(src, root, fast=True)
    assert "/A3/B2/deep.mod" in seen
    # The unchanged artists A0–A2 are no longer listed.
    assert listed_after < listed_before, (listed_before, listed_after)


def test_a_failed_listing_is_listed_again_and_confirms_no_deletion():
    root = "smb://mem/fail"
    remote_dirstate.forget(root)
    src = _mem_tree(propagate=False)
    _walk(src, root, fast=False)
    src.fail.add("/A1")
    plan, seen, _l, failed = _walk(src, root, fast=True)
    assert "/A1" in {remote_dirstate.norm(p) for p in failed}
    assert not plan.confirmed_absent("/A1/B0/t.mod")
    assert "/A1" not in plan.share.dirs                   # forgotten: listed next time
    src.fail.clear()
    src.listed.clear()
    plan, seen, _l, _ = _walk(src, root, fast=True)
    assert "/A1" in src.listed and "/A1/B0/t.mod" in seen


def test_an_empty_listing_never_confirms_a_deletion():
    root = "smb://mem/empty"
    remote_dirstate.forget(root)
    src = _mem_tree(propagate=False)
    _walk(src, root, fast=False)
    del src.files["/A0/B0/t.mod"]
    src.dir_mtime["/A0/B0"] = time.time()
    plan, _s, _l, _ = _walk(src, root, fast=True)
    assert "/A0/B0" in plan.listed
    assert not plan.confirmed_absent("/A0/B0/t.mod")      # the folder read empty
    src.files["/A0/B1/u.mod"] = (5, time.time())
    del src.files["/A0/B1/t.mod"]
    src.dir_mtime["/A0/B1"] = time.time() + 1
    plan, _s, _l, _ = _walk(src, root, fast=True)
    assert plan.confirmed_absent("/A0/B1/t.mod")
    assert not plan.confirmed_absent("/A0/B1/u.mod")


def test_folders_with_subfolders_beyond_the_budget_are_listed_in_turn(monkeypatch):
    """A share with more folders-with-subfolders than one fast walk lists:
    each fast walk stays within the budget, and a deep new file is found
    within ceil(folders / budget) fast walks."""
    monkeypatch.setattr(remote_dirstate, "_DEEP_MIN", 3)
    monkeypatch.setattr(remote_dirstate, "_DEEP_SQRT", 0.0)
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 0)
    monkeypatch.setattr(remote_dirstate, "_ROTATE_SQRT", 0.0)
    root = "smb://mem/budget"
    remote_dirstate.forget(root)
    src = _MemSource(propagate=False)
    for a in range(9):                       # 9 folders with subfolders, 18 leaves
        for b in range(2):
            src.add(f"/A{a}/B{b}/t.mod", OLD)
    _walk(src, root, fast=False)
    src.add("/A8/B1/new.mod", time.time() - 1000)        # A8 itself is unchanged
    found = None
    for k in range(1, 5):
        plan, seen, listed, _ = _walk(src, root, fast=True)
        assert listed <= 1 + 3 + 1, (k, listed)          # root + budget + the changed leaf
        if "/A8/B1/new.mod" in seen:
            found = k
            break
    assert found is not None and found <= 3, found


def test_a_shift_of_every_folder_mtime_is_no_propagation_evidence(monkeypatch):
    """An FTP switch between MLSD (seconds) and LIST (minutes) moves every
    folder's mtime in one walk: that must not switch on propagation (which
    would prune folders with subfolders again and miss deep new files)."""
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 1)
    root = "smb://mem/shift"
    remote_dirstate.forget(root)
    src = _MemSource(propagate=False)
    for a in range(5):                        # one album per artist: each artist
        src.add(f"/A{a}/B1/t.mod", OLD)       # sees exactly one changed subfolder
    _walk(src, root, fast=False)
    for d in list(src.dir_mtime):
        src.dir_mtime[d] -= 37
    _walk(src, root, fast=True)
    assert not remote_dirstate.snapshot(root)["propagates"], remote_dirstate.snapshot(root)
    for k in range(3):
        src.add(f"/A{k}/B1/new{k}.mod", time.time() - 1000)
        _plan, seen, _l, _ = _walk(src, root, fast=True)
        assert f"/A{k}/B1/new{k}.mod" in seen, k
    assert not remote_dirstate.snapshot(root)["propagates"]


def test_forgotten_folders_are_listed_by_the_next_fast_walk(monkeypatch):
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 0)
    monkeypatch.setattr(remote_dirstate, "_ROTATE_SQRT", 0.0)
    root = "smb://mem/forget"
    remote_dirstate.forget(root)
    src = _mem_tree(propagate=False)
    _walk(src, root, fast=False)
    src.add("/A1/B2/new.mod", time.time() - 1000)
    plan = remote_dirstate.WalkPlan(root, cap=time.time() - 900)
    scanner._find_remote_audio_entries(root, src, scan_zips=False, walk_plan=plan,
                                       commit_plan=False)
    plan.finish(forget=["/A1/B2"])          # its new file could not be stored
    src.listed.clear()
    _p, seen, _l, _ = _walk(src, root, fast=True)
    assert "/A1/B2" in src.listed and "/A1/B2/new.mod" in seen
    src.listed.clear()
    _walk(src, root, fast=True)
    assert "/A1/B2" not in src.listed        # recorded this time: pruned again


def test_a_folder_named_with_a_double_colon_is_not_an_archive():
    assert remote_dirstate.container("/Live :: 1998/t.mod") == "/Live :: 1998/t.mod"
    assert remote_dirstate.container("/x/pack.zip::song.mod") == "/x/pack.zip"
    root = "smb://mem/colon"
    remote_dirstate.forget(root)
    src = _MemSource(propagate=False)
    src.add("/Live :: 1998/t.mod", OLD)
    src.add("/Live/u.mod", OLD)
    _walk(src, root, fast=False)
    plan, _s, _l, _ = _walk(src, root, fast=True)
    assert not plan.confirmed_absent("/Live :: 1998/t.mod")


async def test_a_file_that_could_not_be_stored_is_retried_by_the_next_fast_poll(fresh,
                                                                               monkeypatch):
    """A new file whose download fails keeps its folder unknown: the next
    fast poll lists it again and picks the file up once it can be read
    (rotation off, folder mtime older than the poll cap: nothing else would
    list the folder)."""
    from soniqboom.core.filesource import register_source, remove_source
    monkeypatch.setattr(remote_dirstate, "_ROTATE_MIN", 0)
    monkeypatch.setattr(remote_dirstate, "_ROTATE_SQRT", 0.0)
    lib, store = fresh
    sh = _Dav(lib)
    register_source(sh.root, sh.src)
    try:
        await _poll(sh, fast=False)
        p = lib / "Artist" / "Album" / "x4.mod"
        p.write_bytes(_mod(b"X four"))
        os.chmod(p, 0)                       # listed, but every read fails
        t = time.time() - 1000
        os.utime(lib / "Artist" / "Album", (t, t))
        plan, _ = await _poll(sh, fast=True)
        # (not stored: no new track — and a passing failure is tried again)
        assert plan["new"] == 0 and "x4.mod" not in _names(store)
        os.chmod(p, 0o644)
        plan, _ = await _poll(sh, fast=True)
        assert "x4.mod" in _names(store), plan
    finally:
        remove_source(sh.root)
        sh.stop()
