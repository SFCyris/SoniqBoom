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

import asyncio
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
    """Return tracks that have never been played.

    Backed by the store's incrementally-maintained ``_unplayed_ids`` set —
    we walk ``_sorted_added_at`` in reverse and stop at ``limit`` matches
    rather than scanning every track in the library (previously O(N) per
    call, even for tiny pages).
    """
    return get_store().list_unplayed(limit)


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
#  INSTANT MIX  (heuristic "song radio")
# ══════════════════════════════════════════════════════════════════════════════

def _as_dict(t) -> dict | None:
    if t is None:
        return None
    if hasattr(t, "model_dump"):
        return t.model_dump()
    return dict(t)


@router.get("/smart/radio")
async def instant_mix(
    seed: str = Query(..., description="Seed track id to build the mix around"),
    limit: int = Query(60, ge=5, le=200),
):
    """Build an endless, varied queue around a seed track.

    Returns the seed first, followed by up to ``limit`` tracks chosen by genre /
    artist / era / tempo / rating / format affinity, artist-diversified. Pure
    in-memory; scoring runs off the event loop.
    """
    seed_track = await get_track(seed)
    seed_dict = _as_dict(seed_track)
    if seed_dict is None:
        raise HTTPException(404, "Seed track not found")

    # Score over the store's raw in-RAM dicts directly. Converting all ~170K
    # tracks to TrackMeta and back (``_all_tracks_meta``) cost ~6.7 s/request;
    # the radio only reads a handful of fields, so we read the live dicts and
    # copy just the selected ~60 on the way out (never mutating the store).
    store = get_store()
    candidates = store.all_tracks()
    ratings = await get_all_ratings()
    try:
        history = store.get_history(40) or []
    except Exception:
        history = []
    recent_ids = [h.get("track_id") for h in history if isinstance(h, dict) and h.get("track_id")]

    from soniqboom.core.radio import build_instant_mix
    mix = await asyncio.to_thread(
        build_instant_mix,
        seed_dict,
        candidates,
        ratings=ratings or {},
        recent_ids=recent_ids,
        limit=limit,
    )

    def _clean(t: dict) -> dict:
        d = dict(t)              # copy — candidates are live store references
        d.pop("embedding", None)
        return d

    return [_clean(seed_dict), *(_clean(t) for t in mix)]


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

# Cache the (tracks, annotations) pair so back-to-back ``/smart/duplicates``
# requests (the UI hits the list, then group-detail, then sometimes again on
# pagination) don't re-scan every track each time.
#
# The cache is keyed on the store's mutation sequence number — bumped on
# every track upsert / update / delete — so a rescan or retag that doesn't
# change the *count* still invalidates the cache.  Callers always receive
# a deep copy of the cached tracks so they can't mutate the cached pair
# in-place (``format_score`` / ``is_duplicate_primary`` injection used to
# leak across calls).
import copy as _copy

_DUP_CACHE: dict[str, object] = {"seq": -1, "count": -1, "tracks": None, "annotations": None}


def _store_seq() -> int:
    """Mutation sequence number from the store; ``-1`` if not exposed yet."""
    try:
        from soniqboom.core.store import get_store
        return int(getattr(get_store(), "_mutation_seq", -1))
    except Exception:
        return -1


async def _dup_snapshot():
    """Return ``(all_tracks, annotations)`` reusing a cached pair when valid.

    Always returns *fresh copies* — the cache stores reference originals,
    the caller gets a deep clone so per-request annotation injection can't
    leak into subsequent calls.
    """
    from soniqboom.core.duplicates import compute_duplicate_groups

    all_tracks = await _all_tracks_meta()
    count = len(all_tracks)
    seq = _store_seq()
    cached_seq = _DUP_CACHE.get("seq", -1)
    cached_count = _DUP_CACHE.get("count", -1)
    cached_tracks = _DUP_CACHE.get("tracks")
    cached_anno = _DUP_CACHE.get("annotations")
    if (
        cached_seq == seq
        and cached_count == count
        and cached_tracks is not None
        and cached_anno is not None
    ):
        # Shallow-copy the *list* of track dicts but reuse the dict
        # references; the caller's per-track ``format_score`` /
        # ``is_duplicate_primary`` injection still leaks across calls —
        # so we also restore the original keys on a tracked copy of each
        # mutated dict.  Full ``deepcopy`` of a 170K-track list was
        # ~150–300 ms blocking on the event loop (Perf #1).
        track_copies = [dict(t) for t in cached_tracks]
        anno_copies = {k: dict(v) for k, v in cached_anno.items()}
        return track_copies, anno_copies

    annotations = compute_duplicate_groups(all_tracks)
    # Stash the originals; future callers get their own per-dict copies.
    _DUP_CACHE["seq"] = seq
    _DUP_CACHE["count"] = count
    _DUP_CACHE["tracks"] = all_tracks
    _DUP_CACHE["annotations"] = annotations
    track_copies = [dict(t) for t in all_tracks]
    anno_copies = {k: dict(v) for k, v in annotations.items()}
    return track_copies, anno_copies


def _invalidate_dup_cache() -> None:
    _DUP_CACHE["seq"] = -1
    _DUP_CACHE["count"] = -1
    _DUP_CACHE["tracks"] = None
    _DUP_CACHE["annotations"] = None


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
    from soniqboom.core.duplicates import format_quality_score  # noqa: F401

    all_tracks, annotations = await _dup_snapshot()

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
    all_tracks, annotations = await _dup_snapshot()

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
    # Bulk-apply via a single AOF record instead of 170K individual ones —
    # Perf #1 caught the journal blow-up that starved play/rating writes
    # for the duration of a recompute.
    items = [
        (tid, {
            "duplicate_group_id": ann["duplicate_group_id"],
            "format_score": ann["format_score"],
            "is_duplicate_primary": ann["is_duplicate_primary"],
        })
        for tid, ann in annotations.items()
    ]
    updated = store.update_track_fields_batch(items)

    log.info("Duplicate recompute: annotated %d tracks", updated)
    # Recompute invalidates the cached snapshot so the next
    # ``/smart/duplicates`` hit reflects the fresh annotations.
    _invalidate_dup_cache()
    # Count distinct group ids — the previous ``// 2`` assumed pairs and
    # under-counted (or over-counted) any group with ≥3 members.
    distinct_groups = {
        a["duplicate_group_id"] for a in annotations.values()
        if a["duplicate_group_id"] is not None
    }
    return {"updated": updated, "groups": len(distinct_groups)}


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

    # Annotations changed but the track count didn't — invalidate the cache
    # explicitly so the next ``/smart/duplicates`` hit reflects the new
    # primary instead of serving the previous snapshot.
    _invalidate_dup_cache()

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


