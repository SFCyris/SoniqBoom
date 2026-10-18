# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Long tracker renders, and modules that end in a loop repeated as written.

Aminet's "Jonahara" (Face The Music) sets its closing 32 measures to repeat
63 times: libopenmpt lists it as 65:56 (5:02 of music, then 62 sample-
identical repeats), and openmpt123's default output made that a 1.5 GB WAV.
A render larger than the conversion cache's budget was evicted by its own
store, and its play failed (500, a missing file).  Covers:

- the conversion cache never evicts the entry it has just stored;
- the song-end loop rule (``openmpt_vu.song_end_loop``): an order list that
  ends in one block played 8+ times back to back, whose passes render the
  same sample for sample, ends where the block first repeats — fewer
  passes, passes that change and other subsongs play as written;
- the tracker render: libopenmpt's 32-bit float at 48 kHz, copied into our
  own float WAV (the live path takes float), with libopenmpt's 100 ms end
  fade kept, a loud module unclipped, stopped at the loop point with that
  fade, the same file whether or not a listener plays it live — and a
  renderer that rewrites its header before its first sample (openmpt123)
  never has header bytes copied as audio.

The modules are built here (a Face The Music module with a counted loop, a
few ProTracker modules); the real Aminet file is never part of the suite."""
from __future__ import annotations

import array
import math
import shutil
import struct
import sys
import wave
from collections import OrderedDict
from pathlib import Path

import pytest

from soniqboom.core import conversion_cache as cc
from soniqboom.core import metadata, openmpt_vu

needs_lib = pytest.mark.skipif(not openmpt_vu.is_available(),
                               reason="libopenmpt can't be loaded on this host")
needs_ftm = pytest.mark.skipif(".ftm" not in getattr(metadata, "OPENMPT_EXTRA_EXTS", {".ftm"}),
                               reason="the installed libopenmpt doesn't read Face The Music")
needs_cli = pytest.mark.skipif(shutil.which("openmpt123") is None,
                               reason="openmpt123 not installed")


# ── modules ──────────────────────────────────────────────────────────────────

def _ftm(count: int, *, measures: int = 4, loop_at: int = 2) -> bytes:
    """A Face The Music module (FTMN v3, one sample, 24 rows a measure) whose
    measures ``loop_at``… end in a loop played ``count`` times: channel 1
    holds the loop events (``E`` with the count in bits 6–11 at the loop's
    first row, ``E000`` on its last row — the layout Jonahara has), channel
    2 a note every 4 rows."""
    wave_ = bytes(([100] * 8 + [156] * 8) * 256)
    h = (b"FTMN" + bytes([3, 1]) + struct.pack(">HH", measures, 0x2000)
         + bytes([0, 0xFF, 63, 1, 4, 24]) + b"Loop".ljust(32, b"\0")
         + b"SSilk".ljust(32, b"\0") + bytes([0, 0]))
    rows, start = measures * 24, loop_at * 24
    ctl = (struct.pack(">HH", 0xF000 | start, 0xE000 | (count << 6))
           + struct.pack(">HH", 0xF000 | (rows - start - 2), 0xE000))
    notes = bytes([0xA0, 0x4D, 0xF0, 0x03]) * (rows // 4)
    body = b"".join(struct.pack(">hI", 0, len(c)) + c for c in [ctl, notes] + [b""] * 6)
    return (h + b"Inst".ljust(30, b"\0") + b"\0\0" + body
            + struct.pack(">HH", 0, len(wave_) // 2) + wave_)


def _mod(orders: list, patterns: list) -> bytes:
    """A 4-channel ProTracker module: ``patterns`` maps ``(row, channel)`` to
    ``(sample, period, effect, param)``; sample 1 is a looped square wave."""
    smp = bytes(([100] * 16 + [156] * 16) * 64)
    hdr = (b"Synth".ljust(20, b"\0") + b"Square".ljust(22, b"\0")
           + struct.pack(">HBBHH", len(smp) // 2, 0, 64, 0, len(smp) // 2)
           + (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
           + bytes([len(orders), 0x7F]) + bytes(orders).ljust(128, b"\0") + b"M.K.")
    body = bytearray()
    for p in patterns:
        for row in range(64):
            for ch in range(4):
                s, per, eff, par = p.get((row, ch), (0, 0, 0, 0))
                body += bytes([(s & 0xF0) | (per >> 8), per & 0xFF, ((s & 0x0F) << 4) | eff, par])
    return hdr + bytes(body) + smp


_NOTE = {(0, 0): (1, 856, 0, 0)}


# ── the conversion cache ────────────────────────────────────────────────────

@pytest.fixture
def small_cache(tmp_data_dir, tmp_path, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", 10_000)
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", OrderedDict())
    monkeypatch.setattr(cc, "_total_bytes", 0)
    monkeypatch.setattr(cc, "_pin_refs", {})
    monkeypatch.setattr(cc, "_pending_purge", {})
    return tmp_path


def _wav(path: Path, frames: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes((b"\x00\x40\x00\x40" * 8 + b"\x00\xc0\x00\xc0" * 8) * (frames // 16)
                      + b"\x00\x40\x00\x40" * (frames % 16))          # a square wave
    return path


async def test_a_render_bigger_than_the_cache_budget_is_kept_for_its_play(small_cache):
    """Before: the store evicted everything over budget — the new entry too —
    so get_or_render handed back a path that no longer existed (the play's
    500).  Now the entry just stored stays; older ones go."""
    old = await cc.store_cached("old__sub0", "tracker", _wav(small_cache / "o.wav", 1000))
    assert old.exists()

    async def render():
        return _wav(small_cache / "big.wav", 20_000)          # 80 KB > the 10 KB budget
    path, hit = await cc.get_or_render(track_id="big", format_type="tracker", subsong=0,
                                       render_fn=render)
    assert hit is False and path.exists() and path.stat().st_size > 10_000
    assert await cc.get_cached(cc._cache_key("big", "tracker", 0)) == path
    assert not old.exists()                                    # the older entry made room
    # The next store may evict it (nothing pins it) — a reader's pin defers that.
    cc.pin(cc._cache_key("big", "tracker", 0))
    try:
        await cc.store_cached("next__sub0", "tracker", _wav(small_cache / "n.wav", 100))
        assert path.exists()
    finally:
        cc.unpin(cc._cache_key("big", "tracker", 0))
    assert not path.exists()                                   # unlinked once unpinned


async def test_a_cold_store_bigger_than_the_budget_is_kept_too(small_cache):
    p = await cc.store_cached("probe__sub0", "uade", _wav(small_cache / "p.wav", 20_000),
                              cold=True)
    assert p.exists() and await cc.get_cached("probe__sub0") == p


# ── the rule, on order lists ─────────────────────────────────────────────────

def test_the_closing_block_repeated_eight_times_or_more_is_found():
    jonahara = list(range(164)) + list(range(132, 164)) * 62
    assert openmpt_vu.terminal_block(jonahara, 164) == (132, 32, 63)
    assert openmpt_vu.terminal_block([0, 1] + [2, 3] * 8, 4) == (2, 2, 8)
    assert openmpt_vu.terminal_block([0] + [5] * 9, 6) == (1, 1, 9)
    # Seven passes, more music after the repeats, a marker in them: no.
    assert openmpt_vu.terminal_block([0, 1] + [2, 3] * 7, 4) is None
    assert openmpt_vu.terminal_block([0] + [1, 2] * 9 + [3], 4) is None
    assert openmpt_vu.terminal_block([0] + [1, 255] * 9, 4) is None
    assert openmpt_vu.terminal_block([7] * 9, 4) is None              # no such pattern


# ── the rule, rendered ───────────────────────────────────────────────────────

@needs_lib
@needs_ftm
def test_a_loop_counted_63_times_ends_where_it_first_repeats():
    data = _ftm(63)
    cut = openmpt_vu.song_end_loop(data)
    plan = openmpt_vu.render_plan(data, 0)
    assert cut is not None and 4.0 < cut < 5.0              # two measures, then the loop once
    assert openmpt_vu.module_duration(data) == openmpt_vu.duration_ms(cut) / 1000
    assert plan == (cut, cut)
    # libopenmpt's own length plays the 63 passes (2:21).
    full = openmpt_vu.render_plan(data, 1)
    assert full is not None and full[0] > 140 and full[1] is None   # (subsong 1: never cut)


@needs_lib
@needs_ftm
@pytest.mark.parametrize("count", [0, 2, 7])
def test_a_loop_of_fewer_than_eight_passes_plays_as_written(count):
    data = _ftm(count)
    assert openmpt_vu.song_end_loop(data) is None
    seconds, end_at = openmpt_vu.render_plan(data, 0)
    assert end_at is None and openmpt_vu.module_duration(data) == pytest.approx(seconds, abs=0.001)


@needs_lib
def test_repeats_that_change_play_out_and_identical_ones_are_cut():
    same = _mod([0] + [1] * 12, [_NOTE, {(0, 0): (1, 428, 0, 0), (32, 0): (1, 214, 0, 0)}])
    slide = _mod([0] + [1] * 12, [_NOTE, {(r, 0): (0, 0, 1, 1) for r in range(64)}])
    fade = _mod([0] + [1] * 12, [{(0, 0): (1, 428, 0, 0)},
                                 {(r, 0): (0, 0, 0xA, 0x01) for r in range(0, 64, 8)}])
    few = _mod([0] + [1] * 5, [_NOTE, {(0, 0): (1, 428, 0, 0)}])
    cut = openmpt_vu.song_end_loop(same)
    assert cut == pytest.approx(15.36, abs=0.01)             # pattern 0 + one pass of pattern 1
    assert openmpt_vu.song_end_loop(slide) is None            # the pitch keeps rising
    assert openmpt_vu.song_end_loop(fade) is None             # the volume keeps falling
    assert openmpt_vu.song_end_loop(few) is None
    assert openmpt_vu.song_end_loop(same, subsong=1) is None
    assert openmpt_vu.song_end_loop(b"not a module") is None


@needs_lib
@needs_ftm
def test_the_vu_sidecar_covers_the_rendered_length():
    r = openmpt_vu.extract_vu(_ftm(63))
    assert r is not None and r.frames == pytest.approx(4.42 * r.sample_rate, abs=2)


# ── the render ───────────────────────────────────────────────────────────────

def _read_wav(p: Path) -> dict:
    """The fmt facts and samples of a WAV (16-bit PCM or 32-bit float —
    the stdlib ``wave`` reads no float)."""
    d = p.read_bytes()
    assert d[:4] == b"RIFF" and d[8:12] == b"WAVE"
    pos, f = 12, {}
    while pos + 8 <= len(d):
        cid, size = d[pos:pos + 4], int.from_bytes(d[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            tag, ch, rate = struct.unpack("<HHI", d[pos + 8:pos + 16])
            f = {"tag": tag, "channels": ch, "rate": rate,
                 "bits": int.from_bytes(d[pos + 22:pos + 24], "little")}
        elif cid == b"data":
            a = array.array("f" if f["tag"] == 3 else "h")
            a.frombytes(d[pos + 8:pos + 8 + size])
            f.update(offset=pos + 8, samples=a,
                     seconds=len(a) / f["channels"] / f["rate"])
            return f
        pos += 8 + size + (size & 1)
    raise AssertionError("no data chunk")


@needs_cli
@needs_lib
@needs_ftm
@pytest.mark.parametrize("live", [False, True])
async def test_the_render_stops_at_the_loop_point_with_libopenmpts_fade(
        tmp_data_dir, tmp_path, monkeypatch, live):
    """Float, 48 kHz; the loop point plus libopenmpt's 100 ms end fade (no
    click); the same length whether a listener plays it live or not (a
    cast's render and the web player's share one cache key)."""
    from soniqboom.api import stream
    src = tmp_path / "Loop.ftm"
    src.write_bytes(_ftm(63))
    cut = openmpt_vu.song_end_loop(src.read_bytes())
    told = {}
    real = stream._tail_render

    async def spy(cmd, src_wav, live_key, **kw):
        told.update(kw, cmd=cmd, key=live_key)
        return await real(cmd, src_wav, live_key, **kw)
    monkeypatch.setattr(stream, "_tail_render", spy)
    key = "loop-live-key" if live else None
    out = await stream._render_tracker(src, 0, live_key=key)
    try:
        w = _read_wav(out)
        assert (w["tag"], w["rate"], w["bits"], w["channels"]) == (3, 48000, 32, 2)
        assert w["offset"] == 44                                  # our own canonical header
        assert w["seconds"] == pytest.approx(cut + 0.1, abs=0.002)   # not the 2:21 of 63 passes
        assert told["key"] == key and told["expected_seconds"] == pytest.approx(cut + 0.1)
        assert (told["stop_after_s"], told["fade_s"]) == (pytest.approx(cut + 0.1), 0.1)
        a = w["samples"]
        assert max(abs(x) for x in a[-2 * 48:]) < 0.03           # faded out: no click
        assert max(abs(x) for x in a[2 * 48000:4 * 48000]) > 0.1   # the music before it
        assert out.with_suffix(".vu").exists()
        if live:
            entry = stream._UADE_LIVE.get(key)
            assert entry is not None and entry["complete"].is_set() and entry["clean_exit"]
    finally:
        if key:
            stream._UADE_LIVE.pop(key, None)
        out.unlink(missing_ok=True)
        out.with_suffix(".vu").unlink(missing_ok=True)


@needs_cli
@needs_lib
async def test_a_module_keeps_libopenmpts_end_fade(tmp_data_dir, tmp_path):
    """A module that just ends: its render is libopenmpt's length plus the
    100 ms fade, and the fade is in it (before: a live render was trimmed to
    the length, cutting the fade into a click)."""
    from soniqboom.api import stream
    data = _mod([0] + [1] * 5, [_NOTE, {(0, 0): (1, 428, 0, 0)}])
    src = tmp_path / "few.mod"
    src.write_bytes(data)
    full = openmpt_vu.render_plan(data, 0)
    out = await stream._render_tracker(src, 0, live_key="few-live-key")
    try:
        w = _read_wav(out)
        assert w["seconds"] == pytest.approx(full[0] + 0.1, abs=0.005)
        assert max(abs(x) for x in w["samples"][-2 * 48:]) < 0.03
    finally:
        stream._UADE_LIVE.pop("few-live-key", None)
        out.unlink(missing_ok=True)
        out.with_suffix(".vu").unlink(missing_ok=True)


_REWRITES_ITS_HEADER = r"""
import struct, sys, time
out, flt = sys.argv[1], sys.argv[2] == "float"
def hdr(extra, data_bytes):
    fmt = (struct.pack("<4sIHHIIHH", b"fmt ", 16, 3, 2, 48000, 384000, 8, 32) if flt
           else struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 2, 44100, 176400, 4, 16))
    body = fmt + extra + b"data" + struct.pack("<I", data_bytes)
    return b"RIFF" + struct.pack("<I", 4 + len(body) + data_bytes) + b"WAVE" + body
with open(out, "wb") as f:                 # a bare header first ...
    f.write(hdr(b"", 0)); f.flush()
time.sleep(0.15)
info = b"INFOINAM" + struct.pack("<I", 300) + b"x" * 300
lst = b"LIST" + struct.pack("<I", len(info)) + info
pcm = bytes(range(256)) * 4096            # 1 MB of "audio"
with open(out, "r+b") as f:                # ... rewritten with a LIST chunk
    f.write(hdr(lst, 0)); f.flush()
    for i in range(0, len(pcm), 65536):
        f.write(pcm[i:i + 65536]); f.flush(); time.sleep(0.01)
    f.seek(0); f.write(hdr(lst, len(pcm)))
"""


@pytest.mark.parametrize("fmt", ["s16", "float"])
async def test_a_header_rewritten_before_the_first_sample_is_never_copied_as_audio(tmp_path, fmt):
    """openmpt123 writes a bare 44-byte header, then rewrites it with its
    LIST chunk before any sample — the audio starts at byte 360, not 44.
    Before: the live copy latched offset 44 and cached the LIST bytes as the
    first frames of the tune (a full-scale click)."""
    from soniqboom.api import stream
    script = tmp_path / "renderer.py"
    script.write_text(_REWRITES_ITS_HEADER)
    src = tmp_path / "out.wav"
    kw = {"float_out": True, "rate": 48000} if fmt == "float" else {}
    out = await stream._tail_render([sys.executable, str(script), str(src), fmt], src,
                                    "rewrite-live-key", kind="FAKE", timeout=30,
                                    require_audio=False, **kw)
    try:
        body = out.read_bytes()
        assert body[44:44 + 1024] == bytes(range(256)) * 4
        assert len(body) == 44 + 256 * 4096
        assert b"INFO" not in body[:4096]
        assert body[20:22] == (b"\x03\x00" if fmt == "float" else b"\x01\x00")
    finally:
        stream._UADE_LIVE.pop("rewrite-live-key", None)
        out.unlink(missing_ok=True)


@needs_lib
def test_a_sample_ringing_across_the_repeats_is_not_cut(monkeypatch):
    """A long sample struck once keeps sounding through the repeated passes:
    the passes differ, and the rule leaves the module alone — also where a
    library would seek without rebuilding what still sounds (the rule sets
    ``seek.sync_samples`` itself)."""
    import ctypes
    n = 120000
    chirp = bytes((int(100 * math.sin(2 * math.pi * (0.002 + 5e-8 * i) * i)) & 0xFF)
                  for i in range(n))
    smp_hdr = b"Chirp".ljust(22, b"\0") + struct.pack(">HBBHH", n // 2, 0, 64, 0, 1)
    hdr = (b"Synth".ljust(20, b"\0") + smp_hdr
           + (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
           + bytes([13, 0x7F]) + bytes([0] + [1] * 12).ljust(128, b"\0") + b"M.K.")
    pats = [{(0, 0): (1, 856, 0, 0), (15, 1): (0, 0, 0xD, 0)}, {(15, 1): (0, 0, 0xD, 0)}]
    body = bytearray()
    for p in pats:
        for row in range(64):
            for ch in range(4):
                s_, per, eff, par = p.get((row, ch), (0, 0, 0, 0))
                body += bytes([(s_ & 0xF0) | (per >> 8), per & 0xFF, ((s_ & 0x0F) << 4) | eff, par])
    data = hdr + bytes(body) + chirp
    assert openmpt_vu.song_end_loop(data) is None
    lib = openmpt_vu._load()
    off = lib.openmpt_module_ctl_set_boolean
    off.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    real = openmpt_vu._open_silent

    def without_sync(lib_, b):
        m = real(lib_, b)
        if m:
            off(m, b"seek.sync_samples", 0)
        return m
    monkeypatch.setattr(openmpt_vu, "_open_silent", without_sync)
    assert openmpt_vu.song_end_loop(data) is None


async def test_a_stale_tracker_render_is_not_reused(tmp_data_dir):
    """Renders made before this revision (float 48 kHz, a song-end loop
    played out) have another key: never served, nor read back as a length."""
    assert cc._cache_key("t", "tracker", 0) == "t__sub0__r2"


@needs_cli
@needs_lib
@needs_ftm
def test_a_catalogue_tune_read_on_its_first_play_gets_the_rendered_length(tmp_data_dir, tmp_path):
    """The server reads a catalogue tune (an opened Aminet package's member)
    with ``openmpt123 --info`` — which plays the loop out — and then asks
    libopenmpt for the song-end loop, so the list and Track Info show the
    length the render has (5:02, not 65:56 — here 4.4 s, not 2:21)."""
    from soniqboom.core import modland_catalog
    local = tmp_path / "3f9a.ftm"                     # the cached copy's own name
    local.write_bytes(_ftm(63))
    d = modland_catalog._extract_copy(local, "tid-loop", "Loop.ftm")
    assert d["format"] == "Face The Music"
    assert d["duration"] == openmpt_vu.duration_ms(openmpt_vu.song_end_loop(local.read_bytes())) / 1000
    local.write_bytes(_ftm(7))                        # a loop played 7 times stays as written
    assert modland_catalog._extract_copy(local, "tid-7", "Seven.ftm")["duration"] > 17


# ── float all the way ────────────────────────────────────────────────────────

def test_float_wav_headers_and_the_audibility_meter():
    from soniqboom.api import stream
    from soniqboom.core.silence import AudibilityMeter
    h = stream._build_wav_header(48000, 2, 1000, bits_per_sample=32, float_=True)
    assert len(h) == 44 and h[20:22] == b"\x03\x00"
    assert struct.unpack("<IHH", h[28:36]) == (384000, 8, 32)
    assert int.from_bytes(h[40:44], "little") == 8000
    st = stream._streaming_wav_header(48000, 2, 32, float_=True)
    assert st[20:22] == b"\x03\x00" and st[40:44] == b"\xff\xff\xff\xff"
    assert stream._build_wav_header(44100, 2, 10, bits_per_sample=16)[20:22] == b"\x01\x00"
    m = AudibilityMeter(2, 48000, kind="f")
    assert m.feed(array.array("f", [0.0001, -0.0001] * 600).tobytes()) is False
    assert m.feed(array.array("f", [0.5, -0.5, -0.5, 0.5] * 300).tobytes()) is True
    assert m.frames == 1200
    faded = stream._fade_pcm(array.array("f", [1.0, -1.0] * 4).tobytes(), 0, 4, 2, float_=True)
    assert list(array.array("f", faded)) == [1.0, -1.0, 0.75, -0.75, 0.5, -0.5, 0.25, -0.25]


@needs_cli
@needs_lib
async def test_a_loud_module_is_not_clipped(tmp_data_dir, tmp_path):
    """Four channels of a full-scale square wave mix past full scale: the
    float render keeps those peaks (a 16-bit render held 34 % of its samples
    flat at full scale)."""
    from soniqboom.api import stream
    sq = bytes(([127] * 16 + [0x80] * 16) * 64)
    hdr = (b"Loud".ljust(20, b"\0") + b"Sq".ljust(22, b"\0")
           + struct.pack(">HBBHH", len(sq) // 2, 0, 64, 0, len(sq) // 2)
           + (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
           + bytes([1, 0x7F]) + bytes([0]).ljust(128, b"\0") + b"M.K.")
    body = bytearray()
    for row in range(64):
        for ch in range(4):
            body += bytes([0, 428 & 0xFF, 0x10, 0]) if row == 0 else b"\0" * 4
    body[0:4] = bytes([0x01, 0xAC, 0x10, 0])           # (period 428 = 0x1AC)
    for ch in range(1, 4):
        body[ch * 4:ch * 4 + 4] = bytes([0x01, 0xAC, 0x10, 0])
    src = tmp_path / "loud.mod"
    src.write_bytes(hdr + bytes(body) + sq)
    out = await stream._render_tracker(src, 0, live_key="loud-live-key")
    try:
        w = _read_wav(out)
        a = w["samples"]
        assert w["tag"] == 3
        peak = max(abs(x) for x in a)
        assert peak > 1.05                                   # past full scale, kept
        # (16-bit stops at full scale: there, every one of these was cut flat)
        assert sum(1 for x in a if abs(x) > 1.0) > len(a) // 10
    finally:
        stream._UADE_LIVE.pop("loud-live-key", None)
        out.unlink(missing_ok=True)
        out.with_suffix(".vu").unlink(missing_ok=True)


async def test_a_float_live_wav_of_unknown_length_streams_a_float_header(tmp_data_dir):
    """Length unknown: the file and the feed a transcode reads carry a float
    "read to the end" header; partial frames are carried over; the finished
    file gets the real float header."""
    from soniqboom.api import stream
    key = "float-live-unknown"
    lw = stream._LiveWav(key, rate=48000, float_=True)
    try:
        head = lw.path.read_bytes()[:44]
        assert head[20:22] == b"\x03\x00" and head[34:36] == b"\x20\x00"
        assert head[40:44] == b"\xff\xff\xff\xff"
        tone = array.array("f", [0.5, -0.5, -0.5, 0.5] * 6000).tobytes()
        lw.feed(tone[:13])                                   # a partial frame first
        lw.feed(tone[13:])
        live = stream._UADE_LIVE[key]
        assert live["audible"] and live["float"] and live["bits"] == 32
        assert live["bytes_per_sec"] == 384000
        feed = await stream._live_feed_of(live)
        first = await feed.__anext__()
        assert first[:4] == b"RIFF" and first[20:22] == b"\x03\x00" and first[40:44] == b"\xff" * 4
        out = lw.finish()
        lw.close()
        rest = b"".join([first[44:]] + [c async for c in feed])
        assert rest == tone
        w = _read_wav(out)
        assert (w["tag"], w["bits"], w["rate"]) == (3, 32, 48000)
        assert w["seconds"] == pytest.approx(12000 / 48000)
    finally:
        stream._UADE_LIVE.pop(key, None)
        lw.path.unlink(missing_ok=True)
