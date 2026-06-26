"""Read-only abstraction over the archive formats SoniqBoom cracks open to
surface the music inside.

* **ZIP** — Python stdlib ``zipfile``.
* **LHA / LZH** — Amiga's standard archive format, via the optional
  ``lhafile`` package (a zipfile-style reader).  If ``lhafile`` isn't
  installed, ``.lha``/``.lzh`` archives are skipped with a warning rather than
  crashing a scan.

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
import subprocess
import tempfile
import zipfile

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

# Fallback LHA decoder.  ``lhafile`` (in-process) can't decode some methods
# (notably ``-lh1-``, common in older Amiga archives), and 7-Zip on macOS
# doesn't decode LHA at all.  The reference ``lha`` CLI from **lhasa**
# (``brew install lhasa`` / ``apt install lhasa``) handles every method, so we
# shell out to it when lhafile rejects an archive.
_LHA_BIN = shutil.which("lha")

# Amiga prefix → renderer extension.  The prefix is the part before the first
# dot in the basename (``MOD`` in ``MOD.cool-tune``).
_AMIGA_PREFIX_EXT: dict[str, str] = {
    "mod": ".mod", "med": ".med", "mmd": ".med", "ahx": ".ahx", "thx": ".ahx",
    "hvl": ".hvl", "xm": ".xm", "s3m": ".s3m", "it": ".it", "okt": ".okt",
    "dbm": ".dbm", "mtm": ".mtm", "stm": ".stm", "digi": ".mod", "dgi": ".mod",
    "ptm": ".mod", "sid": ".sid", "psid": ".sid",
}
_EXT_SET = {e.lower() for e in SUPPORTED_EXTENSIONS}

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
    disk.  ``close`` removes the temp tree (called on cache eviction)."""

    def __init__(self, path: str):
        self._dir = tempfile.mkdtemp(prefix="sb_lha_")
        try:
            subprocess.run(
                [_LHA_BIN, f"xw={self._dir}", path],
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

    def read(self, member: str) -> bytes:
        full = self._files.get(member) or self._files.get(member.replace("\\", "/"))
        if full is None:
            raise KeyError(member)
        with open(full, "rb") as fh:
            return fh.read()

    def close(self):
        shutil.rmtree(self._dir, ignore_errors=True)


def _open(local_path):
    """Open a LOCAL archive: ZIP via stdlib; LHA via lhafile, falling back to
    the ``lha`` (lhasa) CLI for methods lhafile can't decode."""
    p = str(local_path)
    if p.lower().endswith(_LHA_EXTS):
        if HAVE_LHA:
            try:
                return _lhafile.LhaFile(p)
            except Exception as exc:
                if _LHA_BIN:
                    log.debug("lhafile can't decode %s (%s) — using lhasa", p, exc)
                    return _LhaCliArchive(p)
                raise
        if _LHA_BIN:
            return _LhaCliArchive(p)
        raise RuntimeError(
            "LHA archive support needs the 'lhafile' package or the 'lha' "
            "(lhasa) CLI"
        )
    return zipfile.ZipFile(p)


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
_OPEN_MAX = 8
_MAP_MAX = 256


def _cache_key(local_path) -> tuple:
    p = str(local_path)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    return (p, mtime)


def _cached_open(key, local_path):
    """Get-or-open the archive for *key* (parsed once).  Hold ``_CACHE_LOCK``."""
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


def _build_map(names) -> dict[str, str]:
    out: dict[str, str] = {}
    for real in names:
        disp = _display_name(real)
        if disp is None:
            continue
        name = disp
        i = 2
        while name in out:                # de-dup display names per archive
            stem, dot, ext = disp.rpartition(".")
            name = f"{stem} ({i}).{ext}" if (dot and stem) else f"{disp} ({i})"
            i += 1
        out[name] = real
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
        names = _cached_open(key, local_path).namelist()
        out = _build_map(names)
        _MAP_CACHE[key] = out
        while len(_MAP_CACHE) > _MAP_MAX:
            _MAP_CACHE.popitem(last=False)
        return out


def list_members(local_path) -> list[str]:
    """Playable member display-names inside *local_path* (.zip/.lha/.lzh).

    Returns ``[]`` (with a warning) on a broken/unsupported archive so one bad
    file never aborts a scan.
    """
    try:
        return list(_members(local_path).keys())
    except Exception as exc:
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
        return _cached_open(key, local_path).read(real)
