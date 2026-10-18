# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playback I/O: the playback thread pool, descriptors on cancelled requests,
file sniffs off the event loop, the codec-probe memo, RFC 9110 ranges and
validators, render slots of renders nobody wants, and what the next-track
prewarm prepares.

Everything runs in-process against temp files, a throw-away conversion cache
and fakes — never the live server or real data.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.core import conversion_cache


def _req(headers: dict | None = None, *, method: str = "GET", cookies: dict | None = None):
    return types.SimpleNamespace(headers={k.lower(): v for k, v in (headers or {}).items()},
                                 method=method, cookies=cookies or {})


def _fd_open(fd: int) -> bool:
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def _range_app():
    """A one-route app answering ``/f`` with ``_range_file_response``."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    path_box: dict = {}

    async def endpoint(request):
        return await stream._range_file_response(request, path_box["p"], "audio/mpeg")
    return Starlette(routes=[Route("/f", endpoint)]), path_box


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@contextlib.contextmanager
def _blocked_default_executor(loop):
    """The loop's default executor with its only thread held — what a scan of
    a slow share does to it on a small host."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="blocked")
    loop.set_default_executor(pool)
    release = threading.Event()
    held = loop.run_in_executor(None, release.wait, 30)
    try:
        yield
    finally:
        release.set()
        with contextlib.suppress(Exception):
            pool.shutdown(wait=False, cancel_futures=True)
        held.cancel()


# ── B1: the playback path never waits for the default executor ─────────────

@pytest.mark.asyncio
async def test_range_reads_do_not_wait_for_the_default_executor(tmp_path):
    f = tmp_path / "a.mp3"
    data = os.urandom(3 * 1024 * 1024)
    f.write_bytes(data)
    stream._STAT_CACHE.pop(str(f), None)
    loop = asyncio.get_running_loop()
    with _blocked_default_executor(loop):
        # cold stat + small single read
        r = await asyncio.wait_for(stream._range_file_response(
            _req({"range": "bytes=0-65535"}), f, "audio/mpeg"), timeout=3)
        assert r.status_code == 206 and r.body == data[:65536]
        # streamed (open + preads + close)
        r = await asyncio.wait_for(stream._range_file_response(
            _req({"range": "bytes=1000000-"}), f, "audio/mpeg"), timeout=3)
        body = await asyncio.wait_for(
            _collect(r.body_iterator), timeout=3)
        assert body == data[1000000:]
        # a local play's source check
        t = types.SimpleNamespace(id="b1", path=str(f), scan_root_hash="")
        got = await asyncio.wait_for(stream._resolve_play_source("b1", t), timeout=3)
        assert got[0] == f


async def _collect(it) -> bytes:
    out = bytearray()
    async for c in it:
        out += c
    return bytes(out)


@pytest.mark.asyncio
async def test_a_cached_remote_copy_is_found_without_the_default_executor(monkeypatch, tmp_path):
    local = tmp_path / "copy.mp3"
    local.write_bytes(b"x" * 1000)
    from soniqboom.core import filesource, remote_cache

    class _Cache:
        root = tmp_path / "remote-cache"

        def peek_cached(self, root, rel):
            return rel == "/a/b.mp3"

        def get_cached(self, root, rel, *, ident=None):
            return local if rel == "/a/b.mp3" else None

        def fetch(self, *a, **k):
            raise AssertionError("a cached copy is never fetched")
    monkeypatch.setattr(remote_cache, "get_cache", lambda: _Cache())
    monkeypatch.setattr(filesource, "get_source", lambda root: object())
    t = types.SimpleNamespace(id="rc", path="ftp://nas/music:/a/b.mp3", scan_root_hash="")
    loop = asyncio.get_running_loop()
    with _blocked_default_executor(loop):
        got = await asyncio.wait_for(stream._resolve_play_source("rc", t), timeout=3)
    assert got[0] == local


def test_playback_pool_is_its_own_and_restarts_after_release():
    from soniqboom.core import playback_io
    p1 = playback_io.pool()
    assert p1._thread_name_prefix == "sb-playback-io"
    assert p1._max_workers == playback_io.threads() >= 32
    playback_io.release()
    assert playback_io._POOL is None
    assert stream._pb_submit(lambda: 7).result(timeout=5) == 7      # a fresh pool
    assert playback_io._POOL is not p1


# ── B2: no descriptor outlives a cancelled request ─────────────────────────

@pytest.mark.asyncio
async def test_an_open_cancelled_while_it_runs_closes_its_descriptor(monkeypatch, tmp_path):
    f = tmp_path / "slow.wav"
    f.write_bytes(b"\0" * 1000)
    real_open = os.open
    opened: list[int] = []
    entered = threading.Event()

    def slow_open(path, flags, *a):
        if str(path) == str(f):
            entered.set()
            time.sleep(0.3)                 # the open is running when the cancel lands
            fd = real_open(path, flags, *a)
            opened.append(fd)
            return fd
        return real_open(path, flags, *a)
    monkeypatch.setattr(os, "open", slow_open)
    task = asyncio.ensure_future(stream._open_fd_handle(f))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if opened and not _fd_open(opened[0]):
            break
        await asyncio.sleep(0.02)
    assert opened and not _fd_open(opened[0])          # before: left open for good


@pytest.mark.asyncio
async def test_an_open_cancelled_while_queued_never_opens(monkeypatch, tmp_path):
    f = tmp_path / "q.wav"
    f.write_bytes(b"\0" * 1000)
    calls = []
    real_open = os.open

    def rec_open(path, flags, *a):
        if str(path) == str(f):
            calls.append(path)
        return real_open(path, flags, *a)
    monkeypatch.setattr(os, "open", rec_open)
    one = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    one.submit(gate.wait, 10)                          # the only thread is busy
    from soniqboom.core import playback_io
    monkeypatch.setattr(playback_io, "_POOL", one)
    try:
        task = asyncio.ensure_future(stream._open_fd_handle(f))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.sleep(0.2)
        assert calls == []
    finally:
        gate.set()
        one.shutdown(wait=True)


@pytest.mark.asyncio
async def test_a_body_cancelled_mid_read_closes_after_the_read(monkeypatch, tmp_path):
    f = tmp_path / "big.wav"
    f.write_bytes(os.urandom(4 * 1024 * 1024))
    real_pread = os.pread
    in_read = threading.Event()
    release = threading.Event()
    seen_fd: list[int] = []

    def slow_pread(fd, n, pos):
        if pos >= 1024 * 1024:                         # the second chunk
            seen_fd.append(fd)
            in_read.set()
            release.wait(5)
        return real_pread(fd, n, pos)
    monkeypatch.setattr(os, "pread", slow_pread)
    r = await stream._range_file_response(_req({"range": "bytes=0-"}), f, "audio/wav")
    it = r.body_iterator

    async def consume():
        async for _c in it:
            pass
    task = asyncio.ensure_future(consume())
    while not in_read.is_set():
        await asyncio.sleep(0.01)
    # The way Starlette cancels a body whose client left: again and again.
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0.01)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert _fd_open(seen_fd[0])                        # never closed under the read
    release.set()
    for _ in range(100):
        if not _fd_open(seen_fd[0]):
            break
        await asyncio.sleep(0.02)
    assert not _fd_open(seen_fd[0])                    # …and closed right after it


@pytest.mark.asyncio
async def test_aborted_range_bodies_leave_no_descriptor(tmp_path):
    f = tmp_path / "leak.wav"
    f.write_bytes(os.urandom(3 * 1024 * 1024))

    def count() -> int:
        n = 0
        for fd in range(3, 4096):
            try:
                st = os.fstat(fd)
            except OSError:
                continue
            if st.st_ino == f.stat().st_ino and st.st_dev == f.stat().st_dev:
                n += 1
        return n
    before = count()
    for i in range(40):
        r = await stream._range_file_response(_req({"range": f"bytes={i * 1000}-"}), f, "audio/wav")
        it = r.body_iterator
        task = asyncio.ensure_future(it.__anext__())
        if i % 2:
            await asyncio.sleep(0)                     # cancel before / during the open
        else:
            await task                                 # one chunk read
            task = asyncio.ensure_future(it.__anext__())
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        with contextlib.suppress(BaseException):
            await it.aclose()
    await asyncio.sleep(0.5)
    assert count() == before


# ── B3: source sniffs run off the event loop ───────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,sniff", [("t.sid", "_is_c64_sid"), ("t.dsf", "_dsf_is_dreamcast"),
                                        ("t.psf", "_psf_has_magic")])
async def test_play_time_sniffs_run_on_a_worker_thread(monkeypatch, tmp_path, name, sniff):
    f = tmp_path / name
    f.write_bytes(b"\0" * 256)
    loop_thread = threading.current_thread()
    seen = []

    class _Stop(Exception):
        pass

    def rec(path):
        seen.append(threading.current_thread() is loop_thread)
        raise _Stop()
    monkeypatch.setattr(stream, sniff, rec)
    t = types.SimpleNamespace(id="sn" + name, path=str(f), scan_root_hash="", duration=0.0,
                              format="", genre=[], title="t")

    async def get_track(tid):
        return t
    monkeypatch.setattr(stream, "get_track", get_track)

    async def _noauth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)

    async def _same(*a, **k):
        return a[1] if len(a) > 1 else t
    monkeypatch.setattr(stream, "refreshed_after_expand", _same)
    monkeypatch.setattr(stream, "_catalogue_sid_read", lambda tid, tr, ext: _same(tid, tr))
    with pytest.raises(_Stop):
        await stream.stream_track(t.id, _req(), seek=0.0, subsong=None, file_path=None,
                                  target_format=None, max_bitrate_kbps=0,
                                  target_sample_rate=0, force_transcode=False,
                                  sb_session=None, u=None, p=None, s=None, t=None)
    assert seen and not any(seen)                      # never on the loop's thread


# ── F3: the codec probe runs once per version of a file ────────────────────

@pytest.mark.asyncio
async def test_codec_probe_is_memoised_per_file_version(monkeypatch, tmp_path):
    calls: list[str] = []

    async def fake_probe(path):
        calls.append(Path(path).name)
        await asyncio.sleep(0.05)
        return {"a.m4a": "aac", "b.m4a": "alac"}.get(Path(path).name)
    monkeypatch.setattr(stream, "_ffprobe_codec", fake_probe)
    stream._CODEC_MEMO.clear()
    a, b = tmp_path / "a.m4a", tmp_path / "b.m4a"
    a.write_bytes(b"a" * 100)
    b.write_bytes(b"b" * 100)
    # cold: concurrent requests share one probe
    got = await asyncio.gather(*(stream._probe_codec(a) for _ in range(5)))
    assert got == ["aac"] * 5 and calls == ["a.m4a"]
    # warm: no probe
    assert await stream._probe_codec(a) == "aac" and calls == ["a.m4a"]
    # another file: its own probe (cold), then warm
    assert await stream._probe_codec(b) == "alac"
    assert await stream._probe_codec(b) == "alac" and calls == ["a.m4a", "b.m4a"]
    # the file changes (replaced: new size / mtime / inode): probed again
    a.unlink()
    a.write_bytes(b"A" * 200)
    os.utime(a, (time.time() + 5, time.time() + 5))
    assert await stream._probe_codec(a) == "aac" and calls == ["a.m4a", "b.m4a", "a.m4a"]


@pytest.mark.asyncio
async def test_a_failed_codec_probe_is_not_remembered(monkeypatch, tmp_path):
    results = [None, "aac"]

    async def fake_probe(path):
        return results.pop(0)
    monkeypatch.setattr(stream, "_ffprobe_codec", fake_probe)
    stream._CODEC_MEMO.clear()
    f = tmp_path / "x.m4a"
    f.write_bytes(b"x")
    assert await stream._probe_codec(f) is None
    assert await stream._probe_codec(f) == "aac"       # probed again, not the failure
    assert await stream._probe_codec(tmp_path / "missing.m4a") is None


def test_codec_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(stream, "_CODEC_MEMO_MAX", 3)
    stream._CODEC_MEMO.clear()
    loop = asyncio.new_event_loop()
    try:
        for i in range(6):
            fut = loop.create_future()
            fut.set_result("aac")
            stream._remember_codec((f"/p{i}", 1, 1, 1), fut)
    finally:
        loop.close()
    assert list(stream._CODEC_MEMO) == [("/p3", 1, 1, 1), ("/p4", 1, 1, 1), ("/p5", 1, 1, 1)]
    stream._CODEC_MEMO.clear()


@pytest.mark.asyncio
@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg missing")
async def test_real_aac_and_alac_probe_cold_then_warm(tmp_path):
    import subprocess
    aac, alac = tmp_path / "t.m4a", tmp_path / "u.m4a"
    for out, codec in ((aac, "aac"), (alac, "alac")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", codec, str(out)], check=True)
    stream._CODEC_MEMO.clear()
    t0 = time.perf_counter()
    assert await stream._probe_codec(aac) == "aac"
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert await stream._probe_codec(aac) == "aac"
    warm = time.perf_counter() - t0
    assert await stream._probe_codec(alac) == "alac"
    assert warm < cold and warm < 0.01


# ── F6: RFC 9110 ranges and validators ─────────────────────────────────────

@pytest.mark.parametrize("hdr,total,want", [
    (None, 100, None), ("", 100, None), ("items=0-5", 100, None), ("bytes=abc", 100, None),
    ("bytes=", 100, None), ("bytes=-", 100, None), ("bytes=5-3", 100, None),
    ("bytes=0-0", 100, (0, 1)), ("bytes=0-1", 100, (0, 2)), ("bytes=10-", 100, (10, 100)),
    ("bytes=99-99", 100, (99, 100)), ("bytes=90-500", 100, (90, 100)),
    ("bytes=-10", 100, (90, 100)), ("bytes=-500", 100, (0, 100)), ("BYTES = 3-4", 100, (3, 5)),
    ("bytes=100-", 100, "unsatisfiable"), ("bytes=150-160", 100, "unsatisfiable"),
    ("bytes=-0", 100, "unsatisfiable"), ("bytes=0-", 0, "unsatisfiable"),
    ("bytes=-5", 0, "unsatisfiable"), ("bytes=0-1,4-5", 100, [(0, 2), (4, 6)]),
    ("bytes=0-1,5-3", 100, None), ("bytes=0-1,150-", 100, [(0, 2)]),
])
def test_byte_range_set(hdr, total, want):
    if isinstance(want, tuple):
        want = [want]
    assert stream._byte_range_set(hdr, total) == want


@pytest.mark.asyncio
async def test_range_responses_follow_rfc9110(tmp_path):
    f = tmp_path / "r.mp3"
    data = os.urandom(600_000)
    f.write_bytes(data)
    n = len(data)
    app, box = _range_app()
    box["p"] = f
    async with _client(app) as c:
        full = await c.get("/f")
        etag, lm = full.headers["etag"], full.headers["last-modified"]
        assert full.status_code == 200 and full.content == data

        async def get(h):
            return await c.get("/f", headers=h)
        cases = [
            ({"Range": "bytes=0-0"}, 206, data[:1], f"bytes 0-0/{n}"),
            ({"Range": "bytes=-500"}, 206, data[-500:], f"bytes {n - 500}-{n - 1}/{n}"),
            ({"Range": f"bytes={n - 1}-"}, 206, data[-1:], f"bytes {n - 1}-{n - 1}/{n}"),
            ({"Range": "bytes=1000-"}, 206, data[1000:], f"bytes 1000-{n - 1}/{n}"),
            ({"Range": "bytes=10-299999"}, 206, data[10:300000], f"bytes 10-299999/{n}"),
            ({"Range": f"bytes={n}-"}, 416, b"", f"bytes */{n}"),
            ({"Range": f"bytes={n + 5}-{n + 9}"}, 416, b"", f"bytes */{n}"),
            ({"Range": "bytes=-0"}, 416, b"", f"bytes */{n}"),
            ({"Range": "bytes=9-3"}, 200, data, None),
            ({"Range": "bytes=junk"}, 200, data, None),
            ({"Range": "items=0-3"}, 200, data, None),
            ({"Range": "bytes=10-19", "If-Range": etag}, 206, data[10:20], f"bytes 10-19/{n}"),
            ({"Range": "bytes=10-19", "If-Range": lm}, 206, data[10:20], f"bytes 10-19/{n}"),
            ({"Range": "bytes=10-19", "If-Range": '"other"'}, 200, data, None),
            ({"Range": "bytes=10-19", "If-Range": "W/" + etag}, 200, data, None),
            ({"Range": "bytes=10-19",
              "If-Range": "Mon, 01 Jan 2001 00:00:00 GMT"}, 200, data, None),
        ]
        for h, status, body, cr in cases:
            r = await get(h)
            assert r.status_code == status, (h, r.status_code)
            if status != 416:
                assert r.content == body, h
            assert r.headers.get("content-range") == cr, h
            # every answer carries the same validators as the 200
            assert r.headers.get("etag") == etag and r.headers.get("last-modified") == lm, h
            assert r.headers.get("accept-ranges") == "bytes"
        multi = await get({"Range": "bytes=0-1,4-5"})
        assert multi.status_code == 206
        assert multi.headers["content-type"].startswith("multipart/byteranges")


@pytest.mark.asyncio
async def test_empty_file_ranges(tmp_path):
    f = tmp_path / "z.mp3"
    f.write_bytes(b"")
    app, box = _range_app()
    box["p"] = f
    async with _client(app) as c:
        r = await c.get("/f", headers={"Range": "bytes=0-"})
        assert r.status_code == 416 and r.headers["content-range"] == "bytes */0"
        r = await c.get("/f")
        assert r.status_code == 200 and r.content == b""


class _Up:
    """A catalogue archive's answer to one ranged GET (``_WebDAVStream``-like)."""

    def __init__(self, data: bytes, offset: int, length, headers: dict):
        end = len(data) if length is None else min(len(data), offset + length)
        self._buf = data[offset:end]
        self.size = len(data)
        self._resp = types.SimpleNamespace(headers=headers)
        self.closed = False

    def read(self, n):
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_catalogue_passthrough_passes_validators_and_honours_if_range(monkeypatch):
    from starlette.requests import Request
    data = os.urandom(300_000)
    opened: list = []
    hdrs = {"etag": '"v1"', "last-modified": "Tue, 01 Sep 2026 10:00:00 GMT"}

    class Src:
        def open_stream(self, path, *, offset=0, lane="stream", length=None):
            up = _Up(data, offset, length, hdrs)
            opened.append((offset, length, up))
            return up

        def stat(self, path, *, lane="scan", strict=False):
            return types.SimpleNamespace(size=len(data))
    track = types.SimpleNamespace(id="cp1", path="https://example.invalid/pub:/a/b.mp3",
                                  mtime=0, duration=0)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {"cp1"})

    async def run(h):
        req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                       "headers": [(k.lower().encode(), v.encode()) for k, v in h.items()]})
        resp = await stream._catalogue_passthrough(req, track, Src(), "/a/b.mp3", "audio/mpeg")
        return resp, b"".join([c async for c in resp.body_iterator])
    resp, body = await run({"Range": "bytes=100-199"})
    assert resp.status_code == 206 and body == data[100:200]
    assert resp.headers["etag"] == '"v1"' and resp.headers["last-modified"] == hdrs["last-modified"]
    resp, body = await run({"Range": "bytes=100-199", "If-Range": '"v1"'})
    assert resp.status_code == 206 and body == data[100:200]
    opened.clear()
    resp, body = await run({"Range": "bytes=100-199", "If-Range": '"v0"'})   # stale
    assert resp.status_code == 200 and body == data
    assert [o[:2] for o in opened] == [(100, 100), (0, None)] and opened[0][2].closed
    resp, body = await run({"Range": "bytes=-50"})
    assert resp.status_code == 206 and body == data[-50:]


# ── F4: render slots of renders nobody wants ───────────────────────────────

_RATE = 44100 * 4


def _noise(seconds: float) -> bytes:
    return os.urandom(int(seconds * _RATE) // 4 * 4)


def _live_render_fn(key: str, total_s: float, wall_s: float, events: dict):
    """A stand-in live renderer: ``total_s`` of audible PCM over ``wall_s``
    seconds, under a render slot — the shape of ``_render_pipe_live``."""
    async def render():
        lw = stream._LiveWav(key, expected_seconds=total_s, exact=True)
        events.setdefault("paths", {})[key] = lw.path
        try:
            async with stream._render_sem:
                events.setdefault("slot", []).append(key)
                steps = 40
                for _ in range(steps):
                    lw.feed(_noise(total_s / steps))
                    await asyncio.sleep(wall_s / steps)
            return lw.finish()
        except asyncio.CancelledError:
            events.setdefault("stopped", []).append(key)
            raise
        finally:
            lw.close()
    return render


@pytest.fixture()
def slots(monkeypatch, tmp_path):
    """Two render slots; a short grace after a listener leaves (the default
    ``_ABANDON_GRACE_S`` is covered by its own tests)."""
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    sem = stream._RenderSlots(2)
    monkeypatch.setattr(stream, "_render_sem", sem)
    monkeypatch.setattr(stream, "_ABANDON_GRACE_S", 0.3)
    yield sem


async def _play(tid: str, fn, *, read: int = 64 * 1024):
    """A web play of a live render: the response once audible, its first
    ``read`` bytes, and the open body iterator (still a reader)."""
    key = conversion_cache._cache_key(tid, "uade", 0)
    t0 = time.perf_counter()
    resp = await stream._serve_live(_req(), track_id=tid, format_type="uade", subsong=0,
                                    key=key, render_fn=fn, web_session=True, background=None,
                                    xf=None, rendered="test")
    it = resp.body_iterator
    got = 0
    first = None
    async for c in it:
        if first is None:
            first = time.perf_counter() - t0
        got += len(c)
        if got >= read:
            break
    return key, it, first, got


async def _until(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_two_skips_do_not_hold_the_next_play(slots):
    ev: dict = {}
    ids = [f"f4-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    keys = [conversion_cache._cache_key(i, "uade", 0) for i in ids]
    fns = [_live_render_fn(k, 8.0, 6.0, ev) for k in keys]
    ka, ita, _, _ = await _play(ids[0], fns[0])
    await ita.aclose()                                  # skipped
    kb, itb, _, _ = await _play(ids[1], fns[1])
    await itb.aclose()                                  # skipped
    assert slots.free() == 0
    await asyncio.sleep(0.35)                           # (both past the test's grace)
    kc, itc, first_c, _ = await _play(ids[2], fns[2])
    # before: C waited for A or B to finish their whole renders (~6 s here)
    assert first_c < 2.0, first_c
    assert ev["stopped"] == [kb]                         # the one started last
    assert not ev["paths"][kb].exists()                  # its temp file is gone
    assert await conversion_cache.get_cached(kb) is None  # and nothing was cached
    # A still finishes into the cache
    assert await _until(lambda: conversion_cache._meta.get(ka) is not None)
    await itc.aclose()


@pytest.mark.asyncio
async def test_a_render_someone_still_reads_is_never_stopped(slots):
    ev: dict = {}
    ids = [f"f4s-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    keys = [conversion_cache._cache_key(i, "uade", 0) for i in ids]
    fns = [_live_render_fn(k, 4.0, 3.0, ev) for k in keys]
    kx, itx, _, _ = await _play(ids[0], fns[0])
    # a second listener of the same render (another tab / a DLNA renderer)
    kx2, itx2, _, got2 = await _play(ids[0], fns[0], read=1024)
    await itx.aclose()                                   # one of the two leaves
    ky, ity, _, _ = await _play(ids[1], fns[1])
    await ity.aclose()                                   # abandoned
    kz, itz, first_z, _ = await _play(ids[2], fns[2])
    assert ev["stopped"] == [ky]                         # not X: it still has a reader
    rest = await _collect(itx2)                          # the remaining reader gets it all
    assert got2 + len(rest) == 44 + 4 * 44100 * 4
    assert await _until(lambda: conversion_cache._meta.get(kx) is not None)
    await itz.aclose()


@pytest.mark.asyncio
async def test_a_waiter_keeps_a_render_and_a_stopped_one_renders_again_whole(slots):
    ev: dict = {}
    ids = [f"f4w-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    keys = [conversion_cache._cache_key(i, "uade", 0) for i in ids]
    fns = [_live_render_fn(k, 3.0, 2.5, ev) for k in keys]
    # W: a blocking client waits for the whole render (Subsonic / DLNA)
    w = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id=ids[0], format_type="uade", subsong=0, render_fn=fns[0]))
    assert await _until(lambda: keys[0] in ev.get("slot", []))
    kv, itv, _, _ = await _play(ids[1], fns[1])
    await itv.aclose()                                   # V abandoned
    kz, itz, _, _ = await _play(ids[2], fns[2])
    assert ev["stopped"] == [kv]                         # W has a waiter: kept
    wpath, hit = await w
    assert not hit and wpath.stat().st_size == 44 + int(3.0 * 44100) * 4
    await itz.aclose()
    # V again: renders whole — a stopped render never poisons the cache
    fv2 = _live_render_fn(kv, 3.0, 0.5, ev)
    vpath, _ = await conversion_cache.get_or_render(
        track_id=ids[1], format_type="uade", subsong=0, render_fn=fv2)
    raw = vpath.read_bytes()
    assert len(raw) == 44 + int(3.0 * 44100) * 4
    assert int.from_bytes(raw[40:44], "little") == len(raw) - 44


@pytest.mark.asyncio
async def test_a_retired_prewarm_render_gives_its_slot_up_and_probes_never_take_one(slots):
    ev: dict = {}
    ids = [f"f4p-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    keys = [conversion_cache._cache_key(i, "uade", 0) for i in ids]
    alive = {"v": True}

    async def prewarm(i):
        tok = conversion_cache.render_interest.set(lambda: alive["v"])
        try:
            return await conversion_cache.get_or_render(
                track_id=ids[i], format_type="uade", subsong=0,
                render_fn=_live_render_fn(keys[i], 3.0, 3.0, ev))
        finally:
            conversion_cache.render_interest.reset(tok)
    pa = asyncio.ensure_future(prewarm(0))
    pb = asyncio.ensure_future(prewarm(1))
    assert await _until(lambda: len(ev.get("slot", [])) == 2)
    # a duration probe (PRIO_PROBE) never stops anything, wanted or not
    alive["v"] = False                                   # both prewarms retired

    async def probe_acquire():
        tok = stream._BG_PRIO.set(stream.PRIO_PROBE)
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.3)
            slots.release()
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            stream._BG_PRIO.reset(tok)
    assert await probe_acquire() is False and "stopped" not in ev
    # a play does
    await asyncio.wait_for(slots.acquire(), timeout=2)
    slots.release()
    assert len(ev["stopped"]) == 1
    for t in (pa, pb):
        with contextlib.suppress(BaseException):
            await t


def _slow_pump_source(monkeypatch, tmp_path) -> Path:
    """A 30 s AIFF and an ffmpeg stand-in that pumps PCM at ~2x real time
    (s24le stereo 44.1 kHz) until killed — a transcode that is still
    running when the test looks."""
    import subprocess
    from soniqboom.config import settings
    src = tmp_path / "src.aiff"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=30", str(src)], check=True)
    body = tmp_path / "fake_ffmpeg.py"
    body.write_text("import sys, time\n"
                    "out = sys.stdout.buffer\n"
                    "chunk = bytes(44100 * 6 // 10)\n"
                    "while True:\n"
                    "    out.write(chunk); out.flush(); time.sleep(0.05)\n")
    fake = tmp_path / "ffmpeg"
    fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}"\n')
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "ffmpeg_path", str(fake))
    return src


@pytest.mark.asyncio
@pytest.mark.skipif(not __import__("shutil").which("ffprobe")
                    or not __import__("shutil").which("ffmpeg"), reason="ffmpeg missing")
async def test_an_abandoned_transcode_pump_is_stopped_and_leaves_no_partial(monkeypatch, tmp_path,
                                                                            slots):
    src = _slow_pump_source(monkeypatch, tmp_path)
    tid = f"pump-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(src), duration=30.0)
    resp = await stream._serve_inflight_wav(_req(), track, src, tid, None, None, "pcm", None)
    inflight = stream._INFLIGHT_TRANSCODES[tid]
    partial = inflight["wav_path"]
    pump = inflight["pump_task"]
    it = resp.body_iterator
    got = 0
    async for c in it:
        got += len(c)
        if got > 256 * 1024:
            break
    await it.aclose()                                    # the listener left
    assert await _until(lambda: inflight["subscribers"] == 0, 3)
    # another play needs a slot: hold the other one, then ask
    await slots.acquire()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=5)
        slots.release()
    finally:
        slots.release()
    assert await _until(pump.done, 5) and pump.cancelled()
    assert not Path(partial).exists()
    key = stream._inflight_cache_key(tid, None)
    assert await conversion_cache.get_cached(key) is None
    assert await _until(lambda: tid not in stream._INFLIGHT_TRANSCODES, 3)


# ── F5: the prewarm prepares what the coming play reads ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("ext,codec,client,hints,expect", [
    (".m4a", "aac", {}, {}, "nothing"),
    (".m4a", "aac", {}, {"target_format": "mp3"}, ("transcode", "mp3", None, None)),
    (".m4a", "alac", {"user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 "
                                    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"},
     {}, "nothing"),
    (".m4a", "alac", {"user-agent": "Mozilla/5.0 Chrome/129.0 Safari/537.36"}, {},
     ("inflight", None)),
    (".m4a", "alac", {}, {"target_format": "mp3", "max_bitrate_kbps": 192},
     ("transcode", "mp3", None, 192)),
    (".aiff", None, {}, {"target_sample_rate": 48000}, ("inflight", 48000)),
    (".dsf", None, {}, {}, ("inflight", 96000)),
])
async def test_prewarm_prepares_exactly_what_the_play_reads(monkeypatch, tmp_path, ext, codec,
                                                            client, hints, expect):
    f = tmp_path / ("x" + ext)
    f.write_bytes(b"\0" * 64)
    tid = f"pw5-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(f), duration=60.0, bitrate=900)

    async def get_track(t):
        return track
    monkeypatch.setattr(stream, "get_track", get_track)

    async def probe(p):
        return codec
    monkeypatch.setattr(stream, "_probe_codec", probe)
    did: list = []

    async def fake_inflight(*, track_id, src_path, track, target_rate, target_channels_hint):
        did.append(("inflight", target_rate))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return {"pump_task": fut}
    monkeypatch.setattr(stream, "_get_or_start_inflight_wav", fake_inflight)

    async def fake_gor(**kw):
        did.append(("transcode", kw["codec"], kw["target_rate"], kw["bitrate"]))
        return Path("/x"), False
    monkeypatch.setattr(conversion_cache, "get_or_render", fake_gor)
    serve = stream._prewarm_serve(_req(client), **hints)
    await stream._prewarm_conversion(tid, f, ext, serve)
    assert did == ([] if expect == "nothing" else [expect])
    # …and it is what the play itself decides (the same ``_serve_plan``)
    plan = await stream._serve_plan(_req(client), track, f, ext,
                                    target_format=hints.get("target_format"),
                                    max_bitrate_kbps=hints.get("max_bitrate_kbps", 0),
                                    target_sample_rate=hints.get("target_sample_rate", 0),
                                    force_transcode=False)
    assert plan.kind == ("direct" if expect == "nothing" else expect[0])


@pytest.mark.asyncio
async def test_a_stopped_render_is_not_restarted_by_bookkeeping_but_is_by_a_play(slots):
    ev: dict = {}
    tid = f"f4r-{uuid.uuid4().hex[:8]}"
    key = conversion_cache._cache_key(tid, "uade", 0)
    starts: list = []

    def fn():
        starts.append(1)
        return _live_render_fn(key, 3.0, 3.0, ev)()
    first = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id=tid, format_type="uade", subsong=0, render_fn=fn,
        interest=conversion_cache.NO_INTEREST))
    assert await _until(lambda: key in ev.get("slot", []))
    # bookkeeping attached to it (``_live_finish_later``): doesn't want it either
    weak = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id=tid, format_type="uade", subsong=0, render_fn=fn,
        interest=conversion_cache.NO_INTEREST))
    await asyncio.sleep(0.1)
    await slots.acquire()                                # the other slot
    await asyncio.wait_for(slots.acquire(), timeout=3)   # a play: stops it
    slots.release()
    slots.release()
    with pytest.raises(BaseException):
        await first
    with pytest.raises(conversion_cache.RenderStopped):
        await weak                                       # …and never starts it again
    assert starts == [1] and await conversion_cache.get_cached(key) is None
    # a caller who wants it renders it (whole)
    path, hit = await conversion_cache.get_or_render(
        track_id=tid, format_type="uade", subsong=0,
        render_fn=_live_render_fn(key, 3.0, 0.3, ev))
    assert not hit and path.stat().st_size == 44 + int(3.0 * 44100) * 4


@pytest.mark.asyncio
async def test_a_whole_file_answer_states_the_current_length(tmp_path):
    f = tmp_path / "grow.mp3"
    f.write_bytes(b"a" * 1000)
    app, box = _range_app()
    box["p"] = f
    async with _client(app) as c:
        r = await c.get("/f", headers={"Range": "bytes=0-9"})      # caches the stat
        assert r.status_code == 206
        f.write_bytes(b"b" * 3000)                                   # replaced within the TTL
        r = await c.get("/f")
        assert r.status_code == 200 and r.content == b"b" * 3000
        assert r.headers["content-length"] == "3000"


@pytest.mark.asyncio
@pytest.mark.skipif(not __import__("shutil").which("ffprobe")
                    or not __import__("shutil").which("ffmpeg"), reason="ffmpeg missing")
async def test_an_aborted_waveform_request_never_cancels_the_listeners_pump(monkeypatch, tmp_path,
                                                                           slots):
    from starlette.responses import Response
    from soniqboom.api import tracks as tracks_api
    from soniqboom.core import data as data_mod
    src = _slow_pump_source(monkeypatch, tmp_path)
    tid = f"wf-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(src), duration=30.0, format="AIFF")

    async def get_track(t):
        return track

    async def no_waveform(t):
        return None
    monkeypatch.setattr(data_mod, "get_track", get_track)
    monkeypatch.setattr(data_mod, "get_waveform", no_waveform)
    resp = await stream._serve_inflight_wav(_req(), track, src, tid, None, None, "pcm", None)
    pump = stream._INFLIGHT_TRANSCODES[tid]["pump_task"]
    it = resp.body_iterator
    await it.__anext__()                                 # a listener is streaming it
    wf = asyncio.ensure_future(tracks_api.get_track_waveform(tid, Response(), subsong=None,
                                                             request=None))
    await asyncio.sleep(0.6)                             # waiting on the pump now
    wf.cancel()                                          # the player moved on
    with contextlib.suppress(BaseException):
        await wf
    await asyncio.sleep(0.3)
    assert not pump.done()                               # before: cancelled with the request
    await it.aclose()
    pump.cancel()
    with contextlib.suppress(BaseException):
        await pump


@pytest.mark.asyncio
@pytest.mark.parametrize("path,hints,want_serve", [
    ("/m/next.m4a", {"target_format": "mp3", "max_bitrate_kbps": 192, "target_sample_rate": 0},
     {"target_format": "mp3", "max_bitrate_kbps": 192, "target_sample_rate": 0}),
    ("/m/next.dsf", {"target_format": None, "max_bitrate_kbps": 0, "target_sample_rate": 48000},
     {"target_format": None, "max_bitrate_kbps": 0, "target_sample_rate": 48000}),
    ("/m/next.mod", {"target_format": "mp3", "max_bitrate_kbps": 0, "target_sample_rate": 0},
     {"target_format": "mp3"}),                     # a rendered format: its mp3 too
    ("/m/next.mod", {"target_format": None, "max_bitrate_kbps": 0, "target_sample_rate": 0},
     None),                                         # …else the same for everyone
    ("/m/next.flac", {"target_format": "mp3", "max_bitrate_kbps": 0, "target_sample_rate": 0},
     {"target_format": "mp3"}),                     # native, transcoded for this client
])
async def test_subsonic_next_track_prewarm_carries_the_clients_hints(monkeypatch, path, hints,
                                                                     want_serve):
    from soniqboom.api import subsonic
    got: list = []

    async def fake_start(ntid, nsub, ext, who, serve=None):
        got.append((ntid, ext, serve))
    monkeypatch.setattr(subsonic, "_start_prewarm", fake_start)
    monkeypatch.setattr(subsonic, "_next_tune", lambda *a: ("nx", 0))
    store = types.SimpleNamespace(get_track=lambda tid: {"id": tid, "path": path})
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(stream, "_remote_bytes_local", lambda p: True)
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: True)
    ua = "AppleCoreMedia/1.0.0 (iPhone; U; CPU OS 18_1)"
    req = types.SimpleNamespace(method="GET", headers={"user-agent": ua}, cookies={},
                                client=None)
    subsonic._prewarm_next_for_subsonic(req, types.SimpleNamespace(username="u", id="u"),
                                        "cur", "cur", 0, 0.0, **hints)
    await asyncio.sleep(0)
    assert len(got) == 1 and got[0][0] == "nx"
    serve = got[0][2]
    if want_serve is None:
        assert serve is None
    else:
        assert {k: serve[k] for k in want_serve} == want_serve
        assert serve["client"].headers["user-agent"] == ua


# ── QA follow-ups: render slots ────────────────────────────────────────────

_SKIP_NO_FFMPEG = pytest.mark.skipif(
    not __import__("shutil").which("ffprobe") or not __import__("shutil").which("ffmpeg"),
    reason="ffmpeg missing")


@pytest.mark.asyncio
async def test_a_render_abandoned_while_a_play_waits_still_frees_its_slot(slots):
    """M1: the play is already queued (both renders listened to) when one
    listener leaves — the waiting play looks again and gets that slot."""
    ev: dict = {}
    ids = [f"m1-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    keys = [conversion_cache._cache_key(i, "uade", 0) for i in ids]
    fns = [_live_render_fn(k, 8.0, 6.0, ev) for k in keys]
    ka, ita, _, _ = await _play(ids[0], fns[0])
    kb, itb, _, _ = await _play(ids[1], fns[1])
    t0 = time.perf_counter()
    c = asyncio.ensure_future(_play(ids[2], fns[2]))
    await asyncio.sleep(0.3)
    assert not c.done()                                  # queued: nothing to stop yet
    await itb.aclose()                                   # B's listener leaves now
    kc, itc, _, _ = await c
    waited = time.perf_counter() - t0
    assert waited < 2.0, waited                          # before: B's whole render (~6 s)
    assert ev["stopped"] == [kb]
    await ita.aclose()
    await itc.aclose()


@pytest.mark.asyncio
async def test_a_player_between_two_range_requests_keeps_its_render(slots, monkeypatch):
    """M2: within the grace after a body ends the render stays — unless the
    same listener has since started another track (a skip)."""
    monkeypatch.setattr(stream, "_ABANDON_GRACE_S", 12.0)
    ev: dict = {}
    tid = f"m2-{uuid.uuid4().hex[:8]}"
    key = conversion_cache._cache_key(tid, "uade", 0)
    tok = stream._CLIENT_TAG.set("listener-1")
    try:
        resp = await stream._serve_live(_req({"Range": "bytes=0-65535"}), track_id=tid,
                                        format_type="uade", subsong=0, key=key,
                                        render_fn=_live_render_fn(key, 8.0, 6.0, ev),
                                        web_session=True, background=None, xf=None,
                                        rendered="t")
        body = await _collect(resp.body_iterator)
        assert len(body) == 65536 and resp.status_code == 206
        await slots.acquire()                            # the other slot is busy
        with pytest.raises(asyncio.TimeoutError):        # another play / prewarm waits…
            await slots.acquire(timeout=1.0)
        assert "stopped" not in ev                       # …the playing render is kept
        stream._note_client_play("listener-1", "some-other-track")   # the listener skipped
        await slots.acquire(timeout=2.0)                 # now it goes at once
        assert ev["stopped"] == [key]
        slots.release()
        slots.release()
    finally:
        stream._CLIENT_TAG.reset(tok)


@pytest.mark.asyncio
async def test_a_play_arriving_while_its_render_is_stopped_gets_a_fresh_whole_one(slots):
    """M3 (live): a replay in the stop window never attaches to the dying
    render — it gets the whole track from a new render."""
    ev: dict = {}
    tid = f"m3-{uuid.uuid4().hex[:8]}"
    key = conversion_cache._cache_key(tid, "uade", 0)

    def fn():
        async def render():
            lw = stream._LiveWav(key, expected_seconds=3.0, exact=True)
            try:
                async with stream._render_sem:
                    ev.setdefault("slot", []).append(key)
                    for _ in range(40):
                        lw.feed(_noise(3.0 / 40))
                        await asyncio.sleep(2.0 / 40)
                return lw.finish()
            except asyncio.CancelledError:
                ev.setdefault("stopped", []).append(key)
                await asyncio.sleep(0.05)               # a renderer's kill + wait
                raise
            finally:
                lw.close()
        return render()
    k, it, _, _ = await _play(tid, fn)
    await it.aclose()                                    # abandoned
    await asyncio.sleep(0.35)                            # past the test's grace
    await slots.acquire()
    acq = asyncio.ensure_future(slots.acquire())         # a play: stops it
    await asyncio.sleep(0)
    assert stream._UADE_LIVE[key]["stopped"]           # marked at once, still cleaning up
    replay = asyncio.ensure_future(_play(tid, fn, read=10 ** 9))   # the listener is back
    await asyncio.sleep(0)
    await acq                                            # (the slots go back:
    slots.release()                                      #  the new render needs one)
    slots.release()
    k2, it2, _, got = await asyncio.wait_for(replay, 20)
    assert ev["stopped"] == [key]
    assert got == 44 + int(3.0 * 44100) * 4              # before: ended at ~100 KB


@pytest.mark.asyncio
@_SKIP_NO_FFMPEG
async def test_a_play_arriving_while_its_pump_is_stopped_gets_real_audio(monkeypatch, tmp_path,
                                                                        slots):
    """M3 (pump): before, the replay got 200 + real audio + megabytes of zeros."""
    src = _slow_pump_source(monkeypatch, tmp_path)
    (tmp_path / "fake_ffmpeg.py").write_text(
        "import sys, time\nout = sys.stdout.buffer\nchunk = b'\\x01' * (44100 * 6 // 10)\n"
        "while True:\n    out.write(chunk); out.flush(); time.sleep(0.004)\n")
    tid = f"m3p-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(src), duration=30.0)
    resp = await stream._serve_inflight_wav(_req(), track, src, tid, None, None, "pcm", None)
    first = stream._INFLIGHT_TRANSCODES[tid]
    it = resp.body_iterator
    async for _c in it:
        break
    await it.aclose()                                    # the listener left
    await asyncio.sleep(0.35)
    await slots.acquire()
    acq = asyncio.ensure_future(slots.acquire())         # stops the pump
    await asyncio.sleep(0)
    assert first["stopped"]
    resp2 = await stream._serve_inflight_wav(_req(), track, src, tid, None, None, "pcm", None)
    assert stream._INFLIGHT_TRANSCODES.get(tid) is not first
    await acq                                            # (the slots go back:
    slots.release()                                      #  the new pump needs one)
    slots.release()
    body = await asyncio.wait_for(_collect(resp2.body_iterator), timeout=30)
    assert len(body) == int(resp2.headers["content-length"])
    assert body[44:].count(0) == 0                       # every byte the new pump's
    stream._INFLIGHT_TRANSCODES[tid]["pump_task"].cancel()


@pytest.mark.asyncio
async def test_the_freed_slot_goes_to_the_play_that_stopped_the_render(slots):
    """Minor 1: a probe queued earlier doesn't take the slot a play freed."""
    ev: dict = {}
    ids = [f"n1-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    keys = [conversion_cache._cache_key(i, "uade", 0) for i in ids]
    fns = [_live_render_fn(k, 8.0, 6.0, ev) for k in keys]
    ka, ita, _, _ = await _play(ids[0], fns[0])
    kb, itb, _, _ = await _play(ids[1], fns[1])
    await itb.aclose()
    await asyncio.sleep(0.35)
    order = []

    async def probe():
        tok = stream._BG_PRIO.set(stream.PRIO_PROBE)
        try:
            async with slots:
                order.append("probe")
                await asyncio.sleep(1.0)
        finally:
            stream._BG_PRIO.reset(tok)
    p = asyncio.ensure_future(probe())
    await asyncio.sleep(0.1)                             # the probe queues first
    t0 = time.perf_counter()
    async with slots:
        order.append("play")
        waited = time.perf_counter() - t0
    assert order[0] == "play" and waited < 1.0, (order, waited)
    await p
    await ita.aclose()


def test_slot_holders_are_the_acquiring_task_on_this_python():
    """M5: ``acquire(timeout=)`` runs in the caller's task (``wait_for`` gave
    the acquire a task of its own on 3.11 and leaked a holder per call)."""
    async def main():
        sem = stream._RenderSlots(2)

        async def pump():
            await stream._acquire_slot(sem, 30)
            held = sem.held_by(asyncio.current_task())
            sem.release()
            return held
        helds = [await asyncio.ensure_future(pump()) for _ in range(5)]
        return helds, len(sem._holders), sem.free()
    assert asyncio.run(main()) == ([True] * 5, 0, 2)


# ── QA follow-ups: descriptors and threads ─────────────────────────────────

@pytest.mark.asyncio
async def test_range_bodies_close_their_descriptor_off_the_loop(tmp_path, monkeypatch):
    """M4: ``os.close`` of a body's descriptor runs on a playback thread."""
    from soniqboom.core import playback_io
    f = tmp_path / "a.mp3"
    f.write_bytes(os.urandom(2 * 1024 * 1024))
    seen = []
    real = playback_io.close_quiet

    def spy(fd):
        seen.append(threading.current_thread().name)
        real(fd)
    monkeypatch.setattr(playback_io, "close_quiet", spy)
    app, box = _range_app()
    box["p"] = f
    async with _client(app) as c:
        r = await c.get("/f", headers={"Range": "bytes=0-"})
        assert r.status_code == 206
    await asyncio.sleep(0.2)
    assert seen and all(n.startswith("sb-playback-io") for n in seen), seen


@pytest.mark.asyncio
async def test_a_handle_closed_from_a_worker_thread_tells_the_loop(tmp_path):
    """Minor 4: Starlette runs ``BackgroundTask(fd.close)`` in a worker;
    the listener-gone note still runs on the loop, once."""
    f = tmp_path / "x"
    f.write_bytes(b"x")
    loop_thread = threading.current_thread()
    calls = []
    h = stream._FdHandle(os.open(str(f), os.O_RDONLY),
                         on_close=lambda: calls.append(threading.current_thread() is loop_thread))
    await asyncio.gather(asyncio.to_thread(h.close), asyncio.to_thread(h.close))
    h.close()
    await asyncio.sleep(0.05)
    assert calls == [True]


def test_a_pump_body_that_never_started_is_no_listener():
    """Minor 5: a body dropped unsent (never iterated: its finally never
    runs) stops counting as a listener."""
    import gc
    inflight = {"bodies": stream._weakref.WeakSet(), "listen": stream._listen_state()}

    async def body():
        yield b"x"
    resp = types.SimpleNamespace(body_iterator=body())
    stream._track_pump_body(inflight, resp)
    alive = lambda: any(getattr(g, "ag_frame", None) is not None for g in inflight["bodies"])
    assert alive()
    del resp
    gc.collect()
    assert not alive()


@pytest.mark.asyncio
async def test_one_hung_mount_leaves_the_others_their_reads(monkeypatch):
    """Minor 9: jobs on a mount that stopped answering hold at most its
    share of the threads; another mount's reads go on."""
    from soniqboom.core import playback_io
    monkeypatch.setattr(playback_io, "per_mount_limit", lambda: 2)
    hang = threading.Event()
    running = []

    def stuck():
        running.append(1)
        hang.wait(10)
    try:
        hung = [asyncio.ensure_future(playback_io.run("/Volumes/Dead", stuck)) for _ in range(6)]
        await asyncio.sleep(0.2)
        assert len(running) == 2                          # the rest wait on the loop
        t0 = time.perf_counter()
        assert await asyncio.wait_for(playback_io.run("/Volumes/Music", lambda: 7), 2) == 7
        assert time.perf_counter() - t0 < 0.5
        for t in hung:
            t.cancel()
        await asyncio.sleep(0.1)
        assert len(running) == 2                          # cancelled waiters never start
    finally:
        hang.set()
    assert playback_io.mount_key("/Volumes/Music/a/b.mp3") == "/Volumes/Music"
    assert playback_io.mount_key("/mnt/nas/x.flac") == "/mnt/nas"


@pytest.mark.asyncio
async def test_cache_lookups_and_growing_reads_are_off_the_loop(tmp_path, monkeypatch):
    """Minor 7: ``get_cached``'s file check and a growing file's reads /
    fstat run on the playback threads."""
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    loop_thread = threading.current_thread()
    on_loop = []
    real_pread, real_fstat = os.pread, os.fstat

    def pr(fd, n, pos):
        on_loop.append(threading.current_thread() is loop_thread)
        return real_pread(fd, n, pos)

    def fs(fd):
        on_loop.append(threading.current_thread() is loop_thread)
        return real_fstat(fd)
    f = tmp_path / "g.wav"
    payload = os.urandom(300_000)
    f.write_bytes(payload)
    done = asyncio.Event()
    done.set()
    monkeypatch.setattr(os, "pread", pr)
    monkeypatch.setattr(os, "fstat", fs)
    r = await stream._growing_file_range_response(_req({"range": "bytes=10-"}), f, len(payload),
                                                   done, media_type="audio/wav")
    assert await _collect(r.body_iterator) == payload[10:]
    assert on_loop and not any(on_loop)
    monkeypatch.setattr(os, "pread", real_pread)
    monkeypatch.setattr(os, "fstat", real_fstat)
    tid = f"gc-{uuid.uuid4().hex[:6]}"
    key = conversion_cache._cache_key(tid, "uade", 0)
    wav = tmp_path / "r.wav"
    wav.write_bytes(stream._build_wav_header(44100, 2, 44100, 16) + _noise(1.0))
    await conversion_cache.store_cached(key, "uade", wav)
    seen = []
    real_exists = Path.exists

    def ex(self, *a, **k):
        seen.append(threading.current_thread() is loop_thread)
        return real_exists(self, *a, **k)
    monkeypatch.setattr(Path, "exists", ex)
    assert await conversion_cache.get_cached(key) is not None
    assert seen and not any(seen)


@pytest.mark.asyncio
async def test_a_cancelled_non_live_libgme_render_keeps_its_slot_until_it_ends(monkeypatch,
                                                                               tmp_path, slots):
    """Minor 8: the slot is released when the render's thread is done, not
    when its caller is cancelled."""
    from soniqboom.core import gme_render
    gate = threading.Event()
    monkeypatch.setattr(gme_render, "is_available", lambda: True)
    monkeypatch.setattr(gme_render, "render_plan", lambda d, s, dur: (5.0, None))

    def slow_wav(data, subsong, duration, fade_at_s=None):
        gate.wait(5)
        return b""
    monkeypatch.setattr(gme_render, "render_wav", slow_wav)
    f = tmp_path / "t.nsf"
    f.write_bytes(b"NESM\x1a" + bytes(200))
    t = asyncio.ensure_future(stream._render_gme(f))
    await asyncio.sleep(0.2)
    assert slots.free() == 1
    t.cancel()
    await asyncio.sleep(0.2)
    assert slots.free() == 1                             # still rendering: still held
    gate.set()
    with contextlib.suppress(BaseException):
        await t
    assert slots.free() == 2


# ── QA follow-ups: ranges and validators ───────────────────────────────────

@pytest.mark.asyncio
async def test_if_range_is_checked_against_the_file_as_it_is_now(tmp_path):
    """Minor 2: an If-Range validator from before a replacement no longer
    matches, even inside the stat cache's TTL."""
    f = tmp_path / "b.mp3"
    f.write_bytes(b"a" * 1000)
    app, box = _range_app()
    box["p"] = f
    async with _client(app) as c:
        r1 = await c.get("/f", headers={"Range": "bytes=0-9"})
        etag1 = r1.headers["etag"]
        f.write_bytes(b"b" * 900)
        os.utime(f, (time.time() + 10, time.time() + 10))
        r2 = await c.get("/f", headers={"Range": "bytes=100-199", "If-Range": etag1})
    assert r2.status_code == 200 and r2.content == b"b" * 900
    assert r2.headers["etag"] != etag1


@pytest.mark.asyncio
@pytest.mark.parametrize("hdr,status,kind", [
    ("bytes=0-1,999999-", 206, "single"),               # one satisfiable range
    ("bytes=0-1,-2", 206, "multipart"),
    ("bytes=0-1,5-3", 200, "whole"),                    # an invalid spec: ignored
    ("bytes=999990-,999999-", 416, None),
    ("bytes=0-1,0-1", 206, "single"),                   # coalesced
    ("bytes=0-1,2-3", 206, "single"),                   # adjacent: coalesced
])
async def test_several_ranges_follow_rfc9110(tmp_path, hdr, status, kind):
    """Minor 3: before, 416 for a set with a satisfiable range and 400 for
    one with an invalid spec."""
    f = tmp_path / "m.mp3"
    data = bytes(range(256)) * 4
    f.write_bytes(data)
    app, box = _range_app()
    box["p"] = f
    async with _client(app) as c:
        r = await c.get("/f", headers={"Range": hdr})
    assert r.status_code == status
    if kind == "single":
        rs = stream._byte_range_set(hdr, len(data))
        assert len(rs) == 1 and r.content == data[rs[0][0]:rs[0][1]]
    elif kind == "multipart":
        assert r.headers["content-type"].startswith("multipart/byteranges")
        assert data[:2] in r.content and data[-2:] in r.content
    elif kind == "whole":
        assert r.content == data
    else:
        assert r.headers["content-range"] == f"bytes */{len(data)}"
        assert "content-type" not in r.headers and r.headers.get("etag")


def test_if_range_dates_compare_as_dates():
    lm = "Tue, 01 Sep 2026 10:00:00 GMT"
    req = _req({"if-range": "Tuesday, 01-Sep-26 10:00:00 GMT"})   # RFC 850 form
    assert stream._if_range_holds(req, '"e"', lm)
    assert not stream._if_range_holds(_req({"if-range": "Tue, 01 Sep 2026 10:00:01 GMT"}),
                                      '"e"', lm)


@pytest.mark.parametrize("hdr,want", [
    (None, (0, 100, False)), ("bytes=5-3", (0, 100, False)), ("bytes=0-1,4-5", (0, 100, False)),
    ("bytes=0-1,2-3", (0, 4, True)), ("bytes=-10", (90, 100, True)), ("bytes=100-", None),
    ("items=0-1", (0, 100, False)),
])
def test_single_range_responders_read_ranges_the_same_way(hdr, want):
    """Nit: ``_parse_audio_range`` (growing files, SID, offsets, far seeks)
    reads a header as ``_range_file_response`` does — an invalid spec or a
    set that doesn't coalesce is ignored, never answered with its first part."""
    assert stream._parse_audio_range(hdr, 100) == want


@pytest.mark.asyncio
async def test_catalogue_if_range_reopen_closes_the_first_stream_even_when_cancelled(monkeypatch):
    """Minor 10: the close before the reopen survives a cancel; a 416 carries
    the archive's validators."""
    from starlette.requests import Request
    data = os.urandom(5000)
    hdrs = {"etag": '"v2"', "last-modified": "Tue, 01 Sep 2026 10:00:00 GMT"}
    ups: list = []
    close_started = threading.Event()

    class SlowUp(_Up):
        def close(self):
            close_started.set()
            time.sleep(0.3)
            self.closed = True

    class Src:
        def open_stream(self, path, *, offset=0, lane="stream", length=None):
            up = SlowUp(data, offset, length, hdrs)
            ups.append(up)
            return up

        def stat(self, path, *, lane="scan", strict=False):
            return types.SimpleNamespace(size=len(data))
    track = types.SimpleNamespace(id="cq", path="https://example.invalid/pub:/a/b.mp3",
                                  mtime=0, duration=0)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {"cq"})

    def req(h):
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                        "headers": [(k.lower().encode(), v.encode()) for k, v in h.items()]})
    t = asyncio.ensure_future(stream._catalogue_passthrough(
        req({"Range": "bytes=10-19", "If-Range": '"stale"'}), track, Src(), "/a/b.mp3",
        "audio/mpeg"))
    while not close_started.is_set():
        await asyncio.sleep(0.01)
    t.cancel()
    with contextlib.suppress(BaseException):
        await t
    await asyncio.sleep(0.5)
    assert ups[0].closed                                 # closed although the request went
    r = await stream._catalogue_passthrough(req({"Range": "bytes=9000-"}), track, Src(),
                                            "/a/b.mp3", "audio/mpeg")
    assert r.status_code == 416 and r.headers["etag"] == '"v2"'
    assert r.headers["content-range"] == f"bytes */{len(data)}"


@pytest.mark.asyncio
async def test_the_playback_pool_goes_at_stop_even_when_a_writer_is_late(monkeypatch):
    """Minor 11: released also when ``_release_thread_pool`` returns early."""
    from soniqboom import main
    from soniqboom.core import playback_io
    playback_io.pool()
    assert playback_io._POOL is not None
    released = await main._release_thread_pool({"snapshot": "TIMEOUT"})
    assert released is False and playback_io._POOL is None


# ── QA follow-ups: what the next-track prewarm prepares ────────────────────

def test_prewarm_registry_key_tells_clients_apart():
    """Minor 6: a Safari prewarm (ALAC: nothing to do) never stands in for a
    Chrome one (ALAC: the WAV)."""
    safari = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    chrome = "Mozilla/5.0 Chrome/129.0 Safari/537.36"
    a = stream._prewarm_serve(_req({"user-agent": safari}))
    b = stream._prewarm_serve(_req({"user-agent": chrome}))
    c = stream._prewarm_serve(_req({"user-agent": chrome}, cookies={"sb_caps": "alac.aac"}))
    tags = {stream._prewarm_client_tag(x) for x in (a, b, c)}
    assert len(tags) == 3


@pytest.mark.asyncio
async def test_a_rendered_next_track_asked_as_mp3_is_prepared_as_that_mp3(monkeypatch, tmp_path):
    """Minor 6: the WAV render, then the mp3 of it the play will read."""
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    tid = f"pr-{uuid.uuid4().hex[:6]}"
    rkey = conversion_cache._cache_key(tid, "tracker", 0)
    calls = []

    async def fake_prewarm_render(track_id, file_path, ext, subsong, *, uade_named=False,
                                  track=None, serve=None):
        async def render():
            p = tmp_path / "w.wav"
            p.write_bytes(stream._build_wav_header(44100, 2, 44100, 16) + _noise(1.0))
            return p
        await conversion_cache.get_or_render(track_id=track_id, format_type="tracker",
                                             subsong=0, render_fn=render)

    async def fake_encode(src, *, codec, bitrate_kbps=None, **kw):
        calls.append((Path(src).name, codec, bitrate_kbps))
        p = tmp_path / "e.mp3"
        p.write_bytes(b"ID3" + bytes(4000))
        return p
    monkeypatch.setattr(stream, "_do_prewarm_render", fake_prewarm_render)
    monkeypatch.setattr(stream, "_render_to_transcoded_flac", fake_encode)
    serve = stream._prewarm_serve(_req(), target_format="mp3", max_bitrate_kbps=192)
    await stream._prewarm_render_then_encode(tid, tmp_path / "x.mod", ".mod", None, False,
                                             None, serve)
    assert calls and calls[0][1:] == ("mp3", 192)
    tkey = conversion_cache._cache_key(
        tid, "transcoded", 0, codec="mp3", bitrate=192,
        variant=conversion_cache.rendered_transcode_variant(rkey))
    assert await conversion_cache.get_cached(tkey) is not None   # what _serve_rendered reads


@pytest.mark.asyncio
async def test_a_native_next_track_asked_as_mp3_is_prepared_as_that_mp3(monkeypatch, tmp_path):
    f = tmp_path / "n.flac"
    f.write_bytes(b"fLaC" + bytes(64))
    tid = f"pn-{uuid.uuid4().hex[:6]}"
    track = types.SimpleNamespace(id=tid, path=str(f), duration=60.0, bitrate=900)

    async def get_track(t):
        return track
    monkeypatch.setattr(stream, "get_track", get_track)

    async def resolve(track_id, tr):
        return f, ".flac", False, None
    monkeypatch.setattr(stream, "_resolve_for_prewarm", resolve)
    did = []

    async def fake_gor(**kw):
        did.append((kw["format_type"], kw.get("codec")))
        return Path("/x"), False
    monkeypatch.setattr(conversion_cache, "get_or_render", fake_gor)
    await stream._do_prewarm(tid, track, None, stream.PRIO_NEXT,
                             serve=stream._prewarm_serve(_req(), target_format="mp3"))
    assert did == [("transcoded", "mp3")]
    did.clear()
    await stream._do_prewarm(tid, track, None, stream.PRIO_NEXT,
                             serve=stream._prewarm_serve(_req()))
    assert did == []                                     # played as is: nothing


# ── Second QA pass ─────────────────────────────────────────────────────────

async def _disconnect_while_send_blocks(resp, *, block_after: int = 1,
                                        disconnect_after: float = 0.3) -> None:
    """Run ``resp`` as uvicorn (ASGI spec 2.3) + Starlette 1.7 do: the client
    stops reading after ``block_after`` body messages (``send`` blocks, TCP
    back-pressure), then goes away (``http.disconnect``)."""
    sent = {"n": 0}
    never = asyncio.Event()

    async def send(msg):
        if msg["type"] == "http.response.body":
            sent["n"] += 1
            if sent["n"] >= block_after:
                await never.wait()

    calls = {"n": 0}

    async def receive():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(disconnect_after)
        return {"type": "http.disconnect"}
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "GET", "headers": [], "path": "/x"}
    await resp(scope, receive, send)


@contextlib.contextmanager
def _gc_off():
    import gc
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


@pytest.mark.asyncio
@_SKIP_NO_FFMPEG
@pytest.mark.parametrize("web", [False, True])
async def test_a_pump_listener_gone_at_a_blocked_send_is_released_at_once(monkeypatch, tmp_path,
                                                                         slots, web):
    """N1: before, the body generator stayed suspended at its ``yield`` until a
    cyclic GC pass — subscriber counted, pump never stoppable."""
    src = _slow_pump_source(monkeypatch, tmp_path)
    tid = f"n1-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(src), duration=30.0)
    req = _req(cookies={"sb_session": "x"} if web else None)
    resp = await stream._serve_inflight_wav(req, track, src, tid, None, None, "pcm", None)
    inflight = stream._INFLIGHT_TRANSCODES[tid]
    with _gc_off():
        await _disconnect_while_send_blocks(resp)
        del resp
        await asyncio.sleep(0.1)
        alive = any(getattr(g, "ag_frame", None) is not None for g in list(inflight["bodies"]))
        assert not alive and inflight["subscribers"] == 0
        assert inflight["listen"]["last_read"] is not None
    inflight["pump_task"].cancel()
    with contextlib.suppress(BaseException):
        await inflight["pump_task"]


def _fds_on(path) -> int:
    st0 = os.stat(path)
    n = 0
    for fd in range(3, 4096):
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if st.st_ino == st0.st_ino and st.st_dev == st0.st_dev:
            n += 1
    return n


@pytest.mark.asyncio
async def test_a_range_body_gone_at_a_blocked_send_closes_its_descriptor_at_once(tmp_path):
    """N1 (range bodies): before, the descriptor stayed open until GC."""
    f = tmp_path / "big.mp3"
    f.write_bytes(os.urandom(4 * 1024 * 1024))
    with _gc_off():
        resp = await stream._range_file_response(_req({"Range": "bytes=0-"}), f, "audio/mpeg")
        await _disconnect_while_send_blocks(resp)
        del resp
        await asyncio.sleep(0.3)
        assert _fds_on(f) == 0
        whole = await stream._range_file_response(_req(), f, "audio/mpeg")
        await _disconnect_while_send_blocks(whole)
        del whole
        await asyncio.sleep(0.3)
        assert _fds_on(f) == 0


@pytest.mark.asyncio
async def test_a_catalogue_body_gone_at_a_blocked_send_closes_the_archive_stream(monkeypatch):
    from starlette.requests import Request
    data = os.urandom(3 * 256 * 1024)
    ups: list = []

    class Src:
        def open_stream(self, path, *, offset=0, lane="stream", length=None):
            up = _Up(data, offset, length, {})
            ups.append(up)
            return up
    track = types.SimpleNamespace(id="n1c", path="https://example.invalid/pub:/a/b.mp3",
                                  mtime=0, duration=0)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {"n1c"})
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b""})
    with _gc_off():
        resp = await stream._catalogue_passthrough(req, track, Src(), "/a/b.mp3", "audio/mpeg")
        await _disconnect_while_send_blocks(resp)
        del resp
        await asyncio.sleep(0.3)
        assert ups and ups[0].closed


@pytest.mark.asyncio
async def test_a_library_root_is_one_mount_and_closes_have_their_own_threads(monkeypatch):
    """QA2 minor 1: with the library at /music (the Docker layout), every
    artist is the same mount; a hung /music leaves /data its reads."""
    from soniqboom.core import playback_io
    monkeypatch.setattr(playback_io, "_ROOTS_PROVIDER", lambda: ["/music", "/data"])
    monkeypatch.setattr(playback_io, "_ROOTS", (0.0, ()))
    monkeypatch.setenv("SONIQBOOM_PLAYBACK_IO_THREADS", "16")
    playback_io.release()
    keys = {playback_io.mount_key(f"/music/Artist {a}/x.flac") for a in range(8)}
    assert keys == {"/music"}
    assert playback_io.mount_key("/data/cache/x.wav") == "/data"
    hang = threading.Event()
    tasks = [asyncio.ensure_future(playback_io.run(playback_io.mount_key(f"/music/A{a}/x"),
                                                   hang.wait, 20))
             for a in range(8) for _ in range(playback_io.per_mount_limit())]
    try:
        await asyncio.sleep(0.2)
        assert await asyncio.wait_for(
            playback_io.run(playback_io.mount_key("/data/x"), lambda: "ok"), 3) == "ok"
        names = []
        done = threading.Event()
        playback_io._close_pool().submit(lambda: (names.append(threading.current_thread().name),
                                                  done.set()))
        assert done.wait(2) and names[0].startswith("sb-playback-io-close")
    finally:
        hang.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        playback_io.release()


@pytest.mark.asyncio
@_SKIP_NO_FFMPEG
async def test_a_request_at_another_rate_keeps_the_tracks_pump_wanted(monkeypatch, tmp_path,
                                                                       slots):
    """QA2 minor 2: one pump per track; the interest is the track's."""
    src = _slow_pump_source(monkeypatch, tmp_path)
    tid = f"q2r-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(src), duration=30.0)
    resp = await stream._serve_inflight_wav(_req(), track, src, tid, 96000, 2, "dsd", None)
    inflight = stream._INFLIGHT_TRANSCODES[tid]
    entry = next(e for t, e in stream._PREEMPTIBLE.items() if t is inflight["pump_task"])
    gate = asyncio.Event()
    real = stream._get_or_start_inflight_wav

    async def slow_attach(**kw):
        await gate.wait()
        return await real(**kw)
    monkeypatch.setattr(stream, "_get_or_start_inflight_wav", slow_attach)
    t = asyncio.ensure_future(stream._serve_inflight_wav(_req(), track, src, tid, 48000, 2,
                                                         "dsd", None))
    await asyncio.sleep(0.05)
    assert entry["wanted"]()                             # the 48 kHz request waits on it
    gate.set()
    r2 = await t
    await resp.body_iterator.aclose()
    await r2.body_iterator.aclose()
    inflight["pump_task"].cancel()
    with contextlib.suppress(BaseException):
        await inflight["pump_task"]


@pytest.mark.asyncio
async def test_the_stat_cache_stays_bounded_for_whole_file_answers(tmp_path, monkeypatch):
    """QA2 minor 3."""
    monkeypatch.setattr(stream, "_STAT_CACHE_MAX", 50)
    stream._STAT_CACHE.clear()
    for i in range(200):
        f = tmp_path / f"f{i}.mp3"
        f.write_bytes(b"x")
        await stream._range_file_response(_req(), f, "audio/mpeg")
    assert len(stream._STAT_CACHE) <= 50
    stream._STAT_CACHE.clear()


@pytest.mark.asyncio
async def test_play_path_file_work_runs_on_the_playback_threads(tmp_path, monkeypatch):
    """QA2 minor 4: libgme's source read and emulation, the AHX sniff, the
    HEAD stat and a whole file's reads — on ``sb-playback-io`` threads."""
    from soniqboom.core import gme_render
    names: dict = {}

    def rec(tag, fn):
        def w(*a, **k):
            names[tag] = threading.current_thread().name
            return fn(*a, **k)
        return w
    f = tmp_path / "t.nsf"
    f.write_bytes(b"NESM\x1a" + bytes(200))
    monkeypatch.setattr(gme_render, "is_available", lambda: True)
    monkeypatch.setattr(gme_render, "render_plan", rec("plan", lambda d, s, dur: (1.0, None)))
    monkeypatch.setattr(gme_render, "render_pcm",
                        rec("pcm", lambda data, sub, dur, sink, fade_at_s=None: 0))
    real_rb = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (names.__setitem__("read", threading.current_thread().name),
                                      real_rb(self))[1])
    key = conversion_cache._cache_key("gt", "gme", 0)
    tok = conversion_cache.rendering_key.set(key)
    try:
        with contextlib.suppress(Exception):
            await stream._render_gme(f, live_key=key)
    finally:
        conversion_cache.rendering_key.reset(tok)
    monkeypatch.setattr(Path, "read_bytes", real_rb)
    monkeypatch.setattr(stream, "_is_ahx", rec("ahx", lambda p: False))
    assert await stream._ahx_play_seconds(f) is None
    g = tmp_path / "w.mp3"
    g.write_bytes(os.urandom(3 * 1024 * 1024))
    real_pread = os.pread
    monkeypatch.setattr(os, "pread", rec("whole", real_pread))
    r = await stream._range_file_response(_req(), g, "audio/mpeg")
    assert await _collect(r.body_iterator) == g.read_bytes()
    for tag in ("read", "plan", "pcm", "ahx", "whole"):
        assert names.get(tag, "").startswith("sb-playback-io"), (tag, names)


@pytest.mark.asyncio
@_SKIP_NO_FFMPEG
async def test_pump_writes_are_off_the_loop_and_each_pump_has_its_own_partial(monkeypatch,
                                                                             tmp_path, slots):
    """QA2 minors 4 + 8."""
    src = _slow_pump_source(monkeypatch, tmp_path)
    names = []
    real = stream._write_flush
    monkeypatch.setattr(stream, "_write_flush",
                        lambda f, d: (names.append(threading.current_thread().name), real(f, d)))
    tid = f"q8-{uuid.uuid4().hex[:8]}"
    track = types.SimpleNamespace(id=tid, path=str(src), duration=30.0)
    resp = await stream._serve_inflight_wav(_req(), track, src, tid, None, None, "pcm", None)
    first = stream._INFLIGHT_TRANSCODES[tid]
    p1 = first["wav_path"]
    async for _c in resp.body_iterator:
        break
    await resp.body_iterator.aclose()
    assert await _until(lambda: bool(names), 3)
    assert all(n.startswith("sb-playback-io") for n in names)
    first["stopped"] = True                           # as a stop marks it…
    old = first["pump_task"]
    gate = threading.Event()
    real_unlink = Path.unlink

    def slow_unlink(self, *a, **k):                   # …and the old pump is slow to go
        if self == p1:
            gate.wait(5)
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", slow_unlink)
    old.cancel()
    t = asyncio.ensure_future(stream._serve_inflight_wav(_req(), track, src, tid, None, None,
                                                         "pcm", None))
    await asyncio.sleep(0.3)
    gate.set()
    r2 = await asyncio.wait_for(t, 15)
    second = stream._INFLIGHT_TRANSCODES[tid]
    assert second is not first and second["wav_path"] != p1
    assert Path(second["wav_path"]).exists() and not p1.exists()
    await r2.body_iterator.aclose()
    second["pump_task"].cancel()
    with contextlib.suppress(BaseException):
        await second["pump_task"]


@pytest.mark.asyncio
async def test_a_cancelled_catalogue_open_closes_the_stream_it_produces(monkeypatch):
    """QA2 minor 5."""
    from starlette.requests import Request
    data = os.urandom(5000)
    ups: list = []
    opening = threading.Event()

    class Src:
        def open_stream(self, path, *, offset=0, lane="stream", length=None):
            opening.set()
            time.sleep(0.3)
            up = _Up(data, offset, length, {})
            ups.append(up)
            return up
    track = types.SimpleNamespace(id="q5", path="https://example.invalid/pub:/a/b.mp3",
                                  mtime=0, duration=0)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {"q5"})
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b""})
    t = asyncio.ensure_future(stream._catalogue_passthrough(req, track, Src(), "/a/b.mp3",
                                                            "audio/mpeg"))
    while not opening.is_set():
        await asyncio.sleep(0.01)
    t.cancel()
    with contextlib.suppress(BaseException):
        await t
    await asyncio.sleep(0.6)
    assert ups and ups[0].closed


@pytest.mark.asyncio
async def test_if_range_on_the_single_range_responders_gets_the_whole(tmp_path):
    """QA2 minor 6: those answers state no validator, so an If-Range never
    matches — the whole representation (200), never a part."""
    f = tmp_path / "g.wav"
    payload = os.urandom(200_000)
    f.write_bytes(payload)
    done = asyncio.Event()
    done.set()
    r = await stream._growing_file_range_response(
        _req({"range": "bytes=10-19", "if-range": '"x"'}), f, len(payload), done,
        media_type="audio/wav")
    assert r.status_code == 200 and await _collect(r.body_iterator) == payload
    wav = tmp_path / "o.wav"
    wav.write_bytes(stream._build_wav_header(44100, 2, 44100, 16) + bytes(44100 * 4))
    r = await stream._wav_offset_response(_req({"range": "bytes=0-9", "if-range": '"x"'}), wav,
                                          0.5, {}, None)
    assert r.status_code == 200 and "content-range" not in r.headers
    assert stream._request_audio_range(_req({"range": "bytes=0-9"}), 100) == (0, 10, True)
    assert stream._request_audio_range(_req({"range": "bytes=0-9", "if-range": "Tue, 01 Sep 2026 10:00:00 GMT"}), 100) == (0, 100, False)


@pytest.mark.asyncio
async def test_a_prewarm_never_stops_a_render_a_listener_paused(slots, monkeypatch):
    """QA2 minor 7: past the grace a listener's play may stop it, a prewarm
    may not."""
    ev: dict = {}
    tid = f"q7-{uuid.uuid4().hex[:8]}"
    key = conversion_cache._cache_key(tid, "uade", 0)
    k, it, _, _ = await _play(tid, _live_render_fn(key, 8.0, 6.0, ev))
    await it.aclose()
    await asyncio.sleep(0.4)                             # past the (test) grace
    await slots.acquire()

    async def prewarm_acquire():
        tok = stream._BG_PRIO.set(stream.PRIO_NEXT)
        try:
            await slots.acquire(timeout=0.8)
            slots.release()
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            stream._BG_PRIO.reset(tok)
    assert await prewarm_acquire() is False and "stopped" not in ev
    await slots.acquire(timeout=2)                       # a listener's play does
    assert ev.get("stopped") == [key]
    slots.release()
    slots.release()


def test_listener_tags_are_per_device_and_a_prefetch_is_no_skip():
    """QA2 minor 7: two devices of one user are two listeners; a play of
    another track made while the old one was still being read (a prefetch)
    doesn't count as moving on."""
    a = _req({"user-agent": "Amperfy/1.0"})
    a.client = types.SimpleNamespace(host="10.0.0.2")
    b = _req({"user-agent": "Mozilla/5.0 Safari"})
    b.client = types.SimpleNamespace(host="10.0.0.3")
    assert stream._client_tag(a, None, "u") != stream._client_tag(b, None, "u")
    tag = "dev-1"
    stream._note_client_play(tag, "B")
    t_prefetch = time.monotonic()
    assert not stream._moved_on(tag, "A", last_read=t_prefetch + 10)   # A read on after it
    assert stream._moved_on(tag, "A", last_read=t_prefetch - 1)        # a skip
    assert not stream._moved_on(tag, "B", last_read=t_prefetch - 1)    # same track


def test_a_handle_closed_in_a_worker_sets_the_last_read_before_it_reads_closed():
    """QA2 nit: ``on_closing`` runs before the descriptor reads as closed."""
    import tempfile as _tf
    fd, name = _tf.mkstemp()
    state = {"last_read": None}
    seen = []
    h = stream._FdHandle(fd, on_closing=lambda: state.__setitem__("last_read", 1.0))

    def closer():
        h.close()
        seen.append((h.fd, state["last_read"]))
    th = threading.Thread(target=closer)
    th.start()
    th.join()
    os.unlink(name)
    assert seen == [(-1, 1.0)]


def _page_req(pg=None, *, host="10.0.0.9", ua="Mozilla/5.0 Firefox"):
    r = _req({"user-agent": ua}, cookies={"sb_session": "sess-1"})
    r.client = types.SimpleNamespace(host=host)
    r.query_params = {"pg": pg} if pg is not None else {}
    return r


def test_two_pages_of_one_browser_are_two_listeners():
    """Two tabs share session, address and player: their page id (``pg``)
    tells them apart.  Without one (Subsonic apps, DLNA, cast devices) the
    tag is the device tag it always was."""
    import hashlib
    a = stream._client_tag(_page_req("a" * 32), "sess-1", None)
    b = stream._client_tag(_page_req("b" * 32), "sess-1", None)
    assert a != b
    assert a == stream._client_tag(_page_req("a" * 32), "sess-1", None)   # stable per page
    legacy = hashlib.sha1("s:sess-1|10.0.0.9|Mozilla/5.0 Firefox".encode()).hexdigest()[:16]
    assert stream._client_tag(_page_req(None), "sess-1", None) == legacy
    for junk in ("", "x", "a|b" * 8, "é" * 10, "a" * 65):    # not a page id: ignored
        assert stream._client_tag(_page_req(junk), "sess-1", None) == legacy


@pytest.mark.asyncio
async def test_a_play_in_another_tab_keeps_a_paused_tabs_render(slots, monkeypatch):
    """Decision 3: tab A pauses mid-render (its body closed), tab B of the same
    browser starts another track — A's render is not "moved on" (before: one
    tag for both tabs, so B's play stopped it at once).  A skip in A itself
    still frees the slot at once."""
    monkeypatch.setattr(stream, "_ABANDON_GRACE_S", 12.0)
    ev: dict = {}
    tid = f"pg-{uuid.uuid4().hex[:8]}"
    key = conversion_cache._cache_key(tid, "uade", 0)
    tag_a = stream._client_tag(_page_req("a" * 32), "sess-1", None)
    tag_b = stream._client_tag(_page_req("b" * 32), "sess-1", None)
    tok = stream._CLIENT_TAG.set(tag_a)
    try:
        stream._note_client_play(tag_a, tid)
        resp = await stream._serve_live(_req({"Range": "bytes=0-65535"}), track_id=tid,
                                        format_type="uade", subsong=0, key=key,
                                        render_fn=_live_render_fn(key, 8.0, 6.0, ev),
                                        web_session=True, background=None, xf=None,
                                        rendered="t")
        body = await _collect(resp.body_iterator)            # A read, then paused
        assert len(body) == 65536
        await slots.acquire()                                # the other slot is busy
        stream._note_client_play(tag_b, "another-track")     # tab B starts a track
        with pytest.raises(asyncio.TimeoutError):            # B's render waits…
            await slots.acquire(timeout=1.0)
        assert "stopped" not in ev                           # …A's render is kept
        stream._note_client_play(tag_a, "a-next-track")      # tab A skips
        await slots.acquire(timeout=2.0)                     # now it goes at once
        assert ev["stopped"] == [key]
        slots.release()
        slots.release()
    finally:
        stream._CLIENT_TAG.reset(tok)


@pytest.mark.asyncio
async def test_a_page_that_goes_away_frees_its_paused_render_at_once(slots, monkeypatch):
    """A reloaded / closed tab (``pagehide`` → ``POST /stream/page-gone`` with
    its ``pg``) counts as moved on at once — its old render doesn't hold a
    slot for the grace period while the reloaded page (a new ``pg``) waits."""
    monkeypatch.setattr(stream, "_ABANDON_GRACE_S", 12.0)

    async def ok(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", ok)
    ev: dict = {}
    tid = f"pgg-{uuid.uuid4().hex[:8]}"
    key = conversion_cache._cache_key(tid, "uade", 0)
    tag_a = stream._client_tag(_page_req("a" * 32), "sess-1", None)
    tok = stream._CLIENT_TAG.set(tag_a)
    try:
        stream._note_client_play(tag_a, tid)
        resp = await stream._serve_live(_req({"Range": "bytes=0-65535"}), track_id=tid,
                                        format_type="uade", subsong=0, key=key,
                                        render_fn=_live_render_fn(key, 8.0, 6.0, ev),
                                        web_session=True, background=None, xf=None,
                                        rendered="t")
        await _collect(resp.body_iterator)
        await slots.acquire()
        stream._note_client_play(stream._client_tag(_page_req("c" * 32), "sess-1", None), "x")
        with pytest.raises(asyncio.TimeoutError):            # the reloaded page waits…
            await slots.acquire(timeout=0.6)
        bad = await stream.page_gone(_page_req("x"), sb_session="sess-1")    # not a page id
        assert bad.status_code == 204
        with pytest.raises(asyncio.TimeoutError):
            await slots.acquire(timeout=0.4)
        assert "stopped" not in ev
        r = await stream.page_gone(_page_req("a" * 32), sb_session="sess-1")  # tab A went
        assert r.status_code == 204
        await slots.acquire(timeout=2.0)                     # …now at once
        assert ev["stopped"] == [key]
        slots.release()
        slots.release()
    finally:
        stream._CLIENT_TAG.reset(tok)
