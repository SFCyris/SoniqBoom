# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track CRUD endpoints."""
from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query

from soniqboom.core.data import (
    delete_track, get_track, track_count,
    set_rating, get_rating, get_ratings_batch, get_all_ratings,
    record_play, get_play_stats, get_play_stats_batch, get_all_play_stats,
    ft_search,
)
from soniqboom.core.metadata import extract_lyrics
from soniqboom.models.track import TrackMeta

router = APIRouter(prefix="/tracks", tags=["tracks"])

# ── Shared httpx client for LRCLib requests ──────────────────────────────────

_lrclib_client: httpx.AsyncClient | None = None


def _get_lrclib_client() -> httpx.AsyncClient:
    global _lrclib_client
    if _lrclib_client is None:
        _lrclib_client = httpx.AsyncClient(timeout=8.0)
    return _lrclib_client


@router.get("", response_model=list[TrackMeta])
async def list_tracks(
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    """Return all tracks (paginated), sorted by added_at desc."""
    return await ft_search("*", limit=limit, offset=offset)


@router.get("/count")
async def count_tracks():
    return {"count": await track_count()}


# ── Ratings (batch endpoints — must be before /{track_id} to avoid capture) ──

@router.get("/meta/ratings")
async def all_ratings():
    """Return all ratings as {track_id: rating}."""
    return await get_all_ratings()


@router.post("/meta/ratings/batch")
async def batch_ratings(body: dict):
    """Return ratings for a list of track IDs."""
    ids = body.get("ids", [])
    return await get_ratings_batch(ids)


@router.get("/meta/playstats")
async def all_play_stats_endpoint():
    """Return all play stats as {track_id: {count, last_played}}."""
    return await get_all_play_stats()


@router.post("/meta/playstats/batch")
async def batch_play_stats(body: dict):
    """Return play stats for a list of track IDs."""
    ids = body.get("ids", [])
    return await get_play_stats_batch(ids)


@router.get("/{track_id}", response_model=TrackMeta)
async def read_track(track_id: str):
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return track


@router.delete("/{track_id}")
async def remove_track(track_id: str):
    removed = await delete_track(track_id)
    if not removed:
        raise HTTPException(404, "Track not found")
    return {"deleted": track_id}


@router.get("/{track_id}/extended")
async def get_track_extended(track_id: str):
    """Return extended metadata for tracker/SID/MIDI files."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")

    result = {
        "format": track.format,
        "instruments": track.instruments or [],
        "channels": track.channels,
        "patterns": track.patterns,
        "subsongs": track.subsongs,
    }
    return result


@router.get("/{track_id}/lyrics")
async def get_lyrics(track_id: str):
    """Return lyrics for a track: embedded tags first, LRCLib fallback."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")

    # 1. Try embedded lyrics (sync-safe via executor)
    loop = asyncio.get_event_loop()
    path_str = track.path
    # For remote tracks, try the locally cached copy
    if path_str.startswith(("smb://", "ftp://")):
        from soniqboom.core.remote_cache import get_cache
        sep = path_str.index(":", 6)
        scan_root, remote_path = path_str[:sep], path_str[sep + 1:]
        cached = get_cache().get_cached(scan_root, remote_path)
        path = cached if cached and cached.exists() else None
    else:
        path = Path(path_str)
        if not path.exists():
            path = None
    embedded = None
    if path:
        embedded = await loop.run_in_executor(None, extract_lyrics, path)
    if embedded:
        # Detect LRC synced format: lines starting with [mm:ss.xx]
        import re
        is_synced = bool(re.search(r'^\[\d{1,2}:\d{2}[.\:]\d{2,3}\]', embedded, re.MULTILINE))
        return {"lyrics": embedded, "synced": is_synced, "source": "Embedded tags"}

    # 2. Fallback: LRCLib (free, no API key)
    artist = track.artist or track.album_artist or ""
    title  = track.title or ""
    album  = track.album or ""
    if not (artist and title):
        return {"lyrics": None, "source": None}

    try:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if track.duration:
            params["duration"] = str(int(track.duration))

        client = _get_lrclib_client()
        resp = await client.get("https://lrclib.net/api/get", params=params)
        if resp.status_code == 200:
            data = resp.json()
            synced = data.get("syncedLyrics") or ""
            plain  = data.get("plainLyrics") or ""
            if synced.strip():
                return {"lyrics": synced.strip(), "synced": True, "source": "LRCLib.net"}
            if plain.strip():
                return {"lyrics": plain.strip(), "synced": False, "source": "LRCLib.net"}
    except Exception:
        pass

    return {"lyrics": None, "synced": False, "source": None}


async def _waveform_from_conversion_cache(track_id: str, path_str: str, ext: str):
    """Get WAV path for a converted format via the conversion cache.

    Uses get_or_render() which has thundering-herd prevention — if the
    stream endpoint is currently rendering the same track, this waits for it
    instead of starting a duplicate render.
    """
    import tempfile
    from pathlib import Path as _Path
    from soniqboom.api.stream import (
        _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS,
        _render_sid, _render_midi, _render_tracker,
    )
    from soniqboom.core.conversion_cache import get_or_render

    _zip_tmp = None
    try:
        # Resolve actual file path (extract from ZIP if needed)
        if '::' in path_str:
            from soniqboom.core.scanner import _read_from_zip_path
            data, member_name = _read_from_zip_path(path_str)
            suffix = _Path(member_name).suffix
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(data)
            tmp.close()
            path = _Path(tmp.name)
            _zip_tmp = path
        else:
            path = _Path(path_str)

        # Determine format type and build render function
        if ext in _SID_EXTS:
            fmt, sf_path = "sid", None
            render_fn = lambda: _render_sid(path, subsong=0)
        elif ext in _MIDI_EXTS:
            from soniqboom.config import get_active_soundfont
            sf = get_active_soundfont()
            fmt, sf_path = "midi", (str(sf) if sf else "")
            render_fn = lambda: _render_midi(path)
        else:  # tracker
            fmt, sf_path = "tracker", None
            render_fn = lambda: _render_tracker(path, subsong=0)

        wav_path, _ = await get_or_render(
            track_id=track_id, format_type=fmt, subsong=0,
            render_fn=render_fn, soundfont_path=sf_path,
        )
        return wav_path
    finally:
        if _zip_tmp is not None:
            _zip_tmp.unlink(missing_ok=True)


@router.get("/{track_id}/waveform")
async def get_track_waveform(track_id: str):
    """Return waveform amplitude data, computing on-demand if not cached.

    For converted formats (SID, MIDI, tracker modules) the waveform is
    computed from the conversion-cache WAV rather than the raw source file.
    """
    import asyncio
    from pathlib import Path as _Path
    from soniqboom.core.data import get_waveform, get_track, store_waveform
    from soniqboom.core.scanner import _compute_waveform
    from soniqboom.api.stream import _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS

    # Fast path: already cached
    waveform = await get_waveform(track_id)
    if waveform is not None:
        return {"waveform": waveform}

    track = await get_track(track_id)
    if track is None:
        raise HTTPException(404, "Track not found")

    path_str = track.path
    # Extract extension from final path component (handles ZIP virtual paths)
    ext = _Path(
        path_str.split('::')[-1] if '::' in path_str else path_str
    ).suffix.lower()

    loop = asyncio.get_event_loop()

    # ── Converted formats: compute waveform from conversion-cache WAV ────
    if ext in _SID_EXTS or ext in _MIDI_EXTS or ext in _TRACKER_EXTS:
        wav_path = await _waveform_from_conversion_cache(track_id, path_str, ext)
        waveform = await loop.run_in_executor(None, _compute_waveform, str(wav_path))
        await store_waveform(track_id, waveform)
        return {"waveform": waveform}

    # ── Non-converted ZIP files: skip (ffmpeg can't read ZIP directly) ───
    if '::' in path_str:
        raise HTTPException(404, "Waveform not available for this format")

    # ── Remote files: compute from cached local copy ─────────────────────
    if path_str.startswith(("smb://", "ftp://")):
        from soniqboom.core.filesource import get_source
        from soniqboom.core.remote_cache import get_cache
        sep = path_str.index(":", 6)
        scan_root, remote_path = path_str[:sep], path_str[sep + 1:]
        source = get_source(scan_root)
        if source is None:
            raise HTTPException(503, "Network share unavailable")
        try:
            local_path = get_cache().fetch(scan_root, remote_path, source)
        except Exception as exc:
            raise HTTPException(502, f"Could not fetch remote file: {exc}")
        waveform = await loop.run_in_executor(None, _compute_waveform, str(local_path))
        await store_waveform(track_id, waveform)
        return {"waveform": waveform}

    # ── Standard local files: compute directly from source ───────────────
    waveform = await loop.run_in_executor(None, _compute_waveform, track.path)
    await store_waveform(track_id, waveform)
    return {"waveform": waveform}


# ── Ratings ──────────────────────────────────────────────────────────────────

@router.put("/{track_id}/rating")
async def update_rating(track_id: str, body: dict):
    """Set or remove a track rating (0-5). Pass {"rating": 0} to remove."""
    rating = body.get("rating", 0)
    if not isinstance(rating, int) or rating < 0 or rating > 5:
        raise HTTPException(400, "Rating must be 0-5")
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    await set_rating(track_id, rating)
    return {"id": track_id, "rating": rating}


@router.get("/{track_id}/rating")
async def read_rating(track_id: str):
    return {"id": track_id, "rating": await get_rating(track_id)}


# ── Play stats (per-track endpoints) ─────────────────────────────────────────

@router.post("/{track_id}/played")
async def mark_played(track_id: str):
    """Record a play event for the track (increments count, sets last_played).

    Also pushes the event to the listening history log (smart.py).
    """
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    stats = await record_play(track_id)

    # Push to listening history (non-blocking, fire-and-forget)
    try:
        from soniqboom.api.smart import push_history
        await push_history(track_id, title=track.title or "", artist=track.artist or "")
    except Exception:
        pass  # history is best-effort, don't fail the play recording

    return {"id": track_id, **stats}


@router.get("/{track_id}/stats")
async def read_play_stats(track_id: str):
    stats = await get_play_stats(track_id)
    return {"id": track_id, **stats}
