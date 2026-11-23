# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which cover image each music folder holds — or that it holds none.

Why it exists (opportunity "B")
-------------------------------
A track without an embedded cover falls back to its folder's image
(``folder.jpg``, ``cover.jpg``, … — the ``folder_art_names`` setting).  Finding
that image used to mean listing the folder at request time.  For a network
share nothing remembered a folder WITHOUT an image (deliberately: an outage
must not hide real covers), so every list row of such a folder listed the
folder on the server again — on the test rig (2026-11-22) a 36-row window
cost 30 listings + 6 downloads of the same ``folder.jpg``, 2.5 s over a
simulated internet FTP link.  The scan lists every folder anyway; recording
what it saw makes the request need no listing at all: 0 listings + 1
download, 0.35 s.

What is recorded
----------------
``Entry(name, mtime, at, dir_hash)`` per folder key (``key_for_path``):

* ``name``  — the image's file name in that folder, or None: the folder was
  listed completely and holds none of the configured names.  A failed or
  partial listing records NOTHING (unknown stays unknown, so a transient
  outage never turns into "no cover").
* ``mtime`` — the image's modification time where it is known (a local
  folder's ``stat``, a share listing's date) — part of the "has this track's
  cover changed?" stamp (``api/art._known_source_mtime``) and of "was the
  image replaced?" (``take_changed``).
* ``at``    — when it was recorded (wall clock).  A "none" answer expires
  (``_NONE_TTL_*``) and is otherwise re-confirmed by every scan.
* ``dir_hash`` — the store's ``dir_hash`` of the folder's tracks, so a folder
  that gains an image can tell its tracks' list rows (``take_flips``).

Writers: the local scan's walk (it already has every file name), the remote
scan's folder-cover warm-up (it lists every folder), the request path when
it had to list a folder itself, and ``core/folder_covers`` jobs.  The
recorded name follows ``folder_art_names``: a change of that setting, of
"Use folder art", or a cleared art cache forgets everything
(``forget_all``).

Persisted as one small JSON file in the art cache folder (one line per
folder, a few MB for 100k folders), written by a debounced background
thread — never on the request path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path, PurePosixPath
from typing import NamedTuple

log = logging.getLogger(__name__)

_FILE = "folders-v1.json"
# A "none" is re-recorded by every scan whose walk lists the folder (local
# and network); one not seen again for a week (share) / a month (local) is
# asked again.  Local folders are also re-checked live in the background
# (``api/art._verify_later``).
_NONE_TTL_REMOTE = 7 * 24 * 3600.0
_NONE_TTL_LOCAL = 30 * 24 * 3600.0
_SAVE_DELAY_S = 5.0
_MAX_FLIPS = 2000
# Folders of removed roots / deleted folders are never told apart from
# live ones here; past this many entries the longest-unconfirmed go first.
_MAX_ENTRIES = 400_000


class Entry(NamedTuple):
    name: "str | None"
    mtime: "float | None"
    at: float
    dir_hash: "str | None"


_lock = threading.Lock()
_entries: "dict[str, Entry] | None" = None     # loaded on first use
_dirty = False
_saver: "threading.Timer | None" = None
_flips: "set[str]" = set()                     # dir_hashes that gained an image
# (key, dir_hash) of folders whose image was replaced (another name, a newer
# file) or removed — the covers cached from the old one are stale
# (``take_changed``; ``api/art.refresh_changed_folders``).
_changed: "set[tuple[str, str]]" = set()     # (kept in the file too: a stop before the refresh loses none)
_gen = 0                                       # bumped by forget_all (stale writers drop out)


# ── keys ─────────────────────────────────────────────────────────────────────

def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def key_for_local_dir(dirpath: str) -> str:
    """Key of a local folder (an absolute path, as the scan walks it)."""
    return "L" + _h(str(dirpath))


def key_for_remote_dir(scan_root: str, remote_dir: str) -> str:
    """Key of a network-share folder: the share root plus the folder's path
    inside it.  (The share root is part of the key: two shares can both
    have a ``/Music``.)"""
    d = str(PurePosixPath("/" + str(remote_dir or "").lstrip("/")))
    return "R" + _h(f"{scan_root}|{d}")


def remote_dir_of(remote_path: str) -> str:
    """The folder of a path inside a share (an archive member: its
    archive's folder), slash-prefixed — the form ``key_for_remote_dir``
    and the scan's warm-up use."""
    from soniqboom.core.filesource import member_container
    outer = member_container(remote_path)
    return str(PurePosixPath("/" + outer.lstrip("/")).parent)


def member_free(path_str: str) -> str:
    """The real file of a local track path (an archive member: its
    archive)."""
    from soniqboom.core.filesource import member_container
    return member_container(path_str) if "::" in path_str else path_str


def local_dir_of(path_str: str) -> str:
    """The folder of a local track path (an archive member: its archive's
    folder)."""
    return str(Path(member_free(path_str)).parent)


def key_for_path(path_str: str) -> "str | None":
    """Key of the folder whose image is ``path_str``'s folder cover (None
    for a path that has none: a URL without a share part)."""
    if not path_str:
        return None
    from soniqboom.core.filesource import is_remote_path, parse_remote_path
    if is_remote_path(path_str):
        try:
            scan_root, remote_path = parse_remote_path(path_str)
        except Exception:                          # noqa: BLE001
            return None
        if not remote_path:
            return None
        return key_for_remote_dir(scan_root, remote_dir_of(remote_path))
    return key_for_local_dir(local_dir_of(path_str))


def is_remote_key(key: str) -> bool:
    return key.startswith("R")


# ── storage ──────────────────────────────────────────────────────────────────

def _path() -> Path:
    from soniqboom.config import get_art_cache_dir
    return get_art_cache_dir() / _FILE


def _load_locked() -> "dict[str, Entry]":
    global _entries
    if _entries is not None:
        return _entries
    out: "dict[str, Entry]" = {}
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        for k, v in (raw.get("folders") or {}).items():
            if isinstance(v, list) and len(v) == 4:
                out[k] = Entry(v[0], v[1], float(v[2]), v[3])
        for c in raw.get("changed") or []:
            if isinstance(c, list) and len(c) == 2:
                _changed.add((str(c[0]), str(c[1])))
    except FileNotFoundError:
        pass
    except Exception as exc:                       # noqa: BLE001 — a damaged file is rebuilt by the next scan
        log.debug("folder image index unreadable (%s) — starting empty", exc)
    _entries = out
    return out


def _save_soon_locked() -> None:
    global _dirty, _saver
    _dirty = True
    if _saver is not None:
        return
    _saver = threading.Timer(_SAVE_DELAY_S, _save)
    _saver.daemon = True
    _saver.start()


_save_lock = threading.Lock()                  # one writer at a time (timer vs flush)


def _save() -> None:
    global _dirty, _saver
    with _lock:
        _saver = None
        if not _dirty or _entries is None:
            return
        _dirty = False
        if len(_entries) > _MAX_ENTRIES:
            for k, _e in sorted(_entries.items(), key=lambda kv: kv[1].at)[
                    : len(_entries) - _MAX_ENTRIES]:
                del _entries[k]
        snapshot = dict(_entries)                  # (a shallow copy: the lock is held briefly)
        changed = [list(c) for c in _changed]
    data = {"v": 1, "folders": {k: list(e) for k, e in snapshot.items()}, "changed": changed}
    p = _path()
    with _save_lock:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            log.debug("folder image index not saved: %s", exc)


def preload() -> None:
    """Read the stored index now (startup, in a worker thread) — so the first
    cover request does not read the file on the event loop."""
    with _lock:
        _load_locked()


def flush() -> None:
    """Write a pending change now (the server's stop — ``main.shutdown``'s
    "folder-index" step — and tests)."""
    global _saver
    with _lock:
        t, _saver = _saver, None
    if t is not None:
        t.cancel()
    _save()


# ── reads and writes ─────────────────────────────────────────────────────────

def lookup(key: "str | None") -> "Entry | None":
    """What is known of the folder ``key`` — None when nothing (or only an
    expired "none") is."""
    if not key:
        return None
    with _lock:
        e = _load_locked().get(key)
    if e is None:
        return None
    if e.name is None:
        ttl = _NONE_TTL_REMOTE if is_remote_key(key) else _NONE_TTL_LOCAL
        if time.time() - e.at > ttl:
            return None
    return e


def generation() -> int:
    """Bumped by ``forget_all`` — a writer that read the folder before the
    reset passes the value it saw to ``record`` and is ignored."""
    return _gen


def record(key: "str | None", name: "str | None", *, mtime: "float | None" = None,
           dir_hash: "str | None" = None, gen: "int | None" = None) -> None:
    """Record that folder ``key`` holds image ``name`` (None: a COMPLETE
    listing found none).  Only call after a listing that succeeded.  A
    folder that was known to hold none and now holds one is remembered for
    ``take_flips``; one whose image was replaced (another name, or an
    ``mtime`` newer by a second or more) or removed, for ``take_changed``.
    ``mtime`` None (a writer that does not know it) keeps the recorded one
    for the same name — it is no "change"."""
    if not key:
        return
    with _lock:
        if gen is not None and gen != _gen:
            return                                  # settings changed meanwhile
        entries = _load_locked()
        old = entries.get(key)
        if mtime is None and old is not None and old.name == name:
            mtime = old.mtime
        new = Entry(name, mtime, time.time(), dir_hash or (old.dir_hash if old else None))
        if (old is not None and old.name is not None and new.dir_hash
                and (name != old.name
                     or (mtime and old.mtime and not _same_time(float(old.mtime), float(mtime))))):
            if len(_changed) >= _MAX_FLIPS:
                return          # (not taken in: the entry stays as it was — seen again next time)
            _changed.add((key, new.dir_hash))
            _save_soon_locked()
        if old is not None and old[:2] == new[:2] and old.dir_hash == new.dir_hash \
                and new.at - old.at < 3600:
            return                                  # nothing new (spare the writes)
        entries[key] = new
        if name is not None and old is not None and old.name is None and new.dir_hash:
            if len(_flips) < _MAX_FLIPS:
                _flips.add(new.dir_hash)
        _save_soon_locked()


def _same_time(a: float, b: float) -> bool:
    """Do two image dates mean the same file?  The same rule as the scan's
    ``_mtime_matches_tolerant`` — an FTP LIST gives minutes where MLSD gives
    seconds, so when either is minute-aligned they are compared by the
    minute — plus a whole-hour offset of up to 14 h (a LIST in the server's
    local time against a UTC date).  Otherwise a source switching between
    the two would "replace" every folder image at once and drop every cached
    folder cover."""
    if int(a) % 60 == 0 or int(b) % 60 == 0:
        a -= int(a) % 60
        b -= int(b) % 60
    d = abs(a - b)
    if d < 2.0:
        return True
    hours = round(d / 3600.0)
    return 1 <= hours <= 14 and abs(d - hours * 3600.0) < 61.0


def requeue(changes: "list[tuple[str, str]]") -> None:
    """Put changes back whose refresh failed (``api/art.refresh_changed_folders``)."""
    with _lock:
        _load_locked()
        for c in changes:
            if len(_changed) < _MAX_FLIPS:
                _changed.add(tuple(c))
        _save_soon_locked()


def drop(key: "str | None") -> None:
    """Forget folder ``key`` (its recorded image could not be read)."""
    if not key:
        return
    with _lock:
        if _load_locked().pop(key, None) is not None:
            _save_soon_locked()


def take_changed() -> "list[tuple[str, str]]":
    """``(key, dir_hash)`` of each folder whose image was replaced or removed
    since the last call (see ``record``)."""
    with _lock:
        _load_locked()                  # (the stored file also holds changes from before a stop)
        out = list(_changed)
        _changed.clear()
        if out:
            _save_soon_locked()
    return out


def take_change(key: str) -> "str | None":
    """Take the pending change of folder ``key`` alone (its ``dir_hash``, or
    None) — a folder job that found it refreshes it itself, with the old
    image in hand (``core/folder_covers._cover_found``)."""
    with _lock:
        _load_locked()
        hit = next((c for c in _changed if c[0] == key), None)
        if hit is not None:
            _changed.discard(hit)
            _save_soon_locked()
        return hit[1] if hit else None


def take_flips() -> "list[str]":
    """The ``dir_hash`` of each folder that gained an image since the last
    call (their list rows were told "no cover")."""
    with _lock:
        out = list(_flips)
        _flips.clear()
    return out


def forget_all() -> None:
    """Forget every folder (a setting the names depend on changed, or the
    art cache was cleared)."""
    global _entries, _gen
    with _lock:
        _entries = {}
        _flips.clear()
        _changed.clear()
        _gen += 1
        _save_soon_locked()


def _reset_for_tests() -> None:
    global _entries, _dirty, _saver, _gen
    with _lock:
        if _saver is not None:
            _saver.cancel()
        _entries, _dirty, _saver = None, False, None
        _flips.clear()
        _changed.clear()
        _gen += 1


def folder_image_names() -> "list[str]":
    """The configured image names, lower-cased, in preference order — read
    synchronously from the store (the walk runs in a worker thread)."""
    from soniqboom.api.art import _parse_folder_art_names
    from soniqboom.core.store import get_store
    try:
        csv = get_store().get_config("folder_art_names", "")
    except Exception:                              # noqa: BLE001
        csv = ""
    return _parse_folder_art_names(csv if isinstance(csv, str) else "")


def pick(names_in_folder, priority: "list[str]") -> "str | None":
    """The folder image among ``names_in_folder`` (real-case file names) by
    ``priority`` (lower-cased, first wins) — the real-case name, or None."""
    lowered = {}
    for n in names_in_folder:
        lowered.setdefault(n.lower(), n)
    for lname in priority:
        real = lowered.get(lname)
        if real:
            return real
    return None
