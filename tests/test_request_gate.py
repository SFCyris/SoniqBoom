# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The request pipeline in main.py: the one pure-ASGI request gate (session
gate + Cache-Control policy + deadlock-watchdog registration — formerly three
``@app.middleware("http")`` functions, kept below verbatim as the reference),
the auth dependencies, and the gzip of big bodies off the event loop."""
from __future__ import annotations

import asyncio
import gzip
import threading

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from soniqboom import main
from soniqboom.core import deadlock_watchdog, users as core_users


# ── The three middlewares this gate replaced (verbatim, the reference) ──────

def _install_reference(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_auth_on_api(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
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
                "/api/auth/logout",
                "/api/docs",
                "/api/openapi.json",
            }
            or path.startswith("/api/docs/")
        ):
            return await call_next(request)
        if path.endswith("/ws"):
            return await call_next(request)
        if path.startswith(("/api/stream/", "/api/rest/")) or path == "/api/stream":
            return await call_next(request)
        try:
            from soniqboom.core.users import get_user_store
            store = get_user_store()
        except Exception:
            return await call_next(request)
        if not store.has_any():
            return await call_next(request)
        cookie = request.cookies.get("sb_session")
        if cookie and store.lookup_session(cookie):
            return await call_next(request)
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Sign in to access this endpoint."}, status_code=401)

    @app.middleware("http")
    async def no_cache_api(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if "cache-control" in {k.lower() for k in response.headers.keys()}:
            return response
        if path.startswith("/api/stream"):
            return response
        if path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def watchdog_track(request: Request, call_next):
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


def _routes(app: FastAPI) -> None:
    @app.get("/api/health")
    async def _health():
        return {"ok": 1}

    @app.get("/api/private")
    async def _private():
        return {"secret": 1}

    @app.get("/api/cc")
    async def _cc():
        return Response(b"x", headers={"Cache-Control": "private, max-age=5"})

    @app.get("/api/pragma")
    async def _pragma():
        return Response(b"x", headers={"Pragma": "keep", "X-A": "1"})

    @app.get("/api/stream/{tid}")
    async def _stream(tid: str):
        return Response(b"\0" * 64, media_type="audio/wav")

    @app.get("/api/stream")
    async def _stream_root():
        return {"s": 1}

    @app.get("/api/library/ws")
    async def _ws_http():
        return {"ws": 1}

    @app.get("/api/docs/x")
    async def _docs():
        return {"docs": 1}

    @app.get("/apix")
    async def _apix():
        return {"apix": 1}

    @app.get("/static/app.js")
    async def _static():
        return Response("js", media_type="text/javascript")

    @app.get("/rest/ping")
    async def _rest():
        return {"rest": 1}

    @app.get("/api/err")
    async def _err():
        raise RuntimeError("boom")

    @app.get("/{rest:path}")
    async def _spa(rest: str):
        return {"spa": rest}


class _Users:
    def __init__(self, n_users: int, sessions=("good",)) -> None:
        self.n = n_users
        self.sessions = set(sessions)

    def has_any(self) -> bool:
        return self.n > 0

    def lookup_session(self, tok):
        return object() if tok in self.sessions else None


_STORES = {
    "uninitialised": None,
    "no users": _Users(0),
    "users": _Users(2),
}
# Every path the old middlewares judged the way the gate does.  (The two
# deliberate differences — paths whose URL re-parse changed them, and the "/ws"
# suffix exemption — have their own tests below.)
_PATHS = ["/api/health", "/api/private", "/api/cc", "/api/pragma", "/api/stream/t1", "/api/stream",
          "/api/streamx", "/api/docs/x", "/apix", "/static/app.js", "/rest/ping",
          "/api/err", "/api/auth/me", "/", "/api/"]
_COOKIES = [
    [],
    [("cookie", "sb_session=good")],
    [("cookie", "sb_session=bad")],
    [("cookie", "a=1; sb_session=good"), ("cookie", "sb_session=bad")],
    [("cookie", "sb_session=bad"), ("cookie", "x=2; sb_session=good")],
    [("cookie", 'sb_session="good"')],
    [("cookie", "sb_session=")],
]


def _apps():
    ref = FastAPI()
    _routes(ref)
    _install_reference(ref)
    new = FastAPI()
    _routes(new)
    new.add_middleware(main._RequestGateMiddleware)
    return ref, new


@pytest.mark.parametrize("store_state", list(_STORES))
def test_the_gate_answers_exactly_as_the_three_middlewares_did(store_state, monkeypatch):
    """Every path × cookie set × user-store state: the same status, body and
    header list (order included), and the same watchdog registrations."""
    store = _STORES[store_state]

    def get_store():
        if store is None:
            raise RuntimeError("UserStore not initialised")
        return store
    monkeypatch.setattr(core_users, "get_user_store", get_store)
    seen: list = []
    monkeypatch.setattr(deadlock_watchdog, "begin_request",
                        lambda m, p, client="": seen.append(("begin", m, p, client)) or len(seen))
    monkeypatch.setattr(deadlock_watchdog, "end_request", lambda tok: seen.append(("end", tok)))
    ref, new = _apps()
    results = {}
    for name, app in (("ref", ref), ("new", new)):
        seen.clear()
        out = []
        with TestClient(app, raise_server_exceptions=False) as c:
            for path in _PATHS:
                for cookies in _COOKIES:
                    for method in ("GET", "HEAD"):
                        r = c.request(method, path, headers=cookies)
                        out.append((method, path, tuple(cookies), r.status_code, r.content,
                                    tuple(r.headers.raw)))
        results[name] = (out, list(seen))
    ref_out, ref_seen = results["ref"]
    new_out, new_seen = results["new"]
    for a, b in zip(ref_out, new_out):
        assert a == b
    assert len(ref_out) == len(new_out)
    assert new_seen == ref_seen
    if store_state == "users":                       # the gate does refuse
        assert any(r[3] == 401 for r in new_out) and any(r[3] == 200 and r[1] == "/api/private" for r in new_out)


@pytest.mark.parametrize("host", [b"testserver", b"x/api/health#", b"x/api/health?",
                                  b"x/api/auth/login#", b"x/api/docs/#"])
@pytest.mark.parametrize("path", ["/api/private", "/api/cast/position/Living Room",
                                  "/api/health%3Fx", "/api/private%23frag", "/api/priv%09ate"])
def test_the_host_header_cannot_open_the_gate(host, path, monkeypatch):
    """The old middlewares judged ``request.url.path`` — Starlette rebuilds
    it from the Host HEADER and the path and parses it again, so ``Host:
    x/api/health#`` made ANY request look like the public /api/health while
    the router served the real path: a sign-in bypass for every /api
    endpoint (shown here against the reference).  The gate judges the scope's
    own path — the one the router matches."""
    monkeypatch.setattr(core_users, "get_user_store", lambda: _Users(2))
    ref, new = _apps()

    @new.get("/api/cast/position/{tid}")
    async def _pos(tid: str):
        return {"secret_for": tid}

    @ref.get("/api/cast/position/{tid}")
    async def _pos_ref(tid: str):
        return {"secret_for": tid}
    hdr = {"host": host.decode()}
    r = TestClient(new, raise_server_exceptions=False).get(path, headers=hdr)
    assert r.status_code == 401, (path, host, r.status_code, r.text)
    if host != b"testserver" and path in ("/api/private", "/api/cast/position/Living Room"):
        old = TestClient(ref, raise_server_exceptions=False).get(path, headers=hdr)
        assert old.status_code == 200                    # the bypass the gate closes
    good = TestClient(new).get(path, headers={**hdr, "cookie": "sb_session=good"})
    assert good.status_code != 401


def test_a_path_ending_in_ws_is_not_public_over_http(monkeypatch):
    """Websockets never reach the gate (their scope passes through untouched);
    the old "/ws" suffix exemption only opened HTTP routes whose path ended
    in it — a path parameter included."""
    monkeypatch.setattr(core_users, "get_user_store", lambda: _Users(2))
    _ref, new = _apps()                                 # its catch-all route takes any path
    c = TestClient(new)
    assert c.get("/api/library/ws").status_code == 401
    assert c.get("/api/relay/x/ws").status_code == 401
    assert c.get("/api/relay/x/ws", headers={"cookie": "sb_session=good"}).json() == {"spa": "api/relay/x/ws"}


def test_a_streamed_body_is_not_buffered_and_the_watchdog_ends_at_its_start(monkeypatch):
    """The body goes out chunk by chunk (the first chunk reaches the client
    before the second exists); the watchdog entry ends when the response
    starts, not when a long stream ends — as with the old ``call_next``."""
    monkeypatch.setattr(core_users, "get_user_store", lambda: _Users(0))
    inflight = {}
    monkeypatch.setattr(deadlock_watchdog, "begin_request",
                        lambda m, p, client="": inflight.setdefault(p, 1) and p)
    monkeypatch.setattr(deadlock_watchdog, "end_request", lambda tok: inflight.pop(tok, None))
    app = FastAPI()
    gate = asyncio.Event()

    @app.get("/api/sse")
    async def _sse():
        async def gen():
            yield b"first"
            await gate.wait()
            yield b"second"
        return StreamingResponse(gen(), media_type="text/event-stream")

    wrapped = main._RequestGateMiddleware(app)
    events = []

    async def run():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/api/sse", "raw_path": b"/api/sse", "query_string": b"",
                 "root_path": "", "headers": [(b"host", b"t")], "client": ("1.2.3.4", 5), "server": ("t", 80)}

        async def receive():
            await asyncio.sleep(3600)

        async def send(m):
            events.append((m["type"], m.get("body"), dict(inflight)))
            if m.get("body") == b"first":
                gate.set()
        await asyncio.wait_for(wrapped(scope, receive, send), 5)
    asyncio.run(run())
    start = events[0]
    assert start[0] == "http.response.start" and start[2] == {}          # ended at the start
    bodies = [e[1] for e in events if e[0] == "http.response.body" and e[1]]
    assert bodies == [b"first", b"second"]


def test_websockets_and_lifespan_pass_straight_through():
    calls = []

    async def inner(scope, receive, send):
        calls.append((scope["type"], send))

    gate = main._RequestGateMiddleware(inner)

    async def send(m):
        pass
    for typ in ("websocket", "lifespan"):
        asyncio.run(gate({"type": typ, "path": "/api/library/ws"}, None, send))
    assert [c[0] for c in calls] == ["websocket", "lifespan"]
    assert all(c[1] is send for c in calls)                              # the very same send


def test_the_watchdog_entry_ends_when_the_handler_raises(monkeypatch):
    monkeypatch.setattr(core_users, "get_user_store", lambda: _Users(0))
    log = []
    monkeypatch.setattr(deadlock_watchdog, "begin_request", lambda m, p, client="": log.append("b") or 7)
    monkeypatch.setattr(deadlock_watchdog, "end_request", lambda tok: log.append(("e", tok)))

    async def boom(scope, receive, send):
        raise RuntimeError("x")
    gate = main._RequestGateMiddleware(boom)
    with pytest.raises(RuntimeError):
        asyncio.run(gate({"type": "http", "path": "/api/x", "method": "GET", "headers": []}, None, None))
    assert log == ["b", ("e", 7)]


def test_the_real_app_stack_keeps_its_order():
    """The gate sits where the three middlewares sat: inside the Subsonic
    CORS layer, outside CORS and GZip — and nothing BaseHTTPMiddleware is
    left on the request path."""
    from starlette.middleware.base import BaseHTTPMiddleware
    names = [m.cls.__name__ for m in main.app.user_middleware]
    assert names == ["_SubsonicCORSMiddleware", "_RequestGateMiddleware",
                     "CORSMiddleware", "_SelectiveGZipMiddleware"]
    assert not any(m.cls is BaseHTTPMiddleware for m in main.app.user_middleware)


# ── Auth dependencies on the loop ───────────────────────────────────────────

def test_the_auth_dependencies_run_on_the_event_loop():
    """No thread-pool hop: they are coroutines, FastAPI awaits them."""
    import inspect
    from soniqboom.api import users as users_api
    for dep in (users_api.current_user, users_api.require_user, users_api.require_admin,
                users_api.require_edit, users_api.require_role("admin")):
        assert inspect.iscoroutinefunction(dep), dep


def test_the_auth_dependencies_still_gate(monkeypatch):
    from fastapi import Depends
    from soniqboom.api import users as users_api

    class _U:
        def __init__(self, role):
            self.role = role

    class _S:
        def lookup_session(self, tok):
            return {"a": _U("admin"), "r": _U("readonly")}.get(tok)
    monkeypatch.setattr(users_api, "get_user_store", lambda: _S())
    app = FastAPI()

    @app.get("/me")
    async def me(u=Depends(users_api.current_user)):
        return {"role": getattr(u, "role", None)}

    @app.get("/edit")
    async def edit(u=Depends(users_api.require_edit)):
        return {"ok": 1}

    @app.get("/admin")
    async def admin(u=Depends(users_api.require_admin)):
        return {"ok": 1}

    c = TestClient(app)
    assert c.get("/me").json() == {"role": None}
    assert c.get("/me", cookies={"sb_session": "r"}).json() == {"role": "readonly"}
    assert c.get("/edit").status_code == 401
    assert c.get("/edit", cookies={"sb_session": "r"}).status_code == 403
    assert c.get("/edit", cookies={"sb_session": "a"}).status_code == 200
    assert c.get("/admin", cookies={"sb_session": "r"}).status_code == 403
    assert c.get("/admin", cookies={"sb_session": "a"}).status_code == 200


# ── GZip off the loop ───────────────────────────────────────────────────────

_LOOP_THREADS: list = []


def _gzip_app():
    app = FastAPI()
    big = ("{\"k\": \"" + "abc" * 40_000 + "\"}").encode()           # ~120 KB
    small = b"{\"k\": \"" + b"x" * 2000 + b"\"}"

    @app.get("/api/big")
    async def _big():
        _LOOP_THREADS.append(threading.get_ident())
        return Response(big, media_type="application/json")

    @app.get("/api/small")
    async def _small():
        return Response(small, media_type="application/json")

    pre = gzip.compress(big)

    @app.get("/api/pre")
    async def _pre():
        return Response(pre, media_type="application/json",
                        headers={"Content-Encoding": "gzip"})

    @app.get("/api/chunks")
    async def _chunks():
        async def gen():
            for _ in range(3):
                yield big
        return StreamingResponse(gen(), media_type="application/json")

    @app.get("/api/sse")
    async def _sse():
        async def gen():
            yield big
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/stream/x")
    async def _audio():
        return Response(big, media_type="audio/wav")
    return app, big, small


def test_a_big_body_is_gzipped_in_a_worker_thread(monkeypatch):
    app, big, small = _gzip_app()
    threads = []
    real = gzip.compress

    def spy(data, *a, **k):
        threads.append(threading.get_ident())
        return real(data, *a, **k)
    monkeypatch.setattr(main.gzip, "compress", spy)
    _LOOP_THREADS.clear()
    c = TestClient(main._SelectiveGZipMiddleware(app, minimum_size=1000))
    gz = {"Accept-Encoding": "gzip"}
    r = c.get("/api/big", headers=gz)
    assert r.headers["content-encoding"] == "gzip" and r.content == big
    assert "Accept-Encoding" in r.headers["vary"]
    assert int(r.headers["content-length"]) == r.num_bytes_downloaded < len(big)
    # compressed once, in a thread other than the event loop's (the handler's)
    assert len(threads) == 1 and threads[0] != _LOOP_THREADS[0]
    # small bodies: inline compression as before (gzip.compress not called)
    r = c.get("/api/small", headers=gz)
    assert r.headers["content-encoding"] == "gzip" and r.content == small and len(threads) == 1
    # pre-encoded bodies pass untouched; event streams and media never gzipped
    r = c.get("/api/pre", headers=gz)
    assert r.headers["content-encoding"] == "gzip" and r.content == big and len(threads) == 1
    assert "content-encoding" not in c.get("/api/sse", headers=gz).headers
    assert "content-encoding" not in c.get("/api/stream/x", headers=gz).headers
    # streamed chunks: compressed inline, chunk by chunk (unchanged)
    r = c.get("/api/chunks", headers=gz)
    assert r.headers["content-encoding"] == "gzip" and r.content == big * 3 and len(threads) == 1
    # no gzip asked → none given
    r = c.get("/api/big", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers and r.content == big


def test_the_big_body_gzip_does_not_hold_the_event_loop():
    """While a big body deflates, the loop keeps running other work."""
    app, big, _small = _gzip_app()
    huge = big * 60                                                      # ~7 MB

    @app.get("/api/huge")
    async def _huge():
        return Response(huge, media_type="application/json")
    mw = main._SelectiveGZipMiddleware(app, minimum_size=1000)

    async def run():
        ticks = 0
        done = False

        async def ticker():
            nonlocal ticks
            while not done:
                await asyncio.sleep(0)
                ticks += 1
        t = asyncio.create_task(ticker())
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/api/huge", "raw_path": b"/api/huge", "query_string": b"",
                 "root_path": "", "headers": [(b"host", b"t"), (b"accept-encoding", b"gzip")],
                 "client": ("1.2.3.4", 5), "server": ("t", 80)}
        await mw(scope, receive, send)
        done = True
        await t
        return ticks, sent
    ticks, sent = asyncio.run(run())
    assert gzip.decompress(sent[1]["body"]) == huge
    assert ticks > 10, ticks                        # the loop ran while it deflated


def test_a_ranged_request_is_never_gzipped(tmp_path):
    """A Range request anywhere (a static asset, the manual) gets the bytes it
    asked for — a gzipped 206 would carry a Content-Range of the uncompressed
    bytes over a body that isn't them."""
    from fastapi.responses import FileResponse
    f = tmp_path / "app.js"
    f.write_text("x" * 100_000)
    app = FastAPI()

    @app.get("/assets/app.js")
    async def _js():
        return FileResponse(f, media_type="text/javascript")
    c = TestClient(main._SelectiveGZipMiddleware(app, minimum_size=1000))
    r = c.get("/assets/app.js", headers={"Accept-Encoding": "gzip", "Range": "bytes=0-99"})
    assert r.status_code == 206 and "content-encoding" not in r.headers and r.content == b"x" * 100
    r = c.get("/assets/app.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
