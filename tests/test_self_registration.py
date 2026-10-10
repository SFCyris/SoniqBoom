# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Users → "Sign-up & passwords": admins switch the sign-in page's
"Create account" on and off, and whether new passwords must be strong; the
server enforces both.

In-process only: a throw-away UserStore and the test session's scratch
SoniqBoom.conf (conftest), no live server.
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom import config
from soniqboom.api import users as users_api
from soniqboom.core import users as users_mod

STRONG = "Str0ng!pass"     # 11 chars: upper, lower, digit, symbol
WEAK = "weakpass12"        # 10 chars, no upper / symbol


def _defaults():
    config.set_self_registration_enabled(True)
    config.set_strong_passwords_required(False)


@pytest.fixture()
def app_client(monkeypatch, tmp_path):
    store = users_mod.UserStore(tmp_path)
    store.create(username="amy", password="adminpass1", role="admin")
    store.create(username="rob", password="readpass12", role="readonly")
    monkeypatch.setattr(users_api, "get_user_store", lambda: store)
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    _defaults()
    yield app, store
    _defaults()


def _signed_in(app, username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _register(app, username, password="visitorpass1"):
    return TestClient(app).post(
        "/api/auth/register", json={"username": username, "password": password})


def _status(app):
    return TestClient(app).get("/api/auth/status").json()


# ── Self-registration ────────────────────────────────────────────────────────

def test_registration_is_on_by_default(app_client):
    app, store = app_client
    assert _status(app)["registration_open"] is True
    r = _register(app, "visitor1")
    assert r.status_code == 200, r.text
    assert store.get_by_username("visitor1").role == "readonly"


def test_admin_turns_registration_off_and_on(app_client):
    app, store = app_client
    admin = _signed_in(app, "amy", "adminpass1")

    r = admin.put("/api/users/settings", json={"self_registration": False})
    assert r.status_code == 200
    assert r.json() == {"self_registration": False, "strong_passwords": False}
    assert admin.get("/api/users/settings").json()["self_registration"] is False
    assert config.load_local_conf()["self_registration"] is False
    assert _status(app)["registration_open"] is False
    r = _register(app, "visitor2")
    assert r.status_code == 403
    assert "turned off" in r.json()["detail"]
    assert store.get_by_username("visitor2") is None

    assert admin.put("/api/users/settings", json={"self_registration": True}).json()["self_registration"] is True
    assert _status(app)["registration_open"] is True
    assert _register(app, "visitor3").status_code == 200


def test_only_admins_change_the_settings(app_client):
    app, _store = app_client
    anon = TestClient(app)
    assert anon.put("/api/users/settings", json={"self_registration": False}).status_code == 401
    assert anon.get("/api/users/settings").status_code == 401
    reader = _signed_in(app, "rob", "readpass12")
    assert reader.put("/api/users/settings", json={"strong_passwords": True}).status_code == 403
    assert reader.get("/api/users/settings").status_code == 403
    assert config.is_self_registration_enabled() is True
    assert config.is_strong_passwords_required() is False


def test_settings_endpoint_is_not_public():
    # The app-wide session gate (main.py) must not let these through unsigned.
    from soniqboom import main
    assert "/api/users/settings" not in main._PUBLIC_API_PATHS
    assert not any(p.startswith("/api/users") for p in main._PUBLIC_API_PATHS)


def test_no_admin_keeps_registration_closed(monkeypatch, tmp_path):
    store = users_mod.UserStore(tmp_path)
    monkeypatch.setattr(users_api, "get_user_store", lambda: store)
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    _defaults()
    assert _status(app)["registration_open"] is False
    assert _register(app, "visitor4").status_code == 403


def test_hand_edited_strings_are_read_strictly(app_client):
    def put(value):
        config.update_local_conf(lambda c: c.update(self_registration=value))
    try:
        for value, expected in (("false", False), ("off", False), (0, False),
                                ("true", True), ("on", True), (1, True),
                                ("garbage", True), (None, True)):
            put(value)
            assert config.is_self_registration_enabled() is expected, value
    finally:
        put(True)


# ── Strong passwords ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("pw, ok", [
    (STRONG, True),
    ("Ab1!Ab1!Ab", True),          # exactly 10
    ("Ab1!Ab1!A", False),          # 9
    ("str0ng!pass", False),        # no uppercase
    ("STR0NG!PASS", False),        # no lowercase
    ("Strong!pass", False),        # no digit
    ("Str0ngpass1", False),        # no symbol
    ("Str0ng pass1", False),       # a space is not a symbol
])
def test_strong_password_rule(pw, ok):
    assert users_mod.is_strong_password(pw) is ok


def test_off_any_non_empty_password_is_accepted(app_client):
    for pw in ("a", "1", "abc", WEAK):
        users_mod.validate_password(pw)
    with pytest.raises(ValueError, match="empty"):
        users_mod.validate_password("")
    with pytest.raises(ValueError, match="at most"):
        users_mod.validate_password("x" * 1025)
    app, store = app_client
    assert _register(app, "shorty", "ab").status_code == 200
    assert TestClient(app).post("/api/auth/login", json={"username": "shorty", "password": "ab"}).status_code == 200


def test_rule_applies_only_while_on(app_client):
    users_mod.validate_password(WEAK)                  # off: any length is fine
    config.set_strong_passwords_required(True)
    with pytest.raises(ValueError, match="uppercase letter"):
        users_mod.validate_password(WEAK)
    users_mod.validate_password(STRONG)


def test_every_way_of_setting_a_password_follows_the_rule(app_client):
    app, store = app_client
    admin = _signed_in(app, "amy", "adminpass1")
    assert admin.put("/api/users/settings", json={"strong_passwords": True}).json()["strong_passwords"] is True
    assert _status(app)["strong_passwords"] is True

    # Admin adds a user
    r = admin.post("/api/users", json={"username": "w1", "password": WEAK, "role": "edit"})
    assert r.status_code == 400 and "uppercase letter" in r.json()["detail"]
    assert admin.post("/api/users", json={"username": "s1", "password": STRONG, "role": "edit"}).status_code == 200
    # Sign-up
    assert _register(app, "w2", WEAK).status_code == 400
    assert _register(app, "s2", STRONG).status_code == 200
    # Admin resets someone's password
    rob = store.get_by_username("rob")
    assert admin.post(f"/api/users/{rob.id}/password", json={"new_password": WEAK}).status_code == 400
    assert admin.post(f"/api/users/{rob.id}/password", json={"new_password": STRONG}).status_code == 200
    # A user changes their own
    me = _signed_in(app, "amy", "adminpass1")      # existing weak password still signs in
    r = me.post("/api/auth/change-password", json={"current_password": "adminpass1", "new_password": WEAK})
    assert r.status_code == 400

    # Off again: 8+ characters is enough
    admin.put("/api/users/settings", json={"strong_passwords": False})
    assert _register(app, "w3", WEAK).status_code == 200


def test_update_local_conf_writes_under_its_own_lock(tmp_path):
    # flock is per open file: re-taking the lock on a new fd inside
    # update_local_conf used to wait forever.
    path = tmp_path / "SoniqBoom.conf"
    path.write_text('{"_comment": "kept", "port_hint": 1}')
    done = threading.Event()

    def run():
        config.update_local_conf(lambda c: c.update(self_registration=False), path=path)
        done.set()
    threading.Thread(target=run, daemon=True).start()
    assert done.wait(5), "update_local_conf deadlocked on its own lock"
    saved = json.loads(path.read_text())
    assert saved["self_registration"] is False and saved["_comment"] == "kept"
