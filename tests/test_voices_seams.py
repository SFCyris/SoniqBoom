# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the voice mixer meets the reference render: the SID mixer's
command lines (2SID / 3SID stereo, the SID fade, 3 voices per chip) and the
libgme voice renders' fade (the chip fade set, an NSFe-stated one) equal the
reference render's.  No renderer or libgme needed: the SID side compares
argv lists, the libgme side records the fade calls on a fake libgme."""
import ctypes
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from soniqboom.api import stream as s
from soniqboom.api import voices
from soniqboom.config import settings
from soniqboom.core import gme_render, gme_voices, render_options

SID = Path("tune.sid")


def _tune(chips: int):
    return SimpleNamespace(channels=chips)


def _mix(track, mask: int, sub: int = 0):
    (cmd,) = voices._sid_voice_cmds("sidplayfp", SID, track, sub, 180, 1, "mix", mask, ["o.wav"])
    return cmd


def _reference(track, sub: int = 0):
    return s._sid_render_cmd("sidplayfp", SID, sub, 180, "o.wav", start=1,
                             stereo=s.sid_stereo(track))


@pytest.mark.parametrize("chips", [1, 2])
@pytest.mark.parametrize("fade", [0.0, 3.0])
def test_sid_mix_all_voices_on_is_the_reference_render(monkeypatch, chips, fade):
    monkeypatch.setattr(settings, "sid_fade", fade)
    t = _tune(chips)
    full = (1 << (3 * chips)) - 1
    assert _mix(t, full) == _reference(t)
    assert _mix(t, full, sub=2) == _reference(t, sub=2)
    ref = _reference(t)
    assert ("-s" in ref) == (chips >= 2) and ("-m" in ref) == (chips == 1)
    assert (f"-fob{fade:g}" in ref) == (fade > 0)


@pytest.mark.parametrize("fade", [0.0, 3.0])
def test_sid_mix_mutes_keep_stereo_and_fade(monkeypatch, fade):
    monkeypatch.setattr(settings, "sid_fade", fade)
    one, two = _tune(1), _tune(2)
    # 1SID, voice 2 off: -u2, mono, the fade kept
    c = _mix(one, 0b101)
    assert [a for a in c if a.startswith("-u")] == ["-u2"]
    assert "-m" in c and "-s" not in c
    assert (f"-fob{fade:g}" in c) == (fade > 0)
    # 2SID, voice 5 (chip 2, voice 2) off: -u5 only, stereo, the fade kept
    c = _mix(two, 0b111111 & ~(1 << 4))
    assert [a for a in c if a.startswith("-u")] == ["-u5"]
    assert "-s" in c and "-m" not in c
    assert (f"-fob{fade:g}" in c) == (fade > 0)
    # everything but chip 1: -u1..-u3; a 3SID tune: up to -u9
    assert [a for a in _mix(two, 0b111000) if a.startswith("-u")] == ["-u1", "-u2", "-u3"]
    assert [a for a in _mix(_tune(3), 0b011111111) if a.startswith("-u")] == ["-u9"]
    # the mix differs from the reference only by the mutes
    assert [a for a in c if not a.startswith("-u")] == _reference(two)


@pytest.mark.parametrize("chips", [1, 2, 3])
def test_sid_scopes_one_mono_pass_per_voice(monkeypatch, chips):
    monkeypatch.setattr(settings, "sid_fade", 3.0)
    n = 3 * chips
    outs = [f"v{i}.wav" for i in range(n)]
    cmds = voices._sid_voice_cmds("sidplayfp", SID, _tune(chips), 0, 180, 1, "scope", 0, outs)
    assert len(cmds) == n
    for k, c in enumerate(cmds, start=1):
        assert sorted(a for a in c if a.startswith("-u")) == sorted(
            f"-u{v}" for v in range(1, n + 1) if v != k)
        assert "-m" in c and "-s" not in c and not any(a.startswith("-fob") for a in c)
    assert len(voices._sid_voice_names(_tune(chips))) == n
    assert voices._sid_voice_names(_tune(1)) == ["Voice 1", "Voice 2", "Voice 3"]


# ── libgme: the voices fade like the reference render ──────────────────────

class _FakeGme:
    """Enough libgme for one render's set-up: records the fade calls."""

    def __init__(self):
        self.fades = []

    def gme_open_data(self, data, n, emu_ref, rate):
        emu_ref._obj.value = 1
        return 0

    def gme_start_track(self, emu, track):
        return 0

    def gme_set_fade(self, emu, start):
        self.fades.append((int(start), gme_render.FADE_S * 1000))

    def gme_set_fade_msecs(self, emu, start, length):
        self.fades.append((int(start), int(length)))

    def gme_track_ended(self, emu):
        return 1

    def gme_play(self, emu, n, buf):
        return 0

    def gme_delete(self, emu):
        pass

    def gme_ignore_silence(self, emu, on):
        pass

    def gme_voice_count(self, emu):
        return 4

    def gme_mute_voices(self, emu, mask):
        pass


def _nsfe_with_fade(fade_ms: int) -> bytes:
    body = struct.pack("<i", fade_ms)
    return b"NSFE" + struct.pack("<I", len(body)) + b"fade" + body + struct.pack("<I", 0) + b"NEND"


def _fades(monkeypatch, data: bytes, stated: "float | None"):
    """(the reference render's fade, the voice render's) for ``data``."""
    fake = _FakeGme()
    monkeypatch.setattr(gme_render, "_lib", fake)
    monkeypatch.setattr(gme_voices, "_lib", fake)
    monkeypatch.setattr(gme_voices, "_ok", True)
    monkeypatch.setattr(gme_render, "_have_fade_len", True)
    monkeypatch.setattr(gme_render, "is_available", lambda: True)
    monkeypatch.setattr(gme_render, "play_seconds", lambda d, sub: stated)
    # the reference: stream._render_gme's plan and fade
    duration, fade_at = gme_render.render_plan(data, 0, float(settings.sid_default_duration))
    gme_render.render_pcm(data, 0, duration, lambda c: True, fade_at_s=fade_at,
                          fade_len_s=gme_render.reference_fade_len_s(duration, fade_at))
    ref = fake.fades[-1] if fake.fades else None
    # the voices: what the mixer job gets
    _d, vdur, vfade_at = voices.gme_inputs(Path("x.nsf"), 0, raw=data)
    fake.fades.clear()
    r = gme_voices.VoiceRender(data, 0, 0b1111, fade_at_ms=gme_voices.fade_ms(vdur, vfade_at),
                               lead_in=0)
    r.close()
    return ref, (fake.fades[-1] if fake.fades else None)


def test_the_reference_takes_its_fade_from_the_shared_helper():
    src = Path(s.__file__).read_text()
    body = src[src.index("async def _render_gme("):]
    body = body[:body.index("\nasync def ", 10)]
    assert "fade_len = gme_render.reference_fade_len_s(duration, fade_at)" in body


@pytest.mark.parametrize("chip_fade,stated", [(-1.0, None), (3.0, None), (0.0, None),
                                              (-1.0, 60.0), (3.0, 60.0), (0.0, 60.0)])
def test_gme_voice_fade_equals_the_reference(monkeypatch, chip_fade, stated):
    monkeypatch.setattr(settings, "chip_fade", chip_fade)
    ref, got = _fades(monkeypatch, b"NESM\x1a" + bytes(200), stated)
    assert got == ref
    if chip_fade == 0.0:
        assert ref is None                                  # no fade at all
    else:                                                   # the chip fade set (or its default)
        assert ref[1] == int(render_options.chip_fade_s() * 1000)
        assert ref[1] == (3000 if chip_fade == 3.0 else int(render_options.CHIP_DEFAULT_FADE_S * 1000))


@pytest.mark.parametrize("chip_fade", [-1.0, 3.0, 0.0])
def test_gme_voice_fade_follows_an_nsfe_stated_fade(monkeypatch, chip_fade):
    monkeypatch.setattr(settings, "chip_fade", chip_fade)
    ref, got = _fades(monkeypatch, _nsfe_with_fade(1500), 60.0)
    assert ref == (60000, 1500)                              # the NSFe's own 1.5 s
    assert got == ref


def test_set_fade_lengths():
    assert gme_render.fade_window_ms(180, None, 0.0) == (180000, 0)
    assert gme_render.fade_window_ms(180, None, 3.0) == (177000, 3000)
    assert gme_render.fade_window_ms(180, None) == (172000, gme_render.FADE_S * 1000)
    assert gme_render.fade_window_ms(68, 60.0, 8.0) == (60000, 8000)
