"""XPK unpacking for Amiga modules packed with the XPK-SQSH cruncher.

uade refuses an XPK-packed module ("The file is SQSH packed. Please depack
first"), so a packed module is unpacked before it is read or rendered
(``unpack``).  SQSH is the method modules were usually packed with; any other
XPK method raises :class:`XpkUnsupported`.

The SQSH decoder is a port of libxmp's ``src/depackers/unsqsh.c``:

    XPK-SQSH depacker
    Algorithm from the portable decruncher by Bert Jahn (24.12.97)
    Checksum added by Sipos Attila <h430827@stud.u-szeged.hu>
    Rewritten for libxmp by Claudio Matsuoka

    Copyright (C) 2013-2026 Claudio Matsuoka

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
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_MAX_LEN = 0x100000          # libxmp's bound on the packed and unpacked sizes

# Literal bit widths, by the previous width and a 3-bit code (``unsqsh.c``).
_CTABLE = (
    2, 3, 4, 5, 6, 7, 8, 0,
    3, 2, 4, 5, 6, 7, 8, 0,
    4, 3, 5, 2, 6, 7, 8, 0,
    5, 4, 6, 2, 3, 7, 8, 0,
    6, 5, 7, 2, 3, 4, 8, 0,
    7, 6, 8, 2, 3, 4, 5, 0,
    8, 7, 6, 2, 3, 4, 5, 0,
)


class XpkError(ValueError):
    """The XPK data is damaged or truncated."""


class XpkUnsupported(XpkError):
    """Packed with an XPK method other than SQSH."""

    def __init__(self, method: str):
        super().__init__(f"XPK method {method!r} is not supported")
        self.method = method


def xpk_method(data: bytes) -> str | None:
    """The XPK method of ``data`` ("SQSH", "NUKE", …), or None when it is no
    XPK file (it doesn't start with "XPKF")."""
    if len(data) < 12 or data[:4] != b"XPKF":
        return None
    return data[8:12].decode("latin-1").strip() or "?"


def unpack(data: bytes) -> bytes:
    """``data`` unpacked when it is XPK-packed, else ``data`` unchanged.

    Raises :class:`XpkUnsupported` for an XPK method other than SQSH and
    :class:`XpkError` for damaged or truncated packed data (a block checksum
    that doesn't match, or an unpacked size other than the header's)."""
    method = xpk_method(data)
    if method is None:
        return data
    if method != "SQSH":
        raise XpkUnsupported(method)
    try:
        return _unsqsh_file(data)
    except IndexError as exc:            # a read or write past a buffer
        raise XpkError("damaged SQSH data") from exc


def unpack_file(path: Path) -> bytes:
    """``path``'s content, unpacked when it is XPK-packed (``unpack``) — only
    the bytes the XPK header accounts for are read, after its size bounds are
    checked.  OSError when the file can't be read."""
    with open(path, "rb") as fh:
        head = fh.read(16)
        if xpk_method(head) is None:
            return head + fh.read()
        if len(head) < 16:
            raise XpkError("truncated XPK header")
        srclen = _be32(head, 4)
        if srclen <= 8 or srclen > _MAX_LEN:
            raise XpkError("bad XPK header")
        return unpack(head + fh.read(srclen - 8))


def write_unpacked(path: Path, data: bytes, companion_names=()) -> Path:
    """Write ``data`` (``path`` unpacked) as ``<new temp folder>/<path's
    name>``, with symlinks to the files of ``path``'s folder named (case-
    insensitively) in ``companion_names`` — a player looks for its sample
    half beside the module.  Returns the new path; the caller removes its
    folder.  ``path``'s own folder is never written to."""
    path = Path(path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="sb-xpk-"))
    out = tmp_dir / path.name
    try:
        out.write_bytes(data)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    wanted = {n.lower() for n in companion_names}
    if wanted:
        try:
            entries = list(os.scandir(path.absolute().parent))
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.lower() in wanted and entry.name != out.name:
                try:
                    (tmp_dir / entry.name).symlink_to(entry.path)
                except OSError:
                    pass                     # e.g. two names differing in case only
    return out


def _be16(b, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def _be32(b, i: int) -> int:
    return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]


def _unsqsh_file(data: bytes) -> bytes:
    # decrunch_sqsh: "XPKF", packed length, "SQSH", unpacked length, then the
    # rest of the stream (srclen - 8 bytes) into a zero-padded buffer.
    srclen = _be32(data, 4)
    destlen = _be32(data, 12)
    if srclen <= 8 or srclen > _MAX_LEN or destlen > _MAX_LEN:
        raise XpkError("bad XPK header")
    body = data[16:16 + srclen - 8]
    if len(body) != srclen - 8:
        raise XpkError("truncated XPK data")
    src = bytearray(body) + bytes(16)       # calloc(srclen + 3), plus read slack
    dest = bytearray(destlen + 100)
    if _unsqsh(src, srclen, dest, destlen) != destlen:
        raise XpkError("damaged SQSH data")
    return bytes(dest[:destlen])


def _unsqsh(src: bytearray, srclen: int, dest: bytearray, destlen: int) -> int:
    remaining = destlen
    decrunched = 0
    d = 0                                    # io.dest
    c = 20                                   # past the rest of the XPK header
    while remaining > 0:
        if c + 8 > srclen:
            raise XpkError("truncated SQSH chunk")
        kind = src[c]
        c += 2                               # type, header checksum
        chunk_sum = _be16(src, c)
        c += 2
        packed = _be16(src, c)
        c += 2
        unpacked = _be16(src, c)
        c += 2
        if packed <= 0 or unpacked <= 0:
            raise XpkError("bad SQSH chunk sizes")
        if c + packed + 3 > srclen:
            raise XpkError("truncated SQSH chunk")
        io_src = c + 2
        # The chunk checksum: XOR of its big-endian longwords, with the three
        # bytes after the packed data counted as zero.
        words = (packed + 3) >> 2
        blk = bytearray(src[c:c + words * 4])
        for i in range(packed, min(packed + 3, len(blk))):
            blk[i] = 0
        s = 0
        for i in range(0, words * 4, 4):
            s ^= _be32(blk, i)
        if ((s ^ (s >> 16)) & 0xFFFF) != chunk_sum:
            return decrunched
        if kind == 0:                        # a stored chunk
            decrunched += packed
            if decrunched > destlen:
                raise XpkError("SQSH data longer than its header says")
            dest[d:d + packed] = src[c:c + packed]
            d += packed
            c += packed
            remaining -= packed
            continue
        if kind != 1:
            return decrunched
        remaining -= unpacked
        decrunched += unpacked
        if decrunched > destlen:
            raise XpkError("SQSH data longer than its header says")
        c += (packed + 3) & 0xFFFC
        dest_end = d + unpacked
        _unsqsh_block(src, io_src, packed << 3, dest, d, dest_end)
        d = dest_end
    return decrunched


def _unsqsh_block(src: bytearray, pos: int, nbits: int, dest: bytearray,
                  d: int, dest_end: int) -> None:
    """One packed chunk: ``src[pos:]`` (``nbits`` bits) into ``dest[d:dest_end]``."""
    offs = 0

    def get_bits(count: int) -> int:
        nonlocal offs
        if count > nbits - offs:
            return -1
        i = pos + (offs >> 3)
        r = (src[i] << 16) | (src[i + 1] << 8) | src[i + 2]
        r = (r << (offs & 7)) & 0xFFFFFF
        r >>= 24 - count
        offs += count
        return r

    def get_bits_final(count: int) -> int:
        # Signed: the 24 bits shifted to the top of a 32-bit int, then an
        # arithmetic shift down (``XMP_ASL`` then ``>>`` in the C).
        nonlocal offs
        i = pos + (offs >> 3)
        r = (src[i] << 16) | (src[i + 1] << 8) | src[i + 2]
        r = (r << ((offs & 7) + 8)) & 0xFFFFFFFF
        if r & 0x80000000:
            r -= 1 << 32
        r >>= 32 - count
        offs += count
        return r

    def copy_data(d1: int) -> int:
        nonlocal d, data
        if get_bits(1) == 0:
            copy_len = get_bits(1) + 2
        elif get_bits(1) == 0:
            copy_len = get_bits(1) + 4
        elif get_bits(1) == 0:
            copy_len = get_bits(1) + 6
        elif get_bits(1) == 0:
            copy_len = get_bits(3) + 8
        else:
            copy_len = get_bits(5) + 16
        r = get_bits(1)
        if copy_len < 0 or r < 0:
            raise XpkError("damaged SQSH data")
        if r == 0:
            r = get_bits(1)
            if r < 0:
                raise XpkError("damaged SQSH data")
            if r == 0:
                count, dest_offset = 8, 0
            else:
                count, dest_offset = 14, -0x1100
        else:
            count, dest_offset = 12, -0x100
        copy_len -= 3
        if copy_len >= 0:
            if copy_len != 0:
                d1 -= 1
            d1 -= 1
            if d1 < 0:
                d1 = 0
        copy_len += 2
        r = get_bits(count)
        if r < 0:
            raise XpkError("damaged SQSH data")
        cs = d + dest_offset - r - 1
        if cs < 0 or cs + copy_len >= dest_end:
            raise XpkError("damaged SQSH data")
        for _ in range(copy_len + 1):        # byte by byte: the copy may overlap
            dest[d] = dest[cs]
            d += 1
            cs += 1
        data = dest[cs - 1]
        return d1

    data = src[pos]
    pos += 1
    dest[d] = data
    d += 1
    d1 = d2 = old_count = 0
    while True:
        r = get_bits(1)
        if r < 0:
            raise XpkError("damaged SQSH data")
        if d1 < 8:
            if r:
                d1 = copy_data(d1)
                d2 -= d2 >> 3
                if d >= dest_end:
                    return
                continue
            unpack_len, count = 0, 8
        else:
            if r:
                count = 8
                if count == old_count:
                    if d2 >= 20:
                        unpack_len = 1
                        d2 += 8
                    else:
                        unpack_len = 0
                else:
                    count = old_count
                    unpack_len = 4
                    d2 += 8
            else:
                r = get_bits(1)
                if r < 0:
                    raise XpkError("damaged SQSH data")
                if r == 0:
                    d1 = copy_data(d1)
                    d2 -= d2 >> 3
                    if d >= dest_end:
                        return
                    continue
                r = get_bits(1)
                if r < 0:
                    raise XpkError("damaged SQSH data")
                if r == 0:
                    count = 2
                else:
                    r = get_bits(1)
                    if r < 0:
                        raise XpkError("damaged SQSH data")
                    if r:
                        offs -= 1
                        count = get_bits(3)
                        if count < 0:
                            raise XpkError("damaged SQSH data")
                    else:
                        count = 3
                idx = 8 * old_count + count - 17
                if idx < 0:                  # Python would index from the end
                    raise XpkError("damaged SQSH data")
                count = _CTABLE[idx]
                if count != 8:
                    unpack_len = 4
                    d2 += 8
                elif d2 >= 20:
                    unpack_len = 1
                    d2 += 8
                else:
                    unpack_len = 0
        if count * (unpack_len + 2) > nbits - offs:
            raise XpkError("damaged SQSH data")
        for _ in range(unpack_len + 1):
            data -= get_bits_final(count)
            dest[d] = data & 0xFF
            d += 1
        if d1 != 31:
            d1 += 1
        old_count = count
        d2 -= d2 >> 3
        if d >= dest_end:
            return
