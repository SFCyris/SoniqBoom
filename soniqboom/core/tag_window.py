# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fetch just the tag bytes of a network-share audio file.

A tag read of a remote file (the GAME-tag re-read of "Read game names", the
remote cover backfill) must not download the audio.  Where the tags live is
format-deterministic, so the container's own layout is walked with small range
reads (``FileSource.read_partial`` / ``read_at``) and the tag bytes are handed
back as a compact, valid file of the same format for the extractor:

* MP3 → the ID3v2 tag (its size is in the 10-byte header) plus a little audio,
  for the MPEG frame header the extractor reads;
* FLAC → ``fLaC`` + STREAMINFO + VORBIS_COMMENT, found by walking the metadata
  block headers — a PICTURE block is stepped over, never read;
* MP4 / M4A → ``ftyp`` + an empty ``mdat`` + ``moov``, the ``moov`` found by
  walking the top-level atom headers (it can sit at either end of the file).

``tag_window`` returns ``None`` for any other format or an unexpected layout,
and the caller then reads the file whole, as before — as it does for a source
without real range reads (``supports_ranges``).  Blocking — run it in a
worker thread."""
from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

MP4_EXTS = frozenset({".m4a", ".mp4", ".m4b", ".m4p"})
MP4_FRONT = 256 * 1024           # ftyp + a fast-start moov header walk
MOOV_CAP = 32 * 1024 * 1024      # refuse a moov larger than this (covers long m4b)
MAX_HDR_READS = 16               # cap header range reads (corrupt-file guard)

_ID3_FRONT = 64 * 1024           # the ID3v2 header, and most whole tags
_ID3_AUDIO = 16 * 1024           # past the tag: the first MPEG frames
_FLAC_FRONT = 64 * 1024          # fLaC + STREAMINFO + usually VORBIS_COMMENT
_TAG_CAP = 64 * 1024 * 1024      # refuse a tag / block larger than this

# A permissive, valid ftyp, used only if the real one isn't in the front read
# (rare — ftyp is the first atom).  Broad compatible-brands so mutagen accepts
# it regardless of the source file's brand.
GENERIC_FTYP = struct.pack(">I", 32) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isomiso2mp41M4A "


def supports_ranges(source) -> bool:
    """Does ``source`` do real range reads?  The ``FileSource`` defaults read
    the whole file and slice it, so a window there would cost more than one
    whole read."""
    from soniqboom.core.filesource import FileSource
    cls = type(source)
    return (getattr(cls, "read_partial", None) is not FileSource.read_partial
            and getattr(cls, "read_at", None) is not FileSource.read_at)


def tag_window(source, path: str, ext: str, file_size: int = 0) -> bytes | None:
    """The compact tag file of remote ``path`` on ``source`` (see the module
    docstring), or None when ``ext`` has no window or the layout is not the
    expected one, or ``source`` has no real range reads (``supports_ranges``).
    Network errors propagate."""
    if not supports_ranges(source):
        return None
    ext = ext.lower()
    if ext == ".mp3":
        return id3_window(source, path)
    if ext == ".flac":
        return flac_window(source, path)
    if ext in MP4_EXTS:
        return mp4_window(source, path, file_size)
    return None


def _syncsafe(b: bytes) -> int | None:
    if len(b) != 4 or any(x & 0x80 for x in b):
        return None
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_size(hdr: bytes) -> int | None:
    """Total bytes of the ID3v2 tag whose 10-byte header is ``hdr`` (header,
    body and footer), or None when ``hdr`` is not an ID3v2 header."""
    if len(hdr) < 10 or hdr[:3] != b"ID3":
        return None
    size = _syncsafe(hdr[6:10])
    if size is None:
        return None
    return 10 + size + (10 if hdr[5] & 0x10 else 0)


def id3_window(source, path: str) -> bytes | None:
    """The front of an MP3 through its ID3v2 tag plus ``_ID3_AUDIO`` bytes.
    A file without an ID3v2 tag at the front gets the plain front read (it
    carries no TXXX frame the extractor could read)."""
    front = source.read_partial(path, _ID3_FRONT, lane="scan")
    tag = _id3_size(front)
    if tag is None:
        return front
    want = tag + _ID3_AUDIO
    if want > _TAG_CAP:
        return None
    if want <= len(front):
        return front[:want]
    return front + source.read_at(path, len(front), want - len(front), lane="scan")


def flac_window(source, path: str) -> bytes | None:
    """``fLaC`` + STREAMINFO + VORBIS_COMMENT (flagged as the last block) of
    a FLAC file.  Only block HEADERS are read past the front; the comment
    block's body is fetched on its own.  A leading ID3v2 tag (which some
    taggers add) is stepped over."""
    front = source.read_partial(path, _FLAC_FRONT, lane="scan")
    reads = [0]

    def read(off: int, n: int) -> bytes:
        if off + n <= len(front):
            return front[off:off + n]
        reads[0] += 1
        if reads[0] > MAX_HDR_READS:
            return b""
        return source.read_at(path, off, n, lane="scan")

    off = _id3_size(front) or 0
    if read(off, 4) != b"fLaC":
        return None
    off += 4
    streaminfo: bytes | None = None
    comment: bytes | None = None
    for _ in range(256):
        hdr = read(off, 4)
        if len(hdr) < 4:
            return None
        last = bool(hdr[0] & 0x80)
        btype = hdr[0] & 0x7F
        blen = int.from_bytes(hdr[1:4], "big")
        if btype == 127:                                # invalid block type
            return None
        if btype in (0, 4):
            if blen > _TAG_CAP:
                return None
            body = read(off + 4, blen)
            if len(body) < blen:
                return None
            if btype == 0:
                streaminfo = body
            else:
                comment = body
        off += 4 + blen
        if last or (streaminfo is not None and comment is not None):
            break
    else:
        return None
    if streaminfo is None:
        return None
    out = bytearray(b"fLaC")
    blocks = [(0, streaminfo)] + ([(4, comment)] if comment is not None else [])
    for i, (btype, body) in enumerate(blocks):
        flag = 0x80 if i == len(blocks) - 1 else 0
        out += bytes([flag | btype]) + len(body).to_bytes(3, "big") + body
    return bytes(out)


def locate_moov(read_hdr, file_size: int):
    """Walk top-level MP4 atoms via ``read_hdr(offset) -> up to 16 bytes``.

    Returns (moov_offset, moov_size) or None.  Reads only atom HEADERS, never
    atom bodies — so finding a ``moov`` at the END of the file costs a handful
    of 16-byte range reads, not the whole ``mdat``.
    """
    off = 0
    guard = 0
    while off + 8 <= file_size and guard < 256:
        guard += 1
        hdr = read_hdr(off)
        if len(hdr) < 8:
            return None
        size = int.from_bytes(hdr[0:4], "big")
        typ = hdr[4:8]
        hlen = 8
        if size == 1:                           # 64-bit extended size
            if len(hdr) < 16:
                return None
            size = int.from_bytes(hdr[8:16], "big")
            hlen = 16
        elif size == 0:                         # atom extends to EOF
            size = file_size - off
        if typ == b"moov":
            return off, size
        if size < hlen:
            return None                         # malformed (also kills size==0 walks)
        off += size
    return None


def mp4_window(source, path: str, file_size: int = 0) -> bytes | None:
    """``ftyp`` + an empty ``mdat`` + ``moov`` of an MP4 file: mutagen walks
    atoms sequentially and reads only ``ftyp`` and ``moov`` for tags, so the
    audio is never fetched."""
    front = source.read_at(path, 0, MP4_FRONT, lane="scan")
    if not front or len(front) < 8:
        return None
    if not file_size:
        try:
            file_size = max(len(front), source.stat(path).size or len(front))
        except Exception:                               # noqa: BLE001
            file_size = len(front)

    hdr_reads = [0]

    def read_hdr(off: int) -> bytes:
        if off + 16 <= len(front):
            return front[off:off + 16]
        hdr_reads[0] += 1
        if hdr_reads[0] > MAX_HDR_READS:      # corrupt / pathological layout
            return b""
        return source.read_at(path, off, 16, lane="scan")

    loc = locate_moov(read_hdr, file_size)
    if loc is None:
        return None
    moov_off, moov_size = loc
    if moov_size <= 0:
        return None
    if moov_size > MOOV_CAP:
        # Refuse a pathologically large moov rather than CLAMP it — a truncated
        # moov is a corrupt atom mutagen can't parse.
        log.debug("tag-window: moov %d bytes > cap for %s — skipping", moov_size, path)
        return None

    if moov_off + moov_size <= len(front):
        moov_bytes = front[moov_off:moov_off + moov_size]
    else:
        moov_bytes = source.read_at(path, moov_off, moov_size, lane="scan")
    if not moov_bytes or len(moov_bytes) < 8:
        return None

    # ftyp (the first atom) taken from the front read — but only if it really IS
    # an ftyp; otherwise fall back to a generic one rather than slicing garbage.
    ftyp_size = int.from_bytes(front[0:4], "big")
    if 8 <= ftyp_size <= len(front) and front[4:8] == b"ftyp":
        ftyp = front[0:ftyp_size]
    else:
        ftyp = GENERIC_FTYP
    return ftyp + b"\x00\x00\x00\x08mdat" + moov_bytes
