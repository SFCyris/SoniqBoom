#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Archive self-test: can this install read every member of an LHA / ZIP
archive, including from parallel scan workers?

The Docker build runs it (see the Dockerfile) so an image that loses archive
members can't ship.  Two checks, on archives synthesized here (no fixture
files, nothing downloaded):

1. **lhafile decode.**  ``lhafile``'s ``lzhlib`` C
   extension is compiled from its source package at install time (the image
   build log shows the sdist build), per architecture.  A synthesized LHA
   archive with stored (``-lh0-``) and compressed (``-lh5-``) members must
   decode to the exact bytes, and the CRC-16 lzhlib computes while decoding
   must match the header's, written from the pure-Python reference below.

2. **Scan workers.**  The scanner lists an archive in the server process —
   ``soniqboom.core.archive`` keeps it OPEN — then reads the members in a
   process pool.  With fork-started workers (Linux) every worker inherited the
   server's open archive and its file descriptor, i.e. one shared file offset,
   and parallel seek + read pairs returned other members' bytes (274 of
   AHXSONGS.LHA's 513 members failed "crc is not matched" in one Docker
   scan).  Here the parent lists an LHA and a ZIP archive the same way, then
   fork-started workers read every member through ``archive.read_member`` and
   the bytes are compared — plus a deterministic probe: a worker's read must
   not move the parent's file offset.

Exit status 0 = every check passed, 1 = a failure (listed on stderr) or no
result within ``--deadline`` seconds (a hung worker fails the build instead
of stalling it).
Usage: ``python scripts/archive_selftest.py [--workers N] [--members N] [--deadline S]``
"""

from __future__ import annotations

import argparse
import concurrent.futures
import heapq
import multiprocessing
import os
import random
import signal
import struct
import sys
import tempfile
import zipfile

# ── LHA writer (stored -lh0- and LZSS+Huffman -lh5-) ──────────────────────────

_CRC16_TABLE = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ 0xA001 if _c & 1 else _c >> 1
    _CRC16_TABLE.append(_c)


def crc16(data: bytes, crc: int = 0) -> int:
    """LHA's CRC-16 (reflected 0xA001, init 0) — independent of lzhlib."""
    for b in data:
        crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ b) & 0xFF]
    return crc


class _Bits:
    """MSB-first bit writer."""

    def __init__(self) -> None:
        self.out = bytearray()
        self._acc = 0
        self._n = 0

    def put(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            return
        self._acc = (self._acc << nbits) | (value & ((1 << nbits) - 1))
        self._n += nbits
        while self._n >= 8:
            self._n -= 8
            self.out.append((self._acc >> self._n) & 0xFF)
        self._acc &= (1 << self._n) - 1

    def unary7(self, v: int) -> None:
        """A table length: 3 bits, or ``7`` + (v-7) one-bits + a zero bit."""
        if v < 7:
            self.put(v, 3)
        else:
            self.put(7, 3)
            for _ in range(v - 7):
                self.put(1, 1)
            self.put(0, 1)

    def flush(self) -> bytes:
        if self._n:
            self.out.append((self._acc << (8 - self._n)) & 0xFF)
            self._acc = self._n = 0
        return bytes(self.out)


def _huffman_lengths(freq: list[int], limit: int = 16) -> list[int]:
    """Code lengths (0 = unused) of a Huffman code for *freq*, max *limit*
    bits.  Needs at least two used symbols."""
    f = list(freq)
    while True:
        heap = [(c, i, (i,)) for i, c in enumerate(f) if c]
        heapq.heapify(heap)
        lengths = [0] * len(f)
        tie = len(f)
        while len(heap) > 1:
            c1, _, s1 = heapq.heappop(heap)
            c2, _, s2 = heapq.heappop(heap)
            for s in s1 + s2:
                lengths[s] += 1
            heapq.heappush(heap, (c1 + c2, tie, s1 + s2))
            tie += 1
        if max(lengths) <= limit:
            return lengths
        f = [(c + 1) // 2 if c else 0 for c in f]       # flatten, retry


def _canonical_codes(lengths: list[int]) -> list[int]:
    """Canonical codes — shorter first, then by symbol (lzhlib's order)."""
    maxlen = max(lengths)
    count = [0] * (maxlen + 1)
    for n in lengths:
        if n:
            count[n] += 1
    nxt, code = [0] * (maxlen + 1), 0
    for bits in range(1, maxlen + 1):
        code = (code + count[bits - 1]) << 1 if bits > 1 else 0
        nxt[bits] = code
    codes = [0] * len(lengths)
    for sym, n in enumerate(lengths):
        if n:
            codes[sym] = nxt[n]
            nxt[n] += 1
    return codes


_NC, _NP, _NT = 510, 14, 19          # literal/length, position, length-of-length
_WINDOW, _MAXMATCH, _MINMATCH = 8191, 256, 3


def _lz_tokens(data: bytes) -> list[tuple[int, int]]:
    """Greedy LZSS: ``(byte, 0)`` literals and ``(256 + len - 3, dist)``."""
    heads: dict[bytes, list[int]] = {}
    toks: list[tuple[int, int]] = []
    i, n = 0, len(data)

    def remember(pos: int) -> None:
        if pos + _MINMATCH <= n:
            heads.setdefault(data[pos:pos + _MINMATCH], []).append(pos)

    while i < n:
        best_len = best_dist = 0
        if i + _MINMATCH <= n:
            maxl = min(_MAXMATCH, n - i)
            for p in reversed(heads.get(data[i:i + _MINMATCH], [])[-64:]):
                d = i - p
                if d > _WINDOW:
                    break
                k = _MINMATCH
                while k < maxl and data[p + k] == data[i + k]:
                    k += 1
                if k > best_len:
                    best_len, best_dist = k, d
                    if k == maxl:
                        break
        if best_len >= _MINMATCH:
            toks.append((256 + best_len - _MINMATCH, best_dist))
            for k in range(i, i + best_len):
                remember(k)
            i += best_len
        else:
            toks.append((data[i], 0))
            remember(i)
            i += 1
    return toks


def _pt_tokens(c_len: list[int]) -> list[tuple[int, int, int]]:
    """The literal table's lengths as ``(symbol, extra, extra_bits)`` of the
    length-of-length alphabet: 0 = one zero, 1 = 3..18 zeros, 2 = 20..531
    zeros, k+2 = length k."""
    out: list[tuple[int, int, int]] = []
    i, n = 0, len(c_len)
    while i < n:
        if c_len[i]:
            out.append((c_len[i] + 2, 0, 0))
            i += 1
            continue
        run = 1
        while i + run < n and c_len[i + run] == 0:
            run += 1
        i += run
        while run:
            if run <= 2 or run == 19:
                out.append((0, 0, 0))
                run -= 1
            elif run <= 18:
                out.append((1, run - 3, 4))
                run = 0
            else:
                take = min(run, 20 + 511)
                out.append((2, take - 20, 9))
                run -= take
    return out


def _put_lengths(bits: _Bits, lengths: list[int], nbits: int, special3: bool) -> None:
    """A length-of-length (``special3``) or position table."""
    used = [s for s, n in enumerate(lengths) if n]
    if len(used) <= 1:                       # one leaf: 0 bits per symbol
        bits.put(0, nbits)
        bits.put(used[0] if used else 0, nbits)
        return
    n = max(used) + 1
    bits.put(n, nbits)
    i = 0
    while i < n:
        bits.unary7(lengths[i])
        i += 1
        if special3 and i == 3:
            k = 0
            while k < 3 and i + k < n and lengths[i + k] == 0:
                k += 1
            bits.put(k, 2)
            i += k


def lh5_compress(data: bytes) -> bytes:
    """``-lh5-`` (8 KiB window) — single-pass greedy, real Huffman tables per
    block (the fixture also decodes with lhasa's ``lha t`` and ``7z t``)."""
    toks = _lz_tokens(data)
    bits = _Bits()
    for start in range(0, len(toks), 0xFFFF):
        block = toks[start:start + 0xFFFF]
        c_freq, p_freq = [0] * _NC, [0] * _NP
        for c, d in block:
            c_freq[c] += 1
            if c >= 256:
                p_freq[(d - 1).bit_length()] += 1
        bits.put(len(block), 16)
        c_used = [s for s, f in enumerate(c_freq) if f]
        if len(c_used) == 1:
            c_len, c_code = [0] * _NC, [0] * _NC
            _put_lengths(bits, [0] * _NT, 5, True)            # unused
            bits.put(0, 9)
            bits.put(c_used[0], 9)
        else:
            c_len = _huffman_lengths(c_freq)
            c_code = _canonical_codes(c_len)
            ptoks = _pt_tokens(c_len[:max(c_used) + 1])
            t_freq = [0] * _NT
            for s, _, _ in ptoks:
                t_freq[s] += 1
            t_used = [s for s, f in enumerate(t_freq) if f]
            t_len = (_huffman_lengths(t_freq) if len(t_used) > 1 else [0] * _NT)
            t_code = _canonical_codes(t_len) if len(t_used) > 1 else [0] * _NT
            if len(t_used) == 1:
                t_len[t_used[0]] = 1                          # (leaf form)
            _put_lengths(bits, t_len, 5, True)
            bits.put(max(c_used) + 1, 9)
            for s, extra, nb in ptoks:
                if len(t_used) > 1:
                    bits.put(t_code[s], t_len[s])
                bits.put(extra, nb)
        p_used = [s for s, f in enumerate(p_freq) if f]
        p_len = _huffman_lengths(p_freq) if len(p_used) > 1 else [0] * _NP
        p_code = _canonical_codes(p_len) if len(p_used) > 1 else [0] * _NP
        if len(p_used) == 1:
            p_len[p_used[0]] = 1
        _put_lengths(bits, p_len, 4, False)
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


def lha_archive(members: list[tuple[str, bytes, str]]) -> bytes:
    """A level-0 LHA archive of ``(name, data, method)`` members (method
    ``-lh0-`` or ``-lh5-``; empty data is always stored).  Names use the Amiga
    backslash directory separator, as real archives do."""
    out = bytearray()
    dos_time = (0 << 11) | (0 << 5) | 0                      # 00:00:00
    dos_date = ((2026 - 1980) << 9) | (1 << 5) | 1           # 2026-01-01
    for name, data, method in members:
        packed = lh5_compress(data) if (method == "-lh5-" and data) else data
        if method != "-lh5-" or not data:
            method = "-lh0-"
        fname = name.encode("latin-1")
        body = (method.encode() + struct.pack("<IIHHBB", len(packed), len(data),
                                              dos_time, dos_date, 0x20, 0)
                + bytes([len(fname)]) + fname + struct.pack("<H", crc16(data)))
        out += bytes([len(body), sum(body) & 0xFF]) + body + packed
    out += b"\x00"
    return bytes(out)


# ── Fixture content ──────────────────────────────────────────────────────────

def fixture_members(count: int, seed: int = 0x5B) -> list[tuple[str, bytes, str]]:
    """Deterministic members: module-like bytes (repeated patterns, far and
    overlapping repeats, every byte value, random tails), alternating methods."""
    rng = random.Random(seed)
    members = []
    for i in range(count):
        motif = bytes(rng.randrange(256) for _ in range(rng.randrange(16, 300)))
        noise = bytes(rng.randrange(256) for _ in range(rng.randrange(64, 700)))
        body = (bytes(range(256)) + motif * rng.randrange(2, 6) + noise
                + bytes([i & 0xFF]) * rng.randrange(3, 600)           # run (dist 1)
                + noise[: rng.randrange(1, len(noise))] + motif)
        if i % 7 == 3:
            body += bytes(rng.randrange(256) for _ in range(6000)) + motif  # far repeat
        method = "-lh5-" if i % 2 else "-lh0-"
        members.append((f"Selftest {i // 50}\\AHX.tune {i:03d}", body, method))
    return members


# ── Checks ───────────────────────────────────────────────────────────────────

def check_lhafile(path: str, members) -> list[str]:
    import lhafile
    problems = []
    lf = lhafile.LhaFile(path)
    want = {name: (data, method) for name, data, method in members}
    infos = {i.filename: i for i in lf.infolist()}
    if set(infos) != set(want):
        problems.append(f"lhafile listed {len(infos)} members, expected {len(want)}")
    for name, (data, method) in want.items():
        info = infos.get(name)
        if info is None:
            continue
        try:
            # lhafile computes the CRC-16 of what lzhlib decoded and raises
            # unless it equals the header's — written from the reference above.
            got = lf.read(name)
        except Exception as exc:
            problems.append(f"{name} ({info.compress_type.decode()}): {type(exc).__name__}: {exc}")
            continue
        if got != data:
            problems.append(f"{name}: decoded bytes differ ({len(got)} vs {len(data)})")
    methods = {i.compress_type for i in infos.values()}
    if not {b"-lh0-", b"-lh5-"} <= methods:
        problems.append(f"fixture lacks a method: {sorted(methods)}")
    return problems


def _worker_read(args):
    path, name = args
    from soniqboom.core import archive
    try:
        return name, archive.read_member(path, name), None
    except Exception as exc:
        return name, None, f"{type(exc).__name__}: {exc}"


def _worker_read_first(path):
    from soniqboom.core import archive
    name = sorted(archive.list_members(path, strict=True))[0]
    archive.read_member(path, name)
    return os.getpid()


def _parent_offsets(path) -> list[int]:
    """Kernel file offsets of the parent's cached open handles of *path*."""
    from soniqboom.core import archive
    offs = []
    for key, obj in list(archive._OPEN_CACHE.items()):
        if key[0] != str(path):
            continue
        fp = getattr(obj, "fp", None)
        if fp is None or fp.closed:
            continue
        offs.append(os.lseek(fp.fileno(), 0, os.SEEK_CUR))
    return offs


def check_scan_workers(path: str, members, workers: int, ctx) -> list[str]:
    """The scanner's order of events: list in the parent (archive stays open
    there), then read every member from fork-started workers."""
    from soniqboom.core import archive
    problems = []
    listed = archive.list_members(path, strict=True)
    expected = {}
    for name, data, _m in members:
        expected[name if name in listed else name + ".ahx"] = data
    if sorted(listed) != sorted(expected):
        return [f"{os.path.basename(path)}: listed {len(listed)} members, expected {len(expected)}"]
    # Deterministic probe: one worker reads a member far from the parent's
    # file position.  An inherited (shared) descriptor would move it.
    before = _parent_offsets(path)          # [] = nothing cached open: nothing to share
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        ex.submit(_worker_read_first, path).result()
    after = _parent_offsets(path)
    if before != after:
        problems.append(f"{os.path.basename(path)}: a worker's read moved the parent's "
                        f"file offset {before} -> {after} (inherited descriptor)")
    # Stress: every member, in parallel.
    bad = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for name, got, err in ex.map(_worker_read, [(path, n) for n in listed], chunksize=1):
            if err or got != expected[name]:
                bad.append(f"{name}: {err or 'wrong bytes'}")
    if bad:
        problems.append(f"{os.path.basename(path)}: {len(bad)}/{len(listed)} members failed "
                        f"in scan workers, e.g. {bad[:3]}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--members", type=int, default=160)
    ap.add_argument("--deadline", type=int, default=600)
    args = ap.parse_args(argv)
    if args.members < 2:
        ap.error("--members must be at least 2 (one stored, one -lh5- member)")
    if hasattr(signal, "SIGALRM"):
        def _deadline(_sig, _frame):
            print(f"ARCHIVE SELF-TEST FAILED: no result within {args.deadline} s "
                  "(a worker hung, or the host is far too slow)", file=sys.stderr, flush=True)
            for child in multiprocessing.active_children():
                child.kill()
            os._exit(1)
        signal.signal(signal.SIGALRM, _deadline)
        signal.alarm(args.deadline)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    methods = multiprocessing.get_all_start_methods()
    ctx = multiprocessing.get_context("fork" if "fork" in methods else None)
    members = fixture_members(args.members)
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sb_arcselftest_") as tmp:
        lha = os.path.join(tmp, "SELFTEST.LHA")
        with open(lha, "wb") as fh:
            fh.write(lha_archive(members))
        zpath = os.path.join(tmp, "selftest.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data, _m in members:
                zf.writestr(name.replace("\\", "/") + ".ahx", data)
        zmembers = [(n.replace("\\", "/") + ".ahx", d, m) for n, d, m in members]
        problems += check_lhafile(lha, members)
        problems += check_scan_workers(lha, members, args.workers, ctx)
        problems += check_scan_workers(zpath, zmembers, args.workers, ctx)
        size = os.path.getsize(lha)
    if problems:
        print("ARCHIVE SELF-TEST FAILED:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print(f"archive self-test passed: {len(members)} LHA members (-lh0-/-lh5-, "
          f"{size} bytes) + ZIP, read by {args.workers} {ctx.get_start_method()}-started workers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
