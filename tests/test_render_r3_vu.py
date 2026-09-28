# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amiga per-voice VU from review round 3: uade writes its Paula dump into a
FIFO that is parsed as it arrives (nothing stored on disk), and a tune whose
pass ran without a result is reported as ``X-VU-Unavailable: skipped`` so the
meter stops polling.

In-process only: synthetic dumps, fake uade scripts and the local test
modules (real uade123 tests skip when it is not installed).
"""
from __future__ import annotations

import asyncio
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from itertools import compress
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.api import tracks as tracks_api
from soniqboom.core import conversion_cache
from soniqboom.core import uade_vu as U

REPO = Path(__file__).resolve().parent.parent
DW = REPO / "internal/testdata/uade/David Whittaker/carrier command.dw"
needs_uade = pytest.mark.skipif(not shutil.which("uade123") or not DW.exists(),
                                reason="uade123 or the local Amiga test modules are missing")


def _dump_bytes(frames: int, seed: int) -> bytes:
    rng = random.Random(seed)
    out = bytearray(U.DUMP_MAGIC)
    for _ in range(frames):
        if rng.random() < 0.5:                                   # event frame
            out += bytes([0x80 | rng.randrange(128)]) + bytes(3) + bytes(
                rng.randrange(256) for _ in range(8))
        else:                                                    # audio frame
            out += bytes(4) + bytes(rng.randrange(256) for _ in range(8))
    return bytes(out)


def _binned_reference(data: bytes, duration_s: float, vu_rate_hz: int = 30) -> bytes:
    """What parse_stream promises: per window, the peak over the ``_BIN``-frame
    groups its audio frames touch, normalised like parse_dump."""
    body = data[U._HEADER:]
    body = body[:len(body) // U._FRAME * U._FRAME]
    keep = body[0::U._FRAME].translate(U._KEEP)
    cols = [bytes(compress(body[4 + 2 * ch::U._FRAME], keep)).translate(U._ABS2)
            for ch in range(4)]
    n = len(cols[0])
    peak = max(max(c) for c in cols)
    n_win = max(1, int(duration_s * vu_rate_hz))
    step = n / n_win
    mono = bytearray(n_win * 4)
    for k in range(n_win):
        s = int(k * step)
        e = max(s + 1, int((k + 1) * step))
        lo, hi = s // U._BIN * U._BIN, min(n, ((e - 1) // U._BIN + 1) * U._BIN)
        for ch in range(4):
            mono[k * 4 + ch] = max(cols[ch][lo:hi], default=0)
    if peak < 255:
        lut = bytes(min(255, (v * 255) // peak) for v in range(256))
        mono = mono.translate(lut)
    return bytes(mono)


def _feed_pipe(data: bytes, seed: int):
    """A read handle on a pipe a thread fills in random, frame-unaligned pieces."""
    r, w = os.pipe()
    rng = random.Random(seed)

    def writer():
        with os.fdopen(w, "wb", buffering=0) as fh:
            i = 0
            while i < len(data):
                n = rng.randrange(1, 40000)
                fh.write(data[i:i + n])
                i += n
    t = threading.Thread(target=writer)
    t.start()
    return os.fdopen(r, "rb", buffering=0), t


# ── r3-ren-12: one-pass parse of a streamed dump ───────────────────────────

@pytest.mark.parametrize("frames,dur,chunk", [(20000, 2.0, 1 << 16), (5000, 3.3, 7),
                                              (9000, 60.0, 300), (1000, 0.2, 64)])
def test_stream_parse_matches_the_binned_spec_and_never_undershoots(
        monkeypatch, tmp_path, frames, dur, chunk):
    monkeypatch.setattr(U, "_CHUNK_FRAMES", chunk)
    data = _dump_bytes(frames, seed=frames) + b"\x01\x02\x03"      # partial tail frame
    fh, t = _feed_pipe(data, seed=chunk)
    stats: dict = {}
    with fh:
        got = U.parse_stream(fh, dur, stats=stats)
    t.join()
    assert stats["bytes"] == len(data)
    assert got is not None and got.frames == max(1, int(dur * 30))
    assert got.mono == _binned_reference(data, dur)
    dump = tmp_path / "d.bin"
    dump.write_bytes(data)
    exact = U.parse_dump(dump, dur)
    assert all(b >= a for a, b in zip(exact.mono, got.mono))     # never below exact
    assert got.channels == 4 and got.pan == U._PAULA_PAN


def test_stream_parse_drains_bad_input_to_eof():
    for data in (b"not a dump" * 50_000,
                 U.DUMP_MAGIC + (b"\x80" + bytes(11)) * 50_000):   # events only
        fh, t = _feed_pipe(data, seed=len(data))
        stats: dict = {}
        with fh:
            assert U.parse_stream(fh, 5.0, stats=stats) is None
        t.join(5)
        assert not t.is_alive() and stats["bytes"] == len(data)   # the writer never blocks


_FAKE_UADE = r'''
import sys
data = open(sys.argv[1], "rb").read()
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--write-audio="))
with open(out, "wb") as f:
    for i in range(0, len(data), 50000):
        f.write(data[i:i + 50000])
'''

_FAKE_UADE_NO_DUMP = r'''
import sys
sys.stderr.write("uade123: unrecognized option `--write-audio'\n")
sys.exit(1)
'''


def _fake_uade(monkeypatch, tmp_path, script: str, data: bytes = b""):
    src = tmp_path / "dump.src"
    src.write_bytes(data)
    exe = tmp_path / "fake_uade.py"
    exe.write_text(script)
    monkeypatch.setattr(stream, "_uade_vu_cmd",
                        lambda binary, path, subsong, base, dump:
                            [sys.executable, str(exe), str(src), f"--write-audio={dump}"])


def _leftover_fifo_dirs() -> set:
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("uadevu-")}


@pytest.mark.asyncio
async def test_fifo_pass_parses_while_uade_writes_and_stores_nothing(monkeypatch, tmp_path):
    data = _dump_bytes(30000, seed=7)
    _fake_uade(monkeypatch, tmp_path, _FAKE_UADE, data)
    src = tmp_path / "tune.dw"
    src.write_bytes(b"x")
    before = _leftover_fifo_dirs()
    got = await stream._uade_vu_dump_fifo("uade123", src, 0, 4.0)
    assert got is not None and got.mono == _binned_reference(data, 4.0)
    assert _leftover_fifo_dirs() == before


@pytest.mark.asyncio
async def test_fifo_pass_of_a_build_without_write_audio_latches_quickly(monkeypatch,
                                                                       tmp_path):
    _fake_uade(monkeypatch, tmp_path, _FAKE_UADE_NO_DUMP)
    src = tmp_path / "tune.dw"
    src.write_bytes(b"x")
    t0 = time.monotonic()
    assert await stream._uade_vu_dump_fifo("uade123", src, 0, 4.0) is False
    assert time.monotonic() - t0 < 5          # the reader never hangs on the FIFO
    # a vanished source is never a build-wide verdict
    assert await stream._uade_vu_dump_fifo("uade123", tmp_path / "gone.dw", 0, 4.0) is None


@pytest.mark.asyncio
async def test_fifo_pass_times_out_without_leaking(monkeypatch, tmp_path):
    exe = tmp_path / "hang.py"
    exe.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(stream, "_uade_vu_cmd",
                        lambda *a: [sys.executable, str(exe)])
    real_wait_for = asyncio.wait_for

    async def short_wait_for(aw, timeout):
        return await real_wait_for(aw, min(timeout, 0.5))
    monkeypatch.setattr(stream.asyncio, "wait_for", short_wait_for)
    src = tmp_path / "tune.dw"
    src.write_bytes(b"x")
    before = _leftover_fifo_dirs()
    t0 = time.monotonic()
    assert await stream._uade_vu_dump_fifo("uade123", src, 0, 4.0) is None
    assert time.monotonic() - t0 < 5
    assert _leftover_fifo_dirs() == before


@needs_uade
@pytest.mark.asyncio
async def test_real_uade_fifo_pass_matches_the_file_dump(tmp_path):
    wav = await stream._render_uade(DW, with_vu=False)
    try:
        dur = stream._wav_audio_seconds(wav)
        got = await stream._uade_vu_dump("uade123", DW, 0, wav)
        ref_dump = tmp_path / "ref.uadedump"
        subprocess.run(stream._uade_vu_cmd(shutil.which("uade123"), DW, 0, 0, str(ref_dump)),
                       check=True, capture_output=True, timeout=120,
                       cwd=stream._uade_cwd(DW))
        ref = U.parse_dump(ref_dump, dur)
        assert got is not None and ref is not None and got.frames == ref.frames
        same = sum(1 for a, b in zip(ref.mono, got.mono) if a == b) / len(ref.mono)
        assert same > 0.99 and all(b >= a for a, b in zip(ref.mono, got.mono))
        # and the whole pass writes the sidecar
        assert await stream._uade_vu_pass("uade123", DW, 0, wav) is True
        from soniqboom.core.openmpt_vu import parse_and_validate_vumr
        channels, _rate, frames = parse_and_validate_vumr(wav.with_suffix(".vu").read_bytes())
        assert channels == 4 and frames == got.frames
    finally:
        wav.with_suffix(".vu").unlink(missing_ok=True)
        wav.unlink(missing_ok=True)


# ── r3-ren-8: a pass that ran without a result says "skipped" ──────────────

def _wav(path: Path, seconds: float) -> Path:
    frames = int(seconds * 44100)
    path.write_bytes(stream._build_wav_header(44100, 2, frames, bits_per_sample=16)
                     + b"\0" * (frames * 4))
    return path


@pytest.mark.asyncio
async def test_a_tune_too_long_for_the_pass_reports_skipped(monkeypatch, tmp_path):
    from fastapi import FastAPI
    import httpx
    tid = "vuskip1"
    key = stream._ck(tid, "uade", subsong=0)
    wav = _wav(tmp_path / "long.wav", 2.0)
    src = tmp_path / "long.dw"
    src.write_bytes(b"x")
    with conversion_cache._state_lock:
        conversion_cache._meta[key] = {"path": str(wav), "size_bytes": wav.stat().st_size,
                                       "format_type": "uade", "created_at": 0}
        conversion_cache._lru[key] = time.time()
    monkeypatch.setattr(stream, "_UADE_VU_MAX_TUNE_S", 1)
    monkeypatch.setattr(stream, "_UADE_VU_START_DELAY", 0)
    monkeypatch.setattr(stream, "_UADE_VU_MIN_PLAYED_S", 0)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/bin/true")
    monkeypatch.setattr(stream, "_uade_vu_enabled", lambda: True)
    monkeypatch.setattr(stream, "uade_vu_unavailable_reason", lambda: None)
    monkeypatch.setattr(conversion_cache, "_find_orphan_sidecar", lambda *a, **k: None)
    t = type("T", (), {"id": tid, "path": str(src), "duration": 2.0, "format": "DW"})()

    async def get_track(x):
        return t if x == tid else None
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            # not served yet: a plain 404, the player keeps asking
            r = await c.get(f"/api/tracks/{tid}/vu", params={"start": "false"})
            assert r.status_code == 404 and "x-vu-unavailable" not in r.headers
            stream._note_uade_vu_wanted(key, src, 0, None)
            r = await c.get(f"/api/tracks/{tid}/vu")               # starts the pass
            assert "x-vu-unavailable" not in r.headers
            for _ in range(100):
                if key not in stream._UADE_VU_INFLIGHT:
                    break
                await asyncio.sleep(0.01)
            r = await c.get(f"/api/tracks/{tid}/vu")
            assert r.status_code == 404 and r.headers["x-vu-unavailable"] == "skipped"
            # no second pass is started for it, not even by a new play …
            stream._note_uade_vu_wanted(key, src, 0, None)
            assert stream.request_uade_vu(tid, 0) is False
            # … until the marker ages out
            stream._UADE_VU_SKIPPED[key] -= stream._UADE_VU_SKIP_TTL_S + 1
            assert stream.uade_vu_skipped_reason(tid, 0) is None
    finally:
        conversion_cache._purge_entry(key)
        stream._UADE_VU_SKIPPED.pop(key, None)
        stream._UADE_VU_WANTED.pop(key, None)
        stream._UADE_VU_LAST_POLL.pop(key, None)


@pytest.mark.asyncio
async def test_a_pass_dropped_because_the_listener_left_is_not_skipped(monkeypatch,
                                                                      tmp_path):
    key = "vuleft3__sub0"
    wav = _wav(tmp_path / "w.wav", 2.0)
    src = tmp_path / "t.dw"
    src.write_bytes(b"x")
    with conversion_cache._state_lock:
        conversion_cache._meta[key] = {"path": str(wav), "size_bytes": wav.stat().st_size,
                                       "format_type": "uade", "created_at": 0}
        conversion_cache._lru[key] = time.time()
    monkeypatch.setattr(stream, "_UADE_VU_START_DELAY", 0)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/bin/true")
    monkeypatch.setattr(stream, "_uade_vu_enabled", lambda: True)
    stream._UADE_VU_LAST_POLL[key] = time.monotonic() - 3600     # stale: listener left
    try:
        stream._spawn_uade_vu(key, src, 0)
        for _ in range(100):
            if key not in stream._UADE_VU_INFLIGHT:
                break
            await asyncio.sleep(0.01)
        assert key not in stream._UADE_VU_SKIPPED
    finally:
        conversion_cache._purge_entry(key)
        stream._UADE_VU_LAST_POLL.pop(key, None)
