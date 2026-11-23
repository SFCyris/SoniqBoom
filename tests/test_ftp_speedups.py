# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FTP speed-ups of 2.1.2, through the suite's FTP test server:

* a scan reads an MP4 / M4A file's front and ``moov`` index, not its audio
  (``scanner`` ladder stage "index", ``tag_window.mp4_window``);
* a walk lists several folders at once (``FTPFileSource.walk_with_stat``);
* fewer commands per transfer: no ``NOOP`` for a connection that answered
  moments ago (``_PooledFTP.fresh_enough``), ``TYPE`` only when the mode
  changes, MLSD left in binary mode (``_TypedFTP``);
* the remote folder state is kept across restarts (``remote_dirstate``
  ``preload`` / ``flush``), checked against the library's track count."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402

from soniqboom.core import filesource, metadata, remote_dirstate, scanner, tag_window  # noqa: E402
from soniqboom.core.filesource import DirEntry, FileSource, FTPFileSource  # noqa: E402

OLD = time.time() - 7200
SECS = 30
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


# ── fixtures ────────────────────────────────────────────────────────────────

def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, timeout=180)


@pytest.fixture(scope="module")
def m4a(tmp_path_factory) -> dict[str, Path]:
    """name → file: AAC with a 300 KB cover and the index at the END
    (ffmpeg's default), the same with the index at the FRONT ("fast start"),
    an ALAC file (index at the end), and a short AAC file under the size the
    index read pays off for (read whole)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    d = tmp_path_factory.mktemp("m4a")
    noise = ["-f", "lavfi", "-i", f"anoisesrc=d={SECS}:c=pink:a=0.3", "-ac", "2"]
    _ff("-f", "lavfi", "-i", "cellauto=s=600x600:rate=1", "-frames:v", "1", "-q:v", "1",
        str(d / "cover.jpg"))
    assert (d / "cover.jpg").stat().st_size > 200_000
    _ff(*noise, "-c:a", "aac", "-b:a", "256k", str(d / "plain.m4a"))
    tags = ["-metadata", "title=Index Song", "-metadata", "artist=The Atoms",
            "-metadata", "album=Moov"]
    _ff("-i", str(d / "plain.m4a"), "-i", str(d / "cover.jpg"), "-map", "0", "-map", "1",
        "-c", "copy", "-disposition:v", "attached_pic", *tags, str(d / "end.m4a"))
    _ff("-i", str(d / "end.m4a"), "-map", "0", "-c", "copy", "-movflags", "+faststart",
        str(d / "front.m4a"))
    _ff(*noise, "-c:a", "alac", *tags, str(d / "lossless.m4a"))
    _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", "-b:a", "96k",
        *tags, str(d / "short.m4a"))
    (d / "plain.m4a").unlink()
    (d / "cover.jpg").unlink()
    out = {p.name: p for p in sorted(d.iterdir())}
    assert out["short.m4a"].stat().st_size <= 2 * tag_window.MP4_FRONT
    for name in ("end.m4a", "front.m4a", "lossless.m4a"):
        assert out[name].stat().st_size > 4 * tag_window.MP4_FRONT, name
    return out


@pytest.fixture
def blank(monkeypatch, tmp_data_dir):
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
    _reset_dirstate(monkeypatch)
    return store_mod._store


def _reset_dirstate(monkeypatch) -> None:
    """A fresh process as far as ``remote_dirstate`` knows (persistence off)."""
    monkeypatch.setattr(remote_dirstate, "_persist", False)
    monkeypatch.setattr(remote_dirstate, "_saved", {})
    monkeypatch.setattr(remote_dirstate, "_dirty", False)
    monkeypatch.setattr(remote_dirstate, "_saver", None)
    with remote_dirstate._states_lock:
        remote_dirstate._states.clear()


def _age(lib: Path) -> None:
    for q in sorted(lib.rglob("*"), key=lambda q: -len(q.parts)):
        os.utime(q, (OLD, OLD))
    os.utime(lib, (OLD, OLD))


class _Ftp:
    """The test FTP server serving ``lib`` as a registered share."""

    def __init__(self, lib: Path, **server_kw):
        self.srv = FtpServer(lib.parent, **server_kw).start()
        self.root = f"ftp://127.0.0.1/{lib.name}"
        self.src = FTPFileSource("127.0.0.1", port=self.srv.port, remote_path=f"/{lib.name}")
        filesource.register_source(self.root, self.src)

    def stop(self):
        filesource.remove_source(self.root)
        with filesource._FTP_POOLS_LOCK:
            pools = [filesource._FTP_POOLS.pop(k) for k in list(filesource._FTP_POOLS)
                     if k[1] == self.srv.port]
        for p in pools:
            p.close_all()
        self.srv.stop()


def _by_name(store) -> dict[str, dict]:
    return {t["path"].rsplit("/", 1)[-1]: t for t in store._tracks.values()}


# ── 1. MP4 / M4A: the index, not the audio ──────────────────────────────────

FIELDS = ("title", "artist", "album", "format", "channels", "sample_rate", "bit_depth")


@needs_ffmpeg
async def test_m4a_scan_reads_the_index_not_the_audio(blank, m4a, tmp_path):
    store = blank
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    for name, p in m4a.items():
        shutil.copy(p, lib / "A" / name)
    _age(lib)
    sh = _Ftp(lib)
    try:
        await scanner.start_remote_scan("", sh.root, sh.src)
        sent, retrs = sh.srv.bytes_sent, list(sh.srv.retrs)
        abors = sh.srv.commands["ABOR"]
    finally:
        sh.stop()
    got = _by_name(store)
    assert set(got) == set(m4a)
    for name, p in m4a.items():
        want = metadata.extract(p, "x")                 # the whole file, read locally
        t = got[name]
        for f in FIELDS:
            assert t.get(f) == getattr(want, f), (name, f, t.get(f), getattr(want, f))
        assert abs(t["duration"] - want.duration) < 1e-6, name
        assert t["file_size"] == p.stat().st_size, name
        assert bool(t.get("cover_art")) == bool(want.cover_art), name
        # ALAC's bitrate is size / length: the whole file's, not the window's
        assert abs((t.get("bitrate") or 0) - (want.bitrate or 0)) <= 1, (name, t.get("bitrate"),
                                                                          want.bitrate)
    assert got["lossless.m4a"]["format"] == "ALAC"
    assert got["end.m4a"]["cover_art"] and got["front.m4a"]["cover_art"]
    # Bytes: the big files' audio never crossed; the short one was read whole.
    big = sum(m4a[n].stat().st_size for n in ("end.m4a", "front.m4a", "lossless.m4a"))
    assert sent < 0.25 * big + m4a["short.m4a"].stat().st_size + 1_000_000, (sent, big)
    reads = {}
    for path, off in retrs:
        reads.setdefault(path.rsplit("/", 1)[-1], []).append(off)
    assert reads["short.m4a"] == [0]                    # (small: one whole read)
    # index at the end: the front, then ONE read of the end of the file
    assert len(reads["end.m4a"]) == 2 and reads["end.m4a"][0] == 0 and reads["end.m4a"][1] > 0
    assert len(reads["lossless.m4a"]) == 2
    # index at the front, larger than the front read (it holds the cover):
    # the front, then only the rest of the index
    assert len(reads["front.m4a"]) == 2 and reads["front.m4a"][1] == tag_window.MP4_FRONT
    # Early stops (ABOR): each front read, and the rest of a front index; a
    # read of the end of the file runs to EOF and needs none.
    assert abors == 1 + 1 + 2, abors


@needs_ffmpeg
@pytest.mark.parametrize("window", ["none", "no-index"])
async def test_m4a_falls_back_to_the_whole_file(blank, m4a, tmp_path, monkeypatch, window):
    """An unexpected layout (``mp4_window`` → None), or a window the tags
    can't be read from, is followed by the whole-file read."""
    store = blank
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    shutil.copy(m4a["end.m4a"], lib / "A" / "end.m4a")
    _age(lib)
    real = tag_window.mp4_window
    calls = []

    def fake(source, path, file_size=0, *, lane="scan"):
        calls.append(path)
        if window == "none":
            return None
        whole = real(source, path, file_size, lane=lane)
        return whole[:whole.index(b"mdat") + 4]          # ftyp + mdat, no moov
    monkeypatch.setattr(tag_window, "mp4_window", fake)
    sh = _Ftp(lib)
    try:
        await scanner.start_remote_scan("", sh.root, sh.src)
        retrs = list(sh.srv.retrs)
    finally:
        sh.stop()
    assert calls == ["/A/end.m4a"]
    t = _by_name(store)["end.m4a"]
    assert t["title"] == "Index Song" and t["cover_art"]
    assert abs(t["duration"] - metadata.extract(m4a["end.m4a"], "x").duration) < 1e-6
    assert ("/lib/A/end.m4a", 0) in retrs and retrs[-1] == ("/lib/A/end.m4a", 0)


class _Ranges(FileSource):
    """A local file as a share with real range reads, counting them."""

    def __init__(self, base: Path):
        self.base, self.reads = base, []

    def list_dir(self, path):
        raise NotImplementedError

    def walk(self, top):
        raise NotImplementedError

    def is_dir(self, path):
        return False

    def stat(self, path):
        raise NotImplementedError

    def read_file(self, path, *, lane="stream"):
        self.reads.append(("file", 0, -1))
        return (self.base / path.lstrip("/")).read_bytes()

    def read_partial(self, path, max_bytes, *, lane="scan"):
        return self.read_at(path, 0, max_bytes, lane=lane)

    def read_at(self, path, offset, length, *, lane="scan"):
        self.reads.append(("at", offset, length))
        with open(self.base / path.lstrip("/"), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)


@needs_ffmpeg
def test_mp4_window_reads_the_end_of_the_file_once(m4a):
    for name, n_reads in (("end.m4a", 2), ("lossless.m4a", 2), ("front.m4a", 2)):
        src = _Ranges(m4a[name].parent)
        size = m4a[name].stat().st_size
        win = tag_window.mp4_window(src, f"/{name}", size)
        assert win is not None and len(src.reads) == n_reads, (name, src.reads)
        assert all(kind == "at" for kind, _o, _n in src.reads)
        if name != "front.m4a":                         # the tail read runs to the end
            _k, off, length = src.reads[1]
            assert off + length == size + 1, (name, src.reads)   # (one past: runs to EOF)
        # what mutagen reads of it is the whole file's
        tmp = m4a[name].parent / f"win_{name}"
        tmp.write_bytes(win)
        try:
            a, b = metadata.extract(m4a[name], "x"), metadata.extract(tmp, "x")
        finally:
            tmp.unlink()
        for f in ("title", "artist", "album", "format", "duration", "cover_art", "sample_rate"):
            assert getattr(a, f) == getattr(b, f), (name, f)


# ── 2. folders listed several at once ───────────────────────────────────────

def _tree(tmp_path: Path, artists: int = 3, albums: int = 8) -> Path:
    lib = tmp_path / "tree"
    for a in range(artists):
        for b in range(albums):
            d = lib / f"artist{a}" / f"album{b}"
            d.mkdir(parents=True)
            for t in range(2):
                (d / f"t{t}.mp3").write_bytes(b"x" * (100 + t))
    _age(lib)
    return lib


def _walk(src, **kw):
    return {(p, tuple(sorted(e.name for e in fl))) for p, _dl, fl in src.walk_with_stat("/", **kw)}


def test_walk_lists_several_folders_at_once(tmp_path, monkeypatch):
    lib = _tree(tmp_path)
    sh = _Ftp(lib)
    try:
        live, peak, lock = [0], [0], threading.Lock()
        real = sh.src._list_entries

        def slow(path, *a, **k):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                time.sleep(0.02)
                return real(path, *a, **k)
            finally:
                with lock:
                    live[0] -= 1
        monkeypatch.setattr(sh.src, "_list_entries", slow)
        assert sh.src._walk_width() == filesource._FTP_WALK_WIDTH == 4
        par = _walk(sh.src)
        assert peak[0] == 4                             # (never more than the width)
        monkeypatch.setattr(filesource, "_FTP_WALK_WIDTH", 1)
        peak[0] = 0
        seq = _walk(sh.src)
        assert peak[0] == 1
    finally:
        sh.stop()
    assert par == seq and len(par) == 1 + 3 + 24
    assert ("/artist2/album7", ("t0.mp3", "t1.mp3")) in par


def test_walk_order_of_events_per_folder_is_kept(tmp_path):
    """A folder is yielded before its subfolders are checked with
    ``skip_subtree_fn``, which sees what the caller recorded for it; a pruned
    subtree is never listed; failures land in ``error_sink``."""
    lib = _tree(tmp_path)
    (lib / "artist1" / "album3").chmod(0)
    sh = _Ftp(lib)
    yielded: set[str] = set()
    try:
        def skip(d):
            parent = d.path.rsplit("/", 1)[0] or "/"
            assert parent in yielded                    # its folder came first
            return d.path == "/artist0"
        errors: list = []
        for path, _dl, _fl in sh.src.walk_with_stat("/", skip_subtree_fn=skip, error_sink=errors):
            yielded.add(path)
        listed = list(sh.srv.listed)
    finally:
        (lib / "artist1" / "album3").chmod(0o755)
        sh.stop()
    assert not any("artist0/" in p for p in listed) and "/artist0" not in yielded
    assert [e[0] for e in errors] == ["/artist1/album3"]
    assert "/artist2/album0" in yielded


def test_walk_stopped_early_hands_its_connections_back(tmp_path):
    lib = _tree(tmp_path)
    sh = _Ftp(lib)
    try:
        gen = sh.src.walk_with_stat("/")
        next(gen)
        next(gen)
        t0 = time.monotonic()
        gen.close()
        assert time.monotonic() - t0 < 1.0
        deadline = time.monotonic() + 5
        while sh.src._pool.status().get("in_use", 0) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sh.src._pool.status().get("in_use", 0) == 0
        # and the share still lists
        assert len(_walk(sh.src)) == 28
    finally:
        sh.stop()


def test_walk_is_one_at_a_time_on_a_two_connection_server(tmp_path, monkeypatch):
    lib = _tree(tmp_path, 1, 2)
    sh = _Ftp(lib)
    try:
        monkeypatch.setattr(sh.src._pool, "_max_size", 2)
        assert sh.src._walk_width() == 1
    finally:
        sh.stop()


# ── 3. fewer commands per transfer ──────────────────────────────────────────

def _ops(src, n: int) -> None:
    for i in range(n):
        src.read_file(f"/artist0/album{i % 8}/t0.mp3", lane="scan")
        src.list_dir(f"/artist0/album{i % 8}")


def test_no_type_or_noop_per_transfer(tmp_path):
    lib = _tree(tmp_path)
    sh = _Ftp(lib)
    try:
        _ops(sh.src, 1)
        time.sleep(0.2)                                 # (the pool's first warm-up)
        before = dict(sh.srv.commands)
        _ops(sh.src, 10)
        after = dict(sh.srv.commands)
    finally:
        sh.stop()
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    assert delta.get("RETR") == 10 and delta.get("MLSD") == 10
    assert delta.get("TYPE", 0) == 0, delta             # was 20: TYPE I + TYPE A each round
    assert delta.get("NOOP", 0) == 0, delta             # was 20: one per borrow
    assert delta.get("LIST", 0) == 0


def test_a_connection_idle_for_long_is_checked_again(tmp_path, monkeypatch):
    lib = _tree(tmp_path)
    sh = _Ftp(lib)
    try:
        _ops(sh.src, 1)
        time.sleep(0.2)
        monkeypatch.setattr(filesource, "_FTP_TRUST_IDLE_S", 0.0)
        before = sh.srv.commands["NOOP"]
        _ops(sh.src, 3)
        assert sh.srv.commands["NOOP"] - before == 6    # one per borrow, as before
    finally:
        sh.stop()


@pytest.mark.parametrize("trust", [20.0, 0.0])
def test_a_closed_idle_connection_is_not_lent(tmp_path, monkeypatch, trust):
    """A connection whose socket became readable (the server closed it, or
    sent "421" first) is dropped without a round trip — also once it is old
    enough for a NOOP (which would read a late reply as its own); the read
    works on a new connection."""
    lib = _tree(tmp_path)
    sh = _Ftp(lib)
    try:
        _ops(sh.src, 1)
        time.sleep(0.2)
        pool = sh.src._pool
        with pool._lock:
            idle = list(pool._idle)
        assert idle
        for h in idle:
            assert h.fresh_enough(time.monotonic())
            h.conn.sock.shutdown(socket.SHUT_RD)        # readable: EOF
            assert h.has_input() and not h.fresh_enough(time.monotonic())
        monkeypatch.setattr(filesource, "_FTP_TRUST_IDLE_S", trust)
        logins, noops = sh.srv.logins, sh.srv.commands["NOOP"]
        assert sh.src.read_file("/artist1/album1/t1.mp3", lane="scan") == b"x" * 101
        assert sh.srv.logins > logins                   # a new connection
        assert sh.srv.commands["NOOP"] == noops         # none sent down a dropped one
    finally:
        sh.stop()


def test_a_late_reply_waiting_counts_as_input():
    a, b = socket.socketpair()
    try:
        class C:
            sock = a
        h = filesource._PooledFTP(C())
        assert not h.has_input() and h.fresh_enough(time.monotonic())
        b.sendall(b"226 Transfer complete.\r\n")
        time.sleep(0.05)
        assert h.has_input() and not h.fresh_enough(time.monotonic())
        C.sock = None
        assert h.has_input()
    finally:
        a.close()
        b.close()


def test_mlsd_asked_again_in_ascii_by_a_server_that_wants_it(tmp_path, monkeypatch):
    monkeypatch.setattr(filesource, "_MLSD_NEEDS_ASCII", set())
    lib = _tree(tmp_path, 1, 2)
    sh = _Ftp(lib, mlsd_ascii_only=True)
    try:
        sh.src.read_file("/artist0/album0/t0.mp3", lane="scan")       # now in TYPE I
        got = sh.src.list_dir("/artist0/album0")
        assert sorted(e.name for e in got) == ["t0.mp3", "t1.mp3"]
        assert ("127.0.0.1", sh.srv.port) in filesource._MLSD_NEEDS_ASCII
        assert sh.srv.commands["LIST"] == 0
        assert all(e.mtime == int(e.mtime) and e.mtime > 0 for e in got)   # MLSD's times
        before = dict(sh.srv.commands)
        sh.src.read_file("/artist0/album0/t1.mp3", lane="scan")
        sh.src.list_dir("/artist0/album1")
        assert sh.srv.commands["TYPE"] - before.get("TYPE", 0) == 2      # I, then A again
        assert sh.srv.commands["MLSD"] - before.get("MLSD", 0) == 1      # no refused try
    finally:
        sh.stop()


def test_type_tracking_on_its_own():
    f = filesource._TypedFTP()
    sent = []
    f.putcmd = lambda line: sent.append(line)
    f.getresp = lambda: "200 ok"
    f.voidresp = lambda: "200 ok"
    f.voidcmd("TYPE I")
    f.voidcmd("TYPE I")
    f.sendcmd("TYPE I")
    f.sendcmd("TYPE A")
    f.voidcmd("NOOP")
    assert sent == ["TYPE I", "TYPE A", "NOOP"]
    # a refused mode change leaves the mode unknown: asked again next time
    def refuse():
        raise filesource.ftplib.error_perm("504 no")
    f.getresp = refuse
    with pytest.raises(filesource.ftplib.error_perm):
        f.sendcmd("TYPE I")
    f.getresp = lambda: "200 ok"
    f.sendcmd("TYPE A")
    assert sent[-1] == "TYPE A"


# ── 4. the folder state across restarts ─────────────────────────────────────

ROOT = "ftp://h/share"


def _state(root: str = ROOT) -> None:
    plan = remote_dirstate.WalkPlan(root, cap=None)
    plan.record("/", [DirEntry("A", "/A", True, 0, 1000.0)], [DirEntry("x", "/x", False, 1, 9.0)],
                failed=False)
    plan.record("/A", [], [DirEntry("a.mp3", "/A/a.mp3", False, 5, 9.0)], failed=False)
    plan.skip(DirEntry("A", "/A", True, 0, 1000.0))
    plan.finish()


def _restart(monkeypatch) -> None:
    remote_dirstate.flush()
    _reset_dirstate(monkeypatch)
    remote_dirstate.preload()


def test_state_kept_across_a_restart(tmp_data_dir, monkeypatch):
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 7)
    remote_dirstate.preload()                           # (the server's start: persistence on)
    _state()
    before = {k: (d.mtime, d.subdirs, d.sig, d.n, d.listed, d.fast)
              for k, d in remote_dirstate._get(ROOT).dirs.items()}
    _restart(monkeypatch)
    assert remote_dirstate.has_state(ROOT)
    plan = remote_dirstate.WalkPlan(ROOT, cap=time.time() - 60)
    assert plan.fast                                    # (was: full after every restart)
    after = {k: (d.mtime, d.subdirs, d.sig, d.n, d.listed, d.fast)
             for k, d in plan.share.dirs.items()}
    assert after == before and after["/"][1] == ("/A",) and after["/A"][3] == 1
    assert plan.seq == max(v[4] for v in before.values()) + 1


@pytest.mark.parametrize("yes, no", [(remote_dirstate._PROPAGATION_MIN_YES + 2, 0), (4, 1)])
def test_the_propagation_verdict_is_learned_again_after_a_restart(tmp_data_dir, monkeypatch,
                                                                   yes, no):
    """Both verdicts ("propagates" and "doesn't") start over at each start;
    the folders themselves are kept."""
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 7)
    remote_dirstate.preload()
    _state()
    sh = remote_dirstate._get(ROOT)
    sh.prop_yes, sh.prop_no = yes, no
    assert sh.propagates == (no == 0)
    remote_dirstate._save_soon()
    _restart(monkeypatch)
    raw = json.loads((tmp_data_dir / remote_dirstate._FILE).read_text())
    assert not {"yes", "no"} & set(raw["shares"][ROOT])
    plan = remote_dirstate.WalkPlan(ROOT, cap=time.time() - 60)
    assert plan.fast and "/A" in plan.share.dirs           # (the folders are kept)
    assert (plan.share.prop_yes, plan.share.prop_no) == (0, 0)
    assert not plan.share.propagates


def test_state_dropped_when_the_library_changed(tmp_data_dir, monkeypatch):
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 7)
    remote_dirstate.preload()
    _state()
    _restart(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 6)   # one lost
    assert not remote_dirstate.WalkPlan(ROOT, cap=time.time() - 60).fast
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: None)
    _state("ftp://h/other")
    remote_dirstate.flush()
    raw = json.loads((tmp_data_dir / remote_dirstate._FILE).read_text())
    assert "ftp://h/other" not in raw["shares"]         # (no count: not saved)


def test_unused_shares_stay_saved_and_forget_removes(tmp_data_dir, monkeypatch):
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 3)
    remote_dirstate.preload()
    _state(ROOT)
    _state("ftp://h/two")
    _restart(monkeypatch)
    _state("ftp://h/two")                               # only this one used this run
    remote_dirstate.flush()
    raw = json.loads((tmp_data_dir / remote_dirstate._FILE).read_text())
    assert set(raw["shares"]) == {ROOT, "ftp://h/two"}
    remote_dirstate.forget(ROOT)
    remote_dirstate.flush()
    raw = json.loads((tmp_data_dir / remote_dirstate._FILE).read_text())
    assert set(raw["shares"]) == {"ftp://h/two"}


def test_no_file_without_preload_and_a_damaged_file_is_ignored(tmp_data_dir, monkeypatch):
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 3)
    _state()
    remote_dirstate.flush()
    assert not (tmp_data_dir / remote_dirstate._FILE).exists()
    (tmp_data_dir / remote_dirstate._FILE).write_text("{not json")
    _reset_dirstate(monkeypatch)
    remote_dirstate.preload()
    assert not remote_dirstate.has_state(ROOT)
    (tmp_data_dir / remote_dirstate._FILE).write_text(json.dumps(
        {"v": 1, "shares": {ROOT: {"tracks": 3, "dirs": [["/", "x"]]}}}))
    _reset_dirstate(monkeypatch)
    remote_dirstate.preload()
    assert not remote_dirstate.has_state(ROOT)


def test_names_sig():
    s = remote_dirstate.names_sig
    assert s(["b", "a"]) == s(frozenset({"a", "b"}))
    assert s(["a", "b"]) != s(["a", "b", "c"]) != s(["ab"])
    assert s(["a\udcff"])                               # (latin-1 / surrogate names)


async def test_first_poll_after_a_restart_is_a_fast_walk(blank, tmp_path, monkeypatch):
    """Through a real scan: a full walk, a restart, then the background
    check's fast walk lists a fraction of the folders — and keeps every
    track."""
    store = blank
    lib = _tree(tmp_path, 2, 25)                        # 1 + 2 + 50 folders
    sh = _Ftp(lib)
    try:
        remote_dirstate.preload()
        await scanner.start_remote_scan("", sh.root, sh.src)
        n = len(store._tracks)
        assert n == 100
        full = len(sh.srv.listed)
        _restart(monkeypatch)
        sh.srv.listed.clear()
        await scanner.start_remote_scan("", sh.root, sh.src, dir_mtime_cap=time.time() - 60)
        fast = len(sh.srv.listed)
    finally:
        sh.stop()
    assert full == 53
    assert fast < full / 2, (fast, full)                # root + artists + the rotation
    assert len(store._tracks) == n


def test_a_failed_listing_is_matched_to_its_folder_not_to_the_next_one(tmp_path, monkeypatch):
    """Listings run at once, so a fast-failing folder's error is reported
    while an earlier-started, slower folder is yielded first.  The failed
    folder must still count as failed — no "holds no image" record, no
    empty listing in the walk plan — however the errors interleave."""
    from soniqboom.core import folder_art_index as fai
    lib = tmp_path / "tree"
    for d in ("a", "b", "c", "d"):
        (lib / d).mkdir(parents=True)
        (lib / d / "cover.jpg").write_bytes(b"jpg")
        (lib / d / "t.mp3").write_bytes(b"x" * 100)
    _age(lib)
    sh = _Ftp(lib)
    try:
        real = sh.src._list_entries

        def patched(path, *a, error_sink=None, **k):
            if path == "/d":                            # started first, slow
                time.sleep(0.3)
            if path == "/c":                            # fails at once
                if error_sink is not None:
                    error_sink.append(("/c", "simulated LIST failure"))
                return []
            return real(path, *a, error_sink=error_sink, **k)
        monkeypatch.setattr(sh.src, "_list_entries", patched)
        recorded = []
        monkeypatch.setattr(fai, "record", lambda key, img, **kw: recorded.append((key, img)))
        monkeypatch.setattr(fai, "folder_image_names", lambda: ["cover.jpg"])
        plan = remote_dirstate.WalkPlan(sh.root, cap=None)
        _entries, _pruned, _ok, failed = scanner._find_remote_audio_entries(
            sh.root, sh.src, walk_plan=plan, commit_plan=False)
    finally:
        sh.stop()
    imgs = {k: img for k, img in recorded}
    c_key = fai.key_for_remote_dir(sh.root, "/c")
    assert c_key not in imgs, imgs                       # (was: recorded as "no image")
    for d in ("a", "b", "d"):
        assert imgs[fai.key_for_remote_dir(sh.root, f"/{d}")] == "cover.jpg"
    assert "/c" in plan.failed and "/c" not in plan.listed
    assert "/c" in {remote_dirstate.norm(p) for p in failed}


def test_mlsd_refusals_that_say_nothing_about_the_mode_are_not_retried(monkeypatch):
    """Only a 5xx other than 550 / 500 / 502 to an MLSD in binary is asked
    again in ASCII (and remembered); a 4xx (passing trouble) and 500 / 502
    (no MLSD at all) are raised as before and remember nothing."""
    monkeypatch.setattr(filesource, "_MLSD_NEEDS_ASCII", set())
    for reply, retried in (("504 MLSD only in ASCII mode", True),
                           ("425 Can't open data connection", False),
                           ("450 Busy", False),
                           ("500 MLSD not understood", False),
                           ("502 Not implemented", False),
                           ("550 No such folder", False)):
        f = filesource._TypedFTP()
        f.host, f.port = "h", 21
        f._sb_type = "I"
        calls = []

        def fake_retrlines(self, cmd, callback=None, _reply=reply):
            calls.append(self._sb_keep)
            if self._sb_keep:
                err = filesource.ftplib.error_temp if _reply[0] == "4" else filesource.ftplib.error_perm
                raise err(_reply)
            return "226 ok"
        monkeypatch.setattr(filesource.ftplib.FTP, "retrlines", fake_retrlines)
        if retried:
            assert f.retrlines("MLSD /x", lambda line: None) == "226 ok"
            assert calls == [True, False] and ("h", 21) in filesource._MLSD_NEEDS_ASCII
        else:
            with pytest.raises((filesource.ftplib.error_perm, filesource.ftplib.error_temp)):
                f.retrlines("MLSD /x", lambda line: None)
            assert calls == [True], reply
        filesource._MLSD_NEEDS_ASCII.clear()


def test_idle_connection_check_works_past_descriptor_1023(tmp_path):
    """``has_input`` is a non-blocking peek — ``select`` failed for
    descriptors from 1024 up and dropped every idle connection."""
    import resource
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 1300:
        pytest.skip("open-file limit too low for descriptors past 1023")
    hold = []
    try:
        while len(hold) < 1100:
            hold.append(os.open(os.devnull, os.O_RDONLY))
        lib = _tree(tmp_path, 1, 2)
        sh = _Ftp(lib)
        try:
            for _ in range(10):
                sh.src.read_file("/artist0/album0/t0.mp3", lane="scan")
            with sh.src._pool._lock:
                h = sh.src._pool._idle[0]
            assert h.conn.sock.fileno() >= 1024
            assert not h.has_input() and h.fresh_enough(time.monotonic())
            assert sh.srv.logins <= 3, sh.srv.logins    # (was one login per read)
        finally:
            sh.stop()
    finally:
        for fd in hold:
            os.close(fd)


def test_saving_drops_long_unused_shares_and_stale_temp_files(tmp_data_dir, monkeypatch):
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 2)
    remote_dirstate.preload()
    _state(ROOT)
    _state("ftp://h/gone")
    remote_dirstate.flush()
    raw = json.loads((tmp_data_dir / remote_dirstate._FILE).read_text())
    raw["shares"]["ftp://h/gone"]["at"] = time.time() - 61 * 86400
    (tmp_data_dir / remote_dirstate._FILE).write_text(json.dumps(raw))
    stale = tmp_data_dir / f"{remote_dirstate._FILE}.999.tmp"
    stale.write_text("partial")
    _reset_dirstate(monkeypatch)
    remote_dirstate.preload()
    assert not stale.exists()
    _state(ROOT)                                       # only ROOT used this run
    remote_dirstate.flush()
    raw = json.loads((tmp_data_dir / remote_dirstate._FILE).read_text())
    assert set(raw["shares"]) == {ROOT}


def test_flush_waits_for_a_save_in_progress(tmp_data_dir, monkeypatch):
    _reset_dirstate(monkeypatch)
    monkeypatch.setattr(remote_dirstate, "_tracks_stored", lambda root: 2)
    remote_dirstate.preload()
    _state(ROOT)
    gate, started = threading.Event(), threading.Event()
    real = remote_dirstate._share_out

    def slow(st, count):
        started.set()
        gate.wait(5)
        return real(st, count)
    monkeypatch.setattr(remote_dirstate, "_share_out", slow)
    t = threading.Thread(target=remote_dirstate._save)
    t.start()
    assert started.wait(5)
    done = threading.Event()
    threading.Thread(target=lambda: (remote_dirstate.flush(), done.set())).start()
    time.sleep(0.2)
    assert not done.is_set()                            # waits for the running save
    gate.set()
    assert done.wait(5)
    t.join(5)
    assert (tmp_data_dir / remote_dirstate._FILE).exists()


def test_stat_and_is_dir_still_check_a_reused_connection(tmp_path):
    """They have no retry: a silently dead connection would read as a
    missing file / an unreachable share, so they NOOP a reused one."""
    lib = _tree(tmp_path, 1, 2)
    sh = _Ftp(lib)
    try:
        _ops(sh.src, 1)
        time.sleep(0.2)
        before = sh.srv.commands["NOOP"]
        assert sh.src.is_dir("/artist0")
        assert sh.src.stat("/artist0/album0/t1.mp3").size == 101
        assert sh.srv.commands["NOOP"] - before == 2
        before = sh.srv.commands["NOOP"]
        sh.src.read_file("/artist0/album0/t0.mp3", lane="scan")      # (a download doesn't)
        assert sh.srv.commands["NOOP"] == before
    finally:
        sh.stop()
