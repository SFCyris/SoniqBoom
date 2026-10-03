# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Renders that play silence, and the default tune of multi-tune files.

* ``core.silence`` judges a render audible by the peak-to-peak swing inside
  short blocks (a silent emulated chip's decaying DC offset is not sound);
  the conversion cache refuses a silent render with a 422 and remembers it so
  the progressive paths stop streaming it.
* ``.rsid`` (RealSID) files are C64 SID tunes everywhere a SID extension is
  listed.
* A packed GYMX is inflated before libgme sees it; its length is its frame
  count.
* ``.psf`` without the PSF magic is an Amiga SoundFactory module (uade).
* A bare play of a multi-tune uade / libgme / sc68 file starts at its first
  tune that isn't empty (probed once, recorded as ``default_subsong``); a
  named tune (``?subsong=N``, ``<id>~N``, a playlist entry) is always tune N —
  wires never change meaning.
* Old MED modules render through zxtune, cut at a bounded length; an early
  DSIK module is a clear 422.

The real-audio checks use the local fixtures under internal/ and skip when a
fixture or renderer is missing.
"""
from __future__ import annotations

import array
import asyncio
import math
import shutil
import struct
import time
import types
import zlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream
from soniqboom.core import conversion_cache, gme_render, metadata, silence

REPO = Path(__file__).resolve().parent.parent
TD = REPO / "internal/testdata"
FS = REPO / "internal/format-samples"
MORBASE = TD / "uade/Richard Joseph/cannon fodder 2-morbase.sng"
ECO = FS / "Fred Gray/Fred Gray/eco.gray"
STARGOOSE = FS / "Fred Gray/Fred Gray/stargoose.gray"
DW = TD / "uade/David Whittaker/carrier command.dw"
RSID = FS / "RealSID/Wiz/nr. 6.rsid"
GYM = FS / "Megadrive GYM/- unknown/Art Alive/art alive.gym"
AXELF_PSF = FS / "SoundFactory/- unknown/axelf.psf"
KSS_MSX = FS / "KSS/BDD/member profiles bgm.kss"
GBS = TD / "game/hello kitty no happy house.gbs"

needs_uade = pytest.mark.skipif(not shutil.which("uade123"), reason="uade123 not installed")


def _pcm16(samples, channels: int = 2) -> bytes:
    a = array.array("h", [int(x) for x in samples])
    return a.tobytes()


def _wav16(path: Path, samples, channels: int = 2, rate: int = 44100,
           extensible: bool = False) -> Path:
    body = _pcm16(samples, channels)
    if extensible:          # uade's own header shape (WAVE_FORMAT_EXTENSIBLE)
        fmt = (struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * 2 * channels,
                           2 * channels, 16)
               + struct.pack("<HHI", 22, 16, 3) + struct.pack("<H", 1) + bytes(14))
    else:
        fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(body)) + b"WAVE"
                     + b"fmt " + struct.pack("<I", len(fmt)) + fmt
                     + b"data" + struct.pack("<I", len(body)) + body)
    return path


def _square(n: int, amp: int) -> list[int]:
    return [amp if (i // 50) % 2 else -amp for i in range(n)]


# ── silence ─────────────────────────────────────────────────────────────────

def test_block_swing_tells_silence_from_quiet_sound():
    assert not silence.pcm16_audible(bytes(44100 * 4))                    # digital zero
    assert not silence.pcm16_audible(_pcm16([-1500] * 88200))            # DC offset
    # a decaying DC offset — a silent reSIDfp 8580 render opens like this
    decay = [round(-1530 * math.exp(-i / 9000)) for i in range(44100)]
    assert not silence.pcm16_audible(_pcm16(decay), channels=1)
    # quiet but real: ±544 (the quietest real render measured, 0.0166 FS)
    assert silence.pcm16_audible(_pcm16(_square(4410, 544)), channels=1)
    # just above / below the threshold (peak-to-peak 196)
    assert silence.pcm16_audible(_pcm16(_square(4410, 99)), channels=1)
    assert not silence.pcm16_audible(_pcm16(_square(4410, 98)), channels=1)
    # sound in ONE channel of a stereo stream counts
    stereo = []
    for v in _square(2000, 3000):
        stereo += [0, v]
    assert silence.pcm16_audible(_pcm16(stereo), channels=2)


def test_meter_accounts_frames_across_odd_chunks():
    m = silence.AudibilityMeter(2, 44100)
    data = _pcm16(_square(44100 * 2, 5000))            # 1 s stereo
    for i in range(0, len(data), 1001):                # chunks split frames
        m.feed(data[i:i + 1001])
    assert m.frames == 44100 and abs(m.seconds - 1.0) < 1e-9 and m.audible
    q = silence.AudibilityMeter(2, 44100)
    q.feed(bytes(4 * 1000))
    assert not q.audible and q.frames == 1000


def test_wav_audible_reads_pcm_float_extensible_and_streaming_headers(tmp_path):
    assert silence.wav_audible(_wav16(tmp_path / "z.wav", [0] * 8000)) is False
    assert silence.wav_audible(_wav16(tmp_path / "a.wav", _square(8000, 4000))) is True
    ext = _wav16(tmp_path / "e.wav", [0] * 4000 + _square(4000, 4000), extensible=True)
    assert silence.wav_layout(ext)[0] == 1 and silence.wav_audible(ext) is True
    # a streaming header (data size 0xFFFFFFFF) reads to the end of the file
    raw = bytearray(_wav16(tmp_path / "s.wav", [0] * 8000 + _square(800, 4000)).read_bytes())
    i = raw.find(b"data")
    raw[i + 4:i + 8] = b"\xff\xff\xff\xff"
    (tmp_path / "s.wav").write_bytes(bytes(raw))
    assert silence.wav_audible(tmp_path / "s.wav") is True
    # 32-bit float (openmpt123 writes it)
    fl = array.array("f", [0.0] * 4000 + [0.2 if (i // 30) % 2 else -0.2 for i in range(4000)])
    fmt = struct.pack("<HHIIHH", 3, 2, 48000, 48000 * 8, 8, 32)
    (tmp_path / "f.wav").write_bytes(
        b"RIFF" + struct.pack("<I", 36 + len(fl) * 4) + b"WAVE" + b"fmt "
        + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(fl) * 4) + fl.tobytes())
    assert silence.wav_audible(tmp_path / "f.wav") is True
    fz = array.array("f", [0.0] * 8000)
    (tmp_path / "fz.wav").write_bytes(
        b"RIFF" + struct.pack("<I", 36 + len(fz) * 4) + b"WAVE" + b"fmt "
        + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(fz) * 4) + fz.tobytes())
    assert silence.wav_audible(tmp_path / "fz.wav") is False
    # can't judge: 24-bit, not a WAV, missing
    fmt24 = struct.pack("<HHIIHH", 1, 2, 44100, 44100 * 6, 6, 24)
    (tmp_path / "t.wav").write_bytes(b"RIFF" + struct.pack("<I", 36 + 600) + b"WAVE" + b"fmt "
                                     + struct.pack("<I", 16) + fmt24 + b"data"
                                     + struct.pack("<I", 600) + bytes(600))
    assert silence.wav_audible(tmp_path / "t.wav") is None
    (tmp_path / "x.flac").write_bytes(b"fLaC" + bytes(100))
    assert silence.wav_audible(tmp_path / "x.flac") is None
    assert silence.wav_audible(tmp_path / "nope.wav") is None


@pytest.mark.skipif(not shutil.which("sidplayfp"), reason="sidplayfp not installed")
def test_a_silent_c64_tune_reads_as_silent_on_every_chip_setting(tmp_path):
    """A PSID whose play routine does nothing: reSIDfp still emits a decaying
    DC offset (8580 + digiboost the largest), which must read as silence."""
    import os
    import subprocess
    hdr = bytearray(0x7C)
    hdr[0:4] = b"PSID"
    struct.pack_into(">HH", hdr, 4, 2, 0x7C)
    struct.pack_into(">HHH", hdr, 8, 0, 0x1000, 0x1001)
    struct.pack_into(">HH", hdr, 0x0E, 1, 1)
    sid = tmp_path / "quiet.sid"
    sid.write_bytes(bytes(hdr) + b"\x00\x10" + b"\x60" * 32)        # RTS
    env = dict(os.environ, HOME=str(tmp_path))
    for flags in ([], ["-mn"], ["-mn", "--digiboost"], ["-mn", "--digiboost", "-nf"]):
        out = tmp_path / "o.wav"
        subprocess.run(["sidplayfp", *flags, "-f44100", "-t5", f"-w{out}", str(sid)],
                       capture_output=True, env=env, check=True)
        assert silence.wav_audible(out) is False, flags


# ── the cache refuses silent renders ────────────────────────────────────────

@pytest.fixture()
def conv(monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    return tmp_path


@pytest.mark.asyncio
async def test_a_silent_render_is_a_422_never_cached(conv):
    tmp = conv
    made = []

    async def silent():
        p = _wav16(tmp / f"s{len(made)}.wav", [0] * 44100)
        p.with_suffix(".vu").write_bytes(b"VUMR")
        made.append(p)
        return p
    key = conversion_cache._cache_key("sil1", "uade", 0)
    with pytest.raises(HTTPException) as ei:
        await conversion_cache.get_or_render(track_id="sil1", format_type="uade",
                                             subsong=0, render_fn=silent)
    assert ei.value.status_code == 422
    assert ei.value.detail == conversion_cache.SILENT_RENDER_DETAIL
    assert not made[0].exists() and not made[0].with_suffix(".vu").exists()
    assert await conversion_cache.get_cached(key) is None
    assert conversion_cache.known_silent(key)
    assert conversion_cache.recent_failure("sil1", 0)["status"] == 422

    # a second play is told at once — the silence is not rendered again
    async def loud():
        made.append("render")
        return _wav16(tmp / "l.wav", _square(44100, 4000))
    with pytest.raises(HTTPException) as ei:
        await conversion_cache.get_or_render(track_id="sil1", format_type="uade",
                                             subsong=0, render_fn=loud)
    assert ei.value.status_code == 422 and "render" not in made
    # …until the memo expires (or a good render is stored for the key)
    conversion_cache._silent_keys[key] -= conversion_cache._SILENT_TTL_S
    path, hit = await conversion_cache.get_or_render(track_id="sil1", format_type="uade",
                                                     subsong=0, render_fn=loud)
    assert not hit and path.exists() and not conversion_cache.known_silent(key)
    conversion_cache._purge_entry(key)


@pytest.mark.asyncio
async def test_transcodes_are_not_judged(conv):
    async def quiet_recording():
        return _wav16(conv / "q.wav", [0] * 4410)
    path, _ = await conversion_cache.get_or_render(
        track_id="sil2", format_type="transcoded", subsong=0, codec="wav",
        render_fn=quiet_recording)
    assert path.exists()
    conversion_cache._purge_entry(path.stem)


@pytest.mark.asyncio
async def test_a_silent_progressive_render_is_dropped_and_reported(conv):
    key = conversion_cache._cache_key("sil3", "sid", 0, duration=60)
    wav = _wav16(conv / "p.wav", [0] * 44100, channels=1)
    assert await stream._progressive_render_silent(key, "sid", wav) is True
    assert not wav.exists() and conversion_cache.known_silent(key)
    assert conversion_cache.recent_failure("sil3", 0)["status"] == 422
    ok = _wav16(conv / "ok.wav", _square(44100, 4000), channels=1)
    assert await stream._progressive_render_silent(key, "sid", ok) is False and ok.exists()


# ── .rsid ───────────────────────────────────────────────────────────────────

def test_rsid_is_a_sid_extension_everywhere():
    from soniqboom.core import cast_render, scanner
    assert ".rsid" in metadata.SID_EXTS and ".rsid" in metadata.SUPPORTED_EXTENSIONS
    assert metadata.FORMAT_NAMES[".rsid"] == "SID"
    assert stream._SID_EXTS is metadata.SID_EXTS
    assert cast_render._SID_EXTS is metadata.SID_EXTS
    assert set(scanner._SID_DETECT_EXTS) == set(metadata.SID_EXTS)
    # a RealSID file name that a uade prefix token matches ("nr" = ProWizard)
    # still goes by its extension
    assert stream._render_ident("/m/nr. 6.rsid") == (".rsid", False)


@pytest.mark.skipif(not RSID.exists(), reason="RealSID fixture missing")
def test_rsid_fixture_extracts_as_a_c64_tune():
    d = metadata.extract(RSID, "rsid1")
    assert d.format == "SID" and d.title == "Nr. 6" and "C64" in d.genre
    assert stream._is_c64_sid(RSID)


# ── GYMX ────────────────────────────────────────────────────────────────────

def _gymx(body: bytes, *, pack: bool, loop: int = 0) -> bytes:
    head = bytearray(gme_render.GYMX_HEADER)
    head[0:4] = b"GYMX"
    head[4:4 + 9] = b"Song Name"
    head[36:36 + 9] = b"Game Name"
    struct.pack_into("<II", head, 420, loop, len(body) if pack else 0)
    return bytes(head) + (zlib.compress(body) if pack else body)


def test_gymx_is_inflated_and_measured():
    body = (b"\x01\x2b\x80" + b"\x00") * 120 + b"\x03\x9f" + b"\x00" * 60     # 180 frames
    packed = _gymx(body, pack=True)
    out = gme_render.unpack_gym(packed)
    assert out[gme_render.GYMX_HEADER:] == body
    assert gme_render.gymx_fields(out)["packed_size"] == 0
    assert gme_render.gymx_fields(packed)["song"] == "Song Name"
    assert gme_render.gym_seconds(out) == 3.0
    plain = _gymx(body, pack=False)
    assert gme_render.unpack_gym(plain) is plain
    assert gme_render.unpack_gym(b"\x00\x00") == b"\x00\x00"           # a raw GYM
    bad = bytearray(packed)
    struct.pack_into("<I", bad, 424, len(body) + 5)                    # wrong size
    with pytest.raises(ValueError):
        gme_render.unpack_gym(bytes(bad))
    with pytest.raises(ValueError):
        gme_render.unpack_gym(packed[:gme_render.GYMX_HEADER] + b"not zlib")


@pytest.mark.skipif(not GYM.exists(), reason="GYM fixture missing")
def test_packed_gym_fixture_metadata_and_render(tmp_path):
    d = metadata.extract(GYM, "gym1")
    assert d.title == "Art Alive Theme" and d.album == "Art Alive"
    assert d.duration == pytest.approx(73.95)
    if not gme_render.is_available():
        pytest.skip("libgme not installed")
    out = asyncio.run(stream._render_gme(GYM))
    try:
        secs = stream._wav_audio_seconds(out)
        assert 70 < secs < 76 and silence.wav_audible(out) is True
    finally:
        out.unlink(missing_ok=True)


# ── libgme header tune counts ───────────────────────────────────────────────

def test_gme_header_tunes():
    nsf = bytearray(128)
    nsf[0:5] = b"NESM\x1a"
    nsf[6], nsf[7] = 12, 3
    assert gme_render.header_tunes(bytes(nsf)) == (12, 2)
    gbs = bytearray(112)
    gbs[0:3] = b"GBS"
    gbs[4], gbs[5] = 44, 1
    assert gme_render.header_tunes(bytes(gbs)) == (44, 0)
    ay = bytearray(32)
    ay[0:8] = b"ZXAYEMUL"
    ay[16], ay[17] = 4, 2
    assert gme_render.header_tunes(bytes(ay)) == (5, 2)
    nsfe = (b"NSFE" + struct.pack("<I4s", 10, b"INFO") + bytes(8) + bytes([7, 1])
            + struct.pack("<I4s", 0, b"NEND"))
    assert gme_render.header_tunes(nsfe) == (7, 1)
    sap = b'SAP\r\nAUTHOR "x"\r\nSONGS 3\r\nDEFSONG 1\r\n\xff\xff' + bytes(16)
    assert gme_render.header_tunes(sap) == (3, 1)
    assert gme_render.header_tunes(b"KSCC" + bytes(28)) == (None, None)
    bad = bytearray(nsf)
    bad[6], bad[7] = 0, 0
    assert gme_render.header_tunes(bytes(bad)) == (None, None)


@pytest.mark.skipif(not GBS.exists(), reason="GBS fixture missing")
def test_gbs_fixture_records_its_tune_count():
    assert metadata.extract(GBS, "gbs1").subsongs == 44


# ── .psf collision ──────────────────────────────────────────────────────────

def test_psf_magic_routes_a_soundfactory_module_to_uade(tmp_path):
    rip = tmp_path / "a.psf"
    rip.write_bytes(b"PSF\x01" + bytes(64))
    mod = tmp_path / "axelf.psf"
    mod.write_bytes(b"\x00\x01=g" + bytes(64))
    assert stream._psf_has_magic(rip) and not stream._psf_has_magic(mod)
    amiga = types.SimpleNamespace(genre=["Amiga", "Module"], format="Soundfactory")
    assert stream._render_ident(str(mod), amiga) == (".psf", True)
    assert stream._render_ident(str(rip), types.SimpleNamespace(genre=["Chiptune"])) == (".psf", False)


@needs_uade
@pytest.mark.skipif(not AXELF_PSF.exists(), reason="SoundFactory fixture missing")
def test_soundfactory_fixture_is_indexed_as_an_amiga_module():
    d = metadata.extract(AXELF_PSF, "sf1")
    assert d.genre == ["Amiga", "Module"] and d.format == "Soundfactory"




# ── default tune: a bare play takes it, a named tune never moves ───────────

def _track(tid="t", fmt="Fred Gray", subsongs=4, default=None, mtime=1.0, duration=0.0,
           start=None):
    return types.SimpleNamespace(id=tid, format=fmt, subsongs=subsongs, default_subsong=default,
                                 start_subsong=start, mtime=mtime, duration=duration,
                                 genre=["Amiga", "Module"], path="/x/" + tid)


class _Req:
    """Just enough of a Request for ``explicit_wire``."""
    def __init__(self, qs: str = ""):
        from starlette.datastructures import QueryParams
        self.query_params = QueryParams(qs)


def test_a_named_tune_is_the_wire_a_bare_play_is_the_default():
    t = _track(default=2)
    assert [stream.tune_index("t", t, w) for w in range(4)] == [0, 1, 2, 3]
    assert stream.tune_index("t", t, None) == 2
    assert stream.tune_index("t", _track(default=None), None) == 0      # not probed yet
    assert stream.default_tune_index("t", t) == 2
    # SID / SNDH: the wire as it is (their renderers map it), a bare play = wire 0
    sid = _track(fmt="SID", default=2, start=2)
    assert stream.tune_index("t", sid, None) == 0 and stream.tune_index("t", sid, 3) == 3
    assert stream.default_tune_known("t", sid) is None
    # a store record dict (the cast keys read the store's)
    assert stream.tune_index("t", {"format": "GBS", "subsongs": 5, "default_subsong": 3}, None) == 3
    # the Subsonic tune ids keep their mapping: default_subsong never enters it
    from soniqboom.core import subsonic_index as sx
    rec = {"id": "t", "subsongs": 4, "default_subsong": 2}
    assert [sx.wire_tune(rec, w) for w in range(4)] == [0, 1, 2, 3]


def test_explicit_wire_tells_a_named_first_tune_from_a_bare_play():
    assert stream.explicit_wire(None, _Req()) is None
    assert stream.explicit_wire(2, _Req()) == 2
    assert stream.explicit_wire(0, _Req("subsong=0")) == 0              # the web picker's tune 1
    assert stream.explicit_wire(0, _Req("id=x~0")) is None             # in-process Subsonic / cast
    assert stream.explicit_wire(0, None) is None


def test_probed_default_memo_is_tied_to_the_file_version():
    stream._DEFAULT_TUNE.pop("mv1", None)
    stream._DEFAULT_PROBE_STATE.pop("mv1", None)
    t = _track("mv1", mtime=10.0)
    stream._DEFAULT_TUNE["mv1"] = (stream._file_version(t), 2)
    try:
        assert stream.default_tune_known("mv1", t) == 2
        assert stream.default_tune_known("mv1", _track("mv1", mtime=11.0)) is None
        # a file replaced keeping its mtime is another version too (its size)
        other = _track("mv1", mtime=10.0)
        other.file_size = 123
        assert stream.default_tune_known("mv1", other) is None
        # an undecided probe's pick plays, but the stored length stays tune 1's
        stream._DEFAULT_TUNE.pop("mv1")
        stream._set_probe_state("mv1", t, 3, 3, 0.0)
        assert stream.tune_index("mv1", t, None) == 3 and stream.default_tune_index("mv1", t) == 0
        assert stream.default_tune_known("mv1", other) is None
    finally:
        stream._DEFAULT_TUNE.pop("mv1", None)
        stream._DEFAULT_PROBE_STATE.pop("mv1", None)


class _Store:
    """The few store calls the default-tune write-back and the tune-count
    backfill make."""
    def __init__(self, recs: dict):
        self.recs = recs
        self.updates: list = []
        self.waves: dict = {}

    def get_track(self, tid):
        return self.recs.get(tid)

    def update_track_fields(self, tid, fields):
        self.updates.append((tid, dict(fields)))
        if tid in self.recs:
            self.recs[tid].update(fields)
        return tid in self.recs

    def get_waveform(self, tid):
        return self.waves.get(tid)

    def store_waveform(self, tid, w):
        self.waves[tid] = w


@pytest.fixture()
def store(monkeypatch):
    """A fake store whose records ``stream.get_track`` serves (as objects)."""
    from soniqboom.core import store as store_mod
    st = _Store({})
    monkeypatch.setattr(store_mod, "get_store", lambda: st)

    async def get_track(tid):
        r = st.recs.get(tid)
        return types.SimpleNamespace(**r) if r is not None else None
    monkeypatch.setattr(stream, "get_track", get_track)

    async def base0(*a):
        return 0
    monkeypatch.setattr(stream, "_uade_resolve_base", base0)
    return st


def _rec(st, tid, **kw):
    t = _track(tid, **kw)
    st.recs[tid] = dict(vars(t))
    stream._DEFAULT_TUNE.pop(tid, None)
    stream._DEFAULT_PROBE_STATE.pop(tid, None)
    stream._DEFAULT_PROBES.pop(tid, None)
    return t


@pytest.mark.asyncio
async def test_the_first_tune_that_is_not_empty_becomes_the_default(monkeypatch, store):
    calls = []

    async def audible(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        return index >= 2                         # tunes 0 and 1 are empty stubs
    monkeypatch.setattr(stream, "_tune_audible", audible)
    t = _rec(store, "dt1", subsongs=5, duration=1.7)
    store.waves["dt1"] = [0.5] * 10               # the first tune's scrubber waveform
    try:
        got = await asyncio.gather(*(stream.ensure_default_tune("dt1", t, Path("/x"), "uade")
                                     for _ in range(3)))
        assert got == [2, 2, 2] and calls == [0, 1, 2]          # one probe, shared
        # written to the record at once: the default, and the old tune's
        # length and waveform dropped
        assert store.updates == [("dt1", {"default_subsong": 2, "duration": 0.0})]
        assert store.waves["dt1"] == []
        assert await stream.ensure_default_tune("dt1", t, Path("/x"), "uade") == 2
        assert calls == [0, 1, 2]                                # known: no second probe
        # named wires are untouched
        fresh = await stream.get_track("dt1")
        assert [stream.tune_index("dt1", fresh, w) for w in (0, 1, 4)] == [0, 1, 4]
        assert stream.tune_index("dt1", fresh, None) == 2
    finally:
        stream._DEFAULT_TUNE.pop("dt1", None)


@pytest.mark.asyncio
async def test_the_first_play_after_the_default_moves_promises_no_stale_length(monkeypatch,
                                                                             store, tmp_path):
    """M1: a record from an earlier build stores the FIRST tune's length
    (1.7 s of silence); the bare play now renders tune 2 and must not be cut
    at 1.7 s — neither on the play nor on the prewarm path."""
    async def audible(family, path, index, *, base=0, gme_data=None):
        return index == 1
    monkeypatch.setattr(stream, "_tune_audible", audible)
    t = _rec(store, "m1", subsongs=7, duration=1.7)
    try:
        assert stream._uade_expected_seconds(t, 0, "m1") == 1.7     # before: tune 1's
        fresh = await stream._bare_play_track("m1", t, Path("/x"), "uade")
        idx = stream.tune_index("m1", fresh, None)
        assert idx == 1 and stream._uade_expected_seconds(fresh, idx, "m1") == 0.0
        # the prewarm promises the same (no stale length) for its live render
        seen = []

        async def render(path, subsong=0, with_vu=True, *, live_key=None, expected_seconds=0.0,
                         subsong_base=0):
            seen.append((subsong, expected_seconds))
            return _wav16(tmp_path / "r.wav", _square(44100 * 4, 4000))
        monkeypatch.setattr(stream, "_render_uade", render)
        from soniqboom.config import settings
        monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
        mod = tmp_path / "x.jmf"
        mod.write_bytes(b"\x01" * 64)
        await stream._do_prewarm_render("m1", mod, ".jmf", None, uade_named=True, track=t)
        assert seen == [(1, 0.0)]
        conversion_cache._purge_entry(stream.uade_cache_key("m1", 1))
    finally:
        stream._DEFAULT_TUNE.pop("m1", None)


@pytest.mark.asyncio
async def test_a_fine_first_tune_is_recorded_as_probed(monkeypatch, store):
    async def audible(family, path, index, *, base=0, gme_data=None):
        return True
    monkeypatch.setattr(stream, "_tune_audible", audible)

    async def no_base(*a):
        raise AssertionError("the first tune needs no subsong base")
    monkeypatch.setattr(stream, "_uade_resolve_base", no_base)
    t = _rec(store, "dt2", subsongs=3, duration=42.0)
    try:
        assert await stream.ensure_default_tune("dt2", t, Path("/x"), "uade") == 0
        assert store.updates == [("dt2", {"default_subsong": 0})]   # the length stays
    finally:
        stream._DEFAULT_TUNE.pop("dt2", None)


@pytest.mark.asyncio
async def test_no_probe_for_single_tunes_known_defaults_or_other_families(monkeypatch, store):
    async def boom(*a, **k):
        raise AssertionError("no probe expected")
    monkeypatch.setattr(stream, "_tune_audible", boom)
    assert await stream.ensure_default_tune("s1", _track(subsongs=None), Path("/x"), "uade") is None
    assert await stream.ensure_default_tune("s2", _track(default=1), Path("/x"), "uade") == 1
    assert await stream.ensure_default_tune("s3", _track(), Path("/x"), None) is None
    assert await stream.ensure_default_tune("s4", _track(fmt="SID"), Path("/x"), "uade") is None
    assert store.updates == []


@pytest.mark.asyncio
async def test_undecided_or_all_silent_keeps_the_first_tune(monkeypatch, store):
    async def never(family, path, index, *, base=0, gme_data=None):
        return False
    monkeypatch.setattr(stream, "_tune_audible", never)
    t = _rec(store, "dt3", subsongs=40)
    try:
        assert await stream.ensure_default_tune("dt3", t, Path("/x"), "uade") == 0
        assert store.updates == [("dt3", {"default_subsong": 0})]
    finally:
        stream._DEFAULT_TUNE.pop("dt3", None)

    async def unknown(family, path, index, *, base=0, gme_data=None):
        return None if index == 0 else True
    monkeypatch.setattr(stream, "_tune_audible", unknown)
    t = _rec(store, "dt4")
    try:
        assert await stream.ensure_default_tune("dt4", t, Path("/x"), "uade") == 0
    finally:
        stream._DEFAULT_TUNE.pop("dt4", None)


@pytest.mark.asyncio
async def test_a_gme_default_resets_the_placeholder_length(monkeypatch, store, tmp_path):
    from soniqboom.config import settings

    async def audible(family, path, index, *, base=0, gme_data=None):
        return index == 1
    monkeypatch.setattr(stream, "_tune_audible", audible)
    f = tmp_path / "x.nsf"
    f.write_bytes(b"NESM\x1a" + bytes(123))
    t = _rec(store, "g1", fmt="NSF", subsongs=6, duration=1.0)
    try:
        assert await stream.ensure_default_tune("g1", t, f, "gme") == 1
        assert store.updates == [("g1", {"default_subsong": 1,
                                         "duration": float(settings.sid_default_duration)})]
    finally:
        stream._DEFAULT_TUNE.pop("g1", None)


@pytest.mark.asyncio
async def test_an_old_gme_record_learns_its_tune_count(store, tmp_path):
    gbs = bytearray(112)
    gbs[0:3] = b"GBS"
    gbs[4], gbs[5] = 12, 1
    f = tmp_path / "x.gbs"
    f.write_bytes(bytes(gbs))
    t = _rec(store, "gc1", fmt="GBS", subsongs=None)
    got = await stream._gme_backfill_tune_count("gc1", t, f)
    assert got.subsongs == 12 and store.updates == [("gc1", {"subsongs": 12})]
    one = tmp_path / "one.nsf"
    one.write_bytes(b"NESM\x1a\x01\x01\x01" + bytes(120))
    t2 = _rec(store, "gc2", fmt="NSF", subsongs=None)
    assert (await stream._gme_backfill_tune_count("gc2", t2, one)).subsongs is None


@pytest.mark.asyncio
async def test_lengths_belong_to_the_default_tune(tmp_path):
    wav = _wav16(tmp_path / "w.wav", _square(44100 * 6, 4000))          # 3 s stereo
    t = _track("bf1", default=2, duration=0.0)
    sink: list = []
    assert await stream._backfill_rendered_duration("bf1", t, wav, 0.0, subsong=0,
                                                    authoritative=True, sink=sink) is None
    assert await stream._backfill_rendered_duration("bf1", t, wav, 0.0, subsong=2,
                                                    authoritative=True, sink=sink) == 3.0
    assert sink == [("bf1", {"duration": 3.0})]
    t.duration = 42.0
    assert stream._uade_expected_seconds(t, 2, "bf1") == 42.0
    assert stream._uade_expected_seconds(t, 0, "bf1") == 0.0
    assert stream._uade_expected_seconds(_track(duration=42.0), 0) == 42.0


def test_a_probed_default_survives_an_unchanged_rescan():
    """M2: ``default_subsong = 0`` ("probed, the first tune is fine") is a
    value — a re-extract of the same file is no change (no full re-upsert
    wiping the measured length)."""
    from soniqboom.core.scanner import _same_track_content
    old = {"id": "r1", "path": "/x/a.jmf", "title": "a", "format": "Janko Mrsic-Flogel",
           "duration": 8.36, "subsongs": 7, "default_subsong": 0, "file_size": 100,
           "mtime": 1.0}
    new = {**old, "duration": 0.0, "mtime": 2.0}
    new.pop("default_subsong")
    assert _same_track_content(old, new)
    assert _same_track_content({**old, "default_subsong": 3}, new)
    assert _same_track_content({**old, "start_subsong": 0}, new)
    # another file (other size) is a change
    assert not _same_track_content(old, {**new, "file_size": 101})


def test_cast_keys_follow_the_default_only_for_a_bare_play(monkeypatch):
    from soniqboom.core import cast_render
    from soniqboom.core import store as store_mod
    rec = {"id": "c1", "format": "GBS", "subsongs": 5, "default_subsong": 2, "mtime": 1.0}

    class _S:
        def get_track(self, tid):
            return rec if tid == "c1" else None
    monkeypatch.setattr(store_mod, "get_store", lambda: _S())
    ck = conversion_cache._cache_key
    assert cast_render.rendered_cache_key("c1", ".gbs", 0) == ck("c1", "gme", subsong=2)
    assert cast_render.rendered_cache_key("c1", ".gbs", 2) == ck("c1", "gme", subsong=2)
    assert cast_render.rendered_cache_key("c1", ".gbs", 3) == ck("c1", "gme", subsong=3)


# ── /extended: the default tune for archive members and remote files ───────

@pytest.mark.asyncio
async def test_extended_probes_an_archive_members_extracted_copy(monkeypatch, store, tmp_path):
    from soniqboom.api import tracks as tracks_api
    member = tmp_path / "x.gray"
    member.write_bytes(b"\x01" * 64)
    t = _rec(store, "ax1", subsongs=4)
    t.path = str(tmp_path / "pack.zip") + "::x.gray"
    unpinned = []

    async def resolve(tid, track, *, lane="stream"):
        return member, ".gray", True, "pin-ax1"
    monkeypatch.setattr(stream, "_resolve_play_source", resolve)
    monkeypatch.setattr(stream, "_zip_unpin", lambda pid: unpinned.append(pid))

    async def audible(family, path, index, *, base=0, gme_data=None):
        assert path == member
        return index == 1
    monkeypatch.setattr(stream, "_tune_audible", audible)
    try:
        assert await tracks_api._default_tune("ax1", t, None) == (2, False)
        assert unpinned == ["pin-ax1"]
    finally:
        stream._DEFAULT_TUNE.pop("ax1", None)


@pytest.mark.asyncio
async def test_extended_waits_for_the_plays_probe_of_a_remote_file(monkeypatch, store):
    from soniqboom.api import tracks as tracks_api
    t = _rec(store, "rm1", subsongs=4)
    t.path = "ftp://host/share:/mods/x.gray"
    monkeypatch.setattr(stream, "_remote_bytes_local", lambda p: False)
    fut = asyncio.get_running_loop().create_future()
    stream._DEFAULT_PROBES["rm1"] = fut
    try:
        task = asyncio.ensure_future(tracks_api._default_tune("rm1", t, None))
        await asyncio.sleep(0.05)
        fut.set_result(1)
        assert await task == (2, False)
        # nothing running and nothing local: not fetched just for this — unknown
        stream._DEFAULT_PROBES.pop("rm1", None)
        assert await tracks_api._default_tune("rm1", t, None) == (None, False)
    finally:
        stream._DEFAULT_PROBES.pop("rm1", None)


@pytest.mark.asyncio
async def test_extended_answers_pending_while_a_slow_probe_goes_on(monkeypatch, store, tmp_path):
    """A probe slower than the reply window: /extended answers at once with
    ``pending`` and the lookup goes on; the next ask gets the tune."""
    from soniqboom.api import tracks as tracks_api
    monkeypatch.setattr(tracks_api, "_DEFAULT_TUNE_REPLY_S", 0.1)
    f = tmp_path / "slow.gray"
    f.write_bytes(b"\x01" * 64)
    t = _rec(store, "sl1", subsongs=4)
    t.path = str(f)
    release = asyncio.Event()

    async def audible(family, path, index, *, base=0, gme_data=None):
        await release.wait()
        return index == 2
    monkeypatch.setattr(stream, "_tune_audible", audible)
    try:
        t0 = asyncio.get_running_loop().time()
        assert await tracks_api._default_tune("sl1", t, None) == (None, True)
        assert asyncio.get_running_loop().time() - t0 < 1.0
        # asking again while it runs joins the same lookup (one probe)
        assert await tracks_api._default_tune("sl1", t, None) == (None, True)
        assert len([k for k in tracks_api._DEFAULT_TUNE_LOOKUPS if k == "sl1"]) == 1
        release.set()
        for _ in range(100):
            if "sl1" not in tracks_api._DEFAULT_TUNE_LOOKUPS:
                break
            await asyncio.sleep(0.02)
        assert await tracks_api._default_tune("sl1", t, None) == (3, False)
    finally:
        release.set()
        stream._DEFAULT_TUNE.pop("sl1", None)
        stream._DEFAULT_PROBES.pop("sl1", None)


@pytest.mark.asyncio
async def test_extended_stays_pending_while_the_probe_outlives_the_lookup(monkeypatch, store, tmp_path):
    """The lookup's own wait for the probe can end before the probe does: the
    reply must still say pending (the client keeps asking), not "unknown"."""
    from soniqboom.api import tracks as tracks_api
    monkeypatch.setattr(tracks_api, "_DEFAULT_TUNE_REPLY_S", 0.3)
    monkeypatch.setattr(tracks_api, "_DEFAULT_TUNE_WAIT_S", 0.05)
    f = tmp_path / "long.gray"
    f.write_bytes(b"\x01" * 64)
    t = _rec(store, "lp1", subsongs=4)
    t.path = str(f)
    release = asyncio.Event()

    async def audible(family, path, index, *, base=0, gme_data=None):
        await release.wait()
        return index == 1
    monkeypatch.setattr(stream, "_tune_audible", audible)
    try:
        got, pending = await tracks_api._default_tune("lp1", t, None)
        assert stream.default_probe_running("lp1")
        assert pending is True
    finally:
        release.set()
        for _ in range(100):
            if not stream.default_probe_running("lp1"):
                break
            await asyncio.sleep(0.02)
        stream._DEFAULT_TUNE.pop("lp1", None)
        stream._DEFAULT_PROBES.pop("lp1", None)
        stream._DEFAULT_PROBE_STATE.pop("lp1", None)


@pytest.mark.asyncio
async def test_extended_reports_an_undecided_pick_as_pending_while_probing(monkeypatch, store):
    """An undecided probe's pick plays now, but it isn't the decided default:
    while a probe still runs the reply carries it AND says pending."""
    from soniqboom.api import tracks as tracks_api
    t = _rec(store, "pk1", subsongs=12)
    monkeypatch.setattr(stream, "_default_tune_decided", lambda tid, tr: None)
    monkeypatch.setattr(stream, "default_tune_known", lambda tid, tr: 2)
    monkeypatch.setattr(stream, "default_probe_running", lambda tid: True)
    assert await tracks_api._default_tune("pk1", t, None) == (3, True)
    monkeypatch.setattr(stream, "default_probe_running", lambda tid: False)
    assert await tracks_api._default_tune("pk1", t, None) == (3, False)
    monkeypatch.setattr(stream, "_default_tune_decided", lambda tid, tr: 10)
    assert await tracks_api._default_tune("pk1", t, None) == (11, False)


# ── the stream endpoint ─────────────────────────────────────────────────────

async def _noauth(*a, **k):
    return None


def _app(monkeypatch, tmp_path, store):
    from fastapi import FastAPI
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_stream_bare_id_plays_the_default_named_tunes_play_themselves(monkeypatch,
                                                                          tmp_path, store):
    import httpx
    mod = tmp_path / "x.gray"
    mod.write_bytes(b"\x00" * 64)
    t = _rec(store, "st1", subsongs=4)
    store.recs["st1"]["path"] = str(mod)
    rendered = []

    async def audible(family, path, index, *, base=0, gme_data=None):
        return index != 0
    monkeypatch.setattr(stream, "_tune_audible", audible)

    async def render(path, subsong=0, with_vu=True, *, live_key=None, expected_seconds=0.0,
                     subsong_base=0):
        rendered.append(subsong)
        return _wav16(tmp_path / f"r{len(rendered)}.wav",
                      _square(4410, 4000) if subsong else [0] * 4410)
    monkeypatch.setattr(stream, "_render_uade", render)
    monkeypatch.setattr(stream, "_spawn_uade_vu", lambda *a, **k: None)
    app = _app(monkeypatch, tmp_path, store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            # HEAD before any probe: no length promised for the bare id
            h = await c.head("/api/stream/st1?u=a&p=b")
            assert h.status_code == 200 and "content-length" not in h.headers
            r = await c.get("/api/stream/st1?u=a&p=b")              # bare id
            assert r.status_code == 200 and rendered == [1]
            assert store.recs["st1"]["default_subsong"] == 1
            r = await c.get("/api/stream/st1?u=a&p=b&subsong=1")    # tune 2: itself
            assert r.status_code == 200 and rendered == [1]         # the same render, cached
            r = await c.get("/api/stream/st1?u=a&p=b&subsong=0")    # tune 1: the empty stub
            assert r.status_code == 422 and rendered == [1, 0]
            assert r.json()["detail"] == conversion_cache.SILENT_RENDER_DETAIL
            r = await c.get("/api/stream/st1?u=a&p=b&subsong=2")
            assert r.status_code == 200 and rendered == [1, 0, 2]
            st = await c.get("/api/stream/st1/render-status")
            assert st.json()["state"] == "complete"                 # the bare play's tune 2
            st = await c.get("/api/stream/st1/render-status?subsong=0")
            assert st.json()["state"] == "failed"                   # the empty stub
            h = await c.head("/api/stream/st1?u=a&p=b")             # default known now
            assert int(h.headers["content-length"]) > 44
        # an in-process caller's 0 (Subsonic's bare id, the cast byte server)
        # is the bare play — its own request carries no ``subsong``
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": "/rest/stream",
                       "query_string": b"id=st1&u=a&p=b", "headers": [], "app": app})
        stream._set_cast_internal_bypass(True)
        resp = await stream.stream_track(track_id="st1", request=req, seek=0.0, subsong=0,
                                         file_path=None, target_format=None,
                                         max_bitrate_kbps=0, target_sample_rate=0,
                                         force_transcode=False, sb_session=None,
                                         u=None, p=None, s=None, t=None)
        assert resp.headers.get("x-cache") == "hit" and rendered == [1, 0, 2]
    finally:
        stream._DEFAULT_TUNE.pop("st1", None)
        for k in ("st1__sub1", "st1__sub2"):
            conversion_cache._purge_entry(k)
        conversion_cache._silent_keys.clear()


# ── old MED / early DSIK by content ─────────────────────────────────────────

MED4 = FS / "Music Editor/Alex Van Starrex/synth.med"
DSM10 = FS / "Digital Sound Interface Kit/Necros/andante.dsm"


def test_old_med_magic():
    assert stream._old_med(b"MED\x04") and stream._old_med(b"MED\x02")
    assert not stream._old_med(b"MMD0") and not stream._old_med(b"MED\x05")


@pytest.mark.asyncio
@pytest.mark.skipif(not DSM10.exists(), reason="DSIK fixture missing")
async def test_an_early_dsik_module_is_a_clear_422():
    with pytest.raises(HTTPException) as ei:
        await stream._render_tracker(DSM10)
    assert ei.value.status_code == 422 and "early DSM variant" in ei.value.detail


@pytest.mark.asyncio
@pytest.mark.skipif(not MED4.exists() or not shutil.which("zxtune123"),
                    reason="MED 4 fixture or zxtune123 missing")
async def test_an_old_med_module_plays_through_zxtune_bounded(monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "sid_default_duration", 30)
    out = await stream._render_tracker(MED4)
    try:
        secs = stream._wav_audio_seconds(out)
        assert abs(secs - 30.0) < 0.01                  # zxtune alone: 1:03:40
        assert out.stat().st_size == 44 + 30 * 44100 * 4
        assert silence.wav_audible(out) is True
        a = array.array("h")
        a.frombytes(out.read_bytes()[-4000:])
        assert max(map(abs, a)) < 400                   # faded out at the cut
    finally:
        out.unlink(missing_ok=True)


# ── client SID uploads are judged too ───────────────────────────────────────

@pytest.mark.asyncio
async def test_a_silent_client_sid_render_is_refused(monkeypatch, conv):
    import hashlib
    from soniqboom.api import tracks as tracks_api
    t = types.SimpleNamespace(id="su1", format="SID", duration=5.0, subsongs=None,
                              start_subsong=None, hvsc_lengths=None, path="/x/a.sid")

    async def get_track(tid):
        return t
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    body = bytearray(44 + 5 * 88200)
    body[:44] = stream._build_wav_header(44100, 1, 5 * 44100, bits_per_sample=16)

    class _Up:
        headers = {"content-length": str(len(body))}

        async def stream(self):
            yield bytes(body)
    key = conversion_cache._cache_key("su1", "sid", 0, duration=5)
    with pytest.raises(HTTPException) as ei:
        await tracks_api.upload_sid_audio("su1", _Up(), subsong=0, duration=5,
                                          wav_sha256=hashlib.sha256(body).hexdigest(),
                                          tune=None, user=None)
    assert ei.value.status_code == 422
    assert await conversion_cache.get_cached(key) is None
    conversion_cache._silent_keys.pop(key, None)


# ── real renders ────────────────────────────────────────────────────────────

@needs_uade
@pytest.mark.asyncio
@pytest.mark.parametrize("path,count,want", [
    (MORBASE, 8, 1),          # tune 0: 0.3 s of nothing; 1–7 music
    (ECO, 4, 1),              # tune 0: 0.05 s
    (STARGOOSE, 5, 1),
    (DW, 0, None),            # one tune: no probe
])
async def test_real_modules_default_to_their_first_real_tune(store, path, count, want):
    if not path.exists():
        pytest.skip("fixture missing")
    t = _rec(store, "real-" + path.stem, subsongs=count or None)
    try:
        assert await stream.ensure_default_tune(t.id, t, path, "uade") == want
    finally:
        stream._DEFAULT_TUNE.pop(t.id, None)


@needs_uade
@pytest.mark.asyncio
async def test_real_uade_probe_verdicts():
    if not MORBASE.exists():
        pytest.skip("fixture missing")
    assert await stream._tune_audible("uade", MORBASE, 0) is False
    assert await stream._tune_audible("uade", MORBASE, 1) is True


@pytest.mark.skipif(not gme_render.is_available() or not GBS.exists(),
                    reason="libgme or the GBS fixture missing")
def test_libgme_probe_on_real_files():
    assert gme_render.tune_audible(GBS.read_bytes(), 0) is True
    # a silent track: the MSX KSS rips play mute in libgme (track 0)
    if KSS_MSX.exists():
        assert gme_render.tune_audible(KSS_MSX.read_bytes(), 0) is False


@pytest.mark.asyncio
async def test_a_cached_first_tune_spares_the_probe_only_when_it_is_music(conv, monkeypatch,
                                                                       store):
    """A first tune already in the cache (a play before this version) is the
    default without a probe — unless it is silence or a stub: a render cached
    before silent renders were refused must not decide it."""
    calls = []

    async def audible(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        return index == 1
    monkeypatch.setattr(stream, "_tune_audible", audible)
    for tid, samples, want, probed in (
            ("cd1", _square(44100 * 4, 4000), 0, []),       # 2 s of music cached
            ("cd2", [0] * (44100 * 40), 1, [0, 1]),           # 20 s of silence cached
            ("cd3", _square(4410, 4000), 1, [0, 1])):         # a 0.05 s stub cached
        calls.clear()
        src = _wav16(conv / f"{tid}.wav", samples)
        await conversion_cache.store_cached(stream.uade_cache_key(tid, 0), "uade", src)
        t = _rec(store, tid, subsongs=3)
        try:
            got = await stream.ensure_default_tune(tid, t, Path("/x"), "uade")
            assert (got, calls) == (want, probed), tid
        finally:
            stream._DEFAULT_TUNE.pop(tid, None)
            conversion_cache._purge_entry(stream.uade_cache_key(tid, 0))


# ── Final-QA M1: a probe under load records nothing it didn't test ─────────

@pytest.fixture()
def fresh_slots(monkeypatch):
    """Render gates of this test's own (the module's would stay bound to the
    first loop that waited on them)."""
    monkeypatch.setattr(stream, "_render_sem", asyncio.Semaphore(stream._RENDER_SLOTS))
    monkeypatch.setattr(stream, "_bg_render_sem",
                        stream._PriorityGate(max(1, stream._RENDER_SLOTS - 1)))


async def _hold_slots(seconds: float) -> list:
    async def hold():
        async with stream._render_sem:
            await asyncio.sleep(seconds)
    holders = [asyncio.create_task(hold()) for _ in range(stream._RENDER_SLOTS)]
    await asyncio.sleep(0.01)
    return holders


async def _settled(tid: str, limit: float = 10.0) -> None:
    """Wait until no probe run of ``tid`` is going (a background chain included)."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        fut = stream._DEFAULT_PROBES.get(tid)
        if fut is None:
            await asyncio.sleep(0.02)
            if stream._DEFAULT_PROBES.get(tid) is None:
                return
            continue
        await asyncio.wait({fut}, timeout=deadline - time.monotonic())
    raise AssertionError(f"probe of {tid} still running")


@pytest.mark.asyncio
async def test_waiting_for_a_render_slot_costs_the_probe_no_candidate(monkeypatch, store,
                                                                      fresh_slots):
    """Every render slot busy for longer than the probe budget: the budget
    starts once the probe holds a slot, so it still tests its candidates
    (it used to exit before the first and record tune 1 for good)."""
    calls = []

    async def audible(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        return index >= 2
    monkeypatch.setattr(stream, "_tune_audible", audible)
    monkeypatch.setattr(stream, "_DEFAULT_PROBE_BUDGET_S", 0.3)
    t = _rec(store, "ld1", subsongs=6)
    try:
        holders = await _hold_slots(0.6)
        t0 = time.monotonic()
        assert await stream.ensure_default_tune("ld1", t, Path("/x"), "uade") == 2
        assert time.monotonic() - t0 >= 0.5                     # it waited for a slot
        assert calls == [0, 1, 2]
        assert store.updates == [("ld1", {"default_subsong": 2, "duration": 0.0})]
        await asyncio.gather(*holders)
    finally:
        stream._DEFAULT_TUNE.pop("ld1", None)


@pytest.mark.asyncio
async def test_no_slot_in_time_is_undecided_and_probed_again_later(monkeypatch, store,
                                                                   fresh_slots):
    """The QA repro: slots busy past the bounded wait — nothing is tested,
    so nothing is written to the record.  The next bare play doesn't wait
    again; once the retry time is up a probe runs in the background and
    records the real default."""
    calls = []

    async def audible(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        return index >= 2
    monkeypatch.setattr(stream, "_tune_audible", audible)
    monkeypatch.setattr(stream, "_DEFAULT_PROBE_SLOT_WAIT_S", 0.1)
    t = _rec(store, "ld2", subsongs=6)
    try:
        holders = await _hold_slots(0.4)
        t0 = time.monotonic()
        fresh = await stream._bare_play_track("ld2", t, Path("/x"), "uade")
        assert 0.1 <= time.monotonic() - t0 < 0.4               # the bounded slot wait
        assert calls == [] and store.updates == []              # nothing tested, nothing recorded
        assert store.recs["ld2"].get("default_subsong") is None
        assert stream.tune_index("ld2", fresh, None) == 0       # the first tune, for now
        await asyncio.gather(*holders)
        t0 = time.monotonic()
        await stream._bare_play_track("ld2", t, Path("/x"), "uade")
        assert calls == [] and time.monotonic() - t0 < 0.05     # no second wait, no retry yet
        stream._DEFAULT_PROBE_STATE["ld2"]["retry"] = 0.0      # the retry time is up
        t0 = time.monotonic()
        fresh = await stream._bare_play_track("ld2", t, Path("/x"), "uade")
        assert time.monotonic() - t0 < 0.05                     # plays at once …
        await _settled("ld2")                                   # … the probe runs behind it
        assert calls == [0, 1, 2] and store.recs["ld2"]["default_subsong"] == 2
        fresh = await stream.get_track("ld2")
        assert stream.tune_index("ld2", fresh, None) == 2
    finally:
        stream._DEFAULT_TUNE.pop("ld2", None)
        stream._DEFAULT_PROBE_STATE.pop("ld2", None)


@pytest.mark.asyncio
async def test_judged_candidates_are_kept_three_bare_plays_try_each_once(monkeypatch, store,
                                                                         fresh_slots):
    """The QA repro: the budget runs out after candidates judged silent (the
    real tune is the 11th).  Each candidate is tried once in all — the
    probe resumes after the judged ones, behind the play, never on its clock
    again — and the tune played moves to the one found."""
    calls = []

    async def audible(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        await asyncio.sleep(0.05)
        return index >= 10
    monkeypatch.setattr(stream, "_tune_audible", audible)
    monkeypatch.setattr(stream, "_DEFAULT_PROBE_BUDGET_S", 0.12)
    t = _rec(store, "tp", subsongs=12)
    played = []
    try:
        for n in range(3):
            st = stream._DEFAULT_PROBE_STATE.get("tp")
            if st is not None:
                st["retry"] = 0.0                               # any wait has expired
            t0 = time.monotonic()
            fresh = await stream._bare_play_track("tp", t, Path("/x"), "uade")
            waited = time.monotonic() - t0
            played.append(stream.tune_index("tp", fresh, None))
            if n == 0:
                assert waited < 0.3                             # one budget (+ its last candidate)
                assert played[0] == 3 and calls[:3] == [0, 1, 2]    # the first one not judged yet
                assert "tp" in stream._DEFAULT_PROBES           # going on behind the play
            else:
                assert waited < 0.05                            # never waits again
            await _settled("tp")
        assert calls == list(range(11))                         # each candidate once
        assert played[1:] == [10, 10]
        assert store.recs["tp"]["default_subsong"] == 10
        assert store.updates == [("tp", {"default_subsong": 10, "duration": 0.0})]
    finally:
        stream._DEFAULT_TUNE.pop("tp", None)
        stream._DEFAULT_PROBE_STATE.pop("tp", None)


@pytest.mark.asyncio
async def test_an_undecided_verdict_or_a_spent_budget_is_not_recorded(monkeypatch, store,
                                                                      fresh_slots):
    """A candidate that can't be told (a renderer that timed out) is kept as
    the pick — never written, tried again only after a while.  A spent
    budget is no verdict either: the probe goes on behind the play, and
    every candidate judged empty is one — the first tune, recorded."""
    async def timed_out(family, path, index, *, base=0, gme_data=None):
        return None if index == 1 else False
    monkeypatch.setattr(stream, "_tune_audible", timed_out)
    t = _rec(store, "ud1", subsongs=5)
    try:
        assert await stream.ensure_default_tune("ud1", t, Path("/x"), "uade") == 1
        assert store.updates == [] and stream.default_tune_known("ud1", t) == 1
        st = stream._DEFAULT_PROBE_STATE["ud1"]
        assert st["next"] == 1 and st["retry"] > time.monotonic()
        assert "ud1" not in stream._DEFAULT_PROBES              # no retry at once
    finally:
        stream._DEFAULT_PROBE_STATE.pop("ud1", None)

    calls = []

    async def slow_silent(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        await asyncio.sleep(0.06)
        return False
    monkeypatch.setattr(stream, "_tune_audible", slow_silent)
    monkeypatch.setattr(stream, "_DEFAULT_PROBE_BUDGET_S", 0.1)
    t = _rec(store, "ud2", subsongs=12)
    try:
        assert await stream.ensure_default_tune("ud2", t, Path("/x"), "uade") == 2
        assert store.updates == []                              # untested candidates left
        await _settled("ud2")
        assert calls == list(range(12))
        assert store.updates == [("ud2", {"default_subsong": 0})]
    finally:
        stream._DEFAULT_TUNE.pop("ud2", None)

    monkeypatch.setattr(stream, "_DEFAULT_PROBE_BUDGET_S", 8.0)
    t = _rec(store, "ud3", subsongs=3)
    try:
        assert await stream.ensure_default_tune("ud3", t, Path("/x"), "uade") == 0
        assert store.updates[-1] == ("ud3", {"default_subsong": 0})   # all three judged
    finally:
        stream._DEFAULT_TUNE.pop("ud3", None)


@pytest.mark.asyncio
async def test_a_failed_probe_is_tried_again_after_a_while(monkeypatch, store, fresh_slots):
    calls = []

    async def boom(family, path, index, *, base=0, gme_data=None):
        calls.append(index)
        raise RuntimeError("renderer exploded")
    monkeypatch.setattr(stream, "_tune_audible", boom)
    t = _rec(store, "fp1", subsongs=4)
    try:
        assert await stream.ensure_default_tune("fp1", t, Path("/x"), "uade") == 0
        assert await stream.ensure_default_tune("fp1", t, Path("/x"), "uade") == 0
        assert calls == [0] and store.updates == []
        stream._DEFAULT_PROBE_STATE["fp1"]["retry"] = 0.0
        assert await stream.ensure_default_tune("fp1", t, Path("/x"), "uade") == 0
        await _settled("fp1")
        assert calls == [0, 0] and store.updates == []
    finally:
        stream._DEFAULT_PROBE_STATE.pop("fp1", None)


def test_a_probed_default_survives_a_rescan_that_changes_another_field():
    """A same-file re-extract that changes another field (a title the
    extractor now reads better) is a full upsert — it dropped the probed
    default tune, and the next bare play probed again.  Carried while the
    file is the same and its tune count still covers it, with the length
    stored for a default past the first tune."""
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    base = {"id": "c1", "path": "/x/a.jmf", "title": "old", "format": "Janko Mrsic-Flogel",
            "subsongs": 7, "file_size": 100, "mtime": 1.0}
    st.upsert_tracks_batch([{**base, "duration": 42.0, "default_subsong": 3}])
    st.upsert_tracks_batch([{**base, "title": "new", "duration": 0.0, "mtime": 2.0}])
    t = st.get_track("c1")
    assert (t["title"], t["default_subsong"], t["duration"]) == ("new", 3, 42.0)
    # 0 is a value: carried, and the fresh length stays (it is tune 1's)
    st.upsert_tracks_batch([{**base, "id": "c2", "duration": 9.0, "default_subsong": 0}])
    st.upsert_tracks_batch([{**base, "id": "c2", "title": "new", "duration": 8.5}])
    assert (st.get_track("c2")["default_subsong"], st.get_track("c2")["duration"]) == (0, 8.5)
    # another file (size, or content hash) or too few tunes now: dropped
    for change in ({"file_size": 101}, {"subsongs": 3}, {"file_md5": "b"}, {"subsongs": None}):
        st.upsert_tracks_batch([{**base, "id": "c3", "file_md5": "a", "duration": 42.0,
                                 "default_subsong": 3}])
        st.upsert_tracks_batch([{**base, "id": "c3", "file_md5": "a", "title": "new",
                                 "duration": 0.0, **change}])
        assert st.get_track("c3").get("default_subsong") is None, change
        assert st.get_track("c3")["duration"] == 0.0, change


def test_a_default_tune_write_is_not_an_enrichment_change():
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.upsert_track({"id": "e1", "path": "/x/e.jmf", "title": "e", "subsongs": 4})
    cur = st.enrich_cursor()
    st.update_track_fields("e1", {"default_subsong": 2})
    assert st.enrich_changes(cur, 100) == set()


@pytest.mark.asyncio
async def test_the_psf_magic_is_never_read_on_the_event_loop(tmp_path, monkeypatch):
    """An O(1) path (HEAD, waveform, VU) on the loop read the file there — a
    mount that stopped answering froze the server.  The read runs off the
    loop; a disk that answers routes the first request right, one that
    hangs costs the loop a bounded wait and the file counts as a PSF rip
    until its read is in."""
    import threading
    fast = tmp_path / "fast.psf"
    fast.write_bytes(b"\x00\x01=g" + bytes(64))
    t = types.SimpleNamespace(genre=["Chiptune"], mtime=5.0)
    try:
        assert stream._render_ident(str(fast), t) == (".psf", True)      # first hit right
    finally:
        stream._PSF_AMIGA_MEMO.pop((str(fast), 5.0), None)

    mod = tmp_path / "axelf.psf"
    mod.write_bytes(b"\x00\x01=g" + bytes(64))
    loop_thread = threading.get_ident()
    read_in = []
    gate = threading.Event()
    real = stream._psf_has_magic

    def slow_read(path):
        read_in.append(threading.get_ident())
        gate.wait(5)                                # a share that stopped answering
        return real(path)
    monkeypatch.setattr(stream, "_psf_has_magic", slow_read)
    try:
        t0 = time.monotonic()
        assert stream._render_ident(str(mod), t) == (".psf", False)     # a bounded wait …
        assert stream._render_ident(str(mod), t) == (".psf", False)     # … once: read in flight
        assert time.monotonic() - t0 < 1.0 and loop_thread not in read_in
        gate.set()
        for _ in range(200):
            if (str(mod), 5.0) in stream._PSF_AMIGA_MEMO:
                break
            await asyncio.sleep(0.01)
        assert stream._render_ident(str(mod), t) == (".psf", True)
        assert len(read_in) == 1 and read_in[0] != loop_thread
        assert (str(mod), 5.0) not in stream._PSF_AMIGA_READING
    finally:
        gate.set()
        stream._PSF_AMIGA_MEMO.pop((str(mod), 5.0), None)
