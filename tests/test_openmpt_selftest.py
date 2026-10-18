# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/openmpt_selftest.py — the Docker build's tracker-render gate."""
import importlib.util
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("openmpt_selftest", ROOT / "scripts" / "openmpt_selftest.py")
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


def _wav(tag: int, bits: int, frames: bytes, channels: int = 2, rate: int = 48000,
         extensible: bool = False) -> bytes:
    align = channels * bits // 8
    if extensible:
        fmt = struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * align, align, bits)
        fmt += struct.pack("<HHI", 22, bits, 3) + struct.pack("<H", tag) + b"\0" * 14
    else:
        fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"PEAK" + struct.pack("<I", 4) + b"\0" * 4        # a chunk to skip
    body += b"data" + struct.pack("<I", len(frames)) + frames
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize("tag,bits,pack,extensible", [
    (3, 32, lambda v: struct.pack("<f", v), False),
    (3, 32, lambda v: struct.pack("<f", v), True),
    (1, 16, lambda v: struct.pack("<h", int(v * 32767)), False),
])
def test_wav_level_reads_float_and_pcm(tmp_path, tag, bits, pack, extensible):
    vals = [0.5, -0.5] * 48000                    # 1 s of stereo at ±0.5
    p = tmp_path / "x.wav"
    p.write_bytes(_wav(tag, bits, b"".join(pack(v) for v in vals), extensible=extensible))
    seconds, peak, rms = st.wav_level(str(p))
    assert seconds == pytest.approx(1.0)
    assert peak == pytest.approx(0.5, abs=1e-3) and rms == pytest.approx(0.5, abs=1e-3)


def test_silence_reads_as_silence(tmp_path):
    p = tmp_path / "s.wav"
    p.write_bytes(_wav(3, 32, b"\0" * 8 * 48000))
    assert st.wav_level(str(p)) == (pytest.approx(1.0), 0.0, 0.0)


def test_the_gate_renders_the_suites_face_the_music_module():
    # The module the gate renders is the one the archive tests build.
    from tests import test_lha_reader, test_online_empty_packages
    assert st.ftm_module() == test_lha_reader._ftm() == test_online_empty_packages._ftm()
    assert st.mod_module()[1080:1084] == b"M.K."


def test_the_dockerfile_runs_the_gate_on_the_runtime_image():
    df = (ROOT / "Dockerfile").read_text()
    runtime = df.split("# ═══════════════════════════════ Stage 2: runtime")[1]
    assert "source=scripts/openmpt_selftest.py" in runtime
    assert re.search(r"python -B -I /tmp/openmpt_selftest\.py --prefix /usr/local/lib", runtime)
    assert "--linked /usr/lib/*-linux-gnu/libavformat.so" in runtime
    # Debian's openmpt123 is not installed next to the source build.
    apt = runtime.split("RUN apt-get update && apt-get install", 1)[1].split("rm -rf", 1)[0]
    assert "openmpt123" not in apt


def _openmpt123_version() -> tuple:
    exe = shutil.which("openmpt123")
    if not exe:
        return ()
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout
    m = re.search(r"libopenmpt (\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else ()


@pytest.mark.skipif(_openmpt123_version() < st.MIN_VERSION,
                    reason="no openmpt123 with libopenmpt 0.8 or newer")
def test_the_gates_modules_render_audibly(tmp_path):
    exe = shutil.which("openmpt123")
    assert st.render(exe, "t.mod", st.mod_module(), str(tmp_path)) == []
    assert st.render(exe, "t.ftm", st.ftm_module(), str(tmp_path)) == []
    # …and a module that plays nothing fails it.
    assert any("silent" in e or "only" in e
               for e in st.render(exe, "z.mod", st.mod_module()[:1084] + b"\0" * 1024, str(tmp_path)))


def test_the_docs_name_the_pinned_libopenmpt_version():
    pin = re.search(r"^ARG LIBOPENMPT_VERSION=(\S+)$", (ROOT / "Dockerfile").read_text(), re.M).group(1)
    deploy = (ROOT / "DEPLOY.md").read_text()
    assert f"libopenmpt / openmpt123 {pin}," in deploy
    assert f"the\n  image's libopenmpt {pin};" in deploy
    assert f"builds libopenmpt and openmpt123 {pin} from the upstream release" in (
        ROOT / "THIRD-PARTY-LICENSES.md").read_text()
