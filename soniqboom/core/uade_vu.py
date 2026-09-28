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

The dump is read in fixed-size chunks (two passes: count the audio frames,
then fold each chunk into per-window peaks), never mapped or copied whole:
memory stays at a few chunk buffers whatever the tune's length, and no
single C call runs long enough to hold the GIL (and the server's event loop,
when this runs in a worker thread) for more than a few milliseconds.

``parse_stream`` is the same parse in ONE pass over a stream — a FIFO uade
writes the dump into — so the dump never touches the disk (the server's VU
pass uses it wherever ``os.mkfifo`` exists).  The window span is only known
at EOF, so it keeps each channel's peak per ``_BIN`` audio frames and takes
the windows from those at the end.

The result serializes through ``openmpt_vu.serialize_vumr`` — the same VUMR
v1 sidecar the tracker pipeline writes — so the frontend's per-channel VU
meters light up for TFMX / Future Composer / SidMon / AHX / … with zero
frontend changes.  Paula panning is hardwired LRRL (channels 0+3 left,
1+2 right).
"""

from __future__ import annotations

import logging
from itertools import compress
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


# Frames per read: 64 K frames = 768 KB.  Every C-level step below works on
# one chunk, so its GIL hold is bounded (measured on a 773 MB dump of a 315 s
# tune: longest event-loop gap 10.6 ms vs 1.1 s for a whole-dump parse,
# peak RSS 29 MB vs 1.47 GB, same total time), and memory is O(chunk).
_CHUNK_FRAMES = 1 << 16


def _read_chunks(fh, total: int, buf: bytearray):
    """Yield successive views of up to ``_CHUNK_FRAMES`` whole frames read
    into ``buf`` (reused; each view is valid until the next one)."""
    left = total
    while left:
        n = min(left, _CHUNK_FRAMES)
        mv = memoryview(buf)[:n * _FRAME]
        got = fh.readinto(mv)
        if got != n * _FRAME:
            mv.release()
            raise ValueError("uade dump shrank while it was being read")
        try:
            yield mv
        finally:
            mv.release()
        left -= n


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

    Two chunked passes: the first counts audio frames (which fixes each
    window's span), the second folds every chunk's audio-only per-channel
    magnitudes into per-window raw peaks — a window may straddle two chunks,
    so its partial peak carries over.  The song-wide normalisation is applied
    to the small per-window result at the end (the LUT is monotonic, so the
    max of normalised values equals the normalised max).

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
    total = (size - _HEADER) // _FRAME
    if total == 0:
        return None
    buf = bytearray(min(total, _CHUNK_FRAMES) * _FRAME)
    try:
        with open(dump_path, "rb", buffering=0) as fh:
            if fh.read(_HEADER) != DUMP_MAGIC:
                log.debug("uade dump magic mismatch in %s", dump_path)
                return None

            # Pass 1 — audio-frame count from the tdelta high byte.  Event
            # frames (register writes) are NOT sparse — players hammer
            # volume/period registers every tick, ~half the frames in a real
            # dump — so they are COMPRESSED OUT, not zeroed in place: only
            # then are the remaining audio frames uniformly spaced in time
            # (exactly one per output sample), letting frame index stand in
            # for the clock with no cumsum.
            n_audio = 0
            for mv in _read_chunks(fh, total, buf):
                n_audio += bytes(mv[0::_FRAME]).translate(_KEEP).count(1)
            if n_audio <= 0:
                return None

            n_win = max(1, int(duration_s * vu_rate_hz))
            step = n_audio / n_win
            mono = bytearray(n_win * 4)       # raw per-window peaks for now
            peak = 0
            k = 0                             # current window
            a0 = 0                            # audio index of the chunk's first frame
            fh.seek(_HEADER)
            for mv in _read_chunks(fh, total, buf):
                keep = bytes(mv[0::_FRAME]).translate(_KEEP)
                # Per-channel HIGH-byte magnitude columns, audio frames only.
                cols = [bytes(compress(bytes(mv[4 + 2 * ch::_FRAME]), keep)).translate(_ABS2)
                        for ch in range(4)]
                m = len(cols[0])
                if not m:
                    continue
                a1 = a0 + m
                peak = max(peak, max(max(c) for c in cols))
                while k < n_win:
                    s_ = int(k * step)
                    e_ = max(s_ + 1, int((k + 1) * step))
                    if s_ >= a1:
                        break
                    lo = max(s_, a0) - a0
                    hi = min(e_, a1) - a0
                    if hi > lo:
                        base = k * 4
                        for ch in range(4):
                            v = max(cols[ch][lo:hi])
                            if v > mono[base + ch]:
                                mono[base + ch] = v
                    if e_ > a1:
                        break                 # the window continues in the next chunk
                    k += 1
                a0 = a1
    except (OSError, ValueError) as exc:
        log.debug("uade dump parse failed for %s: %s", dump_path, exc)
        return None

    # Paula channels carry ~1/4 of full scale each (they sum into the mix),
    # so raw peaks sit far below 255 and the bars would never leave the
    # bottom of the meter.  Normalize by the song-wide peak across all
    # channels — preserves the balance BETWEEN channels while using the
    # meter's full range (mirrors libopenmpt's per-channel [0,1] VU
    # convention).
    if peak <= 0:
        return None
    if peak < 255:
        lut = bytes(min(255, (v * 255) // peak) for v in range(256))
        mono = mono.translate(lut)
    return VUResult(
        channels=4,
        sample_rate=vu_rate_hz,
        frames=n_win,
        mono=bytes(mono),
        pan=_PAULA_PAN,
    )


# Audio frames per stored peak in the one-pass parser: ~11 MB of peaks for a
# 420 s tune at uade's ~100 kHz internal rate.
_BIN = 16


def _read_full(fh, n: int) -> bytes:
    """Up to ``n`` bytes from ``fh`` — fewer only at EOF (a pipe hands out
    short reads)."""
    parts = []
    while n > 0:
        b = fh.read(n)
        if not b:
            break
        parts.append(b)
        n -= len(b)
    return b"".join(parts)


def parse_stream(
    fh,
    duration_s: float,
    *,
    vu_rate_hz: int = DEFAULT_VU_RATE_HZ,
    stats: dict | None = None,
) -> VUResult | None:
    """``parse_dump`` in ONE pass over a binary stream (a FIFO uade is
    writing ``--write-audio`` into), so the dump — ~2.6 MB per tune-second —
    is never stored.

    Each window's span depends on the total audio-frame count, known only at
    EOF, so every channel's magnitudes are first reduced to the peak of each
    ``_BIN`` consecutive audio frames; the windows are taken from those peaks
    at the end.  A window then spans the bins its frames touch, so its value
    is never below ``parse_dump``'s and exceeds it only where a louder frame
    lies within ``_BIN`` frames of the window's edge (well under a
    millisecond of audio).  Normalisation is ``parse_dump``'s.

    Always reads to EOF — after bad magic or a parse error too — so the
    writer never blocks on a full pipe.  ``stats["bytes"]`` receives the
    number of bytes read.  None on bad magic, no audio frames or an error.
    """
    total = 0
    ok = duration_s > 0
    head = _read_full(fh, _HEADER)
    total += len(head)
    if head != DUMP_MAGIC:
        ok = False
    tail = b""
    pend = [b""] * 4
    bins = [bytearray() for _ in range(4)]
    n_audio = 0
    while True:
        chunk = _read_full(fh, _CHUNK_FRAMES * _FRAME)
        if not chunk:
            break
        total += len(chunk)
        if not ok:
            continue                          # keep draining: never stall the writer
        try:
            data = tail + chunk if tail else chunk
            whole = len(data) - len(data) % _FRAME
            tail = data[whole:]
            if not whole:
                continue
            with memoryview(data) as mv_all, mv_all[:whole] as mv:
                keep = bytes(mv[0::_FRAME]).translate(_KEEP)
                for ch in range(4):
                    col = bytes(compress(bytes(mv[4 + 2 * ch::_FRAME]), keep)).translate(_ABS2)
                    if ch == 0:
                        n_audio += len(col)
                    c = pend[ch] + col if pend[ch] else col
                    # zip over one iterator groups _BIN frames and drops the
                    # partial group at the end — carried to the next chunk.
                    bins[ch] += bytes(map(max, zip(*[iter(c)] * _BIN)))
                    pend[ch] = c[len(c) - len(c) % _BIN:]
        except Exception as exc:              # keep reading to EOF, report None
            log.debug("uade dump stream parse failed: %s", exc)
            ok = False
    if stats is not None:
        stats["bytes"] = total
    if not ok or n_audio <= 0:
        return None
    for ch in range(4):
        if pend[ch]:
            bins[ch].append(max(pend[ch]))
    peak = max(max(b) if b else 0 for b in bins)
    if peak <= 0:
        return None
    n_win = max(1, int(duration_s * vu_rate_hz))
    step = n_audio / n_win
    mono = bytearray(n_win * 4)
    for k in range(n_win):
        s_ = int(k * step)
        e_ = max(s_ + 1, int((k + 1) * step))
        b0, b1 = s_ // _BIN, (e_ - 1) // _BIN + 1
        base = k * 4
        for ch in range(4):
            seg = bins[ch][b0:b1]
            if seg:
                mono[base + ch] = max(seg)
    if peak < 255:
        lut = bytes(min(255, (v * 255) // peak) for v in range(256))
        mono = mono.translate(lut)
    return VUResult(
        channels=4,
        sample_rate=vu_rate_hz,
        frames=n_win,
        mono=bytes(mono),
        pan=_PAULA_PAN,
    )
