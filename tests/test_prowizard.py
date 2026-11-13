# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""soniqboom/core/prowizard.py — packed Amiga modules rebuilt as ProTracker
modules: identification, structure of the output, playback in openmpt123,
no false positives on other formats, and robustness on damaged input."""
from __future__ import annotations

import array
import math
import random
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from soniqboom.core import prowizard as pw

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "internal" / "testdata"
NP = TESTDATA / "nostalgicplayer"

# folder -> (format name, file extension)
CASES = {
    "benreplay": ("Ben Replay", "ben"),
    "binarypacker": ("Binary Packer", "bnr"),
    "devilsreplay": ("Devils Replay", "dev"),
    "gnoipacker": ("GnoiPacker", "gnoi"),
    "gnuprotectedmodule": ("Gnu Protected Module", "gpmo"),
    "gnuplayer": ("GnuPlayer", "gnu"),
    "hcdprotector": ("HCD Protector", "hcd"),
    "newtronpacker10": ("Newtron Packer 1.0", "nw1"),
    "newtronpacker20": ("Newtron Packer 2.0", "nw2"),
    "perfectsong1": ("Perfect Song 1", "pf1"),
    "perfectsong2": ("Perfect Song 2", "pf2"),
    "modulepatterncompressor": ("Module-Patterncompressor", "pmd3"),
    "noisetrackercompressed": ("NoiseTracker Compressed", "ntc"),
    "theplayer22a": ("The Player 2.2A", "p22a"),
    "theplayer30a": ("The Player 3.0A", "p30a"),
}

# The only sample of this module has volume 0 in its source header (the
# word at 0x1e is 0x0000) and the song has no volume effects, so the
# converted module plays digital silence.
SILENT_IN_SOURCE = {"Gnu Design - Skizzo Demo - Part #15.gnoi"}


def _positive_files() -> list[tuple[str, Path]]:
    out = []
    for folder in CASES:
        d = NP / folder
        if d.is_dir():
            out += [(folder, p) for p in sorted(d.iterdir())
                    if p.is_file() and not p.name.startswith(".")]
    return out


def _negative_files() -> list[Path]:
    out: list[Path] = []
    if NP.is_dir():
        for p in sorted(NP.iterdir()):
            if p.name in CASES:
                continue
            if p.is_file():
                out.append(p)
            elif p.is_dir():
                out += sorted(x for x in p.rglob("*") if x.is_file())
    for sub in ("tracker", "uade"):
        d = TESTDATA / sub
        if d.is_dir():
            out += sorted(x for x in d.rglob("*") if x.is_file())
    return out


POSITIVES = _positive_files()
_IDS = [f"{folder}/{p.name}" for folder, p in POSITIVES]


def _need_data() -> None:
    if not POSITIVES:
        pytest.skip("internal/testdata/nostalgicplayer is not present")


def _openmpt() -> str:
    exe = shutil.which("openmpt123")
    if not exe:
        pytest.skip("openmpt123 is not installed")
    return exe


# ── helpers: reading the converted module ─────────────────────────────────

_PERIODS = {hi * 256 + lo for hi, lo in pw._PERIODS}


def _layout(mod: bytes) -> dict:
    """Header fields of a 4-channel ProTracker module."""
    lengths = [int.from_bytes(mod[42 + i * 30:44 + i * 30], "big") * 2 for i in range(31)]
    volumes = [mod[45 + i * 30] for i in range(31)]
    song_length = mod[950]
    positions = mod[952:1080]
    patterns = max(positions) + 1
    return {
        "lengths": lengths, "volumes": volumes, "song_length": song_length,
        "positions": positions, "patterns": patterns,
        "sample_data": mod[1084 + patterns * 1024:],
    }


def _notes(mod: bytes, patterns: int):
    for o in range(1084, 1084 + patterns * 1024, 4):
        b0, b1, b2, b3 = mod[o:o + 4]
        yield ((b0 & 0x0F) << 8) | b1, (b0 & 0xF0) | (b2 >> 4), b2 & 0x0F, b3


def _duration_seconds(info: str) -> float:
    m = re.search(r"^Duration\.*:\s*([0-9:.]+)", info, re.M)
    assert m, info
    secs = 0.0
    for part in m.group(1).split(":"):
        secs = secs * 60 + float(part)
    return secs


def _peak_dbfs(wav_path: Path) -> float:
    with wave.open(str(wav_path)) as w:
        assert w.getsampwidth() == 2
        frames = w.readframes(w.getnframes())
    samples = array.array("h", frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return -math.inf
    peak = max(max(samples), -min(samples))
    return 20 * math.log10(peak / 32768) if peak else -math.inf


# ── identification and conversion ─────────────────────────────────────────

def test_formats_table_matches_the_cases():
    assert set(pw.FORMATS) == {name for name, _ in CASES.values()}
    for name, ext in CASES.values():
        assert ext in pw.FORMATS[name]
        assert all(t == t.lower() and "." not in t for t in pw.FORMATS[name])


@pytest.mark.parametrize("folder,path", POSITIVES, ids=_IDS)
def test_identify_and_convert(folder, path):
    _need_data()
    data = path.read_bytes()
    name, ext = CASES[folder]
    assert path.suffix.lower() == "." + ext
    assert pw.identify(data) == name
    assert pw.identify(bytearray(data)) == name
    mod = pw.convert(data)
    assert isinstance(mod, bytes)
    assert mod[1080:1084] == b"M.K."

    lay = _layout(mod)
    assert 1 <= lay["song_length"] <= 128
    assert lay["patterns"] <= 64
    # The patterns a player counts (from all 128 list entries) are the
    # patterns written, so the sample data is exactly what the 31 sample
    # headers describe.
    assert len(lay["sample_data"]) == sum(lay["lengths"])
    assert sum(1 for n in lay["lengths"] if n) >= 1
    assert all(v <= 64 for v in lay["volumes"])

    notes = list(_notes(mod, lay["patterns"]))
    played = [(per, smp) for per, smp, _, _ in notes if per]
    assert played, "no notes in the converted patterns"
    # Every note is a ProTracker period.
    assert all(per in _PERIODS for per, _ in played)
    # Nearly every note's sample number names a sample that has data (one
    # Binary Packer file has 2 notes on an empty sample in its source).
    refs = [smp for per, smp, _, _ in notes if per and smp]
    assert refs
    empty = sum(1 for s in refs if not lay["lengths"][s - 1])
    assert empty <= max(2, len(refs) // 100), (empty, len(refs))


@pytest.mark.parametrize("folder,path", [c for c in POSITIVES if c[0] != "gnuplayer"],
                         ids=[i for i, c in zip(_IDS, POSITIVES) if c[0] != "gnuplayer"])
def test_sample_data_is_copied_from_the_source(folder, path):
    # Every format except GnuPlayer (delta-packed samples) stores the sample
    # bytes verbatim, in sample order, at the end of the file (Perfect Song 1
    # keeps its 702-byte title/sample-name block after them).  Walking back
    # from that end, each converted sample must occur in the source before
    # the one after it, the last one ending exactly there.
    _need_data()
    data = path.read_bytes()
    lay = _layout(pw.convert(data))
    chunks = []
    pos = 0
    for length in lay["lengths"]:
        if length:
            chunks.append(lay["sample_data"][pos:pos + length])
            pos += length
    end = len(data) - (702 if folder == "perfectsong1" else 0)
    first = True
    for chunk in reversed(chunks):
        found = data.rfind(chunk, 0, end)
        assert found >= 0, "sample data not found in order"
        if first:
            assert found + len(chunk) == end, "last sample does not end the data"
            first = False
        end = found


@pytest.mark.parametrize("folder", ["benreplay", "binarypacker", "hcdprotector",
                                    "gnuprotectedmodule", "devilsreplay",
                                    "modulepatterncompressor"])
def test_protracker_style_headers_carry_over(folder):
    # Formats whose header is a ProTracker header keep the title, the sample
    # names and lengths, the song length and the position list unchanged.
    _need_data()
    files = [p for f, p in POSITIVES if f == folder]
    if not files:
        pytest.skip(f"no {folder} test files")
    for path in files:
        data = path.read_bytes()
        mod = pw.convert(data)
        assert mod[:20] == data[:20]
        for i in range(31):
            o = 20 + i * 30
            assert mod[o:o + 24] == data[o:o + 24]          # name + length
        assert mod[950] == data[950]
        assert mod[952:952 + mod[950]] == data[952:952 + data[950]]


def _files_of(folder: str) -> list[Path]:
    _need_data()
    files = [p for f, p in POSITIVES if f == folder]
    if not files:
        pytest.skip(f"no {folder} test files")
    return files


@pytest.mark.parametrize("folder,offset", [("newtronpacker20", 380)])
def test_raw_patterns_are_copied(folder, offset):
    for path in _files_of(folder):
        data = path.read_bytes()
        mod = pw.convert(data)
        n = _layout(mod)["patterns"]
        assert mod[1084:1084 + n * 1024] == data[offset:offset + n * 1024]


def test_gnu_protected_module_drops_8xx_and_halves_volume():
    for path in _files_of("gnuprotectedmodule"):
        data = path.read_bytes()
        mod = pw.convert(data)
        for i in range(31):
            assert mod[45 + i * 30] == data[45 + i * 30] // 2
        n = _layout(mod)["patterns"]
        assert all(eff != 8 for _, _, eff, _ in _notes(mod, n))


def test_gnuplayer_samples_are_unpacked():
    for path in _files_of("gnuplayer"):
        data = path.read_bytes()
        mod = pw.convert(data)
        lay = _layout(mod)
        # 4-bit deltas: the unpacked samples are about twice the packed size.
        assert len(lay["sample_data"]) > len(data)
        assert all(v == 64 for v, n in zip(lay["volumes"], lay["lengths"]) if n)
        # Signed deltas of -8..7 give a smooth waveform: a jump of more than
        # 64 between neighbouring samples (an 8-bit wrap) is rare.
        values = [b - 256 if b > 127 else b for b in lay["sample_data"]]
        jumps = sum(1 for a, b in zip(values, values[1:]) if abs(a - b) > 64)
        assert jumps < len(values) // 1000


def _gnoi(header_patterns: int, positions: bytes) -> bytes:
    """A minimal GnoiPacker module: one 4-byte sample, ``header_patterns``
    patterns of 768 bytes (pattern ``i`` holds note byte ``i + 1`` on row 0),
    the sample data 01 02 03 04 at the end."""
    head = b"synthetic gnoi".ljust(20, b"\0")
    head += bytes((0x80 | (len(positions) + 1), 0, 1, header_patterns)) + b"-GD-"
    head += (2).to_bytes(2, "big") + (0x40).to_bytes(2, "big") + bytes(2) + (1).to_bytes(2, "big")
    head += positions + b"\0"                       # position list + padding byte
    pats = b"".join(bytes((i + 1, 0x10, 0)) + bytes(765) for i in range(header_patterns))
    return head + pats + b"\x01\x02\x03\x04"


def test_gnoipacker_skips_unused_patterns():
    # The header counts 3 stored patterns; the song plays only pattern 0, so
    # one pattern is written and the other two are skipped before the samples.
    data = _gnoi(3, b"\x00")
    assert pw.identify(data) == "GnoiPacker"
    mod = pw.convert(data)
    lay = _layout(mod)
    assert lay["song_length"] == 1 and lay["patterns"] == 1
    assert lay["sample_data"] == b"\x01\x02\x03\x04"
    # Row 0, channel 1: note byte 1 = period index 0 (856), sample 1.
    assert mod[1084:1088] == bytes((0x03, 0x58, 0x10, 0x00))
    # Using all three patterns reads them all and lands on the same samples.
    mod3 = pw.convert(_gnoi(3, b"\x00\x02\x01"))
    lay3 = _layout(mod3)
    assert lay3["patterns"] == 3 and lay3["sample_data"] == b"\x01\x02\x03\x04"
    assert lay3["positions"][:3] == b"\x00\x02\x01"
    assert mod3[1084 + 1024:1084 + 1024 + 2] == bytes((0x03, 0x28))           # index 1 = 808
    assert mod3[1084 + 2 * 1024:1084 + 2 * 1024 + 2] == bytes((0x02, 0xFA))   # index 2 = 762


def test_silent_module_is_silent_in_its_source():
    _need_data()
    for folder, path in POSITIVES:
        if path.name in SILENT_IN_SOURCE:
            data = path.read_bytes()
            assert data[22] == 1 and data[0x1E:0x20] == b"\0\0"   # 1 sample, volume 0
            lay = _layout(pw.convert(data))
            assert [v for v, n in zip(lay["volumes"], lay["lengths"]) if n] == [0]


# ── note decoding, checked against the ProTracker period table ────────────

# ProTracker finetune-0 periods C-1..B-3; NostalgicPlayer's table repeats 113
# for note indexes 36-44.
PT = [856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
      428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
      214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113] + [113] * 9


def _cell(mod: bytes, pattern: int, row: int, channel: int) -> bytes:
    o = 1084 + pattern * 1024 + row * 16 + channel * 4
    return mod[o:o + 4]


def _period_sample(cell: bytes) -> tuple[int, int]:
    return ((cell[0] & 0x0F) << 8) | cell[1], (cell[0] & 0xF0) | (cell[2] >> 4)


def test_ben_replay_notes():
    # Ben Replay cell: b1 = sample hi nibble | sample lo nibble, b2 = 2 * (note
    # index + 1), b3 = 2 * effect, b4 = effect parameter.
    for path in _files_of("benreplay"):
        data = path.read_bytes()
        mod = pw.convert(data)
        n = _layout(mod)["patterns"]
        for o in range(0, n * 1024, 4):
            b1, b2, b3, b4 = data[0x43C + o:0x43C + o + 4]
            per = PT[b2 // 2 - 1] if b2 // 2 else 0
            want = bytes(((b1 & 0xF0) | (per >> 8), per & 0xFF,
                          (((b1 & 0x0F) << 4) | (b3 >> 1)) & 0xFF, b4))
            assert mod[1084 + o:1088 + o] == want, o


def test_binary_packer_notes():
    # Binary Packer cell: note index counted down from 36, flag 0x40 = no note.
    for path in _files_of("binarypacker"):
        data = path.read_bytes()
        mod = pw.convert(data)
        n = _layout(mod)["patterns"]
        for o in range(0, n * 1024, 4):
            b1, b2, b3, b4 = data[0x43C + o:0x43C + o + 4]
            per, smp = _period_sample(mod[1084 + o:1088 + o])
            assert smp == ((b4 << 1) & 0xF0) >> 4 | ((b4 >> 3) & 0xF0), o
            assert per == (0 if b1 & 0x40 else PT[36 - (b2 >> 1)]), o


@pytest.mark.parametrize("folder", ["perfectsong1", "perfectsong2"])
def test_perfect_song_notes(folder):
    # Perfect Song cell: note = 2 * (index + 1) | sample bit 4; next byte's
    # high nibble = sample bits 0-3.  Patterns are stored from 0x3fe.
    for path in _files_of(folder):
        data = path.read_bytes()
        mod = pw.convert(data)
        n = _layout(mod)["patterns"]
        for o in range(0, n * 1024, 4):
            note, smp = data[0x3FE + o], data[0x3FF + o]
            per = PT[note // 2 - 1] if note // 2 else 0
            assert _period_sample(mod[1084 + o:1088 + o]) == (per, ((note & 1) << 4) | (smp >> 4)), o


@pytest.mark.parametrize("folder", ["theplayer22a", "theplayer30a"])
def test_the_player_first_row(folder):
    # Row 0 of the first pattern is the first 4 cells of the first track:
    # c1 = 2 * (note index + 1) | sample bit 4, c2 high nibble = sample bits 0-3.
    for path in _files_of(folder):
        data = path.read_bytes()
        mod = pw.convert(data)
        track_data = int.from_bytes(data[8:12], "big")
        track_table = int.from_bytes(data[12:16], "big")
        first = int.from_bytes(data[track_table + 4:track_table + 6], "big")
        base = track_data + 4 + first
        for ch in range(4):
            c1, c2 = data[base + ch * 4], data[base + ch * 4 + 1]
            note = (c1 & 0x7F) // 2
            per = PT[note - 1] if note else 0
            assert _period_sample(_cell(mod, 0, 0, ch)) == (per, ((c1 & 1) << 4) | (c2 >> 4))


def test_noisetracker_compressed_patterns():
    # Each pattern starts (at an even offset) with "PATT" + 3 words, then one
    # byte stream per channel: 0xff n skips n rows; otherwise the byte is note
    # index + 1 plus a class offset - 0xc0 (sample bit 4, no effect), 0x80
    # (sample bit 4 and effect), 0x40 (no effect), 0x00 (effect) - then the
    # sample/effect byte and, for the effect classes, the parameter byte.
    for path in _files_of("noisetrackercompressed"):
        data = path.read_bytes()
        mod = pw.convert(data)
        o = 0x1F6
        classes = set()
        for pat in range(_layout(mod)["patterns"]):
            o += o & 1
            assert data[o:o + 4] == b"PATT"
            o += 10
            for ch in range(4):
                row = 0
                while row < 64:
                    b = data[o]
                    if b == 0xFF:
                        for r in range(row, min(64, row + data[o + 1])):
                            assert _cell(mod, pat, r, ch) == bytes(4)
                        row += data[o + 1]
                        o += 2
                        continue
                    base = 0xC0 if b >= 0xC0 else 0x80 if b >= 0x80 else 0x40 if b >= 0x40 else 0
                    note = b - base
                    per = PT[note - 1] if note else 0
                    param = data[o + 2] if base in (0x80, 0x00) else 0
                    want = bytes(((per >> 8) | (0x10 if base >= 0x80 else 0), per & 0xFF,
                                  data[o + 1], param))
                    assert _cell(mod, pat, row, ch) == want, (pat, ch, row)
                    o += 3 if base in (0x80, 0x00) else 2
                    row += 1
                    classes.add(base)
        assert classes, "no notes decoded"


def test_noisetracker_compressed_all_note_classes():
    # A one-pattern module using samples 1 and 17; channel 1 holds one event
    # of each class, then a run of empty rows; channels 2-4 are empty.
    stream = bytes((0xC0 + 1, 0x10,                 # note 0, sample 0x11
                    0x80 + 2, 0x1C, 0x20,           # note 1, sample 0x11, C20
                    0x40 + 3, 0x20,                 # note 2, sample 2
                    0x00 + 4, 0x1A, 0x05,           # note 3, sample 1, A05
                    0xFF, 60)) + bytes((0xFF, 64)) * 3
    body = b"PATT" + bytes(6) + stream
    sample_offset = 0x1F6 + len(body) + (len(body) & 1)
    head = bytearray()
    for i in range(31):
        used = i in (0, 16)
        head += sample_offset.to_bytes(4, "big")
        head += (256 if used else 0).to_bytes(2, "big")     # 512 bytes each
        head += bytes((0, 0x40 if used else 0)) + bytes(2) + (1 if used else 0).to_bytes(2, "big")
    head += bytes((1, 0)) + bytes(128)              # 1 position: pattern 0
    assert len(head) == 0x1F6
    samples = bytes(range(256)) * 4
    data = bytes(head) + body + bytes(len(body) & 1) + samples
    assert len(data) >= 1080                        # the format's minimum
    assert pw.identify(data) == "NoiseTracker Compressed"
    mod = pw.convert(data)
    assert _cell(mod, 0, 0, 0) == bytes((0x13, 0x58, 0x10, 0x00))
    assert _cell(mod, 0, 1, 0) == bytes((0x13, 0x28, 0x1C, 0x20))
    assert _cell(mod, 0, 2, 0) == bytes((0x02, 0xFA, 0x20, 0x00))
    assert _cell(mod, 0, 3, 0) == bytes((0x02, 0xD0, 0x1A, 0x05))
    assert all(_cell(mod, 0, r, c) == bytes(4) for r in range(64) for c in range(4)
               if not (c == 0 and r < 4))
    assert _layout(mod)["sample_data"] == samples


# ── the pattern count a player sees ───────────────────────────────────────

def test_pattern_referenced_only_past_the_song_end():
    # Ben Replay reads its pattern count from all 128 list entries.  Add a
    # pattern that only list entry 127 (past the song end) refers to: it is
    # written, so the output's list must make a player count it too.
    path = _files_of("benreplay")[0]
    data = path.read_bytes()
    n = max(data[0x3B8:0x438]) + 1
    end = 0x43C + n * 1024
    extra = bytearray(data[:end]) + bytes(1024) + data[end:]
    extra[0x3B8 + 127] = n
    assert extra[0x3B6] < 128
    assert pw.identify(bytes(extra)) == "Ben Replay"
    mod = pw.convert(bytes(extra))
    lay = _layout(mod)
    assert lay["patterns"] == n + 1
    assert lay["sample_data"] == data[end:]
    assert lay["positions"][:lay["song_length"]] == data[0x3B8:0x3B8 + data[0x3B6]]


# ── playback in libopenmpt ─────────────────────────────────────────────────

def _render_params():
    out = []
    for (folder, path), ident in zip(POSITIVES, _IDS):
        marks = ()
        if path.name in SILENT_IN_SOURCE:
            marks = (pytest.mark.xfail(strict=True, reason="sample volume is 0 in the source"),)
        out.append(pytest.param(folder, path, id=ident, marks=marks))
    return out


@pytest.mark.parametrize("folder,path", _render_params())
def test_openmpt_plays_the_converted_module(folder, path, tmp_path):
    _need_data()
    exe = _openmpt()
    mod_path = tmp_path / "converted.mod"
    mod_path.write_bytes(pw.convert(path.read_bytes()))

    info = subprocess.run([exe, "--info", str(mod_path)], capture_output=True,
                          text=True, timeout=60)
    assert info.returncode == 0, info.stderr
    assert re.search(r"^Type\.*:\s*mod\b", info.stdout, re.M), info.stdout
    assert _duration_seconds(info.stdout) > 5.0

    render = subprocess.run(
        [exe, "--render", "--end-time", "20", "--no-float", "--output-type", "wav",
         "--force", "--banner", "0", "--no-progress", "--no-meters", str(mod_path)],
        capture_output=True, text=True, timeout=120)
    assert render.returncode == 0, render.stderr
    wav = Path(str(mod_path) + ".wav")
    assert wav.is_file()
    assert _peak_dbfs(wav) > -40.0


# ── no false positives ─────────────────────────────────────────────────────

def test_other_formats_are_not_identified():
    files = _negative_files()
    if not files:
        pytest.skip("no negative test data present")
    hits = []
    for p in files:
        data = p.read_bytes()
        name = pw.identify(data)
        if name:
            hits.append((str(p.relative_to(TESTDATA)), name))
        else:
            with pytest.raises(ValueError):
                pw.convert(data)
    assert hits == []


def test_converted_modules_are_not_identified():
    # The 27 converted modules are ordinary ProTracker M.K. modules (the mark
    # HCD Protector and Devils Replay also use).
    _need_data()
    for folder, path in POSITIVES:
        assert pw.identify(pw.convert(path.read_bytes())) is None, path.name


def _synthetic_mod(rng: random.Random) -> bytes:
    """A random but well-formed 31-sample ProTracker M.K. module."""
    out = bytearray(rng.choice([b"song", b"untitled", b""]).ljust(20, b"\0"))
    lengths = []
    for i in range(31):
        used = rng.random() < 0.6
        words = rng.randrange(1, 0x4000) if used else 0
        loop_start = rng.randrange(words) if used and rng.random() < 0.4 else 0
        loop_len = rng.randrange(1, words - loop_start + 1) if loop_start or (used and rng.random() < 0.3) else 1
        out += f"sample {i}".encode().ljust(22, b"\0")
        out += words.to_bytes(2, "big") + bytes((rng.randrange(16), rng.randrange(65)))
        out += loop_start.to_bytes(2, "big") + loop_len.to_bytes(2, "big")
        lengths.append(words * 2)
    patterns = rng.randrange(1, 20)
    song = rng.randrange(1, 128)
    order = [rng.randrange(patterns) for _ in range(song)]
    order[rng.randrange(song)] = patterns - 1
    out += bytes((song, 0x7F)) + bytes(order).ljust(128, b"\0") + b"M.K."
    for _ in range(patterns * 256):
        if rng.random() < 0.4:
            per, smp = rng.choice(PT[:36]), rng.randrange(1, 32)
        else:
            per, smp = 0, 0
        eff, arg = (rng.randrange(16), rng.randrange(256)) if rng.random() < 0.3 else (0, 0)
        out += bytes(((smp & 0xF0) | (per >> 8), per & 0xFF, ((smp & 0x0F) << 4) | eff, arg))
    out += rng.randbytes(sum(lengths))
    return bytes(out)


def test_synthetic_protracker_modules_are_not_identified():
    rng = random.Random(42)
    for _ in range(200):
        assert pw.identify(_synthetic_mod(rng)) is None


def test_rejects_empty_and_tiny_input():
    for data in (b"", b"\0", b"M.K.", bytes(1084), b"\xff" * 2000):
        assert pw.identify(data) is None
        with pytest.raises(ValueError):
            pw.convert(data)


def test_identify_is_cheap_on_large_input():
    # A NoiseTracker-Compressed-looking header in front of 8 MB of filler:
    # the pattern-mark scan must not walk the buffer byte by byte.
    data = bytearray(b"\x10\0\0\0" + bytes(0x170) + b"\x02\0\x00\x01" + bytes(0x7E) + b"PATT")
    assert data[0x1F6:0x1FA] == b"PATT"
    head = bytes(data)
    data += b"PA" * 4_000_000
    t = time.perf_counter()
    assert pw.identify(bytes(data)) is None
    assert time.perf_counter() - t < 2.0
    # 2 million "PATT" marks, all at odd offsets (which do not count).
    odd = head + b"x" + b"PATT" * 2_000_000
    t = time.perf_counter()
    assert pw.identify(odd) is None
    assert time.perf_counter() - t < 2.0


# ── robustness ─────────────────────────────────────────────────────────────

def _convert_or_reject(data: bytes) -> None:
    t = time.perf_counter()
    try:
        mod = pw.convert(data)
    except ValueError:
        pass
    else:
        assert mod[1080:1084] in (b"M.K.", b"CD81")
        assert len(mod) < 8_000_000
    assert time.perf_counter() - t < 5.0


@pytest.mark.parametrize("folder,path", POSITIVES, ids=_IDS)
def test_truncated_and_damaged_files(folder, path):
    _need_data()
    data = path.read_bytes()
    n = len(data)
    cuts = {n * i // 25 for i in range(25)} | {0, 1, 20, 1080, 1084, n - 1, n - 256, n - 300}
    for cut in sorted(c for c in cuts if 0 <= c < n):
        _convert_or_reject(data[:cut])
    rng = random.Random(f"{folder}/{path.name}")
    for _ in range(40):
        buf = bytearray(data)
        for _ in range(rng.randint(1, 8)):
            buf[rng.randrange(min(n, 4096))] = rng.randrange(256)
        _convert_or_reject(bytes(buf))


def test_random_buffers():
    rng = random.Random(1234)
    marks = [(0x438, b"Fuck"), (0x438, b"BNR!"), (0x438, b"GPMO"), (0x438, b"M.K."),
             (1080, b"PMD3"), (1080, b"PMd3"), (0x1F6, b"PATT"), (0, b"P22A"),
             (0, b"P30A"), (0, b"PERFSONG"), (0, b"PERFSNG2"), (0x92, b"GnPl"),
             (24, b"-GD-")]
    for _ in range(300):
        size = rng.choice([16, 200, 1100, 1500, 5000, 70000])
        buf = bytearray(rng.randbytes(size)) if rng.random() < 0.5 else bytearray(size)
        off, mark = rng.choice(marks)
        if off + len(mark) <= size:
            buf[off:off + len(mark)] = mark
        _convert_or_reject(bytes(buf))
