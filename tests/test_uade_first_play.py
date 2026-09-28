# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""First play of uncached Amiga (uade) tracks — GitHub issue #15.

Covers the pieces that made a cold first play slow or fail:
  * uade renders stream through a pipe into a growing WAV (live entry), with
    an exact header when the length is known and a patched one otherwise;
  * the waveform endpoint never starts a render (it attaches or says pending);
  * background renders are served most-urgent-first (priority gate);
  * a listener's stale prewarms can be dropped without touching another's.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.api import tracks as tracks_api
from soniqboom.core import conversion_cache


async def _noauth(*a, **k):
    """Stand-in for the (async) stream sign-in check."""
    return None


REPO = Path(__file__).resolve().parent.parent
DW = REPO / "internal/testdata/uade/David Whittaker/carrier command.dw"
BD = REPO / "internal/testdata/uade/Ben Daglish/mickey mouse.bd"

needs_uade = pytest.mark.skipif(
    not shutil.which("uade123") or not DW.exists(),
    reason="uade123 or the local Amiga test modules are not available")


def _pcm(wav: bytes) -> bytes:
    i = wav.find(b"data")
    size = int.from_bytes(wav[i + 4:i + 8], "little")
    return wav[i + 8:i + 8 + size]


@needs_uade
@pytest.mark.asyncio
async def test_pipe_render_is_byte_identical_to_uades_own_wav(tmp_path):
    ref = tmp_path / "ref.wav"
    # Same invocation as the renderer: in the module's folder, by bare name
    # (the emulated player sees the name, so the path changes the output).
    proc = await asyncio.create_subprocess_exec(
        "uade123", "-1", "--filter=A1200", "--headphones", "-e", "wav",
        "-f", str(ref), "--", DW.name, cwd=str(DW.parent),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    out = await stream._render_uade(DW)
    try:
        got = out.read_bytes()
        assert got[:4] == b"RIFF" and got[8:12] == b"WAVE"
        assert _pcm(got) == _pcm(ref.read_bytes())
        # header sizes describe exactly the bytes written
        assert int.from_bytes(got[40:44], "little") == len(got) - 44
        assert int.from_bytes(got[4:8], "little") == len(got) - 8
    finally:
        out.unlink(missing_ok=True)


@needs_uade
@pytest.mark.asyncio
async def test_live_render_is_playable_before_it_finishes_and_header_exact():
    if not BD.exists():
        pytest.skip("long test module missing")
    ref = await stream._render_uade(BD)
    seconds = (ref.stat().st_size - 44) / stream._UADE_BYTES_PER_SEC
    ref_pcm = _pcm(ref.read_bytes())
    ref.unlink()

    key = "t-live__sub0"
    task = asyncio.ensure_future(stream._render_uade(BD, live_key=key, expected_seconds=seconds))
    live = None
    for _ in range(200):
        live = stream._UADE_LIVE.get(key)
        if live is not None and live["bytes"] > 0:
            break
        await asyncio.sleep(0.005)
    assert live is not None and live["bytes"] > 0
    assert not live["complete"].is_set(), "a 2-minute tune should still be rendering"
    assert live["expected_size"] == 44 + len(ref_pcm)
    head = Path(live["path"]).read_bytes()[:44]
    assert int.from_bytes(head[40:44], "little") == len(ref_pcm)   # exact from byte 0
    out = await task
    try:
        assert live["complete"].is_set() and live["clean_exit"] is True
        assert _pcm(out.read_bytes()) == ref_pcm
    finally:
        out.unlink(missing_ok=True)
        stream._UADE_LIVE.pop(key, None)


@needs_uade
@pytest.mark.asyncio
async def test_unknown_length_header_is_patched_and_failures_are_errors(tmp_path):
    out = await stream._render_uade(DW, live_key="t-unk__sub0", expected_seconds=0)
    try:
        data = out.read_bytes()
        assert int.from_bytes(data[40:44], "little") == len(data) - 44 > 0
    finally:
        out.unlink(missing_ok=True)
        stream._UADE_LIVE.pop("t-unk__sub0", None)

    junk = tmp_path / "dw.notamodule"
    junk.write_bytes(b"MZ" + b"\x00" * 4000)
    with pytest.raises(stream.HTTPException) as exc:
        await stream._render_uade(junk, live_key="t-bad__sub0")
    assert exc.value.status_code in (422, 502)
    assert "t-bad__sub0" not in stream._UADE_LIVE          # a failed live entry is dropped


def test_renderer_failure_mapping():
    e = stream._renderer_failure("uade", "uade123", 1, "uadecore: module check failed")
    assert e.status_code == 422
    assert stream._renderer_failure("uade", "uade123", 1, "score died").status_code == 502
    assert stream._renderer_failure("x", "/bin/x", -6, "dyld: Symbol not found").status_code == 501


@pytest.mark.asyncio
async def test_priority_gate_serves_the_most_urgent_waiter_first():
    gate = stream._PriorityGate(1)
    order: list[str] = []
    await gate.acquire()                       # the slot is busy

    async def job(name, prio):
        async with gate.slot(prio):
            order.append(name)
            await asyncio.sleep(0)

    tasks = [asyncio.ensure_future(job("probe", stream.PRIO_PROBE)),
             asyncio.ensure_future(job("vu", stream.PRIO_VU)),
             asyncio.ensure_future(job("next", stream.PRIO_NEXT)),
             asyncio.ensure_future(job("ahead", stream.PRIO_AHEAD))]
    await asyncio.sleep(0)
    assert gate.waiting() == 4 and gate.waiting(stream.PRIO_AHEAD) == 2
    gate.release()
    await asyncio.gather(*tasks)
    assert order == ["next", "ahead", "probe", "vu"]


@pytest.mark.asyncio
async def test_priority_gate_cancelled_waiter_never_leaks_the_slot():
    gate = stream._PriorityGate(1)
    await gate.acquire()
    waiter = asyncio.ensure_future(gate.acquire(0))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.release()
    await asyncio.wait_for(gate.acquire(1), timeout=1)   # the slot came back
    gate.release()


@pytest.mark.asyncio
async def test_waveform_never_renders_it_attaches_or_says_pending(monkeypatch, tmp_path):
    tid = "wf-track"
    # nothing cached, nothing rendering → pending, quickly
    got = await tracks_api._waveform_from_conversion_cache(
        tid, "/x/dw.intro", ".intro", appear_wait=0.2)
    assert got is tracks_api._WAVEFORM_PENDING

    # a render in flight → the waveform waits for it and returns the cached WAV
    key = conversion_cache._cache_key(tid, "uade", 0)
    ev = asyncio.Event()
    conversion_cache._inflight[key] = ev
    wav = tmp_path / "w.wav"
    wav.write_bytes(b"RIFF")
    state = {"cached": None}

    async def fake_get_cached(k):
        return state["cached"] if k == key else None
    monkeypatch.setattr(conversion_cache, "get_cached", fake_get_cached)
    waiter = asyncio.ensure_future(tracks_api._waveform_from_conversion_cache(
        tid, "/x/dw.intro", ".intro", appear_wait=0.2, finish_wait=5))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    state["cached"] = wav
    conversion_cache._inflight.pop(key)
    ev.set()
    assert await waiter == wav

    # the render failed (nothing cached) → None (blank waveform, no error)
    state["cached"] = None
    ev2 = asyncio.Event()
    conversion_cache._inflight[key] = ev2
    waiter = asyncio.ensure_future(tracks_api._waveform_from_conversion_cache(
        tid, "/x/dw.intro", ".intro", appear_wait=0.2, finish_wait=5))
    await asyncio.sleep(0.05)
    conversion_cache._inflight.pop(key)
    ev2.set()
    assert await waiter is None


def test_render_state_reports_rendering_complete_idle():
    tid = "rs-track"
    key = conversion_cache._cache_key(tid, "uade", 0)
    assert conversion_cache.render_state(tid) == "idle"
    conversion_cache._inflight[key] = asyncio.Event()
    try:
        assert conversion_cache.render_state(tid) == "rendering"
        assert stream._render_state(tid) == "rendering"
    finally:
        conversion_cache._inflight.pop(key)
    with conversion_cache._state_lock:
        conversion_cache._meta[key] = {"path": "/nope.wav", "size_bytes": 0,
                                       "format_type": "uade", "created_at": 0}
    try:
        assert conversion_cache.render_state(tid) == "complete"
        assert conversion_cache.render_state("rs-trac") == "idle"     # prefix-safe
    finally:
        with conversion_cache._state_lock:
            conversion_cache._meta.pop(key, None)


@pytest.mark.asyncio
async def test_retain_drops_only_the_callers_stale_prewarms(monkeypatch):
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)

    class Req:
        client = None
        cookies = {}

    async def forever():
        await asyncio.sleep(3600)

    a = stream._prewarm_requester(Req(), "session-a", None)
    b = stream._prewarm_requester(Req(), "session-b", None)
    tasks = {}
    for tid, owners in (("keep", {a}), ("stale", {a}), ("shared", {a, b}), ("other", {b})):
        k = stream._prewarm_key(tid, ".dw", 0)
        tasks[tid] = asyncio.ensure_future(forever())
        stream._prewarm_tasks[k] = tasks[tid]
        stream._prewarm_owner[k] = set(owners)
    try:
        res = await stream.prewarm_retain({"ids": ["keep"]}, sb_session="session-a",
                                          u=None, p=None, s=None, t=None, request=Req())
        await asyncio.sleep(0)
        assert res == {"cancelled": 1}
        assert tasks["stale"].cancelled()
        assert not tasks["keep"].done() and not tasks["shared"].done() and not tasks["other"].done()
        assert stream._prewarm_owner[stream._prewarm_key("shared", ".dw", 0)] == {b}
    finally:
        for t in tasks.values():
            t.cancel()
        for tid in tasks:
            k = stream._prewarm_key(tid, ".dw", 0)
            stream._prewarm_tasks.pop(k, None)
            stream._prewarm_owner.pop(k, None)


# ── Through the real stream endpoint ─────────────────────────────────────────

ZIP = REPO / "internal/testdata/archives/amiga_pack.zip"


def _app_with(monkeypatch, tmp_path, tracks: dict):
    import types
    from fastapi import FastAPI
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: (tmp_path / "zx").mkdir(exist_ok=True) or tmp_path / "zx")
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop_backfill)
    monkeypatch.setattr(stream, "_spawn_uade_vu", lambda *a, **k: None)

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(stream, "get_track", get_track)
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    return app


async def _noop_backfill(*a, **k):
    return None


class _T:
    def __init__(self, tid, path, duration):
        self.id, self.path, self.duration = tid, str(path), duration
        self.format, self.genre, self.title = "David Whittaker", ["Amiga", "Module"], "t"


@needs_uade
@pytest.mark.asyncio
async def test_stream_cold_unknown_length_then_cache_hit(monkeypatch, tmp_path):
    import httpx
    app = _app_with(monkeypatch, tmp_path, {"cold1": _T("cold1", DW, 0.0)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/stream/cold1", cookies={"sb_session": "x"})
        # A 4 s tune may finish rendering before the answer (→ the cached
        # file, "miss") or stream while it renders under a provisional
        # header ("miss-progressive") — both are complete WAVs.
        assert r.status_code == 200
        assert r.headers["x-cache"] in ("miss", "miss-progressive")
        assert r.content[:4] == b"RIFF" and len(r.content) > 700_000
        r2 = await c.get("/api/stream/cold1", headers={"Range": "bytes=0-99"})
        assert r2.status_code == 206 and r2.headers["x-cache"] == "hit"


@needs_uade
@pytest.mark.asyncio
@pytest.mark.parametrize("ua", [
    # iOS home-screen web app / in-app WKWebView: WebKit without "Safari"
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
])
async def test_unknown_length_range_probe_gets_a_real_206(monkeypatch, tmp_path, ua):
    """A cold unknown-length render answers a ``bytes=0-1`` probe with its two
    bytes against the finished size — whatever the UA — never with the whole
    render as a chunked 200."""
    import httpx
    if not BD.exists():
        pytest.skip("long test module missing")
    app = _app_with(monkeypatch, tmp_path, {"probe1": _T("probe1", BD, 0.0)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/stream/probe1", cookies={"sb_session": "x"},
                        headers={"Range": "bytes=0-1", "User-Agent": ua})
        assert r.status_code == 206, (r.status_code, r.headers.get("x-cache"))
        assert r.content == b"RI" and r.headers["content-length"] == "2"
        size = int(r.headers["content-range"].rpartition("/")[2])
        assert r.headers["content-range"] == f"bytes 0-1/{size}" and size > 1_000_000
        assert "x-render-length" not in r.headers
        # the listener's next request is a cache hit with full range support
        r2 = await c.get("/api/stream/probe1", cookies={"sb_session": "x"},
                         headers={"Range": "bytes=0-", "User-Agent": ua})
        assert r2.status_code == 206 and r2.headers["x-cache"] == "hit"
        assert r2.headers["content-length"] == str(size)


@needs_uade
@pytest.mark.asyncio
async def test_stream_known_length_plays_progressively_for_the_web_ui(monkeypatch, tmp_path):
    import httpx
    if not BD.exists():
        pytest.skip("long test module missing")
    ref = await stream._render_uade(BD)
    seconds = (ref.stat().st_size - 44) / stream._UADE_BYTES_PER_SEC
    ref_pcm = _pcm(ref.read_bytes())
    ref.unlink()
    app = _app_with(monkeypatch, tmp_path, {"prog1": _T("prog1", BD, seconds)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/stream/prog1", cookies={"sb_session": "x"})
        assert r.headers["x-cache"] == "miss-progressive"
        assert _pcm(r.content) == ref_pcm
        # Subsonic-style request (no session cookie) never gets a growing file
        r2 = await c.get("/api/stream/prog1?u=a&p=b")
        assert r2.headers["x-cache"] == "hit" and "content-length" in r2.headers


@needs_uade
@pytest.mark.asyncio
async def test_zip_packed_tfmx_first_play_with_a_concurrent_waveform_request(monkeypatch, tmp_path):
    """The #15 failure: the waveform request used to prepare an archive member
    without its Amiga name + sample half and poison the render the audio request
    was waiting on.  Now the waveform only attaches."""
    import httpx
    if not ZIP.exists():
        pytest.skip("archive fixture missing")
    tid = "zipt1"
    t = _T(tid, f"{ZIP}::mdat.acieed1", 0.0)
    t.format = "TFMX Pro"
    app = _app_with(monkeypatch, tmp_path, {tid: t})

    async def fake_get_track(x):
        return t if x == tid else None
    monkeypatch.setattr(tracks_api, "_sid_target_duration", lambda tr: None)
    wf = asyncio.ensure_future(tracks_api._waveform_from_conversion_cache(
        tid, t.path, ".acieed1", appear_wait=3, finish_wait=60))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
    assert r.status_code == 200, r.text[:200]
    assert r.content[:4] == b"RIFF" and len(r.content) > 1_000_000
    got = await wf
    assert isinstance(got, Path) and got.exists()
