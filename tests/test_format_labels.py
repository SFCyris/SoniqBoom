# SPDX-License-Identifier: AGPL-3.0-or-later
"""Format labels by codec: Dolby Digital (AC-3) and Dolby Digital Plus
(E-AC-3) in an MP4 / M4A file aren't "AAC"; an Opus stream in an ``.ogg``
file isn't "Ogg Vorbis".  A track an older scan labelled so gets the right
label when it plays (``stream._correct_format_label``)."""
from __future__ import annotations

import shutil
import subprocess
import types

import pytest

from soniqboom.core import metadata, search_values

FFMPEG = shutil.which("ffmpeg")


def _encode(tmp_path, name: str, *args: str, channels: int = 2):
    if not FFMPEG:
        pytest.skip("ffmpeg missing")
    out = tmp_path / name
    layout = "5.1" if channels == 6 else "stereo"
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=f=220:d=2", "-af", f"aformat=channel_layouts={layout}",
                        *args, str(out)], capture_output=True)
    if r.returncode != 0 or not out.exists():
        pytest.skip(f"this ffmpeg can't write {name}: {r.stderr.decode(errors='replace')[-120:]}")
    return out


@pytest.mark.parametrize("name, args, want", [
    ("dd.m4a", ("-c:a", "ac3", "-f", "mp4"), "AC-3"),
    ("ddp.m4a", ("-c:a", "eac3", "-b:a", "640k", "-f", "mp4"), "E-AC-3"),
    ("aac.m4a", ("-c:a", "aac",), "AAC"),
])
def test_an_mp4_file_is_labelled_by_its_codec(tmp_path, name, args, want):
    f = _encode(tmp_path, name, *args, channels=6)
    d = metadata.extract(f, "t").model_dump()
    assert d["format"] == want
    assert d["channels"] == 6 and d["is_lossless"] is False


@pytest.mark.parametrize("channels", [2, 6])
def test_an_ogg_file_holding_opus_is_opus(tmp_path, channels):
    f = _encode(tmp_path, f"o{channels}.ogg", "-c:a", "libopus", "-mapping_family",
                "1" if channels > 2 else "0", channels=channels)
    d = metadata.extract(f, "t").model_dump()
    assert (d["format"], d["channels"], d["sample_rate"]) == ("Opus", channels, 48000)
    assert d["duration"] > 1.5


def test_an_ogg_vorbis_file_stays_ogg_vorbis(tmp_path):
    f = _encode(tmp_path, "v.ogg", "-c:a", "vorbis", "-strict", "-2")
    assert metadata.extract(f, "t").model_dump()["format"] == "Ogg Vorbis"
    assert not metadata._ogg_is_opus(f)


def test_format_searches_find_the_new_labels():
    assert {"ac-3", "e-ac-3", "aac", "alac"} <= search_values.format_names_for_ext("m4a")
    assert {"ogg vorbis", "opus"} <= search_values.format_names_for_ext("ogg")


class _Store:
    def __init__(self):
        self.updates = []

    def update_track_fields(self, tid, fields):
        self.updates.append((tid, fields))
        return True


@pytest.mark.asyncio
async def test_a_play_corrects_an_older_scans_label(tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core import store as store_mod
    stream._LABEL_CHECKED.clear()
    fs = _Store()
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    m4a = tmp_path / "x.m4a"
    m4a.write_bytes(b"")
    await stream._correct_format_label("a", types.SimpleNamespace(format="AAC"), m4a, ".m4a", "eac3")
    await stream._correct_format_label("b", types.SimpleNamespace(format="AAC"), m4a, ".m4a", "ac3")
    await stream._correct_format_label("c", types.SimpleNamespace(format="AAC"), m4a, ".m4a", "aac")
    await stream._correct_format_label("d", types.SimpleNamespace(format="E-AC-3"), m4a, ".m4a", "eac3")
    opus = _encode(tmp_path, "o.ogg", "-c:a", "libopus")
    vorbis = _encode(tmp_path, "v.ogg", "-c:a", "vorbis", "-strict", "-2")
    await stream._correct_format_label("e", types.SimpleNamespace(format="Ogg Vorbis"), opus, ".ogg", None)
    await stream._correct_format_label("f", types.SimpleNamespace(format="Ogg Vorbis"), vorbis, ".ogg", None)
    assert [u[0] for u in fs.updates] == ["a", "b", "e"]
    assert fs.updates[0][1] == {"format": "E-AC-3", "is_lossless": False}
    assert fs.updates[1][1] == {"format": "AC-3", "is_lossless": False}
    opus_fix = fs.updates[2][1]                  # (an older scan's stub: read again, filled in)
    assert opus_fix["format"] == "Opus" and opus_fix["is_lossless"] is False
    assert opus_fix["channels"] == 2 and opus_fix["sample_rate"] == 48000
    assert abs(opus_fix["duration"] - 2.0) < 0.1
    # Each track is checked once per run.
    await stream._correct_format_label("a", types.SimpleNamespace(format="AAC"), m4a, ".m4a", "eac3")
    assert len(fs.updates) == 3


# ── raw Dolby Digital (Plus), DTS and Dolby TrueHD / MLP streams ─────────────

@pytest.mark.parametrize("name, args, want, ch, lossless", [
    ("a.ac3", ("-c:a", "ac3"), "AC-3", 6, False),
    ("b.eac3", ("-c:a", "eac3", "-b:a", "640k"), "E-AC-3", 6, False),
    ("c.ec3", ("-c:a", "eac3", "-b:a", "640k", "-f", "eac3"), "E-AC-3", 6, False),
    ("d.dts", ("-c:a", "dca", "-strict", "-2"), "DTS", 6, False),
    ("e.thd", ("-ar", "48000", "-c:a", "truehd", "-strict", "-2"), "TrueHD", 6, True),
    ("f.truehd", ("-ar", "48000", "-c:a", "truehd", "-strict", "-2", "-f", "truehd"), "TrueHD", 6, True),
    ("g.mlp", ("-ar", "48000", "-c:a", "mlp", "-strict", "-2"), "MLP", 2, True),
])
def test_a_raw_surround_stream_is_read(tmp_path, name, args, want, ch, lossless):
    f = _encode(tmp_path, name, *args, channels=ch)
    d = metadata.extract(f, "t").model_dump()
    assert (d["format"], d["channels"], d["is_lossless"]) == (want, ch, lossless)
    assert abs(d["duration"] - 2.0) < 0.1                  # (TrueHD / MLP: by walking the stream)
    assert d["title"] == f.stem


def test_dts_streams_are_named_by_their_profile():
    assert metadata._dts_label("DTS") == "DTS"
    assert metadata._dts_label("DTS-ES") == "DTS"
    assert metadata._dts_label("DTS-HD HRA") == "DTS-HD HRA"
    assert metadata._dts_label("DTS-HD MA") == "DTS-HD MA"
    assert metadata._dts_label("DTS-HD MA + DTS:X") == "DTS-HD MA"
    assert metadata._is_lossless_format("DTS-HD MA") and not metadata._is_lossless_format("DTS-HD HRA")


def test_the_new_extensions_are_audio_never_amiga_modules():
    from soniqboom.core import skip_rules, uade_formats
    for ext in metadata._SURROUND_STREAM_EXTS:
        assert ext in metadata.SUPPORTED_EXTENSIONS and ext in skip_rules._AUDIO_EXTS
        assert uade_formats.classify(f"x{ext}") is None
        assert metadata.HEADER_BUDGET[ext] is None          # (read whole on a share)


@pytest.mark.asyncio
async def test_a_stream_without_a_stated_length_keeps_its_channels(tmp_path):
    from soniqboom.api import stream
    f = _encode(tmp_path, "e.thd", "-ar", "48000", "-c:a", "truehd", "-strict", "-2", channels=6)
    info = await stream._probe_source_info(f)
    assert info and info.get("channels") == 6 and info.get("sample_rate") == 48000


def test_subsonic_advertises_what_the_stream_sends():
    from soniqboom.api import subsonic as s
    assert s._delivery({"path": "/m/x.m4a", "format": "E-AC-3"})[2:] == ("wav", "audio/wav")
    assert s._delivery({"path": "/m/x.m4a", "format": "AC-3"})[2:] == ("wav", "audio/wav")
    assert s._delivery({"path": "/m/x.m4a", "format": "AAC"})[2:] == (None, None)
    assert s._delivery({"path": "/m/x.ac3", "format": "AC-3"}) == ("ac3", "audio/ac3", "wav", "audio/wav")
    assert s._delivery({"path": "/m/x.thd", "format": "TrueHD"})[1] == "audio/vnd.dolby.mlp"
    assert s._source_codec({"format": "E-AC-3"}, "m4a") == "eac3"
    assert s._source_codec({"format": "DTS-HD MA"}, "dts") == "dts"


def test_dolby_in_mp4_takes_its_channels_from_the_stream(tmp_path):
    """A Dolby sample entry states 2 channels whatever the stream holds (a
    non-ffmpeg muxer writes it so): the count comes from the stream."""
    f = _encode(tmp_path, "ddp.m4a", "-c:a", "eac3", "-b:a", "640k", "-f", "mp4", channels=6)
    data = bytearray(f.read_bytes())
    i = data.find(b"ec-3")
    assert i > 0
    data[i + 20:i + 22] = (2).to_bytes(2, "big")    # (the entry's channelcount field)
    f.write_bytes(bytes(data))
    from mutagen.mp4 import MP4
    assert MP4(f).info.channels == 2
    assert metadata.extract(f, "t").model_dump()["channels"] == 6


def test_the_inflight_wav_must_fit_a_wav_header():
    from soniqboom.api import stream
    T = types.SimpleNamespace
    assert stream._inflight_wav_fits(T(duration=3600, channels=6, sample_rate=48000))
    assert not stream._inflight_wav_fits(T(duration=100 * 60, channels=6, sample_rate=48000))
    assert not stream._inflight_wav_fits(T(duration=40 * 60, channels=8, sample_rate=96000))
    assert stream._inflight_wav_fits(T(duration=0, channels=8, sample_rate=96000))       # (unknown)
    # A DSD file states its 1-bit rate: the WAV is 96 kHz stereo.
    assert stream._inflight_wav_fits(T(duration=3600, channels=2, sample_rate=2822400), 96000, 2)


def test_head_says_what_get_sends_for_dolby_in_mp4():
    from soniqboom.api import stream
    assert stream.delivered_format("/m/x.m4a", "AAC", None) is None
    for f in ("ALAC", "AC-3", "E-AC-3"):
        assert stream.delivered_format("/m/x.m4a", f, None) == ("wav", "audio/wav")


def test_a_long_stream_gets_an_estimate_when_it_cant_be_walked_in_time(tmp_path, monkeypatch):
    f = _encode(tmp_path, "e.thd", "-ar", "48000", "-c:a", "truehd", "-strict", "-2", channels=6)
    monkeypatch.setattr(metadata, "_stream_walk_seconds", lambda p: None)     # (timed out)
    d = metadata.extract(f, "t").model_dump()
    assert abs(d["duration"] - 2.0) < 0.3


def test_lyrics_of_an_opus_ogg_are_read_and_written(tmp_path):
    f = _encode(tmp_path, "o.ogg", "-c:a", "libopus")
    assert metadata.write_lyrics(f, "la la la")
    assert metadata.extract_lyrics(f) == "la la la"


@pytest.mark.asyncio
async def test_a_surround_file_too_long_for_a_wav_is_routed_per_browser(tmp_path):
    """Chromium / Firefox get a FLAC stream with every channel; Safari, which
    can't play a FLAC that is still being encoded, a stereo WAV."""
    from soniqboom.api import stream
    T = types.SimpleNamespace
    long51 = T(id="L", format="AC-3", duration=100 * 60, channels=6, sample_rate=48000, bitrate=448000)
    short51 = T(id="S", format="AC-3", duration=30, channels=6, sample_rate=48000, bitrate=448000)
    chrome = stream._ClientView(T(headers={"user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"}, cookies={}))
    safari = stream._ClientView(T(headers={"user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15"}, cookies={}))
    f = tmp_path / "x.ac3"
    f.write_bytes(b"")
    kw = dict(target_format=None, max_bitrate_kbps=0, target_sample_rate=0, force_transcode=False)
    p = await stream._serve_plan(chrome, long51, f, ".ac3", **kw)
    assert (p.kind, p.eff_codec) == ("transcode", "flac")
    p = await stream._serve_plan(safari, long51, f, ".ac3", **kw)
    assert (p.kind, p.channels_hint) == ("inflight", 2)
    for client in (chrome, safari):
        p = await stream._serve_plan(client, short51, f, ".ac3", **kw)
        assert (p.kind, p.channels_hint) == ("inflight", None)
    dsd = T(id="D", format="DSD64", duration=3600, channels=2, sample_rate=2822400, bitrate=0)
    p = await stream._serve_plan(chrome, dsd, tmp_path / "x.dsf", ".dsf", **kw)
    assert p.kind == "inflight"                         # (its 1-bit rate is not the WAV's)
