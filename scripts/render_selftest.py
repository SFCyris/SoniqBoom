# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render self-test for the AdLib (adplay), KSS (kssrender), Dreamcast DSF
(dsfrender), early DSIK (milkyrender), Musicline Editor (mlrender), X68000
MDX (mdxrender), GoatTracker (gt2reloc → sidplayfp), FamiTracker (Furnace),
libxmp (xmprender) and Sawteeth (sawteethrender) renderers.

Synthesizes a three-second id-Software IMF tune (an OPL2 voice, twelve
notes), a three-second MSX KSS (a Z80 init routine that starts one PSG
tone), a Dreamcast DSF (an ARM7 routine that starts one looped AICA voice),
a DSIK v1 module (one looped square-wave note), a Musicline Editor module
(one tune, one part of eight notes on a square-wave instrument), an MDX
song (one FM voice, eight notes on track A), a GoatTracker 2 song (eight
sawtooth notes), a FamiTracker module (eight pulse notes) and a ProTracker
module (eight notes on a square-wave sample — xmprender's check: libxmp
loads and mixes it) and a Sawteeth song (two channels, eight sawtooth
notes), renders
each as the server does — AdLib at the OPL chip's own rate piped into
ffmpeg's resampler when ffmpeg is found (``adlib_pipeline``, the same
command as ``stream._adlib_cmd``; a test keeps them equal); a GoatTracker
song packed by gt2reloc and played by sidplayfp — and fails unless every
WAV is audible.  Used as a Docker build gate (a renderer that starts but
can't read its format fails the build) and by the test suite.

Usage: ``python scripts/render_selftest.py [--adplay PATH] [--kssrender PATH]
[--dsfrender PATH] [--milkyrender PATH] [--mlrender PATH] [--mdxrender PATH]
[--gt2reloc PATH] [--sidplayfp PATH] [--sunvoxrender PATH] [--furnace PATH]
[--xmprender PATH] [--sawteethrender PATH] [--only NAME] [--examples DIR]`` (a renderer not given is looked up on PATH;
one not found fails).

sunvoxrender gets no synthesized project: without ``--examples`` it must
start (print its version — the SunVox Library loads).
``--examples DIR`` (the Docker builder stage, with the example songs their
own sources ship — never copied into the image) also renders DIR's
GoatTracker songs (``*.sng``), SunVox projects (``*.sunvox``) and Furnace
songs (``*.fur`` / ``*.ftm``), each of which must come out audible.
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
import zlib


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


def mdx_tune() -> bytes:
    """An MDX song: a title, no PDX, the offset table (the voice data, then
    tracks A–H and P — B to P share one end mark), track A setting tempo 200,
    voice 0, volume 15 and both speakers, then eight notes; voice 0 an
    all-carrier algorithm (7) with every operator at a fast attack."""
    voice = (bytes([0, 0x07, 0x0F]) + bytes([0x01] * 4) + bytes([0x08] * 4)
             + bytes([0x1F] * 4) + bytes(4) + bytes(4) + bytes([0x0F] * 4))
    track_a = bytes([0xFF, 200, 0xFD, 0, 0xFB, 0x0F, 0xFC, 3])
    for note in (0xA0, 0xA4, 0xA7, 0xAC) * 2:
        track_a += bytes([note, 0x2F])
    track_a += b"\xF1\x00\x00"                    # end of the song
    end = b"\xF1\x00\x00"
    a_off = 2 + 2 * 9
    end_off = a_off + len(track_a)
    voice_off = end_off + len(end)
    table = struct.pack(">H", voice_off) + struct.pack(">H", a_off) + struct.pack(">H", end_off) * 8
    return b"SoniqBoom self-test\r\n\x1a\x00" + table + track_a + end + voice


def kss_tune() -> bytes:
    """A KSCC file whose init routine starts PSG tone A (MSX ports A0/A1)."""
    load = 0x8000

    def out(reg: int, val: int) -> bytes:       # LD A,reg; OUT (A0),A; LD A,val; OUT (A1),A
        return bytes([0x3E, reg, 0xD3, 0xA0, 0x3E, val, 0xD3, 0xA1])
    init = out(7, 0x3E) + out(8, 15) + out(0, 0xFE) + out(1, 0) + b"\xC9"
    code = init + b"\xC9"                        # play: RET
    return (b"KSCC" + struct.pack("<HHHHBBBB", load, len(code), load, load + len(init),
                                  0, 0, 0, 0) + code)


def dsf_tune() -> bytes:
    """A Dreamcast DSF ('PSF\\x12', one section loaded at 0): an ARM7 routine
    that writes a table of AICA registers — master volume, then channel 0
    looping a 16-bit square wave (200 samples, 220.5 Hz) at full level — and
    keys the channel on."""
    def arm(*words: int) -> bytes:
        return b"".join(struct.pack("<I", w) for w in words)
    vectors = arm(0xEA00000E) + arm(*([0xE25EF004] * 7))      # b 0x40; subs pc, lr, #4
    code = arm(0xE28F2038,                  # 0x40 add r2, pc, #0x38  (r2 = the table, 0x80)
               0xE4920004,                  # 0x44 ldr r0, [r2], #4   (a register)
               0xE3500000,                  # 0x48 cmp r0, #0
               0x0A000002,                  # 0x4C beq 0x5C
               0xE4921004,                  # 0x50 ldr r1, [r2], #4   (its value)
               0xE5801000,                  # 0x54 str r1, [r0]
               0xEAFFFFF9,                  # 0x58 b 0x44
               0xEAFFFFFE)                  # 0x5C b .
    sample, loop_end, ch0 = 0x1000, 200, 0x00800000
    regs = [(0x00802800, 0x000F),                   # master volume
            (ch0 + 0x04, sample), (ch0 + 0x08, 0), (ch0 + 0x0C, loop_end),
            (ch0 + 0x10, 0x001F),                   # fastest attack, no decay
            (ch0 + 0x14, 0), (ch0 + 0x18, 0),       # native pitch (44.1 kHz)
            (ch0 + 0x24, 0x0F00),                   # direct send level 15, centre
            (ch0 + 0x28, 0x0020),                   # total level 0, filter off
            (ch0 + 0x00, 0xC200)]                   # 16-bit PCM, looped: key on
    table = arm(*[w for pair in regs for w in pair], 0)
    image = bytearray(sample + 2 * loop_end)
    image[:len(vectors)] = vectors
    image[0x40:0x40 + len(code)] = code
    image[0x80:0x80 + len(table)] = table
    for i in range(loop_end):
        struct.pack_into("<h", image, sample + 2 * i, 12000 if i < loop_end // 2 else -12000)
    exe = zlib.compress(struct.pack("<I", 0) + bytes(image))
    return (b"PSF\x12" + struct.pack("<III", 0, len(exe), zlib.crc32(exe)) + exe
            + b"[TAG]title=SoniqBoom self-test\n")


def dsik_tune() -> bytes:
    """A DSIK v1 module (``DSM\\x10``, the layout MilkyPlay's LoaderDSMv1
    reads): one channel, one 64-row pattern holding one note (C-4, full
    volume) of a looped 64-byte signed square wave at 6 ticks / 125 BPM —
    7.68 s, then the song ends where it would play row 0 again."""
    head = bytearray(176)
    head[0:4] = b"DSM\x10"
    head[4:4 + 9] = b"selftest\x00"
    head[37:44] = bytes([1, 1, 1, 1, 6, 125, 64])     # channels, orders, ins, pats, ticks, BPM, vol
    head[48] = 0                                       # order 0: pattern 0
    ins_at, pat_at, smp_at = 192, 240, 256
    ptrs = struct.pack("<HH", ins_at // 16, pat_at // 16)
    ins = (b"DSI\x10" + b"square".ljust(30, b"\x00") + b"\x00"
           + bytes([smp_at >> 20]) + struct.pack("<HHHHH", (smp_at >> 4) & 0xFFFF, 64, 0, 64, 8363)
           + bytes([64]))
    # Packed pattern: a flag byte (channel 0; note, instrument, volume follow)
    # XORed with 3 + its offset, then the three bytes as they are.
    packed = bytes([0xE0 ^ 3, 25, 1, 64])
    pat = struct.pack("<H", 2 + len(packed)) + packed
    smp = bytes([100] * 32 + [256 - 100] * 32)
    out = bytearray(head + ptrs)
    out += bytes(ins_at - len(out)) + ins
    out += bytes(pat_at - len(out)) + pat
    out += bytes(smp_at - len(out)) + smp
    return bytes(out)


def ml_tune(*, loop: bool = False) -> bytes:
    """A Musicline Editor module: one 4-channel tune whose first channel plays
    part 1 (eight notes of instrument 1, then a note off) and ends — or, with
    ``loop``, jumps back to its start — on a 256-byte square-wave sample."""
    def chunk(cid: bytes, payload: bytes) -> bytes:
        return cid + struct.pack(">I", len(payload)) + payload
    # Tune list words: part 1 (transpose 0), then END (0x0060) or JUMP to 0 (0x00A0).
    chdata = struct.pack(">HH", 0x0110, 0x00A0 if loop else 0x0060)
    tune = (b"SelfTest".ljust(32, b"\0") + struct.pack(">HBBHBB", 125, 2, 0, 64, 0, 4)
            + struct.pack(">IIII", len(chdata), 0, 0, 0) + chdata)
    rows = b"".join(b"\x01" + bytes([note, 1]) + b"\x00" * 7
                    for note in (0x19, 0x1B, 0x1D, 0x1E, 0x20, 0x22, 0x24, 0x25))
    part = struct.pack(">H", 1) + rows + b"\x01" + bytes([61, 0]) + b"\xff"
    inst = bytearray(206)
    inst[0:6] = b"Square"
    inst[32], inst[33] = 1, 2                       # sample 1, a 256-byte wave
    for off, val in ((38, 128), (44, 128), (52, 128), (56, 128), (58, 64)):
        struct.pack_into(">H", inst, off, val)      # lengths (words), volume 64
    wave = bytes([0x60] * 128 + [0xA0] * 128)
    head = bytearray(50)
    head[0:10] = b"square.256"
    head[33] = 2
    struct.pack_into(">IHIH", head, 34, 0x10000, 128, 0x10000, 128)
    smpl = (b"SMPL" + struct.pack(">I", len(head) + len(wave))
            + struct.pack(">IBB", len(wave), 0, 0) + bytes(head) + wave)
    return (b"MLEDMODL" + struct.pack(">I", 4) + b"\x12\x34\x56\x78"
            + chunk(b"VERS", b"\x01\x15" + b"1.15") + chunk(b"TUNE", tune)
            + chunk(b"PART", part) + chunk(b"INST", bytes(inst)) + smpl)


def goat_tune() -> bytes:
    """A GoatTracker 2 song (GTS5, the layout GoatTracker 2.77 saves): one
    subtune whose first channel plays eight sawtooth notes and a key off
    (channels 2 and 3 rest), one instrument whose wavetable sets the
    sawtooth at the note."""
    def text(s: str) -> bytes:
        return s.encode().ljust(32, b"\0")
    out = b"GTS5" + text("SoniqBoom selftest") + text("") + text("")
    out += bytes([1])                                   # subtunes
    for pat in (0, 1, 1):                               # order lists: [pattern, LOOP, restart 0]
        out += bytes([2, pat, 0xFF, 0])
    out += bytes([1])                                   # instruments: AD, SR, table pointers,
    out += bytes([0x0A, 0x9A, 1, 0, 0, 0, 0, 2, 0x09]) + b"tone".ljust(16, b"\0")  # vib, gate, 1st wave
    out += bytes([2, 0x21, 0xFF, 0x00, 0x00])           # wavetable: saw + gate at the note; stop
    out += bytes([0, 0, 0])                             # pulse, filter, speed tables: empty
    rest = bytes([0xBD, 0, 0, 0])
    notes = (0x90, 0x94, 0x97, 0x9C, 0x97, 0x94, 0x90, 0xBE)   # C-4 E-4 G-4 C-5 G-4 E-4 C-4, off
    pats = (b"".join(bytes([n, 1, 0, 0]) + rest * 3 for n in notes), rest * 32)
    out += bytes([len(pats)])
    for p in pats:                                      # rows + the end row
        out += bytes([len(p) // 4 + 1]) + p + bytes([0xFF, 0, 0, 0])
    return out


def ftm_tune() -> bytes:
    """A FamiTracker module (0x0440, the blocks FamiTracker 0.4.6 writes):
    the 2A03 alone, one track of one 64-row frame, one 2A03 instrument
    without sequences, eight notes and a halt on the first pulse channel."""
    def i32(v: int) -> bytes:
        return struct.pack("<i", v)

    def block(name: str, version: int, data: bytes) -> bytes:
        return name.encode().ljust(16, b"\0") + i32(version) + i32(len(data)) + data
    chans = 5
    # expansion chips, channels, NTSC, default engine speed, new vibrato,
    # highlights, speed/tempo split point
    params = bytes([0]) + i32(chans) + i32(0) + i32(0) + i32(1) + i32(4) + i32(16) + i32(32)
    info = b"SoniqBoom selftest".ljust(32, b"\0") + bytes(64)
    header = bytes([0]) + b"\0" + b"".join(bytes([ch, 0]) for ch in range(chans))
    instrument = (i32(1) + i32(0) + bytes([1]) + i32(5) + bytes(2 * 5)   # 2A03, no sequences,
                  + bytes(3 * 96) + i32(4) + b"tone")                     # no DPCM; its name
    frames = i32(1) + i32(6) + i32(150) + i32(64) + bytes(chans)          # speed 6, tempo 150
    notes = ((1, 4), (5, 4), (8, 4), (1, 5), (8, 4), (5, 4), (1, 4), (13, 0))  # C E G C' G E C, halt
    rows = b"".join(i32(n * 8) + bytes([note, octave, 0 if note != 13 else 0x40, 0x10, 0, 0])
                    for n, (note, octave) in enumerate(notes))
    patterns = i32(0) + i32(0) + i32(0) + i32(len(notes)) + rows
    return (b"FamiTracker Module" + i32(0x0440)
            + block("PARAMS", 6, params) + block("INFO", 1, info)
            + block("HEADER", 3, header) + block("INSTRUMENTS", 6, instrument)
            + block("SEQUENCES", 6, i32(0)) + block("FRAMES", 3, frames)
            + block("PATTERNS", 4, patterns) + block("DPCM SAMPLES", 1, bytes([0])) + b"END")


OPL_NATIVE_RATE = 49716
RESAMPLE_SOXR = "aresample=44100:resampler=soxr:precision=28:dither_method=triangular_hp"
RESAMPLE_SWR = "aresample=44100:filter_size=64:phase_shift=10:dither_method=triangular_hp"


def sawteeth_tune() -> bytes:
    """A Sawteeth song (binary "SWTD", version 1200): two channels play one
    part — eight sawtooth notes, six ticks each, at 882 samples a tick
    (about a second) — once."""
    notes = (48, 52, 55, 60, 55, 52, 48, 43)
    out = bytearray(b"SWTD")
    out += (1200).to_bytes(2, "big") + (882).to_bytes(2, "big")    # version, samples per tick
    out.append(2)                                                   # channels
    for pan in ((255, 128), (128, 255)):
        out += bytes(pan) + (1).to_bytes(2, "big")                  # left, right; 1 step
        out += (0).to_bytes(2, "big") + (0).to_bytes(2, "big")      # loop start, right loop
        out += bytes((0, 0, 0))                                     # part 0, transpose, damp
    out += bytes((1, 6, len(notes)))                                # 1 part: 6 ticks a step
    for n in notes:
        out += bytes((1, 0, n))                                     # instrument 1, no effect
    out.append(1)                                                   # instruments − 1
    out += bytes((1, 0, 255, 1, 0, 200, 0, 0))                     # filter, amp envelope, mode, clip
    out += bytes((0, 0, 0, 0, 0, 1, 1, 0, 0x81, 0))                # vibrato … one saw step
    #                                         (0x81: saw, relative to the part's note)
    out.append(0)                                                   # breakpoints
    for text in (b"SoniqBoom self-test", b"SoniqBoom", b"p", b"i"):
        out += text + b"\0"                                        # name, author, part, instrument
    return bytes(out)


def mod_tune() -> bytes:
    """A ProTracker ("M.K.") module: one pattern, eight notes (C-2 … C-3) on
    channel 1, eight rows apart, played by a looped square-wave sample."""
    out = b"selftest".ljust(20, b"\0")
    out += b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 16, 0, 64, 0, 16)
    out += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    out += bytes([1, 127]) + bytes(128) + b"M.K."
    rows = []
    for row in range(64):
        cells = [b"\0\0\0\0"] * 4
        if row % 8 == 0:
            period = (428, 381, 339, 320, 285, 254, 226, 214)[row // 8]
            cells[0] = bytes([0x00 | (period >> 8), period & 0xFF, 0x10, 0x00])  # sample 1
        rows.append(b"".join(cells))
    out += b"".join(rows)
    return out + bytes([0x7F] * 16 + [0x80] * 16)


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


# A synthesized tune's notes peak far above this, the near-silence a
# renderer that drops them writes far below (verified: Furnace's start-up
# click -32.6 dBFS, sidplayfp on a song without notes -44.7 dBFS; the
# synthesized songs themselves -10 to -12 dBFS).
NOTES_DB = -25.0


def render(name: str, cmd: list[str], out: str, min_db: float = -60.0) -> str | None:
    """Run ``cmd``; None when ``out`` is an audible WAV (peak at least
    ``min_db`` dBFS), else why not."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{name}: {exc}"
    if r.returncode != 0:
        return f"{name}: exit {r.returncode}: {r.stderr.decode(errors='replace')[:200]}"
    if not os.path.exists(out):
        return f"{name}: no WAV written"
    db = peak_dbfs(out)
    if db < min_db:
        return f"{name}: rendered silence (peak {db:.1f} dBFS, {os.path.getsize(out)} bytes)"
    print(f"{name}: ok — peak {db:.1f} dBFS, {os.path.getsize(out)} bytes")
    return None


def starts(name: str, cmd: list[str], want: bytes, env: "dict | None" = None) -> str | None:
    """Run ``cmd``; None when its output names ``want``, else why not."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{name}: {exc}"
    if want not in r.stdout + r.stderr:
        return f"{name}: exit {r.returncode}, {want.decode()!r} not printed: " \
               f"{(r.stderr or r.stdout).decode(errors='replace')[:200]}"
    print(f"{name}: ok — starts")
    return None


def goat_convert(gt2reloc: str, song: str, work: str) -> "tuple[str | None, str]":
    """gt2reloc ``song`` into a PSID in ``work`` under short names (its
    file-name buffer is 60 bytes — ``stream._goat_workdir``): (error or None,
    the .sid's path)."""
    sng, sid = os.path.join(work, "s.sng"), os.path.join(work, "s.sid")
    shutil.copyfile(song, sng)
    try:
        r = subprocess.run([gt2reloc, sng, sid], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"gt2reloc: {exc}", sid
    if r.returncode != 0 or not os.path.exists(sid):
        return f"gt2reloc: exit {r.returncode}, no SID written", sid
    with open(sid, "rb") as fh:
        if fh.read(4) != b"PSID":
            return "gt2reloc: its output is no PSID", sid
    return None, sid


def main() -> int:
    names = ("adplay", "kssrender", "dsfrender", "milkyrender", "mlrender", "mdxrender",
             "gt2reloc", "sunvoxrender", "furnace", "xmprender", "sawteethrender")
    ap = argparse.ArgumentParser()
    for name in names + ("sidplayfp",):
        ap.add_argument(f"--{name}")
    ap.add_argument("--only", choices=names)
    ap.add_argument("--examples", help="a folder of example songs to render")
    a = ap.parse_args()
    found = {name: getattr(a, name) or shutil.which(name) for name in names + ("sidplayfp",)}
    found["furnace"] = a.furnace or shutil.which("furnace-headless") or shutil.which("furnace")
    errors = []

    def wanted(name: str) -> bool:
        """Is ``name`` tested this run (``--only``), and found?  (A wanted
        renderer that isn't found is an error.)"""
        if a.only not in (None, name):
            return False
        if not found[name]:
            errors.append(f"{name}: not found")
            return False
        return True

    def one(label: str, src_name: str, tune: bytes, args: list[str]) -> None:
        """Write ``tune`` to ``src_name`` and render it: ``args`` + the tune +
        its own output WAV (named by the tune's extension)."""
        src = os.path.join(tmp, src_name)
        with open(src, "wb") as fh:
            fh.write(tune)
        out = os.path.join(tmp, src_name.split(".")[-1] + ".wav")    # (kss.wav, dsf.wav …)
        err = render(label, args + [src, out], out)
        if err:
            errors.append(err)

    with tempfile.TemporaryDirectory(prefix="sb_render_selftest_") as tmp:
        if wanted("adplay"):
            adplay = found["adplay"]
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
        if wanted("kssrender"):
            one("kssrender (KSS)", "selftest.kss", kss_tune(),
                [found["kssrender"], "-m", "3", "-f", "0"])
        if wanted("dsfrender"):
            one("dsfrender (DSF)", "selftest.dsf", dsf_tune(),
                [found["dsfrender"], "-m", "3", "-f", "0"])
        if wanted("milkyrender"):
            one("milkyrender (DSIK)", "selftest.dsm", dsik_tune(),
                [found["milkyrender"], "-m", "3", "-f", "0"])
        if wanted("mlrender"):
            one("mlrender (Musicline Editor)", "selftest.ml", ml_tune(),
                [found["mlrender"], "-m", "3", "-f", "0", "-p", "0.3"])
        if wanted("mdxrender"):
            one("mdxrender (MDX)", "selftest.mdx", mdx_tune(),
                [found["mdxrender"], "-m", "3", "-f", "0"])
        if wanted("xmprender"):
            src = os.path.join(tmp, "selftest.mod")
            with open(src, "wb") as fh:
                fh.write(mod_tune())
            out = os.path.join(tmp, "mod.wav")
            err = render("xmprender (ProTracker)",
                         [found["xmprender"], "-m", "5", "-f", "0", src, out], out, min_db=NOTES_DB)
            if err:
                errors.append(err)
        if wanted("sawteethrender"):
            src = os.path.join(tmp, "selftest.st")
            with open(src, "wb") as fh:
                fh.write(sawteeth_tune())
            out = os.path.join(tmp, "st.wav")
            err = render("sawteethrender (Sawteeth)",
                         [found["sawteethrender"], "-m", "5", "-f", "0", src, out], out,
                         min_db=NOTES_DB)
            if err:
                errors.append(err)
        examples = sorted(os.listdir(a.examples)) if a.examples else []

        def goat(label: str, song: str) -> None:
            """gt2reloc ``song`` and play the SID tune with sidplayfp, as the
            server does (``stream._render_goattracker``)."""
            if not found["sidplayfp"]:
                errors.append(f"{label}: sidplayfp not found (GoatTracker songs play through it)")
                return
            work = tempfile.mkdtemp(prefix="sbgt", dir="/tmp" if os.path.isdir("/tmp") else tmp)
            try:
                err, sid = goat_convert(found["gt2reloc"], song, work)
                if err:
                    errors.append(f"{label}: {err}")
                    return
                out = os.path.join(tmp, "goat.wav")
                err = render(label, [found["sidplayfp"], "-s", "-f44100", "-t5", f"-w{out}", sid], out,
                             min_db=NOTES_DB)
                if err:
                    errors.append(err)
            finally:
                shutil.rmtree(work, ignore_errors=True)

        if wanted("gt2reloc"):
            src = os.path.join(tmp, "selftest.sng")
            with open(src, "wb") as fh:
                fh.write(goat_tune())
            goat("gt2reloc → sidplayfp (GoatTracker)", src)
            songs = [f for f in examples if f.lower().endswith(".sng")]
            for f in songs:
                goat(f"gt2reloc → sidplayfp ({f})", os.path.join(a.examples, f))
            if a.examples and not songs:
                errors.append("gt2reloc: no example song (*.sng) to render")
        if wanted("sunvoxrender"):
            songs = [f for f in examples if f.lower().endswith(".sunvox")]
            if not a.examples:
                err = starts("sunvoxrender", [found["sunvoxrender"], "--version"], b"SunVox Library")
                if err:
                    errors.append(err)
            for f in songs:
                out = os.path.join(tmp, "sunvox.wav")
                err = render(f"sunvoxrender ({f})", [found["sunvoxrender"], "-m", "5", "-f", "0",
                                                     os.path.join(a.examples, f), out], out)
                if err:
                    errors.append(err)
            if a.examples and not songs:
                errors.append("sunvoxrender: no example project (*.sunvox) to render")
        if wanted("furnace"):
            home = tempfile.mkdtemp(prefix="home", dir=tmp)     # (Furnace writes a config there)
            env = {**{k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"},
                   "HOME": home}                         # (XDG_CONFIG_HOME would come first)

            def furnace(label: str, song: str) -> None:
                out = os.path.join(tmp, "furnace.wav")
                if os.path.exists(out):
                    os.unlink(out)
                try:
                    subprocess.run([found["furnace"], "-output", out, "-loops", "1", song],
                                   capture_output=True, timeout=120, env=env)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"{label}: {exc}")
                    return
                if not os.path.exists(out):
                    errors.append(f"{label}: no WAV written")
                    return
                db = peak_dbfs(out)
                if db < NOTES_DB:
                    errors.append(f"{label}: rendered silence (peak {db:.1f} dBFS)")
                else:
                    print(f"{label}: ok — peak {db:.1f} dBFS, {os.path.getsize(out)} bytes")

            src = os.path.join(tmp, "selftest.ftm")
            with open(src, "wb") as fh:
                fh.write(ftm_tune())
            furnace("furnace (FamiTracker)", src)
            songs = [f for f in examples if f.lower().endswith((".fur", ".ftm"))]
            for f in songs:
                furnace(f"furnace ({f})", os.path.join(a.examples, f))
            if a.examples and not songs:
                errors.append("furnace: no example song (*.fur / *.ftm) to render")
    for e in errors:
        print("RENDER SELF-TEST FAILED — " + e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
