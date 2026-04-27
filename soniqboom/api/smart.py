# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Smart library views, duplicate management, and listening history.

Endpoints
─────────
Smart playlists (curated views based on play stats / ratings):
    GET /smart/most-played      Tracks sorted by play count (desc)
    GET /smart/recently-added   Tracks sorted by added_at (desc)
    GET /smart/unplayed         Tracks with zero plays
    GET /smart/top-rated        Tracks sorted by star rating (desc)
    GET /smart/history          Listening history (chronological log)

Duplicate management (format-variant deduplication):
    GET  /smart/duplicates                 All duplicate groups
    GET  /smart/duplicates/{group_id}      Tracks in one group
    POST /smart/duplicates/recompute       Force re-detection
    POST /smart/duplicates/{group_id}/primary   Set primary track
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Query

from soniqboom.core.data import (
    get_all_play_stats, get_all_ratings, get_track,
)
from soniqboom.core.store import get_store
from soniqboom.models.track import TrackMeta

log = logging.getLogger(__name__)
router = APIRouter(tags=["smart"])

# ── History settings ──────────────────────────────────────────────────────────
HISTORY_MAX = 500                       # cap to avoid unbounded growth


# ── Helper: fetch all tracks as dicts ────────────────────────────────────────

async def _all_tracks_meta() -> list[dict]:
    """Return every track in the library as a list of dicts."""
    from soniqboom.core.data import scan_all_tracks_meta
    metas = await scan_all_tracks_meta()
    return [t.model_dump() for t in metas]


# ══════════════════════════════════════════════════════════════════════════════
#  SMART PLAYLIST VIEWS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/smart/most-played")
async def most_played(limit: int = Query(100, ge=1, le=500)):
    """Return tracks sorted by play count (most → least)."""
    all_stats = await get_all_play_stats()
    if not all_stats:
        return []

    # Sort by count descending, then by last_played descending
    ranked_ids = sorted(
        all_stats.keys(),
        key=lambda tid: (-all_stats[tid].get("count", 0),
                         -all_stats[tid].get("last_played", 0)),
    )[:limit]

    return await _enrich_tracks(ranked_ids, stats=all_stats)


@router.get("/smart/recently-added")
async def recently_added(limit: int = Query(100, ge=1, le=500)):
    """Return the most recently added tracks (by added_at timestamp)."""
    return get_store().recently_added(limit)


@router.get("/smart/unplayed")
async def unplayed(limit: int = Query(100, ge=1, le=500)):
    """Return tracks that have never been played."""
    all_stats = await get_all_play_stats()
    played_ids = set(all_stats.keys())

    # Fetch all tracks, filter out those with play stats
    all_tracks = await _all_tracks_meta()
    unplayed_list = [t for t in all_tracks if t["id"] not in played_ids]

    # Sort by added_at descending (newest first)
    unplayed_list.sort(key=lambda t: t.get("added_at", 0), reverse=True)
    return unplayed_list[:limit]


@router.get("/smart/top-rated")
async def top_rated(limit: int = Query(100, ge=1, le=500)):
    """Return tracks sorted by star rating (highest first)."""
    all_ratings = await get_all_ratings()
    if not all_ratings:
        return []

    # Only include tracks with rating >= 1
    rated_ids = sorted(
        (tid for tid, r in all_ratings.items() if r >= 1),
        key=lambda tid: -all_ratings[tid],
    )[:limit]

    return await _enrich_tracks(rated_ids, ratings=all_ratings)


# ══════════════════════════════════════════════════════════════════════════════
#  LISTENING HISTORY
# ══════════════════════════════════════════════════════════════════════════════

async def push_history(track_id: str, title: str = "", artist: str = "") -> None:
    """Append a play event to the listening history.

    Called from the mark_played() endpoint in tracks.py.
    """
    get_store().push_history({
        "track_id": track_id,
        "title": title,
        "artist": artist,
        "ts": int(time.time()),
    })


@router.get("/smart/history")
async def listening_history(limit: int = Query(50, ge=1, le=200)):
    """Return recent listening history (newest first)."""
    return get_store().get_history(limit)


# ══════════════════════════════════════════════════════════════════════════════
#  DUPLICATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/smart/duplicates")
async def list_duplicate_groups(limit: int = Query(100, ge=1, le=500)):
    """Return all duplicate groups with their member tracks.

    Response format: [
        {
            "group_id": "abc123...",
            "primary_id": "track-uuid",
            "count": 3,
            "tracks": [ { TrackMeta fields + format_score }, ... ]
        },
        ...
    ]
    """
    from soniqboom.core.duplicates import compute_duplicate_groups, format_quality_score

    all_tracks = await _all_tracks_meta()
    annotations = compute_duplicate_groups(all_tracks)

    # Build groups dict
    groups: dict[str, list[dict]] = {}
    for t in all_tracks:
        tid = t["id"]
        ann = annotations.get(tid, {})
        gid = ann.get("duplicate_group_id")
        if gid:
            t["format_score"] = ann.get("format_score", 0)
            t["is_duplicate_primary"] = ann.get("is_duplicate_primary", False)
            groups.setdefault(gid, []).append(t)

    # Convert to response list, sorted by group size descending
    result = []
    for gid, tracks in sorted(groups.items(), key=lambda x: -len(x[1])):
        primary = next((t for t in tracks if t.get("is_duplicate_primary")), tracks[0])
        # Sort within group: primary first, then by format_score descending
        tracks.sort(key=lambda t: (-t.get("is_duplicate_primary", False),
                                   -t.get("format_score", 0)))
        result.append({
            "group_id": gid,
            "primary_id": primary["id"],
            "count": len(tracks),
            "tracks": tracks,
        })

    return result[:limit]


@router.get("/smart/duplicates/{group_id}")
async def get_duplicate_group(group_id: str):
    """Return all tracks in a specific duplicate group."""
    from soniqboom.core.duplicates import compute_duplicate_groups

    all_tracks = await _all_tracks_meta()
    annotations = compute_duplicate_groups(all_tracks)

    tracks_in_group = []
    for t in all_tracks:
        ann = annotations.get(t["id"], {})
        if ann.get("duplicate_group_id") == group_id:
            t["format_score"] = ann.get("format_score", 0)
            t["is_duplicate_primary"] = ann.get("is_duplicate_primary", False)
            tracks_in_group.append(t)

    if not tracks_in_group:
        raise HTTPException(404, "Duplicate group not found")

    return tracks_in_group


@router.post("/smart/duplicates/recompute")
async def recompute_duplicates():
    """Force recomputation of duplicate groups and store annotations."""
    from soniqboom.core.duplicates import compute_duplicate_groups

    all_tracks = await _all_tracks_meta()
    annotations = compute_duplicate_groups(all_tracks)

    store = get_store()
    updated = 0
    for tid, ann in annotations.items():
        store.update_track_fields(tid, {
            "duplicate_group_id": ann["duplicate_group_id"],
            "format_score": ann["format_score"],
            "is_duplicate_primary": ann["is_duplicate_primary"],
        })
        updated += 1

    log.info("Duplicate recompute: annotated %d tracks", updated)
    return {"updated": updated, "groups": sum(
        1 for a in annotations.values() if a["duplicate_group_id"] is not None
    ) // 2}


@router.post("/smart/duplicates/{group_id}/primary")
async def set_group_primary(group_id: str, track_id: str):
    """Override which track is the primary in a duplicate group."""
    from soniqboom.core.duplicates import compute_duplicate_groups

    all_tracks = await _all_tracks_meta()
    annotations = compute_duplicate_groups(all_tracks)

    group_tids = [
        tid for tid, ann in annotations.items()
        if ann.get("duplicate_group_id") == group_id
    ]
    if not group_tids:
        raise HTTPException(404, "Group not found")
    if track_id not in group_tids:
        raise HTTPException(400, "Track is not in this duplicate group")

    store = get_store()
    for tid in group_tids:
        store.update_track_fields(tid, {"is_duplicate_primary": tid == track_id})

    return {"group_id": group_id, "primary_id": track_id}


# ── Helper utilities ─────────────────────────────────────────────────────────

async def _enrich_tracks(
    track_ids: list[str],
    stats: dict | None = None,
    ratings: dict | None = None,
) -> list[dict]:
    """Fetch full TrackMeta for a list of IDs and optionally merge stats/ratings."""
    from soniqboom.core.data import get_tracks_batch
    tracks = await get_tracks_batch(track_ids)
    result = []
    for tid, t in zip(track_ids, tracks):
        if t is None:
            continue
        d = t.model_dump()
        d.pop("embedding", None)
        if stats and tid in stats:
            d["play_count"] = stats[tid].get("count", 0)
            d["last_played"] = stats[tid].get("last_played")
        if ratings and tid in ratings:
            d["rating"] = ratings[tid]
        result.append(d)
    return result


