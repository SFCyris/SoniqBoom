# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telling a moved file from a new one.

A track id is ``uuid5`` of its path — of ``<scan root>:<path>`` for a network
share — so a file that is moved or renamed (a folder renamed, a local HVSC
update that moved tunes, a share added again under another address) gets a
new id.  Its rating, plays, history, playlist entries, waveform and Subsonic
stars stay keyed by the old one.  A scan that finds a file under a new path
and no longer finds a stored one hands the old row's user data over
(``scanner._hand_over_user_data`` → ``store.remap_track_ids``) when both are
the same file:

* **strong identity** — a whole-file checksum plus the size: ``sid_md5`` /
  ``file_md5``, or ``content_key`` (``content_key()``: a hash of the size and
  the first and last 64 KiB, made by the local scan's workers for the files
  without a checksum);
* **weak identity**, for rows without a strong key (indexed before 2.3.0, or
  read from a share): size, modification time (whole seconds — a rename or a
  move on one file system keeps it), format, duration, title, artist, album;
  then, for the rows still unpaired, size, time and format alone (a stored
  row's tags may be the user's edits).

Only an unambiguous pair moves (``match_moves``): exactly one old row and
exactly one new row with that identity; two rows that both have a strong key
and differ in it are never the same file.  The new row also takes what the
user gave the old one and what a later pass found out about the same bytes
(``carried_fields``).

A share root added again under another address
(``scanner._rerooted_moves``): the old root's rows (still listed, the root
offline or being removed — or, once removed, the rows with user data the
removal kept: ``scanner.remember_removed_root``) and the new root's rows of
the same path below their roots are the same file when the sizes match and
the strong keys (or, without, the modification times and formats) do.

Import-light: pure functions over track dicts (and one file read in a scan
worker).
"""
from __future__ import annotations

import hashlib
import math
import os
import time
import uuid
from collections import defaultdict

CONTENT_KEY = "content_key"
_CK_SPAN = 64 * 1024

# Store config key: the rows with user data of roots removed with their
# tracks (``scanner.remember_removed_root``) — ``{root: {"at": epoch s,
# "rows": {rel: [old id, size, mtime, strong key or None, carried fields,
# format]}}}``.  A root added later under another address of the same share
# takes their user data (``scanner._rerooted_moves``).  Kept ``GHOST_TTL_S``,
# at most ``GHOST_MAX_ROWS`` rows.
REMOVED_ROOTS_KEY = "removed_root_rows"
GHOST_TTL_S = 180 * 86400
GHOST_MAX_ROWS = 100_000


# ── Identity ────────────────────────────────────────────────────────────────

def content_key(path: str) -> "str | None":
    """(scan worker) A hash of a local file's size and its first and last
    64 KiB — what tells a moved file from another one when it has no
    whole-file checksum.  One more read than the extraction made (the tail);
    None when the file can't be read."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            head = f.read(_CK_SPAN)
            if size > 2 * _CK_SPAN:
                f.seek(size - _CK_SPAN)
            tail = f.read(_CK_SPAN) if size > _CK_SPAN else b""
    except OSError:
        return None
    h = hashlib.blake2b(digest_size=12)
    h.update(size.to_bytes(8, "little"))
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def _size(row: dict) -> "int | None":
    v = row.get("file_size")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return int(v)


def strong_key(row: dict) -> "tuple | None":
    """``(kind, checksum, size)`` — a whole-file checksum, or the content
    key — or None."""
    size = _size(row)
    if size is None:
        return None
    for f in ("sid_md5", "file_md5", CONTENT_KEY):
        v = row.get(f)
        if isinstance(v, str) and v:
            return (f, v, size)
    return None


def _num(v) -> "float | None":
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def weak_key(row: dict) -> "tuple | None":
    """Size, modification time (whole seconds), format, duration and the
    title / artist / album tags — or None without a size or a time."""
    size = _size(row)
    mtime = _num(row.get("mtime"))
    if size is None or not mtime:
        return None
    dur = _num(row.get("duration"))
    return (size, int(round(mtime)), str(row.get("format") or ""),
            round(dur, 1) if dur is not None else None,
            str(row.get("title") or ""), str(row.get("artist") or ""),
            str(row.get("album") or ""))


def _index(rows: dict, keyf) -> dict:
    out: dict = defaultdict(list)
    for tid, r in rows.items():
        if isinstance(r, dict):
            k = keyf(r)
            if k is not None:
                out[k].append(tid)
    return out


def match_moves(old_rows: dict, new_rows: dict) -> dict:
    """``{old id: new id}`` for the old rows (stored, their files no longer
    listed) that are the same file as one new row (listed now, not stored
    before): strong identity first, then weak identity for the rest — each
    only where exactly one old and one new row have it."""
    mapping: dict = {}
    if not old_rows or not new_rows:
        return mapping
    so, sn = _index(old_rows, strong_key), _index(new_rows, strong_key)
    for k, olds in so.items():
        news = sn.get(k)
        if news and len(olds) == 1 and len(news) == 1 and olds[0] != news[0]:
            mapping[olds[0]] = news[0]
    taken = set(mapping.values())
    rest_old = {t: r for t, r in old_rows.items() if t not in mapping}
    rest_new = {t: r for t, r in new_rows.items() if t not in taken}
    if not rest_old or not rest_new:
        return mapping
    # Weak identity with the tags first, then without them: a stored row's
    # tags may be the user's edits or a later pass's fills, which a fresh read
    # of the file doesn't have (the size and the second of the file's time
    # alone, with its format, still single out one file in practice).
    for keyf in (weak_key, _bare_key):
        rest_old = {t: r for t, r in rest_old.items() if t not in mapping}
        taken = set(mapping.values())
        rest_new = {t: r for t, r in rest_new.items() if t not in taken}
        if not rest_old or not rest_new:
            break
        wo, wn = _index(rest_old, keyf), _index(rest_new, keyf)
        for k, olds in wo.items():
            news = wn.get(k)
            if not news or len(olds) != 1 or len(news) != 1:
                continue
            o, n = olds[0], news[0]
            a, b = strong_key(old_rows[o]), strong_key(new_rows[n])
            if (a is not None and b is not None and a != b) or o == n:
                continue                        # (known to be other bytes)
            mapping[o] = n
    return mapping


def _bare_key(row: dict) -> "tuple | None":
    """``weak_key`` without the tags and the length: size, the file's time
    (whole seconds), format."""
    k = weak_key(row)
    return k[:3] if k is not None else None


# ── What the new row takes over ─────────────────────────────────────────────

_HEALTH_FIELDS = ("health", "health_note", "health_by", "health_at", "health_renderer")
# Learned from the same bytes by a later pass (a render, a probe, HVSC):
# taken over where the new row has no value of its own.
_LEARNED_FIELDS = ("duration_rev", "default_subsong", "tune_lengths", "hvsc_lengths", "stil",
                   "loudness", "loudness_source", "loudness_v", "subsongs", "tune_slots")


def _blank(v) -> bool:
    return v is None or v == "" or v == [] or v == 0


def carried_fields(old: dict, new: dict) -> dict:
    """The fields the row of a moved file (``new``) takes over from its old
    row: when it was added (``added_at`` — it is not new to the library), the
    user's own edits (``user_edited`` fields, a user year), a listener's
    playback report, and what a later pass learned from the same bytes (a
    probed length, the default tune, HVSC lengths and STIL) where ``new`` has
    none.  Nothing that comes from the path (a folder album, a folder game)."""
    out: dict = {}
    if not isinstance(old, dict):
        return out
    if old.get("added_at"):
        out["added_at"] = old["added_at"]
    edited = old.get("user_edited")
    if isinstance(edited, list) and edited:
        for f in edited:
            if isinstance(f, str) and f != "year" and f in old:
                out[f] = old[f]
        out["user_edited"] = list(edited)
    if old.get("year_source") == "user":
        for f in ("year", "year_source", "year_file"):
            if f in old:
                out[f] = old[f]
    if old.get("health") and not new.get("health"):
        for f in _HEALTH_FIELDS:
            if f in old:
                out[f] = old[f]
    for f in _LEARNED_FIELDS:
        if not _blank(old.get(f)) and _blank(new.get(f)):
            out[f] = old[f]
    od, nd = _num(old.get("duration")), _num(new.get("duration"))
    if od and not nd:
        out["duration"] = old["duration"]
    return {k: v for k, v in out.items() if new.get(k) != v}


def file_changed(old: "dict | None", new: dict) -> bool:
    """Did the file of a stored row ``old`` change on disk, by the freshly
    read row ``new`` of the same path?  Another size, another whole-file
    checksum, or — without checksums on both sides — another modification
    time (a head/tail ``content_key`` does not prove the middle unchanged)."""
    if not isinstance(old, dict):
        return False
    a, b = _size(old), _size(new)
    if a is not None and b is not None and a != b:
        return True
    for f in ("sid_md5", "file_md5"):
        x, y = old.get(f), new.get(f)
        if x and y:
            return x != y
    mo, mn = _num(old.get("mtime")), _num(new.get("mtime"))
    if mo is None or mn is None:
        return mo != mn
    return abs(mo - mn) >= 1.0


# ── A root added again under another address ────────────────────────────────

def share_key(root: str) -> "tuple | None":
    """What two addresses of one share have in common: the scheme and the
    path after the host (``smb://10.0.0.5/Music`` and ``smb://nas/Music`` →
    ``("smb", "/music")``).  None for a local folder."""
    if "://" not in root:
        return None
    scheme, rest = root.split("://", 1)
    _host, sep, path = rest.partition("/")
    scheme = scheme.lower()
    if scheme in ("webdavs", "https"):
        scheme = "https"
    elif scheme in ("webdav", "http", "httpindex"):
        scheme = "http"
    path = ("/" + path if sep else "/").rstrip("/") or "/"
    return (scheme, path.lower() if scheme == "smb" else path)


def rel_of(root: str, path: str) -> "str | None":
    """The path of a track row below its root (``<root>:<rel>`` for a share,
    ``<root>/<rel>`` for a folder) — None when ``path`` isn't below ``root``."""
    if "://" in root:
        if path.startswith(root + ":"):
            return path[len(root) + 1:]
        return None
    base = root.rstrip("/")
    if path.startswith(base + "/"):
        return path[len(base) + 1:]
    return None


def track_id_for(root: str, rel: str) -> str:
    """The id the file ``rel`` below ``root`` has (the scanners' ``uuid5``)."""
    p = f"{root}:{rel}" if "://" in root else f"{root.rstrip('/')}/{rel}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, p))


def same_file_elsewhere(old: dict, new: dict) -> bool:
    """Is the old root's row ``old`` the file of the new root's row ``new``
    (the same path below both roots)?  Equal sizes, and equal strong keys —
    or, when one side has none, modification times within 2 s and the same
    format."""
    a, b = _size(old), _size(new)
    if a is None or a != b:
        return False
    ka, kb = strong_key(old), strong_key(new)
    if ka is not None and kb is not None and ka[0] == kb[0]:
        return ka == kb
    mo, mn = _num(old.get("mtime")), _num(new.get("mtime"))
    if not mo or not mn or abs(mo - mn) > 2.0:
        return False
    return str(old.get("format") or "") == str(new.get("format") or "")


def ghost_entry(row: dict) -> list:
    """A removed root's row as ``remember_removed_root`` keeps it."""
    sk = strong_key(row)
    return [row.get("id"), _size(row), _num(row.get("mtime")),
            list(sk) if sk else None, carried_fields(row, {}), str(row.get("format") or "")]


def ghost_row(rel: str, entry) -> "dict | None":
    """A kept row (``ghost_entry``) back as a row ``same_file_elsewhere`` and
    ``carried_fields`` read."""
    if not isinstance(entry, list) or len(entry) < 5 or not isinstance(entry[0], str):
        return None
    row = dict(entry[4]) if isinstance(entry[4], dict) else {}
    row.update(id=entry[0], file_size=entry[1], mtime=entry[2])
    if len(entry) > 5 and isinstance(entry[5], str):
        row["format"] = entry[5]
    sk = entry[3]
    if isinstance(sk, list) and len(sk) == 3 and sk[0] in ("sid_md5", "file_md5", CONTENT_KEY):
        row[sk[0]] = sk[1]
    return row


def prune_ghosts(reg, now: "float | None" = None) -> dict:
    """The removed roots' rows still kept: none older than ``GHOST_TTL_S``,
    at most ``GHOST_MAX_ROWS`` (the newest roots first)."""
    if not isinstance(reg, dict):
        return {}
    now = time.time() if now is None else now
    roots = [(r, v) for r, v in reg.items()
             if isinstance(v, dict) and isinstance(v.get("rows"), dict) and v["rows"]
             and now - float(v.get("at") or 0) < GHOST_TTL_S]
    roots.sort(key=lambda rv: -float(rv[1].get("at") or 0))
    out: dict = {}
    n = 0
    for r, v in roots:
        if n + len(v["rows"]) > GHOST_MAX_ROWS:
            continue                            # (a smaller, older root may still fit)
        out[r] = v
        n += len(v["rows"])
    return out
