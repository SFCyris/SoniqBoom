# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MIDI on four synths and the DOS game MIDI formats (``midirender``).

The synth a file plays on (``stream.midi_synth_for``: Settings → Renderers →
MIDI synths — the SoundFont, OPL3, OPN2 or the MT-32 for Standard MIDI
files; OPL3, OPN2 or the MT-32 for game MIDI) is part of its render's cache
key (``midi_variant``: the SoundFont's path, as every MIDI key had before,
for FluidSynth's).  The scan tells the game formats by their headers (a DMX
MUS from uade's / the C64 Sidplayer's ``.mus`` too), a RIFF MIDI file's
Standard MIDI file is what FluidSynth plays.  The MT-32 plays only with the
user's ROM folder: "MT-32 ROMs not set" until there is one, its state checked
by libmt32emu (``mt32_rom_state``).  Where midirender is built
(``SB_MIDIRENDER``, else on PATH), real renders on OPL3 and OPN2."""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from soniqboom.config import settings
from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
MIDIRENDER = os.environ.get("SB_MIDIRENDER") or shutil.which("midirender")
NOTES = (60, 64, 67, 72, 67, 64, 60, 55)


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.insert(0, 0x80 | (n & 0x7F))
        n >>= 7
    return bytes(out)


def smf() -> bytes:
    ev = bytearray(b"\x00\xff\x03\x04Test\x00\xff\x51\x03\x07\xa1\x20\x00\xc0\x00")
    for n in NOTES:
        ev += b"\x00\x90" + bytes([n, 100]) + _vlq(240) + b"\x80" + bytes([n, 0])
    ev += b"\x00\xff\x2f\x00"
    return (b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + b"MTrk" + struct.pack(">I", len(ev))
            + bytes(ev))


def rmi() -> bytes:
    s = smf()
    body = b"RMID" + b"data" + struct.pack("<I", len(s)) + s
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mus() -> bytes:
    score = bytearray(b"\x40\x00\x00")
    for n in NOTES:
        score += bytes([0x90, 0x80 | n, 100]) + _vlq(35) + bytes([0x00, n])
    score += b"\x60"
    return (b"MUS\x1a" + struct.pack("<HHHHHH", len(score), 18, 1, 0, 1, 0) + struct.pack("<H", 0)
            + bytes(score))


def xmi() -> bytes:
    evnt = bytearray(b"\xff\x51\x03\x07\xa1\x20\xc0\x00")
    for n in NOTES:
        evnt += b"\x90" + bytes([n, 100]) + _vlq(30) + bytes([30])
    evnt += b"\xff\x2f\x00"

    def chunk(cid, body):
        return cid + struct.pack(">I", len(body)) + body + (b"\0" if len(body) & 1 else b"")
    form = chunk(b"FORM", b"XMID" + chunk(b"TIMB", struct.pack("<H", 0)) + chunk(b"EVNT", bytes(evnt)))
    return chunk(b"FORM", b"XDIR" + chunk(b"INFO", struct.pack("<H", 1))) + chunk(b"CAT ", b"XMID" + form)


@pytest.fixture
def synths(monkeypatch):
    for k, v in (("midi_synth", "soundfont"), ("game_midi_synth", "opl3"), ("mt32_rom_dir", ""),
                 ("midirender_path", MIDIRENDER or "")):
        monkeypatch.setattr(settings, k, v, raising=False)
    return settings


# ── Which synth, which key ───────────────────────────────────────────────────

def test_midi_synth_for_follows_the_two_settings(synths):
    from soniqboom.api import stream as st
    assert st.midi_synth_for("/m/a.mid") == "soundfont"
    assert st.midi_synth_for(".rmi") == st.midi_synth_for(".kar") == "soundfont"
    assert st.midi_synth_for("/m/a.xmi") == "opl3"
    assert st.midi_synth_for(st._DMXMUS_ROUTE) == st.midi_synth_for("/m/doom.mus") == "opl3"
    synths.midi_synth, synths.game_midi_synth = "mt32", "opn2"
    assert st.midi_synth_for("/m/a.mid") == "mt32"
    assert st.midi_synth_for("/m/a.hmp") == "opn2"
    assert st.midi_synth_for("/m/a.klm") == "opl3"          # (OPL register writes)
    synths.game_midi_synth = "soundfont"                    # (not a choice: FluidSynth reads SMF only)
    assert st.midi_synth_for("/m/a.xmi") == "opl3"
    synths.midi_synth = "bogus"
    assert st.midi_synth_for("/m/a.mid") == "soundfont"


def test_midi_variant_keeps_the_soundfont_key_and_names_every_other_synth(synths, monkeypatch):
    from soniqboom.api import stream as st
    from soniqboom import config
    monkeypatch.setattr(config, "get_active_soundfont", lambda: Path("/sf/GeneralUser.sf2"))
    assert st.midi_variant("/m/a.mid") == "/sf/GeneralUser.sf2"      # (unchanged keys)
    keys = {st.midi_variant("/m/a.mid")}
    for synth in ("opl3", "opn2", "mt32"):
        synths.midi_synth = synth
        keys.add(st.midi_variant("/m/a.mid"))
    assert len(keys) == 4
    synths.midi_synth = "opl3"
    assert "Win9x" in st.midi_variant("/m/a.mid")
    assert "default AIL" in st.midi_variant("/m/a.xmi")              # (the format's bank)
    assert "HMI (Descent" in st.midi_variant("/m/a.hmi")
    assert "DMXOPL3" in st.midi_variant("/m/doom.mus")
    assert st.midi_variant("/m/doom.mus") == st.midi_variant(st._DMXMUS_ROUTE)
    assert st.midi_variant("/m/a.xmi") != st.midi_variant("/m/a.mid")


def test_mt32_variant_changes_with_the_rom_folder(synths, tmp_path):
    from soniqboom.api import stream as st
    synths.midi_synth = "mt32"
    synths.mt32_rom_dir = ""
    unset = st.midi_variant("/m/a.mid")
    roms = tmp_path / "roms"
    roms.mkdir()
    synths.mt32_rom_dir = str(roms)
    one = st.midi_variant("/m/a.mid")
    os.utime(roms, ns=(1, 1))
    two = st.midi_variant("/m/a.mid")
    assert len({unset, one, two}) == 3


def test_cache_key_call_sites_use_the_variant():
    # every MIDI key / render the server makes goes through midi_variant
    # (stream, the waveform, cast); none computes a SoundFont key of its own
    for rel in ("soniqboom/api/stream.py", "soniqboom/api/tracks.py",
                "soniqboom/core/cast_render.py", "soniqboom/core/cast_session.py"):
        src = (ROOT / rel).read_text()
        assert 'soundfont_path=str(sf) if sf else ""' not in src, rel
        assert '_ck(track_id, "midi", 0, str(' not in src, rel


# ── The scan ─────────────────────────────────────────────────────────────────

def test_riff_midi_and_dmx_mus_headers():
    assert md.riff_midi_data(rmi()) == smf()
    assert md.riff_midi_data(smf()) is None
    assert md.riff_midi_data(b"RIFF\0\0\0\0WAVEfmt ") is None
    assert md.is_dmx_mus(mus())
    assert not md.is_dmx_mus(b"MUS\x1a" + b"\0" * 12)
    assert not md.is_dmx_mus(smf())


def test_game_midi_files_scan_by_their_headers(synths, tmp_path, monkeypatch):
    if not MIDIRENDER:
        monkeypatch.setattr(md, "find_midirender", lambda: None)
    for name, body, fmt in (("a.xmi", xmi(), "XMIDI"), ("doom.mus", mus(), "DMX MUS"),
                            ("b.rmi", rmi(), "RIFF MIDI"), ("c.kar", smf(), "MIDI")):
        f = tmp_path / name
        f.write_bytes(body)
        m = md.extract(f, "g1")
        assert m.format == fmt, name
        if fmt in ("RIFF MIDI", "MIDI"):
            assert m.title == "Test" and m.duration == pytest.approx(2.0, abs=0.1)
        elif MIDIRENDER:
            assert m.duration >= 2.0, name                    # (midirender's length)
    for name, body in (("x.xmi", smf()), ("x.hmi", smf()), ("x.hmp", b"\0" * 64),
                       ("x.rmi", smf()), ("x.gmf", b"GMF\x02" + b"\0" * 40)):
        f = tmp_path / name
        f.write_bytes(body)
        with pytest.raises(ValueError):
            md.extract(f, "g2")


def test_game_midi_routes_through_the_midi_branch():
    from soniqboom.api import stream as st
    from soniqboom.core import cast_render
    for ext in (".xmi", ".hmi", ".hmp", ".gmf", ".klm", ".kar", ".rmi"):
        assert ext in st._MIDI_EXTS and ext in cast_render._MIDI_EXTS, ext
        assert st._render_ident(f"/m/x{ext}", None) == (ext, False)
    assert st._DMXMUS_ROUTE in st._MIDI_EXTS
    assert st._render_ident("/m/doom.mus", {"format": md.DMX_MUS_FORMAT}) == (st._DMXMUS_ROUTE, False)
    assert st._is_rendered_ext(".xmi") and st._is_rendered_ext(st._DMXMUS_ROUTE)


# ── The MT-32 ROM folder ─────────────────────────────────────────────────────

def test_mt32_rom_state_not_set_and_not_a_folder(synths, tmp_path):
    from soniqboom.api import stream as st
    assert st.mt32_rom_state("") == {"state": "not_set"}
    assert st.mt32_rom_state("   ") == {"state": "not_set"}
    assert st.mt32_rom_state(str(tmp_path / "nope"))["state"] == "invalid"
    f = tmp_path / "file.rom"
    f.write_bytes(b"\0")
    assert st.mt32_rom_state(str(f)) == {"state": "invalid", "error": "That is not a folder."}


@pytest.mark.skipif(not MIDIRENDER, reason="midirender not built")
def test_mt32_rom_state_finds_no_roms_in_files_that_are_none(synths, tmp_path):
    from soniqboom.api import stream as st
    roms = tmp_path / "roms"
    roms.mkdir()
    assert st.mt32_rom_state(str(roms))["state"] == "invalid"          # (empty)
    (roms / "MT32_CONTROL.ROM").write_bytes(b"\0" * 65536)
    (roms / "MT32_PCM.ROM").write_bytes(b"\x55" * 524288)
    state = st.mt32_rom_state(str(roms))
    assert state == {"state": "invalid", "error": "No MT-32 or CM-32L ROM image is in the folder."}


def test_mt32_play_without_roms_says_they_are_not_set(synths, tmp_path, monkeypatch):
    from soniqboom.api import stream as st
    monkeypatch.setattr(st, "_find_renderer", lambda *_a: "/usr/bin/true")
    synths.midi_synth = "mt32"
    f = tmp_path / "a.mid"
    f.write_bytes(smf())
    with pytest.raises(st.HTTPException) as exc:
        asyncio.run(st._render_midi(f))
    assert exc.value.status_code == 501 and exc.value.detail.startswith("MT-32 ROMs not set")
    synths.mt32_rom_dir = str(tmp_path / "missing")
    with pytest.raises(st.HTTPException) as exc:
        asyncio.run(st._render_midi(f))
    assert exc.value.status_code == 503 and "MT-32 ROMs not usable" in exc.value.detail


def test_midirender_missing_is_a_clear_501(synths, tmp_path, monkeypatch):
    from soniqboom.api import stream as st
    monkeypatch.setattr(st, "_find_renderer", lambda *_a: None)
    f = tmp_path / "a.xmi"
    f.write_bytes(xmi())
    with pytest.raises(st.HTTPException) as exc:
        asyncio.run(st._render_midi(f))
    assert exc.value.status_code == 501 and "midirender not installed" in exc.value.detail


def test_settings_api_refuses_an_unknown_synth_before_saving_anything(synths):
    from fastapi import HTTPException
    from soniqboom.api import admin
    before = (settings.midi_synth, settings.sid_model)
    for body in ({"renderers": {"midi_synth": "sb16"}},
                 {"renderers": {"game_midi_synth": "soundfont"}},
                 {"renderers": {"sid_model": "6581", "midi_synth": "gm"}}):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(admin.update_settings(body, _tok="t"))
        assert exc.value.status_code == 422
    assert (settings.midi_synth, settings.sid_model) == before     # (nothing applied)


def test_mt32_endpoint_reports_the_state(synths, tmp_path):
    from soniqboom.api import admin
    assert asyncio.run(admin.mt32_roms(path="", _tok="t")) == {"state": "not_set"}
    assert asyncio.run(admin.mt32_roms(path=None, _tok="t")) == {"state": "not_set"}
    assert asyncio.run(admin.mt32_roms(path=str(tmp_path / "x"), _tok="t"))["state"] == "invalid"


def test_the_settings_ui_has_the_controls():
    html = (ROOT / "soniqboom/frontend/index.html").read_text()
    for el in ('id="setting-midi-synth"', 'id="setting-game-midi-synth"',
               'id="setting-mt32-rom-dir"', 'id="mt32-rom-state"', 'id="btn-check-mt32"',
               'id="btn-save-midi-synths"', 'id="renderer-icon-midirender"',
               'id="setting-path-midirender"'):
        assert el in html, el
    js = (ROOT / "soniqboom/frontend/js/admin.js").read_text()
    assert "/admin/mt32" in js and "MT-32 ROMs not set." in js


# ── Real renders ─────────────────────────────────────────────────────────────

def _seconds(p: Path) -> float:
    with wave.open(str(p)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (44100, 2, 2)
        return w.getnframes() / w.getframerate()


@pytest.mark.skipif(not MIDIRENDER, reason="midirender not built")
@pytest.mark.parametrize("synth", ["opl3", "opn2"])
def test_real_renders_on_fm(synths, tmp_path, synth):
    from soniqboom.api import stream as st
    synths.midi_synth = synths.game_midi_synth = synth
    for name, body in (("a.mid", smf()), ("b.rmi", rmi()), ("c.xmi", xmi()), ("doom.mus", mus())):
        f = tmp_path / name
        f.write_bytes(body)
        out = asyncio.run(st._render_midi(f))
        try:
            secs = _seconds(out)
            # eight quarter-second notes, then the release tail (at most 6 s)
            assert 1.9 < secs < 9, (name, secs)
            assert st._wav_is_audible(out), name
        finally:
            Path(out).unlink(missing_ok=True)


@pytest.mark.skipif(not MIDIRENDER, reason="midirender not built")
def test_midirender_lists_its_synths_and_banks():
    import subprocess
    out = subprocess.run([MIDIRENDER, "-L"], capture_output=True, text=True, timeout=30).stdout
    first, *banks = out.splitlines()
    assert first.startswith("synths=opl3")
    from soniqboom.api import stream as st
    names = [b.split(" ", 1)[1] for b in banks]
    for bank in {*st._OPL3_BANKS.values(), st._OPL3_GM_BANK}:
        assert any(n.startswith(bank) for n in names), bank
    assert not any("grey" in n.lower() for n in names)
