# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fallbacks between renderers, and the renderer a play is named by.

* A Dreamcast rip dsfrender fails on (an error, no audio, silence) is
  rendered by zxtune123 when that is installed, cached under zxtune123's key
  and named zxtune123 (``stream._render_psf`` / ``_serve_psf``,
  ``dsf_render.note_failed``).
* A Musicline Editor tune whose uade probe couldn't decide is rendered by
  uade; a uade render that fails or comes out silent is rendered by mlrender
  (``stream._musicline_after_uade``).  A module's content hash is read once
  per path, size and modification time (``musicline.digest``).
* A tune index mlrender says the module doesn't have is no refusal of the
  file (``unplayable.classify``).
* ``X-Rendered`` / ``renderer_key_for`` name milkyrender for an early DSIK
  module and mlrender for a Musicline tune it plays.
"""
from __future__ import annotations

import asyncio
import importlib.util
import math
import os
import shutil
import struct
import sys
import zlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream
from soniqboom.core import dsf_render, musicline, unplayable

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("render_selftest",
                                               ROOT / "scripts/render_selftest.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def _psf(section: bytes, tags: str = "", version: int = 0x12) -> bytes:
    exe = zlib.compress(section)
    return (b"PSF" + bytes([version]) + struct.pack("<III", 0, len(exe), zlib.crc32(exe))
            + exe + (b"[TAG]" + tags.encode() if tags else b""))


def _wav(seconds: float, amp: int = 9000, rate: int = 44100) -> bytes:
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * 440 * i / rate)) if amp else 0
        frames += struct.pack("<hh", v, v)
    return stream._build_wav_header(rate, 2, len(frames), 16) + bytes(frames)


@pytest.fixture(autouse=True)
def _fresh():
    dsf_render.forget_failures()
    musicline.forget_all()
    saved = dict(stream._RENDERED_BY)
    stream._RENDERED_BY.clear()
    yield
    dsf_render.forget_failures()
    musicline.forget_all()
    stream._RENDERED_BY.clear()
    stream._RENDERED_BY.update(saved)


# ── Dreamcast: dsfrender → zxtune123 ─────────────────────────────────────────

def _dsf_tools(monkeypatch, tmp_path, *, zxtune=True):
    """dsfrender installed (``dsf_render.render`` faked by each test) and a
    zxtune123 whose renders are recorded (``_await_renderer`` faked: it
    writes a 1 s tone)."""
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: "/opt/zxtune123" if zxtune and name == "zxtune123"
                        else None)
    zx = []

    async def fake_await(cmd, out, **kw):
        zx.append(cmd)
        Path(out).write_bytes(_wav(1.0))
    monkeypatch.setattr(stream, "_await_renderer", fake_await)
    return zx


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["refusal", "silence"])
async def test_a_dsfrender_failure_is_rendered_by_zxtune(tmp_path, monkeypatch, how):
    rip = tmp_path / "a.minidsf"
    rip.write_bytes(_psf(b"\0" * 8))
    zx = _dsf_tools(monkeypatch, tmp_path)

    async def failing(binary, path, *, live_key=None, expected_seconds=0.0):
        unplayable.renderer_ran("dsfrender", "psf")
        if how == "refusal":
            raise unplayable.RenderRefusal(422, "x", renderer="dsfrender", why="y",
                                           diagnosed=True)
        out = tmp_path / "silent.wav"
        out.write_bytes(_wav(1.0, amp=0))
        return out
    monkeypatch.setattr(dsf_render, "render", failing)
    out = await stream._render_psf(rip)
    try:
        assert len(zx) == 1 and zx[0][0] == "/opt/zxtune123" and zx[0][-1] == str(rip)
        assert stream._wav_is_audible(out)
    finally:
        out.unlink(missing_ok=True)
    assert not (tmp_path / "silent.wav").exists()          # (the silent render is dropped)
    assert unplayable._ran.get() == ("zxtune123", "psf")  # (zxtune123's render, not dsfrender's)
    # routed to zxtune123 from now on, under its key and name
    assert dsf_render.binary_for(rip) is None
    assert dsf_render.cache_variant_for(rip) is None


@pytest.mark.asyncio
async def test_a_cached_render_hands_the_failure_back_unmarked(tmp_path, monkeypatch):
    rip = tmp_path / "a.minidsf"
    rip.write_bytes(_psf(b"\0" * 8))
    zx = _dsf_tools(monkeypatch, tmp_path)

    async def failing(*a, **k):
        raise unplayable.RenderRefusal(422, "x", renderer="dsfrender", why="y", diagnosed=True)
    monkeypatch.setattr(dsf_render, "render", failing)
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(rip, live_key="k", dsf_key=True)
    # a plain error: render_checked never verifies or marks it
    assert not isinstance(e.value, unplayable.RenderRefusal) and e.value.status_code == 422
    assert zx == [] and dsf_render.cache_variant_for(rip) is None
    # a render for dsfrender's key of a rip now routed to zxtune123: a plain
    # 503 (the caller renders under zxtune123's key) — never zxtune123's
    # audio under dsfrender's key
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(rip, live_key="k", dsf_key=True)
    assert e.value.status_code == 503 and zx == []


@pytest.mark.asyncio
async def test_a_render_for_zxtunes_key_never_runs_dsfrender(tmp_path, monkeypatch):
    rip = tmp_path / "a.minidsf"
    rip.write_bytes(_psf(b"\0" * 8))
    zx = _dsf_tools(monkeypatch, tmp_path)
    ran = []

    async def dsf(*a, **k):
        ran.append(a)
    monkeypatch.setattr(dsf_render, "render", dsf)
    out = await stream._render_psf(rip, dsf_key=False)
    out.unlink(missing_ok=True)
    assert ran == [] and len(zx) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [HTTPException(504, "DSF render timed out after 600s"),
                                 HTTPException(502, "DSF renderer was stopped")])
async def test_a_transient_dsfrender_failure_is_not_handed_to_zxtune(tmp_path, monkeypatch, exc):
    rip = tmp_path / "a.minidsf"
    rip.write_bytes(_psf(b"\0" * 8))
    zx = _dsf_tools(monkeypatch, tmp_path)

    async def failing(*a, **k):
        raise exc
    monkeypatch.setattr(dsf_render, "render", failing)
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(rip)
    assert e.value is exc and zx == []
    assert dsf_render.cache_variant_for(rip) == "dsf1"     # (dsfrender is tried again)


@pytest.mark.asyncio
async def test_without_zxtune_a_dsfrender_failure_stays_its_own(tmp_path, monkeypatch):
    rip = tmp_path / "a.minidsf"
    rip.write_bytes(_psf(b"\0" * 8))
    _dsf_tools(monkeypatch, tmp_path, zxtune=False)

    async def failing(*a, **k):
        raise unplayable.RenderRefusal(422, "x", renderer="dsfrender", why="y", diagnosed=True)
    monkeypatch.setattr(dsf_render, "render", failing)
    with pytest.raises(unplayable.RenderRefusal):
        await stream._render_psf(rip)
    assert dsf_render.cache_variant_for(rip) == "dsf1"     # (nothing to route it to)


_STUB = r'''import math, os, struct, sys
args = sys.argv[1:]
if "--version" in args:
    print("{name} 1.0"); sys.exit(0)
if {fails!r} and "bad" in os.path.basename(args[-2 if {name!r} == "dsfrender" else -1]):
    sys.stderr.write("dsfrender: not a Dreamcast DSF this build can play: x\n"); sys.exit(1)
out = args[-1] if {name!r} == "dsfrender" else [a for a in args if a.startswith("filename=")][0][9:]
rate, n = 44100, 44100
hz = {hz}
data = b"".join(struct.pack("<hh", v, v) for v in
                (int(8000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))
with open(out, "wb") as fh:
    fh.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
             + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
             + b"data" + struct.pack("<I", len(data)) + data)
'''


def _stub(tmp_path, name, *, fails, hz):
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    (tmp_path / "bin" / f"{name}.py").write_text(_STUB.format(name=name, fails=fails, hz=hz))
    # (a shell wrapper: the interpreter's path may hold spaces, a #! line can't)
    p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{p}.py" "$@"\n')
    p.chmod(0o755)
    return str(p)


def _pcm(wav: bytes) -> bytes:
    i = wav.find(b"data")
    return wav[i + 8:]


@pytest.mark.asyncio
async def test_the_stream_route_serves_zxtune_when_dsfrender_fails(monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core import conversion_cache
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(settings, "dsfrender_path", _stub(tmp_path, "dsfrender", fails=True, hz=440))
    monkeypatch.setattr(settings, "zxtune123_path", _stub(tmp_path, "zxtune123", fails=False,
                                                          hz=1000))
    bad = tmp_path / "bad.minidsf"
    bad.write_bytes(_psf(b"\0" * 8, "length=1\n"))
    good = tmp_path / "good.minidsf"
    good.write_bytes(_psf(b"\0" * 8, "length=1\n"))
    tracks = {"bad": _T("bad", bad, 1.0, fmt="DSF (Dreamcast)"),
              "good": _T("good", good, 1.0, fmt="DSF (Dreamcast)")}
    app, fs = _app(monkeypatch, tmp_path, tracks)
    monkeypatch.setattr(unplayable, "_proven", {})
    got = []
    async with _client(app) as c:
        for tid in ("bad", "good", "bad"):                  # cold (fallback), cold, warm
            r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            got.append((tid, r.headers["x-rendered"], r.headers["x-cache"], _pcm(r.content)))
            await asyncio.sleep(0.2)
    assert [g[:2] for g in got] == [("bad", "zxtune"), ("good", "dsfrender"), ("bad", "zxtune")]
    assert got[0][2].startswith("miss") and got[2][2] == "hit"
    assert got[0][3] == got[2][3] and got[0][3] != got[1][3]    # (1 kHz vs 440 Hz)
    keys = sorted(p.stem for p in (tmp_path / "conv" / "psf").rglob("*.wav"))
    assert keys == ["bad__r1", "good__r1__vdsf1"]         # each under its renderer's key
    assert conversion_cache.recent_failure("bad") is None  # (dsfrender's failure forgotten)
    assert all("defect" not in f for _, f in fs.updates)  # never marked
    assert stream.renderer_key_for({"id": "bad", "path": str(bad)}) == "zxtune123"
    assert stream.renderer_key_for({"id": "good", "path": str(good)}) == "dsfrender"


# ── Musicline: an undecided probe ────────────────────────────────────────────

def _ml_tools(monkeypatch, tmp_path, uade_result):
    """mlrender installed (its render faked), uade's probe undecided, uade's
    render ``uade_result`` ("tone", "silence" or an exception)."""
    real = stream._find_renderer
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: "/opt/mlrender" if name == "mlrender"
                        else real(conf, name))
    probes, ran = [], []

    async def probe(src, index, base=0):
        probes.append(index)
        return None                                     # (it timed out)

    async def fake_uade(src, *args, **kwargs):
        ran.append(("uade", kwargs.get("live_key")))
        if isinstance(uade_result, BaseException):
            raise uade_result
        out = tmp_path / "uade.wav"
        out.write_bytes(_wav(1.0, amp=9000 if uade_result == "tone" else 0))
        return out

    async def fake_ml(binary, src, subsong=0, *, live_key=None, expected_seconds=0.0):
        ran.append(("mlrender", live_key))
        out = tmp_path / "ml.wav"
        out.write_bytes(_wav(1.0))
        return out
    monkeypatch.setattr(stream, "_uade_pcm_verdict", probe)
    monkeypatch.setattr(stream, "_render_uade_file", fake_uade)
    monkeypatch.setattr(stream, "_render_mlrender", fake_ml)
    return probes, ran


@pytest.mark.asyncio
@pytest.mark.parametrize("uade_result, want, remembered", [
    ("silence", "ml.wav", False),               # silent: mlrender, and uade's verdict kept
    (HTTPException(502, "x"), "ml.wav", None),  # failed: mlrender, no verdict on uade
    ("tone", "uade.wav", None),                 # audible: uade's render stands
])
async def test_an_undecided_tune_falls_to_mlrender_when_uade_gives_nothing(
        tmp_path, monkeypatch, uade_result, want, remembered):
    ml = tmp_path / "a.ml"
    ml.write_bytes(rs.ml_tune())
    probes, ran = _ml_tools(monkeypatch, tmp_path, uade_result)
    out = await stream._render_uade(ml, subsong=0, live_key="k", subsong_base=0)
    assert out.name == want
    assert ran[0] == ("uade", "k")
    assert ran[1:] == ([("mlrender", None)] if want == "ml.wav" else [])
    assert musicline.uade_verdict(musicline.digest(ml), 0, 0) is remembered
    if want == "ml.wav":
        assert not (tmp_path / "uade.wav").exists() or uade_result != "silence"
    # the play is named by the renderer that made it
    assert stream._uade_rendered_by("t", ml, 0, 0) == ("mlrender" if want == "ml.wav"
                                                       else "uade123")
    assert stream.renderer_key_for({"id": "t", "path": str(ml)}) == (
        "mlrender" if want == "ml.wav" else "uade123")
    if want == "ml.wav" and remembered is None:
        # (a probe that decides later wins: uade plays it after all)
        musicline.remember(musicline.digest(ml), 0, 0, True)
        assert stream._uade_rendered_by("t", ml, 0, 0) == "uade123"


@pytest.mark.asyncio
async def test_a_module_that_is_not_musicline_never_reaches_mlrender(tmp_path, monkeypatch):
    mod = tmp_path / "x.mod"
    mod.write_bytes(b"M.K." + b"\0" * 2000)
    probes, ran = _ml_tools(monkeypatch, tmp_path, "silence")
    out = await stream._render_uade(mod, subsong=0)
    assert out.name == "uade.wav" and ran == [("uade", None)] and probes == []


@pytest.mark.asyncio
async def test_a_cancelled_uade_render_stays_cancelled(tmp_path, monkeypatch):
    ml = tmp_path / "a.ml"
    ml.write_bytes(rs.ml_tune())
    probes, ran = _ml_tools(monkeypatch, tmp_path, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await stream._render_uade(ml, subsong=0)
    assert ran == [("uade", None)]


def test_the_content_hash_is_read_once_per_size_and_time(tmp_path, monkeypatch):
    ml = tmp_path / "a.ml"
    ml.write_bytes(rs.ml_tune())
    other = tmp_path / "b.ml"
    other.write_bytes(rs.ml_tune(loop=True))
    reads = []
    real = musicline._digest_read
    monkeypatch.setattr(musicline, "_digest_read", lambda p: reads.append(Path(p).name)
                        or real(p))
    first = musicline.digest(ml)
    assert musicline.digest(ml) == first and reads == ["a.ml"]          # warm: a stat only
    assert musicline.digest(other) not in (None, first) and reads == ["a.ml", "b.ml"]
    os.utime(ml, ns=(1, 1))                                             # changed time
    assert musicline.digest(ml) == first and reads == ["a.ml", "b.ml", "a.ml"]
    assert musicline.known_digest(ml) == first
    assert musicline.known_digest(tmp_path / "never.ml") is None


def test_a_missing_tune_index_is_no_refusal_of_the_file():
    for err in ("mlrender: no subsong 5 (the module has 2)\n",
                "mlrender: no tune 5 in this module\n",
                "mlrender: tune 7 out of range\n"):
        assert unplayable.classify("Musicline", 1, err) is None, err
        assert not isinstance(unplayable.refusal("Musicline", 422, "x", 1, err),
                              unplayable.RenderRefusal)
    # (words of the file's name are never taken for it)
    for err in ("mlrender: can't load this Musicline Editor module: /m/No Tune Today.ml\n",
                "mlrender: can't load this Musicline Editor module: /m/not found yet/a.ml\n"):
        assert unplayable.classify("Musicline", 1, err) is not None, err
    # (a tune it has but can't play still is one)
    c = unplayable.classify("Musicline", 1, "mlrender: can't play subsong 1 of a.ml\n")
    assert c is not None and c[0] == "mlrender"
    assert unplayable.classify("Musicline", 1, "mlrender: can't load this Musicline Editor "
                               "module: a.ml\n")[1] == "can't load this file"


@pytest.mark.asyncio
async def test_a_missing_tune_index_never_trips_the_breaker(tmp_path, monkeypatch):
    # Four different tracks asking mlrender for tunes their modules don't
    # have: no mark, no streak, mlrender not taken for broken.
    unplayable.reset_for_tests()
    store = {}

    class _S:
        def get_track(self, tid):
            return store.setdefault(tid, {"id": tid, "file_size": 8})

        def update_track_fields(self, tid, f):
            store[tid].update(f)
    monkeypatch.setattr(unplayable, "_store", lambda: _S())
    try:
        for i in range(5):
            f = tmp_path / f"{i}.ml"
            f.write_bytes(b"MLEDMODL")
            unplayable.set_play(f"t{i}", f, bare=True, remote=False)

            async def render():
                raise unplayable.refusal("Musicline", 422, "x", 1,
                                         "mlrender: no subsong 9 (the module has 1)\n")
            with pytest.raises(HTTPException) as e:
                await unplayable.render_checked(render, "uade")
            assert not isinstance(e.value, unplayable.RenderRefusal)
        assert "mlrender" not in unplayable._streaks and "mlrender" not in unplayable._tripped
        assert all(t.get("defect") is None for t in store.values())
    finally:
        unplayable.reset_for_tests()


# ── X-Rendered for an early DSIK module ──────────────────────────────────────

def _milky():
    p = os.environ.get("SB_MILKYRENDER") or shutil.which("milkyrender")
    return p if p and os.path.exists(p) else None


@pytest.mark.skipif(_milky() is None, reason="milkyrender not built here")
@pytest.mark.asyncio
async def test_a_dsik_v1_play_is_named_milkyrender(monkeypatch, tmp_path):
    from soniqboom.config import settings
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(settings, "milkyrender_path", _milky())
    v1 = tmp_path / "v1.dsm"
    v1.write_bytes(rs.dsik_tune())
    tracks = {"v1": _T("v1", v1, 0.0, fmt="DSIK")}
    app, _fs = _app(monkeypatch, tmp_path, tracks)
    async with _client(app) as c:
        heads = []
        for _ in range(2):                                  # cold, then warm (cache hit)
            r = await c.get("/api/stream/v1", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            heads.append((r.headers["x-rendered"], r.headers["x-cache"]))
            await asyncio.sleep(0.2)
    assert [h[0] for h in heads] == ["milkyrender", "milkyrender"] and heads[1][1] == "hit"
    assert stream.renderer_key_for({"id": "v1", "path": str(v1)}) == "milkyrender"
    # (without a play in this run: the suffix's renderer)
    assert stream.renderer_key_for({"id": "other", "path": str(v1)}) == "openmpt123"


def test_a_riff_dsm_is_not_milkyrenders(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "_find_renderer", lambda conf, name: "/opt/" + name)
    riff = tmp_path / "r.dsm"
    riff.write_bytes(b"RIFF" + struct.pack("<I", 200) + b"DSMFSONG" + bytes(200))
    v1 = tmp_path / "v1.dsm"
    v1.write_bytes(rs.dsik_tune())
    assert stream._dsik_v1_milky(riff) is False and stream._dsik_v1_milky(v1) is True
    monkeypatch.setattr(stream, "_find_renderer", lambda conf, name: None)
    assert stream._dsik_v1_milky(v1) is False


@pytest.mark.asyncio
async def test_lookups_try_the_render_a_play_makes_now(tmp_path, monkeypatch):
    # HEAD and the waveform look up the key a play renders under: a plain
    # local rip's own (dsfrender's, or zxtune123's after a dsfrender failure)
    # — never an older zxtune123 render beside dsfrender's — and, for a file
    # they can't read (an archive member, a share), both.
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    rip = tmp_path / "a.minidsf"
    rip.write_bytes(_psf(b"\0" * 8))
    assert await stream._psf_variants_known(".minidsf", str(rip), True) == ("dsf1",)
    dsf_render.note_failed(rip)
    assert await stream._psf_variants_known(".minidsf", str(rip), True) == (None,)
    assert await stream._psf_variants_known(".minidsf", f"{tmp_path}/x.zip::a.minidsf",
                                            False) == ("dsf1", None)
    assert await stream._psf_variants_known(".minipsf", str(rip), True) == (None,)
