"""Known game titles per retro platform — a retro track's game from the name
of the archive it sits in.

The sixth of a retro track's game-name sources (``store.GAME_NAME_SLOTS``:
the file's own tag, Modland, Demozoo, the song database, a Modland file name,
then this, then the opt-in folder name): when an archive holding the track is
named like a game of the track's platform — "Turrican.lha::mdat.title" for an
Amiga TFMX tune, "Last_Ninja_2.zip::tune.sid" for a C64 tune — that game is
the track's ``game_by_archive``.  The lists also spell the Modland file-name
guess (``list_spelling``).

Title lists (``platform<TAB>title`` gzip TSVs):

* bundled (``soniqboom/data/game_titles/``, built by
  ``scripts/build_game_titles.py``): Wikidata, No-Intro, the MAME software
  lists, ZXDB — see ``sources.json`` there;
* downloaded on request into ``<data dir>/game_titles/``: TOSEC (home
  computers) and Redump (disc consoles) — ``start_download``;
* the library's own HVSC copy: the tune names of its ``GAMES`` folder are C64
  games.

Matching (``archive_game``) compares normalised titles (``title_key``) on the
track's platform(s) only (``platforms_for``), innermost archive first — an
archive that only wraps its one tune is skipped — and refuses what is likely
no game: a generic / part-of-game word, a pure number, a name a tune of that
same archive credits as its artist, composer, group or label (a C64 tune's
"released" line included).  A one-word name shorter than eight letters needs
two lists to agree; a PC/Amiga tracker-style module (MOD, XM, S3M, IT, MED,
AHX …) needs a name of two or more words that two lists agree on and that is
no module's own title in that archive.  The pass (``apply_archive_games``)
writes the slot of every archive member and withdraws it where the name no
longer matches or the setting (``CONFIG_KEY``) is off.  After its first run it
judges only the tracks written since (``enrich_delta``) together with every
other member of their outermost archives (whose credits, song titles and
member counts the verdict reads) — the whole library again when the lists, the
setting or the library's HVSC ``GAMES`` names changed.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import gc
import gzip
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import urllib.request
import zipfile
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_KEY = "game_from_archive_name"        # setting: on by default
SLOT = "game_by_archive"
SOURCE = "archive"


def _find_bundled_dir() -> Path:
    """The bundled lists: ``<package>/data/game_titles`` (source install), else
    next to a frozen app's executable (``<exe>/data/game_titles``, or under
    ``Contents/Resources``) — like ``main._find_frontend_dir``."""
    dev = Path(__file__).resolve().parent.parent / "data" / "game_titles"
    if dev.is_dir():
        return dev
    import sys
    exe_dir = Path(sys.executable).resolve().parent
    for cand in (exe_dir / "data" / "game_titles",
                 exe_dir.parent / "Resources" / "data" / "game_titles"):
        if cand.is_dir():
            return cand
    return dev


BUNDLED_DIR = _find_bundled_dir()
BUNDLED_SOURCES = ("wikidata", "nointro", "mame", "zxdb")
DOWNLOAD_SOURCES = ("tosec", "redump")
SOURCE_NAMES = {"wikidata": "Wikidata", "nointro": "No-Intro", "mame": "MAME software lists",
                "zxdb": "ZXDB", "tosec": "TOSEC", "redump": "Redump", "hvsc": "HVSC"}
# A title's spelling comes from the first list in this order that holds it.
_SOURCE_ORDER = ("wikidata", "nointro", "tosec", "redump", "mame", "zxdb", "hvsc")
_SOURCE_BIT = {s: 1 << i for i, s in enumerate(_SOURCE_ORDER)}

# ── Normalised titles ─────────────────────────────────────────────────────────

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
          "xi": "11", "xii": "12", "xiii": "13"}
_TAG_RE = re.compile(r"[\(\[][^\(\)\[\]]*[\)\]]")     # (Europe) [a] (1989) … (linear)
# A name longer than this is cut before the patterns run (titles are far
# shorter; a hostile list line must not cost quadratic regex time).
_MAX_KEYED = 512
_APOS_RE = re.compile(r"['’‘`´]")
# 'n' / 'n / n' between words ("Ghouls'n Ghosts", "Rock 'n' Roll", "Guns n' Roses")
_AND_N_RE = re.compile(r"(?<=\w)\s*['’‘`´]n['’‘`´](?=[\w\s]|$)"
                       r"|(?<=\w)\s*['’‘`´]n(?=\s|$)"
                       r"|(?<=\s)n['’‘`´](?=\s)", re.IGNORECASE)
_TRAINER_RE = re.compile(r"\s*\+\s*\d+\s*$")           # "Turrican +3" (a cracked release's trainer)
_LETTER_DIGIT_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")
_SUBTITLE_RE = re.compile(r"\s*[:–—]\s*|\s+-\s+")


def _fold(s: str) -> str:
    """Compatibility forms folded (full-width Latin, half-width kana), Latin
    accents dropped ("Pokémon" → "pokemon") — other scripts' marks kept (a
    Japanese voiced kana stays itself) — and lower case."""
    s = unicodedata.normalize("NFKC", s or "")
    out = []
    for c in s:
        if not c.isascii() and ord(c) < 0x250:
            out.append("".join(x for x in unicodedata.normalize("NFKD", c)
                               if not unicodedata.combining(x)))
        else:
            out.append(c)
    return "".join(out).lower()


def _join_letters(words: list[str]) -> list[str]:
    """Runs of two or more single letters as one word (["chase", "h", "q"] →
    ["chase", "hq"]); a lone letter stays."""
    out: list[str] = []
    run: list[str] = []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if run:
            out.append("".join(run))
            run = []
        if w:
            out.append(w)
    return out


def title_key(name: str) -> str:
    """A title reduced to what two spellings of it share: case and Latin
    accents dropped, apostrophes removed ("Don't" → "dont"), "&" and "'n'" →
    "and", bracketed tags and a trainer "+N" cut, punctuation → spaces, letters
    and digits split ("Lotus3" → "lotus 3"), Roman numerals → digits, a run of
    single letters joined ("Chase H.Q." → "chase hq"), a leading "The" / a
    trailing ", The" dropped."""
    s = _fold((name or "")[:_MAX_KEYED])
    s = _TAG_RE.sub(" ", s)
    s = _TRAINER_RE.sub("", s)
    s = _AND_N_RE.sub(" and ", s)
    s = _APOS_RE.sub("", s).replace("&", " and ").replace("_", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = _LETTER_DIGIT_RE.sub(" ", s)
    words = [_ROMAN.get(w, w) for w in s.split()]
    words = ["and" if w == "n" and 0 < i < len(words) - 1 else w for i, w in enumerate(words)]
    words = _join_letters(words)
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    if len(words) > 1 and words[-1] == "the":
        words = words[:-1]
    return " ".join(words)


def base_key(name: str) -> str:
    """``title_key`` of the title before its subtitle ("Turrican II: The
    Final Fight" → "turrican 2"), "" when it has none."""
    parts = _SUBTITLE_RE.split((name or "")[:_MAX_KEYED], maxsplit=1)
    return title_key(parts[0]) if len(parts) > 1 else ""


_ROMAN_WORD_RE = re.compile(r"\b(?:II|III|IV|VI|VII|VIII|IX|XI|XII|XIII)\b")


def display_title(title: str) -> str:
    """A list title for display: trailing region / version tags cut ("Alpha
    Waves (Europe)" → "Alpha Waves"; "688(I) Hunter-Killer" kept; a title
    that is only a tag stays).  Linear: cut from the end, one tag at a time."""
    t = (title or "")[:_MAX_KEYED].rstrip()
    while t and t[-1] in ")]":
        i = t.rfind("(" if t[-1] == ")" else "[")
        if i <= 0:
            break
        t = t[:i].rstrip()
    return t or (title or "").strip()


def _display_rank(title: str) -> tuple:
    """Lower is a better spelling of one list to show: no ", The" tail, not
    ALL CAPS, Latin script, Roman numerals where the title has them
    ("Turrican II" over "Turrican 2"), shorter."""
    return (title.rstrip().lower().endswith(", the"), title.isupper(), not title.isascii(),
            -len(_ROMAN_WORD_RE.findall(title)), len(title))


# ── The index ─────────────────────────────────────────────────────────────────

class TitleIndex:
    """Titles by platform: ``keys[platform][title_key] = (display, sources,
    display source)`` (``sources`` a bit set of ``_SOURCE_BIT``; the display
    spelling is the first list's in ``_SOURCE_ORDER``, that list's best one)
    and ``bases[platform][base_key]`` alike for the part before a title's
    subtitle.  Entries are tuples of strings and ints, which the cyclic GC
    stops tracking — a full collection then skips the ~250k entries."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple]] = {}
        self.bases: dict[str, dict[str, tuple]] = {}
        self.counts: dict[str, dict[str, int]] = {}
        self._squashed: dict[str, dict[str, tuple]] | None = None

    @staticmethod
    def _merge(table: dict, key: str, display: str, order: int, bit: int) -> None:
        cur = table.get(key)
        if cur is None:
            table[key] = (display, bit, order)
            return
        shown, bits, shown_order = cur
        if order < shown_order or (order == shown_order
                                   and _display_rank(display) < _display_rank(shown)):
            shown, shown_order = display, order
        table[key] = (shown, bits | bit, shown_order)

    def add(self, source: str, platform: str, title: str) -> None:
        k = title_key(title)
        if not k:
            return
        bit, order = _SOURCE_BIT[source], _SOURCE_ORDER.index(source)
        self._merge(self.keys.setdefault(platform, {}), k, display_title(title), order, bit)
        parts = _SUBTITLE_RE.split(title, maxsplit=1)
        if len(parts) > 1:
            b = title_key(parts[0])
            if b and b != k:
                self._merge(self.bases.setdefault(platform, {}), b,
                            display_title(parts[0]), order, bit)

    def load_tsv(self, source: str, path: Path) -> int:
        n = 0
        counts = self.counts.setdefault(source, {})
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                plat, sep, title = line.rstrip("\n").partition("\t")
                if sep and title and len(title) <= _MAX_LISTED:
                    self.add(source, plat, title)
                    counts[plat] = counts.get(plat, 0) + 1
                    n += 1
        return n

    def squashed(self) -> dict[str, dict[str, tuple]]:
        """``{platform: {title key without its spaces: ((display, words, key), …)}}``
        — every spelling a title written without spaces could be, best first
        (the first list in ``_SOURCE_ORDER``, then ``_display_rank``).  Built
        on first use (~0.3 s)."""
        sq = self._squashed
        if sq is None:
            sq = {}
            for plat, keys in self.keys.items():
                cands: dict[str, list] = {}
                for k, (display, _bits, order) in keys.items():
                    cands.setdefault(k.replace(" ", ""), []).append(
                        ((order, _display_rank(display)), display, len(k.split()), k))
                sq[plat] = {s: tuple((d, n, k) for _r, d, n, k in sorted(c))
                            for s, c in cands.items()}
            self._squashed = sq
        return sq

    def total(self) -> int:
        """Distinct titles (per platform) — spellings of one title count once."""
        return sum(len(v) for v in self.keys.values())

    def lookup(self, key: str, platforms, extra: dict | None = None
               ) -> tuple[str | None, int, str | None, int]:
        """``key`` on ``platforms``: the display title and sources (bit set) of
        the exact title, and of a title whose part before its subtitle it is —
        each from the ONE platform whose lists know it best (two lists of two
        different platforms are not two lists agreeing).  ``extra``:
        ``{platform: {key: title}}`` of one more list (source "hvsc").  A
        title the lists write with more spaces ("Mega Man 2" for "megaman 2")
        counts as the exact title when no list has the key itself."""
        title, exact, btitle, base = None, 0, None, 0
        for p in platforms:
            t, bits = None, 0
            hit = self.keys.get(p, {}).get(key)
            if hit is not None:
                t, bits = hit[0], hit[1]
            ext = (extra or {}).get(p, {}).get(key)
            if ext is not None:
                t, bits = t or ext, bits | _SOURCE_BIT["hvsc"]
            if _bits(bits) > _bits(exact):
                title, exact = t, bits
            bhit = self.bases.get(p, {}).get(key)
            if bhit is not None and _bits(bhit[1]) > _bits(base):
                btitle, base = bhit[0], bhit[1]
        if not exact:
            words = len(key.split())
            squashed = self.squashed()
            for p in platforms:
                for _display, n, k in squashed.get(p, {}).get(key.replace(" ", ""), ()):
                    if n > words:
                        hit = self.keys[p][k]
                        if _bits(hit[1]) > _bits(exact):
                            title, exact = hit[0], hit[1]
                        break
        return title, exact, btitle, base


# A list line with a longer title is skipped (see game_titles_dat.MAX_NAME).
_MAX_LISTED = 300


def _bits(n: int) -> int:
    return bin(n).count("1")


_index: TitleIndex | None = None
_index_sig: tuple | None = None
_index_lock = threading.Lock()


def _download_dir() -> Path:
    from soniqboom.config import get_data_dir
    return get_data_dir() / "game_titles"


def _list_files() -> list[tuple[str, Path]]:
    out = [(s, BUNDLED_DIR / f"{s}.tsv.gz") for s in BUNDLED_SOURCES]
    d = _download_dir()
    out += [(s, d / f"{s}.tsv.gz") for s in DOWNLOAD_SOURCES]
    return [(s, p) for s, p in out if p.exists()]


def _lists_sig() -> tuple:
    sig = []
    for s, p in _list_files():
        try:
            sig.append((s, str(p), p.stat().st_mtime_ns))
        except OSError:
            pass
    return tuple(sig)


def get_index() -> TitleIndex:
    """The title index of every list present (bundled + downloaded), built
    once and rebuilt when a list file changes.  Thread-safe; blocking (a
    second or two) — call it off the event loop."""
    global _index, _index_sig
    sig = _lists_sig()
    with _index_lock:
        if _index is not None and sig == _index_sig:
            return _index
        idx = TitleIndex()
        t0 = time.perf_counter()
        for source, path, _mtime in sig:
            try:
                idx.load_tsv(source, Path(path))
            except (OSError, EOFError, ValueError) as exc:
                log.warning("game titles: can't read %s: %s", path, exc)
        log.info("game titles: %d titles from %s in %.1fs", idx.total(),
                 ", ".join(s for s, _p, _m in sig), time.perf_counter() - t0)
        _index, _index_sig = idx, sig
        return idx


def list_spelling(name: str, fmt: str | None, index: TitleIndex | None = None) -> str | None:
    """The title lists' spelling of game ``name`` for a track of format
    ``fmt``: the known title of the track's platform(s) that reads the same
    once case, punctuation, a leading "The", Roman numerals and spaces are
    ignored — "Backtothefuture3" → "Back to the Future III", "Mortal Kombat 2"
    → "Mortal Kombat II", "Kgb" → "KGB" — and splits it into no fewer words
    ("Dragon Stone" stays; "Dragonstone" joins it).  None when no list has
    it.  Used for the Modland file-name guess (``scene_metadata``).  Blocking
    the first time (the index is built) — call it off the event loop."""
    platforms, _strict = platforms_for(fmt)
    key = title_key(name)
    if not platforms or not key:
        return None
    idx = index if index is not None else get_index()
    for p in platforms:
        hit = idx.keys.get(p, {}).get(key)
        if hit is not None:
            return hit[0]
    words = len(key.split())
    squashed = idx.squashed()
    for p in platforms:
        for display, n, _k in squashed.get(p, {}).get(key.replace(" ", ""), ()):
            if n >= words:
                return display
    return None


# ── Platforms of a track ──────────────────────────────────────────────────────

# Chip family (``retro.chip_family``) → platforms.
_FAMILY_PLATFORMS: dict[str, tuple[str, ...]] = {
    "sid": ("c64",), "atari": ("atarist",), "adlib": ("dos",), "paula": ("amiga",),
    "nes": ("nes",), "snes": ("snes",), "gameboy": ("gb",), "pokey": ("atari8",),
    "genesis": ("megadrive",), "pce": ("pce",),
    "vgm": ("megadrive", "sms", "gamegear", "arcade", "pce", "msx", "x68000", "neogeo",
            "gb", "nes"),
}
_FORMAT_PLATFORMS: dict[str, tuple[str, ...]] = {
    "PSF": ("psx",), "PSF2": ("ps2",), "USF": ("n64",), "GSF": ("gba",), "2SF": ("nds",),
    "NCSF": ("nds",), "SSF": ("saturn",), "DSF (Dreamcast)": ("dreamcast",),
    "KSS": ("msx", "sms", "gamegear"), "AY": ("zx", "cpc"), "Amiga": ("amiga",),
}
_TRACKER_PLATFORMS = ("amiga", "dos")
# uade players of Atari ST music ("Jochen Hippel ST", "TFMX ST", "Quartet PSG",
# "YM 2149") and Sierra's AGI (PC) — uade labels, matched case-sensitively.
_ATARI_PLAYER_RE = re.compile(r"\bST\b|\bPSG\b|YM[- ]?2149")
_DOS_PLAYER_RE = re.compile(r"\bAGI\b")
# Tracker-style Amiga players (sample trackers, MED, AHX …): the strict rules.
_TRACKERISH_RE = re.compile(r"tracker|\bmed\b|octa ?med|sound ?fx|oktalyzer|digi ?booster"
                            r"|startrekker|noisetracker|protracker|\bahx\b|hively",
                            re.IGNORECASE)


def platforms_for(fmt: str | None) -> tuple[tuple[str, ...], bool]:
    """The platforms whose game lists a track of ``format`` is matched
    against, and whether the strict tracker rules apply (sample trackers,
    MED, AHX / HivelyTracker).  ((), False) for a non-retro format (MIDI and
    PCM audio included)."""
    fmt = (fmt or "").strip()
    if not fmt:
        return (), False
    hit = _FORMAT_PLATFORMS.get(fmt)
    if hit:
        return hit, False
    from soniqboom.core.retro import chip_family
    fam = chip_family(fmt)
    if fam is None or fam == "midi":
        return (), False
    if fam == "tracker":
        return _TRACKER_PLATFORMS, True
    if fam == "ahx":
        return ("amiga",), True
    if fam == "paula":
        if _ATARI_PLAYER_RE.search(fmt):
            return ("atarist",), False
        if _DOS_PLAYER_RE.search(fmt):
            return ("dos",), False
        return ("amiga",), bool(_TRACKERISH_RE.search(fmt))
    return _FAMILY_PLATFORMS.get(fam, ()), False


# ── Matching ──────────────────────────────────────────────────────────────────

# Words that name a pile of tunes, a part of a game's music or a scene event,
# not a game — on top of ``folder_album._generic`` / ``_part_name``.  Compared
# with ``title_key`` (a leading "The" already dropped: "The Party" → "party").
_STOP_KEYS = frozenset({
    "intro", "intros", "outro", "demo", "demos", "music", "musics", "musik", "tune", "tunes",
    "song", "songs", "soundtrack", "soundtracks", "ost", "mod", "mods", "module", "modules",
    "title", "titles", "menu", "game", "games", "game over", "end", "ending", "boss", "unknown",
    "untitled", "test", "party", "compo", "pack", "collection", "disk", "disc", "sfx", "sound",
    "sounds", "jingle", "jingles", "various", "misc", "other", "others", "remix", "remixes",
    "cracktro", "cracktros", "loader", "loaders", "chip", "chiptune", "chiptunes", "sid", "sids",
    "ahx", "xm", "it", "s3m", "ym", "sndh", "spc", "nsf", "vgm", "psf", "musicdisk",
    "music disk", "diskmag", "assembly", "revision", "breakpoint", "evoke",
    "gathering", "solskogen", "x party", "datastorm", "forever",
})
_MUSIC_EXT_RE = re.compile(
    r"\.(mod|xm|it|s3m|mtm|669|med|ahx|hvl|sid|ym|sndh|sc68|spc|nsf|nsfe|gbs|vgm|vgz|kss|hes"
    r"|ay|sap|gym|minipsf|psf|psf2|gsf|minigsf|usf|miniusf|2sf|mini2sf|ssf|minissf|dsf|minidsf"
    r"|ncsf|minincsf|mid|midi)$", re.IGNORECASE)
# A one-word name shorter than this needs two lists.
_SHORT_WORD = 8
# Only this many innermost archives of a path are looked at (real nesting is
# two or three deep).
_MAX_NESTING = 8


def archive_names(path: str, multi=frozenset(), *,
                  skip_wrapper: bool = True) -> list[tuple[str, str]]:
    """The archives holding ``path`` ("a/Turrican.lha::b/x.mod" →
    [("Turrican", "a/Turrican.lha")]), innermost first, as (cleaned name,
    archive key) — the name without its extension (and a wrapped module's
    own: "axelf.mod.zip" → "axelf").  With ``skip_wrapper`` (formats named
    like songs, ``_song_named``) an innermost archive that only wraps its
    tune — named like it ("abracadabra.mod.zip::abracadabra.mod"; see
    ``folder_album._is_wrapper``; ``multi``: the archives holding several
    tunes, ``folder_album.multi_member_archives``) — names nothing but the
    song and is skipped; a custom-player tune or console rip is usually named
    after its game, so its wrapper is looked at."""
    from soniqboom.core.folder_album import (_ARCHIVE_EXT_RE, _is_wrapper, _music_exts,
                                             clean_folder_name)
    exts = _music_exts()
    segs = (path or "").split("::")
    member = segs[-1].replace("\\", "/")[-_MAX_KEYED:]
    out = []
    for i in range(len(segs) - 2, max(-1, len(segs) - 2 - _MAX_NESTING), -1):
        key = "::".join(segs[:i + 1])
        # A name is far shorter than _MAX_KEYED; the cut bounds every pattern.
        stem = _ARCHIVE_EXT_RE.sub("", segs[i].replace("\\", "/").rsplit("/", 1)[-1][-_MAX_KEYED:])
        if (skip_wrapper and i == len(segs) - 2 and "/" not in member
                and _is_wrapper(stem, member, key in multi)):
            continue
        base, dot, ext = stem.rpartition(".")
        if dot and base and ext.lower() in exts:
            stem = base                          # "turrican.hip.zip" → "turrican"
        name = clean_folder_name(_MUSIC_EXT_RE.sub("", stem))
        if name:
            out.append((name, key))
    return out


def _refused_name(name: str, key: str, nkey: str, credited) -> bool:
    from soniqboom.core import folder_album as fa
    if len(key.replace(" ", "")) < 3 or key in _STOP_KEYS:
        return True
    if fa._generic(name) or fa._part_name(name):         # pure numbers included
        return True
    return nkey in credited


def archive_game(path: str, fmt: str | None, index: TitleIndex, *,
                 credits: dict | None = None, tune_titles: dict | None = None,
                 extra: dict[str, dict[str, str]] | None = None,
                 multi=frozenset()) -> str | None:
    """The game ``path``'s archive is named after, as the lists spell it, or
    None (see the module docstring for the rules).  ``credits``: ``{archive
    key: {name_key, …}}`` of the artists, composers, groups and labels its
    tunes credit (``_archive_credits``); ``tune_titles``: ``{archive key:
    [title, …]}`` of its tracker-style modules' own titles (``_tracker_titles``;
    normalised in place the first time the archive matches) — a tracker archive
    named like one of its songs is that song's; ``extra``: ``{platform:
    {title_key: title}}`` of an additional list (the library's HVSC ``GAMES``
    names); ``multi``: see ``archive_names``."""
    if "::" not in (path or ""):
        return None
    platforms, strict = platforms_for(fmt)
    if not platforms:
        return None
    from soniqboom.core.folder_album import name_key
    for name, akey in archive_names(path, multi, skip_wrapper=_song_named(fmt, strict)):
        key = title_key(name)
        if not key or _refused_name(name, key, name_key(name), (credits or {}).get(akey, ())):
            continue
        title, exact, btitle, base = index.lookup(key, platforms, extra)
        words = key.split()
        if strict:
            if len(words) < 2:
                continue                         # a one-word tracker archive is a song title
            need = 2
        else:
            need = 1 if (len(words) > 1 or len(key) >= _SHORT_WORD) else 2
        if _bits(exact) >= need:
            hit = title
        elif len(words) > 1 and _bits(base) >= need:
            hit = btitle or name                 # a game's title before its subtitle
        else:
            continue
        if strict and tune_titles and _is_song_title(tune_titles, akey, key):
            continue                             # named after one of its songs
        return hit
    return None


def _song_named(fmt: str | None, strict: bool) -> bool:
    """Formats whose files are mostly named like songs — tracker-style
    modules and SIDs (scene music) — so an archive wrapping one such file
    names the song, not a game (on a real library the one SID wrapper that
    matched was a 2003 scene tune, "halloween.zip::halloween.sid").  A custom
    Amiga player's or a console rip's file is named after its game."""
    if strict:
        return True
    from soniqboom.core.retro import chip_family
    return chip_family(fmt) == "sid"


def _is_song_title(tune_titles: dict, akey: str, key: str) -> bool:
    titles = tune_titles.get(akey)
    if not titles:
        return False
    if not isinstance(titles, frozenset):
        titles = tune_titles[akey] = frozenset(filter(None, map(title_key, titles)))
    return key in titles


# ── Library-derived inputs ────────────────────────────────────────────────────

_HVSC_GAME_RE = re.compile(r"/GAMES/[^/]+/([^/:]+)\.sid$")
_CREDIT_FIELDS = ("artist", "composer", "album_artist", "scene_group", "label")


def _hvsc_games(tracks) -> dict[str, dict[str, str]]:
    """The C64 games of the library's HVSC ``GAMES`` folder (the tune file's
    name is its game)."""
    games: dict[str, str] = {}
    for t in tracks:
        m = _HVSC_GAME_RE.search(t.get("path") or "")
        if m:
            title = m.group(1).replace("_", " ")
            games.setdefault(title_key(title), title)
    games.pop("", None)
    return {"c64": games} if games else {}


# A SID's "released" line (its ``comment``): "1988 Fairlight", "(C) 1990 Thalamus".
_SID_RELEASED_RE = re.compile(r"^\s*(?:\(c\)|©)?\s*(?:(?:19|20)\d\d(?:\s*[-/]\s*\d{2,4})?)?\s*",
                              re.IGNORECASE)


def _tracker_titles(tracks) -> dict[str, list]:
    """``{archive key: [title, …]}`` — the own titles of the tracker-style
    modules inside each archive (at any depth), as stored."""
    titles: dict[str, list] = collections.defaultdict(list)
    strict_of: dict = {}
    for t in tracks:
        p = t.get("path") or ""
        title = t.get("title")
        if "::" not in p or not isinstance(title, str) or not title.strip():
            continue
        fmt = t.get("format")
        strict = strict_of.get(fmt)
        if strict is None:
            strict = strict_of[fmt] = platforms_for(fmt)[1]
        if not strict:
            continue
        segs = p.split("::")
        for i in range(1, len(segs)):
            titles["::".join(segs[:i])].append(title)
    return titles


def _archive_credits(tracks) -> dict[str, set]:
    """``{archive key: {name_key, …}}`` — every artist, composer, album
    artist, group and label a tune inside that archive (at any depth) credits,
    and the releaser of a SID ("1988 Fairlight" → Fairlight)."""
    from soniqboom.core.folder_album import name_key
    credits: dict[str, set] = collections.defaultdict(set)
    for t in tracks:
        p = t.get("path") or ""
        if "::" not in p:
            continue
        keys = set()
        for f in _CREDIT_FIELDS:
            v = t.get(f)
            if isinstance(v, str) and v.strip():
                keys.add(name_key(v))
        if t.get("format") == "SID" and isinstance(t.get("comment"), str):
            rel = _SID_RELEASED_RE.sub("", t["comment"], count=1).strip()
            keys.update(name_key(x) for x in re.split(r"\s*/\s*", rel) if x.strip())
        keys.discard("")
        if not keys:
            continue
        segs = p.split("::")
        for i in range(1, len(segs)):
            credits["::".join(segs[:i])] |= keys
    return credits


# ── The pass ──────────────────────────────────────────────────────────────────

_pending = False
_running = False
_last_result: dict | None = None
_last_sig: tuple | None = None
_bg_tasks: set = set()
_pass_lock: asyncio.Lock | None = None
_pass_lock_loop = None
# Tracks written per store call: small enough that one call (with the game
# derivation of every track in it) keeps the event loop responsive.
_CHUNK = 200


def enabled() -> bool:
    try:
        from soniqboom.core.store import get_store
        return bool(get_store().get_config(CONFIG_KEY, True))
    except Exception:                            # noqa: BLE001
        return True


@contextlib.contextmanager
def _gc_paused():
    """The cyclic GC paused while the pass builds its per-archive tables — tens
    of thousands of short-lived acyclic sets and strings that otherwise trip
    two or three full collections per pass (measured on a 263k-track library
    with the heap frozen: event-loop pauses up to 42 ms with 2–3 full
    collections per pass → up to 36 ms with none); like
    ``subsonic_index._gc_paused``."""
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


def desired_slots(tracks, *, on: bool | None = None,
                  extra: dict | None = None) -> dict[str, str | None]:
    """``{track id: game_by_archive}`` for every track of ``tracks`` whose slot
    should change (None = withdraw).  ``tracks`` holds every member of each
    archive it touches; ``extra``: the library's HVSC ``GAMES`` names
    (``_hvsc_games``) when ``tracks`` is not the whole library (default: taken
    from ``tracks``).  Blocking — run it in a thread."""
    on = enabled() if on is None else on
    out: dict[str, str | None] = {}
    if not on:
        for t in tracks:
            if t.get(SLOT):
                out[t["id"]] = None
        return out
    from soniqboom.core.folder_album import multi_member_archives
    index = get_index()
    with _gc_paused():
        if extra is None:
            extra = _hvsc_games(tracks)
        credits = _archive_credits(tracks)
        tune_titles = _tracker_titles(tracks)
        multi = multi_member_archives(tracks)
        for t in tracks:
            path = t.get("path") or ""
            want = archive_game(path, t.get("format"), index, credits=credits,
                                tune_titles=tune_titles, extra=extra,
                                multi=multi) if "::" in path else None
            if (t.get(SLOT) or None) != want:
                out[t["id"]] = want
    return out


def _get_pass_lock() -> asyncio.Lock:
    global _pass_lock, _pass_lock_loop
    loop = asyncio.get_running_loop()
    if _pass_lock is None or _pass_lock_loop is not loop:
        _pass_lock, _pass_lock_loop = asyncio.Lock(), loop
    return _pass_lock


def _count_ids(store, tracks) -> tuple[set[str], set[str]]:
    """Of ``tracks`` as the store now holds them: the ids of those that show
    their archive's name — as their game or as another name of it (an alias
    another name starts with, or one of a game the user cleared, is not shown)
    — and of those that take their game from it.  Safe off the event loop
    (one ``dict.get`` per track)."""
    from soniqboom.core.store import _game_key
    named: set[str] = set()
    primary: set[str] = set()
    get = store._tracks.get
    for old in tracks:
        t = get(old["id"])
        v = t.get(SLOT) if t else None
        if not v:
            continue
        if t.get("game_source") == SOURCE:
            named.add(t["id"])
            primary.add(t["id"])
        elif _game_key(v) in {_game_key(a) for a in (t.get("game_aliases") or ()) if isinstance(a, str)}:
            named.add(t["id"])
    return named, primary


# Delta bookkeeping, as of the last pass (rebuilt by a full one): each archive
# member's outermost archive and the members of each — a member's verdict
# reads its archives' credits, song titles and member count, so a change
# re-judges every member of its outermost archive —, the tracks named like an
# HVSC GAMES tune (``_HVSC_GAME_RE``) and those names (``_hvsc_games``), and
# the ids the counts (``_count_ids``) are made of.  Members are kept as tuples
# of ids, which the cyclic GC does not track (a set per archive did).
_outer: dict[str, str] = {}
_members: dict[str, tuple] = {}
_hvsc_ids: set[str] = set()
_extra: dict = {}
_named_ids: set[str] = set()
_primary_ids: set[str] = set()


def _outer_of(path: str) -> str | None:
    return path.split("::", 1)[0] if "::" in path else None


def _bookkeeping(tracks) -> tuple:
    """``(_outer, _members, _hvsc_ids, _extra)`` of the whole library
    ``tracks``.  Blocking — run it in a thread."""
    outer: dict[str, str] = {}
    members: dict[str, list] = {}
    hvsc: list = []
    for t in tracks:
        path = t.get("path") or ""
        o = _outer_of(path)
        if o is not None:
            outer[t["id"]] = o
            members.setdefault(o, []).append(t["id"])
        if "/GAMES/" in path and _HVSC_GAME_RE.search(path):
            hvsc.append(t)
    return (outer, {o: tuple(ids) for o, ids in members.items()},
            {t["id"] for t in hvsc}, _hvsc_games(hvsc))


def _widen(changed: dict[str, str | None]) -> tuple[set[str], bool]:
    """Bring the bookkeeping up to date with the changed tracks (``changed``:
    id → its path now, None when deleted) and return ``(the ids to judge,
    whether an HVSC GAMES tune came or went)`` — the changed tracks plus every
    member of the outermost archives they were or are in.  Reads no store:
    run it in a thread (under the pass lock)."""
    from soniqboom.core.folder_album import _Edits
    outers: set[str] = set()
    edits = _Edits()
    hvsc = False
    for tid, path in changed.items():
        old = _outer.pop(tid, None)
        if old is not None:
            outers.add(old)
            edits.pop(old, tid)
        if tid in _hvsc_ids:
            _hvsc_ids.discard(tid)
            hvsc = True
        if path is None:
            continue
        o = _outer_of(path)
        if o is not None:
            _outer[tid] = o
            edits.put(o, tid)
            outers.add(o)
        if "/GAMES/" in path and _HVSC_GAME_RE.search(path):
            _hvsc_ids.add(tid)
            hvsc = True
    edits.apply(_members)
    ids = set(changed)
    for o in outers:
        ids.update(_members.get(o, ()))
    return ids, hvsc


async def apply_archive_games(force: bool = False) -> dict:
    """Write the ``game_by_archive`` slots (see ``desired_slots``).  One pass
    at a time.  After its first run a pass judges only the tracks written
    since the last one with every other member of their outermost archives
    (``enrich_delta``, ``_widen``) — ``{"skipped": "unchanged"}`` when there
    are none — and the whole library when ``force``d, when the lists or the
    setting changed or the library's HVSC GAMES names did.  A pass that
    another write overlapped schedules one more (which re-reads what was
    written since its snapshot).  Returns ``{"updated": n, "named": m,
    "primary": k, "seconds": s}`` (``named``: tracks of the library that show
    their archive's name; ``primary``: those whose game it is)."""
    global _last_result, _last_sig, _outer, _members, _hvsc_ids, _extra
    global _named_ids, _primary_ids
    from soniqboom.core import enrich_delta
    from soniqboom.core.store import get_store
    async with _get_pass_lock():
        store = get_store()
        on = enabled()
        lsig = _lists_sig()
        inputs = (lsig, on)
        changed = enrich_delta.changes(store, _last_sig, inputs, force=force)
        if changed is not None and not changed:
            return {"skipped": "unchanged"}
        try:
            t0 = time.perf_counter()
            snap = store.enrich_cursor()                 # taken with the snapshot
            ids: set[str] | None = None
            if changed is not None:
                paths = {}
                for tid in changed:                      # as of the snapshot
                    t = store.get_track(tid)
                    paths[tid] = None if t is None else (t.get("path") or "")
                ids, hvsc = await asyncio.to_thread(_widen, paths)
                if len(ids) > enrich_delta.limit(store):
                    ids = None                           # no cheaper than all of it
                elif hvsc:
                    # A GAMES tune came or went: another C64 archive anywhere
                    # may match now — only when the names really changed.
                    every, hv = store.all_tracks(), set(_hvsc_ids)
                    names = await asyncio.to_thread(
                        lambda: _hvsc_games([t for t in every if t["id"] in hv]))
                    if names != _extra:
                        ids = None
            if ids is None:
                tracks = store.all_tracks()

                def full():
                    book = _bookkeeping(tracks)
                    return desired_slots(tracks, on=on, extra=book[3]), book
                changes, book = await asyncio.to_thread(full)
                _outer, _members, _hvsc_ids, _extra = book
            else:
                tracks = enrich_delta.tracks_of(store, ids)
                changes = await asyncio.to_thread(desired_slots, tracks, on=on, extra=_extra)
            items = [(tid, {SLOT: v}) for tid, v in changes.items()]
            updated = own = 0
            stale = False
            for i in range(0, len(items), _CHUNK):
                if enabled() != on:
                    stale = True                     # switched meanwhile: the next pass decides
                    break
                before = store._enrich_seq
                updated += store.update_track_fields_batch(items[i:i + _CHUNK])
                own += store._enrich_seq - before
                await asyncio.sleep(0)
            named, primary = await asyncio.to_thread(_count_ids, store, tracks)
        except BaseException:
            _last_sig = None                         # the bookkeeping may be half done
            raise
        if ids is None:
            _named_ids, _primary_ids = named, primary
        else:
            _named_ids = (_named_ids - ids) | named
            _primary_ids = (_primary_ids - ids) | primary
        _last_result = {"updated": updated, "named": len(_named_ids),
                        "primary": len(_primary_ids),
                        "seconds": round(time.perf_counter() - t0, 2)}
        if stale or _lists_sig() != lsig:
            _last_sig = None
            schedule()                               # something changed while it ran
        else:
            if enrich_delta.foreign_writes(store, snap, own):
                schedule()                           # another write landed meanwhile
            _last_sig = enrich_delta.record(store, snap, own, inputs)
    if updated:
        log.info("Game from archive name: %d track(s) updated, %d named", updated,
                 _last_result["named"])
        try:
            from soniqboom.core.folder_album import refresh_album_caches
            await refresh_album_caches([tid for tid, _ in items])
        except Exception:                        # noqa: BLE001
            log.debug("album cache refresh after the archive pass failed", exc_info=True)
    return _last_result


def schedule() -> None:
    """Run ``apply_archive_games`` in the background (after a scan, on
    start-up, after a list changes, after an on-demand ingest); calls while it
    runs coalesce into one more run.  A running scan is waited out first (a
    scan that changes nothing schedules no pass of its own)."""
    global _pending, _running
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                                   # nothing could run it
    _pending = True
    if _running:
        return
    _running = True

    async def _run() -> None:
        global _pending, _running
        try:
            while _pending:
                _pending = False
                if enabled():
                    # The title count shows while the pass waits for a scan.
                    try:
                        await asyncio.to_thread(get_index)
                    except Exception:            # noqa: BLE001
                        log.debug("title index not built", exc_info=True)
                await _scan_idle()
                try:
                    await apply_archive_games()
                except Exception:                # noqa: BLE001
                    log.exception("game-from-archive pass failed")
        finally:
            _running = False
    task = loop.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


_SCAN_POLL_S = 1.0


_waiting_for_scan = False


async def _scan_idle() -> None:
    """Return once no scan runs (checked every ``_SCAN_POLL_S``)."""
    global _waiting_for_scan
    try:
        from soniqboom.core import scanner
        while scanner.is_scanning():
            _waiting_for_scan = True
            await asyncio.sleep(_SCAN_POLL_S)
    except Exception:                            # noqa: BLE001
        log.debug("scan state unavailable", exc_info=True)
    finally:
        _waiting_for_scan = False


def pass_busy() -> bool:
    """A pass runs or waits to run."""
    return _running or _pending


def status() -> dict:
    """The lists present, their title counts, the downloads and the last pass.
    While a pass is due the index is not rebuilt here (the pass does it):
    ``titles`` is then the last index's count, or None before the first."""
    busy = pass_busy()
    idx = _index if busy else get_index()
    downloads = {}
    d = _download_dir()
    for s in DOWNLOAD_SOURCES:
        downloads[s] = {**_read_meta(s), "present": (d / f"{s}.tsv.gz").exists(),
                        "files": files_info(s), **_jobs.get(s, {})}
    bundled = {}
    try:
        with open(BUNDLED_DIR / "sources.json", encoding="utf-8") as fh:
            bundled = json.load(fh)
    except (OSError, ValueError):
        pass
    return {
        "enabled": enabled(),
        "pass_busy": busy,
        "pass_waiting_for_scan": busy and _waiting_for_scan,
        "titles": idx.total() if idx is not None else None,
        "bundled": {s: {"name": SOURCE_NAMES[s],
                        "titles": sum(idx.counts.get(s, {}).values()) if idx is not None else None,
                        "generated": bundled.get(s, {}).get("generated"),
                        "licence": bundled.get(s, {}).get("licence")}
                    for s in BUNDLED_SOURCES if s in bundled},
        "downloads": downloads,
        "last_pass": _last_result,
    }


# ── Downloads (TOSEC, Redump) ─────────────────────────────────────────────────
#
# The downloaded source files are KEPT (``sources/<source>/`` in the lists'
# folder): TOSEC's newest DAT pack, one zip per Redump system.  Adding a
# removed list again builds it from them without the network; an Update
# downloads a TOSEC pack only when tosecdev.org has a newer release (and
# builds from the kept one when the site can't be reached); a Redump
# download that was stopped continues with the systems it had not fetched,
# and a system that can't be fetched keeps the titles of its kept file.
# tosecdev.org answers byte-range requests wrongly (asked for bytes 0-99 of
# the pack, it sends "bytes 100621532-100621630"), so a transfer cut off
# mid-file starts that file again.

_UA = "SoniqBoom (https://github.com/SFCyris/SoniqBoom) game-title lists"
_TOSEC_HOME = "https://www.tosecdev.org/downloads"
_TOSEC_BASE = "https://www.tosecdev.org"
_REDUMP_URL = "http://redump.org/datfile/{code}/"      # redump.org serves no HTTPS
_REDUMP_SITE = "http://redump.org/"
_MAX_DOWNLOAD = 400 * 1024 * 1024
_MAX_PAGE = 5 * 1024 * 1024                              # a tosecdev.org web page
_MAX_REDUMP = 16 * 1024 * 1024                           # one Redump system (the largest: ~4 MB)
_TOSEC_DEADLINE_S = 45 * 60
_REDUMP_DEADLINE_S = 5 * 60                             # per system
_TOSEC_PACK_RE = re.compile(r"^tosec-dat-pack-(\d{4}-\d{2}-\d{2})\.zip$")
_PAUSE_S = 1.0                                          # between requests to one site
_CONNECT_S = 30                                          # to connect and get the headers
_jobs: dict[str, dict] = {}
_job_tasks: dict[str, asyncio.Task] = {}
_TERMINAL = ("done", "error", "interrupted")


class DownloadRunning(RuntimeError):
    """The list is being downloaded — it can't be removed now."""


class _Stopped(Exception):
    """The download was stopped (``cancel_download``)."""


_job_seq = itertools.count(1)


class _Ctl:
    """One download's controls — kept out of its job dict, which is served
    as JSON.  A new download gets new ones; a stopped download's thread
    keeps its own (stopped) ones, so it can't write anything any more even
    while it is still ending."""

    def __init__(self, epoch: int = 0) -> None:
        self.cancel = threading.Event()
        self.commit = threading.Lock()   # "not stopped → write" in one step (``publish``)
        self.committed = False           # the list was written: the job completes
        self.sock_lock = threading.Lock()
        self.sock = None                 # the socket of the transfer in progress
        self.epoch = epoch               # the server run (``scanner.begin_run``)
        self.tag = f"{os.getpid()}-{next(_job_seq)}"
        self.thread: threading.Thread | None = None

    def check(self) -> None:
        if self.cancel.is_set():
            raise _Stopped()

    def pause(self, seconds: float) -> None:
        """Wait ``seconds`` (between requests), or stop."""
        if self.cancel.wait(seconds):
            raise _Stopped()

    def set_sock(self, sock) -> None:
        with self.sock_lock:
            self.sock = sock

    def stop(self) -> None:
        """Stop: the next ``check`` raises, and a read in progress returns at
        once (its socket is shut down — the thread blocked in it can't be
        interrupted otherwise; the plain socket's shutdown, not TLS's)."""
        self.cancel.set()
        with self.sock_lock:
            sock = self.sock
            if sock is not None:
                import socket
                try:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except (OSError, TypeError, ValueError):
                    pass

    def publish(self, write):
        """Run ``write`` (a write into the lists' folder) unless stopped —
        never after a stop."""
        with self.commit:
            self.check()
            return write()


_ctls: dict[str, _Ctl] = {}


def _meta_path(source: str) -> Path:
    return _download_dir() / f"{source}.json"


def _read_meta(source: str) -> dict:
    try:
        with open(_meta_path(source), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _sources_dir(source: str) -> Path:
    """Where ``source``'s downloaded files are kept."""
    return _download_dir() / "sources" / source


def _read_sources_meta(source: str) -> dict:
    try:
        with open(_sources_dir(source) / "files.json", encoding="utf-8") as fh:
            meta = json.load(fh)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sources_meta(source: str, meta: dict) -> None:
    d = _sources_dir(source)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "files.json",
                  lambda tmp: tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8"))


# Reading a kept source file failed because the FILE is bad — not a valid zip,
# damaged (CRC, compressed data, a truncated member), or not a real list (too
# large, an unsupported compression): it is deleted.  Any other error (an I/O
# error, a permission, a data volume briefly gone) leaves it kept.
_BAD_FILE_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError,
                    ValueError, NotImplementedError)


def _kept_file(path: Path, size) -> bool:
    """``path`` is a whole kept file (its recorded size)."""
    try:
        return isinstance(size, int) and size > 0 and path.stat().st_size == size
    except OSError:
        return False


def _kept_tosec(meta: dict) -> Path | None:
    """The kept TOSEC pack, or None."""
    name = meta.get("file")
    if not isinstance(name, str) or not _TOSEC_PACK_RE.match(name):
        return None
    path = _sources_dir("tosec") / name
    return path if _kept_file(path, meta.get("size")) else None


def _kept_redump(meta: dict) -> dict[str, dict]:
    """``{code: entry}`` of the Redump systems whose kept file is whole."""
    from soniqboom.core import game_titles_dat as dat
    systems = meta.get("systems")
    if not isinstance(systems, dict):
        return {}
    d = _sources_dir("redump")
    return {c: e for c, e in systems.items()
            if c in dat.REDUMP_SYSTEMS and isinstance(e, dict)
            and _kept_file(d / f"{c}.zip", e.get("size"))}


def _socket_of(resp):
    """The socket under an ``http.client`` response, or None."""
    fp = getattr(resp, "fp", None)
    return getattr(getattr(fp, "raw", fp), "_sock", None)


def _fsync_dir(d: Path) -> None:
    """Make a rename in ``d`` durable (a power loss mustn't leave the new
    name over unwritten blocks)."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fetch(url: str, dest: Path | None = None, progress=None, timeout: float = 120,
           deadline: float | None = None, max_bytes: int | None = None,
           site: str | None = None, ctl: _Ctl | None = None) -> bytes | None:
    """GET ``url`` — into ``dest`` (written through to disk; returns None) or
    as bytes — capped at ``max_bytes`` (default ``_MAX_DOWNLOAD``);
    ``deadline`` (``time.monotonic()``) bounds the whole transfer; connecting
    and the headers get ``_CONNECT_S`` seconds at most, then each read
    ``timeout``; a body shorter than its Content-Length is an error; with
    ``site``, a redirect off that site is refused; with ``ctl``,
    ``cancel_download`` stops it (``_Stopped``) — a read in progress too."""
    cap = _MAX_DOWNLOAD if max_bytes is None else max_bytes
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    if deadline is not None:
        timeout = max(1.0, min(timeout, deadline - time.monotonic()))
    if ctl is not None:
        ctl.check()
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, _CONNECT_S)) as r:
            sock = _socket_of(r)
            if sock is not None:
                try:
                    sock.settimeout(timeout)
                except (OSError, AttributeError):
                    pass
            if ctl is not None:
                ctl.set_sock(sock)
            try:
                if ctl is not None:
                    ctl.check()                 # stopped while connecting
                final = r.geturl() if hasattr(r, "geturl") else url
                if site is not None and not (final or "").startswith(site):
                    raise ValueError(f"the download was redirected off {site}")
                total = int(r.headers.get("Content-Length") or 0)
                if total > cap:
                    raise ValueError(f"download too large ({total} bytes)")
                got = 0
                parts: list[bytes] = []
                with (open(dest, "wb") if dest is not None else contextlib.nullcontext()) as fh:
                    while True:
                        if ctl is not None:
                            ctl.check()
                        if deadline is not None and time.monotonic() > deadline:
                            raise TimeoutError("the download took too long")
                        # read1: what has arrived (≤ 256 KB) — a server trickling
                        # bytes can't hold a read past the deadline.
                        chunk = r.read1(256 * 1024) if hasattr(r, "read1") else r.read(256 * 1024)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > cap:
                            raise ValueError("download too large")
                        if fh is None:
                            parts.append(chunk)
                        else:
                            fh.write(chunk)
                        if progress:
                            progress(got, total)
                    if ctl is not None:
                        ctl.check()             # a cut-off read ends like the end of the body
                    if total and got != total:
                        raise ValueError(f"the download was cut off ({got} of {total} bytes)")
                    if fh is not None:
                        fh.flush()
                        os.fsync(fh.fileno())
            finally:
                if ctl is not None:
                    ctl.set_sock(None)          # before the response closes the socket
    except _Stopped:
        raise
    except Exception:
        if ctl is not None and ctl.cancel.is_set():
            raise _Stopped() from None          # the read failed because it was stopped
        raise
    return b"".join(parts) if dest is None else None


def _atomic_write(path: Path, write) -> None:
    tmp = path.with_name(f".{path.name}.part")
    write(tmp)
    os.replace(tmp, path)


def _read_list(source: str) -> dict[str, set[str]]:
    store: dict[str, set[str]] = collections.defaultdict(set)
    try:
        with gzip.open(_download_dir() / f"{source}.tsv.gz", "rt", encoding="utf-8") as fh:
            for line in fh:
                plat, sep, title = line.rstrip("\n").partition("\t")
                if sep and title:
                    store[plat].add(title)
    except (OSError, EOFError, ValueError):
        pass
    return store


def _write_list(source: str, store: dict[str, set[str]], meta: dict) -> dict[str, int]:
    d = _download_dir()
    d.mkdir(parents=True, exist_ok=True)
    counts = {}

    def write_tsv(tmp: Path) -> None:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for plat in sorted(store):
                titles = sorted({n for n in (" ".join(t.split()) for t in store[plat])
                                 if 0 < len(n) <= _MAX_LISTED}, key=str.casefold)
                if not titles:
                    continue
                counts[plat] = len(titles)
                for t in titles:
                    fh.write(f"{plat}\t{t}\n")
    _atomic_write(d / f"{source}.tsv.gz", write_tsv)
    meta = {**meta, "titles": sum(counts.values()), "counts": counts,
            "downloaded": time.strftime("%Y-%m-%d %H:%M")}
    _atomic_write(_meta_path(source),
                  lambda tmp: tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8"))
    return counts


def _commit(source: str, ctl: _Ctl, store, meta: dict) -> None:
    """Write the list — unless the download was stopped: then never (a
    stopped download must not write it after the stop)."""
    def write():
        _write_list(source, store, meta)
        ctl.committed = True
    ctl.publish(write)


_PROCESS_START = time.time()


def clean_leftovers() -> None:
    """At start-up, before requests are served (``main``): remove what a
    download that a restart interrupted left behind — never the work of a
    download this process runs (it is skipped, and only files older than
    this process are removed).  Kept source files stay."""
    for s in DOWNLOAD_SOURCES:
        job = _jobs.get(s)
        if job and job.get("state") not in _TERMINAL:
            continue
        try:
            _clean_leftovers(s, older_than=_PROCESS_START)
        except OSError:
            log.debug("could not remove download leftovers", exc_info=True)


def _clean_leftovers(source: str, older_than: float | None = None) -> None:
    """Remove what an interrupted download of ``source`` left behind (a
    ``dl-<source>-*`` work folder of an earlier version, a half-written list
    or a half-downloaded source file) — with ``older_than``, only what was
    last changed before that time."""
    d = _download_dir()
    src = _sources_dir(source)
    for old in (*d.glob(f"dl-{source}-*"), *d.glob(f".{source}.*.part"), *src.glob("*.part")):
        if older_than is not None:
            try:
                if old.stat().st_mtime >= older_than:
                    continue
            except OSError:
                continue
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)


def _tosec_newest(deadline: float, ctl: _Ctl) -> tuple[str, str]:
    """The newest TOSEC release that has a DAT pack: (its pack's URL, its date)."""
    site = _TOSEC_BASE + "/"
    page = _fetch(_TOSEC_HOME, deadline=deadline, max_bytes=_MAX_PAGE, site=site,
                  ctl=ctl).decode("utf-8", "replace")
    cats = sorted(set(re.findall(r'href="(/downloads/category/\d+-(\d{4}-\d{2}-\d{2}))"', page)),
                  key=lambda c: c[1], reverse=True)
    if not cats:
        raise RuntimeError("no TOSEC release found on tosecdev.org")
    url = date = None
    for href, rdate in cats[:3]:                 # the newest release with a DAT pack
        ctl.pause(_PAUSE_S)
        rel = _fetch(_TOSEC_BASE + href, deadline=deadline, max_bytes=_MAX_PAGE, site=site,
                     ctl=ctl).decode("utf-8", "replace")
        m = re.search(r'href="([^"]*\?download=[^"]*tosec-dat-pack-complete[^"]*)"', rel, re.I)
        if m:
            url, date = m.group(1).replace("&amp;", "&"), rdate
            break
    if not url:
        raise RuntimeError("no DAT pack found in the newest TOSEC releases")
    if url.startswith("/"):
        url = _TOSEC_BASE + url
    if not url.startswith(site):
        raise RuntimeError("the TOSEC page links its DAT pack elsewhere")
    return url, date


def _download_tosec(job: dict, ctl: _Ctl | None = None, local: bool = False) -> None:
    from soniqboom.core import game_titles_dat as dat
    ctl = ctl or _Ctl()
    kept_meta = _read_sources_meta("tosec")
    kept = _kept_tosec(kept_meta)
    src = _sources_dir("tosec")
    url = date = None
    how, why = "local", ""
    store = None
    if not (local and kept is not None):
        deadline = time.monotonic() + _TOSEC_DEADLINE_S
        job.update(state="finding", message="Looking for the newest TOSEC release")
        try:
            url, date = _tosec_newest(deadline, ctl)
        except _Stopped:
            raise
        except Exception as exc:                 # noqa: BLE001
            if kept is None:
                raise
            log.warning("TOSEC: %s — using the kept pack", exc)
            how, why = "offline", str(exc)
        if date is not None:
            if kept is not None and kept_meta.get("release") == date:
                how = "current"
            else:
                how = "downloaded"
                name = f"tosec-dat-pack-{date}.zip"
                src.mkdir(parents=True, exist_ok=True)
                for old in src.glob("*.part"):   # a transfer cut off earlier starts again
                    old.unlink(missing_ok=True)
                part = src / f".{name}.{ctl.tag}.part"
                job.update(state="downloading", message=f"Downloading the TOSEC DAT pack of {date}")
                try:
                    ctl.pause(_PAUSE_S)
                    _fetch(url, part, progress=lambda got, total: job.update(bytes=got, total=total),
                           timeout=300, deadline=deadline, site=_TOSEC_BASE + "/", ctl=ctl)
                    job.update(state="reading", message="Reading the TOSEC games lists")
                    try:
                        store = dat.titles_from_tosec_pack(str(part))
                    except Exception as exc:     # noqa: BLE001
                        raise RuntimeError(f"the downloaded TOSEC pack can't be read ({exc})") from None
                    if not store:
                        raise RuntimeError("the TOSEC pack held no games lists")
                    new_meta = {"release": date, "file": name, "size": part.stat().st_size,
                                "source": url, "downloaded": time.strftime("%Y-%m-%d %H:%M")}

                    def keep() -> None:
                        os.replace(part, src / name)
                        _write_sources_meta("tosec", new_meta)
                        for old in src.glob("tosec-dat-pack-*.zip"):   # only the newest is kept
                            if old.name != name:
                                old.unlink(missing_ok=True)
                    ctl.publish(keep)
                    _fsync_dir(src)
                finally:
                    part.unlink(missing_ok=True)     # can't be continued (no byte ranges)
                kept, kept_meta = src / name, new_meta
    release = kept_meta.get("release")
    listed = _read_meta("tosec")
    if (how == "current" and (_download_dir() / "tosec.tsv.gz").exists()
            and listed.get("release") == release):
        # The list is built from this very pack: nothing to do.
        job["changed"] = False
        job["message"] = f"TOSEC is up to date (release of {release}) — nothing was downloaded"
        return
    if store is None:
        job.update(state="reading", message="Reading the TOSEC games lists")
        try:
            store = dat.titles_from_tosec_pack(str(kept))
            bad = "it holds no games lists"
        except _BAD_FILE_ERRORS as exc:
            store, bad = None, str(exc) or type(exc).__name__
        except Exception as exc:                 # noqa: BLE001 — not the file: it stays
            raise RuntimeError(f"the kept TOSEC pack could not be read ({exc}) — it is kept; "
                               "try again") from None
        if not store:
            # A kept pack that is bad: dropped, so the next download fetches it anew.
            def drop() -> None:
                kept.unlink(missing_ok=True)
                (src / "files.json").unlink(missing_ok=True)
            ctl.publish(drop)
            raise RuntimeError(f"the kept TOSEC pack can't be used ({bad}) — it was removed; "
                               "the next download fetches it again")
    _commit("tosec", ctl, store, {"release": release, "source": kept_meta.get("source") or url})
    job["message"] = {
        "downloaded": f"Downloaded the TOSEC release of {release}",
        "current": f"Rebuilt from the kept TOSEC release of {release} (the newest)",
        "offline": f"No newer TOSEC release could be looked for ({why}) — the kept release of {release} is used",
        "local": f"Added from the kept TOSEC release of {release}",
    }[how]


def _same_zip_content(a: Path, b: Path) -> bool:
    """Two zips hold the same files — names, CRC-32s, sizes — whatever their
    timestamps: redump.org builds its zip anew for every request (the bytes
    differ each time, the DAT inside doesn't)."""
    import zipfile

    def content(p):
        with zipfile.ZipFile(p) as z:
            return sorted((i.filename, i.CRC, i.file_size) for i in z.infolist())
    try:
        return content(a) == content(b)
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def _download_redump(job: dict, ctl: _Ctl | None = None, local: bool = False) -> None:
    from soniqboom.core import game_titles_dat as dat
    ctl = ctl or _Ctl()
    systems = list(dat.REDUMP_SYSTEMS.items())
    src = _sources_dir("redump")
    meta = _read_sources_meta("redump")
    meta["systems"] = entries = {c: e for c, e in (meta.get("systems") or {}).items()
                                 if isinstance(e, dict)}
    kept = _kept_redump(meta)
    failed: list[str] = []
    stale: list[str] = []                        # not fetched now: an earlier kept file is used
    fetched: list[str] = []                      # new data by this download (or the stopped one it continues)
    same: list[str] = []                         # fetched: the same as the kept file

    def save_meta() -> None:
        ctl.publish(lambda: _write_sources_meta("redump", meta))

    if local and kept:
        failed = [c for c, _p in systems if c not in kept]
    else:
        last = meta.get("job") if isinstance(meta.get("job"), dict) else {}
        # A download that was stopped: this one continues it — what it had
        # fetched is not fetched again.
        resume_from = last.get("started") if last and not last.get("complete", True) else None
        meta["job"] = {"started": resume_from or time.time(), "complete": False}
        src.mkdir(parents=True, exist_ok=True)
        save_meta()
        for i, (code, plat) in enumerate(systems):
            ctl.check()
            job.update(state="downloading", bytes=i, total=len(systems),
                       message=f"Downloading Redump lists ({i + 1}/{len(systems)})")
            e = kept.get(code)
            if resume_from is not None and e and float(e.get("fetched") or 0) >= resume_from:
                fetched.append(code)
                continue                         # fetched by the stopped download
            part = src / f".{code}.zip.{ctl.tag}.part"
            try:
                _fetch(_REDUMP_URL.format(code=code), part, timeout=120, max_bytes=_MAX_REDUMP,
                       site=_REDUMP_SITE, deadline=time.monotonic() + _REDUMP_DEADLINE_S, ctl=ctl)
                got = dat.titles_from_redump_zip(str(part), plat)
                n = sum(len(t) for t in got.values()) if got else 0
                if not n:
                    raise ValueError("no games in the list")
                if e and _same_zip_content(part, src / f"{code}.zip"):
                    entries[code] = {**e, "fetched": time.time(), "titles": n}
                    same.append(code)
                    save_meta()
                else:
                    prev = int(e.get("titles") or 0) if e else 0
                    if prev and n < prev // 2:
                        raise ValueError(f"the new list has {n} games, the kept one {prev} — "
                                         "the kept one stays")
                    entry = {"size": part.stat().st_size, "fetched": time.time(), "titles": n}

                    def keep(code=code, entry=entry, part=part) -> None:
                        os.replace(part, src / f"{code}.zip")
                        entries[code] = entry
                        _write_sources_meta("redump", meta)
                    ctl.publish(keep)
                    _fsync_dir(src)
                    kept[code] = entry
                    fetched.append(code)
            except _Stopped:
                raise
            except Exception as exc:             # noqa: BLE001 — one system may be down
                if ctl.cancel.is_set():
                    raise _Stopped() from None
                log.warning("Redump %s: %s", code, exc)
                (stale if code in kept else failed).append(code)
            finally:
                part.unlink(missing_ok=True)
            ctl.pause(_PAUSE_S)
        if (len(same) == len(systems)
                and (_download_dir() / "redump.tsv.gz").exists()
                and not _read_meta("redump").get("systems_failed")):
            # Every list is the one the kept file holds: nothing to rebuild.
            # (A list built while a system could not be fetched is built
            # again — its "could not be fetched" note would stay.)
            meta["job"] = {**meta["job"], "complete": True}
            save_meta()
            job["changed"] = False
            job["message"] = "Redump is up to date — no list changed"
            return
    job.update(state="reading", message="Reading the Redump lists")
    store: dict[str, set[str]] = collections.defaultdict(set)
    for code, plat in systems:
        if code not in kept:
            continue
        damaged = True                           # a bad kept file (or no games in it): deleted
        try:
            got = dat.titles_from_redump_zip(str(src / f"{code}.zip"), plat)
        except _BAD_FILE_ERRORS as exc:
            log.warning("Redump %s: the kept list is damaged (%s) — it is removed", code, exc)
            got = None
        except Exception as exc:                 # noqa: BLE001 — not the file: it stays
            log.warning("Redump %s: the kept list could not be read (%s) — it is kept", code, exc)
            got, damaged = None, False
        if not got:
            if damaged:
                ctl.publish(lambda code=code: (src / f"{code}.zip").unlink(missing_ok=True))
                entries.pop(code, None)
            kept.pop(code, None)
            if code in stale:
                stale.remove(code)
            if code not in failed:
                failed.append(code)
            continue
        entries[code].setdefault("titles", sum(len(t) for t in got.values()))
        for p, titles in got.items():
            store[p].update(titles)
    ctl.check()
    if not store:
        raise RuntimeError("no Redump list could be downloaded")
    # A system with no kept file: the titles an earlier list had for it.
    earlier: list[str] = []
    if failed:
        previous = _read_list("redump")
        for code in failed:
            plat = dat.REDUMP_SYSTEMS[code]
            if previous.get(plat):
                store[plat].update(previous[plat])
                earlier.append(code)
    missing = stale + failed                     # not fetched this time
    _commit("redump", ctl, store, {"source": "http://redump.org/downloads/",
                                   "systems_failed": missing,
                                   "systems_kept": stale + earlier})
    if not local:
        meta["job"] = {**meta["job"], "complete": True}
    try:
        _write_sources_meta("redump", meta)      # after the list: a stop can't undo that
    except OSError:
        log.debug("could not record the Redump files", exc_info=True)
    names = dat.REDUMP_NAMES
    if local:
        job["message"] = (f"Added from the kept Redump lists ({len(systems) - len(missing)} of "
                          f"{len(systems)} systems)")
    elif not missing:
        job["message"] = "Downloaded"
    elif not fetched and not same:
        job["message"] = (f"redump.org could not be reached — the kept lists of "
                          f"{len(systems) - len(failed)} of {len(systems)} systems are used; "
                          "Update again later")
    else:
        job["message"] = (f"Downloaded, except {len(missing)} of {len(systems)} systems "
                          f"({', '.join(names.get(c, c) for c in missing)})"
                          + (f" — the earlier titles of {', '.join(names.get(c, c) for c in stale + earlier)} are kept"
                             if stale or earlier else "")
                          + "; Update again later for the rest")


def files_info(source: str) -> dict | None:
    """What ``source``'s kept downloaded files hold: ``bytes``, and TOSEC's
    ``release`` / Redump's ``systems`` (of ``of``) and ``resumable`` (a
    stopped download to continue, ``done`` of its systems fetched) — None
    when none are kept."""
    from soniqboom.core import game_titles_dat as dat
    meta = _read_sources_meta(source)
    if source == "tosec":
        pack = _kept_tosec(meta)
        if pack is None:
            return None
        return {"bytes": meta["size"], "release": meta.get("release"), "resumable": False}
    kept = _kept_redump(meta)
    if not kept:
        return None
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    resumable = bool(job) and not job.get("complete", True)
    done = sum(1 for e in kept.values()
               if resumable and float(e.get("fetched") or 0) >= float(job.get("started") or 0))
    return {"bytes": sum(e["size"] for e in kept.values()), "systems": len(kept),
            "of": len(dat.REDUMP_SYSTEMS), "resumable": resumable, "done": done}


def start_download(source: str, local: bool = False) -> dict:
    """Start downloading ``source`` ("tosec" / "redump") in the background —
    with ``local``, build the list from the kept files alone (no network)
    when there are any; the passes that use the lists run again when it is
    in (``_lists_changed``).  A second call while it runs returns the running
    job.  Returns the job state.  The download runs in its own daemon
    thread: a stop never waits for it (it can't write anything once
    stopped), and the app's exit doesn't either."""
    if source not in DOWNLOAD_SOURCES:
        raise ValueError(source)
    job = _jobs.get(source)
    if job and job.get("state") not in _TERMINAL:
        return job
    from soniqboom.core import scanner
    job = {"state": "starting", "message": "Starting", "bytes": 0, "total": 0,
           "started": time.time()}
    ctl = _Ctl(epoch=scanner._run_epoch)
    _jobs[source] = job
    _ctls[source] = ctl
    fn = _download_tosec if source == "tosec" else _download_redump
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _settle(res) -> None:
        if not fut.done():
            fut.set_result(res)

    def _thread() -> None:
        try:
            fn(job, ctl, local=local)
            res = None
        except BaseException as exc:             # noqa: BLE001
            res = exc
        try:
            loop.call_soon_threadsafe(_settle, res)
        except RuntimeError:                     # the loop is closed: the server stopped
            pass

    async def _run() -> None:
        try:
            res = await fut
        except asyncio.CancelledError:
            # The event loop is closing (the server stops) — stop the thread too.
            _stop(source, ctl, "stop")
            raise
        if res is None:
            job["state"] = "done"
            if job.get("changed", True):
                _lists_changed()
        elif not isinstance(res, _Stopped) and not ctl.cancel.is_set():
            log.warning("%s download failed: %s", SOURCE_NAMES[source], res)
            job.update(state="error", message=f"The download failed: {res}")
    ctl.thread = threading.Thread(target=_thread, daemon=True, name=f"download-{source}")
    ctl.thread.start()
    _job_tasks[source] = loop.create_task(_run())
    return job


def _stopped_message(source: str, by: str) -> str:
    """What a stopped download's job says — ``by`` "you" (the Stop button)
    or "stop" (the server stopped)."""
    head = "Stopped" if by == "you" else "Interrupted when SoniqBoom stopped"
    if source == "redump":
        info = files_info("redump") or {}
        done, of = int(info.get("done") or 0), int(info.get("of") or 0)
        if info.get("resumable") and of:
            return (f"{head} after {done} of {of} systems — continuing the download "
                    f"fetches the other {of - done}")
        return f"{head} — the next download fetches the lists"
    return f"{head} — the next download starts it again (a TOSEC download can't be continued)"


def _stop(source: str, ctl: _Ctl, by: str) -> bool:
    """Stop the download ``ctl`` controls: it ends at once ("interrupted") —
    a transfer in progress is cut off, the thread stops at its next step and
    writes nothing more; the files it had finished stay kept.  False when
    it had already finished, or its list is being written (it completes)."""
    if not ctl.commit.acquire(timeout=5):
        return False
    try:
        if ctl.committed:
            return False
        ctl.stop()
    finally:
        ctl.commit.release()
    job = _jobs.get(source)
    if _ctls.get(source) is ctl and job is not None and job.get("state") not in _TERMINAL:
        _jobs[source] = {"state": "interrupted", "message": _stopped_message(source, by),
                         "started": job.get("started")}
    return True


def cancel_download(source: str, by: str = "you") -> bool:
    """Stop ``source``'s running download (``_stop``); False when none runs."""
    if source not in DOWNLOAD_SOURCES:
        raise ValueError(source)
    job, ctl = _jobs.get(source), _ctls.get(source)
    if not job or job.get("state") in _TERMINAL or ctl is None:
        return False
    return _stop(source, ctl, by)


def cancel_downloads(epoch: int | None = None) -> list[str]:
    """At a stop: stop every download (of server run ``epoch``, when given —
    a previous run's stop that ends late leaves this run's alone); the
    sources stopped."""
    out = []
    for s in DOWNLOAD_SOURCES:
        ctl = _ctls.get(s)
        if ctl is not None and (epoch is None or ctl.epoch == epoch) and cancel_download(s, "stop"):
            out.append(s)
    return out


def wait_for_downloads(timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for every download thread to end;
    True when none runs."""
    deadline = time.monotonic() + timeout
    for ctl in list(_ctls.values()):
        t = ctl.thread
        if t is not None:
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                return False
    return True


def remove_download(source: str) -> bool:
    """Delete a downloaded list; the passes that use the lists run again
    (``_lists_changed``).  True when one was there.  The kept source files
    stay (adding the list again uses them; ``delete_files`` removes them).
    Raises ``DownloadRunning`` while it is being downloaded (the download
    would write it back)."""
    if source not in DOWNLOAD_SOURCES:
        raise ValueError(source)
    job = _jobs.get(source)
    if job and job.get("state") not in _TERMINAL:
        raise DownloadRunning(source)
    d = _download_dir()
    had = False
    _clean_leftovers(source)
    for p in (d / f"{source}.tsv.gz", _meta_path(source)):
        try:
            p.unlink()
            had = True
        except FileNotFoundError:
            pass
    _jobs.pop(source, None)
    if had:
        _lists_changed()
    return had


def delete_files(source: str) -> int:
    """Delete ``source``'s kept downloaded files (the list stays) — only the
    files this module keeps there; the bytes freed.  Raises
    ``DownloadRunning`` while it is being downloaded."""
    from soniqboom.core import game_titles_dat as dat
    if source not in DOWNLOAD_SOURCES:
        raise ValueError(source)
    job = _jobs.get(source)
    if job and job.get("state") not in _TERMINAL:
        raise DownloadRunning(source)
    d = _sources_dir(source)
    if not d.is_dir():
        return 0
    names = {f"{c}.zip" for c in dat.REDUMP_SYSTEMS} if source == "redump" else set()
    freed = 0
    for f in d.iterdir():
        n = f.name
        if not (n == "files.json" or n.endswith(".part") or n in names
                or (source == "tosec" and _TOSEC_PACK_RE.match(n))):
            continue
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass
    return freed


def _lists_changed() -> None:
    """A downloaded list came or went: this module's pass runs, and the
    scene-enrichment runner re-spells the Modland file-name guesses (its skip
    signature includes the lists)."""
    schedule()
    try:
        from soniqboom.core import scanner
        scanner._spawn_scene_autoapply()
    except Exception:                            # noqa: BLE001
        log.debug("could not queue scene enrichment after a list change", exc_info=True)
