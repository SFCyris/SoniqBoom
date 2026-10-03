# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Library management — scan dirs, WebSocket progress, aggregations."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from soniqboom.api.tracks import json_route, public_tracks
from soniqboom.config import settings
from soniqboom.core.data import (
    delete_scan_dir, rebuild_indexes,
    list_hash_lookups, list_scan_dirs, path_hash, resolve_hash,
    upsert_scan_dir,
)
from soniqboom.core.scanner import get_progress, start_scan
from soniqboom.core.store import get_store
from soniqboom.api.users import require_admin

router = APIRouter(prefix="/library", tags=["library"])

_ws_clients: set[WebSocket] = set()
# Parallel dict so the auth re-check on every broadcast tick knows which
# user_id each open socket belongs to (without having to re-parse the
# cookie + hit the user store on every tick).  Populated on accept,
# cleared on disconnect.
_ws_user_id: dict[WebSocket, str] = {}


def _verify_ws_session(ws: WebSocket) -> bool:
    """Re-check that the user behind ``ws`` is still valid.

    Cheap dict lookup against the in-memory session store — runs once per
    broadcast tick so a session revoked / user disabled mid-stream
    immediately stops receiving scan-progress events.  Pre-bootstrap
    installs (no users registered yet) keep the anonymous-open behaviour.
    """
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        return True
    if not store.has_any():
        return True
    cookie = ws.cookies.get("sb_session") if hasattr(ws, "cookies") else None
    if not cookie:
        return False
    user = store.lookup_session(cookie)
    return user is not None and user.enabled


async def _broadcast(data: dict) -> None:
    """Push *data* to every connected library WebSocket in parallel.

    Sends were previously serial — a single slow / back-pressured client
    blocked every other listener from receiving scan-progress ticks.  Each
    send now has its own 2 s timeout so one stuck socket can't stall the
    whole fan-out.

    Each tick also re-verifies the WebSocket's session; if revoked (the
    user was disabled / had their role removed / explicit logout) we
    close the socket with code ``4401`` before sending.  Without this a
    long-lived WS could keep streaming events well after its owner's
    privileges were revoked.
    """
    if not _ws_clients:
        return

    async def _send(ws):
        try:
            if not _verify_ws_session(ws):
                try:
                    await asyncio.wait_for(ws.close(code=4401), timeout=1.0)
                except Exception:
                    pass
                return ws
            await asyncio.wait_for(ws.send_json(data), timeout=2.0)
            return None
        except Exception:
            return ws

    results = await asyncio.gather(
        *(_send(ws) for ws in list(_ws_clients)),
        return_exceptions=True,
    )
    dead = {r for r in results if r is not None and not isinstance(r, BaseException)}
    if dead:
        _ws_clients.difference_update(dead)
        for ws in dead:
            _ws_user_id.pop(ws, None)


# ── Aggregation cache (event-driven: cached until scan invalidates) ───────────

# Entry is ``(mutation_seq, data)`` — the HTTP cache is gated on the store's
# ``_mutation_seq`` (same freshness signal the store's own aggregate cache uses)
# so ANY track mutation — not just a scan-complete/reindex/filter-toggle — makes
# a stale legend recompute.  Without the seq gate the Galaxy legend could serve
# counts from before a retag / set-primary / metadata backfill and disagree with
# the (always-fresh) ``/api/tracks?format=X`` drill-down.
_AGG_CACHE: dict[str, tuple[int, list]] = {}
# Parallel dict: cache_key → (etag, raw_json_bytes, gzipped_json_bytes | None).
# Computed lazily on first HTTP hit so we don't pay the hash+serialise cost when
# the cache is populated only via internal helpers; the gzip member on the first
# 200 to a gzip-accepting client (see ``_etag_response``).  One tuple per key, so
# the gzip is dropped in lock-step with the body + ETag it was made from — every
# invalidation pops the whole entry — and can never be served for another body.
_AGG_ETAGS: dict[str, tuple[str, bytes, bytes | None]] = {}

# Mirrors main.py's ``_SelectiveGZipMiddleware(minimum_size=1000)`` /
# ``compresslevel=6``: below the threshold the middleware sends the body as is,
# so we do too (it adds no Vary there either).
_GZIP_MIN_BYTES = 1000
_GZIP_LEVEL = 6


def _current_agg_seq() -> int:
    try:
        return int(getattr(get_store(), "_mutation_seq", 0))
    except Exception:
        return 0


def _cache_get(key: str) -> list | None:
    entry = _AGG_CACHE.get(key)
    if entry is None:
        return None
    seq, data = entry
    if seq != _current_agg_seq():          # a mutation happened → stale
        _AGG_CACHE.pop(key, None)
        _AGG_ETAGS.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: list) -> None:
    _AGG_CACHE[key] = (_current_agg_seq(), data)
    # Drop any stale etag entry; it will be recomputed on the next request.
    _AGG_ETAGS.pop(key, None)


def invalidate_agg_cache() -> None:
    """Call after a scan completes to force fresh aggregations."""
    _AGG_CACHE.clear()
    _AGG_ETAGS.clear()
    # Tell DLNA controllers the tree changed (bumps SystemUpdateID + NOTIFYs
    # any GENA subscribers).  Lazy import to avoid an import cycle.
    try:
        from soniqboom.api.dlna_upnp import notify_library_changed
        notify_library_changed()
    except Exception:                       # noqa: BLE001 — never break a scan
        pass


def _dedup_on(store) -> bool:
    """Whether the web library legends should show primary-only (deduped)
    counts — driven by the ``filter_duplicates`` config.  This is the ONE place
    the config is mapped to the aggregates' ``primary_only`` parameter, so the
    dedup preference reaches the web browse/Galaxy views without leaking into
    the Subsonic / DLNA consumers of the same store methods."""
    return bool(store.get_config("filter_duplicates", False))


def _tagged_denominator(store) -> int:
    """Total-track count to subtract the tagged buckets from when sizing the
    "[No Artist]" / "[No Album Artist]" catch-all.

    Must match the mode the aggregate was computed in: with ``filter_duplicates``
    on, ``aggregate_artists`` counts only primaries, so the untagged remainder
    has to be measured against the primary-only total too — otherwise the
    hidden duplicate copies (which have artists) would inflate the untagged
    bucket and it would list tracks the drill-down never shows.
    """
    if _dedup_on(store):
        return store.primary_track_count()
    return store.track_count()


def _accepts_gzip(request: Request) -> bool:
    # The exact test Starlette's GZipMiddleware applies, so a client gets the
    # encoding it got before this body was memoised.
    return "gzip" in request.headers.get("Accept-Encoding", "")


def _etag_response(request: Request, cache_key: str, result: list) -> Response:
    """Return either 304 Not Modified or a JSONResponse with an ETag header.

    The ETag is derived from a stable-ordered JSON representation of the
    result and cached in ``_AGG_ETAGS`` keyed by ``cache_key`` so repeated
    hits don't re-hash.  Scan invalidation clears both caches in lock-step
    via :func:`invalidate_agg_cache`.

    The gzipped body is memoised in the same entry: the app's gzip middleware
    used to re-compress the cached body on EVERY 200 (~4 ms on the loop for an
    850 KB album list).  A gzip-accepting client now gets the stored gzip with
    ``Content-Encoding: gzip`` + ``Vary: Accept-Encoding`` — Starlette's
    GZipMiddleware passes a response that already carries Content-Encoding
    through untouched — and any other client the plain body, which the
    middleware sends as before.
    """
    cached = _AGG_ETAGS.get(cache_key)
    if cached is None:
        payload = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
        etag = hashlib.md5(payload).hexdigest()  # noqa: S324 — non-cryptographic
        cached = _AGG_ETAGS[cache_key] = (etag, payload, None)
    etag, payload, gz = cached

    quoted = f'"{etag}"'
    inm = request.headers.get("if-none-match")
    headers = {
        "ETag": quoted,
        # Private + must-revalidate: the client may cache the body, but must
        # ask us for a fresh etag each time.  The middleware checks for an
        # existing Cache-Control and leaves this alone.
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    compressible = len(payload) >= _GZIP_MIN_BYTES
    # Parse the If-None-Match header per RFC 7232 instead of using substring
    # ``etag in inm`` — substring match falsely 304s when one md5 is a prefix
    # of another in a multi-tag header, or when a token happens to appear
    # inside another value.  Also strip the optional weak-validator ``W/``
    # prefix so clients that hedge (per RFC 7232) still hit the 304 path.
    def _normalise_etag(token: str) -> str:
        t = token.strip()
        if t.startswith(("W/", "w/")):
            t = t[2:]
        return t.strip('"')

    if inm:
        candidates = {_normalise_etag(e) for e in inm.split(",") if e.strip()}
        if etag in candidates or "*" in candidates:
            if compressible:
                # A 304 carries the Vary its 200 would have (RFC 9110 §15.4.5).
                headers["Vary"] = "Accept-Encoding"
            return Response(status_code=304, headers=headers)
    if compressible and _accepts_gzip(request):
        if gz is None:
            gz = gzip.compress(payload, compresslevel=_GZIP_LEVEL, mtime=0)
            # Stored only into the entry it was made from (never over one an
            # invalidation dropped or replaced meanwhile).
            if _AGG_ETAGS.get(cache_key) is cached:
                _AGG_ETAGS[cache_key] = (etag, payload, gz)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
        return Response(content=gz, media_type="application/json", headers=headers)
    return Response(content=payload, media_type="application/json", headers=headers)


# ── Scan dirs ─────────────────────────────────────────────────────────────────

@json_route(router, "/by-dir")
async def tracks_in_directory(
    path: str = Query(..., description="Exact directory path"),
    recursive: bool = Query(False),
    limit: int = Query(1000, ge=1, le=5000),
):
    """Return all tracks whose parent directory equals *path*.
    If recursive=True, returns all tracks under the scan root that contains this path.

    TrackMeta-shaped rows (``tracks.public_track``) encoded with orjson — the
    same JSON as the ``TrackMeta`` list ``data.tracks_by_dir`` /
    ``tracks_by_scan_root`` build, without a model per row (5000 rows: ~550 ms
    → ~35 ms, half of it the store's own filter).  A stored row the model
    rejects is skipped instead of failing the whole listing with a 500.
    """
    store = get_store()
    if recursive:
        rows = store.filter_tracks(scan_root_hash=path_hash(path), limit=limit)
    else:
        rows = store.filter_tracks(dir_hash=path_hash(path), limit=limit)
    return public_tracks(rows)


@router.get("/hashes")
async def get_all_hashes():
    """Return all hash→path mappings (SoniqBoom:Hash:*). Useful for export/import."""
    return await list_hash_lookups()


@router.get("/hashes/{h}")
async def resolve_hash_value(h: str):
    """Resolve a single hash to its original path value."""
    value = await resolve_hash(h)
    if value is None:
        raise HTTPException(404, f"Hash not found: {h}")
    return {"hash": h, "value": value}


@router.post("/reindex")
async def reindex(_admin=Depends(require_admin)):
    """Rebuild the in-memory indexes (use after schema changes).
    Existing track documents are preserved; the index is rebuilt automatically.
    """
    report = await rebuild_indexes()   # diagnoses drift + heals it (atomic swap)
    # A store write landing during the build makes it skip its swap
    # (``skipped``) rather than install indexes that miss the write; retry a
    # couple of times, then report the skip (admin_reindex does the same).
    for _ in range(2):
        # Only a write that raced the build is worth another try; a running
        # scan keeps the batch for minutes.
        if report.get("skipped") != "concurrent-mutation":
            break
        report = await rebuild_indexes()
    from soniqboom.core import index_health
    if not report.get("skipped"):
        index_health.record(report, kind="reindex", healed=True)
    # The HTTP aggregation cache (/library/formats etc.) is NOT keyed on the
    # store mutation seq, so a rebuild alone would leave it serving pre-reindex
    # counts — e.g. the Galaxy legend showing "Ken's AdLib · 41" while the
    # freshly-rebuilt live index is queried by the filter.  Invalidate it here.
    invalidate_agg_cache()
    return {
        "reindexed": not report.get("skipped"),
        "reindex_skipped": report.get("skipped"),
        "drift_detected": not report.get("index_ok", True),
        "drift": report.get("mismatches", []),
    }


@router.get("/dirs")
async def get_library_dirs():
    """Return all registered scan directories, each with a live-ish reachability
    ``status`` ('ok' / 'unavailable').  The probe is bounded + cached, so a
    stalled mount marks the folder unavailable instead of hanging the request or
    making it look empty."""
    from soniqboom.core.data import refresh_scan_dir_availability
    return {"dirs": await refresh_scan_dir_availability()}


@router.post("/dirs")
async def add_library_dir(body: dict, _admin=Depends(require_admin)):
    """Add a directory to the scan list and immediately scan it."""
    raw = body.get("path", "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    path = str(Path(raw).expanduser().resolve())
    if not Path(path).is_dir():
        raise HTTPException(400, f"Directory not found: {path}")
    await upsert_scan_dir(path)

    # Store alias in config file if provided
    alias = body.get("alias", "").strip()
    if alias:
        from soniqboom.config import load_local_conf, save_local_conf, settings
        conf = load_local_conf()
        aliases = conf.get("folder_aliases", {})
        aliases[path] = alias
        conf["folder_aliases"] = aliases
        save_local_conf(conf)
        settings.folder_aliases = aliases

    # Automatically scan the newly added folder in the background
    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    await start_scan([path], on_progress=_progress_cb)
    try:
        from soniqboom.core import watcher
        await watcher.add_root(path)
    except Exception:
        __import__("logging").getLogger(__name__).exception("watcher.add_root failed for %s", path)

    return {"dirs": await list_scan_dirs()}


@router.delete("/dirs")
async def remove_library_dir(body: dict, _admin=Depends(require_admin)):
    """Remove a directory from the scan list."""
    raw = body.get("path", "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    path = str(Path(raw).expanduser().resolve())
    await delete_scan_dir(path)
    try:
        from soniqboom.core import watcher
        from soniqboom.core.scanner import forget_root
        forget_root(path)
        await watcher.remove_root(path)
    except Exception:
        __import__("logging").getLogger(__name__).exception("watcher.remove_root failed for %s", path)
    return {"dirs": await list_scan_dirs()}


# ── Scan ──────────────────────────────────────────────────────────────────────

@router.post("/scan")
async def scan_library(body: dict | None = None, _admin=Depends(require_admin)):
    """Start a library scan.

    - If body contains {"dirs": [...]}, scan those specific dirs.
    - Otherwise scan all registered dirs.
    """
    if body and body.get("dirs"):
        dirs = [str(Path(d).expanduser().resolve()) for d in body["dirs"]]
    else:
        scan_dir_docs = await list_scan_dirs()
        dirs = [d["path"] for d in scan_dir_docs]

    if not dirs:
        raise HTTPException(400, "No scan directories registered. Add one via POST /api/library/dirs")

    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    task = await start_scan(dirs, on_progress=_progress_cb)
    return {"started": True, "dirs": dirs}


@router.get("/scan/status")
async def scan_status():
    return get_progress().to_dict()


# ── WebSocket ─────────────────────────────────────────────────────────────────

def _ws_auth_ok(ws: WebSocket) -> tuple[bool, str | None]:
    """Gate a WebSocket on the session cookie.  Pre-bootstrap installs
    (no users at all) keep the old anonymous-open behaviour so the
    initial setup UI still works.

    Returns ``(allowed, user_id)`` so the caller can record the user_id
    in the per-user open-socket registry.  ``user_id`` is None for
    anonymous-bootstrap connections (registry no-ops on None).
    """
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        return True, None
    if not store.has_any():
        return True, None
    cookie = ws.cookies.get("sb_session") if hasattr(ws, "cookies") else None
    if not cookie:
        return False, None
    user = store.lookup_session(cookie)
    if user is None or not user.enabled:
        return False, None
    return True, user.id


@router.websocket("/ws")
async def library_ws(ws: WebSocket):
    allowed, user_id = _ws_auth_ok(ws)
    if not allowed:
        await ws.close(code=4401)  # custom code: unauthorized
        return
    await ws.accept()
    _ws_clients.add(ws)
    if user_id is not None:
        _ws_user_id[ws] = user_id
        # Cross-module registry so admin demote/disable can iterate this
        # user's open sockets and slam them shut.
        try:
            from soniqboom.api.users import register_open_ws
            register_open_ws(user_id, ws)
        except Exception:
            pass
    try:
        await ws.send_json({"event": "scan_progress", **get_progress().to_dict()})
        # Initial snapshot of any in-flight transcodes so a client that
        # connects mid-render learns the current determinate progress
        # immediately (otherwise its badge would spin until the next ~1 Hz
        # push).  Imported lazily to avoid a stream↔library load-order
        # cycle; skip entries already marked ready (nothing useful to push
        # and they're pruned server-side after a TTL anyway).
        try:
            from soniqboom.api.stream import _TRANSCODE_PROGRESS
            for track_id, entry in list(_TRANSCODE_PROGRESS.items()):
                if entry.get("ready"):
                    continue
                await ws.send_json({
                    "event": "transcode_progress",
                    "track_id": track_id,
                    "percent": float(entry.get("percent") or 0.0),
                    "eta_seconds": entry.get("eta_seconds"),
                    "ready": False,
                })
        except Exception:
            # Snapshot is best-effort — never let it abort the WS accept.
            pass
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        _ws_user_id.pop(ws, None)
        if user_id is not None:
            try:
                from soniqboom.api.users import unregister_open_ws
                unregister_open_ws(user_id, ws)
            except Exception:
                pass


# ── Aggregations ──────────────────────────────────────────────────────────────


@router.get("/artists")
async def list_artists(request: Request):
    cached = _cache_get("artists")
    if cached is None:
        store = get_store()
        # ``list(...)`` copies the store's MEMOISED aggregate before we append
        # the synthetic "[No Artist]" bucket — appending to the returned list
        # in place would corrupt the store's cache (the same object is served
        # to Subsonic / DLNA) until the next mutation.
        cached = list(store.aggregate_artists(primary_only=_dedup_on(store)))
        untagged = _tagged_denominator(store) - sum(d["count"] for d in cached)
        if untagged > 0:
            cached.append({"artist": "", "count": untagged, "label": "[No Artist]"})
        _cache_set("artists", cached)
    return _etag_response(request, "artists", cached)


@router.get("/album-artists")
async def list_album_artists(request: Request):
    cached = _cache_get("album_artists")
    if cached is None:
        store = get_store()
        cached = list(store.aggregate_album_artists(primary_only=_dedup_on(store)))
        untagged = _tagged_denominator(store) - sum(d["count"] for d in cached)
        if untagged > 0:
            cached.append({"album_artist": "", "count": untagged, "label": "[No Album Artist]"})
        _cache_set("album_artists", cached)
    return _etag_response(request, "album_artists", cached)


@router.get("/albums")
async def list_albums(
    request: Request,
    artist: str | None = None,
    album_artist: str | None = None,
):
    cache_key = f"albums:{artist}:{album_artist}"
    cached = _cache_get(cache_key)
    if cached is None:
        store = get_store()
        rows = store.aggregate_albums(
            artist=artist, album_artist=album_artist,
            primary_only=_dedup_on(store),
        )
        # New dicts (don't mutate the store's memoised album rows in place).
        cached = [
            {**d, "artist": artist or "", "album_artist": album_artist or ""}
            for d in rows
        ]
        _cache_set(cache_key, cached)
    return _etag_response(request, cache_key, cached)


@router.get("/genres")
async def list_genres(request: Request):
    cached = _cache_get("genres")
    if cached is None:
        store = get_store()
        cached = store.aggregate_genres(primary_only=_dedup_on(store))
        _cache_set("genres", cached)
    return _etag_response(request, "genres", cached)


@router.get("/years")
async def list_years(request: Request):
    cached = _cache_get("years")
    if cached is None:
        store = get_store()
        cached = store.aggregate_years(primary_only=_dedup_on(store))
        _cache_set("years", cached)
    return _etag_response(request, "years", cached)


@router.get("/formats")
async def list_formats(request: Request):
    """Per-format track counts + coarse family — drives the library Galaxy view.

    Each entry is ``{format, count, family}`` where ``family`` is the coarse
    browse-by-family bucket (trackers/chiptune/lossless/lossy/other) the Galaxy
    family filter groups by.
    """
    cached = _cache_get("formats")
    if cached is None:
        store = get_store()
        cached = store.aggregate_formats(primary_only=_dedup_on(store))
        _cache_set("formats", cached)
    return _etag_response(request, "formats", cached)


@router.get("/scene-groups")
async def list_scene_groups(request: Request):
    """Demoscene groups with track counts — the browse facet over the Demozoo
    ``scene_group`` enrichment.  Empty until the Demozoo apply has run."""
    cached = _cache_get("scene_groups")
    if cached is None:
        store = get_store()
        cached = store.aggregate_scene_group(primary_only=_dedup_on(store))
        _cache_set("scene_groups", cached)
    return _etag_response(request, "scene_groups", cached)
