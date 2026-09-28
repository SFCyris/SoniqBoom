# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stream / render fixes from review round 2: exact uade lengths up to an hour
and render-status length fields, live first bytes for Subsonic transcodes,
the Amiga VU pass (chunked parse, off the render slot, dropped for tracks the
listener left), remote prewarm lanes and limits, low-power duration probes,
stream auth fast paths and lockout, chip-format lyrics, range answers for
progressive uade, per-subsong waveforms, status auth, X-VU-Unavailable,
uade subsong bases and per-page prewarm owners.

Everything runs in-process (httpx ASGITransport) against a throw-away
conversion cache and fake stores — never the live server or real data.
"""
from __future__ import annotations

import asyncio
import os
import random
import shutil
import threading
import time
import types
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.api import tracks as tracks_api
from soniqboom.core import conversion_cache

from test_render_r1_stream import (
    BD, CF, DW, UADE, FakeStore, _T, _app, _client, _ffmpeg, _pcm, _ref_render,
    needs_ffmpeg, needs_uade,
)


async def _noauth(*a, **k):
    """Stand-in for the (async) stream sign-in check."""
    return None


def _auth(*a):
    """Run the async stream sign-in check to completion (sync tests)."""
    return asyncio.run(stream._require_stream_auth(*a))


HIPC = UADE / "Hippel COSO/dragonflight (town).hipc"


def _wav(path: Path, seconds: float) -> Path:
    frames = int(seconds * 44100)
    path.write_bytes(stream._build_wav_header(44100, 2, frames, bits_per_sample=16)
                     + b"\0" * (frames * 4))
    return path


def _register(key: str, wav: Path, fmt: str = "uade") -> None:
    with conversion_cache._state_lock:
        conversion_cache._meta[key] = {"path": str(wav), "size_bytes": wav.stat().st_size,
                                       "format_type": fmt, "created_at": 0}
        conversion_cache._lru[key] = time.time()


def _unregister(*keys: str) -> None:
    with conversion_cache._state_lock:
        for k in keys:
            conversion_cache._meta.pop(k, None)
            conversion_cache._lru.pop(k, None)


# ── r2-ren-1: exact lengths up to an hour; render-status length fields ──────

def test_expected_seconds_are_capped_by_audio_length_not_the_render_timeout():
    t = types.SimpleNamespace
    assert stream._uade_expected_seconds(t(duration=512.0), 0) == 512.0   # uade's loop cap
    assert stream._uade_expected_seconds(t(duration=3600.0), 0) == 3600.0
    assert stream._uade_expected_seconds(t(duration=3601.0), 0) == 0.0
    assert stream._uade_expected_seconds(t(duration=512.0), 2) == 0.0


@pytest.mark.asyncio
async def test_a_512s_tune_streams_with_an_exact_length(monkeypatch, tmp_path):
    seen = {}

    async def fake_render(path, subsong=0, with_vu=True, *, live_key=None,
                          expected_seconds=0.0, subsong_base=0):
        seen["expected"] = expected_seconds
        return _wav(tmp_path / "r512.wav", 1.0)
    monkeypatch.setattr(stream, "_render_uade", fake_render)
    monkeypatch.setattr(stream, "_backfill_rendered_duration",
                        lambda *a, **k: asyncio.sleep(0))
    app, _ = _app(monkeypatch, tmp_path, {"l512": _T("l512", tmp_path / "x.dw", 512.0)})
    (tmp_path / "x.dw").write_bytes(b"x")
    async with _client(app) as c:
        r = await c.get("/api/stream/l512?u=a&p=b")
    assert r.status_code == 200 and "x-render-length" not in r.headers
    assert seen["expected"] == 512.0
    _unregister(stream._ck("l512", "uade", subsong=0))


@pytest.mark.asyncio
async def test_render_status_reports_provisional_and_the_exact_length(monkeypatch, tmp_path):
    app, _ = _app(monkeypatch, tmp_path, {"rsp": _T("rsp", "/x/a.dw", 0.0)})
    ev = asyncio.Event()
    stream._UADE_LIVE["rsp__sub0"] = {"complete": ev, "expected_size": 0,
                                      "bytes": stream._UADE_BYTES_PER_SEC * 2}
    try:
        async with _client(app) as c:
            j = (await c.get("/api/stream/rsp/render-status")).json()
            assert j["state"] == "ready_for_playback" and j["provisional"] is True
            assert "duration_seconds" not in j
            # tune 2 is not the one rendering
            j2 = (await c.get("/api/stream/rsp/render-status?subsong=2")).json()
            assert j2["provisional"] is False
            stream._UADE_LIVE["rsp__sub0"]["expected_size"] = 1000   # known length
            assert (await c.get("/api/stream/rsp/render-status")).json()["provisional"] is False
            ev.set()
            stream._UADE_LIVE.pop("rsp__sub0")
            wav = _wav(tmp_path / "rsp.wav", 123.4)
            _register("rsp__sub0", wav)
            j = (await c.get("/api/stream/rsp/render-status")).json()
            assert j["state"] == "complete" and j["provisional"] is False
            assert abs(j["duration_seconds"] - 123.4) < 1e-3
            assert "duration_seconds" not in (
                await c.get("/api/stream/rsp/render-status?subsong=1")).json()
    finally:
        stream._UADE_LIVE.pop("rsp__sub0", None)
        _unregister("rsp__sub0")


# ── r2-ren-2: Subsonic codec / bitrate requests answer at once ──────────────

@needs_ffmpeg
@pytest.mark.asyncio
async def test_cold_capped_transcode_streams_before_the_cache_encode_finishes(
        monkeypatch, tmp_path):
    flac = tmp_path / "src.flac"
    flac2 = tmp_path / "src2.flac"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-ac", "2", str(flac))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=6", "-ac", "2", str(flac2))
    tracks = {"tc1": _T("tc1", flac, 6.0, fmt="FLAC", bitrate=900000),
              "tc2": _T("tc2", flac2, 6.0, fmt="FLAC", bitrate=900000)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    real = stream._render_to_transcoded_flac
    gate = asyncio.Event()

    async def slow_encode(*a, **k):
        await gate.wait()                       # the cache encode is "slow"
        return await real(*a, **k)
    monkeypatch.setattr(stream, "_render_to_transcoded_flac", slow_encode)
    async with _client(app) as c:
        t0 = time.monotonic()
        r = await asyncio.wait_for(c.get("/api/stream/tc1?maxBitRate=128"), timeout=10)
        took = time.monotonic() - t0
        assert r.status_code == 200 and r.headers["x-cache"] == "miss"
        assert r.headers["accept-ranges"] == "none" and "content-length" not in r.headers
        assert r.headers["x-accel-buffering"] == "no" and "x-time-offset" not in r.headers
        assert r.headers["content-type"] == "audio/mpeg" and len(r.content) > 10_000
        assert took < 5
        fill = stream._TRANSCODE_FILLS.get(stream._ck("tc1", "transcoded", 0, codec="mp3",
                                                      bitrate=128))
        assert fill is not None and not fill.done()
        # a second cold request while the fill waits: live again, no second fill
        r = await c.get("/api/stream/tc1?maxBitRate=128")
        assert r.headers["x-cache"] == "miss" and len(stream._TRANSCODE_FILLS) == 1
        gate.set()
        await asyncio.wait_for(fill, timeout=30)
        warm = await c.get("/api/stream/tc1?maxBitRate=128", headers={"Range": "bytes=0-99"})
        assert warm.status_code == 206 and warm.headers["x-cache"] == "hit"
        assert len(warm.content) == 100
        # another track takes the cold path on its own
        r2 = await c.get("/api/stream/tc2?maxBitRate=128")
        assert r2.headers["x-cache"] == "miss" and len(r2.content) > 10_000
    await asyncio.gather(*list(stream._TRANSCODE_FILLS.values()), return_exceptions=True)


@needs_uade
@needs_ffmpeg
@pytest.mark.asyncio
async def test_rendered_track_in_another_codec_streams_while_the_cache_fills(
        monkeypatch, tmp_path):
    real_len, _ref = await _ref_render(DW)
    app, _ = _app(monkeypatch, tmp_path, {"rc1": _T("rc1", DW, round(real_len, 2))})
    real = stream._render_to_transcoded_flac
    gate = asyncio.Event()

    async def slow_encode(*a, **k):
        await gate.wait()
        return await real(*a, **k)
    monkeypatch.setattr(stream, "_render_to_transcoded_flac", slow_encode)
    async with _client(app) as c:
        await c.get("/api/stream/rc1?u=a&p=b")              # render + cache the WAV
        r = await asyncio.wait_for(c.get("/api/stream/rc1?u=a&p=b&format=mp3"), timeout=10)
        assert r.headers["x-transcode-cache"] == "miss"
        assert r.headers["content-type"] == "audio/mpeg" and len(r.content) > 1000
        wav_key = stream._ck("rc1", "uade", subsong=0)
        assert conversion_cache._pin_refs.get(wav_key, 0) >= 1    # held by the fill
        gate.set()
        await asyncio.gather(*list(stream._TRANSCODE_FILLS.values()))
        assert conversion_cache._pin_refs.get(wav_key, 0) == 0
        warm = await c.get("/api/stream/rc1?u=a&p=b&format=mp3")
        assert warm.headers["x-transcode-cache"] == "hit" and "content-length" in warm.headers


# ── r2-ren-3: the Amiga VU pass ─────────────────────────────────────────────

def _reference_parse(dump: Path, duration_s: float, vu_rate_hz: int = 30):
    """The whole-dump parser the chunked one replaced (same arithmetic)."""
    from itertools import compress
    from soniqboom.core import uade_vu as U
    body = dump.read_bytes()[U._HEADER:]
    total = len(body) // U._FRAME
    body = body[:total * U._FRAME]
    keep = body[0::U._FRAME].translate(U._KEEP)
    n_audio = keep.count(1)
    cols = [bytes(compress(body[4 + 2 * ch::U._FRAME], keep)).translate(U._ABS2)
            for ch in range(4)]
    peak = max(max(c) for c in cols)
    if peak < 255:
        lut = bytes(min(255, (v * 255) // peak) for v in range(256))
        cols = [c.translate(lut) for c in cols]
    n_win = max(1, int(duration_s * vu_rate_hz))
    step = n_audio / n_win
    mono = bytearray(n_win * 4)
    for k in range(n_win):
        s = int(k * step)
        e = max(s + 1, int((k + 1) * step))
        for ch in range(4):
            mono[k * 4 + ch] = max(cols[ch][s:e], default=0)
    return bytes(mono)


def _synthetic_dump(path: Path, frames: int, seed: int) -> Path:
    from soniqboom.core import uade_vu as U
    rng = random.Random(seed)
    out = bytearray(U.DUMP_MAGIC)
    for _ in range(frames):
        if rng.random() < 0.5:                                   # event frame
            out += bytes([0x80 | rng.randrange(128)]) + bytes(3) + bytes(rng.randrange(256) for _ in range(8))
        else:                                                    # audio frame
            out += bytes(4) + bytes(rng.randrange(256) for _ in range(8))
    out += b"\x01\x02\x03"                                       # a partial trailing frame
    path.write_bytes(bytes(out))
    return path


@pytest.mark.parametrize("chunk,frames,dur", [(7, 5000, 3.3), (64, 5000, 1.0),
                                              (1 << 16, 20000, 2.0), (5, 900, 60.0)])
def test_chunked_vu_parse_is_byte_identical_to_the_whole_dump_parse(
        monkeypatch, tmp_path, chunk, frames, dur):
    from soniqboom.core import uade_vu as U
    dump = _synthetic_dump(tmp_path / "d.bin", frames, seed=frames + chunk)
    monkeypatch.setattr(U, "_CHUNK_FRAMES", chunk)
    got = U.parse_dump(dump, dur)
    assert got is not None and got.frames == max(1, int(dur * 30))
    assert got.mono == _reference_parse(dump, dur)
    assert got.channels == 4 and got.pan == U._PAULA_PAN


def test_vu_parse_rejects_bad_input(tmp_path):
    from soniqboom.core import uade_vu as U
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"x" * 100)
    assert U.parse_dump(bad, 5.0) is None
    only_events = tmp_path / "ev.bin"
    only_events.write_bytes(U.DUMP_MAGIC + (b"\x80" + bytes(11)) * 50)
    assert U.parse_dump(only_events, 5.0) is None
    assert U.parse_dump(tmp_path / "missing.bin", 5.0) is None


@pytest.mark.asyncio
async def test_vu_parse_runs_after_the_render_slot_is_released(monkeypatch, tmp_path):
    wav = _wav(tmp_path / "vu.wav", 5.0)
    src = tmp_path / "t.dw"
    src.write_bytes(b"x")
    key = "vuslot__sub0"
    _register(key, wav)
    monkeypatch.setattr(stream, "_UADE_VU_START_DELAY", 0)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/bin/true")
    monkeypatch.setattr(stream, "_bg_render_sem", stream._PriorityGate(1))
    seen = {}
    dump = tmp_path / "d.bin"
    dump.write_bytes(b"x")

    async def fake_dump(binary, path, subsong, wav_path, *, base=0):
        seen["free_during_dump"] = stream._bg_render_sem._free
        return dump, 5.0

    async def fake_parse(d, duration, wav_path, name=""):
        seen["free_during_parse"] = stream._bg_render_sem._free
        d.unlink()
        return True
    monkeypatch.setattr(stream, "_uade_vu_dump", fake_dump)
    monkeypatch.setattr(stream, "_uade_vu_parse", fake_parse)
    stream._UADE_VU_LAST_POLL[key] = time.monotonic()
    try:
        stream._spawn_uade_vu(key, src, 0)
        for _ in range(100):
            if "free_during_parse" in seen:
                break
            await asyncio.sleep(0.01)
        assert seen == {"free_during_dump": 0, "free_during_parse": 1}
    finally:
        _unregister(key)
        stream._UADE_VU_LAST_POLL.pop(key, None)


@pytest.mark.asyncio
async def test_vu_pass_is_dropped_for_a_track_the_listener_left(monkeypatch, tmp_path):
    wav = _wav(tmp_path / "vu2.wav", 5.0)
    src = tmp_path / "t2.dw"
    src.write_bytes(b"x")
    key = "vuleft__sub0"
    _register(key, wav)
    monkeypatch.setattr(stream, "_UADE_VU_START_DELAY", 0.05)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/bin/true")
    ran = []

    async def fake_dump(*a, **k):
        ran.append(a)
        return None
    monkeypatch.setattr(stream, "_uade_vu_dump", fake_dump)
    stream._note_uade_vu_wanted(key, src, 0, None)
    try:
        # the last poll is stale: the pass is dropped, NOT marked done
        stream._UADE_VU_LAST_POLL[key] = time.monotonic() - 3600
        stream._spawn_uade_vu(key, src, 0)
        for _ in range(50):
            if key not in stream._UADE_VU_INFLIGHT:
                break
            await asyncio.sleep(0.02)
        assert ran == [] and key in stream._UADE_VU_WANTED
        # the listener is still there: a fresh poll runs it
        stream._UADE_VU_LAST_POLL[key] = time.monotonic()
        stream._spawn_uade_vu(key, src, 0)
        for _ in range(50):
            if ran:
                break
            await asyncio.sleep(0.02)
        assert len(ran) == 1
    finally:
        _unregister(key)
        stream._UADE_VU_LAST_POLL.pop(key, None)
        stream._UADE_VU_WANTED.pop(key, None)


@pytest.mark.asyncio
async def test_a_left_track_never_queues_for_a_busy_render_slot(monkeypatch, tmp_path):
    wav = _wav(tmp_path / "vu3.wav", 5.0)
    src = tmp_path / "t3.dw"
    src.write_bytes(b"x")
    key = "vubusy__sub0"
    _register(key, wav)
    monkeypatch.setattr(stream, "_UADE_VU_START_DELAY", 0.05)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "/bin/true")
    gate = stream._PriorityGate(1)
    monkeypatch.setattr(stream, "_bg_render_sem", gate)
    await gate.acquire()                          # every background slot is busy
    stream._UADE_VU_LAST_POLL[key] = time.monotonic() - 3600
    try:
        stream._spawn_uade_vu(key, src, 0)
        for _ in range(50):
            if key not in stream._UADE_VU_INFLIGHT:
                break
            await asyncio.sleep(0.02)
        assert key not in stream._UADE_VU_INFLIGHT and gate.waiting() == 0
    finally:
        gate.release()
        _unregister(key)
        stream._UADE_VU_LAST_POLL.pop(key, None)


def test_vu_request_waits_until_the_track_has_played_a_little(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(stream, "_spawn_uade_vu",
                        lambda key, src, ss, pin=None, **kw: spawned.append((key, kw)))
    monkeypatch.setattr(stream, "_uade_vu_enabled", lambda: True)
    key = stream._ck("vuw", "uade", subsong=3)
    stream._note_uade_vu_wanted(key, tmp_path / "x.hipc", 3, None,
                                cache_key=key + "__vb1", base=1)
    try:
        assert stream.request_uade_vu("vuw", 3) is True and spawned == []
        assert stream._uade_vu_still_wanted(key)
        # a re-serve (seek) keeps the first-served time
        first = stream._UADE_VU_WANTED[key][3]
        stream._note_uade_vu_wanted(key, tmp_path / "x.hipc", 3, None,
                                    cache_key=key + "__vb1", base=1)
        assert stream._UADE_VU_WANTED[key][3] == first
        ent = stream._UADE_VU_WANTED[key]
        stream._UADE_VU_WANTED[key] = ent[:3] + (ent[3] - 10,) + ent[4:]
        assert stream.request_uade_vu("vuw", 3) is True
        assert spawned == [(key + "__vb1", {"base": 1, "poll_key": key})]
    finally:
        stream._UADE_VU_WANTED.pop(key, None)
        stream._UADE_VU_LAST_POLL.pop(key, None)


# ── r2-ren-4: remote prewarm ────────────────────────────────────────────────

class _FakeRemoteCache:
    def __init__(self, local: Path, block: "threading.Event | None" = None):
        self.local, self.block = local, block
        self.fetches: list = []
        self.cached: set = set()

    def get_cached(self, share, rel):
        return self.local if (share, rel) in self.cached else None

    def fetch(self, share, rel, source, *, lane="stream"):
        self.fetches.append((rel, lane))
        if self.block is not None:
            self.block.wait(5)
        return self.local


def _remote_env(monkeypatch, tmp_path, cache):
    from soniqboom.core import filesource, remote_cache
    monkeypatch.setattr(filesource, "get_source", lambda root: object())
    monkeypatch.setattr(remote_cache, "get_cache", lambda: cache)
    monkeypatch.setattr(stream, "_REMOTE_PREWARM_GATES", {})


@pytest.mark.asyncio
async def test_remote_prewarm_fetches_on_the_scan_lane(monkeypatch, tmp_path):
    local = tmp_path / "t.flac"
    local.write_bytes(b"x")
    cache = _FakeRemoteCache(local)
    _remote_env(monkeypatch, tmp_path, cache)
    rendered = []

    async def fake_render(*a, **k):
        rendered.append(a)
    monkeypatch.setattr(stream, "_do_prewarm_render", fake_render)
    t = _T("rp1", "ftp://nas/music:/Albums/x/01.dsf", 300.0, fmt="DSF")
    await stream._do_prewarm("rp1", t, 0, stream.PRIO_NEXT)
    assert cache.fetches == [("/Albums/x/01.dsf", "scan")] and len(rendered) == 1
    # playback keeps the stream lane
    await stream._resolve_play_source("rp1", t)
    assert cache.fetches[-1] == ("/Albums/x/01.dsf", "stream")


@pytest.mark.asyncio
async def test_queued_remote_prewarm_waits_per_share_and_retain_cancels_it(
        monkeypatch, tmp_path):
    local = tmp_path / "t.dsf"
    local.write_bytes(b"x")
    block = threading.Event()
    cache = _FakeRemoteCache(local, block)
    _remote_env(monkeypatch, tmp_path, cache)
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: True)

    async def fake_render(*a, **k):
        return None
    monkeypatch.setattr(stream, "_do_prewarm_render", fake_render)
    tracks = {"ra": _T("ra", "ftp://nas/music:/A/a.dsf", 300.0, fmt="DSF"),
              "rb": _T("rb", "ftp://nas/music:/B/b.dsf", 300.0, fmt="DSF"),
              "rc": _T("rc", "ftp://nas/music:/C/c.dsf", 300.0, fmt="DSF")}

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(stream, "get_track", get_track)
    req = types.SimpleNamespace(client=None, cookies={})
    kw = dict(subsong=0, priority="next", file_path=None, pw="p1", sb_session="s",
              u=None, p=None, s=None, t=None, request=req)
    try:
        a = await stream.prewarm("ra", **kw)
        await asyncio.sleep(0.1)                   # ra is downloading (blocked)
        b = await stream.prewarm("rb", **kw)
        c = await stream.prewarm("rc", **kw)
        await asyncio.sleep(0.1)
        assert a["status"] == b["status"] == c["status"] == "queued"
        assert [f[0] for f in cache.fetches] == ["/A/a.dsf"]        # rb, rc wait their turn
        res = await stream.prewarm_retain({"ids": ["rc"]}, pw="p1", sb_session="s",
                                          u=None, p=None, s=None, t=None, request=req)
        assert res == {"cancelled": 2}                              # ra (running) + rb (queued)
        await asyncio.sleep(0.2)
        # ra's download can't be stopped: it keeps the share's turn until it ends
        assert [f[0] for f in cache.fetches] == ["/A/a.dsf"]
        block.set()
        await asyncio.sleep(0.3)
        assert [f[0] for f in cache.fetches] == ["/A/a.dsf", "/C/c.dsf"]   # rb never fetched
    finally:
        block.set()
        for k in list(stream._prewarm_tasks):
            if k.startswith(("ra::", "rb::", "rc::")):
                stream._prewarm_tasks.pop(k).cancel()
                stream._prewarm_owner.pop(k, None)


@pytest.mark.asyncio
async def test_remote_lookahead_beyond_next_is_skipped_unless_already_local(
        monkeypatch, tmp_path):
    cache = _FakeRemoteCache(tmp_path / "z.zip")
    _remote_env(monkeypatch, tmp_path, cache)
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: True)
    t = _T("rz", "ftp://nas/music:/Amiga/pack.zip::mdat.song", 0.0, fmt="TFMX")

    async def get_track(tid):
        return t
    monkeypatch.setattr(stream, "get_track", get_track)
    started = []

    async def fake_do(*a, **k):
        started.append(a)
    monkeypatch.setattr(stream, "_do_prewarm", fake_do)
    req = types.SimpleNamespace(client=None, cookies={})
    kw = dict(subsong=0, file_path=None, pw=None, sb_session="s", u=None, p=None,
              s=None, t=None, request=req)
    r = await stream.prewarm("rz", priority="ahead", **kw)
    assert r == {"status": "skipped", "reason": "remote lookahead"} and started == []
    cache.cached.add(("ftp://nas/music", "/Amiga/pack.zip"))        # outer zip is local
    r = await stream.prewarm("rz", priority="ahead", **kw)
    assert r["status"] == "queued"
    await asyncio.sleep(0)
    stream._prewarm_tasks.pop(r["key"], None)
    stream._prewarm_owner.pop(r["key"], None)


# ── r2-ren-5: duration probes honour "Prepare upcoming tracks" ──────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("prewarm_on", [False, True])
async def test_uade_probe_obeys_render_prewarm_and_sits_at_the_cold_end(
        monkeypatch, tmp_path, prewarm_on):
    from soniqboom.config import settings
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    tid = f"pr-{prewarm_on}"
    t = _T(tid, tmp_path / "tune.dw", 0.0)
    fs = FakeStore({tid: t})
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: prewarm_on)
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)

    async def get_track(x):
        return t if x == tid else None
    monkeypatch.setattr(stream, "get_track", get_track)

    async def resolve(x, path_str, **k):
        return Path(path_str)
    monkeypatch.setattr(stream, "_resolve_adlib_local_path", resolve)
    rendered = []

    async def render(path, subsong=0, with_vu=True, *, live_key=None, expected_seconds=0.0,
                     subsong_base=0):
        rendered.append(live_key)
        return _wav(tmp_path / f"p{len(rendered)}.wav", 7.0)
    monkeypatch.setattr(stream, "_render_uade", render)
    hot = _wav(tmp_path / "hot.wav", 1.0)
    _register("played__sub0", hot)
    n0, bytes0 = len(conversion_cache._meta), conversion_cache._total_bytes
    key = stream._ck(tid, "uade", subsong=0)
    try:
        out = await stream.probe_durations({"track_ids": [tid]}, None, None)
        if not prewarm_on:
            assert out == {} and rendered == []
            assert len(conversion_cache._meta) == n0
            assert conversion_cache._total_bytes == bytes0
        else:
            assert out == {tid: 7.0} and rendered == [key]
            assert next(iter(conversion_cache._lru)) == key          # the eviction end
            assert conversion_cache._pin_refs.get(key, 0) == 0
            await conversion_cache.get_cached(key)                   # a real play
            assert next(reversed(conversion_cache._lru)) == key
    finally:
        _unregister("played__sub0")
        conversion_cache._purge_entry(key)


# ── r2-ren-6: stream auth fast paths and token lockout ─────────────────────

@pytest.fixture()
def real_users(monkeypatch, tmp_path):
    from soniqboom.core import users as users_mod
    ustore = users_mod.UserStore(tmp_path)
    ustore.create(username="amy", password="loginpass1", role="admin")
    u = ustore.get_by_username("amy")
    u.subsonic_password = "app-secret"                 # a generated app password
    monkeypatch.setattr(users_mod, "get_user_store", lambda: ustore)
    return ustore


def test_login_password_is_checked_in_O1_once_verified(monkeypatch, real_users):
    from soniqboom.core import users as users_mod
    req = types.SimpleNamespace()
    _auth(req, None, "amy", "loginpass1")       # one scrypt
    calls = []
    real_verify = users_mod.verify_password
    monkeypatch.setattr(users_mod, "verify_password",
                        lambda *a: calls.append(a) or real_verify(*a))
    for _ in range(5):
        _auth(req, None, "amy", "loginpass1")
        _auth(req, None, "amy", "app-secret")
    assert calls == []
    with pytest.raises(stream.HTTPException):
        _auth(req, None, "amy", "wrong-pass")
    assert len(calls) == 1


def test_wrong_tokens_lock_the_token_scope_only(real_users):
    import hashlib
    req = types.SimpleNamespace()

    def tok(secret, salt="abc"):
        return hashlib.md5((secret + salt).encode()).hexdigest()
    _auth(req, None, "amy", None, "abc", tok("app-secret"))
    for i in range(20):
        with pytest.raises(stream.HTTPException):
            _auth(req, None, "amy", None, "abc", tok(f"bad{i}"))
        if real_users.is_locked("amy", "token"):
            break
    assert real_users.is_locked("amy", "token")
    with pytest.raises(stream.HTTPException) as e:
        _auth(req, None, "amy", None, "abc", tok("app-secret"))
    assert e.value.status_code == 401
    # the login password keeps working (its own scope)
    assert not real_users.is_locked("amy")
    _auth(req, None, "amy", "app-secret")
    # an unknown user's guesses count too
    with pytest.raises(stream.HTTPException):
        _auth(req, None, "nobody", None, "abc", tok("x"))


def test_right_token_for_a_disabled_account_is_refused_without_counting(real_users):
    import hashlib
    u = real_users.get_by_username("amy")
    u.enabled = False
    req = types.SimpleNamespace()
    t = hashlib.md5(("app-secret" + "s1").encode()).hexdigest()
    for _ in range(20):
        with pytest.raises(stream.HTTPException):
            _auth(req, None, "amy", None, "s1", t)
    assert not real_users.is_locked("amy", "token")


# ── r2-ren-7: no online lyrics lookups for chip tunes ──────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("fmt,online", [("SID", 0), ("David Whittaker", 0), ("NSF", 0),
                                        ("MIDI", 1), ("FLAC", 1), ("MP3", 1)])
async def test_online_lyrics_only_for_songs(monkeypatch, tmp_path, fmt, online):
    song = tmp_path / "f.bin"
    song.write_bytes(b"\0" * 10)
    t = types.SimpleNamespace(path=str(song), artist="A", album_artist="", title="T",
                              album="", duration=100.0, format=fmt)

    async def get_track(x):
        return t
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(tracks_api, "extract_lyrics", lambda p: None)
    monkeypatch.setattr(tracks_api, "_lyrics_miss", {})
    monkeypatch.setattr(tracks_api, "_lyrics_cache", {})
    calls = []

    async def online_fn(*a):
        calls.append(a)
        return None, True
    monkeypatch.setattr(tracks_api, "_resolve_online_lyrics", online_fn)
    res = await tracks_api.get_lyrics(f"ly-{fmt}")
    assert res["lyrics"] is None and len(calls) == online
    await tracks_api.get_lyrics(f"ly-{fmt}")
    assert len(calls) == online                   # the miss is remembered either way


@pytest.mark.asyncio
async def test_chip_tune_embedded_lyrics_still_served(monkeypatch, tmp_path):
    song = tmp_path / "k.sid"
    song.write_bytes(b"\0")
    t = types.SimpleNamespace(path=str(song), artist="A", album_artist="", title="T",
                              album="", duration=100.0, format="SID")

    async def get_track(x):
        return t
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(tracks_api, "extract_lyrics", lambda p: "la la")
    monkeypatch.setattr(tracks_api, "_lyrics_cache", {})
    monkeypatch.setattr(tracks_api, "_lyrics_miss", {})
    assert (await tracks_api.get_lyrics("ly-emb"))["lyrics"] == "la la"


# ── r2-ren-10: range answers for a progressive uade render ─────────────────

@needs_uade
@pytest.mark.asyncio
async def test_known_length_progressive_answers_byte_range_probes(monkeypatch, tmp_path):
    if not BD.exists():
        pytest.skip("long test module missing")
    seconds, ref_pcm = await _ref_render(BD)
    total = 44 + len(ref_pcm)
    app, _ = _app(monkeypatch, tmp_path, {"kp1": _T("kp1", BD, seconds),
                                          "kp2": _T("kp2", BD, seconds)})
    monkeypatch.setattr(stream, "_backfill_rendered_duration", lambda *a, **k: asyncio.sleep(0))
    async with _client(app) as c:
        r = await c.get("/api/stream/kp1", headers={"Range": "bytes=0-1"},
                        cookies={"sb_session": "x"})
        assert r.status_code == 206 and r.headers["x-cache"] == "miss-progressive"
        assert r.headers["content-range"] == f"bytes 0-1/{total}" and r.content == b"RI"
        r = await c.get("/api/stream/kp2", headers={"Range": "bytes=0-"},
                        cookies={"sb_session": "x"})
        assert r.status_code == 206 and r.headers["x-cache"] == "miss-progressive"
        assert r.headers["content-length"] == str(total)
        assert _pcm(r.content) == ref_pcm


@needs_uade
@pytest.mark.asyncio
async def test_safari_waits_for_an_unknown_length_render(monkeypatch, tmp_path):
    if not BD.exists():
        pytest.skip("long test module missing")
    app, _ = _app(monkeypatch, tmp_path, {"sf1": _T("sf1", BD, 0.0)})
    monkeypatch.setattr(stream, "_backfill_rendered_duration", lambda *a, **k: asyncio.sleep(0))
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
    async with _client(app) as c:
        r = await c.get("/api/stream/sf1", headers={"Range": "bytes=0-1", "User-Agent": ua},
                        cookies={"sb_session": "x"})
    assert r.status_code == 206 and r.headers["x-cache"] == "miss"
    assert "x-render-length" not in r.headers and r.content == b"RI"
    assert r.headers["content-range"].startswith("bytes 0-1/")
    # a non-Safari browser streams the same unknown-length render at once
    _unregister(stream._ck("sf1", "uade", subsong=0))
    async with _client(app) as c:
        r = await c.get("/api/stream/sf1", headers={"Range": "bytes=0-"},
                        cookies={"sb_session": "x"})
    assert r.headers["x-cache"] == "miss-progressive"
    assert r.headers["x-render-length"] == "unknown"


# ── r2-ren-11: per-subsong waveforms ────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.asyncio
async def test_waveform_for_another_subsong_reads_that_tunes_render(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from soniqboom.core import data as data_mod
    t = _T("wfs", str(tmp_path / "x.hipc"), 60.0, fmt="Hippel-COSO")
    wav = tmp_path / "sub2.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "2", str(wav))
    _register(stream._ck("wfs", "uade", subsong=2), wav)
    stored = []

    async def get_track(x):
        return t

    async def get_waveform(x):
        return [0.5] * 200                   # the default tune's stored waveform

    async def store_waveform(x, w):
        stored.append(x)
    monkeypatch.setattr(data_mod, "get_track", get_track)
    monkeypatch.setattr(data_mod, "get_waveform", get_waveform)
    monkeypatch.setattr(data_mod, "store_waveform", store_waveform)
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    try:
        async with _client(app) as c:
            t0 = time.monotonic()
            r = await c.get("/api/tracks/wfs/waveform?subsong=2")
            assert time.monotonic() - t0 < 3
            body = r.json()
            assert "pending" not in body and isinstance(body["waveform"], dict)
            assert stored == []                              # never stored for tune 2
            # no parameter → unchanged: the stored default-tune waveform
            assert (await c.get("/api/tracks/wfs/waveform")).json() == {"waveform": [0.5] * 200}
    finally:
        _unregister(stream._ck("wfs", "uade", subsong=2))


# ── r2-ren-12: status endpoints need a sign-in ─────────────────────────────

@pytest.mark.asyncio
async def test_status_endpoints_are_auth_gated(monkeypatch, tmp_path):
    from soniqboom.core import users as users_mod

    class _Users:
        def has_any(self):
            return True

        def lookup_session(self, tok):
            return types.SimpleNamespace(enabled=True) if tok == "good" else None

        def get_by_username(self, u):
            return None

        def authenticate(self, u, p):
            return None
    monkeypatch.setattr(users_mod, "get_user_store", lambda: _Users())
    app, _ = _app(monkeypatch, tmp_path, {"st1": _T("st1", "/x/a.dw", 0.0)}, auth=True)
    async with _client(app) as c:
        for ep in ("render-status", "transcode-status"):
            assert (await c.get(f"/api/stream/st1/{ep}")).status_code == 401
            assert (await c.get(f"/api/stream/nope/{ep}")).status_code == 401
            ok = await c.get(f"/api/stream/st1/{ep}", cookies={"sb_session": "good"})
            assert ok.status_code == 200


# ── r2-ren-13: X-VU-Unavailable ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vu_endpoint_says_when_amiga_meters_cannot_come(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from soniqboom.core import data as data_mod
    tr = {"am": _T("am", "/x/carrier command.dw", 4.0),
          "tk": _T("tk", "/x/song.mod", 60.0, fmt="MOD")}
    cfg = {"uade_vu_meters": False}
    fs = FakeStore(tr, config=cfg)
    monkeypatch.setattr(data_mod, "get_store", lambda: fs)

    async def get_track(x):
        return tr.get(x)
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(conversion_cache, "_find_orphan_sidecar", lambda *a: None)

    async def no_backfill(*a, **k):
        return None
    monkeypatch.setattr(tracks_api, "_try_backfill_vu_sidecar", no_backfill)
    monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED", False)
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    async with _client(app) as c:
        r = await c.get("/api/tracks/am/vu")
        assert r.status_code == 404 and r.headers["x-vu-unavailable"] == "off"
        r = await c.get("/api/tracks/tk/vu")
        assert r.status_code == 404 and "x-vu-unavailable" not in r.headers
        cfg["uade_vu_meters"] = True
        r = await c.get("/api/tracks/am/vu")
        assert r.status_code == 404 and "x-vu-unavailable" not in r.headers
        monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED", True)
        monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED_AT", time.monotonic())
        r = await c.get("/api/tracks/am/vu")
        assert r.headers["x-vu-unavailable"] == "unsupported"


# ── r2-ren-14: uade subsong bases ───────────────────────────────────────────

def test_subsong_index_maps_onto_the_modules_numbering():
    assert stream._uade_subsong_arg(0, 1) == []
    assert stream._uade_subsong_arg(1, 1) == ["--subsong=2"]
    assert stream._uade_subsong_arg(2, 1) == ["--subsong=3"]
    assert stream._uade_subsong_arg(3, 0) == ["--subsong=3"]
    # keys change only for tune N > 0 of a module numbered from 1
    assert stream.uade_cache_key("x", 0, 1) == "x__sub0"
    assert stream.uade_cache_key("x", 2, 0) == "x__sub2"
    assert stream.uade_cache_key("x", 2, 1) == "x__sub2__vb1"
    assert conversion_cache.key_matches("x__sub2__vb1", "x", 2)
    assert not conversion_cache.key_matches("x__sub2__vb1", "x", 1)


@needs_uade
@pytest.mark.asyncio
async def test_base_probe_and_rendered_tunes(monkeypatch):
    if not HIPC.exists():
        pytest.skip("Hippel COSO fixture missing")
    assert await stream._probe_uade_base(HIPC) == 1
    assert await stream._probe_uade_base(CF) == 0
    monkeypatch.setattr(stream, "_UADE_BASE_MEMO", stream.OrderedDict())
    t = types.SimpleNamespace(subsong_base=None)

    async def no_track(x):
        return None
    monkeypatch.setattr(stream, "get_track", no_track)
    assert await stream._uade_resolve_base("h1", t, HIPC, 0) == 0          # no probe
    assert "h1" not in stream._UADE_BASE_MEMO
    assert await stream._uade_resolve_base("h1", t, HIPC, 2) == 1
    assert stream._UADE_BASE_MEMO["h1"] == 1
    assert stream._uade_base_known("h9", types.SimpleNamespace(subsong_base=1)) == 1
    lens = []
    for idx in (0, 1, 2):
        out = await stream._render_uade(HIPC, subsong=idx, subsong_base=1)
        lens.append((out.stat().st_size - 44) / stream._UADE_BYTES_PER_SEC)
        out.unlink()
    # index 2 is the last tune (163.9 s), never reachable before; 0 != 1 now
    assert abs(lens[2] - 163.88) < 1.0
    assert abs(lens[0] - lens[1]) > 1.0


@pytest.mark.asyncio
async def test_serve_uses_the_base_in_the_key_and_the_render(monkeypatch, tmp_path):
    seen = {}

    async def fake_render(path, subsong=0, with_vu=True, *, live_key=None,
                          expected_seconds=0.0, subsong_base=0):
        seen.update(key=live_key, base=subsong_base)
        return _wav(tmp_path / "b.wav", 1.0)
    monkeypatch.setattr(stream, "_render_uade", fake_render)
    monkeypatch.setattr(stream, "_backfill_rendered_duration", lambda *a, **k: asyncio.sleep(0))
    src = tmp_path / "x.hipc"
    src.write_bytes(b"x")
    t = _T("sb1", src, 0.0, fmt="Hippel-COSO", subsong_base=1)
    app, _ = _app(monkeypatch, tmp_path, {"sb1": t})
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/sb1?subsong=2&u=a&p=b")
        assert r.status_code == 200
        assert seen == {"key": "sb1__sub2__vb1", "base": 1}
        assert "sb1__sub2__vb1" in conversion_cache._meta
        assert stream._UADE_VU_WANTED["sb1__sub2"][4] == "sb1__sub2__vb1"
    finally:
        _unregister("sb1__sub2__vb1")
        stream._UADE_VU_WANTED.pop("sb1__sub2", None)


# ── r2-ren-15: per-page prewarm owners ──────────────────────────────────────

@pytest.mark.asyncio
async def test_retain_from_one_tab_keeps_the_other_tabs_prewarms(monkeypatch):
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    req = types.SimpleNamespace(client=None, cookies={})
    a = stream._prewarm_requester(req, "sess", None, "tabA")
    b = stream._prewarm_requester(req, "sess", None, "tabB")
    assert a != b and stream._prewarm_requester(req, "sess", None) not in (a, b)

    async def forever():
        await asyncio.sleep(3600)
    tasks = {}
    for tid, owner in (("pa", a), ("pb", b)):
        k = stream._prewarm_key(tid, ".dw", 0)
        tasks[tid] = asyncio.ensure_future(forever())
        stream._prewarm_tasks[k] = tasks[tid]
        stream._prewarm_owner[k] = {owner}
    try:
        kw = dict(sb_session="sess", u=None, p=None, s=None, t=None, request=req)
        assert await stream.prewarm_retain({"ids": []}, pw="tabA", **kw) == {"cancelled": 1}
        await asyncio.sleep(0)
        assert tasks["pa"].cancelled() and not tasks["pb"].done()
        assert await stream.prewarm_retain({"ids": []}, pw="tabB", **kw) == {"cancelled": 1}
    finally:
        for tid, tk in tasks.items():
            tk.cancel()
            k = stream._prewarm_key(tid, ".dw", 0)
            stream._prewarm_tasks.pop(k, None)
            stream._prewarm_owner.pop(k, None)
