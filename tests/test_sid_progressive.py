# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coverage for the progressive-SID cold-start path and the cache-accounting
fix it depends on.

The feature (soniqboom/api/stream.py) streams sidplayfp's still-rendering WAV
to the browser for ~instant SID playback, promoting the finished render to the
conversion cache.  These tests lock down the pieces that a prior review found
fragile:

  1. ``store_cached`` idempotency — a second store of the same key must not
     double-count ``_total_bytes`` (the progressive finaliser can race a
     blocking render onto the same key).
  2. ``_synth_wav_header`` — the synthesised 44-byte header the stream ships
     before sidplayfp's own header exists must be a valid RIFF/WAVE PCM header
     declaring exactly the expected length.
  3. ``_await_first_sid_output`` — the first-byte probe that decides
     stream-vs-fallback: True once PCM appears, False when the process dies
     without output, True (stream anyway) on timeout with the proc alive.
  4. End-to-end ``_render_sid`` on a real .sid (guarded by sidplayfp) — the
     renderer produces a full-length, non-truncated PCM WAV.
"""
from __future__ import annotations

import asyncio
import struct
import tempfile
import wave
from pathlib import Path

import pytest

_FIXTURE_SID = Path(__file__).resolve().parent.parent / "internal" / "testdata" / "sid" / "SX-64_Demo.sid"


# ── 1) store_cached idempotency ────────────────────────────────────────────

async def test_store_cached_idempotent_total_bytes():
    """Two stores of the SAME key must net the second file's size only once —
    the first store's bytes are subtracted before the second is added, so
    ``_total_bytes`` reflects the single on-disk file, not the sum."""
    import soniqboom.core.conversion_cache as cc

    key = "test:sidprog:idempotent"
    fmt = "sid"

    def _mk(nbytes: int) -> Path:
        p = Path(tempfile.mkstemp(suffix=".wav", prefix="idemtest-")[1])
        p.write_bytes(b"\x00" * nbytes)
        return p

    # Ensure a clean slate for this key.
    try:
        cc._purge_entry(key)
    except Exception:
        pass

    base = cc._total_bytes
    dest = await cc.store_cached(key, fmt, _mk(1000))
    assert cc._total_bytes - base == 1000, "first store should add exactly its size"

    # Re-store a DIFFERENT size on the same key: delta must be (new - old),
    # not (+new).  1000 -> 2500 means net +1500 over the original baseline.
    dest = await cc.store_cached(key, fmt, _mk(2500))
    assert cc._total_bytes - base == 2500, "re-store must replace, not accumulate"

    # Re-store the SAME size again: net zero change.
    before = cc._total_bytes
    dest = await cc.store_cached(key, fmt, _mk(2500))
    assert cc._total_bytes == before, "same-size re-store must add 0 bytes"

    # One file on disk, meta size matches.
    assert Path(dest).exists()
    assert cc._meta[key]["size_bytes"] == 2500

    # Cleanup.
    cc._purge_entry(key)
    Path(dest).unlink(missing_ok=True)


# ── 2) _synth_wav_header ───────────────────────────────────────────────────

def test_synth_wav_header_is_valid_riff():
    from soniqboom.api.stream import _synth_wav_header

    rate, ch, bits, data = 44100, 1, 16, 26_460_000
    h = _synth_wav_header(rate, ch, bits, data)

    assert len(h) == 44
    assert h[0:4] == b"RIFF"
    assert h[4:8] == struct.pack("<I", 36 + data)      # RIFF chunk size
    assert h[8:12] == b"WAVE"
    assert h[12:16] == b"fmt "
    assert struct.unpack("<H", h[20:22])[0] == 1       # PCM
    assert struct.unpack("<H", h[22:24])[0] == ch
    assert struct.unpack("<I", h[24:28])[0] == rate
    assert struct.unpack("<I", h[28:32])[0] == rate * ch * bits // 8   # byte rate
    assert struct.unpack("<H", h[32:34])[0] == ch * bits // 8          # block align
    assert struct.unpack("<H", h[34:36])[0] == bits
    assert h[36:40] == b"data"
    assert struct.unpack("<I", h[40:44])[0] == data    # data chunk size


# ── 2b) _parse_audio_range (A1 — Range/206 handling) ───────────────────────

def test_parse_audio_range():
    from soniqboom.api.stream import _parse_audio_range
    T = 44 + 1000
    assert _parse_audio_range(None, T) == (0, T, False)          # no header → full, 200
    assert _parse_audio_range("bogus", T) == (0, T, False)       # malformed → full
    assert _parse_audio_range("bytes=0-", T) == (0, T, True)     # open-ended → 206 full
    assert _parse_audio_range("bytes=0-1", T) == (0, 2, True)    # Safari probe
    assert _parse_audio_range("bytes=100-199", T) == (100, 200, True)
    assert _parse_audio_range("bytes=44-", T) == (44, T, True)   # seek to data start
    assert _parse_audio_range("bytes=-10", T) == (T - 10, T, True)   # suffix
    assert _parse_audio_range("bytes=0-99999", T) == (0, T, True)    # clamped to total
    assert _parse_audio_range("bytes=99999-", T) is None         # past end → 416
    assert _parse_audio_range("bytes=-0", T) is None             # zero-length suffix → 416


# ── 3) _await_first_sid_output ─────────────────────────────────────────────

class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode


async def test_await_first_output_true_when_pcm_present():
    from soniqboom.api.stream import _await_first_sid_output, _WAV_HEADER_LEN

    tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="fbtest-")[1])
    try:
        tmp.write_bytes(b"\x00" * (_WAV_HEADER_LEN + 100))   # header + real PCM
        assert await _await_first_sid_output(_FakeProc(returncode=None), tmp, 1.0) is True
    finally:
        tmp.unlink(missing_ok=True)


async def test_await_first_output_false_on_dead_proc_no_data():
    from soniqboom.api.stream import _await_first_sid_output, _WAV_HEADER_LEN

    tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="fbtest-")[1])
    try:
        # Header-only (== 44 bytes): not > _WAV_HEADER_LEN, and proc already
        # exited non-zero → treated as an immediate failure → fall back.
        tmp.write_bytes(b"\x00" * _WAV_HEADER_LEN)
        assert await _await_first_sid_output(_FakeProc(returncode=1), tmp, 1.0) is False
    finally:
        tmp.unlink(missing_ok=True)


async def test_await_first_output_true_on_timeout_with_live_proc():
    from soniqboom.api.stream import _await_first_sid_output

    tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="fbtest-")[1])
    try:
        # Empty file, proc still alive, tiny timeout → stream anyway (the
        # generator's own idle-timeout handles a genuinely stuck render).
        assert await _await_first_sid_output(_FakeProc(returncode=None), tmp, 0.2) is True
    finally:
        tmp.unlink(missing_ok=True)


# ── 4) End-to-end: _render_sid produces a full-length, valid WAV ───────────

@pytest.mark.requires_sidplayfp
async def test_render_sid_produces_full_length_wav(have_sidplayfp: bool):
    """The blocking renderer (also the source of the cached WAV the progressive
    path promotes) must produce a valid PCM WAV of the requested duration — a
    truncated render would poison the cache and make warm plays short."""
    if not have_sidplayfp:
        pytest.skip("sidplayfp not installed")
    if not _FIXTURE_SID.exists():
        pytest.skip(f"SID fixture missing: {_FIXTURE_SID}")

    from soniqboom.api.stream import _render_sid

    dur = 10
    out = await _render_sid(_FIXTURE_SID, subsong=0, duration=dur)
    try:
        assert out.exists() and out.stat().st_size > 44, "no audio rendered"
        w = wave.open(str(out), "rb")
        try:
            assert w.getframerate() == 44100
            assert w.getsampwidth() == 2
            rendered = w.getnframes() / w.getframerate()
        finally:
            w.close()
        # Full length, not truncated — sidplayfp renders the whole -t window
        # (allow a small tolerance for rounding).
        assert rendered >= dur - 1.0, f"render truncated: {rendered:.2f}s < {dur}s"
    finally:
        out.unlink(missing_ok=True)
