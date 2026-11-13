# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conversion of fifteen Amiga "packed module" formats into ProTracker modules.

``identify(data)`` returns the name of the packed format the bytes are in (a
key of ``FORMATS``) or ``None``; ``convert(data)`` rebuilds the module as a
standard 31-sample ProTracker module (20-byte title, 31 sample headers, song
length, restart byte, 128-entry position list, the ``M.K.`` mark at offset
1080, 64-row patterns, then the sample data).  The one exception is an
8-channel Module-Patterncompressor module ("PMd3"), which is written with the
``CD81`` mark and 8-channel (2048-byte) patterns.  ``convert`` raises
``ValueError`` for data ``identify`` rejects and for data that is
inconsistent or truncated before the end of its pattern data or by more than
256 bytes of sample data.  Only the Python standard library is used.

The identification checks and the conversions are a port of the ProWizard
converter in NostalgicPlayer (``Source/Agents/ModuleConverters/
ProWizardConverter``: the format classes named in each converter's docstring
and the bases ``ProWizardConverterWorkerBase``,
``ProWizardConverterWorker31SamplesBase`` and ``ProWizardForPcBase``):

    Copyright (c) 2023 Thomas Neumann

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    THE SOFTWARE.

Reading follows NostalgicPlayer's ``ModuleStream``: a read past the end of the
data returns zeros and sets an end-of-stream flag that the conversion checks
at the same points NostalgicPlayer does.  Where NostalgicPlayer would throw (an
index outside the period table, a pattern row outside the pattern, more than
31 samples) ``convert`` raises ``ValueError``.  Where this port differs:

* a position list longer than 128 entries is rejected (NostalgicPlayer would
  write it into the header and misalign the rest of the module);
* when the pattern count comes from list entries past the song end (the
  formats with a 128-entry list read all of it), NostalgicPlayer writes those
  entries as zeros, so a player counts fewer patterns than were written and
  reads the sample data from the wrong place; here the last unused list entry
  names the highest pattern written;
* sample data missing at the end of the file (NostalgicPlayer tolerates up to
  256 missing bytes) is padded with zeros, so the module is as long as its
  sample headers say;
* the NoiseTracker Compressed pattern-mark scan stops at the end of the data
  (NostalgicPlayer's scan can loop forever there; its result is "not this
  format" either way);
* a sample's finetune is written as its low nibble, as ProTracker players
  read it (NostalgicPlayer writes the whole byte: a The Player header's
  offset / 74 can exceed 15, and libxmp then refuses the module).
"""
from __future__ import annotations

import struct

__all__ = ["FORMATS", "identify", "convert"]

# Packed format name -> lower-case file-name tokens (extension or Amiga prefix).
FORMATS: dict[str, tuple[str, ...]] = {
    "NoiseTracker Compressed": ("ntc",),
    "Ben Replay": ("ben",),
    "Binary Packer": ("bnr",),
    "Devils Replay": ("dev",),
    "GnoiPacker": ("gnoi",),
    "GnuPlayer": ("gnu",),
    "Gnu Protected Module": ("gpmo",),
    "HCD Protector": ("hcd",),
    "Module-Patterncompressor": ("pmd3",),
    "Newtron Packer 1.0": ("nw1",),
    "Newtron Packer 2.0": ("nw2",),
    "Perfect Song 1": ("pf1",),
    "Perfect Song 2": ("pf2",),
    "The Player 2.2A": ("p22a",),
    "The Player 3.0A": ("p30a",),
}

_MAX_MISSING = 256          # MaxNumberOfMissingBytes
_ZERO = bytes(128)

# ProTracker periods as (high byte, low byte); entries 36-44 repeat 0x71.
_PERIODS = (
    (0x3, 0x58), (0x3, 0x28), (0x2, 0xfa), (0x2, 0xd0), (0x2, 0xa6), (0x2, 0x80),
    (0x2, 0x5c), (0x2, 0x3a), (0x2, 0x1a), (0x1, 0xfc), (0x1, 0xe0), (0x1, 0xc5),
    (0x1, 0xac), (0x1, 0x94), (0x1, 0x7d), (0x1, 0x68), (0x1, 0x53), (0x1, 0x40),
    (0x1, 0x2e), (0x1, 0x1d), (0x1, 0x0d), (0x0, 0xfe), (0x0, 0xf0), (0x0, 0xe2),
    (0x0, 0xd6), (0x0, 0xca), (0x0, 0xbe), (0x0, 0xb4), (0x0, 0xaa), (0x0, 0xa0),
    (0x0, 0x97), (0x0, 0x8f), (0x0, 0x87), (0x0, 0x7f), (0x0, 0x78), (0x0, 0x71),
) + ((0x0, 0x71),) * 9

_U32 = 0xFFFFFFFF


def _period(index: int) -> tuple[int, int]:
    if not 0 <= index < len(_PERIODS):
        raise ValueError("note outside the period table")
    return _PERIODS[index]


def _i32(value: int) -> int:
    """``value`` truncated to a signed 32-bit int (a C# ``(int)`` cast)."""
    value &= _U32
    return value - 0x100000000 if value & 0x80000000 else value


class _Reader:
    """Bounds-safe big-endian reader with NostalgicPlayer ``ModuleStream``
    semantics: reads past the end return zeros, ``eos`` reflects the last
    read or seek, and seeks clamp to ``[0, length]``."""

    __slots__ = ("data", "length", "pos", "eos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.length = len(data)
        self.pos = 0
        self.eos = False

    def seek(self, offset: int, current: bool = False) -> None:
        p = self.pos + offset if current else offset
        if p < 0:
            p = 0
        self.eos = p > self.length
        self.pos = min(p, self.length)

    def read(self, count: int) -> bytes:
        """``count`` bytes, zero-padded where the data ends."""
        if count <= 0:
            self.eos = False
            return b""
        p = self.pos
        chunk = self.data[p:p + count]
        self.pos = p + len(chunk)
        self.eos = len(chunk) < count
        if self.eos:
            chunk += bytes(count - len(chunk))
        return chunk

    def u8(self) -> int:
        p = self.pos
        if p < self.length:
            self.pos = p + 1
            self.eos = False
            return self.data[p]
        self.eos = True
        return 0

    def s8(self) -> int:
        v = self.u8()
        return v - 256 if v & 0x80 else v

    def u16(self) -> int:
        p = self.pos
        if p + 2 <= self.length:
            self.pos = p + 2
            self.eos = False
            return (self.data[p] << 8) | self.data[p + 1]
        self.pos = self.length
        self.eos = True
        return 0

    def u32(self) -> int:
        p = self.pos
        if p + 4 <= self.length:
            self.pos = p + 4
            self.eos = False
            return int.from_bytes(self.data[p:p + 4], "big")
        self.pos = self.length
        self.eos = True
        return 0

    def mark(self, count: int = 4) -> bytes:
        """``ReadMark``: ``count`` bytes with trailing NULs removed, or
        ``b""`` when fewer than ``count`` bytes are left."""
        p = self.pos
        chunk = self.data[p:p + count]
        self.pos = p + len(chunk)
        self.eos = len(chunk) < count
        if self.eos:
            return b""
        return chunk.rstrip(b"\0")


class _Sample:
    __slots__ = ("name", "length", "loop_start", "loop_length", "volume", "fine_tune")

    def __init__(self, name, length, loop_start, loop_length, volume, fine_tune):
        self.name = name
        self.length = length
        self.loop_start = loop_start
        self.loop_length = loop_length
        self.volume = volume
        self.fine_tune = fine_tune


def _test_sample(size: int, loop_start: int, loop_size: int, volume: int, fine_tune: int) -> bool:
    """``ProWizardForPcBase.TestSample``."""
    if loop_start > size:
        return False
    if loop_size > size + 2:
        return False
    if loop_start + loop_size > size + 2:
        return False
    if loop_start != 0 and loop_size < 2:
        return False
    if (loop_start != 0 or loop_size > 2) and size == 0:
        return False
    if volume > 0x40 or fine_tune > 0x0F:
        return False
    return True


class _Format:
    """``ProWizardConverterWorker31SamplesBase``: one instance per file;
    ``check`` must run before the conversion steps (they reuse its state)."""

    name = ""
    mark = b"M.K."

    def __init__(self) -> None:
        self.number_of_patterns = 0
        self.sample_lengths = [0] * 31

    # -- steps a format overrides -------------------------------------------
    def check(self, r: _Reader) -> bool:
        raise NotImplementedError

    def prepare(self, r: _Reader) -> bool:
        return True

    def module_name(self, r: _Reader) -> bytes | None:
        return None

    def samples(self, r: _Reader):
        raise NotImplementedError

    def position_list(self, r: _Reader) -> bytes:
        raise NotImplementedError

    def restart_position(self, r: _Reader) -> int:
        return 0x7F

    def patterns(self, r: _Reader):
        raise NotImplementedError

    def write_samples(self, r: _Reader, out: bytearray) -> bool:
        raise NotImplementedError

    # -- shared helpers ------------------------------------------------------
    def find_highest(self, r: _Reader, length: int, offset: int | None = None) -> int:
        """``FindHighestPatternNumber`` over ``length`` bytes at ``offset``
        (or the current position); sets and returns the pattern count."""
        if offset is not None:
            r.seek(offset)
        return self.highest_of(r.read(length))

    def highest_of(self, positions) -> int:
        self.number_of_patterns = (max(positions) if positions else 0) + 1
        return self.number_of_patterns

    def build_position_list(self, count: int, key) -> bytes:
        """``BuildPositionList``: one pattern per distinct key, in order."""
        used: list = []
        out = bytearray(count)
        for i in range(count):
            k = key(i)
            try:
                out[i] = used.index(k) & 0xFF
            except ValueError:
                out[i] = len(used) & 0xFF
                used.append(k)
        self.number_of_patterns = len(used)
        return bytes(out)

    def save_marks(self, r: _Reader, out: bytearray) -> bool:
        """``SaveMarksForAllSamples``: copy each sample's bytes from the
        current position on."""
        for length in self.sample_lengths:
            if length:
                if not self.copy_sample(r, out, length):
                    return False
        return True

    @staticmethod
    def copy_sample(r: _Reader, out: bytearray, length: int) -> bool:
        if r.length - r.pos < length - _MAX_MISSING:
            return False
        chunk = r.data[r.pos:r.pos + length]
        out += chunk
        if len(chunk) < length:
            out += bytes(length - len(chunk))
        r.seek(length, current=True)
        return True

    # -- the conversion (ProWizardConverterWorker31SamplesBase.Convert) ------
    def convert(self, r: _Reader) -> bytes:
        self.sample_lengths = [0] * 31
        if not self.prepare(r):
            raise ValueError(f"{self.name}: bad header")

        out = bytearray()
        name = self.module_name(r)
        out += (name or _ZERO)[:20].ljust(20, b"\0")

        count = 0
        for s in self.samples(r):
            if r.eos:
                raise ValueError(f"{self.name}: sample information truncated")
            if count >= 31:
                raise ValueError(f"{self.name}: more than 31 samples")
            out += (s.name or _ZERO)[:22].ljust(22, b"\0")
            # (finetune: the low nibble ProTracker players read — a source
            # header out of range, The Player's offset / 74 above 15, would
            # make libxmp refuse the whole module)
            out += struct.pack(">HBBHH", s.length & 0xFFFF, s.fine_tune & 0x0F,
                               s.volume & 0xFF, s.loop_start & 0xFFFF,
                               s.loop_length & 0xFFFF)
            self.sample_lengths[count] = (s.length & 0xFFFF) * 2
            count += 1
        if count == 0:
            raise ValueError(f"{self.name}: no samples")
        for _ in range(count, 31):
            out += bytes(28) + b"\x00\x01"

        positions = self.position_list(r)
        if not positions or r.eos:
            raise ValueError(f"{self.name}: bad position list")
        if len(positions) > 128:
            raise ValueError(f"{self.name}: more than 128 positions")
        out.append(len(positions))
        out.append(self.restart_position(r) & 0xFF)
        list_offset = len(out)
        out += bytes(positions).ljust(128, b"\0")
        out += self.mark

        written = 0
        for pattern in self.patterns(r):
            if pattern is None or r.eos:
                raise ValueError(f"{self.name}: pattern data truncated")
            out += pattern
            written += 1

        # Players count a module's patterns from all 128 list entries.  When
        # more patterns are written than the song's positions reach (the
        # count came from list entries past the song end), the last unused
        # entry names the highest one so the sample data is found after it.
        if written > max(positions) + 1:
            if len(positions) >= 128 or written > 256:
                raise ValueError(f"{self.name}: pattern count does not fit the position list")
            out[list_offset + 127] = written - 1

        if not self.write_samples(r, out):
            raise ValueError(f"{self.name}: sample data truncated")
        return bytes(out)


# ---------------------------------------------------------------------------
# Formats with a 31-sample ProTracker-like header (ProWizard for PC formats)
# ---------------------------------------------------------------------------

class _PcHeaderFormat(_Format):
    """Shared code of the formats whose header is laid out like a ProTracker
    header (Ben Replay, Binary Packer, Gnu Protected Module and HCD Protector
    use ``check_pt_header``; Devils Replay shares only the conversion steps)."""

    def check_pt_header(self, r: _Reader, mark: bytes, min_length: int,
                        sample_fields) -> int | None:
        """Check the length, the mark at 0x438 and the 31 sample headers;
        returns the total sample size or ``None``."""
        if r.length < min_length:
            return None
        r.seek(0x438)
        if r.mark() != mark:
            return None
        r.seek(20)
        total = 0
        for _ in range(31):
            r.seek(22, current=True)
            size, fine_tune, volume, loop_start, loop_size = sample_fields(r)
            if not _test_sample(size, loop_start, loop_size, volume, fine_tune):
                return None
            total += size
        return total

    def check_positions_and_length(self, r: _Reader, total: int) -> bool:
        temp = r.u8()
        if temp == 0 or temp > 0x7F:
            return False
        if self.find_highest(r, 128, 0x3B8) > 64:
            return False
        return 0x43C + self.number_of_patterns * 1024 + total <= r.length + _MAX_MISSING

    def prepare(self, r):
        r.seek(0x3B6)
        self.npos = r.u8()
        self.restart = r.u8()
        return True

    def module_name(self, r):
        r.seek(0)
        return r.read(20)

    def position_list(self, r):
        r.seek(0x3B8)
        return r.read(self.npos)

    def restart_position(self, r):
        return self.restart

    def samples(self, r):
        for _ in range(31):
            name = r.read(22)
            length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = r.u16()
            loop_length = r.u16()
            yield _Sample(name, length, loop_start, loop_length, volume, fine_tune)

    def write_samples(self, r, out):
        return self.save_marks(r, out)


def _std_fields(r: _Reader):
    size = (r.u16() * 2) & 0xFFFF
    fine_tune = r.u8()
    volume = r.u8()
    loop_start = (r.u16() * 2) & 0xFFFF
    loop_size = (r.u16() * 2) & 0xFFFF
    return size, fine_tune, volume, loop_start, loop_size


class _BenReplay(_PcHeaderFormat):
    """Ben Replay (BenReplayFormat.cs): ProTracker layout with mark "Fuck";
    notes are stored as period-table indexes."""

    name = "Ben Replay"

    def check(self, r):
        total = self.check_pt_header(r, b"Fuck", 0x43C, _std_fields)
        return total is not None and self.check_positions_and_length(r, total)

    def patterns(self, r):
        r.seek(0x43C)
        for _ in range(self.number_of_patterns):
            src = r.read(1024)
            pattern = bytearray(1024)
            for j in range(0, 1024, 4):
                b1, b2, b3, b4 = src[j], src[j + 1], src[j + 2], src[j + 3]
                pattern[j] = b1 & 0xF0
                pattern[j + 2] = ((b1 & 0x0F) << 4) | (b3 // 2)
                pattern[j + 3] = b4
                index = b2 // 2
                if index:
                    hi, lo = _period(index - 1)
                    pattern[j] |= hi
                    pattern[j + 1] = lo
            yield pattern


class _BinaryPacker(_PcHeaderFormat):
    """Binary Packer (BinaryPackerFormat.cs): mark "BNR!", loop length and
    loop start swapped in the sample headers, notes stored as reversed
    period-table indexes with flag bits."""

    name = "Binary Packer"

    def check(self, r):
        def fields(r):
            size = (r.u16() * 2) & 0xFFFF
            fine_tune = r.u8()
            volume = r.u8()
            loop_size = (r.u16() * 2) & 0xFFFF
            loop_start = (r.u16() * 2) & 0xFFFF
            return size, fine_tune, volume, loop_start, loop_size

        total = self.check_pt_header(r, b"BNR!", 0x43C, fields)
        return total is not None and self.check_positions_and_length(r, total)

    def samples(self, r):
        for _ in range(31):
            name = r.read(22)
            length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_length = r.u16()
            loop_start = r.u16()
            yield _Sample(name, length, loop_start, loop_length, volume, fine_tune)

    def patterns(self, r):
        r.seek(0x43C)
        for _ in range(self.number_of_patterns):
            src = r.read(1024)
            pattern = bytearray(1024)
            for j in range(0, 1024, 4):
                b1, b2, b3, b4 = src[j], src[j + 1], src[j + 2], src[j + 3]
                pattern[j + 3] = b3
                pattern[j + 2] = ((b1 >> 2) & 0x0F) | ((b4 << 1) & 0xF0)
                pattern[j] = (b4 >> 3) & 0xF0
                if b1 & 0x40:
                    continue
                if b1 & 0x80:
                    pattern[j + 2] |= 0x0F          # forced set-BPM
                hi, lo = _period(36 - (b2 >> 1))
                pattern[j] |= hi
                pattern[j + 1] = lo
            yield pattern


class _DevilsReplay(_PcHeaderFormat):
    """Devils Replay (DevilsReplayFormat.cs): ProTracker header with "M.K.";
    each pattern row is a flag byte plus an offset into a reference table,
    one flag bit per 2-byte half of the row's four 4-byte cells."""

    name = "Devils Replay"

    def check(self, r):
        if r.length < 0x440:
            return False
        r.seek(0x438)
        if r.mark() != b"M.K.":
            return False
        r.seek(20)
        for _ in range(31):
            r.seek(22, current=True)
            size, fine_tune, volume, loop_start, loop_size = _std_fields(r)
            if not _test_sample(size, loop_start, loop_size, volume, fine_tune):
                return False
        if self.find_highest(r, 128, 0x3B8) > 64:
            return False
        r.seek(0x43C)
        temp = r.u16()
        if temp == 0 or temp + 2 + 1084 > r.length:
            return False
        if self.number_of_patterns * 192 + 4 != temp:
            return False
        r.seek(temp, current=True)
        temp1 = r.u16()
        if temp1 == 0 or temp + temp1 + 2 + 1084 + 2 > r.length:
            return False
        return True

    def prepare(self, r):
        super().prepare(r)
        r.seek(0x43C)
        self.pattern_source_size = r.u16()
        r.seek(self.pattern_source_size, current=True)
        self.reference_table_size = r.u16()
        self.reference_table = r.read(self.reference_table_size)
        return True

    def patterns(self, r):
        r.seek(0x43E)
        table = self.reference_table
        end_offset = 1084 + 2 + self.pattern_source_size
        for _ in range(self.number_of_patterns):
            pattern = bytearray(1024)
            for j in range(64):
                if r.pos >= end_offset:
                    break
                flags = r.u8()
                ref = r.u16()
                if flags == 0:
                    continue
                base = j * 16
                for bit in range(8):
                    if flags & (0x80 >> bit):
                        if ref + 1 >= len(table):
                            raise ValueError("Devils Replay: reference outside the table")
                        pattern[base + bit * 2] = table[ref]
                        pattern[base + bit * 2 + 1] = table[ref + 1]
                        ref = (ref + 2) & 0xFFFF
            yield pattern

    def write_samples(self, r, out):
        r.seek(1084 + 2 + self.pattern_source_size + 2 + self.reference_table_size)
        return self.save_marks(r, out)


class _GnuProtectedModule(_PcHeaderFormat):
    """Gnu Protected Module (GnuProtectedModuleFormat.cs): mark "GPMO",
    doubled volumes; the 8xx effects (demo triggers) are removed."""

    name = "Gnu Protected Module"

    def check(self, r):
        def fields(r):
            size = (r.u16() * 2) & 0xFFFF
            fine_tune = r.u8()
            volume = r.u8() // 2
            loop_start = (r.u16() * 2) & 0xFFFF
            loop_size = (r.u16() * 2) & 0xFFFF
            return size, fine_tune, volume, loop_start, loop_size

        total = self.check_pt_header(r, b"GPMO", 0x440, fields)
        if total is None or not self.check_positions_and_length(r, total):
            return False
        r.seek(0x43C)
        for _ in range(4 * 64):
            temp = r.u8()
            if temp > 0x13:
                return False
            temp1 = (temp & 0x0F) * 256 + r.u8()
            if 0 < temp1 < 0x1C:
                return False
            r.seek(2, current=True)
        return True

    def samples(self, r):
        for s in super().samples(r):
            s.volume //= 2
            yield s

    def patterns(self, r):
        r.seek(0x43C)
        for _ in range(self.number_of_patterns):
            pattern = bytearray(r.read(1024))
            for j in range(2, 1024, 4):
                c = pattern[j]
                if c & 0x0F == 0x08:
                    pattern[j] = c & 0xF0
                    pattern[j + 1] = 0
            yield pattern


class _HcdProtector(_PcHeaderFormat):
    """HCD Protector (HcdProtectorFormat.cs): ProTracker header with "M.K.",
    loop starts offset by 0x345 words, pattern bytes shifted by 3 bits."""

    name = "HCD Protector"

    def check(self, r):
        def fields(r):
            size = (r.u16() * 2) & 0xFFFF
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = (r.u16() * 2 - 0x68A) & 0xFFFF
            loop_size = (r.u16() * 2) & 0xFFFF
            return size, fine_tune, volume, loop_start, loop_size

        total = self.check_pt_header(r, b"M.K.", 0x43C, fields)
        return total is not None and self.check_positions_and_length(r, total)

    def samples(self, r):
        for s in super().samples(r):
            s.loop_start = (s.loop_start - 0x345) & 0xFFFF
            yield s

    def patterns(self, r):
        r.seek(0x43C)
        for _ in range(self.number_of_patterns):
            src = r.read(1024)
            pattern = bytearray(1024)
            for j in range(0, 1024, 4):
                b1, b2, b3, b4 = src[j], src[j + 1], src[j + 2], src[j + 3]
                pattern[j] = b1 >> 3
                pattern[j + 1] = ((b2 >> 3) | (b1 << 5)) & 0xFF
                pattern[j + 2] = ((b3 >> 3) | (b2 << 5)) & 0xFF
                pattern[j + 3] = ((b4 >> 3) | (b3 << 5)) & 0xFF
            yield pattern


class _GnoiPacker(_Format):
    """GnoiPacker (GnoiPackerFormat.cs): mark "-GD-", short sample headers,
    3-byte notes with period-table indexes; 8xx effects are removed."""

    name = "GnoiPacker"

    def check(self, r):
        if r.length < 28:
            return False
        r.seek(22)
        sample_count = r.u8()
        if sample_count == 0 or sample_count > 31:
            return False
        self.number_of_patterns = r.u8()
        if self.number_of_patterns == 0 or self.number_of_patterns > 64:
            return False
        if r.mark() != b"-GD-":
            return False
        for _ in range(sample_count):
            size = (r.u16() * 2) & 0xFFFF
            volume = r.u16() & 0xFF
            loop_start = (r.u16() * 2) & 0xFFFF
            loop_size = (r.u16() * 2) & 0xFFFF
            if not _test_sample(size, loop_start, loop_size, volume, 0):
                return False
        return True

    def prepare(self, r):
        r.seek(20)
        npos = r.u8()
        self.restart = r.u8()
        self.number_of_samples = r.u8()
        self.position_padding = bool(npos & 0x80)
        npos &= 0x7F
        if self.position_padding:
            npos = (npos - 1) & 0xFF
        self.npos = npos
        return True

    def module_name(self, r):
        r.seek(0)
        return r.read(20)

    def samples(self, r):
        r.seek(28)
        for _ in range(self.number_of_samples):
            length = r.u16()
            volume = r.u16() & 0xFF
            loop_start = r.u16()
            loop_length = r.u16()
            yield _Sample(None, length, loop_start, loop_length, volume, 0)

    def position_list(self, r):
        positions = r.read(self.npos)
        if self.position_padding:
            r.seek(1, current=True)
        self.real_number_of_patterns = self.number_of_patterns
        self.highest_of(positions)
        return positions

    def restart_position(self, r):
        return self.restart

    def patterns(self, r):
        for _ in range(self.number_of_patterns):
            src = r.read(768)
            pattern = bytearray(1024)
            for j in range(256):
                b1, b2, b3 = src[j * 3], src[j * 3 + 1], src[j * 3 + 2]
                if b2 & 0x0F == 0x08:
                    b2 &= 0xF0
                    b3 = 0
                o = j * 4
                pattern[o] = (b1 >> 3) & 0x10
                pattern[o + 2] = b2
                pattern[o + 3] = b3
                b1 &= 0x7F
                if b1:
                    hi, lo = _period(b1 - 1)
                    pattern[o] |= hi
                    pattern[o + 1] = lo
            yield pattern
        r.seek((self.real_number_of_patterns - self.number_of_patterns) * 768, current=True)

    def write_samples(self, r, out):
        return self.save_marks(r, out)


class _GnuPlayer(_Format):
    """GnuPlayer (GnuPlayerFormat.cs): mark "GnPl" at 0x92, two command
    tracks that are rebuilt into one pattern per position (the same notes on
    channels 1+2 and 3+4), and 4-bit delta-packed samples."""

    name = "GnuPlayer"

    def check(self, r):
        if r.length < 0x96:
            return False
        r.seek(0x92)
        if r.mark() != b"GnPl":
            return False
        r.seek(0x14)
        for _ in range(31):
            size = r.u16()
            loop_start = r.u16()
            if loop_start > size + 1:
                return False
        return True

    @staticmethod
    def _read_track(r: _Reader) -> bytes:
        length = r.u16()
        if length < 2:
            raise ValueError("GnuPlayer: bad track length")
        return r.read(length - 2)

    @staticmethod
    def _count_lines(track: bytes) -> int:
        lines = 0
        for i in range(0, len(track), 2):
            if track[i] == 4:
                if i + 1 >= len(track):
                    raise ValueError("GnuPlayer: truncated track")
                lines += track[i + 1]
        return lines

    def prepare(self, r):
        r.seek(0x90)
        self.period = r.u16()
        r.seek(4, current=True)
        self.track1 = self._read_track(r)
        self.track2 = self._read_track(r)
        lines = max(self._count_lines(self.track1), self._count_lines(self.track2))
        self.npos = ((lines + 1) // 64) & 0xFF
        return True

    def module_name(self, r):
        r.seek(0)
        return r.read(20)

    def samples(self, r):
        for _ in range(31):
            length = r.u16()
            loop_start = r.u16()
            loop_length = 1 if loop_start == 0 else (length - loop_start) & 0xFFFF
            yield _Sample(None, length, loop_start, loop_length, 0x40, 0)

    def position_list(self, r):
        return bytes(range(self.npos))

    def patterns(self, r):
        state1 = [0, 0]             # track offset, empty lines left
        state2 = [0, 0]
        for _ in range(self.npos):
            pattern = bytearray(1024)
            self._fill_track(pattern, 0, self.track1, state1)
            self._fill_track(pattern, 8, self.track2, state2)
            yield pattern

    def _fill_track(self, pattern: bytearray, offset: int, track: bytes, state: list) -> None:
        pos, empty = state
        eff_count = 0
        i = 0
        while i < 64:
            if empty > 0:
                offset += 16
                empty -= 1
                i += 1
                continue
            if pos + 1 >= len(track):
                raise ValueError("GnuPlayer: track ends without an end command")
            cmd = track[pos]
            arg = track[pos + 1]
            if cmd == 0:            # end of track
                break
            pos += 2
            if cmd == 1:            # set volume
                pattern[offset + 2] |= 0x0C
                pattern[offset + 3] = arg
                pattern[offset + 6] |= 0x0C
                pattern[offset + 7] = arg
                eff_count += 1
            elif cmd == 2:          # volume slide
                pattern[offset + 2] |= 0x0A
                pattern[offset + 3] = arg
                pattern[offset + 6] |= 0x0A
                pattern[offset + 7] = arg
                eff_count += 1
            elif cmd == 3:          # set speed
                o = offset if eff_count == 0 else offset + 4
                pattern[o + 2] |= 0x0F
                pattern[o + 3] = arg
                eff_count += 1
            elif cmd == 4:          # advance rows
                empty = arg
                eff_count = 0
            elif cmd == 5:          # note
                byt1 = (arg & 0xF0) | ((self.period >> 8) & 0x0F)
                byt2 = self.period & 0xFF
                byt3 = (arg << 4) & 0xFF
                pattern[offset] = byt1
                pattern[offset + 1] = byt2
                pattern[offset + 2] |= byt3
                pattern[offset + 4] = byt1
                pattern[offset + 5] = byt2
                pattern[offset + 6] |= byt3
        state[0] = pos
        state[1] = empty

    def write_samples(self, r, out):
        r.seek(0x96 + 2 + len(self.track1) + 2 + len(self.track2))
        for length in self.sample_lengths:
            if not length:
                continue
            if r.length - r.pos < (length // 2) - _MAX_MISSING:
                return False
            r.seek(2, current=True)             # packed length
            sample = r.s8()
            dest = bytearray(length + 1)
            dest[0] = sample & 0xFF
            packed = r.read(length // 2)
            j = 1
            for b in packed:
                if j >= length:
                    break
                hi = b >> 4
                lo = b & 0x0F
                sample += hi - 16 if hi & 8 else hi
                sample = ((sample + 128) & 0xFF) - 128
                dest[j] = sample & 0xFF
                j += 1
                sample += lo - 16 if lo & 8 else lo
                sample = ((sample + 128) & 0xFF) - 128
                dest[j] = sample & 0xFF
                j += 1
            if r.pos % 2:
                r.seek(1, current=True)
            out += dest[:length]
        return True


class _ModulePatterncompressor(_Format):
    """Module-Patterncompressor (ModulePatterncompressorFormat.cs): mark
    "PMD3" (4 channels) or "PMd3" (8 channels); each pattern channel is a
    run-length control stream into a table of 4-byte notes."""

    name = "Module-Patterncompressor"

    def check(self, r):
        if r.length < 1092:
            return False
        r.seek(1080)
        if r.mark() not in (b"PMD3", b"PMd3"):
            return False
        r.seek(20)
        total = 0
        for _ in range(31):
            r.seek(22, current=True)
            size, fine_tune, volume, loop_start, loop_size = _std_fields(r)
            if not _test_sample(size, loop_start, loop_size, volume, fine_tune):
                return False
            total += size
        if r.u8() > 127:
            return False
        if self.find_highest(r, 128, 952) > 127:
            return False
        r.seek(1084)
        control_len = r.u32()
        data_len = r.u32()
        # A C# uint sum: it wraps at 32 bits.
        return ((1092 + control_len + data_len + total) & _U32) <= r.length + _MAX_MISSING

    def prepare(self, r):
        r.seek(950)
        self.npos = r.u8()
        self.restart = r.u8()
        r.seek(1080)
        self.eight_channels = (r.u32() & 0x0000FF00) == 0x00006400
        control_len = r.u32()
        data_len = r.u32()
        channels = 8 if self.eight_channels else 4
        self.channels = channels
        self.offsets = [[(r.u32(), r.u32()) for _ in range(channels)]
                        for _ in range(self.number_of_patterns)]
        self.control_start = 1092 + self.number_of_patterns * channels * 8
        self.data_start = _i32(self.control_start + control_len)
        self.sample_start = _i32(self.data_start + data_len)
        self.mark = b"CD81" if self.eight_channels else b"M.K."
        return True

    def module_name(self, r):
        r.seek(0)
        return r.read(20)

    def samples(self, r):
        for _ in range(31):
            name = r.read(22)
            length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = r.u16()
            loop_length = r.u16()
            yield _Sample(name, length, loop_start, loop_length, volume, fine_tune)

    def position_list(self, r):
        r.seek(952)
        return r.read(self.npos)

    def restart_position(self, r):
        return self.restart

    def patterns(self, r):
        channels = self.channels
        size = 2048 if self.eight_channels else 1024
        step = 32 if self.eight_channels else 16
        for i in range(self.number_of_patterns):
            pattern = bytearray(size)
            for j in range(channels):
                control_offset, data_offset = self.offsets[i][j]
                k = 0
                m = 0
                while k < 64:
                    r.seek(self.control_start + control_offset + m)
                    c = r.u8()
                    repeat = (c & 0x03) + 1
                    r.seek(self.data_start + data_offset + (c & 0xFC))
                    note = bytes((r.u8(), r.u8(), r.u8(), r.u8()))
                    o = j * 4 + k * step
                    for _ in range(repeat):
                        if o + 4 > size:
                            raise ValueError("Module-Patterncompressor: row outside the pattern")
                        pattern[o:o + 4] = note
                        o += step
                    k += repeat
                    m += 1
            yield pattern

    def write_samples(self, r, out):
        r.seek(self.sample_start)
        return self.save_marks(r, out)


class _NewtronPacker10(_Format):
    """Newtron Packer 1.0 (NewtronPacker10Format.cs): no mark; a small
    header with the sample count, nameless sample headers, the position
    list, then raw ProTracker patterns."""

    name = "Newtron Packer 1.0"

    def check(self, r):
        if r.length < 8:
            return False
        r.seek(0)
        sample_count = r.u16()
        if sample_count == 0 or sample_count % 8:
            return False
        sample_count //= 8
        if r.u8() != 0:
            return False
        npos = r.u8()
        if npos == 0 or npos > 0x7F:
            return False
        r.seek(8)
        total = 0
        for _ in range(sample_count):
            size, fine_tune, volume, loop_start, loop_size = _std_fields(r)
            if not _test_sample(size, loop_start, loop_size, volume, fine_tune):
                return False
            if size == 0 and loop_start == 0 and loop_size == 0:
                return False
            total += size
        if total <= 2:
            return False
        if self.find_highest(r, npos) > 64:
            return False
        if (8 + sample_count * 8 + npos + self.number_of_patterns * 1024 + total
                > r.length + _MAX_MISSING):
            return False
        for _ in range(4 * 64):
            temp = r.u8()
            if temp & 0x0F > 0x03:
                return False
            if temp & 0xF0 > 0x10:
                return False
            temp1 = (temp & 0x0F) * 256 + r.u8()
            if 0 < temp1 < 0x71:
                return False
            r.seek(2, current=True)
        return True

    def prepare(self, r):
        r.seek(0)
        self.number_of_samples = (r.u16() // 8) & 0xFF
        self.npos = r.u16() & 0xFF
        return True

    def samples(self, r):
        r.seek(8)
        for _ in range(self.number_of_samples):
            length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = r.u16()
            loop_length = r.u16()
            yield _Sample(None, length, loop_start, loop_length, volume, fine_tune)

    def position_list(self, r):
        return r.read(self.npos)

    def patterns(self, r):
        for _ in range(self.number_of_patterns):
            yield r.read(1024)

    def write_samples(self, r, out):
        return self.save_marks(r, out)


class _NewtronPacker20(_Format):
    """Newtron Packer 2.0 (NewtronPacker20Format.cs): no mark; a 4-byte
    header, 31 nameless sample headers, a 128-entry position list, then raw
    ProTracker patterns from offset 380."""

    name = "Newtron Packer 2.0"

    def check(self, r):
        if r.length < 380:
            return False
        r.seek(0)
        npos = r.u8()
        if npos == 0 or npos > 0x7F:
            return False
        if r.u8() != 0:
            return False
        pattern_size = r.u16()
        total = 0
        for _ in range(31):
            size, fine_tune, volume, loop_start, loop_size = _std_fields(r)
            if not _test_sample(size, loop_start, loop_size, volume, fine_tune):
                return False
            total += size
        if total <= 2:
            return False
        if self.find_highest(r, 128) > 64:
            return False
        if 380 + self.number_of_patterns * 1024 != pattern_size + 4:
            return False
        if 380 + self.number_of_patterns * 1024 + total > r.length + _MAX_MISSING:
            return False
        for _ in range(4 * 64):
            temp = r.u8()
            if temp > 0x13:
                return False
            temp1 = (temp & 0x0F) * 256 + r.u8()
            if 0 < temp1 < 0x71:
                return False
            r.seek(2, current=True)
        return True

    def prepare(self, r):
        r.seek(0)
        self.npos = r.u8()
        return True

    def samples(self, r):
        r.seek(4)
        for _ in range(31):
            length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = r.u16()
            loop_length = r.u16()
            yield _Sample(None, length, loop_start, loop_length, volume, fine_tune)

    def position_list(self, r):
        return r.read(self.npos)

    def patterns(self, r):
        r.seek(380)
        for _ in range(self.number_of_patterns):
            yield r.read(1024)

    def write_samples(self, r, out):
        return self.save_marks(r, out)


def _find_patt(data: bytes, start: int, limit: int) -> int:
    """The first offset ``p >= start`` with ``p - start`` even and
    ``data[p:p + 4] == b"PATT"``, ``p + 4 <= limit``; -1 if there is none.

    Each chunk is rewritten with a 0x01 byte after every 2-byte unit, so
    ``PA\\x01TT`` can only match at a unit boundary (an even offset)."""
    chunk_size = 1 << 16                        # even
    pos = start
    while pos + 4 <= limit:
        seg = data[pos:min(limit, pos + chunk_size + 4)]
        seg = seg[:len(seg) & ~1]
        units = len(seg) // 2
        spread = bytearray(units * 3)
        spread[0::3] = seg[0::2]
        spread[1::3] = seg[1::2]
        spread[2::3] = b"\x01" * units
        hit = spread.find(b"PA\x01TT")
        if hit >= 0:
            return pos + (hit // 3) * 2
        pos += chunk_size
    return -1


class _NoiseTrackerCompressed(_Format):
    """NoiseTracker Compressed (NoiseTrackerCompressedFormat.cs): sample
    headers with data offsets, "PATT"-marked patterns stored as per-channel
    byte streams with empty-row runs."""

    name = "NoiseTracker Compressed"

    def check(self, r):
        if r.length < 1080:
            return False
        r.seek(0x1F6)
        if r.mark() != b"PATT":
            return False
        r.seek(0x174)
        npos = r.u8()
        if npos == 0 or npos >= 128:
            return False
        if self.find_highest(r, npos, 0x176) > 64:
            return False
        r.seek(0)
        first_sample = r.u32()
        if first_sample & 1:
            return False
        # Each pattern starts with "PATT": NostalgicPlayer steps two bytes at a
        # time from 0x1f6 and fails once it is past the first sample's offset.
        data = r.data
        start = 0x1F6
        for _ in range(self.number_of_patterns):
            if data[start:start + 4] != b"PATT":
                # A later mark must start before the first sample's offset.
                start = _find_patt(data, start, min(r.length, first_sample + 3))
                if start < 0:
                    return False
            start += 2
        r.seek(0)
        total = 0
        for _ in range(31):
            if r.u32() & 1:
                return False
            temp = r.u16()
            if temp >= 0x8000:
                return False
            temp1 = r.u16()
            temp2 = r.u16()
            temp3 = r.u16()
            if (temp1 & 0x00FF) > 0x40 or (temp1 & 0xFF00) > 0x0F00:
                return False
            if temp == 0:
                if temp1 + temp2 + temp3 > 1:
                    return False
            else:
                if temp2 + temp3 > temp:
                    return False
                total += temp * 2
        return ((first_sample + total) & _U32) <= r.length + _MAX_MISSING

    def prepare(self, r):
        r.seek(0x174)
        self.npos = r.u8()
        self.restart = r.u8()
        return True

    def samples(self, r):
        r.seek(0)
        for _ in range(31):
            r.seek(4, current=True)
            length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = r.u16()
            loop_length = r.u16() or 1
            yield _Sample(None, length, loop_start, loop_length, volume, fine_tune)

    def position_list(self, r):
        r.seek(0x176)
        return r.read(self.npos)

    def restart_position(self, r):
        return self.restart

    def patterns(self, r):
        r.seek(0x1F6)
        for _ in range(self.number_of_patterns):
            if r.pos & 1:
                r.seek(1, current=True)
            r.seek(4 + 3 * 2, current=True)     # "PATT" + 3 words
            pattern = bytearray(1024)
            for j in range(4):
                k = 0
                while k < 64:
                    temp = r.u8()
                    o = k * 16 + j * 4
                    if temp == 0xFF:            # empty rows
                        k += r.u8()
                        continue
                    if temp >= 0xC0:            # sample 0x1x, no effect
                        temp -= 0xC0
                        hi_sample, has_effect = True, False
                    elif temp >= 0x80:          # sample 0x1x and effect
                        temp -= 0x80
                        hi_sample, has_effect = True, True
                    elif temp >= 0x40:          # sample 0x0x, no effect
                        temp -= 0x40
                        hi_sample, has_effect = False, False
                    else:                       # sample 0x0x and effect
                        hi_sample, has_effect = False, True
                    if temp:
                        hi, lo = _period(temp - 1)
                        pattern[o] = hi
                        pattern[o + 1] = lo
                    if hi_sample:
                        pattern[o] |= 0x10
                    pattern[o + 2] = r.u8()
                    if has_effect:
                        pattern[o + 3] = r.u8()
                    k += 1
            yield pattern

    def write_samples(self, r, out):
        r.seek(0)
        r.seek(r.u32())
        return self.save_marks(r, out)


class _PerfectSongBase(_Format):
    """Shared code of Perfect Song 1 and 2 (PerfectSongFormatBase.cs):
    sample headers with data offsets, position list of pattern offsets,
    notes stored as period-table indexes and remapped effect numbers."""

    _EFFECTS: dict = {}

    def _effect(self, effect: int, value: int) -> tuple[int, int]:
        if effect == 0x00:
            return effect, value
        entry = self._EFFECTS.get(effect)
        if entry is None:
            return 0, 0
        new, op = entry
        if op == "swap":
            value = ((value & 0xF0) >> 4) | ((value & 0x0F) << 4)
        elif op == "one":
            value = 0x01
        elif op is not None:
            value |= op
        return new, value

    def create_position_list(self, r: _Reader) -> None:
        r.seek(0x1FC)
        length = r.u16()
        positions = bytearray(length)
        highest = 0
        for i in range(length):
            positions[i] = (((r.u32() - 0x3FE) & _U32) // 1024) & 0xFF
            if positions[i] > highest:
                highest = positions[i]
        self.positions = bytes(positions)
        self.number_of_patterns = highest + 1

    def module_name(self, r):
        r.seek(self.text_offset)
        return r.read(20)

    def samples(self, r):
        self.sample_offsets = [0] * 31
        for i in range(31):
            r.seek(((self.text_offset + 20) & _U32) + i * 22)
            name = r.read(22)
            r.seek(12 + i * 16)
            self.sample_offsets[i] = (r.u32() + self.sample_start) & _U32
            loop_address = (r.u32() + self.sample_start) & _U32
            length = r.u16()
            loop_length = r.u16()
            fine_tune = r.u8()
            volume = r.u8()
            loop_start = (((loop_address - self.sample_offsets[i]) & _U32) // 2) & 0xFFFF
            yield _Sample(name, length, loop_start, loop_length, volume, fine_tune)

    def position_list(self, r):
        return self.positions

    def patterns(self, r):
        r.seek(0x3FE)
        for _ in range(self.number_of_patterns):
            src = r.read(1024)
            pattern = bytearray(1024)
            for j in range(0, 1024, 4):
                note, sample, effect, value = src[j], src[j + 1], src[j + 2], src[j + 3]
                effect, value = self._effect(effect, value)
                sample = ((note & 0x01) << 4) | (sample >> 4)
                pattern[j] = sample & 0xF0
                pattern[j + 2] = ((sample << 4) & 0xF0) | effect
                pattern[j + 3] = value
                note //= 2
                if note:
                    hi, lo = _period(note - 1)
                    pattern[j] |= hi
                    pattern[j + 1] = lo
            yield pattern

    def write_samples(self, r, out):
        for i, length in enumerate(self.sample_lengths):
            if length:
                r.seek(self.sample_offsets[i])
                if not self.copy_sample(r, out, length):
                    return False
        return True


_PS_EFFECTS_COMMON = {
    0x04: (0x0, None), 0x08: (0x1, None), 0x0C: (0x2, None),
    0x10: (0x3, None), 0x14: (0x3, None), 0x18: (0x4, None), 0x1C: (0x4, None),
    0x20: (0x5, None), 0x24: (0x6, "swap"), 0x28: (0x6, None), 0x2C: (0x6, None),
    0x30: (0x7, None), 0x38: (0x9, None), 0x3C: (0xA, "swap"), 0x40: (0xA, None),
    0x48: (0xC, None), 0x4C: (0xD, None), 0x50: (0xF, None),
    0x58: (0xE, "one"), 0x5C: (0xE, 0x10), 0x60: (0xE, 0x20),
    0x74: (0xE, 0xA0), 0x78: (0xE, 0xB0), 0x7C: (0xE, 0xE0),
}


class _PerfectSong1(_PerfectSongBase):
    """Perfect Song 1 (PerfectSong1Format.cs): mark "PERFSONG"."""

    name = "Perfect Song 1"
    _EFFECTS = {**_PS_EFFECTS_COMMON, 0x44: (0xB, None)}

    def check(self, r):
        if r.length < 0x3FE:
            return False
        r.seek(0)
        if r.mark(8) != b"PERFSONG":
            return False
        temp = (r.u32() + 12) & _U32
        return 802 <= temp <= r.length

    def prepare(self, r):
        r.seek(8)
        temp = r.u32()
        self.text_offset = (temp - 690) & _U32
        self.sample_start = 0
        self.create_position_list(r)
        return True


class _PerfectSong2(_PerfectSongBase):
    """Perfect Song 2 (PerfectSong2Format.cs): mark "PERFSNG2", sample data
    offsets relative to the end of the text block."""

    name = "Perfect Song 2"
    # NostalgicPlayer maps 0x44 to Fxx here (Perfect Song 1 maps it to Bxx).
    _EFFECTS = {**_PS_EFFECTS_COMMON, 0x44: (0xF, None)}

    def check(self, r):
        if r.length < 0x3FE:
            return False
        r.seek(0)
        if r.mark(8) != b"PERFSNG2":
            return False
        temp = (r.u32() + 12 + 12) & _U32
        if temp > r.length or temp < 802:
            return False
        r.seek(temp - 4)
        temp = (temp + r.u32()) & _U32
        return temp <= r.length + _MAX_MISSING

    def prepare(self, r):
        r.seek(8)
        temp = r.u32()
        self.text_offset = (temp - 690) & _U32
        self.sample_start = (temp + 12) & _U32
        self.create_position_list(r)
        return True


class _ThePlayer2x3x(_Format):
    """Shared code of The Player 2.2A and 3.0A (ThePlayer2x_3xFormatBase.cs):
    mark at offset 0, sample headers with data offsets, positions given as
    offsets into packed track data with row-skip counts."""

    tag = b""

    def check(self, r):
        if r.length < 20:
            return False
        r.seek(0)
        if r.mark() != self.tag:
            return False
        self.number_of_patterns = r.u8()
        if self.number_of_patterns > 0x7F:
            return False
        r.seek(1, current=True)
        sample_count = r.u8()
        if sample_count > 0x1F or sample_count == 0:
            return False
        r.seek(20)
        if r.u32() != 0:
            return False
        r.seek(-4, current=True)
        total = 0
        for _ in range(sample_count):
            r.seek(4, current=True)
            length = r.u16() * 2
            r.seek(4, current=True)
            loop_length = r.u16() * 2
            if length > 0xFFFF or loop_length > 0xFFFF:
                return False
            if loop_length > length + 2:
                return False
            total += length
            r.seek(3, current=True)
            if r.u8() > 0x40:
                return False
        return total > 4

    def prepare(self, r):
        r.seek(5)
        self.npos = r.u8() // 2
        self.number_of_samples = r.u8()
        r.seek(1, current=True)
        self.track_data_offset = r.u32()
        track_table_offset = r.u32()
        self.sample_data_offset = r.u32()
        r.seek((track_table_offset + 4) & _U32)
        self.track_offsets = [r.u16() for _ in range(self.npos)]
        self.positions = self.build_position_list(self.npos, lambda i: self.track_offsets[i])
        return True

    def samples(self, r):
        self.sample_starts = [0] * self.number_of_samples
        r.seek(20)
        for i in range(self.number_of_samples):
            start = r.u32()
            self.sample_starts[i] = start
            length = r.u16()
            loop_address = r.u32()
            loop_length = r.u16()
            fine_tune = r.u16()
            volume = r.u16()
            yield _Sample(None, length, (((loop_address - start) & _U32) // 2) & 0xFFFF,
                          loop_length, volume & 0xFF, (fine_tune // 74) & 0xFF)

    def position_list(self, r):
        return self.positions

    def patterns(self, r):
        last = -1
        for i in range(self.npos):
            if self.positions[i] > last:
                last += 1
                pattern = bytearray(1024)
                rows = [0, 0, 0, 0]
                r.seek((self.track_data_offset + 4 + self.track_offsets[i]) & _U32)
                for k in range(64):
                    for j in range(4):
                        if rows[j] > k:
                            continue
                        c1 = r.u8()
                        c2 = r.u8()
                        c3 = r.u8()
                        c4 = r.u8()
                        self.convert_row(c1, c2, c3, c4, j, pattern, rows)
                yield pattern

    def convert_row(self, c1, c2, c3, c4, channel, pattern, rows):
        raise NotImplementedError

    def effect_56a(self, c3: int) -> int:
        raise NotImplementedError

    def convert_data(self, c1: int, c2: int, c3: int) -> bytes:
        sample = ((c1 << 4) & 0x10) | ((c2 >> 4) & 0x0F)
        note = c1 & 0x7F
        if note:
            hi, lo = _period(((note // 2) - 1) & 0xFF)
        else:
            hi = lo = 0
        effect = c2 & 0x0F
        if effect == 0x08:
            c2 -= 0x08
        elif effect in (0x05, 0x06, 0x0A):
            c3 = self.effect_56a(c3)
        return bytes(((sample & 0xF0) | (hi & 0x0F), lo, c2, c3))

    @staticmethod
    def store(note: bytes, channel: int, row: int, pattern: bytearray) -> None:
        if not 0 <= row < 64:
            raise ValueError("The Player: row outside the pattern")
        o = row * 16 + channel * 4
        pattern[o:o + 4] = note

    def write_samples(self, r, out):
        base = (self.sample_data_offset + 4) & _U32
        for i in range(self.number_of_samples):
            length = self.sample_lengths[i]
            if length:
                r.seek((base + self.sample_starts[i]) & _U32)
                if not self.copy_sample(r, out, length):
                    return False
        return True


class _ThePlayer22A(_ThePlayer2x3x):
    """The Player 2.2A (ThePlayer22AFormat.cs): mark "P22A"."""

    name = "The Player 2.2A"
    tag = b"P22A"

    def convert_row(self, c1, c2, c3, c4, channel, pattern, rows):
        self.store(self.convert_data(c1, c2, c3), channel, rows[channel], pattern)
        if 0x00 < c4 < 0x80:
            rows[channel] += c4
        rows[channel] += 1

    def effect_56a(self, c3):
        return (((0x100 - c3) << 4) & 0xFF) if c3 > 0x7F else c3


class _ThePlayer30A(_ThePlayer2x3x):
    """The Player 3.0A (ThePlayer30AFormat.cs): mark "P30A"; a note byte of
    0x80 repeats the row ``c4 + 1`` times."""

    name = "The Player 3.0A"
    tag = b"P30A"

    def convert_row(self, c1, c2, c3, c4, channel, pattern, rows):
        if c1 != 0x80:
            self.store(self.convert_data(c1, c2, c3), channel, rows[channel], pattern)
        else:
            note = self.convert_data(c1 & 0x7F, c2, c3)
            row = rows[channel]
            for _ in range(c4 + 1):
                self.store(note, channel, row, pattern)
                row += 1
        if 0x00 < c4 < 0x80:
            rows[channel] += c4
        rows[channel] += 1

    def effect_56a(self, c3):
        return ((c3 << 4) & 0xF0) if c3 > 0x7F else c3


# Checked in this order (NostalgicPlayer's agent order).
_CONVERTERS = (
    _NoiseTrackerCompressed, _BenReplay, _BinaryPacker, _DevilsReplay,
    _GnoiPacker, _GnuPlayer, _GnuProtectedModule, _HcdProtector,
    _ModulePatterncompressor, _NewtronPacker10, _NewtronPacker20,
    _PerfectSong1, _PerfectSong2, _ThePlayer22A, _ThePlayer30A,
)
assert tuple(c.name for c in _CONVERTERS) == tuple(FORMATS)


def _as_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise TypeError("data must be bytes")


def _match(data: bytes) -> _Format | None:
    for cls in _CONVERTERS:
        fmt = cls()
        if fmt.check(_Reader(data)):
            return fmt
    return None


def identify(data: bytes) -> str | None:
    """The ``FORMATS`` name of the packed format ``data`` is in, or None."""
    fmt = _match(_as_bytes(data))
    return fmt.name if fmt else None


def convert(data: bytes) -> bytes:
    """``data`` rebuilt as a 31-sample ProTracker module (``M.K.``; ``CD81``
    for an 8-channel Module-Patterncompressor module).  Raises ``ValueError``
    when ``data`` is not one of the ``FORMATS`` or is malformed."""
    data = _as_bytes(data)
    fmt = _match(data)
    if fmt is None:
        raise ValueError("not a supported packed module")
    try:
        return fmt.convert(_Reader(data))
    except IndexError as exc:       # an out-of-range table/track index
        raise ValueError(f"{fmt.name}: malformed data") from exc
