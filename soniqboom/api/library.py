# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Library management — scan dirs, WebSocket progress, aggregations."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import secrets
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
    data = _with_lib_gen(data)

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


async def send_to_user(user_id: str, data: dict) -> int:
    """Push *data* to the open library WebSockets of ONE account only — the
    channel for anything carrying that user's own data (plays, history):
    ``_broadcast`` reaches every signed-in page.  As ``_broadcast``: a 2 s
    timeout per send, a socket whose session was revoked is closed (4401),
    and a socket that failed is dropped.  Returns the sockets sent to."""
    if not user_id:
        return 0
    targets = [ws for ws, uid in list(_ws_user_id.items()) if uid == user_id]

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
    results = await asyncio.gather(*(_send(ws) for ws in targets), return_exceptions=True)
    dead = {r for r in results if r is not None and not isinstance(r, BaseException)}
    if dead:
        _ws_clients.difference_update(dead)
        for ws in dead:
            _ws_user_id.pop(ws, None)
    return sum(1 for r in results if r is None)


_USER_PUSHES: set = set()


def push_to_user(user_id: str, data: dict) -> None:
    """``send_to_user`` in the background (on the running loop): a caller
    answering a request (a Subsonic scrobble) never waits for a slow page."""
    if not user_id or not any(uid == user_id for uid in list(_ws_user_id.values())):
        return
    t = asyncio.get_running_loop().create_task(send_to_user(user_id, data))
    _USER_PUSHES.add(t)
    t.add_done_callback(_USER_PUSHES.discard)


def _with_lib_gen(data: dict) -> dict:
    """A ``scan_progress`` frame with ``running`` false (a scan's completion,
    its embedding phase, a silent scan's end) and a ``catalogue_changed``
    event (after which a page re-reads its counts, open view and folder tree)
    carry the :func:`library_generation` they left the library at.  So does
    ``remote_new_tracks`` (sent right after its share check's end frame): a
    page that already re-read at that generation only shows the toast."""
    ev = data.get("event")
    if "lib_gen" not in data and (ev in ("catalogue_changed", "remote_new_tracks")
                                  or (ev == "scan_progress" and not data.get("running"))):
        return {**data, "lib_gen": library_generation()}
    return data


async def broadcast_track_updated(track_id: str) -> None:
    """Tell every open page one track's stored fields changed (a catalogue
    tune read on its first play): pages holding it re-read it."""
    await _broadcast({"event": "track_updated", "id": track_id})


async def broadcast_catalogue_changed(root: str) -> None:
    """Tell every open page a catalogue root's tracks changed (built, refreshed,
    removed): the folder tree and the open view refresh."""
    await _broadcast({"event": "catalogue_changed", "root": root})


async def broadcast_server_restarting() -> None:
    """Tell every open page the server restarts in place now (the source
    install's restart, right before it starts refusing requests): a play
    that fails while it is away is waited out (``player.noteServerRestart``)."""
    await _broadcast({"event": "server_restarting"})


async def broadcast_library_replaced() -> None:
    """Tell every open page the whole library was replaced (a backup import):
    the same event as a catalogue change — the folder tree, the open view and
    the counts are re-read (``root`` is empty: no one root)."""
    await _broadcast({"event": "catalogue_changed", "root": ""})


async def broadcast_silent_scan_end(dirs: "list[str] | None" = None) -> None:
    """The ``running: false`` frame of a scan that sends no progress (the
    folder watcher's rescans, a share's freshness poll): marked ``silent`` —
    no "Done" badge — with the folders it covered (``last_dirs``), so an open
    folder among them is re-read in place, the counts follow and the page
    records the generation it reached (``lib_gen``)."""
    from soniqboom.core.scanner import get_progress as _gp
    frame = {"event": "scan_progress", **_gp().to_dict(),
             "running": False, "embedding": False, "silent": True}
    if dirs is not None:
        frame["last_dirs"] = list(dirs)
    await _broadcast(frame)


async def broadcast_catalogue_tracks_left(root: str, folder: str, ids: list,
                                          reasons: "dict | None" = None) -> None:
    """Tell every open page that rows of a catalogue root left the library
    (a package that holds nothing playable): its lists and counts re-read in
    place, the folder tree stays as it is, and a queue / Track Info holding
    one of ``ids`` shows why (``reasons``: id → sentence)."""
    await _broadcast({"event": "catalogue_tracks_left", "root": root, "folder": folder,
                      "ids": list(ids), "reasons": dict(reasons or {})})


async def broadcast_catalogue_folder_changed(root: str, folder: str) -> None:
    """Tell every open page that ONE folder of a catalogue root changed (a
    package opened into its tunes): a view showing it re-reads it; the folder
    tree stays as it is."""
    await _broadcast({"event": "catalogue_folder_changed", "root": root, "folder": folder})


# ── Aggregation cache (event-driven: cached until scan invalidates) ───────────

# Entry is ``(browse_seq, data)`` — the HTTP cache is gated on the store's
# ``_browse_seq`` (same freshness signal the store's own aggregate cache uses)
# so ANY track mutation a legend can show — not just a scan-complete/reindex/
# filter-toggle — makes a stale legend recompute, while writes no legend reads
# (a play-time duration backfill, a health mark, cover art) keep it.  Without
# the seq gate the Galaxy legend could serve counts from before a retag /
# set-primary / metadata backfill and disagree with the (always-fresh)
# ``/api/tracks?format=X`` drill-down.
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


def _current_agg_seq():
    """What an aggregate cache entry is valid for: the store's browse
    sequence (``_browse_seq``), plus — while "Tracks on unavailable drives:
    Hide" or "Tracks that can't be played: Hide" leaves tracks out, or a
    duplicate's copy stands in for a primary that can't play — the
    availability tokens (``hide_token`` / ``swap_token``: the counts follow
    them; a mark moves no ``_browse_seq``)."""
    try:
        store = get_store()
        seq = int(getattr(store, "_browse_seq", getattr(store, "_mutation_seq", 0)))
    except Exception:
        return 0
    try:
        from soniqboom.core import availability
        tok = availability.hide_token(store)
        swap_tok = availability.swap_token(store)
    except Exception:
        tok = swap_tok = None
    return seq if tok is None and swap_tok is None else (seq, tok, swap_tok)


# Library generation (``library_generation``): this server run, the number of
# ``invalidate_agg_cache`` calls, the aggregate cache key above and the store's
# write and duration sequences.
_BOOT_ID = secrets.token_hex(4)
_AGG_EPOCH = 0


def new_server_run() -> None:
    """A server run starts (``main.startup``; the bundled app runs one after
    another in the same process): a new run id for :func:`library_generation`
    — a page connected to the previous run re-reads — and an empty aggregate
    cache (the library is loaded again)."""
    global _BOOT_ID
    _BOOT_ID = secrets.token_hex(4)
    _AGG_CACHE.clear()
    _AGG_ETAGS.clear()


def _duration_seq() -> int:
    """The store's count of duration-only writes (a render-only format's real
    length found at its first play): the lists show durations, so the
    generation follows them — the aggregates (no duration in them) don't."""
    try:
        return int(getattr(get_store(), "_duration_seq", 0))
    except Exception:
        return 0


def _mutation_seq() -> int:
    """The store's write sequence: it also moves for fields the rows show but
    the browse sequence leaves out (health / defect badges, tune counts, track
    and disc numbers, surround channels)."""
    try:
        return int(getattr(get_store(), "_mutation_seq", 0))
    except Exception:
        return 0


def library_generation() -> str:
    """An opaque token that moves whenever the browse lists, counts or the
    All-Tracks list may have changed: a track write a list can show (the
    store's browse sequence, ``_browse_seq``), a duration write
    (``_duration_seq``), an availability change that the lists
    follow, an ``invalidate_agg_cache`` call (scan, reindex, "Hide duplicates"
    toggle, album-cache reset, a library loaded or imported) or a server run
    (``new_server_run``).  A page compares the WebSocket connect snapshot's
    ``lib_gen`` with the one it last synced to (the previous snapshot, the
    last ``scan_progress`` frame with ``running`` false or ``catalogue_changed``
    event) — and, on its first snapshot, with the ``X-SB-Lib-Gen`` its first
    data requests were served at (``LIB_GEN_HEADER``)."""
    return (f"{_BOOT_ID}.{_AGG_EPOCH}.{_current_agg_seq()!r}.{_mutation_seq()}"
            f".{_duration_seq()}".replace(" ", ""))


# Response header with the :func:`library_generation` a browse request was
# served at (taken before its handler read anything) — ``main``'s request
# gate adds it to GET requests under ``LIB_GEN_PATHS``.
LIB_GEN_HEADER = "X-SB-Lib-Gen"
LIB_GEN_PATHS = ("/api/library/", "/api/tracks", "/api/fstree/", "/api/search/", "/api/smart/")


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
    global _AGG_EPOCH
    _AGG_EPOCH += 1
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


def _hidden(store):
    """The ids the web lists leave out ("Tracks on unavailable drives: Hide",
    "Tracks that can't be played: Hide"), or None — the browse counts leave
    them out too."""
    from soniqboom.core import availability
    return availability.hidden(store)


def _swap(store):
    """The duplicate groups the deduped web lists show by another member while
    their primary can't play (``core.availability.primary_swap``), or None —
    the browse counts count those members instead."""
    from soniqboom.core import availability
    return availability.primary_swap(store, dedup=_dedup_on(store))


def _untagged_count(store, field: str) -> int:
    """Size of the "[No Artist]" / "[No Album Artist]" catch-all: the rows its
    drill-down (``GET /api/tracks?untagged=<field>``) lists, in the same mode —
    duplicates hidden or not, "Tracks on unavailable drives: Hide" or not.
    Counted directly, not as "all tracks minus the tagged buckets": a track
    tagged "A • B" sits in two artist buckets, so that subtraction came out
    short and the bucket vanished while its drill-down still listed tracks."""
    return store.untagged_count(field, primary_only=_dedup_on(store),
                                exclude=_hidden(store), swap=_swap(store))


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
    # The HTTP aggregation cache (/library/formats etc.) is keyed on the
    # store's browse sequence (``_browse_seq``), which a swap moves; a skipped
    # rebuild moves nothing.  Invalidated here either way, so no pre-reindex
    # count (the Galaxy legend's "Ken's AdLib · 41" while the rebuilt index
    # answered the filter) is served after a reindex.
    invalidate_agg_cache()
    return {
        "reindexed": not report.get("skipped"),
        "reindex_skipped": report.get("skipped"),
        "drift_detected": not report.get("index_ok", True),
        "drift": report.get("mismatches", []),
    }


@router.get("/availability")
async def get_availability():
    """Settings → "Tracks on unavailable drives" and what it acts on: ``mode``
    (mark / hide / show), ``token`` (changes whenever the set of tracks that
    can't play right now, or the mode, changes — and, with "Tracks that can't
    be played: Hide", whenever those tracks change — clients re-read their
    lists then; also pushed as the ``availability_changed`` WebSocket event),
    ``unavailable`` (how many tracks can't play right now) and the offline
    ``roots`` (path, kind drive / share / catalogue, tracks, unavailable);
    ``unplayable_mode`` (mark / hide) and ``cant_play`` (how many tracks are
    reported broken, corrupt or refused by their player)."""
    from soniqboom.core import availability
    return availability.state()


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
        settings.folder_aliases = dict(save_local_conf(conf).get("folder_aliases") or {})

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
        # The state on connect, not an event: ``snapshot`` marks it so a page
        # does not take ``running: false`` for a scan that just finished, and
        # ``lib_gen`` tells a reconnecting page whether the library changed
        # since it last synced.
        # A scan running now ends with a ``running: false`` frame, also one
        # that sends no progress (``silent`` — scanner.note_scan_shown).
        snap = get_progress().to_dict()
        if snap.get("running"):
            from soniqboom.core.scanner import note_scan_shown
            note_scan_shown()
        await ws.send_json({"event": "scan_progress", **snap,
                            "snapshot": True, "lib_gen": library_generation()})
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


@router.get("/counts")
async def library_counts(request: Request):
    """The sidebar badge numbers in one small reply (ETag / 304).

    ``tracks`` / ``visible`` as ``GET /api/tracks/count``; ``artists``,
    ``album_artists``, ``albums``, ``genres``, ``years`` = the rows of the
    matching list endpoint, without its "[No Artist]"-style bucket row
    (``label``); ``scene_groups`` = the rows of ``/library/scene-groups``.
    Same mode as the lists ("Hide duplicates", "Tracks on unavailable
    drives: Hide", "Tracks that can't be played: Hide")."""
    cached = _cache_get("counts")
    if cached is None:
        from soniqboom.core.data import track_count
        store = get_store()
        kw = {"primary_only": _dedup_on(store), "exclude": _hidden(store), "swap": _swap(store)}

        def rows(agg) -> int:
            return sum(1 for r in agg if not r.get("label"))

        cached = {
            "tracks": await track_count(),
            "visible": store.visible_track_count(**kw),
            "artists": rows(store.aggregate_artists(**kw)),
            "album_artists": rows(store.aggregate_album_artists(**kw)),
            "albums": rows(store.aggregate_albums(artist=None, album_artist=None, **kw)),
            "genres": rows(store.aggregate_genres(**kw)),
            "years": rows(store.aggregate_years(**kw)),
            "scene_groups": len(store.aggregate_scene_group(**kw)),
        }
        _cache_set("counts", cached)
    return _etag_response(request, "counts", cached)


@router.get("/artists")
async def list_artists(request: Request):
    cached = _cache_get("artists")
    if cached is None:
        store = get_store()
        # ``list(...)`` copies the store's MEMOISED aggregate before we append
        # the synthetic "[No Artist]" bucket — appending to the returned list
        # in place would corrupt the store's cache (the same object is served
        # to Subsonic / DLNA) until the next mutation.
        cached = list(store.aggregate_artists(primary_only=_dedup_on(store),
                                              exclude=_hidden(store), swap=_swap(store)))
        untagged = _untagged_count(store, "artist")
        if untagged > 0:
            cached.append({"artist": "", "count": untagged, "label": "[No Artist]"})
        _cache_set("artists", cached)
    return _etag_response(request, "artists", cached)


@router.get("/album-artists")
async def list_album_artists(request: Request):
    cached = _cache_get("album_artists")
    if cached is None:
        store = get_store()
        cached = list(store.aggregate_album_artists(primary_only=_dedup_on(store),
                                                    exclude=_hidden(store),
                                                    swap=_swap(store)))
        untagged = _untagged_count(store, "album_artist")
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
            primary_only=_dedup_on(store), exclude=_hidden(store), swap=_swap(store),
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
        cached = store.aggregate_genres(primary_only=_dedup_on(store),
                                        exclude=_hidden(store), swap=_swap(store))
        _cache_set("genres", cached)
    return _etag_response(request, "genres", cached)


@router.get("/years")
async def list_years(request: Request):
    cached = _cache_get("years")
    if cached is None:
        store = get_store()
        cached = store.aggregate_years(primary_only=_dedup_on(store),
                                       exclude=_hidden(store), swap=_swap(store))
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
        cached = store.aggregate_formats(primary_only=_dedup_on(store),
                                         exclude=_hidden(store), swap=_swap(store))
        _cache_set("formats", cached)
    return _etag_response(request, "formats", cached)


@router.get("/scene-groups")
async def list_scene_groups(request: Request):
    """Demoscene groups with track counts — the browse facet over the Demozoo
    ``scene_group`` enrichment.  Empty until the Demozoo apply has run."""
    cached = _cache_get("scene_groups")
    if cached is None:
        store = get_store()
        cached = store.aggregate_scene_group(primary_only=_dedup_on(store),
                                             exclude=_hidden(store), swap=_swap(store))
        _cache_set("scene_groups", cached)
    return _etag_response(request, "scene_groups", cached)
