# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in sessions survive a restart (sessions.json: token hashes only,
0600), and every revocation the in-memory store makes — sign-out, password
change, user delete, demotion, disable — is on disk before the call returns."""
import hashlib
import json
import threading
import time

import pytest

from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

PW = "Longenough-1"


def _server(d):
    return UserStore(d, persist_sessions=True)


def _rows(d):
    return json.loads((d / "sessions.json").read_text())["sessions"]


@pytest.fixture
def setup(tmp_path):
    st = _server(tmp_path)
    a = st.create("admin1", PW, role="admin")
    b = st.create("bob", PW, role="admin")
    c = st.create("carol", PW, role="readonly")
    return tmp_path, st, a, b, c


def test_a_session_survives_a_restart_and_only_its_hash_is_on_disk(setup):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    assert st.flush_sessions()
    raw = (d / "sessions.json").read_text()
    assert tok not in raw
    assert hashlib.sha256(tok.encode()).hexdigest() in raw
    assert (d / "sessions.json").stat().st_mode & 0o777 == 0o600
    st2 = _server(d)                                  # "restart"
    assert st2.lookup_session(tok).id == a.id
    assert st2.lookup_session("not-a-token") is None


def test_new_sessions_are_written_in_the_background_coalesced(setup, monkeypatch):
    d, st, a, b, c = setup
    monkeypatch.setattr(users_mod, "_SESSION_WRITE_DELAY_SEC", 0.2)
    writes = []
    real = users_mod.atomic_write_bytes

    def spy(path, data, mode=0o600):
        if path.name == "sessions.json":
            writes.append(threading.current_thread().name)
        return real(path, data, mode)
    monkeypatch.setattr(users_mod, "atomic_write_bytes", spy)
    toks = [st.issue_session(a.id)[0] for _ in range(50)]
    assert writes == []                               # nothing on the caller's thread
    deadline = time.time() + 5
    while not writes and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    assert len(writes) == 1 and writes[0] != threading.main_thread().name
    st2 = _server(d)
    assert all(st2.lookup_session(t) for t in toks)


def test_sign_out_is_on_disk_before_it_returns(setup):
    d, st, a, b, c = setup
    t1, _ = st.issue_session(a.id)
    t2, _ = st.issue_session(a.id)
    st.flush_sessions()
    st.revoke_session(t1)
    hashes = {r["h"] for r in _rows(d)}               # no flush in between
    assert hashlib.sha256(t1.encode()).hexdigest() not in hashes
    assert hashlib.sha256(t2.encode()).hexdigest() in hashes
    st2 = _server(d)
    assert st2.lookup_session(t1) is None and st2.lookup_session(t2) is not None


def test_password_change_revokes_that_users_sessions_on_disk(setup):
    d, st, a, b, c = setup
    ta, _ = st.issue_session(a.id)
    tb, _ = st.issue_session(b.id)
    st.flush_sessions()
    st.set_password(a.id, "Another-pass-2")
    assert {r["uid"] for r in _rows(d)} == {b.id}
    st2 = _server(d)
    assert st2.lookup_session(ta) is None and st2.lookup_session(tb).id == b.id


def test_user_delete_revokes_on_disk(setup):
    d, st, a, b, c = setup
    tc, _ = st.issue_session(c.id)
    st.flush_sessions()
    st.delete(c.id)
    assert c.id not in {r["uid"] for r in _rows(d)}
    assert _server(d).lookup_session(tc) is None


@pytest.mark.parametrize("change,purged", [
    ({"role": "readonly"}, True),     # demotion
    ({"enabled": False}, True),       # disable
    ({"role": "admin"}, False),       # same as the in-memory rule
    ({"display_name": "Bobby"}, False),
])
def test_update_revocations_match_the_in_memory_rule(setup, change, purged):
    d, st, a, b, c = setup
    tb, _ = st.issue_session(b.id)
    st.flush_sessions()
    st.update(b.id, **change)
    on_disk = b.id in {r["uid"] for r in _rows(d)}
    assert on_disk is (not purged)
    assert (st.lookup_session(tb) is None) is purged


def test_expired_sessions_are_neither_kept_nor_restored(setup, monkeypatch):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    st.flush_sessions()
    future = time.time() + users_mod._SESSION_TTL_SEC + 5
    monkeypatch.setattr(users_mod.time, "time", lambda: future)
    st2 = _server(d)
    assert st2.lookup_session(tok) is None
    assert st.lookup_session(tok) is None             # the live store expires it too


def test_a_password_changed_by_another_process_ends_the_session(setup):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    st.flush_sessions()
    cli = UserStore(d)                                # soniqboom-setadm
    cli.set_password(a.id, "Changed-by-cli-3")
    assert not (d / "sessions.json").read_text().count("Changed")
    st.reload()
    assert st.lookup_session(tok) is None             # live server, after reload
    assert _server(d).lookup_session(tok) is None     # and after a restart


def test_the_cli_store_never_writes_sessions(tmp_path):
    cli = UserStore(tmp_path)
    u = cli.create("admin1", PW, role="admin")
    tok, _ = cli.issue_session(u.id)
    cli.revoke_session(tok)
    cli.set_password(u.id, "Another-pass-2")
    assert not (tmp_path / "sessions.json").exists()


def test_sliding_expiry_is_written_once_it_moves_far_enough(setup, monkeypatch):
    d, st, a, b, c = setup
    monkeypatch.setattr(users_mod, "_SESSION_WRITE_DELAY_SEC", 0.05)
    tok, _ = st.issue_session(a.id)
    time.sleep(0.3)                                   # the issue's own write lands
    exp0 = _rows(d)[0]["exp"]
    st.lookup_session(tok)                            # a second later: not worth a write
    time.sleep(0.3)
    assert _rows(d)[0]["exp"] == exp0
    later = time.time() + 2 * users_mod._SESSION_EXPIRY_PERSIST_SEC
    real_time = time.time
    monkeypatch.setattr(users_mod.time, "time", lambda: later)
    st.lookup_session(tok)
    monkeypatch.setattr(users_mod.time, "time", real_time)
    deadline = real_time() + 5
    while _rows(d)[0]["exp"] == exp0 and real_time() < deadline:
        time.sleep(0.05)
    assert _rows(d)[0]["exp"] > exp0


def test_an_unreadable_sessions_file_signs_everyone_out_without_failing(setup):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    st.flush_sessions()
    (d / "sessions.json").write_bytes(b"\x00{not json")
    st2 = _server(d)
    assert st2.lookup_session(tok) is None and st2.count() == 3


def test_recovery_never_overwrites_the_sessions_file(setup):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    st.flush_sessions()
    before = (d / "sessions.json").read_bytes()
    (d / "users.json").write_bytes(b"{")
    (d / "users.json.bak").write_bytes(b"{")
    st2 = _server(d)
    assert st2.recovery_required and st2.lookup_session(tok) is None
    st2.flush_sessions()
    assert (d / "sessions.json").read_bytes() == before


def test_a_far_future_expiry_on_disk_is_clamped(setup):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    st.flush_sessions()
    rows = _rows(d)
    rows[0]["exp"] = time.time() + 10 * 365 * 86400
    (d / "sessions.json").write_text(json.dumps({"version": 1, "sessions": rows}))
    st2 = _server(d)
    assert st2.lookup_session(tok) is not None
    h = hashlib.sha256(tok.encode()).hexdigest()
    assert st2._sessions[h]["expiry"] <= time.time() + users_mod._SESSION_TTL_SEC + 1


def test_a_new_server_store_retires_the_previous_one(tmp_path, monkeypatch):
    """The bundled app restarts the server in the same process: the old
    store writes what it holds, then a late timer of it never overwrites the
    new store's sessions (e.g. bringing back a session the new one revoked)."""
    monkeypatch.setattr(users_mod, "_instance", None)
    old = users_mod.init_user_store(tmp_path)
    u = old.create("admin1", PW, role="admin")
    tok, _ = old.issue_session(u.id)               # write pending in ``old``
    new = users_mod.init_user_store(tmp_path)
    assert new.lookup_session(tok) is not None      # flushed before the new load
    new.revoke_session(tok)
    old._sess_dirty = True
    old._timer_write()                              # a late timer of the old store
    assert _server(tmp_path).lookup_session(tok) is None


def test_a_failed_revocation_write_is_retried(setup, monkeypatch):
    d, st, a, b, c = setup
    tok, _ = st.issue_session(a.id)
    st.flush_sessions()
    monkeypatch.setattr(users_mod, "_SESSION_WRITE_RETRY_SEC", 0.2)
    real = users_mod.atomic_write_bytes
    fail = {"n": 1}

    def flaky(path, data, mode=0o600):
        if path.name == "sessions.json" and fail["n"]:
            fail["n"] -= 1
            raise OSError(28, "No space left on device")
        return real(path, data, mode)
    monkeypatch.setattr(users_mod, "atomic_write_bytes", flaky)
    st.revoke_session(tok)                            # its write fails…
    h = hashlib.sha256(tok.encode()).hexdigest()
    assert h in {r["h"] for r in _rows(d)}
    deadline = time.time() + 5                        # …and is retried in the background
    while h in {r["h"] for r in _rows(d)} and time.time() < deadline:
        time.sleep(0.05)
    assert h not in {r["h"] for r in _rows(d)}
    assert _server(d).lookup_session(tok) is None
