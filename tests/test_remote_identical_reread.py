# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A re-read file whose metadata is unchanged (touched, rewritten identically)
is an mtime / size refresh, not a library change — on a network share's scan
and on a folder click's refresh, as on a local scan: its ``added_at`` stays,
no sequence moves (no ``lib_gen``), no post-scan pass runs, no end frame is
sent and no sorted list is rebuilt.  A changed file still updates everything
but keeps its ``added_at``; the HVSC join after a share's scan covers only the
SIDs that scan added or changed.

Real scans of the WebDAV and FTP test servers (listings + downloads of a local
folder) and of a local folder."""
from __future__ import annotations

import os
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402
from _webdav_server import DavServer  # noqa: E402

from soniqboom.core import scanner  # noqa: E402

OLD = time.time() - 7200


def _mod(title: bytes, fill: int = 0) -> bytes:
    """A minimal 4-channel ProTracker module (the title padded: a renamed
    one is the same size)."""
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 512)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, fill & 0xFF])
    return (title[:20].ljust(20, b"\0") + samples + bytes([1, 127]) + bytes(128)
            + b"M.K." + bytes(pat) + bytes(([100] * 16 + [156] * 16) * 32))


def _psid(name: bytes, songs: int = 1) -> bytes:
    h = bytearray(0x7C)
    h[0:4] = b"PSID"
    struct.pack_into(">HHHHHHH", h, 4, 2, 0x7C, 0, 0x1000, 0x1003, songs, 1)
    h[0x16:0x16 + 20] = name[:20].ljust(20, b"\0")
    h[0x36:0x36 + 3] = b"Rob"
    h[0x56:0x56 + 4] = b"1987"
    return bytes(h) + b"\x00" * 32


FILES = {
    "Artist/Album/x1.mod": _mod(b"X one"),
    "Artist/Album/x2.mod": _mod(b"X two"),
    "Other/y1.mod": _mod(b"Y one"),
    "Other/control.mod": _mod(b"Control"),
    "Sids/a.sid": _psid(b"Tune A"),
    "Sids/b.sid": _psid(b"Tune B"),
}


def _age(root: Path) -> None:
    for p in sorted(root.rglob("*"), key=lambda q: -len(q.parts)):
        os.utime(p, (OLD, OLD))
    os.utime(root, (OLD, OLD))


@pytest.fixture
def fresh(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())
    calls: list = []

    async def _none():
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda *a, **k: calls.append("runner"))
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass",
                        lambda *a, **k: calls.append("folder"))
    ends: list = []
    real_end = scanner._send_scan_end

    async def _end(end):
        ends.append(end)
        await real_end(end)
    monkeypatch.setattr(scanner, "_send_scan_end", _end)
    lib = tmp_path / "lib"
    for rel, data in FILES.items():
        p = lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _age(lib)
    return lib, store_mod._store, calls, ends


class _Dav:
    def __init__(self, lib: Path):
        from soniqboom.core.filesource_webdav import WebDAVFileSource
        self.srv = DavServer(lib).start()
        self.root = self.srv.url
        self.src = WebDAVFileSource(self.root + "/")

    def stop(self):
        self.srv.stop()


class _Ftp:
    def __init__(self, lib: Path):
        from soniqboom.core.filesource import FTPFileSource
        self.srv = FtpServer(lib.parent).start()
        self.root = f"ftp://127.0.0.1/{lib.name}"
        self.src = FTPFileSource("127.0.0.1", port=self.srv.port, remote_path=f"/{lib.name}")

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
    lib, store, calls, ends = fresh
    sh = _Dav(lib) if request.param == "webdav" else _Ftp(lib)
    register_source(sh.root, sh.src)
    try:
        yield sh, lib, store, calls, ends
    finally:
        remove_source(sh.root)
        sh.stop()


def _names(store) -> dict[str, dict]:
    return {t["path"].rsplit("/", 1)[-1]: t for t in store._tracks.values()}


def _seqs(store) -> tuple:
    return (store._mutation_seq, store._browse_seq, store._catalog_seq, store._duration_seq)


def _lib_gen() -> str:
    from soniqboom.api.library import library_generation
    return library_generation()


async def _scan(sh) -> dict:
    return await scanner.start_remote_scan("", sh.root, sh.src)


def _touch(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _date_back(store) -> None:
    """Every track ``added`` long ago (a re-extract within the same second
    would otherwise stamp the same value)."""
    store.update_track_fields_batch([(tid, {"added_at": 1_000_000 + i})
                                     for i, tid in enumerate(sorted(store._tracks))])


async def test_an_identical_reread_only_refreshes_the_mtime(share):
    sh, lib, store, calls, ends = share
    plan = await _scan(sh)
    assert set(_names(store)) == {p.rsplit("/", 1)[-1] for p in FILES}
    assert plan.get("changed") == len(FILES)
    _date_back(store)
    before = {n: dict(t) for n, t in _names(store).items()}
    seqs, gen = _seqs(store), _lib_gen()
    calls.clear()
    ends.clear()
    when = time.time() - 120
    _touch(lib / "Artist/Album/x1.mod", when)
    _touch(lib / "Other/y1.mod", when)
    plan = await _scan(sh)
    after = _names(store)
    for n in ("x1.mod", "y1.mod"):
        assert after[n]["added_at"] == before[n]["added_at"]
        # (moved by the touch — whatever offset a server's listing times carry)
        assert abs((after[n]["mtime"] - before[n]["mtime"]) - (when - OLD)) <= 61
        assert {k: v for k, v in after[n].items() if k != "mtime"} == \
            {k: v for k, v in before[n].items() if k != "mtime"}
    assert after["control.mod"] == before["control.mod"]
    # no library change: no sequence (so no lib_gen) moved — no sorted list
    # was rebuilt either (that moves ``_browse_seq``) —, no post-scan pass,
    # no end frame
    assert _seqs(store) == seqs and _lib_gen() == gen
    assert calls == ["folder"] and ends == [None]
    assert store.verify_indexes()["index_ok"]
    assert (plan["extract"], plan.get("changed"), plan.get("unchanged_rereads")) == (2, 0, 2), plan


async def test_a_changed_reread_updates_everything_but_added_at(share):
    sh, lib, store, calls, ends = share
    await _scan(sh)
    _date_back(store)
    before = {n: dict(t) for n, t in _names(store).items()}
    seqs, gen = _seqs(store), _lib_gen()
    calls.clear()
    ends.clear()
    p = lib / "Artist/Album/x2.mod"
    p.write_bytes(_mod(b"X renamed"))             # same size, new title
    _touch(p, time.time() - 60)
    plan = await _scan(sh)
    after = _names(store)
    assert after["x2.mod"]["title"] == "X renamed"
    assert after["x2.mod"]["added_at"] == before["x2.mod"]["added_at"]
    assert all(after[n] == before[n] for n in before if n != "x2.mod")
    s1 = _seqs(store)
    assert s1[0] > seqs[0] and s1[1] > seqs[1] and _lib_gen() != gen
    assert calls == ["runner"] and ends and ends[-1] is not None
    assert store.verify_indexes()["index_ok"]
    # its sorted entries moved with it (the batch's merge)
    assert any(tid == after["x2.mod"]["id"] for k, tid in store._sorted_title if k == "x renamed")
    assert (plan["extract"], plan.get("changed")) == (1, 1), plan


async def test_a_new_file_and_a_removed_one_are_changes(share):
    sh, lib, store, calls, ends = share
    await _scan(sh)
    calls.clear()
    t0 = time.time()
    (lib / "Other" / "new.mod").write_bytes(_mod(b"New one"))
    (lib / "Other" / "control.mod").unlink()
    plan = await _scan(sh)
    names = _names(store)
    assert "new.mod" in names and "control.mod" not in names
    assert names["new.mod"]["added_at"] >= int(t0) - 1
    assert calls == ["runner"]
    assert store.verify_indexes()["index_ok"]
    assert plan.get("changed") == 2 and plan["new"] == 1, plan


class _Hvsc:
    def is_configured(self):
        return True

    def reload(self):
        pass

    def lookup_durations_by_md5(self, md5):
        return [111.0]

    def stil_key_for(self, path):
        return None

    def lookup_stil_by_relpath(self, key):
        return None


async def test_the_hvsc_join_covers_only_the_sids_a_scan_changed(fresh, monkeypatch):
    from soniqboom.core import hvsc, hvsc_apply
    from soniqboom.core.filesource import register_source, remove_source
    lib, store, calls, ends = fresh
    monkeypatch.setattr(hvsc, "get_hvsc", lambda: _Hvsc())
    joined: list = []
    real = hvsc_apply.apply_hvsc_to_library

    async def spy(*, reload=False, ids=None):
        joined.append(None if ids is None else set(ids))
        return await real(reload=reload, ids=ids)
    monkeypatch.setattr(hvsc_apply, "apply_hvsc_to_library", spy)
    sh = _Dav(lib)
    register_source(sh.root, sh.src)
    try:
        await _scan(sh)
        sid = {n: t["id"] for n, t in _names(store).items() if n.endswith(".sid")}
        # (the SIDs the scan added / changed)
        assert len(joined) == 1 and joined[0] == set(sid.values())
        assert _names(store)["a.sid"]["duration"] == 111.0          # (the join ran)
        joined.clear()
        await _scan(sh)                                            # nothing changed
        _touch(lib / "Sids/a.sid", time.time() - 90)               # identical re-read
        await _scan(sh)
        assert joined == []
        (lib / "Sids/b.sid").write_bytes(_psid(b"Tune B renamed"))   # same size
        _touch(lib / "Sids/b.sid", time.time() - 30)
        await _scan(sh)
        assert joined == [{sid["b.sid"]}]
        assert _names(store)["b.sid"]["title"] == "Tune B renamed"
    finally:
        remove_source(sh.root)
        sh.stop()


async def test_a_folder_click_refresh_of_an_identical_file_is_no_update(fresh, monkeypatch):
    from soniqboom.core.data import upsert_scan_dir
    lib, store, calls, ends = fresh
    root = str(lib.resolve())
    await upsert_scan_dir(root, track_count_val=0)
    sub = str((lib / "Artist" / "Album").resolve())
    res = await scanner.refresh_subtree_under_root(root, sub)
    assert res["added"] == 2 and res["updated"] == 0
    _date_back(store)
    before = {n: dict(t) for n, t in _names(store).items()}
    seqs = _seqs(store)
    _touch(lib / "Artist/Album/x1.mod", time.time() - 100)
    res = await scanner.refresh_subtree_under_root(root, sub)
    after = _names(store)
    assert after["x1.mod"]["added_at"] == before["x1.mod"]["added_at"]
    assert after["x1.mod"]["mtime"] != before["x1.mod"]["mtime"]
    assert _seqs(store) == seqs
    assert (res["added"], res["updated"], res["removed"], res.get("unchanged")) == (0, 0, 0, 1), res
    p = lib / "Artist/Album/x2.mod"
    p.write_bytes(_mod(b"X two renamed"))
    _touch(p, time.time() - 50)
    res = await scanner.refresh_subtree_under_root(root, sub)
    assert (res["added"], res["updated"], res.get("unchanged", 0)) == (0, 1, 0), res
    after = _names(store)
    assert after["x2.mod"]["title"] == "X two renamed"
    assert after["x2.mod"]["added_at"] == before["x2.mod"]["added_at"]
    assert _seqs(store)[0] > seqs[0]
    assert store.verify_indexes()["index_ok"]


async def test_a_scan_that_configures_hvsc_joins_every_sid(fresh, monkeypatch):
    """HVSC found (auto-configured) by THIS scan: its documents are joined
    onto every SID of the library — the unchanged ones too."""
    from soniqboom.core import hvsc, hvsc_apply
    from soniqboom.core.filesource import register_source, remove_source
    lib, store, calls, ends = fresh
    state = {"on": False}

    class _Later(_Hvsc):
        def is_configured(self):
            return state["on"]
    monkeypatch.setattr(hvsc, "get_hvsc", lambda: _Later())
    joined: list = []
    real = hvsc_apply.apply_hvsc_to_library

    async def spy(*, reload=False, ids=None):
        joined.append(None if ids is None else set(ids))
        return await real(reload=reload, ids=ids)
    monkeypatch.setattr(hvsc_apply, "apply_hvsc_to_library", spy)
    sh = _Dav(lib)
    register_source(sh.root, sh.src)
    try:
        await _scan(sh)                          # (HVSC not configured: no join)
        assert joined == []
        monkeypatch.setattr(scanner, "_hvsc_probed_roots", set())
        monkeypatch.setattr(scanner, "_detect_hvsc_docs_remote",
                            lambda source, root, dirs: "/somewhere/DOCUMENTS")

        async def autoconfig(docs):
            state["on"] = True
            return True
        monkeypatch.setattr(scanner, "_apply_hvsc_autoconfig", autoconfig)
        plan = await _scan(sh)                   # nothing changed, HVSC configured now
        assert plan["changed"] == 0
        assert joined == [None]                  # every SID
        assert _names(store)["a.sid"]["duration"] == 111.0
    finally:
        remove_source(sh.root)
        sh.stop()
