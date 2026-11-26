# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``POST /api/auth/reload`` closes every open WebSocket whose account is
gone, disabled, demoted or has another password since the socket opened —
also when a server save merged setadm's change in before its knock.  The "CLI"
here is a second ``UserStore`` on the same data dir (what soniqboom-setadm
writes with); the server is ``TestClient`` with a chosen peer, no lifespan —
no setadm or server process runs."""
from __future__ import annotations

import asyncio
import time

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from soniqboom import main
from soniqboom.api import users as api_users
from soniqboom.core import startup_status
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

_TOK = "the-run-token-0123456789abcdefghij"
_HDR = startup_status.RELOAD_TOKEN_HEADER
PW = "Longenough-1"
NEW = "Longenough-2"


def _receive_within(ws, seconds: float) -> dict:
    """The next message the app sent on test-client socket ``ws``, or
    ``TimeoutError`` after ``seconds`` (starlette's ``WebSocketTestSession``
    internals: its portal and send stream)."""
    async def rx():
        with anyio.fail_after(seconds):
            return await ws._send_rx.receive()
    return ws.portal.call(rx)


def _next_close(ws, seconds: float) -> dict:
    """Read ``ws`` until the app's close frame (``TimeoutError`` after
    ``seconds``); event frames before it are skipped."""
    deadline = time.monotonic() + seconds
    while True:
        msg = _receive_within(ws, max(0.05, deadline - time.monotonic()))
        if msg["type"] == "websocket.close":
            return msg


class FakeWS:
    def __init__(self):
        self.codes: list[int] = []

    async def close(self, code=1000):
        self.codes.append(code)


@pytest.fixture
def srv(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(startup_status, "_status",
                        {**startup_status._status, "reload_token": _TOK})
    monkeypatch.setattr(api_users, "_open_ws_by_user", {})
    monkeypatch.setattr(api_users, "_ws_admitted", {})

    class S:
        data_dir = tmp_data_dir
        store = None
        ids: dict[str, str] = {}

        @staticmethod
        def start(*accounts):
            """``accounts``: ``(name, role)`` — written to disk, then the
            server's store loads them; each gets one open (fake) socket."""
            w = UserStore(tmp_data_dir)
            for name, role in accounts:
                S.ids[name] = w.create(name, PW, role).id
            S.store = UserStore(tmp_data_dir, persist_sessions=True)
            monkeypatch.setattr(users_mod, "_instance", S.store)
            socks = {}
            for name in S.ids:
                socks[name] = FakeWS()
                api_users.register_open_ws(S.ids[name], socks[name])
            return socks

        @staticmethod
        def only(name, ws):
            """``ws`` becomes ``name``'s one registered socket."""
            uid = S.ids[name]
            for old in list(api_users._open_ws_by_user.get(uid, ())):
                api_users.unregister_open_ws(uid, old)
            api_users.register_open_ws(uid, ws)

        @staticmethod
        def cli():
            return UserStore(tmp_data_dir)        # setadm's own store

        @staticmethod
        def knock(token=_TOK, peer="127.0.0.1"):
            c = TestClient(main.app, client=(peer, 50123))
            hdr = {_HDR: token} if token is not None else {}
            return c.post("/api/auth/reload", headers=hdr)
    return S


@pytest.mark.parametrize("change", ["disable", "password", "demote", "demote_admin",
                                    "delete"])
def test_a_cli_change_that_revokes_closes_only_that_accounts_sockets(srv, change):
    socks = srv.start(("root", "admin"), ("bob", "admin" if change == "demote_admin" else "edit"),
                      ("carol", "edit"))
    cli, bob = srv.cli(), srv.ids["bob"]
    if change == "disable":
        cli.update(bob, enabled=False)
    elif change == "password":
        cli.set_password(bob, NEW)
    elif change == "demote":
        cli.update(bob, role="readonly")
    elif change == "demote_admin":
        cli.update(bob, role="edit")
    else:
        cli.delete(bob)
    r = srv.knock()
    assert r.status_code == 200, r.text
    assert socks["bob"].codes == [4401]
    assert socks["root"].codes == [] and socks["carol"].codes == []
    assert srv.ids["bob"] not in api_users._open_ws_by_user
    assert srv.ids["root"] in api_users._open_ws_by_user


@pytest.mark.parametrize("change", ["promote", "display_name", "nothing", "new_account"])
def test_a_cli_change_that_revokes_nothing_keeps_every_socket(srv, change):
    socks = srv.start(("root", "admin"), ("bob", "readonly"))
    cli, bob = srv.cli(), srv.ids["bob"]
    if change == "promote":
        cli.update(bob, role="edit")
    elif change == "display_name":
        cli.update(bob, display_name="Bobby")
    elif change == "new_account":
        cli.create("dave", PW, "readonly")
    assert srv.knock().status_code == 200
    assert socks["bob"].codes == [] and socks["root"].codes == []
    if change == "promote":
        assert srv.store.get(bob).role == "edit"          # (the reload did happen)


def test_a_foreign_token_reloads_nothing_and_closes_nothing(srv):
    socks = srv.start(("root", "admin"), ("bob", "edit"))
    srv.cli().update(srv.ids["bob"], enabled=False)
    assert srv.knock(token="another-data-dir-token-0123456789").status_code == 409
    assert socks["bob"].codes == []
    assert srv.store.get(srv.ids["bob"]).enabled is True


def test_the_recheck_button_reload_closes_sockets_too(srv):
    # No enabled admin is loaded: the login overlay's re-check (no token, any
    # address) adopts users.json — a disable it brings in closes that socket.
    socks = srv.start(("ed", "edit"), ("bob", "readonly"))
    srv.cli().update(srv.ids["bob"], enabled=False)
    r = srv.knock(token=None, peer="192.168.1.50")
    assert r.status_code == 200, r.text
    assert socks["bob"].codes == [4401] and socks["ed"].codes == []


def test_a_socket_that_fails_to_close_does_not_fail_the_reload(srv):
    srv.start(("root", "admin"), ("bob", "edit"), ("carol", "edit"))

    class Broken:
        async def close(self, code=1000):
            raise RuntimeError("already gone")
    srv.only("bob", Broken())
    carol = FakeWS()
    srv.only("carol", carol)
    cli = srv.cli()
    cli.set_password(srv.ids["bob"], NEW)
    cli.update(srv.ids["carol"], enabled=False)
    assert srv.knock().status_code == 200
    assert carol.codes == [4401]
    assert srv.ids["bob"] not in api_users._open_ws_by_user     # (dropped all the same)
    assert not api_users._ws_admitted.keys() - {
        w for b in api_users._open_ws_by_user.values() for w in b}


@pytest.mark.parametrize("change", ["disable", "password", "demote"])
def test_a_change_a_server_save_merged_in_before_the_knock_still_closes(srv, change):
    # setadm writes; before its knock arrives the server saves something of
    # its own (here a display name), which merges setadm's change in from
    # disk (UserStore._merge_disk) — the knock's reload then changes nothing.
    socks = srv.start(("root", "admin"), ("bob", "edit"))
    bob, cli = srv.ids["bob"], srv.cli()
    if change == "disable":
        cli.update(bob, enabled=False)
    elif change == "password":
        cli.set_password(bob, NEW)
    else:
        cli.update(bob, role="readonly")
    before = srv.store.get(bob).to_storage()
    srv.store.update(srv.ids["root"], display_name="Root")
    assert srv.store.get(bob).to_storage() != before            # (merged already)
    assert socks["bob"].codes == []
    assert srv.knock().status_code == 200
    assert socks["bob"].codes == [4401] and socks["root"].codes == []


def test_a_failed_read_answers_500_and_closes_only_what_memory_revoked(srv):
    socks = srv.start(("root", "admin"), ("bob", "edit"), ("carol", "edit"))
    srv.cli().set_password(srv.ids["bob"], NEW)
    srv.store.update(srv.ids["root"], display_name="Root")      # (merges bob's new password)
    srv.cli().update(srv.ids["carol"], enabled=False)            # (not read: see below)
    users_json = srv.data_dir / "users.json"
    users_json.chmod(0)
    try:
        r = srv.knock()
    finally:
        users_json.chmod(0o600)
    assert r.status_code == 500
    assert socks["bob"].codes == [4401]
    assert socks["carol"].codes == [] and socks["root"].codes == []
    assert srv.store.get(srv.ids["carol"]).enabled is True


def test_a_users_file_with_no_account_closes_every_socket(srv):
    import json
    socks = srv.start(("root", "admin"), ("bob", "edit"))
    users_json = srv.data_dir / "users.json"
    doc = json.loads(users_json.read_text())
    doc["users"], doc["subsonic_password_custom"] = [], {}
    users_json.write_text(json.dumps(doc))
    (srv.data_dir / "users.json.bak").write_text(json.dumps(doc))   # (else the backup loads)
    r = srv.knock()
    assert r.status_code == 200, r.text
    assert srv.store.count() == 0
    assert socks["root"].codes == [4401] and socks["bob"].codes == [4401]


def test_the_recheck_button_on_a_half_written_file_closes_nothing(srv):
    socks = srv.start(("ed", "edit"), ("bob", "readonly"))
    (srv.data_dir / "users.json").write_bytes(b'{"version": 1, "users": [')
    r = srv.knock(token=None, peer="192.168.1.50")
    assert r.status_code == 200, r.text
    assert socks["bob"].codes == [] and socks["ed"].codes == []
    assert srv.store.count() == 2


def test_the_accounts_sockets_close_at_once_not_one_after_another(srv):
    # A restored users.json can revoke many accounts in one reload; each close
    # may wait up to 1 s, and setadm waits 5 s for the answer.
    srv.start(("root", "admin"), ("a1", "edit"), ("a2", "edit"), ("a3", "edit"))
    peak = {"now": 0, "max": 0}

    class Slow:
        async def close(self, code=1000):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.3)
            peak["now"] -= 1
    cli = srv.cli()
    for name in ("a1", "a2", "a3"):
        srv.only(name, Slow())
        cli.update(srv.ids[name], enabled=False)
    assert srv.knock().status_code == 200
    assert peak["max"] == 3


def test_a_live_library_websocket_is_closed_by_a_cli_disable(srv):
    """The real ``/api/library/ws`` handler (it registers itself): signed in
    as bob, the socket is open; setadm disables bob and knocks; the socket
    ends with 4401 and bob's cookie no longer opens one."""
    srv.start(("root", "admin"), ("bob", "edit"))
    api_users._open_ws_by_user.clear()                 # (drop the fake sockets)
    api_users._ws_admitted.clear()
    c = TestClient(main.app, client=("127.0.0.1", 50124))
    r = c.post("/api/auth/login", json={"username": "bob", "password": PW})
    assert r.status_code == 200
    with c.websocket_connect("/api/library/ws") as ws:
        assert ws.receive_json()["event"] == "scan_progress"
        assert srv.ids["bob"] in api_users._open_ws_by_user
        srv.cli().update(srv.ids["bob"], enabled=False)
        assert srv.knock().status_code == 200
        # The knock waited for the close: it is the next frame, at once (a
        # bounded read — the test client's own ``receive`` waits forever).
        msg = _receive_within(ws, 5.0)
        assert msg["type"] == "websocket.close" and msg.get("code") == 4401, msg
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/library/ws") as ws2:
            ws2.receive_json()
    assert api_users._ws_admitted == {} and api_users._open_ws_by_user == {}


@pytest.mark.parametrize("close_pass", [True, False], ids=["whole-reload", "read-in-only"])
@pytest.mark.parametrize("change", ["demote", "disable", "password", "remove", "nothing"])
@pytest.mark.parametrize("path", ["/api/library/ws", "/api/multiroom/ws"])
def test_a_change_landing_while_the_socket_is_accepted_closes_it_at_once(
        srv, monkeypatch, path, change, close_pass):
    """The handler checks the session, then ``accept()`` yields, and setadm's
    change is read in right then.  ``whole-reload``: the knock's close pass
    runs during the accept too and cannot see the socket, which is not
    registered yet.  ``read-in-only``: the change is only read in (a save
    merged it, say).  Either way the handler closes the socket (4401) right
    after registering it, with no later reload.  When nothing changed, the
    socket stays open.  Both registries end up empty."""
    srv.start(("root", "admin"), ("bob", "edit"))
    api_users._open_ws_by_user.clear()
    api_users._ws_admitted.clear()
    bob = srv.ids["bob"]
    c = TestClient(main.app, client=("127.0.0.1", 50125))
    assert c.post("/api/auth/login", json={"username": "bob", "password": PW}).status_code == 200
    real_accept, fired = WebSocket.accept, []

    async def accept_meanwhile(self, *a, **kw):
        if not fired:
            fired.append("accept")
            cli = srv.cli()
            if change == "demote":
                cli.update(bob, role="readonly")
            elif change == "disable":
                cli.update(bob, enabled=False)
            elif change == "password":
                cli.set_password(bob, NEW)
            elif change == "remove":
                cli.delete(bob)
            srv.store.reload()
            if close_pass:
                fired.append(await api_users._close_ws_revoked(srv.store))
        return await real_accept(self, *a, **kw)
    monkeypatch.setattr(WebSocket, "accept", accept_meanwhile)
    with c.websocket_connect(path) as ws:
        assert fired[:1] == ["accept"]
        if close_pass:
            assert fired[1] == 0                    # (the pass saw no socket)
        if change == "nothing":
            with pytest.raises(TimeoutError):
                _next_close(ws, 0.3)
            assert bob in api_users._open_ws_by_user
        else:
            msg = _next_close(ws, 5.0)
            assert msg.get("code") == 4401, msg
            assert bob not in api_users._open_ws_by_user
    assert api_users._ws_admitted == {} and api_users._open_ws_by_user == {}


def test_a_socket_whose_account_was_not_loaded_closes_only_on_remove_or_disable(srv):
    srv.start(("root", "admin"), ("bob", "edit"), ("carol", "edit"))
    b, c = FakeWS(), FakeWS()
    for name, ws in (("bob", b), ("carol", c)):
        api_users.unregister_open_ws(srv.ids[name],
                                     next(iter(api_users._open_ws_by_user[srv.ids[name]])))
        api_users.register_open_ws(srv.ids[name], ws, None)
    cli = srv.cli()
    cli.set_password(srv.ids["bob"], NEW)
    cli.update(srv.ids["carol"], enabled=False)
    assert srv.knock().status_code == 200
    assert b.codes == [] and c.codes == [4401]


def test_own_password_change_keeping_the_sign_in_closes_that_socket_at_a_reload(srv):
    # The documented trade-off (``_close_ws_revoked``): the session stays (it
    # moved to the new password) but the socket's standing has the old hash —
    # it is closed at the next reload and reconnects on the same session.
    socks = srv.start(("root", "admin"), ("bob", "edit"))
    tok, _ = srv.store.issue_session(srv.ids["bob"])
    srv.store.set_password(srv.ids["bob"], NEW, tok)
    assert srv.store.lookup_session(tok) is not None
    assert srv.knock().status_code == 200
    assert socks["bob"].codes == [4401] and socks["root"].codes == []


def test_the_check_after_registering_drops_a_doomed_socket_from_the_registry(srv):
    # So a reload whose close pass runs while the handler closes that socket
    # does not close it a second time; an unchanged account's socket stays.
    srv.start(("root", "admin"), ("bob", "edit"), ("carol", "edit"))
    b, c = FakeWS(), FakeWS()
    for name, ws in (("bob", b), ("carol", c)):
        user = srv.store.get(srv.ids[name])
        srv.only(name, ws)
        api_users._ws_admitted[ws] = api_users.ws_standing(user)
    srv.cli().update(srv.ids["bob"], role="readonly")
    srv.store.reload()
    assert api_users.ws_revoked_since_admitted(srv.ids["bob"], b) == "demoted"
    assert api_users.ws_revoked_since_admitted(srv.ids["carol"], c) is None
    assert srv.ids["bob"] not in api_users._open_ws_by_user and b not in api_users._ws_admitted
    assert c in api_users._open_ws_by_user[srv.ids["carol"]]
    assert api_users.ws_revoked_since_admitted(srv.ids["bob"], b) is None   # (no longer known)
    assert srv.knock().status_code == 200
    assert b.codes == [] and c.codes == []


@pytest.mark.parametrize("change", ["demote", "disable", "remove"])
@pytest.mark.parametrize("path", ["/api/library/ws", "/api/multiroom/ws"])
def test_an_admin_change_in_the_server_during_the_accept_closes_the_socket_too(
        srv, monkeypatch, path, change):
    """The admin's PATCH / DELETE in this process changes the account object
    in place and closes the user's sockets (``close_open_ws_for``), but that
    finds none while the socket is still being accepted.  The check after
    registering closes it."""
    srv.start(("root", "admin"), ("bob", "edit"))
    api_users._open_ws_by_user.clear()
    api_users._ws_admitted.clear()
    bob = srv.ids["bob"]
    c = TestClient(main.app, client=("127.0.0.1", 50126))
    assert c.post("/api/auth/login", json={"username": "bob", "password": PW}).status_code == 200
    real_accept, fired = WebSocket.accept, []

    async def accept_meanwhile(self, *a, **kw):
        if not fired:
            if change == "demote":
                srv.store.update(bob, role="readonly")
            elif change == "disable":
                srv.store.update(bob, enabled=False)
            else:
                srv.store.delete(bob)
            fired.append(await api_users.close_open_ws_for(bob))
        return await real_accept(self, *a, **kw)
    monkeypatch.setattr(WebSocket, "accept", accept_meanwhile)
    with c.websocket_connect(path) as ws:
        assert fired == [0]                         # (it found no socket)
        msg = _next_close(ws, 5.0)
        assert msg.get("code") == 4401, msg
    assert api_users._ws_admitted == {} and api_users._open_ws_by_user == {}
