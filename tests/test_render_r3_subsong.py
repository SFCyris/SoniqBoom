# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-tune SID / SNDH / SC68 from review round 3: the renderers follow the
wire contract the web player and the Subsonic tune ids share — wire 0 is the
file's default tune s, wire s-1 is tune 1 when s isn't, and every other wire
N is track N+1 (the renderers' track numbers are 1-based).  The render length
(and so the cache key) is the length of the tune the wire plays.  Renders
cached before the fix hold another tune under the same key: a one-time
startup sweep removes them with their VU sidecars and transcodes, and client
(WASM) uploads for a subsong must say which tune they rendered.

In-process only; the real-audio checks use the local fixtures under
internal/testdata and skip when the renderer is not installed.
"""
from __future__ import annotations

import array
import asyncio
import math
import shutil
import subprocess
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream
from soniqboom.api import tracks as tracks_api
from soniqboom.core import conversion_cache

REPO = Path(__file__).resolve().parent.parent
SID = REPO / "internal/testdata/sid/SX-64_Demo.sid"                   # 4 tunes
SNDH = REPO / "internal/testdata/atari/sndh/Barker_Christopher__Spirit_of_Excalibur.sndh"


# ── argv: wire index → 1-based track ───────────────────────────────────────

def test_sid_argv_maps_the_wire_index_to_the_1_based_start_song():
    def argv(ss):
        return stream._sid_render_cmd("sidplayfp", Path("/x.sid"), ss, 30, "/o.wav")
    assert not any(a.startswith("-o") for a in argv(0))       # header start song
    assert "-o2" in argv(1) and "-o1" not in argv(1)
    assert "-o4" in argv(3)
    # the VU isolation passes use the same mapping
    muted = stream._sid_render_cmd("sidplayfp", Path("/x.sid"), 2, 30, "/o.wav",
                                   mute=(2, 3))
    assert "-o3" in muted and "-u2" in muted and "-u3" in muted


def test_wire_mapping_swaps_tune_1_with_a_later_default():
    """The shared wire contract (utils.js subsongWireToTune, subsonic_index
    .wire_tune): wire 0 is the default tune s, wire s-1 is tune 1, every other
    wire w is tune w+1; a start song outside 1..count counts as 1."""
    def js(w, s):                                   # utils.js, 1-based start
        if w == 0:
            return s
        if s != 1 and w == s - 1:
            return 1
        return w + 1
    for n in range(1, 9):
        for s in range(1, n + 1):
            tunes = [stream.sid_wire_tune(w, s, n) for w in range(n)]
            assert tunes == [js(w, s) for w in range(n)]
            assert sorted(tunes) == list(range(1, n + 1))       # one wire per tune
    assert stream.sid_wire_tune(0, 9, 4) == 1 and stream.sid_wire_tune(3, 0, 4) == 4
    assert stream.sid_wire_tune(2, 3) == 1                      # count unknown
    # …and exactly what the Subsonic tune ids send (0-based start_subsong)
    from soniqboom.core import subsonic_index as sx
    for n in range(2, 9):
        for s0 in range(n):
            t = {"id": "x", "subsongs": n, "start_subsong": s0}
            assert [sx.wire_tune(t, w) + 1 for w in range(n)] == \
                [stream.sid_wire_tune(w, s0 + 1, n) for w in range(n)]
    # the argv: a file whose start song is 3
    def argv(ss):
        return stream._sid_render_cmd("sidplayfp", Path("/x.sid"), ss, 30, "/o.wav",
                                      start=3)
    assert not any(a.startswith("-o") for a in argv(0))       # header start song (3)
    assert "-o1" in argv(2) and "-o2" in argv(1) and "-o4" in argv(3)


def test_render_length_follows_the_tune_the_wire_plays(monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 180)
    stream._SID_START_SONG.pop("len1", None)
    t = types.SimpleNamespace(id="len1", hvsc_lengths=[100.0, 61.0, 42.0, 30.0],
                              subsongs=4, duration=100.0)
    # start song unknown → tune order = wire order (as before)
    assert [stream._sid_target_seconds(t, w) for w in range(4)] == [100, 61, 42, 30]
    # start song 3 read from the file → wire 0 = tune 3, wire 2 = tune 1
    stream._note_sid_start_song("len1", 3)
    try:
        assert [stream._sid_target_seconds(t, w) for w in range(4)] == [42, 61, 100, 30]
    finally:
        stream._SID_START_SONG.pop("len1", None)
    # a start song the scan recorded (0-based start_subsong) wins
    t.start_subsong = 1
    assert [stream._sid_target_seconds(t, w) for w in range(4)] == [61, 100, 42, 30]


@pytest.mark.asyncio
async def test_sndh_wire_index_selects_the_next_track_and_its_time(monkeypatch):
    seen = []
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "psgplay")
    monkeypatch.setattr(stream, "_sndh_info",
                        lambda p: (3, {1: 30, 2: 40, 3: 50, 4: 60}, 4))

    async def fake_await(cmd, tmp, *, timeout, kind):
        seen.append(cmd)
    monkeypatch.setattr(stream, "_await_renderer", fake_await)
    for ss in (0, 1, 2, 3):
        out = await stream._render_sndh(Path("/x.sndh"), subsong=ss)
        out.unlink(missing_ok=True)
    tracks = [c[c.index("-t") + 1] for c in seen]
    lengths = [next(a for a in c if a.startswith("--length=")) for c in seen]
    # 0 = the !# default track; wire 2 (= default - 1) is track 1
    assert tracks == ["3", "2", "1", "4"]
    assert lengths == ["--length=50", "--length=40", "--length=30", "--length=60"]


@pytest.mark.asyncio
async def test_sc68_wire_index_selects_the_next_track(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "sc68")
    monkeypatch.setattr(stream, "_sc68_home", lambda: tmp_path)

    class _P:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        seen.append(argv)
        return _P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    for ss in (0, 1, 4):
        with pytest.raises(HTTPException):        # no PCM from the fake → 422
            await stream._render_sc68(Path("/x.sc68"), subsong=ss)
    assert [a[2] for a in seen] == ["--track=1", "--track=2", "--track=5"]


# ── real renders ───────────────────────────────────────────────────────────

def _envelope(data: bytes, win: int = 2205) -> list[float]:
    a = array.array("h")
    body = data[44:]
    a.frombytes(body[:len(body) // 2 * 2])
    return [sum(abs(x) for x in a[i:i + win]) / win for i in range(0, len(a) - win, win)]


def _corr(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("sidplayfp") or not SID.exists(),
                    reason="sidplayfp or the SID fixture is not available")
async def test_sid_wire_1_renders_tune_2(tmp_path):
    # sidplayfp's power-on delay is random, so compare loudness envelopes (the
    # same tune correlates ~1.0, a different one ≤ 0.7 on this file).
    def ref(o: str) -> list[float]:
        out = tmp_path / f"ref{o}.wav"
        subprocess.run(["sidplayfp", o, "-f44100", "-t4", f"-w{out}", str(SID)],
                       check=True, capture_output=True, timeout=60)
        return _envelope(out.read_bytes())
    refs = {n: ref(f"-o{n}") for n in (1, 2, 3)}
    for wire, tune in ((0, 1), (1, 2), (2, 3)):
        wav = await stream._render_sid(SID, subsong=wire, duration=4)
        try:
            env = _envelope(wav.read_bytes())
        finally:
            wav.unlink(missing_ok=True)
        scores = {n: _corr(env, r) for n, r in refs.items()}
        assert max(scores, key=scores.get) == tune, (wire, scores)
        assert scores[tune] > 0.9, (wire, scores)


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("psgplay") or not SNDH.exists(),
                    reason="psgplay or the SNDH fixture is not available")
async def test_sndh_wire_1_renders_track_2_bit_exactly(monkeypatch, tmp_path):
    monkeypatch.setattr(stream, "_ATARI_DEFAULT_S", 3)
    ref = tmp_path / "t2.wav"
    subprocess.run(["psgplay", "-t", "2", "-f", "44100", "--length=3", "-o", str(ref),
                    str(SNDH)], check=True, capture_output=True, timeout=60)
    wav = await stream._render_sndh(SNDH, subsong=1)
    try:
        assert wav.read_bytes() == ref.read_bytes()
    finally:
        wav.unlink(missing_ok=True)


def _sid_with_start(tmp_path: Path, start: int) -> Path:
    """A copy of the 4-tune fixture whose PSID header names ``start`` as the
    default tune."""
    data = bytearray(SID.read_bytes())
    data[0x10:0x12] = start.to_bytes(2, "big")
    out = tmp_path / f"start{start}.sid"
    out.write_bytes(bytes(data))
    return out


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("sidplayfp") or not SID.exists(),
                    reason="sidplayfp or the SID fixture is not available")
async def test_sid_with_a_later_default_plays_tune_1_on_its_swapped_wire(tmp_path):
    src = _sid_with_start(tmp_path, 3)

    def ref(n: int) -> list[float]:
        out = tmp_path / f"ref{n}.wav"
        subprocess.run(["sidplayfp", f"-o{n}", "-f44100", "-t4", f"-w{out}", str(SID)],
                       check=True, capture_output=True, timeout=60)
        return _envelope(out.read_bytes())
    refs = {n: ref(n) for n in (1, 2, 3, 4)}
    for wire, tune in ((0, 3), (2, 1), (1, 2), (3, 4)):
        wav = await stream._render_sid(src, subsong=wire, duration=4)
        try:
            env = _envelope(wav.read_bytes())
        finally:
            wav.unlink(missing_ok=True)
        scores = {n: _corr(env, r) for n, r in refs.items()}
        assert max(scores, key=scores.get) == tune, (wire, scores)
        assert scores[tune] > 0.9, (wire, scores)


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("sidplayfp") or not SID.exists(),
                    reason="sidplayfp or the SID fixture is not available")
async def test_progressive_web_play_takes_the_swapped_wire_too(monkeypatch, tmp_path):
    from test_render_r1_stream import _T, _app, _client
    src = _sid_with_start(tmp_path, 3)
    t = _T("pg3", src, 5.0, fmt="SID", hvsc_lengths=[5.0, 5.0, 5.0, 5.0], subsongs=4)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    app, _ = _app(monkeypatch, tmp_path, {"pg3": t})

    def ref(n: int) -> list[float]:
        out = tmp_path / f"pref{n}.wav"
        subprocess.run(["sidplayfp", f"-o{n}", "-f44100", "-t5", f"-w{out}", str(SID)],
                       check=True, capture_output=True, timeout=60)
        return _envelope(out.read_bytes())
    refs = {n: ref(n) for n in (1, 2, 3, 4)}
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/pg3?subsong=2", cookies={"sb_session": "x"})
        assert r.status_code == 200 and r.headers["x-cache"] == "miss-progressive"
        scores = {n: _corr(_envelope(r.content), e) for n, e in refs.items()}
        assert max(scores, key=scores.get) == 1 and scores[1] > 0.9, scores
    finally:
        stream._SID_START_SONG.pop("pg3", None)
        for _ in range(40):                     # let the finaliser promote / drop it
            if not stream._SID_PROG_INFLIGHT:
                break
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.skipif(not SID.exists(), reason="SID fixture missing")
async def test_vu_isolation_passes_render_the_swapped_tune(monkeypatch, tmp_path):
    src = _sid_with_start(tmp_path, 3)
    seen = []
    monkeypatch.setattr(stream, "_find_renderer", lambda *a: "sidplayfp")

    async def fake_await(cmd, tmp, *, timeout, kind):
        seen.append(cmd)
    monkeypatch.setattr(stream, "_await_renderer", fake_await)
    await stream._sid_vu_worker("vukey", tmp_path / "x.wav", src, 2, 5)
    assert len(seen) == 3 and all("-o1" in c for c in seen), seen


CROWE = SNDH.parent / "Crowe_Mark__Space_Quest_Chapter_1.sndh"   # !# 10 of 46


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("psgplay") or not CROWE.exists(),
                    reason="psgplay or the SNDH fixture is not available")
async def test_sndh_with_a_later_default_plays_track_1_on_its_swapped_wire(tmp_path):
    ref = tmp_path / "t1.wav"
    subprocess.run(["psgplay", "-t", "1", "-f", "44100", "--length=1", "-o", str(ref),
                    str(CROWE)], check=True, capture_output=True, timeout=60)
    wav = await stream._render_sndh(CROWE, subsong=9)        # default 10 → wire 9
    try:
        assert wav.read_bytes() == ref.read_bytes()
    finally:
        wav.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("sidplayfp") or not SID.exists(),
                    reason="sidplayfp or the SID fixture is not available")
async def test_play_learns_the_start_song_and_status_agrees(monkeypatch, tmp_path):
    """A play reads the file's start song, so its render length is the default
    tune's own — and the O(1) /render-status then finds the same cache key."""
    from test_render_r1_stream import _T, _app, _client
    src = _sid_with_start(tmp_path, 3)
    t = _T("st3", src, 5.0, fmt="SID", hvsc_lengths=[5.0, 6.0, 7.0, 8.0], subsongs=4)
    stream._SID_START_SONG.pop("st3", None)
    app, _ = _app(monkeypatch, tmp_path, {"st3": t})
    try:
        async with _client(app) as c:
            r = await c.get("/api/stream/st3")                  # blocking (no session)
            assert r.status_code == 200
            assert r.headers["x-sid-target-seconds"] == "7"     # tune 3's own length
            st = (await c.get("/api/stream/st3/render-status")).json()
            assert st["target_seconds"] == 7 and st["ready"] is True
            st2 = (await c.get("/api/stream/st3/render-status?subsong=2")).json()
            assert st2["target_seconds"] == 5                   # wire 2 = tune 1
    finally:
        stream._SID_START_SONG.pop("st3", None)


@pytest.mark.asyncio
async def test_track_info_names_the_default_tune(monkeypatch, tmp_path):
    src = _sid_with_start(tmp_path, 3) if SID.exists() else None
    tracks = {
        "one": types.SimpleNamespace(id="one", path="/x.sid", format="SID", subsongs=1),
        "rec": types.SimpleNamespace(id="rec", path="ftp://h/x.sid", format="SID",
                                     subsongs=4, start_subsong=1),
        "far": types.SimpleNamespace(id="far", path="ftp://h/y.sid", format="SID",
                                     subsongs=4),
    }
    if src is not None:
        tracks["loc"] = types.SimpleNamespace(id="loc", path=str(src), format="SID",
                                              subsongs=4)
    for k in tracks:
        stream._SID_START_SONG.pop(k, None)
    try:
        assert await tracks_api._default_track("one", tracks["one"]) is None
        assert await tracks_api._default_track("rec", tracks["rec"]) == 2
        assert await tracks_api._default_track("far", tracks["far"]) is None   # not local
        if src is not None:
            assert await tracks_api._default_track("loc", tracks["loc"]) == 3
            # …remembered for the O(1) paths (render length, upload gate)
            assert stream.sid_start_song_known("loc", None) == 3
        if shutil.which("psgplay") and CROWE.exists():
            sndh = types.SimpleNamespace(id="sq", path=str(CROWE), format="SNDH",
                                         subsongs=46)
            assert await tracks_api._default_track("sq", sndh) == 10
    finally:
        for k in tracks:
            stream._SID_START_SONG.pop(k, None)


# ── the one-time sweep of pre-fix renders ──────────────────────────────────

def test_startup_sweeps_pre_fix_multi_tune_renders_once(monkeypatch, tmp_path):
    from soniqboom.config import settings
    base = tmp_path / "conv"
    monkeypatch.setattr(settings, "conversion_cache_dir", str(base))
    ck = conversion_cache._cache_key

    def put(fmt: str, key: str, vu: bool = False) -> Path:
        p = conversion_cache._cache_path(key, fmt)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF" + b"\0" * 60)
        if vu:
            p.with_suffix(".vu").write_bytes(b"VUMR")
        return p
    stale = [
        put("sid", ck("sw1", "sid", 1, duration=39), vu=True),
        put("sid", ck("sw1", "sid", 3, duration=22)),
        put("sndh", ck("sw2", "sndh", 2)),
        put("sc68", ck("sw3", "sc68", 1)),
    ]
    orphan_vu = conversion_cache._cache_path(ck("sw4", "sid", 2, duration=60), "sid") \
        .with_suffix(".vu")
    orphan_vu.parent.mkdir(parents=True, exist_ok=True)
    orphan_vu.write_bytes(b"VUMR")
    stale_tc = put("transcoded", ck("sw1", "transcoded", 0, codec="mp3", bitrate=128,
                                    variant=conversion_cache.rendered_transcode_variant(
                                        ck("sw1", "sid", 1, duration=39))))
    keep = [
        put("sid", ck("sw1", "sid", 0, duration=55), vu=True),     # default tune
        put("sndh", ck("sw2", "sndh", 0)),
        put("uade", ck("sw5", "uade", 2)),                          # uade was right
        put("gme", ck("sw6", "gme", 3)),
        put("transcoded", ck("sw1", "transcoded", 0, codec="mp3", bitrate=128,
                             variant=conversion_cache.rendered_transcode_variant(
                                 ck("sw1", "sid", 0, duration=55)))),
    ]
    added: list[str] = []
    try:
        conversion_cache.warmup_from_disk()
        added = [p.stem for p in keep]
        for p in stale + [stale_tc]:
            assert not p.exists(), p
            assert p.stem not in conversion_cache._meta
        assert not stale[0].with_suffix(".vu").exists() and not orphan_vu.exists()
        for p in keep:
            assert p.exists() and p.stem in conversion_cache._meta, p
        assert keep[0].with_suffix(".vu").exists()
        assert (base / conversion_cache._SUBSONG_BASE_MARKER).exists()
        # renders made after the fix survive every later start
        fresh = put("sid", ck("sw1", "sid", 1, duration=39))
        added.append(fresh.stem)
        conversion_cache.warmup_from_disk()
        assert fresh.exists() and fresh.stem in conversion_cache._meta
    finally:
        with conversion_cache._state_lock:
            for k in added:
                e = conversion_cache._meta.pop(k, None)
                conversion_cache._lru.pop(k, None)
                if e:
                    conversion_cache._total_bytes -= e["size_bytes"]


# ── client (WASM) uploads must name the tune they rendered ─────────────────

class _Req:
    def __init__(self, body: bytes = b"junk"):
        self.headers = {"content-length": str(len(body))}
        self._body = body

    async def stream(self):
        yield self._body


@pytest.mark.asyncio
async def test_client_vu_upload_for_a_subsong_needs_the_tune(monkeypatch):
    kw = dict(content_hash="0" * 64, user=None)
    for tune in (None, 1):                     # old worker: rendered the previous tune
        with pytest.raises(HTTPException) as e:
            await tracks_api.upload_vu_sidecar("t", _Req(), subsong=1, tune=tune, **kw)
        assert e.value.status_code == 409
    # the right tune (and the default tune, which needs none) pass the gate —
    # and fail later on the junk body instead
    for ss, tune in ((1, 2), (0, None)):
        with pytest.raises(HTTPException) as e:
            await tracks_api.upload_vu_sidecar("t", _Req(), subsong=ss, tune=tune, **kw)
        assert e.value.status_code == 422
    # a file whose default is tune 3: wire 2 is tune 1 on both sides
    stream._note_sid_start_song("t3", 3)
    try:
        with pytest.raises(HTTPException) as e:
            await tracks_api.upload_vu_sidecar("t3", _Req(), subsong=2, tune=3, **kw)
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:
            await tracks_api.upload_vu_sidecar("t3", _Req(), subsong=2, tune=1, **kw)
        assert e.value.status_code == 422
    finally:
        stream._SID_START_SONG.pop("t3", None)


@pytest.mark.asyncio
async def test_client_sid_audio_upload_for_a_subsong_needs_the_tune(monkeypatch):
    async def get_track(tid):
        return None
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    kw = dict(duration=30, wav_sha256="0" * 64, user=None)
    with pytest.raises(HTTPException) as e:
        await tracks_api.upload_sid_audio("t", _Req(), subsong=2, tune=2, **kw)
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        await tracks_api.upload_sid_audio("t", _Req(), subsong=2, tune=3, **kw)
    assert e.value.status_code == 404                  # past the gate: no such track
