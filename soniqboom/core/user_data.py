# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-user listening data: play counts and listening history.

Each account has its own play stats (``{track id: {count, last_played}}``)
and history (a list of ``{track_id, title, artist, ts}`` in play order,
capped at ``HISTORY_MAX``), keyed by the account's id; ``NO_USER`` (``""``)
holds what was played while no account existed.  Track ratings are not
per user.

The rules here are shared by the live store (``TrackStore``) and AOF replay
/ merging (``merger._apply_entry``) so the two can't disagree.  Import-light
on purpose: the merger process uses it.
"""
from __future__ import annotations

import bisect

# The listening history kept per user (the oldest entries drop out).
HISTORY_MAX = 500

# The bucket of plays made while no account existed.
NO_USER = ""

# Store config key: the account the data of the unversioned (V1) database and
# the ``NO_USER`` bucket went to.
OWNER_CONFIG_KEY = "user_data_owner"

# Store config key: ``{bucket key: user name}`` — the data of a restored
# backup's users that have no account here yet, kept under ``PENDING_PREFIX``
# + their id on the backed-up server (no account id has that form, so nobody
# sees it) until an account of that name exists (``claim_pending``).  A
# name of None: not known (an account deleted before the backup) — kept.
PENDING_CONFIG_KEY = "pending_user_data"
PENDING_PREFIX = "import:"


def add_play(stats: dict, track_id: str, ts: int) -> dict:
    """Count one play of ``track_id`` at ``ts`` in one user's ``stats`` map
    (in place) and return the track's entry.  A back-dated play never moves
    ``last_played`` backwards."""
    existing = stats.get(track_id)
    if existing:
        existing["count"] = existing.get("count", 0) + 1
        existing["last_played"] = max(existing.get("last_played", 0) or 0, ts)
        return existing
    entry = {"count": 1, "last_played": ts}
    stats[track_id] = entry
    return entry


def add_history(history: list, entry: dict) -> None:
    """Add ``entry`` to one user's ``history`` list in place — in PLAY order,
    not arrival order (an offline client's back-dated scrobble lands where it
    was played) — and drop the oldest beyond ``HISTORY_MAX``."""
    ts = (entry or {}).get("ts") or 0
    if history and ts < (history[-1].get("ts") or 0):
        i = bisect.bisect_right([e.get("ts") or 0 for e in history], ts)
        history.insert(i, entry)
    else:
        history.append(entry)
    if len(history) > HISTORY_MAX:
        del history[: -HISTORY_MAX]


def merge_user_data(play_stats: dict, history: dict, src: str, dst: str) -> bool:
    """Move user ``src``'s play stats and history to user ``dst`` (both maps
    keyed by user id, changed in place): counts add up, ``last_played`` is the
    later one, history entries merge in play order (capped).  Nested values
    are replaced, never edited in place (AOF replay rolls a failed record back
    from shallow copies).  True when anything moved."""
    if src == dst:
        return False
    moved = False
    src_stats = play_stats.pop(src, None)
    if src_stats:
        dst_stats = dict(play_stats.get(dst) or {})
        for tid, st in src_stats.items():
            have = dst_stats.get(tid)
            if have:
                dst_stats[tid] = {
                    "count": (have.get("count", 0) or 0) + (st.get("count", 0) or 0),
                    "last_played": max(have.get("last_played", 0) or 0,
                                       st.get("last_played", 0) or 0),
                }
            else:
                dst_stats[tid] = dict(st)
        play_stats[dst] = dst_stats
        moved = True
    src_hist = history.pop(src, None)
    if src_hist:
        merged = sorted([*(history.get(dst) or ()), *src_hist],
                        key=lambda e: (e.get("ts") or 0) if isinstance(e, dict) else 0)
        history[dst] = merged[-HISTORY_MAX:]
        moved = True
    return moved


def drop_user_data(play_stats: dict, history: dict, uid: str) -> dict:
    """Remove user ``uid``'s play stats and history (in place).  Returns how
    many tracks / history entries went."""
    stats = play_stats.pop(uid, None) or {}
    hist = history.pop(uid, None) or []
    return {"play_stats": len(stats), "history": len(hist)}


def move_everything(store, src: str, dst: str) -> bool:
    """All of user ``src``'s own data goes to ``dst``: plays and history
    (``TrackStore.move_user_data``), playlists (``TrackStore.reassign_playlists``)
    and favourite stations (``radiodir.move_favorites``).  True when
    anything moved."""
    moved = store.move_user_data(src, dst)
    reassign = getattr(store, "reassign_playlists", None)
    if callable(reassign):
        moved = bool(reassign(src, dst)) or moved
    try:
        from soniqboom.core import radiodir
        moved = radiodir.move_favorites(src, dst) or moved
    except Exception:                                   # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "The favourite stations of %r could not be moved to %r", src, dst, exc_info=True)
    return moved


def claim_unowned(store, user_store) -> bool:
    """Once accounts exist, the legacy data goes to the legacy owner (made
    the recorded ``user_data_owner``): the recorded
    ``user_data_owner`` while it names an account, else
    ``users.legacy_owner_id`` (the oldest enabled admin; no admin: the oldest
    account).  Moved to it (``move_everything``: plays, history, playlists,
    favourite stations): what was made with no account, and — when the
    recorded owner names no account any more (a users.json restored or made
    anew: the ids changed) — that former owner's data.  Another
    vanished id's data is not moved (a deleted account's was dropped; an
    imported one waits for its user name — ``claim_pending``).  Called at a
    start once the accounts are loaded and whenever accounts appear.  Runs on
    the event loop (store writes).  True when data moved."""
    users = user_store.list_users()
    if not users:
        return False
    ids = {u.id for u in users}
    recorded = store.user_data_owner()
    owner = recorded
    moved = False
    if owner not in ids:
        from soniqboom.core.users import legacy_owner_id
        owner = legacy_owner_id(users)
        # (not the data of an owner being deleted: that goes with the account)
        if recorded and owner and recorded != owner and not store.user_deleted(recorded):
            moved = move_everything(store, recorded, owner)
    if not owner:
        return moved
    if store.user_data_owner() != owner:
        store.set_config(OWNER_CONFIG_KEY, owner)
    return move_everything(store, NO_USER, owner) or moved


def claim_accounts(users) -> dict:
    """``{lower-cased user name: account id}`` of the accounts kept-aside or
    restored per-user data may attach to by name: all but self-registered
    ones (``User.self_registered``).  ``users``: ``User`` objects or stored
    user records."""
    out = {}
    for u in users or ():
        get = u.get if isinstance(u, dict) else (lambda k, d=None, _u=u: getattr(_u, k, d))
        name, uid = get("username"), get("id")
        if isinstance(name, str) and isinstance(uid, str) and get("self_registered") is not True:
            out[name.lower()] = uid
    return out


def claim_pending(store, user_store) -> bool:
    """A restored backup's data kept aside for a user with no account here
    (``PENDING_CONFIG_KEY``, ``persistence.import_backup_state``) goes to the
    account of that user name (case-insensitive) once there is one that an
    admin created (or that existed) — never a self-registered one
    (``User.self_registered``: anyone may register a free name).  Each move
    one AOF record, the pending list updated.  Runs on the event loop.  True
    when data moved."""
    pending = store.get_config(PENDING_CONFIG_KEY)
    if not isinstance(pending, dict) or not pending:
        return False
    by_name = claim_accounts(user_store.list_users())
    left = dict(pending)
    moved = False
    for key, name in pending.items():
        uid = by_name.get(name.lower()) if isinstance(name, str) else None
        if uid is None:
            continue
        moved = move_everything(store, key, uid) or moved
        left.pop(key, None)
    if left != pending:
        if left:
            store.set_config(PENDING_CONFIG_KEY, left)
        else:
            store.delete_config(PENDING_CONFIG_KEY)
    return moved
