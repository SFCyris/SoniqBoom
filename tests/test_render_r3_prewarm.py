# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background renders from review round 3: a cancelled prewarm keeps its gate
slot while its render runs; duration probes resolve (and download) outside
the gate, on the scan lane; a SID play retires the prewarm of the same tune
instead of rendering beside it; the Cast lookahead honours "Prepare upcoming
tracks" and runs under the same gate.

In-process only (fake renderers, throw-away cache dirs, fake remote shares).
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.core import conversion_cache

from test_render_r1_stream import FakeStore, _T, _app, _client

REPO = Path(__file__).resolve().parent.parent
SID = REPO / "internal/testdata/sid/SX-64_Demo.sid"


def _wav(path: Path, seconds: float) -> Path:
    frames = int(seconds * 44100)
    path.write_bytes(stream._build_wav_header(44100, 2, frames, bits_per_sample=16)
                     + b"\0" * (frames * 4))
    return path


@pytest.fixture()
def gates(monkeypatch, tmp_path):
    """Two render slots, one background slot, a throw-away cache."""
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_render_sem", asyncio.Semaphore(2))
    monkeypatch.setattr(stream, "_bg_render_sem", stream._PriorityGate(1))
    return stream


# ── r3-ren-7: a cancelled prewarm keeps its slot while its render runs ─────

@pytest.mark.asyncio
async def test_cancelled_prewarm_holds_its_slot_until_its_render_ends(gates, monkeypatch,
                                                                     tmp_path):
    started: list[str] = []
    done: list[str] = []

    async def slow_render(tid):
        async with stream._render_sem:
            started.append(tid)
            await asyncio.sleep(0.6)
            done.append(tid)
            return _wav(tmp_path / f"{tid}.wav", 0.1)

    async def fake_prewarm_render(track_id, file_path, ext, subsong, *, uade_named=False,
                                  track=None):
        await conversion_cache.get_or_render(
            track_id=track_id, format_type="tracker", subsong=0,
            render_fn=lambda: slow_render(track_id))

    async def resolve(track_id, track):
        return Path(track.path), ".mod", False, None
    monkeypatch.setattr(stream, "_resolve_for_prewarm", resolve)
    monkeypatch.setattr(stream, "_do_prewarm_render", fake_prewarm_render)
    a = asyncio.ensure_future(stream._do_prewarm("cpA", _T("cpA", "/x/a.mod"), 0))
    await asyncio.sleep(0.15)
    assert started == ["cpA"]
    a.cancel()                                   # /prewarm/retain or the FIFO cap
    b = asyncio.ensure_future(stream._do_prewarm("cpB", _T("cpB", "/x/b.mod"), 0))
    try:
        await asyncio.sleep(0.2)
        # A's render still runs (it fills the cache) and still counts: B waits
        # for the background slot, so a render slot stays free for a play.
        assert started == ["cpA"] and stream._render_sem._value == 1
        await asyncio.sleep(0.5)
        assert done == ["cpA"] and started == ["cpA", "cpB"]
        assert a.cancelled()
        await asyncio.wait_for(b, 2)
        assert done == ["cpA", "cpB"]
        assert await conversion_cache.get_cached(stream._ck("cpA", "tracker")) is not None
    finally:
        for k in ("cpA", "cpB"):
            conversion_cache._purge_entry(stream._ck(k, "tracker"))


@pytest.mark.asyncio
async def test_prewarm_still_queued_for_a_slot_cancels_at_once(gates, monkeypatch):
    ran = []

    async def fake_prewarm_render(*a, **k):
        ran.append(a[0])

    async def resolve(track_id, track):
        return Path(track.path), ".mod", False, None
    monkeypatch.setattr(stream, "_resolve_for_prewarm", resolve)
    monkeypatch.setattr(stream, "_do_prewarm_render", fake_prewarm_render)
    await stream._bg_render_sem.acquire()        # someone else holds the only slot
    try:
        q = asyncio.ensure_future(stream._do_prewarm("cq", _T("cq", "/x/q.mod"), 0))
        await asyncio.sleep(0.05)
        q.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(q, 0.5)
    finally:
        stream._bg_render_sem.release()
    await asyncio.sleep(0.05)
    assert ran == [] and stream._bg_render_sem._free == 1


# ── r3-ren-5: probes download outside the gate, on the scan lane ───────────

class _SlowRemote:
    def __init__(self, local: Path, delay: float):
        self.local, self.delay = local, delay
        self.fetches: list = []
        self.cached: set = set()

    def get_cached(self, share, rel):
        return self.local if (share, rel) in self.cached else None

    def fetch(self, share, rel, source, *, lane="stream"):
        self.fetches.append((rel, lane))
        time.sleep(self.delay)
        return self.local


@pytest.mark.asyncio
async def test_probe_download_does_not_hold_the_render_slot(gates, monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core import filesource, remote_cache
    from soniqboom.core import store as store_mod
    local = tmp_path / "tune.vgm"
    local.write_bytes(b"x")
    remote = _SlowRemote(local, 1.0)
    monkeypatch.setattr(filesource, "get_source", lambda root: object())
    monkeypatch.setattr(remote_cache, "get_cache", lambda: remote)
    monkeypatch.setattr(stream, "_REMOTE_PREWARM_GATES", {})
    t = _T("pv1", "ftp://nas/music:/Chip/tune.vgm", float(settings.sid_default_duration),
           fmt="VGM")
    fs = FakeStore({"pv1": t})
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)

    async def get_track(tid):
        return t if tid == "pv1" else None
    monkeypatch.setattr(stream, "get_track", get_track)
    rendered = []

    async def fake_gme(path, subsong=0):
        rendered.append(path)
        return _wav(tmp_path / "g.wav", 42.0)
    monkeypatch.setattr(stream, "_render_gme", fake_gme)

    async def _noauth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: True)
    probe = asyncio.ensure_future(stream.probe_durations({"track_ids": ["pv1"]}, None, None))
    await asyncio.sleep(0.2)                     # the download is under way
    t0 = time.monotonic()
    await asyncio.wait_for(stream._bg_render_sem.acquire(stream.PRIO_NEXT), 0.5)
    waited = time.monotonic() - t0
    stream._bg_render_sem.release()
    assert waited < 0.1, waited                  # the NEXT track isn't queued behind it
    assert await asyncio.wait_for(probe, 5) == {"pv1": 42.0}
    assert fs.batches == [[("pv1", {"duration": 42.0})]]
    assert remote.fetches == [("/Chip/tune.vgm", "scan")] and rendered == [local]


@pytest.mark.asyncio
async def test_probe_skips_remote_archive_members_not_cached_locally(gates, monkeypatch,
                                                                    tmp_path):
    from soniqboom.core import filesource, remote_cache
    remote = _SlowRemote(tmp_path / "pack.zip", 0.0)
    monkeypatch.setattr(filesource, "get_source", lambda root: object())
    monkeypatch.setattr(remote_cache, "get_cache", lambda: remote)
    monkeypatch.setattr(stream, "_REMOTE_PREWARM_GATES", {})
    t = _T("pv2", "ftp://nas/music:/Adlib/pack.zip::song.hsc", 180.0, fmt="HSC")

    async def get_track(tid):
        return t
    monkeypatch.setattr(stream, "get_track", get_track)
    assert await stream._probe_one_rendered_duration("pv2") is None
    assert remote.fetches == []


# ── r3-ren-6: a SID play retires the prewarm of the same tune ──────────────

_FAKE_SIDPLAYFP = r'''
import struct, sys, time
out, dur = None, 60
for a in sys.argv[1:]:
    if a.startswith("-w"):
        out = a[2:]
    elif a.startswith("-t"):
        dur = int(a[2:])
rate = 44100
total = dur * rate * 2
with open(out, "wb") as f:
    f.write(b"RIFF" + struct.pack("<I", 36 + total) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", total))
    step, written = rate * 2 // 10, 0
    while written < total:
        n = min(step, total - written)
        f.write(b"\x01\x00" * (n // 2))
        f.flush()
        written += n
        time.sleep(0.1 / 30)             # ~30x realtime
'''


def _fake_sid_env(monkeypatch, tmp_path):
    script = tmp_path / "fake_sidplayfp.py"
    script.write_text(_FAKE_SIDPLAYFP)
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "sidplayfp")
    monkeypatch.setattr(
        stream, "_sid_render_cmd",
        lambda binary, path, subsong, dur, out, mute=(), start=1:
            [sys.executable, str(script), f"-t{int(dur)}", f"-w{out}", str(path)])
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    procs: list = []
    real_exec = asyncio.create_subprocess_exec

    async def spy_exec(*argv, **kw):
        p = await real_exec(*argv, **kw)
        if len(argv) > 1 and argv[1] == str(script):
            procs.append(p)
        return p
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
    return procs


async def _max_alive(procs: list, stop: asyncio.Event, out: list) -> None:
    while not stop.is_set():
        out.append(sum(1 for p in procs if p.returncode is None))
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.skipif(not SID.exists(), reason="SID fixture missing")
async def test_clicking_a_sid_during_its_prewarm_runs_one_render_at_a_time(
        monkeypatch, tmp_path):
    procs = _fake_sid_env(monkeypatch, tmp_path)
    tid = "sidpw1"
    t = _T(tid, SID, 60.0, fmt="SID")
    app, _ = _app(monkeypatch, tmp_path, {tid: t})
    full_key = stream._ck(tid, "sid", subsong=0, duration=60)
    stop, alive = asyncio.Event(), []
    sampler = asyncio.ensure_future(_max_alive(procs, stop, alive))
    pw = asyncio.ensure_future(stream._do_prewarm(tid, t, 0, stream.PRIO_NEXT))
    try:
        for _ in range(100):
            if procs:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.4)
        assert len(procs) == 1 and procs[0].returncode is None     # prewarm rendering
        async with _client(app) as c:
            r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
        assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
        assert len(r.content) == 44 + 60 * 44100 * 2
        await asyncio.wait({pw}, timeout=3)
        assert pw.done() and pw.cancelled()                        # retired
        assert len(procs) == 2 and max(alive) == 1
        for _ in range(100):                                       # the play caches it
            if await conversion_cache.get_cached(full_key) is not None:
                break
            await asyncio.sleep(0.05)
        assert await conversion_cache.get_cached(full_key) is not None
        assert full_key not in stream._SID_PREWARM_RENDERS
    finally:
        stop.set()
        await sampler
        for p in procs:
            if p.returncode is None:
                p.kill()
        conversion_cache._purge_entry(full_key)


@pytest.mark.asyncio
@pytest.mark.skipif(not SID.exists(), reason="SID fixture missing")
async def test_sid_prewarm_never_starts_beside_a_progressive_play(monkeypatch, tmp_path):
    procs = _fake_sid_env(monkeypatch, tmp_path)
    tid = "sidpw2"
    t = _T(tid, SID, 30.0, fmt="SID")
    app, _ = _app(monkeypatch, tmp_path, {tid: t})
    full_key = stream._ck(tid, "sid", subsong=0, duration=30)
    try:
        async with _client(app) as c:
            play = asyncio.ensure_future(
                c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"}))
            for _ in range(200):
                if procs:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            await stream._do_prewarm(tid, t, 0, stream.PRIO_NEXT)   # returns at once
            assert len(procs) == 1
            # the in-render registration refuses too (the race the check closes)
            with pytest.raises(asyncio.CancelledError):
                await stream._sid_prewarm_render(full_key, SID, 0, 30)
            assert len(procs) == 1
            r = await play
        assert r.headers["x-cache"] == "miss-progressive"
    finally:
        for p in procs:
            if p.returncode is None:
                p.kill()
        await asyncio.sleep(0.2)
        conversion_cache._purge_entry(full_key)


# ── r3-ren-15: the Cast lookahead obeys the setting and the gate ───────────

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_cast_lookahead_obeys_prepare_upcoming_tracks(gates, monkeypatch, enabled):
    from soniqboom.core import cast_render, cast_session
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: enabled)
    tracks = {k: _T(k, f"/x/{k}.mod") for k in ("ca", "cb", "cc")}

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(cast_session, "get_track", get_track)
    fetched, started = [], []

    async def materialize(path, track_id, *, lane="stream"):
        fetched.append((track_id, lane))
        return Path(path)
    monkeypatch.setattr(cast_render, "materialize_source", materialize)

    async def gated(prio, ck, fmt, render_fn):
        started.append((prio, ck, fmt))
    monkeypatch.setattr(cast_session, "_start_gated_render", gated)
    sess = cast_session.CastSession(types.SimpleNamespace(protocol="dlna"))
    sess._caps = {"audio/mpeg", "audio/wav"}
    sess.state.queue = [cast_session.QueueItem(k) for k in tracks]
    await sess._prewarm_lookahead("ca")
    if not enabled:
        assert fetched == [] and started == []
    else:
        assert fetched == [("cb", "scan"), ("cc", "scan")]
        assert [(p, f) for p, _, f in started] == [(stream.PRIO_NEXT, "tracker"),
                                                   (stream.PRIO_AHEAD, "tracker")]


@pytest.mark.asyncio
async def test_cast_gated_render_waits_for_a_slot_before_it_is_in_flight(gates, tmp_path):
    from soniqboom.core import cast_session
    gate = stream._bg_render_sem
    ran = []

    async def render():
        ran.append(gate._free)
        await asyncio.sleep(0.1)
        return _wav(tmp_path / "cg.wav", 0.1)
    key = stream._ck("cg1", "tracker")
    await gate.acquire()                         # the only background slot is busy
    try:
        s = asyncio.ensure_future(
            cast_session._start_gated_render(stream.PRIO_AHEAD, key, "tracker", render))
        await asyncio.sleep(0.1)
        # queued at the gate — NOT registered as in flight, so a foreground
        # play of this track renders it itself instead of waiting behind us
        assert not s.done() and key not in conversion_cache._inflight and ran == []
    finally:
        gate.release()
    await asyncio.wait_for(s, 1)
    assert ran == [0]                            # rendered while holding the slot
    for _ in range(50):
        if gate._free == 1 and key not in conversion_cache._inflight:
            break
        await asyncio.sleep(0.02)
    assert gate._free == 1                       # handed back when the render ended
    assert await conversion_cache.get_cached(key) is not None
    # already cached: no render, slot handed straight back
    await cast_session._start_gated_render(stream.PRIO_NEXT, key, "tracker", render)
    assert ran == [0] and gate._free == 1
    conversion_cache._purge_entry(key)
