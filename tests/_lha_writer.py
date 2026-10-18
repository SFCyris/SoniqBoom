# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An LHA writer for tests: every method ``core.lha`` reads (``-lh0-``,
``-lz4-``, ``-lhd-``, ``-lh1-``, ``-lh4-`` … ``-lh7-``, ``-lz5-``, ``-lzs-``)
and header levels 0 – 3, with Amiga comments and directory headers.

The compressors are simple (greedy matching) but produce real streams:
``tests/test_lha_reader.py`` has the reference decoder of lhasa (``lha t``,
when installed) check every one of them, so the reader is not only checked
against this writer.  Built on ``scripts/archive_selftest.py`` (the Docker
build's own LHA writer)."""
from __future__ import annotations

import importlib.util
import pathlib
import struct
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# (its own copy, under its own name: test_archive_fork_handles pickles the
# functions of the copy it registers as ``archive_selftest``)
_NAME = "_lha_writer_archive_selftest"
selftest = sys.modules.get(_NAME)
if selftest is None:
    _spec = importlib.util.spec_from_file_location(_NAME, _ROOT / "scripts" / "archive_selftest.py")
    selftest = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = selftest
    _spec.loader.exec_module(selftest)

_Bits = selftest._Bits
crc16 = selftest.crc16

# method → (window bits, position symbols, bits of the position-table size)
_STATIC = {"-lh4-": (12, 14, 4), "-lh5-": (13, 14, 4),
           "-lh6-": (15, 16, 5), "-lh7-": (16, 17, 5)}


def _greedy(data: bytes, window: int, minlen: int, maxlen: int, chain: int = 48):
    """Greedy LZSS tokens: ``(literal byte, None)`` or ``(length, distance)``."""
    heads: dict = {}
    toks = []
    i, n = 0, len(data)

    def remember(pos: int) -> None:
        if pos + minlen <= n:
            heads.setdefault(data[pos:pos + minlen], []).append(pos)

    while i < n:
        best = bdist = 0
        if i + minlen <= n:
            lim = min(maxlen, n - i)
            for p in reversed(heads.get(data[i:i + minlen], [])[-chain:]):
                d = i - p
                if d > window:
                    break
                k = minlen
                while k < lim and data[p + k] == data[i + k]:
                    k += 1
                if k > best:
                    best, bdist = k, d
                    if k == lim:
                        break
        if best >= minlen:
            toks.append((best, bdist))
            for k in range(i, i + best):
                remember(k)
            i += best
        else:
            toks.append((data[i], None))
            remember(i)
            i += 1
    return toks


def lzh_compress(data: bytes, method: str) -> bytes:
    """``-lh4-`` … ``-lh7-``: LZSS + static Huffman, blocks of ≤ 0xFFFF codes."""
    dicbit, np_, pbit = _STATIC[method]
    toks = [((256 + ln - 3), d) if d is not None else (ln, 0)
            for ln, d in _greedy(data, (1 << dicbit) - 1, 3, 256)]
    hl, cc, ptok, putl = (selftest._huffman_lengths, selftest._canonical_codes,
                          selftest._pt_tokens, selftest._put_lengths)
    bits = _Bits()
    NC, NT = 510, 19
    for start in range(0, len(toks), 0xFFFF):
        block = toks[start:start + 0xFFFF]
        c_freq, p_freq = [0] * NC, [0] * np_
        for c, d in block:
            c_freq[c] += 1
            if c >= 256:
                p_freq[(d - 1).bit_length()] += 1
        bits.put(len(block), 16)
        c_used = [s for s, f in enumerate(c_freq) if f]
        if len(c_used) == 1:
            c_len, c_code = [0] * NC, [0] * NC
            putl(bits, [0] * NT, 5, True)
            bits.put(0, 9)
            bits.put(c_used[0], 9)
        else:
            c_len = hl(c_freq)
            c_code = cc(c_len)
            ptoks = ptok(c_len[:max(c_used) + 1])
            t_freq = [0] * NT
            for s, _, _ in ptoks:
                t_freq[s] += 1
            t_used = [s for s, f in enumerate(t_freq) if f]
            t_len = hl(t_freq) if len(t_used) > 1 else [0] * NT
            t_code = cc(t_len) if len(t_used) > 1 else [0] * NT
            if len(t_used) == 1:
                t_len[t_used[0]] = 1
            putl(bits, t_len, 5, True)
            bits.put(max(c_used) + 1, 9)
            for s, extra, nb in ptoks:
                if len(t_used) > 1:
                    bits.put(t_code[s], t_len[s])
                bits.put(extra, nb)
        p_used = [s for s, f in enumerate(p_freq) if f]
        p_len = hl(p_freq) if len(p_used) > 1 else [0] * np_
        p_code = cc(p_len) if len(p_used) > 1 else [0] * np_
        if len(p_used) == 1:
            p_len[p_used[0]] = 1
        putl(bits, p_len, pbit, False)
        for c, d in block:
            if len(c_used) > 1:
                bits.put(c_code[c], c_len[c])
            if c >= 256:
                off = d - 1
                bl = off.bit_length()
                if len(p_used) > 1:
                    bits.put(p_code[bl], p_len[bl])
                if bl > 1:
                    bits.put(off - (1 << (bl - 1)), bl - 1)
    return bits.flush()


class _LzhufTree:
    """LZHUF's adaptive Huffman tree (H. Okumura / H. Yoshizaki — LHarc's
    ``-lh1-``), the encoder's own copy: 314 symbols, frequencies halved when
    the root reaches 0x8000."""
    N_CHAR = 256 - 2 + 60
    T = N_CHAR * 2 - 1
    R = T - 1

    def __init__(self) -> None:
        T, N = self.T, self.N_CHAR
        self.freq = [0] * (T + 1)
        self.son = [0] * T
        self.prnt = [0] * (T + N)
        for i in range(N):
            self.freq[i] = 1
            self.son[i] = i + T
            self.prnt[i + T] = i
        i, j = 0, N
        while j <= self.R:
            self.freq[j] = self.freq[i] + self.freq[i + 1]
            self.son[j] = i
            self.prnt[i] = self.prnt[i + 1] = j
            i += 2
            j += 1
        self.freq[T] = 0xFFFF
        self.prnt[self.R] = 0

    def _reconst(self) -> None:
        T, N, freq, son, prnt = self.T, self.N_CHAR, self.freq, self.son, self.prnt
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) // 2
                son[j] = son[i]
                j += 1
        i, j = 0, N
        while j < T:
            f = freq[i] + freq[i + 1]
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
        T, freq, son, prnt = self.T, self.freq, self.son, self.prnt
        if freq[self.R] == 0x8000:
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
                freq[c], freq[l] = freq[l], k
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


# Upper 6 bits of an -lh1- position: a fixed canonical code.
_P1_LENGTHS = [3] + [4] * 3 + [5] * 8 + [6] * 12 + [7] * 24 + [8] * 16


def lh1_compress(data: bytes) -> bytes:
    """``-lh1-``: LZSS (4 KB, lengths 3..60) + LZHUF's adaptive Huffman."""
    tree = _LzhufTree()
    p_code = selftest._canonical_codes(_P1_LENGTHS)
    bits = _Bits()
    T, R = tree.T, tree.R

    def put_char(c: int) -> None:
        path = []
        k = tree.prnt[c + T]
        while k != R:
            path.append(k & 1)
            k = tree.prnt[k]
        for b in reversed(path):
            bits.put(b, 1)
        tree.update(c)

    for ln, d in _greedy(data, 4096 - 60, 3, 60):
        if d is None:
            put_char(ln)
        else:
            put_char(ln + 253)
            pos = d - 1
            bits.put(p_code[pos >> 6], _P1_LENGTHS[pos >> 6])
            bits.put(pos & 63, 6)
    return bits.flush()


def lz5_compress(data: bytes) -> bytes:
    """``-lz5-`` (LArc): a flag byte per 8 tokens (bit set = literal), a
    match = 12-bit ring position + 4-bit length - 3."""
    out = bytearray()
    toks = _greedy(data, 4096 - 18, 3, 18)
    pos = 0                                     # bytes produced so far
    for g in range(0, len(toks), 8):
        group = toks[g:g + 8]
        flags = 0
        body = bytearray()
        for k, (ln, d) in enumerate(group):
            if d is None:
                flags |= 1 << k
                body.append(ln)
                pos += 1
            else:
                ring = (4096 - 18 + pos - d) & 4095
                body += bytes([ring & 0xFF, ((ring >> 4) & 0xF0) | (ln - 3)])
                pos += ln
        out.append(flags)
        out += body
    return bytes(out)


def lzs_compress(data: bytes) -> bytes:
    """``-lzs-`` (LArc): bit 1 + byte, or bit 0 + 11-bit ring position +
    4-bit length - 2."""
    bits = _Bits()
    pos = 0
    for ln, d in _greedy(data, 2048 - 17, 2, 17):
        if d is None:
            bits.put(1, 1)
            bits.put(ln, 8)
            pos += 1
        else:
            bits.put(0, 1)
            bits.put((2048 - 17 + pos - d) & 2047, 11)
            bits.put(ln - 2, 4)
            pos += ln
    return bits.flush()


def pack(data: bytes, method: str) -> bytes:
    if method in ("-lh0-", "-lz4-", "-lhd-") or not data:
        return data if method != "-lhd-" else b""
    if method in _STATIC:
        return lzh_compress(data, method)
    return {"-lh1-": lh1_compress, "-lz5-": lz5_compress, "-lzs-": lzs_compress}[method](data)


def _ext(kind: int, body: bytes) -> tuple[int, bytes]:
    return kind, body


def member(name: str, data: bytes, method: str = "-lh5-", *, level: int = 0,
           os_id: bytes = b"A", comment: str | None = None, dirname: str | None = None,
           packed: bytes | None = None) -> bytes:
    """One member (header + data).  ``dirname`` uses a directory extended
    header (levels 1 – 3); levels 0 / 1 store an Amiga comment after a NUL in
    the name, levels 2 / 3 in a comment header."""
    method = "-lh0-" if (not data and method not in ("-lhd-", "-lz4-")) else method
    body = pack(data, method) if packed is None else packed
    crc = crc16(data)
    raw_name = name.encode("latin-1")
    dos_time = struct.pack("<HH", 0, ((2026 - 1980) << 9) | (1 << 5) | 1)
    exts: list = []
    if dirname is not None:
        exts.append((0x02, dirname.replace("/", "\xff").encode("latin-1")))
    if level in (0, 1):
        nm = raw_name + (b"\x00" + comment.encode("latin-1") if comment else b"")
        if level == 0:
            base = (method.encode() + struct.pack("<II", len(body), len(data)) + dos_time
                    + bytes([0x20, 0, len(nm)]) + nm + struct.pack("<H", crc))
            return bytes([len(base), sum(base) & 0xFF]) + base + body
        ext_bytes = b""
        sizes = [3 + len(b) for _, b in exts]
        for i, (kind, b) in enumerate(exts):
            nxt = sizes[i + 1] if i + 1 < len(sizes) else 0
            ext_bytes += bytes([kind]) + b + struct.pack("<H", nxt)
        base = (method.encode() + struct.pack("<II", len(body) + len(ext_bytes), len(data))
                + dos_time + bytes([0x20, 1, len(nm)]) + nm + struct.pack("<H", crc)
                + os_id + struct.pack("<H", sizes[0] if sizes else 0))
        return bytes([len(base), sum(base) & 0xFF]) + base + ext_bytes + body
    # Levels 2 and 3: name / comment / directory in extended headers, a
    # common header carrying the header's CRC first.
    if level == 2:
        sz, fmt, hdr_len = 2, "<H", 26
    else:
        sz, fmt, hdr_len = 4, "<I", 32
    exts = [(0x00, b"\0\0"), (0x01, raw_name)] + exts
    if comment:
        exts.append((0x3F, comment.encode("latin-1")))
    sizes = [1 + len(b) + sz for _, b in exts]
    total = hdr_len + sum(sizes)
    pad = b""
    if level == 2 and total & 0xFF == 0:
        pad, total = b"\0", total + 1
    tail = b""
    for i, (kind, b) in enumerate(exts):
        nxt = sizes[i + 1] if i + 1 < len(sizes) else 0
        tail += bytes([kind]) + b + struct.pack(fmt, nxt)
    if level == 2:
        head = (struct.pack("<H", total) + method.encode()
                + struct.pack("<III", len(body), len(data), 1767225600)
                + bytes([0x20, 2]) + struct.pack("<H", crc) + os_id
                + struct.pack("<H", sizes[0]))
    else:
        head = (struct.pack("<H", 4) + method.encode()
                + struct.pack("<III", len(body), len(data), 1767225600)
                + bytes([0x20, 3]) + struct.pack("<H", crc) + os_id
                + struct.pack("<II", total, sizes[0]))
    hdr = bytearray(head + tail + pad)
    hcrc = crc16(bytes(hdr))
    at = len(head) + 1                           # the common header's CRC field
    hdr[at:at + 2] = struct.pack("<H", hcrc)
    return bytes(hdr) + body


def archive(members: list, *, level: int = 0, end: bytes = b"\x00", **kw) -> bytes:
    """An archive of ``(name, data, method)`` (or dicts of ``member`` args)."""
    out = bytearray()
    for m in members:
        if isinstance(m, dict):
            out += member(**{"level": level, **kw, **m})
        else:
            name, data, method = m
            out += member(name, data, method, level=level, **kw)
    return bytes(out + end)
