# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stream / render fixes from the review loop (issue #15 follow-ups, Subsonic
delivery): subsong-exact uade lengths, unknown-length progressive play, short
tunes, render-status per subsong, VU on demand, codec / bitrate / time-offset
delivery, HEAD, proxy headers, prewarm owner tags and auth fast paths.

Everything runs in-process (httpx ASGITransport) against a throw-away
conversion cache and a fake store — never the live server or real data.
"""
from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import types
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.core import conversion_cache


async def _noauth(*a, **k):
    """Stand-in for the (async) stream sign-in check."""
    return None


def _auth(*a):
    """Run the async stream sign-in check to completion (sync tests)."""
    return asyncio.run(stream._require_stream_auth(*a))


REPO = Path(__file__).resolve().parent.parent
UADE = REPO / "internal/testdata/uade"
DW = UADE / "David Whittaker/carrier command.dw"          # 4.36 s
BD = UADE / "Ben Daglish/mickey mouse.bd"                  # ~2 min
CF = UADE / "Richard Joseph/cannon fodder 2-morbase.sng"   # multi-subsong

needs_uade = pytest.mark.skipif(
    not shutil.which("uade123") or not DW.exists(),
    reason="uade123 or the local Amiga test modules are not available")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


# ── helpers ─────────────────────────────────────────────────────────────────

class _T:
    """A minimal track record (attribute access + __dict__, like Track)."""

    def __init__(self, tid, path, duration=0.0, fmt="David Whittaker", **kw):
        self.id, self.path, self.duration = tid, str(path), duration
        self.format, self.genre, self.title = fmt, ["Amiga", "Module"], "t"
        self.bitrate = kw.pop("bitrate", None)
        self.scan_root_hash = kw.pop("scan_root_hash", "")
        for k, v in kw.items():
            setattr(self, k, v)


class FakeStore:
    def __init__(self, tracks=None, scan_dirs=None, config=None):
        self.tracks = tracks or {}
        self.scan_dirs = scan_dirs or []
        self.config = config or {}
        self.updates: list = []
        self.batches: list = []

    def update_track_fields(self, tid, fields):
        self.updates.append((tid, dict(fields)))
        t = self.tracks.get(tid)
        for k, v in fields.items():
            if t is not None:
                setattr(t, k, v)
        return True

    def update_track_fields_batch(self, items):
        self.batches.append(list(items))
        for tid, fields in items:
            t = self.tracks.get(tid)
            for k, v in fields.items():
                if t is not None:
                    setattr(t, k, v)
        return len(items)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def list_scan_dirs(self):
        return [dict(d) for d in self.scan_dirs]


def _pcm(wav: bytes) -> bytes:
    i = wav.find(b"data")
    size = int.from_bytes(wav[i + 4:i + 8], "little")
    return wav[i + 8:i + 8 + size]


def _app(monkeypatch, tmp_path, tracks: dict, *, store: FakeStore | None = None,
         auth: bool = False):
    from fastapi import FastAPI
    from soniqboom.config import settings
    from soniqboom.core import data as data_mod
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_zip_extract_dir",
                        lambda: (tmp_path / "zx").mkdir(exist_ok=True) or tmp_path / "zx")
    if not auth:
        monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    fs = store or FakeStore(tracks)
    fs.tracks.update(tracks)
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    monkeypatch.setattr(data_mod, "get_store", lambda: fs)

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(stream, "get_track", get_track)
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    return app, fs


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _ref_render(src: Path, subsong: int = 0) -> tuple[float, bytes]:
    out = await stream._render_uade(src, subsong=subsong)
    try:
        data = out.read_bytes()
    finally:
        out.unlink(missing_ok=True)
    return (len(data) - 44) / stream._UADE_BYTES_PER_SEC, _pcm(data)


# ── r1-ren-1: a stored length describes the DEFAULT tune only ───────────────

def test_expected_seconds_only_for_the_default_tune():
    t = types.SimpleNamespace(duration=24.62)
    assert stream._uade_expected_seconds(t, 0) == 24.62
    assert stream._uade_expected_seconds(t, 5) == 0.0
    assert stream._uade_expected_seconds(types.SimpleNamespace(duration=0), 0) == 0.0


@pytest.mark.asyncio
async def test_backfill_skips_other_subsongs_and_uade_corrects_stale_values(
        monkeypatch, tmp_path):
    from soniqboom.core import store as store_mod
    fs = FakeStore()
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    wav = tmp_path / "w.wav"
    wav.write_bytes(stream._build_wav_header(44100, 2, 44100 * 10, 16)
                    + b"\0" * (44100 * 4 * 10))
    t = _T("bf1", wav, duration=24.62)
    # a non-default subsong never writes the track's duration
    assert await stream._backfill_rendered_duration(
        "bf1", t, wav, 0.0, subsong=5, authoritative=True) is None
    assert fs.updates == []
    # non-authoritative: a real stored value wins
    assert await stream._backfill_rendered_duration("bf1", t, wav, 0.0) == 24.62
    assert fs.updates == []
    # uade (authoritative): the render corrects a wrong stored value
    assert await stream._backfill_rendered_duration(
        "bf1", t, wav, 0.0, authoritative=True) == 10.0
    assert fs.updates == [("bf1", {"duration": 10.0})]
    # sink: collected, not written
    sink: list = []
    t2 = _T("bf2", wav, duration=0)
    await stream._backfill_rendered_duration("bf2", t2, wav, 0.0, sink=sink)
    assert sink == [("bf2", {"duration": 10.0})] and len(fs.updates) == 1


@needs_uade
@pytest.mark.asyncio
async def test_other_subsong_streams_without_a_foreign_length(monkeypatch, tmp_path):
    """Stored = subsong 2's length; playing subsong 5 must not be padded to it,
    and must not rewrite the track's duration."""
    real5, ref5 = await _ref_render(CF, 5)
    t = _T("cf1", CF, duration=24.62, fmt="Richard Joseph")
    app, fs = _app(monkeypatch, tmp_path, {"cf1": t})
    async with _client(app) as c:
        r = await c.get("/api/stream/cf1?subsong=5", cookies={"sb_session": "x"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    body_pcm = r.content[44:]
    # the header never claims a length the body doesn't have (unknown → 0xFFFFFFFF)
    assert int.from_bytes(r.content[40:44], "little") in (0xFFFFFFFF, len(body_pcm))
    trailing_zero = len(body_pcm) - len(body_pcm.rstrip(b"\0"))
    assert trailing_zero <= stream._UADE_BYTES_PER_SEC      # ≤ 1 s, not 11.8 s
    assert body_pcm[:len(ref5)] == ref5
    assert abs(len(body_pcm) - len(ref5)) <= stream._UADE_BYTES_PER_SEC
    assert t.duration == 24.62 and fs.updates == []
    await asyncio.sleep(0.3)          # let the post-render bookkeeping run
    assert fs.updates == []


@needs_uade
@pytest.mark.asyncio
async def test_live_render_lands_exactly_on_the_promised_length():
    real, ref = await _ref_render(DW)
    promised = round(real + 0.3, 2)                  # a slightly-off stored length
    key = "exact-len__sub0"
    out = await stream._render_uade(DW, live_key=key, expected_seconds=promised)
    try:
        live = stream._UADE_LIVE.get(key)
        data = out.read_bytes()
        assert len(data) == 44 + int(round(promised * 44100)) * 4
        assert int.from_bytes(data[40:44], "little") == len(data) - 44
        assert _pcm(data)[:len(ref)] == ref           # audio untouched, silence appended
        assert live is None or live["bytes"] == len(data) - 44
    finally:
        out.unlink(missing_ok=True)
        stream._UADE_LIVE.pop(key, None)


@pytest.mark.asyncio
async def test_chunked_body_never_pads_seconds_of_silence_for_a_wrong_length(tmp_path):
    f = tmp_path / "g.wav"
    f.write_bytes(stream._build_wav_header(44100, 2, 44100 * 10, 16) + b"\1" * 44100 * 4)
    done = asyncio.Event()
    done.set()
    live = {"no_pad_on_failure": True, "clean_exit": True}
    req = types.SimpleNamespace(headers={})
    resp = await stream._chunked_growing_file_response(
        req, f, 44 + 44100 * 4 * 10, done, media_type="audio/wav", inflight=live)
    body = b"".join([c async for c in resp.body_iterator])
    assert len(body) == 44 + 44100 * 4                  # not padded by 9 s
    assert resp.headers["x-accel-buffering"] == "no"
    # a small (rounding) gap is still padded for an ffmpeg pump entry
    resp = await stream._chunked_growing_file_response(
        req, f, 44 + 44100 * 4 + 1000, done, media_type="audio/wav", inflight={})
    body = b"".join([c async for c in resp.body_iterator])
    assert len(body) == 44 + 44100 * 4 + 1000


# ── r1-ren-2: unknown length plays progressively for the web UI ────────────

@needs_uade
@pytest.mark.asyncio
async def test_unknown_length_first_play_is_progressive_with_a_streaming_header(
        monkeypatch, tmp_path):
    if not BD.exists():
        pytest.skip("long test module missing")
    _real, ref = await _ref_render(BD)
    app, fs = _app(monkeypatch, tmp_path, {"unk1": _T("unk1", BD, 0.0, fmt="Ben Daglish")})
    async with _client(app) as c:
        r = await c.get("/api/stream/unk1", cookies={"sb_session": "x"})
        assert r.status_code == 200
        assert r.headers["x-cache"] == "miss-progressive"
        assert r.headers.get("x-render-length") == "unknown"
        assert "content-length" not in r.headers
        assert r.content[:4] == b"RIFF"
        assert r.content[4:8] == b"\xff\xff\xff\xff" and r.content[40:44] == b"\xff\xff\xff\xff"
        assert r.content[44:] == ref                     # every sample, no padding
        await asyncio.sleep(0.2)
        r2 = await c.get("/api/stream/unk1", headers={"Range": "bytes=0-99"})
        assert r2.status_code == 206 and r2.headers["x-cache"] == "hit"
        assert r2.headers["content-range"] == f"bytes 0-99/{44 + len(ref)}"
    # the real length was learnt from the render
    assert fs.updates and fs.updates[-1] == ("unk1", {"duration": round(len(ref) / 176400, 2)})


@needs_uade
@pytest.mark.asyncio
async def test_unknown_length_range_request_waits_and_subsonic_gets_a_length(
        monkeypatch, tmp_path):
    if not BD.exists():
        pytest.skip("long test module missing")
    from soniqboom.core.conversion_cache import get_or_render
    t = _T("unk2", BD, 0.0, fmt="Ben Daglish")
    app, _fs = _app(monkeypatch, tmp_path, {"unk2": t})
    key = stream._ck("unk2", "uade", subsong=0)
    # a render already in flight (e.g. a prewarm), length unknown
    task = asyncio.ensure_future(get_or_render(
        track_id="unk2", format_type="uade", subsong=0,
        render_fn=lambda: stream._render_uade(BD, live_key=key)))
    for _ in range(100):
        if stream._UADE_LIVE.get(key) is not None:
            break
        await asyncio.sleep(0.01)
    async with _client(app) as c:
        r = await c.get("/api/stream/unk2", cookies={"sb_session": "x"},
                        headers={"Range": "bytes=1000-"})
        assert r.status_code == 206
        total = int(r.headers["content-range"].rsplit("/", 1)[1])
        assert total > 44 and r.headers["x-cache"] in ("hit", "miss")
        assert len(r.content) == total - 1000
        r3 = await c.get("/api/stream/unk2?u=a&p=b")
        assert "content-length" in r3.headers and r3.headers["x-cache"] == "hit"
    await task


def test_render_state_reports_unknown_length_live_renders_as_playable():
    ev = asyncio.Event()
    live = {"complete": ev, "expected_size": 0, "bytes": stream._UADE_BYTES_PER_SEC,
            "audible": True}
    stream._UADE_LIVE["rsu__sub0"] = live
    try:
        assert stream._render_state("rsu", 0) == "ready_for_playback"
        live["bytes"] = 10
        assert stream._render_state("rsu", 0) == "rendering"
        # Nothing audible yet: no listener would be attached — still rendering.
        live["bytes"], live["audible"] = stream._UADE_BYTES_PER_SEC * 5, False
        assert stream._render_state("rsu", 0) == "rendering"
    finally:
        stream._UADE_LIVE.pop("rsu__sub0", None)


# ── r1-ren-5: short tunes (render done before the response) ─────────────────

@needs_uade
@pytest.mark.asyncio
@pytest.mark.parametrize("rng", [None, "bytes=0-", "bytes=100-"])
async def test_short_known_length_tunes_never_answer_empty(monkeypatch, tmp_path, rng):
    real, ref = await _ref_render(DW)
    tid = f"short-{(rng or 'none').replace('=', '').replace('-', '_')}"
    app, _fs = _app(monkeypatch, tmp_path, {tid: _T(tid, DW, round(real, 2))})
    hdrs = {"Range": rng} if rng else {}
    async with _client(app) as c:
        r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"}, headers=hdrs)
    assert r.content, "empty body"
    if rng == "bytes=100-":
        assert r.status_code == 206
        total = int(r.headers["content-range"].rsplit("/", 1)[1])
        assert len(r.content) == total - 100
    else:
        assert r.content[:4] == b"RIFF"
        assert _pcm(r.content)[:len(ref)] == ref


@pytest.mark.asyncio
async def test_growing_responders_read_through_a_handed_over_descriptor(tmp_path):
    """The descriptor opened before the response keeps the data readable after
    the file is moved away (store_cached) — the empty-200 window."""
    import os
    f = tmp_path / "grow.wav"
    payload = stream._build_wav_header(44100, 2, 1000, 16) + b"\2" * 4000
    f.write_bytes(payload)
    done = asyncio.Event()
    done.set()
    req = types.SimpleNamespace(headers={})
    h = stream._FdHandle(os.open(str(f), os.O_RDONLY))
    resp = await stream._chunked_growing_file_response(
        req, f, len(payload), done, media_type="audio/wav", fd=h)
    f.rename(tmp_path / "moved.wav")
    body = b"".join([c async for c in resp.body_iterator])
    assert body == payload and h.fd == -1                   # read, then closed
    g = tmp_path / "grow2.wav"
    g.write_bytes(payload)
    h2 = stream._FdHandle(os.open(str(g), os.O_RDONLY))
    req2 = types.SimpleNamespace(headers={"range": "bytes=10-"})
    resp2 = await stream._growing_file_range_response(
        req2, g, len(payload), done, media_type="audio/wav", fd=h2)
    g.unlink()
    body2 = b"".join([c async for c in resp2.body_iterator])
    assert body2 == payload[10:] and resp2.status_code == 206
    assert resp2.headers["x-accel-buffering"] == "no"


# ── r1-ren-9 / r1-ren-22: render state per subsong, in-flight transcodes ─────

def test_render_state_matches_the_requested_subsong_only():
    with conversion_cache._state_lock:
        conversion_cache._meta["rsx__sub0"] = {"path": "/n.wav", "size_bytes": 0,
                                               "format_type": "uade", "created_at": 0}
        conversion_cache._meta["rsy__cflac__ar0"] = {"path": "/n.wav", "size_bytes": 0,
                                                     "format_type": "transcoded",
                                                     "created_at": 0}
    try:
        assert stream._render_state("rsx", 0) == "complete"
        assert stream._render_state("rsx", 3) == "idle"
        assert stream._render_state("rsx", 1) == "idle"
        assert stream._render_state("rsy", 0) == "complete"    # subsong-agnostic key
        assert stream._render_state("rsy", 4) == "complete"
        assert conversion_cache.key_matches("a__sub1__dur60", "a", 1)
        assert not conversion_cache.key_matches("a__sub10", "a", 1)
        assert not conversion_cache.key_matches("ab__sub1", "a", 1)
    finally:
        with conversion_cache._state_lock:
            conversion_cache._meta.pop("rsx__sub0", None)
            conversion_cache._meta.pop("rsy__cflac__ar0", None)


@pytest.mark.asyncio
async def test_render_state_covers_in_flight_wav_transcodes():
    ev = asyncio.Event()
    stream._INFLIGHT_TRANSCODES["tx1"] = {"setup_ready": ev}
    pump = asyncio.ensure_future(asyncio.sleep(30))
    prewarm = asyncio.ensure_future(asyncio.sleep(30))
    try:
        assert stream._render_state("tx1") == "rendering"
        ev.set()
        stream._INFLIGHT_TRANSCODES["tx1"]["pump_task"] = pump
        assert stream._render_state("tx1") == "ready_for_playback"
        stream._prewarm_tasks[stream._prewarm_key("tx1", ".dsf", 0)] = prewarm
        assert stream._render_state("tx1") == "ready_for_playback"   # not "queued"
    finally:
        pump.cancel()
        prewarm.cancel()
        stream._INFLIGHT_TRANSCODES.pop("tx1", None)
        stream._prewarm_tasks.pop(stream._prewarm_key("tx1", ".dsf", 0), None)


@pytest.mark.asyncio
async def test_render_status_endpoint_passes_the_subsong(monkeypatch, tmp_path):
    app, _ = _app(monkeypatch, tmp_path, {"rsz": _T("rsz", "/x/a.dw", 0.0)})
    with conversion_cache._state_lock:
        conversion_cache._meta["rsz__sub2"] = {"path": "/n.wav", "size_bytes": 0,
                                               "format_type": "uade", "created_at": 0}
    try:
        async with _client(app) as c:
            a = (await c.get("/api/stream/rsz/render-status?subsong=2")).json()["state"]
            b = (await c.get("/api/stream/rsz/render-status")).json()["state"]
        assert (a, b) == ("complete", "idle")
    finally:
        with conversion_cache._state_lock:
            conversion_cache._meta.pop("rsz__sub2", None)


# ── r1-ren-8 / r1-ren-12: VU passes ─────────────────────────────────────────

class _Proc:
    def __init__(self, rc, err=b""):
        self.returncode, self._err = rc, err

    async def communicate(self):
        return b"", self._err

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


@pytest.mark.asyncio
async def test_vu_pass_latches_only_on_a_capability_failure(monkeypatch, tmp_path):
    wav = tmp_path / "x.wav"
    wav.write_bytes(stream._build_wav_header(44100, 2, 44100 * 5, 16) + b"\0" * 44100 * 20)
    src = tmp_path / "tune.mod"
    src.write_bytes(b"M" * 100)
    replies = []

    async def fake_exec(*a, **k):
        return replies.pop(0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    replies.append(_Proc(1, b"uadecore: score died"))
    assert await stream._uade_vu_pass("uade123", src, 0, wav) is None
    replies.append(_Proc(1, b"Can not set write audio fname"))
    assert await stream._uade_vu_pass("uade123", src, 0, wav) is False
    replies.append(_Proc(1, b"uade123: unrecognized option `--write-audio=/x'"))
    assert await stream._uade_vu_pass("uade123", src, 0, wav) is False
    # a source that vanished is a per-tune failure, whatever uade printed
    assert await stream._uade_vu_pass("uade123", tmp_path / "gone.mod", 0, wav) is None


@pytest.mark.asyncio
async def test_vu_latch_expires_and_can_be_reset(monkeypatch):
    monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED", True)
    monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED_AT", __import__("time").monotonic())
    assert stream._uade_vu_enabled() is False
    stream.reset_uade_vu_latch()
    assert stream._UADE_VU_UNSUPPORTED is False
    monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED", True)
    monkeypatch.setattr(stream, "_UADE_VU_UNSUPPORTED_AT", -1e9)
    stream._uade_vu_enabled()
    assert stream._UADE_VU_UNSUPPORTED is False


@needs_uade
@pytest.mark.asyncio
async def test_vu_pass_starts_only_when_the_meter_asks(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from soniqboom.api import tracks as tracks_api
    spawned = []
    monkeypatch.setattr(stream, "_spawn_uade_vu",
                        lambda key, src, ss, pin=None, **kw: spawned.append((key, ss)))
    t = _T("vu1", DW, 0.0)
    app, _fs = _app(monkeypatch, tmp_path, {"vu1": t})
    async with _client(app) as c:
        r = await c.get("/api/stream/vu1?u=a&p=b")          # a Subsonic-style play
        assert r.status_code == 200
    await asyncio.sleep(0.2)
    assert spawned == []                                   # nothing spawned on play

    async def get_track(tid):
        return t if tid == "vu1" else None
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(conversion_cache, "_find_orphan_sidecar", lambda *a: None)
    tapp = FastAPI()
    tapp.include_router(tracks_api.router, prefix="/api")
    key = stream._ck("vu1", "uade", subsong=0)
    async with _client(tapp) as c:
        r = await c.get("/api/tracks/vu1/vu")
        # within the first seconds of a play the pass is left for a later ask
        assert r.status_code == 404 and spawned == []
        ent = stream._UADE_VU_WANTED[key]
        stream._UADE_VU_WANTED[key] = ent[:3] + (ent[3] - 60,) + ent[4:]
        r = await c.get("/api/tracks/vu1/vu?start=0")       # cache-only ask
        assert r.status_code == 404 and spawned == []
        r = await c.get("/api/tracks/vu1/vu")
    assert r.status_code == 404
    assert spawned == [(key, 0)]
    assert stream.request_uade_vu("never-played", 0) is False


# ── r1-ren-10 / r1-ren-11: codec, bitrate cap, time offset ──────────────────

def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_bitrate_cap_and_format_are_honoured_for_direct_files(monkeypatch, tmp_path):
    mp3 = tmp_path / "src.mp3"
    flac = tmp_path / "src.flac"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-ac", "2",
            "-b:a", "320k", str(mp3))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-ac", "2", str(flac))
    tracks = {"b-mp3": _T("b-mp3", mp3, 6.0, fmt="MP3", bitrate=320000),
              "b-flac": _T("b-flac", flac, 6.0, fmt="FLAC", bitrate=900000)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    async with _client(app) as c:
        plain = await c.get("/api/stream/b-mp3")
        assert plain.content == mp3.read_bytes() and "x-transcoded" not in plain.headers
        raw = await c.get("/api/stream/b-mp3?format=raw&maxBitRate=64")
        assert raw.content == mp3.read_bytes()
        capped = await c.get("/api/stream/b-mp3?maxBitRate=128")
        assert capped.headers["x-transcoded"] == "1"
        assert capped.headers["content-type"] == "audio/mpeg"
        assert 0 < len(capped.content) < len(mp3.read_bytes()) * 0.6
        f128 = await c.get("/api/stream/b-flac?maxBitRate=128")
        assert f128.headers["content-type"] == "audio/mpeg"
        assert f128.headers["x-target-codec"] == "mp3"
        hi = await c.get("/api/stream/b-mp3?format=ogg&maxBitRate=160")
        assert hi.headers["content-type"] == "audio/ogg" and hi.content[:4] == b"OggS"
    # cold requests stream a live encode; the cache fills in the background
    await asyncio.gather(*list(stream._TRANSCODE_FILLS.values()), return_exceptions=True)
    keys = [k for k in conversion_cache._meta if k.startswith("b-mp3__")]
    assert any("br128" in k for k in keys) and any("br160" in k for k in keys)


@needs_uade
@needs_ffmpeg
@pytest.mark.asyncio
async def test_rendered_tracks_honour_format_cap_and_time_offset(monkeypatch, tmp_path):
    real, ref = await _ref_render(DW)
    t = _T("rx1", DW, round(real, 2))
    app, _ = _app(monkeypatch, tmp_path, {"rx1": t})
    async with _client(app) as c:
        wav = await c.get("/api/stream/rx1?u=a&p=b")
        assert wav.headers["content-type"] == "audio/wav"
        mp3 = await c.get("/api/stream/rx1?u=a&p=b&format=mp3")
        assert mp3.headers["content-type"] == "audio/mpeg"
        assert mp3.headers["x-transcoded"] == "1" and len(mp3.content) < len(wav.content) / 4
        cap = await c.get("/api/stream/rx1?u=a&p=b&maxBitRate=96")
        assert cap.headers["content-type"] == "audio/mpeg"
        # time offset, no transcode: a WAV whose header covers the rest exactly
        off = await c.get("/api/stream/rx1?u=a&p=b&seek=2")
        assert off.status_code == 200 and off.headers["x-time-offset"] == "2.000"
        skip = 2 * 44100 * 4
        # the render was aligned to the stored (10 ms-rounded) length
        cached_pcm = ref.ljust(int(round(t.duration * 44100)) * 4, b"\0")
        assert _pcm(wav.content) == cached_pcm
        assert int.from_bytes(off.content[40:44], "little") == len(cached_pcm) - skip
        assert off.content[44:] == cached_pcm[skip:]
        rng = await c.get("/api/stream/rx1?u=a&p=b&seek=2", headers={"Range": "bytes=40-99"})
        assert rng.status_code == 206 and rng.content == off.content[40:100]
        # time offset + codec: a live pipe (no whole-file wait, no length)
        live = await c.get("/api/stream/rx1?u=a&p=b&seek=1&format=mp3")
        assert live.headers["content-type"] == "audio/mpeg"
        assert "content-length" not in live.headers and len(live.content) > 1000
        # seek=0 unchanged
        zero = await c.get("/api/stream/rx1?u=a&p=b&seek=0")
        assert zero.content == wav.content


@needs_ffmpeg
@pytest.mark.asyncio
async def test_transcode_time_offset_streams_without_waiting(monkeypatch, tmp_path):
    aiff = tmp_path / "src.aiff"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-ac", "2", str(aiff))
    t = _T("ao1", aiff, 8.0, fmt="AIFF")
    app, _ = _app(monkeypatch, tmp_path, {"ao1": t})
    async with _client(app) as c:
        r = await c.get("/api/stream/ao1?u=a&p=b&format=mp3&seek=4")
        assert r.headers["content-type"] == "audio/mpeg" and r.headers["x-time-offset"]
        assert "content-length" not in r.headers
        full = await c.get("/api/stream/ao1?u=a&p=b&format=mp3")
        assert 0 < len(r.content) < len(full.content)
        # WAV path with an offset: header sized to the rest
        w = await c.get("/api/stream/ao1?u=a&p=b&seek=4")
        rest = int.from_bytes(w.content[40:44], "little")
        assert rest == len(w.content) - 44 and 0 < rest


@needs_ffmpeg
@pytest.mark.asyncio
async def test_ogg_output_uses_a_real_encoder():
    args = await stream._ffmpeg_encode_args("ogg", bitrate_kbps=96)
    assert args[args.index("-acodec") + 1] in ("libvorbis", "libopus", "vorbis")
    assert "ogg" not in args[args.index("-acodec") + 1:args.index("-acodec") + 2]


# ── r1-ren-4: what a Subsonic client will receive ────────────────────────────

def test_delivered_format_mirrors_the_stream_routing():
    df = stream.delivered_format
    assert df("/m/tune.ahx") == ("wav", "audio/wav")
    assert df("/m/song.mod") == ("wav", "audio/wav")
    assert df("/m/c64.sid", "SID") == ("wav", "audio/wav")
    assert df("/m/mdat.song", "TFMX") == ("wav", "audio/wav")
    assert df("/m/a.aiff", "AIFF") == ("wav", "audio/wav")
    assert df("/m/a.dsf", "DSD") == ("wav", "audio/wav")
    assert df("/m/a.aiff", "AIFF", "mp3") == ("mp3", "audio/mpeg")
    assert df("/m/a.dsf", "DSD", "mp3") == ("mp3", "audio/mpeg")
    assert df("/m/a.flac", "FLAC") is None
    assert df("/m/a.flac", "FLAC", "mp3") == ("mp3", "audio/mpeg")
    assert df("/m/a.mp3", "MP3", "mp3") is None
    assert df("/m/a.m4a", "AAC") is None
    assert df("/m/a.m4a", "ALAC") == ("wav", "audio/wav")
    assert df("/m/song.mod", "", "mp3") == ("mp3", "audio/mpeg")
    assert df("/z/pack.zip::mdat.acieed1") == ("wav", "audio/wav")


@needs_uade
@pytest.mark.asyncio
async def test_delivered_format_agrees_with_the_served_media_type(monkeypatch, tmp_path):
    real, _ = await _ref_render(DW)
    t = _T("df1", DW, round(real, 2))
    app, _ = _app(monkeypatch, tmp_path, {"df1": t})
    async with _client(app) as c:
        for fmt in (None, "mp3"):
            q = "?u=a&p=b" + (f"&format={fmt}" if fmt else "")
            r = await c.get(f"/api/stream/df1{q}")
            assert r.headers["content-type"] == stream.delivered_format(t.path, t.format, fmt)[1]


# ── r1-ren-17: HEAD ─────────────────────────────────────────────────────────

@needs_ffmpeg
@pytest.mark.asyncio
async def test_head_answers_headers_only_and_never_renders(monkeypatch, tmp_path):
    mp3 = tmp_path / "h.mp3"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(mp3))
    tracks = {"h1": _T("h1", mp3, 2.0, fmt="MP3"),
              "h2": _T("h2", tmp_path / "cold.dw", 0.0)}
    app, _ = _app(monkeypatch, tmp_path, tracks)
    called = []

    async def boom(*a, **k):
        called.append(a)
        raise AssertionError("HEAD must not render")
    monkeypatch.setattr(stream, "_render_uade", boom)
    async with _client(app) as c:
        r = await c.head("/api/stream/h1")
        assert r.status_code == 200 and r.content == b""
        assert r.headers["content-length"] == str(mp3.stat().st_size)
        assert r.headers["content-type"] == "audio/mpeg"
        r2 = await c.head("/api/stream/h2")
        assert r2.status_code == 200 and "content-length" not in r2.headers
        assert r2.headers["content-type"] == "audio/wav"
        assert (await c.head("/api/stream/nope")).status_code == 404
    assert called == []


@pytest.mark.asyncio
async def test_head_is_auth_gated(monkeypatch, tmp_path):
    from soniqboom.core import users as users_mod

    class _Users:
        def has_any(self):
            return True

        def lookup_session(self, tok):
            return None

        def get_by_username(self, u):
            return None

        def authenticate(self, u, p):
            return None
    monkeypatch.setattr(users_mod, "get_user_store", lambda: _Users())
    app, _ = _app(monkeypatch, tmp_path, {"h3": _T("h3", "/x/a.mp3", 1.0)}, auth=True)
    async with _client(app) as c:
        assert (await c.head("/api/stream/h3")).status_code == 401
        assert (await c.get("/api/stream/h3")).status_code == 401


# ── r1-ren-3: auth fast paths ───────────────────────────────────────────────

def test_subsonic_password_match_skips_scrypt(monkeypatch):
    from soniqboom.core import users as users_mod
    calls = []

    class _U:
        enabled, subsonic_password = True, "s3cr€t"

    class _Users:
        def has_any(self):
            return True

        def lookup_session(self, tok):
            return None

        def get_by_username(self, u):
            return _U()

        def authenticate(self, u, p):
            calls.append(p)
            return None
    monkeypatch.setattr(users_mod, "get_user_store", lambda: _Users())
    req = types.SimpleNamespace()
    _auth(req, None, "bob", "s3cr€t")
    _auth(req, None, "bob", "enc:" + "s3cr€t".encode().hex())
    assert calls == []                                   # no scrypt on a match
    with pytest.raises(stream.HTTPException):
        _auth(req, None, "bob", "wrong")
    assert calls == ["wrong"]


@pytest.mark.asyncio
async def test_in_process_preauth_flag_skips_the_second_auth(monkeypatch, tmp_path):
    """subsonic.py authenticates, then calls stream_track under the internal
    flag: no second credential check (no second scrypt)."""
    from soniqboom.core import users as users_mod
    checks = []

    class _Users:
        def has_any(self):
            checks.append(1)
            return True

        def lookup_session(self, tok):
            return None

        def get_by_username(self, u):
            return None

        def authenticate(self, u, p):
            return None
    monkeypatch.setattr(users_mod, "get_user_store", lambda: _Users())
    app, _ = _app(monkeypatch, tmp_path, {}, auth=True)
    req = types.SimpleNamespace(headers={}, cookies={}, method="GET")
    tok = stream._set_cast_internal_bypass(True)
    try:
        with pytest.raises(stream.HTTPException) as exc:
            await stream.stream_track("unknown-id", req, seek=0.0, subsong=0,
                                      file_path=None, target_format=None,
                                      max_bitrate_kbps=0, target_sample_rate=0,
                                      force_transcode=False, sb_session=None,
                                      u=None, p=None, s=None, t=None)
        assert exc.value.status_code == 404                 # past auth, not 401
        assert checks == []
    finally:
        stream._reset_cast_internal_bypass(tok)
    async with _client(app) as c:                          # never bindable from a request
        r = await c.get("/api/stream/unknown-id?_cast_internal_bypass=1")
        assert r.status_code == 401


# ── r1-ren-20: prewarm owner tags ───────────────────────────────────────────

def test_prewarm_requester_tags_distinguish_anonymous_clients():
    def req(host):
        return types.SimpleNamespace(client=types.SimpleNamespace(host=host))
    a = stream._prewarm_requester(req("192.168.1.10"), None, None)
    b = stream._prewarm_requester(req("192.168.1.99"), None, None)
    assert a != b
    assert (stream._prewarm_requester(req("1.1.1.1"), "sess", "bob")
            == stream._prewarm_requester(req("2.2.2.2"), "sess", None))
    assert (stream._prewarm_requester(req("1.1.1.1"), None, "bob")
            == stream._prewarm_requester(req("2.2.2.2"), None, "bob"))
    assert stream._prewarm_requester(None, None, None)            # no crash


# ── r1-ren-18: proxy buffering header on the progressive uade path ──────────

@needs_uade
@pytest.mark.asyncio
async def test_progressive_responses_ask_proxies_not_to_buffer(monkeypatch, tmp_path):
    if not BD.exists():
        pytest.skip("long test module missing")
    real, _ref = await _ref_render(BD)
    app, _ = _app(monkeypatch, tmp_path, {"ax1": _T("ax1", BD, round(real, 2)),
                                          "ax2": _T("ax2", BD, round(real, 2))})
    async with _client(app) as c:
        r = await c.get("/api/stream/ax1", cookies={"sb_session": "x"})
        assert r.headers["x-cache"] == "miss-progressive"
        assert r.headers["x-accel-buffering"] == "no"
        r2 = await c.get("/api/stream/ax2", cookies={"sb_session": "x"},
                         headers={"Range": "bytes=1000-"})
        assert r2.headers["x-accel-buffering"] == "no"
