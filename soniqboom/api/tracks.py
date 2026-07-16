# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track CRUD endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Query, Request, Response

# Dedicated thread-pool for ``_compute_waveform`` — that helper spawns a
# 60s-timeout ffmpeg subprocess per call and ties up its worker the whole
# time.  Letting it share the default executor with AOF flush + art reads
# led to flush starvation under 5 concurrent users (Perf #1).  Sized
# small so a flood of waveform requests can't drown the rest of the app.
_WAVEFORM_POOL = ThreadPoolExecutor(
    max_workers=max(2, min(4, (os.cpu_count() or 4) // 2)),
    thread_name_prefix="sb-waveform",
)


async def _compute_waveform_safe(path: str, points: int = 200):
    """Compute a track's waveform WITHOUT forking ffmpeg from a worker thread.

    ``scanner._compute_waveform`` decodes via a blocking ``subprocess.run``;
    offloaded to ``_WAVEFORM_POOL`` that fork runs on a non-main thread, which
    SEGFAULTS on macOS once the process has initialised Core Foundation (e.g.
    after the stations relay's outbound networking) — the worker dies and every
    waveform comes back all-zero (blank).  Decode ffmpeg on the EVENT LOOP via
    ``create_subprocess_exec`` instead — the same fork-safe pattern the whole
    streaming path uses — then crunch the PCM in the pool (numpy doesn't fork).
    """
    from soniqboom.config import settings
    from soniqboom.core.scanner import _pcm_to_waveform
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.ffmpeg_path, "-i", path,
            "-ac", "1", "-ar", "22050", "-f", "f32le", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:               # noqa: BLE001 — ffmpeg missing / spawn failure
        return [0.0] * points
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except Exception:               # noqa: BLE001 — timeout / transport error
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return [0.0] * points
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_WAVEFORM_POOL, _pcm_to_waveform, raw, points)


async def _resolve_zip_member_to_local(path_str: str):
    """Extract a ZIP member to a local temp file ffmpeg can read.

    Handles both a local archive (``/path/a.zip::member``) and a remote one
    (``ftp://host/scanroot:/a.zip::member`` — fetch the archive to the cache
    first, then read the member).  Returns a ``Path`` the CALLER must unlink,
    or ``None`` if it can't be resolved (the caller then degrades to 404).
    Mirrors the extraction already used for converted formats in
    ``_waveform_from_conversion_cache``.
    """
    from pathlib import Path as _Path
    import tempfile
    loop = asyncio.get_event_loop()
    try:
        if path_str.startswith(("ftp://", "smb://")):
            from soniqboom.core import archive as _archive
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            source = get_source(scan_root)
            if source is None or "::" not in remote_path:
                return None
            arc_rel, member_name = remote_path.split("::", 1)
            local_archive = await loop.run_in_executor(
                None, get_cache().fetch, scan_root, arc_rel, source)
            data = await loop.run_in_executor(
                None, _archive.read_member, local_archive, member_name)
        else:
            from soniqboom.core.scanner import _read_from_zip_path
            data, member_name = await loop.run_in_executor(
                None, _read_from_zip_path, path_str)
        tmp = tempfile.NamedTemporaryFile(suffix=_Path(member_name).suffix, delete=False)
        try:
            tmp.write(data)
            tmp.close()
        except Exception:
            # Don't orphan the just-created (delete=False) temp if the write
            # fails mid-stream (e.g. ENOSPC) — unlink before degrading.
            tmp.close()
            _Path(tmp.name).unlink(missing_ok=True)
            raise
        return _Path(tmp.name)
    except Exception:                       # noqa: BLE001 — degrade, never 500
        return None

import orjson

from soniqboom.core.data import (
    delete_track, get_track, track_count,
    set_rating, get_rating, get_ratings_batch, get_all_ratings,
    record_play, get_play_stats, get_play_stats_batch, get_all_play_stats,
    ft_search, ft_search_dicts,
)
from soniqboom.core.metadata import extract_lyrics
from soniqboom.models.track import TrackMeta

router = APIRouter(prefix="/tracks", tags=["tracks"])


# ── Tag editing ───────────────────────────────────────────────────────────────

from fastapi import Depends as _Depends
from pydantic import BaseModel as _BaseModel


class _TagUpdate(_BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    year: int | None = None
    track_number: int | None = None


from soniqboom.api.users import (
    require_user as _require_user, require_edit as _require_edit,
    require_admin as _require_admin,
)


@router.put("/{track_id}/tags")
async def update_tags(track_id: str, body: _TagUpdate, user=_Depends(_require_edit)):
    """Write tags into the local audio file AND mirror them into the library.

    Local files only — remote-share tracks (smb/ftp/webdav) and zip members
    are refused.  Requires a signed-in non-read-only account.
    """
    if user.role == "readonly":
        raise HTTPException(403, "Your account is read-only — tag editing needs an 'edit' or admin account.")

    from soniqboom.core.store import get_store
    t = get_store().get_track(track_id)
    if not t:
        raise HTTPException(404, "Track not found")
    path = t.get("path") or ""
    if path.startswith(("smb://", "ftp://", "http://", "https://")):
        raise HTTPException(422, "Tags can only be edited on local files (this track lives on a network share).")
    if "::" in path:
        raise HTTPException(422, "Tags can't be edited on files inside archives.")

    from soniqboom.core.tagwriter import write_tags
    try:
        applied = await asyncio.to_thread(write_tags, path, body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception:
        raise HTTPException(500, "Could not write tags to the file.")

    store_updates: dict = dict(applied)
    if "genre" in store_updates:
        store_updates["genre"] = [store_updates["genre"]]
    if "year" in store_updates:
        # A hand-edited year is authoritative: mark its provenance so the
        # Demozoo year backfill (demozoo.collect_updates) never overwrites a
        # deliberate user correction on a later apply.
        store_updates["year_source"] = "user"
    get_store().update_track_fields(track_id, store_updates)
    # The edited artist/title/album change what LRCLib would return, so drop any
    # cached lyrics for this track — the next LYRICS open re-resolves with the
    # corrected tags instead of serving a stale (possibly mismatched) result.
    _lyrics_cache.pop(track_id, None)
    return {"id": track_id, "applied": applied}

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


# ── LRCLib fuzzy-fallback helpers ─────────────────────────────────────────────
# LRCLib's exact ``/api/get`` 404s when a store/label appends release noise to
# the title or album — "(No Narration)", "(24-bit HD audio)", "(Deluxe
# Edition)", "(Remastered 2021)", "[Bonus Track]" — or when the tagged duration
# differs from LRCLib's by more than its tolerance.  When that happens we clean
# the title and fall back to the fuzzy ``/api/search``, then pick the candidate
# closest in duration so a noisy tag still resolves WITHOUT attaching a
# different recording's words (confidence-gated: refuse over guess).
import re as _re

# A TRAILING parenthetical / bracket group + its inner text (group 1).
_TRAILING_GROUP_RE = _re.compile(r"\s*[\(\[]([^\(\)\[\]]*)[\)\]]\s*$")
# Inner text that marks the group as release NOISE (safe to strip) rather than a
# meaningful subtitle.  Only noise groups are peeled — so "(24-bit HD audio)",
# "(No Narration)", "(Deluxe Edition)", "(Remastered 2021)", "(Live)" go, but
# "(How Does It Feel)" or "(Pt. 2)" stay (stripping them would let a fuzzy match
# collide with a *different* same-artist song — dangerous once write-back is on).
_QUALIFIER_NOISE_RE = _re.compile(
    r"(?i)(remaster|deluxe|expanded|\bedition\b|bonus|reissue|anniversary|"
    r"\bmono\b|\bstereo\b|\bversion\b|\bmix\b|remix|\bedit\b|\blive\b|acoustic|"
    r"instrumental|\bdemo\b|\d+\s*-?\s*bit|\d+\s*k?hz|hd\s*audio|hi-?res|"
    r"narration|explicit|\bclean\b|\bradio\b|\bsingle\b|original|feat\.?|"
    r"featuring|ft\.?|no\s*vocals?)"
)

# Duration windows (seconds) for accepting a fuzzy /api/search candidate.
_LRCLIB_DUR_MAX_S = 30.0   # nothing within this of the track → refuse the match

# Per-request timeouts (seconds).  The exact /api/get is quick; the full-text
# /api/search is heavier and slower (5–9 s under load), so it gets a longer
# budget.  Both degrade cleanly to "no lyrics" on timeout.
_LRCLIB_GET_TIMEOUT_S = 6.0
_LRCLIB_SEARCH_TIMEOUT_S = 12.0

# Resolved-lyrics cache (in-memory, POSITIVES ONLY).  LRCLib is slow and flaky,
# so once a track's lyrics resolve we keep them for the process lifetime: the
# LYRICS tab re-opens instantly and — crucially — the lyrics survive LRCLib
# later going down (the "used to have lyrics, now nothing" report).  Misses are
# deliberately NOT cached, so a transient LRCLib outage self-heals on the next
# open instead of pinning a false "no lyrics".  Bounded to cap memory.
_lyrics_cache: dict[str, dict] = {}
_LYRICS_CACHE_MAX = 4000


def _remember_lyrics(track_id: str, result: dict) -> dict:
    """Cache a resolved lyrics payload if it actually has lyrics; return it."""
    if result.get("lyrics"):
        if len(_lyrics_cache) >= _LYRICS_CACHE_MAX:
            _lyrics_cache.clear()          # simple bound — cheap, rare
        _lyrics_cache[track_id] = result
    return result


def lyrics_cache_size() -> int:
    """Number of tracks with cached resolved lyrics (for the admin panel)."""
    return len(_lyrics_cache)


def clear_lyrics_cache() -> int:
    """Empty the resolved-lyrics cache; return how many entries were dropped.
    The next LYRICS open re-resolves online (used by Admin → System → Cache)."""
    n = len(_lyrics_cache)
    _lyrics_cache.clear()
    return n


async def _maybe_writeback_lyrics(track, lyrics_text: str) -> None:
    """If the ``lyrics_writeback`` setting is on and this is a LOCAL file with no
    embedded lyrics, embed the freshly-fetched lyrics into it — in the
    background, off the response path so the LYRICS tab never waits on a tag
    write.  A no-op when the toggle is off, for remote/zip paths, or when the
    file already has lyrics (``write_lyrics`` re-checks and never overwrites)."""
    try:
        from soniqboom.core.data import get_config
        if not await get_config("lyrics_writeback", False):
            return
        path_str = getattr(track, "path", "") or ""
        if not path_str or path_str.startswith(("smb://", "ftp://", "http://", "https://")):
            return                              # only real local files
        if "!" in path_str or "::" in path_str:
            return                              # zip-virtual member — not a writable file
        p = Path(path_str)

        async def _bg() -> None:
            log = logging.getLogger(__name__)
            try:
                from soniqboom.core.metadata import write_lyrics
                wrote = await asyncio.to_thread(write_lyrics, p, lyrics_text)
                if wrote:
                    log.info("Lyrics writeback: embedded fetched lyrics into %s", p.name)
            except Exception:
                log.debug("Lyrics writeback failed for %s", p, exc_info=True)

        asyncio.create_task(_bg())
    except Exception:
        pass


def _strip_release_qualifiers(name: str) -> str:
    """Strip trailing release-NOISE qualifiers from a title/album so a fuzzy
    lyrics lookup matches the canonical release — but only groups that look like
    noise (see ``_QUALIFIER_NOISE_RE``); a meaningful subtitle is kept.  Never
    strips to empty."""
    s = (name or "").strip()
    while True:
        m = _TRAILING_GROUP_RE.search(s)
        if not m or not _QUALIFIER_NOISE_RE.search(m.group(1)):
            break                             # no trailing group, or it's a real subtitle
        stripped = s[:m.start()].strip()
        if not stripped:
            break                             # would empty the title — keep as-is
        s = stripped
    return s


def _norm_title(name: str) -> str:
    """Normalise a title for equality comparison: drop trailing qualifiers,
    lowercase, strip punctuation, collapse whitespace.  So "Shaggathon (Album
    Version)" == "Shaggathon", but "Angel" != "Angel of Death"."""
    s = _strip_release_qualifiers(name).lower()
    s = _re.sub(r"[^\w\s]", " ", s)
    return _re.sub(r"\s+", " ", s).strip()


# A trailing "feat./ft./featuring/with …" credit — stripped so a primary-artist
# comparison treats "Artist feat. X" == "Artist" WITHOUT the substring looseness
# that made "Sia" match "Basia".
_FEAT_RE = _re.compile(r"\s*[\(\[]?\s*\b(feat\.?|featuring|ft\.?|with)\b.*$", _re.IGNORECASE)


def _artist_core(name: str) -> str:
    """Primary artist, lowercased, trailing feat.-clause removed."""
    return _FEAT_RE.sub("", (name or "").strip().lower()).strip()


def _lrclib_lyrics_payload(rec: dict) -> dict | None:
    """Shape an LRCLib record into the endpoint's response, synced over plain.
    Returns None for an instrumental / lyric-less record."""
    if not isinstance(rec, dict):
        return None
    synced = (rec.get("syncedLyrics") or "").strip()
    if synced:
        return {"lyrics": synced, "synced": True, "source": "LRCLib.net"}
    plain = (rec.get("plainLyrics") or "").strip()
    if plain:
        return {"lyrics": plain, "synced": False, "source": "LRCLib.net"}
    return None


def _lrclib_best_match(results, artist: str, title: str, duration: float | None) -> dict | None:
    """Pick the /api/search candidate most likely to be THIS recording: same
    artist, same (cleaned) title, has lyrics, and — when we know the track
    duration — closest in length within a tolerance.  Refuse if everything is
    wildly off so we never show another song's lyrics (refuse over guess)."""
    if not isinstance(results, list):
        return None
    wa = _artist_core(artist)
    wt = _norm_title(title)
    wt_raw = _strip_release_qualifiers(title).strip().lower()

    def _artist_ok(r: dict) -> bool:
        ra = _artist_core(r.get("artistName") or "")
        if not wa or not ra:
            return True                       # can't compare → trust the search's artist filter
        return wa == ra                       # exact primary artist (feat.-clauses stripped)

    def _title_ok(r: dict) -> bool:
        rt = _norm_title(r.get("trackName") or "")
        if not wt:
            # A title that normalises to empty (punctuation-only names — "!!!",
            # "+/-") must NOT become a wildcard matching any same-artist song.
            # Fall back to a raw comparison so "!!!" only matches "!!!".
            rt_raw = _strip_release_qualifiers(r.get("trackName") or "").strip().lower()
            return bool(wt_raw) and wt_raw == rt_raw
        if not rt:
            return False                      # candidate has no comparable title → refuse
        return wt == rt                       # same core title (± qualifiers)

    cands = [
        r for r in results
        if isinstance(r, dict)
        and (r.get("syncedLyrics") or r.get("plainLyrics"))
        and _artist_ok(r)
        and _title_ok(r)
    ]
    if not cands:
        return None

    if duration and duration > 0:
        def _delta(r):
            try:
                return abs(float(r.get("duration") or 0) - float(duration))
            except (TypeError, ValueError):
                return float("inf")
        # Closest duration first; a synced result breaks a tie.
        cands.sort(key=lambda r: (_delta(r), 0 if (r.get("syncedLyrics") or "").strip() else 1))
        best = cands[0]
        if _delta(best) > _LRCLIB_DUR_MAX_S:
            return None                        # nothing close enough — refuse
        return best

    # No duration to disambiguate — prefer a synced candidate, else the first.
    cands.sort(key=lambda r: 0 if (r.get("syncedLyrics") or "").strip() else 1)
    return cands[0]


_ALLOWED_SORT_KEYS = {
    "added", "year", "duration", "bpm",
    "title", "artist", "album_artist", "album", "format",
}


@router.get("")
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
    # Hot path: return the store's dicts serialized straight to JSON bytes with
    # orjson, skipping the per-row TrackMeta construction + Pydantic re-encode
    # (~12 ms/page on a 2000-track page).  The dicts are already the TrackMeta
    # field set (``_meta_dict`` strips ``embedding``), so the response bytes are
    # identical to the response_model path — we just trade this endpoint's
    # OpenAPI schema for the speed.  See data.ft_search_dicts.
    dicts = await ft_search_dicts(
        query, limit=limit, offset=offset,
        sort_by=sort_by, sort_order=sort_order,
    )
    return Response(content=orjson.dumps(dicts), media_type="application/json")


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


@router.post("/meta/batch")
async def batch_tracks(body: dict):
    """Return full track objects for a list of IDs in ONE request.

    Hydrates client-side id lists (e.g. the History Smart view's play-log
    entries) without an N+1 storm of ``GET /api/tracks/{id}`` round-trips —
    ``get_track`` is an in-memory lookup, so N of them in a single request is
    cheap.  Unknown ids are skipped; order follows the request.  Capped to
    keep a pathological request bounded.
    """
    ids = body.get("ids", [])
    if not isinstance(ids, list):
        raise HTTPException(422, "ids must be a list")
    out = []
    for tid in ids[:5000]:
        t = await get_track(tid)
        if t:
            out.append(t)
    return out


@router.get("/{track_id}", response_model=TrackMeta)
async def read_track(track_id: str):
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return track


@router.delete("/{track_id}")
async def remove_track(track_id: str, _user=_Depends(_require_edit)):
    removed = await delete_track(track_id)
    if not removed:
        raise HTTPException(404, "Track not found")
    _lyrics_cache.pop(track_id, None)
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
        # The file's intended default tune (1-based, matching the PSID/SNDH
        # header).  ``None`` when unknown — the client then treats the first
        # tune (wire subsong 0) as the default.  Read the ?subsong= wire index
        # as 0-based: display "Tune K" -> ?subsong=K-1.
        "default_track": getattr(track, "default_track", None),
        # Per-subsong lengths (seconds), indexed by 0-based wire subsong, when
        # an HVSC Songlengths DB is configured; else None — the picker then
        # shows tune numbers without times (graceful degrade).
        "hvsc_lengths": getattr(track, "hvsc_lengths", None),
        # HVSC STIL commentary blob (raw text) for SID files, when configured;
        # else None.  The client parses the ``(#N)`` subtune markers into
        # per-tune titles for the picker and shows the file-level comment as a
        # "STIL" panel.  We ship the raw text (not a parsed structure) so the
        # backend stays agnostic to STIL's freeform layout.
        "stil": getattr(track, "stil", None),
        # SID chip model ("6581" / "8580" / "6581/8580"), read from the PSID
        # header at scan time; None when the header didn't specify one.  Drives
        # a chip badge in the Track-Info header.
        "sid_model": getattr(track, "sid_model", None),
        # Known playback defect flagged at scan ("partial" | "corrupt") + human
        # context — drives the health badge in the info panel + listings.
        "defect": getattr(track, "defect", None),
        "defect_detail": getattr(track, "defect_detail", None),
    }
    return result


# Format names (metadata.FORMAT_NAMES values) whose files libopenmpt can
# parse into a pattern grid.  AHX/HivelyTracker are uade/hvl2wav territory
# and SID/MIDI have no pattern grid — deliberately absent.
_PATTERN_FORMAT_NAMES = frozenset({
    "ProTracker", "ScreamTracker 3", "ScreamTracker 2", "FastTracker 2",
    "Impulse Tracker", "MultiTracker", "OctaMED", "Composer 669",
    "DigiBooster Pro", "UltraTracker", "Farandole", "ASYLUM/DMP",
    "General DigiMusic", "Imago Orpheus", "Oktalyzer", "SoundFX",
    "Grave Composer", "DSIK",
})


def _read_module_bytes(path_str: str) -> bytes | None:
    """Raw module bytes for a local / archive-virtual / remote / composite
    remote-archive path.  Thin alias over the shared canonical resolver
    (``core.source_bytes.read_source_bytes``) so the SID-bytes endpoint, the
    VU backfill, and the core services (hvsc_apply / repair / art_backfill)
    all share ONE ``::``-before-remote implementation.  Blocking — run in an
    executor.  Never raises; returns None on any miss."""
    from soniqboom.core.source_bytes import read_source_bytes
    return read_source_bytes(path_str)


# Extracted pattern payloads, LRU keyed by (track_id, mtime).  The row→time
# map costs one libopenmpt seek per row, which grows with order count
# (~3.7 s for an 82-order S3M) — re-opening the same track's info modal
# shouldn't re-pay it.  The mtime is part of the key so an in-place edit +
# rescan (same path → same track_id, new mtime) invalidates the entry
# instead of serving a stale grid; nothing else clears this cache.  Tiny
# (≤16 payloads).
_PATTERNS_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_PATTERNS_CACHE_MAX = 16
# Coalesce concurrent cache-misses for the SAME (track_id, mtime): the
# extraction is a multi-second libopenmpt seek, so a second request that lands
# mid-flight awaits the first computation instead of re-paying it.
_PATTERNS_INFLIGHT: "dict[tuple, asyncio.Future]" = {}


@router.get("/{track_id}/patterns")
async def get_patterns(track_id: str):
    """Return the tracker pattern grid, order list, row→time map, song
    message and initial tempo for a module — extracted in-process via
    libopenmpt (see ``core/tracker_patterns.py`` for the payload
    contract).  Drives the Track-Info "Patterns" and "Song message"
    sections.  Returns ``{"available": False}`` for non-tracker files,
    unreachable sources, or hosts without libopenmpt."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if (track.format or "") not in _PATTERN_FORMAT_NAMES:
        return {"id": track_id, "available": False, "channels": 0,
                "order": [], "patterns": []}
    cache_key = (track_id, getattr(track, "mtime", None))
    cached = _PATTERNS_CACHE.get(cache_key)
    if cached is not None:
        _PATTERNS_CACHE.move_to_end(cache_key)
        return cached
    # No await between the cache-miss above and this in-flight check, so the two
    # lookups are atomic under asyncio: a concurrent request either sees the
    # cache populated (owner fully done) or joins this future (owner still in
    # the executor) — never recomputes redundantly.
    inflight = _PATTERNS_INFLIGHT.get(cache_key)
    if inflight is not None:
        return await inflight
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _PATTERNS_INFLIGHT[cache_key] = fut
    try:
        data = await loop.run_in_executor(None, _read_module_bytes, track.path)
        if not data:
            payload = {"id": track_id, "available": False, "channels": 0,
                       "order": [], "patterns": []}
        else:
            from soniqboom.core.tracker_patterns import extract_patterns
            payload = await loop.run_in_executor(None, extract_patterns, data)
            payload["id"] = track_id
            if payload.get("available"):
                _PATTERNS_CACHE[cache_key] = payload
                while len(_PATTERNS_CACHE) > _PATTERNS_CACHE_MAX:
                    _PATTERNS_CACHE.popitem(last=False)
        fut.set_result(payload)
    except Exception as exc:  # propagate to the owner AND any joined waiters
        fut.set_exception(exc)
    finally:
        # Pop and set_result happen with no await between them, so no coroutine
        # can observe a populated cache with a stale in-flight entry.
        _PATTERNS_INFLIGHT.pop(cache_key, None)
    return await fut


@router.get("/{track_id}/vu")
async def get_vu_sidecar(track_id: str, subsong: int = Query(0, ge=0, le=1024)):
    """Return the binary VUMR sidecar for a rendered tracker module.

    ``subsong`` (0-based wire index, default 0) selects the tune: a
    multi-subsong UADE/tracker file renders a distinct sidecar per tune,
    so the meters match the tune actually playing rather than always
    showing tune 0.  The frontend passes the current subsong when the
    playing track carries one (mirroring the ``?subsong=`` it threads onto
    the stream URL); plain playback omits it → the default tune 0.

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

    sidecar = get_vu_sidecar_path(track_id, subsong)
    if sidecar is None:
        # Lazy-backfill path.  Only attempt for known tracker formats
        # (we don't want to spin up libopenmpt against a 10 GB FLAC).
        sidecar = await _try_backfill_vu_sidecar(track, track_id, subsong)

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


def _is_sid_format(track) -> bool:
    """True if *track* is a C64 SID (format primary name ``SID``) — the gate
    for both the raw-bytes and VU-upload endpoints.  Amiga SidMon ``*.sid``
    carries a uade format name, not ``SID``."""
    return (str(track.format or "").split("/")[0].strip() == "SID")


@router.get("/{track_id}/sid")
async def get_sid_bytes(track_id: str, _user=_Depends(_require_user)):
    """Serve the raw C64 SID container bytes for the client-side WASM VU worker.

    The worker (``frontend/js/vu-sid-worker.js``) renders the tune in-browser to
    produce the per-voice VU sidecar, offloading the server's 3-pass sidplayfp
    render.  This is the ONLY route that returns SID source bytes (normal
    playback always transcodes to WAV), so it is tightly gated: any signed-in
    user (read), SID-format tracks only, and a PSID/RSID magic re-check on the
    resolved bytes (415 for an Amiga SidMon ``*.sid``).  Bytes are immutable and
    tiny (a few KB) → a plain immutable-cached Response, no range needed."""
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not _is_sid_format(track):
        raise HTTPException(415, "Not a C64 SID track")
    # Bound a mislabelled/replaced file BEFORE reading it all into RAM.  For a
    # plain local path we can stat cheaply; remote/zip sources fall through to
    # the post-read length check (those members are tiny + fetched anyway).
    _pstr = str(track.path)
    if "::" not in _pstr and not _pstr.startswith(
            ("smb://", "ftp://", "http://", "https://", "webdav://", "webdavs://")):
        try:
            if Path(_pstr).stat().st_size > 1024 * 1024:   # SIDs are < 64 KB
                raise HTTPException(415, "File too large to be a SID")
        except OSError:
            pass
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _read_module_bytes, track.path)
    if not data:
        raise HTTPException(404, "SID source unreachable")
    if len(data) > 1024 * 1024:                 # remote/zip belt-and-braces
        raise HTTPException(415, "File too large to be a SID")
    if data[:4] not in (b"PSID", b"RSID"):
        raise HTTPException(415, "Not a C64 SID (missing PSID/RSID magic)")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/{track_id}/vu")
async def upload_vu_sidecar(
    track_id: str,
    request: Request,
    subsong: int = Query(0, ge=0, le=1024),
    content_hash: str = Query(..., min_length=64, max_length=64),
    user=_Depends(_require_edit),
):
    """Persist a client-rendered VUMR sidecar into the shared SID cache slot.

    The browser worker computes the per-voice VU (offloading the server's
    sidplayfp render) and uploads it here; we drop it beside the cached SID WAV
    so the existing ``GET /vu`` serves it to every later play, any client.

    Trust model: the VU sidecar drives only the cosmetic per-voice meter, and
    the uploader is a ``require_edit`` user, so this is low-stakes.  The real
    containment (all reject BEFORE any write) is: (1) a 256 KB hard ceiling
    stream-read — no global body limit exists and a chunked upload has no
    Content-Length; (2) strict VUMR structural validation incl. exact length;
    (3) SID must be 3-channel; (4) the write target is SERVER-derived from the
    ``sid``-format cache slot (never a client-supplied path), so an upload can
    only land beside a genuine C64-SID render — 425 when that slot isn't cached
    yet (client retries).  ``content_hash`` is a transit-integrity check only
    (the same client computes body+hash, so it is NOT an anti-forgery gate).
    Idempotent + server-prefers: skip if a ``.vu`` already exists."""
    import hashlib
    from soniqboom.core.openmpt_vu import (
        parse_and_validate_vumr, write_sidecar_bytes,
    )
    from soniqboom.core.conversion_cache import get_sid_wav_path_for_upload

    # (1) size cap — a chunked upload carries no Content-Length, so the header
    # check alone is bypassable; stream-read with a HARD ceiling and abort the
    # instant we cross it, before the whole body is ever in memory.
    _MAX = 256 * 1024
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX:
        raise HTTPException(413, "VU sidecar too large")
    _buf = bytearray()
    async for _chunk in request.stream():
        _buf += _chunk
        if len(_buf) > _MAX:
            raise HTTPException(413, "VU sidecar too large")
    raw = bytes(_buf)

    # (2)+(3) structural validation.
    try:
        channels, _rate, _frames = parse_and_validate_vumr(raw)
    except ValueError as exc:
        raise HTTPException(422, f"invalid VUMR: {exc}")
    if channels != 3:
        raise HTTPException(422, "SID VU sidecar must have 3 channels")

    # (4) content-hash integrity.
    if hashlib.sha256(raw).hexdigest() != content_hash.lower():
        raise HTTPException(422, "content_hash mismatch")

    # track existence + SID gate (belt-and-braces; the slot scan also gates).
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not _is_sid_format(track):
        raise HTTPException(415, "Not a C64 SID track")

    # (5) SERVER-derived destination slot.
    wav_path = get_sid_wav_path_for_upload(track_id, subsong)
    if wav_path is None:
        raise HTTPException(425, "SID WAV not cached yet — retry")
    vu_path = wav_path.with_suffix(".vu")

    # Idempotent, server-wins: never overwrite an existing sidecar.
    if vu_path.exists():
        return Response(status_code=204)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_sidecar_bytes, vu_path, raw)
    return Response(status_code=204)


# Bound concurrent client SID-WAV uploads (A2): each body is spooled to disk, but
# the semaphore + the Content-Length gate stop N concurrent ~50 MB uploads from
# hammering a low-power box's RAM / disk / CPU all at once.
_SID_UPLOAD_SEM = asyncio.Semaphore(2)


def _validate_sid_wav(header: bytes, total_len: int, expect_data_bytes: int) -> str | None:
    """Validate a client-rendered SID WAV from its 44-byte canonical header + the
    total streamed byte count (the body is spooled to disk, never held in RAM).
    Canonical RIFF/WAVE, mono / 44100 / 16-bit PCM, and a data chunk within ~1 s
    of the expected length (rejects a truncated WAV under a full-length key).
    The WASM worker writes exactly this canonical layout (vu-sid-worker.js
    buildWav), so a non-canonical upload is refused."""
    import struct
    if len(header) < 44:
        return "shorter than a WAV header"
    if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        return "not a RIFF/WAVE"
    if header[12:16] != b"fmt ":
        return "no fmt chunk at offset 12"
    audio_format, channels, rate = struct.unpack("<HHI", header[20:28])
    bits = struct.unpack("<H", header[34:36])[0]
    if audio_format != 1:
        return "not PCM"
    if channels != 1:
        return f"not mono ({channels} channels)"
    if rate != 44100:
        return f"sample rate {rate} != 44100"
    if bits != 16:
        return f"{bits}-bit != 16-bit"
    if header[36:40] != b"data":
        return "no data chunk at offset 36"
    data_bytes = struct.unpack("<I", header[40:44])[0]
    if abs(data_bytes - expect_data_bytes) > 88200 + 4096:
        return f"data length {data_bytes} not within 1 s of expected {expect_data_bytes}"
    if total_len < 44 + data_bytes - 4096:
        return "body truncated (fewer bytes than the declared data chunk)"
    return None


@router.post("/{track_id}/sid-audio")
async def upload_sid_audio(
    track_id: str,
    request: Request,
    subsong: int = Query(0, ge=0, le=1024),
    duration: int = Query(..., ge=1, le=3600),
    wav_sha256: str = Query(..., min_length=64, max_length=64),
    user=_Depends(_require_admin),
):
    """Cache-warm: persist a CLIENT-rendered SID WAV into the exact conversion-
    cache slot the SERVER render would occupy, so every later play (any client,
    cast, offline, Subsonic) streams it via the normal cached-file path with
    ZERO ``sidplayfp`` render.  This is what lets a low-power box never re-render
    a SID once a capable browser has played it once.

    Companion to ``POST /vu`` (the VU sidecar upload); same trust tier and the
    same "reject BEFORE any write" containment:
      (1) ``_require_admin`` — the stored WAV is served verbatim to EVERY later
          consumer (anonymous stream, cast, offline SW, Subsonic), and SID PCM is
          non-deterministic so no hash can prove a client sent the real render;
          admin-only bounds that audio-injection capability to the operator.
          (Non-admin plays still get a warm cache via the server's own render.)
      (2) ``_is_sid_format`` gate → 415 (Amiga SidMon ``*.sid`` renders under uade).
      (3) ``target_dur`` is RE-DERIVED server-side, IDENTICAL to ``render_status``
          / the SID stream branch; a client ``duration`` that disagrees → 409, so
          the warmed slot always matches the key ``/stream`` later requests (a
          mismatch would be invisible — the server would simply re-render).
      (4) ``sid_warm_eligible()``: only when ALL SID fidelity settings are default,
          so the key carries no fidelity suffix AND the WASM render (which has no
          chip-model/filter/curve/digiboost setter) actually matches the server
          → 409 otherwise.  Blocks cross-chip-model cache poisoning.
      (5) streamed body with a HARD ceiling derived from ``target_dur``; WAV
          structural validation (mono/44100/16-bit, data length within ~1 s).
      (6) ``wav_sha256`` = transit-integrity only (the same client computes
          body+hash; NOT anti-forgery — SID PCM is non-deterministic so no hash
          can prove fidelity; the authenticated ADMIN user IS the trust boundary).
      (7) per-key lock + ``get_cached`` re-check → idempotent, server-wins skip.
    """
    from soniqboom.config import settings
    from soniqboom.core.conversion_cache import (
        _cache_key, get_cached, store_cached, _lock_for,
        get_conversion_cache_dir, sid_warm_eligible,
    )

    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    if not _is_sid_format(track):
        raise HTTPException(415, "Not a C64 SID track")

    # (3) RE-derive target_dur exactly like render_status / the SID stream branch.
    target_dur = int(getattr(settings, "sid_default_duration", 300) or 300)
    meta = track.__dict__ if hasattr(track, "__dict__") else {}
    hvsc_lengths = meta.get("hvsc_lengths") or []
    if hvsc_lengths and 0 <= subsong < len(hvsc_lengths):
        target_dur = int(round(float(hvsc_lengths[subsong])))
    elif meta.get("duration") and float(meta.get("duration") or 0) > 0:
        target_dur = int(round(float(meta.get("duration"))))
    target_dur = max(5, min(int(target_dur), 3600))
    if int(duration) != target_dur:
        raise HTTPException(409, f"duration mismatch — server target is {target_dur}s")

    # (4) fidelity gate — refuse warming a slot the WASM can't reproduce.
    if not sid_warm_eligible():
        raise HTTPException(409, "server SID fidelity is non-default — cannot cache-warm")

    # (5) Bound RAM + concurrency (A2): require Content-Length (reject chunked so
    # we can't be forced to buffer an unbounded body), cap the size, gate
    # concurrent uploads, and SPOOL the streamed body straight to a temp file so
    # the ~50 MB WAV never sits in RAM.  mono/44100/16-bit = 88200 bytes/s.
    _EXPECT = target_dur * 88200
    _MAX = min(_EXPECT + 2 * 88200 + 4096, 64 * 1024 * 1024)
    clen = request.headers.get("content-length")
    if not (clen and clen.isdigit()):
        raise HTTPException(411, "Content-Length required")
    if int(clen) > _MAX:
        raise HTTPException(413, "SID WAV too large")

    full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)
    async with _SID_UPLOAD_SEM:
        import hashlib
        import tempfile
        cdir = get_conversion_cache_dir() / "sid"
        cdir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=str(cdir))
        digest = hashlib.sha256()
        header = bytearray()
        total = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                async for _chunk in request.stream():
                    total += len(_chunk)
                    if total > _MAX:
                        raise HTTPException(413, "SID WAV too large")
                    digest.update(_chunk)
                    if len(header) < 44:
                        header += _chunk[: 44 - len(header)]
                    fh.write(_chunk)
            if digest.hexdigest() != wav_sha256.lower():
                raise HTTPException(422, "wav_sha256 mismatch")
            err = _validate_sid_wav(bytes(header), total, _EXPECT)
            if err:
                raise HTTPException(422, f"invalid SID WAV: {err}")
            # (7) per-key lock: idempotent, server-wins — the spooled temp file is
            # moved into the keyed slot, or dropped if a render beat us to it.
            async with _lock_for(full_key):
                if await get_cached(full_key) is not None:
                    return Response(status_code=204)      # already warmed — first-wins
                await store_cached(full_key, "sid", Path(tmp))
                tmp = None                                # moved into the cache
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return Response(status_code=204)


# Tracker-only formats — gating the lazy backfill so we don't try to
# open a FLAC with libopenmpt.  Matches stream.py's _TRACKER_EXTS but
# imported lazily to avoid a cycle.
_VU_BACKFILL_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mptm", ".med", ".oct",
    ".669", ".dbm", ".dsm", ".far", ".gdm", ".imf", ".mtm",
    ".okt", ".sfx", ".stm", ".ult", ".wow",
}


async def _try_backfill_vu_sidecar(track, track_id: str, subsong: int = 0):
    """One-shot VU extraction against a track's source file.

    ``subsong`` (0-based wire index) selects the tune to render so a
    multi-subsong module backfills the meters for the tune being played,
    not always tune 0.  Written to a per-subsong slot so tune 0's and
    tune 3's backfills never overwrite each other.

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
    # Resolve raw module bytes via the shared resolver, the SAME helper the
    # SID-bytes endpoint uses.  It partitions the ``::`` archive tail FIRST,
    # so a composite remote-archive path (``ftp://…foo.zip::inner.mod``)
    # fetches the OUTER ``.zip`` into the local remote-cache — a cache HIT
    # returns the copy playback already downloaded, no re-fetch — and THEN
    # extracts the member before handing bytes to libopenmpt.
    #
    # The previous hand-rolled block here checked ``startswith("ftp://")``
    # BEFORE the ``"::" in path`` case, so a remote-zip module never reached
    # the archive extractor: it fed either a bogus ``…zip::member`` remote path
    # (fetch fails) or the raw ZIP container (unparseable) to libopenmpt →
    # None → 404 → FFT fallback.  ``subsong`` is intentionally NOT applied here;
    # the bytes are subsong-agnostic and the tune is selected downstream in
    # ``extract_vu``.
    path_str = track.path
    src_bytes = await asyncio.get_event_loop().run_in_executor(
        None, _read_module_bytes, path_str,
    )
    if not src_bytes:
        log.debug("VU backfill found no bytes for %s", path_str)
        return None

    loop = asyncio.get_event_loop()
    # extract_vu takes -1 = "libopenmpt default subsong" (what _render_tracker
    # uses for subsong 0 — it only passes --subsong for N>0), and an explicit
    # index for N>0.  Mirror that so the backfilled sidecar matches the streamed
    # render's sidecar frame-for-frame.
    vu_subsong = subsong if subsong > 0 else -1
    result = await loop.run_in_executor(
        None, lambda: openmpt_vu.extract_vu(src_bytes, subsong=vu_subsong),
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
                # This subsong's cached WAV specifically — write the .vu next to
                # it so eviction stays uniform.  Exact match (not startswith) so
                # subsong 3's backfill doesn't land on subsong 0's WAV.
                if cache_key == f"{track_id}__sub{subsong}" and entry.get("format_type") == "tracker":
                    candidate = Path(entry["path"]).with_suffix(".vu")
                    break
        if candidate is None:
            # No cached WAV for this subsong yet — synthesize a sidecar-only slot
            # keyed per-subsong (the ``__novubackfill`` suffix keeps it from
            # colliding with a real render slot).  Lives in the same cache dir so
            # it gets evicted alongside other tracker assets.
            base = _cache_path(f"{track_id}__sub{subsong}__novubackfill", "tracker")
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


# ── Online lyrics providers (responsiveness-ordered fallback chain) ──────────
# LRCLib is the richer source (synced + fuzzy + duration-gated); lyrics.ovh is
# the fallback (plain-only, exact artist+title, no key).  Both are probed at
# startup — and lazily re-probed — and the FASTER one is tried FIRST, so when
# LRCLib is degraded (it 502'd during testing) the quicker source answers
# without the user waiting on the slow one.  Every result still passes the same
# confidence gate downstream, so the order changes only latency, never which
# lyrics get attached.
_LYRICS_OVH_TIMEOUT_S = 8.0
_PROBE_TIMEOUT_S = 5.0
_PROBE_INTERVAL_S = float(os.environ.get("SONIQBOOM_LYRICS_PROBE_INTERVAL_S", "1800"))  # 30 min


async def _provider_lrclib(client, artist, title, album, duration):
    """LRCLib: exact ``/api/get`` first, then the duration-gated fuzzy
    ``/api/search``.  Each call is guarded so a slow/failed exact still lets the
    fuzzy run; a total failure returns None → the resolver tries the next
    provider."""
    log = logging.getLogger(__name__)
    try:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = str(int(duration))
        resp = await client.get("https://lrclib.net/api/get", params=params,
                                timeout=_LRCLIB_GET_TIMEOUT_S)
        if resp.status_code == 200:
            hit = _lrclib_lyrics_payload(resp.json())
            if hit:
                return hit
    except Exception:
        log.debug("LRCLib exact lookup failed", exc_info=True)
    try:
        resp = await client.get(
            "https://lrclib.net/api/search",
            params={"artist_name": artist, "track_name": _strip_release_qualifiers(title)},
            timeout=_LRCLIB_SEARCH_TIMEOUT_S,
        )
        if resp.status_code == 200:
            cand = _lrclib_best_match(resp.json(), artist, title, duration)
            if cand:
                return _lrclib_lyrics_payload(cand)
    except Exception:
        log.debug("LRCLib fuzzy lookup failed", exc_info=True)
    return None


async def _provider_lyrics_ovh(client, artist, title, album, duration):
    """lyrics.ovh: one exact ``/v1/{artist}/{title}`` lookup (plain only, no
    key).  Uses the primary artist (feat.-clause dropped) + de-noised title so a
    store-tagged "(Remastered)" / "feat. X" still matches its canonical entry."""
    a = _FEAT_RE.sub("", artist).strip()
    t = _strip_release_qualifiers(title)
    if not a or not t:
        return None
    url = (
        "https://api.lyrics.ovh/v1/"
        f"{urllib.parse.quote(a, safe='')}/{urllib.parse.quote(t, safe='')}"
    )
    resp = await client.get(url, timeout=_LYRICS_OVH_TIMEOUT_S)
    if resp.status_code == 200:
        ly = ((resp.json() or {}).get("lyrics") or "").strip()
        if ly:
            return {"lyrics": ly, "synced": False, "source": "lyrics.ovh"}
    return None


# name, provider fn, and a representative "is it up + how fast" probe URL.
_LYRICS_PROVIDERS = [
    {"name": "LRCLib", "fn": _provider_lrclib,
     "probe": "https://lrclib.net/api/get?artist_name=Coldplay&track_name=Yellow"},
    {"name": "lyrics.ovh", "fn": _provider_lyrics_ovh,
     "probe": "https://api.lyrics.ovh/v1/Coldplay/Yellow"},
]
_lyrics_order = list(range(len(_LYRICS_PROVIDERS)))   # provider indices, primary first
_last_probe_ts = 0.0
_probe_in_flight = False


async def probe_lyrics_providers() -> None:
    """Measure each provider's response latency and rank them fastest-first, so
    the faster source is tried first.  A provider that errors/times out sorts
    LAST (used only as a fallback).  Called at startup and lazily re-run every
    ``_PROBE_INTERVAL_S`` — provider health drifts (LRCLib was fast, then 5–13 s
    within a day)."""
    global _lyrics_order, _last_probe_ts, _probe_in_flight
    _probe_in_flight = True
    _last_probe_ts = time.monotonic()
    client = _get_lrclib_client()
    log = logging.getLogger(__name__)

    async def _latency(url: str) -> float:
        t = time.monotonic()
        try:
            await client.get(url, timeout=_PROBE_TIMEOUT_S)
            return time.monotonic() - t          # any HTTP response = "up"
        except Exception:
            return float("inf")                  # down → sort last

    try:
        lats = await asyncio.gather(*(_latency(p["probe"]) for p in _LYRICS_PROVIDERS))
        _lyrics_order = sorted(range(len(_LYRICS_PROVIDERS)), key=lambda i: lats[i])
        log.info(
            "Lyrics providers ranked by responsiveness: %s",
            ", ".join(
                f"{_LYRICS_PROVIDERS[i]['name']}="
                + ("down" if lats[i] == float("inf") else f"{lats[i]:.2f}s")
                for i in _lyrics_order
            ),
        )
    except Exception:
        log.debug("lyrics provider probe failed", exc_info=True)
    finally:
        _probe_in_flight = False


async def _resolve_online_lyrics(artist, title, album, duration):
    """Try the online providers fastest-first; return the first confident hit.
    Kicks a non-blocking background re-probe when the ranking is stale."""
    if not _probe_in_flight and (time.monotonic() - _last_probe_ts) > _PROBE_INTERVAL_S:
        asyncio.create_task(probe_lyrics_providers())
    client = _get_lrclib_client()
    log = logging.getLogger(__name__)
    for i in list(_lyrics_order):
        prov = _LYRICS_PROVIDERS[i]
        try:
            hit = await prov["fn"](client, artist, title, album, duration)
            if hit:
                return hit
        except Exception:
            log.debug("lyrics provider %s failed", prov["name"], exc_info=True)
    return None


@router.get("/{track_id}/lyrics")
async def get_lyrics(track_id: str):
    """Return lyrics: embedded tags first, then the online providers
    (LRCLib + lyrics.ovh) tried fastest-first (see ``probe_lyrics_providers``)."""
    # 0. Serve a previously-resolved result instantly (and independently of
    # LRCLib's current health) — see ``_lyrics_cache``.
    cached = _lyrics_cache.get(track_id)
    if cached is not None:
        return cached

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
        return _remember_lyrics(track_id, {"lyrics": embedded, "synced": is_synced, "source": "Embedded tags"})

    # 2. Online fallback chain — LRCLib + lyrics.ovh, tried fastest-first.  Each
    # provider applies the same confidence gate, so the ranking only affects
    # latency (and, when the faster source is lyrics.ovh, plain-vs-synced).
    artist = track.artist or track.album_artist or ""
    title  = track.title or ""
    album  = track.album or ""
    if not (artist and title):
        return {"lyrics": None, "source": None}

    online = await _resolve_online_lyrics(artist, title, album, track.duration)
    if online:
        await _maybe_writeback_lyrics(track, online["lyrics"])
        return _remember_lyrics(track_id, online)

    return {"lyrics": None, "synced": False, "source": None}


def _sid_target_duration(track, subsong: int = 0) -> int:
    """Per-tune SID render length, IDENTICAL to the stream path's logic
    (stream.py ~5262).  Prefer HVSC per-subsong length, then the stored
    duration, then the global default; clamp 5..3600.

    The waveform MUST use this same value (and the same cache key) as the
    audio render — otherwise it renders the SID at ``sid_default_duration``
    (e.g. 300 s) while the tune is ~54 s, producing 54 s of audio + 246 s
    of trailing silence.  The 200 waveform bars then spread across 300 s,
    so the real signal lands in only the leftmost ~18 % of the seek bar.
    """
    from soniqboom.config import settings
    meta = track.__dict__ if hasattr(track, "__dict__") else (track or {})
    target = settings.sid_default_duration
    hvsc_lengths = meta.get("hvsc_lengths") or []
    if hvsc_lengths and 0 <= subsong < len(hvsc_lengths):
        target = int(round(float(hvsc_lengths[subsong])))
    elif meta.get("duration") and float(meta["duration"]) > 0:
        target = int(round(float(meta["duration"])))
    return max(5, min(int(target), 3600))


async def _waveform_from_conversion_cache(track_id: str, path_str: str, ext: str,
                                          *, sid_duration: int | None = None):
    """Get WAV path for a converted format via the conversion cache.

    Uses get_or_render() which has thundering-herd prevention — if the
    stream endpoint is currently rendering the same track, this waits for it
    instead of starting a duplicate render.

    ``sid_duration`` (SID only) MUST match the stream path's per-tune length
    so the waveform reuses the already-rendered stream WAV instead of
    rendering a separate default-duration one padded with silence.
    """
    import tempfile
    from pathlib import Path as _Path
    from soniqboom.api.stream import (
        _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS,
        _ADLIB_EXTS, _GME_EXTS_STREAM,
        _SNDH_EXTS, _YM_EXTS, _SC68_EXTS, _PSF_STREAM_EXTS,
        _render_sid, _render_midi, _render_tracker, _render_uade, _render_hvl,
        _render_adlib, _render_imf, _render_gme,
        _render_sndh, _render_ym, _render_sc68, _render_psf,
        _dsf_is_dreamcast, _is_c64_sid,
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
        render_dur = None
        if ext in _SID_EXTS and _is_c64_sid(path):
            # C64 SID → sidplayfp.  Render (and cache-key) at the SAME
            # per-tune duration the stream uses, so this reuses the stream's
            # WAV instead of rendering a default-length one padded with
            # silence (which squashed the waveform into the left ~18% of the
            # seek bar).  A non-C64 ``.sid`` (Amiga SidMon) is NOT gated here
            # and falls through to the uade branch below — matching the
            # stream, which routes it to _render_uade (stream.py:5253).
            fmt, sf_path = "sid", None
            render_dur = sid_duration
            render_fn = lambda: _render_sid(path, subsong=0, duration=render_dur)
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
        elif ext == ".imf":
            # .imf is overloaded: Imago Orpheus (openmpt) vs id/Apogee AdLib
            # (AdPlug).  _render_imf sniffs the content and routes; cache key
            # matches the stream path's format_type="imf".  Must precede the
            # tracker fallback (.imf is also in _TRACKER_EXTS for scanning).
            fmt, sf_path = "imf", None
            render_fn = lambda: _render_imf(path, subsong=0)
        elif ext in _ADLIB_EXTS:
            # AdLib / OPL2 FM (ROL/CMF/D00/RAD/…) via AdPlug — openmpt can't
            # decode these, so the tracker fallback below would just fail.
            fmt, sf_path = "adlib", None
            render_fn = lambda: _render_adlib(path, subsong=0)
        elif ext in _GME_EXTS_STREAM:
            # Console chiptunes (NSF/SPC/GBS/VGM/AY/KSS/…) via libgme.
            fmt, sf_path = "gme", None
            render_fn = lambda: _render_gme(path, subsong=0)
        elif ext in _PSF_STREAM_EXTS or (
                ext == ".dsf" and _dsf_is_dreamcast(path)):
            # PSF console rips (+ Dreamcast .dsf) via zxtune123 — matches
            # the stream path's format_type="psf".
            fmt, sf_path = "psf", None
            render_fn = lambda: _render_psf(path)
        elif ext in _SNDH_EXTS:
            fmt, sf_path = "sndh", None
            render_fn = lambda: _render_sndh(path, subsong=0)
        elif ext in _YM_EXTS:
            fmt, sf_path = "ym", None
            render_fn = lambda: _render_ym(path)
        elif ext in _SC68_EXTS:
            fmt, sf_path = "sc68", None
            render_fn = lambda: _render_sc68(path, subsong=0)
        elif ext not in _TRACKER_EXTS:
            # Exotic-Amiga prefix/suffix names (mdat.song, song.fc13) reach
            # here with an unknown ext — matches format_type="uade".
            fmt, sf_path = "uade", None
            render_fn = lambda: _render_uade(path, subsong=0)
        else:  # tracker
            fmt, sf_path = "tracker", None
            render_fn = lambda: _render_tracker(path, subsong=0)

        wav_path, _ = await get_or_render(
            track_id=track_id, format_type=fmt, subsong=0,
            render_fn=render_fn, soundfont_path=sf_path,
            duration=render_dur,
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
    from soniqboom.api.stream import (
        _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS,
        _ADLIB_EXTS, _GME_EXTS_STREAM,
    )

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
    # Route the scrubber waveform render exactly like playback: an AdLib
    # extension wins over a uade name-token collision, and an archive routing
    # suffix (``STAR.AMD.star``) is stripped so AMUSIC ``.amd`` files get their
    # AdLib waveform instead of a failed uade render — including library rows
    # scanned before ``.amd`` was recognized (mirrors ``stream._render_ident``).
    from soniqboom.api.stream import _render_ident
    ext, _ = _render_ident(path_str)

    loop = asyncio.get_event_loop()

    # ── Converted formats: compute waveform from conversion-cache WAV ────
    from soniqboom.api.stream import (
        _SNDH_EXTS, _YM_EXTS, _SC68_EXTS, _PSF_STREAM_EXTS,
    )
    from soniqboom.core import uade_formats as _uadef
    # ``.dsf`` is ambiguous (Sony DSD vs Dreamcast rip) — the scanner already
    # content-sniffed it, so trust the STORE's format field here (no file
    # access needed; works for remote paths too).
    _dreamcast = ext == ".dsf" and (
        getattr(track, "format", "") or "").startswith("DSF")
    _base_name = path_str.split("::")[-1].rsplit("/", 1)[-1]
    if (ext in _SID_EXTS or ext in _MIDI_EXTS or ext in _TRACKER_EXTS
            or ext in _UADE_EXTS or ext in _HVL_EXTS
            or ext in _ADLIB_EXTS or ext in _GME_EXTS_STREAM
            or ext in _SNDH_EXTS or ext in _YM_EXTS or ext in _SC68_EXTS
            or ext in _PSF_STREAM_EXTS or _dreamcast
            or _uadef.classify(_base_name) is not None):
        # SID: pass the per-tune duration so the waveform reuses the stream's
        # render (see _sid_target_duration).  Other converted formats render
        # full-length by nature and need no duration hint.
        _sid_dur = _sid_target_duration(track) if ext in _SID_EXTS else None
        try:
            wav_path = await _waveform_from_conversion_cache(
                track_id, path_str, ext, sid_duration=_sid_dur)
        except HTTPException as exc:
            # A render that EXITED nonzero (502 — e.g. fluidsynth can't parse
            # this .mid, an AdLib tune with no resolvable instruments) means the
            # file is unrenderable; degrade to a blank waveform like every other
            # branch instead of throwing a 502 error toast.  Re-raise genuine
            # transient/availability errors (501 renderer-missing, 503/504
            # timeout) so a real misconfiguration stays visible.
            if exc.status_code == 502:
                wav_path = None
            else:
                raise
        result = await _compute_waveform_safe(str(wav_path) if wav_path else "")
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
        if cached_path:
            src_for_waveform = str(cached_path)
        elif '::' in path_str:
            # Transcoded-format member inside a LOCAL or REMOTE archive with no
            # cached transcode yet.  ffmpeg can't read the ``archive.zip::member``
            # virtual path — nor our composite ``ftp://host/scan:/…zip::member``
            # form — directly, and the plain-remote branch below would hand the
            # whole composite string to get_cache().fetch (no such remote file →
            # HTTP 502).  Partition the ``::`` tail FIRST and extract the member
            # to a local temp, exactly like the plain-audio branch does further
            # down.
            local = await _resolve_zip_member_to_local(path_str)
            if local is None:
                raise HTTPException(404, "Waveform not available for this format")
            try:
                result = await _compute_waveform_safe(str(local))
                stored, response = _normalise_waveform(result)
                if not _waveform_is_blank(stored):
                    await store_waveform(track_id, stored)
                return {"waveform": response}
            finally:
                try:
                    local.unlink()
                except Exception:
                    pass
        elif path_str.startswith(("smb://", "ftp://")):
            # Remote transcoded source (e.g. a .m4a/.aac on an FTP/SMB share)
            # with no cached transcode yet: ffmpeg can't open our internal
            # ``ftp://host/scanroot:/rel`` pseudo-URL — it returns all-zeros and
            # the waveform shows blank.  Fetch a local copy first (same as the
            # remote branch below) instead of handing ffmpeg the pseudo-URL.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(path_str)
            source = get_source(scan_root) if remote_path else None
            if source is None:
                raise HTTPException(503, "Network share unavailable")
            try:
                src_for_waveform = str(get_cache().fetch(scan_root, remote_path, source))
            except Exception as exc:        # noqa: BLE001 — surface fetch failure
                raise HTTPException(502, f"Could not fetch remote file: {exc}")
        else:
            src_for_waveform = path_str
        result = await _compute_waveform_safe(src_for_waveform)
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

    # ── Plain audio inside a ZIP: extract the member, then compute ───────
    # ffmpeg can't read the ``archive.zip::member`` virtual path directly, so
    # pull the member out to a local temp file first (works for local and
    # remote archives), exactly like the converted-format path already does.
    if '::' in path_str:
        local = await _resolve_zip_member_to_local(path_str)
        if local is None:
            raise HTTPException(404, "Waveform not available for this format")
        try:
            result = await _compute_waveform_safe(str(local))
            stored, response = _normalise_waveform(result)
            if not _waveform_is_blank(stored):
                await store_waveform(track_id, stored)
            return {"waveform": response}
        finally:
            try:
                local.unlink()
            except Exception:
                pass

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
        result = await _compute_waveform_safe(str(local_path))
        stored, response = _normalise_waveform(result)
        if not _waveform_is_blank(stored):
            await store_waveform(track_id, stored)
        return {"waveform": response}

    # ── Standard local files: compute directly from source ───────────────
    result = await _compute_waveform_safe(track.path)
    stored, response = _normalise_waveform(result)
    if not _waveform_is_blank(stored):
        await store_waveform(track_id, stored)
    return {"waveform": response}


# ── Ratings ──────────────────────────────────────────────────────────────────

@router.put("/{track_id}/rating")
async def update_rating(track_id: str, body: dict, _user=_Depends(_require_edit)):
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
