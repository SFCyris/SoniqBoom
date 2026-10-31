# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Values of the search box's ``length:``, ``added:`` / ``played:`` and
``format:`` operators — shared by the parser (``api/search.py``, which keeps
a value it can't read as free text) and the store's predicate resolver
(``TrackStore._candidate_ids``).

* ``length:`` — a length as ``90``, ``90s``, ``5m``, ``1h``, ``1:30`` or
  ``1:02:03``, alone or as ``>N`` ``<N`` ``>=N`` ``<=N`` ``A-B`` ``A-`` ``-B``;
  :func:`length_value` rewrites it in whole seconds (``>5:00`` → ``>300``) for
  the numeric range syntax ``year:`` uses.
* ``added:`` / ``played:`` — a time spec: relative (``<30d`` = within the last
  30 days, ``>2w`` = longer ago than 2 weeks, ``30d`` = ``<30d``; units ``h``
  hours, ``d`` days, ``w`` weeks, ``m`` months of 30 days, ``y`` years of 365
  days) or a calendar period (``2026``, ``2026-10``, ``2026-10-23``) alone, as
  a range (``2026-01-2026-06``, ``2026-`` , ``-2025``) or with ``>`` ``<``
  ``>=`` ``<=``.  :func:`when_value` gives its canonical spelling (what the
  predicate carries), :func:`when_range` the epoch-second bounds it stands
  for at a moment — the store resolves a relative spec when it runs the
  search, so the same spec keys every cache.  Calendar periods are read in
  the server's local time.
* ``format:`` — :func:`format_names_for_ext` gives the format names files
  with that extension are given (``metadata.FORMAT_NAMES`` plus the names an
  extract picks by content); :data:`FAMILIES` maps the family words to
  ``retro.coarse_family`` buckets.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# ── length: ──────────────────────────────────────────────────────────────────

# (At most 9 digits a number: a longer one is no length — and no int() of
# thousands of digits.)
_DUR = r'(?:\d{1,9}:\d{1,2}(?::\d{1,2})?|\d{1,9}[smh]?)'
_LEN_OP_RE = re.compile(r'(<=|>=|<|>)?(' + _DUR + r')')
_LEN_RANGE_RE = re.compile(r'(' + _DUR + r')?-(' + _DUR + r')?')


def _seconds(token: str) -> int | None:
    """``90`` / ``90s`` / ``5m`` / ``1h`` / ``1:30`` / ``1:02:03`` in whole
    seconds; None when a ``:`` part is 60 or more (``1:75``)."""
    if ":" in token:
        parts = [int(p) for p in token.split(":")]
        if any(p >= 60 for p in parts[1:]):
            return None
        total = 0
        for p in parts:
            total = total * 60 + p
        return total
    unit = token[-1]
    if unit.isdigit():
        return int(token)
    n = int(token[:-1])
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def length_value(value: str) -> str | None:
    """A ``length:`` value as the numeric range text ``api.search._range_tag``
    reads, in whole seconds (``5:00`` → ``300``, ``>5m`` → ``>300``,
    ``3:00-4:00`` → ``180-240``), or None when it is no length or range
    (the parser then keeps it as free text)."""
    v = (value or "").replace(" ", "").lower()
    if not v:
        return None
    m = _LEN_OP_RE.fullmatch(v)
    if m:
        n = _seconds(m.group(2))
        return None if n is None else f"{m.group(1) or ''}{n}"
    m = _LEN_RANGE_RE.fullmatch(v)
    if m and (m.group(1) or m.group(2)):
        lo = _seconds(m.group(1)) if m.group(1) else None
        hi = _seconds(m.group(2)) if m.group(2) else None
        if (m.group(1) and lo is None) or (m.group(2) and hi is None):
            return None
        return f"{'' if lo is None else lo}-{'' if hi is None else hi}"
    return None


# ── added: / played: ─────────────────────────────────────────────────────────

_UNIT_S = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
_REL_RE = re.compile(r'(<=|>=|<|>)?(\d{1,9})([hdwmy])')
_DATE = r'\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?'
_ABS_RE = re.compile(r'(<=|>=|<|>)?(' + _DATE + r')')
_ABS_RANGE_RE = re.compile(r'(' + _DATE + r')?-(' + _DATE + r')?')
# Years a calendar period may name (its end, the next year, must still be a
# date ``datetime`` can turn into a timestamp on every platform).
_MIN_YEAR, _MAX_YEAR = 1900, 9998


def _period(text: str) -> tuple[float, float] | None:
    """The calendar period ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD`` as epoch
    seconds ``[start, end)`` in local time, or None for no such date."""
    parts = [int(p) for p in text.split("-")]
    y = parts[0]
    if not _MIN_YEAR <= y <= _MAX_YEAR:
        return None
    try:
        if len(parts) == 1:
            start, end = datetime(y, 1, 1), datetime(y + 1, 1, 1)
        elif len(parts) == 2:
            mo = parts[1]
            start = datetime(y, mo, 1)
            end = datetime(y + (mo == 12), mo % 12 + 1, 1)
        else:
            start = datetime(y, parts[1], parts[2])
            end = start + timedelta(days=1)             # (wall clock: DST-safe)
        return start.timestamp(), end.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def when_range(spec: str, now: float) -> tuple[float | None, float | None] | None:
    """The epoch-second bounds ``(lo, hi)`` — ``lo <= t < hi``, None: open at
    that end — of an ``added:`` / ``played:`` spec at the moment ``now``, or
    None when it is no time spec.

    Relative: ``<N`` / ``<=N`` / ``N`` = within the last N units
    (``[now − N, open)``), ``>N`` / ``>=N`` = longer ago (``[open, now − N)``).
    A period P (local time): P itself, ``>P`` after it, ``>=P`` from its
    start, ``<P`` before it, ``<=P`` up to its end, ``A-B`` from A's start to
    B's end, ``A-`` from A's start, ``-B`` up to B's end."""
    v = (spec or "").replace(" ", "").lower()
    if not v:
        return None
    m = _REL_RE.fullmatch(v)
    if m:
        cut = now - int(m.group(2)) * _UNIT_S[m.group(3)]
        return (None, cut) if (m.group(1) or "").startswith(">") else (cut, None)
    m = _ABS_RE.fullmatch(v)
    if m:
        p = _period(m.group(2))
        if p is None:
            return None
        op = m.group(1) or ""
        if op == ">":
            return p[1], None
        if op == ">=":
            return p[0], None
        if op == "<":
            return None, p[0]
        if op == "<=":
            return None, p[1]
        return p
    m = _ABS_RANGE_RE.fullmatch(v)
    if m and (m.group(1) or m.group(2)):
        a = _period(m.group(1)) if m.group(1) else None
        b = _period(m.group(2)) if m.group(2) else None
        if (m.group(1) and a is None) or (m.group(2) and b is None):
            return None
        return (a[0] if a else None), (b[1] if b else None)
    return None


def when_value(value: str) -> str | None:
    """The canonical spelling of an ``added:`` / ``played:`` value (spaces
    dropped, lower case) — or None when it is no time spec (the parser then
    keeps it as free text)."""
    v = (value or "").replace(" ", "").lower()
    return v if when_range(v, 0.0) is not None else None


def is_relative(spec) -> bool:
    """Does the ``added:`` / ``played:`` spec count back from now (``<30d``)?"""
    return isinstance(spec, str) and _REL_RE.fullmatch(spec.replace(" ", "").lower()) is not None


# ── format: ──────────────────────────────────────────────────────────────────

# Family words ``format:`` takes → the ``retro.coarse_family`` bucket (the
# library Galaxy's family filter).
FAMILIES = {
    "tracker": "trackers", "trackers": "trackers",
    "chiptune": "chiptune", "chiptunes": "chiptune",
    "lossless": "lossless", "lossy": "lossy", "other": "other",
}

# Format names an extract picks by content for files of an extension, next
# to ``metadata.FORMAT_NAMES``'s: MP4 audio is labelled by its codec, a
# Dreamcast ``.dsf`` and an id / Apogee ``.imf`` by their magic.  (The DSD
# quality tiers — DSD64, DSD128, … — follow "DSD": ``TrackStore._format_ids``.)
_EXT_EXTRA = {
    "m4a": ("AAC", "ALAC"),
    "dsf": ("DSF (Dreamcast)",),
    "imf": ("AdLib IMF",),
}


def format_names_for_ext(word: str) -> set[str] | None:
    """The lower-cased format names a file with extension ``word`` (no dot,
    any case) is given, or None when no supported extension is ``word``."""
    from soniqboom.core.metadata import FORMAT_NAMES
    ext = (word or "").strip().lower().lstrip(".")
    if not ext:
        return None
    name = FORMAT_NAMES.get("." + ext)
    extra = _EXT_EXTRA.get(ext, ())
    if name is None and not extra:
        return None
    names = {n.lower() for n in extra}
    if name:
        names.add(name.lower())
    return names


# ── several values: ``key:a,b`` ──────────────────────────────────────────────

def split_values(value: str) -> list[str]:
    """The items of an operator value: split on ``,`` outside double quotes,
    each item with its quotes removed and its ends stripped; empty items are
    left out (``flac,`` → ``["flac"]``, ``metal,"hard rock"`` → ``["metal",
    "hard rock"]``, ``"Rock, Pop"`` → ``["Rock, Pop"]``)."""
    items: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in value or "":
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [s for s in (i.strip() for i in items) if s]


# ── numbers with a unit: ``samplerate:`` / ``bitrate:`` ──────────────────────

# Units ``number_value`` reads → multiplier.
RATE_UNITS = {"": 1, "hz": 1, "k": 1000, "khz": 1000}
KBPS_UNITS = {"": 1, "k": 1, "kbps": 1}
_NUM_UNIT_RE = r'(\d{1,9}(?:\.\d{1,3})?)([a-z]*)'
_NUM_OP_RE = re.compile(r'(<=|>=|<|>)?' + _NUM_UNIT_RE)
_NUM_RANGE_RE = re.compile(r'(?:' + _NUM_UNIT_RE + r')?-(?:' + _NUM_UNIT_RE + r')?')


def _unit_number(num: str, unit: str, units: dict, small_unit: str | None = None) -> int | None:
    """``num`` (digits, maybe a decimal part) times ``units[unit]`` as an int,
    or None for an unknown unit or a result that is no whole number
    (``small_unit``: the unit of a number under 1000 written without one)."""
    if not unit and small_unit is not None and float(num) < 1000:
        unit = small_unit
    mult = units.get(unit)
    if mult is None:
        return None
    if "." in num:
        whole, frac = num.split(".")
        scaled = int(whole + frac) * mult
        div = 10 ** len(frac)
        if scaled % div:
            return None
        return scaled // div
    return int(num) * mult


def number_value(value: str, units: dict, small_unit: str | None = None) -> str | None:
    """A numeric operator value whose numbers may carry a unit of ``units``
    (``RATE_UNITS``: ``44.1k`` → 44100; ``KBPS_UNITS``: ``320k`` → 320) as the
    range text ``api.search._range_tag`` reads (``>48k`` → ``>48000``,
    ``44.1k-96k`` → ``44100-96000``) — or None when it is no number or range
    (the parser then keeps it as free text).  ``small_unit``: the unit a
    number under 1000 written without one is in (``samplerate:44.1`` =
    44.1 kHz)."""
    v = (value or "").replace(" ", "").lower()
    if not v:
        return None
    m = _NUM_OP_RE.fullmatch(v)
    if m:
        n = _unit_number(m.group(2), m.group(3), units, small_unit)
        return None if n is None else f"{m.group(1) or ''}{n}"
    m = _NUM_RANGE_RE.fullmatch(v)
    if m and (m.group(1) or m.group(3)):
        lo = _unit_number(m.group(1), m.group(2), units, small_unit) if m.group(1) else None
        hi = _unit_number(m.group(3), m.group(4), units, small_unit) if m.group(3) else None
        if (m.group(1) and lo is None) or (m.group(3) and hi is None):
            return None
        return f"{'' if lo is None else lo}-{'' if hi is None else hi}"
    return None


# ── words of fixed meaning ───────────────────────────────────────────────────

# ``status:`` words → the predicate's value.  ``cantplay``: what "Tracks
# that can't be played" covers (``skip_rules.cant_play``).
STATUSES = {
    "broken": "broken", "glitched": "glitched", "corrupt": "corrupt",
    "partial": "partial", "unplayable": "unplayable", "empty": "empty",
    "cantplay": "cantplay",
}

# ``source:`` words → the predicate's value: an online collection's key
# (``catalogue.key_of``), a network-share protocol, ``share`` / ``collection``
# (any of either), ``local``.
SOURCES = {
    "hvsc": "hvsc", "aminet": "aminet", "sceneorg": "sceneorg", "scene.org": "sceneorg",
    "modland": "modland", "local": "local", "ftp": "ftp", "smb": "smb",
    "webdav": "webdav", "share": "share", "collection": "collection",
}
COLLECTION_SOURCES = frozenset(("hvsc", "aminet", "sceneorg", "modland"))
SHARE_SOURCES = frozenset(("ftp", "smb", "webdav"))

# ``chip:`` words (a SID tune's model, or its number of chips) and ``has:``
# words → the predicate's value.
CHIPS = {"6581": "6581", "8580": "8580", "2sid": "2sid", "3sid": "3sid"}
HAS = {"stil": "stil", "game": "game"}

_YES = {"yes": "yes", "y": "yes", "true": "yes", "1": "yes",
        "no": "no", "n": "no", "false": "no", "0": "no"}


def yes_no(value: str) -> str | None:
    """``yes`` / ``no`` for a ``lossless:`` value (``yes``/``no``, ``y``/``n``,
    ``true``/``false``, ``1``/``0``, any case), else None."""
    return _YES.get((value or "").strip().lower())


def word_of(value: str, words: dict) -> str | None:
    """The predicate value of ``value`` (any case, spaces ignored) in
    ``words`` (``STATUSES`` / ``SOURCES`` / ``CHIPS`` / ``HAS``), else None."""
    return words.get((value or "").replace(" ", "").lower())


# ── path: / folder: ──────────────────────────────────────────────────────────

def _strip_userinfo(root: str) -> str:
    """``root`` (a remote root after ``scheme://``) without its
    ``user:password@``: the last ``@`` whose text before it is a user name —
    no ``/`` in it, or a ``user:password`` whose password may hold ``/`` and
    ``@`` (a ``host:port/…`` is no password)."""
    at = root.rfind("@")
    while at != -1:
        before = root[:at]
        slash = before.find("/")
        colon = before.find(":")
        if slash == -1 or (colon != -1 and colon < slash
                           and not before[colon + 1:slash].isdigit()):
            return root[at + 1:]
        at = root.rfind("@", 0, at)
    return root


def path_texts(path, fold) -> tuple[str, str]:
    """``(folder_text, path_text)`` of a track path, as ``folder:`` and
    ``path:`` read it.  A remote path (``scheme://…``) is read as its
    scheme, its host (an IPv6 literal and a port too) and the rest; the
    ``user:password@`` before the host and, for a web address (http /
    https / webdav), a ``?query`` / ``#fragment`` in the share's root are
    left out (never indexed, never matched); the root's ``:/`` boundary
    and an archive member's ``::`` read as ``/``.  Then ``fold`` (lower
    case, accents folded, runs of spaces as one — ``store._game_key``).
    ``path_text`` is the whole path (a URL's scheme and host included);
    ``folder_text`` the folders only — no scheme or host, no file name."""
    s = str(path or "")
    if not s:
        return "", ""
    head = ""
    if "://" in s:
        scheme, rest = s.split("://", 1)
        b = rest.find(":/")                     # (the share root's end)
        root, rel = (rest[:b], rest[b + 1:]) if b != -1 else (rest, "")
        root = _strip_userinfo(root)
        if scheme.lower() in ("http", "https", "webdav", "webdavs"):
            root = re.split(r"[?#]", root, maxsplit=1)[0]
        cut = root.find("/")
        host, root_path = (root[:cut], root[cut:]) if cut != -1 else (root, "")
        head = fold(f"{scheme}://{host}")
        s = root_path + rel
    body = fold(s.replace("::", "/"))
    cut = body.rfind("/")
    return (body[:cut] if cut > 0 else ""), head + body
