# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SoniqBoom — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipResponder, IdentityResponder
from starlette.requests import cookie_parser as _cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from soniqboom import __version__
from soniqboom.api import art, artist, cast, fstree, library, multiroom, playlist, search, smart, stream, subsonic, tracks, users as users_api
from soniqboom.config import settings, get_data_dir, _CONF_PATH
from soniqboom.core import deadlock_watchdog as _watchdog
from soniqboom.core import forksafe
from soniqboom.core import users as _core_users
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


def _find_docs_dir() -> Path:
    """Locate the bundled HTML user manual (``docs/manual``).

    Checked in order: the repo checkout (``<repo>/docs/manual``), next to the
    executable in a packaged build, and next to the frontend.  Falls back to
    the repo path so callers get a predictable ``.exists()`` false when the
    manual wasn't shipped."""
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "docs" / "manual",                 # dev checkout
        Path(sys.executable).resolve().parent / "docs" / "manual",   # bundled
        FRONTEND_DIR.parent / "docs" / "manual",         # alongside frontend
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


DOCS_DIR = _find_docs_dir()

# ── Interpreter tuning ────────────────────────────────────────────────────────
# The event loop shares the GIL with every ``asyncio.to_thread`` worker.  With
# CPython's default 5 ms switch interval a pure-Python job in a worker (the
# shuffle-order sort, a big JSON build) holds the loop's median tick at ~15 ms and
# opens 100–600 ms holes under concurrency; at 0.5 ms the loop's p95 lag measured
# 1.8 ms (synthetic 263 K-track store).  Cost: ~+4–6 % on CPU-bound work that runs
# in several threads at once; blocking pipe reads got ~8× more responsive.  Streams,
# art and list chunks all ride this loop — latency there beats raw throughput.
sys.setswitchinterval(0.0005)

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
    # The API reference (Swagger UI + its OpenAPI document) sits under /api,
    # behind the sign-in like every other /api path.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

# Paths whose responses are already compressed (or carry byte-range audio/
# image bytes that browsers stream incrementally).  Re-gzipping audio adds
# CPU + latency and breaks Range without giving up any meaningful bytes.
_GZIP_SKIP_PREFIXES = (
    "/api/stream",
    # Subsonic is mounted at /rest (not /api/rest): audio, art and the radio
    # relay must never be gzipped — it breaks Range and stalls live streams.
    "/rest/stream",
    "/rest/download",
    "/rest/getCoverArt",
    "/rest/getAvatar",
    "/rest/getTranscodeStream",
    "/rest/radioStream",
    "/rest/hls",
    "/api/art",
    # Live radio relay — an infinite audio stream; gzip buffering would
    # stall it and the bytes are already compressed audio.
    "/api/stations/relay",
    # The anonymous Cast / DLNA / AirPlay byte server (/cast/{token}/{file}):
    # ranged audio a renderer seeks in — never gzipped.  (The /api/cast JSON
    # control API is a different prefix and stays compressible.)
    "/cast/",
)


# A whole (not streamed) response body at least this big is gzipped in a worker
# thread: zlib releases the GIL while it deflates, so the event loop serves
# other requests meanwhile instead of stalling (a 4.7 MB JSON listing is ~25 ms
# of deflate at level 6).  Smaller bodies — and every chunk of a streamed one —
# are compressed inline as before; the hop (~0.1 ms) isn't worth it there.
_GZIP_OFFLOAD_MIN_BYTES = 64 * 1024


class _OffloopGZipResponder(GZipResponder):
    """Starlette's ``GZipResponder`` with the one-shot compression of a big
    body moved off the event loop (``_GZIP_OFFLOAD_MIN_BYTES``) — the same
    headers (``Vary: Accept-Encoding``, ``Content-Encoding``, the new
    ``Content-Length``), and a body that already carries ``Content-Encoding``
    (or is an event stream) still passes untouched."""

    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int) -> None:
        super().__init__(app, minimum_size, compresslevel=compresslevel)
        self._level = compresslevel

    async def send_with_compression(self, message) -> None:
        body = message.get("body", b"") if message["type"] == "http.response.body" else b""
        if (len(body) >= _GZIP_OFFLOAD_MIN_BYTES and not self.started
                and not message.get("more_body", False)
                and not (self.content_encoding_set or self.content_type_is_excluded)):
            self.started = True
            gz = await asyncio.to_thread(gzip.compress, body, self._level)
            headers = MutableHeaders(raw=self.initial_message["headers"])
            headers.add_vary_header("Accept-Encoding")
            headers["Content-Encoding"] = self.content_encoding
            headers["Content-Length"] = str(len(gz))
            message["body"] = gz
            await self.send(self.initial_message)
            await self.send(message)
            return
        await super().send_with_compression(message)


class _SelectiveGZipMiddleware:
    """GZip middleware that skips compression for audio / art endpoints.
    The stock GZipMiddleware applies to every response over ``minimum_size``,
    which clobbered ranged audio streams and re-encoded already-compressed
    JPEG/PNG thumbnails for no benefit.  A big one-shot body is compressed
    off the event loop (``_OffloopGZipResponder``).
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1000) -> None:
        # compresslevel 6 (not Starlette's default 9): the large /api/tracks +
        # /api/search JSON pages (~1.8 MB) are gzipped per request, and level
        # 9 is markedly slower for a few percent smaller output.  Level 6
        # roughly halves that per-page CPU cost while keeping the ~5-8x
        # compression these highly-repetitive JSON payloads get.
        self._raw = app
        self._minimum_size = minimum_size
        self._level = 6

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._raw(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = Headers(scope=scope)
        if any(path.startswith(p) for p in _GZIP_SKIP_PREFIXES) or "range" in headers:
            # Bypass GZip entirely — preserves Range headers + avoids
            # double-encoding already-compressed media.  A ranged request
            # anywhere (a static asset, the manual) gets its bytes as asked:
            # a gzipped 206 would carry a Content-Range of the uncompressed
            # bytes over a body that isn't them.
            await self._raw(scope, receive, send)
            return
        # The choice Starlette's GZipMiddleware makes.
        if "gzip" in headers.get("Accept-Encoding", ""):
            responder = _OffloopGZipResponder(self._raw, self._minimum_size, self._level)
        else:
            responder = IdentityResponder(self._raw, self._minimum_size)
        await responder(scope, receive, send)


app.add_middleware(_SelectiveGZipMiddleware, minimum_size=1000)


# ── Origins: CSRF / WebSocket origin check, CORS, framing ──────────────────
# A page of another origin — another port of the same host counts: cookies
# are per host, not per port, and SameSite=Lax treats it as the same site —
# must not drive the signed-in API.  A request that changes state (any method
# but GET / HEAD / OPTIONS) under /api, and every WebSocket handshake, is
# refused (403) when the browser says another origin started it: an
# ``Origin`` that is neither this server (its Host, or a reverse proxy's
# X-Forwarded-Host) nor one of ``SONIQBOOM_CORS_ORIGINS``, unless
# ``Sec-Fetch-Site: same-origin``; without an Origin, ``Sec-Fetch-Site:
# cross-site / same-site`` or a foreign ``Referer``.  A request with none of
# these (curl, soniqboom-setadm, Subsonic apps) is not a browser's and passes.
# ``SONIQBOOM_CORS_ORIGINS`` (comma-separated origins, e.g.
# ``https://music.example.com``) may call the API cross-origin, signed in
# (``_ApiCORSMiddleware``); ``SONIQBOOM_FRAME_ANCESTORS`` (space- or
# comma-separated origins) may embed the web UI in a frame — by default only
# the server's own pages may (``frame-ancestors 'self'``).

def _parse_origins(raw: str) -> frozenset:
    return frozenset(o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip())


_CORS_ORIGINS = _parse_origins(os.environ.get("SONIQBOOM_CORS_ORIGINS", ""))
_FRAME_ANCESTORS = " ".join(
    o for o in os.environ.get("SONIQBOOM_FRAME_ANCESTORS", "").replace(",", " ").split() if o)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _header(scope: Scope, name: bytes) -> "str | None":
    """The first ``name`` header of the request, or None."""
    for k, v in scope.get("headers") or ():
        if k == name:
            return v.decode("latin-1")
    return None


def _netloc_key(netloc: str) -> str:
    """``host[:port]`` lower-cased, a default port (80 / 443) dropped."""
    n = netloc.strip().lower()
    for suffix in (":80", ":443"):
        if n.endswith(suffix):
            return n[:-len(suffix)]
    return n


def _origin_allowed(scope: Scope, origin: str) -> bool:
    """``origin`` (``scheme://host[:port]``) is this server — the request's
    Host, or the X-Forwarded-Host a reverse proxy sets (a page can't make a
    browser send either for another server) — or a ``SONIQBOOM_CORS_ORIGINS``
    entry."""
    o = origin.strip().rstrip("/").lower()
    if o in _CORS_ORIGINS:
        return True
    scheme, sep, netloc = o.partition("://")
    if not sep or scheme not in ("http", "https") or not netloc or "/" in netloc:
        return False
    own = set()
    host = _header(scope, b"host")
    if host:
        own.add(_netloc_key(host))
    fwd = _header(scope, b"x-forwarded-host")
    if fwd:
        own.add(_netloc_key(fwd.split(",")[0]))
    return _netloc_key(netloc) in own


def _cross_origin_refused(scope: Scope) -> bool:
    """A browser request another origin started (see the section comment)."""
    site = (_header(scope, b"sec-fetch-site") or "").strip().lower()
    origin = _header(scope, b"origin")
    if origin is not None:
        return not (_origin_allowed(scope, origin) or site == "same-origin")
    if site in ("cross-site", "same-site"):
        return True
    referer = _header(scope, b"referer")
    if referer:
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(referer)
        except ValueError:
            return True
        return not _origin_allowed(scope, f"{parts.scheme}://{parts.netloc}")
    return False


class _ApiCORSMiddleware:
    """CORS for the origins in ``SONIQBOOM_CORS_ORIGINS`` — signed in (with
    ``Access-Control-Allow-Credentials``), every path but the Subsonic API
    (``/rest`` has its own open policy, ``_SubsonicCORSMiddleware``).  Their
    preflights are answered here, before the session gate (a preflight
    carries no cookie).  Any other origin gets no CORS headers: the browser
    keeps its responses from the page."""

    _ALLOW_METHODS = b"GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
    _EXPOSE = b"Content-Length, Content-Range, Accept-Ranges, Content-Type, X-SB-Lib-Gen"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _CORS_ORIGINS:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        origin = _header(scope, b"origin")
        if (origin is None or path == "/rest" or path.startswith("/rest/")
                or origin.strip().rstrip("/").lower() not in _CORS_ORIGINS):
            await self.app(scope, receive, send)
            return
        allow = origin.encode("latin-1")
        req_method = _header(scope, b"access-control-request-method")
        if scope.get("method") == "OPTIONS" and req_method is not None:
            req_headers = _header(scope, b"access-control-request-headers") or ""
            allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_, ")
            hdrs = [(b"access-control-allow-origin", allow),
                    (b"access-control-allow-credentials", b"true"),
                    (b"access-control-allow-methods", self._ALLOW_METHODS),
                    (b"access-control-max-age", b"600"),
                    (b"vary", b"Origin"),
                    (b"content-length", b"0")]
            if req_headers and len(req_headers) <= 1024 and set(req_headers) <= allowed_chars:
                hdrs.append((b"access-control-allow-headers", req_headers.encode("latin-1")))
            await send({"type": "http.response.start", "status": 204, "headers": hdrs})
            await send({"type": "http.response.body", "body": b""})
            return

        async def _send(message) -> None:
            if message["type"] == "http.response.start":
                hdrs = [(k, v) for k, v in message.get("headers") or ()
                        if k.lower() not in (b"access-control-allow-origin",
                                             b"access-control-allow-credentials",
                                             b"access-control-expose-headers")]
                hdrs += [(b"access-control-allow-origin", allow),
                         (b"access-control-allow-credentials", b"true"),
                         (b"access-control-expose-headers", self._EXPOSE),
                         (b"vary", b"Origin")]
                message = {**message, "headers": hdrs}
            await send(message)

        await self.app(scope, receive, _send)


# ── Request-body ceilings ─────────────────────────────────────────────────
# A body over its path's ceiling is refused with 413 — up front when its
# Content-Length says so, else the moment the bytes read cross it.  Sign-in
# requests are small; the library import and soundfont upload carry whole
# files; a client-rendered SID WAV is capped by its own handler (64 MB).
_BODY_MAX_DEFAULT = 16 * 1024 * 1024
_BODY_MAX_AUTH = 64 * 1024
_BODY_MAX_UPLOAD = 4 * 1024 * 1024 * 1024
_BODY_MAX_SID_AUDIO = 80 * 1024 * 1024
_UPLOAD_PATHS = frozenset({"/api/admin/import", "/api/admin/soundfonts/upload"})


def _body_limit(path: str) -> int:
    if path.startswith("/api/auth/"):
        return _BODY_MAX_AUTH
    if path in _UPLOAD_PATHS:
        return _BODY_MAX_UPLOAD
    if path.startswith("/api/tracks/") and path.endswith("/sid-audio"):
        return _BODY_MAX_SID_AUDIO
    return _BODY_MAX_DEFAULT


def _limited_receive(receive: Receive, limit: int) -> Receive:
    total = 0

    async def _receive():
        nonlocal total
        message = await receive()
        if message.get("type") == "http.request":
            total += len(message.get("body") or b"")
            if total > limit:
                raise HTTPException(413, "Request body too large.")
        return message
    return _receive


def _body_too_large(scope: Scope, limit: int) -> bool:
    clen = _header(scope, b"content-length")
    try:
        return clen is not None and int(clen) > limit
    except ValueError:
        return False


# ── Security headers (pages, API, frontend files) ─────────────────────────
# Not on the media byte servers other origins' players read (/rest, /cast,
# /dlna): nosniff there could make a browser refuse a mislabelled stream.
_CSP = ("frame-ancestors 'self'" + (f" {_FRAME_ANCESTORS}" if _FRAME_ANCESTORS else "")
        + "; base-uri 'self'; object-src 'none'").encode("latin-1")
_SECURITY_HEADERS = tuple(h for h in (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    None if _FRAME_ANCESTORS else (b"x-frame-options", b"SAMEORIGIN"),
    (b"content-security-policy", _CSP),
) if h is not None)
_NO_SECURITY_HEADER_PREFIXES = ("/rest/", "/cast/", "/dlna/")


def _add_security_headers(message: dict, path: str) -> dict:
    if path == "/rest" or path.startswith(_NO_SECURITY_HEADER_PREFIXES):
        return message
    raw = list(message.get("headers") or ())
    have = {k.lower() for k, _v in raw}
    added = [(k, v) for k, v in _SECURITY_HEADERS if k not in have]
    if not added:
        return message
    return {**message, "headers": raw + added}


def _cookie_refresh(message: dict, scope: Scope, cookie: "str | None") -> dict:
    """Send the session cookie again when its sliding expiry has moved on
    (``UserStore.session_cookie_due``: about once an hour per session) — the
    browser's copy then expires 7 days after the last use, not after the
    sign-in.  Never over a response that sets the cookie itself (sign-in,
    sign-out)."""
    if not cookie or message.get("status", 200) == 401:
        return message
    raw = list(message.get("headers") or ())
    if any(k.lower() == b"set-cookie" and v.lstrip().startswith(b"sb_session=") for k, v in raw):
        return message
    try:
        store = _core_users.get_user_store()
        if not store.session_cookie_due(cookie):
            return message
    except Exception:                                       # noqa: BLE001
        return message
    secure = users_api.request_is_tls(scope.get("scheme", "http"),
                                      _header(scope, b"x-forwarded-proto"))
    raw.append((b"set-cookie", users_api.session_cookie_header(cookie, secure)))
    return {**message, "headers": raw}


# ── The request gate: auth, Cache-Control, deadlock watchdog ───────────────
# One pure-ASGI layer for what were three ``@app.middleware("http")`` functions
# (``BaseHTTPMiddleware``): each of those ran the rest of the request in a
# task of its own behind a memory stream — three hops on every request
# (/api/health in-process: 688 µs with them, 253 µs with this layer).  Here the scope is read
# directly and the response's ``send`` wrapped; a streamed body (audio, SSE,
# chunked) goes through chunk by chunk; a websocket passes through once its
# Origin is judged.  Stack, outermost first: ``_SubsonicCORSMiddleware``,
# ``_ApiCORSMiddleware`` (a listed origin's preflight is answered before the
# session gate), this gate, GZip.  In the gate: the watchdog registration
# outermost, then the response headers (Cache-Control, security headers, the
# session cookie's refresh), then the cross-origin check, the body ceiling and
# the session gate.

# Public /api/* allowlist — endpoints the unauthenticated UI needs.
_PUBLIC_API_PATHS = frozenset({
    "/api/health",
    "/api/ui-config",
    "/api/plugins",
    "/api/auth/status",
    "/api/auth/reload",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/me",
    "/api/auth/logout",          # idempotent on a missing session
})
_API_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


def _request_path(scope: Scope) -> str:
    """The path every rule below judges: the scope's own — the very path the
    router matches.  (The middlewares before this one judged
    ``request.url.path``, which Starlette builds from the Host HEADER and the
    path and parses again: ``Host: x/api/health#`` turned any request into
    "/api/health" for the allowlist while the router still served the real
    path — a sign-in bypass for every /api endpoint.)"""
    return scope["path"]


def _session_cookie(scope: Scope) -> "str | None":
    """``request.cookies.get("sb_session")``: every Cookie header parsed,
    the last value winning."""
    value = None
    for k, v in scope.get("headers") or ():
        if k == b"cookie":
            got = _cookie_parser(v.decode("latin-1")).get("sb_session")
            if got is not None:
                value = got
    return value


def _api_auth_refused(scope: Scope, path: str) -> bool:
    """Gate every ``/api/*`` endpoint on a valid session, with a small
    public allowlist (login / register / ping / health / SPA shell).

    Before this gate, individual routers had to remember to add a
    ``Depends(require_user)``.  The QA pen-test found that tracks /
    art / library / fstree / search / smart were all anonymous, which
    defeats the point of the stream-auth gate (a network neighbor could
    enumerate the entire library, listening history, and download cover
    art without logging in).

    On a fresh install with no users yet, the allowlist is wider so the
    bootstrap UI can finish setting up the first admin.  Once any user
    exists, only the explicit public paths slip through.
    """
    # Anything outside /api passes through unconditionally — static SPA
    # assets, websockets (they have their own gate), and /rest/* (Subsonic
    # has its own auth in every handler).
    if not path.startswith("/api/"):
        return False
    if path in _PUBLIC_API_PATHS:
        return False
    # (WebSocket endpoints handle their own auth in-handler: a websocket
    # scope never reaches this gate.  The old "path ends in /ws" exemption
    # also opened any HTTP route whose path parameter ended in "/ws".)
    # Stream + Subsonic stream are auth-gated inside their handlers
    # (they also accept Subsonic-style ?u=&p= which middleware can't
    # easily validate without duplicating that logic), so let them
    # through here and trust the handler.
    if path.startswith(("/api/stream/", "/api/rest/")) or path == "/api/stream":
        return False
    # Default: require a valid session cookie.
    try:
        store = _core_users.get_user_store()
    except Exception:
        # User store not initialised yet (very early boot).  Let through
        # so health/status work.
        return False
    # Pre-bootstrap: no users → allowlist is effectively everything so the
    # operator can finish the first-time setup without auth.
    if not store.has_any():
        return False
    cookie = _session_cookie(scope)
    return not (cookie and store.lookup_session(cookie))


def _add_cache_headers(message: dict, path: str) -> dict:
    """Attach sensible Cache-Control to each response class.

    Rules, in precedence order:
      * If a handler set its own ``Cache-Control`` (e.g. art endpoints with
        ``immutable`` thumbnails, or aggregation endpoints returning 304s),
        respect it — we don't clobber handler intent — except that under
        ``/api`` (signed-in content) ``public`` becomes ``private``: no shared
        cache may keep it.
      * ``/api/stream/*``: ``private`` (the browser may keep the bytes it
        buffered, no shared cache may) so byte-range audio buffering stays
        healthy.
      * Other ``/api/*``: ``no-store`` so admin/library state stays fresh.
      * The frontend's own files (``/assets/*``, ``/manual/*``) and HTML
        shells (``/``, ``/m``, ``/multiroom``): ``no-cache`` — the browser
        may keep a copy but asks again on every use, and an unchanged file
        comes back as a 304 against its ETag / Last-Modified.  Not
        ``immutable``: the JS URLs in the shells carry content hashes
        (``core.shell_html``), but the CSS ``?v=`` tags are set by hand,
        so an edit without a bump must still reach the next load.
      * Everything else (``/rest``, ``/dlna``, ``/cast``, …): ``no-store``.
    """
    raw = message.get("headers") or ()
    # Respect handler-set headers.  This matters for ETag 304s from
    # /api/library/* which choose their own revalidation policy.
    own = [v for k, v in raw if k.lower() == b"cache-control"]
    if own:
        if path.startswith("/api") and any(b"public" in v.lower() for v in own):
            import re as _re
            headers = MutableHeaders(raw=list(raw))
            headers["Cache-Control"] = _re.sub(
                "public", "private", own[0].decode("latin-1"), flags=_re.IGNORECASE)
            return {**message, "headers": headers.raw}
        return message
    headers = MutableHeaders(raw=list(raw))
    if path.startswith("/api/stream"):
        headers["Cache-Control"] = "private"   # media pipeline owns its buffering
        return {**message, "headers": headers.raw}
    if path.startswith("/api"):
        headers["Cache-Control"] = _API_NO_STORE
        headers["Pragma"] = "no-cache"
        headers["Expires"] = "0"
    elif _revalidated_static(path):
        headers["Cache-Control"] = "no-cache"
    else:
        headers["Cache-Control"] = "no-store"
    return {**message, "headers": headers.raw}


# Frontend files and HTML shells (``_add_cache_headers``): revalidated, not
# re-downloaded — the static mounts answer a conditional request with a 304.
_STATIC_SHELLS = frozenset({"/", "/m", "/multiroom", "/manual"})
_STATIC_PREFIXES = ("/assets/", "/manual/", "/m/", "/multiroom/")


def _revalidated_static(path: str) -> bool:
    return path in _STATIC_SHELLS or path.startswith(_STATIC_PREFIXES)


# The source install's restart (``admin._do_restart`` → ``os.execv``): from
# ``begin_exec_restart`` until the exec this process answers every request but
# the frontend's own files with 503 — no sign-in, store write or new stream is
# taken on while the restart quiesces the server, seals the journal and writes
# the sign-ins (and ``/api/health`` stays unhealthy until the new image answers).
_exec_restarting = False


def begin_exec_restart() -> None:
    """The exec restart begins: requests are refused (503) and sign-ins
    closed (``UserStore.close_sign_ins`` — a sign-in already past the gate is
    refused too, so every session issued is in the final sessions write)."""
    global _exec_restarting
    _exec_restarting = True
    try:
        _core_users.get_user_store().close_sign_ins()
    except Exception:                                       # noqa: BLE001
        log.debug("closing sign-ins for the restart failed", exc_info=True)


def _lib_gen_for(scope: Scope, path: str) -> "str | None":
    """The library generation a browse GET is served at (``X-SB-Lib-Gen``) —
    read before its handler runs, so a change while it runs makes the header
    older than the data, never newer."""
    if scope.get("method") != "GET":
        return None
    from soniqboom.api import library as _lib
    if not path.startswith(_lib.LIB_GEN_PATHS):
        return None
    try:
        return _lib.library_generation()
    except Exception:                                       # noqa: BLE001
        return None


_LIB_GEN_HEADER = "X-SB-Lib-Gen"          # = api.library.LIB_GEN_HEADER


async def _restarting_response(scope: Scope, receive: Receive, path: str):
    """The answer to a request while the source install's restart runs:
    503 + ``Retry-After`` — for the Subsonic API (``/rest``) a Subsonic error
    envelope (code 0) in the format the request asked for (``f``, in the
    query or a form-encoded POST body), as Subsonic errors are answered
    (HTTP 200)."""
    if path == "/rest" or path.startswith("/rest/"):
        try:
            from urllib.parse import parse_qs
            from starlette.requests import Request as _Req
            from soniqboom.api import subsonic as _ss
            req = _Req(scope)
            fmt = req.query_params.get("f")
            ctype = req.headers.get("content-type", "")
            if (fmt is None and scope.get("method") == "POST"
                    and ctype.startswith("application/x-www-form-urlencoded")):
                body, more = b"", True
                while more and len(body) < 65536:
                    msg = await receive()
                    body += msg.get("body", b"")
                    more = msg.get("more_body", False)
                fmt = (parse_qs(body.decode("latin-1")).get("f") or [None])[0]
            fmt = (fmt or "xml").lower()
            resp = _ss._maybe_jsonp(req, _ss._err(
                0, "The server is restarting — try again in a moment.", fmt=fmt))
            resp.headers["Retry-After"] = "2"
            return resp
        except Exception:                                   # noqa: BLE001
            log.debug("Subsonic restart envelope failed", exc_info=True)
    return JSONResponse({"detail": "The server is restarting."}, status_code=503,
                        headers={"Retry-After": "2"})


class _RequestGateMiddleware:
    """The session gate (``_api_auth_refused`` → 401), the Cache-Control
    policy (``_add_cache_headers``) and the deadlock-watchdog registration
    for every HTTP request.

    The watchdog: a stale entry (request still in-flight after the
    configured threshold, default 90 s) triggers a full-thread stack dump
    in the log so the operator can identify the lock-holder instead of
    resorting to ``kill -9``.  The entry ends when the response starts —
    when the old middleware's ``call_next`` returned — so a long streamed
    body never counts as a stuck request; stream + WebSocket paths are
    skipped inside :mod:`deadlock_watchdog` anyway."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket" and _cross_origin_refused(scope):
                # A page of another origin (cross-site WebSocket hijacking):
                # refused before the handshake completes (HTTP 403).
                await receive()                                   # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
                return
            if scope["type"] == "websocket" and _exec_restarting:
                # (the exec restart: accepted, then closed "service restart"
                # — a close before the accept reaches the page as 1006 — and
                # the page reconnects to the new image right away)
                await receive()                                   # websocket.connect
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 1012})
                return
            await self.app(scope, receive, send)
            return
        path = _request_path(scope)
        is_api = path.startswith("/api/")
        cookie = _session_cookie(scope) if is_api else None
        client = scope.get("client")
        token = _watchdog.begin_request(
            scope.get("method", ""), path,
            client=f"{client[0]}:{client[1]}" if client else "",
        )
        ended = False
        lib_gen = _lib_gen_for(scope, path)

        async def _send(message) -> None:
            nonlocal ended
            if message["type"] == "http.response.start":
                message = _add_cache_headers(message, path)
                message = _add_security_headers(message, path)
                if cookie:
                    message = _cookie_refresh(message, scope, cookie)
                if lib_gen is not None:
                    headers = MutableHeaders(raw=list(message.get("headers") or ()))
                    headers[_LIB_GEN_HEADER] = lib_gen
                    message = {**message, "headers": headers.raw}
                if not ended:
                    ended = True
                    _watchdog.end_request(token)
            await send(message)

        limit = _body_limit(path)
        try:
            if _exec_restarting and not _revalidated_static(path):
                await (await _restarting_response(scope, receive, path))(scope, receive, _send)
            elif (is_api and scope.get("method", "GET") not in _SAFE_METHODS
                    and _cross_origin_refused(scope)):
                await JSONResponse({"detail": "Cross-origin request refused."},
                                   status_code=403)(scope, receive, _send)
            elif _body_too_large(scope, limit):
                await JSONResponse({"detail": "Request body too large."},
                                   status_code=413)(scope, receive, _send)
            elif _api_auth_refused(scope, path):
                await JSONResponse({"detail": "Sign in to access this endpoint."},
                                   status_code=401)(scope, receive, _send)
            else:
                await self.app(scope, _limited_receive(receive, limit), _send)
        finally:
            if not ended:
                _watchdog.end_request(token)


app.add_middleware(_RequestGateMiddleware)
app.add_middleware(_ApiCORSMiddleware)


class _SubsonicCORSMiddleware:
    """Open CORS for the Subsonic API (``/rest``) only.

    Browser-based Subsonic clients (Airsonic-refix, Feishin web …) run on
    their own origin and call ``/rest/*`` with credentials in the query
    string / headers, so the app-wide localhost-only CORS policy blocked
    them.  For ``/rest`` requests that carry an ``Origin``: a preflight is
    answered here with 204; every other response gets
    ``Access-Control-Allow-Origin: *`` (never ``Allow-Credentials``, so a
    cookie-authenticated response stays unreadable cross-origin) plus the
    headers range-seeking players need to read.  ``/api``, ``/ws`` and the
    SPA keep the localhost-only policy.  Registered LAST, so it is the
    outermost layer.  ``SONIQBOOM_SUBSONIC_CORS=0`` turns it off."""

    _EXPOSE = b"Content-Length, Content-Range, Accept-Ranges, Content-Type"
    _ALLOW_METHODS = b"GET, POST, HEAD, OPTIONS"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.enabled = os.environ.get("SONIQBOOM_SUBSONIC_CORS", "1").strip().lower() \
            not in ("0", "false", "no", "off")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (self.enabled and (path == "/rest" or path.startswith("/rest/"))):
            await self.app(scope, receive, send)
            return
        origin = req_method = req_headers = None
        for k, v in scope.get("headers") or ():
            if k == b"origin":
                origin = v
            elif k == b"access-control-request-method":
                req_method = v
            elif k == b"access-control-request-headers":
                req_headers = v
        if origin is None:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS" and req_method is not None:
            allow_headers = b"*"
            if req_headers and len(req_headers) <= 1024 and all(
                    c in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_, "
                    for c in req_headers):
                allow_headers = req_headers
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", self._ALLOW_METHODS),
                (b"access-control-allow-headers", allow_headers),
                (b"access-control-max-age", b"600"),
                (b"content-length", b"0"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def _send(message) -> None:
            if message["type"] == "http.response.start":
                hdrs = [(k, v) for k, v in message.get("headers") or ()
                        if k.lower() not in (b"access-control-allow-origin",
                                             b"access-control-allow-credentials",
                                             b"access-control-expose-headers")]
                hdrs.append((b"access-control-allow-origin", b"*"))
                hdrs.append((b"access-control-expose-headers", self._EXPOSE))
                message = {**message, "headers": hdrs}
            await send(message)

        await self.app(scope, receive, _send)


app.add_middleware(_SubsonicCORSMiddleware)


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
    @app.api_route("/cast/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
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


# ── Subsonic-aware request-validation handler ──────────────────────────────
# FastAPI raises RequestValidationError *before* a handler runs (during param
# parsing), so subsonic's @_wrap can't catch it — a missing/invalid ``id=``
# would otherwise leak a raw HTTP 422, which Subsonic clients treat as a
# transport error ("server unreachable") rather than a protocol error.  For
# /rest/* we convert it to a Subsonic error-10 envelope (required parameter
# missing) at HTTP 200, honouring the requested ``f=`` format.  Every other
# path keeps FastAPI's default 422 JSON.
from fastapi.exceptions import RequestValidationError as _ReqValErr
from fastapi.exception_handlers import (
    request_validation_exception_handler as _default_val_handler,
)


@app.exception_handler(_ReqValErr)
async def _subsonic_validation_handler(request: Request, exc: _ReqValErr):
    if request.url.path.startswith("/rest/"):
        fmt = (request.query_params.get("f") or "xml").lower()
        try:
            loc = (exc.errors()[0].get("loc") or ())
            pname = loc[-1] if loc else "?"
            msg = f"Required parameter '{pname}' is missing or invalid."
        except Exception:
            msg = "A required parameter is missing or invalid."
        return subsonic._maybe_jsonp(request, subsonic._err(10, msg, fmt=fmt))
    return await _default_val_handler(request, exc)


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


def _signed_in_or_fresh(request: Request) -> bool:
    """The caller is signed in — or no account exists yet (a fresh install:
    everything is open).  ``request`` None: an in-process call."""
    if request is None:
        return True
    try:
        store = _core_users.get_user_store()
    except Exception:                                   # noqa: BLE001
        return False
    if not store.has_any():
        return True
    cookie = request.cookies.get("sb_session")
    return bool(cookie) and store.lookup_session(cookie) is not None


@app.get("/api/health")
async def health(request: Request):
    # Include the current service-toggle state in the health payload so
    # the run.sh / restart.sh banner can render "Services:" without
    # needing to parse the Python log.  Public — no PII, just on/off.
    # ``version``: for a signed-in caller and for the server's own host
    # (run.sh, the menu-bar app); null for anyone else.
    from soniqboom.config import (
        SERVICE_NAMES, SERVICE_LABELS, is_service_enabled,
    )
    # Folder watcher: counts only (no paths — this endpoint is public).
    try:
        from soniqboom.core import watcher as _watcher
        watch = _watcher.summary()
    except Exception:                                   # noqa: BLE001
        watch = None
    show_version = (users_api._is_loopback_request(request)
                    or _signed_in_or_fresh(request))
    return {
        "status":  "ok",
        "version": __version__ if show_version else None,
        "watcher": watch,
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


def _availability_mode() -> str:
    try:
        from soniqboom.core import availability
        return availability.mode()
    except Exception:
        return "mark"


def _catalogue_list_reads() -> bool:
    try:
        from soniqboom.core import modland_catalog
        return modland_catalog.enabled() and modland_catalog.tune_details() == "lists"
    except Exception:                                   # noqa: BLE001
        return False


def _tune_placeholders() -> list:
    try:
        from soniqboom.core import skip_rules
        return skip_rules.placeholder_seconds()
    except Exception:                                   # noqa: BLE001
        return [180.0]


def _skip_short_tunes() -> int:
    try:
        from soniqboom.core import skip_rules
        return skip_rules.short_secs()
    except Exception:                                   # noqa: BLE001
        return 0


@app.get("/api/ui-config")
async def ui_config(request: Request = None):
    """Public UI configuration — no auth required.  Safe subset only: the
    folder aliases and catalogue roots (keyed by library paths) only for a
    signed-in caller (the web UI reads this after its sign-in).

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
    from soniqboom.core import catalogue as _catalogue
    signed_in = _signed_in_or_fresh(request)
    aliases = dict(settings.folder_aliases or {}) if signed_in else {}
    for r in (_catalogue.roots() if signed_in else ()):   # catalogue roots show their label
        aliases.setdefault(r["root"], r["label"])
    return {
        "display_startup_logo": settings.display_startup_logo,
        "expose_local_files": settings.expose_local_files,
        "folder_aliases": aliases,
        "filter_duplicates": await get_config("filter_duplicates", False),
        # Settings → "Tracks on unavailable drives": mark / hide / show.
        "offline_tracks": _availability_mode(),
        # Settings → "Skip short tunes": seconds (0 = off) — core/skip_rules.py.
        "skip_short_tunes": _skip_short_tunes(),
        "tune_placeholders": _tune_placeholders(),
        # External catalogues: their roots, and whether their tunes shown in a
        # list are read for their details (Modland "Read details of tunes not
        # played yet: For tunes shown in lists").
        "catalogue_roots": [r["root"] for r in _catalogue.roots()] if signed_in else [],
        "catalogue_list_reads": _catalogue_list_reads(),
        "placeholder_art_data_uri": f"data:image/jpeg;base64,{placeholder_b64}",
    }


# ── Lifecycle ─────────────────────────────────────────────────────────────────

_aof_writer = None
_merger_proc = None
_run_epoch = 0                  # this server run (scanner.begin_run)
_reap_later_task = None
_health_task = None
_cast_reaper_task = None
# Seconds after startup before the one-time upgrade enrichment is scheduled
# (scanner.schedule_startup_enrichment).
_STARTUP_ENRICHMENT_DELAY_S = 30.0
_STARTUP_RECONCILE_DELAY_S = 180.0
_reconcile_handle: "asyncio.TimerHandle | None" = None


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


def _proc_cmdline(pid: int) -> str:
    """``procinfo.cmdline`` (a process's command line, read without forking)."""
    from soniqboom.core import procinfo
    return procinfo.cmdline(pid)


def _is_our_forkserver_child(pid: int) -> bool:
    """``procinfo.is_our_forkserver_child``."""
    from soniqboom.core import procinfo
    return procinfo.is_our_forkserver_child(pid)


def _orphan_tree_targets(procs: "dict[int, tuple[int, str]]", me: int,
                         own_leftovers: bool = False) -> list[int]:
    """The processes ``_reap_orphaned_forkservers`` kills, from ``{pid: (ppid,
    command line)}``: every SoniqBoom forkserver whose parent is gone (PPID
    1) — its command line (and, forked from it, its workers' and the
    merger's) carries the preload ``['soniqboom']`` and the app's sys_path
    (``scanner.prepare_worker_forkserver``) — and every process below it.  A
    forkserver whose parent lives belongs to that process and is left
    alone, as is everything else."""
    def _ours(cmd: str) -> bool:
        return "multiprocessing.forkserver" in cmd and (
            "SoniqBoom" in cmd or "'soniqboom'" in cmd)

    parents = (1, me) if own_leftovers else (1,)
    roots = [pid for pid, (ppid, cmd) in procs.items()
             if pid != me and ppid in parents and _ours(cmd)]
    children: dict[int, list[int]] = {}
    for pid, (ppid, _cmd) in procs.items():
        children.setdefault(ppid, []).append(pid)
    targets: list[int] = []
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid == me or pid in targets:
            continue
        targets.append(pid)
        stack.extend(children.get(pid, ()))
    return targets


_started_in_this_image = False

# The thread whose server run holds this process's server state (the store,
# the scan pools and their run epoch, the AOF writer, the merger, downloads —
# all process-wide): one run at a time (``_claim_server``).
_server_owner: "threading.Thread | None" = None
_server_owner_lock = threading.Lock()


_LATE_AOF_KEEP_S = 30 * 86400


def _prune_late_aof_files(data_dir: Path) -> None:
    """Delete ``library.aof.late-*`` files older than ``_LATE_AOF_KEEP_S``:
    records a sealed journal set aside at a stop (``AOFWriter.seal``), kept a
    while for inspection, never replayed."""
    cutoff = time.time() - _LATE_AOF_KEEP_S
    try:
        for p in data_dir.glob("library.aof.late-*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _claim_server() -> None:
    """A server run starts in this thread — refused (``RuntimeError``, before
    anything is touched) while another thread's run is still on: a second
    start in the same process (the bundled app's Start clicked again during a
    start-up) would take over the running server's state, and its stop would
    stop that server's scans.  A run whose thread ended without its shutdown
    (a failed start-up) no longer counts."""
    global _server_owner
    me = threading.current_thread()
    with _server_owner_lock:
        owner = _server_owner
        if owner is not None and owner is not me and owner.is_alive():
            raise RuntimeError(f"a SoniqBoom server already runs in this process "
                               f"(thread {owner.name}) — this one is not started")
        _server_owner = me


def _owns_server() -> bool:
    """This thread's server run holds the server state (``_claim_server``)."""
    return _server_owner is threading.current_thread()


def _release_server() -> None:
    global _server_owner
    with _server_owner_lock:
        if _server_owner is threading.current_thread():
            _server_owner = None


# Shutdown steps whose to_thread job writes the library's files: one that
# timed out may still be writing (``_release_thread_pool``).
_WRITING_STEPS = frozenset({"aof-flush", "snapshot", "browse-cache", "remote-freshness", "merger",
                            "aof-seal"})


# The server run's thread pool (``asyncio.to_thread`` / ``run_in_executor``),
# set as the loop's default at start-up so the stop can see its jobs on both
# loops (uvloop keeps its own default executor private).
_thread_pool = None
# How long a stop the process exits after (the bundled app's Quit or a
# signal: its exit kills running threads) lets the pool's running jobs — a tag
# or lyrics write, a playlist save — finish.  Set by the app before that stop.
_POOL_GRACE_S = 5.0
_process_exiting = False


def _counting_pool_class():
    from concurrent.futures import ThreadPoolExecutor

    class _CountingPool(ThreadPoolExecutor):
        """A ``ThreadPoolExecutor`` that knows how many jobs run right now
        (``running_jobs``) — its threads stay alive idle, so counting live
        threads counts idle ones too."""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._running = 0
            self._running_lock = threading.Lock()

        def submit(self, fn, /, *args, **kwargs):
            def _job(*a, **k):
                with self._running_lock:
                    self._running += 1
                try:
                    return fn(*a, **k)
                finally:
                    with self._running_lock:
                        self._running -= 1
            return super().submit(_job, *args, **kwargs)

        def running_jobs(self) -> int:
            with self._running_lock:
                return self._running
    return _CountingPool


def _install_thread_pool() -> None:
    global _thread_pool
    from soniqboom.core import procinfo
    # The library default (CPUs + 4, max 32) counted the host's CPUs (GitHub #21).
    _thread_pool = _counting_pool_class()(max_workers=min(32, procinfo.usable_cpus() + 4),
                                          thread_name_prefix="sb-pool")
    asyncio.get_running_loop().set_default_executor(_thread_pool)


async def _release_thread_pool(results: dict) -> bool:
    """At the end of the stop: the loop gets a fresh thread pool FIRST (a
    task still ending can go on using ``asyncio.to_thread``), then the run's
    pool is shut down (queued jobs cancelled) without waiting for its running
    jobs — the loop's close (``asyncio.Runner``, uvloop alike) would otherwise
    wait up to 300 s for a job stuck on a share that stopped answering (a
    folder listing, a stat, the scan's walk), and the bundled app waits for
    this server's thread before it starts the server again.  Such a job goes
    on until the share answers; its result is dropped.  When the process
    exits after this stop (``_process_exiting``: the app's Quit or a signal —
    its exit kills running threads) the running jobs get ``_POOL_GRACE_S`` to
    finish (a tag write).  (A command-line server's exit waits for pool
    threads by itself.)  Not released when one of the stop's own writing
    steps timed out or failed (``_WRITING_STEPS``): its job may still write,
    and the close waits for it as before.  (A merge of this run still running
    can't overlap the next run's load either: ``persistence.library_files_lock``.)
    True when released."""
    # The playback I/O threads (``core.playback_io``: reads of the files
    # being played, never a library write) go in any case: queued jobs
    # dropped, a read stuck on a share not waited for; a play after this
    # starts a fresh pool.
    try:
        from soniqboom.core import playback_io
        playback_io.release()
    except Exception:                                   # noqa: BLE001
        log.debug("Shutdown: playback pool release failed", exc_info=True)
    late = sorted(k for k, v in results.items() if v in ("TIMEOUT", "FAIL") and k in _WRITING_STEPS)
    if late:
        log.warning("Shutdown: waiting for %s to finish before the server's thread ends", ", ".join(late))
        return False
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(thread_name_prefix="sb-after-stop"))
    pool = _thread_pool
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)

        def _busy() -> int:
            running = getattr(pool, "running_jobs", None)
            if running is not None:
                return running()
            return sum(t.is_alive() for t in list(getattr(pool, "_threads", ())))
        if _process_exiting:
            deadline = time.monotonic() + _POOL_GRACE_S
            while _busy() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        busy = _busy()
        if busy:
            log.warning("Shutdown: %d thread-pool job(s) still running (a share that doesn't "
                        "answer?) — not waited for", busy)
    return True


# The children a process image had right before the source install's restart
# replaced it (``os.execv`` keeps the pid, so they stay this process's
# children): ``[[pid, command line], …]`` as JSON, for the image it starts.
_EXEC_LEFTOVERS_ENV = "SONIQBOOM_EXEC_LEFTOVERS"
# Taken (and removed from the environment, so no process this one starts
# inherits it) once, as this image loads.
_exec_leftovers_raw: "str | None" = os.environ.pop(_EXEC_LEFTOVERS_ENV, None)


def _own_children() -> "dict[int, tuple[str, str]]":
    """This process's children: ``{pid: (state, command line)}``
    (``procinfo.process_table``: ``/proc`` on Linux, else a fork-free ``ps``)."""
    from soniqboom.core import procinfo
    me = os.getpid()
    return {pid: (st, cmd) for pid, (ppid, st, cmd) in procinfo.process_table().items()
            if ppid == me}


def note_exec_leftovers() -> int:
    """Right before ``os.execv``: record this process's children (a stopped
    renderer not yet waited for, the forkserver, the resource tracker) for
    the exec'd image, which ends and waits for them
    (``_reap_exec_leftovers``) — nothing else would: they'd stay zombies.
    Returns how many."""
    import json
    rows = _own_children()
    os.environ[_EXEC_LEFTOVERS_ENV] = json.dumps([[pid, cmd] for pid, (_st, cmd) in rows.items()])
    return len(rows)


def _waitpid_quiet(pid: int, flags: int = 0) -> bool:
    """Reap child ``pid``; True when it was reaped (or is no child of ours)."""
    try:
        got, _status = os.waitpid(pid, flags)
    except ChildProcessError:
        return True
    except OSError:
        return True
    return got == pid


def _reap_exec_leftovers(raw: "str | None" = None) -> "tuple[int, int]":
    """At the first start of an image the source install's restart exec'd:
    end the children the previous image recorded (``note_exec_leftovers``,
    read as this image loaded — or ``raw``) that are still this process's
    children with the same command line, and wait for them and for those
    that already ended (zombies).  A pid that is no longer our child, or now
    runs something else, is left alone.  Once per image.  Returns
    ``(reaped, signalled)``."""
    import json
    global _exec_leftovers_raw
    if raw is None:
        raw, _exec_leftovers_raw = _exec_leftovers_raw, None
    if not raw:
        return 0, 0
    try:
        recorded = {int(pid): str(cmd) for pid, cmd in json.loads(raw)}
    except (ValueError, TypeError):
        return 0, 0
    now = _own_children()
    zombies = [pid for pid in recorded if pid in now and "Z" in now[pid][0]]
    live = [pid for pid in recorded
            if pid in now and "Z" not in now[pid][0] and now[pid][1] == recorded[pid]]
    signalled = 0
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled += 1
        except OSError:
            pass
    reaped = sum(1 for pid in zombies if _waitpid_quiet(pid, os.WNOHANG))
    pending = set(live)
    deadline = time.monotonic() + 1.0
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            if _waitpid_quiet(pid, os.WNOHANG):
                pending.discard(pid)
                reaped += 1
        if pending:
            time.sleep(0.05)
    for pid in list(pending):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + 1.0
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            if _waitpid_quiet(pid, os.WNOHANG):
                pending.discard(pid)
                reaped += 1
        if pending:
            time.sleep(0.05)
    return reaped, signalled


def _reap_at_start() -> int:
    """The orphan reaper (``_reap_orphaned_forkservers``) at a start.  It
    runs at every start — it doesn't fork (``forksafe``) — and the first
    start of this process image also takes the SoniqBoom forkservers that
    are this process's own children: a restart by ``os.execv`` keeps the
    pid, so the image before it left them here, parent still "alive".
    Later starts in the same image (the bundled app's Stop/Start/Restart)
    leave this process's own forkserver alone — it is kept and reused."""
    global _started_in_this_image
    first = not _started_in_this_image
    _started_in_this_image = True
    return _reap_orphaned_forkservers(own_leftovers=first)


def _reap_orphaned_forkservers(own_leftovers: bool = False) -> int:
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
    matching-cmdline-only.  ``ps`` runs through ``forksafe`` (posix_spawn —
    this server must not fork once it used the network).  With
    ``own_leftovers``, this process's own SoniqBoom forkservers are taken
    too (``_reap_at_start``).
    """
    from soniqboom.core import forksafe
    me = os.getpid()
    try:
        out = forksafe.run(["ps", "-A", "-o", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return 0
    procs: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])

    targets = _orphan_tree_targets(procs, me, own_leftovers=own_leftovers)
    reaped = 0
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
            reaped += 1
            log.info("reap: SIGTERM orphaned forkserver-tree pid=%d", pid)
        except OSError:
            pass
    # Give them a beat to exit cleanly, then SIGKILL the survivors.
    if reaped:
        import time as _t
        _t.sleep(0.5)
        for pid in targets:
            try:
                os.kill(pid, 0)  # check alive
                os.kill(pid, signal.SIGKILL)
                log.info("reap: SIGKILL stubborn forkserver-tree pid=%d", pid)
            except OSError:
                pass  # already dead — good
    return reaped


# Strong refs to detached fire-and-forget startup tasks — without a live reference
# asyncio may garbage-collect a bare create_task() mid-flight (CPython docs).
_BG_TASKS: "set" = set()

# How often a server with no account loaded (recovery, fresh install) looks
# for a users.json put back on disk (``UserStore.check_restored``).
_USERS_RESTORED_POLL_SEC = 2.0


async def _watch_for_restored_accounts() -> None:
    """While no user account is loaded, pick up a users.json restored on disk
    (``UserStore.check_restored``: stat both files; read only when they
    changed; never moves a file aside).  ``POST /api/auth/reload`` without
    the CLI's token is refused in that state, so this is how a restored file
    goes live without a restart."""
    from soniqboom.core.users import get_user_store
    while True:
        await asyncio.sleep(_USERS_RESTORED_POLL_SEC)
        try:
            store = get_user_store()
            if store.count() == 0:
                await asyncio.to_thread(store.check_restored)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            log.debug("restored-accounts check failed", exc_info=True)


# Seconds after startup the Subsonic catalogue prewarm starts — past the
# startup phases, so it never competes with them.
_SUBSONIC_PREWARM_DELAY_SEC = 5.0


def _prewarm_subsonic_catalogue() -> None:
    """One background, time-sliced build of the Subsonic catalogue (the same
    single rebuild task a request would start — a request arriving mid-build
    joins it), then its artist index (``subsonic.prewarm_catalogue``).
    Skipped for an empty library or when a client already triggered the
    build.  Runs on the event loop (store access)."""
    try:
        # The store as Subsonic clients see it (catalogue tunes hidden from
        # them — ``subsonic.get_store``): the catalogue cache is keyed on it.
        t = subsonic.prewarm_catalogue(subsonic.get_store())
        if t is not None:
            _BG_TASKS.add(t)
            t.add_done_callback(_BG_TASKS.discard)
    except Exception:
        log.debug("Subsonic catalogue prewarm failed", exc_info=True)


@app.on_event("startup")
async def startup():
    global _aof_writer, _merger_proc

    _claim_server()                     # one server run per process, first of all
    _install_thread_pool()
    data_dir = get_data_dir()
    _setup_logging(data_dir)
    # Attach the access-log filter + set default verbosity dials so ordinary
    # traffic (expected fallback-404s etc.) doesn't read as faults.  The
    # persisted values are re-applied once the store's config is loaded below.
    from soniqboom.core import log_control
    log_control.install()
    log.info("SoniqBoom %s starting on %s:%s", __version__, settings.host, settings.port)
    _install_sighup_handler()

    from soniqboom.core import forksafe as _fs_start
    _fs_start.open_spawns()
    # Offline mode (SONIQBOOM_OFFLINE / conf ``offline``): no request to an
    # internet host — the clients check ``netpolicy``; this is the safety net.
    from soniqboom.core import netpolicy as _netpolicy
    if _netpolicy.offline():
        _netpolicy.install_guard()
        log.info("Offline mode: on — no requests to internet hosts")
    # The children the image before an in-place restart left (stopped
    # renderers not yet waited for, its forkserver, its resource tracker).
    try:
        n_reaped, n_sig = await asyncio.to_thread(_reap_exec_leftovers)
        if n_reaped or n_sig:
            log.info("Restart: ended %d and reaped %d child process(es) of the previous image",
                     n_sig, n_reaped)
    except Exception:                                   # noqa: BLE001
        log.debug("reaping the previous image's children failed", exc_info=True)
    # Render temp folders of server images that are gone (killed, or the image
    # an in-place restart replaced) — only ``core.render_tmp``'s own folders.
    try:
        from soniqboom.core import render_tmp as _rt
        n_killed = await asyncio.to_thread(_rt.kill_orphan_renderers)
        if n_killed:
            log.info("Ended %d renderer(s) left running by stopped server runs", n_killed)
        n_dirs, n_files, n_bytes = await asyncio.to_thread(_rt.sweep_orphans)
        if n_dirs:
            log.info("Removed %d render temp folder(s) of stopped server runs: %d file(s), %.1f MB",
                     n_dirs, n_files, n_bytes / 1e6)
        n_stray, b_stray = await asyncio.to_thread(_rt.stray_temp_wavs)
        if n_stray:
            log.info("%d temp WAV file(s) (%.1f MB) of unknown origin in %s (tmp*.wav) — "
                     "not removed", n_stray, b_stray / 1e6, __import__("tempfile").gettempdir())
    except Exception:                                   # noqa: BLE001
        log.debug("render temp sweep failed", exc_info=True)

    # Reap any forkserver-tree leftovers from a previous instance that
    # was SIGKILL'd (or whose SIGTERM handler timed out).  Must run
    # BEFORE the first ProcessPoolExecutor() in scanner.py creates our
    # OWN forkserver, otherwise we'd risk reaping fresh children.
    reaped = _reap_at_start()
    if reaped:
        log.info("reaped %d orphaned forkserver process(es) from prior session", reaped)
    # A new server run: the scan state a previous run in this process left is
    # reset, and (macOS) the forkserver the scan pools fork from is ready —
    # started before the server uses the network (scanner._process_pool).
    global _run_epoch, _reap_later_task
    from soniqboom.core.scanner import begin_run
    _run_epoch = begin_run()
    # … and the library generation / aggregate cache of the web lists (a page
    # still connected to the previous run in this process re-reads).
    from soniqboom.api.library import new_server_run
    new_server_run()

    # Once more a little later: the instance an in-app Restart replaced may
    # still have been ending at this start (its forkserver's parent alive).
    async def _reap_later() -> None:
        await asyncio.sleep(30)
        n = await asyncio.to_thread(_reap_orphaned_forkservers)
        if n:
            log.info("reaped %d orphaned forkserver process(es) of the replaced instance", n)
    _reap_later_task = asyncio.create_task(_reap_later())
    # A TOSEC / Redump download a restart interrupted — removed before any
    # request can start a new one.
    try:
        from soniqboom.core import game_titles as _gt
        _gt.clean_leftovers()
    except Exception:                                       # noqa: BLE001
        log.debug("game-title download leftovers not removed", exc_info=True)

    # Begin emitting structured phase markers (stderr + status file).
    # The misleading "ready" banner from cli() already printed before
    # uvicorn called us, so the first thing the user sees here is the
    # tracker's opening line — confirming the slow work is starting and
    # not just hung after the banner.
    from soniqboom.core.startup_status import init as _ss_init, set_phase as _ss_phase
    _ss_init(data_dir)

    # Load snapshot + replay AOF → populate in-memory store + rebuild indexes
    _ss_phase("loading_library", "Loading library snapshot")
    from soniqboom.core.persistence import claim_library, init_persistence
    # This run becomes THE server of the data directory: an instance still
    # stopping (the app's Restart) finishes writing the library first.
    claim_library(data_dir)
    # The bundled app's previous run in this process: its journal ends before
    # this run loads (sealed by its stop — again here, idempotent, should that
    # stop not have got so far: what it still held is written now, before the
    # load, not at the app's quit after this run's records), and this run's
    # writes before its own writer starts below are not that run's records.
    if _aof_writer is not None:
        try:
            _aof_writer.seal()
        except Exception:
            log.warning("The previous run's journal could not be closed", exc_info=True)
    from soniqboom.core.store import get_store as _get_store
    _get_store()._aof_append = None
    init_persistence(data_dir)

    # The journal (library.aof) is attached right after the load, before
    # anything that may write the store starts (the folder watcher's
    # rescans, remote freshness polls, the share health monitor, startup
    # maintenance): a write with no journal attached lives in memory only.
    from soniqboom.core.aof import AOFWriter
    _prune_late_aof_files(data_dir)
    _aof_writer = AOFWriter(data_dir / "library.aof", flush_interval=settings.aof_flush_interval)
    _get_store()._aof_append = _aof_writer.append
    await _aof_writer.start_auto_flush()

    # Duplicate groups follow every duplicate-relevant write from here on
    # (tag edits, duration backfills, scans local and remote).
    try:
        from soniqboom.core.scanner import install_dup_regroup
        install_dup_regroup()
    except Exception:
        log.debug("duplicate re-group hook failed", exc_info=True)

    # The Modland catalogue's root is known before anything that walks or
    # polls remote roots starts (no network, no store write).
    try:
        from soniqboom.core import modland_catalog
        modland_catalog.register_at_startup()
    except Exception:
        log.warning("Modland catalogue registration failed", exc_info=True)
    # … and the online collections' (HVSC on 6581.nl …).
    try:
        from soniqboom.core import online_catalog
        online_catalog.register_at_startup()
    except Exception:
        log.warning("Online collection registration failed", exc_info=True)

    # FTP listing times are UTC from this build on: convert the ones an
    # earlier build stored as host-local time, once — after the load and the
    # journal (it writes through the store), with the catalogue roots known
    # (their tracks are skipped), before any FTP share is listed.
    try:
        from soniqboom.core import ftp_mtime_migration
        ftp_mtime_migration.run(_get_store())
    except Exception:
        log.warning("Converting stored FTP listing times failed (FTP files may "
                    "be read again once)", exc_info=True)

    # Now that config is loaded, apply the persisted log-verbosity dials
    # (app level + access-log mode).  See core/log_control.py.
    try:
        from soniqboom.core.store import get_store
        applied = log_control.apply_from_config(get_store().get_config)
        log.info("Log verbosity: app=%s access=%s", applied["log_level"], applied["access_log"])
    except Exception:
        log.debug("log verbosity apply-from-config failed", exc_info=True)

    # Reclaim on-disk zip-extracts / AdLib .adlib companion dirs whose track is
    # no longer in the store (dir-aware).  Fire-and-forget — it walks the extract
    # dir + does store lookups, so never block the splash on it.
    try:
        from soniqboom.api.stream import reap_orphan_zip_extracts
        asyncio.create_task(reap_orphan_zip_extracts(), name="reap_zip_extracts")
    except Exception:
        log.debug("zip-extract reap scheduling failed", exc_info=True)

    # Full renderer health check a few seconds after boot: exec-probe every
    # plain-binary renderer and log a one-line summary, WARNing on any that are
    # present-but-broken (e.g. a from-source zxtune123/sidplayfp orphaned by a
    # Homebrew library upgrade — dyld "Symbol not found"/"Library not loaded" —
    # which passes a file-exists check but 501s at play time).  Surfaces it once,
    # loudly, so the operator learns before a listener hits a dead format.
    # Fire-and-forget with a strong ref (asyncio may GC a bare task); the short
    # delay keeps the exec probes off the boot-critical path.
    async def _probe_renderer_health():
        try:
            await asyncio.sleep(5)
            from soniqboom.api.admin import validate_renderers
            status = await validate_renderers()
            broken = {n: v for n, v in status.items() if v["status"] == "broken"}
            missing = sorted(n for n, v in status.items() if v["status"] == "missing")
            ok = sum(1 for v in status.values() if v["status"] == "ok")
            log.info("renderer health: %d ok, %d broken, %d missing (of %d binary renderers)",
                     ok, len(broken), len(missing), len(status))
            for name, v in broken.items():
                log.warning(
                    "renderer %s is installed at %s but FAILS TO LOAD — a shared "
                    "library was upgraded out from under it; reinstall or rebuild it "
                    "to fix (its formats are disabled until then).", name, v["path"])
            if missing:
                log.info("renderers not installed (their formats are unavailable): %s",
                         ", ".join(missing))
        except Exception:
            log.debug("renderer health probe failed", exc_info=True)
        # Tracks a renderer refused (core/unplayable.py) are tried again once
        # their renderer or SoniqBoom changed: now, and every hour.
        try:
            from soniqboom.core import unplayable as _unplayable
            await _unplayable.sweep_loop()
        except Exception:
            log.debug("can't-play mark sweep stopped", exc_info=True)
    try:
        _t = asyncio.create_task(_probe_renderer_health(), name="renderer_health_probe")
        _BG_TASKS.add(_t); _t.add_done_callback(_BG_TASKS.discard)
    except Exception:
        log.debug("renderer health probe scheduling failed", exc_info=True)

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

    # Initialise user store (users.json + sessions.json).  Safe even on fresh
    # installs — ``has_any()`` returns False until the first
    # ``soniqboom-setadm`` run (and stays True in recovery: an unreadable
    # users.json never opens the server, see ``UserStore._load``).
    _ss_phase("loading_users", "Loading user accounts")
    from soniqboom.core.users import init_user_store
    init_user_store(data_dir)
    _t = asyncio.create_task(_watch_for_restored_accounts(),
                             name="users_restored_watch")
    _BG_TASKS.add(_t)
    _t.add_done_callback(_BG_TASKS.discard)

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
                r = forksafe.run([binary, "-hide_banner", "-formats"],
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
                r = forksafe.run([binary, "-hide_banner", "-encoders"],
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
                    r = forksafe.run([binary, "-version"], capture_output=True,
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
        proc = await forksafe.spawn(
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
        from soniqboom.core.filesource import is_remote_path
        local_roots = [
            d["path"] for d in dirs
            if not is_remote_path(str(d.get("path", "")))
        ]
        if watcher.is_supported():
            # Started even with no local roots yet: a root added later is
            # armed by ``watcher.add_root``, which needs a running observer.
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

    _ss_phase("persistence", "Starting the merger")
    # (the AOF writer was started right after the load, above)
    from soniqboom.core.store import get_store

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

    try:
        from soniqboom.config import seed_default_soundfont
        if (seeded := seed_default_soundfont()) is not None:
            log.info("SoundFonts: added the default SoundFont %s", seeded.name)
    except Exception as exc:
        log.warning("Default SoundFont not added: %s", exc)

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

    # Library milestone — persistence has loaded and indexes are rebuilt, but
    # the optional services (network shares, DLNA, stations, watchdog) haven't
    # started yet; the authoritative "ready" line with the bind address + full
    # module health is logged at the very end of startup (see mark_ready).
    # NOTE: run.sh greps this line's "<n> tracks loaded" for its banner count.
    log.info("Library loaded — %d tracks loaded", get_store().track_count())

    # The loaded library is ~1M long-lived GC-tracked objects; move them out
    # of the cyclic GC's generations once, so a full collection — triggered by
    # any allocation-heavy request (a Subsonic catalogue build, a big JSON
    # page) — no longer walks them on the event loop.  See the helper.
    try:
        from soniqboom.core.store import freeze_long_lived_heap
        freeze_long_lived_heap("the startup load")
    except Exception:
        log.debug("heap freeze after startup load failed", exc_info=True)

    _ss_phase("network_shares", "Connecting network shares")
    await _init_network_shares()
    _start_share_health_monitor()

    # External sources: the Modland catalogue (heals / arms its daily index
    # refresh; never touches the network at startup).
    try:
        from soniqboom.core import modland_catalog
        await modland_catalog.startup()
    except Exception:
        log.warning("Modland catalogue startup failed", exc_info=True)
    try:
        from soniqboom.core import online_catalog
        await online_catalog.startup()
    except Exception:
        log.warning("Online collections startup failed", exc_info=True)

    # Which tracks can play right now (Settings → "Tracks on unavailable
    # drives"): follow scan-root status changes, re-probe local roots, and
    # tell the web clients when that changes.
    try:
        from soniqboom.core import availability
        from soniqboom.core.store import get_store as _avail_store
        availability.install(_avail_store())
    except Exception:
        log.warning("Availability watcher failed to start", exc_info=True)

    # One-time enrichment an upgrade needs (a newer Modland apply, the
    # header-game album backfill): schedule the coalescing scene runner —
    # it only marks work pending and waits out any scan.  Deferred so the
    # Modland join / re-extracts don't compete with the first page loads
    # right after boot.  Best-effort; a later scan re-triggers it anyway.
    try:
        from soniqboom.core.scanner import schedule_startup_enrichment
        asyncio.get_running_loop().call_later(_STARTUP_ENRICHMENT_DELAY_S,
                                              schedule_startup_enrichment)
    except Exception:
        log.debug("startup enrichment scheduling failed", exc_info=True)

    # Changes made while the server was stopped: the watcher only sees what
    # happens while it runs.  One incremental rescan of the local folders,
    # deferred past the first page loads (Settings → "Rescan local folders
    # after startup").
    try:
        from soniqboom.core.scanner import schedule_startup_reconcile
        global _reconcile_handle
        _reconcile_handle = asyncio.get_running_loop().call_later(
            _STARTUP_RECONCILE_DELAY_S, schedule_startup_reconcile)
    except Exception:
        log.debug("startup reconcile scheduling failed", exc_info=True)

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

    # Stations (internet radio): seed favorites (no network — the Radio
    # Browser country list is fetched when the world browser first opens).
    from soniqboom.core import radiodir as _radiodir
    asyncio.create_task(_radiodir.ensure_ready())

    # SSRF-validating egress proxy for the radio relay (best-effort; the
    # relay degrades to direct connections if it can't bind).
    from soniqboom.core import ssrf_proxy as _ssrf_proxy
    await _ssrf_proxy.start()

    # (The online lyrics providers are ranked by responsiveness on the first
    # lyrics lookup, in the background — ``tracks._resolve_online_lyrics``.)

    # Arm the deadlock watchdog last — once everything else has loaded,
    # so a slow startup step (e.g. HVSC reindex) doesn't trip the
    # watchdog while it isn't even servicing requests yet.
    from soniqboom.core import deadlock_watchdog
    deadlock_watchdog.start()

    # Background index-integrity sweep: periodically (default 30 min, and only
    # when the library has changed) rebuild the derived indexes via the atomic
    # shadow-swap, which DETECTS and AUTO-HEALS any incremental-maintenance
    # drift and keeps GET /admin/stats' ``index_ok`` honest.  The boot rebuild
    # above is authoritative, so indexes start healthy; this catches drift that
    # creeps in between restarts.  Disable with SONIQBOOM_INDEX_SWEEP_SECONDS<=0.
    from soniqboom.core import index_health
    index_health.start()

    # All startup work done — mark ready so the menubar can switch its
    # title from "starting…" to running and any status-file pollers can
    # stop spinning.  Message includes the track count so the final
    # stderr line answers "what was loaded?" at a glance.
    from soniqboom.core.startup_status import mark_ready as _ss_mark_ready, get_status as _ss_status
    _ss_mark_ready(f"{get_store().track_count():,} tracks at http://{settings.host}:{settings.port}")

    # Subsonic: build the catalogue once in the background a few seconds
    # after startup, so the first client request after a restart is served
    # warm instead of waiting out the whole build (~0.6 s at 263k tracks).
    if _svc_on("subsonic"):
        asyncio.get_running_loop().call_later(_SUBSONIC_PREWARM_DELAY_SEC,
                                              _prewarm_subsonic_catalogue)

    # Consolidated startup-health line — one greppable record carrying the bind
    # address and the up/down state of every optional service, so an operator
    # (or a log-scraping monitor) can confirm what actually came up without
    # piecing it together from the phase-by-phase lines above.
    from soniqboom.config import SERVICE_NAMES as _SVC_NAMES
    _svc: list[str] = []
    for _n in _SVC_NAMES:
        if not _svc_on(_n):
            _svc.append(f"{_n}=off")
        elif _n == "dlna_server":
            # Enabled but the SSDP socket may have failed to bind — report the
            # real state, not just the toggle.
            try:
                from soniqboom.core.dlna_server import get_instance as _dlna_inst
                _bound = _dlna_inst() is not None
            except Exception:
                _bound = False
            _svc.append(f"{_n}=" + ("announcing" if _bound else "on(not-bound)"))
        else:
            _svc.append(f"{_n}=on")
    try:
        from soniqboom.core import index_health as _ih
        _idx = "ok" if _ih.snapshot().get("index_ok", True) else "DRIFT"
    except Exception:
        _idx = "unknown"
    try:
        _startup_s = _ss_status().get("elapsed_ms", 0) / 1000.0
    except Exception:
        _startup_s = 0.0
    log.info(
        "SoniqBoom ready — listening on %s:%s · library=%s tracks index=%s · services: %s · startup %.1fs",
        settings.host, settings.port, f"{get_store().track_count():,}", _idx,
        "  ".join(_svc), _startup_s,
    )



# Startup / reconnect budget for network shares.  Shares are grouped by the
# server they live on: ONE quick TCP connect answers for all of a server's
# shares (an unreachable host costs this, once — not 3 connect attempts with
# back-off per share in turn), servers are probed concurrently, and the shares
# of a server that answers connect concurrently, each bounded by the probe
# timeout.
_SHARE_REACH_TIMEOUT_S = 3.0
_SHARE_PROBE_TIMEOUT_S = 10.0
# Shares of one server connect at most this many at a time: each FTP share's
# first probe is a fresh login, and six at once (plus the pool's warm-up) trip
# a per-IP connection cap ("421 Too many connections") on small NAS servers.
_SHARE_CONNECT_PER_HOST = 2


def _tcp_unreachable(host: str, port: int, timeout: float) -> str | None:
    """None when a TCP connect to ``host:port`` succeeds, else why it failed
    (a malformed host name is "unreachable" too — never an exception)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _behind_proxy(proto: str, host: str) -> bool:
    """An http(s) share reached through a proxy (``HTTPS_PROXY`` & co., which
    the WebDAV client honours): a direct TCP check says nothing about it."""
    if proto not in ("http", "https"):
        return False
    import urllib.request
    try:
        return proto in urllib.request.getproxies() and not urllib.request.proxy_bypass(host)
    except Exception:
        return False


def _auto_connect_groups(shares: dict) -> dict[tuple, list[tuple[str, dict, str]]]:
    """Configured auto-connect shares grouped by ``(protocol, host, port)``:
    ``{endpoint: [(share_id, share, scan_root), …]}``."""
    from soniqboom.core.filesource import scan_root_for_share, share_endpoint
    groups: dict[tuple, list[tuple[str, dict, str]]] = {}
    for share_id, share in (shares or {}).items():
        if not isinstance(share, dict) or not share.get("auto_connect", True):
            continue
        try:
            scan_root = scan_root_for_share(share)
            endpoint = share_endpoint(share)
        except (KeyError, ValueError) as exc:
            log.warning("Share %s: unusable configuration (%s) — skipped", share_id, exc)
            continue
        groups.setdefault(endpoint, []).append((share_id, share, scan_root))
    return groups


async def _connect_share(share_id: str, share: dict, scan_root: str, *,
                         recovering: bool = False) -> bool:
    """Create, probe and register one share; publish its status (a retry by
    the health monitor writes only a change: the share is unavailable already)."""
    from soniqboom.core.credentials import decrypt
    from soniqboom.core.data import upsert_scan_dir
    from soniqboom.core.filesource import (
        create_source, is_auth_failure, note_share_auth_failure, register_source,
        reset_share_backoff, share_auth_key, share_retry_wait,
    )

    loop = asyncio.get_running_loop()
    source = None
    reason = ""
    refused = False
    key = share_auth_key(share)
    wait = share_retry_wait(key)
    if wait > 0:
        # The server refused these credentials (for this share or a sibling
        # logging in as the same user): no login until the back-off is over.
        if not recovering:
            try:
                await upsert_scan_dir(scan_root, network_share_id=share_id,
                                      status="unavailable")
            except Exception:
                pass
            log.warning("Share %s: not connected — the server refused its "
                        "credentials; next try in %.0f min (or press Reconnect)",
                        share_id, wait / 60)
        return False
    try:
        password = decrypt(share.get("password_enc", "")) or ""
        source = create_source(share, password=password)
        # Probe before serving traffic — a share that's dead now is marked
        # ``unavailable`` at once instead of failing its first plays.
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(None, source.is_dir, "/"),
                timeout=_SHARE_PROBE_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, Exception):
            ok = False
            reason = "no answer"
        if not ok:
            reason = getattr(source, "last_error", None) or reason or "root not accessible"
            refused = bool(getattr(source, "last_error_auth", False))
    except Exception as exc:
        ok = False
        reason = f"{type(exc).__name__}: {exc}"
        refused = is_auth_failure(exc)
    if ok and recovering:
        # Removed (or reconnected by hand) while we probed: leave it alone.
        from soniqboom.config import load_local_conf
        from soniqboom.core.filesource import get_source
        if (share_id not in (load_local_conf().get("network_shares") or {})
                or get_source(scan_root) is not None):
            try:
                source.close()
            except Exception:
                pass
            return get_source(scan_root) is not None
    if ok:
        reset_share_backoff(key)
        register_source(scan_root, source)
        await upsert_scan_dir(scan_root, network_share_id=share_id, status="ok")
        log.info("%s share %s (%s)", "Reconnected" if recovering else "Connected to",
                 share_id, scan_root)
        return True
    if source is not None:
        # Safe for a share on a server other shares use: closing drops only
        # this source's claim (an SMB connection is deleted by its last user).
        try:
            source.close()
        except Exception:
            pass
    if refused:
        wait = note_share_auth_failure(key, scan_root)
        log.warning("Share %s: the server refused the credentials (%s) — not "
                    "retrying automatically for %.0f min; fix them or press "
                    "Reconnect", share_id, reason, wait / 60)
    if not recovering:
        try:
            await upsert_scan_dir(scan_root, network_share_id=share_id,
                                  status="unavailable")
        except Exception:
            pass
        log.warning("Share %s: not accessible at startup (%s) — marked "
                    "unavailable; the health monitor retries it", share_id, reason)
    return False


async def _connect_share_group(endpoint: tuple, members: list, *,
                               recovering: bool = False) -> int:
    """Connect every share of one server: one reachability check for all of
    them, then the shares — SMB ones in turn, the others at most
    ``_SHARE_CONNECT_PER_HOST`` at a time, one share per set of credentials
    first (a refused login then skips its siblings instead of repeating the
    refusal for each).  Returns how many connected."""
    from soniqboom.core.data import upsert_scan_dir
    from soniqboom.core.filesource import share_auth_key
    proto, host, port = endpoint
    loop = asyncio.get_running_loop()
    why = None if _behind_proxy(proto, host) else await loop.run_in_executor(
        None, _tcp_unreachable, host, port, _SHARE_REACH_TIMEOUT_S)
    if why is not None:
        if not recovering:                  # a retry: unavailable already
            for share_id, _share, scan_root in members:
                try:
                    await upsert_scan_dir(scan_root, network_share_id=share_id,
                                          status="unavailable")
                except Exception:
                    pass
            log.warning(
                "%s server %s:%s unreachable at startup (%s) — %d share(s) "
                "marked unavailable; the health monitor retries them",
                proto.upper(), host, port, why, len(members))
        return 0
    if proto == "smb":
        # One SMB session per server, registered by the first share — the
        # SMB client's session registry isn't safe to fill concurrently.
        results = [await _connect_share(share_id, share, scan_root, recovering=recovering)
                   for share_id, share, scan_root in members]
    else:
        gate = asyncio.Semaphore(_SHARE_CONNECT_PER_HOST)

        async def _one(share_id: str, share: dict, scan_root: str) -> bool:
            async with gate:
                return await _connect_share(share_id, share, scan_root,
                                            recovering=recovering)
        by_key: dict[str, list] = {}
        for m in members:
            by_key.setdefault(share_auth_key(m[1]), []).append(m)

        async def _chain(ms: list) -> list:
            # The first share of these credentials alone, then its siblings
            # (each set of credentials on its own — no wait for another's).
            first = await _one(*ms[0])
            return [first, *await asyncio.gather(*[_one(*m) for m in ms[1:]])]
        results = [r for rs in await asyncio.gather(*[_chain(ms) for ms in by_key.values()])
                   for r in rs]
    return sum(1 for r in results if r)


async def _init_network_shares():
    """Connect to configured shares that have auto_connect enabled."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.remote_cache import init_cache

    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    if not shares:
        return

    max_mb = int(conf.get("remote_cache_max_mb", 2048))
    cache_root = get_data_dir() / "cache" / "remote"
    init_cache(cache_root, max_mb)

    groups = _auto_connect_groups(shares)
    if groups:
        await asyncio.gather(*[
            _connect_share_group(endpoint, members)
            for endpoint, members in groups.items()
        ])


async def _reconnect_pending_shares() -> bool:
    """Health-monitor pass for configured auto-connect shares that have NO
    registered source (down at startup, or a failed connect): try them again,
    one reachability check per server.  Returns True while any stays down."""
    from soniqboom.config import load_local_conf
    from soniqboom.core.filesource import get_source, share_auth_key, share_retry_wait

    groups = _auto_connect_groups(load_local_conf().get("network_shares", {}))
    pending = {ep: [m for m in members if get_source(m[2]) is None]
               for ep, members in groups.items()}
    pending = {ep: m for ep, m in pending.items() if m}
    if not pending:
        return False
    # Shares whose server refused their credentials wait out their back-off
    # (``note_share_auth_failure``) — they stay down meanwhile.
    due = {ep: [m for m in members if share_retry_wait(share_auth_key(m[1])) <= 0]
           for ep, members in pending.items()}
    due = {ep: m for ep, m in due.items() if m}
    waiting = sum(len(m) for m in pending.values()) - sum(len(m) for m in due.values())
    if not due:
        return True
    connected = await asyncio.gather(*[
        _connect_share_group(ep, members, recovering=True)
        for ep, members in due.items()
    ])
    return waiting > 0 or sum(connected) < sum(len(m) for m in due.values())


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


def _busy_serving(source) -> bool:
    """Is ``source`` moving file data right now (``serving_transfers``)?"""
    try:
        return bool(source.serving_transfers())
    except Exception:                                       # noqa: BLE001
        return False


async def _share_health_monitor():
    """Probe network shares periodically; flip status up and down as they come and go.

    Behaviour:
      * A share that's been healthy must fail **twice in a row** before we
        flip it to ``unavailable`` — absorbs single-probe blips.
      * Once unavailable, we keep probing (via the FileSource's own
        connect-retry logic) so recovery is automatic when the network
        comes back — no user action required.  That includes shares that
        never connected (their server was down at startup): each pass
        tries them again, one reachability check per server.
      * Probe interval is adaptive: 60 s while everything is healthy, up
        to 5 min while any share is down (so a dead host isn't hammered),
        resetting to 60 s as soon as everything is green again.
      * A probe the server answers by REFUSING the credentials marks the
        share unavailable at once and arms the credentials' back-off
        (``filesource.note_share_auth_failure``: 5 min doubling to 6 h):
        neither the probe nor the reconnect of unconnected shares logs in
        with them again until it is over or the user presses Reconnect.
      * Only log + upsert the DB when the status actually *changes*, so a
        long outage doesn't spam the log.
      * A probe that times out while the share's transfers are moving data
        (``FileSource.serving_transfers`` — a scan holding every pooled FTP
        connection) is no miss: the share is busy, not gone.  FTP probes go
        on the ``browse`` lane, ahead of queued scan downloads.
    """
    from soniqboom.core.data import upsert_scan_dir
    from soniqboom.core.filesource import (
        all_sources, note_share_auth_failure, reset_share_backoff, share_retry_wait,
        source_auth_key,
    )
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
    failed_passes = 0          # consecutive passes that raised

    async def _probe_one(scan_root: str, source, loop) -> bool:
        """Probe one share and publish a status change.  True = it is down."""
        akey = source_auth_key(source)
        if share_retry_wait(akey) > 0:
            # The server refused this share's credentials: no probe (a
            # WebDAV probe is a login) until its back-off is over.
            return True
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(None, source.is_dir, "/"),
                timeout=PROBE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # A probe that waited out its timeout behind transfers the share
            # is serving (every pooled connection busy, data arriving) says
            # "busy", not "gone": no miss, no change.
            if _busy_serving(source):
                log.debug("Share %s: probe timed out behind its transfers — busy, "
                          "not counted", scan_root)
                return last_status.get(scan_root, "ok") != "ok"
            ok = False
        except Exception:
            ok = False

        prev = last_status.get(scan_root, "ok")
        if ok and prev == "ok" and _stored_unavailable(scan_root):
            # Marked down by someone else (a refused sign-in, the folder
            # availability probe while the source was missing) — and it
            # answers: say so.  Catalogue roots keep their own offline
            # hold (their source answers from the index, never the net).
            prev = "unavailable"

        if not ok and akey and getattr(source, "last_error_auth", False):
            wait = note_share_auth_failure(akey, scan_root)
            log.warning("Share %s: the server refused the credentials — not "
                        "probing it for %.0f min; fix them or press Reconnect",
                        scan_root, wait / 60)
            fail_streak[scan_root] = fail_streak.get(scan_root, 0) + 1
            if prev == "ok":
                await _set_share_status(scan_root, "unavailable")
                last_status[scan_root] = "unavailable"
            return True

        if ok:
            reset_share_backoff(akey)
            fail_streak[scan_root] = 0
            if prev != "ok":
                log.info("Share %s is back online", scan_root)
                await _set_share_status(scan_root, "ok")
                last_status[scan_root] = "ok"
            return False

        fail_streak[scan_root] = fail_streak.get(scan_root, 0) + 1
        # First miss after being healthy: try an explicit reconnect —
        # the source object may just have a stale socket.  Success here
        # keeps the share "ok" without flipping state.
        if prev == "ok" and fail_streak[scan_root] == 1:
            try:
                recovered = await loop.run_in_executor(None, source.reconnect)
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
                    return True
        # Still down — flip state only after FLIP_THRESHOLD misses,
        # so one flaky probe doesn't alarm the UI.
        if prev == "ok" and fail_streak[scan_root] >= FLIP_THRESHOLD:
            log.warning("Share %s became unavailable", scan_root)
            await _set_share_status(scan_root, "unavailable")
            last_status[scan_root] = "unavailable"
        return True

    while True:
        await asyncio.sleep(interval)
        loop = asyncio.get_running_loop()
        any_down = False
        try:
            for scan_root, source in list(all_sources().items()):
                try:
                    if await _probe_one(scan_root, source, loop):
                        any_down = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One share's failure (a status write, a broken source
                    # object) never ends the pass for the others.
                    any_down = True
                    log.warning("Share health: probing %s failed", scan_root,
                                exc_info=True)

            # Configured shares with no source at all (their server was down
            # at startup, or a connect failed): try them again.
            try:
                if await _reconnect_pending_shares():
                    any_down = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Retrying unconnected shares failed", exc_info=True)
            failed_passes = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            # A pass that raised (outside the per-share guard) is logged and
            # retried with the down-share back-off — recovery never stops.
            failed_passes += 1
            any_down = True
            if failed_passes == 1 or failed_passes & (failed_passes - 1) == 0:
                log.exception("Share health pass failed (%d in a row) — retrying",
                              failed_passes)
            else:
                log.warning("Share health pass failed (%d in a row)", failed_passes)

        # Adaptive cadence: back off while anything is down, snap back when healthy.
        interval = min(interval * 1.5, MAX_INTERVAL) if any_down else BASE_INTERVAL


# Restart delays of the health monitor's supervisor (doubling, capped); a
# monitor that ran this long before dying restarts at the base delay again.
_HEALTH_RESTART_BASE_S = 5.0
_HEALTH_RESTART_MAX_S = 300.0
_HEALTH_RESTART_RESET_S = 600.0
_health_stopping = False


async def _share_health_supervisor():
    """Run :func:`_share_health_monitor` for the server's lifetime: when it
    raises, log it and start it again after a doubling delay.  Cancelling
    this task (shutdown) cancels the monitor with it."""
    delay = _HEALTH_RESTART_BASE_S
    while True:
        started = time.monotonic()
        try:
            await _share_health_monitor()
            log.error("Share health monitor returned — restarting in %.0f s", delay)
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if _health_stopping or me is None or me.cancelling():
                raise                       # this task is being cancelled (shutdown)
            # A cancellation raised by something the monitor awaited, not
            # aimed at this task: the monitor ended — restart it.
            log.error("Share health monitor was cancelled from inside — restarting "
                      "in %.0f s", delay)
        except Exception:
            log.exception("Share health monitor crashed — restarting in %.0f s", delay)
        if time.monotonic() - started >= _HEALTH_RESTART_RESET_S:
            delay = _HEALTH_RESTART_BASE_S
        await asyncio.sleep(delay)
        delay = min(delay * 2, _HEALTH_RESTART_MAX_S)


def _start_share_health_monitor() -> "asyncio.Task":
    """Start the supervised share health monitor; a task that ends while the
    server is not stopping is started again (``_on_health_task_done``)."""
    global _health_task, _health_stopping
    _health_stopping = False
    _health_task = asyncio.create_task(_share_health_supervisor(),
                                       name="share-health-monitor")
    _health_task.add_done_callback(_on_health_task_done)
    return _health_task


def _on_health_task_done(task: "asyncio.Task") -> None:
    if _health_stopping or task is not _health_task:
        return                              # shutdown, or already replaced
    exc = None if task.cancelled() else task.exception()
    log.error("Share health monitor task ended unexpectedly (%r) — restarting in %.0f s",
              exc, _HEALTH_RESTART_BASE_S)

    def _restart() -> None:
        if not _health_stopping and _health_task is task:
            _start_share_health_monitor()
    task.get_loop().call_later(_HEALTH_RESTART_BASE_S, _restart)


async def _stop_share_health_monitor(timeout: float = 1.0) -> None:
    """Cancel the health monitor (shutdown); no restart follows."""
    global _health_stopping
    _health_stopping = True
    t = _health_task
    if t and not t.done():
        t.cancel()
        try:
            await asyncio.wait_for(t, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass


def _stored_unavailable(scan_root: str) -> bool:
    """The store has this (non-catalogue) share marked unavailable."""
    try:
        from soniqboom.core import catalogue
        from soniqboom.core.store import get_store
        if catalogue.is_catalogue_root(scan_root):
            return False
        sd = get_store()._scan_dirs.get(scan_root)
        return bool(sd) and sd.get("status") == "unavailable"
    except Exception:
        return False


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


def _background_writer_tasks() -> list:
    """The tasks outside the scanner that write the store and are stopped
    with the scans (``scanner.stop_background_writers``' ``extra``): the admin
    API's remote scans and jobs, catalogue builds and merges, the online
    collections' jobs (told to stop at their next request), Track Info reads."""
    extra: list = []
    try:
        from soniqboom.api import admin as _admin
        extra = [*_admin._bg_scan_tasks, *_admin._BG_TASKS]
    except Exception:
        log.debug("admin background tasks not listed", exc_info=True)
    try:
        from soniqboom.core import modland_catalog
        extra.extend(modland_catalog._tasks)       # catalogue builds / merges
        from soniqboom.core import online_catalog
        online_catalog.signal_stop()               # a running listing ends at its next request
        extra.extend(online_catalog.all_tasks())   # online collections' jobs / merges
        from soniqboom.api import stream as _stream_mod
        extra.extend(_stream_mod._INFO_READ_TASKS)  # Track Info reads, merge watchers
    except Exception:
        log.debug("catalogue tasks not listed", exc_info=True)
    return extra


async def quiesce_for_exec() -> None:
    """The source install's restart (``admin._graceful_pre_exec_flush``, on
    this loop), before the journal is sealed: what the stop's first steps
    stop — the playback work (renderers, temp files: ``core.playback_stop``),
    the share health monitor, the folder watcher and its supervisor,
    the remote freshness loops, the availability watch (it re-probes local
    roots and records their status), the index-integrity sweep, a pending
    startup reconcile and the duplicate re-group runner (its pending work
    saved) — then the scans and the passes that write the store (as the
    stop's step 1b).  Each step bounded like the stop's."""
    t0 = time.monotonic()
    if _reconcile_handle is not None:
        _reconcile_handle.cancel()

    async def _bounded(name: str, coro, timeout: float) -> None:
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except Exception:                                   # noqa: BLE001 (incl. timeout)
            log.warning("Pre-restart stop of the %s failed or timed out — continuing",
                        name, exc_info=True)

    # Renders, transcodes, pumps and prewarms first: ``os.execv`` keeps the
    # pid, so a renderer left running would stay this process's child — never
    # waited for (a zombie once it ends) — and go on writing its temp file.
    from soniqboom.core.playback_stop import stop_playback_work
    await _bounded("playback work", stop_playback_work(), 4.5)
    await _stop_share_health_monitor(timeout=1.0)

    from soniqboom.core import remote_freshness, watcher
    await _bounded("folder watcher", watcher.stop(), 3.0)
    await _bounded("remote freshness polls", remote_freshness.stop(), 5.0)
    for stop_one in ("availability", "index_health", "dup_runner"):
        try:
            if stop_one == "availability":
                from soniqboom.core import availability
                availability.uninstall()
            elif stop_one == "index_health":
                from soniqboom.core import index_health
                index_health.stop()
            else:
                from soniqboom.core.scanner import cancel_background_tasks
                cancel_background_tasks()
        except Exception:                                   # noqa: BLE001
            log.debug("pre-restart stop of %s failed", stop_one, exc_info=True)
    from soniqboom.core.scanner import stop_background_writers
    await _bounded("scans and passes", stop_background_writers(_background_writer_tasks(),
                                                               timeout=3.0), 4.0)
    log.info("Pre-restart: background work stopped in %dms", (time.monotonic() - t0) * 1000)


def _snapshot_skippable(aof_ok: bool, journal_ended: bool, aof_bytes: int, max_mb: float) -> bool:
    """The stop's full snapshot may be skipped: the AOF flushed cleanly and is
    lean (``max_mb``) — and the journal hadn't already ended before the stop
    (a failed exec restart: the writes since are in memory only)."""
    return aof_ok and not journal_ended and aof_bytes <= max_mb * 1024 * 1024


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

    Only the run that holds the server state (``_claim_server``) stops it;
    its run epoch is read once, here — a start after this stop (the bundled
    app's Restart) can't redirect it.
    """
    global _aof_writer, _merger_proc
    if not _owns_server():
        log.warning("Shutdown of a server run that doesn't hold the server state — nothing stopped")
        return
    run_epoch = _run_epoch
    t_total = time.monotonic()

    # Per-step health, collected for the single consolidated "stopped" line at
    # the end so an operator (or a log-scraping monitor) can see at a glance
    # which subsystems shut down cleanly.  ``_step`` records "ok"/"TIMEOUT"/
    # "FAIL"; conditional steps (snapshot skip) record their own state, and a
    # step that never ran (no AOF writer, no remote sources) simply isn't in
    # the map — an honest picture of what was actually torn down.
    _results: dict[str, str] = {}

    async def _step(name: str, coro, timeout: float) -> bool:
        """Run one bounded shutdown step; return True iff it completed cleanly."""
        t = time.monotonic()
        try:
            await asyncio.wait_for(coro, timeout=timeout)
            log.info("Shutdown step %s: %dms", name, (time.monotonic() - t) * 1000)
            _results[name] = "ok"
            return True
        except asyncio.TimeoutError:
            log.warning("Shutdown step %s timed out after %.1fs — continuing",
                        name, timeout)
            _results[name] = "TIMEOUT"
            return False
        except Exception:
            log.exception("Shutdown step %s failed", name)
            _results[name] = "FAIL"
            return False

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

    # Cancel any detached fire-and-forget startup tasks still pending (e.g. the
    # renderer-health probe within its 5 s delay) so shutdown doesn't log a stray
    # "Task was destroyed but it is pending".
    for _t in list(_BG_TASKS):
        if not _t.done():
            _t.cancel()

    # Renders, transcodes, pumps and prewarms: their renderer processes and
    # temp files must not outlive the process (uvicorn re-raises the signal
    # once this stop returns — nothing runs after it; ``core.playback_stop``).
    async def _stop_playback() -> None:
        from soniqboom.core.playback_stop import stop_playback_work
        await stop_playback_work()
    await _step("playback", _stop_playback(), 4.5)

    # Stop the deadlock watchdog so its asyncio.sleep doesn't keep the
    # event loop alive past the shutdown budget.  Idempotent.
    try:
        from soniqboom.core import deadlock_watchdog
        deadlock_watchdog.stop()
    except Exception:
        log.debug("deadlock_watchdog.stop() failed (non-fatal)", exc_info=True)

    # Close the SSRF relay proxy (idempotent, best-effort).
    try:
        from soniqboom.core import ssrf_proxy
        await ssrf_proxy.stop()
    except Exception:
        log.debug("ssrf_proxy.stop() failed (non-fatal)", exc_info=True)

    # Stop the background index-integrity sweep.  Idempotent.
    try:
        from soniqboom.core import index_health
        index_health.stop()
    except Exception:
        log.debug("index_health.stop() failed (non-fatal)", exc_info=True)

    # Pending startup reconcile / duplicate re-group runner: don't start work
    # during shutdown (the re-group's pending state is persisted).
    if _reconcile_handle is not None:
        _reconcile_handle.cancel()
    try:
        from soniqboom.core.scanner import cancel_background_tasks
        cancel_background_tasks()
    except Exception:
        log.debug("scanner background cancel failed", exc_info=True)
    # TOSEC / Redump downloads: stopped now (their jobs end as "interrupted",
    # a transfer is cut off, no list is written after this point; the files
    # they finished stay kept).  Their threads are waited for below.
    try:
        from soniqboom.core import game_titles as _gt_stop
        stopped = _gt_stop.cancel_downloads(run_epoch)
        if stopped:
            log.info("Shutdown: stopped the %s download(s)", ", ".join(stopped))
    except Exception:
        log.debug("download cancel failed", exc_info=True)
    # (``finalize_dup_pending`` runs right before the journal flush, below)

    # Stop the folder watcher (observer thread + pending debounce).
    async def _stop_watcher():
        from soniqboom.core import watcher
        await watcher.stop()
    await _step("watcher", _stop_watcher(), 3.0)

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

    await _stop_share_health_monitor(timeout=1.0)
    try:
        from soniqboom.core import availability
        availability.uninstall()
    except Exception:
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

    # ── Step 1a: sign-in sessions → sessions.json (a coalesced write may
    # still be pending; they are restored at the next start) ──────────────
    async def _flush_sessions() -> None:
        from soniqboom.core.users import get_user_store
        await asyncio.to_thread(get_user_store().flush_sessions)

    await _step("sessions", _flush_sessions(), timeout=2.0)

    # ── Step 1a': the shared outbound HTTP clients (scrobbling, lyrics) are
    # bound to this run's event loop: closed with it (the bundled app's next
    # run, on a new loop, makes new ones) ──────────────────────────────────
    async def _close_http_clients() -> None:
        from soniqboom.core import scrobble as _scrobble
        await _scrobble.shutdown()
        await tracks.shutdown_lyrics_client()

    await _step("http-clients", _close_http_clients(), timeout=2.0)

    # ── Step 1b: scans + enrichment passes (3 s budget) ───────────────────
    # Stopped BEFORE the journal's final flush: a scan or post-scan pass still
    # running writes the store after it — records only the process's exit
    # flushed, after the next instance had loaded the AOF (lost: its shutdown
    # snapshot drops them) or, in the bundled app, after a later run's own
    # records (replayed over them).  Remote scans and an HVSC apply are the
    # admin API's tasks.
    async def _stop_writers() -> None:
        from soniqboom.core.scanner import stop_background_writers
        await stop_background_writers(_background_writer_tasks(), timeout=3.0)
    await _step("scans", _stop_writers(), timeout=4.0)

    # ── Step 2: AOF flush + close fd (flock budget + 1 s) ─────────────────
    # Cancel the periodic flush task ON the loop first (Task.cancel is
    # loop-bound), then do the blocking flush_sync + fd close off the loop
    # so a contended flock can't freeze the async runtime.  The step outlasts
    # the writer's flock wait (a merge holding the AOF), so it ends with the
    # flush — a stop still flushing when the snapshot's own stop starts
    # duplicated records (``AOFWriter.stop`` also serialises the two).
    # A duplicate-relevant write after the shutdown save → full pass next start.
    try:
        from soniqboom.core.scanner import finalize_dup_pending
        finalize_dup_pending()
    except Exception:
        log.debug("finalize_dup_pending failed", exc_info=True)
    aof_ok = False
    # The journal already ended when this stop began: the source install's
    # restart sealed it and its exec failed — writes since then are in memory
    # only (set aside, never replayed): the snapshot below is not optional.
    journal_ended = bool(_aof_writer) and bool(getattr(_aof_writer, "sealed", False))
    if _aof_writer:
        from soniqboom.core.aof import AOFWriter as _AOFWriter
        _aof_writer.cancel_flush_task()
        aof_ok = await _step("aof-flush",
                             asyncio.to_thread(_aof_writer.stop),
                             timeout=_AOFWriter._FLOCK_BUDGET_SECONDS + 1.0)

    # ── Step 3: Snapshot — CONDITIONAL (10 s budget) ──────────────────────
    # The full snapshot (``library.json`` — O(library), hundreds of MB on a
    # six-figure library) is only an OPTIMISATION for a fast next boot: boot
    # loads it and replays the AOF, so ``library.json`` + AOF already
    # reconstruct the full state.  (The ``os.execv`` restart path relies on
    # exactly this and skips the snapshot entirely.)
    #
    # So skip the expensive write when it's redundant — the AOF flushed
    # cleanly AND is lean, which the background merger keeps it (folds it into
    # ``library.json`` once it reaches ``merger_max_aof_mb``).  That frees the
    # port sooner on a restart.  Fall back to a full snapshot only when the AOF
    # flush was incomplete (durability safety net) or the AOF is large (a big
    # batch like a Demozoo apply — snapshotting now beats a long replay next
    # boot).  The snapshot holds the AOF it was written over, so it drops it
    # (``consume_aof`` — replayed on top of it, every play counted twice).
    async def _write_snap() -> None:
        from soniqboom.core.persistence import owns_library, write_snapshot_sync
        # (this loop runs the store's encode — at one instant with the count of
        # the journal records it holds, ``persistence._call_on_loop``)
        await asyncio.to_thread(write_snapshot_sync, get_data_dir(), owns_library(), _aof_writer,
                                asyncio.get_running_loop())

    try:
        _max_aof_mb = max(0.0, float(os.environ.get("SONIQBOOM_SHUTDOWN_SNAPSHOT_MAX_AOF_MB", "32")))
    except ValueError:
        _max_aof_mb = 32.0
    try:
        _aof_bytes = (get_data_dir() / "library.aof").stat().st_size
    except OSError:
        _aof_bytes = 0
    if _snapshot_skippable(aof_ok, journal_ended, _aof_bytes, _max_aof_mb):
        log.info("Shutdown step snapshot: SKIPPED — AOF flushed cleanly and lean "
                 "(%.2f MB ≤ %.0f MB); next boot replays it, so the full snapshot "
                 "write is unnecessary.", _aof_bytes / 1e6, _max_aof_mb)
        _results["snapshot"] = "skipped"
    else:
        reason = ("the journal had ended (a failed restart)" if journal_ended
                  else "AOF flush incomplete" if not aof_ok
                  else f"AOF is large ({_aof_bytes / 1e6:.1f} MB)")
        log.info("Shutdown: writing the full snapshot (%s).", reason)
        await _step("snapshot", _write_snap(), timeout=10.0)

    # Re-persist the folder-browse cache when an enrichment pass refreshed its
    # rows in memory this run (it deleted the stale disk copy then): the next
    # boot restores it (~0.6 s) instead of rebuilding every root (~6 s).  AFTER
    # the AOF flush + snapshot (library data first, a stop timeout may be
    # short) and off the loop, so its timeout actually applies.
    async def _persist_browse_cache():
        from soniqboom.core import folder_album
        if not folder_album.browse_disk_stale():
            return
        from soniqboom.api.fstree import _save_browse_cache
        from soniqboom.config import get_data_dir
        n = await asyncio.to_thread(_save_browse_cache, get_data_dir())
        log.info("Browse cache: re-persisted %d root(s) at shutdown", n)
    await _step("browse-cache", _persist_browse_cache(), timeout=10.0)

    # ── Step 4: Merger (3 s budget — interruptible since merger.py fix) ───
    async def _stop_merger() -> None:
        if isinstance(_merger_proc, asyncio.Task):
            # The bundled app's merger is a task of this loop (it has no
            # ``is_alive``): stopped cooperatively, without a final merge —
            # the AOF was just flushed and the next start replays it (a full
            # snapshot was written above when it isn't lean).  A merge in
            # progress is waited for (``_WRITING_STEPS``).
            from soniqboom.core import merger as _merger
            await _merger.stop_merger(_merger_proc, final=False)
            return
        if _merger_proc and _merger_proc.is_alive():
            _merger_proc.terminate()
            await asyncio.to_thread(_merger_proc.join, 3)
            if _merger_proc.is_alive():
                log.warning("Merger ignored SIGTERM — escalating to SIGKILL")
                _merger_proc.kill()
        elif _merger_proc is not None and getattr(_merger_proc, "pid", None):
            # Its forkserver died: ``is_alive`` can't tell any more, but the
            # merger itself may still run — stop it by pid (only while that
            # pid is still a process forked from our forkserver).
            pid = _merger_proc.pid
            if not _is_our_forkserver_child(pid):
                return
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return                                  # gone
            for _ in range(30):
                await asyncio.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    await _step("merger", _stop_merger(), timeout=3.5)
    # The journal ends here: what is still buffered is written, and a store
    # write still arriving (a task that outlived step 1b, a thread-pool job)
    # is set aside instead of reaching the AOF at the process's exit — after
    # the next instance (or run) loaded it (``AOFWriter.seal``).  Intake is
    # closed on the loop first and the journal ended after the step whatever
    # it did, so even a seal job that timed out (still waiting for a lock)
    # writes nothing after ``release_library`` (``AOFWriter.end_journal``).
    if _aof_writer:
        from soniqboom.core.aof import AOFWriter as _AOFWriter
        _seal_budget = _AOFWriter._FLOCK_BUDGET_SECONDS * 2
        _aof_writer.close_intake()
        await _step("aof-seal", asyncio.to_thread(_aof_writer.seal, _seal_budget),
                    timeout=_seal_budget + 1.0)
        _aof_writer.end_journal()
    # The library is written: an instance waiting to start may load it now
    # (``persistence.claim_library``).
    from soniqboom.core.persistence import release_library
    release_library()

    # ── Step 4b: scan worker pools + their forkserver (a stop during a scan
    # would otherwise leave them running) ───────────────────────────────────
    async def _stop_scan_workers() -> None:
        from soniqboom.core.scanner import shutdown_worker_pools
        await asyncio.to_thread(shutdown_worker_pools, run_epoch)

    await _step("scan-workers", _stop_scan_workers(), timeout=3.0)

    async def _wait_downloads() -> None:
        from soniqboom.core import game_titles as _gt_wait
        if not await asyncio.to_thread(_gt_wait.wait_for_downloads, 2.0):
            log.warning("a stopped download's thread is still ending")

    await _step("downloads", _wait_downloads(), timeout=2.5)

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

    # One consolidated "stopped" line: the address we were bound to plus the
    # per-subsystem shutdown health, so an operator can confirm a clean stop
    # (and diagnose a dirty one) from a single grep.  ``shutdown.sh`` also
    # tails for the word "stopped" to echo the final state to the terminal.
    _clean = sum(1 for v in _results.values() if v in ("ok", "skipped"))
    _detail = " ".join(f"{k}={v}" for k, v in _results.items()) or "nothing to stop"
    log.info(
        "SoniqBoom stopped — was listening on %s:%s · modules %d/%d clean [%s] · shutdown %.0fms",
        settings.host, settings.port, _clean, len(_results), _detail,
        (time.monotonic() - t_total) * 1000,
    )
    await _release_thread_pool(_results)
    # (A later run in this process — the bundled app's Start — starts
    # renderers again; this run's tasks are gone with its loop.)
    from soniqboom.core import forksafe as _fs_reopen
    _fs_reopen.open_spawns()
    _release_server()


# ── Frontend static files ─────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    log.info("Serving frontend from %s", FRONTEND_DIR)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="frontend")

# Bundled HTML user manual — served at /manual (html=True → /manual/ serves
# index.html).  Best-effort: mounted only when the docs shipped.  The header's
# docs button links here.  Mounted before the SPA routes below: their
# ``{full_path:path}`` catch-all would otherwise answer /manual too.
if DOCS_DIR.is_dir():
    log.info("Serving user manual from %s", DOCS_DIR)
    app.mount("/manual", StaticFiles(directory=DOCS_DIR, html=True), name="manual")

def _shell_file(request: "Request | None", path: Path):
    """An HTML shell (``/``, ``/m``, ``/multiroom``) with its JS URLs versioned
    by content (``core.shell_html``): ETag, and a 304 when the browser's copy
    is current (the shells are ``no-cache``, so every load asks)."""
    from fastapi.responses import Response
    from soniqboom.core import shell_html
    body, etag = shell_html.render_shell(path, FRONTEND_DIR / "js")
    headers = {"ETag": etag}
    if request is not None and shell_html.shell_etag(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="text/html; charset=utf-8", headers=headers)


if FRONTEND_DIR.exists():
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
    async def mobile_shell(request: Request, rest: str = ""):
        # Plain serve — deliberately does NOT pin sb_ui=mobile.  Setting a
        # sticky cookie here had a nasty side effect: the service worker
        # precaches ``/m`` (and any link/prefetch hits it too), so a desktop
        # browser would silently acquire sb_ui=mobile just from the SW
        # install and then bounce ``/`` to ``/m`` forever.  Deliberate
        # mobile pinning still happens via the explicit ``?ui=mobile``
        # override on ``/``; real phones get ``/m`` via UA sniffing each
        # visit (cheap), so losing the optimization costs nothing.
        return _shell_file(request, FRONTEND_DIR / "mobile.html")

    # ``/multiroom`` HTML shell is gated on the multiroom service toggle —
    # disable in Settings → Services to hide the page entirely (404 from
    # the SPA fallback below).  The API router is gated above.
    if _svc_on("multiroom"):
        @app.get("/multiroom", include_in_schema=False)
        @app.get("/multiroom/{rest:path}", include_in_schema=False)
        async def multiroom_shell(request: Request, rest: str = ""):
            return _shell_file(request, FRONTEND_DIR / "multiroom.html")

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
                    _shell_file(None, FRONTEND_DIR / "index.html"))
            resp.set_cookie("sb_ui", override, max_age=60 * 60 * 24 * 365,
                            samesite="lax")
            return resp
        # Sticky cookie wins over UA sniffing.
        pinned = request.cookies.get("sb_ui", "")
        if pinned == "mobile":
            return RedirectResponse("/m", status_code=302)
        if pinned == "desktop":
            return _shell_file(request, FRONTEND_DIR / "index.html")
        # First visit: route phones to the mobile shell.  iPads + desktops
        # still get the rich UI because the regex deliberately excludes them.
        ua = request.headers.get("user-agent", "")
        if _MOBILE_UA_RE.search(ua):
            return RedirectResponse("/m", status_code=302)
        return _shell_file(request, FRONTEND_DIR / "index.html")

    # SPA fallback MUST be registered after explicit "/" — Starlette's
    # ``{full_path:path}`` converter matches empty string, so ``/`` would
    # otherwise resolve to the fallback first and the root handler above
    # (mobile redirect + cookie pinning) would never run.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # An unknown /api path is a JSON 404, not the SPA page — a JSON
        # client must be able to tell "endpoint missing" from success.
        # (Unknown /rest paths get a Subsonic envelope from api/subsonic.)
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        return _shell_file(None, FRONTEND_DIR / "index.html")
else:
    log.error(
        "Frontend directory not found — UI will return 404. "
        "Tried dev (%s), exe-dir/frontend, and .app Resources/frontend. "
        "If running a built app, the installer bundle is missing assets.",
        Path(__file__).resolve().parent / "frontend",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_port(host: str, port: int) -> None:
    """Ensure ``port`` is bindable, WAITING for a departing instance to free it.

    A restart launches the new instance while the OLD one may still be flushing
    a large library's AOF + snapshot and hasn't released its listen socket yet
    — very visible on big volumes.  The app-bundle restart in particular runs
    both processes briefly side by side.  Failing immediately turned that
    slow-but-normal shutdown into "port already in use" and aborted the restart.

    So instead of exiting on first contact, poll until the socket is actually
    free, up to ``SONIQBOOM_PORT_WAIT_S`` seconds (default 60).  The
    ``SO_REUSEADDR`` probe succeeds the moment the old process closes its LISTEN
    socket (even while it lingers in TIME_WAIT), so the wait ends as soon as the
    departing server is genuinely down — not a fixed sleep that a big flush
    outlasts.  A port held by an *unrelated* process still fails, after the
    timeout, with the same actionable error.
    """
    try:
        wait_s = max(0.0, float(os.environ.get("SONIQBOOM_PORT_WAIT_S", "60")))
    except ValueError:
        wait_s = 60.0
    deadline = time.monotonic() + wait_s
    announced = False
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                if announced:
                    print(f"Port {port} is free — continuing startup.", file=sys.stderr)
                return                                  # free — go
            except OSError:
                pass
        if time.monotonic() >= deadline:
            print(
                f"ERROR: port {port} is still in use on {host} after {int(wait_s)}s "
                f"— another process is holding it.\n"
                f"Use --port <number> to choose a different port, or set "
                f"SONIQBOOM_PORT_WAIT_S to wait longer for a slow shutdown.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not announced:
            print(
                f"Port {port} is in use — a previous instance may still be shutting "
                f"down (flushing a large library).  Waiting up to {int(wait_s)}s for "
                f"it to release the port…",
                file=sys.stderr,
            )
            announced = True
        time.sleep(0.25)


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
        sys.exit(forksafe.run(cmd).returncode)

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
