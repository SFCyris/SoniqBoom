# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remote playback starts from a partial download.

* The remote cache streams downloads into a ``.part`` file and publishes them
  while they run; ``open_progressive`` attaches a reader to the head of the
  file (exact final size known) — except for a FLAC without a SEEKTABLE,
  whose cached copy gets one inserted (that shifts every audio byte).
* ``stream_track`` serves such a track from the growing file (200 / 206
  against the final size) and answers a seek far past the downloaded prefix
  straight from the share.
* A member of a remote ZIP is read by ranges — the central directory and the
  member's bytes, not the whole archive.
* FTP: an ``ABOR`` no longer leaves a reply behind (every later reply on the
  pooled connection used to be off by one: SIZE answered 0, connections were
  burned).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402
from _webdav_server import DavServer  # noqa: E402

from soniqboom.core import remote_cache, remote_zip  # noqa: E402
from soniqboom.core.filesource import (  # noqa: E402
    FileSource, FileStat, FTPFileSource, SourceStream, register_source, remove_source,
)
from soniqboom.core.filesource_webdav import WebDAVFileSource  # noqa: E402
from soniqboom.core.remote_cache import ProgressiveRead, RemoteCache  # noqa: E402

TESTDATA = Path(__file__).resolve().parents[1] / "internal" / "testdata"


def _flac_bytes(blocks: list[tuple[int, bytes]], audio: bytes = b"\xff\xf8" * 64) -> bytes:
    """A FLAC-shaped byte string: magic + metadata blocks + 'audio'."""
    out = bytearray(b"fLaC")
    for i, (btype, body) in enumerate(blocks):
        last = 0x80 if i == len(blocks) - 1 else 0
        out += bytes([last | btype]) + struct.pack(">I", len(body))[1:] + body
    return bytes(out) + audio


@pytest.fixture
def cache(tmp_data_dir, monkeypatch):
    # A private remote cache (the module singleton is restored afterwards) in
    # a private data dir (archive extracts land there too).
    c = RemoteCache(tmp_data_dir / "remote-cache", 2048)
    monkeypatch.setattr(remote_cache, "_cache", c)
    remote_zip.clear_blocks()
    getattr(remote_zip, "_revalidated", {}).clear()
    yield c
    remote_zip.clear_blocks()
    getattr(remote_zip, "_revalidated", {}).clear()
    if c._dl_pool is not None:
        c._dl_pool.shutdown(wait=False, cancel_futures=True)


def _drop_ftp_pools(port: int) -> None:
    """Close the pooled connections a test opened to its own FTP server."""
    from soniqboom.core import filesource
    with filesource._FTP_POOLS_LOCK:
        keys = [k for k in filesource._FTP_POOLS if k[1] == port]
        pools = [filesource._FTP_POOLS.pop(k) for k in keys]
    for p in pools:
        p.close_all()


@pytest.fixture
def big(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    data = os.urandom(6 * 1024 * 1024 + 4321)
    (root / "big.mp3").write_bytes(data)
    return root, data


# ── the FLAC verdict on a partial download ──────────────────────────────────

def test_flac_seektable_verdict_on_a_prefix(tmp_path):
    from soniqboom.core.remote_cache import _flac_has_seektable, _flac_seektable_in_prefix
    with_st = _flac_bytes([(0, b"\0" * 34), (6, b"\0" * 3000), (3, b"\0" * 18)])
    without = _flac_bytes([(0, b"\0" * 34), (4, b"\0" * 100)])
    for name, data, final in (("a.flac", with_st, True), ("b.flac", without, False)):
        p = tmp_path / name
        p.write_bytes(data)
        assert _flac_seektable_in_prefix(p, 3) is None
        assert _flac_seektable_in_prefix(p, 30) is None          # inside STREAMINFO
        assert _flac_seektable_in_prefix(p, len(data)) is final
        assert _flac_has_seektable(p) is final                   # the cache's own verdict agrees
    # A big PICTURE before the SEEKTABLE: undecided until its end has arrived.
    p = tmp_path / "a.flac"
    assert _flac_seektable_in_prefix(p, 3000) is None
    assert _flac_seektable_in_prefix(p, 3050) is True
    p = tmp_path / "id3.flac"
    p.write_bytes(b"ID3\x04" + with_st)
    assert _flac_seektable_in_prefix(p, 100) == "not-flac"


# ── streamed downloads ──────────────────────────────────────────────────────

class _FlakySource(FileSource):
    """Serves ``data``; the first stream breaks after ``break_at`` bytes."""

    def __init__(self, data: bytes, break_at: int | None = None, grow: bool = False):
        self.data, self.break_at, self.grow = data, break_at, grow
        self.opens: list[int] = []

    def open_stream(self, path, *, offset=0, lane="stream", length=None):
        self.opens.append(offset)
        src = self

        class _S(SourceStream):
            def __init__(self):
                self.size = len(src.data) + (1 if (src.grow and len(src.opens) > 1) else 0)
                self.pos = offset

            def read(self, n=1 << 20):
                if src.break_at is not None and len(src.opens) == 1 and self.pos >= src.break_at:
                    raise ConnectionResetError("link dropped")
                stop = len(src.data)
                if src.break_at is not None and len(src.opens) == 1:
                    stop = src.break_at
                out = src.data[self.pos:min(stop, self.pos + min(n, 65536))]
                self.pos += len(out)
                return out
        return _S()

    def read_file(self, path, *, lane="stream"):
        return self.data

    def walk(self, top):
        return iter(())

    def list_dir(self, path):
        return []

    def stat(self, path):
        return FileStat(size=len(self.data))

    def is_dir(self, path):
        return True


def test_a_broken_download_resumes_where_it_stopped(cache):
    data = os.urandom(900_000)
    src = _FlakySource(data, break_at=300_000)
    p = cache.fetch("share", "/a.mp3", src)
    assert p.read_bytes() == data
    assert src.opens == [0, 300_000]                        # resumed, not restarted
    assert cache.total_size() == len(data)


def test_a_file_that_changes_during_the_download_fails_cleanly(cache):
    src = _FlakySource(os.urandom(500_000), break_at=100_000, grow=True)
    with pytest.raises(OSError, match="changed size"):
        cache.fetch("share", "/a.mp3", src)
    assert cache.get_cached("share", "/a.mp3") is None
    assert cache.total_size() == 0
    assert not list(cache._root.glob("*.part"))


@pytest.mark.parametrize("proto", ["webdav", "ftp"])
def test_open_progressive_attaches_before_the_download_ends(cache, big, proto):
    root, data = big
    if proto == "webdav":
        srv = DavServer(root, rate=8e6).start()
        src = WebDAVFileSource(srv.url)
    else:
        srv = FtpServer(root, rate=8e6).start()
        src = FTPFileSource("127.0.0.1", port=srv.port)
    try:
        t0 = time.monotonic()
        h = cache.open_progressive("share", "/big.mp3", src)
        attached = time.monotonic() - t0
        assert isinstance(h, ProgressiveRead)
        assert h.size == len(data) and not h.done
        assert attached < 0.5                                # the whole file takes ~0.8 s
        assert cache.total_size() == 0                       # counted only once complete
        got = bytearray()
        deadline = time.monotonic() + 30
        while len(got) < h.size and time.monotonic() < deadline:
            avail = h.written
            if avail > len(got):
                got += os.pread(h.fd, avail - len(got), len(got))
            else:
                time.sleep(0.01)
        os.close(h.fd)
        assert bytes(got) == data
        for _ in range(100):
            if h.done:
                break
            time.sleep(0.02)
        assert h.ok and h.final is not None
        assert cache.get_cached("share", "/big.mp3") == h.final
        assert cache.total_size() == len(data)
        # Warm: the cached copy, no new download.
        assert cache.open_progressive("share", "/big.mp3", src) == h.final
    finally:
        if proto == "ftp":
            _drop_ftp_pools(srv.port)
        srv.stop()


@pytest.mark.skipif(shutil.which("metaflac") is None, reason="needs metaflac")
def test_a_flac_without_seektable_is_never_served_while_growing(cache, tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    plain = _flac_bytes([(0, b"\0" * 34), (1, b"\0" * 4096)], audio=os.urandom(3_000_000))
    seek = _flac_bytes([(0, b"\0" * 34), (3, b"\0" * 18), (1, b"\0" * 64)],
                       audio=os.urandom(3_000_000))
    (root / "plain.flac").write_bytes(plain)
    (root / "seek.flac").write_bytes(seek)
    srv = DavServer(root, rate=6e6).start()
    try:
        src = WebDAVFileSource(srv.url)
        h = cache.open_progressive("share", "/seek.flac", src)
        assert isinstance(h, ProgressiveRead)
        os.close(h.fd)
        p = cache.open_progressive("share", "/plain.flac", src)
        assert isinstance(p, Path)                         # waited for the finished copy
        # Without metaflac nothing rewrites the file: no reason to wait.
        cache.clear_all()
        monkeypatch.setattr(remote_cache.shutil, "which", lambda name: None)
        h = cache.open_progressive("share", "/plain.flac", src)
        assert isinstance(h, ProgressiveRead)
        os.close(h.fd)
    finally:
        srv.stop()


# ── the stream endpoint ─────────────────────────────────────────────────────

async def _asgi(resp) -> tuple[int, dict, bytes]:
    msgs: list[dict] = []

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(m):
        msgs.append(m)
    await resp({"type": "http", "method": "GET", "headers": []}, receive, send)
    start = next(m for m in msgs if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, body


def _request(headers: dict | None = None):
    from starlette.requests import Request
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "headers": raw, "query_string": b"",
                    "path": "/api/stream/x"})


class _Track:
    def __init__(self, path):
        self.path = path
        self.format = ""


@pytest.fixture
def dav_share(cache, big):
    root, data = big
    srv = DavServer(root, rate=4e6).start()        # the whole file: ~1.5 s
    url = srv.url
    register_source(url, WebDAVFileSource(url))
    yield url, data, srv
    remove_source(url)
    srv.stop()


async def test_stream_serves_the_growing_file(dav_share):
    from soniqboom.api.stream import _maybe_remote_progressive
    url, data, _srv = dav_share
    t = _Track(f"{url}:/big.mp3")
    t0 = time.monotonic()
    resp = await _maybe_remote_progressive(_request(), t, target_format=None,
                                           max_bitrate_kbps=0, force_transcode=False)
    assert resp is not None and time.monotonic() - t0 < 0.5
    status, hdrs, body = await _asgi(resp)
    assert (status, hdrs["x-remote-stream"], hdrs["content-length"]) == \
        (200, "progressive", str(len(data)))
    assert body == data
    # Warm: the regular path serves the cached copy.
    assert await _maybe_remote_progressive(_request(), t, target_format=None,
                                           max_bitrate_kbps=0, force_transcode=False) is None


async def test_ranges_while_growing_and_a_far_seek_from_the_share(dav_share):
    from soniqboom.api.stream import _maybe_remote_progressive
    url, data, srv = dav_share
    t = _Track(f"{url}:/big.mp3")
    kw = dict(target_format=None, max_bitrate_kbps=0, force_transcode=False)
    resp = await _maybe_remote_progressive(_request({"Range": "bytes=0-"}), t, **kw)
    status, hdrs, body = await _asgi(resp)
    assert (status, hdrs["content-range"], body == data) == \
        (206, f"bytes 0-{len(data) - 1}/{len(data)}", True)
    # Cold again, then seek to 80 % straight away: answered from the share.
    remote_cache.get_cache().clear_all()
    far = int(len(data) * 0.8)
    t0 = time.monotonic()
    resp = await _maybe_remote_progressive(_request({"Range": f"bytes={far}-"}), t, **kw)
    status, hdrs, body = await _asgi(resp)
    assert (status, hdrs["x-remote-stream"], hdrs["content-range"]) == \
        (206, "direct", f"bytes {far}-{len(data) - 1}/{len(data)}")
    assert body == data[far:]
    # From the share: ~0.3 s; waiting for the download to get there: ~1.5 s.
    assert time.monotonic() - t0 < 0.9
    assert any(r[2].startswith(f"bytes={far}-") for r in srv.gets())


async def test_only_files_served_as_is_take_the_progressive_path(dav_share, tmp_path):
    from soniqboom.api.stream import NATIVE, _maybe_remote_progressive
    url, _data, _srv = dav_share
    kw = dict(target_format=None, max_bitrate_kbps=0, force_transcode=False)
    for path in (f"{url}:/x.mod", f"{url}:/a.zip::x.mp3", "/local/x.mp3", f"{url}:/x.m4a"):
        assert await _maybe_remote_progressive(_request(), _Track(path), **kw) is None
    t = _Track(f"{url}:/big.mp3")
    assert await _maybe_remote_progressive(
        _request(), t, target_format="ogg", max_bitrate_kbps=0, force_transcode=False) is None
    assert await _maybe_remote_progressive(
        _request(), t, target_format=None, max_bitrate_kbps=128, force_transcode=False) is None
    assert await _maybe_remote_progressive(
        _request(), t, target_format=None, max_bitrate_kbps=0, force_transcode=True) is None
    # Every format served as-is skips all the render branches of stream_track.
    from soniqboom.api import stream as st
    rendered = set().union(*(getattr(st, n) for n in (
        "_SID_EXTS", "_MIDI_EXTS", "_HVL_EXTS", "_PSF_STREAM_EXTS", "_SNDH_EXTS",
        "_YM_EXTS", "_SC68_EXTS", "_UADE_EXTS", "_ADLIB_EXTS", "_TRACKER_EXTS",
        "_GME_EXTS_STREAM", "_DSD_EXTS")))
    assert not (set(NATIVE) & rendered)


# ── FTP: a clean control channel after ABOR ────────────────────────────────

@pytest.mark.parametrize("replies", [2, 1], ids=["426+226", "226-only"])
def test_ftp_reads_keep_the_control_channel_in_step(tmp_path, replies):
    (tmp_path / "m").mkdir()
    data = os.urandom(2 * 1024 * 1024)
    (tmp_path / "m" / "a.flac").write_bytes(data)
    srv = FtpServer(tmp_path, abor_replies=replies).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/m")
        assert src.is_dir("/")
        time.sleep(0.2)
        logins = srv.logins
        for i in range(12):
            assert src.read_at("/a.flac", i * 9999, 777) == data[i * 9999:i * 9999 + 777]
            assert src.stat("/a.flac", lane="stream").size == len(data)
            with src.open_stream("/a.flac", offset=i) as st:     # closed early → ABOR
                assert st.size == len(data) and st.read(500) == data[i:i + 500]
        # Before: every read left a reply behind → SIZE answered 0 and nearly
        # every other operation burned its connection (≈ 1 login per op).
        assert srv.logins - logins <= 4
    finally:
        _drop_ftp_pools(srv.port)
        srv.stop()


# ── remote ZIP members by range ─────────────────────────────────────────────

@pytest.fixture
def pack(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    mod = (TESTDATA / "tracker" / "actionplatform.mod")
    mod_bytes = mod.read_bytes() if mod.exists() else b"M.K." * 4000
    with zipfile.ZipFile(root / "pack.zip", "w") as z:
        z.writestr(zipfile.ZipInfo("filler/a.bin"), os.urandom(5 * 1024 * 1024))
        for i in range(50):
            z.writestr(f"mods/t{i:02d}.mod", mod_bytes, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("amiga/mdat.song", b"TFMX-SONG " + os.urandom(3000))
        z.writestr("amiga/smpl.song", os.urandom(9000))
        z.writestr("amiga/readme.txt", b"hello")
        z.writestr("amiga/other.mod", mod_bytes)
        z.writestr(zipfile.ZipInfo("filler/b.bin"), os.urandom(4 * 1024 * 1024))
    with zipfile.ZipFile(root / "small.zip", "w") as z:
        z.writestr("s.mod", mod_bytes)
    return root, mod_bytes


def test_a_remote_zip_member_reads_only_its_bytes(cache, pack):
    from soniqboom.api.stream import _archive_companion_filter
    root, mod_bytes = pack
    srv = DavServer(root).start()
    try:
        src = WebDAVFileSource(srv.url)
        size = (root / "pack.zip").stat().st_size
        b0 = srv.bytes_sent
        p = remote_zip.archive_for_member("s", "/pack.zip", "mods/t07.mod", src)
        first = srv.bytes_sent - b0
        assert zipfile.ZipFile(p).namelist() == ["mods/t07.mod"]
        assert zipfile.ZipFile(p).read("mods/t07.mod") == mod_bytes
        assert first < size / 10
        assert cache.get_cached("s", "/pack.zip") is None        # the archive wasn't fetched
        # Another member of the same archive: its central directory is cached.
        b1 = srv.bytes_sent
        remote_zip.archive_for_member("s", "/pack.zip", "mods/t40.mod", src)
        assert srv.bytes_sent - b1 < first
        # Companions travel with the member (uade halves + non-music files),
        # other music in the folder does not.
        p = remote_zip.archive_for_member(
            "s", "/pack.zip", "amiga/mdat.song.mdat", src,
            companion=_archive_companion_filter("amiga/mdat.song.mdat"))
        assert sorted(zipfile.ZipFile(p).namelist()) == \
            ["amiga/mdat.song", "amiga/readme.txt", "amiga/smpl.song"]
        # A small archive is simply fetched whole.
        p = remote_zip.archive_for_member("s", "/small.zip", "s.mod", src)
        assert p == cache.get_cached("s", "/small.zip")
    finally:
        srv.stop()


async def test_a_ranged_subset_extracts_like_the_archive(cache, pack):
    from soniqboom.api.stream import _get_or_extract_zip_member
    root, mod_bytes = pack
    srv = DavServer(root).start()
    try:
        src = WebDAVFileSource(srv.url)
        p = remote_zip.archive_for_member("s", "/pack.zip", "mods/t03.mod", src)
        out = await _get_or_extract_zip_member(f"{p}::mods/t03.mod", "t-subset-1")
        assert out is not None and Path(out).read_bytes() == mod_bytes
    finally:
        srv.stop()


def test_a_ranged_read_that_fails_falls_back_to_the_whole_archive(cache, pack):
    root, mod_bytes = pack
    srv = DavServer(root).start()
    try:
        src = WebDAVFileSource(srv.url)

        def _boom(*a, **k):
            raise OSError("ranges unsupported")
        src.read_at = _boom
        p = remote_zip.archive_for_member("s", "/pack.zip", "mods/t01.mod", src)
        assert p == cache.get_cached("s", "/pack.zip")
        with pytest.raises(FileNotFoundError):
            remote_zip.archive_for_member("s", "/gone.zip", "x.mod", src)
    finally:
        srv.stop()


async def test_a_stalled_growing_range_ends_short_instead_of_zeros(cache, tmp_path, monkeypatch):
    """Compressed audio must never be padded with zeros: a range the download
    doesn't reach in time ends short (the client retries)."""
    from soniqboom.api import stream
    from soniqboom.api.stream import _maybe_remote_progressive
    root = tmp_path / "slow"
    root.mkdir()
    data = os.urandom(400_000)
    (root / "s.mp3").write_bytes(data)
    srv = DavServer(root, rate=40e3).start()          # the whole file: ~10 s
    url = srv.url
    register_source(url, WebDAVFileSource(url))
    monkeypatch.setattr(stream, "_GROWING_READ_TIMEOUT", 0.4)
    try:
        resp = await _maybe_remote_progressive(
            _request({"Range": "bytes=300000-399999"}), _Track(f"{url}:/s.mp3"),
            target_format=None, max_bitrate_kbps=0, force_transcode=False)
        status, hdrs, body = await _asgi(resp)
        assert (status, hdrs["content-length"]) == (206, "100000")
        # Before: 100000 zero bytes after the timeout.  Now: what had arrived
        # (here nothing), every byte the file's own.
        assert len(body) < 100000
        assert body == data[300000:300000 + len(body)]
    finally:
        remove_source(url)
        srv.stop()


def test_a_subset_carries_sonix_instruments_and_parent_folder_banks(cache, tmp_path):
    from soniqboom.api.stream import _archive_companion_filter
    root = tmp_path / "share"
    root.mkdir()
    with zipfile.ZipFile(root / "pack.zip", "w") as z:
        z.writestr(zipfile.ZipInfo("filler.bin"), os.urandom(5 * 1024 * 1024))
        z.writestr("Music/Sonix/song.smus", b"FORM" + os.urandom(2000))
        z.writestr("Music/Sonix/Instruments/piano.instr", os.urandom(500))
        z.writestr("Music/Sonix/Instruments/piano.ss", os.urandom(800))
        z.writestr("Rol/standard.bnk", os.urandom(300))
        z.writestr("Rol/Disk1/tune.rol", os.urandom(1500))
        z.writestr("Rol/Disk1/other.rol", os.urandom(1500))
    srv = DavServer(root).start()
    try:
        src = WebDAVFileSource(srv.url)
        p = remote_zip.archive_for_member("s", "/pack.zip", "Music/Sonix/song.smus", src)
        assert sorted(zipfile.ZipFile(p).namelist()) == [
            "Music/Sonix/Instruments/piano.instr", "Music/Sonix/Instruments/piano.ss",
            "Music/Sonix/song.smus"]
        p = remote_zip.archive_for_member(
            "s", "/pack.zip", "Rol/Disk1/tune.rol", src,
            companion=_archive_companion_filter("Rol/Disk1/tune.rol"))
        assert sorted(zipfile.ZipFile(p).namelist()) == ["Rol/Disk1/tune.rol",
                                                        "Rol/standard.bnk"]
    finally:
        srv.stop()


def test_smb_reads_share_the_file_with_other_readers(monkeypatch):
    """smbclient opens exclusively by default — a second reader of the same
    file (a seek from the share while the download runs) was refused."""
    import io
    import smbclient
    from soniqboom.core.filesource import SMBFileSource
    seen = []

    def _open(path, mode="r", share_access=None, **kw):
        seen.append(share_access)
        return io.BytesIO(b"0123456789")
    monkeypatch.setattr(smbclient, "open_file", _open)
    src = SMBFileSource("nas", "Music")
    src._registered = True
    assert src.read_file("/a.mp3") == b"0123456789"
    assert src.read_at("/a.mp3", 3, 4) == b"3456"
    with src.open_stream("/a.mp3", offset=8) as st:
        assert (st.size, st.read(10)) == (10, b"89")
    assert seen == ["r", "r", "r"]


# ── a cached subset needs nothing from the share ────────────────────────────

class _CountingZipSource(FileSource):
    """A remote ZIP served from memory; counts calls; can go down."""

    def __init__(self, data: bytes, *, strict_kw: bool = True):
        self.data = data
        self.calls = {"stat": 0, "read_at": 0, "open_stream": 0}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionRefusedError("share down")

    def stat(self, path, *, lane="scan", strict=False):
        self.calls["stat"] += 1
        if self.down:
            if strict:
                raise ConnectionRefusedError("share down")
            return FileStat()               # what FTP's stat answers on ANY error
        return FileStat(size=len(self.data), mtime=1000.0)

    def read_at(self, path, offset, length, *, lane="scan"):
        self.calls["read_at"] += 1
        self._check()
        return self.data[offset:offset + length]

    def open_stream(self, path, *, offset=0, lane="stream", length=None):
        self.calls["open_stream"] += 1
        self._check()
        from soniqboom.core.filesource import _BytesStream
        return _BytesStream(self.data, offset)

    def read_file(self, path, *, lane="stream"):
        self._check()
        return self.data

    def walk(self, top):
        return iter(())

    def list_dir(self, path):
        return []

    def is_dir(self, path):
        return True


def _zip_bytes(extra: dict | None = None) -> bytes:
    import io
    import random
    buf = io.BytesIO()
    rnd = random.Random(1)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i in range(6):
            z.writestr(f"other/big{i}.bin", rnd.randbytes(1024 * 1024))
        z.writestr("pack/tune.mod", b"M.K." * 2000)
        z.writestr("pack/next.mod", b"M.K." * 2100)
        z.writestr("standard.bnk", b"BNK" * 3000)          # AdLib bank, parent folder
        z.writestr("rol/song.rol", b"\x04\x00" * 2000)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


def test_a_cached_subset_plays_without_asking_the_share(cache):
    from soniqboom.api.stream import _remote_bytes_local
    src = _CountingZipSource(_zip_bytes())
    p1 = remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod", src)
    assert zipfile.ZipFile(p1).namelist() == ["pack/tune.mod"]
    assert src.calls["stat"] == 1 and src.calls["open_stream"] == 0
    # Again: before, every play paid a live SIZE / PROPFIND first.
    for _ in range(3):
        assert remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod",
                                             src) == p1
    assert src.calls["stat"] == 1
    # The share goes down: the cached subset still plays (before: a stat
    # failure read as "size 0" and the whole archive was downloaded).
    src.down = True
    assert remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod",
                                         src) == p1
    assert src.calls["open_stream"] == 0
    # Prewarm / lookahead / duration probes see the subset as local bytes.
    register_source("ftp://h/m", src)
    try:
        assert _remote_bytes_local("ftp://h/m:/x/pack.zip::pack/tune.mod") is True
        assert _remote_bytes_local("ftp://h/m:/x/pack.zip::pack/next.mod") is False
    finally:
        remove_source("ftp://h/m")


def test_an_unanswered_archive_stat_raises_instead_of_downloading_it_whole(cache):
    src = _CountingZipSource(_zip_bytes())
    src.down = True
    with pytest.raises(ConnectionRefusedError):
        remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod", src)
    assert src.calls["open_stream"] == 0                 # no whole-archive download
    assert cache.get_cached("ftp://h/m", "/x/pack.zip") is None


def test_ftp_stat_strict_and_without_size(tmp_path):
    from soniqboom.core.filesource import FTPFileSource
    (tmp_path / "m").mkdir()
    data = _zip_bytes()
    (tmp_path / "m" / "pack.zip").write_bytes(data)
    srv = FtpServer(tmp_path).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/m")
        assert src.stat("/pack.zip", strict=True).size == len(data)
        assert src.stat("/", strict=True).is_dir
        assert src.stat("/gone.zip", strict=True).size == 0      # can't tell: no raise
    finally:
        _drop_ftp_pools(srv.port)
        srv.stop()
    # The server is gone: lenient stat answers empty, strict raises.
    dead = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/m")
    assert dead.stat("/pack.zip") == FileStat()
    with pytest.raises(Exception):
        dead.stat("/pack.zip", strict=True)
    _drop_ftp_pools(srv.port)
    # No SIZE at all: the archive's size is unknown → fetched whole, as before.
    srv = FtpServer(tmp_path, size=False).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/m")
        assert src.stat("/pack.zip", strict=True).size == 0
    finally:
        _drop_ftp_pools(srv.port)
        srv.stop()


def test_every_caller_shares_one_subset_with_the_members_companions(cache):
    src = _CountingZipSource(_zip_bytes())
    # A companion-less caller first (read_source_bytes / art / waveform) …
    a = remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "rol/song.rol", src)
    # … then playback.  Before: playback got the first build — without its bank.
    from soniqboom.api.stream import _archive_companion_filter
    b = remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "rol/song.rol", src,
                                      companion=_archive_companion_filter("rol/song.rol"))
    assert a == b
    assert sorted(zipfile.ZipFile(b).namelist()) == ["rol/song.rol", "standard.bnk"]


def test_subsets_of_a_changed_archive_are_retired(cache, monkeypatch):
    src = _CountingZipSource(_zip_bytes())
    old = remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod", src)
    # The scanner sees another size in the listing: the subset is never
    # served again (its file stays for a play that may be extracting from
    # it right now; eviction removes it).
    cache.validate_size("ftp://h/m", "/x/pack.zip", len(src.data) + 1)
    assert cache.find_subset("ftp://h/m", "/x/pack.zip", "pack/tune.mod") is None
    assert old.exists()
    src.data = _zip_bytes({"pack/a.mod": b"M.K." * 7})
    mid = remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod", src)
    assert mid != old
    # A play serves the cached subset at once and re-checks the archive in the
    # background (throttled): a changed archive retires the subset, the next
    # play builds a new one.
    monkeypatch.setattr(remote_zip, "_REVALIDATE_S", 0.0)
    src.data = _zip_bytes({"pack/new.mod": b"M.K." * 10})
    assert remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod",
                                         src) == mid
    deadline = time.monotonic() + 5
    while (cache.find_subset("ftp://h/m", "/x/pack.zip", "pack/tune.mod", peek=True)
           is not None and time.monotonic() < deadline):
        time.sleep(0.02)
    assert cache.find_subset("ftp://h/m", "/x/pack.zip", "pack/tune.mod", peek=True) is None
    assert mid.exists()
    new = remote_zip.archive_for_member("ftp://h/m", "/x/pack.zip", "pack/tune.mod", src)
    assert new not in (old, mid) and zipfile.ZipFile(new).namelist() == ["pack/tune.mod"]
    # The subset index (and the retired flags) survive a restart.
    again = RemoteCache(cache._root, 2048)
    assert again.find_subset("ftp://h/m", "/x/pack.zip", "pack/tune.mod") == new


# ── far seeks: one share read, then the local copy for good ─────────────────

async def test_a_far_seek_switches_to_the_local_copy_once():
    import random
    from soniqboom.api import stream
    MB = 1024 * 1024
    size = 24 * MB
    data = random.Random(4).randbytes(size)
    opens, closes = [], []

    class _Prog:                   # a download at ~60 MB/s, 1 MB steps
        def __init__(self):
            self.t0 = time.monotonic()
            self.done = self.ok = False
            self.cbs = []

        @property
        def written(self):
            return min(size, int((time.monotonic() - self.t0) * 60) * MB)

        def add_listener(self, cb):
            self.cbs.append(cb)

        def remove_listener(self, cb):
            self.cbs.remove(cb)

    class _St(SourceStream):
        def __init__(self, off):
            self.size, self.pos = size, off

        def read(self, n=MB):
            time.sleep(0.03)       # ~33 MB/s share reads
            out = data[self.pos:self.pos + n]
            self.pos += len(out)
            return out

        def close(self):
            closes.append(self.pos)

    class _Src:
        def open_stream(self, p, *, offset=0, lane="stream", length=None):
            opens.append(offset)
            return _St(offset)

    import tempfile
    import threading
    stop = threading.Event()

    def _ticker():                 # the download's progress notifications
        while not stop.wait(0.02):
            for cb in list(prog.cbs):
                cb()
    with tempfile.NamedTemporaryFile() as tf:
        tf.write(data)
        tf.flush()
        fd = stream._FdHandle(os.open(tf.name, os.O_RDONLY))
        prog = _Prog()
        threading.Thread(target=_ticker, daemon=True).start()
        start = 4 * MB
        try:
            resp = await stream._remote_direct_range(prog, size, start, size - 1,
                                                     "audio/mpeg", _Src(), "/x.mp3", fd, None)
            got = bytearray()
            async for chunk in resp.body_iterator:
                got += chunk
        finally:
            stop.set()
    assert bytes(got) == data[start:]
    # Before: the body re-opened the share every time it overtook the
    # download (~10 opens / ABORs in one response).
    assert len(opens) == 1 and len(closes) == 1
    assert prog.cbs == []                                   # listener removed


async def test_an_abandoned_direct_open_leaves_the_ftp_pool_queue(tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core import filesource
    (tmp_path / "m").mkdir()
    data = os.urandom(3 * 1024 * 1024)
    (tmp_path / "m" / "a.mp3").write_bytes(data)
    srv = FtpServer(tmp_path).start()
    monkeypatch.setattr(stream, "_FAR_SEEK_OPEN_S", 0.3)
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/m")
        pool = src._pool
        await asyncio.sleep(0.3)                       # let the warm-up finish
        pool.resize(1)
        with pool.borrow(lane="stream"):              # the only connection is busy
            pool.recycle_all_idle()
            fd = stream._FdHandle(os.open(tmp_path / "m" / "a.mp3", os.O_RDONLY))

            class _P:
                written, done, ok = 0, False, False
            resp = await stream._remote_direct_range(
                _P(), len(data), 2 * 1024 * 1024, len(data) - 1, "audio/mpeg",
                src, "/a.mp3", fd, None)
            assert resp is None                        # caller waits on the download
            await asyncio.sleep(0.4)
            # Before: the open stayed queued as a priority stream waiter for
            # the pool's full 60 s, holding back every scan / browse borrow.
            assert pool._waiting_stream == 0
            fd.close()
    finally:
        _drop_ftp_pools(srv.port)
        srv.stop()


async def test_a_request_cancelled_during_the_open_closes_the_stream():
    from soniqboom.api import stream
    closed = []

    class _St(SourceStream):
        size = 10_000_000

        def read(self, n=1 << 20):
            return b"x" * n

        def close(self):
            closed.append(True)

    class _Src:
        def open_stream(self, p, *, offset=0, lane="stream", length=None):
            time.sleep(0.4)
            return _St()

    class _P:
        written, done, ok = 0, False, False
    fd = stream._FdHandle(os.open(os.devnull, os.O_RDONLY))
    task = asyncio.ensure_future(stream._remote_direct_range(
        _P(), 10_000_000, 5_000_000, 9_999_999, "audio/mpeg", _Src(), "/a.mp3", fd, None))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 3
    while not closed and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    fd.close()
    assert closed == [True]          # before: the late stream (an FTP slot) leaked


# ── downloads ───────────────────────────────────────────────────────────────

def test_ftp_without_size_never_caches_a_cut_transfer(cache, tmp_path):
    (tmp_path / "m").mkdir()
    data = os.urandom(900_000)
    (tmp_path / "m" / "a.flac").write_bytes(data)
    srv = FtpServer(tmp_path, size=False, abort_after=300_000).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/m")
        p = cache.fetch("ftp://h/m", "/a.flac", src)
        # Before: the 426 was swallowed and the first 300 000 bytes were
        # cached as the whole file.
        assert p.read_bytes() == data
        assert srv.retrs == [("/m/a.flac", 0), ("/m/a.flac", 300_000)]
    finally:
        _drop_ftp_pools(srv.port)
        srv.stop()


def test_a_resumed_download_writes_at_the_byte_it_counted(cache, monkeypatch):
    """A write that fails part-way (ENOSPC …) moved the file position past the
    counted bytes; the resumed transfer was appended there."""
    import builtins
    data = os.urandom(3 * 1024 * 1024)
    src = _FlakySource(data)
    real_open = builtins.open
    state = {"failed": False}

    class _Half:
        def __init__(self, fh):
            self.fh = fh

        def write(self, view):
            if not state["failed"] and self.fh.tell() > 1024 * 1024:
                state["failed"] = True
                self.fh.write(bytes(view[:1000]))
                raise OSError(28, "No space left on device")
            return self.fh.write(view)

        def __getattr__(self, name):
            return getattr(self.fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

    def _open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        return _Half(fh) if str(path).endswith(".part") and "w" in mode else fh
    monkeypatch.setattr(remote_cache, "open", _open, raising=False)
    p = cache.fetch("share", "/a.mp3", src)
    assert state["failed"]
    assert p.read_bytes() == data


def test_removing_a_share_stops_its_downloads(cache):
    data = os.urandom(4 * 1024 * 1024)

    class _Slow(_FlakySource):
        def open_stream(self, path, *, offset=0, lane="stream", length=None):
            st = super().open_stream(path, offset=offset, lane=lane, length=length)
            real = st.read

            def _read(n=1 << 20):
                time.sleep(0.02)
                return real(n)
            st.read = _read
            return st
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        fut = ex.submit(cache.fetch, "ftp://h/gone", "/a.mp3", _Slow(data))
        time.sleep(0.3)
        cache.invalidate_share("ftp://h/gone")
        with pytest.raises(OSError, match="cancelled"):
            fut.result(timeout=5)
    assert cache.get_cached("ftp://h/gone", "/a.mp3") is None
    assert not list(cache._root.glob("*.part")) and cache.total_size() == 0


def test_webdav_streams_ask_for_the_files_own_bytes():
    import httpx
    seen = []

    def _handler(req):
        seen.append((req.headers.get("range"), req.headers.get("accept-encoding")))
        return httpx.Response(206, content=iter([b"abcd"]),
                              headers={"content-range": "bytes 10-13/100"})
    src = WebDAVFileSource("https://dav.example.com/x")
    src._client = httpx.Client(base_url=src._base, transport=httpx.MockTransport(_handler))
    with src.open_stream("/a.flac", offset=10, length=4) as st:
        assert (st.size, st.read(10)) == (100, b"abcd")
    assert seen == [("bytes=10-13", "identity")]


async def test_cached_bytes_play_while_the_share_is_not_connected(cache):
    """A share down since startup isn't registered: its cached tracks (a
    loose file, a remote ZIP member's subset) still play."""
    from fastapi import HTTPException

    from soniqboom.api import stream
    src = _CountingZipSource(_zip_bytes())
    remote_zip.archive_for_member("ftp://h/off", "/x/pack.zip", "pack/tune.mod", src)
    loose = os.urandom(200_000)
    cache.fetch("ftp://h/off", "/a.mp3", _FlakySource(loose))
    assert remote_cache.get_cache() is cache
    # Nothing registered under ftp://h/off.
    path, ext, _named, pin = await stream._resolve_play_source(
        "t-off-1", _Track("ftp://h/off:/x/pack.zip::pack/tune.mod"))
    try:
        assert Path(path).read_bytes() == b"M.K." * 2000
    finally:
        if pin:
            stream._zip_unpin(pin)
    path, ext, _named, pin = await stream._resolve_play_source(
        "t-off-2", _Track("ftp://h/off:/a.mp3"))
    assert Path(path).read_bytes() == loose
    # Not cached: still the clear "source offline" answer.
    with pytest.raises(HTTPException) as e:
        await stream._resolve_play_source("t-off-3",
                                          _Track("ftp://h/off:/x/pack.zip::pack/next.mod"))
    assert e.value.status_code == 503
    with pytest.raises(HTTPException) as e:
        await stream._resolve_play_source("t-off-4", _Track("ftp://h/off:/b.mp3"))
    assert e.value.status_code == 503


@pytest.mark.parametrize("how", ["stalled", "failed"])
async def test_a_far_seek_finishes_from_the_share_when_the_download_stops(monkeypatch, how):
    import tempfile
    from soniqboom.api import stream
    MB = 1024 * 1024
    size = 6 * MB
    data = os.urandom(size)
    opens = []
    monkeypatch.setattr(stream, "_GROWING_READ_TIMEOUT", 0.3)

    class _Prog:                   # 3 MB arrived, then nothing more
        written = 3 * MB
        done = how == "failed"
        ok = False

    class _St(SourceStream):
        def __init__(self, off):
            self.size, self.pos = size, off

        def read(self, n=MB):
            out = data[self.pos:self.pos + n]
            self.pos += len(out)
            return out

    class _Src:
        def open_stream(self, p, *, offset=0, lane="stream", length=None):
            opens.append(offset)        # 2 MB: closed unread (3 MB are local)
            return _St(offset)

    with tempfile.NamedTemporaryFile() as tf:
        tf.write(data[:3 * MB])
        tf.flush()
        fd = stream._FdHandle(os.open(tf.name, os.O_RDONLY))
        resp = await stream._remote_direct_range(_Prog(), size, 2 * MB, size - 1,
                                                 "audio/mpeg", _Src(), "/x.mp3", fd, None)
        got = bytearray()
        async for chunk in resp.body_iterator:
            got += chunk
    assert bytes(got) == data[2 * MB:]
    # The opening read, then (local copy up to 3 MB) one re-open to the end.
    assert opens == [2 * MB, 3 * MB]


async def test_a_share_whose_sign_in_was_refused_plays_only_cached_bytes(cache):
    """While the credentials' back-off runs no play logs in with them (each
    refused login counts toward a lockout): cached bytes play, the rest is a
    clear 503."""
    from fastapi import HTTPException

    from soniqboom.api import stream
    from soniqboom.core import filesource
    src = _CountingZipSource(_zip_bytes())
    remote_zip.archive_for_member("ftp://h/ref", "/x/pack.zip", "pack/tune.mod", src)
    src.auth_key = "ftp://u@h:21"
    register_source("ftp://h/ref", src)
    calls = dict(src.calls)
    filesource.note_share_auth_failure(src.auth_key)
    try:
        path, ext, _named, pin = await stream._resolve_play_source(
            "t-ref-1", _Track("ftp://h/ref:/x/pack.zip::pack/tune.mod"))
        try:
            assert Path(path).read_bytes() == b"M.K." * 2000
        finally:
            if pin:
                stream._zip_unpin(pin)
        with pytest.raises(HTTPException) as e:
            await stream._resolve_play_source(
                "t-ref-2", _Track("ftp://h/ref:/x/pack.zip::pack/next.mod"))
        assert e.value.status_code == 503 and "Sign-in refused" in e.value.detail
        time.sleep(0.2)                       # (no background re-check either)
        assert src.calls == calls             # nothing asked the share
    finally:
        filesource.reset_share_backoff(src.auth_key)
        remove_source("ftp://h/ref")
