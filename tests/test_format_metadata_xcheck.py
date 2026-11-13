# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tracker, chip and Amiga files' own data, as the scan stores it (a
cross-check against each file's header and its own player found these):
module names with a stray control byte, the FA04/FA06/FA08 magics, a
31-sample module without a magic, junk named ``.mod``, AHX / HivelyTracker
subsongs and channels; SAP's header (name, author, date, each tune's TIME),
SPC / NSF / GBS / VGM years and the SPC comment, SC68's disk name, author
and track lengths (``info68``), AdLib titles and authors (``adplay``); the
TFMX ST player for an ``mdat.`` module the TFMX player refuses, and a Sonix
score's missing instruments (archived: silent stand-ins, ``partial`` — none of
them there, ``corrupt``; on disk: refused at play, naming them, never marked).
Also from the review of those fixes: a 15-sample module, a remote scan's
front window of a WAV or of a module libopenmpt refuses, a WAV whose chunks
come in another order, an SPC's dump date."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import gme_render, metadata as md, scanner, uade_formats, xpk

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "internal/format-samples"
TD = ROOT / "internal/testdata"
UADE = shutil.which("uade123") or ("/opt/homebrew/bin/uade123"
                                   if os.path.exists("/opt/homebrew/bin/uade123") else None)


def _x(p: Path) -> dict:
    return md.extract(p, "x").model_dump()


def _have(p: Path):
    return pytest.mark.skipif(not p.is_file(), reason=f"no {p.name}")


# ── trackers ────────────────────────────────────────────────────────────────

def test_a_stray_control_byte_no_longer_drops_a_sample_name():
    assert md._tracker_name(b"bass\x01drum\x00\x00") == "bassdrum"
    assert md._tracker_name(b"\x00" * 22) == ""


def _m15() -> bytes:
    """A 15-sample Soundtracker module (no tag at 1080): two named samples,
    song length 2 over patterns 0 and 1."""
    h = b"st song".ljust(20, b"\0")
    h += b"kick".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 1)
    h += b"snare".ljust(22, b"\0") + struct.pack(">HBBHH", 0, 0, 0, 0, 1)
    h += (b"\0" * 30) * 13
    h += bytes([2, 0x78]) + bytes([0, 1]) + bytes(126)
    pat = bytearray(1024)
    pat[0:4] = bytes([0x10, 0xAC, 0x00, 0x00])
    return h + bytes(pat) * 2 + bytes(range(256)) * 4


def _tagged(magic: bytes, channels: int) -> bytes:
    """A 31-sample module tagged ``magic`` at 1080, one pattern; its 16th
    sample is named — where a 15-sample module's song length and orders
    would be (a 15-sample read loses that name)."""
    h = (b"fa song".ljust(20, b"\0") + b"lead".ljust(22, b"\0")
         + struct.pack(">HBBHH", 16, 0, 64, 0, 1) + (b"\0" * 30) * 14
         + b"pad".ljust(22, b"\0") + struct.pack(">HBBHH", 0, 0, 0, 0, 1)
         + (b"\0" * 30) * 15)
    h += bytes([1, 127]) + bytes(128) + magic
    pat = bytearray(256 * channels)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, 0])
    return h + bytes(pat) + bytes(32)


@pytest.mark.parametrize("in_worker", [True, False])
@pytest.mark.parametrize("name, data, want", [
    ("m15.mod", _m15(), ("st song", 4, 2, ["kick", "snare"])),
    ("fa8.mod", _tagged(b"FA08", 8), ("fa song", 8, 1, ["lead", "pad"])),
    ("fest.mod", _tagged(b"FEST", 4), ("fa song", 4, 1, ["lead", "pad"])),   # (a tag: 31 samples)
])
def test_mod_sample_count_and_channels(tmp_path, monkeypatch, in_worker, name, data, want):
    if not shutil.which("openmpt123"):
        pytest.skip("openmpt123 not installed")
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", in_worker)
    f = tmp_path / name
    f.write_bytes(data)
    d = _x(f)
    assert (d["title"], d["channels"], d["patterns"], d["instruments"]) == want


def test_the_header_reads_15_samples_without_a_tag(tmp_path, monkeypatch):
    # (the header parse alone — no libopenmpt to fill in)
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", False)
    monkeypatch.setattr(md.forksafe, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    for name, data, names in (("m15.mod", _m15(), ["kick", "snare"]),
                              ("fest.mod", _tagged(b"FEST", 4), ["lead", "pad"])):
        f = tmp_path / name
        f.write_bytes(data)
        assert _x(f)["instruments"] == names, name


REF8 = SAMPLES / "Soundtracker 2.6/Jan/ref 8.mod"


@_have(REF8)
def test_a_31_sample_module_without_a_magic_keeps_its_names_and_patterns():
    d = _x(REF8)
    assert (d["format"], d["channels"], d["patterns"]) == ("ProTracker", 4, 17)
    assert d["instruments"][0] == "(C) 1990 by stalker!"


def test_junk_named_mod_is_no_track(tmp_path):
    f = tmp_path / "junk.mod"
    f.write_bytes(os.urandom(4096))
    with pytest.raises(ValueError):
        md.extract(f, "x")


AHX = TD / "tracker/agony end.ahx"
HVL = SAMPLES / "HivelyTracker/Breed/testing123.hvl"


@_have(AHX)
def test_ahx_subsongs_from_its_header(tmp_path):
    assert _x(AHX)["subsongs"] is None                  # (byte 13: 0 — the main song only)
    b = bytearray(AHX.read_bytes())
    b[13] = 2
    f = tmp_path / "sub.ahx"
    f.write_bytes(bytes(b))
    d = _x(f)
    assert (d["subsongs"], d["channels"]) == (3, 4)


@_have(HVL)
def test_hivelytracker_channels_from_its_header():
    assert _x(HVL)["channels"] == (HVL.read_bytes()[8] >> 2) + 4 == 5


# ── chip / console ──────────────────────────────────────────────────────────

def test_sap_header_and_its_tune_times():
    data = (b'SAP\r\nAUTHOR "A. Guilfoyle"\r\nNAME "The Domain"\r\nDATE "1986"\r\n'
            b'SONGS 2\r\nTYPE B\r\nTIME 01:27.34\r\nTIME 00:45.82 LOOP\r\n\xff\xff' + bytes(64))
    h = gme_render.sap_header(data)
    assert (h["name"], h["author"], h["date"]) == ("The Domain", "A. Guilfoyle", "1986")
    assert h["times"] == pytest.approx([87.34, 45.82])
    assert gme_render.play_seconds(data, 1) == pytest.approx(45.82)
    assert gme_render.play_seconds(data, 5) is None


SAP = SAMPLES / "Slight Atari Player/A. Guilfoyle/domain of the undead.sap"


@_have(SAP)
def test_a_sap_file_is_named_dated_and_timed():
    d = _x(SAP)
    assert (d["title"], d["artist"], d["year"]) == (
        "The Domain of the Undead", "A. Guilfoyle", 1986)
    assert len(d["tune_lengths"]) == 3 and d["duration"] == d["tune_lengths"][0]
    assert d["duration"] > 87.34                       # (its TIME, then the fade)


@pytest.mark.parametrize("rel, year", [
    ("testdata/game/mmgg-02.spc", 1993),
    ("format-samples/Nintendo Sound Format/3-108/new rally-x.nsf", 1981),
    ("testdata/game/hello kitty no happy house.gbs", 1997),
    ("testdata/game/09.vgz", 1995),
])
def test_chip_years_from_the_files_own_fields(rel, year):
    p = ROOT / "internal" / rel
    if not p.is_file():
        pytest.skip(f"no {p.name}")
    assert _x(p)["year"] == year


@_have(TD / "game/mmgg-02.spc")
def test_an_spc_comment():
    assert _x(TD / "game/mmgg-02.spc")["comment"] == "Level 2"


MICKEY = TD / "game/mickey to minnie - magical adventure 2 (as1) (end boss 2).spc"


@_have(MICKEY)
def test_an_spc_dump_date_is_no_year():
    assert MICKEY.read_bytes()[0x9E:0xA2] == bytes([15, 1, 0xD0, 7])   # (dumped 15/01/2000)
    assert _x(MICKEY)["year"] is None


# ── a remote scan's front window ────────────────────────────────────────────

WAV16 = SAMPLES / "mainstream/sample-16.wav"


@_have(WAV16)
def test_a_wav_window_gets_the_whole_files_length():
    b = WAV16.read_bytes()
    assert md.wav_window_length(b[:128 * 1024], len(b)) == pytest.approx(12.0)
    assert md.wav_window_length(b"RIFF\0\0\0\0WAVE", len(b)) is None


def test_a_wav_with_data_before_fmt(tmp_path):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(16000))
    b = buf.getvalue()
    fmt, data = b[12:36], b[36:]
    f = tmp_path / "odd.wav"
    f.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(data)) + b"WAVE" + data + fmt)
    d = _x(f)
    assert (d["duration"], d["sample_rate"], d["format"]) == (1.0, 8000, "WAV")


def test_a_libopenmpt_refusal_of_a_window_fetches_more(tmp_path, monkeypatch):
    monkeypatch.setattr(md, "_LIBOPENMPT_IN_PROCESS", False)
    if not shutil.which("openmpt123"):
        pytest.skip("openmpt123 not installed")
    f = tmp_path / "m15.mod"
    f.write_bytes(_m15()[:700])                         # (cut before its patterns)
    with pytest.raises(md.NotPlayableHere) as e:
        md.extract(f, "x")
    text = f"NotPlayableHere: {e.value}"
    assert scanner._not_playable_here(text) and scanner._window_refusal(text)
    assert not scanner._window_refusal("NotPlayableHere: a song in the Fred Editor's own format")


KITTY = TD / "game/hello kitty no happy house.gbs"


@_have(KITTY)
def test_a_default_tune_other_than_1_is_no_start_subsong(tmp_path):
    # (``start_subsong`` maps the wire for SID / SNDH only: a GBS's bare play
    # plays the default its probe decides — the record must not claim one)
    b = bytearray(KITTY.read_bytes())
    assert b[4] == 44
    b[5] = 3                                            # (default tune 3)
    f = tmp_path / "def3.gbs"
    f.write_bytes(bytes(b))
    d = _x(f)
    assert (d["subsongs"], d["start_subsong"]) == (44, None)


GEAR = TD / "atari/sc68/gear.sc68"


@_have(GEAR)
def test_sc68_disk_name_author_and_lengths():
    if not (md._find_atari_binary("sc68_path", "sc68") or md._find_atari_binary("", "info68")):
        pytest.skip("sc68 not installed")
    d = _x(GEAR)
    assert (d["title"], d["artist"], d["subsongs"]) == ("5th Gear", "Jochen Hippel (Mad Max)", 5)
    assert d["tune_lengths"] == [391.0, 76.0, 20.0, 23.0, 10.0] and d["duration"] == 391.0
    assert d["start_subsong"] is None


ALLOY = TD / "adlib/ALLOYRUN.A2M"


@_have(ALLOY)
def test_adlib_title_and_author_from_adplay():
    if not md._find_atari_binary("adplay_path", "adplay"):
        pytest.skip("adplay not installed")
    d = _x(ALLOY)
    assert (d["title"], d["artist"]) == ("ALLOYRUN (REMASTERED VERSiON)", "VOiD / REALiTY")
    n = _x(TD / "adlib/Newtune.a2m")
    assert (n["title"], n["artist"]) == ("Newtune", "")   # (none stated: the file name)


# ── Amiga (uade) ────────────────────────────────────────────────────────────

TFMX_ST = SAMPLES / "TFMX ST/- unknown/mdat.dynablaster st"
TFMX = SAMPLES / "TFMX/Axk/mdat.unicorn"


@pytest.mark.skipif(not UADE, reason="uade not installed")
@_have(TFMX_ST)
def test_an_mdat_module_the_tfmx_player_refuses_plays_with_tfmx_st():
    conf = uade_formats.conf_path()
    if not (conf and (conf.parent / "players" / "TFMX_ST").is_file()):
        pytest.skip("no TFMX_ST player installed")
    uade_formats._ARGS_CACHE.clear()
    args = uade_formats.player_args(TFMX_ST, UADE)
    assert args[:1] == ["-P"] and Path(args[1]).name == "TFMX_ST"
    d = _x(TFMX_ST)
    assert (d["format"], d["subsongs"]) == ("TFMX ST", 12)
    if TFMX.is_file():
        assert uade_formats.player_args(TFMX, UADE) == []   # (uade's own pick plays it)


SMUS = SAMPLES / "IFF-SMUS/- unknown/something in space.smus"
needs_smus = pytest.mark.skipif(not SMUS.is_file(), reason="no Sonix score")


@needs_smus
def test_sonix_defect_partial_or_corrupt():
    data = SMUS.read_bytes()
    names = uade_formats.sonix_instrument_names(data)
    assert len(names) == 17
    assert uade_formats.sonix_defect(data, []) is None
    assert uade_formats.sonix_defect(data, names[:1])[0] == "partial"
    assert uade_formats.sonix_defect(data, list(names))[0] == "corrupt"


def test_sonix_instruments_are_compared_by_file_name():
    import unicodedata
    key = uade_formats.sonix_instr_key
    assert key("sub/Piano") == key("piano") == "piano.instr"
    assert key("..") == key("") == ""
    assert key(unicodedata.normalize("NFD", "Café")) == key("CAFÉ")


@needs_smus
def test_a_loose_sonix_score_without_its_instruments(tmp_path):
    from soniqboom.api import stream
    names = uade_formats.sonix_instrument_names(SMUS.read_bytes())
    some = tmp_path / "some"
    some.mkdir()
    shutil.copy(SMUS, some / SMUS.name)
    (some / "Instruments").mkdir()
    for n in names[:-1]:
        (some / "Instruments" / f"{n}.instr").write_bytes(b"FORM")
    before = sorted(p.name for p in (some / "Instruments").iterdir())

    # (not marked at scan — the mark would outlive the instruments arriving)
    assert scanner._extract_one(some / SMUS.name)[1].defect is None
    assert stream._sonix_missing_here(some / SMUS.name) == [names[-1]]
    staged = xpk.stage_for_uade(some / SMUS.name)
    if staged is not None:
        shutil.rmtree(staged.parent, ignore_errors=True)
    assert sorted(p.name for p in (some / "Instruments").iterdir()) == before   # (nothing written)

    (some / "Instruments" / f"{names[-1].upper()}.INSTR").write_bytes(b"FORM")
    assert stream._sonix_missing_here(some / SMUS.name) == []
    assert stream._sonix_missing_here(TD / "tracker/agony end.ahx") == []


@needs_smus
@pytest.mark.asyncio
async def test_playing_it_is_refused_naming_them(tmp_path):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    shutil.copy(SMUS, tmp_path / SMUS.name)
    with pytest.raises(HTTPException) as e:
        async with stream._xpk_unpacked(tmp_path / SMUS.name):
            pass
    assert e.value.status_code == 422 and "17" not in e.value.detail
    assert "Instruments folder" in e.value.detail and "back4" in e.value.detail
    assert not isinstance(e.value, unplayable.RenderRefusal)          # (never marks the track)


@needs_smus
def test_an_archived_sonix_score(tmp_path):
    names = uade_formats.sonix_instrument_names(SMUS.read_bytes())
    for label, present, want in (("some", names[:-1], "partial"), ("none", [], "corrupt")):
        z = tmp_path / f"{label}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("song/space.smus", SMUS.read_bytes())
            for n in present:
                zf.writestr(f"song/Instruments/{n}.instr", b"FORM")
        m = scanner._extract_from_zip(f"{z}::song/space.smus", "t")
        assert m.defect == want, label


# ── SAP tunes that end, Amiga header names, uade label spelling ────────────

_SAP = (b'SAP\r\nNAME "N"\r\nSONGS 3\r\nTYPE B\r\nTIME 01:00.00 LOOP\r\nTIME 00:20.50\r\n'
        b'TIME 00:10.00 LOOP\r\n\xff\xff' + bytes(64))


def test_a_sap_tune_without_loop_has_no_fade():
    assert gme_render.sap_header(_SAP)["loops"] == [True, False, True]
    assert gme_render.render_plan(_SAP, 0, 180) == (68.0, 60.0)
    assert gme_render.render_plan(_SAP, 1, 180) == (20.5, 20.5)      # (ends at its TIME)
    assert gme_render.tune_fades(_SAP, 3) == [8.0, 0.0, 8.0]
    every_loop = _SAP.replace(b"00:20.50\r\n", b"00:20.50 LOOP\r\n")
    assert gme_render.tune_fades(every_loop, 3) is None             # (the format's fixed fade)
    garbled = _SAP.replace(b"TIME 00:20.50", b"TIME garbage")
    assert gme_render.play_seconds(garbled, 1) is None              # (unknown — and tune 3
    assert gme_render.render_plan(garbled, 2, 180) == (18.0, 10.0)  #  keeps its own)


@_have(SAP)
def test_skip_short_tunes_judges_each_sap_tune_by_its_time():
    from soniqboom.core import skip_rules
    d = _x(SAP)
    assert d["tune_fades"] == [8.0, 8.0, 0.0]
    assert d["tune_lengths"] == pytest.approx([95.34, 53.82, 11.48])
    assert [skip_rules.known_len(d, w) for w in (0, 1, 2)] == pytest.approx([87.34, 45.82, 11.48])
    if gme_render.is_available():
        data = SAP.read_bytes()
        wav = gme_render.render_wav(data, 2, *gme_render.render_plan(data, 2, 180))
        assert (len(wav) - 44) / 176_400 == pytest.approx(11.48, abs=0.1)   # (no restart after it)


AON = TD / "uade/Art Of Noise/sunray.aon"


@_have(AON)
def test_art_of_noise_name_author_and_date():
    assert md._amiga_header_tags(AON) == {"title": "sunray", "artist": "twice/lego", "year": 1995}


def test_art_of_noise_without_its_chunks(tmp_path):
    f = tmp_path / "x.aon"
    f.write_bytes(b"AON4" + b"artofnoise by bastian spiegel (twice/lego)" + b"INFO" + bytes(64))
    assert md._amiga_header_tags(f) == {}


@needs_smus
def test_a_sonix_song_name():
    assert md._amiga_header_tags(SMUS) == {"title": "Something in SPACE"}


BD = TD / "uade/Ben Daglish/mickey mouse.bd"


@pytest.mark.skipif(not UADE, reason="uade not installed")
@_have(BD)
def test_the_ben_daglish_player_is_spelt_right():
    assert _x(BD)["format"] == "Ben Daglish"
    bds = SAMPLES / "Ben Daglish SID/Ben Daglish/chubbygristle.bds"
    if bds.is_file():
        assert _x(bds)["format"] == "Ben Daglish SID"


@pytest.mark.asyncio
async def test_stored_tracks_are_relabelled_once():
    from soniqboom.core import repair

    class _Store:
        def __init__(self):
            self._tracks = {"a": {"format": "Benn Daglish"}, "b": {"format": "TFMX"},
                            "d": {"format": "Benn Daglish SID"}}
            self.conf = {}

        def get_config(self, k, default=None):
            return self.conf.get(k, default)

        def set_config(self, k, v):
            self.conf[k] = v

        def update_track_fields_batch(self, items):
            for tid, f in items:
                self._tracks[tid].update(f)
            return len(items)

    s = _Store()
    assert await repair.rename_formats_once(s) == {"renamed": 2}
    assert [t["format"] for t in s._tracks.values()] == ["Ben Daglish", "TFMX", "Ben Daglish SID"]
    s._tracks["c"] = {"format": "Benn Daglish"}
    assert await repair.rename_formats_once(s) == {"skipped": True}     # (once)
