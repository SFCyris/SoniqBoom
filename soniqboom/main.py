# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SoniqBoom — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from soniqboom import __version__
from soniqboom.api import art, artist, cast, fstree, library, multiroom, playlist, search, smart, stream, subsonic, tracks, users as users_api
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

# Paths whose responses are already compressed (or carry byte-range audio/
# image bytes that browsers stream incrementally).  Re-gzipping audio adds
# CPU + latency and breaks Range without giving up any meaningful bytes.
_GZIP_SKIP_PREFIXES = (
    "/api/stream",
    "/api/rest/stream",
    "/api/rest/download",
    "/api/rest/getCoverArt",
    "/api/art",
    # Live radio relay — an infinite audio stream; gzip buffering would
    # stall it and the bytes are already compressed audio.
    "/api/stations/relay",
)


class _SelectiveGZipMiddleware:
    """GZip middleware that skips compression for audio / art endpoints.
    The stock GZipMiddleware applies to every response over ``minimum_size``,
    which clobbered ranged audio streams and re-encoded already-compressed
    JPEG/PNG thumbnails for no benefit.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1000) -> None:
        self._inner = GZipMiddleware(app, minimum_size=minimum_size)
        self._raw = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if any(path.startswith(p) for p in _GZIP_SKIP_PREFIXES):
                # Bypass GZip entirely — preserves Range headers + avoids
                # double-encoding already-compressed media.
                await self._raw(scope, receive, send)
                return
        await self._inner(scope, receive, send)


app.add_middleware(_SelectiveGZipMiddleware, minimum_size=1000)
# CORS: default to same-origin (localhost) only.  ``allow_origins=["*"]``
# would let any webpage in the user's browser drive the admin endpoints once
# auth is skipped — set ``SONIQBOOM_CORS_ORIGINS`` to a comma-separated list
# of explicit origins if you actually need cross-origin access.
#
# Note: any literal ``allow_origins=[f".../{settings.port}"]`` would freeze
# the port at import time, which is wrong because ``cli()`` may rewrite the
# port after parsing arguments.  Use a regex over the localhost loopback so
# whatever port the server eventually binds is accepted.
_extra_cors_raw = os.environ.get("SONIQBOOM_CORS_ORIGINS", "")
_extra_cors = [o.strip() for o in _extra_cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_origins=_extra_cors,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_auth_on_api(request: Request, call_next):
    """Gate every ``/api/*`` endpoint on a valid session, with a small
    public allowlist (login / register / ping / health / SPA shell).

    Before this middleware, individual routers had to remember to add a
    ``Depends(require_user)``.  The QA pen-test found that tracks /
    art / library / fstree / search / smart were all anonymous, which
    defeats the point of the stream-auth gate (a network neighbor could
    enumerate the entire library, listening history, and download cover
    art without logging in).

    On a fresh install with no users yet, the allowlist is wider so the
    bootstrap UI can finish setting up the first admin.  Once any user
    exists, only the explicit public paths slip through.
    """
    path = request.url.path
    # Anything outside /api passes through unconditionally — static SPA
    # assets, websockets (they have their own gate), and /rest/* (Subsonic
    # has its own auth in every handler).
    if not path.startswith("/api/"):
        return await call_next(request)
    # Public /api/* allowlist — endpoints the unauthenticated UI needs.
    if (
        path in {
            "/api/health",
            "/api/ui-config",
            "/api/plugins",
            "/api/auth/status",
            "/api/auth/reload",
            "/api/auth/login",
            "/api/auth/register",
            "/api/auth/me",
            "/api/auth/logout",          # idempotent on a missing session
            "/api/admin/auth",           # legacy OS auth handshake
            "/api/admin/auth/status",
            "/api/docs",
            "/api/openapi.json",
        }
        or path.startswith("/api/docs/")
    ):
        return await call_next(request)
    # WebSocket endpoints handle their own auth in-handler — the HTTP
    # layer never sees an Upgrade request as middleware here, but
    # being defensive about the upgrade prefix is cheap.
    if path.endswith("/ws"):
        return await call_next(request)
    # Stream + Subsonic stream are auth-gated inside their handlers
    # (they also accept Subsonic-style ?u=&p= which middleware can't
    # easily validate without duplicating that logic), so let them
    # through here and trust the handler.
    if path.startswith(("/api/stream/", "/api/rest/")) or path == "/api/stream":
        return await call_next(request)

    # Default: require a valid session cookie OR the legacy admin token.
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        # User store not initialised yet (very early boot).  Let through
        # so health/status work, but log so we notice in deployment.
        return await call_next(request)
    # Pre-bootstrap: no users → allowlist is effectively everything so the
    # operator can finish the first-time setup without auth.
    if not store.has_any():
        return await call_next(request)

    cookie = request.cookies.get("sb_session")
    if cookie and store.lookup_session(cookie):
        return await call_next(request)
    # Legacy admin token still honoured for back-compat with single-tenant
    # installs that use SONIQBOOM_ADMIN_TOKEN.
    admin_tok = request.headers.get("x-admin-token") or request.headers.get("X-Admin-Token")
    if admin_tok:
        try:
            from soniqboom.api import admin as _admin_mod
            if (admin_tok == _admin_mod._static_admin_token
                or admin_tok in _admin_mod._tokens):
                return await call_next(request)
        except Exception:
            pass
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "Sign in to access this endpoint."}, status_code=401)


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


@app.middleware("http")
async def watchdog_track(request: Request, call_next):
    """Register every HTTP request with the deadlock watchdog.

    A stale entry (request still in-flight after the configured
    threshold, default 90 s) triggers a full-thread stack dump in the
    log so the operator can identify the lock-holder instead of
    resorting to ``kill -9``.

    Stream + WebSocket paths are skipped inside :mod:`deadlock_watchdog`
    so legitimate long-lived requests don't false-positive.
    """
    from soniqboom.core import deadlock_watchdog
    token = deadlock_watchdog.begin_request(
        request.method,
        request.url.path,
        client=f"{request.client.host}:{request.client.port}" if request.client else "",
    )
    try:
        return await call_next(request)
    finally:
        deadlock_watchdog.end_request(token)


# ── API routes ────────────────────────────────────────────────────────────────

app.include_router(tracks.router,    prefix="/api")
app.include_router(playlist.router,  prefix="/api")
app.include_router(art.router,       prefix="/api")
app.include_router(search.router,  prefix="/api")
app.include_router(stream.router,  prefix="/api")
app.include_router(library.router, prefix="/api")
app.include_router(fstree.router,  prefix="/api")
app.include_router(smart.router,   prefix="/api")
app.include_router(artist.router,  prefix="/api")
app.include_router(users_api.router, prefix="/api")

from soniqboom.api import stations as _stations_api  # noqa: E402
app.include_router(_stations_api.router, prefix="/api")

# ── Optional access services ────────────────────────────────────────────
# Each gated by ``services.<name>`` in SoniqBoom.conf (default ON).  Toggle
# via Settings → Services, ``soniqboom services enable|disable <name>``,
# or by editing the conf file directly.  Disabled services don't mount
# their router — and we explicitly 404 their URL prefixes BEFORE the SPA
# catch-all so a Subsonic client gets a clean 404 / JSON error instead of
# the SPA's index.html (HTML to a JSON client is a hard-to-diagnose
# misconfiguration).
from soniqboom.config import is_service_enabled as _svc_on

if _svc_on("multiroom"):
    app.include_router(multiroom.router, prefix="/api")
else:
    @app.get("/api/multiroom/{rest:path}", include_in_schema=False)
    @app.post("/api/multiroom/{rest:path}", include_in_schema=False)
    async def _multiroom_disabled(rest: str = ""):
        raise HTTPException(404, "Multiroom service is disabled — enable it in Settings → Services.")

if _svc_on("cast"):
    app.include_router(cast.router, prefix="/api")
    # Anonymous byte-server for Cast / DLNA / AirPlay renderers.  Lives
    # OUTSIDE /api/ so the cookie-auth middleware doesn't block dumb
    # DLNA renderers that can't carry session cookies — token in the
    # URL path is the auth.  See cast_stream.py for the rationale.
    from soniqboom.api import cast_stream as _cast_stream
    app.include_router(_cast_stream.router)
else:
    @app.get("/api/cast/{rest:path}", include_in_schema=False)
    @app.post("/api/cast/{rest:path}", include_in_schema=False)
    async def _cast_disabled(rest: str = ""):
        raise HTTPException(404, "Cast service is disabled — enable it in Settings → Services.")
    @app.get("/cast/{rest:path}", include_in_schema=False)
    async def _cast_stream_disabled(rest: str = ""):
        # Mirrors the disabled-service handler so a stale signed URL
        # produces a clean 404 instead of falling through to the SPA.
        raise HTTPException(404, "Cast service is disabled.")

# DLNA Media Server (incoming) — SSDP + UPnP-AV ContentDirectory.
# Mounts the HTTP-side endpoints when the toggle is on; the SSDP
# socket itself only spins up via _start_dlna_server in the startup
# hook below.
if _svc_on("dlna_server"):
    from soniqboom.api import dlna_upnp as _dlna_upnp
    app.include_router(_dlna_upnp.router)
else:
    @app.api_route("/dlna/{rest:path}", methods=["GET", "POST", "SUBSCRIBE", "UNSUBSCRIBE"], include_in_schema=False)
    async def _dlna_disabled(rest: str = ""):
        raise HTTPException(404, "DLNA Media Server is disabled — enable it in Settings → Services.")

if _svc_on("subsonic"):
    # Subsonic mounts under /rest/* (not /api), matching the Subsonic spec
    # so DSub / Symfonium / Substreamer can talk to SoniqBoom unmodified.
    app.include_router(subsonic.router)
else:
    @app.get("/rest/{rest:path}", include_in_schema=False)
    @app.post("/rest/{rest:path}", include_in_schema=False)
    async def _subsonic_disabled(rest: str = ""):
        # Subsonic clients expect a structured error envelope; emit a
        # minimal one so they fail visibly instead of treating the HTML
        # SPA fallback as an opaque server error.
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"subsonic-response": {
                "status": "failed",
                "version": "1.16.1",
                "error": {"code": 0, "message": "OpenSubsonic service is disabled on this server."},
            }},
            status_code=404,
        )

# Lazy-import admin router (avoids import errors if optional deps missing)
try:
    from soniqboom.api import admin as _admin_mod
    app.include_router(_admin_mod.router, prefix="/api")
except Exception as _admin_err:
    log.warning("Admin API not loaded: %s", _admin_err)


@app.get("/api/plugins")
async def list_plugins():
    return registry.info()


# ── Ampache-probe rejector ──────────────────────────────────────────────
# Amperfy (and a few other multi-protocol clients) probe Ampache before
# Subsonic: ``GET /server/xml.server.php?action=handshake&...``.  Without
# this explicit 404, the SPA catch-all returns the index.html HTML body
# with status 200, which Amperfy mistakes for a "maybe Ampache" response
# and gets stuck retrying the Ampache handshake instead of falling
# through to Subsonic.  Returning a real 404 makes the client move on.
@app.get("/server/xml.server.php", include_in_schema=False)
async def _ampache_probe_reject():
    raise HTTPException(404, "This server speaks Subsonic, not Ampache.")


@app.get("/api/health")
async def health():
    # Include the current service-toggle state in the health payload so
    # the run.sh / restart.sh banner can render "Services:" without
    # needing to parse the Python log.  Public — no PII, just on/off.
    from soniqboom.config import (
        SERVICE_NAMES, SERVICE_LABELS, is_service_enabled,
    )
    return {
        "status":  "ok",
        "version": __version__,
        "services": [
            {
                "name":    n,
                "label":   SERVICE_LABELS.get(n, n),
                "enabled": is_service_enabled(n),
            }
            for n in SERVICE_NAMES
        ],
    }


def _require_admin_session(request: Request):
    """Local admin gate for top-level routes in main.py.

    The api/users.py ``require_admin`` Depends is the canonical check
    elsewhere, but importing it at module load risks a circular import.
    This wrapper does the cookie/token check inline using the already-
    imported user store.
    """
    cookie = request.cookies.get("sb_session")
    if not cookie:
        raise HTTPException(401, "Sign in required.")
    try:
        from soniqboom.core.users import get_user_store
        user = get_user_store().lookup_session(cookie)
    except Exception:
        raise HTTPException(401, "Auth unavailable.")
    if not user or not user.enabled or user.role != "admin":
        raise HTTPException(403, "Admin role required.")
    return user


@app.get("/api/admin/metrics")
async def admin_metrics(_admin=Depends(_require_admin_session)):
    """Lightweight observability endpoint exposing what counters the
    backend currently keeps.  Modules that don't yet publish counters
    contribute a stub so the schema is stable for monitoring tooling.

    Admin-gated — the counters reveal cache sizes, track counts, and
    version metadata that we don't want leaked to anyone on the LAN
    (R2/R3 finding: was previously unauthenticated).
    """
    # Read from modules that already maintain counters.  Modules without
    # exposed counters surface as None so the shape stays stable; we'll
    # add them as the modules grow them.
    metrics: dict = {
        "version": __version__,
        # Cache hit/miss totals are aggregated across every tier that
        # publishes counters via cache_stats (conversion, art, zip-extract,
        # …); the per-tier breakdown lands under "cache_stats" below.
        "cache_hit": None,
        "cache_miss": None,
        "render_failure": None,
        "prewarm_evictions": None,
        "growing_file_timeouts": None,
        "aof_flock_contention": None,
    }
    # Cross-tier cache counters (cache_stats keeps hit/miss per tier).
    try:
        from soniqboom.core import cache_stats as _cstats
        snap = _cstats.snapshot()
        tiers = snap.get("tiers", {})
        metrics["cache_hit"] = sum(int(t.get("hits", 0) or 0) for t in tiers.values())
        metrics["cache_miss"] = sum(int(t.get("misses", 0) or 0) for t in tiers.values())
        metrics["cache_stats"] = snap
    except Exception:
        metrics["cache_stats"] = None
    # Available now: track count + art-absent count.
    try:
        from soniqboom.core.store import get_store
        store = get_store()
        metrics["track_count"] = store.track_count()
        metrics["art_absent_count"] = len(store._art_absent)
    except Exception:
        pass
    # Conversion cache stats (already shaped for the Settings panel).
    try:
        from soniqboom.core.conversion_cache import cache_stats
        cs = await cache_stats()
        metrics["conversion_cache"] = cs
    except Exception:
        metrics["conversion_cache"] = None
    # Remote cache stats — total size + entry count.
    try:
        from soniqboom.core.remote_cache import get_cache
        rc = get_cache()
        metrics["remote_cache"] = {
            "bytes": rc.total_size(),
            "entries": rc.entry_count(),
            "max_mb": rc.max_mb,
        }
    except Exception:
        metrics["remote_cache"] = None
    return metrics


@app.get("/api/ui-config")
async def ui_config():
    """Public UI configuration — no auth required.  Safe subset only.

    ``placeholder_art_data_uri`` ships the cover-art placeholder JPEG
    inline (base64-encoded) so the frontend can render tagless tracks
    without an HTTP round-trip to ``/api/art/{id}``.  The ~1.5 KB
    payload is downloaded ONCE on app load; thereafter every track
    list with N tagless rows skips N HTTP requests.
    """
    import base64
    from soniqboom.core.data import get_config
    from soniqboom.api.art import _PLACEHOLDER_JPEG
    placeholder_b64 = base64.b64encode(_PLACEHOLDER_JPEG).decode("ascii")
    return {
        "display_startup_logo": settings.display_startup_logo,
        "expose_local_files": settings.expose_local_files,
        "folder_aliases": settings.folder_aliases,
        "filter_duplicates": await get_config("filter_duplicates", False),
        "placeholder_art_data_uri": f"data:image/jpeg;base64,{placeholder_b64}",
    }


# ── Lifecycle ─────────────────────────────────────────────────────────────────

_aof_writer = None
_merger_proc = None
_health_task = None
_cast_reaper_task = None


def _setup_logging(data_dir: Path) -> None:
    """Configure logging: console (only when stderr is a TTY) + rotating
    file in ``data_dir``.

    Why the TTY check on the console handler: ``run.sh`` launches the
    server with ``>> "$LOG_FILE" 2>&1``, redirecting stderr into the same
    ``soniqboom.log`` that the ``RotatingFileHandler`` below writes to
    directly.  With an unconditional ``StreamHandler`` on stderr we'd
    write every record TWICE — once via Python's file handler, once via
    the shell redirect of our stderr.  Detecting "stderr is captured,
    not a terminal" via ``isatty()`` is the cheapest reliable signal:
      * ``run.sh`` / launchd / systemd / nohup all have stderr pointing
        at a file or pipe → not a tty → skip the StreamHandler.
      * Developer running ``python -m soniqboom.main`` in a terminal
        keeps the live console output.
      * Tests that capture stderr (pytest with ``capfd``) are also not
        a tty, so we skip there too, which is what we want.
    """
    import sys
    from logging.handlers import RotatingFileHandler

    fmt = "%(asctime)s %(levelname)-5s %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

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
    # Remove any pre-existing handlers (e.g. from basicConfig in tests,
    # or a stale config left over by a previous ``_setup_logging`` call
    # in the same process — uvicorn ``--reload`` re-runs startup).
    root.handlers.clear()
    root.addHandler(file_h)

    # Console handler only when stderr is an interactive terminal —
    # otherwise the shell redirect in run.sh would double the file.
    if getattr(sys.stderr, "isatty", lambda: False)():
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def _install_sighup_handler() -> None:
    """Treat SIGHUP the same as SIGTERM so a terminal-close (run.sh path on
    macOS) triggers FastAPI's shutdown event — otherwise the AOF buffer is
    dropped and the merger child is orphaned.
    """
    import signal as _signal
    try:
        # signal.SIGTERM is the one uvicorn handles; redirecting SIGHUP to
        # the same handler is the supported way to extend its trapped set.
        prev = _signal.getsignal(_signal.SIGTERM)
        if callable(prev):
            _signal.signal(_signal.SIGHUP, prev)
    except (AttributeError, ValueError, OSError):
        # Windows (no SIGHUP), signal-handlers-not-installed-yet (workers
        # haven't bound their own yet), or non-main-thread — silently skip.
        pass


def _reap_orphaned_forkservers() -> int:
    """Kill multiprocessing.forkserver children left over from a
    previously-killed soniqboom instance.

    Why this exists
    ---------------
    When the server is SIGKILL'd (force-quit, OOM, ``shutdown.sh -9``)
    or its SIGTERM handler times out, the ProcessPoolExecutor used by
    the scanner doesn't get a chance to call ``executor.shutdown``.
    The forkserver process tree (one ``forkserver`` daemon + N worker
    children) is left orphaned to PPID=1 and survives indefinitely.
    Each orphan holds ~300 MB resident memory; across 4 hard kills in
    a session that's ~2.4 GB stranded RAM that never frees until
    reboot.

    Why we only kill ``PPID==1`` matches
    ------------------------------------
    macOS has no ``PR_SET_PDEATHSIG`` equivalent, so the kernel won't
    auto-reap on parent death.  We could check process groups, but
    PPID=1 is the simplest signal that the parent is genuinely gone
    (only ``init`` has PID 1 on macOS; a forkserver normally lives
    under its spawning Python process).  This avoids killing
    forkservers belonging to OTHER live soniqboom instances if the
    user is somehow running more than one (unusual — the menubar's
    ``_kill_existing_instances`` already enforces singleton — but
    correct under that edge case).

    Returns the number of processes signalled.  Silent on permission
    errors (signalling someone else's process) since we filter to
    matching-cmdline-only.
    """
    import subprocess
    me = os.getpid()
    try:
        out = subprocess.run(
            ["pgrep", "-f", "multiprocessing.forkserver.*SoniqBoom"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return 0
    pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
    reaped = 0
    for pid in pids:
        if pid == me:
            continue
        # Look up the parent.  Only orphans (PPID==1) are safe to kill
        # — a forkserver with a live non-1 parent belongs to that
        # process and we shouldn't touch it.
        try:
            ppid_str = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=1,
            ).stdout.strip()
            ppid = int(ppid_str or 0)
        except (subprocess.SubprocessError, ValueError):
            continue
        if ppid != 1:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            reaped += 1
            log.info("reap: SIGTERM orphaned forkserver pid=%d", pid)
        except OSError:
            pass
    # Give them a beat to exit cleanly, then SIGKILL the survivors.
    if reaped:
        import time as _t
        _t.sleep(0.5)
        for pid in pids:
            if pid == me:
                continue
            try:
                os.kill(pid, 0)  # check alive
                os.kill(pid, signal.SIGKILL)
                log.info("reap: SIGKILL stubborn forkserver pid=%d", pid)
            except OSError:
                pass  # already dead — good
    return reaped


@app.on_event("startup")
async def startup():
    global _aof_writer, _merger_proc

    data_dir = get_data_dir()
    _setup_logging(data_dir)
    log.info("SoniqBoom %s starting on %s:%s", __version__, settings.host, settings.port)
    _install_sighup_handler()

    # Reap any forkserver-tree leftovers from a previous instance that
    # was SIGKILL'd (or whose SIGTERM handler timed out).  Must run
    # BEFORE the first ProcessPoolExecutor() in scanner.py creates our
    # OWN forkserver, otherwise we'd risk reaping fresh children.
    reaped = _reap_orphaned_forkservers()
    if reaped:
        log.info("reaped %d orphaned forkserver process(es) from prior session", reaped)

    # Begin emitting structured phase markers (stderr + status file).
    # The misleading "ready" banner from cli() already printed before
    # uvicorn called us, so the first thing the user sees here is the
    # tracker's opening line — confirming the slow work is starting and
    # not just hung after the banner.
    from soniqboom.core.startup_status import init as _ss_init, set_phase as _ss_phase
    _ss_init(data_dir)

    # Load snapshot + replay AOF → populate in-memory store + rebuild indexes
    _ss_phase("loading_library", "Loading library snapshot")
    from soniqboom.core.persistence import init_persistence
    init_persistence(data_dir)

    # Pre-warm the per-scan-root sorted cache for every LOCAL scan root.
    # Pays the one-time O(bucket-size) iterate + TrackMeta shape cost
    # NOW — while the splash is already displayed — so the first user
    # click on any subfolder under any local scan root lands warm
    # instead of paying ~1.2 s (SID, 56K) / ~3 s (modarchive, 111K) on
    # interaction.  Remote (smb://, ftp://) roots are skipped — they
    # use ``store.filter_tracks`` directly via ``_remote_tracks_with_meta``
    # and don't benefit from this index.
    _ss_phase("warmup_browse", "Pre-warming folder browse cache")
    t_warm = time.monotonic()
    try:
        from soniqboom.api.fstree import warmup_scan_root_caches
        # Pass data_dir so the cache is restored from
        # ``{data_dir}/browse_cache.pickle`` on warm boots (and re-saved
        # whenever a scan-root bucket size changes).  Cold boot still
        # pays the full ~5 s build, but every subsequent boot — until a
        # scan adds/removes tracks — comes up in ~300 ms for that phase.
        warm_counts = warmup_scan_root_caches(data_dir)
        warm_total = sum(warm_counts.values())
        log.info(
            "Folder browse cache warmed: %d local root(s), %d total tracks indexed in %.0fms",
            len(warm_counts), warm_total, (time.monotonic() - t_warm) * 1000,
        )
    except Exception as exc:
        log.warning("Folder browse cache warmup failed (will lazy-build on first click): %s", exc)

    # Initialise user store (users.json).  Safe even on fresh installs —
    # ``has_any()`` returns False until the first ``soniqboom-setadm`` run.
    _ss_phase("loading_users", "Loading user accounts")
    from soniqboom.core.users import init_user_store
    init_user_store(data_dir)

    # AirPlay pairing credentials.  Stored at ``data_dir/airplay_credentials.json``
    # and applied on each ``AirPlayController.connect()`` so the user
    # doesn't have to retype the 4-digit PIN every session.  Init is cheap
    # (just records the data_dir path); the actual file is read lazily on
    # first lookup.
    from soniqboom.core import airplay_credentials
    airplay_credentials.init(data_dir)

    # FTP pool: persisted server-cap detection state.  Same init pattern —
    # records the data_dir path; the JSON file is read lazily on the first
    # cap lookup.  Without init the cap-detection logic is a no-op (still
    # safe; the pool just uses user-configured budgets directly).
    from soniqboom.core import ftp_pool_config
    ftp_pool_config.init(data_dir)

    # Configure HVSC if the user pointed at a DOCUMENTS folder.  The
    # database loads lazily on first SID lookup, so this is cheap.
    if settings.hvsc_docs_path:
        from soniqboom.core.hvsc import get_hvsc
        get_hvsc().configure(settings.hvsc_docs_path)

    # Rebuild conversion-cache metadata from disk so previously-rendered
    # SID / MIDI / tracker WAVs survive a restart — otherwise the next play
    # of a known track wastes a render because the in-memory _meta is empty.
    _ss_phase("conversion_cache", "Adopting conversion cache")
    try:
        from soniqboom.core.conversion_cache import warmup_from_disk
        adopted = await asyncio.to_thread(warmup_from_disk)
        if adopted:
            log.info("Conversion cache warmup: adopted %d existing render(s)", adopted)
    except Exception:
        log.exception("Conversion cache warmup failed (non-fatal — renders will rebuild on demand)")

    _ss_phase("ffmpeg", "Selecting ffmpeg binary")
    # ── Resolve ffmpeg path ──────────────────────────────────────────────
    # If the operator hasn't pinned ``settings.ffmpeg_path`` in config, we
    # decide at startup: prefer the system ffmpeg when it has all the
    # demuxers we need for DSD playback (dsf/iff/wsd); otherwise fall back
    # to the bundled one we drop into the data dir (see fetch_ffmpeg.py /
    # ``soniqboom fetch-ffmpeg``).  This is a runtime-only override — it
    # mutates settings in memory, not on disk.
    try:
        from pathlib import Path as _P
        import shutil as _sh, subprocess as _sub

        def _probe_dsd(binary: str) -> set[str]:
            try:
                r = _sub.run([binary, "-hide_banner", "-formats"],
                             capture_output=True, text=True, timeout=10, check=False)
            except (FileNotFoundError, _sub.SubprocessError):
                return set()
            have: set[str] = set()
            for ln in (r.stdout or "").splitlines():
                cols = ln.strip().split(None, 2)
                if len(cols) < 2 or "D" not in cols[0]:
                    continue
                nm = cols[1].lower()
                if nm in ("dsf", "iff", "wsd"):
                    have.add(nm)
            return have

        def _probe_encoders(binary: str) -> set[str]:
            """Return the set of REQUIRED encoder/filter names that are
            present in ``binary``.  Demuxer support alone isn't enough —
            Homebrew's stock ffmpeg can decode DSF but ships without
            libmp3lame, libvorbis, etc., so a Subsonic client asking for
            ``format=mp3`` fails with "Could not open encoder before EOF".
            We probe the encoder list once at startup and prefer the
            bundled binary when the system one is missing any.
            """
            try:
                r = _sub.run([binary, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=10, check=False)
                e_out = (r.stdout or "") + (r.stderr or "")
            except (FileNotFoundError, _sub.SubprocessError):
                return set()
            have: set[str] = set()
            for name in ("libmp3lame", "libvorbis", "libopus", "aac",
                         "flac", "pcm_s24le", "pcm_s16le"):
                if name in e_out:
                    have.add(name)
            return have

        def _ffmpeg_runs(binary: str) -> bool:
            """Liveness check — True if ``binary -version`` exits 0.

            Kept SEPARATE from the feature probes: this is what decides whether
            to trust a bundled binary, and it must not be fooled by a probe
            that merely timed out under heavy scan load (the old code abandoned
            a perfectly good bundled ffmpeg whenever the feature probe came back
            empty, which forced a needless reinstall).  Retried for exactly
            that reason, with a generous timeout.
            """
            for _ in range(2):
                try:
                    r = _sub.run([binary, "-version"], capture_output=True,
                                 text=True, timeout=20, check=False)
                    if r.returncode == 0 and "ffmpeg version" in (r.stdout or "").lower():
                        return True
                except (FileNotFoundError, _sub.SubprocessError):
                    pass
            return False

        # Demuxers we need to read DSD / general containers.
        required_demuxers = {"dsf", "iff", "wsd"}
        # Encoders we need to deliver to clients (esp. Subsonic
        # ``format=mp3`` / ``format=ogg`` requests).  AAC is bundled into
        # every modern ffmpeg via the native encoder; libmp3lame +
        # libvorbis come from --enable-libmp3lame / --enable-libvorbis
        # and are NOT present in the Homebrew default bottle.
        required_encoders = {"libmp3lame", "libvorbis", "flac", "pcm_s24le"}
        operator_pinned = bool((settings.ffmpeg_path or "").strip()) and \
                          settings.ffmpeg_path != "ffmpeg"

        if not operator_pinned:
            system = _sh.which("ffmpeg")
            bundled = _P(get_data_dir()) / "bin" / "ffmpeg"

            sys_demux = _probe_dsd(system) if system else set()
            sys_enc   = _probe_encoders(system) if system else set()
            sys_ok = bool(system) and required_demuxers.issubset(sys_demux) \
                            and required_encoders.issubset(sys_enc)

            # A bundled binary is ONLY ever written by fetch_ffmpeg AFTER it
            # passed the full feature check, and a binary doesn't lose codecs
            # over time — so a present, real-sized, *executable* bundled copy
            # is trusted.  We gate on "does it run" (a liveness check), NOT on
            # the feature probe: that probe spawns ffmpeg with a short timeout
            # and returned empty under heavy scan load, which falsely condemned
            # a perfectly good binary and forced a reinstall.  The size gate
            # rejects a truncated/zero-byte file from a previously-interrupted
            # install.  Feature probes stay diagnostic-only for the bundled one.
            try:
                bundled_present = bundled.is_file() and bundled.stat().st_size > 1_000_000
            except OSError:
                bundled_present = False
            bundled_runs = bundled_present and _ffmpeg_runs(str(bundled))

            # Preference order:
            #   1. bundled, if it EXISTS and EXECUTES (complete-by-installation)
            #   2. system, if fully capable
            #   3. system (incomplete) with a loud, actionable warning
            #   4. nothing — fatal log
            if bundled_runs:
                settings.ffmpeg_path = str(bundled)
                log.info("ffmpeg: using bundled binary at %s "
                         "(installed complete; verified it executes).", bundled)
                if system and not sys_ok:
                    log.info(
                        "ffmpeg: system binary at %s is missing %s — "
                        "the bundled copy is preferred.",
                        system,
                        sorted((required_demuxers - sys_demux) | (required_encoders - sys_enc)),
                    )
            elif sys_ok:
                settings.ffmpeg_path = system
                log.info("ffmpeg: using system binary at %s "
                         "(full feature set).", system)
            elif system:
                settings.ffmpeg_path = system
                missing_sys = sorted(
                    (required_demuxers - sys_demux) | (required_encoders - sys_enc),
                )
                if bundled_present:
                    log.warning(
                        "ffmpeg: bundled binary at %s EXISTS but failed to execute — "
                        "falling back to system ffmpeg (%s, missing %s).  Re-download the "
                        "bundled ffmpeg from Admin → Renderers, or run "
                        "`soniqboom fetch-ffmpeg --force`.",
                        bundled, system, missing_sys,
                    )
                else:
                    log.warning(
                        "ffmpeg: system binary at %s is missing %s — "
                        "DSD/lossy transcoded playback will fail until "
                        "you run `soniqboom fetch-ffmpeg` to install a "
                        "complete bundled ffmpeg.",
                        system, missing_sys,
                    )
            else:
                if bundled_present:
                    log.error(
                        "ffmpeg: bundled binary at %s exists but failed to execute and no "
                        "system ffmpeg was found.  Re-download the bundled ffmpeg or install "
                        "ffmpeg via your package manager.  Transcoded playback will fail.",
                        bundled,
                    )
                else:
                    log.error(
                        "ffmpeg: no binary found.  Install ffmpeg via your package "
                        "manager or run `soniqboom fetch-ffmpeg` to download a "
                        "bundled copy.  Transcoded playback will fail until this is fixed."
                    )
    except Exception:
        log.exception("ffmpeg path resolution failed (non-fatal — falling back to config)")

    # Probe ffmpeg for DSD-relevant demuxers so the operator gets an early
    # heads-up if their build is so stripped-down it can't play any DSD file.
    # ffmpeg has three handlers we care about:
    #   - ``dsf``  — DSF (Sony's DSD Stream File).  Standalone demuxer.
    #   - ``wsd``  — WSD (Wideband Single-bit Data).  Standalone demuxer.
    #   - ``iff``  — IFF, which is *also* how DSDIFF (DFF) is parsed; the
    #                iff demuxer has had DSD chunk support since 2014.
    # All three ship with stock ffmpeg builds (Homebrew, apt, BtbN static).
    # The probe is informational only — actual transcode failures surface
    # at stream time with a clear "Invalid data" from ffmpeg.
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.ffmpeg_path or "ffmpeg", "-hide_banner", "-formats",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        formats = out.decode("utf-8", "replace").lower()
        # Format-list rows look like "  D   dsf   DSD Stream File (DSF)"
        # so anchor on the demuxer flag column to avoid false hits on
        # descriptions that happen to contain "dsf"/"iff"/"wsd".
        present = {
            "dsf": "\n d   dsf " in "\n" + formats or " d   dsf " in formats,
            "wsd": " d   wsd " in formats,
            "iff": " d   iff " in formats,  # this is how DFF is parsed
        }
        missing = [k for k, v in present.items() if not v]
        if missing:
            log.warning(
                "ffmpeg is missing demuxers for DSD playback: %s. "
                "DSF needs the 'dsf' demuxer, DFF needs 'iff', "
                "WSD needs 'wsd'.  Affected files will fail to play.",
                ", ".join(missing),
            )
        else:
            log.info("ffmpeg has all DSD demuxers (dsf/iff/wsd) — DSF/DFF/WSD playback ready.")
    except Exception:
        log.debug("DSD demuxer probe failed (non-fatal)", exc_info=True)

    _ss_phase("background_tasks", "Starting background services")
    # Kick off the scrobble retry pump — drains failures every 60s.  Stored
    # on the app state so the shutdown handler can cancel it cleanly.
    from soniqboom.core.scrobble import retry_loop
    app.state._scrobble_task = asyncio.create_task(retry_loop())

    # Filesystem watcher — auto-rescan on file changes inside any scan root.
    # Skipped silently for remote shares (SMB/FTP) — they have no push API.
    try:
        from soniqboom.core import watcher
        from soniqboom.core.data import list_scan_dirs as _list_dirs
        dirs = await _list_dirs()
        local_roots = [
            d["path"] for d in dirs
            if not str(d.get("path", "")).startswith(("smb://", "ftp://", "http://", "https://"))
        ]
        if watcher.is_supported() and local_roots:
            await watcher.start(local_roots)
    except Exception:
        log.exception("watcher start failed (non-fatal — manual scan still works)")

    # Remote freshness — adaptive background polling for FTP/SMB shares
    # (no push notifications available).  Skips when pool is saturated;
    # only fires a WebSocket toast when NEW tracks were added.
    try:
        from soniqboom.core import remote_freshness
        from soniqboom.core.filesource import get_source
        from soniqboom.api.library import _broadcast

        async def _on_new_tracks(scan_root: str, count: int, _sample_titles: list[str]) -> None:
            """Push a 'new tracks discovered' event over the library WS so the
            frontend can render a toast.  Sample titles are empty for now —
            frontend looks up share alias via its existing config and shows
            'N new tracks in <alias>'."""
            try:
                await _broadcast({
                    "event":       "remote_new_tracks",
                    "scan_root":   scan_root,
                    "count":       int(count),
                })
            except Exception:
                log.exception("freshness: _broadcast(remote_new_tracks) failed")

        await remote_freshness.start(
            data_dir=data_dir,
            source_lookup=get_source,
            on_new_tracks=_on_new_tracks,
        )
    except Exception:
        log.exception("freshness start failed (non-fatal — manual re-index still works)")

    _ss_phase("persistence", "Starting AOF writer & merger")
    # Start AOF writer (appends every mutation to library.aof)
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store
    _aof_writer = AOFWriter(data_dir / "library.aof", flush_interval=settings.aof_flush_interval)
    get_store()._aof_append = _aof_writer.append
    await _aof_writer.start_auto_flush()

    # Spawn background merger process
    from soniqboom.core.merger import start_merger
    _merger_proc = start_merger(data_dir, settings.merger_interval)

    _ss_phase("plugins", "Loading plugins")
    load_all()

    _ss_phase("maintenance", "Running startup maintenance")
    # One-shot maintenance: purge ghost tracks created from AppleDouble (``._*``)
    # sidecars on FTP/SMB shares and repair titles that leaked from temp-file
    # basenames in older builds.  Idempotent; cheap on a healthy library.
    try:
        from soniqboom.core.scanner import purge_junk_tracks
        await purge_junk_tracks()
    except Exception as exc:
        log.warning("purge_junk_tracks failed: %s", exc)

    # Reap any ``.partial`` orphans from interrupted soundfont uploads —
    # crashed uploads used to leave ``<name>.<pid>.<token>.partial`` in
    # the soundfonts dir indefinitely.
    try:
        from soniqboom.config import get_soundfonts_dir
        sf_dir = get_soundfonts_dir()
        if sf_dir.exists():
            removed = 0
            for f in sf_dir.iterdir():
                # Match both the new ``<base>.<pid>.<tok>.partial`` form
                # and any legacy ``<base>.partial`` from earlier builds.
                if f.is_file() and f.name.endswith(".partial"):
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            if removed:
                log.info("Reaped %d orphaned .partial soundfont upload(s)", removed)
    except Exception as exc:
        log.warning("Soundfont .partial reap failed: %s", exc)

    log.info("SoniqBoom ready — %d tracks loaded", get_store().track_count())

    _ss_phase("network_shares", "Connecting network shares")
    await _init_network_shares()
    global _health_task
    _health_task = asyncio.create_task(_share_health_monitor())

    # DLNA Media Server (incoming) — only spin up the SSDP socket when
    # the service is enabled.  We deliberately defer this until after
    # network share init so the LAN IP detection picks up the right
    # interface (sometimes ifconfig settles a beat after process start).
    if _svc_on("dlna_server"):
        _ss_phase("dlna_server", "Announcing DLNA Media Server")
        await _start_dlna_server()

    # Periodically reap idle Cast / DLNA / AirPlay sessions.  Without
    # this, a user who picked a target, walked away, then came back to
    # the SoniqBoom tab would still see the session "live" on the
    # backend even though the renderer was probably power-saved and
    # would 500 on the first command.  Cheap (single asyncio.Lock +
    # dict scan); runs every 60 s.
    global _cast_reaper_task
    _cast_reaper_task = asyncio.create_task(_cast_session_reaper())

    # Stations (internet radio): seed favorites and warm the Radio Browser
    # country metadata in the background — startup never blocks on the
    # public directory being reachable; a cache on disk serves either way.
    from soniqboom.core import radiodir as _radiodir
    asyncio.create_task(_radiodir.ensure_ready())

    # Arm the deadlock watchdog last — once everything else has loaded,
    # so a slow startup step (e.g. HVSC reindex) doesn't trip the
    # watchdog while it isn't even servicing requests yet.
    from soniqboom.core import deadlock_watchdog
    deadlock_watchdog.start()

    # All startup work done — mark ready so the menubar can switch its
    # title from "starting…" to running and any status-file pollers can
    # stop spinning.  Message includes the track count so the final
    # stderr line answers "what was loaded?" at a glance.
    from soniqboom.core.startup_status import mark_ready as _ss_mark_ready
    _ss_mark_ready(f"{get_store().track_count():,} tracks at http://{settings.host}:{settings.port}")



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
            # Synchronous probe before serving traffic — if a share is dead
            # at boot, mark it ``unavailable`` immediately so callers see an
            # accurate status instead of waiting BASE_INTERVAL=60 s for the
            # background monitor's first probe.
            try:
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, source.is_dir, "/"),
                    timeout=10.0,
                )
            except (asyncio.TimeoutError, Exception):
                ok = False
            if ok:
                register_source(scan_root, source)
                from soniqboom.core.data import upsert_scan_dir
                await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")
                log.info("Connected to share %s (%s)", share_id, scan_root)
            else:
                try:
                    source.close()
                except Exception:
                    pass
                from soniqboom.core.data import upsert_scan_dir
                await upsert_scan_dir(
                    scan_root, network_share_id=share_id, status="unavailable",
                )
                log.warning(
                    "Share %s: root not accessible at startup — marked "
                    "unavailable; health monitor will retry",
                    share_id,
                )
        except Exception as exc:
            log.warning("Share %s connect failed: %s", share_id, exc)
            try:
                from soniqboom.core.data import upsert_scan_dir
                await upsert_scan_dir(
                    scan_root, network_share_id=share_id, status="unavailable",
                )
            except Exception:
                pass


async def _start_dlna_server():
    """Start the DLNA Media Server SSDP responder + announcer.

    Failures are logged but non-fatal — the rest of the app keeps
    working even if SSDP can't bind (port 1900 collision with another
    DLNA stack on the same host is the realistic failure mode)."""
    try:
        import socket as _socket
        # Best LAN IP — same heuristic the cast session uses.  Bind
        # a UDP socket to a public address (no packets sent) and read
        # back the kernel's chosen source IP.
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 53))
                lan_ip = s.getsockname()[0]
        except OSError:
            lan_ip = "127.0.0.1"
        port = int(getattr(settings, "port", 8080) or 8080)
        base_url = f"http://{lan_ip}:{port}"

        # Friendly name — show on DLNA controllers' picker lists.
        # Hostname-derived so multi-machine setups distinguish each
        # SoniqBoom instance.
        try:
            hostname = _socket.gethostname()
        except OSError:
            hostname = "soniqboom"
        friendly = f"SoniqBoom ({hostname})"

        from soniqboom.core.dlna_server import DLNAServer, set_instance
        srv = DLNAServer(base_url=base_url, friendly_name=friendly)
        await srv.start()
        set_instance(srv)
        log.info("DLNA Media Server announcing as '%s' at %s", friendly, base_url)
    except Exception:
        log.exception("DLNA Media Server failed to start (continuing without)")


async def _cast_session_reaper():
    """Close Cast / DLNA / AirPlay sessions idle for >10 min.

    Renderers go to sleep on their own clock — a Sonos auto-pauses,
    a TV goes into standby, a Chromecast disconnects from the cast
    SDK after a few minutes of no traffic.  If we keep their
    SoniqBoom-side ``CastSession`` alive indefinitely, the user's
    next command 502s because the underlying socket has been gone
    for hours.  Reaping idle sessions makes the next /api/cast/play
    cleanly re-handshake instead of inheriting a dead connection.
    """
    from soniqboom.core.cast_session import reap_idle_sessions
    while True:
        try:
            await asyncio.sleep(60)
            reaped = await reap_idle_sessions()
            if reaped:
                log.info("cast: reaped %d idle session(s)", reaped)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("cast reaper iteration failed (continuing)")


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
    """Graceful shutdown — bounded.

    Every step has an explicit timeout and runs sync I/O off the event
    loop (``asyncio.to_thread``) so a slow operation can actually be
    interrupted by ``asyncio.wait_for`` rather than blocking the whole
    handler.  Without these guards a stuck FTP share / disk fsync /
    AOF flock contention used to hang shutdown for ~30 s until
    ``shutdown.sh`` SIGKILL'd us.

    Total worst-case budget:  3 + 2 + 10 + 3 + 3 = 21 s.  Each step logs
    its elapsed time so a future hang can be diagnosed without strace.
    """
    global _aof_writer, _merger_proc
    t_total = time.monotonic()

    async def _step(name: str, coro, timeout: float) -> None:
        t = time.monotonic()
        try:
            await asyncio.wait_for(coro, timeout=timeout)
            log.info("Shutdown step %s: %dms", name, (time.monotonic() - t) * 1000)
        except asyncio.TimeoutError:
            log.warning("Shutdown step %s timed out after %.1fs — continuing",
                        name, timeout)
        except Exception:
            log.exception("Shutdown step %s failed", name)

    # ── Step 0: Cancel the share-health monitor early ─────────────────────
    # The monitor sleeps up to 5 min between probes; cancelling it first
    # means subsequent shutdown steps don't race against an in-flight
    # source.is_dir() probe.  Bounded by a 1 s wait so a probe stuck in
    # native code can't stall shutdown.
    if _cast_reaper_task and not _cast_reaper_task.done():
        _cast_reaper_task.cancel()
        try:
            await asyncio.wait_for(_cast_reaper_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    # Stop the deadlock watchdog so its asyncio.sleep doesn't keep the
    # event loop alive past the shutdown budget.  Idempotent.
    try:
        from soniqboom.core import deadlock_watchdog
        deadlock_watchdog.stop()
    except Exception:
        log.debug("deadlock_watchdog.stop() failed (non-fatal)", exc_info=True)

    # Stop the remote-freshness loops — each share has its own asyncio
    # task that sleeps for the adaptive interval.  Without explicit
    # cancellation those sleeps would extend shutdown to the full poll
    # window (up to 4 h on stable shares).  freshness.stop() also
    # flushes the per-share state to disk so cadence survives restart.
    async def _stop_freshness():
        from soniqboom.core import remote_freshness
        await remote_freshness.stop()
    await _step("remote-freshness", _stop_freshness(), 5.0)

    # Stop the DLNA SSDP server.  ``stop`` sends ssdp:byebye so DLNA
    # controllers evict us from their picker lists immediately rather
    # than waiting for max-age expiry (~30 min).
    async def _stop_dlna():
        from soniqboom.core.dlna_server import get_instance, set_instance
        srv = get_instance()
        if srv is None:
            return
        try:
            await srv.stop()
        finally:
            set_instance(None)
    await _step("dlna-server", _stop_dlna(), 2.0)

    # Close every live Cast / DLNA / AirPlay session so the underlying
    # aiohttp sessions, pychromecast worker threads, and pyatv sessions
    # release their OS-level resources before the interpreter exits.
    # Without this, a graceful shutdown still leaked all of those handles
    # for ~30 s until the kernel reaped them, and the next start-up could
    # briefly see "device busy" responses from the same renderers.
    async def _close_cast_sessions():
        try:
            from soniqboom.core.cast_session import list_sessions, close_session
            sessions = await list_sessions()
            await asyncio.gather(
                *(close_session(s.target.id) for s in sessions),
                return_exceptions=True,
            )
        except Exception:
            log.exception("close_cast_sessions failed")
    await _step("cast-sessions", _close_cast_sessions(), 3.0)

    if _health_task and not _health_task.done():
        _health_task.cancel()
        try:
            await asyncio.wait_for(_health_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    # ── Step 1: WebSocket close (3 s budget total) ────────────────────────
    async def _close_one(ws_obj) -> None:
        try:
            await asyncio.wait_for(ws_obj.close(code=1001), timeout=1.5)
        except Exception:
            pass

    async def _close_all_ws() -> None:
        from soniqboom.api.library import _ws_clients
        if _ws_clients:
            await asyncio.gather(
                *(_close_one(ws) for ws in list(_ws_clients)),
                return_exceptions=True,
            )
        _ws_clients.clear()
        from soniqboom.api.multiroom import _rooms as _mr_rooms
        mr_closers = [
            _close_one(client.ws)
            for room in list(_mr_rooms.values())
            for client in list(room.clients.values())
        ]
        if mr_closers:
            await asyncio.gather(*mr_closers, return_exceptions=True)
        _mr_rooms.clear()

    await _step("websockets", _close_all_ws(), timeout=3.0)

    # ── Step 2: AOF flush + close fd (2 s budget) ─────────────────────────
    # Cancel the periodic flush task ON the loop first (Task.cancel is
    # loop-bound), then do the blocking flush_sync + fd close off the loop
    # so a contended flock can't freeze the async runtime.
    if _aof_writer:
        _aof_writer.cancel_flush_task()
        await _step("aof-flush",
                    asyncio.to_thread(_aof_writer.stop),
                    timeout=2.0)

    # ── Step 3: Snapshot (10 s budget — large libraries justify this) ─────
    # 100 k tracks → ~50 MB JSON; on a slow disk that's still well under
    # 10 s.  On miss we still have the AOF, which is replayed on next
    # startup, so the snapshot being stale by one shutdown is recoverable.
    async def _write_snap() -> None:
        from soniqboom.core.persistence import write_snapshot_sync
        await asyncio.to_thread(write_snapshot_sync, get_data_dir())

    await _step("snapshot", _write_snap(), timeout=10.0)

    # ── Step 4: Merger (3 s budget — interruptible since merger.py fix) ───
    async def _stop_merger() -> None:
        if _merger_proc and _merger_proc.is_alive():
            _merger_proc.terminate()
            await asyncio.to_thread(_merger_proc.join, 3)
            if _merger_proc.is_alive():
                log.warning("Merger ignored SIGTERM — escalating to SIGKILL")
                _merger_proc.kill()

    await _step("merger", _stop_merger(), timeout=3.5)

    # ── Step 5: File sources (1 s budget — force_close is near-instant) ───
    # We only do reads against remote shares, so the protocol-graceful
    # close (FTP QUIT / SMB LOGOFF) buys us nothing but server-side log
    # tidiness — and costs up to 75 s per source when the remote is
    # unreachable.  ``force_close`` drops the TCP socket without the
    # handshake; the remote server times out its session on its own
    # schedule (5–15 min), and we hold no locks worth releasing politely.
    from soniqboom.core.filesource import all_sources

    async def _close_src(src):
        try:
            await asyncio.wait_for(asyncio.to_thread(src.force_close), timeout=0.5)
        except Exception:
            pass

    sources = list(all_sources().values())
    if sources:
        await _step(
            "filesources",
            asyncio.gather(*(_close_src(s) for s in sources), return_exceptions=True),
            timeout=1.0,
        )

    log.info("Shutdown complete in %.0fms", (time.monotonic() - t_total) * 1000)


# ── Frontend static files ─────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    log.info("Serving frontend from %s", FRONTEND_DIR)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="frontend")

    # Service Worker for the offline shell (PERC-6).  Must be served from
    # a top-level path AND carry ``Service-Worker-Allowed: /`` so the
    # default-scope rule doesn't restrict it to ``/assets/`` only.  The
    # client registers it via ``/assets/sw.js`` for backward compat with
    # the static mount; we also expose it at ``/sw.js`` for the proper
    # site-wide scope.
    @app.get("/sw.js", include_in_schema=False)
    @app.get("/assets/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(
            FRONTEND_DIR / "sw.js",
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                # SW updates rely on the browser revalidating the
                # script itself; no-cache forces that check every load.
                "Cache-Control": "no-cache",
            },
        )

    @app.get("/m", include_in_schema=False)
    @app.get("/m/{rest:path}", include_in_schema=False)
    async def mobile_shell(rest: str = ""):
        # Plain serve — deliberately does NOT pin sb_ui=mobile.  Setting a
        # sticky cookie here had a nasty side effect: the service worker
        # precaches ``/m`` (and any link/prefetch hits it too), so a desktop
        # browser would silently acquire sb_ui=mobile just from the SW
        # install and then bounce ``/`` to ``/m`` forever.  Deliberate
        # mobile pinning still happens via the explicit ``?ui=mobile``
        # override on ``/``; real phones get ``/m`` via UA sniffing each
        # visit (cheap), so losing the optimization costs nothing.
        return FileResponse(FRONTEND_DIR / "mobile.html")

    # ``/multiroom`` HTML shell is gated on the multiroom service toggle —
    # disable in Settings → Services to hide the page entirely (404 from
    # the SPA fallback below).  The API router is gated above.
    if _svc_on("multiroom"):
        @app.get("/multiroom", include_in_schema=False)
        @app.get("/multiroom/{rest:path}", include_in_schema=False)
        async def multiroom_shell(rest: str = ""):
            return FileResponse(FRONTEND_DIR / "multiroom.html")

    # Mobile user agents that should get the mobile shell on / by default.
    # The user can override either way: append ``?ui=desktop`` (or ``?ui=mobile``)
    # to set a sticky cookie, or visit /m / / directly after the cookie is set.
    #
    # Regex tightened to catch Vivaldi / Opera GX / Firefox Focus mobile builds
    # which only carry "Mobile" (not "Mobile Safari" literally).  iPads under
    # iPadOS 13+ spoof as "Macintosh" by default — they fall through to
    # desktop, which is what we want (more screen real estate available).
    _MOBILE_UA_RE = __import__("re").compile(
        r"\bAndroid\b|\biPhone\b|\biPod\b|\bMobile\b|Opera Mini|IEMobile|FxiOS|EdgiOS",
        __import__("re").IGNORECASE,
    )

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        from fastapi.responses import RedirectResponse
        # Explicit override via ``?ui=``: pin the choice in a cookie so the
        # user isn't bounced again on the next visit.
        override = request.query_params.get("ui", "").lower()
        if override in ("desktop", "mobile"):
            resp = (RedirectResponse("/m", status_code=302)
                    if override == "mobile" else
                    FileResponse(FRONTEND_DIR / "index.html"))
            resp.set_cookie("sb_ui", override, max_age=60 * 60 * 24 * 365,
                            samesite="lax")
            return resp
        # Sticky cookie wins over UA sniffing.
        pinned = request.cookies.get("sb_ui", "")
        if pinned == "mobile":
            return RedirectResponse("/m", status_code=302)
        if pinned == "desktop":
            return FileResponse(FRONTEND_DIR / "index.html")
        # First visit: route phones to the mobile shell.  iPads + desktops
        # still get the rich UI because the regex deliberately excludes them.
        ua = request.headers.get("user-agent", "")
        if _MOBILE_UA_RE.search(ua):
            return RedirectResponse("/m", status_code=302)
        return FileResponse(FRONTEND_DIR / "index.html")

    # SPA fallback MUST be registered after explicit "/" — Starlette's
    # ``{full_path:path}`` converter matches empty string, so ``/`` would
    # otherwise resolve to the fallback first and the root handler above
    # (mobile redirect + cookie pinning) would never run.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
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
    # NB: previously this said "ready" — misleading because uvicorn
    # hasn't started yet and the lifespan handler still has to load the
    # snapshot and build indexes (3–15s on a 268K-track library).  The
    # ``▸ Loading library snapshot…`` chips below come from the startup
    # tracker (core/startup_status.py); the final ``▸ Ready (Ns total)``
    # line is the real ready signal.
    print(f"  SoniqBoom {__version__}  ·  starting…")
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

    # ── Optional access services ────────────────────────────────────────
    # Show which extras are mounted on this server and how clients reach
    # them.  A disabled service is printed with a strike-through marker
    # so the operator immediately sees what's off — toggle in Settings →
    # Services or via ``soniqboom services enable|disable <name>``.
    from soniqboom.config import (
        SERVICE_NAMES,
        SERVICE_LABELS,
        is_service_enabled,
    )
    base = f"http://{'localhost' if localhost_only else (ips[0] if ips else 'localhost')}:{port}"
    svc_endpoints = {
        "subsonic":  f"{base}/rest/ping.view",
        "multiroom": f"{base}/multiroom",
        "cast":      f"{base}/api/cast/targets",
    }
    print()
    print(f"  Services:")
    for name in SERVICE_NAMES:
        label = SERVICE_LABELS.get(name, name)
        on = is_service_enabled(name)
        mark = "✓" if on else "✗"
        url = svc_endpoints.get(name, "")
        line = f"  {' ':<10} {mark}  {label:<24} {url if on else '(disabled)'}"
        print(line)

    data_dir = get_data_dir()
    print()
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

    # ── fetch-ffmpeg ─────────────────────────────────────────────────────────
    ff_p = sub.add_parser(
        "fetch-ffmpeg",
        help="Download / refresh the bundled static ffmpeg with full DSD support",
    )
    ff_p.add_argument("--force", action="store_true",
                      help="Re-download even if a bundled ffmpeg is already current")
    ff_p.add_argument("--print", dest="print_only", action="store_true",
                      help="Print the install path and exit (no download)")
    ff_p.add_argument("--check", action="store_true",
                      help="Exit 0 if bundled ffmpeg is current, 1 otherwise")

    # ── services list|enable|disable ─────────────────────────────────────────
    svc_p = sub.add_parser(
        "services",
        help="List / toggle optional access services (subsonic, multiroom, cast)",
    )
    svc_sub = svc_p.add_subparsers(dest="svc_cmd")
    svc_sub.add_parser("list",   help="Show each service and whether it's enabled")
    svc_en  = svc_sub.add_parser("enable",  help="Enable a service")
    svc_en.add_argument("name", help="Service name (subsonic | multiroom | cast)")
    svc_dis = svc_sub.add_parser("disable", help="Disable a service")
    svc_dis.add_argument("name", help="Service name (subsonic | multiroom | cast)")

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

    if args.cmd == "services":
        from soniqboom.config import (
            SERVICE_NAMES,
            SERVICE_LABELS,
            is_service_enabled,
            set_service_enabled,
        )
        if args.svc_cmd in (None, "list"):
            print("Service           Status   Description")
            print("─" * 60)
            for n in SERVICE_NAMES:
                state = "enabled" if is_service_enabled(n) else "disabled"
                print(f"  {n:<15} {state:<8} {SERVICE_LABELS.get(n, '')}")
            print("\nUse `soniqboom services enable|disable <name>` to toggle.")
            return
        if args.svc_cmd in ("enable", "disable"):
            name = args.name
            if name not in SERVICE_NAMES:
                print(f"error: unknown service {name!r}.  Known: {', '.join(SERVICE_NAMES)}",
                      file=sys.stderr)
                sys.exit(2)
            set_service_enabled(name, args.svc_cmd == "enable")
            state = "enabled" if args.svc_cmd == "enable" else "disabled"
            print(f"{SERVICE_LABELS.get(name, name)} → {state}.")
            print("Restart the SoniqBoom server (or use the admin restart) for the change to take effect.")
            return

    if args.cmd == "fetch-ffmpeg":
        # Locate the standalone helper script.  In a source checkout it sits
        # at ``<repo>/scripts/fetch_ffmpeg.py``; in a wheel install we ship
        # it alongside the package under ``soniqboom/scripts/``.
        import subprocess as _sub
        from pathlib import Path as _P
        candidates = [
            _P(__file__).resolve().parent.parent / "scripts" / "fetch_ffmpeg.py",
            _P(__file__).resolve().parent / "scripts" / "fetch_ffmpeg.py",
        ]
        helper = next((c for c in candidates if c.is_file()), None)
        if helper is None:
            print("error: fetch_ffmpeg.py helper not found in the install tree.",
                  file=sys.stderr)
            sys.exit(3)
        cmd = [sys.executable, str(helper)]
        if args.force:      cmd.append("--force")
        if args.print_only: cmd.append("--print")
        if args.check:      cmd.append("--check")
        sys.exit(_sub.call(cmd))

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
