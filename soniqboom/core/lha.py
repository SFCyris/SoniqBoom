# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""LHA / LZH archive reader, in pure Python.

The archive layer (``core.archive``) reads LHA archives with ``lhafile``
first (its decoder is C); this reader takes every archive ``lhafile`` turns
down, so an LHA archive opens like a ZIP on every host, with no program to
install:

* header levels 0, 1, 2 and 3, with the extended headers (file and directory
  names, comments) and the Amiga ``name\\0comment`` form;
* methods ``-lh0-`` / ``-lz4-`` (stored), ``-lhd-`` (a directory), ``-lh1-``
  (4 KB window, adaptive Huffman), ``-lh4-`` … ``-lh7-`` (4 / 8 / 32 / 64 KB
  window, static Huffman), ``-lz5-`` (LArc, 4 KB) and ``-lzs-`` (LArc, 2 KB).
  An archive with a member packed otherwise (``-lh2-``, ``-lh3-``, PMarc's
  ``-pm0-`` … ``-pm2-``, LHark's ``-lk7-`` — rare on the Amiga) is refused
  (``LhaUnsupported``), never listed short;
* an archive whose last member is followed by padding or junk, or by no end
  marker (a cut or damaged archive is refused — ``LhaError`` — never listed
  short).

The interface follows ``zipfile`` / ``lhafile``: ``namelist()``,
``infolist()`` (``filename``, ``file_size``, ``compress_size``,
``compress_type``, ``comment``), ``read(name)`` (the CRC is checked),
``read_head(name, n)`` and ``close()``.  Member names are spelled the way
``lhafile`` spells them (so the two readers list an archive alike).  The
archive file is opened for each read: an instance holds no descriptor.
"""
from __future__ import annotations

import os
import struct

__all__ = ["LhaArchive", "LhaError", "LhaInfo", "METHODS", "crc16"]


class LhaError(ValueError):
    """A damaged archive, or one this reader can't decode."""


class LhaUnsupported(LhaError):
    """An archive packed with a method this reader doesn't decode
    (``-lh2-``, ``-lh3-``, ``-pm0-`` … ``-pm2-``, ``-lk7-`` …): ``method``."""

    def __init__(self, method: str) -> None:
        self.method = method
        super().__init__(f"unsupported method {method}")


# ── CRC-16 (reflected 0xA001, initial value 0) ───────────────────────────────

_CRC_TABLE = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ 0xA001 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)
del _n, _c


def crc16(data, crc: int = 0) -> int:
    t = _CRC_TABLE
    for b in data:
        crc = t[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc


# ── Bit input (most significant bit first) ───────────────────────────────────

class _Bits:
    __slots__ = ("data", "pos", "buf", "cnt")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.buf = 0
        self.cnt = 0

    def _fill(self, need: int) -> None:
        data, pos, buf, cnt = self.data, self.pos, self.buf, self.cnt
        n = len(data)
        if pos > n + 8:
            # Read well past the data: a damaged (or hostile) stream that would
            # otherwise decode zeros up to the size its header claims.
            raise LhaError("data ends early")
        while cnt < need:
            buf = (buf << 8) | (data[pos] if pos < n else 0)    # (zeros past the end)
            pos += 1
            cnt += 8
        self.pos, self.buf, self.cnt = pos, buf, cnt

    def peek(self, n: int) -> int:
        if self.cnt < n:
            self._fill(n)
        return (self.buf >> (self.cnt - n)) & ((1 << n) - 1)

    def skip(self, n: int) -> None:
        if self.cnt < n:
            self._fill(n)
        self.cnt -= n
        self.buf &= (1 << self.cnt) - 1

    def get(self, n: int) -> int:
        if n == 0:
            return 0
        if self.cnt < n:
            self._fill(n)
        self.cnt -= n
        v = self.buf >> self.cnt
        self.buf &= (1 << self.cnt) - 1
        return v

    def consumed(self) -> int:
        """Bits taken so far."""
        return self.pos * 8 - self.cnt

    def overrun(self) -> bool:
        """Read further than the data holds (by more than the look-ahead)?"""
        return self.pos - (self.cnt // 8) > len(self.data) + 2


# ── Canonical Huffman tables ─────────────────────────────────────────────────

class _Table:
    """Decode table of a canonical code (shorter codes first, then by symbol):
    ``entries[peek(bits)] = symbol << 5 | length``.  A table of one symbol
    and no bits (``const``) decodes to that symbol without reading."""
    __slots__ = ("bits", "entries", "const")

    def __init__(self, lengths=None, const: int | None = None) -> None:
        self.const = const
        self.bits = 0
        self.entries = None
        if const is not None:
            return
        maxlen = max(lengths) if lengths else 0
        if maxlen == 0:
            raise LhaError("empty Huffman table")
        if maxlen > 16:                             # (LHA codes are at most 16 bits)
            raise LhaError("Huffman code too long")
        size = 1 << maxlen
        entries = [-1] * size
        code = 0
        for length in range(1, maxlen + 1):
            span = 1 << (maxlen - length)
            for sym, ln in enumerate(lengths):
                if ln == length:
                    start = code * span
                    if start + span > size:
                        raise LhaError("bad Huffman table")
                    entries[start:start + span] = [(sym << 5) | length] * span
                    code += 1
            code <<= 1
        self.bits = maxlen
        self.entries = entries

    def decode(self, bits: _Bits) -> int:
        if self.const is not None:
            return self.const
        e = self.entries[bits.peek(self.bits)]
        if e < 0:
            raise LhaError("bad Huffman code")
        bits.skip(e & 31)
        return e >> 5


# ── -lh4- … -lh7-: LZSS + static Huffman, a table set per block ──────────────

_NC = 510                      # literals 0..255, lengths 3..256
_NT = 19                       # code lengths of the literal table
_THRESHOLD = 3
# method → (window bits, position symbols, bits of the position-table size)
_STATIC = {"-lh4-": (12, 14, 4), "-lh5-": (13, 14, 4),
           "-lh6-": (15, 16, 5), "-lh7-": (16, 17, 5)}


def _read_lengths(bits: _Bits, nn: int, nbit: int, special: int) -> _Table:
    n = bits.get(nbit)
    if n == 0:
        c = bits.get(nbit)
        if c >= nn:
            raise LhaError("bad table")
        return _Table(const=c)
    if n > nn:
        raise LhaError("bad table size")
    lengths = [0] * nn
    i = 0
    while i < n:
        c = bits.peek(3)
        if c == 7:
            bits.skip(3)
            while bits.get(1):
                c += 1
                if c > 32:
                    raise LhaError("bad code length")
        else:
            bits.skip(3)
        lengths[i] = c
        i += 1
        if i == special:
            z = bits.get(2)
            while z > 0 and i < nn:
                lengths[i] = 0
                i += 1
                z -= 1
    return _Table(lengths)


def _read_c_table(bits: _Bits, pt: _Table) -> _Table:
    n = bits.get(9)
    if n == 0:
        c = bits.get(9)
        if c >= _NC:
            raise LhaError("bad table")
        return _Table(const=c)
    if n > _NC:
        raise LhaError("bad table size")
    lengths = [0] * _NC
    i = 0
    while i < n:
        c = pt.decode(bits)
        if c <= 2:
            if c == 0:
                run = 1
            elif c == 1:
                run = bits.get(4) + 3
            else:
                run = bits.get(9) + 20
            if i + run > _NC:
                raise LhaError("bad table")
            i += run
        else:
            lengths[i] = c - 2
            i += 1
    return _Table(lengths)


def _decode_static(data: bytes, size: int, method: str, limit: int) -> bytes:
    dicbit, np_, pbit = _STATIC[method]
    bits = _Bits(data)
    out = bytearray()
    want = min(size, limit)
    blocksize = 0
    c_table = p_table = None
    mark = 0
    while len(out) < want:
        if blocksize == 0:
            if c_table is not None and bits.consumed() == mark:
                # A whole block of codes read no bit at all (one-symbol tables):
                # only a hostile stream does that — it unpacks forever.
                raise LhaError("block without data")
            blocksize = bits.get(16)
            if blocksize == 0:
                raise LhaError("empty block")
            pt = _read_lengths(bits, _NT, 5, 3)
            c_table = _read_c_table(bits, pt)
            p_table = _read_lengths(bits, np_, pbit, -1)
            if bits.overrun():
                raise LhaError("data ends early")
            mark = bits.consumed()
        blocksize -= 1
        c = c_table.decode(bits)
        if c < 256:
            out.append(c)
            continue
        length = c - 256 + _THRESHOLD
        p = p_table.decode(bits)
        if p:
            p = (1 << (p - 1)) + bits.get(p - 1)
        start = len(out) - p - 1
        if start < 0:
            raise LhaError("match before the start")
        if p + 1 >= length:
            out += out[start:start + length]
        else:                                   # overlapping copy
            for k in range(length):
                out.append(out[start + k])
        if bits.overrun():
            raise LhaError("data ends early")
    return bytes(out[:want])


# ── -lh1-: LZSS (4 KB) + adaptive Huffman (LHarc's LZHUF) ────────────────────

_N1 = 4096
_F1 = 60
_NCHAR = 256 - 2 + _F1               # 314
_T1 = _NCHAR * 2 - 1                  # 627
_R1 = _T1 - 1
_MAXFREQ = 0x8000
# Upper 6 bits of a position: a fixed canonical code (3 bits for 0, 4 for 1-3,
# 5 for 4-11, 6 for 12-23, 7 for 24-47, 8 for 48-63).
_P1_LENGTHS = [3] + [4] * 3 + [5] * 8 + [6] * 12 + [7] * 24 + [8] * 16


class _AdaptiveHuffman:
    """The adaptive Huffman tree of LZHUF (H. Okumura / H. Yoshizaki):
    ``freq`` / ``son`` / ``prnt`` over 627 nodes, rebuilt with halved
    frequencies when the root reaches 0x8000."""

    def __init__(self) -> None:
        T, N = _T1, _NCHAR
        self.freq = freq = [0] * (T + 1)
        self.son = son = [0] * T
        self.prnt = prnt = [0] * (T + N)
        for i in range(N):
            freq[i] = 1
            son[i] = i + T
            prnt[i + T] = i
        i, j = 0, N
        while j <= _R1:
            freq[j] = freq[i] + freq[i + 1]
            son[j] = i
            prnt[i] = prnt[i + 1] = j
            i += 2
            j += 1
        freq[T] = 0xFFFF
        prnt[_R1] = 0

    def _reconst(self) -> None:
        T, N = _T1, _NCHAR
        freq, son, prnt = self.freq, self.son, self.prnt
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) // 2
                son[j] = son[i]
                j += 1
        i, j = 0, N
        while j < T:
            f = freq[i] + freq[i + 1]
            freq[j] = f
            k = j - 1
            while f < freq[k]:
                k -= 1
            k += 1
            freq[k + 1:j + 1] = freq[k:j]
            freq[k] = f
            son[k + 1:j + 1] = son[k:j]
            son[k] = i
            i += 2
            j += 1
        for i in range(T):
            k = son[i]
            if k >= T:
                prnt[k] = i
            else:
                prnt[k] = prnt[k + 1] = i

    def update(self, c: int) -> None:
        freq, son, prnt = self.freq, self.son, self.prnt
        T = _T1
        if freq[_R1] == _MAXFREQ:
            self._reconst()
        c = prnt[c + T]
        while True:
            freq[c] += 1
            k = freq[c]
            l = c + 1
            if k > freq[l]:
                l += 1
                while k > freq[l]:
                    l += 1
                l -= 1
                freq[c] = freq[l]
                freq[l] = k
                i = son[c]
                prnt[i] = l
                if i < T:
                    prnt[i + 1] = l
                j = son[l]
                son[l] = i
                prnt[j] = c
                if j < T:
                    prnt[j + 1] = c
                son[c] = j
                c = l
            c = prnt[c]
            if c == 0:
                break

    def decode(self, bits: _Bits) -> int:
        son = self.son
        T = _T1
        c = son[_R1]
        while c < T:
            c = son[c + bits.get(1)]
        c -= T
        self.update(c)
        return c


def _decode_lh1(data: bytes, size: int, limit: int) -> bytes:
    bits = _Bits(data)
    tree = _AdaptiveHuffman()
    ptab = _Table(_P1_LENGTHS)
    out = bytearray()
    want = min(size, limit)
    hist = bytearray(b" " * _N1)            # (the window starts as spaces)
    r = _N1 - _F1
    mask = _N1 - 1
    while len(out) < want:
        c = tree.decode(bits)
        if c < 256:
            out.append(c)
            hist[r] = c
            r = (r + 1) & mask
        else:
            pos = (ptab.decode(bits) << 6) | bits.get(6)
            i = (r - pos - 1) & mask
            for k in range(c - 253):
                b = hist[(i + k) & mask]
                out.append(b)
                hist[r] = b
                r = (r + 1) & mask
        if bits.overrun():
            raise LhaError("data ends early")
    return bytes(out[:want])


# ── -lz5- (LArc, 4 KB) and -lzs- (LArc, 2 KB): plain LZSS ────────────────────

def _lz5_window() -> bytearray:
    w = bytearray()
    for i in range(256):
        w += bytes([i]) * 13
    w += bytes(range(256))
    w += bytes(range(255, -1, -1))
    w += bytes(128)
    w += b" " * (128 - 18)
    w += bytes(18)
    return w                                  # 4096 bytes


def _decode_lz5(data: bytes, size: int, limit: int) -> bytes:
    hist = _lz5_window()
    r = 4096 - 18
    out = bytearray()
    want = min(size, limit)
    n = len(data)
    pos = 0
    while len(out) < want:
        if pos >= n:
            raise LhaError("data ends early")
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= want:
                break
            if flags & (1 << bit):
                if pos >= n:
                    raise LhaError("data ends early")
                b = data[pos]
                pos += 1
                out.append(b)
                hist[r] = b
                r = (r + 1) & 4095
            else:
                if pos + 1 >= n:
                    raise LhaError("data ends early")
                b1, b2 = data[pos], data[pos + 1]
                pos += 2
                i = b1 | ((b2 & 0xF0) << 4)
                for k in range((b2 & 0x0F) + 3):
                    b = hist[(i + k) & 4095]
                    out.append(b)
                    hist[r] = b
                    r = (r + 1) & 4095
    return bytes(out[:want])


def _decode_lzs(data: bytes, size: int, limit: int) -> bytes:
    hist = bytearray(b" " * 2048)
    r = 2048 - 17
    out = bytearray()
    want = min(size, limit)
    bits = _Bits(data)
    while len(out) < want:
        if bits.get(1):
            b = bits.get(8)
            out.append(b)
            hist[r] = b
            r = (r + 1) & 2047
        else:
            i = bits.get(11)
            for k in range(bits.get(4) + 2):
                b = hist[(i + k) & 2047]
                out.append(b)
                hist[r] = b
                r = (r + 1) & 2047
        if bits.overrun():
            raise LhaError("data ends early")
    return bytes(out[:want])


# The most a member's data can grow by when it unpacks (bytes out per byte
# in): what each method's codes can at most stand for — a member whose header
# claims more (beyond ``_SIZE_FLOOR``) is damaged or hostile, and the archive
# is refused rather than decoded up to that size.
_MAX_RATIO = {"-lz5-": 16, "-lzs-": 16, "-lh1-": 512}
_DEFAULT_MAX_RATIO = 4096
_SIZE_FLOOR = 64 * 1024 * 1024
_MAX_MEMBERS = 65536


def _check_sizes(method: str, packed: int, size: int) -> None:
    if method in ("-lh0-", "-lz4-"):
        if size != packed:
            raise LhaError("stored member sizes disagree")
        return
    if method == "-lhd-":
        return
    if size > max(_SIZE_FLOOR, packed * _MAX_RATIO.get(method, _DEFAULT_MAX_RATIO)):
        raise LhaError("member size implausible for its packed size")


def plausible_sizes(method: str, packed: int, size: int) -> bool:
    """Could ``packed`` bytes of ``method`` data unpack to ``size`` bytes?
    (The same check the reader makes — for another reader's listing.)"""
    try:
        _check_sizes(method, int(packed), int(size))
    except LhaError:
        return False
    return True


METHODS = ("-lh0-", "-lz4-", "-lhd-", "-lh1-", "-lh4-", "-lh5-", "-lh6-", "-lh7-",
           "-lz5-", "-lzs-")


def decompress(method: str, data: bytes, size: int, limit: int | None = None) -> bytes:
    """``size`` bytes of member data packed by ``method`` (the first ``limit``
    of them when given)."""
    lim = size if limit is None else max(0, min(size, limit))
    if method in ("-lh0-", "-lz4-"):
        return bytes(data[:lim])
    if method == "-lhd-":
        return b""
    if size == 0:
        return b""
    if method in _STATIC:
        return _decode_static(data, size, method, lim)
    if method == "-lh1-":
        return _decode_lh1(data, size, lim)
    if method == "-lz5-":
        return _decode_lz5(data, size, lim)
    if method == "-lzs-":
        return _decode_lzs(data, size, lim)
    raise LhaError(f"unsupported method {method}")


# ── Headers ──────────────────────────────────────────────────────────────────

class LhaInfo:
    """One member: ``filename`` (as ``lhafile`` spells it), ``file_size``,
    ``compress_size``, ``compress_type`` (``"-lh5-"``), ``comment``,
    ``os_id`` (``"A"`` = Amiga, ``"M"`` = MS-DOS, ``"U"`` = Unix …),
    ``level``, ``CRC`` and where its data starts (``file_offset``)."""
    __slots__ = ("filename", "file_size", "compress_size", "compress_type", "comment",
                 "os_id", "level", "CRC", "file_offset", "header_offset", "directory")

    def __repr__(self) -> str:
        return (f"<LhaInfo {self.filename!r} {self.compress_type} {self.compress_size}"
                f"->{self.file_size} level {self.level}>")

    def is_dir(self) -> bool:
        return self.compress_type == "-lhd-"


def _latin1(b: bytes) -> str:
    return b.decode("latin-1")


def _ext_headers(f, first: int, size_bytes: int, info: LhaInfo, names: dict) -> int:
    """Read the chain of extended headers starting with one of ``first``
    bytes; returns their total size."""
    total = 0
    nxt = first
    fmt = "<H" if size_bytes == 2 else "<I"
    guard = 0
    while nxt:
        guard += 1
        if guard > 1024 or nxt < 1 + size_bytes or nxt > 65536:
            raise LhaError("bad extended header")
        raw = f.read(nxt)
        if len(raw) != nxt:
            raise LhaError("header ends early")
        total += nxt
        kind = raw[0]
        body = raw[1:nxt - size_bytes]
        nxt = struct.unpack(fmt, raw[nxt - size_bytes:])[0]
        if kind == 0x01:
            names["file"] = body
        elif kind == 0x02:
            names["dir"] = body
        elif kind == 0x3F:
            names["comment"] = body
        elif kind == 0x42 and len(body) >= 16:          # 64-bit sizes
            info.compress_size, info.file_size = struct.unpack("<QQ", body[:16])
    return total


def _read_header(f, at: int, file_size: int) -> "LhaInfo | None":
    """The member whose header starts at ``at``; None at the archive's end
    (a 0 byte, the end of the file, or anything that is no header)."""
    f.seek(at)
    head = f.read(24)
    if not head or head[0] == 0:
        return None
    method = head[2:7]
    if not (method[:1] == b"-" and method[4:5] == b"-" and method[1:4].isalnum()):
        return None                                 # padding or junk: the archive's end
    if len(head) < 22:
        raise LhaError("header ends early")         # (a cut archive is no shorter archive)
    level = head[20]
    info = LhaInfo()
    info.header_offset = at
    info.compress_type = _latin1(method)
    info.comment = None
    info.os_id = None
    info.directory = None
    info.level = level
    names: dict = {}
    if level in (0, 1):
        hsize = head[0]
        hdr_total = hsize + 2
        f.seek(at)
        raw = f.read(hdr_total)
        if len(raw) != hdr_total:
            raise LhaError("header ends early")
        if (sum(raw[2:]) & 0xFF) != raw[1]:
            raise LhaError("header checksum mismatch")
        skip, osize = struct.unpack("<II", raw[7:15])
        nlen = raw[21]
        if 22 + nlen + 2 > hdr_total:
            raise LhaError("bad header")
        name = raw[22:22 + nlen]
        info.CRC = struct.unpack("<H", raw[22 + nlen:24 + nlen])[0]
        info.file_size = osize
        ext_total = 0
        if level == 1:
            info.os_id = chr(raw[24 + nlen]) if 24 + nlen < hdr_total else None
            first = struct.unpack("<H", raw[hdr_total - 2:hdr_total])[0]
            f.seek(at + hdr_total)
            ext_total = _ext_headers(f, first, 2, info, names)
            info.compress_size = skip - ext_total
        else:
            info.compress_size = skip
            if 24 + nlen < hdr_total:
                info.os_id = chr(raw[24 + nlen])
        info.file_offset = at + hdr_total + ext_total
        names.setdefault("file", name)
    elif level == 2:
        f.seek(at)
        raw = f.read(26)
        if len(raw) != 26:
            raise LhaError("header ends early")
        total, = struct.unpack("<H", raw[0:2])
        info.compress_size, info.file_size = struct.unpack("<II", raw[7:15])
        info.CRC = struct.unpack("<H", raw[21:23])[0]
        info.os_id = chr(raw[23])
        first, = struct.unpack("<H", raw[24:26])
        ext = _ext_headers(f, first, 2, info, names)
        if total < 26 + ext:
            raise LhaError("bad level-2 header size")
        info.file_offset = at + total
    elif level == 3:
        f.seek(at)
        raw = f.read(32)
        if len(raw) != 32 or struct.unpack("<H", raw[0:2])[0] != 4:
            raise LhaError("bad level-3 header")
        info.compress_size, info.file_size = struct.unpack("<II", raw[7:15])
        info.CRC = struct.unpack("<H", raw[21:23])[0]
        info.os_id = chr(raw[23])
        total, first = struct.unpack("<II", raw[24:32])
        ext = _ext_headers(f, first, 4, info, names)
        if total < 32 + ext:
            raise LhaError("bad level-3 header size")
        info.file_offset = at + total
    else:
        raise LhaError(f"header level {level} unknown")
    if info.compress_size < 0 or info.file_offset + info.compress_size > file_size:
        raise LhaError("member data ends early")
    if info.file_offset <= at:
        raise LhaError("bad header size")
    _check_sizes(info.compress_type, info.compress_size, info.file_size)
    # Names spelled as lhafile spells them: Latin-1; the directory header's
    # 0xFF separators → os.sep, joined to the file name; an Amiga
    # ``name\0comment`` split.
    filename = _latin1(names.get("file") or b"")
    if "comment" in names:
        info.comment = _latin1(names["comment"])
    if "dir" in names:
        d = _latin1(names["dir"])
        info.directory = os.sep.join(d.split("\xff"))
        filename = os.path.join(info.directory, filename)
    if "\x00" in filename:
        filename, _, comment = filename.partition("\x00")
        info.comment = comment
    info.filename = filename
    return info


class LhaArchive:
    """A local LHA / LZH archive (see the module docstring)."""

    def __init__(self, path) -> None:
        self.filename = str(path)
        self.filelist: list[LhaInfo] = []
        self.NameToInfo: dict[str, LhaInfo] = {}
        size = os.path.getsize(self.filename)
        with open(self.filename, "rb") as f:
            at = 0
            while at < size:
                info = _read_header(f, at, size)       # (LhaError: cut or damaged)
                if info is None:
                    break                           # the end marker, padding or junk
                if len(self.filelist) >= _MAX_MEMBERS:
                    raise LhaError("too many members")
                if info.compress_type not in METHODS:
                    raise LhaUnsupported(info.compress_type)
                self.filelist.append(info)
                self.NameToInfo.setdefault(info.filename, info)
                nxt = info.file_offset + info.compress_size
                if nxt <= at:
                    raise LhaError("bad header size")
                at = nxt
        if not self.filelist:
            raise LhaError("not an LHA archive (no member header)")

    def namelist(self) -> list[str]:
        return [i.filename for i in self.filelist]

    def infolist(self) -> list[LhaInfo]:
        return list(self.filelist)

    def getinfo(self, name: str) -> LhaInfo:
        return self.NameToInfo[name]

    def _packed(self, info: LhaInfo) -> bytes:
        with open(self.filename, "rb") as f:
            f.seek(info.file_offset)
            data = f.read(info.compress_size)
        if len(data) != info.compress_size:
            raise LhaError(f"{info.filename}: member data ends early")
        return data

    def read(self, name: str) -> bytes:
        info = self.NameToInfo[name]
        if info.compress_type not in METHODS:
            raise LhaError(f"{name}: unsupported method {info.compress_type}")
        out = decompress(info.compress_type, self._packed(info), info.file_size)
        if len(out) != info.file_size:
            raise LhaError(f"{name}: size mismatch")
        if info.compress_type != "-lhd-" and crc16(out) != info.CRC:
            raise LhaError(f"{name}: crc is not matched")
        return out

    def read_head(self, name: str, n: int) -> bytes:
        """The first ``n`` bytes of member ``name`` (decoding stops there; no
        CRC check)."""
        info = self.NameToInfo[name]
        if info.compress_type not in METHODS:
            raise LhaError(f"{name}: unsupported method {info.compress_type}")
        return decompress(info.compress_type, self._packed(info), info.file_size, n)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
