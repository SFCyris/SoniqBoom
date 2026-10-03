# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Connecting network shares at startup (and reconnecting them later).

Shares are grouped by the server they live on: ONE quick reachability check
answers for all of a server's shares — six FTP shares on a host that is down
used to cost each one three connect attempts with back-off, in turn (17.6 s
in the field) — servers are probed concurrently, and the shares of a server
that answers connect concurrently.  Shares that didn't connect are retried by
the health monitor (they used to be left unconnected until a restart), and
WebDAV shares connect at startup at all (they were skipped).
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402

from soniqboom import main  # noqa: E402
from soniqboom.core import filesource  # noqa: E402
from soniqboom.core.store import TrackStore  # noqa: E402


class _Src:
    def __init__(self, delay: float = 0.0, ok: bool = True):
        self.delay, self.ok = delay, ok
        self.last_error = None if ok else "no route"
        self.closed = False

    def is_dir(self, path):
        time.sleep(self.delay)
        return self.ok

    def close(self):
        self.closed = True


def _ftp(host, port, i):
    return {"protocol": "ftp", "host": host, "port": port, "share": "",
            "remote_path": f"/m{i}", "username": "", "password_enc": "",
            "auto_connect": True}


@pytest.fixture
def env(tmp_data_dir, monkeypatch):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    conf: dict = {"network_shares": {}}
    monkeypatch.setattr("soniqboom.config.load_local_conf", lambda *a, **k: conf)
    created: list[dict] = []
    behaviour: dict[str, _Src] = {}

    def _create(share, password=""):
        created.append(share)
        return behaviour.get(share["remote_path"]) or _Src()
    monkeypatch.setattr(filesource, "create_source", _create)
    before = set(filesource.all_sources())
    yield store, conf, created, behaviour
    for root in set(filesource.all_sources()) - before:
        filesource.remove_source(root)


def _status(store):
    return {d["path"]: d.get("status") for d in store.list_scan_dirs()}


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    yield s.getsockname()[1]
    s.close()


def test_a_malformed_host_is_unreachable_not_a_crash():
    assert main._tcp_unreachable("nas..local", 21, 0.5)
    assert main._tcp_unreachable("x" * 70 + ".local", 21, 0.5)


def test_shares_group_by_server():
    shares = {
        "a": _ftp("10.0.0.88", 21, 1), "b": _ftp("10.0.0.88", 21, 2),
        "c": _ftp("10.0.0.88", 2121, 3),
        "d": {"protocol": "smb", "host": "NAS", "share": "Music"},
        "e": {"protocol": "webdav", "host": "dav:8080", "base_url": "http://dav:8080/x"},
        "f": {**_ftp("10.0.0.88", 21, 4), "auto_connect": False},
        "g": {"protocol": "gopher", "host": "x"},
    }
    groups = main._auto_connect_groups(shares)
    assert {ep: [m[0] for m in ms] for ep, ms in groups.items()} == {
        ("ftp", "10.0.0.88", 21): ["a", "b"],
        ("ftp", "10.0.0.88", 2121): ["c"],
        ("smb", "nas", 445): ["d"],
        ("http", "dav", 8080): ["e"],
    }


async def test_a_down_server_is_probed_once_for_all_its_shares(env, monkeypatch):
    store, conf, created, _ = env
    port = _closed_port()
    conf["network_shares"] = {f"ftp-{i}": _ftp("127.0.0.1", port, i) for i in range(6)}
    probes = []
    real = main._tcp_unreachable
    monkeypatch.setattr(main, "_tcp_unreachable",
                        lambda h, p, t: probes.append((h, p)) or real(h, p, t))
    t0 = time.monotonic()
    await main._init_network_shares()
    assert time.monotonic() - t0 < 1.0
    assert probes == [("127.0.0.1", port)]
    assert created == []                      # no per-share connect attempts at all
    assert set(_status(store).values()) == {"unavailable"} and len(_status(store)) == 6
    assert not any(r.startswith("ftp://127.0.0.1/m") for r in filesource.all_sources())


async def test_a_reachable_server_connects_its_shares_concurrently(env, listener):
    store, conf, created, behaviour = env
    conf["network_shares"] = {f"ftp-{i}": _ftp("127.0.0.1", listener, i) for i in range(4)}
    conf["network_shares"]["dav"] = {
        "protocol": "webdav", "host": f"127.0.0.1:{listener}", "remote_path": "/dav",
        "base_url": f"http://127.0.0.1:{listener}/dav", "auto_connect": True}
    for i in range(4):
        behaviour[f"/m{i}"] = _Src(delay=0.4)
    behaviour["/m3"] = _Src(delay=0.4, ok=False)          # one share's folder is gone
    t0 = time.monotonic()
    await main._init_network_shares()
    # Two at a time per server (``_SHARE_CONNECT_PER_HOST``): ~0.8 s; in turn
    # they took ≥ 1.6 s.
    assert time.monotonic() - t0 < 1.4
    st = _status(store)
    assert st[f"http://127.0.0.1:{listener}/dav"] == "ok"   # WebDAV connects at startup
    assert [st[f"ftp://127.0.0.1/m{i}"] for i in range(4)] == ["ok", "ok", "ok", "unavailable"]
    assert behaviour["/m3"].closed
    assert filesource.get_source("ftp://127.0.0.1/m0") is behaviour["/m0"]


async def test_the_health_monitor_reconnects_shares_that_never_connected(env, listener,
                                                                        monkeypatch):
    store, conf, created, behaviour = env
    down = _closed_port()
    conf["network_shares"] = {
        "up-0": _ftp("127.0.0.1", listener, 0),
        "down-1": _ftp("127.0.0.1", down, 1),
    }
    await main._init_network_shares()
    assert _status(store) == {"ftp://127.0.0.1/m0": "ok", "ftp://127.0.0.1/m1": "unavailable"}
    created.clear()
    # Still down: retried, nothing registered, the connected share is left
    # alone, and nothing is rewritten (the share is unavailable already).
    writes = []
    import soniqboom.core.data as data_mod
    real_upsert = data_mod.upsert_scan_dir

    async def _counting_upsert(*a, **k):
        writes.append(a[0])
        return await real_upsert(*a, **k)
    monkeypatch.setattr(data_mod, "upsert_scan_dir", _counting_upsert)
    assert await main._reconnect_pending_shares() is True
    assert created == [] and writes == []
    # The server comes back.
    conf["network_shares"]["down-1"]["port"] = listener
    assert await main._reconnect_pending_shares() is False
    assert [s["remote_path"] for s in created] == ["/m1"]
    assert _status(store)["ftp://127.0.0.1/m1"] == "ok"
    assert filesource.get_source("ftp://127.0.0.1/m1") is not None
    # A share removed from the configuration is never reconnected.
    filesource.remove_source("ftp://127.0.0.1/m1")
    del conf["network_shares"]["down-1"]
    created.clear()
    assert await main._reconnect_pending_shares() is False
    assert created == []


# ── a server's shares connect at most two at a time ─────────────────────────

class _Counting(_Src):
    live = 0
    peak = 0
    lock = None

    def is_dir(self, path):
        with _Counting.lock:
            _Counting.live += 1
            _Counting.peak = max(_Counting.peak, _Counting.live)
        try:
            return super().is_dir(path)
        finally:
            with _Counting.lock:
                _Counting.live -= 1


async def test_startup_connects_at_most_two_shares_of_a_server_at_once(env, listener):
    import threading
    store, conf, created, behaviour = env
    _Counting.lock, _Counting.live, _Counting.peak = threading.Lock(), 0, 0
    conf["network_shares"] = {f"ftp-{i}": _ftp("127.0.0.1", listener, i) for i in range(6)}
    for i in range(6):
        behaviour[f"/m{i}"] = _Counting(delay=0.15)
    await main._init_network_shares()
    assert set(_status(store).values()) == {"ok"}
    # Before: all six logged in at once (a per-IP cap of 5 refused one).
    assert _Counting.peak == 2
    assert main._SHARE_CONNECT_PER_HOST == 2


async def test_startup_logins_stay_under_a_per_ip_cap(env, tmp_path, monkeypatch):
    """Six real FTP shares on one server that allows 4 connections per IP."""
    from soniqboom.core.filesource import FTPFileSource
    store, conf, created, behaviour = env
    for i in range(6):
        (tmp_path / f"m{i}").mkdir()
    srv = FtpServer(tmp_path, max_conns=4).start()
    # The real source this time (the fixture's stub records nothing).
    monkeypatch.setattr(filesource, "create_source", lambda share, password="": FTPFileSource(
        "127.0.0.1", port=srv.port, remote_path=share["remote_path"]))
    try:
        conf["network_shares"] = {f"ftp-{i}": _ftp("127.0.0.1", srv.port, i) for i in range(6)}
        await main._init_network_shares()
        assert set(_status(store).values()) == {"ok"} and len(_status(store)) == 6
        assert srv.peak_conns <= 4
    finally:
        with filesource._FTP_POOLS_LOCK:
            pools = [filesource._FTP_POOLS.pop(k) for k in list(filesource._FTP_POOLS)
                     if k[1] == srv.port]
        for p in pools:
            p.close_all()
        srv.stop()


# ── refused credentials are not retried every health pass ───────────────────

class _Refused(_Src):
    def __init__(self):
        super().__init__(ok=False)
        self.last_error = "error_perm: 530 Login incorrect."
        self.last_error_auth = True


def _key(share):
    return getattr(filesource, "share_auth_key", lambda s: "")(share)


async def test_refused_credentials_back_off_until_a_manual_reconnect(env, listener):
    from soniqboom.api import admin
    store, conf, created, behaviour = env
    root = "ftp://127.0.0.1/m0"
    conf["network_shares"] = {"bad": _ftp("127.0.0.1", listener, 0)}
    key = _key(conf["network_shares"]["bad"])
    behaviour["/m0"] = _Refused()
    try:
        await main._init_network_shares()
        assert _status(store) == {root: "unavailable"} and len(created) == 1
        # Health passes inside the back-off try nothing (before: a refused
        # login per pass, forever) — and still count the share as down.
        for _ in range(3):
            assert await main._reconnect_pending_shares() is True
        assert len(created) == 1
        wait = filesource.share_retry_wait(key)
        assert 290 < wait <= filesource._SHARE_AUTH_BACKOFF_S
        # Each further refusal doubles the wait (capped).
        assert filesource.note_share_auth_failure(key) == 2 * filesource._SHARE_AUTH_BACKOFF_S
        # A manual Reconnect tries at once and ends the back-off …
        out = None
        try:
            await admin.reconnect_share({"id": "bad"}, _tok="t")
        except Exception as exc:        # still refused: 502, back-off re-armed
            out = exc
        assert getattr(out, "status_code", None) == 502
        assert filesource.share_retry_wait(key) > 290 and len(created) == 2
        # … and with working credentials it connects.
        behaviour["/m0"] = _Src()
        out = await admin.reconnect_share({"id": "bad"}, _tok="t")
        assert out["connected"] is True and filesource.share_retry_wait(key) == 0
        assert len(created) == 3
    finally:
        getattr(filesource, "reset_share_backoff", lambda r: None)(key)


async def test_one_refusal_spares_the_other_shares_with_those_credentials(env, listener):
    store, conf, created, behaviour = env
    conf["network_shares"] = {f"s{i}": _ftp("127.0.0.1", listener, i) for i in range(5)}
    conf["network_shares"]["other"] = {**_ftp("127.0.0.1", listener, 9), "username": "bob"}
    for i in range(5):
        behaviour[f"/m{i}"] = _Refused()
    key = _key(conf["network_shares"]["s0"])
    try:
        await main._init_network_shares()
        # Before: one refused login per share (and per retry pass).  Now the
        # first refusal holds back its four siblings; another user still tries.
        assert sorted(s["remote_path"] for s in created) == ["/m0", "/m9"]
        st = _status(store)
        assert [st[f"ftp://127.0.0.1/m{i}"] for i in range(5)] == ["unavailable"] * 5
        assert st["ftp://127.0.0.1/m9"] == "ok"
    finally:
        getattr(filesource, "reset_share_backoff", lambda r: None)(key)


async def test_a_plain_failure_is_still_retried_every_pass(env, listener):
    store, conf, created, behaviour = env
    conf["network_shares"] = {"gone": _ftp("127.0.0.1", listener, 0)}
    behaviour["/m0"] = _Src(ok=False)             # folder missing, not a login problem
    await main._init_network_shares()
    assert filesource.share_retry_wait(_key(conf["network_shares"]["gone"])) == 0
    assert await main._reconnect_pending_shares() is True
    assert len(created) == 2


async def test_the_health_monitor_stops_probing_a_share_whose_login_is_refused(env, monkeypatch):
    import types
    store, conf, created, behaviour = env
    root = "ftp://127.0.0.1/hm"
    key = "ftp://u@127.0.0.1:21"

    class _Flip(_Src):
        probes = 0

        def is_dir(self, path):
            _Flip.probes += 1
            self.last_error_auth = True          # the password was changed on the server
            return False
    src = _Flip()
    src.auth_key = key
    filesource.register_source(root, src)
    passes = []

    async def _fast_sleep(t):
        passes.append(t)
        if len(passes) > 4:
            raise asyncio.CancelledError
        await asyncio.sleep(0)
    # main's own view of asyncio only — the event loop's sleeps stay real.
    fake = types.SimpleNamespace(**{n: getattr(asyncio, n) for n in dir(asyncio)
                                    if not n.startswith("__")})
    fake.sleep = _fast_sleep
    monkeypatch.setattr(main, "asyncio", fake)
    try:
        with pytest.raises(asyncio.CancelledError):
            await main._share_health_monitor()
        # Four passes, ONE probe (a login): the rest waited out the back-off.
        assert _Flip.probes == 1
        assert filesource.share_retry_wait(key) > 0
        # Meanwhile nothing else logs in with them: the share reads as offline.
        assert filesource.get_source(root) is None
        assert filesource.credentials_refused(root)
        assert root in filesource.all_sources()
    finally:
        getattr(filesource, "reset_share_backoff", lambda r: None)(key)


def test_auth_failures_are_told_apart_from_other_errors(tmp_path):
    import ftplib

    import httpx
    from smbprotocol import exceptions as smbx
    from soniqboom.core.filesource import FTPLoginRefused, is_auth_failure
    req = httpx.Request("PROPFIND", "https://dav/x")
    # Only a refused LOGIN counts — a mid-session "530 Not logged in" doesn't.
    assert is_auth_failure(FTPLoginRefused("530 Login incorrect."))
    assert not is_auth_failure(ftplib.error_perm("530 Not logged in."))
    assert not is_auth_failure(ftplib.error_perm("550 No such file or directory"))
    assert not is_auth_failure(ftplib.error_temp("421 Too many connections"))
    # The login factory raises it — but not for a too-many-clients 530.
    srv = FtpServer(tmp_path, password="right").start()
    try:
        with pytest.raises(FTPLoginRefused):
            filesource._build_ftp_factory("127.0.0.1", srv.port, "u", "wrong", "utf-8")()
    finally:
        srv.stop()
    for msg in ("530 Sorry, the maximum number of clients (10) for this user are "
                "already connected", "530 Sorry, the maximum number of allowed "
                "clients (5) are already connected."):
        assert filesource._is_too_many_clients_error(ftplib.error_perm(msg))
    assert is_auth_failure(httpx.HTTPStatusError("x", request=req,
                                                 response=httpx.Response(401, request=req)))
    assert is_auth_failure(httpx.HTTPStatusError("x", request=req,
                                                 response=httpx.Response(429, request=req)))
    # 403 is ONE folder the account may not read — not a refused login (it
    # would take the account's other shares offline).
    assert not is_auth_failure(httpx.HTTPStatusError(
        "x", request=req, response=httpx.Response(403, request=req)))
    assert not is_auth_failure(httpx.HTTPStatusError("x", request=req,
                                                     response=httpx.Response(404, request=req)))
    assert is_auth_failure(smbx.LogonFailure())
    assert not is_auth_failure(smbx.BadNetworkName())
    assert not is_auth_failure(OSError("no route to host"))


def test_an_ftp_pool_stops_warming_up_with_refused_credentials(tmp_path):
    srv = FtpServer(tmp_path, password="right").start()
    pool = filesource._FTPConnectionPool(
        filesource._build_ftp_factory("127.0.0.1", srv.port, "u", "wrong", "utf-8"),
        max_size=3, min_size=2, keepalive_s=0.05, label="test", host="127.0.0.1",
        port=srv.port)
    good = filesource._FTPConnectionPool(
        filesource._build_ftp_factory("127.0.0.1", srv.port, "u", "right", "utf-8"),
        max_size=3, min_size=2, keepalive_s=0.05, label="test-ok", host="127.0.0.1",
        port=srv.port)
    try:
        time.sleep(0.5)
        # No warm-up before a login has worked (before: one refused login per
        # keep-alive cycle — ~20 here — forever).
        assert srv.refused_logins == 0 and srv.logins == 0
        with pytest.raises(filesource.FTPLoginRefused):
            with pool.borrow():
                pass
        time.sleep(0.5)
        assert srv.refused_logins == 1
        assert pool._auth_retry_at > time.monotonic() + 200
        # Working credentials: the first login starts the warm-up at once.
        with good.borrow():
            pass
        deadline = time.monotonic() + 3
        while len(good._idle) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(good._idle) == 2
    finally:
        pool.close_all()
        good.close_all()
        srv.stop()


# ── SMB: one server connection, shared by every share on it ─────────────────

class _FakeSmbSession:
    def __init__(self, username):
        self.username = username
        self.tree_connect_table: dict = {}
        self.dead = False                       # ended by the server


class _FakeSmbTree:
    def __init__(self, share_name):
        self.share_name = share_name
        self.stale = False                      # dropped by the server


class _FakeTransport:
    def __init__(self):
        self.connected = True
        self.closed = False

    def close(self):
        self.connected, self.closed = False, True


class _FakeSmbConn:
    def __init__(self, key, calls):
        from collections import OrderedDict
        self.key, self.calls = key, calls
        self.transport = _FakeTransport()
        self.session_table: "OrderedDict[int, _FakeSmbSession]" = OrderedDict()
        self.alive = True
        self.logoffs = 0

    def echo(self, sid=0, timeout=60):
        if not self.alive:
            raise OSError("no echo")
        return 1

    def disconnect(self, close=True, timeout=None):
        self.calls["delete"].append(self.key)
        if close:
            if any(x.dead for x in self.session_table.values()):
                raise OSError("logoff on a dead session")    # smbprotocol raises here
            self.logoffs += 1
        self.transport.close()


@pytest.fixture
def fake_smb(monkeypatch):
    """smbclient as it behaves: ONE connection per server:port (deleted for
    every share on it by ``delete_session``), sessions found by user name
    (``username=None``: the first one), tree connects cached per session by
    share name — none of them re-checked before reuse."""
    import itertools
    import sys
    import types

    from smbprotocol import exceptions as smbx
    mod = types.ModuleType("smbclient")
    pool = types.ModuleType("smbclient._pool")
    path = types.ModuleType("smbclient.path")
    conns: dict = {}
    calls = {"register": [], "delete": [], "tree": []}
    shares = {"Music", "Video"}                 # what the server exports now
    ids = itertools.count(1)

    def register_session(host, username=None, password=None, port=445):
        key = f"{host.lower()}:{port}"
        calls["register"].append((key, username))
        c = conns.get(key)
        if c is None or not c.transport.connected:
            c = conns[key] = _FakeSmbConn(key, calls)
        sess = next((x for x in c.session_table.values()
                     if username is None or x.username == username), None)
        if sess is None:
            sess = _FakeSmbSession(username)
            c.session_table[next(ids)] = sess
        return sess

    def delete_session(host, port=445):
        c = conns.pop(f"{host.lower()}:{port}", None)
        if c is not None:
            c.disconnect(close=True)

    def _tree(p, port=445, username=None, password=None):
        host, share = p.strip("\\").split("\\")[:2]
        sess = register_session(host, username, password, port)
        if sess.dead:
            raise smbx.UserSessionDeleted()
        name = f"\\\\{host}\\{share}"
        tree = next((t for t in sess.tree_connect_table.values() if t.share_name == name),
                    None)
        if tree is None:
            if share not in shares:
                raise smbx.BadNetworkName()
            tree = _FakeSmbTree(name)
            sess.tree_connect_table[next(ids)] = tree
            calls["tree"].append((f"{host.lower()}:{port}", share, sess.username))
        if tree.stale:
            raise smbx.SMBOSError(0xC00000C9, p)          # NETWORK_NAME_DELETED
        return tree

    def isdir(p, **kw):
        _tree(p, **kw)
        return True
    mod.register_session, mod.delete_session = register_session, delete_session
    pool._SMB_CONNECTIONS = conns
    path.isdir = isdir
    mod.path, mod._pool = path, pool
    monkeypatch.setitem(sys.modules, "smbclient", mod)
    monkeypatch.setitem(sys.modules, "smbclient._pool", pool)
    monkeypatch.setitem(sys.modules, "smbclient.path", path)
    monkeypatch.setattr(filesource, "_SMB_SESSION_USERS", {}, raising=False)
    monkeypatch.setattr(filesource, "_SMB_SESSION_GEN", {}, raising=False)
    return conns, calls, shares


def _until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def _tree_of(conns, key, share):
    for sess in conns[key].session_table.values():
        for t in sess.tree_connect_table.values():
            if t.share_name.endswith("\\" + share):
                return t
    return None


def test_closing_one_smb_share_keeps_the_connection_of_the_others(fake_smb):
    conns, calls, _shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "u", "p")
    usb = filesource.SMBFileSource("NAS", "USB", "u", "p")
    assert music.is_dir("/") is True
    conn = conns["nas:445"]
    assert usb.is_dir("/") is False             # an unplugged USB share
    usb.close()
    assert calls["delete"] == [] and conns["nas:445"] is conn
    assert music.is_dir("/") is True
    music.close()                               # the last user
    assert _until(lambda: calls["delete"] == ["nas:445"]) and "nas:445" not in conns


async def test_retrying_a_missing_smb_share_never_cuts_its_siblings(fake_smb, tmp_data_dir,
                                                                     monkeypatch, listener):
    conns, calls, _shares = fake_smb
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    share = {"protocol": "smb", "host": "127.0.0.1", "port": listener, "username": "u",
             "password_enc": "", "auto_connect": True}
    conf = {"network_shares": {"music": {**share, "share": "Music"},
                               "usb": {**share, "share": "USB"}}}
    monkeypatch.setattr("soniqboom.config.load_local_conf", lambda *a, **k: conf)
    before = set(filesource.all_sources())
    try:
        await main._init_network_shares()
        assert _status(store) == {"smb://127.0.0.1/Music": "ok",
                                  "smb://127.0.0.1/USB": "unavailable"}
        conn = conns[f"127.0.0.1:{listener}"]
        for _ in range(3):
            assert await main._reconnect_pending_shares() is True
        # Before: every pass deleted the server connection Music runs on.
        assert calls["delete"] == []
        assert conns[f"127.0.0.1:{listener}"] is conn and conn.transport.connected
        assert filesource.get_source("smb://127.0.0.1/Music").is_dir("/") is True
        # (and it is reached on ITS port, not 445)
        assert "127.0.0.1:445" not in conns
    finally:
        for root in set(filesource.all_sources()) - before:
            filesource.remove_source(root)


def test_reconnect_rebuilds_a_dead_smb_connection_for_every_share(fake_smb):
    conns, calls, _shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "alice", "p")
    video = filesource.SMBFileSource("NAS", "Video", "bob", "q")
    assert music.is_dir("/") and video.is_dir("/")
    # Alive: a reconnect of one share leaves the shared connection alone.
    conn = conns["nas:445"]
    assert music.reconnect() is True
    assert calls["delete"] == [] and conns["nas:445"] is conn
    # Dead (no ECHO answer): rebuilt — and the sibling re-registers with ITS
    # credentials on its next call instead of riding a deleted connection.
    conn.alive = False
    calls["register"].clear()
    assert music.reconnect() is True
    assert conn.transport.closed and conn.logoffs == 0     # dead: socket closed only
    assert conns["nas:445"] is not conn
    assert video.is_dir("/") is True
    assert ("nas:445", "bob") in calls["register"]


def test_a_share_the_server_dropped_comes_back_without_cutting_its_siblings(fake_smb):
    conns, calls, shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "u", "p")
    usb = filesource.SMBFileSource("NAS", "USB", "u", "p")
    shares.add("USB")
    assert music.is_dir("/") and usb.is_dir("/")
    music_tree = _tree_of(conns, "nas:445", "Music")
    # The USB disk is re-plugged: the server drops the share's tree connect.
    _tree_of(conns, "nas:445", "USB").stale = True
    # Before: every call failed until a restart (smbclient reuses the cached
    # tree, and a reconnect with a live sibling changed nothing).
    assert usb.is_dir("/") is True
    fresh = _tree_of(conns, "nas:445", "USB")
    assert fresh is not None and not fresh.stale
    # A reconnect (health monitor) leaves a LIVE tree alone — in-flight reads
    # on it keep working — and so does a late failure of the old tree.
    assert usb.reconnect() is True
    assert _tree_of(conns, "nas:445", "USB") is fresh
    assert calls["delete"] == []
    assert _tree_of(conns, "nas:445", "Music") is music_tree
    assert music.is_dir("/") is True


def test_a_late_failure_never_drops_the_fresh_tree(fake_smb):
    conns, calls, shares = fake_smb
    shares.add("USB")
    usb = filesource.SMBFileSource("NAS", "USB", "u", "p")
    assert usb.is_dir("/")
    conn = conns["nas:445"]
    stale = usb._share_trees(conn)
    _tree_of(conns, "nas:445", "USB").stale = True
    assert usb.is_dir("/") is True                    # healed: a fresh tree
    fresh = _tree_of(conns, "nas:445", "USB")
    # A call that started on the OLD tree reports its failure late.
    from smbprotocol import exceptions as smbx
    assert usb._heal(smbx.SMBOSError(0xC00000C9, "x"), conn, stale) is True
    assert _tree_of(conns, "nas:445", "USB") is fresh


def test_closing_the_last_share_logs_off_and_always_closes_the_socket(fake_smb):
    conns, calls, _shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "u", "p")
    assert music.is_dir("/")
    conn = conns["nas:445"]
    t0 = time.monotonic()
    music.close()
    assert time.monotonic() - t0 < 0.5            # never waits on the server
    assert "nas:445" not in conns
    assert _until(lambda: conn.logoffs == 1 and conn.transport.closed)
    # A logoff that fails (dead session) still closes the socket.
    video = filesource.SMBFileSource("NAS", "Video", "u", "p")
    assert video.is_dir("/")
    conn = conns["nas:445"]
    for sess in conn.session_table.values():
        sess.dead = True
    video.close()
    assert _until(lambda: conn.transport.closed) and "nas:445" not in conns
    # A server that no longer answers (half-open TCP): no logoff attempt at
    # all — it would wait without a timeout — just the socket, at once.
    film = filesource.SMBFileSource("NAS", "Video", "u", "p")
    assert film.is_dir("/")
    conn = conns["nas:445"]
    conn.alive = False
    film.close()
    assert _until(lambda: conn.transport.closed) and conn.logoffs == 0


def test_closing_a_connection_wakes_the_requests_still_waiting_on_it(fake_smb):
    """smbprotocol's receive() waits without a timeout and nothing signals it
    when the socket closes: a stream of another share on the same server hung
    forever when a heal dropped the connection under it."""
    import threading
    import types

    from smbprotocol import exceptions as smbx
    conns, calls, _shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "u", "p")
    assert music.is_dir("/")
    conn = conns["nas:445"]
    waiting = types.SimpleNamespace(response_event=threading.Event())
    conn.outstanding_requests = {41: waiting}           # another share's read
    for sess in conn.session_table.values():
        sess.dead = True
    assert music.is_dir("/") is True                    # heal: connection dropped
    assert waiting.response_event.is_set() and conn.transport.closed
    assert music._heal(smbx.UserSessionDeleted(), conn, []) is True   # late: no-op
    assert conns["nas:445"] is not conn


def test_a_session_the_server_ended_is_rebuilt(fake_smb):
    conns, calls, _shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "u", "p")
    video = filesource.SMBFileSource("NAS", "Video", "u", "p")
    assert music.is_dir("/") and video.is_dir("/")
    old = conns["nas:445"]
    for sess in old.session_table.values():
        sess.dead = True                        # kicked by the admin
    assert music.is_dir("/") is True            # healed: a fresh session
    assert video.is_dir("/") is True
    # One rebuild; the dead connection's socket closed without a logoff (a
    # logoff on the dead session raises before the socket closes — a leak).
    assert old.transport.closed and old.logoffs == 0 and calls["delete"] == []
    assert conns["nas:445"] is not old


def test_every_smb_call_uses_its_shares_port_and_user(fake_smb):
    conns, calls, _shares = fake_smb
    music = filesource.SMBFileSource("NAS", "Music", "alice", "p", port=1445)
    video = filesource.SMBFileSource("NAS", "Video", "bob", "q", port=1445)
    assert music.is_dir("/") and video.is_dir("/")
    # Before: the calls passed no port / user — they went to nas:445 under a
    # credential-less session, and a second user rode the first one's session.
    assert set(conns) == {"nas:1445"}
    assert sorted(calls["tree"]) == [("nas:1445", "Music", "alice"),
                                     ("nas:1445", "Video", "bob")]


async def test_a_slow_first_share_holds_back_only_its_own_credentials(env, listener):
    store, conf, created, behaviour = env
    conf["network_shares"] = {
        "slow": {**_ftp("127.0.0.1", listener, 0), "username": "a"},
        **{f"b{i}": {**_ftp("127.0.0.1", listener, i + 1), "username": "b"}
           for i in range(3)},
    }
    behaviour["/m0"] = _Src(delay=1.0)
    for i in range(1, 4):
        behaviour[f"/m{i}"] = _Src(delay=0.1)
    t0 = time.monotonic()
    done_at = {}
    real = main._connect_share

    async def _timed(share_id, *a, **k):
        r = await real(share_id, *a, **k)
        done_at[share_id] = time.monotonic() - t0
        return r
    import unittest.mock as um
    with um.patch.object(main, "_connect_share", _timed):
        await main._init_network_shares()
    # B's siblings don't wait for A's slow first probe (a global barrier did).
    assert max(done_at[f"b{i}"] for i in range(3)) < 0.8 < done_at["slow"]


async def test_scan_and_share_list_respect_the_back_off(env, tmp_data_dir):
    from soniqboom.api import admin
    store, conf, created, behaviour = env
    share = {**_ftp("127.0.0.1", 21, 0), "username": "u"}
    conf["network_shares"] = {"s": share}
    root = "ftp://127.0.0.1/m0"
    key = filesource.share_auth_key(share)
    src = _Src()
    src.auth_key = key
    filesource.register_source(root, src)
    filesource.note_share_auth_failure(key)
    try:
        out = await admin._scan_dirs_split([root])
        # Before: get_source() was None, so the scan made a new source and
        # logged in with the refused credentials again.
        assert out["started"] == [] and created == []
        assert "refused" in out["skipped"][0]["reason"]
        listed = (await admin.list_shares(_tok="t"))["shares"][0]
        assert listed["auth_refused"] is True and listed["auth_retry_in_s"] > 290
        assert listed["connected"] is False
    finally:
        filesource.reset_share_backoff(key)


async def test_a_share_refused_before_it_ever_connected_stays_untried(env, listener):
    from soniqboom.api import admin
    from soniqboom.api.stream import _offline_error
    store, conf, created, behaviour = env
    conf["network_shares"] = {"bad": _ftp("127.0.0.1", listener, 0)}
    root = "ftp://127.0.0.1/m0"
    key = _key(conf["network_shares"]["bad"])
    behaviour["/m0"] = _Refused()
    try:
        await main._init_network_shares()
        assert filesource.get_source(root) is None and len(created) == 1
        # A manual scan / reindex doesn't log in with them through the
        # "reconnect from config" path, and a play says why.
        out = await admin._scan_dirs_split([root])
        assert out["started"] == [] and len(created) == 1
        assert "refused" in out["skipped"][0]["reason"]
        assert "Sign-in refused" in _offline_error(root).detail
    finally:
        filesource.reset_share_backoff(key)
