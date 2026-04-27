# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SoniqBoom — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from soniqboom import __version__
from soniqboom.api import art, fstree, library, multiroom, playlist, search, smart, stream, tracks
from soniqboom.config import settings, get_data_dir, _CONF_PATH
from soniqboom.plugins import load_all
from soniqboom.plugins.base import registry

log = logging.getLogger("soniqboom")


def _find_frontend_dir() -> Path:
    """Locate the bundled frontend assets.

    Tries, in order:
      1. ``<package>/frontend``  — source install (next to main.py).
      2. ``<sys.executable dir>/frontend`` — frozen / standalone deployments
         that place static assets next to the executable.
      3. ``<sys.executable dir>/../Resources/frontend`` — deployments that
         place non-executable assets under ``Contents/Resources``.

    Returns the first directory that exists.  Falls back to the dev path so
    callers can still check ``.exists()`` and get a predictable false.
    """
    dev = Path(__file__).resolve().parent / "frontend"
    if dev.is_dir():
        return dev
    exe_dir = Path(sys.executable).resolve().parent
    bundled = exe_dir / "frontend"
    if bundled.is_dir():
        return bundled
    resources = exe_dir.parent / "Resources" / "frontend"
    if resources.is_dir():
        return resources
    return dev


FRONTEND_DIR = _find_frontend_dir()

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SoniqBoom",
    version=__version__,
    description=(
        "Self-hosted music server for personal libraries — FLAC, ALAC, MP3, "
        "Opus, plus SID, MIDI, and 20+ tracker formats.\n\n"
        "Source: https://github.com/SFCyris/SoniqBoom"
    ),
    contact={
        "name": "SoniqBoom on GitHub",
        "url":  "https://github.com/SFCyris/SoniqBoom",
    },
    license_info={
        "name":       "AGPL-3.0-or-later",
        "identifier": "AGPL-3.0-or-later",
        "url":        "https://www.gnu.org/licenses/agpl-3.0.html",
    },
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    """Attach sensible Cache-Control to each response class.

    Rules, in precedence order:
      * If a handler set its own ``Cache-Control`` (e.g. art endpoints with
        ``immutable`` thumbnails, or aggregation endpoints returning 304s),
        respect it — we don't clobber handler intent.
      * ``/api/stream/*``: leave untouched so byte-range audio buffering
        stays healthy.
      * Other ``/api/*``: ``no-store`` so admin/library state stays fresh.
      * Static paths / SPA HTML / JS / CSS: ``no-store`` so edits are visible
        on the next refresh.  The ``?v=`` tag in ``index.html`` is static
        (not a content hash), so we deliberately don't mark versioned
        assets as immutable — that caused browsers to keep serving stale
        JS forever after code changes.  Re-enable later once ``?v=`` is
        bumped automatically on every build.
    """
    response = await call_next(request)
    path = request.url.path
    # Respect handler-set headers.  This matters for ETag 304s from
    # /api/library/* which choose their own revalidation policy.
    if "cache-control" in {k.lower() for k in response.headers.keys()}:
        return response

    if path.startswith("/api/stream"):
        return response  # media pipeline owns its own buffering

    if path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    else:
        # Static JS/CSS/HTML — serve fresh each load.
        response.headers["Cache-Control"] = "no-store"
    return response

# ── API routes ────────────────────────────────────────────────────────────────

app.include_router(tracks.router,    prefix="/api")
app.include_router(playlist.router,  prefix="/api")
app.include_router(art.router,       prefix="/api")
app.include_router(search.router,  prefix="/api")
app.include_router(stream.router,  prefix="/api")
app.include_router(library.router, prefix="/api")
app.include_router(fstree.router,  prefix="/api")
app.include_router(smart.router,   prefix="/api")
app.include_router(multiroom.router, prefix="/api")

# Lazy-import admin router (avoids import errors if optional deps missing)
try:
    from soniqboom.api import admin as _admin_mod
    app.include_router(_admin_mod.router, prefix="/api")
except Exception as _admin_err:
    log.warning("Admin API not loaded: %s", _admin_err)


@app.get("/api/plugins")
async def list_plugins():
    return registry.info()


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/ui-config")
async def ui_config():
    """Public UI configuration — no auth required.  Safe subset only."""
    from soniqboom.core.data import get_config
    return {
        "display_startup_logo": settings.display_startup_logo,
        "expose_local_files": settings.expose_local_files,
        "folder_aliases": settings.folder_aliases,
        "filter_duplicates": await get_config("filter_duplicates", False),
    }


# ── Lifecycle ─────────────────────────────────────────────────────────────────

_aof_writer = None
_merger_proc = None
_health_task = None


def _setup_logging(data_dir: Path) -> None:
    """Configure logging: console + rotating file in data_dir."""
    from logging.handlers import RotatingFileHandler

    fmt = "%(asctime)s %(levelname)-5s %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    # Console handler (keeps existing behaviour)
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    # File handler — 5 MB per file, 3 backups (20 MB total max)
    log_dir = data_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "soniqboom.log"
    file_h = RotatingFileHandler(
        str(log_path),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Remove any pre-existing handlers (e.g. from basicConfig in tests)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_h)


@app.on_event("startup")
async def startup():
    global _aof_writer, _merger_proc

    data_dir = get_data_dir()
    _setup_logging(data_dir)
    log.info("SoniqBoom %s starting on %s:%s", __version__, settings.host, settings.port)

    # Load snapshot + replay AOF → populate in-memory store + rebuild indexes
    from soniqboom.core.persistence import init_persistence
    init_persistence(data_dir)

    # Start AOF writer (appends every mutation to library.aof)
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    _aof_writer = AOFWriter(data_dir / "library.aof", flush_interval=settings.aof_flush_interval)
    get_store()._aof_append = _aof_writer.append
    await _aof_writer.start_auto_flush()

    # Spawn background merger process
    from soniqboom.core.merger import start_merger
    _merger_proc = start_merger(data_dir, settings.merger_interval)

    load_all()

    # One-shot maintenance: purge ghost tracks created from AppleDouble (``._*``)
    # sidecars on FTP/SMB shares and repair titles that leaked from temp-file
    # basenames in older builds.  Idempotent; cheap on a healthy library.
    try:
        from soniqboom.core.scanner import purge_junk_tracks
        await purge_junk_tracks()
    except Exception as exc:
        log.warning("purge_junk_tracks failed: %s", exc)

    log.info("SoniqBoom ready — %d tracks loaded", get_store().track_count())

    await _init_network_shares()
    global _health_task
    _health_task = asyncio.create_task(_share_health_monitor())



def _scan_root_for_share(share: dict) -> str:
    proto = share["protocol"].lower()
    host = share["host"]
    if proto == "smb":
        return f"smb://{host}/{share['share']}"
    if proto == "ftp":
        return f"ftp://{host}{share.get('remote_path', '/')}"
    return ""


async def _init_network_shares():
    """Connect to configured shares that have auto_connect enabled."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.credentials import decrypt
    from soniqboom.core.filesource import create_source, register_source
    from soniqboom.core.remote_cache import init_cache

    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    if not shares:
        return

    max_mb = int(conf.get("remote_cache_max_mb", 2048))
    cache_root = get_data_dir() / "cache" / "remote"
    init_cache(cache_root, max_mb)

    loop = asyncio.get_running_loop()
    for share_id, share in shares.items():
        if not share.get("auto_connect", True):
            continue
        scan_root = _scan_root_for_share(share)
        if not scan_root:
            continue
        try:
            password = decrypt(share.get("password_enc", "")) or ""
            source = create_source(share, password=password)
            ok = await loop.run_in_executor(None, source.is_dir, "/")
            if ok:
                register_source(scan_root, source)
                from soniqboom.core.data import upsert_scan_dir
                await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")
                log.info("Connected to share %s (%s)", share_id, scan_root)
            else:
                source.close()
                log.warning("Share %s: root not accessible", share_id)
        except Exception as exc:
            log.warning("Share %s connect failed: %s", share_id, exc)


async def _share_health_monitor():
    """Probe network shares periodically; flip status up and down as they come and go.

    Behaviour:
      * A share that's been healthy must fail **twice in a row** before we
        flip it to ``unavailable`` — absorbs single-probe blips.
      * Once unavailable, we keep probing (via the FileSource's own
        connect-retry logic) so recovery is automatic when the network
        comes back — no user action required.
      * Probe interval is adaptive: 60 s while everything is healthy, up
        to 5 min while any share is down (so a dead host isn't hammered),
        resetting to 60 s as soon as everything is green again.
      * Only log + upsert the DB when the status actually *changes*, so a
        long outage doesn't spam the log.
    """
    from soniqboom.core.data import upsert_scan_dir
    from soniqboom.core.filesource import all_sources
    from soniqboom.core.store import get_store

    # scan_root → current published status  ("ok" | "unavailable")
    last_status: dict[str, str] = {}
    # scan_root → consecutive failure count  (used only for the first flip)
    fail_streak: dict[str, int] = {}

    BASE_INTERVAL = 60.0
    MAX_INTERVAL = 300.0
    FLIP_THRESHOLD = 2   # consecutive fails before declaring "unavailable"
    PROBE_TIMEOUT = 10.0

    interval = BASE_INTERVAL

    while True:
        await asyncio.sleep(interval)
        loop = asyncio.get_running_loop()
        any_down = False

        for scan_root, source in list(all_sources().items()):
            try:
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, source.is_dir, "/"),
                    timeout=PROBE_TIMEOUT,
                )
            except Exception:
                ok = False

            prev = last_status.get(scan_root, "ok")

            if ok:
                fail_streak[scan_root] = 0
                if prev != "ok":
                    log.info("Share %s is back online", scan_root)
                    await _set_share_status(scan_root, "ok")
                    last_status[scan_root] = "ok"
            else:
                fail_streak[scan_root] = fail_streak.get(scan_root, 0) + 1
                any_down = True
                # First miss after being healthy: try an explicit reconnect —
                # the source object may just have a stale socket.  Success here
                # keeps the share "ok" without flipping state.
                if prev == "ok" and fail_streak[scan_root] == 1:
                    try:
                        recovered = await loop.run_in_executor(
                            None, source.reconnect,
                        )
                    except Exception:
                        recovered = False
                    if recovered:
                        # Verify: probe once more after reconnect.
                        try:
                            ok2 = await asyncio.wait_for(
                                loop.run_in_executor(None, source.is_dir, "/"),
                                timeout=PROBE_TIMEOUT,
                            )
                        except Exception:
                            ok2 = False
                        if ok2:
                            fail_streak[scan_root] = 0
                            log.info("Share %s recovered via reconnect", scan_root)
                            continue
                # Still down — flip state only after FLIP_THRESHOLD misses,
                # so one flaky probe doesn't alarm the UI.
                if prev == "ok" and fail_streak[scan_root] >= FLIP_THRESHOLD:
                    log.warning("Share %s became unavailable", scan_root)
                    await _set_share_status(scan_root, "unavailable")
                    last_status[scan_root] = "unavailable"

        # Adaptive cadence: back off while anything is down, snap back when healthy.
        interval = min(interval * 1.5, MAX_INTERVAL) if any_down else BASE_INTERVAL


async def _set_share_status(scan_root: str, status: str) -> None:
    """Update a share's status in the scan-dirs store.  Silent on missing row."""
    from soniqboom.core.data import upsert_scan_dir
    from soniqboom.core.store import get_store

    store = get_store()
    for sd in store.list_scan_dirs():
        if sd.get("path") == scan_root:
            await upsert_scan_dir(
                scan_root,
                network_share_id=sd.get("network_share_id"),
                status=status,
            )
            return


@app.on_event("shutdown")
async def shutdown():
    global _aof_writer, _merger_proc

    # Close any open library WebSockets so uvicorn's graceful-shutdown wait
    # doesn't park indefinitely on idle `await ws.receive_text()` loops.
    from soniqboom.api.library import _ws_clients
    for ws in list(_ws_clients):
        try:
            await ws.close(code=1001)
        except Exception:
            pass
    _ws_clients.clear()

    # Same treatment for multi-room WebSockets, across every active room.
    from soniqboom.api.multiroom import _rooms as _mr_rooms
    for room in list(_mr_rooms.values()):
        for client in list(room.clients.values()):
            try:
                await client.ws.close(code=1001)
            except Exception:
                pass
    _mr_rooms.clear()

    # Flush pending AOF writes
    if _aof_writer:
        _aof_writer.stop()

    # Write final snapshot so next startup is fast
    from soniqboom.core.persistence import write_snapshot_sync
    write_snapshot_sync(get_data_dir())

    # Terminate merger process
    if _merger_proc and _merger_proc.is_alive():
        _merger_proc.terminate()
        _merger_proc.join(timeout=3)

    # Stop health monitor and close network sources
    if _health_task:
        _health_task.cancel()
    from soniqboom.core.filesource import all_sources
    for src in all_sources().values():
        try:
            src.close()
        except Exception:
            pass


# ── Frontend static files ─────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    log.info("Serving frontend from %s", FRONTEND_DIR)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="frontend")

    @app.get("/m", include_in_schema=False)
    @app.get("/m/{rest:path}", include_in_schema=False)
    async def mobile_shell(rest: str = ""):
        return FileResponse(FRONTEND_DIR / "mobile.html")

    @app.get("/multiroom", include_in_schema=False)
    @app.get("/multiroom/{rest:path}", include_in_schema=False)
    async def multiroom_shell(rest: str = ""):
        return FileResponse(FRONTEND_DIR / "multiroom.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(FRONTEND_DIR / "index.html")
else:
    log.error(
        "Frontend directory not found — UI will return 404. "
        "Tried dev (%s), exe-dir/frontend, and .app Resources/frontend. "
        "If running a built app, the installer bundle is missing assets.",
        Path(__file__).resolve().parent / "frontend",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_port(host: str, port: int) -> None:
    """Raise SystemExit if the port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            print(
                f"ERROR: port {port} is already in use on {host}.\n"
                f"Use --port <number> to choose a different port.",
                file=sys.stderr,
            )
            sys.exit(1)


def _get_local_ips() -> list[str]:
    """Return all non-loopback IPv4 addresses for this machine."""
    ips: set[str] = set()

    # Primary: route trick — connect a UDP socket to find the outbound interface.
    # No data is actually sent; this just selects the OS routing table entry.
    for target in ("8.8.8.8", "10.255.255.255", "192.168.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    ips.add(ip)
                break
        except OSError:
            continue

    # Secondary: resolve the machine hostname (catches additional interfaces)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    return sorted(ips)


def _validate_and_print_banner(host: str, port: int) -> None:
    """
    Validate network reachability and print a startup banner listing every
    URL that clients can use to connect to SoniqBoom.
    """
    SEP  = "─" * 54
    localhost_only = host == "127.0.0.1"

    print(f"\n  {SEP}")
    print(f"  SoniqBoom {__version__}  ·  ready")
    print(f"  {SEP}")
    print(f"  Local:     http://localhost:{port}")

    if localhost_only:
        print(f"  Network:   ✗  not reachable from other machines")
        print(f"             (server is bound to 127.0.0.1)")
        print(f"             Fix: set server.host = \"0.0.0.0\" in SoniqBoom.conf")
    else:
        ips = _get_local_ips()
        hostname = socket.gethostname()
        # mDNS .local name works on all Apple devices without DNS
        mdns = hostname if hostname.endswith(".local") else f"{hostname}.local"

        if ips:
            label = "Network:"
            for ip in ips:
                # Validate the socket is actually reachable on this IP
                reachable = True
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.settimeout(0.5)
                        probe.bind((ip, 0))   # can we bind this interface?
                except OSError:
                    reachable = False

                status = "✓" if reachable else "?"
                print(f"  {label:<10} {status}  http://{ip}:{port}")
                label = " "               # indent subsequent IPs under the first

        print(f"  Hostname:  ✓  http://{mdns}:{port}")

    data_dir = get_data_dir()
    print(f"  Config:    {_CONF_PATH}")
    print(f"  Data:      {data_dir}")
    print(f"  {SEP}\n")


# ── CLI entry point ───────────────────────────────────────────────────────────

def cli():
    import argparse

    parser = argparse.ArgumentParser(
        prog="soniqboom",
        description="SoniqBoom music server",
    )
    sub = parser.add_subparsers(dest="cmd")

    # ── serve (default) ──────────────────────────────────────────────────────
    serve_p = sub.add_parser("serve", help="Start the server (default when no subcommand given)")
    serve_p.add_argument("--host", default=None, help="Bind host (default: from config)")
    serve_p.add_argument("--port", type=int, default=None, help="Bind port (default: 8080)")

    # ── export ───────────────────────────────────────────────────────────────
    exp_p = sub.add_parser("export", help="Export database to a .sbz file")
    exp_p.add_argument("file", help="Output path (e.g. backup.sbz)")

    # ── import ───────────────────────────────────────────────────────────────
    imp_p = sub.add_parser("import", help="Import database from a .sbz file")
    imp_p.add_argument("file", help="Input path (e.g. backup.sbz)")

    # Support `soniqboom --port 9090` without explicit 'serve' subcommand
    parser.add_argument("--host", default=None, dest="root_host")
    parser.add_argument("--port", type=int, default=None, dest="root_port")

    args = parser.parse_args()

    if args.cmd == "export":
        from soniqboom.cli.export_import import export_db
        asyncio.run(export_db(args.file))
        return

    if args.cmd == "import":
        from soniqboom.cli.export_import import import_db
        asyncio.run(import_db(args.file))
        return

    # Serve (default, with or without 'serve' subcommand)
    host = (
        getattr(args, "host", None)
        or args.root_host
        or settings.host
    )
    port = (
        getattr(args, "port", None)
        or args.root_port
        or settings.port
    )

    _check_port(host, port)
    _validate_and_print_banner(host, port)

    # Patch settings so startup handler sees the right values
    settings.host = host
    settings.port = port

    uvicorn.run(
        "soniqboom.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    cli()
