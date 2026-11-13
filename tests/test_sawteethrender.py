# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sawteethrender (soniqboom/native/sawteethrender): Sawteeth modules — the
binary SWTD and the text SWTT format — rendered to WAV.  The binary is
``SB_SAWTEETHRENDER``, else ``sawteethrender`` on PATH, else built with its
build.sh; the modules are internal/testdata/nostalgicplayer/sawteeth."""
from __future__ import annotations

import array
import math
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "soniqboom/native/sawteethrender"
DATA = ROOT / "internal/testdata/nostalgicplayer/sawteeth"

# Lengths of NostalgicPlayer's own renders (the decoded MP3s at
# files.nostalgicplayer.dk/format/sawteeth/mp3/), and what the modules hold.
REFERENCE = {
    "64k.st": (69.068, "64k", "Jonas Norberg ©2001 LINEFEED", 5),
    "Infinity.st": (165.825, "Infinity", "biteoN", 8),
    "Second.st": (38.583, "Second", "Tigoro", 4),
    "Techno Loop.st": (15.412, "TechnoLoop", "Spruce / LF © 2000", 3),
}

# The exact rendered frame counts at 44100 Hz (ticks × samples-per-tick).
FRAMES = {"64k.st": 4416 * 689, "Infinity.st": 7310016, "Second.st": 1926 * 882,
          "Techno Loop.st": 768 * 882}

pytestmark = pytest.mark.skipif(not DATA.is_dir(), reason="no Sawteeth test modules")


def module_fields(channels: int) -> list:
    """A minimal version-1200 module: every channel plays one 6-tick part
    with a saw instrument, then loops; ("w", n) is a 16-bit field, ("s",
    text) a name, a plain int an 8-bit field."""
    f = [("w", 1200), ("w", 882), channels]
    for _ in range(channels):
        f += [255, 255, ("w", 1), ("w", 0), ("w", 0), 0, 0, 0]   # left right len lloop rloop | part transp damp
    f += [1, 6, 1, 1, 0, 48]                     # parts: sps len | ins eff note
    f += [1]                                     # instruments - 1
    f += [1, 0, 255, 1, 0, 200, 0, 0]            # filter pts (t, lev), amp pts (t, lev), mode, clip
    f += [0, 0, 0, 0, 0, 1, 1, 0, 1, 0]          # vibs vibd pwms pwmd res sps len loop | wform note
    f += [0, ("s", "T"), ("s", "A"), ("s", "p"), ("s", "i")]   # breakpoints, names
    return f


def binary_module(fields: list) -> bytes:
    out = bytearray(b"SWTD")
    for v in fields:
        if isinstance(v, tuple) and v[0] == "s":
            out += v[1].encode() + b"\0"
        elif isinstance(v, tuple):
            out += v[1].to_bytes(2, "big")
        else:
            out.append(v)
    return bytes(out)


def text_module(fields: list) -> bytes:
    lines = ["SWTT", "// a comment"]
    lines += [str(v[1]) if isinstance(v, tuple) else str(v) for v in fields]
    return ("\r\n".join(lines) + "\r\n").encode()


@pytest.fixture(scope="session")
def binary(tmp_path_factory) -> str:
    env = os.environ.get("SB_SAWTEETHRENDER")
    if env:
        return env
    found = shutil.which("sawteethrender")
    if found:
        return found
    if not shutil.which(os.environ.get("CC", "cc")):
        pytest.skip("no C compiler to build sawteethrender")
    dest = tmp_path_factory.mktemp("sawteethrender")
    subprocess.run(["sh", str(SRC / "build.sh"), str(dest)], check=True,
                   capture_output=True, timeout=300)
    return str(dest / "sawteethrender")


def run(binary: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], capture_output=True, timeout=timeout)


def info(binary: str, path: Path, *opts: str) -> dict[str, str]:
    p = run(binary, "-i", *opts, str(path))
    assert p.returncode == 0, p.stderr
    return dict(line.split("=", 1) for line in p.stdout.decode().splitlines())


def read_wav(path: Path) -> tuple[int, int, array.array]:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 2 and w.getsampwidth() == 2
        rate, n = w.getframerate(), w.getnframes()
        pcm = array.array("h")
        pcm.frombytes(w.readframes(n))
    if sys.byteorder == "big":
        pcm.byteswap()
    assert len(pcm) == 2 * n
    return rate, n, pcm


def rms(samples) -> float:
    return math.sqrt(sum(v * v for v in samples) / max(1, len(samples)))


def test_version(binary):
    p = run(binary, "--version")
    assert p.returncode == 0
    assert p.stdout == b"sawteethrender 1.1 (NostalgicPlayer c6eb0c5)\n"


@pytest.mark.parametrize("name", sorted(REFERENCE))
def test_info_fields(binary, name):
    ref_s, title, author, channels = REFERENCE[name]
    got = info(binary, DATA / name)
    assert set(got) == {"seconds", "title", "author", "channels"}
    assert got["title"] == title
    assert got["author"] == author
    assert got["channels"] == str(channels)
    assert abs(float(got["seconds"]) - ref_s) < 1.0


@pytest.mark.parametrize("name", sorted(REFERENCE))
def test_render_matches_reference_length_and_is_audible(binary, name, tmp_path):
    out = tmp_path / "out.wav"
    p = run(binary, str(DATA / name), str(out))
    assert p.returncode == 0, p.stderr
    rate, frames, pcm = read_wav(out)
    assert rate == 44100
    assert frames == FRAMES[name]
    seconds = frames / rate
    assert abs(seconds - REFERENCE[name][0]) < 1.0
    # -i reports exactly the rendered length
    assert float(info(binary, DATA / name)["seconds"]) == pytest.approx(seconds, abs=0.0005)
    peak = max(abs(v) for v in pcm)
    assert 20 * math.log10(max(peak, 1) / 32768) > -40


def test_info_length_equals_render_with_options(binary, tmp_path):
    out = tmp_path / "out.wav"
    opts = ("-r", "22050", "-l", "2", "-m", "70", "-f", "1000")
    path = DATA / "Techno Loop.st"
    assert run(binary, *opts, str(path), str(out)).returncode == 0
    rate, frames, _ = read_wav(out)
    assert rate == 22050
    assert float(info(binary, path, *opts)["seconds"]) == pytest.approx(frames / rate, abs=0.0005)
    _, _, pcm = read_wav(out)
    assert 20 * math.log10(max(max(abs(v) for v in pcm), 1) / 32768) > -40
    # a second pass through the loop makes it longer than one pass
    assert frames / rate > float(info(binary, path, "-r", "22050")["seconds"]) + 1


@pytest.mark.parametrize("name", ["64k.st", "Second.st"])
def test_render_is_deterministic(binary, name, tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    assert run(binary, str(DATA / name), str(a)).returncode == 0
    assert run(binary, str(DATA / name), str(b)).returncode == 0
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("channels", [1, 4, 12])
def test_synthesized_module_both_formats(binary, channels, tmp_path):
    fields = module_fields(channels)
    wavs = []
    for kind, data in (("bin", binary_module(fields)), ("txt", text_module(fields))):
        src = tmp_path / f"m.{kind}.st"
        src.write_bytes(data)
        out = tmp_path / f"m.{kind}.wav"
        p = run(binary, str(src), str(out))
        assert p.returncode == 0, p.stderr
        got = info(binary, src)
        assert (got["title"], got["author"], got["channels"]) == ("T", "A", str(channels))
        rate, frames, pcm = read_wav(out)
        # the end is reported on the 7th tick, which isn't played
        assert frames == 6 * 882
        assert max(abs(v) for v in pcm) > 1000
        wavs.append(out.read_bytes())
    assert wavs[0] == wavs[1]


def test_max_seconds_cuts_and_fades(binary, tmp_path):
    out = tmp_path / "cut.wav"
    p = run(binary, "-m", "5", "-f", "2000", str(DATA / "64k.st"), str(out))
    assert p.returncode == 0, p.stderr
    rate, frames, pcm = read_wav(out)
    assert frames == 5 * rate
    first, last = pcm[: 2 * rate], pcm[-2 * rate:]
    assert rms(last) < rms(first)
    assert max(abs(v) for v in pcm[-2 * 441:]) < max(abs(v) for v in first) / 10
    assert float(info(binary, DATA / "64k.st", "-m", "5", "-f", "2000")["seconds"]) == 5.0


def test_not_sawteeth_exits_1(binary, tmp_path):
    junk = tmp_path / "junk.st"
    junk.write_bytes(b"RIFF" + bytes(range(256)) * 8)
    out = tmp_path / "o.wav"
    assert run(binary, str(junk), str(out), timeout=10).returncode == 1
    assert run(binary, "-i", str(junk), timeout=10).returncode == 1
    empty = tmp_path / "empty.st"
    empty.write_bytes(b"")
    assert run(binary, "-i", str(empty), timeout=10).returncode == 1
    assert run(binary, "-i", str(tmp_path / "missing.st"), timeout=10).returncode == 1


@pytest.mark.parametrize("name", sorted(REFERENCE))
def test_truncated_exits_1_quickly(binary, name, tmp_path):
    data = (DATA / name).read_bytes()
    cut = tmp_path / "cut.st"
    for n in (3, 9, 12, 40, len(data) // 4, len(data) // 2, len(data) * 3 // 4):
        cut.write_bytes(data[:n])
        p = run(binary, str(cut), str(tmp_path / "o.wav"), timeout=10)
        assert p.returncode == 1, (n, p.stderr)
        assert run(binary, "-i", str(cut), timeout=10).returncode == 1


def test_unwritable_output_exits_1(binary, tmp_path):
    p = run(binary, str(DATA / "Second.st"), str(tmp_path / "no/such/dir/o.wav"))
    assert p.returncode == 1


@pytest.mark.parametrize("args", [
    [],
    ["--version", "x"],
    ["FILE"],
    ["-i", "FILE", "OUT"],
    ["FILE", "OUT", "EXTRA"],
    ["-x", "FILE", "OUT"],
    ["-r", "abc", "FILE", "OUT"],
    ["-r", "7999", "FILE", "OUT"],
    ["-r", "48001", "FILE", "OUT"],
    ["-m", "0", "FILE", "OUT"],
    ["-m", "7201", "FILE", "OUT"],
    ["-f", "-1", "FILE", "OUT"],
    ["-f", "60001", "FILE", "OUT"],
    ["-l", "0", "FILE", "OUT"],
    ["-l", "17", "FILE", "OUT"],
    ["-m"],
])
def test_usage_exits_2(binary, args, tmp_path):
    args = [str(DATA / "Second.st") if a == "FILE" else str(tmp_path / "o.wav") if a == "OUT" else a
            for a in args]
    assert run(binary, *args, timeout=10).returncode == 2
    assert not (tmp_path / "o.wav").exists()


@pytest.mark.parametrize("name", sorted(REFERENCE))
def test_vu_levels(binary, name, tmp_path):
    """-v writes a VUMR v1 file: 30 level frames a second covering the
    render (a last partial window included), a column per channel, a side
    per channel; the same from a render and from -i, and the WAV unchanged."""
    src = DATA / name
    plain, with_vu = tmp_path / "a.wav", tmp_path / "b.wav"
    vu_r, vu_i = tmp_path / "r.vu", tmp_path / "i.vu"
    assert run(binary, str(src), str(plain)).returncode == 0
    assert run(binary, "-v", str(vu_r), str(src), str(with_vu)).returncode == 0
    assert run(binary, "-i", "-v", str(vu_i), str(src)).returncode == 0
    assert plain.read_bytes() == with_vu.read_bytes()
    raw = vu_r.read_bytes()
    assert raw == vu_i.read_bytes()
    assert raw[:4] == b"VUMR" and raw[4] == 1
    channels = raw[5]
    rate = int.from_bytes(raw[8:12], "little")
    frames = int.from_bytes(raw[12:16], "little")
    _r, wav_frames, _pcm = read_wav(plain)
    assert channels == REFERENCE[name][3] and rate == 30
    assert frames == -(-wav_frames // (44100 // 30))
    assert len(raw) == 16 + channels + frames * channels
    assert all(side in (0, 1, 2) for side in raw[16:16 + channels])
    levels = raw[16 + channels:]
    assert max(levels) > 100


def test_vu_at_another_rate_and_cut(binary, tmp_path):
    src = DATA / "64k.st"
    vu = tmp_path / "x.vu"
    assert run(binary, "-i", "-v", str(vu), "-r", "22050", "-m", "5", str(src)).returncode == 0
    raw = vu.read_bytes()
    assert int.from_bytes(raw[12:16], "little") == 5 * 30


def test_vu_unwritable_exits_1(binary, tmp_path):
    p = run(binary, "-i", "-v", str(tmp_path / "no" / "x.vu"), str(DATA / "64k.st"))
    assert p.returncode == 1 and b"can't write" in p.stderr
