# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Library management — scan dirs, WebSocket progress, aggregations."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from soniqboom.config import settings
from soniqboom.core.data import (
    delete_scan_dir, rebuild_indexes,
    list_hash_lookups, list_scan_dirs, resolve_hash,
    tracks_by_dir, tracks_by_scan_root, upsert_scan_dir,
)
from soniqboom.core.scanner import get_progress, start_scan
from soniqboom.core.store import get_store

router = APIRouter(prefix="/library", tags=["library"])

_ws_clients: set[WebSocket] = set()


async def _broadcast(data: dict) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ── Aggregation cache (event-driven: cached until scan invalidates) ───────────

_AGG_CACHE: dict[str, list] = {}
# Parallel dict: cache_key → (etag, raw_json_bytes).  Computed lazily on first
# HTTP hit so we don't pay the hash+serialise cost when the cache is populated
# only via internal helpers.
_AGG_ETAGS: dict[str, tuple[str, bytes]] = {}


def _cache_get(key: str) -> list | None:
    return _AGG_CACHE.get(key)


def _cache_set(key: str, data: list) -> None:
    _AGG_CACHE[key] = data
    # Drop any stale etag entry; it will be recomputed on the next request.
    _AGG_ETAGS.pop(key, None)


def invalidate_agg_cache() -> None:
    """Call after a scan completes to force fresh aggregations."""
    _AGG_CACHE.clear()
    _AGG_ETAGS.clear()


def _etag_response(request: Request, cache_key: str, result: list) -> Response:
    """Return either 304 Not Modified or a JSONResponse with an ETag header.

    The ETag is derived from a stable-ordered JSON representation of the
    result and cached in ``_AGG_ETAGS`` keyed by ``cache_key`` so repeated
    hits don't re-hash.  Scan invalidation clears both caches in lock-step
    via :func:`invalidate_agg_cache`.
    """
    cached = _AGG_ETAGS.get(cache_key)
    if cached is None:
        payload = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
        etag = hashlib.md5(payload).hexdigest()  # noqa: S324 — non-cryptographic
        _AGG_ETAGS[cache_key] = (etag, payload)
    else:
        etag, payload = cached

    quoted = f'"{etag}"'
    inm = request.headers.get("if-none-match")
    headers = {
        "ETag": quoted,
        # Private + must-revalidate: the client may cache the body, but must
        # ask us for a fresh etag each time.  The middleware checks for an
        # existing Cache-Control and leaves this alone.
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    if inm and etag in inm:
        return Response(status_code=304, headers=headers)
    return Response(content=payload, media_type="application/json", headers=headers)


# ── Scan dirs ─────────────────────────────────────────────────────────────────

@router.get("/by-dir")
async def tracks_in_directory(
    path: str = Query(..., description="Exact directory path"),
    recursive: bool = Query(False),
    limit: int = Query(1000, ge=1, le=5000),
):
    """Return all tracks whose parent directory equals *path*.
    If recursive=True, returns all tracks under the scan root that contains this path.
    """
    if recursive:
        return await tracks_by_scan_root(path, limit=limit)
    return await tracks_by_dir(path, limit=limit)


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
async def reindex():
    """Rebuild the in-memory indexes (use after schema changes).
    Existing track documents are preserved; the index is rebuilt automatically.
    """
    await rebuild_indexes()
    return {"reindexed": True}


@router.get("/dirs")
async def get_library_dirs():
    """Return all registered scan directories."""
    return {"dirs": await list_scan_dirs()}


@router.post("/dirs")
async def add_library_dir(body: dict):
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

    return {"dirs": await list_scan_dirs()}


@router.delete("/dirs")
async def remove_library_dir(body: dict):
    """Remove a directory from the scan list."""
    raw = body.get("path", "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    path = str(Path(raw).expanduser().resolve())
    await delete_scan_dir(path)
    return {"dirs": await list_scan_dirs()}


# ── Scan ──────────────────────────────────────────────────────────────────────

@router.post("/scan")
async def scan_library(body: dict | None = None):
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

@router.websocket("/ws")
async def library_ws(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_json({"event": "scan_progress", **get_progress().to_dict()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ── Aggregations ──────────────────────────────────────────────────────────────


@router.get("/artists")
async def list_artists(request: Request):
    cached = _cache_get("artists")
    if cached is None:
        store = get_store()
        cached = store.aggregate_artists()
        untagged = store.track_count() - sum(d["count"] for d in cached)
        if untagged > 0:
            cached.append({"artist": "", "count": untagged, "label": "[No Artist]"})
        _cache_set("artists", cached)
    return _etag_response(request, "artists", cached)


@router.get("/album-artists")
async def list_album_artists(request: Request):
    cached = _cache_get("album_artists")
    if cached is None:
        store = get_store()
        cached = store.aggregate_album_artists()
        untagged = store.track_count() - sum(d["count"] for d in cached)
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
        rows = store.aggregate_albums(artist=artist, album_artist=album_artist)
        cached = []
        for d in rows:
            d["artist"] = artist or ""
            d["album_artist"] = album_artist or ""
            cached.append(d)
        _cache_set(cache_key, cached)
    return _etag_response(request, cache_key, cached)


@router.get("/genres")
async def list_genres(request: Request):
    cached = _cache_get("genres")
    if cached is None:
        cached = get_store().aggregate_genres()
        _cache_set("genres", cached)
    return _etag_response(request, "genres", cached)


@router.get("/years")
async def list_years(request: Request):
    cached = _cache_get("years")
    if cached is None:
        cached = get_store().aggregate_years()
        _cache_set("years", cached)
    return _etag_response(request, "years", cached)
