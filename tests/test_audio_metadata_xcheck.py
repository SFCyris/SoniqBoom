# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ordinary audio files' own data, as the scan stores it (a cross-check
against ffprobe / the files' chunks found these): raw ADTS ``.aac``; WAV —
compressed (ADPCM) lengths and lossiness, a cut file, ``LIST/INFO`` and
``id3 `` tags, bit depth; AIFF text chunks; APEv2 tags (WavPack); DSD's rate
and label; a comment stored as DESCRIPTION; separate track / disc totals;
Opus R128 gains; ALAC's real bitrate; no bit depth for lossy AAC; WMA's
other fields; IFF samples lossless."""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "internal/format-samples/mainstream"
NP = ROOT / "internal/testdata/nostalgicplayer"
DSD = ROOT / "internal/testdata/dsd/dsd_stream_file.dsf"
FFMPEG = shutil.which("ffmpeg")


def _x(p: Path) -> dict:
    return md.extract(p, "x").model_dump()


def _ffmpeg(*args) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True, timeout=60)


needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


@pytest.mark.skipif(not (MAIN / "sample.aac").is_file(), reason="no sample")
def test_a_raw_adts_aac_is_read():
    d = _x(MAIN / "sample.aac")
    assert d["format"] == "AAC" and d["duration"] == pytest.approx(12.0, abs=0.2)
    assert (d["channels"], d["sample_rate"]) == (2, 96000) and d["bitrate"] > 100_000


@pytest.mark.skipif(not (NP / "riffwave_adpcm/Cleanup.wav").is_file(), reason="no sample")
def test_an_adpcm_wav_has_its_real_length_and_is_lossy():
    d = _x(NP / "riffwave_adpcm/Cleanup.wav")
    assert d["duration"] == pytest.approx(8.47, abs=0.02)       # (fact: 186,762 / 22,050)
    assert d["is_lossless"] is False and d["bit_depth"] is None
    assert d["year"] == 1997                                     # (its INFO ICRD)


@pytest.mark.skipif(not (NP / "riffwave_pcm/Bad Ole.wav").is_file(), reason="no sample")
def test_a_cut_wav_is_as_long_as_what_it_holds():
    d = _x(NP / "riffwave_pcm/Bad Ole.wav")
    assert d["duration"] == pytest.approx(2.69, abs=0.02)       # (header: 107 s)
    assert d["is_lossless"] is True and d["bit_depth"] == 8


@pytest.mark.skipif(not (MAIN / "sample-24.wav").is_file(), reason="no sample")
def test_wav_info_tags_and_bit_depth():
    d = _x(MAIN / "sample-24.wav")
    assert d["title"] == "Variatio 4 a 1 Clav." and d["artist"] == "Kimiko Ishizaka"
    assert d["album"].startswith("J.S. Bach") and d["track_number"] == 5
    assert d["year"] == 2012 and d["isrc"] == "QMNYZ1200005" and d["comment"]
    assert d["bit_depth"] == 24 and d["duration"] == pytest.approx(12.0, abs=0.01)


@needs_ffmpeg
def test_a_wav_id3_chunk_wins_over_info(tmp_path):
    f = tmp_path / "t.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=d=1", "-metadata", "title=Info Title", str(f))
    from mutagen.id3 import TIT2, TPE1
    from mutagen.wave import WAVE
    w = WAVE(f)
    w.add_tags()
    w.tags.add(TIT2(encoding=3, text="ID3 Title"))
    w.tags.add(TPE1(encoding=3, text="ID3 Artist"))
    w.save()
    d = _x(f)
    assert (d["title"], d["artist"]) == ("ID3 Title", "ID3 Artist")
    assert d["duration"] == pytest.approx(1.0, abs=0.01) and d["bit_depth"] == 16


def test_junk_named_wav_is_no_track(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(b"RIFF" + struct.pack("<I", 4) + b"WAVE")
    d = _x(f)                                     # (the permissive fallback: a stub)
    assert d["duration"] == 0.0


@pytest.mark.skipif(not (MAIN / "sample.aif").is_file(), reason="no sample")
def test_aiff_text_chunks():
    d = _x(MAIN / "sample.aif")
    assert d["title"] == "Variatio 4 a 1 Clav." and "kimiko" in d["comment"].lower()


@pytest.mark.skipif(not (MAIN / "sample.wv").is_file(), reason="no sample")
def test_wavpack_apev2_tags():
    d = _x(MAIN / "sample.wv")
    assert (d["album_artist"], d["track_number"], d["isrc"]) == (
        "Kimiko Ishizaka", 5, "QMNYZ1200005") or (d["track_number"], d["isrc"]) == (
        5, "QMNYZ1200005")
    assert d["comment"] and d["bit_depth"] and d["bitrate"]


@pytest.mark.skipif(not DSD.is_file(), reason="no sample")
def test_dsd_rate_and_label():
    d = _x(DSD)
    assert (d["sample_rate"], d["format"]) == (2822400, "DSD64")
    assert d["bitrate"] == 2822400 * 2 and d["duration"] == pytest.approx(26.05, abs=0.05)


@pytest.mark.skipif(not (MAIN / "sample.flac").is_file(), reason="no sample")
def test_a_comment_stored_as_description():
    for name in ("sample.flac", "sample.opus"):
        assert _x(MAIN / name)["comment"].startswith("Visit http"), name


@needs_ffmpeg
def test_vorbis_separate_totals_and_opus_r128(tmp_path):
    f = tmp_path / "t.flac"
    _ffmpeg("-f", "lavfi", "-i", "sine=d=1", "-metadata", "track=3", "-metadata",
            "TRACKTOTAL=12", "-metadata", "disc=1", "-metadata", "DISCTOTAL=2", str(f))
    d = _x(f)
    assert (d["track_number"], d["total_tracks"], d["disc_number"], d["total_discs"]) == (
        3, 12, 1, 2)
    o = tmp_path / "t.opus"
    _ffmpeg("-f", "lavfi", "-i", "sine=d=1", "-c:a", "libopus", str(o))
    from mutagen.oggopus import OggOpus
    a = OggOpus(o)
    a["R128_TRACK_GAIN"] = str(-11 * 256 - 128)                # −11.5 dB to −23 LUFS
    a["R128_ALBUM_GAIN"] = str(-12 * 256)
    a.save()
    d = _x(o)
    assert d["replaygain_track_gain"] == pytest.approx(-6.5)    # (+5 dB: ReplayGain's −18)
    assert d["replaygain_album_gain"] == pytest.approx(-7.0)


@pytest.mark.skipif(not (MAIN / "sample-alac.m4a").is_file(), reason="no sample")
def test_alac_real_bitrate_and_no_bit_depth_for_aac():
    a = _x(MAIN / "sample-alac.m4a")
    size = (MAIN / "sample-alac.m4a").stat().st_size
    assert a["bitrate"] == pytest.approx(size * 8 / a["duration"], rel=0.01)
    assert a["bit_depth"] == 24 and a["total_tracks"] is None   # (0: not stated)
    assert _x(MAIN / "sample-aac.m4a")["bit_depth"] is None


@needs_ffmpeg
def test_wma_other_fields(tmp_path):
    f = tmp_path / "t.wma"
    _ffmpeg("-f", "lavfi", "-i", "sine=d=1", "-c:a", "wmav2", str(f))
    from mutagen.asf import ASF
    a = ASF(f)
    a["Description"] = "a comment"
    a["WM/Composer"] = "Composer C"
    a["WM/PartOfSet"] = "1/2"
    a["WM/ISRC"] = "USABC9900001"
    a["replaygain_track_gain"] = "-6.50 dB"
    a.save()
    d = _x(f)
    assert (d["comment"], d["composer"], d["disc_number"], d["total_discs"], d["isrc"]) == (
        "a comment", "Composer C", 1, 2, "USABC9900001")
    assert d["replaygain_track_gain"] == pytest.approx(-6.5)


@pytest.mark.skipif(not (NP / "iff8svx_pcm").is_dir(), reason="no sample")
def test_iff_samples_lossless_unless_packed():
    from soniqboom.core import retro
    pcm = next((NP / "iff8svx_pcm").iterdir())
    d = _x(pcm)
    assert d["is_lossless"] is True and d["bit_depth"] == 8
    assert retro.coarse_family("IFF 8SVX") == "lossless"
    fib = NP / "iff8svx_fibonacci"
    if fib.is_dir():
        assert _x(next(fib.iterdir()))["is_lossless"] is False
