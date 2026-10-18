# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Online collections: index-driven catalogues of HTTP archives.

The Modland catalogue (``core/modland_catalog.py``) showed the model: a
collection's own index file is the source of truth, its tunes become library
rows without any of their files being read, and a file is downloaded only on
the listener's intent (a play, the one next-track prewarm, Track Info — the
rule decided 2026-10-06, ``core/catalogue.py``).  This module is that model as
a base class for the collections that followed (HVSC on 6581.nl —
``core/online_hvsc.py``; Aminet and scene.org next), so each one only says
where its index is, how to read it and how to name its tunes:

* ``OnlineCatalogue.fetch_index`` — download the index, return its rows
  ``(rel, size, extra)`` (or ``None``: unchanged since the last refresh).
* ``OnlineCatalogue.build_track`` — the library row of one index row.
* ``OnlineCatalogue.changed`` — does a row differ from the library's track
  (a refresh rewrites it)?

Everything else is shared: the index sqlite (``<data>/scene/online/<key>/``),
applying it to the library in slices (one batch section; playlist entries and
played rows of tunes that leave are parked, as Modland's are), the
switch-on / switch-off / refresh jobs under one lock per collection, the
refresh schedule, the status the Settings card shows, the file source
(``CatalogueHTTPSource`` — listings and stats from the index, every read gated
by ``catalogue.allowed``, an offline hold after a network failure) and the
first-play merge of the file's own metadata.

Collections register in ``REGISTRY`` (``register``); ``main`` starts and stops
them, ``api/admin`` exposes ``/api/admin/catalog/<key>``, and the play paths
call ``merge_after_play`` / ``await_merge`` for any catalogue tune (Modland's
are passed to its own module).
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from soniqboom.core import catalogue
from soniqboom.core.filesource import DirEntry, FileStat
from soniqboom.core.filesource_webdav import WebDAVFileSource

log = logging.getLogger(__name__)

INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600,
             "biweekly": 14 * 24 * 3600, "monthly": 30 * 24 * 3600}
INTERVAL_LABELS = {"daily": "Daily", "weekly": "Weekly", "biweekly": "Every 2 weeks",
                   "monthly": "Monthly"}
_RETRY_AFTER_FAILURE_S = 3600
# An index refused by its own checks (implausibly small, shrunk too much,
# folders that can't be listed) or by the archive (403 / 404 / 410 / 451) —
# a refusal the next try would repeat — waits a day, doubling (each further
# failure in a row doubles any wait), at most the refresh interval and never
# more than ``_RETRY_MAX_S``.
_RETRY_AFTER_REFUSAL_S = 24 * 3600
_RETRY_MAX_S = 7 * 24 * 3600
_REARM_GRACE_S = 300
_OFFLINE_HOLD_S = 60.0
_OFFLINE_HOLD_MAX_S = 900.0
# One file the archive refuses (403 / 410 / 451 …) is that file's problem: it
# is not asked for again for this long, and the collection stays online.
_ITEM_REFUSED_S = 3600.0
_ITEM_REFUSED_MAX = 4096
_MAX_SHRINK = 0.25                 # a refresh may lose at most this share of the rows
_INITIAL_ADDED_AT = 1              # rows of the first index: not "Recently added"
_INDEX_SCHEMA = 2                  # 2: files.changed (when a file last changed on the archive)


def user_agent() -> str:
    """The User-Agent every online-collection request carries — the archives
    can see who asks and reach the project."""
    try:
        from soniqboom import __version__
    except Exception:                                   # noqa: BLE001
        __version__ = "?"
    return f"SoniqBoom/{__version__} (+https://github.com/SFCyris/SoniqBoom)"


def _store():
    from soniqboom.core.store import get_store
    return get_store()


class IndexRefused(ValueError):
    """A new index refused — a refusal the next try would repeat: retried
    after ``_RETRY_AFTER_REFUSAL_S``, not the hour of a failed connection."""


class IndexShrank(IndexRefused):
    """A refreshed index lists far fewer tunes than the one in use."""


class ItemRefused(FileNotFoundError):
    """The archive refuses this one file (an HTTP 403 / 410 / 451 …), lately:
    not asked again for a while (``_ITEM_REFUSED_S``)."""


def http_status_of(exc: BaseException | None) -> "int | None":
    """The HTTP status of an answer ``exc`` carries (``httpx.HTTPStatusError``
    or a status-bearing error raised from one), else None."""
    seen = 0
    while exc is not None and seen < 4:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
        code = getattr(exc, "status_code", None)
        if isinstance(code, int):
            return code
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def retry_after_of(exc: BaseException | None, now: "float | None" = None) -> "float | None":
    """Seconds an answer's ``Retry-After`` (a 429 / 503) asks to wait — the
    ``retry_after`` attribute of ``exc`` or the header of its response (a
    number of seconds or an HTTP date); None when it asks nothing."""
    seen = 0
    while exc is not None and seen < 4:
        ra = getattr(exc, "retry_after", None)
        if isinstance(ra, (int, float)) and ra > 0:
            return float(ra)
        resp = getattr(exc, "response", None)
        hdrs = getattr(resp, "headers", None)
        if hdrs is not None:
            got = parse_retry_after(hdrs.get("retry-after"), now)
            if got is not None:
                return got
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def parse_retry_after(value, now: "float | None" = None) -> "float | None":
    """A ``Retry-After`` header value as seconds from now (None: absent or
    unreadable)."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.isdigit():
        return float(int(v))
    try:
        from email.utils import parsedate_to_datetime
        at = parsedate_to_datetime(v).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, at - (time.time() if now is None else now))


def is_refusal(exc: BaseException | None) -> bool:
    """A failure the next try of the same request would repeat: an index the
    collection's checks refuse (``IndexRefused``) or an answer refusing the
    request itself (HTTP 400–499 but 408 / 425 / 429)."""
    if isinstance(exc, IndexRefused):
        return True
    code = http_status_of(exc)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 425, 429)


class PackageEmpty(FileNotFoundError):
    """An opened package holds nothing playable (it has left the library):
    ``name`` the package's file name, ``holds`` what it holds instead
    (``archive.describe_census``), ``reason`` the sentence for the
    listener."""

    def __init__(self, name: str, holds: str = "") -> None:
        self.name = name
        self.holds = holds
        self.reason = empty_package_reason(name, holds)
        super().__init__(self.reason)


class PackageDamaged(ValueError):
    """A fetched package the archive readers can't open (a damaged file)."""


# The detail older versions marked an opened package with nothing playable
# with (``defect`` "corrupt"), and the start of the one a package that can't
# be opened is marked with.
_LEGACY_EMPTY_DETAIL = "The package holds nothing SoniqBoom plays."
DAMAGED_DETAIL_PREFIX = "The archive is damaged"


def _music_disk(rel: str) -> bool:
    """Is package ``rel`` filed under a folder of music disks (scene.org's
    ``disks``, ``musicdisks`` …)?"""
    return any("disk" in part.lower() for part in rel.split("/")[:-1])


def empty_package_reason(name: str, holds: str = "") -> str:
    """The sentence a play (or Track Info) of an empty package answers:
    "Antom.lha has no tunes SoniqBoom plays — it holds … It is no longer
    listed."""
    what = f" — {holds}" if holds else ""
    return f"{name} has no tunes SoniqBoom plays{what}. It is no longer listed."


# ── The file source ───────────────────────────────────────────────────────────

# A row's own bookkeeping — never "taken from the index" by a first-play read.
_OWN_FIELDS = frozenset({"id", "path", "dir_hash", "scan_root_hash", "added_at", "file_size",
                         "mtime", "is_duplicate_primary", "index_fields", "index_vals",
                         "user_edited"})


class _HeldOffline(ConnectionError):
    """A read while the collection's source holds itself offline."""


class RangeNotHonoured(OSError):
    """A strict range read (``CatalogueHTTPSource.read_range``) the server
    answered with the whole file: it would be read up to the offset."""


class CatalogueHTTPSource(WebDAVFileSource):
    """An online collection's files over plain HTTP(S).  Listings, stats and
    folder checks come from the collection's index — never the network.  A
    file is read only inside a play's (prewarm's, Track Info's) fetch window
    (``catalogue.allowed``); a network failure holds the source offline for a
    while (``get_source`` → None: uncached plays answer "unavailable",
    cached ones play)."""

    is_catalogue = True

    def __init__(self, cat: "OnlineCatalogue") -> None:
        self._cat = cat
        super().__init__(cat.root + "/", timeout=60.0)
        self._offline_until = 0.0
        self._offline_streak = 0
        self._sizes: dict[str, int] = {}
        self._refused: dict[str, tuple[float, int]] = {}   # rel → (until, HTTP status)

    def _new_client(self):
        import httpx
        return httpx.Client(base_url=self._base, verify=self._verify, timeout=self._timeout,
                            follow_redirects=True, headers={"User-Agent": user_agent()})

    # Offline hold -------------------------------------------------------------
    def is_offline(self) -> bool:
        from soniqboom.core import netpolicy
        return netpolicy.blocked(self._base) or time.monotonic() < self._offline_until

    def _note_network_failure(self, exc: BaseException, rel: "str | None" = None) -> None:
        if not isinstance(exc, Exception):
            return
        if isinstance(exc, (catalogue.CatalogueFetchRefused, FileNotFoundError, _HeldOffline,
                            RangeNotHonoured)):
            return
        code = http_status_of(exc)
        if isinstance(code, int) and 400 <= code < 500 and code not in (408, 425, 429):
            # The archive answered — and refused THIS file (403 / 410 / 451 …):
            # that file waits a while, every other one plays.
            if rel:
                if len(self._refused) >= _ITEM_REFUSED_MAX:
                    self._refused.pop(next(iter(self._refused)), None)
                self._refused[rel] = (time.monotonic() + _ITEM_REFUSED_S, code)
                log.warning("%s refuses %s (HTTP %d) — not asked for it again for %d min",
                            self._cat.label, rel, code, _ITEM_REFUSED_S // 60)
            return
        self._offline_streak += 1
        hold = min(_OFFLINE_HOLD_MAX_S, _OFFLINE_HOLD_S * (2 ** min(self._offline_streak - 1, 4)))
        self._offline_until = time.monotonic() + hold
        log.warning("%s unreachable (%s: %s) — treating it as offline for %d s",
                    self._cat.label, type(exc).__name__, exc, hold)
        self._cat._schedule_status_flip("unavailable")
        self._cat._schedule_status_flip("ok", after=hold + 0.25)

    def _note_network_ok(self) -> None:
        if self._offline_streak or self._offline_until:
            self._offline_streak = 0
            self._offline_until = 0.0
        self._cat._schedule_status_flip("ok")

    def _gate(self, path: str, op: str, *, resume: bool = False) -> str:
        rel = catalogue.norm_rel(self._cat.root, self._rel(path))
        if not catalogue.allowed(self._cat.root, rel) and not (
                resume and self._downloading(rel)):
            catalogue.note_refused(self._cat.root, rel, op)
            raise catalogue.CatalogueFetchRefused(
                f"{self._cat.label}: {rel} is fetched only when it is played")
        if self.is_offline():
            raise _HeldOffline(f"{self._cat.label} can't be reached right now")
        held = self._refused.get(rel.split("::", 1)[0]) or self._refused.get(rel)
        if held is not None:
            if time.monotonic() < held[0]:
                raise ItemRefused(errno.EACCES,
                                  f"{self._cat.label} refuses this file (HTTP {held[1]})", rel)
            self._refused.pop(rel.split("::", 1)[0], None)
            self._refused.pop(rel, None)
        return rel

    # Index-backed (no network) ------------------------------------------------
    def list_dir(self, path: str) -> list[DirEntry]:
        return self._cat.list_dir(self._rel(path))

    def _list_entries(self, path: str, lane: str = "scan", *, error_sink=None) -> list[DirEntry]:
        return self._cat.list_dir(self._rel(path))

    def stat(self, path: str, *, lane: str = "scan", strict: bool = False) -> FileStat:
        rel = self._rel(path).strip("/")
        e = self._cat.entry(rel)
        if e is not None:
            size = e[0] or self._sizes.get(rel, 0)
            if not size and catalogue.allowed(self._cat.root, rel) and not self.is_offline():
                # The index has no size (HVSC's Songlengths lists none): ask once,
                # inside a play's fetch window only.
                try:
                    r = self._client.head(self._url(rel))
                except Exception as exc:                # noqa: BLE001
                    if strict:
                        self._note_network_failure(exc, rel)
                        raise
                    r = None
                if r is not None and r.status_code < 400:
                    size = int(r.headers.get("content-length") or 0)
                    if not size:
                        # A HEAD without a length (a server streaming its
                        # HEAD, a proxy): a one-byte range read's
                        # ``Content-Range`` tells it.
                        size = self._size_by_range(rel)
                    if size:
                        self._sizes[rel] = size
                elif r is not None and strict:
                    # A caller that must know the size (a big ZIP read by
                    # ranges) never takes "0" for an answer: no whole download.
                    if r.status_code == 404:
                        raise FileNotFoundError(errno.ENOENT, "not on the archive", rel)
                    raise ConnectionError(f"{self._cat.label} answered {r.status_code} "
                                          f"for the size of {rel}")
            return FileStat(size=int(size or 0), mtime=0.0, is_dir=False)
        if self._cat.is_dir(rel):
            return FileStat(size=0, mtime=0.0, is_dir=True)
        raise FileNotFoundError(errno.ENOENT, f"not in the {self._cat.label} index", rel)

    def is_dir(self, path: str) -> bool:
        return self._cat.is_dir(self._rel(path))

    def _size_by_range(self, rel: str) -> int:
        """``rel``'s length from a one-byte range read (its 206 answer's
        ``Content-Range``); 0 when the server tells none.  Inside a fetch
        window only (the caller's)."""
        try:
            with WebDAVFileSource.open_stream(self, rel, offset=0, length=1) as st:
                if getattr(st, "_skip", 0) or st.size is None:
                    return 0
                return int(st.size or 0)
        except Exception:                               # noqa: BLE001
            return 0

    def forget_size(self, rel: str) -> None:
        """The size asked of the archive for ``rel`` is stale (it changed)."""
        self._sizes.pop((rel or "").strip("/"), None)

    def walk(self, top: str):
        raise catalogue.CatalogueFetchRefused(f"the {self._cat.label} catalogue is never walked")

    def walk_with_stat(self, top: str, **_kw):
        raise catalogue.CatalogueFetchRefused(f"the {self._cat.label} catalogue is never walked")

    def reconnect(self) -> bool:
        return True

    # Gated network reads ------------------------------------------------------
    def read_file(self, path: str, *, lane: str = "stream") -> bytes:
        rel = self._gate(path, "read")
        try:
            data = super().read_file(path, lane=lane)
        except BaseException as exc:
            self._note_network_failure(exc, rel)
            raise
        self._note_network_ok()
        catalogue.note_read(self._cat.root, rel, "read", len(data))
        return data

    def read_at(self, path: str, offset: int, length: int, *, lane: str = "scan",
                strict: bool = False) -> bytes:
        """``length`` bytes at ``offset`` (one read, logged once).  ``strict``:
        a server that answers with the whole file instead of the range is
        refused (``RangeNotHonoured``) rather than read up to ``offset``."""
        rel = self._gate(path, "range read")
        if length <= 0:
            return b""
        out = bytearray()
        try:
            # The plain WebDAV stream — this class's ``open_stream`` logs a download.
            with WebDAVFileSource.open_stream(self, path, offset=offset, lane=lane,
                                              length=length) as st:
                if strict and getattr(st, "_skip", 0):
                    raise RangeNotHonoured(f"{self._cat.label} sent the whole of {rel} "
                                           f"for a range read at {offset}")
                while len(out) < length:
                    chunk = st.read(min(256 * 1024, length - len(out)))
                    if not chunk:
                        break
                    out += chunk
        except BaseException as exc:
            self._note_network_failure(exc, rel)
            raise
        self._note_network_ok()
        catalogue.note_read(self._cat.root, rel, "range read", len(out))
        return bytes(out)

    def read_range(self, path: str, offset: int, length: int, *, lane: str = "scan") -> bytes:
        """``read_at`` from a server that answers the range only."""
        return self.read_at(path, offset, length, lane=lane, strict=True)

    def read_partial(self, path: str, max_bytes: int, *, lane: str = "scan") -> bytes:
        return self.read_at(path, 0, max_bytes, lane=lane)

    def _downloading(self, rel: str) -> bool:
        """Is the remote cache downloading ``rel`` right now?  A long download
        (a big file) may resume past its play's fetch window."""
        try:
            from soniqboom.core.remote_cache import get_cache
            return get_cache().downloading(self._cat.root, "/" + rel.split("::", 1)[0])
        except Exception:                               # noqa: BLE001
            return False

    def open_stream(self, path: str, *, offset: int = 0, lane: str = "stream",
                    length: int | None = None):
        rel = self._gate(path, "download", resume=offset > 0)
        try:
            st = super().open_stream(path, offset=offset, lane=lane, length=length)
        except BaseException as exc:
            self._note_network_failure(exc, rel)
            raise
        self._note_network_ok()
        catalogue.note_read(self._cat.root, rel,
                            "download" if not offset else f"resume@{offset}", None)
        return _CountingStream(st)


class _CountingStream:
    """Counts the bytes a catalogue download moved (status / tests)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.size = getattr(inner, "size", None)

    def read(self, n: int = 256 * 1024) -> bytes:
        data = self._inner.read(n)
        if data:
            catalogue.note_bytes(len(data))
        return data

    def close(self) -> None:
        self._inner.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── The collection base ───────────────────────────────────────────────────────

class OnlineCatalogue:
    """One online collection.  Subclasses set the class attributes and
    implement ``fetch_index`` / ``build_track`` (and, when the index's size
    isn't the change marker, ``changed``)."""

    key = ""                       # "hvsc" — config keys, the API path, the data folder
    label = ""                     # "HVSC" — status, logs, the folder tree's root name
    root = ""                      # the scan root: "https://6581.nl/C64Music"
    site_url = ""                  # the collection's home page (Settings link)
    default_interval = "weekly"
    min_rows = 1                   # an index with fewer music rows is refused
    description = ""
    packaged = False               # its archives are packages, opened on intent (``expand``)
    index_rules = 1                # how ``fetch_index`` reads the index: a bump rebuilds it once
    build_rules = 1                # how ``build_track`` derives rows: a bump rebuilds every row
    # The fields a first-play read took from the index, for a played row read
    # before ``index_fields`` was recorded (``_rebuild_merged``).
    index_fill_fields: tuple = ()
    # Fields the build sets that other passes may set too (Demozoo's group):
    # what the build set is recorded (``index_vals``), so a later build that
    # sets none withdraws exactly that (``_withdrawals``).
    owned_fields: tuple = ()
    how = ("Listed from the collection's own index; a tune is fetched when you play it or "
           "open its Track Info, then plays from the cache.")

    def __init__(self) -> None:
        self._status: dict = {"state": "off", "phase": "", "done": 0, "total": 0, "error": None,
                              "started_at": None, "last_attempt": None, "last_result": None,
                              "shrink_refused": False}
        self._job_lock: asyncio.Lock | None = None
        self._job_tasks: set = set()
        self._tasks: set = set()
        self._refresh_task: asyncio.Task | None = None
        self._refresh_wake: asyncio.Event | None = None
        self._rearm_not_before = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._local = threading.local()
        self._db_gen = 0
        self._merging: set = set()
        self._merge_tasks: dict = {}
        self._pkg_locks: dict = {}
        self._pkg_db_lock = threading.Lock()
        self._purge_queue: set = set()
        self._expanded_at: dict = {}       # track id → when a package opening wrote it
        self._empty_ids: "tuple | None" = None   # (file key, {track id: package}) — empty_package_ids

    # Config -------------------------------------------------------------------
    def cfg(self, name: str) -> str:
        return f"online_{self.key}_{name}"

    def enabled(self) -> bool:
        try:
            return bool(_store().get_config(self.cfg("enabled"), False))
        except Exception:                               # noqa: BLE001
            return False

    def interval(self) -> str:
        v = _store().get_config(self.cfg("interval"), self.default_interval)
        return v if v in INTERVALS else self.default_interval

    def interval_seconds(self) -> int:
        return INTERVALS[self.interval()]

    def subsonic_visible(self) -> bool:
        try:
            return bool(_store().get_config(self.cfg("subsonic"), False))
        except Exception:                               # noqa: BLE001
            return False

    def options(self) -> dict:
        """Collection-specific switches shown on its card: ``name → value``."""
        return {}

    def option_labels(self) -> dict:
        return {}

    def set_option(self, name: str, value) -> None:
        raise ValueError(f"unknown option {name!r}")

    # Paths and ids --------------------------------------------------------------
    def dir(self) -> Path:
        from soniqboom.config import get_data_dir
        d = Path(get_data_dir()) / "scene" / "online" / self.key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def db_path(self) -> Path:
        return self.dir() / "index.sqlite"

    def parked_path(self) -> Path:
        return self.dir() / "played.json"

    def root_hash(self) -> str:
        return hashlib.sha256(self.root.encode()).hexdigest()[:16]

    def track_path(self, rel: str) -> str:
        return f"{self.root}:/{rel}"

    def track_id(self, rel: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.track_path(rel)))

    def rel_of(self, path: str) -> str:
        p = str(path or "")
        pre = self.root + ":/"
        return p[len(pre):].lstrip("/") if p.startswith(pre) else ""

    def owns(self, path) -> bool:
        return str(path or "").startswith(self.root + ":/")

    def row_id(self, rel: str, extra: dict | None) -> str:
        """The track id of index row ``rel``: a package's first member keeps
        the package's own id (a playlist entry or queue made before the
        package was opened plays it), every other row its own."""
        if extra and extra.get("first"):
            return self.track_id(rel.split("::", 1)[0])
        return self.track_id(rel)

    def is_package(self, rel: str) -> bool:
        from soniqboom.core import archive
        return self.packaged and "::" not in rel and archive.is_archive_name(rel)

    # Subclass hooks ---------------------------------------------------------------
    def fetch_index(self, workdir: Path, info: dict) -> "tuple[Iterable, dict] | None":
        """Download the index into ``workdir``.  Returns ``(rows, info)`` —
        rows ``(rel, size, extra)`` of every MUSIC file (``extra``: a small
        JSON-able dict or None) and the values to keep in the index's info
        (e.g. ``etag`` / ``last_modified`` for the next conditional request,
        ``index_date``) — or None when the index hasn't changed since
        ``info`` (the last build's).  Blocking."""
        raise NotImplementedError

    def build_track(self, rel: str, size: int, extra: dict | None, *, added_at: int,
                    dir_hash: str, root_h: str) -> dict:
        raise NotImplementedError

    def changed(self, old: dict, rel: str, size: int, extra: dict | None) -> bool:
        """Does the library's track ``old`` differ from its index row?"""
        return (old.get("file_size") or 0) != size

    def excluded_rels(self) -> frozenset:
        return frozenset()

    def merged_fields(self, d: dict, old: dict, rel: str) -> dict:
        """The first-play merge's row ``d`` (the file's own metadata on the
        catalogue's identity) — a subclass adds what its index knows better."""
        return d

    def after_index(self) -> None:
        """A new index was built (blocking; e.g. HVSC hands its documents to
        the SID lookups)."""

    def after_disable(self) -> None:
        """The collection was switched off (blocking)."""

    def after_merge(self, track_id: str, d: dict, old: dict) -> None:
        """The first-play read of ``track_id`` was stored (``d``; ``old`` the
        row before it)."""

    def unavailable_folders(self) -> list:
        """Folders the last index refresh could not read (their tunes kept)."""
        return []

    def move_key(self, rel: str, size: int, extra: dict | None) -> "str | None":
        """A key naming the same FILE whatever its path — the HVSC checksum —
        so a tune the archive moved (another folder, same file) keeps its
        user data; ``track_move_key`` reads it off a library row.  None: the
        collection has no such key (a moved file is a new tune)."""
        return None

    def track_move_key(self, t: dict) -> "str | None":
        return None

    def row_changed(self, prev_size: int, prev_extra: dict | None, size: int,
                    extra: dict | None) -> bool:
        """Is a refreshed index row ANOTHER VERSION of the file than the one
        the previous index listed?  Its library row is then built again and
        its cached copy dropped (``apply_index``), an opened package folds
        back into one entry.  Default: the package marker differs."""
        return self.package_marker(prev_size, prev_extra) != self.package_marker(size, extra)

    def signal_stop(self) -> None:
        """Ask a blocking index download / listing to end at its next step
        (switch-off, shutdown) — a worker thread outlives a cancelled job."""

    def seen_for(self, extra: dict | None, now: int) -> int:
        """When a file NEW to the index counts as first seen (``added_at``
        unless it is older than the collection's first index).  A collection
        whose index dates its files says when the file appeared."""
        return now

    def prepare_build(self) -> None:
        """Read the settings ``build_track`` depends on (before an apply or a
        package opening builds rows)."""

    def _drop_cached(self, rel: str, *, before: float | None = None) -> None:
        """``rel`` (a package member: its package) changed at the archive at
        ``before``: forget the remote cache's copy and ZIP subsets fetched
        before then (one fetched since is the new version), its in-memory ZIP
        blocks, and — queued for ``_purge_derived`` — what was made from its
        tunes (renders, VU data, waveforms)."""
        pkg = rel.split("::", 1)[0]
        try:
            from soniqboom.core import remote_zip
            from soniqboom.core.remote_cache import get_cache
            get_cache().drop_if_older(self.root, "/" + pkg,
                                      before if before else float("inf"))
            remote_zip.forget_blocks(self.root, "/" + pkg)
            from soniqboom.core.filesource import get_source
            src = get_source(self.root)
            if src is not None and hasattr(src, "forget_size"):
                src.forget_size(pkg)
        except Exception:                               # noqa: BLE001
            log.debug("%s: cached copy of %s not dropped", self.label, rel, exc_info=True)
        ids = {self.track_id(pkg)}
        if self.packaged and self.is_package(pkg):
            op = self._opened(pkg).get(pkg)
            for m in (op[1] if op else ()):
                ids.add(self.track_id(f"{pkg}::{m[0]}"))
        self._purge_queue |= ids

    async def _purge_derived(self) -> None:
        """Drop what was made from the files that changed (``_drop_cached``):
        their rendered audio and VU data, stored waveforms, a ranged Track
        Info read."""
        ids, self._purge_queue = set(self._purge_queue), set()
        if not ids:
            return
        try:
            from soniqboom.core import conversion_cache
            await conversion_cache.purge_tracks(ids)
            _store().forget_waveforms(ids)
            from soniqboom.api import stream
            for tid in ids:
                stream._RANGED_INFO_DONE.discard(tid)
        except Exception:                               # noqa: BLE001
            log.debug("%s: renders of changed files not purged", self.label, exc_info=True)

    def package_marker(self, size: int, extra: dict | None) -> str:
        """What changes when a package's index row changes — its opened
        members are then stale and it is opened again on its next play."""
        return str(int(size or 0))

    def merge_view(self, rel: str, name: str, local) -> tuple[str, dict]:
        """The name the first-play read takes a fetched file under, and the
        fields it then sets over what the file said (Aminet: an MPEG audio
        ``.mpg`` read as ``.mp3`` with the layer its frames name)."""
        return name, {}

    def package_meta(self, rel: str, source) -> dict:
        """Facts about package ``rel`` read when it is opened (the archive's
        fetch window is open: its companions may be read).  Blocking."""
        return {}

    def loose_facts(self, rel: str) -> dict | None:
        """Facts about a loose (unpackaged) file ``rel`` read with it on its
        first play — inside its fetch window — as ``package_meta`` gives them;
        None when there are none (the default) or they can't be read.
        Blocking."""
        return None

    # The index sqlite -------------------------------------------------------------
    def _ro(self) -> sqlite3.Connection | None:
        p = self.db_path()
        if not p.exists():
            return None
        con = getattr(self._local, "con", None)
        if con is not None and getattr(self._local, "gen", -1) == self._db_gen:
            return con
        if con is not None:
            try:
                con.close()
            except Exception:                           # noqa: BLE001
                pass
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        except sqlite3.Error:
            # (no descriptor left, the data volume gone for a moment): no
            # index for this call — the next one opens it again
            self._local.con = None
            return None
        self._local.con = con
        self._local.gen = self._db_gen
        return con

    def has_index(self) -> bool:
        return self.db_path().exists()

    def index_info(self) -> dict:
        con = self._ro()
        if con is None:
            return {}
        try:
            return {k: v for k, v in con.execute("SELECT key, value FROM info")}
        except sqlite3.Error:
            return {}

    def entry(self, rel: str) -> "tuple[int, dict | None] | None":
        con = self._ro()
        if con is None:
            return None
        rel = rel.strip("/")
        d, _, name = rel.rpartition("/")
        try:
            try:
                row = con.execute("SELECT f.size, f.extra, f.changed FROM files f JOIN dirs "
                                  "ON f.dir = dirs.id WHERE dirs.dir = ? AND f.name = ?",
                                  (d, name)).fetchone()
            except sqlite3.OperationalError:            # an index from before ``changed``
                row = con.execute("SELECT f.size, f.extra, 0 FROM files f JOIN dirs "
                                  "ON f.dir = dirs.id WHERE dirs.dir = ? AND f.name = ?",
                                  (d, name)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        extra = json.loads(row[1]) if row[1] else None
        if row[2]:
            extra = dict(extra or {})
            extra["_chg"] = float(row[2])               # (as ``_rows`` gives it)
        return int(row[0] or 0), extra

    def is_dir(self, rel: str) -> bool:
        rel = rel.strip("/")
        if not rel:
            # The root is a folder while the collection is registered — also
            # while its first index is still being built (the share watcher
            # probes ``is_dir("/")``; a False marked it unavailable).
            return True
        con = self._ro()
        if con is None:
            return False
        try:
            return con.execute("SELECT 1 FROM dirs WHERE dir = ?", (rel,)).fetchone() is not None
        except sqlite3.Error:
            return False

    def list_dir(self, rel_dir: str) -> list[DirEntry]:
        rel_dir = rel_dir.strip("/")
        con = self._ro()
        if con is None:
            return []
        out: list[DirEntry] = []
        try:
            row = con.execute("SELECT id FROM dirs WHERE dir = ?", (rel_dir,)).fetchone()
            if row is None:
                return []
            did = row[0]
            for (d,) in con.execute("SELECT dir FROM dirs WHERE parent = ? AND id != ? ORDER BY dir",
                                    (did, did)):
                out.append(DirEntry(name=d.rsplit("/", 1)[-1], path="/" + d, is_dir=True))
            base = f"/{rel_dir}/" if rel_dir else "/"
            for name, size in con.execute("SELECT name, size FROM files WHERE dir = ? ORDER BY name",
                                          (did,)):
                out.append(DirEntry(name=name, path=base + name, is_dir=False, size=int(size or 0)))
        except sqlite3.Error:
            return []
        return out

    def _rows(self) -> list[tuple[str, int, int, dict | None]]:
        """``(rel, size, seen, extra)`` of every library row the index gives:
        each indexed tune, and for an opened package its playable members
        (``pkg.lha::member``) in its place — none for a package that holds
        nothing playable.  Blocking."""
        con = self._ro()
        if con is None:
            return []
        opened = self._opened() if self.packaged else {}
        out = []
        try:
            cur = con.execute("SELECT dirs.dir, f.name, f.size, f.seen, f.extra, f.changed "
                              "FROM files f JOIN dirs ON f.dir = dirs.id")
        except sqlite3.OperationalError:               # an index from before ``changed``
            cur = con.execute("SELECT dirs.dir, f.name, f.size, f.seen, f.extra, 0 "
                              "FROM files f JOIN dirs ON f.dir = dirs.id")
        for d, name, size, seen, extra, chg in cur:
            rel = f"{d}/{name}" if d else name
            ex = json.loads(extra) if extra else None
            if chg:
                ex = dict(ex or {})
                ex["_chg"] = float(chg)                  # (when the file last changed)
            if opened and rel in opened:
                out.extend(self._package_rows(rel, int(size or 0), int(seen or 0), ex,
                                              opened[rel]))
            else:
                out.append((rel, int(size or 0), int(seen or 0), ex))
        return out

    def _seen(self, rel: str) -> int:
        con = self._ro()
        if con is None:
            return 0
        d, _, name = rel.strip("/").rpartition("/")
        try:
            row = con.execute("SELECT f.seen FROM files f JOIN dirs ON f.dir = dirs.id "
                              "WHERE dirs.dir = ? AND f.name = ?", (d, name)).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0) if row else 0

    # Packages (archives opened on intent) ----------------------------------------
    def packages_path(self) -> Path:
        return self.dir() / "packages.sqlite"

    def _pkg_con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.packages_path(), timeout=10)
        con.execute("CREATE TABLE IF NOT EXISTS packages (pkg TEXT PRIMARY KEY, marker TEXT,"
                    " members TEXT, meta TEXT, at REAL, holds TEXT)")
        where = str(self.packages_path())
        if getattr(self, "_pkg_holds_col", None) != where:
            cols = {r[1] for r in con.execute("PRAGMA table_info(packages)")}
            if "holds" not in cols:          # (a table from before ``holds``)
                con.execute("ALTER TABLE packages ADD COLUMN holds TEXT")
                con.commit()
            self._pkg_holds_col = where
        return con

    def _opened(self, rel: str | None = None) -> dict:
        """``pkg → (marker, members, meta, at, holds)`` of the packages opened
        so far (``members``: ``[display name, size, stored name]`` of each
        playable member; ``holds``: what one with none holds,
        ``archive.census``) — of ``rel`` alone when given.  Blocking."""
        if not self.packages_path().exists():
            return {}
        with self._pkg_db_lock:
            con = self._pkg_con()
            try:
                if rel is None:
                    rows = con.execute("SELECT pkg, marker, members, meta, at, holds "
                                       "FROM packages").fetchall()
                else:
                    rows = con.execute("SELECT pkg, marker, members, meta, at, holds FROM packages "
                                       "WHERE pkg = ?", (rel,)).fetchall()
            finally:
                con.close()
        out = {}
        for pkg, marker, members, meta, at, holds in rows:
            try:
                out[pkg] = (marker, json.loads(members or "[]"), json.loads(meta) if meta else None,
                            float(at or 0), json.loads(holds) if holds else None)
            except ValueError:
                continue
        return out

    def _stored_marker(self, size: int, extra: dict | None, *, empty: bool = False) -> str:
        """The marker an opened package is kept under: its version
        (``package_marker``) and the member naming rules it was listed by
        (``archive.MEMBER_NAMING_VERSION`` — a change re-opens it).  One that
        held nothing playable (``empty``) also keeps what decided that — the
        formats this host plays (``archive.formats_fingerprint``): when they
        change it is listed again, to be opened anew."""
        from soniqboom.core import archive
        m = f"{self.package_marker(size, extra)}#n{archive.MEMBER_NAMING_VERSION}"
        return f"{m}#f{archive.formats_fingerprint()}" if empty else m

    def _package_version_moved(self, opened: tuple, size: int, extra: dict | None) -> bool:
        """Is the archive itself another version (not just listed by other
        naming rules — its cached copy then still serves)?"""
        if str(opened[0]).split("#n", 1)[0] != self.package_marker(size, extra):
            return True
        chg = float((extra or {}).get("_chg") or 0)
        return bool(chg) and chg > (opened[3] if len(opened) > 3 else 0)

    def _package_stale(self, opened: tuple, size: int, extra: dict | None) -> bool:
        """Was package ``opened`` (``_opened``'s value) opened from another
        version than the one its index row lists now — or listed by other
        member naming rules?"""
        if opened[0] != self._stored_marker(size, extra, empty=not opened[1]):
            if (not opened[1] and str(opened[0]).split("#f", 1)[0]
                    == self._stored_marker(size, extra)):
                # Found empty by other formats than this host plays: listed
                # again (to be opened anew) only when what it holds could now
                # name a tune — not every empty package at each change.
                from soniqboom.core import archive
                holds = opened[4] if len(opened) > 4 else None
                if archive.could_hold_a_tune(holds):
                    return True
            else:
                return True
        chg = float((extra or {}).get("_chg") or 0)
        return bool(chg) and chg > (opened[3] if len(opened) > 3 else 0)

    def _save_opened(self, rel: str, marker: str, members: list, meta: dict,
                     holds: dict | None = None) -> None:
        with self._pkg_db_lock:
            con = self._pkg_con()
            try:
                con.execute("INSERT OR REPLACE INTO packages (pkg, marker, members, meta, at, holds)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (rel, marker, json.dumps(members, separators=(",", ":")),
                             json.dumps(meta, separators=(",", ":")) if meta is not None else None,
                             time.time(),
                             json.dumps(holds, separators=(",", ":")) if holds else None))
                con.commit()
            finally:
                con.close()

    def _holds(self, rel: str) -> dict | None:
        """What package ``rel`` holds (``archive.census``) when its opening
        found nothing playable; None when unknown."""
        if not self.packages_path().exists():
            return None
        with self._pkg_db_lock:
            con = self._pkg_con()
            try:
                row = con.execute("SELECT holds FROM packages WHERE pkg = ?", (rel,)).fetchone()
            finally:
                con.close()
        try:
            return json.loads(row[0]) if row and row[0] else None
        except ValueError:
            return None

    def empty_package_ids(self) -> dict:
        """``track id → package`` of the packages whose opening found nothing
        playable (by the current rules) — rows that left the library.
        Cached until the packages file changes."""
        p = self.packages_path()
        try:
            st = p.stat()
        except OSError:
            return {}
        key = (str(p), st.st_mtime_ns, st.st_size)
        cache = self._empty_ids
        if cache is not None and cache[0] == key:
            return cache[1]
        out: dict = {}
        try:
            for rel, op in self._opened().items():
                if not op[1]:
                    out[self.track_id(rel)] = rel
        except Exception:                               # noqa: BLE001
            out = {}
        self._empty_ids = (key, out)
        return out

    def gone_reason(self, track_id: str) -> str | None:
        """Why ``track_id`` is no longer in the library when it is a package
        that held nothing playable (``empty_package_reason``); None else."""
        rel = self.empty_package_ids().get(track_id)
        if rel is None:
            return None
        from soniqboom.core import archive
        return empty_package_reason(rel.rsplit("/", 1)[-1],
                                    archive.describe_census(self._holds(rel),
                                                            music_disk=_music_disk(rel)))

    def _package_rows(self, rel: str, size: int, seen: int, extra: dict | None,
                      opened: tuple) -> list:
        """The library rows of package ``rel``: itself while it isn't opened
        (or its index row changed since), else one per playable member."""
        members, meta = opened[1], opened[2]
        if self._package_stale(opened, size, extra):
            return [(rel, size, seen, extra)]
        out = []
        for i, m in enumerate(members):
            name, msize = m[0], int(m[1] or 0)
            ex = dict(extra or {})
            ex.update(member=name, real=(m[2] if len(m) > 2 else name), first=(i == 0),
                      members=len(members), pkg_size=size)
            if meta:
                ex["pkg"] = meta
            out.append((f"{rel}::{name}", msize, seen, ex))
        return out

    def _open_package(self, rel: str, lane: str, *,
                      cached_only: bool = False) -> tuple[list, "dict | None", "dict | None"]:
        """Fetch package ``rel`` (inside its play's fetch window) and list its
        playable members — a ZIP too big to fetch whole by its central
        directory alone (ranged reads).  Returns ``(members, meta, holds)``:
        ``holds`` what it holds instead (``archive.census``) when no member
        plays.  ``PackageDamaged`` when the fetched file can't be opened.
        ``cached_only``: from the cached copy alone (``FileNotFoundError``
        without one), its facts not read.  Blocking."""
        from soniqboom.core import archive
        from soniqboom.core.filesource import get_source
        from soniqboom.core.remote_cache import get_cache
        cache = get_cache()
        remote = "/" + rel
        local = cache.get_cached(self.root, remote)
        members = holds = None
        if local is None:
            if cached_only:
                raise FileNotFoundError(errno.ENOENT, "not cached", rel)
            src = get_source(self.root)
            if src is None:
                raise ConnectionError(f"{self.label} can't be reached right now")
            if rel.lower().endswith(".zip"):
                got = self._zip_members_ranged(src, remote, lane)
                if got is not None:
                    members, holds = got
            if members is None:
                local = cache.fetch(self.root, remote, src, lane=lane)
        if members is None:
            from soniqboom.core import lha as _lha
            try:
                names = archive.list_members(local, strict=True)
                real = archive._members(local)
                sizes = _member_sizes(local)
            except (OSError, ConnectionError):
                raise
            except _lha.LhaUnsupported as exc:
                # Not damaged: packed with a method SoniqBoom doesn't unpack.
                meta = None if cached_only else self._package_meta_safe(rel)
                return [], meta, {"files": 0, "unsupported_method": exc.method}
            except Exception as exc:                    # noqa: BLE001
                raise PackageDamaged(str(exc) or type(exc).__name__) from exc
            members = [[n, int(sizes.get(real.get(n, n), 0)), real.get(n, n)] for n in names]
            nested, opened, inner = _nested_members(
                archive.raw_namelist(local), sizes, lambda r: archive.read_member(local, r))
            members += nested
            if not members:
                holds = _local_census(local, opened, inner)
        meta = None if cached_only else self._package_meta_safe(rel)
        return members, meta, (holds if not members else None)

    def _package_meta_safe(self, rel: str) -> dict | None:
        """``package_meta`` — None when it couldn't be read now (no fetch
        window, offline): tried again on the package's next opening."""
        from soniqboom.core.filesource import get_source
        src = get_source(self.root)
        if src is None:
            return None
        try:
            return self.package_meta(rel, src) or {}
        except FileNotFoundError:
            return {}
        except Exception as exc:                        # noqa: BLE001
            log.debug("%s: facts of package %s not read (%s)", self.label, rel, exc)
            return None

    def _zip_members_ranged(self, src, remote: str, lane: str) -> list | None:
        """The playable members of a big remote ZIP, read from its central
        directory by ranges; None for a ZIP small enough to fetch whole."""
        import io
        import zipfile
        from soniqboom.core import archive, remote_zip
        st = src.stat(remote, lane=lane, strict=True)
        size = int(getattr(st, "size", 0) or 0)
        if not size:
            # No length from the archive (``stat`` asked by HEAD, then by a
            # range read): never a whole download of what the index says is
            # too big for one.
            hint = self._index_size(remote.lstrip("/"))
            from soniqboom.core.remote_cache import get_cache
            if hint and hint > get_cache().whole_download_limit:
                raise ConnectionError(f"{self.label} tells no size for "
                                      f"{remote.rsplit('/', 1)[-1]} — too big to fetch whole")
            return None
        if size < remote_zip._SUBSET_MIN_ARCHIVE:
            return None
        rf = remote_zip.RangeFile(src, remote, size,
                                  (self.root, remote, size, float(getattr(st, "mtime", 0) or 0)),
                                  lane)
        from soniqboom.core.remote_zip import _zip_head_reader
        holds = None
        try:
            with zipfile.ZipFile(io.BufferedReader(rf, 64 * 1024)) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                sizes = {i.filename: i.file_size for i in infos}
                head_of = _zip_head_reader(zf)
                # Named as a fetched copy names them (the same reader rules:
                # members only their bytes name included) — their first bytes
                # by ranges — and the archives / disk images inside read too.
                names = archive._build_map(list(sizes), head_of, sniff=True)
                members = [[disp, int(sizes.get(real, 0)), real] for disp, real in names.items()]
                nested, opened, inner = _nested_members(list(sizes), sizes, zf.read)
                members += nested
                if not members:
                    holds = archive.census(list(sizes), head_of, heads=16,
                                           opened=opened, inner_names=inner)
        except zipfile.BadZipFile as exc:
            raise PackageDamaged(str(exc)) from exc
        return members, holds

    def _index_size(self, rel: str) -> int:
        """The size the index lists for ``rel`` (its approximate one when it
        has no exact one); 0 when unknown."""
        try:
            e = self.entry(rel)
        except Exception:                               # noqa: BLE001
            return 0
        if not e:
            return 0
        size, extra = e[0], e[1]
        return int(size or (extra or {}).get("approx") or 0)

    async def expand(self, track_id: str, path: str, *, lane: str = "stream",
                     cached_only: bool = False) -> str | None:
        """The path to play for library row ``path`` of this collection: a
        package (an archive the index lists as one row) is opened the first
        time — fetched on the play's (prewarm's, Track Info's) grant, its
        playable members listed and written to the library in its place (the
        first keeps the package's id) — and the member that ``track_id`` now
        is returned.  None for any row that is no package.  Raises
        ``PackageEmpty`` when the package holds nothing playable (its row
        leaves the library at once — ``_leave_library``), ``PackageDamaged``
        when the fetched file can't be opened (its row is marked corrupt), and
        what the fetch raised.  ``cached_only``: opened from its cached copy
        alone (``FileNotFoundError`` without one)."""
        rel = self.rel_of(path)
        if not rel or not self.is_package(rel):
            return None
        lock = self._pkg_locks.setdefault(rel, asyncio.Lock())
        loop = asyncio.get_running_loop()
        async with lock:
            e = await loop.run_in_executor(None, self.entry, rel)
            if e is None:
                raise FileNotFoundError(errno.ENOENT, f"not in the {self.label} index", rel)
            size, extra = e
            import functools
            op = (await loop.run_in_executor(None, self._opened, rel)).get(rel)
            before = None                  # the member list of an earlier opening
            restore = True                 # the members' reads kept when it folded back
            holds = None
            if op is None or self._package_stale(op, size, extra):
                before = op
                if op is not None and self._package_version_moved(op, size, extra):
                    # Another version of the package: what is cached from
                    # before its change goes (renders of its tunes too), and
                    # what its tunes' plays read.
                    self._drop_cached(rel, before=float((extra or {}).get("_chg") or 0) or None)
                    restore = False
                    old_ids = [self.row_id(f"{rel}::{m[0]}", {"first": i == 0})
                               for i, m in enumerate(op[1] or ())]
                    await loop.run_in_executor(None, self._drop_parked_rows, old_ids)
                try:
                    members, meta, holds = await loop.run_in_executor(
                        None, functools.partial(self._open_package, rel, lane,
                                                cached_only=cached_only))
                except PackageDamaged as exc:
                    await self._mark_damaged(rel, str(exc))
                    raise
                marker = self._stored_marker(size, extra, empty=not members)
                await loop.run_in_executor(None, self._save_opened, rel, marker, members, meta,
                                           holds)
                op = (marker, members, meta, time.time())
                log.info("%s: opened %s — %d playable member(s)", self.label, rel, len(members))
            elif op[2] is None and not cached_only:
                # Its facts couldn't be read when it was opened (no fetch
                # window then): now, inside this one.
                meta = await loop.run_in_executor(None, self._package_meta_safe, rel)
                if meta is not None:
                    holds = await loop.run_in_executor(None, self._holds, rel)
                    await loop.run_in_executor(None, self._save_opened, rel, op[0], op[1], meta,
                                               holds)
                    op = (op[0], op[1], meta, time.time())
            self.prepare_build()
            seen = await loop.run_in_executor(None, self._seen, rel)
            rows = self._package_rows(rel, size, seen, extra, op)
            await self._write_package_rows(rel, rows, restore=restore)
            if before is not None and rows:
                await self._carry_renamed_members(rel, before[1], op[1])
            if not rows:
                await self._leave_library(rel, before)
            await self._purge_derived()
        if not rows:
            from soniqboom.core import archive
            if holds is None:
                holds = await loop.run_in_executor(None, self._holds, rel)
            raise PackageEmpty(rel.rsplit("/", 1)[-1],
                               archive.describe_census(holds, music_disk=_music_disk(rel)))
        for r, _s, _n, ex in rows:
            if self.row_id(r, ex) == track_id:
                return self.track_path(r)
        return self.track_path(rows[0][0])

    async def settle_empty_packages(self) -> int:
        """Packages an opening found empty by other rules than this code's (a
        format learned since, other naming rules — listed again as packages)
        and kept in the cache: opened again from that copy now, so their tunes
        are listed — or their rows leave — at once, not at their next play.
        Returns how many were opened."""
        if not self.packaged or not self.enabled():
            return 0
        from soniqboom.core.remote_cache import get_cache
        loop = asyncio.get_running_loop()
        opened = await loop.run_in_executor(None, self._opened)
        cache = get_cache()
        n = 0
        for rel, op in opened.items():
            if op[1]:
                continue
            e = await loop.run_in_executor(None, self.entry, rel)
            if e is None or not self._package_stale(op, e[0], e[1]):
                continue
            if cache.get_cached(self.root, "/" + rel) is None:
                continue                            # (opened again on its next play)
            try:
                await self.expand(self.track_id(rel), self.track_path(rel), lane="scan",
                                  cached_only=True)
            except PackageEmpty:
                pass
            except Exception as exc:                    # noqa: BLE001
                log.debug("%s: %s not opened again (%s)", self.label, rel, exc)
                continue
            n += 1
            await asyncio.sleep(0)
        if n:
            log.info("%s: %d package(s) found empty before opened again by the current rules",
                     self.label, n)
        return n

    async def _carry_renamed_members(self, rel: str, before: list, now: list) -> int:
        """Package ``rel`` opened again lists members under other names than
        its earlier opening (``before``) did — other member naming rules:
        ``mod.Opus`` became ``mod.Opus.mod``, the stored member is the same.
        Each old row's rating, plays, history, playlist entries and waveform
        (``store.remap_track_ids``), Subsonic stars, queue and bookmarks move
        to its new row; the old row goes.  Returns the members moved."""
        from soniqboom.core.store import PARKED_PLAYLIST_IDS

        def _ids(members: list) -> dict:
            out: dict = {}
            for i, m in enumerate(members or ()):
                real = m[2] if len(m) > 2 else m[0]
                # (``\`` and ``/`` alike: lhasa listed an archive's members
                # under ``/`` directories, the built-in reader keeps ``\``)
                out.setdefault(real.replace("\\", "/"),
                               self.row_id(f"{rel}::{m[0]}", {"first": i == 0}))
            return out
        old, new = _ids(before), _ids(now)
        mapping = {old[r]: new[r] for r in old if r in new and old[r] != new[r]}
        if not mapping:
            return 0
        store = _store()
        counts = store.remap_track_ids(mapping)
        try:
            from soniqboom.core.subsonic_state import get_state
            await get_state().remap_track_ids(mapping)
        except Exception:                               # noqa: BLE001
            log.debug("%s: Subsonic stars / queues of renamed members not moved",
                      self.label, exc_info=True)
        targets = set(mapping.values())
        parked = store.get_config(PARKED_PLAYLIST_IDS) or []
        if any(p in mapping for p in parked):
            store.set_config(PARKED_PLAYLIST_IDS, sorted(
                {mapping.get(p, p) for p in parked} - {t for t in targets if t in store._tracks}))
        # (A parked row of an old name holds what reading it under that name gave.)
        await asyncio.get_running_loop().run_in_executor(None, self._drop_parked_rows,
                                                         list(mapping))
        gone = [o for o in mapping if o not in targets and o in store._tracks]
        if gone:
            store.delete_track_ids(gone)
            await self._after_library_change(package=rel)
        log.info("%s: %d member(s) of %s listed under a new name keep their user data (%s)",
                 self.label, len(mapping), rel, counts)
        return len(mapping)

    async def _write_package_rows(self, rel: str, rows: list, *, restore: bool = True) -> None:
        """Put an opened package's member rows in the library in place of its
        own (a package with none: nothing here — ``_leave_library``).
        ``restore``: a member played before the package folded back (listed
        by other naming rules) gets what its play read back."""
        from soniqboom.core.store import share_values
        if not self.enabled() or not catalogue.is_catalogue_root(self.root):
            return                          # switched off meanwhile: no rows of it
        store = _store()
        loop = asyncio.get_running_loop()
        parked = await loop.run_in_executor(None, self._load_parked_rows) if restore else {}
        used: list = []
        pkg_tid = self.track_id(rel)
        old = store.get_track(pkg_tid) or {}
        added = int(old.get("added_at") or 0) or int(time.time())
        parent = "/" + rel.rpartition("/")[0]
        dh = hashlib.sha256(parent.encode()).hexdigest()[:16]
        store.store_hash_lookups_batch([parent])
        built = []
        now = time.time()
        t0 = time.perf_counter()
        for r, size, _seen, ex in rows:
            if time.perf_counter() - t0 > 0.008:
                await asyncio.sleep(0)          # (a big package: in slices)
                t0 = time.perf_counter()
            tid = self.row_id(r, ex)
            self._expanded_at[tid] = now                # (an apply running now leaves it)
            cur = store.get_track(tid)
            if cur is not None and cur.get("path") == self.track_path(r):
                continue                                # written by an earlier opening
            t = share_values(self._build(r, size, ex, added_at=added,
                                         dir_hash=dh, root_h=self.root_hash()))
            kept = parked.get(tid)
            if kept is not None and is_merged(kept) and kept.get("path") == t.get("path"):
                t = self._rebuild_merged(kept, t)       # read by a play before it folded back
                used.append(tid)
            built.append(t)
        if not rows or not built:
            return
        now = time.time()
        for t in built:
            self._expanded_at[t["id"]] = now
        # In chunks with a loop turn after each: one upsert of thousands of
        # members held the loop for a quarter second.
        for i in range(0, len(built), 250):
            store.upsert_tracks_batch(built[i:i + 250])
            if i + 250 < len(built):
                await asyncio.sleep(0)
        if used:
            await loop.run_in_executor(None, self._drop_parked_rows, used)
        await self._broadcast_updated(pkg_tid)
        await self._after_library_change(package=rel)

    async def _leave_library(self, rel: str, before: "tuple | None" = None) -> list:
        """Package ``rel`` holds nothing playable: its row — and the rows of
        the members an earlier opening listed (``before``) — leave the
        library now, as the next apply of the index leaves them out (that
        apply reads the empty opening too: ``_rows``): playlist entries
        parked, a played member's row kept for its return
        (``_park_user_refs``), every open page told
        (``catalogue_tracks_left``).  Returns the ids that left."""
        store = _store()
        ids = [self.track_id(rel)]
        for m in ((before[1] or ()) if before else ()):
            tid = self.track_id(f"{rel}::{m[0]}")
            if tid not in ids:
                ids.append(tid)
        gone = [t for t in ids if t in store._tracks]
        if not gone:
            return []
        now = time.time()
        for t in gone:
            self._expanded_at[t] = now          # (an apply running now doesn't write it back)
        try:
            await self._park_user_refs(gone)
        except Exception:                               # noqa: BLE001
            log.warning("%s: playlist / played-tune bookkeeping failed", self.label,
                        exc_info=True)
        store.delete_track_ids(gone)
        log.info("%s: %s holds nothing playable — %d row(s) left the library",
                 self.label, rel, len(gone))
        await self._after_tracks_left(rel, gone)
        return gone

    async def _after_tracks_left(self, rel: str, ids: list) -> None:
        """Rows of package ``rel`` left the library (``_leave_library``): the
        folder tree's counts, the genres, and every open page — its lists and
        counts re-read in place, a queue or Track Info holding one of them
        learns why (``catalogue_tracks_left``)."""
        try:
            from soniqboom.api import fstree
            fstree.invalidate_scan_root_cache(self.root)
        except Exception:                               # noqa: BLE001
            pass
        try:
            from soniqboom.core import genre_derive
            genre_derive.schedule()
        except Exception:                               # noqa: BLE001
            pass
        reason = self.gone_reason(self.track_id(rel)) or ""
        try:
            from soniqboom.api import library
            folder = self.root + ("/" + rel.rpartition("/")[0] if "/" in rel else "")
            await library.broadcast_catalogue_tracks_left(
                self.root, folder, list(ids), {t: reason for t in ids})
        except Exception:                               # noqa: BLE001
            log.debug("tracks-left broadcast failed", exc_info=True)

    async def _mark_damaged(self, rel: str, why: str) -> None:
        """Package ``rel`` was fetched but can't be opened: its cached copy
        goes (a play asks for it again — a cut download comes whole next
        time) and its row, while it is still the package's, is marked corrupt
        (passed by when a list plays through; a pick says why)."""
        self._drop_cached(rel)
        await self._purge_derived()
        tid = self.track_id(rel)
        store = _store()
        t = store.get_track(tid)
        if t is None or "::" in str(t.get("path") or ""):
            return                                  # (opened before: its members play on)
        why = " ".join(str(why or "").split())[:200]
        # (what went wrong goes to the log: the reader's own words can name
        # local paths, and every user sees the detail)
        detail = f"{DAMAGED_DETAIL_PREFIX} — it can't be opened."
        if t.get("defect") == "corrupt" and t.get("defect_detail") == detail:
            return
        store.update_track_fields_batch([(tid, {"defect": "corrupt", "defect_detail": detail})])
        log.info("%s: %s can't be opened (%s) — marked corrupt", self.label, rel, why)
        await self._broadcast_updated(tid)

    def _build_index_db(self, rows: Iterable, info: dict, *, accept_shrink: bool) -> dict:
        """Write a new index sqlite from ``rows`` (keeping each known file's
        first-seen time) and swap it in.  Blocking."""
        d = self.dir()
        tmp = d / f"index.sqlite.{uuid.uuid4().hex[:12]}.building"
        prev = self.db_path() if self.db_path().exists() else None
        now = int(time.time())
        prev_seen: dict = {}
        prev_state: dict = {}          # rel → (size, extra, changed)
        baseline = now
        prev_rows = 0
        info_prev: dict = {}
        if prev is not None:
            pcon = sqlite3.connect(f"file:{prev}?mode=ro", uri=True)
            try:
                try:
                    cur = pcon.execute("SELECT dirs.dir, f.name, f.seen, f.size, f.extra, f.changed "
                                       "FROM files f JOIN dirs ON f.dir = dirs.id")
                except sqlite3.OperationalError:       # an index from before ``changed``
                    cur = pcon.execute("SELECT dirs.dir, f.name, f.seen, f.size, f.extra, 0 "
                                       "FROM files f JOIN dirs ON f.dir = dirs.id")
                for dd, name, sn, sz, ex, chg in cur:
                    rel = f"{dd}/{name}" if dd else name
                    prev_seen[rel] = int(sn or 0)
                    prev_state[rel] = (int(sz or 0), ex, float(chg or 0))
                info_prev = {k: v for k, v in pcon.execute("SELECT key, value FROM info")}
                baseline = int(float(info_prev.get("baseline") or now))
            finally:
                pcon.close()
            prev_rows = len(prev_seen)
        con = sqlite3.connect(tmp)
        try:
            con.executescript(
                "CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE dirs (id INTEGER PRIMARY KEY, dir TEXT UNIQUE, parent INTEGER);"
                "CREATE TABLE files (dir INTEGER, name TEXT, size INTEGER, seen INTEGER, extra TEXT,"
                " changed INTEGER, PRIMARY KEY (dir, name));"
                "CREATE INDEX dirs_parent ON dirs(parent);")
            dir_ids: dict[str, int] = {}

            def _dir_id(path: str) -> int:
                i = dir_ids.get(path)
                if i is not None:
                    return i
                parent = None if not path else _dir_id(path.rpartition("/")[0])
                cur = con.execute("INSERT INTO dirs (dir, parent) VALUES (?, ?)", (path, parent))
                i = dir_ids[path] = cur.lastrowid
                if parent is None:
                    con.execute("UPDATE dirs SET parent = ? WHERE id = ?", (i, i))
                return i
            _dir_id("")
            n = 0
            batch = []
            changed_n = 0
            for rel, size, extra in rows:
                rel = rel.strip("/")
                if not rel:
                    continue
                dd, _, name = rel.rpartition("/")
                was = prev_state.get(rel)
                changed = 0
                if was is not None:
                    try:
                        prev_extra = json.loads(was[1]) if was[1] else None
                    except ValueError:
                        prev_extra = None
                    if self.row_changed(was[0], prev_extra, int(size or 0), extra):
                        changed = time.time()       # another version of the file
                        changed_n += 1
                    else:
                        changed = was[2]
                batch.append((_dir_id(dd), name, int(size or 0),
                              prev_seen.get(rel) or self.seen_for(extra, now),
                              json.dumps(extra, separators=(",", ":")) if extra else None,
                              changed or None))
                n += 1
                if len(batch) >= 5000:
                    con.executemany("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)", batch)
                    batch = []
            if batch:
                con.executemany("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)", batch)
            if changed_n:
                log.info("%s: %d file(s) changed on the archive since the last index",
                         self.label, changed_n)
            if n < self.min_rows:
                raise IndexRefused(f"the {self.label} index lists only {n} tunes — implausible")
            if prev_rows and not accept_shrink and n < prev_rows * (1 - _MAX_SHRINK):
                raise IndexShrank(f"the new {self.label} index lists {n} tunes, the current one "
                                  f"{prev_rows} — press Refresh now to take it anyway")
            # (What an apply recorded belongs to the library, not to one index file.)
            carried = {k: info_prev[k] for k in ("applied_at", "built_rules", "built_naming")
                       if prev is not None and k in info_prev}
            values = {"schema": _INDEX_SCHEMA, "built_at": time.time(), "rows": n,
                      "baseline": baseline if prev is not None else now,
                      "rules": self.index_rules, **carried, **(info or {})}
            con.executemany("INSERT OR REPLACE INTO info VALUES (?, ?)",
                            [(k, str(v)) for k, v in values.items() if v is not None])
            con.commit()
        except BaseException:
            con.close()
            tmp.unlink(missing_ok=True)
            raise
        con.close()
        os.replace(tmp, self.db_path())
        self._db_gen += 1
        return {"rows": n}

    def _touch_info(self, values: dict) -> None:
        """Update the index's info (a refresh that found it unchanged).  Blocking."""
        if not self.db_path().exists():
            return
        con = sqlite3.connect(self.db_path())
        try:
            con.executemany("INSERT OR REPLACE INTO info VALUES (?, ?)",
                            [(k, str(v)) for k, v in values.items() if v is not None])
            con.commit()
        finally:
            con.close()
        self._db_gen += 1

    def _update_index(self, *, force: bool, accept_shrink: bool) -> dict:
        """Fetch the index and rebuild the sqlite when it changed.  Blocking.
        Offline mode (``netpolicy``): ``Offline``, nothing fetched."""
        import sys as _sys
        from soniqboom.core import netpolicy
        netpolicy.check(getattr(_sys.modules.get(type(self).__module__), "BASE", None))
        d = self.dir()
        work = d / f"download.{uuid.uuid4().hex[:8]}"
        work.mkdir(exist_ok=True)
        try:
            info = {} if force else self.index_info()
            if info and str(info.get("rules") or "1") != str(self.index_rules):
                info = {}                 # read by other rules: downloaded again, rebuilt
            got = self.fetch_index(work, info)
            if got is None and self.has_index():
                self._touch_info({"built_at": time.time()})
                return {"unchanged": True}
            if got is None:
                got = self.fetch_index(work, {})
            rows, new_info = got
            res = self._build_index_db(rows, new_info, accept_shrink=accept_shrink)
            self.after_index()
            return res
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

    # Status ------------------------------------------------------------------------
    def tune_count(self) -> int:
        try:
            return len(_store()._tag_scan_root_hash.get(self.root_hash()) or ())
        except Exception:                               # noqa: BLE001
            return 0

    def status(self) -> dict:
        out = dict(self._status)
        info = self.index_info()
        out.update(key=self.key, label=self.label, root=self.root, site=self.site_url,
                   description=self.description, enabled=self.enabled(),
                   subsonic=self.subsonic_visible(), interval=self.interval(),
                   intervals=dict(INTERVAL_LABELS), tunes=self.tune_count(),
                   options=self.options(), option_labels=self.option_labels(),
                   how=self.how, packaged=self.packaged)
        try:
            out["index_rows"] = int(info.get("rows") or 0)
            out["last_refresh"] = float(info.get("built_at") or 0) or None
        except (TypeError, ValueError):
            out["index_rows"], out["last_refresh"] = 0, None
        out["index_date"] = _iso_date(info.get("index_date"))
        out["next_refresh"] = self._next_due() if (out["last_refresh"] and out["enabled"]) else None
        # Folders the archive couldn't list at the last refresh: their tunes
        # are kept (from the refresh before), marked here.
        out["unavailable_folders"] = self.unavailable_folders()
        try:
            from soniqboom.core.filesource import _active_sources
            src = _active_sources.get(self.root)
            out["offline"] = bool(src is not None and src.is_offline())
        except Exception:                               # noqa: BLE001
            out["offline"] = False
        return out

    def _progress(self, phase: str, done: int, total: int) -> None:
        self._status.update(phase=phase, done=done, total=total)

    # Root registration ----------------------------------------------------------------
    def _capture_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _is_music(self, rel: str) -> bool:
        return self.entry(rel) is not None

    def _register_root(self) -> None:
        from soniqboom.core.filesource import _active_sources, register_source
        self._capture_loop()
        catalogue.register_root(self.root, key=self.key, label=self.label,
                                is_music=self._is_music, subsonic_visible=self.subsonic_visible)
        if not isinstance(_active_sources.get(self.root), CatalogueHTTPSource):
            register_source(self.root, CatalogueHTTPSource(self))

    def _unregister_root(self) -> None:
        from soniqboom.core.filesource import remove_source
        remove_source(self.root)
        catalogue.unregister_root(self.root)

    def _ensure_scan_dir(self) -> None:
        store = _store()
        store.store_hash_lookup(self.root)
        if self.root not in store._scan_dirs:
            store.upsert_scan_dir(self.root, status="ok")

    def _schedule_status_flip(self, status: str, *, after: float = 0.0) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _flip() -> None:
            try:
                if not catalogue.is_catalogue_root(self.root):
                    return
                if status == "ok":
                    from soniqboom.core.filesource import _active_sources
                    src = _active_sources.get(self.root)
                    if src is not None and src.is_offline():
                        return
                sd = _store()._scan_dirs.get(self.root)
                if sd is not None and (sd.get("status") or "ok") != status:
                    _store().set_scan_dir_status(self.root, status)
            except Exception:                           # noqa: BLE001
                pass

        def _arm() -> None:
            if after > 0:
                loop.call_later(after, _flip)
            else:
                _flip()
        try:
            loop.call_soon_threadsafe(_arm)
        except RuntimeError:
            pass

    # Applying the index ---------------------------------------------------------------
    async def apply_index(self) -> dict:
        """Bring the library's tunes of this collection in line with the index:
        add the missing, remove the unlisted (parked: playlist entries and the
        rows of played tunes are kept for when they come back), rewrite the
        changed.  Chunked store writes in one batch section."""
        from soniqboom.core import scanner
        from soniqboom.core.modland_catalog import _delete_sliced, _slices
        store = _store()
        loop = asyncio.get_running_loop()
        rh = self.root_hash()
        self._progress("reading the index", 0, 0)
        self.prepare_build()
        started = time.time()            # (before the rows: an opening during the read is newer)
        rows = await loop.run_in_executor(None, self._rows)
        if not rows:
            raise RuntimeError(f"the {self.label} index lists no tunes")
        excluded = self.excluded_rels()
        info = self.index_info()
        try:
            baseline = int(float(info.get("baseline") or 0))
        except (TypeError, ValueError):
            baseline = 0
        try:
            applied_at = float(info.get("applied_at") or 0)
        except (TypeError, ValueError):
            applied_at = 0.0
        current = store.get_track_ids_for_scan_root(rh)
        tracks = store._tracks
        parked = await loop.run_in_executor(None, self._load_parked_rows)
        parked_used: list = []
        # Rows built by other build rules than this code's: all built again.
        rules_changed = bool(current) and str(info.get("built_rules") or "1") != str(self.build_rules)

        def _plan():
            want = {self.row_id(rel, extra): (rel, size, seen, extra)
                    for rel, size, seen, extra in rows
                    if rel.split("::", 1)[0] not in excluded}
            to_add = [tid for tid in want if tid not in current]
            to_remove = [tid for tid in list(current) if tid not in want]
            changed = [tid for tid, (rel, size, _s, extra) in want.items()
                       if tid in current and tid in tracks
                       and (rules_changed
                            or tracks[tid].get("path") != self.track_path(rel)
                            or float((extra or {}).get("_chg") or 0) > applied_at
                            or self.changed(tracks[tid], rel, size, extra))]
            dirs = sorted({"/" + want[t][0].split("::", 1)[0].rpartition("/")[0]
                           for t in (*to_add, *changed)})
            return want, to_add, to_remove, changed, dirs, _moves(want, to_add, to_remove)

        def _moves(want, to_add, to_remove) -> dict:
            """old id → new id of the tunes the archive moved: a row that
            leaves and one that arrives with the same ``move_key`` (each key
            once on either side — an ambiguous key moves nothing)."""
            if not to_add or not to_remove:
                return {}
            arrive: dict = {}
            for tid in to_add:
                rel, size, _s, extra = want[tid]
                k = self.move_key(rel, size, extra)
                if k:
                    arrive[k] = None if k in arrive else tid
            if not arrive:
                return {}
            leave: dict = {}
            for tid in to_remove:
                t = tracks.get(tid)
                k = self.track_move_key(t) if t else None
                if k and k in arrive:
                    leave[k] = None if k in leave else tid
            return {o: arrive[k] for k, o in leave.items() if o and arrive.get(k)}
        want, to_add, to_remove, changed, dirs, moves = await loop.run_in_executor(None, _plan)
        # Package rows an older version marked "holds nothing SoniqBoom plays"
        # (``defect`` corrupt) and listed by these rules again: unmarked
        # (their next opening says what they hold).
        legacy = [(tid, {"defect": None, "defect_detail": None}) for tid in want
                  if tid in tracks and tracks[tid].get("defect_detail") == _LEGACY_EMPTY_DETAIL]
        if legacy:
            store.update_track_fields_batch(legacy)
        if self._expanded_at:
            # A package opened by a play while this ran: its rows are newer
            # than the rows read above.
            fresh = {t for t, at in self._expanded_at.items() if at >= started}
            if fresh:
                to_remove = [t for t in to_remove if t not in fresh]
                changed = [t for t in changed if t not in fresh]
            self._expanded_at = {t: at for t, at in self._expanded_at.items() if at >= started}
        result = {"added": 0, "removed": 0, "changed": 0, "tracks": len(want)}
        total = len(to_add) + len(to_remove) + len(changed)
        if not total:
            if (str(info.get("built_rules") or "") != str(self.build_rules)
                    or str(info.get("built_naming") or "") != self._naming()):
                await loop.run_in_executor(None, self._touch_info,
                                           {"built_rules": self.build_rules,
                                            "built_naming": self._naming()})
            return result
        if rules_changed:
            log.info("%s: rebuilding %d tunes by the current build rules (%s → %s)", self.label,
                     len(changed), info.get("built_rules") or "1", self.build_rules)
        memo: dict = {}

        def _dir_hash(rel: str) -> str:
            dd = "/" + rel.split("::", 1)[0].rpartition("/")[0]
            h = memo.get(dd)
            if h is None:
                h = memo[dd] = hashlib.sha256(dd.encode()).hexdigest()[:16]
            return h

        def _added(seen: int) -> int:
            return _INITIAL_ADDED_AT if seen and baseline and seen <= baseline else int(seen or time.time())

        for i in range(0, len(dirs), 5_000):
            store.store_hash_lookups_batch(dirs[i:i + 5_000])
            await asyncio.sleep(0)
        done = 0
        store.enter_batch_mode()
        try:
            moved_from = {n: o for o, n in moves.items()}

            def _mk(tid: str) -> dict:
                rel, size, seen, extra = want[tid]
                was = tracks.get(moved_from[tid]) if tid in moved_from else None
                t = self._build(rel, size, extra,
                                added_at=(int(was.get("added_at") or 0) or _added(seen))
                                if was else _added(seen),
                                dir_hash=_dir_hash(rel), root_h=rh)
                old = parked.get(tid) if parked else None
                if (old is not None and is_merged(old) and not self.changed(old, rel, size, extra)
                        and float((extra or {}).get("_chg") or 0) <= (old.get("mtime") or 0)):
                    t = self._rebuild_merged(old, t)       # (by the current build rules)
                    parked_used.append(tid)
                return t
            self._progress("adding tunes", 0, total)
            async for chunk in _slices(to_add, _mk):
                chunk = self._not_opened_since(chunk, started)
                store.upsert_tracks_batch(chunk)
                done += len(chunk)
                result["added"] += len(chunk)
                self._progress("adding tunes", done, total)
                await asyncio.sleep(0)

            folded: dict = {}
            withdraw: list = []

            def _mk_changed(tid: str) -> dict:
                rel, size, seen, extra = want[tid]
                old = tracks.get(tid) or {}
                chg = float((extra or {}).get("_chg") or 0)
                fresh = self._build(rel, size, extra,
                                    added_at=old.get("added_at") or _added(seen),
                                    dir_hash=_dir_hash(rel), root_h=rh)
                gone = self._withdrawals(old, fresh)
                if gone:
                    withdraw.append((tid, gone))
                if (is_merged(old) and "::" in (old.get("path") or "")
                        and "::" not in (fresh.get("path") or "")):
                    # A played first member folding back into its package (the
                    # package's id): its read is kept for the next opening.
                    folded[tid] = dict(old)
                if chg > applied_at:
                    # Its file changed on the archive: the cached copy fetched
                    # before then is stale, the row is read anew.  (A row
                    # rewritten for another reason — a new description, a
                    # package folding back to be listed by newer naming rules —
                    # keeps it.)
                    self._drop_cached(rel, before=chg)
                    return fresh
                if (rules_changed and is_merged(old) and old.get("path") == fresh.get("path")
                        and not self.changed(old, rel, size, extra)):
                    return self._rebuild_merged(old, fresh)     # what its play read stays
                return fresh
            async for chunk in _slices(changed, _mk_changed):
                chunk = self._not_opened_since(chunk, started)
                store.upsert_tracks_batch(chunk)
                if withdraw:
                    # (after the upsert: it carries such values over a rebuild)
                    kept = {t["id"] for t in chunk}
                    store.update_track_fields_batch([w for w in withdraw if w[0] in kept])
                    withdraw.clear()
                done += len(chunk)
                result["changed"] += len(chunk)
                self._progress("updating tunes", done, total)
                await asyncio.sleep(0)
            if folded:
                def _park_folded() -> None:
                    kept = self._load_parked_rows()
                    kept.update(folded)
                    self._save_parked_rows(kept)
                await loop.run_in_executor(None, _park_folded)
            to_remove = [t for t in to_remove if self._expanded_at.get(t, 0) < started]
            moves = {o: n for o, n in moves.items() if o in to_remove and n in tracks}
            if moves:
                # Moved on the archive (same file, another folder): ratings,
                # plays, history, playlist entries, waveforms and Subsonic
                # stars / queues / bookmarks go with the tune before its old
                # row goes.
                counts = store.remap_track_ids(moves)
                try:
                    from soniqboom.core.subsonic_state import get_state
                    await get_state().remap_track_ids(moves)
                except Exception:                       # noqa: BLE001
                    log.debug("%s: Subsonic stars / queues of moved tunes not moved",
                              self.label, exc_info=True)
                result["moved"] = len(moves)
                log.info("%s: %d tune(s) moved on the archive keep their user data (%s)",
                         self.label, len(moves), counts)
            if to_remove:
                try:
                    await self._park_user_refs(to_remove)
                except Exception:                       # noqa: BLE001
                    log.warning("%s: playlist / played-tune bookkeeping failed", self.label,
                                exc_info=True)
                base = done

                def _removed(n: int) -> None:
                    result["removed"] = n
                    self._progress("removing tunes", base + n, total)
                await _delete_sliced(store, to_remove, _removed)
        finally:
            self._progress("sorting", done, total)
            await scanner._async_exit_batch_mode(store)
        if parked_used:
            await loop.run_in_executor(None, self._drop_parked_rows, parked_used)
        await loop.run_in_executor(None, self._touch_info,
                                   {"applied_at": started, "built_rules": self.build_rules,
                                    "built_naming": self._naming()})
        await self._purge_derived()
        self._unpark_returned_ids(store)
        await self._after_library_change()
        log.info("%s: %d added, %d updated, %d removed (%d tunes)", self.label,
                 result["added"], result["changed"], result["removed"], result["tracks"])
        return result

    async def _broadcast_updated(self, track_id: str) -> None:
        try:
            from soniqboom.api import library
            await library.broadcast_track_updated(track_id)
        except Exception:                               # noqa: BLE001
            log.debug("track-updated broadcast failed", exc_info=True)

    async def _after_library_change(self, *, package: str | None = None) -> None:
        try:
            from soniqboom.api import fstree
            fstree.invalidate_scan_root_cache(self.root)
        except Exception:                               # noqa: BLE001
            pass
        try:
            from soniqboom.core import genre_derive
            genre_derive.schedule()
        except Exception:                               # noqa: BLE001
            pass
        try:
            from soniqboom.api import library
            if package is not None:
                # One package opened: only the views of its folder change (the
                # folder tree keeps its folders — no tree rebuild for anyone).
                folder = self.root + ("/" + package.rpartition("/")[0]
                                      if "/" in package else "")
                await library.broadcast_catalogue_folder_changed(self.root, folder)
            else:
                await library.broadcast_catalogue_changed(self.root)
        except Exception:                               # noqa: BLE001
            log.debug("catalogue-changed broadcast failed", exc_info=True)

    # Parking (playlist entries and played rows of tunes that leave) ---------------------
    def _load_parked_rows(self) -> dict:
        try:
            data = json.loads(self.parked_path().read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return {r["id"]: r for r in data if isinstance(r, dict) and r.get("id")} \
            if isinstance(data, list) else {}

    def _save_parked_rows(self, rows: dict) -> None:
        p = self.parked_path()
        try:
            if not rows:
                p.unlink(missing_ok=True)
                return
            tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:12]}.tmp")
            tmp.write_text(json.dumps(list(rows.values())), "utf-8")
            os.replace(tmp, p)
        except OSError:
            log.debug("could not write %s", p, exc_info=True)

    def _drop_parked_rows(self, ids) -> None:
        rows = self._load_parked_rows()
        for tid in ids:
            rows.pop(tid, None)
        self._save_parked_rows(rows)

    def _unpark_returned_ids(self, store) -> None:
        from soniqboom.core.store import PARKED_PLAYLIST_IDS
        parked = store.get_config(PARKED_PLAYLIST_IDS) or []
        if parked:
            rest = [t for t in parked if t not in store._tracks]
            if len(rest) != len(parked):
                store.set_config(PARKED_PLAYLIST_IDS, rest)

    async def _park_user_refs(self, ids) -> None:
        from soniqboom.core.store import PARKED_PLAYLIST_IDS
        store = _store()
        ids = ids if isinstance(ids, (set, frozenset)) else set(ids)
        refs: set = set()
        for pl in store.list_playlists():
            for e in pl.get("track_ids") or ():
                tid = e.get("id") if isinstance(e, dict) else e
                if tid in ids:
                    refs.add(tid)
        prev = set(store.get_config(PARKED_PLAYLIST_IDS) or ())
        if refs - prev:
            store.set_config(PARKED_PLAYLIST_IDS, sorted(refs | prev))
        tracks = store._tracks

        def _played() -> None:
            rows = {tid: dict(tracks[tid]) for tid in ids
                    if tid in tracks and is_merged(tracks[tid])}
            if rows:
                kept = self._load_parked_rows()
                kept.update(rows)
                self._save_parked_rows(kept)
        await asyncio.get_running_loop().run_in_executor(None, _played)

    def _not_opened_since(self, rows: list, started: float) -> list:
        """``rows`` less those a package opening wrote while this apply ran
        (its rows are newer than the index read this apply builds from)."""
        if not self._expanded_at:
            return rows
        return [t for t in rows if self._expanded_at.get(t.get("id"), 0) < started]

    def _build(self, rel, size, extra, **kw) -> dict:
        """``build_track`` + what it set of ``owned_fields`` (``index_vals``)."""
        t = self.build_track(rel, size, extra, **kw)
        vals = {f: t[f] for f in self.owned_fields if t.get(f)}
        if vals:
            t["index_vals"] = vals
        else:
            t.pop("index_vals", None)
        return t

    def _withdrawals(self, old: dict, fresh: dict) -> dict:
        """What an earlier build put on a row that this one no longer does —
        the ``owned_fields`` it set, still as it set them (another pass's
        value stays), and a ``scene_path`` on a row never matched by its
        checksum (only that match sets one now).  A listener's edit stays."""
        upd: dict = {}
        edited = set(old.get("user_edited") or ())
        for f, v in (old.get("index_vals") or {}).items():
            if f not in edited and not fresh.get(f) and old.get(f) == v:
                upd[f] = None
        if (old.get("scene_path") and not fresh.get("scene_path") and not old.get("file_md5")
                and "scene_path" not in edited):
            upd["scene_path"] = None
        return upd

    def _rederive_merged(self, old: dict, fresh: dict) -> dict:
        """A played tune's kept row with the identity fields of the row the
        current index builds: what its play read stays."""
        d = dict(old)
        for f in ("id", "path", "dir_hash", "scan_root_hash", "file_size"):
            if f in fresh:
                d[f] = fresh[f]
        from soniqboom.core.store import share_values
        return share_values(d)

    def _rebuild_merged(self, old: dict, fresh: dict) -> dict:
        """A played tune rebuilt by other build rules: what its play read
        stays, the fields it took from the index (``index_fields``; a row
        read before they were recorded: ``index_fill_fields``) follow the
        new rules — unless the listener edited them."""
        d = self._rederive_merged(old, fresh)
        legacy = "index_fields" not in old
        fields = set(old.get("index_fields") or ()) | (set(self.index_fill_fields) if legacy
                                                       else set())
        if legacy:
            fields.add("scene_path")             # (an older build set it from the path)
        for f in fields - set(old.get("user_edited") or ()):
            v = fresh.get(f)
            if v in (None, "", [], {}):
                d.pop(f, None)
            else:
                d[f] = v
        if legacy:
            d["index_fields"] = sorted(f for f in fields if f in d)
        if fresh.get("index_vals"):
            d["index_vals"] = fresh["index_vals"]
        else:
            d.pop("index_vals", None)
        return d

    async def remove_all(self) -> int:
        from soniqboom.core import scanner
        from soniqboom.core.modland_catalog import _delete_sliced
        store = _store()
        ids = list(store.get_track_ids_for_scan_root(self.root_hash()))
        if not ids:
            return 0
        try:
            await self._park_user_refs(ids)
        except Exception:                               # noqa: BLE001
            log.warning("%s: playlist / played-tune bookkeeping failed", self.label, exc_info=True)
        store.enter_batch_mode()
        try:
            removed = await _delete_sliced(store, ids,
                                           lambda n: self._progress("removing tunes", n, len(ids)))
        finally:
            await scanner._async_exit_batch_mode(store)
        await self._after_library_change()
        return removed

    # Jobs ---------------------------------------------------------------------------------
    def _get_job_lock(self) -> asyncio.Lock:
        if self._job_lock is None:
            self._job_lock = asyncio.Lock()
        return self._job_lock

    def busy(self) -> bool:
        return self._job_lock is not None and self._job_lock.locked()

    def _spawn(self, coro, name: str) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro, name=f"online-catalog.{self.key}.{name}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _run_job(self, name: str, body) -> dict:
        me = asyncio.current_task()
        if me is not None:
            self._job_tasks.add(me)
        try:
            async with self._get_job_lock():
                if name not in ("enable", "disable") and not self.enabled():
                    return self.status()
                self._status.update(state="building" if name != "disable" else "removing",
                                    error=None, started_at=time.time(), last_attempt=time.time(),
                                    phase="", done=0, total=0)
                try:
                    res = await body()
                except asyncio.CancelledError:
                    self._status.update(state="ready" if self.enabled() and self.has_index()
                                        else "off", phase="")
                    raise
                except Exception as exc:                # noqa: BLE001
                    log.warning("%s %s failed: %s", self.label, name, exc)
                    self._status.update(error=f"{exc}", phase="",
                                        shrink_refused=isinstance(exc, IndexShrank),
                                        state=("ready" if self.enabled() and self.tune_count()
                                               else ("error" if self.enabled() else "off")))
                    self._note_failure(exc)
                    return self.status()
                self._status.update(state="ready" if self.enabled() else "off", phase="",
                                    last_result=res, shrink_refused=False)
                return self.status()
        finally:
            if me is not None:
                self._job_tasks.discard(me)

    def start_job(self, fn, *args, **kwargs) -> bool:
        if self.busy():
            return False
        t = self._spawn(fn(*args, **kwargs), getattr(fn, "__name__", "job"))
        self._job_tasks.add(t)
        t.add_done_callback(self._job_tasks.discard)
        return True

    async def _update_and_apply(self, *, force: bool = False, accept_shrink: bool = False) -> dict:
        loop = asyncio.get_running_loop()
        self._progress("downloading the index", 0, 0)
        try:
            res = await loop.run_in_executor(
                None, lambda: self._update_index(force=force, accept_shrink=accept_shrink))
        except asyncio.CancelledError:
            self.signal_stop()          # the worker thread outlives the job: end it too
            raise
        except IndexShrank:
            raise
        except Exception as exc:                        # noqa: BLE001
            if not self.has_index():
                raise
            log.warning("%s: the index could not be updated (%s) — the kept one is used",
                        self.label, exc)
            self._note_failure(exc)             # (the next try waits: ``_next_due``)
            out = await self.apply_index()
            self._status["error"] = f"{exc}"
            return out
        self._clear_failure()               # (the index was read: the waits are over)
        out = await self.apply_index()
        out["index"] = res
        return out

    async def enable(self) -> dict:
        async def body():
            _store().set_config(self.cfg("enabled"), True)
            self._register_root()
            self._start_refresh_loop()
            res = await self._update_and_apply(force=not self.has_index())
            self._ensure_scan_dir()
            return res
        return await self._run_job("enable", body)

    async def disable(self) -> dict:
        self.signal_stop()
        async def body():
            _store().set_config(self.cfg("enabled"), False)
            if self._refresh_task is not None:
                self._refresh_task.cancel()
                self._refresh_task = None
            if not catalogue.is_catalogue_root(self.root) and self.tune_count():
                catalogue.register_root(self.root, key=self.key, label=self.label,
                                        is_music=self._is_music,
                                        subsonic_visible=self.subsonic_visible)
            n = await self.remove_all()
            _store().delete_scan_dir(self.root)
            self._unregister_root()
            self._clear_failure()
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.after_disable)
            except Exception:                           # noqa: BLE001
                log.debug("%s after_disable failed", self.label, exc_info=True)
            return {"removed": n}
        return await self._run_job("disable", body)

    def start_disable(self) -> None:
        _store().set_config(self.cfg("enabled"), False)
        self.signal_stop()
        running = False
        for t in list(self._job_tasks) + list(self._tasks):
            if t.done():
                continue
            if "disable" in t.get_name():
                running = True
            elif ".merge[" not in t.get_name():
                t.cancel()
        if running:
            return
        t = self._spawn(self.disable(), "disable")
        self._job_tasks.add(t)
        t.add_done_callback(self._job_tasks.discard)

    async def refresh(self, *, manual: bool = False) -> dict:
        if not self.enabled():
            return {**self.status(), "error": f"{self.label} is off"}
        accept = bool(manual and (self._status.get("shrink_refused")
                                  or self._failure().get("shrink_refused")))

        async def body():
            self._register_root()
            self._start_refresh_loop()
            res = await self._update_and_apply(force=accept, accept_shrink=accept)
            self._ensure_scan_dir()
            return res
        return await self._run_job("refresh", body)

    async def reapply(self) -> dict:
        """Apply the kept index again (an option changed what it lists)."""
        if not self.enabled():
            return self.status()

        async def body():
            return await self.apply_index()
        return await self._run_job("reapply", body)

    def set_interval(self, name: str) -> dict:
        if name not in INTERVALS:
            raise ValueError(f"interval must be one of {list(INTERVALS)}")
        store = _store()
        if store.get_config(self.cfg("interval"), self.default_interval) != name:
            store.set_config(self.cfg("interval"), name)
            self._rearm_not_before = time.time() + _REARM_GRACE_S
            if self._refresh_wake is not None:
                try:
                    self._refresh_wake.set()
                except RuntimeError:
                    pass
        return self.status()

    def set_subsonic(self, visible: bool) -> dict:
        store = _store()
        store.set_config(self.cfg("subsonic"), bool(visible))
        store._catalog_seq += 1
        store._mutation_seq += 1
        store.browse_changed()      # (and the hidden-catalogue counts on this)
        return self.status()

    # Schedule ------------------------------------------------------------------------------
    def _start_refresh_loop(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = self._spawn(self._refresh_loop(), "refresh-loop")

    # Failed refreshes ------------------------------------------------------------------------
    def _failure(self) -> dict:
        """The failed refreshes in a row (``_note_failure``) — kept in the
        store, so a restart neither forgets the wait nor retries at once."""
        try:
            f = _store().get_config(self.cfg("failure"))
        except Exception:                               # noqa: BLE001
            f = None
        return f if isinstance(f, dict) else {}

    def _note_failure(self, exc: BaseException) -> None:
        prev = self._failure()
        now = time.time()
        refused = is_refusal(exc)
        ra = retry_after_of(exc, now)
        if ra:
            ra = min(ra, float(_RETRY_MAX_S))    # (an absurd one is not taken at its word)
        f = {"at": now, "streak": min(int(prev.get("streak") or 0) + 1, 30),
             "refused": refused, "error": str(exc)[:300],
             "shrink_refused": isinstance(exc, IndexShrank),
             "retry_at": now + ra if ra else 0.0}
        try:
            _store().set_config(self.cfg("failure"), f)
        except Exception:                               # noqa: BLE001
            log.debug("%s: failure record not saved", self.label, exc_info=True)
        log.info("%s: next index refresh no sooner than %s (%s, %d failed in a row)",
                 self.label, time.strftime("%Y-%m-%d %H:%M", time.localtime(self._retry_due(f))),
                 "a refusal" if refused else "a failure", f["streak"])

    def _clear_failure(self) -> None:
        try:
            if _store().get_config(self.cfg("failure")) is not None:
                _store().delete_config(self.cfg("failure"))
        except Exception:                               # noqa: BLE001
            pass

    def _retry_due(self, f: dict) -> float:
        """When the next try after failure record ``f`` is due: an hour after a
        failed connection, a day after a refusal, doubling with each further
        failure in a row (at most the refresh interval, never more than
        ``_RETRY_MAX_S``) — and never before an answer's ``Retry-After``."""
        try:
            at = float(f.get("at") or 0)
            streak = max(1, int(f.get("streak") or 1))
        except (TypeError, ValueError):
            return 0.0
        base = _RETRY_AFTER_REFUSAL_S if f.get("refused") else _RETRY_AFTER_FAILURE_S
        cap = max(base, min(_RETRY_MAX_S, self.interval_seconds()))
        wait = min(cap, base * (2 ** min(streak - 1, 10)))
        try:
            retry_at = min(float(f.get("retry_at") or 0), at + _RETRY_MAX_S)
        except (TypeError, ValueError):
            retry_at = 0.0
        return max(at + wait, retry_at)

    def _next_due(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        info = self.index_info()
        if not info and self.has_index() and self._ro() is None:
            # The index is there but can't be read right now (no descriptor
            # left, the volume gone for a moment): no refresh on that — a
            # look again in a few minutes.
            return now + 300
        try:
            built = float(info.get("built_at") or 0)
        except (TypeError, ValueError):
            built = 0.0
        due = built + self.interval_seconds() if built else now
        f = self._failure()
        last_try = float(f.get("at") or 0) if f else 0.0
        if f and last_try:
            due = max(due if built and built > last_try else 0, self._retry_due(f))
        return max(due, self._rearm_not_before)

    async def _refresh_loop(self) -> None:
        """The schedule — supervised: a failure computing it (an index that
        can't be opened for a moment) waits a minute and carries on, never
        ends it."""
        failures = 0
        while self.enabled():
            try:
                wait = self._next_due() - time.time()
                if wait > 0:
                    ev = self._refresh_wake = asyncio.Event()
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=min(wait, 3600))
                    except asyncio.TimeoutError:
                        pass
                    failures = 0
                    continue
                if self.busy():
                    await asyncio.sleep(60)
                    continue
                log.info("%s: scheduled index refresh (%s)", self.label, self.interval())
                await self.refresh()
                failures = 0
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception:                           # noqa: BLE001
                failures += 1
                log.warning("%s: refresh schedule hit an error (%d in a row) — "
                            "carrying on", self.label, failures, exc_info=True)
                await asyncio.sleep(min(3600, 60 * failures))

    # Start / stop ------------------------------------------------------------------------------
    def register_at_startup(self) -> None:
        """Right after the library loaded (no network, no store write): an
        enabled collection's root is a catalogue root again before anything
        walks or polls remote roots."""
        self._capture_loop()
        if self.enabled():
            self._register_root()
        elif self.tune_count() or self.root in _store()._scan_dirs:
            catalogue.register_root(self.root, key=self.key, label=self.label,
                                    is_music=self._is_music, subsonic_visible=self.subsonic_visible)

    def _drop_leftovers(self) -> None:
        try:
            d = self.dir()
        except Exception:                               # noqa: BLE001
            return
        import shutil
        for f in d.glob("index.sqlite.*.building"):
            f.unlink(missing_ok=True)
        for f in d.glob("download.*"):
            shutil.rmtree(f, ignore_errors=True)

    async def startup(self) -> None:
        store = _store()
        self._capture_loop()
        self._drop_leftovers()
        if self.enabled():
            self._register_root()
            if self.has_index() or self.tune_count():
                self._ensure_scan_dir()
            sd = store._scan_dirs.get(self.root)
            if sd is not None and sd.get("status") == "unavailable":
                store.set_scan_dir_status(self.root, "ok")
            self._status.update(state="ready")
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.after_index)
            except Exception:                           # noqa: BLE001
                log.debug("%s after_index at start failed", self.label, exc_info=True)
            f = self._failure()
            if f and self._retry_due(f) > time.time():
                # The last index run failed and its wait isn't over: the
                # schedule (``_next_due``) tries again when it is — not every
                # start.
                pass
            elif not self.has_index() or self.tune_count() == 0:
                async def _heal():
                    await asyncio.sleep(20)
                    await self._run_job("startup", lambda: self._update_and_apply())
                # (the schedule, due at once without an index, leaves this
                # run to the heal: one index job, not two)
                self._rearm_not_before = max(self._rearm_not_before,
                                             time.time() + 20 + _REARM_GRACE_S)
                self._spawn(_heal(), "startup")
            elif self._rules_moved():
                # Read or built by other rules than this code's: the index read
                # again (other index rules), else the rows built again — now,
                # not at the next scheduled refresh.
                async def _rebuild():
                    await asyncio.sleep(20)
                    info = self.index_info()
                    if str(info.get("rules") or "1") != str(self.index_rules):
                        await self._run_job("startup", lambda: self._update_and_apply())
                    else:
                        await self._run_job("startup", self.apply_index)
                    await self.settle_empty_packages()
                self._spawn(_rebuild(), "startup")
            self._start_refresh_loop()
        elif self.tune_count() or self.root in store._scan_dirs:
            log.info("%s: finishing an interrupted switch-off", self.label)
            catalogue.register_root(self.root, key=self.key, label=self.label,
                                    is_music=self._is_music, subsonic_visible=self.subsonic_visible)
            self._spawn(self.disable(), "finish-disable")

    def _naming(self) -> str:
        """The rules packages are listed by: the member naming rules and the
        formats this host plays (a package found empty by others is listed
        again — ``_stored_marker``)."""
        from soniqboom.core import archive
        if not self.packaged:
            return ""
        return f"{archive.MEMBER_NAMING_VERSION}/{archive.formats_fingerprint()}"

    def _rules_moved(self) -> bool:
        """Was the index read, its rows built or its packages listed by other
        rules than this code's?"""
        info = self.index_info()
        return (str(info.get("rules") or "1") != str(self.index_rules)
                or str(info.get("built_rules") or "1") != str(self.build_rules)
                or str(info.get("built_naming") or "") != self._naming())

    async def shutdown(self) -> None:
        self.signal_stop()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):     # noqa: BLE001
                pass

    # First play: the file's own metadata --------------------------------------------------------
    def merge_after_play(self, track_id: str, local_path) -> None:
        try:
            t = _store().get_track(track_id)
        except Exception:                               # noqa: BLE001
            return
        if (not t or not self.owns(t.get("path")) or is_merged(t)
                or track_id in self._merging or not local_path):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._merging.add(track_id)
        t_ = loop.create_task(self._merge(track_id, Path(str(local_path))),
                              name=f"online-catalog.{self.key}.merge[{track_id}]")
        self._tasks.add(t_)
        t_.add_done_callback(self._tasks.discard)
        self._merge_tasks[track_id] = t_
        t_.add_done_callback(lambda _t, k=track_id: self._merge_tasks.pop(k, None))

    async def await_merge(self, track_id: str, timeout: float) -> bool:
        t_ = self._merge_tasks.get(track_id)
        if t_ is None:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(t_), timeout)
        except (asyncio.TimeoutError, Exception):       # noqa: BLE001
            return t_.done()
        return True

    async def _merge(self, track_id: str, local: Path) -> None:
        try:
            from soniqboom.core.modland_catalog import _extract_copy, _good_title
            store = _store()
            t = store.get_track(track_id)
            if not t or is_merged(t):
                return
            real_name = orig_name = t["path"].rsplit("/", 1)[-1].rsplit("::", 1)[-1]
            rel = self.rel_of(t["path"])
            loop = asyncio.get_running_loop()
            try:
                real_name, fix = await loop.run_in_executor(
                    None, self.merge_view, rel, real_name, local)
                meta = await loop.run_in_executor(None, _extract_copy, local, track_id, real_name)
            except Exception as exc:                    # noqa: BLE001
                log.info("%s: metadata of %s not read (%s)", self.label, real_name, exc)
                return
            if not meta:
                return
            facts = None
            if "::" not in rel and not self.is_package(rel):
                try:
                    facts = await loop.run_in_executor(None, self.loose_facts, rel)
                except Exception:                       # noqa: BLE001
                    facts = None
            cur = store.get_track(track_id)
            if not cur or is_merged(cur) or cur.get("path") != t.get("path"):
                return
            told: dict = {}
            if facts:
                # What a loose upload's companion says (Aminet: its readme) —
                # read with the file, so it is no index field.
                e = await loop.run_in_executor(None, self.entry, rel)
                if e is not None:
                    idx = self.build_track(rel, e[0], {**(e[1] or {}), "pkg": facts},
                                           added_at=cur.get("added_at") or 0,
                                           dir_hash=cur.get("dir_hash"),
                                           root_h=cur.get("scan_root_hash"))
                    cur = dict(cur)
                    for f in ("artist", "comment", "scene_group"):
                        if idx.get(f) and idx[f] != cur.get(f):
                            cur[f] = told[f] = idx[f]
            d = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
            d.pop("embedding", None)
            d.pop("cover_art", None)
            d.update(fix)
            read = set(d)                        # what the file itself said
            for f in ("id", "path", "dir_hash", "scan_root_hash", "added_at", "file_size"):
                if f in cur:
                    d[f] = cur[f]
            d["mtime"] = time.time()
            if not d.get("file_size"):
                try:
                    d["file_size"] = local.stat().st_size        # (an index without sizes)
                except OSError:
                    pass
            names = {real_name.casefold(), Path(real_name).stem.casefold(),
                     orig_name.casefold(), Path(orig_name).stem.casefold()}
            if (not _good_title(d.get("title"), local.stem)
                    or str(d.get("title") or "").strip().casefold() in names):
                d["title"] = cur.get("title")            # (a tag that only repeats the file name)
                read.discard("title")
            for f in ("format", "genre"):
                if not d.get(f) and cur.get(f):
                    d[f] = cur[f]
            if not d.get("artist") and cur.get("artist"):
                d["artist"] = cur["artist"]
            d.setdefault("is_duplicate_primary", True)
            d = self.merged_fields(d, cur, self.rel_of(cur["path"]))
            # What the build set that still stands (``_withdrawals``), and the
            # fields taken from the index rather than the file: other build
            # rules re-derive them (``_rebuild_merged``).
            vals = {f: v for f, v in (cur.get("index_vals") or {}).items() if d.get(f) == v}
            if vals:
                d["index_vals"] = vals
            else:
                d.pop("index_vals", None)
            d["index_fields"] = sorted(
                f for f, v in d.items()
                if f not in read and v not in (None, "", [], {}) and f not in _OWN_FIELDS
                and not (f in told and v == told[f]))
            from soniqboom.core.store import share_values
            store.upsert_tracks_batch([share_values(d)])
            try:
                self.after_merge(track_id, d, cur)
            except Exception:                           # noqa: BLE001
                log.debug("%s: after-merge step failed", self.label, exc_info=True)
            log.info("%s: read %s on its first play (%s, %.1f s)", self.label, real_name,
                     d.get("format"), float(d.get("duration") or 0))
            if d.get("sid_md5"):
                try:
                    from soniqboom.core import hvsc_apply
                    await hvsc_apply.apply_hvsc_to_library(ids=[track_id])
                except Exception:                       # noqa: BLE001
                    log.debug("HVSC apply after a catalogue merge failed", exc_info=True)
            try:
                from soniqboom.core import scanner
                scanner._spawn_scene_autoapply()
            except Exception:                           # noqa: BLE001
                pass
            try:
                from soniqboom.api import library
                b_ = asyncio.get_running_loop().create_task(
                    library.broadcast_track_updated(track_id),
                    name=f"online-catalog.{self.key}.track-updated[{track_id}]")
                self._tasks.add(b_)
                b_.add_done_callback(self._tasks.discard)
            except Exception:                           # noqa: BLE001
                pass
        except Exception:                               # noqa: BLE001
            log.warning("%s merge of %s failed", self.label, track_id, exc_info=True)
        finally:
            self._merging.discard(track_id)


def _iso_date(value) -> str | None:
    """An index date as YYYY-MM-DD (an index built before 2026-10-13 kept
    the server's HTTP date: ``Fri, 26 Jun 2026 13:39:13 GMT``)."""
    if not value:
        return None
    v = str(value)
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return v
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(v).date().isoformat()
    except (TypeError, ValueError):
        return v


_NESTED_MAX = 64 * 1024 * 1024


def _nested_members(names, sizes: dict, read) -> tuple[list, set, dict]:
    """Members of the archives and disk images inside a package
    (``inner.zip::track.mod``, ``disk.adf::mod.title``) — one level deep,
    inner ones up to ``_NESTED_MAX``; ``read(stored name) -> bytes`` reads
    one out of the package (a fetched copy, or by ranges).  Returns
    ``(members, opened, inner_names)``: the stored names of the inner
    archives / disk images that were read (the census tells them from those
    it couldn't open) and, per one of them, its own member names."""
    from soniqboom.core import archive, diskimage
    out: list = []
    opened: set = set()
    inner_names: dict = {}
    for real in names:
        image = diskimage.is_disk_image(real)
        if not image and not archive.is_archive_name(real):
            continue
        if sizes.get(real, 0) > _NESTED_MAX:
            continue
        import tempfile
        tmp = None
        try:
            data = read(real)
            suffix = "." + real.rsplit(".", 1)[-1].lower()
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                fh.write(data)
                tmp = fh.name
            if image:
                found = diskimage._enumerate(tmp)
                out += [[f"{real}::{n}", len(b), f"{real}::{n}"] for n, b in found.items()]
                inner_names[real] = list(found)
            else:
                inner_sizes = _member_sizes(tmp)
                arc = archive._open(tmp)              # (not the shared open-archive cache)
                try:
                    raw = list(arc.namelist())
                    inner = archive._build_map(
                        raw, lambda r, a=arc: archive._head_of_open(a, r, archive.HEAD_BYTES))
                finally:
                    close = getattr(arc, "close", None)
                    if callable(close):
                        close()
                out += [[f"{real}::{n}", int(inner_sizes.get(r, 0)), f"{real}::{r}"]
                        for n, r in inner.items()]
                inner_names[real] = raw[:500]
            opened.add(real)
        except Exception as exc:                        # noqa: BLE001
            log.debug("inner archive / disk image %s not listed (%s)", real, exc)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return out, opened, inner_names


def _local_census(local, opened: set, inner_names: dict) -> dict | None:
    """What the fetched package ``local`` holds (``archive.census``) — read
    when no member of it plays."""
    from soniqboom.core import archive
    arc = None
    try:
        arc = archive._open(local)
        names = list(arc.namelist())
        return archive.census(names, lambda r: archive._head_of_open(arc, r, archive.HEAD_BYTES),
                              opened=opened, inner_names=inner_names)
    except Exception:                                   # noqa: BLE001
        return None
    finally:
        close = getattr(arc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:                           # noqa: BLE001
                pass


def gone_reason(track_id: str) -> str | None:
    """Why ``track_id`` is not in the library when it was a package of an
    online collection that held nothing playable; None for any other id."""
    for cat in REGISTRY.values():
        if not cat.packaged:
            continue
        try:
            why = cat.gone_reason(track_id)
        except Exception:                               # noqa: BLE001
            why = None
        if why:
            return why
    return None


def _member_sizes(local) -> dict:
    """``stored name → size`` of a local archive's members (ZIP and LHA
    readers both list them); {} when the reader can't tell."""
    from soniqboom.core import archive
    arc = None
    try:
        arc = archive._open(local)
        return {i.filename: int(i.file_size) for i in arc.infolist()
                if getattr(i, "filename", None) is not None
                and getattr(i, "file_size", None) is not None}
    except Exception:                                   # noqa: BLE001
        return {}
    finally:
        close = getattr(arc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:                           # noqa: BLE001
                pass


def is_merged(t: dict | None) -> bool:
    return bool(t) and (t.get("mtime") or 0) > 0


# ── Registry ──────────────────────────────────────────────────────────────────

REGISTRY: dict[str, OnlineCatalogue] = {}


def register(cat: OnlineCatalogue) -> OnlineCatalogue:
    REGISTRY[cat.key] = cat
    return cat


def _load() -> None:
    """Import the collections (each registers itself)."""
    from soniqboom.core import online_aminet, online_hvsc, online_sceneorg  # noqa: F401


def all_catalogues() -> list[OnlineCatalogue]:
    _load()
    return list(REGISTRY.values())


def get(key: str) -> OnlineCatalogue | None:
    _load()
    return REGISTRY.get(key)


def owner_of(path) -> OnlineCatalogue | None:
    _load()
    for cat in REGISTRY.values():
        if cat.owns(path):
            return cat
    return None


def merge_after_play(track_id: str, local_path) -> None:
    """The first-play merge of any catalogue tune: Modland's own, or the
    online collection's that owns the track."""
    try:
        t = _store().get_track(track_id)
    except Exception:                                   # noqa: BLE001
        return
    path = (t or {}).get("path")
    cat = owner_of(path)
    if cat is not None:
        cat.merge_after_play(track_id, local_path)
        return
    from soniqboom.core import modland_catalog
    modland_catalog.merge_after_play(track_id, local_path)


def package_member_ids(track_id: str) -> list[str]:
    """The ids of the OTHER tunes of the package ``track_id`` is a member
    of, in the archive's order — what follows it when a queued package
    opens (``[]`` for any other track)."""
    try:
        t = _store().get_track(track_id) or {}
    except Exception:                                   # noqa: BLE001
        return []
    path = str(t.get("path") or "")
    cat = owner_of(path)
    if cat is None or not cat.packaged or "::" not in path:
        return []
    pkg = cat.rel_of(path).split("::", 1)[0]
    op = cat._opened(pkg).get(pkg)
    e = cat.entry(pkg)
    if op is None or e is None:
        return []
    rows = cat._package_rows(pkg, e[0], 0, e[1], op)
    tracks = _store()._tracks
    return [tid for tid in (cat.row_id(r, ex) for r, _s, _n, ex in rows)
            if tid != track_id and tid in tracks]


async def expand(track_id: str, path, *, lane: str = "stream") -> str | None:
    """The member path to play for a package row of an online collection
    (``OnlineCatalogue.expand``); None for any other path."""
    cat = owner_of(path)
    if cat is None or not cat.packaged:
        return None
    return await cat.expand(track_id, str(path), lane=lane)


async def await_merge(track_id: str, timeout: float) -> bool:
    try:
        t = _store().get_track(track_id)
    except Exception:                                   # noqa: BLE001
        t = None
    cat = owner_of((t or {}).get("path"))
    if cat is not None:
        return await cat.await_merge(track_id, timeout)
    from soniqboom.core import modland_catalog
    return await modland_catalog.await_merge(track_id, timeout)


def register_at_startup() -> None:
    for cat in all_catalogues():
        try:
            cat.register_at_startup()
        except Exception:                               # noqa: BLE001
            log.warning("%s: start registration failed", cat.label, exc_info=True)


async def startup() -> None:
    for cat in all_catalogues():
        try:
            await cat.startup()
        except Exception:                               # noqa: BLE001
            log.warning("%s: start failed", cat.label, exc_info=True)


async def shutdown() -> None:
    for cat in all_catalogues():
        await cat.shutdown()


def signal_stop() -> None:
    for cat in REGISTRY.values():
        try:
            cat.signal_stop()
        except Exception:                               # noqa: BLE001
            pass


def all_tasks() -> list:
    out: list = []
    for cat in REGISTRY.values():
        out.extend(cat._tasks)
    return out


def any_busy() -> bool:
    return any(cat.busy() for cat in REGISTRY.values())
