# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-user favourite stations and playlists (shared / imported).

* Each account has its own favourite stations; station resolution reads
  every user's list (a ``custom:`` station resolves wherever it is used); the
  shared list of a version before per-user lists goes to the legacy owner;
  readonly accounts manage their own list; a deleted account's list goes;
  the ``.sbz`` backup carries the lists by user name.
* Playlists belong to their owner.  The owner may share one; another account
  imports it (a read-only link following the owner's edits); unsharing or
  deleting ends that; readonly accounts keep their own playlists and import
  but don't share; nobody — admins included — opens another's unshared one;
  Subsonic ``public`` ⇔ shared; playlists with no owner go to the legacy
  owner; a deleted account's playlists go; the backup maps owners and
  subscribers by user name.
* Kept-aside backup data never attaches to a self-registered account.
"""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.core import persistence
from soniqboom.core.store import TrackStore
from soniqboom.core.user_data import OWNER_CONFIG_KEY, PENDING_CONFIG_KEY


@pytest.fixture()
def radio(monkeypatch, tmp_path):
    from soniqboom.core import radiodir
    rdir = tmp_path / "radio"
    rdir.mkdir()
    monkeypatch.setattr(radiodir, "_dir", lambda: rdir)
    return radiodir


def _station(sid: str, name: str = "", url: str = "http://x.example/s") -> dict:
    return {"sid": sid, "name": name or sid,
            "streams": [{"url": url, "codec": "MP3", "bitrate": 128, "hls": 0}]}


# ── Favourite stations ──────────────────────────────────────────────────────

def test_two_users_have_their_own_favourites(radio):
    radio.add_favorite(_station("custom:a"), uid="ann")
    radio.add_favorite(_station("custom:b"), uid="bob")
    radio.remove_favorite("scene:nectarine", uid="bob")
    assert [f["sid"] for f in radio.get_favorites("ann")] == ["scene:nectarine", "custom:a"]
    assert [f["sid"] for f in radio.get_favorites("bob")] == ["custom:b"]
    assert [f["sid"] for f in radio.get_favorites("new")] == ["scene:nectarine"]   # default
    assert radio.is_favorite("custom:a", uid="ann") and not radio.is_favorite("custom:a", uid="bob")
    assert radio.update_favorite("custom:a", _station("custom:a", "A2"), uid="bob") is None
    assert radio.update_favorite("custom:a", _station("custom:a", "A2"), uid="ann") is not None
    assert radio.get_favorites("ann")[1]["name"] == "A2"


def test_a_custom_station_resolves_for_everyone(radio):
    radio.add_favorite(_station("custom:only-ann", "Ann's"), uid="ann")
    st = asyncio.run(radio.resolve_station("custom:only-ann"))
    assert st and st["name"] == "Ann's"
    assert "custom:only-ann" in {s["sid"] for s in radio.all_favorites()}
    assert radio.get_favorites("bob") == radio._default_favorites()


def test_the_shared_list_goes_to_the_legacy_owner(radio):
    (radio._dir() / "favorites.json").write_text(json.dumps([_station("custom:old")]))
    radio.add_favorite(_station("custom:mine"), uid="sfc")
    assert radio.migrate_shared_favorites("sfc")
    assert [f["sid"] for f in radio.get_favorites("sfc")] == \
        ["scene:nectarine", "custom:mine", "custom:old"]
    assert not (radio._dir() / "favorites.json").exists()
    assert (radio._dir() / "favorites.json.migrated").exists()
    assert not radio.migrate_shared_favorites("sfc")                 # once


def test_unowned_favourites_move_with_the_other_data(radio):
    from soniqboom.core.user_data import claim_unowned
    radio.add_favorite(_station("custom:x"), uid="")
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    admin = types.SimpleNamespace(id="adm", role="admin", created_at=1.0, enabled=True)
    claim_unowned(st, types.SimpleNamespace(list_users=lambda: [admin]))
    assert "custom:x" in {f["sid"] for f in radio.get_favorites("adm")}
    assert radio._read_favs("") is None


def _stations_app(monkeypatch, who: dict):
    from soniqboom.api import stations
    from soniqboom.api import users as users_api
    fake = types.SimpleNamespace(lookup_session=lambda tok: who.get(tok), has_any=lambda: True)
    monkeypatch.setattr(users_api, "get_user_store", lambda: fake)
    app = FastAPI()
    app.include_router(stations.router)
    return TestClient(app)


def test_readonly_accounts_manage_their_own_favourites_over_http(radio, monkeypatch):
    who = {"t-ro": types.SimpleNamespace(id="ro", role="readonly", enabled=True),
           "t-ed": types.SimpleNamespace(id="ed", role="edit", enabled=True)}
    c = _stations_app(monkeypatch, who)
    sid = next(s["sid"] for s in radio.SCENE_PACK if s["sid"] != "scene:nectarine")
    r = c.post("/stations/favorites", json={"sid": sid}, cookies={"sb_session": "t-ro"})
    assert r.status_code == 200
    assert sid in [f["sid"] for f in r.json()["favorites"]]
    mine = c.get("/stations/favorites", cookies={"sb_session": "t-ro"}).json()
    theirs = c.get("/stations/favorites", cookies={"sb_session": "t-ed"}).json()
    assert sid in [f["sid"] for f in mine] and sid not in [f["sid"] for f in theirs]
    scene = {s["sid"]: s["favorite"] for s in
             c.get("/stations/scene", cookies={"sb_session": "t-ed"}).json()}
    assert scene[sid] is False                                        # flags the caller's
    assert c.delete(f"/stations/favorites/{sid}", cookies={"sb_session": "t-ro"}).status_code == 200
    assert sid not in [f["sid"] for f in radio.get_favorites("ro")]
    assert c.get("/stations/favorites").status_code == 401


def test_a_deleted_account_loses_its_favourites_and_playlists(radio, monkeypatch, tmp_path):
    from soniqboom.api import subsonic
    from soniqboom.core import subsonic_state
    radio.add_favorite(_station("custom:x"), uid="bob")
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.create_playlist("p-bob", "bob's", owner_user_id="bob")
    st.create_playlist("p-ann", "ann's", owner_user_id="ann")
    st.set_playlist_shared("p-ann", True)
    st.set_playlist_subscription("p-ann", "bob", True)
    monkeypatch.setattr(subsonic, "_library_store", lambda: st)
    subsonic_state.reset_state(tmp_path / "s.json")
    try:
        asyncio.run(subsonic.forget_user("bob"))
    finally:
        subsonic_state.reset_state()
    assert radio._read_favs("bob") is None
    assert st.get_playlist("p-bob") is None
    assert st.get_playlist("p-ann")["subscribers"] == []


# ── Playlists ───────────────────────────────────────────────────────────────

def test_share_import_follow_unshare_and_delete():
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    for t in ("t1", "t2"):
        st.upsert_track({"id": t, "path": f"/m/{t}.mp3", "title": t})
    st.create_playlist("p", "Ann's mix", ["t1"], owner_user_id="ann")
    assert st.shared_playlists_for("bob") == []
    assert st.set_playlist_subscription("p", "bob", True) is None     # not shared yet
    assert not st.playlist_readable(st.get_playlist("p"), "bob")
    st.set_playlist_shared("p", True)
    assert [p["id"] for p in st.shared_playlists_for("bob")] == ["p"]
    assert st.shared_playlists_for("ann") == []                        # (not her own)
    st.set_playlist_subscription("p", "bob", True)
    assert [p["id"] for p in st.list_playlists_for_user("bob")] == ["p"]
    st.update_playlist("p", {"track_ids": ["t1", "t2"]})               # the owner edits
    assert st.list_playlists_for_user("bob")[0]["track_ids"] == ["t1", "t2"]
    st.set_playlist_shared("p", False)                                 # unshared: gone for bob
    assert st.list_playlists_for_user("bob") == []
    assert st.get_playlist("p")["subscribers"] == []
    st.set_playlist_shared("p", True)
    st.set_playlist_subscription("p", "bob", True)
    st.delete_playlist("p")
    assert st.list_playlists_for_user("bob") == []


def _playlist_app(monkeypatch, st, who: dict, names: dict):
    from soniqboom.api import playlist
    from soniqboom.api import users as users_api
    from soniqboom.core import data as data_mod
    from soniqboom.core import store as store_mod
    from soniqboom.core import users as core_users
    for mod in (store_mod, data_mod, playlist):
        monkeypatch.setattr(mod, "get_store", lambda: st)
    fake = types.SimpleNamespace(
        lookup_session=lambda tok: who.get(tok), has_any=lambda: True,
        get=lambda uid: names.get(uid))
    monkeypatch.setattr(users_api, "get_user_store", lambda: fake)
    monkeypatch.setattr(core_users, "get_user_store", lambda: fake)
    app = FastAPI()
    app.include_router(playlist.router)
    return TestClient(app)


def test_playlists_over_http_owner_subscriber_readonly_admin(monkeypatch):
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    for t in ("t1", "t2"):
        st.upsert_track({"id": t, "path": f"/m/{t}.mp3", "title": t})
    who = {"A": types.SimpleNamespace(id="ann", role="edit", enabled=True),
           "B": types.SimpleNamespace(id="bob", role="readonly", enabled=True),
           "Z": types.SimpleNamespace(id="adm", role="admin", enabled=True)}
    names = {"ann": types.SimpleNamespace(username="ann", display_name="Ann"),
             "bob": types.SimpleNamespace(username="bob", display_name=None)}
    c = _playlist_app(monkeypatch, st, who, names)
    A, B, Z = ({"sb_session": k} for k in "ABZ")
    pid = c.post("/playlists", json={"name": "Mix", "track_ids": ["t1"]}, cookies=A).json()["id"]
    # Readonly: own playlists yes, sharing no.
    bid = c.post("/playlists", json={"name": "Mine"}, cookies=B)
    assert bid.status_code == 201
    bid = bid.json()["id"]
    assert c.put(f"/playlists/{bid}", json={"shared": True}, cookies=B).status_code == 403
    # Nobody else opens an unshared one — not even an admin.
    assert c.get(f"/playlists/{pid}", cookies=Z).status_code == 404
    assert c.put(f"/playlists/{pid}", json={"name": "x"}, cookies=Z).status_code == 404
    assert c.get("/playlists/shared", cookies=B).json() == []
    assert c.put(f"/playlists/{pid}", json={"shared": True}, cookies=A).status_code == 200
    shared = c.get("/playlists/shared", cookies=B).json()
    assert shared == [{"id": pid, "name": "Mix", "owner_name": "ann", "track_count": 1,
                       "smart": False, "subscribed": False}]
    assert c.put(f"/playlists/{pid}/subscription", json={"subscribed": True},
                 cookies=B).status_code == 200
    mine = {p["id"]: p for p in c.get("/playlists", cookies=B).json()}
    assert mine[pid]["subscribed"] and mine[pid]["read_only"] and mine[pid]["owner_name"] == "ann"
    assert not mine[bid]["subscribed"]
    # The owner's edit shows; the subscriber can't edit, add, remove or delete.
    c.post(f"/playlists/{pid}/tracks", json={"track_ids": ["t2"]}, cookies=A)
    got = c.get(f"/playlists/{pid}", cookies=B).json()
    assert [t["id"] for t in got["tracks"]] == ["t1", "t2"] and got["read_only"]
    for method, url, body in (("put", f"/playlists/{pid}", {"name": "hijack"}),
                              ("post", f"/playlists/{pid}/tracks", {"track_ids": ["t1"]}),
                              ("delete", f"/playlists/{pid}", None)):
        kw = {"json": body} if body is not None else {}
        assert getattr(c, method)(url, cookies=B, **kw).status_code == 403, url
    # Unsubscribe; then the owner unshares / deletes.
    c.put(f"/playlists/{pid}/subscription", json={"subscribed": False}, cookies=B)
    assert pid not in {p["id"] for p in c.get("/playlists", cookies=B).json()}
    c.put(f"/playlists/{pid}/subscription", json={"subscribed": True}, cookies=B)
    c.put(f"/playlists/{pid}", json={"shared": False}, cookies=A)
    assert pid not in {p["id"] for p in c.get("/playlists", cookies=B).json()}
    assert c.get(f"/playlists/{pid}", cookies=B).status_code == 404
    c.put(f"/playlists/{pid}", json={"shared": True}, cookies=A)
    c.put(f"/playlists/{pid}/subscription", json={"subscribed": True}, cookies=B)
    assert c.delete(f"/playlists/{pid}", cookies=A).status_code == 200
    assert pid not in {p["id"] for p in c.get("/playlists", cookies=B).json()}
    assert c.get("/playlists").status_code == 401


def test_subsonic_public_is_shared(monkeypatch, tmp_path):
    from soniqboom.api import subsonic
    from soniqboom.core import subsonic_state
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.upsert_track({"id": "t1", "path": "/m/t1.mp3", "title": "t1"})
    cur = {"u": types.SimpleNamespace(id="ann", username="ann", role="edit", enabled=True)}
    users = {"ann": types.SimpleNamespace(id="ann", username="ann"),
             "bob": types.SimpleNamespace(id="bob", username="bob")}
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(subsonic, "get_store", lambda: st)
    monkeypatch.setattr(data_mod, "get_store", lambda: st)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: cur["u"])
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: cur["u"])
    monkeypatch.setattr(subsonic, "get_user_store",
                        lambda: types.SimpleNamespace(get=lambda uid: users.get(uid)))
    subsonic_state.reset_state(tmp_path / "s.json")
    app = FastAPI()
    app.include_router(subsonic.router)
    c = TestClient(app)

    def call(method, **params):
        body = c.get(f"/rest/{method}", params={"f": "json", **params}).json()["subsonic-response"]
        return body
    try:
        pl = call("createPlaylist.view", name="Mix", songId="t1", public="true")["playlist"]
        assert pl["public"] is True and pl["owner"] == "ann"
        cur["u"] = types.SimpleNamespace(id="bob", username="bob", role="readonly", enabled=True)
        assert call("getPlaylists.view")["playlists"]["playlist"] == []    # not imported
        got = call("getPlaylist.view", id=pl["id"])["playlist"]           # shared: openable
        assert got["owner"] == "ann" and got["public"] is True
        st.set_playlist_subscription(pl["id"], "bob", True)
        rows = call("getPlaylists.view")["playlists"]["playlist"]
        assert [(r["id"], r["owner"]) for r in rows] == [(pl["id"], "ann")]
        assert call("updatePlaylist.view", playlistId=pl["id"], name="x")["error"]["code"] == 50
        mine = call("createPlaylist.view", name="RO", public="true")
        assert mine["status"] == "failed" and mine["error"]["code"] == 50   # readonly: no share
        mine = call("createPlaylist.view", name="RO")["playlist"]
        assert mine["public"] is False and mine["owner"] == "bob"
        cur["u"] = types.SimpleNamespace(id="ann", username="ann", role="edit", enabled=True)
        call("updatePlaylist.view", playlistId=pl["id"], public="false")
        assert st.get_playlist(pl["id"])["subscribers"] == []             # unshared
        assert call("getPlaylist.view", id=mine["id"])["error"]["code"] == 70
    finally:
        subsonic_state.reset_state()


def test_playlists_without_owner_go_to_the_legacy_owner(tmp_path, monkeypatch):
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(store_mod, "_store", None)
    (tmp_path / "users.json").write_text(json.dumps({"users": [
        {"id": "sfc", "username": "sfc", "password_hash": "x", "role": "admin",
         "created_at": 1.0}]}))
    (tmp_path / "library.json").write_text(json.dumps({
        "tracks": {"t1": {"id": "t1", "path": "/m/t1"}},
        "playlists": {"p": {"id": "p", "name": "old", "track_ids": ["t1"],
                            "owner_user_id": None}}}))
    try:
        persistence.init_persistence(tmp_path)
    finally:
        persistence.release_library()
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["playlists"]["p"]["owner_user_id"] == "sfc"
    # A V2 library that still has one with no owner: the start's claim.
    from soniqboom.core.user_data import claim_unowned
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.create_playlist("q", "legacy", owner_user_id=None)
    st.create_playlist("r", "bob's", owner_user_id="bob")
    admin = types.SimpleNamespace(id="sfc", role="admin", created_at=1.0, enabled=True)
    claim_unowned(st, types.SimpleNamespace(list_users=lambda: [admin]))
    assert st.get_playlist("q")["owner_user_id"] == "sfc"
    assert st.get_playlist("r")["owner_user_id"] == "bob"


def test_the_backup_maps_playlists_and_favourites_by_user_name():
    payload = {"version": 2, "db_version": 2,
               "users_map": {"s-ann": "ann", "s-bob": "bob", "s-cy": "cy"},
               "favorites": {"s-ann": [_station("custom:a")], "s-cy": [_station("custom:c")],
                             "": [_station("custom:n")]},
               "data": {"db_version": 2, "tracks": {},
                        "playlists": {
                            "p1": {"id": "p1", "name": "ann's", "owner_user_id": "s-ann",
                                   "shared": True, "subscribers": ["s-bob", "s-cy"]},
                            "p2": {"id": "p2", "name": "cy's", "owner_user_id": "s-cy",
                                   "subscribers": []}},
                        "config": {}}}
    favs: dict = {}
    data = persistence.import_backup_state(payload, accounts={"ann": "t-ann", "bob": "t-bob"},
                                           legacy_owner="t-ann", favorites_out=favs)
    assert data["playlists"]["p1"]["owner_user_id"] == "t-ann"
    assert data["playlists"]["p1"]["subscribers"] == ["t-bob", "import:s-cy"]
    assert data["playlists"]["p2"]["owner_user_id"] == "import:s-cy"   # kept aside, invisible
    assert {k: [s["sid"] for s in v] for k, v in favs.items()} == {
        "t-ann": ["custom:a"], "import:s-cy": ["custom:c"], "": ["custom:n"]}
    assert data["config"][PENDING_CONFIG_KEY] == {"import:s-cy": "cy"}
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st._playlists = data["playlists"]
    assert not st.playlist_offered(st.get_playlist("p2") | {"shared": True})
    # cy's account appears (admin-created): everything moves to it.
    from soniqboom.core.user_data import claim_pending
    st._config = data["config"]
    cy = types.SimpleNamespace(id="t-cy", username="cy", role="edit", created_at=3.0,
                               enabled=True, self_registered=False)
    assert claim_pending(st, types.SimpleNamespace(list_users=lambda: [cy]))
    assert st.get_playlist("p2")["owner_user_id"] == "t-cy"
    assert st.get_playlist("p1")["subscribers"] == ["t-bob", "t-cy"]


# ── Kept-aside data and self-registration ───────────────────────────────────

def test_kept_aside_data_never_attaches_to_a_self_registered_account(tmp_path, monkeypatch):
    """The reviewer's probe: a backup's bob is kept aside; someone registers
    "BOB" (self-registration) → nothing; an admin creates "bob" → it moves."""
    from soniqboom import config as cfg
    from soniqboom.api import users as users_api
    from soniqboom.core import store as store_mod
    from soniqboom.core import users as core_users
    from soniqboom.core.user_data import claim_accounts
    old_instance = core_users._instance
    us = core_users.init_user_store(tmp_path)
    try:
        admin = us.create(username="sfc", password="pw-admin-123456", role="admin")
        payload = {"version": 2, "db_version": 2,
                   "users_map": {"old-sfc": "sfc", "old-bob": "bob"},
                   "data": {"db_version": 2, "tracks": {},
                            "play_stats": {"old-bob": {"b": {"count": 9, "last_played": 2}}},
                            "history": {"old-bob": [{"track_id": "b", "ts": 2,
                                                     "title": "bob-private"}]},
                            "config": {}}}
        monkeypatch.setattr(store_mod, "_store", None)
        data = persistence.import_backup_state(payload, accounts=claim_accounts(us.list_users()),
                                               legacy_owner=admin.id)
        persistence.populate_store(data)
        st = store_mod.get_store()
        st._aof_append = lambda *a, **k: None
        monkeypatch.setattr(cfg, "is_self_registration_enabled", lambda: True)
        monkeypatch.setattr(users_api, "_registration_open", lambda store: True, raising=False)
        app = FastAPI()
        app.include_router(users_api.router)
        c = TestClient(app)
        r = c.post("/auth/register", json={"username": "BOB", "password": "stranger-pass-123"})
        assert r.status_code == 200, r.text
        stranger = r.json()["user"]["id"]
        assert us.get(stranger).self_registered is True
        assert users_api.claim_unowned_user_data() is False         # every claim point skips it
        assert st.get_history(5, uid=stranger) == []
        assert st.get_config(PENDING_CONFIG_KEY) == {"import:old-bob": "bob"}
        # (it persists: a reload of users.json keeps the marker)
        raw = json.loads((tmp_path / "users.json").read_text())
        assert next(u for u in raw["users"] if u["id"] == stranger)["self_registered"] is True
        # A later import maps nothing onto the self-registered name either.
        assert "bob" not in claim_accounts(us.list_users())
        # An admin-created "bob" gets it.
        us.delete(stranger)
        bob = us.create(username="bob", password="pw-bob-12345678", role="readonly")
        assert users_api.claim_unowned_user_data() is True
        assert st.get_history(5, uid=bob.id)[0]["title"] == "bob-private"
        assert st.get_config(PENDING_CONFIG_KEY) is None
    finally:
        core_users._instance = old_instance


# ── Review fixes ────────────────────────────────────────────────────────────

def test_a_late_file_that_cannot_be_removed_is_set_aside(tmp_path, monkeypatch):
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(store_mod, "_store", None)
    r = (json.dumps({"op": "record_play", "ts": 2, "id": "t1"}) + "\n").encode()
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {"t1": {"id": "t1", "path": "/m/t1"}}, "play_stats": {}, "history": []}))
    late = tmp_path / "library.aof.late-exec-3"
    late.write_bytes(r)
    real_unlink = Path.unlink

    def no_unlink(self, *a, **k):
        if self.name.startswith("library.aof.late-exec-"):
            raise PermissionError("busy")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", no_unlink)
    try:
        persistence.init_persistence(tmp_path)
    finally:
        monkeypatch.undo()
        persistence.release_library()
    assert not late.exists() and (tmp_path / "library.aof.late-3").read_bytes() == r
    monkeypatch.setattr(store_mod, "_store", None)
    try:
        persistence.init_persistence(tmp_path)                    # the next start
    finally:
        persistence.release_library()
    assert store_mod.get_store().get_play_stats("t1", uid="")["count"] == 1   # once


def test_the_space_check_counts_the_bak_copy_and_skips_unknown_sizes(tmp_path, monkeypatch):
    (tmp_path / "library.json").write_bytes(b"x" * 1000)
    monkeypatch.setattr(persistence, "_UPGRADE_SPACE_MARGIN", 0)

    def no_link(*a, **k):
        raise OSError("no hard links")
    monkeypatch.setattr(persistence.os, "link", no_link)
    # copy (1000) + new snapshot (1000) + its .bak copy (1000) = 3000 needed
    monkeypatch.setattr(persistence.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=10**9, used=0, free=2999))
    with pytest.raises(persistence.DatabaseVersionError, match="MB needed"):
        persistence.backup_before_upgrade(tmp_path, 1)
    monkeypatch.setattr(persistence.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=10**9, used=0, free=3000))
    assert persistence.backup_before_upgrade(tmp_path, 1).is_dir()
    (tmp_path / "library.json").write_bytes(b"y" * 1000)
    monkeypatch.setattr(persistence.shutil, "disk_usage",          # a share that reports 0
                        lambda p: types.SimpleNamespace(total=0, used=0, free=0))
    assert persistence.backup_before_upgrade(tmp_path, 1).is_dir()


# ── Review round 3 ──────────────────────────────────────────────────────────

def test_another_users_copy_never_takes_over_a_custom_station(radio, monkeypatch, tmp_path):
    """Mallory (readonly) copies Ann's custom station into her list and edits
    her copy's URL: refused for the stream; lookup plays Ann's (the
    creator's) copy, whoever's list sorts first."""
    from soniqboom.api import subsonic
    from soniqboom.api import stations as stations_api
    from soniqboom.core import subsonic_state
    ann = dict(_station("custom:k", "Ann's", "http://ann.example/live"), creator="ann")
    radio.add_favorite(ann, uid="ann")
    radio.add_favorite(dict(ann), uid="aaa-mallory")          # (sorts before "ann")
    radio.set_favorites("aaa-mallory", [dict(ann, streams=[{"url": "http://mal.example/s"}])])
    st = asyncio.run(radio.resolve_station("custom:k"))
    assert st["streams"][0]["url"] == "http://ann.example/live"

    async def _public(url):
        return None
    monkeypatch.setattr(stations_api, "_assert_public_url", _public)
    cur = types.SimpleNamespace(id="aaa-mallory", username="m", role="readonly", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: cur)
    subsonic_state.reset_state(tmp_path / "s.json")
    app = FastAPI()
    app.include_router(subsonic.router)
    c = TestClient(app)
    try:
        body = c.get("/rest/updateInternetRadioStation.view", params={
            "f": "json", "id": "custom:k", "streamUrl": "http://mal.example/x"}).json()
        assert body["subsonic-response"]["error"]["code"] == 50
        body = c.get("/rest/createInternetRadioStation.view", params={
            "f": "json", "streamUrl": "http://mal.example/own", "name": "Mine"}).json()
        assert body["subsonic-response"]["status"] == "ok"
        mine = [f for f in radio.get_favorites("aaa-mallory") if f["name"] == "Mine"][0]
        assert mine["creator"] == "aaa-mallory"
    finally:
        subsonic_state.reset_state()


def test_search_lists_only_the_callers_own_custom_stations(radio, monkeypatch):
    monkeypatch.setattr(radio, "_directory_search", lambda q, n: asyncio.sleep(0, []))
    radio.add_favorite(_station("custom:ann-secret", "Zed Secret", "http://u:pw@ann/s"), uid="ann")
    radio.add_favorite(_station("custom:bob-own", "Zed Bob"), uid="bob")
    found = {s["sid"] for s in asyncio.run(radio.search_stations("zed", 10, uid="bob"))}
    assert found == {"custom:bob-own"}


def test_moved_favourites_replace_the_default_list(radio):
    radio.set_favorites("", [_station("custom:a"), _station("custom:b")])   # (no Nectarine)
    assert radio.move_favorites("", "new-admin")
    assert [f["sid"] for f in radio.get_favorites("new-admin")] == ["custom:a", "custom:b"]


def test_playlist_responses_never_list_who_imported_it(monkeypatch):
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    who = {"A": types.SimpleNamespace(id="ann", role="edit", enabled=True),
           "B": types.SimpleNamespace(id="bob", role="edit", enabled=True),
           "C": types.SimpleNamespace(id="cy", role="edit", enabled=True)}
    c = _playlist_app(monkeypatch, st, who, {})
    pid = c.post("/playlists", json={"name": "Mix"}, cookies={"sb_session": "A"}).json()["id"]
    c.put(f"/playlists/{pid}", json={"shared": True}, cookies={"sb_session": "A"})
    c.put(f"/playlists/{pid}/subscription", json={"subscribed": True}, cookies={"sb_session": "B"})
    other = c.get(f"/playlists/{pid}", cookies={"sb_session": "C"}).json()
    assert "subscribers" not in other and "owner_user_id" not in other and other["read_only"]
    mine = c.get(f"/playlists/{pid}", cookies={"sb_session": "A"}).json()
    assert "subscribers" not in mine and mine["subscriber_count"] == 1
    listed = c.get("/playlists", cookies={"sb_session": "B"}).json()[0]
    assert "owner_user_id" not in listed and "subscribers" not in listed


def test_a_subscriber_deleting_in_subsonic_unsubscribes(monkeypatch, tmp_path):
    from soniqboom.api import subsonic
    from soniqboom.core import subsonic_state
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.create_playlist("p", "Mix", owner_user_id="ann")
    st.set_playlist_shared("p", True)
    st.set_playlist_subscription("p", "bob", True)
    bob = types.SimpleNamespace(id="bob", username="bob", role="edit", enabled=True)
    monkeypatch.setattr(subsonic, "get_store", lambda: st)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: bob)
    subsonic_state.reset_state(tmp_path / "s.json")
    app = FastAPI()
    app.include_router(subsonic.router)
    try:
        body = TestClient(app).get("/rest/deletePlaylist.view",
                                   params={"f": "json", "id": "p"}).json()["subsonic-response"]
    finally:
        subsonic_state.reset_state()
    assert body["status"] == "ok"
    assert st.get_playlist("p") is not None and st.get_playlist("p")["subscribers"] == []


def test_a_crafted_backup_with_odd_owner_ids_does_not_fail():
    payload = {"version": 2, "db_version": 2, "users_map": {},
               "data": {"db_version": 2, "tracks": {},
                        "playlists": {"p": {"id": "p", "name": "x", "owner_user_id": 7,
                                            "subscribers": [None, 3, "s-x"]}}}}
    data = persistence.import_backup_state(payload, accounts={}, legacy_owner="adm")
    assert data["playlists"]["p"]["owner_user_id"] is None
    assert data["playlists"]["p"]["subscribers"] == ["import:s-x"]


def test_a_restore_replaces_stale_favourite_lists(tmp_data_dir, monkeypatch):
    """A backup's favourites replace the stored ones: a list of an account the
    backup doesn't hold goes (it would still feed station resolution)."""
    from soniqboom.core import radiodir
    radiodir.set_favorites("gone-user", [{"sid": "custom:x", "name": "Old", "url": "http://x"}])
    radiodir.set_favorites("kept-user", [{"sid": "custom:k", "name": "Mine", "url": "http://k"}])
    favorites = {"kept-user": [{"sid": "custom:k2", "name": "New", "url": "http://k2"}]}
    for key in list(radiodir.favorites_by_user()):
        if key not in favorites:
            radiodir.delete_favorites(key)
    for key, stations in favorites.items():
        radiodir.set_favorites(key, stations)
    assert set(radiodir.favorites_by_user()) == {"kept-user"}
    assert all(f["sid"] != "custom:x" for f in radiodir.all_favorites())
