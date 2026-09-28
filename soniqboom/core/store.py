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
import logging
import math
import re
import time
import unicodedata
from collections import Counter
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

# Word-index tokens (``_tokenize_track``).
_TOKEN_FIELDS = frozenset(("title", "artist", "album_artist", "album", "composer",
                           "scene_group", "game", "game_aliases"))
# Scalar tag indexes: field → attribute (``_tag_set(idx, t.get(field, ""))``).
_SCALAR_TAG_INDEXES = (
    ("artist", "_tag_artist"), ("album_artist", "_tag_album_artist"),
    ("album", "_tag_album"), ("format", "_tag_format"),
    ("dir_hash", "_tag_dir_hash"), ("scan_root_hash", "_tag_scan_root_hash"),
)
# ``_agg_*`` counters.
_AGG_FIELDS = frozenset(("artist", "album_artist", "album", "genre", "year"))


def _lex_key(v) -> str:
    return (v or "").strip().lower() or _EMPTY_SORT_KEY


def _game_key(v) -> str:
    """The ``game:`` operator's match key of a title / album / query: runs of
    whitespace collapsed to one space, lower-cased, accents folded like the
    search tokens ("Pokémon" → "pokemon", "real evil      180s" → "real evil
    180s")."""
    s = " ".join((v or "").split()).lower()
    return s if s.isascii() else _fold_text(s)


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


def _added_value(t: dict):
    a = t.get("added_at", 0)
    return a if a else None


def _added_primary_value(t: dict):
    a = t.get("added_at", 0)
    return a if a and t.get("is_duplicate_primary", True) else None


def _sortable_duration(d) -> bool:
    """A duration that belongs in ``_sorted_duration``: non-zero and, when
    numeric, finite (a NaN from a broken probe breaks the list's order)."""
    if not d:
        return False
    try:
        return math.isfinite(d)
    except TypeError:
        return True


def _duration_value(t: dict):
    d = t.get("duration", 0.0)
    return d if _sortable_duration(d) else None


# Sorted list → (input fields, value function).  The value function returns the
# list key for a track, or None when the track is not in that list.
_SORTED_SPECS: dict[str, tuple[frozenset[str], Callable[[dict], Any]]] = {
    "_sorted_year":             (frozenset(("year",)), lambda t: normalise_year(t.get("year"))),
    "_sorted_added_at":         (frozenset(("added_at",)), _added_value),
    "_sorted_added_at_primary": (frozenset(("added_at", "is_duplicate_primary")),
                                 _added_primary_value),
    "_sorted_duration":         (frozenset(("duration",)), _duration_value),
    "_sorted_bpm":              (frozenset(("bpm",)), lambda t: t.get("bpm")),
    "_sorted_title":            (frozenset(("title",)), lambda t: _lex_key(t.get("title"))),
    "_sorted_artist":           (frozenset(("artist",)), lambda t: _lex_key(t.get("artist"))),
    "_sorted_album_artist":     (frozenset(("album_artist",)),
                                 lambda t: _lex_key(t.get("album_artist"))),
    "_sorted_album":            (frozenset(("album",)), lambda t: _lex_key(t.get("album"))),
    "_sorted_format":           (frozenset(("format",)), lambda t: _lex_key(t.get("format"))),
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
                   | frozenset(("duplicate_group_id", "genre", "scene_group", "game"))
                   | frozenset().union(*(f for f, _ in _SORTED_SPECS.values())))

# Fields NO ``_mutation_seq``-keyed consumer reads (store ``_agg_cache``, the
# library legend cache, the Subsonic caches, smart.py's duplicate memo, the
# folder-album pass, the index-health sweep).  An ALLOWLIST, deliberately: a
# change to any other field bumps the seq.  ``cover_art`` is written on the
# first art extraction of every track, so bumping for it re-cooled every
# library-wide cache on normal browsing.
_SEQ_EXEMPT_FIELDS = frozenset(("cover_art", "mtime", "file_size"))

# Inputs of the duplicate grouping (``duplicates.group_key_for`` + the primary
# pick) and its output.  A write that changes an input — or loses the output —
# records the track's PREVIOUS values in ``_dup_dirty`` so the scanner re-groups
# exactly the affected groups (``take_dup_dirty``), whoever made the change: a
# scan, a tag edit, an HVSC / render duration backfill, a Modland artist fill.
_DUP_FIELDS = frozenset(("title", "artist", "album_artist", "duration",
                         "format", "bitrate", "added_at"))
_DUP_OUTPUT_FIELDS = frozenset(("duplicate_group_id", "is_duplicate_primary", "format_score"))
_DUP_DIRTY_MAX = 20_000     # more pending changes than this → one full pass instead

# Fields the Subsonic catalogue (core/subsonic_index.py) groups/sorts/counts on
# — including a multi-tune file's tune count, default tune and per-tune
# lengths, from which it expands the tunes and sums album song counts.
# ``_catalog_seq`` bumps on inserts/deletes and on a change to one of these, so
# the catalogue can key on it instead of the (much hotter) ``_mutation_seq``.
_CATALOG_FIELDS = frozenset(("title", "artist", "album_artist", "album", "genre",
                             "year", "duration", "added_at", "dir_hash", "path",
                             "track_number", "disc_number",
                             "subsongs", "start_subsong", "hvsc_lengths"))

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


def _duration_only(changed: dict) -> bool:
    return "duration" in changed and changed.keys() <= _DURATION_SEQ_FIELDS


# ``_paginate_all``: a sorted page with duplicates hidden at an offset of at
# least this many rows slices the memoised primary-only order
# (``TrackStore._primary_order``) instead of walking and filtering ``offset``
# rows; shallower pages keep the walk (~1.5 ms at this depth), so writes that
# keep invalidating the memo never make a first page pay for building it.
_PRIMARY_ORDER_MIN_OFFSET = 5000

# ``filter_tracks`` sorts a candidate set of up to this many ids (or 1/64 of
# the library, if larger) directly instead of walking the full sorted index:
# measured on a 263K-track library, ``game:uridium`` (8 ids) 28 ms → 0.03 ms.
_SMALL_CANDIDATE_SET = 2000


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
)


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
    for f, keep in (("game_by_folder", True), ("game_by_archive", True),
                    ("game_by_demozoo", same_artist and old.get("title") == new.get("title")),
                    ("game_by_modland", same_md5),
                    ("game_by_modland_filename", same_md5),
                    ("game_by_songdb", same_md5)):
        if keep and old.get(f) and not new.get(f):
            new[f] = old[f]
    new.update(game_follow(new))                # the game follows the carried album


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
        # Duration-only field writes (``_duration_only``), which bump neither
        # sequence above.
        self._duration_seq: int = 0
        # Tracks upserted since the heap was last frozen (``FREEZE_AFTER_UPSERTS``).
        self._upserts_since_freeze: int = 0

        # ── Inverted word index ──────────────────────────────────────────
        self._word_index: dict[str, set[str]] = {}
        self._word_list: list[str] = []
        self._word_list_dirty = False

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
        self._tag_scan_root_hash: dict[str, set[str]] = {}
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
        self._sorted_year: list[tuple[int, str]] = []
        self._sorted_added_at: list[tuple[int, str]] = []
        # Parallel index tracking only primary (non-secondary-dup) tracks so
        # ``filter_tracks(filter_duplicates=True)`` and ``_paginate_all`` can
        # walk a shorter pre-filtered list rather than testing every entry's
        # ``is_duplicate_primary`` flag per request.
        self._sorted_added_at_primary: list[tuple[int, str]] = []
        self._sorted_duration: list[tuple[float, str]] = []
        self._sorted_bpm: list[tuple[float, str]] = []
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

        # ── Pre-computed aggregations ────────────────────────────────────
        self._agg_artists: Counter[str] = Counter()
        self._agg_album_artists: Counter[str] = Counter()
        self._agg_albums: Counter[str] = Counter()
        self._agg_genres: Counter[str] = Counter()
        self._agg_years: Counter[int] = Counter()
        self._agg_albums_by_artist: dict[str, Counter[str]] = {}
        self._agg_albums_by_album_artist: dict[str, Counter[str]] = {}

        # ── Memoised aggregations keyed on _mutation_seq ─────────────────
        # Each entry is ``(seq, key, value)`` so a single ``_mutation_seq``
        # bump invalidates every cached aggregation in lock-step.  Mirrors
        # the (already-existing) duplicate-snapshot cache in api/smart.py.
        self._agg_cache: dict[str, tuple[int, Any]] = {}
        # ``_primary_order`` memo: sorted-index name → (the list it was built
        # from, ``_primary_order_key``, primary-only ids in list order).
        self._primary_order_memo: dict[str, tuple[list, tuple, list[str]]] = {}

        # ── Track IDs that have never been played ────────────────────────
        # Mirrors `play_stats` keys; updated on every track upsert / delete
        # and on `record_play`.  Lets api/smart.py serve the "unplayed"
        # view by walking ``_sorted_added_at`` filtered through this set
        # instead of scanning every track in the library.
        self._unplayed_ids: set[str] = set()

        # ── AOF hook (set by aof module after init) ──────────────────────
        self._aof_append: Callable[..., None] | None = None

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

    def sorted_rebuild_plan(self) -> dict[str, dict[str, Any] | None]:
        """Snapshot of the pending sorted-list rebuilds: name → ``{track id:
        its pre-batch key (None = was not in the list)}`` to merge, or None
        for a full rebuild."""
        tids = self._dirty_sorted_tids
        return {n: (dict(tids[n]) if n in tids else None) for n in self._dirty_sorted}

    def _sorted_rebuilt(self, names) -> None:
        """Mark ``names`` rebuilt (their pending state is cleared) and drop
        their ``_primary_order`` memo entries, which hold the replaced lists."""
        for n in names:
            self._dirty_sorted.discard(n)
            self._dirty_sorted_tids.pop(n, None)
            self._primary_order_memo.pop(n, None)

    # ── AOF helper ───────────────────────────────────────────────────────

    def _aof(self, op: str, **kwargs: Any) -> None:
        if self._aof_append:
            self._aof_append(op, **kwargs)

    # ── Index maintenance ────────────────────────────────────────────────

    def _index_track(self, tid: str, t: dict) -> None:
        """Add a track to all indexes."""
        for token in _tokenize_track(t):
            self._word_index.setdefault(token, set()).add(tid)
        self._word_list_dirty = True

        self._tag_set(self._tag_artist, t.get("artist", ""), tid)
        self._tag_set(self._tag_album_artist, t.get("album_artist", ""), tid)
        self._tag_set(self._tag_album, t.get("album", ""), tid)
        self._tag_set(self._tag_format, t.get("format", ""), tid)
        self._tag_set(self._tag_dir_hash, t.get("dir_hash", ""), tid)
        self._tag_set(self._tag_scan_root_hash, t.get("scan_root_hash", ""), tid)
        gid = t.get("duplicate_group_id")
        if gid:
            self._tag_set(self._tag_dup_group, gid, tid)
        for g in t.get("genre", []):
            self._tag_set(self._tag_genre, g, tid)
        for grp in _scene_group_keys(t.get("scene_group")):
            self._tag_set(self._tag_scene_group, grp, tid)
        for k in _game_keys(t):
            self._tag_set(self._tag_game, k, tid)

        # Extract numeric fields used by both sorted indexes and aggregations.
        # ``normalise_year`` collapses YYYYMMDD-form ints down to YYYY so the
        # sorted index agrees with the aggregation Counter — the same rule
        # is reused by scanner._async_exit_batch_mode via the module helper.
        year = normalise_year(t.get("year"))
        added = t.get("added_at", 0)
        dur = t.get("duration", 0.0)
        bpm = t.get("bpm")

        # Lexical sort keys for the column-header sort (windowed view).
        # Lower-cased so ABBA and abba sort adjacent; empty values fall
        # to the end of asc / start of desc — we use the ``￿``
        # sentinel for empties to keep that behaviour without a separate
        # filter pass at query time.  Using a high-BMP unicode codepoint
        # not present in real metadata.
        EMPTY_SORT_KEY = "￿"
        title_key        = (t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY
        artist_key       = (t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY
        album_artist_key = (t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY
        album_key        = (t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY
        fmt_key          = (t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY

        if not self._batch_mode:
            if year is not None:
                _sorted_insert(self._sorted_year, (year, tid))
            if added:
                _sorted_insert(self._sorted_added_at, (added, tid))
                if t.get("is_duplicate_primary", True):
                    _sorted_insert(self._sorted_added_at_primary, (added, tid))
            if _sortable_duration(dur):
                _sorted_insert(self._sorted_duration, (dur, tid))
            if bpm is not None:
                _sorted_insert(self._sorted_bpm, (bpm, tid))
            _sorted_insert(self._sorted_title,        (title_key,        tid))
            _sorted_insert(self._sorted_artist,       (artist_key,       tid))
            _sorted_insert(self._sorted_album_artist, (album_artist_key, tid))
            _sorted_insert(self._sorted_album,        (album_key,        tid))
            _sorted_insert(self._sorted_format,       (fmt_key,          tid))
            title_fold = _game_fold_value(t.get("title"))
            if title_fold is not None:
                _sorted_insert(self._sorted_title_fold, (title_fold, tid))
        elif len(self._dirty_sorted) != _N_SORTED or self._dirty_sorted_tids:
            self._mark_all_sorted_dirty()

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
        artist = (t.get("artist") or "").strip()
        album_artist = (t.get("album_artist") or "").strip()
        album = (t.get("album") or "").strip()
        if artist:
            self._agg_artists[artist.lower()] += 1
            if album:
                self._agg_albums_by_artist.setdefault(artist.lower(), Counter())[album.lower()] += 1
        if album_artist:
            self._agg_album_artists[album_artist.lower()] += 1
            if album:
                self._agg_albums_by_album_artist.setdefault(album_artist.lower(), Counter())[album.lower()] += 1
        if album:
            self._agg_albums[album.lower()] += 1
        for g in t.get("genre", []):
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
        self._word_list_dirty = True

        self._tag_del(self._tag_artist, t.get("artist", ""), tid)
        self._tag_del(self._tag_album_artist, t.get("album_artist", ""), tid)
        self._tag_del(self._tag_album, t.get("album", ""), tid)
        self._tag_del(self._tag_format, t.get("format", ""), tid)
        self._tag_del(self._tag_dir_hash, t.get("dir_hash", ""), tid)
        self._tag_del(self._tag_scan_root_hash, t.get("scan_root_hash", ""), tid)
        gid = t.get("duplicate_group_id")
        if gid:
            self._tag_del(self._tag_dup_group, gid, tid)
        for g in t.get("genre", []):
            self._tag_del(self._tag_genre, g, tid)
        for grp in _scene_group_keys(t.get("scene_group")):
            self._tag_del(self._tag_scene_group, grp, tid)
        for k in _game_keys(t):
            self._tag_del(self._tag_game, k, tid)

        # Extract numeric fields used by both sorted indexes and aggregations.
        # ``normalise_year`` keeps insert/remove in agreement.
        year = normalise_year(t.get("year"))
        added = t.get("added_at", 0)
        dur = t.get("duration", 0.0)
        bpm = t.get("bpm")

        # Same lexical key derivation as ``_index_track`` — must match
        # exactly or the remove turns into a no-op and the index drifts.
        EMPTY_SORT_KEY = "￿"
        title_key        = (t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY
        artist_key       = (t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY
        album_artist_key = (t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY
        album_key        = (t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY
        fmt_key          = (t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY

        if not self._batch_mode:
            if year is not None:
                _sorted_remove(self._sorted_year, (year, tid))
            if added:
                _sorted_remove(self._sorted_added_at, (added, tid))
                if t.get("is_duplicate_primary", True):
                    _sorted_remove(self._sorted_added_at_primary, (added, tid))
            if _sortable_duration(dur):
                _sorted_remove(self._sorted_duration, (dur, tid))
            if bpm is not None:
                _sorted_remove(self._sorted_bpm, (bpm, tid))
            _sorted_remove(self._sorted_title,        (title_key,        tid))
            _sorted_remove(self._sorted_artist,       (artist_key,       tid))
            _sorted_remove(self._sorted_album_artist, (album_artist_key, tid))
            _sorted_remove(self._sorted_album,        (album_key,        tid))
            _sorted_remove(self._sorted_format,       (fmt_key,          tid))
            title_fold = _game_fold_value(t.get("title"))
            if title_fold is not None:
                _sorted_remove(self._sorted_title_fold, (title_fold, tid))
        elif len(self._dirty_sorted) != _N_SORTED or self._dirty_sorted_tids:
            self._mark_all_sorted_dirty()

        # Unplayed bookkeeping — when a track is removed it can't be unplayed
        # anymore; this stops the set leaking entries for deleted tracks.
        self._unplayed_ids.discard(tid)

        self._agg_remove(t, year)

    def _agg_remove(self, t: dict, year) -> None:
        """Inverse of ``_agg_add``."""
        artist = (t.get("artist") or "").strip()
        album_artist = (t.get("album_artist") or "").strip()
        album = (t.get("album") or "").strip()
        if artist:
            self._agg_artists[artist.lower()] -= 1
            if self._agg_artists[artist.lower()] <= 0:
                del self._agg_artists[artist.lower()]
            by_art = self._agg_albums_by_artist.get(artist.lower())
            if by_art and album:
                by_art[album.lower()] -= 1
                if by_art[album.lower()] <= 0:
                    del by_art[album.lower()]
                if not by_art:
                    # An empty per-artist counter is not "no entry" to
                    # verify_indexes (a fresh build never creates one).
                    del self._agg_albums_by_artist[artist.lower()]
        if album_artist:
            self._agg_album_artists[album_artist.lower()] -= 1
            if self._agg_album_artists[album_artist.lower()] <= 0:
                del self._agg_album_artists[album_artist.lower()]
            by_aa = self._agg_albums_by_album_artist.get(album_artist.lower())
            if by_aa and album:
                by_aa[album.lower()] -= 1
                if by_aa[album.lower()] <= 0:
                    del by_aa[album.lower()]
                if not by_aa:
                    del self._agg_albums_by_album_artist[album_artist.lower()]
        if album:
            self._agg_albums[album.lower()] -= 1
            if self._agg_albums[album.lower()] <= 0:
                del self._agg_albums[album.lower()]
        for g in t.get("genre", []):
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

    def _rebuild_word_list(self) -> None:
        if self._word_list_dirty:
            self._word_list = sorted(self._word_index.keys())
            self._word_list_dirty = False

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
                            self._word_list_dirty = True
                for tok in new_tok - old_tok:
                    s = wi.get(tok)
                    if s is None:
                        wi[tok] = {tid}
                        self._word_list_dirty = True
                    else:
                        s.add(tid)

        for field, attr in _SCALAR_TAG_INDEXES:
            if field in changed:
                idx = getattr(self, attr)
                self._tag_del(idx, old.get(field, ""), tid)
                self._tag_set(idx, t.get(field, ""), tid)
        if "duplicate_group_id" in changed:
            if old.get("duplicate_group_id"):
                self._tag_del(self._tag_dup_group, old["duplicate_group_id"], tid)
            if t.get("duplicate_group_id"):
                self._tag_set(self._tag_dup_group, t["duplicate_group_id"], tid)
        if "genre" in changed:
            for g in old.get("genre", []):
                self._tag_del(self._tag_genre, g, tid)
            for g in t.get("genre", []):
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
            self._agg_remove(old, normalise_year(old.get("year")))
            self._agg_add(t, normalise_year(t.get("year")))

    def _apply_field_changes(self, tid: str, t: dict, changed: dict) -> None:
        """Write the (already diffed, non-empty) ``changed`` fields onto
        stored track ``t`` and maintain the derived indexes."""
        if not _DUP_FIELDS.isdisjoint(changed):
            self._note_dup_dirty(tid, t)
        if _INDEXED_FIELDS.isdisjoint(changed):
            t.update(changed)                   # e.g. cover_art, file_md5, defect
            return
        old = dict(t)
        t.update(changed)
        self._reindex_changed(tid, old, t, changed)

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
        self._tracks = tracks
        self._waveforms = waveforms
        self._ratings = ratings
        self._play_stats = play_stats
        self._playlists = playlists
        self._history = history
        self._scan_dirs = scan_dirs
        self._hash_lookups = hash_lookups
        self._config = config

    def rebuild_indexes(self) -> None:
        """Rebuild all indexes from current data.

        Runs in batch mode so the 9 sorted indexes are built by one O(N log N)
        ``sort()`` at the end rather than N × ``bisect.insort`` (which is
        O(N) per call thanks to the memmove, so the per-track loop ends up
        O(N²) wall-clock and goes from ~17 s baseline to >70 s once we
        started maintaining the 5 lexical indexes too).
        """
        self.clear_indexes()
        self.enter_batch_mode()
        try:
            for tid, t in self._tracks.items():
                self._index_track(tid, t)
        finally:
            self.exit_batch_mode()
        self._rebuild_word_list()
        self._build_sim_token_index()
        # ``_index_track`` adds every track to ``_unplayed_ids`` whose key
        # isn't already in ``_play_stats`` — the order above guarantees that
        # snapshot-loaded play stats correctly suppress those tids.
        log.info("Indexes rebuilt for %d tracks", len(self._tracks))

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
    #   _agg_cache  — per-query aggregation MEMO (key -> (mutation_seq, value)):
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
        self._primary_order_memo.clear()        # built from the lists emptied below
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
        every ``chunk`` tracks so an async caller can give the loop a turn.
        Iterates a snapshot; binds the result only at the end."""
        from soniqboom.core.retro import is_retro_format, instrument_tokens
        postings: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        items = list(self._tracks.items())
        for i in range(0, len(items), chunk):
            for tid, t in items[i : i + chunk]:
                if not is_retro_format(t.get("format")):
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
        postings = {tok: ids for tok, ids in postings.items() if len(ids) > 1}
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

    def upsert_track(self, track: dict) -> None:
        tid = track["id"]
        old = self._tracks.get(tid)
        track.update(game_follow(track))
        self._note_dup_upsert(tid, old, track)
        if old:
            self._unindex_track(tid, old)
        self._tracks[tid] = track
        self._index_track(tid, track)
        self._mutation_seq += 1
        self._catalog_seq += 1
        self._upserts_since_freeze += 1
        self._aof("upsert_track", id=tid, data=track)

    def upsert_tracks_batch(self, tracks: list[dict]) -> int:
        for t in tracks:
            tid = t["id"]
            old = self._tracks.get(tid)
            if old:
                _carry_enrichment(old, t)     # keep post-scan enrichment (+ the game)
                self._unindex_track(tid, old)
            else:
                t.update(game_follow(t))
            self._note_dup_upsert(tid, old, t)
            self._tracks[tid] = t
            self._index_track(tid, t)
        self._mutation_seq += 1
        self._catalog_seq += 1
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
        """Exit batch mode; on the OUTERMOST exit only, rebuild the sorted
        indexes marked dirty (``_dirty_sorted``) — all of them after an
        insert/delete, only the re-keyed ones after field updates."""
        self._batch_depth -= 1
        if self._batch_depth > 0:
            return                       # a concurrent batch section is still open
        self._batch_depth = 0
        self._batch_mode = False
        if self._dirty_sorted:
            plan = self.sorted_rebuild_plan()
            if all(v is None for v in plan.values()):
                self._rebuild_sorted_indexes(set(plan))
            else:
                for name, tids in plan.items():
                    lst = self.build_sorted_list(name, tids)
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
            y = normalise_year(t.get("year"))
            if y is not None:
                year.append((y, tid))
            a = t.get("added_at", 0)
            if a:
                added.append((a, tid))
                if t.get("is_duplicate_primary", True):
                    added_primary.append((a, tid))
            d = t.get("duration", 0.0)
            if _sortable_duration(d):
                dur.append((d, tid))
            b = t.get("bpm")
            if b is not None:
                bpm.append((b, tid))
            # Lexical keys mirror ``_index_track`` exactly — same
            # ``.strip().lower()`` + EMPTY sentinel — so per-track
            # incremental insert/remove and the full rebuild produce
            # byte-identical index contents.
            title.append(         ((t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
            artist_s.append(      ((t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY, tid))
            album_artist_s.append(((t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY, tid))
            album_s.append(       ((t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
            fmt.append(           ((t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY, tid))
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
        t = self._tracks.pop(track_id, None)
        if t is None:
            return False
        self._note_dup_dirty(track_id, t)
        self._unindex_track(track_id, t)
        self._waveforms.pop(track_id, None)
        self._mutation_seq += 1
        self._catalog_seq += 1
        self._aof("delete_tracks", ids=[track_id])
        return True

    def delete_track_ids(self, track_ids: list[str]) -> int:
        deleted = 0
        ids = []
        for tid in track_ids:
            t = self._tracks.pop(tid, None)
            if t:
                self._note_dup_dirty(tid, t)
                self._unindex_track(tid, t)
                self._waveforms.pop(tid, None)
                deleted += 1
                ids.append(tid)
        if ids:
            self._mutation_seq += 1
            self._catalog_seq += 1
            self._aof("delete_tracks", ids=ids)
        return deleted

    def track_count(self) -> int:
        return len(self._tracks)

    # ── Duplicate-relevant change tracking ───────────────────────────────

    def _note_dup_dirty(self, tid: str, old: "dict | None") -> None:
        """Record ``tid``'s duplicate-relevant values BEFORE a change (None =
        a new track).  The first record per track wins until it is taken."""
        if self._dup_dirty_overflow or tid in self._dup_dirty:
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
        (``_duration_only``), which bumps ``_duration_seq`` instead."""
        t = self._tracks.get(track_id)
        if not t:
            return False
        changed = _with_game(t, _diff_updates(t, updates))
        if not changed:
            return True
        self._apply_field_changes(track_id, t, changed)
        if _duration_only(changed):
            self._duration_seq += 1
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
        bump = catalog = duration = False
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
            if _duration_only(changed):
                duration = True
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
        limit: int = 200,
        offset: int = 0,
        filter_duplicates: bool = False,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> list[dict]:
        """Filter tracks by tag and/or range criteria, intersecting results.

        ``sort_by`` selects which pre-computed sorted index drives the page
        walk: one of ``title``, ``artist``, ``album``, ``format``, ``year``,
        ``duration``, ``bpm``, or ``added`` (default).  ``sort_order`` is
        ``"asc"`` or ``"desc"`` — falsy / unknown values default to ``desc``
        for ``added`` (newest first, historical) and ``asc`` for everything
        else (the natural reading order for a column header click).
        """
        result = self._candidate_ids(
            artist=artist, album_artist=album_artist, album=album, genre=genre,
            scene_group=scene_group, format_=format_,
            year_min=year_min, year_max=year_max,
            dir_hash=dir_hash, scan_root_hash=scan_root_hash, query=query,
            game=game, filter_duplicates=filter_duplicates,
        )
        if result is None:
            return self._paginate_all(
                limit, offset,
                filter_duplicates=filter_duplicates,
                sort_by=sort_by, sort_order=sort_order,
            )
        if not result:
            return []

        attr = self._sort_index_attr(sort_by, filter_duplicates=filter_duplicates)
        idx = getattr(self, attr)
        descending = self._is_descending(sort_by, sort_order)
        if len(result) <= max(_SMALL_CANDIDATE_SET, len(idx) // 64):
            # A small candidate set (a ``game:`` / specific search) is sorted
            # directly — O(k log k) — instead of walking the whole library's
            # sorted index for it.  Same (value, id) keys and membership rule
            # as the index, so the page is identical.
            page = self._sort_candidates(result, attr, descending)
            if page is not None:
                return [self._meta_dict(tid) for tid in page[offset:offset + limit]]

        # Walk the chosen sorted index in the requested direction keeping only
        # tids that are in the candidate set.  ``descending`` walk = reversed
        # iterator over the ascending index — Python's reversed() over a list
        # is O(1) setup + O(k) for k items consumed.
        walk_iter = reversed(idx) if descending else iter(idx)

        need = offset + limit
        collected: list[str] = []
        for _, tid in walk_iter:
            if tid in result:
                collected.append(tid)
                if len(collected) >= need:
                    break
        page = collected[offset : offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    def _sort_candidates(self, ids, attr: str, descending: bool) -> list[str] | None:
        """``ids`` in the order of sorted index ``attr`` (``descending`` = the
        reversed index), built from the same ``_SORTED_SPECS`` value function:
        ids the index leaves out (no value) are left out, and ties break on the
        id in the same direction as the index walk.
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
        filter_duplicates: bool = False,
    ) -> set[str] | None:
        """THE predicate → candidate-id-set resolver (single source of truth).

        Shared by :meth:`filter_tracks` (sorted pages), :meth:`filter_track_ids`
        (sampling) and the shuffled play order, so a list view and a shuffle over
        "the same filter" resolve the predicates identically.  (Whether duplicate
        copies are hidden is the CALLER's choice via ``filter_duplicates`` — the
        web views pass the setting, Subsonic/DLNA deliberately do not.)

        Returns ``None`` when NO predicate was given (= every track; callers take
        their all-tracks fast path), otherwise the intersected set — already
        reduced to duplicate-group primaries when ``filter_duplicates`` is set.

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
        if untagged and untagged not in self._UNTAGGED_FIELDS:
            # An unknown field must not quietly mean "no predicate" (= everything).
            raise ValueError(f"untagged must be one of {self._UNTAGGED_FIELDS}, got {untagged!r}")
        if untagged:
            # Tracks whose tag is missing/blank — the tag indexes can't express
            # "empty", so this is one O(N) pass (callers cache the result).
            def _blank(v) -> bool:
                if isinstance(v, (list, tuple)):
                    return not any(str(x).strip() for x in v)
                return not (str(v).strip() if v is not None else "")
            sets.append({tid for tid, t in self._tracks.items()
                         if _blank(t.get(untagged))})

        if not sets:
            return None

        result = sets[0]
        for s in sets[1:]:
            result = result & s
            if not result:
                return set()

        if filter_duplicates:
            result = {tid for tid in result
                      if self._tracks.get(tid, {}).get("is_duplicate_primary", True)}
        return result

    def filter_track_ids(self, *, filter_duplicates: bool = False,
                         **predicates) -> list[str]:
        """Eligible track ids for the given filters — without sorting or
        paginating.  For callers that need the candidate SET to sample or order
        (``getRandomSongs``, the shuffled play order) rather than a sorted page.

        Accepts every :meth:`_candidate_ids` predicate, so its semantics are
        identical to :meth:`filter_tracks` by construction.  With no predicate it
        returns every id — or every duplicate-group PRIMARY when
        ``filter_duplicates`` is set, matching what the deduped All-Tracks list
        shows."""
        result = self._candidate_ids(filter_duplicates=filter_duplicates, **predicates)
        if result is not None:
            return list(result)
        if filter_duplicates:
            # The same test ``_candidate_ids`` and ``primary_track_count`` use.  NOT
            # ``_sorted_added_at_primary``: that is a sort index and leaves out
            # tracks with no ``added_at``, so it cannot stand in for the set.
            # Memoised on ``_mutation_seq`` like the aggregates: this O(N) pass runs
            # on the event loop for every cold "shuffle all" (25 ms at 263 K).
            cached = self._agg_cache_get("primary_track_ids")
            if cached is None:
                cached = [tid for tid, t in self._tracks.items()
                          if t.get("is_duplicate_primary", True)]
                self._agg_cache_set("primary_track_ids", cached)
            return list(cached)
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
        """Resolve a text query to a set of matching track IDs.

        Query tokens are accent-folded: every non-ASCII word is indexed under
        its folded form too, so "beyonce" and "beyoncé" both find "Beyoncé"
        and "Beyonce", and "öö" prefix-matches "Öörni"."""
        tokens = [fold_token(t) for t in _tokenize_text(query)]
        if not tokens:
            return None

        self._rebuild_word_list()
        sets: list[set[str]] = []
        for token in tokens:
            exact = self._word_index.get(token)
            if exact:
                sets.append(exact)
            else:
                prefix_match = self._prefix_match(token)
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

    def _prefix_match(self, prefix: str) -> set[str]:
        """Find all track IDs matching tokens that start with prefix."""
        lo = bisect.bisect_left(self._word_list, prefix)
        hi = bisect.bisect_right(self._word_list, prefix + "\uffff")
        result: set[str] = set()
        for word in self._word_list[lo:hi]:
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
        if not filter_duplicates or offset >= _PRIMARY_ORDER_MIN_OFFSET:
            # Every row counts (no duplicate filter), or a deep page of the
            # primary-only order: a plain slice — O(limit) instead of walking
            # ``offset`` rows (a deep page cost 68-136 ms at 263K tracks).
            order = idx if not filter_duplicates else self._primary_order(attr, idx)
            n = len(order)
            if descending:
                page = order[max(0, n - offset - limit):max(0, n - offset)]
                page = page[::-1]
            else:
                page = order[offset:offset + limit]
            if order is idx:
                page = [tid for _v, tid in page]
            return [self._meta_dict(tid) for tid in page if tid in self._tracks]

        # A shallow page with duplicates hidden: walk the chosen sorted index
        # in the requested direction, filtering duplicates per row (cheaper
        # than building the primary-only order for it).
        walk_iter = reversed(idx) if descending else iter(idx)

        # Consume offset items first, then collect limit items.
        skipped = 0
        collected: list[str] = []
        for _, tid in walk_iter:
            if filter_duplicates:
                t = self._tracks.get(tid)
                if not t or not t.get("is_duplicate_primary", True):
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
        return (self._mutation_seq,
                self._duration_seq if attr == "_sorted_duration" else -1)

    def _primary_order(self, attr: str, idx: list[tuple]) -> list[str]:
        """The ids of sorted index ``attr`` (``idx``) that are duplicate-group
        primaries, in index order — memoised until the next track write
        (``_mutation_seq``; for ``_sorted_duration`` also ``_duration_seq``, a
        duration-only write re-keys it) or until the list itself is replaced
        (a batch exit rebuilds it without a new write).  O(N) to build (~125
        ms at 263K tracks), then a deep page is a slice (~1 ms for 500 rows).
        A rebuild also drops every other stale entry, so the memo never keeps
        a replaced sorted list (~17 MB of tuples at 263K tracks) alive."""
        key = self._primary_order_key(attr)
        memo = self._primary_order_memo
        hit = memo.get(attr)
        if hit is not None and hit[0] is idx and hit[1] == key:
            return hit[2]
        for a in [a for a, (lst, k, _o) in memo.items()
                  if k != self._primary_order_key(a) or lst is not getattr(self, a, None)]:
            del memo[a]
        tracks = self._tracks
        order = [tid for _v, tid in idx
                 if (t := tracks.get(tid)) is not None and t.get("is_duplicate_primary", True)]
        memo[attr] = (idx, key, order)
        return order

    def _meta_dict(self, tid: str) -> dict:
        t = self._tracks.get(tid)
        if not t:
            return {}
        return {k: v for k, v in t.items() if k != "embedding"}

    # ── Aggregations ─────────────────────────────────────────────────────

    def _agg_cache_get(self, key: str) -> Any | None:
        """Return the cached aggregation result for ``key`` if still valid.

        The cache is keyed on ``_mutation_seq`` so any track upsert / update /
        delete since the previous call automatically invalidates every entry
        without an explicit ``clear()`` — mirrors the duplicate-snapshot
        cache pattern in api/smart.py.
        """
        entry = self._agg_cache.get(key)
        if entry is not None and entry[0] == self._mutation_seq:
            return entry[1]
        return None

    def _agg_cache_set(self, key: str, value: Any) -> Any:
        self._agg_cache[key] = (self._mutation_seq, value)
        return value

    def _count_primaries(self, tids: set[str]) -> int:
        """Number of ``tids`` that are duplicate-group primaries.

        Uses the exact same predicate as ``filter_tracks(filter_duplicates=
        True)`` — ``is_duplicate_primary`` defaulting to True for any track not
        in a duplicate group — so an aggregate legend count computed with this
        helper agrees with what the corresponding drill-down actually returns.
        """
        return sum(
            1 for tid in tids
            if self._tracks.get(tid, {}).get("is_duplicate_primary", True)
        )

    def primary_track_count(self) -> int:
        """Total tracks excluding non-primary duplicates.

        The deduped analogue of ``track_count`` — the denominator the library
        API uses for the "[No Artist]" / "[No Album Artist]" bucket when
        ``filter_duplicates`` is on, so that bucket counts only the primaries
        the drill-down would surface (never the hidden duplicate copies).
        Memoised on ``_mutation_seq`` like the other aggregate views.
        """
        cached = self._agg_cache_get("primary_track_count")
        if cached is not None:
            return cached
        n = sum(
            1 for t in self._tracks.values()
            if t.get("is_duplicate_primary", True)
        )
        return self._agg_cache_set("primary_track_count", n)

    def aggregate_artists(self, primary_only: bool = False) -> list[dict]:
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
        cache_key = "artists:primary" if hide_dups else "artists"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_artists.items():
            tids = self._tag_artist.get(key)
            if not tids:
                continue
            if hide_dups:
                count = self._count_primaries(tids)
                if count == 0:
                    continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("artist") or "").strip() if t else key
            results.append({"artist": name or key, "count": count})
        results.sort(key=lambda x: x["artist"].lower())
        return self._agg_cache_set(cache_key, results)

    def aggregate_album_artists(self, primary_only: bool = False) -> list[dict]:
        """Album-artists with track counts.  Mirrors ``aggregate_artists``'s
        ``primary_only`` handling — caller-controlled primary-only counts, drop
        0-count entries, distinct ``:primary`` cache key — so the web Album
        Artists browse legend stays consistent with its deduped drill-down
        while Subsonic / DLNA keep raw counts."""
        hide_dups = primary_only
        cache_key = "album_artists:primary" if hide_dups else "album_artists"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_album_artists.items():
            tids = self._tag_album_artist.get(key)
            if not tids:
                continue
            if hide_dups:
                count = self._count_primaries(tids)
                if count == 0:
                    continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("album_artist") or "").strip() if t else key
            results.append({"album_artist": name or key, "count": count})
        results.sort(key=lambda x: x["album_artist"].lower())
        return self._agg_cache_set(cache_key, results)

    def aggregate_albums(
        self, artist: str | None = None, album_artist: str | None = None,
        primary_only: bool = False,
    ) -> list[dict]:
        """Albums, optionally filtered by artist/album_artist.

        With ``primary_only`` the count excludes non-primary duplicates so the
        "N Tracks" legend agrees with the deduped album drill-down
        (``/search/filter?album=X`` intersected with the same artist /
        album_artist scope, ``filter_duplicates=True``); an album whose members
        are all hidden duplicates is dropped.  Same caller-controlled,
        Subsonic/DLNA-stays-raw contract as the other browse aggregates."""
        # Cache key embeds the filter args + dup mode so each combination
        # caches independently.
        suffix = ":primary" if primary_only else ""
        cache_key = (
            f"albums::{(artist or '').lower()}::{(album_artist or '').lower()}{suffix}"
        )
        cached = self._agg_cache_get(cache_key)
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
            if primary_only:
                members = tids if scope is None else (tids & scope)
                # Prefer a PRIMARY representative so the cover-art id points at
                # a track the drill-down actually returns.
                primaries = [
                    x for x in members
                    if self._tracks.get(x, {}).get("is_duplicate_primary", True)
                ]
                count = len(primaries)
                if count == 0:
                    continue
                rep = primaries[0]
            else:
                rep = next(iter(tids))
            t = self._tracks.get(rep)
            name = (t.get("album") or "").strip() if t else key
            # ``track_id`` is a representative track for the album so the
            # frontend can build its cover-art URL directly instead of
            # round-tripping a /search/filter lookup per grid card.
            results.append({"album": name or key, "count": count, "track_id": rep})
        results.sort(key=lambda x: x["album"].lower())
        return self._agg_cache_set(cache_key, results)

    def aggregate_genres(self, primary_only: bool = False) -> list[dict]:
        """Genres with track counts.  With ``primary_only`` the count excludes
        non-primary duplicates so the legend agrees with the deduped drill-down
        (``/search/filter?genre=X``); a genre whose members are all hidden
        duplicates is dropped instead of showing a "ghost" count.  Like the
        other browse aggregates ``primary_only`` is caller-controlled (web
        library opts in; Subsonic keeps raw).  Distinct ``:primary`` cache
        key, mirroring the format legend."""
        hide_dups = primary_only
        cache_key = "genres:primary" if hide_dups else "genres"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_genres.items():
            tids = self._tag_genre.get(key)
            if not tids:
                continue
            if hide_dups:
                count = self._count_primaries(tids)
                if count == 0:
                    continue
            # Resolve proper-cased display name from one track
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = key  # fallback
            if t:
                for g in t.get("genre", []):
                    if g.strip().lower() == key:
                        name = g.strip()
                        break
            results.append({"genre": name, "count": count})
        results.sort(key=lambda x: x["genre"].lower())
        return self._agg_cache_set(cache_key, results)

    def aggregate_scene_group(self, primary_only: bool = False) -> list[dict]:
        """Demoscene groups with track counts — a browse facet over the Demozoo
        ``scene_group`` enrichment (a track credited to "Fairlight • Maniacs of
        Noise" counts under both).  Mirrors ``aggregate_genres``: ``primary_only``
        excludes hidden duplicates (web library opts in, Subsonic keeps raw),
        cached under a distinct ``:primary`` key."""
        hide_dups = primary_only
        cache_key = "scene_group:primary" if hide_dups else "scene_group"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, tids in self._tag_scene_group.items():
            if not tids:
                continue
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
        return self._agg_cache_set(cache_key, results)

    def aggregate_formats(self, primary_only: bool = False) -> list[dict]:
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
        # Key on the dup-filter state so a runtime toggle can't serve a list
        # computed under the other mode (the cache is also seq-invalidated).
        cache_key = "formats:primary" if hide_dups else "formats"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, tids in self._tag_format.items():
            if not key or not tids:
                continue
            if hide_dups:
                # Count only primary tracks; a format that survives ONLY as
                # non-primary duplicates is dropped (matches the drill-down).
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
        return self._agg_cache_set(cache_key, results)

    def aggregate_years(self, primary_only: bool = False) -> list[dict]:
        """Years with track counts.  With ``primary_only`` the count excludes
        non-primary duplicates so the legend agrees with the deduped drill-down
        (``/search/filter?year_min=Y&year_max=Y``).

        There is no ``_tag_year`` bucket to walk (years drive a *range* index,
        not a tag set), so the primary path scans ``_tracks`` once — the same
        O(N)-on-cache-miss cost the format/genre buckets already pay — keying
        each primary by ``normalise_year`` exactly as ``_sorted_year`` (which
        backs the drill-down) does, so the counts line up.  Any year left with
        no primaries simply never appears (no "ghost" year).  Caller-controlled
        like the sibling browse aggregates."""
        hide_dups = primary_only
        cache_key = "years:primary" if hide_dups else "years"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached
        # Counter stores raw year values; normalize YYYYMMDD → YYYY.  The
        # previous code had two branches that performed the same division —
        # the first was unreachable because the second matched first for any
        # 5-or-more-digit value.
        merged: dict[int, int] = {}
        if hide_dups:
            for t in self._tracks.values():
                if not t.get("is_duplicate_primary", True):
                    continue
                y = normalise_year(t.get("year"))
                if y is None:
                    continue
                merged[y] = merged.get(y, 0) + 1
        else:
            for y, count in self._agg_years.items():
                if isinstance(y, int) and y > 9999:
                    y = y // 10000
                merged[y] = merged.get(y, 0) + count
        results = sorted(
            [{"year": y, "count": c} for y, c in merged.items()],
            key=lambda x: -x["year"],
        )
        return self._agg_cache_set(cache_key, results)

    # ── Recently added (sorted index) ────────────────────────────────────

    def recently_added(self, limit: int = 100) -> list[dict]:
        ids = _sorted_tail(self._sorted_added_at, limit)
        return [self._meta_dict(tid) for tid in ids if tid in self._tracks]

    def list_unplayed(self, limit: int = 100) -> list[dict]:
        """Return tracks never played, newest-first.

        Backed by the incrementally-maintained ``_unplayed_ids`` set — walks
        ``_sorted_added_at`` in reverse, filtering membership.  This is
        O(limit + misses) instead of the previous O(N) scan over every
        track + ``get_all_play_stats`` snapshot in api/smart.py.
        """
        results: list[dict] = []
        unplayed = self._unplayed_ids
        for _, tid in reversed(self._sorted_added_at):
            if tid in unplayed and tid in self._tracks:
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
        self, playlist_id: str, updates: dict,
    ) -> dict | None:
        """Apply ``updates`` to a playlist.

        When ``updates`` contains ``track_ids`` (or the legacy ``tracks``
        alias), any ids that don't exist in the track store are silently
        dropped and recorded under ``dropped_ids`` on the returned dict —
        a stale client clinging to deleted tracks no longer leaves orphan
        ids inside the playlist after a save.  Callers that want strict
        validation can inspect ``dropped_ids`` and 400 the response.
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
            result.append(d)
        return result

    def delete_scan_dir(self, path: str) -> bool:
        if self._scan_dirs.pop(path, None) is not None:
            self._aof("delete_scan_dir", path=path)
            return True
        return False

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
