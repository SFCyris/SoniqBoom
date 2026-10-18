# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A remote file replaced in place never plays from its old cached copy: the
remote cache checks a copy against the identity the library holds for the
file (``RemoteCache.get_cached(ident=)``, no request to the share), and a
freshness poll drops the copies of files it found changed or removed
(``RemoteCache.forget``, called by the remote scan)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from soniqboom.core import remote_cache as rc_mod  # noqa: E402
from soniqboom.core.remote_cache import RemoteCache  # noqa: E402


class _Src:
    """A share of in-memory files; counts the reads that reach it."""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.reads: list[str] = []

    def read_file(self, path, *, lane="stream"):
        self.reads.append(path)
        return self.files[path]


@pytest.fixture
def cache(tmp_path):
    return RemoteCache(tmp_path / "remote", max_mb=64)


def test_a_copy_of_another_version_is_dropped_and_fetched_again(cache):
    src = _Src({"/a.m4a": b"A" * 1000})
    p1 = cache.fetch("s", "/a.m4a", src, ident=(1000, 1_700_000_000.0))
    assert p1.read_bytes() == b"A" * 1000 and src.reads == ["/a.m4a"]
    # warm: same identity → the copy, no read of the share
    assert cache.get_cached("s", "/a.m4a", ident=(1000, 1_700_000_000.0)) == p1
    assert cache.fetch("s", "/a.m4a", src, ident=(1000, 1_700_000_000.0)) == p1
    assert src.reads == ["/a.m4a"]
    # replaced at the source (the record now says so): the old copy goes
    src.files["/a.m4a"] = b"B" * 1500
    assert cache.get_cached("s", "/a.m4a", ident=(1500, 1_700_000_500.0)) is None
    assert not p1.exists()
    p2 = cache.fetch("s", "/a.m4a", src, ident=(1500, 1_700_000_500.0))
    assert p2.read_bytes() == b"B" * 1500 and src.reads == ["/a.m4a", "/a.m4a"]
    assert cache.total_size() == 1500


def test_same_size_rewrite_is_told_by_its_mtime(cache):
    src = _Src({"/b.flac": b"x" * 64})
    cache.fetch("s", "/b.flac", src, ident=(64, 1_700_000_010.0))
    assert cache.get_cached("s", "/b.flac", ident=(64, 1_700_000_200.0)) is None


def test_mtime_precision_shift_is_the_same_version(cache):
    """MLSD seconds vs LIST minutes: the scanner's tolerant rule."""
    src = _Src({"/c.mp3": b"c" * 10})
    p = cache.fetch("s", "/c.mp3", src, ident=(10, 1_700_000_037.0))
    minute = 1_700_000_037.0 - (1_700_000_037 % 60)
    assert cache.get_cached("s", "/c.mp3", ident=(10, minute)) == p
    # an unknown value (0) on either side is not compared
    assert cache.get_cached("s", "/c.mp3", ident=(10, 0.0)) == p
    assert cache.get_cached("s", "/c.mp3", ident=(0, 1_700_000_037.0)) == p


def test_a_copy_indexed_without_identity_takes_the_records(cache):
    src = _Src({"/d.mp3": b"d" * 100, "/e.mp3": b"e" * 100, "/f.flac": b"f" * 100})
    for rp in ("/d.mp3", "/e.mp3", "/f.flac"):
        cache.fetch("s", rp, src)                       # no identity known (a scan's fetch)
    # same size → kept, and stamped: a later change is told apart
    pd = cache.get_cached("s", "/d.mp3", ident=(100, 1_700_000_000.0))
    assert pd is not None
    assert cache.get_cached("s", "/d.mp3", ident=(100, 1_700_009_000.0)) is None
    # its size disagrees with the record → another version
    assert cache.get_cached("s", "/e.mp3", ident=(120, 1_700_000_000.0)) is None
    # a FLAC copy may be bigger (SEEKTABLE inserted): size not compared
    assert cache.get_cached("s", "/f.flac", ident=(90, 1_700_000_000.0)) is not None


def test_get_cached_without_identity_is_unchanged(cache):
    src = _Src({"/g.mp3": b"g" * 10})
    p = cache.fetch("s", "/g.mp3", src, ident=(10, 1.0e9))
    assert cache.get_cached("s", "/g.mp3") == p          # offline play, other callers


def test_identity_survives_a_restart(tmp_path):
    src = _Src({"/h.mp3": b"h" * 10})
    c1 = RemoteCache(tmp_path / "remote", max_mb=64)
    c1.fetch("s", "/h.mp3", src, ident=(10, 1_700_000_000.0))
    c2 = RemoteCache(tmp_path / "remote", max_mb=64)
    assert c2.get_cached("s", "/h.mp3", ident=(10, 1_700_000_000.0)) is not None
    assert c2.get_cached("s", "/h.mp3", ident=(11, 1_700_000_000.0)) is None


def test_forget_drops_copies_and_retires_subsets(cache, tmp_path):
    src = _Src({"/x.mp3": b"x" * 10, "/y.mp3": b"y" * 10})
    px = cache.fetch("s", "/x.mp3", src)
    py = cache.fetch("s", "/y.mp3", src)
    sub = rc_mod.subset_name("/arc.zip", "m.mod", 999, 1_700_000_000)
    t = cache.temp_path()
    t.write_bytes(b"z" * 5)
    cache.put_file("s", sub, t)
    assert cache.find_subset("s", "/arc.zip", "m.mod", peek=True) is not None
    assert cache.forget("s", ["/x.mp3", "/arc.zip", "/never-cached.mp3"]) == 1
    assert not px.exists() and py.exists()
    assert not cache.peek_cached("s", "/x.mp3") and cache.peek_cached("s", "/y.mp3")
    assert cache.find_subset("s", "/arc.zip", "m.mod", peek=True) is None
    assert cache.total_size() == 15


# ── The remote scan drops changed / removed files' copies (WebDAV) ─────────

from test_remote_fast_walk import _Dav, _mod, _names, fresh  # noqa: E402,F401


async def test_a_poll_drops_the_copies_of_changed_and_removed_files(fresh, monkeypatch, tmp_path):
    from soniqboom.core import scanner
    from soniqboom.core.filesource import parse_remote_path, register_source, remove_source
    lib, store = fresh
    cache = RemoteCache(tmp_path / "rcache", max_mb=64)
    monkeypatch.setattr(rc_mod, "_cache", cache)
    sh = _Dav(lib)
    register_source(sh.root, sh.src)
    try:
        await scanner.start_remote_scan("", sh.root, sh.src, dir_mtime_cap=None)
        rel = {n: parse_remote_path(t["path"])[1] for n, t in _names(store).items()}
        for n in ("x1.mod", "x3.mod", "y2.mod"):
            cache.fetch(sh.root, rel[n], sh.src)
        # a render of x1 and of x3 in the conversion cache
        from soniqboom.core import conversion_cache as cc
        renders, renders_path = {}, {}
        for n in ("x1.mod", "x3.mod"):
            tid = _names(store)[n]["id"]
            src_wav = tmp_path / f"r-{n}.wav"
            src_wav.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"\0" * 32)
            key = cc._cache_key(tid, "tracker", 0)
            renders_path[n] = await cc.store_cached(key, "tracker", src_wav)
            renders[n] = key
        # x1 rewritten in place (same size, new bytes, new mtime), y2 removed,
        # x3 untouched
        p = lib / "Artist" / "Album" / "x1.mod"
        size = p.stat().st_size
        p.write_bytes(_mod(b"X one, new", fill=7))
        assert p.stat().st_size == size
        os.utime(p, (time.time(), time.time()))
        (lib / "Other" / "y2.mod").unlink()
        plan = await scanner.start_remote_scan("", sh.root, sh.src, dir_mtime_cap=None)
        assert plan["extract"] >= 1 and plan["ghosts"] == 1, plan
        # (the scan's own extraction may have cached the NEW bytes again)
        c = cache.get_cached(sh.root, rel["x1.mod"])
        assert c is None or c.read_bytes() == p.read_bytes()
        assert not cache.peek_cached(sh.root, rel["y2.mod"])
        assert cache.peek_cached(sh.root, rel["x3.mod"])          # unchanged: kept
        got = cache.fetch(sh.root, rel["x1.mod"], sh.src)
        assert got.read_bytes() == p.read_bytes()
        # … and what was rendered from the changed file went too
        assert renders["x1.mod"] not in cc._meta and not renders_path["x1.mod"].exists()
        assert renders["x3.mod"] in cc._meta and renders_path["x3.mod"].exists()
        await cc.purge_tracks([_names(store)["x3.mod"]["id"]])     # (leave the cache as found)
    finally:
        remove_source(sh.root)
        sh.stop()


# ── The play path checks the record's identity ─────────────────────────────

async def test_play_lookup_checks_the_record_identity(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from soniqboom.api import stream
    cache = RemoteCache(tmp_path / "rcache", max_mb=64)
    monkeypatch.setattr(rc_mod, "_cache", cache)
    src = _Src({"/k.m4a": b"k" * 50})
    p = cache.fetch("s", "/k.m4a", src, ident=(50, 1_700_000_000.0))
    rec = SimpleNamespace(file_size=50, mtime=1_700_000_000.0)
    ident = await stream._remote_play_ident(rec, src, "s", "/k.m4a")
    assert ident == (50, 1_700_000_000.0)
    assert stream._remote_cached_copy("s", "/k.m4a", ident) == p
    assert src.reads == ["/k.m4a"]                        # warm: nothing asked of the share
    rec2 = SimpleNamespace(file_size=70, mtime=1_700_000_900.0)   # a poll saw the change
    ident2 = await stream._remote_play_ident(rec2, src, "s", "/k.m4a")
    assert stream._remote_cached_copy("s", "/k.m4a", ident2) is None
    assert not p.exists()


async def test_a_record_without_identity_asks_the_share_once(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from soniqboom.api import stream
    from soniqboom.core.filesource import FileStat
    cache = RemoteCache(tmp_path / "rcache", max_mb=64)
    monkeypatch.setattr(rc_mod, "_cache", cache)
    src = _Src({"/m.m4a": b"m" * 50})
    stats: list[str] = []
    src.stat = lambda path, lane="scan": (stats.append(path), FileStat(size=50, mtime=1.7e9))[1]
    rec = SimpleNamespace(file_size=None, mtime=0.0)
    assert await stream._remote_play_ident(rec, src, "s", "/m.m4a") is None   # nothing cached: no stat
    assert stats == []
    cache.fetch("s", "/m.m4a", src)
    assert await stream._remote_play_ident(rec, src, "s", "/m.m4a") == (50, 1.7e9)
    assert stats == ["/m.m4a"]


def test_a_legacy_copy_older_than_the_files_change_is_another_version(cache):
    """Indexed without identity (before identities were kept), same size: the
    file's mtime after the copy's fetch time tells it changed."""
    src = _Src({"/n.mp3": b"n" * 100})
    cache.fetch("s", "/n.mp3", src)
    fetched = cache._index[cache._cache_key("s", "/n.mp3")]["fetched"]
    assert cache.get_cached("s", "/n.mp3", ident=(100, fetched + 600)) is None
    cache.fetch("s", "/n.mp3", src)
    assert cache.get_cached("s", "/n.mp3", ident=(100, fetched - 86400)) is not None


def test_a_download_overlapping_a_forget_is_never_current(cache):
    """A poll drops a file while a download of it (from a path that knows no
    identity) is still running: those bytes may be the old version."""
    import threading
    gate, started = threading.Event(), threading.Event()

    class _Slow(_Src):
        def read_file(self, path, *, lane="stream"):
            started.set()
            gate.wait(5)
            return super().read_file(path, lane=lane)
    src = _Slow({"/o.mp3": b"old" * 10})
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("p", cache.fetch("s", "/o.mp3", src)))
    t.start()
    assert started.wait(5)
    cache.forget("s", ["/o.mp3"])
    gate.set()
    t.join(5)
    assert out["p"].read_bytes() == b"old" * 10                # the caller still gets its bytes
    assert cache.get_cached("s", "/o.mp3", ident=(30, 1.7e9)) is None
    src.files["/o.mp3"] = b"new" * 10
    assert cache.fetch("s", "/o.mp3", src, ident=(30, 1.7e9)).read_bytes() == b"new" * 10
    assert cache.get_cached("s", "/o.mp3", ident=(30, 1.7e9)) is not None
