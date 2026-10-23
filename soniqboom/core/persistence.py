# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistence layer — snapshot loading and AOF replay.

On startup:
  1. Load library.json (full snapshot)
  2. An older database version: back the files up, convert (see "Database
     version" below)
  3. Replay library.aof (unapplied changes since last merge)
  4. Populate the TrackStore and rebuild indexes
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import orjson

log = logging.getLogger(__name__)


# ── Reading / writing the library's files ────────────────────────────────────
# ``library.json`` (hundreds of MB on a big library) is read at every start and
# by every merge.  orjson parses it about twice as fast as the stdlib, and is
# used whenever it gives exactly what ``json.loads`` gives: it refuses NaN /
# Infinity tokens and lone surrogates (such a file takes the stdlib parser),
# and it reads an integer beyond 64 bits as a float — so a document with an
# integer token of 19+ digits takes the stdlib parser too.  Every snapshot —
# the merger's, the shutdown's and a web import's — is encoded by
# ``encode_state`` (orjson, ~7x faster than ``json.dumps``): UTF-8, NaN /
# ±Infinity as ``null``, and datetime / UUID / dataclass values encoded rather
# than refused; it falls back to ``json.dumps`` for what orjson refuses.  (The
# AOF's records likewise: ``aof.encode_record``.)  Every
# reader here opens the file as bytes and decodes UTF-8; a version before
# 1.11 read it in the locale's encoding, so downgrading on a host whose locale
# isn't UTF-8 can misread non-ASCII names.  (Either encoder is one C call that
# holds the GIL for its whole length — a consistent picture of the live store
# dicts it encodes.)

_DIGIT_MASK = bytes(0x30 if 0x30 <= b <= 0x39 else 0x20 for b in range(256))
_LONG_DIGITS = b"0" * 19


def _long_int_token(raw: bytes) -> bool:
    """Does the JSON text ``raw`` hold an integer token of 19+ digits (one
    orjson might read as a float)?  A digit run that long inside a string (a
    hex id) is told apart by what precedes it — a number follows ``:``, ``,``
    or ``[`` (after the blank ``json.dumps`` writes, and a minus sign); one
    that only looks like it makes the caller take the (exact) stdlib parser."""
    if len(raw) < 19:
        return False
    mask = raw.translate(_DIGIT_MASK)
    i = mask.find(_LONG_DIGITS)
    while i >= 0:
        j = i - 1
        if j >= 0 and raw[j] == 0x2D:                  # "-"
            j -= 1
        while j >= 0 and raw[j] in b" \t\r\n":
            j -= 1
        if j < 0 or raw[j] in b":,[":
            return True
        end = mask.find(b" ", i)
        if end < 0:
            return False
        i = mask.find(_LONG_DIGITS, end)
    return False


def loads_json(raw: bytes) -> Any:
    """``json.loads`` of the UTF-8 text ``raw`` — through orjson when that
    gives the same result (see above).  Raises ``json.JSONDecodeError`` (from
    the stdlib parser, with its position) for text that isn't JSON; bytes that
    aren't UTF-8 are read as U+FFFD instead of failing the whole file."""
    if not _long_int_token(raw):
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            pass
    return json.loads(raw.decode("utf-8", errors="replace"))


def _try_load_json(path: Path) -> dict | None:
    """Attempt to load JSON from *path*.

    Returns the parsed dict on success, or ``None`` on any failure.
    Handles a common corruption pattern (valid JSON followed by trailing
    garbage — e.g. a partial second write) by truncating at the first
    successful parse boundary.
    """
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        log.warning("Failed to load %s: %s", path, exc)
        return None
    if not _long_int_token(raw):
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            pass                    # the stdlib parser below decides (and locates the error)
    text = raw.decode("utf-8", errors="replace")
    del raw
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # The file might be valid JSON followed by trailing garbage
        # (observed on network volumes after interrupted writes).
        # Try parsing only up to the reported error position.
        if exc.pos and exc.pos > 2:
            try:
                state = json.loads(text[:exc.pos])
                log.warning(
                    "Loaded %s with %d trailing garbage bytes trimmed",
                    path.name, path.stat().st_size - exc.pos,
                )
                return state
            except Exception:
                pass
        log.warning("Failed to load %s: %s", path, exc)
        return None


def _rotate_backup(primary: Path, backup: Path, who: str) -> None:
    """Keep the current snapshot as ``library.json.bak`` before it is replaced:
    a hard link to it (``library.json.bak.new``, then ``os.replace`` over the
    .bak — swapped atomically, no copy of a file hundreds of MB large); the
    ``os.replace`` of the snapshot that follows leaves the link holding the old
    bytes.  A file system without hard links (some network shares) gets the
    copy as before; a failure only costs the backup."""
    if not primary.exists():
        return
    link = backup.with_name(backup.name + ".new")
    try:
        link.unlink(missing_ok=True)
        os.link(primary, link)
        os.replace(link, backup)
        return
    except OSError as exc:
        log.debug("%s: no hard link for the backup (%s) — copying", who, exc)
        try:
            link.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        # (a copy to a new file swapped in, never over the old .bak in place:
        # that file may be the conversion backup's hard link — ``backup_before_upgrade``)
        shutil.copy2(primary, link)
        os.replace(link, backup)
    except OSError as exc:
        log.warning("%s: backup copy failed (%s), continuing", who, exc)
        try:
            link.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_dir(path: Path) -> None:
    """Make a rename in directory ``path`` durable (best-effort): without it a
    power loss can keep a later step (the AOF drop) but lose the rename."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def encode_state(state: dict) -> str:
    """``state`` as JSON text — orjson (~7x faster than ``json.dumps``: the
    snapshot is encoded ON the event loop, so for a 270k-track library this
    is ~0.1 s instead of ~0.8 s of a stalled server), falling back to
    ``json.dumps`` for what orjson refuses (a lone surrogate, an int beyond
    64 bits, a non-str key).  Differences: a non-finite float (``nan``,
    ``inf``) is ``null``, as in the API's responses; datetime / UUID /
    dataclass values are encoded instead of refused."""
    try:
        return orjson.dumps(state).decode("utf-8")
    except (orjson.JSONEncodeError, UnicodeDecodeError):
        return json.dumps(state)


def encode_state_bytes(state: dict) -> bytes:
    """``encode_state`` as the UTF-8 bytes written to the file — orjson's
    output as it is (no text copy of a snapshot of several hundred MB)."""
    try:
        return orjson.dumps(state)
    except orjson.JSONEncodeError:
        return json.dumps(state).encode("utf-8")


def write_library_file(data_dir: Path, state: dict, who: str) -> bool:
    """Write ``state`` as the new ``library.json`` — the merger's and the
    shutdown snapshot's shared rotation, robust against network-volume quirks:

      • ``library.json.new`` written + fsync'd so the bytes reach the server
        before anything is renamed (a failed write raises, the temp removed)
      • the temp verified present and non-empty (an SMB/NFS fsync may return
        before the data is committed — a few short retries)
      • the current snapshot kept as ``library.json.bak`` (``_rotate_backup``)
        — the original stays in place until the swap
      • ``os.replace`` for the swap (atomic on POSIX, overwrites the target)

    True when ``library.json`` now holds ``state`` (``encode_state_bytes``);
    ``who`` prefixes the log."""
    return _write_library_text(data_dir, encode_state_bytes(state), who)


def _write_library_text(data_dir: Path, text: "str | bytes", who: str) -> bool:
    """``write_library_file`` of an already encoded state ``text`` (str, or
    its UTF-8 bytes)."""
    primary = data_dir / "library.json"
    tmp = data_dir / "library.json.new"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (open(tmp, "wb") if isinstance(text, bytes)
              else open(tmp, "w", encoding="utf-8")) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    del text
    verified = False
    for _attempt in range(4):
        try:
            if tmp.exists() and tmp.stat().st_size > 0:
                verified = True
                break
        except OSError:
            pass
        time.sleep(0.25)
    if not verified:
        log.error("%s: temp snapshot missing or empty after write — skipping rotation", who)
        tmp.unlink(missing_ok=True)
        return False
    _rotate_backup(primary, data_dir / "library.json.bak", who)
    try:
        os.replace(tmp, primary)
    except OSError as exc:
        log.error("%s: could not replace %s (%s) — skipping this write", who, primary.name, exc)
        return False
    _fsync_dir(data_dir)
    return True


# ── Database version ─────────────────────────────────────────────────────────
# ``library.json`` carries ``"db_version"``: the layout of what it holds.  A
# snapshot without it is V1 (before 1.14.5: one shared play-stats map and one
# shared listening history); V2 keeps both per user (``core/user_data``).  A
# start that finds an older database — the snapshot, or AOF records written
# by an older version (a per-user record without ``uid``) — first copies the
# library's files to ``<data>/backups/db-v<old>-<stamp>/`` (``backup_before_upgrade``),
# and only then converts (``_convert``): the snapshot is brought up step by
# step (``_UPGRADES``), the AOF and the set-aside late-exec records replayed on
# top (records without ``uid`` go to the legacy owner), and the result written
# as a fresh snapshot that also holds those records, which are then dropped —
# so no V1 record remains.  Nothing but the backup is written before that
# snapshot: a crash before it leaves the V1 files as they were, and the next
# start converts again (reusing the backup).  A database newer than this code
# is refused (``DatabaseVersionError``).

DB_VERSION = 2

BACKUPS_DIRNAME = "backups"

# The files a backup before an upgrade copies (those that exist), relative to
# the data directory; ``library.aof.late-exec-*`` files are added.
_UPGRADE_BACKUP_FILES = ("library.json", "library.aof", "radio/favorites.json",
                         "subsonic_state.json")

_BACKUP_README = """\
SoniqBoom database backup
=========================

SoniqBoom {version} found a library database in an older format (V{old}) in
this data folder and copied its files here on {when}, before converting
them to the current format (V{new}).  The conversion only started after this
copy was complete.

Files: {files}

To go back to the state before the conversion:
  1. Stop the SoniqBoom server.
  2. Copy these files back into the data folder ({data_dir}),
     replacing the files there of the same name.  Remove a library.aof that
     this folder does not hold, and library.json.bak / library.json.prev
     (they may hold the converted V{new} database).
  3. Run the previous SoniqBoom version (a version that writes V{old}).

What changed after the conversion is not in this copy: plays and listening
history, and — when you copy those files back too — Subsonic stars, ratings,
play queues and bookmarks, and the favourite stations.  SoniqBoom never
deletes this folder; remove it yourself when you no longer need it.
"""


class DatabaseVersionError(RuntimeError):
    """The library's database can't be opened by this version: it is newer
    than ``DB_VERSION``, or an older one could not be backed up (or written)
    before its conversion.  Raised by ``init_persistence``: the server does
    not start."""


def state_db_version(state: Any) -> int | None:
    """The database version of a loaded snapshot state: None for an empty
    state (no snapshot), 1 for one without ``db_version``."""
    if not isinstance(state, dict) or not state:
        return None
    v = state.get("db_version")
    if v is None:
        return 1
    if type(v) is not int or v < 1:
        raise DatabaseVersionError(
            f"library.json holds an unknown database version ({v!r}) — not loaded")
    return v


def _is_play_entry(v: Any) -> bool:
    """A V1 play-stats value (``{count, last_played}``), as opposed to a V2
    user's map of them."""
    return (isinstance(v, dict) and ("count" in v or "last_played" in v)
            and not any(isinstance(x, dict) for x in v.values()))


def _has_v1_shapes(state: dict) -> bool:
    """Does a (V2-stamped) state hold V1-shaped data — flat play-stats
    entries or a flat history list?  An older version's merger rewrites a V2
    library.json keeping its ``db_version`` and adds its own flat entries (a
    downgrade); ``_needs_upgrade`` converts such a file again."""
    ps, hist = state.get("play_stats"), state.get("history")
    return (isinstance(hist, list) and bool(hist)) or (
        isinstance(ps, dict) and any(_is_play_entry(v) for v in ps.values()))


def _upgrade_v1_to_v2(state: dict, owner: str) -> None:
    """V1 → V2: the shared play stats and history — and every playlist with
    no owner — become ``owner``'s (the legacy owner, or the no-user bucket
    ``""``: then the playlists stay unowned, claimed with that bucket).

    A "V1" snapshot can also be a V2 one an older version wrote back after a
    downgrade (it drops ``db_version``): its play stats then hold the per-user
    maps beside the flat entries that version added, and its history is still
    the per-user dict.  Per-user maps are kept as they are; only the flat
    entries (and a flat history list) go to ``owner``, added to what that user
    has (``user_data.merge_user_data``)."""
    from soniqboom.core.user_data import HISTORY_MAX, merge_user_data
    ps = state.get("play_stats")
    hist = state.get("history")
    ps = ps if isinstance(ps, dict) else {}
    users = {k: v for k, v in ps.items()
             if isinstance(v, dict) and not _is_play_entry(v)
             and all(isinstance(x, dict) for x in v.values())}
    flat = {k: v for k, v in ps.items() if k not in users and _is_play_entry(v)}
    users_h = ({k: v for k, v in hist.items() if isinstance(v, list)}
               if isinstance(hist, dict) else {})
    flat_h = hist[-HISTORY_MAX:] if isinstance(hist, list) else []
    src = "\x00v1"                                     # (no account id)
    if flat:
        users[src] = flat
    if flat_h:
        users_h[src] = flat_h
    merge_user_data(users, users_h, src, owner)
    state["play_stats"] = {k: v for k, v in users.items() if v}
    state["history"] = {k: v for k, v in users_h.items() if v}
    # Playlists with no owner (the shared ones of V1) become the owner's.
    pls = state.get("playlists")
    if owner and isinstance(pls, dict):
        for pid, pl in list(pls.items()):
            if isinstance(pl, dict) and not pl.get("owner_user_id"):
                pls[pid] = {**pl, "owner_user_id": owner}
    _set_owner(state, owner)


# Upgrade steps, run in order: ``{from version: step(state, owner)}``.
_UPGRADES = {1: _upgrade_v1_to_v2}


def _newer_database_error(version: int, data_dir: Path | None) -> DatabaseVersionError:
    from soniqboom import __version__
    where = (f" or restore the backup SoniqBoom made before its last upgrade from "
             f"{data_dir / BACKUPS_DIRNAME} (stop the server, copy those files back into "
             f"the data folder)") if data_dir is not None else ""
    return DatabaseVersionError(
        f"The library database is V{version}, but SoniqBoom {__version__} reads up to "
        f"V{DB_VERSION}: it was written by a newer SoniqBoom.  Run that version again{where}.")


def upgrade_state(state: dict, owner: str, data_dir: Path | None = None) -> int | None:
    """Bring a loaded snapshot ``state`` to ``DB_VERSION`` in place (data of
    an older layout goes to ``owner``) and return the version it had — None
    for an empty state, which is just stamped.  No files are touched (the
    backup is the caller's: ``init_persistence``).  A newer state raises
    ``DatabaseVersionError`` (naming ``data_dir``'s backups folder)."""
    v = state_db_version(state)
    if v is None:
        state["db_version"] = DB_VERSION
        return None
    if v > DB_VERSION:
        raise _newer_database_error(v, data_dir)
    old = v
    while v < DB_VERSION:
        _UPGRADES[v](state, owner)
        v += 1
        state["db_version"] = v
    return old


def import_backup_state(payload: dict, *, accounts: dict[str, str], legacy_owner: str,
                        favorites_out: dict | None = None) -> dict:
    """The library state of a ``.sbz`` backup ``payload`` (``{"version": 2,
    "db_version", "users_map": {uid: username}, "data": snapshot}``), made
    ready to load on this server (``populate_store``).  Nobody is given
    another user's data:

      * a backup of an older database (no ``db_version``: V1) is upgraded as
        a start would (``upgrade_state``) — its shared data goes to
        ``legacy_owner`` (this server's legacy owner, ``users.legacy_owner_id``;
        ``""`` without accounts: the no-user bucket);
      * a V2 backup's per-user data is mapped onto this server's accounts by
        user name (``users_map`` → ``accounts``: lower-cased user name →
        account id; two that land on one account are merged —
        ``user_data.merge_user_data``).  A user with no account of that name
        here keeps its data under ``PENDING_PREFIX`` + its id, the name noted
        in ``PENDING_CONFIG_KEY``, until such an account exists
        (``user_data.claim_pending``); the backup's own pending data the same
        way.  The no-user bucket stays one (``claim_unowned``).
        ``user_data_owner`` follows its user (dropped when that has no account
        here: this server's legacy owner then follows the usual rule).
        Playlist owners and subscribers, and the per-user favourite stations
        (``payload["favorites"]``), follow the same mapping — the stations
        into ``favorites_out`` (``{account id / kept-aside key: stations}``)
        for the caller to save (``radiodir.set_favorites``).

    Raises ``DatabaseVersionError`` for a backup newer than ``DB_VERSION``."""
    from soniqboom.core.user_data import (NO_USER, OWNER_CONFIG_KEY, PENDING_CONFIG_KEY,
                                          PENDING_PREFIX, merge_user_data)
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    outer = payload.get("db_version")
    if type(outer) is int and outer > DB_VERSION:
        raise _newer_database_error(outer, None)
    cfg = data.get("config")
    if not isinstance(cfg, dict):
        cfg = data["config"] = {}
    if state_db_version(data) not in (None, DB_VERSION):
        # (an older database: its owner is this server's, whoever the backup named)
        cfg.pop(OWNER_CONFIG_KEY, None)
    favs_in = payload.get("favorites")
    favs_in = favs_in if isinstance(favs_in, dict) else {}
    old = upgrade_state(data, legacy_owner or NO_USER)
    if old is not None and old < DB_VERSION:
        return data                     # (an older backup holds no per-user stations)
    users_map = payload.get("users_map")
    users_map = users_map if isinstance(users_map, dict) else {}
    src_pending = cfg.get(PENDING_CONFIG_KEY)
    src_pending = src_pending if isinstance(src_pending, dict) else {}
    pending: dict = {}

    def _target(uid: str) -> str:
        if not isinstance(uid, str) or uid == NO_USER:
            return NO_USER
        name = src_pending.get(uid) if uid.startswith(PENDING_PREFIX) else users_map.get(uid)
        hit = accounts.get(name.lower()) if isinstance(name, str) else None
        if hit:
            return hit
        key = uid if uid.startswith(PENDING_PREFIX) else PENDING_PREFIX + uid
        pending[key] = name if isinstance(name, str) else None
        return key

    ps = data.get("play_stats")
    hist = data.get("history")
    ps = data["play_stats"] = ps if isinstance(ps, dict) else {}
    hist = data["history"] = hist if isinstance(hist, dict) else {}
    src_uids = list(dict.fromkeys([*ps, *hist]))
    # Aside first: a source id that is also a target id must not be merged into
    # before its own data moved.
    aside_ps = {u: v for u in src_uids if isinstance(v := ps.pop(u, None), dict)}
    aside_h = {u: v for u in src_uids if isinstance(v := hist.pop(u, None), list)}
    src = "\x00import"                                 # (no account id)
    for uid in src_uids:
        dst = _target(uid)
        tmp_ps = {src: aside_ps[uid]} if uid in aside_ps else {}
        tmp_h = {src: aside_h[uid]} if uid in aside_h else {}
        if dst in ps:
            tmp_ps[dst] = ps.pop(dst)
        if dst in hist:
            tmp_h[dst] = hist.pop(dst)
        merge_user_data(tmp_ps, tmp_h, src, dst)
        ps.update(tmp_ps)
        hist.update(tmp_h)
    pls = data.get("playlists")
    if isinstance(pls, dict):
        for pid, pl in list(pls.items()):
            if not isinstance(pl, dict):
                continue
            owner_src = pl.get("owner_user_id")
            owner_dst = _target(owner_src) if isinstance(owner_src, str) else NO_USER
            subs = []
            for s_uid in pl.get("subscribers") or ():
                if isinstance(s_uid, str) and s_uid:
                    t = _target(s_uid)
                    if t and t != owner_dst and t not in subs:
                        subs.append(t)
            pls[pid] = {**pl, "owner_user_id": owner_dst or None, "subscribers": subs}
    if favorites_out is not None:
        for uid, stations in favs_in.items():
            if isinstance(uid, str) and isinstance(stations, list):
                key = _target(uid)
                have = favorites_out.setdefault(key, [])
                seen = {st.get("sid") for st in have if isinstance(st, dict)}
                have += [st for st in stations
                         if isinstance(st, dict) and st.get("sid") not in seen]
    if pending:
        cfg[PENDING_CONFIG_KEY] = pending
    else:
        cfg.pop(PENDING_CONFIG_KEY, None)
    owner = cfg.get(OWNER_CONFIG_KEY)
    if isinstance(owner, str) and owner:
        name = users_map.get(owner)
        hit = accounts.get(name.lower()) if isinstance(name, str) else None
        if hit:
            cfg[OWNER_CONFIG_KEY] = hit
        else:
            cfg.pop(OWNER_CONFIG_KEY, None)
    return data


def _has_uidless_record(data: bytes) -> bool:
    """Does ``data`` (AOF records) hold a per-user record (``record_play`` /
    ``push_history``) without ``uid`` — one written by a version before V2?
    Stops at the first.  A line carrying ``"uid":`` is passed over unparsed
    (in a title the quotes would be escaped), so a V2 journal costs a byte
    search per line."""
    for raw in data.split(b"\n"):
        if b'"uid":' in raw or (b'"record_play"' not in raw and b'"push_history"' not in raw):
            continue
        try:
            e = loads_json(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if (isinstance(e, dict) and e.get("op") in ("record_play", "push_history")
                and not isinstance(e.get("uid"), str)):
            return True
    return False


def _needs_upgrade(data_dir: Path, state: dict) -> int | None:
    """The database version a start must convert from (backup first), or
    None when the files are this version's (or there are none).  Older: the
    snapshot's ``db_version``, or 1 when the AOF (past the bytes the snapshot
    already holds) or a set-aside ``library.aof.late-exec-*`` file it doesn't
    hold (``_late_records``) holds a per-user record without ``uid``, or a
    V2-stamped snapshot holds V1-shaped data (``_has_v1_shapes``).  Raises
    ``DatabaseVersionError`` for a newer snapshot."""
    v = state_db_version(state)
    if v is not None and v > DB_VERSION:
        raise _newer_database_error(v, data_dir)
    if v is not None and v < DB_VERSION:
        return v
    if v == DB_VERSION and _has_v1_shapes(state):
        log.info("library.json holds data written by a version before database V%d",
                 DB_VERSION)
        return 1
    aof_path = data_dir / "library.aof"
    try:
        data = aof_path.read_bytes()
    except FileNotFoundError:
        data = b""
    if (_has_uidless_record(data[aof_merged_prefix(state, data):])
            or any(_has_uidless_record(body)
                   for _p, body in _late_records(data_dir, data, state.get(LATE_MARK)))):
        log.info("AOF: records written by a version before database V%d", DB_VERSION)
        return 1
    return None


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# The files of a backup a hard link may hold instead of a copy: they are only
# ever replaced (``os.replace`` of a new file — the snapshot writes, the .bak
# rotation's link or copy, ``_rotate_backup``), never written in place, so the
# backup's link keeps the old bytes.  The AOF is appended to: always copied.
_LINKABLE = ("library.json", "library.json.bak")

# Free space a conversion needs beyond the backup's copies: the new snapshot
# (about the size of the current one) and this much headroom.
_UPGRADE_SPACE_MARGIN = 16 * 1024 * 1024


def _backup_manifest(folder: Path) -> dict | None:
    try:
        m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) and isinstance(m.get("files"), dict) else None


def backup_before_upgrade(data_dir: Path, old_version: int, snapshot_from: Path | None = None,
                          ) -> Path:
    """Back up the library's files before a conversion: ``_UPGRADE_BACKUP_FILES``
    that exist, the ``library.aof.late-exec-*`` files, and ``snapshot_from``
    (the ``library.json.bak`` the snapshot was loaded from, when it was).

      * A complete backup of exactly these files (``manifest.json``: each
        file's size and SHA-256) is reused — a start that failed after its
        backup and is tried again (a restart loop) makes no second copy.
      * Else a new ``<data>/backups/db-v<old>-<YYYYmmdd-HHMMSS>/``: the
        snapshot hard-linked where the file system allows (``_LINKABLE``),
        everything else copied; each file checked against the original's
        SHA-256 (a link: the same file) and fsync'd; ``manifest.json`` and
        a ``README.txt`` written.  Built under a temporary name and renamed
        when complete, so a folder with the final name is a whole copy (a
        temporary one a crash left is removed first).  Before copying, the
        free space is checked: the copies plus the new snapshot (plus the
        .bak copy of the current one where hard links fail); a file system
        that reports no size at all (some network shares) is not checked.

    Returns the folder; raises ``DatabaseVersionError`` when the backup could
    not be made (the partial copy removed) — no conversion without a backup."""
    from soniqboom import __version__
    root = data_dir / BACKUPS_DIRNAME
    rels = [r for r in _UPGRADE_BACKUP_FILES if (data_dir / r).is_file()]
    rels += sorted(p.name for p in data_dir.glob("library.aof.late-exec-*") if p.is_file())
    if snapshot_from is not None and snapshot_from.is_file():
        rel = os.path.relpath(snapshot_from, data_dir)
        if rel not in rels:
            rels.append(rel)
    tmp = None
    try:
        files = {rel: {"size": (data_dir / rel).stat().st_size,
                       "sha256": _sha256(data_dir / rel)} for rel in rels}
        if root.is_dir():
            for prior in sorted(root.glob(f"db-v{old_version}-*"), reverse=True):
                m = _backup_manifest(prior)
                if (m is not None and m["files"] == files
                        and all((prior / r).is_file()
                                and (prior / r).stat().st_size == f["size"]
                                for r, f in files.items())):
                    log.info("Database V%d: the backup %s already holds these files — "
                             "reused", old_version, prior)
                    return prior
        stamp = time.strftime("%Y%m%d-%H%M%S")
        final = root / f"db-v{old_version}-{stamp}"
        n = 1
        while final.exists():
            final = root / f"db-v{old_version}-{stamp}-{n}"
            n += 1
        tmp = root / f".{final.name}.incomplete"
        root.mkdir(parents=True, exist_ok=True)
        for stale in root.glob(".db-v*.incomplete"):    # (a copy a crash cut short)
            shutil.rmtree(stale, ignore_errors=True)
        tmp.mkdir()
        linked = set()
        for rel in rels:
            if rel in _LINKABLE:
                try:
                    os.link(data_dir / rel, tmp / rel)
                    linked.add(rel)
                except OSError:
                    pass                                # (copied below)
        snap = data_dir / "library.json"
        # (the new snapshot is about the size of the one loaded — library.json,
        # or the .bak it fell back to)
        snap_size = max((p.stat().st_size for p in (snap, snapshot_from)
                         if p is not None and p.is_file()), default=0)
        # The copies, the new snapshot, and — without hard links — the copy
        # of the current snapshot the write keeps as library.json.bak.
        need = (sum(files[r]["size"] for r in rels if r not in linked) + snap_size
                + (snap_size if "library.json" not in linked else 0) + _UPGRADE_SPACE_MARGIN)
        usage = shutil.disk_usage(root)
        if usage.total == 0:
            log.info("Database V%d: the file system reports no size — the free space "
                     "before the backup is not checked", old_version)
        elif usage.free < need:
            raise OSError(f"not enough free space: {need / 1e6:.0f} MB needed "
                          f"(the backup and the converted library), "
                          f"{usage.free / 1e6:.0f} MB free")
        for rel in rels:
            src, dst = data_dir / rel, tmp / rel
            if rel in linked:
                if not os.path.samefile(src, dst):
                    raise OSError(f"{rel}: the link is not the original")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            try:
                shutil.copystat(src, dst)
            except OSError:
                pass                                    # (times / modes: not needed to restore)
            _fsync_file(dst)
            if _sha256(dst) != files[rel]["sha256"]:
                raise OSError(f"{rel}: the copy differs from the original")
        (tmp / "manifest.json").write_text(json.dumps(
            {"db_version": old_version, "files": files}, indent=1), encoding="utf-8")
        readme = tmp / "README.txt"
        readme.write_text(_BACKUP_README.format(
            version=__version__, old=old_version, new=DB_VERSION,
            when=time.strftime("%Y-%m-%d %H:%M:%S"),
            files=", ".join(rels) or "(none)", data_dir=data_dir), encoding="utf-8")
        _fsync_file(readme)
        _fsync_file(tmp / "manifest.json")
        for d in {(tmp / r).parent for r in rels} | {tmp}:
            _fsync_dir(d)
        os.rename(tmp, final)
        _fsync_dir(root)
        _fsync_dir(data_dir)
    except Exception as exc:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise DatabaseVersionError(
            f"The library database V{old_version} could not be backed up to {root} "
            f"({exc}) — it was not converted to V{DB_VERSION}.  Free some space or fix the "
            f"folder's permissions, then start SoniqBoom again.") from exc
    log.info("Database V%d backed up to %s (%s%s) before converting it to V%d",
             old_version, final, ", ".join(rels) or "no files",
             f"; linked: {', '.join(sorted(linked))}" if linked else "", DB_VERSION)
    return final


def _legacy_owner(state: dict, data_dir: Path) -> str:
    """The account the data of an older database goes to: the state's
    ``user_data_owner`` when set (and an account of that id is on disk, or
    none could be read), else ``users.legacy_owner_id`` of the accounts on
    disk (``users.accounts_on_disk`` — read-only, the user store isn't loaded
    yet at a start): the oldest enabled admin, else the oldest admin, else the
    oldest account; else the no-user bucket ``""`` (no account yet: it moves
    to the first account once there is one — ``user_data.claim_unowned``)."""
    from soniqboom.core.user_data import NO_USER, OWNER_CONFIG_KEY
    cfg = state.get("config")
    owner = cfg.get(OWNER_CONFIG_KEY) if isinstance(cfg, dict) else None
    try:
        from soniqboom.core.users import accounts_on_disk, legacy_owner_id
        accounts = accounts_on_disk(data_dir)
        if isinstance(owner, str) and owner and (
                not accounts or any(u.get("id") == owner for u in accounts)):
            return owner
        return legacy_owner_id(accounts) or NO_USER
    except Exception:                                   # noqa: BLE001
        log.warning("The accounts could not be read for the database upgrade — "
                    "the listening data stays unassigned until an account exists",
                    exc_info=True)
        return NO_USER


# ── The AOF bytes a snapshot holds ───────────────────────────────────────────
# A merge (and a shutdown snapshot) folds the AOF's first N bytes into
# ``library.json``, replaces the file, and only THEN drops those bytes from the
# AOF.  A process that dies between the two (a SIGKILL at a stop, a crash, a
# power cut) leaves an AOF that still starts with bytes the snapshot already
# holds — replayed at the next start (or by the next merge), every play and
# history entry in them counted twice.  So the snapshot records which bytes it
# holds (``AOF_MARK``: their size + a digest), and a reader skips an AOF prefix
# with exactly that size and digest.  Once the bytes are dropped the AOF starts
# with different ones (every record carries its timestamp), so the mark
# matches nothing any more.

AOF_MARK = "aof_merged"


def aof_mark(data: bytes) -> dict:
    """The ``AOF_MARK`` value for a snapshot that holds the AOF bytes ``data``."""
    return {"size": len(data), "digest": hashlib.blake2b(data, digest_size=16).hexdigest()}


def aof_merged_prefix(state: Any, data: bytes) -> int:
    """How many leading bytes of the AOF contents ``data`` the snapshot
    ``state`` already holds (its ``AOF_MARK``) — 0 when they don't match."""
    mark = state.get(AOF_MARK) if isinstance(state, dict) else None
    if not isinstance(mark, dict):
        return 0
    size, digest = mark.get("size"), mark.get("digest")
    if type(size) is not int or not 0 < size <= len(data) or not isinstance(digest, str):
        return 0
    got = hashlib.blake2b(memoryview(data)[:size], digest_size=16).hexdigest()
    return size if got == digest else 0


def read_aof(aof_path: Path) -> bytes:
    """The AOF's contents, read under its flock — the writer
    (``AOFWriter._write_sync``) appends whole records under it."""
    with open(aof_path, "rb+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            return f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def drop_aof_prefix(aof_path: Path, size: int) -> None:
    """Remove the first ``size`` bytes of the AOF (now in ``library.json``),
    under the AOF's flock.  Anything the writer appended after them is kept
    for a later merge: written to ``library.aof.new`` (fsync'd), which then
    replaces the AOF atomically — shifting it to the front in place (the old
    way) left, if the process died between the write and the truncate, the
    rest + leftover merged records + the rest again, all replayed.  The
    writer follows the new file (``AOFWriter._follow_replaced``).  Nothing
    appended: a truncate (one step).  fsync'd so a power loss can't bring
    the dropped bytes back."""
    with open(aof_path, "rb+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            current_size = os.fstat(f.fileno()).st_size
            if current_size > size:
                f.seek(size)
                tail = f.read()
                new = aof_path.with_name(aof_path.name + ".new")
                try:
                    with open(new, "wb") as nf:
                        nf.write(tail)
                        nf.flush()
                        os.fsync(nf.fileno())
                    os.replace(new, aof_path)
                except BaseException:
                    new.unlink(missing_ok=True)
                    raise
                _fsync_dir(aof_path.parent)
            else:
                f.truncate(0)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_snapshot(data_dir: Path) -> dict:
    """Load the most recent snapshot from disk.

    Falls back to .bak if the primary is missing, corrupt, **or empty**
    while the backup has actual data (safety net against a bad merge or
    shutdown writing an empty state over a populated snapshot).

    Returns an empty state dict if neither file exists.
    """
    global _last_loaded_from
    _last_loaded_from = None      # set to the file a POPULATED state came from

    primary = data_dir / "library.json"
    backup = data_dir / "library.json.bak"

    primary_state = _try_load_json(primary)
    backup_state = None  # loaded lazily

    if primary_state is not None:
        primary_tracks = len(primary_state.get("tracks", {}))
        if primary_tracks > 0:
            log.info("Loaded snapshot from %s (%d tracks)", primary.name, primary_tracks)
            _last_loaded_from = primary
            return primary_state

        # Primary loaded but has 0 tracks — check backup before accepting.
        backup_state = _try_load_json(backup)
        if backup_state is not None and len(backup_state.get("tracks", {})) > 0:
            backup_tracks = len(backup_state.get("tracks", {}))
            log.warning(
                "Primary snapshot is empty but backup has %d tracks — using %s",
                backup_tracks, backup.name,
            )
            _last_loaded_from = backup
            return backup_state

        # Both empty or no backup — use primary as-is
        log.info("Loaded snapshot from %s (%d tracks)", primary.name, 0)
        return primary_state

    # Primary failed — try backup
    backup_state = backup_state or _try_load_json(backup)
    if backup_state is not None:
        log.warning("Primary snapshot missing/corrupt — falling back to %s (%d tracks)",
                     backup.name, len(backup_state.get("tracks", {})))
        if len(backup_state.get("tracks", {})) > 0:
            _last_loaded_from = backup
        return backup_state

    log.info("No snapshot found — starting with empty state")
    return {}


# Path a POPULATED snapshot loaded from this boot (primary or the .bak
# fallback), or None if the state was empty — consumed by ``make_prev_backup``.
_last_loaded_from: "Path | None" = None


def make_prev_backup(data_dir: Path) -> None:
    """Refresh ``library.json.prev`` from the snapshot that just PROVEN-loaded.

    ``library.json.bak`` is rotated on every snapshot write, so a bad write can
    clobber it within two cycles.  ``.prev`` is refreshed ONLY here — after a
    snapshot has demonstrably loaded and populated the store with real tracks —
    giving a stable "last known good" to restore from by hand if both the
    primary and the .bak go bad.

    Cheap + best-effort: copies the file ``load_snapshot`` just read (no
    re-parse), skips when ``.prev`` is already a copy of that exact file
    (size + mtime match — ``copy2`` preserves mtime), writes atomically via a
    temp + ``os.replace``, and swallows any error so it can never block boot.
    """
    src = _last_loaded_from
    if src is None:
        return                       # empty / no snapshot this boot — nothing good to preserve
    import os
    import shutil
    try:
        if not src.exists() or src.stat().st_size == 0:
            return
        prev = data_dir / "library.json.prev"
        s = src.stat()
        if prev.exists():
            p = prev.stat()
            if p.st_size == s.st_size and int(p.st_mtime) == int(s.st_mtime):
                return               # already backed up this exact snapshot — skip the re-copy
        tmp = data_dir / "library.json.prev.tmp"
        shutil.copy2(src, tmp)       # preserves mtime so the skip-check works next boot
        os.replace(tmp, prev)        # atomic — a crash mid-copy can't leave a torn .prev
        log.info("Known-good backup refreshed: %s → library.json.prev (%.0f MB)",
                 src.name, s.st_size / 1e6)
    except Exception:
        log.warning("Known-good .prev backup failed (non-fatal)", exc_info=True)


import copy

# Count of AOF entries quarantined during the last replay.  Exposed via
# ``aof_quarantine_count`` so an admin/health endpoint (or test) can detect
# a partially-corrupted journal without grepping logs.
_aof_quarantine_count: int = 0


def aof_quarantine_count() -> int:
    """Return the number of AOF entries quarantined on the last replay.

    Reset to zero at the start of each ``replay_aof`` call.  When the
    journal is healthy this stays at zero; non-zero values indicate that
    ``library.aof.quarantine`` has fresh entries an operator should
    inspect (most often after a crash or unclean shutdown).
    """
    return _aof_quarantine_count


# Sentinel: "this key did not exist before the op" in an undo record.
_MISSING = object()


def _entry_undo(state: dict, entry: dict):
    """Capture the MINIMAL pre-state needed to roll one AOF entry back.

    Returns a list of undo records covering only the sub-dict keys (or the
    per-user map keys) the op can actually touch — O(touched), not O(whole store).  This
    deliberately mirrors the op set in
    :func:`soniqboom.core.merger._apply_entry`; **keep the two in sync**.
    Returns ``None`` for an unrecognised op so the caller falls back to a
    full (slow) snapshot — correct, just slow, and only for ops we don't know.

    WHY THIS EXISTS: the previous implementation shallow-copied EVERY
    top-level slot — including the 170K-entry ``tracks`` dict — on EVERY
    replayed entry, making AOF replay O(entries × tracks).  A clean shutdown
    annotates every track (the duplicate-detection pass → ~one
    ``update_track_fields`` per track), so a 172K-track library produced a
    172K-entry AOF and replay became 172K × 172K ≈ 3e10 dict copies — startup
    spun at 100% CPU for many minutes and never became ready.
    """
    from soniqboom.core.merger import _entry_uid

    op = entry.get("op")

    def kv(slot: str, keys):
        # Undo specific KEYS of a dict slot.  copy.copy the pre-value because
        # some ops (update_track_fields, push_history) mutate it in place — a
        # bare reference would already reflect the mutation by rollback time.
        d = state.get(slot)
        d = d if isinstance(d, dict) else {}
        return ("kv", slot, {
            k: (copy.copy(d[k]) if k in d else _MISSING)
            for k in keys if k is not None
        })

    def nested(slot: str, outer_keys, inner_keys):
        # Undo specific INNER keys of the per-user maps (``play_stats``:
        # ``{user: {track: stats}}``) — the stats dicts copied, as ``kv`` does
        # (``record_play`` edits them in place) — and whether each user's map
        # existed at all.
        d = state.get(slot)
        d = d if isinstance(d, dict) else {}
        saved = {}
        for ok in outer_keys:
            if ok is None:
                continue
            inner = d.get(ok, _MISSING)
            if not isinstance(inner, dict):
                saved[ok] = (copy.copy(inner) if inner is not _MISSING else _MISSING, None)
                continue
            saved[ok] = (True, {k: (copy.copy(inner[k]) if k in inner else _MISSING)
                                for k in inner_keys if k is not None})
        return ("nested", slot, saved)

    if op in ("upsert_track", "update_track_fields"):
        return [kv("tracks", [entry.get("id")])]
    if op == "batch_upsert_tracks":
        return [kv("tracks", [t.get("id") for t in entry.get("data", [])
                              if isinstance(t, dict)])]
    if op == "update_track_fields_batch":
        return [kv("tracks", [r.get("id") for r in entry.get("data", [])
                              if isinstance(r, dict)])]
    if op == "delete_tracks":
        ids = list(entry.get("ids", []))
        return [kv("tracks", ids), kv("waveforms", ids)]   # touches BOTH
    if op == "set_rating":
        return [kv("ratings", [entry.get("id")])]
    if op == "record_play":
        return [nested("play_stats", [_entry_uid(state, entry)], [entry.get("id")])]
    if op in ("upsert_playlist", "delete_playlist"):
        return [kv("playlists", [entry.get("id")])]
    if op in ("upsert_scan_dir", "delete_scan_dir"):
        return [kv("scan_dirs", [entry.get("path")])]
    if op in ("set_config", "delete_config"):
        return [kv("config", [entry.get("key")])]
    if op == "push_history":
        return [kv("history", [_entry_uid(state, entry)])]
    if op == "remap_track_ids":
        m = entry.get("mapping") or {}
        keys = [*m.keys(), *m.values()] if isinstance(m, dict) else []
        pls = state.get("playlists")
        ps = state.get("play_stats")
        hist = state.get("history")
        return [kv("ratings", keys),
                nested("play_stats", list(ps) if isinstance(ps, dict) else [], keys),
                kv("waveforms", keys),
                kv("history", list(hist) if isinstance(hist, dict) else []),
                kv("playlists", list(pls.keys()) if isinstance(pls, dict) else [])]
    if op == "move_user_data":
        uids = [entry.get("src"), entry.get("dst")]
        return [kv("play_stats", uids), kv("history", uids)]
    if op == "delete_user_data":
        return [kv("play_stats", [entry.get("uid")]), kv("history", [entry.get("uid")])]
    return None


def _entry_restore(state: dict, undo) -> None:
    """Undo a partially-applied entry from a :func:`_entry_undo` record."""
    for rec in undo:
        kind = rec[0]
        if kind == "kv":
            _, slot, saved = rec
            d = state.setdefault(slot, {})
            for k, v in saved.items():
                if v is _MISSING:
                    d.pop(k, None)
                else:
                    d[k] = v
        else:  # "nested"
            _, slot, saved = rec
            d = state.setdefault(slot, {})
            for ok, (existed, inner_saved) in saved.items():
                if inner_saved is None:
                    # (no map for that user before the op, or not a dict)
                    if existed is _MISSING:
                        d.pop(ok, None)
                    else:
                        d[ok] = existed
                    continue
                inner = d.get(ok)
                if not isinstance(inner, dict):
                    inner = d[ok] = {}
                for k, v in inner_saved.items():
                    if v is _MISSING:
                        inner.pop(k, None)
                    else:
                        inner[k] = v


def _apply_entry_transactional(state: dict, entry: dict) -> bool:
    """Apply ``entry`` to ``state`` atomically.

    ``_apply_entry`` mutates ``state`` in place, so a half-applied batch (e.g.
    an AttributeError mid-loop) would leave the store torn.  We capture the
    minimal pre-state for the keys this op touches (:func:`_entry_undo`) and
    restore them on failure — O(touched), so AOF replay is O(entries), not
    O(entries × tracks).

    Returns ``True`` on success, ``False`` if the entry was rolled back.
    """
    from soniqboom.core.merger import _apply_entry

    undo = _entry_undo(state, entry)
    if undo is None:
        # Unrecognised op — fall back to a full shallow-snapshot.  Correct for
        # ops _entry_undo doesn't know about; rare, so the O(n) cost is fine.
        snapshot = {k: copy.copy(v) for k, v in state.items()}
        try:
            _apply_entry(state, entry)
            return True
        except Exception:
            for k, v in snapshot.items():
                state[k] = v
            for k in list(state.keys()):
                if k not in snapshot:
                    state.pop(k, None)
            return False

    try:
        _apply_entry(state, entry)
        return True
    except Exception:
        _entry_restore(state, undo)
        return False


def replay_aof(state: dict, data_dir: Path, data: bytes | None = None, *,
               extra: "list[bytes]" = (), quarantine: "bool | list" = True) -> int:
    """Replay AOF entries on top of the loaded snapshot.

    Returns the number of entries applied.  Corrupt or unparseable entries
    (and any that fail the transactional apply) are written to
    ``library.aof.quarantine`` with the current timestamp so an operator
    can inspect them rather than the events being silently dropped
    (``quarantine`` False: only counted and logged — a reader that must not
    write the data folder, ``read_library_state``; a list: those lines are
    collected in it, for the caller to write later — ``_convert``, after its
    snapshot).  Counters are surfaced
    via ``aof_quarantine_count``.  An AOF prefix the snapshot already holds
    (``AOF_MARK`` — a merge or shutdown snapshot that ended before dropping
    it) is skipped, not applied a second time.

    ``data``: the AOF's contents when the caller read them already (the
    conversion marks exactly the bytes replayed), else the file is read.
    ``extra``: more records replayed after them, in order (the set-aside
    ``library.aof.late-exec-*`` records a conversion replays without adding
    them to the AOF first).
    """
    global _aof_quarantine_count
    _aof_quarantine_count = 0

    aof_path = data_dir / "library.aof"
    if data is None:
        try:
            with open(aof_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            data = b""
    if not data and not any(extra):
        return 0
    if not state:
        # (no snapshot: the records build one of this database version — the
        # caller converts an older snapshot before, ``init_persistence``)
        state["db_version"] = DB_VERSION

    quarantine_path = data_dir / "library.aof.quarantine"
    quarantine_fp = None

    def _quarantine(raw: str, reason: str) -> None:
        nonlocal quarantine_fp
        global _aof_quarantine_count
        _aof_quarantine_count += 1
        line = json.dumps({"quarantined_at": time.time(), "reason": reason, "raw": raw}) + "\n"
        if isinstance(quarantine, list):
            quarantine.append(line)
            return
        if not quarantine:
            return
        try:
            if quarantine_fp is None:
                quarantine_fp = open(quarantine_path, "a")
            quarantine_fp.write(line)
        except OSError:
            log.exception("Could not write to AOF quarantine at %s", quarantine_path)

    applied = 0
    skip = aof_merged_prefix(state, data)
    if skip:
        log.info("AOF: its first %d bytes are already in the snapshot (a merge that "
                 "ended before dropping them) — not replayed", skip)
    for blob in (data[skip:], *extra):
        for raw_line in blob.split(b"\n"):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = loads_json(raw_line)
            except json.JSONDecodeError as exc:
                log.warning("Skipping corrupt AOF entry: %s", exc)
                _quarantine(raw_line.decode("utf-8", errors="replace"), f"json: {exc}")
                continue
            try:
                ok = _apply_entry_transactional(state, entry)
            except Exception as exc:
                # Defensive — _apply_entry_transactional already catches
                # most failures; this covers anything that escapes the
                # snapshot/restore (e.g. an OOM mid-copy).
                log.warning("AOF entry raised %s — quarantining", exc)
                _quarantine(raw_line.decode("utf-8", errors="replace"), f"apply: {exc}")
                continue
            if ok:
                applied += 1
            else:
                _quarantine(raw_line.decode("utf-8", errors="replace"), "transactional rollback")

    if quarantine_fp is not None:
        try:
            quarantine_fp.close()
        except OSError:
            pass

    if applied:
        log.info("Replayed %d AOF entries", applied)
    if _aof_quarantine_count:
        log.warning(
            "Quarantined %d AOF entries%s",
            _aof_quarantine_count,
            f" to {quarantine_path}" if quarantine is True else "",
        )
    return applied


def forget_derived_caches(store, *, drop_browse_disk: bool = False) -> None:
    """A whole library replaced the one in memory (a start's load — in the
    bundled app also a later run in the same process — or a backup import):
    every cache built from the previous one starts over.

      * the store's write counters move (``_mutation_seq``, ``_catalog_seq``,
        ``_duration_seq``; ``bulk_load`` keeps them): what is keyed on them —
        the store's aggregate memos, the Subsonic catalogue, the duplicate
        memos — is rebuilt (as ``data.rebuild_indexes``' heal does);
      * the web lists' aggregate cache (``invalidate_agg_cache``: the library
        generation moves, so a connected page re-reads);
      * the folder-browse caches in memory (validated by per-root track
        counts, which a replaced library can match), the catalogue roots' path
        order, every root's listing generation and the Subsonic catalogue;
        with ``drop_browse_disk`` also the browse caches' copy on disk
        (``browse_cache.pickle`` — an import: a start restores it, and its
        check is the same per-root count)."""
    store._mutation_seq += 1
    store._catalog_seq += 1
    store._duration_seq += 1
    try:
        from soniqboom.api.library import invalidate_agg_cache
        invalidate_agg_cache()
    except Exception:                                   # noqa: BLE001
        log.warning("The library lists' cache could not be reset", exc_info=True)
    try:
        from soniqboom.api import fstree
        fstree.invalidate_browse_cache(drop_disk=drop_browse_disk)
        fstree._CAT_SORTED.clear()
        for h in list(fstree._ROOT_GEN):
            fstree._bump_root_gen(h)
    except Exception:                                   # noqa: BLE001
        log.warning("The folder-browse cache could not be reset", exc_info=True)
    try:
        # (rebuilt at the next request, not after the catalogue's debounce)
        from soniqboom.api import subsonic
        c = subsonic._ALBUM_LIST_CACHE
        c["cat"] = None                                 # (a rebuild under way won't
        c["gen"] = int(c.get("gen") or 0) + 1           # install: ``gen`` moved)
    except Exception:                                   # noqa: BLE001
        log.debug("The Subsonic catalogue cache could not be reset", exc_info=True)


def populate_store(state: dict) -> None:
    """Populate the TrackStore singleton from a loaded snapshot state, and
    start every cache built from the previous library over
    (``forget_derived_caches``).  Every caller: the start's load, the web and
    CLI imports (which also drop the browse cache's copy on disk)."""
    from soniqboom.core.store import get_store
    from soniqboom.core.startup_status import set_phase as _ss_phase

    store = get_store()
    t0 = time.monotonic()
    if state_db_version(state) != DB_VERSION:
        # (a state no start converted: one built in memory — the start and
        # the imports convert theirs before; its data goes to its owner)
        from soniqboom.core.user_data import NO_USER, OWNER_CONFIG_KEY
        cfg = state.get("config")
        owner = cfg.get(OWNER_CONFIG_KEY) if isinstance(cfg, dict) else None
        upgrade_state(state, owner if isinstance(owner, str) else NO_USER)

    store.bulk_load(
        tracks=state.get("tracks", {}),
        waveforms=state.get("waveforms", {}),
        ratings=state.get("ratings", {}),
        play_stats=state.get("play_stats", {}),
        playlists=state.get("playlists", {}),
        history=state.get("history", {}),
        scan_dirs=state.get("scan_dirs", {}),
        hash_lookups=state.get("hash_lookups", {}),
        config=state.get("config", {}),
    )

    # Index rebuild is the biggest chunk of startup wall-clock (3–15 s for
    # 268K tracks even with batch_mode).  Surface it as its own phase so
    # the menubar / CLI watcher sees "Building search indexes" instead of
    # still showing "Loading library snapshot" while seconds tick by.
    track_count = len(state.get("tracks", {}))
    _ss_phase("building_indexes", "Building search indexes",
              f"{track_count:,} tracks")
    store.rebuild_indexes()
    forget_derived_caches(store)
    elapsed = (time.monotonic() - t0) * 1000
    log.info(
        "Store loaded: %d tracks, %d waveforms, %d ratings — indexes built in %.0fms",
        store.track_count(),
        len(state.get("waveforms", {})),
        len(state.get("ratings", {})),
        elapsed,
    )


# Held while the library's files (library.json + the AOF) are rewritten (the
# merger's ``_do_merge``, ``write_snapshot_sync``) or loaded at a start
# (``init_persistence``): a merge of a stopped run that is still ending (the
# bundled app restarts the server in one process and doesn't wait for every
# job of the stopped one) finishes before the next run reads the files.
library_files_lock = threading.RLock()

# ``_library_flock``: the same exclusion between PROCESSES — the server, the
# merger process (``merger.merger_loop``), the bundled app's merge child, the
# command-line import / export.  An flock on ``library.lock`` in the data
# directory, released by the kernel when its holder dies.  Re-entrant within a
# process (callers hold ``library_files_lock`` first, so one thread at a time
# gets here; a merge child is single-threaded).  On a file system without
# flock (some network shares) it is skipped.
_flocks_held: dict[str, list] = {}


@contextlib.contextmanager
def _library_flock(data_dir: Path, create: bool = True):
    key = os.path.abspath(data_dir / "library.lock")
    held = _flocks_held.get(key)
    if held is not None:
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
        return
    fd: int | None = None
    try:
        if create:
            data_dir.mkdir(parents=True, exist_ok=True)
        # (``create`` False: a reader that writes nothing — an existing lock
        # file is taken, a missing one means no lock)
        fd = os.open(key, os.O_RDWR | (os.O_CREAT if create else 0), 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Waiting for another SoniqBoom process (a merge) to finish "
                     "writing the library files")
            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        log.debug("library.lock unavailable (%s) — no cross-process lock", exc)
        if fd is not None:
            os.close(fd)
        fd = None
    _flocks_held[key] = [fd, 1]
    try:
        yield
    finally:
        del _flocks_held[key]
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


@contextlib.contextmanager
def library_files_locked(data_dir: Path):
    """``library_files_lock`` + the cross-process ``_library_flock``."""
    with library_files_lock, _library_flock(data_dir):
        yield


# ``claim_library``: this server run is THE server of the data directory — an
# flock on ``library.instance.lock`` held from before the start's load until
# its stop has written the library (``release_library``).  A new instance (the
# app's Restart opens a new one while the old is still stopping; uvicorn frees
# the port before the old one's shutdown runs) waits for it before loading, so
# the old one's shutdown snapshot never drops AOF records the new one wrote,
# and the new one never loads before the old one's last writes.  Re-entrant
# per process (the bundled app's runs never overlap — ``main._claim_server``).
_INSTANCE_WAIT_S = 60.0
_instance_fd: int | None = None
_instance_dir: str | None = None
# Why the last ``claim_library`` returned False: "held" (another process holds
# the data directory) or "no-lock" (no lock file / no flock on this file system).
claim_failure: str | None = None


def claim_library(data_dir: Path, wait: float = _INSTANCE_WAIT_S, quiet: bool = False) -> bool:
    """Hold the data directory for this process (see above).  True when held
    (already, or now); False when another process still holds it after
    ``wait`` seconds or the file system has no flock — then this run's
    shutdown snapshot leaves the AOF alone (``owns_library``).  ``quiet``: no
    server-start log lines (the command-line import reports it itself)."""
    global _instance_fd, _instance_dir, claim_failure
    claim_failure = None
    if _instance_fd is not None:
        if _instance_dir == os.path.abspath(data_dir):
            return True
        release_library()                           # another data directory (tests)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(data_dir / "library.instance.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        if not quiet:
            log.warning("library.instance.lock unavailable (%s) — the shutdown snapshot "
                        "will leave the AOF as it is", exc)
        claim_failure = "no-lock"
        return False
    deadline = time.monotonic() + wait
    waiting = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if not waiting:
                waiting = True
                if not quiet:
                    log.info("Waiting for the previous SoniqBoom instance to finish writing "
                             "the library before loading it")
            if time.monotonic() >= deadline:
                if not quiet:
                    log.warning("Another SoniqBoom process still holds this data directory "
                                "after %.0f s — starting anyway; this run's shutdown snapshot "
                                "will leave the AOF as it is", wait)
                os.close(fd)
                claim_failure = "held"
                return False
            time.sleep(0.1)
        except OSError as exc:
            if not quiet:
                log.warning("No file locks on the data directory (%s) — the shutdown snapshot "
                            "will leave the AOF as it is", exc)
            os.close(fd)
            claim_failure = "no-lock"
            return False
    _instance_fd, _instance_dir = fd, os.path.abspath(data_dir)
    return True


def owns_library() -> bool:
    """This process holds the data directory (``claim_library``)."""
    return _instance_fd is not None


def release_library() -> None:
    """End this process's hold on the data directory (``claim_library``)."""
    global _instance_fd, _instance_dir
    fd, _instance_fd, _instance_dir = _instance_fd, None, None
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def write_snapshot_sync(data_dir: Path, consume_aof: bool = False, aof_writer=None,
                        loop=None, who: str = "Shutdown") -> bool:
    """``_write_snapshot_sync`` under ``library_files_locked``; True when
    ``library.json`` was written."""
    with library_files_locked(data_dir):
        return _write_snapshot_sync(data_dir, consume_aof, aof_writer, loop, who)


# How long a snapshot written from a worker thread waits for the event loop to
# take its encode (``_call_on_loop``) before it encodes in its own thread.
_LOOP_ENCODE_WAIT_S = 30.0


def _call_on_loop(loop, fn):
    """``fn()`` run BY the event loop ``loop`` — between two of its callbacks,
    so no task of that loop runs while it does — and its result (or its
    exception).  Run in this thread instead when there is no such loop, it is
    this thread's own, it is not running (closed, stopped) or doesn't take it
    within ``_LOOP_ENCODE_WAIT_S`` (logged): never both."""
    if loop is None:
        return fn()
    try:
        if asyncio.get_running_loop() is loop:
            return fn()                                 # this thread runs it
    except RuntimeError:
        pass                                            # a worker thread
    if loop.is_closed() or not loop.is_running():
        return fn()
    claim = threading.Lock()
    done = threading.Event()
    box: dict = {}

    def _run() -> None:
        if not claim.acquire(blocking=False):
            return                                      # the thread ran it itself
        try:
            box["result"] = fn()
        except BaseException as exc:                    # noqa: BLE001 — re-raised below
            box["error"] = exc
        finally:
            done.set()
    try:
        loop.call_soon_threadsafe(_run)
    except RuntimeError:                                # closed meanwhile
        return fn()
    deadline = time.monotonic() + _LOOP_ENCODE_WAIT_S
    while not done.wait(0.1):
        stopped = loop.is_closed() or not loop.is_running()
        if (stopped or time.monotonic() >= deadline) and claim.acquire(blocking=False):
            if not stopped:
                log.warning("Snapshot: the event loop did not take the encode within %.0f s — "
                            "encoding in the writer's thread", _LOOP_ENCODE_WAIT_S)
            return fn()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _write_snapshot_sync(data_dir: Path, consume_aof: bool = False, aof_writer=None,
                         loop=None, who: str = "Shutdown") -> bool:
    """Write a full snapshot synchronously.  Used during shutdown.

    Safety: refuses to overwrite a populated snapshot with an empty state.
    This guards against the edge case where the server starts, loads an
    empty/broken snapshot, and shuts down before a scan repopulates it —
    without this check the good backup would be rotated away.

    ``consume_aof`` (the server's shutdown, when it holds the data directory
    — ``owns_library``: its store was loaded from these files and journalled
    every write since): the AOF bytes present before the store is read are in
    the snapshot, so they are dropped from the AOF after it (and recorded in
    it, ``AOF_MARK``) — left in place, the next start (or the merger's final
    merge) applied them a second time: every play since the last merge
    counted twice.  ``aof_writer`` (its journal): flushed first, so what it
    still held is consumed with the rest; the records still in its buffer
    when the store is encoded (a failed flush kept them, or they were
    written since) are in the snapshot too, and are dropped from the buffer
    once the snapshot is saved — written at exit, they'd be applied on top
    of it.  That count and the encode must see the same store: ``loop`` (the
    event loop that makes the store's writes, this function then running in
    a worker thread) runs both together (``_call_on_loop``) — read apart, a
    write landing in between was in the snapshot AND written at exit (plays
    counted twice).  The writer's lock is held from its flush to that drop,
    so no flush writes a record in between either.  The command-line import
    writes a library that isn't this AOF's, and leaves the AOF as it is.
    """
    from soniqboom.core.store import get_store

    store = get_store()
    data_dir.mkdir(parents=True, exist_ok=True)
    held = (aof_writer.exclusive() if consume_aof and aof_writer is not None
            else contextlib.nullcontext())
    with held:
        return _write_snapshot_locked(data_dir, store, consume_aof, aof_writer, loop, who)


def _write_snapshot_locked(data_dir: Path, store, consume_aof: bool, aof_writer, loop,
                           who: str = "Shutdown") -> bool:
    primary = data_dir / "library.json"
    aof_path = data_dir / "library.aof"

    consumed = b""
    if consume_aof:
        if aof_writer is not None:
            try:
                aof_writer.stop()                       # flush + close (reopened only to write)
            except Exception:                           # noqa: BLE001 — records stay buffered
                log.warning("%s: final AOF flush failed", who, exc_info=True)
        try:
            consumed = read_aof(aof_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("%s: AOF unreadable (%s) — the snapshot leaves it as it is", who, exc)
            consume_aof = False
    mark = aof_mark(consumed) if consumed else None     # (hashing: off the loop)
    count_held = consume_aof and aof_writer is not None

    def _encode() -> tuple[int, int, str]:
        held_back = aof_writer.buffer_depth if count_held else 0   # written to the store already
        state = store.to_snapshot()
        if mark is not None:
            state[AOF_MARK] = mark
        return held_back, len(state.get("tracks", {})), encode_state(state)

    t0 = time.monotonic()
    held_back, new_count, text = _call_on_loop(loop, _encode)

    # Refuse to overwrite a populated snapshot with an empty one.
    if new_count == 0 and primary.exists():
        old = _try_load_json(primary)
        old_count = len(old.get("tracks", {})) if isinstance(old, dict) else 0
        if old_count > 0:
            log.warning(
                "%s: store is empty but snapshot has %d tracks — skipping write to preserve data",
                who, old_count,
            )
            return False

    if not _write_library_text(data_dir, text, who):
        return False
    del text
    try:
        if consumed:
            drop_aof_prefix(aof_path, len(consumed))
    finally:
        # (also when the drop failed: the snapshot holds these records; its
        # mark makes the next start skip the AOF bytes it holds, but records
        # written after them at exit would be applied on top of it)
        if held_back:
            aof_writer.discard_buffered(held_back)
            log.info("%s: %d unflushed AOF record(s) are in the snapshot — not written",
                     who, held_back)

    elapsed = (time.monotonic() - t0) * 1000
    log.info("%s snapshot written in %.0fms (%d tracks)", who, elapsed, new_count)
    return True


def write_imported_library(data_dir: Path) -> str | None:
    """Save the store (just filled from a backup by the command-line import)
    as the data directory's library: ``library.json`` written, then the AOF
    removed — its records are the replaced library's, and replayed on top of
    the import they'd bring back what the backup doesn't hold.  The snapshot
    carries the AOF's mark (``AOF_MARK``), so a stop between the write and the
    removal still skips those records at the next start.  The caller holds the
    data directory (``claim_library``), so no server writes meanwhile.

    None when saved; else why not ("empty": the backup has no tracks and the
    library has — refused, as the shutdown snapshot refuses it; "write": the
    file could not be written).  Nothing is changed then."""
    from soniqboom.core.store import get_store

    primary = data_dir / "library.json"
    aof_path = data_dir / "library.aof"
    with library_files_locked(data_dir):
        state = get_store().to_snapshot()
        if not state.get("tracks") and primary.exists():
            old = _try_load_json(primary)
            if isinstance(old, dict) and old.get("tracks"):
                return "empty"
        try:
            old_aof = read_aof(aof_path)
        except FileNotFoundError:
            old_aof = b""
        if old_aof:
            state[AOF_MARK] = aof_mark(old_aof)
        if not _write_library_text(data_dir, encode_state(state), "Import"):
            return "write"
        # (records a restart set aside for the replaced library, and its
        # folder-browse cache, go with its AOF)
        for stale in (aof_path, *data_dir.glob("library.aof.late-exec-*"),
                      data_dir / "browse_cache.pickle"):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        _fsync_dir(data_dir)
    return None


# The set-aside ``library.aof.late-exec-*`` files a snapshot written by a
# conversion holds (``_convert``): ``{file name: _late_digest(its records)}``.
# A start finding such a file still there (a stop between the write and the
# removal) removes it instead of adding its records a second time.
LATE_MARK = "late_exec_merged"


def _late_digest(body: bytes) -> str:
    return hashlib.blake2b(body, digest_size=16).hexdigest()


def _late_records(data_dir: Path, aof: bytes, held: Any,
                  covered: "list | None" = None) -> "list[tuple[Path, bytes]]":
    """The set-aside ``library.aof.late-exec-*`` files whose records are still
    to replay after the AOF ``aof``, oldest first, as ``(path, complete
    lines)`` — read only (``adopt_exec_late_records`` adds them to the AOF
    instead).  Left out: one ``held`` names (``LATE_MARK``: in the snapshot),
    one the AOF already ends with (a start that stopped before removing it —
    added to ``covered``, when given: its records are replayed with the AOF),
    one with no complete record or that can't be read."""
    held = held if isinstance(held, dict) else {}
    out = []
    for late in sorted(data_dir.glob("library.aof.late-exec-*")):
        try:
            raw = late.read_bytes()
        except OSError:
            log.warning("AOF: %s could not be read — not replayed", late.name)
            continue
        body = raw[:raw.rfind(b"\n") + 1]
        if not body or held.get(late.name) == _late_digest(body):
            continue
        if aof.endswith(body):
            if covered is not None:
                covered.append((late, body))
            continue
        out.append((late, body))
    return out


def adopt_exec_late_records(data_dir: Path, held: Any = None) -> int:
    """Append the records the source install's restart set aside between
    sealing its journal and its ``os.execv`` (``library.aof.late-exec-*``,
    ``AOFWriter.late_tag``) to ``library.aof``, oldest file first, and remove
    those files.  They follow every record of that journal and precede this
    start's load, so they are replayed with the AOF.  Only complete lines are
    taken (an exec in the middle of a write leaves a torn last one), the
    append is fsynced before the file is removed (and the removal before
    returning), and an AOF that already ends with them (a start that stopped
    between the two) gets nothing twice.  A file that can't be added is
    renamed to a plain set-aside file (``library.aof.late-…``, kept for an
    operator, never replayed — later it would land after newer records).
    A file ``held`` names (the snapshot's ``LATE_MARK``: a conversion holds
    its records) is removed without adding anything.  Caller holds the
    library's locks (``init_persistence``).  Returns the records adopted."""
    aof = data_dir / "library.aof"
    adopted = 0
    held = held if isinstance(held, dict) else {}
    for late in sorted(data_dir.glob("library.aof.late-exec-*")):
        def _set_aside(why: str) -> None:
            kept = late.with_name(late.name.replace(".late-exec-", ".late-", 1))
            log.exception("AOF: %s %s — kept as %s, not replayed", late.name, why, kept.name)
            try:
                os.replace(late, kept)
            except OSError:
                log.warning("AOF: %s could not be renamed", late.name, exc_info=True)
        try:
            raw = late.read_bytes()
        except OSError:
            _set_aside("could not be read")
            continue
        body = raw[:raw.rfind(b"\n") + 1]
        if len(body) < len(raw):
            log.warning("AOF: %s ends in a partial record (%d bytes) — not replayed",
                        late.name, len(raw) - len(body))
        if body and held.get(late.name) == _late_digest(body):
            body = b""                                  # (already in the snapshot)
        if body:
            try:
                existing = aof.read_bytes() if aof.exists() else b""
                if not existing.endswith(body):
                    sep = b"" if not existing or existing.endswith(b"\n") else b"\n"
                    with open(aof, "ab") as f:
                        f.write(sep + body)
                        f.flush()
                        os.fsync(f.fileno())
                    _fsync_dir(data_dir)
            except OSError:
                _set_aside("could not be added to the AOF")
                continue
            adopted += body.count(b"\n")
        try:
            late.unlink()
            _fsync_dir(data_dir)
        except OSError:
            log.warning("AOF: %s could not be removed", late.name, exc_info=True)
    if adopted:
        log.info("AOF: %d record(s) written while the last restart was ending added to "
                 "the journal", adopted)
    return adopted


def init_persistence(data_dir: Path) -> None:
    """Full startup sequence: load snapshot → replay AOF → populate store —
    the files read under ``library_files_lock`` (waiting for a merge of a
    stopped run still ending) and ``_library_flock`` (a merge of the merger
    process).  First, the records a restart set aside right before its exec
    join the AOF (``adopt_exec_late_records``).

    An older database (``_needs_upgrade``) is converted instead
    (``_convert``): backed up first, converted in memory, saved — and the
    files are only changed by that save.  A database newer than
    ``DB_VERSION``, a failed backup or a failed save raise
    ``DatabaseVersionError`` (logged, and shown as the startup status):
    nothing is loaded."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if not library_files_lock.acquire(timeout=1.0):
        log.info("Waiting for a merge of the previous server run to finish before loading the library")
        library_files_lock.acquire()
    try:
        with _library_flock(data_dir):
            state = load_snapshot(data_dir)
            try:
                old = _needs_upgrade(data_dir, state)
                if old is not None:
                    _convert(data_dir, state, old)
                else:
                    adopt_exec_late_records(data_dir, held=state.get(LATE_MARK))
                    upgrade_state(state, "", data_dir)
                    replay_aof(state, data_dir)
            except DatabaseVersionError as exc:
                log.critical("%s", exc)
                try:
                    from soniqboom.core.startup_status import set_phase
                    set_phase("failed", "The library database could not be opened", str(exc))
                except Exception:                       # noqa: BLE001
                    pass
                raise
    finally:
        library_files_lock.release()
    populate_store(state)
    log.info("Library database V%d", DB_VERSION)
    # Preserve a "last known good" copy of the snapshot that just loaded, for
    # hand-restore if the primary + .bak ever both go bad (see make_prev_backup).
    make_prev_backup(data_dir)


def _set_owner(state: dict, owner: str) -> None:
    """Record ``owner`` as the state's ``user_data_owner`` (records without
    uid go to it — ``merger._entry_uid``); nothing for ``""``."""
    from soniqboom.core.user_data import OWNER_CONFIG_KEY
    if owner:
        cfg = state.get("config")
        if not isinstance(cfg, dict):
            cfg = state["config"] = {}
        cfg[OWNER_CONFIG_KEY] = owner


def _drop_orphan_playlists(state: dict, data_dir: Path) -> int:
    """At a conversion: remove the playlists whose owner account no longer
    exists — accounts deleted before 1.14.5 left them behind, reachable by
    nobody (Sebastian's decision: deleted, not handed to an admin).  Only when
    the accounts can be read (``users.accounts_on_disk``: an unreadable or
    missing ``users.json`` removes nothing); a playlist kept aside for an
    account a restore didn't find (``import:…``) stays.  The backup holds
    them.  Returns how many went."""
    from soniqboom.core.users import accounts_on_disk
    pls = state.get("playlists")
    if not isinstance(pls, dict) or not pls:
        return 0
    ids = {a.get("id") for a in accounts_on_disk(data_dir) if isinstance(a, dict)}
    ids.discard(None)
    if not ids:
        return 0
    gone = [pid for pid, pl in pls.items()
            if isinstance(pl, dict) and isinstance(pl.get("owner_user_id"), str)
            and pl["owner_user_id"] and pl["owner_user_id"] not in ids
            and not pl["owner_user_id"].startswith("import:")]
    for pid in gone:
        log.info("Upgrade: playlist %r removed — its account no longer exists",
                 pls[pid].get("name") or pid)
        del pls[pid]
    return len(gone)


def _convert(data_dir: Path, state: dict, old: int) -> None:
    """Convert a database of version ``old`` (loaded: ``state``) at a start.
    Caller holds the library's locks; no journal writes yet.

      1. back the files up (``backup_before_upgrade``);
      2. convert the snapshot in memory (``upgrade_state``; the data goes to
         the legacy owner — ``_legacy_owner``) and replay on top the AOF's
         bytes and the set-aside ``library.aof.late-exec-*`` records — the
         latter without adding them to the AOF first;
      3. refuse when no snapshot loaded (no tracks) while ``library.json`` or
         its ``.bak`` on disk holds tracks (one could not be read just now);
      4. write the V2 snapshot, which marks the AOF bytes (``AOF_MARK``) and
         the set-aside files (``LATE_MARK``) it holds, then write the lines
         the replay refused to the quarantine file, drop those bytes from the
         AOF and remove those files.

    Until step 4's write nothing in the data folder but the backup changes:
    a crash before it converts again at the next start (reusing the backup).
    A crash after it skips what the marks name."""
    global _last_loaded_from
    loaded = _last_loaded_from
    backup = backup_before_upgrade(
        data_dir, old,
        snapshot_from=loaded if loaded is not None and loaded.name != "library.json" else None)
    owner = _legacy_owner(state, data_dir)
    upgrade_state(state, owner, data_dir)
    if _has_v1_shapes(state):
        _upgrade_v1_to_v2(state, owner)                 # (a V2 file an older version wrote to)
    _set_owner(state, owner)
    _drop_orphan_playlists(state, data_dir)
    if not state.get("tracks"):
        # Nothing loaded: refused while the files on disk hold tracks (one
        # could not be read just now) — whatever the AOF would add.
        for name in ("library.json", "library.json.bak"):
            on_disk = _try_load_json(data_dir / name)
            if isinstance(on_disk, dict) and on_disk.get("tracks"):
                raise DatabaseVersionError(
                    f"The library database could not be read completely ({name} holds "
                    f"tracks, the loaded library none) — it was not converted to "
                    f"V{DB_VERSION}.  Start SoniqBoom again; the files are unchanged "
                    f"(backup: {backup}).")
    aof_path = data_dir / "library.aof"
    try:
        data = read_aof(aof_path)
    except FileNotFoundError:
        data = b""
    covered: list = []
    late = _late_records(data_dir, data, state.get(LATE_MARK), covered)
    quarantined: list = []
    replay_aof(state, data_dir, data, extra=[body for _p, body in late], quarantine=quarantined)
    done = late + covered
    if data:
        state[AOF_MARK] = aof_mark(data)
    if done:
        state[LATE_MARK] = {p.name: _late_digest(body) for p, body in done}
    try:
        ok = write_library_file(data_dir, state, "Upgrade")
    except Exception as exc:                            # noqa: BLE001
        raise DatabaseVersionError(
            f"The converted library database could not be written ({exc}) — the V{old} "
            f"files are unchanged (backup: {backup})") from exc
    if not ok:
        raise DatabaseVersionError(
            f"The converted library database could not be written — the V{old} files are "
            f"unchanged (backup: {backup})")
    # (the converted file is the one that loaded — ``make_prev_backup`` keeps a
    # copy of a POPULATED one only, as for any load)
    _last_loaded_from = data_dir / "library.json" if state.get("tracks") else None
    if quarantined:
        try:
            with open(data_dir / "library.aof.quarantine", "a") as f:
                f.writelines(quarantined)
        except OSError:
            log.exception("Could not write to the AOF quarantine")
    if data:
        drop_aof_prefix(aof_path, len(data))
    for p, _body in done:
        try:
            p.unlink()
        except OSError:
            # Set aside under a name no start replays (as adopt_exec_late_records
            # does), so its records — in the snapshot now — can't apply twice.
            kept = p.with_name(p.name.replace(".late-exec-", ".late-", 1))
            log.warning("AOF: %s could not be removed (its records are in the snapshot) "
                        "— renamed %s", p.name, kept.name, exc_info=True)
            try:
                os.replace(p, kept)
            except OSError:
                log.error("AOF: %s could not be renamed either — the snapshot's mark "
                          "keeps its records from being applied again", p.name)
    if done:
        _fsync_dir(data_dir)
    log.info("Library database converted from V%d to V%d (the previous files: %s)",
             old, DB_VERSION, backup)


def read_library_state(data_dir: Path) -> dict:
    """The library as a start would load it — the snapshot, the AOF and the
    set-aside records replayed, an older database converted — built in
    memory only: nothing in the data folder is written (no backup, no
    conversion, no quarantine file, no lock file created).  For the
    command-line export, which may run beside a server.  Raises
    ``DatabaseVersionError`` for a newer database."""
    with library_files_lock, _library_flock(data_dir, create=False):
        state = load_snapshot(data_dir)
        old = _needs_upgrade(data_dir, state)
        owner = _legacy_owner(state, data_dir) if old is not None else ""
        upgrade_state(state, owner, data_dir)
        if _has_v1_shapes(state):
            _upgrade_v1_to_v2(state, owner)
        _set_owner(state, owner)
        try:
            data = (data_dir / "library.aof").read_bytes()
        except FileNotFoundError:
            data = b""
        late = _late_records(data_dir, data, state.get(LATE_MARK))
        replay_aof(state, data_dir, data, extra=[body for _p, body in late], quarantine=False)
    state.pop(AOF_MARK, None)
    state.pop(LATE_MARK, None)
    return state
