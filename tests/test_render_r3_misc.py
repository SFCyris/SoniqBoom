# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review round 3, remaining render items: an estimated Content-Length for
live MP3 transcodes (Subsonic ``estimateContentLength``), and /render-status
reporting a render that just failed.

In-process only (httpx ASGITransport, throw-away cache dirs, fake renderers).
"""
from __future__ import annotations

import asyncio
import random
import shutil
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream
from soniqboom.core import conversion_cache

from test_render_r1_stream import DW, _T, _app, _client

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


def _noise_wav(path: Path, seconds: float) -> Path:
    rng = random.Random(1)
    frames = int(seconds * 44100)
    path.write_bytes(stream._build_wav_header(44100, 2, frames, bits_per_sample=16)
                     + rng.randbytes(frames * 4))
    return path


async def _body(resp) -> bytes:
    out = bytearray()
    async for chunk in resp.body_iterator:
        out += chunk
    return bytes(out)


# ── r3-ren-13: estimated Content-Length for a live MP3 transcode ───────────

def test_lame_bitrate_prediction():
    # measured LAME choices at 44.1 kHz (ties go down) and at 22.05 kHz
    for req, lame in ((0, 128), (100, 96), (104, 96), (118, 112), (144, 128),
                      (150, 160), (400, 320), (8, 32), (33, 32)):
        assert stream._mp3_cbr_kbps(req, 44100) == lame, req
    for req, lame in ((140, 144), (150, 144), (320, 160)):
        assert stream._mp3_cbr_kbps(req, 22050) == lame, req
    # rate unknown: never below either table
    for req in (8, 100, 140, 144, 150, 320):
        assert stream._mp3_cbr_kbps(req) >= max(stream._mp3_cbr_kbps(req, 44100),
                                                stream._mp3_cbr_kbps(req, 22050))


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize("kbps", [0, 96, 128, 320])
async def test_estimated_length_is_exact_on_the_wire(tmp_path, kbps):
    src = _noise_wav(tmp_path / "n.wav", 12.0)
    xf = stream._Xform("mp3", kbps, 0.0)
    with stream.estimated_content_length(0):            # unknown → the WAV's header
        resp = stream._live_transcode_response(src, xf, {}, None)
    est = int(resp.headers["content-length"])
    assert est == int(12.0 * stream._mp3_cbr_kbps(kbps, 44100) * 125 * 1.005) + 16384
    body = await _body(resp)
    assert len(body) == est
    # the encode itself ended just short of it: the rest is padding
    real = len(body.rstrip(b"\0"))
    assert est - 40_000 < real < est
    # without the flag: chunked, as before
    resp = stream._live_transcode_response(src, xf, {}, None)
    assert "content-length" not in resp.headers
    await _body(resp)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_estimate_is_honoured_both_ways_and_only_for_mp3(tmp_path):
    import subprocess
    src = _noise_wav(tmp_path / "n.wav", 10.0)
    # a WAV's own header beats the stated length (a rendered track's real one)
    with stream.estimated_content_length(4.0):
        resp = stream._live_transcode_response(src, stream._Xform("mp3", 128, 0.0), {}, None)
    assert int(resp.headers["content-length"]) == int(10.0 * 128 * 125 * 1.005) + 16384
    await _body(resp)
    # another source with a stated length shorter than its audio: cut there
    flac = tmp_path / "n.flac"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                    str(flac)], check=True, timeout=60)
    with stream.estimated_content_length(4.0):
        resp = stream._live_transcode_response(flac, stream._Xform("mp3", 128, 0.0), {}, None)
    est = int(resp.headers["content-length"])
    assert est == int(4.0 * 128 * 125 * 1.005) + 16384
    body = await _body(resp)
    assert len(body) == est and body[-1000:].count(0) < 900      # real audio to the end
    # a time offset estimates the rest of the track
    with stream.estimated_content_length(10.0):
        resp = stream._live_transcode_response(src, stream._Xform("mp3", 128, 6.0), {}, None)
    assert int(resp.headers["content-length"]) == int(4.0 * 128 * 125 * 1.005) + 16384
    assert len(await _body(resp)) == int(resp.headers["content-length"])
    # VBR / lossless stay chunked
    for codec, br in (("ogg", 128), ("flac", 0)):
        with stream.estimated_content_length(10.0):
            resp = stream._live_transcode_response(src, stream._Xform(codec, br, 0.0), {}, None)
        assert "content-length" not in resp.headers
        await _body(resp)


@pytest.mark.asyncio
async def test_a_failed_encode_is_not_padded():
    async def empty():
        if False:
            yield b""
    assert [c async for c in stream._exactly(empty(), 1000)] == []

    async def short():
        yield b"ab"
    assert b"".join([c async for c in stream._exactly(short(), 5)]) == b"ab\0\0\0"


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("uade123") or not DW.exists(), reason="uade123 missing")
async def test_subsonic_style_cold_transcode_of_a_render_carries_the_estimate(
        monkeypatch, tmp_path):
    app, _ = _app(monkeypatch, tmp_path, {"est1": _T("est1", DW, 0.0)})
    monkeypatch.setattr(stream, "_backfill_rendered_duration",
                        lambda *a, **k: asyncio.sleep(0))
    async with _client(app) as c:
        with stream.estimated_content_length(0):
            r = await c.get("/api/stream/est1?u=a&p=b&format=mp3&maxBitRate=128")
        assert r.status_code == 200 and r.headers["x-transcode-cache"] == "miss"
        assert r.headers["x-content-length-estimated"] == "1"
        assert int(r.headers["content-length"]) == len(r.content)
        r2 = await c.get("/api/stream/est1?u=a&p=b&format=mp3&maxBitRate=128")
        # no flag: whatever path serves it, no estimate header
        assert "x-content-length-estimated" not in r2.headers


# ── r3-ren-14: /render-status reports a render that just failed ────────────

@pytest.mark.asyncio
async def test_render_status_says_failed_with_the_reason(monkeypatch, tmp_path):
    t = _T("rf1", tmp_path / "bad.dw", 0.0)
    Path(t.path).write_bytes(b"not a module")
    app, _ = _app(monkeypatch, tmp_path, {"rf1": t})
    monkeypatch.setattr(stream, "_backfill_rendered_duration",
                        lambda *a, **k: asyncio.sleep(0))
    calls = []

    async def render(path, subsong=0, with_vu=True, *, live_key=None,
                     expected_seconds=0.0, subsong_base=0):
        calls.append(subsong)
        if len(calls) == 1:
            raise HTTPException(422, "This file isn't a playable Amiga module")
        out = tmp_path / f"ok{len(calls)}.wav"
        out.write_bytes(stream._build_wav_header(44100, 2, 44100, 16) + bytes(44100 * 4))
        return out
    monkeypatch.setattr(stream, "_render_uade", render)
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/rf1", cookies={"sb_session": "x"})
            assert r.status_code == 422
            st = (await c.get("/api/stream/rf1/render-status")).json()
            assert st["state"] == "failed" and st["error_status"] == 422
            assert st["error"] == "This file isn't a playable Amiga module"
            # another tune of the same file is untouched
            st1 = (await c.get("/api/stream/rf1/render-status?subsong=1")).json()
            assert st1["state"] == "idle" and "error" not in st1
            # the failure expires …
            for k, v in list(conversion_cache._recent_failures.items()):
                conversion_cache._recent_failures[k] = (v[0] - 61, v[1], v[2])
            assert (await c.get("/api/stream/rf1/render-status")).json()["state"] == "idle"
            # … and a new play renders again (never a negative cache)
            r = await c.get("/api/stream/rf1", cookies={"sb_session": "x"})
            assert r.status_code == 200 and len(calls) == 2
            assert (await c.get("/api/stream/rf1/render-status")).json()["state"] == "complete"
    finally:
        conversion_cache._purge_entry(stream._ck("rf1", "uade", subsong=0))


@pytest.mark.asyncio
async def test_a_successful_render_clears_a_recorded_failure(tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    key = stream._ck("rf2", "tracker", subsong=0)
    conversion_cache.note_render_failure(key, 502, "renderer exited with status 1")
    assert conversion_cache.recent_failure("rf2", 0) == {
        "status": 502, "detail": "renderer exited with status 1"}

    async def ok():
        out = tmp_path / "rf2.wav"
        out.write_bytes(stream._build_wav_header(44100, 2, 100, 16) + bytes(400))
        return out
    try:
        await conversion_cache.get_or_render(track_id="rf2", format_type="tracker",
                                             subsong=0, render_fn=ok)
        assert conversion_cache.recent_failure("rf2", 0) is None
    finally:
        conversion_cache._purge_entry(key)


@pytest.mark.asyncio
async def test_a_cancelled_render_is_not_a_failure(tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)
    task = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id="rf3", format_type="tracker", subsong=0, render_fn=slow))
    await started.wait()
    inner = next(t for t in conversion_cache._detached_renders if not t.done())
    inner.cancel()                               # e.g. a retired SID prewarm
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert conversion_cache.recent_failure("rf3", 0) is None
