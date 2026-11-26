# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One identity per scene musician.

The library credits one musician many ways: a SID's own author ("Ben
Daglish"), HVSC's folder ("Daglish_Ben", listed "Daglish Ben" until a play
reads the file), Modland's folder (its SNDH mirror: "Daglish_Ben"), the
diacritics a folder name spells out ("Chris Hülsbeck" / "Huelsbeck_Chris"),
a handle beside a real name ("Thomas Mogensen (DRAX)"), a collaboration
("Rob Hubbard & Ben Daglish").  This module derives ONE identity per musician
from the credits as stored — nothing is rewritten — and serves the Scene →
Composers list, the composer page, the Artists view's scene rows and the
``composer:`` search operator.

What a *scene composer* is: a name credited on a **scene tune** — a track
of a retro format (``retro.is_retro_format``, MIDI excepted) or of an online
scene collection (HVSC, Modland, scene.org, Aminet) — by its artist field or
its composer field.  A mainstream composer tag (MP3 / FLAC …) never makes one.
Credit lists on scene tunes are split ("A & B", "A; B", "A + B" always; "A/B"
and "A, B" only when every part is a name the library credits on its own —
"Laxity/Vibrants" names a group after the slash and stays one credit unless
"Vibrants" is a person here too).

Two spellings are one musician when (each rule is a fact about the names, never
a guess from popularity):

1. they are the same words once case, accents, punctuation and underscores
   are put aside — HVSC's transliteration included (ü→ue, ø→oe, å→aa …:
   "Hülsbeck" = "Huelsbeck"), or the same letters with the spaces and dashes
   left out ("4-Mat" = "4mat", "Lizard King" = "Lizardking"; four letters or
   more, and no word shorter than three letters: "Jo Hansen" ≠ "Johansen");
2. one is a folder name (HVSC / Modland: ``Last_First``, an underscore, or an
   unplayed HVSC tune's folder credit) and the other has the same words in
   another order, initials aside ("Daglish_Ben" = "Ben Daglish", "Judd
   Stephen" = "Stephen L. Judd") — HVSC's own rule (``online_hvsc``);
3. a credit names both ("Stephen L. Judd (Wyndex)": the real name and the
   handle beside it — one word of three letters or more, no remark like
   "(remix)") AND there is evidence: HVSC files the tunes in that handle's
   folder (a group member's own folder inside a group's), or Demozoo gives
   both names to one person.  Never a group (the library's scene groups,
   Demozoo's), never a handle written beside two different names ("Laxity
   (Vibrants)" + "JCH (Vibrants)");
4. the local Demozoo index (``demozoo.identity_rows``) gives both to the same
   person (nick, real name, nick variant of four letters or more), and to no
   one else.  A group of spellings Demozoo splits between two people takes no
   Demozoo link at all.

An identity's tracks: every scene tune credited by one of its names, and every
track of any format whose artist tag is one of them when that name has two
words or more or tags scene tunes only (his CD albums; a one-word handle never
takes a band's albums — "Moby").  Its name: the Demozoo handle when Demozoo
knows the musician, else the spelling most tracks carry that is no folder name
(a folder name's header order, "Daglish_Ben" → "Ben Daglish", is searched and
asked of Demozoo, never shown on a guess).
"""
from __future__ import annotations

import bisect
import heapq
import logging
import re
import threading
import unicodedata
import weakref
from collections import Counter

log = logging.getLogger(__name__)

# The store's multi-artist separator (``metadata.NAME_SEP``).
NAME_SEP = " • "

# HVSC's folder spellings (``online_hvsc._TRANSLIT``: checked against the real
# index) and what NFKD can't take apart.  (Kept here too: this module is
# imported by the Demozoo index build, which must not import the online
# collections.)
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe",
                           "å": "aa", "œ": "oe", "ð": "d", "þ": "th", "ł": "l"})
_UNACCENTED = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th",
                             "ł": "l", "đ": "d", "ı": "i"})
_WORD_RE = re.compile(r"[^\W_]+")
# A bracketed part that is a remark, no handle ("Rob Hubbard (remix)").
_NOT_HANDLES = frozenset({"remix", "cover", "arrangement", "arr", "edit", "version", "intro",
                          "demo", "tune", "music", "sid", "conversion", "rip", "unknown",
                          "original", "remake", "remastered", "live", "mix", "rmx",
                          "unreleased", "unfinished", "preview", "wip", "game", "ingame"})
# What a credit holds when nobody is named.
_PLACEHOLDERS = frozenset({
    "", "?", "??", "???", "<?>", "-", "--", "n/a", "na", "none", "unknown", "unknown artist",
    "unknown composer", "unknown_composer", "- unknown", "-unknown", "_unknown", "unkown",
    "various", "various artists", "va", "v a", "anonymous", "anon", "no name", "noname",
    "not known", "unnamed", "untitled", "artist", "composer", "author", "<unknown>",
})
# Credit-list separators that always split on a scene tune, and the guarded
# ones (split only when every part is a name the library credits alone).
_SPLIT_ALWAYS = re.compile(r"\s+&\s+|\s*;\s*|\s+\+\s+|\s+vs\.?\s+|\s+feat\.?\s+|\s+ft\.\s+"
                           r"|\s+/\s+|\s*•\s*", re.I)
_SPLIT_GUARDED = re.compile(r"\s*[/,]\s*")
_PAREN_RE = re.compile(r"^(?P<name>[^()]*?)\s*\((?P<inner>[^()]+)\)\s*(?P<rest>.*)$")
# Squashed spellings ("4mat") need this many characters to join.
_SQUASH_MIN = 4

_fold_memo: dict[str, tuple] = {}
_FOLD_MEMO_CAP = 400_000


def _plain(v: str) -> str:
    out = []
    for ch in unicodedata.normalize("NFKD", v):
        if not unicodedata.combining(ch):
            out.append(ch)
    return "".join(out)


def spellings(name: str) -> tuple:
    """The word lists ``name`` reads as in plain letters: accents dropped,
    and HVSC-transliterated (both the same for a plain-ASCII name) —
    lower case, punctuation and underscores as spaces.  Memoised."""
    hit = _fold_memo.get(name)
    if hit is not None:
        return hit
    low = (name or "").casefold()
    out: list[tuple] = []
    for table in (_UNACCENTED, _TRANSLIT):
        words = tuple(_WORD_RE.findall(_plain(low.translate(table))))
        if words and words not in out:
            out.append(words)
    res = tuple(out)
    if len(_fold_memo) >= _FOLD_MEMO_CAP:
        _fold_memo.clear()
    _fold_memo[name] = res
    return res


def ordered_keys(name: str) -> list[str]:
    """``name``'s exact keys (rule 1): its words in order, per spelling."""
    return [" ".join(w) for w in spellings(name)]


def squash_keys(name: str) -> list[str]:
    """``name``'s squashed keys ("4-Mat" → "4mat"), four letters or more."""
    out = []
    for w in spellings(name):
        s = "".join(w)
        if len(s) >= _SQUASH_MIN and s not in out:
            out.append(s)
    return out


def order_free_key(name: str) -> str | None:
    """Rule 2's key: the words sorted, single-letter initials left out — two
    words or more, else None."""
    sp = spellings(name)
    if not sp:
        return None
    words = sorted(w for w in sp[-1] if len(w) > 1 or w.isdigit())
    return " ".join(words) if len(words) >= 2 else None


def squash_ok(name: str) -> bool:
    """May ``name`` join by its squashed spelling?  One word ("4mat"), or
    words of three letters or more / numbers ("Lizard King", "4-Mat") — not
    "Jo Hansen" = "Johansen"."""
    sp = spellings(name)
    if not sp:
        return False
    words = sp[0]
    return len(words) == 1 or all(len(w) >= 3 or w.isdigit() for w in words)


def rotated(name: str) -> str | None:
    """A folder name's ``Last_First[_Middle]`` (HVSC's convention for real
    names) read in the order a header writes it ("Daglish_Ben" → "Ben
    Daglish", "Oeie_Karl_Bjoernar" → "Karl Bjoernar Oeie"); None for one
    word.  Only a name to search by and to ask Demozoo — never shown unless
    Demozoo confirms it."""
    words = (name or "").replace("_", " ").split()
    return " ".join(words[1:] + words[:1]) if len(words) >= 2 else None


_MUSICIANS_RE = re.compile(r"(?:^|[/:])MUSICIANS/([^/:]+(?:/[^/:]+)*)$")
_NOT_MEMBER_DIRS = frozenset({"worktunes", "unreleased"})


def hvsc_folder(path: str | None) -> str | None:
    """The HVSC composer folder a tune's path files it under — a group
    member's own folder inside a group's (``MUSICIANS/V/Vibrants/Laxity/…``
    → "Laxity", as ``online_hvsc.composer_dir``), not ``Worktunes``; None
    outside ``MUSICIANS``."""
    m = _MUSICIANS_RE.search((path or "").replace("\\", "/"))
    if not m:
        return None
    parts = m.group(1).split("/")
    if len(parts) < 3:
        return None
    if len(parts) >= 4:
        sub = parts[2].casefold()
        if sub and sub not in _NOT_MEMBER_DIRS and not sub.endswith("_original"):
            return parts[2]
    return parts[1]


def index_keys(name: str) -> list[str]:
    """Every key a Demozoo name is looked up by (ordered + squashed)."""
    out = []
    for k in (*ordered_keys(name), *squash_keys(name)):
        if k and k not in out:
            out.append(k)
    return out


def word_count(name: str) -> int:
    sp = spellings(name)
    return len(sp[0]) if sp else 0


_ph_memo: dict[str, bool] = {}


def is_placeholder(name: str) -> bool:
    """A credit naming nobody ("<?>", "_unknown", "Various" …).  Memoised."""
    hit = _ph_memo.get(name)
    if hit is None:
        n = " ".join((name or "").replace("_", " ").split()).casefold()
        hit = n in _PLACEHOLDERS or not spellings(name)
        if len(_ph_memo) >= _FOLD_MEMO_CAP:
            _ph_memo.clear()
        _ph_memo[name] = hit
    return hit


def handle_like(inner: str) -> bool:
    """Is a bracketed part a scene handle (HVSC's "Real Name (Handle)": one
    word, no remark, no number) rather than a remark or a group?"""
    w = (inner or "").strip()
    if not w or len(w.split()) != 1 or w.isdigit():
        return False
    return w.casefold().strip(".") not in _NOT_HANDLES


def split_parenthetical(name: str) -> tuple[str, str | None]:
    """``"Stephen L. Judd (Wyndex)"`` → ``("Stephen L. Judd", "Wyndex")`` —
    the name and what its brackets hold (a handle or a remark: the caller
    tells them apart, ``handle_like``); no brackets → ``(name, None)``.  A
    name that is only a bracket keeps it."""
    m = _PAREN_RE.match(name or "")
    if not m:
        return (name or "").strip(), None
    outer = " ".join(f"{m.group('name')} {m.group('rest')}".split())
    inner = m.group("inner").strip()
    if not outer:
        return inner, None
    return outer, inner


_parts_memo: dict[str, tuple] = {}


def credit_parts(raw: str) -> tuple:
    """A scene tune's credit split at the separators that always split
    (``_SPLIT_ALWAYS``); each part stripped, placeholders left out.
    Memoised (a tuple of strings: no work for the cyclic GC)."""
    hit = _parts_memo.get(raw)
    if hit is None:
        parts = [p.strip() for p in _SPLIT_ALWAYS.split(raw or "")]
        hit = tuple(p for p in parts if p and not is_placeholder(p))
        if len(_parts_memo) >= _FOLD_MEMO_CAP:
            _parts_memo.clear()
        _parts_memo[raw] = hit
    return hit


def guarded_parts(part: str) -> list[str] | None:
    """A credit part's pieces at "/" or "," — None when it holds neither."""
    if "/" not in part and "," not in part:
        return None
    pieces = [p.strip() for p in _SPLIT_GUARDED.split(part)]
    pieces = [p for p in pieces if p]
    return pieces if len(pieces) >= 2 else None


# ── Scene tunes ──────────────────────────────────────────────────────────────

_scene_fmt_memo: dict[str, bool] = {}


def scene_format(fmt: str | None) -> bool:
    """A retro / scene format (``retro.is_retro_format``), MIDI excepted —
    MIDI files are mostly karaoke and pop covers."""
    if not fmt:
        return False
    hit = _scene_fmt_memo.get(fmt)
    if hit is None:
        from soniqboom.core.retro import chip_family, is_retro_format
        hit = bool(is_retro_format(fmt)) and chip_family(fmt) != "midi"
        _scene_fmt_memo[fmt] = hit
    return hit


def is_scene_track(t: dict | None) -> bool:
    """Is ``t`` a scene tune (``build_from``'s scene ids, one track): a scene
    format, or a track of an online collection?"""
    if not t:
        return False
    if scene_format(t.get("format")):
        return True
    from soniqboom.core import catalogue
    return catalogue.root_of(t.get("path")) is not None


# ── Platform order (the composer page's groups) ──────────────────────────────

_plat_memo: dict[str, str] = {}
OTHER_PLATFORM = "Other formats"


def platform_of(fmt: str | None) -> str:
    """The composer page's group of a format: its platform (``genre_derive.
    platform_genre``: SID, Amiga, Atari ST, NES …), "PC trackers" for the
    sample trackers of no platform, else "Other formats"."""
    f = fmt or ""
    hit = _plat_memo.get(f)
    if hit is None:
        from soniqboom.core.genre_derive import platform_genre
        from soniqboom.core.retro import chip_family
        hit = platform_genre(f) or ""
        if not hit:
            fam = chip_family(f)
            hit = ("PC trackers" if fam == "tracker" else
                   (f if fam else OTHER_PLATFORM))
        _plat_memo[f] = hit
    return hit


def _year_of(t: dict) -> int | None:
    y = t.get("year")
    try:
        y = int(y)
    except (TypeError, ValueError):
        return None
    return y if 1950 <= y <= 2100 else None


def page_order(store, ids) -> list[str]:
    """A composer's tracks in the page's order: platform by size (the
    biggest first; "Other formats" last), then format, then year (unknown
    last), title, id."""
    tracks = store._tracks
    rows = []
    sizes: Counter = Counter()
    for i in ids:
        t = tracks.get(i)
        if t is None:
            continue
        p = platform_of(t.get("format"))
        sizes[p] += 1
        rows.append((i, t, p))

    def key(r):
        i, t, p = r
        y = _year_of(t)
        return (p == OTHER_PLATFORM, -sizes[p], p.casefold(), (t.get("format") or "").casefold(),
                y is None, y or 0, (t.get("title") or "").casefold(), i)
    rows.sort(key=key)
    return [r[0] for r in rows]


def platform_summary(store, ids) -> list[dict]:
    """``[{platform, count, formats: [..], year_min, year_max}]`` in page
    order."""
    tracks = store._tracks
    acc: dict[str, dict] = {}
    for i in ids:
        t = tracks.get(i)
        if t is None:
            continue
        p = platform_of(t.get("format"))
        a = acc.setdefault(p, {"platform": p, "count": 0, "formats": Counter(),
                               "year_min": None, "year_max": None})
        a["count"] += 1
        if t.get("format"):
            a["formats"][t["format"]] += 1
        y = _year_of(t)
        if y is not None:
            a["year_min"] = y if a["year_min"] is None else min(a["year_min"], y)
            a["year_max"] = y if a["year_max"] is None else max(a["year_max"], y)
    out = sorted(acc.values(), key=lambda a: (a["platform"] == OTHER_PLATFORM, -a["count"],
                                              a["platform"].casefold()))
    for a in out:
        a["formats"] = [f for f, _n in a["formats"].most_common()]
    return out


# ── The index ────────────────────────────────────────────────────────────────

class Identity:
    """One musician.  Few GC-tracked parts (``ids`` and the object itself):
    the names are tuples of strings, which the cyclic GC untracks."""
    __slots__ = ("key", "name", "ids", "n_scene", "aliases", "artist_keys", "releaser_id",
                 "real_name", "names")

    def __init__(self, key, name, ids, n_scene, aliases, artist_keys, releaser_id, real_name,
                 names):
        self.key = key                       # ``store._facet_key(name)`` — the list's key
        self.name = name                     # the display name
        self.ids = ids                       # every track of the musician (a set)
        self.n_scene = n_scene               # how many of them are scene tunes
        self.aliases = aliases               # the library's spellings, most tracks first
        self.artist_keys = artist_keys       # the ``_tag_artist`` keys folded in
        self.releaser_id = releaser_id
        self.real_name = real_name
        self.names = names                   # every name it is known by (search)


_versions = __import__("itertools").count(1)


class ComposerIndex:
    """The identities of one library state (``build_from``); ``version``
    tells one build from the next (the list caches key on it)."""

    def __init__(self) -> None:
        self.version = next(_versions)
        self.by_key: dict[str, Identity] = {}
        self.by_name: dict[str, str] = {}          # ordered key of any name → identity key
        self.by_artist: dict[str, str] = {}        # ``_tag_artist`` key → identity key
        self.words: dict[str, tuple] = {}          # a word of a name → identity keys
        self.sorted_words: list[str] = []          # (bisected for a word's start)
        self.demozoo_sig = None
        self._sets: dict | None = None

    def facet_sets(self) -> dict:
        """``{key: (name, ids)}`` — the Scene → Composers rows (one object
        per index: ``aggregate_facet``'s memo names it)."""
        if self._sets is None:
            self._sets = {k: (i.name, i.ids) for k, i in self.by_key.items()}
        return self._sets

    def find(self, name: str) -> Identity | None:
        """The identity a name (any of its spellings or Demozoo names) or a
        list key names, or None."""
        if not name:
            return None
        from soniqboom.core.store import _facet_key
        hit = self.by_key.get(_facet_key(name))
        if hit is not None:
            return hit
        for k in ordered_keys(name):
            ik = self.by_name.get(k)
            if ik is not None:
                return self.by_key.get(ik)
        return None

    def _candidates(self, qs) -> set:
        """The identities a name word of which the query's first word is
        (several words) or starts (one word) — then checked whole."""
        import bisect
        out: set = set()
        for qw in qs:
            if not qw:
                continue
            if len(qw) > 1:
                out.update(self.words.get(qw[0], ()))
                continue
            w = self.sorted_words
            i = bisect.bisect_left(w, qw[0])
            while i < len(w) and w[i].startswith(qw[0]):
                out.update(self.words.get(w[i], ()))
                i += 1
        return out

    def matching(self, q: str) -> list[Identity]:
        """The identities with a name in which the words of ``q`` start
        consecutive words (accents and case aside)."""
        qs = spellings(q)
        if not qs:
            return []
        out = []
        for k in self._candidates(qs):
            ident = self.by_key.get(k)
            if ident is not None and any(_words_start(nm, qs) for nm in ident.names):
                out.append(ident)
        return out

    def search(self, q: str, limit: int = 8) -> list[Identity]:
        """``matching`` — most tracks first."""
        return sorted(self.matching(q), key=lambda i: (-len(i.ids), i.name.casefold()))[:limit]

    def word_hits(self, value: str) -> set:
        """``composer:value`` — the tracks of every identity ``matching`` it."""
        out: set = set()
        for ident in self.matching(value):
            out |= ident.ids
        return out


def _words_start(name: str, qs) -> bool:
    """Do the query's words (``qs``: its spellings) start consecutive words
    of ``name``, the last one perhaps a word's start?"""
    for nw in spellings(name):
        for qw in qs:
            n = len(qw)
            if n == 0 or n > len(nw):
                continue
            for s in range(len(nw) - n + 1):
                if all(nw[s + j] == qw[j] for j in range(n - 1)) and nw[s + n - 1].startswith(qw[-1]):
                    return True
    return False


def _demozoo_sig():
    try:
        from soniqboom.core import demozoo
        return demozoo._index_sig()
    except Exception:                                   # noqa: BLE001
        return None


# Demozoo lookups of earlier builds: key → [(rid, kind)] (one index file).
_dz_memo: dict[str, tuple] = {}
_dz_memo_sig = None
_dz_lock = threading.Lock()


def _demozoo_rows(keys) -> dict:
    """``{key: ((releaser_id, kind), …)}`` for ``keys`` from the Demozoo
    index (memoised per index file)."""
    global _dz_memo_sig
    sig = _demozoo_sig()
    if sig is None:
        return {}
    with _dz_lock:
        if sig != _dz_memo_sig:
            _dz_memo.clear()
            _dz_memo_sig = sig
        want = [k for k in keys if k not in _dz_memo]
    if want:
        try:
            from soniqboom.core import demozoo
            got = demozoo.identity_rows(want)
        except Exception:                               # noqa: BLE001
            log.debug("Demozoo identity lookup failed", exc_info=True)
            got = None
        if got is not None:
            with _dz_lock:
                for k in want:
                    _dz_memo[k] = tuple(got.get(k, ()))
    return _dz_memo


def _demozoo_people(rids) -> dict:
    try:
        from soniqboom.core import demozoo
        return demozoo.identity_details(rids) or {}
    except Exception:                                   # noqa: BLE001
        log.debug("Demozoo identity details failed", exc_info=True)
        return {}


class Snapshot:
    """What a build reads of the store, taken on the store's own thread in
    a few milliseconds: the tag KEYS (lists of strings), the live index
    dicts, two id sets of its own.  ``build_from`` (a worker thread) reads
    each live set by one C-level operation (``&``, ``|=``, ``min``,
    ``isdisjoint`` — atomic under the GIL), so a write meanwhile can't break
    it; the store's stamp, checked after, says whether it missed one."""
    __slots__ = ("art", "comp", "fmt", "art_keys", "comp_keys", "fmt_keys", "collection",
                 "hvsc", "groups", "tracks", "demozoo_sig")


def snapshot(store) -> Snapshot:
    """``Snapshot`` of ``store`` (on its thread)."""
    sn = Snapshot()
    sn.art, sn.comp, sn.fmt = store._tag_artist, store._tag_composer, store._tag_format
    sn.art_keys, sn.comp_keys, sn.fmt_keys = list(sn.art), list(sn.comp), list(sn.fmt)
    try:
        sn.collection = store._source_ids("collection")
        sn.hvsc = store._source_ids("hvsc")
    except Exception:                                   # noqa: BLE001
        log.debug("collection ids unavailable", exc_info=True)
        sn.collection, sn.hvsc = set(), set()
    sn.groups = [g for g, v in list(store._tag_scene_group.items()) if v]
    sn.tracks = store._tracks
    sn.demozoo_sig = _demozoo_sig()
    return sn


def build(store, *, use_demozoo: bool = True) -> ComposerIndex:
    """The identities of ``store`` now, built on the calling thread."""
    return build_from(snapshot(store), use_demozoo=use_demozoo)


# ``build_from`` sleeps this long (s) every ``_YIELD_EVERY`` steps of its main
# loops, so a request the event loop serves while a rebuild runs keeps most of
# the CPU.  Measured at 510K tracks (an Artists aggregation + fold, 131–137 ms
# alone): 286–318 ms during a rebuild without it, 144–172 ms with it.  A real
# sleep — ``sleep(0)`` hands the GIL straight back and starved the loop (170 ms
# pauses).  0: never.
_YIELD_SLEEP = 0.001
_YIELD_EVERY = 400


def _any_id(tids):
    try:
        return next(iter(tids))
    except (StopIteration, RuntimeError):               # (emptied / written meanwhile)
        return None


def build_from(snap: Snapshot, *, use_demozoo: bool = True) -> ComposerIndex:
    """The identities of a ``Snapshot`` (see the module's docstring) — safe
    in a worker thread.  Keeps the cyclic GC's work small: the nodes are
    parallel lists of ints / strings, the credits tuples of strings and ints
    (untracked), no copied set lives past its line.

    Two passes: the scene credits make the musicians (the name rules join
    their spellings); then each artist tag of no scene tune joins the
    musician it is a spelling of (rules 1 and 2 only — his CD albums), read
    by its own folded key."""
    # (The GIL: a 1 ms nap every ``_YIELD_EVERY`` steps of the main loops
    # (``breathe``) lets a busy event loop run; no ``time.sleep(0)`` — it hands
    # the GIL straight back and starves the loop — and no long C calls: a sort
    # with a C key holds the GIL for its whole run.)
    from soniqboom.core.store import _facet_key, name_display
    import time as _time
    idx = ComposerIndex()
    idx.demozoo_sig = snap.demozoo_sig if use_demozoo else None
    tracks, art, comp = snap.tracks, snap.art, snap.comp
    nap = _YIELD_SLEEP
    step = [0]

    def breathe() -> None:
        if nap:
            step[0] += 1
            if step[0] % _YIELD_EVERY == 0:
                _time.sleep(nap)
    acc = set(snap.collection)
    for k in snap.fmt_keys:
        tids = snap.fmt.get(k)
        t = tracks.get(_any_id(tids)) if tids else None
        if t and scene_format(t.get("format")):
            acc |= tids
    # (a dict of strings — its keys view intersects like a set — is no work
    # for the cyclic GC, which a set of 300K ids would be at every full pass)
    scene_all = dict.fromkeys(acc).keys()
    del acc
    hvsc_ids = snap.hvsc
    groups_known: set = set()
    for g in snap.groups:
        groups_known.update(ordered_keys(g))

    # The nodes: index → ordered key, parent (union-find), folder credit?
    nkey: list = []
    nidx: dict = {}
    parent: list = []
    folder: list = []
    # The credits: (node, spelling, weight, field, tag key, the tag's every
    # track?) — "a" artist, "c" composer, "-" a name it is only known by.
    entries: list = []

    def node(spelling: str) -> int:
        ks = ordered_keys(spelling)
        if not ks:
            return -1
        i = nidx.get(ks[0])
        if i is None:
            i = nidx[ks[0]] = len(nkey)
            nkey.append(ks[0])
            parent.append(i)
            folder.append(False)
        return i

    def find(i: int) -> int:
        r = i
        while parent[r] != r:
            r = parent[r]
        while parent[i] != r:
            parent[i], i = r, parent[i]
        return r

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if nkey[ra] > nkey[rb]:
                ra, rb = rb, ra
            parent[rb] = ra

    def spelling_of(tids, fld: str, key: str) -> str:
        try:
            t = tracks.get(min(tids)) or {}
        except (ValueError, RuntimeError):
            t = {}
        return name_display(t.get(fld), key) or key

    # 1. The scene credits.
    credits: list = []          # (raw, weight, field, key, folder?, whole?, folders)
    rest: list = []             # artist tag keys of no scene tune
    for fld, tag, keys in (("a", art, snap.art_keys), ("c", comp, snap.comp_keys)):
        for key in keys:
            breathe()
            tids = tag.get(key)
            if not tids or NAME_SEP in key:
                continue                     # (a joined "A • B": its parts are keys too)
            sc = tids & scene_all
            if not sc:
                if fld == "a":
                    rest.append(key)
                continue                     # (a mainstream composer tag: never)
            raw = spelling_of(sc, "artist" if fld == "a" else "composer", key)
            if is_placeholder(raw):
                continue
            fo = "_" in raw
            if not fo and hvsc_ids:
                hv = sc & hvsc_ids
                if hv:
                    # (an unplayed HVSC tune's credit is its folder's name)
                    fo = any(not ((tracks.get(i) or {}).get("mtime") or 0) > 0
                             for i in (hv if len(hv) <= 3 else heapq.nsmallest(3, hv)))
            # (a bracketed credit: the HVSC folders its tunes are filed under —
            # the evidence that the bracket is the composer's handle)
            mf: tuple = ()
            if "(" in raw:
                mf = tuple({k for i in (sc if len(sc) <= 3 else heapq.nsmallest(3, sc))
                            for k in ordered_keys(hvsc_folder((tracks.get(i) or {}).get("path"))
                                                  or "")[:1]})
            credits.append((raw, len(sc), fld, key, fo, len(sc) == len(tids), mf))
            del sc

    # Names credited alone (the guard of "A/B" and "A, B").
    alone = set()
    for raw, _w, _f, _k, _fo, _wh, _mf in credits:
        if len(credit_parts(raw)) == 1 and guarded_parts(raw) is None:
            alone.update(ordered_keys(split_parenthetical(raw)[0]))
    alone -= groups_known

    def persons(raw: str) -> list:
        out = []
        for part in credit_parts(raw):
            pieces = guarded_parts(part)
            if pieces:
                keys = [ordered_keys(split_parenthetical(p)[0])[:1] for p in pieces]
                if all(k and k[0] in alone for k in keys):
                    out.extend(split_parenthetical(p) for p in pieces)
                    continue
                # "Handle/Group": a known group after the slash names no one
                if "/" in part and len(pieces) == 2 and \
                        not set(ordered_keys(pieces[1])).isdisjoint(groups_known):
                    out.append(split_parenthetical(pieces[0]))
                    continue
            out.append(split_parenthetical(part))
        return out

    brackets: list = []         # (outer node, outer spelling, handle, HVSC folders)
    for raw, w, fld, key, fo, all_scene, mf in credits:
        breathe()
        ps = persons(raw)
        single = len(ps) == 1
        for base, inner in ps:
            if is_placeholder(base):
                continue
            i = node(base)
            if i < 0:
                continue
            sp = " ".join(base.split())
            # (the tag's every track — a CD album too — only for a name of two
            # words or more, or a tag of scene tunes only: a one-word handle
            # shared with a band never takes the band's albums)
            whole = single and fld == "a" and (all_scene or word_count(sp) >= 2)
            entries.append((i, sp, w, fld, key, whole))
            if fo:
                folder[i] = True
            if inner is not None and handle_like(inner) and len(inner.strip()) >= 3:
                iks = ordered_keys(inner)
                if iks and iks[0] not in groups_known:
                    brackets.append((i, sp, inner.strip(), mf))
    del credits

    # Demozoo's rows for every scene spelling (and a folder name's header
    # order, and the handles beside names) — read once, used by rule 3 and 4.
    rows: dict = {}
    if use_demozoo and idx.demozoo_sig is not None:
        want: set = set()
        for e in entries:
            want.update(index_keys(e[1]))
            if folder[e[0]]:
                r = rotated(e[1])
                if r:
                    want.update(index_keys(r))
        for _i, _sp, inner, _mf in brackets:
            want.update(index_keys(inner))
        rows = _demozoo_rows(want) if want else {}
        del want

    def people_of(sp: str, *, rotate: bool = False) -> tuple[set, bool]:
        """(the Demozoo people a spelling names, does it name a group?)"""
        out: set = set()
        grp = False
        cands = [sp] + ([rotated(sp)] if rotate and rotated(sp) else [])
        for c in cands:
            words = word_count(c)
            for k in index_keys(c):
                for rid, kind in rows.get(k, ()):
                    if kind == "group":
                        grp = True
                    elif kind == "real" and words < 2:
                        continue
                    elif kind == "variant" and len(k.replace(" ", "")) < _SQUASH_MIN:
                        continue
                    else:
                        out.add(int(rid))
        return out, grp

    # Rule 3: "Real Name (Handle)" joins the handle only on evidence — HVSC
    # files the tunes under that handle's folder, or Demozoo gives both
    # names to one person; never a group (the library's, Demozoo's), never a
    # handle written beside two different names.
    outers: dict = {}
    for i, _sp, inner, _mf in brackets:
        outers.setdefault(ordered_keys(inner)[0], set()).add(i)
    for i, sp, inner, mf in brackets:
        ik = ordered_keys(inner)[0]
        if len(outers.get(ik, ())) > 1:
            continue
        ip, igrp = people_of(inner) if rows else (set(), False)
        if igrp and not ip:
            continue
        ok = ik in mf
        if not ok and rows:
            op, _g = people_of(sp)
            ok = len(op) == 1 and op == ip
        if ok:
            j = node(inner)
            if j >= 0:
                entries.append((j, inner, 0, "-", "", False))
                union(i, j)
    del brackets, outers

    # 2. Rule 1 (same words / transliterated / squashed) and rule 2 among
    # the scene names …
    # (flat dicts of ints and strings: the cyclic GC has nothing to walk)
    pairs = dict.fromkeys((e[0], e[1]) for e in entries)
    by_key: dict = {}
    frees: dict = {}            # (order-free key, node) of each spelling
    for i, sp in pairs:
        breathe()
        for k in ordered_keys(sp):
            hit = by_key.get(k)
            if hit is None:
                by_key[k] = i
            elif hit != i:
                union(hit, i)
        for q in (squash_keys(sp) if squash_ok(sp) else ()):
            hit = by_key.get("\0" + q)
            if hit is None:
                by_key["\0" + q] = i
            elif hit != i:
                union(hit, i)
        of = order_free_key(sp)
        if of:
            frees[(of, i)] = None
    del pairs
    has_folder: dict = {}
    for of, i in frees:
        if folder[i]:
            has_folder[of] = True
    folder_free: dict = {}
    for of, i in frees:
        if of in has_folder:
            first = folder_free.setdefault(of, i)
            if first != i:
                union(first, i)
    del frees, has_folder

    # … and each artist tag of no scene tune that spells one of them (read
    # by its folded tag key: no track is read unless it joins)
    for key in rest:
        breathe()
        hit = None
        for k in ordered_keys(key):
            hit = by_key.get(k)
            if hit is not None:
                break
        if hit is None and squash_ok(key):
            for q in squash_keys(key):
                hit = by_key.get("\0" + q)
                if hit is not None:
                    break
        if hit is None:
            of = order_free_key(key)
            hit = folder_free.get(of) if of else None
        # (a tag of no scene tune joins by a name of two words or more only:
        # "Moby" the band is not "Moby" the scener)
        if hit is None or is_placeholder(key) or word_count(key) < 2:
            continue
        tids = art.get(key)
        if not tids:
            continue
        raw = spelling_of(tids, "artist", key)
        i = node(raw)
        if i < 0:
            continue
        entries.append((i, " ".join(raw.split()), len(tids), "a", key, True))
        union(hit, i)
    del rest, by_key, folder_free

    # The clusters: root → (start, end) in ``entries`` sorted by root.
    def group_entries() -> dict:
        # (a counting sort by root into one list: no container per cluster
        # for the cyclic GC to promote, no long C call holding the GIL)
        roots = [find(e[0]) for e in entries]
        count = [0] * len(nkey)
        for r in roots:
            count[r] += 1
        start = [0] * len(nkey)
        out: dict = {}
        pos = 0
        for r, n in enumerate(count):
            if n:
                start[r] = pos
                out[r] = (pos, pos + n)
                pos += n
        placed: list = [None] * len(entries)
        for e, r in zip(entries, roots):
            placed[start[r]] = e
            start[r] += 1
        entries[:] = placed
        return out
    clusters = group_entries()

    # 3. Demozoo: one person for every scene spelling of a cluster, or none.
    rid_of: dict = {}           # cluster root → releaser id
    group_only: set = set()     # cluster roots Demozoo knows only as a group
    if rows:
        by_rid: dict = {}           # releaser id → the first cluster root of his
        for root, (a, b) in clusters.items():
            people: set = set()
            grp = False
            for e in entries[a:b]:
                if e[3] == "a" or e[3] == "c" or e[2] == 0:
                    ep, eg = people_of(e[1], rotate=folder[e[0]])
                    people |= ep
                    grp = grp or eg
            if grp and not people:
                group_only.add(root)
            if len(people) == 1:          # (namesakes: the cluster takes none)
                rid = next(iter(people))
                rid_of[root] = rid
                first = by_rid.setdefault(rid, root)
                if first != root:
                    union(first, root)
        if by_rid:
            rid_of = {find(r): rid for r, rid in rid_of.items()}
            group_only = {r for r in group_only if find(r) == r}
            clusters = group_entries()

    # 4. The identities: clusters with a scene tune.
    people = _demozoo_people(set(rid_of.values())) if rid_of else {}
    taken: dict = {}
    for root in sorted(clusters, key=lambda r: nkey[r]):
        breathe()
        a, b = clusters[root]
        es = entries[a:b]
        if root in group_only:
            continue
        ids: set = set()
        spell: dict = {}
        folder_spell: set = set()
        akeys: list = []
        for i, sp, w, fld, key, whole in es:
            spell[sp] = spell.get(sp, 0) + w
            if folder[i]:
                folder_spell.add(sp)
            if fld == "-":
                continue
            tids = (art if fld == "a" else comp).get(key)
            if not tids:
                continue
            if whole:
                ids |= tids
                akeys.append(key)
            else:
                ids |= tids & scene_all
        n_scene = len(ids & scene_all)
        if not n_scene:
            continue
        rid = rid_of.get(root)
        person = people.get(rid) if rid is not None else None
        plain = [sp for sp in spell if sp not in folder_spell and "_" not in sp] or list(spell)
        best = max(plain, key=lambda sp: (spell[sp], sp != sp.lower(), [-ord(c) for c in sp]))
        name = " ".join(((person or {}).get("name") or best.replace("_", " ")).split())
        key = _facet_key(name)
        if key in taken:
            # (two musicians by one name: told apart by a real name, else by a
            # number — in the order of their spellings, the same on every build
            # of the same library)
            extra = (person or {}).get("real_name") or ""
            n = 2
            cand = f"{name} ({extra})" if extra else f"{name} ({n})"
            while _facet_key(cand) in taken:
                n += 1
                cand = f"{name} ({extra}, {n})" if extra else f"{name} ({n})"
            name, key = cand, _facet_key(cand)
        aliases = tuple(sp for sp, _c in sorted(spell.items(), key=lambda kv: (-kv[1], kv[0]))
                        if sp)
        known = list(dict.fromkeys(a.replace("_", " ") for a in aliases))
        for sp in folder_spell:
            r = rotated(sp)                  # ("Daglish Ben" is found as "Ben Daglish")
            if r and r not in known:
                known.append(r)
        if person:
            for nm in (person.get("name"), person.get("real_name"), *person.get("aliases", ()),
                       *person.get("variants", ())):
                if nm and nm not in known:
                    known.append(nm)
        taken[key] = Identity(key, name, ids, n_scene, aliases, tuple(akeys), rid,
                              (person or {}).get("real_name") or "", tuple(known))
    idx.by_key = taken
    wk: dict = {}               # (word, identity key) of every name
    for ident in taken.values():
        for nm in ident.names:
            for k in ordered_keys(nm):
                idx.by_name.setdefault(k, ident.key)
            for sp in spellings(nm):
                for w in sp:
                    wk[(w, ident.key)] = None
        for ak in ident.artist_keys:
            idx.by_artist[ak] = ident.key
    words: dict = {}
    for w, k in wk:
        hit = words.get(w)
        words[w] = (k,) if hit is None else (*hit, k)
    idx.words = words
    idx.sorted_words = sorted(words)
    return idx


# ── Store glue ───────────────────────────────────────────────────────────────

def index_for(store) -> ComposerIndex:
    """The store's identity index (``TrackStore.composer_index``)."""
    return store.composer_index()


# The rebuild running for each store (``rebuild``): one at a time.
_running: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
# How many times a rebuild goes again when the library changed while it ran
# (a scan writing all along: the next reader starts another).
_REBUILD_ROUNDS = 2


# A rebuild starts at most this many of its own durations after the last
# one ended: under a stream of writes (a scan) the identities lag by about
# that much and the worker thread takes at most a quarter of a core; a single
# edit after a quiet spell is rebuilt — and awaited — at once.
_REBUILD_GAP = 3.0
# A reader awaits a rebuild only when the last one took at most this long
# (a small library); else it gets the index built before at once — every
# reader the same one — while the rebuild runs, so no view waits for it.
AWAIT_MAX_S = 0.1
# … and such a rebuild starts at least this long after the read that found
# the index stale.
_QUIET_S = 0.5
# The first build is awaited by the counts and the Artists view on a
# library of at most this many tracks (about 0.1 s of build); a bigger one
# answers without until it is in (the start builds it at once, off the loop).
COLD_AWAIT_TRACKS = 50_000
_last_build: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # store → (end, s)


async def rebuild(store) -> ComposerIndex:
    """Bring the store's identities up to date off the event loop — the
    ``Snapshot`` on the loop (the tag keys), ``build_from`` in a worker
    thread — and install them; a rebuild already running is awaited
    instead.  Within ``_REBUILD_GAP`` of the last one, with an index built,
    that index is returned and a rebuild scheduled for the end of the gap
    (every reader sees the same index: lists, counts and drills agree).
    When the last rebuild took longer than ``AWAIT_MAX_S``, the index
    built before is returned at once and the rebuild goes on in the
    background.  Returns the index to use."""
    import asyncio
    import time
    memo = store._facet_memo.get("composer:index")
    last = _last_build.get(store)
    task = _running.get(store)
    if memo is not None and last is not None and last[1] > AWAIT_MAX_S:
        # (a rebuild a listener would wait for: the index built before, until
        # the new one is in — started after a quiet moment, so the requests a
        # write sets off are served at full speed first and a burst of
        # writes is one rebuild)
        if task is None or task.done():
            _schedule(store, max(_QUIET_S, last[0] + _REBUILD_GAP * last[1] - time.monotonic()))
        return memo[1]
    if task is None or task.done():
        if memo is not None and last is not None:
            wait = last[0] + _REBUILD_GAP * last[1] - time.monotonic()
            if wait > 0:
                _schedule(store, wait)
                return memo[1]
        task = asyncio.get_running_loop().create_task(_rebuild(store))
        _running[store] = task
    return await asyncio.shield(task)


_scheduled: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _schedule(store, delay: float) -> None:
    """A rebuild at the end of the gap (its timer kept in ``_scheduled``)."""
    import asyncio
    if _scheduled.get(store) is not None:
        return
    loop = asyncio.get_running_loop()

    def _go() -> None:
        # (the gap / quiet moment is over: the rebuild starts now)
        _scheduled.pop(store, None)
        if not store.composer_index_fresh():
            _start(store, loop)
    _scheduled[store] = loop.call_later(delay, _go)


def _start(store, loop) -> None:
    """The rebuild task, unless one runs."""
    task = _running.get(store)
    if task is not None and not task.done():
        return

    def _log(t) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.warning("composer identities not rebuilt: %s", t.exception())
    task = loop.create_task(_rebuild(store))
    task.add_done_callback(_log)
    _running[store] = task


async def _rebuild(store) -> ComposerIndex:
    """One rebuild (again when the library changed meanwhile, at most
    ``_REBUILD_ROUNDS`` times).  A build that fails is logged and serves no
    reader an error: the index built before stays (none yet: an empty one,
    not kept), and the next is tried after the gap."""
    import asyncio
    import time
    idx = None
    for _ in range(_REBUILD_ROUNDS):
        stamp = store._composer_stamp()
        t0 = time.monotonic()
        try:
            snap = snapshot(store)              # (a few ms: the keys, two id sets)
            idx = await asyncio.to_thread(build_from, snap)
        except Exception as exc:                        # noqa: BLE001
            log.warning("composer identities not rebuilt: %s", exc, exc_info=True)
            _last_build[store] = (time.monotonic(), max(1.0, time.monotonic() - t0))
            memo = store._facet_memo.get("composer:index")
            return memo[1] if memo is not None else ComposerIndex()
        store._composer_install(stamp, idx)
        end = time.monotonic()
        _last_build[store] = (end, end - t0)
        if store._composer_stamp() == stamp:
            break
    return idx


def rebuild_soon(store, loop) -> None:
    """Start a rebuild in the background (a synchronous reader found the
    identities stale)."""
    task = _running.get(store)
    if task is not None and not task.done():
        return
    import time
    last = _last_build.get(store)
    if last is not None:
        wait = last[0] + _REBUILD_GAP * last[1] - time.monotonic()
        if last[1] > AWAIT_MAX_S:
            wait = max(wait, _QUIET_S)
        if wait > 0:
            _schedule(store, wait)
            return
    _start(store, loop)


def needs_index(preds: dict) -> bool:
    """Do the ``filter_tracks`` predicates ``preds`` read the identities
    (``composer_match`` / ``composer_is``, in ``anyof`` / ``negate`` too)?"""
    items = [preds, *(dict(e) for e in (preds.get("negate") or ()))]
    for group in preds.get("anyof") or ():
        items.extend(dict(e) for e in group)
    return any(d.get("composer_match") is not None or d.get("composer_is") is not None
               for d in items)


def version_of(store) -> int:
    """The version of the identities the store serves now (0: none yet) —
    what a cache of rows folded with them keys on (a background rebuild
    lands without a write)."""
    memo = store._facet_memo.get("composer:index")
    return memo[1].version if memo is not None else 0


def generation(store) -> tuple:
    """What the identities depend on besides the browse state — the
    Demozoo index file (a cached ``composer:`` search follows it)."""
    return (_demozoo_sig(),)


def _composer_counts(store, kw: dict) -> dict:
    return {r["composer"]: r["count"] for r in store.aggregate_facet("composer", **kw)}


def fold_artist_rows(store, rows: list, *, primary_only: bool = False, exclude=None,
                     swap=None) -> list:
    """The Artists view's rows with each scene musician's spellings folded
    into one row: ``{artist: name, count, composer: name}`` — the count the
    composer page lists (``aggregate_facet("composer")``'s).

    In the order of one stable sort of them all by ``(label?,
    artist.lower())``, without that sort: ``rows`` are ``aggregate_artists``'
    (sorted by ``artist.lower()``), so a row left as it is — and a musician's
    row whose name sorts as his first tag row did — stays in place; only the
    few folded rows that sort elsewhere are set in between (bisected)."""
    idx = index_for(store)
    by_artist = idx.by_artist
    if not by_artist:
        return rows
    by_key = idx.by_key
    counts = _composer_counts(store, {"primary_only": primary_only, "exclude": exclude,
                                      "swap": swap})
    out: list = []
    olow: list = []                      # each out row's sort key
    opos: list = []                      # and its place in ``rows``
    moved: list = []                     # (sort key, place, row): sorts elsewhere
    labels: list = []
    done: set = set()
    for i, r in enumerate(rows):
        if r.get("label"):
            labels.append(r)
            continue
        low = (r.get("artist") or "").lower()
        ik = by_artist.get(low.strip())
        ident = by_key.get(ik) if ik else None
        if ident is None:
            out.append(r)
            olow.append(low)
            opos.append(i)
            continue
        if ident in done:
            continue
        done.add(ident)
        n = counts.get(ident.name)
        if n:
            row = {"artist": ident.name, "count": n, "composer": ident.name}
            nl = (ident.name or "").lower()
            if nl == low:
                out.append(row)
                olow.append(low)
                opos.append(i)
            else:
                moved.append((nl, i, row))
    if moved:
        # (after the equal keys from earlier places — as a stable sort)
        moved.sort(key=lambda m: (m[0], m[1]))
        merged: list = []
        lo, n_out = 0, len(out)
        for nl, i, row in moved:
            p = bisect.bisect_left(olow, nl, lo)
            while p < n_out and olow[p] == nl and opos[p] < i:
                p += 1
            merged.extend(out[lo:p])
            merged.append(row)
            lo = p
        merged.extend(out[lo:])
        out = merged
    if labels:
        labels.sort(key=lambda x: (x.get("artist") or "").lower())
        out.extend(labels)
    return out


def folded_artist_count(store, rows: list, *, primary_only: bool = False, exclude=None,
                        swap=None) -> int:
    """How many rows ``fold_artist_rows`` lists without a ``label`` — the
    Artists count of ``/library/counts`` — without building or sorting them:
    the rows, minus the ones of a known musician, plus the musicians with
    tracks."""
    idx = index_for(store)
    by_artist = idx.by_artist
    if not by_artist:
        return sum(1 for r in rows if not r.get("label"))
    by_key = idx.by_key
    counts = _composer_counts(store, {"primary_only": primary_only, "exclude": exclude,
                                      "swap": swap})
    n = 0
    done: set = set()
    for r in rows:
        if r.get("label"):
            continue
        ik = by_artist.get((r.get("artist") or "").lower().strip())
        ident = by_key.get(ik) if ik else None
        if ident is None:
            n += 1
        elif ident not in done:
            done.add(ident)
            if counts.get(ident.name):
                n += 1
    return n
