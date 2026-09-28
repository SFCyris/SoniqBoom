"""Game titles from preservation DAT files (TOSEC, No-Intro, Redump).

Each set catalogues dumps of software per system in Logiqx XML DATs; the
``<game name="…">`` of a games DAT carries the title plus the set's naming
conventions — TOSEC ``Title v1.1 (1989)(Publisher)(Disk 1 of 2)[cr X][a]``,
No-Intro / Redump ``Title (USA) (Rev 1)`` — which are cut off here.  Only
games are kept: TOSEC's Games trees (not Demos, Applications, Compilations …),
No-Intro and Redump entries whose ``<category>`` is Games / Preproduction
(No-Intro leaves many entries uncategorised — those are kept; Redump
categorises all).

``titles_from_*`` return ``{platform: {title, …}}`` with the platform codes of
``core.game_titles`` (a title also under its aliases: the article moved to the
front — "Last Ninja, The" → "The Last Ninja" —, the parts of a multi-game
"A + B" / "A ~ B" entry, machine tags and inner versions removed).
"""
from __future__ import annotations

import collections
import html
import os
import re
import zipfile

_GAME_OPEN_RE = re.compile(r"<game\s")
_GAME_NAME_RE = re.compile(r'name="([^"]*)"')
# Limits on a downloaded zip (the real TOSEC pack: 11,193 DATs, 417 MB
# uncompressed, the largest 19 MB; the largest Redump DAT 46 MB): a DAT larger
# than MAX_DAT_BYTES is skipped, a zip over MAX_ZIP_BYTES in all or with more
# than MAX_ZIP_MEMBERS members is refused.
MAX_DAT_BYTES = 64 * 1024 * 1024
MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_MEMBERS = 50_000
# A game name longer than this is no title (the longest real TOSEC name: 190
# characters) — skipped before any pattern sees it.
MAX_NAME = 300
CAT_RE = re.compile(r"<category>([^<]*)</category>")


def iter_games(data: str):
    """``(name, body)`` of every ``<game name="…"> … </game>`` entry of a
    Logiqx DAT, in linear time whatever the input (each entry is the text up
    to the next ``<game`` tag, cut at its ``</game>``; a malformed or hostile
    file can't make the scan quadratic — the regex engine holds the GIL).
    ``name`` is the raw attribute value (HTML entities still escaped)."""
    for chunk in _GAME_OPEN_RE.split(data)[1:]:
        head, _sep, _rest = chunk.partition(">")
        m = _GAME_NAME_RE.search(head)
        if m is None:
            continue
        body = chunk[len(head) + 1:]
        end = body.find("</game>")
        yield m.group(1), (body if end < 0 else body[:end])

ARTICLES = r"The|A|An|Die|Der|Das|Le|La|Les|L'|El|Il|Lo|Los|Las|De|Het|Een"
ART_RE = re.compile(r"^(?P<main>.+?), (?P<art>" + ARTICLES + r")(?P<rest>(?: - .*)?)$")
VERSION_RE = re.compile(r"\s+(?:[vV]\d[\w.\-]*|[Rr]ev\s+[\w.]+|[Rr]ev\d[\w.]*|r\d+[\w.]*)$")
NIN1_RE = re.compile(r"^\d+\s*in\s*1\s*-\s*", re.I)
MACHINE_TAG_RE = re.compile(r"\s+(?:CD32|CDTV|AGA|ECS|OCS)\b")
INNER_VERSION_RE = re.compile(r"\s+[vV]\d[\w.]*(?=\s|$)")

# ── title cleaning ────────────────────────────────────────────────────────────


def with_article_aliases(t: str) -> list[str]:
    """'Last Ninja, The - Remix' → ['Last Ninja, The - Remix', 'The Last Ninja - Remix']."""
    out = [t]
    m = ART_RE.match(t)
    if m:
        art = m.group("art")
        sep = "" if art.endswith("'") else " "
        out.append(f"{art}{sep}{m.group('main')}{m.group('rest')}")
    return out


def tosec_title(name: str) -> str:
    """TOSEC 'Title v1.1 (demo) (1989)(Publisher)(Disk 1 of 2)[cr X][a]' → 'Title'."""
    t = re.split(r"\s*[\(\[]", " ".join(name[:MAX_NAME].split()), maxsplit=1)[0].strip()
    prev = None
    while prev != t:                      # 'Bundesliga Manager v1.3 rev1' → 'Bundesliga Manager'
        prev, t = t, VERSION_RE.sub("", t).strip()
    return t


def nointro_title(name: str) -> str:
    """No-Intro / Redump 'Title (USA) (Rev 1)' → 'Title' (also cuts '[b]' etc.)."""
    return re.split(r"\s+[\(\[]", " ".join(name[:MAX_NAME].split()), maxsplit=1)[0].strip()


def split_multi(t: str) -> list[str]:
    """'A + B' / 'A ~ B' multi-game or alternative-title names → [whole, A, B]."""
    out = [t]
    for sep in (" + ", " ~ "):
        if sep in t:
            parts = [NIN1_RE.sub("", p).strip() for p in t.split(sep)]
            out += [p for p in parts if len(p) >= 3]
    return out


def emit(store: dict, plat: str, title: str) -> None:
    """Add ``title`` and its aliases to ``store[plat]`` (a title longer than
    ``MAX_NAME`` is none)."""
    title = " ".join(title.split())
    if not title or len(title) > MAX_NAME:
        return
    variants = split_multi(title)
    for t in list(variants):
        s = INNER_VERSION_RE.sub("", MACHINE_TAG_RE.sub("", t)).strip()
        if s and s != t:
            variants.append(s)
    for t in variants:
        for a in with_article_aliases(t):
            a = a.strip(" -")
            if a and len(a) >= 2:
                store[plat].add(a)


# ── TOSEC ─────────────────────────────────────────────────────────────────────

TOSEC_SYS = {
    "Commodore C64": "c64",
    "Commodore Amiga": "amiga", "Commodore Amiga CD32": "amiga", "Commodore Amiga CDTV": "amiga",
    "Atari ST": "atarist",
    "IBM PC Compatibles": "dos",
    "Nintendo Famicom & Entertainment System": "nes", "Nintendo Famicom Disk System": "nes",
    "Nintendo Super Famicom & Super Entertainment System": "snes", "Nintendo Sufami Turbo": "snes",
    "Nintendo Game Boy": "gb", "Nintendo Game Boy Color": "gb",
    "Nintendo Game Boy Advance": "gba",
    "Nintendo 64": "n64", "Nintendo 64DD": "n64",
    "Nintendo DS": "nds",
    "Sony PlayStation": "psx", "Sony PlayStation 2": "ps2",
    "Sega Saturn": "saturn", "Sega Dreamcast": "dreamcast",
    "Sega Mega Drive & Genesis": "megadrive", "Sega Mega-CD & Sega CD": "megadrive", "Sega 32X": "megadrive",
    "Sega Mark III & Master System": "sms", "Sega Game Gear": "gamegear",
    "NEC PC-Engine & TurboGrafx-16": "pce", "NEC PC-Engine CD & TurboGrafx-16 CD": "pce",
    "NEC SuperGrafx": "pce",
    "MSX MSX": "msx", "MSX MSX2": "msx", "MSX MSX2+": "msx", "MSX TurboR": "msx",
    "Sinclair ZX Spectrum": "zx",
    "Amstrad CPC": "cpc", "Amstrad GX4000": "cpc",
    "Atari 8bit": "atari8",
    "Sharp X68000": "x68000",
    "SNK Neo-Geo": "neogeo", "SNK Neo-Geo CD": "neogeo",
    "Sega NAOMI": "arcade", "Sega NAOMI 2": "arcade", "Sega Chihiro": "arcade",
    "Namco-Sega-Nintendo Triforce": "arcade",
}
_TOSEC_EXCLUDE_GAMES_SUB = ("Save Disks", "Addons & Patches", "Unofficial Addons & Patches")


def tosec_is_games(typ: str) -> bool:
    """``typ`` = a DAT's name without its system and ``[format]`` parts, e.g.
    'Games - Adventure', 'CD - Games', 'Homebrew - Games'."""
    parts = typ.split(" - ")
    if "Compilations" in parts or "Collections" in parts or "Games" not in parts:
        return False
    sub = " - ".join(parts[parts.index("Games") + 1:])
    return not any(x in sub for x in _TOSEC_EXCLUDE_GAMES_SUB)


def _members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The members of a downloaded zip; ValueError when it is larger than any
    real list (``MAX_ZIP_BYTES`` / ``MAX_ZIP_MEMBERS``)."""
    infos = z.infolist()
    if len(infos) > MAX_ZIP_MEMBERS or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
        raise ValueError("the list archive is larger than any real one")
    return infos


def _name(raw: str) -> str | None:
    """A DAT entry's name: entities decoded, whitespace runs collapsed; None
    when empty or longer than ``MAX_NAME`` (the title patterns below then
    never see a hostile run of spaces or brackets)."""
    name = " ".join(html.unescape(raw).split())
    return name if 0 < len(name) <= MAX_NAME else None


def _read_dat(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> str | None:
    """A DAT member's text, None when it is larger than ``MAX_DAT_BYTES``
    (``zipfile`` reads no more than the size a member declares)."""
    if info.file_size > MAX_DAT_BYTES:
        return None
    return z.read(info).decode("utf-8", "replace")


def titles_from_tosec_pack(zip_path: str) -> dict[str, set[str]]:
    """The games of a TOSEC "DAT Pack - Complete" zip (TOSEC-PIX skipped).
    Read with ``zipfile`` (a member name in the pack isn't valid UTF-8,
    which some ``unzip`` builds refuse)."""
    store: dict[str, set[str]] = collections.defaultdict(set)
    with zipfile.ZipFile(zip_path) as z:
        for info in _members(z):
            n = info.filename
            if not n.endswith(".dat") or n.startswith("TOSEC-PIX/") or "/" not in n:
                continue
            base = re.sub(r" \(TOSEC-v[^)]*\)\.dat$", "", n.split("/", 1)[1])
            parts = base.split(" - ")
            plat = TOSEC_SYS.get(parts[0])
            typ = " - ".join(p for p in parts[1:] if not p.startswith("["))
            if not plat or not tosec_is_games(typ):
                continue
            data = _read_dat(z, info)
            if data is None:
                continue
            for raw, _body in iter_games(data):
                name = _name(raw)
                if name:
                    emit(store, plat, tosec_title(name))
    return store


# ── No-Intro ──────────────────────────────────────────────────────────────────

# DAT file basename → platform.  The Software Preservation Society-credited
# DATs (Amiga, Atari ST, IBM PC floppy, ZX Spectrum +3) are left out of the
# bundled snapshot: their rights may partly sit with SPS.
NOINTRO_SYS = [
    (r"^Commodore - Commodore 64 \((\d|PP|Tapes)", "c64"),
    (r"^Atari - 8-bit Family \(", "atari8"),
    (r"^Amstrad - CPC \(", "cpc"),
    (r"^Microsoft - MSX2? \(", "msx"),
    (r"^NEC - PC Engine - TurboGrafx-16 \(", "pce"), (r"^NEC - PC Engine SuperGrafx \(", "pce"),
    (r"^Nintendo - Nintendo Entertainment System \(Headered\)", "nes"),
    (r"^Nintendo - Family Computer Disk System \(FDS\)", "nes"),
    (r"^Nintendo - Super Nintendo Entertainment System \(", "snes"),
    (r"^Nintendo - Satellaview \(", "snes"), (r"^Nintendo - Sufami Turbo \(", "snes"),
    (r"^Nintendo - Game Boy \(\d", "gb"), (r"^Nintendo - Game Boy Color \(", "gb"),
    (r"^Nintendo - Game Boy Advance \(\d", "gba"),
    (r"^Nintendo - Nintendo 64 \(BigEndian\)", "n64"), (r"^Nintendo - Nintendo 64DD \(", "n64"),
    (r"^Nintendo - Nintendo DS \((Decrypted|Download Play)\)", "nds"),
    (r"^Sega - Mega Drive - Genesis \(", "megadrive"), (r"^Sega - 32X \(", "megadrive"),
    (r"^Sega - Master System - Mark III \(", "sms"),
    (r"^Sega - Game Gear \(", "gamegear"),
    (r"^Sony - PlayStation \(PS one Classics\)", "psx"),
    (r"^Sharp - X68000 \(", "x68000"),
    (r"^Arcade - PC-based \(", "arcade"),
]
_GAME_CATS = {"Games", "Preproduction"}
_NAME_EXCLUDE = ("[BIOS]", "(Program)", "(Test Program)")


def _keep_entry(name: str, cats: list[str], *, need_category: bool) -> bool:
    if any(x in name for x in _NAME_EXCLUDE):
        return False
    if not cats:
        return not need_category
    return bool(set(cats) & _GAME_CATS)


def titles_from_nointro_dats(dat_files) -> dict[str, set[str]]:
    """The games of No-Intro DAT files (paths; basenames decide the system)."""
    store: dict[str, set[str]] = collections.defaultdict(set)
    for f in sorted(dat_files):
        plat = next((p for rx, p in NOINTRO_SYS if re.search(rx, os.path.basename(f))), None)
        if not plat:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        for raw, body in iter_games(data):
            name = _name(raw)
            if name and _keep_entry(name, CAT_RE.findall(body), need_category=False):
                emit(store, plat, nointro_title(name))
    return store


# ── Redump ────────────────────────────────────────────────────────────────────

# redump.org/datfile/<code>/ → platform, for the systems SoniqBoom plays music
# of (IBM PC is Windows-era software, a poor fit for DOS music — left out).
REDUMP_SYSTEMS = {
    "psx": "psx", "ps2": "ps2", "ss": "saturn", "dc": "dreamcast", "mcd": "megadrive",
    "pce": "pce", "cd32": "amiga", "cdtv": "amiga", "ngcd": "neogeo", "x68k": "x68000",
    "naomi": "arcade", "naomi2": "arcade",
}
REDUMP_NAMES = {
    "psx": "PlayStation", "ps2": "PlayStation 2", "ss": "Saturn", "dc": "Dreamcast",
    "mcd": "Mega-CD", "pce": "PC Engine CD", "cd32": "Amiga CD32", "cdtv": "CDTV",
    "ngcd": "Neo Geo CD", "x68k": "X68000", "naomi": "NAOMI", "naomi2": "NAOMI 2",
}


def titles_from_redump_zip(zip_path: str, plat: str) -> dict[str, set[str]]:
    """The games of one Redump per-system datfile zip (one Logiqx DAT inside)."""
    store: dict[str, set[str]] = collections.defaultdict(set)
    with zipfile.ZipFile(zip_path) as z:
        for info in _members(z):
            if not info.filename.lower().endswith(".dat"):
                continue
            data = _read_dat(z, info)
            if data is None:
                continue
            for raw, body in iter_games(data):
                name = _name(raw)
                if name and _keep_entry(name, CAT_RE.findall(body), need_category=True):
                    emit(store, plat, nointro_title(name))
    return store
