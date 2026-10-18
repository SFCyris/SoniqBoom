# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Modland catalogue: Modland's whole module archive in the library, built
from its index file without downloading a single tune.

Modland (ftp.modland.com) publishes ``allmods.zip`` — ``allmods.txt``, one
``<size>\\t<Format>/<Composer>/[<Game>/]<file>`` line per file under
``/pub/modules`` (~516k files).  With the setting on (Admin → Library →
External sources → "Modland catalogue", default off) SoniqBoom downloads that
index (~6 MB) and adds ONE library track per playable music file:

* path ``ftp://ftp.modland.com/pub/modules:/<rel>`` and id = ``uuid5`` of it —
  exactly what a scanned FTP share would give the same file, so plays,
  ratings, stars and playlist entries keyed by id come back when the catalogue
  is switched off and on again;
* up front, from the path alone: title (the file name — prefix-form Amiga
  names such as ``mdat.foo`` give ``foo``), composer (the Modland author dir,
  ``scene_metadata.modland_credit``; HVSC's MUSICIANS folder name), format
  (the extension / uade token, ``metadata.FORMAT_NAMES``), the game / album
  (``scene_metadata.modland_game_for`` — the game dir or the ``<game>-<part>``
  file name), file size, ``scene_path`` and the derived retro genres
  (``genre_derive.derive``); length 0 ("—") until the first play;
* catalogue tracks sort as the OLDEST in "Date added" (``added_at`` 1) when the
  catalogue is first built, so they never swamp Recently added; tunes Modland
  adds later carry the day they appeared.

Only music SoniqBoom plays is listed (``metadata.is_supported_music_name`` —
the scanner's own discovery rule): companion halves (TFMX ``smpl.*``,
``*.psflib``, instrument files), unsupported formats and the archives are
not tracks (they stay in the index, so a play can fetch the companions).
Scope (setting ``CONFIG_SCOPE``): ``"standard"`` (default) leaves out the
large PC tracker folders (``PC_TRACKER_DIRS``), ``"all"`` lists everything.

The root ``ftp://ftp.modland.com/pub/modules`` is a CATALOGUE root
(``core/catalogue.py``): it is never walked, scanned or polled (the index
refresh replaces scanning), its folders browse from the index, and its file
source (``CatalogueFTPSource``) answers listings / stats from the index and
fetches a file only inside a play's fetch window — on a pool of at most
``POOL_CAP`` anonymous connections with no warm or kept-alive logins.

A play fetches the ONE file (plus the companions its format needs) through
the remote cache, then ``merge_after_play`` extracts the real metadata from
the cached copy (length, ``file_md5`` → song database / Modland joins,
subsongs, the file's own title / tags, defect flags) into the catalogue track
— keeping its path and id — through a normal store write; ``mtime`` > 0 marks
a merged track.

The index is refreshed on a schedule — daily (default), weekly, every two
weeks or monthly (setting ``CONFIG_INTERVAL``, ``INTERVALS``) — and on
demand; the delta (added / removed / resized files) is applied as normal store
writes.  A failed or implausible download keeps the current catalogue and is
tried again an hour (an implausible index: a day) after the attempt, whatever
the interval.

A scheduled (or "Refresh now") refresh reads Modland's daily changelogs
(``core/modland_changelog.py``) instead of the 5.8 MB index whenever it can:
over FTP it asks for each day's changelog since the last one applied by name
(``SIZE`` in the archive's root, then ``/pub/changelogs`` — the folder is
listed only on a server without ``SIZE``), downloads the new or grown ones
(none to a few hundred bytes on most days; a refresh that finds nothing new
exchanges ~1.8 KB, most of it the welcome banner), gets the sizes of the new /
changed files (by name, or by listing their folders when there are many), and
applies the net change to the index sqlite (a copy, swapped in) and the
library — a moved or renamed file keeps its plays, rating, stars and playlist
entries (``store.remap_track_ids``, ``SubsonicState.remap_track_ids``); the
moves are kept in the index (``pending_moves``) until the library has
followed them; doing them again changes nothing (``id_remap``).  An index
kept from an earlier switch-on that is no longer fresh catches up the same
way, and a full index just downloaded is brought forward from the changelogs
since its own date at once (it can be a day behind them).  The full index is downloaded instead
(``NeedFullIndex``) on first enable, when the index is missing, once a month
(``_FULL_RECONCILE_S``), after a gap of more than ``_DELTA_MAX_GAP_DAYS``,
for a changelog that is very large (a reorganisation day) or can't be read,
and when the index's file count drifts from the count in Modland's FTP
welcome banner.  Index storage:
``<data_dir>/scene/modland_catalog.sqlite`` (+ the downloaded zip).
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import io
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
import zipfile
from pathlib import Path

from soniqboom.core import catalogue
from soniqboom.core.filesource import DirEntry, FileStat, FTPFileSource

log = logging.getLogger(__name__)

HOST = "ftp.modland.com"
REMOTE_PATH = "/pub/modules"
ROOT = f"ftp://{HOST}{REMOTE_PATH}"
KEY = "modland"
LABEL = "Modland"
INDEX_URL = "https://ftp.modland.com/allmods.zip"
SITE_URL = "https://ftp.modland.com/"
# Where the file source connects (tests point it at a local FTP server; the
# catalogue's paths and ids keep ``ROOT``).
CONNECT_HOST = HOST
CONNECT_PORT = 21
# Anonymous login; the password names the client (FTP convention).
FTP_USER = "anonymous"
FTP_PASSWORD = "soniqboom@"
# At most this many simultaneous connections to Modland (it accepted 6 in a
# probe and publishes no limit — stay well below).
POOL_CAP = 3
# Idle connections are logged out after this long (no keep-alive NOOPs).
_IDLE_LOGOUT_S = 90.0
# A failed fetch holds the source offline this long, doubling per failure in a
# row up to the maximum (the next play after the hold tries again).
_OFFLINE_HOLD_S = 60.0
_OFFLINE_HOLD_MAX_S = 900.0

CONFIG_ENABLED = "modland_catalog_enabled"
CONFIG_SCOPE = "modland_catalog_scope"
CONFIG_SUBSONIC = "modland_catalog_subsonic"
CONFIG_RULES = "modland_catalog_rules"
CONFIG_INTERVAL = "modland_catalog_interval"
# When a tune not played yet is read for its details (tunes, length, tags):
# "intent" — when a listener opens its Track Info (the default), "lists" —
# also when it is shown in a list (more downloads from Modland).
CONFIG_TUNE_DETAILS = "modland_catalog_tune_details"
TUNE_DETAILS = ("intent", "lists")
# Bytes the index refreshes downloaded (full indexes + changelogs), in all.
CONFIG_INDEX_BYTES = "modland_catalog_index_bytes"
SCOPE_STANDARD = "standard"
SCOPE_ALL = "all"
SCOPES = (SCOPE_STANDARD, SCOPE_ALL)
# The large PC tracker folders the standard scope leaves out (by bytes the
# bulk of Modland: Fasttracker 2 36.7 GB + Impulsetracker 21.8 GB +
# Screamtracker 3 3.1 GB, Fasttracker 0.5 GB, Multitracker 0.2 GB — 2026-10).
PC_TRACKER_DIRS = ("Fasttracker 2", "Impulsetracker", "Screamtracker 3", "Fasttracker",
                   "Multitracker")
# Bump when the derivation of a catalogue track from its index line changes:
# the next build re-derives every catalogue track (user edits and enrichment
# carried, ``store._carry_enrichment``).
RULES_VERSION = 3          # 2: the format family genre (Amiga / Tracker …); fill-only credit
                           # 3: denylisted uade suffixes in their player's folder are music

# How often the index is refreshed: the setting's value → seconds, and the
# wording the settings show.
INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600,
             "biweekly": 14 * 24 * 3600, "monthly": 30 * 24 * 3600}
INTERVAL_LABELS = {"daily": "Daily", "weekly": "Weekly", "biweekly": "Every 2 weeks",
                   "monthly": "Monthly"}
DEFAULT_INTERVAL = "daily"
_RETRY_AFTER_FAILURE_S = 3600
# An index refused as implausible is tried again after a day — the file, not
# the connection, was the problem (independent of the interval).
_RETRY_AFTER_IMPLAUSIBLE_S = 24 * 3600
# A changed interval whose next refresh is already due refreshes this much
# later, not at once.
_REARM_GRACE_S = 300
# Changelog deltas (see the module docstring).
CHANGELOG_DIR = "/pub/changelogs"
_DELTA_MAX_FILE_BYTES = 256 * 1024     # one changelog larger than this: a reorganisation day
_DELTA_MAX_LINES = 3000                # all the changelogs of a refresh together
_DELTA_MAX_DIRS = 300                  # folders listed for the new files' sizes
_DELTA_MAX_GAP_DAYS = 45                # (a monthly schedule's retries stay inside it)
_DELTA_SIZE_BY_NAME_MAX = 40           # new sizes asked for file by file up to this many
_DELTA_SIZE_BY_NAME_DAYS = 40          # changelog dates asked for by name up to this many
_COLLECT_MAX_FILE_BYTES = 16 * 1024 * 1024  # a changelog read only for its moves (a full refresh)
_PENDING_MOVE_TRIES = 3                # failed attempts before pending moves are given up
_DELTA_OVERLAP_DAYS = 2                # changelogs re-read after a full index (idempotent)
_FULL_RECONCILE_S = 30 * 24 * 3600
_COUNT_TOLERANCE = 25                  # files the banner's count may drift from the index
_DOWNLOAD_TIMEOUT_S = 300
_MAX_INDEX_BYTES = 128 * 1024 * 1024
# Plausibility of a downloaded index (else the current catalogue stays).
_MIN_INDEX_ROWS = 100_000
_MIN_MUSIC_ROWS = 50_000
_MAX_SHRINK = 0.25                  # a refresh may lose at most 25 % of the rows

# File kinds in the index.
KIND_MUSIC = 1
KIND_OTHER = 2                      # companion / support file, unsupported format
KIND_ARCHIVE = 3
_ARCHIVE_EXTS = (".zip", ".lha", ".lzh", ".7z", ".rar", ".lzx")

# Store writes per chunk / derivation work per event-loop turn.
_UPSERT_CHUNK = 250
_DELETE_STEP = 100
_SLICE_S = 0.008
# The initial build's tracks: "Date added" sorts them as the oldest.
_INITIAL_ADDED_AT = 1
_INDEX_SCHEMA = 2                   # the sqlite index's layout (2: files.seen)

_status: dict = {
    "state": "off",               # off | building | ready | removing | error
    "phase": "",
    "done": 0,
    "total": 0,
    "error": None,
    "last_attempt": None,
    "started_at": None,
    "last_result": None,
}
_job_lock: asyncio.Lock | None = None
_job_lock_loop = None
_tasks: set = set()
_refresh_task: asyncio.Task | None = None
_refresh_wake: asyncio.Event | None = None     # set → the refresh loop re-reads its schedule
_rearm_not_before = 0.0                         # a re-armed schedule refreshes no sooner
_siblings: dict | None = None
_db_gen = 0
_tl = threading.local()
# The server's event loop (status flips from FTP worker threads land on it).
_loop: asyncio.AbstractEventLoop | None = None


# ── Paths / settings ──────────────────────────────────────────────────────────

def _dir() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "scene"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return _dir() / "modland_catalog.sqlite"


def zip_path() -> Path:
    return _dir() / "modland_allmods.zip"


def _store():
    from soniqboom.core.store import get_store
    return get_store()


def enabled() -> bool:
    try:
        return bool(_store().get_config(CONFIG_ENABLED, False))
    except Exception:                                   # noqa: BLE001
        return False


def scope() -> str:
    try:
        v = _store().get_config(CONFIG_SCOPE, SCOPE_STANDARD)
    except Exception:                                   # noqa: BLE001
        v = SCOPE_STANDARD
    return v if v in SCOPES else SCOPE_STANDARD


def interval() -> str:
    """How often the index is refreshed: a key of ``INTERVALS``."""
    try:
        v = _store().get_config(CONFIG_INTERVAL, DEFAULT_INTERVAL)
    except Exception:                                   # noqa: BLE001
        return DEFAULT_INTERVAL
    return v if v in INTERVALS else DEFAULT_INTERVAL


def tune_details() -> str:
    """When unplayed tunes are read for their details: a ``TUNE_DETAILS``."""
    try:
        v = _store().get_config(CONFIG_TUNE_DETAILS, "intent")
    except Exception:                                   # noqa: BLE001
        return "intent"
    return v if v in TUNE_DETAILS else "intent"


def set_tune_details(mode: str) -> dict:
    if mode not in TUNE_DETAILS:
        raise ValueError(f"tune_details must be one of {list(TUNE_DETAILS)}")
    _store().set_config(CONFIG_TUNE_DETAILS, mode)
    return status()


def interval_seconds() -> int:
    return INTERVALS[interval()]


def subsonic_visible() -> bool:
    try:
        return bool(_store().get_config(CONFIG_SUBSONIC, False))
    except Exception:                                   # noqa: BLE001
        return False


def root_hash() -> str:
    import hashlib
    return hashlib.sha256(ROOT.encode()).hexdigest()[:16]


def track_path(rel: str) -> str:
    return f"{ROOT}:/{rel}"


def track_id(rel: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, track_path(rel)))


def _get_job_lock() -> asyncio.Lock:
    global _job_lock, _job_lock_loop
    loop = asyncio.get_running_loop()
    if _job_lock is None or _job_lock_loop is not loop:
        _job_lock, _job_lock_loop = asyncio.Lock(), loop
    return _job_lock


def _spawn(coro, name: str) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


# ── Index database ────────────────────────────────────────────────────────────

def has_index() -> bool:
    try:
        return db_path().stat().st_size > 0
    except OSError:
        return False


def _ro() -> sqlite3.Connection | None:
    """A read-only connection to the index for this thread (reopened after
    the index file is replaced)."""
    con = getattr(_tl, "con", None)
    if con is not None and getattr(_tl, "gen", -1) == _db_gen:
        return con
    if con is not None:
        try:
            con.close()
        except Exception:                               # noqa: BLE001
            pass
        _tl.con = None
    if not has_index():
        return None
    try:
        con = sqlite3.connect(f"{db_path().resolve().as_uri()}?mode=ro", uri=True,
                              check_same_thread=False)
    except sqlite3.Error:
        return None
    _tl.con, _tl.gen = con, _db_gen
    return con


def _bump_db_gen() -> None:
    """The index file was replaced: every thread reopens it on its next query
    (``_ro``; a connection is only ever closed by its own thread — a query
    still running on the old file finishes on it)."""
    global _db_gen, _siblings
    _db_gen += 1
    _siblings = None


def index_info() -> dict:
    con = _ro()
    if con is None:
        return {}
    try:
        return dict(con.execute("SELECT k, v FROM info").fetchall())
    except sqlite3.Error:
        return {}


def _dir_id(con: sqlite3.Connection, rel_dir: str) -> int | None:
    row = con.execute("SELECT id FROM dirs WHERE dir = ?", (rel_dir,)).fetchone()
    return int(row[0]) if row else None


def entry(rel: str) -> tuple[int, int] | None:
    """``(size, kind)`` of index path ``rel``, or None."""
    con = _ro()
    if con is None:
        return None
    d, _, name = rel.strip("/").rpartition("/")
    try:
        row = con.execute(
            "SELECT f.size, f.kind FROM files f JOIN dirs d ON f.dir = d.id "
            "WHERE d.dir = ? AND f.name = ?", (d, name)).fetchone()
    except sqlite3.Error:
        return None
    return (int(row[0]), int(row[1])) if row else None


def is_music(rel: str) -> bool:
    e = entry(rel.lstrip("/"))
    return e is not None and e[1] == KIND_MUSIC


def _is_dir(rel: str) -> bool:
    rel = rel.strip("/")
    if not rel:
        return has_index()
    con = _ro()
    if con is None:
        return False
    try:
        return _dir_id(con, rel) is not None
    except sqlite3.Error:
        return False


def list_dir(rel_dir: str) -> list[DirEntry]:
    """The index's entries in folder ``rel_dir`` ("" = the root): sub-folders
    first, then files — paths root-relative with a leading slash, as the FTP
    source gives them."""
    rel_dir = rel_dir.strip("/")
    con = _ro()
    if con is None:
        return []
    out: list[DirEntry] = []
    try:
        did = _dir_id(con, rel_dir)
        if did is None:
            return []
        for (d,) in con.execute("SELECT dir FROM dirs WHERE parent = ? AND id != ? ORDER BY dir",
                                (did, did)):
            out.append(DirEntry(name=d.rsplit("/", 1)[-1], path="/" + d, is_dir=True))
        base = f"/{rel_dir}/" if rel_dir else "/"
        for name, size in con.execute(
                "SELECT name, size FROM files WHERE dir = ? ORDER BY name", (did,)):
            out.append(DirEntry(name=name, path=base + name, is_dir=False, size=int(size)))
    except sqlite3.Error:
        return []
    return out


def _all_rels(con: sqlite3.Connection, kind: int | None = None, *, seen: bool = False):
    q = ("SELECT d.dir, f.name, f.size, f.seen FROM files f JOIN dirs d ON f.dir = d.id"
         + (" WHERE f.kind = ?" if kind is not None else ""))
    for d, name, size, sn in con.execute(q, (kind,) if kind is not None else ()):
        rel = f"{d}/{name}" if d else name
        if seen:
            yield rel, int(size), int(sn or 0)
        else:
            yield rel, int(size)


# Modland format dirs whose music SoniqBoom has no engine for, though some of
# their file names collide with an Amiga replayer's name token (uade claims
# ``.mus``, ``.sng``, ``.sqt``, ``sm1`` …): C64 Sidplayer / GoatTracker, MSX,
# ZX Spectrum, Sam Coupé, X68000, PC-98, FM Towns, NES / tracker sources,
# AdLib trackers.  A uade-named file there is not listed (its core-extension
# files — an AdLib ``.rad``, a ``.vgz`` — still are).  From Modland's index
# (2026-10): the dirs whose uade-classified files are foreign formats.
_UADE_FOREIGN_DIRS = frozenset({
    "Ad Lib", "Spectrum", "Sidplayer", "Stereo Sidplayer", "FAC SoundTracker", "MDX",
    "MVS Tracker", "GoatTracker", "GoatTracker 2", "SCC-Musixx", "Synder SNG-Player",
    "Synder SNG-Player Stereo", "Synder Tracker", "Sam Coupe SNG", "Sam Coupe COP",
    "Euphony", "FamiTracker", "FM sound driver (FMP)", "MGSDRV", "MoonBlaster",
    "MoonBlaster (edit mode)", "MuSICA", "OPLLDRV", "Deflemask", "X-Tracker", "Beepola",
    "Buzz", "Astroidea XMF", "PMD", "S98", "Organya", "V2", "Piston Collage",
    "Piston Collage Protected", "Psycle", "Renoise", "Renoise Old", "SunVox",
    "Mad Tracker 2", "OpenMPT MPTM", "Velvet Studio", "WonderSwan", "SGC", "AY Emul",
    "MO3", "Symphonie", "Digital Symphony", "Sound Club", "Archimedes Tracker",
    "Face The Music", "Epic Megagames MASI", "MaxTrax", "TFM Music Maker",
    "Digital Tracker DTM", "SPU", "Capcom Q-Sound Format", "Super Nintendo Sound Format",
    "Sam Coupe E-Tracker",
})
# Support files whose names an Amiga replayer token can still match
# (``pn.<x>.info``, IFF ``Instruments/<x>.instr``): never music.
_NEVER_MUSIC_EXTS = (".info", ".instr", ".txt", ".nfo", ".diz", ".doc", ".readme")
# Families whose author sits one level deeper (``scene_metadata``): their
# format is the second level.
_TWO_LEVEL = frozenset({"Ad Lib", "Spectrum", "Video Game Music"})


def _engine(name: str) -> str | None:
    """``"core"`` (an extension a built-in engine owns), ``"uade"`` (an Amiga
    replayer's name token) or None (not music) — the scanner's discovery rule
    (``metadata.is_supported_music_name``)."""
    from soniqboom.core import metadata
    from soniqboom.core.scanner import _is_junk_filename
    if _is_junk_filename(name) or not metadata.is_supported_music_name(name):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext in metadata.SUPPORTED_EXTENSIONS and ext not in metadata._UADE_SUFFIX_EXTS:
        return "core"
    return "uade"


def folder_player_token(rel: str) -> str | None:
    """The uade token of a file whose extension is one of the suffixes the
    scanner never takes as music on its own (``uade_formats.
    _SUFFIX_REGISTRATION_DENY``: ``.ma``, ``.md``, ``.ps`` … — ``readme.md``)
    but which sits in the Modland folder named after that token's player
    ("Music Assembler/x.ma" → MusicAssembler, "Mike Davies/…/x.md"): a real
    module uade plays.  None otherwise (an IFF-SMUS instrument ``.ss`` is not
    Speedy System)."""
    from soniqboom.core import uade_formats as uf
    name = rel.rsplit("/", 1)[-1]
    tok = os.path.splitext(name)[1].lower().lstrip(".")
    if not tok or tok not in uf._SUFFIX_REGISTRATION_DENY:
        return None
    player = uf.player_map().get(tok)
    if not player:
        return None
    norm = lambda s: "".join(c for c in s.lower() if c.isalnum())  # noqa: E731
    return tok if norm(rel.split("/", 1)[0]) == norm(player) else None


def _dir_key(rel: str) -> str:
    parts = rel.split("/")
    if parts[0] in _TWO_LEVEL and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def _classify(rel: str, majority: dict | None = None) -> int:
    """The index kind of ``rel``: an archive, music SoniqBoom plays (listed),
    or anything else (companion halves, support files, formats it has no
    engine for).  An Amiga-replayer-named file in a foreign format dir
    (``_UADE_FOREIGN_DIRS``) or a stray among a dir of built-in-engine music
    (``majority``: dir key → its engine) is not music."""
    name = rel.rsplit("/", 1)[-1]
    low = name.lower()
    if low.endswith(_ARCHIVE_EXTS):
        return KIND_ARCHIVE
    if low.endswith(_NEVER_MUSIC_EXTS):
        return KIND_OTHER
    eng = _engine(name)
    if eng is None:
        return KIND_MUSIC if folder_player_token(rel) else KIND_OTHER
    if eng == "uade":
        if rel.split("/", 1)[0] in _UADE_FOREIGN_DIRS:
            return KIND_OTHER
        if majority is not None and majority.get(_dir_key(rel)) == "core":
            return KIND_OTHER
    return KIND_MUSIC


def _fingerprint() -> str:
    """What decides which index files are music: the supported formats and the
    installed uade's tokens (``scanner._listing_fingerprint``) + our rules."""
    try:
        from soniqboom.core.scanner import _listing_fingerprint
        fp = _listing_fingerprint()
    except Exception:                                   # noqa: BLE001
        fp = "?"
    return f"{fp}:{RULES_VERSION}:i{_INDEX_SCHEMA}"


def _index_lines(zf: zipfile.ZipFile, member: str):
    """``(rel, size)`` of every well-formed line of ``allmods.txt`` (a path
    with CR/LF/NUL, an empty or dot segment, or an undecodable byte is
    skipped — it is interpolated into FTP commands later)."""
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        for line in text:
            line = line.rstrip("\r\n")
            if not line:
                continue
            size_s, sep, rel = line.partition("\t")
            rel = rel.strip().strip("/")
            if (not sep or not size_s.isdigit() or not rel or "\x00" in rel
                    or "\r" in rel or "\n" in rel or "\ufffd" in rel
                    or any(p in ("", ".", "..") for p in rel.split("/"))):
                yield None, 0
                continue
            yield rel, int(size_s)


def build_index_db(zip_file: Path, out: Path, *, source: str = INDEX_URL,
                   last_modified: str | None = None, prev_db: Path | None = None,
                   now: float | None = None) -> dict:
    """Parse ``allmods.txt`` from ``zip_file`` into a fresh sqlite index at
    ``out``.  Blocking (run in an executor).  Returns the row counts; raises
    on an unreadable / implausible file (the caller keeps the old index).
    Two passes: the first learns each format dir's engine (built-in vs uade)
    so the second can drop uade-named strays among built-in-engine music.

    Every file records when it was first SEEN in an index (``seen``): the
    ``baseline`` (the first index's time, carried from ``prev_db``) for files
    that were there from the start, the build time for files new since
    ``prev_db``, and the earlier time for files that appeared in between —
    a catalogue track's "Date added" (``_added_at``)."""
    now = float(now if now is not None else time.time())
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_file) as zf:
        names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1].lower() == "allmods.txt"]
        if not names:
            raise ValueError("allmods.txt missing from the index archive")
        member = names[0]
        zi = zf.getinfo(member)
        index_date = datetime.datetime(*zi.date_time).strftime("%Y-%m-%d %H:%M")
        counts: dict[str, list[int]] = {}
        for rel, _size in _index_lines(zf, member):
            if rel is None:
                continue
            eng = _engine(rel.rsplit("/", 1)[-1])
            if eng is not None:
                c = counts.setdefault(_dir_key(rel), [0, 0])
                c[0 if eng == "core" else 1] += 1
        majority = {k: ("core" if c[0] > c[1] else "uade") for k, c in counts.items()}
        con = sqlite3.connect(out)
        try:
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            con.execute("CREATE TABLE dirs (id INTEGER PRIMARY KEY, dir TEXT NOT NULL UNIQUE, "
                        "parent INTEGER NOT NULL)")
            con.execute("CREATE TABLE files (dir INTEGER NOT NULL, name TEXT NOT NULL, "
                        "size INTEGER NOT NULL, kind INTEGER NOT NULL, "
                        "seen INTEGER NOT NULL DEFAULT 0, "
                        "PRIMARY KEY (dir, name)) WITHOUT ROWID")
            con.execute("CREATE TABLE info (k TEXT PRIMARY KEY, v TEXT)")
            dir_ids: dict[str, int] = {"": 0}
            con.execute("INSERT INTO dirs VALUES (0, '', 0)")

            def _did(d: str) -> int:
                i = dir_ids.get(d)
                if i is None:
                    parent = _did(d.rpartition("/")[0])
                    i = dir_ids[d] = len(dir_ids)
                    con.execute("INSERT INTO dirs VALUES (?,?,?)", (i, d, parent))
                return i
            rows = music = standard = bad = 0
            pc_dirs = frozenset(PC_TRACKER_DIRS)
            batch: list[tuple] = []
            for rel, size in _index_lines(zf, member):
                if rel is None:
                    bad += 1
                    continue
                d, _, name = rel.rpartition("/")
                kind = _classify(rel, majority)
                batch.append((_did(d), name, size, kind))
                rows += 1
                if kind == KIND_MUSIC:
                    music += 1
                    if rel.split("/", 1)[0] not in pc_dirs:
                        standard += 1
                if len(batch) >= 20_000:
                    con.executemany("INSERT OR REPLACE INTO files (dir, name, size, kind) "
                                    "VALUES (?,?,?,?)", batch)
                    batch.clear()
            if batch:
                con.executemany("INSERT OR REPLACE INTO files (dir, name, size, kind) "
                                "VALUES (?,?,?,?)", batch)
            con.execute("CREATE INDEX dirs_parent ON dirs(parent)")
            # Distinct files (a line repeated in the index is one file).
            rows = con.execute("SELECT count(*) FROM files").fetchone()[0]
            music = con.execute("SELECT count(*) FROM files WHERE kind = ?",
                                (KIND_MUSIC,)).fetchone()[0]
            standard = music - sum(
                1 for (dd,) in con.execute(
                    "SELECT d.dir FROM files f JOIN dirs d ON f.dir = d.id WHERE f.kind = ?",
                    (KIND_MUSIC,))
                if dd.split("/", 1)[0] in pc_dirs)
            baseline, new_files = _stamp_seen(con, prev_db, now)
            built = time.time()
            # Changelogs from a couple of days before the index's own date on
            # are applied over it — the days up to that date in replay mode
            # (the index may hold them already).  A date in the zip that can't
            # be right (unreadable, far in the past or the future) counts as
            # the day it was built.
            today = datetime.date.fromtimestamp(built)
            try:
                idx_day = datetime.date.fromisoformat(index_date[:10])
            except ValueError:
                idx_day = today
            if not (today - datetime.timedelta(days=60) <= idx_day
                    <= today + datetime.timedelta(days=1)):
                idx_day = today
            since = (idx_day - datetime.timedelta(days=_DELTA_OVERLAP_DAYS)).isoformat()
            info = {"rows": rows, "music_rows": music, "standard_rows": standard,
                    "bad_rows": bad, "baseline": int(baseline), "new_files": new_files,
                    "dirs": len(dir_ids) - 1, "index_date": index_date,
                    "built_at": built, "full_at": built, "source": source,
                    "last_modified": last_modified or "", "fingerprint": _fingerprint(),
                    "majority": json.dumps(majority, sort_keys=True),
                    "cl_since": since, "cl_replay_to": idx_day.isoformat(),
                    "cl_applied": "{}", "last_kind": "full"}
            con.executemany("INSERT INTO info VALUES (?,?)",
                            ((k, str(v)) for k, v in info.items()))
            con.commit()
        finally:
            con.close()
    if rows < _MIN_INDEX_ROWS or music < _MIN_MUSIC_ROWS:
        out.unlink(missing_ok=True)
        raise ValueError(f"implausible index: {rows} rows, {music} playable")
    return info


def _stamp_seen(con: sqlite3.Connection, prev_db: Path | None, now: float) -> tuple[float, int]:
    """Fill ``files.seen`` (see ``build_index_db``).  Returns ``(baseline,
    files new since prev_db)``."""
    baseline = now
    prev = None
    if prev_db is not None and prev_db.exists():
        try:
            con.execute("ATTACH DATABASE ? AS p", (str(prev_db),))      # (only read)
            row = con.execute("SELECT v FROM p.info WHERE k = 'baseline'").fetchone()
            con.execute("SELECT seen FROM p.files LIMIT 1").fetchall()      # has the column
            baseline = float(row[0]) if row and row[0] else now
            prev = True
        except sqlite3.Error:
            prev = None
            try:
                con.execute("DETACH DATABASE p")
            except sqlite3.Error:
                pass
    t_base = int(baseline)
    t_now = max(int(now), t_base + 1)        # (a new file always sorts after the first ones)
    con.execute("UPDATE files SET seen = ?", (t_base,))
    if not prev:
        return baseline, 0
    try:
        cur = con.execute(
            "UPDATE files SET seen = ? WHERE NOT EXISTS ("
            " SELECT 1 FROM p.files pf JOIN p.dirs pd ON pf.dir = pd.id"
            " WHERE pd.dir = (SELECT d.dir FROM dirs d WHERE d.id = files.dir)"
            " AND pf.name = files.name)", (t_now,))
        new_files = max(0, cur.rowcount or 0)
        later = con.execute(
            "SELECT pd.dir, pf.name, pf.seen FROM p.files pf JOIN p.dirs pd ON pf.dir = pd.id"
            " WHERE pf.seen > ?", (t_base,)).fetchall()
        con.executemany(
            "UPDATE files SET seen = ? WHERE seen = ? AND name = ?"
            " AND dir = (SELECT id FROM dirs WHERE dir = ?)",
            ((seen, t_base, name, d) for d, name, seen in later))
    finally:
        con.commit()
        con.execute("DETACH DATABASE p")
    return baseline, new_files


def _set_info(db: Path, values: dict) -> None:
    """Write ``values`` into the ``info`` table of index file ``db``.  Blocking."""
    con = sqlite3.connect(db)
    try:
        con.executemany("INSERT OR REPLACE INTO info VALUES (?,?)",
                        ((k, str(v)) for k, v in values.items()))
        con.commit()
    finally:
        con.close()


def _added_at(seen: int, baseline: int) -> int:
    """A catalogue track's "Date added": when its file first appeared in an
    index — the files of the first index (and of scope changes, of an
    interrupted first build) sort as the oldest (1), never as new."""
    return int(seen) if seen and int(seen) > int(baseline) else _INITIAL_ADDED_AT


class IndexShrank(ValueError):
    """A refreshed index lists far fewer files than the current one."""


def _download_index(dest: Path) -> dict:
    """Download ``INDEX_URL`` to ``dest`` (3 attempts, size-capped).
    Blocking.  ``tests/conftest.py`` replaces it (no network in tests)."""
    import httpx
    from soniqboom import __version__
    from soniqboom.core import netpolicy
    netpolicy.check(INDEX_URL)
    last: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True,
                              headers={"User-Agent": f"SoniqBoom/{__version__}"}) as client:
                with client.stream("GET", INDEX_URL) as resp:
                    resp.raise_for_status()
                    got = 0
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_bytes(1 << 16):
                            got += len(chunk)
                            if got > _MAX_INDEX_BYTES:
                                raise _IndexTooLarge(
                                    "the index is larger than expected — implausible")
                            fh.write(chunk)
                    return {"bytes": got, "last_modified": resp.headers.get("last-modified")}
        except _IndexTooLarge:
            # The same file every time: no second download now, and the next
            # try a day later (``_next_due``: "implausible").
            dest.unlink(missing_ok=True)
            raise
        except httpx.HTTPStatusError as exc:
            dest.unlink(missing_ok=True)
            code = exc.response.status_code
            from soniqboom.core.online_catalog import retry_after_of
            ra = retry_after_of(exc)
            if 400 <= code < 500 and code not in (408, 429):
                # Refused, and it would be again: no retry now, the next a
                # day later.
                raise _IndexRefused(f"Modland refused the index download (HTTP {code})") \
                    from exc
            if ra is not None:
                # 429 / 503 with a Retry-After: no retry before it.
                raise _IndexBusy(f"Modland asked for a pause of {ra:.0f} s",
                                 retry_after=ra) from exc
            last = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        except Exception as exc:                        # noqa: BLE001
            last = exc
            dest.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"index download failed: {last}")


class _IndexTooLarge(RuntimeError):
    """The index download passed ``_MAX_INDEX_BYTES``."""


class _IndexRefused(RuntimeError):
    """Modland refused the index download (HTTP 4xx): retried a day later."""


class _IndexBusy(RuntimeError):
    """Modland asked for a pause (``retry_after`` seconds)."""

    def __init__(self, msg: str, retry_after: float) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


def _refresh_index_files(*, download: bool, local_zip: Path | None = None,
                         accept_shrink: bool = False) -> dict:
    """Download (or take ``local_zip`` / the kept zip) and rebuild the index
    into place — only when the new one is plausible (also against the current
    one's size, unless ``accept_shrink``: a refresh the user asked for).
    Blocking."""
    d = _dir()
    tag = uuid.uuid4().hex[:12]                  # (a cancelled job's thread may still run)
    tmp_db = d / f"modland_catalog.sqlite.{tag}.building"
    tmp_zip = None
    meta: dict = {}
    if local_zip is not None:
        src_zip = local_zip
    elif download:
        tmp_zip = d / f"modland_allmods.zip.{tag}.part"
        meta = _download_index(tmp_zip)
        src_zip = tmp_zip
    else:
        src_zip = zip_path()
        if not src_zip.exists():
            raise FileNotFoundError("no downloaded index")
    try:
        info = build_index_db(src_zip, tmp_db,
                              last_modified=meta.get("last_modified"),
                              prev_db=db_path() if has_index() else None)
        old = index_info()
        try:
            old_rows = int(old.get("rows") or 0)
        except (TypeError, ValueError):
            old_rows = 0
        if old_rows and info["rows"] < old_rows * (1 - _MAX_SHRINK) and not accept_shrink:
            raise IndexShrank(f"Modland's new index lists {info['rows']:,} files, the "
                              f"current one {old_rows:,} — the current catalogue stays; "
                              f"press Refresh now to accept the new index")
        info["downloaded"] = int(meta.get("bytes") or 0)
        extra = {"last_bytes": info["downloaded"]}
        if not download and local_zip is None and old.get("full_at"):
            # (The kept zip read again — new classification rules: its data is
            # as old as its download, so the monthly check keeps its date.)
            extra["full_at"] = old.get("full_at")
        _set_info(tmp_db, extra)
        if tmp_zip is not None:
            os.replace(tmp_zip, zip_path())          # keep the downloaded zip
        elif local_zip is not None and local_zip != zip_path():
            import shutil
            tmp_copy = d / f"modland_allmods.zip.{tag}.copy"
            shutil.copyfile(local_zip, tmp_copy)
            os.replace(tmp_copy, zip_path())
        os.replace(tmp_db, db_path())
        _bump_db_gen()
        return info
    finally:
        tmp_db.unlink(missing_ok=True)
        if tmp_zip is not None:
            tmp_zip.unlink(missing_ok=True)


# ── Changelog deltas (module docstring; core/modland_changelog.py) ────────────

class NeedFullIndex(Exception):
    """The changelogs can't bring the index up to date: download the full
    index instead (the message says why).  ``moves``: the moves the
    changelogs were seen to make, when known — the library follows them
    over the full index (``_full_refresh``)."""

    def __init__(self, msg: str = "", moves: "dict[str, str] | None" = None):
        super().__init__(msg)
        self.moves = moves


def _banner_count(welcome: str | None) -> int | None:
    """The file count in Modland's FTP welcome ("Currently there are 516143
    modules online in 409 different formats"), or None."""
    import re
    m = re.search(r"(\d[\d,.]*)\s+modules\s+online", welcome or "", re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", "").replace(".", ""))
    except ValueError:
        return None


def _delta_connect():
    """``(ftp, welcome)``: an FTP session for a changelog refresh — its own
    short connection, not the play pool.  Blocking.  ``tests/conftest.py``
    replaces it (no network in tests)."""
    import ftplib
    from soniqboom.core import netpolicy
    netpolicy.check(CONNECT_HOST)
    ftp = ftplib.FTP(timeout=30)
    welcome = ftp.connect(CONNECT_HOST, CONNECT_PORT)
    ftp.login(FTP_USER, FTP_PASSWORD)
    return ftp, welcome


def _mlsd(ftp, path: str) -> list[tuple[str, dict]]:
    return [(n, f) for n, f in ftp.mlsd(path, facts=["type", "size"])
            if n not in (".", "..") and f.get("type") not in ("cdir", "pdir")]


def _changelog_files(ftp, since: str = "", until: str = "") -> dict[str, tuple[str, int]]:
    """``{date: (remote path, size)}`` of the changelogs dated ``since`` …
    ``until`` (ISO dates, inclusive), in the archive's root (the newest sits
    there until it is archived; its copy wins) or ``/pub/changelogs``.

    Asked for by name with ``SIZE`` — a few short commands on the control
    connection, nothing on a data connection — rather than by listing
    ``/pub/changelogs`` (1,300+ files, over 100 KB a refresh).  A day without
    changes has no file (``550``).  A server without ``SIZE`` (``500`` /
    ``502`` / ``504``), or no dates given: the two folders are listed."""
    import ftplib
    from soniqboom.core import modland_changelog as mcl
    out: dict[str, tuple[str, int]] = {}
    try:
        first = datetime.date.fromisoformat(since)
        last = datetime.date.fromisoformat(until)
    except ValueError:
        first = last = None
    if (first is not None and last is not None
            and 0 <= (last - first).days <= _DELTA_SIZE_BY_NAME_DAYS):
        try:
            ftp.voidcmd("TYPE I")              # (some servers refuse SIZE in ASCII mode)
            day = first
            while day <= last:
                d = day.isoformat()
                for path in (f"/changelog_{d}.txt", f"{CHANGELOG_DIR}/changelog_{d}.txt"):
                    try:
                        size = ftp.size(path)
                    except ftplib.error_perm as exc:
                        if str(exc)[:3] in ("500", "502", "504"):
                            raise _NoSize() from None
                        continue                   # (550: no such file)
                    if size is not None:
                        out[d] = (path, int(size))
                        break
                day += datetime.timedelta(days=1)
            return out
        except _NoSize:
            out.clear()
    listed = False
    for base in (CHANGELOG_DIR, "/"):
        try:
            entries = _mlsd(ftp, base)
        except ftplib.error_perm:
            continue
        listed = True
        for name, facts in entries:
            d = mcl.date_of(name)
            if d and facts.get("type", "file") == "file":
                try:
                    size = int(facts.get("size") or 0)
                except ValueError:
                    size = 0
                out[d] = (f"{base.rstrip('/')}/{name}", size)
    if not listed:
        raise NeedFullIndex("Modland's changelogs could not be listed")
    return out


class _NoSize(Exception):
    """The FTP server has no ``SIZE`` command."""


def _applied_entry(v) -> tuple[int, str | None]:
    """``(bytes applied, sha1 of them)`` of one ``cl_applied`` entry (an
    older index stored the size alone)."""
    if isinstance(v, (list, tuple)) and v:
        try:
            return int(v[0]), (str(v[1]) if len(v) > 1 and v[1] else None)
        except (TypeError, ValueError):
            return 0, None
    try:
        return int(v), None
    except (TypeError, ValueError):
        return 0, None


def _cl_state(info: dict) -> tuple[str, dict]:
    """``(since, applied)``: the first changelog date still to read, and
    ``{date: [bytes applied, sha1 of those bytes]}`` of the changelog files
    applied since (``_applied_entry``)."""
    try:
        applied = json.loads(info.get("cl_applied") or "{}")
    except ValueError:
        applied = {}
    if not isinstance(applied, dict):
        applied = {}
    since = info.get("cl_since") or ""
    if not since:
        idx = (info.get("index_date") or "")[:10]
        try:
            since = (datetime.date.fromisoformat(idx)
                     - datetime.timedelta(days=_DELTA_OVERLAP_DAYS)).isoformat()
        except ValueError:
            since = ""
    return since, applied


def _full_reason(info: dict, now: float | None = None) -> str | None:
    """Why the next refresh must download the full index — or None (the
    changelogs can do it)."""
    now = time.time() if now is None else now
    if not has_index() or not info:
        return "no index yet"
    try:
        full_at = float(info.get("full_at") or info.get("built_at") or 0)
    except ValueError:
        full_at = 0.0
    # (Never twice in a row: with a monthly schedule every update would be one.)
    if not full_at or (now - full_at >= _FULL_RECONCILE_S
                       and info.get("last_kind") != "full"):
        return "the monthly full check"
    since, _applied = _cl_state(info)
    try:
        gap = (datetime.date.fromtimestamp(now) - datetime.date.fromisoformat(since)).days
    except ValueError:
        return "the index's changelog date is unknown"
    if gap > _DELTA_MAX_GAP_DAYS + _DELTA_OVERLAP_DAYS:
        return f"the last update was {gap} days ago"
    return None


def _top_dirs() -> frozenset:
    con = _ro()
    if con is None:
        return frozenset()
    try:
        return frozenset(d for (d,) in con.execute(
            "SELECT dir FROM dirs WHERE parent = 0 AND id != 0"))
    except sqlite3.Error:
        return frozenset()


def _in_index(ops: list, tops: frozenset, inside=None) -> list:
    """The operations on files the index lists: a changelog also names
    folders outside Modland's module index (``software/…``) — dropped; a move
    across that boundary is a delete or an add.  ``inside(rel)``: whether a
    path counts (default: its top folder is one of ``tops``)."""
    if inside is None:
        def inside(rel: str) -> bool:
            return rel.split("/", 1)[0] in tops
    out = []
    for op in ops:
        one = _in_index_one(op, inside)
        if one is not None:
            out.append(one)
    return out


def _in_index_one(op: tuple, inside) -> tuple | None:
    if op[0] == "moved":
        s, d = inside(op[1]), inside(op[2])
        if s and d:
            return op
        if s:
            return ("deleted", op[1])
        if d:
            return ("added", op[2])
        return None
    return op if inside(op[1]) else None


def _top_probe(ftp, tops: frozenset):
    """``inside(rel)`` for a changelog: a top folder the index lists, or —
    a new format folder at Modland — one ``SIZE`` under the module tree says
    is there (asked once per folder, a few folders at most)."""
    import ftplib
    seen: dict[str, bool] = {}

    def inside(rel: str) -> bool:
        top = rel.split("/", 1)[0]
        if top in tops:
            return True
        if top not in seen:
            ok = False
            if len(seen) < 10:
                try:
                    ok = ftp.size(f"{REMOTE_PATH}/{rel}") is not None
                except (ftplib.error_perm, ftplib.error_reply):
                    ok = False
                except (OSError, EOFError, ftplib.Error):
                    ok = False
            seen[top] = ok
        return seen[top]
    return inside


def _counting(ftp, welcome: str | None) -> dict:
    """Count what an FTP session sends and receives from here on (control
    lines, listings, files) — what a refresh "downloaded" is the lot."""
    box = {"bytes": len(welcome or "") + 2}
    put, get, lines = ftp.putline, ftp.getline, ftp.retrlines

    def putline(line):
        box["bytes"] += len(line) + 2
        return put(line)

    def getline():
        line = get()
        box["bytes"] += len(line) + 2
        return line

    def retrlines(cmd, callback=None):
        def cb(line):
            box["bytes"] += len(line) + 2
            if callback is not None:
                callback(line)
        return lines(cmd, cb)
    try:
        ftp.putline, ftp.getline, ftp.retrlines = putline, getline, retrlines
    except AttributeError:                              # (a stand-in without them)
        pass
    return box


def _fetch_delta(info: dict, *, collect: bool = False) -> dict:
    """Read the changelogs since the last update and plan their net change
    over the index.  Blocking (a worker thread; its own FTP session).
    Raises ``NeedFullIndex`` when they can't do it with certainty; network
    errors propagate (the refresh fails and is tried again in an hour).

    A changelog read before is read again only when it grew; then only its
    new lines are applied (those before are checked against the sha1 kept
    of them — a rewritten file is replayed whole).  Only complete lines are
    applied.  Days the last full index may already hold, and rewritten
    files, are planned in replay mode (``modland_changelog.plan``).

    ``collect``: for a full refresh — read the changelogs only for the moves
    they make (no size limits but ``_COLLECT_MAX_FILE_BYTES``, unreadable
    files skipped, everything in replay mode, no sizes)."""
    import ftplib
    import hashlib
    from soniqboom.core import modland_changelog as mcl
    since, applied = _cl_state(info)
    replay_to = info.get("cl_replay_to") or (info.get("index_date") or "")[:10]
    ftp, welcome = _delta_connect()
    box = _counting(ftp, welcome)
    try:
        banner = _banner_count(welcome)
        # (Up to tomorrow: Modland dates its changelogs in its own time zone.)
        until = (datetime.datetime.now(datetime.timezone.utc).date()
                 + datetime.timedelta(days=1)).isoformat()
        files = _changelog_files(ftp, since, until)
        want = sorted(d for d, (_p, size) in files.items()
                      if d >= since and _applied_entry(applied.get(d))[0] != size)
        for d in want:
            # (Days the full index holds already are read whatever their size:
            # a reorganisation day there must not force one full index after
            # another — they are planned in replay mode anyway.)
            cap = (_COLLECT_MAX_FILE_BYTES if collect or d <= replay_to
                   else _DELTA_MAX_FILE_BYTES)
            if files[d][1] > cap:
                raise NeedFullIndex(f"the changelog of {d} is large "
                                    f"({files[d][1] // 1024} KB — a reorganisation)")
        entries: list[tuple[tuple, bool]] = []
        new_applied: dict[str, list] = {}
        lines = 0
        tops = _top_dirs()
        inside = _top_probe(ftp, tops)

        def _retr(remote: str) -> bytes:
            buf = io.BytesIO()

            def _w(chunk: bytes) -> None:
                box["bytes"] += len(chunk)
                buf.write(chunk)
            ftp.retrbinary(f"RETR {remote}", _w)
            return buf.getvalue()

        for d in want:
            path = files[d][0]
            try:
                data = _retr(path)
            except ftplib.error_perm as exc:
                # (Archived from the root between SIZE and RETR: read that copy.)
                alt = f"{CHANGELOG_DIR}/changelog_{d}.txt"
                if not str(exc).startswith("550") or path == alt:
                    raise
                path = alt
                data = _retr(alt)
            if not path.startswith(CHANGELOG_DIR + "/"):
                # The root's copy may still be written to: complete lines only.
                data = data[:data.rfind(b"\n") + 1]
            n_prev, h_prev = _applied_entry(applied.get(d))
            again = collect or d <= replay_to
            body = data
            if n_prev and not collect:
                if (h_prev and len(data) >= n_prev
                        and hashlib.sha1(data[:n_prev]).hexdigest() == h_prev):
                    body = data[n_prev:]                    # what was added since
                elif not h_prev and len(data) == n_prev:
                    body = b""                              # (an older entry: same file)
                else:
                    again = True                            # rewritten: replayed whole
            new_applied[d] = [len(data), hashlib.sha1(data).hexdigest()]
            if not body:
                continue
            if not again:
                lines += body.count(b"\n")
            try:
                ops = mcl.parse(body)
            except mcl.ChangelogError as exc:
                if collect:
                    continue
                raise NeedFullIndex(f"the changelog of {d} could not be read ({exc})") from None
            for op in ops:
                one = _in_index_one(op, inside)
                if one is not None:
                    entries.append((one, again))
        # (Files Modland replaced in place, from records not replayed — a
        # replayed "updated" is most likely held by the index already.)
        fresh_updates = {op[1] for op, again in entries if op[0] == "updated" and not again}
        if lines > _DELTA_MAX_LINES and not collect:
            raise NeedFullIndex(f"the changelogs list {lines:,} changes")
        try:
            plan = mcl.plan([e for e, _a in entries], lambda rel: entry(rel) is not None,
                            [a for _e, a in entries])
        except mcl.ChangelogError as exc:
            raise NeedFullIndex(f"the changelogs don't match the index ({exc})") from None
        out = {"plan": plan, "sizes": {}, "banner": banner, "dates": want,
               "applied": new_applied,
               "updated": [r for r in plan.updated if r in fresh_updates]}
        if collect:
            out["bytes"] = box["bytes"]
            return out
        need = plan.need_size()
        # The new sizes: a few files are asked for by name (SIZE, on the control
        # connection), more by listing their folders.
        sizes: dict[str, int] | None = None
        if need and len(need) <= _DELTA_SIZE_BY_NAME_MAX:
            sizes = {}
            try:
                for rel in need:
                    try:
                        got = ftp.size(f"{REMOTE_PATH}/{rel}")
                    except ftplib.error_perm as exc:
                        if str(exc)[:3] in ("500", "502", "504"):
                            raise _NoSize() from None
                        continue                          # (550: gone since)
                    if got is not None:
                        sizes[rel] = int(got)
            except _NoSize:
                sizes = None
        if sizes is None:
            dirs = sorted({rel.rpartition("/")[0] for rel in need})
            if len(dirs) > _DELTA_MAX_DIRS:
                raise NeedFullIndex(f"the changes touch {len(dirs):,} folders",
                                    moves=dict(plan.moved))
            sizes = {}
            for dd in dirs:
                try:
                    listing = _mlsd(ftp, f"{REMOTE_PATH}/{dd}" if dd else REMOTE_PATH)
                except ftplib.error_perm:
                    continue                              # the folder is gone (a later change)
                for name, facts in listing:
                    if facts.get("type", "file") != "file":
                        continue
                    try:
                        sizes[f"{dd}/{name}" if dd else name] = int(facts.get("size") or 0)
                    except ValueError:
                        pass
        out["sizes"] = sizes
        out["bytes"] = box["bytes"]
        return out
    except NeedFullIndex as exc:
        exc.nbytes = box["bytes"]
        raise
    finally:
        try:
            ftp.quit()
        except Exception:                               # noqa: BLE001
            try:
                ftp.close()
            except Exception:                           # noqa: BLE001
                pass


def _majority_of(con: sqlite3.Connection, info: dict) -> dict:
    """Each format folder's engine (``build_index_db``'s first pass) — kept in
    the index; worked out from its files for an index built before that."""
    try:
        m = json.loads(info.get("majority") or "")
        if isinstance(m, dict):
            return m
    except ValueError:
        pass
    counts: dict[str, list[int]] = {}
    for rel, _size in _all_rels(con):
        eng = _engine(rel.rsplit("/", 1)[-1])
        if eng is not None:
            c = counts.setdefault(_dir_key(rel), [0, 0])
            c[0 if eng == "core" else 1] += 1
    return {k: ("core" if c[0] > c[1] else "uade") for k, c in counts.items()}


def _apply_delta(fetched: dict, now: float) -> dict:
    """Apply a planned delta to a COPY of the index and swap it in (readers
    on the old file finish there).  Raises ``NeedFullIndex`` (nothing
    swapped) when the result's file count drifts from Modland's banner.
    Blocking.  Returns what changed, with ``moved`` = {old rel: new rel} of
    the moves that were applied."""
    import shutil
    plan = fetched["plan"]
    sizes = fetched["sizes"]
    d = _dir()
    tmp = d / f"modland_catalog.sqlite.{uuid.uuid4().hex[:12]}.delta"
    shutil.copyfile(db_path(), tmp)
    out = {"added": 0, "deleted": 0, "resized": 0, "moved": {}, "skipped": 0}
    try:
        con = sqlite3.connect(tmp)
        try:
            info = dict(con.execute("SELECT k, v FROM info").fetchall())
            majority = _majority_of(con, info)
            ids: dict[str, int] = {}

            def did(rel_dir: str, create: bool) -> int | None:
                if rel_dir in ids:
                    return ids[rel_dir]
                row = con.execute("SELECT id FROM dirs WHERE dir = ?", (rel_dir,)).fetchone()
                if row:
                    ids[rel_dir] = int(row[0])
                    return ids[rel_dir]
                if not create:
                    return None
                parent = did(rel_dir.rpartition("/")[0], True) if rel_dir else 0
                new_id = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM dirs")
                             .fetchone()[0])
                con.execute("INSERT INTO dirs VALUES (?,?,?)", (new_id, rel_dir, parent))
                ids[rel_dir] = new_id
                return new_id

            def row_of(rel: str):
                dd, _, name = rel.rpartition("/")
                i = did(dd, False)
                if i is None:
                    return None, None, name
                r = con.execute("SELECT size, kind, seen FROM files WHERE dir = ? AND name = ?",
                                (i, name)).fetchone()
                return i, r, name

            def put(rel: str, size: int, seen: int) -> None:
                dd, _, name = rel.rpartition("/")
                con.execute("INSERT OR REPLACE INTO files (dir, name, size, kind, seen) "
                            "VALUES (?,?,?,?,?)",
                            (did(dd, True), name, int(size), _classify(rel, majority), int(seen)))

            def drop(rel: str) -> bool:
                i, r, name = row_of(rel)
                if r is None:
                    return False
                con.execute("DELETE FROM files WHERE dir = ? AND name = ?", (i, name))
                return True
            t_now = int(now)
            for rel in plan.deleted:
                out["deleted"] += drop(rel)
            # Moves in two steps — every source read and dropped first, then
            # every destination written — so a move onto a path another move
            # leaves (a rename chain, a swap) never overwrites that file.
            leaving = []
            for old, new in plan.moved.items():
                _i, r, _n = row_of(old)
                if r is None:
                    out["skipped"] += 1
                    continue
                leaving.append((old, new, r))
            for old, _new, _r in leaving:
                drop(old)
            for old, new, r in leaving:
                size = int(r[0])
                if new in plan.moved_changed:
                    if new not in sizes:                   # gone from Modland since
                        out["deleted"] += 1
                        continue
                    size = sizes[new]
                put(new, size, int(r[2] or t_now))
                out["moved"][old] = new
            for rel in plan.added:
                if rel not in sizes:
                    out["skipped"] += 1                    # (not on Modland any more)
                    continue
                put(rel, sizes[rel], t_now)
                out["added"] += 1
            for rel in plan.updated:
                _i, r, _n = row_of(rel)
                if rel not in sizes:
                    out["deleted"] += drop(rel)
                    continue
                if r is None:
                    put(rel, sizes[rel], t_now)
                    out["added"] += 1
                elif int(r[0]) != sizes[rel]:
                    put(rel, sizes[rel], int(r[2] or t_now))
                    out["resized"] += 1
            rows = con.execute("SELECT count(*) FROM files").fetchone()[0]
            music = con.execute("SELECT count(*) FROM files WHERE kind = ?",
                                (KIND_MUSIC,)).fetchone()[0]
            pc_dirs = frozenset(PC_TRACKER_DIRS)
            standard = music - sum(
                1 for (dd,) in con.execute(
                    "SELECT d.dir FROM files f JOIN dirs d ON f.dir = d.id WHERE f.kind = ?",
                    (KIND_MUSIC,))
                if dd.split("/", 1)[0] in pc_dirs)
            banner = fetched.get("banner")
            ref = info.get("count_ref")
            # (Modland's count may lag its changelogs by what this refresh changed.)
            tolerance = _COUNT_TOLERANCE + len(plan.added) + len(plan.deleted)
            new_info = {"rows": rows, "music_rows": music, "standard_rows": standard,
                        "built_at": now, "last_kind": "delta",
                        "last_bytes": fetched.get("bytes", 0),
                        "last_changelogs": len(fetched.get("dates") or ()),
                        "majority": json.dumps(majority, sort_keys=True)}
            if banner is not None:
                drift = int(banner) - int(rows)
                if ref in (None, ""):
                    new_info["count_ref"] = drift          # (first check after a full index)
                elif abs(drift - int(float(ref))) > tolerance:
                    raise NeedFullIndex(f"Modland lists {banner:,} files, the updated index "
                                        f"{rows:,}", moves=dict(plan.moved))
            try:
                applied = json.loads(info.get("cl_applied") or "{}")
            except ValueError:
                applied = {}
            applied = applied if isinstance(applied, dict) else {}
            applied.update(fetched.get("applied") or {})
            dates = sorted(applied)
            since = info.get("cl_since") or ""
            if dates:
                # Keep ~the last six weeks of what was applied; read from the
                # newest applied date on (it may still grow).
                keep = (datetime.date.fromtimestamp(now)
                        - datetime.timedelta(days=_DELTA_MAX_GAP_DAYS + 14)).isoformat()
                applied = {k: v for k, v in applied.items() if k >= keep}
                since = max(since, dates[-1])
            new_info["cl_applied"] = json.dumps(applied, sort_keys=True)
            new_info["cl_since"] = since
            # The moves the library still has to follow (``_run_pending_moves``),
            # kept with the index that holds them: a move the library missed
            # (a crash, a failed step) is redone by the next refresh instead of
            # turning into a removed + a new tune (user data lost).  An EARLIER
            # pending a → b followed by this refresh's b → c is a → c (this
            # refresh's own moves all start from the index before it: never
            # composed with each other).
            earlier = _pending_moves(info)
            inv = {b: a for a, b in earlier.items()}
            pend = {}
            for old, new in out["moved"].items():
                a = inv.get(old)
                pend[a if a is not None else old] = new
            for a, b in earlier.items():
                if b not in out["moved"]:
                    pend.setdefault(a, b)
            pend = {a: b for a, b in pend.items() if a != b}   # (moved back: nothing to follow)
            new_info["pending_moves"] = json.dumps(pend, sort_keys=True)
            if out["moved"]:
                new_info["pending_tries"] = 0       # (new moves: their own tries)
            out["pending"] = pend
            con.executemany("INSERT OR REPLACE INTO info VALUES (?,?)",
                            ((k, str(v)) for k, v in new_info.items()))
            con.commit()
        finally:
            con.close()
        os.replace(tmp, db_path())
        _bump_db_gen()               # (here: a cancelled job's thread still gets here)
        return out
    finally:
        tmp.unlink(missing_ok=True)


def _pending_moves(info: dict) -> dict[str, str]:
    """``{old rel: new rel}`` of the index's moves the library has not
    followed yet (info ``pending_moves``)."""
    try:
        got = json.loads((info or {}).get("pending_moves") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(got, dict):
        return {}
    return {o: n for o, n in got.items() if isinstance(o, str) and isinstance(n, str)}


async def _run_pending_moves(moves: "dict[str, str] | None" = None,
                             changed: "frozenset | set" = frozenset()) -> int:
    """Make the library follow the index's pending moves (``_move_tracks``,
    which is idempotent), then clear them.  Returns the tracks moved.  A
    failure leaves them pending (the refresh fails, and is tried again an
    hour later); after ``_PENDING_MOVE_TRIES`` failures they are given up —
    those tunes then come back as new ones — so one bad move can't block
    every later refresh."""
    loop = asyncio.get_running_loop()
    info = index_info()
    if moves is None:
        moves = _pending_moves(info)
    if not moves:
        return 0
    _progress("moving tunes", 0, len(moves))
    try:
        n = await _move_tracks(moves, changed)
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            tries = int(info.get("pending_tries") or 0) + 1
        except (TypeError, ValueError):
            tries = 1
        if tries < _PENDING_MOVE_TRIES or not db_path().exists():
            if db_path().exists():
                await loop.run_in_executor(
                    None, lambda: _set_info(db_path(), {"pending_tries": tries}))
            raise
        log.warning("Modland catalogue: giving up %d moved tune(s) after %d failed "
                    "attempts", len(moves), tries, exc_info=True)
        n = 0
    if db_path().exists():
        await loop.run_in_executor(
            None, lambda: _set_info(db_path(), {"pending_moves": "{}", "pending_tries": 0}))
    return n


async def _move_tracks(moves: dict[str, str], changed: "frozenset | set" = frozenset()) -> int:
    """The library side of moved / renamed catalogue files: each one's track
    takes its new path and id (``build_track``; what a play read stays when
    the file is the same size), and its plays, rating, history and playlist
    entries (``store.remap_track_ids``), Subsonic stars, queue entries and
    bookmarks (``SubsonicState.remap_track_ids``) and parked rows follow it.
    Returns the tracks moved."""
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    store = _store()
    loop = asyncio.get_running_loop()
    moves = {o: n for o, n in moves.items() if o != n}
    mapping = {track_id(o): track_id(n) for o, n in moves.items()}
    if not mapping:
        return 0
    siblings = await loop.run_in_executor(None, _siblings_map)
    from soniqboom.core import genre_derive, scene_metadata as sm
    use_filename = sm.filename_game_enabled()
    names = genre_derive.names_enabled()
    rh = root_hash()
    skip = frozenset(PC_TRACKER_DIRS) if scope() == SCOPE_STANDARD else frozenset()
    memo: dict = {}
    rows: list[dict] = []
    gone: list[str] = []
    outside: list[str] = []
    for old_rel, new_rel in moves.items():
        old_id, new_id = track_id(old_rel), track_id(new_rel)
        old = store._tracks.get(old_id)
        e = entry(new_rel)
        in_scope = (e is not None and e[1] == KIND_MUSIC
                    and new_rel.split("/", 1)[0] not in skip)
        if not in_scope:
            outside.append(new_id)
            continue                     # (apply_index removes the old track)
        if old is None:
            continue                     # (apply_index adds the new one)
        t = build_track(new_rel, e[0], added_at=old.get("added_at") or _INITIAL_ADDED_AT,
                        siblings=siblings, use_filename=use_filename, names=names,
                        dir_hash=_dir_hash_of(new_rel, memo), root_h=rh)
        if old.get("file_size") == e[0] and is_merged(old) and new_rel not in changed:
            t = _rederive_merged(old, t)        # (``changed``: replaced on the way — read anew)
        rows.append(_carry_user_edits(old, t))
        gone.append(old_id)
    if rows:
        store.store_hash_lookups_batch(sorted({"/" + r["path"][len(ROOT) + 2:].rpartition("/")[0]
                                               for r in rows}))
        for i in range(0, len(rows), _UPSERT_CHUNK):
            store.upsert_tracks_batch(rows[i:i + _UPSERT_CHUNK])
            await asyncio.sleep(0)
    store.remap_track_ids(mapping)
    # (A failure propagates: the moves stay pending and are redone — every
    # step here is idempotent.)
    from soniqboom.core.subsonic_state import get_state
    await get_state().remap_track_ids(mapping)
    parked = store.get_config(PARKED_PLAYLIST_IDS) or []
    if any(p in mapping for p in parked):
        store.set_config(PARKED_PLAYLIST_IDS, sorted({mapping.get(p, p) for p in parked}))

    def _remap_parked_rows() -> None:
        kept = _load_parked_rows()
        hit = [k for k in kept if k in mapping]
        for k in hit:
            row = kept.pop(k)
            row["id"] = mapping[k]
            kept.setdefault(mapping[k], row)
        if hit:
            _save_parked_rows(kept)
    await loop.run_in_executor(None, _remap_parked_rows)
    if outside:
        await _park_user_refs(outside)    # a playlist keeps a tune that left the scope
    # (An old id that is also a new one — a rename chain, a swap — now holds
    # its new tune: not deleted.)
    new_ids = set(mapping.values())
    gone = [g for g in gone if g not in new_ids]
    if gone:
        await _delete_sliced(store, gone)
    if rows or gone or outside:
        await _after_library_change()    # (folder listings keep paths: re-read)
    return len(rows)


def _carry_user_edits(old: dict, new: dict) -> dict:
    """What the listener typed into a tune's fields (``user_edited``, a year
    set by hand) follows it to its new row (a moved tune, an updated file)."""
    ue = old.get("user_edited")
    if isinstance(ue, list) and ue:
        for f in ue:
            if isinstance(f, str) and f in old:
                new[f] = old[f]
        new["user_edited"] = list(ue)
        if "album" in ue:
            new["album_source"] = old.get("album_source")
        if "game" in ue:
            new["game_source"] = old.get("game_source")
    if old.get("year_source") == "user":
        new["year"] = old.get("year")
        new["year_source"] = "user"
        if "year_file" in old:
            new["year_file"] = old["year_file"]
    return new


async def _reset_updated(rels: list[str]) -> int:
    """Files Modland replaced in place (``updated``, the size maybe the same):
    the cached copy goes, and a tune whose first play filled in its details
    is listed afresh, so its next play downloads and reads the new file.
    Returns the tunes reset."""
    if not rels:
        return 0
    from soniqboom.core.remote_cache import get_cache
    store = _store()
    cache = get_cache()
    loop = asyncio.get_running_loop()
    siblings = await loop.run_in_executor(None, _siblings_map)
    from soniqboom.core import genre_derive, scene_metadata as sm
    use_filename = sm.filename_game_enabled()
    names = genre_derive.names_enabled()
    rh = root_hash()
    memo: dict = {}
    rows = []
    for rel in rels:
        try:
            # (A size no file has: whatever is cached for it is stale.)
            cache.validate_size(ROOT, "/" + rel, 1 << 62)
        except Exception:                               # noqa: BLE001
            pass
        old = store._tracks.get(track_id(rel))
        e = entry(rel)
        if old is None or e is None or not is_merged(old):
            continue
        t = build_track(rel, e[0], added_at=old.get("added_at") or _INITIAL_ADDED_AT,
                        siblings=siblings, use_filename=use_filename, names=names,
                        dir_hash=_dir_hash_of(rel, memo), root_h=rh)
        rows.append(_carry_user_edits(old, t))
    for i in range(0, len(rows), _UPSERT_CHUNK):
        store.upsert_tracks_batch(rows[i:i + _UPSERT_CHUNK])
        await asyncio.sleep(0)
    return len(rows)


async def _delta_refresh() -> dict:
    """A refresh from the changelogs (module docstring).  Raises
    ``NeedFullIndex`` when the full index must be downloaded instead."""
    loop = asyncio.get_running_loop()
    info = index_info()
    if info and info.get("fingerprint") != _fingerprint():
        if not zip_path().exists():
            raise NeedFullIndex("the classification rules changed")
        # New classification rules: the kept index first (no download), then
        # the changelogs since its date over it.
        _progress("re-reading the index", 0, 0)
        await loop.run_in_executor(None, lambda: _refresh_index_files(download=False))
        info = index_info()
    why = _full_reason(info)
    if why:
        raise NeedFullIndex(why)
    fetched, done, moved = await _apply_changelogs(info)
    res = await apply_index()
    nbytes = int(fetched.get("bytes") or 0)
    res.update(source="changelogs", changelogs=len(fetched.get("dates") or ()),
               downloaded=nbytes, moved=moved, index_added=done["added"],
               index_deleted=done["deleted"], index_resized=done["resized"])
    log.info("Modland catalogue: updated from %d changelog(s) (%d bytes): %d added, "
             "%d removed, %d re-sized, %d moved in the index", res["changelogs"], nbytes,
             done["added"], done["deleted"], done["resized"], len(done["moved"]))
    return res


async def _apply_changelogs(info: dict) -> tuple[dict, dict, int]:
    """Read the changelogs since ``info``'s state, apply them to the index and
    make the library follow its moves and in-place updates (not yet
    ``apply_index``).  Returns ``(fetched, applied, tunes moved)``; raises
    ``NeedFullIndex`` like ``_fetch_delta`` / ``_apply_delta``."""
    loop = asyncio.get_running_loop()
    _progress("reading Modland's changelogs", 0, 0)
    try:
        fetched = await loop.run_in_executor(None, _fetch_delta, info)
    except NeedFullIndex as exc:
        _count_index_bytes(int(getattr(exc, "nbytes", 0) or 0))
        raise
    _count_index_bytes(int(fetched.get("bytes") or 0))
    done = await loop.run_in_executor(None, _apply_delta, fetched, time.time())
    _bump_db_gen()
    moved = await _run_pending_moves(done.get("pending", {}),
                                     set(fetched["plan"].moved_changed))
    await _reset_updated(list(fetched.get("updated") or ()))
    return fetched, done, moved


def _count_index_bytes(n: int) -> None:
    """Add ``n`` to the bytes the index refreshes downloaded in all."""
    if n <= 0:
        return
    try:
        store = _store()
        store.set_config(CONFIG_INDEX_BYTES, int(store.get_config(CONFIG_INDEX_BYTES, 0) or 0)
                         + int(n))
    except Exception:                                   # noqa: BLE001
        pass


# ── Track derivation ──────────────────────────────────────────────────────────

def _uade_name_for(token: str) -> str | None:
    try:
        from soniqboom.core import uade_formats as uf
        p = uf.player_map().get(token.lower())
        return uf.display_name(p) if p else None
    except Exception:                                   # noqa: BLE001
        return None


def format_for(rel: str) -> str:
    """The format name a catalogue file is listed with before it is played
    (the extractor's names — ``metadata.FORMAT_NAMES``, uade's player names;
    the first play replaces it with what the file itself says)."""
    from soniqboom.core import metadata
    from soniqboom.core import uade_formats as uf
    name = rel.rsplit("/", 1)[-1]
    top = rel.split("/", 1)[0]
    ext = os.path.splitext(name)[1].lower()
    if ext in metadata.SID_EXTS:
        if ext == ".sid" and top.startswith("SidMon"):
            return _uade_name_for("sid") or "SidMon"       # Amiga SidMon modules
        return "SID"                                       # C64 (HVSC, RealSID …)
    if ext == ".psf" and top == "SoundFactory":
        return _uade_name_for("psf") or "SoundFactory"
    if ext == ".dsf":
        return "DSF (Dreamcast)"                           # Modland's .dsf are Dreamcast rips
    if ext == ".imf" and top != "Imago Orpheus":
        return "AdLib IMF"
    if ext in metadata.SUPPORTED_EXTENSIONS and ext not in metadata._UADE_SUFFIX_EXTS:
        return metadata.FORMAT_NAMES.get(ext, ext.lstrip(".").upper())
    cls = uf.classify(name)
    if cls:
        return uf.display_name(cls[0])
    tok = folder_player_token(rel)
    if tok:
        return uf.display_name(uf.player_map()[tok])
    return metadata.FORMAT_NAMES.get(ext, "")


def title_for(rel: str) -> str:
    """The title a catalogue file is listed with: its file name without the
    extension — the name BODY of a prefix-form Amiga name (``mdat.foo`` →
    ``foo``); HVSC writes spaces as underscores."""
    from soniqboom.core import scene_metadata as sm
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith("HVSC/"):
        stem = name.rsplit(".", 1)[0] if "." in name else name
        return stem.replace("_", " ").strip() or name
    stem = sm._modland_stem(name)
    return (stem or name).strip() or name


def credit_for(rel: str, fmt: str | None) -> str:
    """The composer credit of a catalogue file: Modland's author dir
    (``scene_metadata.modland_credit`` — "- unknown" gives none); for the
    mirrored HVSC tree its MUSICIANS folder, as HVSC spells it
    ("Bayliss_Richard" → "Bayliss Richard")."""
    from soniqboom.core import scene_metadata as sm
    parts = rel.split("/")
    if parts[0] == "HVSC":
        if len(parts) >= 5 and parts[1] == "MUSICIANS":
            return parts[3].replace("_", " ").strip()
        return ""
    return sm.modland_credit(sm.parse_modland_path(rel), fmt) or ""


def _siblings_map() -> dict:
    """``scene_metadata.siblings_from_paths`` over the catalogue's index (the
    file-name game's sibling evidence).  Blocking the first time per index."""
    global _siblings
    if _siblings is not None:
        return _siblings
    from soniqboom.core import scene_metadata as sm
    con = _ro()
    if con is None:
        return {}
    gen = _db_gen
    try:
        sib = sm.siblings_from_paths(r for r, _s in _all_rels(con))
    except sqlite3.Error:
        return {}                         # (not cached: the next call tries again)
    if gen == _db_gen:
        _siblings = sib
    return sib


def apply_path_fields(t: dict, rel: str, *, siblings: dict | None,
                      use_filename: bool) -> dict:
    """Set the Modland fields a catalogue track takes from its path on ``t``
    (in place): composer credit when ``t`` names none, the game album / game
    names (``scene_metadata.album_update_for`` — fill-only, a header album
    wins), ``scene_path``.  Returns ``t``."""
    from soniqboom.core import scene_metadata as sm
    mp = sm.parse_modland_path(rel)
    from soniqboom.core.folder_album import _ARTIST_PLACEHOLDERS
    if (not (t.get("artist") or "").strip()
            or (t.get("artist") or "").strip().lower() in _ARTIST_PLACEHOLDERS):
        credit = credit_for(rel, t.get("format"))
        if credit:
            t["artist"] = credit
    want = sm.modland_game_for(t, mp, use_filename=use_filename, siblings=siblings,
                               modland_dir=rel.rpartition("/")[0])
    alb = sm.album_update_for(t, mp, use_filename=use_filename, siblings=siblings,
                              modland_dir=rel.rpartition("/")[0], want=want)
    if alb:
        t.update(alb)
    for f, v in sm.modland_game_slots(*want).items():
        if v:
            t[f] = v
        else:
            t.pop(f, None)
    t["scene_path"] = rel
    return t


def family_genre(rel: str) -> list[str] | None:
    """The format words the scanner's extractor stamps on a file of this kind
    (``metadata``: SID → Chiptune / C64, tracker → Tracker / Module, uade →
    Amiga / Module …) — what the web player and the stream route tell a
    module family by, so a catalogue tune is one before its first play
    (parity with local files).  None for kinds without one."""
    from soniqboom.core import metadata
    from soniqboom.core import uade_formats as uf
    name = rel.rsplit("/", 1)[-1]
    top = rel.split("/", 1)[0]
    ext = os.path.splitext(name)[1].lower()
    if ext in metadata.SID_EXTS:
        if ext == ".sid" and top.startswith("SidMon"):
            return ["Amiga", "Module"]
        return ["Chiptune", "C64"]
    if ext == ".psf" and top == "SoundFactory":
        return ["Amiga", "Module"]
    if ext == ".dsf":
        return ["Chiptune", "Game Rip"]                    # Modland's .dsf: Dreamcast rips
    if ext == ".imf" and top != "Imago Orpheus":
        return None                                        # AdLib IMF
    if ext in metadata.SUPPORTED_EXTENSIONS and ext not in metadata._UADE_SUFFIX_EXTS:
        if ext in metadata._TRACKER_EXTS:
            return ["Tracker", "Module"]
        if ext in metadata._ATARI_EXTS:
            return ["Chiptune", "Atari ST"]
        if ext in metadata._PSF_EXTS:
            return ["Chiptune", "Game Rip"]
        if ext in metadata._GME_EXTS:
            return ["Chiptune"]
        if ext in metadata._MIDI_EXTS:
            return ["MIDI"]
        return None
    if uf.classify(name) or folder_player_token(rel):
        return ["Amiga", "Module"]
    return None


def _finish_genres(t: dict, names: bool) -> None:
    from soniqboom.core import genre_derive
    from soniqboom.core.store import game_follow
    t.update(game_follow(t))
    g = genre_derive.derive(t, names=names, roots=(ROOT,))
    if g:
        t["genre_derived"] = g
    else:
        t.pop("genre_derived", None)


def build_track(rel: str, size: int, *, added_at: int, siblings: dict | None,
                use_filename: bool, names: bool, dir_hash: str, root_h: str) -> dict:
    """The library row of catalogue file ``rel`` — a SPARSE dict (fields at
    their model default are left out; every reader uses ``.get`` /
    ``TrackMeta`` defaults)."""
    fmt = format_for(rel)
    t = {
        "id": track_id(rel),
        "path": track_path(rel),
        "title": title_for(rel),
        "format": fmt,
        "file_size": int(size),
        "added_at": int(added_at),
        "dir_hash": dir_hash,
        "scan_root_hash": root_h,
        "is_duplicate_primary": True,
    }
    fam = family_genre(rel)
    if fam:
        t["genre"] = fam
    # (Left out at their defaults: artist "", duration 0 — length unknown
    # until the first play — and mtime 0, the "not merged" mark.)
    apply_path_fields(t, rel, siblings=siblings, use_filename=use_filename)
    _finish_genres(t, names)
    from soniqboom.core.store import share_values
    return share_values(t)


def _hvsc_left_out() -> bool:
    """The online HVSC collection is on with "Leave Modland's HVSC folder out"
    (``core/online_hvsc``): Modland's ``HVSC/`` mirror is left out of the
    catalogue (parked like a narrower scope's tunes)."""
    try:
        st = _store()
        if not (bool(st.get_config("online_hvsc_enabled", False))
                and bool(st.get_config("online_hvsc_replace_modland", True))):
            return False
        # Only while the online collection HAS the tunes (a switch-on that
        # failed — 6581.nl unreachable — leaves Modland's folder in).
        from soniqboom.core import online_hvsc
        return online_hvsc.HVSC_ONLINE.tune_count() > 0
    except Exception:                                   # noqa: BLE001
        return False


def _left_out_tops(scope_: str) -> frozenset:
    """The top folders the scope (and the HVSC option) leave out."""
    tops = set(PC_TRACKER_DIRS) if scope_ == SCOPE_STANDARD else set()
    if _hvsc_left_out():
        tops.add("HVSC")
    return frozenset(tops)


def _wanted_rows(scope_: str) -> list[tuple[str, int, int]]:
    """``(rel, size, seen)`` of every catalogue track the scope lists.
    Blocking."""
    con = _ro()
    if con is None:
        return []
    skip = _left_out_tops(scope_)
    try:
        rows = list(_all_rels(con, KIND_MUSIC, seen=True))
    except sqlite3.OperationalError:                    # an index from before "seen"
        rows = [(rel, size, 0) for rel, size in _all_rels(con, KIND_MUSIC)]
    return [row for row in rows if not skip or row[0].split("/", 1)[0] not in skip]


def _dir_hash_of(rel: str, memo: dict) -> str:
    import hashlib
    d = "/" + rel.rpartition("/")[0]
    h = memo.get(d)
    if h is None:
        h = memo[d] = hashlib.sha256(d.encode()).hexdigest()[:16]
    return h


# ── Applying the index to the library ─────────────────────────────────────────

async def _slices(items, fn, *, slice_s: float = _SLICE_S):
    """``fn(item)`` for every item, yielding to the event loop every
    ``slice_s`` seconds; yields lists of results ``_UPSERT_CHUNK`` long."""
    out: list = []
    t0 = time.perf_counter()
    for it in items:
        r = fn(it)
        if r is not None:
            out.append(r)
        if len(out) >= _UPSERT_CHUNK:
            yield out
            out = []
            t0 = time.perf_counter()
            continue
        if time.perf_counter() - t0 >= slice_s:
            await asyncio.sleep(0)
            t0 = time.perf_counter()
    if out:
        yield out


def _progress(phase: str, done: int, total: int) -> None:
    _status.update(phase=phase, done=done, total=total)


async def apply_index() -> dict:
    """Bring the library's catalogue tracks in line with the index and the
    scope: add the missing, remove the unlisted, rewrite the resized (and all
    of them when ``RULES_VERSION`` changed).  Store writes in chunks with
    event-loop turns between them, in one batch section; the set work runs
    in a worker thread.  The cyclic GC is paused while it runs (a full
    collection over a growing library is the long stall: ~0.5 s at 300k
    tracks) and the new long-lived heap is frozen at the end
    (``store.freeze_long_lived_heap``'s reasoning).  Returns counts."""
    import gc
    from soniqboom.core import scanner
    from soniqboom.core.remote_cache import get_cache
    store = _store()
    loop = asyncio.get_running_loop()
    rh = root_hash()
    scope_ = scope()
    _progress("reading the index", 0, 0)
    rows = await loop.run_in_executor(None, _wanted_rows, scope_)
    if not rows:
        raise RuntimeError("the catalogue index lists no playable tunes")
    siblings = await loop.run_in_executor(None, _siblings_map)

    def _warm_titles() -> None:
        from soniqboom.core import game_titles
        game_titles.get_index().squashed()       # the file-name game's spellings
    try:
        await loop.run_in_executor(None, _warm_titles)
    except Exception:                                   # noqa: BLE001
        log.debug("game-title lists unavailable for the catalogue", exc_info=True)
    from soniqboom.core import genre_derive, scene_metadata as sm
    use_filename = sm.filename_game_enabled()
    names = genre_derive.names_enabled()
    current = store.get_track_ids_for_scan_root(rh)          # (a copy)
    try:
        baseline = int(float(index_info().get("baseline") or 0))
    except (TypeError, ValueError):
        baseline = 0
    parked = await loop.run_in_executor(None, _load_parked_rows)
    parked_used: list = []
    rules_changed = store.get_config(CONFIG_RULES) != RULES_VERSION and bool(current)
    tracks = store._tracks

    def _plan() -> tuple:
        want = {track_id(rel): (rel, size, seen) for rel, size, seen in rows}
        to_add = [tid for tid in want if tid not in current]
        to_remove = [tid for tid in current if tid not in want]
        changed = [tid for tid, (_rel, size, _seen) in want.items()
                   if tid in current and (rules_changed
                                          or (tracks.get(tid) or {}).get("file_size") != size)]
        dirs = sorted({"/" + want[tid][0].rpartition("/")[0] for tid in (*to_add, *changed)})
        # The removed tunes a narrower scope leaves out (they come back with
        # the wider one) — the others left Modland's index.
        pc = _left_out_tops(scope_)
        cut = len(ROOT) + 2
        to_park = [tid for tid in to_remove
                   if pc and ((tracks.get(tid) or {}).get("path") or "")[cut:].split("/", 1)[0]
                   in pc]
        return want, to_add, to_remove, changed, dirs, to_park
    want, to_add, to_remove, changed, dirs, to_park = await loop.run_in_executor(None, _plan)
    total = len(to_add) + len(to_remove) + len(changed)
    result = {"added": 0, "removed": 0, "changed": 0, "tracks": len(want),
              "scope": scope_}
    if not total:
        if store.get_config(CONFIG_RULES) != RULES_VERSION:
            store.set_config(CONFIG_RULES, RULES_VERSION)
        _hvsc_reconcile_soon()
        await _refill_quietly()
        return result
    memo: dict = {}
    big = total >= 20_000
    gc_was = gc.isenabled()
    if big:
        gc.disable()
    try:
        # Folder hashes resolvable like a scanned share's (``_hash_lookups``).
        for i in range(0, len(dirs), 5_000):
            store.store_hash_lookups_batch(dirs[i:i + 5_000])
            await asyncio.sleep(0)
        done = 0
        _progress("adding tunes", 0, total)
        store.enter_batch_mode()
        try:
            def _mk(tid: str) -> dict | None:
                rel, size, seen = want[tid]
                t = build_track(rel, size, added_at=_added_at(seen, baseline),
                                siblings=siblings, use_filename=use_filename, names=names,
                                dir_hash=_dir_hash_of(rel, memo), root_h=rh)
                old = parked.get(tid) if parked else None
                if old is not None and old.get("file_size") == size and is_merged(old):
                    t = _rederive_merged(old, t)          # read on a play before
                    parked_used.append(tid)
                return t
            async for chunk in _slices(to_add, _mk):
                store.upsert_tracks_batch(chunk)
                done += len(chunk)
                result["added"] += len(chunk)
                _progress("adding tunes", done, total)
                _young_gc(big, done)
                await asyncio.sleep(0)
            if changed:
                _progress("updating tunes", done, total)
                cache = get_cache()

                def _mk_changed(tid: str) -> dict | None:
                    old = tracks.get(tid)
                    rel, size, seen = want[tid]
                    keep_added = (old or {}).get("added_at") or _added_at(seen, baseline)
                    if old is not None and old.get("file_size") != size:
                        # The file changed upstream: its cached copy is stale.
                        try:
                            cache.validate_size(ROOT, "/" + rel, size)
                        except Exception:               # noqa: BLE001
                            pass
                    t = build_track(rel, size, added_at=keep_added, siblings=siblings,
                                    use_filename=use_filename, names=names,
                                    dir_hash=_dir_hash_of(rel, memo), root_h=rh)
                    if old is not None and old.get("file_size") == size and is_merged(old):
                        # Same file, new derivation rules: what its play read stays.
                        t = _rederive_merged(old, t)
                    return t
                async for chunk in _slices(changed, _mk_changed):
                    store.upsert_tracks_batch(chunk)
                    done += len(chunk)
                    result["changed"] += len(chunk)
                    _progress("updating tunes", done, total)
                    _young_gc(big, done)
                    await asyncio.sleep(0)
            if to_remove:
                _progress("removing tunes", done, total)
                try:
                    if to_park:
                        await _park_user_refs(to_park)
                except Exception:                       # noqa: BLE001
                    log.warning("Modland catalogue: playlist / played-tune bookkeeping "
                                "failed", exc_info=True)
                base = done

                def _removed(n: int) -> None:
                    result["removed"] = n
                    _progress("removing tunes", base + n, total)
                await _delete_sliced(store, to_remove, _removed)
                done = base + len(to_remove)
        finally:
            _progress("sorting", done, total)
            if big:
                store._upserts_since_freeze = 0         # frozen below instead
                gc.collect(1)        # the young garbage (other requests' cycles)
            await scanner._async_exit_batch_mode(store)
    finally:
        if big:
            # The new rows and sort lists are long-lived: out of the cyclic
            # GC's reach (no full collect first — that is the very stall; the
            # young generations were collected chunk by chunk).
            gc.freeze()
            if gc_was:
                gc.enable()
    # The plan's hundreds of thousands of ids / paths, freed a slice per turn
    # (dropped at once, their deallocation was a ~0.2 s stall).
    await _drop_gradually([rows, want, to_add, to_remove, changed, dirs, memo, to_park])
    del rows, want, to_add, to_remove, changed, dirs, memo, to_park
    if parked_used:
        await loop.run_in_executor(None, _drop_parked_rows, parked_used)
    del parked
    _unpark_returned_ids(store)
    store.set_config(CONFIG_RULES, RULES_VERSION)
    await _after_library_change()
    log.info("Modland catalogue: %d added, %d updated, %d removed (%d tunes, scope %s)",
             result["added"], result["changed"], result["removed"], result["tracks"], scope_)
    _hvsc_reconcile_soon()
    await _refill_quietly()
    return result


def _hvsc_reconcile_soon() -> None:
    """The online HVSC collection moves ratings, plays and playlist entries
    to whichever HVSC copy is in the library (``online_hvsc.reconcile_user_data``)."""
    try:
        from soniqboom.core import online_hvsc
        online_hvsc.schedule_reconcile()
    except Exception:                                   # noqa: BLE001
        log.debug("HVSC user-data reconcile not scheduled", exc_info=True)


async def _refill_quietly() -> None:
    try:
        await refill_tune_counts()
    except Exception:                                   # noqa: BLE001
        log.warning("Modland catalogue: tune counts not filled", exc_info=True)


def _tune_counts(rels: dict) -> dict:
    """``rel → (tune count, first tune)`` of the catalogue files ``rels``
    (rel → is it an Amiga module) holding more than one tune, from what is
    known WITHOUT fetching them: Modland's checksum list (the scene index,
    ``scene_metadata``) joined with the song database's tune lengths (Amiga
    modules) and HVSC's Songlengths (C64 SIDs).  Empty when
    the checksum list isn't installed.  Blocking."""
    from soniqboom.core import scene_metadata as sm, songdb
    out: dict = {}
    if not rels or not sm.has_index():
        return out
    md5s: dict = {}
    con = sm._connect_ro(sm._db_path())
    try:
        for md5, path in con.execute("SELECT md5, path FROM mods"):
            if path in rels:
                md5s[path] = (md5 or "").lower()
    finally:
        con.close()
    if not md5s:
        return out
    if songdb.has_index():
        by_key: dict = {}
        for rel, md5 in md5s.items():
            if md5 and not rel.startswith("HVSC/"):
                by_key.setdefault(md5[:12], []).append(rel)
        keys = list(by_key)
        scon = songdb._connect_ro(songdb._db_path())
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                for key, minsub, subs in scon.execute(
                        f"SELECT key, minsub, subsongs FROM lengths WHERE key IN "
                        f"({','.join('?' * len(chunk))})", chunk):
                    n = len((subs or "").split())
                    if n > 1:
                        for rel in by_key.get(key, ()):
                            out[rel] = (n, int(minsub or 0))
        finally:
            scon.close()
    try:
        from soniqboom.core.hvsc import get_hvsc
        hv = get_hvsc()
        if hv.is_configured():
            for rel, md5 in md5s.items():
                if rel.startswith("HVSC/") and md5:
                    n = len(hv.lookup_durations_by_md5(md5))
                    if n > 1:
                        out[rel] = (n, 1)
    except Exception:                                   # noqa: BLE001
        log.debug("HVSC tune counts unavailable", exc_info=True)
    return out


async def refill_tune_counts() -> int:
    """Give the unplayed catalogue tunes their tune count (the list's "N
    tunes" badge, the Track Info tune list) from ``_tune_counts`` — before
    a play, as a scanned file has it.  Run after a build / refresh, at start
    and when the checksum list, song database or HVSC changes.  Returns the
    number of tunes updated."""
    store = _store()
    if not enabled():
        return 0
    rh = root_hash()
    cut = len(ROOT) + 2
    want: dict = {}
    t0 = time.perf_counter()
    for tid in list(store.get_track_ids_for_scan_root(rh)):
        if time.perf_counter() - t0 >= _SLICE_S:
            await asyncio.sleep(0)          # (400k rows: in slices of the loop)
            t0 = time.perf_counter()
        t = store._tracks.get(tid)
        if not t or is_merged(t):
            continue                        # a played tune has what the file says
        rel = t["path"][cut:]
        amiga = (t.get("genre") or [None])[0] == "Amiga"
        # Only kinds a scan counts the tunes of (uade modules, SIDs) — a
        # tracker's count would vanish on its first play, as a scanned
        # tracker shows none.
        if not (amiga or rel.startswith("HVSC/")):
            continue
        want[rel] = (amiga, tid, t.get("subsongs"), t.get("subsong_base"))
    if not want:
        return 0
    loop = asyncio.get_running_loop()
    counts = await loop.run_in_executor(None, _tune_counts, want)
    items = []
    for rel, (n, first) in counts.items():
        amiga, tid, have, base = want[rel]
        patch = {}
        if have != n:
            patch["subsongs"] = n
        # uade numbers a module's tunes from its first subsong (``-g`` "min").
        if amiga and first > 0 and base != first:
            patch["subsong_base"] = first
        if patch:
            items.append((tid, patch))
    for i in range(0, len(items), 2_000):
        store.update_track_fields_batch(items[i:i + 2_000])
        await asyncio.sleep(0)
    if items:
        log.info("Modland catalogue: tune counts for %d tunes (checksum list, song "
                 "database, HVSC)", len(items))
    return len(items)


async def _delete_sliced(store, ids: list, on_progress=None) -> int:
    """``store.delete_track_ids`` over ``ids`` in steps of ``_DELETE_STEP``,
    yielding to the event loop whenever ``_SLICE_S`` is used up (a removal
    touches every index of the track).  ``on_progress(removed so far)`` after
    each slice.  Returns the count removed."""
    removed = 0
    i, n = 0, len(ids)
    while i < n:
        t0 = time.perf_counter()
        while i < n and time.perf_counter() - t0 < _SLICE_S:
            part = ids[i:i + _DELETE_STEP]
            i += len(part)
            removed += store.delete_track_ids(part)
        if on_progress is not None:
            on_progress(removed)
        await asyncio.sleep(0)
    return removed


def _young_gc(big: bool, done: int) -> None:
    """While a big build has the automatic GC paused: collect the youngest
    generation after every chunk and the middle one every ~10k rows — cyclic
    garbage of concurrent requests is freed as usual, and no collection ever
    walks the whole growing library."""
    if not big:
        return
    import gc
    gc.collect(1 if done % 10_000 < _UPSERT_CHUNK else 0)


async def _drop_gradually(objs: list, step: int = 20_000) -> None:
    """Empty the big lists / dicts in ``objs`` a slice per event-loop turn."""
    for o in objs:
        if isinstance(o, dict):
            while o:
                for _ in range(min(step, len(o))):
                    o.popitem()
                await asyncio.sleep(0)
        elif isinstance(o, list):
            while o:
                del o[-step:]
                await asyncio.sleep(0)


async def _after_library_change() -> None:
    """Browse caches and the derived-genre pass after a catalogue write."""
    try:
        from soniqboom.api import fstree
        fstree.invalidate_scan_root_cache(ROOT)
    except Exception:                                   # noqa: BLE001
        log.debug("fstree cache invalidation failed", exc_info=True)
    try:
        from soniqboom.core import genre_derive
        genre_derive.schedule()
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from soniqboom.api import library
        await library.broadcast_catalogue_changed(ROOT)
    except Exception:                                   # noqa: BLE001
        log.debug("catalogue-changed broadcast failed", exc_info=True)


def _parked_path() -> Path:
    return _dir() / "modland_played.json"


def _load_parked_rows() -> dict:
    """The rows of played (merged) catalogue tunes kept over a switch-off —
    ``id → row``.  Blocking."""
    try:
        data = json.loads(_parked_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {r["id"]: r for r in data if isinstance(r, dict) and r.get("id")} \
        if isinstance(data, list) else {}


def _save_parked_rows(rows: dict) -> None:
    """Blocking."""
    p = _parked_path()
    try:
        if not rows:
            p.unlink(missing_ok=True)
            return
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:12]}.tmp")
        tmp.write_text(json.dumps(list(rows.values())), "utf-8")
        os.replace(tmp, p)
    except OSError:
        log.debug("could not write %s", p, exc_info=True)


def _unpark_returned_ids(store) -> None:
    """Parked playlist ids whose tunes are back in the library are ordinary
    entries again."""
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    parked = store.get_config(PARKED_PLAYLIST_IDS) or []
    if parked:
        rest = [t for t in parked if t not in store._tracks]
        if len(rest) != len(parked):
            store.set_config(PARKED_PLAYLIST_IDS, rest)


def _drop_parked_rows(ids) -> None:
    """The parked rows of ``ids`` are back in the library.  Blocking."""
    rows = _load_parked_rows()
    for tid in ids:
        rows.pop(tid, None)
    _save_parked_rows(rows)


def _rederive_merged(old: dict, fresh: dict) -> dict:
    """A played tune's row ``old`` with the path-derived fields of ``fresh``
    (the index row the current rules build): what the play read — length,
    checksums, subsongs, title, tags — stays."""
    d = merged_track(fresh, old, "")
    d["mtime"] = old.get("mtime") or d["mtime"]
    rel = old["path"][len(ROOT) + 2:]
    credit = credit_for(rel, old.get("format"))
    if (rel.startswith("HVSC/") and old.get("sid_md5") and credit
            and (old.get("artist") or "") == credit):
        # An HVSC SID merged when HVSC's folder name ("Turner Steve") replaced
        # the author its header names: read it again on its next play — it
        # stays a played tune meanwhile (kept on switch-off and rebuilds).
        d[REREAD] = True
    if old.get("added_at"):
        d["added_at"] = old["added_at"]
    from soniqboom.core.store import share_values
    return share_values(d)


async def _park_user_refs(ids) -> None:
    """Before catalogue tracks go (switch-off, a narrower scope, gone from
    the index): remember which of them playlists list
    (``PARKED_PLAYLIST_IDS`` — a playlist saved while they are gone keeps
    those entries) and keep the played tunes' rows aside
    (``_save_parked_rows``) for when they come back."""
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    store = _store()
    ids = ids if isinstance(ids, (set, frozenset)) else set(ids)
    refs: set = set()
    for pl in store.list_playlists():                   # (on the loop: few entries)
        for e in pl.get("track_ids") or ():
            tid = e.get("id") if isinstance(e, dict) else e
            if tid in ids:
                refs.add(tid)
    prev = set(store.get_config(PARKED_PLAYLIST_IDS) or ())
    if refs - prev:
        store.set_config(PARKED_PLAYLIST_IDS, sorted(refs | prev))
    tracks = store._tracks

    def _played() -> None:
        rows = {}
        for tid in ids:
            t = tracks.get(tid)
            if t is not None and (t.get("mtime") or 0) > 0:
                rows[tid] = dict(t)
        if rows:
            kept = _load_parked_rows()
            kept.update(rows)
            _save_parked_rows(kept)
    await asyncio.get_running_loop().run_in_executor(None, _played)


async def remove_all(*, progress: bool = True) -> int:
    """Delete every catalogue track (chunks, one batch section).  Ratings,
    play counts, stars and playlist entries are keyed by id and stay (a
    playlist saved meanwhile keeps its catalogue entries); a played tune's
    row is kept aside for when it comes back."""
    from soniqboom.core import scanner
    store = _store()
    ids = list(store.get_track_ids_for_scan_root(root_hash()))
    if not ids:
        return 0
    try:
        await _park_user_refs(ids)
    except Exception:                                   # noqa: BLE001
        log.warning("Modland catalogue: playlist / played-tune bookkeeping failed",
                    exc_info=True)
    removed = 0
    store.enter_batch_mode()
    try:
        removed = await _delete_sliced(
            store, ids,
            (lambda n: _progress("removing tunes", n, len(ids))) if progress else None)
    finally:
        await scanner._async_exit_batch_mode(store)
    await _after_library_change()
    return removed


# ── The catalogue's file source ───────────────────────────────────────────────

class _HeldOffline(ConnectionError):
    """A catalogue read while the source holds itself offline."""


class CatalogueFTPSource(FTPFileSource):
    """Modland's ``/pub/modules`` for the catalogue.  Listings, stats and
    folder checks come from the index — never the network, so browsing, the
    share health monitor and companion lookups cost Modland nothing.  A file
    is read from the archive only inside a play's fetch window
    (``catalogue.allowed``), on an on-demand pool of at most ``POOL_CAP``
    anonymous logins that are logged out ``_IDLE_LOGOUT_S`` after the last
    read.  A network failure on a fetch holds the source offline for a while
    (``get_source`` → None: uncached plays answer "share unavailable",
    cached ones play) — the next play after the hold tries again."""

    is_catalogue = True

    def __init__(self) -> None:
        super().__init__(CONNECT_HOST, username=FTP_USER, password=FTP_PASSWORD,
                         port=CONNECT_PORT, remote_path=REMOTE_PATH)
        self._idle_timer: threading.Timer | None = None
        self._idle_lock = threading.Lock()
        self._offline_until = 0.0
        self._offline_streak = 0

    @property
    def _pool(self):
        from soniqboom.core.filesource import ensure_fixed_ftp_pool
        return ensure_fixed_ftp_pool(self._host, self._port, self._username,
                                     self._password, self._encoding, POOL_CAP)

    # Offline hold (circuit breaker) ------------------------------------------
    def is_offline(self) -> bool:
        from soniqboom.core import netpolicy
        return netpolicy.blocked(self._host) or time.monotonic() < self._offline_until

    def _note_network_failure(self, exc: BaseException) -> None:
        if not isinstance(exc, Exception):               # cancelled, exiting
            return
        if isinstance(exc, (catalogue.CatalogueFetchRefused, FileNotFoundError,
                            _HeldOffline)):
            return
        if isinstance(exc, TimeoutError) and "acquire timed out" in str(exc):
            return                       # our own connection pool was busy, not Modland
        import ftplib
        if isinstance(exc, ftplib.error_perm) and str(exc)[:3] == "550":
            return                                       # that file, not the server
        self._offline_streak += 1
        hold = min(_OFFLINE_HOLD_MAX_S,
                   _OFFLINE_HOLD_S * (2 ** min(self._offline_streak - 1, 4)))
        self._offline_until = time.monotonic() + hold
        log.warning("Modland unreachable (%s: %s) — treating it as offline for %d s",
                    type(exc).__name__, exc, hold)
        _schedule_status_flip("unavailable")
        _schedule_status_flip("ok", after=hold + 0.25)   # (unless still / again offline)

    def _note_network_ok(self) -> None:
        if self._offline_streak or self._offline_until:
            self._offline_streak = 0
            self._offline_until = 0.0
        _schedule_status_flip("ok")           # (a no-op unless it is marked unavailable)

    def _rel_of(self, path: str) -> str:
        return catalogue.norm_rel(ROOT, path, self._remote_path)

    def _gate(self, path: str, op: str) -> str:
        rel = self._rel_of(path)
        if not catalogue.allowed(ROOT, rel):
            catalogue.note_refused(ROOT, rel, op)
            raise catalogue.CatalogueFetchRefused(
                f"Modland catalogue: {rel} is fetched only when it is played")
        if self.is_offline():
            # (A retry inside the failed fetch: no new connection attempt.)
            raise _HeldOffline("Modland can't be reached right now")
        return rel

    def _arm_idle_logout(self) -> None:
        with self._idle_lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            t = threading.Timer(_IDLE_LOGOUT_S, self._idle_logout)
            t.daemon = True
            self._idle_timer = t
            t.start()

    def _idle_logout(self) -> None:
        try:
            self._pool.recycle_all_idle()
        except Exception:                               # noqa: BLE001
            pass

    # Index-backed (no network) ------------------------------------------------
    def list_dir(self, path: str) -> list[DirEntry]:
        return list_dir(self._rel_of(path))

    def _list_entries(self, path: str, lane: str = "scan", *,
                      error_sink: "list | None" = None) -> list[DirEntry]:
        return list_dir(self._rel_of(path))

    def stat(self, path: str, *, lane: str = "scan", strict: bool = False) -> FileStat:
        rel = self._rel_of(path)
        e = entry(rel)
        if e is not None:
            return FileStat(size=e[0], mtime=0.0, is_dir=False)
        if _is_dir(rel):
            return FileStat(size=0, mtime=0.0, is_dir=True)
        raise FileNotFoundError(errno.ENOENT, "not in the Modland index", rel)

    def is_dir(self, path: str) -> bool:
        return _is_dir(self._rel_of(path))

    def walk(self, top: str):
        raise catalogue.CatalogueFetchRefused("the Modland catalogue is never walked")

    def walk_with_stat(self, top: str, **_kw):
        raise catalogue.CatalogueFetchRefused("the Modland catalogue is never walked")

    def reconnect(self) -> bool:
        # No probe login: drop idle connections; the next fetch logs in.
        try:
            self._pool.recycle_all_idle()
        except Exception:                               # noqa: BLE001
            pass
        return True

    # Gated network reads ------------------------------------------------------
    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        rel = self._gate(path, "read")
        try:
            data = super().read_file(path, lane=lane)
        except BaseException as exc:
            self._note_network_failure(exc)
            raise
        self._note_network_ok()
        catalogue.note_read(ROOT, rel, "read", len(data))
        self._arm_idle_logout()
        return data

    def read_partial(self, path: str, max_bytes: int, *, lane: str = "scan") -> bytes:
        rel = self._gate(path, "partial read")
        try:
            data = super().read_partial(path, max_bytes, lane=lane)
        except BaseException as exc:
            self._note_network_failure(exc)
            raise
        self._note_network_ok()
        catalogue.note_read(ROOT, rel, "partial read", len(data))
        self._arm_idle_logout()
        return data

    def read_at(self, path: str, offset: int, length: int, *, lane: str = "scan") -> bytes:
        rel = self._gate(path, "range read")
        try:
            data = super().read_at(path, offset, length, lane=lane)
        except BaseException as exc:
            self._note_network_failure(exc)
            raise
        self._note_network_ok()
        catalogue.note_read(ROOT, rel, "range read", len(data))
        self._arm_idle_logout()
        return data

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None):
        rel = self._gate(path, "download")
        try:
            st = super().open_stream(path, offset=offset, lane=lane, length=length)
        except BaseException as exc:
            self._note_network_failure(exc)
            raise
        self._note_network_ok()
        catalogue.note_read(ROOT, rel, "download" if not offset else f"resume@{offset}",
                            None)
        return _CountingStream(st, self)


class _CountingStream:
    """A ``SourceStream`` wrapper counting the bytes a catalogue download
    moved (status / tests) and arming the idle logout when it closes."""

    def __init__(self, inner, src: CatalogueFTPSource) -> None:
        self._inner = inner
        self._src = src
        self.size = getattr(inner, "size", None)

    def read(self, n: int = 256 * 1024) -> bytes:
        data = self._inner.read(n)
        if data:
            catalogue.note_bytes(len(data))
        return data

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._src._arm_idle_logout()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _capture_loop() -> None:
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def _schedule_status_flip(status: str, *, after: float = 0.0) -> None:
    """Mark the catalogue root ok / unavailable on the server's loop (from any
    thread), now or ``after`` seconds from now.  "ok" is skipped while the
    source still holds itself offline."""
    loop = _loop
    if loop is None or loop.is_closed():
        return

    def _flip() -> None:
        try:
            if not catalogue.is_catalogue_root(ROOT):
                return
            if status == "ok":
                from soniqboom.core.filesource import _active_sources
                src = _active_sources.get(ROOT)
                if src is not None and getattr(src, "is_offline", lambda: False)():
                    return
            sd = _store()._scan_dirs.get(ROOT)
            if sd is not None and (sd.get("status") or "ok") != status:
                _store().set_scan_dir_status(ROOT, status)
        except Exception:                               # noqa: BLE001
            pass

    def _arm() -> None:
        if after > 0:
            loop.call_later(after, _flip)
        else:
            _flip()
    try:
        loop.call_soon_threadsafe(_arm)
    except RuntimeError:                                # loop closed
        pass


# ── Registration ──────────────────────────────────────────────────────────────

def _register_root() -> None:
    """Make the catalogue root known (catalogue registry + file source) — no
    network."""
    from soniqboom.core.filesource import _active_sources, register_source
    _capture_loop()
    catalogue.register_root(ROOT, key=KEY, label=LABEL, is_music=is_music,
                            subsonic_visible=subsonic_visible)
    src = _active_sources.get(ROOT)
    if not isinstance(src, CatalogueFTPSource):
        register_source(ROOT, CatalogueFTPSource())


def _unregister_root() -> None:
    from soniqboom.core.filesource import remove_source
    remove_source(ROOT)
    catalogue.unregister_root(ROOT)


def _ensure_scan_dir() -> None:
    store = _store()
    store.store_hash_lookup(ROOT)
    sd = store._scan_dirs.get(ROOT)
    if sd is None:
        store.upsert_scan_dir(ROOT, status="ok")


def conflicting_share() -> str | None:
    """The configured network share that already uses the catalogue's root
    (its tracks would collide with the catalogue's), or None."""
    try:
        from soniqboom.config import load_local_conf
        from soniqboom.core.filesource import scan_root_for_share
        shares = (load_local_conf() or {}).get("network_shares") or {}
    except Exception:                                   # noqa: BLE001
        # The shares can't be read: assume one may own the root (nothing of
        # it is touched) rather than none.
        log.warning("Modland catalogue: the server configuration can't be read", exc_info=True)
        return "(unreadable server configuration)"
    for sid, sh in (shares.items() if isinstance(shares, dict) else []):
        try:
            sr = scan_root_for_share(sh)
        except Exception:                               # noqa: BLE001
            continue
        if (sr or "").rstrip("/").lower() == ROOT.lower():
            return sid
    return None


def _check_no_conflict() -> None:
    sid = conflicting_share()
    if sid:
        raise RuntimeError(
            f"Modland's /pub/modules is already added as the network share "
            f"\"{sid}\" — remove that share first")


# ── Jobs ──────────────────────────────────────────────────────────────────────

def status() -> dict:
    out = dict(_status)
    info = index_info()
    out["enabled"] = enabled()
    out["scope"] = scope()
    out["subsonic"] = subsonic_visible()
    out["index_date"] = info.get("index_date")
    try:
        out["index_rows"] = int(info.get("rows") or 0)
        out["music_rows"] = int(info.get("music_rows") or 0)
        out["scope_counts"] = {SCOPE_STANDARD: int(info.get("standard_rows") or 0),
                               SCOPE_ALL: out["music_rows"]}
        out["last_refresh"] = float(info.get("built_at") or 0) or None
    except (TypeError, ValueError):
        out["index_rows"] = out["music_rows"] = 0
        out["scope_counts"] = {}
        out["last_refresh"] = None
    out["tunes"] = _tune_count()
    try:
        kind = info.get("last_kind") or None
        out["last_update"] = {"kind": kind,
                              "changelogs": int(info.get("last_changelogs") or 0),
                              "bytes": int(float(info.get("last_bytes") or 0)),
                              "reason": (info.get("last_reason") or None) if kind == "full"
                              else None}
    except (TypeError, ValueError):
        out["last_update"] = {"kind": None, "changelogs": 0, "bytes": 0, "reason": None}
    # The newest day of Modland's changes the index holds (the index's own
    # date is the last full one's).
    try:
        _since, _applied = _cl_state(info)
        out["changes_to"] = max(_applied) if _applied else None
    except Exception:                                   # noqa: BLE001
        out["changes_to"] = None
    try:
        out["index_bytes"] = int(_store().get_config(CONFIG_INDEX_BYTES, 0) or 0)
    except Exception:                                   # noqa: BLE001
        out["index_bytes"] = 0
    lr = out["last_refresh"]
    out["interval"] = interval()
    out["tune_details"] = tune_details()
    out["intervals"] = dict(INTERVAL_LABELS)
    out["next_refresh"] = _next_due() if (lr and out["enabled"]) else None
    out["root"] = ROOT
    out["label"] = LABEL
    out["site"] = SITE_URL
    out["scopes"] = {SCOPE_STANDARD: f"Everything except the large PC tracker folders "
                                     f"({', '.join(PC_TRACKER_DIRS)})",
                     SCOPE_ALL: "Everything"}
    try:
        from soniqboom.core.filesource import _active_sources
        src = _active_sources.get(ROOT)
        out["offline"] = bool(src is not None and getattr(src, "is_offline", lambda: False)())
    except Exception:                                   # noqa: BLE001
        out["offline"] = False
    out["fetches"] = {k: v for k, v in catalogue.stats().items()
                      if k in ("reads", "bytes", "refused", "rate_limited")}
    return out


async def _run_job(name: str, body) -> dict:
    """Run job ``body`` under the job lock (one at a time).  Every job is
    cancellable by a switch-off (``start_disable``); a job other than the
    switch-off that gets the lock after the catalogue was switched off does
    nothing."""
    me = asyncio.current_task()
    if me is not None:
        _job_tasks.add(me)
    try:
        async with _get_job_lock():
            if name not in ("enable", "disable") and not enabled():
                return status()
            _status.update(state="building" if name != "disable" else "removing",
                           error=None, started_at=time.time(), last_attempt=time.time(),
                           phase="", done=0, total=0)
            try:
                res = await body()
            except asyncio.CancelledError:
                _status.update(state="ready" if enabled() and has_index() else "off",
                               phase="")
                raise
            except Exception as exc:                    # noqa: BLE001
                log.warning("Modland catalogue %s failed: %s", name, exc)
                _note_retry_after(exc)
                _status.update(error=f"{exc}", phase="",
                               shrink_refused=isinstance(exc, IndexShrank),
                               state=("ready" if enabled() and _has_tracks() else
                                      ("error" if enabled() else "off")))
                return status()
            _status.update(state="ready" if enabled() else "off", phase="",
                           last_result=res, shrink_refused=False)
            return status()
    finally:
        if me is not None:
            _job_tasks.discard(me)


def _tune_count() -> int:
    """The catalogue tracks in the library (the store's live per-root set —
    counted, not copied); 0 while a network share owns the root (its tracks
    are the share's)."""
    try:
        n = len(_store()._tag_scan_root_hash.get(root_hash()) or ())
    except Exception:                                   # noqa: BLE001
        return 0
    return n if n and not conflicting_share() else 0


def _expected_count() -> int:
    """How many tunes the index lists for the current scope (what a finished
    build holds; -1 when unknown)."""
    info = index_info()
    try:
        n = int(info.get("standard_rows" if scope() == SCOPE_STANDARD
                         else "music_rows"))
    except (TypeError, ValueError):
        return -1
    if _hvsc_left_out():
        n -= _hvsc_music_rows()
    return n


def _hvsc_music_rows() -> int:
    """Music files the index lists under ``HVSC/``."""
    con = _ro()
    if con is None:
        return 0
    try:
        return int(con.execute(
            "SELECT COUNT(*) FROM files f JOIN dirs d ON f.dir = d.id "
            "WHERE f.kind = ? AND (d.dir = 'HVSC' OR d.dir LIKE 'HVSC/%')",
            (KIND_MUSIC,)).fetchone()[0])
    except sqlite3.Error:
        return 0


def _has_tracks() -> bool:
    return _tune_count() > 0


def _index_fresh() -> bool:
    info = index_info()
    try:
        built = float(info.get("built_at") or 0)
    except (TypeError, ValueError):
        built = 0.0
    return (bool(built) and time.time() - built < interval_seconds()
            and info.get("fingerprint") == _fingerprint())


async def _ensure_index(*, force_download: bool, local_zip: Path | None = None,
                        accept_shrink: bool = False) -> dict | None:
    """A current index: the one on disk when fresh (and not forced), else a
    rebuild from the kept zip (classification rules changed), else a
    download.  Returns the new build's info, None when the old one stays."""
    loop = asyncio.get_running_loop()
    if local_zip is not None:
        _progress("reading the index", 0, 0)
        return await loop.run_in_executor(
            None, lambda: _refresh_index_files(download=False, local_zip=local_zip,
                                               accept_shrink=accept_shrink))
    if not force_download and has_index():
        if _index_fresh():
            return None
        info = index_info()
        if info.get("fingerprint") != _fingerprint() and zip_path().exists():
            _progress("re-reading the index", 0, 0)
            try:
                return await loop.run_in_executor(
                    None, lambda: _refresh_index_files(download=False))
            except Exception:                           # noqa: BLE001
                log.info("re-reading the kept Modland index failed — downloading",
                         exc_info=True)
    _progress("downloading the index", 0, 0)
    return await loop.run_in_executor(
        None, lambda: _refresh_index_files(download=True, accept_shrink=accept_shrink))


async def enable(*, local_zip: Path | None = None) -> dict:
    """Switch the catalogue on: index (download when needed — an index kept
    from an earlier switch-on that is no longer fresh catches up from the
    changelogs first), build, root."""
    async def body():
        _check_no_conflict()
        store = _store()
        store.set_config(CONFIG_ENABLED, True)
        _register_root()
        _start_refresh_loop()            # (also retries a first download that failed)
        res = await _update_then_apply(local_zip=local_zip)
        _ensure_scan_dir()
        return res
    return await _run_job("enable", body)


async def _update_then_apply(*, local_zip: Path | None = None) -> dict:
    """The index brought up to date (as little downloaded as possible), then
    applied — for a switch-on, a scope change and the startup repair: the
    library first follows pending moves; a kept index that is no longer
    fresh catches up from the changelogs (``_delta_refresh``, else
    ``_full_refresh``); when Modland can't be reached the kept index is used
    as it is (the status says the update failed; it is tried again an hour
    later).  A fresh kept index is used without a download."""
    await _run_pending_moves()
    if local_zip is None and has_index() and not _index_fresh():
        try:
            return await _delta_refresh()
        except NeedFullIndex as exc:
            log.info("Modland catalogue: downloading the full index (%s)", exc)
            try:
                return await _full_refresh(str(exc), exc.moves)
            except IndexShrank:
                raise
            except Exception as exc2:                   # noqa: BLE001
                err = exc2
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001
            err = exc
        log.warning("Modland catalogue: the index could not be updated (%s) — the kept "
                    "one is used", err)
        res = await apply_index()
        _status["error"] = f"{err}"
        _note_retry_after(err)
        return res
    info = await _ensure_index(force_download=False, local_zip=local_zip)
    _count_index_bytes(int((info or {}).get("downloaded") or 0))
    return await apply_index()


async def disable() -> dict:
    """Switch the catalogue off: its tracks and root go (user data keyed by
    track id stays for when it comes back).  The index file is kept."""
    async def body():
        global _refresh_task
        store = _store()
        store.set_config(CONFIG_ENABLED, False)
        if _refresh_task is not None:
            _refresh_task.cancel()
            _refresh_task = None
        if conflicting_share():
            # The root (and every track under it) is a network share's: the
            # catalogue owns nothing there to remove.
            if catalogue.is_catalogue_root(ROOT):
                catalogue.unregister_root(ROOT)
            return {"removed": 0}
        # Still registered while the tracks go: every guard keeps protecting.
        if not catalogue.is_catalogue_root(ROOT) and _has_tracks():
            catalogue.register_root(ROOT, key=KEY, label=LABEL, is_music=is_music,
                                    subsonic_visible=subsonic_visible)
        n = await remove_all()
        store.delete_scan_dir(ROOT)
        _unregister_root()
        try:
            from soniqboom.core import remote_freshness
            await remote_freshness.remove_share(ROOT)
        except Exception:                               # noqa: BLE001
            pass
        await _after_library_change()
        return {"removed": n}
    return await _run_job("disable", body)


async def set_scope(new_scope: str) -> dict:
    if new_scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    store = _store()
    if store.get_config(CONFIG_SCOPE, SCOPE_STANDARD) == new_scope:
        return status()
    store.set_config(CONFIG_SCOPE, new_scope)
    if not enabled():
        return status()

    async def body():
        _check_no_conflict()
        return await _update_then_apply()
    return await _run_job("scope", body)


async def reapply() -> dict:
    """Apply the kept index again — what it lists changed (the online HVSC
    collection's "Leave Modland's HVSC folder out")."""
    return await _run_job("reapply", apply_index)


def set_interval(name: str) -> dict:
    """Choose how often the index is refreshed (a key of ``INTERVALS``).  The
    schedule follows at once: next refresh = last successful refresh + the
    interval, or — when that is already past — in ``_REARM_GRACE_S``."""
    global _rearm_not_before
    if name not in INTERVALS:
        raise ValueError(f"interval must be one of {list(INTERVALS)}")
    store = _store()
    if store.get_config(CONFIG_INTERVAL, DEFAULT_INTERVAL) != name:
        store.set_config(CONFIG_INTERVAL, name)
        _rearm_not_before = time.time() + _REARM_GRACE_S
        _wake_refresh_loop()
    return status()


def set_subsonic(visible: bool) -> dict:
    store = _store()
    store.set_config(CONFIG_SUBSONIC, bool(visible))
    # The Subsonic catalogue (artists / albums / genres) is cached on this.
    store._catalog_seq += 1
    store._mutation_seq += 1
    store.browse_changed()          # (and the hidden-catalogue counts on this)
    return status()


async def refresh(*, local_zip: Path | None = None, manual: bool = False) -> dict:
    """Bring the index up to date and apply the change (the "Refresh now"
    button — ``manual`` —, the scheduled refresh): from Modland's changelogs
    when they can do it (``_delta_refresh``), else by downloading the full
    index (``_full_refresh``).  A failed / implausible download keeps
    everything; a much smaller index refused before is taken on a manual
    refresh (the status asked for that)."""
    if not enabled():
        return {**status(), "error": "the Modland catalogue is off"}
    accept_shrink = bool(manual and _status.get("shrink_refused"))

    async def body():
        _check_no_conflict()
        _register_root()
        _start_refresh_loop()
        # (Moves an earlier refresh applied to the index but the library has
        # not followed yet — before a full index could replace that record.)
        await _run_pending_moves()
        if local_zip is None and not accept_shrink:
            try:
                res = await _delta_refresh()
                _ensure_scan_dir()
                return res
            except NeedFullIndex as exc:
                log.info("Modland catalogue: downloading the full index (%s)", exc)
                res = await _full_refresh(str(exc), exc.moves)
                _ensure_scan_dir()
                return res
        res = await _full_refresh("refresh now" if accept_shrink else "a local index",
                                  None if accept_shrink else {}, local_zip=local_zip,
                                  accept_shrink=accept_shrink)
        _ensure_scan_dir()
        return res
    return await _run_job("refresh", body)


async def _full_refresh(reason: str, moves: "dict[str, str] | None" = None, *,
                        local_zip: Path | None = None, accept_shrink: bool = False) -> dict:
    """Download (or take ``local_zip``) the full index, build it and apply
    it.  The tunes Modland moved since the last update keep their plays,
    ratings and playlist entries: ``moves`` (``NeedFullIndex.moves``), or —
    None — the moves read from the changelogs since then (``_fetch_delta``
    ``collect``; nothing when they can't be read), are followed over the new
    index before it is applied."""
    loop = asyncio.get_running_loop()
    if moves is None and has_index():
        info0 = index_info()
        try:
            _progress("reading Modland's changelogs", 0, 0)
            got = await loop.run_in_executor(None, lambda: _fetch_delta(info0, collect=True))
            _count_index_bytes(int(got.get("bytes") or 0))
            moves = dict(got["plan"].moved)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001
            _count_index_bytes(int(getattr(exc, "nbytes", 0) or 0))
            log.info("Modland catalogue: no moves read before the full index (%s)", exc)
            moves = {}
    info = await _ensure_index(force_download=True, local_zip=local_zip,
                               accept_shrink=accept_shrink)
    downloaded = int((info or {}).get("downloaded") or 0)
    _count_index_bytes(downloaded)
    moved = 0
    if moves:
        # Only the moves the new index confirms: the old path gone, the new one there.
        moves = {o: n for o, n in moves.items()
                 if o != n and entry(o) is None and entry(n) is not None}
        if moves:
            _progress("moving tunes", 0, len(moves))
            moved = await _move_tracks(moves)
    extra_bytes = 0
    if local_zip is None and db_path().exists():
        # The full index can be older than the changes already applied (Modland
        # builds it once a day): bring it forward from the changelogs at once,
        # so tunes never go back to where they were for a day.  The days it
        # holds itself are replayed (a no-op) and so recorded as applied.
        try:
            fetched, _done, n = await _apply_changelogs(index_info())
            moved += n
            extra_bytes = int(fetched.get("bytes") or 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001
            log.info("Modland catalogue: the new index stays as downloaded (%s)", exc)
    if db_path().exists():
        await loop.run_in_executor(None, lambda: _set_info(
            db_path(), {"last_kind": "full", "last_reason": reason,
                        "last_bytes": downloaded + extra_bytes, "last_changelogs": 0}))
    res = await apply_index()
    res.update(source="full index", downloaded=downloaded, moved=moved, reason=reason)
    return res


_job_tasks: set = set()


def start_job(fn, *args, **kwargs) -> bool:
    """Run ``fn`` (enable / disable / scope / refresh) in the background;
    False when a job is already running (the caller polls ``status``)."""
    if _get_job_lock().locked():
        return False
    t = _spawn(fn(*args, **kwargs), f"modland-catalog.{getattr(fn, '__name__', 'job')}")
    _job_tasks.add(t)
    t.add_done_callback(_job_tasks.discard)
    return True


def start_disable() -> None:
    """Switch the catalogue off now: every other job still running or waiting
    (a build, a refresh, the startup repair) is cancelled — its batch section
    closes cleanly — then the tunes go (once: a switch-off already running
    carries on)."""
    _store().set_config(CONFIG_ENABLED, False)
    running = False
    for t in list(_job_tasks) + list(_tasks):
        if t.done():
            continue
        if "disable" in t.get_name():
            running = True
        elif t.get_name().startswith("modland-catalog.") and ".merge[" not in t.get_name():
            t.cancel()
    if running:
        return
    t = _spawn(disable(), "modland-catalog.disable")
    _job_tasks.add(t)
    t.add_done_callback(_job_tasks.discard)


def busy() -> bool:
    lock = _job_lock
    return lock is not None and lock.locked()


def _start_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _refresh_task = _spawn(_refresh_loop(), "modland-catalog.refresh-loop")


def _next_due(now: float | None = None) -> float:
    """When the scheduled refresh is due: the last successful build + the
    chosen interval; after a failed refresh no sooner than an hour after that
    attempt (a day after an index refused as implausible) — never the interval
    counted from a failure; and a just re-armed schedule (``set_interval``)
    no sooner than ``_rearm_not_before``."""
    if now is None:
        now = time.time()
    info = index_info()
    if not info and has_index() and _ro() is None:
        # The index is there but can't be read right now (no descriptor
        # left, the volume gone for a moment): no download on that — a look
        # again in a few minutes.
        return now + 300
    try:
        built = float(info.get("built_at") or 0)
    except (TypeError, ValueError):
        built = 0.0
    due = built + interval_seconds() if built else now
    last_try = _status.get("last_attempt") or 0
    if _status.get("error") and last_try:
        err_s = str(_status.get("error"))
        again = (_RETRY_AFTER_IMPLAUSIBLE_S if "implausible" in err_s
                 or _status.get("shrink_refused") or _status.get("index_refused")
                 else _RETRY_AFTER_FAILURE_S)
        again = max(again, float(_status.get("retry_after") or 0))
        # (The failed attempt built the index itself — a later step failed, e.g.
        # applying it: retried from the failure, not an interval later.)
        failed_after_build = bool(built) and built >= last_try
        due = (last_try + again if failed_after_build
               else max(due if built else 0, last_try + again))
    return max(due, _rearm_not_before)


def _note_retry_after(exc: BaseException | None) -> None:
    """Keep the pause an answer's ``Retry-After`` asked for (at most a week)
    for ``_next_due``."""
    try:
        from soniqboom.core.online_catalog import retry_after_of
        ra = retry_after_of(exc)
    except Exception:                                   # noqa: BLE001
        ra = None
    _status["retry_after"] = min(float(ra), 7 * 24 * 3600.0) if ra else 0.0
    e, refused, seen = exc, False, 0
    while e is not None and seen < 4:
        refused = refused or isinstance(e, _IndexRefused)
        e, seen = e.__cause__ or e.__context__, seen + 1
    _status["index_refused"] = refused


def _wake_refresh_loop() -> None:
    ev = _refresh_wake
    if ev is not None:
        try:
            ev.set()
        except RuntimeError:                            # its loop is gone
            pass


async def _sleep_or_wake(seconds: float) -> None:
    """Sleep ``seconds`` — or until the schedule changed (``set_interval``)."""
    global _refresh_wake
    ev = _refresh_wake = asyncio.Event()
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.0, seconds))
    except asyncio.TimeoutError:
        pass


async def _refresh_loop() -> None:
    """Refresh the index on its schedule (``_next_due``) — supervised: an
    error computing it (an index that can't be opened for a moment) waits
    and carries on, never ends the schedule."""
    failures = 0
    while enabled():
        try:
            wait = _next_due() - time.time()
            if wait > 0:
                await _sleep_or_wake(min(wait, 3600))
                failures = 0
                continue
            if busy():
                await asyncio.sleep(60)
                continue
            log.info("Modland catalogue: scheduled index refresh (%s)", interval())
            await refresh()
            failures = 0
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            failures += 1
            log.warning("Modland catalogue: refresh schedule hit an error (%d in a row) — "
                        "carrying on", failures, exc_info=True)
            await asyncio.sleep(min(3600, 60 * failures))


def register_at_startup() -> None:
    """Right after the library loaded: an enabled catalogue's root is a
    catalogue root again — before anything that walks, scans or polls remote
    roots starts.  No network, no store write; ``startup`` does the rest.
    A root whose tracks are still in the library (an interrupted switch-off)
    is registered too, so its removal stays protected."""
    store = _store()
    _capture_loop()
    if conflicting_share():
        return                      # the root is a network share's — not ours
    if enabled():
        _register_root()
    elif _has_tracks() or ROOT in store._scan_dirs:
        catalogue.register_root(ROOT, key=KEY, label=LABEL, is_music=is_music,
                                subsonic_visible=subsonic_visible)


def _drop_leftovers() -> None:
    """Half-written index files a crash or a kill left behind (a build, a
    delta, a download) — nothing is writing them at server start."""
    try:
        d = _dir()
    except Exception:                                   # noqa: BLE001
        return
    for pat in ("modland_catalog.sqlite.*.delta", "modland_catalog.sqlite.*.building",
                "modland_allmods.zip.*.part", "modland_allmods.zip.*.copy"):
        for f in d.glob(pat):
            try:
                f.unlink()
            except OSError:
                pass


async def startup() -> None:
    """At server start (after the library loaded): re-register an enabled
    catalogue (no network), finish an interrupted switch-off, rebuild when
    the classification rules changed, and arm the daily refresh."""
    store = _store()
    _capture_loop()
    _drop_leftovers()
    sid = conflicting_share()
    if sid:
        if enabled():
            log.warning("Modland catalogue: the network share %r uses %s — catalogue "
                        "not started", sid, ROOT)
            _status.update(state="error",
                           error=f"Modland's /pub/modules is added as the network share "
                                 f"\"{sid}\" — remove that share, then press Refresh now")
        return
    if enabled():
        _register_root()
        if has_index() or _has_tracks():
            _ensure_scan_dir()
        sd = store._scan_dirs.get(ROOT)
        if sd is not None and sd.get("status") == "unavailable":
            # Marked by a fetch failure before the restart: a new start tries
            # Modland again on the next play.
            store.set_scan_dir_status(ROOT, "ok")
        _status.update(state="ready")
        info = index_info()
        if (store.get_config(CONFIG_RULES) != RULES_VERSION
                or info.get("fingerprint") != _fingerprint()
                or _tune_count() != _expected_count()):
            async def _heal():
                await asyncio.sleep(20)

                async def body():
                    _check_no_conflict()
                    return await _update_then_apply()
                await _run_job("startup", body)
            _spawn(_heal(), "modland-catalog.startup")
        else:
            async def _counts():
                await asyncio.sleep(30)
                await _refill_quietly()
            _spawn(_counts(), "modland-catalog.tune-counts")
        _start_refresh_loop()
    elif _has_tracks() or ROOT in store._scan_dirs:
        log.info("Modland catalogue: finishing an interrupted switch-off")
        catalogue.register_root(ROOT, key=KEY, label=LABEL, is_music=is_music,
                                subsonic_visible=subsonic_visible)
        _spawn(disable(), "modland-catalog.finish-disable")


async def shutdown() -> None:
    for t in list(_tasks):
        t.cancel()
    for t in list(_tasks):
        try:
            await t
        except (asyncio.CancelledError, Exception):     # noqa: BLE001
            pass


# ── First play: merge the file's own metadata ─────────────────────────────────

_merging: set[str] = set()
_merge_tasks: dict = {}                  # track id → its running merge (``await_merge``)
_GENERIC_TITLES = frozenset({"untitled", "no name", "noname", "<?>", "?", "??", "???",
                             "unknown", "-", "title", "song", "module", "tune"})


# A played tune to read again on its next play (``_rederive_merged``).
REREAD = "catalogue_reread"


def is_merged(t: dict | None) -> bool:
    return bool(t) and (t.get("mtime") or 0) > 0


def _needs_merge(t: dict | None) -> bool:
    return bool(t) and (not is_merged(t) or bool(t.get(REREAD)))


def _good_title(s, local_stem: str) -> bool:
    s = (s or "").strip()
    if not s or s == local_stem:
        return False
    if sum(c.isalnum() for c in s) < 2:
        return False
    return s.lower() not in _GENERIC_TITLES


def merged_track(old: dict, meta: dict, local_stem: str) -> dict:
    """The catalogue track ``old`` after its first play: the file's own
    metadata (``meta`` — ``metadata.extract`` of the cached copy) with the
    catalogue's identity and path-derived Modland data kept."""
    from soniqboom.core import genre_derive, scene_metadata as sm
    rel = old["path"][len(ROOT) + 2:]
    d = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    d.pop("embedding", None)
    d.pop("cover_art", None)                     # (no art store for catalogue tracks)
    # (The index's size stays the row's: what a refresh compares with.)
    for f in ("id", "path", "dir_hash", "scan_root_hash", "added_at", "file_size"):
        if f in old:
            d[f] = old[f]
    d["mtime"] = time.time()                     # merged marker
    if not d.get("genre"):
        d["genre"] = family_genre(rel) or []
    d.setdefault("is_duplicate_primary", True)
    if not _good_title(d.get("title"), local_stem):
        d["title"] = old.get("title") or title_for(rel)
    if not d.get("format"):
        d["format"] = old.get("format") or format_for(rel)
    # A length measured by this play's render beats the extractor's
    # placeholder (the catalogue row had none before).
    if (old.get("duration") or 0) > 0 and not (d.get("duration") or 0) > 0:
        d["duration"] = old["duration"]
    elif (old.get("duration") or 0) > 0 and (meta.get("duration") or 0) in (180, 180.0):
        d["duration"] = old["duration"]
    # The file's own author stays the artist, as for a scanned file
    # ("Steve Turner", not HVSC's folder "Turner Steve"); Modland's credit
    # fills an empty / placeholder one (``apply_path_fields``).
    apply_path_fields(d, rel, siblings=_siblings_map(),
                      use_filename=sm.filename_game_enabled())
    _finish_genres(d, genre_derive.names_enabled())
    return d


def merge_after_play(track_id: str, local_path) -> None:
    """Schedule the first-play merge of catalogue track ``track_id`` from the
    local copy ``local_path`` (a no-op for other tracks, merged ones and a
    merge already running).  Called from the play paths once the file is
    local."""
    try:
        t = _store().get_track(track_id)
    except Exception:                                   # noqa: BLE001
        return
    if (not t or not catalogue.is_catalogue_path(t.get("path"))
            or not _needs_merge(t) or track_id in _merging or not local_path):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _merging.add(track_id)
    t_ = loop.create_task(_merge(track_id, Path(str(local_path))),
                          name=f"modland-catalog.merge[{track_id}]")
    _tasks.add(t_)
    t_.add_done_callback(_tasks.discard)
    _merge_tasks[track_id] = t_
    t_.add_done_callback(lambda _t, k=track_id: _merge_tasks.pop(k, None))


async def await_merge(track_id: str, timeout: float) -> bool:
    """Wait (up to ``timeout`` s) for the first-play merge of ``track_id``
    that ``merge_after_play`` started; True when one finished.  A SID's
    first play waits for it: its render length is the HVSC length the merge
    reads (a local file's is known before it plays), not the default."""
    t_ = _merge_tasks.get(track_id)
    if t_ is None:
        return False
    try:
        await asyncio.wait_for(asyncio.shield(t_), timeout)
    except (asyncio.TimeoutError, Exception):           # noqa: BLE001
        return t_.done()
    return True


def _extract_copy(local: Path, track_id: str, real_name: str) -> dict | None:
    """``metadata.extract`` of the cached file, under its real file name (a
    prefix-form Amiga name means nothing as ``<hash>.ext``) with any
    companion files beside it.  Blocking."""
    import shutil
    import tempfile
    from soniqboom.core import metadata
    if not local.exists():
        return None
    if local.name == real_name:
        meta = metadata.extract(local, track_id)
        return _song_end_loop_length(local, real_name, meta.model_dump())
    tdir = Path(tempfile.mkdtemp(prefix="sq_catalog_"))
    try:
        dst = tdir / real_name
        try:
            os.link(local, dst)
        except OSError:
            shutil.copyfile(local, dst)
        meta = metadata.extract(dst, track_id)
        return _song_end_loop_length(local, real_name, meta.model_dump())
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


def _song_end_loop_length(local: Path, real_name: str, d: dict) -> dict:
    """``d`` with the length SoniqBoom renders for a tracker module that ends
    in a song-end loop (``openmpt_vu.song_end_loop``: the time the loop
    first repeats).  The server's own metadata reads take a module's length
    from ``openmpt123 --info`` (libopenmpt in-process only in scan workers),
    which plays such a loop out — but a catalogue tune is read here only on
    intent (its play, a prewarm, Track Info), whose render reads it with
    libopenmpt in-process anyway.  Blocking; never raises."""
    try:
        from soniqboom.core import metadata, openmpt_vu
        if (os.path.splitext(real_name)[1].lower() not in metadata._TRACKER_EXTS
                or not float(d.get("duration") or 0) > 0):
            return d
        cut = openmpt_vu.song_end_loop(local.read_bytes())
        if cut is not None and cut < float(d["duration"]):
            d["duration"] = openmpt_vu.duration_ms(cut) / 1000
    except Exception:                                   # noqa: BLE001
        log.debug("song-end loop check of %s failed", real_name, exc_info=True)
    return d


async def _merge(track_id: str, local: Path) -> None:
    try:
        store = _store()
        t = store.get_track(track_id)
        if not _needs_merge(t):
            return
        real_name = t["path"].rsplit("/", 1)[-1]
        rel = t["path"][len(ROOT) + 2:]
        tok = folder_player_token(rel)
        if tok:
            # ``x.ma`` is no music name to the extractor; ``ma.x`` (Amiga
            # prefix form, the same module) is.
            real_name = f"{tok}.{os.path.splitext(real_name)[0]}"
        loop = asyncio.get_running_loop()
        try:
            meta = await loop.run_in_executor(None, _extract_copy, local, track_id, real_name)
        except Exception as exc:                        # noqa: BLE001
            log.info("Modland catalogue: metadata of %s not read (%s)", real_name, exc)
            return
        if not meta:
            return
        await loop.run_in_executor(None, _siblings_map)
        cur = store.get_track(track_id)
        if not _needs_merge(cur) or cur.get("path") != t.get("path"):
            return
        new = merged_track(cur, meta, local.stem)
        store.upsert_tracks_batch([new])
        log.info("Modland catalogue: read %s on its first play (%s, %.1f s)",
                 real_name, new.get("format"), float(new.get("duration") or 0))
        # SID lengths (HVSC) and the md5 joins (song database …) follow.
        if new.get("sid_md5"):
            try:
                from soniqboom.core import hvsc_apply
                await hvsc_apply.apply_hvsc_to_library(ids=[track_id])
            except Exception:                           # noqa: BLE001
                log.debug("HVSC apply after a catalogue merge failed", exc_info=True)
        try:
            from soniqboom.core import scanner
            scanner._spawn_scene_autoapply()
        except Exception:                               # noqa: BLE001
            pass
        try:
            # Open pages re-read the track: its genre, length, tunes … (parity
            # with a scanned file — the player kept the bare index row).  Its
            # own task: a slow socket must not hold up a play awaiting this
            # merge (``await_merge``).
            from soniqboom.api import library
            b_ = asyncio.get_running_loop().create_task(
                library.broadcast_track_updated(track_id),
                name=f"modland-catalog.track-updated[{track_id}]")
            _tasks.add(b_)
            b_.add_done_callback(_tasks.discard)
        except Exception:                               # noqa: BLE001
            log.debug("track-updated broadcast failed", exc_info=True)
    except Exception:                                   # noqa: BLE001
        log.warning("Modland catalogue merge of %s failed", track_id, exc_info=True)
    finally:
        _merging.discard(track_id)
