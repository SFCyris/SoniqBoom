# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repairs for known damaged Amiga rips, and a check for Synth Dream tunes
whose sample bank is missing.

Some rips in circulation had zero bytes overwritten with one fixed byte
(Synth Dream "No Buddies Land": $FC, its sample banks $FC or $05; Maximum
Effect "'Allo 'Allo!": $2D, "Dinosaur Detective": $D4; Peter Verswyvelen
"Ziriax": $1A).  The players refuse them, crash on them or play noise.  Each
known file has an entry in ``_TABLE``: its size and SHA-256, the byte runs
that go back to 0 (each run holds only the file's fill byte, checked before
it is changed) and the SHA-256 the repaired bytes must have.  A file whose
size, digest or bytes differ from its entry is used as it is.  The runs lie
in the structures the players read (header offsets, instrument and sample
tables, track and pattern words), where only 0 is a value the player
accepts or, for fields it accepts anything in, the only value clean files
of the format use; and in samples that other files hold undamaged.  Bytes
that could still be either value are left as they are.

``repaired`` gives the bytes uade should see instead of a file's own;
``xpk.stage_for_uade`` writes them into its temp folder (the file itself is
never written).  One entry cuts a file instead: Cryptoburners "pack5.hipc"
is "pack2.hipc" followed by part of "pack3.hipc"; cut after pack2 it plays
as pack2 does.

``coso_with_bank`` gives a Hippel COSO song without instruments of its own
(its instrument table would start at or past its end) the instruments of
the shared file it plays with (``uade_formats.coso_bank_for``: Cryptoburners
"pack2", "pack4" and "pack5" play with "pack3").  A song whose tune table
is cut short keeps the tunes it still has, and ``known_defect`` marks it
``partial`` (pack2, and so pack5: one of four tunes is left).

Only files whose name has one of ``_TOKENS`` as its first or last part and
whose size is one of the table's sizes are read and hashed; a file with
another name isn't touched, one with such a name costs a ``stat``.  The
COSO songs ``uade_formats.coso_bank_for`` names are read whatever their size
(up to ``_COSO_MAX_BYTES``), and their folder is listed for the bank.

``synthdream_refusal`` checks a Synth Dream module (``SDR.`` prefix) against
the sample bank uade loads with it (``SMP.<name>``, else ``SMP.set``): its
sample instruments must start at the samples (IFF ``BODY`` data) of that
bank.  When fewer than half do, the tune is refused with the reason
``SAMPLE_BANK_MISSING``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_TOKENS = frozenset({"sdr", "smp", "max", "pvp", "hipc"})
_SDR_MAX_BYTES = 256 * 1024          # a Synth Dream module (the largest known: 12 KB)
_BANK_MAX_BYTES = 4 * 1024 * 1024    # its sample bank (the largest known: 96 KB)
_FILL_MIN = 4                        # times a fill byte shows in a damaged first entry, at least
SAMPLE_BANK_MISSING = ("the sample bank for this tune is missing (its instruments don't "
                       "match the SMP file beside it)")


class Unplayable(Exception):
    """A file uade can't play for a reason found before it ran (``why``)."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


@dataclass(frozen=True)
class _Entry:
    name: str                 # the file's name where it is known (for logs only)
    size: int
    fill: int                 # the byte its zeros were overwritten with
    sha256: str
    result_sha256: str
    runs: str = ""            # "offset[+length]" runs, both in hex, space separated
    truncate: "int | None" = None
    defect: "str | None" = None
    detail: "str | None" = None

    def patches(self) -> "list[tuple[int, bytes, bytes]]":
        """``(offset, expected old bytes, new bytes)`` for every run."""
        out = []
        for tok in self.runs.split():
            off, _, n = tok.partition("+")
            k = int(n, 16) if n else 1
            out.append((int(off, 16), bytes([self.fill]) * k, bytes(k)))
        return out


def _apply(entry: _Entry, data: bytes) -> "bytes | None":
    """``data`` repaired by ``entry``, or None when ``data`` isn't what the
    entry expects (another size, a run's bytes that aren't the fill byte, a
    result with another digest)."""
    if len(data) != entry.size:
        return None
    if entry.truncate is not None:
        out = data[:entry.truncate]
    else:
        buf = bytearray(data)
        for off, old, new in entry.patches():
            if buf[off:off + len(old)] != old:
                return None
            buf[off:off + len(new)] = new
        out = bytes(buf)
    if hashlib.sha256(out).hexdigest() != entry.result_sha256:
        return None
    return out


def _name_candidate(name: str) -> bool:
    low = name.lower()
    if "." not in low:
        return False
    return low.split(".", 1)[0] in _TOKENS or low.rsplit(".", 1)[-1] in _TOKENS


def _candidate(name: str, size: int) -> bool:
    return size in _SIZES and _name_candidate(name)


def _entry_for(path: Path) -> "tuple[_Entry, bytes] | None":
    """The table entry for the file at ``path`` and its bytes, or None (not
    a candidate, not in the table, unreadable)."""
    if not _name_candidate(path.name):
        return None
    try:
        size = os.stat(path).st_size
    except OSError:
        return None
    if size not in _SIZES:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(size + 1)
    except OSError:
        return None
    entry = _BY_SHA.get(hashlib.sha256(data).hexdigest())
    return (entry, data) if entry is not None else None


def repaired(path: "Path | str") -> "bytes | None":
    """The bytes uade should play instead of the file at ``path``: the file
    repaired (or cut) by its table entry, or None when it has none or the
    repair doesn't check out.  Never writes."""
    path = Path(path)
    hit = _entry_for(path)
    if hit is None:
        return None
    entry, data = hit
    out = _apply(entry, data)
    if out is None:
        log.warning("Known damaged rip %s (%s) did not match its repair; played as it is",
                    path.name, entry.name)
        return None
    log.debug("Playing %s repaired (%s)", path.name, entry.name)
    return out


def known_defect(path: "Path | str") -> "tuple[str, str] | None":
    """``(defect, detail)`` for a known rip that plays only in part once
    repaired ("partial"), else None: a table entry's defect, or a Hippel COSO
    song played with a shared instrument file whose tune table is cut short
    (``coso_tunes``)."""
    path = Path(path)
    hit = _entry_for(path)
    if hit is not None and hit[0].defect and _apply(*hit) is not None:
        return hit[0].defect, hit[0].detail or ""
    tunes = coso_song_tunes(path)
    if tunes is None or not 1 <= tunes[0] < tunes[1]:
        return None
    kept, declared = tunes
    return "partial", (f"only {kept} of its {declared} tunes "
                       f"{'is' if kept == 1 else 'are'} left in the file")


def _coso_song_bytes(path: Path) -> "bytes | None":
    """The bytes of the Hippel COSO song at ``path`` that plays with a shared
    instrument file (``uade_formats.coso_bank_for``), repaired when it is a
    known damaged rip — or None (another file, unreadable, too large)."""
    from soniqboom.core import uade_formats
    if uade_formats.coso_bank_for(path.name) is None:
        return None
    data = repaired(path)
    if data is not None:
        return data
    try:
        if os.stat(path).st_size > _COSO_MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def coso_song_tunes(path: "Path | str") -> "tuple[int, int] | None":
    """``(tunes left, tunes declared)`` of the Hippel COSO song at ``path``
    that plays with a shared instrument file and has none of its own
    (``coso_tunes``), else None.  Read from the song alone: the scan of a
    catalogue tune reads a copy without the file beside it."""
    data = _coso_song_bytes(Path(path))
    return coso_tunes(data) if data is not None else None


def coso_bank_missing(played: "Path | str", name: str) -> "str | None":
    """The shared instrument file a Hippel COSO song called ``name`` plays
    with, when ``played`` (the copy uade would play — staged, or the file
    itself) still has no instruments: the file wasn't beside it (not fetched
    yet, or a folder without it).  None otherwise."""
    from soniqboom.core import uade_formats
    bank = uade_formats.coso_bank_for(name)
    if bank is None:
        return None
    try:
        p = Path(played)
        if os.stat(p).st_size > _COSO_MAX_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    return bank if coso_needs_bank(data) else None


# ── Hippel COSO songs with a shared instrument file ─────────────────────────
# A COSO module: "COSO", seven offsets (frequency and volume sequences,
# patterns, tracks, tunes, instrument table, sample data), then "TFMX" and
# counts — the tune count at 48, the instrument count at 50.  A tune is 6
# bytes (start, end, speed), the table ends with 6 bytes of padding; an
# instrument is 10 (sample offset, length/2, loop start, loop length/2).

_COSO_MAX_BYTES = 1024 * 1024
_COSO_TUNE = 6
_COSO_INSTR = 10


def _coso_head(data: bytes) -> "tuple[tuple[int, ...], int, int] | None":
    """``(offsets, tunes, instruments)`` of a COSO module, else None."""
    if len(data) < 52 or data[:4] != b"COSO" or data[32:36] != b"TFMX":
        return None
    offs = struct.unpack(">7I", data[4:32])
    tunes, instr = struct.unpack(">HH", data[48:52])
    return offs, tunes, instr


def coso_tunes(data: bytes) -> "tuple[int, int] | None":
    """``(tunes left, tunes declared)`` of a COSO song without an instrument
    table of its own (``coso_needs_bank``), else None."""
    if not coso_needs_bank(data):
        return None
    offs, tunes, _instr = _coso_head(data)
    left = max(0, (len(data) - offs[4]) // _COSO_TUNE)
    return min(left, tunes), tunes


def coso_needs_bank(data: bytes) -> bool:
    """Is ``data`` a COSO song (it has tunes) whose instrument table would
    start at or past its end — instruments it must take from a shared file?"""
    head = _coso_head(data)
    if head is None:
        return False
    offs, tunes, instr = head
    return bool(tunes and instr and 52 <= offs[4] <= len(data) and offs[5] >= len(data))


def coso_with_bank(song: bytes, bank: bytes) -> "bytes | None":
    """``song`` (``coso_needs_bank``) with the instruments of ``bank`` (a
    COSO instrument file: no tunes, an instrument table and the sample data
    after it) appended and its offsets set to them; a tune table cut short
    keeps the whole tunes left (and says so in its tune count).  None when
    ``song`` doesn't need a bank or ``bank`` can't serve it (another kind of
    file, fewer instruments, a sample past its end)."""
    if not coso_needs_bank(song):
        return None
    offs, tunes, instr = _coso_head(song)
    b = _coso_head(bank)
    if b is None:
        return None
    b_offs, b_tunes, b_instr = b
    if b_tunes or b_offs[4] or b_instr < instr:
        return None                       # (a song itself, or too few instruments)
    info_at = b_offs[5]
    info_end = info_at + b_instr * _COSO_INSTR
    if not 52 <= info_at or info_end > len(bank):
        return None
    # The sample data follows the instrument table (pack3 states 0 for it).
    data_at = b_offs[6] if b_offs[6] >= info_end else info_end
    samples = bank[data_at:]
    table = bank[info_at:info_at + instr * _COSO_INSTR]
    for i in range(instr):
        off, half = struct.unpack(">IH", table[i * _COSO_INSTR:i * _COSO_INSTR + 6])
        if off + 2 * half > len(samples):
            return None
    left, _ = coso_tunes(song)
    if left < 1:
        return None
    if left == tunes and offs[5] <= len(song):
        body = bytearray(song[:offs[5]])
    else:
        body = bytearray(song[:offs[4] + left * _COSO_TUNE]) + bytes(_COSO_TUNE)
        struct.pack_into(">H", body, 48, left)
    struct.pack_into(">II", body, 24, len(body), len(body) + len(table))
    return bytes(body) + table + samples


def coso_bank_attached(path: "Path | str", data: "bytes | None" = None) -> "bytes | None":
    """The Hippel COSO song at ``path`` (or ``data``: its bytes, already
    repaired) with the instruments of the shared file beside it
    (``uade_formats.coso_bank_for``, matched ignoring case), or None — not
    such a song, the file isn't beside it, or it can't serve the song."""
    from soniqboom.core import uade_formats
    path = Path(path)
    want = uade_formats.coso_bank_for(path.name)
    if want is None:
        return None
    try:
        if data is None:
            if os.stat(path).st_size > _COSO_MAX_BYTES:
                return None
            data = path.read_bytes()
        if not coso_needs_bank(data):
            return None                   # (instruments of its own: played as it is)
        bank_path = next((Path(e.path) for e in os.scandir(path.absolute().parent)
                          if e.name.lower() == want), None)
        if bank_path is None or os.stat(bank_path).st_size > _COSO_MAX_BYTES:
            return None
        bank = bank_path.read_bytes()
    except OSError:
        return None
    out = coso_with_bank(data, bank)
    if out is None:
        log.warning("Hippel COSO %s: %s beside it can't serve as its instruments",
                    path.name, bank_path.name)
    return out


# ── Synth Dream sample banks ────────────────────────────────────────────────

def _bank_for(module: Path) -> "Path | None":
    """The sample bank the Synth Dream player loads with ``module``:
    ``SMP.<name>`` for ``SDR.<name>``, else ``SMP.set`` (names matched
    ignoring case), or None when neither is beside it."""
    name = module.name
    want = []
    if name[:4].lower() == "sdr.":
        want.append(("smp." + name[4:]).lower())
    want.append("smp.set")
    try:
        beside = {e.name.lower(): Path(e.path) for e in os.scandir(module.absolute().parent)}
    except OSError:
        return None
    for w in want:
        if w in beside:
            return beside[w]
    return None


def _fill_byte(entry: bytes) -> "int | None":
    """The byte a damaged rip's zeros were overwritten with, read off its
    first instrument entry (a value other than 0 there at least
    ``_FILL_MIN`` times), or None."""
    counts: dict = {}
    for v in entry:
        if v:
            counts[v] = counts.get(v, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    return best[0] if best[1] >= _FILL_MIN else None


def _sample_starts(sdr: bytes) -> "tuple[list[int], int | None]":
    """The sample start of every sampled instrument (length above 0, and no
    synth waveform inside the module) in a Synth Dream module, and the
    module's fill byte (``_fill_byte``).  The instrument table is the 32-byte
    entries from the offset in the module's first long while an entry's
    first byte is 0 — or, in a damaged rip, its fill byte, up to the first
    envelope an entry points at."""
    if len(sdr) < 12:
        return [], None
    o = struct.unpack(">I", sdr[:4])[0]
    if not 12 <= o <= len(sdr) - 32:
        return [], None
    fill = _fill_byte(sdr[o:o + 32])
    out = []
    env = len(sdr)
    while o + 32 <= len(sdr) and (sdr[o] == 0 or (sdr[o] == fill and o + 32 <= env)):
        longs = struct.unpack(">5I", sdr[o + 4:o + 24])
        for v in longs[:4]:
            if o < v < len(sdr):
                env = min(env, v)
        synth, start = longs[3], longs[4]
        length = struct.unpack(">H", sdr[o + 24:o + 26])[0]
        if length and not 0 < synth < len(sdr):
            out.append(start)
        o += 32
    return out, fill


def _starts_at(start: int, body: int, fill: "int | None") -> bool:
    """Does ``start`` name the BODY at ``body`` — byte for byte, a byte that
    is 0 in ``body`` also matching the fill byte?"""
    for k in (0, 8, 16, 24):
        s, b = (start >> k) & 255, (body >> k) & 255
        if s != b and not (b == 0 and s == fill):
            return False
    return True


def _body_offsets(bank: bytes) -> "set[int]":
    out, i = set(), 0
    while True:
        i = bank.find(b"BODY", i)
        if i < 0:
            return out
        out.add(i + 8)
        i += 4


def synthdream_refusal(path: "Path | str") -> "str | None":
    """Why the Synth Dream module at ``path`` (the copy uade plays, its bank
    beside it) can't be played, or None: ``SAMPLE_BANK_MISSING`` when fewer
    than half of its sampled instruments start at a sample of the bank it is
    played with (``_starts_at``: in a damaged rip, a start whose zero bytes
    hold its fill byte counts).  None for any other file, a module without
    sampled instruments, or one whose bank isn't there (uade reports that
    itself), or one whose bank holds no ``BODY`` chunk to judge it by."""
    path = Path(path)
    if path.name[:4].lower() != "sdr.":
        return None
    try:
        if os.stat(path).st_size > _SDR_MAX_BYTES:
            return None
        sdr = path.read_bytes()
        starts, fill = _sample_starts(sdr)
        if not starts:
            return None
        bank_path = _bank_for(path)
        if bank_path is None or os.stat(bank_path).st_size > _BANK_MAX_BYTES:
            return None
        bodies = _body_offsets(bank_path.read_bytes())
    except OSError:
        return None
    if not bodies:
        return None                     # (no sample in the bank to judge by: play)
    hits = sum(1 for s in starts if s in bodies or any(_starts_at(s, b, fill) for b in bodies))
    if 2 * hits < len(starts):
        log.info("Synth Dream %s: %d of %d sampled instruments start at a sample of %s",
                 path.name, hits, len(starts), bank_path.name)
        return SAMPLE_BANK_MISSING
    return None


# ── The known files ─────────────────────────────────────────────────────────
# Synth Dream "No Buddies Land" (game 1, game 2, hiscore, jigsaw, end 2) and
# four of its sample banks; Maximum Effect "'Allo 'Allo!" level 1-5 and
# "Dinosaur Detective" complete + intro; Peter Verswyvelen "Ziriax" level 1,
# level 2, ingame, jingle; Cryptoburners "pack5" (Hippel COSO).

_TABLE = (
    _Entry(
        'sdr.nobuddiesland game 1', 12284, 0xfc,
        "d58aa97936a808de79e121eb4ca32f7be9cfe5f4a26aea2eddf89ff3ef132c04",
        "e51e540a0fc737ea752269bf1021925e81f25620022fe3b51484f9f95732f8bf",
        runs=(
            "28+2 2d+4 39+4 46+3 51+4 5d+4 82+3 8e+3 cb 152+3 15d 160 169 16c 182+3 "
            "18d 190 199 19c 21f a90 acc b4c b84 ba2 cf8 cfe d04 d10 d1c d28 d34 "
            "d3a d40 d4c d52 d58 d64 d70 d7c d88 d94 d9a da0 dac db2 db8 dc4 dd0 "
            "ddc de8 df4 dfa e00 e0c e12 e18 e24 e30 e3c e54 e5a e9c ea2 ee4 eea "
            "f04 f14 f1a f3e f44 f86 f8c fce fd4 fee 101c 1022 103c 104c 1052 1076 "
            "107c 10a0 10a6 10c0 10d0 10d6 1118 111e 1138 1148 114e 1172 1178 11ba "
            "11c0 1202 1208 124a 1250 126a 127a 1280 12a4 12aa 12ec 12f2 1316 131c "
            "1336 1352 1358 13b8 13be 1414 141a 1434 144e 1454 1482 1488 14de 14e4 "
            "1544 154a 1564 15a6 15ac 15c6 15e0 15e6 1614 161a 1648 164e 1668 1682 "
            "1688 16e8 16ee 1708 1722 1728 1760 1766 17bc 17c2 1818 181e 187e 1884 "
            "189e 18b8 18be 18f6 18fc 1952 1958 1986 198c 19a6 19b8 19be 1a04 1a0a "
            "1a4c 1a52 1a6c 1a7c 1a82 1aa6 1aac 1aee 1af4 1b3a 1b40 1b5a 1b88 1b8e "
            "1ba8 1bb8 1bbe 1be2 1be8 1c0c 1c12 1c2c 1c3c 1c42 1c88 1c8e 1ca8 1cb8 "
            "1cbe 1ce6 1cec 1d2e 1d34 1d76 1d7c 1dc2 1dc8 1de2 1df2 1df8 1e20 1e26 "
            "1e6a 1e76 1e7c 1e82 1e9a 1ea6 1eb2 1ec4 1ed0 1edc 1ee8 2686+f 2698+5 "
            "26a6+e 26b8+5 26c6+2 26ce+6 26d8+5 26e6+6 26ee+6 26f8+5 2706+2 270e+6 "
            "2718+5 2726+6 272e+6 2746+6 274e+6 2758+2 275e+6 276a+2 277f+5 278a+2 "
            "279f+5 27aa+2 27bf+5 27ca+2 27df+5 27ea+2 27ff+5 280a+2 281f+5 282a+2 "
            "283f+5 284a+2 285f 2866+2 2878+5 2880+4 288a+2 289f+5 28aa+6 28bf+5 "
            "28ca+6 28df 28e6+e 28f8+5 2900+4 290a+2 291f+5 292a+2 293f+5 294a+2 "
            "295f+5 296a+2 297f+5 298a+2 29a6+e 29b8+5 29c6+2 29d8+5 29e6+2 29f8+5 "
            "2a06+e 2a18+5 2a26+2 2a2e+6 2a38+5 2a46+6 2a4e+6 2a58+5 2a66+6 2a6e+6 "
            "2a78+5 2a86+2 2a8e+6 2a98+5 2aa6+2 2aae+6 2ab8+5 "
        )),
    _Entry(
        'sdr.nobuddiesland game 2', 4360, 0xfc,
        "47a9acff6e123941d241b730f2a974e769d518f1ac93176e348d935b6445a1fd",
        "5e3fae72c78bdae93f766ac50faebdeaf97c92e59a93facc04e1e195ef8eb3c1",
        runs=(
            "1c+2 20+2 24+2 d7+4 538+3 542 554+3 55e 570+3 592+3 5b0+3 5ba 5cc+3 "
            "5d6 5e8+3 60a+3 a18+f a2a+5 a38+e a4a+5 a58+e a6a+2 a70 a78+e a8a+5 "
            "a98+e aaa+5 ab8+6 ac0+5 aca+5 ad8+6 ae0+5 aea+5 af2+4 afc+6 b12+4 "
            "b1c+6 b32+4 b3c+6 b52+4 b5c+6 b72+4 b7c+6 b92+4 b9c+6 bb2+4 bbc+6 "
            "bd2+4 bdc+6 bf2+4 bf8+d c0a c10+6 c1c+2 c20+2 c32+4 c3c+2 c40+2 c52+4 "
            "c5c+2 c60+2 c72+4 c7c+2 c80+2 c92+4 c9c+2 ca0+2 cb2+4 cbc+2 cc0+2 "
            "cd2+4 cdc+2 ce0+2 cf2+4 cfc+2 d00+2 d12+4 d1c+2 d20+2 d38+e d4a+5 "
            "d58+e d78+e d8a+2 d90 d98+e daa+5 "
        )),
    _Entry(
        'sdr.nobuddiesland hiscore', 5040, 0xfc,
        "332aacf40ee9e5a372c01b5a857bd28ec896c5accb82ffbdcb4400d008ffd94d",
        "4567ea8783f23471ca86af8ea120782401cc52037df12f0df6681879ee0165a7",
        runs=(
            "c70+d c82+5 c90+d ca2+5 cb0+d cc2+5 cd0+d ce2+5 cf0+d d02+5 d10+d "
            "d22+5 d30+d d42+5 d50+d d62+5 d70+6 d78+6 d82+5 d90+6 d98+6 da2+5 "
            "db0+6 db8+6 dc2+5 dca+4 dd0+2 dd4+2 dd8+2 dea+4 df0+2 df4+2 df8+2 "
            "e0a+4 e10+2 e14+2 e18+2 e2a+4 e30+2 e34+2 e38+2 e4a+4 e50+2 e54+2 "
            "e58+2 e6a+4 e70+2 e74+2 e78+2 e8a+4 e90+2 e94+2 e98+2 eaa+4 eb0+2 "
            "eb4+2 eb8+2 eca+4 ed0+2 ed4+2 ed8+2 ef0+6 ef8+6 f02+5 f0a+4 f10+2 "
            "f16+4 f30+6 f38+6 f42+5 f4a+4 f50+2 f54+2 f58+2 f70+e f82+5 f90+f "
            "fa2+5 fb0+e fca+4 fd0+2 fd6+4 fea+4 ff0+2 ff6+4 "
        )),
    _Entry(
        'sdr.nobuddiesland jigsaw', 5102, 0xfc,
        "63c16e8813587778f8d1f1951501d7d39cfb3329c4ae53238ecdff45c5bff334",
        "179640b457b2d45e3679d50049a7385673b8b0f89101666200977cf6b3cde713",
        runs=(
            "bf6+f c08+5 c16+6 c1e+6 c28+5 c36+e c48+2 c4e c56+e c68+5 c76+6 c7e+6 "
            "c88+5 c96+e ca8+5 cb6+d cc8+5 cd6+d ce8+5 cf0+4 cfa+6 d10+4 d1a+6 "
            "d30+4 d3a+6 d50+4 d5a+6 d70+4 d7a+6 d90+4 d9a+6 db0+4 dba+6 dd0+4 "
            "dda+6 df0+4 dfa+2 dfe+2 e10+4 e1a+6 e30+4 e3a+2 e3e+2 e50+4 e5a+2 "
            "e5e+2 e70+4 e7a+2 e7e+2 e90+4 e9a+2 e9e+2 eb0+4 eba+2 ebe+2 ed0+4 "
            "eda+2 ede+2 ef0+4 efa+2 efe+2 f16+e f28+5 f36+e f48+2 f4e f56+e f68+2 "
            "f6e f76+e f88+5 "
        )),
    _Entry(
        'sdr.nobuddiesland end 2', 7874, 0xfc,
        "94928e35742468cb5ed99e56c093adf99ed1ea887eb4d467003730c702fb6d5a",
        "4a41e68d351db053c73b9306c818a83cd906d467b4f40c183622b7ee7696c522",
        runs=(
            "e6+3 116+3 13a+3 152+3 16a+3 182+3 858+3 18a0+f 18b2+5 18c0+e 18d2+5 "
            "18e0+e 18f4+3 1900+6 1908+6 1914+3 1920+6 1928+6 1934+3 193a+4 1944+2 "
            "195a+4 1964+2 197a+4 1984+2 199a+4 19a4+2 19ba+4 19c4+2 19da+4 19e4+2 "
            "19fa+4 1a04+6 1a1a+4 1a24+6 1a3a+4 1a44+2 1a60+e 1a7a+4 1a84+2 1a9a+4 "
            "1aa4+2 1aba+4 1ac4+2 1ada+4 1ae4+2 1afa+4 1b04+2 "
        )),
    _Entry(
        'smp.nobuddiesland game 1', 71398, 0xfc,
        "f09893b67f0b665cc76746b5074a13ccf50e754a72c340836195c6d77681cb63",
        "8123badd5179c33e1f0aab8edd6b3d88c5ae6c2395f1a5515ba1ce6c0550e79b",
        runs=(
            "8e4+19 333e+17 8b6e+16 "
        )),
    _Entry(
        'smp.nobuddiesland game 2', 91506, 0x05,
        "e6111c76a1fac363f78868280652e44b9d5fecfd9ffd06b459727e19d345ff9f",
        "75865b7fccf4c3b6c7b20351725e515adfffe2c49bf26d65128a1ac8625bac30",
        runs=(
            "357 61b 666 8e4+19 cd3 fc3 11d1 17d6 18e1 19a9+2 1ac2 1b75 1b7f 1b8e "
            "1b99 1da4 1e9e 1eb9 1ef9 1f14 1f57 1fe2 2003 2072 20e3 2123 21c1 2269 "
            "22ae+2 22fa+2 233a+4 239d 23a3 23da 244f 246f 24a8+4 24ca 24e7 24ea "
            "2501 250d 257e+17 31fe 34c0 371f 382d+3 3a3a 503d 5342 54ac 664b 6bec "
            "7366+2 77c1 77c3+3 77f8+2 77fe+4 7805 7808 780b+3 7810 7823+3 7859 "
            "785c+3 78a8+3 78af+4 78b6 78b9 78bc+3 78d6+3 78db 78dd+2 78e1+3 790a "
            "790d+3 7956+2 7961+3 7974+2 7985+3 7989 798c 798f+3 7997+3 79a3 79bd+3 "
            "79c5 79cd 79d0+3 79f2 7a07 7a09+2 7a14 7a1e 7a21+2 7a25 7a38+4 7a40+2 "
            "7a8c 7ab0 7ab3+2 7ac8 7ade+3 7af1 7af4+2 7b09+2 7b3b 7b5a 7b5e+2 "
            "7b64+2 7b70+2 7b74+3 7b94+3 7b9d+2 7beb 7c0d 7c10+3 7c28+3 7c34 7c37+3 "
            "7c3c+4 7c46+2 7c4a 7c4f 7c52 7c69+3 7c9a 7cb9+3 7cc1+3 7ccc+4 7cd3 "
            "7cd6 7cdb 7ce5+2 7d15+3 7d4b 7d73 7da2 7da6+2 7daa 7dae+16 7df7+3 7e1f "
            "7e43+4 7e4b+3 7e50+3 7e57 7e59+2 7e5d+3 7e67+2 7eae 7ef1+3 7f12+3 7f28 "
            "7f2b 7f5d+2 7f61 a03b a079 a08b a123 a1ae a5ae+2 a771 ab4a b587 b681 "
            "b737 b85e b8e5 bdd7 c9e9 cda0+2 cefd+2 cfa3 d025 d238 d69d+2 da4e+2 "
            "e0e1 e0e8 e379+2 ebdc f88b 10009 1012e 101c5 10255 102ec 1037d 10413 "
            "104a5 1053a 1081a 10941 109d7 10ddd 110bd 111e4 "
        )),
    _Entry(
        'smp.nobuddiesland hiscore', 83540, 0x05,
        "0d8108ae0a48e74971afbd55898a8b9c574619505cf1fac615ab5628c51f6bca",
        "851b32894de2fcdff11959c6e881ba5f3872a988ea6fd5741daccfac895ae83a",
        runs=(
            "1 8e4+2 1002 1147 1182 11c0+8 11c9+3 11d2+3 1c35 1ce5 1d93 1e42 1ef2 "
            "2413 267a 4450 582d+2 5efb 5fa8+2 6404+4 643a+3 6440+2 6447 644a "
            "644d+3 6452 6465 649b 649e+3 64ea+3 64f1+2 64f8 64fb 64fe+3 6518 651b "
            "651e+3 6523+3 654c 654f+3 6598+2 65a3 65b7 65c7 65cb 65ce 65d1+3 65d9 "
            "6600+2 6607 6612+4 6649+3 6658 665d 6660 6663+3 667b+8 6698 66f5+2 "
            "670a 6721+2 6736+2 674b 677d 679c 679e 67a1+3 67a6+2 67b3 67b6+3 67d6 "
            "67df+2 67fd 682f 6852+4 686a+4 6876 6879+3 687e+3 6888+2 688c 6891 "
            "6894 68ab 68dc 68fb+2 6903+4 690e+2 6915 6918 6926+3 694a 6958 69ab "
            "69e4 69e8+2 69ec 69f0+7 69f8+3 6a01+3 6a39+4 6a61 6a86+8 6a92+2 6a99 "
            "6a9b+2 6a9f+3 6aa9+2 6aef+2 6b33+3 6b54 6b6a 6b6d 6b9e+3 6ba3 b2ce "
            "c0b6 "
        )),
    _Entry(
        'smp.nobuddiesland jigsaw', 85904, 0x05,
        "88b48e0e94c7663f988acb9d2ed99f47dad52fd56f3f0f25a95077af5936d834",
        "d6bd8372b36549a4e455036a495dd2bda26d6f404a2bde1533f87fc2a6bcdd19",
        runs=(
            "0+8 8e4+4 8e9+2 8ec+2 11c0+20 18fe+3 192e+2 1959 19c9+2 1a0e+2 2220+17 "
            "34cf+3 36d9+2 36dc 514c 514e 6f5b 7464+2 749c+3 74a0+4 74ae+3 74c5+2 "
            "74ff+2 754b+2 754f 7551+4 755f+3 7578+2 757f+3 7584 75b0+3 75bc 75f8+3 "
            "7603+2 7627+2 7632+3 7639+2 7661+3 7672+2 76aa+2 76b6+3 76c4+3 76da+3 "
            "76df+3 76f8 7755+2 776a+2 7782+3 7796+2 77ab+2 7802+3 7807 7811+3 7817 "
            "7837+3 783c+3 785d 78b3+3 78ca 78ce 78d9+2 78e0+3 78e5+3 790b+2 793d "
            "795b+2 7964+3 796e+4 7987+2 79aa 79b7+2 7a0b 7a44 7a46+2 7a50+16 "
            "7a9a+3 7ac0+2 7ac3 7ae5+3 7aea+3 7af3+3 7b19 7b4f+3 7b93+2 7bb5+3 "
            "7bba+2 7bca+2 7bff+3 7c52 7d7a 7dc5+3 7eea 8018+2 88ba 8b04 90a9 92f0 "
            "9470 976e 98be 9bf3 9fa6 b405 b5ce bd85 be22 c100 c6cc c761 c7ed c963 "
            "c9a4 ca39 cd1d cf6b d658 da66 dd92+2 de24 deb9 e3e7 e47d+2 e5a4 e75a "
            "e882 e963 ea40 ea86 ed68 "
        )),
    _Entry(
        'MAX.AlloAllo level1', 7976, 0x2d,
        "2797d10f28066a24ccae8cbd7801f8a97f1258b748d638ac0827d3376204ef47",
        "41f6f10b7d1f9f93fc3fbb64829b63659bbbc7d6b07bf8a55cc9ea0acf7bbbc1",
        runs=(
            "0+3 4+3 1fe 200+2 222 224+2 270 272+2 306 308+2 390 392+2 "
        )),
    _Entry(
        'MAX.AlloAllo level2', 3814, 0x2d,
        "82a3b6a1c0c3c9aa44ce9f895bf779beaabbb6d360e39da6c7fab56ff166561f",
        "34aefba92612d54e652e5456f35223a7d09e69a3331cbf91fe5a7f9793eb824b",
        runs=(
            "0+3 4+3 28e 290+2 "
        )),
    _Entry(
        'MAX.AlloAllo level3', 9682, 0x2d,
        "486606b15b83e82933bbcbd31cddfb8e0caea9768a3cbfb8f358a7eedf127cc1",
        "5c9373638aacc6a49780992eafc179a108106e4658f9a525c4b1967a89109f4a",
        runs=(
            "0+3 4+3 "
        )),
    _Entry(
        'MAX.AlloAllo level4', 6060, 0x2d,
        "f08598b0d496dd1a667a696f458560bb549c9c1f94b78aa0d07db5339d9eac62",
        "c178b6e509b496419d4e81e19a02576f1f226a5a986c6d94cbc3b88fbcb2e6af",
        runs=(
            "0+3 4+3 10e 114 156 246 26a "
        )),
    _Entry(
        'MAX.AlloAllo level5', 9264, 0x2d,
        "dc4297fd4bd3a79e76d686a1a7d9da7a71241e49652219ffac390a625c11b650",
        "739e234ad884b7fa5d147ad9dd88d295b0687fd9ae1eae3da96f21d1f1551266",
        runs=(
            "0+3 4+3 3e4 3fc "
        )),
    _Entry(
        'MAX.DinosaurDetective complete', 9452, 0xd4,
        "492e1f6fd390ae134cf33e72c667673515ed58e074adb8552fbe70c6fd04794f",
        "345e081b2159db2f81dc6ba94afa4c008a274f1f71dfb48bfbe670cd64a83e8c",
        runs=(
            "4+3 8+3 3c 3e+2 4a+2 78 7a+2 c2+2 c8+2 ce+2 d4+2 126 128+2 12c 12e+2 "
            "156 158+2 15c 15e+2 164+2 16a+2 1b0 1b2+2 1b6 1b8+2 1be+2 1f2 1f4+2 "
            "1f8 1fa+2 222 224+2 228 22a+2 24e+2 260+2 264 266+2 26a 26c+2 2fc+2 "
            "302+2 "
        )),
    _Entry(
        'MAX.DinosaurDetective intro', 9462, 0xd4,
        "58d288db8fff7cff9f85a64d563cd3e2c0586c75e4b2bef37ffe413e73527195",
        "64e603387d6c264d5052a0e068fc8b29c7729601b09776f2f0550345f9b265fb",
        runs=(
            "0+3 4+3 8+3 c4+3 f6 102 178+3 19e 22c+3 234 23a 2dc 2e0+3 "
        )),
    _Entry(
        'ziriax level 1.pvp', 51004, 0x1a,
        "103801a657ce122a56c2c7f93cdaddeb7e240a4fca6dfee498f0ae823f80a712",
        "e1cc927d08055aab2b3013ec0cdafe756cc9e997ea6439f1a4024243fc413c00",
        runs=(
            "10+7 1c+3 2c+3 34+3 3c+3 44+3 4c+3 50+7 58+7 60+7 68+7 70+7 78+7 80+7 "
            "88+7 90+7 98+7 a0+7 a8+7 b0+7 b8+7 c0+7 c8+7 d0+7 d8+7 e0+7 e8+7 f0+7 "
            "f8 100+3 1cc 1ce 1d0 1d2 1d4 1d6 1d8 1ec 1ee 1f0 1f2 1f4 20c 20e 210 "
            "212 214 216 218 226 22e 230 232 234 24e 250 252 254 26e 270 272 274 "
            "28e 290 292 294 2ae 2b0 2b2 2b4 2c8 2ce 2d0 2d2 2d4 2e8 2ee 2f0 2f2 "
            "2f4 308 30e 310 312 314 32e 330 332 334 34e 350 352 354 370 372 374 "
            "38e 390 392 394 396 3ae 3b0 3b2 3b4 3c8 3ce 3d0 3d2 3d4 3ee 3f0 3f2 "
            "3f4 408 40e 410 412 414 42e 430 432 434 44e 450 452 54d 551 571 575 "
            "599 59d 5c1 5c5 649 64d 66d 671 695 699 6bd 6c1 6f1 6f5 6fd 701 709 "
            "70d 93d 941 ab1 ab9 abd b31 b45 bdd be5 9747+7 9dc4+3 a144+4 aec2+5 "
            "c19d "
        )),
    _Entry(
        'ziriax level 2.pvp', 52354, 0x1a,
        "893daddd487c858c38dcea8be6394ab5812c7a979a031293ca54031d935d8b5b",
        "1b5dfb2f3fb989b8e757001a75a410c2c687059493754c51ac801ef3f7816a62",
        runs=(
            "0+7 10+7 1c+3 2c+3 3c+3 44+3 4c+3 50+7 58+7 60+7 68+7 70+7 78+7 80+7 "
            "88+7 90+7 98+7 a0+7 a8+7 b0+7 b8+7 c0+7 c8+7 d0+7 d8+7 e0+7 e8+7 f0+7 "
            "f8 100+3 1d2 1da 1e2 1ea 1f2 1fa 202 20a 212 21a 222 22a 232 23a 242 "
            "24a 24c 278 28c 2b8 2cc 2f8 30c 338 9c8d+7 a30a+3 a68a+4 b408+5 c6e3 "
        )),
    _Entry(
        'ziriax ingame.pvp', 51000, 0x1a,
        "2cefd7c9c0cc1f3f9d7436fc3eeb9976f2662c4aa94665436a88954b9fe3d964",
        "5661ac791ec6f1ee7fe5b35f32ad199167b08dc6e7bf64d88acec67764251fa6",
        runs=(
            "10+7 1c+3 2c+3 34+3 3c+3 44+3 4c+3 50+7 58+7 60+7 68+7 70+7 78+7 80+7 "
            "88+7 90+7 98+7 a0+7 a8+7 b0+7 b8+7 c0+7 c8+7 d0+7 d8+7 e0+7 e8+7 f0+7 "
            "f8 100+3 182 1a2 1c2 1e2 202 222 242 262 282 2a2 2c2 2c4 2e2 302 322 "
            "5c7 5e3 6a7 6c3 6df 72b 743 74f 77b 783 7c3 7c7 7cf 7fb 836+2 9355+7 "
            "99d2+3 9d52+4 c199 "
        )),
    _Entry(
        'ziriax jingle.pvp', 49172, 0x1a,
        "70c81c37bb1d351dc3160b645e18acb809a5b765603d20f8328651e2bd537918",
        "dd9615d21f58f18ecaef7b69f66f833d4bc615e4be0738f58628ce9abed9cc81",
        runs=(
            "10+7 1c+3 2c+3 34+3 3c+3 44+3 4c+3 50+7 58+7 60+7 68+7 70+7 78+7 80+7 "
            "88+7 90+7 98+7 a0+7 a8+7 b0+7 b8+7 c0+7 c8+7 d0+7 d8+7 e0+7 e8+7 f0+7 "
            "f8 100+3 18e 2c5 36d 375 389 391 441 4fd 509 51d 52d 901f+7 969c+3 "
            "9a1c+4 a79a+5 ba75 "
        )),
    _Entry(
        'cryptoburners - pack5.hipc', 20316, 0x00,
        "bc1092cae3d1cc06bdd01488004e821facaed24ce1b9d91f764d2360b21e7ec0",
        "ee95f722a07c7007d60a47c53cee4e38df2bd7f5b5bcac388d6db8c867aa1707",
        truncate=4838),
)

_BY_SHA = {e.sha256: e for e in _TABLE}
_SIZES = frozenset(e.size for e in _TABLE)
