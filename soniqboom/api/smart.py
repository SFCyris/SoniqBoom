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


# ══════════════════════════════════════════════════════════════════════════════
#  SMART PLAYLIST VIEWS
# ══════════════════════════════════════════════════════════════════════════════

# Ranked-list memos for /smart/most-played + /smart/top-rated.  The full sorted
# id list is reused across requests until a play / rating actually changes — keyed
# on the store's _play_seq / _rating_seq (which record_play / set_rating bump; they
# do NOT touch _mutation_seq).  Sized by the played/rated subset, not the library.
_MOST_PLAYED_MEMO: dict[str, object] = {"seq": -1, "ranked": []}
_TOP_RATED_MEMO: dict[str, object] = {"seq": -1, "ranked": []}


@router.get("/smart/most-played")
async def most_played(limit: int = Query(100, ge=1, le=500)):
    """Return tracks sorted by play count (most → least)."""
    seq = get_store()._play_seq
    all_stats = await get_all_play_stats()
    if not all_stats:
        return []
    if _MOST_PLAYED_MEMO["seq"] != seq:
        # Sort by count descending, then by last_played descending.
        _MOST_PLAYED_MEMO["ranked"] = sorted(
            all_stats.keys(),
            key=lambda tid: (-all_stats[tid].get("count", 0),
                             -all_stats[tid].get("last_played", 0)),
        )
        _MOST_PLAYED_MEMO["seq"] = seq
    ranked_ids = _MOST_PLAYED_MEMO["ranked"][:limit]
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
    seq = get_store()._rating_seq
    all_ratings = await get_all_ratings()
    if not all_ratings:
        return []
    if _TOP_RATED_MEMO["seq"] != seq:
        # Only include tracks with rating >= 1, highest first.
        _TOP_RATED_MEMO["ranked"] = sorted(
            (tid for tid, r in all_ratings.items() if r >= 1),
            key=lambda tid: -all_ratings[tid],
        )
        _TOP_RATED_MEMO["seq"] = seq
    rated_ids = _TOP_RATED_MEMO["ranked"][:limit]
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

# Duplicate-group views are served from the store's maintained ``_tag_dup_group``
# index ({group_id -> member track_ids}) plus the PERSISTED per-track annotation
# fields (``duplicate_group_id`` / ``is_duplicate_primary`` / ``format_score``).
# That index is reconstructed from the AOF on boot, so these views never recompute
# groups over the whole library on a request — they read the saved index.
#
# Freshness: a single-flight background recompute fires whenever the library has
# mutated since the last one (``_dup_seq`` tracks the ``_mutation_seq`` at that
# point).  The persisted index is served instantly meanwhile (eventually
# consistent); only a never-computed library blocks for one build.  The recompute
# diffs against the current annotations and persists just the changed tracks in a
# single batched AOF record, off the event loop.

_dup_seq: int = -1                          # _mutation_seq as of the last completed recompute
_dup_task: "asyncio.Task | None" = None     # single-flight recompute task
_PUBLIC_META_FIELDS = set(TrackMeta.model_fields)


def _compute_dup_changes(raw_tracks: list[dict]) -> list[tuple[str, dict]]:
    """Off-loop: recompute duplicate groups over a snapshot of raw store dicts and
    return ONLY the ``(track_id, fields)`` whose annotation changed, so the persist
    step writes a minimal batch (and re-indexes only what moved).
    ``compute_duplicate_groups`` reads title/artist/duration/format/bitrate — all
    present on the raw dicts — so no Pydantic construction is needed."""
    from soniqboom.core.duplicates import compute_duplicate_groups
    annotations = compute_duplicate_groups(raw_tracks)
    cur = {t["id"]: t for t in raw_tracks if t.get("id")}
    changes: list[tuple[str, dict]] = []
    for tid, a in annotations.items():
        c = cur.get(tid)
        if c is None:
            continue
        if (c.get("duplicate_group_id") != a["duplicate_group_id"]
                or bool(c.get("is_duplicate_primary", True)) != bool(a["is_duplicate_primary"])
                or int(c.get("format_score") or 0) != int(a["format_score"])):
            changes.append((tid, {
                "duplicate_group_id": a["duplicate_group_id"],
                "format_score": a["format_score"],
                "is_duplicate_primary": a["is_duplicate_primary"],
            }))
    return changes


async def _do_dup_recompute() -> int:
    """Recompute + persist duplicate annotations.  Snapshot on the loop, compute +
    diff OFF the loop, then persist the (minimal) changed set in ONE batched AOF
    record — which also updates the store's ``_tag_dup_group`` index.  Returns the
    number of tracks whose annotation changed."""
    global _dup_seq
    store = get_store()
    snap_seq = store._mutation_seq
    try:
        # Snapshot only the fields the grouping + diff + primary tiebreak need
        # (incl. ``added_at`` — _pick_primary's final tiebreaker), not the full
        # ~30-field dicts, so the loop-side copy of 270k tracks is as small as
        # possible before threading.
        raw_snapshot = [{
            "id": d.get("id"), "title": d.get("title"),
            "artist": d.get("artist"), "album_artist": d.get("album_artist"),
            "duration": d.get("duration"), "format": d.get("format"),
            "bitrate": d.get("bitrate"), "added_at": d.get("added_at"),
            "duplicate_group_id": d.get("duplicate_group_id"),
            "is_duplicate_primary": d.get("is_duplicate_primary"),
            "format_score": d.get("format_score"),
        } for d in store.all_tracks()]
        changes = await asyncio.to_thread(_compute_dup_changes, raw_snapshot)
        # Did a REAL mutation interleave during the off-loop compute?  (Our own
        # batch write below is synchronous, so it's excluded from this check.)
        interleaved = store._mutation_seq != snap_seq
        if changes:
            store.update_track_fields_batch(changes)
        # Mark fresh as of the snapshot.  If a real change interleaved during the
        # compute, leave _dup_seq at snap_seq so the next request recomputes once
        # and converges — never silently dropping that change.
        _dup_seq = snap_seq if interleaved else store._mutation_seq
        return len(changes)
    except Exception:
        # Never let a failed recompute storm every request: mark fresh-as-of-now
        # so it doesn't re-fire until the next real mutation.  Handling it here
        # also avoids an unretrieved-exception warning on the detached task.
        log.warning("duplicate-group recompute failed; retrying on next mutation",
                    exc_info=True)
        _dup_seq = store._mutation_seq
        return 0


async def _ensure_dup_fresh(*, await_if_empty: bool = True) -> None:
    """Trigger a single-flight background recompute when the library has mutated
    since the last one.  Serves the persisted index immediately (eventually
    consistent); only blocks when there's nothing to show yet."""
    global _dup_task
    store = get_store()
    if _dup_seq == store._mutation_seq:
        return
    if _dup_task is None or _dup_task.done():
        _dup_task = asyncio.create_task(_do_dup_recompute())
    if await_if_empty and store.duplicate_group_count() == 0:
        try:
            await _dup_task
        except Exception:
            log.warning("duplicate-group recompute failed", exc_info=True)


def _public_meta(store, tid: str) -> dict | None:
    """TrackMeta-shaped dict (every public field present + defaults coerced, incl.
    the persisted dup annotations) for a track id.  Shaped through ``TrackMeta`` so
    the response matches the old ``model_dump()`` contract even for tracks
    persisted before a newer field was added — only the few-hundred members of the
    returned groups are shaped, so the per-track cost is negligible.  ``embedding``
    is excluded from the input so it round-trips as the model default (not the
    stored vector), exactly as the old path did."""
    t = store.get_track(tid)
    if not t:
        return None
    try:
        return TrackMeta(**{
            k: v for k, v in t.items()
            if k in _PUBLIC_META_FIELDS and k != "embedding"
        }).model_dump()
    except Exception:
        return None


def _shape_group(store, gid: str, tids: list[str]) -> dict | None:
    """Assemble one response group from member ids — primary first, then by
    format quality.  Returns ``None`` for groups that no longer have ≥2 members
    (e.g. after a delete dropped one)."""
    members = [m for m in (_public_meta(store, tid) for tid in tids) if m]
    if len(members) < 2:
        return None
    members.sort(key=lambda t: (not t.get("is_duplicate_primary", False),
                                -(t.get("format_score") or 0)))
    return {
        "group_id": gid,
        "primary_id": members[0]["id"],
        "count": len(members),
        "tracks": members,
    }


@router.get("/smart/duplicates")
async def list_duplicate_groups(limit: int = Query(100, ge=1, le=500)):
    """Return duplicate groups (largest first) with their member tracks.

    Response: ``[{group_id, primary_id, count, tracks: [TrackMeta + dup fields]}]``.
    Served from the maintained ``_tag_dup_group`` index — no per-request recompute.
    """
    store = get_store()
    await _ensure_dup_fresh()
    index = store.duplicate_group_index()                 # {gid: [tids]}, grouped tracks only
    sized = [(gid, tids) for gid, tids in index.items() if len(tids) >= 2]
    sized.sort(key=lambda gt: -len(gt[1]))                # rank by size before shaping
    result = []
    for gid, tids in sized[:limit]:
        g = _shape_group(store, gid, tids)
        if g:
            result.append(g)
    return result


@router.get("/smart/duplicates/{group_id}")
async def get_duplicate_group(group_id: str):
    """Return all tracks in a specific duplicate group."""
    store = get_store()
    await _ensure_dup_fresh()
    tids = store.duplicate_group_index().get(group_id)
    g = _shape_group(store, group_id, tids) if tids else None
    if not g:
        raise HTTPException(404, "Duplicate group not found")
    return g["tracks"]


@router.post("/smart/duplicates/recompute")
async def recompute_duplicates():
    """Force a recompute of duplicate-group annotations and persist them."""
    changed = await _do_dup_recompute()
    # Count only real groups (≥2 members) — a delete can decay a group to a lone
    # member that still carries a stale group_id until the next recompute.
    index = get_store().duplicate_group_index()
    groups = sum(1 for tids in index.values() if len(tids) >= 2)
    log.info("Duplicate recompute: %d annotation change(s), %d groups", changed, groups)
    return {"updated": changed, "groups": groups}


@router.post("/smart/duplicates/{group_id}/primary")
async def set_group_primary(group_id: str, track_id: str):
    """Override which track is the primary in a duplicate group.

    Reads the group's members straight from the maintained index (no full
    recompute) and flips ``is_duplicate_primary`` on them in one batched write.
    The override stands until the next recompute re-derives the primary by
    format quality.
    """
    global _dup_seq
    store = get_store()
    tids = store.duplicate_group_index().get(group_id)
    if not tids:
        raise HTTPException(404, "Group not found")
    if track_id not in tids:
        raise HTTPException(400, "Track is not in this duplicate group")
    store.update_track_fields_batch(
        [(tid, {"is_duplicate_primary": tid == track_id}) for tid in tids]
    )
    # Keep _dup_seq in lock-step with the write we just made so the staleness
    # check doesn't immediately recompute the manual override away.
    _dup_seq = store._mutation_seq
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


