# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render-path fixes, round 4.

  * a uade name token (``One.wav``, ``P10.mp3``, ``ONE.IT``) never overrides
    an extension another engine owns — unless the scanner verified the file
    as an Amiga module (tracker extensions / ``.sid`` only);
  * uade runs in the module's folder (Musicline on long paths), a missing
    TFMX/RJP sample half is named, a live render failing mid-stream aborts
    the body, a cold first byte waits on events;
  * a SID prewarm retired for a progressive play neither 502s its waiters
    nor leaves the waveform blank; a play never queues behind a prewarm's
    download of the same loose remote module;
  * "Prepare upcoming tracks" gates rendered formats only; one shared
    ``_schedule_prewarm``; multi-tune start songs are written back to the
    record; the waveform decode streams instead of buffering the whole PCM.

In-process only (fake stores / renderers, throw-away dirs); the uade and
ffmpeg cases skip when the binary or the private test module is missing.
"""
from __future__ import annotations

import asyncio
import shutil
import wave
from pathlib import Path

import pytest
from fastapi import Request

from soniqboom.api import stream
from soniqboom.api import tracks as tracks_api
from soniqboom.core import conversion_cache
from soniqboom.core import uade_formats

REPO = Path(__file__).resolve().parent.parent

needs_conf = pytest.mark.skipif(
    not uade_formats.player_map(),
    reason="uade's eagleplayer.conf is not installed (name classification off)")


async def _noauth(*a, **k):
    return None


async def _noop(*a, **k):
    return None


class _T:
    def __init__(self, tid, path, *, genre=None, fmt="", duration=0.0):
        self.id, self.path, self.duration = tid, str(path), duration
        self.format, self.title = fmt, "t"
        self.genre = list(genre or [])


def _app_with(monkeypatch, tmp_path, tracks: dict):
    from fastapi import FastAPI
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_zip_extract_dir",
                        lambda: (tmp_path / "zx").mkdir(exist_ok=True) or tmp_path / "zx")
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_backfill_rendered_duration", _noop)
    monkeypatch.setattr(stream, "_spawn_uade_vu", lambda *a, **k: None)

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(stream, "get_track", get_track)
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    return app


def _write_wav(path: Path, seconds: float = 0.2) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x10" * int(8000 * seconds))
    return path


# ── r4-ren-1: owned extensions win over a uade name token ──────────────────

@needs_conf
@pytest.mark.parametrize("name", [
    "P10.mp3", "One.wav", "Two.wav", "One.flac", "Sun.mp3",
    "ONE.IT", "UFO.XM", "ASH.XM", "ONE.MOD", "Fred.sid",
    "mv_entry2.zip::ONE.IT",
])
def test_uade_prefix_never_overrides_an_owned_extension(name):
    assert uade_formats.classify(name.split("::")[-1]) is not None   # the collision
    ext, uade_named = stream._render_ident(f"/m/{name}")
    assert uade_named is False, name
    assert not stream.is_uade_routed(f"/m/{name}")


@needs_conf
def test_scanner_verified_amiga_module_keeps_uade_for_tracker_and_sid_exts():
    amiga = _T("a", "/m/bp.song.mod", genre=["Amiga", "Module"])
    assert stream._render_ident("/m/bp.song.mod", amiga) == (".mod", True)
    assert stream._render_ident("/m/ONE.MOD", _T("b", "/m/ONE.MOD",
                                genre=["Amiga", "Module"]))[1] is True
    # dict-shaped records (Subsonic's store rows) work too
    assert stream._render_ident("/m/fred.sid", {"genre": ["Amiga", "Module"]})[1] is True
    assert stream.is_uade_routed("/m/fred.sid", {"genre": ["Amiga", "Module"]})
    # a tracker-verified or C64 record does not
    assert stream._render_ident("/m/ONE.MOD", _T("c", "/m/ONE.MOD",
                                genre=["Tracker", "Module"]))[1] is False
    c64 = _T("d", "/C64Music/GAMES/A-F/Fred.sid", genre=["Chiptune", "C64"])
    assert stream.is_uade_routed(c64.path, c64) is False
    # plain audio never goes to uade, whatever the stored genre says
    assert stream._render_ident("/m/P10.mp3", _T("e", "/m/P10.mp3",
                                genre=["Amiga", "Module"]))[1] is False


@needs_conf
def test_real_amiga_names_still_route_to_uade():
    assert stream._render_ident("/m/mdat.acieed1") == (".acieed1", True)
    assert stream._render_ident("/a/amiga_pack.zip::mdat.acieed1")[1] is True
    assert stream.is_uade_routed("/m/legendcrack.fc")
    # AHX keeps its uade route (prefix and suffix both name it)
    assert stream.is_uade_routed("/m/x.lha::Buzzer\\AHX.Airwolf.ahx")
    # AdLib still wins over a name collision
    assert stream._render_ident("/m/star.amd") == (".amd", False)


@needs_conf
@pytest.mark.asyncio
@pytest.mark.parametrize("name,mime", [("One.wav", "audio/wav"), ("P10.mp3", "audio/mpeg")])
async def test_misnamed_audio_file_streams_natively(monkeypatch, tmp_path, name, mime):
    import httpx
    src = tmp_path / name
    if name.endswith(".wav"):
        _write_wav(src)
    else:
        src.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 400)
    tr = _T("nat1", src, fmt=name.rsplit(".", 1)[1].upper())

    async def boom(*a, **k):
        raise AssertionError("uade must not be asked to play an audio file")
    monkeypatch.setattr(stream, "_serve_uade", boom)
    app = _app_with(monkeypatch, tmp_path, {"nat1": tr})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/api/stream/nat1", cookies={"sb_session": "x"})
        assert r.status_code == 200, r.text[:200]
        assert r.headers["content-type"].startswith(mime)
        assert r.content == src.read_bytes()
        assert "x-rendered" not in r.headers


@needs_conf
@pytest.mark.asyncio
async def test_pc_tracker_module_with_amiga_looking_name_renders_via_openmpt(monkeypatch, tmp_path):
    import httpx
    src = tmp_path / "UFO.XM"
    src.write_bytes(b"Extended Module: " + b"\x00" * 400)
    wav = _write_wav(tmp_path / "out.wav")
    seen: list[str] = []

    async def fake_get_or_render(*, track_id, format_type, subsong=0, render_fn=None, **k):
        seen.append(format_type)
        return wav, True
    monkeypatch.setattr(conversion_cache, "get_or_render", fake_get_or_render)

    async def boom(*a, **k):
        raise AssertionError("uade must not be asked to play a FastTracker module")
    monkeypatch.setattr(stream, "_serve_uade", boom)
    app = _app_with(monkeypatch, tmp_path, {"xm1": _T("xm1", src, genre=["Tracker", "Module"])})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/api/stream/xm1", cookies={"sb_session": "x"})
    assert r.status_code == 200, r.text[:200]
    assert seen == ["tracker"]


@needs_conf
@pytest.mark.asyncio
async def test_waveform_of_a_misnamed_mp3_is_not_left_pending(monkeypatch, tmp_path):
    """``P10.mp3`` used to count as an Amiga render for the waveform, which
    then waited for a uade render that never comes (pending, then blank)."""
    src = tmp_path / "P10.mp3"
    src.write_bytes(b"\xff\xfb\x90\x00" * 100)
    tr = _T("wfmp3", src, fmt="MP3")
    from soniqboom.core import data as data_mod

    async def get_track(tid):
        return tr if tid == "wfmp3" else None

    async def get_waveform(tid):
        return None

    async def store_waveform(tid, wf):
        return None
    monkeypatch.setattr(data_mod, "get_track", get_track)
    monkeypatch.setattr(data_mod, "get_waveform", get_waveform)
    monkeypatch.setattr(data_mod, "store_waveform", store_waveform)

    async def must_not_be_called(*a, **k):
        raise AssertionError("a native MP3 must not wait for a render")
    monkeypatch.setattr(tracks_api, "_waveform_from_conversion_cache", must_not_be_called)
    computed: list[str] = []

    async def fake_compute(path, points=200):
        computed.append(path)
        return {"peaks": [0.5] * points, "rms": [0.25] * points}
    monkeypatch.setattr(tracks_api, "_compute_waveform_safe", fake_compute)
    from starlette.responses import Response
    res = await tracks_api.get_track_waveform("wfmp3", Response(), subsong=0)
    assert res.get("waveform") and not res.get("pending")
    assert computed and computed[0] == str(src)


# ── r4-ren-4: uade runs in the module's folder (long paths) ────────────────

ML = REPO / "internal/testdata/uade/Musicline Editor/anorak3.ml"
needs_ml = pytest.mark.skipif(
    not shutil.which("uade123") or not ML.exists(),
    reason="uade123 or the Musicline test module is not available")


@needs_ml
@pytest.mark.asyncio
async def test_musicline_module_on_a_long_path_still_renders(tmp_path):
    """Musicline Editor's player crashes ("score crashed", no audio) when the
    module path is longer than 127 characters; uade now gets the bare name."""
    deep = tmp_path
    while len(str(deep)) < 150:
        deep = deep / ("d" * 30)
    deep.mkdir(parents=True)
    src = deep / "anorak3.ml"
    shutil.copyfile(ML, src)
    assert len(str(src)) > 160
    out = await stream._render_uade(src, live_key="r4-ml__sub0")
    try:
        assert out.stat().st_size > 100_000        # real audio, not a bare header
    finally:
        out.unlink(missing_ok=True)
        stream._UADE_LIVE.pop("r4-ml__sub0", None)


def test_uade_vu_cmd_passes_the_bare_module_name(tmp_path):
    p = tmp_path / ("x" * 140) / "anorak3.ml"
    cmd = stream._uade_vu_cmd("uade123", p, 0, 0, "/tmp/dump")
    assert cmd[-2:] == ["--", "anorak3.ml"]
    assert stream._uade_cwd(p) == str(p.parent)


# ── r4-ren-7: a missing TFMX/RJP sample half is named, not "exit status 1" ─

@needs_conf
def test_renderer_failure_names_a_missing_companion(tmp_path):
    mod = tmp_path / "mdat.acieed1"
    mod.write_bytes(b"\x00" * 64)
    died = "uade warning: Song ended prematurely due to error: score died\nCan not play mdat.acieed1"
    e = stream._renderer_failure("uade", "uade123", 1, died, module_path=mod)
    assert e.status_code == 422
    assert "smpl.acieed1" in e.detail and "companion sample file" in e.detail
    # the sample half IS there (any case): the generic failure stays
    (tmp_path / "SMPL.ACIEED1").write_bytes(b"\x00")
    assert stream._renderer_failure("uade", "uade123", 1, died,
                                    module_path=mod).status_code == 502
    # a single-file player never blames a companion
    fc = tmp_path / "legendcrack.fc"
    fc.write_bytes(b"\x00")
    assert stream._renderer_failure("uade", "uade123", 1, died,
                                    module_path=fc).status_code == 502
    # "module check failed" keeps its own wording
    e = stream._renderer_failure("uade", "uade123", 1, "module check failed", module_path=mod)
    assert e.status_code == 422 and "isn't a playable Amiga module" in e.detail


ZIP = REPO / "internal/testdata/archives/amiga_pack.zip"


@pytest.mark.skipif(not shutil.which("uade123") or not ZIP.exists(),
                    reason="uade123 or the TFMX archive fixture is not available")
@pytest.mark.asyncio
async def test_tfmx_without_its_sample_file_says_so(tmp_path):
    import zipfile
    with zipfile.ZipFile(ZIP) as z:
        (tmp_path / "mdat.acieed1").write_bytes(z.read("mdat.acieed1"))
    with pytest.raises(stream.HTTPException) as exc:
        await stream._render_uade(tmp_path / "mdat.acieed1")
    assert exc.value.status_code == 422
    assert "smpl.acieed1" in exc.value.detail


# ── r4-ren-6: a live render that fails mid-stream aborts the body ──────────

def _growing_app(tmp_path, live: dict, cleaned: list):
    from fastapi import FastAPI
    from starlette.background import BackgroundTask
    app = FastAPI()

    @app.get("/g")
    async def g(request: Request):
        return await stream._chunked_growing_file_response(
            request, live["path"], live["expected_size"], live["complete"],
            media_type="audio/wav", data_event=live["data"], inflight=live,
            background_task=BackgroundTask(lambda: cleaned.append("unpinned")))
    return app


def _live_entry(tmp_path, first: bytes) -> dict:
    p = tmp_path / "grow.wav"
    p.write_bytes(stream._streaming_wav_header(44100, 2, 16) + first)
    return {"path": p, "expected_size": 0, "complete": asyncio.Event(),
            "data": asyncio.Event(), "clean_exit": None,
            "bytes": len(first), "no_pad_on_failure": True, "subscribers": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("clean", [False, True])
async def test_unknown_length_stream_aborts_when_the_render_fails(tmp_path, clean):
    import httpx
    live = _live_entry(tmp_path, b"\x01" * 100_000)
    cleaned: list = []
    app = _growing_app(tmp_path, live, cleaned)

    async def finish():
        await asyncio.sleep(0.2)
        with open(live["path"], "ab") as f:
            f.write(b"\x02" * 50_000)
        live["clean_exit"] = clean
        live["complete"].set()
        live["data"].set()
    fin = asyncio.ensure_future(finish())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        if clean:
            r = await c.get("/g")
            assert r.status_code == 200
            assert len(r.content) == 44 + 150_000
        else:
            with pytest.raises(stream._RenderAborted):
                await c.get("/g")
    await fin
    assert cleaned == ["unpinned"]            # cleanup ran exactly once either way


# ── r4-ren-8: cold uade first byte waits on events, not 50 ms polls ───────

@pytest.mark.asyncio
async def test_await_uade_live_wakes_on_registration_not_on_a_poll():
    key = "r4-reg__sub0"
    stream._UADE_LIVE.pop(key, None)
    entry = {"complete": asyncio.Event()}

    async def fake_render():
        await asyncio.sleep(0.01)
        stream._UADE_LIVE[key] = entry
        reg = stream._UADE_LIVE_REGISTERED.pop(key, None)
        if reg is not None:
            reg.set()
        await asyncio.sleep(5)
    task = asyncio.ensure_future(fake_render())
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        live = await stream._await_uade_live(key, task, timeout=2.0)
        assert live is entry
        assert loop.time() - t0 < 0.04          # not a 50 ms poll step
        assert key not in stream._UADE_LIVE_REGISTERED
    finally:
        task.cancel()
        stream._UADE_LIVE.pop(key, None)


@pytest.mark.skipif(not shutil.which("uade123"), reason="uade123 not installed")
@needs_conf
@pytest.mark.asyncio
async def test_rejected_module_is_still_an_error_on_a_cold_web_play(monkeypatch, tmp_path):
    import httpx
    junk = tmp_path / "mdat.notreally"
    junk.write_bytes(b"MZ" + bytes(range(256)) * 20)
    app = _app_with(monkeypatch, tmp_path, {"junk1": _T("junk1", junk, genre=["Amiga", "Module"])})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/api/stream/junk1", cookies={"sb_session": "x"})
    assert r.status_code in (422, 502), (r.status_code, r.headers.get("x-cache"))


# ── r4-ren-2: a SID prewarm retired for a progressive play ─────────────────

def _tmp_wav(tmp_path: Path, name: str) -> Path:
    return _write_wav(tmp_path / name, 0.1)


@pytest.mark.asyncio
async def test_waiter_on_a_retired_prewarm_render_is_not_a_502(monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    tid, key = "r4-sid-retire", conversion_cache._cache_key("r4-sid-retire", "sid", 0, duration=300)
    started = asyncio.Event()

    async def prewarm_render():
        stream._SID_PREWARM_RENDERS[key] = asyncio.current_task()
        started.set()
        await asyncio.sleep(30)
        return _tmp_wav(tmp_path, "never.wav")

    async def own_render():
        return _tmp_wav(tmp_path, "own.wav")

    pre = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id=tid, format_type="sid", subsong=0, duration=300, render_fn=prewarm_render))
    await started.wait()
    waiter = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id=tid, format_type="sid", subsong=0, duration=300, render_fn=own_render))
    await asyncio.sleep(0.05)
    assert await stream._retire_sid_prewarm(key) is True
    path, hit = await asyncio.wait_for(waiter, 5)
    assert path.exists() and hit is False
    with pytest.raises(asyncio.CancelledError):
        await pre


@pytest.mark.asyncio
async def test_waiter_on_a_failed_render_still_gets_the_failure(monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    gate = asyncio.Event()
    calls = []

    async def failing():
        calls.append("a")
        await gate.wait()
        raise stream.HTTPException(422, "bad tune")

    async def second():
        calls.append("b")
        return _tmp_wav(tmp_path, "b.wav")
    a = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id="r4-fail", format_type="tracker", subsong=0, render_fn=failing))
    await asyncio.sleep(0.01)
    b = asyncio.ensure_future(conversion_cache.get_or_render(
        track_id="r4-fail", format_type="tracker", subsong=0, render_fn=second))
    await asyncio.sleep(0.01)
    gate.set()
    with pytest.raises(stream.HTTPException):
        await a
    with pytest.raises(stream.HTTPException) as exc:
        await b
    assert exc.value.status_code == 502 and calls == ["a"]     # no second render


@pytest.mark.asyncio
async def test_waveform_follows_a_prewarm_into_the_progressive_render(monkeypatch, tmp_path):
    tid = "r4-wf-sid"
    key = conversion_cache._cache_key(tid, "sid", 0, None, duration=300)
    pre_ev = asyncio.Event()
    conversion_cache._inflight[key] = pre_ev
    state = {"cached": None}

    async def fake_get_cached(k):
        return state["cached"] if k == key else None
    monkeypatch.setattr(conversion_cache, "get_cached", fake_get_cached)
    try:
        wf = asyncio.ensure_future(tracks_api._waveform_from_conversion_cache(
            tid, "/x/tune.sid", ".sid", sid_duration=300, appear_wait=0.2, finish_wait=5))
        await asyncio.sleep(0.05)
        # the play retires the prewarm and renders progressively
        prog = asyncio.Event()
        stream._SID_PROG_DONE[key] = prog
        conversion_cache._inflight.pop(key)
        pre_ev.set()
        await asyncio.sleep(0.3)
        assert not wf.done(), "the waveform must wait for the progressive render"
        wav = _tmp_wav(tmp_path, "prog.wav")
        state["cached"] = wav
        prog.set()
        assert await asyncio.wait_for(wf, 3) == wav

        # still rendering when the time is up → pending (ask again), not blank
        prog2 = asyncio.Event()
        stream._SID_PROG_DONE[key] = prog2
        state["cached"] = None
        got = await tracks_api._waveform_from_conversion_cache(
            tid, "/x/tune.sid", ".sid", sid_duration=300, appear_wait=0.2, finish_wait=0.3)
        assert got is tracks_api._WAVEFORM_PENDING
    finally:
        conversion_cache._inflight.pop(key, None)
        stream._SID_PROG_DONE.pop(key, None)


# ── r4-ren-3: a play never queues behind a prewarm's download ──────────────

class _Entry:
    def __init__(self, name, path):
        self.name, self.path, self.is_dir = name, path, False


class _SlowScanSource:
    """Remote source whose scan-lane reads are slow (a busy share / a scan)."""

    def __init__(self, files: dict, scan_delay: float):
        self.files, self.scan_delay = files, scan_delay

    def stat(self, p):
        class S:
            size, mtime = len(self.files[p]), 1
        return S()

    def list_dir(self, d):
        return [_Entry(p.rsplit("/", 1)[1], p) for p in self.files if p.rsplit("/", 1)[0] == d]

    def read_file(self, p, lane="stream"):
        import time
        if lane == "scan":
            time.sleep(self.scan_delay)
        return self.files[p]


class _FakeCache:
    def __init__(self, src, tmp_path):
        self.src, self.dir = src, tmp_path / "rc"
        self.dir.mkdir()

    def fetch(self, scan_root, remote_path, source, lane="stream"):
        out = self.dir / remote_path.replace("/", "_")
        out.write_bytes(self.src.read_file(remote_path, lane=lane))
        return out


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["uade", "adlib"])
async def test_play_does_not_wait_behind_a_prewarm_materialize(monkeypatch, tmp_path, kind):
    from soniqboom.core import remote_cache
    if kind == "uade":
        files = {"/m/mdat.song": b"M" * 100, "/m/smpl.song": b"S" * 100}
        tune = "/m/mdat.song"
    else:
        files = {"/m/a/tune.rol": b"R" * 100, "/m/a/standard.bnk": b"B" * 100}
        tune = "/m/a/tune.rol"
    src = _SlowScanSource(files, scan_delay=1.0)
    fake = _FakeCache(src, tmp_path)
    monkeypatch.setattr(remote_cache, "get_cache", lambda: fake)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    monkeypatch.setattr(stream, "_register_adlib_extract", lambda *a, **k: None)
    monkeypatch.setattr(stream, "_zip_evict_until_under_budget", lambda *a, **k: None)
    if kind == "uade" and not uade_formats.player_map():
        pytest.skip("uade conf missing (companion names)")

    def run(lane):
        if kind == "uade":
            return stream._materialize_loose_remote_uade("r4mat", "share", tune, src, lane=lane)
        return stream._materialize_loose_remote_adlib("r4mat", "share", tune, src,
                                                      ["*.bnk"], lane=lane)
    loop = asyncio.get_running_loop()
    pre = asyncio.ensure_future(run("scan"))
    await asyncio.sleep(0.1)
    t0 = loop.time()
    got = await run("stream")
    took = loop.time() - t0
    assert took < 0.6, f"the play waited {took:.2f}s behind the prewarm"
    assert got is not None and got.read_bytes() == files[tune]
    sib = [n for n in files if n != tune][0].rsplit("/", 1)[1]
    assert (got.parent / sib).exists()
    assert (await pre) == got              # the prewarm finds it installed


def _remote_cache_mod():
    from soniqboom.core import remote_cache
    return remote_cache


@pytest.mark.skipif(not hasattr(_remote_cache_mod(), "_STREAM_LOCK_WAIT_S"),
                    reason="remote_cache without the stream-lane bounded wait")
def test_stream_fetch_does_not_queue_behind_a_scan_fetch_of_the_same_file(tmp_path):
    import threading
    import time
    rc = _remote_cache_mod()

    class Src:
        def read_file(self, p, lane="stream"):
            time.sleep(2.0 if lane == "scan" else 0.05)
            return b"x" * 1000
    cache = rc.RemoteCache(tmp_path / "rc", max_mb=100)
    res = {}
    th = threading.Thread(target=lambda: res.setdefault(
        "scan", cache.fetch("s", "/a/b.mod", Src(), lane="scan")))
    th.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    p = cache.fetch("s", "/a/b.mod", Src(), lane="stream")
    assert time.monotonic() - t0 < 1.0
    th.join()
    assert p == res["scan"] and p.read_bytes() == b"x" * 1000
    assert len(cache._index) == 1 and cache._total_bytes == 1000
    assert not list((tmp_path / "rc").rglob("*.part"))


@needs_conf
@pytest.mark.asyncio
@pytest.mark.parametrize("name,uade,want", [
    ("P10.mp3", None, False),          # a uade name token over an MP3
    ("star.amd", None, False),         # AdLib wins (goes to its bank path)
    ("mdat.song", None, True),
    ("mdat.song", False, False),       # the caller's verdict wins
])
async def test_probe_resolver_follows_the_render_routing(monkeypatch, tmp_path, name, uade, want):
    from soniqboom.core import filesource, remote_cache
    monkeypatch.setattr(filesource, "parse_remote_path", lambda p: ("share", "/m/" + name))
    monkeypatch.setattr(filesource, "get_source", lambda sr: object())
    fetched = tmp_path / name
    fetched.write_bytes(b"x")

    class Cache:
        def fetch(self, *a, **k):
            return fetched
    monkeypatch.setattr(remote_cache, "get_cache", lambda: Cache())
    calls: list[str] = []

    async def mat_uade(*a, **k):
        calls.append("uade")
        return fetched

    async def mat_adlib(*a, **k):
        calls.append("adlib")
        return None
    monkeypatch.setattr(stream, "_materialize_loose_remote_uade", mat_uade)
    monkeypatch.setattr(stream, "_materialize_loose_remote_adlib", mat_adlib)
    kw = {} if uade is None else {"uade": uade}
    got = await stream._resolve_adlib_local_path("rp1", f"ftp://h/share:/m/{name}",
                                                 lane="scan", **kw)
    assert got == fetched
    assert ("uade" in calls) is want, calls


# ── r4-ren-5: "Prepare upcoming tracks" gates rendered formats only ────────

def _prewarm_env(monkeypatch, tr, *, enabled: bool):
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: enabled)
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_prewarm_tasks", stream._OrderedDict())
    monkeypatch.setattr(stream, "_prewarm_owner", {})

    async def get_track(tid):
        return tr if tid == tr.id else None
    monkeypatch.setattr(stream, "get_track", get_track)
    ran: list[str] = []

    async def fake_do_prewarm(tid, track, subsong, prio=0):
        ran.append(tid)
    monkeypatch.setattr(stream, "_do_prewarm", fake_do_prewarm)
    return ran


async def _call_prewarm(tid):
    return await stream.prewarm(tid, subsong=0, priority="next", file_path=None, pw=None,
                                sb_session=None, u=None, p=None, s=None, t=None,
                                request=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expect", [
    ("a.aiff", "queued"), ("b.dsf", "queued"), ("c.wv", "queued"), ("d.m4a", "queued"),
    ("e.sid", "skipped"), ("f.mod", "skipped"), ("g.nsf", "skipped"), ("h.mid", "skipped"),
    pytest.param("mdat.song", "skipped", marks=needs_conf),
])
async def test_prewarm_off_still_prepares_conversions(monkeypatch, name, expect):
    tr = _T("pw-" + name, f"/x/{name}")
    ran = _prewarm_env(monkeypatch, tr, enabled=False)
    res = await _call_prewarm(tr.id)
    assert res["status"] == expect, res
    await asyncio.sleep(0)
    assert ran == ([tr.id] if expect == "queued" else [])
    # with the setting on, every non-native format is prepared
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: True)
    assert (await _call_prewarm(tr.id))["status"] in ("queued", "already_running")


@pytest.mark.asyncio
@pytest.mark.parametrize("dreamcast", [True, False])
async def test_prewarm_off_skips_a_dreamcast_dsf_but_converts_a_dsd_one(monkeypatch, tmp_path,
                                                                       dreamcast):
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: False)
    monkeypatch.setattr(stream, "_dsf_is_dreamcast", lambda p: dreamcast)
    src = tmp_path / "x.dsf"
    src.write_bytes(b"DSD ")
    tr = _T("dsf1", src)

    async def get_track(tid):
        return tr
    monkeypatch.setattr(stream, "get_track", get_track)
    seen: list[str] = []

    async def fake_get_or_render(**k):
        seen.append(k.get("format_type"))
        return src, True
    monkeypatch.setattr(conversion_cache, "get_or_render", fake_get_or_render)

    async def fake_inflight(**k):
        seen.append("dsd")
        return {"pump_task": asyncio.ensure_future(asyncio.sleep(0))}
    monkeypatch.setattr(stream, "_get_or_start_inflight_wav", fake_inflight)
    await stream._do_prewarm_render("dsf1", src, ".dsf", 0, track=tr)
    assert seen == ([] if dreamcast else ["dsd"])


@pytest.mark.asyncio
async def test_cast_lookahead_with_prewarm_off_still_converts_dsd(monkeypatch, tmp_path):
    import types
    from soniqboom.config import settings
    from soniqboom.core import cast_render, cast_session
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_prewarm_enabled", lambda: False)
    tracks = {"q0": _T("q0", "/x/q0.flac"), "q1": _T("q1", "/x/q1.mod"),
              "q2": _T("q2", "/x/q2.dsf"), "q3": _T("q3", "/x/q3.sid")}

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(cast_session, "get_track", get_track)
    fetched, started = [], []

    async def materialize(path, track_id, *, lane="stream"):
        fetched.append(track_id)
        return Path(path)
    monkeypatch.setattr(cast_render, "materialize_source", materialize)

    async def gated(prio, ck, fmt, render_fn):
        started.append(fmt)
    monkeypatch.setattr(cast_session, "_start_gated_render", gated)
    sess = cast_session.CastSession(types.SimpleNamespace(protocol="dlna"))
    sess._caps = {"audio/mpeg", "audio/wav"}
    sess.state.queue = [cast_session.QueueItem(k) for k in tracks]
    monkeypatch.setattr(cast_session, "_PREWARM_WINDOW", 3)
    await sess._prewarm_lookahead("q0")
    assert fetched == ["q2"] and started == ["transcoded"]


# ── r4-ren-13: one scheduler for the web and Subsonic prewarms ─────────────

@pytest.mark.asyncio
async def test_schedule_prewarm_joins_owners_and_evicts_unpinned_first(monkeypatch):
    monkeypatch.setattr(stream, "_prewarm_tasks", stream._OrderedDict())
    monkeypatch.setattr(stream, "_prewarm_owner", {})
    monkeypatch.setattr(stream, "_PREWARM_CAP", 3)
    monkeypatch.setattr(conversion_cache, "_pin_refs", {"sp1__sub0": 1})
    hold = asyncio.Event()

    async def fake_do_prewarm(tid, track, subsong, prio=0):
        await hold.wait()
    monkeypatch.setattr(stream, "_do_prewarm", fake_do_prewarm)
    sched = stream._schedule_prewarm
    try:
        r = sched("sp1", _T("sp1", "/x/sp1.mod"), 0, stream.PRIO_AHEAD, "alice", ".mod")
        assert r == {"status": "queued", "key": "sp1::.mod::0", "in_flight": 1}
        r = sched("sp1", _T("sp1", "/x/sp1.mod"), 0, stream.PRIO_NEXT, "bob", ".mod")
        assert r == {"status": "already_running", "key": "sp1::.mod::0"}
        assert stream._prewarm_owner["sp1::.mod::0"] == {"alice", "bob"}
        # Subsonic's call shape: no ext — derived from the path
        r = sched("sp2", _T("sp2", "/x/sp2.xm"), 0, stream.PRIO_AHEAD, "carol")
        assert r["key"] == "sp2::.xm::0"
        t2 = stream._prewarm_tasks["sp2::.xm::0"]
        sched("sp3", _T("sp3", "/x/sp3.it"), 0, stream.PRIO_AHEAD, "carol")
        r = sched("sp4", _T("sp4", "/x/sp4.s3m"), 0, stream.PRIO_AHEAD, "carol")
        assert r["in_flight"] == 3
        await asyncio.sleep(0)
        # over the cap: the oldest UNPINNED task goes, the pinned sp1 stays
        assert t2.cancelled() and "sp2::.xm::0" not in stream._prewarm_owner
        assert list(stream._prewarm_tasks) == ["sp1::.mod::0", "sp3::.it::0", "sp4::.s3m::0"]
    finally:
        hold.set()
        for t in list(stream._prewarm_tasks.values()):
            t.cancel()
        await asyncio.sleep(0)


# ── r4-ren-12: a multi-tune file's start song is written back once ─────────

def _psid(path: Path, count: int, start: int) -> Path:
    path.write_bytes(b"PSID\x00\x02\x00\x7c" + b"\x00" * 6
                     + count.to_bytes(2, "big") + start.to_bytes(2, "big") + b"\x00" * 100)
    return path


class _RecStore:
    def __init__(self, tracks):
        self.tracks, self.batches = tracks, []

    def get_track(self, tid):
        return self.tracks.get(tid)

    def update_track_fields_batch(self, items):
        self.batches.append(list(items))
        for tid, f in items:
            self.tracks[tid].update(f)
        return len(items)


@pytest.mark.asyncio
async def test_sid_start_song_is_written_back_to_the_record_in_one_batch(monkeypatch, tmp_path):
    from soniqboom.core import store as store_mod
    st = _RecStore({"ss1": {"id": "ss1", "subsongs": 5, "duration": 60,
                            "hvsc_lengths": [60, 70, 80, 90, 100]},
                    "ss2": {"id": "ss2", "subsongs": 4},
                    "ss3": {"id": "ss3", "subsongs": 6},
                    "ss4": {"id": "ss4", "subsongs": 6, "start_subsong": 3},
                    "ss5": {"id": "ss5", "subsongs": 1}})
    monkeypatch.setattr(store_mod, "get_store", lambda: st)
    monkeypatch.setattr(stream, "_START_SONG_PENDING", {})
    monkeypatch.setattr(stream, "_start_song_flush", None)
    for k in ("ss1", "ss2", "ss3", "ss4", "ss5"):
        stream._SID_START_SONG.pop(k, None)
    try:
        assert await stream.sid_start_song("ss1", _T("ss1", "x"), _psid(tmp_path / "a.sid", 5, 3)) == 3
        assert await stream.sid_start_song("ss2", _T("ss2", "x"), _psid(tmp_path / "b.sid", 4, 2)) == 2
        # start song 1 is the default: nothing to write
        assert await stream.sid_start_song("ss3", _T("ss3", "x"), _psid(tmp_path / "c.sid", 6, 1)) == 1
        # a record that already carries one is never rewritten
        stream._persist_start_song("ss4", 5, 6)
        stream._persist_start_song("ss1", 3, 5)              # a repeat: no second entry
        # a record the scan saw as single-tune (or with fewer tunes) is left alone
        stream._persist_start_song("ss5", 2, 3)
        stream._persist_start_song("ss3", 6, 7)          # the record has 6 tunes: ok
        stream._START_SONG_PENDING.pop("ss3")
        stream._persist_start_song("ss3", 7, 7)          # tune 7 of a 6-tune record: no
        assert stream._START_SONG_PENDING == {
            "ss1": {"start_subsong": 2, "duration": 80}, "ss2": {"start_subsong": 1}}
        assert st.batches == []                              # batched, not per play
        handle = stream._start_song_flush
        assert handle is not None
        handle.cancel()
        stream._flush_start_songs()
        assert st.batches == [[("ss1", {"start_subsong": 2, "duration": 80}),
                               ("ss2", {"start_subsong": 1})]]
        assert st.tracks["ss4"]["start_subsong"] == 3 and "start_subsong" not in st.tracks["ss3"]
        # out-of-range start songs are ignored
        stream._persist_start_song("ss3", 9, 6)
        stream._persist_start_song("ss3", 2, 1)
        assert stream._START_SONG_PENDING == {}
    finally:
        h = stream._start_song_flush
        if h is not None:
            h.cancel()
        for k in ("ss1", "ss2", "ss3", "ss4"):
            stream._SID_START_SONG.pop(k, None)


# ── r4-ren-11: the waveform decode streams instead of buffering ────────────

def _smooth_pcm(n: int) -> bytes:
    import math
    import struct
    return struct.pack(f"<{n}f", *(
        math.sin(i * 0.05) * (0.1 + 0.9 * abs(math.sin(i / 22050 * 0.2))) for i in range(n)))


def _feed_odd(acc, raw: bytes) -> None:
    sizes, i, k = (65537, 3, 131071, 250001), 0, 0
    while i < len(raw):
        acc.feed(raw[i:i + sizes[k % 4]])
        i += sizes[k % 4]
        k += 1


def test_streamed_waveform_matches_the_buffered_one(monkeypatch):
    from soniqboom.core.scanner import _pcm_to_waveform
    monkeypatch.setattr(tracks_api, "_WAVEFORM_EXACT_BYTES", 256 * 1024)
    raw = _smooth_pcm(22050 * 30 + 777)                   # a partial tail block
    ref = _pcm_to_waveform(raw, 200)
    acc = tracks_api._WaveformAccumulator(200)
    _feed_odd(acc, raw)
    got = acc.result()
    assert acc._streaming
    assert len(got["peaks"]) == len(got["rms"]) == 200
    assert max(abs(a - b) for a, b in zip(ref["peaks"], got["peaks"])) < 0.02
    assert max(abs(a - b) for a, b in zip(ref["rms"], got["rms"])) < 0.02
    # short decodes keep the exact whole-buffer reduction
    small = _smooth_pcm(22050 * 2)
    acc = tracks_api._WaveformAccumulator(200)
    _feed_odd(acc, small)
    assert not acc._streaming and acc.result() == _pcm_to_waveform(small, 200)


def test_waveform_accumulator_edge_cases():
    acc = tracks_api._WaveformAccumulator(200)
    assert acc.result() == [0.0] * 200                     # nothing decoded
    acc = tracks_api._WaveformAccumulator(200)
    acc.feed(_smooth_pcm(50) + b"\x01\x02")                # fewer samples than points
    got = acc.result()
    assert got == [0.0] * 200 or len(got["peaks"]) == 200


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.asyncio
async def test_waveform_decode_streams_a_real_file(monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core.scanner import _pcm_to_waveform
    monkeypatch.setattr(settings, "ffmpeg_path", shutil.which("ffmpeg"))
    monkeypatch.setattr(tracks_api, "_WAVEFORM_EXACT_BYTES", 512 * 1024)
    src = tmp_path / "long.wav"
    with wave.open(str(src), "wb") as w:
        import math
        import struct
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(struct.pack(f"<{22050 * 40}h", *(
            int(12000 * math.sin(i * 0.05) * (0.1 + 0.9 * abs(math.sin(i / 22050 * 0.2))))
            for i in range(22050 * 40))))
    proc = await asyncio.create_subprocess_exec(
        settings.ffmpeg_path, "-i", str(src), "-ac", "1", "-ar", "22050", "-f", "f32le", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    raw, _ = await proc.communicate()
    ref = _pcm_to_waveform(raw, 200)
    got = await tracks_api._compute_waveform_safe(str(src))
    assert max(abs(a - b) for a, b in zip(ref["peaks"], got["peaks"])) < 0.02
    assert max(abs(a - b) for a, b in zip(ref["rms"], got["rms"])) < 0.02


@pytest.mark.asyncio
async def test_a_hung_waveform_decode_is_killed_and_blank(monkeypatch, tmp_path):
    import time
    from soniqboom.config import settings
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "ffmpeg_path", str(fake))
    monkeypatch.setattr(tracks_api, "_WAVEFORM_DECODE_TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    got = await tracks_api._compute_waveform_safe(str(tmp_path / "x.flac"))
    assert got == [0.0] * 200
    assert time.monotonic() - t0 < 5


@pytest.mark.asyncio
async def test_a_player_crash_before_any_audio_has_listener_wording(monkeypatch, tmp_path):
    """uade exits 0 with only its WAV header ("score crashed"): the listener
    gets a plain reason, not "uade123 produced no audio"."""
    from soniqboom.config import settings
    fake = tmp_path / "uade123"
    fake.write_text(
        "#!/bin/sh\n"
        "printf 'Amiga message: Exception: Division by zero\\nbad song end: score crashed\\n' >&2\n"
        "printf 'RIFF\\377\\377\\377\\377WAVEfmt " + "\\000" * 20
        + "data\\377\\377\\377\\377'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "uade123_path", str(fake))
    mod = tmp_path / "tune.ml"
    mod.write_bytes(b"\x00" * 64)
    with pytest.raises(stream.HTTPException) as exc:
        await stream._render_uade(mod, live_key="r4-crash__sub0")
    assert exc.value.status_code == 502
    assert exc.value.detail == "The Amiga player couldn't render this module."
    assert "r4-crash__sub0" not in stream._UADE_LIVE


def test_concurrent_stream_fetches_of_one_file_share_one_download(tmp_path):
    """The stream-lane bypass exists for BACKGROUND holders only: playback
    requests for the same file (GET + waveform + VU + Range) share a download,
    and at most one bypasses a scan holder."""
    import threading
    import time
    rc = _remote_cache_mod()

    def run(lanes):
        cache = rc.RemoteCache(tmp_path / f"rc{len(lanes)}{lanes[0]}", max_mb=100)
        reads = []

        class Src:
            def read_file(self, p, lane="stream"):
                reads.append(lane)
                time.sleep(1.0 if lane == "scan" else 0.6)
                return b"x" * 1000
        ths = []
        for ln in lanes:
            th = threading.Thread(target=cache.fetch, args=("s", "/a.mod", Src()), kwargs={"lane": ln})
            th.start()
            ths.append(th)
            time.sleep(0.05)
        for th in ths:
            th.join()
        return reads, cache
    reads, cache = run(["stream"] * 4)
    assert reads == ["stream"] and cache._total_bytes == 1000 and len(cache._index) == 1
    reads, cache = run(["scan", "stream", "stream", "stream"])
    assert sorted(reads) == ["scan", "stream"] and cache._total_bytes == 1000


def test_orphan_part_files_are_swept_at_startup(tmp_path):
    rc = _remote_cache_mod()
    root = tmp_path / "rc"
    (root / "ab").mkdir(parents=True)
    orphan = root / "ab" / "x.mod.deadbeef.part"
    orphan.write_bytes(b"half")
    rc.RemoteCache(root, max_mb=10)
    assert not orphan.exists()


def test_a_waiting_playback_fetch_is_served_as_soon_as_any_download_lands(tmp_path):
    """Scan fast (1 s), the stream bypass slow (3 s): a second playback
    request must return when the scan's copy lands, not after the bypass."""
    import threading
    import time
    rc = _remote_cache_mod()
    cache = rc.RemoteCache(tmp_path / "rc", max_mb=100)

    class Src:
        def read_file(self, p, lane="stream"):
            time.sleep(1.0 if lane == "scan" else 3.0)
            return b"x" * 1000
    done = {}
    t0 = time.monotonic()

    def go(name, lane):
        cache.fetch("s", "/a.mod", Src(), lane=lane)
        done[name] = time.monotonic() - t0
    ths = [threading.Thread(target=go, args=("scan", "scan"))]
    ths[0].start()
    time.sleep(0.05)
    for n in ("bypass", "waiter"):
        th = threading.Thread(target=go, args=(n, "stream"))
        th.start()
        ths.append(th)
        time.sleep(0.05)
    for th in ths:
        th.join()
    assert done["waiter"] < 1.6, done
    assert cache._total_bytes == 1000 and len(cache._index) == 1
