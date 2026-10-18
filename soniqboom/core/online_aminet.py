# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aminet's music section, online (aminet.net/mods).

An online collection (``core/online_catalog.py``) of PACKAGES: Aminet's own
``INDEX`` lists every upload (name, folder, rounded size, a one-line
description); the ``mods/`` rows are the index — the music categories, less
the sample and instrument folders.  An Aminet upload is mostly an LHA archive
holding one module or a set of them, so each archive is listed as ONE row
(titled by its name, the description as its comment, the category's genre or
composer) until the listener plays it or opens its Track Info: the archive is
fetched then, its playable members take its place in the library (the first
keeps its id) and its ``.readme`` gives their composer (``Author:``).  Loose
MP3 uploads are plain rows.

The category names (``mods/4mat`` — "Mods by 4mat") come from Aminet's
category page, read with the index.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from pathlib import Path

from soniqboom.core import online_catalog
from soniqboom.core.online_catalog import OnlineCatalogue, is_merged, user_agent

log = logging.getLogger(__name__)

SITE = "https://aminet.net"
BASE = f"{SITE}/mods"
INDEX_URL = f"{SITE}/INDEX"
TREE_URL = f"{SITE}/tree?path=mods"

# Not music: sound samples and instruments for trackers.
SKIP_CATEGORIES = frozenset({"smpl", "inst"})

# Aminet names MPEG audio ``.mpg`` (the Amiga players' convention; its
# ``mods/mpg`` category is "Music in MPEG audio format").  Only Aminet lists
# them — a local ``.mpg`` is video and never scanned.  Played transcoded until
# the first play's read tells the layer; MPEG-1/2 Layer III then plays as MP3.
MPEG_AUDIO_EXTS = frozenset({".mpg"})

# Categories that name a style (the rest name a composer, a format or a
# collection).
STYLE_GENRES = {
    "techn": "Techno", "hardc": "Hardcore", "house": "House", "jungl": "Jungle",
    "tranc": "Trance", "pop": "Pop", "rock": "Rock", "funk": "Funk", "piano": "Piano",
    "atmos": "Ambient", "chip": "Chiptune", "synth": "Synth",
}

_LINE_RE = re.compile(r"^(\S+)\s+mods/(\S+)\s+(\d+(?:\.\d+)?)([KMG]?)\s+(\d+)\s?(.*)$")
_CAT_RE = re.compile(r"\bmods/([a-z0-9]+)\s+-\s+(.+?)\s+\(\d+\s+packages?\)", re.I)
_BY_RE = re.compile(r"^(?:mods|modules|mp3s|songs|music)\s+by\s+(.+)$", re.I)
_OWN_RE = re.compile(r"^(.+?)'s\s+(?:mods|modules|music|songs)$", re.I)
_FROM_RE = re.compile(r"^(?:mods|modules|music|songs)\s+from\s+(.+)$", re.I)
_STARS_RE = re.compile(r"[\s(\[]*\*+[+-]?[)\]]?\s*$")
# "… by <composer>" — up to a spaced dash, a bracket, punctuation, a word
# that starts another phrase ("for Assembly"), a length / rating, or the end.
# (A hyphen or slash inside the name stays: "Jean-Michel", "Cyborg/Crazy".)
_DESC_BY_RE = re.compile(
    r"(?<!played )(?<!performed )\bby\s+(.+?)(?=\s+[-–]\s|\s*[(\[*,;:=]|[!?](?:\s|$)|\.\s|"
    r"\.{2}|\.$|\s(?:for|from|of|in|on|at|with|using|made|type|remix(?:ed)?|v\d)\b|"
    r"\s+\d+:\d\d|\s+-:--|\s*$)", re.I)
# "by Mr. Mortal", "by Dr. Awesome": an abbreviation's period ends no name —
# nor an initial's ("Michael J. Bertrand", "C.J. Miller").
_ABBREV_RE = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Jr|Sr|Prof|Mt)\.\s+|(?<![A-Za-z0-9])([A-Z])\.\s+(?=[A-Z])")
_COPYRIGHT_RE = re.compile(r"^(?:\(c\)|©)\s*", re.I)
_TRAILING_DATE_RE = re.compile(
    r"(?:\s+(?:\d{4,}|\d{1,4}(?:[/.-]\d{1,4})+|8[5-9]|9\d)"  # "Boray 970308", "… 96/12/30", "… 93"
    r"|\.(?:19|20)\d{2}|\s*['`´‘’]\d{2})$")                   # "CSM.1996", "racoon '95"
_MONTHS = frozenset({"jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
                     "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
                     "oct", "october", "nov", "november", "dec", "december"})
_CONJ_TAIL_RE = re.compile(r"\s+(?:and|with|feat\.?|vs\.?|&)$")   # "Hollywood and" (cut)
_AKA_RE = re.compile(r"^(.+?)\s+a\.?k\.?a\.?\s+(.+)$", re.I)
# "by Adelaide artist Orpheus" — a descriptor before the name.
_DESCRIPTOR_RE = re.compile(r"^(?:[\w-]+\s+)?(?:artist|composer|musician)\s+", re.I)
# "by Brown, Yannis 3:50" — Aminet's rated uploads write "Last, First" before
# a length; anything else after a comma is no first name ("by Salty, House").
_LAST_FIRST_RE = re.compile(r"^,\s+([A-Z][a-z][\w.'-]*)(?=\s+(?:\d{1,3}:\d\d|-:--))")
# A rated upload's length and rating code ("Module -:-- -", "… 0:30 C1++").
_RATING_TAIL_RE = re.compile(r"\s+(?:\d{1,3}:\d\d|-:--)(?:\s+(?:[A-Z]\d*[+-]*|-))?\s*$")
_NOT_NAMES = frozenset({"me", "myself", "the", "a", "an", "various", "various artists",
                        "unknown", "unknown artist", "anonymous", "request", "hand", "n/a", "na", "none",
                        "see readme", "see below", "?", "-", "nobody", "someone"})
_UNITS = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}


def _approx_bytes(num: str, unit: str) -> int:
    try:
        return int(float(num) * _UNITS.get(unit.upper(), 1))
    except ValueError:
        return 0


_CREATED_RE = re.compile(r"created on\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", re.I)
AGE_CAP = 999                       # the INDEX's age column stops here ("999 days or more")


def index_day(text: str) -> int | None:
    """The day (days since 1970-01-01) Aminet's INDEX was created on, from
    its header ("created on 14-Oct-2026")."""
    import calendar
    m = _CREATED_RE.search(text[:2048])
    if not m:
        return None
    try:
        return calendar.timegm(time.strptime(m.group(1), "%d-%b-%Y")) // 86400
    except ValueError:
        return None


def parse_index(text: str, playable_exts) -> list:
    """``(rel, extra)`` of every music upload of Aminet's ``INDEX`` — ``rel``
    under ``mods/`` (``techn/AXL-Dreams.lha``), ``extra`` its rounded size,
    description, category and upload day (``up``: the INDEX's day minus the
    upload's age; 0 when the age is capped).  Archives and playable files
    only."""
    from soniqboom.core import archive
    day = index_day(text)
    out = []
    for raw in text.splitlines():
        m = _LINE_RE.match(raw.rstrip())
        if not m:
            continue
        name, cat, num, unit, age, desc = m.groups()
        if "/" in cat or cat in SKIP_CATEGORIES:
            continue
        ext = os.path.splitext(name)[1].lower()
        if not (archive.is_archive_name(name) or ext in playable_exts or ext in MPEG_AUDIO_EXTS):
            continue
        extra = {"approx": _approx_bytes(num, unit), "desc": clean_desc(desc), "cat": cat}
        if day is not None:
            a = int(age)
            extra["up"] = day - a if a < AGE_CAP else 0
        out.append((f"{cat}/{name}", extra))
    return out


def comment_of(desc: str | None) -> str | None:
    """The comment a description gives: without a rated upload's length and
    rating code ("… 0:30 C1++"), none for a bare "Module"."""
    c = _RATING_TAIL_RE.sub("", desc or "").strip()
    return c if c and c.casefold() not in ("module", "mod") else None


def clean_desc(desc: str | None) -> str | None:
    """An Aminet description without its padding and trailing rating stars
    ("Pop mod by 4mat        ****+" → "Pop mod by 4mat")."""
    d = re.sub(r"\s+", " ", desc or "").strip()
    d = _STARS_RE.sub("", d).strip()
    return d or None


_QUOTED_SLASH_RE = re.compile(r'^[^"]*"[^"]+"\s*/\s*(.+?)\s*$')
_COUNT_NAME_RE = re.compile(
    r"^\d+\s+(?:(?:great|new|old|cool|classic|more)\s+)?"
    r"(?P<n>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+(?:modules|mods|tunes|songs|tracks)\b")
_OWNER_RE = re.compile(r"^(?P<n>[A-Z][\w-]*(?:\s+[A-Z][\w-]*)?)'s\s+.*\b(?:modules|mods|tunes|songs|music)\b",
                       re.I)
# Words that name a format, a style or a quality — never a composer
# ("14 ProTracker modules", "5 Techno mods").
_NOT_COMPOSER_WORDS = frozenset({
    "protracker", "noisetracker", "soundtracker", "startrekker", "fasttracker", "screamtracker",
    "impulsetracker", "octamed", "med", "futurecomposer", "future composer", "ahx", "hively",
    "digibooster", "oktalyzer", "symphonie", "mod", "mods", "module", "modules", "chip", "techno",
    "trance", "house", "dance", "pop", "rock", "amiga", "xm", "s3m", "it", "mp3", "midi",
    "fixed", "paula"})


def _a_name(name: str) -> str:
    name = (name or "").strip(" .'\"")
    if not name or name.casefold() in _NOT_NAMES or len(name.split()) > 4:
        return ""
    if re.fullmatch(r"\d[\d:.\s]*", name):
        return ""                                 # a length, a number ("by 0:15")
    if name.split()[0].casefold() in ("the", "a", "an", "my", "his", "her", "request"):
        return ""
    if name.casefold() in _NOT_COMPOSER_WORDS:
        return ""
    return name


def _edge_clean(name: str) -> str:
    """A name without the decoration around it ("* B.P.M.", "[mASh!]",
    "## Etrimon ##", "!RAMP!", "(c) JazzCat") — the closing bracket of
    "Name (Handle)" stays."""
    name = _COPYRIGHT_RE.sub("", (name or "").strip())
    name = re.sub(r"^[^\w(]+", "", name)
    name = re.sub(r"[^\w)]+$", "", name)
    if name.count("(") != name.count(")"):
        name = name.strip("()").strip()
    return name


def desc_artist(desc: str | None, *, collection: bool = False) -> tuple[str, str]:
    """``(composer, group)`` an upload's description names — "Techno mod by
    Skope - 4ch" → ("Skope", ""), "7 great tunes by Cyborg/Crazy" and
    "… by Tripper^Stx^Moods" → (composer, first group), '"Blue Water Pond" /
    A.Akerheden' → ("A.Akerheden", ""), "by Arash Tavakoli aka HyperionT" →
    ("Arash Tavakoli (HyperionT)", ""), "by Brown, Yannis 3:50" → ("Yannis
    Brown", "").  For a collection's upload also "14 Ron Klaren modules!" and
    "Baroque's Quota of Modules" (a count or an owner before the word
    modules).  ("", "") when it names none."""
    d = _ABBREV_RE.sub(lambda m: (m.group(1) or m.group(2)) + ".\x00", desc or "")
    name = ""
    # The last "by": "Caused by defect module by Estrayk / Caps".
    m = None
    for at in reversed([b.start() for b in re.finditer(r"\bby\s+", d, re.I)]):
        m = _DESC_BY_RE.match(d, at)
        if m:
            break
    if m:
        name = m.group(1)
        rest = d[m.end():]
        lf = _LAST_FIRST_RE.match(rest)
        if lf and " " not in name.strip():
            name = f"{lf.group(1)} {name.strip()}"
        elif name.strip().casefold() in ("me", "myself"):
            who = re.match(r"^,\s*([A-Z][\w.'^/-]*)", rest)       # "by me, Owlz"
            name = who.group(1) if who else name
    else:
        m = _QUOTED_SLASH_RE.match(d)
        if m:
            name = m.group(1)
        elif collection:
            m = _COUNT_NAME_RE.match(d) or _OWNER_RE.match(d)
            if m:
                name = m.group("n")
    name = _DESCRIPTOR_RE.sub("", (name or "").replace("\x00", " ").strip())
    if "\\" in name:
        return "", ""                                     # ASCII art ("/\/\ind \/\/alker")
    name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()   # "Don_Cato", "95/11/18____"
    if name.count('"') % 2:
        name = name.split('"', 1)[0]                     # 'Room 237 "PhoneTap…' (cut)
    name = _TRAILING_DATE_RE.sub("", _edge_clean(name))          # "Boray 970308", "Boray 96/12/30"
    name = _CONJ_TAIL_RE.sub("", name).strip()
    if "^" in name and "/" not in name:                          # "Tripper^Stx^Moods"
        first, _, rest = name.partition("^")
        name = f"{first}/{rest}"
    if "/" in name:                                       # "Chuck Biscuits /iCE/DiE/HRT",
        first, _, rest = name.partition("/")              # "Kanyar/Hutch Olive Oiled HardCore"
        grp = rest.split("/", 1)[0].split("^", 1)[0].strip()
        if len(grp.split()) > 3:
            grp = grp.split()[0]
        grp = _TRAILING_DATE_RE.sub("", grp).strip()
        if len(grp) < 2 or grp.casefold() in _MONTHS:
            grp = ""                                      # "ElbiE^t…" (cut), "/may 1994/"
        name = f"{first.strip()}/{grp}" if grp else first.strip()
    handle = ""
    aka = _AKA_RE.match(name)
    if aka:                                               # "Arash Tavakoli aka HyperionT"
        name, handle = aka.group(1).strip(), _edge_clean(aka.group(2))
    name = _a_name(_edge_clean(name))
    if not name:
        return "", ""
    who, group = clean_author(name)
    who = _a_name(who)
    if who and handle and handle.casefold() != who.casefold() and _a_name(handle):
        who = f"{who} ({handle})"
    return who, group


def category_is_collection(name: str | None) -> bool:
    """Does a category gather other people's uploads (a collection, a chart,
    a review series, a party) — its readme Authors then name whoever compiled
    or uploaded them, not the composers?"""
    return bool(re.search(r"\b(?:collections?|hits|rated|reviewed)\b|^(?:mods|modules)\s+from\b",
                          name or "", re.I))


def parse_categories(page: str) -> dict:
    """``category → its name`` from Aminet's category page
    (``4mat`` → "Mods by 4mat")."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text)
    return {m.group(1).lower(): m.group(2).strip() for m in _CAT_RE.finditer(text)}


def category_artist(name: str | None) -> str:
    """The composer a category is named after ("Mods by Blasa (Mauro R.)"
    → "Blasa", "Neurodancer's modules" → "Neurodancer"); "" for any other
    category."""
    n = (name or "").strip()
    m = _BY_RE.match(n) or _OWN_RE.match(n)
    if not m:
        return ""
    return re.sub(r"\s*\([^)]*\)\s*$", "", m.group(1)).strip()


_INDEX_DESC_CUT = 40                  # Aminet's INDEX cuts descriptions here (40–41 characters)


def _cut_at_end(desc: str | None, name: str) -> bool:
    """Does ``name`` end a description long enough to be Aminet's cut (the
    name may be cut too — "… by Case and" cut from "Case and Filippetto")?"""
    d = (desc or "").rstrip()
    if len(d) < _INDEX_DESC_CUT:
        return False
    key = (name or "").casefold()
    return d.casefold().endswith(key) or _CONJ_TAIL_RE.sub("", d).casefold().endswith(key)


def known_composers(rows) -> set:
    """The composers the descriptions of ``rows`` (``parse_index``) name in
    full — not where a description of Aminet's cut length ends in the name —
    one spelling each (the most frequent: "HKvalhe" over "Hkvalhe")."""
    import collections
    seen: dict = {}
    for _rel, ex in rows:
        d = ex.get("desc") or ""
        name, _group = desc_artist(d)
        if name and not _cut_at_end(d, name):
            seen.setdefault(name.casefold(), collections.Counter())[name] += 1
    return {c.most_common(1)[0][0] for c in seen.values()}


def known_groups(rows) -> set:
    """The groups the descriptions of ``rows`` name in full — not where a
    description of Aminet's cut length ends in the group — one spelling each."""
    import collections
    seen: dict = {}
    for _rel, ex in rows:
        d = ex.get("desc") or ""
        _name, group = desc_artist(d)
        if group and not _cut_at_end(d, group):
            seen.setdefault(group.casefold(), collections.Counter())[group] += 1
    return {c.most_common(1)[0][0] for c in seen.values()}


def complete_name(cut: str, names) -> str:
    """The known composer whose name a cut name begins ("HKval" →
    "HKvalhe", "Jogeir Lil" → "Jogeir Liljedahl" — the shortest when every
    other match begins with it); "" when none or unrelated ones do ("AiR":
    Airon or Airwalk)."""
    key = (cut or "").casefold()
    if not key:
        return ""
    hits = sorted((n for n in names if n.casefold().startswith(key) and len(n) > len(cut)),
                  key=len)
    if not hits:
        return ""
    first = hits[0].casefold()
    return hits[0] if all(h.casefold().startswith(first) for h in hits) else ""


def _named_in(person: str, *texts) -> bool:
    """Is ``person`` (or the name part of "Name (…)") named in any of
    ``texts`` — as whole words, or cut short at the end of a description of
    Aminet's cut length ("… by Darkne" names "Darkness")?"""
    key = re.sub(r"\s*\(.*$", "", person or "").strip().casefold()
    if not key:
        return False
    whole = re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)")
    for t in texts:
        low = (t or "").casefold().rstrip()
        if whole.search(low):
            return True
        if len(low) >= _INDEX_DESC_CUT:
            for k in range(len(key) - 1, 3, -1):
                if low.endswith(key[:k]) and (len(low) == k or not low[-k - 1].isalnum()):
                    return True
    return False


def _name_key(name: str | None) -> str:
    """A composer's name to compare spellings by: case, punctuation and a
    zero for an O aside ("V0yager" / "Voyager", "P.D.Spark" / "P.D. Spark")."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold().replace("0", "o"))


def category_party(name: str | None) -> str:
    """The party a category collects ("Mods from The Party 1996")."""
    m = _FROM_RE.match((name or "").strip())
    return m.group(1).strip() if m else ""


# Addresses in every form readmes use: user@host, <…>, "(user at host dot se)",
# web addresses.
_ADDRESS_RES = (
    re.compile(r"[(\[<][^()\[\]<>]*(?:@|\bat\b[^()\[\]<>]*\bdot\b|https?://|www\.)[^()\[\]<>]*[)\]>]",
               re.I),
    re.compile(r"\S+@\S+"),
    re.compile(r"\S+\s+at\s+\S+\s+dot\s+\S+", re.I),
    re.compile(r"(?:https?://|www\.)\S+", re.I),
    re.compile(r"<[^>]*>"),
)
_GROUPISH = re.compile(r"\b(?:team|group|crew|productions?|records|design|soft(?:ware)?)\b", re.I)


def clean_author(val: str) -> tuple[str, str]:
    """``(composer, group)`` of a readme's Author line — addresses and web
    addresses left out, placeholders ("Various", "Unknown", "me") none, a
    ``Name/Group`` or ``Name (… team)`` split."""
    v = val
    for rx in _ADDRESS_RES:
        v = rx.sub(" ", v)
    v = re.sub(r"\s+", " ", v)
    v = re.sub(r"\s*,\s*", ", ", v).strip(" ,;-:")
    if v.startswith("(") and v.endswith(")") and v.count("(") == 1:
        v = v[1:-1].strip()                     # "(Jogeir Liljedahl)" once the address went
    group = ""
    m = re.match(r"^(.+?)\s*\(([^()]+)\)$", v)
    if m:
        v = m.group(1).strip()
        if _GROUPISH.search(m.group(2)):
            group = m.group(2).strip()
    m = re.match(r"^([^/,]{2,}?)\s*/\s*([^/,]{2,})$", v)
    if m and len(m.group(1).split()) <= 3 and len(m.group(2).split()) <= 3:
        v, group = m.group(1).strip(), group or m.group(2).strip()
    if re.fullmatch(r"[\d\s/.'-]+", group or "") or (group or "").casefold() in _MONTHS:
        group = ""                              # "CSM/1996", "CSM/97": a year, no group
    if v.casefold() in _NOT_NAMES or not re.search(r"\w", v):
        return "", group
    return v[:120], group[:120]


def parse_readme(text: str) -> dict:
    """``author`` (+ ``group``) / ``short`` / ``version`` of an Aminet
    ``.readme`` header — addresses left out; the Uploader is never kept."""
    out: dict = {}
    for line in text.splitlines()[:40]:
        m = re.match(r"^(Short|Author|Version):\s*(.*)$", line.strip(), re.I)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key in out:
            continue
        if key == "author":
            val, group = clean_author(val)
            if group:
                out["group"] = group
        elif key == "short":
            val = clean_desc(val) or ""
        if val:
            out[key] = val[:200]
    return out


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


class AminetOnline(OnlineCatalogue):
    key = "aminet"
    label = "Aminet"
    root = BASE
    site_url = f"{SITE}/"
    default_interval = "weekly"
    min_rows = 10_000
    packaged = True
    index_rules = 2                 # 2: MPEG audio ``.mpg`` uploads, upload days
    build_rules = 3                 # 3: composer / group / comment rules of 2026-10-14
    index_fill_fields = ("scene_group", "scene_party")
    owned_fields = ("scene_group", "scene_party")
    description = "Aminet's music section — Amiga modules and module packs"
    how = ("Listed from Aminet's own index, one entry per upload; an upload is fetched when "
           "you play it or open its Track Info, and the tunes inside it take its place.")

    def __init__(self) -> None:
        super().__init__()
        self._cats: dict | None = None
        self._names: list | None = None
        self._groups: list | None = None
        self._spell: dict | None = None
        self._flags: bool = False

    # Index ------------------------------------------------------------------
    def fetch_index(self, workdir: Path, info: dict):
        import httpx
        from soniqboom.core import metadata
        dest = workdir / "INDEX"
        headers = {"User-Agent": user_agent(), "Accept-Encoding": "identity"}
        if info.get("etag"):
            headers["If-None-Match"] = info["etag"]
        if info.get("last_modified"):
            headers["If-Modified-Since"] = info["last_modified"]
        with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as c:
            got = _download_resumable(c, INDEX_URL, dest, headers)
            if got is None:
                return None
            new_info = {"etag": got.get("etag"), "last_modified": got.get("last-modified"),
                        "index_date": _http_date(got.get("last-modified"))}
            try:
                r = c.get(TREE_URL, headers={"User-Agent": user_agent()})
                r.raise_for_status()
                cats = parse_categories(r.text)
                if cats:
                    new_info["categories"] = json.dumps(cats, separators=(",", ":"))
            except Exception as exc:                    # noqa: BLE001
                log.info("Aminet: category names not read (%s)", exc)
                if info.get("categories"):
                    new_info["categories"] = info["categories"]
        text = dest.read_bytes().decode("latin-1")
        found = parse_index(text, metadata.SUPPORTED_EXTENSIONS)
        if len(found) < self.min_rows:
            raise ValueError(f"Aminet's INDEX lists only {len(found)} music uploads — implausible")
        # The composers the descriptions name in full: a name Aminet's cut
        # left short ("… by HKval") is completed from them.
        new_info["names"] = json.dumps(sorted(known_composers(found)), separators=(",", ":"))
        new_info["groups"] = json.dumps(sorted(known_groups(found)), separators=(",", ":"))
        # Size 0: the source asks the archive for a file's exact size (HEAD,
        # inside its play's window) — Aminet's INDEX rounds it.
        return [(rel, 0, extra) for rel, extra in found], new_info

    def _composer_spellings(self) -> dict:
        if self._spell is None:
            self._spell = {}
            for name in self._categories().values():
                a = category_artist(name)
                if a and _name_key(a):
                    self._spell[_name_key(a)] = a
        return self._spell

    def _known_groups(self) -> list:
        if self._groups is None:
            try:
                self._groups = json.loads(self.index_info().get("groups") or "[]")
            except ValueError:
                self._groups = []
        return self._groups

    def _known_names(self) -> list:
        if self._names is None:
            try:
                self._names = json.loads(self.index_info().get("names") or "[]")
            except ValueError:
                self._names = []
        return self._names

    def _categories(self) -> dict:
        if self._cats is None:
            try:
                self._cats = json.loads(self.index_info().get("categories") or "{}")
            except ValueError:
                self._cats = {}
        return self._cats

    def after_index(self) -> None:
        self._cats = None
        self._names = None
        self._groups = None
        self._spell = None

    def prepare_build(self) -> None:
        from soniqboom.core import genre_derive
        self._cats = None
        self._names = None
        self._groups = None
        self._spell = None
        self._flags = genre_derive.names_enabled()

    # Packages ---------------------------------------------------------------
    def package_marker(self, size: int, extra: dict | None) -> str:
        return str(int((extra or {}).get("approx") or size or 0))

    def seen_for(self, extra: dict | None, now: int) -> int:
        """An upload new to the index dates from its upload day — one listed
        for the first time though years old (a rules change, a category
        that came back) is no recent addition; 999+ days: old."""
        if not extra or "up" not in extra:
            return now
        up = int(extra.get("up") or 0)
        return 1 if not up else min(now, up * 86400)

    def row_changed(self, prev_size, prev_extra, size, extra) -> bool:
        """Another upload under the same name: another rounded size, or an
        upload day that moved — by more than a day (the age column is whole
        days counted from the INDEX's creation), or from "999+ days" to a
        recent one.  An upload aging past the cap is no change; rows of an
        index from before upload days were kept compare by size only."""
        pe, e = prev_extra or {}, extra or {}
        if int(pe.get("approx") or 0) != int(e.get("approx") or 0):
            return True
        if "up" not in pe or "up" not in e:
            return False
        was, now = int(pe.get("up") or 0), int(e.get("up") or 0)
        if not now:
            return False                                # aged past the cap
        if not was:
            return True                                 # "999+ days" → recent: re-uploaded
        return abs(now - was) > 1

    def loose_facts(self, rel: str) -> dict | None:
        """A loose upload's readme (an ``.mpg``, an ``.mp3``), like a package's."""
        return self._package_meta_safe(rel) or None

    def package_meta(self, rel: str, source) -> dict:
        readme = rel.rsplit(".", 1)[0] + ".readme"
        data = source.read_file("/" + readme, lane="scan")
        return parse_readme(data[:16384].decode("latin-1", "replace"))

    # Tracks -----------------------------------------------------------------
    def build_track(self, rel, size, extra, *, added_at, dir_hash, root_h) -> dict:
        from soniqboom.core import archive, genre_derive, metadata
        from soniqboom.core import modland_catalog as mc
        from soniqboom.core.store import share_values
        ex = extra or {}
        pkg_rel = rel.split("::", 1)[0]
        cat, _, pkg_name = pkg_rel.partition("/")
        member = ex.get("member")
        t: dict = {"id": self.row_id(rel, ex), "path": self.track_path(rel),
                   "added_at": int(added_at), "dir_hash": dir_hash, "scan_root_hash": root_h,
                   "is_duplicate_primary": True}
        fam = None
        if member:
            shown = re.split(r"[\\/]", str(member).rsplit("::", 1)[-1])[-1]
            t["title"] = metadata.title_from_filename(shown) or _stem(shown)
            t["format"] = mc.format_for(shown)
            fam = mc.family_genre(shown)
            t["file_size"] = int(size or 0)              # (0: not known before its play)
            if int(ex.get("members") or 1) > 1:
                t["album"] = _stem(pkg_name)
        elif archive.is_archive_name(pkg_name):
            t["title"] = _stem(pkg_name)
            t["format"] = "LHA package" if archive.is_lha_name(pkg_name) else "ZIP package"
            t["file_size"] = int(ex.get("approx") or size or 0)
        elif os.path.splitext(pkg_name)[1].lower() in MPEG_AUDIO_EXTS:
            t["title"] = _stem(pkg_name)
            t["format"] = "MPEG audio"                   # (its layer: read on its first play)
            t["file_size"] = int(ex.get("approx") or size or 0)
        else:
            t["title"] = _stem(pkg_name)
            t["format"] = mc.format_for(pkg_name)
            fam = mc.family_genre(pkg_name)
            t["file_size"] = int(ex.get("approx") or size or 0)
        pkg = ex.get("pkg") or {}
        comment = comment_of(pkg.get("short") or ex.get("desc"))   # (the readme's is not cut short)
        if comment:
            t["comment"] = comment
        cat_name = self._categories().get(cat)
        cat_artist = category_artist(cat_name)
        collection = category_is_collection(cat_name)
        # (The readme's short line first: the INDEX cuts descriptions at ~40.)
        d_src = pkg.get("short") or ex.get("desc")
        d_name, d_group = desc_artist(d_src, collection=collection)
        if not d_name and pkg.get("short") and ex.get("desc"):
            d_src = ex.get("desc")
            d_name, d_group = desc_artist(d_src, collection=collection)
        if d_name and d_src is ex.get("desc") and _cut_at_end(d_src, d_name):
            # "Melodic mod by AiR" — Aminet's cut, not the name: the category's
            # composer it begins ("Mods by Airon"), else no guess when short.
            whole = complete_name(d_name, self._known_names())
            if cat_artist and cat_artist.casefold().startswith(d_name.casefold()):
                d_name = cat_artist
            elif whole:
                d_name = whole
            elif len(d_name) < 4:
                d_name, d_group = "", ""
        if d_group and d_src is ex.get("desc") and _cut_at_end(d_src, d_group):
            # "… by Black Dragon/Honoo+L" — the group may be cut short: one
            # named in full elsewhere stands, else it is completed from those,
            # else kept only when not too short.
            known = self._known_groups()
            if not any(g.casefold() == d_group.casefold() for g in known):
                whole = complete_name(d_group, known)
                d_group = whole or (d_group if len(d_group) >= 4 else "")
        r_author = (pkg.get("author") or "").strip()
        if (collection and not category_party(cat_name) and int(ex.get("members") or 1) > 1
                and r_author and not _named_in(r_author, ex.get("desc"), pkg.get("short"))):
            # A collection's set names who compiled it (14RK_Mods: "14 Ron
            # Klaren modules!" — Author "Mikael Lund"): no composer of its
            # tunes.  (A party's upload is its composer's entry: "Mods from
            # The Party 1996" — Author "Jets/Beam".)
            r_author = ""
        # The readme's Author, else the composer the description names, else
        # the composer the category is named after (a label's category — "Mods
        # by Up Rough Soundsystem" — holds several composers' uploads).
        author = r_author or d_name
        if author and cat_artist and (
                _name_key(author) == _name_key(cat_artist)
                or (d_src is ex.get("desc") and author == d_name and _cut_at_end(d_src, d_name)
                    and _name_key(author).startswith(_name_key(cat_artist)))):
            # One spelling per composer in their own category ("V0yager" /
            # "Voyager", "P.D.Spark No…" cut short / "P.D. Spark").
            author = cat_artist
        elif author:
            # A composer with a category of their own: its spelling everywhere
            # ("blakkhar" in a style category → "Blakkhar").
            author = self._composer_spellings().get(_name_key(author), author)
        artist = author or cat_artist
        if artist:
            t["artist"] = artist
        group = pkg.get("group") or (d_group if not r_author else "")
        if group:
            t["scene_group"] = group
        party = category_party(cat_name)
        if party:
            t["scene_party"] = party
        genres = list(fam or [])
        style = STYLE_GENRES.get(cat)
        if style and style not in genres:
            genres.append(style)
        if genres:
            t["genre"] = genres
        g = genre_derive.derive(t, names=self._flags, roots=(self.root,))
        if g:
            t["genre_derived"] = g
        return share_values(t)

    def changed(self, old: dict, rel: str, size: int, extra) -> bool:
        ex = extra or {}
        if ex.get("member") or is_merged(old):
            return False                 # a member's row follows its package's marker
        return ((old.get("file_size") or 0) != int(ex.get("approx") or 0)
                or (old.get("comment") or None) != comment_of(ex.get("desc")))

    def merge_view(self, rel: str, name: str, local) -> tuple[str, dict]:
        """An MPEG audio ``.mpg`` is read as the ``.mp3`` it is, and keeps
        the format its first frame names (``MP3`` plays as is; MP2 / MP1 are
        transcoded)."""
        if os.path.splitext(name)[1].lower() not in MPEG_AUDIO_EXTS:
            return name, {}
        layer = mpeg_audio_layer(local)
        stem = os.path.splitext(name)[0]
        if layer is None:
            return f"{stem}.mp3", {"format": "MPEG audio"}
        return f"{stem}.mp3", {"format": {3: "MP3", 2: "MP2", 1: "MP1"}[layer]}

    def merged_fields(self, d: dict, old: dict, rel: str) -> dict:
        from soniqboom.core import genre_derive
        for f in ("comment", "album", "scene_group", "scene_party"):
            if old.get(f) and not d.get(f):
                d[f] = old[f]
        # The category's style, unless the file names a genre of its own.
        style = [g for g in (old.get("genre") or ()) if g in STYLE_GENRES.values()]
        if style and not genre_derive.has_real_genre(d):
            cur = list(d.get("genre") or ())
            d["genre"] = cur + [g for g in style if g not in cur]
        return d


def mpeg_audio_layer(path) -> int | None:
    """The layer (1–3) of the first MPEG audio frame of ``path`` (after an
    ID3v2 tag), None when it holds none."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    off = 0
    if head[:3] == b"ID3" and len(head) >= 10:
        off = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14
                    | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
        if off + 4 > len(head):
            try:
                with open(path, "rb") as fh:
                    fh.seek(off)
                    head = b"\0" * off + fh.read(64 * 1024)
            except OSError:
                return None
    for i in range(off, len(head) - 3):
        b1, b2 = head[i + 1], head[i + 2]
        if head[i] != 0xFF or (b1 & 0xE0) != 0xE0:
            continue
        version, layer_bits = (b1 >> 3) & 3, (b1 >> 1) & 3
        if version == 1 or layer_bits == 0 or (b2 >> 4) in (0, 15) or (b2 >> 2) & 3 == 3:
            continue
        return 4 - layer_bits
    return None


def _http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def _download_resumable(client, url: str, dest: Path, headers: dict, *,
                        attempts: int = 6) -> dict | None:
    """GET ``url`` into ``dest``, resuming where a dropped transfer stopped
    (Aminet's server closes long transfers part-way).  Returns the response
    headers, or None for a 304 (unchanged).  Blocking."""
    import httpx
    got = 0
    expected = None
    validator = None
    first_headers: dict | None = None
    stalled = 0                      # attempts in a row that moved no byte
    with open(dest, "wb") as fh:
        for attempt in range(attempts):
            before = got
            h = dict(headers)
            if got:
                h.pop("If-None-Match", None)
                h.pop("If-Modified-Since", None)
                h["Range"] = f"bytes={got}-"
                if validator:
                    h["If-Range"] = validator
            try:
                with client.stream("GET", url, headers=h) as r:
                    if r.status_code == 304 and not got:
                        return None
                    if got and r.status_code == 416:
                        return first_headers or {}   # (it broke after its last byte)
                    if got and r.status_code == 200:
                        fh.seek(0)               # the file changed: start over
                        fh.truncate()
                        got = 0
                    elif got and r.status_code != 206:
                        r.raise_for_status()
                    elif not got:
                        r.raise_for_status()
                    if first_headers is None or r.status_code == 200:
                        first_headers = {k.lower(): v for k, v in r.headers.items()}
                        validator = r.headers.get("etag") or r.headers.get("last-modified")
                        try:
                            expected = int(r.headers.get("content-length") or 0) or None
                        except ValueError:
                            expected = None
                    for chunk in r.iter_bytes(256 * 1024):
                        fh.write(chunk)
                        got += len(chunk)
                if expected is None or got >= expected:
                    return first_headers or {}
            except (httpx.TransportError, httpx.RemoteProtocolError) as exc:
                log.info("Aminet INDEX transfer broke at %d bytes (%s) — resuming", got, exc)
                if attempt == attempts - 1:
                    raise
                # Resumed after a pause; two attempts in a row that moved
                # nothing (no connection, nothing sent) end it — the next
                # refresh tries again (``OnlineCatalogue._retry_due``).
                stalled = stalled + 1 if got == before else 0
                if stalled >= 2:
                    raise
                time.sleep(min(30.0, 2.0 * (attempt + 1)))
    raise ConnectionError(f"Aminet INDEX incomplete ({got} of {expected} bytes)")


AMINET_ONLINE = online_catalog.register(AminetOnline())
