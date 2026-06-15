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
    track_ids: list[str] = []
    query: str | None = None        # non-empty ⇒ smart (auto-updating) playlist


class PlaylistUpdate(BaseModel):
    name: str | None = None
    track_ids: list[str] | None = None
    query: str | None = None        # smart playlists: update the saved search


class TrackIds(BaseModel):
    track_ids: list[str]


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
        track_ids=body.track_ids,
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

    track_ids = playlist.get("track_ids", [])
    tracks = await get_tracks_batch(track_ids) if track_ids else []
    return {
        **playlist,
        "tracks": [t for t in tracks if t is not None],
    }


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
    dropped: list[str] = []
    if "track_ids" in updates:
        store = get_store()
        valid: list[str] = []
        for tid in updates["track_ids"]:
            if store.get_track(tid) is not None:
                valid.append(tid)
            else:
                dropped.append(tid)
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
    # Set membership instead of ``not in list`` — for a 10K-track playlist
    # the previous O(N·M) scan became noticeable when adding many tracks.
    existing_set = set(existing)
    merged = existing + [tid for tid in body.track_ids if tid not in existing_set]
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
    drop = set(body.track_ids)
    filtered = [tid for tid in existing if tid not in drop]
    result = await update_playlist(playlist_id, {"track_ids": filtered})
    return result
