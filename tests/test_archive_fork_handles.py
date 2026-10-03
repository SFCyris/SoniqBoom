# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``archive``: an open archive belongs to the process that opened it.

On Linux the scan pool forks its workers from the server, which has just
listed the archives (``archive`` keeps them open).  The workers used to
inherit those objects and their file descriptors — one shared file offset —
so parallel seek + read pairs returned other members' bytes: in one Docker
scan 274 of AHXSONGS.LHA's 513 members failed "crc is not matched".
A forked child now drops the inherited lhafile / zipfile objects (and the
cache lock) and opens its own; ``_LhaCliArchive`` trees (no descriptor) stay
shared, and only their creator removes them.  The synthesized archives come from
``scripts/archive_selftest.py``, the check the Docker build runs; its LHA
writer is cross-checked here against lhafile (and lhasa when installed)."""
from __future__ import annotations

import concurrent.futures
import importlib.util
import multiprocessing
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest

from soniqboom.core import archive

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "archive_selftest", _ROOT / "scripts" / "archive_selftest.py")
selftest = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("archive_selftest", selftest)   # workers unpickle its functions
_spec.loader.exec_module(selftest)

needs_fork = pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(), reason="no fork start method")


@pytest.fixture()
def lha(tmp_path):
    members = selftest.fixture_members(96)
    path = tmp_path / "SELFTEST.LHA"
    path.write_bytes(selftest.lha_archive(members))
    return str(path), members


@pytest.fixture()
def zpath(tmp_path):
    members = [(n.replace("\\", "/") + ".ahx", d, m) for n, d, m in selftest.fixture_members(96)]
    path = tmp_path / "selftest.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, _m in members:
            zf.writestr(name, data)
    return str(path), members


@pytest.fixture(autouse=True)
def _fresh_cache():
    with archive._CACHE_LOCK:
        archive._OPEN_CACHE.clear()
        archive._MAP_CACHE.clear()
    yield
    with archive._CACHE_LOCK:
        archive._OPEN_CACHE.clear()
        archive._MAP_CACHE.clear()


def test_lha_writer_round_trips_through_lhafile(lha):
    path, members = lha
    assert selftest.check_lhafile(path, members) == []
    methods = {m for _n, _d, m in members}
    assert methods == {"-lh0-", "-lh5-"}


def test_lh5_edge_inputs_round_trip(tmp_path):
    """The writer's single-leaf tables, multi-block streams and stored
    fallback decode exactly."""
    import random

    import lhafile
    cases = {
        "AHX.empty": b"",                            # stored (-lh0-)
        "AHX.one": b"\x7f",                          # one-leaf literal table, no positions
        "AHX.run": b"\x00" * 70000,                  # 256-byte matches, one-leaf position table
        "AHX.all": bytes(range(256)) * 3,
        "AHX.noise": random.Random(1).randbytes(70000),   # > 65535 codes: two blocks
    }
    p = tmp_path / "EDGE.LHA"
    p.write_bytes(selftest.lha_archive([(n, d, "-lh5-") for n, d in cases.items()]))
    lf = lhafile.LhaFile(str(p))
    types = {i.filename: i.compress_type for i in lf.infolist()}
    assert types["AHX.empty"] == b"-lh0-"
    assert types["AHX.noise"] == b"-lh5-"
    for name, data in cases.items():
        assert lf.read(name) == data, name


@pytest.mark.skipif(not shutil.which("lha"), reason="lhasa's lha not installed")
def test_lha_writer_is_valid_for_lhasa(lha):
    path, members = lha
    out = subprocess.run(["lha", "t", path], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.replace("\r", "\n").count("- Tested") == len(members)


@needs_fork
@pytest.mark.parametrize("which", ["lha", "zip"])
def test_fork_workers_read_every_member(which, lha, zpath):
    path, members = lha if which == "lha" else zpath
    ctx = multiprocessing.get_context("fork")
    assert selftest.check_scan_workers(path, members, 4, ctx) == []


@needs_fork
def test_worker_read_leaves_parent_offset_alone(lha):
    path, _members = lha
    archive.list_members(path, strict=True)                  # parent caches it open
    before = selftest._parent_offsets(path)
    assert before, "the parent should hold the archive open"
    ctx = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        ex.submit(selftest._worker_read_first, path).result(timeout=60)
    assert selftest._parent_offsets(path) == before


def _child_state(path, conn):
    names = archive.list_members(path, strict=True)
    inherited = len(archive._OPEN_CACHE)       # before this child opens anything
    archive.read_member(path, names[-1])
    conn.send((inherited, archive._CACHE_PID == os.getpid()))
    conn.close()


@needs_fork
def test_child_forgets_inherited_archives_and_held_lock(lha):
    """A lock held by another thread at the fork must not deadlock the child
    (a plain Process + timeout, so a regression fails instead of hanging)."""
    path, _members = lha
    archive.list_members(path, strict=True)
    held, release = threading.Event(), threading.Event()

    def _hold():
        with archive._CACHE_LOCK:
            held.set()
            release.wait(30)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert held.wait(10)
    ctx = multiprocessing.get_context("fork")
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_state, args=(path, send))
    try:
        proc.start()
        send.close()
        answered = recv.poll(30)
        if not answered:
            proc.kill()
        result = recv.recv() if answered else None
    finally:
        release.set()
        t.join(10)
        proc.join(10)
    assert answered, "the forked child deadlocked on the inherited cache lock"
    inherited, pid_ok = result
    assert inherited == 0
    assert pid_ok


def test_cache_from_another_pid_is_not_used(lha):
    """The PID guard: a fork that skipped the at-fork hook re-opens."""
    path, _members = lha
    names = archive.list_members(path, strict=True)
    archive.read_member(path, names[0])
    (first,) = archive._OPEN_CACHE.values()
    archive._CACHE_PID = -1                     # as if inherited by a child
    try:
        archive.read_member(path, names[0])
        (second,) = archive._OPEN_CACHE.values()
        assert second is not first
        assert archive._CACHE_PID == os.getpid()
    finally:
        archive._CACHE_PID = os.getpid()


class _FakeCli(archive._LhaCliArchive):
    """An ``_LhaCliArchive`` over a hand-made tree (no ``lha`` binary needed);
    ``_extract`` writes the same tree again, as the CLI would."""

    def __init__(self, root, files):
        self._root, self._payload = root, files
        self._path = "unused.lha"
        self._extract()

    def _extract(self):
        import tempfile
        self._owner = os.getpid()
        self._dir = tempfile.mkdtemp(prefix="sb_lha_", dir=self._root)
        self._files = {}
        for name, data in self._payload.items():
            full = os.path.join(self._dir, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(data)
            self._files[name] = full


def test_lha_cli_tree_is_kept_and_only_its_creator_removes_it(tmp_path, lha):
    """Forked workers keep sharing the server's extracted tree (no descriptor
    to share), never delete it, and extract their own copy if it vanished."""
    path, _members = lha
    cli = _FakeCli(str(tmp_path), {"Dir/AHX.one": b"one", "AHX.two": b"two"})
    archive.list_members(path, strict=True)                  # + an lhafile entry
    with archive._CACHE_LOCK:
        archive._OPEN_CACHE[("cli.lha", 0.0)] = cli
    archive._drop_inherited_archives()                       # what a child runs
    kept = list(archive._OPEN_CACHE.values())
    assert kept == [cli]                                     # lhafile dropped, CLI kept

    first_dir = cli._dir
    cli._owner = -1                                          # as seen in a forked child
    cli.close()
    assert os.path.isdir(first_dir)                          # not the child's to remove
    import shutil as _sh
    _sh.rmtree(first_dir)                                    # the server evicted it
    assert cli.read("AHX.two") == b"two"                     # child re-extracts its own
    assert cli._dir != first_dir and cli._owner == os.getpid()
    cli.close()
    assert not os.path.isdir(cli._dir)                       # its creator removes it


def test_lha_cli_owner_still_raises_when_its_tree_is_gone(tmp_path):
    cli = _FakeCli(str(tmp_path), {"AHX.x": b"x"})
    cli.close()
    with pytest.raises(FileNotFoundError):
        cli.read("AHX.x")
