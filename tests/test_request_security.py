# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The request gate's cross-origin rules (CSRF, WebSocket origin, CORS for the
listed origins), request-body ceilings, security and cache headers, and what
anonymous callers may learn (API reference, UI config, health).  In-process
(``TestClient`` without the lifespan) — no server."""
from __future__ import annotations

import asyncio
import gzip
import io
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from soniqboom import main
from soniqboom.core import startup_status
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

PW = "Old-pass-123!"
HOST = "127.0.0.1:8471"
SELF = f"http://{HOST}"
OTHER_PORT = "http://127.0.0.1:8478"          # same site, another origin


@pytest.fixture
def env(tmp_data_dir, monkeypatch):
    st = UserStore(tmp_data_dir, persist_sessions=True)
    monkeypatch.setattr(users_mod, "_instance", st)
    admin = st.create("admin", PW, "admin")

    class E:
        store = st
        data_dir = tmp_data_dir

        @staticmethod
        def client(cookie=None, peer=("127.0.0.1", 50123)):
            c = TestClient(main.app, base_url=SELF, client=peer)
            if cookie:
                c.cookies.set("sb_session", cookie)
            return c
    E.admin = admin
    E.tok = st.issue_session(admin.id)[0]
    return E


def _alive(env) -> bool:
    return env.store.lookup_session(env.tok) is not None


# ── Finding 1: same-site cross-origin requests and WebSockets ───────────────

@pytest.mark.parametrize("headers", [
    {"origin": OTHER_PORT},
    {"origin": "http://localhost:8471"},
    {"origin": "null"},
    {"sec-fetch-site": "same-site"},
    {"sec-fetch-site": "cross-site"},
    {"referer": OTHER_PORT + "/attack.html"},
    {"origin": OTHER_PORT, "sec-fetch-site": "same-site"},
])
def test_a_foreign_origin_cannot_sign_the_admin_out(env, headers):
    r = env.client(env.tok).post("/api/auth/logout", headers=headers)
    assert r.status_code == 403
    assert _alive(env)


def test_a_foreign_origin_multipart_import_is_refused_before_the_handler(env, monkeypatch):
    ran = []
    from soniqboom.api import admin as admin_api
    monkeypatch.setattr(admin_api, "populate_store", lambda *a: ran.append(a), raising=False)
    doc = gzip.compress(json.dumps({"version": 2, "data": {"tracks": {}}}).encode())
    r = env.client(env.tok).post("/api/admin/import", headers={"origin": OTHER_PORT},
                                 files={"file": ("x.sbz", io.BytesIO(doc), "application/octet-stream")})
    assert r.status_code == 403 and not ran


@pytest.mark.parametrize("headers", [
    {},                                                   # curl / setadm / an app
    {"origin": SELF},
    {"origin": SELF, "sec-fetch-site": "same-origin"},
    {"sec-fetch-site": "same-origin"},
    {"sec-fetch-site": "none"},
    {"referer": SELF + "/"},
    # behind a reverse proxy that rewrites Host but sends X-Forwarded-Host
    {"origin": "https://music.example.com", "x-forwarded-host": "music.example.com"},
    # a proxy that sets neither: the browser's own word
    {"origin": "https://music.example.com", "sec-fetch-site": "same-origin"},
])
def test_same_origin_and_non_browser_requests_pass(env, headers):
    r = env.client(env.tok).post("/api/auth/logout", headers=headers)
    assert r.status_code == 200
    assert not _alive(env)


def test_a_listed_cors_origin_may_post(env, monkeypatch):
    monkeypatch.setattr(main, "_CORS_ORIGINS", frozenset({"https://app.example"}))
    r = env.client(env.tok).post("/api/auth/logout", headers={"origin": "https://app.example"})
    assert r.status_code == 200


def test_safe_methods_are_not_origin_checked(env):
    r = env.client(env.tok).get("/api/auth/me", headers={"origin": OTHER_PORT})
    assert r.status_code == 200


# (TestClient's WebSocket requests go to ws://testserver: that is the Host)
WS_SELF = "http://testserver"


@pytest.mark.parametrize("path", ["/api/library/ws", "/api/multiroom/ws"])
@pytest.mark.parametrize("origin", [OTHER_PORT, "http://localhost", "null"])
def test_a_foreign_origin_websocket_is_refused(env, path, origin):
    c = env.client()
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(path, headers={"origin": origin,
                                                "cookie": f"sb_session={env.tok}"}):
            pass                                # (refused: the handshake itself raises)
    assert e.value.code == 1008


@pytest.mark.parametrize("headers", [{"origin": WS_SELF}, {}])
def test_a_same_origin_or_non_browser_library_websocket_is_accepted(env, headers):
    c = env.client()
    with c.websocket_connect("/api/library/ws",
                             headers={**headers, "cookie": f"sb_session={env.tok}"}) as ws:
        first = json.loads(ws.receive_text())
    assert first.get("event") == "scan_progress"


# ── Finding 17: CORS for SONIQBOOM_CORS_ORIGINS, signed in ──────────────────

def test_listed_origin_preflight_and_signed_in_call(env, monkeypatch):
    monkeypatch.setattr(main, "_CORS_ORIGINS", frozenset({"https://app.example"}))
    c = env.client(env.tok)
    pre = c.options("/api/auth/me", headers={
        "origin": "https://app.example", "access-control-request-method": "GET",
        "access-control-request-headers": "content-type"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://app.example"
    assert pre.headers["access-control-allow-credentials"] == "true"
    assert pre.headers["access-control-allow-headers"] == "content-type"
    r = c.get("/api/auth/me", headers={"origin": "https://app.example"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://app.example"
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in r.headers.get("vary", "")


@pytest.mark.parametrize("origin", ["http://localhost:9999", OTHER_PORT, "https://evil.example"])
def test_unlisted_origins_get_no_cors_headers(env, monkeypatch, origin):
    monkeypatch.setattr(main, "_CORS_ORIGINS", frozenset({"https://app.example"}))
    c = env.client(env.tok)
    pre = c.options("/api/auth/me", headers={"origin": origin,
                                              "access-control-request-method": "GET"})
    assert "access-control-allow-origin" not in pre.headers
    r = c.get("/api/auth/me", headers={"origin": origin})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


# ── Finding 21: security headers; request-body ceilings ─────────────────────

@pytest.mark.parametrize("path", ["/", "/api/health", "/api/auth/status", "/m"])
def test_security_headers(env, path):
    r = env.client().get(path)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_no_nosniff_on_the_subsonic_byte_server(env):
    r = env.client().get("/rest/ping.view")
    assert "x-content-type-options" not in r.headers


def test_an_oversized_sign_in_body_is_refused_up_front(env):
    r = env.client().post("/api/auth/login", content=b"{" + b" " * 200_000 + b"}",
                          headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_an_oversized_chunked_body_is_refused_while_read(env):
    def chunks():
        yield b'{"username": "admin", "password": "'
        for _ in range(100):
            yield b"x" * 4096
        yield b'"}'
    r = env.client().post("/api/auth/login", content=chunks(),
                          headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_general_api_bodies_have_a_ceiling_but_uploads_a_bigger_one(env):
    big = b"x" * (main._BODY_MAX_DEFAULT + 1)
    r = env.client(env.tok).put("/api/me/play-queue", content=big,
                                headers={"content-type": "application/json"})
    assert r.status_code == 413
    r = env.client(env.tok).post("/api/admin/import",
                                 files={"file": ("x.sbz", io.BytesIO(big), "application/octet-stream")})
    assert r.status_code == 400                         # read in full, then "Invalid .sbz"


def test_the_receive_wrapper_stops_at_the_limit():
    msgs = [{"type": "http.request", "body": b"a" * 10, "more_body": True}] * 5

    async def run():
        it = iter(msgs)

        async def receive():
            return next(it)
        recv = main._limited_receive(receive, 25)
        await recv()
        await recv()
        with pytest.raises(HTTPException) as e:
            await recv()
        assert e.value.status_code == 413
    asyncio.run(run())


# ── Finding 11: what an anonymous caller learns ─────────────────────────────

@pytest.mark.parametrize("path", ["/api/openapi.json", "/api/docs"])
def test_the_api_reference_needs_a_sign_in(env, path):
    assert env.client().get(path).status_code == 401
    assert env.client(env.tok).get(path).status_code == 200


def test_no_schema_outside_api(env):
    r = env.client().get("/openapi.json")
    assert '"openapi"' not in r.text


def test_ui_config_hides_folder_aliases_from_anonymous_callers(env, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "folder_aliases", {"/Volumes/Secret/Music": "Music"})
    anon = env.client().get("/api/ui-config").json()
    assert anon["folder_aliases"] == {} and anon["catalogue_roots"] == []
    assert "/Volumes/Secret" not in json.dumps(anon)
    me = env.client(env.tok).get("/api/ui-config").json()
    assert me["folder_aliases"] == {"/Volumes/Secret/Music": "Music"}


def test_health_version_only_for_the_signed_in_and_the_servers_own_host(env):
    from soniqboom import __version__
    lan = ("192.168.1.50", 40000)
    assert env.client(peer=lan).get("/api/health").json()["version"] is None
    assert env.client(env.tok, peer=lan).get("/api/health").json()["version"] == __version__
    assert env.client().get("/api/health").json()["version"] == __version__      # loopback
    proxied = env.client().get("/api/health", headers={"x-forwarded-for": "203.0.113.9"})
    assert proxied.json()["version"] is None
    assert env.client(peer=lan).get("/api/health").json()["status"] == "ok"


# ── Finding 15: the re-check button never moves a half-copied file aside ────

def test_anonymous_recheck_leaves_a_half_copied_users_json_alone(tmp_data_dir, monkeypatch):
    st = UserStore(tmp_data_dir)
    st.create("ed", PW, "edit")                         # accounts, none an admin
    monkeypatch.setattr(users_mod, "_instance", st)
    half = b'{"version": 1, "users": [{"id": "x", "user'
    (tmp_data_dir / "users.json").write_bytes(half)
    r = TestClient(main.app, client=("192.168.1.50", 5000)).post("/api/auth/reload")
    assert r.status_code == 200
    assert (tmp_data_dir / "users.json").read_bytes() == half
    assert not any(".corrupt-" in q.name for q in tmp_data_dir.iterdir())
    assert st.get_by_username("ed") is not None


# ── Finding 19: no probing of directories outside the scan roots ────────────

def test_fstree_refresh_answers_alike_for_any_path_outside_the_roots(tmp_path, monkeypatch):
    from soniqboom.api import fstree
    from soniqboom.core import store as store_mod
    root = tmp_path / "music"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()

    class FakeStore:
        def list_scan_dirs(self):
            return [{"path": str(root)}]
    monkeypatch.setattr(store_mod, "get_store", lambda: FakeStore())

    def answer(path):
        try:
            asyncio.run(fstree.refresh_subtree({"path": path}))
        except HTTPException as e:
            return e.status_code, e.detail
        return 200, None
    exists = answer(str(outside))
    missing = answer(str(tmp_path / "no-such-dir"))
    assert exists == missing and exists[0] == 403
    assert answer(str(root / "nope"))[0] == 404          # inside a root: as before


# ── Finding 20: signed-in media is never cacheable by a shared cache ────────

@pytest.mark.parametrize("path,given,want", [
    ("/api/art/abc/300.jpg", b"public, max-age=31536000, immutable",
     "private, max-age=31536000, immutable"),
    ("/api/stations/x/logo", b"public, max-age=86400", "private, max-age=86400"),
    ("/api/stream/abc", None, "private"),
    ("/api/library/counts", b"private, max-age=0, must-revalidate",
     "private, max-age=0, must-revalidate"),
])
def test_cache_control_of_signed_in_responses(path, given, want):
    headers = [(b"content-type", b"image/jpeg")]
    if given is not None:
        headers.append((b"cache-control", given))
    out = main._add_cache_headers({"type": "http.response.start", "status": 200,
                                   "headers": headers}, path)
    got = [v.decode() for k, v in out["headers"] if k.lower() == b"cache-control"]
    assert got == [want]


def test_subsonic_placeholder_art_stays_public():
    out = main._add_cache_headers({"type": "http.response.start", "status": 200,
                                   "headers": [(b"cache-control", b"public, max-age=86400")]},
                                  "/rest/getCoverArt.view")
    assert [v for k, v in out["headers"] if k == b"cache-control"] == [b"public, max-age=86400"]
