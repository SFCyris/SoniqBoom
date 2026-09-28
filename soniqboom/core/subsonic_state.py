# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-user Subsonic state — stars, album / artist ratings, the saved play
queue and bookmarks.

The Subsonic protocol keeps a little state per *user* that SoniqBoom's own
library model has no home for: which songs / albums / artists a user starred
(``star`` / ``unstar`` / ``getStarred2``), the ratings they gave albums and
artists (``setRating`` on an album / artist id — song ratings stay the
library-wide track ratings the web UI edits), and the play queue a client
parks on the server so another device can resume it (``savePlayQueue`` /
``getPlayQueue``).  All small, so they live in ONE JSON document under the
app data dir (``<data_dir>/subsonic_state.json``)::

    {"version": 1,
     "users": {"<user id>": {
         "starred": {"song": {"<id>": <epoch s>}, "album": {...}, "artist": {...}},
         "starred_changed": {"song": <epoch ms>, "album": ..., "artist": ...},
         "ratings": {"album": {"<id>": 1-5}, "artist": {...}},
         "ratings_changed": {"album": <epoch ms>, "artist": ...},
         "queue": {"ids": [...], "current": "<id>", "current_index": <int>,
                   "position": <ms>, "changed": <epoch s>, "changed_by": "<client>"},
         "bookmarks": {"<track id>": {"position": <ms>, "comment": "...",
                                      "created": <epoch s>, "changed": <epoch s>}}}},
     "index_stamp": {"fp": "<artist-index fingerprint>", "ms": <epoch ms>},
     "index_stamps": {"<music folder hash>": {"fp": "...", "ms": <epoch ms>}}}

``index_stamp`` backs getIndexes' ``lastModified``: it only moves when the
artist index's CONTENT changes, and it survives a restart, so a client's
``ifModifiedSince`` keeps getting the cheap "unchanged" answer until the
library really changes.  ``index_stamps`` does the same for each music
folder's own (``musicFolderId``-filtered) index.

Ids are stored verbatim (track ids, ``al:``/``fa:`` album ids, ``ar:`` artist
ids) and resolved lazily on read — a starred id whose track was since deleted is
simply skipped, never an error.

Loaded lazily on first use; every mutation rewrites the file atomically (temp
file + ``os.replace``) from a snapshot serialised on the event loop, with the
disk write itself pushed to a worker thread.  Writes are generation-guarded so a
burst of mutations lands the newest snapshot, never an older one last.  All
reads and mutations happen on the event loop (single-threaded), so no lock is
needed around the in-memory document itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

from soniqboom.config import get_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "subsonic_state.json"
KINDS = ("song", "album", "artist")
RATING_KINDS = ("album", "artist")
# Hard caps so a misbehaving client can't grow the document without bound.
_MAX_STARRED_PER_KIND = 50_000
_MAX_QUEUE = 10_000
_MAX_BOOKMARKS = 5_000
_MAX_COMMENT = 1_000
_MAX_ID_LEN = 256
_MAX_POSITION = 2 ** 53          # ms; a JSON-safe integer (spec type is long)
_MAX_INDEX_SCOPES = 64           # per-music-folder getIndexes stamps kept


def _num(v: Any, default: float = 0) -> float:
    """A stored number, or ``default`` for anything non-numeric / non-finite
    (``json.loads`` happily yields NaN / Infinity from a damaged file)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
        else default


def _clean_ids(ids) -> list[str]:
    """Drop empty / oversized / non-string ids, keep order, de-duplicate."""
    out: list[str] = []
    seen: set[str] = set()
    for i in ids or ():
        if not isinstance(i, str):
            continue
        i = i.strip()
        if not i or len(i) > _MAX_ID_LEN or i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class SubsonicState:
    """In-memory document + lazy load + atomic persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._path_override = path
        self._path: Path | None = None
        self._doc: dict[str, Any] | None = None
        self._gen = 0
        # (loop, lock) — an asyncio.Lock binds to the loop it first contends
        # on, so keep one per running loop (tests spin a loop per test).
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None

    # ── Load / persist ──────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = self._path_override or (get_data_dir() / _FILE_NAME)
        return self._path

    def _ensure_loaded(self) -> dict[str, Any]:
        if self._doc is not None:
            return self._doc
        doc: dict[str, Any] = {"version": 1, "users": {}}
        p = self.path
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            log.warning("Subsonic state unreadable (%s) — starting empty", exc)
            raw = None
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict):
                    doc = loaded
                else:
                    raise ValueError("unexpected document shape")
            except ValueError as exc:
                # Keep the unreadable file for forensics instead of silently
                # overwriting it with an empty document on the next save.
                aside = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
                try:
                    os.replace(p, aside)
                except OSError:
                    pass
                log.warning("Subsonic state corrupt (%s) — moved to %s", exc, aside)
        self._doc = doc
        return doc

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def save(self) -> None:
        """Persist the current document (newest snapshot wins)."""
        doc = self._ensure_loaded()
        self._gen += 1
        gen = self._gen
        payload = json.dumps(doc, separators=(",", ":"))   # snapshot on the loop
        async with self._get_lock():
            if gen != self._gen:
                return          # a newer snapshot is queued behind us — it writes
            try:
                await asyncio.to_thread(_atomic_write, self.path, payload)
            except OSError as exc:
                log.warning("Subsonic state write failed: %s", exc)

    def reload(self) -> None:
        """Drop the in-memory copy; the next access re-reads the file."""
        self._doc = None
        self._path = None

    # ── Per-user access ─────────────────────────────────────────────────────

    def _user(self, uid: str, *, create: bool) -> dict[str, Any] | None:
        users = self._ensure_loaded()["users"]
        u = users.get(uid)
        if not isinstance(u, dict):
            # Missing — or a hand-edited / damaged entry: never let one bad
            # user record break every call for that user.
            u = None
            if create:
                u = users[uid] = {}
        return u

    # ── Stars ───────────────────────────────────────────────────────────────

    def starred(self, uid: str | None, kind: str) -> dict[str, float]:
        """``{id: starred-at epoch seconds}`` for one kind.  Read-only view —
        callers must not mutate it."""
        if not uid:
            return {}
        u = self._user(uid, create=False)
        if not u:
            return {}
        st = u.get("starred")
        b = st.get(kind) if isinstance(st, dict) else None
        return b if isinstance(b, dict) else {}

    def starred_changed_ms(self, uid: str | None, kind: str) -> int:
        """When this user's stars of ``kind`` last changed (epoch ms, strictly
        increasing per user and kind) — doubles as a cache version."""
        if not uid:
            return 0
        u = self._user(uid, create=False)
        ch = (u or {}).get("starred_changed")
        return int(_num(ch.get(kind))) if isinstance(ch, dict) else 0

    def max_starred_changed_ms(self, kind: str) -> int:
        """The newest ``kind`` star change across ALL users (a few users)."""
        users = self._ensure_loaded()["users"]
        return max((self.starred_changed_ms(uid, kind) for uid in users), default=0)

    @staticmethod
    def _touch_stars(u: dict, kind: str) -> None:
        # Strictly increasing, so it doubles as a version for per-user caches
        # (two changes in the same millisecond still differ).
        ch = u.get("starred_changed")
        if not isinstance(ch, dict):
            ch = u["starred_changed"] = {}
        prev = int(_num(ch.get(kind)))
        now = int(time.time() * 1000)
        ch[kind] = now if now > prev else prev + 1

    async def star(self, uid: str, kind: str, ids) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown star kind {kind!r}")
        ids = _clean_ids(ids)
        if not ids:
            return 0
        u = self._user(uid, create=True)
        st = u.get("starred")
        if not isinstance(st, dict):
            st = u["starred"] = {}
        bucket = st.get(kind)
        if not isinstance(bucket, dict):
            bucket = st[kind] = {}
        now = round(time.time(), 3)
        added = 0
        for i in ids:
            if i in bucket:
                continue              # re-starring keeps the original timestamp
            if len(bucket) >= _MAX_STARRED_PER_KIND:
                break
            bucket[i] = now
            added += 1
        if added:
            self._touch_stars(u, kind)
            await self.save()
        return added

    async def unstar(self, uid: str, kind: str, ids) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown star kind {kind!r}")
        ids = _clean_ids(ids)
        u = self._user(uid, create=False)
        bucket = self.starred(uid, kind) if u else {}
        if not bucket or not ids:
            return 0
        removed = 0
        for i in ids:
            if bucket.pop(i, None) is not None:
                removed += 1
        if removed:
            self._touch_stars(u, kind)
            await self.save()
        return removed

    # ── Album / artist ratings ──────────────────────────────────────────────

    def ratings(self, uid: str | None, kind: str) -> dict[str, int]:
        """``{id: 1-5}`` for one kind (read-only view — don't mutate)."""
        if not uid:
            return {}
        u = self._user(uid, create=False)
        r = (u or {}).get("ratings")
        b = r.get(kind) if isinstance(r, dict) else None
        return b if isinstance(b, dict) else {}

    def ratings_changed_ms(self, uid: str | None, kind: str) -> int:
        """When this user's ``kind`` ratings last changed (strictly increasing
        epoch ms — a per-user cache version)."""
        if not uid:
            return 0
        u = self._user(uid, create=False)
        ch = (u or {}).get("ratings_changed")
        return int(_num(ch.get(kind))) if isinstance(ch, dict) else 0

    async def rate(self, uid: str, kind: str, item_id: str, rating: int) -> bool:
        """Set (1-5) or clear (0) the user's rating of an album / artist.
        False when the id is unusable or the per-kind cap is reached."""
        if kind not in RATING_KINDS:
            raise ValueError(f"unknown rating kind {kind!r}")
        if not (isinstance(item_id, str) and 0 < len(item_id) <= _MAX_ID_LEN):
            return False
        rating = int(rating)
        u = self._user(uid, create=True)
        r = u.get("ratings")
        if not isinstance(r, dict):
            r = u["ratings"] = {}
        bucket = r.get(kind)
        if not isinstance(bucket, dict):
            bucket = r[kind] = {}
        if rating <= 0:
            if bucket.pop(item_id, None) is None:
                return True                   # nothing to clear
        else:
            if item_id not in bucket and len(bucket) >= _MAX_STARRED_PER_KIND:
                return False
            if bucket.get(item_id) == min(5, rating):
                return True
            bucket[item_id] = min(5, rating)
        ch = u.get("ratings_changed")
        if not isinstance(ch, dict):
            ch = u["ratings_changed"] = {}
        prev = int(_num(ch.get(kind)))
        now = int(time.time() * 1000)
        ch[kind] = now if now > prev else prev + 1
        await self.save()
        return True

    # ── getIndexes lastModified ─────────────────────────────────────────────

    def index_stamp(self, fp: str, scope: str = "") -> tuple[int, bool]:
        """``(ms, changed)`` for an artist-index fingerprint: the stored stamp
        while the fingerprint is unchanged, else a fresh stamp that is newer
        than the previous stamp AND every user's artist-star change time —
        getIndexes reports ``max(stamp, user's artist-star time)``, so after a
        wall-clock step back a real index change must still out-rank it.

        ``scope`` keeps a separate stamp per filtered view (one music
        folder's index — ``index_stamps``, at most ``_MAX_INDEX_SCOPES``);
        "" is the whole library's (``index_stamp``)."""
        doc = self._ensure_loaded()
        stamps = None
        if scope:
            stamps = doc.get("index_stamps")
            if not isinstance(stamps, dict):
                stamps = doc["index_stamps"] = {}
            cur = stamps.get(scope)
        else:
            cur = doc.get("index_stamp")
        prev_ms = int(_num(cur.get("ms"), -1)) if isinstance(cur, dict) else -1
        if prev_ms >= 0 and cur.get("fp") == fp:
            return prev_ms, False
        floor = max(prev_ms, self.max_starred_changed_ms("artist"))
        ms = max(int(time.time() * 1000), floor + 1)
        if stamps is not None:
            if scope not in stamps and len(stamps) >= _MAX_INDEX_SCOPES:
                oldest = min(stamps, key=lambda k: _num((stamps[k] or {}).get("ms")
                                                        if isinstance(stamps[k], dict) else 0))
                stamps.pop(oldest, None)
            stamps[scope] = {"fp": fp, "ms": ms}
        else:
            doc["index_stamp"] = {"fp": fp, "ms": ms}
        return ms, True

    # ── Play queue ──────────────────────────────────────────────────────────

    def play_queue(self, uid: str | None) -> dict[str, Any] | None:
        if not uid:
            return None
        u = self._user(uid, create=False)
        q = (u or {}).get("queue")
        if not (isinstance(q, dict) and isinstance(q.get("ids"), list) and q["ids"]):
            return None
        ids = [i for i in q["ids"] if isinstance(i, str)]
        if not ids:
            return None
        cur = q.get("current") if isinstance(q.get("current"), str) else None
        idx = q.get("current_index")
        if not (isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(ids)
                and (cur is None or ids[idx] == cur)):
            # Id-based saves (and old documents) carry no index: the first
            # occurrence of ``current`` is the best reconstruction.
            idx = ids.index(cur) if cur in ids else 0
        return {"ids": ids,
                "current": cur if cur is not None else ids[idx],
                "current_index": idx,
                "position": int(_num(q.get("position"))),
                "changed": _num(q.get("changed")),
                "changed_by": q.get("changed_by") if isinstance(q.get("changed_by"), str) else ""}

    async def save_play_queue(self, uid: str, ids, *, current: str | None = None,
                              current_index: int | None = None,
                              position: int, changed_by: str) -> float | None:
        """Save (or, with no ids, clear) the user's queue.  ``current`` (id —
        getPlayQueue) or ``current_index`` (OpenSubsonic indexBasedQueue —
        the only unambiguous form when the queue holds duplicates).  Returns
        the saved queue's ``changed`` stamp (epoch s), or ``None`` when it
        cleared the queue."""
        ids = [i for i in (ids or ()) if isinstance(i, str) and 0 < len(i) <= _MAX_ID_LEN]
        ids = ids[:_MAX_QUEUE]          # duplicates ARE legal in a queue
        u = self._user(uid, create=True)
        if not ids:
            # Spec: saving without ids clears the saved queue.
            if u.pop("queue", None) is not None:
                await self.save()
            return None
        if current_index is not None and 0 <= current_index < len(ids):
            idx = current_index
        elif current is not None and current in ids:
            idx = ids.index(current)
        else:
            idx = 0
        u["queue"] = {
            "ids": ids,
            "current": ids[idx],
            "current_index": idx,
            "position": min(_MAX_POSITION, max(0, int(_num(position)))),
            "changed": round(time.time(), 3),
            "changed_by": (changed_by or "")[:128],
        }
        await self.save()
        return u["queue"]["changed"]

    # ── Bookmarks ───────────────────────────────────────────────────────────

    def bookmarks(self, uid: str | None) -> dict[str, dict]:
        """``{track id: {position, comment, created, changed}}`` (read-only)."""
        if not uid:
            return {}
        u = self._user(uid, create=False)
        b = (u or {}).get("bookmarks")
        return {k: v for k, v in b.items() if isinstance(k, str) and isinstance(v, dict)} \
            if isinstance(b, dict) else {}

    async def set_bookmark(self, uid: str, track_id: str, *, position: int,
                           comment: str = "") -> bool:
        if not (isinstance(track_id, str) and 0 < len(track_id) <= _MAX_ID_LEN):
            return False
        u = self._user(uid, create=True)
        b = u.get("bookmarks")
        if not isinstance(b, dict):
            b = u["bookmarks"] = {}
        now = round(time.time(), 3)
        cur = b.get(track_id)
        if not isinstance(cur, dict):
            if len(b) >= _MAX_BOOKMARKS:
                return False
            cur = b[track_id] = {"created": now}
        cur["position"] = min(_MAX_POSITION, max(0, int(_num(position))))
        cur["comment"] = (comment or "")[:_MAX_COMMENT]
        cur["changed"] = now
        await self.save()
        return True

    async def delete_user(self, uid: str) -> None:
        """Forget everything stored for a deleted account."""
        if self._ensure_loaded()["users"].pop(uid, None) is not None:
            await self.save()

    async def delete_bookmark(self, uid: str, track_id: str) -> bool:
        u = self._user(uid, create=False)
        b = (u or {}).get("bookmarks")
        if isinstance(b, dict) and b.pop(track_id, None) is not None:
            await self.save()
            return True
        return False


_state: SubsonicState | None = None


def get_state() -> SubsonicState:
    global _state
    if _state is None:
        _state = SubsonicState()
    return _state


def reset_state(path: Path | None = None) -> SubsonicState:
    """Replace the singleton (tests; or re-pointing at a new data dir)."""
    global _state
    _state = SubsonicState(path)
    return _state
