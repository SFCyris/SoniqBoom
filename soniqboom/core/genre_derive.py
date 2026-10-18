# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Derived genres for retro tracks whose files name no genre.

Retro files (SID, tracker modules, Amiga custom players, console rips, AdLib,
MIDI …) carry no genre tag — the extractor only stamps format words
("Chiptune", "C64", "Tracker", "Module", "Amiga", "Atari ST", "Game Rip",
"MIDI").  What SoniqBoom knows about such a track is turned into genres here,
written to ``TrackMeta.genre_derived`` (never into ``genre``), and every
genre surface lists ``store.track_genres`` = derived genres first, then the
track's own (the web Genres views, genre filters and searches, the Subsonic
``genre`` / ``genres[]`` of a song, getGenres, getSongsByGenre, getAlbumList
byGenre, getRandomSongs).

Mapping — retro datum → derived genre → where Subsonic clients see it
(``genre`` = the first genre, every client; ``genres[]`` = all of them,
OpenSubsonic clients; the other columns live in their own fields):

    ===========================================  ==========================
    retro datum                                  derived genre
    ===========================================  ==========================
    a game name (any game source: file tag,      ``Game`` (+ the game's name)
    Modland, Demozoo, song database, Modland
    file name, archive name, folder name, a
    typed game); a console-rip format (SPC,
    NSF, GBS, VGM, GYM, HES, KSS, PSF family);
    an HVSC ``GAMES`` / ``games`` folder;
    Demozoo: used by a same-year Game
    Demozoo: used by a same-year demo / intro;   ``Demo`` + ``Scene``
    an HVSC ``DEMOS`` / ``demos`` / ``intros``
    folder
    Demozoo: used by a same-year cracktro; a     ``Cracktro`` + ``Scene``
    ``cracktros`` folder; "crack intro" title
    Demozoo: in a same-year music disk; a        ``Music Disk`` + ``Scene``
    ``musicdisks`` folder
    Demozoo: in a same-year diskmag; ``mags``    ``Diskmag`` + ``Scene``
    a Demozoo party competition entry;           ``Scene`` (+ the party)
    ``parties`` / ``compos`` / ``demoscene``
    folders; a tracker / AHX module whose
    composer has a Demozoo group and nothing
    else is known
    the format's platform                        ``SID``, ``Amiga``, ``Atari
                                                 ST``, ``NES``, ``SNES``,
                                                 ``Game Boy`` … (see
                                                 ``platform_genre``)
    the composer's Demozoo group(s)              the group names
    an HVSC ``GAMES`` tune's file name           the game's name
    ===========================================  ==========================

The names (game, groups, party) are genres only while the setting
``CONFIG_KEY`` is on (default on); the category and platform genres always
are.  The rest of the retro data has its own Subsonic fields, not genres: the
game is the album (``album_source``), the composer the artist (and a
``composer`` contributor), the publisher / label the album's
``recordLabels``.

Rules: derived genres are FILL-ONLY — a track with a real genre (any genre
value that is not one of the extractor's format words or this vocabulary, or
a genre the user typed, ``user_edited``) gets none, and an existing set is
withdrawn.  A non-retro track never gets any.  A retro track nothing is known
about gets just its platform genre (or nothing).  The pass recomputes from
the stored fields alone (a pure function per track, ``derive``), so a change
of any source — a Modland / Demozoo / song-database apply or withdrawal, a
rescan, a user edit, the setting — moves the genres with it.

The pass (``apply_derived_genres``) runs after scans and enrichment passes
(``schedule``; the store's enrichment change log calls ``poke`` on every
write the pass reads, debounced), on start-up, and forced on a setting
change.  After its first run it re-derives only the tracks written since
(``core/enrich_delta``).  It writes through the store's batch update (AOF,
``_track_rev`` → the Subsonic song memo, ``_catalog_seq`` → the catalogue).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

log = logging.getLogger(__name__)

CONFIG_KEY = "genre_names_in_genres"        # setting: on by default
FIELD = "genre_derived"
# Bump when the derivation rules change: the next pass re-derives everything.
RULES_VERSION = 1

# ── Vocabulary ────────────────────────────────────────────────────────────────
GAME = "Game"
DEMO = "Demo"
CRACKTRO = "Cracktro"
MUSIC_DISK = "Music Disk"
DISKMAG = "Diskmag"
SCENE = "Scene"
CATEGORIES = (GAME, DEMO, CRACKTRO, MUSIC_DISK, DISKMAG, SCENE)
# Scene productions: each implies the umbrella ``Scene``.
_SCENE_KINDS = (DEMO, CRACKTRO, MUSIC_DISK, DISKMAG)

# Format → platform genre, for formats whose name alone tells.
_FORMAT_PLATFORM = {
    "SID": "SID",
    "NSF": "NES", "NSFe": "NES",
    "SPC": "SNES",
    "GBS": "Game Boy",
    "GYM": "Mega Drive",
    "HES": "PC Engine",
    "KSS": "MSX",
    "AY": "ZX Spectrum",
    "SAP": "Atari 8-bit",
    "PSF": "PlayStation", "PSF2": "PlayStation 2", "USF": "Nintendo 64",
    "GSF": "Game Boy Advance", "2SF": "Nintendo DS", "NCSF": "Nintendo DS",
    "SSF": "Saturn", "DSF (Dreamcast)": "Dreamcast",
    "SNDH": "Atari ST", "YM": "Atari ST", "SC68": "Atari ST",
    "AHX": "Amiga", "HivelyTracker": "Amiga",
    # Amiga-born sample trackers (PC trackers — S3M, XM, IT … — name no
    # platform).
    "ProTracker": "Amiga", "OctaMED": "Amiga", "SoundFX": "Amiga",
    "Oktalyzer": "Amiga", "DigiBooster Pro": "Amiga", "Face The Music": "Amiga",
    "MIDI": "MIDI", "General MIDI": "MIDI",
}
_FAMILY_PLATFORM = {"adlib": "AdLib", "atari": "Atari ST", "sid": "SID", "midi": "MIDI",
                    "ahx": "Amiga"}
# uade players of Atari ST / PC music among the Amiga formats (as
# ``game_titles.platforms_for``).
_ATARI_PLAYER_RE = re.compile(r"\bST\b|\bPSG\b|YM[- ]?2149")
_DOS_PLAYER_RE = re.compile(r"\bAGI\b")

# Console / handheld rips: the music of a game.
_GAME_RIP_FAMILIES = frozenset({"nes", "snes", "gameboy", "genesis", "pce", "vgm", "psf"})
_GAME_RIP_FORMATS = frozenset({"KSS"})

# The extractor's format words (``core/metadata.py``): not a genre the file
# names.  With the derived vocabulary, the words a "real" genre is not.
_EXTRACTOR_GENRES = frozenset({"chiptune", "c64", "midi", "tracker", "module", "amiga",
                               "atari st", "game rip"})
_VOCAB = frozenset(w.casefold() for w in (*CATEGORIES, *_FORMAT_PLATFORM.values(),
                                         *_FAMILY_PLATFORM.values(), "PC"))
_NOT_REAL = _EXTRACTOR_GENRES | _VOCAB

# Folder (or archive) names below the scan root that say what a tune is,
# compared cleaned (``_seg_key``).  HVSC's GAMES / DEMOS are among them;
# MUSICIANS says nothing (it holds game and scene tunes alike).
_DIR_CATEGORY = {
    **dict.fromkeys(("games", "game music", "game rips", "gamerips", "game soundtracks",
                     "video game music"), GAME),
    **dict.fromkeys(("demos", "megademos", "intros", "trackmos"), DEMO),
    **dict.fromkeys(("cracktros", "cracktro", "crack intros", "crackintros", "crack intro"),
                    CRACKTRO),
    **dict.fromkeys(("musicdisks", "musicdisk", "music disks", "music disk", "musicdiscs",
                     "music discs"), MUSIC_DISK),
    **dict.fromkeys(("diskmags", "diskmag", "mags"), DISKMAG),
    **dict.fromkeys(("demoscene", "scene", "parties", "compos", "competitions"), SCENE),
}
_SEG_SPLIT_RE = re.compile(r"::|[\\/]")
_SEG_CLEAN_RE = re.compile(r"[\s_.\-]+")
_ARCHIVE_EXT_RE = re.compile(
    r"(\.tar)?\.(zip|lha|lzh|lzx|7z|rar|tar|tgz|gz|bz2|xz|adf|adz|dms|d64|d71|d81)$",
    re.IGNORECASE)
# An HVSC ``GAMES`` tune: its file name is its game (as ``game_titles``).
_HVSC_GAME_RE = re.compile(r"/GAMES/[^/]+/([^/:]+)\.sid$")
_CRACKTRO_TITLE_RE = re.compile(r"\bcrack(?:[\s_-]?intro|tro)s?\b", re.IGNORECASE)
_NAME_MAX = 120
_MAX_GROUPS = 4


def platform_genre(fmt: str | None) -> str | None:
    """The platform genre of a retro ``format`` ("SID", "Amiga", "NES" …),
    or None (a PC tracker, a VGM log — many systems —, a non-retro format)."""
    if not fmt:
        return None
    hit = _FORMAT_PLATFORM.get(fmt)
    if hit:
        return hit
    from soniqboom.core.retro import chip_family
    fam = chip_family(fmt)
    if fam == "paula":
        if _ATARI_PLAYER_RE.search(fmt):
            return "Atari ST"
        if _DOS_PLAYER_RE.search(fmt):
            return "PC"
        return "Amiga"
    return _FAMILY_PLATFORM.get(fam) if fam else None


def has_real_genre(t: dict) -> bool:
    """Does track ``t`` name a genre of its own — a genre the user typed (also
    a cleared one), or any value that is no extractor format word and no
    derived-genre word?"""
    if "genre" in (t.get("user_edited") or ()):
        return True
    for g in t.get("genre") or ():
        if isinstance(g, str) and g.strip() and g.strip().casefold() not in _NOT_REAL:
            return True
    return False


def _seg_key(seg: str) -> str:
    seg = _ARCHIVE_EXT_RE.sub("", seg.strip().rstrip(":"))
    return _SEG_CLEAN_RE.sub(" ", seg).strip().casefold()


def _below_root(path: str, roots: tuple[str, ...]) -> str:
    """``path`` relative to its scan root (``roots``: longest first, as
    ``subsonic_index.scan_roots``), archive ``::`` as ``/``; the whole path
    when no root holds it."""
    from soniqboom.core.subsonic_index import below_root, root_of
    root = root_of(path, roots) if roots else ""
    return below_root(path, root) if root else path


def path_categories(path: str, roots: tuple[str, ...] = ()) -> set[str]:
    """The categories the folders / archives holding ``path`` name, below its
    scan root (the root's own name and anything above it — a share's name —
    never count).  The file's own name is not looked at."""
    if not path:
        return set()
    return _rel_categories(_below_root(path, roots))


def _rel_categories(rel: str) -> set[str]:
    segs = _SEG_SPLIT_RE.split(rel)[:-1]          # the folders and archives only
    out: set[str] = set()
    for seg in segs:
        if seg:
            c = _DIR_CATEGORY.get(_seg_key(seg))
            if c:
                out.add(c)
    return out


def _names(t: dict, game: str, party: str) -> list[str]:
    """The game, the composer's scene groups and the party, as genres."""
    out: list[str] = []
    if game:
        out.append(game)
    sg = t.get("scene_group")
    groups = sg.split(" • ") if isinstance(sg, str) else []
    out.extend(g for g in groups[:_MAX_GROUPS] if g)
    if party:
        out.append(party)
    clean: list[str] = []
    for n in out:
        n = " ".join(n.split())[:_NAME_MAX].strip()
        if n and n.casefold() not in _NOT_REAL:
            clean.append(n)
    return clean


def derive(t: dict, *, names: bool = True, roots: tuple[str, ...] = ()) -> list[str] | None:
    """Track ``t``'s derived genres (see the module docstring), or None: a
    non-retro track, a track with a real genre, or nothing to say.  Pure —
    reads only ``t``'s stored fields."""
    from soniqboom.core.retro import chip_family, is_retro_format
    fmt = t.get("format")
    if not is_retro_format(fmt) or has_real_genre(t):
        return None
    fam = chip_family(fmt)
    game = (t.get("game") or "").strip() if isinstance(t.get("game"), str) else ""
    party = t.get("scene_party")
    party = party.strip() if isinstance(party, str) else ""
    use = t.get("scene_use") or ()
    if not isinstance(use, (list, tuple)):
        use = ()
    path = t.get("path") or ""
    rel = "/" + _below_root(path, roots) if path else ""
    pcats = _rel_categories(rel) if rel else set()
    cats: list[str] = []
    if game or fam in _GAME_RIP_FAMILIES or fmt in _GAME_RIP_FORMATS or GAME in use \
            or GAME in pcats:
        cats.append(GAME)
    for c in _SCENE_KINDS:
        if c in use or c in pcats:
            cats.append(c)
    title = t.get("title")
    if CRACKTRO not in cats and isinstance(title, str) and _CRACKTRO_TITLE_RE.search(title):
        cats.append(CRACKTRO)
    if (party or SCENE in pcats or any(c in cats for c in _SCENE_KINDS)
            or (not cats and t.get("scene_group") and fam in ("tracker", "ahx"))):
        cats.append(SCENE)
    out = cats
    plat = platform_genre(fmt)
    if plat:
        out.append(plat)
    if names:
        if not game and fam == "sid":
            m = _HVSC_GAME_RE.search(rel)
            if m:
                game = m.group(1).replace("_", " ").strip()
        seen = {x.casefold() for x in out}
        for n in _names(t, game, party):
            if n.casefold() not in seen:
                seen.add(n.casefold())
                out.append(n)
    return out or None


# ── The pass ──────────────────────────────────────────────────────────────────

_CHUNK = 500                 # tracks per store write
_SLICE_SEC = 0.006           # derivation work per event-loop turn
_POKE_DELAY_S = 2.0          # debounce of ``poke``
_SCAN_POLL_S = 1.0

_pass_lock: asyncio.Lock | None = None
_pass_lock_loop = None
_last_sig: tuple | None = None
_last_result: dict | None = None
_pending = False
_running = False
_poke_handle = None
_bg_tasks: set = set()


def names_enabled() -> bool:
    try:
        from soniqboom.core.store import get_store
        return bool(get_store().get_config(CONFIG_KEY, True))
    except Exception:                            # noqa: BLE001
        return True


def _get_pass_lock() -> asyncio.Lock:
    global _pass_lock, _pass_lock_loop
    loop = asyncio.get_running_loop()
    if _pass_lock is None or _pass_lock_loop is not loop:
        _pass_lock, _pass_lock_loop = asyncio.Lock(), loop
    return _pass_lock


async def apply_derived_genres(force: bool = False) -> dict:
    """Write ``genre_derived`` where ``derive`` says something else than the
    stored value (None withdraws).  One pass at a time; after a first run
    only the tracks written since (``enrich_delta``) — ``{"skipped":
    "unchanged"}`` when there are none — and everything when ``force``d or
    when the setting, the scan roots or the rules changed.  Runs on the event
    loop in time slices; each track is derived from its live dict right
    before its write, so a write landing meanwhile is never undone.  Returns
    ``{"updated": n, "derived": m, "seconds": s}`` (``derived``: tracks with
    derived genres after the pass, counted over what it looked at on a full
    pass, a delta pass adjusts the previous count; None before a full pass)."""
    global _last_sig, _last_result
    from soniqboom.core import enrich_delta
    from soniqboom.core.store import get_store
    from soniqboom.core.subsonic_index import scan_roots
    async with _get_pass_lock():
        store = get_store()
        on = names_enabled()
        roots = scan_roots(store)
        inputs = (on, roots, RULES_VERSION)
        changed = enrich_delta.changes(store, _last_sig, inputs, force=force)
        if changed is not None and not changed:
            return {"skipped": "unchanged"}
        t0 = time.perf_counter()
        snap = store.enrich_cursor()
        full = changed is None
        ids = list(store._tracks) if full else sorted(changed)
        get = store._tracks.get
        written: list[str] = []
        updated = own = 0
        derived = 0              # full pass: tracks with derived genres
        moved = 0                # delta pass: change of that count
        stale = False
        i, n = 0, len(ids)
        try:
            while i < n:
                pending: list[tuple[str, dict]] = []
                s0 = time.perf_counter()
                while i < n and len(pending) < _CHUNK and time.perf_counter() - s0 < _SLICE_SEC:
                    for tid in ids[i:i + 200]:
                        t = get(tid)
                        if t is None:
                            continue
                        want = derive(t, names=on, roots=roots)
                        if want:
                            derived += 1
                        cur = t.get(FIELD) or None
                        if cur != want:
                            pending.append((tid, {FIELD: want}))
                            moved += bool(want) - bool(cur)
                    i += 200
                if pending:
                    if names_enabled() != on:
                        stale = True             # switched meanwhile: the next pass decides
                        break
                    # Written in the same loop turn as derived from the live
                    # dicts: no other write can land in between.
                    before = store._enrich_seq
                    updated += store.update_track_fields_batch(pending)
                    own += store._enrich_seq - before
                    written.extend(tid for tid, _ in pending)
                await asyncio.sleep(0)
        except BaseException:
            _last_sig = None
            raise
        prev = (_last_result or {}).get("derived")
        if full:
            total = derived
        elif isinstance(prev, int):
            total = max(0, prev + moved)
        else:
            total = None
        _last_result = {"updated": updated, "derived": total,
                        "seconds": round(time.perf_counter() - t0, 2)}
        if stale:
            _last_sig = None
            schedule()
        else:
            if enrich_delta.foreign_writes(store, snap, own):
                schedule()                       # another write landed meanwhile
            _last_sig = enrich_delta.record(store, snap, own, inputs)
    if updated:
        log.info("Derived genres: %d track(s) updated (%s with derived genres)", updated,
                 _last_result["derived"])
        try:
            from soniqboom.core.folder_album import refresh_album_caches
            await refresh_album_caches(written)
        except Exception:                        # noqa: BLE001
            log.debug("cache refresh after the derived-genre pass failed", exc_info=True)
    return _last_result


def rederive_now(ids) -> int:
    """Re-derive the genres of tracks ``ids`` now (on the loop) — after a
    user's edit, so the answer to it already shows them.  Returns the number
    of tracks written."""
    from soniqboom.core.store import get_store
    from soniqboom.core.subsonic_index import scan_roots
    store = get_store()
    on, roots = names_enabled(), scan_roots(store)
    items = []
    moved = 0
    for tid in ids:
        t = store.get_track(tid)
        if t is not None:
            want = derive(t, names=on, roots=roots)
            cur = t.get(FIELD) or None
            if cur != want:
                items.append((tid, {FIELD: want}))
                moved += bool(want) - bool(cur)
    if not items:
        return 0
    n = store.update_track_fields_batch(items)
    if _last_result and isinstance(_last_result.get("derived"), int):
        _last_result["derived"] = max(0, _last_result["derived"] + moved)
    return n


def schedule() -> None:
    """Run ``apply_derived_genres`` in the background (after a scan, an
    enrichment pass, on start-up, after a ``poke``); calls while it runs
    coalesce into one more run.  A running scan is waited out first."""
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
                await _scan_idle()
                try:
                    await apply_derived_genres()
                except Exception:                # noqa: BLE001
                    log.exception("derived-genre pass failed")
        finally:
            _running = False
    task = loop.create_task(_run(), name="derived-genres")
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def poke() -> None:
    """The store's enrichment listener (``TrackStore.set_enrich_listener``):
    a write the pass reads happened — schedule a pass ``_POKE_DELAY_S`` later
    (one timer for a burst of writes).  Cheap; never raises."""
    global _poke_handle
    if _poke_handle is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _fire() -> None:
        global _poke_handle
        _poke_handle = None
        schedule()
    _poke_handle = loop.call_later(_POKE_DELAY_S, _fire)


def install() -> None:
    """Follow the store's writes (called once at start-up, after the load)."""
    from soniqboom.core.store import get_store
    get_store().set_enrich_listener(poke)


async def _scan_idle() -> None:
    """Wait until no scan and no catalogue build (``modland_catalog``) runs —
    their writes would only re-trigger the pass."""
    try:
        from soniqboom.core import modland_catalog, online_catalog, scanner
        while scanner.is_scanning() or modland_catalog.busy() or online_catalog.any_busy():
            await asyncio.sleep(_SCAN_POLL_S)
    except Exception:                            # noqa: BLE001
        log.debug("scan state unavailable", exc_info=True)


def pass_busy() -> bool:
    return _running or _pending


def status() -> dict:
    """The setting, whether a pass is due, and the last pass's result."""
    return {"names": names_enabled(), "busy": pass_busy(), "last": _last_result}
