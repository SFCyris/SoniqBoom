# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amiga-accurate modules, stereo width, multi-SID stereo, loops / endings and
endless play (2.3.0 — ``core/render_options.py``, ``core/endless.py``).

Real renderers (openmpt123, sidplayfp, uade123, libgme, ffmpeg) on the local
test files and on tunes made here (a PSID for one, two or three SID chips,
VGMs with and without a loop, an NSFe with a ``fade`` chunk); each test that
needs a binary skips without it.  In-process only (httpx ASGITransport, a
throw-away conversion cache) — never a live server or real data.
"""
from __future__ import annotations

import array
import asyncio
import hashlib
import math
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.config import settings
from soniqboom.core import conversion_cache as cc
from soniqboom.core import render_options as ro

from test_render_r1_stream import _T, _app, _client
from test_render_r2_stream import _register, _unregister

REPO = Path(__file__).resolve().parent.parent
TD = REPO / "internal/testdata"
MOD = TD / "tracker/actionplatform.mod"          # ProTracker, 96.0 s
VGZ = TD / "game/09.vgz"                         # VGM with a loop
NSF = TD / "gme/8bp028-b1-nullsleep-axel_f.nsf"
JAM = TD / "uade/JamCracker/bump set spike.jam"
SID1 = TD / "sid/SX-64_Demo.sid"

needs_openmpt = pytest.mark.skipif(not shutil.which("openmpt123") or not MOD.exists(),
                                   reason="openmpt123 or the local test module missing")
needs_sid = pytest.mark.skipif(not shutil.which("sidplayfp"), reason="sidplayfp missing")
needs_uade = pytest.mark.skipif(not shutil.which("uade123") or not JAM.exists(),
                                reason="uade123 or the local JamCracker module missing")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


def _gme_ok() -> bool:
    from soniqboom.core import gme_render
    return gme_render.is_available()


needs_gme = pytest.mark.skipif(not _gme_ok(), reason="libgme missing")


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Every test starts from the shipped defaults."""
    for k, v in (("amiga_resampler", "auto"), ("stereo_width", 100), ("tracker_loops", 0),
                 ("tracker_fade", -1.0), ("chip_loops", 0), ("chip_fade", -1.0),
                 ("sid_fade", 0.0)):
        monkeypatch.setattr(settings, k, v)


# ── WAV helpers ─────────────────────────────────────────────────────────────

def _wav(path_or_bytes):
    b = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(b):
        cid, size = b[pos:pos + 4], struct.unpack("<I", b[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", b[pos + 8:pos + 24])
        elif cid == b"data":
            data = b[pos + 8:pos + 8 + min(size, len(b) - pos - 8)]
            break
        pos += 8 + size + (size & 1)
    tag, ch, rate, _br, _ba, bits = fmt
    a = array.array("f" if tag == 3 else "h")
    a.frombytes(data[:len(data) // a.itemsize * a.itemsize])
    return {"tag": tag, "ch": ch, "rate": rate, "bits": bits, "data": data, "s": a,
            "secs": len(data) / (ch * bits // 8) / rate}


def _ms(w, t0=0.0, t1=None):
    """``(side/mid rms ratio, L/R correlation)`` of stereo ``w`` (DC removed)."""
    a, rate = w["s"], w["rate"]
    L, R = a[0::2], a[1::2]
    i0, i1 = int(t0 * rate), int(t1 * rate) if t1 else len(L)
    L, R = L[i0:i1], R[i0:i1]
    n = len(L)
    ml, mr = sum(L) / n, sum(R) / n
    sm = ss = sl = sr = slr = 0.0
    for i in range(0, n, 3):
        lv, rv = float(L[i]) - ml, float(R[i]) - mr
        m, s = (lv + rv) / 2, (lv - rv) / 2
        sm += m * m; ss += s * s; sl += lv * lv; sr += rv * rv; slr += lv * rv
    return math.sqrt(ss / sm), slr / math.sqrt(sl * sr)


def _rms(a, ch, rate, t0, t1):
    seg = a[int(t0 * rate) * ch:int(t1 * rate) * ch]
    return math.sqrt(sum(float(x) * float(x) for x in seg[::7]) / max(1, len(seg[::7])))


# ── made tunes ──────────────────────────────────────────────────────────────

def _psid(chips: int, *, version=None, addr=True) -> bytes:
    """A PSID whose init sets one held voice per SID chip (distinct waveform
    and pitch) — chip 2 at $D420, chip 3 at $D440."""
    def lda_sta(v, a):
        return bytes([0xA9, v, 0x8D, a & 0xFF, a >> 8])
    load, code = 0x1000, bytearray()
    for base, fhi, ctrl in ((0xD400, 0x1D, 0x11), (0xD420, 0x2B, 0x21), (0xD440, 0x13, 0x41))[:chips]:
        code += lda_sta(0x0F, base + 0x18) + lda_sta(0, base) + lda_sta(fhi, base + 1)
        code += lda_sta(0, base + 2) + lda_sta(8, base + 3) + lda_sta(9, base + 5)
        code += lda_sta(0xF0, base + 6) + lda_sta(ctrl, base + 4)
    code += b"\x60"
    play = load + len(code)
    code += b"\x60"
    ver = version or (2 if chips == 1 else 3 if chips == 2 else 4)
    h = bytearray(0x7C)
    h[0:4] = b"PSID"
    struct.pack_into(">HHHHHHHI", h, 4, ver, 0x7C, 0, load, play, 1, 1, 0)
    h[0x16:0x1D] = b"SB test"
    flags = (1 << 2) | (1 << 4) | ((1 << 6) if chips >= 2 else 0) | ((1 << 8) if chips >= 3 else 0)
    struct.pack_into(">H", h, 0x76, flags)
    if addr and chips >= 2:
        h[0x7A] = 0x42
    if addr and chips >= 3:
        h[0x7B] = 0x44
    return bytes(h) + struct.pack("<H", load) + bytes(code)


def _vgm(secs=6.0, loop_at=None) -> bytes:
    """A SN76489 VGM of ``secs`` (a tone sequence), looping from ``loop_at``."""
    body, total, loop_off, loop_start = bytearray([0x50, 0x90]), 0, None, 0
    step = 22050
    for k in range(int(secs / 0.5)):
        if loop_at is not None and loop_off is None and k * 0.5 >= loop_at:
            loop_off, loop_start = len(body), total
        f = (0x0FE, 0x0D6, 0x0BE, 0x0A0)[k % 4]
        # (the volume with each note: a loop starts after the closing mute)
        body += bytes([0x50, 0x90, 0x50, 0x80 | (f & 0xF), 0x50, (f >> 4) & 0x3F, 0x61]) \
            + struct.pack("<H", step)
        total += step
    body += b"\x50\x9f\x66"
    h = bytearray(0x40)
    h[0:4] = b"Vgm "
    struct.pack_into("<IIII", h, 4, 0x40 + len(body) - 4, 0x150, 3579545, 0)
    struct.pack_into("<I", h, 0x18, total)
    if loop_off is not None:
        struct.pack_into("<II", h, 0x1C, 0x40 + loop_off - 0x1C, total - loop_start)
    struct.pack_into("<IHB", h, 0x24, 60, 9, 16)
    struct.pack_into("<I", h, 0x34, 0x40 - 0x34)
    return bytes(h) + bytes(body)


def _nsfe(nsf: bytes, times=None, fades=None, plst=None) -> bytes:
    def chunk(cid, d):
        return struct.pack("<I", len(d)) + cid + d
    load, init, play = struct.unpack_from("<HHH", nsf, 8)
    out = b"NSFE" + chunk(b"INFO", struct.pack("<HHHBBBB", load, init, play, nsf[122], nsf[123],
                                                 nsf[6], max(0, nsf[7] - 1)))
    if any(nsf[112:120]):
        out += chunk(b"BANK", nsf[112:120])
    if times is not None:
        out += chunk(b"time", b"".join(struct.pack("<i", t) for t in times))
    if fades is not None:
        out += chunk(b"fade", b"".join(struct.pack("<i", t) for t in fades))
    if plst is not None:
        out += chunk(b"plst", bytes(plst))
    return out + chunk(b"DATA", nsf[128:]) + chunk(b"NEND", b"")


# ═══ F1a — the Amiga (Paula) resampler ═════════════════════════════════════

def test_amiga_resampler_setting_args_and_key_parts(monkeypatch):
    assert ro.amiga_resampler() == "auto"                        # (on by default)
    assert ro.openmpt_ctl_args() == ["--ctl", "render.resampler.emulate_amiga=1",
                                     "--ctl", "render.resampler.emulate_amiga_type=auto"]
    keys = {}
    for t in ("auto", "a500", "a1200", "off", "bogus"):
        monkeypatch.setattr(settings, "amiga_resampler", t)
        keys[t] = cc._cache_key("t", "tracker", 0)
    assert keys["off"] == "t__sub0__r3"                          # (r3: one subsong per tune, test_every_tune)
    assert keys["auto"] == "t__sub0__r3__amauto"
    assert len({keys["auto"], keys["a500"], keys["a1200"], keys["off"]}) == 4
    assert keys["bogus"] == keys["auto"]                         # (unknown: the default)
    monkeypatch.setattr(settings, "amiga_resampler", "off")
    assert ro.openmpt_ctl_args() == []
    # The VU sidecar lookup follows the render's key.
    assert cc._cache_key("t", "tracker", 0) == "t__sub0__r3"


@needs_openmpt
async def test_the_paula_resampler_changes_the_render_and_off_is_the_old_one(monkeypatch):
    """Measured on a ProTracker module: the render without the setting's
    ``--ctl`` (openmpt123's default, the cached render before 2.3.0) equals
    ``off``; auto / a1200 / a500 each differ from it (libopenmpt 0.8.9 picks
    the A1200 filter for ProTracker under auto)."""
    md5 = {}
    for t in ("off", "auto", "a500", "a1200"):
        monkeypatch.setattr(settings, "amiga_resampler", t)
        out = await stream._render_tracker(MOD)
        try:
            w = _wav(out)
            md5[t] = hashlib.md5(w["data"]).hexdigest()
            assert w["tag"] == 3 and abs(w["secs"] - 96.1) < 0.01
        finally:
            out.unlink(missing_ok=True)
            out.with_suffix(".vu").unlink(missing_ok=True)
    plain = subprocess.run(["openmpt123", "--batch", "--quiet", "--float", "--samplerate", "48000",
                            "--stdout", "--", str(MOD)], capture_output=True, check=True).stdout
    assert hashlib.md5(plain).hexdigest() == md5["off"]
    assert md5["auto"] != md5["off"] and md5["a500"] != md5["off"]
    assert md5["auto"] == md5["a1200"] and md5["a500"] != md5["a1200"]


# ═══ F8 — tracker loops and fade ═══════════════════════════════════════════

def test_tracker_ending_defaults_and_key_parts(monkeypatch):
    assert ro.tracker_ending() == (0, None)
    monkeypatch.setattr(settings, "amiga_resampler", "off")
    assert cc._cache_key("t", "tracker", 0) == "t__sub0__r3"
    monkeypatch.setattr(settings, "tracker_loops", 1)
    monkeypatch.setattr(settings, "tracker_fade", 0.1)          # = the default ending
    assert ro.tracker_ending() == (0, None)
    monkeypatch.setattr(settings, "tracker_loops", 2)
    monkeypatch.setattr(settings, "tracker_fade", 2.5)
    assert ro.tracker_ending() == (2, 2.5)
    assert cc._cache_key("t", "tracker", 0) == "t__sub0__r3__l2__f2p5"   # (no dot in a file name)
    monkeypatch.setattr(settings, "tracker_loops", 99)
    assert ro.loops("tracker") == 0                              # out of range: the default


@needs_openmpt
async def test_a_loop_count_and_fade_render_the_passes_and_fade_them(monkeypatch):
    """``--repeat`` passes and the capture's fade: the length is planned from
    libopenmpt (the song's restart position — 0 here: 96.0 s a pass), and the
    last seconds fade to nothing."""
    from soniqboom.core import openmpt_vu
    once, restart = openmpt_vu.loop_plan(MOD.read_bytes(), 0)
    assert abs(once - 96.0) < 0.01 and restart == 0.0
    monkeypatch.setattr(settings, "amiga_resampler", "off")
    monkeypatch.setattr(settings, "tracker_loops", 2)
    monkeypatch.setattr(settings, "tracker_fade", 5)
    out = await stream._render_tracker(MOD)
    try:
        w = _wav(out)
        assert abs(w["secs"] - (once + (once - restart) + 5)) < 0.002   # 197.0 s
        a = w["s"]
        before = _rms(a, 2, 48000, w["secs"] - 10, w["secs"] - 6)
        last = _rms(a, 2, 48000, w["secs"] - 0.3, w["secs"])
        assert before > 0.05 and last < before * 0.1               # faded out
        # The VU sidecar covers the whole render (30 frames a second).
        vu = out.with_suffix(".vu").read_bytes()
        frames = int.from_bytes(vu[12:16], "little")
        assert abs(frames / 30 - w["secs"]) < 0.1
        # the second pass is the first one again (restart at 0) — compared 20 s
        # in, past what still rang on from the first pass's end
        p1 = a[20 * 48000 * 2:25 * 48000 * 2]
        p2 = a[(96 + 20) * 48000 * 2:(96 + 25) * 48000 * 2]
        assert sum(abs(x - y) for x, y in zip(p1[::97], p2[::97])) < 1e-3
    finally:
        out.unlink(missing_ok=True)
        out.with_suffix(".vu").unlink(missing_ok=True)


# ═══ F1c — multi-SID stereo ════════════════════════════════════════════════

def test_psid_chip_count_reads_the_addresses():
    from soniqboom.core.metadata import psid_chip_count
    assert psid_chip_count(_psid(1)) == 1
    assert psid_chip_count(_psid(2)) == 2
    assert psid_chip_count(_psid(3)) == 3
    # model bits without an address: one chip (what sidplayfp plays)
    assert psid_chip_count(_psid(2, addr=False)) == 1
    # a second address in a v2 header means nothing
    assert psid_chip_count(_psid(2, version=2)) == 1
    bad = bytearray(_psid(2)); bad[0x7A] = 0x41                  # odd: invalid
    assert psid_chip_count(bytes(bad)) == 1


def test_the_scan_stores_the_chip_count(tmp_path):
    from soniqboom.core.metadata import _extract_sid
    for n in (1, 2, 3):
        p = tmp_path / f"s{n}.sid"
        p.write_bytes(_psid(n))
        assert _extract_sid(p, f"s{n}")["channels"] == n


def test_sid_commands_keys_and_meter_passes():
    t1, t2, t3 = (_T(f"x{n}", "/x.sid", 60.0, fmt="SID", channels=n) for n in (1, 2, 3))
    assert not stream.sid_stereo(t1) and stream.sid_stereo(t2) and stream.sid_stereo(t3)
    assert stream._sid_key_variant(t1) is None and stream._sid_key_variant(t2) == "st"
    mono = stream._sid_render_cmd("sidplayfp", Path("/x.sid"), 0, 30, "/o.wav")
    st = stream._sid_render_cmd("sidplayfp", Path("/x.sid"), 0, 30, "/o.wav", stereo=True)
    assert "-m" in mono and "-s" not in mono
    assert "-s" in st and "-m" not in st
    # a voice-isolation pass is always mono
    vu = stream._sid_render_cmd("sidplayfp", Path("/x.sid"), 0, 30, "/o.wav", mute=(2, 3), stereo=True)
    assert "-m" in vu
    assert cc._cache_key("x", "sid", 0, duration=60) == "x__sub0__dur60"       # (as before)
    assert cc._cache_key("x", "sid", 0, duration=60, variant="st") == "x__sub0__dur60__st"
    from soniqboom.core import sid_vu
    assert sid_vu.voice_mutes(1) == sid_vu.VOICE_MUTES
    six = sid_vu.voice_mutes(2)
    assert len(six) == 6 and six[3] == (1, 2, 3, 5, 6) and len(sid_vu.voice_mutes(3)) == 9


@needs_sid
@pytest.mark.parametrize("chips", [1, 2, 3])
async def test_a_multi_sid_tune_renders_in_stereo(tmp_path, chips):
    """1 chip: mono, the very render of before (sidplayfp ``-m``); 2 / 3 chips:
    stereo whose channels differ (side/mid well above 0, correlation below 1)."""
    p = tmp_path / f"t{chips}.sid"
    p.write_bytes(_psid(chips))
    out = await stream._render_sid(p, 0, duration=5, stereo=chips >= 2)
    try:
        w = _wav(out)
        assert abs(w["secs"] - 5) < 0.001 and w["rate"] == 44100 and w["bits"] == 16
        if chips == 1:
            # mono, from the very command of before (sidplayfp ``-m`` — its
            # PCM differs run to run: the emulated chips power up randomly)
            assert w["ch"] == 1
        else:
            assert w["ch"] == 2
            sm, corr = _ms(w, 0.5, 4.5)
            assert sm > 0.2 and corr < 0.95, (sm, corr)
    finally:
        out.unlink(missing_ok=True)


@needs_sid
async def test_a_2sid_play_streams_a_stereo_wav_under_its_own_key(monkeypatch, tmp_path):
    """Through the stream endpoint: the progressive header says 2 channels and
    its length (Content-Length) is the stereo one; the render is cached under
    the ``st`` key, and an old mono render under the plain key is never served."""
    p = tmp_path / "two.sid"
    p.write_bytes(_psid(2))
    t = _T("two", p, 5.0, fmt="SID", channels=2, genre=["Chiptune", "C64"])
    t.genre = ["Chiptune", "C64"]
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    monkeypatch.setattr(stream, "ensure_sid_vu_sidecar", lambda *a, **k: None)
    app, _ = _app(monkeypatch, tmp_path, {"two": t})
    async with _client(app) as c:
        r = await c.get("/api/stream/two", headers={"Cookie": "sb_session=x"})
        assert r.status_code == 200
        b = r.content
        assert struct.unpack_from("<H", b, 22)[0] == 2
        assert int(r.headers["content-length"]) == 44 + 5 * 44100 * 4 == len(b)
        for _ in range(100):
            if await cc.get_cached("two__sub0__dur5__st") is not None:
                break
            await asyncio.sleep(0.05)
        assert await cc.get_cached("two__sub0__dur5__st") is not None
        assert await cc.get_cached("two__sub0__dur5") is None
        # the stored mono render of an older release is no "shorter version"
        shorter = await cc.find_shorter_sid_entry("two", 0, 9, "__st")
        assert shorter is not None and shorter[1] == 5
        assert await cc.find_shorter_sid_entry("two", 0, 9, "") is None


async def test_the_browser_never_warms_a_multi_sid_slot(monkeypatch, tmp_path):
    t = _T("two", "/x/two.sid", 5.0, fmt="SID", channels=2)
    app, _ = _app(monkeypatch, tmp_path, {"two": t})
    async with _client(app) as c:
        j = (await c.get("/api/stream/two/render-status")).json()
    assert j["warm_eligible"] is False


# ═══ SID fade ══════════════════════════════════════════════════════════════

@needs_sid
async def test_a_sid_fade_keeps_the_length_and_fades_its_end(monkeypatch, tmp_path):
    p = tmp_path / "one.sid"
    p.write_bytes(_psid(1))
    plain = await stream._render_sid(p, 0, duration=6)
    monkeypatch.setattr(settings, "sid_fade", 3)
    assert "-fob3" in stream._sid_render_cmd("sidplayfp", p, 0, 6, "/o.wav")
    assert cc._cache_key("x", "sid", 0, duration=6) == "x__sub0__dur6__fo3"
    assert cc.sid_warm_eligible() is False                       # the WASM core has no fade
    faded = await stream._render_sid(p, 0, duration=6)
    try:
        a, b = _wav(plain), _wav(faded)
        assert a["secs"] == b["secs"] == 6.0                     # HVSC's length stays the length
        def ac(w, t0, t1):                                       # (AC rms: the SID's DC removed)
            seg = w["s"][int(t0 * 44100):int(t1 * 44100)]
            m = sum(seg) / len(seg)
            return math.sqrt(sum((x - m) ** 2 for x in seg[::5]) / len(seg[::5]))
        assert abs(ac(a, 1, 2) - ac(b, 1, 2)) < 1.0              # before the fade: the same
        # sidplayfp fades by the SID's own volume register: steps down over
        # the last 3 s (measured 1.0 → 0.62 → 0.29 of the level)
        steps = [ac(b, t, t + 0.5) / ac(a, t, t + 0.5) for t in (2.0, 4.0, 5.5)]
        assert steps[0] > 0.98 and steps[1] < 0.75 and steps[2] < 0.4, steps
        assert steps[0] > steps[1] > steps[2]
    finally:
        plain.unlink(missing_ok=True)
        faded.unlink(missing_ok=True)


# ═══ F8 — libgme loops, fades, NSFe fade, VGM without a loop ═══════════════

@needs_gme
def test_a_vgm_without_a_loop_ends_where_its_music_ends():
    from soniqboom.core import gme_render
    data = _vgm(6.0)
    assert gme_render.vgm_loops(data) is False
    assert gme_render.render_plan(data, 0, 180) == (6.0, 6.0)    # (it used to be 14.0: 8 s of fade over silence)
    wav = gme_render.render_wav(data, 0, 6.0, fade_at_s=6.0, fade_len_s=0.0)
    w = _wav(wav)
    assert abs(w["secs"] - 6.0) < 0.2
    # the music plays to its end, unfaded
    assert _rms(w["s"], 2, 44100, 5.0, 5.4) > 0.5 * _rms(w["s"], 2, 44100, 1.0, 1.4)


@needs_gme
def test_a_looping_vgm_plays_the_loop_count_set(monkeypatch):
    from soniqboom.core import gme_render
    data = _vgm(6.0, loop_at=2.0)                                 # intro 2 s, loop 4 s
    assert gme_render.vgm_loops(data) is True
    assert gme_render.render_plan(data, 0, 180) == (18.0, 10.0)   # default: twice + 8 s
    monkeypatch.setattr(settings, "chip_loops", 3)
    monkeypatch.setattr(settings, "chip_fade", 2)
    assert gme_render.render_plan(data, 0, 180) == (16.0, 14.0)   # 2 + 3×4, + 2 s
    assert cc._cache_key("v", "gme", 0, variant="e1") == "v__sub0__r3__ve1__l3__f2"
    total, fade_at = gme_render.render_plan(data, 0, 180)
    w = _wav(gme_render.render_wav(data, 0, total, fade_at_s=fade_at, fade_len_s=total - fade_at))
    assert abs(w["secs"] - 16.0) < 0.2
    assert _rms(w["s"], 2, 44100, 15.7, 16.0) < 0.2 * _rms(w["s"], 2, 44100, 12.0, 13.0)


@needs_gme
def test_the_nsfe_fade_chunk_is_the_tunes_fade():
    from soniqboom.core import gme_render
    nsf = NSF.read_bytes() if NSF.exists() else None
    if nsf is None:
        pytest.skip("the local NSF is missing")
    with_fade = _nsfe(nsf, times=[20000], fades=[3000])
    without = _nsfe(nsf, times=[20000])
    assert gme_render.nsfe_fades(with_fade) == [3.0]
    assert gme_render.nsfe_fades(without) is None
    assert gme_render.render_plan(with_fade, 0, 180) == (23.0, 20.0)
    assert gme_render.render_plan(without, 0, 180) == (28.0, 20.0)   # (the default 8 s)
    assert gme_render.tune_fades(with_fade, 1) == [3.0]
    # the playlist chunk maps libgme's tune order onto the time / fade arrays
    assert gme_render.nsfe_fades(_nsfe(nsf, fades=[1000, 2000, -1], plst=[2, 0])) == [-1.0, 1.0]
    assert stream._gme_variant(".nsfe") == "e1" and stream._gme_variant(".vgz") == "e1"
    assert stream._gme_variant(".spc") is None
    assert stream._gme_renderer_name(".vgm") == "gme"


@needs_gme
def test_a_fade_length_is_rendered_as_set():
    """gme_set_fade_msecs: a 2 s fade is all but silent 2 s after it starts;
    the default 8 s one (libgme's own curve: about 0.3 of the level 1.8 s in)
    is far louder there."""
    from soniqboom.core import gme_render
    data = _vgm(20.0, loop_at=0.0)
    short = _wav(gme_render.render_wav(data, 0, 12.0, fade_at_s=10.0, fade_len_s=2.0))
    long_ = _wav(gme_render.render_wav(data, 0, 18.0, fade_at_s=10.0))
    ref = _rms(long_["s"], 2, 44100, 8.0, 9.5)
    s_end = _rms(short["s"], 2, 44100, 11.7, 11.95)
    l_mid = _rms(long_["s"], 2, 44100, 11.7, 11.95)
    assert s_end < 0.1 * ref and l_mid > 0.2 * ref and s_end < 0.3 * l_mid
    assert abs(short["secs"] - 12.0) < 0.2 and abs(long_["secs"] - 18.0) < 0.2


def test_kss_loops_and_fade_reach_kssrender(monkeypatch):
    seen = {}

    async def fake(cmd, out, **kw):
        seen["cmd"] = cmd
        return out
    monkeypatch.setattr(stream, "_await_renderer", fake)
    asyncio.run(stream._render_kss("kssrender", Path("/x.kss"), 0, 180))
    assert "-l" not in seen["cmd"] and seen["cmd"][seen["cmd"].index("-f") + 1] == "8000"
    monkeypatch.setattr(settings, "chip_loops", 2)
    monkeypatch.setattr(settings, "chip_fade", 4)
    asyncio.run(stream._render_kss("kssrender", Path("/x.kss"), 0, 180))
    c = seen["cmd"]
    assert c[c.index("-l") + 1] == "2" and c[c.index("-f") + 1] == "4000"


# ═══ F1b — stereo width ════════════════════════════════════════════════════

def test_width_matrix_filter_and_variant():
    assert ro.width_gains(100) == (1.0, 0.0)
    assert ro.width_gains(60) == (0.8, 0.2)
    assert ro.width_gains(0) == (0.5, 0.5)
    assert ro.ffmpeg_width_filter(100) == "" and ro.width_variant(100) == ""
    assert ro.width_variant(60) == "-w60"
    assert ro.normalize_width("abc") == 100 and ro.normalize_width(999) == 200
    assert ro.width_af_args(["-acodec", "flac"], 100) == ["-acodec", "flac"]
    merged = ro.width_af_args(["-af", "volume=0.5", "-acodec", "flac"], 60)
    assert merged[1].startswith("volume=0.5,aformat=channel_layouts=stereo,pan=stereo|")


def _stereo_test_wav(path: Path, secs=2.0):
    """L: 440 Hz, R: 660 Hz — a hard-panned pair, side/mid 1."""
    rate = 44100
    a = array.array("h")
    for i in range(int(secs * rate)):
        a.append(int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
        a.append(int(8000 * math.sin(2 * math.pi * 660 * i / rate)))
    d = a.tobytes()
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(d)) + b"WAVEfmt "
                     + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
                     + b"data" + struct.pack("<I", len(d)) + d)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("width", [0, 60, 100, 150])
def test_the_ffmpeg_width_stage_scales_the_side_only(tmp_path, width):
    src = _stereo_test_wav(tmp_path / "s.wav")
    out = tmp_path / "o.wav"
    af = ro.width_af_args(["-acodec", "pcm_s16le"], width)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *af,
                    str(out)], check=True)
    a, b = _wav(src), _wav(out)
    sm_a, _ = _ms(a)
    sm_b, _ = _ms(b)
    if width == 0:
        assert sm_b < 1e-3
    else:
        assert abs(sm_b / sm_a - width / 100) < 0.01, (sm_a, sm_b)
    # the mid is kept
    mid = lambda w: _rms(array.array("h", [(w["s"][i] + w["s"][i + 1]) // 2  # noqa: E731
                                            for i in range(0, len(w["s"]), 2)]), 1, 44100, 0.2, 1.8)
    assert abs(mid(a) - mid(b)) / mid(a) < 0.01


@needs_ffmpeg
def test_a_mono_input_stays_the_same_on_both_channels(tmp_path):
    mono = tmp_path / "m.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-ac", "1", str(mono)], check=True)
    out = tmp_path / "o.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mono),
                    *ro.width_af_args(["-acodec", "pcm_s16le"], 60), str(out)], check=True)
    w = _wav(out)
    assert w["ch"] == 2 and w["s"][0::2] == w["s"][1::2]


@needs_ffmpeg
async def test_subsonic_transcodes_get_the_width_and_the_web_player_the_reference(monkeypatch,
                                                                                  tmp_path):
    """A rendered (cached) tune through the stream endpoint at width 60: a
    Subsonic ``format=flac`` stream is narrowed to 0.6 of the side (and keyed
    ``-w60``); ``format=raw`` and the web player get the render as it is."""
    wav = _stereo_test_wav(tmp_path / "r.wav", 3.0)
    t = _T("trk", MOD, 3.0, fmt="ProTracker")
    t.genre = ["Tracker"]
    monkeypatch.setattr(settings, "stereo_width", 60)
    app, _ = _app(monkeypatch, tmp_path, {"trk": t})
    key = cc._cache_key("trk", "tracker", 0)
    _register(key, wav, "tracker")
    try:
        async with _client(app) as c:
            web = await c.get("/api/stream/trk", headers={"Cookie": "sb_session=x"})
            assert web.status_code == 200 and web.content == wav.read_bytes()
            raw = await c.get("/api/stream/trk?format=raw&u=x")
            assert raw.content == wav.read_bytes()
            fl = await c.get("/api/stream/trk?format=flac&u=x")
            assert fl.status_code == 200 and fl.headers.get("x-target-codec") == "flac"
            got = tmp_path / "got.flac"
            got.write_bytes(fl.content)
            dec = tmp_path / "dec.wav"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(got),
                            "-acodec", "pcm_s16le", str(dec)], check=True)
            sm_ref, _ = _ms(_wav(wav), 0.2, 2.8)
            sm_got, _ = _ms(_wav(dec), 0.2, 2.8)
            assert abs(sm_got / sm_ref - 0.6) < 0.02, (sm_ref, sm_got)
            variant, _gain = await stream._rendered_transcode_variant(key, wav, 60)
            assert variant.endswith("-w60")
    finally:
        _unregister(key)


def test_cast_streams_carry_the_width_filter():
    from soniqboom.core.cast_pipe import _build_cmd
    cmd = _build_cmd(Path("x.wav"), codec="mp3", bitrate_kbps=None, sample_rate=None,
                     ffmpeg_path=None, width=60)
    af = cmd[cmd.index("-af") + 1]
    assert "pan=stereo|c0=0.8*c0+0.2*c1|c1=0.2*c0+0.8*c1" in af
    plain = _build_cmd(Path("x.wav"), codec="mp3", bitrate_kbps=None, sample_rate=None,
                       ffmpeg_path=None)
    assert "-af" not in plain


# ═══ uade: Paula's stereo, the width at playback ═══════════════════════════

def test_uade_renders_paulas_stereo_under_its_own_key():
    assert ro.UADE_STEREO_ARGS == ("--panning=0",)
    assert cc._cache_key("u", "uade", 0) == "u__sub0__r2__pan0"
    src = Path(stream.__file__).read_text()
    i = src.index("async def _render_uade_file(")
    body = src[i:src.index("def _drop_live_after_store", i)]
    assert '"--headphones"' not in body and "UADE_STEREO_ARGS" in body


@needs_uade
async def test_uades_render_is_hard_panned_now(tmp_path):
    """Measured: uade123's defaults (pan 0.7) with ``--headphones`` gave
    side/mid 0.14 on this module; Paula's own stereo is ~0.93."""
    out = await stream._render_uade(JAM)
    try:
        sm, corr = _ms(_wav(out), 1.0, 9.0)
        assert sm > 0.8 and corr < 0.3, (sm, corr)
    finally:
        out.unlink(missing_ok=True)


# ═══ endless play ══════════════════════════════════════════════════════════

def _endless_app(monkeypatch, tmp_path, tracks):
    for t in tracks.values():
        t.genre = getattr(t, "genre", None) or ["Tracker"]
    return _app(monkeypatch, tmp_path, tracks)


@needs_openmpt
async def test_endless_tracker_stream_plays_on_through_the_loop(monkeypatch, tmp_path):
    from soniqboom.core import endless
    t = _T("mod", MOD, 96.1, fmt="ProTracker")
    app, _ = _endless_app(monkeypatch, tmp_path, {"mod": t})
    fb = endless.frame_bytes("tracker")
    async with _client(app) as c:
        h = await c.get("/api/stream/mod/endless", headers={"Range": "bytes=0-0"})
        assert h.status_code == 206 and h.content == b"R"
        total = int(h.headers["content-range"].split("/")[1])
        assert total == 44 + ro.ENDLESS_SECONDS * 48000 * fb
        r = await c.get("/api/stream/mod/endless", headers={"Range": f"bytes=0-{44 + 2 * 48000 * fb - 1}"})
        assert r.status_code == 206 and len(r.content) == 44 + 2 * 48000 * fb
        assert struct.unpack_from("<HHI", r.content, 20) == (3, 2, 48000)
        # the start is the cached render's start (the same engine and options)
        out = await stream._render_tracker(MOD)
        try:
            ref = _wav(out)["data"][:2 * 48000 * fb]
        finally:
            out.unlink(missing_ok=True)
            out.with_suffix(".vu").unlink(missing_ok=True)
        assert r.content[44:] == ref
        # 200 s in — past the 96 s song: it plays on (a seek starts the engine there)
        off = 44 + 200 * 48000 * fb
        t0 = time.perf_counter()
        r2 = await c.get("/api/stream/mod/endless", headers={"Range": f"bytes={off}-{off + 48000 * fb - 1}"})
        took = time.perf_counter() - t0
        a = array.array("f"); a.frombytes(r2.content)
        assert r2.status_code == 206 and max(abs(x) for x in a) > 0.1
        assert took < 2.0
        # … the loop: 200 s is 8 s into its third pass, 104 s 8 s into its
        # second — the same music (the song restarts at 0)
        r3 = await c.get("/api/stream/mod/endless",
                         headers={"Range": f"bytes={44 + 104 * 48000 * fb}-{44 + 105 * 48000 * fb - 1}"})
        b = array.array("f"); b.frombytes(r3.content)
        assert sum(abs(x - y) for x, y in zip(a[::53], b[::53])) < 1e-2
    from soniqboom.core import endless as _e
    assert _e.tracker_seek(200.0, (96.0, 0.0)) == 8.0
    assert _e.tracker_seek(50.0, (96.0, 10.0)) == 50.0
    assert _e.tracker_seek(100.0, (96.0, 10.0)) == 14.0         # loop 86 s from 10 s


@needs_gme
async def test_endless_vgm_without_a_loop_starts_again(monkeypatch, tmp_path):
    v = tmp_path / "n.vgm"
    v.write_bytes(_vgm(6.0))
    t = _T("vgm", v, 6.0, fmt="VGM")
    app, _ = _endless_app(monkeypatch, tmp_path, {"vgm": t})
    async with _client(app) as c:
        off = 44 + 10 * 44100 * 4                                # 4 s after its end
        r = await c.get("/api/stream/vgm/endless", headers={"Range": f"bytes={off}-{off + 44100 * 4 - 1}"})
        a = array.array("h"); a.frombytes(r.content)
        assert r.status_code == 206 and r.headers["x-endless-engine"] == "gme"
        assert max(abs(x) for x in a) > 1000                     # playing again, not silence


async def test_sid_and_other_tunes_have_no_endless_stream(monkeypatch, tmp_path):
    p = tmp_path / "s.sid"
    p.write_bytes(_psid(1))
    t = _T("sid", p, 60.0, fmt="SID")
    t.genre = ["Chiptune", "C64"]
    app, _ = _app(monkeypatch, tmp_path, {"sid": t})
    async with _client(app) as c:
        r = await c.get("/api/stream/sid/endless", headers={"Range": "bytes=0-0"})
    assert r.status_code == 422


@needs_openmpt
async def test_an_endless_stream_stops_its_engine_when_the_reader_goes():
    """The engine renders only as fast as it is read (a pipe): a reader that
    takes a few chunks and stops leaves no engine running, and the engine
    never ran ahead — it was blocked on the pipe.  (``core/endless.pipe_pcm``
    directly: httpx's in-process transport collects a whole body.)"""
    from soniqboom.core import endless, forksafe
    before = {id(p) for p in forksafe.live_processes()}
    gen = endless.pipe_pcm(endless.tracker_cmd(shutil.which("openmpt123"), MOD, 0, 0.0, []),
                           skip_wav_header=False)
    got = 0
    async for chunk in gen:
        got += len(chunk)
        if got >= 1_000_000:
            break
    procs = [p for p in forksafe.live_processes() if id(p) not in before]
    assert len(procs) == 1 and procs[0].returncode is None       # blocked on the full pipe
    await asyncio.sleep(0.5)
    assert procs[0].returncode is None                           # still waiting: nothing read
    await gen.aclose()
    assert procs[0].returncode is not None                       # killed with the reader
    assert {id(p) for p in forksafe.live_processes()} <= before | {id(procs[0])}


# ═══ settings ══════════════════════════════════════════════════════════════

def test_admin_settings_normalize_the_render_options():
    from soniqboom.api import admin
    n = admin._normalize_render_option
    assert n("amiga_resampler", "A500") == "a500" and n("amiga_resampler", "x") == "auto"
    assert n("stereo_width", "60") == 60 and n("stereo_width", -5) == 0
    assert n("tracker_loops", 3) == 3 and n("tracker_loops", 40) == 0
    assert n("tracker_fade", 99) == 30.0 and n("tracker_fade", -3) == -1.0
    assert n("sid_fade", -3) == 0.0 and n("sid_fade", "2.25") == 2.2
    got = admin._render_options_settings()
    assert got == {"amiga_resampler": "auto", "stereo_width": 100, "tracker_loops": 0,
                   "tracker_fade": -1.0, "chip_loops": 0, "chip_fade": -1.0, "sid_fade": 0.0}


def test_the_web_player_reads_the_servers_width():
    assert ro.public_config() == {"stereo_width": 100, "stereo_width_headphones": 60}


# ═══ performance guards ════════════════════════════════════════════════════

def test_perf_cache_keys_stay_cheap(monkeypatch):
    """The render options add a few setting reads to every key: a key stays
    well under 20 µs (measured ~3 µs)."""
    monkeypatch.setattr(settings, "tracker_loops", 2)
    monkeypatch.setattr(settings, "chip_fade", 4)
    n = 20000
    t0 = time.perf_counter()
    for i in range(n):
        cc._cache_key("abcdef0123", "tracker", i & 3)
        cc._cache_key("abcdef0123", "gme", i & 3, variant="e1")
        cc._cache_key("abcdef0123", "sid", 0, duration=180, variant="st")
    per = (time.perf_counter() - t0) / (3 * n)
    assert per < 20e-6, per


async def test_perf_a_cached_tracker_render_answers_at_once(monkeypatch, tmp_path):
    """A cache hit is served from the file as before (the key changed, not the
    path): first byte well under 100 ms in-process."""
    wav = _stereo_test_wav(tmp_path / "hit.wav", 30.0)
    t = _T("hit", MOD, 30.0, fmt="ProTracker")
    t.genre = ["Tracker"]
    app, _ = _app(monkeypatch, tmp_path, {"hit": t})
    key = cc._cache_key("hit", "tracker", 0)
    _register(key, wav, "tracker")
    try:
        async with _client(app) as c:
            await c.get("/api/stream/hit", headers={"Cookie": "sb_session=x", "Range": "bytes=0-1"})
            best = 9.0
            for _ in range(5):
                t0 = time.perf_counter()
                r = await c.get("/api/stream/hit", headers={"Cookie": "sb_session=x",
                                                             "Range": "bytes=2000000-2065535"})
                best = min(best, time.perf_counter() - t0)
                assert r.status_code == 206 and len(r.content) == 65536
        assert best < 0.1, best
    finally:
        _unregister(key)


@needs_openmpt
async def test_cast_reads_a_tracker_render_as_it_grows(monkeypatch, tmp_path):
    """The cast pipeline's feed for a module (``cast_render.prepare_live_source``):
    the growing render, from its first audio — the whole Paula render takes
    about 12× longer than libopenmpt's own, too long to wait on."""
    from soniqboom.core import cast_render
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    src = tmp_path / "a.mod"
    shutil.copy2(MOD, src)
    t0 = time.perf_counter()
    feed = await cast_render.prepare_live_source(track_id="castmod", track_path=str(src))
    assert feed is not None
    first = None
    got = bytearray()
    async for chunk in feed:
        if first is None:
            first = time.perf_counter() - t0
        got += chunk
    whole = time.perf_counter() - t0
    w = _wav(bytes(got))
    assert w["tag"] == 3 and w["ch"] == 2 and abs(w["secs"] - 96.1) < 0.01
    assert first < whole / 3, (first, whole)          # (it started long before the end)
