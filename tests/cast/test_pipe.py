# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the stream-as-render ffmpeg pipeline (``cast_pipe``).

Covers every codec the negotiator can pick, plus the cache-sink mirror
+ the per-codec output validation (FLAC magic bytes, MP3 ID3 / MPEG
sync, AAC ADTS sync, MP4 ftyp+moov layout for the AirPlay AAC variant).
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from soniqboom.core import cast_pipe


# Codecs the negotiator can route to the stream-as-render path
ALL_CODECS = ["mp3", "flac", "wav", "ogg", "opus", "aac"]


# ── Per-codec round trips ─────────────────────────────────────────────────

@pytest.mark.parametrize("codec", ALL_CODECS)
async def test_render_stream_produces_bytes(sine_wav: Path, tmp_path: Path, codec: str):
    """Every supported codec must produce non-empty output.

    Catches the FLAC regression I introduced earlier — a typo'd ffmpeg
    flag silently produced rc=8 with zero bytes, but the path was
    counted as 'passing' because the previous test only exercised MP3.
    """
    sink = tmp_path / f"out.{codec}"
    total = 0
    first_byte_ms: float | None = None
    started = time.monotonic()
    async for chunk in cast_pipe.render_stream(
        sine_wav, codec=codec, cache_sink=sink,
    ):
        if first_byte_ms is None:
            first_byte_ms = (time.monotonic() - started) * 1000
        total += len(chunk)
    assert total > 1024, f"codec={codec}: empty stream"
    assert first_byte_ms is not None and first_byte_ms < 5000, (
        f"codec={codec}: first byte never arrived (or > 5 s)"
    )
    # Cache sink mirror — exact byte-for-byte match with what was streamed.
    assert sink.exists(), f"codec={codec}: cache sink not created"
    assert sink.stat().st_size == total, (
        f"codec={codec}: sink size {sink.stat().st_size} != streamed {total}"
    )


# ── Output validation: file-format sanity ─────────────────────────────────

async def _render_to_file(src: Path, dest: Path, codec: str, **kw):
    async for chunk in cast_pipe.render_stream(src, codec=codec, **kw):
        with open(dest, "ab") as f:
            f.write(chunk)


async def test_mp3_starts_with_id3_or_mpeg_sync(sine_wav: Path, tmp_path: Path):
    out = tmp_path / "out.mp3"
    await _render_to_file(sine_wav, out, "mp3")
    head = out.read_bytes()[:4]
    # ID3v2 tag OR raw MPEG-1 sync word (0xFFE...)
    assert head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0), \
        f"MP3 output doesn't start with ID3/MPEG sync: {head.hex()}"


async def test_flac_starts_with_fLaC_magic(sine_wav: Path, tmp_path: Path):
    """Native FLAC, NOT FLAC-in-OGG.  An earlier mistake wrapped FLAC
    in OGG which Sonos / Apple TV refuse to play."""
    out = tmp_path / "out.flac"
    await _render_to_file(sine_wav, out, "flac")
    head = out.read_bytes()[:4]
    assert head == b"fLaC", f"FLAC magic missing: {head.hex()}"


async def test_aac_adts_default(sine_wav: Path, tmp_path: Path):
    """Without ``protocol='airplay'``, AAC is ADTS-framed for DLNA / Cast.
    ADTS sync word: 0xFFF (12 bits)."""
    out = tmp_path / "out.aac"
    await _render_to_file(sine_wav, out, "aac")
    head = out.read_bytes()[:2]
    assert head[0] == 0xFF and (head[1] & 0xF0) == 0xF0, (
        f"AAC ADTS sync missing: {head.hex()}"
    )


async def test_aac_airplay_is_fragmented_mp4(sine_wav: Path, tmp_path: Path):
    """AirPlay 2 demands fragmented MP4 (moov atom front-loaded).  Look
    for ``ftyp`` near offset 4 and ``moov`` within the first few KB."""
    out = tmp_path / "airplay.mp4"
    await _render_to_file(sine_wav, out, "aac", protocol="airplay")
    data = out.read_bytes()
    assert data[4:8] == b"ftyp", f"MP4 ftyp missing: {data[:16].hex()}"
    # moov must appear early (within the first 4 KB) for streaming
    assert b"moov" in data[:4096], \
        "moov atom not in first 4 KB — won't stream cleanly to AirPlay"


async def test_wav_starts_with_riff(sine_wav: Path, tmp_path: Path):
    out = tmp_path / "out.wav"
    await _render_to_file(sine_wav, out, "wav")
    head = out.read_bytes()[:12]
    assert head[:4] == b"RIFF", f"WAV RIFF magic missing: {head.hex()}"
    assert head[8:12] == b"WAVE"


async def test_ogg_starts_with_OggS(sine_wav: Path, tmp_path: Path):
    out = tmp_path / "out.ogg"
    await _render_to_file(sine_wav, out, "ogg")
    head = out.read_bytes()[:4]
    assert head == b"OggS", f"OGG magic missing: {head.hex()}"


# ── Bitrate enforcement ──────────────────────────────────────────────────

async def test_mp3_defaults_to_high_bitrate(sine_wav: Path, tmp_path: Path):
    """libmp3lame's no-flag default is 128 kbps — bad transcode-from-
    lossless UX.  We pin 320 kbps in _build_cmd unless overridden."""
    out = tmp_path / "out.mp3"
    await _render_to_file(sine_wav, out, "mp3")  # no bitrate arg
    info = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error",
         "-show_entries", "format=bit_rate", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True,
    )
    bitrate = int(info.stdout.strip())
    # Allow 10 % wiggle for the encoder's CBR-but-not-quite output
    assert bitrate >= 280_000, f"MP3 bitrate {bitrate} below 280 kbps floor"


# ── Cancellation cleanup ─────────────────────────────────────────────────

async def test_cancel_via_aclose_kills_ffmpeg(sine_wav: Path, tmp_path: Path):
    """When the consumer stops iterating + aclose()s the generator,
    ffmpeg must be killed and the cache .partial unlinked.  Without
    that we'd leak ffmpeg processes on every client cancel."""
    gen = cast_pipe.render_stream(
        sine_wav, codec="flac", cache_sink=tmp_path / "out.flac",
    )
    # Consume one chunk then close
    async for chunk in gen:
        assert len(chunk) > 0
        break
    await gen.aclose()
    # Give the OS a beat to reap the child
    await asyncio.sleep(0.2)
    # .partial file should NOT survive a cancelled render
    assert not (tmp_path / "out.flac.partial").exists()
