"""Per-voice VU extraction for uade-rendered Amiga formats.

uade123 3.05's ``--write-audio <fname>`` dumps the emulator's four Paula
channel outputs — POST-volume, i.e. exactly what the chip plays — as a
stream of fixed 12-byte frames after a 16-byte magic header (source:
uade ``src/write_audio.c`` / ``src/include/write_audio_ext.h``)::

    header:  "uade_osc_0\\x00\\xec\\x17\\x31\\x03\\x09"   (16 bytes)
    frame:   int32 BE tdelta | union { int16 BE output[4]     (audio frame)
                                     | int8 ch, int8 evt, u16 (event frame) }

The MSB of ``tdelta`` marks Paula *event* frames (register writes); audio
frames arrive once per output sample, so — after compressing event frames
out — frame INDEX ≈ time.  Events are NOT sparse (measured ~53% of frames
on real dumps; players hammer volume/period registers every tick), which
is exactly why they are removed rather than zeroed in place.

This parser is deliberately stdlib-only (no numpy in the dependency set) and
leans on C-speed primitives: ``bytes`` stride slicing pulls each channel's
HIGH byte column out of the dump, ``bytes.translate`` maps signed high bytes
to 8-bit magnitudes, and ``max()`` over byte slices takes per-window peaks.
VUMR amplitudes are uint8 anyway, so high-byte precision is exact for the
sidecar format.  A 345 s tune (≈850 MB dump) parses in a few seconds.

The result serializes through ``openmpt_vu.serialize_vumr`` — the same VUMR
v1 sidecar the tracker pipeline writes — so the frontend's per-channel VU
meters light up for TFMX / Future Composer / SidMon / AHX / … with zero
frontend changes.  Paula panning is hardwired LRRL (channels 0+3 left,
1+2 right).
"""

from __future__ import annotations

import logging
import mmap
from pathlib import Path

from soniqboom.core.openmpt_vu import VUResult, DEFAULT_VU_RATE_HZ

log = logging.getLogger(__name__)

DUMP_MAGIC = b"uade_osc_0\x00\xec\x17\x31\x03\x09"
_FRAME = 12
_HEADER = 16

# Amiga Paula hardware panning: ch0 left, ch1 right, ch2 right, ch3 left.
_PAULA_PAN = bytes((1, 2, 2, 1))

# signed high byte (two's complement) -> uint8 magnitude 0..255.
# |int16| >> 7 == |high_byte| * 2 (±1 LSB) — exact enough for uint8 VU.
_ABS2 = bytes(min(255, (b if b < 128 else 256 - b) * 2) for b in range(256))
# tdelta high byte -> 0x01 for AUDIO frames (MSB clear), 0x00 for events —
# used as an itertools.compress selector.
_KEEP = bytes((0 if b >= 128 else 1) for b in range(256))


def parse_dump(
    dump_path: Path,
    duration_s: float,
    *,
    vu_rate_hz: int = DEFAULT_VU_RATE_HZ,
) -> VUResult | None:
    """Parse a ``--write-audio`` dump into a 4-channel VUResult, or None.

    ``duration_s`` is the rendered WAV's real length — the dump's audio
    frames arrive at uade's INTERNAL mixing rate (tune-dependent; ~79-112 kHz
    measured on real dumps, never the 44.1 kHz output rate), so the time
    axis self-calibrates against the WAV instead of assuming any clock.

    Best-effort: any structural surprise (bad magic, truncated file, zero
    audio frames) returns None — a missing VU sidecar just means the
    frontend falls back to the FFT spectrum, never a failed play.
    """
    if duration_s <= 0:
        return None
    try:
        size = dump_path.stat().st_size
    except OSError:
        return None
    if size < _HEADER + _FRAME:
        return None
    try:
        with open(dump_path, "rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if mm[:_HEADER] != DUMP_MAGIC:
                    log.debug("uade dump magic mismatch in %s", dump_path)
                    return None
                total = (len(mm) - _HEADER) // _FRAME
                if total == 0:
                    return None
                end = _HEADER + total * _FRAME
                body = memoryview(mm)[_HEADER:end]
                try:
                    # Copy the needed byte columns out (C-speed stride
                    # slicing); everything after this point works on copies.
                    tdelta_hi = bytes(body[0::_FRAME])
                    raw_cols = [bytes(body[4 + 2 * ch::_FRAME])
                                for ch in range(4)]
                finally:
                    body.release()   # mmap can't close with live exports

                # Audio-frame mask from the tdelta high byte.  Event frames
                # (register writes) are NOT sparse — players hammer volume/
                # period registers every tick, ~half the frames in a real
                # dump — so they must be COMPRESSED OUT, not zeroed in
                # place: only then are the remaining audio frames uniformly
                # spaced in time (exactly one per output sample), letting
                # frame index stand in for the clock with no cumsum.
                from itertools import compress
                keep01 = tdelta_hi.translate(_KEEP)
                n_audio = keep01.count(1)
                if n_audio <= 0:
                    return None

                # Per-channel HIGH-byte magnitude columns, audio frames only.
                cols = [
                    bytes(compress(raw, keep01)).translate(_ABS2)
                    for raw in raw_cols
                ]

                # Paula channels carry ~1/4 of full scale each (they sum into
                # the mix), so raw peaks sit far below 255 and the bars would
                # never leave the bottom of the meter.  Normalize by the
                # song-wide peak across all channels — preserves the balance
                # BETWEEN channels while using the meter's full range
                # (mirrors libopenmpt's per-channel [0,1] VU convention).
                peak = max(max(c) for c in cols)
                if peak <= 0:
                    return None
                if peak < 255:
                    lut = bytes(min(255, (v * 255) // peak) for v in range(256))
                    cols = [c.translate(lut) for c in cols]

                n_win = max(1, int(duration_s * vu_rate_hz))
                step = n_audio / n_win
                mono = bytearray(n_win * 4)
                for k in range(n_win):
                    s = int(k * step)
                    e = max(s + 1, int((k + 1) * step))
                    base = k * 4
                    for ch in range(4):
                        mono[base + ch] = max(cols[ch][s:e], default=0)

                return VUResult(
                    channels=4,
                    sample_rate=vu_rate_hz,
                    frames=n_win,
                    mono=bytes(mono),
                    pan=_PAULA_PAN,
                )
    except (OSError, ValueError) as exc:
        log.debug("uade dump parse failed for %s: %s", dump_path, exc)
        return None
