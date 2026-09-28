# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Subsonic token secret is never written to users.json in plaintext."""
import json

from soniqboom.core.users import UserStore


def test_subsonic_secret_is_encrypted_on_disk_and_survives_a_reload(tmp_path):
    st = UserStore(tmp_path)
    u = st.create("bob", "hunter2hunter2", role="admin")
    st.update(u.id, subsonic_password="hunter2hunter2")
    text = (tmp_path / "users.json").read_text()
    assert "hunter2hunter2" not in text
    assert json.loads(text)["users"][0]["subsonic_password"].startswith("enc:v2:")
    assert (tmp_path / "secret.key").stat().st_mode & 0o077 == 0
    assert UserStore(tmp_path).get_by_username("bob").subsonic_password == "hunter2hunter2"


def test_legacy_plaintext_loads_and_a_lost_key_degrades_to_none(tmp_path):
    st = UserStore(tmp_path)
    u = st.create("amy", "correcthorse1", role="admin")
    data = json.loads((tmp_path / "users.json").read_text())
    data["users"][0]["subsonic_password"] = "legacy-plain"
    (tmp_path / "users.json").write_text(json.dumps(data))
    assert UserStore(tmp_path).get_by_username("amy").subsonic_password == "legacy-plain"
    st2 = UserStore(tmp_path)
    st2.update(u.id, display_name="Amy")              # any save migrates it
    assert "legacy-plain" not in (tmp_path / "users.json").read_text()
    (tmp_path / "secret.key").unlink()
    assert UserStore(tmp_path).get_by_username("amy").subsonic_password is None


def test_an_empty_key_file_is_replaced_and_never_leads_to_plaintext(tmp_path):
    (tmp_path / "secret.key").write_bytes(b"")          # e.g. a power loss mid-write
    st = UserStore(tmp_path)
    u = st.create("cat", "longenough1", role="admin")
    st.update(u.id, subsonic_password="app-pass-123")
    assert "app-pass-123" not in (tmp_path / "users.json").read_text()
    assert list(tmp_path.glob("secret.key.corrupt-*"))
    assert (tmp_path / "secret.key").read_bytes().strip()
    assert UserStore(tmp_path).get_by_username("cat").subsonic_password == "app-pass-123"


def test_an_unreadable_key_drops_the_secret_instead_of_writing_it_plain(tmp_path, monkeypatch):
    from soniqboom.core import users as users_mod
    monkeypatch.setattr(users_mod, "_load_or_create_data_key", lambda p: None)
    st = UserStore(tmp_path)
    u = st.create("dan", "longenough1", role="admin")
    st.update(u.id, subsonic_password="app-pass-456")
    assert "app-pass-456" not in (tmp_path / "users.json").read_text()
    assert st.get_by_username("dan").subsonic_password == "app-pass-456"   # still usable until restart


def test_a_transient_key_read_error_never_destroys_a_stored_secret(tmp_path, monkeypatch):
    from soniqboom.core import users as users_mod
    st = UserStore(tmp_path)
    u = st.create("eve", "longenough1", role="admin")
    st.update(u.id, subsonic_password="app-pass-789")
    real = users_mod._load_or_create_data_key
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(path)       # first read fails (EMFILE)
    monkeypatch.setattr(users_mod, "_load_or_create_data_key", flaky)
    st2 = UserStore(tmp_path)
    assert st2.get_by_username("eve").subsonic_password is None
    before = (tmp_path / "users.json").read_text()
    enc = json.loads(before)["users"][0]["subsonic_password"]
    st2.update(u.id, display_name="Eve")                   # within the 30 s retry window
    assert json.loads((tmp_path / "users.json").read_text())["users"][0]["subsonic_password"] == enc
    st2._ss_key_retry_at = 0.0                             # …30 s later the key is re-tried
    st2.update(u.id, display_name="Eve")                   # a save: key readable again now
    assert "app-pass-789" not in (tmp_path / "users.json").read_text()
    assert st2.get_by_username("eve").subsonic_password == "app-pass-789"
    assert UserStore(tmp_path).get_by_username("eve").subsonic_password == "app-pass-789"
    assert enc.startswith("enc:v2:")


def test_sign_in_during_a_key_outage_never_overwrites_a_held_back_app_password(tmp_path, monkeypatch):
    from soniqboom.core import users as users_mod
    st = UserStore(tmp_path)
    u = st.create("fay", "longenough1", role="admin")
    st.update(u.id, subsonic_password="gen-app-pass", subsonic_password_custom=True)
    real = users_mod._load_or_create_data_key
    outage = {"on": True}
    monkeypatch.setattr(users_mod, "_load_or_create_data_key",
                        lambda p: None if outage["on"] else real(p))
    st2 = UserStore(tmp_path)
    assert st2.authenticate("fay", "longenough1") is not None      # sign-in during the outage
    st2.change_password(u.id, "longenough2") if hasattr(st2, "change_password") else None
    outage["on"] = False
    st2._ss_key_retry_at = 0.0
    st2.update(u.id, display_name="Fay")
    assert st2.get_by_username("fay").subsonic_password == "gen-app-pass"
    assert UserStore(tmp_path).get_by_username("fay").subsonic_password == "gen-app-pass"


def test_token_sign_in_recovers_a_held_back_secret_without_a_save(tmp_path, monkeypatch):
    from soniqboom.core import users as users_mod
    st = UserStore(tmp_path)
    u = st.create("gus", "longenough1", role="admin")
    st.update(u.id, subsonic_password="gen-pass-1", subsonic_password_custom=True)
    real = users_mod._load_or_create_data_key
    outage = {"on": True}
    monkeypatch.setattr(users_mod, "_load_or_create_data_key",
                        lambda p: None if outage["on"] else real(p))
    st2 = UserStore(tmp_path)
    assert st2.get_by_username("gus").subsonic_password is None     # held back
    outage["on"] = False
    st2._ss_key_retry_at = 0.0                                      # 30 s later
    assert st2.get_by_username("gus").subsonic_password == "gen-pass-1"   # the auth lookup recovers it
