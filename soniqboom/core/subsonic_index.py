# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Derived library views for the Subsonic API (``/rest/*``).

Subsonic clients browse by artist → album → song, but SoniqBoom's store is a
flat track table with tag indexes.  This module derives the album-shaped view
the protocol needs in ONE pass over the track table, so no request ever walks
the library itself:

* **Artist union** — every name that is an album artist OR a track artist,
  de-duplicated case-insensitively (album-artist casing wins).  A library where
  only a handful of files carry a TPE2 tag must still list every artist.
  Placeholder names (``<?>``, ``???``, ``Unknown artist`` …, see
  ``ARTIST_PLACEHOLDERS``) are not artists: they fold into ``[Unknown Artist]``.
* **Album catalogue** — one entry per ``(owner, album)`` where *owner* is the
  album artist, else the track artist (``owner_of``; the same grouping
  getAlbumList uses), with exact song counts, total duration, a newest-track
  sample (cover art / year / created) and the set of genres ANY of its tracks
  carries — so a genre's album count and ``byGenre`` list are by membership,
  not by one sample track's tag.
* **Folder albums** (``subsonic_folder_albums``, on unless switched off) —
  album-less tracks (typical of SID / tracker / chip archives) grouped by
  directory.  A folder where one artist owns at least half of the tracks is
  ONE album, id ``fa:<dir_hash>``, credited to that artist (a composer's HVSC
  directory, a game soundtrack).  A mixed folder (a letter bucket of a module
  archive, a compo pack) is split into one album PER OWNER, id
  ``fa:<dir_hash>~<owner key>`` (``~0`` = the owner-less tracks, credited to
  ``[Unknown Artist]``) — so no artist's page lists thousands of strangers'
  songs.  A mixed folder whose own name is a trivial bucket label (``A``,
  ``0-9``, ``_`` — a module archive's letter buckets) is split per owner
  ACROSS its sibling buckets instead: one album per owner keyed on the
  nearest non-bucket ancestor (the scan root at the top), id
  ``fa:<ancestor hash>~g<owner key>``, named after that ancestor — so a
  composer gets one "modarchive 2007" album, not one per letter.  Name = the
  directory's cleaned name (``Gold_of_the_Aztecs`` → ``Gold of the Aztecs``,
  archive extensions dropped, a generic container like ``A-F`` qualified by
  its parent inside the scan root: ``GAMES / A-F`` — except under a person
  index like ``MUSICIANS``, where the folder IS a composer); a composer
  folder named ``Surname_Firstname`` (or ``Huelsbeck_Chris``) takes the
  composer's own spelling.  Several folder albums of ONE artist that end up
  with the same name (a composer's HVSC, YM and Amiga folders) are told apart
  by a qualifier: "David Whittaker (C64)", "(Atari ST)", "(Amiga)" — the
  format family, else the parent folder, else the scan root.
* **Appears-on** — for an artist with no albums of their own, the albums they
  appear on as a track artist (compilations, a ``feat.`` track of another
  owner's folder album) and, with folder albums on, the single-owner folder
  albums another artist dominates that hold their tracks — so the artist isn't
  an empty page.
* **[Unknown Artist]** — tracks with neither an album-artist nor an artist tag
  (untagged modules) file under one reserved artist (id ``artist_id("")``), so
  they stay browsable through getIndexes → getMusicDirectory.

The builder is pure: it reads the store and returns plain objects.  It runs
either in one go (``build_catalogue``) or as a chunked coroutine
(``build_catalogue_async``) that yields to the event loop every few
milliseconds, so a background rebuild of a 263k-track library never stalls
other requests.  Caching (keyed on the store's catalogue sequence + the
folder-albums flag, debounced during scans, stale-while-revalidate for large
libraries) lives with the API layer.  Everything here runs on the event loop
— the store is not thread-safe.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import gc
import hashlib
import math
import re
import time
from collections import Counter
from typing import Any, Iterator

from soniqboom.core import folder_album as _fa
from soniqboom.core import retro as _retro
from soniqboom.core.store import normalise_year

FOLDER_PREFIX = "fa:"
_NO_GENRES: frozenset = frozenset()
# Display name of the reserved owner-less artist (Navidrome's convention).
UNKNOWN_ARTIST = "[Unknown Artist]"
# ``fa:<dir hash>`` (a whole folder), ``fa:<dir hash>~<owner key>`` (one
# owner's share of a mixed folder; key ``0`` = the owner-less tracks) or
# ``fa:<ancestor hash>~g<owner key>`` (one owner's share of a group of mixed
# letter-bucket folders — see ``bucket_group``).
_FOLDER_REF_RE = re.compile(r"^fa:([0-9a-f]{16})(?:~(g?)(0|[0-9a-f]{12}))?$")


# ── Owner names ──────────────────────────────────────────────────────────────
# Tag conventions for "artist unknown" (HVSC writes ``<?>``) are not artists:
# listed as one, ``<?>`` became a 2,900-song artist beside [Unknown Artist].
# Compared case-folded; the song's own ``artist`` field still shows the tag.
# "Various Artists" is a real compilation owner and is NOT listed.
ARTIST_PLACEHOLDERS = frozenset({
    "<?>", "?", "??", "???", "unknown", "unknown artist", "<unknown>",
    "<unknown artist>", "<no artist>", "no artist", "n/a", "-",
})

# raw tag string → stripped name, "" for a placeholder.  Keyed by the tag
# string itself (its hash is cached on the str object), so the catalogue
# pass pays one dict lookup per field instead of strip + casefold.
_NORM: dict[str, str] = {}
_NORM_MAX = 400_000


def norm_owner(s: Any) -> str:
    """A tag's artist name, stripped; ``""`` when empty or a placeholder."""
    if not s or not isinstance(s, str):
        return ""
    hit = _NORM.get(s)
    if hit is None:
        v = s.strip()
        if v.casefold() in ARTIST_PLACEHOLDERS:
            v = ""
        if len(_NORM) >= _NORM_MAX:
            _NORM.clear()
        _NORM[s] = hit = v
    return hit


# raw tag string → ``norm_owner(s).lower()`` — what the catalogue pass keys
# owners on, one dict lookup per field on a hit.
_NORM_L: dict[str, str] = {}


def norm_owner_l(s: Any) -> str:
    """``norm_owner(s).lower()``, memoised on the raw tag string."""
    if not s or not isinstance(s, str):
        return ""
    hit = _NORM_L.get(s)
    if hit is None:
        if len(_NORM_L) >= _NORM_MAX:
            _NORM_L.clear()
        _NORM_L[s] = hit = norm_owner(s).lower()
    return hit


def owner_of(t: dict) -> str:
    """The artist a track files under: album artist, else track artist
    (placeholders ignored — ``""`` means [Unknown Artist])."""
    return norm_owner(t.get("album_artist")) or norm_owner(t.get("artist"))


# ── Stable ids ───────────────────────────────────────────────────────────────
# Artist / album ids hash the stripped, lower-cased name(s), so they survive a
# restart and are identical whichever casing / padding a tag happens to carry.

def _sha(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def artist_id(name: str) -> str:
    return "ar:" + _sha((name or "").strip().lower())


def album_id(album_artist: str, album: str) -> str:
    return "al:" + _sha((album_artist or "").strip().lower(),
                        (album or "").strip().lower())


_OWNER_KEYS: dict[str, str] = {}


def owner_key(owner_l: str) -> str:
    """The ``~<key>`` suffix of one owner's share of a mixed folder: ``0``
    for the owner-less tracks, else 12 hex of the lower-cased name's sha1
    (48 bits — no realistic collision among one folder's owners).  Memoised."""
    if not owner_l:
        return "0"
    hit = _OWNER_KEYS.get(owner_l)
    if hit is None:
        if len(_OWNER_KEYS) >= _NORM_MAX:
            _OWNER_KEYS.clear()
        hit = _OWNER_KEYS[owner_l] = hashlib.sha1(owner_l.encode("utf-8")).hexdigest()[:12]
    return hit


def folder_album_id(dir_hash: str, owner_l: str | None = None) -> str:
    """``fa:<dir_hash>`` for a whole folder; with ``owner_l`` (``""`` = the
    owner-less tracks) the id of that owner's share of a mixed folder."""
    if owner_l is None:
        return FOLDER_PREFIX + dir_hash
    return f"{FOLDER_PREFIX}{dir_hash}~{owner_key(owner_l)}"


def group_album_id(anc_hash: str, owner_l: str) -> str:
    """The id of one owner's share of a letter-bucket group (``bucket_group``)."""
    return f"{FOLDER_PREFIX}{anc_hash}~g{owner_key(owner_l)}"


def parse_folder_album_ref(raw: str) -> tuple[str, str | None] | None:
    """``fa:<16 hex>[~<key>]`` → ``(dir hash, owner key or None)``, else
    ``None`` (also for a group id — see ``parse_group_ref``)."""
    if raw and raw.startswith(FOLDER_PREFIX):
        m = _FOLDER_REF_RE.match(raw)
        if m and not m.group(2):
            return m.group(1), m.group(3)
    return None


def parse_group_ref(raw: str) -> tuple[str, str] | None:
    """``fa:<ancestor hash>~g<key>`` → ``(ancestor hash, owner key)``, else
    ``None``."""
    if raw and raw.startswith(FOLDER_PREFIX):
        m = _FOLDER_REF_RE.match(raw)
        if m and m.group(2):
            return m.group(1), m.group(3)
    return None


def parse_folder_album_id(raw: str) -> str | None:
    """The dir hash of a folder-album id (any form; a group's ancestor), else
    ``None``."""
    if raw and raw.startswith(FOLDER_PREFIX):
        m = _FOLDER_REF_RE.match(raw)
        if m:
            return m.group(1)
    return None


def is_synthetic_id(raw: str) -> bool:
    return raw.startswith(("ar:", "al:", FOLDER_PREFIX))


def truthy(v: Any) -> bool:
    """Config values may arrive as bools or as strings from a form."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _finite_int(v: Any) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(round(f)) if math.isfinite(f) else 0


def _finite(v: Any) -> float:
    """``v`` as a float ≥ 0, ``0.0`` when missing / non-numeric / non-finite."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return 0.0
    return float(v) if math.isfinite(v) and v > 0 else 0.0


# ── Multi-tune files ─────────────────────────────────────────────────────────
# A file with N subsongs (SID, SNDH, NSF …) is one track.  A tune is reached
# by its WIRE index ``w`` — ``?subsong=w`` on the web, ``<track id>~<w>`` on
# Subsonic — the one mapping the web player, the renderers and these ids share
# (frontend ``utils.js`` ``subsongWireToTune``): wire 0 is the file's DEFAULT
# tune (its header's start song; the scan's 0-based ``start_subsong`` = ``s``);
# when that isn't tune 1, wire ``s`` is tune 1 (the swap); every other wire
# ``w`` is tune ``w + 1``.  So the bare track id (wire 0) plays what the file
# plays by default, and every tune has exactly one wire.  The API lists a
# file as its bare id plus one Child per other wire, in tune order, at most
# ``SUB_CAP`` entries per file.  Album song counts and durations are computed
# from the SAME rule here, so the list views (catalogue) and getAlbum /
# getMusicDirectory (which expand the tunes) always agree.

SUB_CAP = 60


def start_index(t: dict) -> int:
    """0-based index of the file's default tune: the scan's ``start_subsong``
    when recorded and in range, else 0 (tune 1)."""
    s = t.get("start_subsong")
    if not s or not isinstance(s, int) or isinstance(s, bool):
        return 0
    cnt = t.get("subsongs")
    return s if isinstance(cnt, int) and 0 < s < cnt else 0


def wire_tune(t: dict, w: int) -> int:
    """The 0-based tune index wire ``w`` plays (see above)."""
    s = start_index(t)
    if w == 0:
        return s
    return 0 if s and w == s else w


def listed_tunes(t: dict) -> list[int]:
    """The wires of the file's NON-default tunes the listings add, in tune
    order (tune 1 first), capped so the file contributes at most ``SUB_CAP``
    entries."""
    cnt = t.get("subsongs")
    if not isinstance(cnt, int) or cnt <= 1:
        return []
    s = start_index(t)
    ws = [w for w in range(1, min(cnt, SUB_CAP + 1)) if w != s]
    if s:
        ws.insert(0, s)                          # tune 1
    return ws[:SUB_CAP - 1]


def tune_length(t: dict, k: int) -> float:
    """0-based tune ``k``'s own length (HVSC song lengths, indexed by tune),
    ``0.0`` when unknown."""
    lengths = t.get("hvsc_lengths")
    if isinstance(lengths, list) and 0 <= k < len(lengths):
        return _finite(lengths[k])
    return 0.0


def wire_length(t: dict, w: int) -> float:
    """The own length of the tune wire ``w`` plays, ``0.0`` when unknown."""
    return tune_length(t, wire_tune(t, w))


def default_duration(t: dict) -> float:
    """The bare id's duration: the default tune's own length when it isn't
    tune 1 and HVSC lists it (a scan that records ``start_subsong``, and
    hvsc_apply, store the DEFAULT tune's HVSC length as ``duration``; rows
    scanned before ``start_subsong`` was recorded still carry tune 1's),
    else the file's duration (raw, not rounded)."""
    s = start_index(t)
    if s:
        v = tune_length(t, s)
        if v:
            return v
    return _finite(t.get("duration"))


def tune_count(t: dict) -> int:
    """Entries the file contributes to an album listing (1 + listed tunes)."""
    cnt = t.get("subsongs")
    if not isinstance(cnt, int) or cnt <= 1:
        return 1
    return 1 + len(listed_tunes(t))


def tune_duration_sum(t: dict) -> float:
    """Raw (unrounded) total of the durations the file's entries report: the
    default tune's plus each listed tune's own HVSC length (a tune with no
    known length reports 0)."""
    total = default_duration(t)
    cnt = t.get("subsongs")
    if isinstance(cnt, int) and cnt > 1 and isinstance(t.get("hvsc_lengths"), list):
        for w in listed_tunes(t):
            total += wire_length(t, w)
    return total


def sample_key(t: dict) -> tuple:
    """The album-sample pick (cover art / year / created): newest ``added_at``,
    ties broken by the larger track id — the same pick everywhere."""
    a = t.get("added_at") or 0
    return (a if isinstance(a, (int, float)) else 0, t.get("id") or "")


# ── Scan roots ───────────────────────────────────────────────────────────────
# Folder names and client-facing paths are derived BELOW the scan root a file
# lives in — never from the mount components above it ("Volumes", "home").
# A remote root's files carry ``<root>:/<path>`` ("ftp://nas/Music/Demo:/x").

_SEP = re.compile(r"[\\/]")
_ROOTS: dict = {"sig": None, "roots": ()}


def scan_roots(store) -> tuple[str, ...]:
    """The store's scan-dir paths (trailing separators dropped), longest
    first so the innermost root wins.  Recomputed only when the scan-dir set
    changes — which also drops the folder-name memo, whose names depend on
    it.  O(#scan dirs) per call (a handful of keys)."""
    sd = getattr(store, "_scan_dirs", None)
    if not isinstance(sd, dict):
        return ()                  # no store / a stub: no roots, memo untouched
    sig = tuple(sd)
    if sig != _ROOTS["sig"]:
        roots = sorted({(p.rstrip("/\\") or p) for p in sig if isinstance(p, str) and p},
                       key=len, reverse=True)
        _ROOTS.update(sig=sig, roots=tuple(roots))
        _FOLDER_NAMES.clear()
        _GROUP_OF.clear()
        _GROUP_NAMES.clear()
    return _ROOTS["roots"]


def root_of(path: str, roots: tuple[str, ...]) -> str:
    """The innermost scan root ``path`` lies in (at a separator boundary:
    ``/``, ``\\``, a remote root's ``:`` or an archive's ``::``), else ``""``."""
    for r in roots:
        if path == r:
            return r
        if path.startswith(r) and path[len(r):len(r) + 1] in ("/", "\\", ":"):
            return r
    return ""


def below_root(path: str, root: str) -> str:
    """``path`` relative to ``root`` (which it lies in), archive ``::``
    separators turned into ``/``, no leading separator."""
    tail = path[len(root):] if root else path
    return tail.replace("::", "/").replace("\\", "/").lstrip(":/")


# ── Folder albums ────────────────────────────────────────────────────────────

# dir_hash → display name.  A dir hash is derived from the directory's path
# (sha256 prefix), so its name only changes with the scan-root set (the memo
# is dropped then — see ``scan_roots``): memoised across catalogue rebuilds —
# the cleaning below is regex work that would otherwise be paid for every
# folder on every rebuild.
_FOLDER_NAMES: dict[str, str] = {}
_FOLDER_NAMES_MAX = 400_000


def _clean_component(comp: str) -> str:
    """One path component → a display name (archive / wrapper extensions
    dropped, ``Gold_of_the_Aztecs`` → ``Gold of the Aztecs``).  A wrapper
    archive's music extension goes too (``tune.mod.zip`` → ``tune``, as
    ``folder_album._is_wrapper``), any other dotted word stays
    (``Turrican.II.zip`` → ``Turrican II``)."""
    stem = _fa._ARCHIVE_EXT_RE.sub("", comp)
    if stem != comp:
        head, _, ext = stem.rpartition(".")
        if head and ext.lower() in _fa._music_exts():
            stem = head                              # tune.mod.zip → tune
    return _fa.clean_folder_name(stem)


def _trivial(name: str) -> bool:
    """A bucket label that can't qualify anything ("A-F", "2", "1991")."""
    return sum(c.isalnum() for c in name) < 3 or not any(c.isalpha() for c in name)


def _folder_display_name(path: str, root: str = "") -> str:
    """The folder's cleaned innermost name.  A generic container ("S-Z",
    "games", "Disk 1") is qualified by its parent so sibling containers stay
    distinguishable: inside a scan root, the IMMEDIATE parent even when that
    is generic too ("GAMES / A-F" vs "DEMOS / A-F", "modarchive 2007 / R"),
    skipping only trivial bucket labels; nothing above the root is ever used
    (the root's own name stands in for a folder that IS the root).  Without
    a known root: the nearest non-generic ancestor within two levels."""
    if root:
        root_comps = [c for c in _SEP.split(root.split("::")[0]) if c.strip()]
        root_name = _clean_component(root_comps[-1]) if root_comps else ""
        comps = [c for c in _SEP.split(below_root(path, root)) if c.strip()]
        if not comps:
            return root_name
        name = _clean_component(comps[-1]) or comps[-1].strip()
        if name and _fa._generic(name):
            for anc in [_clean_component(c) for c in reversed(comps[:-1])] + [root_name]:
                if anc and not _trivial(anc):
                    if anc.lower() in _fa._PERSON_INDEX_NAMES:
                        # "MUSICIANS/D/Data": the folder IS a person (a
                        # composer's handle that happens to be a generic word).
                        return name
                    return f"{anc} / {name}"
        return name
    comps = [c for c in _SEP.split(path.replace("::", "/")) if c.strip()]
    if not comps:
        return ""
    name = _clean_component(comps[-1]) or comps[-1].strip()
    if name and _fa._generic(name):
        for anc in list(reversed(comps[:-1]))[:2]:
            a = _clean_component(anc)
            if a and not _fa._generic(a):
                return f"{a} / {name}"
    return name


def folder_album_name(store, dir_hash: str) -> str:
    """Display name of the directory behind ``dir_hash`` (``resolve_hash`` is
    an O(1) dict lookup; the cleaned name is memoised).  Never empty."""
    roots = scan_roots(store)
    hit = _FOLDER_NAMES.get(dir_hash)
    if hit is not None:
        return hit
    path = ""
    try:
        path = store.resolve_hash(dir_hash) or ""
    except Exception:            # noqa: BLE001 — a stub store / missing lookup
        path = ""
    name = ""
    if path:
        path = path.rstrip("/\\") or path
        try:
            name = _folder_display_name(path, root_of(path, roots))
        except Exception:        # noqa: BLE001 — naming must never fail a listing
            name = ""
        if not name:
            # Archive members carry "archive.zip::inner/dir" parents — the raw
            # innermost component is the fallback.
            tail = path.split("::")[-1].rstrip("/\\")
            name = (_SEP.split(tail)[-1] if tail else "").strip() or path.strip()
    if not name:
        return f"Folder {dir_hash[:6]}"          # unresolved: not memoised
    if len(_FOLDER_NAMES) >= _FOLDER_NAMES_MAX:
        _FOLDER_NAMES.clear()
    _FOLDER_NAMES[dir_hash] = name
    return name


# ── Letter-bucket groups ─────────────────────────────────────────────────────
# A module archive filed in letter buckets (``modarchive_2007/A`` … ``/Z``,
# HVSC ``DEMOS/A-F``) holds hundreds of composers per bucket, so splitting
# each bucket per owner gave a composer one album PER LETTER ("modarchive
# 2007 / T" ×3,409 on the owner's library).  A MIXED folder whose own name is
# a trivial bucket label (``_trivial``) is keyed on its nearest non-trivial
# ancestor instead — the scan root when the buckets sit right under it:
# every owner gets ONE album across all mixed sibling buckets, id
# ``fa:<ancestor hash>~g<owner key>`` (the ``g`` keeps it apart from the
# ancestor's own folder album, which may split the same way), named after the
# ancestor.  A single-owner bucket stays its own album.

_GROUP_OF: dict[str, str] = {}      # dir_hash → ancestor hash ("" = not a bucket)
_GROUP_NAMES: dict[str, str] = {}   # ancestor hash → display name


def _path_hash(path: str) -> str:
    """The dir hash of ``path`` — the same digest ``TrackStore.store_hash_lookup``
    uses, computed without registering anything in the store."""
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def _strip_last(path: str) -> str:
    """``path`` without its last component (``/``, ``\\`` or an archive's ``::``)."""
    i = max(path.rfind("/"), path.rfind("\\"))
    j = path.rfind("::")
    if j > i:
        return path[:j]
    return path[:i] if i > 0 else ""


def bucket_group(store, dir_hash: str) -> str:
    """For a folder whose cleaned name is a trivial bucket label: the hash of
    its nearest ancestor whose name is not (never above its scan root; the
    root itself when every level up to it is a bucket).  ``""`` for any other
    folder, or one outside every scan root.  Memoised per dir hash (dropped
    with the folder-name memo when the scan roots change); records the
    ancestor's display name for ``group_name``."""
    roots = scan_roots(store)
    hit = _GROUP_OF.get(dir_hash)
    if hit is not None:
        return hit
    g = ""
    try:
        path = store.resolve_hash(dir_hash) or ""
    except Exception:            # noqa: BLE001 — a stub store / missing lookup
        path = ""
    if path:
        path = path.rstrip("/\\") or path
        root = root_of(path, roots)
        comps = [c for c in _SEP.split(below_root(path, root)) if c.strip()] if root else []
        if comps and _trivial(_clean_component(comps[-1]) or comps[-1].strip()):
            anc = path
            k = len(comps) - 1
            while True:
                anc = _strip_last(anc)
                if k == 0:
                    anc = root
                    break
                if not _trivial(_clean_component(comps[k - 1]) or comps[k - 1].strip()):
                    break
                k -= 1
            if anc:
                g = _path_hash(anc)
                if g not in _GROUP_NAMES:
                    try:
                        name = _folder_display_name(anc, root)
                    except Exception:    # noqa: BLE001 — naming must never fail a build
                        name = ""
                    _GROUP_NAMES[g] = name or f"Folder {g[:6]}"
    if len(_GROUP_OF) >= _FOLDER_NAMES_MAX:
        _GROUP_OF.clear()
    _GROUP_OF[dir_hash] = g
    return g


def group_name(store, anc_hash: str) -> str:
    """Display name of a letter-bucket group's ancestor.  Never empty."""
    hit = _GROUP_NAMES.get(anc_hash)
    return hit if hit else folder_album_name(store, anc_hash)


# ── Same-name folder albums ──────────────────────────────────────────────────
# One artist's folder albums can clean to the same name (HVSC
# ``MUSICIANS/W/Whittaker_David``, ``cta-ym/whittaker,_david`` and
# ``uade/David Whittaker`` are all "David Whittaker" by David Whittaker):
# after the build, every such name gets a qualifier that tells them apart —
# the format family ("C64", "Atari ST", "Amiga"), then the nearest non-bucket
# parent folder, then the scan root, then a short stable hash.  A tag album
# of the same name keeps its own name.

_FAMILY_LABELS = {
    "sid": "C64", "paula": "Amiga", "ahx": "Amiga", "tracker": "Tracker",
    "atari": "Atari ST", "adlib": "AdLib", "psf": "PSF", "midi": "MIDI",
    "nes": "NES", "snes": "SNES", "gameboy": "Game Boy", "ay": "AY",
    "pokey": "Atari 8-bit", "genesis": "Mega Drive", "pce": "PC Engine", "vgm": "VGM",
}


def _format_label(e: "AlbumEntry") -> str:
    """The entry's format family for a qualifier ("C64", "Amiga"; a modern
    format by its own name, "FLAC")."""
    fmt = (e.sample or {}).get("format")
    if not isinstance(fmt, str) or not fmt.strip():
        return ""
    fam = _retro.chip_family(fmt)
    if fam:
        return _FAMILY_LABELS.get(fam, fam.upper())
    return fmt.strip()


def _path_labels(store, dir_hash: str | None, roots: tuple[str, ...]) -> tuple[str, str]:
    """``(nearest non-bucket parent folder, scan root)`` names of a folder."""
    try:
        path = (store.resolve_hash(dir_hash) or "") if dir_hash else ""
    except Exception:            # noqa: BLE001 — a stub store / missing lookup
        path = ""
    if not path:
        return "", ""
    path = path.rstrip("/\\") or path
    root = root_of(path, roots)
    comps = [c for c in _SEP.split(below_root(path, root) if root else path.replace("::", "/"))
             if c.strip()]
    parent = ""
    for c in reversed(comps[:-1]):
        n = _clean_component(c) or c.strip()
        if n and not _trivial(n):
            parent = n
            break
    root_name = ""
    if root:
        rc = [c for c in _SEP.split(root.split("::")[0]) if c.strip()]
        root_name = (_clean_component(rc[-1]) or rc[-1].strip()) if rc else ""
    return parent, root_name


def _disambiguate(store, same: list, roots: tuple[str, ...]) -> None:
    """Qualify the folder entries of ``same`` (one artist's entries sharing a
    name, at least one a folder album) so every name in it is unique: each
    level's label is appended only to entries that still clash."""
    n = len(same)
    fixed = [e.kind != "folder" for e in same]
    quals = [""] * n
    paths: list | None = None
    for level in range(4):
        counts = Counter(quals)
        clash = [i for i in range(n) if counts[quals[i]] > 1 and not fixed[i]]
        if not clash:
            break
        if level == 1 and paths is None:
            paths = [_path_labels(store, e.dir_hash, roots) for e in same]
        for i in clash:
            e = same[i]
            if level == 0:
                v = _format_label(e)
            elif level in (1, 2):
                v = paths[i][level - 1]
            else:
                v = hashlib.sha256(e.id.encode()).hexdigest()[:6]
            if not v or v.casefold() == e.name.casefold() or v in quals[i].split(", "):
                continue
            quals[i] = f"{quals[i]}, {v}" if quals[i] else v
    for i, e in enumerate(same):
        if quals[i] and not fixed[i]:
            e.name = f"{e.name} ({quals[i]})"


# German / Nordic transliterations (HVSC folder names spell ``Hülsbeck`` as
# ``Huelsbeck``, ``Bjørnerud`` as ``Bjoernerud``, ``Hård`` as ``Haard``):
# applied to lower-cased text BEFORE the accent fold, which would otherwise
# reduce ``ü`` to ``u`` and drop ``ø`` / ``å`` distinctions.
_TRANSLIT = str.maketrans({"ä": "ae", "æ": "ae", "ö": "oe", "ø": "oe", "œ": "oe",
                           "ü": "ue", "ß": "ss", "å": "aa"})


@functools.lru_cache(maxsize=4096)
def translit_name_key(s: str) -> str:
    """``folder_album.name_key`` of ``s`` with the transliterations above
    applied first (``Chris Hülsbeck`` → ``chris huelsbeck``)."""
    return _fa.name_key((s or "").lower().translate(_TRANSLIT))


def owner_named(name: str, owner_l: str, owner_disp: str) -> str:
    """A single-owner folder named after its composer in another word order
    (HVSC ``Turner_Steve`` → "Turner Steve", by Steve Turner) takes the
    composer's own spelling; any other name is kept.  Order-insensitive,
    accent-folded (``folder_album.name_key``: ``Hulsbeck`` matches
    ``Hülsbeck``) or transliterated (ue / oe / ae / aa / ss: ``Huelsbeck``,
    ``Bjoernerud``, ``Haard`` match ``Hülsbeck``, ``Bjørnerud``, ``Hård``)."""
    if (not owner_l or not name or name == owner_disp or owner_disp == UNKNOWN_ARTIST
            or " / " in name):
        return name
    k = _fa.name_key(name)
    if not k:
        return name
    if k == _fa.name_key(owner_disp) or k == translit_name_key(owner_disp):
        return owner_disp
    return owner_disp if translit_name_key(name) == translit_name_key(owner_disp) else name


def dominant_owner(owners: Counter | dict) -> str:
    """Most common NON-EMPTY owner (lower-cased) — ties broken alphabetically
    so every caller derives the same artist for the same folder."""
    best: tuple[int, str] | None = None
    for k, c in owners.items():
        if not k:
            continue
        cand = (-c, k)
        if best is None or cand < best:
            best = cand
    return best[1] if best else ""


def single_owner(owners: Counter | dict, total: int) -> str | None:
    """The owner a folder is credited to as ONE album — the dominant
    non-empty owner when it holds at least half of the folder's album-less
    tracks, ``""`` when every track is owner-less — else ``None`` (a mixed
    folder, split per owner)."""
    dom = dominant_owner(owners)
    if not dom:
        return ""
    return dom if owners[dom] * 2 >= total else None


# ── Artist union ─────────────────────────────────────────────────────────────

class ArtistUnion:
    """Album artists ∪ track artists, keyed by lower-cased name."""

    __slots__ = ("display", "by_id", "id_of")

    def __init__(self, display: dict[str, str], *, index: bool = True) -> None:
        self.display = display                        # lower → display name
        self.id_of: dict[str, str] = {}               # lower → ar:…
        self.by_id: dict[str, str] = {}               # ar:… → lower
        if index:
            for low in display:
                self._index(low)

    def _index(self, low: str) -> None:
        i = self.id_of[low] = artist_id(low)
        self.by_id[i] = low

    def artist_id(self, low: str) -> str:
        """``artist_id`` without re-hashing a known name."""
        return self.id_of.get(low) or artist_id(low)

    def add(self, low: str, name: str) -> None:
        self.display[low] = name
        self._index(low)


def build_artist_union(store) -> ArtistUnion:
    """Album artists ∪ track artists from the store's maintained counters.

    The display casing of a name tagged several ways ("4-Mat" / "4-MAT") is
    taken from the track with the SMALLEST id in that name's tag bucket — a
    deterministic pick.  (``aggregate_artists`` takes ``next(iter(set))``,
    whose order follows the per-process string-hash seed, so the casing — and
    with it getIndexes' content fingerprint / ``lastModified`` — could change
    on every restart.)  ``min`` over the bucket is C-speed: O(N) total."""
    display: dict[str, str] = {}
    for _ in _union_steps(store, display, chunk=1 << 30):
        pass
    return ArtistUnion(display)


def _union_steps(store, display: dict[str, str], *, chunk: int) -> Iterator[None]:
    """Fill ``display`` (lower → display name), yielding every ``chunk`` names.
    The counters' keys are snapshotted first: between two steps of a chunked
    build the store may change, and a live dict must not be iterated across
    a yield.  Each name's bucket is read at the moment it is processed.
    Placeholder names (``norm_owner`` → "") are skipped."""
    tracks = store._tracks
    # Album-artist casing first so it wins for names that are both.
    for idx, counter, field in ((store._tag_album_artist, store._agg_album_artists, "album_artist"),
                                (store._tag_artist, store._agg_artists, "artist")):
        keys = list(counter)
        for i in range(0, len(keys), chunk):
            for key in keys[i:i + chunk]:
                if not key or key in display or not norm_owner(key):
                    continue
                tids = idx.get(key)
                if not tids:
                    continue
                t = tracks.get(min(tids)) or {}
                display[key] = (t.get(field) or "").strip() or key
            yield


# ── Album catalogue ──────────────────────────────────────────────────────────

class AlbumEntry:
    __slots__ = ("id", "kind", "name", "artist", "artist_l", "artist_id",
                 "song_count", "duration", "sample", "year", "genre",
                 "genres_l", "added", "dir_hash", "dir_hashes")

    def __init__(self) -> None:
        self.dir_hash: str | None = None
        # A letter-bucket group's member folders (``dir_hash`` = the ancestor).
        self.dir_hashes: tuple = ()


class Catalogue:
    """One immutable snapshot of the album-shaped library view."""

    def __init__(self) -> None:
        self.folder_on = False
        self.built_ms = 0
        # CPU seconds the build itself took (excluding event-loop yields of a
        # chunked build) — the API layer paces rebuilds by it.
        self.build_sec = 0.0
        self.entries: list[AlbumEntry] = []
        self.by_id: dict[str, AlbumEntry] = {}
        self.by_key: dict[tuple[str, str], AlbumEntry] = {}
        self.artist_albums: dict[str, list[AlbumEntry]] = {}
        self.appears_on: dict[str, list[AlbumEntry]] = {}
        self.genre_albums: dict[str, list[AlbumEntry]] = {}
        # The artist union this snapshot was built with — ONE snapshot serves
        # artists and albums together so they can never drift apart.
        self.union: ArtistUnion | None = None
        # Album-less tracks with no artist tag at all (the "[Unknown Artist]"
        # directory's loose songs) — the tag indexes can't express "empty".
        self.unknown_loose: list[str] = []
        # Mixed letter-bucket folder → its group's ancestor hash (folder
        # albums on): its owners' shares are ``group_album_id`` entries.
        self.dir_group: dict[str, str] = {}
        # Lazily-filled per-snapshot memos (sorted lists, genre id sets, the
        # getIndexes payload, play/rating rankings).  Die with the snapshot.
        self.memo: dict[Any, Any] = {}

    def album_count(self, artist_l: str) -> int:
        own = self.artist_albums.get(artist_l)
        if own:
            return len(own)
        return len(self.appears_on.get(artist_l, ()))

    def albums_for_artist(self, artist_l: str) -> list[AlbumEntry]:
        """Own albums (incl. their folder albums), else appears-on."""
        return self.artist_albums.get(artist_l) or self.appears_on.get(artist_l) or []

    def genre_sorted(self, genre_l: str) -> list[AlbumEntry]:
        """One genre's albums, name-ordered — sorted on first use, memoised
        (an unknown genre — a client-supplied string — is never memoised)."""
        if genre_l not in self.genre_albums:
            return []
        key = ("genre_sorted", genre_l)
        hit = self.memo.get(key)
        if hit is None:
            hit = self.memo[key] = sorted(self.genre_albums.get(genre_l, ()),
                                          key=lambda e: (e.name.lower(), e.id))
        return hit

    def track_entry(self, t: dict) -> AlbumEntry | None:
        """The catalogue entry a track belongs to (O(1)): its tagged album,
        else (folder albums on) its folder album — the whole folder's, or its
        owner's share of a mixed folder (of a letter-bucket group)."""
        al = (t.get("album") or "").strip()
        if al:
            return self.by_key.get((owner_of(t).lower(), al.lower()))
        if self.folder_on:
            dh = t.get("dir_hash")
            if dh:
                e = self.by_id.get(FOLDER_PREFIX + dh)
                if e is None:
                    g = self.dir_group.get(dh)
                    ol = owner_of(t).lower()
                    e = self.by_id.get(group_album_id(g, ol) if g else folder_album_id(dh, ol))
                return e
        return None


def _entry_sort_key(e: AlbumEntry) -> tuple:
    return (e.year or 0, e.name.lower(), e.id)


@contextlib.contextmanager
def _gc_paused():
    """Pause the cyclic GC for one build (or one slice of a chunked build).
    The pass allocates tens of thousands of small acyclic objects; with GC on
    they trip repeated generation-2 collections, each traversing the whole
    (huge) track heap.  Paused, the allocation count only triggers collection
    once the build is done — usually a single young-generation pass,
    occasionally one full collection.  Measured on a 263k synthetic library,
    8 consecutive builds INCLUDING the deferred collection afterwards: 3.83 s
    paused vs 4.89-5.24 s unpaused (median build ~390 vs ~740 ms).  Nothing
    here creates cycles."""
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


# Items per step of a chunked (async) build: tracks folded / names indexed per
# step (entries built: a quarter of it).  The async driver runs steps back to
# back until ``_SLICE_SEC`` of CPU has passed, then yields to the event loop —
# so a stall is bounded by the slice plus one step, whatever the library size.
BUILD_CHUNK = 4_000
_SLICE_SEC = 0.006
# The yield between slices is a (tiny) timer, not ``sleep(0)``: a
# ``sleep(0)`` continuation is already queued when the loop polls for I/O, so
# every request that arrived meanwhile waited for the NEXT slice too.  As a
# timer it is queued after the poll — requests and due timers run first.
_YIELD_SEC = 0.0005


def build_catalogue(store, *, folder_on: bool) -> Catalogue:
    """The single full pass (artist union included), in one go.  O(N) over
    the track table + O(#artists) + O(A log A) sorts."""
    out: list[Catalogue] = []
    t0 = time.perf_counter()
    with _gc_paused():
        for _ in _build_steps(store, folder_on, 1 << 30, out):
            pass
    out[0].build_sec = time.perf_counter() - t0
    return out[0]


async def build_catalogue_async(store, *, folder_on: bool,
                                chunk: int = BUILD_CHUNK) -> Catalogue:
    """The same build, yielding to the event loop after every ~``_SLICE_SEC``
    of work (steps of ``chunk`` tracks / names / entries run back to back
    within a slice).  The cyclic GC is paused for each slice only — never
    across an await.  The track table is snapshotted up front: a track
    changed or deleted mid-build is at worst reflected in the NEXT build (the
    caller records the store sequence read before starting)."""
    out: list[Catalogue] = []
    steps = _build_steps(store, folder_on, max(1, chunk), out)
    busy = 0.0
    done = False
    while not done:
        t0 = time.perf_counter()
        with _gc_paused():
            while True:
                if next(steps, StopIteration) is StopIteration:
                    done = True
                    break
                if time.perf_counter() - t0 >= _SLICE_SEC:
                    break
        busy += time.perf_counter() - t0
        if not done:
            await asyncio.sleep(_YIELD_SEC)
    out[0].build_sec = busy
    return out[0]


def _build_steps(store, folder_on: bool, chunk: int, out: list) -> Iterator[None]:
    """The catalogue build as a generator yielding between chunks; appends
    the finished ``Catalogue`` to ``out``."""
    display: dict[str, str] = {}
    yield from _union_steps(store, display, chunk=chunk)
    union = ArtistUnion(display, index=False)
    names = list(display)
    for i in range(0, len(names), chunk):
        for low in names[i:i + chunk]:
            union._index(low)
        yield
    del names
    rows = list(store._tracks.values())
    yield
    # Building an entry costs ~4x folding a track: smaller steps for those.
    echunk = max(1, chunk // 4)
    unknown_loose: list[str] = []
    # key (owner_l, album_l) → [count, duration, sample track, sample added, genres]
    acc: dict[tuple[str, str], list] = {}
    appears: dict[str, set] = {}
    # dir_hash → {owner_l: [count, duration, sample, added, genres, track artists]}
    folders: dict[str, dict[str, list]] = {}
    nl_get = _NORM_L.get

    for i in range(0, len(rows), chunk):
        for t in rows[i:i + chunk]:
            # Owners as their lower-cased normalised names (placeholders →
            # ""): inlined ``norm_owner_l``, a memo hit is one dict lookup.
            aa = t.get("album_artist")
            if aa:
                v = nl_get(aa) if isinstance(aa, str) else ""
                aa = v if v is not None else norm_owner_l(aa)
            ar = t.get("artist")
            if ar:
                v = nl_get(ar) if isinstance(ar, str) else ""
                ar = v if v is not None else norm_owner_l(ar)
            al = t.get("album")
            if al:
                al = al.strip()
            if al:
                owner_l = aa or ar or ""
                key = (owner_l, al.lower())
                added = t.get("added_at") or 0
                a = acc.get(key)
                if a is None:
                    a = acc[key] = [0, 0.0, t, added, None]
                elif added > a[3] or (added == a[3] and t["id"] > a[2]["id"]):
                    a[2] = t                  # the ``sample_key`` pick
                    a[3] = added
                sc = t.get("subsongs")
                if sc and type(sc) is int and sc > 1:
                    # A multi-tune file: one entry per listed tune.
                    a[0] += tune_count(t)
                    a[1] += tune_duration_sum(t)
                else:
                    a[0] += 1
                    d = t.get("duration")
                    if d and d - d == 0 and d > 0:   # skip 0 / NaN / ±inf (corrupt decode)
                        a[1] += d
                gs = t.get("genre")
                if gs:
                    if a[4] is None:
                        a[4] = set(gs)
                    else:
                        a[4].update(gs)
                if aa and ar and ar != owner_l:
                    s_ = appears.get(ar)
                    if s_ is None:
                        s_ = appears[ar] = set()
                    s_.add(key)
            elif folder_on:
                ok = aa or ar or ""
                if not ok:
                    unknown_loose.append(t["id"])
                dh = t.get("dir_hash")
                if not dh:
                    # Only tracks ingested outside a scan lack a folder: they
                    # stay reachable as songs of their artist's directory.
                    continue
                added = t.get("added_at") or 0
                f = folders.get(dh)
                if f is None:
                    f = folders[dh] = {}
                s = f.get(ok)
                if s is None:
                    s = f[ok] = [0, 0.0, t, added, None, None]
                elif added > s[3] or (added == s[3] and t["id"] > s[2]["id"]):
                    s[2] = t
                    s[3] = added
                sc = t.get("subsongs")
                if sc and type(sc) is int and sc > 1:
                    s[0] += tune_count(t)
                    s[1] += tune_duration_sum(t)
                else:
                    s[0] += 1
                    d = t.get("duration")
                    if d and d - d == 0 and d > 0:
                        s[1] += d
                gs = t.get("genre")
                if gs:
                    if s[4] is None:
                        s[4] = set(gs)
                    else:
                        s[4].update(gs)
                if aa and ar and ar != ok:
                    # A "feat." track in an album-artist-owned folder: the
                    # track artist appears on that folder album.
                    if s[5] is None:
                        s[5] = {ar}
                    else:
                        s[5].add(ar)
            elif not (aa or ar):
                unknown_loose.append(t["id"])       # same owner rule as owner_of()
        yield
    del rows
    yield

    cat = Catalogue()
    cat.folder_on = folder_on
    cat.union = union
    cat.unknown_loose = unknown_loose
    # Provisionally register the reserved owner-less artist so entries owned
    # by "" get its name / id; dropped again below if nothing is filed there.
    union.add("", UNKNOWN_ARTIST)
    disp = union.display
    glow: dict[str, str] = {}          # raw genre → stripped lower (memo)
    years: dict[Any, int] = {}         # raw year → normalised (memo)

    gsets: dict[frozenset, frozenset] = {}   # interned genre sets (few distinct)

    def _genres_l(gs: set) -> frozenset:
        out_ = []
        for g in gs:
            gl = glow.get(g)
            if gl is None:
                gl = glow[g] = g.strip().lower() if isinstance(g, str) else ""
            if gl:
                out_.append(gl)
        fs = frozenset(out_)
        hit = gsets.get(fs)
        if hit is None:
            hit = gsets[fs] = fs
        return hit

    def _fill(e: AlbumEntry, owner_l: str, cnt: int, dur: float, sample: dict,
              added: Any, gs: set | None) -> None:
        e.artist_l = owner_l
        e.artist = disp.get(owner_l) or owner_of(sample) or UNKNOWN_ARTIST
        e.artist_id = union.artist_id(owner_l)
        e.song_count = cnt
        e.duration = _finite_int(dur)
        e.sample = sample
        y = sample.get("year")
        try:
            yr = years.get(y)
        except TypeError:                 # an unhashable junk value
            yr = normalise_year(y) or 0
        else:
            if yr is None:
                yr = years[y] = normalise_year(y) or 0
        e.year = yr
        sg = sample.get("genre") or []
        e.genre = (sg[0] if sg else "") or ""
        e.genres_l = _genres_l(gs) if gs else _NO_GENRES
        e.added = added if isinstance(added, (int, float)) else 0

    # The accumulators are released as they are consumed (each slot
    # set to None once its entry exists), so freeing them is spread over the
    # steps instead of landing in one.
    items = list(acc.items())
    del acc
    yield
    for i in range(0, len(items), echunk):
        chunk = items[i:i + echunk]
        items[i:i + echunk] = [None] * len(chunk)
        for key, (cnt, dur, sample, added, gs) in chunk:
            owner_l, al_l = key
            e = AlbumEntry()
            e.id = album_id(owner_l, al_l)
            e.kind = "album"
            e.name = (sample.get("album") or "").strip()
            _fill(e, owner_l, cnt, dur, sample, added, gs)
            cat.entries.append(e)
            cat.by_id[e.id] = e
            cat.by_key[key] = e
        del chunk
        yield
    del items
    yield

    appears_folder: dict[str, list[AlbumEntry]] = {}
    fitems = list(folders.items())
    del folders
    yield
    # ``work`` counts entries built / owner slots merged since the last
    # yield: a letter bucket with thousands of owners is split across steps.
    work = 0
    entries_append = cat.entries.append
    by_id = cat.by_id
    dir_group = cat.dir_group
    # ancestor hash → owner → [count, duration, sample, added, genres,
    # track artists, member dir hashes]
    group_acc: dict[str, dict[str, list]] = {}
    for j in range(len(fitems)):
        dh, owners = fitems[j]
        fitems[j] = None
        fname = folder_album_name(store, dh)
        total = 0
        for s in owners.values():
            total += s[0]
        dom = ""
        best = None
        for o, s in owners.items():
            if o:
                cand = (-s[0], o)
                if best is None or cand < best:
                    best = cand
        if best is not None:
            dom = best[1]
        if not dom or owners[dom][0] * 2 >= total:
            # ONE album: the owner holds at least half of the folder.
            dur = 0.0
            sample = None
            added = None
            gs: set | None = None
            feat: set = set()
            for s in owners.values():
                dur += s[1]
                if sample is None or (s[3], s[2]["id"]) > (added, sample["id"]):
                    sample, added = s[2], s[3]
                if s[4]:
                    gs = set(s[4]) if gs is None else (gs | s[4])
                if s[5]:
                    feat |= s[5]
            e = AlbumEntry()
            e.id = FOLDER_PREFIX + dh
            e.kind = "folder"
            e.dir_hash = dh
            _fill(e, dom, total, dur, sample, added, gs)
            e.name = owner_named(fname, dom, e.artist)
            cat.entries.append(e)
            cat.by_id[e.id] = e
            for o in owners:
                # "" included: owner-less tracks in a folder a named artist
                # dominates make it an appears-on album of [Unknown Artist].
                if o != dom:
                    appears_folder.setdefault(o, []).append(e)
            for fa_l in feat:
                if fa_l != dom and fa_l not in owners:
                    appears_folder.setdefault(fa_l, []).append(e)
            work += len(owners)
        elif (g := bucket_group(store, dh)):
            # A mixed letter bucket: its owners' shares join the group of
            # its sibling buckets (entries built below, once all are in).
            dir_group[dh] = g
            ga = group_acc.get(g)
            if ga is None:
                ga = group_acc[g] = {}
            for o, s in owners.items():
                m = ga.get(o)
                if m is None:
                    ga[o] = [s[0], s[1], s[2], s[3], s[4], s[5], [dh]]
                    continue
                m[0] += s[0]
                m[1] += s[1]
                if (s[3], s[2]["id"]) > (m[3], m[2]["id"]):
                    m[2], m[3] = s[2], s[3]
                if s[4]:
                    if m[4] is None:
                        m[4] = s[4]
                    else:
                        m[4].update(s[4])        # this build's own sets
                if s[5]:
                    if m[5] is None:
                        m[5] = s[5]
                    else:
                        m[5].update(s[5])
                m[6].append(dh)
            work += len(owners)
        else:
            # A mixed folder (letter bucket, compo pack): one album per
            # owner, credited to that owner only — nobody's page lists the
            # whole folder.  (``owners`` is this build's own dict: safe to
            # iterate across yields.)
            pre = f"{FOLDER_PREFIX}{dh}~"          # == folder_album_id(dh, o)
            for o, (cnt, dur, sample, added, gs, feat) in owners.items():
                e = AlbumEntry()
                e.id = eid = pre + owner_key(o)
                e.kind = "folder"
                e.dir_hash = dh
                e.name = fname
                _fill(e, o, cnt, dur, sample, added, gs)
                entries_append(e)
                by_id[eid] = e
                if feat:
                    for fa_l in feat:
                        if fa_l != o:
                            appears_folder.setdefault(fa_l, []).append(e)
                work += 1
                if work >= echunk:
                    work = 0
                    yield
        del owners
        if work >= echunk:
            work = 0
            yield
    del fitems
    yield
    gitems = list(group_acc.items())
    del group_acc
    for j in range(len(gitems)):
        g, owners = gitems[j]
        gitems[j] = None
        gname = group_name(store, g)
        pre = f"{FOLDER_PREFIX}{g}~g"              # == group_album_id(g, o)
        for o, (cnt, dur, sample, added, gs, feat, members) in owners.items():
            e = AlbumEntry()
            e.id = eid = pre + owner_key(o)
            e.kind = "folder"
            e.dir_hash = g
            e.dir_hashes = tuple(members)
            e.name = gname
            _fill(e, o, cnt, dur, sample, added, gs)
            entries_append(e)
            by_id[eid] = e
            if feat:
                for fa_l in feat:
                    if fa_l != o:
                        appears_folder.setdefault(fa_l, []).append(e)
            work += 1
            if work >= echunk:
                work = 0
                yield
        del owners
    del gitems
    yield

    if folder_on:
        # One artist's same-name folder albums get a qualifier each
        # (``_disambiguate``): one dict insert per entry, work only for the
        # few names that clash.
        seen_names: dict[tuple[str, str], Any] = {}
        ents = cat.entries
        step = echunk * 4                  # a dict insert, not an entry build
        for i in range(0, len(ents), step):
            for e in ents[i:i + step]:
                k = (e.artist_l, e.name.casefold())
                b = seen_names.get(k)
                if b is None:
                    seen_names[k] = e
                elif type(b) is list:
                    b.append(e)
                else:
                    seen_names[k] = [b, e]
            yield
        clashes = [b for b in seen_names.values()
                   if type(b) is list and any(e.kind == "folder" for e in b)]
        del seen_names
        yield
        roots = scan_roots(store)
        for i in range(0, len(clashes), echunk):
            for b in clashes[i:i + echunk]:
                _disambiguate(store, b, roots)
            yield
        del clashes

    ents = cat.entries
    for i in range(0, len(ents), echunk):
        for e in ents[i:i + echunk]:
            cat.artist_albums.setdefault(e.artist_l, []).append(e)
            for g in e.genres_l:
                cat.genre_albums.setdefault(g, []).append(e)
        yield
    lists = list(cat.artist_albums.values())
    for i in range(0, len(lists), echunk):
        for lst in lists[i:i + echunk]:
            if len(lst) > 1:
                lst.sort(key=_entry_sort_key)
        yield
    del lists
    ap_keys = list(appears.keys() | appears_folder.keys())
    yield
    for i in range(0, len(ap_keys), echunk):
        for ar_l in ap_keys[i:i + echunk]:
            lst = [cat.by_key[k] for k in appears.get(ar_l, ()) if k in cat.by_key]
            lst.extend(appears_folder.get(ar_l, ()))
            lst.sort(key=_entry_sort_key)
            cat.appears_on[ar_l] = lst
        yield
    # genre_albums lists stay UNSORTED here: getGenres only needs their
    # lengths; byGenre sorts one genre's list on demand (``genre_sorted``).
    if not cat.artist_albums.get("") and not unknown_loose:
        union.display.pop("", None)
        union.by_id.pop(union.id_of.pop("", ""), None)
    cat.built_ms = int(time.time() * 1000)
    out.append(cat)


# ── Direct (pass-free) folder-album resolution ───────────────────────────────

def _track_order(t: dict) -> tuple:
    return (_finite_int(t.get("disc_number")), _finite_int(t.get("track_number")),
            (t.get("title") or "").lower(), t.get("id") or "")


def _dir_album_less(store, dir_hash: str, owner: str | None) -> list[dict]:
    tids = store._tag_dir_hash.get(dir_hash) or ()
    out = []
    for tid in tids:
        t = store.get_track(tid)
        if t and not (t.get("album") or "").strip():
            if owner is not None and owner_key(owner_of(t).lower()) != owner:
                continue
            out.append(t)
    return out


def folder_album_tracks(store, dir_hash: str, owner: str | None = None) -> list[dict]:
    """Album-less tracks in one directory — O(dir size) via the dir-hash tag
    index, no library pass.  ``owner`` (an ``owner_key``): only that owner's
    share of the folder.  Sorted disc → track → title."""
    out = _dir_album_less(store, dir_hash, owner)
    out.sort(key=_track_order)
    return out


def _is_mixed(store, dir_hash: str, memo: dict) -> bool:
    """Whether a folder's album-less tracks are split per owner (no owner
    holds at least half) — the build's rule, from the folder's own tracks."""
    hit = memo.get(dir_hash)
    if hit is None:
        owners = Counter(owner_of(t).lower() for t in _dir_album_less(store, dir_hash, None))
        hit = memo[dir_hash] = bool(owners) and single_owner(owners, sum(owners.values())) is None
    return hit


def _owner_candidates(store, cat: Catalogue | None, okey: str) -> list[str]:
    """Track ids that may be owned by the owner behind ``okey`` — the
    owner-less album-less tracks for ``0``, else the owner's tag buckets
    (the owner found through the snapshot's artist union, reverse-mapped
    once per snapshot).  Bounded by that owner's size, never the library."""
    if cat is None:
        return []
    if okey == "0":
        return cat.unknown_loose
    rev = cat.memo.get("owner_keys")
    if rev is None:
        rev = cat.memo["owner_keys"] = {owner_key(low): low for low in cat.union.display if low}
    low = rev.get(okey)
    if not low:
        return []
    a = store._tag_album_artist.get(low) or ()
    b = store._tag_artist.get(low) or ()
    return list(set(a) | set(b)) if (a and b) else list(a or b)


def group_album_tracks(store, cat: Catalogue | None, anc_hash: str, okey: str) -> list[dict]:
    """One owner's album-less tracks across a letter-bucket group's mixed
    member folders, sorted disc → track → title.  From the snapshot's entry
    (its member folders) when it has one; otherwise pass-free from the
    owner's own tracks (O(owner size)), keeping those whose folder groups
    under ``anc_hash`` and is itself mixed — so a group id a client cached
    resolves with folder albums off or before the next rebuild.

    The member track ids are memoised in the snapshot (``cat.memo``; they die
    with it, so they are exactly as fresh as the entry itself): a group can
    span dozens of folders and 100k+ tracks (a 17k-song share walked 112k
    tracks, ~165 ms, on every getAlbum / getMusicDirectory / next-track
    lookup) — a hit only re-reads the listed tracks (deleted ones dropped)."""
    mkey = ("grp_tracks", anc_hash, okey)
    if cat is not None:
        hit = cat.memo.get(mkey)
        if hit is not None:
            get = store.get_track
            return [t for t in map(get, hit) if t is not None]
    e = cat.by_id.get(f"{FOLDER_PREFIX}{anc_hash}~g{okey}") if cat is not None else None
    out: list[dict] = []
    if e is not None and e.dir_hashes and okey == "0":
        # The owner-less share: the snapshot's own list of owner-less
        # album-less tracks, kept when in a member folder — a third of the
        # member-folder walk, which reads every other owner's tracks too.
        members = set(e.dir_hashes)
        get = store.get_track
        for tid in cat.unknown_loose:
            t = get(tid)
            if (t and t.get("dir_hash") in members and not (t.get("album") or "").strip()
                    and not owner_of(t)):
                out.append(t)
    elif e is not None and e.dir_hashes:
        for dh in e.dir_hashes:
            out.extend(_dir_album_less(store, dh, okey))
    else:
        mixed: dict[str, bool] = {}
        for tid in _owner_candidates(store, cat, okey):
            t = store.get_track(tid)
            if not t or (t.get("album") or "").strip():
                continue
            dh = t.get("dir_hash")
            if (not dh or bucket_group(store, dh) != anc_hash
                    or owner_key(owner_of(t).lower()) != okey or not _is_mixed(store, dh, mixed)):
                continue
            out.append(t)
    out.sort(key=_track_order)
    if cat is not None:
        cat.memo[mkey] = tuple(t["id"] for t in out)
    return out


def folder_entry_from_tracks(store, union: ArtistUnion, dir_hash: str,
                             tracks: list[dict], owner: str | None = None, *,
                             group: bool = False) -> AlbumEntry | None:
    """Build the same entry ``build_catalogue`` would, from the folder's own
    tracks — used to resolve ``fa:`` ids without the full pass.  ``owner``
    (an ``owner_key``; ``tracks`` already filtered to it): one owner's share
    of a mixed folder — or, with ``group``, of the letter-bucket group whose
    ancestor is ``dir_hash`` (``group_album_tracks``).  Without it, the WHOLE
    folder, credited to its dominant owner — what a bare ``fa:<dir_hash>``
    always meant (ids starred or cached by a client before the folder turned
    mixed keep resolving).  Song count / duration count every listed tune of
    a multi-tune file (``tune_count`` / ``tune_duration_sum``), and the
    sample is the ``sample_key`` pick — the catalogue's rules."""
    if not tracks:
        return None
    owners: Counter = Counter(owner_of(t).lower() for t in tracks)
    sample = max(tracks, key=sample_key)
    added = sample.get("added_at") or 0
    gs: set = set()
    for t in tracks:
        gs.update(t.get("genre") or ())
    e = AlbumEntry()
    e.kind = "folder"
    e.dir_hash = dir_hash
    if group and owner is not None:
        owner_l = owner_of(tracks[0]).lower()
        e.id = group_album_id(dir_hash, owner_l)
        e.dir_hashes = tuple(dict.fromkeys(t.get("dir_hash") for t in tracks if t.get("dir_hash")))
        name = group_name(store, dir_hash)
    elif owner is not None:
        owner_l = owner_of(tracks[0]).lower()
        e.id = folder_album_id(dir_hash, owner_l)
        name = folder_album_name(store, dir_hash)
    else:
        owner_l = dominant_owner(owners)
        e.id = FOLDER_PREFIX + dir_hash
        name = folder_album_name(store, dir_hash)
    e.artist_l = owner_l
    e.artist = union.display.get(owner_l) or (owner_of(sample) if owner_l else "") \
        or UNKNOWN_ARTIST
    e.artist_id = union.artist_id(owner_l)
    if owner is None and single_owner(owners, len(tracks)) is not None:
        name = owner_named(name, owner_l, e.artist)
    e.name = name
    cnt = 0
    dur = 0.0
    for t in tracks:
        cnt += tune_count(t)
        dur += tune_duration_sum(t)
    e.song_count = cnt
    e.duration = _finite_int(dur)     # same raw-sum-then-round as build_catalogue
    e.sample = sample
    e.year = normalise_year(sample.get("year")) or 0
    sg = sample.get("genre") or []
    e.genre = (sg[0] if sg else "") or ""
    e.genres_l = frozenset(g.strip().lower() for g in gs if isinstance(g, str) and g.strip())
    e.added = added if isinstance(added, (int, float)) else 0
    return e
