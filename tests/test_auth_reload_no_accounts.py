# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``POST /api/auth/reload`` while NO account is loaded (recovery, fresh
install) takes only the CLI's run token, and a users.json put back on disk is
picked up by the server itself (``UserStore.check_restored``) without ever
moving a file aside.  In-process only (``TestClient`` with a chosen peer) —
no setadm, no server process."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from soniqboom.core import startup_status
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

_TOK = "the-run-token-0123456789abcdefghij"
_HDR = startup_status.RELOAD_TOKEN_HEADER
_PW = "Longenough-1"


def _good_users_bytes(tmp_path_factory) -> bytes:
    d = tmp_path_factory.mktemp("src")
    UserStore(d).create("rescue", _PW, role="admin")
    return (d / "users.json").read_bytes()


@pytest.fixture()
def good(tmp_path_factory):
    return _good_users_bytes(tmp_path_factory)


def _recovery_store(d):
    st = UserStore(d)
    st.create("admin1", _PW, role="admin")
    (d / "users.json").write_bytes(b"{")
    (d / "users.json.bak").write_bytes(b"\x00")
    server = UserStore(d, persist_sessions=False)
    assert server.recovery_required and server.count() == 0
    return server


@pytest.fixture()
def gate(tmp_data_dir, monkeypatch):
    from soniqboom.main import app
    monkeypatch.setattr(startup_status, "_status",
                        {**startup_status._status, "reload_token": _TOK})

    class G:
        data_dir = tmp_data_dir

        @staticmethod
        def use(store):
            monkeypatch.setattr(users_mod, "_instance", store)
            return store

        @staticmethod
        def post(peer, token=None, headers=None):
            h = dict(headers or {})
            if token is not None:
                h[_HDR] = token
            return TestClient(app, client=(peer, 50123)).post("/api/auth/reload", headers=h)
    return G


def _corrupt_files(d):
    return sorted(p.name for p in d.iterdir() if ".corrupt-" in p.name)


# ── the endpoint gate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("peer", ["192.168.1.50", "127.0.0.1", "::1"])
def test_recovery_refuses_a_tokenless_reload_and_leaves_a_half_copied_file_alone(
        gate, good, peer):
    """The admin is copying users.json back in (``cp`` is not atomic) and an
    anonymous caller hits reload mid-copy: before, the half file was read as
    corrupt and moved aside, so the restore silently failed."""
    store = gate.use(_recovery_store(gate.data_dir))
    before = _corrupt_files(gate.data_dir)
    (gate.data_dir / "users.json").write_bytes(good[: len(good) // 2])   # mid-copy
    r = gate.post(peer)
    assert r.status_code == 403
    assert (gate.data_dir / "users.json").read_bytes() == good[: len(good) // 2]
    assert _corrupt_files(gate.data_dir) == before          # nothing moved aside
    assert store.recovery_required


def test_recovery_reload_with_the_run_token_from_loopback(gate, good):
    store = gate.use(_recovery_store(gate.data_dir))
    (gate.data_dir / "users.json").write_bytes(good)
    r = gate.post("127.0.0.1", token=_TOK)
    assert r.status_code == 200, r.text
    assert r.json()["has_any_admin"] is True and not store.recovery_required


@pytest.mark.parametrize("peer,headers,code", [
    ("192.168.1.50", {}, 403),                                  # remote, even with the token
    ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}, 403),     # a reverse proxy on this host
    ("127.0.0.1", {}, 409),                                     # wrong token
])
def test_recovery_reload_needs_loopback_and_this_runs_token(gate, good, peer, headers, code):
    store = gate.use(_recovery_store(gate.data_dir))
    (gate.data_dir / "users.json").write_bytes(good)
    tok = "some-other-runs-token-xxxxxxxx" if code == 409 else _TOK
    assert gate.post(peer, token=tok, headers=headers).status_code == code
    assert store.recovery_required


def test_fresh_install_refuses_a_tokenless_reload(gate):
    store = gate.use(UserStore(gate.data_dir))
    UserStore(gate.data_dir).create("firstadmin", _PW, role="admin")   # behind its back
    assert gate.post("192.168.1.50").status_code == 403
    assert gate.post("127.0.0.1").status_code == 403
    assert store.get_by_username("firstadmin") is None
    assert gate.post("127.0.0.1", token=_TOK).status_code == 200
    assert store.get_by_username("firstadmin") is not None


def test_accounts_but_no_admin_still_takes_a_tokenless_reload(gate):
    """Unchanged: accounts are loaded, none is an enabled admin."""
    store = gate.use(UserStore(gate.data_dir))
    store.create("viewer", _PW, role="readonly")
    UserStore(gate.data_dir).create("firstadmin", _PW, role="admin")
    assert gate.post("192.168.1.50").status_code == 200
    assert store.get_by_username("firstadmin") is not None


def test_with_an_admin_only_the_cli_token_reloads(gate):
    """Unchanged: the CLI path (loopback + token) with an admin loaded."""
    store = gate.use(UserStore(gate.data_dir))
    store.create("root1", _PW, role="admin")
    UserStore(gate.data_dir).create("alice", _PW, role="readonly")
    assert gate.post("127.0.0.1").status_code == 403
    assert store.get_by_username("alice") is None
    assert gate.post("127.0.0.1", token=_TOK).status_code == 200
    assert store.get_by_username("alice") is not None


# ── picking a restored users.json up without a request ──────────────────────

def test_check_restored_waits_for_a_complete_file_and_never_moves_one(tmp_path, good):
    store = _recovery_store(tmp_path)
    before = _corrupt_files(tmp_path)
    assert store.check_restored() is False                  # nothing changed on disk
    (tmp_path / "users.json").write_bytes(good[:40])         # half copied
    assert store.check_restored() is False
    assert (tmp_path / "users.json").exists() and _corrupt_files(tmp_path) == before
    assert store.recovery_required
    (tmp_path / "users.json").write_bytes(good)              # copy finished
    assert store.check_restored() is True
    assert not store.recovery_required and store.authenticate("rescue", _PW)
    assert _corrupt_files(tmp_path) == before


def test_check_restored_reads_only_when_the_files_changed(tmp_path, good, monkeypatch):
    store = _recovery_store(tmp_path)
    reads = []
    real = UserStore._read_users_file

    def spy(path):
        reads.append(path.name)
        return real(path)
    monkeypatch.setattr(UserStore, "_read_users_file", staticmethod(spy))
    for _ in range(5):
        store.check_restored()
    assert reads == []                                       # warm: stat only
    (tmp_path / "users.json").write_bytes(b"[")              # cold: a change
    store.check_restored()
    assert reads == ["users.json"]
    store.check_restored()
    assert reads == ["users.json"]                           # same bytes, no re-read
    (tmp_path / "users.json").chmod(0o640)                   # a permission fix is a change
    store.check_restored()
    assert reads == ["users.json", "users.json"]


def test_check_restored_uses_the_backup_only_without_a_main_file(tmp_path, good):
    store = _recovery_store(tmp_path)
    (tmp_path / "users.json").unlink(missing_ok=True)
    (tmp_path / "users.json.bak").write_bytes(good)
    assert store.check_restored() is True
    assert store.get_by_username("rescue") is not None


def test_check_restored_ignores_a_backup_while_a_main_file_is_being_copied(tmp_path, good):
    store = _recovery_store(tmp_path)
    (tmp_path / "users.json.bak").write_bytes(good)
    (tmp_path / "users.json").write_bytes(good[:30])
    assert store.check_restored() is False and store.recovery_required


def test_check_restored_does_nothing_once_accounts_are_loaded(tmp_path, good):
    store = UserStore(tmp_path)
    store.create("admin1", _PW, role="admin")
    (tmp_path / "users.json").write_bytes(good)              # someone else's file
    assert store.check_restored() is False
    assert store.get_by_username("admin1") and store.get_by_username("rescue") is None


def test_the_lifespan_watch_loads_a_restored_file(tmp_path, good, monkeypatch):
    from soniqboom import main
    store = _recovery_store(tmp_path)
    monkeypatch.setattr(users_mod, "_instance", store)
    monkeypatch.setattr(main, "_USERS_RESTORED_POLL_SEC", 0.02)

    async def run():
        task = asyncio.create_task(main._watch_for_restored_accounts())
        try:
            await asyncio.sleep(0.1)
            assert store.recovery_required
            (tmp_path / "users.json").write_bytes(good)
            for _ in range(200):
                if not store.recovery_required:
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert not store.recovery_required
    assert json.loads((tmp_path / "users.json").read_text())["users"][0]["username"] == "rescue"


def test_a_restored_file_brings_its_sign_ins_back_like_a_restart(tmp_path, good):
    """Browsers signed in before users.json broke stay signed in once it is
    restored — as after a restart with the good file."""
    src = tmp_path / "before"
    src.mkdir()
    st0 = UserStore(src, persist_sessions=True)
    st0.create("keeper", _PW, role="admin")
    tok, _ = st0.issue_session(st0.get_by_username("keeper").id)
    assert st0.flush_sessions()
    good_users = (src / "users.json").read_bytes()
    (src / "users.json").write_bytes(b"{")
    (src / "users.json.bak").write_bytes(b"{")
    server = UserStore(src, persist_sessions=True)
    assert server.recovery_required and server.lookup_session(tok) is None
    (src / "users.json").write_bytes(good_users)
    assert server.check_restored() is True
    assert server.lookup_session(tok) is not None
    assert server.lookup_session(tok).username == "keeper"
