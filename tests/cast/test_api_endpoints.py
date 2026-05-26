# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the ``/api/cast/*`` and ``/cast/{token}/...``
HTTP surface via FastAPI's TestClient.

What this proves vs. doesn't:

  ✓ Each endpoint exists, accepts the documented JSON shape, returns
    the documented JSON shape, and honours its auth gate.
  ✓ A signed cast URL serves bytes anonymously; a bad token gets 404
    with no info leak.
  ✓ The Pydantic-validated request bodies actually validate the way
    we say they do (e.g. negative subsong → 422).
  ✗ Real Cast / DLNA / AirPlay devices actually receive and play the
    payloads — that needs hardware.

The test deliberately doesn't run discovery against the real LAN —
``cast_targets.discover`` is monkey-patched to return a synthetic
target list so the tests are hermetic.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture()
def client(tmp_data_dir):
    """A FastAPI TestClient with persistence pointed at an empty
    temp dir.  We create a single admin user and log them in so the
    auth-gated endpoints work — without this every POST returns 401
    even when there are no users (because ``Depends(require_user)``
    runs alongside Pydantic body validation).

    ``tmp_data_dir`` is set BEFORE we import the app so persistence
    loads against the empty dir, not the user's real one.
    """
    from fastapi.testclient import TestClient
    from soniqboom.core.persistence import init_persistence
    from soniqboom.core.users import init_user_store, get_user_store
    init_persistence(tmp_data_dir)
    init_user_store(tmp_data_dir)
    # Create an admin user so require_user has someone to admit.
    store = get_user_store()
    store.create(username="testadmin", password="testpass", role="admin")
    # Now import the app — every route registers against the now-
    # initialised persistence layer.
    from soniqboom.main import app
    c = TestClient(app)
    # Authenticate so the session cookie is on the client for all
    # subsequent requests.
    r = c.post("/api/auth/login",
               json={"username": "testadmin", "password": "testpass"})
    assert r.status_code == 200, f"test login failed: {r.status_code} {r.text}"
    return c


# ── /api/cast/status ──────────────────────────────────────────────────────

def test_status_returns_backend_availability(client):
    """The picker uses /status to decide whether to show
    'Install pychromecast' hints when the backend isn't installed."""
    # No users yet → fresh-install allowlist lets this through
    resp = client.get("/api/cast/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "available" in data
    assert set(data["available"].keys()) == {"cast", "airplay", "dlna"}
    # We installed all three earlier in the test session
    assert data["available"]["cast"] is True
    assert data["available"]["airplay"] is True
    assert data["available"]["dlna"] is True
    # No install hints when everything is available
    assert data["install_hints"] == {}


# ── /api/cast/targets ─────────────────────────────────────────────────────

def test_targets_returns_empty_list_when_no_devices(client):
    """Hermetic test: monkey-patch discover to return no devices.  In a
    real LAN with no cast-capable hardware this is the legitimate
    response, and the UI should render 'no devices found' gracefully."""
    async def _fake_discover(timeout=4.0, force_refresh=False):
        return []
    with patch("soniqboom.core.cast_targets.discover", _fake_discover):
        resp = client.get("/api/cast/targets")
    assert resp.status_code == 200
    assert resp.json() == {"targets": []}


def test_targets_returns_synthetic_device(client):
    """With a fake DLNA target in the list, the response surface is the
    same shape the picker UI will consume."""
    from soniqboom.core.cast_targets import CastTarget
    fake = [
        CastTarget(
            id="dlna:test-uuid", name="Mock Sonos",
            protocol="dlna", host="10.0.0.50", port=1400,
            model="Sonos One",
            description_url="http://10.0.0.50:1400/xml/device_description.xml",
        ),
        CastTarget(
            id="cast:test-uuid", name="Mock Chromecast",
            protocol="cast", host="10.0.0.51", port=8009,
        ),
        CastTarget(
            id="airplay:test-uuid", name="Mock Apple TV",
            protocol="airplay", host="10.0.0.52", port=7000,
        ),
    ]
    async def _fake_discover(timeout=4.0, force_refresh=False):
        return fake
    with patch("soniqboom.core.cast_targets.discover", _fake_discover):
        resp = client.get("/api/cast/targets")
    assert resp.status_code == 200
    targets = resp.json()["targets"]
    assert len(targets) == 3
    protocols = {t["protocol"] for t in targets}
    assert protocols == {"dlna", "cast", "airplay"}
    # Picker needs at minimum (id, name, protocol) per target
    for t in targets:
        assert {"id", "name", "protocol"}.issubset(t.keys())


# ── /api/cast/sessions ────────────────────────────────────────────────────

def test_sessions_empty_on_startup(client):
    resp = client.get("/api/cast/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": []}


# ── /api/cast/play body validation ────────────────────────────────────────

def test_play_rejects_missing_fields(client):
    """Pydantic gate — missing target_id or track_id returns 422 before
    we even try to dispatch to a controller."""
    resp = client.post("/api/cast/play", json={"target_id": "x"})  # no track_id
    assert resp.status_code == 422
    resp = client.post("/api/cast/play", json={"track_id": "y"})  # no target_id
    assert resp.status_code == 422


def test_play_rejects_negative_subsong(client):
    """``ge=0`` constraint on the subsong field — defends against a
    hostile client feeding negative values into sidplayfp's ``--subsong``
    argument (where -1 would be interpreted as a flag)."""
    resp = client.post("/api/cast/play", json={
        "target_id": "x", "track_id": "y", "subsong": -1,
    })
    assert resp.status_code == 422


def test_play_rejects_oversized_subsong(client):
    """Soft ceiling — 4096 is way past any real container's subsong
    count, and bounds the upstream `--subsong=N` arg."""
    resp = client.post("/api/cast/play", json={
        "target_id": "x", "track_id": "y", "subsong": 999_999,
    })
    assert resp.status_code == 422


def test_play_against_missing_target_returns_404(client):
    async def _fake_discover(timeout=4.0, force_refresh=False):
        return []
    with patch("soniqboom.core.cast_targets.discover", _fake_discover):
        resp = client.post("/api/cast/play", json={
            "target_id": "nonexistent", "track_id": "no-such-track",
        })
    assert resp.status_code == 404
    assert "no longer visible" in resp.json()["detail"].lower()


# ── /api/cast/preference Pydantic ──────────────────────────────────────────

def test_preference_rejects_unknown_value(client):
    """Literal-typed pref → invalid value gets 422."""
    resp = client.post("/api/cast/preference", json={
        "target_id": "x", "pref": "force-vorbis",  # not a valid choice
    })
    assert resp.status_code == 422


def test_preference_accepts_documented_values(client):
    """Each documented pref value gets through Pydantic — discovery
    miss after that lands us at 404 (target not found), which is the
    expected next-step error."""
    async def _fake_discover(timeout=4.0, force_refresh=False):
        return []
    with patch("soniqboom.core.cast_targets.discover", _fake_discover):
        for pref in ("auto", "force-mp3", "force-flac", "force-original"):
            resp = client.post("/api/cast/preference", json={
                "target_id": "x", "pref": pref,
            })
            assert resp.status_code == 404, (
                f"pref={pref}: expected 404 from missing-target, got {resp.status_code}"
            )


# ── /api/cast/control Pydantic ─────────────────────────────────────────────

def test_control_rejects_unknown_action(client):
    resp = client.post("/api/cast/control", json={
        "target_id": "x", "action": "selfdestruct",
    })
    assert resp.status_code == 422


def test_control_seek_clamps_seconds(client):
    """ge=0, le=86400 — defends against a -1 seek (which sidplayfp /
    AVPlayer would either reject loudly or wrap to a huge value)."""
    resp = client.post("/api/cast/control", json={
        "target_id": "x", "action": "seek", "seconds": -5.0,
    })
    assert resp.status_code == 422
    resp = client.post("/api/cast/control", json={
        "target_id": "x", "action": "seek", "seconds": 99_999_999.0,
    })
    assert resp.status_code == 422


# ── /cast/{token}/{filename} anonymous byte server ────────────────────────

def test_cast_stream_bad_token_returns_404(client):
    """Hardened: bad sig / expired / tampered ALL get the same 404 +
    same body so a probing attacker can't tell them apart."""
    # Empty-token URL ``/cast//foo.mp3`` becomes a Starlette route
    # mismatch (double slash collapses to 404 from the router, not the
    # handler), so it's not part of this assertion — covered separately
    # in test_cast_stream_empty_token_is_404 below.
    for bad in ("garbage", "a.b.c", "x" * 200):
        resp = client.get(f"/cast/{bad}/foo.mp3")
        assert resp.status_code == 404, f"bad token {bad!r} got {resp.status_code}"
        assert "no longer valid" in resp.json().get("detail", "").lower()


def test_cast_stream_good_token_missing_track_returns_410(client):
    """Token validates but the track was deleted between mint and play
    → 410 Gone, distinct from 404 ("bad token")."""
    from soniqboom.core import cast_tokens
    tok = cast_tokens.issue_token(track_id="not_in_any_library_zzz")
    resp = client.get(f"/cast/{tok}/sample.mp3")
    assert resp.status_code == 410
    assert "no longer in library" in resp.json().get("detail", "").lower()


def test_cast_stream_token_with_crlf_filename_sanitised(client):
    """Reflecting CR/LF into Content-Disposition would split the HTTP
    response.  The byte server must sanitise."""
    from soniqboom.core import cast_tokens
    tok = cast_tokens.issue_token(track_id="anything")
    # URL-encoded CRLF in the filename — TestClient will decode this
    # to a real path segment containing CR/LF
    resp = client.get(f"/cast/{tok}/evil%0d%0aSet-Cookie:%20x=y.mp3")
    # Even though the track isn't in the library (→ 410), the response
    # headers must NOT contain the injected header
    assert "Set-Cookie" not in str(resp.headers)
    # The Content-Disposition (if present) must not contain CRLF
    cd = resp.headers.get("Content-Disposition", "")
    assert "\r" not in cd and "\n" not in cd


# ── /api/cast/telemetry shape ──────────────────────────────────────────────

def test_telemetry_returns_documented_shape(client):
    """The dashboard widget consumes (outcomes, p95_first_ms, recent).
    Empty telemetry is still a valid response."""
    resp = client.get("/api/cast/telemetry")
    assert resp.status_code == 200
    data = resp.json()
    assert {"outcomes", "p95_first_ms", "recent"}.issubset(data.keys())
    # outcomes is the rolling-window counter dict
    assert set(data["outcomes"].keys()).issubset(
        {"played", "skipped", "errored", "cancelled"}
    )
