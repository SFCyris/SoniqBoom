# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Log-noise suppression for benign WebSocket handshake rejects.

An auth-gated progress socket (``/api/library/ws``, ``/api/multiroom/ws``) is
rejected whenever an expired / not-yet-signed-in browser tab retries its
handshake.  uvicorn logs that on ``uvicorn.error`` as
``<client> - "WebSocket <path>" 403`` and the bundled ``websockets`` library
adds ``connection rejected (403 Forbidden)`` + the bare ``connection open`` /
``connection closed`` lifecycle lines.  None is a fault, so ``log_control``
drops them at the quiet access tiers and shows them at ``all`` — parity with
the by-design ``fallback=404`` art requests.

These tests pin two things:

1. The classifier (:func:`_is_benign_ws_line`) — the benign lines are matched
   (incl. a reverse-proxy-prefixed path), and — critically — NO real error is
   ever matched.  This is the anti-drift guard: if the match predicates are
   ever broadened, the "real errors survive" cases fail loudly.
2. The wiring — driving the ACTUAL ``uvicorn.error`` logger through a
   ``LoggerAdapter`` (exactly how uvicorn hands its logger to the websockets
   protocol) proves the filter is reached and the benign lines are dropped in
   ``problems``/``off`` and pass in ``all``.

The exact emitted strings are documented against uvicorn 0.44 +
websockets 13.x/16.0 legacy impl (``websockets_impl.py:279`` for the uvicorn
line; ``legacy/server.py:229/263/642`` + ``legacy/protocol.py`` for the
library lines).  If a dependency upgrade changes those strings, live
suppression regresses silently — re-verify manually with
``access_log=all`` vs ``problems`` and update the constants below.
"""
from __future__ import annotations

import logging

import pytest

from soniqboom.core import log_control

# ── The lines the runtime actually emits on uvicorn.error ─────────────────────
BENIGN = [
    '127.0.0.1:60766 - "WebSocket /api/library/ws" 403',       # uvicorn reject
    '10.0.0.28:5 - "WebSocket /api/multiroom/ws?x=1" 403',     # reject + query
    '1.2.3.4:9 - "WebSocket /music/api/library/ws" 403',       # proxy-prefixed
    'connection rejected (403 Forbidden)',                     # websockets lib
    'connection open',                                          # lifecycle
    'connection closed',                                        # lifecycle
]

# Must ALWAYS survive — informative lines and every real-error class.
NEVER_SUPPRESS = [
    '10.0.0.28:1 - "WebSocket /api/library/ws" [accepted]',    # a real connect
    'connection handler failed',                               # websockets ERROR
    'opening handshake failed',                                # websockets ERROR
    'closing handshake failed',                                # websockets ERROR
    'ASGI callable returned without sending handshake.',       # uvicorn ERROR
    'Exception in ASGI application',                           # uvicorn traceback
    '1.2.3.4:5 - "GET /api/admin/settings" 403',              # a real non-WS 403
    '1.2.3.4:5 - "WebSocket /api/other/socket" 403',          # non-auth WS path
    'connection rejected (400 Bad Request)',                  # non-403 → anomaly
]


def _rec(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.error", level, __file__, 0, msg, None, None)


@pytest.mark.parametrize("msg", BENIGN)
def test_classifier_flags_benign(msg):
    assert log_control._is_benign_ws_line(_rec(msg)) is True


@pytest.mark.parametrize("msg", NEVER_SUPPRESS)
def test_classifier_never_flags_real(msg):
    assert log_control._is_benign_ws_line(_rec(msg)) is False


@pytest.fixture
def uvicorn_error_capture():
    """Attach a capture handler to the real ``uvicorn.error`` logger, restore
    the access mode afterwards so other tests / the app aren't affected."""
    captured: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger = logging.getLogger("uvicorn.error")
    handler = _Cap()
    prev_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    prev_mode = log_control.current()["access_log"]
    try:
        yield captured, logger
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        log_control.apply_access_log_mode(prev_mode)


@pytest.mark.parametrize("mode", ["problems", "off"])
def test_quiet_modes_drop_benign_keep_real(mode, uvicorn_error_capture):
    captured, logger = uvicorn_error_capture
    log_control.apply_access_log_mode(mode)
    # Emit through a LoggerAdapter — exactly how uvicorn hands its logger to the
    # websockets protocol (websockets_impl.py:108 → legacy adapter).
    adapter = logging.LoggerAdapter(logger, {})
    for m in BENIGN:
        adapter.info("%s", m)
    for m in NEVER_SUPPRESS:
        lvl = logging.ERROR if ("failed" in m or "Exception" in m or "without sending" in m) else logging.INFO
        adapter.log(lvl, "%s", m)
    assert not any(m in captured for m in BENIGN), f"{mode}: a benign line leaked"
    for m in NEVER_SUPPRESS:
        assert m in captured, f"{mode}: real/informative line was suppressed: {m!r}"


def test_all_mode_shows_everything(uvicorn_error_capture):
    captured, logger = uvicorn_error_capture
    log_control.apply_access_log_mode("all")
    adapter = logging.LoggerAdapter(logger, {})
    for m in BENIGN + NEVER_SUPPRESS:
        adapter.info("%s", m)
    for m in BENIGN + NEVER_SUPPRESS:
        assert m in captured, f"all: line unexpectedly dropped: {m!r}"


def test_apply_access_log_mode_is_idempotent():
    """Repeated calls must not stack duplicate filters on either logger."""
    for _ in range(3):
        log_control.apply_access_log_mode("problems")
    err = logging.getLogger("uvicorn.error")
    acc = logging.getLogger("uvicorn.access")
    assert sum(f is log_control._ws_handshake_filter for f in err.filters) == 1
    assert sum(f is log_control._access_filter for f in acc.filters) == 1
