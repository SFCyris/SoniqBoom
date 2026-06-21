# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Admin API — OS password auth, folder management, reindex, stats, export/import, soundfonts."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiofiles
import httpx
from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Header, UploadFile
from fastapi.responses import StreamingResponse

from soniqboom.config import settings, get_data_dir
from soniqboom.core.data import (
    delete_scan_dir,
    delete_tracks_by_scan_root,
    rebuild_indexes,
    list_scan_dirs,
    upsert_scan_dir,
)
from soniqboom.core.scanner import get_progress, start_scan
from soniqboom.core.store import get_store

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Token store ───────────────────────────────────────────────────────────────
# Maps token → expiry (Unix timestamp). Kept in-memory; lost on restart.
_tokens: dict[str, float] = {}
_TOKEN_TTL = 3600  # 1 hour


def _issue_token() -> str:
    tok = secrets.token_hex(32)
    _tokens[tok] = time.time() + _TOKEN_TTL
    return tok


log = logging.getLogger(__name__)

# Auth policy:
#   * macOS with `dscl`     → OS-credential flow via /auth
#   * SONIQBOOM_ADMIN_TOKEN → static token; clients send it as X-Admin-Token
#   * neither                → open admin (warn loudly; toggleable via /auth/skip)
_HAS_OS_AUTH = sys.platform == "darwin" and shutil.which("dscl") is not None
_static_admin_token: str | None = os.environ.get("SONIQBOOM_ADMIN_TOKEN") or None
_auth_disabled = not (_HAS_OS_AUTH or _static_admin_token)

if _static_admin_token:
    log.info("Admin auth: static token from SONIQBOOM_ADMIN_TOKEN")
elif not _HAS_OS_AUTH:
    log.warning(
        "Admin auth is DISABLED — set SONIQBOOM_ADMIN_TOKEN, run on macOS "
        "with `dscl`, or bind to 127.0.0.1 only.  Any client that can reach "
        "this host can drive the admin endpoints."
    )


def _require_token(
    x_admin_token: str = Header(default=None),
    sb_session:    str = Cookie(default=None),
) -> str:
    """Accept any of:

    1. A signed-in user with ``role == 'admin'`` (cookie ``sb_session``).
       This is the modern path used by the multi-user UI.
    2. The legacy in-memory admin token (``X-Admin-Token`` header issued
       by ``POST /admin/auth``).  Kept for backward compatibility with
       single-user installs that haven't created any user accounts yet.
    3. The static ``SONIQBOOM_ADMIN_TOKEN`` env var.
    4. If no auth is configured at all *and* no user has been created
       yet, ``_auth_disabled`` is True and the request is let through.
       Once any user exists, this fallback is closed.
    """
    # ── Cookie-based user session ────────────────────────────────────────
    # Imported lazily — admin.py is loaded before users.py runs in some
    # boot orders, and ``init_user_store`` has to have run first.
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
        if sb_session:
            user = store.lookup_session(sb_session)
            if user and user.role == "admin":
                return f"user:{user.id}"
        # If any user exists, the legacy token / auth-skip paths are
        # closed.  This is what flips a single-tenant install into
        # multi-tenant the moment the first admin is created.
        if store.has_any():
            raise HTTPException(401, "Sign in as an admin user.")
    except HTTPException:
        raise
    except Exception:
        # User store not initialised yet (very early boot) — fall through
        # to the legacy paths so the app stays operable.
        pass

    if _auth_disabled:
        return "__skip__"
    if not x_admin_token:
        raise HTTPException(401, "Missing X-Admin-Token header")
    if _static_admin_token and secrets.compare_digest(
        x_admin_token, _static_admin_token,
    ):
        return x_admin_token
    exp = _tokens.get(x_admin_token)
    if exp is None or time.time() > exp:
        _tokens.pop(x_admin_token, None)
        raise HTTPException(401, "Invalid or expired admin token")
    return x_admin_token


# ── OS password verification ─────────────────────────────────────────────────

async def _verify_password(username: str, password: str) -> bool:
    """Verify an OS user's password.

    macOS:  uses ``dscl . -authonly``
    Other:  always returns False (auth auto-disabled at startup)
    """
    if not _HAS_OS_AUTH:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "dscl", ".", "-authonly", username, password,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/auth")
async def admin_auth(body: dict):
    """Authenticate with OS credentials (macOS: dscl).

    Body: { "username": "...", "password": "..." }
    Returns: { "token": "...", "expires_in": 3600 }
    """
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "username and password are required")

    ok = await _verify_password(username, password)
    if not ok:
        raise HTTPException(401, "Authentication failed")

    token = _issue_token()
    return {"token": token, "expires_in": _TOKEN_TTL}


@router.get("/auth/status")
async def admin_auth_status():
    """Check whether authentication is currently required."""
    return {"auth_disabled": _auth_disabled, "has_os_auth": _HAS_OS_AUTH}


@router.post("/auth/skip")
async def admin_auth_skip(
    body: dict,
    _tok: str = Depends(_require_token),
):
    """Enable or disable admin authentication.  Requires existing auth
    to flip — pre-this-fix, anyone reachable could flip it open and
    self-grant admin (P0 from pen-test).

    Body: { "disabled": true }   — skip auth (open admin without credentials)
    Body: { "disabled": false }  — require OS credentials again
    """
    global _auth_disabled
    # Refuse to skip auth once a user store has been bootstrapped — at that
    # point users / sessions are the source of truth and the skip flag
    # would re-introduce an anonymous admin path.
    try:
        from soniqboom.core.users import get_user_store
        if get_user_store().has_any() and bool(body.get("disabled", False)):
            raise HTTPException(
                400,
                "Cannot disable auth once users are configured — "
                "sign in as an admin instead.",
            )
    except HTTPException:
        raise
    except Exception:
        pass
    _auth_disabled = bool(body.get("disabled", False))
    return {"auth_disabled": _auth_disabled}


@router.get("/stats")
async def admin_stats(_tok: str = Depends(_require_token)):
    """Return library and index health stats."""
    dirs = await list_scan_dirs()
    store = get_store()
    count = store.track_count()
    return {
        "track_count": count,
        "dir_count": len(dirs),
        "index_ok": True,
        "index_docs": count,
    }


@router.get("/dirs")
async def admin_list_dirs(_tok: str = Depends(_require_token)):
    return {"dirs": await list_scan_dirs()}


@router.post("/dirs")
async def admin_add_dir(body: dict, _tok: str = Depends(_require_token)):
    raw = (body.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    path = str(Path(raw).expanduser().resolve())
    if not Path(path).is_dir():
        raise HTTPException(400, f"Directory not found: {path}")
    await upsert_scan_dir(path)

    # Honour the scan_zips toggle sent with this request.  The frontend's
    # add-dir form has its own checkbox; when the user explicitly unchecks it,
    # persist that choice as the new global setting so the scanner (which reads
    # settings.scan_zips) picks it up for this scan and all future ones.
    from soniqboom.config import load_local_conf, save_local_conf
    conf = load_local_conf()

    if "scan_zips" in body:
        want_zips = bool(body["scan_zips"])
        if want_zips != settings.scan_zips:
            conf["scan_zips"] = want_zips
            settings.scan_zips = want_zips      # runtime update for the scan
            save_local_conf(conf)

    # Store alias in config file if provided
    alias = (body.get("alias") or "").strip()
    if alias:
        aliases = conf.get("folder_aliases", {})
        aliases[path] = alias
        conf["folder_aliases"] = aliases
        save_local_conf(conf)
        settings.folder_aliases = aliases

    # Automatically scan the newly added folder in the background
    from soniqboom.api.library import _broadcast

    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    await start_scan([path], on_progress=_progress_cb)

    # Arm the filesystem watcher for the new root so future changes are
    # picked up without a manual rescan.  Remote shares are excluded.
    if not raw.startswith(("smb://", "ftp://", "http://", "https://")):
        try:
            from soniqboom.core import watcher
            await watcher.add_root(path)
        except Exception:
            log.exception("watcher.add_root failed for %s", path)
    else:
        # Remote: arm the adaptive-freshness poll loop instead.
        try:
            from soniqboom.core import remote_freshness
            await remote_freshness.add_share(path)
        except Exception:
            log.exception("freshness.add_share failed for %s", path)

    return {"dirs": await list_scan_dirs()}


@router.delete("/dirs")
async def admin_remove_dir(body: dict, _tok: str = Depends(_require_token)):
    """
    Remove a scan directory.
    Body: { "path": "...", "purge_tracks": true/false }

    Track purging runs in the background so the HTTP response returns immediately
    (large libraries can have 100K+ tracks under a single root).
    """
    raw = (body.get("path") or "").strip()
    purge = bool(body.get("purge_tracks", False))
    if not raw:
        raise HTTPException(400, "path is required")
    path = _normalize_dir_path(raw)

    await delete_scan_dir(path)

    # Remove alias from config file if it exists
    from soniqboom.config import load_local_conf, save_local_conf
    conf = load_local_conf()
    aliases = conf.get("folder_aliases", {})
    if path in aliases:
        del aliases[path]
        conf["folder_aliases"] = aliases
        save_local_conf(conf)
        settings.folder_aliases = aliases

    if purge:
        asyncio.create_task(_bg_purge_tracks(path))

    # Disarm the filesystem watcher for the removed root.
    try:
        from soniqboom.core import watcher
        await watcher.remove_root(path)
    except Exception:
        log.exception("watcher.remove_root failed for %s", path)
    # Disarm the adaptive-freshness poll loop for the removed remote share.
    if path.startswith(("smb://", "ftp://", "webdav://", "webdavs://")):
        try:
            from soniqboom.core import remote_freshness
            await remote_freshness.remove_share(path)
        except Exception:
            log.exception("freshness.remove_share failed for %s", path)

    return {
        "removed": path,
        "purging": purge,
        "dirs": await list_scan_dirs(),
    }


@router.patch("/dirs/alias")
async def admin_update_alias(body: dict, _tok: str = Depends(_require_token)):
    """Set or clear the alias for a scan directory. Stored in SoniqBoom.conf only."""
    raw = (body.get("path") or "").strip()
    alias = (body.get("alias") or "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    path = _normalize_dir_path(raw)

    from soniqboom.config import load_local_conf, save_local_conf
    conf = load_local_conf()
    aliases = conf.get("folder_aliases", {})
    if alias:
        aliases[path] = alias
    else:
        aliases.pop(path, None)
    conf["folder_aliases"] = aliases
    save_local_conf(conf)
    settings.folder_aliases = aliases

    return {"ok": True, "folder_aliases": aliases}


async def _bg_purge_tracks(root_path: str):
    """Background task: delete all tracks under a scan root."""
    try:
        n = await delete_tracks_by_scan_root(root_path)
        log.info("Purged %d tracks from %s", n, root_path)
    except Exception:
        log.exception("Background purge failed for %s", root_path)


def _is_remote(path: str) -> bool:
    # WebDAV scan roots start with http:// or https://.  Without these in
    # the match list, reindex / rescan would dispatch them to the LOCAL
    # scanner which then tries Path(http://…).resolve() and fails.
    return path.startswith(("smb://", "ftp://", "http://", "https://",
                            "webdav://", "webdavs://"))


async def _scan_dirs_split(dirs: list[str], progress_cb=None) -> dict:
    """Route local dirs to the local scanner, remote dirs to remote scanner.

    Returns a summary dict so the caller can surface failures to the user:
      * ``started`` — list of scan roots that actually launched
      * ``skipped`` — list of ``{path, reason}`` for ones that didn't,
        typically because a remote share is offline or its source could
        not be reconnected.  Without this, the API previously returned
        ``{started: true}`` for a reindex that silently skipped every
        unreachable FTP share, and the user saw "Done" with no actual
        work because the badge stayed pinned at the previous scan's
        final broadcast.
    """
    local = [d for d in dirs if not _is_remote(d)]
    remote = [d for d in dirs if _is_remote(d)]
    started: list[str] = []
    skipped: list[dict] = []

    if local:
        await start_scan(local, on_progress=progress_cb)
        started.extend(local)

    if remote:
        from soniqboom.config import load_local_conf
        from soniqboom.core.credentials import decrypt
        from soniqboom.core.filesource import create_source, get_source, register_source
        from soniqboom.core.scanner import start_remote_scan

        conf = load_local_conf()
        shares = conf.get("network_shares", {})
        for scan_root in remote:
            source = get_source(scan_root)
            share_id = None
            reconnect_err: str | None = None
            if source is None:
                # Try to reconnect from config
                for sid, share in shares.items():
                    if _scan_root_for_share(share) == scan_root:
                        share_id = sid
                        password = decrypt(share.get("password_enc", "")) or ""
                        try:
                            source = create_source(share, password=password)
                            register_source(scan_root, source)
                        except Exception as exc:
                            log.warning(
                                "Scan: reconnect failed for %s (%s)",
                                scan_root, exc,
                            )
                            reconnect_err = f"{type(exc).__name__}: {exc}"
                        break
            else:
                for sid, share in shares.items():
                    if _scan_root_for_share(share) == scan_root:
                        share_id = sid
                        break
            if source and share_id:
                asyncio.create_task(
                    start_remote_scan(share_id, scan_root, source,
                                      on_progress=progress_cb)
                )
                started.append(scan_root)
            else:
                reason = (
                    reconnect_err if reconnect_err
                    else "no matching share configured" if not share_id
                    else "share not connected (reconnect from Admin UI)"
                )
                skipped.append({"path": scan_root, "reason": reason})
                log.warning("Scan: skipping %s — %s", scan_root, reason)

    return {"started": started, "skipped": skipped}


@router.post("/reindex")
async def admin_reindex(_tok: str = Depends(_require_token)):
    """Rebuild all in-memory indexes, then rescan all registered folders.

    Response surfaces ``skipped`` so the UI can warn the user when one or
    more remote shares couldn't be reached — previously a silent skip
    made it look like a re-index "jumped to Done" because no scan task
    actually ran for those shares.
    """
    await rebuild_indexes()
    scan_dir_docs = await list_scan_dirs()
    dirs = [d["path"] for d in scan_dir_docs]

    from soniqboom.api.library import _broadcast

    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    result = {"started": [], "skipped": []}
    if dirs:
        result = await _scan_dirs_split(dirs, _progress_cb)
    return {
        "reindexed": True,
        "scanning":  bool(result["started"]),
        "dirs":      dirs,
        "started":   result["started"],
        "skipped":   result["skipped"],
    }


@router.post("/scan")
async def admin_scan(body: dict | None = None, _tok: str = Depends(_require_token)):
    """Start a scan. Body: { "dirs": [...] } or omit to scan all registered folders."""
    if body and body.get("dirs"):
        dirs = [
            d if _is_remote(d) else str(Path(d).expanduser().resolve())
            for d in body["dirs"]
        ]
    else:
        scan_dir_docs = await list_scan_dirs()
        dirs = [d["path"] for d in scan_dir_docs]

    if not dirs:
        raise HTTPException(400, "No scan directories configured.")

    from soniqboom.api.library import _broadcast

    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    result = await _scan_dirs_split(dirs, _progress_cb)
    return {
        "started": bool(result["started"]),
        "dirs":    dirs,
        "scanned": result["started"],
        "skipped": result["skipped"],
    }


@router.get("/scan/status")
async def admin_scan_status(_tok: str = Depends(_require_token)):
    return get_progress().to_dict()


@router.post("/scan/pause")
async def admin_scan_pause(_tok: str = Depends(_require_token)):
    """Pause the scanner.  In-flight files complete; no new files
    are submitted until ``/admin/scan/resume`` is called.

    Idempotent.  ``flipped`` in the response tells the UI whether the
    call actually changed state (so a double-tap doesn't toggle back).
    """
    from soniqboom.core.scanner import pause_scan, get_progress
    flipped = pause_scan()
    # Broadcast the new state so any open UI updates without polling.
    from soniqboom.api.library import _broadcast
    await _broadcast({"event": "scan_progress", **get_progress().to_dict()})
    return {"paused": True, "flipped": flipped}


@router.post("/scan/resume")
async def admin_scan_resume(_tok: str = Depends(_require_token)):
    """Resume the scanner if paused.  Idempotent."""
    from soniqboom.core.scanner import resume_scan, get_progress
    flipped = resume_scan()
    from soniqboom.api.library import _broadcast
    await _broadcast({"event": "scan_progress", **get_progress().to_dict()})
    return {"paused": False, "flipped": flipped}


# ── Remote freshness ─────────────────────────────────────────────────────────
#
# Adaptive background polling for FTP/SMB/WebDAV shares that don't
# support push notifications.  The admin UI uses these endpoints to:
#   - GET   /admin/freshness/status — per-share cadence + last_check + next_check
#   - POST  /admin/freshness/check_now/<urlencoded scan_root> — user "Check now"
#                                                               button bypasses
#                                                               the pool gate
#
# See ``soniqboom/core/remote_freshness.py`` for the cadence math.

@router.get("/freshness/status")
async def admin_freshness_status(_tok: str = Depends(_require_token)):
    """Return per-share freshness state for the admin UI."""
    from soniqboom.core import remote_freshness
    return {
        "enabled": remote_freshness.is_enabled(),
        "shares":  remote_freshness.get_status(),
    }


@router.post("/freshness/check_now")
async def admin_freshness_check_now(
    body: dict, _tok: str = Depends(_require_token),
):
    """Trigger an immediate freshness poll for *scan_root*.

    Body: ``{"scan_root": "ftp://…"}``.  Skips the pool-pressure gate
    (the user is actively waiting on the result).  Returns the scan
    plan from the scanner so the UI can show "Checked: walked=N,
    fresh=M, skipped=K".
    """
    scan_root = str(body.get("scan_root", "")).strip()
    if not scan_root:
        raise HTTPException(400, "scan_root required")
    from soniqboom.core import remote_freshness
    plan = await remote_freshness.check_now(scan_root, reason="user")
    return {"ok": True, "scan_root": scan_root, "plan": plan}


# ── Metadata repair ─────────────────────────────────────────────────────────
#
# Find tracks whose stored title / artist / album contains U+FFFD (the
# replacement character produced by the old broken ASCII-replace
# tracker-header decoder) and re-extract them in place.  Local files
# extract synchronously; remote files re-download via the scan-lane
# pool so live streaming isn't impacted.

@router.get("/metadata/repair-status")
async def metadata_repair_status(_tok: str = Depends(_require_token)):
    """Return progress for the most recent / current repair task.

    The shape mirrors ``/admin/scan/status`` so the frontend can reuse
    its badge/progress rendering.  Always returns 200 — when no repair
    has ever run, ``running=False`` and ``total=0``.
    """
    from soniqboom.core.repair import get_progress as _rep_progress
    return _rep_progress().to_dict()


@router.post("/metadata/repair-scan")
async def metadata_repair_scan(
    body: dict | None = None,
    _tok: str = Depends(_require_token),
):
    """Dry-run: count tracks with U+FFFD-tainted metadata.

    Body: ``{"tracker_only": bool}`` — filters to known tracker /
    chiptune extensions when True (saves the operator from queuing
    network re-downloads of regular FLAC files that the decoder fix
    couldn't have touched).

    Returns ``{count, sample: [paths…]}``.  ``sample`` is capped at 50
    paths for the UI preview.
    """
    body = body or {}
    tracker_only = bool(body.get("tracker_only", False))

    from soniqboom.core.repair import find_corrupt_tracks
    candidates = find_corrupt_tracks(tracker_only=tracker_only)

    return {
        "count": len(candidates),
        "tracker_only": tracker_only,
        "sample": [t.get("path", "") for t in candidates[:50]],
    }


@router.post("/metadata/repair-start")
async def metadata_repair_start(
    body: dict | None = None,
    _tok: str = Depends(_require_token),
):
    """Kick off the repair task in the background.

    Body: ``{"tracker_only": bool, "limit": int}`` — ``limit`` 0 means
    unlimited.

    Returns 409 if a repair is already running.  Otherwise returns
    ``{"started": True, "total": int}``.  Watch ``repair_progress`` WS
    events or poll ``/admin/metadata/repair-status`` for progress.
    """
    body = body or {}
    tracker_only = bool(body.get("tracker_only", False))
    limit = int(body.get("limit", 0))

    from soniqboom.core.repair import (
        find_corrupt_tracks, start_repair, is_running,
    )
    if is_running():
        raise HTTPException(409, "A repair task is already running")

    candidates = find_corrupt_tracks(tracker_only=tracker_only)
    if limit > 0:
        candidates = candidates[:limit]

    started = await start_repair(candidates)
    if not started:
        # Race: another caller started in the meantime.
        raise HTTPException(409, "A repair task is already running")

    return {"started": True, "total": len(candidates)}


@router.post("/metadata/repair-cancel")
async def metadata_repair_cancel(_tok: str = Depends(_require_token)):
    """Ask the in-flight repair task to stop after the current file."""
    from soniqboom.core.repair import request_cancel
    flipped = request_cancel()
    return {"cancelled": True, "flipped": flipped}


@router.post("/metadata/recompute-duplicates")
async def metadata_recompute_duplicates(_tok: str = Depends(_require_token)):
    """Recompute every track's duplicate-group annotation against current
    metadata.  Repair runs this automatically after re-titling, but it's
    exposed standalone so a stale ``is_duplicate_primary`` (e.g. tracks that
    were re-titled by an earlier repair before this hook existed) can be
    corrected without a full reindex.  Returns ``{annotated: int}``."""
    from soniqboom.core.repair import recompute_duplicate_groups_now
    n = await recompute_duplicate_groups_now()
    return {"annotated": n}


# ── Diagnostics ─────────────────────────────────────────────────────────────

@router.get("/inflight")
async def admin_inflight(_tok: str = Depends(_require_token)):
    """Return every HTTP request the deadlock watchdog is currently
    tracking, with how long it has been in-flight.

    Lives at ``/admin/inflight``.  Useful when the UI feels stuck:
    a glance at the response tells you whether work is genuinely
    backed up (long ``age_s`` on every entry) versus a frontend bug
    showing a stale spinner.

    ``dumped: true`` on an entry means the watchdog has already
    written a full-thread stack dump to the server log for that
    request — grep ``soniqboom.log`` for ``DEADLOCK SUSPECTED`` to
    find it.
    """
    from soniqboom.core import deadlock_watchdog
    return {
        "inflight": deadlock_watchdog.get_inflight_snapshot(),
        "stuck_threshold_s": deadlock_watchdog.STUCK_THRESHOLD,
        "poll_interval_s": deadlock_watchdog.POLL_INTERVAL,
    }


@router.get("/export")
async def admin_export(_tok: str = Depends(_require_token)):
    """Stream a gzip-compressed JSON backup of the library."""
    store = get_store()
    payload = {"version": 2, "data": store.to_snapshot()}
    # JSON-serialise + gzip on a thread.  For a 170K-track library this can
    # take seconds — running it inline stalls every other request.
    compressed = await asyncio.to_thread(
        lambda: gzip.compress(json.dumps(payload).encode()),
    )
    return StreamingResponse(
        iter([compressed]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=soniqboom.sbz"},
    )


@router.post("/import")
async def admin_import(
    file: UploadFile = File(...),
    _tok: str = Depends(_require_token),
):
    """Import a .sbz backup file."""
    raw = await file.read()
    try:
        # gzip + JSON decode of a large backup is CPU-bound; keep it off
        # the event loop so other requests don't stall mid-import.
        payload = await asyncio.to_thread(
            lambda: json.loads(gzip.decompress(raw).decode()),
        )
    except Exception as exc:
        raise HTTPException(400, f"Invalid .sbz file: {exc}")

    version = payload.get("version")
    if version not in (1, 2):
        raise HTTPException(400, "Unsupported export version")

    store = get_store()
    if version == 2:
        from soniqboom.core.persistence import populate_store
        populate_store(payload.get("data", {}))
        store.rebuild_indexes()
        return {"imported": store.track_count(), "errors": 0}

    raise HTTPException(400, "Version 1 exports are no longer supported — re-export from a v2 instance")


# ── Restart ─────────────────────────────────────────────────────────────────


def _detect_app_bundle() -> Path | None:
    """If running from inside a macOS ``.app`` bundle, return its path.

    Walks the parents of ``sys.executable`` (and ``sys.argv[0]`` as a
    fallback) looking for a directory ending in ``.app``. Returns ``None``
    on non-macOS platforms or when no bundle parent is found (the normal
    case for source installs running ``python -m soniqboom``).
    """
    if sys.platform != "darwin":
        return None
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    argv0 = Path(sys.argv[0]).resolve() if sys.argv else None
    if argv0:
        for parent in argv0.parents:
            if parent.suffix == ".app":
                return parent
    return None


def _graceful_pre_exec_flush() -> None:
    """Run the work FastAPI's shutdown hook would do before ``os.execv``.

    ``execv`` replaces the process image, so neither FastAPI's shutdown event
    nor any ``atexit`` handler fires.  Two things have to happen here:

      1. Flush the AOF buffer + close its fd.
      2. Terminate the background merger process.  ``execv`` keeps the
         parent PID alive but loses the daemon-cleanup guarantee — without
         an explicit terminate, the old merger keeps running and the new
         exec'd instance spawns *another* merger, both racing on
         ``library.json.new``.
    """
    try:
        from soniqboom import main as _main_mod
        writer = getattr(_main_mod, "_aof_writer", None)
        if writer is not None:
            writer.stop()  # flush_sync + close fd
    except Exception:
        log.exception("Pre-restart AOF flush failed — continuing with restart")
    try:
        from soniqboom import main as _main_mod
        proc = getattr(_main_mod, "_merger_proc", None)
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
    except Exception:
        log.exception("Pre-restart merger terminate failed — continuing")


def _do_restart() -> None:
    """Perform the actual process restart — blocking, runs in a worker thread."""
    bundle = _detect_app_bundle()
    if bundle is not None:
        # Relaunch the bundle via LaunchServices so the Dock / window wrapper
        # cleanly replaces the old process.  ``-n`` forces a new instance.
        log.info("Restart: relaunching bundle at %s", bundle)
        try:
            subprocess.Popen(["/usr/bin/open", "-n", str(bundle)])
        except Exception as exc:  # pragma: no cover — last-ditch
            log.exception("Restart: open -n failed: %s", exc)
        # Give LaunchServices a breath, then ask uvicorn to shut down
        # gracefully.  SIGTERM triggers FastAPI's shutdown event, which
        # flushes the AOF buffer and closes the merger cleanly — replacing
        # the previous ``os._exit(0)`` that bypassed all of that.
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    else:
        # Source install — exec in place so the PID is reused and any parent
        # shell keeps its terminal.  execv won't run shutdown hooks, so flush
        # the AOF synchronously first.
        log.info("Restart: exec'ing %s with argv=%r", sys.executable, sys.argv)
        _graceful_pre_exec_flush()
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as exc:  # pragma: no cover
            log.exception("Restart: execv failed: %s", exc)
            os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart")
async def admin_restart(_tok: str = Depends(_require_token)):
    """Restart the application.

    Returns a 200 response immediately, then triggers the restart from a
    worker thread so the browser gets a clean acknowledgement to react to.
    """
    async def _go() -> None:
        # Let the response flush all the way to the client.
        await asyncio.sleep(0.4)
        # Run the blocking restart in a worker thread so the event loop can
        # finish any pending writes.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do_restart)

    asyncio.create_task(_go())
    in_bundle = _detect_app_bundle() is not None
    return {"status": "restarting", "mode": "app" if in_bundle else "python"}


# ── Optional access services (subsonic / multiroom / cast) ─────────────────
#
# Toggle endpoints used by Settings → Services.  Reads/writes the same
# ``services.<name>`` keys in SoniqBoom.conf as the CLI command, so the two
# stay in sync.  A restart is required for the change to take effect — the
# response carries ``needs_restart: true`` so the UI can prompt.

@router.get("/services")
async def admin_services_list(_tok: str = Depends(_require_token)):
    """Return the current enable-state of every optional access service."""
    from soniqboom.config import (
        SERVICE_NAMES, SERVICE_LABELS, is_service_enabled,
    )
    return {
        "services": [
            {
                "name":    n,
                "label":   SERVICE_LABELS.get(n, n),
                "enabled": is_service_enabled(n),
            }
            for n in SERVICE_NAMES
        ],
    }


@router.put("/services/{name}")
async def admin_services_set(
    name: str,
    payload: dict,
    _tok: str = Depends(_require_token),
):
    """Set ``services.<name>`` to ``payload["enabled"]`` (boolean)."""
    from soniqboom.config import SERVICE_NAMES, set_service_enabled
    if name not in SERVICE_NAMES:
        raise HTTPException(404, f"Unknown service: {name}")
    enabled = bool(payload.get("enabled"))
    set_service_enabled(name, enabled)
    return {"name": name, "enabled": enabled, "needs_restart": True}


# ── Network shares ──────────────────────────────────────────────────────────


def _normalize_dir_path(raw: str) -> str:
    """Resolve local filesystem paths; pass remote URLs through unchanged.

    Remote scan dirs use protocol URLs (ftp://, smb://) as their canonical
    path key. Running them through ``Path.resolve()`` mangles them into
    nonsense like ``/cwd/ftp:/host/...`` which then never matches the keys
    stored under ``folder_aliases`` or ``scan_dirs``.
    """
    if raw.startswith(("ftp://", "smb://")):
        return raw
    return str(Path(raw).expanduser().resolve())


def _slug(s: str) -> str:
    """Lowercase, hyphen-only slug for use in share IDs."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "x"


def _auto_share_id(proto: str, host: str, share_name: str,
                    remote_path: str, existing: dict) -> str:
    """Generate a unique share id from (proto, host, share, remote_path).

    Earlier versions used just `<proto>-<host>` which collided whenever a user
    added more than one share for the same host (e.g. multiple FTP roots on
    10.0.0.88), silently overwriting the previous entry. We now incorporate
    the share name (SMB) and remote path so each scan root gets its own id.
    """
    parts = [proto, _slug(host)]
    if proto == "smb" and share_name:
        parts.append(_slug(share_name))
    if remote_path and remote_path != "/":
        parts.append(_slug(remote_path))
    base = "-".join(parts)
    if base not in existing:
        return base
    # Disambiguate against an unrelated share that already grabbed this id.
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _scan_root_for_share(share: dict) -> str:
    proto = share["protocol"].lower()
    host = share["host"]
    if proto == "smb":
        return f"smb://{host}/{share['share']}"
    if proto == "ftp":
        rpath = share.get("remote_path", "/")
        return f"ftp://{host}{rpath}"
    if proto in ("webdav", "webdavs"):
        # Use the original base_url verbatim — that's what users typed
        # and what the URL scheme expects (preserves trailing /).
        return share.get("base_url") or (
            f"{'https' if proto == 'webdavs' else 'http'}://{host}{share.get('remote_path', '/')}"
        )
    raise ValueError(f"Unknown protocol: {proto}")


@router.get("/shares")
async def list_shares(_tok: str = Depends(_require_token)):
    """List configured network shares with connection status."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.filesource import get_source

    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    result = []
    for share_id, share in shares.items():
        scan_root = _scan_root_for_share(share)
        source = get_source(scan_root)
        result.append({
            "id": share_id,
            "protocol": share.get("protocol"),
            "host": share.get("host"),
            "share": share.get("share", ""),
            "remote_path": share.get("remote_path", "/"),
            "username": share.get("username", ""),
            "alias": share.get("alias", ""),
            "auto_connect": share.get("auto_connect", True),
            "scan_root": scan_root,
            "connected": source is not None,
        })
    return {"shares": result}


@router.post("/shares")
async def add_share(body: dict, _tok: str = Depends(_require_token)):
    """Add a network share — validates connectivity, encrypts credentials, starts scan."""
    from soniqboom.config import load_local_conf, save_local_conf
    from soniqboom.core.credentials import encrypt
    from soniqboom.core.filesource import create_source, register_source
    from soniqboom.core.scanner import start_remote_scan

    proto = (body.get("protocol") or "").strip().lower()
    host = (body.get("host") or "").strip()
    if proto not in ("smb", "ftp", "webdav", "webdavs"):
        raise HTTPException(400, "protocol must be 'smb', 'ftp', 'webdav', or 'webdavs'")

    share_name = (body.get("share") or "").strip()
    remote_path = (body.get("remote_path") or "/").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    alias = (body.get("alias") or "").strip()
    base_url = (body.get("base_url") or "").strip()

    if proto in ("webdav", "webdavs"):
        # WebDAV identifies a share by its full base URL.  Validate the URL
        # shape and synthesize host / share / remote_path so downstream
        # code (scan_root, share-id) gets a uniform record.
        if not base_url:
            raise HTTPException(400, "base_url is required for WebDAV")
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(base_url)
        except ValueError:
            raise HTTPException(400, f"Invalid WebDAV URL: {base_url}")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise HTTPException(400, f"WebDAV URL must use http:// or https://")
        host        = parts.netloc
        remote_path = parts.path or "/"
        share_name  = ""
    else:
        if not host:
            raise HTTPException(400, "host is required")
        if proto == "smb" and not share_name:
            raise HTTPException(400, "share name is required for SMB")

    # Look up existing shares once so the auto-id stays unique.
    from soniqboom.config import load_local_conf
    existing_shares = load_local_conf().get("network_shares", {})
    explicit_id = (body.get("id") or "").strip()
    share_id = explicit_id or _auto_share_id(
        proto, host, share_name, remote_path, existing_shares,
    )

    share_conf = {
        "protocol": proto,
        "host": host,
        "share": share_name,
        "remote_path": remote_path,
        "username": username,
        "password_enc": encrypt(password) if password else "",
        "alias": alias,
        "auto_connect": True,
    }
    if proto in ("webdav", "webdavs"):
        share_conf["base_url"] = base_url
    scan_root = _scan_root_for_share(share_conf)

    try:
        source = create_source(share_conf, password=password)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, source.is_dir, "/")
        if not ok:
            raise Exception("Root directory not accessible")
    except Exception as exc:
        raise HTTPException(502, f"Could not connect: {exc}")

    register_source(scan_root, source)

    from soniqboom.config import save_local_conf
    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    shares[share_id] = share_conf
    conf["network_shares"] = shares
    if alias:
        aliases = conf.get("folder_aliases", {})
        aliases[scan_root] = alias
        conf["folder_aliases"] = aliases
        settings.folder_aliases = aliases
    save_local_conf(conf)

    # Re-bind any orphaned scan_dir for this scan_root to the new share id.
    # Prior versions of `_auto_share_id` collided on host alone, so an existing
    # scan_dir may carry a stale `network_share_id` pointing at a share that
    # was overwritten. Update it so Reconnect/Delete in the UI work correctly.
    await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")

    from soniqboom.api.library import _broadcast

    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    asyncio.create_task(
        start_remote_scan(share_id, scan_root, source, on_progress=_progress_cb)
    )

    return {"id": share_id, "scan_root": scan_root, "dirs": await list_scan_dirs()}


@router.delete("/shares")
async def remove_share(body: dict, _tok: str = Depends(_require_token)):
    """Remove a network share. Optionally purge tracks and clear cache.

    Handles orphaned scan_dir entries whose share was already removed
    from the config (e.g., after a previous partial cleanup).
    """
    from soniqboom.config import load_local_conf, save_local_conf
    from soniqboom.core.filesource import remove_source
    from soniqboom.core.remote_cache import get_cache

    share_id = (body.get("id") or "").strip()
    purge = bool(body.get("purge_tracks", False))
    if not share_id:
        raise HTTPException(400, "id is required")

    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    share = shares.pop(share_id, None)

    # Only delete the scan_dir whose path equals this share's actual scan_root.
    # Older versions iterated by `network_share_id` to find "orphaned" dirs,
    # but combined with the host-only id collision that produced cascading
    # deletes — removing one share would wipe every other share that had
    # collided on the same id. Bind deletion strictly to the computed
    # scan_root so siblings are never touched.
    scan_roots: list[str] = []
    if share:
        scan_roots.append(_scan_root_for_share(share))
    elif body.get("scan_root"):
        # Caller may pass an explicit scan_root to clean up an orphan whose
        # share record was already lost (e.g. previous partial cleanup).
        scan_roots.append(str(body["scan_root"]))

    if not scan_roots:
        raise HTTPException(404, f"Share '{share_id}' not found")

    if not share:
        log.info("Cleaning up orphaned scan dirs for share %s: %s", share_id, scan_roots)

    aliases = conf.get("folder_aliases", {})
    for scan_root in scan_roots:
        remove_source(scan_root)
        await delete_scan_dir(scan_root)
        try:
            get_cache().invalidate_share(scan_root)
        except Exception:
            pass  # cache may not be initialised
        aliases.pop(scan_root, None)
        if purge:
            asyncio.create_task(_bg_purge_tracks(scan_root))

    conf["folder_aliases"] = aliases
    conf["network_shares"] = shares
    save_local_conf(conf)
    settings.folder_aliases = aliases

    return {"removed": share_id, "purging": purge, "dirs": await list_scan_dirs()}


@router.post("/shares/test")
async def test_share(body: dict, _tok: str = Depends(_require_token)):
    """Test connectivity to a network share without saving."""
    from soniqboom.core.filesource import create_source

    proto = (body.get("protocol") or "").strip().lower()
    host  = (body.get("host") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    if proto in ("webdav", "webdavs"):
        if not base_url:
            raise HTTPException(400, "base_url is required for WebDAV")
        from urllib.parse import urlsplit
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise HTTPException(400, "WebDAV URL must use http:// or https://")
        host = parts.netloc
    if not proto or (proto not in ("webdav", "webdavs") and not host):
        raise HTTPException(400, "protocol and host are required")

    share_conf: dict = {
        "protocol":   proto,
        "host":       host,
        "share":      (body.get("share") or "").strip(),
        "remote_path": (body.get("remote_path") or "/").strip(),
        "username":   (body.get("username") or "").strip(),
    }
    if base_url:
        share_conf["base_url"] = base_url
    password = body.get("password") or ""

    try:
        source = create_source(share_conf, password=password)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, source.is_dir, "/")
        source.close()
        if not ok:
            raise Exception("Root directory not accessible")
    except Exception as exc:
        raise HTTPException(502, f"Connection failed: {exc}")

    return {"ok": True, "message": f"Connected to {host} via {proto.upper()}"}


@router.post("/shares/reconnect")
async def reconnect_share(body: dict, _tok: str = Depends(_require_token)):
    """Manually reconnect a disconnected share."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.credentials import decrypt
    from soniqboom.core.filesource import create_source, register_source

    share_id = (body.get("id") or "").strip()
    if not share_id:
        raise HTTPException(400, "id is required")

    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    share = shares.get(share_id)
    if not share:
        raise HTTPException(404, f"Share '{share_id}' not found")

    scan_root = _scan_root_for_share(share)
    password = decrypt(share.get("password_enc", "")) or ""

    try:
        source = create_source(share, password=password)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, source.is_dir, "/")
        if not ok:
            raise Exception("Root directory not accessible")
    except Exception as exc:
        raise HTTPException(502, f"Reconnect failed: {exc}")

    register_source(scan_root, source)
    await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")

    return {"id": share_id, "connected": True, "scan_root": scan_root}


# ── FTP pool tuning ──────────────────────────────────────────────────────────
#
# The FTP pool is shared across all scan + stream work for a given
# (host, port, user, pass).  These endpoints let the operator:
#   1. See its current state + the auto-learned server cap
#   2. Tune the scan / stream worker budgets per share
#   3. Actively probe the server's cap (rare — usually the reactive
#      detector handles it; this is the "Test now" button)
#   4. Reset a learned cap after the operator changes the server config
#      ("I bumped MaxClientsPerHost to 30, stop throttling me").
#
# All endpoints share the same admin-token guard as the rest of /admin.

@router.get("/cache-stats")
async def cache_stats_endpoint(_tok: str = Depends(_require_token)):
    """Per-tier cache hit/miss telemetry for the cache-cascade visualization.

    Process-lifetime counters (reset on restart, not persisted).  See
    ``soniqboom/core/cache_stats.py``.  Returns ``{uptime_sec, tiers:{...}}``
    where each tier carries ``hits, misses, hit_rate, rate_1s, size``.
    The cascade viz uses ``hit_rate`` as each tier's real resolve
    probability and ``rate_1s`` to pace the particle drop cadence.
    """
    from soniqboom.core import cache_stats
    return cache_stats.snapshot()


@router.get("/ftp-pool/status")
async def ftp_pool_status(_tok: str = Depends(_require_token)):
    """Return per-server pool snapshots + persisted server caps.

    Critical: results are deduped by ``host:port``, NOT per share.  The
    underlying pool registry is keyed by ``(host, port, user, pass,
    encoding)`` — six shares on the same NAS share ONE pool, so the UI
    must render ONE card per server (listing the shares that use it),
    not one card per share (which previously made three "Save" buttons
    on the same pool race each other).
    """
    from soniqboom.config import load_local_conf
    from soniqboom.core import ftp_pool_config
    from soniqboom.core.filesource import (
        list_ftp_pool_status, _resolve_pool_size,
        _FTP_POOL_SCAN_DEFAULT, _FTP_POOL_STREAM_DEFAULT,
    )

    live = list_ftp_pool_status()
    detected = ftp_pool_config.get_all()
    live_by_label = {p["label"]: p for p in live}

    conf = load_local_conf() if isinstance(load_local_conf(), dict) else {}
    # Re-fetch unconditionally — the above guard is just to satisfy mypy.
    conf = load_local_conf() or {}
    shares = conf.get("network_shares", {}) if isinstance(conf, dict) else {}

    # Group shares by host:port.  Each group becomes one card.
    servers: dict[str, dict] = {}
    for share_id, share in shares.items():
        if not isinstance(share, dict): continue
        if share.get("protocol", "").lower() != "ftp": continue
        host = share.get("host", "")
        port = int(share.get("port", 21))
        label = f"{host}:{port}"
        entry = servers.setdefault(label, {
            "label": label, "host": host, "port": port, "shares": [],
        })
        entry["shares"].append({
            "share_id": share_id,
            "name":     share.get("name") or share_id,
            "alias":    share.get("alias") or "",
        })

    out: list[dict] = []
    for label, info in servers.items():
        host = info["host"]
        port = info["port"]
        max_size, _min, configured_total, det = _resolve_pool_size(host, port)
        # Read the canonical per-server config back from the conf so the
        # UI shows what's saved.  _resolve_pool_size folds the legacy
        # per-share fallback in, but for the UI we want to surface the
        # raw saved values so the sliders match disk state.
        pools_map = conf.get("ftp_pools") or {}
        pool_cfg = pools_map.get(label) if isinstance(pools_map, dict) else None
        auto_grow = False
        if isinstance(pool_cfg, dict):
            scan_budget   = int(pool_cfg.get("scan",   _FTP_POOL_SCAN_DEFAULT))
            stream_budget = int(pool_cfg.get("stream", _FTP_POOL_STREAM_DEFAULT))
            auto_grow     = bool(pool_cfg.get("auto_grow", False))
        else:
            # No saved value — derive from the folded total (covers the
            # legacy-per-share migration window: shows the inherited
            # values until the user saves and writes the canonical key).
            scan_budget   = max(1, configured_total - _FTP_POOL_STREAM_DEFAULT)
            stream_budget = _FTP_POOL_STREAM_DEFAULT
            # If configured_total came from a legacy share with a
            # different split, fold that back: configured_total - stream
            # might miscount.  Re-read legacy directly for accuracy.
            for s in shares.values():
                if not isinstance(s, dict): continue
                if s.get("host") != host or int(s.get("port", 21)) != port: continue
                if s.get("protocol", "").lower() != "ftp": continue
                legacy = s.get("ftp_pool")
                if isinstance(legacy, dict):
                    try:
                        scan_budget   = max(1, int(legacy.get("scan",   scan_budget)))
                        stream_budget = max(1, int(legacy.get("stream", stream_budget)))
                    except (TypeError, ValueError):
                        pass
                    break

        out.append({
            "label":            label,
            "host":             host,
            "port":             port,
            "shares":           info["shares"],   # list of {share_id, name, alias}
            "scan_budget":      scan_budget,
            "stream_budget":    stream_budget,
            "auto_grow":        auto_grow,
            "configured_total": scan_budget + stream_budget,
            "effective_max":    max_size,
            "detected_cap":     det,
            "live":             live_by_label.get(label),
            "defaults": {
                "scan":   _FTP_POOL_SCAN_DEFAULT,
                "stream": _FTP_POOL_STREAM_DEFAULT,
            },
        })
    # Stable order — alphabetical by label so the UI doesn't reshuffle
    # between refreshes.
    out.sort(key=lambda x: x["label"])
    return {"servers": out, "detected_caps": detected}


@router.put("/ftp-pool")
async def set_ftp_pool(body: dict, _tok: str = Depends(_require_token)):
    """Save per-server scan / stream pool budget.

    Body: ``{"host": str, "port": int, "scan": int, "stream": int}``.
    All four required.  The pair (host, port) identifies the actual
    physical pool — shares sharing those credentials share this
    configuration.  Values clamped to ``[1, 32]`` so a UI typo can't
    request 1000 connections.

    Storage is canonical at ``conf["ftp_pools"]["{host}:{port}"]``.
    Any legacy per-share ``ftp_pool`` fields under matching shares are
    REMOVED so the two sources can't disagree on the next read.

    Resizes the live pool live — in-flight borrows complete on the
    old size; the next ones see the new ceiling.
    """
    from soniqboom.config import load_local_conf, save_local_conf
    from soniqboom.core.filesource import reload_ftp_pool_sizes

    host = (body.get("host") or "").strip()
    if not host:
        raise HTTPException(400, "host is required")
    try:
        port   = int(body.get("port", 21))
        scan   = max(1, min(32, int(body.get("scan",   6))))
        stream = max(1, min(32, int(body.get("stream", 2))))
    except (TypeError, ValueError):
        raise HTTPException(400, "port / scan / stream must be integers")
    # Auto-grow toggle: when on, the pool's keepalive loop attempts to
    # add one extra connection per cycle (while there's saturation)
    # and bumps the configured scan budget on success.  Off by default
    # — user must opt in per-server because some shared NAS appliances
    # treat over-the-cap probes as abuse.
    auto_grow = bool(body.get("auto_grow", False))

    label = f"{host}:{port}"
    conf = load_local_conf()
    if not isinstance(conf, dict):
        conf = {}

    pools_map = conf.get("ftp_pools")
    if not isinstance(pools_map, dict):
        pools_map = {}
    # Preserve (or accept) the per-server "browse" lane budget introduced with
    # the dedicated interactive-listing lane.  The UI may not send it yet, so
    # fall back to any existing value, then the default of 1 — never drop it.
    _prev = pools_map.get(label) if isinstance(pools_map.get(label), dict) else {}
    pools_map[label] = {
        "scan":      scan,
        "stream":    stream,
        "browse":    max(1, int(body.get("browse", _prev.get("browse", 1)))),
        "auto_grow": auto_grow,
    }
    conf["ftp_pools"] = pools_map

    # Strip any legacy per-share ``ftp_pool`` fields for this host:port
    # so a future read can't get confused between two sources of truth.
    shares = conf.get("network_shares", {})
    if isinstance(shares, dict):
        for s in shares.values():
            if not isinstance(s, dict): continue
            if s.get("host") != host: continue
            if int(s.get("port", 21)) != port: continue
            s.pop("ftp_pool", None)
        conf["network_shares"] = shares

    save_local_conf(conf)
    changes = reload_ftp_pool_sizes()
    return {
        "host":      host,
        "port":      port,
        "label":     label,
        "scan":      scan,
        "stream":    stream,
        "auto_grow": auto_grow,
        "applied":   changes,
    }


@router.put("/shares/{share_id}/ftp-pool")
async def set_share_ftp_pool_legacy(
    share_id: str, body: dict, _tok: str = Depends(_require_token),
):
    """Legacy alias: resolve the share to host:port then delegate to
    the canonical per-server endpoint above.

    Kept so an already-loaded UI from before this fix doesn't 404 on
    save.  New frontend code should hit ``PUT /admin/ftp-pool`` with
    ``{host, port, scan, stream}`` directly.
    """
    from soniqboom.config import load_local_conf

    conf = load_local_conf()
    share = (conf.get("network_shares") or {}).get(share_id) if isinstance(conf, dict) else None
    if not share:
        raise HTTPException(404, f"Share '{share_id}' not found")
    if share.get("protocol", "").lower() != "ftp":
        raise HTTPException(400, "FTP pool tuning only applies to FTP shares")

    return await set_ftp_pool({
        "host":   share.get("host", ""),
        "port":   int(share.get("port", 21)),
        "scan":   body.get("scan",   6),
        "stream": body.get("stream", 2),
    }, _tok=_tok)


@router.post("/ftp-pool/reset-cap")
async def ftp_pool_reset_cap(body: dict, _tok: str = Depends(_require_token)):
    """Forget the learned server cap for a share's host:port.

    Use after changing the FTP server's MaxClients setting upward —
    next borrow falls back to the user-configured budget without the
    old (low) auto-clamp.

    Body: ``{"share_id": "..."}`` or ``{"host": "...", "port": 21}``.
    """
    from soniqboom.config import load_local_conf
    from soniqboom.core import ftp_pool_config
    from soniqboom.core.filesource import reload_ftp_pool_sizes

    host, port = _resolve_host_port_from_body(body)
    if not host:
        raise HTTPException(400, "share_id (or host + port) is required")

    removed = ftp_pool_config.reset_detected_cap(host, port)
    # Grow the live pool back up to the user-configured budget.
    changes = reload_ftp_pool_sizes()
    return {
        "host":     host,
        "port":     port,
        "removed":  removed,
        "applied":  changes,
    }


@router.post("/ftp-pool/probe-cap")
async def ftp_pool_probe_cap(body: dict, _tok: str = Depends(_require_token)):
    """Actively probe the FTP server's concurrent-client cap.

    Opens connections one at a time up to ``max_probe`` (default 16)
    until the server rejects with too-many-clients.  Records the cap as
    one below the highest successful count; resizes the live pool to
    match.

    Body: either ``{"share_id": "..."}`` or ``{"host": "...", "port": 21}``
    (host+port resolves to a matching configured FTP share to borrow its
    credentials — the probe must authenticate, and shares to the same
    host:port share a credential pool).  Optional ``"max_probe": int``
    (default 16; capped at 32).

    SECURITY: only run this when no scan or stream is active — the
    probe will eat scan slots while it works.
    """
    from soniqboom.config import load_local_conf
    from soniqboom.core.credentials import decrypt
    from soniqboom.core import ftp_pool_config
    from soniqboom.core.filesource import (
        _build_ftp_factory, reload_ftp_pool_sizes,
    )

    conf = load_local_conf()
    share = _resolve_ftp_share_from_body(body, conf)
    if not share:
        raise HTTPException(
            404,
            "No matching FTP share found — provide share_id, or the host"
            " (and port) of a configured FTP share.",
        )
    if share.get("protocol", "").lower() != "ftp":
        raise HTTPException(400, "Probe only applies to FTP shares")

    host = share["host"]
    port = int(share.get("port", 21))
    username = share.get("username", "") or "anonymous"
    password = decrypt(share.get("password_enc", "")) or ""
    encoding = "utf-8"
    max_probe = max(2, min(32, int(body.get("max_probe", 16))))

    factory = _build_ftp_factory(host, port, username, password, encoding)
    opened: list = []
    detected = None
    last_error = ""
    try:
        for n in range(1, max_probe + 1):
            try:
                # Run the connect in a thread so we don't block the loop
                # for the full TCP+LOGIN round trip (~100-300 ms × N).
                conn = await asyncio.to_thread(factory)
                opened.append(conn)
            except Exception as exc:
                # The factory's own retry loop already handled transient
                # errors; landing here means the rejection was permanent
                # (which is exactly what "too many clients" looks like).
                last_error = f"{type(exc).__name__}: {exc}"
                # n connections succeeded → cap is n  (detected stored as n)
                detected = n
                break
        else:
            # Made it to max_probe without rejection — cap is at least
            # max_probe.  Record that as the floor.
            detected = max_probe
    finally:
        for conn in opened:
            try:
                conn.quit()
            except Exception:
                try: conn.close()
                except Exception: pass

    if detected is not None:
        ftp_pool_config.set_detected_cap(host, port, detected)

    changes = reload_ftp_pool_sizes()
    return {
        "host":       host,
        "port":       port,
        "detected":   detected,
        "last_error": last_error or None,
        "applied":    changes,
    }


def _resolve_host_port_from_body(body: dict) -> tuple[str, int]:
    """Pull (host, port) from a body that's either ``{share_id}`` or
    ``{host, port}``.  Returns ``("", 21)`` if nothing usable.
    """
    sid = (body.get("share_id") or "").strip()
    if sid:
        from soniqboom.config import load_local_conf
        share = load_local_conf().get("network_shares", {}).get(sid)
        if share:
            return share.get("host", ""), int(share.get("port", 21))
    return (body.get("host") or "").strip(), int(body.get("port", 21))


def _resolve_ftp_share_from_body(body: dict, conf: dict) -> dict | None:
    """Resolve the FTP share to operate on from a body that is either
    ``{share_id}`` or ``{host[, port]}``.

    With an explicit ``share_id`` the share is looked up directly.  With
    ``host`` (and optional ``port``, default 21) we return the first
    configured FTP share matching that host:port — the probe needs a share to
    borrow credentials from, and shares to the same endpoint share a pool.
    Returns ``None`` when nothing matches.
    """
    shares = conf.get("network_shares", {})
    sid = (body.get("share_id") or "").strip()
    if sid:
        return shares.get(sid)
    host = (body.get("host") or "").strip()
    if not host:
        return None
    try:
        port = int(body.get("port", 21) or 21)
    except (TypeError, ValueError):
        port = 21
    for s in shares.values():
        if (s.get("protocol", "").lower() == "ftp"
                and s.get("host", "") == host
                and int(s.get("port", 21) or 21) == port):
            return s
    return None


# ── Renderers ────────────────────────────────────────────────────────────────

@router.get("/renderers")
async def check_renderers(_tok: str = Depends(_require_token)):
    """Check which format renderers are installed."""

    def _check(configured: str, binary_name: str) -> dict:
        if configured:
            found = Path(configured).is_file()
            return {"installed": found, "path": configured}
        path = shutil.which(binary_name)
        return {"installed": path is not None, "path": path or ""}

    # Augment the ffmpeg check with the encoder/demuxer feature audit so
    # the admin UI can surface "bundled ffmpeg missing — refetch?" when
    # the running binary lacks libmp3lame/libvorbis or DSD demuxers.
    # User reported (2026-05-23): Amperfy DSF playback emitted silence
    # because Homebrew's stock ffmpeg can demux DSF but cannot encode to
    # MP3 — libmp3lame isn't in the default bottle.
    ff_check = _check(
        settings.ffmpeg_path if settings.ffmpeg_path != "ffmpeg" else "",
        "ffmpeg",
    )
    try:
        import subprocess as _sub
        bin_ = settings.ffmpeg_path or "ffmpeg"
        if ff_check.get("installed"):
            r = _sub.run([bin_, "-hide_banner", "-formats"],
                         capture_output=True, text=True, timeout=10, check=False)
            fmts = (r.stdout or "").lower()
            demuxers_ok = all(
                f" d   {d} " in fmts or f" d   {d}\n" in fmts
                for d in ("dsf", "iff", "wsd")
            )
            r2 = _sub.run([bin_, "-hide_banner", "-encoders"],
                          capture_output=True, text=True, timeout=10, check=False)
            enc_out = (r2.stdout or "") + (r2.stderr or "")
            encoders_present = {
                name: name in enc_out
                for name in ("libmp3lame", "libvorbis", "libopus",
                             "flac", "aac")
            }
            missing = [k for k, v in encoders_present.items() if not v]
            if not demuxers_ok:
                missing.insert(0, "dsd-demuxers")
            ff_check["demuxers_ok"]   = demuxers_ok
            ff_check["encoders"]      = encoders_present
            ff_check["missing"]       = missing
            ff_check["fully_capable"] = (not missing)
            # Whether a bundled copy exists alongside — informs the UI
            # whether to offer "Use bundled" vs "Download bundled".
            bundled_path = Path(get_data_dir()) / "bin" / "ffmpeg"
            ff_check["bundled_present"] = bundled_path.is_file()
            ff_check["bundled_path"]    = str(bundled_path)
            ff_check["using_bundled"]   = (
                ff_check.get("path") == str(bundled_path)
            )
    except Exception:
        log.exception("ffmpeg feature probe failed")

    return {
        "ffmpeg": ff_check,
        "sidplayfp": _check(settings.sidplayfp_path, "sidplayfp"),
        "fluidsynth": _check(settings.fluidsynth_path, "fluidsynth"),
        "openmpt123": _check(settings.openmpt123_path, "openmpt123"),
    }


# ── Bundled ffmpeg installer ────────────────────────────────────────────────

@router.post("/ffmpeg/fetch")
async def admin_fetch_ffmpeg(_tok: str = Depends(_require_token)):
    """Download / refresh the bundled ffmpeg with full DSD + lossy-encoder
    support.  Same code path as the ``soniqboom fetch-ffmpeg`` CLI command —
    centralised here so the admin UI can offer a one-click "Re-download
    ffmpeg" affordance when the runtime probe finds an incomplete binary.

    Returns a JSON envelope that's safe to surface in the UI; the long
    download itself runs on a worker thread so the request doesn't block
    the event loop for the ~10 s the fetch takes on a fast LAN.
    """
    import asyncio as _asyncio
    try:
        from scripts.fetch_ffmpeg import install as _ff_install, _default_dest
    except ImportError:
        # Wheel install — the script ships beside the package.
        try:
            from soniqboom.scripts.fetch_ffmpeg import install as _ff_install, _default_dest  # type: ignore
        except ImportError:
            raise HTTPException(500, "fetch_ffmpeg helper not found in install tree.")

    loop = _asyncio.get_running_loop()

    def _do_install() -> dict:
        dest_dir = _default_dest()
        # ``force=True`` so a stale (older version) bundled binary gets
        # overwritten — operators expect the button to actually refresh,
        # not no-op when a binary already exists.
        result = _ff_install(dest_dir=dest_dir, force=True)
        return {
            "ok":   True,
            "path": str(result.get("path") if isinstance(result, dict) else result),
            "dest": str(dest_dir),
        }

    try:
        res = await loop.run_in_executor(None, _do_install)
    except Exception as exc:
        log.exception("admin/ffmpeg/fetch failed")
        raise HTTPException(502, f"Download failed: {exc}")

    # Re-probe the binary so the runtime ``settings.ffmpeg_path`` points
    # at the newly-installed bundled copy without requiring a full server
    # restart.  We mutate in-memory only — the operator can pin via
    # config later if they want this to persist past restart-with-config
    # changes (it already persists by virtue of the file being on disk).
    try:
        from pathlib import Path as _P
        bundled = _P(get_data_dir()) / "bin" / "ffmpeg"
        if bundled.is_file():
            settings.ffmpeg_path = str(bundled)
            res["active_path"] = str(bundled)
    except Exception:
        pass

    return res


# ── Soundfonts ───────────────────────────────────────────────────────────────

def _safe_soundfont_filename(name: str, *, require_ext: bool = True) -> str:
    """Reject path-traversal attempts in caller-supplied soundfont filenames.

    Returns the basename of *name* after validating it has no path separators,
    parent traversal, or hidden-file prefix.  Normalises to Unicode NFC so
    macOS HFS+/APFS round-tripping (which stores NFD on disk) doesn't make
    delete fail-to-match what upload wrote.  Raises HTTP 400 otherwise.
    """
    import unicodedata

    if not name:
        raise HTTPException(400, "Soundfont name is required")
    name = unicodedata.normalize("NFC", name)
    # Reject anything that isn't a plain filename.  Path separators, parent
    # traversal, NULs, and leading dots all map to "give me a file outside
    # the soundfonts directory" attacks.
    if "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(400, "Invalid soundfont name")
    base = os.path.basename(name)
    if base != name or base in ("", ".", "..") or base.startswith("."):
        raise HTTPException(400, "Invalid soundfont name")
    if require_ext and not base.lower().endswith((".sf2", ".sf3")):
        raise HTTPException(400, "Only .sf2 and .sf3 files are accepted")
    return base


@router.get("/soundfonts")
async def list_soundfonts(_tok: str = Depends(_require_token)):
    """List all soundfonts in the soundfonts directory."""
    from soniqboom.config import get_soundfonts_dir, get_active_soundfont

    import unicodedata

    sf_dir = get_soundfonts_dir()
    active = get_active_soundfont()
    # Filesystem listings on HFS+ are NFD-form while config-derived names
    # round-trip through ``_safe_soundfont_filename`` (NFC).  Normalise both
    # sides before comparing so the active flag isn't wrong on case-folded
    # or decomposed Unicode.
    active_name = unicodedata.normalize("NFC", active.name) if active else None

    fonts = []
    for f in sorted(sf_dir.iterdir()):
        if f.suffix.lower() in (".sf2", ".sf3"):
            display_name = unicodedata.normalize("NFC", f.name)
            fonts.append({
                "name": display_name,
                "size": f.stat().st_size,
                "active": display_name == active_name,
                "path": str(f),
            })
    return {"soundfonts": fonts, "active": active_name}


@router.post("/soundfonts/active")
async def set_active_soundfont(body: dict, _tok: str = Depends(_require_token)):
    """Set the active soundfont by filename."""
    from soniqboom.config import get_soundfonts_dir, save_local_conf, load_local_conf

    name = _safe_soundfont_filename(body.get("name", ""))
    sf_path = get_soundfonts_dir() / name
    if not sf_path.exists():
        raise HTTPException(404, f"Soundfont '{name}' not found")

    conf = load_local_conf()
    if "renderers" not in conf:
        conf["renderers"] = {}
    conf["renderers"]["soundfont_path"] = str(sf_path)
    save_local_conf(conf)
    settings.soundfont_path = str(sf_path)
    return {"active": name}


@router.post("/soundfonts/upload")
async def upload_soundfont(
    file: UploadFile = File(...),
    _tok: str = Depends(_require_token),
):
    """Upload a .sf2/.sf3 soundfont file.

    Writes to a sibling ``.partial`` first and ``os.replace``s onto the
    final path so a truncated/aborted upload can't leave a half-written
    file at ``dest`` (FluidSynth would happily load it and segfault).
    """
    from soniqboom.config import get_soundfonts_dir

    safe_name = _safe_soundfont_filename(file.filename or "")
    sf_dir = get_soundfonts_dir()
    dest = sf_dir / safe_name
    # Per-call unique tmp filename so two concurrent uploads of the same
    # ``foo.sf2`` don't both write to ``foo.sf2.partial`` and clobber each
    # other before the ``os.replace`` swap.
    tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.{secrets.token_hex(4)}.partial")

    try:
        async with aiofiles.open(tmp, "wb") as f:
            while chunk := await file.read(65536):
                await f.write(chunk)
        await asyncio.to_thread(os.replace, str(tmp), str(dest))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return {"name": safe_name, "size": dest.stat().st_size}


@router.delete("/soundfonts/{name}")
async def delete_soundfont(name: str, _tok: str = Depends(_require_token)):
    """Delete a soundfont file."""
    from soniqboom.config import get_soundfonts_dir

    safe_name = _safe_soundfont_filename(name)
    sf_path = get_soundfonts_dir() / safe_name
    if not sf_path.exists():
        raise HTTPException(404, f"Soundfont '{safe_name}' not found")
    sf_path.unlink()
    return {"deleted": safe_name}


@router.post("/soundfonts/download")
async def download_known_soundfont(body: dict, _tok: str = Depends(_require_token)):
    """Download a well-known soundfont by name or URL."""
    from soniqboom.config import get_soundfonts_dir

    url = body.get("url", "")
    name = body.get("name", "")
    if not url:
        raise HTTPException(400, "url and name required")

    safe_name = _safe_soundfont_filename(name)
    sf_dir = get_soundfonts_dir()
    dest = sf_dir / safe_name

    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async with aiofiles.open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    await f.write(chunk)

    # Many well-known soundfonts (e.g. GeneralUser GS) are distributed as a
    # ZIP.  A ZIP saved verbatim as ``.sf2`` is unreadable by FluidSynth — the
    # MIDI render fails with "renderer exited with status 255".  Detect the
    # archive and extract the largest ``.sf2`` member in its place so the
    # downloaded soundfont is immediately usable.
    import shutil
    import zipfile
    if zipfile.is_zipfile(dest):
        try:
            with zipfile.ZipFile(dest) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".sf2")]
                if not members:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(422, "Downloaded archive contains no .sf2 soundfont.")
                member = max(members, key=lambda m: zf.getinfo(m).file_size)
                tmp = dest.with_name(dest.name + ".tmp")
                with zf.open(member) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
            os.replace(tmp, dest)
        except zipfile.BadZipFile:
            dest.unlink(missing_ok=True)
            raise HTTPException(422, "Downloaded soundfont archive is corrupt.")

    return {"name": safe_name, "size": dest.stat().st_size}


# ── Disk usage stats ─────────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    """Return total size (bytes) of a directory tree.

    ``os.scandir`` returns cached file/dir-type info per entry so we avoid
    the two ``stat()`` calls that ``rglob`` + ``is_file()`` + ``stat()``
    triggered per file — on a 170K-thumb art cache that's ~340K syscalls
    cut to ~170K, plus a tight C-level recursion.
    """
    if not path.exists():
        return 0

    def _walk(p: str) -> int:
        total = 0
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            total += _walk(entry.path)
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            pass
        return total

    try:
        return _walk(str(path))
    except Exception:
        return -1


@router.get("/disk-usage")
async def disk_usage(_tok: str = Depends(_require_token)):
    """Return disk usage for data, art cache, soundfonts."""
    from soniqboom.config import get_art_cache_dir, get_soundfonts_dir, get_data_dir
    import asyncio

    art_dir = get_art_cache_dir()
    sf_dir = get_soundfonts_dir()
    data_dir = get_data_dir()

    loop = asyncio.get_event_loop()
    data_size, art_size, sf_size = await asyncio.gather(
        loop.run_in_executor(None, _dir_size, data_dir),
        loop.run_in_executor(None, _dir_size, art_dir),
        loop.run_in_executor(None, _dir_size, sf_dir),
    )

    # Remote cache stats (cheap — just sums the in-memory index)
    from soniqboom.core.remote_cache import get_cache
    try:
        rc = get_cache()
        rc_size = rc.total_size()
        rc_count = rc.entry_count()
        rc_max_mb = rc.max_mb
    except Exception:
        rc_size, rc_count, rc_max_mb = 0, 0, 2048

    return {
        "data_dir": data_size,
        "data_dir_path": str(data_dir),
        "art_cache": art_size,
        "art_cache_path": str(art_dir),
        "soundfonts": sf_size,
        "soundfonts_path": str(sf_dir),
        "remote_cache": rc_size,
        "remote_cache_files": rc_count,
        "remote_cache_max_mb": rc_max_mb,
        "track_count": get_store().track_count(),
    }


# ── Cache management ─────────────────────────────────────────────────────────

@router.post("/cache/clear-art")
async def clear_art_cache(_tok: str = Depends(_require_token)):
    """Clear the artwork cache directory.

    Continues on individual ``unlink`` failures so a single locked or
    permission-denied file doesn't abort the whole clear (which previously
    surfaced as a 500 even though most files had been removed already).
    """
    from soniqboom.config import get_art_cache_dir
    art_dir = get_art_cache_dir()
    count = 0
    errors: list[str] = []

    def _scan_and_unlink(d: str) -> None:
        nonlocal count
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            try:
                                os.unlink(entry.path)
                                count += 1
                            except OSError as exc:
                                # Basename-only — the directory portion is
                                # operator-private layout, not useful in
                                # the response, and used to leak full
                                # paths through the admin endpoint.
                                errors.append(
                                    f"{entry.name}: {exc.strerror or 'error'}"
                                )
                        elif entry.is_dir(follow_symlinks=False):
                            _scan_and_unlink(entry.path)
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            return

    _scan_and_unlink(str(art_dir))
    out: dict = {"cleared": count, "path": str(art_dir)}
    if errors:
        out["failed"] = len(errors)
        out["failed_samples"] = errors[:5]
        # Operator-side diagnostic trail — the response only carries the
        # first 5 sample failures, so log the full count + sample so the
        # admin doesn't have to guess what couldn't be removed.
        log.warning(
            "clear-art: %d files could not be removed (e.g. %s)",
            len(errors), "; ".join(errors[:5]),
        )
    return out


@router.post("/cache/clear-waveforms")
async def clear_waveforms(_tok: str = Depends(_require_token)):
    """Clear all waveform data."""
    cleared = get_store().clear_waveforms()
    return {"cleared": cleared}


@router.post("/cache/clear-aggregations")
async def clear_aggregation_cache(_tok: str = Depends(_require_token)):
    """Clear the in-memory aggregation cache."""
    from soniqboom.api.library import invalidate_agg_cache
    invalidate_agg_cache()
    return {"cleared": True}


@router.post("/cache/clear-remote")
async def clear_remote_cache(_tok: str = Depends(_require_token)):
    """Clear all cached remote audio files."""
    from soniqboom.core.remote_cache import get_cache
    try:
        count = get_cache().clear_all()
    except Exception:
        count = 0
    return {"cleared": count}


@router.get("/cache/conversion")
async def get_conversion_cache_stats(_tok: str = Depends(_require_token)):
    """Return conversion-cache fill stats for the Settings → Renderers panel."""
    from soniqboom.core.conversion_cache import cache_stats
    return await cache_stats()


@router.get("/cache/conversion/stream")
async def stream_conversion_cache_stats(_tok: str = Depends(_require_token)):
    """Server-Sent Events stream of cache-fill stats.

    Frontends open one EventSource while the Settings → Renderers panel is
    visible.  Pushes a fresh stats payload every 2 seconds plus an
    immediate snapshot on connect.  Lets the fill bar animate in real time
    as a playlist warms the cache without polling.
    """
    from fastapi.responses import StreamingResponse
    from soniqboom.core.conversion_cache import cache_stats
    import json as _json

    async def _gen():
        # Heartbeat budget: 60 s before the connection idle-closes from
        # the client end (uvicorn defaults).  We push every 2 s anyway,
        # so the heartbeat doubles as data.
        while True:
            try:
                stats = await cache_stats()
            except Exception:
                stats = {"error": "cache_stats failed"}
            yield f"data: {_json.dumps(stats)}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            # nginx-style hint to disable buffering for SSE.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cache/clear-conversion")
async def clear_conversion_cache(
    types: str = "all",
    _tok: str = Depends(_require_token),
):
    """Clear conversion-cache entries.

    ``types`` is a comma-separated list of any of ``sid`` / ``midi`` /
    ``tracker`` / ``transcoded``, or ``all``.  The Settings UI typically
    sends ``transcoded`` only — that lets a user reclaim DSD/ALAC disk
    without losing the (much more expensive to regenerate) SID, MIDI,
    and tracker renders."""
    from soniqboom.core.conversion_cache import clear_cache
    selected = None if types == "all" else [t.strip() for t in types.split(",") if t.strip()]
    return await clear_cache(selected)


@router.post("/cache/clear-zip-extract")
async def clear_zip_extract_cache(_tok: str = Depends(_require_token)):
    """Clear the per-track extracted-from-ZIP audio cache.

    The cache is owned by ``stream.py`` — it has both an in-memory index and
    on-disk files under ``<data_dir>/zip-extracts/``, populated whenever a file
    inside a ZIP is played (extracting the member is expensive, especially for
    nested ZIPs, so the extracted bytes are kept on disk until eviction).

    Delegates to :func:`soniqboom.api.stream.clear_zip_extract_cache`, which
    drops the in-memory index, resets the byte counter, and unlinks the on-disk
    files while honouring read-pins (an actively-streamed member is deferred,
    not yanked mid-Range).
    """
    from soniqboom.api.stream import clear_zip_extract_cache as _clear
    return await _clear()


# ── Log viewer ───────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(
    lines: int = 200,
    _tok: str = Depends(_require_token),
):
    """Return the most recent *lines* from soniqboom.log.

    Uses an efficient tail-read: seeks backwards from EOF so we never load
    the entire file into memory regardless of rotation size.
    """
    from soniqboom.config import get_data_dir
    log_file = get_data_dir() / "log" / "soniqboom.log"

    if not log_file.exists():
        return {"lines": ["Log file not found."], "count": 1}

    try:
        # Read tail efficiently — 8 KB covers ~80-120 typical log lines.
        # Double the chunk if we didn't get enough lines.
        chunk = max(8192, lines * 120)   # ~120 bytes per line estimate
        size = log_file.stat().st_size
        with open(log_file, "rb") as f:
            f.seek(max(0, size - chunk))
            if f.tell() > 0:
                f.readline()  # skip partial first line
            raw = f.read().decode("utf-8", errors="replace")
        result = [ln for ln in raw.split("\n") if ln][-lines:]
    except Exception as exc:
        result = [f"Error reading log: {exc}"]

    return {"lines": result, "count": len(result)}


# ── HVSC integration ─────────────────────────────────────────────────────────

@router.get("/hvsc/status")
async def hvsc_status(_tok: str = Depends(_require_token)):
    """Report whether HVSC is currently configured and how many entries
    the database has loaded.  Used by the Renderers tab status pill."""
    from soniqboom.core.hvsc import get_hvsc
    h = get_hvsc()
    h._ensure_loaded()
    return {
        "enabled":     h.is_configured(),
        "docs_path":   str(h._docs_path) if h._docs_path else "",
        "songlengths": len(h._songlengths),
        "stil":        len(h._stil),
    }


@router.post("/hvsc/cleanup-orphans")
async def hvsc_cleanup_orphans(_tok: str = Depends(_require_token)):
    """Remove scan dirs that an older release auto-imported from the
    HVSC tree.

    Prior to the in-place HVSC re-extract, clicking "Re-extract SID
    metadata" routed through ``start_scan(parent_dirs_of_all_SIDs)``,
    which had the side effect of *registering* every one of those parent
    directories as a scan root via ``upsert_scan_dir``.  Users ended up
    with dozens of ``/.../MUSICIANS/<letter>/<artist>/`` rows in their
    library folder list.

    Heuristic for "orphaned": the path is a subdirectory of the
    configured HVSC root (parent of DOCUMENTS) AND every track under it
    has format == SID.  That keeps any folder the user *explicitly*
    added (e.g. their own SID collection that happens to live under
    HVSC too) safe — provided it has at least one non-SID file, which
    the heuristic detects.  When in doubt we err on the side of keeping
    the dir; the operator can hand-Remove the rest.
    """
    from soniqboom.core.hvsc import get_hvsc
    from soniqboom.core.store import get_store
    hvsc = get_hvsc()
    if not hvsc.is_configured() or not hvsc._docs_path:
        return {"removed": 0, "message": "HVSC isn't configured — nothing to clean."}
    hvsc_root = hvsc._docs_path.parent.resolve()
    try:
        hvsc_root_str = str(hvsc_root)
    except Exception:
        return {"removed": 0, "message": "HVSC root unresolvable — clean up manually."}

    store = get_store()
    all_dirs = await list_scan_dirs()
    # Map scan_root → list of formats under it (lowercased).
    formats_by_root: dict[str, set[str]] = {}
    for t in store.all_track_metas():
        p = t.get("path") or ""
        # Skip remote tracks; they can't live under an HVSC root.
        if p.startswith(("smb://", "ftp://", "http://", "https://")):
            continue
        for sd in all_dirs:
            sdp = sd.get("path") or ""
            if not sdp or sdp.startswith(("smb://", "ftp://", "http://", "https://")):
                continue
            if p == sdp or p.startswith(sdp.rstrip("/") + "/"):
                formats_by_root.setdefault(sdp, set()).add(
                    str(t.get("format", "")).upper(),
                )

    candidates: list[str] = []
    for sd in all_dirs:
        sdp = sd.get("path") or ""
        if not sdp:
            continue
        if sdp.startswith(("smb://", "ftp://", "http://", "https://")):
            continue
        try:
            sdp_abs = str(Path(sdp).resolve())
        except Exception:
            continue
        # Must live UNDER the HVSC root (not the HVSC root itself — the
        # user may legitimately want their HVSC root as a scan dir).
        if sdp_abs == hvsc_root_str:
            continue
        if not sdp_abs.startswith(hvsc_root_str.rstrip("/") + "/"):
            continue
        # Only remove if every track under this dir is a SID — anything
        # else suggests the user mixed in their own content.
        fmts = formats_by_root.get(sdp, set())
        if fmts and fmts == {"SID"}:
            candidates.append(sdp)

    if not candidates:
        return {"removed": 0, "message": "No orphaned HVSC scan dirs found."}

    # Reuse the existing remove path for each — keeps alias / scan_dir
    # bookkeeping consistent.
    from soniqboom.config import load_local_conf, save_local_conf
    conf = load_local_conf()
    aliases = conf.get("folder_aliases", {})
    removed: list[str] = []
    for path in candidates:
        await delete_scan_dir(path)
        aliases.pop(path, None)
        removed.append(path)
    conf["folder_aliases"] = aliases
    save_local_conf(conf)
    settings.folder_aliases = aliases
    return {
        "removed": len(removed),
        "paths":   removed,
        "message": (
            f"Removed {len(removed)} scan dir(s) that older releases "
            f"auto-imported from the HVSC tree."
        ),
    }


@router.post("/hvsc/rescan-sids")
async def hvsc_rescan_sids(_tok: str = Depends(_require_token)):
    """Apply HVSC lookups to every existing SID/PSID track in the library.

    Earlier this routed through ``start_scan(parent_dirs)``, but the
    scanner is *incremental* — it skips files whose mtime + size haven't
    changed, so a HVSC-config change never propagated to existing tracks
    (the file content is identical; only the lookup table is new).

    Instead, walk the store directly: for each SID, compute the MD5,
    query HVSC, and patch ``duration`` / ``hvsc_lengths`` / ``subsongs``
    / ``stil`` in place.  Remote tracks are skipped because we'd have to
    pull the bytes through the FileSource to MD5 them — that's a
    follow-up.
    """
    from soniqboom.core.store import get_store
    from soniqboom.core.hvsc import get_hvsc
    hvsc = get_hvsc()
    if not hvsc.is_configured():
        return {"updated": 0, "message": "HVSC is not configured — set the DOCUMENTS path first."}
    store = get_store()
    updates: list[tuple[str, dict]] = []
    skipped_remote = 0
    skipped_missing = 0
    scanned = 0
    # Track the post-rescan correct duration set for every SID we touched, so
    # we can reconcile the conversion cache against it after the update
    # batch — this catches entries that were rendered at the global
    # default before HVSC was configured.  Multi-subsong tracks contribute
    # one valid duration per subsong so per-tune renders are preserved too.
    correct_durations: dict[str, set[int]] = {}
    sid_track_ids: list[str] = []

    def _ints(seq) -> set[int]:
        out: set[int] = set()
        for v in seq or []:
            try:
                out.add(int(round(float(v))))
            except (TypeError, ValueError):
                pass
        return out

    for t in store.all_track_metas():
        fmt = str(t.get("format", "")).upper()
        if fmt != "SID":
            continue
        scanned += 1
        sid_track_ids.append(t["id"])
        path_str = t.get("path") or ""
        if path_str.startswith(("smb://", "ftp://", "http://", "https://")):
            skipped_remote += 1
            continue
        path = Path(path_str)
        if not path.is_file():
            skipped_missing += 1
            continue
        try:
            durations = hvsc.lookup_durations(path)
            stil = hvsc.lookup_stil(path)
        except Exception:
            continue
        patch: dict = {}
        if durations:
            patch["duration"] = durations[0]
            patch["hvsc_lengths"] = durations
            if len(durations) > 1:
                patch["subsongs"] = len(durations)
            correct_durations[t["id"]] = _ints(durations)
        else:
            # No HVSC entry — keep whatever durations the track already had so
            # its cache survives.  This covers single-subsong tracks (duration
            # field) and multi-subsong tracks (hvsc_lengths field) alike.
            valid = _ints(t.get("hvsc_lengths") or [])
            if t.get("duration"):
                valid |= _ints([t["duration"]])
            if valid:
                correct_durations[t["id"]] = valid
        if stil and stil.get("text"):
            patch["stil"] = stil["text"]
        if patch:
            updates.append((t["id"], patch))

    if updates:
        # Run on the executor — both update_track_fields_batch and the
        # AOF append cost are CPU-bound and synchronous.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, store.update_track_fields_batch, updates,
        )

    # Reconcile the SID conversion cache against the post-rescan track
    # durations.  This also evicts entries from BEFORE HVSC was configured
    # (where the track already had a non-default duration but the cache key
    # still references the old global default), which the user perceives
    # as a flaky "renders twice before sticking" cache.
    from soniqboom.core.conversion_cache import purge_sid_entries_for
    purged = await purge_sid_entries_for(
        sid_track_ids, keep_duration=correct_durations,
    )

    msg_parts = [f"Updated {len(updates)} of {scanned} SID track(s)."]
    if purged:
        msg_parts.append(f"Purged {purged} stale render(s).")
    if skipped_remote:
        msg_parts.append(f"Skipped {skipped_remote} on remote shares.")
    if skipped_missing:
        msg_parts.append(f"Skipped {skipped_missing} missing files.")
    return {
        "updated": len(updates),
        "scanned": scanned,
        "skipped_remote":  skipped_remote,
        "skipped_missing": skipped_missing,
        "message": " ".join(msg_parts),
    }


# ── Settings panel ───────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(_tok: str = Depends(_require_token)):
    """Return current application settings (safe subset)."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.data import get_config
    conf = load_local_conf()
    return {
        "server": conf["server"],
        "renderers": {
            "sidplayfp_path": settings.sidplayfp_path,
            "fluidsynth_path": settings.fluidsynth_path,
            "openmpt123_path": settings.openmpt123_path,
            "soundfont_path": settings.soundfont_path,
            "soundfonts_dir": settings.soundfonts_dir,
            "sid_default_duration": settings.sid_default_duration,
            "hvsc_docs_path": settings.hvsc_docs_path,
        },
        "scan_zips": settings.scan_zips,
        "scan_remote_zips": settings.scan_remote_zips,
        "art_cache_dir": settings.art_cache_dir,
        "expose_local_files": settings.expose_local_files,
        "folder_aliases": conf.get("folder_aliases", {}),
        "filter_duplicates": await get_config("filter_duplicates", False),
        # Folder-tree duplicate filter — independent of the search/library
        # ``filter_duplicates`` above.  When True, ``/api/fstree/tracks-with-meta``
        # collapses duplicate alternate encodings while browsing folders;
        # default off so folder views mirror what's on disk.
        "dedup_folders": await get_config("dedup_folders", False),
        "use_folder_art": await get_config("use_folder_art", True),
        # Sidebar folder-tree filter.  When True, ``/api/fstree/children``
        # drops subdirectories whose subtree contains zero indexed audio
        # tracks (e.g. photo dumps, build dirs, video-only folders
        # sharing a scan root with the music library).
        "hide_empty_folders": await get_config("hide_empty_folders", False),
        # Comma-separated, case-insensitive, ordered list of filenames the
        # folder-art fallback walks (first match wins).  Empty string =
        # ship the historical default (cover.jpg, folder.jpg, front.jpg,
        # album.jpg + their .png/.jpeg variants, in that order).
        "folder_art_names": await get_config("folder_art_names", ""),
        "remote_cache_max_mb": conf.get("remote_cache_max_mb", 2048),
        "conversion_cache_max_mb": int(conf.get("conversion_cache_max_bytes", 4 * 1024 ** 3) / (1024 ** 2)),
        # ZIP-extract cache budget — keeps extracted-from-ZIP audio bytes
        # on disk between plays.  Default 2 GB matches the rough size of a
        # typical chiptune/tracker ZIP collection's hot working set.
        "zip_extract_cache_max_mb": int(conf.get("zip_extract_cache_max_mb", 2048)),
    }


@router.put("/settings")
async def update_settings(body: dict, _tok: str = Depends(_require_token)):
    """Update application settings (persisted to SoniqBoom.conf)."""
    from soniqboom.config import load_local_conf, save_local_conf

    conf = load_local_conf()
    # Merge allowed fields
    if "renderers" in body:
        if "renderers" not in conf:
            conf["renderers"] = {}
        for k in ("sidplayfp_path", "fluidsynth_path", "openmpt123_path",
                   "soundfont_path", "soundfonts_dir", "sid_default_duration",
                   "hvsc_docs_path"):
            if k in body["renderers"]:
                conf["renderers"][k] = body["renderers"][k]
                # Live-update settings so the change takes effect without
                # restart; HVSC re-indexes on the next SID extract.
                if hasattr(settings, k):
                    setattr(settings, k, body["renderers"][k])
        # If hvsc_docs_path changed, reconfigure the HVSC singleton so the
        # next SID extract (or "Re-extract SID metadata" click) picks it up.
        if "hvsc_docs_path" in body["renderers"]:
            try:
                from soniqboom.core.hvsc import get_hvsc
                get_hvsc().configure(body["renderers"]["hvsc_docs_path"] or None)
            except Exception:
                log.exception("HVSC reconfigure failed")
    if "scan_zips" in body:
        conf["scan_zips"] = bool(body["scan_zips"])
    if "scan_remote_zips" in body:
        conf["scan_remote_zips"] = bool(body["scan_remote_zips"])
        settings.scan_remote_zips = conf["scan_remote_zips"]   # runtime effect, no restart
    if "art_cache_dir" in body:
        conf["art_cache_dir"] = body["art_cache_dir"]
    if "expose_local_files" in body:
        conf["expose_local_files"] = bool(body["expose_local_files"])
    if "folder_aliases" in body and isinstance(body["folder_aliases"], dict):
        conf["folder_aliases"] = body["folder_aliases"]

    if "remote_cache_max_mb" in body:
        try:
            mb = max(100, min(50000, int(body["remote_cache_max_mb"])))
        except (TypeError, ValueError):
            mb = 2048
        conf["remote_cache_max_mb"] = mb
        # Update the live cache instance so eviction kicks in immediately
        from soniqboom.core.remote_cache import get_cache
        try:
            get_cache().set_max_mb(mb)
        except Exception:
            pass

    if "conversion_cache_max_mb" in body:
        # Clamp to 256 MB – 100 GB.  Below 256 MB the cache thrashes (a
        # single DSD album exceeds it); above 100 GB is almost certainly
        # a typo (the entire library would fit on most users' disks).
        try:
            mb = max(256, min(102400, int(body["conversion_cache_max_mb"])))
        except (TypeError, ValueError):
            mb = 4096
        new_bytes = mb * 1024 * 1024
        conf["conversion_cache_max_bytes"] = new_bytes
        settings.conversion_cache_max_bytes = new_bytes
        # Run the eviction loop once with the new budget so the cache
        # shrinks immediately if the user lowered the limit.
        try:
            from soniqboom.core.conversion_cache import _maybe_evict
            await asyncio.to_thread(_maybe_evict)
        except Exception:
            log.exception("Conversion-cache eviction after budget change failed")

    if "zip_extract_cache_max_mb" in body:
        # Same clamp shape as the conversion cache — 100 MB floor avoids
        # turning the cache off accidentally, 100 GB ceiling guards against
        # typos.  Persisted only; eviction is owned by stream.py and reads
        # this value on next access.
        try:
            mb = max(100, min(102400, int(body["zip_extract_cache_max_mb"])))
        except (TypeError, ValueError):
            mb = 2048
        conf["zip_extract_cache_max_mb"] = mb

    if (
        "filter_duplicates" in body
        or "dedup_folders" in body
        or "use_folder_art" in body
        or "folder_art_names" in body
        or "hide_empty_folders" in body
    ):
        from soniqboom.core.data import set_config
        if "hide_empty_folders" in body:
            await set_config("hide_empty_folders", bool(body["hide_empty_folders"]))
        if "filter_duplicates" in body:
            await set_config("filter_duplicates", bool(body["filter_duplicates"]))
        if "dedup_folders" in body:
            await set_config("dedup_folders", bool(body["dedup_folders"]))
        if "use_folder_art" in body:
            new_val = bool(body["use_folder_art"])
            await set_config("use_folder_art", new_val)
            # When folder art is turned on, clear the negative art cache so
            # tracks that previously had no embedded art get re-evaluated
            # (this time the folder art fallback will run).
            if new_val:
                get_store().clear_art_absent()
        if "folder_art_names" in body:
            # Stored verbatim as a CSV; the lookup side
            # (``_parse_folder_art_names`` in api/art.py) trims / lowercases
            # / dedupes / falls back to the default when empty.  Keeping the
            # raw user-typed string here means the admin UI can round-trip
            # exactly what the user entered (including capitalisation),
            # which is helpful for "what did I set this to last week?".
            raw = body["folder_art_names"]
            csv = str(raw or "").strip()
            await set_config("folder_art_names", csv)
            # Re-evaluate previously-absent tracks under the new priority
            # list — same reasoning as the ``use_folder_art`` toggle above.
            # We don't blow away the *positive* cache (already-extracted
            # thumbnails stay) because that would force re-extraction for
            # the entire library on every priority tweak; the operator can
            # rebuild the art cache from the System tab if they want a
            # full refresh after reordering the list.
            get_store().clear_art_absent()

    save_local_conf(conf)
    return {"updated": True}
