# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The voices of a libgme chiptune (core/gme_voices.py): renders on the
reference timeline, voice muting, the per-voice VU sidecar.

The fixtures are real rips under internal/testdata (not redistributable):
the tests skip when they, or libgme, are absent."""
import array
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TD = ROOT / "internal" / "testdata"
NSF = TD / "gme" / "8bp028-b1-nullsleep-axel_f.nsf"
SPC = TD / "game" / "mmgg-02.spc"
GBS = TD / "game" / "hello kitty no happy house.gbs"

from soniqboom.core import gme_render, gme_voices, openmpt_vu   # noqa: E402

pytestmark = pytest.mark.skipif(not gme_voices.is_available(), reason="libgme not available")


def _need(p: Path) -> bytes:
    if not p.is_file():
        pytest.skip(f"fixture {p.name} not present")
    return p.read_bytes()


def _pcm(b: bytes) -> array.array:
    a = array.array("h")
    a.frombytes(b)
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _reference(data: bytes, sub: int = 0, default: float = 40.0):
    total, fade_at = gme_render.render_plan(data, sub, default)
    wav = gme_render.render_wav(data, sub, total, fade_at_s=fade_at)
    assert wav
    return _pcm(wav[44:]), total, fade_at


def _render(data, sub, audible, frames, total, fade_at, lead):
    r = gme_voices.VoiceRender(data, sub, audible, fade_at_ms=gme_voices.fade_ms(total, fade_at),
                               lead_in=lead)
    try:
        return _pcm(r.read(frames))
    finally:
        r.close()


@pytest.mark.parametrize("fixture,lead", [(NSF, 1024), (GBS, 1024), (SPC, 0)])
def test_all_voices_on_is_the_reference_render(fixture, lead):
    """On the reference timeline (lead-in skipped, the reference's call grid,
    the fade moved) every voice heard is ``render_pcm``'s render, sample for
    sample — the fade included.  A short default length keeps the fade in."""
    data = _need(fixture)
    ref, total, fade_at = _reference(data, default=40.0 if fixture != SPC else 180.0)
    got_lead = gme_voices.lead_in_frames(data, 0)
    assert got_lead == lead
    allv = _render(data, 0, 0xFFFFFFFF, len(ref) // 2, total, fade_at, got_lead)
    assert allv == ref


@pytest.mark.parametrize("fixture,tol", [(NSF, 2), (GBS, 2), (SPC, 32)])
def test_single_voices_add_up_to_the_reference(fixture, tol):
    """libgme mixes its voices linearly: the renders of each voice alone sum
    to the reference (to rounding: the SPC DSP rounds each voice)."""
    data = _need(fixture)
    ref, total, fade_at = _reference(data)
    lead = gme_voices.lead_in_frames(data, 0)
    n = len(gme_voices.voice_names(data))
    frames = min(len(ref) // 2, 44100 * 20)
    acc = [0] * (frames * 2)
    for v in range(n):
        solo = _render(data, 0, 1 << v, frames, total, fade_at, lead)
        for i, x in enumerate(solo):
            acc[i] += x
    worst = max(abs(a - b) for a, b in zip(acc, ref[:frames * 2]))
    assert worst <= tol


def test_a_muted_voice_is_left_out_and_nothing_else_changes():
    """Muting voice v gives the reference less voice v's own render."""
    data = _need(NSF)
    ref, total, fade_at = _reference(data)
    lead = gme_voices.lead_in_frames(data, 0)
    frames = 44100 * 12
    n = len(gme_voices.voice_names(data))
    muted = _render(data, 0, ((1 << n) - 1) & ~1, frames, total, fade_at, lead)
    solo = _render(data, 0, 1, frames, total, fade_at, lead)
    assert any(solo), "Square 1 plays in the first 12 s"
    worst = max(abs(r - m - s) for r, m, s in zip(ref[:frames * 2], muted, solo))
    assert worst <= 2
    assert muted != ref[:frames * 2]


def test_reads_of_any_size_line_up():
    """``read`` buffers the reference's 4096-frame calls: odd read sizes give
    the same samples as one read."""
    data = _need(SPC)
    _, total, fade_at = _reference(data)
    kw = dict(fade_at_ms=gme_voices.fade_ms(total, fade_at), lead_in=0)
    a = gme_voices.VoiceRender(data, 0, 0b101, **kw)
    b = gme_voices.VoiceRender(data, 0, 0b101, **kw)
    try:
        whole = a.read(30000)
        parts = b.read(1) + b.read(4095) + b.read(7) + b.read(25897)
        assert whole == parts
        assert b.pos == 30000
    finally:
        a.close()
        b.close()


def test_vu_sidecar_per_voice_with_names():
    data = _need(NSF)
    ref, total, fade_at = _reference(data, default=60.0)
    vu = gme_voices.build_vu(data, 0, ref_frames=len(ref) // 2, duration_s=total, fade_at_s=fade_at)
    assert vu is not None
    assert list(vu.names) == ["Square 1", "Square 2", "Triangle", "Noise", "DMC"]
    assert vu.channels == 5 and vu.sample_rate == 30
    assert vu.frames == -(-(len(ref) // 2) // 1470)
    cols = [[vu.mono[f * 5 + v] for f in range(vu.frames)] for v in range(5)]
    # every voice of this tune plays at some point, and the voices differ
    assert all(max(c) > 0 for c in cols)
    assert max(max(c) for c in cols) == 255
    assert len({tuple(c) for c in cols}) == 5
    # the levels follow the music: Square 1 is silent at 24–29 s, plays at 34 s
    assert max(cols[0][24 * 30:29 * 30]) == 0
    assert max(cols[0][34 * 30:35 * 30]) > 0
    raw = openmpt_vu.serialize_vumr(vu)
    assert raw[6] & openmpt_vu.VUMR_FLAG_NAMES
    assert openmpt_vu.vumr_names(raw) == list(vu.names)
    assert openmpt_vu.parse_and_validate_vumr(raw, allow_names=True, max_frames=10**6)[0] == 5


def test_a_file_libgme_cannot_open():
    assert gme_voices.voice_names(b"not a chiptune") is None
    assert gme_voices.lead_in_frames(b"not a chiptune") is None
    with pytest.raises(ValueError):
        gme_voices.VoiceRender(b"nope", 0, 1, fade_at_ms=1000, lead_in=0)
    assert gme_voices.build_vu(b"nope", 0, ref_frames=1000, duration_s=10, fade_at_s=None) is None
