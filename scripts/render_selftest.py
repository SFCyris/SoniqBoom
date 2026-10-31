# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render self-test for the AdLib (adplay) and KSS (kssrender) renderers.

Synthesizes a three-second id-Software IMF tune (an OPL2 voice, twelve
notes) and a three-second MSX KSS (a Z80 init routine that starts one PSG
tone), renders each as the server does — AdLib at the OPL chip's own rate
piped into ffmpeg's resampler when ffmpeg is found (``adlib_pipeline``, the
same command as ``stream._adlib_cmd``; a test keeps them equal) — and fails
unless every WAV is audible.  Used as a Docker build gate (a renderer that
starts but can't read its format fails the build) and by the test suite.

Usage: ``python scripts/render_selftest.py [--adplay PATH] [--kssrender PATH]
[--only adplay|kssrender]`` (a renderer not given is looked up on PATH; one
not found fails).
"""
from __future__ import annotations

import argparse
import array
import os
import shutil
import struct
import subprocess
import sys
import tempfile


def imf_tune() -> bytes:
    """A type-0 IMF (560 Hz ticks; it starts with an empty event, the form
    every AdPlug reads — 2.3.3 refuses a type-1 file this short): one carrier
    voice playing 12 notes."""
    ev = []

    def w(reg: int, val: int, delay: int = 0) -> None:
        ev.append(struct.pack("<BBH", reg, val, delay))
    for reg, val in ((0x01, 0x20), (0x20, 0x01), (0x23, 0x01), (0x40, 0x3F), (0x43, 0x00),
                     (0x60, 0xF0), (0x63, 0xF0), (0x80, 0x0F), (0x83, 0x0F),
                     (0xE0, 0), (0xE3, 0), (0xC0, 0x01)):
        w(reg, val)
    for fnum in (0x158, 0x181, 0x1B0, 0x1CA, 0x202, 0x241) * 2:
        w(0xA0, fnum & 0xFF)
        w(0xB0, 0x20 | (4 << 2) | (fnum >> 8), 112)       # key on, 0.2 s
        w(0xB0, (4 << 2) | (fnum >> 8), 28)
    return b"\x00\x00\x00\x00" + b"".join(ev)


def kss_tune() -> bytes:
    """A KSCC file whose init routine starts PSG tone A (MSX ports A0/A1)."""
    load = 0x8000

    def out(reg: int, val: int) -> bytes:       # LD A,reg; OUT (A0),A; LD A,val; OUT (A1),A
        return bytes([0x3E, reg, 0xD3, 0xA0, 0x3E, val, 0xD3, 0xA1])
    init = out(7, 0x3E) + out(8, 15) + out(0, 0xFE) + out(1, 0) + b"\xC9"
    code = init + b"\xC9"                        # play: RET
    return (b"KSCC" + struct.pack("<HHHHBBBB", load, len(code), load, load + len(init),
                                  0, 0, 0, 0) + code)


OPL_NATIVE_RATE = 49716
RESAMPLE_SOXR = "aresample=44100:resampler=soxr:precision=28:dither_method=triangular_hp"
RESAMPLE_SWR = "aresample=44100:filter_size=64:phase_shift=10:dither_method=triangular_hp"


def has_soxr(ffmpeg: str) -> bool:
    """Does this ffmpeg carry the SoX resampler?"""
    try:
        return subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
             f"sine=f=1000:sample_rate={OPL_NATIVE_RATE}:d=0.1",
             "-af", "aresample=44100:resampler=soxr", "-f", "null", "-"],
            capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# (stream.ADPLAY_TAIL_FILTER — the sizes adplay writes after the audio into a
# pipe are left out; a test keeps the two equal)
ADPLAY_TAIL_FILTER = (
    "import os,struct\n"
    "def w(x):\n"
    " while x: x=x[os.write(1,x):]\n"
    "n=0;h=b''\n"
    "while True:\n"
    " b=os.read(0,65536)\n"
    " if not b: break\n"
    " h+=b;n+=len(b)\n"
    " if len(h)>8: w(h[:-8]);h=h[-8:]\n"
    "d=n-52\n"
    "if not(len(h)==8 and d>=0 and h in (struct.pack('<II',d,d+36),"
    "struct.pack('<II',d+36,d))): w(h)\n")


def adlib_pipeline(adplay: str, ffmpeg: str, src: str, out: str, *, soxr: bool,
                   subsong: int = 0, emulator: str = "nuked") -> list:
    """The server's AdLib command (``stream._adlib_cmd``): adplay at the OPL
    rate writing its WAV to fd 3, through ``ADPLAY_TAIL_FILTER``, piped into
    ffmpeg's resampler."""
    import shlex
    play = [adplay, "-q", "-O", "disk", "-e", emulator, "--stereo"]
    if subsong > 0:
        play += ["-s", str(subsong)]
    resample = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "wav", "-ignore_length", "1", "-i", "pipe:0",
                "-af", RESAMPLE_SOXR if soxr else RESAMPLE_SWR, "-c:a", "pcm_s16le",
                "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                "-f", "wav", out]
    q = shlex.quote
    script = (" ".join(q(a) for a in
                       play + ["-d", "/dev/fd/3", "-f", str(OPL_NATIVE_RATE), src])
              + " 3>&1 >/dev/null 2>&1 | " + q(sys.executable) + " -I -c "
              + q(ADPLAY_TAIL_FILTER) + " | exec " + " ".join(q(a) for a in resample))
    return ["/bin/sh", "-c", script]


def peak_dbfs(path: str) -> float:
    """Peak of a 16-bit PCM WAV in dBFS (-inf when silent or unreadable)."""
    import math
    with open(path, "rb") as fh:
        data = fh.read()
    i, off, size = 12, None, 0
    while i + 8 <= len(data):
        cid, n = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"data":
            off, size = i + 8, n
            break
        i += 8 + n + (n & 1)
    if off is None:
        return float("-inf")
    pcm = data[off:off + min(size, len(data) - off)]
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    peak = max((abs(x) for x in a), default=0)
    return 20 * math.log10(peak / 32768) if peak else float("-inf")


def render(name: str, cmd: list[str], out: str) -> str | None:
    """Run ``cmd``; None when ``out`` is an audible WAV, else why not."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{name}: {exc}"
    if r.returncode != 0:
        return f"{name}: exit {r.returncode}: {r.stderr.decode(errors='replace')[:200]}"
    if not os.path.exists(out):
        return f"{name}: no WAV written"
    db = peak_dbfs(out)
    if db < -60:
        return f"{name}: rendered silence (peak {db:.1f} dBFS, {os.path.getsize(out)} bytes)"
    print(f"{name}: ok — peak {db:.1f} dBFS, {os.path.getsize(out)} bytes")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adplay")
    ap.add_argument("--kssrender")
    ap.add_argument("--only", choices=("adplay", "kssrender"))
    a = ap.parse_args()
    adplay = a.adplay or shutil.which("adplay")
    kss = a.kssrender or shutil.which("kssrender")
    errors = []
    with tempfile.TemporaryDirectory(prefix="sb_render_selftest_") as tmp:
        if a.only == "kssrender":
            pass
        elif not adplay:
            errors.append("adplay: not found")
        else:
            src = os.path.join(tmp, "selftest.imf")
            with open(src, "wb") as fh:
                fh.write(imf_tune())
            out = os.path.join(tmp, "imf.wav")
            err = render("adplay (IMF)", [adplay, "-o", "-q", "-O", "disk", "-e", "nuked",
                                          "--stereo", "-f", "44100", "-d", out, src], out)
            if err:
                errors.append(err)
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:                       # the server's pipeline (stream._adlib_cmd)
                out = os.path.join(tmp, "imf_native.wav")
                err = render("adplay → ffmpeg (IMF, OPL rate)",
                             adlib_pipeline(adplay, ffmpeg, src, out, soxr=has_soxr(ffmpeg)), out)
                if err:
                    errors.append(err)
        if a.only == "adplay":
            pass
        elif not kss:
            errors.append("kssrender: not found")
        else:
            src = os.path.join(tmp, "selftest.kss")
            with open(src, "wb") as fh:
                fh.write(kss_tune())
            out = os.path.join(tmp, "kss.wav")
            err = render("kssrender (KSS)", [kss, "-m", "3", "-f", "0", src, out], out)
            if err:
                errors.append(err)
    for e in errors:
        print("RENDER SELF-TEST FAILED — " + e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
