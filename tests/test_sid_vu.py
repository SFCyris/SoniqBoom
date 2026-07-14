# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-voice SID VU builder (sid_vu.build_vu).

The retro 3-voice SID meter is built from 3 sidplayfp voice-isolation renders.
These tests use synthetic mono WAVs (no sidplayfp) to lock down the envelope
extraction + cross-voice normalisation + VUMR shape."""
from __future__ import annotations

import array
import math
import wave
from pathlib import Path

from soniqboom.core.sid_vu import build_vu


def _write_tone(path: Path, amplitude: int, seconds: float = 2.0, rate: int = 44100):
    """Write a mono 16-bit sine at the given peak amplitude (0 = silence)."""
    n = int(seconds * rate)
    samples = array.array("h", (0 for _ in range(n)))
    if amplitude:
        for i in range(n):
            samples[i] = int(amplitude * math.sin(2 * math.pi * 440 * i / rate))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def test_build_vu_three_voices_normalised(tmp_path: Path):
    loud = tmp_path / "v1.wav"      # full scale
    quiet = tmp_path / "v2.wav"     # ~half scale
    silent = tmp_path / "v3.wav"    # silence
    _write_tone(loud, 32000)
    _write_tone(quiet, 16000)
    _write_tone(silent, 0)

    r = build_vu([loud, quiet, silent], 2.0)
    assert r is not None
    assert r.channels == 3
    assert r.sample_rate == 30
    assert r.frames == 60                       # 2 s × 30 Hz
    assert len(r.pan) == 3 and set(r.pan) == {0}   # SID mono → all centre
    assert len(r.mono) == r.frames * r.channels

    # Per-voice means: loud > quiet > silent, and the loudest voice hits the
    # top of the normalised range (song-wide peak → 255).
    def col_mean(v):
        return sum(r.mono[i * 3 + v] for i in range(r.frames)) / r.frames
    m1, m2, m3 = col_mean(0), col_mean(1), col_mean(2)
    assert m1 > m2 > m3
    assert m3 < 2.0                             # silent voice ≈ floor
    assert max(r.mono[i * 3 + 0] for i in range(r.frames)) >= 250  # loud → full range


def test_build_vu_all_silent_returns_none(tmp_path: Path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _write_tone(a, 0)
    _write_tone(b, 0)
    assert build_vu([a, b], 2.0) is None        # nothing to meter → FFT fallback


def test_build_vu_rejects_bad_inputs(tmp_path: Path):
    assert build_vu([], 2.0) is None
    assert build_vu([tmp_path / "missing.wav"], 2.0) is None   # unreadable → None
    good = tmp_path / "g.wav"; _write_tone(good, 20000)
    assert build_vu([good], 0.0) is None        # zero duration → None
