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
    # Tell DLNA controllers the tree changed (bumps SystemUpdateID + NOTIFYs
    # any GENA subscribers).  Lazy import to avoid an import cycle.
    try:
        from soniqboom.api.dlna_upnp import notify_library_changed
        notify_library_changed()
    except Exception:                       # noqa: BLE001 — never break a scan
        pass


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


@router.get("/formats")
async def list_formats(request: Request):
    """Per-format track counts — drives the library Galaxy visualization."""
    cached = _cache_get("formats")
    if cached is None:
        cached = get_store().aggregate_formats()
        _cache_set("formats", cached)
    return _etag_response(request, "formats", cached)
