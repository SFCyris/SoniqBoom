"""Read playable music files out of vintage **disk images**.

SoniqBoom already surfaces tunes stored inside ZIP archives via the
``container::member`` virtual-path scheme.  This module extends the same idea
to floppy-disk images from the demoscene era:

* **Commodore** ``.d64`` / ``.d71`` / ``.d81`` (1541 / 1571 / 1581) — surfaces
  embedded **SID** tunes (``PSID``/``RSID``).
* **Amiga** ``.adf`` (OFS/FFS floppy) — surfaces embedded **tracker** modules
  (ProTracker ``.mod`` and friends, plus AHX/HVL and OctaMED).

The public surface mirrors what the ZIP path needs:

    is_disk_image(path)          -> bool
    list_members(path)           -> list[str]      # e.g. ['THE RUNNER.sid']
    read_member(path, member)    -> bytes          # the raw .sid/.mod bytes

A *member name* always carries the extension SoniqBoom needs to pick the right
renderer (``.sid`` → sidplayfp, ``.mod`` → openmpt …) once the bytes are spilled
to a temp file by the caller.

Design notes
------------
* These images are tiny (≤ ~1.8 MB) and hold a handful of files, so the
  enumeration reads the whole image once and extracts every playable member's
  bytes in one pass.  ``read_member`` re-runs that pass and returns one member —
  the stream layer's extraction cache means a member is only read on a cache
  miss, so the simple stateless design is fine and guarantees that
  ``list_members`` and ``read_member`` can never disagree on naming.
* Detection is by **content magic**, not extension — a C64 disk stores a SID as
  a ``PRG``/``SEQ`` (no ``.sid`` suffix), an Amiga disk names a module
  ``mod.title`` (prefix, not suffix).  We sniff the bytes.
* Everything is best-effort: a malformed image yields ``{}`` rather than raising,
  so one bad disk never aborts a scan.
"""

from __future__ import annotations

import logging
import os
import re
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# Outer-container extensions this module handles.  The scanner/stream layers
# branch on these the same way they branch on ``.zip``.
DISK_IMAGE_EXTS: tuple[str, ...] = (".d64", ".d71", ".d81", ".adf")


def is_disk_image(path) -> bool:
    """True if *path*'s extension is a disk-image format we can crack open."""
    return str(path).lower().endswith(DISK_IMAGE_EXTS)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def list_members(path) -> list[str]:
    """Return the names of playable members inside the disk image at *path*.

    Names already carry a renderer-friendly extension (``.sid``/``.mod``/…).
    Returns ``[]`` for a non-image, unreadable, or empty/unsupported image.
    """
    try:
        return list(_enumerate(path).keys())
    except Exception as exc:  # never let one bad disk abort a scan
        log.debug("disk-image enumerate failed for %s: %s", path, exc)
        return []


def read_member(path, member: str) -> bytes:
    """Return the raw bytes of *member* inside the disk image at *path*.

    Raises ``KeyError`` if the member is not present (mirrors a missing zip
    entry) and lets genuine IO errors propagate to the caller.
    """
    members = _enumerate(path)
    if member not in members:
        raise KeyError(f"{member!r} not found in disk image {path}")
    return members[member]


def _enumerate(path) -> dict[str, bytes]:
    """``{member_name: file_bytes}`` for every playable member in the image."""
    p = str(path)
    low = p.lower()
    with open(p, "rb") as fh:
        data = fh.read()
    if low.endswith((".d64", ".d71", ".d81")):
        return _enum_cbm(data)
    if low.endswith(".adf"):
        return _enum_adf(data)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# SID detection (shared)
# ─────────────────────────────────────────────────────────────────────────────
_SID_MAGIC = (b"PSID", b"RSID")


def _valid_psid(body: bytes) -> bool:
    """Strict gate: does *body* start with a real PSID/RSID header?

    Checks magic, version (1..4), the canonical data-offset (0x76 for v1,
    0x7C for v2+), and a sane song count (1..256).  Together these make a
    coincidental "PSID" byte run in arbitrary 6502 program code astronomically
    unlikely to pass (~1e-11 per magic occurrence).
    """
    if len(body) < 0x7C or body[0:4] not in _SID_MAGIC:
        return False
    version = struct.unpack(">H", body[0x04:0x06])[0]
    data_off = struct.unpack(">H", body[0x06:0x08])[0]
    songs = struct.unpack(">H", body[0x0E:0x10])[0]
    if version not in (1, 2, 3, 4):
        return False
    if data_off not in (0x76, 0x7C):
        return False
    if not (1 <= songs <= 256):
        return False
    # NB: deliberately NO check on the name field being ASCII — that would
    # reject real PSIDs with PETSCII / non-Latin tune names.  The three numeric
    # gates above already make false positives astronomically unlikely.
    return True


def _extract_sid(content: bytes) -> bytes | None:
    """Return clean standalone ``.sid`` bytes if *content* is or embeds a SID.

    Handles the storage conventions seen on C64 disks:

    * a raw ``.sid`` (magic at offset 0, e.g. stored as a ``SEQ``),
    * a PRG-wrapped ``.sid`` (2-byte load address, then the magic),
    * a **self-playing music PRG** with an embedded PSID further in — the
      common music-compo case (e.g. a BASIC ``SYS`` stub + player + tune).  We
      carve the tune out from its magic to end-of-file; the strict
      :func:`_valid_psid` gate is what authenticates it, so the scan position is
      just where we slice.  A trailing player tail (if any) is harmless — the
      SID engine drives playback from the tune's own init/play vectors.
    """
    if len(content) < 0x7C:
        return None
    for mg in _SID_MAGIC:
        start = 0
        while True:
            i = content.find(mg, start)
            if i < 0:
                break
            body = content[i:]
            if _valid_psid(body):
                return body
            start = i + 1
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Commodore .d64 / .d71 / .d81
# ─────────────────────────────────────────────────────────────────────────────
# Disk geometry keyed by image size in bytes.  Each entry yields a function
# (track -> sectors-on-that-track) plus the directory track and sector size.
def _cbm_geometry(size: int):
    """Return (sectors_per_track_fn, dir_track, dir_sector, sector_size) or None."""
    # 1541 (.d64): 35 tracks (optionally 40), 256-byte sectors, dir on track 18.
    def d64_spt(t: int) -> int:
        if 1 <= t <= 17:
            return 21
        if 18 <= t <= 24:
            return 19
        if 25 <= t <= 30:
            return 18
        return 17  # 31..40

    # 1571 (.d71): double-sided 1541 — 70 tracks, side 2 (36..70) mirrors the
    # 1541 zone pattern; directory still on track 18.
    def d71_spt(t: int) -> int:
        return d64_spt(t if t <= 35 else t - 35)

    # 1581 (.d81): 80 tracks, a flat 40 sectors/track, dir on track 40.
    def d81_spt(_t: int) -> int:
        return 40

    if size in (174848, 175531):          # 35-track .d64 (+ error info)
        return d64_spt, 18, 1, 256
    if size in (196608, 197376):          # 40-track .d64
        return d64_spt, 18, 1, 256
    if size in (349696, 351062):          # 70-track .d71
        return d71_spt, 18, 1, 256
    if size in (819200, 822400):          # 80-track .d81
        return d81_spt, 40, 3, 256
    return None


def _enum_cbm(data: bytes) -> dict[str, bytes]:
    geo = _cbm_geometry(len(data))
    if geo is None:
        return {}
    spt, dir_track, dir_sector, secsize = geo

    def offset(track: int, sector: int) -> int:
        off = 0
        for tt in range(1, track):
            off += spt(tt) * secsize
        return off + sector * secsize

    def sector_bytes(track: int, sector: int) -> bytes | None:
        if track < 1 or sector < 0 or sector >= spt(track):
            return None
        o = offset(track, sector)
        if o + secsize > len(data):
            return None
        return data[o:o + secsize]

    def read_chain(track: int, sector: int) -> bytes:
        """Follow a CBM file's track/sector linked list into a byte string."""
        out = bytearray()
        seen: set[tuple[int, int]] = set()
        while track:
            if (track, sector) in seen:        # corrupt/looping chain
                break
            seen.add((track, sector))
            sec = sector_bytes(track, sector)
            if sec is None:
                break
            nt, ns = sec[0], sec[1]
            if nt == 0:
                # Last sector: ns holds the index of the last used byte.
                out += sec[2:ns + 1] if ns >= 2 else b""
                break
            out += sec[2:secsize]
            track, sector = nt, ns
            if len(out) > 4 * 1024 * 1024:     # guard runaway
                break
        return bytes(out)

    members: dict[str, bytes] = {}
    used: set[str] = set()

    # Walk the directory chain (each dir sector links to the next).
    t, s = dir_track, dir_sector
    seen: set[tuple[int, int]] = set()
    while t and (t, s) not in seen:
        seen.add((t, s))
        sec = sector_bytes(t, s)
        if sec is None:
            break
        nt, ns = sec[0], sec[1]
        for e in range(8):                     # 8 × 32-byte entries per sector
            E = e * 32
            ftype = sec[E + 2]
            typ = ftype & 0x0F                 # 0=DEL 1=SEQ 2=PRG 3=USR 4=REL
            if typ == 0:                       # scratched / scroller-art entry
                continue
            ftrack, fsector = sec[E + 3], sec[E + 4]
            if not ftrack:
                continue
            raw_name = sec[E + 5:E + 5 + 16].split(b"\xa0")[0]
            name = _clean_component(
                "".join(chr(c) if 32 <= c < 127 else "_" for c in raw_name)
            )
            content = read_chain(ftrack, fsector)
            sid = _extract_sid(content)
            if sid is None:
                continue
            base = (name or f"track{ftrack:02d}_{fsector:02d}").replace("/", "_")
            member = _unique(f"{base}.sid", used)
            members[member] = sid
        t, s = nt, ns

    return members


# ─────────────────────────────────────────────────────────────────────────────
# Amiga .adf  (OFS / FFS floppy filesystem)
# ─────────────────────────────────────────────────────────────────────────────
_BSIZE = 512

# Tracker module signatures.  ProTracker/clones carry a 4-byte tag at offset
# 1080; AHX/HVL/MED carry a tag at offset 0.  The tag set is wide: classic
# ``M.K.``/``FLT4``, single-digit ``xCHN`` (2..9 ch), and FastTracker /
# TakeTracker double-digit ``xxCH``/``xxCN`` (10..32 ch), plus a few exotics.
_MOD_TAG_RE = re.compile(
    rb"^(M\.K\.|M!K!|M&K!|N\.T\.|FLT[48]|EXO4|FA0[468]|"
    rb"[1-9]CHN|[1-9][0-9]C[HN]|OCTA|OKTA|CD81|TDZ[1-9])$"
)


def _be32(b: bytes, off: int) -> int:
    return struct.unpack(">I", b[off:off + 4])[0]


def _be32s(b: bytes, off: int) -> int:
    return struct.unpack(">i", b[off:off + 4])[0]


def _tracker_member(path: str, content: bytes) -> tuple[str, bytes] | None:
    """If *content* looks like a playable Amiga module, return (name, bytes)."""
    n = len(content)
    if n < 8:
        return None
    base = path.rsplit("/", 1)[-1].lower()

    ext: str | None = None
    if n >= 1084 and _MOD_TAG_RE.match(content[1080:1084]):
        ext = ".mod"                              # ProTracker & 2..32-ch clones
    elif content[:3] == b"THX" and content[3:4] in (b"\x00", b"\x01"):
        ext = ".ahx"                              # AHX
    elif content[:3] == b"HVL" and content[3:4] in (b"\x00", b"\x01"):
        ext = ".hvl"                              # HivelyTracker (HVL\x00 / \x01)
    elif content[:3] == b"MMD" and content[3:4] in (b"0", b"1", b"2", b"3"):
        ext = ".med"                              # OctaMED / MED
    elif base.startswith("mod.") and n >= 1084:
        # Amiga naming convention: ``mod.title`` is a ProTracker mod even when
        # the 15-instrument Soundtracker variant carries no 1080 tag.
        ext = ".mod"
    if ext is None:
        return None
    return (_with_ext(path, ext), content)


def _enum_adf(data: bytes) -> dict[str, bytes]:
    total_blocks = len(data) // _BSIZE
    if total_blocks not in (1760, 3520):       # DD (880 KB) / HD (1.76 MB)
        return {}
    # Filesystem flavour from the boot block: "DOS\x00"=OFS, "DOS\x01"=FFS …
    boot = data[0:4]
    if boot[:3] != b"DOS":
        return {}
    ffs = bool(boot[3] & 1)
    root_block = total_blocks // 2

    def block(n: int) -> bytes | None:
        o = n * _BSIZE
        if n < 0 or o + _BSIZE > len(data):
            return None
        return data[o:o + _BSIZE]

    def bcpl_name(b: bytes, off: int) -> str:
        ln = b[off]
        if ln > 30:
            ln = 30
        return b[off + 1:off + 1 + ln].decode("latin-1", "replace")

    def read_file(header_blk: int) -> bytes:
        blk = block(header_blk)
        if blk is None:
            return b""
        size = _be32(blk, 0x144)               # file size in bytes
        out = bytearray()
        cur = header_blk
        guard = 0
        while cur and guard < total_blocks:
            guard += 1
            b = block(cur)
            if b is None:
                break
            high_seq = _be32(b, 0x08)
            for i in range(min(high_seq, 72)):
                # Data-block pointers are stored in reverse: first at BSIZE-204,
                # decreasing by 4.
                ptr = _be32(b, _BSIZE - 204 - i * 4)
                if not ptr:
                    continue
                db = block(ptr)
                if db is None:
                    continue
                if ffs:
                    out += db
                else:                          # OFS: 24-byte data-block header
                    dsize = _be32(db, 0x0C)
                    out += db[24:24 + min(dsize, _BSIZE - 24)]
                if len(out) >= size:
                    break
            cur = _be32(b, _BSIZE - 8)          # FileExtBlock chain
        return bytes(out[:size]) if size else bytes(out)

    members: dict[str, bytes] = {}
    used: set[str] = set()
    visited: set[int] = set()

    def walk(dir_block: int, prefix: str, depth: int) -> None:
        if dir_block in visited or depth > 32:
            return
        visited.add(dir_block)
        blk = block(dir_block)
        if blk is None:
            return
        # Hash table: 72 longs at offset 24.
        for h in range(72):
            entry = _be32(blk, 24 + h * 4)
            chain_guard = 0
            while entry and chain_guard < 4096:
                chain_guard += 1
                eb = block(entry)
                if eb is None:
                    break
                sec_type = _be32s(eb, _BSIZE - 4)
                name = _clean_component(bcpl_name(eb, _BSIZE - 80)) or f"file{entry}"
                full = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
                if sec_type == 2:                      # ST_USERDIR
                    walk(entry, full, depth + 1)
                elif sec_type == -3:                   # ST_FILE
                    content = read_file(entry)
                    hit = _tracker_member(full, content)
                    if hit is not None:
                        members[_unique(hit[0], used)] = hit[1]
                entry = _be32(eb, _BSIZE - 16)         # hash_chain
        # FFS/OFS dirs don't chain beyond the hash table at this level.

    walk(root_block, "", 0)
    return members


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _clean_component(s: str) -> str:
    """Neutralise characters in an UNTRUSTED member name that would corrupt the
    ``container::member`` path or stored-string consumers (DB / JSON / logs):
    drop control chars + NUL, and map ``:`` / ``\\`` (which could forge a
    ``::`` boundary or a path separator) to ``_``."""
    out = []
    for ch in s:
        c = ord(ch)
        if c == 0 or c < 32 or c == 127:
            continue
        out.append("_" if ch in (":", "\\") else ch)
    return "".join(out).strip()


def _with_ext(path: str, ext: str) -> str:
    """Ensure *path* ends with *ext* (case-insensitive), else append it."""
    return path if path.lower().endswith(ext) else path + ext


def _unique(name: str, used: set[str]) -> str:
    """De-duplicate member names within one image (``a.sid``, ``a (2).sid`` …)."""
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    i = 2
    while True:
        # Only treat as ``stem.ext`` when both sides are non-empty; a
        # leading-dot name (``.sid``) keeps its whole form plus a counter.
        cand = f"{stem} ({i}).{ext}" if (dot and stem) else f"{name} ({i})"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


# ─────────────────────────────────────────────────────────────────────────────
# self-test:  python -m soniqboom.core.diskimage <image> [member]
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m soniqboom.core.diskimage <image> [member]")
        raise SystemExit(2)
    img = sys.argv[1]
    if len(sys.argv) >= 3:
        data = read_member(img, sys.argv[2])
        sys.stdout.buffer.write(data)
    else:
        mem = list_members(img)
        print(f"{os.path.basename(img)}: {len(mem)} playable member(s)")
        for m in mem:
            print(f"   {m}")
