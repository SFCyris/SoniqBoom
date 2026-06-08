# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track CRUD endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Query, Response

# Dedicated thread-pool for ``_compute_waveform`` — that helper spawns a
# 60s-timeout ffmpeg subprocess per call and ties up its worker the whole
# time.  Letting it share the default executor with AOF flush + art reads
# led to flush starvation under 5 concurrent users (Perf #1).  Sized
# small so a flood of waveform requests can't drown the rest of the app.
_WAVEFORM_POOL = ThreadPoolExecutor(
    max_workers=max(2, min(4, (os.cpu_count() or 4) // 2)),
    thread_name_prefix="sb-waveform",
)

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
        # Cap connection use so a track-change storm (5 users + 3 rooms
        # all switching together) doesn't open 100 concurrent connections
        # to LRClib — Perf #1 flagged the missing limits.
        _lrclib_client = httpx.AsyncClient(
            timeout=8.0,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
    return _lrclib_client


_ALLOWED_SORT_KEYS = {
    "added", "year", "duration", "bpm",
    "title", "artist", "album_artist", "album", "format",
}


@router.get("", response_model=list[TrackMeta])
async def list_tracks(
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    format: str | None = Query(
        None,
        description=(
            "Optional format filter (e.g. 'MIDI', 'ProTracker').  Drives the "
            "library Galaxy view's windowed per-format browse.  Matched "
            "case-insensitively against the store's format index."
        ),
    ),
    sort: str | None = Query(
        None,
        description=(
            "Sort key: added (default, newest first), year, duration, bpm, "
            "title, artist, album, format."
        ),
    ),
    order: str | None = Query(
        None,
        description=(
            "Sort direction: asc or desc.  Defaults to desc for 'added' and "
            "asc for every other key."
        ),
    ),
):
    """Return all tracks (paginated), sorted by added_at desc by default.

    The All Tracks windowed view passes ``sort=<col>&order=<asc|desc>`` to
    drive the per-column lexical / numeric sort indexes maintained by the
    in-memory store, so a sort click on a 267K-row library remains O(limit)
    per page instead of O(N log N) per click.
    """
    # Defensive whitelist — silently ignore unknown sort keys so a stale
    # frontend can't 400 the page; we fall back to the default sort instead.
    sort_by = sort if sort in _ALLOWED_SORT_KEYS else None
    sort_order = order if order in ("asc", "desc") else None
    if format:
        # ft_search parses @format:{value} → store.filter_tracks(format_=value),
        # matched case-insensitively.  _esc_tag keeps odd format names (spaces,
        # slashes) from breaking the tag-query parse.
        from soniqboom.api.search import _esc_tag
        query = f"@format:{{{_esc_tag(format)}}}"
    else:
        query = "*"
    return await ft_search(
        query, limit=limit, offset=offset,
        sort_by=sort_by, sort_order=sort_order,
    )


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


@router.get("/{track_id}/patterns")
async def get_patterns(track_id: str):
    """Return the tracker pattern grid + order list for the file, when
    the operator has ``pyopenmpt`` installed.  Used by the Now-Playing
    pattern viewer for tracker modules (E-15).  Returns
    ``{"available": False}`` for non-tracker files or when the binding
    isn't available."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    fmt = str(track.format or "").upper()
    # Cheap reject: skip anything that isn't a known tracker format.
    if not any(fmt.startswith(prefix) for prefix in (
        "PROTRACKER", "FASTTRACKER", "SCREAMTRACKER", "IMPULSE",
        "MULTITRACKER", "OCTAMED", "COMPOSER", "DIGIBOOSTER",
        "ULTRATRACKER", "FARANDOLE", "OKTALYZER", "AHX", "HIVELY",
    )):
        return {"id": track_id, "available": False, "channels": 0,
                "order": [], "patterns": []}
    path_str = track.path
    if path_str.startswith(("smb://", "ftp://", "http://", "https://")):
        # Fetch the remote module to the local cache so the pattern parser has
        # a real file.  A cache-only lookup returned None for anything not
        # already mirrored (e.g. a remote tracker the user hasn't played yet),
        # leaving the pattern grid empty.  Guarded — a fetch failure degrades
        # to "not available", never a 500.
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        source = get_source(scan_root) if remote_path else None
        path = None
        if source is not None:
            try:
                loop = asyncio.get_event_loop()
                path = await loop.run_in_executor(
                    None, get_cache().fetch, scan_root, remote_path, source)
            except Exception:
                path = None
    else:
        path = Path(path_str)
    if not path or not path.exists():
        return {"id": track_id, "available": False, "channels": 0,
                "order": [], "patterns": []}
    from soniqboom.core.tracker_patterns import extract_patterns
    loop = asyncio.get_event_loop()
    payload = await loop.run_in_executor(None, extract_patterns, path)
    payload["id"] = track_id
    return payload


@router.get("/{track_id}/vu")
async def get_vu_sidecar(track_id: str):
    """Return the binary VUMR sidecar for a rendered tracker module.

    Tracker / chip-format renders produce a per-channel VU sidecar
    alongside the audio cache (see ``docs/vu-cache-format.md``).  The
    frontend fetches this once on track-load and drives the per-channel
    VU bars from it — random-access by frame index against
    ``audio.currentTime``.

    Lazy backfill
    -------------
    When the sidecar doesn't exist yet but the track IS a tracker
    format AND the source file is reachable, we run an in-process VU
    extraction pass on the source file directly.  Result is cached
    alongside the existing audio WAV.  This covers the "v1.3.0 just
    shipped, my 60 K-file library has audio caches but no .vu yet"
    case without forcing the user to wait for natural cache eviction.

    First-call latency: typically < 0.5 s for a sub-5-minute module
    (libopenmpt advances the mixer state at ~1500× real-time per the
    bench in core/openmpt_vu.py).  Cached on disk forever after — a
    given track's sidecar is generated once per (track, subsong)
    pair.

    Returns:
      * 200 ``application/octet-stream`` with the VUMR binary +
        immutable cache headers, when a sidecar exists or was
        just generated.
      * 404 when the track isn't a tracker format, the source file
        can't be reached, or libopenmpt isn't available on this
        host.  The frontend falls back to its FFT-spectrum
        visualiser with the honest label.
    """
    from fastapi.responses import Response
    from soniqboom.core.conversion_cache import (
        get_vu_sidecar_path, _cache_path,
    )

    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")

    sidecar = get_vu_sidecar_path(track_id)
    if sidecar is None:
        # Lazy-backfill path.  Only attempt for known tracker formats
        # (we don't want to spin up libopenmpt against a 10 GB FLAC).
        sidecar = await _try_backfill_vu_sidecar(track, track_id)

    if sidecar is None:
        raise HTTPException(404, "No VU sidecar (not a tracker render or libopenmpt unavailable)")
    try:
        data = sidecar.read_bytes()
    except OSError:
        raise HTTPException(404, "VU sidecar unreadable")
    import hashlib
    etag = f'"{hashlib.sha256(data).hexdigest()[:16]}"'
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag":          etag,
            "X-VU-Version":  "1",
        },
    )


# Tracker-only formats — gating the lazy backfill so we don't try to
# open a FLAC with libopenmpt.  Matches stream.py's _TRACKER_EXTS but
# imported lazily to avoid a cycle.
_VU_BACKFILL_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mptm", ".med", ".oct",
    ".669", ".dbm", ".dsm", ".far", ".gdm", ".imf", ".mtm",
    ".okt", ".sfx", ".stm", ".ult", ".wow",
}


async def _try_backfill_vu_sidecar(track, track_id: str):
    """One-shot VU extraction against a track's source file.

    Returns the path to the freshly-written sidecar, or None if any
    step fails.  Side effect: writes the sidecar next to the cached
    audio WAV when one exists, else next to the source file under a
    pre-determined conversion-cache path.
    """
    import os
    from pathlib import Path
    from soniqboom.core import openmpt_vu
    from soniqboom.core.conversion_cache import _cache_path

    log = logging.getLogger(__name__)
    if not openmpt_vu.is_available():
        log.debug("VU backfill skipped — libopenmpt unavailable")
        return None
    # Tracker path can be a ZIP-virtual like ``foo.zip::inner.xm``
    # where ``Path().suffix`` walks the LAST component (".xm" — good).
    # For raw nested virtuals like ``a.zip::b.zip::c.xm`` it's the
    # same — Path() ignores the ``::`` separator and treats the whole
    # thing as one name; suffix is still ``.xm``.
    ext = (Path(track.path).suffix or "").lower()
    if ext not in _VU_BACKFILL_EXTS:
        log.debug("VU backfill skipped — ext %r not in tracker set", ext)
        return None
    # Resolve the source bytes — local file, remote-cache mirror, or
    # ZIP virtual path.  Reuses the same logic the pattern endpoint
    # already does.
    path_str = track.path
    src_bytes: bytes | None = None
    try:
        if path_str.startswith(("smb://", "ftp://", "http://", "https://")):
            # Fetch remote → local (cache-only previously missed never-played
            # tracks).  The whole function is wrapped in try/except below, so a
            # fetch failure just means no backfill → 404 → FFT fallback.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            source = get_source(scan_root) if remote_path else None
            if source is not None:
                local = await asyncio.get_event_loop().run_in_executor(
                    None, get_cache().fetch, scan_root, remote_path, source)
                if local and local.exists():
                    src_bytes = local.read_bytes()
        elif "::" in path_str:
            # ZIP-virtual path — _read_from_zip_path returns bytes.
            from soniqboom.core.scanner import _read_from_zip_path
            data, _name = _read_from_zip_path(path_str)
            src_bytes = data
        else:
            p = Path(path_str)
            if p.exists():
                src_bytes = p.read_bytes()
    except Exception:
        log.warning("VU backfill source-read failed for %s", path_str, exc_info=True)
        return None
    if not src_bytes:
        log.debug("VU backfill found no bytes for %s", path_str)
        return None

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: openmpt_vu.extract_vu(src_bytes),
    )
    if result is None:
        log.warning("VU backfill: extract_vu returned None for %s", path_str)
        return None
    if result.frames == 0:
        log.warning("VU backfill: 0 frames for %s", path_str)
        return None

    # Pick a destination path.  Prefer next to the existing cached
    # WAV (so eviction is uniform); fall back to a freshly-keyed
    # cache slot if no audio cache exists for this track yet.
    try:
        from soniqboom.core.conversion_cache import _meta, _state_lock
        candidate: Path | None = None
        with _state_lock:
            for cache_key, entry in _meta.items():
                if cache_key.startswith(f"{track_id}__") and entry.get("format_type") == "tracker":
                    candidate = Path(entry["path"]).with_suffix(".vu")
                    break
        if candidate is None:
            # No tracker entry yet — synthesize a sidecar-only slot
            # keyed by track_id alone.  Lives in the same cache dir
            # so it gets evicted alongside other tracker assets.
            base = _cache_path(f"{track_id}__novubackfill", "tracker")
            candidate = base.with_suffix(".vu")
        await loop.run_in_executor(
            None, openmpt_vu.write_sidecar, candidate, result,
        )
        log.info(
            "VU sidecar backfilled for %s: %d ch × %d frames @ %d Hz → %s",
            track_id, result.channels, result.frames, result.sample_rate, candidate,
        )
        return candidate
    except Exception:
        log.warning("VU backfill write failed for %s", track_id, exc_info=True)
        return None


@router.get("/{track_id}/chapters")
async def get_chapters(track_id: str):
    """Return chapter markers for podcasts / audiobooks / long tracks.

    Reads MP4 ``chpl`` atoms and ID3 ``CHAP`` frames from the file.
    Empty list if the file has no chapters."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    path_str = track.path
    if path_str.startswith(("smb://", "ftp://", "http://", "https://")):
        # Remote / WebDAV path — only check the locally cached copy.
        from soniqboom.core.filesource import parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if remote_path:
            cached = get_cache().get_cached(scan_root, remote_path)
            path = cached if cached and cached.exists() else None
        else:
            path = None
    else:
        path = Path(path_str)
    if not path or not path.exists():
        return {"id": track_id, "chapters": []}
    from soniqboom.core.chapters import extract_chapters
    loop = asyncio.get_event_loop()
    chapters = await loop.run_in_executor(None, extract_chapters, path)
    return {"id": track_id, "chapters": chapters}


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
        from soniqboom.core.filesource import parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if remote_path:
            cached = get_cache().get_cached(scan_root, remote_path)
            path = cached if cached and cached.exists() else None
        else:
            path = None
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
        _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS,
        _render_sid, _render_midi, _render_tracker, _render_uade, _render_hvl,
    )
    from soniqboom.core.conversion_cache import get_or_render

    _zip_tmp = None
    try:
        # Resolve actual file path (extract from ZIP if needed)
        if '::' in path_str and path_str.startswith(("ftp://", "smb://")):
            # Remote ZIP member — ``_read_from_zip_path`` can't open an
            # ``ftp://…zip`` outer, so fetch the archive to the local cache
            # first, then extract the member (same as the stream path).
            from soniqboom.core import archive as _archive
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            source = get_source(scan_root)
            if source is None or "::" not in remote_path:
                return None                    # degrade — no waveform, no 500
            arc_rel, member_name = remote_path.split("::", 1)
            local_archive = get_cache().fetch(scan_root, arc_rel, source)
            data = _archive.read_member(local_archive, member_name)
            suffix = _Path(member_name).suffix
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(data)
            tmp.close()
            path = _Path(tmp.name)
            _zip_tmp = path
        elif '::' in path_str:
            from soniqboom.core.scanner import _read_from_zip_path
            data, member_name = _read_from_zip_path(path_str)
            suffix = _Path(member_name).suffix
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(data)
            tmp.close()
            path = _Path(tmp.name)
            _zip_tmp = path
        elif path_str.startswith(("ftp://", "smb://")):
            # Plain remote file (no ``::``).  The renderers need a REAL local
            # path — ``Path("ftp://…")`` collapses to ``ftp:/…`` and
            # openmpt123 / libopenmpt / sidplayfp can't open it.  This was the
            # bug behind "remote MOD → SRC_NOT_SUPPORTED": the waveform render
            # shares the audio render through ``get_or_render``'s thundering-
            # herd dedup, so a failed render here also failed the audio.  Fetch
            # to the local cache first, exactly like the stream endpoint does.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            source = get_source(scan_root) if remote_path else None
            if source is None:
                return None                    # degrade — no waveform, no 500
            try:
                loop = asyncio.get_event_loop()
                path = _Path(await loop.run_in_executor(
                    None, get_cache().fetch, scan_root, remote_path, source))
            except Exception:
                return None                    # FTP hiccup — degrade, no 500
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
        elif ext in _HVL_EXTS:
            # HivelyTracker — bundled hvl2wav (uade/openmpt can't decode HVL).
            fmt, sf_path = "hvl", None
            render_fn = lambda: _render_hvl(path, subsong=0)
        elif ext in _UADE_EXTS:
            # AHX — uade123, distinct cache namespace so an accidental
            # tracker-render of the same file (if we ever mis-route)
            # doesn't poison the right output.
            fmt, sf_path = "uade", None
            render_fn = lambda: _render_uade(path, subsong=0)
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


def _normalise_waveform(result):
    """Normalise ``_compute_waveform`` output to ``(stored, response)``.

    ``_compute_waveform`` returns either a flat list (pure-Python path)
    or a ``{"peaks", "rms"}`` dict (numpy path).  The store layer only
    accepts a flat list, so we keep one of the two arrays for storage.

    User observation (2026-05-23) on a high-dynamic-range DSF: storing
    RMS produced a waveform display where the loud transients dominated
    visually and quieter passages rendered as 1-pixel bars indistinct
    from the seek-track background — read as "blocks with gaps".  PEAKS
    are visually more uniform (less compressed by averaging) and match
    user expectation of a waveform display.  Store peaks when the numpy
    path produced them; fall back to the rms array (or the bare list
    from the pure-Python path) otherwise.  The API response carries the
    full dict when available so the client can mix the two views.
    """
    if isinstance(result, dict):
        stored = result.get("peaks") or result.get("rms") or []
        return stored, result
    return result, result


def _waveform_is_blank(stored) -> bool:
    """Return True if ``stored`` is empty or all-zero.

    Used as the gate before persisting a freshly-computed waveform.
    ``_compute_waveform`` returns ``[0.0] * points`` when ffmpeg's decode
    produces no audio bytes — most commonly because the source path
    couldn't be opened (remote URL ffmpeg doesn't speak, in-flight
    ``.partial`` not yet promoted to the final cache name, malformed
    file).  Storing that blank result would lock the waveform endpoint
    into the cache fast-path forever and the user would never see the
    real waveform after the transcode finished.  Skipping the store on a
    blank result lets the next call (e.g. the one app.js fires when
    ``transcode-ready`` lands) recompute from the now-available cached
    WAV and store a real waveform.
    """
    if not stored:
        return True
    try:
        return all(float(v) == 0.0 for v in stored)
    except (TypeError, ValueError):
        return False


@router.get("/{track_id}/waveform")
async def get_track_waveform(track_id: str, response: Response):
    """Return waveform amplitude data, computing on-demand if not cached.

    For converted formats (SID, MIDI, tracker modules) the waveform is
    computed from the conversion-cache WAV rather than the raw source file.
    """
    import asyncio
    from pathlib import Path as _Path
    from soniqboom.core.data import get_waveform, get_track, store_waveform
    from soniqboom.core.scanner import _compute_waveform
    from soniqboom.api.stream import _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS

    # ``no-store`` on every response so the browser never serves a stale
    # body when the frontend re-fetches after ``transcode-ready``.  The
    # initial fetch on a fresh DSF/SACD track returns the silent-padded
    # reading taken off the in-flight WAV; the transcode-ready refresh
    # is supposed to return the real one once the full conversion lands.
    # Without this header (or the frontend's matching ``cache: no-cache``
    # on its fetch) Chrome happily caches the first body under the URL
    # key and reuses it for the refresh — manifests as "the waveform
    # updates sometimes but not always", because Chrome's disk-cache
    # eviction is LRU+size-bound so what gets reused varies per session.
    response.headers["Cache-Control"] = "no-store"

    # Fast path: already cached — but treat a blank (all-zero / empty)
    # cached entry as a miss so we recompute against a now-available
    # source.  Tracks that were waveform-computed against an unreachable
    # source (remote URL ffmpeg couldn't open, in-flight WAV not yet
    # promoted) wrote zeros into the cache under the pre-fix code; this
    # makes the next call self-heal instead of forever-serving the
    # poisoned zeros, no manual ``/api/admin/cache/waveforms`` clear
    # required.
    waveform = await get_waveform(track_id)
    if waveform is not None and not _waveform_is_blank(waveform):
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
    if ext in _SID_EXTS or ext in _MIDI_EXTS or ext in _TRACKER_EXTS or ext in _UADE_EXTS or ext in _HVL_EXTS:
        wav_path = await _waveform_from_conversion_cache(track_id, path_str, ext)
        result = await loop.run_in_executor(_WAVEFORM_POOL, _compute_waveform, str(wav_path))
        stored, response = _normalise_waveform(result)
        if not _waveform_is_blank(stored):
            await store_waveform(track_id, stored)
        return {"waveform": response}

    # ── Transcoded formats (DSD / ALAC / AIFF / WavPack / Musepack) ──────
    # These also have a cached FLAC the stream endpoint produces.  Using
    # that instead of the raw source means ffmpeg decodes a ~10 MB FLAC
    # instead of a ~60 MB DSD or ~50 MB ALAC, and it shares one render
    # with the stream path (thundering-herd guard prevents duplicate work).
    # Perception payoff: the waveform appears within ~1 s of the audio
    # starting, instead of ~5–10 s in the old code path.
    from soniqboom.api.stream import _DSD_EXTS, _inflight_cache_key
    _TRANSCODED_WAVEFORM_EXTS = _DSD_EXTS | {
        ".m4a", ".aac", ".aiff", ".aif", ".wv", ".mpc",
    }
    if ext in _TRANSCODED_WAVEFORM_EXTS:
        # Prefer the final cached WAV when it exists — fastest path
        # (file already on disk, no ffmpeg invocation needed beyond the
        # 8 kHz mono downsample inside _compute_waveform).
        from soniqboom.core.conversion_cache import get_cached
        from soniqboom.api.stream import (
            _DSD_OUTPUT_RATE, _INFLIGHT_TRANSCODES,
        )
        import logging
        _log = logging.getLogger("soniqboom.waveform-dbg")
        target_rate = _DSD_OUTPUT_RATE if ext in _DSD_EXTS else None
        cache_key = _inflight_cache_key(track_id, target_rate)
        cached_path = await get_cached(cache_key)
        _log.debug("waveform %s ext=%s cache=%s",
                  track_id[:8], ext,
                  "HIT" if cached_path else "MISS")

        # Cache MISS recovery.  Two sub-cases:
        #
        # 1. An in-flight pump is ALREADY rendering this track — await it.
        # 2. No pump yet — wait briefly for one to appear, then await it.
        #
        # Sub-case 2 was the killer the diagnostic logs exposed: the
        # frontend's ``trackchange`` listener fires _fetchWaveform BEFORE
        # the audio element issues its first range GET, so /waveform
        # arrives at the backend a tiny moment ahead of /stream — and
        # /stream is what triggers ``_get_or_start_inflight_wav`` to
        # create the pump.  Without the appear-wait below, our
        # ``_INFLIGHT_TRANSCODES.get(track_id)`` reads ``None``, we skip
        # the await, fall through to ``_compute_waveform(ftp://...)``,
        # ffmpeg can't decode that pseudo-URL, returns zeros, user sees
        # blank.  Polling for the inflight to appear (cheap dict lookup
        # every 100 ms for up to 2 s) gives the streaming side a chance
        # to spawn the pump first; once it's there we join it.
        if cached_path is None:
            # Wait window for one of three exit conditions:
            #   (a) ``get_cached(cache_key)`` flips HIT — some other
            #       concurrent request finished the pump before we did.
            #   (b) ``_INFLIGHT_TRANSCODES[track_id]`` appears — the
            #       streaming-side audio request landed and spawned the
            #       pump; we'll join it.
            #   (c) Wait ceiling exceeded — fall through to blank.
            #
            # 8 s ceiling: prior 2 s missed the cases where the browser
            # delayed its first audio range GET (HTTP/2 prioritisation,
            # connection pool exhaustion under rapid track-skip, etc.).
            # The audio request usually arrives within 50-500 ms, but
            # observed worst case in the diagnostic was ~2.5 s — 8 s
            # gives a comfortable margin without hanging on the genuinely-
            # not-played case for too long.  Re-checks BOTH the cache
            # (covers a concurrent fetch that finished while we slept)
            # and the inflight registry every 100 ms so we exit as soon
            # as either condition is met.
            _log.debug("waveform %s waiting (cache+inflight)...",
                      track_id[:8])
            inflight = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 8.0
            while loop.time() < deadline:
                cached_path = await get_cached(cache_key)
                if cached_path is not None:
                    _log.debug("waveform %s cache flipped to HIT during wait",
                              track_id[:8])
                    break
                inflight = _INFLIGHT_TRANSCODES.get(track_id)
                if inflight is not None:
                    break
                await asyncio.sleep(0.1)

            if cached_path is None:
                # The inflight dict is inserted into _INFLIGHT_TRANSCODES
                # BEFORE its ``pump_task`` key is populated — stream.py
                # holds _INFLIGHT_LOCK only long enough to claim the slot,
                # then drops it for the slow ffprobe + WAV header pre-write
                # (200-500 ms), THEN re-acquires the lock to add
                # ``pump_task`` + ``wav_path`` + the events.  If we look
                # up ``pump_task`` during that window we get ``None`` and
                # silently fall through to blank.  Wait for
                # ``setup_ready`` to fire (the same coordination event
                # other inflight subscribers use, ``stream.py:1245``) so
                # we read the dict only after it's fully populated.
                if inflight is not None:
                    setup_ready = inflight.get("setup_ready")
                    if setup_ready is not None and not setup_ready.is_set():
                        _log.debug("waveform %s awaiting inflight setup...",
                                  track_id[:8])
                        try:
                            await asyncio.wait_for(
                                setup_ready.wait(), timeout=10.0,
                            )
                        except asyncio.TimeoutError:
                            _log.debug(
                                "waveform %s setup_ready timed out",
                                track_id[:8],
                            )
                        # Re-read in case the inflight was replaced.
                        inflight = (
                            _INFLIGHT_TRANSCODES.get(track_id) or inflight
                        )

                pump_task = inflight.get("pump_task") if inflight else None
                if pump_task is not None and not pump_task.done():
                    try:
                        # 120 s ceiling — enough for any reasonable
                        # DSD/ALAC pass, short enough that a wedged pump
                        # fails the request rather than hanging the
                        # worker forever.
                        _log.debug("waveform %s awaiting inflight pump...",
                                  track_id[:8])
                        await asyncio.wait_for(pump_task, timeout=120.0)
                        _log.debug("waveform %s pump finished", track_id[:8])
                    except asyncio.TimeoutError:
                        _log.debug("waveform %s pump timed out after 120s",
                                     track_id[:8])
                    except Exception as exc:
                        _log.debug("waveform %s pump errored: %s: %s",
                                  track_id[:8], type(exc).__name__, exc)
                elif inflight is None:
                    _log.debug("waveform %s no inflight after 8s wait",
                              track_id[:8])
                else:
                    # inflight exists but pump_task still missing or
                    # already done — log so we can spot it.
                    _log.debug(
                        "waveform %s inflight present but no live pump "
                        "(keys=%s, done=%s)",
                        track_id[:8],
                        sorted(inflight.keys()),
                        pump_task.done() if pump_task else "N/A",
                    )

                # Final cache re-check — covers both the post-pump path
                # and the race where the pump completed between our last
                # in-loop check and the pump_task await.
                cached_path = await get_cached(cache_key)
                _log.debug("waveform %s post-wait cache=%s",
                          track_id[:8],
                          "HIT" if cached_path else "STILL MISS")
        # ``_compute_waveform`` runs its own ``ffmpeg -ac 1 -ar 8000 -f f32le``
        # which handles every source format ffmpeg can demux — DSD via the
        # built-in dsf / iff (DFF) / wsd demuxers, ALAC inside .m4a, AIFF,
        # WavPack, Musepack.  Going straight to source means the waveform
        # appears in ~3 s on a typical 5-min DSD instead of waiting the full
        # transcode (~30–50 s) — the single biggest perception polish
        # remaining after the cold-start fix.
        src_for_waveform = str(cached_path) if cached_path else path_str
        result = await loop.run_in_executor(
            _WAVEFORM_POOL, _compute_waveform, src_for_waveform,
        )
        stored, response = _normalise_waveform(result)
        # Cache-poisoning guard: when ``cached_path`` is None (the
        # in-flight pump hasn't promoted .partial yet) and ``path_str``
        # is a remote URL ffmpeg can't read directly (e.g. our internal
        # ``ftp://host/scan:/relative`` form), ``_compute_waveform``
        # returns all-zeros — storing that locks the fast-path forever.
        # Pump-completion reordering (api/stream.py) now closes the race
        # on the happy path; this guard is the belt-and-braces fallback.
        blank = _waveform_is_blank(stored)
        if not blank:
            await store_waveform(track_id, stored)
        _log.debug(
            "waveform %s computed: len=%d shape=%s first5=%s blank=%s",
            track_id[:8], len(stored) if stored else 0,
            type(response).__name__,
            (stored[:5] if stored else []), blank,
        )
        return {"waveform": response}

    # ── Non-converted ZIP files: skip (ffmpeg can't read ZIP directly) ───
    if '::' in path_str:
        raise HTTPException(404, "Waveform not available for this format")

    # ── Remote files: compute from cached local copy ─────────────────────
    if path_str.startswith(("smb://", "ftp://")):
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            raise HTTPException(400, "Remote path is malformed")
        source = get_source(scan_root)
        if source is None:
            raise HTTPException(503, "Network share unavailable")
        try:
            local_path = get_cache().fetch(scan_root, remote_path, source)
        except Exception as exc:
            raise HTTPException(502, f"Could not fetch remote file: {exc}")
        result = await loop.run_in_executor(_WAVEFORM_POOL, _compute_waveform, str(local_path))
        stored, response = _normalise_waveform(result)
        if not _waveform_is_blank(stored):
            await store_waveform(track_id, stored)
        return {"waveform": response}

    # ── Standard local files: compute directly from source ───────────────
    result = await loop.run_in_executor(_WAVEFORM_POOL, _compute_waveform, track.path)
    stored, response = _normalise_waveform(result)
    if not _waveform_is_blank(stored):
        await store_waveform(track_id, stored)
    return {"waveform": response}


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
async def mark_played(track_id: str, sb_session: str | None = Cookie(default=None)):
    """Record a play event for the track (increments count, sets last_played).

    Also pushes the event to the listening history log (smart.py) and
    forwards the play to last.fm / ListenBrainz if the signed-in user
    has scrobble tokens configured.
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

    # External scrobble (last.fm / ListenBrainz) for the signed-in user —
    # queued + retried on network failure inside core.scrobble.
    try:
        from soniqboom.core.scrobble import submit_play
        from soniqboom.core.store import get_store
        from soniqboom.core.users import get_user_store
        store = get_store()
        full_track = store.get_track(track_id)
        if full_track and sb_session:
            user = get_user_store().lookup_session(sb_session)
            if user:
                await submit_play(user, full_track)
    except Exception:
        pass

    return {"id": track_id, **stats}


@router.get("/{track_id}/stats")
async def read_play_stats(track_id: str):
    stats = await get_play_stats(track_id)
    return {"id": track_id, **stats}
