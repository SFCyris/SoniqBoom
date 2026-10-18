# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remote shares: SMB listings, the FTP remote-path frame, archive members in
folders named with ``::``, files that can't be stored, system folders, the
fast walk's budget and round-robin, failed archive downloads, empty
listings, listing normalisation, the health probe and browse-triggered
checks.

The SMB tests run smbclient's own directory-listing code (``_scandir``,
``SMBDirEntry``) with only its network layer replaced by a local folder."""
from __future__ import annotations

import asyncio
import datetime
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402

from soniqboom.core import remote_dirstate, scanner  # noqa: E402
from soniqboom.core.filesource import DirEntry, FileSource  # noqa: E402
from test_remote_fast_walk import _mod  # noqa: E402

OLD = time.time() - 7200


def _age(root: Path, t: float = OLD) -> None:
    for p in sorted(root.rglob("*"), key=lambda q: -len(q.parts)):
        os.utime(p, (t, t), follow_symlinks=False)
    os.utime(root, (t, t))


def _zip(members: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_STORED) as z:
        for n, data in members.items():
            z.writestr(n, data)
    return b.getvalue()


@pytest.fixture
def blank(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import remote_cache
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
    with remote_dirstate._states_lock:
        remote_dirstate._states.clear()
    remote_cache.init_cache(tmp_path / "rcache", 512)
    lib = tmp_path / "lib"
    lib.mkdir()
    return lib, store_mod._store


class _Ftp:
    def __init__(self, base: Path, remote: str, **kw):
        from soniqboom.core.filesource import FTPFileSource
        self.srv = FtpServer(base, **kw).start()
        self.root = f"ftp://127.0.0.1{remote}"
        self.src = FTPFileSource("127.0.0.1", port=self.srv.port, remote_path=remote)

    def stop(self):
        from soniqboom.core import filesource
        with filesource._FTP_POOLS_LOCK:
            pools = [filesource._FTP_POOLS.pop(k) for k in list(filesource._FTP_POOLS)
                     if k[1] == self.srv.port]
        for p in pools:
            p.close_all()
        self.srv.stop()


@pytest.fixture
def ftp(blank):
    from soniqboom.core.filesource import register_source, remove_source
    lib, store = blank
    made = []

    def _make(remote: str = "/lib", **kw):
        sh = _Ftp(lib.parent, remote, **kw)
        register_source(sh.root, sh.src)
        made.append(sh)
        return sh
    yield lib, store, _make
    for sh in made:
        remove_source(sh.root)
        sh.stop()


def _rels(store) -> list[str]:
    from soniqboom.core.filesource import parse_remote_path
    return sorted(parse_remote_path(t["path"])[1] for t in store._tracks.values())


async def _poll(sh, *, fast: bool = False, auto: bool = False) -> dict:
    cap = (time.time() - 15 * 60) if fast else None
    return await scanner.start_remote_scan("", sh.root, sh.src, dir_mtime_cap=cap, auto=auto)


# ── SMB, on smbclient's own listing code ─────────────────────────────────────

class _V:
    def __init__(self, v):
        self.v = v

    def get_value(self):
        return self.v


_REPARSE = 0x400
_SYMLINK_TAG = 0xA000000C


class _SmbNet:
    """smbclient's network layer, answered from a local folder: ``root`` /
    <share> / … is the share.  ``links``: names (``/Artist/link.mod``) listed
    as symbolic links; ``link_error``: the error their stat raises."""

    def __init__(self, root: Path, share: str):
        self.root, self.share = root, share
        self.queries: list[str] = []
        self.stats: list[str] = []
        self.links: set[str] = set()
        self.link_error: BaseException | None = None

    def local(self, unc: str) -> Path:
        parts = [p for p in unc.split("\\") if p]
        return self.root.joinpath(*parts[1:])          # parts[0] = host

    def rel(self, unc: str) -> str:
        parts = [p for p in unc.split("\\") if p]
        return "/" + "/".join(parts[2:])

    def install(self, monkeypatch):
        import smbclient
        import smbclient._io as sio
        import smbclient._os as sos
        net = self

        monkeypatch.setattr(sio, "get_smb_tree", lambda path, **kw: (object(), path))
        monkeypatch.setattr(sio, "Open", lambda tree, fd_path: object())

        class _DirIO:
            def __init__(self, path, **kw):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def query_directory(self, pattern, cls):
                net.queries.append(net.rel(self.path))
                d = net.local(self.path)
                if not d.is_dir():
                    raise FileNotFoundError(2, "STATUS_OBJECT_PATH_NOT_FOUND", self.path)
                for name in (".", "..", *sorted(p.name for p in d.iterdir())):
                    p = d / name
                    st = p.stat() if name not in (".", "..") else d.stat()
                    rel = net.rel(self.path).rstrip("/") + "/" + name
                    attrs = 0x10 if p.is_dir() else 0x80
                    ea = 0
                    if rel in net.links:
                        attrs |= _REPARSE
                        ea = _SYMLINK_TAG
                    when = datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc)
                    yield {"file_name": _V(name.encode("utf-16-le")),
                           "creation_time": _V(when), "last_access_time": _V(when),
                           "last_write_time": _V(when), "change_time": _V(when),
                           "end_of_file": _V(0 if p.is_dir() else st.st_size),
                           "allocation_size": _V(4096), "file_attributes": _V(attrs),
                           "ea_size": _V(ea), "file_id": _V(1)}
        monkeypatch.setattr(sos, "SMBDirectoryIO", _DirIO)

        def _stat(path, follow_symlinks=True, **kw):
            net.stats.append(net.rel(path))
            if net.rel(path) in net.links and net.link_error is not None:
                raise net.link_error
            st = os.stat(net.local(path))
            return st
        monkeypatch.setattr(sos, "stat", _stat)
        monkeypatch.setattr(sos, "lstat", _stat)
        monkeypatch.setattr(smbclient, "open_file",
                            lambda path, mode="rb", **kw: open(net.local(path), "rb"))
        monkeypatch.setattr(smbclient, "stat", _stat)
        monkeypatch.setattr(smbclient.path, "isdir",
                            lambda path, **kw: net.local(path).is_dir())


def _smb_source(host: str, share: str):
    from soniqboom.core import filesource
    src = filesource.SMBFileSource(host, share, "u", "p")
    src._registered = True
    src._gen = filesource._SMB_SESSION_GEN.get(src._skey, 0)
    return src


@pytest.fixture
def smb(blank, monkeypatch):
    from soniqboom.core.filesource import register_source, remove_source
    lib, store = blank
    share = lib.parent / "Music"
    (share / "Artist" / "Album").mkdir(parents=True)
    (share / "Artist" / "Album" / "01.mod").write_bytes(_mod(b"One"))
    (share / "Artist" / "Album" / "02.mod").write_bytes(_mod(b"Two"))
    (share / "top.mod").write_bytes(_mod(b"Top"))
    _age(share)
    net = _SmbNet(lib.parent, "Music")
    net.install(monkeypatch)
    src = _smb_source("nas", "Music")
    root = "smb://nas/Music"
    register_source(root, src)
    yield share, store, net, src, root
    remove_source(root)


def test_smb_listing_paths_sizes_and_times_come_from_the_listing(smb):
    share, _store, net, src, _root = smb
    top = {e.name: e for e in src.list_dir("/")}
    assert {n: e.path for n, e in top.items()} == {"Artist": "/Artist", "top.mod": "/top.mod"}
    assert top["Artist"].is_dir and not top["top.mod"].is_dir
    st = (share / "top.mod").stat()
    assert top["top.mod"].size == st.st_size
    assert abs(top["top.mod"].mtime - st.st_mtime) < 1e-3
    deeper = src.list_dir("/Artist")
    assert [(e.name, e.path, e.is_dir) for e in deeper] == [("Album", "/Artist/Album", True)]
    assert sorted(e.path for e in src.list_dir("/Artist/Album")) == [
        "/Artist/Album/01.mod", "/Artist/Album/02.mod"]
    assert net.stats == []                       # no request per entry
    assert src._to_posix("\\\\nas\\Music\\\\Artist") == "/Artist"
    assert src._to_posix("\\\\nas\\Music\\Artist\\Album") == "/Artist/Album"
    assert src._to_posix("\\\\nas\\Music\\") == "/"


async def test_smb_full_scan_indexes_the_share(smb):
    from soniqboom.core.filesource import get_source
    _share, store, net, src, root = smb
    plan = await scanner.start_remote_scan("", root, get_source(root))
    assert plan["new"] == 3, plan
    assert sorted(t["path"] for t in store._tracks.values()) == [
        "smb://nas/Music:/Artist/Album/01.mod", "smb://nas/Music:/Artist/Album/02.mod",
        "smb://nas/Music:/top.mod"]
    assert sorted(net.queries) == ["/", "/Artist", "/Artist/Album"]
    # An idle re-walk keeps every track and downloads nothing.
    plan = await scanner.start_remote_scan("", root, get_source(root))
    assert plan["extract"] == 0 and plan["ghosts"] == 0 and len(store._tracks) == 3


async def test_smb_a_link_that_fails_to_stat_fails_the_listing_and_deletes_nothing(smb):
    _share, store, net, src, root = smb
    await scanner.start_remote_scan("", root, src)
    assert len(store._tracks) == 3
    os.symlink("01.mod", _share / "Artist" / "Album" / "link.mod")
    net.links = {"/Artist/Album/link.mod"}
    # The connection failing on the link's request fails the listing …
    net.link_error = ConnectionResetError(54, "connection reset (simulated)")
    with pytest.raises(OSError):
        src.list_dir("/Artist/Album")            # the listing fails — no entry dropped
    plan = await scanner.start_remote_scan("", root, src)
    assert len(store._tracks) == 3, plan          # the album's tracks are kept
    # … a link that can't be followed (refused, another server) is listed by
    # its own facts as a file — the folder lists, new files in it are found.
    from smbprotocol.exceptions import SMBLinkRedirectionError
    for err in (PermissionError(13, "STATUS_ACCESS_DENIED (simulated)"),
                SMBLinkRedirectionError("other server", "\\\\other\\x", "\\\\other\\x")):
        net.link_error = err
        got = {e.name: e for e in src.list_dir("/Artist/Album")}
        assert set(got) == {"01.mod", "02.mod", "link.mod"} and not got["link.mod"].is_dir
    (_share / "Artist" / "Album" / "03.mod").write_bytes(_mod(b"Three"))
    await scanner.start_remote_scan("", root, src)
    assert any(t["path"].endswith("/03.mod") for t in store._tracks.values())
    # A link to nothing is left out of an otherwise complete listing.
    net.link_error = FileNotFoundError(2, "STATUS_OBJECT_NAME_NOT_FOUND")
    assert sorted(e.name for e in src.list_dir("/Artist/Album")) == ["01.mod", "02.mod",
                                                                      "03.mod"]
    # A link that resolves is listed with its target's facts.
    net.link_error = None
    got = {e.name: e for e in src.list_dir("/Artist/Album")}
    assert not got["link.mod"].is_dir and got["link.mod"].size == got["01.mod"].size


async def test_smb_a_real_folder_named_like_the_share_never_hands_over_user_data(smb):
    import hashlib
    import uuid
    share, store, _net, src, root = smb
    (share / "Music" / "Artist" / "Album").mkdir(parents=True)
    other = share / "Music" / "Artist" / "Album" / "01.mod"
    other.write_bytes(_mod(b"Other one"))
    _age(share)
    await scanner.start_remote_scan("", root, src)
    deep = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root}:/Music/Artist/Album/01.mod"))
    top = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root}:/Artist/Album/01.mod"))
    store.set_rating(deep, 5)
    store.set_rating(top, 1)
    other.unlink()
    for _ in range(2):                  # (an emptied folder goes at the second walk)
        await scanner.start_remote_scan("", root, src)
    assert store.get_track(deep) is None and store.get_rating(top) == 1


async def test_smb_tracks_stored_under_a_broken_path_keep_their_user_data(smb):
    import uuid
    _share, store, _net, src, root = smb
    broken = f"{root}:/Music//top.mod"
    old_id = str(uuid.uuid5(uuid.NAMESPACE_URL, broken))
    store.upsert_tracks_batch([{"id": old_id, "path": broken, "title": "One",
                                "format": "MOD", "scan_root_hash": __import__("hashlib")
                                .sha256(root.encode()).hexdigest()[:16]}])
    store.set_rating(old_id, 5)
    pl = store.create_playlist("p", "P", [old_id])
    await scanner.start_remote_scan("", root, src)
    new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root}:/top.mod"))
    assert store.get_track(old_id) is None and store.get_track(new_id) is not None
    assert store.get_rating(new_id) == 5
    assert store.get_playlist(pl["id"])["track_ids"] in ([new_id], [{"id": new_id}])


def test_healed_smb_rel_only_touches_the_share_name_prefix():
    h = scanner._healed_smb_rel
    assert h("smb://nas/Music", "/Music//Artist/x.mod") == "/Artist/x.mod"
    assert h("smb://nas/Music", "/Music//x.mod") == "/x.mod"
    assert h("smb://nas/Music", "/Music/x.mod") is None        # a real folder "Music"
    assert h("smb://nas/Music", "/Musicals/x.mod") is None
    assert h("smb://nas/Music", "/Artist/x.mod") is None
    assert h("ftp://h/Music", "/Music//x.mod") is None


# ── FTP: the remote path is a prefix only at a path boundary ─────────────────

async def test_ftp_subfolders_named_like_the_remote_path(ftp):
    lib, store, make = ftp
    music = lib.parent / "Music"
    (music / "Music").mkdir(parents=True)
    (music / "Musicals").mkdir()
    (music / "top.mod").write_bytes(_mod(b"Top"))
    (music / "Music" / "nested.mod").write_bytes(_mod(b"Nested"))
    (music / "Musicals" / "cats.mod").write_bytes(_mod(b"Cats"))
    _age(music)
    sh = make("/Music")
    src = sh.src
    assert src._abs("/Music") == "/Music/Music"
    assert src._abs("/Musicals") == "/Music/Musicals"
    assert src._abs("/") == "/Music"
    assert src._rel("/Music/Musicals") == "/Musicals"
    assert src._rel("/Music") == "/"
    assert src._rel("/Musicals/x") == "/Musicals/x"           # not under the remote path
    walked = [(d, sorted(f.path for f in fs)) for d, _ds, fs in src.walk_with_stat("/")]
    assert sorted(walked) == [("/", ["/top.mod"]), ("/Music", ["/Music/nested.mod"]),
                              ("/Musicals", ["/Musicals/cats.mod"])]
    assert sorted(e.path for e in src.list_dir("/Music")) == ["/Music/nested.mod"]
    plan = await _poll(sh)
    assert plan["new"] == 3 and sorted(t["path"] for t in store._tracks.values()) == [
        "ftp://127.0.0.1/Music:/Music/nested.mod", "ftp://127.0.0.1/Music:/Musicals/cats.mod",
        "ftp://127.0.0.1/Music:/top.mod"]
    # Read back through the root-relative frame: the nested file, not the root.
    assert src.read_file("/Music/nested.mod")[:6] == b"Nested"


# ── "::" in folder names ─────────────────────────────────────────────────────

def test_split_member_splits_only_at_an_archive():
    from soniqboom.core.filesource import is_member_path, member_container, split_member
    assert split_member("/Artist :: Album/loose.mod") is None
    assert split_member("/Artist :: Album/pack.zip::song.mod") == (
        "/Artist :: Album/pack.zip", "song.mod")
    assert split_member("/a.zip::n.zip::b.mod") == ("/a.zip", "n.zip::b.mod")
    assert split_member("ftp://h/s:/A :: B/disk.adf::x.mod") == ("ftp://h/s:/A :: B/disk.adf",
                                                                 "x.mod")
    assert member_container("/Artist :: Album/loose.mod") == "/Artist :: Album/loose.mod"
    assert not is_member_path("/Live :: 1998/a.mod") and is_member_path("/x/A.LHA::m.mod")
    assert remote_dirstate.container("/A :: B/pack.zip::m.mod") == "/A :: B/pack.zip"
    assert remote_dirstate.container("/A :: B/loose.mod") == "/A :: B/loose.mod"


async def test_a_double_colon_folder_indexes_its_files_and_archive_members(ftp):
    lib, store, make = ftp
    d = lib / "Artist :: Album"
    d.mkdir()
    (d / "pack.zip").write_bytes(_zip({"song.mod": _mod(b"Song"), "song2.mod": _mod(b"Two")}))
    (d / "loose.mod").write_bytes(_mod(b"Loose"))
    (lib / "Other").mkdir()
    (lib / "Other" / "y.mod").write_bytes(_mod(b"Y"))
    _age(lib)
    sh = make()
    await _poll(sh)
    want = ["/Artist :: Album/loose.mod", "/Artist :: Album/pack.zip::song.mod",
            "/Artist :: Album/pack.zip::song2.mod", "/Other/y.mod"]
    assert _rels(store) == want
    await _poll(sh)                                   # full walk again: nothing deleted
    await _poll(sh, fast=True)
    assert _rels(store) == want
    # The archive can't be downloaded during a fast poll (its cached copy
    # gone): its members are kept.
    from soniqboom.core.remote_cache import get_cache
    get_cache().forget(sh.root, ["/Artist :: Album/pack.zip"], "test eviction")
    (d / "pack.zip").write_bytes(_zip({"song.mod": _mod(b"Song!"), "song2.mod": _mod(b"Two"),
                                       "x.txt": b"x"}))
    sh.srv.abort_after, sh.srv.abort_retrs = 5, 10_000
    await _poll(sh, fast=True)
    await _poll(sh)
    assert _rels(store) == want


# ── an archive whose download fails during a FULL walk ───────────────────────

async def test_a_full_walk_keeps_the_members_of_an_archive_it_could_not_download(ftp):
    lib, store, make = ftp
    (lib / "Album").mkdir()
    (lib / "Album" / "pack.zip").write_bytes(_zip({"a.mod": _mod(b"A"), "b.mod": _mod(b"B")}))
    (lib / "Other").mkdir()
    (lib / "Other" / "y.mod").write_bytes(_mod(b"Y"))
    _age(lib)
    sh = make()
    await _poll(sh)
    before = _rels(store)
    assert "/Album/pack.zip::a.mod" in before
    (lib / "Album" / "pack.zip").write_bytes(_zip({"a.mod": _mod(b"A!"), "b.mod": _mod(b"B"),
                                                   "r.txt": b"r"}))
    sh.srv.abort_after, sh.srv.abort_retrs = 5, 10_000      # every download cut
    plan = await _poll(sh)
    assert _rels(store) == before, plan
    sh.srv.abort_after = None
    await _poll(sh)                                         # read at last: re-indexed
    assert store.get_track(__import__("uuid").uuid5(
        __import__("uuid").NAMESPACE_URL, f"{sh.root}:/Album/pack.zip::a.mod").__str__())


# ── a file that can't be stored ──────────────────────────────────────────────

@pytest.fixture
def freshness(monkeypatch, tmp_path):
    from soniqboom.core import remote_freshness as rf
    monkeypatch.setattr(rf._reg, "states", {})
    monkeypatch.setattr(rf._reg, "inflight", {})
    monkeypatch.setattr(rf._reg, "state_file", tmp_path / "freshness_state.json")
    toasts: list = []

    async def _cb(root, n, _t):
        toasts.append(n)
    monkeypatch.setattr(rf._reg, "on_new_tracks", _cb)
    return rf, toasts


async def test_a_file_whose_download_keeps_failing_waits_until_it_changes(ftp, freshness):
    """The test server cuts the transfer of an unreadable file (426): a
    passing failure — tried by the next polls, then left alone once it failed
    ``_TRANSIENT_GIVEUPS`` times in a row.  Never a new track, never a
    notice, never a faster poll."""
    rf, toasts = freshness
    lib, store, make = ftp
    for a in range(2):
        (lib / f"A{a}" / "Album").mkdir(parents=True)
        (lib / f"A{a}" / "Album" / "01.mod").write_bytes(_mod(b"ok %d" % a))
    bad = lib / "A1" / "Album" / "02.mod"
    bad.write_bytes(_mod(b"unreadable"))
    os.chmod(bad, 0)
    _age(lib)
    sh = make()
    rf._reg.source_lookup = lambda root: sh.src
    try:
        rows = []
        for _ in range(8):
            r0 = len(sh.srv.retrs)
            plan = await rf._poll_share(sh.root, reason="tick")
            rows.append((plan.get("new"), sum(1 for p, _o in sh.srv.retrs[r0:] if "02.mod" in p)))
        st = rf._reg.states[sh.root]
        k = scanner._TRANSIENT_GIVEUPS
        assert rows[0][0] == 2 and all(r[0] == 0 for r in rows[1:]), rows   # never "new"
        assert all(r[1] > 0 for r in rows[:k]), rows               # tried …
        assert all(r[1] == 0 for r in rows[k:]), rows               # … then left alone
        assert toasts == [2]
        assert st.adaptive_interval() >= 30 * 60   # the poll interval did not shorten
        # A manual scan tries it again; a change of the file tries it at once.
        r0 = len(sh.srv.retrs)
        await _poll(sh)
        assert sum(1 for p, _o in sh.srv.retrs[r0:] if "02.mod" in p) > 0
        os.chmod(bad, 0o644)
        bad.write_bytes(_mod(b"readable now") + b"\0" * 64)
        os.utime(bad, (OLD, OLD))
        os.utime(bad.parent, (time.time(), time.time()))
        plan = await rf._poll_share(sh.root, reason="tick")
        assert "/A1/Album/02.mod" in _rels(store), plan
        assert store.get_config(scanner._REMOTE_GIVEUP_KEY + __import__("hashlib")
                                .sha256(sh.root.encode()).hexdigest()[:16]) is None
    finally:
        os.chmod(bad, 0o644)


async def test_a_file_the_server_refuses_is_tried_once_then_waits(blank, freshness):
    """FTP 550 for one file: one RETR (no retry of a refusal), then nothing
    until the file changes."""
    import ftplib
    from soniqboom.core.filesource import register_source, remove_source
    rf, toasts = freshness
    _lib, store = blank

    class _Src(_Mem):
        reads: list = []

        def read_file(self, path, *, lane="stream"):
            type(self).reads.append(path)
            if path.endswith("/A001/B00/t.mod"):
                raise ftplib.error_perm("550 Permission denied")
            return _mod(path.encode()[-20:])

        def read_partial(self, path, n, *, lane="scan"):
            return self.read_file(path)

    src = _Src(2, 2)
    root = "smb://mem/refused-file"
    register_source(root, src)
    rf._reg.source_lookup = lambda r: src
    try:
        seen = []
        for _ in range(4):
            n0 = len(_Src.reads)
            plan = await rf._poll_share(root, reason="tick")
            seen.append((plan.get("new"),
                         sum(1 for p in _Src.reads[n0:] if p.endswith("/A001/B00/t.mod"))))
        assert seen == [(3, 1), (0, 0), (0, 0), (0, 0)], seen
        assert toasts == [3]
    finally:
        remove_source(root)


async def test_a_file_that_kills_its_worker_runs_once_then_waits(ftp, freshness, monkeypatch):
    import test_scan_pool_recovery as tpr
    rf, toasts = freshness
    lib, store, make = ftp
    d = lib / "Artist" / "Album"
    d.mkdir(parents=True)
    (d / "01.mod").write_bytes(_mod(b"fine"))
    (d / "crash1.mod").write_bytes(_mod(b"kills its worker"))
    _age(lib)
    monkeypatch.setattr(scanner, "SCAN_WORKERS", 1)
    workers = tpr._crashing_real_pools(monkeypatch)
    sh = make()
    rf._reg.source_lookup = lambda root: sh.src
    try:
        runs = []
        for _ in range(4):
            n0 = tpr._SUBMITTED.count("_extract_one_remote")
            r0 = len(sh.srv.retrs)
            plan = await rf._poll_share(sh.root, reason="tick")
            runs.append((tpr._SUBMITTED.count("_extract_one_remote") - n0,
                         sum(1 for p, _o in sh.srv.retrs[r0:] if "crash1" in p), plan.get("new")))
        assert runs[0][1] == 1 and runs[0][2] == 1, runs     # one download; 01.mod is new
        assert all(r == (0, 0, 0) for r in runs[1:]), runs   # no download, no worker, not new
        assert toasts == [1]
    finally:
        tpr._kill_workers(workers)


# ── system folders ───────────────────────────────────────────────────────────

async def test_system_folders_are_neither_listed_nor_indexed(ftp):
    lib, store, make = ftp
    for a in range(2):
        alb = lib / f"Artist{a}" / "Album"
        (alb / "@eaDir" / "01.mod").mkdir(parents=True)
        (alb / "@eaDir" / "01.mod" / "SYNOINDEX_MEDIA_INFO").write_bytes(b"i")
        (alb / "01.mod").write_bytes(_mod(b"t %d" % a))
    (lib / "#recycle").mkdir()
    (lib / "#recycle" / "deleted.mod").write_bytes(_mod(b"deleted"))
    (lib / ".Trashes").mkdir()
    (lib / "lost+found").mkdir()
    _age(lib)
    sh = make()
    await _poll(sh)
    assert _rels(store) == ["/Artist0/Album/01.mod", "/Artist1/Album/01.mod"]
    listed = set(sh.srv.listed)
    assert not any(("@eaDir" in p or "#recycle" in p or "lost+found" in p) for p in listed), listed
    snap = remote_dirstate.snapshot(sh.root)
    assert snap["leaves"] == 2                     # the albums stay leaves
    n0 = len(sh.srv.listed)
    await _poll(sh, fast=True)
    assert not any("@eaDir" in p for p in sh.srv.listed[n0:])


# ── the fast walk's budget and round-robin ──────────────────────────────────

class _Mem(FileSource):
    def __init__(self, artists: int, albums: int):
        self.t = time.time() - 7200
        self.tree = {"/": [f"/A{a:03d}" for a in range(artists)]}
        for a in range(artists):
            self.tree[f"/A{a:03d}"] = [f"/A{a:03d}/B{b:02d}" for b in range(albums)]
            for b in range(albums):
                self.tree[f"/A{a:03d}/B{b:02d}"] = []
        self.listed: list[str] = []
        self.empty: set[str] = set()

    def list_dir(self, p):
        p = p or "/"
        self.listed.append(p)
        if p in self.empty:
            return []
        out = [DirEntry(c.rsplit("/", 1)[1], c, True, 0, self.t) for c in self.tree[p]]
        if not self.tree[p]:
            out.append(DirEntry("t.mod", p.rstrip("/") + "/t.mod", False, 10, self.t))
        return out

    def walk(self, top):
        raise NotImplementedError

    def read_file(self, *a, **k):
        raise FileNotFoundError

    def stat(self, *a, **k):
        raise FileNotFoundError

    def is_dir(self, p):
        return True


def _mwalk(src, root, fast):
    plan = remote_dirstate.WalkPlan(root, cap=(time.time() - 900) if fast else None)
    n0 = len(src.listed)
    scanner._find_remote_audio_entries(root, src, dir_mtime_cap=plan.cap if plan.fast else None,
                                       scan_zips=False, walk_plan=plan)
    return set(src.listed[n0:])


@pytest.mark.parametrize("artists,albums", [(200, 10), (100, 10), (1000, 10)])
def test_an_idle_fast_walk_grows_with_the_square_root_of_the_share(artists, albums):
    import math
    root = f"smb://mem/budget{artists}"
    remote_dirstate.forget(root)
    src = _Mem(artists, albums)
    _mwalk(src, root, False)
    folders = 1 + artists + artists * albums
    sizes = [len(src.listed) - 0]
    n = [len(_mwalk(src, root, True)) for _ in range(3)]
    bound = (1 + max(remote_dirstate._DEEP_MIN,
                     math.ceil(remote_dirstate._DEEP_SQRT * math.sqrt(folders)))
             + 2 * max(remote_dirstate._ROTATE_MIN,
                       math.ceil(remote_dirstate._ROTATE_SQRT * math.sqrt(artists * albums))))
    assert max(n) <= bound, (n, bound, sizes)
    assert max(n) <= 6 * math.sqrt(folders), n


def test_the_rotation_reaches_every_leaf_across_full_walks():
    root = "smb://mem/rotation"
    remote_dirstate.forget(root)
    src = _Mem(100, 10)
    leaves = {k for k, v in src.tree.items() if not v}
    reached: set = set()
    for _cycle in range(12):
        _mwalk(src, root, False)                     # a full walk every 5th poll
        for _ in range(4):
            reached |= _mwalk(src, root, True) & leaves
    assert reached == leaves, len(reached)


# ── a folder that lists empty without an error ──────────────────────────────

async def test_one_empty_listing_of_a_folder_that_held_tracks_deletes_nothing(blank):
    from soniqboom.core.filesource import register_source, remove_source

    class _Src(_Mem):
        def read_file(self, path, *, lane="stream"):
            return _mod(path.encode()[-20:])

        def read_partial(self, path, n, *, lane="scan"):
            return self.read_file(path)

    lib, store = blank
    src = _Src(3, 2)
    root = "smb://mem/empty-listing"
    register_source(root, src)
    try:
        await scanner.start_remote_scan("", root, src)
        n0 = len(store._tracks)
        assert n0 == 6
        src.empty = {"/A001"}
        plan = await scanner.start_remote_scan("", root, src)          # once: not believed
        assert len(store._tracks) == n0, plan
        plan = await scanner.start_remote_scan("", root, src)          # again: believed
        assert len(store._tracks) == n0 - 2, plan
        src.empty = set()
        await scanner.start_remote_scan("", root, src)
        assert len(store._tracks) == n0
    finally:
        remove_source(root)


# ── MLSD and LIST show the same listing ─────────────────────────────────────

def test_list_lines_and_mlsd_facts_name_links_alike():
    from soniqboom.core.filesource import FTPFileSource
    e = FTPFileSource._parse_list_line(
        "lrwxrwxrwx 1 u g 12 Jan 01  2020 song.mod -> ../x/song.mod", "/d")
    assert (e.name, e.path, e.is_dir) == ("song.mod", "/d/song.mod", False)
    e = FTPFileSource._parse_list_line("lrwxrwxrwx 1 u g 12 Jan 01  2020 Album -> /x/Album", "/d")
    assert (e.name, e.is_dir) == ("Album", False)           # a link: never walked into
    src = FTPFileSource("127.0.0.1", port=1, remote_path="/m")
    got = src._rooted([DirEntry(".38 Special", "/m/d/.38 Special", True, 0, 1.0),
                       DirEntry("a.mod", "/m/d/a.mod", False, 1, 1.0)])
    assert [(x.name, x.path) for x in got] == [(".38 Special", "/d/.38 Special"),
                                               ("a.mod", "/d/a.mod")]


async def test_a_switch_from_mlsd_to_list_shows_the_same_links(ftp):
    lib, _store, make = ftp
    d = lib / "Album"
    d.mkdir()
    (d / "a.mod").write_bytes(_mod(b"A"))
    os.symlink("a.mod", d / "link.mod")
    _age(lib)
    mlsd = make()
    plain = make(mlsd=False)
    view = lambda sh: sorted((e.name, e.is_dir) for e in sh.src.list_dir("/Album"))  # noqa: E731
    assert view(mlsd) == view(plain) == [("a.mod", False), ("link.mod", False)]


async def test_a_list_fallback_that_hides_dot_files_deletes_none(blank):
    """MLSD shows ``.38 Special``; a LIST fallback (most servers hide dot
    entries) doesn't — its tracks are no deletion in that walk."""
    from soniqboom.core.filesource import register_source, remove_source

    class _Src(_Mem):
        list_listings = 0
        hide = False

        def __init__(self):
            super().__init__(0, 0)
            self.tree = {"/": ["/.38 Special", "/Band"], "/.38 Special": [], "/Band": []}

        def list_dir(self, p):
            out = super().list_dir(p)
            if self.hide:
                type(self).list_listings += 1
                out = [e for e in out if not e.name.startswith(".")]
            return out

        def read_file(self, path, *, lane="stream"):
            return _mod(path.encode()[-20:])

        def read_partial(self, path, n, *, lane="scan"):
            return self.read_file(path)

    lib, store = blank
    src = _Src()
    root = "smb://mem/dots"
    register_source(root, src)
    try:
        await scanner.start_remote_scan("", root, src)
        assert len(store._tracks) == 2
        src.hide = True
        await scanner.start_remote_scan("", root, src)
        await scanner.start_remote_scan("", root, src, dir_mtime_cap=time.time() - 900)
        assert len(store._tracks) == 2
    finally:
        remove_source(root)


async def test_a_link_back_up_the_tree_is_not_walked(ftp):
    lib, store, make = ftp
    (lib / "Album").mkdir()
    (lib / "Album" / "a.mod").write_bytes(_mod(b"A"))
    os.symlink(".", lib / "All")                # the test server lists it as a folder
    os.symlink("..", lib / "Album" / "Up")
    _age(lib)
    sh = make()
    await _poll(sh)
    assert _rels(store) == ["/Album/a.mod"]
    assert len(sh.srv.listed) < 10, sh.srv.listed


# ── the health probe while a scan holds every connection ─────────────────────

async def test_the_health_probe_answers_while_scan_downloads_hold_the_pool(ftp, monkeypatch):
    from soniqboom.core import filesource
    lib, _store, make = ftp
    for i in range(4):
        (lib / f"Album{i}").mkdir()
        (lib / f"Album{i}" / "01.aiff").write_bytes(b"FORM" + bytes(600_000))
    _age(lib)
    sh = make(rate=150_000)                 # each download holds its connection ~4 s
    port = sh.srv.port
    real = filesource._resolve_pool_size
    monkeypatch.setattr(filesource, "_resolve_pool_size",
                        lambda h, p: (2, 0, 4, None) if p == port else real(h, p))
    loop = asyncio.get_running_loop()
    scan = asyncio.create_task(_poll(sh))
    await asyncio.sleep(1.5)
    assert sh.src._pool_saturated()
    t0 = time.monotonic()
    ok = await asyncio.wait_for(loop.run_in_executor(None, sh.src.is_dir, "/"), timeout=2.0)
    assert ok and time.monotonic() - t0 < 2.0
    assert sh.src.serving_transfers()
    await scan


def test_a_probe_timeout_behind_moving_transfers_is_no_miss():
    from soniqboom import main

    class _Busy:
        def serving_transfers(self):
            return True

    class _Idle:
        def serving_transfers(self):
            return False
    assert main._busy_serving(_Busy()) and not main._busy_serving(_Idle())
    assert not main._busy_serving(object())


# ── browsing a share never brings its full walks closer ──────────────────────

async def test_folder_open_checks_are_fast_polls_and_leave_the_drift_sweep_alone(ftp, freshness):
    rf, _toasts = freshness
    lib, _store, make = ftp
    for a in range(5):
        for b in range(3):
            (lib / f"A{a}" / f"B{b}").mkdir(parents=True)
            (lib / f"A{a}" / f"B{b}" / "01.mod").write_bytes(_mod(b"%d-%d" % (a, b)))
    _age(lib)
    sh = make()
    rf._reg.source_lookup = lambda root: sh.src
    await rf._poll_share(sh.root, reason="tick")             # cold start: full
    await rf._poll_share(sh.root, reason="tick")
    modes = []
    for _ in range(10):
        plan = await rf.check_now(sh.root, reason="folder_open")
        modes.append(plan.get("walk", {}).get("mode"))
    st = rf._reg.states[sh.root]
    assert modes == ["fast"] * 10, modes
    assert st.tick_polls == 2 and st.total_polls == 12
    for _ in range(3):
        await rf._poll_share(sh.root, reason="tick")
    assert rf._reg.states[sh.root].tick_polls == 5


def test_tick_polls_default_to_total_polls_from_an_older_state_file():
    from soniqboom.core.remote_freshness import ShareState
    st = ShareState.from_json({"scan_root": "ftp://h/s", "total_polls": 7})
    assert st.tick_polls == 7


# ── one unreadable folder and the unchanged-archive skip ─────────────────────

async def test_one_unreadable_folder_keeps_the_archive_skip_for_the_rest(ftp):
    from soniqboom.core.remote_cache import get_cache
    lib, _store, make = ftp
    (lib / "Album").mkdir()
    (lib / "Album" / "pack.zip").write_bytes(_zip({"a.mod": _mod(b"A"), "b.mod": _mod(b"B")}))
    (lib / "Locked").mkdir()
    (lib / "Locked" / "x.mod").write_bytes(_mod(b"X"))
    _age(lib)
    os.chmod(lib / "Locked", 0)
    sh = make()
    try:
        downloads = []
        for _ in range(3):
            get_cache().forget(sh.root, ["/Album/pack.zip"], "test: evicted")
            r0 = len(sh.srv.retrs)
            await _poll(sh)
            downloads.append(sum(1 for p, _o in sh.srv.retrs[r0:] if p.endswith("pack.zip")))
        assert downloads == [1, 0, 0], downloads
    finally:
        os.chmod(lib / "Locked", 0o755)


# ── the watcher's watch-limit warning ────────────────────────────────────────

def test_the_watch_limit_warning_is_rate_limited_per_root(monkeypatch):
    from soniqboom.core import watcher
    monkeypatch.setattr(watcher, "_limit_warned", {})
    assert watcher._limit_warning_due("/r") == 0
    assert watcher._limit_warning_due("/r") is None
    assert watcher._limit_warning_due("/r") is None
    assert watcher._limit_warning_due("/other") == 0
    monkeypatch.setattr(watcher, "_LIMIT_WARN_SEC", 0.0)
    assert watcher._limit_warning_due("/r") == 2              # two folders not warned about


@pytest.mark.parametrize("fast_between", [3, 4, 5])
async def test_an_emptied_folder_goes_at_the_second_full_walk_whatever_the_fast_polls(
        blank, fast_between):
    from soniqboom.core.filesource import register_source, remove_source

    class _Src(_Mem):
        def read_file(self, path, *, lane="stream"):
            return _mod(path.encode()[-20:])

        def read_partial(self, path, n, *, lane="scan"):
            return self.read_file(path)

    _lib, store = blank
    src = _Src(3, 2)
    root = f"smb://mem/emptied{fast_between}"
    register_source(root, src)
    try:
        await scanner.start_remote_scan("", root, src)
        src.empty = {"/A001"}
        counts = []
        for _full in range(2):
            await scanner.start_remote_scan("", root, src)
            counts.append(len(store._tracks))
            for _ in range(fast_between):
                await scanner.start_remote_scan("", root, src,
                                                dir_mtime_cap=time.time() - 900)
        assert counts == [6, 4], counts
    finally:
        remove_source(root)


async def test_an_archive_whose_download_keeps_breaking_waits_too(ftp, freshness):
    rf, _toasts = freshness
    lib, store, make = ftp
    (lib / "Album").mkdir()
    (lib / "Album" / "pack.zip").write_bytes(_zip({"a.mod": _mod(b"A")}))
    (lib / "Other").mkdir()
    (lib / "Other" / "y.mod").write_bytes(_mod(b"Y"))
    _age(lib)
    sh = make(abort_after=5, abort_retrs=10_000)
    rf._reg.source_lookup = lambda root: sh.src
    per_poll = []
    for _ in range(7):
        r0 = len(sh.srv.retrs)
        await rf._poll_share(sh.root, reason="tick")
        per_poll.append(sum(1 for p, _o in sh.srv.retrs[r0:] if p.endswith("pack.zip")))
    k = scanner._TRANSIENT_GIVEUPS
    assert all(n > 0 for n in per_poll[:k]) and all(n == 0 for n in per_poll[k:]), per_poll


async def test_the_hvsc_join_of_an_unfinished_scan_is_made_up_by_the_next(blank, monkeypatch):
    """The join raised, or the scan was stopped before it: the next scans
    join the share's SIDs (HVSC says 111 s, not the default 180 s)."""
    from soniqboom.core import hvsc, hvsc_apply
    from soniqboom.core.filesource import register_source, remove_source
    from test_remote_identical_reread import _Hvsc, _psid
    lib, store = blank
    (lib / "Sids").mkdir()
    (lib / "Sids" / "a.sid").write_bytes(_psid(b"Tune A"))
    (lib / "Sids" / "b.sid").write_bytes(_psid(b"Tune B"))
    _age(lib)
    monkeypatch.setattr(hvsc, "get_hvsc", lambda: _Hvsc())
    real = hvsc_apply.apply_hvsc_to_library
    state = {"how": "raise"}

    async def spy(*, reload=False, ids=None):
        if state["how"] == "raise":
            raise RuntimeError("simulated failure of the join")
        if state["how"] == "cancel":
            raise asyncio.CancelledError()
        return await real(reload=reload, ids=ids)
    monkeypatch.setattr(hvsc_apply, "apply_hvsc_to_library", spy)
    from test_remote_fast_walk import _Dav
    sh = _Dav(lib)
    register_source(sh.root, sh.src)
    try:
        for how in ("raise", "cancel"):
            for t in list(store._tracks):
                store.delete_track_ids([t])
            state["how"] = how
            try:
                await scanner.start_remote_scan("", sh.root, sh.src)
            except asyncio.CancelledError:
                pass
            durs = sorted(t.get("duration") for t in store._tracks.values())
            assert durs == [180.0, 180.0], (how, durs)
            state["how"] = "ok"
            await scanner.start_remote_scan("", sh.root, sh.src)       # nothing changed
            durs = sorted(t.get("duration") for t in store._tracks.values())
            assert durs == [111.0, 111.0], (how, durs)
            joined = []

            async def counting(*, reload=False, ids=None):
                joined.append(ids)
                return await real(reload=reload, ids=ids)
            monkeypatch.setattr(hvsc_apply, "apply_hvsc_to_library", counting)
            await scanner.start_remote_scan("", sh.root, sh.src)
            assert joined == [], joined                  # made up once, then done
            monkeypatch.setattr(hvsc_apply, "apply_hvsc_to_library", spy)
    finally:
        remove_source(sh.root)
        sh.stop()


async def test_the_health_monitor_counts_no_miss_behind_moving_transfers(monkeypatch):
    """``_share_health_monitor``: a probe timing out while the share's
    transfers move data neither counts toward "unavailable" nor reconnects;
    one timing out on an idle share does."""
    from soniqboom import main
    from soniqboom.core import filesource
    flips, reconnects = [], []

    class _Src(FileSource):
        busy = True

        def list_dir(self, p): return []
        def walk(self, top): return iter(())
        def read_file(self, *a, **k): raise FileNotFoundError
        def stat(self, *a, **k): raise FileNotFoundError
        def is_dir(self, p): return True

        def serving_transfers(self, within=15.0):
            return type(self).busy

        def reconnect(self):
            reconnects.append(1)
            return False
    src = _Src()
    monkeypatch.setattr(filesource, "all_sources", lambda: {"ftp://h/s": src})

    async def _status(root, st):
        flips.append(st)
    monkeypatch.setattr(main, "_set_share_status", _status)

    async def _none():
        return False
    monkeypatch.setattr(main, "_reconnect_pending_shares", _none)
    monkeypatch.setattr(main, "_stored_unavailable", lambda root: False)
    passes = {"n": 0}

    class _Aio:
        TimeoutError = asyncio.TimeoutError
        CancelledError = asyncio.CancelledError

        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(s, *a, **kw):
            passes["n"] += 1
            if passes["n"] > 6:
                raise asyncio.CancelledError()
            await asyncio.sleep(0)

        @staticmethod
        async def wait_for(aw, timeout):
            if hasattr(aw, "cancel"):
                aw.cancel()
            raise asyncio.TimeoutError()
    monkeypatch.setattr(main, "asyncio", _Aio())
    with pytest.raises(asyncio.CancelledError):
        await main._share_health_monitor()
    assert flips == [] and reconnects == []
    _Src.busy = False
    passes["n"] = 0
    with pytest.raises(asyncio.CancelledError):
        await main._share_health_monitor()
    assert flips == ["unavailable"] and reconnects == [1]
