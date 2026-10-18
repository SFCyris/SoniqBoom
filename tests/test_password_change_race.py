# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A change of one's own password applies only over the password it verified:
an administrator's reset landing between the check of the old password and
the write stands, and the change is refused (web: 409; Subsonic: error 40).
In-process (``TestClient``, no lifespan) — no server."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from soniqboom.core import users as users_mod
from soniqboom.core.users import PasswordChangedMeanwhile, UserStore

OLD = "Old-pass-123!x"
RESET = "Admin-reset-9!x"
MINE = "Users-own-7!xyz"
ADMIN_PW = "Admin-own-pass-5!"
MSG = "Your password was changed by an administrator"


@pytest.fixture
def env(tmp_data_dir, monkeypatch):
    from soniqboom.main import app
    st = UserStore(tmp_data_dir, persist_sessions=True)
    monkeypatch.setattr(users_mod, "_instance", st)
    admin = st.create("admin", ADMIN_PW, "admin")
    bob = st.create("bob", OLD, "readonly")

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
            assert r.status_code == 200, r.text
            return r.cookies.get("sb_session")

        @staticmethod
        def works(name, pw):
            return st.authenticate(name, pw) is not None

        @staticmethod
        def works_on_disk(name, pw):
            return UserStore(tmp_data_dir).authenticate(name, pw) is not None
    E.admin, E.bob = admin, bob
    return E


def _reset_after_verify(env, monkeypatch, user, verified_pw, new_pw):
    """Make an admin's reset of ``user`` land right after ``verified_pw`` was
    verified (off the main thread: inside the request), once."""
    real = users_mod.verify_password
    fired = []

    def verify(password, stored):
        ok = real(password, stored)
        if (ok and password == verified_pw and not fired
                and threading.current_thread() is not threading.main_thread()):
            fired.append(1)
            env.store.set_password(user.id, new_pw)
        return ok
    monkeypatch.setattr(users_mod, "verify_password", verify)
    return fired


def _reset_before_set(env, monkeypatch, user, new_pw):
    """The other window: the reset lands after the verification returned, just
    before the request's ``set_password`` runs."""
    real_set = env.store.set_password
    fired = []

    def set_pw(user_id, password, *a, **kw):
        if not fired and password != new_pw:
            fired.append(1)
            real_set(user.id, new_pw)
        return real_set(user_id, password, *a, **kw)
    monkeypatch.setattr(env.store, "set_password", set_pw)
    return fired


def _assert_admin_won(env, name):
    assert env.works(name, RESET)
    assert not env.works(name, MINE)
    assert env.works_on_disk(name, RESET)
    assert not env.works_on_disk(name, MINE)


# ── web ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("window", ["during-verify", "before-set"])
def test_web_change_password_never_undoes_an_admin_reset(env, monkeypatch, window):
    mine = env.login("bob", OLD)
    if window == "during-verify":
        fired = _reset_after_verify(env, monkeypatch, env.bob, OLD, RESET)
    else:
        fired = _reset_before_set(env, monkeypatch, env.bob, RESET)
    r = env.client(mine).post("/api/auth/change-password",
                              json={"current_password": OLD, "new_password": MINE})
    assert fired
    assert r.status_code == 409, r.text
    assert MSG in r.json()["detail"]
    _assert_admin_won(env, "bob")
    # The admin's reset ended the session the request came from.
    assert env.client(mine).get("/api/auth/me").status_code == 401


def test_web_change_password_without_a_race_keeps_this_session_only(env):
    mine = env.login("bob", OLD)
    other = env.login("bob", OLD)
    r = env.client(mine).post("/api/auth/change-password",
                              json={"current_password": OLD, "new_password": MINE})
    assert r.status_code == 200, r.text
    assert env.works("bob", MINE) and not env.works("bob", OLD)
    assert env.client(mine).get("/api/auth/me").status_code == 200
    assert env.client(other).get("/api/auth/me").status_code == 401


def test_web_change_password_wrong_current_password_is_still_401(env):
    mine = env.login("bob", OLD)
    r = env.client(mine).post("/api/auth/change-password",
                              json={"current_password": "Nope-nope-1!", "new_password": MINE})
    assert r.status_code == 401
    assert env.works("bob", OLD)


# ── Subsonic ────────────────────────────────────────────────────────────────

def _rest(env, endpoint, **params):
    r = env.client().get(f"/rest/{endpoint}",
                         params={"v": "1.16.1", "c": "test", "f": "json", **params})
    assert r.status_code == 200, r.text
    return r.json()["subsonic-response"]


@pytest.mark.parametrize("window", ["during-verify", "before-set"])
def test_subsonic_change_password_never_undoes_an_admin_reset(env, monkeypatch, window):
    if window == "during-verify":
        fired = _reset_after_verify(env, monkeypatch, env.bob, OLD, RESET)
    else:
        fired = _reset_before_set(env, monkeypatch, env.bob, RESET)
    res = _rest(env, "changePassword.view", u="bob", p=OLD, username="bob", password=MINE)
    assert fired
    assert res["status"] == "failed"
    assert res["error"]["code"] == 40 and MSG in res["error"]["message"]
    _assert_admin_won(env, "bob")


def test_subsonic_update_user_own_password_never_undoes_another_admins_reset(env, monkeypatch):
    fired = _reset_after_verify(env, monkeypatch, env.admin, ADMIN_PW, RESET)
    res = _rest(env, "updateUser.view", u="admin", p=ADMIN_PW, username="admin", password=MINE)
    assert fired
    assert res["status"] == "failed" and res["error"]["code"] == 40
    _assert_admin_won(env, "admin")


def test_subsonic_password_changes_without_a_race_still_apply(env):
    res = _rest(env, "changePassword.view", u="bob", p=OLD, username="bob", password=MINE)
    assert res["status"] == "ok", res
    assert env.works("bob", MINE)
    # An admin setting ANOTHER account's password is not tied to that
    # account's current password.
    res = _rest(env, "changePassword.view", u="admin", p=ADMIN_PW, username="bob",
                password=RESET)
    assert res["status"] == "ok", res
    assert env.works("bob", RESET)


# ── the store ───────────────────────────────────────────────────────────────

def test_set_password_expected_hash_refuses_a_stale_hash_and_changes_nothing(env):
    st = env.store
    tok, _ = st.issue_session(env.bob.id)
    stale = st.get(env.bob.id).password_hash
    st.set_password(env.bob.id, RESET)                  # the admin's reset
    after_reset = st.get(env.bob.id).password_hash
    tok2, _ = st.issue_session(env.bob.id)
    with pytest.raises(PasswordChangedMeanwhile):
        st.set_password(env.bob.id, MINE, tok2, expected_hash=stale)
    assert st.get(env.bob.id).password_hash == after_reset
    assert st.lookup_session(tok2) is not None          # nothing was revoked
    assert st.lookup_session(tok) is None               # (the reset's revocation)
    # The current hash is accepted.
    st.set_password(env.bob.id, MINE, tok2, expected_hash=after_reset)
    assert env.works("bob", MINE)
    assert st.lookup_session(tok2) is not None


def test_the_check_is_made_in_the_same_hold_as_the_write(env, monkeypatch):
    """A reset landing INSIDE ``set_password`` (after it was entered, before
    it takes the lock — here: while it hashes the new password) still wins."""
    mine = env.login("bob", OLD)
    real_hash = users_mod.hash_password
    fired = []

    def hash_then_reset(pw):
        h = real_hash(pw)
        if pw == MINE and not fired:
            fired.append(1)
            env.store.set_password(env.bob.id, RESET)
        return h
    monkeypatch.setattr(users_mod, "hash_password", hash_then_reset)
    r = env.client(mine).post("/api/auth/change-password",
                              json={"current_password": OLD, "new_password": MINE})
    assert fired and r.status_code == 409, r.text
    _assert_admin_won(env, "bob")


@pytest.mark.parametrize("when", ["before-the-change", "during-the-save"])
def test_a_reset_by_another_process_is_reported_and_stands(env, monkeypatch, when):
    """``soniqboom-setadm`` (another process: another store on the same data
    dir) resets the password while the user changes it."""
    mine = env.login("bob", OLD)
    other = UserStore(env.data_dir)
    if when == "before-the-change":
        fired = _reset_after_verify_with(monkeypatch, other, env.bob, OLD, RESET)
    else:
        real_save = env.store._save
        fired = []

        def save_after_reset():
            if not fired:
                fired.append(1)
                other.set_password(env.bob.id, RESET)
            return real_save()
        monkeypatch.setattr(env.store, "_save", save_after_reset)
    r = env.client(mine).post("/api/auth/change-password",
                              json={"current_password": OLD, "new_password": MINE})
    assert fired
    assert r.status_code == 409, r.text
    assert MSG in r.json()["detail"]
    _assert_admin_won(env, "bob")


def _reset_after_verify_with(monkeypatch, store, user, verified_pw, new_pw):
    real = users_mod.verify_password
    fired = []

    def verify(password, stored):
        ok = real(password, stored)
        if (ok and password == verified_pw and not fired
                and threading.current_thread() is not threading.main_thread()):
            fired.append(1)
            store.set_password(user.id, new_pw)
        return ok
    monkeypatch.setattr(users_mod, "verify_password", verify)
    return fired


@pytest.mark.parametrize("window", ["during-verify", "before-set"])
def test_subsonic_update_user_refusal_leaves_the_role_unchanged(env, monkeypatch, window):
    if window == "during-verify":
        fired = _reset_after_verify(env, monkeypatch, env.admin, ADMIN_PW, RESET)
    else:
        fired = _reset_before_set(env, monkeypatch, env.admin, RESET)
    # Another admin exists, so the demotion itself would be allowed.
    env.store.create("admin2", "Admin-two-pass-6!", "admin")
    res = _rest(env, "updateUser.view", u="admin", p=ADMIN_PW, username="admin",
                password=MINE, adminRole="false")
    assert fired
    assert res["status"] == "failed" and res["error"]["code"] == 40
    assert env.store.get(env.admin.id).role == "admin"
    _assert_admin_won(env, "admin")


def test_subsonic_update_user_drop_to_readonly_closes_the_sockets(env, monkeypatch):
    from soniqboom.api import subsonic
    closed = []

    async def rec(uid):
        closed.append(uid)
    monkeypatch.setattr(subsonic, "_close_user_sockets", rec)
    ed = env.store.create("eddie", "Eddie-pass-123!", "edit")
    off = {n: "false" for n in ("adminRole", "downloadRole", "uploadRole", "coverArtRole",
                                "commentRole", "podcastRole", "shareRole", "jukeboxRole",
                                "playlistRole")}
    res = _rest(env, "updateUser.view", u="admin", p=ADMIN_PW, username="eddie", **off)
    assert res["status"] == "ok", res
    assert env.store.get(ed.id).role == "readonly"
    assert closed == [ed.id]
