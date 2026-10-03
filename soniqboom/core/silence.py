"""Is a render audible?  Silence detection for rendered (emulated) audio.

A renderer can finish cleanly and still produce nothing to hear: an empty
default subsong, a module its engine can't really play, a console rip whose
emulator stays mute.  Such a render must never be served or cached as music.

The measure is the **peak-to-peak swing inside short blocks** (~6 ms), per
channel, not the plain sample peak: an emulated chip that plays nothing still
starts with a decaying DC offset (a silent C64 tune on reSIDfp's 8580 opens at
−1,530 and drifts to 0 over a second — a peak of 4.7 % full scale with no
sound in it), which a short block's peak-to-peak barely registers (measured:
at most 83 per 256 frames, 193 per 1,024, with 8580 + ``--digiboost`` and no
filter).  Real music swings far more: the quietest renders of the local test
corpus (internal/testdata + one Modland folder per format, 2026-10-02) peak at
0.0166 of full scale, every silent one at exactly 0.

``SILENCE_P2P`` (196 ≈ ±0.003 of full scale, −50 dBFS) sits between the two
with a wide margin both ways; it matches the playback cross-check harness's
"peak ≤ 0.003 is silent" verdict.  16-bit PCM and 32-bit float WAVs are judged
(openmpt123 writes float); anything else counts as audible — a heuristic must
never refuse a tune it can't read.
"""
from __future__ import annotations

import array
import sys
from pathlib import Path

# Peak-to-peak (int16 units) a block must exceed to count as sound.
SILENCE_P2P = 196
# Samples per channel in one block (~6 ms at 44.1 kHz): short enough that a
# decaying DC offset moves little inside one.
BLOCK_FRAMES = 256
# A render shorter than this is "empty" when a DEFAULT tune is chosen (a
# 0.05–0.3 s stub subsong is an init routine, not the music) — never a reason
# to refuse a tune the listener picked.
MIN_TUNE_SECONDS = 1.0

_PCM16, _FLOAT32 = "h", "f"
_THRESHOLD = {_PCM16: SILENCE_P2P, _FLOAT32: SILENCE_P2P / 32768.0}


def _samples(buf: bytes, kind: str) -> array.array:
    a = array.array(kind)
    a.frombytes(buf[: len(buf) - len(buf) % a.itemsize])
    if sys.byteorder == "big":           # WAV / renderer PCM is little-endian
        a.byteswap()
    return a


def _audible(a: array.array, channels: int, kind: str) -> bool:
    thr = _THRESHOLD[kind]
    ch = max(1, int(channels))
    step = BLOCK_FRAMES * ch
    for lo in range(0, len(a), step):
        block = a[lo:lo + step]
        for c in range(ch):
            part = block[c::ch] if ch > 1 else block
            if part and max(part) - min(part) > thr:
                return True
    return False


def pcm16_audible(buf: bytes, channels: int = 2) -> bool:
    """Does interleaved little-endian 16-bit PCM ``buf`` hold sound: a block
    of any channel whose peak-to-peak swing exceeds ``SILENCE_P2P``?"""
    return _audible(_samples(buf, _PCM16), channels, _PCM16)


class AudibilityMeter:
    """Feed a growing 16-bit LE PCM stream (``channels`` interleaved) and
    learn how long it is and whether it is audible.  Cheap once audible:
    later chunks are only counted."""

    def __init__(self, channels: int = 2, rate: int = 44100) -> None:
        self.channels = max(1, int(channels))
        self.rate = max(1, int(rate))
        self.frames = 0
        self.audible = False
        self._carry = b""
        self._frame_bytes = 2 * self.channels

    @property
    def seconds(self) -> float:
        return self.frames / self.rate

    def feed(self, chunk: bytes) -> bool:
        """Account ``chunk``; returns ``audible`` so far."""
        if not chunk:
            return self.audible
        data = self._carry + bytes(chunk) if self._carry else bytes(chunk)
        whole = len(data) - len(data) % self._frame_bytes
        self._carry = data[whole:]
        self.frames += whole // self._frame_bytes
        if not self.audible and whole:
            self.audible = pcm16_audible(data[:whole], self.channels)
        return self.audible


def wav_layout(path: "Path | str") -> "tuple[int, int, int, int, int] | None":
    """``(format_tag, channels, bits, data_offset, data_length)`` of a
    RIFF/WAVE file — ``WAVE_FORMAT_EXTENSIBLE`` reads as its sub-format's tag;
    a 0 / 0xFFFFFFFF data size (a streaming header) reads to the end of the
    file — or None when it isn't one."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, tag, channels, bits = 12, 0, 0, 0
    while pos + 8 <= len(head):
        cid = head[pos:pos + 4]
        length = int.from_bytes(head[pos + 4:pos + 8], "little")
        if cid == b"fmt " and pos + 24 <= len(head):
            tag = int.from_bytes(head[pos + 8:pos + 10], "little")
            channels = int.from_bytes(head[pos + 10:pos + 12], "little")
            bits = int.from_bytes(head[pos + 22:pos + 24], "little")
            if tag == 0xFFFE and length >= 40 and pos + 34 <= len(head):
                tag = int.from_bytes(head[pos + 32:pos + 34], "little")
        elif cid == b"data":
            off = pos + 8
            avail = max(0, size - off)
            if length in (0, 0xFFFFFFFF) or length > avail:
                length = avail
            return tag, channels, bits, off, length
        pos += 8 + length + (length & 1)
    return None


def wav_audible(path: "Path | str", chunk_bytes: int = 1 << 20) -> "bool | None":
    """Is the WAV at ``path`` audible?  True / False, or None when it can't be
    judged (not 16-bit PCM / 32-bit float, unreadable).  Reads in
    ``chunk_bytes`` pieces and stops at the first audible block, so music
    costs one chunk; only a silent file is read to its end (~25 ms per 30 s
    of 44.1 kHz audio).  Blocking — run it in a thread."""
    lay = wav_layout(path)
    if lay is None:
        return None
    tag, channels, bits, off, length = lay
    if tag == 1 and bits == 16:
        kind = _PCM16
    elif tag == 3 and bits == 32:
        kind = _FLOAT32
    else:
        return None
    if channels < 1:
        return None
    frame = (bits // 8) * channels
    step = max(BLOCK_FRAMES * frame, chunk_bytes - chunk_bytes % frame)
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            left = length
            while left > 0:
                buf = fh.read(min(step, left))
                if not buf:
                    break
                left -= len(buf)
                if _audible(_samples(buf, kind), channels, kind):
                    return True
    except OSError:
        return None
    return False
