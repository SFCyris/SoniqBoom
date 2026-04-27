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
import subprocess
import sys
import time
from pathlib import Path

import aiofiles
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Header, UploadFile
from fastapi.responses import StreamingResponse

from soniqboom.config import settings
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

# Auto-disable auth on platforms without OS-level password verification.
# macOS uses `dscl`; other platforms fall back to open admin (with a warning).
_HAS_OS_AUTH = sys.platform == "darwin" and shutil.which("dscl") is not None
_auth_disabled = not _HAS_OS_AUTH  # set via /admin/auth/skip toggle

if not _HAS_OS_AUTH:
    log.info(
        "OS-level admin auth not available on this platform — "
        "admin panel is open.  Use /admin/auth/skip to toggle."
    )


def _require_token(x_admin_token: str = Header(default=None)) -> str:
    if _auth_disabled:
        return "__skip__"
    if not x_admin_token:
        raise HTTPException(401, "Missing X-Admin-Token header")
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
async def admin_auth_skip(body: dict):
    """Enable or disable admin authentication.

    Body: { "disabled": true }   — skip auth (open admin without credentials)
    Body: { "disabled": false }  — require OS credentials again
    """
    global _auth_disabled
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
    return path.startswith(("smb://", "ftp://"))


async def _scan_dirs_split(dirs: list[str], progress_cb=None):
    """Route local dirs to the local scanner, remote dirs to remote scanner."""
    local = [d for d in dirs if not _is_remote(d)]
    remote = [d for d in dirs if _is_remote(d)]

    if local:
        await start_scan(local, on_progress=progress_cb)

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
            if source is None:
                # Try to reconnect from config
                for sid, share in shares.items():
                    if _scan_root_for_share(share) == scan_root:
                        share_id = sid
                        password = decrypt(share.get("password_enc", "")) or ""
                        try:
                            source = create_source(share, password=password)
                            register_source(scan_root, source)
                        except Exception:
                            pass
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


@router.post("/reindex")
async def admin_reindex(_tok: str = Depends(_require_token)):
    """Rebuild all in-memory indexes, then rescan all registered folders."""
    await rebuild_indexes()
    scan_dir_docs = await list_scan_dirs()
    dirs = [d["path"] for d in scan_dir_docs]

    from soniqboom.api.library import _broadcast

    async def _progress_cb(p):
        await _broadcast({"event": "scan_progress", **p.to_dict()})

    if dirs:
        await _scan_dirs_split(dirs, _progress_cb)
    return {"reindexed": True, "scanning": bool(dirs), "dirs": dirs}


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

    await _scan_dirs_split(dirs, _progress_cb)
    return {"started": True, "dirs": dirs}


@router.get("/scan/status")
async def admin_scan_status(_tok: str = Depends(_require_token)):
    return get_progress().to_dict()


@router.get("/export")
async def admin_export(_tok: str = Depends(_require_token)):
    """Stream a gzip-compressed JSON backup of the library."""
    store = get_store()
    payload = {"version": 2, "data": store.to_snapshot()}
    compressed = gzip.compress(json.dumps(payload).encode())
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
        payload = json.loads(gzip.decompress(raw).decode())
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
        # Give LaunchServices a breath, then terminate.
        time.sleep(0.5)
        os._exit(0)
    else:
        # Source install — exec in place so the PID is reused and any parent
        # shell keeps its terminal.
        log.info("Restart: exec'ing %s with argv=%r", sys.executable, sys.argv)
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as exc:  # pragma: no cover
            log.exception("Restart: execv failed: %s", exc)
            os._exit(1)


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
    if proto not in ("smb", "ftp"):
        raise HTTPException(400, "protocol must be 'smb' or 'ftp'")
    if not host:
        raise HTTPException(400, "host is required")

    share_name = (body.get("share") or "").strip()
    remote_path = (body.get("remote_path") or "/").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    alias = (body.get("alias") or "").strip()

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
    host = (body.get("host") or "").strip()
    if not proto or not host:
        raise HTTPException(400, "protocol and host are required")

    share_conf = {
        "protocol": proto,
        "host": host,
        "share": (body.get("share") or "").strip(),
        "remote_path": (body.get("remote_path") or "/").strip(),
        "username": (body.get("username") or "").strip(),
    }
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

    return {
        "ffmpeg": _check(settings.ffmpeg_path if settings.ffmpeg_path != "ffmpeg" else "", "ffmpeg"),
        "sidplayfp": _check(settings.sidplayfp_path, "sidplayfp"),
        "fluidsynth": _check(settings.fluidsynth_path, "fluidsynth"),
        "openmpt123": _check(settings.openmpt123_path, "openmpt123"),
    }


# ── Soundfonts ───────────────────────────────────────────────────────────────

@router.get("/soundfonts")
async def list_soundfonts(_tok: str = Depends(_require_token)):
    """List all soundfonts in the soundfonts directory."""
    from soniqboom.config import get_soundfonts_dir, get_active_soundfont

    sf_dir = get_soundfonts_dir()
    active = get_active_soundfont()
    active_name = active.name if active else None

    fonts = []
    for f in sorted(sf_dir.iterdir()):
        if f.suffix.lower() in (".sf2", ".sf3"):
            fonts.append({
                "name": f.name,
                "size": f.stat().st_size,
                "active": f.name == active_name,
                "path": str(f),
            })
    return {"soundfonts": fonts, "active": active_name}


@router.post("/soundfonts/active")
async def set_active_soundfont(body: dict, _tok: str = Depends(_require_token)):
    """Set the active soundfont by filename."""
    from soniqboom.config import get_soundfonts_dir, save_local_conf, load_local_conf

    name = body.get("name", "")
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
    """Upload a .sf2/.sf3 soundfont file."""
    from soniqboom.config import get_soundfonts_dir

    if not file.filename or not file.filename.lower().endswith((".sf2", ".sf3")):
        raise HTTPException(400, "Only .sf2 and .sf3 files are accepted")

    sf_dir = get_soundfonts_dir()
    dest = sf_dir / file.filename

    async with aiofiles.open(dest, "wb") as f:
        while chunk := await file.read(65536):
            await f.write(chunk)

    return {"name": file.filename, "size": dest.stat().st_size}


@router.delete("/soundfonts/{name}")
async def delete_soundfont(name: str, _tok: str = Depends(_require_token)):
    """Delete a soundfont file."""
    from soniqboom.config import get_soundfonts_dir

    sf_path = get_soundfonts_dir() / name
    if not sf_path.exists():
        raise HTTPException(404, f"Soundfont '{name}' not found")
    sf_path.unlink()
    return {"deleted": name}


@router.post("/soundfonts/download")
async def download_known_soundfont(body: dict, _tok: str = Depends(_require_token)):
    """Download a well-known soundfont by name or URL."""
    from soniqboom.config import get_soundfonts_dir

    url = body.get("url", "")
    name = body.get("name", "")
    if not url or not name:
        raise HTTPException(400, "url and name required")

    sf_dir = get_soundfonts_dir()
    dest = sf_dir / name

    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async with aiofiles.open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    await f.write(chunk)

    return {"name": name, "size": dest.stat().st_size}


# ── Disk usage stats ─────────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    """Return total size (bytes) of a directory tree.

    Pure-Python walk for cross-platform compatibility.
    Returns -1 if the size cannot be determined within the time budget.
    """
    if not path.exists():
        return 0
    try:
        total = 0
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return total
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
    """Clear the artwork cache directory."""
    from soniqboom.config import get_art_cache_dir
    art_dir = get_art_cache_dir()
    count = 0
    for f in art_dir.rglob("*"):
        if f.is_file():
            f.unlink()
            count += 1
    return {"cleared": count, "path": str(art_dir)}


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
        },
        "scan_zips": settings.scan_zips,
        "art_cache_dir": settings.art_cache_dir,
        "expose_local_files": settings.expose_local_files,
        "folder_aliases": conf.get("folder_aliases", {}),
        "filter_duplicates": await get_config("filter_duplicates", False),
        "use_folder_art": await get_config("use_folder_art", True),
        "remote_cache_max_mb": conf.get("remote_cache_max_mb", 2048),
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
                   "soundfont_path", "soundfonts_dir", "sid_default_duration"):
            if k in body["renderers"]:
                conf["renderers"][k] = body["renderers"][k]
    if "scan_zips" in body:
        conf["scan_zips"] = bool(body["scan_zips"])
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

    if "filter_duplicates" in body or "use_folder_art" in body:
        from soniqboom.core.data import set_config
        if "filter_duplicates" in body:
            await set_config("filter_duplicates", bool(body["filter_duplicates"]))
        if "use_folder_art" in body:
            new_val = bool(body["use_folder_art"])
            await set_config("use_folder_art", new_val)
            # When folder art is turned on, clear the negative art cache so
            # tracks that previously had no embedded art get re-evaluated
            # (this time the folder art fallback will run).
            if new_val:
                get_store().clear_art_absent()

    save_local_conf(conf)
    return {"updated": True}
