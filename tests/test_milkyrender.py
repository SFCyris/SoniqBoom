# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Early DSIK modules (``DSM\\x10``) through milkyrender (MilkyPlay —
soniqboom/native/milkyrender, core/milkyrender.py): routed by content (a
RIFF DSMF ``.dsm`` stays with libopenmpt), scanned with milkyrender's
length, and a damaged module refused with a reason.  The tests that run the
real binary use ``SB_MILKYRENDER`` or ``milkyrender`` on PATH and skip
without one."""
from __future__ import annotations

import array
import importlib.util
import os
import shutil
import struct
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream
from soniqboom.core import milkyrender as milky
from soniqboom.core import unplayable

ROOT = Path(__file__).resolve().parents[1]
ANDANTE = Path(os.environ.get("SB_DSIK_ANDANTE") or ROOT / "internal/format-samples/"
               "Digital Sound Interface Kit/Necros/andante.dsm")

_spec = importlib.util.spec_from_file_location("render_selftest",
                                               ROOT / "scripts/render_selftest.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def _binary():
    p = os.environ.get("SB_MILKYRENDER") or shutil.which("milkyrender")
    return p if p and os.path.exists(p) else None


needs_binary = pytest.mark.skipif(_binary() is None,
                                  reason="milkyrender not built here "
                                         "(soniqboom/native/milkyrender/build.sh)")


def _slower(tune: bytes) -> bytes:
    """The self-test tune at 3 ticks a row instead of 6 (half as long)."""
    b = bytearray(tune)
    b[41] = 3
    return bytes(b)


def _stopping() -> bytes:
    """The self-test tune with a stop (F00) 16 rows in: a song that ends
    without jumping back."""
    t = rs.dsik_tune()
    pk = bytearray([0xE0 ^ 3, 25, 1, 64])               # note, as the self-test tune
    for _ in range(16):
        pk.append((3 + len(pk)) & 0xFF)                 # a row ends (0, XORed)
    pk.append(0x10 ^ ((3 + len(pk)) & 0xFF))
    pk += bytes([0x0F, 0x00])                           # effect F00
    pat_at, smp_at = 240, 272
    out = bytearray(t[:192 + 47])
    out[35 + 192] = smp_at >> 20
    out[36 + 192:38 + 192] = struct.pack("<H", (smp_at >> 4) & 0xFFFF)
    out += bytes(pat_at - len(out)) + struct.pack("<H", 2 + len(pk)) + pk
    out += bytes(smp_at - len(out)) + bytes([100] * 32 + [156] * 32)
    return bytes(out)


def _riff_dsmf() -> bytes:
    return b"RIFF" + struct.pack("<I", 200) + b"DSMFSONG" + bytes(200)


def _pcm(path: Path) -> "array.array":
    data = path.read_bytes()
    a = array.array("h")
    a.frombytes(data[44:44 + struct.unpack("<I", data[40:44])[0]])
    return a


def _secs(path: Path) -> float:
    data = path.read_bytes()
    return len(_pcm(path)) / 2 / struct.unpack("<I", data[24:28])[0]


# ── content test, header, command ───────────────────────────────────────────

def test_only_the_dsm10_magic_is_dsik_v1():
    assert milky.is_dsik_v1(rs.dsik_tune())
    assert not milky.is_dsik_v1(_riff_dsmf())
    assert not milky.is_dsik_v1(b"DSm\x1a GUITAR TIME")       # Dynamic Studio
    assert not milky.is_dsik_v1(b"DSM\x11")
    assert not milky.is_dsik_v1(b"DSM")


def test_header_states_title_counts_and_instruments():
    h = milky.header(rs.dsik_tune())
    assert h["name"].rstrip(b"\0") == b"selftest"
    assert (h["channels"], h["patterns"]) == (1, 1)
    assert [n.rstrip(b"\0") for n in h["instruments"]] == [b"square"]
    assert milky.header(rs.dsik_tune()[:100]) is None          # fixed header cut short
    assert milky.header(_riff_dsmf()) is None


def test_render_command_plays_to_the_natural_end_with_the_openmpt_fade():
    cmd = milky.render_cmd("/opt/milkyrender", "/m/a.dsm", "/t/o.wav")
    assert cmd == ["/opt/milkyrender", "-r", "48000", "-m", "3600", "-f", "100", "-l", "1",
                   "/m/a.dsm", "/t/o.wav"]
    assert milky.FADE_MS == int(round(stream._OPENMPT_END_FADE_S * 1000))


# ── routing by content ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_dsik_v1_module_goes_to_milkyrender_when_installed(tmp_path, monkeypatch):
    seen = []

    async def fake(binary, path, *, live_key=None):
        seen.append((binary, path.name, live_key))
        return tmp_path / "out.wav"
    monkeypatch.setattr(stream, "_render_dsik_v1", fake)
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: "/opt/milkyrender" if name == "milkyrender" else None)
    src = tmp_path / "a.dsm"
    src.write_bytes(rs.dsik_tune())
    assert await stream._render_tracker(src, live_key="k1") == tmp_path / "out.wav"
    # (by content, whatever the name)
    odd = tmp_path / "b.mod"
    odd.write_bytes(rs.dsik_tune())
    await stream._render_tracker(odd)
    assert seen == [("/opt/milkyrender", "a.dsm", "k1"), ("/opt/milkyrender", "b.mod", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [_riff_dsmf(), b"DSm\x1a" + bytes(300)],
                         ids=["riff-dsmf", "dynamic-studio"])
async def test_other_dsm_files_stay_with_openmpt(tmp_path, monkeypatch, data):
    called = []

    async def fake(*a, **k):
        called.append(a)
    monkeypatch.setattr(stream, "_render_dsik_v1", fake)
    # milkyrender "installed", openmpt123 not: the openmpt route's 501 shows
    # where the file went.
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: "/opt/milkyrender" if name == "milkyrender" else None)
    src = tmp_path / "a.dsm"
    src.write_bytes(data)
    with pytest.raises(HTTPException) as ei:
        await stream._render_tracker(src)
    assert ei.value.status_code == 501 and "openmpt123" in ei.value.detail
    assert called == []


@pytest.mark.asyncio
async def test_without_milkyrender_a_dsik_v1_module_is_the_clear_422(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "_find_renderer", lambda conf, name: None)
    src = tmp_path / "a.dsm"
    src.write_bytes(rs.dsik_tune())
    with pytest.raises(HTTPException) as ei:
        await stream._render_tracker(src)
    assert ei.value.status_code == 422 and ei.value.detail == stream._DSM_EARLY_DETAIL


# ── refusals ────────────────────────────────────────────────────────────────

def test_a_damaged_module_is_a_diagnosed_refusal():
    err = "milkyrender: damaged DSIK module: pattern 5 is cut short\n"
    assert unplayable.dsik_damage(err) == "pattern 5 is cut short"
    key, why, diagnosed = unplayable.classify("DSIK", 1, err)
    assert key == "milkyrender" and diagnosed
    assert why == "can't play this file: it is damaged (pattern 5 is cut short)"
    exc = stream._renderer_failure("DSIK", "/opt/milkyrender", 1, err)
    assert isinstance(exc, unplayable.RenderRefusal)
    assert exc.status_code == 422
    assert exc.detail == "This DSIK module is damaged (pattern 5 is cut short)."


def test_io_and_usage_failures_never_mark():
    for err in ("milkyrender: can't open /x/a.dsm\n",
                "milkyrender: write error: /tmp/x.wav\n",
                "milkyrender: could not create /tmp/x.wav\n",
                "milkyrender: out of memory\n",
                "usage: milkyrender [-r rate] ...\n"):
        assert unplayable.classify("DSIK", 1, err) is None, err
    # a crash names no reason: the silent class (marked only once proven)
    assert unplayable.classify("DSIK", -11, "")[1:] == ("crashes on this file (SIGSEGV)", False)


# ── the real binary ─────────────────────────────────────────────────────────

def _run(*args, timeout=60):
    import subprocess
    return subprocess.run([_binary(), *map(str, args)], capture_output=True, text=True,
                          timeout=timeout)


@needs_binary
def test_version_names_the_pinned_milkyplay_commit():
    import re
    pin = re.search(r"^ARG MILKYTRACKER_COMMIT=(\w+)", (ROOT / "Dockerfile").read_text(), re.M)
    out = _run("--version").stdout.strip()
    assert out == f"milkyrender 1.1 (MilkyPlay {pin.group(1)[:12]})"


@needs_binary
@pytest.mark.parametrize("tune,secs", [(rs.dsik_tune(), 7.68), (_slower(rs.dsik_tune()), 3.84)],
                         ids=["6-ticks", "3-ticks"])
def test_length_is_exactly_what_the_render_writes(tmp_path, tune, secs):
    src = tmp_path / "t.dsm"
    src.write_bytes(tune)
    for fade in ("0", "100", "2000"):
        length = float(_run("-f", fade, "--length", src).stdout)
        out = tmp_path / f"t{fade}.wav"
        assert _run("-f", fade, src, out).returncode == 0
        assert abs(_secs(out) - length) < 0.0006, (fade, _secs(out), length)
        # the song's end, then the fade that plays on past it
        assert abs(length - secs - int(fade) / 1000) < 0.03, (fade, length)


@needs_binary
def test_a_song_that_stops_ends_there_unfaded_whatever_the_loops(tmp_path):
    src = tmp_path / "s.dsm"
    src.write_bytes(_stopping())
    lengths = {float(_run(*opts, "--length", src).stdout)
               for opts in (("-f", "0"), ("-f", "100"), ("-l", "3", "-f", "2000"))}
    assert len(lengths) == 1
    (length,) = lengths
    assert 1.9 < length < 2.2                           # 16-17 rows at 6 ticks / 125 BPM
    out = tmp_path / "s.wav"
    assert _run("-l", "3", "-f", "2000", src, out).returncode == 0
    assert abs(_secs(out) - length) < 0.0006
    # the looping tune, by contrast, gains its fade and its loops
    loop = tmp_path / "t.dsm"
    loop.write_bytes(rs.dsik_tune())
    one = float(_run("-f", "0", "--length", loop).stdout)
    two = float(_run("-l", "2", "-f", "0", "--length", loop).stdout)
    assert abs(two - 2 * one) < 0.05                   # (each end found to a 4 ms packet)


@needs_binary
def test_a_song_that_jumps_back_fades_out(tmp_path):
    src = tmp_path / "t.dsm"
    src.write_bytes(rs.dsik_tune())
    out = tmp_path / "t.wav"
    assert _run("-f", "500", src, out).returncode == 0
    pcm = _pcm(out)
    last = max(abs(v) for v in pcm[-2 * 480:])               # its last 10 ms
    mid = max(abs(v) for v in pcm[48000 * 2:48000 * 2 + 2 * 480])
    assert mid > 5000 and last < mid / 10


@needs_binary
@pytest.mark.parametrize("cut", [8, 150, 181, 245])
def test_a_cut_short_module_is_refused_at_once(tmp_path, cut):
    src = tmp_path / "t.dsm"
    src.write_bytes(rs.dsik_tune()[:cut])
    t0 = time.monotonic()
    r = _run(src, tmp_path / "o.wav", timeout=10)
    assert time.monotonic() - t0 < 5
    assert r.returncode == 1 and "damaged DSIK module" in r.stderr, r.stderr
    assert not (tmp_path / "o.wav").exists()


@needs_binary
@pytest.mark.parametrize("cut,missing", [(1, 1), (20, 20), (64, 64)])
def test_sample_data_cut_short_plays_and_says_so(tmp_path, cut, missing):
    whole = rs.dsik_tune()
    src = tmp_path / "t.dsm"
    src.write_bytes(whole)
    full = float(_run("-f", "0", "--length", src).stdout)
    assert "warning" not in _run("-f", "0", "--length", src).stderr
    src.write_bytes(whole[:-cut])
    warn = f"milkyrender: warning: truncated sample data ({missing} bytes missing)"
    r = _run("-f", "0", "--length", src)
    assert r.returncode == 0 and warn in r.stderr.splitlines(), r.stderr
    assert float(r.stdout) == full                     # the song itself is whole
    out = tmp_path / "t.wav"
    r = _run("-f", "0", src, out)
    assert r.returncode == 0 and warn in r.stderr.splitlines(), r.stderr
    assert abs(_secs(out) - full) < 0.0006
    pcm = _pcm(out)
    # The square wave's first half is there: audible unless all of it is cut.
    assert (max(abs(v) for v in pcm) > 1000) == (cut < 64)


@needs_binary
def test_garbage_after_the_magic_never_hangs_or_crashes(tmp_path):
    import random
    rng = random.Random(7)
    for i in range(25):
        src = tmp_path / f"g{i}.dsm"
        src.write_bytes(b"DSM\x10" + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 6000))))
        r = _run("-m", "30", src, tmp_path / "o.wav", timeout=30)
        assert r.returncode in (0, 1), (i, r.returncode, r.stderr)


@needs_binary
@pytest.mark.asyncio
async def test_the_stream_render_is_full_length_and_audible(tmp_path):
    src = tmp_path / "t.dsm"
    src.write_bytes(rs.dsik_tune())
    expected = milky.length_seconds(_binary(), src)
    out = await stream._render_dsik_v1(_binary(), src)
    try:
        assert abs(stream._wav_audio_seconds(out) - expected) < 0.001
        assert stream._wav_is_audible(out)
        assert len(set(_pcm(out))) > 1000
    finally:
        out.unlink(missing_ok=True)


@needs_binary
@pytest.mark.asyncio
async def test_a_damaged_module_is_a_422_naming_the_damage(tmp_path):
    src = tmp_path / "t.dsm"
    src.write_bytes(rs.dsik_tune()[:245])                    # its pattern cut short
    with pytest.raises(HTTPException) as ei:
        await stream._render_dsik_v1(_binary(), src)
    assert ei.value.status_code == 422
    assert ei.value.detail == "This DSIK module is damaged (pattern 0 is cut short)."
    assert isinstance(ei.value, unplayable.RenderRefusal) and ei.value.diagnosed


@needs_binary
@pytest.mark.asyncio
@pytest.mark.skipif(not ANDANTE.exists(), reason="DSIK fixture missing")
async def test_andante_renders_to_its_natural_end(monkeypatch):
    monkeypatch.setattr(stream, "_find_renderer",
                        lambda conf, name: _binary() if name == "milkyrender" else None)
    out = await stream._render_tracker(ANDANTE)
    try:
        secs = stream._wav_audio_seconds(out)
        assert abs(secs - 253.436) < 0.01                    # 253.336 s + the 100 ms fade
        assert out.read_bytes()[24:28] == (48000).to_bytes(4, "little")
        assert len(set(_pcm(out))) > 40000
    finally:
        out.unlink(missing_ok=True)


@needs_binary
def test_the_mix_rate_keeps_the_tempo(tmp_path):
    # MilkyPlay's timer steps rate / 250 frames: whole at 48 kHz (7.68 s for
    # 64 rows at 6 ticks / 125 BPM); 44.1 kHz rounds it down and plays fast.
    src = tmp_path / "t.dsm"
    src.write_bytes(rs.dsik_tune())
    at48 = float(_run("-f", "0", "--length", src).stdout)
    at44 = float(_run("-r", "44100", "-f", "0", "--length", src).stdout)
    assert abs(at48 - 7.68) < 0.03 and at44 < at48 * 0.999


@needs_binary
def test_mutated_modules_render_or_are_refused_never_crash(tmp_path):
    import random
    rng = random.Random(11)
    base = rs.dsik_tune()
    seen = set()
    for i in range(60):
        b = bytearray(base)
        for _ in range(rng.randrange(1, 6)):
            b[rng.randrange(4, len(b))] = rng.randrange(256)
        src = tmp_path / f"m{i}.dsm"
        src.write_bytes(bytes(b))
        out = tmp_path / f"m{i}.wav"
        r = _run("-m", "20", src, out, timeout=30)
        assert r.returncode in (0, 1), (i, r.returncode, r.stderr)
        seen.add(r.returncode)
        if r.returncode == 0:
            length = float(_run("-m", "20", "--length", src).stdout)
            assert abs(_secs(out) - length) < 0.0006, i
    assert seen == {0, 1}                                    # both outcomes exercised


def test_a_milkyrender_play_proves_milkyrender_not_openmpt123():
    import asyncio

    async def one_render():
        unplayable.renderer_ran("milkyrender", "tracker")
        unplayable.rendered_ok("tracker")
    unplayable.reset_for_tests()
    try:
        unplayable._streaks["milkyrender"] = ["a", "b"]
        unplayable._tripped.add("milkyrender")
        asyncio.run(one_render())
        assert set(unplayable._proven) == {"milkyrender"}
        assert "milkyrender" not in unplayable._tripped
        assert "milkyrender" not in unplayable._streaks
        # (it stayed in that render's task: a tracker render elsewhere is openmpt123's)
        unplayable.rendered_ok("tracker")
        assert set(unplayable._proven) == {"milkyrender", "openmpt123"}
    finally:
        unplayable.reset_for_tests()


# ── the library scan ────────────────────────────────────────────────────────

@pytest.mark.parametrize("installed", [True, False], ids=["milkyrender", "none"])
def test_scan_names_the_format_and_stores_milkyrenders_length(tmp_path, monkeypatch, installed):
    from soniqboom.core import metadata
    if installed and not _binary():
        pytest.skip("milkyrender not built here")
    monkeypatch.setattr(milky, "find", lambda: _binary() if installed else None)
    src = tmp_path / "t.dsm"
    tune = bytearray(rs.dsik_tune())
    tune[4:4 + 12] = b'"Selftest"\0\0'
    src.write_bytes(bytes(tune))
    d = metadata._extract_tracker(src, "t1")
    assert d["format"] == "DSIK" and d["title"] == "Selftest"
    assert d.get("channels") == 1 and d.get("patterns") == 1
    assert d.get("instruments") == ["square"]
    if installed:
        assert abs(d["duration"] - 7.68) < 0.03
    else:
        assert d["duration"] == 0.0


@needs_binary
def test_the_scan_marks_a_module_with_cut_short_sample_data_partial(tmp_path, monkeypatch):
    """A module milkyrender plays with its last sample cut short is scanned
    as "partial" (the missing bytes named); the whole module is not."""
    from soniqboom.core import metadata
    monkeypatch.setattr(milky, "find", lambda: _binary())
    whole = rs.dsik_tune()
    ok = tmp_path / "whole.dsm"
    ok.write_bytes(whole)
    cut = tmp_path / "cut.dsm"
    cut.write_bytes(whole[:-20])
    d_ok = metadata._extract_tracker(ok, "t-whole")
    d_cut = metadata._extract_tracker(cut, "t-cut")
    assert d_ok["duration"] > 0 and "defect" not in d_ok
    assert d_cut["duration"] == d_ok["duration"]
    assert d_cut["defect"] == "partial"
    assert "20 bytes missing" in d_cut["defect_detail"]
