# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playlist CRUD endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException
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
from soniqboom.api.users import current_user, require_user

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
    """0-based subsong of an entry, or None (default tune).  subsong<=0 collapses
    to None, so ``{id, subsong: 0}`` is the same audio as the bare default."""
    if isinstance(e, dict):
        s = e.get("subsong")
        if isinstance(s, int) and s > 0:
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
# All playlist endpoints honour ``owner_user_id``:
#   * a signed-in user sees their own + any legacy (no-owner) playlists.
#   * reads return 404 for someone else's playlist (don't leak existence).
#   * writes require ownership OR admin role.
# Pre-bootstrap installs (no users at all) keep the old "everything shared"
# behaviour so single-tenant installs aren't disrupted on upgrade.


def _can_read(pl: dict, user) -> bool:
    owner = pl.get("owner_user_id")
    if owner is None:
        return True
    if user is None:
        return False
    return owner == user.id or user.role == "admin"


def _can_write(pl: dict, user) -> bool:
    if user is None:
        return False
    if user.role == "readonly":
        return False
    owner = pl.get("owner_user_id")
    if owner is None:
        return True  # legacy/shared playlists editable by any non-readonly
    return owner == user.id or user.role == "admin"


@router.get("")
async def list_all_playlists(user = Depends(current_user)):
    """Return playlists visible to the signed-in user (summary view)."""
    playlists = await list_playlists(user_id=user.id if user else None)
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "track_count": len(p.get("track_ids", [])),
            "query": p.get("query"),
            "smart": bool(p.get("query")),
            "owner_user_id": p.get("owner_user_id"),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
        }
        for p in playlists
    ]


@router.post("", status_code=201)
async def create_new_playlist(body: PlaylistCreate, user = Depends(require_user)):
    """Create a new playlist owned by the signed-in user."""
    if user.role == "readonly":
        raise HTTPException(
            403,
            "Your account is read-only. Ask an admin to upgrade you to 'edit' "
            "to create playlists.",
        )
    playlist_id = str(uuid.uuid4())
    playlist = await create_playlist(
        body.name,
        playlist_id=playlist_id,
        track_ids=[_norm_entry(e) for e in body.track_ids if _entry_id(e)],
        owner_user_id=user.id,
        query=(body.query or None),
    )
    return playlist


@router.get("/{playlist_id}")
async def read_playlist(playlist_id: str, user = Depends(current_user)):
    """Return a playlist with full track metadata.  404s if the caller
    doesn't own it (and it's not shared)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, user):
        raise HTTPException(404, "Playlist not found")

    q = playlist.get("query")
    if q:
        # Smart playlist — tracks are computed live from the saved search, so it
        # auto-updates as the library grows. Same query engine as /api/search.
        from soniqboom.api.search import run_search
        results = await run_search(q, limit=500)
        tracks = [t.model_dump() if hasattr(t, "model_dump") else t for t in results]
        return {**playlist, "smart": True, "tracks": tracks}

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
    return {**playlist, "tracks": tracks}


@router.put("/{playlist_id}")
async def update_existing_playlist(
    playlist_id: str, body: PlaylistUpdate,
    user = Depends(require_user),
):
    """Update playlist name and/or track list (owner or admin only).

    When ``track_ids`` is supplied, every id is validated against the
    in-memory track store and unknown ids are pruned.  The response
    includes ``dropped_ids`` so the client can surface which tracks
    were silently removed (e.g. tracks deleted between fetch + save).
    """
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, user):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, user):
        raise HTTPException(403, "You can only edit your own playlists.")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
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
        return {**result, "dropped_ids": dropped}
    return result


@router.delete("/{playlist_id}")
async def remove_playlist(playlist_id: str, user = Depends(require_user)):
    """Delete a playlist (owner or admin only)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, user):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, user):
        raise HTTPException(403, "You can only delete your own playlists.")
    removed = await delete_playlist(playlist_id)
    if not removed:
        raise HTTPException(404, "Playlist not found")
    return {"deleted": playlist_id}


@router.post("/{playlist_id}/tracks")
async def add_tracks(playlist_id: str, body: TrackIds, user = Depends(require_user)):
    """Append tracks to a playlist (owner or admin only)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, user):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, user):
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
    return result


@router.delete("/{playlist_id}/tracks")
async def remove_tracks(playlist_id: str, body: TrackIds, user = Depends(require_user)):
    """Remove tracks from a playlist (owner or admin only)."""
    playlist = await get_playlist(playlist_id)
    if not playlist or not _can_read(playlist, user):
        raise HTTPException(404, "Playlist not found")
    if not _can_write(playlist, user):
        raise HTTPException(403, "You can only edit your own playlists.")

    existing = playlist.get("track_ids", [])
    drop = {_entry_key(e) for e in body.track_ids}
    filtered = [e for e in existing if _entry_key(e) not in drop]
    result = await update_playlist(playlist_id, {"track_ids": filtered})
    return result
