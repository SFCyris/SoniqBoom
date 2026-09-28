# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``soniqboom-setadm``'s reload notification reaches only the server that
serves the SAME data dir, and ``POST /api/auth/reload`` only reloads for the
CLI (loopback + this run's reload token) or during first-run bootstrap.

Two layers:

* the endpoint gate — in-process, ``TestClient`` with a chosen peer address;
* the notification — real uvicorn servers (the real app: middleware + users
  router, lifespan off) in subprocesses, each with its own throw-away data dir,
  config and ``HOME``, on an ephemeral loopback port, plus tiny stub servers
  for the odd answers.  Nothing here talks to port 8080 or writes under the
  real app-support dir.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from soniqboom import cli_setadm
from soniqboom.config import settings
from soniqboom.core import startup_status
from soniqboom.core import users as users_mod

_TOKEN_HEADER = startup_status.RELOAD_TOKEN_HEADER
_PW = "pw-12345678"
_STUB_TOK = "stub-run-token-0123456789"


# ── startup-status.json records where the server listens ──────────────────


def test_status_file_records_port_host_data_dir_and_token(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "host", "0.0.0.0")
    monkeypatch.setattr(settings, "port", 18555)
    monkeypatch.setattr(startup_status, "_status", dict(startup_status._status))
    monkeypatch.setattr(startup_status, "_status_file", None)
    startup_status.init(tmp_path)
    path = tmp_path / "startup-status.json"
    doc = json.loads(path.read_text())
    assert doc["port"] == 18555
    assert doc["host"] == "0.0.0.0"
    assert doc["data_dir"] == str(tmp_path)
    assert doc["pid"] == os.getpid()
    assert doc["reload_token"] == startup_status.reload_token()
    assert len(doc["reload_token"]) >= 32
    assert "reload_token" not in startup_status.get_status()   # file only
    assert stat.S_IMODE(path.stat().st_mode) == 0o600      # carries the token
    # A later phase rewrite keeps them; a new run gets a new token.
    startup_status.set_phase("loading_users", "Loading user accounts")
    doc2 = json.loads(path.read_text())
    assert (doc2["port"], doc2["data_dir"], doc2["reload_token"]) == (
        18555, str(tmp_path), doc["reload_token"])
    startup_status.init(tmp_path)
    assert startup_status.reload_token() != doc["reload_token"]


# ── /api/auth/reload gate ─────────────────────────────────────────────────

_TOK = "the-run-token-0123456789abcdefghij"


@pytest.fixture()
def gate(tmp_data_dir, monkeypatch):
    """The live app over a throw-away user store, this run's token = _TOK.
    ``add_on_disk`` writes an account behind the server's back (as the CLI
    does), ``post`` reloads from a chosen peer address, ``server_knows`` reads
    the server's memory."""
    from fastapi.testclient import TestClient
    from soniqboom.main import app

    server_store = users_mod.UserStore(tmp_data_dir)
    monkeypatch.setattr(users_mod, "_instance", server_store)
    monkeypatch.setattr(startup_status, "_status",
                        {**startup_status._status, "reload_token": _TOK})

    class G:
        store = server_store
        data_dir = tmp_data_dir

        @staticmethod
        def add_on_disk(username, role="admin"):
            users_mod.UserStore(tmp_data_dir).create(
                username=username, password=_PW, role=role)

        @staticmethod
        def post(peer, token=None, headers=None):
            h = dict(headers or {})
            if token is not None:
                h[_TOKEN_HEADER] = token
            return TestClient(app, client=(peer, 50123)).post(
                "/api/auth/reload", headers=h)

        @staticmethod
        def server_knows(username):
            return server_store.get_by_username(username) is not None

    return G


def _with_admin(gate):
    gate.store.create(username="root1", password=_PW, role="admin")


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"])
def test_cli_reload_from_loopback_with_the_run_token(gate, peer):
    _with_admin(gate)
    gate.add_on_disk("alice")
    r = gate.post(peer, token=_TOK)
    assert r.status_code == 200, r.text
    assert gate.server_knows("alice")


@pytest.mark.parametrize("peer", ["192.168.1.50", "10.0.0.7", "172.17.0.1",
                                  "fe80::1", "2001:db8::5", "testclient"])
def test_remote_reload_refused_even_with_the_token(gate, peer):
    _with_admin(gate)
    gate.add_on_disk("alice")
    r = gate.post(peer, token=_TOK)
    assert r.status_code == 403
    assert not gate.server_knows("alice")          # nothing was re-read


def test_loopback_without_the_token_is_refused_once_an_admin_exists(gate):
    """A web page in a browser on the server host (cross-site fetch, DNS
    rebinding) is a loopback peer too — without the token it gets nothing."""
    _with_admin(gate)
    gate.add_on_disk("alice")
    r = gate.post("127.0.0.1", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not gate.server_knows("alice")


@pytest.mark.parametrize("admin_exists", [True, False])
def test_a_token_that_is_not_this_runs_reloads_nothing(gate, admin_exists):
    """Stale status file / copied data dir: the CLI knocks with another run's
    token — 409, and not even the bootstrap window reloads."""
    if admin_exists:
        _with_admin(gate)
    gate.add_on_disk("alice")
    r = gate.post("127.0.0.1", token="some-other-runs-token")
    assert r.status_code == 409
    assert not gate.server_knows("alice")


def test_token_refused_when_this_run_has_none(gate, monkeypatch):
    monkeypatch.setattr(startup_status, "_status",
                        {**startup_status._status, "reload_token": None})
    gate.add_on_disk("alice")
    assert gate.post("127.0.0.1", token="").status_code == 409
    assert gate.post("127.0.0.1", token=_TOK).status_code == 409
    assert not gate.server_knows("alice")


@pytest.mark.parametrize("header", [
    {"X-Forwarded-For": "203.0.113.9"},
    {"Forwarded": "for=203.0.113.9"},
    {"X-Real-IP": "203.0.113.9"},
    {"X-Forwarded-Proto": "https"},       # nginx configured for HTTPS only
    {"X-Forwarded-Host": "music.example.com"},
    {"Via": "1.1 caddy"},
    {"X-Forwarded-For": "127.0.0.1"},     # spoofed "local" still means relayed
])
def test_loopback_peer_relaying_for_someone_is_refused(gate, header):
    """A reverse proxy on this host connects over loopback on behalf of a
    remote client — its forwarding header must not ride the loopback pass."""
    _with_admin(gate)
    gate.add_on_disk("alice")
    r = gate.post("127.0.0.1", token=_TOK, headers=header)
    assert r.status_code == 403
    assert not gate.server_knows("alice")


def test_bootstrap_recheck_works_from_any_browser(gate):
    """No admin yet: the login overlay's "re-check" button (a remote browser,
    no token) must still pick up the admin the operator just created."""
    gate.add_on_disk("firstadmin")
    r = gate.post("192.168.1.50")
    assert r.status_code == 200, r.text
    assert r.json()["has_any_admin"] is True
    assert gate.server_knows("firstadmin")
    # …and from then on only the CLI reloads.
    gate.add_on_disk("second")
    assert gate.post("192.168.1.50").status_code == 403
    assert not gate.server_knows("second")


def test_non_admin_users_only_still_counts_as_bootstrap(gate):
    """Users but no enabled admin: the overlay still shows the re-check
    button (it hides on ``has_any_admin``), so a tokenless reload is allowed."""
    gate.store.create(username="viewer", password=_PW, role="readonly")
    gate.add_on_disk("firstadmin")
    r = gate.post("192.168.1.50")
    assert r.status_code == 200, r.text
    assert gate.server_knows("firstadmin")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file anyway")
def test_unreadable_users_json_is_500_not_live(gate):
    """``sudo soniqboom-setadm`` leaves a root-owned 0600 users.json the
    server can't read: the reload must say so, not answer 200."""
    _with_admin(gate)
    gate.add_on_disk("alice")
    (gate.data_dir / "users.json").chmod(0)
    r = gate.post("127.0.0.1", token=_TOK)
    assert r.status_code == 500
    assert gate.server_knows("root1") and not gate.server_knows("alice")


# ── setadm → the right server, end to end ────────────────────────────────

_SERVER = textwrap.dedent("""
    import socket, sys
    import uvicorn
    from soniqboom.config import settings, get_data_dir
    from soniqboom.core import startup_status
    from soniqboom.core.users import init_user_store
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    data_dir = get_data_dir()
    # What cli() does before uvicorn starts, then the lifespan's first step…
    settings.host, settings.port = "0.0.0.0", sock.getsockname()[1]
    startup_status.init(data_dir)
    init_user_store(data_dir)
    from soniqboom.main import app
    startup_status.mark_ready()          # …and its last; uvicorn listens after
    uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning")).run(
        sockets=[sock])
""")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method, port, path, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


class _Server:
    """A real SoniqBoom HTTP server for one throw-away data dir."""

    def __init__(self, root: Path, name: str):
        self.data_dir = root / name / "data"
        self.data_dir.mkdir(parents=True)
        home = root / name / "home"
        home.mkdir()
        conf = root / name / "SoniqBoom.conf"
        conf.write_text(json.dumps({
            "data_dir": str(self.data_dir),
            "art_cache_dir": str(root / name / "cache" / "art"),
            "conversion_cache_dir": str(root / name / "cache" / "conv"),
            "services": {"subsonic": False, "multiroom": False,
                         "cast": False, "dlna_server": False},
        }))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SONIQBOOM_") and k.lower() not in (
                   "http_proxy", "https_proxy", "all_proxy")}
        env.update(HOME=str(home), SONIQBOOM_CONF=str(conf),
                   SONIQBOOM_DATA_DIR=str(self.data_dir))
        self.log = root / name / "server.log"
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-c", _SERVER], env=env,
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=log, stderr=subprocess.STDOUT)
        status = self.data_dir / "startup-status.json"
        deadline = time.monotonic() + 60
        self.port = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(self.log.read_text(errors="replace"))
            try:
                self.port = json.loads(status.read_text())["port"]
                if _http("GET", self.port, "/api/auth/status")[0] == 200:
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("test server didn't come up")

    def status(self):
        return _http("GET", self.port, "/api/auth/status")[1]

    def can_login(self, user, pw=_PW):
        return _http("POST", self.port, "/api/auth/login",
                     {"username": user, "password": pw})[0] == 200

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture()
def servers(tmp_path):
    started: list[_Server] = []

    def start(name):
        s = _Server(tmp_path, name)
        started.append(s)
        return s
    yield start
    for s in started:
        s.stop()


def _setadm(monkeypatch, capsys, data_dir: Path, *argv) -> str:
    monkeypatch.setattr(settings, "data_dir", str(data_dir))
    assert cli_setadm.main(list(argv)) == 0
    return capsys.readouterr().out


def _plant_canary(server: _Server) -> None:
    """Write an admin into a bootstrap-state server's users.json behind its
    back.  The server only learns of it if something reloads it — a public
    ``/auth/status`` then flips ``has_any_admin``."""
    users_mod.UserStore(server.data_dir).create(
        username="canary", password=_PW, role="admin")
    assert server.status()["has_any_admin"] is False


def _status_doc(data_dir: Path, **fields) -> None:
    """A status file as a live server writes it.  The parent pid stands in
    for a live server's (setadm's own pid counts as stale)."""
    doc = {"pid": os.getppid(), "host": "0.0.0.0", "data_dir": str(data_dir),
           "reload_token": "not-this-runs-token", "ready": True}
    doc.update(fields)
    (data_dir / "startup-status.json").write_text(json.dumps(doc))


def test_setadm_notifies_the_server_of_its_own_data_dir(
        servers, monkeypatch, capsys):
    """The reported bug: setadm on a throw-away instance used to POST to the
    DEFAULT port (here: ``decoy``, standing in for the :8080 server) and claim
    the change was live.  Now it notifies the instance that serves its data
    dir — through the CLI gate, since an admin already exists there."""
    mine = servers("mine")
    users_mod.UserStore(mine.data_dir).create(username="root1", password=_PW, role="admin")
    assert _http("POST", mine.port, "/api/auth/reload")[0] == 200   # bootstrap re-check
    decoy = servers("decoy")
    _plant_canary(decoy)
    monkeypatch.setattr(settings, "port", decoy.port)   # "the default port"

    assert not mine.can_login("alice")
    out = _setadm(monkeypatch, capsys, mine.data_dir, "-user", "alice", "-passwd", _PW)
    assert f"Notified the running server (port {mine.port})" in out
    assert mine.can_login("alice")                       # live, no restart
    assert decoy.status()["has_any_admin"] is False      # decoy never reloaded

    # The update flow notifies too: a disabled account stops signing in.
    out = _setadm(monkeypatch, capsys, mine.data_dir, "-user", "alice", "-disable")
    assert "Notified the running server" in out
    assert not mine.can_login("alice")


def test_recheck_button_after_the_cli_already_notified(servers, monkeypatch, capsys):
    """Docker first run: the browser shows the bootstrap hint, the operator
    runs ``docker compose exec … soniqboom-setadm`` (which reloads the server),
    then clicks "re-check".  The POST is now refused (an admin exists, no
    token) — auth.js falls back to the public status, which is already fresh."""
    mine = servers("mine")
    out = _setadm(monkeypatch, capsys, mine.data_dir, "-user", "me", "-passwd", _PW)
    assert "Notified the running server" in out
    assert _http("POST", mine.port, "/api/auth/reload")[0] == 403
    assert mine.status()["has_any_admin"] is True
    assert mine.can_login("me")


def test_setadm_without_a_server_for_its_data_dir_skips_the_notification(
        servers, tmp_path, monkeypatch, capsys):
    decoy = servers("decoy")
    _plant_canary(decoy)
    monkeypatch.setattr(settings, "port", decoy.port)
    throwaway = tmp_path / "throwaway"
    throwaway.mkdir()

    out = _setadm(monkeypatch, capsys, throwaway, "-user", "alice", "-passwd", _PW)
    assert "Created user 'alice'" in out
    assert f"No running server found for {throwaway}" in out
    assert "needs a restart to apply it" in out
    assert "live now" not in out
    assert decoy.status()["has_any_admin"] is False


def test_copied_data_dir_never_reloads_the_original_server(
        servers, tmp_path, monkeypatch, capsys):
    """``cp -r`` of a live data dir copies its status file too — pid alive,
    port answering.  setadm on the copy must not touch the original."""
    orig = servers("orig")
    copy = tmp_path / "copy"
    shutil.copytree(orig.data_dir, copy)
    _plant_canary(orig)

    out = _setadm(monkeypatch, capsys, copy, "-user", "alice", "-passwd", _PW)
    assert f"No running server found for {copy}" in out
    assert f"is the server's for {orig.data_dir}" in out
    assert orig.status()["has_any_admin"] is False       # never knocked


def test_stale_status_file_naming_another_servers_port_reloads_nothing(
        servers, tmp_path, monkeypatch, capsys):
    """This data dir's server is gone and its port now belongs to another
    instance (pid still "alive" — reused): the other instance refuses the
    foreign token without reloading, and nothing is reported live."""
    decoy = servers("decoy")
    _plant_canary(decoy)
    stale = tmp_path / "stale"
    stale.mkdir()
    _status_doc(stale, port=decoy.port)

    out = _setadm(monkeypatch, capsys, stale, "-user", "alice", "-passwd", _PW)
    assert f"the server on port {decoy.port} serves another data dir" in out
    assert "live now" not in out
    assert decoy.status()["has_any_admin"] is False      # 409, nothing re-read


@pytest.mark.parametrize("which", ["dead", "setadm's own"])
def test_stale_pid_skips_the_notification(stub, capsys, which):
    if which == "dead":
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        pid = gone.pid
    else:
        pid = os.getpid()     # a reused pid can't be the server we're after
    _status_doc(stub["data_dir"], pid=pid, port=stub["port"], reload_token=_STUB_TOK)
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert f"No running server found for {stub['data_dir']} — " in out
    assert stub["posts"] == 0                            # never knocked


def test_notification_ignores_an_http_proxy_from_the_environment(
        servers, monkeypatch, capsys):
    mine = servers("mine")
    dead_proxy = f"http://127.0.0.1:{_free_port()}"
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, dead_proxy)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    out = _setadm(monkeypatch, capsys, mine.data_dir, "-user", "alice", "-passwd", _PW)
    assert f"Notified the running server (port {mine.port})" in out
    assert mine.can_login("alice")


# ── notification edge cases against stub servers ─────────────────────────


@pytest.fixture()
def stub(tmp_path):
    """A one-route HTTP stub on an ephemeral loopback port, named by this
    data dir's status file.  Set ``code`` / ``body`` / ``headers`` / ``delay``
    for what POST returns; ``posts`` counts requests, ``tokens`` their token."""
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    state = {"code": 200, "body": json.dumps({"data_dir": str(data_dir)}).encode(),
             "headers": {}, "delay": 0.0, "posts": 0, "tokens": []}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            state["posts"] += 1
            state["tokens"].append(self.headers.get(_TOKEN_HEADER))
            time.sleep(state["delay"])
            hook = state.get("hook")
            self.send_response(hook() if hook else state["code"])
            for k, v in state["headers"].items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["data_dir"] = data_dir
    state["port"] = srv.server_address[1]
    _status_doc(data_dir, port=state["port"], reload_token=_STUB_TOK)
    yield state
    srv.shutdown()
    srv.server_close()


def test_the_run_token_goes_along(stub, capsys):
    cli_setadm._notify_server_reload(stub["data_dir"])
    assert f"Notified the running server (port {stub['port']})" in capsys.readouterr().out
    assert stub["tokens"] == [_STUB_TOK]


def test_refused_reload_says_refused_and_does_not_retry(stub, capsys):
    stub["code"], stub["body"] = 403, b'{"detail": "no"}'
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "refused the reload (HTTP 403)" in out
    assert "live now" not in out
    assert stub["posts"] == 1


@pytest.mark.parametrize("body, shown", [
    (b'{"detail": "users.json could not be read"}', "users.json could not be read"),
    (b"Internal Server Error", "Internal Server Error"),      # e.g. lock unreadable
])
def test_failed_server_side_reload_is_not_live(stub, capsys, body, shown):
    stub["code"], stub["body"] = 500, body
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert f"(HTTP 500: {shown})" in out and "NOT live" in out
    # Restoring the file comes BEFORE re-running setadm (else the server
    # reloads a users.json with only the new account in it).
    assert out.index("restore that file") < out.index("run soniqboom-setadm again")
    assert "live now" not in out


def test_a_redirect_is_not_followed(stub, capsys):
    target = _free_port()
    stub["code"], stub["body"] = 302, b""
    stub["headers"] = {"Location": f"http://127.0.0.1:{target}/elsewhere"}
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "refused the reload (HTTP 302)" in out
    assert stub["posts"] == 1


def test_a_hung_server_gets_one_request_not_five(stub, monkeypatch, capsys):
    monkeypatch.setattr(cli_setadm, "_TIMEOUT_S", 1.0)
    stub["delay"] = 2.0
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "didn't answer within 1 s" in out and "may or may not be live" in out
    assert stub["posts"] == 1


def test_non_soniqboom_answer_is_not_reported_live(stub, capsys):
    stub["body"] = b"<html>hello</html>"
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "isn't a SoniqBoom server" in out
    assert "live now" not in out


def test_answer_without_a_data_dir_is_not_reported_live(stub, capsys):
    stub["body"] = b'{"has_any_admin": true}'
    cli_setadm._notify_server_reload(stub["data_dir"])
    assert "live now" not in capsys.readouterr().out


def test_answer_naming_another_data_dir_is_not_reported_live(stub, tmp_path, capsys):
    stub["body"] = json.dumps({"data_dir": str(tmp_path)}).encode()
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert f"serves {tmp_path}, not {stub['data_dir']}" in out
    assert "live now" not in out


def test_non_http_listener_does_not_crash_setadm(tmp_path, monkeypatch, capsys):
    """Whatever holds the recorded port may not speak HTTP at all — the user
    was already created, so setadm must finish (exit 0) with a message, not a
    ``BadStatusLine`` traceback."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen()

    def garbage():
        while True:
            try:
                c, _ = lsock.accept()
            except OSError:
                return
            c.recv(65536)
            c.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            c.close()
    threading.Thread(target=garbage, daemon=True).start()
    try:
        _status_doc(tmp_path, port=lsock.getsockname()[1])
        out = _setadm(monkeypatch, capsys, tmp_path, "-user", "alice", "-passwd", _PW)
    finally:
        lsock.close()
    assert "Created user 'alice'" in out
    assert "isn't a SoniqBoom server (BadStatusLine)" in out


def test_waits_for_a_server_that_is_still_starting(stub, monkeypatch, capsys):
    _status_doc(stub["data_dir"], port=stub["port"], reload_token=_STUB_TOK, ready=False)

    def becomes_ready():
        time.sleep(1.0)
        _status_doc(stub["data_dir"], port=stub["port"], reload_token=_STUB_TOK, ready=True)
    threading.Thread(target=becomes_ready, daemon=True).start()
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "Waiting for the server" in out
    assert f"Notified the running server (port {stub['port']})" in out
    assert stub["posts"] == 1


def test_gives_up_on_a_server_that_never_finishes_starting(stub, monkeypatch, capsys):
    monkeypatch.setattr(cli_setadm, "_READY_WAIT_S", 1.0)
    _status_doc(stub["data_dir"], port=stub["port"], reload_token=_STUB_TOK, ready=False)
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "is still starting after 1 s" in out
    assert "live now" not in out
    assert stub["posts"] == 0


_OK = {"pid": os.getppid(), "port": 18080, "reload_token": "t" * 43}


@pytest.mark.parametrize("doc", [
    None,                                   # no file
    "{not json",
    "[1, 2]",
    {"pid": os.getppid()},                  # older server: nothing recorded
    {**_OK, "reload_token": None},
    {**_OK, "reload_token": "short"},
    {**_OK, "reload_token": "bad token\nX-Injected: 1 " + "x" * 20},
    {**_OK, "reload_token": "ü" * 43},
    {**_OK, "port": "abc"}, {**_OK, "port": 0}, {**_OK, "port": 70000},
    {**_OK, "port": True},
    {**_OK, "pid": 0}, {**_OK, "pid": -1}, {**_OK, "pid": True}, {**_OK, "pid": None},
    {**_OK, "data_dir": None},
])
def test_unusable_status_file_skips_the_notification(tmp_path, capsys, doc):
    if isinstance(doc, dict) and "data_dir" not in doc:
        doc = {**doc, "data_dir": str(tmp_path)}
    if doc is not None:
        (tmp_path / "startup-status.json").write_text(
            doc if isinstance(doc, str) else json.dumps(doc))
    cli_setadm._notify_server_reload(tmp_path)        # never raises
    out = capsys.readouterr().out
    assert f"No running server found for {tmp_path}" in out


def test_server_bound_to_one_lan_address_is_not_knocked(tmp_path, capsys):
    _status_doc(tmp_path, host="192.168.1.5", port=18080)
    cli_setadm._notify_server_reload(tmp_path)
    out = capsys.readouterr().out
    assert "listens only on 192.168.1.5:18080, not on loopback" in out


@pytest.mark.parametrize("bind, target", [
    ("", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("localhost", "127.0.0.1"),
    ("127.0.0.1", "127.0.0.1"), ("127.0.0.2", "127.0.0.2"),
    ("::", "::1"), ("::1", "::1"), ("[::1]", "::1"),
    ("192.168.1.5", None), ("fe80::1", None), ("music.invalid", None),
])
def test_loopback_host_for_bind_address(bind, target):
    assert cli_setadm._loopback_host(bind) == target


@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal anything")
def test_pid_owned_by_another_user_counts_as_alive():
    assert cli_setadm._pid_alive(1) is True       # init/launchd: PermissionError


def test_restarted_server_is_knocked_again_with_its_new_token(stub, capsys):
    """``docker restart`` / the admin Restart between reading the status file
    and knocking: the new run answers 409 to the old token — setadm re-reads
    the file and knocks again instead of reporting "no server"."""
    new_tok = "new-run-token-9876543210"
    real_post = stub["code"]

    def post_hook():
        if stub["tokens"][-1] == _STUB_TOK:          # the old run's token
            _status_doc(stub["data_dir"], port=stub["port"], reload_token=new_tok)
            return 409
        return real_post
    stub["hook"] = post_hook
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert f"Notified the running server (port {stub['port']})" in out
    assert stub["tokens"] == [_STUB_TOK, new_tok]


def test_port_refused_while_the_server_comes_back(stub, tmp_path, capsys):
    """The status names a port that refuses (server going down); meanwhile the
    next run writes a new status — setadm follows it."""
    closed = _free_port()
    _status_doc(stub["data_dir"], port=closed, reload_token=_STUB_TOK)

    def comes_back():        # after the refusals: only the grace wait sees it
        time.sleep(3.0)
        _status_doc(stub["data_dir"], port=stub["port"], reload_token="next-run-token-0123456789")
    threading.Thread(target=comes_back, daemon=True).start()
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert f"Notified the running server (port {stub['port']})" in out


def test_port_that_keeps_refusing_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_setadm, "_RESTART_GRACE_S", 0.5)
    port = _free_port()
    _status_doc(tmp_path, port=port, reload_token=_STUB_TOK)
    cli_setadm._notify_server_reload(tmp_path)
    out = capsys.readouterr().out
    assert f"isn't answering on port {port} — restart the server" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file anyway")
def test_unreadable_status_file_says_so(tmp_path, capsys):
    _status_doc(tmp_path, port=18080, reload_token=_STUB_TOK)
    (tmp_path / "startup-status.json").chmod(0)
    try:
        cli_setadm._notify_server_reload(tmp_path)
    finally:
        (tmp_path / "startup-status.json").chmod(0o600)
    out = capsys.readouterr().out
    assert "Can't read" in out and "run soniqboom-setadm as the server's OS user" in out


def test_ctrl_c_while_waiting_is_not_a_traceback(stub, monkeypatch, capsys):
    _status_doc(stub["data_dir"], port=stub["port"], reload_token=_STUB_TOK, ready=False)

    def interrupt(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(time, "sleep", interrupt)
    cli_setadm._notify_server_reload(stub["data_dir"])
    out = capsys.readouterr().out
    assert "Waiting for the server" in out and "Stopped" in out
    assert stub["posts"] == 0


def test_reload_answer_reports_an_absolute_data_dir(gate, monkeypatch):
    """A relative data dir must not make setadm compare against its own cwd."""
    monkeypatch.chdir(gate.data_dir.parent)
    monkeypatch.setattr(settings, "data_dir", gate.data_dir.name)
    r = gate.post("192.168.1.50")                      # bootstrap: allowed
    assert r.status_code == 200
    assert r.json()["data_dir"] == str(gate.data_dir)


def test_interactive_setup_admin_notifies_the_server(servers, monkeypatch, capsys):
    """``bash setup-admin.sh`` (no args → ``--setup-admin``) creates the first
    admin interactively; a running server hears about it like with -user."""
    import builtins
    import getpass
    mine = servers("mine")
    monkeypatch.setattr(settings, "data_dir", str(mine.data_dir))
    monkeypatch.setattr(sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "boss")
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": _PW)
    assert cli_setadm.main(["--setup-admin"]) == 0
    out = capsys.readouterr().out
    assert "Admin 'boss' created" in out
    assert f"Notified the running server (port {mine.port})" in out
    assert mine.can_login("boss")
