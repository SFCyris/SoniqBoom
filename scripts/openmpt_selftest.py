#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""openmpt123 / libopenmpt self-test: does this install render tracker
modules, Face The Music included?

The Docker build runs it (see the Dockerfile) so an image whose openmpt123
renders silence, or doesn't read Face The Music, can't ship.  On modules
synthesized here (no fixture files, nothing downloaded):

1. **Render.**  A ProTracker MOD (``M.K.``, 4 channels, one looped sample)
   and a Face The Music module (``FTMN`` v3, one embedded sample) are
   rendered to WAV with the same ``openmpt123`` arguments the server uses
   (``--batch --quiet --force -o <wav> -- <module>``); each WAV must hold at
   least ``MIN_SECONDS`` of audio with a peak level above ``MIN_PEAK``.

2. **Library.**  ``libopenmpt.so.0`` loaded by its SONAME (as the server
   loads it, through ctypes) must be ``MIN_VERSION`` or newer, list ``ftm``
   among its extensions (the server lists ``.ftm`` files only then), and be
   the same version ``openmpt123 --version`` reports.  With ``--prefix``,
   the library that load mapped (``/proc/self/maps``) and the
   ``libopenmpt.so.0`` of every listed binary or library (``ldd``) must lie
   under that directory, not be a copy elsewhere.

Exit status 0 = every check passed, 1 = a failure (listed on stderr).
Usage: ``python scripts/openmpt_selftest.py [--openmpt123 PATH] [--prefix DIR [--linked FILE ...]]``
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

MIN_VERSION = (0, 8)
MIN_SECONDS = 1.0
MIN_PEAK = 0.01            # of full scale


# ── Module writers ────────────────────────────────────────────────────────────

def _square(n: int = 256, lo: int = 100, hi: int = 156) -> bytes:
    """``n`` bytes of an 8-bit signed square wave, 16 samples a period."""
    return bytes(([lo] * 8 + [hi] * 8) * (n // 16))


def mod_module() -> bytes:
    """A 4-channel ProTracker module: one looped square-wave sample, a note
    (C-2, period 428) on each channel of row 0 and a pattern 64 rows long."""
    wave = _square(512)
    samples = [b"square".ljust(22, b"\0")
               + struct.pack(">HBBHH", len(wave) // 2, 0, 64, 0, len(wave) // 2)]
    samples += [b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)] * 30
    order = bytes([0] * 128)
    cell = bytes([0x00 | (428 >> 8), 428 & 0xFF, 0x10, 0x00])     # sample 1, no effect
    rows = [cell * 4] + [b"\0" * 16] * 63
    return (b"selftest".ljust(20, b"\0") + b"".join(samples)
            + bytes([1, 127]) + order + b"M.K." + b"".join(rows) + wave)


def ftm_module(title: bytes = b"Antom", artist: bytes = b"SSilk") -> bytes:
    """A Face The Music module (FTMN v3, one embedded sample, notes on
    channel 1) — the module tests/test_lha_reader.py builds."""
    wave = _square(4096)
    h = (b"FTMN" + bytes([3, 1]) + struct.pack(">HH", 4, 0x2000) + bytes([0, 0xFF, 63, 1, 4, 24])
         + title.ljust(32, b"\0") + artist.ljust(32, b"\0") + bytes([0, 0]))
    ev = bytes([0xA0, 0x4D, 0xF0, 0x03]) * 16
    chans = struct.pack(">hI", 0, len(ev)) + ev + b"".join(struct.pack(">hI", 0, 0)
                                                           for _ in range(7))
    return (h + b"Inst".ljust(30, b"\0") + b"\0\0" + chans
            + struct.pack(">HH", 0, len(wave) // 2) + wave)


# ── WAV reader (PCM or IEEE float, plain or WAVE_FORMAT_EXTENSIBLE) ──────────

def wav_level(path: str) -> tuple[float, float, float]:
    """(seconds, peak, rms) of a WAV file, levels as fractions of full scale."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt = None
    pcm = b""
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None:
        raise ValueError("no fmt chunk")
    tag, channels, rate, bits = fmt
    if tag == 3 and bits == 32:
        vals = struct.unpack(f"<{len(pcm) // 4}f", pcm[:len(pcm) // 4 * 4])
    elif tag == 1 and bits == 16:
        vals = [v / 32768 for v in struct.unpack(f"<{len(pcm) // 2}h", pcm[:len(pcm) // 2 * 2])]
    elif tag == 1 and bits == 32:
        vals = [v / 2147483648 for v in struct.unpack(f"<{len(pcm) // 4}i", pcm[:len(pcm) // 4 * 4])]
    else:
        raise ValueError(f"unsupported WAV format tag {tag} / {bits} bits")
    if not vals or not channels or not rate:
        return 0.0, 0.0, 0.0
    peak = max(abs(v) for v in vals)
    rms = math.sqrt(sum(v * v for v in vals) / len(vals))
    return len(vals) / channels / rate, peak, rms


# ── Checks ────────────────────────────────────────────────────────────────────

def render(openmpt123: str, name: str, module: bytes, work: str) -> list[str]:
    src = os.path.join(work, name)
    wav = os.path.join(work, name + ".wav")
    with open(src, "wb") as fh:
        fh.write(module)
    cmd = [openmpt123, "--batch", "--quiet", "--force", "-o", wav, "--", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return [f"{name}: openmpt123 timed out"]
    except OSError as e:
        return [f"{name}: openmpt123 doesn't run: {e}"]
    if r.returncode != 0 or not os.path.exists(wav):
        return [f"{name}: openmpt123 exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"]
    try:
        seconds, peak, rms = wav_level(wav)
    except (OSError, ValueError, struct.error) as e:
        return [f"{name}: unreadable WAV: {e}"]
    print(f"{name}: {seconds:.2f} s, peak {peak:.3f}, rms {rms:.3f}")
    errors = []
    if seconds < MIN_SECONDS:
        errors.append(f"{name}: only {seconds:.2f} s of audio")
    if peak < MIN_PEAK:
        errors.append(f"{name}: silent (peak {peak:.4f})")
    return errors


def _mapped_libopenmpt() -> list[str]:
    """Paths of the libopenmpt files mapped into this process (Linux)."""
    try:
        with open("/proc/self/maps") as fh:
            return sorted({os.path.realpath(l.split(None, 5)[5].strip()) for l in fh
                           if len(l.split(None, 5)) == 6 and "/libopenmpt.so" in l})
    except OSError:
        return []


def library(openmpt123: str, prefix: str | None = None) -> list[str]:
    try:
        lib = ctypes.CDLL("libopenmpt.so.0")
    except OSError as e:
        return [f"libopenmpt.so.0 doesn't load: {e}"]
    lib.openmpt_get_library_version.restype = ctypes.c_uint32
    lib.openmpt_get_string.restype = ctypes.c_void_p
    lib.openmpt_get_string.argtypes = [ctypes.c_char_p]
    lib.openmpt_get_supported_extensions.restype = ctypes.c_void_p
    lib.openmpt_free_string.argtypes = [ctypes.c_void_p]

    def _str(ptr) -> str:
        try:
            return ctypes.string_at(ptr).decode("ascii", "replace") if ptr else ""
        finally:
            lib.openmpt_free_string(ptr)

    v = lib.openmpt_get_library_version()
    major, minor = v >> 24, (v >> 16) & 0xFF
    version = _str(lib.openmpt_get_string(b"library_version"))
    exts = {e.strip().lower() for e in _str(lib.openmpt_get_supported_extensions()).split(";")}
    print(f"libopenmpt.so.0: {version}, {len(exts)} extensions")
    errors = []
    if prefix:
        mapped = _mapped_libopenmpt()
        print(f"libopenmpt.so.0 mapped from: {', '.join(mapped) or '(nothing found)'}")
        root = os.path.realpath(prefix) + os.sep
        if not mapped or any(not m.startswith(root) for m in mapped):
            errors.append(f"libopenmpt.so.0 loads from {mapped or '?'}, not from {prefix}")
    if (major, minor) < MIN_VERSION:
        errors.append(f"libopenmpt.so.0 is {version}, older than {MIN_VERSION[0]}.{MIN_VERSION[1]}")
    if "ftm" not in exts:
        errors.append("libopenmpt.so.0 doesn't list ftm (Face The Music)")
    try:
        out = subprocess.run([openmpt123, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return errors + [f"openmpt123 --version: {e}"]
    first = (out.stdout or out.stderr).splitlines()[:1]
    print(f"openmpt123 --version: {first[0] if first else '(nothing)'}")
    m = re.search(r"libopenmpt\s+(\S+)", first[0] if first else "")
    if not m or m.group(1).rstrip(",") != version:
        errors.append(f"openmpt123 reports libopenmpt {m.group(1) if m else '?'}, "
                      f"libopenmpt.so.0 is {version}")
    return errors


def linkage(prefix: str, files: list[str]) -> list[str]:
    errors = []
    prefix = os.path.realpath(prefix)
    for f in files:
        try:
            r = subprocess.run(["ldd", f], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"ldd {f}: {e}")
            continue
        m = re.search(r"^\s*libopenmpt\.so\.0 => (\S+)", r.stdout, re.M)
        if not m:
            errors.append(f"{f}: libopenmpt.so.0 not resolved: {r.stdout.strip()[-200:]}")
            continue
        got = os.path.realpath(m.group(1))
        print(f"{f}: libopenmpt.so.0 => {got}")
        if not got.startswith(prefix + os.sep):
            errors.append(f"{f} loads libopenmpt.so.0 from {got}, not from {prefix}")
        if "not found" in r.stdout:
            errors.append(f"{f}: unresolved libraries: "
                          + ", ".join(l.split()[0] for l in r.stdout.splitlines() if "not found" in l))
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--openmpt123", default=shutil.which("openmpt123") or "openmpt123")
    ap.add_argument("--prefix", help="libopenmpt.so.0 must load from under this directory")
    ap.add_argument("--linked", nargs="*", default=[],
                    help="files linking libopenmpt.so.0 to check with --prefix "
                         "(openmpt123 is always checked)")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)      # findings before the verdict
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as work:
        errors += render(a.openmpt123, "selftest.mod", mod_module(), work)
        errors += render(a.openmpt123, "selftest.ftm", ftm_module(), work)
    errors += library(a.openmpt123, a.prefix)
    if a.prefix:
        errors += linkage(a.prefix, [a.openmpt123] + a.linked)
    if errors:
        print("openmpt self-test FAILED:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    print("openmpt self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
