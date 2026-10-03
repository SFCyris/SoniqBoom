# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retro-album provenance + the opt-in "album from folder name" pass.

Retro formats (modules, SID, chip rips) rarely carry an album/game tag, so the
Albums view and the Subsonic album list lose them.  Albums are filled from, in
falling order of confidence (``TrackMeta.album_source``):

  ``"tag"``              the file's own header (set at extract time);
  ``"modland"``          the game dir of the exact-MD5 Modland match;
  ``"songdb"``           the exact-MD5 UADE song-database match;
  ``"modland-filename"`` a gated ``<game>-<part>`` Modland file name;
  ``"folder"``           this module's pass — the track's folder name.

No pass overwrites an album the file carries (except an SPC game name its
32-byte ID666 field cut off, which the exact Modland match completes —
``spc_name_completes``) or touches a field the user hand-edited
(``user_edited``).  A pass fills an empty album, may replace a
weaker guess (Modland a ``"folder"`` album, the song database a ``"folder"``
or ``"modland-filename"`` one), and withdraws its own album when it no longer
applies — the file header's name comes back (``header_album_back``).  Each
source's name is also kept per track (``TrackMeta.game_by_*``, the game and
its aliases — ``store.game_follow``); the folder pass names that way every
retro track whose folder it accepts (none in the HVSC tree), whatever its
album.

The folder pass is opt-in (config ``retro_album_from_folder``, default off).
It runs after each scan and immediately when the setting is switched on;
switching it off reverts exactly the albums it stamped (``album_source ==
"folder"``); each pass re-judges the albums it stamped earlier, so the
result depends on the library alone, not on earlier passes.  It is one O(N)
pass over the stored path strings — no filesystem calls — whose per-folder
verdict is memoised, then a sibling vote over the collected folders
(``finalize_folder_updates``), followed by chunked batch writes and ONE
album/browse cache invalidation.  After its first run a pass judges only the
tracks written since the last one and the tracks whose verdict reads theirs
(``_FolderBook``) — with the same result as judging every track.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata

log = logging.getLogger(__name__)

SOURCE_TAG = "tag"
SOURCE_MODLAND = "modland"
SOURCE_MODLAND_FILENAME = "modland-filename"
SOURCE_FOLDER = "folder"
SOURCE_SONGDB = "songdb"            # the UADE song database (core/songdb.py)
DERIVED_SOURCES = frozenset({SOURCE_MODLAND, SOURCE_MODLAND_FILENAME, SOURCE_FOLDER,
                             SOURCE_SONGDB})

CONFIG_KEY = "retro_album_from_folder"          # default False (opt-in)

# The Subsonic side's folder albums (album-less tracks grouped into one album
# per folder in the ID3 views — api/subsonic.py; nothing is written to the
# tracks).  One default for the API and the Settings form, so the checkbox
# always shows what Subsonic clients actually get.
SUBSONIC_FOLDER_ALBUMS_KEY = "subsonic_folder_albums"
SUBSONIC_FOLDER_ALBUMS_DEFAULT = True

# Store write strategy (measured on a 263K-track library): an album-only
# incremental update costs ~0.15-0.5 ms/track (bisect maintenance of the
# re-keyed sorted lists), batch mode ~0.014 ms/track plus, on exit, a merge of
# the re-keyed entries into each touched sorted list (~85 ms per list).  Up to
# the threshold we write incrementally, a bulk change uses batch mode; either
# way in small chunks, yielding to the loop once ``_YIELD_BUDGET_S`` of loop
# time has passed — a time budget rather than a fixed chunk count, so a slower
# (NAS) CPU yields just as often (fixed 50-item chunks stalled the loop 20-55
# ms per write at 263K tracks).
_BATCH_MODE_THRESHOLD = 10_000
_INCREMENTAL_CHUNK = 10
_WRITE_CHUNK = 200
_YIELD_BUDGET_S = 0.005
# The yield is a short timer, not ``sleep(0)``: a ``sleep(0)`` re-queues the
# pass ahead of the I/O poll, so requests and due timers waited several slices
# (loop p95 12.9 ms under uvloop during a full pass, 7.3 ms with the timer, for
# +3 % pass time) — the catalogue build's pattern (core/subsonic_index.py).
_YIELD_SEC = 0.0005
_SCAN_CHUNK = 500
_SETTLE_ROUNDS = 4              # album votes, taken again after withdrawals to a header name
_REVERT_SCAN_CHUNK = 5000       # a plain field test per track: ~5 ms per chunk
_GUARD_CHUNK = 1000             # guard pre-pass items per budget check
_AUTOAPPLY_SETTLE_S = 10
# Config marker: the ``album_source`` values whose revert (their option was
# switched off) started and has not finished — a restart resumes them
# (``resume_pending_revert``).
REVERT_PENDING_KEY = "album_revert_pending"


# ── Shared album-write helpers (used by the Modland apply too) ──────────────

def field_locked(t: dict, field: str) -> bool:
    """True when the user hand-edited ``field`` on this track (store-only
    edit recorded in ``user_edited``) — an enrichment pass must never touch it."""
    ue = t.get("user_edited")
    return isinstance(ue, list) and field in ue


def album_edit_locked(t: dict) -> bool:
    """True when the user hand-edited this track's album (store-only edit)."""
    return field_locked(t, "album")


def spc_name_completes(full: str, cut: str, fmt: str | None) -> bool:
    """True when ``full`` completes ``cut``, an SPC ID666 game name cut off
    at its 32-byte field (``Street Fighter II - The World Wa`` →
    ``… World Warrior``): SPC, ``cut`` at least 31 characters, and ``full``
    longer and starting with it (case-insensitive).

    One rule for the Modland apply (which completes the cut name) and for the
    re-extract / rescan paths (which must then keep the completion)."""
    if (fmt or "").upper() != "SPC":
        return False
    c = (cut or "").strip()
    f = (full or "").strip()
    return len(c) >= 31 and len(f) > len(c) and f.lower().startswith(c.lower())


def invalidate_album_caches() -> None:
    """Drop the HTTP aggregation cache + EVERY folder-browse cache (and its
    on-disk copy) — both are validated by track COUNT, which a field-only
    update doesn't move.  The fallback for when the changed tracks are not
    known; the enrichment passes use the targeted ``refresh_album_caches``."""
    _invalidate_agg_cache()
    try:
        from soniqboom.api.fstree import invalidate_browse_cache
        invalidate_browse_cache()
    except Exception:                                   # noqa: BLE001
        log.debug("browse cache invalidation failed", exc_info=True)


def _invalidate_agg_cache() -> None:
    try:
        from soniqboom.api.library import invalidate_agg_cache
        invalidate_agg_cache()
    except Exception:                                   # noqa: BLE001
        log.debug("agg cache invalidation failed", exc_info=True)


_MISSING = object()
# Row fields the folder listings' duplicate filter reads (its memos are keyed
# on the listing, so a change to one of these must drop them).
_DEDUP_FIELDS = frozenset(("is_duplicate_primary", "duplicate_group_id"))
# True once this process deleted the on-disk browse cache because rows it
# refreshed in memory made it stale (see ``refresh_album_caches``).
_browse_disk_stale = False


def browse_disk_stale() -> bool:
    """True when ``refresh_album_caches`` deleted the on-disk browse cache in
    this process — the in-memory one is current, so a graceful shutdown can
    persist it again instead of the next boot rebuilding it."""
    return _browse_disk_stale


def mark_browse_disk_stale() -> None:
    """The in-memory browse rows are newer than the on-disk copy (which is
    still self-invalidating by track count): let a graceful shutdown re-save."""
    global _browse_disk_stale
    _browse_disk_stale = True


def _drop_browse_disk_cache() -> None:
    global _browse_disk_stale
    try:
        from soniqboom.api.fstree import _BROWSE_CACHE_FILENAME
        from soniqboom.config import get_data_dir
        (get_data_dir() / _BROWSE_CACHE_FILENAME).unlink(missing_ok=True)
        _browse_disk_stale = True
    except Exception:                                   # noqa: BLE001
        log.debug("browse cache file removal failed", exc_info=True)


async def refresh_album_caches(changed_ids) -> None:
    """After a field-only enrichment write to the tracks ``changed_ids``: drop
    the web aggregation cache (validated by track count, which a field update
    doesn't move) and re-shape exactly those tracks' folder-browse rows IN
    PLACE, instead of dropping every browse cache.

    The per-scan-root sorted cache (``fstree._SCAN_ROOT_FULL_CACHE``) holds
    one dict per track, shared by the per-path listings and the listing memos,
    so updating it refreshes them all: nothing is rebuilt (a rebuild blocked
    the loop 1.6-3.6 s per root on the next folder click at 263K tracks) and
    rows frozen out of the cyclic GC at startup stay frozen.  A row is found
    by a bisect on its path and re-shaped exactly as the cache builds it
    (``TrackMeta`` dump + ``_scanned``); only changed values are written, one
    key at a time, so a worker thread reading the row never sees it half
    empty.  A row that isn't where expected (or doesn't validate) falls back
    to ``invalidate_album_caches``.  An FS-walk listing holding one of the
    tracks drops its shaped rows (it keeps its file list), the memos are dropped when
    a duplicate flag changed, and when a cached row changed the on-disk
    browse cache is deleted — its size-only check would restore the old values
    at the next boot (``browse_disk_stale``).  Yields to the loop every
    ``_YIELD_BUDGET_S``; must run on the loop."""
    ids = list(dict.fromkeys(changed_ids or ()))
    if not ids:
        return
    _invalidate_agg_cache()
    try:
        from bisect import bisect_left

        from soniqboom.api import fstree
        from soniqboom.core.store import get_store
        from soniqboom.models.track import TrackMeta
    except Exception:                                   # noqa: BLE001
        log.debug("browse row refresh unavailable", exc_info=True)
        return
    store = get_store()
    fields = TrackMeta.model_fields
    dedup_changed = False
    rows_changed = 0
    t0 = time.perf_counter()
    for tid in ids:
        t = store.get_track(tid)
        e = fstree._SCAN_ROOT_FULL_CACHE.get(t.get("scan_root_hash") or "") if t else None
        if e is not None:
            paths, dicts = e["paths"], e["dicts"]
            path = t.get("path") or ""
            i = bisect_left(paths, path)
            row = dicts[i] if i < len(paths) and paths[i] == path else None
            try:
                if row is None or row.get("id") != tid:
                    raise LookupError(path)
                new = TrackMeta(**{k: v for k, v in t.items()
                                   if k in fields and k != "embedding"}
                                ).model_dump(exclude={"embedding"})
            except Exception:                           # noqa: BLE001
                log.debug("browse row refresh fell back to a full drop", exc_info=True)
                invalidate_album_caches()
                return
            new["_scanned"] = True
            changed = False
            for k, v in new.items():
                if row.get(k, _MISSING) != v:
                    dedup_changed = dedup_changed or k in _DEDUP_FIELDS
                    row[k] = v
                    changed = True
            rows_changed += changed
        if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
            await asyncio.sleep(_YIELD_SEC)             # keep requests flowing
            t0 = time.perf_counter()
    ids_set = set(ids)
    for entry in list(fstree._TRACKS_META_CACHE.values()):
        id_map = entry.get("id_map") or {}
        small, big = (ids_set, id_map) if len(ids_set) < len(id_map) else (id_map, ids_set)
        if any(k in big for k in small):
            entry.pop("results", None)
    if dedup_changed:
        with fstree._MEMO_LOCK:
            for memo in (fstree._DEDUP_MEMO, fstree._DIRECT_MEMO, fstree._BYID_MEMO):
                memo.clear()
    if rows_changed:
        # The disk copy is written from these in-memory entries at boot
        # (``fstree.warmup_scan_root_caches``), so only a changed row makes it
        # stale.
        _drop_browse_disk_cache()


# Field groups written compare-and-set: a group's fields are dropped from a
# patch when any of them changed since the patch was computed, or when the
# user hand-edited the group's lead field.
_GUARDED_GROUPS = (("album", "album_source"), ("artist",))


def _same(a, b) -> bool:
    return (a or None) == (b or None)             # "" and None are both "unset"


# ``store._diff_updates``, bound on first use (core/store.py imports this module).
_diff_updates = None


def header_album_back(t: dict) -> dict:
    """The album patch withdrawing a derived album from track ``t``: the file
    header's own game back (``game_by_tag`` — e.g. an SPC's 32-byte cut the
    withdrawn album completed), else empty."""
    hdr = (t.get("game_by_tag") or "").strip()
    return ({"album": hdr, "album_source": SOURCE_TAG} if hdr
            else {"album": "", "album_source": None})


def _guard_item(store, tid: str, upd: dict, expect: dict | None
                ) -> tuple[dict | None, bool]:
    """Compare-and-set check of one patch against the track as it is NOW.

    ``upd`` may be a callable: it is then called with the track as it is now
    and returns the patch (or None) — a patch computed at write time needs no
    ``expect``.

    Returns ``(patch, album_written)``: ``patch`` without the fields already
    at their value and without every guarded group (``_GUARDED_GROUPS``) whose
    current value differs from ``expect`` or whose lead field the user
    hand-edited — None when nothing is left, the track is gone, or ``expect``
    names a ``file_md5`` the track no longer has (a rescan swapped in a
    different file: every field of the patch, ``scene_path`` included,
    described the old one).  Synchronous: the caller writes the result with
    no ``await`` in between, so no rescan / edit can land between check and
    write."""
    global _diff_updates
    if _diff_updates is None:
        from soniqboom.core.store import _diff_updates
    t = store.get_track(tid)
    if t is None:
        return None, False
    if callable(upd):
        upd = upd(t)
        if not upd:
            return None, False
    if expect is not None and "file_md5" in expect and t.get("file_md5") != expect["file_md5"]:
        return None, False
    upd = _diff_updates(t, upd)                   # a no-op field is no write
    if not upd:
        return None, False
    album = False
    for group in _GUARDED_GROUPS:
        if not any(f in upd for f in group):
            continue
        stale = expect is not None and any(
            f in expect and not _same(t.get(f), expect[f]) for f in group)
        if stale or field_locked(t, group[0]):
            for f in group:
                upd.pop(f, None)
        elif group[0] == "album":
            album = True
    return (upd or None), album


async def commit_album_updates(
    items: list[tuple[str, dict, dict | None]],
    *, written: list[str] | None = None,
) -> tuple[int, int]:
    """Write ``(track_id, updates, expected)`` patches on the loop thread.

    ``expected`` maps field → the value the patch was computed from (a
    ``file_md5`` entry is an identity precondition for the whole patch).  The
    guarded groups (``album``+``album_source``, ``artist``) are compare-and-set:
    a group is dropped from the patch when the track's current value differs
    from ``expected`` (e.g. a rescan or an edit landed meanwhile) or when the
    user hand-edited it (``user_edited``).  Other fields are written as-is.
    The check runs again for every chunk right before its write (no ``await``
    in between), so a rescan or edit landing between two chunks is kept too.

    Large batches run in store batch mode and exit through the scanner's
    yielding, rebuild-lock-serialised ``_async_exit_batch_mode``.

    ``written`` (optional) collects the ids of the tracks patched — the input
    of ``refresh_album_caches``.

    Returns ``(tracks_updated, album_changes)``.  Must run on the event loop."""
    updated, albums, _bumps = await _commit_album_updates(items, written=written)
    return updated, albums


async def _commit_album_updates(
    items: list[tuple[str, dict, dict | None]],
    *, written: list[str] | None = None,
) -> tuple[int, int, int]:
    """``commit_album_updates`` plus the number of enrichment change-log
    entries its own writes added (``store._enrich_seq``, measured around each
    synchronous store write), so a caller can tell its own writes from a
    concurrent mutation (``enrich_delta.record``)."""
    from soniqboom.core.store import get_store
    store = get_store()
    # Up-front pass: only decides "anything to do?" and the write strategy, so
    # it stops as soon as the batch-mode threshold is crossed and yields once
    # the time budget is used up; the authoritative check is the per-chunk one
    # in ``write`` below, right before each store write.
    pending = 0
    t0 = time.perf_counter()
    for i in range(0, len(items), _GUARD_CHUNK):
        for tid, upd, expect in items[i:i + _GUARD_CHUNK]:
            if _guard_item(store, tid, upd, expect)[0] is not None:
                pending += 1
        if pending > _BATCH_MODE_THRESHOLD:
            break
        if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
            await asyncio.sleep(_YIELD_SEC)
            t0 = time.perf_counter()
    if not pending:
        return 0, 0, 0
    applied = albums = bumps = 0

    def write(chunk) -> None:
        nonlocal applied, albums, bumps
        batch: list[tuple[str, dict]] = []
        for tid, upd, expect in chunk:
            patch, album = _guard_item(store, tid, upd, expect)
            if patch is not None:
                batch.append((tid, patch))
                albums += album
        if batch:
            seq0 = store._enrich_seq
            applied += store.update_track_fields_batch(batch)
            bumps += store._enrich_seq - seq0
            if written is not None:
                written.extend(tid for tid, _patch in batch)

    async def write_all(chunk: int) -> None:
        # Each ``write`` guards and writes its chunk with no await in between;
        # the loop gets a turn whenever the time budget is used up.
        t0 = time.perf_counter()
        for i in range(0, len(items), chunk):
            write(items[i:i + chunk])
            if (i + chunk < len(items)
                    and time.perf_counter() - t0 >= _YIELD_BUDGET_S):
                await asyncio.sleep(_YIELD_SEC)         # keep requests flowing
                t0 = time.perf_counter()

    if pending <= _BATCH_MODE_THRESHOLD:
        await write_all(_INCREMENTAL_CHUNK)
        return applied, albums, bumps
    store.enter_batch_mode()
    try:
        await write_all(_WRITE_CHUNK)
    finally:
        try:
            from soniqboom.core.scanner import _async_exit_batch_mode
        except Exception:                               # noqa: BLE001
            store.exit_batch_mode()
        else:
            await asyncio.shield(_async_exit_batch_mode(store))
    return applied, albums, bumps


# ── Folder-name cleaning + stoplist ──────────────────────────────────────────

_ARCHIVE_EXT_RE = re.compile(
    r"(\.tar)?\.(zip|lha|lzh|lzx|7z|rar|tar|tgz|gz|bz2|xz|adf|adz|dms|d64|d71|d81)$",
    re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W_]+")          # Unicode letters/digits
# Container words that name a pile of tunes, not an album.  Compared against
# the cleaned, lower-cased folder name.  Deliberately broad: a refused folder
# just leaves the album empty, a wrong one mislabels every track in it.
_GENERIC_NAMES = frozenset("""
music musics musik mods mod modules module tracker trackers tracked tracks
amiga c64 sid sids tunes tune songs song misc miscellaneous unsorted sorted
various va mixed mix other others new old incoming inbox downloads download
unknown unnamed untitled chiptune chiptunes chip chips chipmusic collection
collections archive archives files audio sounds sound sfx samples sample
instruments instrument instr smp rips rip ripped games game demos demo intros
intro cracktros cracktro musicdisks musicdisk disks disk discs disc
vgm vgz nsf nsfe spc gbs psf psf2 usf gsf 2sf ssf dsf ay kss sap hes gym ym
sndh sc68 adlib opl atari nes snes gameboy gba nds genesis megadrive sega
nintendo playstation msx pc dos st tfmx protracker fasttracker screamtracker
impulsetracker octamed med xm s3m it mtm 669 ahx hvl hively hvsc modland
unexotica exotica modarchive aminet scene demoscene keygen keygens best
favorites favourites top playlist playlists temp tmp test tests media library
retro oldschool 8bit 16bit vgmrips zophar uade players docs doc extras bonus
unused unreleased sets set pack packs remixes remix covers originals wip
previews conversions converted midi mid unfinished cooped newschool gme entries
folder folders compo compos oldies oldie release releases soundtracker
noisetracker startrekker data code artists authors musicians
mp3 flac ogg oga opus aac m4a wav aif aiff wma ape wv dsd dff
multi multichannel mch stereo 2ch hires
soundtrack soundtracks ost osts snesmusic joshw vgmrips konami capcom namco
taito squaresoft enix hudson tecmo koei atlus sunsoft jaleco natsume
psygnosis ubisoft activision codemasters thalion
""".split()) | frozenset({
    "various artists", "unknown artist", "unknown album", "music disks",
    "the mod archive", "mod archive", "best of", "my music", "music library",
    "old school", "8 bit", "16 bit", "atari st", "game boy", "mega drive",
    "ad lib", "commodore 64", "c64 music", "amiga music", "game music",
    "video game music", "chip music", "not finished", "work in progress",
    # Platform / category folders ("demos/groups/fairlight/ms-dos",
    # "MOD - 4 channels/co-op") and a hi-res disc layer.
    "ms-dos", "msdos", "co-op", "hi-res",
    # Console / platform and publisher folders of game-rip collections.
    "super nintendo", "nintendo entertainment system",
    "super nintendo entertainment system", "sega genesis", "nintendo 64",
    "game boy advance", "game boy color", "nintendo ds", "sega saturn",
    "master system", "sega master system", "pc engine", "turbografx 16",
    "neo geo", "playstation 2", "snesmusic org", "hudson soft",
    "square enix", "game soundtracks", "video game soundtracks",
})
# Compo size categories ("bcompo2.zip::medium/") — generic only as the WHOLE
# name, so "Big Demo" or "Tiny Toon Adventures" stays an album.
_SIZE_WORDS = frozenset({"small", "medium", "large", "big", "tiny", "micro"})
# "Disk 1", "CD2", "Side A", "Part 3", "Vol 2" — a part of an album, not one.
_PART_RE = re.compile(
    r"^(?:(?:disk|disc|cd|side|part|vol|volume|tape)(?:\s*\d{1,3}|\s+[a-z])|d\d{1,2})$")
# A part of a game's music ("Level 1", "In-Game", "Game Over"): a folder or
# archive so named is no album (``_part_album_refused``).  Matched on the
# cleaned, lower-cased name ("Level_1_2" reads "level 1 2"); a numbered one
# too ("Level 1a", "Level 1-A", "Ingame 2", "Game Over 2"), and the plural
# "Levels" / "Stages" (a bare "Level" is not one).
_TUNE_PART_RE = re.compile(
    r"^(?:(?:level|lvl|stage|act|world)[\s._-]*\d{1,3}(?:[\s._-]+\d{1,3})?(?:[\s._-]?[a-z])?"
    r"|levels|stages"
    r"|(?:in[\s-]?game|title(?:[\s-]?screen)?|intro|outro|end(?:ing)?|menus?"
    r"|hi(?:gh)?[\s-]?scores?|game[\s-]?over|loaders?|boss(?:es)?|jingles?|sfx"
    r"|sound[\s-]?fx|bgm|credits?)(?:[\s._-]*\d{1,3}[a-z]?)?)$")


def _part_name(name: str) -> bool:
    return bool(_TUNE_PART_RE.match(clean_folder_name(name).lower()))


def _part_album_refused(name: str, ancestors: list[str]) -> bool:
    """Is container ``name`` named like a part of a game's music
    (``_TUNE_PART_RE``: "Level 1", "Ingame", "Game Over") and so no album —
    unless the first of its ``ancestors`` (nearest first) that is no generic
    folder is a release index ("Games/Game Over", "Games/G/Game Over"): a
    game there.  Such a folder never gives way to the one above it (a
    category's or a composer's name would be a wrong album)."""
    if not _part_name(name):
        return False
    for anc in ancestors:
        g = clean_folder_name(anc)
        if g.lower() in _RELEASE_INDEX_NAMES:
            return False
        if not _generic(g):
            return True
    return True


def _root_name(path: str, roots: frozenset[str]) -> str:
    """The name of the scan root holding ``path`` ("" when none)."""
    best = ""
    for r in roots:
        # A remote root's boundary is ":/" — a bare ":" prefix also matches
        # another share of a path-less WebDAV root (``https://h:8443/x:/…``).
        if (path.startswith(r + "/") or path.startswith(r + ":/")) and len(r) > len(best):
            best = r
    return best.rstrip("/").rsplit("/", 1)[-1]


def clean_folder_name(name: str) -> str:
    """``Gold_of_the_Aztecs`` → ``Gold of the Aztecs``: underscores become
    spaces; dots do too when the name has no spaces at all (dot-separated
    naming like ``Gold.of.the.Aztecs`` — a spaced ``Dr. Robotnik`` keeps its
    dot); whitespace collapsed; stray separators trimmed."""
    s = (name or "").replace("_", " ")
    if " " not in s.strip() and "." in s:
        s = s.replace(".", " ")
    return " ".join(s.split()).strip(" -.")


def _fold(s: str) -> str:
    """Lower-case with accents folded (``Hülsbeck`` → ``hulsbeck``) so a
    folder spelled without diacritics still matches the credited artist."""
    s = s or ""
    if s.isascii():
        return s.lower()                                # nothing to fold
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def name_key(s: str) -> str:
    """Order-insensitive word key — ``Whittaker_David`` and ``David Whittaker``
    compare equal (HVSC ``Surname_Name`` dirs vs a ``Name Surname`` artist)."""
    return " ".join(sorted(_WORD_RE.findall(_fold(s))))


# Size / channel-count categories of compo packs: "64kb", "4 64k", "100k",
# "MOD - 4 channels", "XM 32ch" — also with the format glued on ("64kbMOD",
# "64kMODrmx", "4 64krmxMOD").
_SIZE_TOKEN_RE = re.compile(r"^\d+(k|kb)?$")
_SIZE_FMT_RE = re.compile(r"^\d+kb?(?:mod|xm|it|s3m|rmx|mix)+$")
# A music TOOL's name ("HSC-Tracker", "Master Composer", "Future Player") —
# the tunes were made with it, it is not their album.
_TOOL_WORDS = frozenset({"tracker", "composer", "player", "editor", "packer",
                         "replayer", "sequencer", "synthesizer"})
_CHANNELS_RE = re.compile(r"\b\d+\s*(ch|chn|chans?|channels?)\b")


def _generic(clean: str) -> bool:
    """True when a cleaned folder name is a container / category, not an album.

    Besides the listed words, a name made ONLY of generic words, numbers,
    size tokens and single letters counts ("old mods", "Various A-Z",
    "4 64k", "64kbMOD"), as does a channel-count category ("MOD - 4
    channels") and a tool name, version number or not ("Master Composer",
    "Scream Tracker 3")."""
    low = clean.lower()
    if (low in _GENERIC_NAMES or low in _SIZE_WORDS or _PART_RE.match(low)
            or _CHANNELS_RE.search(low)):
        return True
    if sum(c.isalnum() for c in clean) < 3:
        return True                                     # "A-F", "0-9", "#"
    if not any(c.isalpha() for c in clean):
        return True                                     # "00", "1991", "01-50"
    if low.startswith(("coop-", "coop ", "not by ", "- unknown")):
        return True
    words = _WORD_RE.findall(low)
    last = len(words)
    while last and words[last - 1].isdigit():
        last -= 1                                       # "Scream Tracker 3", "Pro Tracker 3.15"
    if last and words[last - 1] in _TOOL_WORDS:
        return True                                     # "HSC-Tracker", "Master Composer"
    return bool(words) and all(
        w in _GENERIC_NAMES or len(w) == 1 or _SIZE_TOKEN_RE.match(w)
        or _SIZE_FMT_RE.match(w)
        for w in words)


_music_exts_memo: frozenset[str] | None = None


def _music_exts() -> frozenset[str]:
    """Every file extension SoniqBoom plays (``FORMAT_NAMES`` keys, no dot)."""
    global _music_exts_memo
    if _music_exts_memo is None:
        try:
            from soniqboom.core.metadata import FORMAT_NAMES
            _music_exts_memo = frozenset(k.lower().lstrip(".") for k in FORMAT_NAMES)
        except Exception:                               # noqa: BLE001
            _music_exts_memo = frozenset()
    return _music_exts_memo


def _named_like(archive_stem: str, member_name: str) -> bool:
    """Is the member (``tune.mod``, ``Gold_of_the_Aztecs.xm``, Amiga-style
    ``mod.tune``) named like the archive (``tune``, ``Gold of the Aztecs``)?"""
    a = archive_stem.lower()
    m = member_name.lower()
    names = (m, m.rsplit(".", 1)[0], m.split(".", 1)[-1])
    return a in names or name_key(a) in {name_key(x) for x in names}


def _is_wrapper(archive_stem: str, member_name: str, several: bool = False) -> bool:
    """A single-file wrapper archive (``000001.mod.zip::000001.mod``).

    An archive named ``<name>.<music ext>`` (``piano2.xm.zip``) wraps one
    module even when the member is spelled differently (``piano.xm``), or
    holds a second version of it.  Otherwise the member is named like the
    archive, and the archive holds no other tune (not ``several``: a tune
    named after its game archive stays in it)."""
    a = archive_stem.lower()
    if "." in a and a.rsplit(".", 1)[1] in _music_exts():
        return True
    return not several and _named_like(archive_stem, member_name)


def _note_archives(tracks, counts: dict[str, list], retro: dict) -> None:
    """Count the retro members of each archive in ``tracks`` (the path before
    a member's last ``::``): ``counts[archive] = [members, members not named
    like it, the first member]`` — a member inside a folder of the archive
    counts as not named like it unless every such folder is generic
    (``music/tune.mod``, judged by its file name, as ``_container_chain``
    skips those folders).  Names are judged from the second member on (most
    archives wrap one).  ``retro`` memoises ``is_retro_format`` per format."""
    from soniqboom.core.retro import is_retro_format
    for t in tracks:
        path = t.get("path") or ""
        if "::" not in path:
            continue
        fmt = t.get("format") or ""
        r = retro.get(fmt)
        if r is None:
            r = retro[fmt] = is_retro_format(fmt)
        if not r:
            continue
        k, _, member = path.rpartition("::")
        c = counts.get(k)
        if c is None:
            counts[k] = [1, 0, member]
            continue
        if c[0] == 1:
            c[1] += _other_member(k, c[2])
        c[0] += 1
        c[1] += _other_member(k, member)


def _other_member(archive: str, member: str) -> int:
    """1 when ``member`` of ``archive`` is not named like it (see
    ``_note_archives``), else 0."""
    member = member.replace("\\", "/")
    stem = _ARCHIVE_EXT_RE.sub("", archive.rsplit("::", 1)[-1].replace("\\", "/").rsplit("/", 1)[-1])
    *dirs, name = member.split("/")
    return int(any(d and not _generic(clean_folder_name(d)) for d in dirs)
               or not _named_like(stem, name))


def _several_tunes(counts: dict[str, list]) -> frozenset[str]:
    return frozenset(k for k, (n, other, _first) in counts.items() if n >= 2 and other)


def multi_member_archives(tracks) -> frozenset[str]:
    """The archives holding several retro tracks of ``tracks``, one of them
    not named like the archive — never single-file wrappers (``_is_wrapper``):
    a copy in another format (``lotus.mp3``) or version (``lotus.xm``) of the
    wrapped module doesn't count."""
    counts: dict[str, list[int]] = {}
    _note_archives(tracks, counts, {})
    return _several_tunes(counts)


def _is_root(folder: str, roots: frozenset[str]) -> bool:
    """Is ``folder`` a scan root (a remote one stored as "<root>:")?"""
    f = folder.rstrip("/")
    return f in roots or f.rstrip(":") in roots


def _container_chain(path: str, roots: frozenset[str], depth: int,
                     multi: frozenset[str] = frozenset()
                     ) -> list[tuple[str, bool, str]]:
    """The first ``depth`` containers of ``path``, innermost first, as
    ``(name, is_archive, key)`` — ``key`` identifies the container (its path,
    ``outer.zip::dir`` inside an archive).  See ``folder_candidate``; an
    archive in ``multi`` (``multi_member_archives``: several tunes) wraps
    nothing unless named ``<name>.<music ext>`` (``_is_wrapper``)."""
    segs = path.split("::")
    chain: list[tuple[str, bool, str]] = []
    fname = segs[-1].replace("\\", "/").rsplit("/", 1)[-1]
    for k in range(len(segs) - 1, 0, -1):               # innermost archive first
        arch_key = "::".join(segs[:k])
        # Container dirs INSIDE an archive ("Turrican.lha::music/title.mod")
        # are skipped when generic, so the archive's own name is used instead.
        parts = segs[k].replace("\\", "/").split("/")[:-1]
        inner = [(d, arch_key + "::" + "/".join(parts[:j + 1]))
                 for j, d in enumerate(parts)
                 if d and not _generic(clean_folder_name(d))]
        chain.extend((d, False, key) for d, key in reversed(inner))
        arch = segs[k - 1].replace("\\", "/").rsplit("/", 1)[-1]
        stem = _ARCHIVE_EXT_RE.sub("", arch)
        if not (k == len(segs) - 1 and not inner
                and _is_wrapper(stem, fname, arch_key in multi)):
            chain.append((stem, True, arch_key))
        if len(chain) >= depth:
            return chain[:depth]
    # Filesystem (or remote-share) part: start at the directory holding the
    # file — or, for an archive member, the one holding the outermost archive.
    fs = segs[0]
    parent = fs.rsplit("/", 1)[0] if "/" in fs else ""
    while parent and len(chain) < depth:
        # Remote tracks are stored as "<scan_root>:<remote path>"
        # ("ftp://host/Music/Demo:/x.mod"), so the root boundary may carry a
        # trailing colon (``_is_root``).
        if _is_root(parent, roots):
            break
        head, _, name = parent.rpartition("/")
        if name:
            chain.append((name, False, parent))
        parent = head
    return chain


def folder_candidate(path: str, roots: frozenset[str],
                     multi: frozenset[str] = frozenset()
                     ) -> tuple[str, str, bool] | None:
    """``(container_name, grandparent_name, is_archive)`` for the folder that
    holds ``path``, or None when the file sits directly in a scan root.
    ``is_archive`` is True when the container is an archive's own name (a
    release: music disk, compo pack, game rip) rather than a directory.

    Pure string work on the stored path (no filesystem access); a remote
    track's ``<scan_root>:<path>`` form is understood.  For an archive member
    (``outer.zip::dir/member``; LHA members may use ``\\``) the nearest
    container wins: the member's own (non-generic) directory inside the
    archive, else the archive's name (extension stripped); a single-file
    wrapper archive (``tune.mod.zip::tune.mod``; a member named like an
    archive of ``multi``, which holds several tunes, is no wrapper) is
    skipped outward.
    ``grandparent_name`` is the next container up ("" at a scan root) — used
    to spot a ``Format/Author/file`` or ``Artists/<name>/file`` layout, where
    the parent is a person, not an album."""
    chain = _container_chain(path, roots, 2, multi)
    if not chain:
        return None
    return chain[0][0], (chain[1][0] if len(chain) > 1 else ""), chain[0][1]


# A grandparent with one of these names makes the parent a PERSON
# ("Artists/jogeir/…", "Musicians/Avalon/…"), never an album.
_PERSON_INDEX_NAMES = frozenset({
    "artists", "artist", "musicians", "musician", "composers", "composer",
    "authors", "author", "sceners", "groups", "bands",
})
# Platform / machine names are ALSO format labels ("Amiga" is uade's fallback
# format name), but "Amiga/<Game>/file" is a game layout, not the Modland
# "Format/Author/file" one — so they never trigger the grandparent rule.
_PLATFORM_KEYS = frozenset({
    "amiga", "c64", "atari", "st", "atari st", "pc", "dos", "msx", "nes", "snes",
    "gameboy", "game boy", "gba", "nds", "sega", "genesis", "megadrive",
    "mega drive", "nintendo", "playstation", "spectrum", "zx spectrum", "cpc",
    "amstrad", "commodore", "adlib", "ad lib",
})
# A grandparent with one of these names makes the parent a RELEASE (a game,
# demo, music disk, compo) — the positive layout signal that lets a folder name
# an album even when the track carries no artist (see collect_folder_updates).
_RELEASE_INDEX_NAMES = frozenset({
    "games", "game", "game music", "game rips", "gamerips", "soundtracks",
    "soundtrack", "ost", "osts", "demos", "demo", "intros", "cracktros",
    "musicdisks", "musicdisk", "music disks", "disks", "compos", "compo",
    "competitions", "releases", "albums", "productions",
})
# "Surname, First (handle)" — HVSC-style person folder names.
_SURNAME_FIRST_RE = re.compile(r"^[^\W\d_][\w' .-]*,\s*[^\W\d_][\w' .-]*(\s*\([^)]*\))?$")
_ARTIST_PLACEHOLDERS = frozenset({"", "<?>", "?", "??", "???", "unknown", "n/a", "-"})
# HVSC (the C64 SID collection) — every folder under these is a musician, an
# A–Z bucket or a tool name; its folders never name an album.
_HVSC_TOP_DIRS = ("/MUSICIANS/", "/DEMOS/", "/GAMES/")


def _in_hvsc_tree(path: str, fmt: str) -> bool:
    if "/c64music/" in path.lower():
        return True
    return fmt == "SID" and any(d in path for d in _HVSC_TOP_DIRS)


def _words(s: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall(_fold(s)))


def _squash(s: str) -> str:
    """Letters and digits only, lower-cased, accents folded — the spacing-
    and punctuation-blind form (``JaZz^Jolly`` → ``jazzjolly``, ``Scream
    Tracker 3`` and ``ScreamTracker 3`` → ``screamtracker3``)."""
    return "".join(_WORD_RE.findall(_fold(s)))


# Format-key sets hold each name's ``name_key`` and, prefixed with this
# marker (never part of a ``name_key``), its ``_squash`` form.
_SQUASHED = "~"


def format_name_keys(names) -> set[str]:
    """The format-stoplist keys of ``names``: ``name_key`` plus the marked
    squashed key, so a folder spelled with or without spaces matches."""
    out: set[str] = set()
    for n in names:
        k = name_key(n)
        if k:
            out.add(k)
            out.add(_SQUASHED + _squash(n))
    return out


def _is_format(clean: str, key: str, format_keys: frozenset[str]) -> bool:
    return key in format_keys or (_SQUASHED + _squash(clean)) in format_keys


def _format_keys_static() -> frozenset[str]:
    """Format names SoniqBoom itself knows (extension map, retro families,
    uade labels) — the stoplist core that works without a Modland index."""
    names: set[str] = set()
    try:
        from soniqboom.core.metadata import FORMAT_NAMES
        names |= set(FORMAT_NAMES.values())
        names |= {k.lstrip(".") for k in FORMAT_NAMES}
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from soniqboom.core import retro
        names |= set(retro.RETRO_FORMATS)
        names |= set(retro._uade_retro_formats())
    except Exception:                                   # noqa: BLE001
        pass
    return frozenset(format_name_keys(names))


# ── Person match (folder named after the track's composer) ──────────────────

def _within_one_edit(a: str, b: str) -> bool:
    """Levenshtein distance ≤ 1."""
    if len(a) > len(b):
        a, b = b, a
    if len(b) - len(a) > 1:
        return False
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] if len(a) == len(b) else a[i:] == b[i + 1:]


def _credit_parts(v: str) -> list[str]:
    """Each credited person string of an artist field: the whole, its
    ``&``/``,``/``/``-separated parts and both halves of ``Real (Handle)``."""
    parts = [v, _PAREN_RE.sub(" ", v), *_PAREN_RE.findall(v)]
    for part in list(parts):
        parts.extend(_CREDIT_SPLIT_RE.split(part))
    return parts


def _names_person(album: str, names: tuple, archive: bool, memo: dict) -> bool:
    """True when the folder ``album`` names one of the persons ``names``
    (artist, album artist, composer) credit: all its words are the person's
    ("Hubbard" / Rob Hubbard), or its squashed form is (``curtcool`` / Curt
    Cool).  A DIRECTORY also matches a handle spelled differently — one edit
    apart (``freQvibes`` / Freqvibez, ``goto8o`` / Goto80), an 8.3-cut
    prefix of it (``leviatha`` / Leviathan; both ≥ 5 characters), or a
    handle whose words all appear in the folder name next to another name
    (``JaZz^Jolly`` / Jazz (NL)) rather than generic words ("Synergy Demo").
    An ARCHIVE named "<artist>-<release>" is a release, so only the first two
    tests apply to archive names.  ``memo`` (one per pass) caches the
    answer per (folder, names, archive)."""
    key = (album, names, archive)
    hit = memo.get(key)
    if hit is not None:
        return hit
    fwords = _words(album)
    fsq = _squash(album)
    found = False
    for v in names:
        if not v or v.strip().lower() in _ARTIST_PLACEHOLDERS:
            continue
        if fwords <= _words(v):
            found = True                                # "Hubbard" / Rob Hubbard
            break
        for part in _credit_parts(v):
            psq = _squash(part)
            if not psq:
                continue
            if psq == fsq:
                found = True
            elif archive:
                continue
            elif len(fsq) >= 5 and len(psq) >= 5 and (
                    _within_one_edit(fsq, psq) or psq.startswith(fsq)):
                found = True
            elif len(psq) >= 4:
                pw = _words(part)
                found = pw <= fwords and any(
                    not (w in _GENERIC_NAMES or w.isdigit()) for w in fwords - pw)
            if found:
                break
        if found:
            break
    memo[key] = found
    return found


# ── Collect: per-folder verdicts, per-parent votes ───────────────────────────

# A container's folder-level verdict when it is not an album candidate, and
# ``_V_OWN``: a track's own title / file name is the folder's name (one entry
# per folder, as compo packs store them) — refused like ``_V_OTHER``, unless
# another track of the folder is accepted (a game's title tune).
_V_ACCEPT, _V_PERSON, _V_OTHER, _V_OWN = 0, 1, 2, 3
# A container named like a part of a game's music (``_part_album_refused``):
# no album, and it takes no part in the sibling vote — so it never makes its
# archive a release that names it either.
_PART_REFUSED = "part"
# Words that name a release ("Big Demo", "Synergy Demo", "X Musicdisk"): such
# a folder stays an album even among an index of composer folders.
_RELEASE_WORDS = frozenset({
    "demo", "megademo", "trackmo", "intro", "cracktro", "musicdisk",
    "musicdisc", "soundtrack", "ost", "invitation", "compo",
})


def _judge(name: str, grand: str, from_archive: bool, *, format_keys, author_keys,
           artist_keys, root_keys) -> tuple | int:
    """Folder-level verdict: ``(album, words, key, release, from_archive,
    named_release)`` for an album candidate, else ``_V_PERSON`` (a person's
    folder) or ``_V_OTHER``.  ``release`` is the positive release signal;
    ``named_release`` adds a release word in the name (``_RELEASE_WORDS``)."""
    clean = clean_folder_name(name)
    k = name_key(clean)
    if not clean or _generic(clean) or _is_format(clean, k, format_keys) or k in root_keys:
        return _V_OTHER
    g_clean = clean_folder_name(grand)
    g_key = name_key(g_clean)
    # "Whittaker_David/Gold_of_the_Aztecs/…": a grandparent that is a known
    # PERSON (library artist / Modland author) makes this a Composer/Release
    # layout — even when uade happens to name a replayer after that composer
    # (so it is also a format name).
    g_person = bool(g_key) and (g_key in author_keys or g_key in artist_keys)
    if (k in author_keys or k in artist_keys
            or (_is_format(g_clean, g_key, format_keys) and g_key and not g_person
                and g_clean.lower() not in _PLATFORM_KEYS)    # Format/Author/file
            or g_clean.lower() in _PERSON_INDEX_NAMES
            or _SURNAME_FIRST_RE.match(clean)):
        return _V_PERSON
    # An archive's own name, a child of a release index ("Games/<X>",
    # "Compos/<X>") or of a known person is positively a release.
    release = from_archive or g_person or g_clean.lower() in _RELEASE_INDEX_NAMES
    words = _words(clean)
    return (clean, words, k, release, from_archive,
            release or not words.isdisjoint(_RELEASE_WORDS))


def _track_verdict(t: dict, cand: tuple, last: str, fmt: str, memo: dict) -> int | None:
    """Per-track check of an album candidate: ``_V_ACCEPT``, ``_V_PERSON``
    (the folder is this track's composer), ``_V_OWN`` (its own title / file
    name), ``_V_OTHER`` (the track's format) or None (no credit and no
    release signal — not judged)."""
    album, _w, k, release, from_archive, _named = cand
    names = (t.get("artist") or "", t.get("album_artist") or "", t.get("composer") or "")
    # A track that credits nobody is exactly where a plain folder is most
    # often the COMPOSER ("AHXSONGS/Mr Tickle/…"), so without an artist only
    # a positive release signal counts.
    if not release and all(n.strip().lower() in _ARTIST_PLACEHOLDERS for n in names):
        return None
    if _names_person(album, names, from_archive, memo):
        return _V_PERSON
    if k == name_key(fmt):
        return _V_OTHER
    fname = last.rsplit("/", 1)[-1]
    # "Title.mod" and Amiga-style "mdat.Title" name the tune before / after
    # the dot.
    own = (name_key(t.get("title") or ""), name_key(fname.rsplit(".", 1)[0]),
           name_key(fname.split(".", 1)[-1]))
    return _V_OWN if k in own else _V_ACCEPT


# A child folder's tally in its parent's vote: track verdict counts (indexed
# by verdict), then the first accepted track's artist and whether accepted
# tracks name several.
_T_FIRST, _T_MULTI = 4, 5


def _new_tally() -> list:
    return [0, 0, 0, 0, None, False]


def new_folder_state() -> dict:
    """The cross-chunk state of one folder pass (see ``collect_folder_updates``)."""
    return {"votes": {}, "parents": {}, "patches": [], "pending": [], "memo": {},
            "mine": {}, "own": [], "accepted": set()}


def collect_folder_updates(
    tracks: list[dict], *, roots: frozenset[str],
    format_keys: frozenset[str], author_keys: frozenset[str],
    artist_keys: frozenset[str],
    retro_cache: dict | None = None, dir_cache: dict | None = None,
    state: dict | None = None, every_track: bool = False,
    multi: frozenset[str] | None = None,
) -> list[tuple[str, dict, dict]]:
    """Folder-album patches for ``tracks`` (pure; no store access).

    ``every_track``: judge every retro track as if it had no album (one with
    an album, one the user edited — no earlier pass's album re-judged): the
    folder name each would get, for its per-source game name
    (``TrackMeta.game_by_folder``) — the albums themselves come from a normal
    collect, which may take this collect's ``state["accepted"]`` (the
    containers holding an accepted track) as the ``accepted`` of its
    ``finalize_folder_updates``, so a title tune follows siblings that
    already have an album.

    A retro track with an empty, non-user-edited album gets
    ``album = clean(container)`` + ``album_source = "folder"`` unless the
    container is: directly a scan root; a generic container word or an
    album part ("Disk 1"); a format name (SoniqBoom's or a Modland format
    dir, with or without spaces); a person — this track's artist/album-
    artist/composer (``_names_person``: its words, a squashed / one-edit /
    8.3-cut handle), any library artist or Modland author; the scan root's
    own name; the track's own title / file name (unless another track of
    the folder is accepted: a game's title tune takes its folder's verdict);
    the child of a format dir (``Format/Author/file``) or of an ``Artists``-style index; a ``Surname,
    First`` person name; or anywhere inside the HVSC tree.  A track that
    credits no artist/composer additionally needs a positive release signal:
    the container is an archive's own name, or sits under a release index
    (``Games``, ``Demos``, ``Compos``, ``Albums`` …) or under a known person's
    folder — otherwise its folder is too often the composer.

    Siblings vote (``finalize_folder_updates``): a folder whose child folders
    are mostly persons (at least 3, at least half) is an index of composers,
    so its remaining children are refused unless they carry a release signal
    or a release word ("Big Demo") — ``AHXSONGS.LHA::Mr Tickle`` is Xeron
    under an alias.  An archive whose top-level folders are mostly not albums
    (at least 2 composer or container folders, at least half) and whose other
    folders are composers too or categories holding several artists each —
    or whose folders are mostly one-entry folders named after their tune (at
    least 3, a compo pack) — is itself the release: every track in those
    folders takes the archive's name (``bcompo5.zip::weird`` → "bcompo5").
    An archive of single-composer folders (games) keeps them.

    A track whose album an earlier pass stamped (``album_source ==
    "folder"``) is judged again like an album-less one, and its album is kept,
    changed or cleared to match — so the vote, and the result, never depend
    on what earlier passes wrote.

    ``multi``: the archives holding several tunes (``multi_member_archives``
    of the whole library; a single-shot call without it takes them from
    ``tracks``) — a tune named after such an archive stays in it
    (``_is_wrapper``).  A container named like a part of a game's music
    ("Level 1", "Ingame") is no album (``_part_album_refused``).

    ``retro_cache`` / ``dir_cache`` are the per-format and per-folder memos.
    A caller that feeds the library in chunks passes the same two dicts and
    one ``new_folder_state()`` to every call (with the same other arguments)
    — those calls return nothing — and then takes the patches from
    ``finalize_folder_updates(state)``, so a folder is judged once and the
    vote sees every sibling.  Without ``state`` the call is single-shot."""
    from soniqboom.core.retro import is_retro_format
    single = state is None
    if single:
        state = new_folder_state()
    if multi is None:
        multi = multi_member_archives(tracks) if single else frozenset()
    root_keys = frozenset(name_key(r.rstrip("/").rsplit("/", 1)[-1]) for r in roots)
    judge_kw = dict(format_keys=format_keys, author_keys=author_keys,
                    artist_keys=artist_keys, root_keys=root_keys)
    if retro_cache is None:
        retro_cache = {}
    # folder key → (candidate | verdict, container key, parent key,
    #               parent is an archive, the parent archive's candidate,
    #               generic top-level archive dir key | None) or None
    if dir_cache is None:
        dir_cache = {}
    votes, parents = state["votes"], state["parents"]
    patches, pending, memo = state["patches"], state["pending"], state["memo"]
    mine = state["mine"]
    for t in tracks:
        if every_track:
            pass
        elif album_edit_locked(t):
            continue
        elif t.get("album_source") == SOURCE_FOLDER:
            # An album an earlier pass stamped is judged afresh, like an empty
            # one (``finalize_folder_updates`` keeps, changes or clears it).
            mine[t["id"]] = t
        elif (t.get("album") or "").strip():
            continue
        fmt = t.get("format") or ""
        r = retro_cache.get(fmt)
        if r is None:
            r = retro_cache[fmt] = is_retro_format(fmt)
        if not r:
            continue
        path = t.get("path") or ""
        # Memo key = the folder holding the file.  An archive member at the
        # archive's root keys on its full path: whether the archive is a
        # single-file wrapper depends on the member's own name.
        last = path.rsplit("::", 1)[-1].replace("\\", "/")
        if "::" not in path:
            ckey = path.rsplit("/", 1)[0]
        elif "/" not in last:
            ckey = path
        else:
            ckey = path.rsplit("::", 1)[0] + "::" + last.rsplit("/", 1)[0]
        if fmt == "SID":                    # the HVSC verdict depends on format
            ckey = "SID\0" + ckey
        info = dir_cache.get(ckey, ...)
        if info is ...:
            info = None
            chain = [] if _in_hvsc_tree(path, fmt) else _container_chain(path, roots, 3, multi)
            if chain:
                name, from_archive, c_key = chain[0]
                grand = chain[1][0] if len(chain) > 1 else ""
                # the names above it — the scan root's too when the chain got there
                above = [c[0] for c in chain[1:]] + (
                    [_root_name(path, roots)] if len(chain) < 3 else [])
                cand = (_PART_REFUSED if _part_album_refused(name, above) else
                        _judge(name, grand, from_archive, **judge_kw))
                p_key = p_arch = fb = None
                if len(chain) > 1:
                    p_key, p_arch = chain[1][2], chain[1][1]
                    # a part-named archive names its members no more than itself
                    if p_arch and not _part_album_refused(chain[1][0], above[1:]):
                        fb = _judge(chain[1][0], chain[2][0] if len(chain) > 2 else "",
                                    True, **judge_kw)
                        if not isinstance(fb, tuple):
                            fb = None
                # A member whose top-level archive folder was skipped as generic
                # ("bcompo5.zip::remix/…") is that archive's non-album child.
                g_child = None
                if from_archive and "::" in path and "/" in last:
                    g_child = c_key + "::" + last.split("/", 1)[0]
                info = (cand, c_key, p_key, p_arch, fb, g_child)
            dir_cache[ckey] = info
        if info is None:
            continue
        cand, c_key, p_key, p_arch, fb, g_child = info
        if cand is _PART_REFUSED:
            continue                    # no album, no vote, no archive's name
        v = _track_verdict(t, cand, last, fmt, memo) if isinstance(cand, tuple) else cand
        if g_child is not None:
            votes.setdefault(c_key, {}).setdefault(g_child, _new_tally())[_V_OTHER] += 1
        if p_key is not None:
            parents[p_key] = (p_arch, fb)
            if v is not None:
                c = votes.setdefault(p_key, {}).setdefault(c_key, _new_tally())
                c[v] += 1
                if v == _V_ACCEPT and not c[_T_MULTI]:
                    who = (t.get("artist") or t.get("album_artist")
                           or t.get("composer") or "").strip().lower()
                    if c[_T_FIRST] is None:
                        c[_T_FIRST] = who
                    elif who != c[_T_FIRST]:
                        c[_T_MULTI] = True
        if v == _V_ACCEPT:
            patches.append((t, cand, p_key))
            state["accepted"].add(c_key)
        elif p_arch and fb is not None:
            pending.append((t, p_key, last, fmt))
        if v == _V_OWN:
            state["own"].append((t, cand, p_key, c_key))
    return finalize_folder_updates(state) if single else []


def _container_key(t: dict, roots: frozenset[str], multi: frozenset[str]) -> str:
    """The key of the container the album pass judges for track ``t`` (see
    ``_container_chain``: a generic folder inside an archive and a single-file
    wrapper archive are skipped outward), or "" when there is none."""
    chain = _container_chain(t.get("path") or "", roots, 1, multi)
    return chain[0][2] if chain else ""


def _patch(t: dict, album: str) -> tuple[str, dict, dict]:
    return (t["id"], {"album": album, "album_source": SOURCE_FOLDER},
            {"album": t.get("album"), "album_source": t.get("album_source")})


def finalize_folder_updates(state: dict, accepted: set | frozenset = frozenset()
                            ) -> list[tuple[str, dict, dict]]:
    """The patches of a (chunked) collect, after the sibling vote — see
    ``collect_folder_updates``.  A track named after its folder (``_V_OWN``)
    takes the folder's album when another track there is accepted — in this
    collect or in ``accepted`` (container keys, see ``every_track``).  An
    album an earlier pass stamped is left alone when unchanged, rewritten
    when the verdict changed, and replaced by the header's name or cleared
    (``header_album_back``) when its track no longer qualifies."""
    person_index: set = set()
    release_archives: set = set()
    parents = state["parents"]
    for p_key, kids in state["votes"].items():
        n = [0, 0, 0]
        entries = 0                     # children named after their only tune
        categories = True               # every album child holds several artists
        for c in kids.values():
            other = c[_V_OTHER] + c[_V_OWN]
            # a child's verdict: the majority of its tracks (ties → not an album)
            v = max(((_V_PERSON, c[_V_PERSON]), (_V_OTHER, other),
                     (_V_ACCEPT, c[_V_ACCEPT])), key=lambda vc: vc[1])[0]
            n[v] += 1
            if v == _V_OTHER and 2 * c[_V_OWN] >= other:
                entries += 1
            elif v == _V_ACCEPT and not c[_T_MULTI]:
                categories = False
        judged = n[0] + n[1] + n[2]
        if n[_V_PERSON] >= 3 and 2 * n[_V_PERSON] >= judged:
            person_index.add(p_key)
        p_arch, fb = parents.get(p_key, (False, None))
        refused = n[_V_PERSON] + n[_V_OTHER]
        # The archive is the release when its other folders are composers
        # (an index), categories ("weird", "happy": many artists each) or
        # one-tune entry folders (a compo pack) — never when they may be
        # games ("Pack.zip::Turrican/", one composer).
        if (p_arch and fb is not None and refused >= 2 and 2 * refused >= judged
                and (p_key in person_index or categories
                     or (entries >= 3 and 2 * entries >= judged))):
            release_archives.add(p_key)
    out: list[tuple[str, dict, dict]] = []

    def archive_patch(t: dict, p_key: str, last: str, fmt: str) -> None:
        fb = parents[p_key][1]
        if _track_verdict(t, fb, last, fmt, state["memo"]) == _V_ACCEPT:
            out.append(_patch(t, fb[0]))

    for t, cand, p_key in state["patches"]:
        if p_key in release_archives:
            last = (t.get("path") or "").rsplit("::", 1)[-1].replace("\\", "/")
            archive_patch(t, p_key, last, t.get("format") or "")
        elif p_key in person_index and not cand[5]:
            continue                                    # a composer under an alias
        else:
            out.append(_patch(t, cand[0]))
    for t, p_key, last, fmt in state["pending"]:
        if p_key in release_archives:
            archive_patch(t, p_key, last, fmt)
    # A title tune takes what its accepted siblings get (a release archive's
    # members were settled by ``pending`` above).
    for t, cand, p_key, c_key in state["own"]:
        if ((c_key in state["accepted"] or c_key in accepted)
                and p_key not in release_archives
                and not (p_key in person_index and not cand[5])):
            out.append(_patch(t, cand[0]))
    mine = state["mine"]
    if not mine:
        return out
    # Re-judged albums of earlier passes: drop the no-op patches, clear the
    # ones no longer derived.
    derived: set = set()
    kept: list[tuple[str, dict, dict]] = []
    for item in out:
        tid, upd, _exp = item
        derived.add(tid)
        t = mine.get(tid)
        if t is None or (t.get("album") or "") != upd["album"]:
            kept.append(item)
    kept.extend((tid, header_album_back(t),
                 {"album": t.get("album"), "album_source": SOURCE_FOLDER})
                for tid, t in mine.items() if tid not in derived)
    return kept


_PAREN_RE = re.compile(r"\(([^()]*)\)")
_CREDIT_SPLIT_RE = re.compile(r"\s*(?:&|,|/|\bfeat\.?|\bvs\.?)\s*", re.IGNORECASE)


def person_keys(names) -> frozenset[str]:
    """``name_key``s of every credited person in ``names`` (artist / album
    artist strings) — each whole string, its ``&``/``,``/``/``-separated
    parts, and the real-name and handle halves of ``Real Name (Handle)``."""
    keys: set[str] = set()
    for v in set(names):
        if v:
            keys.update(name_key(p) for p in _credit_parts(v))
    keys.discard("")
    return frozenset(keys)


def _library_artist_names(store) -> list[str]:
    """Artist + album-artist names: the keys of the store's tag indexes
    (lower-cased; ``person_keys`` folds case anyway) — O(#artists), and not
    the aggregates, which recompute O(tracks) on the loop after every
    mutation (~45 ms at 263K tracks)."""
    return [*store._tag_artist, *store._tag_album_artist]


# ── Delta bookkeeping (``enrich_delta``) ─────────────────────────────────────
#
# A track's verdicts read its container's verdict (the container's chain of
# names, the scan roots, the archives holding several tunes, the format /
# Modland author / library person names), the vote of its container's PARENT
# (the tracks of every container under that parent, plus the members of the
# parent archive's generic top-level folders, which count as its non-album
# children) and the other tracks of its own container.  So the tracks are
# grouped by that parent (the "group" — the container itself when it has no
# parent), and a delta pass re-judges every group a changed track was or is
# in, every group of a container whose names a changed person name matches,
# every track of an archive whose several-tunes status flipped, and — closed
# over, both ways — the groups of the generic-folder members voting in a
# re-judged group and the archive groups a re-judged group's members vote in
# (a member's own verdict decides whether it still votes in a settle round).
# The verdicts of the other tracks cannot have moved.

def _person_key_counts(names) -> dict[str, int]:
    """``{person key: how many of the names give it}`` — its keys are
    ``person_keys(names)``."""
    counts: dict[str, int] = {}
    for n in set(names):
        for k in person_keys((n,)):
            counts[k] = counts.get(k, 0) + 1
    return counts


class _Edits:
    """Batched edits of a multimap whose values are tuples (``_FolderBook``):
    one rebuild per touched key, whatever the number of edits to it."""

    def __init__(self) -> None:
        self.add: dict[str, list] = {}
        self.drop: dict[str, set] = {}

    def put(self, k, v) -> None:
        self.add.setdefault(k, []).append(v)

    def pop(self, k, v) -> None:
        self.drop.setdefault(k, set()).add(v)

    def apply(self, m: dict) -> None:
        for k in self.add.keys() | self.drop.keys():
            drop = self.drop.get(k, ())
            cur = [x for x in m.get(k, ()) if x not in drop]
            have = set(cur)
            for x in self.add.get(k, ()):
                if x not in have:
                    cur.append(x)
                    have.add(x)
            if cur:
                m[k] = tuple(cur)
            else:
                m.pop(k, None)
        self.add, self.drop = {}, {}


class _FolderBook:
    """The per-track keys of the last pass, kept up to date by delta passes
    (see above).  ``keys``: track id → ``(group, archive it votes in as a
    generic top-level folder member | None, the name keys of its container
    chain)`` for every track the pass judges; ``groups`` / ``voters`` the
    reverse maps; ``by_name``: container-name key → groups (only grows — an
    extra group is re-judged for nothing); ``arch``: innermost archive →
    ``(retro members, members not named like it)`` (``_note_archives``),
    ``arch_of`` / ``arch_members`` / ``places`` (path, format) per member,
    ``multi`` the several-tunes archives; ``names`` / ``persons`` the library's artist names and their
    person-key counts (``_person_key_counts``).  Every value is a tuple of
    strings / ints, which the cyclic GC stops tracking (a set per group or
    archive added ~70 ms to every full collection at 270K tracks); the
    multimaps change through ``_Edits``."""

    def __init__(self, roots: frozenset[str]) -> None:
        self.roots = roots
        self.keys: dict[str, tuple] = {}
        self.groups: dict[str, tuple] = {}
        self.voters: dict[str, tuple] = {}
        self.by_name: dict[str, tuple] = {}
        self.arch: dict[str, tuple[int, int]] = {}
        self.arch_of: dict[str, tuple[str, int]] = {}
        self.arch_members: dict[str, tuple] = {}
        self.places: dict[str, tuple[str, str]] = {}
        self.multi: set[str] = set()
        self.names: set[str] = set()
        self.persons: dict[str, int] = {}

    # archives
    def add_arch(self, tid: str, path: str, fmt: str, retro: dict,
                 members: "_Edits") -> str | None:
        if "::" not in path:
            return None
        r = retro.get(fmt)
        if r is None:
            from soniqboom.core.retro import is_retro_format
            r = retro[fmt] = is_retro_format(fmt)
        if not r:
            return None
        k, _, member = path.rpartition("::")
        other = _other_member(k, member)
        self.arch_of[tid] = (k, other)
        self.places[tid] = (path, fmt)
        members.put(k, tid)
        n, o = self.arch.get(k, (0, 0))
        self.arch[k] = (n + 1, o + other)
        return k

    def drop_arch(self, tid: str, members: "_Edits") -> str | None:
        old = self.arch_of.pop(tid, None)
        if old is None:
            return None
        k, other = old
        self.places.pop(tid, None)
        members.pop(k, tid)
        n, o = self.arch[k]
        if n > 1:
            self.arch[k] = (n - 1, o - other)
        else:
            del self.arch[k]
        return k

    def several(self, k: str) -> bool:
        n, o = self.arch.get(k, (0, 0))
        return n >= 2 and o > 0

    # keys
    def link(self, tid: str, key: tuple, groups: "_Edits", voters: "_Edits",
             by_name: "_Edits") -> None:
        self.keys[tid] = key
        group, voter, names = key
        groups.put(group, tid)
        if voter is not None:
            voters.put(voter, tid)
        for n in names:
            by_name.put(n, group)                       # (``apply`` drops repeats)

    def unlink(self, tid: str, groups: "_Edits", voters: "_Edits") -> tuple | None:
        key = self.keys.pop(tid, None)
        if key is None:
            return None
        groups.pop(key[0], tid)
        if key[1] is not None:
            voters.pop(key[1], tid)
        return key

    def build(self, rows: list[tuple[str, str, str]], retro: dict, memo: dict) -> None:
        """The keys of every track — ``rows``: ``(id, path, format)`` as of
        the full pass's snapshot (``multi`` set)."""
        groups, voters, by_name = _Edits(), _Edits(), _Edits()
        for tid, path, fmt in rows:
            key = self.track_keys(path, fmt, retro, memo)
            if key is not None:
                self.link(tid, key, groups, voters, by_name)
        groups.apply(self.groups)
        voters.apply(self.voters)
        by_name.apply(self.by_name)

    def track_keys(self, path: str, fmt: str, retro: dict, memo: dict) -> tuple | None:
        """``(group, voter archive, chain name keys)`` of a track at ``path``
        in format ``fmt`` as ``collect_folder_updates`` places it, or None
        when the pass doesn't judge it by its folders (not retro, in the HVSC
        tree, in a scan root)."""
        r = retro.get(fmt)
        if r is None:
            from soniqboom.core.retro import is_retro_format
            r = retro[fmt] = is_retro_format(fmt)
        if not r:
            return None
        if _in_hvsc_tree(path, fmt):
            return None
        last = path.rsplit("::", 1)[-1].replace("\\", "/")
        if "::" not in path:
            ckey = path.rsplit("/", 1)[0]
        elif "/" not in last:
            ckey = path
        else:
            ckey = path.rsplit("::", 1)[0] + "::" + last.rsplit("/", 1)[0]
        hit = memo.get(ckey, ...)
        if hit is ...:
            hit = None
            chain = _container_chain(path, self.roots, 3, self.multi)
            if chain:
                c_key = chain[0][2]
                group = chain[1][2] if len(chain) > 1 else "\0" + c_key
                voter = c_key if chain[0][1] and "::" in path and "/" in last else None
                hit = (group, voter,
                       tuple(dict.fromkeys(name_key(clean_folder_name(c[0])) for c in chain)))
            memo[ckey] = hit
        return hit

    def delta(self, changed: dict[str, tuple | None], names_now: set[str]) -> set[str]:
        """Bring the book up to date with the changed tracks (``changed``: id →
        its ``(path, format)`` now, None when deleted) and the library's
        artist names ``names_now``; returns the ids to judge again (the
        changed tracks it holds no keys for among them).  Pure — the caller
        reads the store; run it in a thread."""
        retro: dict = {}
        regroup: set[str] = set()
        # The several-tunes archives (a flip re-places every member).
        members = _Edits()
        touched: set[str] = set()
        for tid, now in changed.items():
            k = self.drop_arch(tid, members)
            if k is not None:
                touched.add(k)
            if now is not None:
                k = self.add_arch(tid, now[0], now[1], retro, members)
                if k is not None:
                    touched.add(k)
        members.apply(self.arch_members)
        rekey = set(changed)
        for k in touched:
            now = self.several(k)
            if now != (k in self.multi):
                (self.multi.add if now else self.multi.discard)(k)
                rekey.update(self.arch_members.get(k, ()))
        # The library's person names.
        flipped: set[str] = set()
        for n, step in [(n, -1) for n in self.names - names_now] + \
                       [(n, 1) for n in names_now - self.names]:
            for k in person_keys((n,)):
                c = self.persons.get(k, 0) + step
                if c > 0:
                    self.persons[k] = c
                else:
                    self.persons.pop(k, None)
                if (c > 0) != (c - step > 0):
                    flipped.add(k)
        self.names = names_now
        for k in flipped:
            regroup.update(self.by_name.get(k, ()))
        # The changed tracks' places, before and after.
        groups, voters, by_name = _Edits(), _Edits(), _Edits()
        loners: set[str] = set()
        memo: dict = {}
        for tid in rekey:
            old = self.unlink(tid, groups, voters)
            if old is not None:
                regroup.add(old[0])
                if old[1] is not None:
                    regroup.add(old[1])
            now = changed.get(tid, ...)
            if now is ...:                              # a several-tunes archive's member
                now = self.places.get(tid)
            if now is None:
                continue
            new = self.track_keys(now[0], now[1], retro, memo)
            if new is None:
                loners.add(tid)
                continue
            self.link(tid, new, groups, voters, by_name)
            regroup.add(new[0])
            if new[1] is not None:
                regroup.add(new[1])
        groups.apply(self.groups)
        voters.apply(self.voters)
        by_name.apply(self.by_name)
        # A generic-folder member votes in its archive's group, and its own
        # verdict (which reads its own group) decides whether it is skipped
        # in a settle round there (withdrawn to its header's game): a group
        # judged again takes along the groups of the members voting in it,
        # and the archive groups its own members vote in — closed over.
        frontier = list(regroup)
        while frontier:
            g = frontier.pop()
            for u in self.voters.get(g, ()):
                gu = self.keys[u][0]
                if gu not in regroup:
                    regroup.add(gu)
                    frontier.append(gu)
            for u in self.groups.get(g, ()):
                v = self.keys[u][1]
                if v is not None and v not in regroup:
                    regroup.add(v)
                    frontier.append(v)
        ids = set(loners)
        for g in regroup:
            ids.update(self.groups.get(g, ()))
        return ids


# ── Pass orchestration ───────────────────────────────────────────────────────

_lock: asyncio.Lock | None = None
# The last pass's position in the store's enrichment change log and its inputs
# (scan roots, Modland index) — ``enrich_delta.record``; None: the next pass
# judges every track.
_last_seq: tuple | None = None
_book: _FolderBook | None = None      # its bookkeeping (see ``_FolderBook``)
_status: dict = {"last_apply": None, "last_revert": None}


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def enabled() -> bool:
    from soniqboom.core.store import get_store
    return bool(get_store().get_config(CONFIG_KEY, False))


def status() -> dict:
    return {"enabled": enabled(), **_status}


async def apply_folder_albums(*, force: bool = False) -> dict:
    """Fill empty retro albums from folder names (and re-judge the ones an
    earlier pass stamped).  Must run on the loop.

    A no-op unless the setting is on.  After its first run a pass judges only
    the tracks written since the last one and the tracks whose verdict reads
    them (``_FolderBook``, ``enrich_delta``) — skipped when there are none —
    and every track when the scan roots or the Modland index changed.
    ``force`` judges every track and bypasses the setting — the settings
    toggle uses it right after switching the option on."""
    global _last_seq, _book
    from soniqboom.core.store import get_store
    if not force and not enabled():
        return {"updated": 0, "skipped": "disabled"}
    async with _get_lock():
        store = get_store()
        loop = asyncio.get_running_loop()
        from soniqboom.core import enrich_delta, scene_metadata
        roots = frozenset(sd.get("path", "").rstrip("/")
                          for sd in store.list_scan_dirs() if sd.get("path"))
        inputs = (roots, scene_metadata._index_sig())
        last = _last_seq
        changed = enrich_delta.changes(store, last, inputs, force=force)
        if changed is not None and not changed:
            return {"updated": 0, "skipped": "unchanged"}
        # The bookkeeping is handed back only when the pass completes: a pass
        # that ends early leaves the next one to judge every track.
        book, _book, _last_seq = _book, None, None
        keys_ok = True
        try:
            fmt_ml, author_ml = await loop.run_in_executor(
                None, scene_metadata.modland_name_keys)
        except Exception:                               # noqa: BLE001
            fmt_ml, author_ml = frozenset(), frozenset()
            keys_ok = False                             # no delta may build on this pass
        # The snapshot, the changes and the change-log position they
        # correspond to are taken together (no await between): any write
        # after this point must reach the NEXT pass, even though our commit
        # writes too.
        if changed is not None:
            changed = enrich_delta.changes(store, last, inputs)
        snap = store.enrich_cursor()
        tracks: list[dict] | None = None
        if changed is not None and book is not None and book.roots == roots:
            # The changed tracks' places and the person names as of the
            # snapshot; the book follows them off the loop.
            now: dict = {}
            for tid in changed:
                t = store.get_track(tid)
                now[tid] = None if t is None else (t.get("path") or "", t.get("format") or "")
            names_now = set(_library_artist_names(store))
            ids = await loop.run_in_executor(None, book.delta, now, names_now)
            if len(ids) <= enrich_delta.limit(store):
                tracks = enrich_delta.tracks_of(store, ids)
                format_keys = await loop.run_in_executor(
                    None, lambda: _format_keys_static() | fmt_ml)
        if tracks is None:
            book = _FolderBook(roots)
            tracks = store.all_tracks()                 # snapshot of refs
            # The places as of the snapshot: an edit in place later (a repair
            # re-reading the format) is then the next delta's, which re-judges
            # the place it had here too.
            rows = [(t["id"], t.get("path") or "", t.get("format") or "") for t in tracks]
            names = _library_artist_names(store)        # tag-index keys
            # Pure string work on immutable inputs → off the loop.
            format_keys, persons = await loop.run_in_executor(
                None, lambda: (_format_keys_static() | fmt_ml, _person_key_counts(names)))
            book.names, book.persons = set(names), persons
            # The archives holding several tunes (never single-file wrappers).
            retro_cache: dict = {}
            members = _Edits()
            t0 = time.perf_counter()
            for i in range(0, len(rows), _GUARD_CHUNK):
                for tid, path, fmt in rows[i:i + _GUARD_CHUNK]:
                    book.add_arch(tid, path, fmt, retro_cache, members)
                if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
                    await asyncio.sleep(_YIELD_SEC)
                    t0 = time.perf_counter()
            members.apply(book.arch_members)
            book.multi = {k for k in book.arch if book.several(k)}
            full = True
        else:
            full = False
        items = await _judge_tracks(tracks, roots=roots, format_keys=format_keys,
                                    author_keys=author_ml, artist_keys=book.persons.keys(),
                                    multi=frozenset(book.multi))
        if full:
            # Every judged track's place, for the delta passes after this one
            # (pure string work on the snapshot → off the loop).
            await loop.run_in_executor(None, book.build, rows, {}, {})
        if not force and not enabled():
            return {"updated": 0, "skipped": "disabled"}   # switched off meanwhile
        written: list[str] = []
        _patches, albums, own_bumps = await _commit_album_updates(items, written=written)
        updated = len(set(written))                     # tracks (an album and a name: one)
        # Past our own writes when nothing else wrote meanwhile; else (a scan
        # adding tracks during the collect) the next pass re-reads what was
        # written since the snapshot.
        if keys_ok:
            _book, _last_seq = book, enrich_delta.record(store, snap, own_bumps, inputs)
        if updated:
            await refresh_album_caches(written)
        # ``updated`` counts every track written, ``albums`` only the album
        # changes (the rest are per-source game names).
        res = {"updated": updated, "albums": albums, "candidates": len(items)}
        _status["last_apply"] = res
        if albums:
            log.info("Folder albums: %d retro album(s) set or changed from their "
                     "folder name", albums)
        if updated > albums:
            log.info("Folder albums: %d retro track(s) got a new folder game name",
                     updated - albums)
        return res


async def _judge_tracks(tracks: list[dict], *, roots: frozenset[str],
                        format_keys, author_keys, artist_keys,
                        multi: frozenset[str]) -> list:
    """The patches of the folder pass for ``tracks`` — the whole library, or
    whole groups of it (``_FolderBook``): albums (``collect_folder_updates``,
    settled over ``_SETTLE_ROUNDS``) and the per-source game names
    (``game_by_folder``).  Yields to the loop every ``_YIELD_BUDGET_S``."""
    retro_cache: dict = {}
    dir_cache: dict = {}                                # one memo across chunks

    async def collect(state: dict, every_track: bool, skip: set[str]) -> None:
        t0 = time.perf_counter()
        for i in range(0, len(tracks), _SCAN_CHUNK):
            chunk = tracks[i:i + _SCAN_CHUNK]
            if skip:
                chunk = [t for t in chunk if t["id"] not in skip]
            collect_folder_updates(
                chunk, roots=roots, format_keys=format_keys,
                author_keys=author_keys, artist_keys=artist_keys,
                retro_cache=retro_cache, dir_cache=dir_cache, state=state,
                every_track=every_track, multi=multi)
            if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
                await asyncio.sleep(_YIELD_SEC)     # keep requests flowing
                t0 = time.perf_counter()
        await asyncio.sleep(_YIELD_SEC)             # the vote: ~10 ms at 263K

    # The folder name of EVERY retro track (its per-source game name —
    # also where the album is another source's or the user's): a collect
    # of its own, so it can't move the album verdicts below — only a
    # title tune follows its folder's other tracks (``accepted``).
    name_state = new_folder_state()
    await collect(name_state, True, set())
    named = {tid: upd["album"] for tid, upd, _e in finalize_folder_updates(name_state)}
    # The albums.  A stamped album withdrawn to the file header's name
    # takes its track out of the vote next time (it has an album of its
    # own then), so the vote is taken again without such tracks until
    # none is left — the result is what the next pass sees.
    items: list = []
    skip: set[str] = set()
    for rnd in range(_SETTLE_ROUNDS):
        state = new_folder_state()
        await collect(state, False, skip)
        got = finalize_folder_updates(state, name_state["accepted"])
        back = [it for it in got
                if "album" in it[1] and it[1].get("album_source") != SOURCE_FOLDER
                and (it[1].get("album") or "").strip()]
        if not back or rnd == _SETTLE_ROUNDS - 1:
            items.extend(got)
            break
        items.extend(back)
        skip.update(it[0] for it in back)
    # A track the album pass itself judges (no album of its own, or one it
    # stamped) takes ITS verdict — the album it gets or keeps, or none when
    # refused — never the every-track guess (the siblings that vote there
    # differ); the others (another source's album, the user's — and one
    # whose withdrawn album gives the header's name back, as the next pass
    # will see it) take the every-track name.
    # A folder the album pass refused for its own tracks gives no name to
    # its other tracks either (the two votes must agree).
    judged: dict[str, str | None] = {}
    restored: set[str] = set()
    for tid, upd, _e in items:
        if "album" not in upd:
            continue
        if upd.get("album_source") == SOURCE_FOLDER:
            judged[tid] = upd.get("album") or None
        elif (upd.get("album") or "").strip():
            restored.add(tid)               # withdrawn: the header's name back
        else:
            judged[tid] = None              # withdrawn, no name left
    own_want: dict[str, str | None] = {}
    refused: set[str] = set()
    accepted: set[str] = set()
    t0 = time.perf_counter()
    for i in range(0, len(tracks), _SCAN_CHUNK):
        for t in tracks[i:i + _SCAN_CHUNK]:
            tid = t["id"]
            if tid in restored or album_edit_locked(t) or not (
                    t.get("album_source") == SOURCE_FOLDER
                    or not (t.get("album") or "").strip()):
                continue
            want = judged[tid] if tid in judged else (
                (t.get("album") or None) if t.get("album_source") == SOURCE_FOLDER
                else None)
            own_want[tid] = want
            if tid in named or want:
                (accepted if want else refused).add(_container_key(t, roots, multi))
        if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
            await asyncio.sleep(_YIELD_SEC)
            t0 = time.perf_counter()
    refused -= accepted
    for i in range(0, len(tracks), _SCAN_CHUNK):
        for t in tracks[i:i + _SCAN_CHUNK]:
            tid = t["id"]
            if tid in own_want:
                want = own_want[tid]
            else:
                want = named.get(tid)
                if want and _container_key(t, roots, multi) in refused:
                    want = None
            if (t.get("game_by_folder") or None) != want:
                items.append((tid, {"game_by_folder": want}, None))
        if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
            await asyncio.sleep(_YIELD_SEC)
            t0 = time.perf_counter()
    return items


async def revert_album_source(source: str) -> int:
    """Withdraw every album stamped with ``album_source == source`` (never a
    user-edited one) — the file header's name comes back, else it is cleared
    (``header_album_back``) — and the per-source game names of a folder /
    Modland file-name source.  Used when the owning setting is switched off.
    Returns the number of albums withdrawn.

    Crash-safe: ``source`` is recorded in the ``REVERT_PENDING_KEY`` config
    marker (AOF, before the first cleared album; the settings endpoint records
    it together with the option) and removed only once every album is
    cleared, so a restart in between resumes the revert
    (``resume_pending_revert``) — while the option is off nothing else would
    remove the leftover albums."""
    from soniqboom.core.store import get_store
    mark_revert_pending(source)
    async with _get_lock():
        store = get_store()
        tracks = store.all_tracks()
        items: list = []
        t0 = time.perf_counter()
        slot = {SOURCE_FOLDER: "game_by_folder",
                SOURCE_MODLAND_FILENAME: "game_by_modland_filename"}.get(source)
        for i in range(0, len(tracks), _REVERT_SCAN_CHUNK):
            items.extend(
                (t["id"], header_album_back(t),
                 {"album": t.get("album"), "album_source": source})
                for t in tracks[i:i + _REVERT_SCAN_CHUNK]
                if t.get("album_source") == source and not album_edit_locked(t))
            if slot:                            # its per-source game names go too
                items.extend((t["id"], {slot: None}, None)
                             for t in tracks[i:i + _REVERT_SCAN_CHUNK] if t.get(slot))
            if time.perf_counter() - t0 >= _YIELD_BUDGET_S:
                await asyncio.sleep(_YIELD_SEC)
                t0 = time.perf_counter()
        written: list[str] = []
        _patches, albums = await commit_album_updates(items, written=written)
        _clear_revert_pending(source)
        updated = len(set(written))                     # tracks, incl. the ones that lost only a name
        if updated:
            await refresh_album_caches(written)
            log.info("Reverted %d album(s) with source %r", albums, source)
        global _last_seq
        _last_seq = None
        _status["last_revert"] = {"source": source, "cleared": albums, "updated": updated}
        return albums


def _pending_reverts(store) -> list[str]:
    v = store.get_config(REVERT_PENDING_KEY)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def mark_revert_pending(source: str) -> None:
    """Record that ``source``'s albums are being reverted (see
    ``revert_album_source``).  Idempotent; no write when already recorded."""
    from soniqboom.core.store import get_store
    store = get_store()
    pending = _pending_reverts(store)
    if source not in pending:
        store.set_config(REVERT_PENDING_KEY, [*pending, source])


def _clear_revert_pending(source: str) -> None:
    from soniqboom.core.store import get_store
    store = get_store()
    pending = _pending_reverts(store)
    if source in pending:
        store.set_config(REVERT_PENDING_KEY, [x for x in pending if x != source])


def _source_option_on(source: str) -> bool:
    """Whether the option owning ``source``'s albums is switched on."""
    if source == SOURCE_FOLDER:
        return enabled()
    if source == SOURCE_MODLAND_FILENAME:
        from soniqboom.core import scene_metadata
        return scene_metadata.filename_game_enabled()
    return False


def resume_pending_revert() -> bool:
    """Resume the album reverts a restart interrupted (config marker
    ``REVERT_PENDING_KEY``); called once the library has loaded.  One O(1)
    config read when there is nothing to do.  A source whose option was
    switched back on meanwhile is just dropped from the marker (the next pass
    re-judges those albums anyway).  Waits out a running scan.  Returns True
    when a revert was scheduled."""
    from soniqboom.core.store import get_store
    store = get_store()
    pending = _pending_reverts(store)
    if not pending:
        return False
    for source in pending:
        if _source_option_on(source):
            _clear_revert_pending(source)
    if not _pending_reverts(store):
        return False

    async def _run() -> None:
        from soniqboom.core import scanner
        while scanner.is_scanning():
            await asyncio.sleep(_AUTOAPPLY_SETTLE_S)
        for source in _pending_reverts(store):
            try:
                if _source_option_on(source):           # switched on meanwhile
                    _clear_revert_pending(source)
                    continue
                n = await revert_album_source(source)
                log.info("Resumed an interrupted album revert (%s): %d album(s) "
                         "cleared", source, n)
            except Exception:                           # noqa: BLE001
                log.warning("Resuming the %s album revert failed", source,
                            exc_info=True)

    try:
        t = asyncio.get_running_loop().create_task(_run(), name="album-revert-resume")
    except RuntimeError:
        return False                                    # no running loop
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return True


# ── Post-scan hook (coalescing, like the Demozoo auto-apply) ─────────────────

_tasks: set = set()
_pending = False
_running = False


def schedule_after_scan() -> None:
    """Called by the scanner when a scan finishes.  No-op while the setting is
    off; otherwise marks the library dirty and makes sure one coalescing runner
    is draining it (a burst of folder-watch scans folds into few passes)."""
    global _pending, _running
    try:
        if not enabled():
            return
    except Exception:                                   # noqa: BLE001
        return
    _pending = True
    if _running:
        return
    _running = True

    async def _run() -> None:
        global _pending, _running
        try:
            while _pending:
                _pending = False
                try:
                    from soniqboom.core import scanner
                    if scanner.is_scanning():
                        # Another scan is still running; its own completion
                        # re-triggers this hook, so skip rather than work on a
                        # half-written library.
                        break
                    await apply_folder_albums()
                except Exception:                       # noqa: BLE001
                    log.debug("Post-scan folder-album pass failed", exc_info=True)
                if _pending:
                    await asyncio.sleep(_AUTOAPPLY_SETTLE_S)
        finally:
            _running = False

    try:
        t = asyncio.get_running_loop().create_task(_run(), name="folder-album-pass")
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    except RuntimeError:
        _running = False                                # no running loop
