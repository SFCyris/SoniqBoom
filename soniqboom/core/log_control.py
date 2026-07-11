# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime-toggleable log verbosity — two independent, UI-controlled dials.

Two log streams get conflated in a default deployment, which makes ordinary
traffic look like faults (a user once opened an issue because a single MP3
playback produced dozens of ``GET /api/art/…?fallback=404`` access lines — all
expected placeholders for tracks without cover art):

* **Application log** (the ``soniqboom`` logger tree): lifecycle, scans, real
  errors.  Controlled by ``log_level`` → WARNING / INFO / DEBUG.  The quietest
  tier is WARNING, not ERROR, so an admin who dials it down still SEES the
  actionable events (share offline, corrupt-snapshot fallback, renderer /
  network failures) — those are logged at WARNING, and hiding them would defeat
  the point of a "quiet but not blind" mode.
* **HTTP access log** (uvicorn's ``uvicorn.access`` logger): one line per
  request, so a ``404`` there is just a status, not a failure.  Controlled by
  ``access_log`` → off / problems / all, where "problems" keeps only genuine
  4xx/5xx **and drops the deliberately-expected 404s** (art requested with
  ``fallback=404`` so the client can show a placeholder).

The access dial ALSO governs a third, WebSocket-specific source of scary-
looking-but-benign lines.  An auth-gated progress socket (``/api/library/ws``,
``/api/multiroom/ws``) is rejected whenever a browser tab whose session has
expired — or a not-yet-signed-in tab — retries its handshake.  uvicorn logs
that on ``uvicorn.error`` as ``<client> - "WebSocket <path>" 403`` and the
bundled ``websockets`` library adds ``connection rejected (403 Forbidden)``.
Neither is a fault (the client simply needs to sign in again), so — exactly
like the ``fallback=404`` art requests — they are hidden at the quiet access
tiers and shown only at "all".  Real ``uvicorn.error`` output (tracebacks, the
"ASGI callable returned without sending handshake" class of message) is never
touched.

Both are applied live (no restart): the app level is a ``setLevel`` on the
``soniqboom`` logger; the access mode is a mutable attribute on a single
``logging.Filter`` attached once to ``uvicorn.access``.  Persisted via the
store's ``set_config``/``get_config`` and surfaced in Settings.
"""
from __future__ import annotations

import logging

# ── App verbosity: UI value → level on the ``soniqboom`` logger tree ──────────
# Only the ``soniqboom`` logger is moved (not root), so "verbose" doesn't drown
# the console in third-party DEBUG and "errors" doesn't hide uvicorn's one-time
# startup lines / tracebacks.
_APP_LEVELS: dict[str, int] = {
    # "errors" → WARNING (NOT ERROR) on purpose: the operational events an admin
    # most wants when they dial the log down — share went offline, snapshot
    # corrupt → fell back, renderer/network failure — are logged at WARNING
    # (187 .warning() calls across the tree vs 30 .error()).  Pure ERROR would
    # silence all of them.  The UI labels this tier "Errors & warnings".
    "errors": logging.WARNING,   # failures + actionable warnings (quietest)
    "normal": logging.INFO,      # + lifecycle milestones (default)
    "verbose": logging.DEBUG,    # + per-request / per-track detail
}
DEFAULT_APP_LEVEL = "normal"

# ── Access verbosity ─────────────────────────────────────────────────────────
_ACCESS_MODES = ("off", "problems", "all")
DEFAULT_ACCESS_MODE = "problems"   # quiet, but real HTTP problems still surface

# Paths whose 404 is BY DESIGN — the client asks for ``fallback=404`` so it can
# render a placeholder.  These must never count as a "problem".  (The mobile SPA
# uses the same ``/api/art/`` endpoint — there is no separate mobile art route.)
_EXPECTED_404_PREFIXES = ("/api/art/",)

# Auth-gated WebSocket endpoints.  A rejected handshake on one of these is an
# EXPECTED, benign event (an expired or not-yet-signed-in browser tab retrying
# its progress socket), not a server fault — see the module docstring.  Both
# the uvicorn access line and the ``websockets`` library's own reject line land
# on ``uvicorn.error`` (uvicorn passes that logger into the websockets protocol
# via a LoggerAdapter — verified against uvicorn 0.44 + websockets 13.x/16.0
# ``ws="auto"`` legacy impl; the matched strings are identical across those).
# INVARIANT this suppression relies on: *every* WebSocket route in SoniqBoom is
# auth-gated, so any WS 403 is an auth reject.  If a future WS endpoint rejects
# with 403 for a non-auth reason, add it here (or it, too, would be quietened).
#
# Matched on the route SUFFIX, not the full path, so a reverse-proxy / uvicorn
# ``--root-path`` deployment that logs a prefixed path (``/music/api/library/ws``)
# is still recognised.  The suffixes are distinctive enough not to collide.
_WS_AUTH_PATH_SUFFIXES = ("/library/ws", "/multiroom/ws")


def _record_status(record: logging.LogRecord) -> int | None:
    """Status code from a uvicorn.access record.  Its args tuple is
    ``(client_addr, method, path+query, http_version, status_code)`` (verified
    against uvicorn 0.44 h11/httptools protocols)."""
    args = record.args
    if isinstance(args, (tuple, list)) and len(args) >= 5:
        try:
            return int(args[4])
        except (TypeError, ValueError):
            return None
    return None


def _record_path(record: logging.LogRecord) -> str:
    args = record.args
    if isinstance(args, (tuple, list)) and len(args) >= 3:
        return str(args[2])
    return ""


class _AccessLogFilter(logging.Filter):
    """Gate on ``uvicorn.access`` — one instance, mode flipped at runtime."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = DEFAULT_ACCESS_MODE

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if self.mode == "all":
            return True
        if self.mode == "off":
            return False
        # "problems": only 4xx/5xx, minus the by-design fallback-404s.
        status = _record_status(record)
        if status is None:
            # Not an access-shaped record (nothing emits these on
            # uvicorn.access today).  Fail OPEN — never silently hide an
            # unclassifiable line in a mode whose job is to surface problems.
            return True
        if status < 400:
            return False
        if status == 404:
            path = _record_path(record)
            # Drop ONLY the by-design fallback-404s.  ``startswith`` (not ``in``)
            # so a genuine 404 on some other path whose query merely contains
            # "/api/art/" isn't wrongly suppressed — uvicorn logs the path as
            # ``quote(scope["path"]) + "?" + query``, so a real art 404 always
            # starts with the prefix.
            if "fallback=404" in path and any(path.startswith(p) for p in _EXPECTED_404_PREFIXES):
                return False
        return True


def _is_benign_ws_line(record: logging.LogRecord) -> bool:
    """True for the benign, non-error WebSocket lines on ``uvicorn.error`` that
    should stay quiet at the default tiers:

      * uvicorn's auth-reject access line ``<client> - "WebSocket <path>" 403``
      * the ``websockets`` library's ``connection rejected (403 Forbidden)``
      * the library's bare ``connection open`` / ``connection closed``
        lifecycle chatter (one pair per socket; noise in a "quiet" log, and
        after the reject lines above are hidden a lone ``connection closed``
        just reads as an orphan)

    Everything else on ``uvicorn.error`` returns False so it is NEVER hidden:
    the informative ``"WebSocket <path>" [accepted]`` line, and — crucially —
    the library's own ERROR-level failures (``opening handshake failed``,
    ``connection handler failed``, ``closing handshake failed``) plus any
    uvicorn traceback."""
    try:
        msg = record.getMessage()
    except Exception:
        return False
    # Bare lifecycle chatter.  These EXACT strings are emitted only by the
    # websockets library at INFO (its abnormal-close paths use distinct
    # ERROR-level messages, which won't match here) — see websockets 13.x
    # legacy/server.py.
    if msg in ("connection open", "connection closed"):
        return True
    # websockets-lib reject line: "connection rejected (403 Forbidden)".  Match
    # on the 403 prefix (the phrase is always "Forbidden" for 403) so a reject
    # with a *different* status — which would be a genuine anomaly — still logs.
    if msg.startswith("connection rejected (403"):
        return True
    # uvicorn's handshake-reject access line ends in `" 403` and names the WS
    # path.  Match the route SUFFIX immediately before the closing quote (no
    # query) or the ``?`` (with query), so a full path like
    # ``"WebSocket /library/ws"`` or a proxied ``"WebSocket /music/api/library/ws"``
    # both anchor, while an unrelated 403 is never hidden.
    if msg.endswith('" 403') and '"WebSocket ' in msg:
        return any(
            f'{s}"' in msg or f'{s}?' in msg
            for s in _WS_AUTH_PATH_SUFFIXES
        )
    return False


class _WsHandshakeFilter(logging.Filter):
    """Gate on ``uvicorn.error`` — drops ONLY the benign WS handshake-reject +
    lifecycle lines (see :func:`_is_benign_ws_line`), and only at the quiet
    access tiers.  Shares the access dial: "all" shows them (parity with the
    by-design art 404s); "off"/"problems" hide them.  Real ``uvicorn.error``
    output — including the websockets library's ERROR-level failures — always
    passes."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = DEFAULT_ACCESS_MODE

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if self.mode == "all":
            return True
        return not _is_benign_ws_line(record)


_access_filter = _AccessLogFilter()
_ws_handshake_filter = _WsHandshakeFilter()
_installed = False


def apply_app_log_level(value: str | None) -> str:
    """Set the ``soniqboom`` logger level from a UI value; return the normalised
    value actually applied (falls back to the default on an unknown value)."""
    norm = value if value in _APP_LEVELS else DEFAULT_APP_LEVEL
    logging.getLogger("soniqboom").setLevel(_APP_LEVELS[norm])
    return norm


def apply_access_log_mode(value: str | None) -> str:
    """Set the access-log mode; return the normalised value applied.  Attaches
    the access filter to ``uvicorn.access`` and the WS-handshake filter to
    ``uvicorn.error`` exactly once each (idempotent).  Both share this dial."""
    norm = value if value in _ACCESS_MODES else DEFAULT_ACCESS_MODE
    _access_filter.mode = norm
    acc = logging.getLogger("uvicorn.access")
    if _access_filter not in acc.filters:
        acc.addFilter(_access_filter)
    # The benign WS-auth-reject lines live on ``uvicorn.error`` (uvicorn passes
    # that logger into the websockets protocol), so a second filter rides there
    # — dropping only those lines, never real errors.
    _ws_handshake_filter.mode = norm
    err = logging.getLogger("uvicorn.error")
    if _ws_handshake_filter not in err.filters:
        err.addFilter(_ws_handshake_filter)
    return norm


def install(app_level: str | None = None, access_mode: str | None = None) -> None:
    """Attach the access filter and set initial levels (called from startup so
    the dials are live even before the persisted config is read)."""
    global _installed
    apply_app_log_level(app_level)
    apply_access_log_mode(access_mode)
    _installed = True


def apply_from_config(get_config) -> dict[str, str]:
    """Apply the persisted settings.  ``get_config`` is the store's SYNC
    ``get_config(key, default)``.  Returns the applied ``{log_level, access_log}``."""
    return {
        "log_level": apply_app_log_level(get_config("log_level", DEFAULT_APP_LEVEL)),
        "access_log": apply_access_log_mode(get_config("access_log", DEFAULT_ACCESS_MODE)),
    }


def current() -> dict[str, str]:
    """Current dial positions, for the Settings panel."""
    lvl = logging.getLogger("soniqboom").level
    app = next((k for k, v in _APP_LEVELS.items() if v == lvl), DEFAULT_APP_LEVEL)
    return {"log_level": app, "access_log": _access_filter.mode}


# Exposed for the Settings UI / validation.
APP_LEVEL_VALUES = tuple(_APP_LEVELS.keys())
ACCESS_MODE_VALUES = _ACCESS_MODES
