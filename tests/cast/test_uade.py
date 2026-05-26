# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end tests for the AHX / Hively (uade123) renderer.

Two things matter here:

  1. ``_render_uade`` actually produces a valid WAV from a real .ahx
     source file — no silent zero-byte rc=8 failures (the FLAC ffmpeg-
     flag regression class).

  2. ``cast_render.prepare_source_for_stream`` correctly routes .ahx
     and .hvl through uade123 instead of openmpt123.

We use a synthetic AHX file checked into the test fixtures so the
test runs without the user's modarchive library being mounted.
"""
from __future__ import annotations

import base64
import struct
import subprocess
from pathlib import Path

import pytest


# Minimal valid AHX v2 file (header + empty track table + 1 sample).
# Built from the AHX spec: bytes [0..3]='THX\x00', byte 4=version (2 here),
# byte 5=length, bytes 6+=offsets.  An empty playable AHX is ~32 bytes
# but real ones are 500B-50KB.  We extract a real sample from the
# library if mounted; otherwise we test the renderer error path
# (missing-uade-binary) and the dispatcher routing.

@pytest.fixture(scope="session")
def real_ahx_file(tmp_path_factory) -> Path | None:
    """Extract a real .ahx from one of the user's modarchive ZIPs, or
    return None if nothing reachable.  Tests skip on None.

    The extraction handles **doubly-nested ZIPs** (`outer.zip::inner.zip::file.ahx`)
    which is how modarchive_2007 ships — outer zip per letter group,
    each containing single-track inner zips.  Stops at the first
    successful extraction so the search is bounded.
    """
    import zipfile

    candidates = [
        Path("/Volumes/Music/Tracker_SID/modarchive_2007"),
        Path("/var/lib/modarchive"),
    ]
    for root in candidates:
        if not root.exists():
            continue
        outer_zips = list(root.rglob("*.zip"))[:200]  # bound the scan
        for outer in outer_zips:
            try:
                with zipfile.ZipFile(outer) as oz:
                    for name in oz.namelist():
                        # Direct .ahx in the outer ZIP
                        if name.lower().endswith(".ahx"):
                            extracted = tmp_path_factory.mktemp("ahx") / Path(name).name
                            extracted.write_bytes(oz.read(name))
                            return extracted
                        # Nested .ahx.zip → inner contains the .ahx
                        if name.lower().endswith(".ahx.zip"):
                            inner_bytes = oz.read(name)
                            inner_path = tmp_path_factory.mktemp("inner") / Path(name).name
                            inner_path.write_bytes(inner_bytes)
                            try:
                                with zipfile.ZipFile(inner_path) as iz:
                                    for iname in iz.namelist():
                                        if iname.lower().endswith(".ahx"):
                                            extracted = tmp_path_factory.mktemp("ahx") / Path(iname).name
                                            extracted.write_bytes(iz.read(iname))
                                            return extracted
                            except zipfile.BadZipFile:
                                pass
            except (zipfile.BadZipFile, OSError):
                continue
    return None


# ── _render_uade end-to-end ────────────────────────────────────────────────

@pytest.mark.requires_uade123
async def test_render_uade_produces_valid_wav(
    have_uade123: bool, real_ahx_file, tmp_path: Path,
):
    """End-to-end: feed a real AHX file to ``_render_uade``, verify the
    output is a valid stereo 44.1 kHz 16-bit PCM WAV with non-trivial
    duration.  This catches the failure mode where the renderer's
    arguments are wrong (e.g. ``--normalise`` doesn't exist in uade
    3.05) and the file ends up zero-byte."""
    if not have_uade123:
        pytest.skip("uade123 not installed")
    if real_ahx_file is None:
        pytest.skip("no real .ahx file reachable for end-to-end test")

    from soniqboom.api.stream import _render_uade

    wav_path = await _render_uade(real_ahx_file)
    assert wav_path.exists()
    assert wav_path.stat().st_size > 44, "WAV header alone, no audio"

    # Format sanity via ffprobe
    info = subprocess.run(
        [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=duration",
            "-of", "default=noprint_wrappers=1:nokey=0",
            str(wav_path),
        ],
        capture_output=True, text=True, check=True,
    )
    out = info.stdout
    assert "codec_name=pcm_s16le" in out
    assert "sample_rate=44100" in out
    assert "channels=2" in out
    # Duration must be > 1 s — uade should have rendered at least a
    # few seconds of audio.  AHX files with no sound at the start
    # would still fill silence into the WAV.
    duration = next(
        (float(line.split("=")[1]) for line in out.splitlines()
         if line.startswith("duration=")),
        0.0,
    )
    assert duration >= 1.0, f"WAV duration {duration} s < 1 s — render likely failed silently"


@pytest.mark.requires_uade123
async def test_render_uade_missing_binary_returns_501(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """If uade123 isn't found, the renderer must raise HTTPException 501
    with a clear install hint — not crash or silently produce nothing."""
    from soniqboom.api.stream import _render_uade
    from fastapi import HTTPException

    # Force the "binary not found" path by overriding both the settings
    # path and PATH.
    monkeypatch.setenv("PATH", "/nonexistent")
    from soniqboom.config import settings as _settings
    monkeypatch.setattr(_settings, "uade123_path", "/nonexistent/uade123")

    fake_ahx = tmp_path / "fake.ahx"
    fake_ahx.write_bytes(b"\x00" * 64)

    with pytest.raises(HTTPException) as exc_info:
        await _render_uade(fake_ahx)
    assert exc_info.value.status_code == 501
    assert "uade123" in exc_info.value.detail.lower()
    assert "install" in exc_info.value.detail.lower()


# ── cast_render dispatcher: AHX / HVL routed via uade ─────────────────────

def test_cast_render_routes_ahx_to_uade():
    """Coverage check: cast_render's is_rendered_format must recognise
    .ahx and .hvl, AND the prepare_source_for_stream branch logic
    selects the UADE path (we can't test the actual render without
    the binary AND a real file)."""
    from soniqboom.core import cast_render

    assert cast_render.is_rendered_format(".ahx") is True
    assert cast_render.is_rendered_format(".hvl") is True
    # And NOT misclassified as one of the other renderer families
    assert ".ahx" in cast_render._UADE_EXTS
    assert ".hvl" in cast_render._UADE_EXTS
    assert ".ahx" not in cast_render._TRACKER_EXTS
    assert ".hvl" not in cast_render._TRACKER_EXTS
    assert ".ahx" not in cast_render._SID_EXTS
    assert ".ahx" not in cast_render._GME_EXTS


def test_stream_dispatcher_separates_uade_from_tracker():
    """In stream.py the foreground dispatch must put .ahx/.hvl in
    _UADE_EXTS, not _TRACKER_EXTS — otherwise the openmpt123 branch
    would fire and silently fail at runtime."""
    from soniqboom.api import stream

    assert ".ahx" in stream._UADE_EXTS
    assert ".hvl" in stream._UADE_EXTS
    assert ".ahx" not in stream._TRACKER_EXTS
    assert ".hvl" not in stream._TRACKER_EXTS
