# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playlist CRUD endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from soniqboom.core.data import (
    create_playlist,
    delete_playlist,
    get_playlist,
    get_tracks_batch,
    list_playlists,
    update_playlist,
)
from soniqboom.core.store import get_store
from soniqboom.api.users import current_user, listener_uid

router = APIRouter(prefix="/playlists", tags=["playlists"])


# ── Request / response models ───────────────────────────────────────────────

class PlaylistCreate(BaseModel):
    name: str
    track_ids: list[str | dict] = []
    query: str | None = None        # non-empty ⇒ smart (auto-updating) playlist


class PlaylistUpdate(BaseModel):
    name: str | None = None
    track_ids: list[str | dict] | None = None
    query: str | None = None        # smart playlists: update the saved search
    shared: bool | None = None      # offer it to every account (owner, not readonly)


class Subscription(BaseModel):
    subscribed: bool


class TrackIds(BaseModel):
    track_ids: list[str | dict]


# ── Playlist entries ─────────────────────────────────────────────────────────
# An entry is EITHER a bare ``"<track_id>"`` string (the file / its default
# tune) OR ``{"id": "<track_id>", "subsong": N}`` for one specific subsong — N
# is the 0-based wire index (matches ?subsong=).  Bare strings keep every
# existing single-song playlist valid; subsong entries ride alongside.  Kept as
# objects (not a delimited "id#N" string) so the id-validator, (id, subsong)
# dedup, and Subsonic export all keep working on ``.id`` without parsing a token.

def _entry_id(e):
    """Base track id of an entry (bare string or {id, subsong})."""
    if isinstance(e, dict):
        return e.get("id")
    return e if isinstance(e, str) else None


def _entry_sub(e):
    """0-based subsong of an entry, or None (the default tune).  An explicit
    ``{id, subsong: 0}`` is tune 1 and stays distinct from the bare id: a
    multi-tune file's default can be another tune (its first is empty)."""
    if isinstance(e, dict):
        s = e.get("subsong")
        if isinstance(s, int) and not isinstance(s, bool) and s >= 0:
            return s
    return None


def _entry_key(e):
    """Dedup identity: (id, subsong-or-None)."""
    return (_entry_id(e), _entry_sub(e))


def _norm_entry(e):
    """Canonical stored form: bare string for the default tune, else {id, subsong}."""
    tid = _entry_id(e)
    sub = _entry_sub(e)
    return {"id": tid, "subsong": sub} if sub is not None else tid


# ── Endpoints ────────────────────────────────────────────────────────────────

# ── Playlist visibility / ownership ─────────────────────────────────────────
# Every playlist is its owner's (``owner_user_id``; ``TrackStore`` "Playlist
# ownership"): the signed-in user's list holds their own playlists and the
# shared ones of others they subscribed to (Import).  Reads: the owner, or
# anyone while it is shared; someone else's unshared playlist is a 404 —
# admins included.  Writes (rename, tracks, delete, share): the owner only;
# readonly accounts may keep their own playlists but not share them.
# Before any account exists every playlist is the no-user bucket's ("").


def _can_read(pl: dict, uid: str) -> bool:
    return get_store().playlist_readable(pl, uid)


def _can_write(pl: dict, uid: str) -> bool:
    return get_store().playlist_owner(pl) == uid


def _owner_name(owner: str) -> str:
    """The owner's user name for the shared views (the name an account signs in
    with — a display name is the user's to choose and could pose as another
    account)."""
    try:
        from soniqboom.core.users import get_user_store
        u = get_user_store().get(owner) if owner else None
    except RuntimeError:
        u = None
    return u.username if u is not None else ""


def _shown(pl: dict | None, uid: str) -> dict | None:
    """A playlist as the API returns it to ``uid``: never who imported it
    (the owner sees how many — ``subscriber_count``), and someone else's
    playlist without its owner's id (``owner_name`` instead)."""
    if pl is None:
        return None
    out = {k: v for k, v in pl.items() if k != "subscribers"}
    if get_store().playlist_owner(pl) == uid:
        out["subscriber_count"] = len(pl.get("subscribers") or ())
    else:
        out.pop("owner_user_id", None)
    return out


def _summary(p: dict, uid: str) -> dict:
    own = get_store().playlist_owner(p) == uid
    out = {
        "id": p["id"],
        "name": p["name"],
        "track_count": len(p.get("track_ids", [])),
        "query": p.get("query"),
        "smart": bool(p.get("query")),
        "shared": bool(p.get("shared")),
        "subscribed": not own,          # someone else's, imported (read-only)
        "read_only": not own,
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
    }
    if own:
        out["owner_user_id"] = p.get("owner_user_id")
        out["subscriber_count"] = len(p.get("subscribers") or ())
    else:
        out["owner_name"] = _owner_name(get_store().playlist_owner(p))
    return out


@router.get("")
async def list_all_playlists(uid: str = Depends(listener_uid)):
    """The signed-in user's playlists (summary view): their own, then the
    shared ones they imported (``subscribed``, read-only)."""
    playlists = await list_playlists(user_id=uid)
    return [_summary(p, uid) for p in playlists]


@router.post("", status_code=201)
async def create_new_playlist(body: PlaylistCreate, uid: str = Depends(listener_uid)):
    """Create a new playlist owned by the signed-in user (readonly too)."""
    playlist_id = str(uuid.uuid4())
    playlist = await create_playlist(
        body.name,
        playlist_id=playlist_id,
        track_ids=[_norm_entry(e) for e in body.track_ids if _entry_id(e)],
        owner_user_id=uid or None,
        query=(body.query or None),
    )
    return _shown(playlist, uid)


@router.get("/shared")
async def shared_playlists(uid: str = Depends(listener_uid)):
    """The playlists other accounts share — the Import list: id, name, the
    owner's name, track count, whether the caller subscribed."""
    store = get_store()
    out = []
    for p in store.shared_playlists_for(uid):
        out.append({
            "id": p["id"], "name": p["name"],
            "owner_name": _owner_name(store.playlist_owner(p)),
            "track_count": len(p.get("track_ids", [])),
            "smart": bool(p.get("query")),
            "subscribed": uid in (p.get("subscribers") or ()),
        })
    out.sort(key=lambda r: (r["owner_name"].lower(), r["name"].lower()))
    return out


@router.put("/{playlist_id}/subscription")
async def set_subscription(playlist_id: str, body: Subscription,
                           uid: str = Depends(listener_uid)):
    """Import (``subscribed`` true) a shared playlist of another account —
    a read-only link that follows its owner's edits — or remove it again."""
    if not uid:
        raise HTTPException(403, "Sign in to import playlists.")
    store = get_store()
    pl = store.get_playlist(playlist_id)
    known = pl is not None and (store.playlist_readable(pl, uid)
                                or uid in (pl.get("subscribers") or ()))
    if not known or store.set_playlist_subscription(
            playlist_id, uid, body.subscribed) is None:
        raise HTTPException(404, "Playlist not found")
    return {"id": playlist_id, "subscribed": body.subscribed}


@router.get("/{playlist_id}")
async def read_playlist(playlist_id: str, uid: str = Depends(listener_uid)):
    """Return a playlist with full track metadata.  404s if the caller
    doesn't own it and it's not shared.  ``read_only`` / ``owner_name`` for
    someone else's (shared) playlist."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, uid):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, uid):
        playlist = {**playlist, "read_only": True,
                    "owner_name": _owner_name(get_store().playlist_owner(playlist)),
                    "subscribed": uid in (playlist.get("subscribers") or ())}

    q = playlist.get("query")
    if q:
        # Smart playlist — tracks are computed live from the saved search, so it
        # auto-updates as the library grows. Same query engine as /api/search.
        from soniqboom.api.search import run_search
        results = await run_search(q, limit=500)
        tracks = [t.model_dump() if hasattr(t, "model_dump") else t for t in results]
        return {**_shown(playlist, uid), "smart": True, "tracks": _mark_offline(tracks)}

    entries = playlist.get("track_ids", [])
    ids = [_entry_id(e) for e in entries]
    base = await get_tracks_batch(ids) if ids else []
    # Resolve each entry to its track dict, in order (duplicates preserved), and
    # attach the 0-based ``subsong`` for subsong entries so the client renders
    # "Tune N".  Bare/default entries carry no subsong.
    tracks = []
    for e, t in zip(entries, base):
        if t is None:
            continue
        sub = _entry_sub(e)
        if sub is None:
            tracks.append(t)                       # bare/default — return as-is (Track or dict)
        else:
            # get_tracks_batch yields Track models; convert to a dict to attach
            # the subsong (drop the embedding vector so it never leaks/bloats).
            td = t.model_dump() if hasattr(t, "model_dump") else dict(t)
            td.pop("embedding", None)
            td["subsong"] = sub
            tracks.append(td)
    return {**_shown(playlist, uid), "tracks": _mark_offline(tracks)}


def _mark_offline(tracks: list) -> list:
    """A playlist lists every entry — "Tracks on unavailable drives: Hide"
    never drops one — but marks the ones that can't play right now
    (``offline``, Mark and Hide modes; ``core.availability``)."""
    from soniqboom.core import availability
    from soniqboom.core.store import get_store
    store = get_store()
    if availability.mode(store) == "show":
        return tracks
    snap = availability.snapshot(store)
    if not snap.dead:
        return tracks
    out = []
    for t in tracks:
        d = t if isinstance(t, dict) else None
        if d is None and hasattr(t, "model_dump"):
            kind = snap.kind_of({"id": getattr(t, "id", None),
                                 "scan_root_hash": getattr(t, "scan_root_hash", None)})
            if kind:
                d = t.model_dump()
                d.pop("embedding", None)
                d["offline"] = kind
                out.append(d)
            else:
                out.append(t)
            continue
        kind = snap.kind_of(d) if d is not None else None
        out.append({**d, "offline": kind} if kind else t)
    return out


@router.put("/{playlist_id}")
async def update_existing_playlist(
    playlist_id: str, body: PlaylistUpdate,
    uid: str = Depends(listener_uid), user=Depends(current_user),
):
    """Update playlist name, track list and/or ``shared`` (owner only;
    sharing not for readonly accounts — unsharing ends every subscription).

    When ``track_ids`` is supplied, every id is validated against the
    in-memory track store and unknown ids are pruned.  The response
    includes ``dropped_ids`` so the client can surface which tracks
    were silently removed (e.g. tracks deleted between fetch + save).
    """
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, uid):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, uid):
        raise HTTPException(403, "You can only edit your own playlists.")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    shared = updates.pop("shared", None)
    if shared is not None:
        if shared and (user is None or user.role == "readonly"):
            raise HTTPException(403, "Read-only accounts can't share playlists.")
        get_store().set_playlist_shared(playlist_id, shared)
        if not updates:
            return _shown(await get_playlist(playlist_id), uid)
    # Validate + prune unknown track ids when the caller supplies them.
    # We accept-and-prune (rather than 400) so a stale client doesn't
    # block an otherwise valid edit — but we tell the client which ids
    # were dropped so they can refresh their view.
    dropped: list = []
    if "track_ids" in updates:
        store = get_store()
        valid: list = []
        for e in updates["track_ids"]:
            tid = _entry_id(e)
            if tid and store.get_track(tid) is not None:
                valid.append(_norm_entry(e))   # keep the subsong, drop nothing valid
            else:
                dropped.append(tid or "malformed-entry")   # always a string (response shape)
        updates["track_ids"] = valid
    result = await update_playlist(playlist_id, updates)
    if result is None:
        raise HTTPException(404, "Playlist not found")
    if dropped:
        # Caller asked for a partial update — surface the dropped ids so
        # the UI can prompt for a rescan / reload.
        return {**_shown(result, uid), "dropped_ids": dropped}
    return _shown(result, uid)


@router.delete("/{playlist_id}")
async def remove_playlist(playlist_id: str, uid: str = Depends(listener_uid)):
    """Delete a playlist (owner only — a shared one goes for every
    subscriber too)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, uid):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, uid):
        raise HTTPException(403, "You can only delete your own playlists.")
    removed = await delete_playlist(playlist_id)
    if not removed:
        raise HTTPException(404, "Playlist not found")
    return {"deleted": playlist_id}


@router.post("/{playlist_id}/tracks")
async def add_tracks(playlist_id: str, body: TrackIds, uid: str = Depends(listener_uid)):
    """Append tracks to a playlist (owner only)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, uid):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, uid):
        raise HTTPException(403, "You can only edit your own playlists.")

    existing = playlist.get("track_ids", [])
    # Dedup on (id, subsong): tune 3 and tune 5 of one file are distinct entries,
    # but adding the same tune twice is a no-op.  Set membership (not ``in list``)
    # keeps this cheap for a 10K-entry playlist.
    existing_keys = {_entry_key(e) for e in existing}
    new_entries = []
    for e in body.track_ids:
        key = _entry_key(e)
        if _entry_id(e) and key not in existing_keys:
            existing_keys.add(key)
            new_entries.append(_norm_entry(e))
    merged = existing + new_entries
    result = await update_playlist(playlist_id, {"track_ids": merged})
    return _shown(result, uid)


@router.delete("/{playlist_id}/tracks")
async def remove_tracks(playlist_id: str, body: TrackIds, uid: str = Depends(listener_uid)):
    """Remove tracks from a playlist (owner only)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, uid):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, uid):
        raise HTTPException(403, "You can only edit your own playlists.")

    existing = playlist.get("track_ids", [])
    drop = {_entry_key(e) for e in body.track_ids}
    filtered = [e for e in existing if _entry_key(e) not in drop]
    result = await update_playlist(playlist_id, {"track_ids": filtered})
    return _shown(result, uid)
