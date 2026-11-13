# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dreamcast / Naomi DSF through dsfrender (core/dsf_render,
soniqboom/native/dsfrender): routing, cache keys, the length plan, and —
where the binary is built (``SB_DSFRENDER`` or on PATH) — real renders of a
synthesized DSF, its library chain and the library-path rules."""
from __future__ import annotations

import array
import importlib.util
import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from soniqboom.core import dsf_render

ROOT = Path(__file__).resolve().parents[1]


def _selftest():
    spec = importlib.util.spec_from_file_location(
        "render_selftest", ROOT / "scripts/render_selftest.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    return rs


def _psf(section: bytes, tags: str = "", version: int = 0x12) -> bytes:
    """A PSF file holding one zlib-packed ``section`` (load address included)."""
    exe = zlib.compress(section)
    return (b"PSF" + bytes([version]) + struct.pack("<III", 0, len(exe), zlib.crc32(exe))
            + exe + (b"[TAG]" + tags.encode() if tags else b""))


def _tune_section() -> bytes:
    """The self-test DSF's one section (its load address, then the image)."""
    raw = _selftest().dsf_tune()
    return zlib.decompress(raw[16:16 + struct.unpack("<I", raw[8:12])[0]])


DSD = b"DSD " + struct.pack("<QQQ", 28, 0, 0) + b"fmt " + b"\0" * 64


# ── The length plan ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tags, plan", [
    ({}, (180.0, 0.0)),                                   # zxtune123's default length
    ({"length": "2:00", "fade": "10"}, (130.0, 10.0)),    # = metadata.psf_play_plan
    ({"length": "1:00.5"}, (60.5, 0.0)),
    ({"fade": "4"}, (180.0, 4.0)),                        # a fade alone: inside the 180 s
    ({"fade": "90"}, (180.0, 60.0)),                      # clamped as psf_play_plan clamps
    ({"fade": "junk"}, (180.0, 0.0)),
    ({"length": "2:00:00"}, (180.0, 0.0)),                # past an hour: no length
])
def test_render_plan(tags, plan):
    from soniqboom.core.metadata import psf_play_plan
    assert dsf_render.render_plan(tags) == plan
    if psf_play_plan(tags)[0] > 0:
        assert dsf_render.render_plan(tags) == psf_play_plan(tags)


def test_command_keeps_the_length_exact():
    cmd = dsf_render.command("/b/dsfrender", Path("a.minidsf"), Path("o.wav"), 0.1 + 0.2, 1.2345)
    assert cmd == ["/b/dsfrender", "-r", "44100", "-m", repr(0.1 + 0.2), "-f", repr(1234.5),
                   "a.minidsf", "o.wav"]
    assert float(cmd[4]) == 0.1 + 0.2


# ── Routing and cache keys ───────────────────────────────────────────────────

def test_binary_for_routes_only_dreamcast_rips(tmp_path, monkeypatch):
    mini = tmp_path / "a.minidsf"
    mini.write_bytes(_psf(b"\0" * 8))
    dc = tmp_path / "b.dsf"
    dc.write_bytes(_psf(b"\0" * 8))
    dsd = tmp_path / "c.dsf"
    dsd.write_bytes(DSD)
    other = tmp_path / "d.minissf"
    other.write_bytes(_psf(b"\0" * 8, version=0x11))
    not_dc = tmp_path / "e.minidsf"               # no Dreamcast header: zxtune123 reads it
    not_dc.write_bytes(_psf(b"\0" * 8, version=0x11))
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    assert dsf_render.binary_for(mini) == "/opt/dsfrender"
    assert dsf_render.binary_for(dc) == "/opt/dsfrender"
    assert dsf_render.binary_for(dsd) is None             # Sony DSD: never dsfrender
    assert dsf_render.binary_for(other) is None
    assert dsf_render.binary_for(not_dc) is None
    monkeypatch.setattr(dsf_render, "binary", lambda: None)
    assert dsf_render.binary_for(mini) is None
    assert dsf_render.binary_for(dc) is None


def test_binary_is_the_configured_path_else_path(tmp_path, monkeypatch):
    from soniqboom.config import settings
    exe = tmp_path / "dsfrender"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(settings, "dsfrender_path", str(exe))
    assert dsf_render.binary() == str(exe)
    monkeypatch.setattr(settings, "dsfrender_path", str(tmp_path / "missing"))
    monkeypatch.setattr(shutil, "which", lambda name: "/on/path/" + name)
    assert dsf_render.binary() == "/on/path/dsfrender"


def test_cache_variant_separates_dsfrender_renders(tmp_path, monkeypatch):
    from soniqboom.core.conversion_cache import _cache_key
    mini = tmp_path / "a.minidsf"
    mini.write_bytes(_psf(b"\0" * 8))
    dc = tmp_path / "b.DSF"
    dc.write_bytes(_psf(b"\0" * 8))
    psf = tmp_path / "c.minipsf"
    psf.write_bytes(_psf(b"\0" * 8, version=0x01))
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    assert dsf_render.cache_variant_for(mini) == "dsf1"
    assert dsf_render.cache_variant_for(dc) == "dsf1"
    assert dsf_render.cache_variant_for(psf) is None
    assert _cache_key("t", "psf", 0, variant="dsf1") == "t__r1__vdsf1"
    assert _cache_key("t", "psf", 0) == "t__r1"             # zxtune123's key, unchanged
    assert dsf_render.cache_variants(".minidsf") == ("dsf1", None)
    assert dsf_render.cache_variants(".minipsf") == (None,)
    monkeypatch.setattr(dsf_render, "binary", lambda: None)
    assert dsf_render.cache_variant_for(mini) is None


def test_a_minidsf_without_the_dreamcast_header_is_keyed_and_named_as_zxtune(
        tmp_path, monkeypatch):
    # The variant follows the routing (``binary_for``), not the suffix: a
    # .minidsf without 'PSF\x12' renders through zxtune123, so it is cached
    # under zxtune123's key.
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    odd = tmp_path / "odd.minidsf"
    odd.write_bytes(_psf(b"\0" * 8, version=0x11))
    assert dsf_render.binary_for(odd) is None
    assert dsf_render.cache_variant_for(odd) is None


def test_a_failed_rip_routes_to_zxtune_until_it_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    dsf_render.forget_failures()
    a = tmp_path / "a.minidsf"
    a.write_bytes(_psf(b"\0" * 8))
    b = tmp_path / "b.minidsf"
    b.write_bytes(_psf(b"\0" * 8))
    try:
        dsf_render.note_failed(a)
        assert dsf_render.binary_for(a) is None and dsf_render.cache_variant_for(a) is None
        assert dsf_render.binary_for(b) == "/opt/dsfrender"     # (another rip: unchanged)
        a.write_bytes(_psf(b"\1" * 9))                          # a new file there
        assert dsf_render.binary_for(a) == "/opt/dsfrender"
    finally:
        dsf_render.forget_failures()


def test_renderer_key_for(monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    monkeypatch.setattr(stream, "_RENDERED_BY", stream.OrderedDict())
    assert stream.renderer_key_for({"path": "/m/a.minidsf"}) == "dsfrender"
    assert stream.renderer_key_for({"path": "/m/a.dsf", "format": "DSF (Dreamcast)"}) == "dsfrender"
    assert stream.renderer_key_for({"path": "/m/a.minipsf"}) == "zxtune123"
    assert stream.renderer_key_for({"path": "/m/a.dsf", "format": "DSD"}) is None
    # a play served by zxtune123 (dsfrender failed on it) names zxtune123
    stream._note_rendered_by("t9", "zxtune123")
    assert stream.renderer_key_for({"id": "t9", "path": "/m/a.minidsf"}) == "zxtune123"
    monkeypatch.setattr(dsf_render, "binary", lambda: None)
    assert stream.renderer_key_for({"path": "/m/a.minidsf"}) == "zxtune123"


@pytest.mark.asyncio
async def test_render_psf_sends_a_dreamcast_rip_to_dsfrender(tmp_path, monkeypatch):
    from soniqboom.api import stream
    lib = tmp_path / "set.dsflib"
    lib.write_bytes(_psf(b"\0" * 8))
    mini = tmp_path / "a.minidsf"
    mini.write_bytes(_psf(b"\0" * 8, "_lib=set.dsflib\n"))
    seen = []

    async def fake_render(binary, path, *, live_key=None, expected_seconds=0.0):
        seen.append((binary, path.name, live_key, expected_seconds))
        return tmp_path / "out.wav"
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    monkeypatch.setattr(dsf_render, "render", fake_render)
    # (zxtune123 is not needed for it)
    monkeypatch.setattr(stream, "_find_renderer", lambda conf, name: None)
    out = await stream._render_psf(mini, live_key="k", expected_seconds=12.0)
    assert out == tmp_path / "out.wav"
    assert seen == [("/opt/dsfrender", "a.minidsf", "k", 12.0)]


@pytest.mark.asyncio
async def test_a_missing_library_is_named_with_dsfrender_too(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import stream
    mini = tmp_path / "a.minidsf"
    mini.write_bytes(_psf(b"\0" * 8, "_lib=gone.dsflib\n"))
    called = []

    async def fake_render(*a, **k):
        called.append(a)
    monkeypatch.setattr(dsf_render, "binary", lambda: "/opt/dsfrender")
    monkeypatch.setattr(dsf_render, "render", fake_render)
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(mini)
    assert e.value.status_code == 422 and "gone.dsflib" in e.value.detail
    assert called == []


@pytest.mark.asyncio
async def test_without_dsfrender_a_dreamcast_rip_stays_with_zxtune(tmp_path, monkeypatch):
    from soniqboom.api import stream
    mini = tmp_path / "a.minidsf"
    mini.write_bytes(_psf(b"\0" * 8))
    cmds, called = [], []

    async def fake_await(cmd, out, **kw):
        cmds.append(cmd)

    async def fake_render(*a, **k):
        called.append(a)
    monkeypatch.setattr(dsf_render, "binary", lambda: None)
    monkeypatch.setattr(dsf_render, "render", fake_render)
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: "/opt/zxtune123" if name == "zxtune123" else None)
    monkeypatch.setattr(stream, "_await_renderer", fake_await)
    out = await stream._render_psf(mini)
    out.unlink(missing_ok=True)
    assert called == []
    assert cmds and cmds[0][0] == "/opt/zxtune123" and cmds[0][-1] == str(mini)


@pytest.mark.asyncio
async def test_without_any_renderer_it_is_a_501(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import stream
    mini = tmp_path / "a.minidsf"
    mini.write_bytes(_psf(b"\0" * 8))
    monkeypatch.setattr(dsf_render, "binary", lambda: None)
    monkeypatch.setattr(stream, "_find_renderer", lambda conf, name: None)
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(mini)
    assert e.value.status_code == 501


def test_unplayable_knows_dsfrender():
    from soniqboom.core import unplayable
    assert unplayable.KIND_KEYS[dsf_render.KIND] == "dsfrender"
    assert "dsfrender" in unplayable.NAMES
    assert unplayable.classify("DSF", 1, "dsfrender: not a Dreamcast DSF this build can play: x")[0] \
        == "dsfrender"
    # a library it can't open is an I/O problem, never the file's
    assert unplayable.classify(
        "DSF", 1, "dsfrender: can't open library x.dsflib: no such file in the rip's folder") is None

    async def _go():
        saved = (dict(unplayable._proven), {k: list(v) for k, v in unplayable._streaks.items()},
                 set(unplayable._tripped))
        try:
            for k in ("dsfrender", "zxtune123"):
                unplayable._proven.pop(k, None)
                unplayable._tripped.add(k)
                unplayable._streaks[k] = ["a"]

            async def dsf_render_fn():          # the render names itself
                unplayable.renderer_ran("dsfrender", "psf")
                return "wav"
            assert await unplayable.render_checked(dsf_render_fn, "psf") == "wav"
            unplayable.rendered_ok("psf")
            # a dsfrender render proves dsfrender — never zxtune123
            assert "dsfrender" in unplayable._proven and "dsfrender" not in unplayable._tripped
            assert "zxtune123" not in unplayable._proven and "zxtune123" in unplayable._tripped

            async def zx_render_fn():
                return "wav"
            await unplayable.render_checked(zx_render_fn, "psf")
            unplayable.rendered_ok("psf")             # (no stale dsfrender from before)
            assert "zxtune123" in unplayable._proven and "zxtune123" not in unplayable._tripped
        finally:
            unplayable._proven.clear(); unplayable._proven.update(saved[0])
            unplayable._streaks.clear(); unplayable._streaks.update(saved[1])
            unplayable._tripped.clear(); unplayable._tripped.update(saved[2])
    import asyncio
    asyncio.run(_go())


# ── Real renders (the binary built from soniqboom/native/dsfrender) ─────────

def _binary():
    p = os.environ.get("SB_DSFRENDER") or shutil.which("dsfrender")
    return p if p and os.path.exists(p) else None


needs_binary = pytest.mark.skipif(_binary() is None,
                                  reason="dsfrender not built here (soniqboom/native/dsfrender/build.sh)")


def _pcm(wav: Path) -> "array.array":
    data = wav.read_bytes()
    assert data[:4] == b"RIFF" and data[36:40] == b"data"
    assert struct.unpack("<HI", data[22:28]) == (2, 44100)
    n = struct.unpack("<I", data[40:44])[0]
    assert len(data) == 44 + n
    a = array.array("h")
    a.frombytes(data[44:44 + n])
    return a


def _run(*args):
    return subprocess.run([_binary(), *map(str, args)], capture_output=True, text=True, timeout=120)


@needs_binary
@pytest.mark.asyncio
async def test_a_tagged_render_plays_its_length_then_fades(tmp_path):
    src = tmp_path / "t.dsf"
    src.write_bytes(_psf(_tune_section(), "length=2\nfade=1\n"))
    out = await dsf_render.render(_binary(), src)
    try:
        a = _pcm(out)
        assert len(a) == 2 * 3 * 44100                       # length + fade
        assert _run("-m", "3", "-f", "0", src, tmp_path / "plain.wav").returncode == 0
        plain = _pcm(tmp_path / "plain.wav")
        start, n = 2 * 44100, 44100
        assert a[:2 * start] == plain[:2 * start]            # unfaded for 2 s
        assert max(map(abs, a[:2 * start])) == 12000
        # the fade: frame k of it scaled by (n - k) / n, truncated (as
        # stream._fade_pcm16 fades a zxtune123 render)
        assert all(a[2 * (start + k) + c] == int(plain[2 * (start + k) + c] * ((n - k) / n))
                   for k in range(n) for c in (0, 1))
        assert max(map(abs, a[-200:])) < 100
    finally:
        out.unlink(missing_ok=True)


@needs_binary
def test_the_cli_defaults_follow_the_tags(tmp_path):
    src = tmp_path / "t.dsf"
    src.write_bytes(_psf(_tune_section(), "length=0:01.5\nfade=0.5\n"))
    r = _run(src, tmp_path / "o.wav")
    assert r.returncode == 0, r.stderr
    assert len(_pcm(tmp_path / "o.wav")) == 2 * 88200        # 1.5 s + 0.5 s
    src.write_bytes(_psf(_tune_section(), "length=1\n"))
    assert _run("-m", "0.5", src, tmp_path / "o.wav").returncode == 0
    assert len(_pcm(tmp_path / "o.wav")) == 2 * 22050        # -m wins


@needs_binary
def test_a_minidsf_plays_its_library_found_in_any_case(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Self.DSFLIB").write_bytes(_psf(_tune_section()))
    whole = tmp_path / "whole.dsf"
    whole.write_bytes(_psf(_tune_section()))
    mini = tmp_path / "song.minidsf"
    # its own section: four bytes nothing reads, after the library's
    mini.write_bytes(_psf(struct.pack("<I", 0x20000) + b"\x01\x02\x03\x04",
                          "_lib=sub\\self.dsflib\n"))
    assert _run("-m", "1", mini, tmp_path / "m.wav").returncode == 0
    assert _run("-m", "1", whole, tmp_path / "w.wav").returncode == 0
    m, w = _pcm(tmp_path / "m.wav"), _pcm(tmp_path / "w.wav")
    assert max(map(abs, m)) == 12000 and m == w


@needs_binary
def test_a_library_path_never_leaves_the_rip_folder(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (tmp_path / "self.dsflib").write_bytes(_psf(_tune_section()))
    for lib in ("../self.dsflib", str(tmp_path / "self.dsflib"), "..\\self.dsflib"):
        mini = inner / "song.minidsf"
        mini.write_bytes(_psf(struct.pack("<I", 0x20000) + b"\0\0\0\0", f"_lib={lib}\n"))
        r = _run("-m", "1", mini, tmp_path / "o.wav")
        assert r.returncode == 1, (lib, r.stderr)
        assert "can't open library" in r.stderr
    # only the file of that name beside the rip is ever tried
    shutil.copy(tmp_path / "self.dsflib", inner / "self.dsflib")
    assert _run("-m", "1", mini, tmp_path / "o.wav").returncode == 0
    assert max(map(abs, _pcm(tmp_path / "o.wav"))) == 12000


@needs_binary
def test_a_section_past_the_sound_ram_is_cut_not_wrapped(tmp_path):
    # 32 bytes at 0x7FFFF0: the 16 past the end must not land at address 0
    # (they would overwrite the reset vector of the tune loaded there)
    mini = tmp_path / "song.minidsf"
    (tmp_path / "t.dsflib").write_bytes(_psf(_tune_section()))
    mini.write_bytes(_psf(struct.pack("<I", 0x7FFFF0) + b"\xff" * 32, "_lib=t.dsflib\n"))
    assert _run("-m", "1", mini, tmp_path / "o.wav").returncode == 0
    assert max(map(abs, _pcm(tmp_path / "o.wav"))) == 12000


@needs_binary
def test_what_it_refuses(tmp_path):
    dsd = tmp_path / "a.dsf"
    dsd.write_bytes(DSD)
    r = _run(dsd, tmp_path / "o.wav")
    assert r.returncode == 1 and "not a Dreamcast DSF" in r.stderr
    ssf = tmp_path / "a.minissf"
    ssf.write_bytes(_psf(b"\0" * 8, version=0x11))
    assert _run(ssf, tmp_path / "o.wav").returncode == 1
    good = tmp_path / "t.dsf"
    good.write_bytes(_psf(_tune_section()))
    for bad in (["-r", "48000"], ["-m", "0"], ["-m", "4000"], ["-m", "3600.01"], ["-f", "-1"],
                ["-f", "3600000.1"], ["-m", "x"]):
        assert _run(*bad, good, tmp_path / "o.wav").returncode == 2, bad
    # A fade a hair over the hour (a float sum) is the hour.
    assert _run("-m", "0.1", "-f", "3600000.0000001", good, tmp_path / "o.wav").returncode == 0
    r = _run("-m", "0.000001", good, tmp_path / "o.wav")     # no frame to render
    assert r.returncode == 1 and "nothing rendered" in r.stderr
    v = _run("--version")
    assert v.returncode == 0 and v.stdout.startswith("dsfrender ")


# ── Through the stream endpoint (in-process; a throw-away cache and store) ──

@needs_binary
@pytest.mark.asyncio
async def test_the_stream_endpoint_plays_dreamcast_rips_through_dsfrender(monkeypatch, tmp_path):
    import asyncio
    import sys
    from soniqboom.api import stream
    from soniqboom.config import settings
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_render_r1_stream import _T, _app, _client
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(settings, "dsfrender_path", _binary())
    lib = tmp_path / "set.dsflib"
    lib.write_bytes(_psf(_tune_section()))
    one = tmp_path / "one.minidsf"
    one.write_bytes(_psf(struct.pack("<I", 0x20000) + b"\0" * 4, "_lib=set.dsflib\nlength=2\n"))
    two = tmp_path / "two.dsf"
    two.write_bytes(_psf(_tune_section(), "length=1\nfade=0.5\n"))
    dsd = tmp_path / "three.dsf"
    dsd.write_bytes(DSD)
    renders = []
    real = dsf_render.render

    async def counting(binary, path, **kw):
        renders.append(path.name)
        return await real(binary, path, **kw)
    monkeypatch.setattr(dsf_render, "render", counting)
    tracks = {"dc1": _T("dc1", one, 2.0, fmt="DSF (Dreamcast)"),
              "dc2": _T("dc2", two, 1.5, fmt="DSF (Dreamcast)"),
              "dsd": _T("dsd", dsd, 1.0, fmt="DSD")}
    app, _fs = _app(monkeypatch, tmp_path, tracks)
    from soniqboom.core import unplayable
    monkeypatch.setattr(unplayable, "_proven", {})
    async with _client(app) as c:
        got = {}
        for tid in ("dc1", "dc2", "dc1"):             # cold, cold (another rip), warm
            r = await c.get(f"/api/stream/{tid}", cookies={"sb_session": "x"})
            assert r.status_code == 200, r.text
            assert r.headers["x-rendered"] == "dsfrender"
            got.setdefault(tid, []).append((r.headers["x-cache"], r.content))
            await asyncio.sleep(0.2)
        r = await c.get("/api/stream/dsd", cookies={"sb_session": "x"})
    assert renders == ["one.minidsf", "two.dsf"]       # the warm play rendered nothing
    assert got["dc1"][0][0].startswith("miss") and got["dc1"][1][0] == "hit"
    assert got["dc2"][0][0].startswith("miss")
    pcm1, pcm2 = _pcm_bytes(got["dc1"][1][1]), _pcm_bytes(got["dc2"][0][1])
    assert len(pcm1) == 2 * 44100 * 4 and len(pcm2) == int(1.5 * 44100) * 4
    assert pcm1 != pcm2[:len(pcm1)]                    # (two renders, not one served twice)
    keys = sorted(p.stem for p in (tmp_path / "conv" / "psf").rglob("*.wav"))
    assert keys == ["dc1__r1__vdsf1", "dc2__r1__vdsf1"]
    assert "three.dsf" not in renders                  # a DSD .dsf never reaches dsfrender
    # the stored renders proved dsfrender, not zxtune123 (core/unplayable.renderer_ran)
    assert "dsfrender" in unplayable._proven and "zxtune123" not in unplayable._proven
    assert r.headers.get("x-rendered") != "dsfrender"


def _pcm_bytes(wav: bytes) -> bytes:
    i = wav.find(b"data")
    size = int.from_bytes(wav[i + 4:i + 8], "little")
    return wav[i + 8:i + 8 + size] if size != 0xFFFFFFFF else wav[i + 8:]


@needs_binary
@pytest.mark.asyncio
async def test_an_archived_minidsf_renders_with_its_library_extracted_beside_it(
        monkeypatch, tmp_path):
    """The companion extraction (``.minidsf`` → ``*.dsflib``) hands dsfrender
    the same local copies it hands zxtune123."""
    import zipfile
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "dsfrender_path", _binary())
    monkeypatch.setattr(stream, "_zip_extract_dir",
                        lambda: (tmp_path / "zx").mkdir(exist_ok=True) or tmp_path / "zx")
    arc = tmp_path / "set.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("set/a.minidsf", _psf(struct.pack("<I", 0x20000) + b"\0" * 4,
                                         "_lib=Set.dsflib\nlength=1\n"))
        z.writestr("set/Set.dsflib", _psf(_tune_section()))
    local = await stream._resolve_adlib_local_path("arc1", f"{arc}::set/a.minidsf")
    assert local is not None and local.name == "a.minidsf"
    assert (local.parent / "Set.dsflib").is_file()
    out = await stream._render_psf(local)
    try:
        a = _pcm(out)
        assert len(a) == 2 * 44100 and max(map(abs, a)) == 12000
    finally:
        out.unlink(missing_ok=True)
