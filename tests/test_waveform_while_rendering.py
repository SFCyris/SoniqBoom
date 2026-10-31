# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seek bar's waveform drawn while the audio renders (``?partial=1``):
the part of a render written so far (``tracks._partial_peaks``), for a render
whose length is known (``stream.growing_render``)."""
from __future__ import annotations

import array
import asyncio
import math
import struct
import sys

import pytest

from soniqboom.api import stream as S
from soniqboom.api import tracks as T


def _header(rate, ch, bits, frames, *, fmt=1, extra=b""):
    data = frames * ch * bits // 8
    fmt_chunk = struct.pack("<HHIIHH", fmt, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt_chunk) + len(extra) + 8 + data) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk + extra
            + b"data" + struct.pack("<I", data))


def _samples(frames, ch, amp_of):
    """Interleaved samples, frame f at amplitude amp_of(f) (0..1)."""
    return [amp_of(f) * (1 if (f + c) % 2 else -1) for f in range(frames) for c in range(ch)]


def _pcm(vals, bits, float_):
    if float_:
        a = array.array("f", vals)
    elif bits == 16:
        a = array.array("h", [int(round(v * 32767)) for v in vals])
    elif bits == 32:
        a = array.array("i", [int(round(v * 2147483647)) for v in vals])
    else:                                                  # 24-bit
        out = bytearray()
        for v in vals:
            out += int(round(v * 8388607)).to_bytes(3, "little", signed=True)
        return bytes(out)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def test_data_offset_walks_the_chunks():
    h = _header(44100, 2, 16, 10)
    assert T._wav_data_offset(h) == 44
    h2 = _header(44100, 2, 16, 10, extra=b"LIST" + struct.pack("<I", 5) + b"abcde\x00")
    assert T._wav_data_offset(h2) == 44 + 14
    assert T._wav_data_offset(b"not a wav at all") is None


@pytest.mark.parametrize("bits,float_", [(16, False), (24, False), (32, True), (32, False)])
def test_points_follow_the_file_as_it_grows(tmp_path, bits, float_):
    T._PARTIAL_STATE.clear()
    total, ch = 40_000, 2
    amp = lambda f: 0.1 + 0.8 * (f / total)                # louder towards the end
    pcm = _pcm(_samples(total, ch, amp), bits, float_)
    path = tmp_path / f"r{bits}{float_}.wav"
    head = _header(48000, ch, bits, total, fmt=3 if float_ else 1)
    fb = ch * bits // 8
    g = {"key": f"k{bits}{float_}", "path": str(path), "total_frames": total,
         "channels": ch, "bits": bits, "float": float_}
    path.write_bytes(head)
    pts, prog = T._partial_peaks(g)
    assert prog == 0.0 and pts == [None] * T._PARTIAL_POINTS
    seen = []
    for frac in (0.25, 0.6, 1.0):                           # the render writes on
        upto = int(total * frac)
        path.write_bytes(head + pcm[:upto * fb])
        pts, prog = T._partial_peaks(g)
        assert prog == pytest.approx(upto / total)
        drawn = sum(v is not None for v in pts)
        # every point whose stretch has begun is drawn, none further
        assert drawn == math.ceil(upto * T._PARTIAL_POINTS / total)
        assert max(v for v in pts if v is not None) == pytest.approx(1.0)
        seen.append(drawn)
    assert seen == sorted(seen) and seen[-1] == T._PARTIAL_POINTS
    # the whole file: the same shape as its true peaks (rising), each point
    # its stretch's largest sample over the loudest
    true = [amp(min(total - 1, -(-(p + 1) * total // T._PARTIAL_POINTS) - 1))
            for p in range(T._PARTIAL_POINTS)]
    for p in (0, 50, 120, 199):
        assert pts[p] == pytest.approx(true[p] / max(true), abs=0.01)


def test_unreadable_file_answers_none_and_a_headerless_one_nothing_yet(tmp_path):
    T._PARTIAL_STATE.clear()
    g = {"key": "gone", "path": str(tmp_path / "moved.wav"), "total_frames": 100,
         "channels": 1, "bits": 16, "float": False}
    assert T._partial_peaks(g) == (None, 0.0)
    # just begun: the renderer hasn't written its header yet
    path = tmp_path / "new.wav"
    path.write_bytes(b"")
    g = dict(g, key="new", path=str(path))
    assert T._partial_peaks(g) == ([None] * T._PARTIAL_POINTS, 0.0)
    path.write_bytes(_header(44100, 1, 16, 100) + _pcm([0.5] * 50, 16, False))
    pts, prog = T._partial_peaks(g)
    assert prog == 0.5 and pts[0] == pytest.approx(1.0) and pts[-1] is None


def test_growing_render_finds_a_sid_stream_and_a_live_render_of_known_length():
    ev_sid, ev_live, ev_unknown = asyncio.Event(), asyncio.Event(), asyncio.Event()
    S._SID_PROG_RENDERS["sidkey"] = {"tmp": "/tmp/x.wav", "data_bytes": 44100 * 2 * 10,
                                     "done_ev": ev_sid}
    S._UADE_LIVE["livekey"] = {"path": "/tmp/y.wav", "expected_size": 44 + 6 * 1000,
                               "complete": ev_live, "channels": 2, "bits": 24, "float": False}
    S._UADE_LIVE["unknown"] = {"path": "/tmp/z.wav", "expected_size": 0,
                               "complete": ev_unknown, "channels": 2, "bits": 24}
    try:
        g = S.growing_render(["nothing", "sidkey"])
        assert g["path"] == "/tmp/x.wav" and g["total_frames"] == 441000 and g["bits"] == 16
        g = S.growing_render(["livekey"])
        assert g["total_frames"] == 1000 and (g["channels"], g["bits"]) == (2, 24)
        assert S.growing_render(["unknown"]) is None        # its length isn't known
        ev_sid.set()
        assert S.growing_render(["sidkey"]) is None         # finished: the cache has it
    finally:
        S._SID_PROG_RENDERS.pop("sidkey", None)
        for k in ("livekey", "unknown"):
            S._UADE_LIVE.pop(k, None)


async def test_partial_lookup_returns_the_render_at_once_and_a_plain_one_waits(monkeypatch):
    """With ``partial_ok`` a running render of known length comes back at
    once; without, the lookup still waits for the cached file."""
    from soniqboom.core import conversion_cache as CC
    key = CC._cache_key("tid", "sid", 0, None, duration=180)
    done = asyncio.Event()
    S._SID_PROG_DONE[key] = done
    S._SID_PROG_RENDERS[key] = {"tmp": "/tmp/r.wav", "data_bytes": 180 * 44100 * 2,
                                "done_ev": done}

    async def _miss(k):
        return None
    monkeypatch.setattr(CC, "get_cached", _miss)
    try:
        g = await asyncio.wait_for(T._waveform_from_conversion_cache(
            "tid", "/music/x.sid", ".sid", sid_duration=180, partial_ok=True), 2)
        assert isinstance(g, dict) and g["path"] == "/tmp/r.wav"
        plain = asyncio.create_task(T._waveform_from_conversion_cache(
            "tid", "/music/x.sid", ".sid", sid_duration=180, finish_wait=0.5))
        await asyncio.sleep(0.2)
        assert not plain.done()                             # waits for the render
        assert await plain is T._WAVEFORM_PENDING
    finally:
        S._SID_PROG_DONE.pop(key, None)
        S._SID_PROG_RENDERS.pop(key, None)


async def test_a_growing_file_made_late_is_still_found(monkeypatch):
    """A render prepares its source before its growing file exists (a read
    from a share): the partial lookup keeps looking while it waits, not only
    at first (QA 2026-10-28)."""
    from soniqboom.core import conversion_cache as CC
    key = CC._cache_key("tid2", "sid", 0, None, duration=180)
    done = asyncio.Event()
    S._SID_PROG_DONE[key] = done

    async def _miss(k):
        return None
    monkeypatch.setattr(CC, "get_cached", _miss)

    async def _later():
        await asyncio.sleep(2.2)                      # past any short first window
        S._SID_PROG_RENDERS[key] = {"tmp": "/tmp/late.wav", "data_bytes": 180 * 44100 * 2,
                                    "done_ev": done}
    try:
        maker = asyncio.create_task(_later())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        g = await asyncio.wait_for(T._waveform_from_conversion_cache(
            "tid2", "/music/y.sid", ".sid", sid_duration=180, partial_ok=True), 5)
        assert isinstance(g, dict) and g["path"] == "/tmp/late.wav"
        assert 2.2 <= loop.time() - t0 < 3.0
        await maker
    finally:
        S._SID_PROG_DONE.pop(key, None)
        S._SID_PROG_RENDERS.pop(key, None)
