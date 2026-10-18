# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-memory TrackStore with indexed search.

All track metadata, ratings, play stats, playlists, history, waveforms, and
scan dirs live in Python dicts.  Search is served by inverted-word, tag, and
sorted indexes that are updated incrementally on every insert/update/delete.

Thread safety: all mutations happen on the asyncio event loop (single writer).
Read-only dict lookups are GIL-atomic and safe from any thread.
"""
from __future__ import annotations

import bisect
import hashlib
import heapq
import itertools
import logging
import math
import re
import sys
import threading
import time
import unicodedata
from collections import Counter, deque
from functools import lru_cache
from typing import Any, Callable


def normalise_year(y):
    """Collapse YYYYMMDD-form year ints to YYYY so the sorted index and the
    aggregation Counter agree.  Single source of truth — both
    ``TrackStore._index_track`` and ``scanner._async_exit_batch_mode``
    call this so neither path can drift from the other again.
    """
    if isinstance(y, int) and y > 9999:
        return y // 10000
    return y

from soniqboom.core import catalogue as _catalogue
from soniqboom.core.folder_album import spc_name_completes as _spc_name_completes
from soniqboom.core.folder_album import _ARTIST_PLACEHOLDERS
from soniqboom.core.retro import chip_family as _chip_family
from soniqboom.core.retro import is_retro_format as _is_retro_format
from soniqboom.models.track import Track, TrackMeta

log = logging.getLogger(__name__)

# ── Tokeniser ────────────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"[\s\-_.,;:!?()\[\]{}\"\'+=/\\|<>@#$%^&*~`]+")
_MIN_TOKEN_LEN = 1


def _tokenize_text(text: str) -> list[str]:
    """Split text into lowercase search tokens (NFC-normalised when not
    ASCII, so a decomposed "é" typed or tagged as e + U+0301 equals "é")."""
    if not text:
        return []
    low = text.lower()
    if not low.isascii():
        low = unicodedata.normalize("NFC", low)
    return [t for t in _SPLIT_RE.split(low) if len(t) >= _MIN_TOKEN_LEN]


# Letters NFKD does not decompose into an ASCII base + combining mark.
FOLD_EXTRA = str.maketrans({"ø": "o", "ł": "l", "æ": "ae", "œ": "oe", "ß": "ss",
                            "đ": "d", "ð": "d", "þ": "th", "ı": "i"})


def _fold_text(tok: str) -> str:
    # A combining mark is dropped only after a LATIN base letter, so
    # Japanese dakuten (か/が) and Cyrillic й/и stay distinct.
    out: list[str] = []
    latin = False
    for ch in unicodedata.normalize("NFKD", tok.translate(FOLD_EXTRA)):
        if unicodedata.combining(ch):
            if latin:
                continue
        else:
            latin = ord(ch) < 0x250
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


# Memoised per distinct search token.  (Whole titles/albums go through the
# uncached ``_fold_text`` — ``_game_key`` — so they can't evict the tokens.)
_fold_nonascii = lru_cache(maxsize=65536)(_fold_text)


def fold_token(tok: str) -> str:
    """Accent-folded form of a lower-cased search token ("öörni" → "oorni",
    "beyoncé" → "beyonce", "ødegaard" → "odegaard"); ASCII is returned as is.
    Memoised per distinct token."""
    return tok if tok.isascii() else _fold_nonascii(tok)


def _tokenize_track(track: dict) -> set[str]:
    """Extract all searchable tokens from a track dict — plus the
    accent-folded form of every non-ASCII token, so "oorni" finds "Öörni"."""
    tokens: set[str] = set()
    for field in ("title", "artist", "album_artist", "album", "composer",
                  "scene_group", "game"):
        for tok in _tokenize_text(track.get(field, "")):
            tokens.add(tok)
            if not tok.isascii():
                tokens.add(_fold_nonascii(tok))
    for alias in track.get("game_aliases") or ():
        for tok in _tokenize_text(alias):
            tokens.add(tok)
            if not tok.isascii():
                tokens.add(_fold_nonascii(tok))
    return tokens


# ── Sorted-list helpers (bisect-based) ───────────────────────────────────────

def _sorted_insert(lst: list[tuple], item: tuple) -> None:
    bisect.insort(lst, item)


def _sorted_remove(lst: list[tuple], item: tuple) -> None:
    i = bisect.bisect_left(lst, item)
    if i < len(lst) and lst[i] == item:
        lst.pop(i)


def _sorted_range(lst: list[tuple], lo: Any, hi: Any) -> list[str]:
    """Return track IDs where lo <= value <= hi."""
    i = bisect.bisect_left(lst, (lo,))
    j = bisect.bisect_right(lst, (hi, "\uffff"))
    return [tid for _, tid in lst[i:j]]


def _sorted_tail(lst: list[tuple], n: int) -> list[str]:
    """Return the last n track IDs (highest values)."""
    return [tid for _, tid in lst[-n:]][::-1]


# ── Field → derived-index map (for diff-based field updates) ─────────────────
#
# ``update_track_fields[_batch]`` touches only the index pieces whose INPUT
# fields changed (``TrackStore._reindex_changed``).  Everything below mirrors
# ``_index_track`` / ``_unindex_track`` exactly; the parity tests
# (tests/test_enrichment_r1_store_diff.py) compare a partially-maintained store
# against a fresh ``rebuild_indexes``.

_MISSING = object()
_EMPTY_SORT_KEY = "￿"
# The numeric lists' counterpart of ``_EMPTY_SORT_KEY``: a track with no year /
# duration / bpm is keyed after every real value (last ascending, first
# descending) instead of being left out, so a sorted page walk reaches every
# track the list's count includes.  Outside every year range and duration
# bucket the year filter and the duplicate grouping bisect.  A float, so the
# lists' tuples stay plain numbers the GC does not track (``_num_key`` makes
# every key in them a number).
_NO_VALUE_KEY = math.inf

# Word-index tokens (``_tokenize_track``).
_TOKEN_FIELDS = frozenset(("title", "artist", "album_artist", "album", "composer",
                           "scene_group", "game", "game_aliases"))
# Scalar tag indexes: field → attribute (``_tag_set(idx, t.get(field, ""))``).
_SCALAR_TAG_INDEXES = (
    ("album", "_tag_album"), ("format", "_tag_format"),
    ("dir_hash", "_tag_dir_hash"), ("scan_root_hash", "_tag_scan_root_hash"),
)
# Name tag indexes (``_name_keys``): a multi-artist tag is indexed under its
# whole value AND each of its artists.
_NAME_TAG_INDEXES = (("artist", "_tag_artist"), ("album_artist", "_tag_album_artist"))
# The separator ``core/metadata.py`` joins a tag's several artists with
# (``metadata.NAME_SEP``; equal to ``subsonic_index.NAME_SEP``).
NAME_SEP = " \u2022 "


def _name_parts_l(v) -> list:
    """The lower-cased artists of a multi-artist tag ("A • B" → ["a", "b"]),
    [] for a single name."""
    if not isinstance(v, str) or NAME_SEP not in v:
        return []
    out: list = []
    for p in v.split(NAME_SEP):
        p = p.strip().lower()
        if p and p not in out:
            out.append(p)
    return out


def _name_keys(v) -> list:
    """The ``_tag_artist`` / ``_tag_album_artist`` keys of a tag value: the
    whole value, and — a multi-artist tag ("A • B") — each of its artists,
    so browsing artist "A" finds the tracks tagged "A • B" and a tag that
    gains a second value stays under "A"."""
    if not isinstance(v, str):
        v = "" if v is None else str(v)
    full = v.strip().lower()
    if not full:
        return []
    parts = _name_parts_l(full)
    return [full, *(p for p in parts if p != full)] if parts else [full]


def _name_counts(v) -> list:
    """The ``_agg_artists`` / ``_agg_album_artists`` keys a tag value counts
    under: each artist of a multi-artist tag (never the joined value — the
    Artists views list A and B, not "A • B"), else the value itself."""
    if not isinstance(v, str):
        return []
    full = v.strip().lower()
    if not full:
        return []
    return _name_parts_l(full) or [full]


def name_display(v, key: str) -> str:
    """The display spelling of artist ``key`` (lower-cased) in tag value
    ``v``: the matching artist of a multi-artist tag, else ``v`` stripped."""
    v = (v or "").strip() if isinstance(v, str) else ""
    if NAME_SEP in v and v.lower() != key:
        for p in v.split(NAME_SEP):
            if p.strip().lower() == key:
                return p.strip()
    return v


# ``_agg_*`` counters.
_AGG_FIELDS = frozenset(("artist", "album_artist", "album", "genre", "genre_derived", "year"))


def track_genres(t: dict) -> list:
    """The genres track ``t`` is browsed and listed under: its derived genres
    (``genre_derived`` — core/genre_derive.py; set only for a retro track
    whose file names no genre) first, then its own ``genre`` list, each name
    once (case-insensitively).  Without derived genres: ``genre`` as stored.
    Every genre index, count and listing reads this — the tag index, the
    aggregates, the Subsonic catalogue and songs — so a genre a track shows
    can always be browsed."""
    g = t.get("genre") or []
    d = t.get("genre_derived")
    if not d:
        return g
    out: list = []
    seen: set = set()
    for x in (*d, *g):
        if not isinstance(x, str):
            continue
        k = x.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out


def _lex_key(v) -> str:
    return (v or "").strip().lower() or _EMPTY_SORT_KEY


def _lex_key_shared(v) -> str:
    """``_lex_key`` interned — for the low-cardinality sort keys (artist,
    album artist, album, format): one key object per distinct value, not one
    per track (~60 B a track and list)."""
    return sys.intern((v or "").strip().lower()) or _EMPTY_SORT_KEY


def _game_key(v) -> str:
    """The ``game:`` operator's match key of a title / album / query: runs of
    whitespace collapsed to one space, lower-cased, accents folded like the
    search tokens ("Pokémon" → "pokemon", "real evil      180s" → "real evil
    180s")."""
    s = " ".join((v or "").split()).lower()
    return s if s.isascii() else _fold_text(s)


def _name_word_hits(index: dict, value) -> set[str]:
    """The ids of every name in ``index`` (``_tag_artist`` / ``_tag_album_artist``)
    in which ``value`` starts a word — the search box's ``artist:Marcin`` finds
    "Marcin Romanowski (Sidder)", ``artist:sidder`` too; case, accents and runs
    of whitespace ignored, as in plain search (GitHub #25)."""
    q = _game_key(value)
    hits: set[str] = set()
    if not q:
        return hits
    for key, tids in index.items():
        k = _game_key(key)
        i = k.find(q)
        while i != -1:
            if i == 0 or not k[i - 1].isalnum():
                hits.update(tids)
                break
            i = k.find(q, i + 1)
    return hits


def _game_keys(t: dict) -> set[str]:
    """The ``_tag_game`` keys of track ``t``: its game and every alias."""
    keys: set[str] = set()
    for v in (t.get("game"), *(t.get("game_aliases") or ())):
        if isinstance(v, str) and v.strip():
            keys.add(_game_key(v))
    keys.discard("")
    return keys


def _game_fold_value(v):
    """Key of ``v`` in a ``_sorted_*_fold`` side list: its ``_game_key`` when
    that differs from its ``_lex_key`` (an accented value, or one with
    internal whitespace runs), else None — so the side lists hold only those
    few values, and the ``game:`` operator bisects them with the folded query
    next to the main lexical lists."""
    if not v or not isinstance(v, str):
        return None
    g = _game_key(v)
    return g if g and g != v.strip().lower() else None


# Retro families whose rips name their game in the TITLE when no game is
# known: HVSC SIDs ("Uridium 2") and Atari ST tunes.  A tracker module's or a
# console rip's title is a song / track name, never its game.
_GAME_TITLE_FAMILIES = frozenset({"sid", "atari"})


# Game sources that only guess the name: a SID / Atari ST tune's title still
# counts beside them.
_GAME_GUESS_SOURCES = frozenset({"folder", "modland-filename"})


def _game_title_hit(t: dict | None) -> bool:
    """May track ``t`` match the ``game:`` operator through its TITLE?  Only a
    SID / Atari ST tune whose game is unknown or only guessed (from its folder
    or a Modland file name — ``_GAME_GUESS_SOURCES``) and not set by the user
    (typed or cleared): HVSC rips name the game in the title.  Every other
    track matches through its game names only."""
    if t is None or "game" in (t.get("user_edited") or ()) or (
            (t.get("game") or "").strip()
            and t.get("game_source") not in _GAME_GUESS_SOURCES):
        return False
    fmt = t.get("format")
    return _is_retro_format(fmt) and _chip_family(fmt) in _GAME_TITLE_FAMILIES


def _num_key(v):
    """Raw field value ``v`` as a numeric-list key: an int / float as it is, a
    numeric string (a legacy raw ``"1999"`` year) as its number, anything else
    — None, NaN, ±inf, a non-numeric string — ``_NO_VALUE_KEY``.  Every key of
    a numeric list is then a number, so no raw value can make its keys
    incomparable (a ``TypeError`` out of an insert, a sort or a year range)."""
    if isinstance(v, int):                      # (a bool is an int)
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else _NO_VALUE_KEY
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return _NO_VALUE_KEY
        if not math.isfinite(f):
            return _NO_VALUE_KEY
        return int(f) if f.is_integer() else f
    return _NO_VALUE_KEY


# The numeric lists' value functions — the ONLY derivation of their keys:
# ``_index_track`` / ``_unindex_track``, the full rebuilds (here and the
# scanner's yielding one) and ``_SORTED_SPECS`` all call them.

def _year_value(t: dict):
    return normalise_year(_num_key(t.get("year")))


def year_number(v):
    """A raw ``year`` field as the number every year surface uses — keyed
    exactly like ``_sorted_year`` (a legacy string or YYYYMMDD year collapses
    to its year; an integral float becomes an int), None for no year."""
    y = normalise_year(_num_key(v))
    if y == _NO_VALUE_KEY:
        return None
    return int(y) if isinstance(y, float) and y.is_integer() else y


def _agg_year(t: dict):
    """The year the Years legend / ``_agg_years`` count ``t`` under."""
    return year_number(t.get("year"))


def _added_value(t: dict):
    # No ``added_at`` sorts as the oldest (0), as the Subsonic genre lists
    # order it — not as the newest, which the default newest-first list and
    # Recently Added would show first.
    a = _num_key(t.get("added_at"))
    return a if a and a != _NO_VALUE_KEY else 0


def _added_primary_value(t: dict):
    return _added_value(t) if t.get("is_duplicate_primary", True) else None


def _sortable_duration(d) -> bool:
    """A duration ``_sorted_duration`` is keyed on: non-zero and, when
    numeric, finite (a NaN from a broken probe breaks the list's order).  Any
    other track is keyed ``_NO_VALUE_KEY``."""
    if not d:
        return False
    try:
        return math.isfinite(d)
    except TypeError:
        return True


def _duration_value(t: dict):
    d = _num_key(t.get("duration"))
    return d if _sortable_duration(d) else _NO_VALUE_KEY


def _bpm_value(t: dict):
    return _num_key(t.get("bpm"))


# Sorted list → (input fields, value function).  The value function returns the
# list key for a track, or None when the track is not in that list (only the
# primary-only and ``game:`` side lists leave tracks out: every other list holds
# every track, its sort pages reaching every row its count includes).
_SORTED_SPECS: dict[str, tuple[frozenset[str], Callable[[dict], Any]]] = {
    "_sorted_year":             (frozenset(("year",)), _year_value),
    "_sorted_added_at":         (frozenset(("added_at",)), _added_value),
    "_sorted_added_at_primary": (frozenset(("added_at", "is_duplicate_primary")),
                                 _added_primary_value),
    "_sorted_duration":         (frozenset(("duration",)), _duration_value),
    "_sorted_bpm":              (frozenset(("bpm",)), _bpm_value),
    "_sorted_title":            (frozenset(("title",)), lambda t: _lex_key(t.get("title"))),
    "_sorted_artist":           (frozenset(("artist",)), lambda t: _lex_key_shared(t.get("artist"))),
    "_sorted_album_artist":     (frozenset(("album_artist",)),
                                 lambda t: _lex_key_shared(t.get("album_artist"))),
    "_sorted_album":            (frozenset(("album",)), lambda t: _lex_key_shared(t.get("album"))),
    "_sorted_format":           (frozenset(("format",)), lambda t: _lex_key_shared(t.get("format"))),
    # ``game:`` title side list (``_game_fold_value``).  Appended LAST: the
    # scanner's full rebuild relies on the order above (numeric lists first).
    "_sorted_title_fold":       (frozenset(("title",)),
                                 lambda t: _game_fold_value(t.get("title"))),
}
SORTED_LISTS = tuple(_SORTED_SPECS)
_ALL_SORTED = frozenset(SORTED_LISTS)
_N_SORTED = len(SORTED_LISTS)

# Every field some derived index reads.  A change confined to other fields is a
# plain dict update (+ AOF record) — no index work at all.
_INDEXED_FIELDS = (_TOKEN_FIELDS | _AGG_FIELDS
                   | frozenset(f for f, _ in _SCALAR_TAG_INDEXES)
                   | frozenset(f for f, _ in _NAME_TAG_INDEXES)
                   | frozenset(("duplicate_group_id", "genre", "genre_derived",
                                "scene_group", "game"))
                   | frozenset().union(*(f for f, _ in _SORTED_SPECS.values())))

# Fields NO ``_mutation_seq``-keyed consumer reads (core/skip_rules, the
# Subsonic caches, smart.py's duplicate memo, the folder-album pass, the
# index-health sweep; the browse memos key on ``_browse_seq``, whose exempt
# fields are ``_BROWSE_EXEMPT_FIELDS``).  An ALLOWLIST, deliberately: a
# change to any other field bumps the seq.  ``cover_art`` is written on the
# first art extraction of every track, so bumping for it re-cooled every
# library-wide cache on normal browsing; ``default_subsong`` (a multi-tune
# file's probed default tune) on its first play.
_SEQ_EXEMPT_FIELDS = frozenset(("cover_art", "mtime", "file_size", "default_subsong"))

# Fields a browse surface reads besides the indexed ones: the duplicate
# stand-in pick (``duplicates.primary_sort_key`` — ``bitrate``) and the
# ``game:`` operator's title test (``_game_title_hit`` — ``game_source``,
# ``user_edited``).
_BROWSE_EXTRA_FIELDS = frozenset(("bitrate", "game_source", "user_edited"))

# Fields NO browse surface reads — the lists, counts, legends and orders
# (``filter_tracks``, ``filter_track_ids``, the ``aggregate_*`` legends and
# their HTTP cache, ``visible_track_count`` / ``untagged_count``,
# ``primary_swap``).  A change confined to these — or to these and
# ``duration``, which the duration order keys on via ``_duration_seq``
# (``_browse_duration_only``) — leaves ``_browse_seq`` alone.  An ALLOWLIST, like ``_SEQ_EXEMPT_FIELDS``: a
# change to any other field — one added later included — bumps it.  Disjoint
# from ``_INDEXED_FIELDS | _BROWSE_EXTRA_FIELDS`` (test-guarded).
_BROWSE_EXEMPT_FIELDS = _SEQ_EXEMPT_FIELDS | frozenset((
    # a listener's playback-problem mark (core/skip_rules) and the health badge
    "health", "health_note", "health_by", "health_at", "health_renderer",
    "defect", "defect_detail", "defect_renderer", "defect_at", "defect_evidence",
    # tune data of SID / libgme / PSF files (the start pass, HVSC, repair)
    "subsongs", "subsong_base", "start_subsong", "tune_lengths", "fade_s",
    "hvsc_lengths", "stil", "sid_md5", "sid_model",
    # enrichment join keys and provenance (the values they set are indexed)
    "file_md5", "scene_path", "scene_use", "scene_party", "year_source", "year_file",
    "album_source", "songdb_fields",
    "game_by_tag", "game_by_modland", "game_by_demozoo", "game_by_songdb",
    "game_by_modland_filename", "game_by_archive", "game_by_folder",
    # technical and tag fields no list sorts, filters or counts by
    "replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain",
    "replaygain_album_peak", "is_lossless", "sample_rate", "channels", "bit_depth",
    "format_score", "instruments", "patterns", "label", "comment", "isrc",
    "track_number", "total_tracks", "disc_number", "total_discs",
))

# Inputs of the duplicate grouping (``duplicates.group_key_for`` + the primary
# pick) and its output.  A write that changes an input — or loses the output —
# records the track's PREVIOUS values in ``_dup_dirty`` so the scanner re-groups
# exactly the affected groups (``take_dup_dirty``), whoever made the change: a
# scan, a tag edit, an HVSC / render duration backfill, a Modland artist fill.
_DUP_FIELDS = frozenset(("title", "artist", "album_artist", "duration",
                         "format", "bitrate", "added_at"))
_DUP_OUTPUT_FIELDS = frozenset(("duplicate_group_id", "is_duplicate_primary", "format_score"))
# What ``TrackStore.primary_swap`` reads of a group member: its group, whether
# it is the primary and ``duplicates.primary_sort_key``'s fields.
_SWAP_FIELDS = frozenset(("duplicate_group_id", "is_duplicate_primary", "format", "bitrate",
                          "added_at", "id"))
_DUP_DIRTY_MAX = 20_000     # more pending changes than this → one full pass instead

# The post-scan enrichment passes (Modland, song database, Demozoo, game from
# archive name, album from folder name) re-join only the tracks written since
# their own last run (``core/enrich_delta.py``): every upsert, delete and field
# update records the track id in ``_enrich_log`` — except a change confined to
# fields no pass reads (these; a field a pass starts to read must leave this
# list).  More ids than ``_ENRICH_LOG_MAX`` → the oldest half is dropped, and a
# pass whose last run is older runs over everything.
_ENRICH_EXEMPT_FIELDS = frozenset(("cover_art", "mtime", "file_size", "duplicate_group_id",
                                   "is_duplicate_primary", "format_score", "default_subsong",
                                   # the derived-genre pass's own output: no
                                   # pass reads it (core/genre_derive.py)
                                   "genre_derived"))
_ENRICH_LOG_MAX = 100_000

# Fields the Subsonic catalogue (core/subsonic_index.py) groups/sorts/counts on
# — including a multi-tune file's tune count, default tune and per-tune
# lengths, from which it expands the tunes and sums album song counts.
# ``_catalog_seq`` bumps on inserts/deletes and on a change to one of these, so
# the catalogue can key on it instead of the (much hotter) ``_mutation_seq``.
_CATALOG_FIELDS = frozenset(("title", "artist", "album_artist", "album", "genre", "genre_derived",
                             "year", "duration", "added_at", "dir_hash", "path",
                             "track_number", "disc_number",
                             "subsongs", "start_subsong", "hvsc_lengths",
                             # (what "Skip short tunes" lists in a multi-tune file)
                             "tune_lengths", "fade_s", "default_subsong"))

# A write that changes ``duration`` and nothing else (besides seq-exempt fields)
# — the render-time / probe backfill of a render-only format's real length,
# which runs on every first play — bumps only ``_duration_seq``.  No web
# aggregate reads a track's duration.  The Subsonic catalogue does sum
# durations into its album / folder totals, and deliberately accepts that
# those precomputed totals may lag a backfill (song rows and getAlbum read the
# live durations) rather than being rebuilt after every first play — so
# ``_catalog_seq`` stays out of it, like ``_mutation_seq``, whose bump re-cooled
# every library-wide cache after each first play.  Consumers that DO read
# durations (the duplicate memo, the scanner's sorted-rebuild generation guard,
# ``_primary_order``) key on both sequences.
_DURATION_SEQ_FIELDS = _SEQ_EXEMPT_FIELDS | frozenset(("duration",))


def _multi_tune(t: dict) -> bool:
    n = t.get("subsongs")
    return type(n) is int and n > 1


def _duration_only(changed: dict) -> bool:
    return "duration" in changed and changed.keys() <= _DURATION_SEQ_FIELDS


def _browse_duration_only(changed: dict) -> bool:
    """Is the (diffed) field update ``changed`` a duration write as far as
    the browse surfaces go — ``duration`` and otherwise only fields none of
    them reads (``_BROWSE_EXEMPT_FIELDS``: the tune-length backfill's
    ``tune_lengths``, a default-tune probe's ``start_subsong``)?  It moves
    ``_duration_seq`` (the duration order keys on it), not ``_browse_seq``;
    ``_mutation_seq`` / ``_catalog_seq`` follow its other fields as usual
    (``_duration_only`` is the narrower case that moves neither)."""
    return "duration" in changed and changed.keys() - {"duration"} <= _BROWSE_EXEMPT_FIELDS


def _browse_change(changed: dict) -> bool:
    """Whether the (diffed) field update ``changed`` can change a browse
    result — what bumps ``_browse_seq``: not a duration write as far as the
    browse surfaces go (``_browse_duration_only`` — the duration order keys on
    ``_duration_seq``), not one confined to ``_BROWSE_EXEMPT_FIELDS``."""
    return not _browse_duration_only(changed) and not changed.keys() <= _BROWSE_EXEMPT_FIELDS


# The "[No Artist]"-style buckets (``untagged``): field → (the maintained id
# set's attribute, the fields its blank test reads).
_UNTAGGED_SPECS = (
    ("artist", "_untagged_artist", frozenset(("artist",))),
    ("album_artist", "_untagged_album_artist", frozenset(("album_artist",))),
    ("album", "_untagged_album", frozenset(("album",))),
    ("genre", "_untagged_genre", frozenset(("genre", "genre_derived"))),
)
_UNTAGGED_ATTR = {f: a for f, a, _i in _UNTAGGED_SPECS}


def _blank_tag(v) -> bool:
    """THE missing/blank tag test of the ``untagged`` buckets: a list counts
    as blank when no element has text, any other value when its text is empty
    (None included)."""
    if isinstance(v, (list, tuple)):
        return not any(str(x).strip() for x in v)
    return not (str(v).strip() if v is not None else "")


def _untagged(t: dict, field: str) -> bool:
    """Is track ``t``'s ``field`` tag blank (``_UNTAGGED_SPECS``)?  Genre reads
    the genres the track is listed under (``track_genres``)."""
    if field == "genre":
        return _blank_tag(track_genres(t))
    return _blank_tag(t.get(field))


# ``_paginate_all`` / ``_paginate_excluding``: a sorted page with duplicates
# hidden (or ids excluded) at an offset of at least this many rows reads the
# memoised order of what is listed (``TrackStore._primary_order`` /
# ``_excluded_order`` — a walk resumed where the deepest page so far stopped)
# instead of walking and filtering ``offset`` rows per request; shallower pages
# keep the plain walk (~1.5 ms at this depth).
_PRIMARY_ORDER_MIN_OFFSET = 5000

# ``filter_tracks`` sorts a candidate set of up to this many ids (or 1/64 of
# the library, if larger) directly instead of walking the full sorted index:
# measured on a 263K-track library, ``game:uridium`` (8 ids) 28 ms → 0.03 ms.
_SMALL_CANDIDATE_SET = 2000

# ``filter_tracks``: the order a larger candidate set's pages walk is kept
# (``_FilterWalk``) — per predicates, sort and direction, valid until the next
# browse write (``_browse_seq``) — and each page resumes the walk where the
# last one stopped, so paging a 300 K-track genre costs one walk of the index
# in all, not one per page.  Bounded: this many orders, and this many ids held
# (their candidate sets and walked prefixes) in all; the least recently used
# goes first.
_FILTER_WALK_MAX = 64
_FILTER_WALK_MAX_IDS = 8_000_000
# A walk reads the index in slices of at least this many rows (doubling).
_FILTER_WALK_STEP = 2048


# Batch mode: a sorted list whose deferred update lists at most this many
# tracks, or 1/``_SORTED_MERGE_DIV`` of the library if more, is merged at the
# exit (``merged_sorted_list``: the list copied around the changes, ~3 ms at
# 680K tracks); past that it is rebuilt and sorted in full — every list, when
# inserts / deletes crossed it (they are in every list), the one list, when
# field updates re-keyed that many of its tracks.
_SORTED_MERGE_MIN = 20_000
_SORTED_MERGE_DIV = 8
# Up to this many entries cut / inserted, a merge copies the list once and
# edits the copy in place (each edit a memmove of its tail).
_SORTED_MERGE_INPLACE = 16


# A scan commit that upserted at least this many tracks since the last freeze
# freezes the heap again (scanner._async_exit_batch_mode) — e.g. a first scan.
FREEZE_AFTER_UPSERTS = 50_000


def freeze_long_lived_heap(reason: str) -> None:
    """``gc.collect()`` then ``gc.freeze()``: every object alive now — the
    library (~1M GC-tracked objects at 263K tracks) — leaves the cyclic GC's
    generations, so a full collection no longer walks it (measured on a copy
    of a 263K-track library: a full collection 513 ms before, ~0 ms after;
    the freeze itself costs one such collection).  Refcounting still frees
    frozen objects that are later dropped (a replaced track dict); only CYCLIC
    garbage alive at the freeze would be kept, which is why the collect runs
    first.  Call it after a large load, never with a bare ``gc.freeze()``."""
    import gc
    t0 = time.monotonic()
    gc.collect()
    gc.freeze()
    log.info("GC: froze the long-lived heap after %s in %.0f ms (%d objects frozen)",
             reason, (time.monotonic() - t0) * 1000, gc.get_freeze_count())


# Track fields whose values repeat across many tracks (a format, a folder's
# hash, a composer, a provenance word, the genre words): ``share_values``
# makes equal values one string object.  A snapshot load parses every value
# into a string of its own — ~60 B each, per field, per track (a 330k-track
# Modland catalogue loaded ~0.3 GB more than it was built with).
_SHARED_STR_FIELDS = ("format", "dir_hash", "scan_root_hash", "album_source", "game_source",
                      "year_source", "defect", "sid_model", "artist", "album_artist", "album",
                      "composer", "label", "game", "scene_group", "game_by_modland",
                      "game_by_modland_filename", "game_by_tag")
_SHARED_LIST_FIELDS = ("genre", "genre_derived", "songdb_fields", "user_edited", "scene_use",
                       "game_aliases")


# Store config key: catalogue track ids that playlists list while the tracks
# are out of the library (``modland_catalog._park_user_refs``).
PARKED_PLAYLIST_IDS = "catalogue_parked_playlist_ids"


def _entry_tid(e):
    return e.get("id") if isinstance(e, dict) else e


def _merge_unseen_entries(old: list, new: list, keep) -> list:
    """``new`` (a client's playlist save) plus the entries of ``old`` whose
    id is in ``keep`` (tracks that client can't see) and that ``new`` lacks —
    each re-inserted after the visible entry it followed before (at the
    front when none)."""
    new_ids = {_entry_tid(e) for e in new}
    pos: dict = {}
    for i, e in enumerate(new):
        pos.setdefault(_entry_tid(e), i)
    inserts: dict = {}
    anchor = -1
    for e in old:
        tid = _entry_tid(e)
        if tid in keep:
            if tid not in new_ids:
                inserts.setdefault(anchor, []).append(e)
            continue
        if tid in pos:
            anchor = pos[tid]
    if not inserts:
        return new
    out = list(inserts.get(-1, ()))
    for i, e in enumerate(new):
        out.append(e)
        out.extend(inserts.get(i, ()))
    return out


def share_values(t: dict) -> dict:
    """Intern ``t``'s repeating string values in place (equal values, one
    object; interned strings are freed with their last user).  Returns ``t``."""
    intern = sys.intern
    for f in _SHARED_STR_FIELDS:
        v = t.get(f)
        if v.__class__ is str and v:
            t[f] = intern(v)
    for f in _SHARED_LIST_FIELDS:
        v = t.get(f)
        if v.__class__ is list and v:
            t[f] = [intern(x) if x.__class__ is str else x for x in v]
    return t


def _diff_updates(t: dict, updates: dict) -> dict:
    """The sub-dict of ``updates`` whose values differ from track ``t``.

    A list/dict/set value that IS the track's own object counts as changed: a
    caller may have mutated it in place, and it must still reach the AOF."""
    out: dict = {}
    for k, v in updates.items():
        cur = t.get(k, _MISSING)
        if cur is v:
            if isinstance(v, (list, dict, set)):
                out[k] = v
            continue
        try:
            if cur != v:
                out[k] = v
        except Exception:                       # noqa: BLE001 — incomparable values
            out[k] = v
    return out


# ── TrackStore ───────────────────────────────────────────────────────────────

# Every per-track DERIVED index attribute on TrackStore — exactly what
# ``clear_indexes`` empties, plus the lazily-built ``_word_list``.  Shared by
# the integrity check (``verify_indexes``) and the atomic async rebuild
# (``data.rebuild_indexes``).  KEEP IN SYNC with ``clear_indexes``.
INDEX_ATTRS = (
    "_word_index", "_word_list",
    "_tag_artist", "_tag_album_artist", "_tag_album", "_tag_genre",
    "_tag_format", "_tag_dir_hash", "_tag_scan_root_hash", "_tag_dup_group",
    "_tag_scene_group", "_tag_game",
    "_sorted_year", "_sorted_added_at", "_sorted_added_at_primary",
    "_sorted_duration", "_sorted_bpm",
    "_sorted_title", "_sorted_artist", "_sorted_album_artist",
    "_sorted_album", "_sorted_format", "_sorted_title_fold",
    "_agg_artists", "_agg_album_artists", "_agg_albums", "_agg_genres",
    "_agg_years", "_agg_albums_by_artist", "_agg_albums_by_album_artist",
    "_agg_cache", "_unplayed_ids",
    # Browse id sets (set arithmetic instead of per-row tests): the non-primary
    # duplicate copies, the "[No Artist]"-style buckets, year → ids.
    "_nonprimary_ids", "_untagged_artist", "_untagged_album_artist",
    "_untagged_album", "_untagged_genre", "_tag_year",
)


# What a sorted-list generator (``iter_merged_sorted_list`` /
# ``iter_filtered_sorted_list``) yields right before it starts assembling the
# new list (a pause yields None): an async driver pauses the cyclic GC from
# there on (every collection holding the half-built n-entry list walks all of
# it), after the new entries were made with the GC running (young passes
# untrack their tuples as they go).
ASSEMBLE = object()


def _run_generator(gen):
    """Run ``gen`` to its end; its return value."""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def shadowed_index_attrs() -> tuple:
    """The ``INDEX_ATTRS`` the index audit (``core/index_health``) compares
    against a shadow rebuild: all but the sorted lists (checked in place),
    ``_unplayed_ids`` (checked against ``_play_stats``) and the lazy
    ``_word_list`` / the ``_agg_cache`` memo (``TrackStore._DIFF_SKIP``)."""
    return tuple(a for a in INDEX_ATTRS
                 if a not in _ALL_SORTED and a not in ("_word_list", "_agg_cache", "_unplayed_ids"))


def _index_sample_diff(live, exp, limit: int = 5) -> list:
    """Up to ``limit`` keys/elements that differ between two index structures —
    so a drift report can show WHAT diverged, not merely that it did."""
    try:
        if isinstance(live, dict) and isinstance(exp, dict):
            diff = list(set(live) ^ set(exp))[:limit]
            if len(diff) < limit:
                diff += [k for k in live if k in exp and live[k] != exp[k]][: limit - len(diff)]
            return [str(k) for k in diff[:limit]]
        if isinstance(live, set) and isinstance(exp, set):
            return [str(x) for x in list(live ^ exp)[:limit]]
    except Exception:
        pass
    return []


# Demozoo stores a composer's collectives as a single " • "-joined string
# (see core/demozoo.py); the scene-group index keys on each collective
# separately.  ONE splitter shared by ``_index_track``/``_unindex_track`` and
# ``similar_candidates`` so the indexed keys and the lookup keys can't drift.
_SCENE_GROUP_SEP = " • "


def _scene_group_keys(scene_group: object) -> list[str]:
    """Individual normalised (lower/stripped) group keys from a track's
    ``scene_group`` string, e.g. ``"Fairlight • Maniacs of Noise"`` →
    ``["fairlight", "maniacs of noise"]``.  Empty for a missing/blank value."""
    if not isinstance(scene_group, str) or not scene_group:
        return []
    return [g.strip().lower() for g in scene_group.split(_SCENE_GROUP_SEP) if g.strip()]


def modland_artist_kept(old: dict, new: dict, *, absent_md5_is_same: bool = False) -> bool:
    """True when ``new`` — a fresh extract of ``old``'s file — carries no artist
    while ``old``'s artist is exactly the Modland credit of its ``scene_path``
    (filled by the md5 join, never read from the file), so a rescan or a
    re-extract must keep it rather than clear it.

    The md5 must be unchanged (a different file loses the credit).  With
    ``absent_md5_is_same`` a re-extract that computed no md5 counts as the same
    file (repair re-reads the file the track already points at).  O(1): the
    path parse runs only when the fresh extract has no artist."""
    if not old.get("scene_path") or not (old.get("artist") or "").strip():
        return False
    if (new.get("artist") or "").strip():
        return False
    new_md5 = new.get("file_md5")
    if new_md5 != old.get("file_md5") and not (absent_md5_is_same and new_md5 is None):
        return False
    from soniqboom.core import scene_metadata as _sm      # lazy: import cycle
    return old["artist"] == _sm.author_credit(_sm.parse_modland_path(old["scene_path"]))


# ``game_source`` of a retro track's game taken from the album the user typed.
GAME_SOURCE_USER_ALBUM = "user-album"
# The game name each source gives a retro track (``TrackMeta.game_by_*``), in
# precedence order — each written only by its own source and cleared by its
# own withdrawals: the file's own tag, the Modland game folder, the Demozoo
# game a tune named after it is the soundtrack of (``core.demozoo``), the UADE
# song database, the Modland file name, the archive's name when it is a known
# game of the track's platform (``core.game_titles``), the folder name
# (opt-in).  The order was chosen from the data (internal/KNOWN-ISSUES.md).
GAME_NAME_SLOTS = (("tag", "game_by_tag"), ("modland", "game_by_modland"),
                   ("demozoo", "game_by_demozoo"),
                   ("songdb", "game_by_songdb"),
                   ("modland-filename", "game_by_modland_filename"),
                   ("archive", "game_by_archive"),
                   ("folder", "game_by_folder"))
_SLOT_OF = dict(GAME_NAME_SLOTS)
GAME_SLOT_FIELDS = frozenset(_SLOT_OF.values())
# The fields ``game_follow`` reads.
_GAME_INPUTS = frozenset({"album", "album_source", "game", "game_source", "user_edited",
                          "format", "game_aliases"}) | GAME_SLOT_FIELDS


def _slot_names(t: dict, override: dict | None = None) -> list[tuple[str, str]]:
    """``(source, name)`` of every per-source game name ``t`` holds (a field
    in ``override`` taken from there), in precedence order."""
    out = []
    get = t.get
    for src, f in GAME_NAME_SLOTS:
        v = override[f] if override and f in override else get(f)
        if v and v.__class__ is str:
            v = v.strip()
            if v:
                out.append((src, v))
    return out


def game_follow(t: dict) -> dict:
    """The change of ``game`` / ``game_source`` / ``game_aliases`` (and of a
    per-source name, see below) track ``t``'s state implies — {} when none.

    Per-source names (``GAME_NAME_SLOTS``): a game album IS its source's name
    — that name follows the album (recording it for an album stamped before
    the per-source names existed); replaced by another source's album it
    stays; withdrawn, it goes (``_with_game``).  An SPC's 32-byte-cut name
    that another name completes (``spc_name_completes``) is not one.

    The game: one the user typed (``user_edited``) or a file's own GAME tag
    (a game with ``game_source`` None) is kept as it is; else the album the
    user typed on a retro track (``GAME_SOURCE_USER_ALBUM``); else the first
    per-source name in precedence order (``game_source`` = its source) —
    usually the album's own; else empty.  So every writer of a retro album or
    name (scan, Modland / song-database / folder passes, their withdrawals
    and resets, rescans, repairs, the user's edits) moves the game with it.

    The aliases: every other per-source name, each once (``_game_key``), a
    name another one starts with dropped; none when the user cleared the
    game.

    Pure — a function of the stored fields alone (and, through
    ``is_retro_format``, of the installed uade's format list) — so it is
    applied on every store write and on load, and an AOF replay lands on the
    same game (``_with_game`` logs the per-source names with every game
    change)."""
    out: dict = {}
    ue = t.get("user_edited") or ()
    album = t.get("album") or ""
    asrc = t.get("album_source")
    slot = _SLOT_OF.get(asrc)
    if slot and album.strip() and "album" not in ue and t.get(slot) != album:
        out[slot] = album                        # the album IS its source's name
    names = _slot_names(t, out)
    fmt = t.get("format")
    if len(names) > 1:                           # an SPC cut another name completes
        names = [(src, n) for src, n in names
                 if not any(o != n and _spc_name_completes(o, n, fmt) for _s, o in names)]
    game = t.get("game") or ""
    gsrc = t.get("game_source")
    if "game" in ue or (game.strip() and gsrc is None):
        want, want_src = game, gsrc              # the user's, or the file's GAME tag
    else:
        if "album" in ue and album.strip() and _is_retro_format(fmt):
            want, want_src = album, GAME_SOURCE_USER_ALBUM
        elif names:
            want_src, want = names[0]
        else:
            want, want_src = "", None
        if game != want:
            out["game"] = want
        if gsrc != want_src:
            out["game_source"] = want_src
    aliases: list[str] = []
    if (want.strip() and names                   # (none when the user cleared it)
            and not (len(names) == 1 and names[0][1] == want)):
        keyed = [(n, _game_key(n)) for _s, n in names]
        pk = _game_key(want)
        every = [pk, *(k for _n, k in keyed)]
        seen = {pk}
        for n, k in keyed:
            if not k or k in seen or any(o != k and o.startswith(k) for o in every):
                continue
            seen.add(k)
            aliases.append(n)
    if (t.get("game_aliases") or None) != (aliases or None):
        out["game_aliases"] = aliases or None
    return out


# The fields of a game change: a record changing any carries the per-source
# names too, so the merged snapshot and an AOF replay see the names the game
# was derived from (one recorded on load exists in memory only).
_GAME_OUTPUTS = frozenset({"album", "album_source", "game", "game_source", "game_aliases"})


def _with_game(t: dict, changed: dict) -> dict:
    """``changed`` (a diffed field update of stored track ``t``) plus the game
    change it implies (``game_follow``).  A game album WITHDRAWN (emptied, its
    source gone, not by the user — replaced by another album, the file's own
    included, it stays an alias) takes its per-source name along.  A result that
    changes the game, the album (``_GAME_OUTPUTS``) or ``user_edited`` also
    carries the game, its source and aliases and every per-source name: the
    game is derived in memory at load, so the AOF record must hold the whole
    game state (a game without its source would load as the file's own GAME
    tag, a typed game without its value as a cleared one)."""
    if not changed or _GAME_INPUTS.isdisjoint(changed):
        return changed
    out = dict(changed)
    old_src = t.get("album_source")
    if ("album_source" in changed and old_src in _SLOT_OF
            and changed["album_source"] in (None, "")
            and not (changed.get("album", t.get("album")) or "").strip()
            and "album" not in (changed.get("user_edited", t.get("user_edited")) or ())
            and _SLOT_OF[old_src] not in changed
            and (t.get(_SLOT_OF[old_src]) or "") == (t.get("album") or "")):
        out[_SLOT_OF[old_src]] = None
    view = {k: out[k] if k in out else t.get(k) for k in _GAME_INPUTS}
    extra = game_follow(view)
    if extra:
        out.update(extra)
    result = _diff_updates(t, out)
    if not _GAME_OUTPUTS.isdisjoint(result) or "user_edited" in result:
        for f in ("game", "game_source", "game_aliases"):
            if f not in result and (f in out or f in t):
                result[f] = out[f] if f in out else t[f]
        for f in GAME_SLOT_FIELDS:
            v = out[f] if f in out else t.get(f)
            if v and f not in result:
                result[f] = v
    return result


def songdb_fill_kept(old: dict, new: dict, field: str, *,
                     absent_md5_is_same: bool = False) -> bool:
    """True when ``old``'s ``field`` (``artist`` / ``label``) was filled by the
    UADE song database (listed in ``songdb_fields``) and ``new`` — a fresh
    extract of the SAME file — names nobody there itself (empty, or a ripper
    placeholder artist), so the fill is kept.  ``absent_md5_is_same``: as in
    ``modland_artist_kept``."""
    sf = old.get("songdb_fields")
    if not sf or field not in sf or not (old.get(field) or "").strip():
        return False
    cur = (new.get(field) or "").strip()
    if cur and not (field == "artist" and cur.lower() in _ARTIST_PLACEHOLDERS):
        return False
    new_md5 = new.get("file_md5")
    return new_md5 == old.get("file_md5") or (absent_md5_is_same and new_md5 is None)


def _carry_enrichment(old: dict, new: dict) -> None:
    """Preserve post-scan enrichment from ``old`` onto a freshly-built ``new``
    (mutated in place).  A rescan builds a fresh track straight from the file,
    so without this an ``upsert`` (a full replace) would wipe every field an
    apply pass produced — the Demozoo year backfill would revert to the rip
    year, scene_group/scene_path would vanish until the next manual apply.

    Idempotent: a no-op when ``new`` already carries the field (an already-
    merged AOF record replays unchanged).

    Each enrichment is keyed on an IDENTITY and is carried only while that
    identity is unchanged — otherwise a changed composer/file would keep stale
    data forever (no apply pass has a withdraw branch for it):

      * ``scene_group`` (Demozoo, composer→group) — carried while ``artist`` is
        unchanged;
      * ``scene_path`` (Modland, md5 join) — carried while ``file_md5`` is
        unchanged;
      * the ``year``/``year_source``/``year_file`` triple — a USER year is a
        deliberate choice, carried regardless; a DEMOZOO year is composer-
        derived, so carried only while ``artist`` is unchanged (a changed
        composer drops it, and the next apply re-evaluates);
      * a DERIVED ``album`` + ``album_source`` (``"modland"`` /
        ``"modland-filename"`` from the md5 join — carried while ``file_md5``
        is unchanged; ``"folder"`` from the path, which the track id is keyed
        on — carried always), and only while the fresh extract has NO album:
        a real tag that appears later wins — except the Modland completion of
        an SPC's 32-byte-cut header game, carried over the cut name while
        ``file_md5`` is unchanged;
      * a Modland-filled ``artist`` (the old artist is exactly the credit of
        the old ``scene_path``) — carried while ``file_md5`` is unchanged and
        only while the fresh extract has NO artist.  It is restored before the
        ``same_artist`` test, so the Demozoo group/year keyed on it survive
        too;
      * the UADE song database's fills (core/songdb.py) — an ``artist`` /
        ``label`` listed in ``songdb_fields``, an ``album_source`` "songdb"
        album and a ``year_source`` "songdb" year — carried while ``file_md5``
        is unchanged and only while the fresh extract has no value of its own
        (the artist is restored before the ``same_artist`` test too);
      * the per-source game names of the passes (``game_by_folder`` /
        ``game_by_archive`` always, ``game_by_modland`` / ``_modland_filename``
        / ``_songdb`` while ``file_md5`` is unchanged, ``game_by_demozoo``
        while ``artist`` and ``title`` are) — the header's (``game_by_tag``)
        is the fresh extract's."""
    # Store-only hand edits (non-taggable formats) are restored FIRST, so the
    # identity comparisons below see the user's corrected artist rather than the
    # file's stale one — otherwise editing the Artist would compute same_artist
    # against the file value and wrongly DROP this track's scene_group and
    # demozoo year on the next rescan.  ('year' rides its own provenance below,
    # so it's excluded here to avoid a double-write.)
    edited = old.get("user_edited")
    if isinstance(edited, list) and edited:
        for f in edited:
            if f != "year" and f in old:
                new[f] = old[f]
        new["user_edited"] = list(edited)
    same_md5 = old.get("file_md5") == new.get("file_md5")
    # (the song-database test runs on the fresh extract BEFORE a Modland
    # credit is restored — the two can be the same name)
    sdb_kept = ([f for f in ("artist", "label") if songdb_fill_kept(old, new, f)]
                if old.get("songdb_fields") and not new.get("songdb_fields") else [])
    if modland_artist_kept(old, new):
        new["artist"] = old["artist"]
    for f in sdb_kept:
        new[f] = old[f]
    if sdb_kept:
        new["songdb_fields"] = sdb_kept
    same_artist = old.get("artist") == new.get("artist")
    if old.get("scene_group") and not new.get("scene_group") and same_artist:
        new["scene_group"] = old["scene_group"]
    if old.get("scene_path") and not new.get("scene_path") and same_md5:
        new["scene_path"] = old["scene_path"]
    a_src = old.get("album_source")
    if ((old.get("album") or "").strip() and not (new.get("album") or "").strip()
            and (a_src == "folder"
                 or (a_src in ("modland", "modland-filename", "songdb") and same_md5))):
        new["album"] = old["album"]
        new["album_source"] = a_src
    elif (a_src in ("modland", "modland-filename", "songdb", "folder")
          and (same_md5 or not old.get("file_md5")) and new.get("album")
          and _spc_name_completes(old.get("album") or "", new["album"],
                                  new.get("format") or old.get("format"))):
        # A derived album (Modland, the song database, the folder name)
        # completes this SPC's 32-byte-cut header game; the rescan read the
        # cut name again — keep the completion (as ``repair._changed_fields``).
        new["album"] = old["album"]
        new["album_source"] = a_src
    src = old.get("year_source")
    if new.get("year_source") in (None, "") and \
            (src == "user" or (src == "demozoo" and same_artist)
             or (src == "songdb" and same_md5 and new.get("year") is None)):
        new["year"] = old.get("year")
        new["year_source"] = src
        new["year_file"] = old.get("year_file")
    # The per-source game names of the passes (the header's comes with the
    # fresh extract): the folder / archive name's always (the track id is keyed
    # on the path), the Modland / song-database ones while ``file_md5`` is
    # unchanged.
    same_title = old.get("title") == new.get("title")
    for f, keep in (("game_by_folder", True), ("game_by_archive", True),
                    ("game_by_demozoo", same_artist and same_title),
                    # the Demozoo use / party: keyed on composer + title too
                    ("scene_use", same_artist and same_title),
                    ("scene_party", same_artist and same_title),
                    # derived genres: the next derived-genre pass recomputes
                    # them from the rescanned track (core/genre_derive.py) —
                    # carried so an unchanged file is no change
                    ("genre_derived", True),
                    ("game_by_modland", same_md5),
                    ("game_by_modland_filename", same_md5),
                    ("game_by_songdb", same_md5)):
        if keep and old.get(f) and not new.get(f):
            new[f] = old[f]
    # A reported playback problem (core/skip_rules.py) is about the file's
    # bytes: carried while its checksum is unchanged (a fixed file replacing a
    # broken one at the same path loses the mark) and the fresh extract has none.
    if (old.get("health") and not new.get("health") and same_md5
            and old.get("sid_md5") == new.get("sid_md5")):
        for f in ("health", "health_note", "health_by", "health_at", "health_renderer"):
            if old.get(f) is not None:
                new[f] = old[f]
    # A renderer's refusal found on play (core/unplayable.py): carried while
    # it is the same file — the checksum (or size) the mark was made on.
    from soniqboom.core import unplayable as _unplayable
    _unplayable.carry(old, new)
    _carry_default_tune(old, new)
    new.update(game_follow(new))                # the game follows the carried album


def same_file_version(old: dict, new: dict) -> bool:
    """Is the freshly extracted ``new`` the same file as stored ``old`` — the
    same size, and no content hash (``file_md5`` / ``sid_md5``) that differs
    where both have one?  (A touch, or a field the extractor now reads
    differently, changes neither.)"""
    return (old.get("file_size") == new.get("file_size")
            and not any(old.get(h) and new.get(h) and old[h] != new[h]
                        for h in ("file_md5", "sid_md5")))


def _carry_default_tune(old: dict, new: dict) -> None:
    """A multi-tune file's probed default tune (``default_subsong``, written at
    its first bare play — no extract ever produces it; 0 is a value) carried
    onto a fresh extract of the SAME file (``same_file_version``) whose tune
    count still covers it: dropped, a re-extract that changed another field
    (a title the extractor now reads better) made the next bare play probe
    again.  A default past the first tune keeps the length stored with it —
    that tune's (or the placeholder its first render measures), where the
    extract has the first tune's."""
    z = old.get("default_subsong")
    if (not isinstance(z, int) or isinstance(z, bool) or z < 0
            or new.get("default_subsong") is not None or not same_file_version(old, new)):
        return
    n = new.get("subsongs")
    if not (isinstance(n, int) and not isinstance(n, bool) and 1 < n and z < n):
        return                          # the fresh extract knows no tune count that covers it
    new["default_subsong"] = z
    if z and "duration" in old:
        new["duration"] = old["duration"]


class PrimarySwap:
    """The duplicate groups whose primary can't play right now while another
    member can — what a list that hides duplicate copies shows instead: that
    group's best playable member (by ``duplicates.primary_sort_key``, the
    order the primary itself was picked by).  Built by
    ``TrackStore.primary_swap`` from the stored primaries, never written to a
    track: when the primary's drive is back, the next swap leaves it out.
    Immutable; callers compare it by identity (memo keys)."""

    __slots__ = ("demoted", "promoted", "changed", "stand_in")

    def __init__(self, stand_in: dict[str, str]) -> None:
        self.stand_in = stand_in                        # primary that can't play → its stand-in
        self.demoted = frozenset(stand_in)
        self.promoted = frozenset(stand_in.values())
        self.changed = self.demoted | self.promoted

    def is_primary(self, tid: str, t: dict) -> bool:
        """Whether the track ``tid`` (its dict ``t``) is the one a deduped list
        shows of its group: a stand-in, or a primary that is not stood in for."""
        if tid in self.promoted:
            return True
        return t.get("is_duplicate_primary", True) and tid not in self.demoted


class _FilterWalk:
    """One list order: the ids of sorted index ``idx``, walked in one
    direction (``desc``: from its end), that pass a membership test —
    collected as far as the deepest page so far needed them (``out``) and
    resumed from ``pos`` for a deeper one.  The test (``mode``):

    * ``_IN`` — in ``member``: a ``filter_tracks`` candidate set (``exclude``
      already left out); exactly the ids, in the order, that its per-request
      walk of the index collects;
    * ``_OUT_LISTED`` — not in ``member`` (the ids a list hides,
      ``TrackStore._invisible``) and in ``tracks``: the deep-page orders of a
      deduped list (``_primary_order`` / ``_excluded_order``);
    * ``_OUT`` — not in ``member`` (the ``exclude`` ids).

    ``filter_tracks`` keeps its walks in ``_filter_walk_memo``: valid while
    ``gen`` (``TrackStore._primary_order_key``), the index list object and
    the ``exclude`` / ``swap`` objects are the ones it was built with —
    ``TrackStore._filter_walk_get`` checks."""

    __slots__ = ("idx", "gen", "exclude", "swap", "member", "desc", "pos", "out", "done",
                 "mode", "tracks")

    _IN, _OUT_LISTED, _OUT = 0, 1, 2

    def __init__(self, idx: list, gen, exclude, swap, member, desc: bool,
                 mode: int = 0, tracks=None) -> None:
        self.idx = idx
        self.gen = gen
        self.exclude = exclude
        self.swap = swap
        self.member = member
        self.desc = desc
        self.mode = mode
        self.tracks = tracks
        self.pos = len(idx) if desc else 0
        self.out: list[str] = []
        self.done = not idx

    def upto(self, need: int) -> list[str]:
        """``out`` holding at least ``need`` ids, or every one the walk finds.
        The index is read in slices that double in size, filtered by a list
        comprehension each (a short page reads a few thousand rows)."""
        out = self.out
        if len(out) >= need or self.done:
            return out
        idx, member, mode, tracks = self.idx, self.member, self.mode, self.tracks
        n = len(idx)
        step = max(_FILTER_WALK_STEP, 2 * (need - len(out)))
        while len(out) < need and not self.done:
            if self.desc:
                hi = self.pos
                lo = hi - step if hi > step else 0
                seg = idx[lo:hi]
                seg.reverse()
                self.pos = lo
                self.done = lo == 0
            else:
                lo = self.pos
                hi = lo + step if lo + step < n else n
                seg = idx[lo:hi]
                self.pos = hi
                self.done = hi == n
            if mode == 0:
                out += [tid for _v, tid in seg if tid in member]
            elif mode == 1:
                out += [tid for _v, tid in seg if tid not in member and tid in tracks]
            else:
                out += [tid for _v, tid in seg if tid not in member]
            step *= 2
        return out

    def page(self, offset: int, limit: int) -> list[str]:
        """``limit`` ids from position ``offset`` of the order (both ≥ 0)."""
        return self.upto(offset + limit)[offset:offset + limit]

    def full(self) -> list[str]:
        """The whole order (walked to the end)."""
        return self.upto(len(self.idx) + 1)


class TrackStore:
    """Central in-memory data store with indexed search."""

    def __init__(self) -> None:
        # ── Primary data ─────────────────────────────────────────────────
        self._tracks: dict[str, dict] = {}
        self._waveforms: dict[str, list[float]] = {}
        self._ratings: dict[str, int] = {}
        self._play_stats: dict[str, dict] = {}
        # Bumped on every rating / play change so the /smart/top-rated and
        # /smart/most-played ranked-list memos invalidate WITHOUT touching the
        # library-wide _mutation_seq (which play/rating writes deliberately
        # don't bump — they're far hotter than structural mutations).
        self._rating_seq: int = 0
        self._play_seq: int = 0
        self._playlists: dict[str, dict] = {}
        self._history: list[dict] = []
        self._scan_dirs: dict[str, dict] = {}
        self._hash_lookups: dict[str, str] = {}
        self._config: dict[str, Any] = {}
        self._art_absent: set[str] = set()
        # Mutation sequence number — bumps on every track upsert / field
        # update / delete so caches keyed on store state can detect
        # changes even when the *count* doesn't move (rescan, retag,
        # set-primary).  A field update that changes nothing, only fields in
        # ``_SEQ_EXEMPT_FIELDS`` (``cover_art``), or only ``duration``
        # (``_duration_seq`` below) does not bump it.
        self._mutation_seq: int = 0
        # Narrower sequence for the Subsonic catalogue: inserts / deletes and
        # changes to ``_CATALOG_FIELDS`` only.
        self._catalog_seq: int = 0
        # Duration writes no browse surface sees otherwise
        # (``_browse_duration_only``): the duration-only ones
        # (``_duration_only``), which bump neither sequence above, and those
        # with fields only non-browse consumers read (``tune_lengths``,
        # ``start_subsong``), which still bump them.
        self._duration_seq: int = 0
        # Narrower sequence for what the browse lists, counts, legends and
        # orders show (``_browse_change``): inserts / deletes, a change to a
        # field outside ``_BROWSE_EXEMPT_FIELDS`` (a duration write with only
        # such fields beside it — ``_browse_duration_only`` — moves
        # ``_duration_seq`` instead), a sorted list replaced (a batch exit, a
        # rebuild) and an index swap.  The browse memos key on it — the store's
        # aggregate / order / swap memos and the library legend cache — so
        # a play-time backfill, a health mark or an art write keeps them warm;
        # ``_mutation_seq`` keeps its meaning for every other consumer
        # (core/skip_rules, the Subsonic caches, smart.py, the sweep).
        self._browse_seq: int = 0
        # Per-track write counter: bumped by every upsert and every in-place
        # field write, whatever the field (seq-exempt ones included), so a
        # per-track memo (the Subsonic song memo) validates an entry with one
        # lookup.  Never reset — an id that comes back counts on.
        self._track_rev: dict[str, int] = {}
        # Tracks upserted since the heap was last frozen (``FREEZE_AFTER_UPSERTS``).
        self._upserts_since_freeze: int = 0

        # ── Inverted word index ──────────────────────────────────────────
        self._word_index: dict[str, set[str]] = {}
        self._word_list: list[str] = []
        self._word_list_dirty = False
        # The words that appeared in / vanished from ``_word_index`` since
        # ``_word_list`` was last sorted (``_word_appeared`` /
        # ``_word_vanished``): the next ``_rebuild_word_list`` merges them in
        # instead of sorting the whole vocabulary again, and a word that goes
        # and comes back (a re-upsert) is no change at all.
        # ``_word_list_full``: no such account (after a clear or an index
        # swap) — the next one sorts everything.
        self._word_fresh: set[str] = set()
        self._word_gone: set[str] = set()
        self._word_list_full = True             # (an empty list: the first sort is a full one)

        # ── Tag indexes (lowered value → set of track IDs) ──────────────
        self._tag_artist: dict[str, set[str]] = {}
        self._tag_album_artist: dict[str, set[str]] = {}
        self._tag_album: dict[str, set[str]] = {}
        self._tag_genre: dict[str, set[str]] = {}
        self._tag_format: dict[str, set[str]] = {}
        self._tag_dir_hash: dict[str, set[str]] = {}
        # Duplicate-relevant changes not yet re-grouped (see _DUP_FIELDS).
        self._dup_dirty: dict[str, dict | None] = {}
        self._dup_dirty_overflow = False
        self._on_dup_dirty = None           # callback: the pending set became non-empty
        # Enrichment change log (see _ENRICH_EXEMPT_FIELDS): track id → the
        # number of its latest change, oldest first.  Changes up to
        # ``_enrich_floor`` are no longer listed (dropped at the cap); a new
        # ``_enrich_epoch`` (every load) invalidates every pass's position.
        self._enrich_seq = 0
        self._enrich_log: dict[str, int] = {}
        self._enrich_floor = 0
        self._enrich_epoch = object()
        # Called after every new change-log entry (``set_enrich_listener``).
        self._enrich_listener: Callable[[], None] | None = None
        self._tag_scan_root_hash: dict[str, set[str]] = {}
        # Per scan root: bumped whenever a track is added to, removed from or
        # moved within it (its path changed) — the availability snapshot keys
        # on it (``core/availability._signature``), so an add + a delete that
        # leave the root's count unchanged still re-snapshot.  A version, not
        # an index: monotonic, never cleared, not in ``INDEX_ATTRS``.
        self._root_rev: dict[str, int] = {}
        self._tag_dup_group: dict[str, set[str]] = {}
        # Scene-group (Demozoo) index: each collective a retro composer belonged
        # to → the track ids by that composer.  Lets ``similar_candidates`` pool
        # SAME-COLLECTIVE tracks (different composer/format) that the artist/
        # genre/format buckets miss, so the retro scorer's group signal
        # (radio._W_R_GROUP) actually has candidates to fire on.  A track's
        # ``scene_group`` is the multi-group string ``"Fairlight • Maniacs of
        # Noise"``; each group is indexed separately (see ``_scene_group_keys``).
        self._tag_scene_group: dict[str, set[str]] = {}
        # ``game`` → ids, keyed on the game's ``_game_key`` (lower-cased,
        # whitespace collapsed, accents folded) — the ``game:`` operator's
        # prefix match walks these keys (a few thousand distinct games).
        self._tag_game: dict[str, set[str]] = {}

        # ── Inverted instrument/sample-token index (retro sample lineage) ────
        # ``token → track-ids`` for the retro tracker-sample Jaccard signal
        # (radio._W_R_SAMPLES).  Recomputing ``instrument_tokens`` for every
        # candidate on each "more like this" request cost ~35 ms over a ~5 K
        # pool (measured, 262 K library); this precomputes it once so the retro
        # scorer reads a shared-token count instead.  Singleton tokens (present
        # in exactly ONE track) are pruned — they can never be shared, so
        # dropping them changes no Jaccard value (lossless) while removing the
        # bulk of the vocabulary.  Batch-built (needs global counts to know
        # singletons), NOT maintained per-track: rebuilt by ``rebuild_indexes``/
        # ``finish_rebuild`` and swapped in by ``data.rebuild_indexes`` (the
        # ``index_health`` sweep triggers one after mutations; a restart always
        # rebuilds).  ``retro_sample_jaccard`` falls back to on-the-fly
        # tokenisation for a candidate NOT yet in the count map (added since the
        # last build).  Caveat: a track whose ``instruments`` change IN PLACE
        # (same id, re-scan) stays in the count map, so its sample sub-signal
        # can lag until the next rebuild — a bounded, self-healing imprecision
        # on ONE retro signal, never a wrong-by-more-than-that or a crash.
        self._sim_tok_postings: dict[str, list[str]] = {}   # non-singleton token → track ids
        self._sim_tok_count: dict[str, int] = {}            # retro-with-samples track id → |tokens|

        # ── Sorted indexes: list of (value, track_id) ───────────────────
        self._sorted_year: list[tuple[int | float, str]] = []      # (float: _NO_VALUE_KEY)
        self._sorted_added_at: list[tuple[int, str]] = []
        # Parallel index tracking only primary (non-secondary-dup) tracks so
        # ``filter_tracks(filter_duplicates=True)`` and ``_paginate_all`` can
        # walk a shorter pre-filtered list rather than testing every entry's
        # ``is_duplicate_primary`` flag per request.
        self._sorted_added_at_primary: list[tuple[int, str]] = []
        self._sorted_duration: list[tuple[float, str]] = []
        self._sorted_bpm: list[tuple[int | float, str]] = []
        # Lexical sort indexes for column-header sort in the All Tracks
        # windowed view.  Keys are lower-cased so the order is locale-
        # agnostic case-insensitive — matches the user's expectation that
        # "ABBA" and "abba" sort adjacent.  Cost: ~2 MB each at 267K
        # tracks (interned string pointers + tuple overhead), ~10 MB
        # total for the four new indexes vs the 280 MB snapshot already
        # in RAM.  Insert/remove cost: bisect O(log N) per index, same
        # shape as the numeric sort indexes above.
        self._sorted_title: list[tuple[str, str]] = []
        self._sorted_artist: list[tuple[str, str]] = []
        self._sorted_album_artist: list[tuple[str, str]] = []
        self._sorted_album: list[tuple[str, str]] = []
        self._sorted_format: list[tuple[str, str]] = []
        # ``game:`` title side list: only the titles whose folded,
        # whitespace-collapsed key differs from their lexical one (accented or
        # oddly spaced), keyed on that folded key (``_game_fold_value``).
        self._sorted_title_fold: list[tuple[str, str]] = []

        # ── Browse id sets (maintained on every write, like the tag indexes) ──
        # So the lists, counts and legends leave out what a list hides with set
        # arithmetic (in C) instead of a per-row test: the non-primary
        # duplicate copies (``not t.get("is_duplicate_primary", True)``), the
        # tracks with a blank artist / album artist / album / genre (the
        # "[No Artist]" buckets — ``_untagged``), and year → ids keyed like
        # ``_agg_years`` (``_agg_year``; no year: in none).
        self._nonprimary_ids: set[str] = set()
        self._untagged_artist: set[str] = set()
        self._untagged_album_artist: set[str] = set()
        self._untagged_album: set[str] = set()
        self._untagged_genre: set[str] = set()
        self._tag_year: dict[Any, set[str]] = {}

        # ── Pre-computed aggregations ────────────────────────────────────
        self._agg_artists: Counter[str] = Counter()
        self._agg_album_artists: Counter[str] = Counter()
        self._agg_albums: Counter[str] = Counter()
        self._agg_genres: Counter[str] = Counter()
        self._agg_years: Counter[int] = Counter()
        self._agg_albums_by_artist: dict[str, Counter[str]] = {}
        self._agg_albums_by_album_artist: dict[str, Counter[str]] = {}

        # ── Memoised aggregations keyed on _browse_seq ───────────────────
        # Each entry is ``(seq, value)`` so a single ``_browse_seq`` bump
        # invalidates every cached aggregation in lock-step.
        self._agg_cache: dict[str, tuple[int, Any]] = {}
        # ``_primary_order`` memo: sorted-index name → (the list it was built
        # from, ``_primary_order_key``, primary-only ids in list order).
        self._primary_order_memo: dict[str, tuple[list, tuple, list[str]]] = {}
        # ``_excluded_order`` memo: (sorted-index name, duplicates hidden,
        # exclude set, primary swap) → (the list, ``_primary_order_key``, the
        # ``exclude`` set, ids in list order without the excluded ones — with
        # duplicates hidden, the effective primaries' — the ``swap``).  For the
        # sets in use (``core.availability``: the tracks hidden while their
        # drive is offline, the copies standing in for primaries that can't
        # play): a stale entry is dropped on the next build.
        self._excluded_order_memo: dict[tuple, tuple] = {}
        # Exclude-aware aggregate memo: key → (``_browse_seq``, the exclude
        # set it was computed without, value, the primary swap it used) — see
        # ``_agg_x_get``.
        self._agg_x_cache: dict[str, tuple[int, Any, Any, Any]] = {}
        # ``primary_swap`` memo: (the unplayable set, the swap or None, {group
        # key: (its primaries, their stand-in)}), and the groups written since
        # it was built (``_swap_touch``; None: build it all again).
        self._primary_swap_memo: tuple | None = None
        self._swap_dirty: set[str] | None = None
        # ``_dup_invisible`` memo: (``_browse_seq``, the swap, the ids a
        # deduped list leaves out with it).
        self._dup_inv_memo: tuple | None = None
        # ``_invisible`` memo: (``_browse_seq``, the exclude set, the swap, the
        # ids a deduped list leaves out with both).
        self._inv_memo: tuple | None = None
        # ``filter_tracks`` orders (``_FilterWalk``), least recently used first:
        # (predicates, duplicates hidden, id(exclude), id(swap), sorted-index
        # name, descending) → walk (which holds the exclude set and the swap:
        # an id is not reused while its entry lives).
        self._filter_walk_memo: dict[tuple, _FilterWalk] = {}
        # The thread that writes the store (the event loop's — the one that
        # created or loaded it, ``bulk_load``).  Only it keeps, extends or
        # stores the browse memos — the walks, the incremental swap state, the
        # hidden-id sets, the aggregate memos — and the sorted word list
        # (``_memo_ok``): a read off the loop (DLNA queries in a worker
        # thread) computes its answer privately (it may read a valid memo).
        self._owner_thread = threading.get_ident()
        # Called (no arguments) after a scan dir was added or removed or its
        # reachability ``status`` changed — ``core.availability`` listens.
        self._on_scan_dirs_changed: Callable[[], None] | None = None

        # ── Track IDs that have never been played ────────────────────────
        # Mirrors `play_stats` keys; updated on every track upsert / delete
        # and on `record_play`.  Lets api/smart.py serve the "unplayed"
        # view by walking ``_sorted_added_at`` filtered through this set
        # instead of scanning every track in the library.
        self._unplayed_ids: set[str] = set()

        # ── AOF hook (set by aof module after init) ──────────────────────
        self._aof_append: Callable[..., None] | None = None
        # Called as ``(track id, the track before the write or None)`` before
        # every track upsert, delete and field write while an index audit
        # runs (``core/index_health``) — None otherwise.
        self._write_watch: Callable[[str, "dict | None"], None] | None = None

        # ── Batch mode: defer O(n) sorted-list rebuilds ─────────────────
        # Reference-counted so concurrent scan commits on the ONE shared store
        # (the local re-scan delta-apply + each remote freshness scan, which
        # run on the same event loop with no lock between them) nest safely:
        # only the OUTERMOST exit rebuilds.  With a plain boolean a nested
        # commit reset ``_sorted_dirty`` / flipped ``_batch_mode`` out from
        # under an outer batch section, dropping its deferred rebuild so those
        # tracks vanished from every sorted/windowed browse view until restart.
        self._batch_mode: bool = False
        self._batch_depth: int = 0
        # Names of the sorted lists (``SORTED_LISTS``) whose deferred rebuild is
        # pending.  An insert/delete marks all of them for a FULL rebuild; a
        # field update marks only the lists whose key changed and records each
        # re-keyed track id with its PRE-batch key in ``_dirty_sorted_tids`` —
        # such a list is rebuilt by cutting those entries out of its (untouched)
        # pre-batch contents and merging the fresh ones back in, O(n + k log k)
        # instead of a full O(n log n) re-sort.  A name in ``_dirty_sorted``
        # without a ``_dirty_sorted_tids`` entry = full rebuild.
        # ``_sorted_dirty`` is the boolean view (setting True marks all full).
        self._dirty_sorted: set[str] = set()
        self._dirty_sorted_tids: dict[str, dict[str, Any]] = {}

        self.history_max = 500

    @property
    def _sorted_dirty(self) -> bool:
        return bool(self._dirty_sorted)

    @_sorted_dirty.setter
    def _sorted_dirty(self, value: bool) -> None:
        if value:
            self._mark_all_sorted_dirty()
        else:
            self._dirty_sorted.clear()
            self._dirty_sorted_tids.clear()

    def _mark_all_sorted_dirty(self) -> None:
        self._dirty_sorted.update(_ALL_SORTED)
        self._dirty_sorted_tids.clear()

    def _note_sorted_batch(self, tid: str, old: "dict | None") -> None:
        """Batch mode: record track ``tid`` for every sorted list's deferred
        merge (``_dirty_sorted_tids``) with the key it has in that list's
        pre-batch contents — ``old``'s (the track as the lists hold it), None
        when it is in none of them (a track added in this batch).  The first
        record per track and list is kept: a later write in the same batch
        leaves the pre-batch contents as they are.  Once a list has more
        tracks recorded than ``_SORTED_MERGE_MIN`` or 1/``_SORTED_MERGE_DIV``
        of the library, every list is rebuilt in full instead."""
        dirty = self._dirty_sorted
        tids = self._dirty_sorted_tids
        if not tids and len(dirty) == _N_SORTED:
            return                              # every list is rebuilt in full anyway
        over = False
        cap = None
        for name, (_fields, value) in _SORTED_SPECS.items():
            pending = tids.get(name)
            if pending is None:
                if name in dirty:
                    continue                    # (this list is rebuilt in full)
                dirty.add(name)
                pending = tids[name] = {}
            if tid not in pending:
                pending[tid] = None if old is None else value(old)
                if len(pending) > _SORTED_MERGE_MIN:
                    if cap is None:
                        cap = max(_SORTED_MERGE_MIN, len(self._tracks) // _SORTED_MERGE_DIV)
                    over = over or len(pending) > cap
        if over:
            self._mark_all_sorted_dirty()

    def merged_sorted_list(self, name: str, changed: dict) -> "list[tuple] | None":
        """Sorted list ``name`` with the tracks of ``changed`` (``{track id:
        its key in the list now, None = not in it}``, a ``sorted_rebuild_plan``
        entry) moved to their current keys — the list itself when none of
        them moves.  A new list, sorted: the entries between two changes are
        copied as slices (in C), each change is located by bisection —
        O(n + k log n) for k changes, no sort of the n entries.  None when an
        entry is not where its recorded key says, a changed track is listed
        twice, or keys don't compare: the caller rebuilds that list in full.
        (``iter_merged_sorted_list`` run to its end.)"""
        return _run_generator(self.iter_merged_sorted_list(name, changed))

    def iter_merged_sorted_list(self, name: str, changed: dict, step: int = 0):
        """``merged_sorted_list`` as a generator — its result is the
        ``StopIteration`` value; with ``step``, it pauses (yields None) after
        every ``step`` changes, positions and assembly runs, so an async
        caller can give the loop a turn between slices of a large merge, and
        yields ``ASSEMBLE`` once, before it assembles the new list.  The list
        must not be written meanwhile (batch mode keeps it as it is); replaced
        meanwhile, the result is None."""
        cur = getattr(self, name)
        value = _SORTED_SPECS[name][1]
        tracks = self._tracks
        bl = bisect.bisect_left
        n = len(cur)
        items = list(changed.items())
        width = step or max(1, len(items))
        cuts: list[int] = []
        fresh: list[tuple] = []
        try:
            for j in range(0, len(items), width):
                for tid, ov in items[j:j + width]:
                    t = tracks.get(tid)
                    nv = value(t) if t is not None else None
                    if ov is not None:
                        if nv is not None and nv == ov:
                            continue            # (unchanged entry: stays where it is)
                        e = (ov, tid)
                        i = bl(cur, e)
                        if i >= n or cur[i] != e:
                            return None
                        cuts.append(i)
                    if nv is not None:
                        fresh.append((nv, tid))
                if step:
                    yield
            if not cuts and not fresh:
                return cur
            if step and len(fresh) > 4 * step:
                # (sorted in runs, merged a slice at a time)
                runs = []
                for j in range(0, len(fresh), 4 * step):
                    r = fresh[j:j + 4 * step]
                    r.sort()
                    runs.append(r)
                    yield
                merged = heapq.merge(*runs)
                fresh = []
                while True:
                    part = list(itertools.islice(merged, 4 * step))
                    if not part:
                        break
                    fresh.extend(part)
                    yield
            else:
                fresh.sort()
            cut_set = set(cuts)
            ins: list[int] = []
            width = step or max(1, len(fresh))
            for j in range(0, len(fresh), width):
                for e in fresh[j:j + width]:
                    p = bl(cur, e)
                    if p < n and p not in cut_set and cur[p] == e:
                        return None             # (already listed: a duplicate entry)
                    ins.append(p)
                if step:
                    yield
        except TypeError:
            return None
        cuts.sort()
        if step:
            yield ASSEMBLE
        if len(cuts) + len(ins) <= _SORTED_MERGE_INPLACE:
            # A few changes: one copy, then each change in place from the
            # back (a memmove of the tail each) — one allocation instead of
            # a slice per run between changes.
            if step and getattr(self, name) is not cur:
                return None
            out = cur.copy()
            events = sorted([(c, 1, None) for c in cuts]
                            + [(p, 0, i) for i, p in enumerate(ins)], reverse=True)
            for pos, kind, i in events:
                if kind:
                    del out[pos]
                else:
                    out.insert(pos, fresh[i])
            return out
        out: list[tuple] = []
        prev = ci = ii = 0
        nc, ni = len(cuts), len(ins)
        done = 0
        while ci < nc or ii < ni:
            if ii < ni and (ci >= nc or ins[ii] <= cuts[ci]):
                p = ins[ii]
                if p > prev:
                    out += cur[prev:p]
                    prev = p
                out.append(fresh[ii])
                ii += 1
            else:
                c = cuts[ci]
                if c > prev:
                    out += cur[prev:c]
                prev = c + 1
                ci += 1
            done += 1
            if step and done % step == 0:
                yield
        out += cur[prev:]
        if step and getattr(self, name) is not cur:
            return None                         # (replaced meanwhile)
        return out

    def sorted_merge_cap(self) -> int:
        """How many recorded tracks a sorted list's deferred update is merged
        for (``iter_merged_sorted_list``); past it, the list is filtered
        instead (``iter_filtered_sorted_list``)."""
        return max(_SORTED_MERGE_MIN, len(self._tracks) // _SORTED_MERGE_DIV)

    def iter_filtered_sorted_list(self, name: str, changed, step: int = 0):
        """Sorted list ``name`` with EVERY entry of the tracks in ``changed``
        (track ids) taken out and their current entries merged back in — the
        update of a list a batch re-keyed many of its tracks in (past
        ``sorted_merge_cap``), and the fallback of a merge that found the list
        out of step (a stale duplicate is dropped too).  O(n + k log k), the
        untouched entries' tuples reused.  A generator like
        ``iter_merged_sorted_list``: the result is the ``StopIteration``
        value; with ``step`` it pauses after every slice of about ``4 * step``
        entries and yields ``ASSEMBLE`` once its new entries are made.  None
        when keys don't compare (the caller builds the list from every track)
        or the list was replaced meanwhile."""
        cur = getattr(self, name)
        value = _SORTED_SPECS[name][1]
        tracks = self._tracks
        chg = changed if isinstance(changed, (set, frozenset, dict)) else set(changed)
        width = 4 * step if step else max(1, len(cur), len(chg))
        fresh: list[tuple] = []
        ids = list(chg)
        for j in range(0, len(ids), width):
            for tid in ids[j:j + width]:
                t = tracks.get(tid)
                if t is not None:
                    v = value(t)
                    if v is not None:
                        fresh.append((v, tid))
            if step:
                yield
        if step:
            yield ASSEMBLE
        kept: list[tuple] = []
        for j in range(0, len(cur), width):
            kept += [e for e in cur[j:j + width] if e[1] not in chg]
            if step:
                yield
        try:
            if not step:
                kept += fresh
                kept.sort()                     # (two sorted runs: one merge)
                return kept
            runs = []
            for j in range(0, len(fresh), width):
                r = fresh[j:j + width]
                r.sort()
                runs.append(r)
                yield
            if not runs:
                out = kept
            else:
                merged = heapq.merge(kept, *runs)
                out = []
                while True:
                    part = list(itertools.islice(merged, width))
                    if not part:
                        break
                    out += part
                    yield
        except TypeError:
            return None
        if getattr(self, name) is not cur:
            return None                         # (replaced meanwhile)
        return out

    def sorted_rebuild_plan(self) -> dict[str, dict[str, Any] | None]:
        """Snapshot of the pending sorted-list rebuilds: name → ``{track id:
        its pre-batch key (None = was not in the list)}`` to merge, or None
        for a full rebuild."""
        tids = self._dirty_sorted_tids
        return {n: (dict(tids[n]) if n in tids else None) for n in self._dirty_sorted}

    def _memo_ok(self) -> bool:
        """Whether this thread may use the kept walks / swap state
        (``_owner_thread``)."""
        return threading.get_ident() == self._owner_thread

    def _sorted_rebuilt(self, names) -> None:
        """Mark ``names`` rebuilt (their pending state is cleared) and drop
        their ``_primary_order`` memo entries, which hold the replaced lists."""
        for n in names:
            self._dirty_sorted.discard(n)
            self._dirty_sorted_tids.pop(n, None)
            self._primary_order_memo.pop(n, None)
        self._excluded_order_memo.clear()
        # A replaced list can change a ``game:`` / year predicate's ids (they
        # read the sorted lists) with no write of its own.
        self._browse_seq += 1
        self._filter_walk_memo.clear()

    def browse_changed(self) -> None:
        """Invalidate every browse memo (``_browse_seq``) and drop the ones
        holding index lists — after the indexes were swapped
        (``data.rebuild_indexes``) or what a list may show changed outside a
        track write (the Subsonic visibility of a catalogue)."""
        self._browse_seq += 1
        self._filter_walk_memo.clear()
        self._primary_order_memo.clear()
        self._excluded_order_memo.clear()
        self._swap_dirty = None
        # (the word index may have been swapped with its list: no account of
        # what changed since that list was sorted)
        self._word_fresh.clear()
        self._word_gone.clear()
        self._word_list_full = self._word_list_dirty

    # ── AOF helper ───────────────────────────────────────────────────────

    def _aof(self, op: str, **kwargs: Any) -> None:
        if self._aof_append:
            self._aof_append(op, **kwargs)

    # ── Index maintenance ────────────────────────────────────────────────

    def _index_track(self, tid: str, t: dict) -> None:
        """Add a track to all indexes."""
        wi = self._word_index
        for token in _tokenize_track(t):
            s = wi.get(token)
            if s is None:
                wi[token] = {tid}
                self._word_appeared(token)
            else:
                s.add(tid)

        for k in _name_keys(t.get("artist")):
            self._tag_set(self._tag_artist, k, tid)
        for k in _name_keys(t.get("album_artist")):
            self._tag_set(self._tag_album_artist, k, tid)
        self._tag_set(self._tag_album, t.get("album", ""), tid)
        self._tag_set(self._tag_format, t.get("format", ""), tid)
        self._tag_set(self._tag_dir_hash, t.get("dir_hash", ""), tid)
        self._tag_set(self._tag_scan_root_hash, t.get("scan_root_hash", ""), tid)
        self._bump_root_rev(t.get("scan_root_hash", ""))
        gid = t.get("duplicate_group_id")
        if gid:
            self._tag_set(self._tag_dup_group, gid, tid)
            self._swap_touch(gid)
        genres = track_genres(t)
        for g in genres:
            self._tag_set(self._tag_genre, g, tid)
        for grp in _scene_group_keys(t.get("scene_group")):
            self._tag_set(self._tag_scene_group, grp, tid)
        for k in _game_keys(t):
            self._tag_set(self._tag_game, k, tid)

        # Extract numeric fields used by both sorted indexes and aggregations.
        # ``normalise_year`` collapses YYYYMMDD-form ints down to YYYY so the
        # sorted index agrees with the aggregation Counter — the same rule
        # is reused by scanner._async_exit_batch_mode via the module helper.
        year = _agg_year(t)

        # Browse id sets (eager, also in batch mode — they are not sorted).
        if not t.get("is_duplicate_primary", True):
            self._nonprimary_ids.add(tid)
        if _blank_tag(t.get("artist")):
            self._untagged_artist.add(tid)
        if _blank_tag(t.get("album_artist")):
            self._untagged_album_artist.add(tid)
        if _blank_tag(t.get("album")):
            self._untagged_album.add(tid)
        if _blank_tag(genres):
            self._untagged_genre.add(tid)
        if year is not None:
            ys = self._tag_year.get(year)
            if ys is None:
                self._tag_year[year] = {tid}
            else:
                ys.add(tid)

        if not self._batch_mode:
            # Lexical sort keys for the column-header sort (windowed view).
            # Lower-cased so ABBA and abba sort adjacent; empty values fall
            # to the end of asc / start of desc — we use the ``￿``
            # sentinel for empties to keep that behaviour without a separate
            # filter pass at query time.  Using a high-BMP unicode codepoint
            # not present in real metadata.  (The numeric keys come from their
            # value functions: a missing year / duration / bpm likewise keys
            # ``_NO_VALUE_KEY``, a missing ``added_at`` 0.)
            EMPTY_SORT_KEY = "￿"
            title_key        = (t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY
            artist_key       = _lex_key_shared(t.get("artist"))
            album_artist_key = _lex_key_shared(t.get("album_artist"))
            album_key        = _lex_key_shared(t.get("album"))
            fmt_key          = _lex_key_shared(t.get("format"))
            _sorted_insert(self._sorted_year, (_year_value(t), tid))
            added = _added_value(t)
            _sorted_insert(self._sorted_added_at, (added, tid))
            if t.get("is_duplicate_primary", True):
                _sorted_insert(self._sorted_added_at_primary, (added, tid))
            _sorted_insert(self._sorted_duration, (_duration_value(t), tid))
            _sorted_insert(self._sorted_bpm, (_bpm_value(t), tid))
            _sorted_insert(self._sorted_title,        (title_key,        tid))
            _sorted_insert(self._sorted_artist,       (artist_key,       tid))
            _sorted_insert(self._sorted_album_artist, (album_artist_key, tid))
            _sorted_insert(self._sorted_album,        (album_key,        tid))
            _sorted_insert(self._sorted_format,       (fmt_key,          tid))
            title_fold = _game_fold_value(t.get("title"))
            if title_fold is not None:
                _sorted_insert(self._sorted_title_fold, (title_fold, tid))
        else:
            # (in no list before this batch, unless an ``_unindex_track`` of
            # this batch already recorded where it was)
            self._note_sorted_batch(tid, None)

        # Unplayed bookkeeping — a freshly-indexed track has no play stats yet
        # so it counts as unplayed unless ``record_play`` has already fired
        # (e.g. AOF replay applied "record_play" before the track upsert in
        # rare reorder cases — we still respect the existing entry).
        if tid not in self._play_stats:
            self._unplayed_ids.add(tid)

        self._agg_add(t, year)

    def _agg_add(self, t: dict, year) -> None:
        """Count ``t`` into the ``_agg_*`` counters (``year`` already
        normalised).  Shared by ``_index_track`` and the partial reindex."""
        album = (t.get("album") or "").strip()
        for a in _name_counts(t.get("artist")):
            self._agg_artists[a] += 1
            if album:
                self._agg_albums_by_artist.setdefault(a, Counter())[album.lower()] += 1
        for a in _name_counts(t.get("album_artist")):
            self._agg_album_artists[a] += 1
            if album:
                self._agg_albums_by_album_artist.setdefault(a, Counter())[album.lower()] += 1
        if album:
            self._agg_albums[album.lower()] += 1
        for g in track_genres(t):
            gl = g.strip().lower()
            if gl:
                self._agg_genres[gl] += 1
        if year is not None:
            self._agg_years[year] += 1

    def _unindex_track(self, tid: str, t: dict) -> None:
        """Remove a track from all indexes."""
        for token in _tokenize_track(t):
            s = self._word_index.get(token)
            if s:
                s.discard(tid)
                if not s:
                    del self._word_index[token]
                    self._word_vanished(token)

        for k in _name_keys(t.get("artist")):
            self._tag_del(self._tag_artist, k, tid)
        for k in _name_keys(t.get("album_artist")):
            self._tag_del(self._tag_album_artist, k, tid)
        self._tag_del(self._tag_album, t.get("album", ""), tid)
        self._tag_del(self._tag_format, t.get("format", ""), tid)
        self._tag_del(self._tag_dir_hash, t.get("dir_hash", ""), tid)
        self._tag_del(self._tag_scan_root_hash, t.get("scan_root_hash", ""), tid)
        self._bump_root_rev(t.get("scan_root_hash", ""))
        gid = t.get("duplicate_group_id")
        if gid:
            self._tag_del(self._tag_dup_group, gid, tid)
            self._swap_touch(gid)
        for g in track_genres(t):
            self._tag_del(self._tag_genre, g, tid)
        for grp in _scene_group_keys(t.get("scene_group")):
            self._tag_del(self._tag_scene_group, grp, tid)
        for k in _game_keys(t):
            self._tag_del(self._tag_game, k, tid)

        # Extract numeric fields used by both sorted indexes and aggregations.
        # ``normalise_year`` keeps insert/remove in agreement.
        year = _agg_year(t)

        self._nonprimary_ids.discard(tid)
        self._untagged_artist.discard(tid)
        self._untagged_album_artist.discard(tid)
        self._untagged_album.discard(tid)
        self._untagged_genre.discard(tid)
        if year is not None:
            ys = self._tag_year.get(year)
            if ys:
                ys.discard(tid)
                if not ys:
                    del self._tag_year[year]

        if not self._batch_mode:
            # Same lexical key derivation as ``_index_track`` — must match
            # exactly or the remove turns into a no-op and the index drifts.
            EMPTY_SORT_KEY = "￿"
            title_key        = (t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY
            artist_key       = _lex_key_shared(t.get("artist"))
            album_artist_key = _lex_key_shared(t.get("album_artist"))
            album_key        = _lex_key_shared(t.get("album"))
            fmt_key          = _lex_key_shared(t.get("format"))
            _sorted_remove(self._sorted_year, (_year_value(t), tid))
            added = _added_value(t)
            _sorted_remove(self._sorted_added_at, (added, tid))
            if t.get("is_duplicate_primary", True):
                _sorted_remove(self._sorted_added_at_primary, (added, tid))
            _sorted_remove(self._sorted_duration, (_duration_value(t), tid))
            _sorted_remove(self._sorted_bpm, (_bpm_value(t), tid))
            _sorted_remove(self._sorted_title,        (title_key,        tid))
            _sorted_remove(self._sorted_artist,       (artist_key,       tid))
            _sorted_remove(self._sorted_album_artist, (album_artist_key, tid))
            _sorted_remove(self._sorted_album,        (album_key,        tid))
            _sorted_remove(self._sorted_format,       (fmt_key,          tid))
            title_fold = _game_fold_value(t.get("title"))
            if title_fold is not None:
                _sorted_remove(self._sorted_title_fold, (title_fold, tid))
        else:
            self._note_sorted_batch(tid, t)     # (where it is in every list)

        # Unplayed bookkeeping — when a track is removed it can't be unplayed
        # anymore; this stops the set leaking entries for deleted tracks.
        self._unplayed_ids.discard(tid)

        self._agg_remove(t, year)

    def _agg_remove(self, t: dict, year) -> None:
        """Inverse of ``_agg_add``."""
        album = (t.get("album") or "").strip()
        for a in _name_counts(t.get("artist")):
            self._agg_artists[a] -= 1
            if self._agg_artists[a] <= 0:
                del self._agg_artists[a]
            by_art = self._agg_albums_by_artist.get(a)
            if by_art and album:
                by_art[album.lower()] -= 1
                if by_art[album.lower()] <= 0:
                    del by_art[album.lower()]
                if not by_art:
                    # An empty per-artist counter is not "no entry" to
                    # verify_indexes (a fresh build never creates one).
                    del self._agg_albums_by_artist[a]
        for a in _name_counts(t.get("album_artist")):
            self._agg_album_artists[a] -= 1
            if self._agg_album_artists[a] <= 0:
                del self._agg_album_artists[a]
            by_aa = self._agg_albums_by_album_artist.get(a)
            if by_aa and album:
                by_aa[album.lower()] -= 1
                if by_aa[album.lower()] <= 0:
                    del by_aa[album.lower()]
                if not by_aa:
                    del self._agg_albums_by_album_artist[a]
        if album:
            self._agg_albums[album.lower()] -= 1
            if self._agg_albums[album.lower()] <= 0:
                del self._agg_albums[album.lower()]
        for g in track_genres(t):
            gl = g.strip().lower()
            if gl:
                self._agg_genres[gl] -= 1
                if self._agg_genres[gl] <= 0:
                    del self._agg_genres[gl]
        if year is not None:
            self._agg_years[year] -= 1
            if self._agg_years[year] <= 0:
                del self._agg_years[year]

    @staticmethod
    def _tag_key(value) -> str:
        """The key a scalar tag index files ``value`` under — the rule
        ``_tag_set`` / ``_tag_del`` inline (a hot path)."""
        return value.strip().lower() if isinstance(value, str) else str(value)

    @staticmethod
    def _tag_set(idx: dict[str, set[str]], value: str, tid: str) -> None:
        key = value.strip().lower() if isinstance(value, str) else str(value)
        if key:
            idx.setdefault(key, set()).add(tid)

    @staticmethod
    def _tag_del(idx: dict[str, set[str]], value: str, tid: str) -> None:
        key = value.strip().lower() if isinstance(value, str) else str(value)
        if key:
            s = idx.get(key)
            if s:
                s.discard(tid)
                if not s:
                    del idx[key]

    def _word_appeared(self, tok: str) -> None:
        """``tok`` became a ``_word_index`` key."""
        self._word_list_dirty = True
        if self._word_list_full:
            return
        if tok in self._word_gone:
            self._word_gone.discard(tok)        # back again: still in the sorted list
        else:
            self._word_fresh.add(tok)
        self._word_list_dirty = bool(self._word_fresh or self._word_gone)

    def _word_vanished(self, tok: str) -> None:
        """``tok`` is no longer a ``_word_index`` key."""
        self._word_list_dirty = True
        if self._word_list_full:
            return
        if tok in self._word_fresh:
            self._word_fresh.discard(tok)       # never sorted in
        else:
            self._word_gone.add(tok)
        self._word_list_dirty = bool(self._word_fresh or self._word_gone)

    def _rebuild_word_list(self) -> None:
        """``_word_list`` = the sorted ``_word_index`` keys again (lazily, before
        a prefix lookup): the words that came and went since the last sort
        merged in — a few against a long list in about O(vocabulary) memmove
        — else (many, or no account of them) one full sort."""
        if not self._word_list_dirty:
            return
        fresh, gone = self._word_fresh, self._word_gone
        lst = self._word_list
        new = None
        if not self._word_list_full and len(fresh) + len(gone) <= len(lst) // 8 + 64:
            if len(gone) > 32:
                new = [w for w in lst if w not in gone]
            else:
                new = lst.copy()
                for w in sorted(gone, reverse=True):
                    i = bisect.bisect_left(new, w)
                    if i < len(new) and new[i] == w:
                        del new[i]
                    else:                       # (not where it should be: sort all)
                        new = None
                        break
            if new is not None:
                if len(new) != len(lst) - len(gone):
                    new = None
                elif fresh:
                    new.extend(sorted(fresh))
                    new.sort()                  # two sorted runs: one merge
        if new is None or len(new) != len(self._word_index):
            new = sorted(self._word_index.keys())
        self._word_list = new
        fresh.clear()
        gone.clear()
        self._word_list_full = False
        self._word_list_dirty = False

    def _bump_root_rev(self, root_hash) -> None:
        if root_hash:
            self._root_rev[root_hash] = self._root_rev.get(root_hash, 0) + 1

    def root_rev(self, root_hash: str) -> int:
        """``_root_rev`` of one scan root (0 for one never written)."""
        return self._root_rev.get(root_hash, 0)

    def _reindex_changed(self, tid: str, old: dict, t: dict, changed) -> None:
        """Move track ``tid`` from its ``old`` field values to ``t``'s in only
        the derived indexes that read a field in ``changed`` — the diff-based
        twin of ``_unindex_track(old)`` + ``_index_track(t)``, with identical
        key derivation.

        Tokens: only the added/removed tokens are touched, and the sorted word
        list is dirtied only when a word-index KEY appears or disappears.
        Sorted lists: a list is re-keyed only when its key changed (in batch
        mode the list is marked for the deferred rebuild instead).
        ``_unplayed_ids`` is not field-derived, so it is left alone."""
        if "scan_root_hash" in changed or "path" in changed:
            self._bump_root_rev(old.get("scan_root_hash", ""))
            self._bump_root_rev(t.get("scan_root_hash", ""))
        if not _TOKEN_FIELDS.isdisjoint(changed):
            old_tok = _tokenize_track(old)
            new_tok = _tokenize_track(t)
            if old_tok != new_tok:
                wi = self._word_index
                for tok in old_tok - new_tok:
                    s = wi.get(tok)
                    if s:
                        s.discard(tid)
                        if not s:
                            del wi[tok]
                            self._word_vanished(tok)
                for tok in new_tok - old_tok:
                    s = wi.get(tok)
                    if s is None:
                        wi[tok] = {tid}
                        self._word_appeared(tok)
                    else:
                        s.add(tid)

        for field, attr in _SCALAR_TAG_INDEXES:
            if field in changed:
                idx = getattr(self, attr)
                self._tag_del(idx, old.get(field, ""), tid)
                self._tag_set(idx, t.get(field, ""), tid)
        for field, attr in _NAME_TAG_INDEXES:
            if field in changed:
                idx = getattr(self, attr)
                for k in _name_keys(old.get(field)):
                    self._tag_del(idx, k, tid)
                for k in _name_keys(t.get(field)):
                    self._tag_set(idx, k, tid)
        if "duplicate_group_id" in changed:
            if old.get("duplicate_group_id"):
                self._tag_del(self._tag_dup_group, old["duplicate_group_id"], tid)
            if t.get("duplicate_group_id"):
                self._tag_set(self._tag_dup_group, t["duplicate_group_id"], tid)
        if "genre" in changed or "genre_derived" in changed:
            for g in track_genres(old):
                self._tag_del(self._tag_genre, g, tid)
            for g in track_genres(t):
                self._tag_set(self._tag_genre, g, tid)
        if "scene_group" in changed:
            for grp in _scene_group_keys(old.get("scene_group")):
                self._tag_del(self._tag_scene_group, grp, tid)
            for grp in _scene_group_keys(t.get("scene_group")):
                self._tag_set(self._tag_scene_group, grp, tid)
        if "game" in changed or "game_aliases" in changed:
            for k in _game_keys(old):
                self._tag_del(self._tag_game, k, tid)
            for k in _game_keys(t):
                self._tag_set(self._tag_game, k, tid)

        for name, (fields, value) in _SORTED_SPECS.items():
            if fields.isdisjoint(changed):
                continue
            ov, nv = value(old), value(t)
            if ov is None and nv is None:
                continue
            if ov is not None and nv is not None and ov == nv:
                continue
            if self._batch_mode:
                if name not in self._dirty_sorted:
                    self._dirty_sorted.add(name)
                    self._dirty_sorted_tids[name] = {tid: ov}
                else:
                    pending = self._dirty_sorted_tids.get(name)
                    if pending is not None:          # None: already a full rebuild
                        pending.setdefault(tid, ov)  # keep the PRE-batch key
                continue
            lst = getattr(self, name)
            if ov is not None:
                _sorted_remove(lst, (ov, tid))
            if nv is not None:
                _sorted_insert(lst, (nv, tid))

        if not _AGG_FIELDS.isdisjoint(changed):
            self._agg_remove(old, _agg_year(old))
            self._agg_add(t, _agg_year(t))

        if "is_duplicate_primary" in changed:
            if t.get("is_duplicate_primary", True):
                self._nonprimary_ids.discard(tid)
            else:
                self._nonprimary_ids.add(tid)
        for field, attr, inputs in _UNTAGGED_SPECS:
            if not inputs.isdisjoint(changed):
                ids = getattr(self, attr)
                if _untagged(t, field):
                    ids.add(tid)
                else:
                    ids.discard(tid)
        if "year" in changed:
            oy, ny = _agg_year(old), _agg_year(t)
            if oy is not None:
                ys = self._tag_year.get(oy)
                if ys:
                    ys.discard(tid)
                    if not ys:
                        del self._tag_year[oy]
            if ny is not None:
                ys = self._tag_year.get(ny)
                if ys is None:
                    self._tag_year[ny] = {tid}
                else:
                    ys.add(tid)

    def _apply_field_changes(self, tid: str, t: dict, changed: dict) -> None:
        """Write the (already diffed, non-empty) ``changed`` fields onto
        stored track ``t`` and maintain the derived indexes."""
        if self._write_watch is not None:
            self._write_watch(tid, t)
        if not _DUP_FIELDS.isdisjoint(changed):
            self._note_dup_dirty(tid, t)
        if not changed.keys() <= _ENRICH_EXEMPT_FIELDS:
            self._note_enrich(tid)
        if not _SWAP_FIELDS.isdisjoint(changed):
            self._swap_touch(t.get("duplicate_group_id"))
            self._swap_touch(changed.get("duplicate_group_id"))
        if _INDEXED_FIELDS.isdisjoint(changed):
            t.update(changed)                   # e.g. cover_art, file_md5, defect
        else:
            old = dict(t)
            t.update(changed)
            self._reindex_changed(tid, old, t, changed)
        # Counted once the fields are written: whatever a per-track memo built
        # from ``t`` before is keyed to the old count.
        self._track_rev[tid] = self._track_rev.get(tid, 0) + 1

    # ── Bulk load (used on startup — indexes rebuilt after all data loaded) ──

    def bulk_load(
        self,
        tracks: dict[str, dict],
        waveforms: dict[str, list[float]],
        ratings: dict[str, int],
        play_stats: dict[str, dict],
        playlists: dict[str, dict],
        history: list[dict],
        scan_dirs: dict[str, dict],
        hash_lookups: dict[str, str],
        config: dict[str, Any],
    ) -> None:
        """Load all data from a snapshot.  Call rebuild_indexes() afterwards.

        Every track's game is brought in step with its game album on the way
        in (``game_follow``) — a library saved before the ``game`` field
        existed gets its retro games here, in memory, without a write."""
        for t in tracks.values():
            extra = game_follow(t)
            if extra:
                t.update(extra)
            share_values(t)
        self._owner_thread = threading.get_ident()      # (the loop of this run)
        self._tracks = tracks
        self._waveforms = waveforms
        self._ratings = ratings
        self._play_stats = play_stats
        self._playlists = playlists
        self._history = history
        self._scan_dirs = scan_dirs
        self._hash_lookups = hash_lookups
        self._config = config
        self._reset_enrich_log()
        # Another library (a start, a backup import, the app's Stop / Start):
        # nothing keyed on the old one may be served — not even before the
        # caller's ``rebuild_indexes``.
        self._library_replaced()
        self._scan_dirs_changed()

    def rebuild_indexes(self) -> None:
        """Rebuild all indexes from current data.

        Runs in batch mode so the 9 sorted indexes are built by one O(N log N)
        ``sort()`` at the end rather than N × ``bisect.insort`` (which is
        O(N) per call thanks to the memmove, so the per-track loop ends up
        O(N²) wall-clock and goes from ~17 s baseline to >70 s once we
        started maintaining the 5 lexical indexes too).  Then every sequence
        and memo moves (``_library_replaced``): nothing built from the indexes
        before is served after.
        """
        self.clear_indexes()
        self.enter_batch_mode()
        self._mark_all_sorted_dirty()           # (every list is built anew: nothing to record)
        try:
            for tid, t in self._tracks.items():
                self._index_track(tid, t)
        finally:
            self.exit_batch_mode()
        self._rebuild_word_list()
        self._build_sim_token_index()
        # Every index is new (and usually ``_tracks`` too — ``bulk_load``):
        # every cache keyed on the store's sequences is stale.
        self._library_replaced()
        # ``_index_track`` adds every track to ``_unplayed_ids`` whose key
        # isn't already in ``_play_stats`` — the order above guarantees that
        # snapshot-loaded play stats correctly suppress those tids.
        log.info("Indexes rebuilt for %d tracks", len(self._tracks))

    def _library_replaced(self) -> None:
        """The library was loaded or its indexes rebuilt: move every sequence
        a cache keys on (``_mutation_seq`` — core/skip_rules, the Subsonic
        caches, smart.py —, ``_catalog_seq``, ``_duration_seq``, ``_play_seq``,
        ``_rating_seq``, ``_browse_seq``) and drop the store's own memos
        (aggregates, the exclude-aware ones, the duplicate stand-ins, the
        orders and walks).  ``index_generation`` moves with them, so an index
        rebuild built from the library before (``data.rebuild_indexes``) is
        not swapped in over this one."""
        self._mutation_seq += 1
        self._catalog_seq += 1
        self._duration_seq += 1
        self._play_seq += 1
        self._rating_seq += 1
        self._agg_cache.clear()
        self._agg_x_cache.clear()
        self._primary_swap_memo = None
        self._dup_inv_memo = None
        self._inv_memo = None
        self.browse_changed()

    def index_generation(self) -> tuple[int, int, int]:
        """``(_mutation_seq, _duration_seq, _play_seq)``: moves on every write
        that changes an ``INDEX_ATTRS`` index — track upserts / deletes /
        field updates (``_mutation_seq``; ``_SEQ_EXEMPT_FIELDS`` feed no
        index), duration-only backfills (``_duration_seq``, they re-key
        ``_sorted_duration``) and plays (``_play_seq``, a first play leaves
        ``_unplayed_ids``).  An index rebuild that yields while it builds from
        a snapshot (``data.rebuild_indexes``) compares it before and after, and
        must not install indexes built from a snapshot a write has outdated."""
        return (self._mutation_seq, self._duration_seq, self._play_seq)

    def verify_indexes(self) -> dict:
        """Check the live derived indexes against a fresh rebuild from _tracks.

        Builds a throwaway shadow store from the SAME _tracks/_play_stats and
        diffs every index in ``INDEX_ATTRS`` against the live one.  Returns
        ``{index_ok, track_count, mutation_seq, indexes:{attr:{actual,expected,
        ok}}, mismatches:[...]}``.  Authoritative (uses the same _index_track
        derivation) and non-destructive to the live store.

        Synchronous, ~3-5 s for a 270K library, and NON-DESTRUCTIVE (does not
        touch the live store).  MUST run on the event loop (or with _tracks
        otherwise quiescent): it shares _tracks by reference, so a concurrent
        mutation mid-build would be a data race.
        """
        # ``_word_list`` is a lazy derivative of ``_word_index`` and is skipped
        # by ``_diff_indexes``, so a dirty word list is irrelevant here — no
        # need to refresh it (which would mutate self).  ``_word_index`` itself
        # IS compared.
        shadow = TrackStore()
        shadow._tracks = self._tracks          # shared, read-only during build
        shadow._play_stats = self._play_stats  # _index_track derives _unplayed_ids from this
        shadow.rebuild_indexes()
        return self._diff_indexes(shadow)

    # Derived/cache layers EXCLUDED from drift detection — they legitimately
    # differ between the live store and a fresh rebuild, so comparing them
    # yields FALSE positives.  Both are still SWAPPED by a rebuild (so it resets
    # them); they're just not drift signals:
    #   _word_list  — lazy derivative of _word_index (which IS compared).
    #   _agg_cache  — per-query aggregation MEMO (key -> (_browse_seq, value)):
    #     empty after a rebuild but populated by serving aggregate_* requests,
    #     so its contents track query traffic, not _tracks.  The structural
    #     _agg_* COUNTERS it memoizes from ARE compared, so coverage is intact.
    _DIFF_SKIP = frozenset({"_word_list", "_agg_cache"})

    def _diff_indexes(self, expected: "TrackStore") -> dict:
        """Diff this store's live indexes against a freshly-built ``expected``
        shadow.  Pure comparison, no mutation."""
        indexes: dict[str, dict] = {}
        mismatches: list[dict] = []
        for attr in INDEX_ATTRS:
            if attr in self._DIFF_SKIP:
                continue
            live = getattr(self, attr)
            exp = getattr(expected, attr)
            ok = live == exp
            indexes[attr] = {"actual": len(live), "expected": len(exp), "ok": ok}
            if not ok:
                mismatches.append({
                    "index": attr,
                    "actual": len(live),
                    "expected": len(exp),
                    "sample": _index_sample_diff(live, exp),
                })
        return {
            "index_ok": not mismatches,
            "track_count": len(self._tracks),
            "mutation_seq": self._mutation_seq,
            "indexes": indexes,
            "mismatches": mismatches,
        }

    def clear_indexes(self) -> None:
        """Clear all indexes (first phase of rebuild, can be followed by
        batched ``index_tracks_batch`` calls)."""
        self._word_index.clear()
        self._word_list_dirty = True
        self._word_list_full = True
        self._primary_order_memo.clear()        # built from the lists emptied below
        self._excluded_order_memo.clear()
        self._primary_swap_memo = None          # built from the duplicate groups emptied below
        self._swap_dirty = None
        self._dup_inv_memo = None
        self._inv_memo = None
        self.browse_changed()
        self._nonprimary_ids.clear()
        self._untagged_artist.clear()
        self._untagged_album_artist.clear()
        self._untagged_album.clear()
        self._untagged_genre.clear()
        self._tag_year.clear()
        for tag_idx in (
            self._tag_artist, self._tag_album_artist, self._tag_album,
            self._tag_genre, self._tag_format, self._tag_dir_hash,
            self._tag_scan_root_hash, self._tag_dup_group, self._tag_scene_group,
            self._tag_game,
        ):
            tag_idx.clear()
        # Inverted sample-token index (B) is derived/batch-built, not in
        # INDEX_ATTRS; ``rebuild_indexes``/``finish_rebuild`` refill it after
        # this clear, so emptying it here just avoids a stale window mid-rebuild.
        self._sim_tok_postings = {}
        self._sim_tok_count = {}
        self._sorted_year.clear()
        self._sorted_added_at.clear()
        self._sorted_added_at_primary.clear()
        self._sorted_duration.clear()
        self._sorted_bpm.clear()
        # Lexical sort indexes — must be cleared in lock-step with the
        # numeric ones, otherwise a ``rebuild_indexes()`` after a snapshot
        # load would leave stale ``(key, tid)`` entries from a previous
        # snapshot alongside the fresh inserts, and the windowed sort
        # would point at deleted track ids.
        self._sorted_title.clear()
        self._sorted_artist.clear()
        self._sorted_album_artist.clear()
        self._sorted_album.clear()
        self._sorted_format.clear()
        self._sorted_title_fold.clear()
        self._agg_artists.clear()
        self._agg_album_artists.clear()
        self._agg_albums.clear()
        self._agg_genres.clear()
        self._agg_years.clear()
        self._agg_albums_by_artist.clear()
        self._agg_albums_by_album_artist.clear()
        self._agg_cache.clear()
        self._unplayed_ids.clear()
        if self._batch_mode:
            # (a batch's deferred merge would start from the emptied lists)
            self._mark_all_sorted_dirty()

    def index_tracks_batch(self, items: list[tuple[str, dict]]) -> None:
        """Index a batch of (track_id, track_dict) tuples."""
        for tid, t in items:
            self._index_track(tid, t)

    def finish_rebuild(self) -> None:
        """Finalise an async rebuild — build word list + sample index and log."""
        self._rebuild_word_list()
        self._build_sim_token_index()
        log.info("Indexes rebuilt for %d tracks", len(self._tracks))

    # ── Retro sample-lineage index (B) ─────────────────────────────────────

    def _build_sim_token_index(self) -> None:
        """(Re)build the inverted instrument-token index used by the retro
        sample-lineage similarity signal.  Build-and-swap: assembles fresh
        structures then rebinds in one step, so a reader on the loop only ever
        sees the old-complete or new-complete index.

        Singleton tokens (present in exactly one track) are dropped: they can
        never be SHARED by two tracks, so excluding them from the postings
        changes no pairwise Jaccard (lossless) while removing the bulk of the
        vocabulary.  Per-track token COUNTS keep the full ``|tokens|`` so the
        Jaccard denominator ``|A|+|B|-|A∩B|`` is exact.
        """
        for _ in self._iter_build_sim_token_index():
            pass

    def _iter_build_sim_token_index(self, chunk: int = 20_000):
        """``_build_sim_token_index`` as a generator that pauses (yields)
        every ``chunk`` tracks (and every ``chunk`` tokens of the singleton
        prune) so an async caller can give the loop a turn.  Iterates a
        snapshot of the track ids (a track removed meanwhile is left out);
        binds the result only at the end."""
        from soniqboom.core.retro import is_retro_format, instrument_tokens
        postings: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        tracks = self._tracks
        ids = list(tracks)
        for i in range(0, len(ids), chunk):
            for tid in ids[i : i + chunk]:
                t = tracks.get(tid)
                if t is None or not is_retro_format(t.get("format")):
                    continue
                inst = t.get("instruments")
                if not inst:
                    continue
                toks = instrument_tokens(inst)
                counts[tid] = len(toks)          # full count (incl. singletons) for |B|
                for tok in toks:
                    postings.setdefault(tok, []).append(tid)
            yield
        # Drop singleton tokens — lossless (a token in one track is never shared).
        kept: dict[str, list[str]] = {}
        toks = list(postings.items())
        for i in range(0, len(toks), chunk):
            for tok, tids in toks[i : i + chunk]:
                if len(tids) > 1:
                    kept[tok] = tids
            yield
        postings = kept
        self._sim_tok_postings = postings    # atomic rebind (build-and-swap)
        self._sim_tok_count = counts
        log.info(
            "Sample-token index: %d retro-with-samples tracks, %d shared tokens",
            len(counts), len(postings),
        )

    def retro_sample_jaccard(self, seed: dict, candidates: list[dict]) -> dict[str, float]:
        """``{track_id: sample-name Jaccard vs seed}`` over ``candidates`` for a
        RETRO seed, via the inverted instrument-token index.

        On the CURRENT index snapshot the value is identical to the pairwise
        ``|A∩B|/|A∪B|`` the scorer computes on the fly (singleton pruning is
        lossless — a token in one track is never shared, so it can't appear in
        any intersection), but derived from a shared-token walk instead of
        re-tokenising every candidate — ~40 ms → <1 ms over a ~5 K pool
        (measured, 262 K library).  Empty for a non-retro / token-less seed.
        Runs synchronously on the event loop (no ``await``, so it can't race the
        ``data.rebuild_indexes`` swap).

        A candidate ADDED since the last build (``tid`` absent from the count
        map) falls back to an exact on-the-fly tokenisation.  NOT covered by the
        fallback (bounded, self-healing at the next rebuild): a candidate whose
        ``instruments`` changed IN PLACE — its id stays in the count map, so a
        stale token count is used — and a candidate that shares with the seed
        ONLY a token that was a singleton at build time (pruned from postings),
        which can happen when the SEED itself is newer than the index.  These
        lag the sample sub-signal only until the next ``_build_sim_token_index``.
        """
        from soniqboom.core.retro import is_retro_format, instrument_tokens
        if not is_retro_format(seed.get("format")):
            return {}
        stoks = instrument_tokens(seed.get("instruments"))
        if not stoks:
            return {}
        s_len = len(stoks)
        postings = self._sim_tok_postings
        counts = self._sim_tok_count
        # Seed → shared-token count per track, from the non-singleton postings.
        inter: dict[str, int] = {}
        for tok in stoks:
            bucket = postings.get(tok)
            if bucket:
                for tid in bucket:
                    inter[tid] = inter.get(tid, 0) + 1
        out: dict[str, float] = {}
        for t in candidates:
            tid = t.get("id")
            if not tid:
                continue
            i = inter.get(tid)
            if i is not None:                       # shares ≥1 sample with seed
                union = s_len + counts.get(tid, 0) - i
                if union > 0:
                    out[tid] = i / union
            elif tid not in counts and t.get("instruments"):
                # Candidate added since the last index build → exact fallback.
                ti = instrument_tokens(t.get("instruments"))
                if ti:
                    shared = len(stoks & ti)
                    if shared:
                        out[tid] = shared / len(stoks | ti)
        return out

    def track_items_list(self) -> list[tuple[str, dict]]:
        """Return a snapshot of (track_id, track_dict) for chunked iteration."""
        return list(self._tracks.items())

    # ── Track CRUD ───────────────────────────────────────────────────────

    def get_track(self, track_id: str) -> dict | None:
        return self._tracks.get(track_id)

    def get_tracks_batch(self, track_ids: list[str]) -> list[dict | None]:
        return [self._tracks.get(tid) for tid in track_ids]

    def _restore_mark(self, t: dict) -> None:
        """A track added (again) gets the playback-problem mark the registry
        keeps for its file (core/skip_rules.restore)."""
        reg = self._config.get("health_marks")
        if reg:
            from soniqboom.core import skip_rules
            skip_rules.restore(reg, t)

    def upsert_track(self, track: dict) -> None:
        tid = track["id"]
        old = self._tracks.get(tid)
        if self._write_watch is not None:
            self._write_watch(tid, old)
        if not old:
            self._restore_mark(track)
        track.update(game_follow(track))
        self._note_dup_upsert(tid, old, track)
        self._note_enrich(tid)
        if old:
            self._unindex_track(tid, old)
        self._tracks[tid] = track
        self._track_rev[tid] = self._track_rev.get(tid, 0) + 1
        self._index_track(tid, track)
        self._mutation_seq += 1
        self._catalog_seq += 1
        self._browse_seq += 1
        self._upserts_since_freeze += 1
        self._aof("upsert_track", id=tid, data=track)

    def upsert_tracks_batch(self, tracks: list[dict]) -> int:
        watch = self._write_watch
        for t in tracks:
            tid = t["id"]
            old = self._tracks.get(tid)
            if watch is not None:
                watch(tid, old)
            if old:
                _carry_enrichment(old, t)     # keep post-scan enrichment (+ the game)
                self._unindex_track(tid, old)
            else:
                self._restore_mark(t)          # (a file that came back keeps its mark)
                t.update(game_follow(t))
            self._note_dup_upsert(tid, old, t)
            self._note_enrich(tid)
            self._tracks[tid] = t
            self._track_rev[tid] = self._track_rev.get(tid, 0) + 1
            self._index_track(tid, t)
        self._mutation_seq += 1
        self._catalog_seq += 1
        self._browse_seq += 1
        self._upserts_since_freeze += len(tracks)
        # ``t`` now carries the merged enrichment, so the AOF record replays
        # the same result — the carry is idempotent (a stamped record re-upserted
        # already has year_source set, so the guard below is a no-op on replay).
        self._aof("batch_upsert_tracks", count=len(tracks), data=tracks)
        return len(tracks)

    # ── Batch mode: defer O(n) sorted-list operations ───────────────

    def enter_batch_mode(self) -> None:
        """Enter batch mode — sorted indexes deferred until the OUTERMOST exit.

        Reference-counted (see ``__init__``): nesting an entry must NOT reset
        ``_sorted_dirty``, or an outer batch section's pending rebuild is lost.
        """
        self._batch_depth += 1
        self._batch_mode = True

    def exit_batch_mode(self) -> None:
        """Exit batch mode; on the OUTERMOST exit only, bring the sorted
        indexes marked dirty (``_dirty_sorted``) up to date — the tracks
        written in the batch merged in (``merged_sorted_list``), or every list
        rebuilt after a large batch."""
        self._batch_depth -= 1
        if self._batch_depth > 0:
            return                       # a concurrent batch section is still open
        self._batch_depth = 0
        self._batch_mode = False
        if self._dirty_sorted:
            plan = self.sorted_rebuild_plan()
            if len(plan) == _N_SORTED and all(v is None for v in plan.values()):
                self._rebuild_sorted_indexes(set(plan))
            else:
                cap = self.sorted_merge_cap()
                for name, tids in plan.items():
                    lst = None
                    if tids is not None:
                        if len(tids) <= cap:
                            lst = self.merged_sorted_list(name, tids)
                        if lst is None:
                            # (many re-keyed, or the merge found the list out
                            # of step: every entry of those tracks replaced)
                            lst = _run_generator(self.iter_filtered_sorted_list(name, tids))
                    if lst is None:
                        lst = self.build_sorted_list(name)
                        lst.sort()
                    setattr(self, name, lst)
            self._sorted_rebuilt(list(plan))

    def build_sorted_list(self, name: str, changed=None) -> list[tuple]:
        """One sorted index (a ``SORTED_LISTS`` name), NOT yet sorted — the
        caller sorts it (the scanner's yielding exit sorts with a type guard).

        ``changed`` None: built from every track, with the same key derivation
        as ``_index_track``.  Otherwise (``{track id: pre-batch key}``, see
        ``sorted_rebuild_plan``) the list's current, pre-batch contents with
        those entries cut out (located by bisect on their old key), followed
        by the ids' fresh entries — one sorted run plus a short sorted tail,
        which ``list.sort`` merges in O(n + k log k)."""
        value = _SORTED_SPECS[name][1]
        out: list[tuple] = []
        if changed is None:
            for tid, t in self._tracks.items():
                v = value(t)
                if v is not None:
                    out.append((v, tid))
            return out
        cur = getattr(self, name)
        cut: list[int] = []
        for tid, ov in changed.items():
            if ov is None:
                continue
            i = bisect.bisect_left(cur, (ov, tid))
            if i < len(cur) and cur[i] == (ov, tid):
                cut.append(i)
            else:                               # not where expected: filter instead
                cut = None
                break
        if cut is None:
            out = [e for e in cur if e[1] not in changed]
        else:
            cut.sort()
            prev = 0
            for i in cut:
                out.extend(cur[prev:i])
                prev = i + 1
            out.extend(cur[prev:])
        fresh: list[tuple] = []
        for tid in changed:
            t = self._tracks.get(tid)
            if t is not None:
                v = value(t)
                if v is not None:
                    fresh.append((v, tid))
        fresh.sort()
        out.extend(fresh)
        return out

    def _rebuild_sorted_indexes(self, names=None) -> None:
        """Rebuild sorted indexes from scratch.  O(n log n) via sort.

        ``names`` limits the rebuild to those ``SORTED_LISTS``; None or all of
        them takes the single-pass full rebuild below, which also rebuilds
        ``_sorted_added_at_primary`` (subset of ``_sorted_added_at`` for tracks
        that are the primary copy of their duplicate group, or aren't
        duplicates at all).  Callers clear the pending state themselves.
        """
        self._primary_order_memo.clear()        # it would pin the replaced lists
        self._excluded_order_memo.clear()
        self._browse_seq += 1                   # (as ``_sorted_rebuilt``)
        self._filter_walk_memo.clear()
        if names is not None and not _ALL_SORTED <= set(names):
            for name in list(names):
                lst = self.build_sorted_list(name)
                lst.sort()
                setattr(self, name, lst)
            log.info("Sorted indexes rebuilt: %s", ", ".join(sorted(names)))
            return
        EMPTY_SORT_KEY = "￿"
        year, added, added_primary, dur, bpm = [], [], [], [], []
        title, artist_s, album_artist_s, album_s, fmt = [], [], [], [], []
        title_fold = []
        for tid, t in self._tracks.items():
            # Every track in every numeric list, keyed by its value function
            # (no year / duration / bpm → ``_NO_VALUE_KEY``, no ``added_at`` → 0).
            year.append((_year_value(t), tid))
            a = _added_value(t)
            added.append((a, tid))
            if t.get("is_duplicate_primary", True):
                added_primary.append((a, tid))
            dur.append((_duration_value(t), tid))
            bpm.append((_bpm_value(t), tid))
            # Lexical keys mirror ``_index_track`` exactly — same
            # ``.strip().lower()`` + EMPTY sentinel — so per-track
            # incremental insert/remove and the full rebuild produce
            # byte-identical index contents.
            title.append(         ((t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
            artist_s.append(      (_lex_key_shared(t.get("artist")),       tid))
            album_artist_s.append((_lex_key_shared(t.get("album_artist")), tid))
            album_s.append(       (_lex_key_shared(t.get("album")),        tid))
            fmt.append(           (_lex_key_shared(t.get("format")),       tid))
            tf = _game_fold_value(t.get("title"))
            if tf is not None:
                title_fold.append((tf, tid))
        year.sort(); added.sort(); added_primary.sort(); dur.sort(); bpm.sort()
        title.sort(); artist_s.sort(); album_artist_s.sort(); album_s.sort(); fmt.sort()
        title_fold.sort()
        self._sorted_year = year
        self._sorted_added_at = added
        self._sorted_added_at_primary = added_primary
        self._sorted_duration = dur
        self._sorted_bpm = bpm
        self._sorted_title = title
        self._sorted_artist = artist_s
        self._sorted_album_artist = album_artist_s
        self._sorted_album = album_s
        self._sorted_format = fmt
        self._sorted_title_fold = title_fold
        log.info(
            "Sorted indexes rebuilt: %d year, %d added (%d primary), %d dur, %d bpm, "
            "%d title, %d artist, %d album_artist, %d album, %d fmt",
            len(year), len(added), len(added_primary), len(dur), len(bpm),
            len(title), len(artist_s), len(album_artist_s), len(album_s), len(fmt),
        )

    def delete_track(self, track_id: str) -> bool:
        t = self._tracks.get(track_id)
        if t is None:
            return False
        if self._write_watch is not None:
            self._write_watch(track_id, t)
        del self._tracks[track_id]
        self._note_dup_dirty(track_id, t)
        self._note_enrich(track_id)
        self._unindex_track(track_id, t)
        self._waveforms.pop(track_id, None)
        self._mutation_seq += 1
        self._catalog_seq += 1
        self._browse_seq += 1
        self._aof("delete_tracks", ids=[track_id])
        return True

    def delete_track_ids(self, track_ids: list[str]) -> int:
        deleted = 0
        ids = []
        watch = self._write_watch
        for tid in track_ids:
            if watch is not None and tid in self._tracks:
                watch(tid, self._tracks[tid])
            t = self._tracks.pop(tid, None)
            if t:
                self._note_dup_dirty(tid, t)
                self._note_enrich(tid)
                self._unindex_track(tid, t)
                self._waveforms.pop(tid, None)
                deleted += 1
                ids.append(tid)
        if ids:
            self._mutation_seq += 1
            self._catalog_seq += 1
            self._browse_seq += 1
            self._aof("delete_tracks", ids=ids)
        return deleted

    def track_count(self) -> int:
        return len(self._tracks)

    # ── Duplicate-relevant change tracking ───────────────────────────────

    def _note_dup_dirty(self, tid: str, old: "dict | None") -> None:
        """Record ``tid``'s duplicate-relevant values BEFORE a change (None =
        a new track).  The first record per track wins until it is taken.
        Catalogue tracks (``core/catalogue.py``) are never grouped."""
        if self._dup_dirty_overflow or tid in self._dup_dirty:
            return
        if old is not None and _catalogue.is_catalogue_path(old.get("path")):
            return
        first = not self._dup_dirty
        if len(self._dup_dirty) >= _DUP_DIRTY_MAX:
            self._dup_dirty.clear()
            self._dup_dirty_overflow = True
        else:
            self._dup_dirty[tid] = (None if old is None else
                                    {"id": tid, **{k: old.get(k) for k in _DUP_FIELDS}})
        cb = self._on_dup_dirty
        if first and cb is not None:
            try:
                cb()
            except Exception:                     # noqa: BLE001 — never fail a write
                log.debug("dup-dirty callback failed", exc_info=True)

    def _note_dup_upsert(self, tid: str, old: "dict | None", new: dict) -> None:
        if _catalogue.is_catalogue_path(new.get("path")) and (
                old is None or _catalogue.is_catalogue_path(old.get("path"))):
            return
        if (old is None
                or any(old.get(k) != new.get(k) for k in _DUP_FIELDS)
                or any(old.get(k) != new.get(k) for k in _DUP_OUTPUT_FIELDS)):
            self._note_dup_dirty(tid, old)

    def take_dup_dirty(self) -> "tuple[dict[str, dict | None], bool]":
        """Hand over (and reset) the pending duplicate-relevant changes:
        ``({track id: previous values or None}, overflowed)``."""
        out, over = self._dup_dirty, self._dup_dirty_overflow
        self._dup_dirty = {}
        self._dup_dirty_overflow = False
        return out, over

    # ── Enrichment change log (``core/enrich_delta.py``) ─────────────────

    def set_enrich_listener(self, fn: Callable[[], None] | None) -> None:
        """Call ``fn()`` (cheap, non-blocking — it may only schedule work)
        after every new enrichment change-log entry; None removes it.  The
        derived-genre pass uses it to follow every write it reads
        (core/genre_derive.py ``poke``)."""
        self._enrich_listener = fn

    def _note_enrich(self, tid: str) -> None:
        """Record a change to track ``tid`` (an insert, a delete, or a field
        a pass reads) as the newest entry of the enrichment change log."""
        fn = self._enrich_listener
        if fn is not None:
            try:
                fn()
            except Exception:                   # noqa: BLE001 — never fail a write
                pass
        self._enrich_seq += 1
        log_ = self._enrich_log
        log_.pop(tid, None)
        log_[tid] = self._enrich_seq
        if len(log_) > _ENRICH_LOG_MAX:
            items = list(log_.items())
            cut = len(items) // 2
            self._enrich_floor = items[cut - 1][1]
            self._enrich_log = dict(items[cut:])

    def _reset_enrich_log(self) -> None:
        """A load replaced the tracks: no earlier position is valid any more
        (every pass's next run covers the whole library)."""
        self._enrich_log = {}
        self._enrich_floor = self._enrich_seq
        self._enrich_epoch = object()

    def enrich_cursor(self) -> tuple:
        """The current position in the enrichment change log — what a pass
        records with the snapshot it reads (``enrich_changes``)."""
        return (self._enrich_epoch, self._enrich_seq)

    def enrich_changes(self, cursor, limit: int | None = None) -> "set[str] | None":
        """The ids of the tracks inserted, deleted or changed after ``cursor``
        (an ``enrich_cursor()`` value) — None when that is not known (no
        cursor, a load since, the entries were dropped at the cap) or there
        are more than ``limit``.  Newest first: O(changes)."""
        if cursor is None:
            return None
        epoch, seq = cursor
        if epoch is not self._enrich_epoch or seq < self._enrich_floor:
            return None
        out: set[str] = set()
        log_ = self._enrich_log
        for tid in reversed(log_):
            if log_[tid] <= seq:
                break
            out.add(tid)
            if limit is not None and len(out) > limit:
                return None
        return out

    def all_tracks(self) -> list[dict]:
        return list(self._tracks.values())

    def similar_candidates(self, seed: dict, *, floor: int = 400) -> list[dict]:
        """Bounded candidate pool for similarity / instant-mix scoring.

        Returns the union of the seed's **artist**, **album_artist** and
        **genre** buckets (via the tag indexes) plus a sample of its **format**
        bucket — instead of the whole library.  ``build_instant_mix``'s score
        is dominated by genre (6.0) + artist (2.4) + album_artist (1.4) +
        format (1.6), so this pool contains essentially every track that could
        reach the top-k, turning its O(N)-over-the-library scan into O(pool)
        (≈6 000 vs ~260 000 here).

        Strong-signal ids (artist/album_artist/genre) are never dropped; only
        the format sample + overflow are trimmed to ``cap``.  A too-sparse seed
        is topped up toward ``floor`` with a random sample so the mix still has
        variety.  Keys are lower-cased to match the tag indexes.
        """
        import random as _rnd
        _GENRE_CAP, _FORMAT_CAP, _ARTIST_CAP, _GROUP_CAP = 3000, 2000, 3000, 2000
        # Generic "artist"/"album_artist" tags aren't a real similarity signal —
        # a compilation's "Various Artists" bucket is tens of thousands of
        # unrelated tracks and would re-inflate the pool to O(N).  Skip them.
        _GENERIC = {
            "various artists", "various", "va", "unknown artist", "unknown",
            "compilation", "soundtrack", "ost", "[unknown]", "n/a",
        }
        # Seed a PER-SEED-DETERMINISTIC rng so a given track's sampled pool (and
        # therefore its ranked "more like this") is stable across requests —
        # find_similar's fixed-rng contract relied on that.  Varies across seeds.
        _r = _rnd.Random(str(seed.get("id") or ""))

        def _n(s: object) -> str:
            return s.strip().lower() if isinstance(s, str) else ""

        def _sampled(bucket: set[str], n: int) -> set[str]:
            # Bound large buckets so a broad scene genre / common format (60–130 K
            # tracks) can't blow the pool back up to O(N).  NOTE: for tagless
            # retro files these buckets carry no within-bucket ranking signal so
            # sampling is loss-free; for a large WELL-TAGGED genre (year/bpm/
            # rating differentiate) sampling can drop the single closest track —
            # accepted trade-off, measured via the score-parity check.
            return bucket if len(bucket) <= n else set(_r.sample(list(bucket), n))

        sid = seed.get("id")
        ids: set[str] = set()
        a, aa = _n(seed.get("artist")), _n(seed.get("album_artist"))
        if a and a not in _GENERIC:                 # same-artist — strong signal, but cap runaway buckets
            ids |= _sampled(self._tag_artist.get(a, set()), _ARTIST_CAP)
        if aa and aa not in _GENERIC:
            ids |= _sampled(self._tag_album_artist.get(aa, set()), _ARTIST_CAP)

        g = seed.get("genre") or []
        if isinstance(g, str):
            g = [g]
        gids: set[str] = set()
        for gg in g:
            k = _n(gg)
            if k:
                gids |= self._tag_genre.get(k, set())
        ids |= _sampled(gids, _GENRE_CAP)

        fmt = _n(seed.get("format"))
        if fmt:
            ids |= _sampled(self._tag_format.get(fmt, set()), _FORMAT_CAP)

        # Scene-group (Demozoo): pool SAME-COLLECTIVE tracks — other composers'
        # music from the group(s) this composer belonged to — which the
        # artist/genre/format buckets miss (measured: only ~10-25% of a large
        # collective's tracks land in the pool without this).  Retro-only; a
        # non-retro seed has no ``scene_group`` so this is a no-op for it.
        for grp in _scene_group_keys(seed.get("scene_group")):
            ids |= _sampled(self._tag_scene_group.get(grp, set()), _GROUP_CAP)
        ids.discard(sid)

        # ── Tier 0: retro ↔ modern segregation ───────────────────────────
        # Retro (chip/tracker/synth) and modern recorded audio are separate
        # similarity universes — a retro seed's neighbours must be retro, and
        # vice-versa.  Filter the pool to the seed's universe, then top up
        # toward ``floor`` from the SAME universe if the filter thinned it.
        from soniqboom.core.retro import is_retro_format
        seed_retro = is_retro_format(seed.get("format"))
        pool = [t for t in (self._tracks.get(tid) for tid in ids)
                if t and is_retro_format(t.get("format")) == seed_retro]

        if len(pool) < floor:
            allids = list(self._tracks.keys())
            _r.shuffle(allids)                      # seeded rng → deterministic
            have = {t["id"] for t in pool}
            for tid in allids:
                if len(pool) >= floor:
                    break
                if tid == sid or tid in have:
                    continue
                t = self._tracks.get(tid)
                if t and is_retro_format(t.get("format")) == seed_retro:
                    pool.append(t)
                    have.add(tid)
        return pool

    def all_track_metas(self) -> list[dict]:
        """Return all tracks without embedding field."""
        return [
            {k: v for k, v in t.items() if k != "embedding"}
            for t in self._tracks.values()
        ]

    def get_track_ids_for_scan_root(self, root_hash: str) -> set[str]:
        return set(self._tag_scan_root_hash.get(root_hash, set()))

    def duplicate_group_index(self) -> dict[str, list[str]]:
        """Snapshot of the maintained ``{duplicate_group_id -> member track_ids}``
        index (``_tag_dup_group``).  It's reconstructed from the persisted
        ``duplicate_group_id`` track fields during AOF replay, so it survives
        restart — duplicate views can read it directly instead of recomputing
        groups over the whole library.  Returns copies (the live sets are never
        exposed) so callers can't mutate the index."""
        return {gid: list(tids) for gid, tids in self._tag_dup_group.items() if tids}

    def duplicate_group_count(self) -> int:
        """Cheap O(1)-ish count of maintained duplicate groups (no snapshot)."""
        return sum(1 for tids in self._tag_dup_group.values() if tids)

    def primary_swap(self, unplayable) -> PrimarySwap | None:
        """The duplicate groups whose primary is in ``unplayable`` (the tracks
        that can't play right now — ``core.availability``) while another member
        is not, each with its stand-in: the best member that can play.  None
        when there is no such group.  A group whose members all can't play
        keeps its primary.

        Memoised per ``unplayable`` set (compared by identity — callers hand
        the same frozenset back until it changes); a result equal to the
        previous one keeps the previous object, so memos keyed on it stay
        valid.  A build reads the unplayable ids or the duplicate groups,
        whichever are fewer, then the found groups' members.  After writes,
        only the groups they touched (``_swap_dirty``: a member added, removed
        or re-grouped, or a change to what the pick reads — ``_SWAP_FIELDS``)
        are worked out again; the result is the full build's."""
        if not unplayable or not self._tag_dup_group:
            return None
        owner = self._memo_ok()
        memo = self._primary_swap_memo if owner else None
        dirty = self._swap_dirty
        if memo is not None and memo[0] is unplayable and dirty is not None:
            if not dirty:
                return memo[1]
            if len(dirty) <= max(256, len(self._tag_dup_group) // 8):
                by_group = dict(memo[2])
                for gid in dirty:
                    by_group.pop(gid, None)
                    entry = self._swap_group(gid, unplayable)
                    if entry is not None:
                        by_group[gid] = entry
                return self._swap_done(unplayable, by_group, memo[1])
        tracks = self._tracks
        groups = self._tag_dup_group
        if len(unplayable) <= len(groups):
            gids = set()
            for tid in unplayable:
                t = tracks.get(tid)
                if t is not None and t.get("is_duplicate_primary", True):
                    gid = t.get("duplicate_group_id")
                    if gid:
                        gids.add(self._tag_key(gid))
        else:
            gids = {gid for gid, members in groups.items()
                    if members and not members.isdisjoint(unplayable)}
        by_group: dict[str, tuple[list[str], str]] = {}
        for gid in gids:
            entry = self._swap_group(gid, unplayable)
            if entry is not None:
                by_group[gid] = entry
        if not owner:                   # (off the loop: nothing kept — ``_memo_ok``)
            prev = self._primary_swap_memo
            stand_in = {p: best for prims, best in by_group.values() for p in prims}
            if not stand_in:
                return None
            prev = prev[1] if prev is not None else None
            return prev if prev is not None and prev.stand_in == stand_in else PrimarySwap(stand_in)
        return self._swap_done(unplayable, by_group, memo[1] if memo is not None else None)

    def _swap_group(self, gid: str, unplayable) -> "tuple[list[str], str] | None":
        """Duplicate group ``gid`` (a ``_tag_dup_group`` key)'s stand-in for
        ``unplayable``: ``(its primaries, the best member that can play)``
        when every primary can't play and another member can, else None."""
        groups = self._tag_dup_group
        members = groups.get(gid)
        if not members or len(members) < 2:
            return None
        tracks = self._tracks
        prims = [m for m in members
                 if (t := tracks.get(m)) is not None and t.get("is_duplicate_primary", True)]
        if not prims or not unplayable.issuperset(prims):
            return None                 # no primary (a group being re-picked), or one plays
        alts = [m for m in members if m not in unplayable and m in tracks]
        if not alts:
            return None                 # no member plays: the primary stays
        from soniqboom.core.duplicates import primary_sort_key
        return prims, min(alts, key=lambda m: primary_sort_key(tracks[m]))

    def _swap_done(self, unplayable, by_group: dict, prev: "PrimarySwap | None"):
        """Memoise and return the swap of ``by_group`` (``_swap_group``
        results) — ``prev`` itself when it is equal."""
        stand_in: dict[str, str] = {}
        for prims, best in by_group.values():
            for p in prims:
                stand_in[p] = best
        swap = None
        if stand_in:
            swap = prev if prev is not None and prev.stand_in == stand_in else PrimarySwap(stand_in)
        self._primary_swap_memo = (unplayable, swap, by_group)
        self._swap_dirty = set()
        return swap

    def _swap_touch(self, gid) -> None:
        """Duplicate group ``gid`` (a field value) changed in a way the swap
        reads: work it out again at the next ``primary_swap``."""
        dirty = self._swap_dirty
        if dirty is not None and gid:
            key = gid.strip().lower() if isinstance(gid, str) else str(gid)
            if key:
                dirty.add(key)

    def shown_member(self, tid: str, swap: PrimarySwap | None = None) -> str:
        """The member of ``tid``'s duplicate group a deduped list shows now:
        its primary — or, with ``swap``, the stand-in for a primary that can't
        play.  ``tid`` itself when that is the one, when it is in no group, or
        when its group has no primary."""
        if swap is not None:
            if tid in swap.promoted:
                return tid
            hit = swap.stand_in.get(tid)
            if hit is not None:
                return hit
        tracks = self._tracks
        t = tracks.get(tid)
        if t is None or t.get("is_duplicate_primary", True):
            return tid
        for m in self._tag_dup_group.get(self._tag_key(t.get("duplicate_group_id") or ""), ()):
            mt = tracks.get(m)
            if mt is not None and mt.get("is_duplicate_primary", True):
                return swap.stand_in.get(m, m) if swap is not None else m
        return tid

    def tracks_for_format(self, fmt: str) -> list[dict]:
        """All track meta-dicts (no embedding) for one format, via the maintained
        ``_tag_format`` index — O(|format bucket|), not a full-library scan.  The
        index is keyed on the lowercased format string (same as ``_index_track`` /
        ``filter_tracks``)."""
        tids = self._tag_format.get(str(fmt).strip().lower(), set())
        return [self._meta_dict(tid) for tid in tids if tid in self._tracks]

    def update_track_fields(self, track_id: str, updates: dict) -> bool:
        """Update specific fields on an existing track.  Returns False only
        when the track doesn't exist.

        Diff-based: fields whose value is unchanged are dropped first; when
        nothing changed there is no index work, no ``_mutation_seq`` bump and no
        AOF record.  Otherwise only the derived indexes that read a changed
        field are maintained (``_reindex_changed``), the AOF record carries the
        changed fields, and ``_mutation_seq`` bumps unless every changed field
        is in ``_SEQ_EXEMPT_FIELDS`` — or the change is duration-only
        (``_duration_only``), which bumps ``_duration_seq`` instead.
        ``_browse_seq`` bumps when the change can alter a browse result
        (``_browse_change``); a duration write that can't otherwise
        (``_browse_duration_only``) bumps ``_duration_seq`` instead."""
        t = self._tracks.get(track_id)
        if not t:
            return False
        changed = _with_game(t, _diff_updates(t, updates))
        if not changed:
            return True
        self._apply_field_changes(track_id, t, changed)
        if _browse_change(changed):
            self._browse_seq += 1
        elif "duration" in changed:
            self._duration_seq += 1             # (``_browse_duration_only``)
        if _duration_only(changed):
            if _multi_tune(t):
                # (a multi-tune file's default-tune length decides whether
                # Subsonic lists its own entry — core/skip_rules)
                self._catalog_seq += 1
        else:
            if not changed.keys() <= _SEQ_EXEMPT_FIELDS:
                self._mutation_seq += 1
            if not _CATALOG_FIELDS.isdisjoint(changed):
                self._catalog_seq += 1
        self._aof("update_track_fields", id=track_id, data=changed)
        return True

    def update_track_fields_batch(self, items: list[tuple[str, dict]]) -> int:
        """Apply many field-updates in one batch AOF record.

        Recompute-duplicates and bulk-tag-edit paths used to call
        ``update_track_fields`` once per track — for a 170K-track library
        that's 170K AOF records and hundreds of MB of journal data, which
        starved every other write during the recompute.  One batched record
        plus a single ``_mutation_seq`` bump is dramatically cheaper.

        Same diff-based rules as ``update_track_fields``; returns the number
        of tracks that actually changed.
        """
        applied = 0
        bump = catalog = duration = browse = False
        records: list[dict] = []
        for tid, updates in items:
            t = self._tracks.get(tid)
            if not t:
                continue
            changed = _with_game(t, _diff_updates(t, updates))
            if not changed:
                continue
            self._apply_field_changes(tid, t, changed)
            applied += 1
            if _browse_change(changed):
                browse = True
            elif "duration" in changed:
                duration = True                 # (``_browse_duration_only``)
            if _duration_only(changed):
                catalog = catalog or _multi_tune(t)
            else:
                bump = bump or not changed.keys() <= _SEQ_EXEMPT_FIELDS
                catalog = catalog or not _CATALOG_FIELDS.isdisjoint(changed)
            records.append({"id": tid, "data": changed})
        if applied:
            if bump:
                self._mutation_seq += 1
            if catalog:
                self._catalog_seq += 1
            if duration:
                self._duration_seq += 1
            if browse:
                self._browse_seq += 1
            self._aof("update_track_fields_batch", data=records)
        return applied

    # ── Search ───────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 50, offset: int = 0) -> list[dict]:
        """Full-text search across indexed fields.

        Returns track dicts (without embedding) matching all query tokens.
        """
        if not query or query == "*":
            return self._paginate_all(limit, offset)

        result_ids = self._resolve_query(query)
        if result_ids is None:
            return []

        # ``heapq.nlargest`` is O(N log K) vs. the previous full
        # ``sorted(...)`` which was O(N log N).  For a query that matches
        # tens of thousands of tracks but only needs the first page back,
        # this collapses the work to the top ``offset+limit`` entries.
        top = heapq.nlargest(
            offset + limit,
            result_ids,
            key=lambda tid: self._tracks[tid].get("added_at", 0),
        )
        page = top[offset : offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    # Map a public ``sort_by`` value (the one the API exposes and the
    # column-header click sends) to the in-memory sorted index that drives
    # the paginated walk.  Single source of truth so the All Tracks /tracks
    # endpoint, ``filter_tracks``, and ``_paginate_all`` can't drift.
    #
    # ``added`` defaults to descending (newest first) because that's the
    # historical contract — every other key defaults to ascending and the
    # column header click flips with each press (handled at the API layer).
    _SORT_INDEX_MAP: dict[str, str] = {
        "added":        "_sorted_added_at",
        "year":         "_sorted_year",
        "duration":     "_sorted_duration",
        "bpm":          "_sorted_bpm",
        "title":        "_sorted_title",
        "artist":       "_sorted_artist",
        "album_artist": "_sorted_album_artist",
        "album":        "_sorted_album",
        "format":       "_sorted_format",
    }

    def _sort_index_attr(self, sort_by: str | None, *, filter_duplicates: bool) -> str:
        """Name of the sorted index (a ``SORTED_LISTS`` entry) for ``sort_by``.

        Falls back to ``_sorted_added_at`` (or its ``_primary`` variant when
        ``filter_duplicates`` is set) so callers that don't pass an explicit
        sort key still get the historical newest-first behaviour.
        """
        attr = (self._SORT_INDEX_MAP.get(sort_by)
                if sort_by and sort_by != "added" else None)
        if not attr:
            return "_sorted_added_at_primary" if filter_duplicates else "_sorted_added_at"
        return attr

    def _pick_sort_index(self, sort_by: str | None, *, filter_duplicates: bool) -> list[tuple]:
        """Return the sorted index list matching ``sort_by``
        (``_sort_index_attr``)."""
        return getattr(self, self._sort_index_attr(sort_by, filter_duplicates=filter_duplicates))

    def filter_tracks(
        self,
        artist: str | None = None,
        album_artist: str | None = None,
        album: str | None = None,
        genre: str | None = None,
        scene_group: str | None = None,
        format_: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        dir_hash: str | None = None,
        scan_root_hash: str | None = None,
        query: str | None = None,
        game: str | None = None,
        artist_match: str | None = None,
        album_artist_match: str | None = None,
        limit: int = 200,
        offset: int = 0,
        filter_duplicates: bool = False,
        sort_by: str | None = None,
        sort_order: str | None = None,
        exclude: "set[str] | frozenset[str] | None" = None,
        untagged: str | None = None,
        swap: PrimarySwap | None = None,
    ) -> list[dict]:
        """Filter tracks by tag and/or range criteria, intersecting results.

        ``exclude``: ids no page may contain (the catalogue tracks Subsonic
        clients must not see — ``core/catalogue_view.py``; the tracks
        "Tracks on unavailable drives: Hide" leaves out — ``core/availability``);
        pagination counts only the remaining tracks.

        ``swap`` (with ``filter_duplicates``): the duplicate groups whose
        primary can't play right now — their stand-in is listed instead of the
        primary (``primary_swap``; the web lists pass
        ``core.availability.primary_swap``).

        ``untagged``: only tracks whose ``artist`` / ``album_artist`` / ``album``
        / ``genre`` tag is blank — the "[No Artist]"-style drill-down, which
        :meth:`untagged_count` sizes.

        ``sort_by`` selects which pre-computed sorted index drives the page
        walk: one of ``title``, ``artist``, ``album``, ``format``, ``year``,
        ``duration``, ``bpm``, or ``added`` (default).  ``sort_order`` is
        ``"asc"`` or ``"desc"`` — falsy / unknown values default to ``desc``
        for ``added`` (newest first, historical) and ``asc`` for everything
        else (the natural reading order for a column header click).
        """
        if not filter_duplicates:
            swap = None
        # (With a swap the candidates are already the effective primaries:
        # ordered by the full list, which holds the stand-ins too — the
        # primary-only side list doesn't.)
        attr = self._sort_index_attr(sort_by,
                                     filter_duplicates=filter_duplicates and swap is None)
        descending = self._is_descending(sort_by, sort_order)
        # A deeper page of a list walked before resumes that walk
        # (``_FilterWalk``) instead of resolving the predicates and walking the
        # index from its start again.
        wkey = (artist, album_artist, album, genre, scene_group, format_, year_min, year_max,
                dir_hash, scan_root_hash, query, game, artist_match, album_artist_match,
                untagged, bool(filter_duplicates), id(exclude), id(swap), attr, descending)
        owner = self._memo_ok()
        walk = self._filter_walk_get(wkey, attr, exclude, swap) if owner else None
        if walk is not None:
            result = walk.member
            idx = walk.idx
        else:
            result = self._candidate_ids(
                artist=artist, album_artist=album_artist, album=album, genre=genre,
                scene_group=scene_group, format_=format_,
                year_min=year_min, year_max=year_max,
                dir_hash=dir_hash, scan_root_hash=scan_root_hash, query=query,
                game=game, artist_match=artist_match, album_artist_match=album_artist_match,
                untagged=untagged, filter_duplicates=filter_duplicates,
                swap=swap,
            )
            if result is None and (exclude or swap is not None):
                return self._paginate_excluding(limit, offset, exclude,
                                                filter_duplicates=filter_duplicates,
                                                sort_by=sort_by, sort_order=sort_order,
                                                swap=swap)
            if result is None:
                return self._paginate_all(
                    limit, offset,
                    filter_duplicates=filter_duplicates,
                    sort_by=sort_by, sort_order=sort_order,
                )
            if exclude and result:
                result = result.difference(exclude)           # (in C)
            if not result:
                return []
            idx = getattr(self, attr)
        if len(result) <= max(_SMALL_CANDIDATE_SET, len(idx) // 64):
            # A small candidate set (a ``game:`` / specific search) is sorted
            # directly — O(k log k) — instead of walking the whole library's
            # sorted index for it.  Same (value, id) keys and membership rule
            # as the index, so the page is identical.
            page = self._sort_candidates(result, attr, descending)
            if page is not None:
                return [self._meta_dict(tid) for tid in page[offset:offset + limit]]

        # Walk the chosen sorted index in the requested direction keeping only
        # tids that are in the candidate set — collecting the first
        # ``offset + limit`` of them (at least one) — and page that.  The walk
        # is kept, so the next page goes on from where this one stopped.
        if walk is None:
            walk = _FilterWalk(idx, self._primary_order_key(attr), exclude, swap,
                               result, descending)
            if owner:
                self._filter_walk_put(wkey, walk)
        need = offset + limit
        order = walk.upto(need if need > 0 else 1)
        if offset >= 0 and limit >= 0:
            page = order[offset:need]
        else:                       # (the walk's ``collected[offset:offset + limit]``)
            page = order[:need if need > 0 else 1][offset:offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    def _filter_walk_get(self, key: tuple, attr: str, exclude, swap) -> "_FilterWalk | None":
        """The kept ``filter_tracks`` order for ``key`` if it still holds — the
        same browse state (``_primary_order_key``), index list, ``exclude`` and
        ``swap`` objects it was walked with — else None (a stale one is
        dropped)."""
        memo = self._filter_walk_memo
        try:
            walk = memo.get(key)
        except TypeError:                       # an unhashable predicate value
            return None
        if walk is None:
            return None
        if (walk.gen == self._primary_order_key(attr) and walk.idx is getattr(self, attr)
                and walk.exclude is exclude and walk.swap is swap):
            memo[key] = memo.pop(key)           # (most recently used last)
            return walk
        del memo[key]
        return None

    def _filter_walk_put(self, key: tuple, walk: _FilterWalk) -> None:
        """Keep ``walk`` under ``key`` — after dropping the stale orders, and
        the least recently used ones past ``_FILTER_WALK_MAX`` orders /
        ``_FILTER_WALK_MAX_IDS`` ids held."""
        memo = self._filter_walk_memo
        try:
            memo.pop(key, None)
        except TypeError:                       # an unhashable predicate value: not kept
            return
        for k in [k for k, w in memo.items()
                  if w.gen != self._primary_order_key(k[-2]) or w.idx is not getattr(self, k[-2])]:
            del memo[k]
        held = sum(len(w.member) + len(w.out) for w in memo.values())
        size = len(walk.member)
        while memo and (len(memo) >= _FILTER_WALK_MAX or held + size > _FILTER_WALK_MAX_IDS):
            old = memo.pop(next(iter(memo)))
            held -= len(old.member) + len(old.out)
        memo[key] = walk

    def _sort_candidates(self, ids, attr: str, descending: bool) -> list[str] | None:
        """``ids`` in the order of sorted index ``attr`` (``descending`` = the
        reversed index), built from the same ``_SORTED_SPECS`` value function:
        ids the index leaves out (None: the primary-only and ``game:`` side
        lists) are left out, and ties break on the id in the same direction as
        the index walk.
        None when the keys don't compare (mixed types) — the caller then walks
        the index instead."""
        value = _SORTED_SPECS[attr][1]
        tracks = self._tracks
        rows = []
        for tid in ids:
            t = tracks.get(tid)
            if t is None:
                continue
            v = value(t)
            if v is not None:
                rows.append((v, tid))
        try:
            rows.sort(reverse=descending)
        except TypeError:
            return None
        return [tid for _v, tid in rows]

    # Fields the ``untagged`` predicate accepts (the "[No Artist]"-style views).
    _UNTAGGED_FIELDS = ("artist", "album_artist", "album", "genre")

    @classmethod
    def _untagged_test(cls, field: str):
        """THE "``field`` tag is missing/blank" test (``t`` → bool): what the
        ``untagged`` predicate lists and :meth:`untagged_count` counts, so a
        "[No Artist]" bucket and its drill-down cannot disagree.  Genre reads the
        genres the track is listed under (``track_genres``, derived ones too)."""
        if field not in cls._UNTAGGED_FIELDS:
            # An unknown field must not quietly mean "no predicate" (= everything).
            raise ValueError(f"untagged must be one of {cls._UNTAGGED_FIELDS}, got {field!r}")
        return lambda t: _untagged(t, field)

    def _untagged_ids(self, field: str) -> set[str]:
        """The maintained id set of the "``field`` tag is blank" bucket — the
        tracks ``_untagged_test(field)`` passes (raises like it for an unknown
        field).  The live set: callers must not change it."""
        self._untagged_test(field)
        return getattr(self, _UNTAGGED_ATTR[field])

    def _dup_invisible(self, swap: PrimarySwap | None) -> set[str]:
        """The ids a list with duplicates hidden leaves out: the non-primary
        copies — with a ``swap``, the effective ones: ``(non-primaries ∪
        demoted) − promoted``, the complement of ``PrimarySwap.is_primary``.
        Without a swap the live ``_nonprimary_ids`` (callers must not change
        it or keep it past a write); with one memoised per swap until the next
        browse write."""
        if swap is None:
            return self._nonprimary_ids
        seq = self._browse_seq                  # (read before the build: a write
        memo = self._dup_inv_memo               #  meanwhile leaves the entry stale)
        if memo is not None and memo[0] == seq and memo[1] is swap:
            return memo[2]
        inv = self._nonprimary_ids | swap.demoted
        inv -= swap.promoted
        if self._memo_ok():
            self._dup_inv_memo = (seq, swap, inv)
        return inv

    def _invisible(self, dedup: bool, exclude, swap: PrimarySwap | None = None):
        """The ids a list leaves out: ``exclude`` and — ``dedup`` — the
        duplicate copies it hides (``_dup_invisible``).  ``exclude`` itself
        (maybe None) without ``dedup``.  Memoised for one (exclude, swap) pair
        until the next browse write (kept only by the owner thread —
        ``_memo_ok``); never to be changed by callers."""
        if not dedup:
            return exclude
        seq = self._browse_seq                  # (read before the build)
        dinv = self._dup_invisible(swap)
        if not exclude:
            return dinv
        memo = self._inv_memo
        if (memo is not None and memo[0] == seq and memo[1] is exclude
                and memo[2] is swap):
            return memo[3]
        inv = set(exclude)
        inv |= dinv
        if self._memo_ok():
            self._inv_memo = (seq, exclude, swap, inv)
        return inv

    def _candidate_ids(
        self,
        *,
        artist: str | None = None,
        album_artist: str | None = None,
        album: str | None = None,
        genre: str | None = None,
        scene_group: str | None = None,
        format_: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        dir_hash: str | None = None,
        scan_root_hash: str | None = None,
        query: str | None = None,
        untagged: str | None = None,
        game: str | None = None,
        artist_match: str | None = None,
        album_artist_match: str | None = None,
        filter_duplicates: bool = False,
        swap: PrimarySwap | None = None,
    ) -> set[str] | None:
        """THE predicate → candidate-id-set resolver (single source of truth).

        Shared by :meth:`filter_tracks` (sorted pages), :meth:`filter_track_ids`
        (sampling) and the shuffled play order, so a list view and a shuffle over
        "the same filter" resolve the predicates identically.  (Whether duplicate
        copies are hidden is the CALLER's choice via ``filter_duplicates`` — the
        web views pass the setting, Subsonic/DLNA deliberately do not.)

        Returns ``None`` when NO predicate was given (= every track; callers take
        their all-tracks fast path), otherwise the intersected set — already
        reduced to duplicate-group primaries when ``filter_duplicates`` is set
        (with ``swap``: the stand-ins instead of the primaries that can't play).
        It may be a LIVE index set (one predicate, nothing left out): callers
        must not change it.

        ``artist`` / ``album_artist`` are exact (case-insensitive) names (an
        artist's page); ``artist_match`` / ``album_artist_match`` (the search
        box's ``artist:`` / ``album_artist:``) match a word start in a name
        (``_name_word_hits``).
        ``album`` is an exact (case-insensitive) match; ``game`` (the search
        box's ``game:`` operator) is a case-insensitive PREFIX match on the
        ``game`` field — any format: a retro track's game (header, Modland,
        song database, folder name), a modern file's GAME tag, one the user
        typed — or, for a SID / Atari ST tune whose game is unknown or only
        guessed from its folder or a Modland file name (and not set by the
        user), on its title (HVSC rips name the game there: ``game:uridium`` finds the game-less
        "Uridium 2" SIDs as well as the game "Uridium"; a tracker module
        titled "uridium remix" is not a game).  A leading "The" is
        ignored on either side (``game:"last ninja"`` also finds "The Last
        Ninja 3", and vice versa), and so are accents and runs of whitespace,
        as in plain search (``game:pokemon`` finds "Pokémon", ``game:"real evil
        180s"`` finds "real evil      180s").  A walk of the distinct games
        (``_tag_game``) plus bisect ranges over the title lists.
        """
        sets: list[set[str]] = []
        if artist:
            sets.append(self._tag_artist.get(artist.lower(), set()))
        if album_artist:
            sets.append(self._tag_album_artist.get(album_artist.lower(), set()))
        if artist_match is not None:
            sets.append(_name_word_hits(self._tag_artist, artist_match))
        if album_artist_match is not None:
            sets.append(_name_word_hits(self._tag_album_artist, album_artist_match))
        if album:
            sets.append(self._tag_album.get(album.lower(), set()))
        if genre:
            sets.append(self._tag_genre.get(genre.lower(), set()))
        if scene_group:
            sets.append(self._tag_scene_group.get(scene_group.lower(), set()))
        if format_:
            sets.append(self._tag_format.get(str(format_).strip().lower(), set()))
        if dir_hash:
            sets.append(self._tag_dir_hash.get(dir_hash, set()))
        if scan_root_hash:
            sets.append(self._tag_scan_root_hash.get(scan_root_hash, set()))
        if year_min is not None or year_max is not None:
            lo = year_min if year_min is not None else -999999
            hi = year_max if year_max is not None else 999999
            sets.append(set(_sorted_range(self._sorted_year, lo, hi)))
        if query:
            q_ids = self._resolve_query(query)
            if q_ids is not None:
                sets.append(q_ids)
        if game is not None:
            g = " ".join(str(game).split()).lower()
            if g:
                # Like the year range above, this reads the sorted lists as they
                # stand: inside a batch section (a scan commit) they catch up on
                # the batch exit, so a hit may be a since-deleted id — dropped.
                tracks = self._tracks

                def variants(q: str) -> set[str]:
                    # The same game with and without its article: the library
                    # spells it both ways ("The Last Ninja" / "Last Ninja 2").
                    return ({q, q[4:]} if q.startswith("the ") and len(q) > 4
                            else {q, "the " + q})
                gf = _game_key(g)
                folded = variants(gf)
                hits: set[str] = set()
                # The game field, any format: its index is keyed on the folded
                # game, so the folded query covers accents and odd spacing.
                starts = tuple(folded)
                for key, tids in self._tag_game.items():
                    if key.startswith(starts):
                        hits.update(tids)
                # A SID / Atari ST tune without a sure game: its title.  Raw prefixes
                # over the lexical list (an accented query finds the accented
                # spelling), folded ones over it and its side list ("pokemon"
                # finds "Pokémon", "real evil 180s" finds "real evil      180s").
                for lst, prefixes in ((self._sorted_title, variants(g) | folded),
                                      (self._sorted_title_fold, folded)):
                    for p in prefixes:
                        lo = bisect.bisect_left(lst, (p,))
                        hi = bisect.bisect_left(lst, (p + "\uffff",))
                        hits.update(tid for _, tid in lst[lo:hi]
                                    if _game_title_hit(tracks.get(tid)))
                hits &= tracks.keys()
                sets.append(hits)
        if untagged:
            # Tracks whose tag is missing/blank: the maintained bucket (the
            # live set — nothing below changes it in place).
            sets.append(self._untagged_ids(untagged))

        if not sets:
            return None

        result = sets[0]
        for s in sets[1:]:
            result = result & s
            if not result:
                return set()

        if filter_duplicates:
            # The copies the list hides, as one set difference (in C) — the
            # complement of the per-row ``is_duplicate_primary`` /
            # ``PrimarySwap.is_primary`` test.
            inv = self._dup_invisible(swap)
            if inv:
                result = result.difference(inv)
        return result

    def _paginate_excluding(self, limit: int, offset: int, exclude, *,
                            filter_duplicates: bool = False,
                            sort_by: str | None = None,
                            sort_order: str | None = None,
                            swap: PrimarySwap | None = None) -> list[dict]:
        """``_paginate_all`` without the ``exclude`` ids (and, with
        ``filter_duplicates`` and a ``swap``, with the stand-ins in place of the
        primaries that can't play): a walk of the sort index skipping the rest
        (O(offset + limit + skipped)) — or, for a page at an offset of at least
        ``_PRIMARY_ORDER_MIN_OFFSET``, a slice of the memoised order of what is
        listed (``_excluded_order``)."""
        if not filter_duplicates:
            swap = None
        # A swap walks the full list: the primary-only side list of the default
        # sort lacks the stand-ins (same keys, so the same order).
        attr = self._sort_index_attr(sort_by,
                                     filter_duplicates=filter_duplicates and swap is None)
        idx = getattr(self, attr)
        descending = self._is_descending(sort_by, sort_order)
        if offset >= _PRIMARY_ORDER_MIN_OFFSET:
            return self._deep_page(
                lambda desc: self._excluded_order(attr, idx, exclude, filter_duplicates,
                                                  swap, desc),
                offset, limit, descending)
        walk_iter = reversed(idx) if descending else iter(idx)
        skipped = 0
        collected: list[str] = []
        tracks = self._tracks
        if filter_duplicates:
            # Per row with set lookups only — a shallow page reads a few rows,
            # so no union is built for it: not excluded, in the library, and
            # shown of its group (``PrimarySwap.is_primary``: a stand-in, or a
            # primary not stood in for).
            ex = exclude or ()
            nonprim = self._nonprimary_ids
            get = tracks.get
            promoted = swap.promoted if swap is not None else ()
            demoted = swap.demoted if swap is not None else ()
            for _, tid in walk_iter:
                if tid in ex or not get(tid):
                    continue
                if tid not in promoted and (tid in nonprim or tid in demoted):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                collected.append(tid)
                if len(collected) >= limit:
                    break
            return [self._meta_dict(tid) for tid in collected if tid in tracks]
        ex = exclude or ()
        for _, tid in walk_iter:
            if tid in ex:
                continue
            if skipped < offset:
                skipped += 1
                continue
            collected.append(tid)
            if len(collected) >= limit:
                break
        return [self._meta_dict(tid) for tid in collected if tid in tracks]

    # Deep-page orders kept (``_excluded_order``): one per sort index, duplicates
    # setting, exclude set and swap in use — every column of a deduped list in
    # Mark mode with a swap, and of Hide mode too, without evicting each other.
    _EXCLUDED_ORDER_MEMO_MAX = 32

    def _deep_page(self, walk_of, offset: int, limit: int, descending: bool) -> list[dict]:
        """A deep page of a memoised list order: ``walk_of(desc)`` gives the
        order walked in that direction (``_FilterWalk``), read only as far as
        the page needs.  (A negative offset / limit: the full ascending order,
        sliced as a list of it always was.)"""
        if offset >= 0 and limit >= 0:
            page = walk_of(descending).page(offset, limit)
        else:
            order = walk_of(False).full()
            n = len(order)
            if descending:
                page = order[max(0, n - offset - limit):max(0, n - offset)][::-1]
            else:
                page = order[offset:offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    def _excluded_order(self, attr: str, idx: list[tuple], exclude,
                        filter_duplicates: bool,
                        swap: PrimarySwap | None = None,
                        descending: bool = False) -> _FilterWalk:
        """The ids of sorted index ``attr`` (``idx``) that are not in
        ``exclude`` (and, with ``filter_duplicates``, are duplicate-group
        primaries — with a ``swap``, the effective ones — in the library), in
        index order (``descending``: from its end) — a walk read as far as the
        deepest page so far (``_FilterWalk``).  Memoised like
        ``_primary_order`` — until the next browse write or the list is
        replaced — and only for the exclude set and swap it was built with
        (compared by identity: callers hand the same objects back until they
        change).  A page at offset k reads O(k) index rows once, then a deeper
        one goes on from there."""
        # (Keyed by the exclude set too: two callers with their own sets — the
        # web's Hide set, the Subsonic API's catalogue exclusion — must not
        # throw each other's orders away on every page.)
        mkey = (attr, bool(filter_duplicates), id(exclude), id(swap))
        key = self._primary_order_key(attr)
        memo = self._excluded_order_memo
        hit = memo.get(mkey)
        if not self._memo_ok():
            walks = {}                  # (off the loop: a private walk — ``_memo_ok``)
        elif (hit is not None and hit[0] is idx and hit[1] == key and hit[2] is exclude
                and hit[4] is swap):
            walks = hit[3]
        else:
            # Drop what can't be hit again (written since, or for the same id
            # slot of a replaced set) and keep a few.
            for k in [k for k, e in memo.items()
                      if e[1] != self._primary_order_key(k[0]) or k == mkey]:
                memo.pop(k, None)
            while len(memo) >= self._EXCLUDED_ORDER_MEMO_MAX:
                memo.pop(next(iter(memo)))
            walks = {}
            memo[mkey] = (idx, key, exclude, walks, swap)
        walk = walks.get(bool(descending))
        if walk is None:
            if filter_duplicates:
                # Not excluded, not a hidden copy (``_invisible`` — with a swap
                # the effective primaries), and in the library (a row a
                # deferred batch rebuild still lists after its delete is left
                # out).
                walk = _FilterWalk(idx, key, exclude, swap, self._invisible(True, exclude, swap),
                                   bool(descending), _FilterWalk._OUT_LISTED, self._tracks)
            else:
                walk = _FilterWalk(idx, key, exclude, swap, exclude or (), bool(descending),
                                   _FilterWalk._OUT)
            walks[bool(descending)] = walk
        return walk

    def filter_track_ids(self, *, filter_duplicates: bool = False,
                         swap: PrimarySwap | None = None,
                         **predicates) -> list[str]:
        """Eligible track ids for the given filters — without sorting or
        paginating.  For callers that need the candidate SET to sample or order
        (``getRandomSongs``, the shuffled play order) rather than a sorted page.

        Accepts every :meth:`_candidate_ids` predicate, so its semantics are
        identical to :meth:`filter_tracks` by construction.  With no predicate it
        returns every id — or every duplicate-group PRIMARY when
        ``filter_duplicates`` is set, matching what the deduped All-Tracks list
        shows (with ``swap``: the stand-ins instead of the primaries that can't
        play)."""
        if not filter_duplicates:
            swap = None
        result = self._candidate_ids(filter_duplicates=filter_duplicates, swap=swap,
                                     **predicates)
        if result is not None:
            return list(result)
        if filter_duplicates:
            # The same test ``_candidate_ids`` and ``primary_track_count`` use.  NOT
            # ``_sorted_added_at_primary``: that is a sort index, behind the live
            # tracks while a batch section defers its rebuild, so it cannot stand
            # in for the set.
            # Memoised on ``_browse_seq`` like the aggregates (one pass over the
            # ids with a set lookup each, in library order).
            cached = self._agg_cache_get("primary_track_ids")
            if cached is None:
                nonprim = self._nonprimary_ids
                cached = [tid for tid in self._tracks if tid not in nonprim]
                self._agg_cache_set("primary_track_ids", cached)
            if swap is None:
                return list(cached)
            demoted = swap.demoted
            return ([tid for tid in cached if tid not in demoted]
                    + [tid for tid in swap.promoted if tid in self._tracks])
        return list(self._tracks.keys())

    @staticmethod
    def _is_descending(sort_by: str | None, sort_order: str | None) -> bool:
        """Resolve the effective sort direction.

        Explicit ``sort_order`` wins; otherwise ``added`` defaults to desc
        (newest first, historical behaviour) and everything else defaults
        to asc (natural reading order for a fresh column click).
        """
        if sort_order:
            o = sort_order.strip().lower()
            if o in ("desc", "descending", "down", "d"):
                return True
            if o in ("asc", "ascending", "up", "a"):
                return False
        # No explicit order: only ``added`` (and its default-empty alias)
        # defaults to descending.
        return not sort_by or sort_by == "added"

    def _resolve_query(self, query: str) -> set[str] | None:
        """Resolve a text query to a set of matching track IDs: every word
        must start a word of the track (a one-character word: be one, if any
        track has it).

        Query tokens are accent-folded: every non-ASCII word is indexed under
        its folded form too, so "beyonce" and "beyoncé" both find "Beyoncé"
        and "Beyonce", and "öö" prefix-matches "Öörni"."""
        tokens = [fold_token(t) for t in _tokenize_text(query)]
        if not tokens:
            return None

        words = self._query_word_list()
        sets: list[set[str]] = []
        for token in tokens:
            # Every word of 2+ characters is a prefix ("var" finds "Varga" even
            # when some track has the word "var" — GitHub #25); a lone
            # character matches that word only, unless no track has it.
            exact = self._word_index.get(token) if len(token) < 2 else None
            if exact:
                sets.append(exact)
            else:
                prefix_match = self._prefix_match(token, words)
                if prefix_match:
                    sets.append(prefix_match)
                else:
                    return set()

        if not sets:
            return set()

        result = sets[0].copy()
        for s in sets[1:]:
            result &= s
        return result

    def _query_word_list(self) -> list[str]:
        """The sorted vocabulary a prefix lookup bisects: ``_word_list``
        brought up to date (``_rebuild_word_list``) — or, off the owner thread
        while it is out of date, a private sorted copy (the shared list and its
        account are only ever changed by the owner — ``_memo_ok``)."""
        if not self._memo_ok():
            # Read the flag once: a rebuild here could race the owner's
            # writes (a word indexed but left out of a list marked clean).
            return sorted(self._word_index) if self._word_list_dirty else self._word_list
        self._rebuild_word_list()
        return self._word_list

    def _prefix_match(self, prefix: str, words: "list[str] | None" = None) -> set[str]:
        """Find all track IDs matching tokens that start with prefix
        (``words``: the sorted vocabulary — ``_query_word_list``)."""
        if words is None:
            words = self._word_list
        lo = bisect.bisect_left(words, prefix)
        hi = bisect.bisect_right(words, prefix + "\uffff")
        result: set[str] = set()
        for word in words[lo:hi]:
            result |= self._word_index.get(word, set())
        return result

    def _paginate_all(
        self,
        limit: int,
        offset: int,
        *,
        filter_duplicates: bool = False,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> list[dict]:
        """Return a paginated slice of all tracks.

        Default sort = ``added`` desc (newest first, historical contract).
        With ``filter_duplicates=True`` the pre-built
        ``_sorted_added_at_primary`` index drives a pure slice when the
        default sort is in use; for other sort keys a page at an offset of at
        least ``_PRIMARY_ORDER_MIN_OFFSET`` slices the memoised primary-only
        order of that index (``_primary_order``), and a shallower one filters
        duplicates per row while walking it.  Without the duplicate filter
        every sort key is a plain slice of its index.
        """
        descending = self._is_descending(sort_by, sort_order)

        # Fast path: default sort + no filter_duplicates ⇒ pure slice
        # off the already-sorted ``_sorted_added_at`` index.  This is the
        # path the windowed All Tracks view hits on the very first page
        # so it stays O(limit) regardless of library size.
        if (not sort_by or sort_by == "added") and not filter_duplicates:
            idx = self._sorted_added_at
            n = len(idx)
            if descending:
                # newest first → walk from the tail
                start = max(0, n - offset - limit)
                end = n - offset
                if end <= 0:
                    return []
                page = idx[max(start, 0) : end]
                return [self._meta_dict(tid) for _, tid in reversed(page) if tid in self._tracks]
            else:
                # oldest first → slice from the head
                page = idx[offset : offset + limit]
                return [self._meta_dict(tid) for _, tid in page if tid in self._tracks]

        # filter_duplicates + default sort retains its primary-variant fast
        # path so the duplicates-hidden default page is still O(limit).
        if (not sort_by or sort_by == "added") and filter_duplicates:
            idx = self._sorted_added_at_primary
            n = len(idx)
            if descending:
                start = max(0, n - offset - limit)
                end = n - offset
                if end <= 0:
                    return []
                page = idx[max(start, 0) : end]
                return [self._meta_dict(tid) for _, tid in reversed(page) if tid in self._tracks]
            else:
                page = idx[offset : offset + limit]
                return [self._meta_dict(tid) for _, tid in page if tid in self._tracks]

        # General path for non-default sort keys.
        attr = self._sort_index_attr(sort_by, filter_duplicates=filter_duplicates)
        idx = getattr(self, attr)
        if not filter_duplicates:
            # Every row counts (no duplicate filter): a plain slice — O(limit)
            # instead of walking ``offset`` rows.
            n = len(idx)
            if descending:
                page = idx[max(0, n - offset - limit):max(0, n - offset)]
                page = page[::-1]
            else:
                page = idx[offset:offset + limit]
            return [self._meta_dict(tid) for _v, tid in page if tid in self._tracks]
        if offset >= _PRIMARY_ORDER_MIN_OFFSET:
            # A deep page of the primary-only order: the memoised walk of it,
            # read only as deep as the page (a deep page cost 68-136 ms at 263K
            # tracks walking ``offset`` rows per request).
            return self._deep_page(lambda desc: self._primary_order(attr, idx, desc),
                                   offset, limit, descending)

        # A shallow page with duplicates hidden: walk the chosen sorted index
        # in the requested direction, filtering duplicates per row (cheaper
        # than building the primary-only order for it).
        walk_iter = reversed(idx) if descending else iter(idx)

        # Consume offset items first, then collect limit items.
        skipped = 0
        collected: list[str] = []
        get = self._tracks.get
        nonprim = self._nonprimary_ids
        for _, tid in walk_iter:
            if filter_duplicates:
                if tid in nonprim or not get(tid):
                    continue
            if skipped < offset:
                skipped += 1
                continue
            collected.append(tid)
            if len(collected) >= limit:
                break
        return [self._meta_dict(tid) for tid in collected if tid in self._tracks]

    def _primary_order_key(self, attr: str) -> tuple[int, int]:
        # A duration-only write (the render / probe backfill on a first play)
        # re-keys only ``_sorted_duration``, so only that order keys on it.
        return (self._browse_seq,
                self._duration_seq if attr == "_sorted_duration" else -1)

    def _primary_order(self, attr: str, idx: list[tuple],
                       descending: bool = False) -> _FilterWalk:
        """The ids of sorted index ``attr`` (``idx``) that are duplicate-group
        primaries in the library, in index order (``descending``: from its
        end) — a walk read as far as the deepest page so far
        (``_FilterWalk``), memoised until the next browse write
        (``_browse_seq``; for ``_sorted_duration`` also ``_duration_seq``, a
        duration-only write re-keys it) or until the list itself is replaced
        (a batch exit rebuilds it without a new write).  A page at offset k
        reads O(k) index rows once (two set lookups a row), then a deeper one
        goes on from there.  A rebuild also drops every other stale entry, so
        the memo never keeps a replaced sorted list (~17 MB of tuples at 263K
        tracks) alive."""
        key = self._primary_order_key(attr)
        memo = self._primary_order_memo
        hit = memo.get(attr)
        if not self._memo_ok():
            walks = {}                  # (off the loop: a private walk — ``_memo_ok``)
        elif hit is not None and hit[0] is idx and hit[1] == key:
            walks = hit[2]
        else:
            for a in [a for a, (lst, k, _o) in memo.items()
                      if k != self._primary_order_key(a) or lst is not getattr(self, a, None)]:
                del memo[a]
            walks = {}
            memo[attr] = (idx, key, walks)
        walk = walks.get(bool(descending))
        if walk is None:
            walk = walks[bool(descending)] = _FilterWalk(
                idx, key, None, None, self._nonprimary_ids, bool(descending),
                _FilterWalk._OUT_LISTED, self._tracks)
        return walk

    def _meta_dict(self, tid: str) -> dict:
        t = self._tracks.get(tid)
        if not t:
            return {}
        return {k: v for k, v in t.items() if k != "embedding"}

    # ── Aggregations ─────────────────────────────────────────────────────

    def _agg_cache_get(self, key: str) -> Any | None:
        """Return the cached aggregation result for ``key`` if still valid.

        The cache is keyed on ``_browse_seq`` so any track upsert / delete or
        browse-relevant field update since the previous call invalidates every
        entry without an explicit ``clear()``.  (Every value cached here reads
        only fields outside ``_BROWSE_EXEMPT_FIELDS``.)  Values are stored by
        the owner thread only (``_agg_cache_set``): one computed off the loop
        could have been built from the tracks before a write the loop made
        meanwhile, and would be filed under the counter after it.
        """
        entry = self._agg_cache.get(key)
        if entry is not None and entry[0] == self._browse_seq:
            return entry[1]
        return None

    def _agg_cache_set(self, key: str, value: Any) -> Any:
        if self._memo_ok():
            self._agg_cache[key] = (self._browse_seq, value)
        return value

    def _agg_x_get(self, key: str, exclude, swap: PrimarySwap | None = None) -> Any | None:
        """``_agg_cache_get`` for a result computed WITHOUT the ids of
        ``exclude`` (the tracks the web UI hides while their drive is
        offline) and with the stand-ins of ``swap`` (``primary_swap``): valid
        until the next browse write (``_browse_seq``) or another exclude set /
        swap (compared by identity — callers hand the same objects back until
        they change).  One entry per key, so a new set replaces the old results
        instead of piling up; stored by the owner thread only (as
        ``_agg_cache_set``)."""
        entry = self._agg_x_cache.get(key)
        if (entry is not None and entry[0] == self._browse_seq and entry[1] is exclude
                and entry[3] is swap):
            return entry[2]
        return None

    def _agg_x_set(self, key: str, exclude, value: Any, swap: PrimarySwap | None = None) -> Any:
        if self._memo_ok():
            self._agg_x_cache[key] = (self._browse_seq, exclude, value, swap)
        return value

    def _count_visible(self, tids, primary_only: bool, exclude,
                       swap: PrimarySwap | None = None) -> int:
        """How many of ``tids`` a list shows that leaves out ``exclude`` (and,
        with ``primary_only``, non-primary duplicates — with a ``swap``, the
        effective ones) — the count the matching ``filter_tracks(...,
        exclude=…, swap=…)`` drill-down returns."""
        if not primary_only:
            return len(tids) - len(exclude.intersection(tids))   # set ops, in C
        # ``exclude`` and the hidden copies in one set (``_invisible``): the
        # shown ones are the rest — set arithmetic in C, no per-row test.
        inv = self._invisible(True, exclude, swap)
        return len(tids) - len(tids.intersection(inv)) if inv else len(tids)

    def _bucket_count(self, tids, primary_only: bool, exclude,
                      swap: PrimarySwap | None) -> int | None:
        """A browse bucket's count when ``exclude`` or ``swap`` touches it
        (``_count_visible``), else None — the caller's own count holds."""
        if exclude and not exclude.isdisjoint(tids):
            return self._count_visible(tids, primary_only, exclude, swap)
        if swap is not None and not swap.changed.isdisjoint(tids):
            return self._count_visible(tids, True, exclude, swap)
        return None

    def visible_track_count(self, primary_only: bool = False, exclude=None,
                            swap: PrimarySwap | None = None) -> int:
        """The length of the All-Tracks list: every track (``primary_only``:
        duplicate-group primaries only — with a ``swap``, the stand-ins in place
        of the primaries that can't play) minus the ``exclude`` ids — what
        ``filter_tracks(filter_duplicates=primary_only, exclude=exclude,
        swap=swap)`` pages through."""
        base = self.primary_track_count() if primary_only else len(self._tracks)
        if not primary_only:
            if not exclude:
                return base
            # (Set operations in C, ~13 ms at 270K ids: no memo — one would be
            # dropped by every track write; the Python loop cost ~90 ms.)
            ex = exclude if isinstance(exclude, (set, frozenset)) else set(exclude)
            return max(0, base - (len(ex) - len(ex.difference(self._tracks))))
        n = base
        if exclude:
            key = "visible:1"
            gone = self._agg_x_get(key, exclude)
            if gone is None:
                # The excluded ids that are primaries in the library: those in
                # the library less the non-primary ones (all in the library).
                ex = exclude if isinstance(exclude, (set, frozenset)) else set(exclude)
                gone = ((len(ex) - len(ex.difference(self._tracks)))
                        - len(ex.intersection(self._nonprimary_ids)))
                self._agg_x_set(key, exclude, gone)
            n -= gone
        if swap is not None:
            # The stand-ins come in, the primaries they stand in for go — each
            # counted only where ``exclude`` didn't already leave it out.
            if exclude:
                n += (len(swap.promoted.difference(exclude))
                      - len(swap.demoted.difference(exclude)))
            else:
                n += len(swap.promoted) - len(swap.demoted)
        return max(0, n)

    def untagged_count(self, field: str, primary_only: bool = False, exclude=None,
                       swap: PrimarySwap | None = None) -> int:
        """The size of the "[No Artist]"-style bucket for ``field``: how many rows
        ``filter_tracks(untagged=field, filter_duplicates=primary_only,
        exclude=exclude, swap=swap)`` lists.  Counted from the same blank-tag
        test, never as "all tracks minus the tagged buckets" — a multi-artist
        track ("A • B") sits in two artist buckets, so that subtraction comes
        out short (or negative) and the bucket vanished while its drill-down
        still listed tracks.  The maintained bucket (``_untagged_ids``) less
        what the list hides (``_invisible``) — set arithmetic, memoised until
        the next browse write (or another ``exclude`` set / swap)."""
        ids = self._untagged_ids(field)
        if not primary_only:
            swap = None
        key = f"untagged:{field}:{int(bool(primary_only))}"
        cached = (self._agg_x_get(key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(key))
        if cached is not None:
            return cached
        inv = self._invisible(bool(primary_only), exclude, swap)
        n = len(ids) - len(ids.intersection(inv)) if inv else len(ids)
        if exclude or swap is not None:
            return self._agg_x_set(key, exclude, n, swap)
        return self._agg_cache_set(key, n)

    def _count_primaries(self, tids: set[str], swap: PrimarySwap | None = None) -> int:
        """Number of ``tids`` that are duplicate-group primaries (with a
        ``swap``: effective primaries — ``PrimarySwap.is_primary``).

        Uses the exact same predicate as ``filter_tracks(filter_duplicates=
        True)`` — ``is_duplicate_primary`` defaulting to True for any track not
        in a duplicate group — so an aggregate legend count computed with this
        helper agrees with what the corresponding drill-down actually returns.
        Set arithmetic against ``_dup_invisible`` (the complement of that
        predicate) for a set of ids; any other iterable is counted per row.
        """
        if not isinstance(tids, (set, frozenset)):
            tracks = self._tracks
            if swap is not None:
                return sum(1 for tid in tids if swap.is_primary(tid, tracks.get(tid, {})))
            return sum(1 for tid in tids
                       if tracks.get(tid, {}).get("is_duplicate_primary", True))
        inv = self._dup_invisible(swap)
        return len(tids) - len(tids.intersection(inv)) if inv else len(tids)

    def primary_track_count(self) -> int:
        """Total tracks excluding non-primary duplicates.

        The deduped analogue of ``track_count`` — the length of the deduped
        All-Tracks list (``visible_track_count(primary_only=True)``): every
        track less the maintained non-primary copies (all in the library).
        """
        return len(self._tracks) - len(self._nonprimary_ids)

    def aggregate_artists(self, primary_only: bool = False, exclude=None,
                          swap: PrimarySwap | None = None) -> list[dict]:
        """All artists with track counts, sorted alphabetically.

        With ``primary_only`` the count excludes non-primary duplicates so the
        legend agrees with the deduped drill-down (``/search/filter?artist=X``
        → ``filter_tracks(filter_duplicates=True)``); an artist whose only
        tracks are hidden duplicate copies is dropped instead of showing an
        inflated (or fully "ghost") count — the same class of bug the format
        legend had.

        ``primary_only`` is a PARAMETER, not read from the global
        ``filter_duplicates`` config, precisely so it stays opt-in per caller:
        the web library / Galaxy layer passes it (mapped from that config), but
        the Subsonic / DLNA consumers keep their historical RAW counts — their
        track *listings* don't dedup, so a primary-only count there would just
        disagree with the songs they still serve.  Cached under a distinct
        ``:primary`` key so a runtime toggle can't serve a list computed under
        the other mode.
        """
        hide_dups = primary_only
        if not hide_dups:
            swap = None
        cache_key = "artists:primary" if hide_dups else "artists"
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_artists.items():
            tids = self._tag_artist.get(key)
            if not tids:
                continue
            touched = self._bucket_count(tids, hide_dups, exclude, swap)
            if touched is not None:
                count = touched
                if count == 0:
                    continue
            elif hide_dups:
                count = self._count_primaries(tids)
                if count == 0:
                    continue
            tid = min(tids)                  # deterministic display spelling
            t = self._tracks.get(tid)
            name = name_display(t.get("artist"), key) if t else key
            results.append({"artist": name or key, "count": count})
        results.sort(key=lambda x: x["artist"].lower())
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    def aggregate_album_artists(self, primary_only: bool = False, exclude=None,
                                swap: PrimarySwap | None = None) -> list[dict]:
        """Album-artists with track counts.  Mirrors ``aggregate_artists``'s
        ``primary_only`` handling — caller-controlled primary-only counts, drop
        0-count entries, distinct ``:primary`` cache key — so the web Album
        Artists browse legend stays consistent with its deduped drill-down
        while Subsonic / DLNA keep raw counts."""
        hide_dups = primary_only
        if not hide_dups:
            swap = None
        cache_key = "album_artists:primary" if hide_dups else "album_artists"
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_album_artists.items():
            tids = self._tag_album_artist.get(key)
            if not tids:
                continue
            touched = self._bucket_count(tids, hide_dups, exclude, swap)
            if touched is not None:
                count = touched
                if count == 0:
                    continue
            elif hide_dups:
                count = self._count_primaries(tids)
                if count == 0:
                    continue
            tid = min(tids)                  # deterministic display spelling
            t = self._tracks.get(tid)
            name = name_display(t.get("album_artist"), key) if t else key
            results.append({"album_artist": name or key, "count": count})
        results.sort(key=lambda x: x["album_artist"].lower())
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    def aggregate_albums(
        self, artist: str | None = None, album_artist: str | None = None,
        primary_only: bool = False, exclude=None, swap: PrimarySwap | None = None,
    ) -> list[dict]:
        """Albums, optionally filtered by artist/album_artist.

        With ``primary_only`` the count excludes non-primary duplicates so the
        "N Tracks" legend agrees with the deduped album drill-down
        (``/search/filter?album=X`` intersected with the same artist /
        album_artist scope, ``filter_duplicates=True``); an album whose members
        are all hidden duplicates is dropped.  Same caller-controlled,
        Subsonic/DLNA-stays-raw contract as the other browse aggregates."""
        if not primary_only:
            swap = None
        # Cache key embeds the filter args + dup mode so each combination
        # caches independently.
        suffix = ":primary" if primary_only else ""
        cache_key = (
            f"albums::{(artist or '').lower()}::{(album_artist or '').lower()}{suffix}"
        )
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached

        # ``scope`` restricts an album's global tid bucket to the requested
        # artist / album_artist so a primary count matches what the scoped
        # drill-down (`?album=X&artist=Y`) actually returns.  ``None`` = whole
        # library (unfiltered albums view → `?album=X` only).
        if artist:
            album_counter = self._agg_albums_by_artist.get(artist.lower(), Counter())
            # ``or set()`` so a filtered-but-missing key means "no members"
            # (skip), never falls through to the ``scope is None`` whole-library
            # path below.
            scope = self._tag_artist.get(artist.lower()) or set()
        elif album_artist:
            album_counter = self._agg_albums_by_album_artist.get(
                album_artist.lower(), Counter(),
            )
            scope = self._tag_album_artist.get(album_artist.lower()) or set()
        else:
            album_counter = self._agg_albums
            scope = None

        results: list[dict] = []
        for key, count in album_counter.items():
            tids = self._tag_album.get(key)
            if not tids:
                continue
            if ((exclude and not exclude.isdisjoint(tids))
                    or (swap is not None and not swap.changed.isdisjoint(tids))):
                # Some members are hidden (their drive is offline) or stand in
                # for a primary that can't play: count and pick the cover from
                # the ones the drill-down lists.
                members = tids if scope is None else (tids & scope)
                inv = self._invisible(primary_only, exclude, swap)
                shown = members.difference(inv) if inv else members
                count = len(shown)
                if count == 0:
                    continue
                rep = min(shown)
            elif primary_only:
                members = tids if scope is None else (tids & scope)
                # Prefer a PRIMARY representative so the cover-art id points at
                # a track the drill-down actually returns: the first one in the
                # members' order.
                nonprim = self._nonprimary_ids
                count = len(members) - len(members.intersection(nonprim))
                if count == 0:
                    continue
                rep = next(x for x in members if x not in nonprim)
            else:
                rep = next(iter(tids))
            t = self._tracks.get(rep)
            name = (t.get("album") or "").strip() if t else key
            # ``track_id`` is a representative track for the album so the
            # frontend can build its cover-art URL directly instead of
            # round-tripping a /search/filter lookup per grid card.
            results.append({"album": name or key, "count": count, "track_id": rep})
        results.sort(key=lambda x: x["album"].lower())
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    def aggregate_genres(self, primary_only: bool = False, exclude=None,
                         swap: PrimarySwap | None = None) -> list[dict]:
        """Genres with track counts.  With ``primary_only`` the count excludes
        non-primary duplicates so the legend agrees with the deduped drill-down
        (``/search/filter?genre=X``); a genre whose members are all hidden
        duplicates is dropped instead of showing a "ghost" count.  Like the
        other browse aggregates ``primary_only`` is caller-controlled (web
        library opts in; Subsonic keeps raw).  Distinct ``:primary`` cache
        key, mirroring the format legend."""
        hide_dups = primary_only
        if not hide_dups:
            swap = None
        cache_key = "genres:primary" if hide_dups else "genres"
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_genres.items():
            tids = self._tag_genre.get(key)
            if not tids:
                continue
            touched = self._bucket_count(tids, hide_dups, exclude, swap)
            if touched is not None:
                count = touched
                if count == 0:
                    continue
            elif hide_dups:
                count = self._count_primaries(tids)
                if count == 0:
                    continue
            # Resolve proper-cased display name from one track
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = key  # fallback
            if t:
                for g in track_genres(t):
                    if g.strip().lower() == key:
                        name = g.strip()
                        break
            results.append({"genre": name, "count": count})
        results.sort(key=lambda x: x["genre"].lower())
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    def aggregate_scene_group(self, primary_only: bool = False, exclude=None,
                              swap: PrimarySwap | None = None) -> list[dict]:
        """Demoscene groups with track counts — a browse facet over the Demozoo
        ``scene_group`` enrichment (a track credited to "Fairlight • Maniacs of
        Noise" counts under both).  Mirrors ``aggregate_genres``: ``primary_only``
        excludes hidden duplicates (web library opts in, Subsonic keeps raw),
        cached under a distinct ``:primary`` key."""
        hide_dups = primary_only
        if not hide_dups:
            swap = None
        cache_key = "scene_group:primary" if hide_dups else "scene_group"
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, tids in self._tag_scene_group.items():
            if not tids:
                continue
            count = self._bucket_count(tids, hide_dups, exclude, swap)
            if count is None:
                count = self._count_primaries(tids) if hide_dups else len(tids)
            if count == 0:
                continue
            # Proper-cased display name from one member's scene_group string.
            name = key
            t = self._tracks.get(next(iter(tids)))
            if t:
                for g in (t.get("scene_group") or "").split(_SCENE_GROUP_SEP):
                    if g.strip().lower() == key:
                        name = g.strip()
                        break
            results.append({"scene_group": name, "count": count})
        results.sort(key=lambda x: x["scene_group"].lower())
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    def aggregate_formats(self, primary_only: bool = False, exclude=None,
                          swap: PrimarySwap | None = None) -> list[dict]:
        """Return ``[{format, count, family}]`` from the format tag index.

        ``family`` is the coarse browse-by-family bucket (trackers / chiptune /
        lossless / lossy / other) via :func:`soniqboom.core.retro.coarse_family`,
        derived from the format name so the Galaxy family filter needs no extra
        per-track work.

        Drives the library "Galaxy" visualization (per-format star
        clusters).  Counts come straight from ``_tag_format`` bucket
        sizes — O(number of distinct formats), no track scan.

        With ``primary_only`` the count MUST exclude non-primary duplicates so
        it agrees with the Galaxy drill-down (``/api/tracks?format=X`` →
        ``filter_tracks(filter_duplicates=True)``, which only returns
        ``is_duplicate_primary`` tracks).  Otherwise a format whose members are
        ALL non-primary duplicates shows a non-zero count in the legend but
        returns 0 tracks when clicked — the "ghost format" bug (e.g. Jam
        Cracker · 3 → 0 live tracks).  In that mode we walk the bucket and
        count primaries, dropping any format with 0.  ``primary_only`` is
        caller-controlled (the Galaxy layer maps it from the
        ``filter_duplicates`` config); no non-library consumer reads this.
        """
        from soniqboom.core.retro import coarse_family
        hide_dups = primary_only
        if not hide_dups:
            swap = None
        # Key on the dup-filter state so a runtime toggle can't serve a list
        # computed under the other mode (the cache is also seq-invalidated).
        cache_key = "formats:primary" if hide_dups else "formats"
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached
        from soniqboom.core.catalogue import is_package_format
        results: list[dict] = []
        for key, tids in self._tag_format.items():
            if not key or not tids or is_package_format(key):
                continue        # (an unopened package: no music format yet)
            touched = self._bucket_count(tids, hide_dups, exclude, swap)
            if touched is not None:
                # Some of the format's tracks are hidden (their drive is
                # offline) or stand in for a primary that can't play: count
                # what the drill-down lists.
                count = touched
                if count == 0:
                    continue
                shown = tids.difference(exclude) if exclude else tids
                t = self._tracks.get(next(iter(shown))) if shown else None
                name = (t.get("format") if t else None) or key
            elif hide_dups:
                # Count only primary tracks; a format that survives ONLY as
                # non-primary duplicates is dropped (matches the drill-down).
                # The display name is the format of the bucket's LAST member
                # (in the set's order) — the one a walk over the members ends
                # on; that member is found in C.  (A last member without a
                # format, not in the library, falls back to the walk.)
                t = self._tracks.get(deque(tids, maxlen=1)[0])
                if t is not None and t.get("format"):
                    name = t.get("format")
                    count = len(tids) - len(tids.intersection(self._nonprimary_ids))
                else:
                    count = 0
                    name = key
                    for tid in tids:
                        t = self._tracks.get(tid)
                        if t is None:
                            continue
                        name = t.get("format") or name
                        if t.get("is_duplicate_primary", True):
                            count += 1
                if count == 0:
                    continue
            else:
                # Resolve a display-cased name from one member track.
                tid = next(iter(tids))
                t = self._tracks.get(tid)
                name = (t.get("format") if t else None) or key
                count = len(tids)
            # `family` = coarse browse-by-family bucket for the Galaxy filter
            # (trackers/chiptune/lossless/lossy/other), derived from the name.
            results.append({"format": name, "count": count, "family": coarse_family(name)})
        # Count desc, then name asc as a stable tiebreaker so equal-count formats
        # keep a deterministic order across rebuilds (dict insertion order can
        # differ between scans) — otherwise the Galaxy cluster layout, which is
        # index-positional, would reshuffle equal-count formats each rebuild.
        results.sort(key=lambda x: (-x["count"], x["format"].lower()))
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    def aggregate_years(self, primary_only: bool = False, exclude=None,
                        swap: PrimarySwap | None = None) -> list[dict]:
        """Years with track counts.  With ``primary_only`` the count excludes
        non-primary duplicates so the legend agrees with the deduped drill-down
        (``/search/filter?year_min=Y&year_max=Y``).

        With duplicates hidden, an ``exclude`` set or a ``swap`` each year's
        bucket of the maintained ``_tag_year`` (keyed by ``_agg_year`` exactly
        as ``_sorted_year``, which backs the drill-down, is) less what the list
        hides (``_invisible``) — set arithmetic, so the counts line up with the
        drill-down.  Any year left with no shown track simply never appears (no
        "ghost" year).  Caller-controlled like the sibling browse aggregates."""
        hide_dups = primary_only
        if not hide_dups:
            swap = None
        cache_key = "years:primary" if hide_dups else "years"
        cached = (self._agg_x_get(cache_key, exclude, swap) if exclude or swap is not None
                  else self._agg_cache_get(cache_key))
        if cached is not None:
            return cached
        # Counter stores raw year values; normalize YYYYMMDD → YYYY.  The
        # previous code had two branches that performed the same division —
        # the first was unreachable because the second matched first for any
        # 5-or-more-digit value.
        merged: dict[int, int] = {}
        if swap is not None or exclude or hide_dups:
            # Copies stand in for primaries that can't play, some tracks are
            # hidden (their drive is offline) or duplicate copies are: each
            # year's ids less the ones the list leaves out.
            inv = self._invisible(hide_dups, exclude, swap)
            for y, ids in self._tag_year.items():
                n = len(ids) - len(ids.intersection(inv)) if inv else len(ids)
                if n:
                    merged[y] = n
        else:
            for y, count in self._agg_years.items():
                if isinstance(y, int) and y > 9999:
                    y = y // 10000
                merged[y] = merged.get(y, 0) + count
        results = sorted(
            [{"year": y, "count": c} for y, c in merged.items()],
            key=lambda x: -x["year"],
        )
        if exclude or swap is not None:
            return self._agg_x_set(cache_key, exclude, results, swap)
        return self._agg_cache_set(cache_key, results)

    # ── Recently added (sorted index) ────────────────────────────────────

    def recently_added(self, limit: int = 100, exclude=None) -> list[dict]:
        """The ``limit`` newest tracks; with ``exclude`` (the tracks the web
        UI hides while their drive is offline) the newest ``limit`` others."""
        if not exclude:
            ids = _sorted_tail(self._sorted_added_at, limit)
            return [self._meta_dict(tid) for tid in ids if tid in self._tracks]
        results: list[dict] = []
        for _, tid in reversed(self._sorted_added_at):
            if tid not in exclude and tid in self._tracks:
                results.append(self._meta_dict(tid))
                if len(results) >= limit:
                    break
        return results

    def list_unplayed(self, limit: int = 100, exclude=None) -> list[dict]:
        """Return tracks never played, newest-first.

        Backed by the incrementally-maintained ``_unplayed_ids`` set — walks
        ``_sorted_added_at`` in reverse, filtering membership.  This is
        O(limit + misses) instead of the previous O(N) scan over every
        track + ``get_all_play_stats`` snapshot in api/smart.py.
        """
        results: list[dict] = []
        unplayed = self._unplayed_ids
        exclude = exclude or ()
        for _, tid in reversed(self._sorted_added_at):
            if tid in unplayed and tid in self._tracks and tid not in exclude:
                results.append(self._meta_dict(tid))
                if len(results) >= limit:
                    break
        return results

    # ── Waveforms ────────────────────────────────────────────────────────

    def get_waveform(self, track_id: str) -> list[float] | None:
        return self._waveforms.get(track_id)

    def waveforms_view(self) -> dict[str, list[float]]:
        """Snapshot of all stored waveforms (shallow copy — safe to read
        off-thread while a scan stores new waveforms concurrently)."""
        return dict(self._waveforms)

    def store_waveform(self, track_id: str, amplitudes: list[float]) -> None:
        self._waveforms[track_id] = amplitudes

    def store_waveforms_batch(self, mapping: dict[str, list[float]]) -> None:
        self._waveforms.update(mapping)

    def waveform_exists_batch(self, track_ids: list[str]) -> dict[str, bool]:
        return {tid: tid in self._waveforms for tid in track_ids}

    def forget_waveforms(self, track_ids) -> int:
        """Drop the stored waveforms of these tracks (their files changed)."""
        n = 0
        for tid in track_ids or ():
            if self._waveforms.pop(tid, None) is not None:
                n += 1
        return n

    def clear_waveforms(self) -> int:
        n = len(self._waveforms)
        self._waveforms.clear()
        return n

    # ── Ratings ──────────────────────────────────────────────────────────

    def get_rating(self, track_id: str) -> int:
        return self._ratings.get(track_id, 0)

    def set_rating(self, track_id: str, rating: int) -> None:
        if rating <= 0:
            self._ratings.pop(track_id, None)
        else:
            self._ratings[track_id] = rating
        self._rating_seq += 1
        self._aof("set_rating", id=track_id, rating=rating)

    def remap_track_ids(self, mapping: dict[str, str]) -> dict:
        """Move user data from old track ids to new ones (a moved / renamed
        file: ``core/id_remap.remap_user_data`` — ratings, play stats,
        history, playlist entries, waveforms).  One AOF record (op
        ``remap_track_ids``).  Returns the per-container counts."""
        mapping = {o: n for o, n in (mapping or {}).items()
                   if isinstance(o, str) and isinstance(n, str) and o and n and o != n}
        if not mapping:
            return {}
        from soniqboom.core.id_remap import remap_user_data
        counts = remap_user_data(mapping, ratings=self._ratings, play_stats=self._play_stats,
                                 history=self._history, playlists=self._playlists,
                                 waveforms=self._waveforms)
        for tid in {*mapping, *mapping.values()}:
            if tid in self._tracks and tid not in self._play_stats:
                self._unplayed_ids.add(tid)
            else:
                self._unplayed_ids.discard(tid)
        if counts.get("ratings"):
            self._rating_seq += 1
        if counts.get("play_stats"):
            self._play_seq += 1
        self._aof("remap_track_ids", mapping=mapping)
        return counts

    def get_ratings_batch(self, track_ids: list[str]) -> dict[str, int]:
        return {tid: self._ratings[tid] for tid in track_ids if tid in self._ratings}

    def get_all_ratings(self) -> dict[str, int]:
        return dict(self._ratings)

    # ── Play stats ───────────────────────────────────────────────────────

    def record_play(self, track_id: str, at: int | None = None) -> dict:
        """Count one play.  ``at`` (epoch seconds, default now) is when it
        happened — an offline client's submitted play, say; it is clamped to
        ``[0, now]`` and never moves ``last_played`` backwards."""
        now = int(time.time())
        ts = now
        if at is not None:
            try:
                ts = max(0, min(int(at), now))
            except (TypeError, ValueError):
                ts = now
        stats = self._play_stats.get(track_id)
        if stats:
            stats["count"] = stats.get("count", 0) + 1
            stats["last_played"] = max(stats.get("last_played", 0) or 0, ts)
        else:
            stats = {"count": 1, "last_played": ts}
            self._play_stats[track_id] = stats
            # First play — track is no longer unplayed.  Discard rather than
            # remove() so the set stays consistent if a play event arrives
            # before the corresponding track upsert (rare AOF replay reorder).
            self._unplayed_ids.discard(track_id)
        self._play_seq += 1
        self._aof("record_play", id=track_id, ts=ts)
        return dict(stats)

    def get_play_stats(self, track_id: str) -> dict:
        return dict(self._play_stats.get(track_id, {"count": 0}))

    def get_play_stats_batch(self, track_ids: list[str]) -> dict[str, dict]:
        return {tid: dict(self._play_stats[tid]) for tid in track_ids if tid in self._play_stats}

    def get_all_play_stats(self) -> dict[str, dict]:
        return {tid: dict(s) for tid, s in self._play_stats.items()}

    # ── Playlists ────────────────────────────────────────────────────────

    def create_playlist(
        self,
        playlist_id: str,
        name: str,
        track_ids: list[str] | None = None,
        owner_user_id: str | None = None,
        query: str | None = None,
    ) -> dict:
        now = int(time.time())
        # The API layer reads/writes ``track_ids`` consistently — historically
        # this code wrote ``tracks`` instead, so freshly-created playlists
        # appeared empty to every reader.  Use the same key everywhere.
        pl = {
            "id": playlist_id,
            "name": name,
            "track_ids": list(track_ids or []),
            "owner_user_id": owner_user_id,  # None ⇒ legacy/shared
            "query": query,                  # non-None ⇒ smart (auto-updating) playlist
            "created_at": now,
            "updated_at": now,
        }
        self._playlists[playlist_id] = pl
        self._aof("upsert_playlist", id=playlist_id, data=pl)
        return dict(pl)

    def list_playlists_for_user(self, user_id: str | None) -> list[dict]:
        """Return playlists visible to ``user_id``.  Visibility rule:
          * owner_user_id == user_id  → always visible (their own)
          * owner_user_id is None     → legacy/shared, visible to all
          * otherwise                 → hidden
        Pass ``user_id=None`` to get every playlist (admin view).
        """
        out = []
        for pl in self._playlists.values():
            pl = self._migrate_playlist(pl)
            owner = pl.get("owner_user_id")
            if user_id is None or owner is None or owner == user_id:
                out.append(dict(pl))
        return out

    def _migrate_playlist(self, pl: dict) -> dict:
        # Legacy playlists persisted with ``tracks`` instead of ``track_ids``.
        # Normalise on read so the API sees the canonical key without forcing
        # a full snapshot rewrite.
        if "track_ids" not in pl and "tracks" in pl:
            pl["track_ids"] = pl.pop("tracks")
        return pl

    def get_playlist(self, playlist_id: str) -> dict | None:
        pl = self._playlists.get(playlist_id)
        if not pl:
            return None
        return dict(self._migrate_playlist(pl))

    def list_playlists(self) -> list[dict]:
        return [dict(self._migrate_playlist(pl)) for pl in self._playlists.values()]

    def update_playlist(
        self, playlist_id: str, updates: dict, *, keep_ids=None,
    ) -> dict | None:
        """Apply ``updates`` to a playlist.

        When ``updates`` contains ``track_ids`` (or the legacy ``tracks``
        alias), any ids that don't exist in the track store are silently
        dropped and recorded under ``dropped_ids`` on the returned dict —
        a stale client clinging to deleted tracks no longer leaves orphan
        ids inside the playlist after a save.  Callers that want strict
        validation can inspect ``dropped_ids`` and 400 the response.

        Stored entries the caller cannot see stay in the list, near their
        old neighbours: ids in ``keep_ids`` (tracks hidden from that client)
        and catalogue tracks that are out of the library for now
        (``PARKED_PLAYLIST_IDS`` — the catalogue switched off, a narrower
        scope).
        """
        pl = self._playlists.get(playlist_id)
        if not pl:
            return None
        # Normalise legacy "tracks" → "track_ids" *before* applying the
        # update; otherwise PUT /playlists/{id} would leave both keys behind
        # and ``_migrate_playlist`` (guarded on "track_ids" not in pl) would
        # never run again on this playlist.
        self._migrate_playlist(pl)
        # Also strip a legacy "tracks" key from inbound updates: callers
        # should only send "track_ids", but accepting "tracks" here would
        # reintroduce the duplicate key that the migration just cleared.
        cleaned = {k: v for k, v in updates.items() if k != "tracks"}
        if "tracks" in updates and "track_ids" not in cleaned:
            cleaned["track_ids"] = updates["tracks"]
        # Prune unknown track ids from the inbound list so a playlist
        # update can't silently pin references to deleted tracks.
        dropped: list = []
        if "track_ids" in cleaned and isinstance(cleaned["track_ids"], list):
            kept: list = []
            for entry in cleaned["track_ids"]:
                # An entry is a bare id string OR {id, subsong} (subsong picker).
                tid = entry.get("id") if isinstance(entry, dict) else entry
                if tid in self._tracks:
                    kept.append(entry)        # keep the original entry — preserves subsong
                else:
                    dropped.append(tid)
            keep = set(keep_ids or ())
            parked = self.get_config(PARKED_PLAYLIST_IDS) or ()
            if parked:
                keep.update(t for t in parked if t not in self._tracks)
            if keep:
                kept = _merge_unseen_entries(pl.get("track_ids") or [], kept, keep)
                if dropped:
                    back = {_entry_tid(e) for e in kept}
                    dropped = [t for t in dropped if t not in back]
            cleaned["track_ids"] = kept
        pl.update(cleaned)
        pl["updated_at"] = int(time.time())
        self._aof("upsert_playlist", id=playlist_id, data=dict(pl))
        out = dict(pl)
        if dropped:
            out["dropped_ids"] = dropped
        return out

    def delete_playlist(self, playlist_id: str) -> bool:
        if self._playlists.pop(playlist_id, None) is not None:
            self._aof("delete_playlist", id=playlist_id)
            return True
        return False

    # ── History ──────────────────────────────────────────────────────────

    def push_history(self, entry: dict) -> None:
        # Kept in PLAY order, not arrival order: an offline Subsonic client's
        # back-dated scrobbles land where they were played (same rule on AOF
        # replay — merger._apply_entry).
        h = self._history
        ts = entry.get("ts") or 0
        if h and ts < (h[-1].get("ts") or 0):
            import bisect
            i = bisect.bisect_right([e.get("ts") or 0 for e in h], ts)
            h.insert(i, entry)
        else:
            h.append(entry)
        if len(self._history) > self.history_max:
            # In-place slice delete — avoids the previous full-list
            # reallocation that fired on every recorded play.
            del self._history[: -self.history_max]
        self._aof("push_history", data=entry)

    def get_history(self, limit: int = 50) -> list[dict]:
        return list(reversed(self._history[-limit:]))

    # ── Scan dirs ────────────────────────────────────────────────────────

    def upsert_scan_dir(self, path: str, track_count_val: int | None = None,
                        network_share_id: str | None = None,
                        status: str = "ok") -> dict:
        existing = self._scan_dirs.get(path, {})
        now = int(time.time())
        # Cache the path_hash once at upsert time so ``list_scan_dirs`` no
        # longer recomputes ``hashlib.sha256(path).hexdigest()[:16]`` for
        # every dir on every request (admin UI polls this endpoint).
        ph = existing.get("path_hash") or hashlib.sha256(path.encode()).hexdigest()[:16]
        sd = {
            "path": path,
            # Preserve the existing count when no explicit value is given
            # (e.g. at scan start before the final count is known).
            "track_count": track_count_val if track_count_val is not None
                           else existing.get("track_count", 0),
            "added_at": existing.get("added_at", now),
            "last_scanned": now,
            "network_share_id": network_share_id or existing.get("network_share_id"),
            "status": status,
            "path_hash": ph,
        }
        self._scan_dirs[path] = sd
        self._aof("upsert_scan_dir", path=path, data=sd)
        if not existing or existing.get("status") != status:
            self._scan_dirs_changed()
        return dict(sd)

    def list_scan_dirs(self) -> list[dict]:
        result = []
        for sd in self._scan_dirs.values():
            d = dict(sd)
            # Always compute the live track count from the tag index
            # instead of relying on the cached field (which can be stale
            # if a scan was interrupted or the old code reset it to 0).
            path = d.get("path", "")
            # ``upsert_scan_dir`` writes ``path_hash`` at insert time; for
            # legacy entries persisted before this cache existed, fall back
            # to a one-shot recompute and stash it for next call.
            h = d.get("path_hash")
            if not h:
                h = hashlib.sha256(path.encode()).hexdigest()[:16]
                sd["path_hash"] = h
                d["path_hash"] = h
            d["track_count"] = len(self._tag_scan_root_hash.get(h, set()))
            key = _catalogue.key_of(path)
            if key:
                # A catalogue root (core/catalogue.py): managed under Admin →
                # External sources, never scanned.
                d["catalogue"] = key
                d["label"] = _catalogue.label_of(path)
            result.append(d)
        return result

    def delete_scan_dir(self, path: str) -> bool:
        if self._scan_dirs.pop(path, None) is not None:
            self._aof("delete_scan_dir", path=path)
            self._scan_dirs_changed()
            return True
        return False

    def _scan_dirs_changed(self) -> None:
        """A scan dir came, went, or changed its reachability ``status``:
        tell the listener (``core.availability`` — which tracks can play)."""
        cb = self._on_scan_dirs_changed
        if cb is not None:
            try:
                cb()
            except Exception:                     # noqa: BLE001 — never fail a write
                log.debug("scan-dir listener failed", exc_info=True)

    def set_scan_dir_status(self, path: str, status: str) -> bool:
        """Update ONLY the reachability status of a scan dir, and only when it
        actually changed (so an availability probe that finds nothing moved
        writes no AOF entry).  Does NOT touch ``last_scanned`` — a liveness
        check is not a scan.  Returns True iff the status changed."""
        sd = self._scan_dirs.get(path)
        if sd is None or sd.get("status") == status:
            return False
        sd["status"] = status
        self._aof("upsert_scan_dir", path=path, data=dict(sd))
        self._scan_dirs_changed()
        return True

    # ── Hash lookups ─────────────────────────────────────────────────────

    def store_hash_lookup(self, value: str) -> str:
        h = hashlib.sha256(value.encode()).hexdigest()[:16]
        self._hash_lookups[h] = value
        return h

    def store_hash_lookups_batch(self, values: list[str]) -> dict[str, str]:
        result = {}
        for v in values:
            h = hashlib.sha256(v.encode()).hexdigest()[:16]
            self._hash_lookups[h] = v
            result[v] = h
        return result

    def resolve_hash(self, h: str) -> str | None:
        return self._hash_lookups.get(h)

    def list_hash_lookups(self) -> dict[str, str]:
        return dict(self._hash_lookups)

    # ── Config ───────────────────────────────────────────────────────────

    def set_config(self, key: str, value: Any) -> None:
        self._config[key] = value
        self._aof("set_config", key=key, value=value)

    def delete_config(self, key: str) -> None:
        """Remove config ``key`` (``set_config(key, None)`` keeps the key)."""
        if key in self._config:
            del self._config[key]
            self._aof("delete_config", key=key)

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    # ── Art absent tracking ──────────────────────────────────────────────

    # Soft cap on the "art known to be absent" set so it can't grow
    # unboundedly across a long-running session (Perf #1 flagged a
    # 170K-entry set on a fully-browsed library ≈ 9 MB).
    _ART_ABSENT_CAP = 20_000

    def mark_art_absent(self, track_id: str) -> None:
        if len(self._art_absent) >= self._ART_ABSENT_CAP:
            # Drop ~5% — pop_random would be O(1) but unstable; pop a few
            # arbitrary elements via ``pop()`` until under cap.
            drop_n = max(1, self._ART_ABSENT_CAP // 20)
            for _ in range(drop_n):
                try:
                    self._art_absent.pop()
                except KeyError:
                    break
        self._art_absent.add(track_id)

    def is_art_absent(self, track_id: str) -> bool:
        return track_id in self._art_absent

    def discard_art_absent(self, track_id: str) -> None:
        """Drop one track's in-memory 'no art' hint — used when its source
        changed (re-tag / a folder image appeared) so the negative cache must
        re-evaluate that track. Cheap set-discard; no-op if not present."""
        self._art_absent.discard(track_id)

    def clear_art_absent(self) -> None:
        self._art_absent.clear()

    # ── Serialisation (for persistence snapshot) ─────────────────────────

    def to_snapshot(self) -> dict:
        """Serialise entire store state to a dict for JSON persistence."""
        return {
            "tracks": self._tracks,
            "waveforms": self._waveforms,
            "ratings": self._ratings,
            "play_stats": self._play_stats,
            "playlists": self._playlists,
            "history": self._history,
            "scan_dirs": self._scan_dirs,
            "hash_lookups": self._hash_lookups,
            "config": self._config,
        }


# ── Module-level singleton ───────────────────────────────────────────────────

_store: TrackStore | None = None


def get_store() -> TrackStore:
    global _store
    if _store is None:
        _store = TrackStore()
    return _store
