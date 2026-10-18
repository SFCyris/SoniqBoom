# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in sessions: bound to the password a sign-in verified, capped per
account, kept on the device that changes its own password, refreshed in the
browser as the expiry slides; the lockout counts parallel guesses exactly;
recovery restores start the lockout over; a role change signs out only on a
privilege drop.  In-process (``TestClient``, no lifespan) — no server."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

PW = "Old-pass-123!"
NEW = "New-pass-456!"


@pytest.fixture
def env(tmp_data_dir, monkeypatch):
    from soniqboom.main import app
    st = UserStore(tmp_data_dir, persist_sessions=True)
    monkeypatch.setattr(users_mod, "_instance", st)
    admin = st.create("admin", PW, "admin")
    bob = st.create("bob", PW, "readonly")

    class E:
        store = st
        data_dir = tmp_data_dir

        @staticmethod
        def client(cookie=None):
            c = TestClient(app, base_url="http://127.0.0.1:8471")
            if cookie:
                c.cookies.set("sb_session", cookie)
            return c

        @staticmethod
        def login(name, pw):
            r = E.client().post("/api/auth/login", json={"username": name, "password": pw})
            return r.status_code, r.cookies.get("sb_session")
    E.admin, E.bob = admin, bob
    return E


# ── Finding 3: a reset racing a sign-in ends that sign-in's session ──────

def test_a_password_reset_during_a_sign_in_ends_its_session(env, monkeypatch):
    real = users_mod.verify_password
    fired = []

    def verify_then_reset(password, stored):
        ok = real(password, stored)
        if ok and password == PW and stored == env.store.get(env.bob.id).password_hash \
                and not fired:
            fired.append(1)
            env.store.set_password(env.bob.id, NEW)     # the admin's reset lands now
        return ok
    monkeypatch.setattr(users_mod, "verify_password", verify_then_reset)
    status, cookie = env.login("bob", PW)
    assert fired
    if status == 200:
        assert env.client(cookie).get("/api/auth/me").status_code == 401
    else:
        assert status == 401
    assert env.login("bob", NEW)[0] == 200


# ── Finding 6: sessions per account are capped ──────────────────────────

def test_sessions_per_account_are_capped_least_recently_used_first(env):
    st = env.store
    cap = getattr(users_mod, "_MAX_SESSIONS_PER_USER", 50)
    toks = [st.issue_session(env.bob.id)[0] for _ in range(cap)]
    time.sleep(0.01)
    assert st.lookup_session(toks[1]) is not None          # recently used now
    more = [st.issue_session(env.bob.id)[0] for _ in range(cap)]
    mine = [s for s in st._sessions.values() if s["user_id"] == env.bob.id]
    assert len(mine) == cap
    assert all(st.lookup_session(t) is not None for t in more[-5:])
    assert st.lookup_session(toks[0]) is None
    # another account is untouched
    assert st.lookup_session(st.issue_session(env.admin.id)[0]) is not None


def test_display_name_over_the_limit_is_refused_by_the_api(env, monkeypatch):
    from soniqboom import config
    monkeypatch.setattr(config, "is_self_registration_enabled", lambda: True)
    r = env.client().post("/api/auth/register", json={
        "username": "mallory", "password": PW, "display_name": "x" * 1_000_000})
    assert r.status_code in (400, 413, 422)
    assert env.store.get_by_username("mallory") is None


def test_self_registration_is_rate_limited(env, monkeypatch):
    from soniqboom import config
    from soniqboom.api import users as api_users
    monkeypatch.setattr(config, "is_self_registration_enabled", lambda: True)
    if hasattr(api_users, "_register_log"):
        api_users._register_log.clear()
    codes = []
    for i in range(14):
        r = env.client().post("/api/auth/register",
                              json={"username": f"reg{i}", "password": PW})
        codes.append(r.status_code)
    assert codes[:10] == [200] * 10
    assert set(codes[10:]) == {429}
    assert env.store.get_by_username("reg13") is None


# ── Finding 8: your own password change keeps this device signed in ─────

def test_own_password_change_keeps_this_session_and_ends_the_others(env):
    _, mine = env.login("bob", PW)
    _, other = env.login("bob", PW)
    r = env.client(mine).post("/api/auth/change-password",
                              json={"current_password": PW, "new_password": NEW})
    assert r.status_code == 200
    assert env.client(mine).get("/api/auth/me").status_code == 200
    assert env.client(other).get("/api/auth/me").status_code == 401
    assert env.login("bob", PW)[0] == 401 and env.login("bob", NEW)[0] == 200
    # … and the kept session survives a restart (bound to the new password)
    env.store.flush_sessions()
    again = UserStore(env.data_dir, persist_sessions=True)
    assert again.lookup_session(mine) is not None
    assert again.lookup_session(other) is None


def test_admin_reset_of_another_user_ends_all_their_sessions(env):
    _, adm = env.login("admin", PW)
    _, bobs = env.login("bob", PW)
    r = env.client(adm).post(f"/api/users/{env.bob.id}/password", json={"new_password": NEW})
    assert r.status_code == 200
    assert env.client(bobs).get("/api/auth/me").status_code == 401
    assert env.client(adm).get("/api/auth/me").status_code == 200


# ── Finding 9: the cookie follows the sliding expiry ────────────────────

def test_the_session_cookie_is_sent_again_about_once_an_hour(env, monkeypatch):
    _, tok = env.login("bob", PW)
    c = env.client(tok)
    r = c.get("/api/library/counts")
    assert "sb_session" not in r.headers.get("set-cookie", "")      # just issued
    # an hour later
    rec = env.store._sessions[users_mod._token_hash(tok)]
    rec["cookie_at"] = time.time() - 3601
    r = c.get("/api/auth/me")
    sc = r.headers.get("set-cookie", "")
    assert sc.startswith("sb_session=" + tok) and "Max-Age=604800" in sc and "HttpOnly" in sc
    assert "SameSite=lax" in sc or "SameSite=Lax" in sc
    r = c.get("/api/auth/me")                                         # coalesced
    assert "sb_session" not in r.headers.get("set-cookie", "")


def test_a_restored_session_gets_its_cookie_again_on_first_use(env):
    _, tok = env.login("bob", PW)
    env.store.flush_sessions()
    st2 = UserStore(env.data_dir, persist_sessions=True)
    users_mod._instance = st2
    r = env.client(tok).get("/api/auth/me")
    assert r.status_code == 200
    assert r.headers.get("set-cookie", "").startswith("sb_session=" + tok)


# ── Finding 12: parallel guesses don't get past the lockout ─────────────

def _hold_wrong_guesses(env, monkeypatch):
    """verify_password, with every WRONG guess at bob's real hash held until
    ``release`` is set (they are all "in flight" at once)."""
    real = users_mod.verify_password
    held, release = [], threading.Event()
    bob_hash = env.store.get(env.bob.id).password_hash

    def verify(password, stored):
        if stored == bob_hash and password.startswith("wrong"):
            held.append(password)
            release.wait(30)
        return real(password, stored)
    monkeypatch.setattr(users_mod, "verify_password", verify)
    return held, release


def test_parallel_guesses_get_no_more_checks_than_serial_ones(env, monkeypatch):
    st = env.store
    n_max = users_mod._LOCKOUT_MAX_ATTEMPTS
    held, release = _hold_wrong_guesses(env, monkeypatch)
    ths = [threading.Thread(target=st.authenticate, args=("bob", f"wrong-{i}"))
           for i in range(n_max * 3)]
    for t in ths:
        t.start()
    deadline = time.time() + 20
    while time.time() < deadline and len(held) + sum(not t.is_alive() for t in ths) < len(ths):
        time.sleep(0.02)
    in_flight = len(held)
    # the QA's case: the RIGHT password while those guesses are still running
    right = st.authenticate("bob", PW)
    release.set()
    for t in ths:
        t.join(60)
    assert in_flight <= n_max, f"{in_flight} guesses checked at once (max {n_max})"
    assert right is None
    assert st.is_locked("bob")


def test_a_success_within_the_budget_still_signs_in(env):
    st = env.store
    for i in range(users_mod._LOCKOUT_MAX_ATTEMPTS - 1):
        assert st.authenticate("bob", f"wrong-{i}") is None
    assert st.authenticate("bob", PW) is not None
    assert not st.is_locked("bob")


# ── Finding 14: a recovery restore starts the lockout over ──────────────

def test_lockout_counted_during_recovery_is_cleared_when_accounts_return(env, tmp_path):
    good = (env.data_dir / "users.json").read_bytes()
    (env.data_dir / "users.json").write_bytes(b"{")
    (env.data_dir / "users.json.bak").write_bytes(b"{")
    st = UserStore(env.data_dir, persist_sessions=True)
    assert st.recovery_required
    for _ in range(30):                     # a Subsonic app keeps polling meanwhile
        st.authenticate_subsonic_password("bob", PW)
    assert st.is_locked("bob", "p")
    (env.data_dir / "users.json").write_bytes(good)
    assert st.check_restored() is True
    assert not st.is_locked("bob", "p")
    assert st.authenticate_subsonic_password("bob", PW) is not None


# ── Code-read: a role change signs out only when privileges drop ────────

@pytest.mark.parametrize("start,to,ends", [
    ("readonly", "edit", False), ("readonly", "readonly", False), ("edit", "admin", False),
    ("edit", "readonly", True), ("admin", "edit", True), ("admin", "readonly", True),
])
def test_role_change_revokes_only_on_a_privilege_drop(env, start, to, ends):
    st = env.store
    u = st.create(f"r-{start}-{to}", PW, start)
    tok, _ = st.issue_session(u.id)
    st.update(u.id, role=to)
    assert (st.lookup_session(tok) is None) is ends


def test_demotion_edit_to_readonly_closes_their_websocket(env):
    from soniqboom.api import users as api_users
    st = env.store
    ed = st.create("ed", PW, "edit")
    closed = []

    class FakeWS:
        async def close(self, code=1000):
            closed.append(code)
    api_users.register_open_ws(ed.id, FakeWS())
    _, adm = env.login("admin", PW)
    r = env.client(adm).patch(f"/api/users/{ed.id}", json={"role": "readonly"})
    assert r.status_code == 200
    assert closed == [4401]
    api_users._open_ws_by_user.pop(ed.id, None)
