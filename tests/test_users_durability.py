# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""users.json is written crash-safely with a backup, an unreadable file falls
back to the backup, and an install that had accounts never comes back as an
open no-account server (recovery) — while a fresh install keeps its open
first-run setup."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from soniqboom.core import durable
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

PW = "Longenough-1"


def _store_with_admin(d):
    st = UserStore(d)
    st.create("admin1", PW, role="admin")
    st.create("reader", PW, role="readonly")
    return st


# ── the write path ───────────────────────────────────────────────────────────

def test_save_fsyncs_file_and_dir_and_writes_an_identical_0600_backup(tmp_path, monkeypatch):
    calls = []
    real = durable.full_fsync

    def spy(fd):
        calls.append(os.fstat(fd))
        return real(fd)
    monkeypatch.setattr(durable, "full_fsync", spy)
    st = UserStore(tmp_path)
    calls.clear()
    st.create("admin1", PW, role="admin")
    main, bak = tmp_path / "users.json", tmp_path / "users.json.bak"
    assert main.read_bytes() == bak.read_bytes()
    assert json.loads(main.read_text())["users"][0]["username"] == "admin1"
    for p in (main, bak):
        assert p.stat().st_mode & 0o777 == 0o600
    synced = [(c.st_dev, c.st_ino) for c in calls]
    ident = lambda p: (p.stat().st_dev, p.stat().st_ino)      # noqa: E731
    assert ident(main) in synced and ident(bak) in synced      # the renamed temp files
    assert synced.count(ident(tmp_path)) >= 2                  # the rename of each
    i_main, i_bak = synced.index(ident(main)), synced.index(ident(bak))
    assert i_main < synced.index(ident(tmp_path), i_main) < i_bak   # main durable before the backup
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_crash_between_write_and_rename_keeps_the_old_file(tmp_path, monkeypatch):
    st = _store_with_admin(tmp_path)
    before = (tmp_path / "users.json").read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash before rename")
    monkeypatch.setattr(durable.os, "replace", boom)
    with pytest.raises(OSError):
        st.create("newbie", PW, role="readonly")
    monkeypatch.undo()
    assert (tmp_path / "users.json").read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    # A real crash leaves the temp file behind (no cleanup ran): ignored.
    (tmp_path / "users.json.123.abcd.tmp").write_bytes(before[: len(before) // 2])
    st2 = UserStore(tmp_path)
    assert st2.get_by_username("admin1") and st2.get_by_username("reader")
    assert not st2.recovery_required


# ── macOS: F_FULLFSYNC (the drive's write cache too) ─────────────────────────

class _FakeFcntl:
    F_FULLFSYNC = 51

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def fcntl(self, fd, op):
        self.calls.append((os.fstat(fd), op))
        if self.fail:
            raise OSError(45, "Operation not supported")
        return 0


def _fsync_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(durable.os, "fsync", lambda fd: calls.append(os.fstat(fd)))
    return calls


def test_darwin_users_json_bak_and_sessions_use_full_fsync(tmp_path, monkeypatch):
    fake = _FakeFcntl()
    monkeypatch.setattr(durable.sys, "platform", "darwin")
    monkeypatch.setattr(durable, "fcntl", fake)
    plain = _fsync_spy(monkeypatch)
    st = UserStore(tmp_path, persist_sessions=True)
    fake.calls.clear()
    plain.clear()
    st.create("admin1", PW, role="admin")
    st.issue_session(st.get_by_username("admin1").id)
    assert st.flush_sessions()
    ident = lambda p: (p.stat().st_dev, p.stat().st_ino)      # noqa: E731
    synced = [(c.st_dev, c.st_ino) for c, op in fake.calls]
    assert {op for _c, op in fake.calls} == {_FakeFcntl.F_FULLFSYNC}
    for name in ("users.json", "users.json.bak", "sessions.json"):
        assert ident(tmp_path / name) in synced, name
    assert synced.count(ident(tmp_path)) >= 3                  # each rename too
    # (the data dir itself may also see a plain fsync: secret.key's own write)
    durable_files = {ident(tmp_path / n) for n in ("users.json", "users.json.bak",
                                                   "sessions.json")}
    assert not [c for c in plain if (c.st_dev, c.st_ino) in durable_files]


@pytest.mark.parametrize("fake", [_FakeFcntl(fail=True), object()],
                         ids=["F_FULLFSYNC fails", "no F_FULLFSYNC"])
def test_darwin_falls_back_to_fsync(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(durable.sys, "platform", "darwin")
    monkeypatch.setattr(durable, "fcntl", fake)
    plain = _fsync_spy(monkeypatch)
    durable.atomic_write_bytes(tmp_path / "f.json", b"{}")
    ident = lambda p: (p.stat().st_dev, p.stat().st_ino)      # noqa: E731
    synced = [(c.st_dev, c.st_ino) for c in plain]
    assert ident(tmp_path / "f.json") in synced and ident(tmp_path) in synced
    assert (tmp_path / "f.json").read_bytes() == b"{}"


def test_other_platforms_use_plain_fsync(tmp_path, monkeypatch):
    fake = _FakeFcntl()
    monkeypatch.setattr(durable.sys, "platform", "linux")
    monkeypatch.setattr(durable, "fcntl", fake)
    plain = _fsync_spy(monkeypatch)
    durable.atomic_write_bytes(tmp_path / "f.json", b"{}")
    assert fake.calls == [] and len(plain) == 2


def test_the_aof_stays_on_plain_fsync(tmp_path, monkeypatch):
    """The AOF never pays for F_FULLFSYNC (``durable.atomic_write_bytes`` —
    users.json / .bak / sessions.json and one-time small files — does)."""
    from soniqboom.core.aof import AOFWriter
    fake = _FakeFcntl()
    monkeypatch.setattr(durable.sys, "platform", "darwin")
    monkeypatch.setattr(durable, "fcntl", fake)
    import fcntl as real_fcntl
    seen = []
    real = real_fcntl.fcntl

    def spy(fd, op, *a):
        seen.append(op)
        return real(fd, op, *a)
    monkeypatch.setattr(real_fcntl, "fcntl", spy)
    plain = _fsync_spy(monkeypatch)
    w = AOFWriter(tmp_path / "library.aof")
    w.append("set", id="t1", title="x")
    w.stop()
    assert plain                                                # fsync'd…
    assert fake.calls == [] and getattr(real_fcntl, "F_FULLFSYNC", -1) not in seen


# ── fallback to the backup ───────────────────────────────────────────────────

@pytest.mark.parametrize("damage", [
    b"", b"{", b"\x00" * 64, b"[]", b"null", b"{}", b'{"users": "x"}',
    b'{"users": [1, 2]}', b'{"users": [{"bogus": 1}]}', "truncated",
])
def test_an_unreadable_main_file_loads_the_backup(tmp_path, damage):
    _store_with_admin(tmp_path)
    main = tmp_path / "users.json"
    if damage == "truncated":
        damage = main.read_bytes()[:-40]
    main.write_bytes(damage)
    st = UserStore(tmp_path)
    assert st.get_by_username("admin1") is not None
    assert st.count() == 2 and st.has_any_admin() and not st.recovery_required
    assert list(tmp_path.glob("users.json.corrupt-*"))     # kept for the admin
    # the next write restores the main file from memory
    st.update(st.get_by_username("reader").id, display_name="R")
    assert json.loads(main.read_text())["users"]


def test_a_missing_main_file_with_a_backup_loads_the_backup(tmp_path):
    _store_with_admin(tmp_path)
    (tmp_path / "users.json").unlink()
    st = UserStore(tmp_path)
    assert st.count() == 2 and not st.recovery_required


# ── recovery (fail closed) ───────────────────────────────────────────────────

@pytest.mark.parametrize("damage", [b"", b"{", b"garbage", b"{}", b'{"users": []}'])
def test_no_readable_accounts_on_a_set_up_install_is_recovery_not_open(tmp_path, damage):
    _store_with_admin(tmp_path)
    (tmp_path / "users.json").write_bytes(damage)
    (tmp_path / "users.json.bak").write_bytes(b"\x00garbage")
    st = UserStore(tmp_path)
    assert st.count() == 0
    assert st.recovery_required
    assert st.has_any()                  # every "no users → open" gate stays closed
    assert not st.has_any_admin()        # registration stays closed
    # still in recovery after a restart (the files were moved aside)
    st2 = UserStore(tmp_path)
    assert st2.recovery_required and st2.has_any()


def test_only_quarantined_or_temp_files_left_is_still_recovery(tmp_path):
    (tmp_path / "users.json.corrupt-1700000000").write_bytes(b"{")
    assert UserStore(tmp_path).recovery_required
    other = tmp_path / "other"
    other.mkdir()
    (other / "users.json.4242.beef.tmp").write_bytes(b'{"users": [')
    assert UserStore(other).recovery_required


def test_a_fresh_install_keeps_the_open_first_run_setup(tmp_path):
    st = UserStore(tmp_path)
    assert not st.recovery_required and not st.has_any() and st.count() == 0
    st.reload()                          # the overlay's "re-check" creates the lock file
    assert (tmp_path / "users.json.lock").exists()
    st2 = UserStore(tmp_path)
    assert not st2.recovery_required and not st2.has_any()


def test_recovery_ends_when_setadm_creates_an_admin_and_the_server_reloads(tmp_path):
    _store_with_admin(tmp_path)
    (tmp_path / "users.json").write_bytes(b"{")
    (tmp_path / "users.json.bak").unlink()
    server = UserStore(tmp_path, persist_sessions=True)
    assert server.recovery_required
    cli = UserStore(tmp_path)            # soniqboom-setadm's own store
    assert cli.recovery_required
    cli.create("rescue", PW, role="admin")
    assert server.reload() is True
    assert not server.recovery_required and server.has_any_admin()
    assert server.authenticate("rescue", PW) is not None


def test_reload_of_a_damaged_file_keeps_the_accounts_in_memory(tmp_path):
    st = _store_with_admin(tmp_path)
    (tmp_path / "users.json").write_bytes(b"{")
    (tmp_path / "users.json.bak").write_bytes(b"{")
    assert st.reload() is False
    assert st.count() == 2 and not st.recovery_required


# ── over HTTP ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from soniqboom.main import app
    return TestClient(app)


def test_http_recovery_refuses_anonymous_api_and_reports_it(tmp_path, monkeypatch, client):
    _store_with_admin(tmp_path)
    (tmp_path / "users.json").write_bytes(b"\x00")
    (tmp_path / "users.json.bak").write_bytes(b"\x00")
    monkeypatch.setattr(users_mod, "_instance", UserStore(tmp_path))
    for path in ("/api/library/dirs", "/api/tracks?limit=1", "/api/playlists"):
        assert client.get(path).status_code == 401, path
    st = client.get("/api/auth/status").json()
    assert st["recovery_required"] is True and st["has_any_user"] is False
    assert "soniqboom-setadm" in st["recovery_message"]
    assert client.post("/api/auth/register",
                       json={"username": "x1", "password": PW}).status_code == 403


def test_http_fresh_install_is_unchanged(tmp_path, monkeypatch, client):
    monkeypatch.setattr(users_mod, "_instance", UserStore(tmp_path))
    assert client.get("/api/library/dirs").status_code == 200
    st = client.get("/api/auth/status").json()
    assert st["recovery_required"] is False and st["has_any_user"] is False
    assert st["recovery_message"] is None


def test_a_read_error_is_not_corruption(tmp_path, monkeypatch):
    """EMFILE / EACCES / EIO on users.json: nothing is moved aside, the backup
    is not used, no account → recovery; the next start reads the files again."""
    _store_with_admin(tmp_path)
    real = users_mod.Path.read_bytes

    def failing(self):
        if self.name.startswith("users.json"):
            raise OSError(24, "Too many open files")
        return real(self)
    monkeypatch.setattr(users_mod.Path, "read_bytes", failing)
    st = UserStore(tmp_path)
    assert st.recovery_required and st.has_any() and st.count() == 0
    assert not list(tmp_path.glob("*.corrupt-*"))
    monkeypatch.undo()
    st2 = UserStore(tmp_path)
    assert st2.count() == 2 and not st2.recovery_required


def test_an_unreadable_main_file_on_reload_keeps_memory_and_reports_it(tmp_path):
    """``sudo soniqboom-setadm`` leaving a root-owned users.json: the reload
    fails (500 at the endpoint), the backup's newer account is not loaded."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0000 file anyway")
    st = _store_with_admin(tmp_path)
    UserStore(tmp_path).create("alice", PW, role="readonly")
    (tmp_path / "users.json").chmod(0)
    try:
        assert st.reload() is False
        assert st.get_by_username("admin1") and not st.get_by_username("alice")
        assert (tmp_path / "users.json").exists()          # not moved aside
    finally:
        (tmp_path / "users.json").chmod(0o600)


def test_a_sessions_file_alone_is_evidence_of_a_set_up_install(tmp_path):
    (tmp_path / "sessions.json").write_text('{"version": 1, "sessions": []}')
    st = UserStore(tmp_path)
    assert st.recovery_required and st.has_any()
