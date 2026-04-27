# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playlist CRUD endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from soniqboom.core.data import (
    create_playlist,
    delete_playlist,
    get_playlist,
    get_tracks_batch,
    list_playlists,
    update_playlist,
)

router = APIRouter(prefix="/playlists", tags=["playlists"])


# ── Request / response models ───────────────────────────────────────────────

class PlaylistCreate(BaseModel):
    name: str
    track_ids: list[str] = []


class PlaylistUpdate(BaseModel):
    name: str | None = None
    track_ids: list[str] | None = None


class TrackIds(BaseModel):
    track_ids: list[str]


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
async def list_all_playlists():
    """Return all playlists (summary view)."""
    playlists = await list_playlists()
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "track_count": len(p.get("track_ids", [])),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
        }
        for p in playlists
    ]


@router.post("", status_code=201)
async def create_new_playlist(body: PlaylistCreate):
    """Create a new playlist with an auto-generated UUID."""
    playlist_id = str(uuid.uuid4())
    playlist = await create_playlist(playlist_id, body.name, body.track_ids)
    return playlist


@router.get("/{playlist_id}")
async def read_playlist(playlist_id: str):
    """Return a playlist with full track metadata."""
    playlist = await get_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")

    track_ids = playlist.get("track_ids", [])
    tracks = await get_tracks_batch(track_ids) if track_ids else []
    return {
        **playlist,
        "tracks": [t for t in tracks if t is not None],
    }


@router.put("/{playlist_id}")
async def update_existing_playlist(playlist_id: str, body: PlaylistUpdate):
    """Update playlist name and/or track list."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    result = await update_playlist(playlist_id, updates)
    if result is None:
        raise HTTPException(404, "Playlist not found")
    return result


@router.delete("/{playlist_id}")
async def remove_playlist(playlist_id: str):
    """Delete a playlist."""
    removed = await delete_playlist(playlist_id)
    if not removed:
        raise HTTPException(404, "Playlist not found")
    return {"deleted": playlist_id}


@router.post("/{playlist_id}/tracks")
async def add_tracks(playlist_id: str, body: TrackIds):
    """Append tracks to a playlist."""
    playlist = await get_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")

    existing = playlist.get("track_ids", [])
    merged = existing + [tid for tid in body.track_ids if tid not in existing]
    result = await update_playlist(playlist_id, {"track_ids": merged})
    return result


@router.delete("/{playlist_id}/tracks")
async def remove_tracks(playlist_id: str, body: TrackIds):
    """Remove tracks from a playlist."""
    playlist = await get_playlist(playlist_id)
    if not playlist:
        raise HTTPException(404, "Playlist not found")

    existing = playlist.get("track_ids", [])
    filtered = [tid for tid in existing if tid not in body.track_ids]
    result = await update_playlist(playlist_id, {"track_ids": filtered})
    return result
