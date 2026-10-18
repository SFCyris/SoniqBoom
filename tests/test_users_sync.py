# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""users.json written by another process (``soniqboom-setadm``) is never
undone by the running server's next save; a users.json that can't be read is
never overwritten; a read error at load is retried; leftover temp copies of
users.json are removed."""
import errno
import json
import os
from pathlib import Path

import pytest

from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore, verify_password

# (looked up, not imported: the module also runs against older code, where
# these tests fail rather than not collect)
UsersFileUnreadable = getattr(users_mod, "UsersFileUnreadable", OSError)
DISPLAY_NAME_MAX_LEN = getattr(users_mod, "DISPLAY_NAME_MAX_LEN", 100)

PW = "Old-pass-123!"
NEW = "New-pass-456!"


def _disk(d: Path, name: str = "users.json") -> dict:
    return {u["username"]: u for u in json.loads((d / name).read_text())["users"]}


@pytest.fixture
def d(tmp_data_dir):
    return tmp_data_dir


# ── Finding 2: a CLI change is not undone by the server's next save ────────

def test_out_of_band_password_reset_survives_a_server_save(d):
    server = UserStore(d, persist_sessions=True)
    admin = server.create("admin", PW, "admin")
    server.create("bob", PW, "readonly")
    # soniqboom-setadm (another process): reset admin's password, no reload
    cli = UserStore(d)
    cli.set_password(cli.get_by_username("admin").id, NEW)
    cli_hash = _disk(d)["admin"]["password_hash"]
    # an ordinary server write meanwhile (bob signs in: last-login stamp)
    assert server.authenticate("bob", PW) is not None
    server.update(server.get_by_username("bob").id, display_name="Bob")   # a sure save
    on_disk = _disk(d)
    assert on_disk["admin"]["password_hash"] == cli_hash        # the reset stays
    assert on_disk["bob"]["display_name"] == "Bob"              # the server's change too
    # the running server took the reset in: the old password is refused now
    assert server.get(admin.id).password_hash == cli_hash
    assert server.authenticate("admin", PW) is None
    assert server.authenticate("admin", NEW) is not None
    # and after a restart
    again = UserStore(d)
    assert again.authenticate("admin", NEW) is not None
    assert again.authenticate("admin", PW) is None


def test_out_of_band_disable_survives_a_server_token_edit(d):
    server = UserStore(d, persist_sessions=True)
    server.create("admin", PW, "admin")
    eve = server.create("eve", PW, "edit")
    tok, _ = server.issue_session(eve.id)
    assert server.lookup_session(tok) is not None
    cli = UserStore(d)
    cli.update(cli.get_by_username("eve").id, enabled=False)
    # a server write of the same account in between: eve edits her scrobble token
    server.update(eve.id, listenbrainz_token="lb-token")
    assert _disk(d)["eve"]["enabled"] is False
    assert server.get(eve.id).enabled is False
    assert server.get(eve.id).listenbrainz_token == "lb-token"
    assert server.lookup_session(tok) is None                   # her session is over
    assert UserStore(d).get_by_username("eve").enabled is False


def test_merge_keeps_creations_and_deletions_of_both_sides(d):
    server = UserStore(d)
    server.create("admin", PW, "admin")
    gone = server.create("gone", PW, "readonly")
    cli = UserStore(d)
    cli.create("fromcli", PW, "edit")                  # created by the other process
    cli.delete(cli.get_by_username("gone").id)         # deleted by the other process
    server.create("fromserver", PW, "readonly")        # created here meanwhile
    names = set(_disk(d))
    assert names == {"admin", "fromcli", "fromserver"}
    assert {u.username for u in server.list_users()} == names
    assert server.get(gone.id) is None
    # a delete here vs an edit there: the account stays deleted
    cli2 = UserStore(d)
    cli2.update(cli2.get_by_username("fromcli").id, display_name="edited there")
    server.delete(server.get_by_username("fromcli").id)
    assert "fromcli" not in _disk(d)


def test_merge_takes_api_keys_of_both_sides(d):
    server = UserStore(d)
    u = server.create("admin", PW, "admin")
    k_srv0, e0 = server.create_api_key(u.id, "zero")
    cli = UserStore(d)
    k_cli, e_cli = cli.create_api_key(u.id, "cli")
    cli.revoke_api_key(u.id, e0["id"])
    k_srv, e_srv = server.create_api_key(u.id, "srv")
    names = {k["name"] for k in server.list_api_keys(u.id)}
    assert names == {"cli", "srv"}
    assert server.lookup_api_key(k_cli) is not None
    assert server.lookup_api_key(k_srv0) is None
    assert {k["name"] for k in UserStore(d).list_api_keys(u.id)} == {"cli", "srv"}


def test_unchanged_file_is_not_reread_on_save(d, monkeypatch):
    """The fast path: a file only this store wrote is not read before a save."""
    st = UserStore(d)
    st.create("admin", PW, "admin")
    reads = []
    real = UserStore._read_users_file
    monkeypatch.setattr(UserStore, "_read_users_file",
                        staticmethod(lambda p: reads.append(p.name) or real(p)))
    st.update(st.get_by_username("admin").id, display_name="x")
    assert "users.json" not in reads


# ── Finding 4: a create on an unreadable users.json keeps the old accounts ─

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file")
def test_create_on_an_unreadable_users_json_keeps_every_account(d):
    st = UserStore(d)
    st.create("admin", PW, "admin")
    st.create("bob", PW, "readonly")
    st.create("carol", PW, "edit")
    for name in ("users.json", "users.json.bak"):
        os.chmod(d / name, 0)
    try:
        cli = UserStore(d)                       # setadm's store: recovery
        assert cli.recovery_required
        cli.create("rescue", PW, "admin")
    finally:
        for q in d.iterdir():
            try:
                os.chmod(q, 0o600)
            except OSError:
                pass
    holders = [q.name for q in d.iterdir() if q.is_file()
               and b'"carol"' in q.read_bytes() and b'"bob"' in q.read_bytes()]
    assert any(n.startswith("users.json.corrupt-") for n in holders), sorted(
        q.name for q in d.iterdir())
    assert any(n.startswith("users.json.bak.corrupt-") for n in holders)
    assert set(_disk(d)) == {"rescue"}


def test_a_changed_unreadable_users_json_is_never_overwritten(d, monkeypatch):
    st = UserStore(d)
    st.create("admin", PW, "admin")
    (d / "users.json").write_bytes(b'{"users": [ half-copied')   # changed + not a users doc
    before = (d / "users.json").read_bytes()
    with pytest.raises(UsersFileUnreadable):
        st.create("bob", PW, "readonly")
    assert (d / "users.json").read_bytes() == before
    # a sign-in still works while the file can't be written (its stamp waits)
    assert st.authenticate("admin", PW) is not None


# ── Finding 7: a transient read error at load is retried ─────────────────

def test_one_eio_at_load_does_not_lock_sign_in_until_a_restart(d, monkeypatch):
    UserStore(d).create("admin", PW, "admin")
    calls = {"n": 0}
    real = Path.read_bytes

    def flaky(self):
        if self.name == "users.json" and calls["n"] == 0:
            calls["n"] += 1
            raise OSError(errno.EIO, "Input/output error (simulated, once)")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", flaky)
    st = UserStore(d, persist_sessions=True)
    assert st.recovery_required and st.count() == 0
    assert st.check_restored() is True                  # the file didn't change
    assert st.count() == 1 and not st.recovery_required
    assert st.authenticate("admin", PW) is not None


def test_a_lasting_read_error_is_retried_but_logged_once(d, monkeypatch, caplog):
    UserStore(d).create("admin", PW, "admin")
    real = Path.read_bytes

    def broken(self):
        if self.name == "users.json":
            raise OSError(errno.EIO, "Input/output error (simulated)")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", broken)
    st = UserStore(d, persist_sessions=True)
    caplog.clear()
    for _ in range(5):
        assert st.check_restored() is False
    assert sum("could not be loaded" in r.getMessage() for r in caplog.records) == 1
    monkeypatch.setattr(Path, "read_bytes", real)
    assert st.check_restored() is True


# ── Finding 15: the re-check button's reload never moves a file aside ────

def test_recheck_reload_leaves_a_half_copied_users_json_alone(d):
    st = UserStore(d)
    st.create("ed", PW, "edit")                         # accounts, no admin
    half = b'{"version": 1, "users": [{"id": "x", "user'
    (d / "users.json").write_bytes(half)
    assert st.adopt_from_disk(False) is False
    assert (d / "users.json").read_bytes() == half
    assert not any(q.name.startswith("users.json.corrupt") for q in d.iterdir())
    assert st.get_by_username("ed") is not None


# ── Finding 18: leftover temp copies are removed at the start ────────────

def test_leftover_users_and_sessions_temp_files_are_removed(d):
    UserStore(d).create("admin", PW, "admin")
    left = ["users.json.4242.deadbeef.tmp", "users.json.bak.4242.0badc0de.tmp",
            "sessions.json.4242.abcdef01.tmp"]
    for n in left:
        (d / n).write_bytes(b'{"secret": "hashes"}')
    other = d / "users.json.notatemp"
    other.write_bytes(b"x")
    UserStore(d, persist_sessions=True)
    assert not any((d / n).exists() for n in left)
    assert other.exists()


def test_temp_files_stay_while_no_account_can_be_loaded(d):
    (d / "users.json.4242.deadbeef.tmp").write_bytes(b'{"users": []}')
    st = UserStore(d, persist_sessions=True)
    assert st.recovery_required
    assert (d / "users.json.4242.deadbeef.tmp").exists()


def test_display_name_is_length_limited(d):
    st = UserStore(d)
    with pytest.raises(ValueError):
        st.create("admin", PW, "admin", display_name="x" * (DISPLAY_NAME_MAX_LEN + 1))
    u = st.create("admin", PW, "admin", display_name="x" * DISPLAY_NAME_MAX_LEN)
    with pytest.raises(ValueError):
        st.update(u.id, display_name="y" * 20_000_000)
    assert st.get(u.id).display_name == "x" * DISPLAY_NAME_MAX_LEN
    assert verify_password(PW, st.get(u.id).password_hash)
