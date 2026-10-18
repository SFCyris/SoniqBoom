"""Read-only abstraction over the archive formats SoniqBoom cracks open to
surface the music inside.

* **ZIP** — Python stdlib ``zipfile``.
* **LHA / LZH** — Amiga's standard archive format, via the ``lhafile``
  package (a zipfile-style reader with a C decoder) and, for every archive it
  turns down (a method it lacks — ``-lh1-``, ``-lh4-``, ``-lz5-`` … —, header
  level 3, padding after the last member), the embedded reader
  ``core.lha``.

Both formats expose the same shape — :func:`list_members` + :func:`read_member`
— so the scanner / stream code can treat ``foo.zip::member`` and
``foo.lha::member`` identically.

Amiga twist
-----------
Files inside Amiga archives use a **prefix** naming convention
(``MOD.title``, ``MED.title``, ``AHX.title`` …) rather than a suffix extension.
We map those to a renderer-friendly *display name* (``MOD.title`` →
``MOD.title.mod``) so the existing extension-driven pipeline picks the right
renderer.  The display→real mapping is rebuilt from the archive on each read,
so :func:`list_members` and :func:`read_member` can never disagree.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import zipfile

from soniqboom.core import forksafe
from soniqboom.core import lha as _lha
from soniqboom.core.metadata import SUPPORTED_EXTENSIONS

log = logging.getLogger(__name__)

ARCHIVE_EXTS: tuple[str, ...] = (".zip", ".lha", ".lzh")
_LHA_EXTS: tuple[str, ...] = (".lha", ".lzh")

try:
    import lhafile as _lhafile
    HAVE_LHA = True
except Exception:                      # pragma: no cover - optional dependency
    _lhafile = None
    HAVE_LHA = False

# Last-resort LHA reader: the ``lha`` CLI from **lhasa**, when installed, for
# an archive neither ``lhafile`` nor the embedded reader (``core.lha``) reads.
_LHA_BIN = shutil.which("lha")

# Amiga prefix → renderer extension.  The prefix is the part before the first
# dot in the basename (``MOD`` in ``MOD.cool-tune``).
_AMIGA_PREFIX_EXT: dict[str, str] = {
    "mod": ".mod", "med": ".med", "mmd": ".med", "ahx": ".ahx", "thx": ".ahx",
    "hvl": ".hvl", "xm": ".xm", "s3m": ".s3m", "it": ".it", "okt": ".okt",
    "dbm": ".dbm", "mtm": ".mtm", "stm": ".stm", "digi": ".mod", "dgi": ".mod",
    "ptm": ".mod", "psid": ".sid",
    # ``sid.X`` is Amiga SidMon (uade), NOT C64 — the old ``"sid": ".sid"``
    # entry silently fed SidMon modules to sidplayfp, which can't play them.
    # ``.sid1`` is SidMon's own registered uade token.
    "sid": ".sid1",
}
# Every other uade eagleplayer prefix token (mdat, fc13, dw, hip, rh, ...)
# maps to its own registered extension so archived prefix-named exotica
# surfaces as playable members.  ``setdefault`` keeps the hand-tuned openmpt
# entries above authoritative; companion halves (smpl/smp) are never tokens.
from soniqboom.core import uade_formats as _uade_formats


def _extend_amiga_prefix_map() -> None:
    # Function scope: a module-level ``del`` after a possibly-empty loop
    # raised NameError on uade-less hosts (QA C1).  PREFIX tokens: the suffix
    # list leaves out tokens like ``di`` / ``md`` (``readme.md``), which are
    # still real modules in prefix form (``di.title``) — GitHub #24.
    for tok in _uade_formats.prefix_tokens():
        _AMIGA_PREFIX_EXT.setdefault(tok, f".{tok}")


_extend_amiga_prefix_map()
_EXT_SET = {e.lower() for e in SUPPORTED_EXTENSIONS}
if ".ftm" in _EXT_SET:                 # Face The Music, where libopenmpt reads it
    _AMIGA_PREFIX_EXT.setdefault("ftm", ".ftm")

# Documentation/text suffixes that never hold music even under a music prefix
# (``MOD.readme``, ``IT.txt`` …) — stops prefix matching from inventing tracks.
_NON_MUSIC_EXTS = {
    ".txt", ".nfo", ".diz", ".doc", ".readme", ".me", ".1st",
    ".info", ".guide", ".asc", ".ans", ".bbs",
}


def is_archive_name(name) -> bool:
    """True if *name* is a ZIP/LHA/LZH archive we crack open."""
    return str(name).lower().endswith(ARCHIVE_EXTS)


def is_lha_name(name) -> bool:
    """True if *name* is an LHA/LZH archive (needs the lhafile decoder)."""
    return str(name).lower().endswith(_LHA_EXTS)


class _LhaCliArchive:
    """A zipfile-style reader backed by the ``lha`` (lhasa) CLI, for LHA
    archives ``lhafile`` can't decode (e.g. ``-lh1-``).  Extracts the whole
    archive to a temp directory once; ``namelist``/``read`` then serve from
    disk.  ``close`` removes the temp tree (called on cache eviction) — only in
    the process that extracted it: a forked scan worker reads the server's
    tree (it holds no open descriptor) and extracts its own copy only if the
    server has removed it meanwhile."""

    def __init__(self, path: str):
        self._path = path
        self._extract()

    def _extract(self) -> None:
        self._owner = os.getpid()
        self._dir = tempfile.mkdtemp(prefix="sb_lha_")
        try:
            # forksafe: this runs from scanner/stream worker THREADS of the
            # CF-initialised server — a plain subprocess.run fork here
            # segfaulted the child (8 dumps in the 2026-07-02 scan).
            forksafe.run(
                [_LHA_BIN, f"xw={self._dir}", self._path],
                check=True, capture_output=True, timeout=120,
            )
        except Exception:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise
        self._files: dict[str, str] = {}
        for dp, _dirs, fns in os.walk(self._dir):
            for fn in fns:
                full = os.path.join(dp, fn)
                self._files[os.path.relpath(full, self._dir)] = full

    def namelist(self):
        return list(self._files.keys())

    def _read(self, member: str) -> bytes:
        full = self._files.get(member) or self._files.get(member.replace("\\", "/"))
        if full is None:
            raise KeyError(member)
        with open(full, "rb") as fh:
            return fh.read()

    def read(self, member: str) -> bytes:
        try:
            return self._read(member)
        except FileNotFoundError:
            if self._owner == os.getpid():
                raise
            self._extract()        # the server removed the tree we shared
            return self._read(member)

    def close(self):
        if self._owner == os.getpid():
            shutil.rmtree(self._dir, ignore_errors=True)


def open_lha(local_path):
    """Open a LOCAL LHA / LZH file (whatever its name — a ``.ym`` is one):
    lhafile (C decoder) first, else the embedded reader (``core.lha``: header
    levels 0–3, methods ``-lh0-``, ``-lh1-``, ``-lh4-`` … ``-lh7-``,
    ``-lz4-``, ``-lz5-``, ``-lzs-``), else the ``lha`` (lhasa) CLI when
    installed.  ``lha.LhaUnsupported`` for another method without lhasa."""
    p = str(local_path)
    with open(p, "rb") as fh:
        head = fh.read(7)
    if len(head) < 7 or head[2:3] != b"-" or head[6:7] != b"-":
        raise _lha.LhaError(f"not an LHA archive: {p}")
    if HAVE_LHA:
        try:
            arc = _lhafile.LhaFile(p)
        except Exception as exc:
            log.debug("lhafile can't read %s (%s) — using the embedded reader", p, exc)
        else:
            # (lhafile unpacks whatever size a header claims: the embedded
            # reader's plausibility check for every member first)
            for i in arc.infolist():
                m = i.compress_type
                m = m.decode("latin-1") if isinstance(m, bytes) else str(m)
                if not _lha.plausible_sizes(m, i.compress_size, i.file_size):
                    raise _lha.LhaError(f"member size implausible for its packed size: {p}")
            return arc
    try:
        return _lha.LhaArchive(p)
    except Exception as exc:
        if not _LHA_BIN:
            raise
        log.debug("embedded LHA reader can't read %s (%s) — using lhasa", p, exc)
    return _LhaCliArchive(p)


def _open(local_path):
    """Open a LOCAL archive: ZIP via stdlib, LHA / LZH via ``open_lha``."""
    p = str(local_path)
    if p.lower().endswith(_LHA_EXTS):
        return open_lha(p)
    return zipfile.ZipFile(p)


# ── Prefix names that also read as a suffix name ────────────────────────────
# ``mod.Opus``, ``mod.Sun``, ``MOD.cool.It``: an Amiga prefix token AND an
# extension SoniqBoom plays (``.opus``, uade's ``.sun``, ``.it``).  By name
# alone the suffix wins (an Opus file named "mod") — wrong for a ProTracker
# module named "Opus" in an Amiga archive.  Where the member's first bytes can
# be read, its content decides: the PREFIX reading when they carry that
# format's signature (and not the suffix format's), or neither format's where
# the suffix's has one (a Soundtracker module has none).  Unreadable bytes, or a
# prefix format without a signature (most uade tokens): the name reading, as
# before.  Bump ``MEMBER_NAMING_VERSION`` when display names change for some
# members (the scanner re-enumerates archives it cached).
MEMBER_NAMING_VERSION = 4
# 4: members with no name SoniqBoom reads, recognised by their first bytes
#    (``_SNIFF_ORDER``); Face The Music (``.ftm`` / ``FTM.x``); LHA archives
#    lhafile turns down read by the embedded reader.
HEAD_BYTES = 2048             # enough for every signature below (ProTracker: 1084)

_PT_TAGS = frozenset({b"M.K.", b"M!K!", b"M&K!", b"FLT4", b"FLT8", b"EXO4", b"EXO8",
                      b"CD81", b"OKTA", b"OCTA", b"FA04", b"FA06", b"FA08", b"N.T.",
                      b"LARD", b"NSMS"})


def _ftm_with_samples(head: bytes) -> bool:
    """A Face The Music module saved with its samples (flag bit 0 at offset
    13) — libopenmpt plays no other kind: one whose samples are separate
    files doesn't load."""
    return head[:4] == b"FTMN" and len(head) > 13 and bool(head[13] & 1)


def _pt_tag(head: bytes) -> bool:
    tag = head[1080:1084]
    if len(tag) < 4:
        return False
    if tag in _PT_TAGS:
        return True
    if tag[1:] == b"CHN" and tag[:1].isdigit():                # 6CHN, 8CHN
        return True
    if tag[2:] == b"CH" and tag[:2].isdigit():                 # 10CH … 32CH
        return True
    return tag[:3] == b"TDZ" and tag[3:4].isdigit()


def _mp3_head(head: bytes) -> bool:
    if head[:3] == b"ID3":
        return True
    for i in range(0, min(len(head) - 4, 1024)):
        b1, b2 = head[i + 1], head[i + 2]
        if (head[i] == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 >> 3) & 3 != 1
                and (b1 >> 1) & 3 != 0 and (b2 >> 4) not in (0, 15) and (b2 >> 2) & 3 != 3):
            return True
    return False


# Signatures of the formats a prefix token stands for (``_AMIGA_PREFIX_EXT``
# values).  A token whose format has none (SidMon, most uade players) is
# never re-read by content.
_PREFIX_SIGNATURE = {
    ".mod": _pt_tag,
    ".med": lambda h: h[:4] in (b"MMD0", b"MMD1", b"MMD2", b"MMD3", b"MMDC"),
    ".ahx": lambda h: h[:3] == b"THX",
    ".hvl": lambda h: h[:3] == b"HVL",
    ".xm": lambda h: h[:17] == b"Extended Module: ",
    ".s3m": lambda h: h[44:48] == b"SCRM",
    ".it": lambda h: h[:4] == b"IMPM",
    ".okt": lambda h: h[:8] == b"OKTASONG",
    ".dbm": lambda h: h[:4] == b"DBM0",
    ".mtm": lambda h: h[:3] == b"MTM",
    ".stm": lambda h: h[20:28] in (b"!Scream!", b"BMOD2STM", b"WUZAMOD!"),
    ".sid": lambda h: h[:4] in (b"PSID", b"RSID"),
    ".ftm": lambda h: _ftm_with_samples(h),
}
# Signatures of the suffix formats (a member that carries one keeps its name
# reading even when a prefix signature matched by chance).
_SUFFIX_SIGNATURE = {
    **_PREFIX_SIGNATURE,
    ".ogg": lambda h: h[:4] == b"OggS",
    ".opus": lambda h: h[:4] == b"OggS",
    ".flac": lambda h: h[:4] == b"fLaC",
    ".mp3": _mp3_head,
    ".wav": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WAVE",
    ".aif": lambda h: h[:4] == b"FORM" and h[8:12] in (b"AIFF", b"AIFC"),
    ".aiff": lambda h: h[:4] == b"FORM" and h[8:12] in (b"AIFF", b"AIFC"),
    ".m4a": lambda h: h[4:8] == b"ftyp",
    ".wv": lambda h: h[:4] == b"wvpk",
    ".mpc": lambda h: h[:4] == b"MPCK" or h[:3] == b"MP+",
    ".mid": lambda h: h[:4] in (b"MThd", b"RIFF"),
    ".midi": lambda h: h[:4] in (b"MThd", b"RIFF"),
    ".psid": lambda h: h[:4] in (b"PSID", b"RSID"),
    ".rsid": lambda h: h[:4] in (b"PSID", b"RSID"),
}


def _prefix_alternative(real: str) -> str | None:
    """The prefix-reading extension of a member whose name reads both ways
    (``mod.Opus`` → ``.mod``); None for any other name."""
    base = re.split(r"[\\/]", real)[-1]
    prefix, sep, body = base.partition(".")
    if not sep or not body:
        return None
    ext = os.path.splitext(base)[1].lower()
    pext = _AMIGA_PREFIX_EXT.get(prefix.lower())
    if not pext or ext not in _EXT_SET or ext == pext:
        return None
    return pext


def _content_display_name(real: str, head_of) -> str | None:
    """The PREFIX reading (``mod.Opus`` → ``mod.Opus.mod``) of a member whose
    name reads both ways, when its first bytes say so (``head_of(real)``):
    they carry the prefix format's signature and not the suffix format's —
    or neither, where the suffix format has one (a Soundtracker module, which
    has no signature, named ``mod.Ref.ogg``).  None to keep the name reading."""
    pext = _prefix_alternative(real)
    if pext is None or head_of is None:
        return None
    sig = _PREFIX_SIGNATURE.get(pext)
    if sig is None:
        return None
    try:
        head = head_of(real) or b""
    except Exception:                                  # unreadable: the name reading
        return None
    suffix_sig = _SUFFIX_SIGNATURE.get(os.path.splitext(real)[1].lower())
    if not sig(head):
        if suffix_sig is not None and len(head) >= 64 and not suffix_sig(head):
            return real + pext                 # not the suffix's format: the prefix names it
        return None
    if suffix_sig is not None and suffix_sig is not sig and suffix_sig(head):
        return None
    return real + pext


def needs_content_naming(name: str) -> bool:
    """Is ``name`` (a stored member name, or the display name an older scan
    stored) one whose reading only its bytes decide — a prefix token with a
    signature and an extension of another format?  (An archive listed before
    the content rule is enumerated again when it holds one.)"""
    pext = _prefix_alternative(name)
    return pext is not None and pext in _PREFIX_SIGNATURE


def content_name(real: str, head_of) -> str:
    """The name a member is listed under where only the stored names are at
    hand (the scanner's ZIP listing): its prefix reading when its bytes say
    so (``mod.Opus`` → ``mod.Opus.mod``), else the stored name.  Reads the
    member's head only for a name that reads both ways."""
    if not needs_content_naming(real):
        return real
    return _content_display_name(real, head_of) or real


def stored_name(names, display: str, head_of=None) -> str | None:
    """The stored member of ``names`` that ``display`` names: itself, the
    stored name a prefix reading appended its extension to
    (``mod.Opus.mod`` → ``mod.Opus``), or the playable-member map's entry;
    None when none matches."""
    pool = names if isinstance(names, (set, frozenset, dict)) else set(names)
    if display in pool:
        return display
    stem, dot, ext = display.rpartition(".")
    if dot and stem in pool and _prefix_alternative(stem) == "." + ext.lower():
        return stem
    real = _build_map(list(pool), head_of).get(display)
    return real


def _display_name(real: str) -> str | None:
    """Renderer-friendly member name if *real* is playable, else ``None``.

    Handles suffix naming (``song.mod``) and Amiga prefix naming
    (``MOD.song`` → ``MOD.song.mod``).  Directory entries return ``None``.
    """
    base = re.split(r"[\\/]", real)[-1]    # Amiga subdirs use BACKSLASH separators
    if not base:
        return None
    ext = os.path.splitext(base)[1].lower()
    if ext in _EXT_SET:
        return real                        # already suffix-named
    if "." in base and ext not in _NON_MUSIC_EXTS:
        prefix = base.split(".", 1)[0].lower()
        amap = _AMIGA_PREFIX_EXT.get(prefix)
        if amap:
            return real + amap             # append the renderer extension
    return None


# ── Open-object + member-map cache ──────────────────────────────────────────
# Re-opening an archive on every member read re-parses its WHOLE directory each
# time — O(members) per read → O(members²) over a full archive.  That stalled
# the scanner on big compilations (a real 4491-member ``.zip`` took minutes).
# We keep a tiny LRU of OPEN archive objects (parsed once) + their display→real
# maps, guarded by one lock (the underlying readers aren't thread-safe), keyed
# on (path, mtime) so a rewritten archive invalidates.
import threading as _threading
from collections import OrderedDict as _OrderedDict

_OPEN_CACHE: "_OrderedDict[tuple, object]" = _OrderedDict()
_MAP_CACHE: "_OrderedDict[tuple, dict]" = _OrderedDict()
_CACHE_LOCK = _threading.RLock()
_CACHE_PID = os.getpid()       # the process whose descriptors _OPEN_CACHE holds
_OPEN_MAX = 8
_MAP_MAX = 256


# An open archive object belongs to the process that opened it.  On Linux the
# scan pool FORKS its workers from the server (``scanner._process_pool`` uses
# the platform default there), so without this every worker inherited the
# server's open archives — and their file descriptors, which share ONE file
# offset with the server and with each other.  lhafile and zipfile read a
# member as seek + read (two system calls), so parallel workers read one
# another's bytes: in one Linux (Docker) scan 274 of AHXSONGS.LHA's 513
# members failed "crc is not matched", while macOS (workers forked from a
# clean forkserver) indexed all 513.  A forked child therefore forgets the
# inherited lhafile / zipfile objects and opens its own.  They are dropped,
# not closed explicitly (collecting them closes only the child's copies of the
# descriptors; nothing seeks).  ``_LhaCliArchive`` entries stay: they hold no
# descriptor, and only their creator removes their temp tree.  The lock is
# replaced too — another thread may have held it at the fork, and a held lock
# would deadlock the child's first archive read.  ``_MAP_CACHE`` holds plain
# name maps and stays valid.
def _drop_inherited_archives() -> None:
    global _CACHE_PID
    for key in [k for k, a in _OPEN_CACHE.items() if not isinstance(a, _LhaCliArchive)]:
        del _OPEN_CACHE[key]
    _CACHE_PID = os.getpid()


def _forget_inherited_archives() -> None:
    global _CACHE_LOCK
    _CACHE_LOCK = _threading.RLock()
    _drop_inherited_archives()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_inherited_archives)


def _cache_key(local_path) -> tuple:
    p = str(local_path)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    return (p, mtime)


def _cached_open(key, local_path):
    """Get-or-open the archive for *key* (parsed once).  Hold ``_CACHE_LOCK``."""
    if _CACHE_PID != os.getpid():
        # A fork that bypassed ``os.register_at_fork`` (a C-level fork):
        # same rule — never use another process's open archives.
        _drop_inherited_archives()
    a = _OPEN_CACHE.get(key)
    if a is not None:
        _OPEN_CACHE.move_to_end(key)
        return a
    a = _open(local_path)
    _OPEN_CACHE[key] = a
    while len(_OPEN_CACHE) > _OPEN_MAX:
        _, old = _OPEN_CACHE.popitem(last=False)
        try:
            old.close()
        except Exception:
            pass
    return a


# ── Members recognised by their bytes ────────────────────────────────────────
# A module stored under a plain name — no extension, no prefix token (an
# Amiga file needs neither: ``Antom``) — is listed when its first bytes carry
# one of these signatures, under its name + the format's extension
# (``Antom`` → ``Antom.ftm``).  Only where the bytes can be read
# (``head_of``), only names without a dot, and never under a name another
# member already has (a name-recognised member keeps its name).
_SNIFF_ORDER = (".mod", ".med", ".okt", ".ftm", ".xm", ".s3m", ".it", ".dbm", ".stm",
                ".ahx", ".hvl", ".sid")
_SNIFF_EXTRA = {                       # (stricter than the prefix check)
    ".ahx": lambda h: h[:3] == b"THX" and h[3:4] in (b"\0", b"\1"),
    ".hvl": lambda h: h[:3] == b"HVL" and h[3:4] in (b"\0", b"\1"),
}
# Suffix-named members whose extension another format shares: listed only
# when their bytes (where readable) are the format SoniqBoom plays —
# FamiTracker saves ``.ftm`` too.
_SUFFIX_VERIFY = {".ftm": lambda h: _ftm_with_samples(h)}


def _base_of(real: str) -> str:
    return re.split(r"[\\/]", real)[-1]


def sniffable(real: str) -> bool:
    """Is ``real`` (a stored member name) one only its bytes can name — a
    plain name with no dot?"""
    base = _base_of(real)
    return bool(base) and "." not in base and not real.endswith(("/", "\\"))


def sniff_ext(head: bytes) -> str | None:
    """The extension of the playable format ``head`` (a member's first
    bytes) carries a signature of, else None."""
    for ext in _SNIFF_ORDER:
        if ext not in _EXT_SET:
            continue
        sig = _SNIFF_EXTRA.get(ext) or _PREFIX_SIGNATURE.get(ext)
        try:
            if sig is not None and sig(head):
                return ext
        except Exception:                                  # noqa: BLE001
            continue
    return None


def rejected_by_bytes(real: str, display: str, head_of) -> bool:
    """Is member ``real``, listed as ``display``, one no reading of it plays,
    by its readable first bytes?  A name whose format another one shares
    (``_SUFFIX_VERIFY``: FamiTracker's ``.ftm``) holding the other format; or
    any reading of a Face The Music module saved without its samples
    (``FTM.title`` read as ``.ftm`` — or as the uade format its suffix
    names, ``FTM.One``): libopenmpt loads no such module."""
    if head_of is None:
        return False
    base = _base_of(real)
    ext = os.path.splitext(_base_of(display))[1].lower()
    check = _SUFFIX_VERIFY.get(ext)
    ftm_named = base.split(".", 1)[0].lower() == "ftm" or base.lower().endswith(".ftm")
    if check is None and not ftm_named:
        return False
    try:
        head = head_of(real) or b""
    except Exception:                                      # noqa: BLE001
        return False
    if len(head) < 4:
        return False
    if head[:4] == b"FTMN" and not _ftm_with_samples(head):
        return True
    return check is not None and not check(head)


def sniffed_members(names, head_of, taken=()) -> dict[str, str]:
    """``{display name: stored name}`` of the members of ``names`` only their
    bytes name (``sniffable``), skipping a display name in ``taken`` (or
    already given to another stored name)."""
    out: dict[str, str] = {}
    if head_of is None:
        return out
    pool = set(names) | set(taken)
    for real in names:
        if not sniffable(real):
            continue
        try:
            head = head_of(real) or b""
        except Exception:                                  # noqa: BLE001
            continue
        ext = sniff_ext(head)
        if ext is None:
            continue
        disp = real + ext
        if disp in pool or disp in out:
            continue
        out[disp] = real
    return out


def resolve_display(names, display: str, head_of=None) -> str | None:
    """The stored member of ``names`` that ``display`` names, by the naming a
    full read of the archive gives (``_members``: members only their bytes
    name included) — reading the first bytes only of the member ``display``
    could be (an archive read by ranges asks for as few as it can)."""
    m = _build_map(names, head_of)
    if display in m:
        return m[display]
    stem, dot, _ext = display.rpartition(".")
    pool = set(names)
    if dot and stem in pool and sniffable(stem) and head_of is not None:
        if sniffed_members([stem], head_of, taken=set(m) | pool).get(display) == stem:
            return stem
    return None


def formats_fingerprint() -> str:
    """What decides which members of an archive are listed: the formats this
    host plays, the prefix tokens, the naming rules — an archive found to hold
    nothing playable is opened again when this changes."""
    import hashlib
    blob = repr((MEMBER_NAMING_VERSION, sorted(_EXT_SET), sorted(_AMIGA_PREFIX_EXT.items()),
                 _SNIFF_ORDER))
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def _build_map(names, head_of=None, *, sniff: bool = False) -> dict[str, str]:
    """``{display name: stored name}`` of the playable members of ``names``.
    ``head_of(stored name) -> bytes`` (the member's first ``HEAD_BYTES``)
    lets a name that reads both ways be decided by its content
    (``_content_display_name``) — every caller that can read the members
    passes it, so they all agree on the names.  ``sniff``: members only
    their bytes name (``sniffed_members``) follow, after every other one."""
    out: dict[str, str] = {}
    for real in names:
        disp = _display_name(real)
        if disp is None:
            continue
        if disp == real and head_of is not None:
            disp = _content_display_name(real, head_of) or disp
        if rejected_by_bytes(real, disp, head_of):
            continue
        name = disp
        i = 2
        while name in out:                # de-dup display names per archive
            stem, dot, ext = disp.rpartition(".")
            name = f"{stem} ({i}).{ext}" if (dot and stem) else f"{disp} ({i})"
            i += 1
        out[name] = real
    if sniff and head_of is not None:
        out.update(sniffed_members(names, head_of, taken=out))
    return out


def _members(local_path) -> dict[str, str]:
    """``{display_name: real_member}`` for playable members — cached per
    (path, mtime) so a scan never re-enumerates an archive per member."""
    key = _cache_key(local_path)
    with _CACHE_LOCK:
        cached = _MAP_CACHE.get(key)
        if cached is not None:
            _MAP_CACHE.move_to_end(key)
            return cached
        arc = _cached_open(key, local_path)
        names = arc.namelist()
        out = _build_map(names, head_of=lambda real: _head_of_open(arc, real, HEAD_BYTES),
                         sniff=True)
        _MAP_CACHE[key] = out
        while len(_MAP_CACHE) > _MAP_MAX:
            _MAP_CACHE.popitem(last=False)
        return out


def list_members(local_path, *, strict: bool = False) -> list[str]:
    """Playable member display-names inside *local_path* (.zip/.lha/.lzh).

    Returns ``[]`` (with a warning) on a broken/unsupported archive so one bad
    file never aborts a scan — or, with ``strict``, raises, so the scanner
    can tell "unreadable" (keep its indexed members) from "empty".
    """
    try:
        return list(_members(local_path).keys())
    except Exception as exc:
        if strict:
            raise
        log.warning("Cannot read archive %s: %s", local_path, exc)
        return []


def raw_namelist(local_path) -> list[str]:
    """ALL stored member names in *local_path* (.zip/.lha/.lzh), UNFILTERED —
    including non-playable companion files (AdLib instrument banks / patches)
    that ``list_members`` deliberately drops.  Names are returned exactly as
    stored, so each one round-trips back through ``read_member`` (which falls
    through to the raw name for members not in the playable map).  Returns
    ``[]`` on a broken/unsupported archive so one bad file never aborts a scan.
    """
    arc = None
    try:
        arc = _open(local_path)
        return list(arc.namelist())
    except Exception as exc:
        log.warning("Cannot list archive %s: %s", local_path, exc)
        return []
    finally:
        close = getattr(arc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def read_member(local_path, display_name: str) -> bytes:
    """Raw bytes of *display_name* inside the archive at *local_path*.

    The archive is opened + parsed once (cached); the lock serialises reads
    because the underlying zip/lha objects aren't thread-safe — cheap, since
    decompressing one member is fast and the per-read re-parse was the cost.
    """
    key = _cache_key(local_path)
    with _CACHE_LOCK:
        real = _members(local_path).get(display_name, display_name)
        arc = _cached_open(key, local_path)
        if not isinstance(arc, _lha.LhaArchive):
            return arc.read(real)
    # The embedded LHA reader opens the file for each read and shares no
    # state: it decodes outside the lock (a slow pure-Python decode doesn't
    # hold up every other archive read).
    return arc.read(real)


# ── PC-program detection ─────────────────────────────────────────────────────
# ``.exe``/``.com`` members SoniqBoom would never surface as tracks, but whose
# presence is a strong signal about the archive's NATURE.
_DOS_EXEC_EXTS: tuple[str, ...] = (".exe", ".com")


def _read_member_head(local_path, real: str, n: int) -> bytes:
    """First *n* bytes of raw member *real* WITHOUT decompressing the rest.

    ZIP streams just enough to yield *n* bytes (a packed multi-MB ``.exe``
    sibling isn't fully inflated just to sniff its 2-byte magic); LHA readers
    lack a streaming open, so they fall back to a full member read.  Holds the
    cache lock like :func:`read_member` — the underlying readers aren't
    thread-safe.  *real* is a raw stored name (from :func:`raw_namelist`).
    """
    key = _cache_key(local_path)
    with _CACHE_LOCK:
        return _head_of_open(_cached_open(key, local_path), real, n)


def _head_of_open(arc, real: str, n: int) -> bytes:
    """First *n* bytes of member *real* of an OPEN archive (hold the lock)."""
    if isinstance(arc, zipfile.ZipFile):
        with arc.open(real) as fh:
            return fh.read(n)
    head = getattr(arc, "read_head", None)        # (the embedded LHA reader stops early)
    if head is not None:
        return head(real, n)
    if HAVE_LHA and isinstance(arc, _lhafile.LhaFile):
        # lhafile decodes a whole member to give its head: the embedded
        # reader (same names) decodes only the head.
        emb = getattr(arc, "_sb_embedded", None)
        if emb is None:
            try:
                emb = _lha.LhaArchive(arc.filename)
            except Exception:                              # noqa: BLE001
                emb = False
            try:
                arc._sb_embedded = emb
            except Exception:                              # noqa: BLE001
                pass
        if emb:
            try:
                return emb.read_head(real, n)
            except Exception:                              # noqa: BLE001
                pass
    return arc.read(real)[:n]


def has_dos_executable(local_path, within_dir: str = "") -> bool:
    """True iff the archive holds a DOS/PC executable (an ``.exe``/``.com``
    member whose first two bytes are the ``MZ`` magic) in *within_dir*.

    This is a strong "this is a PC program bundle, not an Amiga music archive"
    signal: Amiga executables are AmigaDOS HUNK (magic ``0x000003F3``), never
    ``MZ``, so an MZ member reliably means x86/DOS.  The scanner uses it to
    veto indexing a PC data payload as an Amiga module — e.g. a demo's
    ``X.dat`` that matches PaulRobotham purely by the ``.dat`` extension while
    its sibling ``X.EXE`` proves the archive is a PC demo (see the lenient
    fallback in :func:`soniqboom.core.metadata.extract`).

    *within_dir* scopes the check to the module's own directory inside the
    archive (``""`` = archive root), so a DOS tool buried in an unrelated
    subfolder of a big Amiga compilation doesn't taint every member.

    Note: genuine ``.com`` files are raw code with no header, so only ``MZ``
    ``.exe`` (and the rare ``MZ`` ``.com``) match — a headerless ``.com`` is
    left ambiguous rather than risk a false veto.  Nested-archive outer paths
    (``a.zip::inner.zip``) can't be opened here and yield ``False`` (admit).
    Best-effort: returns ``False`` on a broken/unreadable archive — never
    raises, so it can't abort a scan.
    """
    want = within_dir.replace("\\", "/").strip("/").lower()
    for real in raw_namelist(local_path):
        norm = real.replace("\\", "/")            # Amiga dirs use backslashes
        parent, _, base = norm.rpartition("/")
        if parent.strip("/").lower() != want:
            continue
        if not base.lower().endswith(_DOS_EXEC_EXTS):
            continue
        try:
            head = _read_member_head(local_path, real, 2)
        except Exception:
            continue                              # unreadable member — skip
        if head == b"MZ":
            return True
    return False


# ── What an archive with nothing playable holds ─────────────────────────────
# ``census`` sorts the members of an archive no tune was found in, by name and
# — where they can be read — by their first bytes; ``describe_census`` says
# it in one clause for the listener ("it holds an Amiga program, not tune
# files").  Nothing here decides what is listed.  An inner archive or disk
# image is said to hold no tunes only when it was opened (``opened``);
# otherwise it is one SoniqBoom didn't open.
_ARCHIVE_KINDS = {".lzx": "LZX", ".rar": "RAR", ".7z": "7-Zip", ".arj": "ARJ",
                  ".zoo": "ZOO", ".arc": "ARC", ".sit": "StuffIt", ".tar": "TAR",
                  ".gz": "gzip", ".tgz": "gzip", ".bz2": "bzip2", ".xz": "xz",
                  ".lzh": "LHA", ".lha": "LHA", ".zip": "ZIP", ".pp": "PowerPacker",
                  ".xpk": "XPK"}
_READABLE_ARCHIVES = {".zip", ".lha", ".lzh"}
# disk image extension → (singular, plural)
_IMAGE_KINDS = {
    ".adf": ("an Amiga disk image", "Amiga disk images"),
    ".adz": ("a compressed Amiga disk image (ADZ)", "compressed Amiga disk images (ADZ)"),
    ".dms": ("an Amiga disk image in DMS format", "Amiga disk images in DMS format"),
    ".hdf": ("an Amiga hard-disk image", "Amiga hard-disk images"),
    ".ipf": ("a disk image in IPF format", "disk images in IPF format"),
    ".st": ("an Atari ST disk image", "Atari ST disk images"),
    ".msa": ("an Atari ST disk image (MSA)", "Atari ST disk images (MSA)"),
    ".d64": ("a C64 disk image", "C64 disk images"),
    ".d71": ("a C64 disk image", "C64 disk images"),
    ".d81": ("a C64 disk image", "C64 disk images"),
    ".dsk": ("a disk image", "disk images"),
    ".img": ("a disk image", "disk images"),
    ".iso": ("a CD image", "CD images"),
}
_READABLE_IMAGES = {".adf", ".d64", ".d71", ".d81"}
_TEXT_EXTS = {".txt", ".nfo", ".diz", ".doc", ".readme", ".me", ".1st", ".guide",
              ".asc", ".ans", ".bbs", ".md", ".htm", ".html", ".text", ".rtf", ".pdf"}
_PICTURE_EXTS = {".iff", ".ilbm", ".lbm", ".gif", ".png", ".jpg", ".jpeg", ".bmp", ".pcx",
                 ".pic", ".tif", ".tiff", ".ico"}
# Tunes recognised by their bytes that this host doesn't play:
# (test, key, extension that would make it playable here or None).
_FOREIGN_TUNES = (
    (lambda h: h[:4] == b"FTMN" and not _ftm_with_samples(h), "ftm_nosamples", None),
    (lambda h: h[:4] == b"FTMN", "ftm", ".ftm"),
    (lambda h: h[:18] == b"FamiTracker Module", "famitracker", None),
    (lambda h: h[:4] == b"SymM", "symphonie", None),
    (lambda h: h[:20] == b"DIGI Booster module\0", "digibooster", None),
)
# key → (singular, plural, what about it)
_FOREIGN_WORDS = {
    "ftm_nosamples": ("a Face The Music module saved without its samples",
                      "{n} Face The Music modules saved without their samples",
                      "which libopenmpt doesn't load"),
    "ftm": ("a Face The Music module", "{n} Face The Music modules",
            "which this server's libopenmpt doesn't play"),
    "famitracker": ("a FamiTracker module", "{n} FamiTracker modules",
                    "a format SoniqBoom doesn't play"),
    "symphonie": ("a Symphonie module", "{n} Symphonie modules",
                  "a format SoniqBoom doesn't play"),
    "digibooster": ("a DigiBooster 2 module", "{n} DigiBooster 2 modules",
                    "a format SoniqBoom doesn't play"),
}
_KEEP_NAMES = 500
_KEEP_HEADS = 16


def census(names, head_of=None, *, heads: int = 64, opened=None, inner_names=None) -> dict:
    """What the members ``names`` (stored names) of an archive with no
    playable member are, with the first bytes of up to ``heads`` of them read
    through ``head_of``; ``opened``: the inner archives / disk images that
    were read (``inner_names``: their member names).  Also keeps the names
    (``names``) and the first bytes of the members only their bytes could
    name (``heads``), so whether the archive could hold a tune by other
    format rules can be told without opening it again."""
    opened = set(opened or ())
    c: dict = {"amiga_program": 0, "pc_program": 0, "atari_program": 0,
               "images": {}, "archives": {}, "foreign_tunes": {},
               "text": 0, "pictures": 0, "icons": 0, "other": 0, "files": 0,
               "names": [], "heads": {}, "inner": dict(inner_names or {})}
    read = 0
    for real in names:
        real = str(real)
        base = _base_of(real)
        if not base or real.endswith(("/", "\\")) or base.startswith("._"):
            continue
        c["files"] += 1
        if len(c["names"]) < _KEEP_NAMES:
            c["names"].append(real)
        ext = os.path.splitext(base)[1].lower()
        if ext in _IMAGE_KINDS:
            state = ("empty" if real in opened else "unopened") if ext in _READABLE_IMAGES \
                else "foreign"
            key = f"{ext}|{state}"
            c["images"][key] = c["images"].get(key, 0) + 1
            continue
        if ext in _ARCHIVE_KINDS:
            state = ("empty" if real in opened else "unopened") if ext in _READABLE_ARCHIVES \
                else "foreign"
            key = f"{_ARCHIVE_KINDS[ext]}|{state}"
            c["archives"][key] = c["archives"].get(key, 0) + 1
            continue
        if ext == ".info":
            c["icons"] += 1
            continue
        head = b""
        if head_of is not None and read < heads:
            read += 1
            try:
                head = head_of(real) or b""
            except Exception:                              # noqa: BLE001
                head = b""
        if head and len(c["heads"]) < _KEEP_HEADS and (sniffable(real) or "ftm" in base.lower()):
            c["heads"][real] = head[:HEAD_BYTES].hex()
        if head[:4] == b"\x00\x00\x03\xf3":
            c["amiga_program"] += 1
            continue
        if head[:2] == b"MZ" or ext in (".exe", ".com"):
            c["pc_program"] += 1
            continue
        if head[:2] == b"\x60\x1a" or ext in (".prg", ".tos", ".ttp"):
            c["atari_program"] += 1
            continue
        foreign = next((key for test, key, fext in _FOREIGN_TUNES
                        if head and test(head) and not (fext and fext in _EXT_SET)), None)
        if foreign:
            c["foreign_tunes"][foreign] = c["foreign_tunes"].get(foreign, 0) + 1
            continue
        if ext in _TEXT_EXTS:
            c["text"] += 1
        elif ext in _PICTURE_EXTS or head[:4] == b"FORM" and head[8:12] == b"ILBM":
            c["pictures"] += 1
        else:
            c["other"] += 1
    return c


_AN_WORDS = ("LZX", "LHA", "ARJ", "ARC", "xz", "XPK", "MSA", "FTM", "RAR")


def _an(word: str) -> str:
    vowel = word[:1].lower() in "aeiou" or word.startswith(_AN_WORDS)
    return ("an " if vowel else "a ") + word


def _join(parts: list) -> str:
    if len(parts) > 3:
        parts = parts[:3] + ["more"]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def could_hold_a_tune(c: dict | None) -> bool:
    """Would the archive whose ``census`` this is list a member by the
    current format rules (names, and the first bytes it kept)?  True when it
    can't be told (no census, or members it couldn't keep the bytes of)."""
    if not c or "names" not in c or c.get("unsupported_method"):
        return True
    names = list(c.get("names") or ())
    if c.get("files", 0) > len(names):
        return True
    heads = {k: bytes.fromhex(v) for k, v in (c.get("heads") or {}).items()}
    if _build_map(names, lambda r: heads.get(r, b""), sniff=True):
        return True
    for inner in (c.get("inner") or {}).values():
        if _build_map(list(inner)):
            return True
    return any(("|unopened" in k) for k in list(c.get("images") or {}) + list(c.get("archives") or {}))


def describe_census(c: dict | None, *, music_disk: bool = False) -> str:
    """What an archive with no playable tune holds, as one clause for the
    listener ("it holds an Amiga program, not tune files").  ``music_disk``:
    it comes from a folder of music disks (their music plays from inside
    their program).  '' when nothing useful can be said."""
    if not c:
        return ""
    if c.get("unsupported_method"):
        return f"it is packed with a method SoniqBoom doesn't unpack ({c['unsupported_method']})"
    parts: list = []
    program = ""
    if c.get("amiga_program"):
        program = "an Amiga program"
    elif c.get("pc_program"):
        program = "a PC program"
    elif c.get("atari_program"):
        program = "an Atari ST program"
    for key, n in (c.get("foreign_tunes") or {}).items():
        one, many, why = _FOREIGN_WORDS.get(key, ("a tune", "{n} tunes", "a format SoniqBoom doesn't play"))
        parts.append(f"{one if n == 1 else many.format(n=n)}, {why}")
    for key, n in (c.get("images") or {}).items():
        ext, _, state = key.partition("|")
        one, many = _IMAGE_KINDS.get(ext, ("a disk image", "disk images"))
        what = one if n == 1 else f"{n} {many}"
        if state == "empty":
            parts.append(f"{what} with no tune file SoniqBoom reads on {'it' if n == 1 else 'them'}")
        elif state == "unopened":
            parts.append(f"{what} SoniqBoom didn't open")
        else:
            parts.append(f"{what}, which SoniqBoom doesn't open")
    for key, n in (c.get("archives") or {}).items():
        kind, _, state = key.partition("|")
        what = f"{_an(kind)} archive" if n == 1 else f"{n} {kind} archives"
        if state == "empty":
            parts.append(f"{what} with no tunes in {'it' if n == 1 else 'them'}")
        elif state == "unopened":
            parts.append(f"{what} SoniqBoom didn't open")
        else:
            parts.append(f"{what}, which SoniqBoom doesn't open")
    if program:
        if parts:
            parts.insert(0, program)
        elif music_disk:
            parts = [f"{program}, not tune files (a music disk plays its music from inside "
                     "its program)"]
        else:
            parts = [f"{program}, not tune files"]
    if not parts:
        rest = c.get("text", 0) + c.get("pictures", 0) + c.get("icons", 0)
        if c.get("files") and c.get("files") == rest:
            parts.append("only text and pictures")
        elif c.get("files"):
            parts.append("no file in a format SoniqBoom plays")
        else:
            parts.append("no files")
    return f"it holds {_join(parts)}"
